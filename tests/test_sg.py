"""sg diff と sg show の本体のテスト。AWS は Stubber で置き換える。"""

from pathlib import Path
from typing import Any

import pytest
from conftest import Stubs

from temt import sg
from temt.msg import TemtError
from temt.names import Service, defaults_path

NAME = "temt-dev-main"
SUBNET = "subnet-0123456789abcdef0"
VPC = "vpc-0123456789abcdef0"
GROUP = "sg-0123456789abcdef0"
INGRESS = "[[security-group.ingress]]\n"
THE_SUBNET = {"Filters": [{"Name": "subnet-id", "Values": [SUBNET]}]}
THE_GROUPS = {"Filters": [{"Name": "group-name", "Values": [NAME]}]}
THE_RULES = {"Filters": [{"Name": "group-id", "Values": [GROUP]}]}


def add_rules(service: Service, text: str) -> None:
    """設定ファイルの末尾に受信規則を足す。"""
    with service.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write(text)


def subnet(stubs: Stubs) -> None:
    stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "172.31.0.0/20"}]},
        THE_SUBNET,
    )


def groups(stubs: Stubs, *found: dict[str, Any]) -> None:
    stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": list(found)}, THE_GROUPS)


def a_group(group_id: str = GROUP, vpc_id: str = VPC) -> dict[str, Any]:
    return {
        "GroupId": group_id,
        "GroupName": NAME,
        "VpcId": vpc_id,
        "Description": "Managed by temt",
        "Tags": [{"Key": "Name", "Value": NAME}, {"Key": "temt:service", "Value": "dev-main"}],
    }


def rules(stubs: Stubs, *found: dict[str, Any]) -> None:
    stubs.ec2.add_response(
        "describe_security_group_rules", {"SecurityGroupRules": list(found)}, THE_RULES
    )


def a_rule(rule_id: str, port: int, cidr: str, description: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {
        "SecurityGroupRuleId": rule_id,
        "IsEgress": False,
        "IpProtocol": "tcp",
        "FromPort": port,
        "ToPort": port,
        "CidrIpv4": cidr,
    }
    if description:
        item["Description"] = description
    return item


def test_diff_shows_what_apply_would_do(
    configured: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """足す、説明を直す、消すの順に、1 つ 1 行で出す。"""
    add_rules(
        configured,
        "\n" + INGRESS + 'Port = 22\nCidr = "198.51.100.7/32"\nDescription = "home"\n'
        "\n" + INGRESS + 'Port = 80\nCidr = "203.0.113.10/32"\nDescription = "web"\n',
    )
    subnet(aws_stubs)
    groups(aws_stubs, a_group(group_id="sg-00000000000000002", vpc_id="vpc-other"), a_group())
    rules(
        aws_stubs,
        a_rule("sgr-1", 80, "203.0.113.10/32", "office"),
        a_rule("sgr-2", 22, "192.0.2.5/32"),
    )
    sg.diff(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "+   22/tcp   198.51.100.7/32   home",
        "~   80/tcp   203.0.113.10/32   office -> web",
        "-   22/tcp   192.0.2.5/32      -",
    ]
    assert err == ""


def test_diff_without_differences(
    configured: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    subnet(aws_stubs)
    groups(aws_stubs, a_group())
    rules(aws_stubs)
    sg.diff(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out == ""
    assert err.endswith(f"security group {NAME}: no differences\n")


def test_diff_without_the_group_adds_every_rule(
    configured: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    add_rules(configured, "\n" + INGRESS + 'Protocol = "icmp"\nCidr = "2001:db8::/64"\n')
    subnet(aws_stubs)
    groups(aws_stubs, a_group(group_id="sg-00000000000000002", vpc_id="vpc-other"))
    sg.diff(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out == "+   icmp   2001:db8::/64   -\n"
    assert f"no security group {NAME} in {VPC}; the next apply creates it\n" in err


def test_diff_without_a_subnet(
    service: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """SubnetId が無ければセキュリティグループも決まらないので、AWS を読まずにすべてを
    足すものとする。"""
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text(INGRESS + 'Port = 22\nCidr = "198.51.100.7/32"\n')
    sg.diff(service)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out == "+   22/tcp   198.51.100.7/32   -\n"
    assert (
        "no launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId in "
        "~/.config/temt/dev-main.toml; the next apply picks a subnet\n"
    ) in err


def test_diff_reads_the_subnet_of_the_primary_interface(
    service: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """SubnetId は NetworkInterfaces の DeviceIndex が 0 の要素のものを使う。ほかの要素の
    SubnetId は見ない。"""
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text(
        "[[launch-template.NetworkInterfaces]]\n"
        "DeviceIndex = 1\n"
        'SubnetId = "subnet-00000000000000001"\n'
        "\n"
        "[[launch-template.NetworkInterfaces]]\n"
        "DeviceIndex = 0\n"
        f'SubnetId = "{SUBNET}"\n'
    )
    subnet(aws_stubs)
    groups(aws_stubs, a_group())
    rules(aws_stubs)
    sg.diff(service)
    aws_stubs.done()
    assert capsys.readouterr().err.endswith(f"security group {NAME}: no differences\n")


def test_diff_without_the_primary_interface(
    service: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """DeviceIndex が 0 の要素が無ければ、ほかの要素に SubnetId があっても、無いものとする。"""
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text(
        "[[launch-template.NetworkInterfaces]]\n"
        "DeviceIndex = 1\n"
        f'SubnetId = "{SUBNET}"\n'
        "\n" + INGRESS + 'Port = 22\nCidr = "198.51.100.7/32"\n'
    )
    sg.diff(service)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out == "+   22/tcp   198.51.100.7/32   -\n"
    assert "no launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId in " in err


def test_diff_names_the_file_with_a_missing_subnet(
    service: Service, home_dir: Path, aws_stubs: Stubs
) -> None:
    """無いサブネットは、その SubnetId を書いたファイルの名前を添えて誤りにする。共通ファイル
    に書いたものなら、共通ファイルを挙げる。"""
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text("")
    defaults_path().write_text(
        f'[[launch-template.NetworkInterfaces]]\nDeviceIndex = 0\nSubnetId = "{SUBNET}"\n'
    )
    aws_stubs.ec2.add_response("describe_subnets", {"Subnets": []}, THE_SUBNET)
    with pytest.raises(TemtError) as caught:
        sg.diff(service)
    aws_stubs.done()
    assert str(caught.value) == (
        f"no such subnet: {SUBNET}; for another account, remove "
        "launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId and "
        "launch-template.ImageId, and the other items that point into this account "
        "(Groups, IamInstanceProfile, KeyName, ...), from "
        "~/.config/temt/defaults.toml and run apply again"
    )


def test_show_the_group(
    configured: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    subnet(aws_stubs)
    groups(aws_stubs, a_group(group_id="sg-00000000000000002", vpc_id="vpc-other"), a_group())
    rules(aws_stubs, a_rule("sgr-1", 22, "192.0.2.5/32"))
    sg.show(configured)
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert err == ""
    assert out.splitlines() == [
        f"security group {NAME}:",
        f"  id:            {GROUP}",
        f"  vpc:           {VPC}",
        "  description:   Managed by temt",
        f"  tags:          Name={NAME} temt:service=dev-main",
        "  ingress rules: 1",
        "in other VPCs (destroy removes them):",
        "  sg-00000000000000002  vpc-other",
    ]


def test_show_a_group_that_is_not_there(
    configured: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    subnet(aws_stubs)
    groups(aws_stubs)
    sg.show(configured)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        f"security group {NAME}:",
        f"  state: (not found in {VPC}; the next apply creates it)",
    ]


def test_diff_shows_rules_that_temt_cannot_write(
    configured: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """表せない規則は、プロトコルと送信元の ID で出す。消える説明は - と書く。"""
    add_rules(configured, "\n" + INGRESS + 'Port = 22\nCidr = "192.0.2.5/32"\n')
    subnet(aws_stubs)
    groups(aws_stubs, a_group())
    rules(
        aws_stubs,
        a_rule("sgr-1", 22, "192.0.2.5/32", "home"),
        {
            "SecurityGroupRuleId": "sgr-2",
            "IsEgress": False,
            "IpProtocol": "-1",
            "FromPort": -1,
            "ToPort": -1,
            "ReferencedGroupInfo": {"GroupId": "sg-00000000000000009"},
        },
        {
            "SecurityGroupRuleId": "sgr-3",
            "IsEgress": False,
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "PrefixListId": "pl-00000000",
        },
    )
    sg.diff(configured)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "~   22/tcp    192.0.2.5/32           home -> -",
        "-   all       sg-00000000000000009   -",
        "-   443/tcp   pl-00000000            -",
    ]


def test_show_reads_the_subnet_of_the_primary_interface(
    service: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """sg show も、DeviceIndex が 0 の要素の SubnetId の VPC で探す。"""
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text(
        f'[[launch-template.NetworkInterfaces]]\nDeviceIndex = 0\nSubnetId = "{SUBNET}"\n'
    )
    subnet(aws_stubs)
    groups(aws_stubs)
    sg.show(service)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        f"security group {NAME}:",
        f"  state: (not found in {VPC}; the next apply creates it)",
    ]


def test_show_without_a_subnet(
    service: Service, aws_stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    """VPC が決まらないので、同じ名前のセキュリティグループを挙げるだけにする。"""
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text('AwsRegion = "ap-northeast-1"\n')
    groups(aws_stubs, a_group(), a_group(group_id="sg-00000000000000002", vpc_id="vpc-other"))
    sg.show(service)
    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        f"security group {NAME}:",
        "  state: (no subnet yet; the next apply picks one)",
        "groups with this name:",
        f"  {GROUP}  {VPC}",
        "  sg-00000000000000002  vpc-other",
    ]
