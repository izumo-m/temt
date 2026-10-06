"""AWS からの読み取りのテスト。botocore の Stubber で応答を差し替える。

AWS にはつながない。開発環境の ~/.aws や AWS_* の環境変数にも左右されないよう、
ホームを一時ディレクトリに向け、環境変数を消してからクライアントを作る。
"""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from botocore.stub import Stubber
from conftest import offline, watched
from types_boto3_ec2.client import EC2Client
from types_boto3_ssm.client import SSMClient

from temt import aws, conf
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import Service

REGION = "ap-northeast-1"
DEFAULT_VPC = {"Filters": [{"Name": "is-default", "Values": ["true"]}]}
ONE_VPC = {"Vpcs": [{"VpcId": "vpc-1"}]}
PUBLIC_SUBNETS = {
    "Filters": [
        {"Name": "vpc-id", "Values": ["vpc-1"]},
        {"Name": "map-public-ip-on-launch", "Values": ["true"]},
    ]
}
SUBNETS = {
    "Subnets": [
        {"SubnetId": "subnet-000000c1", "AvailabilityZone": "ap-northeast-1c", "VpcId": "vpc-1"},
        {"SubnetId": "subnet-000000b1", "AvailabilityZone": "ap-northeast-1a", "VpcId": "vpc-1"},
        {"SubnetId": "subnet-000000a1", "AvailabilityZone": "ap-northeast-1a", "VpcId": "vpc-1"},
    ]
}
SERVICE_IMAGES = {
    "Owners": ["self"],
    "Filters": [
        {"Name": "tag:temt:service", "Values": ["dev"]},
        {"Name": "state", "Values": ["available"]},
    ],
}
THE_SUBNET = {"Filters": [{"Name": "subnet-id", "Values": ["subnet-000000c1"]}]}


def a_subnet(**changes: Any) -> dict[str, Any]:
    """describe-subnets が返す 1 つ分。既定は IPv4 の CIDR だけを持つ。"""
    return {"SubnetId": "subnet-000000c1", "VpcId": "vpc-1", "CidrBlock": "10.0.0.0/24", **changes}


def ipv6(*states: str) -> list[dict[str, Any]]:
    """サブネットに結び付いた IPv6 の CIDR。"""
    return [
        {"Ipv6CidrBlock": f"2001:db8:{index}::/64", "Ipv6CidrBlockState": {"State": state}}
        for index, state in enumerate(states)
    ]


def profile(region: str = "us-east-1") -> None:
    """一時的なホームに、プロファイル work を持つ設定ファイルを置く。"""
    aws_dir = Path.home() / ".aws"
    aws_dir.mkdir()
    (aws_dir / "config").write_text(f"[profile work]\nregion = {region}\n", encoding="utf-8")


def test_session_passes_nothing_that_is_not_written() -> None:
    made = aws.session(Conf())
    assert made.profile_name == "default"
    # 書かれていなければ Conf の既定のリージョンになる
    assert made.region_name == REGION


def test_session_passes_what_is_written() -> None:
    profile()
    made = aws.session(Conf(AwsProfile="work", AwsRegion=REGION))
    assert made.profile_name == "work"
    # 設定ファイルに書いたリージョンが、プロファイルのリージョンより優先される
    assert made.region_name == REGION


def test_session_leaves_the_profile_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """何も書かれていなければプロファイルを渡さず、AWS の既定の規則で決まる。"""
    profile()
    monkeypatch.setenv("AWS_PROFILE", "work")
    made = aws.session(Conf())
    assert made.profile_name == "work"


def test_session_ignores_the_region_in_the_profile() -> None:
    """リージョンは必ず渡すので、プロファイルの設定は使われない。"""
    profile()
    assert aws.session(Conf(AwsProfile="work")).region_name == REGION


def test_session_takes_the_profile_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """--profile は設定ファイルの AwsProfile より優先される。"""
    profile()
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    assert aws.session(Conf()).profile_name == "work"
    assert aws.session(Conf(AwsProfile="other")).profile_name == "work"


def test_env_session_takes_the_region_option_first(monkeypatch: pytest.MonkeyPatch) -> None:
    profile("us-east-1")
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    monkeypatch.setattr(conf, "ENV_REGION", "us-west-2")
    made = aws.env_session("eu-west-1")
    assert (made.profile_name, made.region_name) == ("work", "eu-west-1")


def test_env_session_takes_the_region_of_the_environment_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile("us-east-1")
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    monkeypatch.setattr(conf, "ENV_REGION", "us-west-2")
    assert aws.env_session(None).region_name == "us-west-2"


def test_env_session_takes_the_region_of_the_profile_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile("us-east-1")
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    assert aws.env_session(None).region_name == "us-east-1"


def test_env_session_leaves_the_profile_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile("us-east-1")
    monkeypatch.setenv("AWS_PROFILE", "work")
    made = aws.env_session(None)
    assert (made.profile_name, made.region_name) == ("work", "us-east-1")


def test_env_session_without_a_region() -> None:
    with pytest.raises(TemtError, match="no region; give --region, set AWS_REGION"):
        aws.env_session(None)


class StsSession:
    """sts のクライアントだけを返す Session の代わり。"""

    def __init__(self, sts: Any) -> None:
        self.sts = sts

    def client(self, name: str) -> Any:
        assert name == "sts"
        return self.sts


def test_account(stub_failures: list[BotoCoreError]) -> None:
    sts = watched(offline().client("sts"), stub_failures)
    with Stubber(sts) as stub:
        stub.add_response(
            "get_caller_identity",
            {"UserId": "u", "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/u"},
            {},
        )
        assert aws.account(cast("boto3.Session", StsSession(sts))) == "123456789012"
        stub.assert_no_pending_responses()


def test_public_subnet_takes_the_first_zone_then_the_first_id(
    ec2: EC2Client, service: Service
) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_vpcs", ONE_VPC, DEFAULT_VPC)
        stub.add_response("describe_subnets", SUBNETS, PUBLIC_SUBNETS)
        assert aws.public_subnet(ec2, REGION, service.conf_path) == "subnet-000000a1"
        stub.assert_no_pending_responses()


def test_public_subnet_without_a_default_vpc(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_vpcs", {"Vpcs": []}, DEFAULT_VPC)
        with pytest.raises(TemtError) as caught:
            aws.public_subnet(ec2, REGION, service.conf_path)
    assert f"no default VPC in {REGION}" in str(caught.value)
    # SubnetId はテーブルの中なので、位置付きで書くよう促す
    assert f"write launch-template.SubnetId in {service.conf_path}" in str(caught.value)


def test_public_subnet_without_a_public_subnet(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_vpcs", ONE_VPC, DEFAULT_VPC)
        stub.add_response("describe_subnets", {"Subnets": []}, PUBLIC_SUBNETS)
        with pytest.raises(TemtError, match="no public subnet"):
            aws.public_subnet(ec2, REGION, service.conf_path)


def test_public_subnet_without_a_zone_in_the_answer(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_vpcs", ONE_VPC, DEFAULT_VPC)
        stub.add_response(
            "describe_subnets", {"Subnets": [{"SubnetId": "subnet-000000a1"}]}, PUBLIC_SUBNETS
        )
        with pytest.raises(TemtError, match="AWS did not return AvailabilityZone"):
            aws.public_subnet(ec2, REGION, service.conf_path)


def test_subnet(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_subnets", {"Subnets": [a_subnet()]}, THE_SUBNET)
        assert aws.subnet(ec2, "subnet-000000c1", service.conf_path) == aws.Subnet(
            vpc_id="vpc-1", ipv6=False
        )


def test_subnet_with_an_ipv6_cidr(ec2: EC2Client, service: Service) -> None:
    """結び付いた IPv6 の CIDR が 1 つでもあれば、IPv6 のサブネットとして扱う。"""
    found = a_subnet(Ipv6CidrBlockAssociationSet=ipv6("disassociated", "associated"))
    with Stubber(ec2) as stub:
        stub.add_response("describe_subnets", {"Subnets": [found]}, THE_SUBNET)
        assert aws.subnet(ec2, "subnet-000000c1", service.conf_path) == aws.Subnet(
            vpc_id="vpc-1", ipv6=True
        )


def test_subnet_with_an_ipv6_cidr_that_is_not_associated(ec2: EC2Client, service: Service) -> None:
    """結び付いていない IPv6 の CIDR は数えない。"""
    found = a_subnet(Ipv6CidrBlockAssociationSet=ipv6("associating"))
    with Stubber(ec2) as stub:
        stub.add_response("describe_subnets", {"Subnets": [found]}, THE_SUBNET)
        assert aws.subnet(ec2, "subnet-000000c1", service.conf_path) == aws.Subnet(
            vpc_id="vpc-1", ipv6=False
        )


def test_subnet_without_an_ipv4_cidr(ec2: EC2Client, service: Service) -> None:
    """IPv6 だけのサブネットは扱わない。"""
    found = a_subnet(Ipv6Native=True)
    del found["CidrBlock"]
    with Stubber(ec2) as stub:
        stub.add_response("describe_subnets", {"Subnets": [found]}, THE_SUBNET)
        with pytest.raises(TemtError, match="subnet-000000c1 has no IPv4 CIDR"):
            aws.subnet(ec2, "subnet-000000c1", service.conf_path)


def test_subnet_without_the_subnet(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_subnets",
            {"Subnets": []},
            {"Filters": [{"Name": "subnet-id", "Values": ["subnet-000000a1bcdef01"]}]},
        )
        with pytest.raises(TemtError, match="no such subnet: subnet-000000a1bcdef01") as caught:
            aws.subnet(ec2, "subnet-000000a1bcdef01", service.conf_path)
    # 別のアカウントで作るときの選び直し方を添える
    assert str(caught.value).endswith(
        "remove launch-template.SubnetId and launch-template.ImageId "
        f"from {service.conf_path} and run apply again"
    )


# 絞り込みの値は * と ? を展開するので、形を確かめてから渡す
@pytest.mark.parametrize(
    "subnet_id", ["subnet-000000a1bc*", "subnet-", "vpc-01234567", "", "subnet-0ABC1234"]
)
def test_subnet_refuses_a_bad_id(ec2: EC2Client, service: Service, subnet_id: str) -> None:
    with Stubber(ec2) as stub:
        with pytest.raises(TemtError, match="not a subnet id"):
            aws.subnet(ec2, subnet_id, service.conf_path)
        stub.assert_no_pending_responses()


def test_latest_ami_takes_the_newest_of_the_service(ec2: EC2Client, ssm: SSMClient) -> None:
    images = {
        "Images": [
            {
                "ImageId": "ami-old",
                "Name": "temt-dev-2609010000",
                "CreationDate": "2026-09-01T00:00:00.000Z",
            },
            {
                "ImageId": "ami-new",
                "Name": "temt-dev-2609200000",
                "CreationDate": "2026-09-20T00:00:00.000Z",
            },
        ]
    }
    with Stubber(ec2) as ec2_stub, Stubber(ssm) as ssm_stub:
        ec2_stub.add_response("describe_images", images, SERVICE_IMAGES)
        # 設定ファイルに書くのは、ID ではなく名前
        assert aws.latest_ami(ec2, ssm, "dev") == "temt-dev-2609200000"
        ec2_stub.assert_no_pending_responses()
        ssm_stub.assert_no_pending_responses()


def test_latest_ami_falls_back_to_ubuntu(ec2: EC2Client, ssm: SSMClient) -> None:
    with Stubber(ec2) as ec2_stub, Stubber(ssm) as ssm_stub:
        ec2_stub.add_response("describe_images", {"Images": []}, SERVICE_IMAGES)
        ssm_stub.add_response(
            "get_parameter",
            {"Parameter": {"Name": aws.UBUNTU_AMI_PARAMETER, "Value": "ami-ubuntu"}},
            {"Name": aws.UBUNTU_AMI_PARAMETER},
        )
        assert aws.latest_ami(ec2, ssm, "dev") == "ami-ubuntu"
        ssm_stub.assert_no_pending_responses()


IMAGE_ID = "ami-0123456789abcdef0"
IMAGE_NAME = "temt-dev-2609200000"
BY_NAME = {"Owners": ["self"], "Filters": [{"Name": "name", "Values": [IMAGE_NAME]}]}
BY_ID = {"ImageIds": [IMAGE_ID]}


def found(state: str = "available", image_id: str = IMAGE_ID) -> dict[str, Any]:
    """describe-images が返す AMI 1 つ分。"""
    return {"ImageId": image_id, "Name": IMAGE_NAME, "State": state}


FOUND = {"Images": [found()]}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (IMAGE_ID, True),
        ("ami-01234567", True),
        (IMAGE_NAME, False),
        ("ami-0123456789ABCDEF0", False),
        ("ami-0123", False),
        ("ami-0123456789", False),
        ("ami-0123456789abcdef01", False),
    ],
)
def test_is_image_id(value: str, expected: bool) -> None:
    assert aws.is_image_id(value) is expected


def test_image_by_name(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", FOUND, BY_NAME)
        assert aws.image(ec2, IMAGE_NAME, service.conf_path)["ImageId"] == IMAGE_ID
        stub.assert_no_pending_responses()


def test_image_by_id(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", FOUND, BY_ID)
        assert aws.image(ec2, IMAGE_ID, service.conf_path)["ImageId"] == IMAGE_ID
        stub.assert_no_pending_responses()


def test_image_without_the_name(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", {"Images": []}, BY_NAME)
        with pytest.raises(TemtError) as caught:
            aws.image(ec2, IMAGE_NAME, service.conf_path)
        stub.assert_no_pending_responses()
    assert str(caught.value) == (
        f"no such AMI: {IMAGE_NAME}; remove launch-template.ImageId from "
        f"{service.conf_path} and run apply again to pick one"
    )


@pytest.mark.parametrize(
    "answer", [aws.NO_SUCH_IMAGE, aws.MALFORMED_IMAGE, "empty", "deregistered"]
)
def test_image_without_the_id(ec2: EC2Client, service: Service, answer: str) -> None:
    """ID で引いて無いとき、AWS は誤りを返すことも、空を返すこともある。

    登録を解除した直後の AMI は、しばらく deregistered の状態で見える。
    """
    with Stubber(ec2) as stub:
        if answer == "empty":
            stub.add_response("describe_images", {"Images": []}, BY_ID)
        elif answer == "deregistered":
            stub.add_response("describe_images", {"Images": [found(answer)]}, BY_ID)
        else:
            stub.add_client_error("describe_images", answer, expected_params=BY_ID)
        with pytest.raises(TemtError, match=f"no such AMI: {IMAGE_ID}; remove "):
            aws.image(ec2, IMAGE_ID, service.conf_path)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("state", ["failed", "error", "invalid", "disabled"])
def test_image_that_is_not_usable(ec2: EC2Client, service: Service, state: str) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", {"Images": [found(state)]}, BY_NAME)
        with pytest.raises(TemtError, match=f"the AMI {IMAGE_NAME} is {state}; remove "):
            aws.image(ec2, IMAGE_NAME, service.conf_path)
        stub.assert_no_pending_responses()


def test_a_pending_image_is_usable(ec2: EC2Client, service: Service) -> None:
    """手で書いた ImageId が、作っている途中（pending）の AMI を指していても受け付ける。"""
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", {"Images": [found("pending")]}, BY_NAME)
        assert aws.image(ec2, IMAGE_NAME, service.conf_path)["ImageId"] == IMAGE_ID
        stub.assert_no_pending_responses()


def test_the_root_volume_is_the_one_on_the_root_device() -> None:
    image = {
        **found(),
        "RootDeviceName": "/dev/sda1",
        "BlockDeviceMappings": [
            {"DeviceName": "/dev/sdb", "Ebs": {"VolumeSize": 100}},
            {"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 8}},
        ],
    }
    assert aws.root_volume(image) == aws.RootVolume(device="/dev/sda1", size=8)


@pytest.mark.parametrize(
    "image",
    [
        pytest.param(found(), id="no root device"),
        pytest.param(
            {**found(), "RootDeviceName": "/dev/sda1", "BlockDeviceMappings": []},
            id="no mapping",
        ),
        pytest.param(
            {
                **found(),
                "RootDeviceName": "/dev/sda1",
                "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "VirtualName": "ephemeral0"}],
            },
            id="instance store",
        ),
    ],
)
def test_no_ebs_root_volume(image: dict[str, Any]) -> None:
    assert aws.root_volume(image) is None


def test_a_deregistered_image_beside_a_new_one_with_the_name(ec2: EC2Client) -> None:
    """登録を解除した直後に同じ名前で作り直しても、解除した方は数えない。"""
    other = "ami-0fedcba9876543210"
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_images", {"Images": [found("deregistered"), found(image_id=other)]}, BY_NAME
        )
        assert aws.image_id(ec2, IMAGE_NAME) == other
        stub.assert_no_pending_responses()


def test_more_than_one_image_with_the_name(ec2: EC2Client) -> None:
    other = "ami-0fedcba9876543210"
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", {"Images": [found(), found(image_id=other)]}, BY_NAME)
        with pytest.raises(TemtError, match=f"more than one AMI is named {IMAGE_NAME}"):
            aws.find_image(ec2, IMAGE_NAME)
        stub.assert_no_pending_responses()


def test_an_image_of_another_account_is_not_found_when_own(ec2: EC2Client) -> None:
    """own なら ID でも自分のアカウントの AMI からだけ探す。共有された AMI は返らない。"""
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", {"Images": []}, {**BY_ID, "Owners": ["self"]})
        assert aws.find_image(ec2, IMAGE_ID, own=True) is None
        stub.assert_no_pending_responses()


def test_other_errors_from_reading_the_image_are_left_to_the_caller(ec2: EC2Client) -> None:
    with Stubber(ec2) as stub:
        stub.add_client_error("describe_images", "UnauthorizedOperation", expected_params=BY_ID)
        with pytest.raises(ClientError):
            aws.find_image(ec2, IMAGE_ID)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("value", ["temt-dev-*", "temt-dev-26092?0000"])
def test_a_name_with_a_wildcard_is_refused(ec2: EC2Client, value: str) -> None:
    with Stubber(ec2) as stub:
        with pytest.raises(TemtError, match="not an AMI id or name: "):
            aws.find_image(ec2, value)
        # テンプレートと比べるだけのときは、誤りにせず、使える AMI が無いとする
        assert aws.image_id(ec2, value) is None
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("value", [IMAGE_ID, IMAGE_NAME])
def test_image_id(ec2: EC2Client, value: str) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", FOUND, BY_ID if value == IMAGE_ID else BY_NAME)
        assert aws.image_id(ec2, value) == IMAGE_ID
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("answer", [{"Images": []}, {"Images": [found("failed")]}])
def test_image_id_without_a_usable_image(ec2: EC2Client, answer: dict[str, Any]) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", answer, BY_NAME)
        assert aws.image_id(ec2, IMAGE_NAME) is None
        stub.assert_no_pending_responses()


def test_image_id_of_a_name_that_more_than_one_image_has(ec2: EC2Client) -> None:
    """表示を止めないよう、誤りにせず None にする。何がいけないかは apply が告げる。"""
    other = "ami-0fedcba9876543210"
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", {"Images": [found(), found(image_id=other)]}, BY_NAME)
        assert aws.image_id(ec2, IMAGE_NAME) is None
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 手元のタイムゾーン（テストでは日本時間）で表す
        (datetime(2026, 9, 20, 3, tzinfo=UTC), "2026-09-20 12:00:00"),
        (datetime(2026, 9, 20, 12, tzinfo=timezone(timedelta(hours=-5))), "2026-09-21 02:00:00"),
        # AMI の CreationDate と deploy の記録の時刻は文字列で来る
        ("2026-09-20T03:00:00.000Z", "2026-09-20 12:00:00"),
        ("2026-09-20T03:00:00Z", "2026-09-20 12:00:00"),
        ("not a time", ""),
        # 手元の時刻に直すと年が 9999 を超える
        ("9999-12-31T23:59:59Z", ""),
        # 時差の分からない時刻は読まない
        (datetime(2026, 9, 20, 3), ""),
        ("2026-09-20T03:00:00", ""),
        (None, ""),
    ],
)
def test_cell_time(value: Any, expected: str) -> None:
    assert aws.cell_time(value) == expected
    assert aws.when(value) == (expected or "(unknown)")


def test_tag() -> None:
    item = {"Tags": [{"Key": "Name", "Value": "temt-web"}, {"Key": "temt:service", "Value": "web"}]}
    assert aws.tag(item, "temt:service") == "web"
    assert aws.tag(item, "other") == ""
    assert aws.tag({}, "temt:service") == ""
