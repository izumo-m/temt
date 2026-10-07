"""instances の、コマンドをまたいで使う部品のテスト。AWS は Stubber で差し替える。

状態の見分け、起動してからの秒数と起動して間もないかの判定、2 台以上あるときの
案内、待ち合わせの知らせ、TCP の確かめを、コマンドを通さずに確かめる。TCP の確かめは
socket.create_connection を差し替え、実際のソケットは開かない。
"""

import re
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from conftest import Clock, an_instance, reservations
from types_boto3_ec2.client import EC2Client
from types_boto3_ssm.client import SSMClient

from temt import instances
from temt.instances import Instance
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
ADDRESS = "203.0.113.10"

SEARCH = {
    "Filters": [
        {"Name": "tag:Name", "Values": [NAME]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        },
    ]
}
BY_ID = {"InstanceIds": [INSTANCE]}
AGENT = {"Filters": [{"Key": "InstanceIds", "Values": [INSTANCE]}]}
# 応答の Date ヘッダの時刻と、それを表す文字列
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
DATE = "Sun, 27 Sep 2026 12:00:00 GMT"

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(err: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in err.splitlines()]


def one(state: str, *, age: float | None = None) -> Instance:
    """見つかったインスタンス 1 台。age は起動してからの秒数。"""
    return Instance(INSTANCE, state, "", (), age)


def agent(stub: Stubber, status: str) -> None:
    """describe-instance-information の応答を積む。status が空なら未登録。"""
    listed = [{"InstanceId": INSTANCE, "PingStatus": status}] if status else []
    stub.add_response("describe_instance_information", {"InstanceInformationList": listed}, AGENT)


def not_found(stub: Stubber) -> None:
    """ID で引いても、まだ見えないという誤りを積む。"""
    stub.add_client_error(
        "describe_instances",
        service_error_code=instances.NOT_FOUND,
        http_status_code=400,
        expected_params=BY_ID,
    )


def answered(*items: dict[str, Any], date: str | None = None) -> dict[str, Any]:
    """describe-instances の応答。date があれば、AWS が付ける Date ヘッダを添える。"""
    found = reservations(*items)
    if date is not None:
        found["ResponseMetadata"] = {"HTTPHeaders": {"date": date}}
    return found


def launched(ago: timedelta, base: datetime = NOW) -> dict[str, Any]:
    """base から ago だけ前に起動した running のインスタンス 1 台分。"""
    return {**an_instance(INSTANCE, "running"), "LaunchTime": base - ago}


@pytest.mark.parametrize("state", ["pending", "running"])
def test_require_usable_passes_pending_and_running(service: Service, state: str) -> None:
    instances.require_usable(service, one(state))


@pytest.mark.parametrize("state", ["stopped", "stopping"])
def test_require_usable_asks_to_start_a_stopped_instance(service: Service, state: str) -> None:
    """停止したものも、停止の途中のものも、start で再開させる。"""
    with pytest.raises(
        TemtError, match=f"{INSTANCE} is {state}; run 'temt start --svc dev-main' first"
    ):
        instances.require_usable(service, one(state))


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0.0, True),
        (299.0, True),
        # ログインできるまでの締め切りを過ぎたら、もう間もなくはない
        (300.0, False),
        (3600.0, False),
        # 起動時刻が分からなければ、起動から時間がたったものとして扱う
        (None, False),
    ],
)
def test_recent_is_within_the_login_deadline(age: float | None, expected: bool) -> None:
    assert instances.recent(one("running", age=age)) is expected


def test_the_age_is_counted_from_the_date_of_the_response() -> None:
    """手元の時計ではなく、AWS の応答の Date ヘッダから数える。"""
    [found] = instances._read(answered(launched(timedelta(seconds=120)), date=DATE))
    assert found.age == 120.0


def test_a_date_in_another_zone_is_counted_by_its_offset() -> None:
    """UTC 以外の時差を持つ Date ヘッダも、同じ時点として数える。"""
    date = "Sun, 27 Sep 2026 21:00:00 +0900"
    [found] = instances._read(answered(launched(timedelta(seconds=120)), date=date))
    assert found.age == 120.0


def test_a_launch_time_without_a_zone_is_read_as_utc() -> None:
    """時差の無い LaunchTime は UTC として数え、引き算で落ちない。"""
    item = {**an_instance(INSTANCE, "running"), "LaunchTime": datetime(2026, 9, 27, 11, 58)}
    [found] = instances._read(answered(item, date=DATE))
    assert found.age == 120.0


def test_a_date_without_a_zone_is_read_as_utc() -> None:
    """時差を -0000 と書いた Date ヘッダは、UTC として数える。"""
    date = NOW.strftime("%a, %d %b %Y %H:%M:%S -0000")
    [found] = instances._read(answered(launched(timedelta(seconds=120)), date=date))
    assert found.age == 120.0


@pytest.mark.parametrize("date", [None, "not a date", ""])
def test_the_age_falls_back_to_the_local_clock(date: str | None) -> None:
    """Date ヘッダが無いか読めなければ、手元の時刻から数える。"""
    base = datetime.now(UTC)
    before = datetime.now(UTC)
    [found] = instances._read(answered(launched(timedelta(seconds=120), base), date=date))
    after = datetime.now(UTC)
    assert found.age is not None
    assert (before - base).total_seconds() + 120 <= found.age
    assert found.age <= (after - base).total_seconds() + 120


def test_the_image_is_read_from_the_image_id() -> None:
    """起動に使った AMI は ImageId から読む。無ければ空文字列。"""
    item = {**an_instance(INSTANCE, "running"), "ImageId": "ami-0123456789abcdef0"}
    found = instances._read(answered(item, an_instance(OTHER, "pending"), date=DATE))
    assert [one.image_id for one in found] == ["ami-0123456789abcdef0", ""]


def test_the_root_volume_is_the_one_on_the_root_device() -> None:
    """ルートボリュームは、RootDeviceName のデバイスのものを読む。無ければ空文字列。"""
    item = {
        **an_instance(INSTANCE, "running"),
        "RootDeviceName": "/dev/sda1",
        "BlockDeviceMappings": [
            {"DeviceName": "/dev/sdb", "Ebs": {"VolumeId": "vol-0000000000000000b"}},
            {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-0000000000000000a"}},
        ],
    }
    found = instances._read(answered(item, an_instance(OTHER, "pending"), date=DATE))
    assert [one.root_volume for one in found] == ["vol-0000000000000000a", ""]


def test_no_root_device_means_no_root_volume() -> None:
    """RootDeviceName が無ければ、デバイス名の無い行も拾わない。"""
    item = {
        **an_instance(INSTANCE, "running"),
        "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-0000000000000000a"}}],
    }
    [found] = instances._read(answered(item, date=DATE))
    assert found.root_volume == ""


def test_find_reads_the_age(ec2: EC2Client, service: Service) -> None:
    """起動時刻が応答に無いインスタンスの age は None である。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_instances",
            reservations(
                launched(timedelta(seconds=0), datetime.now(UTC)), an_instance(OTHER, "pending")
            ),
            SEARCH,
        )
        found = instances.find(ec2, service)
        stub.assert_no_pending_responses()
    assert [item.id for item in found] == [INSTANCE, OTHER]
    assert found[0].age is not None
    assert 0 <= found[0].age < instances.LOGIN_DEADLINE
    assert found[1].age is None


def test_remaining_leaves_out_instances_that_are_shutting_down() -> None:
    """終了の途中のものは、terminated と同じく無いものとして扱う。"""
    found = [
        Instance(state, state, "", (), None)
        for state in ("pending", "running", "stopping", "stopped", "shutting-down")
    ]
    assert [item.state for item in instances.remaining(found)] == [
        "pending",
        "running",
        "stopping",
        "stopped",
    ]


@pytest.mark.parametrize(("count", "ones"), [(2, "one"), (3, "ones")])
def test_extra_shows_how_to_terminate_the_extra_instances(
    service: Service, count: int, ones: str
) -> None:
    assert instances.extra(service, count) == (
        f"{count} instances for {NAME}; terminate the extra {ones} with "
        "'temt terminate --svc dev-main <instance-id>'"
    )


def test_too_many_lists_the_instances_and_tells_how_to_go_on(
    service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    found = [one("running"), Instance(OTHER, "stopped", "", (), None)]
    with pytest.raises(TemtError) as caught:
        instances.too_many(service, found, "bake")
    assert str(caught.value) == (
        f"2 instances for {NAME}; terminate the extra one with "
        "'temt terminate --svc dev-main <instance-id>' and bake again"
    )
    # 続くエラーに添えるので、標準エラー出力に出す
    assert capsys.readouterr() == (
        "",
        f"instances tagged Name={NAME}:\n  {INSTANCE} (running)\n  {OTHER} (stopped)\n",
    )


def test_wait_online_tells_the_notice_once(
    ssm: SSMClient, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """待つことになったら、1 回目に知らせる。2 回目以降は知らせない。"""
    with Stubber(ssm) as stub:
        agent(stub, "")
        agent(stub, "ConnectionLost")
        agent(stub, "Online")
        instances.wait_online(ssm, INSTANCE, "waiting for the agent")
        stub.assert_no_pending_responses()
    assert clock.slept == [5, 5]
    assert told(capsys.readouterr().out) == ["waiting for the agent"]


def test_wait_online_tells_nothing_when_it_is_already_online(
    ssm: SSMClient, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """すでに Online なら、問い合わせは 1 回だけで、何も知らせない。"""
    with Stubber(ssm) as stub:
        agent(stub, "Online")
        instances.wait_online(ssm, INSTANCE, "waiting for the agent")
        stub.assert_no_pending_responses()
    assert clock.slept == []
    assert capsys.readouterr().err == ""


def test_wait_online_without_a_notice_tells_nothing(
    ssm: SSMClient, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(ssm) as stub:
        agent(stub, "")
        agent(stub, "Online")
        instances.wait_online(ssm, INSTANCE)
        stub.assert_no_pending_responses()
    assert clock.slept == [5]
    assert capsys.readouterr().err == ""


def test_wait_accepting_tells_the_notice_once(
    clock: Clock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """つながらない理由が何であれ、待つ。待つことは 1 回だけ知らせる。

    つながったら、そのソケットを返す。
    """
    answers: list[OSError | None] = [
        ConnectionRefusedError(111, "Connection refused"),
        TimeoutError("timed out"),
        None,
    ]
    calls: list[tuple[str, int]] = []
    opened = Connection()

    def connect(address: str, port: int) -> Connection:
        calls.append((address, port))
        answer = answers.pop(0)
        if answer is not None:
            raise answer
        return opened

    monkeypatch.setattr(instances, "connect", connect)
    assert instances.wait_accepting(ADDRESS, 22, "waiting for the port") is opened
    assert calls == [(ADDRESS, 22)] * 3
    assert clock.slept == [5, 5]
    assert told(capsys.readouterr().out) == ["waiting for the port"]


def test_wait_accepting_tells_nothing_when_it_already_accepts(
    clock: Clock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, int]] = []
    opened = Connection()

    def connect(address: str, port: int) -> Connection:
        calls.append((address, port))
        return opened

    monkeypatch.setattr(instances, "connect", connect)
    assert instances.wait_accepting(ADDRESS, 22, "waiting for the port") is opened
    assert calls == [(ADDRESS, 22)]
    assert clock.slept == []
    assert capsys.readouterr().err == ""


def test_wait_accepting_keeps_the_last_reason_when_it_times_out(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """締め切りを過ぎたら、最後につながらなかった理由を添えて上げる。"""
    _ = clock
    last = TimeoutError("timed out")
    answers: list[OSError] = [ConnectionRefusedError(111, "Connection refused")] * 60
    answers.append(last)

    def connect(address: str, port: int) -> Connection:
        _ = (address, port)
        raise answers.pop(0)

    monkeypatch.setattr(instances, "connect", connect)
    with pytest.raises(instances.NotAccepting, match="timed out after 5 minutes") as raised:
        instances.wait_accepting(ADDRESS, 22)
    assert raised.value.reason is last


def test_wait_address_tells_the_notice_once(
    ec2: EC2Client, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """ID で引いてまだ見えないときも、アドレスが無いときも、待つことになる。"""
    with Stubber(ec2) as stub:
        not_found(stub)
        stub.add_response(
            "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
        )
        stub.add_response(
            "describe_instances",
            reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
            BY_ID,
        )
        assert instances.wait_address(ec2, INSTANCE, "waiting for an address") == ADDRESS
        stub.assert_no_pending_responses()
    assert clock.slept == [5, 5]
    assert told(capsys.readouterr().out) == ["waiting for an address"]


def test_wait_address_tells_nothing_when_the_address_is_there(
    ec2: EC2Client, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_instances",
            reservations(an_instance(INSTANCE, "pending", address=ADDRESS)),
            BY_ID,
        )
        assert instances.wait_address(ec2, INSTANCE, "waiting for an address") == ADDRESS
        stub.assert_no_pending_responses()
    assert clock.slept == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("state", ["stopping", "stopped", "shutting-down", "terminated"])
def test_wait_address_stops_when_the_instance_goes_away(
    ec2: EC2Client, clock: Clock, state: str
) -> None:
    """止まるか終わりに向かえば、アドレスは付かないので、締め切りを待たずに終わる。"""
    with Stubber(ec2) as stub:
        stub.add_response("describe_instances", reservations(an_instance(INSTANCE, state)), BY_ID)
        with pytest.raises(
            TemtError, match=f"{INSTANCE} went to {state} while waiting for an address"
        ):
            _ = instances.wait_address(ec2, INSTANCE)
        stub.assert_no_pending_responses()
    assert clock.slept == []


class Connection:
    """socket.create_connection が返すものの代わり。締め切りと付けた設定を控える。

    error があれば、設定を付けるときにそれを上げる。
    """

    def __init__(self, error: OSError | None = None) -> None:
        self.timeouts: list[float | None] = []
        self.options: list[tuple[int, int, int]] = []
        self.error = error
        self.closed = False

    def settimeout(self, timeout: float | None) -> None:
        self.timeouts.append(timeout)

    def setsockopt(self, level: int, option: int, value: int) -> None:
        if self.error is not None:
            raise self.error
        self.options.append((level, option, value))

    def close(self) -> None:
        self.closed = True


def test_connect_drops_the_timeout_and_sets_the_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """つなぐまでの締め切りは CONNECT_TIMEOUT で、つないだ後は外す。

    中継のあいだ、通信が途絶えても切らないためである。ただし相手が消えたのには気づける
    よう、SO_KEEPALIVE を付ける。打った文字を待たせずに送るよう、TCP_NODELAY も付ける。
    """
    made: list[tuple[tuple[str, int], float]] = []
    connection = Connection()

    def create(address: tuple[str, int], timeout: float) -> Connection:
        made.append((address, timeout))
        return connection

    monkeypatch.setattr(socket, "create_connection", create)
    assert instances.connect(ADDRESS, 22) is connection
    assert made == [((ADDRESS, 22), instances.CONNECT_TIMEOUT)]
    assert connection.timeouts == [None]
    assert connection.options == [
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ]
    assert not connection.closed


def test_connect_closes_what_it_cannot_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    """つないだ後の設定に失敗したら、閉じてその誤りを上げる。"""
    error = OSError(22, "Invalid argument")
    connection = Connection(error)

    def create(address: tuple[str, int], timeout: float) -> Connection:
        _ = (address, timeout)
        return connection

    monkeypatch.setattr(socket, "create_connection", create)
    with pytest.raises(OSError) as raised:
        _ = instances.connect(ADDRESS, 22)
    assert raised.value is error
    assert connection.closed


@pytest.mark.parametrize(
    "error", [ConnectionRefusedError(111, "refused"), TimeoutError("timed out"), OSError("down")]
)
def test_connect_raises_the_reason_it_cannot_connect(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    """つながらなければ、その OSError をそのまま上げる。proxy が拒否と時間切れを見分ける。"""

    def create(address: tuple[str, int], timeout: float) -> Connection:
        _ = (address, timeout)
        raise error

    monkeypatch.setattr(socket, "create_connection", create)
    with pytest.raises(OSError) as raised:
        _ = instances.connect(ADDRESS, 22)
    assert raised.value is error


# 起動した時刻。since に渡す
LAUNCHED = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("pinged", "online"),
    [
        # 止まる前の報告は数えない
        (LAUNCHED - timedelta(seconds=1), False),
        (LAUNCHED, True),
        (LAUNCHED + timedelta(seconds=30), True),
        # 時差の無い時刻は UTC として比べる
        (datetime(2026, 9, 28, 3, 0, 30), True),
        # 最後の応答の時刻が無ければ、起動より後とは言えない
        (None, False),
    ],
)
def test_is_online_since_counts_only_pings_after_the_launch(
    ssm: SSMClient, pinged: datetime | None, online: bool
) -> None:
    item: dict[str, Any] = {"InstanceId": INSTANCE, "PingStatus": "Online"}
    if pinged is not None:
        item["LastPingDateTime"] = pinged
    with Stubber(ssm) as stub:
        stub.add_response(
            "describe_instance_information", {"InstanceInformationList": [item]}, AGENT
        )
        assert instances.is_online(ssm, INSTANCE, LAUNCHED) is online
        stub.assert_no_pending_responses()


def test_is_online_since_needs_the_agent_online(ssm: SSMClient) -> None:
    with Stubber(ssm) as stub:
        stub.add_response(
            "describe_instance_information",
            {
                "InstanceInformationList": [
                    {
                        "InstanceId": INSTANCE,
                        "PingStatus": "ConnectionLost",
                        "LastPingDateTime": LAUNCHED + timedelta(seconds=30),
                    }
                ]
            },
            AGENT,
        )
        assert instances.is_online(ssm, INSTANCE, LAUNCHED) is False
        stub.assert_no_pending_responses()


def test_launched_is_the_launch_time_in_utc(ec2: EC2Client) -> None:
    """停止したものを再開すると、LaunchTime は再開した時刻になる。それをそのまま持つ。"""
    item = {**an_instance(INSTANCE, "running"), "LaunchTime": datetime(2026, 9, 28, 3, 0)}
    with Stubber(ec2) as stub:
        stub.add_response("describe_instances", reservations(item), BY_ID)
        found = instances.look_up(ec2, INSTANCE)
        stub.assert_no_pending_responses()
    assert found is not None
    assert found.launched == LAUNCHED


def test_wait_running_gives_the_reason_when_the_instance_goes_away(ec2: EC2Client) -> None:
    """StateReason があれば、状態の後ろに添える。"""
    item = {
        **an_instance(INSTANCE, "stopped"),
        "StateReason": {"Code": "Client.InternalError", "Message": "Client.InternalError: x"},
    }
    with Stubber(ec2) as stub:
        stub.add_response("describe_instances", reservations(item), BY_ID)
        with pytest.raises(TemtError) as caught:
            _ = instances.wait_running(ec2, INSTANCE)
        stub.assert_no_pending_responses()
    assert str(caught.value) == (
        f"{INSTANCE} went to stopped while waiting to be running (Client.InternalError: x)"
    )


def test_wait_stopped_gives_the_reason_when_the_instance_goes_away(ec2: EC2Client) -> None:
    """StateReason があれば、状態の後ろに添える。"""
    item = {
        **an_instance(INSTANCE, "shutting-down"),
        "StateReason": {
            "Code": "Client.UserInitiatedShutdown",
            "Message": "Client.UserInitiatedShutdown: User initiated shutdown",
        },
    }
    with Stubber(ec2) as stub:
        stub.add_response("describe_instances", reservations(item), BY_ID)
        with pytest.raises(TemtError) as caught:
            instances.wait_stopped(ec2, INSTANCE)
        stub.assert_no_pending_responses()
    assert str(caught.value) == (
        f"{INSTANCE} went to shutting-down while waiting to be stopped "
        "(Client.UserInitiatedShutdown: User initiated shutdown)"
    )


def test_wait_address_gives_the_reason_when_the_instance_goes_away(ec2: EC2Client) -> None:
    """StateReason があれば、状態の後ろに添える。"""
    item = {
        **an_instance(INSTANCE, "stopped"),
        "StateReason": {"Code": "Client.InternalError", "Message": "Client.InternalError: x"},
    }
    with Stubber(ec2) as stub:
        stub.add_response("describe_instances", reservations(item), BY_ID)
        with pytest.raises(TemtError) as caught:
            _ = instances.wait_address(ec2, INSTANCE)
        stub.assert_no_pending_responses()
    assert str(caught.value) == (
        f"{INSTANCE} went to stopped while waiting for an address (Client.InternalError: x)"
    )


def test_refused_shows_the_aws_error_safely() -> None:
    """AWS が断った誤りの文は、行を 1 行につなぎ、表示できない文字を ? にして見せる。"""
    error = ClientError(
        {"Error": {"Code": "IncorrectInstanceState", "Message": "not now\n\x1b[2J later"}},
        "RebootInstances",
    )
    with pytest.raises(TemtError) as caught, instances.refused(Service("dev-main"), "reboot"):
        raise error
    assert str(caught.value) == (
        "An error occurred (IncorrectInstanceState) when calling the RebootInstances "
        "operation: not now; ?[2J later; see 'temt status --svc dev-main' and reboot again"
    )
