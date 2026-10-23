"""instance show（短縮形 status）と instance list の本体のテスト。AWS は Stubber で差し替える。

instance show は端から端まで（status.run か cli.main）を通し、表示の形を確かめる。
"""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import (
    REGION,
    Stubs,
    a_manifest,
    a_record,
    an_instance,
    commanded,
    online,
    reservations,
)

from temt import aws, cli, conf, instances, resources, status
from temt.msg import TemtError, UsageError
from temt.names import Service, user_apps_dir

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
ADDRESS = "203.0.113.10"

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
# 起動テンプレートがあるときの 1 行目
APPLIED = f"template:   {TEMPLATE}"
# アプリの状態が 1 つも無いときの最後の行
NO_STATUS = "app status: none"
PATH = f"/{NAME}"


def applied(stubs: Stubs) -> None:
    """起動テンプレートがある、という応答を積む。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )


def agents(stubs: Stubs, ids: list[str], pings: dict[str, str]) -> None:
    """describe-instance-information の応答を積む。pings に無い ID は未登録。"""
    stubs.ssm.add_response(
        "describe_instance_information",
        {
            "InstanceInformationList": [
                {"InstanceId": instance_id, "PingStatus": ping}
                for instance_id, ping in pings.items()
            ]
        },
        {"Filters": [{"Key": "InstanceIds", "Values": ids}]},
    )


def statuses(stubs: Stubs, *parameters: dict[str, Any]) -> None:
    """get-parameters-by-path の応答を積む。/<名前> の下にある SSM パラメータ。"""
    stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": list(parameters)},
        {"Path": PATH, "Recursive": True},
    )


def a_status(app: str, value: str, ago: timedelta, kind: str = "String") -> dict[str, Any]:
    """アプリが書いた状態のパラメータ 1 つ。ago 前に書かれた。

    status は本物の時計で経過時間を測るので、テストが動く間に表示が変わらないよう、
    ago は単位の境目から離して選ぶ。
    """
    return {
        "Name": f"{PATH}/{app}/status",
        "Type": kind,
        "Value": value,
        "LastModifiedDate": datetime.now(UTC) - ago,
    }


def test_it_shows_a_running_instance(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    # 表示は標準出力に書く
    assert err == ""
    assert out == (
        f"template:   {TEMPLATE}\n"
        f"instance:   {INSTANCE} (running)\n"
        f"ipv4:       {ADDRESS}\n"
        "ssm-agent:  Online\n"
        f"{NO_STATUS}\n"
    )
    # ロックは取らないので、known_hosts のディレクトリも作らない
    assert not configured.known_hosts_dir.exists()


def test_it_shows_nothing_running(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    # 1 台も無ければ、エージェントは尋ねない。アプリの状態は見る
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().out == f"{APPLIED}\ninstance:   none\n{NO_STATUS}\n"


def test_it_shows_every_instance(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """2 台以上あれば全部を表示し、エージェントは 1 回の問い合わせで尋ねる。

    余分なものを ID を指して終了するよう警告する。
    """
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(
            an_instance(OTHER, "stopping"), an_instance(INSTANCE, "running", address=ADDRESS)
        ),
        SEARCH,
    )
    # 応答の順は ID の順になる。OTHER は一度も登録していない
    agents(aws_stubs, [INSTANCE, OTHER], {INSTANCE: "ConnectionLost"})
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        APPLIED,
        f"instance:   {INSTANCE} (running)",
        f"ipv4:       {ADDRESS}",
        "ssm-agent:  ConnectionLost",
        f"instance:   {OTHER} (stopping)",
        "ipv4:       (none)",
        "ssm-agent:  (not registered)",
        NO_STATUS,
    ]
    assert err.splitlines() == [
        f"temt: warning: 2 instances for {NAME}; terminate the extra one with "
        "'temt terminate --svc dev-main <instance-id>'"
    ]


def test_it_does_not_count_an_instance_that_is_shutting_down(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """終了の途中のものは表示するが、2 台とは数えず、警告しない。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "shutting-down"), an_instance(INSTANCE, "running")),
        SEARCH,
    )
    agents(aws_stubs, [INSTANCE, OTHER], {INSTANCE: "Online"})
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert f"instance:   {OTHER} (shutting-down)" in out.splitlines()
    assert err == ""


def test_it_takes_an_answer_with_missing_items(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """AWS が省いた項目は、無いものとして扱う。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running"), an_instance(OTHER, "pending")),
        SEARCH,
    )
    aws_stubs.ssm.add_response(
        "describe_instance_information",
        {"InstanceInformationList": [{"InstanceId": INSTANCE}, {"PingStatus": "Online"}]},
        {"Filters": [{"Key": "InstanceIds", "Values": [INSTANCE, OTHER]}]},
    )
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        APPLIED,
        f"instance:   {INSTANCE} (running)",
        "ipv4:       (none)",
        "ssm-agent:  (not registered)",
        f"instance:   {OTHER} (pending)",
        "ipv4:       (none)",
        "ssm-agent:  (not registered)",
        NO_STATUS,
    ]
    assert err.splitlines() == [
        f"temt: warning: 2 instances for {NAME}; terminate the extra one with "
        "'temt terminate --svc dev-main <instance-id>'"
    ]


def test_it_takes_an_answer_without_the_list(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    aws_stubs.ssm.add_response(
        "describe_instance_information",
        {},
        {"Filters": [{"Key": "InstanceIds", "Values": [INSTANCE]}]},
    )
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines()[3:] == [
        "ssm-agent:  (not registered)",
        NO_STATUS,
    ]


def test_it_shows_that_the_service_is_not_applied(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """起動テンプレートが無くても止めず、取り残されたインスタンスも表示する。"""
    aws_stubs.ec2.add_client_error(
        "describe_launch_templates",
        service_error_code=resources.NO_SUCH_TEMPLATE,
        http_status_code=400,
        expected_params=TEMPLATES,
    )
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "template:   none (not applied; run 'temt apply --svc dev-main')",
        f"instance:   {INSTANCE} (running)",
        f"ipv4:       {ADDRESS}",
        "ssm-agent:  Online",
        NO_STATUS,
    ]


def test_other_errors_from_reading_the_template_are_not_swallowed(
    aws_stubs: Stubs, configured: Service
) -> None:
    """テンプレートが無いことのほかの誤りは、apply されていないことにせず、そこで止まる。"""
    aws_stubs.ec2.add_client_error(
        "describe_launch_templates",
        service_error_code="UnauthorizedOperation",
        http_status_code=403,
        expected_params=TEMPLATES,
    )
    with pytest.raises(ClientError) as raised:
        status.run(configured)
    # 先へ進んでいれば、積んでいない describe-instances で Stubber が失敗する
    aws_stubs.done()
    assert raised.value.operation_name == "DescribeLaunchTemplates"
    assert raised.value.response.get("Error", {}).get("Code") == "UnauthorizedOperation"


def test_it_asks_for_apply_when_there_is_no_config_file(aws_stubs: Stubs, service: Service) -> None:
    with pytest.raises(TemtError, match="run 'temt apply --svc dev-main' first"):
        status.run(service)
    assert aws_stubs.passed == []


def test_main_runs_it(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    before = configured.conf_path.read_text(encoding="utf-8")
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    statuses(aws_stubs)
    assert cli.main(["temt", "--profile", "work", "status", "--svc", "dev-main"]) == 0
    aws_stubs.done()
    # --profile はその実行だけに効き、設定ファイルには書かない
    assert conf.OPTION_PROFILE == "work"
    assert aws_stubs.passed[0].AwsProfile == "work"
    assert configured.conf_path.read_text(encoding="utf-8") == before
    out, err = capsys.readouterr()
    assert out == f"{APPLIED}\ninstance:   none\n{NO_STATUS}\n"
    assert err == ""


def test_it_shows_the_app_statuses(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """/<名前>/<アプリID>/status だけを、アプリ ID の順に、経過時間とともに出す。

    ほかのキー、階層の深いもの、規則に外れたアプリ ID は出さない。SecureString は
    値の代わりに (SecureString) を出し、表示できない文字は ? にする。インスタンスが
    起動するより前に書かれたものには stale を添える。
    """
    applied(aws_stubs)
    running = an_instance(INSTANCE, "running", address=ADDRESS)
    running["LaunchTime"] = datetime.now(UTC) - timedelta(hours=1)
    aws_stubs.ec2.add_response("describe_instances", reservations(running), SEARCH)
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    statuses(
        aws_stubs,
        a_status("myapp", "3 job(s)\tqueued", timedelta(minutes=12)),
        {"Name": f"{PATH}/myapp/deploy", "Type": "String", "Value": "{}"},
        {"Name": f"{PATH}/myapp/sub/status", "Type": "String", "Value": "deep"},
        {"Name": f"{PATH}/Bad_ID/status", "Type": "String", "Value": "bad"},
        a_status("idle-shutdown", "power off in 42m", timedelta(seconds=30)),
        a_status("secret", "AQEC...", timedelta(days=2, hours=5), kind="SecureString"),
    )
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines()[4:] == [
        "app status:",
        "  idle-shutdown  power off in 42m  [just now]",
        "  myapp          3 job(s)?queued  [12m ago]",
        "  secret         (SecureString)  [2d ago, stale]",
    ]


def test_it_goes_through_every_page_of_statuses(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": [a_status("b", "two", timedelta(hours=3))], "NextToken": "t"},
        {"Path": PATH, "Recursive": True},
    )
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": [a_status("a", "one", timedelta(minutes=1, seconds=30))]},
        {"Path": PATH, "Recursive": True, "NextToken": "t"},
    )
    status.run(configured)
    aws_stubs.done()
    # 動いているインスタンスが無いので、どれも stale
    assert capsys.readouterr().out.splitlines()[2:] == [
        "app status:",
        "  a  one  [1m ago, stale]",
        "  b  two  [3h ago, stale]",
    ]


def test_it_marks_the_statuses_of_a_stopped_instance_stale(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """止まったインスタンスは書けないので、起動した後に書かれたものでも stale を添える。"""
    applied(aws_stubs)
    stopped = an_instance(INSTANCE, "stopped")
    stopped["LaunchTime"] = datetime.now(UTC) - timedelta(hours=1)
    aws_stubs.ec2.add_response("describe_instances", reservations(stopped), SEARCH)
    agents(aws_stubs, [INSTANCE], {INSTANCE: "ConnectionLost"})
    statuses(
        aws_stubs,
        a_status("idle-shutdown", "in use: 1 ssh session(s) (limit 1h)", timedelta(minutes=10)),
    )
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "app status:",
        "  idle-shutdown  in use: 1 ssh session(s) (limit 1h)  [10m ago, stale]",
    ]


def local_app(app_id: str, version: str) -> None:
    """アプリ ID で使える手元のアプリを置く。"""
    path = user_apps_dir() / app_id
    path.mkdir(parents=True)
    (path / "manifest.toml").write_text(a_manifest(version), encoding="utf-8")


def test_apps_shows_the_apps_on_the_running_instance(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """--apps を付ければ、Run Command で読み、入っているアプリをアプリ ID の順に表で出す。

    BAKED は、記録の instance がほかのインスタンスなら yes、このインスタンスなら no、
    記録が無ければ -。手元のアプリの版が違えば、VERSION に * を付ける。
    """
    local_app("jupyter", "1.3")
    local_app("emacs", "2")
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    commanded(
        aws_stubs,
        INSTANCE,
        {
            "jupyter": {"manifest.toml": a_manifest("1.2"), "temt-deploy.json": a_record(INSTANCE)},
            "emacs": {"manifest.toml": a_manifest("2"), "temt-deploy.json": a_record(OTHER)},
            "old": {"manifest.toml": a_manifest("1")},
        },
    )
    statuses(aws_stubs)
    status.run(configured, apps=True)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert err == ""
    assert out.splitlines()[3:] == [
        "ssm-agent:  Online",
        "apps:",
        "  ID       VERSION  BAKED  DEPLOYED",
        "  emacs    2        yes    2026-09-27 12:04:05",
        "  jupyter  1.2*     no     2026-09-27 12:04:05",
        "  old      1        -      -",
        NO_STATUS,
    ]


@pytest.mark.parametrize(
    ("state", "ping", "why"),
    [
        ("stopped", "ConnectionLost", f"{INSTANCE} is stopped"),
        ("running", "ConnectionLost", f"the SSM agent on {INSTANCE} is not online"),
    ],
)
def test_apps_tells_why_it_does_not_read(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
    state: str,
    ping: str,
    why: str,
) -> None:
    """running のインスタンスが無いか、SSM エージェントが Online でなければ、読まずに理由を出す。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, state)), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: ping})
    if state == "running":
        online(aws_stubs, INSTANCE, ping)
    statuses(aws_stubs)
    status.run(configured, apps=True)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines()[-2:] == [
        f"apps:       (not read; {why})",
        NO_STATUS,
    ]


def test_apps_goes_on_when_the_command_fails(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """読めなければ、警告して、ほかの表示を続ける。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    commanded(aws_stubs, INSTANCE, {}, status="Failed", error="tar: \x1b[2J broken\nmore")
    statuses(aws_stubs)
    status.run(configured, apps=True)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out.splitlines()[-2:] == [f"apps:       (not read; cannot read {INSTANCE})", NO_STATUS]
    assert err == f"temt: warning: cannot read the apps on {INSTANCE}: Failed: tar: ?[2J broken\n"


def test_apps_goes_on_when_aws_refuses_the_command(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """権限が無いなどの AWS の誤りでも、警告して、app status の表示を続ける。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    online(aws_stubs, INSTANCE)
    aws_stubs.ssm.add_client_error(
        "send_command", service_error_code="AccessDeniedException", http_status_code=400
    )
    statuses(aws_stubs)
    status.run(configured, apps=True)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out.splitlines()[-2:] == [f"apps:       (not read; cannot read {INSTANCE})", NO_STATUS]
    assert err.startswith(f"temt: warning: cannot read the apps on {INSTANCE}: ")
    assert "AccessDenied" in err


def test_apps_without_any_app(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """アプリを置くディレクトリが無ければ、出力は空で、アプリは無い。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    commanded(aws_stubs, INSTANCE, {})
    statuses(aws_stubs)
    status.run(configured, apps=True)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines()[-2:] == ["apps:       none", NO_STATUS]


NOW = datetime(2026, 9, 29, 6, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("ago", "shown"),
    [
        (timedelta(seconds=-30), "just now"),
        (timedelta(seconds=59), "just now"),
        (timedelta(seconds=60), "1m ago"),
        (timedelta(seconds=3599), "59m ago"),
        (timedelta(hours=1), "1h ago"),
        (timedelta(hours=23, minutes=59), "23h ago"),
        (timedelta(days=5, hours=1), "5d ago"),
    ],
)
def test_age(ago: timedelta, shown: str) -> None:
    """60 秒未満は just now。それ以上は、いちばん大きい単位 1 つで切り捨てる。"""
    assert status.age(NOW - ago, NOW) == shown


def test_age_compares_aware_times_across_time_zones() -> None:
    """LastModifiedDate が UTC 以外のタイムゾーンで返っても、差はタイムゾーンを踏まえる。"""
    jst = timezone(timedelta(hours=9))
    assert status.age(datetime(2026, 9, 29, 14, 57, 0, tzinfo=jst), NOW) == "3m ago"


def test_age_of_what_is_not_a_time() -> None:
    assert status.age(None, NOW) == "?"
    assert status.age("2026-09-29T06:00:00Z", NOW) == "?"


# test_stale のインスタンスが起動した時刻
BOOTED = NOW - timedelta(hours=1)


def an_instance_launched(state: str, launched: datetime | None = BOOTED) -> instances.Instance:
    """launched に起動した、state のインスタンス。"""
    return instances.Instance(
        id=INSTANCE, state=state, public_ipv4="", kept_devices=(), age=None, launched=launched
    )


@pytest.mark.parametrize(
    ("modified", "found", "expected"),
    [
        pytest.param(NOW, [], True, id="no instance"),
        pytest.param(NOW, [an_instance_launched("running")], False, id="after launch"),
        pytest.param(BOOTED, [an_instance_launched("running")], False, id="at launch"),
        pytest.param(
            BOOTED - timedelta(seconds=1),
            [an_instance_launched("running")],
            True,
            id="before launch",
        ),
        pytest.param(NOW, [an_instance_launched("pending")], False, id="pending"),
        # 止まったインスタンスは書けないので、停止する前に書いたものも古い
        pytest.param(NOW, [an_instance_launched("stopping")], True, id="stopping"),
        pytest.param(NOW, [an_instance_launched("stopped")], True, id="stopped"),
        pytest.param(NOW, [an_instance_launched("shutting-down")], True, id="shutting down"),
        # 比べられなければ、古いとはみなさない
        pytest.param(
            BOOTED - timedelta(days=1),
            [an_instance_launched("running", None)],
            False,
            id="unknown launch",
        ),
        pytest.param(None, [an_instance_launched("running")], False, id="unknown modified"),
        # 動いているインスタンスが無ければ、更新時刻が読めなくても古い
        pytest.param(None, [], True, id="unknown modified and no instance"),
        # 複数台なら、動いているもののどれかが書いたとみなせれば古くない
        pytest.param(
            BOOTED,
            [an_instance_launched("running"), an_instance_launched("pending", NOW)],
            False,
            id="one launched before",
        ),
        pytest.param(
            BOOTED - timedelta(seconds=1),
            [an_instance_launched("running"), an_instance_launched("pending", NOW)],
            True,
            id="all launched after",
        ),
        pytest.param(
            BOOTED - timedelta(seconds=1),
            [an_instance_launched("running", None), an_instance_launched("running")],
            False,
            id="one unknown launch",
        ),
        # 止まっているものは数えない
        pytest.param(
            BOOTED - timedelta(seconds=1),
            [
                an_instance_launched("stopped", NOW - timedelta(days=1)),
                an_instance_launched("running"),
            ],
            True,
            id="stopped and running",
        ),
    ],
)
def test_stale(modified: Any, found: list[instances.Instance], expected: bool) -> None:
    """pending か running のインスタンスが、書かれたより前に起動していなければ古い。"""
    assert status.stale(modified, found) is expected


LIVE = ["pending", "running", "stopping", "stopped", "shutting-down"]
ALL_TEMT = {
    "Filters": [
        {"Name": "tag-key", "Values": ["temt:service"]},
        {"Name": "instance-state-name", "Values": LIVE},
    ]
}
LAUNCHED = datetime(2026, 9, 26, 0, 12, 34, tzinfo=UTC)


def a_temt_instance(
    instance_id: str, svc: str, state: str, *, launched: datetime = LAUNCHED, address: str = ""
) -> dict[str, Any]:
    """describe-instances の応答の、temt のインスタンス 1 台分。"""
    item = an_instance(instance_id, state, address=address)
    item.update(
        {
            "InstanceType": "t3.medium",
            "ImageId": "ami-0123456789abcdef0",
            "LaunchTime": launched,
            "Tags": [
                {"Key": "Name", "Value": f"temt-{svc}"},
                {"Key": "temt:service", "Value": svc},
            ],
        }
    )
    return item


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, aws_stubs: Stubs) -> list[str | None]:
    """aws.environment を、aws_stubs のクライアントを返すものに差し替える。

    渡されたリージョンを返す。環境の行は aws のテストが確かめる。
    """
    regions: list[str | None] = []
    session = aws.session  # aws_stubs が差し替えたもの

    def environment(region: str | None) -> Any:
        regions.append(region)
        return session(conf.Conf())

    monkeypatch.setattr(aws, "environment", environment)
    return regions


def test_list_shows_every_temt_instance(
    aws_stubs: Stubs, environment: list[str | None], capsys: pytest.CaptureFixture[str]
) -> None:
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(
            a_temt_instance(OTHER, "web", "stopped"),
            a_temt_instance(INSTANCE, "dev-main", "running", address=ADDRESS),
            a_temt_instance(
                "i-0000000000000000a",
                "dev-main",
                "shutting-down",
                launched=datetime(2026, 9, 25, tzinfo=UTC),
            ),
        ),
        ALL_TEMT,
    )

    status.list_instances(None, "us-east-1", None)

    aws_stubs.done()
    assert environment == ["us-east-1"]
    out, err = capsys.readouterr()
    assert err == ""
    # サービス名、起動時刻、ID の順に並ぶ
    assert out.splitlines() == [
        "ID                    SERVICE    STATE           TYPE        LAUNCHED",
        "i-0000000000000000a   dev-main   shutting-down   t3.medium   2026-09-25 09:00:00",
        f"{INSTANCE}   dev-main   running         t3.medium   2026-09-26 09:12:34",
        f"{OTHER}   web        stopped         t3.medium   2026-09-26 09:12:34",
    ]


def test_list_of_a_service_uses_its_config_file(
    aws_stubs: Stubs,
    environment: list[str | None],
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(a_temt_instance(INSTANCE, "dev-main", "running", address=ADDRESS)),
        {
            "Filters": [
                {"Name": "tag:temt:service", "Values": ["dev-main"]},
                {"Name": "instance-state-name", "Values": LIVE},
            ]
        },
    )

    status.list_instances(configured, None, "ip,Image,id")

    aws_stubs.done()
    assert environment == []
    assert [one.AwsRegion for one in aws_stubs.passed] == [REGION]
    assert capsys.readouterr().out.splitlines() == [
        "IP             IMAGE                   ID",
        f"{ADDRESS}   ami-0123456789abcdef0   {INSTANCE}",
    ]


def test_list_without_instances(
    aws_stubs: Stubs, environment: list[str | None], capsys: pytest.CaptureFixture[str]
) -> None:
    """1 台も無ければ見出しだけを出す。値の無いところは - と出す。"""
    aws_stubs.ec2.add_response("describe_instances", reservations(), ALL_TEMT)

    status.list_instances(None, None, "IP")

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == ["IP"]


def test_list_shows_a_dash_for_what_is_missing(
    aws_stubs: Stubs, environment: list[str | None], capsys: pytest.CaptureFixture[str]
) -> None:
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(a_temt_instance(INSTANCE, "web", "stopped")), ALL_TEMT
    )

    status.list_instances(None, None, "ID,IP")

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == ["ID                    IP", f"{INSTANCE}   -"]


def test_list_checks_the_columns_before_aws(
    aws_stubs: Stubs, environment: list[str | None]
) -> None:
    with pytest.raises(UsageError, match="unknown column in --format: ssm; use ID,SERVICE,"):
        status.list_instances(None, None, "ID,ssm")
    assert environment == []


def test_list_goes_through_every_page(
    aws_stubs: Stubs, environment: list[str | None], capsys: pytest.CaptureFixture[str]
) -> None:
    first = reservations(a_temt_instance(INSTANCE, "web", "running"))
    first["NextToken"] = "t1"
    aws_stubs.ec2.add_response("describe_instances", first, ALL_TEMT)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(a_temt_instance(OTHER, "dev-main", "stopped")),
        {**ALL_TEMT, "NextToken": "t1"},
    )

    status.list_instances(None, None, "SERVICE,ID")

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "SERVICE    ID",
        f"dev-main   {OTHER}",
        f"web        {INSTANCE}",
    ]


def test_list_shows_a_dash_for_an_empty_service_tag(
    aws_stubs: Stubs, environment: list[str | None], capsys: pytest.CaptureFixture[str]
) -> None:
    """temt:service の値が空でも、タグはあるので挙げる。空の値は - と出し、先頭に並べる。"""
    empty = a_temt_instance(OTHER, "", "running")
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(a_temt_instance(INSTANCE, "web", "running"), empty),
        ALL_TEMT,
    )

    status.list_instances(None, None, "SERVICE,ID")

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "SERVICE   ID",
        f"-         {OTHER}",
        f"web       {INSTANCE}",
    ]


def test_list_of_a_service_without_its_config_file(
    aws_stubs: Stubs, environment: list[str | None], service: Service
) -> None:
    with pytest.raises(TemtError, match="run 'temt apply --svc dev-main'"):
        status.list_instances(service, None, None)
    aws_stubs.done()
