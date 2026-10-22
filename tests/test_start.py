"""start の本体のテスト。AWS は Stubber で、待ち合わせの時計は進むものに差し替える。

端から端まで（start.run か cli.main）を通し、手順の順序と締め切りを確かめる。
"""

import fcntl
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY
from conftest import Clock, Stubs, an_instance, deadline, reservations, taken

from temt import aws, cli, conf, home, instances, lock, resources, start, wait
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
ADDRESS = "203.0.113.10"

# 手順 1 のテンプレートの確かめと探索。探す状態の一覧は設計のとおりに固定する
TEMPLATES = {"LaunchTemplateNames": [TEMPLATE]}
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
# 手順 2 の起動。タグは起動テンプレートが付けるので渡さない。ClientToken は毎回違う
LAUNCH = {
    "LaunchTemplate": {"LaunchTemplateName": TEMPLATE, "Version": "$Default"},
    "MinCount": 1,
    "MaxCount": 1,
    "ClientToken": ANY,
}
# 手順 3 の問い合わせ
AGENT = {"Filters": [{"Key": "InstanceIds", "Values": [INSTANCE]}]}
# 状態が合わないときに、AWS が start-instances を断るメッセージ
REFUSED = f"The instance '{INSTANCE}' is not in a state from which it can be started."

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(err: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in err.splitlines()]


def applied(stubs: Stubs) -> None:
    """ロックの直後に確かめる起動テンプレートがある、という応答を積む。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )


def launched(stubs: Stubs) -> None:
    """手順 2 の起動の応答を積む。"""
    stubs.ec2.add_response("run_instances", {"Instances": [{"InstanceId": INSTANCE}]}, LAUNCH)


def agent(status: str) -> dict[str, Any]:
    """describe-instance-information の応答。status が空なら未登録。"""
    if not status:
        return {"InstanceInformationList": []}
    return {"InstanceInformationList": [{"InstanceId": INSTANCE, "PingStatus": status}]}


def test_it_launches_and_waits_until_login_is_possible(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    # 手順 1。テンプレートはあり、インスタンスは 1 台も無い
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    # 手順 2。起動する
    launched(aws_stubs)
    # 手順 3。running になるのを待ち、エージェントが応答するのを待つ
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "pending")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    aws_stubs.ssm.add_response("describe_instance_information", agent(""), AGENT)
    aws_stubs.ssm.add_response("describe_instance_information", agent("Online"), AGENT)

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [5, 5]
    # 設定ファイルに書かれたプロファイルとリージョンで AWS につなぐ
    assert (aws_stubs.passed[0].AwsProfile, aws_stubs.passed[0].AwsRegion) == (
        None,
        "ap-northeast-1",
    )
    assert (configured.known_hosts_dir / lock.LOCK_NAME).exists()
    err = capsys.readouterr().out
    assert told(err) == [
        f"launched {INSTANCE} from {TEMPLATE}",
        f"{INSTANCE} is running",
        f"{INSTANCE} is ready for login",
    ]
    # start は何も確認しない
    assert "[y/N]" not in err


def test_it_ends_at_running_without_waiting_for_ssm(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """WaitForSsm = false なら、running になったところで終わり、SSM には問い合わせない。"""
    configured.conf_path.write_text(
        configured.conf_path.read_text(encoding="utf-8").replace(
            "[launch-template]", "WaitForSsm = false\n\n[launch-template]", 1
        ),
        encoding="utf-8",
    )
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [
        f"launched {INSTANCE} from {TEMPLATE}",
        f"{INSTANCE} is running",
    ]


def test_it_makes_the_known_hosts_directories_private(
    aws_stubs: Stubs, configured: Service
) -> None:
    """~/.ssh/temt/ がまだ無くても、作るディレクトリはすべてモード 700 になる。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    assert not (configured.known_hosts_dir.parent.parent).exists()

    start.run(configured, is_async=True)

    aws_stubs.done()
    made = [
        home.ssh_dir(),
        configured.known_hosts_dir.parent,
        configured.known_hosts_dir,
    ]
    assert [path.stat().st_mode & 0o777 for path in made] == [0o700] * 3


def test_it_waits_for_a_pending_instance(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """pending のものがあれば、起動せずに running になるのを待つ。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "pending")), SEARCH
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running", address=ADDRESS)), BY_ID
    )
    aws_stubs.ssm.add_response("describe_instance_information", agent("Online"), AGENT)

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == []
    assert told(capsys.readouterr().out) == [
        f"{INSTANCE} is already pending",
        f"{INSTANCE} is running",
        f"{INSTANCE} is ready for login",
    ]


def test_it_uses_an_instance_that_is_already_running(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """running のものがあれば起動せず、running を待たずにエージェントだけを待つ。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    aws_stubs.ssm.add_response("describe_instance_information", agent("Online"), AGENT)

    start.run(configured, is_async=False)

    # describe-instances は探索の 1 回しか積んでいないので、待てば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == []
    assert told(capsys.readouterr().out) == [
        f"{INSTANCE} is already running",
        f"{INSTANCE} is ready for login",
    ]


def test_it_does_not_wait_for_an_address(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """アドレスの無い running でも、アドレスは待たない。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    aws_stubs.ssm.add_response("describe_instance_information", agent("Online"), AGENT)

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [
        f"{INSTANCE} is already running",
        f"{INSTANCE} is ready for login",
    ]


# 再開した時刻。LaunchTime は再開すると、その時刻になる
RESUMED_AT = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)


def resumed_instance(state: str) -> dict[str, Any]:
    """再開したインスタンスの describe-instances の 1 台分。"""
    return {**an_instance(INSTANCE, state), "LaunchTime": RESUMED_AT}


def pinged(at: datetime) -> dict[str, Any]:
    """Online のエージェントが、最後に at に応答したという describe-instance-information の応答。"""
    return {
        "InstanceInformationList": [
            {"InstanceId": INSTANCE, "PingStatus": "Online", "LastPingDateTime": at}
        ]
    }


def test_it_resumes_a_stopped_instance_and_waits_until_login_is_possible(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopped")), SEARCH
    )
    # 手順 2。起動せずに再開する
    aws_stubs.ec2.add_response("start_instances", {}, BY_ID)
    # 手順 3。describe-instances は遅れるので、古い stopped が見えなくなるまで待つ
    for state in ("stopped", "pending"):
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(resumed_instance(state)), BY_ID
        )
    # 手順 3
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(resumed_instance("running")), BY_ID
    )
    # 止まる前の Online の報告は数えない
    aws_stubs.ssm.add_response(
        "describe_instance_information", pinged(RESUMED_AT - timedelta(minutes=1)), AGENT
    )
    aws_stubs.ssm.add_response(
        "describe_instance_information", pinged(RESUMED_AT + timedelta(seconds=30)), AGENT
    )

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [5, 5]
    assert told(capsys.readouterr().out) == [
        f"resuming {INSTANCE}",
        f"{INSTANCE} is running",
        f"{INSTANCE} is ready for login",
    ]


@pytest.mark.parametrize("state", ["stopped", "stopping"])
def test_async_returns_as_soon_as_the_resume_is_accepted(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    """--async は、start-instances が受け付けられるまでしか待たない。

    停止の途中でも、止まるのを待たずに再開を求める。古い状態が見えなくなるのも待たない。
    """
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, state)), SEARCH
    )
    aws_stubs.ec2.add_response("start_instances", {}, BY_ID)

    start.run(configured, is_async=True)

    # describe-instances は探索の 1 回しか積んでいないので、待てば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == []
    assert told(capsys.readouterr().out) == [f"resuming {INSTANCE}"]


def test_it_stops_when_the_resumed_instance_stops_again(
    aws_stubs: Stubs, configured: Service
) -> None:
    """一度動き出してから stopped に戻れば、待っても running にはならない。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopped")), SEARCH
    )
    aws_stubs.ec2.add_response("start_instances", {}, BY_ID)
    for state in ("pending", "stopped"):
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(resumed_instance(state)), BY_ID
        )

    with pytest.raises(TemtError, match=f"{INSTANCE} went to stopped while waiting to be running"):
        start.run(configured, is_async=False)

    aws_stubs.done()


def test_the_profile_option_is_not_written_to_the_config_file(
    aws_stubs: Stubs, configured: Service
) -> None:
    """--profile は start の実行のあいだだけ効く。設定ファイルに書くのは apply だけ。

    start を最後まで通し、その後も設定ファイルが変わらないことを見る。
    """
    before = configured.conf_path.read_text(encoding="utf-8")
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)

    assert cli.main(["temt", "--profile", "work", "start", "--svc", "dev-main", "--async"]) == 0

    aws_stubs.done()
    # --profile が効くのは Session を作るときで、設定ファイルは変わらない
    assert conf.OPTION_PROFILE == "work"
    assert configured.conf_path.read_text(encoding="utf-8") == before


def test_it_launches_when_the_only_instance_is_shutting_down(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """終了の途中のものは、terminated と同じく無いものとして扱う。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(OTHER, "shutting-down")), SEARCH
    )
    launched(aws_stubs)

    start.run(configured, is_async=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"launched {INSTANCE} from {TEMPLATE}"]


def test_it_does_not_count_an_instance_that_is_shutting_down(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """終了の途中のものと動いているものがあっても、2 台とは数えない。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "shutting-down"), an_instance(INSTANCE, "running")),
        SEARCH,
    )

    start.run(configured, is_async=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"{INSTANCE} is already running"]


def test_it_resumes_a_stopping_instance_and_waits_until_both_old_states_go_away(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """停止の途中なら、止まるのを待たずに再開を求める。

    AWS が受け付けて、止まり終えてから動き出すこともある。stopping を抜けるまでは
    止まるまでと同じだけ待つので、1 分を過ぎても失敗と取り違えない。続けて stopped を
    抜けるまで待つ。
    """
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopping")), SEARCH
    )
    aws_stubs.ec2.add_response("start_instances", {}, BY_ID)
    for state in ["stopping"] * 13 + ["stopped", "stopped", "pending", "running"]:
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(resumed_instance(state)), BY_ID
        )
    aws_stubs.ssm.add_response(
        "describe_instance_information", pinged(RESUMED_AT + timedelta(seconds=30)), AGENT
    )

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [5] * 14
    assert told(capsys.readouterr().out) == [
        f"resuming {INSTANCE}",
        f"{INSTANCE} is stopping; waiting for it to stop",
        f"{INSTANCE} is running",
        f"{INSTANCE} is ready for login",
    ]


def test_it_does_not_tell_about_stopping_once_it_has_stopped(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """再開を求めた後、最初に見たときにもう stopping を抜けていれば、
    止まるのを待つとは知らせない。
    """
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopping")), SEARCH
    )
    aws_stubs.ec2.add_response("start_instances", {}, BY_ID)
    for state in ("stopped", "pending", "running"):
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(resumed_instance(state)), BY_ID
        )
    aws_stubs.ssm.add_response(
        "describe_instance_information", pinged(RESUMED_AT + timedelta(seconds=30)), AGENT
    )

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == []
    assert told(capsys.readouterr().out) == [
        f"resuming {INSTANCE}",
        f"{INSTANCE} is running",
        f"{INSTANCE} is ready for login",
    ]


def test_it_waits_ten_minutes_for_a_resumed_instance_to_leave_stopping(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止の途中から再開を求めたとき、stopping を抜けるまでの締め切りは 10 分。"""
    monkeypatch.setattr(wait, "INTERVAL", 60)
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopping")), SEARCH
    )
    aws_stubs.ec2.add_response("start_instances", {}, BY_ID)
    for _ in range(11):
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(resumed_instance("stopping")), BY_ID
        )

    with pytest.raises(TemtError) as caught:
        start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [60] * 10
    assert str(caught.value) == (
        f"timed out after 10 minutes waiting for {INSTANCE} to leave stopping"
    )


def test_it_waits_one_minute_for_a_resumed_instance_to_leave_stopped_after_stopping(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stopping を抜けた後、stopped を抜けるまでの締め切りは、そこから数えて 1 分。

    stopping に 10 分近くかかっても、stopped の段階は 1 分待つ。
    """
    monkeypatch.setattr(wait, "INTERVAL", 60)
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopping")), SEARCH
    )
    aws_stubs.ec2.add_response("start_instances", {}, BY_ID)
    # stopping の段階は 10 回目の休みの後に stopped を見て抜け、stopped の段階は
    # 同じ時刻から 1 分待つ
    for state in ["stopping"] * 10 + ["stopped"] * 3:
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(resumed_instance(state)), BY_ID
        )

    with pytest.raises(TemtError) as caught:
        start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [60] * 11
    assert str(caught.value) == (
        f"timed out after 1 minute waiting for {INSTANCE} to leave stopped"
    )


def test_it_gives_the_reason_when_the_resume_fails(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    """再開がすぐ失敗して stopped に戻れば、1 分で諦めて理由を添える。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopped")), SEARCH
    )
    aws_stubs.ec2.add_response("start_instances", {}, BY_ID)
    failed = {
        **resumed_instance("stopped"),
        "StateReason": {
            "Code": "Client.InternalError",
            "Message": "Client.InternalError: Client error on launch",
        },
    }
    for _ in range(13):
        aws_stubs.ec2.add_response("describe_instances", reservations(failed), BY_ID)

    with pytest.raises(TemtError) as caught:
        start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [5] * 12
    assert str(caught.value) == (
        f"timed out after 1 minute waiting for {INSTANCE} to leave stopped "
        "(Client.InternalError: Client error on launch)"
    )


@pytest.mark.parametrize("state", ["stopped", "stopping"])
@pytest.mark.parametrize("code", ["IncorrectInstanceState", "IncorrectState"])
def test_it_shows_the_refusal_from_aws(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
    state: str,
    code: str,
) -> None:
    """インスタンスの状態が合わないと AWS が断れば、その誤りを見せ、status を促す。"""
    _ = configured
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, state)), SEARCH
    )
    aws_stubs.ec2.add_client_error(
        "start_instances",
        service_error_code=code,
        service_message=REFUSED,
        http_status_code=400,
        expected_params=BY_ID,
    )

    assert cli.main(["temt", "start", "--svc", "dev-main"]) == 1

    aws_stubs.done()
    assert capsys.readouterr().err.endswith(
        f"temt: An error occurred ({code}) when calling the StartInstances operation: "
        f"{REFUSED}; "
        "see 'temt status --svc dev-main' and start again\n"
    )


def test_another_error_from_start_instances_is_not_swallowed(
    aws_stubs: Stubs, configured: Service
) -> None:
    """状態の誤りのほかは、そのまま渡す。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopped")), SEARCH
    )
    aws_stubs.ec2.add_client_error(
        "start_instances",
        service_error_code="UnauthorizedOperation",
        http_status_code=403,
        expected_params=BY_ID,
    )

    with pytest.raises(ClientError, match="UnauthorizedOperation"):
        start.run(configured, is_async=True)

    aws_stubs.done()


def test_an_instance_already_running_counts_only_pings_after_its_launch(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    """既に running でも、起動時刻より前の Online の報告は数えない。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(resumed_instance("running")), SEARCH
    )
    aws_stubs.ssm.add_response(
        "describe_instance_information", pinged(RESUMED_AT - timedelta(seconds=1)), AGENT
    )
    aws_stubs.ssm.add_response(
        "describe_instance_information", pinged(RESUMED_AT + timedelta(minutes=5)), AGENT
    )

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [5]


def test_it_stops_when_more_than_one_instance_is_tagged(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """2 台以上あれば、挙げて終わる。余分なものは、ID を指して terminate で終了する。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "stopped"), an_instance(INSTANCE, "running")),
        SEARCH,
    )

    with pytest.raises(TemtError) as caught:
        start.run(configured, is_async=False)

    assert str(caught.value) == (
        f"2 instances for {NAME}; terminate the extra one with "
        "'temt terminate --svc dev-main <instance-id>' and start again"
    )

    aws_stubs.done()
    assert capsys.readouterr().err.splitlines() == [
        f"instances tagged Name={NAME}:",
        f"  {INSTANCE} (running)",
        f"  {OTHER} (stopped)",
    ]


def test_it_asks_for_apply_when_the_launch_template_is_gone(
    aws_stubs: Stubs, configured: Service
) -> None:
    """確かめた後で消えていても、run-instances の誤りから apply を促す。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    aws_stubs.ec2.add_client_error(
        "run_instances",
        service_error_code=resources.NO_SUCH_TEMPLATE,
        http_status_code=400,
        expected_params=LAUNCH,
    )

    with pytest.raises(
        TemtError, match=f"no launch template {TEMPLATE}; run 'temt apply --svc dev-main' first"
    ):
        start.run(configured, is_async=False)

    aws_stubs.done()


def test_it_asks_for_apply_before_looking_for_instances(
    aws_stubs: Stubs, configured: Service
) -> None:
    """テンプレートはロックの直後に確かめるので、無ければ探しもせず起動もしない。"""
    aws_stubs.ec2.add_client_error(
        "describe_launch_templates",
        service_error_code=resources.NO_SUCH_TEMPLATE,
        http_status_code=400,
        expected_params=TEMPLATES,
    )

    with pytest.raises(
        TemtError, match=f"no launch template {TEMPLATE}; run 'temt apply --svc dev-main' first"
    ):
        start.run(configured, is_async=False)

    # describe-instances は積んでいないので、探せば Stubber が失敗する
    aws_stubs.done()


def test_async_returns_as_soon_as_it_launches(
    aws_stubs: Stubs, configured: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """--async は、run-instances が受け付けられるまでしか待たない。

    起動したインスタンスが探索に見えるのも、running もエージェントも待たない。
    """
    _ = configured
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)

    assert cli.main(["temt", "start", "--svc", "dev-main", "--async"]) == 0

    aws_stubs.done()
    assert clock.slept == []
    assert told(capsys.readouterr().out) == [f"launched {INSTANCE} from {TEMPLATE}"]


@pytest.mark.parametrize("state", ["shutting-down", "terminated"])
def test_it_stops_when_the_new_instance_goes_away_right_after_launch(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    """起動してすぐ終わりに向かえば、締め切りを待たずに終了する。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, state)), BY_ID
    )

    with pytest.raises(TemtError, match=f"{INSTANCE} went to {state} while waiting to be running"):
        start.run(configured, is_async=False)

    # エージェントは積んでいないので、待てば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == []
    assert told(capsys.readouterr().out) == [f"launched {INSTANCE} from {TEMPLATE}"]


@pytest.mark.parametrize("launch", [True, False])
def test_async_does_not_use_ssm(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch, launch: bool
) -> None:
    """--async はエージェントを待たないので、SSM のクライアントを作らない。"""
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
    applied(aws_stubs)
    if launch:
        aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
        launched(aws_stubs)
    else:
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
        )

    start.run(configured, is_async=True)

    aws_stubs.done()
    assert made == ["ec2"]


@pytest.mark.parametrize("state", ["pending", "running"])
def test_async_does_not_wait_for_an_instance_it_found(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    _ = configured
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, state)), SEARCH
    )

    assert cli.main(["temt", "start", "--svc", "dev-main", "--async"]) == 0

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"{INSTANCE} is already {state}"]


def test_it_keeps_waiting_while_the_new_instance_is_not_found_by_id(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    """起動した直後は、ID で引いても見えないことがある。その誤りでは止まらない。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "describe_instances",
        service_error_code=instances.NOT_FOUND,
        http_status_code=400,
        expected_params=BY_ID,
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running", address=ADDRESS)), BY_ID
    )
    aws_stubs.ssm.add_response("describe_instance_information", agent("Online"), AGENT)

    start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [5]


def test_another_error_from_describe_instances_is_not_swallowed(
    aws_stubs: Stubs, configured: Service
) -> None:
    """待ち合わせが飲み込むのは、そのインスタンスが見えないという誤りだけである。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "describe_instances",
        service_error_code="UnauthorizedOperation",
        http_status_code=403,
        expected_params=BY_ID,
    )

    with pytest.raises(ClientError, match="UnauthorizedOperation"):
        start.run(configured, is_async=False)

    aws_stubs.done()


def test_it_stops_when_the_instance_goes_away_while_it_waits(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    """running を待つ間に終わってしまったら、締め切りを待たずに終了する。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )

    with pytest.raises(
        TemtError, match=f"{INSTANCE} went to terminated while waiting to be running"
    ):
        start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == []


def test_it_gives_up_when_the_instance_does_not_become_running(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """締め切りは 10 分。どの段階が終わらなかったかを述べる。"""
    monkeypatch.setattr(wait, "INTERVAL", 120)
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)
    for _ in range(6):
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(an_instance(INSTANCE, "pending")), BY_ID
        )

    with pytest.raises(
        TemtError, match=f"timed out after 10 minutes waiting for {INSTANCE} to be running"
    ):
        start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [120] * 5


def test_it_gives_up_when_the_agent_stays_offline(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """締め切りは 5 分。running にはなっているので、直接接続は使える。"""
    monkeypatch.setattr(wait, "INTERVAL", 60)
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    for _ in range(6):
        aws_stubs.ssm.add_response("describe_instance_information", agent(""), AGENT)

    # 何を待って何が起きたかだけを出し、どうするかは添えない
    with pytest.raises(
        TemtError,
        match=f"^timed out after 5 minutes waiting for the SSM agent on {INSTANCE} to come online$",
    ):
        start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == [60] * 5


def test_it_adds_no_hint_when_a_new_agent_stays_offline(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動したばかりでも、何が起きたかだけを出し、起動し直すよう添えない。

    apply の直後に起動すると、エージェントがロールの資格情報を取れずに止まったり、
    消えた古いインスタンスプロファイルが付いたりする。直し方は文書に書く。
    """
    monkeypatch.setattr(wait, "INTERVAL", 60)
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    launched(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    for _ in range(6):
        aws_stubs.ssm.add_response("describe_instance_information", agent(""), AGENT)

    with pytest.raises(
        TemtError,
        match=f"^timed out after 5 minutes waiting for the SSM agent on {INSTANCE} to come online$",
    ):
        start.run(configured, is_async=False)

    aws_stubs.done()


def profile_refused(stubs: Stubs, name: str) -> None:
    """EC2 がインスタンスプロファイル name を見つけられずに run-instances を断る。"""
    stubs.ec2.add_client_error(
        "run_instances",
        service_error_code=instances.INVALID_PARAMETER,
        service_message=f"Value ({name}) for parameter iamInstanceProfile.name is invalid. "
        "Invalid IAM Instance Profile name",
        http_status_code=400,
        expected_params=LAUNCH,
    )


def test_it_stops_when_ec2_does_not_see_the_instance_profile(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    """apply の直後は、待ってやり直さず、何が起きたかだけを知らせて止まる。

    いちばん早い時点で起動すると、エージェントがロールの資格情報を取れないことがある。
    """
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    profile_refused(aws_stubs, NAME)

    with pytest.raises(
        TemtError,
        match=f"^EC2 does not see the instance profile {NAME}$",
    ):
        start.run(configured, is_async=False)

    aws_stubs.done()
    assert clock.slept == []


def test_it_passes_on_a_refused_instance_profile_of_another_name(
    aws_stubs: Stubs, configured: Service
) -> None:
    """テンプレートを手で別のプロファイルに替えていれば、AWS の誤りをそのまま上げる。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    profile_refused(aws_stubs, "other")

    with pytest.raises(ClientError, match=r"Value \(other\)"):
        start.run(configured, is_async=False)

    aws_stubs.done()


def test_the_deadlines_are_the_ones_in_the_design() -> None:
    """running になるまで 10 分、ログインできるまで 5 分、前の状態が見えなくなるまで 1 分。

    停止の途中から再開を求めたとき、stopping を抜けるまでは止まるまでと同じ 10 分。
    ポートにつながるかを 1 回確かめるときの締め切りは 5 秒。
    """
    assert (instances.RUNNING_DEADLINE, instances.LOGIN_DEADLINE) == (600, 300)
    assert instances.STOPPED_DEADLINE == 600
    assert instances.LEAVE_DEADLINE == 60
    assert instances.CONNECT_TIMEOUT == 5


def test_it_stops_when_another_command_holds_the_lock(
    aws_stubs: Stubs, configured: Service
) -> None:
    """ロックはテンプレートを確かめるより先に取る。取れなければ待たずに終わる。"""
    held = home.prepare_known_hosts(configured) / lock.LOCK_NAME
    with held.open("a", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            deadline(5.0),
            pytest.raises(TemtError, match=f"another temt command is running for {NAME}"),
        ):
            start.run(configured, is_async=False)

    # 応答を 1 つも積んでいないので、AWS を読んでいれば Stubber が失敗する
    aws_stubs.done()


def test_it_holds_the_lock_while_it_waits(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """待ち合わせの間もロックを持っている。別の start は入ってこない。"""
    attempts: list[bool] = []

    def probe(seconds: float) -> None:
        clock.sleep(seconds)
        attempts.append(taken(configured))

    monkeypatch.setattr(wait, "sleep", probe)
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "pending")), SEARCH
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "pending")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running", address=ADDRESS)), BY_ID
    )
    aws_stubs.ssm.add_response("describe_instance_information", agent("Online"), AGENT)

    with deadline(5.0):
        start.run(configured, is_async=False)

    aws_stubs.done()
    assert attempts == [False]
    # 終わればロックは離す
    assert taken(configured)
