"""destroy の本体のテスト。AWS は Stubber で、標準入力と時計は偽物に差し替える。

端から端まで（destroy.run か cli.main）を通し、消す順序と締め切りを確かめる。
"""

import fcntl
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import (
    ASSOCIATION,
    SSHD,
    Clock,
    Stubs,
    an_instance,
    deadline,
    listed_associations,
    reservations,
    taken,
)
from types_boto3_ec2.client import EC2Client
from types_boto3_iam.client import IAMClient
from types_boto3_ssm.client import SSMClient

from temt import cli, destroy, home, lock, resources, wait
from temt.msg import TemtError, UsageError
from temt.names import Service

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
# /<名前> の下にあるアプリの値のひとつ。temt のタグは持たない
PARAMETER = f"/{NAME}/app/token"
INSTANCE = "i-0123456789abcdef0"
# 同じ名前を持つ、もう 1 台のインスタンス
OTHER = "i-0fedcba9876543210"
ADDRESS = "203.0.113.10"
GROUP = "sg-0123456789abcdef0"
OTHER_GROUP = "sg-0fedcba9876543210"
VPC = "vpc-0123456789abcdef0"
OTHER_VPC = "vpc-0fedcba9876543210"
IMAGE = "ami-0123456789abcdef0"
IMAGE_NAME = "temt-dev-main-2609200900"
WHEN = datetime(2026, 9, 20, tzinfo=UTC)

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
THE_TEMPLATE = {"LaunchTemplateName": TEMPLATE}
UNDER_PATH = {"Path": f"/{NAME}", "Recursive": True}
THE_PARAMETERS = {"Names": [PARAMETER]}
THE_GROUP = {"Filters": [{"Name": "group-name", "Values": [NAME]}]}
THE_GROUP_ID = {"GroupId": GROUP}
THE_PROFILE = {"InstanceProfileName": NAME}
THE_ROLE = {"RoleName": NAME}
SERVICE_IMAGES = {
    "Owners": ["self"],
    "Filters": [
        {"Name": "tag:temt:service", "Values": ["dev-main"]},
        {"Name": "state", "Values": ["available"]},
    ],
}

PROMPT = f"destroy {NAME}? [y/N] "

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def listed(found: str = "none", *, role: str = f"role {NAME}") -> str:
    """手順 2 の一覧。"""
    lines = [
        f"instances: {found}",
        f"launch template {TEMPLATE}",
        f"association {SSHD}",
        f"parameters under /{NAME}/",
        f"security group {NAME}",
        f"instance profile {NAME}",
        role,
        f"~/.ssh/temt/known_hosts.d/{NAME}/",
        f"~/.local/bin/{NAME}",
    ]
    return f"destroy {NAME}:\n" + "".join(f"  {line}\n" for line in lines)


LISTING = listed()


def told(err: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in err.splitlines()]


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


@pytest.fixture
def command(configured: Service) -> Path:
    """apply が書いたサービスのコマンド。"""
    home.write_command(configured)
    return configured.command_path


@pytest.fixture
def calls(ec2: EC2Client, ssm: SSMClient, iam: IAMClient) -> list[str]:
    """3 つのクライアントが呼んだ API を、呼ばれた順に集める。

    Stubber が見るのはクライアントごとの順序だけなので、クライアントをまたぐ順序は
    これで確かめる。
    """
    made: list[str] = []

    def note(model: Any, **kwargs: Any) -> None:
        _ = kwargs
        made.append(model.name)

    for client in (ec2, ssm, iam):
        client.meta.events.register("provide-client-params", note)
    return made


def missing(stub: Any, method: str, code: str, **params: Any) -> None:
    """そのリソースが無いときの誤りを積む。"""
    stub.add_client_error(
        method, service_error_code=code, http_status_code=404, expected_params=params or None
    )


def a_profile(roles: list[str]) -> dict[str, Any]:
    """get-instance-profile が返す形。"""
    return {
        "InstanceProfile": {
            "Path": "/",
            "InstanceProfileName": NAME,
            "InstanceProfileId": "AIPAEXAMPLE000000",
            "Arn": f"arn:aws:iam::123456789012:instance-profile/{NAME}",
            "CreateDate": WHEN,
            "Roles": [
                {
                    "Path": "/",
                    "RoleName": name,
                    "RoleId": "AROAEXAMPLE000000",
                    "Arn": f"arn:aws:iam::123456789012:role/{name}",
                    "CreateDate": WHEN,
                }
                for name in roles
            ],
        }
    }


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


def no_instance(stubs: Stubs) -> None:
    """手順 1 と 2。インスタンスは 1 台も無く、ロールにインラインポリシーも無い。"""
    stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    inline(stubs)


def inline(stubs: Stubs, *names: str) -> None:
    """手順 2 の一覧が読む、ロールのインラインポリシー。"""
    stubs.iam.add_response("list_role_policies", {"PolicyNames": list(names)}, THE_ROLE)


def the_group(stubs: Stubs, *groups: tuple[str, str]) -> None:
    """名前で探したセキュリティグループ。VPC は問わない。"""
    stubs.ec2.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                {"GroupId": group_id, "GroupName": NAME, "VpcId": vpc} for group_id, vpc in groups
            ]
        },
        THE_GROUP,
    )


def template_deleted(stubs: Stubs) -> None:
    """起動テンプレートと、その次に消す関連付けを消す。"""
    stubs.ec2.add_response(
        "delete_launch_template", {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}}, THE_TEMPLATE
    )
    listed_associations(stubs.ssm, ASSOCIATION)
    stubs.ssm.add_response("delete_association", {}, {"AssociationId": ASSOCIATION})


def no_parameters(stubs: Stubs) -> None:
    """/<名前> の下に SSM パラメータが 1 つも無い。"""
    stubs.ssm.add_response("get_parameters_by_path", {"Parameters": []}, UNDER_PATH)


def parameter_deleted(stubs: Stubs) -> None:
    """/<名前> の下の SSM パラメータを探し、見つかった 1 つを消す。"""
    stubs.ssm.add_response(
        "get_parameters_by_path", {"Parameters": [{"Name": PARAMETER}]}, UNDER_PATH
    )
    stubs.ssm.add_response("delete_parameters", {"DeletedParameters": [PARAMETER]}, THE_PARAMETERS)


def group_deleted(stubs: Stubs) -> None:
    stubs.ec2.add_response("delete_security_group", {"Return": True}, THE_GROUP_ID)


def profile_deleted(stubs: Stubs) -> None:
    stubs.iam.add_response("get_instance_profile", a_profile([NAME]), THE_PROFILE)
    stubs.iam.add_response(
        "remove_role_from_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
    )
    stubs.iam.add_response("delete_instance_profile", {}, THE_PROFILE)


def unheld(stubs: Stubs) -> None:
    """ロールを消せなくするものは無い。管理ポリシーは付いていない。"""
    stubs.iam.add_response("list_attached_role_policies", {"AttachedPolicies": []}, THE_ROLE)
    stubs.iam.add_response("list_instance_profiles_for_role", {"InstanceProfiles": []}, THE_ROLE)


def role_deleted(stubs: Stubs, *names: str) -> None:
    """ロールを消す。names のインラインポリシーが付いていて、先に消す。"""
    unheld(stubs)
    inline(stubs, *names)
    for name in names:
        stubs.iam.add_response("delete_role_policy", {}, {"RoleName": NAME, "PolicyName": name})
    stubs.iam.add_response("delete_role", {}, THE_ROLE)


def all_deleted(stubs: Stubs) -> None:
    """手順 4。5 つとも、あって消せる。"""
    template_deleted(stubs)
    parameter_deleted(stubs)
    the_group(stubs, (GROUP, VPC))
    group_deleted(stubs)
    profile_deleted(stubs)
    role_deleted(stubs)


def images(stubs: Stubs, *found: str) -> None:
    """手順 6 が読む、自分のアカウントにあるサービスの AMI。"""
    stubs.ec2.add_response(
        "describe_images",
        {
            "Images": [
                {"ImageId": one, "Name": IMAGE_NAME, "CreationDate": "2026-09-20T00:00:00.000Z"}
                for one in found
            ]
        },
        SERVICE_IMAGES,
    )


def gone(*, what: str = "deleted") -> list[str]:
    """手順 4 と 5 の報告の行。"""
    return [
        f"launch template {TEMPLATE}: {what}",
        f"association {SSHD} ({ASSOCIATION}): {what}"
        if what == "deleted"
        else f"association {SSHD}: {what}",
        f"parameters under /{NAME}/: deleted (1)"
        if what == "deleted"
        else f"parameters under /{NAME}/: {what}",
        f"security group {NAME} ({GROUP}): {what}"
        if what == "deleted"
        else f"security group {NAME}: {what}",
        f"instance profile {NAME}: {what}",
        f"role {NAME}: {what}",
    ]


def staying(*, image: str | None = IMAGE) -> list[str]:
    """手順 6 の報告の行。AMI は、自分のアカウントにあるときだけ並ぶ。"""
    lines = [
        "left for the next apply:",
        "  key pair temt",
        "  ~/.ssh/temt/keys/temt",
        "  ~/.config/temt/dev-main.toml",
        f"  the Host {NAME} block in ~/.ssh/temt/config",
    ]
    if image is not None:
        lines.append(f"  AMI {IMAGE_NAME} ({image}, temt:service=dev-main)")
    return lines


def test_it_destroys_everything_and_reports_what_is_left(
    aws_stubs: Stubs,
    configured: Service,
    command: Path,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs, IMAGE)

    destroy.run(configured, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    # 手順 5。known_hosts のディレクトリとサービスのコマンドが消える
    assert not configured.known_hosts_dir.exists()
    assert not command.exists()
    captured = capsys.readouterr()
    # 一覧と進行は標準出力に、問いかけは標準エラー出力に書く
    assert captured.out.startswith(LISTING)
    assert told(captured.out.removeprefix(LISTING)) == [
        *gone(),
        f"~/.ssh/temt/known_hosts.d/{NAME}: deleted",
        f"~/.local/bin/{NAME}: deleted",
        *staying(),
    ]
    assert captured.err == PROMPT


def test_it_deletes_in_the_order_of_the_design(
    aws_stubs: Stubs, configured: Service, command: Path, calls: list[str]
) -> None:
    """起動テンプレート、関連付け、/<名前> の下の SSM パラメータ、セキュリティグループ、
    インスタンスプロファイル、IAM ロールの順に消す。"""
    _ = command
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs, IMAGE)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert calls == [
        "DescribeInstances",
        "ListRolePolicies",
        "DeleteLaunchTemplate",
        "ListAssociations",
        "DeleteAssociation",
        "GetParametersByPath",
        "DeleteParameters",
        "DescribeSecurityGroups",
        "DeleteSecurityGroup",
        "GetInstanceProfile",
        "RemoveRoleFromInstanceProfile",
        "DeleteInstanceProfile",
        "ListAttachedRolePolicies",
        "ListInstanceProfilesForRole",
        "ListRolePolicies",
        "DeleteRole",
        "DescribeImages",
    ]


def test_it_leaves_the_ami_out_when_the_service_has_none(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """AMI を挙げるのは、自分のアカウントに temt:service=dev-main のものがあるときだけである。"""
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[-5:] == staying(image=None)


def test_it_finishes_when_the_amis_cannot_be_read(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """手順 6 は報告だけなので、AMI が読めなくても destroy は終わる。"""
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "describe_images",
        service_error_code="UnauthorizedOperation",
        http_status_code=403,
        expected_params=SERVICE_IMAGES,
    )

    assert cli.main(["temt", "destroy", "--svc", "dev-main", "--yes"]) == 0

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err.startswith(
        "temt: warning: cannot tell which AMIs are tagged temt:service=dev-main: "
    )
    assert len(captured.err.splitlines()) == 1
    assert told(captured.out)[-5:] == staying(image=None)


def test_it_reports_what_was_not_there(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """何も無ければ、5 つとも absent と知らせて終わる。エラーにはしない。"""
    no_instance(aws_stubs)
    missing(aws_stubs.ec2, "delete_launch_template", resources.NO_SUCH_TEMPLATE, **THE_TEMPLATE)
    listed_associations(aws_stubs.ssm)
    no_parameters(aws_stubs)
    the_group(aws_stubs)
    missing(aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, **THE_PROFILE)
    missing(aws_stubs.iam, "list_attached_role_policies", resources.NO_SUCH_ENTITY, **THE_ROLE)
    missing(aws_stubs.iam, "list_role_policies", resources.NO_SUCH_ENTITY, **THE_ROLE)
    missing(aws_stubs.iam, "delete_role", resources.NO_SUCH_ENTITY, **THE_ROLE)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [
        *LISTING.splitlines(),
        *gone(what="absent"),
        # known_hosts のディレクトリはロックを取るときに作られるので、つねにある
        f"~/.ssh/temt/known_hosts.d/{NAME}: deleted",
        f"~/.local/bin/{NAME}: absent",
        *staying(image=None),
    ]


def test_it_names_and_deletes_every_inline_policy(
    aws_stubs: Stubs,
    configured: Service,
    command: Path,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """インラインポリシーは、temt-<ポリシー名> か手で付けたものかを問わず消す。

    手で付けたものも消えるので、確認の一覧に名前を挙げる。
    """
    _ = command
    names = ("temt-jupyter", "temt-idle", "by-hand")
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    inline(aws_stubs, *names)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    profile_deleted(aws_stubs)
    role_deleted(aws_stubs, *names)
    images(aws_stubs, IMAGE)

    destroy.run(configured, yes=False)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err == PROMPT
    listing = listed(role=f"role {NAME} (inline policies: temt-jupyter, temt-idle, by-hand)")
    assert captured.out.startswith(listing)
    reported = told(captured.out.removeprefix(listing))
    assert reported[5:9] == [
        "role policy temt-jupyter: deleted",
        "role policy temt-idle: deleted",
        "role policy by-hand: deleted",
        f"role {NAME}: deleted",
    ]


def test_it_goes_on_when_an_inline_policy_is_already_gone(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """一覧を読んでから消すまでに消えたインラインポリシーは、飛ばして続ける。"""
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    profile_deleted(aws_stubs)
    unheld(aws_stubs)
    inline(aws_stubs, "temt-jupyter")
    missing(
        aws_stubs.iam,
        "delete_role_policy",
        resources.NO_SUCH_ENTITY,
        RoleName=NAME,
        PolicyName="temt-jupyter",
    )
    aws_stubs.iam.add_response("delete_role", {}, THE_ROLE)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert "role policy temt-jupyter" not in out
    assert f"role {NAME}: deleted" in out


def test_it_terminates_the_instance_before_it_deletes(
    aws_stubs: Stubs,
    configured: Service,
    command: Path,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """動いているインスタンスは終了して terminated を待つ。"""
    _ = command
    directory = home.prepare_known_hosts(configured)
    (directory / INSTANCE).touch(mode=0o600)
    # 手順 1
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    # 手順 2
    inline(aws_stubs)
    # 手順 3
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )
    all_deleted(aws_stubs)
    images(aws_stubs, IMAGE)

    destroy.run(configured, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    assert not configured.known_hosts_dir.exists()
    captured = capsys.readouterr()
    assert captured.err == PROMPT
    running = listed(f"{INSTANCE} (running)")
    assert captured.out.startswith(running)
    assert told(captured.out.removeprefix(running)) == [
        f"terminating {INSTANCE}",
        f"{INSTANCE} is terminated",
        *gone(),
        f"~/.ssh/temt/known_hosts.d/{NAME}: deleted",
        f"~/.local/bin/{NAME}: deleted",
        *staying(),
    ]


def test_it_terminates_every_instance_with_the_name(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """2 台以上あれば、すべてを終了し、すべてが terminated になってから消す。"""
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running"), an_instance(OTHER, "stopped")),
        SEARCH,
    )
    inline(aws_stubs)
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response("terminate_instances", terminating(OTHER), {"InstanceIds": [OTHER]})
    both = {"InstanceIds": [INSTANCE, OTHER]}
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "terminated"), an_instance(OTHER, "shutting-down")),
        both,
    )
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "terminated"), an_instance(OTHER, "terminated")),
        both,
    )
    all_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    listing = listed(f"{INSTANCE} (running), {OTHER} (stopped)")
    assert told(capsys.readouterr().out) == [
        *listing.splitlines(),
        f"terminating {INSTANCE}",
        f"terminating {OTHER}",
        f"{INSTANCE} is terminated",
        f"{OTHER} is shutting-down",
        f"{OTHER} is terminated",
        *gone(),
        f"~/.ssh/temt/known_hosts.d/{NAME}: deleted",
        f"~/.local/bin/{NAME}: absent",
        *staying(image=None),
    ]


def test_it_deletes_nothing_when_the_instance_does_not_go_away(
    aws_stubs: Stubs,
    configured: Service,
    command: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """terminated を待つ締め切りを過ぎたら、リソースを消さずに終了する。"""
    monkeypatch.setattr(wait, "INTERVAL", 120)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    inline(aws_stubs)
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    for _ in range(6):
        aws_stubs.ec2.add_response(
            "describe_instances", reservations(an_instance(INSTANCE, "shutting-down")), BY_ID
        )

    with pytest.raises(
        TemtError, match=f"timed out after 10 minutes waiting for {INSTANCE} to be terminated"
    ):
        destroy.run(configured, yes=True)

    # 手順 4 は積んでいないので、消しに進んでいれば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == [120] * 5
    assert command.exists()
    assert configured.known_hosts_dir.is_dir()


def test_yes_skips_the_question(
    aws_stubs: Stubs,
    configured: Service,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs, IMAGE)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert answer.reads == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    # 何を消そうとしているかは、問いかけないときも見せる
    assert captured.out.startswith(LISTING)


def test_it_changes_nothing_when_the_answer_is_not_y(
    aws_stubs: Stubs,
    configured: Service,
    command: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """y と答えなければ、何も消さずに終了コード 1 で終わる。"""
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    no_instance(aws_stubs)

    assert cli.main(["temt", "destroy", "--svc", "dev-main"]) == 1

    # 探す以外は何も積んでいないので、消していれば Stubber が失敗する。main はそれも
    # 終了コード 1 にするが、Stubber の失敗は conftest が控えていて、テストを落とす
    aws_stubs.done()
    assert command.exists()
    assert configured.known_hosts_dir.is_dir()
    captured = capsys.readouterr()
    assert captured.out == LISTING
    assert captured.err == PROMPT + "temt: cancelled\n"


def test_it_needs_yes_when_stdin_is_not_a_terminal(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("y\n", tty=False))
    no_instance(aws_stubs)

    with pytest.raises(TemtError, match="stdin is not a terminal; pass --yes"):
        destroy.run(configured, yes=False)

    aws_stubs.done()


def test_it_stops_when_the_security_group_is_still_in_use(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    """待たずに、AWS の文に直し方を添えて止める。IAM には進まない。"""
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    aws_stubs.ec2.add_client_error(
        "delete_security_group",
        service_error_code=resources.DEPENDENCY_VIOLATION,
        service_message=f"resource {GROUP} has a dependent object",
        http_status_code=400,
        expected_params=THE_GROUP_ID,
    )

    with pytest.raises(TemtError) as caught:
        destroy.run(configured, yes=True)

    # IAM には 1 つも積んでいないので、先へ進んでいれば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == []
    assert str(caught.value) == (
        f"cannot delete the security group {GROUP}: An error occurred (DependencyViolation) "
        f"when calling the DeleteSecurityGroup operation: resource {GROUP} has a dependent "
        "object; if its instances were just terminated, run destroy again; otherwise remove "
        "what uses it and run destroy again"
    )


def test_the_error_codes_are_the_ones_aws_returns() -> None:
    """名前を間違えると、既に無いものや、待てば消せるもので止まってしまう。

    テストの応答も同じ定数で作るので、値はここで固定する。
    """
    assert resources.NO_SUCH_GROUP == "InvalidGroup.NotFound"
    assert resources.NO_SUCH_ASSOCIATION == "AssociationDoesNotExist"
    assert resources.DEPENDENCY_VIOLATION == "DependencyViolation"
    assert resources.DELETE_CONFLICT == "DeleteConflict"


def test_it_deletes_every_group_with_the_name(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """名前が <名前> のセキュリティグループを VPC を問わず消す。

    SubnetId を別の VPC に変えたあと古い VPC に残ったものも、ここで消える。
    """
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC), (OTHER_GROUP, OTHER_VPC))
    group_deleted(aws_stubs)
    aws_stubs.ec2.add_response("delete_security_group", {"Return": True}, {"GroupId": OTHER_GROUP})
    profile_deleted(aws_stubs)
    role_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    lines = told(capsys.readouterr().out)
    assert f"security group {NAME} ({GROUP}): deleted" in lines
    assert f"security group {NAME} ({OTHER_GROUP}): deleted" in lines


def test_it_counts_a_group_gone_after_listing_as_absent(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """探した後で消えたセキュリティグループ（並行する destroy など）は absent と知らせ、
    先へ進む。
    """
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    missing(aws_stubs.ec2, "delete_security_group", resources.NO_SUCH_GROUP, **THE_GROUP_ID)
    profile_deleted(aws_stubs)
    role_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    lines = told(capsys.readouterr().out)
    assert f"security group {NAME} ({GROUP}): absent" in lines
    assert f"role {NAME}: deleted" in lines


def test_it_stops_when_the_instance_profile_holds_another_role(
    aws_stubs: Stubs, configured: Service, command: Path
) -> None:
    """外すのはロール <名前> だけである。他のロールが入っていれば消せない。"""
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    aws_stubs.iam.add_response("get_instance_profile", a_profile(["other"]), THE_PROFILE)

    with pytest.raises(
        TemtError,
        match=(
            f"the instance profile {NAME} holds another role: other; "
            "remove it by hand and run destroy again"
        ),
    ):
        destroy.run(configured, yes=True)

    # ロールを外す呼び出しも、その先も積んでいない
    aws_stubs.done()
    assert command.exists()


def test_it_stops_when_the_instance_profile_will_not_go(
    aws_stubs: Stubs, configured: Service
) -> None:
    """読んだ後に何かが付いたときも、外したことが IAM にまだ映らないときも、消せなかった
    ことと AWS の文が伝わる。"""
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    aws_stubs.iam.add_response("get_instance_profile", a_profile([NAME]), THE_PROFILE)
    aws_stubs.iam.add_response(
        "remove_role_from_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
    )
    aws_stubs.iam.add_client_error(
        "delete_instance_profile",
        service_error_code=resources.DELETE_CONFLICT,
        service_message="Cannot delete entity, must remove roles from instance profile first.",
        http_status_code=409,
        expected_params=THE_PROFILE,
    )

    with pytest.raises(TemtError) as caught:
        destroy.run(configured, yes=True)
    assert str(caught.value) == (
        f"cannot delete the instance profile {NAME}: An error occurred (DeleteConflict) when "
        "calling the DeleteInstanceProfile operation: Cannot delete entity, must remove roles "
        "from instance profile first.; IAM may not show the role removed yet, so run destroy "
        "again; if this persists, remove the role from it by hand"
    )

    aws_stubs.done()


def test_it_stops_when_the_role_has_more_attached(
    aws_stubs: Stubs, configured: Service, command: Path
) -> None:
    """管理ポリシーは temt が付けないので、付いていれば手で付けたものである。インライン
    ポリシーも消さずに止まる。"""
    other = "arn:aws:iam::aws:policy/ReadOnlyAccess"
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    profile_deleted(aws_stubs)
    aws_stubs.iam.add_response(
        "list_attached_role_policies",
        {"AttachedPolicies": [{"PolicyName": "ReadOnlyAccess", "PolicyArn": other}]},
        THE_ROLE,
    )
    aws_stubs.iam.add_response(
        "list_instance_profiles_for_role",
        # 自分のプロファイルは直前に消したもので、IAM の反映が遅れて残っても数えない
        {
            "InstanceProfiles": [
                a_profile([NAME])["InstanceProfile"],
                {**a_profile([NAME])["InstanceProfile"], "InstanceProfileName": "other"},
            ]
        },
        THE_ROLE,
    )

    with pytest.raises(TemtError) as caught:
        destroy.run(configured, yes=True)

    assert str(caught.value) == (
        f"the role {NAME} is still held by the managed policy {other}, "
        "the instance profile other; detach or remove them by hand and run destroy again"
    )

    aws_stubs.done()
    # 手順 5 は手順 4 の後なので、サービスのコマンドも known_hosts も残る
    assert command.exists()
    assert configured.known_hosts_dir.is_dir()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_it_deletes_whatever_is_in_place_of_the_command(
    aws_stubs: Stubs,
    configured: Service,
    home_dir: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """サービスのコマンドの場所にあるものは、中身にかかわらず消す。"""
    path = configured.command_path
    path.parent.mkdir(parents=True)
    if kind == "file":
        path.write_text("mine\n", encoding="utf-8")
    else:
        path.symlink_to(home_dir / "missing")
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert not path.is_symlink()
    assert not path.exists()
    assert f"~/.local/bin/{NAME}: deleted" in capsys.readouterr().out


def test_it_stops_when_the_known_hosts_directory_is_a_symlink(
    aws_stubs: Stubs, configured: Service, home_dir: Path
) -> None:
    """利用者が置き換えていれば、その先まで消さずに断って終わる。"""
    elsewhere = home_dir / "keep"
    elsewhere.mkdir()
    mine = elsewhere / INSTANCE
    mine.write_text("mine\n", encoding="utf-8")
    configured.known_hosts_dir.parent.mkdir(parents=True)
    configured.known_hosts_dir.symlink_to(elsewhere)
    no_instance(aws_stubs)
    all_deleted(aws_stubs)

    with pytest.raises(TemtError, match=f"known_hosts.d/{NAME} is a symlink"):
        destroy.run(configured, yes=True)

    # 手順 6 は積んでいないので、そこまで進んでいれば Stubber が失敗する
    aws_stubs.done()
    assert configured.known_hosts_dir.is_symlink()
    # ロックファイルはリンクを通して置くが、利用者のものには触れない
    assert mine.read_text(encoding="utf-8") == "mine\n"


def test_it_leaves_another_service_alone(
    aws_stubs: Stubs, configured: Service, command: Path
) -> None:
    """消すのは、このサービスの known_hosts とサービスのコマンドだけである。"""
    _ = command
    other = Service("dev-other")
    kept = home.prepare_known_hosts(other) / INSTANCE
    kept.touch(mode=0o600)
    home.write_command(other)
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert not configured.known_hosts_dir.exists()
    assert kept.exists()
    assert other.command_path.exists()


def test_it_stops_when_there_is_no_config_file(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """destroy が要るのは設定ファイルだけなので、無ければ apply は促さない。"""
    assert cli.main(["temt", "destroy", "--svc", "dev-main", "--yes"]) == 1

    # Session も作っていないので、AWS には触れていない
    aws_stubs.done()
    assert aws_stubs.passed == []
    err = capsys.readouterr().err
    assert err == (f"temt: no config file: {service.conf_path}; destroy needs AwsRegion from it\n")
    assert "apply" not in err


def test_it_stops_at_the_first_failure(
    aws_stubs: Stubs, configured: Service, command: Path
) -> None:
    """途中で失敗したら、そこで止まる。後のものには触れない。"""
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path", {"Parameters": [{"Name": PARAMETER}]}, UNDER_PATH
    )
    aws_stubs.ssm.add_client_error(
        "delete_parameters",
        service_error_code="AccessDeniedException",
        http_status_code=400,
        expected_params=THE_PARAMETERS,
    )

    with pytest.raises(ClientError) as raised:
        destroy.run(configured, yes=True)

    # 上がってきたのは、パラメータを消したときの誤りである。セキュリティグループと
    # IAM は積んでいないので、触れていれば Stubber が失敗する
    assert raised.value.operation_name == "DeleteParameters"
    assert raised.value.response.get("Error", {}).get("Code") == "AccessDeniedException"
    aws_stubs.done()
    assert command.exists()
    assert configured.known_hosts_dir.is_dir()


def test_a_second_run_goes_on_from_where_it_stopped(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """消えたものは absent で飛ばし、残りを消す。"""
    no_instance(aws_stubs)
    missing(aws_stubs.ec2, "delete_launch_template", resources.NO_SUCH_TEMPLATE, **THE_TEMPLATE)
    listed_associations(aws_stubs.ssm)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs)
    missing(aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, **THE_PROFILE)
    role_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[len(LISTING.splitlines()) :][:6] == [
        f"launch template {TEMPLATE}: absent",
        f"association {SSHD}: absent",
        f"parameters under /{NAME}/: deleted (1)",
        f"security group {NAME}: absent",
        f"instance profile {NAME}: absent",
        f"role {NAME}: deleted",
    ]


def test_it_stops_when_another_command_holds_the_lock(
    aws_stubs: Stubs, configured: Service
) -> None:
    """ロックは AWS を読むより先に取る。取れなければ待たずに終わる。"""
    held = home.prepare_known_hosts(configured) / lock.LOCK_NAME
    with held.open("a", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            deadline(5.0),
            pytest.raises(TemtError, match=f"another temt command is running for {NAME}"),
        ):
            destroy.run(configured, yes=True)

    # 応答を 1 つも積んでいないので、AWS を読んでいれば Stubber が失敗する
    aws_stubs.done()


def test_it_holds_the_lock_while_it_waits(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """待ち合わせの間もロックを持っている。別の destroy は入ってこない。"""
    attempts: list[bool] = []

    def probe(seconds: float) -> None:
        clock.sleep(seconds)
        attempts.append(taken(configured))

    monkeypatch.setattr(wait, "sleep", probe)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    inline(aws_stubs)
    aws_stubs.ec2.add_response("terminate_instances", terminating(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "shutting-down")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )
    all_deleted(aws_stubs)
    images(aws_stubs)

    with deadline(5.0):
        destroy.run(configured, yes=True)

    aws_stubs.done()
    assert attempts == [False]
    # 終わったときには、ロックファイルごとディレクトリが消えている
    assert not configured.known_hosts_dir.exists()


def test_destroy_takes_yes_and_no_async() -> None:
    """destroy はつねに完了を待つので、--async は取らない。"""
    assert cli.parse(["destroy", "--svc", "dev-main"]).yes is False
    assert cli.parse(["destroy", "--svc", "dev-main", "--yes"]).yes is True
    assert cli.parse(["destroy", "--svc", "dev-main", "--yes"]).is_async is False
    with pytest.raises(UsageError, match="destroy: unexpected argument: --async"):
        cli.parse(["destroy", "--svc", "dev-main", "--async"])
