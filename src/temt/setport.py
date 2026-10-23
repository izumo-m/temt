"""ssh set-port の本体。設計の手順 1〜8 を順に行う（design/commands/ssh.md）。

焼く前に止まる条件と Host ブロックを書けるかを先に確かめてから、確認を求め、
インスタンスを起動し（start と同じ）、Run Command で sshd の待ち受けポートを変える
スクリプト（sshd.script）を動かし、焼き（bake と同じ）、Host ブロックの Port を書き換え、
受信規則の変え方を案内する。失敗しても元に戻さず、焼かず、Port も書き換えない。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from temt import aws, bake, conf, home, instances, lock, msg, resources, sshd, start, wait
from temt.aws import error_code, field
from temt.msg import TemtError
from temt.names import PREFIX, defaults_path, ssh_dir
from temt.table import printable, tilde

if TYPE_CHECKING:
    from collections.abc import Sequence

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.conf import Conf, Ingress
    from temt.names import Service

DOCUMENT = "AWS-RunShellScript"
# SSM エージェントが受け取るまでの締め切り（秒）
DELIVERY_TIMEOUT = 60
# インスタンスの中で実行する締め切り（秒）。スクリプトは、sshd を読み直させて待ち受けを
# 待つ 10 秒ほどで終わる
EXECUTION_TIMEOUT = 300
# 手元で結果を待つ締め切り（秒）。受け取りと実行の締め切りより長くする
DEADLINE = DELIVERY_TIMEOUT + EXECUTION_TIMEOUT + 60
# Run Command の状態
SUCCESS = "Success"
UNFINISHED = ("Pending", "InProgress", "Delayed", "Cancelling")
# 起動の直後に、SSM エージェントがまだ受け取れないときの誤りの名前
NOT_REACHABLE = "InvalidInstanceId"
# send-command の直後に、まだ実行の記録が無いときの誤りの名前
NO_INVOCATION = "InvocationDoesNotExist"


def _ports(ports: Sequence[int]) -> str:
    """ポートの並びの表示。, でつなぐ。"""
    return ",".join(str(port) for port in ports)


def _plan(service: Service, ports: Sequence[int]) -> list[str]:
    """手順 3 で挙げる、これからすること。"""
    config = printable(tilde(ssh_dir() / "config"))
    return [
        f"start {service.name} if it is not running",
        f"set the ports sshd listens on to {_ports(ports)} through Run Command",
        "bake the instance, apps not yet baked included; the instance is terminated",
        f"write Port {ports[0]} in the Host {service.name} block of {config}",
    ]


def _send(ssm: SSMClient, instance_id: str, ports: Sequence[int]) -> str | None:
    """スクリプトを送り、Run Command の ID を返す。エージェントが受け取れなければ None。"""
    try:
        sent = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName=DOCUMENT,
            Parameters={
                "commands": sshd.script(ports),
                "executionTimeout": [str(EXECUTION_TIMEOUT)],
            },
            TimeoutSeconds=DELIVERY_TIMEOUT,
            Comment="temt set-port: set the sshd ports",
        )
    except ClientError as e:
        if error_code(e) != NOT_REACHABLE:
            raise
        return None
    return field(sent.get("Command") or {}, "CommandId")


class _Run:
    """スクリプトを 1 回送り、終わるまで結果を見る。wait.until に poll を渡す。

    起動の直後は、エージェントが Online でも受け取れずに断られることがあり、そのときは
    次の回に送り直す。終われば、状態、標準出力、標準エラー出力を返す。
    """

    def __init__(self, ssm: SSMClient, instance_id: str, ports: Sequence[int]) -> None:
        self.ssm = ssm
        self.instance_id = instance_id
        self.ports = ports
        self.command_id: str | None = None

    def poll(self) -> tuple[str, str, str] | None:
        if self.command_id is None:
            self.command_id = _send(self.ssm, self.instance_id, self.ports)
            return None
        try:
            found = self.ssm.get_command_invocation(
                CommandId=self.command_id, InstanceId=self.instance_id
            )
        except ClientError as e:
            if error_code(e) not in (NO_INVOCATION, NOT_REACHABLE):
                raise
            return None
        status = str(found.get("Status") or "")
        if status in UNFINISHED:
            return None
        return (
            status,
            str(found.get("StandardOutputContent") or ""),
            str(found.get("StandardErrorContent") or ""),
        )


def _show(output: str, error: str) -> None:
    """スクリプトの標準出力と標準エラー出力を 1 行ずつ知らせる。"""
    for line in output.splitlines():
        if line.strip():
            msg.info(printable(line))
    for line in error.splitlines():
        if line.strip():
            msg.warn(printable(line))


def _failed(service: Service, instance_id: str, found: sshd.Outcome, status: str) -> TemtError:
    """スクリプトが失敗したときの誤り。失敗した手順で直し方を分ける。"""
    running = f"{instance_id} is left running and costs money"
    previous = _ports(found.before) or "its previous ports"
    if found.last.startswith(sshd.FAILED_BEFORE):
        login = (
            f"ssh -p {found.before[0]} {service.name}" if found.before else f"ssh {service.name}"
        )
        return TemtError(
            f"set-port did not change the sshd ports ({printable(found.last)}); sshd still "
            f"listens on {previous}, so log in with '{login}' and fix the files with Port "
            f"lines; until then, do not reboot, stop or bake it; then run set-port again; "
            f"{running}"
        )
    if found.last.startswith(sshd.FAILED_AFTER):
        # 直し方は添えない。何が起きたかだけを知らせる
        return TemtError(
            f"set-port changed the sshd ports, but {printable(found.last)}; sshd may listen on "
            f"no port; {running}"
        )
    return TemtError(
        f"the Run Command that sets the sshd ports ended with {status or '(no status)'}; {running}"
    )


def _run_script(
    ssm: SSMClient, service: Service, instance_id: str, ports: Sequence[int]
) -> sshd.Outcome:
    """手順 5。スクリプトを動かし、終わるまで待つ。成功しなければ直し方を添えて止める。"""
    run = _Run(ssm, instance_id, ports)
    try:
        status, output, error = wait.until(
            DEADLINE, f"the sshd ports to be set on {instance_id}", run.poll
        )
    except TemtError as e:
        # 締め切りを過ぎても、インスタンスではまだ走っていることがある
        sent = f"the Run Command {run.command_id}" if run.command_id else "no Run Command"
        raise TemtError(
            f"{e}; {sent} was sent; see Run Command in Systems Manager; "
            f"{instance_id} is left running and costs money"
        ) from None
    _show(output, error)
    found = sshd.outcome(output)
    if status != SUCCESS or not found.succeeded:
        raise _failed(service, instance_id, found, status)
    return found


def _covers(rule: Ingress, port: int) -> bool:
    """規則が tcp のそのポートを通すか。"""
    if rule.Protocol != conf.TCP or rule.Port is None:
        return False
    low, high = rule.Port
    return low <= port <= high


def _uses_temt_group(ec2: EC2Client, service: Service) -> bool:
    """起動テンプレートの既定バージョンが、temt のセキュリティグループを使っているか。"""
    data = resources.default_version(resources.versions(ec2, service), service).get(
        "LaunchTemplateData"
    )
    data = data or {}
    used = set(data.get("SecurityGroupIds") or [])
    for one in data.get("NetworkInterfaces") or []:
        used.update(one.get("Groups") or [])
    return any(field(group, "GroupId") in used for group in resources.groups(ec2, service))


def _guide(
    ec2: EC2Client, service: Service, values: Conf, before: Sequence[int], ports: Sequence[int]
) -> None:
    """手順 8。受信規則の変え方を案内する。直接接続のときだけ要る。temt は変えない。"""
    msg.info("for direct connection (TEMT_LOGIN=direct), the ingress rules must allow the ports")
    if not _uses_temt_group(ec2, service):
        msg.info(
            "the launch template uses no security group of temt, so sg does not apply; "
            "change the rules of its security groups in AWS"
        )
        return
    own = conf.load(service, common=False).security_group.ingress
    shared = [] if not values.Defaults else conf.common_rules()
    new = [port for port in ports if not any(_covers(rule, port) for rule in [*own, *shared])]
    if not new:
        msg.info(f"the rules already allow {_ports(ports)}; no rule is needed")
        return
    old = [port for port in before if port not in ports]
    # 前のポートの規則の送信元を、新しいポートにも使う。前のポートを残すときも同じ
    sources = [rule for rule in own if any(_covers(rule, port) for port in before)]
    cidrs = list(dict.fromkeys(rule.Cidr for rule in sources))
    removed = [rule for rule in sources if not any(_covers(rule, port) for port in ports)]
    command = f"{PREFIX}-{service.svc}"
    lines: list[str] = []
    for port in new:
        for cidr in cidrs or [None]:
            lines.append(f"{command} sg add {port}" + (f" {cidr}" if cidr else ""))
    for rule in removed:
        lines.append(f"{command} sg remove {conf.port_text(rule.Protocol, rule.Port)} {rule.Cidr}")
    lines.append(f"{command} apply")
    msg.tell("".join(f"  {line}\n" for line in lines))
    if any(_covers(rule, port) for rule in shared for port in old):
        msg.info(
            f"the rules for {_ports(old)} in {printable(tilde(defaults_path()))} apply to every "
            "service; sg remove cannot remove them, so edit that file"
        )


def run(service: Service, ports: Sequence[int], *, yes: bool) -> None:
    """インスタンスの sshd が待ち受けるポートを ports にし、焼き、Host ブロックの Port を
    先頭のポートにする。yes なら確認しない。
    """
    values = conf.load(service)
    # 手順 1。ImageId を書いていれば、焼けないので何もせずに止まる
    bake.check_unfixed(service, values)
    session = aws.session(values)
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    with lock.hold(service):
        # 手順 2。sshd を変えてから止まると、変えたが焼いていない状態が残るので、焼くときに
        # 止まる条件と、Host ブロックを書けない条件を先に確かめる
        resources.require_template(ec2, service)
        home.check_owner(service)
        _ = home.detect_port(service, ports[0])
        built = bake.check_template(ec2, service, values)
        found = instances.remaining(instances.find(ec2, service))
        if len(found) > 1:
            instances.too_many(service, found, "set-port")
        if found:
            bake.check_size(ec2, service, built, found[0], command="set-port")
        # 手順 3
        planned = "".join(f"  {line}\n" for line in _plan(service, ports))
        msg.tell(f"set-port {service.name}:\n{planned}")
        aws.confirm(session, f"set the sshd ports of {service.name} to {_ports(ports)}", yes=yes)
        # 手順 4。SSM を使うので、WaitForSsm によらずエージェントを待つ
        instance_id = start.bring_up(
            session, service, is_async=False, wait_ssm=True, command="set-port"
        )
        # 手順 5
        done = _run_script(ssm, service, instance_id, ports)
        # 手順 6
        one = instances.look_up(ec2, instance_id)
        if one is None:
            raise TemtError(f"{instance_id} is not found; run set-port again")
        bake.make(ec2, service, one)
        # 手順 7
        if home.write_edit(home.detect_port(service, ports[0])):
            msg.info(
                f"wrote Port {ports[0]} in the Host {service.name} block of "
                f"{printable(tilde(ssh_dir() / 'config'))}"
            )
        # 手順 8
        _guide(ec2, service, values, done.before, ports)
