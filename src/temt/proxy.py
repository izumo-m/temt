"""proxy の本体。ssh の ProxyCommand として動く。

環境変数 TEMT_LOGIN で経路を選び、describe-instances でインスタンスを探し、
起動して間もなければつなげるようになるまで待ち、ssh が使う鍵の公開鍵を EC2 Instance
Connect でインスタンスに送り、known_hosts を用意してから、ssm なら aws-cli を exec
し、direct なら確かめでつないだ接続と標準入出力を中継する。
ssh は ProxyCommand に自分の環境変数を渡すので、TEMT_LOGIN=direct ssh <名前> で
その接続だけ経路を変えられる。ロックは取らない。標準出力は ssh の通信路なので、
何も書かない。待つあいだの知らせは標準エラー出力に書き、ssh がそのまま画面に出す。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from temt import aws, conf, home, instances, keys, msg, proc, relay, table
from temt.msg import TemtError

if TYPE_CHECKING:
    import socket

    import boto3
    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.conf import Conf
    from temt.instances import Instance
    from temt.names import Service

# 経路を選ぶ環境変数と、その値。無いか空なら ssm
LOGIN_ENV = "TEMT_LOGIN"
SSM = "ssm"
DIRECT = "direct"
# 経路ごとに使う外部コマンド。実行前に有無を確かめる。どちらの経路も、送る鍵を
# ssh -G で調べる
AWS_CLI = "aws"
PLUGIN = "session-manager-plugin"
COMMANDS = {SSM: (keys.SSH, AWS_CLI, PLUGIN), DIRECT: (keys.SSH,)}
# Session Manager で ssh の通信路を通す文書
SSH_DOCUMENT = "AWS-StartSSHSession"
# direct でつながらないときに添える手がかり。拒まれたならパケットは届いていて、
# sshd が受け付けていない。他の理由なら、途中で落とされている
SSHD_HINT = "sshd is not accepting"
RULES_HINT = "check the security group ingress rules in the config file"
# 起動して間もないインスタンスにつながらないまま締め切りを過ぎたときに添える。Host
# ブロックの Port と、インスタンスの sshd のポートが違うことがある
PORT_HINT = "the Port of the Host block may differ from the ports sshd listens on; try ssh -p"

# プロセスを置き換える。テストから差し替える
execvpe = os.execvpe


def _ssm_argv(values: Conf, instance_id: str, port: int) -> list[str]:
    """Session Manager 経由でつなぐ aws-cli のコマンドライン。

    リージョンは引数で明示的に渡す。プロファイルは、--profile か AwsProfile が
    あるときだけ引数で渡す。無いときは _align_profile が環境で揃える。
    """
    argv = [
        AWS_CLI,
        "ssm",
        "start-session",
        "--target",
        instance_id,
        "--document-name",
        SSH_DOCUMENT,
        "--parameters",
        f"portNumber={port}",
        "--region",
        values.AwsRegion,
    ]
    profile = aws.profile(values)
    if profile:
        argv += ["--profile", profile]
    return argv


def _align_profile(values: Conf, env: dict[str, str]) -> None:
    """aws-cli に渡す環境のプロファイルを、boto3 が選んだものに揃える。

    --profile か AwsProfile があれば引数で渡すので、何もしない。無ければ、
    プロファイルの環境変数のうち boto3 が選ぶものを、両方の変数に入れる。読む順が
    boto3 と aws-cli で逆なので、両方あって値が違うと食い違うからである。引数で
    渡さないのは、明示したプロファイルがあると aws-cli が環境変数の鍵や web identity
    の環境変数を読まなくなり、それらを先に使う boto3 と食い違うからである。
    """
    if aws.profile(values):
        return
    chosen = next((env[name] for name in aws.PROFILE_ENV if env.get(name)), None)
    if chosen:
        for name in aws.PROFILE_ENV:
            env[name] = chosen


def _login() -> str:
    """環境変数 TEMT_LOGIN が選ぶ経路。無いか空なら ssm。他の値はエラーにする。

    空を無いのと同じに扱うのは、設定ファイルの項目と揃えるためである。
    """
    value = os.environ.get(LOGIN_ENV) or SSM
    if value not in COMMANDS:
        raise TemtError(
            f"{LOGIN_ENV} must be one of {', '.join(COMMANDS)}: {table.printable(value)}"
        )
    return value


def _running(ec2: EC2Client, service: Service) -> tuple[Instance, bool]:
    """サービスのインスタンスを 1 台に決め、running になるまで待って返す。

    起動して間もないか（pending だったか、起動からログインできるまでの締め切りの
    内にあるか）を添える。無いか 2 台以上あるか、running に向かわない状態なら終了
    する。
    """
    # 終了の途中のものは、無いものとして扱う
    found = instances.remaining(instances.find(ec2, service))
    if not found:
        raise TemtError(
            f"no instance for {service.name}; run 'temt start --svc {service.svc}' first"
        )
    if len(found) > 1:
        instances.too_many(service, found, "connect")
    one = found[0]
    instances.require_usable(service, one)
    if one.state == instances.PENDING:
        msg.info(f"{one.id} is {instances.PENDING}; waiting for it")
        return instances.wait_running(ec2, one.id), True
    return one, instances.recent(one)


def _ssm_ready(ssm: SSMClient, service: Service, one: Instance, *, fresh: bool) -> None:
    """SSM エージェントが Online になるのを確かめる。起動して間もなければ待つ。

    起動して間もなければ、起動した時刻より後の Online の報告を待つ（停止してすぐ
    再開したときに、止まる前の報告を数えない）。
    """
    instance_id = one.id
    hint = instances.login_hint(service)
    if fresh:
        instances.wait_online(
            ssm,
            instance_id,
            f"waiting for the SSM agent on {instance_id}",
            since=one.launched,
            hint=hint,
        )
    elif not instances.is_online(ssm, instance_id):
        raise TemtError(f"the SSM agent on {instance_id} is not online; {hint}")


def _send_key(session: boto3.Session, one: Instance, user: str, public_key: str) -> None:
    """ssh が使う鍵の公開鍵を、EC2 Instance Connect でインスタンスに送る。

    EC2 にキーペアは登録しないので、AMI の authorized_keys に無い鍵では、送らなければ
    入れない。送った鍵が認められるのは 60 秒だけなので、つなぐ直前に、つなぐたびに送る。
    """
    connect = session.client("ec2-instance-connect")
    _ = connect.send_ssh_public_key(InstanceId=one.id, InstanceOSUser=user, SSHPublicKey=public_key)


def _hint(reason: OSError | None) -> str:
    """direct でつながらなかった理由に合う手がかり。"""
    return SSHD_HINT if isinstance(reason, ConnectionRefusedError) else RULES_HINT


def _direct_ready(ec2: EC2Client, one: Instance, port: int, *, fresh: bool) -> socket.socket:
    """パブリック IPv4 アドレスのポートに TCP でつなぎ、つないだソケットを返す。

    起動して間もなければ、アドレスが付き、ポートに TCP でつながるまで待つ。
    """
    address = one.public_ipv4
    if not address:
        if not fresh:
            raise TemtError(f"{one.id} has no public IPv4 address")
        address = instances.wait_address(ec2, one.id, f"waiting for a public address on {one.id}")
    if fresh:
        try:
            return instances.wait_accepting(
                address, port, f"waiting for {address} to accept connections on port {port}"
            )
        except instances.NotAccepting as e:
            raise TemtError(f"{e}; {_hint(e.reason)}; {PORT_HINT}") from None
    try:
        return instances.connect(address, port)
    except ConnectionRefusedError:
        raise TemtError(f"{address} refused connections on port {port}; {SSHD_HINT}") from None
    except OSError as e:
        raise TemtError(f"cannot connect to {address} port {port}: {e}; {RULES_HINT}") from None


def run(service: Service, port: int, user: str | None = None) -> None:
    """インスタンスへの通信路を開く。

    ssm なら戻らず、aws-cli に置き換わる。direct なら、接続が閉じられるまで中継して
    から戻る。

    インスタンスが起動して間もなければ、その経路でつなげるようになるまで待つ。ssm
    なら SSM エージェントが Online になるまで、direct ならパブリック IPv4 アドレスが
    付き、そのポートに TCP でつながるまでである。起動から時間がたっていれば待たず、
    つなげなければ手がかりを添えて終了する。送る鍵は、AWS を呼ぶ前に決める。

    user は ssh がログインするユーザー（ProxyCommand の %r）。ssh -G には ssh の
    コマンドラインで変えたユーザーが見えないので、ProxyCommand から受け取る。省けば
    ssh -G のユーザーを使う。
    """
    route = _login()
    values = conf.load(service)
    for command in COMMANDS[route]:
        proc.require(command)
    user, path = keys.offered(service.name, user)
    public_key = keys.read_public(path)
    session = aws.session(values)
    ec2 = session.client("ec2")
    one, fresh = _running(ec2, service)
    if route == DIRECT:
        with _direct_ready(ec2, one, port, fresh=fresh) as connection:
            _send_key(session, one, user, public_key)
            home.use_known_hosts(service, one.id)
            relay.run(connection)
        return
    _ssm_ready(session.client("ssm"), service, one, fresh=fresh)
    _send_key(session, one, user, public_key)
    argv = _ssm_argv(values, one.id, port)
    env = dict(os.environ)
    # aws-cli の出力をページャに渡さない
    env["AWS_PAGER"] = ""
    _align_profile(values, env)
    home.use_known_hosts(service, one.id)
    # exec の後は Python の出力バッファが失われるので、知らせたものを先に流す。標準エラー
    # 出力が閉じていても落ちない
    msg.flush()
    execvpe(argv[0], argv, env)
