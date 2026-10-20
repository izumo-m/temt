"""reboot の本体。設計の手順 1〜7 を順に行う。

再起動したことは、インスタンスの boot_id（起動のたびに変わる ID）で見分ける。
reboot-instances の後もインスタンスは running のままで、SSM エージェントの Online の
報告は落ちてからもしばらく残るので、どちらも再起動の前と後を見分けられない。
boot_id は Run Command（send-command）で読む。

起動テンプレートの有無は確かめない。再起動はテンプレートを使わないからである。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from temt import aws, conf, instances, lock, msg, table, wait
from temt.aws import error_code, field
from temt.msg import TemtError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.names import Service

# boot_id を読む Run Command
DOCUMENT = "AWS-RunShellScript"
BOOT_ID_COMMAND = "cat /proc/sys/kernel/random/boot_id"
# SSM エージェントが受け取るまでの締め切り（秒）。send-command が受け付ける最小
DELIVERY_TIMEOUT = 30
# インスタンスの中で実行する締め切り（秒）
EXECUTION_TIMEOUT = 30
# 送った 1 回の Run Command を待つのをやめ、次を送るまで（秒）。コマンドは受け取りと
# 実行の締め切りの和（60 秒）で打ち切られるはずだが、再起動に巻き込まれたものは
# 終わったという報告が遅れることがある
COMMAND_PATIENCE = 60

# 締め切り（秒）。BEFORE は再起動の前に boot_id を読むまで、AFTER は再起動を求めて
# から boot_id が変わったと読めるまで
BEFORE_DEADLINE = 60
AFTER_DEADLINE = 600

# Run Command の状態
PENDING = "Pending"
SUCCESS = "Success"
# まだ終わっていない状態
UNFINISHED = (PENDING, "InProgress", "Delayed", "Cancelling")
# SSM エージェントが受け取れないインスタンスを指したときの誤りの名前
NOT_REACHABLE = "InvalidInstanceId"
# send-command の直後に、まだ実行の記録が無いときの誤りの名前
NO_INVOCATION = "InvocationDoesNotExist"


def _send(ssm: SSMClient, instance_id: str) -> str | None:
    """boot_id を読む Run Command を送り、その ID を返す。受け取れない状態なら None。"""
    try:
        sent = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName=DOCUMENT,
            Parameters={
                "commands": [BOOT_ID_COMMAND],
                "executionTimeout": [str(EXECUTION_TIMEOUT)],
            },
            TimeoutSeconds=DELIVERY_TIMEOUT,
            Comment="temt reboot: read the boot id",
        )
    except ClientError as e:
        if error_code(e) != NOT_REACHABLE:
            raise
        return None
    return field(sent.get("Command") or {}, "CommandId")


def _result(ssm: SSMClient, command_id: str, instance_id: str) -> tuple[str, str, str]:
    """送った Run Command の状態と、標準出力の前後の空白を除いたもの、失敗の様子を返す。

    失敗の様子は、状態に StatusDetails と、標準エラー出力の空でない最初の行を添えた
    もの。まだ実行の記録が無いときと、エージェントが受け取れないと断られたときは、まだ
    終わっていないものとする。
    """
    try:
        found = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    except ClientError as e:
        if error_code(e) not in (NO_INVOCATION, NOT_REACHABLE):
            raise
        return PENDING, "", ""
    status = found.get("Status") or ""
    detail = status or "(no status)"
    if found.get("StatusDetails"):
        detail += f" ({found.get('StatusDetails')})"
    error = (found.get("StandardErrorContent") or "").strip()
    if error:
        detail += f": {error.splitlines()[0].strip()}"
    return status, (found.get("StandardOutputContent") or "").strip(), table.printable(detail)


class _Reader:
    """boot_id を読む Run Command を、1 つずつ送っては結果を見る。

    wait.until に渡す look から、問い合わせのたびに poll を呼ぶ。送ったものが
    COMMAND_PATIENCE を過ぎても終わらないか、成功せずに終われば、次を送る。
    since があれば、SSM エージェントがそれより後に Online と報告するまで送らない。
    offline があれば送れない回に、sending があれば送る直前に呼ぶ。
    """

    def __init__(
        self,
        ssm: SSMClient,
        instance_id: str,
        since: datetime | None = None,
        offline: Callable[[], None] | None = None,
        sending: Callable[[], None] | None = None,
    ) -> None:
        self.ssm = ssm
        self.instance_id = instance_id
        self.since = since
        self.offline = offline
        self.sending = sending
        self.command_id: str | None = None
        self.sent_at = 0.0
        # 成功せずに終わったことを知らせたか
        self.told = False

    def poll(self) -> str | None:
        """読めた boot_id を返す。まだ読めていなければ None。"""
        if self.command_id is None:
            # Online でないインスタンスには送れない
            if not instances.is_online(self.ssm, self.instance_id, self.since):
                if self.offline is not None:
                    self.offline()
                return None
            if self.sending is not None:
                self.sending()
            self.command_id = _send(self.ssm, self.instance_id)
            self.sent_at = wait.clock()
            return None
        status, output, detail = _result(self.ssm, self.command_id, self.instance_id)
        if status in UNFINISHED and wait.clock() - self.sent_at < COMMAND_PATIENCE:
            return None
        command_id, self.command_id = self.command_id, None
        if status == SUCCESS and output:
            return output
        if status not in UNFINISHED and not self.told:
            # 送り直せば読めることが多いが、実行に失敗し続けるときに理由が分かるよう、
            # 最初の 1 回だけ AWS の返した様子を知らせる
            if status == SUCCESS:
                detail += ", but printed nothing"
            msg.info(f"get-command-invocation {command_id}: {detail}; sending it again")
            self.told = True
        return None


def _async_hint(service: Service) -> str:
    """終わりを見分けられないときに添える、待たずに再起動する道。

    インスタンスの状態によらず同じ文を添える。受け付けるかは AWS が決める。
    """
    return (
        f"try 'temt reboot --svc {service.svc} --async'; "
        "it reboots without waiting for the reboot to finish"
    )


def _require_online(ssm: SSMClient, service: Service, instance_id: str) -> None:
    """手順 2。SSM エージェントが Online でなければ、再起動の終わりを見分けられない。"""
    if not instances.is_online(ssm, instance_id):
        raise TemtError(
            f"the SSM agent on {instance_id} is not online, so temt cannot tell when it has "
            f"rebooted; {_async_hint(service)}"
        )


def _boot_id(ssm: SSMClient, service: Service, instance_id: str) -> str:
    """手順 4。再起動の前の boot_id を読む。"""
    return wait.until(
        BEFORE_DEADLINE,
        f"the boot id of {instance_id}",
        _Reader(ssm, instance_id).poll,
        hint=_async_hint(service),
    )


def _wait_rebooted(
    ec2: EC2Client,
    ssm: SSMClient,
    service: Service,
    instance_id: str,
    before: str,
    requested: datetime,
) -> None:
    """手順 7。boot_id が再起動の前と変わったと読めるまで待つ。

    Run Command は、SSM エージェントが再起動を求めた時刻 requested より後に Online と
    報告してから送る。落ちる前のエージェントに送ると、再起動に巻き込まれて
    COMMAND_PATIENCE まで待つことになる。エージェントは起動するとすぐ報告する。
    送れない間は、インスタンスが止まるか終わりに向かっていないかを見て、そうなれば
    締め切りを待たずに終了する。段階が進むたびに 1 回ずつ知らせる。エージェントの
    戻りを待ち始めたとき、戻って boot_id を読み始めたとき、前の boot_id が返って
    きたときである。
    """
    told: set[str] = set()

    def tell_once(key: str, text: str) -> None:
        if key not in told:
            told.add(key)
            msg.info(text)

    def offline() -> None:
        tell_once("offline", f"waiting for the SSM agent on {instance_id} to come back")
        one = instances.look_up(ec2, instance_id)
        if one is not None and one.state in instances.GONE_STATES:
            raise TemtError(
                f"{instance_id} went to {one.state} while waiting for it to reboot{one.because()}"
            )

    def sending() -> None:
        tell_once("back", f"the SSM agent on {instance_id} is back; reading the boot id")

    reader = _Reader(ssm, instance_id, requested, offline, sending)

    def look() -> bool | None:
        now = reader.poll()
        if now is not None and now == before:
            tell_once("old", f"{instance_id} has not rebooted yet; reading the boot id again")
        return True if now is not None and now != before else None

    _ = wait.until(
        AFTER_DEADLINE,
        f"{instance_id} to come back after the reboot",
        look,
        hint=f"see 'temt status --svc {service.svc}'",
    )


def run(service: Service, *, is_async: bool, yes: bool) -> None:
    """サービスのインスタンスを再起動する。

    is_async なら、AWS が再起動の要求を受け付けるまでしか待たない。そうでなければ、
    再起動してログインできるようになるまで待つ。yes なら確認しない。
    """
    values = conf.load(service)
    session = aws.session(values)
    ec2 = session.client("ec2")
    with lock.hold(service):
        # 手順 1。終了の途中のものは、無いものとして扱う
        found = instances.remaining(instances.find(ec2, service))
        if not found:
            raise TemtError(
                f"no instance for {service.name}; run 'temt start --svc {service.svc}' first"
            )
        if len(found) > 1:
            instances.too_many(service, found, "reboot")
        one = found[0]
        # pending と stopping でも再起動を求める。断るかは AWS が決める
        if one.state == instances.STOPPED:
            raise TemtError(f"{one.id} is {one.state}; run 'temt start --svc {service.svc}' first")
        # 手順 2。--async なら待たないので、SSM は使わない
        ssm = None if is_async else session.client("ssm")
        if ssm is not None:
            _require_online(ssm, service, one.id)
        # 手順 3
        aws.confirm(session, f"reboot {one.id} ({one.state})", yes=yes)
        # 手順 4
        before = None if ssm is None else _boot_id(ssm, service, one.id)
        # 手順 5
        with instances.refused(service, "reboot"):
            requested = instances.reboot(ec2, one.id)
        # 手順 6
        if ssm is None or before is None:
            return
        # 手順 7
        _wait_rebooted(ec2, ssm, service, one.id, before, requested)
        msg.info(f"{one.id} is ready for login")
