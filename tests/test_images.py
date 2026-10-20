"""ami list、ami prune、ami delete の本体のテスト。AWS は Stubber で、標準入力は偽物に差し替える。

サービスを与えないときの環境は aws.env_session が決めるので、それを用意したクライアントを
返すものに差し替える。サービスを与えるときは aws.session を差し替える。
"""

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from botocore.stub import Stubber
from conftest import REGION, offline, taken, watched
from types_boto3_ec2.client import EC2Client

from temt import aws, conf, home, images, lock
from temt.conf import Conf
from temt.msg import TemtError, UsageError
from temt.names import SSH_DIR_ENV, Service, defaults_path
from temt.resources import TemplateVersion

ACCOUNT = "123456789012"
OLD = "ami-0000000000000000a"
NEW = "ami-0000000000000000b"
OTHER = "ami-0000000000000000c"
LOOSE = "ami-0000000000000000d"

ALL_TEMT = {"Owners": ["self"], "Filters": [{"Name": "tag-key", "Values": ["temt:service"]}]}
DEV_MAIN = {
    "Owners": ["self"],
    "Filters": [{"Name": "tag:temt:service", "Values": ["dev-main"]}],
}
# プロファイルを選んでいないときの、環境を知らせる行
ENVIRONMENT = f"account {ACCOUNT}, region {REGION}, profile (none)"
# ami list の表の見出し
HEADER = "ID                      NAME                       STATE       CREATED               USED"


def an_image(
    image_id: str, name: str, created: str, *, service: str = "dev-main", state: str = "available"
) -> dict[str, Any]:
    """describe-images が返す AMI 1 つ分。"""
    item: dict[str, Any] = {
        "ImageId": image_id,
        "State": state,
        "CreationDate": created,
        "Tags": [{"Key": "temt:service", "Value": service}],
    }
    if name:
        item["Name"] = name
    return item


class Session:
    """aws.env_session と aws.session の代わり。用意したクライアントを返す。"""

    region_name = REGION
    profile_name = "default"

    def __init__(self, ec2: EC2Client, sts: Any) -> None:
        self.clients: dict[str, Any] = {"ec2": ec2, "sts": sts}

    def client(self, name: str) -> Any:
        return self.clients[name]


@dataclass(frozen=True)
class Stubs:
    ec2: Stubber
    sts: Stubber
    # aws.env_session に渡されたリージョン
    regions: list[str | None]

    def done(self) -> None:
        self.ec2.assert_no_pending_responses()
        self.sts.assert_no_pending_responses()


@pytest.fixture
def stubs(
    monkeypatch: pytest.MonkeyPatch, ec2: EC2Client, stub_failures: list[BotoCoreError]
) -> Iterator[Stubs]:
    """aws.env_session が返すクライアントを Stubber で包む。

    teardown で、積んだ応答がすべて使われたことを確かめる。
    """
    sts = watched(offline().client("sts"), stub_failures)
    session = Session(ec2, sts)
    regions: list[str | None] = []

    def env_session(region: str | None) -> Session:
        regions.append(region)
        return session

    def service_session(values: Conf) -> Session:
        _ = values
        return session

    monkeypatch.setattr(aws, "env_session", env_session)
    monkeypatch.setattr(aws, "session", service_session)
    with Stubber(ec2) as ec2_stub, Stubber(sts) as sts_stub:
        made = Stubs(ec2_stub, sts_stub, regions)
        yield made
        made.done()


def identified(stubs: Stubs) -> None:
    """資格情報のアカウントを積む。"""
    stubs.sts.add_response(
        "get_caller_identity",
        {"UserId": "u", "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/u"},
        {},
    )


def listed(stubs: Stubs, params: dict[str, Any], *found: dict[str, Any]) -> None:
    """temt の AMI を積む。"""
    stubs.ec2.add_response("describe_images", {"Images": list(found)}, params)


def templates(stubs: Stubs, **found: list[tuple[int, str]]) -> None:
    """起動テンプレートと、そのバージョンが指す AMI を積む。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": name} for name in found]},
        {},
    )
    for name, versions in found.items():
        stubs.ec2.add_response(
            "describe_launch_template_versions",
            {
                "LaunchTemplateVersions": [
                    {"VersionNumber": number, "LaunchTemplateData": {"ImageId": image_id}}
                    for number, image_id in versions
                ]
            },
            {"LaunchTemplateName": name},
        )


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


def test_list_shows_every_temt_image_with_its_templates(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    identified(stubs)
    listed(
        stubs,
        ALL_TEMT,
        an_image(NEW, "temt-dev-main-2609201200", "2026-09-20T03:00:00.000Z"),
        an_image(OTHER, "", "2026-09-19T03:00:00.000Z", service="api"),
        an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"),
        an_image(
            LOOSE, "temt-dev-main-2609021200", "2026-09-02T03:00:00.000Z", state="deregistered"
        ),
    )
    templates(stubs, **{"temt-dev-main-template": [(3, NEW)], "mine": [(1, NEW), (2, OTHER)]})

    images.list_images(None, "us-east-1", None)

    stubs.done()
    assert stubs.regions == ["us-east-1"]
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [ENVIRONMENT]
    # サービス名、作成日時の順に並ぶ。登録を解除した直後の AMI は出さない
    assert captured.out.splitlines() == [
        HEADER,
        f"{OTHER}   -                          available   2026-09-19 12:00:00   mine:2",
        f"{OLD}   temt-dev-main-2609011200   available   2026-09-01 12:00:00   -",
        f"{NEW}   temt-dev-main-2609201200   available   2026-09-20 12:00:00"
        "   temt-dev-main-template:3,mine:1",
    ]


def test_list_reads_no_templates_without_used(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """USED を選ばなければ、起動テンプレートを読まない。"""
    identified(stubs)
    listed(
        stubs,
        ALL_TEMT,
        an_image(OTHER, "", "2026-09-19T03:00:00.000Z", service="api"),
        an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"),
    )

    images.list_images(None, None, "service,id")

    stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "SERVICE    ID",
        f"api        {OTHER}",
        f"dev-main   {OLD}",
    ]


def test_list_checks_the_columns_before_aws(stubs: Stubs) -> None:
    with pytest.raises(UsageError, match="unknown column in --format: size; use ID,NAME,"):
        images.list_images(None, None, "id,size")
    stubs.done()
    assert stubs.regions == []


def test_list_of_a_service_uses_its_config_file(
    stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """サービスを与えれば、環境を知らせる行は出さず、そのサービスの AMI だけを挙げる。"""
    listed(stubs, DEV_MAIN, an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"))
    templates(stubs)

    images.list_images(configured, None, None)

    stubs.done()
    assert stubs.regions == []
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        HEADER,
        f"{OLD}   temt-dev-main-2609011200   available   2026-09-01 12:00:00   -",
    ]


def test_list_without_images(stubs: Stubs, capsys: pytest.CaptureFixture[str]) -> None:
    identified(stubs)
    listed(stubs, ALL_TEMT)

    images.list_images(None, None, None)

    stubs.done()
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [ENVIRONMENT]
    assert captured.out.splitlines() == ["ID   NAME   STATE   CREATED   USED"]


def test_a_template_that_goes_away_while_reading_is_skipped(stubs: Stubs, ec2: EC2Client) -> None:
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": "gone"}, {"LaunchTemplateName": "here"}]},
        {},
    )
    stubs.ec2.add_client_error(
        "describe_launch_template_versions",
        "InvalidLaunchTemplateName.NotFoundException",
        expected_params={"LaunchTemplateName": "gone"},
    )
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [{"VersionNumber": 1, "LaunchTemplateData": {}}]},
        {"LaunchTemplateName": "here"},
    )

    assert images.references(ec2) == images.References(used={}, unknown=[])

    stubs.done()


def test_other_errors_from_reading_the_templates_are_left_to_cli(
    stubs: Stubs, ec2: EC2Client
) -> None:
    stubs.ec2.add_response(
        "describe_launch_templates", {"LaunchTemplates": [{"LaunchTemplateName": "mine"}]}, {}
    )
    stubs.ec2.add_client_error(
        "describe_launch_template_versions",
        "UnauthorizedOperation",
        expected_params={"LaunchTemplateName": "mine"},
    )

    with pytest.raises(ClientError, match="UnauthorizedOperation"):
        images.references(ec2)


def a_mix(stubs: Stubs) -> None:
    """候補になるもの、ならないものを混ぜた temt の AMI と、テンプレートを積む。"""
    listed(
        stubs,
        ALL_TEMT,
        # テンプレートが指すので残す
        an_image(NEW, "temt-dev-main-2609201200", "2026-09-20T03:00:00.000Z"),
        # どこからも指されていないので消す。failed も候補になる
        an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"),
        an_image(
            LOOSE, "temt-api-2609021200", "2026-09-02T03:00:00.000Z", service="api", state="failed"
        ),
        # 候補にならない。bake の途中、名前が bake の形でない、名前と印のサービスが違う
        an_image(
            "ami-0000000000000001a",
            "temt-web-2609211200",
            "2026-09-21",
            service="web",
            state="pending",
        ),
        an_image("ami-0000000000000001b", "golden", "2026-09-03", service="web"),
        an_image("ami-0000000000000001c", "temt-dev-2609041200", "2026-09-04", service="dev-main"),
    )
    templates(stubs, **{"temt-dev-main-template": [(3, NEW)]})


def rechecked(stubs: Stubs) -> None:
    """消す直前に、テンプレートを読み直す。a_mix と同じく NEW だけが指されている。"""
    templates(stubs, **{"temt-dev-main-template": [(3, NEW)]})


def deregistered(stubs: Stubs, image_id: str, *codes: str) -> None:
    """登録の解除を積む。codes はスナップショットごとの結果。"""
    stubs.ec2.add_response(
        "deregister_image",
        {
            "Return": True,
            "DeleteSnapshotResults": [
                {"SnapshotId": f"snap-{index}", "ReturnCode": code}
                for index, code in enumerate(codes)
            ],
        },
        {"ImageId": image_id, "DeleteAssociatedSnapshots": True},
    )


def test_prune_deregisters_the_images_no_template_uses(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)
    identified(stubs)
    a_mix(stubs)
    rechecked(stubs)
    deregistered(stubs, LOOSE, "success")
    rechecked(stubs)
    deregistered(stubs, OLD, "success", "client-error")

    images.prune(None, None, yes=False)

    stubs.done()
    assert terminal.reads == 1
    captured = capsys.readouterr()
    assert captured.out.startswith(
        "deregister:\n"
        f"  {LOOSE}  temt-api-2609021200       failed     2026-09-02 12:00:00\n"
        f"  {OLD}  temt-dev-main-2609011200  available  2026-09-01 12:00:00\n"
    )
    # 指されている AMI は挙げない
    assert NEW not in captured.out
    assert f"deregistered {OLD} (temt-dev-main-2609011200)" in captured.out
    assert captured.err == (
        f"{ENVIRONMENT}\n"
        "deregister 2 AMIs and their snapshots? [y/N] "
        f"temt: warning: snapshot snap-1 of {OLD}: client-error\n"
    )


def test_prune_with_nothing_to_deregister_does_not_ask(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)
    identified(stubs)
    listed(stubs, ALL_TEMT, an_image(NEW, "temt-dev-main-2609201200", "2026-09-20T03:00:00.000Z"))
    templates(stubs, **{"temt-dev-main-template": [(3, NEW)]})

    images.prune(None, "us-east-1", yes=False)

    stubs.done()
    assert terminal.reads == 0
    assert capsys.readouterr().out.splitlines()[-1] == "no AMIs to deregister"


def test_prune_does_nothing_when_the_answer_is_no(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    identified(stubs)
    a_mix(stubs)

    with pytest.raises(TemtError, match="cancelled"):
        images.prune(None, None, yes=False)

    # deregister-image を積んでいないので、呼べば Stubber が失敗する
    stubs.done()


def test_prune_goes_on_after_a_failure(stubs: Stubs, capsys: pytest.CaptureFixture[str]) -> None:
    identified(stubs)
    a_mix(stubs)
    rechecked(stubs)
    stubs.ec2.add_client_error(
        "deregister_image",
        "InvalidAMIID.Unavailable",
        expected_params={"ImageId": LOOSE, "DeleteAssociatedSnapshots": True},
    )
    rechecked(stubs)
    deregistered(stubs, OLD, "success")

    with pytest.raises(TemtError) as caught:
        images.prune(None, None, yes=True)

    stubs.done()
    assert str(caught.value) == f"kept 1 of 2 AMIs: {LOOSE}"
    captured = capsys.readouterr()
    assert f"temt: warning: keeping {LOOSE}: cannot deregister it: " in captured.err
    assert f"deregistered {OLD} (temt-dev-main-2609011200)" in captured.out


def test_prune_stops_when_aws_does_not_answer(
    stubs: Stubs,
    ec2: EC2Client,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """接続が切れたら、残りの AMI の登録を解除せずに、そこで止まる。"""
    identified(stubs)
    a_mix(stubs)
    rechecked(stubs)
    asked: list[str] = []

    def unreachable(**kwargs: Any) -> Any:
        asked.append(kwargs["ImageId"])
        raise EndpointConnectionError(endpoint_url="https://ec2")

    monkeypatch.setattr(ec2, "deregister_image", unreachable)

    with pytest.raises(EndpointConnectionError):
        images.prune(None, None, yes=True)

    assert asked == [LOOSE]
    assert "deregistered" not in capsys.readouterr().err


def test_prune_counts_one_image(stubs: Stubs) -> None:
    identified(stubs)
    listed(stubs, ALL_TEMT, an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"))
    templates(stubs)
    templates(stubs)
    stubs.ec2.add_client_error(
        "deregister_image",
        "InvalidAMIID.Unavailable",
        expected_params={"ImageId": OLD, "DeleteAssociatedSnapshots": True},
    )

    with pytest.raises(TemtError, match=f"^kept 1 of 1 AMI: {OLD}$"):
        images.prune(None, None, yes=True)


@pytest.mark.parametrize(
    "setting",
    [
        ("option", "work"),
        ("defaults", "work"),
        ("AWS_PROFILE", "work"),
        ("AWS_DEFAULT_PROFILE", "work"),
    ],
)
def test_the_environment_shows_a_chosen_profile(
    stubs: Stubs,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: tuple[str, str],
) -> None:
    """選んだプロファイルは、Session が答える名前で出す。"""
    name, value = setting
    monkeypatch.setattr(Session, "profile_name", value)
    if name == "option":
        monkeypatch.setattr(conf, "OPTION_PROFILE", value)
    elif name == "defaults":
        defaults_path().parent.mkdir(parents=True, exist_ok=True)
        defaults_path().write_text(f'AwsProfile = "{value}"\n', encoding="utf-8")
    else:
        monkeypatch.setenv(name, value)
    identified(stubs)
    listed(stubs, ALL_TEMT)

    images.list_images(None, None, None)

    assert capsys.readouterr().err.splitlines()[0] == (
        f"account {ACCOUNT}, region {REGION}, profile work"
    )


# 解決を ParameterNotFound で断られたときの理由。AWS の文をそのまま添える
NOT_RESOLVED = (
    "An error occurred (ParameterNotFound) when calling the DescribeLaunchTemplateVersions "
    "operation: no parameter /golden"
)


def alias_version(
    stubs: Stubs, name: str, resolved: dict[str, Any] | None, *, code: str = "ParameterNotFound"
) -> None:
    """SSM パラメータで AMI を指すバージョン 1 を積み、それを解決した結果を積む。

    resolved が None なら、解決を code で断られる。
    """
    stubs.ec2.add_response(
        "describe_launch_templates", {"LaunchTemplates": [{"LaunchTemplateName": name}]}, {}
    )
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {"VersionNumber": 1, "LaunchTemplateData": {"ImageId": "resolve:ssm:/golden"}}
            ]
        },
        {"LaunchTemplateName": name},
    )
    params = {"LaunchTemplateName": name, "Versions": ["1"], "ResolveAlias": True}
    if resolved is None:
        stubs.ec2.add_client_error(
            "describe_launch_template_versions",
            code,
            service_message="no parameter /golden",
            expected_params=params,
        )
        return
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [{"VersionNumber": 1, "LaunchTemplateData": resolved}]},
        params,
    )


def test_a_version_that_points_through_ssm_is_resolved(stubs: Stubs, ec2: EC2Client) -> None:
    alias_version(stubs, "mine", {"ImageId": OLD})

    assert images.references(ec2) == images.References(
        used={OLD: [TemplateVersion("mine", 1)]}, unknown=[]
    )
    stubs.done()


@pytest.mark.parametrize(
    ("resolved", "reason"),
    [
        (None, NOT_RESOLVED),
        ({"ImageId": "resolve:ssm:/golden"}, "resolved to resolve:ssm:/golden"),
        ({}, "resolved to nothing"),
    ],
)
def test_a_version_that_cannot_be_resolved_is_unknown(
    stubs: Stubs, ec2: EC2Client, resolved: dict[str, Any] | None, reason: str
) -> None:
    alias_version(stubs, "mine", resolved)

    assert images.references(ec2) == images.References(
        used={}, unknown=[f"mine version 1 (resolve:ssm:/golden: {reason})"]
    )
    stubs.done()


def test_a_template_that_goes_away_while_resolving_is_skipped(stubs: Stubs, ec2: EC2Client) -> None:
    alias_version(stubs, "mine", None, code="InvalidLaunchTemplateName.NotFoundException")

    assert images.references(ec2) == images.References(used={}, unknown=[])
    stubs.done()


def test_list_warns_about_a_version_it_cannot_resolve(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    identified(stubs)
    listed(stubs, ALL_TEMT, an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"))
    alias_version(stubs, "mine", None)

    images.list_images(None, None, None)

    stubs.done()
    assert (
        "temt: warning: cannot resolve the AMI of mine version 1 "
        f"(resolve:ssm:/golden: {NOT_RESOLVED})" in capsys.readouterr().err
    )


def test_prune_deregisters_nothing_when_a_version_cannot_be_resolved(stubs: Stubs) -> None:
    identified(stubs)
    listed(stubs, ALL_TEMT, an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"))
    alias_version(stubs, "mine", None)

    with pytest.raises(
        TemtError,
        match=r"cannot resolve the AMI of: mine version 1 "
        rf"\(resolve:ssm:/golden: {re.escape(NOT_RESOLVED)}\); nothing is deregistered",
    ):
        images.prune(None, None, yes=True)

    stubs.done()


def test_references_go_through_every_page(stubs: Stubs, ec2: EC2Client) -> None:
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": "one"}], "NextToken": "t1"},
        {},
    )
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {"VersionNumber": 1, "LaunchTemplateData": {"ImageId": OLD}}
            ],
            "NextToken": "v1",
        },
        {"LaunchTemplateName": "one"},
    )
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [{"VersionNumber": 2, "LaunchTemplateData": {"ImageId": NEW}}]},
        {"LaunchTemplateName": "one", "NextToken": "v1"},
    )
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": "two"}]},
        {"NextToken": "t1"},
    )
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [{"VersionNumber": 5, "LaunchTemplateData": {"ImageId": OLD}}]},
        {"LaunchTemplateName": "two"},
    )

    assert images.references(ec2).used == {
        OLD: [TemplateVersion("one", 1), TemplateVersion("two", 5)],
        NEW: [TemplateVersion("one", 2)],
    }
    stubs.done()


@pytest.mark.parametrize(
    ("service", "name", "expected"),
    [
        ("dev-main", "temt-dev-main-2609011200", True),
        # 正規表現の . は、どの文字にも一致させない
        ("a.b", "temt-aXb-2609011200", False),
        ("a.b", "temt-a.b-2609011200", True),
        ("dev-main", "temt-dev-main-260901120", False),
        ("dev-main", "temt-dev-main-2609011200-copy", False),
    ],
)
def test_prunable_names(service: str, name: str, expected: bool) -> None:
    image = images.Image(OLD, name, "available", "2026-09-01", service)
    assert images._prunable(image) is expected


@pytest.mark.parametrize("command", ["list", "prune", "delete"])
def test_a_service_without_its_config_file(stubs: Stubs, service: Service, command: str) -> None:
    """サービスを与えたのに設定ファイルが無ければ、AWS を呼ぶ前に apply を促して止まる。"""
    with pytest.raises(TemtError, match="run 'temt apply --svc dev-main'"):
        if command == "list":
            images.list_images(service, None, None)
        elif command == "prune":
            images.prune(service, None, yes=True)
        else:
            images.delete(OLD, service, None, force=True, yes=True)
    stubs.done()


def test_prune_needs_yes_without_a_terminal(stubs: Stubs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", None)
    identified(stubs)
    a_mix(stubs)

    with pytest.raises(TemtError, match="stdin is not a terminal; pass --yes"):
        images.prune(None, None, yes=False)

    stubs.done()


def test_prune_of_a_service_deregisters_only_its_images(
    stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """サービスの AMI だけを候補にし、指されているかは、他のサービスのテンプレートも見る。"""
    listed(
        stubs,
        DEV_MAIN,
        # 他のサービスが ImageId で指しているので残す
        an_image(NEW, "temt-dev-main-2609201200", "2026-09-20T03:00:00.000Z"),
        an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"),
    )
    templates(stubs, **{"temt-web-template": [(1, NEW)]})
    templates(stubs, **{"temt-web-template": [(1, NEW)]})
    deregistered(stubs, OLD, "success")

    images.prune(configured, None, yes=True)

    stubs.done()
    assert stubs.regions == []
    captured = capsys.readouterr()
    # --svc を付けたので、環境の行は出さない
    assert captured.err == ""
    assert NEW not in captured.out
    assert f"deregistered {OLD} (temt-dev-main-2609011200)" in captured.out


def test_prune_of_a_service_names_the_region_and_the_account_when_it_asks(
    stubs: Stubs,
    configured: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--svc を付ければ環境の行を出さないので、確認にリージョンとアカウントを添える。"""
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    listed(stubs, DEV_MAIN, an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z"))
    templates(stubs)
    identified(stubs)

    with pytest.raises(TemtError, match="cancelled"):
        images.prune(configured, None, yes=False)

    stubs.done()
    assert capsys.readouterr().err == (
        f"deregister 1 AMI and their snapshots in {REGION} (account {ACCOUNT})? [y/N] "
    )


DEV_MAIN_OLD = an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z")
DEV_MAIN_LOOSE = an_image(LOOSE, "temt-dev-main-2609021200", "2026-09-02T03:00:00.000Z")


def test_prune_keeps_an_image_whose_service_is_locked(
    stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """そのサービスのロックを他のコマンドが持っていれば、消さずに残し、最後に挙げる。"""
    identified(stubs)
    listed(stubs, ALL_TEMT, DEV_MAIN_OLD)
    templates(stubs)

    with lock.hold(service), pytest.raises(TemtError, match=f"^kept 1 of 1 AMI: {OLD}$"):
        images.prune(None, None, yes=True)

    # テンプレートの読み直しも登録の解除も積んでいないので、呼べば Stubber が失敗する
    stubs.done()
    assert (
        f"temt: warning: keeping {OLD}: another temt command is running for temt-dev-main\n"
    ) in capsys.readouterr().err


def test_prune_keeps_an_image_that_a_template_uses_when_read_again(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """確認の後で読み直したテンプレートが指していれば、消さずに残す。

    確認を待つあいだに、並行する bake がテンプレートを向けたときに当たる。
    """
    identified(stubs)
    listed(stubs, ALL_TEMT, DEV_MAIN_OLD)
    templates(stubs)
    templates(stubs, **{"temt-dev-main-template": [(2, OLD)]})

    with pytest.raises(TemtError, match=f"^kept 1 of 1 AMI: {OLD}$"):
        images.prune(None, None, yes=True)

    stubs.done()
    assert (
        f"temt: warning: keeping {OLD}: used by temt-dev-main-template version 2\n"
    ) in capsys.readouterr().err


def test_prune_does_not_make_the_known_hosts_directory(stubs: Stubs, service: Service) -> None:
    """known_hosts のディレクトリが無ければ、ロックを取らずに消し、ディレクトリも作らない。"""
    identified(stubs)
    listed(stubs, ALL_TEMT, DEV_MAIN_OLD)
    templates(stubs)
    templates(stubs)
    deregistered(stubs, OLD, "success")

    images.prune(None, None, yes=True)

    stubs.done()
    assert not service.known_hosts_dir.exists()


def test_prune_holds_the_lock_for_each_image(
    stubs: Stubs, ec2: EC2Client, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登録を解除する間はロックを持ち、1 つ消すごとに離す。

    同じサービスの 2 つめを消せるのは、1 つめの後で離したからである。flock は開いた
    ファイルごとに効くので、離さなければ同じプロセスの 2 つめも取れない。
    """
    _ = home.prepare_known_hosts(service)
    identified(stubs)
    listed(stubs, ALL_TEMT, DEV_MAIN_OLD, DEV_MAIN_LOOSE)
    templates(stubs)
    held: list[bool] = []

    def deregister(**kwargs: Any) -> Any:
        held.append(not taken(service))
        return {"Return": True}

    monkeypatch.setattr(ec2, "deregister_image", deregister)
    templates(stubs)
    templates(stubs)

    images.prune(None, None, yes=True)

    stubs.done()
    assert held == [True, True]
    assert taken(service)


def test_prune_deregisters_an_image_of_an_odd_service_without_a_lock(stubs: Stubs) -> None:
    """temt:service がサービス名の規則に合わなければ、ロックを取らずに消す。

    その名前のサービスは temt のコマンドが扱えないので、ロックも無い。
    """
    identified(stubs)
    listed(
        stubs,
        ALL_TEMT,
        an_image(OLD, "temt-Dev_Main-2609011200", "2026-09-01T03:00:00.000Z", service="Dev_Main"),
    )
    templates(stubs)
    templates(stubs)
    deregistered(stubs, OLD, "success")

    images.prune(None, None, yes=True)

    stubs.done()


def test_prune_keeps_an_image_when_a_version_read_again_cannot_be_resolved(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """読み直したテンプレートに、どの AMI を指すか分からないバージョンがあれば残す。"""
    identified(stubs)
    listed(stubs, ALL_TEMT, DEV_MAIN_OLD)
    templates(stubs)
    alias_version(stubs, "mine", None)

    with pytest.raises(TemtError, match=f"^kept 1 of 1 AMI: {OLD}$"):
        images.prune(None, None, yes=True)

    stubs.done()
    assert (
        f"temt: warning: keeping {OLD}: cannot resolve the AMI of: mine version 1 "
        f"(resolve:ssm:/golden: {NOT_RESOLVED})\n"
    ) in capsys.readouterr().err


def test_prune_checks_the_ssh_directory_before_asking(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEMT_SSH_DIR の誤りは、AWS を読む前、確認より前に出す。

    ロックの場所は ssh ディレクトリの下にあるので、確認の後で AMI を消す途中に
    止まらないようにする。
    """
    monkeypatch.setenv(SSH_DIR_ENV, "relative/ssh")
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)

    with pytest.raises(TemtError, match=f"^{SSH_DIR_ENV} must be an absolute path: relative/ssh$"):
        images.prune(None, None, yes=False)

    # 何も積んでいないので、AWS を呼べば Stubber が失敗する
    stubs.done()
    assert terminal.reads == 0


def test_prune_lets_the_lock_go_when_aws_does_not_answer(
    stubs: Stubs, ec2: EC2Client, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登録の解除の途中で止まっても、取ったロックは離す。後で同じロックが取れる。"""
    _ = home.prepare_known_hosts(service)
    identified(stubs)
    listed(stubs, ALL_TEMT, DEV_MAIN_OLD)
    templates(stubs)
    templates(stubs)
    held: list[bool] = []

    def unreachable(**kwargs: Any) -> Any:
        held.append(not taken(service))
        raise EndpointConnectionError(endpoint_url="https://ec2")

    monkeypatch.setattr(ec2, "deregister_image", unreachable)

    with pytest.raises(EndpointConnectionError):
        images.prune(None, None, yes=True)

    assert held == [True]
    assert taken(service)


def by_name(stubs: Stubs, *found: dict[str, Any]) -> None:
    """名前で探した AMI を積む。"""
    listed(
        stubs,
        {"Owners": ["self"], "Filters": [{"Name": "name", "Values": ["temt-dev-main-2609011200"]}]},
        *found,
    )


def by_id(stubs: Stubs, image_id: str, *found: dict[str, Any]) -> None:
    """ID で探した AMI を積む。ami delete は自分のアカウントの AMI からだけ探す。"""
    listed(stubs, {"ImageIds": [image_id], "Owners": ["self"]}, *found)


OLD_IMAGE = an_image(OLD, "temt-dev-main-2609011200", "2026-09-01T03:00:00.000Z")


def test_delete_deregisters_an_unused_image_by_name(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)
    identified(stubs)
    by_name(stubs, OLD_IMAGE)
    templates(stubs, **{"temt-dev-main-template": [(3, NEW)]})
    deregistered(stubs, OLD, "success", "client-error")

    images.delete("temt-dev-main-2609011200", None, None, force=False, yes=False)

    stubs.done()
    assert terminal.reads == 1
    captured = capsys.readouterr()
    assert captured.out.startswith(
        f"deregister:\n  {OLD}  temt-dev-main-2609011200  available  2026-09-01 12:00:00\n"
    )
    assert f"deregistered {OLD} (temt-dev-main-2609011200)" in captured.out
    assert captured.err == (
        f"{ENVIRONMENT}\n"
        f"deregister {OLD} and its snapshots? [y/N] "
        f"temt: warning: snapshot snap-1 of {OLD}: client-error\n"
    )


def test_delete_refuses_a_used_image_without_force(stubs: Stubs) -> None:
    identified(stubs)
    by_id(stubs, OLD, OLD_IMAGE)
    templates(stubs, **{"temt-dev-main-template": [(2, OLD), (3, NEW)], "mine": [(1, OLD)]})

    with pytest.raises(TemtError) as caught:
        images.delete(OLD, None, None, force=False, yes=True)

    # deregister-image を積んでいないので、呼べば Stubber が失敗する
    stubs.done()
    assert str(caught.value) == (
        f"{OLD} (temt-dev-main-2609011200): used by temt-dev-main-template version 2, "
        "mine version 1; pass --force to deregister it anyway"
    )


def test_delete_deregisters_a_used_image_with_force(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    identified(stubs)
    by_id(stubs, OLD, OLD_IMAGE)
    templates(stubs, **{"temt-dev-main-template": [(2, OLD)]})
    deregistered(stubs, OLD, "success")

    images.delete(OLD, None, None, force=True, yes=True)

    stubs.done()
    captured = capsys.readouterr()
    assert f"temt: warning: {OLD}: used by temt-dev-main-template version 2" in captured.err
    assert f"deregistered {OLD} (temt-dev-main-2609011200)" in captured.out


def test_delete_warns_every_reason_with_force(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """pending と、解決できないバージョンも、force なら警告して消す。"""
    identified(stubs)
    by_id(stubs, OLD, an_image(OLD, "temt-dev-main-2609011200", "2026-09-01", state="pending"))
    alias_version(stubs, "mine", None)
    deregistered(stubs, OLD, "success")

    images.delete(OLD, None, "us-east-1", force=True, yes=True)

    stubs.done()
    assert stubs.regions == ["us-east-1"]
    captured = capsys.readouterr()
    assert (
        f"temt: warning: {OLD}: cannot resolve the AMI of: mine version 1 "
        f"(resolve:ssm:/golden: {NOT_RESOLVED})\n"
        f"temt: warning: {OLD}: pending; bake may be creating it\n"
    ) in captured.err
    assert f"deregistered {OLD} (temt-dev-main-2609011200)" in captured.out


def test_delete_names_every_reason_without_force(stubs: Stubs) -> None:
    identified(stubs)
    by_id(stubs, OLD, an_image(OLD, "temt-dev-main-2609011200", "2026-09-01", state="pending"))
    templates(stubs, **{"temt-dev-main-template": [(2, OLD)]})

    with pytest.raises(TemtError) as caught:
        images.delete(OLD, None, None, force=False, yes=True)

    stubs.done()
    assert str(caught.value) == (
        f"{OLD} (temt-dev-main-2609011200): used by temt-dev-main-template version 2; "
        "pending; bake may be creating it; pass --force to deregister it anyway"
    )


def test_delete_raises_when_aws_refuses(stubs: Stubs, capsys: pytest.CaptureFixture[str]) -> None:
    """prune と違って捕まえず、cli.run に包ませる。"""
    identified(stubs)
    by_id(stubs, OLD, OLD_IMAGE)
    templates(stubs)
    stubs.ec2.add_client_error(
        "deregister_image",
        "InvalidAMIID.Unavailable",
        expected_params={"ImageId": OLD, "DeleteAssociatedSnapshots": True},
    )

    with pytest.raises(ClientError, match="InvalidAMIID.Unavailable"):
        images.delete(OLD, None, None, force=False, yes=True)

    stubs.done()
    assert "deregistered" not in capsys.readouterr().err


def test_delete_refuses_a_pending_image_without_force(stubs: Stubs) -> None:
    identified(stubs)
    by_id(stubs, OLD, an_image(OLD, "temt-dev-main-2609011200", "2026-09-01", state="pending"))
    templates(stubs)

    with pytest.raises(TemtError, match="pending; bake may be creating it; pass --force"):
        images.delete(OLD, None, None, force=False, yes=True)

    stubs.done()


def test_delete_refuses_when_a_version_cannot_be_resolved(stubs: Stubs) -> None:
    identified(stubs)
    by_id(stubs, OLD, OLD_IMAGE)
    alias_version(stubs, "mine", None)

    with pytest.raises(
        TemtError,
        match=r"cannot resolve the AMI of: mine version 1 "
        rf"\(resolve:ssm:/golden: {re.escape(NOT_RESOLVED)}\); pass --force",
    ):
        images.delete(OLD, None, None, force=False, yes=True)

    stubs.done()


def test_delete_refuses_an_image_without_the_tag(stubs: Stubs) -> None:
    identified(stubs)
    by_name(stubs, {"ImageId": OLD, "Name": "temt-dev-main-2609011200", "State": "available"})

    with pytest.raises(TemtError, match="has no temt:service tag"):
        images.delete("temt-dev-main-2609011200", None, None, force=True, yes=True)

    stubs.done()


def test_delete_of_a_missing_image(stubs: Stubs) -> None:
    identified(stubs)
    by_name(stubs)

    with pytest.raises(TemtError, match="no such AMI in this account: temt-dev-main-2609011200"):
        images.delete("temt-dev-main-2609011200", None, None, force=True, yes=True)

    stubs.done()


def test_delete_does_nothing_when_the_answer_is_no(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    identified(stubs)
    by_id(stubs, OLD, OLD_IMAGE)
    templates(stubs)

    with pytest.raises(TemtError, match="cancelled"):
        images.delete(OLD, None, None, force=False, yes=False)

    stubs.done()


def test_delete_of_a_service_deregisters_its_image(
    stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    by_id(stubs, OLD, OLD_IMAGE)
    templates(stubs)
    deregistered(stubs, OLD, "success")

    images.delete(OLD, configured, None, force=False, yes=True)

    stubs.done()
    assert stubs.regions == []
    assert f"deregistered {OLD} (temt-dev-main-2609011200)" in capsys.readouterr().out


def test_delete_of_a_service_names_the_region_and_the_account_when_it_asks(
    stubs: Stubs,
    configured: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    by_id(stubs, OLD, OLD_IMAGE)
    templates(stubs)
    identified(stubs)

    with pytest.raises(TemtError, match="cancelled"):
        images.delete(OLD, configured, None, force=False, yes=False)

    stubs.done()
    assert capsys.readouterr().err == (
        f"deregister {OLD} and its snapshots in {REGION} (account {ACCOUNT})? [y/N] "
    )


@pytest.mark.parametrize(
    "found",
    [
        an_image(OLD, "temt-api-2609011200", "2026-09-01", service="api"),
        {"ImageId": OLD, "Name": "temt-dev-main-2609011200", "State": "available"},
    ],
    ids=["other", "untagged"],
)
def test_delete_of_a_service_does_not_find_an_image_of_another(
    stubs: Stubs, configured: Service, found: dict[str, Any]
) -> None:
    """--svc を付ければ、そのサービスの AMI でないものは、force でも見つからないとする。"""
    by_id(stubs, OLD, found)

    with pytest.raises(TemtError, match=f"no such AMI of the service dev-main: {OLD}"):
        images.delete(OLD, configured, None, force=True, yes=True)

    stubs.done()
