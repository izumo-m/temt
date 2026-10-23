"""terminate の本体のテスト。AWS は Stubber で、標準入力と時計は偽物に差し替える。

端から端まで（terminate.run か cli.main）を通し、手順の順序と締め切りを確かめる。
"""

import fcntl
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import WHERE, Clock, Stubs, an_instance, deadline, identified, reservations, taken

from temt import cli, home, instances, lock, terminate, wait
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
ADDRESS = "203.0.113.10"

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
BOTH = {"InstanceIds": [INSTANCE, OTHER]}
# 手順 3 の問いかけ。標準エラー出力に書く
PROMPT = f"terminate {INSTANCE} (running) {WHERE}? [y/N] "
TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(out: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in out.splitlines()]


class Terminal:
    """端末につながった標準入力の代わり。"""

    def __init__(self, answer: str, *, tty: bool = True) -> None:
        self.answer = answer
        self.tty = tty
        self.reads = 0

    def isatty(self) -> bool:
        return self.tty

    def readline(self) -> str:
        self.reads += 1
        return self.answer


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> Terminal:
    """問いかけに y と答える端末。"""
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)
    return terminal


def terminating(instance_id: str) -> dict[str, Any]:
    """terminate-instances の応答。"""
    return {
        "TerminatingInstances": [
            {
                "InstanceId": instance_id,
                "CurrentState": {"Name": "shutting-down"},
                "PreviousState": {"Name": "running"},
            }
        ]
    }


def known_hosts(service: Service, *instance_ids: str, current: str = "") -> list[Path]:
    """インスタンスごとの known_hosts と、current のリンクを用意する。"""
    directory = home.prepare_known_hosts(service)
    made = [directory / instance_id for instance_id in instance_ids]
    for path in made:
        path.touch(mode=0o600)
    if current:
        service.known_hosts_current.symlink_to(current)
    return made


def test_it_terminates_the_instance_and_waits(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identified(aws_stubs)
    file = known_hosts(configured, INSTANCE, current=INSTANCE)[0]
    # 手順 1
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    # 手順 4
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    # 手順 5
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "shutting-down")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )

    terminate.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    assert clock.slept == [5]
    # 手順 4.3。known_hosts を消し、リンク切れになった current も消す
    assert not file.exists()
    assert not configured.known_hosts_current.is_symlink()
    out, err = capsys.readouterr()
    # 問いかけは、標準出力をファイルやパイプに回しても見えるよう、標準エラー出力に書く
    assert err == PROMPT
    assert told(out) == [
        f"terminating {INSTANCE}",
        f"{INSTANCE} is shutting-down",
        f"{INSTANCE} is terminated",
    ]


def test_it_changes_nothing_when_the_answer_is_not_y(
    aws_stubs: Stubs,
    configured: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """y と答えなければ、何もせずに終了コード 1 で終わる。"""
    identified(aws_stubs)
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    file = known_hosts(configured, INSTANCE)[0]
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )

    assert cli.main(["temt", "terminate", "--svc", "dev-main"]) == 1

    # 探す以外は何も積んでいないので、変えていれば Stubber が失敗する
    aws_stubs.done()
    assert file.exists()
    err = capsys.readouterr().err
    assert err.startswith(PROMPT)
    assert err.endswith("temt: cancelled\n")


def test_it_cancels_at_the_end_of_input(
    aws_stubs: Stubs,
    configured: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-D は端末に何も出さないので、行を閉じてから終わる。"""
    identified(aws_stubs)
    _ = configured
    monkeypatch.setattr(sys, "stdin", Terminal(""))
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )

    assert cli.main(["temt", "terminate", "--svc", "dev-main"]) == 1

    aws_stubs.done()
    assert capsys.readouterr().err == f"{PROMPT}\ntemt: cancelled\n"


def test_it_needs_yes_when_stdin_is_not_a_terminal(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("y\n", tty=False))
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )

    with pytest.raises(TemtError, match="stdin is not a terminal; pass --yes"):
        terminate.run(configured, is_async=False, yes=False)

    aws_stubs.done()


def test_yes_skips_the_question(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )

    terminate.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert clock.slept == []
    assert answer.reads == 0
    assert "[y/N]" not in capsys.readouterr().err


def test_it_reports_that_there_is_no_instance(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 台も無いのはエラーではない。起動テンプレートの有無も確かめない。"""
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)

    terminate.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"no instance for {NAME}"]


def test_it_terminates_every_instance_when_there_are_two(
    aws_stubs: Stubs,
    configured: Service,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """手違いで 2 台あれば、両方とも終了する。"""
    identified(aws_stubs)
    files = known_hosts(configured, INSTANCE, OTHER, current=OTHER)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "stopped"), an_instance(INSTANCE, "running")),
        SEARCH,
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response("terminate_instances", terminating(OTHER), {"InstanceIds": [OTHER]})
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "terminated"), an_instance(OTHER, "terminated")),
        BOTH,
    )

    terminate.run(configured, is_async=False, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    assert [file.exists() for file in files] == [False, False]
    assert not configured.known_hosts_current.is_symlink()
    out, err = capsys.readouterr()
    assert err == f"terminate {INSTANCE} (running), {OTHER} (stopped) {WHERE}? [y/N] "
    assert f"{INSTANCE} is terminated" in told(out)
    assert f"{OTHER} is terminated" in told(out)


# ルートボリュームのデバイス名と、ほかの EBS のデバイス名
ROOT_DEVICE = "/dev/sda1"
DATA_DEVICE = "/dev/sdf"
# 終了の前にルートボリュームを消えるようにする modify-instance-attribute の引数
ROOT_GOES = {
    "InstanceId": INSTANCE,
    "BlockDeviceMappings": [{"DeviceName": ROOT_DEVICE, "Ebs": {"DeleteOnTermination": True}}],
}


def with_volumes(*, root_kept: bool, data_kept: bool = True) -> dict[str, Any]:
    """ルートボリュームとほかの EBS を持つ、describe-instances の応答の 1 台分。

    root_kept と data_kept は、それぞれが終了しても消えない（DeleteOnTermination が
    false）かどうか。
    """
    return {
        **an_instance(INSTANCE, "running", address=ADDRESS),
        "RootDeviceName": ROOT_DEVICE,
        "BlockDeviceMappings": [
            {"DeviceName": ROOT_DEVICE, "Ebs": {"DeleteOnTermination": not root_kept}},
            {"DeviceName": DATA_DEVICE, "Ebs": {"DeleteOnTermination": not data_kept}},
        ],
    }


def test_it_makes_the_root_volume_go_with_the_instance(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """DeleteOnTermination が false のルートボリュームは、終了の前に true にする。

    ほかの EBS は、インスタンスの中の側が付けるもので、インスタンスより長く使うので、
    false のままにする。modify-instance-attribute はルートボリュームの分だけ積んでいる。
    """
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(with_volumes(root_kept=True)), SEARCH
    )
    aws_stubs.ec2.add_response("modify_instance_attribute", {}, ROOT_GOES)
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )

    terminate.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert (
        told(capsys.readouterr().out)[0]
        == f"{INSTANCE}: {ROOT_DEVICE} will be deleted with the instance"
    )


def test_it_leaves_the_other_volumes_alone(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ルートボリュームが終了と一緒に消えるなら、ほかの EBS が残るものでも何も変えない。

    modify-instance-attribute は積んでいないので、呼べば Stubber が失敗する。
    """
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(with_volumes(root_kept=False)), SEARCH
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )

    terminate.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[0] == f"terminating {INSTANCE}"


def test_it_goes_on_when_the_root_volume_cannot_be_fixed(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ルートボリュームを直せなくても終了は続ける。残ることを知らせる。"""
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(with_volumes(root_kept=True)), SEARCH
    )
    aws_stubs.ec2.add_client_error(
        "modify_instance_attribute",
        service_error_code="UnauthorizedOperation",
        http_status_code=403,
        expected_params=ROOT_GOES,
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )

    terminate.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    out, err = capsys.readouterr()
    assert err.startswith(
        f"temt: warning: {INSTANCE}: {ROOT_DEVICE} will remain after the instance is gone"
    )
    assert f"terminating {INSTANCE}" in told(out)


def test_it_keeps_the_known_hosts_when_the_request_fails(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """known_hosts を消すのは、終了の要求が受け付けられた後である。"""
    file = known_hosts(configured, INSTANCE, current=INSTANCE)[0]
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    aws_stubs.ec2.add_client_error(
        "terminate_instances",
        service_error_code="UnauthorizedOperation",
        http_status_code=403,
        expected_params=BY_ID,
    )

    assert cli.main(["temt", "terminate", "--svc", "dev-main", "--yes"]) == 1

    aws_stubs.done()
    assert file.exists()
    assert configured.known_hosts_current.is_symlink()
    assert "UnauthorizedOperation" in capsys.readouterr().err


def test_async_returns_before_the_wait(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """--async でも、known_hosts は済ませる。"""
    file = known_hosts(configured, INSTANCE, current=INSTANCE)[0]
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)

    assert cli.main(["temt", "terminate", "--svc", "dev-main", "--async", "--yes"]) == 0

    aws_stubs.done()
    assert not file.exists()
    assert told(capsys.readouterr().out) == [f"terminating {INSTANCE}"]


def test_it_keeps_current_when_it_points_at_another_instance(
    aws_stubs: Stubs, configured: Service
) -> None:
    """current がリンク切れにならなければ、そのままにする。"""
    file, other = known_hosts(configured, INSTANCE, OTHER, current=OTHER)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )

    terminate.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert not file.exists()
    assert other.exists()
    assert configured.known_hosts_current.readlink() == Path(OTHER)


def test_it_gives_up_when_the_instance_does_not_go_away(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """締め切りは 10 分。どの段階が終わらなかったかを述べる。"""
    monkeypatch.setattr(wait, "INTERVAL", 120)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    for _ in range(6):
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(an_instance(INSTANCE, "shutting-down")), BY_ID
        )

    with pytest.raises(
        TemtError, match=f"timed out after 10 minutes waiting for {INSTANCE} to be terminated"
    ):
        terminate.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert clock.slept == [120] * 5


def test_the_deadline_is_the_one_in_the_design() -> None:
    """terminated になるまで 10 分。"""
    assert instances.TERMINATED_DEADLINE == 600


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
            terminate.run(configured, is_async=False, yes=True)

    # 応答を 1 つも積んでいないので、AWS を読んでいれば Stubber が失敗する
    aws_stubs.done()


def test_it_holds_the_lock_while_it_waits(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """待ち合わせの間もロックを持っている。別の terminate は入ってこない。"""
    attempts: list[bool] = []

    def probe(seconds: float) -> None:
        clock.sleep(seconds)
        attempts.append(taken(configured))

    monkeypatch.setattr(wait, "sleep", probe)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "shutting-down")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )

    with deadline(5.0):
        terminate.run(configured, is_async=False, yes=True)

    aws_stubs.done()
    assert attempts == [False]
    # 終わればロックは離す
    assert taken(configured)


TEMPLATE_IMAGE = "ami-0123456789abcdef0"


def test_it_terminates_only_the_instance_it_is_given(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ID を指せば、確認、known_hosts、待ち合わせもそれだけに行う。"""
    identified(aws_stubs)
    files = known_hosts(configured, INSTANCE, OTHER, current=INSTANCE)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "stopped"), an_instance(INSTANCE, "running")),
        SEARCH,
    )
    aws_stubs.ec2.add_response("terminate_instances", terminating(OTHER), {"InstanceIds": [OTHER]})
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "terminated")),
        {"InstanceIds": [OTHER]},
    )

    terminate.run(configured, ids=(OTHER,), is_async=False, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    assert clock.slept == []
    assert [file.exists() for file in files] == [True, False]
    assert configured.known_hosts_current.is_symlink()
    prompt = f"terminate {OTHER} (stopped) {WHERE}? [y/N] "
    out, err = capsys.readouterr()
    assert err == prompt
    assert told(out) == [f"terminating {OTHER}", f"{OTHER} is terminated"]


def test_it_terminates_nothing_when_an_instance_it_is_given_is_not_found(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """指した ID がそのサービスのインスタンスに無ければ、見つかったものを挙げて終了する。"""
    files = known_hosts(configured, INSTANCE, OTHER)
    missing = "i-0aaaaaaaaaaaaaaaa"
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "stopped"), an_instance(INSTANCE, "running")),
        SEARCH,
    )

    assert cli.main(["temt", "terminate", "--svc", "dev-main", OTHER, missing, "--yes"]) == 1

    # 探す以外は何も積んでいないので、変えていれば Stubber が失敗する
    aws_stubs.done()
    assert [file.exists() for file in files] == [True, True]
    out, err = capsys.readouterr()
    assert out == ""
    # 見つかったものを挙げてから、エラーを出す
    assert err == (
        f"instances tagged Name={NAME}:\n  {INSTANCE} (running)\n  {OTHER} (stopped)\n"
        f"temt: no instance {missing} for {NAME}; nothing was terminated; "
        "see 'temt status --svc dev-main' and terminate again\n"
    )


def test_it_fails_when_it_is_given_an_instance_but_finds_none(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 台も無くても、ID を指したのなら、エラーにする。

    start --async の直後で、起動したものがまだ探索に見えないこともあるので、status を
    見てやり直すよう添える。
    """
    _ = configured
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)

    assert cli.main(["temt", "terminate", INSTANCE, "--svc", "dev-main", "--yes"]) == 1

    aws_stubs.done()
    assert capsys.readouterr().err == (
        f"temt: no instance {INSTANCE} for {NAME}; nothing was terminated; "
        "see 'temt status --svc dev-main' and terminate again\n"
    )
