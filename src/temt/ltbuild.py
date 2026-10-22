"""設定ファイルの [launch-template] から、起動テンプレートに入れる中身を組み立てる。
design/topics/launch-template.md の Filled when missing、Extensions to AWS's form、
What temt adds。

fill は、書かれていない項目を既定で埋める。apply と diff の手順 2、bake の手順 3、
set-port の確かめ、sg diff と sg show が使う。埋めた値は Conf にだけ入れ、設定ファイルには
書かない。build は、apply と diff の手順 4、bake の手順 3、set-port の確かめが使う。変数を
置き換え、ImageId を AMI の ID に、Groups のセキュリティグループの名前を ID に直し、省いた
DeviceName を AMI のルートボリュームのデバイス名で埋め、temt が足すタグと Groups を
入れる。AWS は読むだけで、何も作らない。temt のセキュリティグループがまだ無ければ、その
ID の代わりに NEW_GROUP を入れる。

ImageId を書いていなければ、使う AMI は build の前に決める（design/topics/ami.md の
Selecting）。--image-id の AMI（given_image）、起動テンプレートの AMI（kept_image）、
起動テンプレートを作るときに選ぶ AMI（new_image）のどれかで、build に渡す。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from temt import aws, conf, ltdata, policy
from temt.aws import field
from temt.msg import TemtError
from temt.names import PREFIX
from temt.table import printable, tilde

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.conf import Conf
    from temt.names import Service

# まだ作っていないセキュリティグループの ID の表示。diff が起動テンプレートの差分に書く
NEW_GROUP = "(new)"
# temt が Name と temt:service のタグを付けるリソースの種類
TAGGED = ("instance", "volume", "network-interface")
# AWS がセキュリティグループの名前に許さない頭。これで始まれば ID とみなす
GROUP_ID_PREFIX = "sg-"
NETWORK_INTERFACES = "NetworkInterfaces"
DEVICE_INDEX = "DeviceIndex"
BLOCK_DEVICES = "BlockDeviceMappings"
IMAGE_ID = "ImageId"
# --image-id。AMI を選び直す方法として、メッセージに書く
IMAGE_ID_OPTION = "--image-id"


@dataclass(frozen=True)
class Image:
    """起動テンプレートに入れる AMI。

    found は describe-images が返す AMI。note は、diff と apply が ImageId の行に括弧で
    添えること。ふつうは AMI の名前で、Ubuntu 24.04 を選んだときはそのわけ。表示できない
    文字は、行を書くときに置き換える。
    """

    found: Mapping[str, Any]
    note: str


def _named(found: Mapping[str, Any]) -> Image:
    """AMI の名前を添える Image。名前が無ければ何も添えない。"""
    return Image(found, str(found.get("Name") or ""))


def given_image(ec2: EC2Client, value: str) -> Image:
    """--image-id の AMI（ID か名前）。見つからないか使えなければ、それを告げて止める。"""
    found = aws.find_image(ec2, value)
    if found is None:
        raise TemtError(f"no such AMI: {printable(value)}")
    state = found.get("State")
    if state not in aws.USABLE_IMAGE_STATES:
        raise TemtError(f"the AMI {printable(value)} is {state}")
    return _named(found)


def kept_image(ec2: EC2Client, service: Service, data: Mapping[str, Any]) -> Image:
    """起動テンプレートの既定バージョン（中身 data）の AMI。ImageId を書いていないときに使う。

    AMI が無いか available でなければ（ami delete --force や手で登録を解除したとき）、
    黙って選び直さず、--image-id で選ぶよう告げて止める。
    """
    value = data.get(IMAGE_ID)
    template = service.launch_template
    again = f"choose one with {IMAGE_ID_OPTION}"
    if not isinstance(value, str) or not value:
        raise TemtError(f"the launch template {template} has no {IMAGE_ID}; {again}")
    found = aws.find_image(ec2, value)
    if found is None:
        raise TemtError(
            f"the AMI {printable(value)} of the launch template {template} is not found; {again}"
        )
    state = found.get("State")
    if state != aws.AVAILABLE:
        raise TemtError(
            f"the AMI {printable(value)} of the launch template {template} is {state}; {again}"
        )
    return _named(found)


def new_image(ec2: EC2Client, ssm: SSMClient, service: Service, region: str) -> Image:
    """起動テンプレートを作るときに使う AMI。

    サービスの最新の AMI（aws.newest_image）を選び、それが pending（コピーの途中など）なら
    止める。1 つも無ければ Ubuntu 24.04 の公開 AMI を選び、そのリージョンにサービスの AMI が
    無いことを添える。AMI をコピーし忘れたクローンが、確認の前に気づけるようにする。
    """
    newest = aws.newest_image(ec2, service.svc)
    if newest is not None:
        if newest.get("State") == aws.PENDING:
            raise TemtError(
                f"the newest AMI of {service.name}, {printable(field(newest, 'Name'))} "
                f"({field(newest, 'ImageId')}), is {aws.PENDING}; run again when it is "
                f"{aws.AVAILABLE}"
            )
        return _named(newest)
    image_id = aws.ubuntu_image_id(ssm)
    found = aws.find_image(ec2, image_id)
    if found is None:
        raise TemtError(f"no such AMI: {printable(image_id)} (Ubuntu 24.04)")
    return Image(found, f"Ubuntu 24.04; no {PREFIX}-{service.svc} AMI in {region}")


@dataclass(frozen=True)
class Built:
    """組み立てた中身と、組み立てるときに分かったこと。

    data は起動テンプレートに入れる中身（temt のセキュリティグループがまだ無ければ、その
    ID は NEW_GROUP）。image は ImageId の AMI で、note は diff の ImageId の行に添えること
    （Image）。vpc_id は NetworkInterfaces の
    DeviceIndex が 0 の要素の SubnetId の VPC で、決まらなければ None。group_id はその
    VPC にある temt のセキュリティグループの ID で、無ければ None。makes_group は temt の
    セキュリティグループを作るか（Groups を temt が埋めるか、名前で指すか）、uses_group は
    起動テンプレートがそれを使うか（ID で書いたものも含む）。root はルートボリュームの
    要素で、無ければ None。ipv6 は、そのサブネットに IPv6 の CIDR が結び付いているか。
    """

    data: dict[str, Any]
    image: Mapping[str, Any]
    vpc_id: str | None
    group_id: str | None
    makes_group: bool
    uses_group: bool
    root: Mapping[str, Any] | None
    ipv6: bool = False
    note: str = ""


def primary(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """NetworkInterfaces の DeviceIndex が 0 の要素。無ければ None。"""
    return ltdata.element(data.get(NETWORK_INTERFACES) or [], DEVICE_INDEX, 0)


def points_at_temt(data: Mapping[str, Any], service: Service) -> bool:
    """IamInstanceProfile が temt のインスタンスプロファイル <名前> を指すか。

    data は変数を置き換える前でも後でもよい。Name か、名前が <名前> の Arn を見る。
    """
    profile = data.get(ltdata.PROFILE)
    if not isinstance(profile, dict):
        return False
    name = ltdata.profile_name(policy.substitute(profile, policy.variables(service)))
    return name == service.name


def _substituted(service: Service, values: Conf) -> dict[str, Any]:
    """[launch-template] のコピーの、変数を置き換えたもの。"""
    try:
        return policy.substitute(copy.deepcopy(values.launch_template), policy.variables(service))
    except ValueError as error:
        raise TemtError(
            f"{printable(tilde(service.conf_path))}: {conf.LAUNCH_TEMPLATE} {error}"
        ) from None


def _written_image(ec2: EC2Client, service: Service, data: Mapping[str, Any]) -> Image:
    """書いた ImageId（変数を置き換えたもの）の AMI。使えなければ、選び直す方法を添えて止める。"""
    key = conf.launch_template_key(IMAGE_ID)
    value = data.get(IMAGE_ID)
    if not isinstance(value, str) or not value:
        raise TemtError(f"no {key} in {printable(tilde(service.conf_path))}")
    return _named(aws.image(ec2, value, conf.source(service, key)))


def subnet(ec2: EC2Client, service: Service, data: Mapping[str, Any]) -> aws.Subnet | None:
    """DeviceIndex が 0 の要素の SubnetId のサブネット。SubnetId が無ければ None。"""
    first = primary(data)
    subnet_id = None if first is None else first.get("SubnetId")
    if not isinstance(subnet_id, str):
        return None
    key = conf.primary_key("SubnetId")
    return aws.subnet(ec2, subnet_id, conf.source(service, key))


# 書いていなければ apply と diff が毎回埋める値。design/topics/launch-template.md の
# Filled when missing。InstanceType の既定は service list も使うので conf が持つ
SHUTDOWN_BEHAVIOR = "terminate"
HTTP_TOKENS = "required"
METADATA_TAGS = "enabled"
CPU_CREDITS = "unlimited"
# サブネットに IPv6 の CIDR が結び付いているときに付ける IPv6 アドレスの数
IPV6_ADDRESSES = 1
# CPU クレジットを持つ T 系のインスタンスの型（t3.micro、t4g.small など）。旧世代の
# t1 は CPU クレジットを持たない
BURSTABLE = re.compile(r"t[2-9]")


def fill(ec2: EC2Client, service: Service, values: Conf) -> aws.Subnet | None:
    """書かれていない項目を既定で埋め、values の [launch-template] に入れる。

    設定ファイルには書かない。共通ファイルに書かれた項目は、書かれているものとする。
    NetworkInterfaces に DeviceIndex が 0 の要素が無ければ足し、その要素に
    NetworkInterfaceId があれば、SubnetId、AssociatePublicIpAddress、Ipv6AddressCount は
    入れない。SubnetId と Ipv6AddressCount は、そのリージョンの AWS を見て決める。
    ImageId は埋めない。

    Ipv6AddressCount を決めるために読んだサブネットを返す。読まなければ None。build に
    渡せば、同じサブネットを読み直さない。
    """
    data = values.launch_template
    data.setdefault("InstanceType", conf.INSTANCE_TYPE)
    interfaces = data.setdefault(NETWORK_INTERFACES, [])
    first: dict[str, Any] | None = next(
        (one for one in interfaces if one.get(DEVICE_INDEX) == 0), None
    )
    if first is None:
        first = {DEVICE_INDEX: 0}
        interfaces.append(first)
    found = None
    if "NetworkInterfaceId" not in first:
        if "SubnetId" not in first:
            first["SubnetId"] = aws.public_subnet(ec2, values.AwsRegion, service.conf_path)
        first.setdefault("AssociatePublicIpAddress", True)
        if "Ipv6AddressCount" not in first:
            found = subnet(ec2, service, data)
            if found is not None and found.ipv6:
                first["Ipv6AddressCount"] = IPV6_ADDRESSES
    metadata = data.setdefault("MetadataOptions", {})
    metadata.setdefault("HttpTokens", HTTP_TOKENS)
    metadata.setdefault("InstanceMetadataTags", METADATA_TAGS)
    data.setdefault("InstanceInitiatedShutdownBehavior", SHUTDOWN_BEHAVIOR)
    if BURSTABLE.match(str(data["InstanceType"])):
        data.setdefault("CreditSpecification", {}).setdefault("CpuCredits", CPU_CREDITS)
    return found


def _root(
    service: Service, data: dict[str, Any], image: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """省いた DeviceName を AMI のルートボリュームのデバイス名で埋め、ルートボリュームの要素を
    返す。大きさがルートボリュームより小さければ止める。

    埋めた値は設定ファイルに書かない。ImageId を別の OS の AMI に替えても、ルート
    ボリュームを指し続ける。
    """
    mappings = data.get(BLOCK_DEVICES) or []
    device = image.get("RootDeviceName")
    image_id = field(image, "ImageId")
    where = printable(tilde(service.conf_path))
    for one in mappings:
        if "DeviceName" not in one:
            if not isinstance(device, str) or not device:
                raise TemtError(
                    f"{where}: the AMI {image_id} has no root device name; write DeviceName in "
                    f"{conf.launch_template_key(BLOCK_DEVICES)}"
                )
            one["DeviceName"] = device
    names = [one.get("DeviceName") for one in mappings]
    for name in names:
        if names.count(name) > 1:
            raise TemtError(
                f"{where}: {conf.launch_template_key(BLOCK_DEVICES)} has two elements for "
                f"{printable(str(name))} (an element without DeviceName means {device})"
            )
    root = ltdata.element(mappings, "DeviceName", device)
    if root is None:
        return None
    size = (root.get("Ebs") or {}).get("VolumeSize")
    if size is None:
        return root
    key = conf.launch_template_key(f"{BLOCK_DEVICES}[DeviceName={device}].Ebs.VolumeSize")
    found = aws.root_volume(image)
    source = printable(tilde(conf.source(service, conf.launch_template_key(BLOCK_DEVICES))))
    if found is None:
        raise TemtError(
            f"the AMI {image_id} has no EBS root volume; remove Ebs.VolumeSize from {source}"
        )
    if size < found.size:
        raise TemtError(
            f"{key} {size} is smaller than the root volume of {image_id} ({found.size} GiB); "
            f"raise it or remove it in {source}"
        )
    return root


def _temt_group(ec2: EC2Client, service: Service, vpc_id: str | None) -> str | None:
    """その VPC にある temt のセキュリティグループ <名前> の ID。無ければ None。"""
    if vpc_id is None:
        return None
    found = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [service.name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    ).get("SecurityGroups", [])
    return field(found[0], "GroupId") if found else None


def _group_id(ec2: EC2Client, service: Service, vpc_id: str | None, name: str) -> str:
    """セキュリティグループの名前を、その VPC の中の ID に直す。無ければ止める。"""
    if vpc_id is None:
        raise TemtError(
            f"cannot find the security group {printable(name)} without "
            f"{conf.primary_key('SubnetId')}; write its id in Groups"
        )
    found = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    ).get("SecurityGroups", [])
    if not found:
        source = conf.source(service, conf.launch_template_key(NETWORK_INTERFACES))
        raise TemtError(
            f"no security group named {printable(name)} in {vpc_id}; fix Groups in "
            f"{printable(tilde(source))}"
        )
    return field(found[0], "GroupId")


def _groups(
    ec2: EC2Client, service: Service, data: dict[str, Any], vpc_id: str | None
) -> tuple[str | None, bool, bool]:
    """Groups の名前を ID に直し、temt が埋める Groups を入れる。

    temt のセキュリティグループの ID（無ければ None）、作るか、使うかを返す。作るのは、
    DeviceIndex が 0 の要素の Groups を temt が埋めるか、Groups が名前で指すときだけ
    （名前だけで決める）。使うのは、それに加えて、ID で書いた Groups に入っているとき。
    """
    temt_id = _temt_group(ec2, service, vpc_id)
    placed = temt_id or NEW_GROUP
    named = False
    used = False
    for one in data.get(NETWORK_INTERFACES) or []:
        groups = one.get("Groups")
        if not isinstance(groups, list):
            continue
        resolved: list[str] = []
        for group in groups:
            if group.startswith(GROUP_ID_PREFIX):
                resolved.append(group)
                used = used or group == temt_id
            elif group == service.name and vpc_id is not None:
                named = True
                resolved.append(placed)
            else:
                resolved.append(_group_id(ec2, service, vpc_id, group))
        one["Groups"] = resolved
    top = data.get("SecurityGroupIds") or []
    used = used or (temt_id is not None and temt_id in top)
    # 最上位の SecurityGroups は名前で書く
    used = used or service.name in (data.get("SecurityGroups") or [])
    first = primary(data)
    # VPC が決まらなければ、temt のセキュリティグループを置けない
    fills = (
        vpc_id is not None
        and "SecurityGroupIds" not in data
        and "SecurityGroups" not in data
        and (first is None or ("Groups" not in first and "NetworkInterfaceId" not in first))
    )
    if fills:
        if first is None:
            data.setdefault(NETWORK_INTERFACES, []).append({DEVICE_INDEX: 0})
            first = primary(data)
        if first is not None:
            first["Groups"] = [placed]  # pyright: ignore[reportIndexIssue]
    makes = (fills or named) and vpc_id is not None
    return temt_id, makes, used or makes


def _tags(service: Service, data: dict[str, Any]) -> None:
    """インスタンス、ボリューム、ネットワークインターフェースのタグを入れる。

    temt:service は書いてあるかどうかに関係なく付け、Name は書いていなければ <名前> を
    付ける。種類の要素が無ければ足す。
    """
    specifications = data.setdefault("TagSpecifications", [])
    for kind in TAGGED:
        one = ltdata.element(specifications, "ResourceType", kind)
        if one is None:
            one = {"ResourceType": kind, "Tags": []}
            specifications.append(one)
        tags = [tag for tag in one.get("Tags") or [] if tag.get("Key") != aws.TAG_SERVICE]
        if not any(tag.get("Key") == "Name" for tag in tags):
            tags.insert(0, {"Key": "Name", "Value": service.name})
        tags.append({"Key": aws.TAG_SERVICE, "Value": service.svc})
        one["Tags"] = tags  # pyright: ignore[reportIndexIssue]


def build(
    ec2: EC2Client,
    service: Service,
    values: Conf,
    *,
    image: Image | None = None,
    known: aws.Subnet | None = None,
) -> Built:
    """設定ファイルから、起動テンプレートに入れる中身を組み立てる。

    values は fill で埋めたもの。image は ImageId を書いていないときに使う AMI で、書いて
    いれば None（書いた ImageId の AMI を引く）。known は fill が読んだサブネットで、あれば
    読み直さない。
    """
    data = _substituted(service, values)
    if image is None:
        image = _written_image(ec2, service, data)
    data[IMAGE_ID] = field(image.found, "ImageId")
    found = known or subnet(ec2, service, data)
    vpc_id = None if found is None else found.vpc_id
    root = _root(service, data, image.found)
    group_id, makes, uses = _groups(ec2, service, data, vpc_id)
    _tags(service, data)
    ipv6 = found is not None and found.ipv6
    return Built(data, image.found, vpc_id, group_id, makes, uses, root, ipv6, image.note)


def with_group(data: Mapping[str, Any], group_id: str) -> dict[str, Any]:
    """中身の NEW_GROUP を、作ったセキュリティグループの ID に置き換えたもの。"""
    made = copy.deepcopy(dict(data))
    for one in made.get(NETWORK_INTERFACES) or []:
        if isinstance(one.get("Groups"), list):
            one["Groups"] = [group_id if group == NEW_GROUP else group for group in one["Groups"]]
    return made
