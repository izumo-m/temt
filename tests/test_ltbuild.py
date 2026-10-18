"""起動テンプレートの中身を組み立てる処理のテスト。AWS は Stubber で差し替える。"""

import copy
from typing import Any

import pytest
from botocore.stub import Stubber
from types_boto3_ec2.client import EC2Client

from temt import ltbuild
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
VPC = "vpc-0123456789abcdef0"
SUBNET = "subnet-0123456789abcdef0"
# temt のセキュリティグループと、temt のものではないもの
GROUP = "sg-0123456789abcdef0"
WEB = "sg-00000000000000077"
IMAGE = "ami-0123456789abcdef0"
IMAGE_NAME = "temt-dev-main-2609200900"
ROOT_DEVICE = "/dev/sda1"

# temt が足すタグ
NAME_TAG = {"Key": "Name", "Value": NAME}
MARK = {"Key": "temt:service", "Value": "dev-main"}
# DeviceIndex が 0 の要素の、SubnetId だけを書いたもの
PRIMARY: dict[str, Any] = {"DeviceIndex": 0, "SubnetId": SUBNET}


def an_image(*, size: int | None = 8, device: str | None = ROOT_DEVICE) -> dict[str, Any]:
    """describe-images が返す AMI。size はルートボリュームの GiB で、None なら EBS の
    ルートボリュームを持たない。device はルートデバイスの名前で、None なら持たない。
    """
    image: dict[str, Any] = {"ImageId": IMAGE, "Name": IMAGE_NAME, "State": "available"}
    if device is not None:
        image["RootDeviceName"] = device
    if size is not None:
        image["BlockDeviceMappings"] = [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": size}}]
    return image


def in_vpc(name: str) -> dict[str, Any]:
    """名前と VPC で絞った describe-security-groups の引数。"""
    return {
        "Filters": [
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [VPC]},
        ]
    }


def a_group(group_id: str, name: str) -> dict[str, Any]:
    """describe-security-groups が返す 1 つ分。"""
    return {"GroupId": group_id, "GroupName": name, "VpcId": VPC}


def read(
    stub: Stubber,
    *,
    image: dict[str, Any] | None = None,
    by_name: bool = False,
    subnet: bool = True,
    groups: bool = True,
    group: str | None = GROUP,
) -> None:
    """build が読むものを積む。ImageId の AMI（by_name なら名前で引く）、subnet なら
    SubnetId のサブネット、groups ならその VPC にある temt のセキュリティグループ（group が
    None なら無い）。ルートボリュームを確かめて止まるときは、セキュリティグループを読まない。
    """
    params = (
        {"Owners": ["self"], "Filters": [{"Name": "name", "Values": [IMAGE_NAME]}]}
        if by_name
        else {"ImageIds": [IMAGE]}
    )
    stub.add_response("describe_images", {"Images": [image or an_image()]}, params)
    if not subnet:
        return
    stub.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "10.0.0.0/24"}]},
        {"Filters": [{"Name": "subnet-id", "Values": [SUBNET]}]},
    )
    if not groups:
        return
    found = [] if group is None else [a_group(group, NAME)]
    stub.add_response("describe_security_groups", {"SecurityGroups": found}, in_vpc(NAME))


def values(**data: Any) -> Conf:
    """[launch-template] に data を書いた設定。ImageId を省けば IMAGE、NetworkInterfaces を
    省けば PRIMARY だけを書く。
    """
    content: dict[str, Any] = {"ImageId": IMAGE, "NetworkInterfaces": [dict(PRIMARY)], **data}
    return Conf(launch_template={key: value for key, value in content.items() if value is not None})


def built(ec2: EC2Client, service: Service, conf: Conf, **reads: Any) -> ltbuild.Built:
    """reads のとおりに読むものを積み、中身を組み立てる。積んだ応答はすべて使われる。

    組み立てても設定は書き換わらない。
    """
    before = copy.deepcopy(conf)
    with Stubber(ec2) as stub:
        read(stub, **reads)
        found = ltbuild.build(ec2, service, conf)
        stub.assert_no_pending_responses()
    assert conf == before
    return found


def failed(ec2: EC2Client, service: Service, conf: Conf, **reads: Any) -> str:
    """reads のとおりに読むものを積んで組み立て、止まったときの誤りの文を返す。"""
    with Stubber(ec2) as stub:
        read(stub, **reads)
        with pytest.raises(TemtError) as caught:
            _ = ltbuild.build(ec2, service, conf)
        stub.assert_no_pending_responses()
    return str(caught.value)


def test_temt_adds_its_tags_and_groups_to_the_config_file(ec2: EC2Client, service: Service) -> None:
    """設定ファイルの中身に、変数を置き換え、temt が足すものを入れる。

    Groups は temt のセキュリティグループで埋め、インスタンス、ボリューム、ネットワーク
    インターフェースに Name と temt:service のタグを付ける。
    """
    conf = values(
        InstanceType="t3.micro",
        IamInstanceProfile={"Name": "${temt:name}"},
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": "${temt:service}"}]}
        ],
    )

    found = built(ec2, service, conf)

    assert found.data == {
        "ImageId": IMAGE,
        "NetworkInterfaces": [{**PRIMARY, "Groups": [GROUP]}],
        "InstanceType": "t3.micro",
        "IamInstanceProfile": {"Name": NAME},
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [NAME_TAG, {"Key": "Env", "Value": "dev-main"}, MARK],
            },
            {"ResourceType": "volume", "Tags": [NAME_TAG, MARK]},
            {"ResourceType": "network-interface", "Tags": [NAME_TAG, MARK]},
        ],
    }
    assert found.image["ImageId"] == IMAGE
    assert (found.vpc_id, found.group_id) == (VPC, GROUP)
    assert (found.makes_group, found.uses_group) == (True, True)
    assert found.root is None


def test_an_ami_name_becomes_its_id(ec2: EC2Client, service: Service) -> None:
    """ImageId に書いた AMI の名前は、自分のアカウントの AMI から引いて ID に直す。"""
    found = built(ec2, service, values(ImageId=IMAGE_NAME), by_name=True)
    assert found.data["ImageId"] == IMAGE


def test_no_image_stops(ec2: EC2Client, service: Service) -> None:
    """ImageId が無ければ、AWS を読まずに止まる。apply は手順 2 で書いている。"""
    with Stubber(ec2), pytest.raises(TemtError) as caught:
        _ = ltbuild.build(ec2, service, values(ImageId=None))
    assert str(caught.value) == f"no launch-template.ImageId in {service.conf_path}"


def test_an_ami_that_is_gone_stops(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        stub.add_response("describe_images", {"Images": []}, {"ImageIds": [IMAGE]})
        with pytest.raises(TemtError, match=f"^no such AMI: {IMAGE}; remove launch-template"):
            _ = ltbuild.build(ec2, service, values())
        stub.assert_no_pending_responses()


def test_a_missing_group_of_temt_is_new(ec2: EC2Client, service: Service) -> None:
    """VPC に temt のセキュリティグループがまだ無ければ、その ID の代わりに NEW_GROUP を
    入れ、作るものとする。
    """
    found = built(ec2, service, values(), group=None)
    assert found.data["NetworkInterfaces"] == [{**PRIMARY, "Groups": [ltbuild.NEW_GROUP]}]
    assert found.group_id is None
    assert (found.makes_group, found.uses_group) == (True, True)


@pytest.mark.parametrize("temt", ["${temt:name}", NAME])
def test_groups_are_resolved_by_name_in_the_vpc(
    ec2: EC2Client, service: Service, temt: str
) -> None:
    """Groups の名前は、SubnetId の VPC の中で ID に直す。sg- で始まれば ID のまま使う。
    temt のセキュリティグループは、名前で書けば作るものになる。書いた順は変えない。
    """
    primary = {**PRIMARY, "Groups": ["web", "sg-0000000000000aaaa", temt]}
    with Stubber(ec2) as stub:
        read(stub, group=None)
        stub.add_response(
            "describe_security_groups", {"SecurityGroups": [a_group(WEB, "web")]}, in_vpc("web")
        )
        found = ltbuild.build(ec2, service, values(NetworkInterfaces=[primary]))
        stub.assert_no_pending_responses()
    assert found.data["NetworkInterfaces"] == [
        {**PRIMARY, "Groups": [WEB, "sg-0000000000000aaaa", ltbuild.NEW_GROUP]}
    ]
    assert (found.makes_group, found.uses_group) == (True, True)


def test_a_group_name_not_in_the_vpc_stops(ec2: EC2Client, service: Service) -> None:
    primary = {**PRIMARY, "Groups": ["web"]}
    with Stubber(ec2) as stub:
        read(stub)
        stub.add_response("describe_security_groups", {"SecurityGroups": []}, in_vpc("web"))
        with pytest.raises(TemtError) as caught:
            _ = ltbuild.build(ec2, service, values(NetworkInterfaces=[primary]))
        stub.assert_no_pending_responses()
    assert str(caught.value) == (
        f"no security group named web in {VPC}; fix Groups in {service.conf_path}"
    )


def test_a_group_name_without_a_subnet_stops(ec2: EC2Client, service: Service) -> None:
    """SubnetId が無ければ VPC が決まらず、名前を ID に直せない。"""
    conf = values(NetworkInterfaces=[{"DeviceIndex": 0, "Groups": ["web"]}])
    assert failed(ec2, service, conf, subnet=False) == (
        "cannot find the security group web without "
        "launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId; write its id in Groups"
    )


def test_the_name_of_temt_without_a_subnet_stops(ec2: EC2Client, service: Service) -> None:
    """VPC が決まらなければ、temt のセキュリティグループも名前で指せない。(new) のまま
    起動テンプレートに入れない。
    """
    conf = values(
        NetworkInterfaces=[
            {"DeviceIndex": 0, "NetworkInterfaceId": "eni-0123456789abcdef0"},
            {"DeviceIndex": 1, "Groups": ["${temt:name}"]},
        ]
    )
    assert failed(ec2, service, conf, subnet=False) == (
        f"cannot find the security group {NAME} without "
        "launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId; write its id in Groups"
    )


@pytest.mark.parametrize(
    ("group", "used"),
    [(WEB, False), (GROUP, True)],
    ids=["another group", "the group of temt"],
)
def test_groups_written_by_id(ec2: EC2Client, service: Service, group: str, used: bool) -> None:
    """Groups を ID だけで書けば、temt のセキュリティグループを作らない。temt のものの ID を
    書いたときだけ、それを使う（受信規則を揃える）ものとする。
    """
    found = built(ec2, service, values(NetworkInterfaces=[{**PRIMARY, "Groups": [group]}]))
    assert found.data["NetworkInterfaces"] == [{**PRIMARY, "Groups": [group]}]
    assert found.group_id == GROUP
    assert (found.makes_group, found.uses_group) == (False, used)


@pytest.mark.parametrize(
    ("data", "used"),
    [
        pytest.param({"SecurityGroupIds": [GROUP]}, True, id="the id of temt"),
        pytest.param({"SecurityGroupIds": [WEB]}, False, id="another id"),
        pytest.param({"SecurityGroups": ["default"]}, False, id="a name"),
        pytest.param({"SecurityGroups": [NAME]}, True, id="the name of temt"),
    ],
)
def test_top_level_security_groups_leave_groups_alone(
    ec2: EC2Client, service: Service, data: dict[str, Any], used: bool
) -> None:
    """最上位に SecurityGroupIds か SecurityGroups を書けば、Groups を埋めない。AWS は両方を
    書いたものを断る。
    """
    found = built(ec2, service, values(**data))
    assert found.data["NetworkInterfaces"] == [PRIMARY]
    assert (found.makes_group, found.uses_group) == (False, used)


def test_an_existing_network_interface_gets_no_groups(ec2: EC2Client, service: Service) -> None:
    """既存のネットワークインターフェースを付けるなら、Groups を埋めない。サブネットも
    セキュリティグループも読まない。
    """
    primary = {"DeviceIndex": 0, "NetworkInterfaceId": "eni-0123456789abcdef0"}
    found = built(ec2, service, values(NetworkInterfaces=[primary]), subnet=False)
    assert found.data["NetworkInterfaces"] == [primary]
    assert found.vpc_id is None
    assert (found.makes_group, found.uses_group) == (False, False)


def test_only_the_primary_element_gets_groups(ec2: EC2Client, service: Service) -> None:
    """Groups を埋めるのは、DeviceIndex が 0 の要素だけである。"""
    secondary = {"DeviceIndex": 1, "SubnetId": SUBNET}
    found = built(ec2, service, values(NetworkInterfaces=[secondary, dict(PRIMARY)]))
    assert found.data["NetworkInterfaces"] == [secondary, {**PRIMARY, "Groups": [GROUP]}]


def test_an_omitted_device_name_is_the_root_device(ec2: EC2Client, service: Service) -> None:
    """DeviceName を省いた要素には、AMI のルートデバイスの名前を入れる。設定には書かない。"""
    conf = values(BlockDeviceMappings=[{"Ebs": {"VolumeSize": 30}}])

    found = built(ec2, service, conf)

    root = {"Ebs": {"VolumeSize": 30}, "DeviceName": ROOT_DEVICE}
    assert found.data["BlockDeviceMappings"] == [root]
    assert found.root == root


@pytest.mark.parametrize("size", [8, 30])
def test_a_volume_size_as_large_as_the_ami_is_accepted(
    ec2: EC2Client, service: Service, size: int
) -> None:
    conf = values(BlockDeviceMappings=[{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": size}}])
    found = built(ec2, service, conf, image=an_image(size=8))
    assert found.root == {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": size}}


@pytest.mark.parametrize(
    "mapping",
    [{"Ebs": {"VolumeSize": 6}}, {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 6}}],
    ids=["DeviceName omitted", "DeviceName written"],
)
def test_a_volume_size_smaller_than_the_ami_stops(
    ec2: EC2Client, service: Service, mapping: dict[str, Any]
) -> None:
    """AWS は小さい値をテンプレートには受け付け、起動するときに断るので、その前に止まる。"""
    conf = values(BlockDeviceMappings=[mapping])
    assert failed(ec2, service, conf, groups=False) == (
        f"launch-template.BlockDeviceMappings[DeviceName={ROOT_DEVICE}].Ebs.VolumeSize 6 is "
        f"smaller than the root volume of {IMAGE} (8 GiB); raise it or remove it in "
        f"{service.conf_path}"
    )


def test_a_volume_size_without_an_ebs_root_volume_stops(ec2: EC2Client, service: Service) -> None:
    conf = values(BlockDeviceMappings=[{"Ebs": {"VolumeSize": 30}}])
    assert failed(ec2, service, conf, image=an_image(size=None), groups=False) == (
        f"the AMI {IMAGE} has no EBS root volume; remove Ebs.VolumeSize from {service.conf_path}"
    )


def test_another_device_is_not_the_root_volume(ec2: EC2Client, service: Service) -> None:
    """ルートデバイスでない名前は、足すディスクなので、AMI と大きさを比べない。"""
    mapping = {"DeviceName": "/dev/sdb", "Ebs": {"VolumeSize": 1}}
    found = built(ec2, service, values(BlockDeviceMappings=[mapping]))
    assert found.data["BlockDeviceMappings"] == [mapping]
    assert found.root is None


def test_a_device_name_that_collides_after_filling_stops(ec2: EC2Client, service: Service) -> None:
    conf = values(
        BlockDeviceMappings=[
            {"Ebs": {"VolumeSize": 30}},
            {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeType": "gp3"}},
        ]
    )
    assert failed(ec2, service, conf, groups=False) == (
        f"{service.conf_path}: launch-template.BlockDeviceMappings has two elements for "
        f"{ROOT_DEVICE} (an element without DeviceName means {ROOT_DEVICE})"
    )


def test_an_ami_without_a_root_device_name_stops(ec2: EC2Client, service: Service) -> None:
    """AMI がルートデバイスの名前を持たなければ、省いた DeviceName を埋められない。"""
    conf = values(BlockDeviceMappings=[{"Ebs": {"VolumeSize": 30}}])
    assert failed(ec2, service, conf, image=an_image(device=None), groups=False) == (
        f"{service.conf_path}: the AMI {IMAGE} has no root device name; write DeviceName in "
        "launch-template.BlockDeviceMappings"
    )


def test_tags_written_are_kept_and_temt_service_is_always_temts(
    ec2: EC2Client, service: Service
) -> None:
    """書いた Name はそのまま、temt:service は書いた値にかかわらず temt の値にする。
    temt がタグを付けない種類の要素には触らない。
    """
    conf = values(
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": "mine"},
                    {"Key": "temt:service", "Value": "other"},
                    {"Key": "Env", "Value": "demo"},
                ],
            },
            {"ResourceType": "spot-instances-request", "Tags": [{"Key": "Env", "Value": "demo"}]},
        ]
    )

    found = built(ec2, service, conf)

    assert found.data["TagSpecifications"] == [
        {
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": "mine"}, {"Key": "Env", "Value": "demo"}, MARK],
        },
        {"ResourceType": "spot-instances-request", "Tags": [{"Key": "Env", "Value": "demo"}]},
        {"ResourceType": "volume", "Tags": [NAME_TAG, MARK]},
        {"ResourceType": "network-interface", "Tags": [NAME_TAG, MARK]},
    ]


@pytest.mark.parametrize(
    ("profile", "points"),
    [
        pytest.param({"Name": "${temt:name}"}, True, id="the variable"),
        pytest.param({"Name": NAME}, True, id="the name"),
        pytest.param(
            {"Arn": f"arn:aws:iam::123456789012:instance-profile/{NAME}"}, True, id="the arn"
        ),
        pytest.param(
            {"Arn": f"arn:aws:iam::123456789012:instance-profile/team/{NAME}"},
            True,
            id="an arn with a path",
        ),
        pytest.param({"Name": "other"}, False, id="another"),
        pytest.param({"Name": "${temt:service}"}, False, id="the service name"),
        pytest.param(None, False, id="not written"),
    ],
)
def test_points_at_temt(service: Service, profile: dict[str, str] | None, points: bool) -> None:
    """IamInstanceProfile が temt のインスタンスプロファイル <名前> を指すか。変数を置き換える
    前でも後でもよい。
    """
    data = {} if profile is None else {"IamInstanceProfile": profile}
    assert ltbuild.points_at_temt(data, service) is points


def test_primary_is_the_element_with_device_index_zero() -> None:
    first = {"DeviceIndex": 0, "SubnetId": SUBNET}
    assert ltbuild.primary({"NetworkInterfaces": [{"DeviceIndex": 1}, first]}) == first
    assert ltbuild.primary({"NetworkInterfaces": [{"DeviceIndex": 1}]}) is None
    assert ltbuild.primary({}) is None


def test_with_group_puts_the_created_id_in_place_of_new_group() -> None:
    """すべての要素の NEW_GROUP を置き換え、ほかの ID は残す。元の中身は書き換えない。"""
    data = {
        "NetworkInterfaces": [
            {"DeviceIndex": 0, "Groups": [ltbuild.NEW_GROUP, WEB]},
            {"DeviceIndex": 1, "Groups": [ltbuild.NEW_GROUP]},
            {"DeviceIndex": 2},
        ],
        "InstanceType": "t3.micro",
    }
    before = copy.deepcopy(data)

    made = ltbuild.with_group(data, GROUP)

    assert made == {
        "NetworkInterfaces": [
            {"DeviceIndex": 0, "Groups": [GROUP, WEB]},
            {"DeviceIndex": 1, "Groups": [GROUP]},
            {"DeviceIndex": 2},
        ],
        "InstanceType": "t3.micro",
    }
    assert data == before


@pytest.mark.parametrize(
    ("data", "ipv6", "missing"),
    [
        pytest.param({}, False, [], id="all written"),
        pytest.param({"InstanceType": None}, False, ["InstanceType"], id="instance type"),
        pytest.param(
            {"NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": SUBNET}]},
            False,
            ["NetworkInterfaces[DeviceIndex=0].AssociatePublicIpAddress"],
            id="public address",
        ),
        pytest.param(
            {}, True, ["NetworkInterfaces[DeviceIndex=0].Ipv6AddressCount"], id="ipv6 subnet"
        ),
        pytest.param(
            {"CreditSpecification": None}, False, ["CreditSpecification.CpuCredits"], id="credits"
        ),
        pytest.param(
            {"NetworkInterfaces": [{"DeviceIndex": 0, "NetworkInterfaceId": "eni-1"}]},
            True,
            [],
            id="existing interface",
        ),
    ],
)
def test_unfilled_lists_what_apply_has_not_written(
    data: dict[str, Any], ipv6: bool, missing: list[str]
) -> None:
    """書かなければ apply が書く項目のうち、まだ書かれていないものを挙げる。bake と
    set-port が、先に apply するよう促すのに使う。
    """
    written: dict[str, Any] = {
        "ImageId": IMAGE,
        "InstanceType": "t3.micro",
        "InstanceInitiatedShutdownBehavior": "terminate",
        "MetadataOptions": {"HttpTokens": "required", "InstanceMetadataTags": "enabled"},
        "CreditSpecification": {"CpuCredits": "unlimited"},
        "NetworkInterfaces": [{**PRIMARY, "AssociatePublicIpAddress": True}],
    }
    written.update(data)
    conf = Conf(launch_template={key: value for key, value in written.items() if value is not None})
    assert ltbuild.unfilled(conf, ipv6=ipv6) == missing
