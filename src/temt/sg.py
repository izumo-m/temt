"""sg diff と sg show の本体。セキュリティグループを AWS から読んで見せる。

どちらも表示するだけで、ロックは取らず、設定ファイルにも AWS にも書き込まない。
sg diff は、次の apply が受信規則をどう変えるかを標準出力に書く。sg show は
セキュリティグループを表示する。比べるセキュリティグループは、設定ファイルの
SubnetId が属する VPC にある <名前> のものである。設定ファイルだけを扱う sg list、
sg add、sg remove は ingress にある。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from temt import aws, conf, msg, resources, table
from temt.aws import field
from temt.change import ADD, REMOVE, UPDATE
from temt.names import tilde

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from types_boto3_ec2.client import EC2Client

    from temt.conf import Conf
    from temt.names import Service


def _vpc(ec2: EC2Client, service: Service, values: Conf, *, quiet: bool = False) -> str | None:
    """SubnetId が属する VPC。SubnetId が書かれていなければ None。

    quiet でなければ、書かれていないことを知らせる。sg show は自分の表に書くので知らせない。
    """
    subnet_id = values.launch_template.SubnetId
    if subnet_id is conf.UNSET:
        if not quiet:
            msg.info(
                f"no {conf.launch_template_key('SubnetId')} in "
                f"{table.printable(tilde(service.conf_path))}; the next apply picks a subnet"
            )
        return None
    return aws.subnet(
        ec2, subnet_id, conf.source(service, conf.launch_template_key("SubnetId"))
    ).vpc_id


def _split(
    groups: Sequence[Mapping[str, Any]], vpc_id: str
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    """VPC にあるセキュリティグループと、ほかの VPC にある同じ名前のものに分ける。"""
    mine = next((one for one in groups if field(one, "VpcId") == vpc_id), None)
    return mine, [one for one in groups if one is not mine]


def diff(service: Service) -> None:
    """次の apply が受信規則をどう変えるかを、1 つ 1 行で標準出力に書く。

    セキュリティグループが無ければ、すべての規則を足すものとして出す。標準出力には変更
    だけを書くよう、途中の知らせは標準エラー出力に回す。
    """
    with msg.stderr_only():
        _diff(service)


def _diff(service: Service) -> None:
    """sg diff の本体。"""
    values = conf.load(service)
    ec2 = aws.session(values).client("ec2")
    vpc_id = _vpc(ec2, service, values)
    current: list[resources.Rule] = []
    if vpc_id is not None:
        mine, _ = _split(resources.groups(ec2, service), vpc_id)
        if mine is None:
            msg.info(f"no security group {service.name} in {vpc_id}; the next apply creates it")
        else:
            current = resources.current_rules(ec2, field(mine, "GroupId"))
    found = resources.changes(resources.wanted_rules(values), current)
    if not found:
        msg.info(f"security group {service.name}: no differences", side=True)
        return
    lines: list[list[str]] = []
    lines += [[ADD, one.port(), one.source, one.description] for one in found.added]
    lines += [
        [
            UPDATE,
            now.port(),
            now.source,
            f"{resources.shown_description(now)} -> {resources.shown_description(wanted)}",
        ]
        for now, wanted in found.described
    ]
    lines += [[REMOVE, one.port(), one.source, one.description] for one in found.removed]
    table.write_lines(lines)


def show(service: Service) -> None:
    """セキュリティグループを表示する。受信規則は数だけを出す。

    SubnetId が書かれていなければ VPC が決まらないので、同じ名前のセキュリティグループを
    VPC とともに挙げるだけにする。どれを次の apply が使うかは、選ぶサブネットで決まる。
    """
    values = conf.load(service)
    ec2 = aws.session(values).client("ec2")
    vpc_id = _vpc(ec2, service, values, quiet=True)
    groups = resources.groups(ec2, service)
    msg.tell(f"security group {service.name}:\n")
    if vpc_id is None:
        msg.rows([("state", "(no subnet yet; the next apply picks one)")])
        _list_groups("groups with this name:", groups)
        return
    mine, others = _split(groups, vpc_id)
    if mine is None:
        msg.rows([("state", f"(not found in {vpc_id}; the next apply creates it)")])
    else:
        group_id = field(mine, "GroupId")
        tags = " ".join(
            f"{one.get('Key', '')}={one.get('Value', '')}" for one in mine.get("Tags") or []
        )
        msg.rows(
            [
                ("id", group_id),
                ("vpc", field(mine, "VpcId")),
                ("description", table.printable(mine.get("Description") or "")),
                ("tags", table.printable(tags)),
                ("ingress rules", str(len(resources.current_rules(ec2, group_id)))),
            ]
        )
    _list_groups("in other VPCs (destroy removes them):", others)


def _list_groups(heading: str, groups: Sequence[Mapping[str, Any]]) -> None:
    """セキュリティグループを ID と VPC で 1 行ずつ挙げる。無ければ何も書かない。"""
    if not groups:
        return
    lines = [[field(one, "GroupId"), field(one, "VpcId")] for one in groups]
    msg.tell(f"{heading}\n")
    msg.columns(lines)
