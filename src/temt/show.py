"""template show の本体。起動テンプレートの詳細を表示する。

この module は template show だけを持つ。表示するだけなので、ロックは取らず、
設定ファイルは読むが書かず、AWS にも書き込まない。出力は status と同じく標準出力に
書く。instance show（短縮形 status）は status の module が持つ。次の apply が
何を変えるかは diff が出すので、ここでは設定ファイルと比べない。

既定バージョンの中身は、設定ファイルの [launch-template] と同じ TOML の形で、全部を
出す。そのまま設定ファイルに写せる。読み出したときにだけある項目は出さない。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from temt import aws, conf, ltdata, msg, resources
from temt.table import printable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from types_boto3_ec2.client import EC2Client

    from temt.names import Service

# 値が無いときの表示
NOT_SET = "(not set)"
# テンプレートが指す AMI が見つからないときの表示
NO_SUCH_IMAGE_NOTE = "(not found; it may have been deregistered)"
# AMI に EBS のルートボリュームが無いときの、その大きさの表示
NO_ROOT_VOLUME = "(no EBS root volume)"
# サブネットが見つからないときに SubnetId に添えること
NO_SUCH_SUBNET_NOTE = "(no such subnet)"
# 一覧で既定バージョンに付ける印
DEFAULT_MARK = "*"


def _text(value: Any) -> str:
    """項目の表示。無ければ (not set)、真偽値は true と false にする。

    値は AWS や設定ファイルから来るので、表示できない文字を置き換える。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None or value == "":
        return NOT_SET
    return printable(str(value))


def _interface(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """NetworkInterfaces の DeviceIndex が 0 の要素。無ければ空とする。"""
    return ltdata.element(data.get("NetworkInterfaces") or [], "DeviceIndex", 0) or {}


def _vpc(ec2: EC2Client, subnet_id: Any) -> str:
    """SubnetId に添える、そのサブネットが属する VPC。

    aws.subnet は IPv4 の CIDR が無いサブネットをエラーにするので、表示には使わない。
    サブネットが消えていても、その旨を添えて表示を続ける。
    """
    found = ec2.describe_subnets(Filters=[{"Name": "subnet-id", "Values": [subnet_id]}]).get(
        "Subnets", []
    )
    vpc_id = found[0].get("VpcId") if found else None
    return str(vpc_id) if vpc_id else NO_SUCH_SUBNET_NOTE


def _show_default(
    ec2: EC2Client, service: Service, version: Mapping[str, Any], data: Mapping[str, Any]
) -> None:
    """1 つめ。既定バージョンの中身を、設定ファイルと同じ TOML の形で表示する。"""
    number = _text(version.get("VersionNumber"))
    subnet_id = _interface(data).get("SubnetId")
    notes: dict[str, str] = {}
    if isinstance(subnet_id, str) and aws.SUBNET_ID_RE.fullmatch(subnet_id):
        notes[subnet_id] = _vpc(ec2, subnet_id)
    msg.tell(f"launch template {service.launch_template}:\n")
    msg.rows([("version", f"{number} (created {aws.when(version.get('CreateTime'))})")])
    lines = ltdata.render(ltdata.readable(data), notes)
    msg.tell("".join(f"  {printable(line)}\n" if line else "\n" for line in lines))


def _image(ec2: EC2Client, image_id: str) -> Mapping[str, Any] | None:
    """テンプレートが指す AMI。見つからなければ None。"""
    if not aws.is_image_id(image_id):
        return None
    return aws.find_image(ec2, image_id)


def _root_volume(found: Mapping[str, Any]) -> str:
    """AMI のルートボリュームの大きさの表示。"""
    root = aws.root_volume(found)
    return f"{root.size} GiB" if root is not None else NO_ROOT_VOLUME


def _latest(ec2: EC2Client, svc: str, found: Mapping[str, Any] | None) -> str:
    """サービスの最新の AMI の表示。テンプレートのものより新しければ、そう添える。"""
    tag = f"{aws.TAG_SERVICE}={svc}"
    latest = aws.own_ami(ec2, svc)
    if latest is None:
        return f"none ({tag})"
    created = found.get("CreationDate") if found is not None else None
    newer = ", newer" if isinstance(created, str) and latest.created > created else ""
    return f"{latest.name} ({latest.image_id}, {tag}) created {aws.when(latest.created)}{newer}"


def _show_image(ec2: EC2Client, service: Service, image_id: Any) -> None:
    """2 つめ。テンプレートが指す AMI と、サービスの最新の AMI を表示する。"""
    found = _image(ec2, image_id) if isinstance(image_id, str) and image_id else None
    rows: list[tuple[str, str]] = []
    if found is not None:
        rows += [
            ("name", _text(found.get("Name"))),
            ("created", aws.when(found.get("CreationDate"))),
            ("state", _text(found.get("State"))),
            ("root volume", _root_volume(found)),
        ]
    elif image_id:
        rows.append(("state", NO_SUCH_IMAGE_NOTE))
    rows.append(("latest", _latest(ec2, service.svc, found)))
    msg.tell(f"image {_text(image_id)}:\n")
    msg.rows(rows)


def _show_versions(versions: Sequence[Mapping[str, Any]]) -> None:
    """3 つめ。バージョンの一覧を、番号の順に 1 行ずつ表示する。"""
    ordered = sorted(versions, key=lambda one: one.get("VersionNumber") or 0)
    numbers = [_text(one.get("VersionNumber")) for one in ordered]
    width = max(len(number) for number in numbers)
    msg.tell(f"versions ({DEFAULT_MARK} is the default):\n")
    for number, one in zip(numbers, ordered, strict=True):
        data = one.get("LaunchTemplateData") or {}
        mark = DEFAULT_MARK if one.get("DefaultVersion") else " "
        msg.tell(
            f"  {number:>{width}} {mark} {aws.when(one.get('CreateTime'))} "
            f"{_text(data.get('ImageId'))} {_text(data.get('InstanceType'))} "
            f"{_text(_interface(data).get('SubnetId'))}\n"
        )


def template(service: Service) -> None:
    """起動テンプレートの詳細を、3 つの節に分けて表示する。"""
    values = conf.load(service)
    session = aws.session(values)
    ec2 = session.client("ec2")
    versions = resources.versions(ec2, service)
    current = resources.default_version(versions, service)
    data = current.get("LaunchTemplateData") or {}
    _show_default(ec2, service, current, data)
    _show_image(ec2, service, data.get("ImageId"))
    _show_versions(versions)
