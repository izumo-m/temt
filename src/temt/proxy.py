"""proxy の本体。ssh の ProxyCommand として動く。

環境変数 TEMT_LOGIN で経路を選び、describe-instances でインスタンスを探し、
起動して間もなければつなげるようになるまで待ち、known_hosts を用意してから、ssm
なら aws-cli を exec し、direct なら確かめでつないだ接続と標準入出力を中継する。
ssh は ProxyCommand に自分の環境変数を渡すので、TEMT_LOGIN=direct ssh <名前> で
その接続だけ経路を変えられる。ロックは取らない。標準出力は ssh の通信路なので、
何も書かない。待つあいだの知らせは標準エラー出力に書き、ssh がそのまま画面に出す。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from temt import aws, conf, home, instances, msg, proc, relay, sshd, table
from temt.msg import TemtError

if TYPE_CHECKING:
    import socket

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.conf import Conf
    from temt.instances import Instance
    from temt.names import Service

# 経路を選ぶ環境変数と、その値。無いか空なら ssm
LOGIN_ENV = "TEMT_LOGIN"
SSM = "ssm"
DIRECT = "direct"
# 経路ごとに使う外部コマンド。実行前に有無を確かめる。direct は使わない
AWS_CLI = "aws"
PLUGIN = "session-manager-plugin"
COMMANDS = {SSM: (AWS_CLI, PLUGIN), DIRECT: ()}
# Session Manager で ssh の通信路を通す文書
SSH_DOCUMENT = "AWS-StartSSHSession"
# direct でつながらないときに添える手がかり。拒まれたならパケットは届いていて、
# sshd が受け付けていない。他の理由なら、途中で落とされている
SSHD_HINT = "sshd is not accepting"
RULES_HINT = "check the security group ingress rules in the config file"

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

    起動して間もなければ、起動した時刻より後の Online の報告を待ち（停止してすぐ
    再開したときに、止まる前の報告を数えない）、続けて sshd の待ち受けポートが揃う
    のを待つ。
    """
    instance_id = one.id
    if fresh:
        instances.wait_online(
            ssm, instance_id, f"waiting for the SSM agent on {instance_id}", since=one.launched
        )
        _sshd_ready(ssm, service, instance_id)
    elif not instances.is_online(ssm, instance_id):
        raise TemtError(
            f"the SSM agent on {instance_id} is not online; "
            f"see 'temt status --svc {service.svc}' or "
            f"run 'temt reboot --svc {service.svc} --async'"
        )


def _sshd_ready(ssm: SSMClient, service: Service, instance_id: str) -> None:
    """関連付け <名前>-sshd の今の版が、起動したインスタンスで実行し終わるのを待つ。

    それまでは sshd が AMI のポートで待ち受けているので、SshdPorts のポートへは
    つながらない。今の版は 1 度だけ読む。関連付けそのものが無いときと、終わった状態が
    Success でないときは、警告してそのままつなぐ。Success でないときはスクリプトが前の
    ポートに戻しているので、ssh -p でそのポートを指せば入って直せる。
    """
    name = service.sshd_association
    current = sshd.find(ssm, service)
    if current is None:
        msg.warn(
            f"no association {name}; sshd listens on the ports in the AMI; "
            f"run 'temt apply --svc {service.svc}'"
        )
        return
    state = instances.wait_sshd_ports(
        ssm,
        service,
        instance_id,
        current,
        f"waiting for {name} to set the sshd ports on {instance_id}",
    )
    if state != sshd.SUCCESS:
        msg.warn(
            f"{name} ended with {state} on {instance_id}; sshd stays on its previous ports; "
            f"see 'temt status --svc {service.svc}'"
        )


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
            raise TemtError(f"{e}; {_hint(e.reason)}") from None
    try:
        return instances.connect(address, port)
    except ConnectionRefusedError:
        raise TemtError(f"{address} refused connections on port {port}; {SSHD_HINT}") from None
    except OSError as e:
        raise TemtError(f"cannot connect to {address} port {port}: {e}; {RULES_HINT}") from None


def run(service: Service, port: int) -> None:
    """インスタンスへの通信路を開く。

    ssm なら戻らず、aws-cli に置き換わる。direct なら、接続が閉じられるまで中継して
    から戻る。

    インスタンスが起動して間もなければ、その経路でつなげるようになるまで待つ。ssm
    なら SSM エージェントが Online になり、sshd の待ち受けポートを揃える関連付けが
    実行し終わるまで、direct ならパブリック IPv4 アドレスが付き、そのポートに TCP で
    つながるまでである。起動から時間がたっていれば待たず、
    つなげなければ手がかりを添えて終了する。
    """
    route = _login()
    values = conf.load(service)
    for command in COMMANDS[route]:
        proc.require(command)
    session = aws.session(values)
    ec2 = session.client("ec2")
    one, fresh = _running(ec2, service)
    if route == DIRECT:
        with _direct_ready(ec2, one, port, fresh=fresh) as connection:
            home.use_known_hosts(service, one.id)
            relay.run(connection)
        return
    _ssm_ready(session.client("ssm"), service, one, fresh=fresh)
    argv = _ssm_argv(values, one.id, port)
    env = dict(os.environ)
    # aws-cli の出力をページャに渡さない
    env["AWS_PAGER"] = ""
    _align_profile(values, env)
    home.use_known_hosts(service, one.id)
    # exec の後は Python の出力バッファが失われるので、知らせたものを先に流す
    sys.stderr.flush()
    execvpe(argv[0], argv, env)
