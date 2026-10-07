"""instance show（短縮形 status）と instance list の本体のテスト。AWS は Stubber で差し替える。

instance show は端から端まで（status.run か cli.main）を通し、表示の形を確かめる。
"""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import (
    ASSOCIATION,
    OLD_VERSION,
    REGION,
    SSHD,
    Stubs,
    an_instance,
    association_ran,
    listed_associations,
    reservations,
)

from temt import app, aws, cli, conf, instances, resources, status
from temt.msg import TemtError, UsageError
from temt.names import Service

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
# 名前が同じ、別の関連付け <名前>-sshd の ID
OTHER_ASSOCIATION = "99999999-8888-7777-6666-555555555555"
ADDRESS = "203.0.113.10"

TEMPLATES = {"LaunchTemplateNames": [TEMPLATE]}
SEARCH = {
    "Filters": [
        {"Name": "tag:Name", "Values": [NAME]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        },
    ]
}
# 関連付けそのものが無いときの sshd-ports の行
NO_ASSOCIATION = f"sshd-ports: none (no association {SSHD}; run 'temt apply --svc dev-main')"
# 起動テンプレートがあるときの 1 行目
APPLIED = f"template:   {TEMPLATE}"
# アプリの状態が 1 つも無いときの最後の行
NO_STATUS = "app status: none"
# deploy の記録が 1 つも無いときの、焼いていないアプリの行
NO_APPS = "apps:       none"
PATH = f"/{NAME}"
# 起動テンプレートが指す AMI と、ほかの AMI
IMAGE = "ami-0123456789abcdef0"
OTHER_IMAGE = "ami-0fedcba9876543210"


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


def sshd_ran(stubs: Stubs, instance_id: str, *runs: dict[str, Any]) -> None:
    """describe-instance-associations-status の応答を積む。runs が無ければ一覧に無い。"""
    stubs.ssm.add_response(
        "describe_instance_associations_status",
        {"InstanceAssociationStatusInfos": list(runs)},
        {"InstanceId": instance_id},
    )


def apps(stubs: Stubs, *parameters: dict[str, Any]) -> None:
    """焼いたかどうかを決めるのに読む、起動テンプレートの AMI と deploy の記録。

    テンプレートがあるときだけ読む。
    """
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {
                    "VersionNumber": 1,
                    "DefaultVersion": True,
                    "LaunchTemplateData": {"ImageId": IMAGE},
                }
            ]
        },
        {"LaunchTemplateName": TEMPLATE},
    )
    stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": list(parameters)},
        {"Path": PATH, "Recursive": True},
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
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, INSTANCE, association_ran("Success"))
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    # 標準出力には何も書かない
    assert out == ""
    assert err == (
        f"template:   {TEMPLATE}\n"
        f"instance:   {INSTANCE} (running)\n"
        f"ipv4:       {ADDRESS}\n"
        "ssm-agent:  Online\n"
        "sshd-ports: applied\n"
        f"{NO_APPS}\n"
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
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err == f"{APPLIED}\ninstance:   none\n{NO_APPS}\n{NO_STATUS}\n"


def test_it_shows_every_instance(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """2 台以上あれば全部を表示し、エージェントは 1 回の問い合わせで尋ねる。

    関連付けの今の版も 1 回だけ読み、インスタンスごとの状態はそれぞれ読む。余分な
    ものを ID を指して終了するよう警告する。
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
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, INSTANCE, association_ran("InProgress"))
    sshd_ran(aws_stubs, OTHER)
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines() == [
        APPLIED,
        f"instance:   {INSTANCE} (running)",
        f"ipv4:       {ADDRESS}",
        "ssm-agent:  ConnectionLost",
        "sshd-ports: pending",
        f"instance:   {OTHER} (stopping)",
        "ipv4:       (none)",
        "ssm-agent:  (not registered)",
        "sshd-ports: (not run)",
        f"temt: warning: 2 instances for {NAME}; terminate the extra one with "
        "'temt terminate --svc dev-main <instance-id>'",
        NO_APPS,
        NO_STATUS,
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
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, INSTANCE)
    sshd_ran(aws_stubs, OTHER)
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    err = capsys.readouterr().err
    assert f"instance:   {OTHER} (shutting-down)" in err.splitlines()
    assert "warning" not in err


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
    # 関連付けそのものが無ければ、インスタンスでの状態は読まずに apply を促す
    listed_associations(aws_stubs.ssm)
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines() == [
        APPLIED,
        f"instance:   {INSTANCE} (running)",
        "ipv4:       (none)",
        "ssm-agent:  (not registered)",
        NO_ASSOCIATION,
        f"instance:   {OTHER} (pending)",
        "ipv4:       (none)",
        "ssm-agent:  (not registered)",
        NO_ASSOCIATION,
        f"temt: warning: 2 instances for {NAME}; terminate the extra one with "
        "'temt terminate --svc dev-main <instance-id>'",
        NO_APPS,
        NO_STATUS,
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
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, INSTANCE, association_ran("Failed"))
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines()[3:] == [
        "ssm-agent:  (not registered)",
        "sshd-ports: failed",
        NO_APPS,
        NO_STATUS,
    ]


@pytest.mark.parametrize(
    ("runs", "shown"),
    [
        pytest.param([association_ran("Success")], "applied", id="success"),
        pytest.param([association_ran("Queued")], "pending", id="queued"),
        pytest.param([association_ran("Pending")], "pending", id="pending"),
        pytest.param([association_ran("InProgress")], "pending", id="in progress"),
        # update-association の直後は、前の版の状態が残っている
        pytest.param([association_ran("Success", OLD_VERSION)], "pending", id="previous success"),
        pytest.param([association_ran("Failed", OLD_VERSION)], "pending", id="previous failed"),
        pytest.param(
            [association_ran("Failed", OLD_VERSION), association_ran("Success")],
            "applied",
            id="previous and current",
        ),
        pytest.param([association_ran("Failed")], "failed", id="failed"),
        pytest.param([association_ran("TimedOut")], "timedout", id="timed out"),
        pytest.param([], "(not run)", id="not run"),
        # 名前で見つけた関連付けと ID の違うものの状態は、名前が同じでも数えない
        pytest.param(
            [association_ran("Success", association_id=OTHER_ASSOCIATION)],
            "(not run)",
            id="another association",
        ),
    ],
)
def test_it_shows_the_sshd_ports_of_the_current_version(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
    runs: list[dict[str, Any]],
    shown: str,
) -> None:
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, INSTANCE, *runs)
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines()[-3] == f"sshd-ports: {shown}"


def test_it_shows_that_the_service_is_not_applied(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """起動テンプレートが無くても止めず、取り残されたインスタンスも表示する。

    destroy のあとなので、関連付けも無い。
    """
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
    listed_associations(aws_stubs.ssm)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines() == [
        "template:   none (not applied; run 'temt apply --svc dev-main')",
        f"instance:   {INSTANCE} (running)",
        f"ipv4:       {ADDRESS}",
        "ssm-agent:  Online",
        NO_ASSOCIATION,
        NO_STATUS,
    ]


def test_it_asks_for_apply_when_there_is_no_association(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """関連付けそのものが無ければ、apply を促す。

    インスタンスでの状態は読まない。積んでいないので、読めば失敗する。
    """
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    listed_associations(aws_stubs.ssm)
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines()[-3:] == [NO_ASSOCIATION, NO_APPS, NO_STATUS]


def test_it_reads_the_current_version_of_the_association_found_by_name(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """今の版は、名前で見つけた関連付けのものを使う。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "Online"})
    listed_associations(aws_stubs.ssm, ASSOCIATION, version=OLD_VERSION)
    sshd_ran(aws_stubs, INSTANCE, association_ran("Success", OLD_VERSION))
    apps(aws_stubs)
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines()[-3] == "sshd-ports: applied"


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
    apps(aws_stubs)
    statuses(aws_stubs)
    assert cli.main(["temt", "--profile", "work", "status", "--svc", "dev-main"]) == 0
    aws_stubs.done()
    # --profile はその実行だけに効き、読んだ設定にも設定ファイルにも混ざらない。設定
    # ファイルに書くのは apply だけである
    assert conf.OPTION_PROFILE == "work"
    assert aws_stubs.passed[0].AwsProfile is None
    assert configured.conf_path.read_text(encoding="utf-8") == before
    assert capsys.readouterr().out == ""


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
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, INSTANCE, association_ran("Success"))
    apps(aws_stubs)
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
    assert capsys.readouterr().err.splitlines()[5:] == [
        NO_APPS,
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
    apps(aws_stubs)
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
    assert capsys.readouterr().err.splitlines()[2:] == [
        NO_APPS,
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
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, INSTANCE, association_ran("Success"))
    apps(aws_stubs)
    statuses(
        aws_stubs,
        a_status("idle-shutdown", "in use: 1 ssh session(s) (limit 1h)", timedelta(minutes=10)),
    )
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines()[-2:] == [
        "app status:",
        "  idle-shutdown  in use: 1 ssh session(s) (limit 1h)  [10m ago, stale]",
    ]


def a_record(app_id: str, **fields: str) -> dict[str, Any]:
    """deploy の記録のパラメータ 1 つ。fields に instance、baked_version、baked_image を渡す。"""
    value = {"name": app_id, "version": "1", **fields}
    return {"Name": f"{PATH}/{app_id}/deploy", "Type": "String", "Value": json.dumps(value)}


def test_it_shows_the_apps_that_are_not_baked(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """焼いていないアプリを、unbaked、lost の順に、アプリ ID の順で挙げる。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopped")), SEARCH
    )
    agents(aws_stubs, [INSTANCE], {INSTANCE: "ConnectionLost"})
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, INSTANCE, association_ran("Success"))
    apps(
        aws_stubs,
        a_record("emacs", baked_version="1", baked_image=IMAGE),
        a_record("mise", instance=INSTANCE),
        a_record("cloudflared", instance=INSTANCE),
        a_record("timezone-tokyo", instance=OTHER),
        a_record("moved", baked_version="1", baked_image=OTHER_IMAGE),
    )
    statuses(aws_stubs)
    status.run(configured)
    aws_stubs.done()
    assert capsys.readouterr().err.splitlines()[-2] == (
        "apps:       unbaked cloudflared, mise; lost moved, timezone-tokyo"
    )


ALL_IN = app.Place(image_id=IMAGE, alive=frozenset({INSTANCE}))


@pytest.mark.parametrize(
    ("records", "shown"),
    [
        pytest.param(
            [("a", {"baked_version": "1", "baked_image": IMAGE})], "all baked", id="all baked"
        ),
        pytest.param(
            [("a", {"baked_version": "1", "baked_image": IMAGE}), ("b", {"instance": INSTANCE})],
            "unbaked b",
            id="unbaked",
        ),
        pytest.param([("a", {"instance": OTHER})], "lost a", id="lost"),
        pytest.param(
            [
                ("a", {"removed": INSTANCE, "baked_version": "1", "baked_image": IMAGE}),
                ("b", {"instance": OTHER}),
                ("c", {"instance": INSTANCE}),
            ],
            "unbaked c; lost b; removed a (still in the AMI; bake to drop it)",
            id="removed",
        ),
        pytest.param([], "none", id="no record"),
        # 焼いたかどうか分からない記録は数えない
        pytest.param([("a", {"version": "1"})], "none", id="unknown"),
        pytest.param(
            [("a", {"version": "1"}), ("b", {"baked_version": "1", "baked_image": IMAGE})],
            "all baked",
            id="unknown and baked",
        ),
    ],
)
def test_apps_line(records: list[tuple[str, dict[str, str]]], shown: str) -> None:
    assert status.apps_line(records, ALL_IN) == shown


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
