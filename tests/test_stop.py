"""stop の本体のテスト。AWS は Stubber で、標準入力と時計は偽物に差し替える。

端から端まで（stop.run か cli.main）を通し、手順の順序と締め切りを確かめる。
"""

import fcntl
import re
import sys
from typing import Any

import pytest
from conftest import Clock, Stubs, an_instance, deadline, reservations, taken

from temt import cli, home, lock, stop, wait
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"

# 手順 1 の探索。探す状態の一覧は設計のとおりに固定する
SEARCH = {
    "Filters": [
        {"Name": "tag:temt:service", "Values": ["dev-main"]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        },
    ]
}
BY_ID = {"InstanceIds": [INSTANCE]}
# 手順 3 の問いかけ。標準エラー出力に書く
PROMPT = f"stop {INSTANCE} (running)? [y/N] "
# 状態が合わないときに、AWS が stop-instances を断るメッセージ
REFUSED = f"This instance '{INSTANCE}' is not in a state from which it can be stopped."
TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(out: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in out.splitlines()]


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


def stopping(instance_id: str) -> dict[str, Any]:
    """stop-instances の応答。"""
    return {
        "StoppingInstances": [
            {
                "InstanceId": instance_id,
                "CurrentState": {"Name": "stopping"},
                "PreviousState": {"Name": "running"},
            }
        ]
    }


def seen(stubs: Stubs, *states: str) -> None:
    """ID で引いたときの状態を、順に積む。"""
    for state in states:
        stubs.ec2.add_response(
            "describe_instances", reservations(an_instance(INSTANCE, state)), BY_ID
        )


def test_it_stops_the_instance_and_waits(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    known_hosts = home.prepare_known_hosts(configured) / INSTANCE
    known_hosts.touch(mode=0o600)
    found(aws_stubs)
    aws_stubs.ec2.add_response("stop_instances", stopping(INSTANCE), BY_ID)
    # stopped を待つ。古い running が見えても待ち続ける
    seen(aws_stubs, "running", "stopping", "stopped")

    stop.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    assert clock.slept == [5, 5]
    # 再開すれば同じホスト鍵で戻るので、known_hosts は残す
    assert known_hosts.exists()
    out, err = capsys.readouterr()
    assert err == PROMPT
    assert told(out) == [f"stopping {INSTANCE}", f"{INSTANCE} is stopped"]


@pytest.mark.parametrize("state", ["running", "pending"])
def test_async_returns_as_soon_as_the_stop_is_accepted(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    """--async は、stop-instances が受け付けられるまでしか待たない。

    pending でも、running を待たずに停止を求める。古い状態が見えなくなるのも待たない。
    """
    _ = configured
    found(aws_stubs, state)
    aws_stubs.ec2.add_response("stop_instances", stopping(INSTANCE), BY_ID)

    assert cli.main(["temt", "stop", "--svc", "dev-main", "--async", "--yes"]) == 0

    # describe-instances は探索の 1 回しか積んでいないので、待てば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == []
    assert told(capsys.readouterr().out) == [f"stopping {INSTANCE}"]


def test_it_changes_nothing_when_the_answer_is_not_y(
    aws_stubs: Stubs,
    configured: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ = configured
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    found(aws_stubs)

    assert cli.main(["temt", "stop", "--svc", "dev-main"]) == 1

    # 探す以外は何も積んでいないので、変えていれば Stubber が失敗する
    aws_stubs.done()
    err = capsys.readouterr().err
    assert err.startswith(PROMPT)
    assert err.endswith("temt: cancelled\n")


def test_it_reports_that_there_is_no_instance(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 台も無いのはエラーではない。起動テンプレートの有無も確かめない。"""
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)

    stop.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"no instance for {NAME}"]


def test_it_does_nothing_when_the_instance_is_already_stopped(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    found(aws_stubs, "stopped")

    stop.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"{INSTANCE} is already stopped"]


def test_it_waits_for_an_instance_that_is_already_stopping(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """停止の途中なら、停止を求めず、確認もせずに待つ。"""
    found(aws_stubs, "stopping")
    seen(aws_stubs, "stopped")

    stop.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [
        f"{INSTANCE} is already stopping",
        f"{INSTANCE} is stopped",
    ]


def test_it_stops_a_pending_instance_and_waits(
    aws_stubs: Stubs, configured: Service, answer: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    """pending でも確認して停止を求め、stopped を待つ。"""
    found(aws_stubs, "pending")
    aws_stubs.ec2.add_response("stop_instances", stopping(INSTANCE), BY_ID)
    seen(aws_stubs, "pending", "stopped")

    stop.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    out, err = capsys.readouterr()
    assert err == f"stop {INSTANCE} (pending)? [y/N] "
    assert told(out) == [f"stopping {INSTANCE}", f"{INSTANCE} is stopped"]


@pytest.mark.parametrize("code", ["IncorrectInstanceState", "IncorrectState"])
def test_it_shows_the_refusal_from_aws(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str], code: str
) -> None:
    """インスタンスの状態が合わないと AWS が断れば、その誤りを見せ、status を促す。"""
    _ = configured
    found(aws_stubs, "pending")
    aws_stubs.ec2.add_client_error(
        "stop_instances",
        service_error_code=code,
        service_message=REFUSED,
        http_status_code=400,
        expected_params=BY_ID,
    )

    assert cli.main(["temt", "stop", "--svc", "dev-main", "--yes"]) == 1

    aws_stubs.done()
    assert capsys.readouterr().err.endswith(
        f"temt: An error occurred ({code}) when calling the StopInstances operation: "
        f"{REFUSED}; "
        "see 'temt status --svc dev-main' and stop again\n"
    )


def test_it_treats_an_instance_that_is_shutting_down_as_gone(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """終了の途中のものは、terminated と同じく無いものとして扱う。"""
    found(aws_stubs, "shutting-down")

    stop.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"no instance for {NAME}"]


def test_it_lists_two_instances_and_stops(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(
            an_instance(INSTANCE, "running"),
            an_instance(OTHER, "stopped"),
            # 終了の途中のものは数えない
            an_instance("i-0aaaaaaaaaaaaaaaa", "shutting-down"),
        ),
        SEARCH,
    )

    with pytest.raises(TemtError) as caught:
        stop.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"2 instances for {NAME}; terminate the extra one with "
        "'temt terminate --svc dev-main <instance-id>' and stop again"
    )
    err = capsys.readouterr().err
    assert f"  {OTHER} (stopped)" in err
    assert "shutting-down" not in err


def test_it_stops_waiting_when_the_instance_goes_away(
    aws_stubs: Stubs, configured: Service
) -> None:
    """止まるのを待つ間に終わりに向かえば、締め切りを待たない。"""
    found(aws_stubs)
    aws_stubs.ec2.add_response("stop_instances", stopping(INSTANCE), BY_ID)
    seen(aws_stubs, "stopping", "shutting-down")

    with pytest.raises(
        TemtError, match=f"{INSTANCE} went to shutting-down while waiting to be stopped"
    ):
        stop.run(configured, is_async=False, yes=True)

    aws_stubs.done()


def test_it_gives_up_when_the_instance_does_not_stop(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """締め切りは 10 分。どの段階が終わらなかったかを述べる。"""
    monkeypatch.setattr(wait, "INTERVAL", 120)
    found(aws_stubs)
    aws_stubs.ec2.add_response("stop_instances", stopping(INSTANCE), BY_ID)
    seen(aws_stubs, *["stopping"] * 6)

    with pytest.raises(TemtError) as caught:
        stop.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert clock.slept == [120] * 5
    assert str(caught.value) == (
        f"timed out after 10 minutes waiting for {INSTANCE} to be stopped; "
        "see 'temt status --svc dev-main'; "
        "a systemd-inhibit lock on the instance can hold the stop back"
    )


def test_async_does_not_wait_for_an_instance_that_is_already_stopping(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    found(aws_stubs, "stopping")

    stop.run(configured, is_async=True, yes=False)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"{INSTANCE} is already stopping"]


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
            stop.run(configured, is_async=False, yes=True)

    aws_stubs.done()


def test_it_holds_the_lock_while_it_waits(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[bool] = []

    def probe(seconds: float) -> None:
        clock.sleep(seconds)
        attempts.append(taken(configured))

    monkeypatch.setattr(wait, "sleep", probe)
    found(aws_stubs)
    aws_stubs.ec2.add_response("stop_instances", stopping(INSTANCE), BY_ID)
    seen(aws_stubs, "stopping", "stopped")

    with deadline(5.0):
        stop.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert attempts == [False]
    assert taken(configured)
