"""template show の本体のテスト。AWS は Stubber で差し替える。

端から端まで（show.template か cli.main）を通し、表示の形を確かめる。instance show は
test_status.py が確かめる。
"""

import base64
import tomllib
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import Stubs

from temt import cli, show
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
IMAGE = "ami-0123456789abcdef0"
NEWEST = "ami-0fedcba9876543210"
IMAGE_NAME = "temt-dev-main-2609201800"
NEWEST_NAME = "temt-dev-main-2609211800"
NAMES = {IMAGE: IMAGE_NAME, NEWEST: NEWEST_NAME}
SUBNET = "subnet-0123456789abcdef0"
VPC = "vpc-0123456789abcdef0"
GROUP = "sg-0123456789abcdef0"
# AMI の作成日時。AWS は文字列で返す
MADE = "2026-09-20T09:00:00.000Z"
LATER = "2026-09-21T09:00:00.000Z"
# MADE と LATER を手元の時刻（日本時間）で表したもの
MADE_SHOWN = "2026-09-20 18:00:00"
LATER_SHOWN = "2026-09-21 18:00:00"
# AMI のルートボリュームのデバイス名
ROOT_DEVICE = "/dev/sda1"
# バージョンの作成時刻。AWS は datetime で返す
WHEN = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)

THE_TEMPLATE = {"LaunchTemplateName": TEMPLATE}
THE_SUBNET = {"Filters": [{"Name": "subnet-id", "Values": [SUBNET]}]}
THE_IMAGE = {"ImageIds": [IMAGE]}
SERVICE_IMAGES = {
    "Owners": ["self"],
    "Filters": [
        {"Name": "name", "Values": ["temt-dev-main-??????????"]},
        {"Name": "state", "Values": ["available", "pending"]},
    ],
}
# temt が付けるタグ
MARKS = [{"Key": "Name", "Value": NAME}, {"Key": "temt:service", "Value": "dev-main"}]


def data(**changes: Any) -> dict[str, Any]:
    """apply が入れた起動テンプレートの中身。AWS はこの形で返す。changes で項目を替える。"""
    return {
        "ImageId": IMAGE,
        "InstanceType": "t3.micro",
        "InstanceInitiatedShutdownBehavior": "terminate",
        "IamInstanceProfile": {"Name": NAME},
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "SubnetId": SUBNET,
                "AssociatePublicIpAddress": True,
                "Groups": [GROUP],
                "Ipv6AddressCount": 1,
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


def a_version(number: int = 1, *, default: bool = True, **changes: Any) -> dict[str, Any]:
    """describe-launch-template-versions が返すバージョン 1 つ分。"""
    return {
        "LaunchTemplateName": TEMPLATE,
        "VersionNumber": number,
        "DefaultVersion": default,
        "CreateTime": WHEN,
        "LaunchTemplateData": data(**changes),
    }


def section(out: str, head: str) -> list[str]:
    """出力のうち、head で始まる行から、次の節の見出し（字下げの無い行）の前までの行。"""
    lines = out.splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith(head))
    rest = lines[first + 1 :]
    last = next(
        (index for index, line in enumerate(rest) if line and not line.startswith(" ")),
        len(rest),
    )
    return lines[first : first + 1 + last]


def content(out: str) -> str:
    """1 つめの節の、設定ファイルと同じ TOML の行。字下げを外し、つなげる。"""
    lines = section(out, "launch template ")[2:]
    return "".join(f"{line.removeprefix('  ')}\n" for line in lines)


def versions(stubs: Stubs, *found: dict[str, Any]) -> None:
    """起動テンプレートのバージョンを積む。"""
    stubs.ec2.add_response(
        "describe_launch_template_versions", {"LaunchTemplateVersions": list(found)}, THE_TEMPLATE
    )


def subnet(stubs: Stubs, *, vpc_id: str = VPC) -> None:
    """テンプレートの SubnetId のサブネットを積む。vpc_id が空なら、消えたものとする。"""
    found = [{"SubnetId": SUBNET, "VpcId": vpc_id}] if vpc_id else []
    stubs.ec2.add_response("describe_subnets", {"Subnets": found}, THE_SUBNET)


def image(stubs: Stubs, *, created: str = MADE, state: str = "available") -> None:
    """テンプレートが指す AMI を積む。"""
    found = {
        "ImageId": IMAGE,
        "Name": IMAGE_NAME,
        "CreationDate": created,
        "State": state,
        "RootDeviceName": ROOT_DEVICE,
        "BlockDeviceMappings": [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 8}}],
    }
    stubs.ec2.add_response("describe_images", {"Images": [found]}, THE_IMAGE)


def no_image(stubs: Stubs) -> None:
    """テンプレートが指す AMI が登録解除されているときの誤りを積む。"""
    stubs.ec2.add_client_error(
        "describe_images", "InvalidAMIID.NotFound", expected_params=THE_IMAGE
    )


def service_images(stubs: Stubs, *found: tuple[str, str], state: str = "available") -> None:
    """名前が temt-dev-main-<時刻> の AMI を積む。"""
    stubs.ec2.add_response(
        "describe_images",
        {
            "Images": [
                {"ImageId": one, "Name": NAMES[one], "State": state, "CreationDate": created}
                for one, created in found
            ]
        },
        SERVICE_IMAGES,
    )


def a_template(stubs: Stubs, *found: dict[str, Any]) -> None:
    """よくある応答の組。バージョン、サブネット、AMI、サービスの AMI の順に積む。"""
    versions(stubs, *(found or (a_version(1),)))
    subnet(stubs)
    image(stubs)
    service_images(stubs, (IMAGE, MADE))


def test_it_shows_the_default_version(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    written = configured.conf_path.read_text(encoding="utf-8")
    versions(aws_stubs, a_version(3))
    subnet(aws_stubs)
    image(aws_stubs)
    service_images(aws_stubs, (IMAGE, MADE))
    show.template(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    # 表示はすべて標準出力に書く
    assert err == ""
    tags = (
        f'Tags = [{{ Key = "Name", Value = "{NAME}" }}, '
        '{ Key = "temt:service", Value = "dev-main" }]'
    )
    assert out == (
        f"launch template {TEMPLATE}:\n"
        "  version: 3 (created 2026-09-20 18:00:00)\n"
        "  [launch-template]\n"
        f'  ImageId = "{IMAGE}"\n'
        '  InstanceType = "t3.micro"\n'
        '  InstanceInitiatedShutdownBehavior = "terminate"\n'
        f'  IamInstanceProfile = {{ Name = "{NAME}" }}\n'
        "\n"
        "  [[launch-template.NetworkInterfaces]]\n"
        "  DeviceIndex = 0\n"
        f'  SubnetId = "{SUBNET}"  # {VPC}\n'
        "  AssociatePublicIpAddress = true\n"
        f'  Groups = ["{GROUP}"]\n'
        "  Ipv6AddressCount = 1\n"
        "\n"
        "  [launch-template.MetadataOptions]\n"
        '  HttpTokens = "required"\n'
        '  InstanceMetadataTags = "enabled"\n'
        "\n"
        "  [launch-template.CreditSpecification]\n"
        '  CpuCredits = "unlimited"\n'
        "\n"
        "  [[launch-template.TagSpecifications]]\n"
        '  ResourceType = "instance"\n'
        f"  {tags}\n"
        "\n"
        "  [[launch-template.TagSpecifications]]\n"
        '  ResourceType = "volume"\n'
        f"  {tags}\n"
        "\n"
        "  [[launch-template.TagSpecifications]]\n"
        '  ResourceType = "network-interface"\n'
        f"  {tags}\n"
        f"image {IMAGE}:\n"
        f"  name:        {IMAGE_NAME}\n"
        f"  created:     {MADE_SHOWN}\n"
        "  state:       available\n"
        "  root volume: 8 GiB\n"
        f"  latest:      {IMAGE_NAME} ({IMAGE}) created {MADE_SHOWN}\n"
        "versions (* is the default):\n"
        f"  3 * 2026-09-20 18:00:00 {IMAGE} t3.micro {SUBNET}\n"
    )
    # ロックは取らないので、known_hosts のディレクトリも作らない
    assert not configured.known_hosts_dir.exists()
    # 設定ファイルは読むだけで、書かない
    assert configured.conf_path.read_text(encoding="utf-8") == written


def test_it_shows_the_content_in_the_form_of_the_config_file(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """中身は、そのまま設定ファイルにコピーできる TOML で出す。読み戻せば、テンプレートの
    中身になる。

    UserData は base64 のまま、ほかの値も型を保って出す。
    """
    user_data = base64.b64encode(b"#!/bin/sh\necho hi\n").decode()
    mappings = [
        {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30, "DeleteOnTermination": True}},
        {"DeviceName": "/dev/sdf", "Ebs": {"VolumeSize": 100, "VolumeType": "gp3"}},
    ]
    found = data(UserData=user_data, BlockDeviceMappings=mappings, KeyName="mine")
    a_template(
        aws_stubs, a_version(1, UserData=user_data, BlockDeviceMappings=mappings, KeyName="mine")
    )
    show.template(configured)
    aws_stubs.done()
    assert tomllib.loads(content(capsys.readouterr().out)) == {"launch-template": found}


def test_it_leaves_out_what_exists_only_when_read(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """読み出したときにだけある項目は、設定ファイルに書けないので出さない。"""
    interface = {
        **data()["NetworkInterfaces"][0],
        "Ipv6Addresses": [{"Ipv6Address": "2001:db8::1", "IsPrimaryIpv6": True}],
    }
    a_template(
        aws_stubs,
        a_version(
            1,
            MetadataOptions={
                "HttpTokens": "required",
                "InstanceMetadataTags": "enabled",
                "State": "applied",
            },
            NetworkInterfaces=[interface],
        ),
    )
    show.template(configured)
    aws_stubs.done()
    shown = tomllib.loads(content(capsys.readouterr().out))["launch-template"]
    assert shown["MetadataOptions"] == {"HttpTokens": "required", "InstanceMetadataTags": "enabled"}
    assert shown["NetworkInterfaces"][0]["Ipv6Addresses"] == [{"Ipv6Address": "2001:db8::1"}]


def test_it_shows_the_vpc_of_the_primary_interface_only(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """VPC を添えるのは、DeviceIndex が 0 の要素の SubnetId だけ。ほかの要素のサブネットは
    引かない。"""
    other = {"DeviceIndex": 1, "SubnetId": "subnet-00000000000000001"}
    primary = data()["NetworkInterfaces"][0]
    a_template(aws_stubs, a_version(1, NetworkInterfaces=[other, primary]))
    show.template(configured)
    aws_stubs.done()
    lines = capsys.readouterr().out.splitlines()
    assert '  SubnetId = "subnet-00000000000000001"' in lines
    assert f'  SubnetId = "{SUBNET}"  # {VPC}' in lines
    # 一覧の SubnetId も、DeviceIndex が 0 の要素のもの
    assert lines[-1] == f"  1 * 2026-09-20 18:00:00 {IMAGE} t3.micro {SUBNET}"


def test_it_shows_a_template_without_a_subnet(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """SubnetId が無ければ、サブネットを引かない。一覧には (not set) と出す。"""
    versions(aws_stubs, a_version(1, NetworkInterfaces=[{"DeviceIndex": 0, "Groups": [GROUP]}]))
    image(aws_stubs)
    service_images(aws_stubs, (IMAGE, MADE))
    show.template(configured)
    aws_stubs.done()
    out = capsys.readouterr().out
    assert "SubnetId" not in content(out)
    assert out.splitlines()[-1] == f"  1 * 2026-09-20 18:00:00 {IMAGE} t3.micro (not set)"


def test_it_does_not_look_up_a_subnet_id_of_another_form(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """サブネットの ID の形でない値は、引かずにそのまま出す。絞り込みの値は * を展開する。"""
    interface = {**data()["NetworkInterfaces"][0], "SubnetId": "subnet-*"}
    versions(aws_stubs, a_version(1, NetworkInterfaces=[interface]))
    image(aws_stubs)
    service_images(aws_stubs, (IMAGE, MADE))
    show.template(configured)
    aws_stubs.done()
    assert '  SubnetId = "subnet-*"' in capsys.readouterr().out.splitlines()


def test_it_shows_an_image_without_an_ebs_root_volume(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """EBS のルートボリュームが見つからない AMI は、そう出す。"""
    versions(aws_stubs, a_version(1))
    subnet(aws_stubs)
    found = {"ImageId": IMAGE, "Name": IMAGE_NAME, "CreationDate": MADE, "State": "available"}
    aws_stubs.ec2.add_response("describe_images", {"Images": [found]}, THE_IMAGE)
    service_images(aws_stubs, (IMAGE, MADE))
    show.template(configured)
    aws_stubs.done()
    assert "  root volume: (no EBS root volume)" in capsys.readouterr().out.splitlines()


def test_it_shows_an_image_that_is_gone(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """登録解除された AMI でも、表示は止まらない。"""
    versions(aws_stubs, a_version(1))
    subnet(aws_stubs)
    no_image(aws_stubs)
    service_images(aws_stubs, (NEWEST, LATER))
    show.template(configured)
    aws_stubs.done()
    assert section(capsys.readouterr().out, "image ") == [
        f"image {IMAGE}:",
        "  state:  (not found; it may have been deregistered)",
        f"  latest: {NEWEST_NAME} ({NEWEST}) created {LATER_SHOWN}",
    ]


def test_it_marks_a_newer_image(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """サービスの最新 AMI（apply が起動テンプレートを作るときに選ぶもの）がテンプレートのもの
    より新しければ、そう分かるようにする。"""
    versions(aws_stubs, a_version(1))
    subnet(aws_stubs)
    image(aws_stubs)
    service_images(aws_stubs, (IMAGE, MADE), (NEWEST, LATER))
    show.template(configured)
    aws_stubs.done()
    assert section(capsys.readouterr().out, "image ")[-1] == (
        f"  latest:      {NEWEST_NAME} ({NEWEST}) created {LATER_SHOWN}, newer"
    )


def test_it_shows_no_ami_of_the_service(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    versions(aws_stubs, a_version(1))
    subnet(aws_stubs)
    image(aws_stubs)
    service_images(aws_stubs)
    show.template(configured)
    aws_stubs.done()
    assert (
        section(capsys.readouterr().out, "image ")[-1]
        == "  latest:      none (no AMI named temt-dev-main-<YYMMDDHHMM>)"
    )


def test_it_lists_every_version(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """一覧は番号の順に並べ、既定バージョンに印を付ける。中身を出すのは既定バージョンだけ。"""
    versions(
        aws_stubs,
        a_version(2),
        a_version(1, default=False, ImageId=NEWEST, InstanceType="t3.small"),
    )
    subnet(aws_stubs)
    image(aws_stubs)
    service_images(aws_stubs, (IMAGE, MADE))
    show.template(configured)
    aws_stubs.done()
    out = capsys.readouterr().out
    assert section(out, "versions ") == [
        "versions (* is the default):",
        f"  1   2026-09-20 18:00:00 {NEWEST} t3.small {SUBNET}",
        f"  2 * 2026-09-20 18:00:00 {IMAGE} t3.micro {SUBNET}",
    ]
    assert "  version: 2 (created 2026-09-20 18:00:00)" in out.splitlines()
    assert NEWEST not in content(out)


def test_it_shows_a_subnet_that_is_gone(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """サブネットが消えていても、表示は止まらない。"""
    versions(aws_stubs, a_version(1))
    subnet(aws_stubs, vpc_id="")
    image(aws_stubs)
    service_images(aws_stubs, (IMAGE, MADE))
    show.template(configured)
    aws_stubs.done()
    assert f'  SubnetId = "{SUBNET}"  # (no such subnet)' in capsys.readouterr().out.splitlines()


def test_it_asks_for_apply_without_the_launch_template(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    aws_stubs.ec2.add_client_error(
        "describe_launch_template_versions",
        "InvalidLaunchTemplateName.NotFoundException",
        expected_params=THE_TEMPLATE,
    )
    with pytest.raises(TemtError, match="run 'temt apply --svc dev-main' first"):
        show.template(configured)
    aws_stubs.done()
    assert capsys.readouterr() == ("", "")


def test_it_asks_for_apply_when_there_is_no_config_file(aws_stubs: Stubs, service: Service) -> None:
    with pytest.raises(TemtError, match="run 'temt apply --svc dev-main' first"):
        show.template(service)
    assert aws_stubs.passed == []


def test_main_runs_it(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    a_template(aws_stubs)
    assert cli.main(["temt", "template", "show", "--svc", "dev-main"]) == 0
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert err == ""
    assert out.startswith(f"launch template {TEMPLATE}:\n")


def test_it_tells_that_the_newest_image_is_pending(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """最新の AMI が pending（コピーの途中など）なら、そう添える。"""
    versions(aws_stubs, a_version(1))
    subnet(aws_stubs)
    image(aws_stubs)
    service_images(aws_stubs, (NEWEST, LATER), state="pending")
    show.template(configured)
    aws_stubs.done()
    assert section(capsys.readouterr().out, "image ")[-1] == (
        f"  latest:      {NEWEST_NAME} ({NEWEST}, pending) created {LATER_SHOWN}, newer"
    )
