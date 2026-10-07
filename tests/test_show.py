"""template show の本体のテスト。AWS は Stubber で差し替える。

端から端まで（show.template か cli.main）を通し、表示の形を確かめる。instance show は
test_status.py が確かめる。
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import Stubs

from temt import cli, resources, show
from temt.conf import Conf, LaunchTemplate
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
        {"Name": "tag:temt:service", "Values": ["dev-main"]},
        {"Name": "state", "Values": ["available"]},
    ],
}

VALUES = Conf(
    KeyName="temt", launch_template=LaunchTemplate(InstanceType="t3.micro", SubnetId=SUBNET)
)


def data(**changes: Any) -> dict[str, Any]:
    """apply が入れた起動テンプレートの設定。AWS はこの形で返す。"""
    return {
        **resources.template_data(Service("dev-main"), VALUES, IMAGE, SUBNET, GROUP, ipv6=True),
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


def service_images(stubs: Stubs, *found: tuple[str, str]) -> None:
    """temt:service=dev-main の AMI を積む。"""
    stubs.ec2.add_response(
        "describe_images",
        {
            "Images": [
                {"ImageId": one, "Name": NAMES[one], "CreationDate": created}
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
    assert out == (
        f"launch template {TEMPLATE}:\n"
        "  version:                3 (created 2026-09-20 18:00:00)\n"
        f"  ImageId:                {IMAGE}\n"
        "  InstanceType:           t3.micro\n"
        "  KeyName:                temt\n"
        "  VolumeSize:             (not set)\n"
        f"  SubnetId:               {SUBNET} ({VPC})\n"
        f"  security groups:        {GROUP}\n"
        f"  instance profile:       {NAME}\n"
        "  shutdown behavior:      terminate\n"
        "  cpu credits:            unlimited\n"
        "  public ipv4:            true\n"
        "  ipv6 addresses:         1\n"
        "  http tokens:            required\n"
        "  metadata tags:          enabled\n"
        f"  instance tags:          Name={NAME} temt:service=dev-main\n"
        f"  network-interface tags: Name={NAME} temt:service=dev-main\n"
        f"  volume tags:            Name={NAME} temt:service=dev-main\n"
        f"image {IMAGE}:\n"
        f"  name:        {IMAGE_NAME}\n"
        f"  created:     {MADE_SHOWN}\n"
        "  state:       available\n"
        "  root volume: 8 GiB\n"
        f"  latest:      {IMAGE_NAME} ({IMAGE}, temt:service=dev-main) created {MADE_SHOWN}\n"
        "versions (* is the default):\n"
        f"  3 * 2026-09-20 18:00:00 {IMAGE} t3.micro {SUBNET}\n"
    )
    # ロックは取らないので、known_hosts のディレクトリも作らない
    assert not configured.known_hosts_dir.exists()
    # 設定ファイルは読むだけで、書かない
    assert configured.conf_path.read_text(encoding="utf-8") == written


def test_it_shows_no_ipv6_as_zero(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """IPv6 アドレスを付けないテンプレートは、0 と出す。"""
    interface = {**data()["NetworkInterfaces"][0]}
    del interface["Ipv6AddressCount"]
    a_template(aws_stubs, a_version(1, NetworkInterfaces=[interface]))
    show.template(configured)
    aws_stubs.done()
    assert "  ipv6 addresses:         0" in capsys.readouterr().out.splitlines()


def test_it_shows_the_volume_size(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """VolumeSize を入れたテンプレートは、その大きさを出す。"""
    mappings = [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30}}]
    a_template(aws_stubs, a_version(1, BlockDeviceMappings=mappings))
    show.template(configured)
    aws_stubs.done()
    assert "  VolumeSize:             30" in capsys.readouterr().out.splitlines()


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
    lines = capsys.readouterr().out.splitlines()
    assert lines[18:21] == [
        f"image {IMAGE}:",
        "  state:  (not found; it may have been deregistered)",
        f"  latest: {NEWEST_NAME} ({NEWEST}, temt:service=dev-main) created {LATER_SHOWN}",
    ]


def test_it_marks_a_newer_image(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """temt:service=<svc> の最新 AMI がテンプレートのものより新しければ、そう分かるようにする。"""
    versions(aws_stubs, a_version(1))
    subnet(aws_stubs)
    image(aws_stubs)
    service_images(aws_stubs, (IMAGE, MADE), (NEWEST, LATER))
    show.template(configured)
    aws_stubs.done()
    lines = capsys.readouterr().out.splitlines()
    assert lines[23] == (
        f"  latest:      {NEWEST_NAME} ({NEWEST}, temt:service=dev-main) "
        f"created {LATER_SHOWN}, newer"
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
    assert capsys.readouterr().out.splitlines()[23] == "  latest:      none (temt:service=dev-main)"


def test_it_lists_every_version(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """一覧は番号の順に並べ、既定バージョンに印を付ける。"""
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
    lines = capsys.readouterr().out.splitlines()
    assert lines[24:] == [
        "versions (* is the default):",
        f"  1   2026-09-20 18:00:00 {NEWEST} t3.small {SUBNET}",
        f"  2 * 2026-09-20 18:00:00 {IMAGE} t3.micro {SUBNET}",
    ]


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
    assert (
        capsys.readouterr().out.splitlines()[6]
        == f"  SubnetId:               {SUBNET} (no such subnet)"
    )


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
