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
    REGION,
    WHERE,
    Clock,
    Stubs,
    an_instance,
    deadline,
    identified,
    reservations,
    taken,
)
from types_boto3_ec2.client import EC2Client
from types_boto3_iam.client import IAMClient
from types_boto3_ssm.client import SSMClient

from temt import cli, destroy, home, lock, resources, wait
from temt.msg import TemtError, UsageError
from temt.names import Service
from temt.table import tilde

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
        {"Name": "tag:temt:service", "Values": ["dev-main"]},
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
        {"Name": "name", "Values": ["temt-dev-main-??????????"]},
        {"Name": "state", "Values": ["available", "pending"]},
    ],
}

# temt のものであることを示すタグ。消してよいのは、これが付いたものだけである
OURS = [{"Key": "temt:service", "Value": "dev-main"}]
# このリージョンと、ほかのリージョンのタグのキー
HERE = f"temt:region:{REGION}"
THERE = "temt:region:us-west-2"
# ロールに付いた管理ポリシー。AWS 管理のものと、顧客管理のもの
MANAGED = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
CUSTOMER = "arn:aws:iam::123456789012:policy/web-data"
# 消さずに残したときの報告
LEFT = "left (not tagged temt:service=dev-main)"

PROMPT = f"destroy {NAME} {WHERE}? [y/N] "

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def listed(
    found: str = "none",
    *,
    role: str = f"role {NAME}",
    profile: str = f"instance profile {NAME}",
) -> str:
    """手順 2 の一覧。"""
    lines = [
        f"instances: {found}",
        f"launch template {TEMPLATE}",
        f"parameters under /{NAME}/",
        f"security group {NAME}",
        profile,
        role,
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


def a_profile(
    roles: list[str], *, name: str = NAME, tags: list[dict[str, str]] | None = OURS
) -> dict[str, Any]:
    """get-instance-profile が返す形。

    tags が None なら、タグを 1 つも持たない。AWS はそのとき Tags を返さない。
    """
    profile: dict[str, Any] = {
        "Path": "/",
        "InstanceProfileName": name,
        "InstanceProfileId": "AIPAEXAMPLE000000",
        "Arn": f"arn:aws:iam::123456789012:instance-profile/{name}",
        "CreateDate": WHEN,
        "Roles": [
            {
                "Path": "/",
                "RoleName": role,
                "RoleId": "AROAEXAMPLE000000",
                "Arn": f"arn:aws:iam::123456789012:role/{role}",
                "CreateDate": WHEN,
            }
            for role in roles
        ],
    }
    if tags is not None:
        profile["Tags"] = tags
    return {"InstanceProfile": profile}


def a_role(*, tags: list[dict[str, str]] | None = OURS) -> dict[str, Any]:
    """get-role が返す、ロール <名前> の形。tags は a_profile と同じ。"""
    role: dict[str, Any] = {
        "Path": "/",
        "RoleName": NAME,
        "RoleId": "AROAEXAMPLE000000",
        "Arn": f"arn:aws:iam::123456789012:role/{NAME}",
        "CreateDate": WHEN,
    }
    if tags is not None:
        role["Tags"] = tags
    return {"Role": role}


def attached(*arns: str) -> dict[str, Any]:
    """list-attached-role-policies が返す形。"""
    return {
        "AttachedPolicies": [
            {"PolicyName": arn.rsplit("/", 1)[-1], "PolicyArn": arn} for arn in arns
        ]
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


def no_instance(
    stubs: Stubs, *, tags: list[dict[str, str]] | None = OURS, exists: bool = True
) -> None:
    """手順 1 と 2。インスタンスは 1 台も無く、ロールにポリシーも付いていない。tags と
    exists は held と同じ。
    """
    stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    held(stubs, tags=tags, exists=exists)


def held(
    stubs: Stubs,
    inline: tuple[str, ...] = (),
    managed: tuple[str, ...] = (),
    *,
    tags: list[dict[str, str]] | None = OURS,
    exists: bool = True,
) -> None:
    """手順 2 の一覧が読む、ロールのタグ tags と、消すなら、インラインポリシーの名前と
    管理ポリシーの ARN。

    exists が偽ならロールは無い。temt:service=<svc> が付いていないか、ほかのリージョンの
    タグが残っていれば、消さないのでポリシーは読まない。
    """
    if not exists:
        missing(stubs.iam, "get_role", resources.NO_SUCH_ENTITY, **THE_ROLE)
        return
    stubs.iam.add_response("get_role", a_role(tags=tags), THE_ROLE)
    keys = [one["Key"] for one in tags or []]
    if {"Key": "temt:service", "Value": "dev-main"} not in (tags or []) or any(
        key.startswith("temt:region:") and key != HERE for key in keys
    ):
        return
    stubs.iam.add_response("list_role_policies", {"PolicyNames": list(inline)}, THE_ROLE)
    stubs.iam.add_response("list_attached_role_policies", attached(*managed), THE_ROLE)


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
    """起動テンプレートを消す。"""
    stubs.ec2.add_response(
        "delete_launch_template", {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}}, THE_TEMPLATE
    )


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


def role_read(stubs: Stubs, *profiles: str, tags: list[dict[str, str]] = OURS) -> None:
    """インスタンスプロファイルを消す前に、ロールのタグ tags と、ロールを入れた
    インスタンスプロファイル profiles を読む。tags にこのリージョンのタグがあれば外す。

    ロールがほかのインスタンスプロファイルにも入っていれば、インスタンスプロファイルも
    消さずに止めるためである。
    """
    stubs.iam.add_response("get_role", a_role(tags=tags), THE_ROLE)
    if any(one["Key"] == HERE for one in tags):
        untagged(stubs)
    stubs.iam.add_response(
        "list_instance_profiles_for_role",
        {"InstanceProfiles": [a_profile([NAME], name=one)["InstanceProfile"] for one in profiles]},
        THE_ROLE,
    )


def untagged(stubs: Stubs) -> None:
    """ロールから、このリージョンのタグを外す。"""
    stubs.iam.add_response("untag_role", {}, {"RoleName": NAME, "TagKeys": [HERE]})


def role_stripped(
    stubs: Stubs, inline: tuple[str, ...] = (), managed: tuple[str, ...] = ()
) -> None:
    """インスタンスプロファイルの後で、ロールを消す。managed の管理ポリシーを外し、inline の
    インラインポリシーを消してから消す。
    """
    stubs.iam.add_response("list_attached_role_policies", attached(*managed), THE_ROLE)
    for arn in managed:
        stubs.iam.add_response("detach_role_policy", {}, {"RoleName": NAME, "PolicyArn": arn})
    stubs.iam.add_response("list_role_policies", {"PolicyNames": list(inline)}, THE_ROLE)
    for name in inline:
        stubs.iam.add_response("delete_role_policy", {}, {"RoleName": NAME, "PolicyName": name})
    stubs.iam.add_response("delete_role", {}, THE_ROLE)


def iam_deleted(stubs: Stubs, inline: tuple[str, ...] = (), managed: tuple[str, ...] = ()) -> None:
    """インスタンスプロファイルとロールを消す。ロールを読み、ロールを入れたインスタンス
    プロファイル <名前> を消してから、ロールを消す。inline と managed は role_stripped と同じ。
    """
    role_read(stubs, NAME)
    profile_deleted(stubs)
    role_stripped(stubs, inline, managed)


def all_deleted(stubs: Stubs) -> None:
    """手順 4。どれも、あって消せる。"""
    template_deleted(stubs)
    parameter_deleted(stubs)
    the_group(stubs, (GROUP, VPC))
    group_deleted(stubs)
    iam_deleted(stubs)


def images(stubs: Stubs, *found: str, state: str = "available") -> None:
    """手順 6 が読む、自分のアカウントにあるサービスの AMI。名前で探す。"""
    stubs.ec2.add_response(
        "describe_images",
        {
            "Images": [
                {
                    "ImageId": one,
                    "Name": IMAGE_NAME,
                    "State": state,
                    "CreationDate": "2026-09-20T00:00:00.000Z",
                }
                for one in found
            ]
        },
        SERVICE_IMAGES,
    )


def gone(*, what: str = "deleted") -> list[str]:
    """手順 4 の報告の行。"""
    return [
        f"launch template {TEMPLATE}: {what}",
        f"parameters under /{NAME}/: deleted (1)"
        if what == "deleted"
        else f"parameters under /{NAME}/: {what}",
        f"security group {NAME} ({GROUP}): {what}"
        if what == "deleted"
        else f"security group {NAME}: {what}",
        f"instance profile {NAME}: {what}",
        f"role {NAME}: {what}",
    ]


def staying(*, image: str | None = IMAGE, note: str = "") -> list[str]:
    """手順 5 の報告の行。AMI は、次の apply が選ぶものが自分のアカウントにあるときだけ並ぶ。

    known_hosts のディレクトリとサービスのコマンドも、ほかのリージョンのクローンが使うかも
    しれないので残す。
    """
    lines = [
        "left for the next apply:",
        "  ~/.config/temt/dev-main.toml",
        f"  the Host {NAME} block in ~/.ssh/temt/config",
        f"  ~/.ssh/temt/known_hosts.d/{NAME}/",
        f"  ~/.local/bin/{NAME}",
    ]
    if image is not None:
        lines.append(f"  AMI {IMAGE_NAME} ({image}{note})")
    return lines


def test_it_destroys_everything_and_reports_what_is_left(
    aws_stubs: Stubs,
    configured: Service,
    command: Path,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identified(aws_stubs)
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs, IMAGE)

    destroy.run(configured, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    # known_hosts のディレクトリとサービスのコマンドは残す
    assert configured.known_hosts_dir.is_dir()
    assert command.exists()
    captured = capsys.readouterr()
    # 一覧と進行は標準出力に、問いかけは標準エラー出力に書く
    assert captured.out.startswith(LISTING)
    assert told(captured.out.removeprefix(LISTING)) == [
        *gone(),
        *staying(),
    ]
    assert captured.err == PROMPT


def test_it_deletes_in_the_order_of_the_design(
    aws_stubs: Stubs, configured: Service, command: Path, calls: list[str]
) -> None:
    """起動テンプレート、/<名前> の下の SSM パラメータ、セキュリティグループ、
    インスタンスプロファイル、IAM ロールの順に消す。

    ロールのタグと、ロールがほかのインスタンスプロファイルに入っていないことは、
    インスタンスプロファイルを消す前に確かめる。ロールは、管理ポリシーを外し、インライン
    ポリシーを消し、最後に消す。IAM の DeleteRole は、どちらかが残っていると断る。
    """
    _ = command
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    iam_deleted(aws_stubs, ("temt-ec2",), (MANAGED,))
    images(aws_stubs, IMAGE)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert calls == [
        "DescribeInstances",
        "GetRole",
        "ListRolePolicies",
        "ListAttachedRolePolicies",
        "DeleteLaunchTemplate",
        "GetParametersByPath",
        "DeleteParameters",
        "DescribeSecurityGroups",
        "DeleteSecurityGroup",
        "GetRole",
        "ListInstanceProfilesForRole",
        "GetInstanceProfile",
        "RemoveRoleFromInstanceProfile",
        "DeleteInstanceProfile",
        "ListAttachedRolePolicies",
        "DetachRolePolicy",
        "ListRolePolicies",
        "DeleteRolePolicy",
        "DeleteRole",
        "DescribeImages",
    ]


def test_it_leaves_the_ami_out_when_the_service_has_none(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """AMI を挙げるのは、自分のアカウントに temt-dev-main-<時刻> の AMI があるときだけである。
    無ければ次の apply は Ubuntu 24.04 を選ぶが、それは残したものではない。
    """
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[-5:] == staying(image=None)


def test_it_tells_that_the_newest_ami_is_pending(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """最新の AMI が pending なら、そう添える。次の apply は、それが available になるまで止まる。"""
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs, IMAGE, state="pending")

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[-6:] == staying(note=", pending")


def test_it_leaves_the_ami_out_when_image_id_is_written(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ImageId を書いていれば、次の apply はそれを使い、選ばない。AMI を探さずに、設定ファイル
    だけを挙げる。
    """
    text = configured.conf_path.read_text(encoding="utf-8")
    configured.conf_path.write_text(
        text.replace("[launch-template]\n", f'[launch-template]\nImageId = "{IMAGE}"\n'),
        encoding="utf-8",
    )
    no_instance(aws_stubs)
    all_deleted(aws_stubs)

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
    assert captured.err.startswith(f"temt: warning: cannot tell the newest AMI of {NAME}: ")
    assert len(captured.err.splitlines()) == 1
    assert told(captured.out)[-5:] == staying(image=None)


def test_it_reports_what_was_not_there(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """何も無ければ、どれも absent と知らせて終わる。エラーにはしない。"""
    # 手順 2 の一覧も、ロールが無ければポリシーを読まない
    no_instance(aws_stubs, exists=False)
    missing(aws_stubs.ec2, "delete_launch_template", resources.NO_SUCH_TEMPLATE, **THE_TEMPLATE)
    no_parameters(aws_stubs)
    the_group(aws_stubs)
    # ロールが無ければ、ロールを入れたインスタンスプロファイルは読まない
    missing(aws_stubs.iam, "get_role", resources.NO_SUCH_ENTITY, **THE_ROLE)
    missing(aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, **THE_PROFILE)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [
        *LISTING.splitlines(),
        *gone(what="absent"),
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
    identified(aws_stubs)
    _ = command
    names = ("temt-jupyter", "temt-idle", "by-hand")
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    held(aws_stubs, names)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    iam_deleted(aws_stubs, names)
    images(aws_stubs, IMAGE)

    destroy.run(configured, yes=False)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err == PROMPT
    listing = listed(role=f"role {NAME} (inline policies: temt-jupyter, temt-idle, by-hand)")
    assert captured.out.startswith(listing)
    reported = told(captured.out.removeprefix(listing))
    assert reported[4:8] == [
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
    role_read(aws_stubs, NAME)
    profile_deleted(aws_stubs)
    aws_stubs.iam.add_response("list_attached_role_policies", attached(), THE_ROLE)
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": ["temt-jupyter"]}, THE_ROLE)
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
    identified(aws_stubs)
    _ = command
    directory = home.prepare_known_hosts(configured)
    (directory / INSTANCE).touch(mode=0o600)
    # ほかのリージョンのクローンのインスタンスのもの
    (directory / OTHER).touch(mode=0o600)
    # 手順 1
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running", address=ADDRESS)),
        SEARCH,
    )
    # 手順 2
    held(aws_stubs)
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
    # 終了したインスタンスの known_hosts のファイルだけを消し、ディレクトリは残す
    assert not (directory / INSTANCE).exists()
    assert (directory / OTHER).exists()
    captured = capsys.readouterr()
    assert captured.err == PROMPT
    running = listed(f"{INSTANCE} (running)")
    assert captured.out.startswith(running)
    assert told(captured.out.removeprefix(running)) == [
        f"terminating {INSTANCE}",
        f"{INSTANCE} is terminated",
        *gone(),
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
    held(aws_stubs)
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
    held(aws_stubs)
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
    identified(aws_stubs)
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
    iam_deleted(aws_stubs)
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
    iam_deleted(aws_stubs)
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
    # ロール <名前> は、どのインスタンスプロファイルにも入っていない
    role_read(aws_stubs)
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
    role_read(aws_stubs, NAME)
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


def test_it_detaches_the_managed_policies_and_deletes_the_role(
    aws_stubs: Stubs,
    configured: Service,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """管理ポリシーは外すだけで、ポリシーそのものは残す。temt が作ったものではない。

    外すものも確認の一覧に挙げ、外したものを 1 つずつ知らせる。
    """
    identified(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    held(aws_stubs, ("temt-ec2", "by-hand"), (MANAGED, CUSTOMER))
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    iam_deleted(aws_stubs, ("temt-ec2", "by-hand"), (MANAGED, CUSTOMER))
    images(aws_stubs)

    destroy.run(configured, yes=False)

    # 管理ポリシーを消す呼び出し（DeletePolicy）は積んでいないので、呼べば Stubber が失敗する
    aws_stubs.done()
    assert answer.reads == 1
    captured = capsys.readouterr()
    listing = listed(
        role=(
            f"role {NAME} (inline policies: temt-ec2, by-hand; "
            f"managed policies: {MANAGED}, {CUSTOMER})"
        )
    )
    assert captured.out.startswith(listing)
    assert told(captured.out.removeprefix(listing))[3:9] == [
        f"instance profile {NAME}: deleted",
        f"role {NAME}: detached {MANAGED}",
        f"role {NAME}: detached {CUSTOMER}",
        "role policy temt-ec2: deleted",
        "role policy by-hand: deleted",
        f"role {NAME}: deleted",
    ]


def test_it_lists_only_the_managed_policies_when_there_is_no_inline_one(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """確認の一覧には、付いているものだけを挙げる。"""
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    held(aws_stubs, managed=(MANAGED,))
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    iam_deleted(aws_stubs, managed=(MANAGED,))
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert capsys.readouterr().out.startswith(
        listed(role=f"role {NAME} (managed policies: {MANAGED})")
    )


def test_it_stops_when_the_role_is_in_another_instance_profile(
    aws_stubs: Stubs,
    configured: Service,
    command: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ロールがほかのインスタンスプロファイルにも入っていれば、インスタンスプロファイル
    <名前> も消さず、ロールから何も外さず、ロールも消さずに止まる。それより前に消すもの
    （セキュリティグループまで）は消える。

    インスタンスプロファイル <名前> は、ロールを入れていても、ほかのものとは数えない。
    """
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    role_read(aws_stubs, NAME, "web-profile")

    with pytest.raises(TemtError) as caught:
        destroy.run(configured, yes=True)

    assert str(caught.value) == (
        f"the role {NAME} is also in the instance profile web-profile; "
        "remove it from there by hand and run destroy again"
    )
    # インスタンスプロファイルを読む呼び出しも、ロールのポリシーを外す呼び出しも積んで
    # いない
    aws_stubs.done()
    assert told(capsys.readouterr().out)[-1] == f"security group {NAME} ({GROUP}): deleted"
    # 手順 5 は手順 4 の後なので、サービスのコマンドも known_hosts も残る
    assert command.exists()
    assert configured.known_hosts_dir.is_dir()


def test_it_stops_when_the_role_is_in_the_instance_profile_left_alone(
    aws_stubs: Stubs,
    configured: Service,
    command: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """タグが違って残したインスタンスプロファイル <名前> にロールが入っていれば、ロールの
    ポリシーを外しも消しもせずに止まる。

    残したインスタンスプロファイルは誰かが使っているかもしれず、ロールのポリシーだけを
    消すと、そのインスタンスが権限を失う。
    """
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    role_read(aws_stubs, NAME)
    aws_stubs.iam.add_response("get_instance_profile", a_profile([NAME], tags=None), THE_PROFILE)

    with pytest.raises(TemtError) as caught:
        destroy.run(configured, yes=True)

    assert str(caught.value) == (
        f"the role {NAME} is also in the instance profile {NAME}, which is left as it is not "
        "tagged temt:service=dev-main; remove the role from it by hand and run destroy again"
    )
    # ロールを外す、ポリシーを外す、消す呼び出しは積んでいない
    aws_stubs.done()
    assert told(capsys.readouterr().out)[-1] == f"instance profile {NAME}: {LEFT}"
    assert command.exists()


@pytest.mark.parametrize(
    "tags",
    [
        pytest.param(None, id="untagged"),
        pytest.param([{"Key": "Name", "Value": NAME}], id="other-tags"),
        pytest.param([{"Key": "temt:service", "Value": "dev-other"}], id="other-service"),
    ],
)
def test_it_leaves_a_pair_not_tagged_for_the_service(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
    tags: list[dict[str, str]] | None,
) -> None:
    """temt:service=<svc> の無いインスタンスプロファイルとロールは、外しも消しもせずに
    残し、そう知らせて先へ進む。設定ファイルが指しているかどうかは問わない。

    ロールを消さないので、ロールを入れたインスタンスプロファイルも読まない。
    """
    no_instance(aws_stubs, tags=tags)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    aws_stubs.iam.add_response("get_role", a_role(tags=tags), THE_ROLE)
    aws_stubs.iam.add_response("get_instance_profile", a_profile([NAME], tags=tags), THE_PROFILE)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    # ロールを外す、ポリシーを外す、消す呼び出しは積んでいない
    aws_stubs.done()
    assert told(capsys.readouterr().out)[len(LISTING.splitlines()) :] == [
        f"launch template {TEMPLATE}: deleted",
        f"parameters under /{NAME}/: deleted (1)",
        f"security group {NAME} ({GROUP}): deleted",
        f"instance profile {NAME}: {LEFT}",
        f"role {NAME}: {LEFT}",
        *staying(image=None),
    ]


@pytest.mark.parametrize("left", ["instance profile", "role"])
def test_it_decides_each_of_the_pair_by_its_own_tag(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str], left: str
) -> None:
    """インスタンスプロファイルとロールは、それぞれ自分のタグで決める。片方だけでも消す。

    残すインスタンスプロファイルにロールが入っていなければ、ロールは消す。残すロールが
    入っていても、インスタンスプロファイルからロール <名前> を外して消す。
    """
    no_instance(aws_stubs, tags=OURS if left == "instance profile" else None)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    if left == "instance profile":
        role_read(aws_stubs)
        aws_stubs.iam.add_response("get_instance_profile", a_profile([], tags=None), THE_PROFILE)
        role_stripped(aws_stubs)
    else:
        aws_stubs.iam.add_response("get_role", a_role(tags=None), THE_ROLE)
        profile_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    lines = told(capsys.readouterr().out)
    profile_state = LEFT if left == "instance profile" else "deleted"
    role_state = LEFT if left == "role" else "deleted"
    assert lines[len(LISTING.splitlines()) :][3:5] == [
        f"instance profile {NAME}: {profile_state}",
        f"role {NAME}: {role_state}",
    ]


def test_it_deletes_the_instance_profile_when_the_role_is_gone(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ロールが無くても、インスタンスプロファイル <名前> は消し、ロールは absent と知らせる。"""
    no_instance(aws_stubs, exists=False)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    missing(aws_stubs.iam, "get_role", resources.NO_SUCH_ENTITY, **THE_ROLE)
    # ロールが無いので、インスタンスプロファイルにも入っていない。外す呼び出しは積まない
    aws_stubs.iam.add_response("get_instance_profile", a_profile([]), THE_PROFILE)
    aws_stubs.iam.add_response("delete_instance_profile", {}, THE_PROFILE)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[len(LISTING.splitlines()) :][3:5] == [
        f"instance profile {NAME}: deleted",
        f"role {NAME}: absent",
    ]


def test_it_removes_the_tag_of_its_region_before_deleting_the_last_use(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ロールにこのリージョンのタグしか無ければ、タグを外してから、インスタンスプロファイルと
    ロールを消す。"""
    tags = [*OURS, {"Key": HERE, "Value": ""}]
    no_instance(aws_stubs, tags=tags)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    role_read(aws_stubs, NAME, tags=tags)
    profile_deleted(aws_stubs)
    role_stripped(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert out.startswith(LISTING)
    assert told(out)[len(LISTING.splitlines()) :][3:6] == [
        f"role {NAME}: untagged {HERE}",
        f"instance profile {NAME}: deleted",
        f"role {NAME}: deleted",
    ]


@pytest.mark.parametrize("here", [True, False], ids=["tagged here", "not tagged here"])
def test_it_keeps_the_pair_that_another_region_uses(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str], here: bool
) -> None:
    """ロールにほかのリージョンのタグが残っていれば、このリージョンのタグだけを外し、
    インスタンスプロファイルもロールも残す。確認の一覧にも、残すことを、使うリージョンを
    添えて出す。ポリシーは読まず、インスタンスプロファイルは報告のために読むだけである。
    """
    tags = [*OURS, *([{"Key": HERE, "Value": ""}] if here else []), {"Key": THERE, "Value": ""}]
    no_instance(aws_stubs, tags=tags)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    aws_stubs.iam.add_response("get_role", a_role(tags=tags), THE_ROLE)
    if here:
        untagged(aws_stubs)
    # 報告のために、インスタンスプロファイルがあるかだけを読む
    aws_stubs.iam.add_response("get_instance_profile", a_profile([NAME]), THE_PROFILE)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    listing = listed(
        role=f"role {NAME} (kept: used in us-west-2)", profile=f"instance profile {NAME} (kept)"
    )
    assert out.startswith(listing)
    assert told(out)[len(listing.splitlines()) :][3:] == [
        *([f"role {NAME}: untagged {HERE}"] if here else []),
        f"instance profile {NAME}: kept (used in us-west-2)",
        f"role {NAME}: kept (used in us-west-2)",
        *staying(image=None),
    ]


def test_it_does_not_count_the_instance_profile_deleted_before(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """前の実行で消したインスタンスプロファイル <名前> が、IAM の反映が遅れて、ロールを
    入れたものの一覧に残っていても、ほかのものとは数えずにロールを消す。
    """
    no_instance(aws_stubs)
    missing(aws_stubs.ec2, "delete_launch_template", resources.NO_SUCH_TEMPLATE, **THE_TEMPLATE)
    no_parameters(aws_stubs)
    the_group(aws_stubs)
    role_read(aws_stubs, NAME)
    missing(aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, **THE_PROFILE)
    role_stripped(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[len(LISTING.splitlines()) :][3:5] == [
        f"instance profile {NAME}: absent",
        f"role {NAME}: deleted",
    ]


def test_it_stops_when_the_role_will_not_go(aws_stubs: Stubs, configured: Service) -> None:
    """外したことが IAM にまだ映らないときも、読んだ後に何かが付いたときも、消せなかった
    ことと AWS の文、両方の直し方が伝わる。"""
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    role_read(aws_stubs, NAME)
    profile_deleted(aws_stubs)
    aws_stubs.iam.add_response("list_attached_role_policies", attached(), THE_ROLE)
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": []}, THE_ROLE)
    aws_stubs.iam.add_client_error(
        "delete_role",
        service_error_code=resources.DELETE_CONFLICT,
        service_message="Cannot delete entity, must detach all policies first.",
        http_status_code=409,
        expected_params=THE_ROLE,
    )

    with pytest.raises(TemtError) as caught:
        destroy.run(configured, yes=True)

    assert str(caught.value) == (
        f"cannot delete the role {NAME}: An error occurred (DeleteConflict) when calling the "
        "DeleteRole operation: Cannot delete entity, must detach all policies first.; if "
        "nothing is attached to it, IAM may not show the change yet, so run destroy "
        "again; otherwise detach it by hand and run destroy again"
    )
    aws_stubs.done()


def test_it_counts_a_role_gone_before_deleting_as_absent(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """読んでから消すまでに消えたロール（並行する destroy など）は absent と知らせる。"""
    no_instance(aws_stubs)
    template_deleted(aws_stubs)
    parameter_deleted(aws_stubs)
    the_group(aws_stubs, (GROUP, VPC))
    group_deleted(aws_stubs)
    role_read(aws_stubs, NAME)
    profile_deleted(aws_stubs)
    aws_stubs.iam.add_response("list_attached_role_policies", attached(), THE_ROLE)
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": []}, THE_ROLE)
    missing(aws_stubs.iam, "delete_role", resources.NO_SUCH_ENTITY, **THE_ROLE)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert f"role {NAME}: absent" in told(capsys.readouterr().out)


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_it_keeps_whatever_is_in_place_of_the_command(
    aws_stubs: Stubs,
    configured: Service,
    home_dir: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """サービスのコマンドは、中身にかかわらず消さず、次の apply のために残したものに挙げる。

    ほかのリージョンのクローンが使うかもしれない。
    """
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
    if kind == "file":
        assert path.read_text(encoding="utf-8") == "mine\n"
    else:
        assert path.is_symlink()
    assert told(capsys.readouterr().out)[-len(staying(image=None)) :] == staying(image=None)


def test_it_keeps_a_known_hosts_directory_that_is_a_symlink(
    aws_stubs: Stubs, configured: Service, home_dir: Path
) -> None:
    """利用者が置き換えていても、ディレクトリにもその先にも、ロックのほかは触れない。"""
    elsewhere = home_dir / "keep"
    elsewhere.mkdir()
    mine = elsewhere / OTHER
    mine.write_text("mine\n", encoding="utf-8")
    configured.known_hosts_dir.parent.mkdir(parents=True)
    configured.known_hosts_dir.symlink_to(elsewhere)
    no_instance(aws_stubs)
    all_deleted(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert configured.known_hosts_dir.is_symlink()
    # ロックファイルはリンクを通して置くが、利用者のものには触れない
    assert mine.read_text(encoding="utf-8") == "mine\n"


def test_it_leaves_another_service_alone(
    aws_stubs: Stubs, configured: Service, command: Path
) -> None:
    """ほかのサービスの known_hosts とサービスのコマンドには触れない。"""
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
    assert kept.exists()
    assert other.command_path.exists()


def test_it_stops_when_there_is_no_config_file(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """打ち間違えたサービス名ですぐ止まる。片づける相手なので、apply は促さない。"""
    assert cli.main(["temt", "destroy", "--svc", "dev-main", "--yes"]) == 1

    # Session も作っていないので、AWS には触れていない
    aws_stubs.done()
    assert aws_stubs.passed == []
    err = capsys.readouterr().err
    assert err == f"temt: no config file: {tilde(service.conf_path)}\n"
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
    parameter_deleted(aws_stubs)
    the_group(aws_stubs)
    # インスタンスプロファイルは前の実行で消えていて、ロールはどれにも入っていない
    role_read(aws_stubs)
    missing(aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, **THE_PROFILE)
    role_stripped(aws_stubs)
    images(aws_stubs)

    destroy.run(configured, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[len(LISTING.splitlines()) :][:5] == [
        f"launch template {TEMPLATE}: absent",
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
    held(aws_stubs)
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
    # ロックファイルは、サービスごとのまま残す
    assert (configured.known_hosts_dir / lock.LOCK_NAME).exists()


def test_destroy_takes_yes_and_no_async() -> None:
    """destroy はつねに完了を待つので、--async は取らない。"""
    assert cli.parse(["destroy", "--svc", "dev-main"]).yes is False
    assert cli.parse(["destroy", "--svc", "dev-main", "--yes"]).yes is True
    assert cli.parse(["destroy", "--svc", "dev-main", "--yes"]).is_async is False
    with pytest.raises(UsageError, match="destroy: unexpected argument: --async"):
        cli.parse(["destroy", "--svc", "dev-main", "--async"])
