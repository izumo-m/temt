"""bake の本体のテスト。AWS は Stubber で、標準入力と時計と時刻は偽物に差し替える。

端から端まで（bake.run か cli.main）を通し、手順の順序と、失敗したときに設定ファイル、
テンプレート、記録がどう残るかを確かめる。Stubber は積んだ順にしか応答しないので、
応答を積む順が手順の順序を表す。
"""

import json
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import Clock, Stubs, an_instance, deadline, reservations, taken
from types_boto3_ec2.client import EC2Client
from types_boto3_ec2.type_defs import BlockDeviceMappingTypeDef

from temt import app, bake, cli, conf, home, lock, resources, wait
from temt.msg import TemtError
from temt.names import Service, defaults_path
from temt.table import tilde

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
ADDRESS = "203.0.113.10"
# 設定ファイル（conftest の configured）とテンプレートが指している AMI
OLD = "ami-0123456789abcdef0"
NEW = "ami-0fedcba9876543210"
SUBNET = "subnet-0123456789abcdef0"
VPC = "vpc-0123456789abcdef0"
GROUP = "sg-0123456789abcdef0"
# AMI のルートボリュームのデバイス名と、インスタンスのルートボリューム
ROOT_DEVICE = "/dev/sda1"
ROOT_VOLUME = "vol-0123456789abcdef0"
# ルートボリューム以外の EBS のデバイス名
DATA_DEVICE = "/dev/sdf"
# AMI の名前に入る時刻。2026-09-23 12:34（ローカル時刻）
AT = time.struct_time((2026, 9, 23, 12, 34, 0, 2, 266, -1))
IMAGE_NAME = "temt-dev-main-2609231234"
WHEN = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
# 差し替える前の、AMI の名前に使う時刻
LOCALTIME = bake.localtime

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
TEMPLATES = {"LaunchTemplateNames": [TEMPLATE]}
THE_TEMPLATE = {"LaunchTemplateName": TEMPLATE}
TAGS = [{"Key": "Name", "Value": IMAGE_NAME}, {"Key": "temt:service", "Value": "dev-main"}]
CREATE = {
    "InstanceId": INSTANCE,
    "Name": IMAGE_NAME,
    # ルートボリューム以外の EBS が無ければ、NoDevice にするものも無い
    "BlockDeviceMappings": [],
    "TagSpecifications": [
        {"ResourceType": "image", "Tags": TAGS},
        {"ResourceType": "snapshot", "Tags": TAGS},
    ],
}
THE_IMAGE = {"ImageIds": [NEW]}
PROMPT = f"bake {INSTANCE} (running)? [y/N] "
# 手順 3 で、設定ファイルの SubnetId と、その VPC にある temt のセキュリティグループを引く
THE_SUBNET = {"Filters": [{"Name": "subnet-id", "Values": [SUBNET]}]}
THE_GROUP = {
    "Filters": [
        {"Name": "group-name", "Values": [NAME]},
        {"Name": "vpc-id", "Values": [VPC]},
    ]
}
# temt が付けるタグ
MARKS = [{"Key": "Name", "Value": NAME}, {"Key": "temt:service", "Value": "dev-main"}]
# ImageId が無いか、指す AMI が使えないときの誤り。行を消す案内は apply に任せる
UNUSABLE_IMAGE = (
    f"the launch template {TEMPLATE} does not match ~/.config/temt/dev-main.toml in ImageId; "
    "run 'temt apply --svc dev-main' first"
)

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


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """AMI の名前に入る時刻を決める。"""
    monkeypatch.setattr(bake, "localtime", lambda: AT)


def applied(image_id: str = OLD, **changes: Any) -> dict[str, Any]:
    """設定ファイル（conftest の configured）を apply したテンプレートの中身。

    設定ファイルの [launch-template] に、temt が足す Groups とタグが入る。changes で項目を
    替える。
    """
    return {
        "ImageId": image_id,
        "InstanceType": "t3.micro",
        "InstanceInitiatedShutdownBehavior": "terminate",
        "IamInstanceProfile": {"Name": NAME},
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "SubnetId": SUBNET,
                "AssociatePublicIpAddress": True,
                "Groups": [GROUP],
            }
        ],
        "MetadataOptions": {"HttpTokens": "required", "InstanceMetadataTags": "enabled"},
        "CreditSpecification": {"CpuCredits": "unlimited"},
        "TagSpecifications": [
            {"ResourceType": kind, "Tags": MARKS}
            for kind in ("instance", "volume", "network-interface")
        ],
        **changes,
    }


def a_version(
    number: int, image_id: str, *, default: bool = True, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """describe-launch-template-versions が返すバージョン 1 つ分。data を省けば apply が
    作った形。"""
    return {
        "LaunchTemplateName": TEMPLATE,
        "VersionNumber": number,
        "DefaultVersion": default,
        "CreateTime": WHEN,
        "LaunchTemplateData": applied(image_id) if data is None else data,
    }


def an_image(image_id: str = OLD, **changes: Any) -> dict[str, Any]:
    """describe-images が返す、設定ファイルの ImageId の AMI。ルートボリュームは 8 GiB。"""
    return {
        "ImageId": image_id,
        "State": "available",
        "RootDeviceName": ROOT_DEVICE,
        "BlockDeviceMappings": [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 8}}],
        **changes,
    }


def found(stubs: Stubs, *items: dict[str, Any]) -> None:
    """手順 1 のテンプレートの確かめと、探索の結果を積む。テンプレートはある。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )
    stubs.ec2.add_response("describe_instances", reservations(*items), SEARCH)


def read_template(stubs: Stubs, *versions: dict[str, Any]) -> None:
    """手順 3 のうち、テンプレートの読み取りを積む。省けば apply したままの既定バージョン。"""
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": list(versions or (a_version(1, OLD),))},
        THE_TEMPLATE,
    )


def looked_up(stubs: Stubs, *images: dict[str, Any], params: dict[str, Any] | None = None) -> None:
    """設定ファイルの ImageId の AMI を引く describe-images を積む。

    ID で書かれていれば、その ID で引く。省けば、使える AMI が 1 つ見つかる。
    """
    stubs.ec2.add_response(
        "describe_images",
        {"Images": list(images or (an_image(),))},
        params or {"ImageIds": [OLD]},
    )


def networked(stubs: Stubs, *, group: str | None = GROUP, ipv6: bool = False) -> None:
    """設定ファイルの SubnetId の VPC と、そこにある temt のセキュリティグループを積む。

    group が None なら、temt のセキュリティグループはまだ無い。ipv6 なら、サブネットに
    IPv6 の CIDR が結び付いている。
    """
    subnet: dict[str, Any] = {"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "172.31.0.0/20"}
    if ipv6:
        subnet["Ipv6CidrBlockAssociationSet"] = [
            {"Ipv6CidrBlock": "2600:1f18::/64", "Ipv6CidrBlockState": {"State": "associated"}}
        ]
    stubs.ec2.add_response("describe_subnets", {"Subnets": [subnet]}, THE_SUBNET)
    groups = [] if group is None else [{"GroupId": group, "GroupName": NAME, "VpcId": VPC}]
    stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": groups}, THE_GROUP)


def checked(stubs: Stubs, *versions: dict[str, Any]) -> None:
    """手順 3 のテンプレートの確かめを積む。

    テンプレートを読み、設定ファイルから中身を組み立てる（ImageId の AMI、SubnetId の VPC、
    temt のセキュリティグループを引く）。
    """
    read_template(stubs, *versions)
    looked_up(stubs)
    networked(stubs)


def stopped(stubs: Stubs) -> None:
    """手順 5。停止して、stopped になるのを見る。"""
    stubs.ec2.add_response(
        "stop_instances",
        {"StoppingInstances": [{"InstanceId": INSTANCE, "CurrentState": {"Name": "stopping"}}]},
        BY_ID,
    )
    stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopping")), BY_ID
    )
    stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopped")), BY_ID
    )


def created(stubs: Stubs) -> None:
    """手順 6。AMI を作る。"""
    stubs.ec2.add_response("create_image", {"ImageId": NEW}, CREATE)


def pointing(stubs: Stubs, *older: int) -> int:
    """手順 8 のうち、テンプレートの新しいバージョンを既定にするまでを積む。

    既定バージョン（OLD を指す）と、それより古い older のバージョンがある。既定
    バージョンから、ImageId だけを NEW に替えたバージョンを作る。既定だった
    バージョンの番号を返す。
    """
    number = max(older, default=0) + 1
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                *(a_version(one, OLD, default=False) for one in older),
                a_version(number, OLD),
            ]
        },
        THE_TEMPLATE,
    )
    stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": number + 1}},
        {
            "LaunchTemplateName": TEMPLATE,
            "SourceVersion": str(number),
            "LaunchTemplateData": {"ImageId": NEW},
        },
    )
    stubs.ec2.add_response(
        "modify_launch_template",
        {},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": str(number + 1)},
    )
    return number


def pointed(stubs: Stubs, *older: int) -> None:
    """手順 8 のテンプレート。新しいバージョンを既定にし、作る前に読んだものを消す。"""
    number = pointing(stubs, *older)
    dropped = [*older, number]
    stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": one} for one in dropped]},
        {"LaunchTemplateName": TEMPLATE, "Versions": [str(one) for one in dropped]},
    )


def image_state(stubs: Stubs, state: str, **more: Any) -> None:
    """手順 7 の問い合わせの応答を 1 つ積む。"""
    stubs.ec2.add_response(
        "describe_images", {"Images": [{"ImageId": NEW, "State": state, **more}]}, THE_IMAGE
    )


def read_records(stubs: Stubs, *parameters: dict[str, Any]) -> None:
    """手順 8。焼いたことを書くために、AMI が available になってから deploy の記録を読む。"""
    stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": list(parameters)},
        {"Path": f"/{NAME}", "Recursive": True},
    )


def written_record(stubs: Stubs, app_id: str, record: dict[str, str]) -> None:
    """記録を 1 つ書く。"""
    stubs.ssm.add_response(
        "put_parameter",
        {"Version": 2},
        {
            "Name": f"/{NAME}/{app_id}/deploy",
            "Value": json.dumps(record),
            "Type": "String",
            "Overwrite": True,
        },
    )


# deploy の記録の項目のうち、焼いても変わらないもの
RECORD = {"name": "J", "version": "2", "user": "root", "deployed": "2026-09-26T03:00:00Z"}
# このインスタンスに入れて、まだ焼いていない記録
UNBAKED = {**RECORD, "instance": INSTANCE}


@pytest.fixture
def unmarked(monkeypatch: pytest.MonkeyPatch) -> None:
    """記録に触れないことを確かめる。mark_baked を呼べばテストが落ちる。

    Stubber の積んでいない呼び出しの誤りは、記録が読めないときの警告に紛れるので、
    mark_baked そのものを差し替える。
    """

    def touched(*args: Any) -> None:
        pytest.fail("mark_baked was called")

    monkeypatch.setattr(app, "mark_baked", touched)


def terminated(stubs: Stubs) -> None:
    """手順 9。終了し、terminated になるのを見る。"""
    stubs.ec2.add_response(
        "terminate_instances",
        {
            "TerminatingInstances": [
                {"InstanceId": INSTANCE, "CurrentState": {"Name": "shutting-down"}}
            ]
        },
        BY_ID,
    )
    stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )


def image_id(service: Service) -> str | None:
    """設定ファイルの ImageId。"""
    return conf.load(service).launch_template.get("ImageId")


def test_it_bakes_a_running_instance(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    known = home.prepare_known_hosts(configured) / INSTANCE
    known.touch()
    found(aws_stubs, an_instance(INSTANCE, "running", address=ADDRESS))
    checked(aws_stubs)
    stopped(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "pending")
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    read_records(aws_stubs)
    terminated(aws_stubs)

    bake.run(configured, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    assert image_id(configured) == IMAGE_NAME
    assert not known.exists()
    captured = capsys.readouterr()
    # 問いかけだけを標準エラー出力に書く
    assert captured.err == PROMPT
    listing = (
        f"bake {NAME}:\n"
        f"  stop {INSTANCE}\n"
        f"  make an AMI of {INSTANCE}\n"
        "  point ImageId in ~/.config/temt/dev-main.toml and the launch template at the AMI "
        "when it is available\n"
        f"  terminate {INSTANCE}\n"
    )
    assert captured.out.startswith(listing)
    lines = told(captured.out.removeprefix(listing))
    assert f"creating the AMI {NEW} ({IMAGE_NAME}) from {INSTANCE}" in lines
    assert (
        f"wrote launch-template.ImageId = {IMAGE_NAME} in {tilde(configured.conf_path)} (was {OLD})"
        in lines
    )
    assert f"launch template {TEMPLATE}: updated (version 2, {NEW})" in lines
    # 設定ファイルを書き換えるのは、AMI が使えるようになってから
    assert lines.index(f"the AMI {NEW} is available") < lines.index(
        f"wrote launch-template.ImageId = {IMAGE_NAME} in {tilde(configured.conf_path)} (was {OLD})"
    )
    assert lines[-1] == f"baked {IMAGE_NAME} ({NEW}); the next start boots from it"
    # 停止を 1 回、AMI を 1 回待った
    assert clock.slept == [5, 5]


def test_it_bakes_a_stopped_instance_without_stopping_it(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    read_records(aws_stubs)
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()
    assert image_id(configured) == IMAGE_NAME
    assert "  stop " not in capsys.readouterr().out


def test_it_leaves_the_other_volumes_out_of_the_ami(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ルートボリューム以外の EBS は、NoDevice にして AMI に入れない。

    終了するときも、DeleteOnTermination を true にするのはルートボリュームだけで、ほかの
    EBS には触れない。インスタンスの中の側が付けるもので、インスタンスより長く使う。
    """
    instance = {
        **an_instance(INSTANCE, "stopped"),
        "RootDeviceName": ROOT_DEVICE,
        "BlockDeviceMappings": [
            {
                "DeviceName": ROOT_DEVICE,
                "Ebs": {"VolumeId": ROOT_VOLUME, "DeleteOnTermination": False},
            },
            {
                "DeviceName": DATA_DEVICE,
                "Ebs": {"VolumeId": "vol-00000000000000001", "DeleteOnTermination": False},
            },
            {
                "DeviceName": "/dev/sdg",
                "Ebs": {"VolumeId": "vol-00000000000000002", "DeleteOnTermination": True},
            },
        ],
    }
    found(aws_stubs, instance)
    # Ebs.VolumeSize が無いので、ルートボリュームの大きさは読まない
    checked(aws_stubs)
    aws_stubs.ec2.add_response(
        "create_image",
        {"ImageId": NEW},
        {
            **CREATE,
            "BlockDeviceMappings": [
                {"DeviceName": DATA_DEVICE, "NoDevice": ""},
                {"DeviceName": "/dev/sdg", "NoDevice": ""},
            ],
        },
    )
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    read_records(aws_stubs)
    aws_stubs.ec2.add_response(
        "modify_instance_attribute",
        {},
        {
            "InstanceId": INSTANCE,
            "BlockDeviceMappings": [
                {"DeviceName": ROOT_DEVICE, "Ebs": {"DeleteOnTermination": True}}
            ],
        },
    )
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()
    lines = told(capsys.readouterr().out)
    assert f"{INSTANCE}: {ROOT_DEVICE} will be deleted with the instance" in lines
    assert not [line for line in lines if DATA_DEVICE in line]


def test_it_keeps_the_rest_of_the_config_file(
    aws_stubs: Stubs, configured: Service, answer: Terminal
) -> None:
    _ = answer
    text = configured.conf_path.read_text(encoding="utf-8")
    configured.conf_path.write_text("# mine\n" + text, encoding="utf-8")
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    read_records(aws_stubs)
    terminated(aws_stubs)

    bake.run(configured, yes=False)

    assert configured.conf_path.read_text(encoding="utf-8") == "# mine\n" + text.replace(
        OLD, IMAGE_NAME
    )


def test_it_does_nothing_when_the_answer_is_no(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    found(aws_stubs, an_instance(INSTANCE, "running", address=ADDRESS))
    checked(aws_stubs)

    with pytest.raises(TemtError, match="cancelled"):
        bake.run(configured, yes=False)

    aws_stubs.done()
    assert image_id(configured) == OLD


def test_it_stops_when_there_is_no_instance(aws_stubs: Stubs, configured: Service) -> None:
    found(aws_stubs)

    with pytest.raises(TemtError, match=f"no instance for {NAME} to bake"):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_stops_when_more_than_one_instance_is_tagged(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "running"), an_instance(OTHER, "stopped"))

    with pytest.raises(
        TemtError,
        match=(
            f"2 instances for {NAME}; terminate the extra one with "
            "'temt terminate --svc dev-main <instance-id>' and bake again"
        ),
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()
    err = capsys.readouterr().err
    assert f"  {INSTANCE} (running)\n" in err
    assert f"  {OTHER} (stopped)\n" in err


def test_it_treats_an_instance_that_is_shutting_down_as_gone(
    aws_stubs: Stubs, configured: Service
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "shutting-down"))

    with pytest.raises(TemtError, match=f"no instance for {NAME} to bake"):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_does_not_count_an_instance_that_is_shutting_down(
    aws_stubs: Stubs, configured: Service
) -> None:
    """終了の途中のものは 2 台と数えず、running の方を焼こうと先へ進む。"""
    _ = conf.update(
        configured, conf.load(configured), conf.launch_template_key("InstanceType"), "t3.small"
    )
    found(aws_stubs, an_instance(OTHER, "shutting-down"), an_instance(INSTANCE, "running"))
    checked(aws_stubs)

    # 手順 3 まで進んだことを、テンプレートとの食い違いで確かめる
    with pytest.raises(TemtError, match="does not match"):
        bake.run(configured, yes=True)

    aws_stubs.done()


@pytest.mark.parametrize("state", ["pending", "stopping"])
def test_it_stops_while_the_instance_is_changing(
    aws_stubs: Stubs, configured: Service, state: str
) -> None:
    found(aws_stubs, an_instance(INSTANCE, state))

    with pytest.raises(TemtError, match=f"{INSTANCE} is {state}; wait for it to finish"):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_asks_for_apply_when_the_config_file_differs(
    aws_stubs: Stubs, configured: Service
) -> None:
    _ = conf.update(
        configured, conf.load(configured), conf.launch_template_key("InstanceType"), "t3.small"
    )
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs)

    with pytest.raises(
        TemtError,
        match=(
            f"the launch template {TEMPLATE} does not match ~/.config/temt/dev-main.toml "
            "in InstanceType; run 'temt apply --svc dev-main' first"
        ),
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_lists_every_position_that_differs(aws_stubs: Stubs, configured: Service) -> None:
    """テンプレートの中身の全体を比べ、違う項目の位置をすべて挙げる。

    設定ファイルにある項目が先で、テンプレートにだけある項目が後。
    """
    _ = conf.update(
        configured, conf.load(configured), conf.launch_template_key("InstanceType"), "t3.small"
    )
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs, a_version(1, OLD, data=applied(KeyName="mine")))

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the launch template {TEMPLATE} does not match ~/.config/temt/dev-main.toml "
        "in InstanceType, KeyName; run 'temt apply --svc dev-main' first"
    )


def test_it_asks_for_apply_when_an_item_is_not_applied(
    aws_stubs: Stubs, configured: Service
) -> None:
    """設定ファイルに足した項目がテンプレートに無ければ、揃っていない。位置は要素の中まで書く。"""
    text = configured.conf_path.read_text(encoding="utf-8")
    configured.conf_path.write_text(
        text.replace(
            "AssociatePublicIpAddress = true\n",
            "AssociatePublicIpAddress = true\nIpv6AddressCount = 1\n",
        ),
        encoding="utf-8",
    )
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs)

    with pytest.raises(
        TemtError,
        match=re.escape(
            "in NetworkInterfaces[DeviceIndex=0].Ipv6AddressCount; "
            "run 'temt apply --svc dev-main' first"
        ),
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_asks_for_apply_when_a_filled_item_is_removed(
    aws_stubs: Stubs, configured: Service
) -> None:
    """apply が埋める項目の行を消して、まだ apply していなければ、テンプレートにだけ残る
    ので揃っていない。"""
    text = configured.conf_path.read_text(encoding="utf-8")
    configured.conf_path.write_text(
        text.replace('InstanceType = "t3.micro"\n', ""), encoding="utf-8"
    )
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs)

    with pytest.raises(
        TemtError, match=re.escape("in InstanceType; run 'temt apply --svc dev-main' first")
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_asks_for_apply_when_an_item_apply_fills_is_not_written(
    aws_stubs: Stubs, configured: Service
) -> None:
    """テンプレートが設定ファイルと揃っていても、apply が書く項目がまだ無ければ、次の apply
    が書いて中身を変えうるので揃っていない。サブネットに IPv6 があれば Ipv6AddressCount
    も書く。"""
    found(aws_stubs, an_instance(INSTANCE, "running"))
    read_template(aws_stubs)
    looked_up(aws_stubs)
    networked(aws_stubs, ipv6=True)

    with pytest.raises(
        TemtError,
        match=re.escape(
            "in NetworkInterfaces[DeviceIndex=0].Ipv6AddressCount; "
            "run 'temt apply --svc dev-main' first"
        ),
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_names_a_tag_by_its_whole_key(aws_stubs: Stubs, configured: Service) -> None:
    """タグの位置は Tags[Key=<キー>] で、temt:service のように : を含むキーも切らずに書く。

    設定ファイルに足したタグがまだ無く、テンプレートの temt:service が別のサービスを
    指している（他のサービスのテンプレートを写したときなど）。
    """
    with configured.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write(
            "\n[[launch-template.TagSpecifications]]\n"
            'ResourceType = "instance"\n'
            'Tags = [{ Key = "Env", Value = "dev" }]\n'
        )
    other = [{"Key": "Name", "Value": NAME}, {"Key": "temt:service", "Value": "dev-other"}]
    tags = [
        {"ResourceType": kind, "Tags": other if kind == "instance" else MARKS}
        for kind in ("instance", "volume", "network-interface")
    ]
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs, a_version(1, OLD, data=applied(TagSpecifications=tags)))

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the launch template {TEMPLATE} does not match ~/.config/temt/dev-main.toml in "
        "TagSpecifications[ResourceType=instance].Tags[Key=Env], "
        "TagSpecifications[ResourceType=instance].Tags[Key=temt:service]; "
        "run 'temt apply --svc dev-main' first"
    )


def test_it_asks_for_apply_when_only_the_whole_content_differs(
    aws_stubs: Stubs, configured: Service
) -> None:
    """位置に開いて違いが見えなくても、中身の全体が違えば揃っていない。

    テンプレートに同じ ResourceType の要素が 2 つあると、位置は 1 つに重なる。位置を
    挙げられないので、中身の全体が違うと告げる。
    """
    tags = [
        {"ResourceType": kind, "Tags": MARKS}
        for kind in ("instance", "instance", "volume", "network-interface")
    ]
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs, a_version(1, OLD, data=applied(TagSpecifications=tags)))

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the launch template {TEMPLATE} does not match ~/.config/temt/dev-main.toml in "
        "the content; run 'temt apply --svc dev-main' first"
    )


def test_it_asks_for_apply_when_the_security_group_is_not_made(
    aws_stubs: Stubs, configured: Service
) -> None:
    """temt のセキュリティグループが SubnetId の VPC にまだ無ければ、Groups が揃っていない。

    SubnetId を別の VPC のサブネットに替えて、まだ apply していないときに当たる。
    """
    found(aws_stubs, an_instance(INSTANCE, "running"))
    read_template(aws_stubs)
    looked_up(aws_stubs)
    networked(aws_stubs, group=None)

    with pytest.raises(
        TemtError,
        match=re.escape(
            "in NetworkInterfaces[DeviceIndex=0].Groups; run 'temt apply --svc dev-main' first"
        ),
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_compares_the_template_as_aws_reads_it_back(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AWS が読み出しで変える形は揃えて比べる。

    IamInstanceProfile は ARN で返り、読み出したときにだけある項目が付き、配列の並びも
    変わる。揃っているので、確認まで進む。
    """
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    read_back = applied(
        IamInstanceProfile={"Arn": f"arn:aws:iam::123456789012:instance-profile/{NAME}"},
        MetadataOptions={
            "HttpTokens": "required",
            "InstanceMetadataTags": "enabled",
            "State": "applied",
        },
        TagSpecifications=[
            {"ResourceType": kind, "Tags": list(reversed(MARKS))}
            for kind in ("volume", "network-interface", "instance")
        ],
    )
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs, a_version(1, OLD, data=read_back))

    with pytest.raises(TemtError, match="cancelled"):
        bake.run(configured, yes=False)

    aws_stubs.done()


def test_it_compares_with_the_values_over_the_common_file(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """設定ファイルを共通ファイルの上に重ねたものと比べる。共通ファイルの項目も揃って
    いなければならない。"""
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    defaults_path().write_text('[launch-template]\nKeyName = "mine"\n', encoding="utf-8")
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs, a_version(1, OLD, data=applied(KeyName="mine")))

    with pytest.raises(TemtError, match="cancelled"):
        bake.run(configured, yes=False)

    aws_stubs.done()


def sized_conf(service: Service, size: int, *, device: str | None = None) -> None:
    """設定ファイルに、ルートボリュームの要素の Ebs.VolumeSize を書く。

    device を省けば DeviceName を書かず、temt が AMI の RootDeviceName で埋める。
    """
    named = "" if device is None else f'DeviceName = "{device}"\n'
    with service.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write(
            f"\n[[launch-template.BlockDeviceMappings]]\n{named}Ebs = {{ VolumeSize = {size} }}\n"
        )


def sized_version(size: int) -> dict[str, Any]:
    """ルートボリュームの Ebs.VolumeSize を入れて apply したテンプレートの既定バージョン。"""
    mappings = [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": size}}]
    return a_version(1, OLD, data=applied(BlockDeviceMappings=mappings))


def with_root(state: str) -> dict[str, Any]:
    """ルートボリュームの分かるインスタンス 1 台分。"""
    return {
        **an_instance(INSTANCE, state),
        "RootDeviceName": ROOT_DEVICE,
        "BlockDeviceMappings": [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeId": ROOT_VOLUME}}],
    }


def root_size(stubs: Stubs, size: int) -> None:
    """手順 3 のルートボリュームの大きさの読み取りを積む。

    ルートボリュームの要素は、テンプレートの確かめで組み立てた中身から決めるので、AMI を
    もう一度は引かない。
    """
    stubs.ec2.add_response(
        "describe_volumes",
        {"Volumes": [{"VolumeId": ROOT_VOLUME, "Size": size}]},
        {"VolumeIds": [ROOT_VOLUME]},
    )


ROOT_SIZE_KEY = "launch-template.BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize"


@pytest.mark.parametrize("device", [None, ROOT_DEVICE])
def test_it_does_not_bake_a_root_volume_larger_than_the_volume_size(
    aws_stubs: Stubs, configured: Service, device: str | None
) -> None:
    """焼いた AMI は Ebs.VolumeSize のテンプレートから起動できなくなるので、焼かずに終わる。

    DeviceName を省いても、AMI のルートボリュームのデバイス名を書いても、ルートボリュームの
    要素とする。
    """
    sized_conf(configured, 20, device=device)
    found(aws_stubs, with_root("running"))
    checked(aws_stubs, sized_version(20))
    root_size(aws_stubs, 30)

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the root volume of {INSTANCE} is 30 GiB, larger than {ROOT_SIZE_KEY} 20; "
        "raise it to 30 or more or remove it in ~/.config/temt/dev-main.toml, "
        "then run 'temt apply --svc dev-main' and bake again"
    )
    assert image_id(configured) == OLD


def test_it_names_the_common_file_for_the_volume_size(
    aws_stubs: Stubs, configured: Service
) -> None:
    """Ebs.VolumeSize を共通ファイルに書いていれば、共通ファイルを直すよう告げる。"""
    defaults_path().write_text(
        "[[launch-template.BlockDeviceMappings]]\nEbs = { VolumeSize = 20 }\n", encoding="utf-8"
    )
    found(aws_stubs, with_root("running"))
    checked(aws_stubs, sized_version(20))
    root_size(aws_stubs, 30)

    with pytest.raises(TemtError, match="remove it in ~/.config/temt/defaults.toml, then run"):
        bake.run(configured, yes=True)

    aws_stubs.done()


@pytest.mark.parametrize("size", [20, 30])
def test_it_bakes_a_root_volume_up_to_the_volume_size(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """ルートボリュームが Ebs.VolumeSize 以下なら、確認まで進む。"""
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    sized_conf(configured, 30)
    found(aws_stubs, with_root("running"))
    checked(aws_stubs, sized_version(30))
    root_size(aws_stubs, size)

    with pytest.raises(TemtError, match="cancelled"):
        bake.run(configured, yes=False)

    aws_stubs.done()


@pytest.mark.parametrize("answer", ["no volume id", "no volume", "no size"])
def test_it_bakes_when_the_root_volume_size_is_unknown(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """ルートボリュームの大きさが分からなければ、比べられないので止めない。"""
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    sized_conf(configured, 30)
    if answer == "no volume id":
        found(aws_stubs, an_instance(INSTANCE, "running"))
    else:
        found(aws_stubs, with_root("running"))
    checked(aws_stubs, sized_version(30))
    if answer != "no volume id":
        volumes = [] if answer == "no volume" else [{"VolumeId": ROOT_VOLUME}]
        aws_stubs.ec2.add_response(
            "describe_volumes", {"Volumes": volumes}, {"VolumeIds": [ROOT_VOLUME]}
        )

    with pytest.raises(TemtError, match="cancelled"):
        bake.run(configured, yes=False)

    aws_stubs.done()


def test_it_asks_for_apply_when_a_mapping_without_a_size_is_added(
    aws_stubs: Stubs, configured: Service
) -> None:
    """設定ファイルに無い BlockDeviceMappings がテンプレートにあれば、揃っていない。"""
    mappings = [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeType": "gp3"}}]
    found(aws_stubs, with_root("running"))
    checked(aws_stubs, a_version(1, OLD, data=applied(BlockDeviceMappings=mappings)))

    with pytest.raises(
        TemtError,
        match=re.escape(
            "in BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeType; "
            "run 'temt apply --svc dev-main' first"
        ),
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_does_not_look_at_the_root_volume_without_a_volume_size(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ebs.VolumeSize が無ければ、焼いた AMI の大きさで起動するので、比べない。"""
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    found(aws_stubs, with_root("running"))
    checked(aws_stubs)

    with pytest.raises(TemtError, match="cancelled"):
        bake.run(configured, yes=False)

    # describe-volumes は積んでいないので、読めば Stubber が失敗する
    aws_stubs.done()


def test_it_does_not_take_another_device_for_the_root_volume(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DeviceName が AMI の RootDeviceName でない要素は、ルートボリュームでないので比べない。

    その要素の Ebs.VolumeSize がインスタンスのルートボリュームより小さくても止めない。
    """
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    sized_conf(configured, 4, device=DATA_DEVICE)
    mappings = [{"DeviceName": DATA_DEVICE, "Ebs": {"VolumeSize": 4}}]
    found(aws_stubs, with_root("running"))
    checked(aws_stubs, a_version(1, OLD, data=applied(BlockDeviceMappings=mappings)))

    with pytest.raises(TemtError, match="cancelled"):
        bake.run(configured, yes=False)

    # describe-volumes は積んでいないので、読めば Stubber が失敗する
    aws_stubs.done()


def test_it_asks_for_apply_when_the_volume_size_is_not_applied(
    aws_stubs: Stubs, configured: Service
) -> None:
    sized_conf(configured, 30)
    found(aws_stubs, with_root("running"))
    checked(aws_stubs)

    with pytest.raises(
        TemtError,
        match=re.escape(
            "in BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize; "
            "run 'temt apply --svc dev-main' first"
        ),
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_asks_for_apply_when_the_volume_size_is_removed(
    aws_stubs: Stubs, configured: Service
) -> None:
    found(aws_stubs, with_root("running"))
    checked(aws_stubs, sized_version(30))

    with pytest.raises(
        TemtError,
        match=re.escape(
            "in BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize; "
            "run 'temt apply --svc dev-main' first"
        ),
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_asks_for_apply_when_the_image_id_is_not_written(
    aws_stubs: Stubs, configured: Service
) -> None:
    """ImageId が無ければ、組み立てられないので、AWS で AMI を引かずに、ImageId が揃って
    いないとして apply を促す。"""
    text = configured.conf_path.read_text(encoding="utf-8")
    configured.conf_path.write_text(text.replace(OLD, ""), encoding="utf-8")
    found(aws_stubs, an_instance(INSTANCE, "running"))
    read_template(aws_stubs)

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value) == UNUSABLE_IMAGE


def test_it_asks_for_apply_before_looking_for_instances(
    aws_stubs: Stubs, configured: Service
) -> None:
    """テンプレートはロックの直後に確かめるので、無ければインスタンスを探しもしない。"""
    aws_stubs.ec2.add_client_error(
        "describe_launch_templates", resources.NO_SUCH_TEMPLATE, expected_params=TEMPLATES
    )

    with pytest.raises(
        TemtError, match=f"no launch template {TEMPLATE}; run 'temt apply --svc dev-main' first"
    ):
        bake.run(configured, yes=True)

    # describe-instances は積んでいないので、探せば Stubber が失敗する
    aws_stubs.done()


def test_it_asks_for_apply_when_the_launch_template_is_gone(
    aws_stubs: Stubs, configured: Service
) -> None:
    """確かめた後で消えていても、バージョンの読み取りの誤りから apply を促す。"""
    found(aws_stubs, an_instance(INSTANCE, "running"))
    aws_stubs.ec2.add_client_error(
        "describe_launch_template_versions",
        "InvalidLaunchTemplateName.NotFoundException",
        expected_params=THE_TEMPLATE,
    )

    with pytest.raises(TemtError, match=f"no launch template {TEMPLATE}; run 'temt apply"):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_tells_to_wait_a_minute_when_the_name_is_taken(
    aws_stubs: Stubs, configured: Service
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "create_image", "InvalidAMIName.Duplicate", expected_params=CREATE
    )

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"an AMI named {IMAGE_NAME} already exists; wait a minute and run bake again"
    )
    assert image_id(configured) == OLD


def test_create_takes_the_name_the_mappings_and_the_command_to_run_again(
    aws_stubs: Stubs, configured: Service, ec2: EC2Client
) -> None:
    """ami import は、確認に出した名前と、足す BlockDeviceMappings を渡し、同じ名前の AMI が
    あれば、やり直すコマンドとして自分を告げさせる。

    ルートボリューム以外の EBS の NoDevice に続けて、足す指定を並べる。
    """
    name = "temt-dev-main-2609231200"
    tags = [{"Key": "Name", "Value": name}, {"Key": "temt:service", "Value": "dev-main"}]
    mappings: list[BlockDeviceMappingTypeDef] = [
        {"DeviceName": ROOT_DEVICE, "Ebs": {"DeleteOnTermination": True}}
    ]
    aws_stubs.ec2.add_client_error(
        "create_image",
        "InvalidAMIName.Duplicate",
        expected_params={
            "InstanceId": INSTANCE,
            "Name": name,
            "BlockDeviceMappings": [{"DeviceName": DATA_DEVICE, "NoDevice": ""}, *mappings],
            "TagSpecifications": [
                {"ResourceType": "image", "Tags": tags},
                {"ResourceType": "snapshot", "Tags": tags},
            ],
        },
    )

    with pytest.raises(TemtError) as caught:
        _ = bake.create(
            ec2, configured, INSTANCE, [DATA_DEVICE], mappings, name, command="ami import"
        )

    aws_stubs.done()
    assert str(caught.value) == (
        f"an AMI named {name} already exists; wait a minute and run ami import again"
    )


def test_it_changes_nothing_when_the_image_fails(
    aws_stubs: Stubs, configured: Service, unmarked: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """作れなかったら、何も向けずに今の状態を知らせ、AWS の理由でエラーにする。

    設定ファイル、テンプレート、記録は元のままで、インスタンスは停止したまま残る。
    テンプレートへの書き込みは積んでいないので、書けば Stubber が失敗する。
    """
    _ = unmarked
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "failed", StateReason={"Code": "x", "Message": "no space"})

    with pytest.raises(TemtError, match=f"^the AMI {NEW} is failed: no space$"):
        bake.run(configured, yes=True)

    # インスタンスを終了するものは積んでいないので、終了しようとすれば Stubber が失敗する
    aws_stubs.done()
    assert image_id(configured) == OLD
    # 続くエラーに添えるので、標準エラー出力に出す
    assert capsys.readouterr().err == (
        "bake has not changed ~/.config/temt/dev-main.toml, the launch template "
        f"or the deploy records; {INSTANCE} is left stopped\n"
    )


def test_it_waits_while_the_new_image_is_not_visible_yet(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "describe_images", "InvalidAMIID.NotFound", expected_params=THE_IMAGE
    )
    aws_stubs.ec2.add_response("describe_images", {"Images": []}, THE_IMAGE)
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    read_records(aws_stubs)
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()
    assert clock.slept == [5, 5]
    assert image_id(configured) == IMAGE_NAME


def test_it_gives_up_on_an_image_it_never_sees(
    aws_stubs: Stubs,
    configured: Service,
    clock: Clock,
    unmarked: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """作った AMI が見えないまま 5 分たてば、消えたものとして、作れなかったときと同じく終わる。"""
    _ = unmarked
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    # 5 秒おきに 5 分。60 回目の後で見切る
    for _ in range(bake.UNSEEN_DEADLINE // 5 + 1):
        aws_stubs.ec2.add_response("describe_images", {"Images": []}, THE_IMAGE)

    with pytest.raises(TemtError, match=f"^the AMI {NEW} is not found$"):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert clock.now == bake.UNSEEN_DEADLINE
    assert image_id(configured) == OLD
    assert capsys.readouterr().err.endswith(f"{INSTANCE} is left stopped\n")


def test_an_aws_error_while_waiting_tells_what_is_left(
    aws_stubs: Stubs, configured: Service, unmarked: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """待つあいだに AWS の誤り（資格情報の期限切れなど）が出たら、今の状態を知らせて
    その誤りをそのまま上げる。何も向けず、インスタンスも終了しない。"""
    _ = unmarked
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "pending")
    aws_stubs.ec2.add_client_error(
        "describe_images", "ExpiredToken", http_status_code=400, expected_params=THE_IMAGE
    )

    with pytest.raises(ClientError, match="ExpiredToken"):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert image_id(configured) == OLD
    assert capsys.readouterr().err == (
        "bake has not changed ~/.config/temt/dev-main.toml, the launch template "
        f"or the deploy records; {INSTANCE} is left stopped\n"
        f"the AMI {NEW} is not available yet\n"
    )


def test_it_gives_up_on_an_image_that_is_deregistered_while_it_waits(
    aws_stubs: Stubs, configured: Service, unmarked: None
) -> None:
    """いったん pending で見えた AMI が登録解除されたら、待ち続けずに作れなかったとする。"""
    _ = unmarked
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "pending")
    image_state(aws_stubs, "deregistered")

    with pytest.raises(TemtError, match=f"^the AMI {NEW} is deregistered$"):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert image_id(configured) == OLD


def test_it_waits_as_long_as_the_image_is_pending(
    aws_stubs: Stubs, configured: Service, clock: Clock
) -> None:
    """AMI を待つ締め切りは無い。pending のあいだは、何時間でも待ち続ける。"""
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    # 5 秒おきに 2 時間
    for _ in range(2 * 60 * 60 // 5):
        image_state(aws_stubs, "pending")
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    read_records(aws_stubs)
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()
    assert clock.now == 2 * 60 * 60
    assert image_id(configured) == IMAGE_NAME


def test_an_interrupted_wait_changes_nothing(
    aws_stubs: Stubs, configured: Service, unmarked: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """待つあいだに止めれば、設定ファイル、テンプレート、記録は元のまま。"""
    _ = unmarked

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(wait, "sleep", interrupt)
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "pending")

    with pytest.raises(KeyboardInterrupt):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert image_id(configured) == OLD


def test_it_stops_when_the_instance_goes_away_while_it_stops(
    aws_stubs: Stubs, configured: Service
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs)
    aws_stubs.ec2.add_response(
        "stop_instances",
        {"StoppingInstances": [{"InstanceId": INSTANCE, "CurrentState": {"Name": "stopping"}}]},
        BY_ID,
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "shutting-down")), BY_ID
    )

    with pytest.raises(
        TemtError, match=f"{INSTANCE} went to shutting-down while waiting to be stopped"
    ):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert image_id(configured) == OLD


def test_it_stops_when_aws_refuses_to_stop_the_instance(
    aws_stubs: Stubs, configured: Service
) -> None:
    """確認を待つ間に止まっていれば、AWS が断る。status を見てやり直すよう添える。"""
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "stop_instances",
        service_error_code="IncorrectInstanceState",
        service_message="This instance is not in a state from which it can be stopped.",
        expected_params=BY_ID,
    )

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value).endswith("; see 'temt status --svc dev-main' and bake again")
    assert image_id(configured) == OLD


def test_the_deadlines_are_the_ones_in_the_design() -> None:
    from temt import instances

    assert instances.STOPPED_DEADLINE == 600
    assert bake.UNSEEN_DEADLINE == 300


def test_the_image_name_uses_the_local_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """AMI の名前の時刻は、手元のローカル時刻（テストでは UTC+9）で書く。

    UTC とは 9 時間ずれるので、UTC で書けば落ちる。分の変わり目をまたいでもよいよう、
    前後の時刻の両方を受け付ける。
    """
    monkeypatch.setattr(bake, "localtime", LOCALTIME)
    before = time.strftime(bake.NAME_TIME, time.localtime())
    name = bake.image_name("dev-main")
    after = time.strftime(bake.NAME_TIME, time.localtime())
    assert name in (f"temt-dev-main-{before}", f"temt-dev-main-{after}")


def test_it_stops_when_another_command_holds_the_lock(
    aws_stubs: Stubs, configured: Service
) -> None:
    """ロックはテンプレートを確かめるより先に取る。取れなければ待たずに終わる。"""
    with (
        lock.hold(configured),
        deadline(5.0),
        pytest.raises(TemtError, match=f"another temt command is running for {NAME}"),
    ):
        bake.run(configured, yes=True)
    # 応答を 1 つも積んでいないので、AWS を読んでいれば Stubber が失敗する
    aws_stubs.done()
    assert taken(configured)


def test_it_holds_the_lock_while_it_waits(
    aws_stubs: Stubs, configured: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止と AMI を待つ間もロックを持っている。別のコマンドは入ってこない。"""
    attempts: list[bool] = []

    def probe(seconds: float) -> None:
        clock.sleep(seconds)
        attempts.append(taken(configured))

    monkeypatch.setattr(wait, "sleep", probe)
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs)
    stopped(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "pending")
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    read_records(aws_stubs)
    terminated(aws_stubs)

    with deadline(5.0):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert attempts == [False, False]
    # 終わればロックは離す
    assert taken(configured)


def test_main_runs_bake(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    found(aws_stubs)

    assert cli.main(["temt", "bake", "--svc", "dev-main", "--yes"]) == 1

    aws_stubs.done()
    assert capsys.readouterr().err == f"temt: no instance for {NAME} to bake\n"


def test_it_asks_for_apply_when_the_image_id_differs(aws_stubs: Stubs, configured: Service) -> None:
    found(aws_stubs, an_instance(INSTANCE, "running"))
    checked(aws_stubs, a_version(1, NEW))

    with pytest.raises(TemtError, match="in ImageId; run 'temt apply --svc dev-main' first"):
        bake.run(configured, yes=True)

    aws_stubs.done()


def test_it_leaves_the_config_file_alone_when_the_instance_cannot_be_stopped(
    aws_stubs: Stubs, configured: Service
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "running", address=ADDRESS))
    checked(aws_stubs)
    aws_stubs.ec2.add_client_error("stop_instances", "UnsupportedOperation", expected_params=BY_ID)

    with pytest.raises(ClientError):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert image_id(configured) == OLD


def test_the_config_file_points_at_the_new_image_before_the_template(
    aws_stubs: Stubs, configured: Service
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    aws_stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [a_version(1, OLD)]},
        THE_TEMPLATE,
    )
    aws_stubs.ec2.add_client_error(
        "create_launch_template_version",
        "RequestLimitExceeded",
        expected_params={
            "LaunchTemplateName": TEMPLATE,
            "SourceVersion": "1",
            "LaunchTemplateData": {"ImageId": NEW},
        },
    )

    # テンプレートで止まれば、設定ファイルは新しい AMI を指したまま
    with pytest.raises(ClientError):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert image_id(configured) == IMAGE_NAME


def test_it_does_not_make_a_config_file_that_went_away(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    create = bake.create

    def create_then_lose(*args: Any) -> tuple[str, str]:
        made = create(*args)
        configured.conf_path.unlink()
        return made

    monkeypatch.setattr(bake, "create", create_then_lose)

    with pytest.raises(TemtError, match="no config file: "):
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert not configured.conf_path.exists()


OLD_NAME = "temt-dev-main-2609201800"
BY_OLD_NAME = {"Owners": ["self"], "Filters": [{"Name": "name", "Values": [OLD_NAME]}]}


def named(service: Service) -> None:
    """設定ファイルの ImageId を、OLD の名前にする。"""
    _ = conf.update(service, conf.load(service), conf.launch_template_key("ImageId"), OLD_NAME)


def test_it_asks_for_apply_when_the_image_name_is_gone(
    aws_stubs: Stubs, configured: Service
) -> None:
    """ImageId の名前の AMI が無ければ、ImageId が揃っていないものとして apply を促す。

    行を消すよう案内するのは apply なので、bake は同じ案内を重ねない。
    """
    named(configured)
    found(aws_stubs, an_instance(INSTANCE, "running"))
    read_template(aws_stubs)
    aws_stubs.ec2.add_response("describe_images", {"Images": []}, BY_OLD_NAME)

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value) == UNUSABLE_IMAGE


def test_it_bakes_with_an_image_name_in_the_config_file(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    named(configured)
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    # 名前を ID に直して、テンプレートの ImageId と比べる
    read_template(aws_stubs)
    looked_up(aws_stubs, an_image(Name=OLD_NAME), params=BY_OLD_NAME)
    networked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    read_records(aws_stubs)
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()
    assert image_id(configured) == IMAGE_NAME
    assert (
        f"wrote launch-template.ImageId = {IMAGE_NAME} in {tilde(configured.conf_path)} "
        f"(was {OLD_NAME})" in told(capsys.readouterr().out)
    )


def test_it_goes_on_when_the_old_versions_cannot_be_deleted(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """古いバージョンを消せなくても、警告して記録を向け、インスタンスの終了へ進む。"""
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    pointing(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "delete_launch_template_versions",
        "UnauthorizedOperation",
        expected_params={"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
    )
    read_records(aws_stubs)
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()
    assert any(
        line.startswith(
            f"temt: warning: launch template {TEMPLATE}: cannot delete the old versions 1: "
        )
        for line in capsys.readouterr().err.splitlines()
    )


def test_it_deletes_every_old_version(aws_stubs: Stubs, configured: Service) -> None:
    """新しいバージョンを既定にしたら、既定でなかったものも、既定だったものも消す。"""
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    pointed(aws_stubs, 1, 2)
    read_records(aws_stubs)
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()


@pytest.mark.parametrize("answer", [[], [{"ImageId": OLD, "State": "failed"}]])
def test_it_asks_for_apply_when_the_written_id_is_not_usable(
    aws_stubs: Stubs, configured: Service, answer: list[dict[str, Any]]
) -> None:
    """ID で書かれていて、テンプレートと同じ値でも、使える AMI が無ければ揃っていない。"""
    found(aws_stubs, an_instance(INSTANCE, "running"))
    read_template(aws_stubs)
    aws_stubs.ec2.add_response("describe_images", {"Images": answer}, {"ImageIds": [OLD]})

    with pytest.raises(TemtError) as caught:
        bake.run(configured, yes=True)

    aws_stubs.done()
    assert str(caught.value) == UNUSABLE_IMAGE


def test_it_records_the_apps_of_the_instance_as_baked(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """AMI が available になったら、テンプレートに続けて記録を新しい AMI に向ける。

    インスタンスに入れてまだ焼いていない記録には、焼いた版と AMI を書く。
    """
    found(aws_stubs, {**an_instance(INSTANCE, "stopped"), "ImageId": OLD})
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    kept = {**RECORD, "baked_version": "1", "baked_image": OLD}
    read_records(
        aws_stubs,
        {"Name": f"/{NAME}/jupyter/deploy", "Type": "String", "Value": json.dumps(UNBAKED)},
        # 起動元の AMI に焼いてあったものは、新しい AMI にも引き継がれる
        {"Name": f"/{NAME}/mise/deploy", "Type": "String", "Value": json.dumps(kept)},
        # ほかの AMI に焼いたものには触れない
        {
            "Name": f"/{NAME}/old/deploy",
            "Type": "String",
            "Value": json.dumps({**RECORD, "baked_version": "1", "baked_image": "ami-0aaa"}),
        },
    )
    written_record(aws_stubs, "jupyter", {**RECORD, "baked_version": "2", "baked_image": NEW})
    written_record(aws_stubs, "mise", {**kept, "baked_image": NEW})
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()
    lines = told(capsys.readouterr().out)
    # 焼いたと知らせるのは、このインスタンスに入れたものだけ
    assert f"recorded jupyter as baked in {NEW}" in lines


def test_it_goes_on_when_the_records_cannot_be_read(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """記録が読めなくても焼くことは止めず、警告してインスタンスの終了へ進む。"""
    found(aws_stubs, an_instance(INSTANCE, "stopped"))
    checked(aws_stubs)
    created(aws_stubs)
    image_state(aws_stubs, "available")
    pointed(aws_stubs)
    aws_stubs.ssm.add_client_error(
        "get_parameters_by_path",
        service_error_code="AccessDeniedException",
        http_status_code=400,
        expected_params={"Path": f"/{NAME}", "Recursive": True},
    )
    terminated(aws_stubs)

    bake.run(configured, yes=True)

    aws_stubs.done()
    captured = capsys.readouterr()
    warnings = captured.err.splitlines()
    assert len(warnings) == 1
    assert warnings[0].startswith("temt: warning: cannot read the deploy records: ")
    assert warnings[0].endswith("; app list may show the apps as lost")
    assert told(captured.out)[-1] == f"baked {IMAGE_NAME} ({NEW}); the next start boots from it"
