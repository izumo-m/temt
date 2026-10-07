"""reboot の本体のテスト。AWS は Stubber で、標準入力と時計は偽物に差し替える。

端から端まで（reboot.run か cli.main）を通し、手順の順序と締め切りを確かめる。
"""

import fcntl
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import Clock, Stubs, an_instance, deadline, reservations, taken

from temt import aws, cli, home, lock, reboot, wait
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"

# 手順 1 の探索。探す状態の一覧は設計のとおりに固定する
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
# boot_id を読む Run Command。設計のとおりに固定する
SEND = {
    "InstanceIds": [INSTANCE],
    "DocumentName": "AWS-RunShellScript",
    "Parameters": {
        "commands": ["cat /proc/sys/kernel/random/boot_id"],
        "executionTimeout": ["30"],
    },
    "TimeoutSeconds": 30,
    "Comment": "temt reboot: read the boot id",
}
# Run Command の ID
READ_BEFORE = "11111111-0000-4000-8000-000000000000"
FIRST = "11111111-0000-4000-8000-000000000001"
SECOND = "11111111-0000-4000-8000-000000000002"
# 再起動を求めた時刻（reboot-instances の応答の時刻）と、その前後の応答の時刻
REQUESTED = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)
STALE = REQUESTED - timedelta(minutes=3)
BOOTED = REQUESTED + timedelta(seconds=30)
BEFORE = "5f0c3d1e-0000-4000-8000-000000000001"
AFTER = "5f0c3d1e-0000-4000-8000-000000000002"
# 手順 3 の問いかけ。標準エラー出力に書く
PROMPT = f"reboot {INSTANCE} (running)? [y/N] "
# 状態が合わないときに、AWS が reboot-instances を断るメッセージ
REFUSED = f"The instance '{INSTANCE}' is not in a state from which it can be rebooted."
TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(err: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in err.splitlines()]


class Terminal:
    """端末につながった標準入力の代わり。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.reads = 0

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        self.reads += 1
        return self.answer


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> Terminal:
    """問いかけに y と答える端末。"""
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)
    return terminal


def found(stubs: Stubs, state: str = "running") -> None:
    """手順 1 の探索で、1 台だけ見つかる応答を積む。"""
    stubs.ec2.add_response("describe_instances", reservations(an_instance(INSTANCE, state)), SEARCH)


def agent(stubs: Stubs, status: str, pinged: datetime = BOOTED) -> None:
    """describe-instance-information の応答を積む。status が空なら未登録。

    最後の応答の時刻は pinged で、省けば再起動を求めたより後である。
    """
    listed = (
        [{"InstanceId": INSTANCE, "PingStatus": status, "LastPingDateTime": pinged}]
        if status
        else []
    )
    stubs.ssm.add_response(
        "describe_instance_information", {"InstanceInformationList": listed}, AGENT
    )


def sent(stubs: Stubs, command_id: str) -> None:
    """boot_id を読む Run Command を受け付けた応答を積む。"""
    stubs.ssm.add_response("send_command", {"Command": {"CommandId": command_id}}, SEND)


def invocation(stubs: Stubs, command_id: str, status: str, output: str = "") -> None:
    """送った Run Command の状態を積む。"""
    response: dict[str, Any] = {"Status": status}
    if output:
        response["StandardOutputContent"] = f"{output}\n"
    stubs.ssm.add_response(
        "get_command_invocation",
        response,
        {"CommandId": command_id, "InstanceId": INSTANCE},
    )


def read_before(stubs: Stubs) -> None:
    """手順 2 と 4。エージェントが Online であることを確かめ、再起動の前の boot_id を読む。"""
    agent(stubs, "Online")
    agent(stubs, "Online")
    sent(stubs, READ_BEFORE)
    invocation(stubs, READ_BEFORE, "Success", BEFORE)


def rebooted(stubs: Stubs) -> None:
    """手順 5 の再起動の要求を積む。"""
    stubs.ec2.add_response(
        "reboot_instances",
        {"ResponseMetadata": {"HTTPHeaders": {"date": "Mon, 28 Sep 2026 02:00:00 GMT"}}},
        BY_ID,
    )


def test_it_reboots_and_waits_until_the_boot_id_changes(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    found(aws_stubs)
    read_before(aws_stubs)
    rebooted(aws_stubs)
    # 手順 7。再起動を求めるより前の Online の報告では送らず、インスタンスが止まって
    # いないかを見る
    agent(aws_stubs, "Online", STALE)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    # 落ちる直前に報告したエージェントが、前の boot_id を返す
    agent(aws_stubs, "Online")
    sent(aws_stubs, FIRST)
    aws_stubs.ssm.add_client_error(
        "get_command_invocation",
        service_error_code="InvocationDoesNotExist",
        http_status_code=400,
        expected_params={"CommandId": FIRST, "InstanceId": INSTANCE},
    )
    invocation(aws_stubs, FIRST, "Success", BEFORE)
    # 落ちている間は送らず、インスタンスが止まっていないかを見る
    agent(aws_stubs, "ConnectionLost")
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    # 戻ったエージェントが、新しい boot_id を返す
    agent(aws_stubs, "Online")
    sent(aws_stubs, SECOND)
    invocation(aws_stubs, SECOND, "InProgress")
    invocation(aws_stubs, SECOND, "Success", AFTER)

    reboot.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    assert clock.slept == [5] * 8
    captured = capsys.readouterr()
    assert captured.err == PROMPT
    # 段階が進むたびに 1 回ずつ知らせる。2 回目に落ちたときと戻ったときは知らせない
    assert told(captured.out) == [
        f"rebooting {INSTANCE}",
        f"waiting for the SSM agent on {INSTANCE} to come back",
        f"the SSM agent on {INSTANCE} is back; reading the boot id",
        f"{INSTANCE} has not rebooted yet; reading the boot id again",
        f"{INSTANCE} is ready for login",
    ]


def test_it_tells_each_stage_once(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """よくある流れ。エージェントが戻るのを待ち、戻ったら boot_id を読む。"""
    found(aws_stubs)
    read_before(aws_stubs)
    rebooted(aws_stubs)
    for _ in range(2):
        agent(aws_stubs, "Online", STALE)
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
        )
    agent(aws_stubs, "Online")
    sent(aws_stubs, FIRST)
    invocation(aws_stubs, FIRST, "Success", AFTER)

    reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [
        f"rebooting {INSTANCE}",
        f"waiting for the SSM agent on {INSTANCE} to come back",
        f"the SSM agent on {INSTANCE} is back; reading the boot id",
        f"{INSTANCE} is ready for login",
    ]


def test_async_reboots_without_reading_the_boot_id(
    aws_stubs: Stubs,
    configured: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--async は待たないので、SSM のクライアントを作らない。"""
    _ = configured
    made: list[str] = []
    session = aws.session

    class Recording:
        """作ったクライアントの名前を控える Session。"""

        def __init__(self, values: Conf) -> None:
            self.inner: Any = session(values)

        def client(self, name: str) -> Any:
            made.append(name)
            return self.inner.client(name)

    monkeypatch.setattr(aws, "session", Recording)
    found(aws_stubs)
    rebooted(aws_stubs)

    assert cli.main(["temt", "reboot", "--svc", "dev-main", "--async", "--yes"]) == 0

    aws_stubs.done()
    assert made == ["ec2"]
    assert told(capsys.readouterr().out) == [f"rebooting {INSTANCE}"]


def test_it_changes_nothing_when_the_answer_is_not_y(
    aws_stubs: Stubs,
    configured: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """確認は boot_id を読む前に求める。y でなければ何もしない。"""
    _ = configured
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    found(aws_stubs)
    agent(aws_stubs, "Online")

    assert cli.main(["temt", "reboot", "--svc", "dev-main"]) == 1

    aws_stubs.done()
    assert capsys.readouterr() == ("", f"{PROMPT}temt: cancelled\n")


def test_it_needs_the_agent_online_to_wait(
    aws_stubs: Stubs, configured: Service, answer: Terminal
) -> None:
    """エージェントが Online でなければ、確認を求める前に --async を促して終わる。"""
    found(aws_stubs)
    agent(aws_stubs, "ConnectionLost")

    with pytest.raises(
        TemtError,
        match=re.escape(
            f"the SSM agent on {INSTANCE} is not online, so temt cannot tell when it has "
            "rebooted; try 'temt reboot --svc dev-main --async'; it reboots without waiting "
            "for the reboot to finish"
        ),
    ):
        reboot.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert answer.reads == 0


def test_it_shows_only_the_aws_error_when_the_agent_cannot_be_checked(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """手順 2 で AWS に断られたら、その誤りだけを出し、--async を促さない。"""
    _ = configured
    found(aws_stubs, "running")
    aws_stubs.ssm.add_client_error(
        "describe_instance_information",
        service_error_code="AccessDeniedException",
        service_message="denied",
        http_status_code=400,
        expected_params=AGENT,
    )

    assert cli.main(["temt", "reboot", "--svc", "dev-main", "--yes"]) == 1

    aws_stubs.done()
    err = capsys.readouterr().err
    assert err.endswith(
        "temt: An error occurred (AccessDeniedException) when calling the "
        "DescribeInstanceInformation operation: denied\n"
    )
    assert "--async" not in err


def test_it_gives_up_reading_the_boot_id_before_the_reboot(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    """再起動の前の boot_id は 1 分で諦め、再起動を求めない。"""
    found(aws_stubs)
    agent(aws_stubs, "Online")
    agent(aws_stubs, "Online")
    sent(aws_stubs, READ_BEFORE)
    for _ in range(12):
        invocation(aws_stubs, READ_BEFORE, "Pending")

    with pytest.raises(TemtError) as caught:
        reboot.run(configured, is_async=False, yes=True)

    # reboot-instances を積んでいないので、求めていれば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == [5] * 12
    assert str(caught.value) == (
        f"timed out after 1 minute waiting for the boot id of {INSTANCE}; "
        "try 'temt reboot --svc dev-main --async'; it reboots without waiting for the "
        "reboot to finish"
    )


def test_it_asks_to_start_a_stopped_instance(aws_stubs: Stubs, configured: Service) -> None:
    found(aws_stubs, "stopped")

    with pytest.raises(
        TemtError,
        match=re.escape(f"{INSTANCE} is stopped; run 'temt start --svc dev-main' first"),
    ):
        reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()


@pytest.mark.parametrize("state", ["pending", "stopping"])
def test_async_asks_aws_to_reboot_an_instance_on_its_way(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    """途中の状態でも先回りして断らず、再起動を求める。断るかは AWS が決める。"""
    found(aws_stubs, state)
    rebooted(aws_stubs)

    reboot.run(configured, is_async=True, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"rebooting {INSTANCE}"]


@pytest.mark.parametrize("code", ["IncorrectInstanceState", "IncorrectState"])
def test_it_shows_the_refusal_from_aws(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str], code: str
) -> None:
    """インスタンスの状態が合わないと AWS が断れば、その誤りを見せ、status を促す。"""
    _ = configured
    found(aws_stubs, "stopping")
    aws_stubs.ec2.add_client_error(
        "reboot_instances",
        service_error_code=code,
        service_message=REFUSED,
        http_status_code=400,
        expected_params=BY_ID,
    )

    assert cli.main(["temt", "reboot", "--svc", "dev-main", "--async", "--yes"]) == 1

    aws_stubs.done()
    assert capsys.readouterr().err.endswith(
        f"temt: An error occurred ({code}) when calling the RebootInstances operation: "
        f"{REFUSED}; see 'temt status --svc dev-main' and reboot again\n"
    )


def test_it_shows_the_refusal_after_reading_the_boot_id(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """--async でなくても、boot_id を読んだ後に AWS が断れば、同じく見せて待たずに終わる。

    boot_id を読む間に、インスタンスが止まり始めたときに当たる。
    """
    _ = configured
    found(aws_stubs)
    read_before(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "reboot_instances",
        service_error_code="IncorrectInstanceState",
        service_message=REFUSED,
        http_status_code=400,
        expected_params=BY_ID,
    )

    assert cli.main(["temt", "reboot", "--svc", "dev-main", "--yes"]) == 1

    # 手順 7 の問い合わせは積んでいないので、待てば Stubber が失敗する
    aws_stubs.done()
    # 再起動の前に boot_id を読む待ち合わせの 1 回だけ
    assert clock.slept == [5]
    assert capsys.readouterr().err.endswith(
        "temt: An error occurred (IncorrectInstanceState) when calling the RebootInstances "
        f"operation: {REFUSED}; see 'temt status --svc dev-main' and reboot again\n"
    )


@pytest.mark.parametrize("states", [(), ("shutting-down",)])
def test_it_needs_an_instance(
    aws_stubs: Stubs, configured: Service, states: tuple[str, ...]
) -> None:
    """終了の途中のものは、terminated と同じく無いものとして扱う。"""
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(*(an_instance(INSTANCE, state) for state in states)),
        SEARCH,
    )

    with pytest.raises(
        TemtError, match=f"no instance for {NAME}; run 'temt start --svc dev-main' first"
    ):
        reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()


def test_it_lists_two_instances_and_stops(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running"), an_instance(OTHER, "running")),
        SEARCH,
    )

    with pytest.raises(TemtError) as caught:
        reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"2 instances for {NAME}; terminate the extra one with "
        "'temt terminate --svc dev-main <instance-id>' and reboot again"
    )
    assert f"  {OTHER} (running)" in capsys.readouterr().err


def test_it_sends_another_command_when_one_does_not_finish(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再起動に巻き込まれて終わらないコマンドは、1 分で見切って次を送る。"""
    monkeypatch.setattr(wait, "INTERVAL", 30)
    found(aws_stubs)
    read_before(aws_stubs)
    rebooted(aws_stubs)
    agent(aws_stubs, "Online")
    sent(aws_stubs, FIRST)
    invocation(aws_stubs, FIRST, "InProgress")
    invocation(aws_stubs, FIRST, "InProgress")
    agent(aws_stubs, "Online")
    sent(aws_stubs, SECOND)
    invocation(aws_stubs, SECOND, "Success", AFTER)

    reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert clock.slept == [30] * 5


def test_it_retries_when_the_agent_cannot_take_the_command(
    aws_stubs: Stubs, configured: Service
) -> None:
    """Online と報告されていても、落ちたエージェントには send-command が通らない。"""
    found(aws_stubs)
    read_before(aws_stubs)
    rebooted(aws_stubs)
    agent(aws_stubs, "Online")
    aws_stubs.ssm.add_client_error(
        "send_command",
        service_error_code="InvalidInstanceId",
        http_status_code=400,
        expected_params=SEND,
    )
    # 送れなかったので、次もまず送る
    agent(aws_stubs, "Online")
    sent(aws_stubs, SECOND)
    # 結果を見るときに断られても、まだ終わっていないものとして待つ
    aws_stubs.ssm.add_client_error(
        "get_command_invocation",
        service_error_code="InvalidInstanceId",
        http_status_code=400,
        expected_params={"CommandId": SECOND, "InstanceId": INSTANCE},
    )
    invocation(aws_stubs, SECOND, "Success", AFTER)

    reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()


def test_it_gives_up_when_the_instance_does_not_come_back(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """締め切りは 10 分。どの段階が終わらなかったかを述べる。"""
    found(aws_stubs)
    read_before(aws_stubs)
    rebooted(aws_stubs)
    monkeypatch.setattr(wait, "INTERVAL", 120)
    for _ in range(6):
        agent(aws_stubs, "ConnectionLost")
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
        )

    with pytest.raises(TemtError) as caught:
        reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    # 1 つめは、再起動の前に boot_id を読む待ち合わせ
    assert clock.slept == [120] * 6
    assert str(caught.value) == (
        f"timed out after 10 minutes waiting for {INSTANCE} to come back after the reboot; "
        "see 'temt status --svc dev-main'"
    )


def test_it_stops_when_the_instance_goes_away_while_it_waits(
    aws_stubs: Stubs, configured: Service
) -> None:
    """再起動の途中で中から電源を切られるなどして終わりに向かえば、締め切りを待たない。

    StateReason があれば、状態の後ろに添える。
    """
    found(aws_stubs)
    read_before(aws_stubs)
    rebooted(aws_stubs)
    agent(aws_stubs, "ConnectionLost")
    item = {
        **an_instance(INSTANCE, "shutting-down"),
        "StateReason": {
            "Code": "Client.UserInitiatedShutdown",
            "Message": "Client.UserInitiatedShutdown: User initiated shutdown",
        },
    }
    aws_stubs.ec2.add_response("describe_instances", reservations(item), BY_ID)

    with pytest.raises(TemtError) as caught:
        reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"{INSTANCE} went to shutting-down while waiting for it to reboot "
        "(Client.UserInitiatedShutdown: User initiated shutdown)"
    )


@pytest.mark.parametrize(("status", "output"), [("Failed", ""), ("TimedOut", ""), ("Success", "")])
def test_it_sends_another_command_when_one_does_not_read_the_boot_id(
    aws_stubs: Stubs, configured: Service, status: str, output: str
) -> None:
    """成功せずに終わったか、成功しても何も出さなければ、次を送る。"""
    found(aws_stubs)
    read_before(aws_stubs)
    rebooted(aws_stubs)
    agent(aws_stubs, "Online")
    sent(aws_stubs, FIRST)
    invocation(aws_stubs, FIRST, status, output)
    agent(aws_stubs, "Online")
    sent(aws_stubs, SECOND)
    invocation(aws_stubs, SECOND, "Success", AFTER)

    reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()


def test_another_error_from_send_command_is_not_swallowed(
    aws_stubs: Stubs, configured: Service
) -> None:
    """飲み込むのは、エージェントが受け取れないという誤りだけである。再起動は求めない。"""
    found(aws_stubs)
    agent(aws_stubs, "Online")
    agent(aws_stubs, "Online")
    aws_stubs.ssm.add_client_error(
        "send_command",
        service_error_code="AccessDeniedException",
        http_status_code=400,
        expected_params=SEND,
    )

    with pytest.raises(ClientError, match="AccessDeniedException"):
        reboot.run(configured, is_async=False, yes=True)

    # reboot-instances を積んでいないので、求めていれば Stubber が失敗する
    aws_stubs.done()


def test_another_error_from_get_command_invocation_is_not_swallowed(
    aws_stubs: Stubs, configured: Service
) -> None:
    found(aws_stubs)
    agent(aws_stubs, "Online")
    agent(aws_stubs, "Online")
    sent(aws_stubs, READ_BEFORE)
    aws_stubs.ssm.add_client_error(
        "get_command_invocation",
        service_error_code="AccessDeniedException",
        http_status_code=400,
        expected_params={"CommandId": READ_BEFORE, "InstanceId": INSTANCE},
    )

    with pytest.raises(ClientError, match="AccessDeniedException"):
        reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()


def test_the_deadlines_are_the_ones_in_the_design() -> None:
    """再起動の前に boot_id を読むまで 1 分、再起動して戻るまで 10 分。"""
    assert (reboot.BEFORE_DEADLINE, reboot.AFTER_DEADLINE) == (60, 600)
    assert reboot.COMMAND_PATIENCE == 60


def test_it_stops_when_another_command_holds_the_lock(
    aws_stubs: Stubs, configured: Service
) -> None:
    """ロックはインスタンスを探すより先に取る。取れなければ待たずに終わる。"""
    held = home.prepare_known_hosts(configured) / lock.LOCK_NAME
    with held.open("a", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            deadline(5.0),
            pytest.raises(TemtError, match=f"another temt command is running for {NAME}"),
        ):
            reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()


def test_it_holds_the_lock_while_it_waits(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """待ち合わせの間もロックを持っている。"""
    attempts: list[bool] = []

    def probe(seconds: float) -> None:
        clock.sleep(seconds)
        attempts.append(taken(configured))

    monkeypatch.setattr(wait, "sleep", probe)
    found(aws_stubs)
    read_before(aws_stubs)
    rebooted(aws_stubs)
    agent(aws_stubs, "Online")
    sent(aws_stubs, FIRST)
    invocation(aws_stubs, FIRST, "Success", AFTER)

    with deadline(5.0):
        reboot.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert attempts == [False, False]
    assert taken(configured)


@pytest.mark.parametrize(
    ("state", "status"),
    [("pending", ""), ("pending", "ConnectionLost"), ("stopping", "ConnectionLost")],
)
def test_it_points_to_async_when_the_agent_is_not_online_whatever_the_state(
    aws_stubs: Stubs, configured: Service, answer: Terminal, state: str, status: str
) -> None:
    """手順 2 の道。エージェントが Online でなければ、状態によらず --async を試すよう添える。

    status が空なのは、エージェントがまだ登録されていないときである。確認も求めない。
    """
    found(aws_stubs, state)
    agent(aws_stubs, status)

    with pytest.raises(TemtError) as caught:
        reboot.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"the SSM agent on {INSTANCE} is not online, so temt cannot tell when it has "
        "rebooted; try 'temt reboot --svc dev-main --async'; it reboots without waiting "
        "for the reboot to finish"
    )


@pytest.mark.parametrize("state", ["stopping", "pending"])
def test_it_points_to_async_whatever_the_state(
    aws_stubs: Stubs, configured: Service, clock: Clock, state: str
) -> None:
    """手順 4 の道。boot_id が読めなければ、状態によらず --async を試すよう添える。

    エージェントを Online にしているのは、手順 2 を通して boot_id を読む道に進めるため
    である。手順 2 の道は、その前のテストで確かめる。再起動は求めない。
    """
    found(aws_stubs, state)
    agent(aws_stubs, "Online")
    agent(aws_stubs, "Online")
    sent(aws_stubs, READ_BEFORE)
    for _ in range(12):
        invocation(aws_stubs, READ_BEFORE, "Pending")

    with pytest.raises(TemtError) as caught:
        reboot.run(configured, is_async=False, yes=True)

    # reboot-instances を積んでいないので、求めていれば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == [5] * 12
    assert str(caught.value) == (
        f"timed out after 1 minute waiting for the boot id of {INSTANCE}; "
        "try 'temt reboot --svc dev-main --async'; it reboots without waiting for the "
        "reboot to finish"
    )
