"""service import の本体。設計の手順 1〜7 を順に行う（design/commands/service.md の
service import）。

止まっている既存のインスタンスのルートボリュームから AMI を作り、同じインスタンスを
その AMI から起動できる設定ファイルを書く。起動テンプレートは、続けて打つ apply が作る。
元のインスタンスと、それが使う AWS のリソースには何もしない。リージョンとプロファイルは
apply と同じく設定ファイルに書かず、共通ファイルも読むままにする。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import tomlkit
from botocore.exceptions import BotoCoreError, ClientError

from temt import aws, bake, conf, lock, ltdata, msg, resources
from temt.aws import error_code, field
from temt.msg import TemtError
from temt.names import PREFIX, create_text, make_dir
from temt.table import printable, tilde

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import boto3
    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.names import Service

STOPPED = "stopped"
# 見つからないインスタンス ID を指したときの誤りの名前
NO_SUCH_INSTANCE = "InvalidInstanceID.NotFound"
# コピーしない項目。design の What is copied
DROPPED = (
    "BlockDeviceMappings",
    "InstanceMarketOptions",
    "DisableApiTermination",
    "DisableApiStop",
)
# ネットワークインターフェースの要素から消し、数に直す項目
COUNTED = {
    "Ipv4Prefixes": "Ipv4PrefixCount",
    "Ipv6Prefixes": "Ipv6PrefixCount",
}
# AWS が予約しているタグのキーの頭。書けない
RESERVED_TAG = "aws:"
# 元の値が読めないときに書く、AWS の既定（その項目を書かずに起動したときの値）
AWS_DEFAULTS = {
    "HttpTokens": "optional",
    "InstanceMetadataTags": "disabled",
}
# 取り込んだサービスの起動テンプレートに書く、インスタンスの中からの電源断のふるまい
SHUTDOWN_BEHAVIOR = "terminate"
# CPU クレジットの AWS の既定。t2 は standard、ほかの T 系は unlimited
T2 = "t2."
# service import が新しく作る設定ファイルの先頭のコメント
HEADER = "# Written by temt service import. Edit and run apply to update.\n"
# AMI に入れない EBS に添える文
NOT_IN_AMI = (
    "not put in the AMI; to use one, detach it from the source instance and attach it from "
    "inside the instance"
)


def _source(ec2: EC2Client, instance_id: str) -> Mapping[str, Any]:
    """元のインスタンス。無ければ止める。止まっていなければ、止めるよう告げて止める。"""
    try:
        found = ec2.describe_instances(InstanceIds=[instance_id])
    except ClientError as e:
        if error_code(e) != NO_SUCH_INSTANCE:
            raise
        raise TemtError(f"no such instance: {instance_id}") from None
    items = [
        item
        for reservation in found.get("Reservations") or []
        for item in reservation.get("Instances") or []
    ]
    if not items:
        raise TemtError(f"no such instance: {instance_id}")
    item = items[0]
    state = (item.get("State") or {}).get("Name")
    if state != STOPPED:
        raise TemtError(
            f"{instance_id} is {printable(str(state))}; stop it first (service import changes "
            "nothing on it)"
        )
    return item


def _public(ec2: EC2Client, interface_id: str) -> bool:
    """ENI の起動時の AssociatePublicIpAddress。止めたインスタンスでも持ち続けている。"""
    found = ec2.describe_network_interface_attribute(
        NetworkInterfaceId=interface_id, Attribute="associatePublicIpAddress"
    )
    return bool(found.get("AssociatePublicIpAddress"))


def _interfaces(ec2: EC2Client, item: Mapping[str, Any]) -> dict[int, str]:
    """DeviceIndex から、元のインスタンスの ENI の ID への対応。"""
    return {
        int((one.get("Attachment") or {}).get("DeviceIndex", 0)): field(one, "NetworkInterfaceId")
        for one in item.get("NetworkInterfaces") or []
    }


def _interface(ec2: EC2Client, one: dict[str, Any], ids: Mapping[int, str]) -> dict[str, Any]:
    """ネットワークインターフェースの要素 1 つを、コピーできる形にする。

    アドレスは値をコピーせず数にし（元のインスタンスが持ち続けるので、コピーすると start が
    断られる）、AssociatePublicIpAddress は ENI の属性から読み、DeleteOnTermination は
    true にする。
    """
    made = dict(one)
    secondary = [
        address
        for address in made.pop("PrivateIpAddresses", None) or []
        if not address.get("Primary")
    ]
    if secondary:
        made["SecondaryPrivateIpAddressCount"] = len(secondary)
    made.pop("PrivateIpAddress", None)
    made.pop("NetworkInterfaceId", None)
    for listed, counted in COUNTED.items():
        values = made.pop(listed, None) or []
        if values:
            made[counted] = len(values)
    made["Ipv6AddressCount"] = len(made.pop("Ipv6Addresses", None) or [])
    made["DeleteOnTermination"] = True
    interface_id = ids.get(int(made.get("DeviceIndex", 0)))
    if interface_id is not None:
        made["AssociatePublicIpAddress"] = _public(ec2, interface_id)
    return made


def copied(
    ec2: EC2Client, item: Mapping[str, Any], data: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """get-launch-template-data の中身を、[launch-template] にコピーできる形に直す。

    消した aws: のタグのキーも返す。ImageId は呼ぶ側が、作る AMI の名前にする。
    """
    made = copy.deepcopy(dict(data))
    for key in DROPPED:
        made.pop(key, None)
    placement = made.get("Placement")
    if isinstance(placement, dict):
        placement.pop("AvailabilityZoneId", None)
    metadata = made.setdefault("MetadataOptions", {})
    metadata.pop("State", None)
    for key, value in AWS_DEFAULTS.items():
        metadata.setdefault(key, value)
    operator = made.get("Operator")
    if isinstance(operator, dict):
        operator.pop("HiddenByDefault", None)
        operator.pop("Managed", None)
    ids = _interfaces(ec2, item)
    made["NetworkInterfaces"] = [
        _interface(ec2, one, ids) for one in made.get("NetworkInterfaces") or []
    ]
    made["InstanceInitiatedShutdownBehavior"] = SHUTDOWN_BEHAVIOR
    instance_type = str(made.get("InstanceType") or "")
    if resources.BURSTABLE.match(instance_type) and not made.get("CreditSpecification"):
        made["CreditSpecification"] = {
            "CpuCredits": "standard" if instance_type.startswith(T2) else "unlimited"
        }
    removed: list[str] = []
    specifications = []
    for specification in made.get("TagSpecifications") or []:
        kept = []
        for tag in specification.get("Tags") or []:
            if str(tag.get("Key", "")).startswith(RESERVED_TAG):
                removed.append(str(tag.get("Key")))
            else:
                kept.append(tag)
        if kept:
            specifications.append({**specification, "Tags": kept})
    made["TagSpecifications"] = specifications
    return ltdata.without_blanks(made), removed


def _others(item: Mapping[str, Any], data: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    """ルートボリューム以外の EBS。デバイス名、ボリューム ID、大きさ（GiB）の組。

    デバイス名とボリューム ID は describe-instances から、大きさは
    get-launch-template-data から取り、AWS への問い合わせを増やさない。
    """
    sizes = {
        one.get("DeviceName"): (one.get("Ebs") or {}).get("VolumeSize")
        for one in data.get("BlockDeviceMappings") or []
    }
    root = item.get("RootDeviceName")
    return [
        (
            field(one, "DeviceName"),
            str((one.get("Ebs") or {}).get("VolumeId") or "?"),
            sizes.get(one.get("DeviceName")),
        )
        for one in item.get("BlockDeviceMappings") or []
        if one.get("Ebs") is not None and one.get("DeviceName") != root
    ]


def _addresses(item: Mapping[str, Any]) -> list[str]:
    """元のインスタンスのプライベート IP アドレスと IPv6 アドレス。"""
    found: list[str] = []
    for one in item.get("NetworkInterfaces") or []:
        found += [
            str(address.get("PrivateIpAddress"))
            for address in one.get("PrivateIpAddresses") or []
            if address.get("PrivateIpAddress")
        ]
        found += [
            str(address.get("Ipv6Address"))
            for address in one.get("Ipv6Addresses") or []
            if address.get("Ipv6Address")
        ]
    return found


def _registered(ssm: SSMClient, instance_id: str) -> bool:
    """元のインスタンスが SSM に登録されているか。"""
    found = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )
    return bool(found.get("InstanceInformationList"))


def _text(data: Mapping[str, Any], *, registered: bool) -> str:
    """書く設定ファイルの中身。最上位には、SSM に登録されていないときの WaitForSsm だけを
    書く。
    """
    document = tomlkit.parse(HEADER)
    if not registered:
        document["WaitForSsm"] = False
    text = tomlkit.dumps(document)
    body = "\n".join(ltdata.render(data))
    return f"{text}\n{body}\n"


def _confirm(
    session: boto3.Session,
    service: Service,
    instance_id: str,
    item: Mapping[str, Any],
    name: str,
    shown: str,
    data: Mapping[str, Any],
    *,
    yes: bool,
) -> None:
    """手順 3。取り込むインスタンス、作る AMI、コピーする設定、変わるものを挙げて確認する。"""
    tag = aws.tag(item, "Name")
    lines = [
        f"instance: {instance_id}" + (f" (Name={printable(tag)})" if tag else ""),
        f"AMI to make: {name}",
    ]
    addresses = _addresses(item)
    if addresses:
        lines.append(f"private addresses: {', '.join(addresses)}; they change at every start")
    others = _others(item, data)
    for device, volume, size in others:
        shown_size = f"{size} GiB" if size is not None else "? GiB"
        lines.append(f"EBS {device} {volume} {shown_size}: {NOT_IN_AMI}")
    msg.tell(
        f"service import {service.name}:\n"
        + "".join(f"  {line}\n" for line in lines)
        + f"config file {printable(tilde(service.conf_path))}:\n"
        + "".join(f"  {printable(line)}\n" if line else "\n" for line in shown.splitlines())
    )
    aws.confirm(session, f"import {instance_id} as {service.name}", yes=yes)


def _write(service: Service, text: str) -> None:
    """手順 6。設定ファイルを新しく作る。そのあいだに作られていれば、上書きせずに止める。"""
    path = service.conf_path
    make_dir(path.parent)
    try:
        create_text(path, text)
    except FileExistsError:
        raise TemtError(
            f"{printable(tilde(path))} was made while importing; it is left as it is"
        ) from None
    msg.info(f"wrote {printable(tilde(path))}")


def run(service: Service, instance_id: str, *, yes: bool) -> None:
    """既存のインスタンス instance_id を、設定ファイルの無いサービス service に取り込む。"""
    # 手順 1。既にあるサービスには取り込まない
    if service.conf_path.exists():
        raise TemtError(
            f"{printable(tilde(service.conf_path))} exists; service import makes a new service only"
        )
    values = conf.load(service, missing_ok=True)
    session = aws.session(values)
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    with lock.hold(service):
        item = _source(ec2, instance_id)
        # 手順 2
        raw = ec2.get_launch_template_data(InstanceId=instance_id).get("LaunchTemplateData") or {}
        data, removed = copied(ec2, item, raw)
        name = bake.image_name(service.svc)
        data["ImageId"] = name
        try:
            data = ltdata.check(data, conf.LAUNCH_TEMPLATE)
        except ValueError as error:
            raise TemtError(f"cannot copy the settings of {instance_id}: {error}") from None
        registered = _registered(ssm, instance_id)
        if not registered:
            msg.warn(
                f"{instance_id} is not registered with SSM; temt logs in through the SSM agent "
                "and EC2 Instance Connect (see docs/usage.md); start will not wait for it"
            )
        for key in removed:
            msg.warn(
                f"the tag {printable(key)} is not copied; AWS reserves keys starting with aws:"
            )
        text = _text(data, registered=registered)
        # 手順 3
        _confirm(session, service, instance_id, item, name, text, raw, yes=yes)
        # 手順 4
        root = item.get("RootDeviceName")
        mappings: Sequence[Any] = (
            [{"DeviceName": root, "Ebs": {"DeleteOnTermination": True}}] if root else []
        )
        others = [device for device, _, _ in _others(item, raw)]
        image_id, _ = bake.create(ec2, service, instance_id, others, mappings, name)
        # 手順 5。締め切りは置かない。止めても、元のインスタンスは何も変わっていない
        try:
            why = bake.wait_available(ec2, image_id)
        except (ClientError, BotoCoreError):
            msg.tell(f"the AMI {image_id} is not available yet; nothing was written\n", side=True)
            raise
        if why is not None:
            raise TemtError(why)
        # 手順 6
        _write(service, text)
    # 手順 7
    msg.info(
        f"run '{PREFIX} apply --svc {service.svc}' to make the launch template; "
        f"{instance_id} is left as it was"
    )
