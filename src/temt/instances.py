"""インスタンスの探索と起動と停止と終了、そして状態が変わるまでの待ち合わせ。

start、stop、terminate、reboot、bake、destroy、proxy、status が使う。待ち合わせの繰り返しは wait が
持ち、ここには締め切りと、何を待つかだけを書く。インスタンスがつなげる状態かを
確かめる is_online と、つないで接続を返す connect は、proxy が待たずに 1 回だけ
呼ぶのにも使う。

botocore の例外は、起動テンプレートやインスタンスが無いことと、インスタンスの状態が
要求に合わないことを表す誤りだけをここで扱い、他は cli.run にそのまま渡す。
"""

from __future__ import annotations

import socket
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, NoReturn

from botocore.exceptions import ClientError

from temt import msg, resources, sshd, wait
from temt.aws import error_code, field
from temt.msg import TemtError
from temt.table import aws_error, printable

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.names import Service

PENDING = "pending"
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
SHUTTING_DOWN = "shutting-down"
TERMINATED = "terminated"
# 探すインスタンスの状態。terminated は片付いたものなので入れない
SEARCH_STATES = (PENDING, RUNNING, STOPPING, STOPPED, SHUTTING_DOWN)
# start が再開を求める状態
RESUMABLE_STATES = (STOPPING, STOPPED)
# 動いているとみなすインスタンスの状態。使えるのも、アプリが状態を書けるのもこのとき
USABLE_STATES = (PENDING, RUNNING)
# running を待つ間にこのどれかになったら、待っても running にはならない
GONE_STATES = (STOPPING, STOPPED, SHUTTING_DOWN, TERMINATED)
# stopped を待つ間にこのどれかになったら、待っても stopped にはならない
ENDING_STATES = (SHUTTING_DOWN, TERMINATED)
# SSM エージェントが応答しているときの報告
ONLINE = "Online"

# 締め切り（秒）
RUNNING_DEADLINE = 600
STOPPED_DEADLINE = 600
LOGIN_DEADLINE = 300
# 起動後に、sshd の待ち受けポートを揃える関連付けが実行し終わるまで
SSHD_DEADLINE = 300
TERMINATED_DEADLINE = 600
# 再開を求めてから、describe-instances が前の状態を返さなくなるまで
LEAVE_DEADLINE = 60
# ポートに 1 回つなぐときの締め切り（秒）
CONNECT_TIMEOUT = 5

# その ID のインスタンスが見えないときに describe-instances が返す誤りの名前
NOT_FOUND = "InvalidInstanceID.NotFound"
# インスタンスの状態が合わないために、AWS が状態を変える要求を断ったときの誤りの名前
WRONG_STATE = ("IncorrectInstanceState", "IncorrectState")


@dataclass(frozen=True)
class Instance:
    """見つかったインスタンスの、temt が見る部分。"""

    id: str
    state: str
    # パブリック IPv4 アドレス。まだ付いていなければ空文字列
    public_ipv4: str
    # 終了しても消えない EBS ボリュームのデバイス名
    kept_devices: tuple[str, ...]
    # 起動してからの秒数。AWS の応答の時刻から数えるので、ふつうは手元の時計に左右
    # されない。起動時刻が応答に無ければ None
    age: float | None
    # 起動した時刻。停止したものを再開すると、再開した時刻になる。応答に無ければ None
    launched: datetime | None = None
    # 状態が変わった理由（StateReason の Message）。応答に無ければ空文字列
    reason: str = ""
    # 起動に使った AMI の ID。応答に無ければ空文字列
    image_id: str = ""
    # ルートボリュームの ID。応答に無ければ空文字列
    root_volume: str = ""

    def because(self) -> str:
        """状態を述べる文に添える、変わった理由。理由が無ければ空文字列。"""
        return f" ({printable(self.reason)})" if self.reason else ""


def _aware(moment: datetime) -> datetime:
    """時差の無い datetime を UTC として読む。時差があればそのまま返す。"""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _response_time(found: Mapping[str, Any]) -> datetime:
    """応答の Date ヘッダの時刻。読めなければ手元の時刻。"""
    date = ((found.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}).get("date")
    if isinstance(date, str):
        try:
            # 時差を -0000 と書いた時刻は、時差の無い datetime になる
            return _aware(parsedate_to_datetime(date))
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


def _root_volume(item: Mapping[str, Any]) -> str:
    """describe-instances の応答の 1 台分の、ルートボリュームの ID。無ければ空文字列。

    BlockDeviceMappings のうち、DeviceName が RootDeviceName のものを見る。
    """
    device = item.get("RootDeviceName")
    for mapping in item.get("BlockDeviceMappings") or []:
        if device and mapping.get("DeviceName") == device:
            return str((mapping.get("Ebs") or {}).get("VolumeId") or "")
    return ""


def _one(item: Mapping[str, Any], now: datetime) -> Instance:
    """describe-instances の応答の 1 台分を読む。now はその応答の時刻。"""
    launch_time = item.get("LaunchTime")
    launched = _aware(launch_time) if isinstance(launch_time, datetime) else None
    return Instance(
        id=field(item, "InstanceId"),
        state=field(item.get("State") or {}, "Name"),
        public_ipv4=item.get("PublicIpAddress") or "",
        kept_devices=tuple(
            field(mapping, "DeviceName")
            for mapping in item.get("BlockDeviceMappings") or []
            if (mapping.get("Ebs") or {}).get("DeleteOnTermination") is False
        ),
        age=(now - launched).total_seconds() if launched is not None else None,
        launched=launched,
        reason=(item.get("StateReason") or {}).get("Message") or "",
        image_id=str(item.get("ImageId") or ""),
        root_volume=_root_volume(item),
    )


def _read(found: Mapping[str, Any]) -> list[Instance]:
    """応答のすべての予約からインスタンスを取り出し、ID の順に並べる。"""
    now = _response_time(found)
    return sorted(
        (
            _one(item, now)
            for reservation in found.get("Reservations") or []
            for item in reservation.get("Instances") or []
        ),
        key=lambda one: one.id,
    )


def find(ec2: EC2Client, service: Service) -> list[Instance]:
    """そのサービスのインスタンスを探す。

    1 サービスにつき 1 台なので、ページは繰らない。2 台以上あれば、呼んだ側が
    それを見て決める。
    """
    return _read(
        ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [service.name]},
                {"Name": "instance-state-name", "Values": list(SEARCH_STATES)},
            ]
        )
    )


def remaining(found: list[Instance]) -> list[Instance]:
    """終了の途中（shutting-down）のものを除く。terminate と destroy のほかが使う。

    終わりかけのものは、terminated と同じく無いものとして扱う。2 台以上かどうかも、
    これを除いて数える。
    """
    return [one for one in found if one.state != SHUTTING_DOWN]


def extra(service: Service, count: int) -> str:
    """2 台以上見つかったことと、ID を指して余分なものを終了する書き方。"""
    ones = "one" if count == 2 else "ones"
    return (
        f"{count} instances for {service.name}; terminate the extra {ones} with "
        f"'temt terminate --svc {service.svc} <instance-id>'"
    )


def tell_found(service: Service, found: list[Instance]) -> None:
    """見つかったインスタンスを、1 台 1 行で挙げる。"""
    msg.tell(f"instances tagged Name={service.name}:\n")
    for one in found:
        msg.tell(f"  {one.id} ({one.state})\n")


def too_many(service: Service, found: list[Instance], command: str) -> NoReturn:
    """2 台以上見つかったときに、挙げて終了する。片付けるのは terminate である。

    command は、余分なものを終了した後にやり直すコマンドの名前。
    """
    tell_found(service, found)
    raise TemtError(f"{extra(service, len(found))} and {command} again")


def require_usable(service: Service, one: Instance) -> None:
    """pending か running でなければ、状態を述べて終了する。proxy が使う。

    remaining で除いた後のものを受け取るので、残るのは stopped と stopping である。
    どちらも start で再開させる。start は停止の途中でも再開を求める。
    """
    if one.state in USABLE_STATES:
        return
    raise TemtError(f"{one.id} is {one.state}; run 'temt start --svc {service.svc}' first")


def recent(one: Instance) -> bool:
    """起動してから、ログインできるようになるまでの締め切りの内にあるか。"""
    return one.age is not None and one.age < LOGIN_DEADLINE


def launch(ec2: EC2Client, service: Service) -> str:
    """起動テンプレートの既定バージョンから 1 台起動し、その ID を返す。

    タグは起動テンプレートが付けるので、ここでは付けない。
    """
    try:
        made = ec2.run_instances(
            LaunchTemplate={
                "LaunchTemplateName": service.launch_template,
                "Version": resources.DEFAULT_VERSION,
            },
            MinCount=1,
            MaxCount=1,
            # botocore が再試行しても 2 台立たないよう、呼ぶたびに 1 つ作る
            ClientToken=uuid.uuid4().hex,
        )
    except ClientError as e:
        if error_code(e) != resources.NO_SUCH_TEMPLATE:
            raise
        raise resources.not_applied(service) from None
    started = made.get("Instances") or []
    if not started:
        raise TemtError(f"AWS did not start an instance from {service.launch_template}")
    instance_id = field(started[0], "InstanceId")
    msg.info(f"launched {instance_id} from {service.launch_template}")
    return instance_id


def delete_volumes(ec2: EC2Client, one: Instance) -> None:
    """終了しても消えない EBS ボリュームを、消えるようにする。

    失敗しても続ける。そのボリュームが残ることを知らせる。
    """
    for device in one.kept_devices:
        try:
            _ = ec2.modify_instance_attribute(
                InstanceId=one.id,
                BlockDeviceMappings=[{"DeviceName": device, "Ebs": {"DeleteOnTermination": True}}],
            )
        except ClientError as e:
            msg.warn(f"{one.id}: {device} will remain after the instance is gone: {aws_error(e)}")
        else:
            msg.info(f"{one.id}: {device} will be deleted with the instance")


def terminate(ec2: EC2Client, instance_id: str) -> None:
    """インスタンスの終了を要求する。"""
    _ = ec2.terminate_instances(InstanceIds=[instance_id])
    msg.info(f"terminating {instance_id}")


def reboot(ec2: EC2Client, instance_id: str) -> datetime:
    """インスタンスの再起動を要求し、AWS がそれを受け付けた時刻（応答の時刻）を返す。"""
    requested = _response_time(ec2.reboot_instances(InstanceIds=[instance_id]))
    msg.info(f"rebooting {instance_id}")
    return requested


def stop(ec2: EC2Client, instance_id: str) -> None:
    """インスタンスの停止を要求する。"""
    _ = ec2.stop_instances(InstanceIds=[instance_id])
    msg.info(f"stopping {instance_id}")


def resume(ec2: EC2Client, instance_id: str) -> None:
    """停止したインスタンスの再開を要求する。"""
    _ = ec2.start_instances(InstanceIds=[instance_id])
    msg.info(f"resuming {instance_id}")


@contextmanager
def refused(service: Service, command: str) -> Generator[None]:
    """状態を変える要求を、インスタンスの状態が合わないと AWS が断れば終了する。

    temt は、インスタンスが途中の状態でも先回りして断らず、要求をそのまま送る。
    AWS が断れば、その誤りのコードとメッセージに、status を見て command を
    やり直すよう添える。ほかの誤りは、そのまま渡す。
    """
    try:
        yield
    except ClientError as e:
        if error_code(e) not in WRONG_STATE:
            raise
        raise TemtError(
            f"{aws_error(e)}; see 'temt status --svc {service.svc}' and {command} again"
        ) from None


def _seen(ec2: EC2Client, ids: list[str]) -> dict[str, Instance]:
    """今のインスタンスを ID ごとに返す。AWS にまだ見えないものは入らない。

    describe-instances は run-instances から数秒遅れるので、起動した直後はその ID
    が見つからないことがある。
    """
    try:
        found = ec2.describe_instances(InstanceIds=ids)
    except ClientError as e:
        if error_code(e) != NOT_FOUND:
            raise
        return {}
    return {one.id: one for one in _read(found)}


def look_up(ec2: EC2Client, instance_id: str) -> Instance | None:
    """ID でインスタンスを引く。AWS にまだ見えなければ None。"""
    return _seen(ec2, [instance_id]).get(instance_id)


def wait_leaving(
    ec2: EC2Client,
    instance_id: str,
    state: str,
    deadline: int = LEAVE_DEADLINE,
    notice: str | None = None,
) -> None:
    """状態を変えるよう求めたインスタンスが、describe-instances で state 以外に見えるまで待つ。

    describe-instances は start-instances から数秒遅れる。前の状態が見えるうちに完了を
    待ち始めると、古い状態を失敗と取り違える。締め切りを過ぎたら、最後に見えた状態が
    変わった理由を添えて終了する。再開がすぐ失敗して stopped に戻ったときも、ここで
    終わる。deadline は締め切り（秒）。notice は、待ち始めるときに知らせる文。すでに
    抜けていれば知らせない。
    """
    last: list[Instance | None] = [None]

    def check() -> bool | None:
        one = look_up(ec2, instance_id)
        last[0] = one
        return True if one is not None and one.state != state else None

    try:
        _ = wait.until(deadline, f"{instance_id} to leave {state}", _noticing(notice, check))
    except TemtError as e:
        one = last[0]
        raise TemtError(f"{e}{one.because() if one else ''}") from None


def wait_running(ec2: EC2Client, instance_id: str) -> Instance:
    """running になるまで待ち、そのインスタンスを返す。

    起動した直後で AWS にまだ見えなければ、見えるまで待つ。
    """

    def look() -> Instance | None:
        one = look_up(ec2, instance_id)
        if one is None:
            return None
        if one.state in GONE_STATES:
            raise TemtError(
                f"{instance_id} went to {one.state} while waiting to be running{one.because()}"
            )
        return one if one.state == RUNNING else None

    one = wait.until(RUNNING_DEADLINE, f"{instance_id} to be running", look)
    msg.info(f"{instance_id} is {RUNNING}")
    return one


def wait_stopped(ec2: EC2Client, instance_id: str, hint: str | None = None) -> None:
    """stopped になるまで待つ。hint は、締め切りを過ぎたときに添える手がかり。"""

    def look() -> bool | None:
        one = look_up(ec2, instance_id)
        if one is None:
            return None
        if one.state in ENDING_STATES:
            raise TemtError(
                f"{instance_id} went to {one.state} while waiting to be stopped{one.because()}"
            )
        return True if one.state == STOPPED else None

    _ = wait.until(STOPPED_DEADLINE, f"{instance_id} to be {STOPPED}", look, hint=hint)
    msg.info(f"{instance_id} is {STOPPED}")


def _noticing[T](notice: str | None, check: Callable[[], T | None]) -> Callable[[], T | None]:
    """wait.until に渡す look。1 回目に check が値を返さなければ、notice を知らせる。

    すでに済んでいれば何も知らせない。
    """
    told = False

    def look() -> T | None:
        nonlocal told
        found = check()
        if found is None and notice is not None and not told:
            msg.info(notice)
            told = True
        return found

    return look


def wait_address(ec2: EC2Client, instance_id: str, notice: str | None = None) -> str:
    """パブリック IPv4 アドレスが付くまで待ち、そのアドレスを返す。

    待つあいだにインスタンスが止まるか終わりに向かえば、締め切りを待たずに終了する。
    """

    def check() -> str | None:
        one = look_up(ec2, instance_id)
        if one is None:
            return None
        if one.state in GONE_STATES:
            raise TemtError(
                f"{instance_id} went to {one.state} while waiting for an address{one.because()}"
            )
        return one.public_ipv4 or None

    return wait.until(
        RUNNING_DEADLINE, f"a public address on {instance_id}", _noticing(notice, check)
    )


def is_online(ssm: SSMClient, instance_id: str, since: datetime | None = None) -> bool:
    """SSM エージェントが Online と報告しているか。ログインできる合図である。

    since があれば、最後の応答（LastPingDateTime）がそれより後であることも求める。
    停止してすぐ再開すると、止まる前の Online の報告が残っていることがあるので、
    起動した時刻より後の応答だけを数える。
    """
    found = (
        ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        ).get("InstanceInformationList")
        or []
    )
    if not found or found[0].get("PingStatus") != ONLINE:
        return False
    if since is None:
        return True
    pinged = found[0].get("LastPingDateTime")
    return isinstance(pinged, datetime) and _aware(pinged) >= since


def wait_online(
    ssm: SSMClient,
    instance_id: str,
    notice: str | None = None,
    since: datetime | None = None,
    hint: str | None = None,
) -> None:
    """SSM エージェントが Online と報告するまで待つ。since は is_online と同じ。

    hint は、締め切りを過ぎたときに添える手がかり。
    """
    _ = wait.until(
        LOGIN_DEADLINE,
        f"the SSM agent on {instance_id} to come online",
        _noticing(notice, lambda: True if is_online(ssm, instance_id, since) else None),
        hint=hint,
    )


def wait_sshd_ports(
    ssm: SSMClient,
    service: Service,
    instance_id: str,
    current: sshd.Association,
    notice: str | None = None,
) -> str:
    """そのインスタンスで関連付け <名前>-sshd の今の版が実行し終わるまで待ち、その状態を返す。

    current は sshd.find で読んだ今の版。前の版の状態と、一覧に無い状態は、まだ実行
    されていないものとみなす。締め切りを過ぎたら、status を見るよう添えて終了する。
    """
    name = service.sshd_association
    return wait.until(
        SSHD_DEADLINE,
        f"association {name} to finish on {instance_id}",
        _noticing(
            notice, lambda: sshd.outcome(sshd.instance_status(ssm, current, instance_id), current)
        ),
        hint=f"see 'temt status --svc {service.svc}'",
    )


def connect(address: str, port: int) -> socket.socket:
    """そのアドレスとポートに TCP でつなぎ、つないだソケットを返す。

    つなぐまでの締め切りは CONNECT_TIMEOUT で、つないだ後は締め切りを外す。既定の ssh
    が自分で TCP につなぐときと同じく、打った文字を待たせずに送るよう TCP_NODELAY を、
    相手が消えたのに気づけるよう SO_KEEPALIVE を付ける。ProxyCommand 越しでは、ssh の
    側はパイプなので、ssh はこれらを付けられない。

    つながらなければ OSError を上げる。拒まれたなら ConnectionRefusedError で、
    パケットは届いているが sshd が受け付けていない。時間切れなら、途中で落とされて
    いる。
    """
    connection = socket.create_connection((address, port), timeout=CONNECT_TIMEOUT)
    try:
        connection.settimeout(None)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        connection.close()
        raise
    return connection


class NotAccepting(TemtError):
    """締め切りまでにポートにつながらなかった。最後につながらなかった理由を持つ。"""

    def __init__(self, text: str, reason: OSError | None) -> None:
        super().__init__(text)
        self.reason = reason


def wait_accepting(address: str, port: int, notice: str | None = None) -> socket.socket:
    """そのアドレスとポートに TCP でつながるまで待ち、つないだソケットを返す。

    つながることが、sshd が上がった合図である。締め切りを過ぎたら、最後につながら
    なかった理由を添えた NotAccepting を上げる。
    """
    last: list[OSError | None] = [None]

    def check() -> socket.socket | None:
        try:
            return connect(address, port)
        except OSError as e:
            last[0] = e
            return None

    try:
        return wait.until(
            LOGIN_DEADLINE,
            f"{address} to accept connections on port {port}",
            _noticing(notice, check),
        )
    except TemtError as e:
        raise NotAccepting(str(e), last[0]) from None


def wait_terminated(ec2: EC2Client, found: list[Instance]) -> None:
    """すべてが terminated になるまで待つ。状態が変わるたびに知らせる。"""
    states = {one.id: one.state for one in found}

    def look() -> bool | None:
        seen = _seen(ec2, list(states))
        for instance_id, state in states.items():
            one = seen.get(instance_id)
            if one is not None and one.state != state:
                states[instance_id] = one.state
                msg.info(f"{instance_id} is {one.state}")
        return True if all(state == TERMINATED for state in states.values()) else None

    _ = wait.until(TERMINATED_DEADLINE, f"{', '.join(states)} to be {TERMINATED}", look)
