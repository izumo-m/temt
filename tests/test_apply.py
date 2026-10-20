"""apply の本体のテスト。ssh-keygen は実際に呼び、AWS は Stubber で差し替える。

クライアントの差し替えは conftest の aws_stubs が行う。ホームは一時ディレクトリに
向く。
"""

import base64
import copy
import io
import json
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from conftest import CONFIGURED, REGION, WHERE, Stubs, identified
from types_boto3_ec2.client import EC2Client
from types_boto3_iam.client import IAMClient

from temt import apply, aws, conf, home, keys, policy, resources
from temt.msg import TemtError, UsageError
from temt.names import CONF_DIR_ENV, SSH_DIR_ENV, Service, defaults_path, keys_dir, ssh_dir
from temt.table import tilde

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
VPC = "vpc-0123456789abcdef0"
SUBNET = "subnet-0123456789abcdef0"
GROUP = "sg-0123456789abcdef0"
# temt のものではないセキュリティグループ
WEB = "sg-00000000000000077"
IMAGE = "ami-0123456789abcdef0"
IMAGE_NAME = "temt-dev-main-2609200900"
# AMI のルートボリュームのデバイス名
ROOT_DEVICE = "/dev/sda1"
# 既定の鍵の、diff での表示
KEY = "~/.ssh/temt/keys/temt"
# 設定ファイルと共通ファイルの、~ で始まる表示
CONF_SHOWN = "~/.config/temt/dev-main.toml"
DEFAULTS_SHOWN = "~/.config/temt/defaults.toml"
WHEN = datetime(2026, 9, 20, tzinfo=UTC)

# 書けるものをすべて書いた設定ファイル。apply はこれに何も書かない。ImageId も書き、AMI を
# 固定する
PINNED = CONFIGURED.replace("[launch-template]\n", f'[launch-template]\nImageId = "{IMAGE}"\n')
WRITTEN = PINNED + conf.EC2_DEFAULT
# apply が新しく作る雛形
SCAFFOLD = conf.scaffold(profile=True, ec2=True)
# WRITTEN の IamInstanceProfile の行
PROFILE_LINE = 'IamInstanceProfile = { Name = "${temt:name}" }\n'
# WRITTEN の、DeviceIndex が 0 の要素の最後の行。要素に項目を足すときに、この後ろに書く
LAST_OF_PRIMARY = "AssociatePublicIpAddress = true\n"
# WRITTEN の MetadataOptions のテーブル
METADATA_TABLE = (
    "[launch-template.MetadataOptions]\n"
    'HttpTokens = "required"\n'
    'InstanceMetadataTags = "enabled"\n'
    "\n"
)
INGRESS = (
    '\n[[security-group.ingress]]\nPort = 22\nCidr = "203.0.113.10/32"\nDescription = "home"\n'
)

DEFAULT_VPC = {"Filters": [{"Name": "is-default", "Values": ["true"]}]}
PUBLIC_SUBNETS = {
    "Filters": [
        {"Name": "vpc-id", "Values": [VPC]},
        {"Name": "map-public-ip-on-launch", "Values": ["true"]},
    ]
}
SERVICE_IMAGES = {
    "Owners": ["self"],
    "Filters": [
        {"Name": "name", "Values": ["temt-dev-main-??????????"]},
        {"Name": "state", "Values": ["available", "pending"]},
    ],
}
# Ubuntu 24.04 の公開 AMI の名前
UBUNTU_NAME = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-20260901"
# サービスの AMI が無く Ubuntu 24.04 を選んだときに、ImageId の行に添えること
UBUNTU_NOTE = f"(Ubuntu 24.04; no {NAME} AMI in {REGION})"
THE_GROUP = {"Filters": [{"Name": "group-name", "Values": [NAME]}]}
# temt が管理していることを示すタグ
MARK = [{"Key": "temt:service", "Value": "dev-main"}]
# インスタンスロールに付くタグ。temt のタグと、このリージョンのタグ
REGION_TAG = f"temt:region:{REGION}"
ROLE_MARK = [*MARK, {"Key": REGION_TAG, "Value": ""}]
# 別のサービスのものであることを示すタグ
OTHER_MARK = [{"Key": "temt:service", "Value": "other"}]
GROUP_TAGS = [{"Key": "Name", "Value": NAME}, *MARK]
THE_TEMPLATE = {"LaunchTemplateName": TEMPLATE}
# temt が起動テンプレートに足すタグ。インスタンス、ボリューム、ネットワークインター
# フェースの順
TAGGED = [
    {"ResourceType": kind, "Tags": GROUP_TAGS}
    for kind in ("instance", "volume", "network-interface")
]

# ProxyCommand の先頭の env。既定の場所を ~ で始まる形で書く
PLACES = f"env {CONF_DIR_ENV}='~/.config/temt' {SSH_DIR_ENV}='~/.ssh/temt'"

# [[role.ec2]] の既定から作るインラインポリシー temt-ec2 の名前と中身
EC2 = policy.policy_name(conf.EC2)
EC2_POLICY = policy.document(conf.ec2_default(), policy.variables(Service("dev-main")), ["", ""])
# [[role.jupyter]] を書いた設定ファイルの末尾と、それから作る temt-jupyter の名前と
# 中身。Effect は temt が補う
STATEMENT = (
    "\n[[role.jupyter]]\n"
    'Action = "ssm:GetParameter"\n'
    'Resource = "arn:aws:ssm:*:*:parameter/${temt:name}/*"\n'
)
JUPYTER = policy.policy_name("jupyter")
ROLE_POLICY = {
    "Version": policy.VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "ssm:GetParameter",
            "Resource": f"arn:aws:ssm:*:*:parameter/{NAME}/*",
        }
    ],
}
# 管理ポリシーの ARN
S3_READ = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
SSM_CORE = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"


def edited(old: str, new: str, text: str = WRITTEN) -> str:
    """text の old を new に替えたもの。old が 1 つだけあることを確かめる。"""
    assert text.count(old) == 1, old
    return text.replace(old, new)


def in_primary(lines: str, text: str = WRITTEN) -> str:
    """text の、DeviceIndex が 0 の要素の最後に lines を足したもの。"""
    return edited(LAST_OF_PRIMARY, LAST_OF_PRIMARY + lines, text)


def before_role(lines: str) -> str:
    """WRITTEN の [[role.ec2]] の前に lines を足したもの。[launch-template] の
    テーブルの続きや、[role] を書くのに使う。
    """
    return PINNED + lines + conf.EC2_DEFAULT


def write_conf(service: Service, text: str = WRITTEN) -> str:
    """設定ファイルを置き、その中身を返す。"""
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(text, encoding="utf-8")
    return text


def write_defaults(text: str) -> None:
    """共通ファイルを置く。"""
    defaults_path().parent.mkdir(parents=True, exist_ok=True)
    defaults_path().write_text(text, encoding="utf-8")


def make_key() -> None:
    """既定の鍵を、前の apply が作ったものとして置く。"""
    keys.generate(keys.default_path(), keys.DEFAULT_NAME)


def set_up_here(service: Service) -> None:
    """手元のものを、前の apply が揃えたものとして置く。ssh ディレクトリ、既定の鍵、
    Host ブロック、Include、サービスのコマンド。diff には AWS と設定ファイルの違いだけが出る。
    """
    make_key()
    home.prepare_dirs()
    host = home.detect_host_block(service)
    _ = home.write_edit(host)
    _ = home.write_include(home.detect_include(), host)
    home.write_command(service)


# 変更の行の頭。印と、その後ろの空白
CHANGE_HEADS = ("+   ", "~   ", "-   ")


def changes(out: str) -> list[str]:
    """出力のうち、変更の行。続く字下げした行と、apply の更新の知らせは除く。"""
    return [line for line in out.splitlines() if line.startswith(CHANGE_HEADS)]


def heads(out: str) -> list[list[str]]:
    """出力のうち、変更の行の印、種類、名前。"""
    return [line.split()[:3] for line in changes(out)]


# 手元のものの差分の種類
LOCAL_KINDS = ("dir", "command", "key", "ssh-host", "ssh-include")


def aws_heads(out: str) -> list[list[str]]:
    """heads のうち、設定ファイルと AWS のものの行。"""
    return [head for head in heads(out) if head[1] not in LOCAL_KINDS]


def details(out: str, kind: str) -> list[str]:
    """diff の出力のうち、種類 kind の行の、印と違い。"""
    found: list[str] = []
    for line in changes(out):
        parts = line.split(maxsplit=3)
        if parts[1] == kind:
            found.append(f"{parts[0]} {parts[3] if len(parts) > 3 else ''}".rstrip())
    return found


def template_data(*, primary: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    """WRITTEN から temt が組み立てる起動テンプレートの中身。

    primary は DeviceIndex が 0 の要素に足すか替える項目、changes は最上位の項目で、
    値が None の項目は除く。
    """
    data: dict[str, Any] = {
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
                **(primary or {}),
            }
        ],
        "MetadataOptions": {"HttpTokens": "required", "InstanceMetadataTags": "enabled"},
        "CreditSpecification": {"CpuCredits": "unlimited"},
        "TagSpecifications": copy.deepcopy(TAGGED),
        **changes,
    }
    return {key: value for key, value in data.items() if value is not None}


def missing(stub: Stubber, method: str, code: str, **params: Any) -> None:
    """そのリソースが無いときの誤りを積む。IAM の waiter に合わせて 404 で揃える。"""
    stub.add_client_error(
        method, service_error_code=code, http_status_code=404, expected_params=params or None
    )


def a_role(tags: list[dict[str, str]] = ROLE_MARK) -> dict[str, Any]:
    """get-role と create-role が返す形。"""
    return {
        "Role": {
            "Path": "/",
            "RoleName": NAME,
            "RoleId": "AROAEXAMPLE000000",
            "Arn": f"arn:aws:iam::123456789012:role/{NAME}",
            "CreateDate": WHEN,
            "Tags": tags,
        }
    }


def a_profile(roles: list[str], tags: list[dict[str, str]] = MARK) -> dict[str, Any]:
    """get-instance-profile と create-instance-profile が返す形。"""
    return {
        "InstanceProfile": {
            "Path": "/",
            "InstanceProfileName": NAME,
            "InstanceProfileId": "AIPAEXAMPLE000000",
            "Arn": f"arn:aws:iam::123456789012:instance-profile/{NAME}",
            "CreateDate": WHEN,
            "Tags": tags,
            "Roles": [
                {
                    "Path": "/",
                    "RoleName": name,
                    "RoleId": "AROAEXAMPLE000000",
                    "Arn": f"arn:aws:iam::123456789012:role/{name}",
                    "CreateDate": WHEN,
                }
                for name in roles
            ],
        }
    }


def a_group(group_id: str = GROUP, vpc_id: str = VPC, name: str = NAME) -> dict[str, Any]:
    """describe-security-groups が返す 1 つ分。temt のものには temt のタグが付いている。"""
    tags = GROUP_TAGS if name == NAME else []
    return {"GroupId": group_id, "GroupName": name, "VpcId": vpc_id, "Tags": tags}


def in_vpc(name: str, vpc_id: str = VPC) -> dict[str, Any]:
    """名前と VPC で絞った describe-security-groups の引数。"""
    return {
        "Filters": [
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    }


def an_image(*, size: int | None = 8) -> dict[str, Any]:
    """describe-images が返す AMI。size はルートボリュームの GiB で、None なら EBS の
    ルートボリュームを持たない。
    """
    image: dict[str, Any] = {
        "ImageId": IMAGE,
        "Name": IMAGE_NAME,
        "State": "available",
        "RootDeviceName": ROOT_DEVICE,
    }
    if size is not None:
        image["BlockDeviceMappings"] = [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": size}}]
    return image


def subnet(stubs: Stubs, *, ipv6: bool = False, subnet_id: str = SUBNET, vpc_id: str = VPC) -> None:
    """describe-subnets でサブネットを 1 つ読む。ipv6 なら IPv6 の CIDR が結び付いている。"""
    found: dict[str, Any] = {"SubnetId": subnet_id, "VpcId": vpc_id, "CidrBlock": "10.0.0.0/24"}
    if ipv6:
        found["Ipv6CidrBlockAssociationSet"] = [
            {"Ipv6CidrBlock": "2001:db8::/64", "Ipv6CidrBlockState": {"State": "associated"}}
        ]
    stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [found]},
        {"Filters": [{"Name": "subnet-id", "Values": [subnet_id]}]},
    )


def public_subnet(stubs: Stubs) -> None:
    """手順 2 で、書かれていない SubnetId を既定 VPC のサブネットから選ぶ。"""
    stubs.ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": VPC}]}, DEFAULT_VPC)
    stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "AvailabilityZone": f"{REGION}a", "VpcId": VPC}]},
        PUBLIC_SUBNETS,
    )


def no_template(stubs: Stubs) -> None:
    """起動テンプレートを読み、無いと分かる。"""
    missing(
        stubs.ec2, "describe_launch_template_versions", resources.NO_SUCH_TEMPLATE, **THE_TEMPLATE
    )


def own_image(stubs: Stubs, *, state: str = "available", older: bool = False) -> None:
    """手順 4 で、起動テンプレートが無いので、サービスの最新の AMI を選ぶ。older なら、
    それより古い available の AMI も並べる。
    """
    image = {**an_image(), "State": state, "CreationDate": "2026-09-20T00:00:00.000Z"}
    images = [image]
    if older:
        old = {
            **an_image(),
            "ImageId": "ami-0aaaaaaaaaaaaaaaa",
            "Name": f"{NAME}-2609010000",
            "State": "available",
            "CreationDate": "2026-09-01T00:00:00.000Z",
        }
        images = [old, image]
    stubs.ec2.add_response("describe_images", {"Images": images}, SERVICE_IMAGES)


def ubuntu(stubs: Stubs) -> None:
    """手順 4 で、起動テンプレートが無く、サービスの AMI も無いので、Ubuntu 24.04 の公開 AMI を
    選ぶ。
    """
    stubs.ec2.add_response("describe_images", {"Images": []}, SERVICE_IMAGES)
    stubs.ssm.add_response(
        "get_parameter",
        {"Parameter": {"Name": aws.UBUNTU_AMI_PARAMETER, "Value": IMAGE}},
        {"Name": aws.UBUNTU_AMI_PARAMETER},
    )
    stubs.ec2.add_response(
        "describe_images", {"Images": [{**an_image(), "Name": UBUNTU_NAME}]}, {"ImageIds": [IMAGE]}
    )


def chosen(stubs: Stubs, *, ipv6: bool = False) -> None:
    """手順 2 で、書かれていない SubnetId を AWS から選び、サブネットの IPv6 を読む。手順 4 で、
    起動テンプレートがまだ無いので AMI を選ぶ。サービスの AMI はまだ無いので、Ubuntu の公開
    AMI になる。手順 5 は起動テンプレートを読み直さない。
    """
    public_subnet(stubs)
    subnet(stubs, ipv6=ipv6)
    no_template(stubs)
    ubuntu(stubs)


def kept(stubs: Stubs, data: dict[str, Any]) -> None:
    """手順 2 で、書かれていない SubnetId を AWS から選び、サブネットの IPv6 を読む。手順 4 で、
    起動テンプレート（既定バージョンの中身 data）を読み、その AMI を使う。
    """
    public_subnet(stubs)
    subnet(stubs)
    template_versions(stubs, data)
    stubs.ec2.add_response("describe_images", {"Images": [an_image()]}, {"ImageIds": [IMAGE]})


def built(
    stubs: Stubs,
    *,
    group: str | None = GROUP,
    image: dict[str, Any] | None = None,
    read_image: bool = True,
    subnet_id: str = SUBNET,
    vpc_id: str = VPC,
    stop: bool = False,
    read_subnet: bool = False,
) -> None:
    """手順 4 で起動テンプレートの中身を組み立てるときに読むもの。

    書いた ImageId の AMI（read_image が偽なら、ImageId を書いておらず、AMI は先に決めて
    あるので読まない）、read_subnet なら SubnetId のサブネット（手順 2
    で Ipv6AddressCount を決めるために読んでいれば、読み直さない）、その VPC にある temt の
    セキュリティグループ（group が None なら無い）。stop なら、ルートボリュームを確かめて
    止まるので、セキュリティグループは読まない。
    """
    if read_image:
        stubs.ec2.add_response(
            "describe_images", {"Images": [image or an_image()]}, {"ImageIds": [IMAGE]}
        )
    if read_subnet:
        subnet(stubs, subnet_id=subnet_id, vpc_id=vpc_id)
    if stop:
        return
    found = [] if group is None else [a_group(group, vpc_id)]
    stubs.ec2.add_response(
        "describe_security_groups", {"SecurityGroups": found}, in_vpc(NAME, vpc_id)
    )


def validated(stubs: Stubs, content: dict[str, Any], *findings: dict[str, Any]) -> None:
    """手順 4 の ValidatePolicy。content を検査し、findings を返す。"""
    stubs.analyzer.add_response(
        "validate_policy",
        {"findings": list(findings)},
        {"policyDocument": json.dumps(content), "policyType": "IDENTITY_POLICY", "locale": "EN"},
    )


def a_finding(kind: str, code: str) -> dict[str, Any]:
    """ValidatePolicy の指摘 1 つ。位置は持たない。"""
    return {
        "findingDetails": f"details of {code}",
        "findingType": kind,
        "issueCode": code,
        "learnMoreLink": "https://example.com/",
        "locations": [],
    }


def attached(*arns: str) -> dict[str, Any]:
    """list-attached-role-policies が返す形。"""
    return {
        "AttachedPolicies": [
            {"PolicyName": arn.rpartition("/")[2], "PolicyArn": arn} for arn in arns
        ],
        "IsTruncated": False,
    }


def iam_in_place(
    stubs: Stubs,
    *,
    absent: tuple[str, ...] = (),
    managed: tuple[str, ...] = (),
    profile_tags: list[dict[str, str]] = MARK,
) -> None:
    """手順 5 で、ロール、temt-ec2、インスタンスプロファイルが揃っているときに読むもの。

    absent は、設定ファイルにあってロールにまだ無いインラインポリシーの名前。managed は
    ロールに付いている管理ポリシーの ARN。
    """
    stubs.iam.add_response("get_role", a_role(), {"RoleName": NAME})
    # 中身は URL エンコードした JSON で返る
    stubs.iam.add_response(
        "get_role_policy",
        {"RoleName": NAME, "PolicyName": EC2, "PolicyDocument": quote(json.dumps(EC2_POLICY))},
        {"RoleName": NAME, "PolicyName": EC2},
    )
    for name in absent:
        missing(
            stubs.iam, "get_role_policy", resources.NO_SUCH_ENTITY, RoleName=NAME, PolicyName=name
        )
    stubs.iam.add_response("list_role_policies", {"PolicyNames": [EC2]}, {"RoleName": NAME})
    stubs.iam.add_response("list_attached_role_policies", attached(*managed), {"RoleName": NAME})
    stubs.iam.add_response(
        "get_instance_profile", a_profile([NAME], profile_tags), {"InstanceProfileName": NAME}
    )


def group_in_place(stubs: Stubs, *rules: dict[str, Any]) -> None:
    """手順 5 で、セキュリティグループが揃っているときに読むもの。rules は受信規則。"""
    stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": [a_group()]}, THE_GROUP)
    stubs.ec2.add_response(
        "describe_security_group_rules",
        {"SecurityGroupRules": list(rules)},
        {"Filters": [{"Name": "group-id", "Values": [GROUP]}]},
    )


def template_in_place(stubs: Stubs, data: dict[str, Any], *, number: int = 3) -> None:
    """手順 5 で、既定バージョン number の中身が data の起動テンプレートを読む。"""
    template_versions(stubs, data, number=number)
    template_tags(stubs)


def template_versions(stubs: Stubs, data: dict[str, Any], *, number: int = 3) -> None:
    """起動テンプレートのバージョンを読む。既定バージョン number の中身が data。

    ImageId を書いていなければ手順 4 で、書いていれば手順 5 で読む。
    """
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {
                    "LaunchTemplateName": TEMPLATE,
                    "VersionNumber": number,
                    "DefaultVersion": True,
                    "LaunchTemplateData": data,
                }
            ]
        },
        THE_TEMPLATE,
    )


def template_tags(stubs: Stubs) -> None:
    """手順 5 で、起動テンプレート自身のタグを読む。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {
            "LaunchTemplates": [
                {
                    "LaunchTemplateId": "lt-0123456789abcdef0",
                    "LaunchTemplateName": TEMPLATE,
                    "Tags": MARK,
                }
            ]
        },
        {"LaunchTemplateNames": [TEMPLATE]},
    )


def new_version(stubs: Stubs, data: dict[str, Any], *, number: int = 4) -> None:
    """起動テンプレートの新しいバージョン number を中身 data で作って既定にし、前の既定
    バージョン（number - 1）を消す。
    """
    stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": number}},
        {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": data},
    )
    stubs.ec2.add_response(
        "modify_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": str(number)},
    )
    stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": number - 1}]},
        {"LaunchTemplateName": TEMPLATE, "Versions": [str(number - 1)]},
    )


def in_place(stubs: Stubs, data: dict[str, Any] | None = None) -> None:
    """WRITTEN の設定ファイルで、AWS のものがすべて揃っているときに読むものを積む。

    data を渡せば、起動テンプレートの既定バージョンはその中身になる。書き込みを積まない
    ので、diff が読むものにもなる。
    """
    # 手順 2。Ipv6AddressCount が書かれていないので、サブネットの IPv6 を見る
    subnet(stubs)
    built(stubs)
    validated(stubs, EC2_POLICY)
    iam_in_place(stubs)
    group_in_place(stubs)
    template_in_place(stubs, template_data() if data is None else data)


def nothing_there(stubs: Stubs, *, template: bool = True) -> None:
    """手順 5 で、ロール、temt-ec2、インスタンスプロファイル、セキュリティグループ、起動
    テンプレートがどれも無いときに読むもの。template が偽なら、起動テンプレートは手順 4 で
    読んであるので読まない。
    """
    missing(stubs.iam, "get_role", resources.NO_SUCH_ENTITY, RoleName=NAME)
    missing(stubs.iam, "get_role_policy", resources.NO_SUCH_ENTITY, RoleName=NAME, PolicyName=EC2)
    missing(stubs.iam, "list_role_policies", resources.NO_SUCH_ENTITY, RoleName=NAME)
    missing(stubs.iam, "list_attached_role_policies", resources.NO_SUCH_ENTITY, RoleName=NAME)
    missing(stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME)
    stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": []}, THE_GROUP)
    if template:
        no_template(stubs)


def no_iam(stubs: Stubs) -> None:
    """[role] の無い apply が、手放すロールとインスタンスプロファイルが無いことを読む。"""
    missing(stubs.iam, "get_role", resources.NO_SUCH_ENTITY, RoleName=NAME)
    missing(stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME)


def iam_made(stubs: Stubs) -> None:
    """手順 5 の更新で、ロール、temt-ec2、インスタンスプロファイルを、この順に作る。"""
    stubs.iam.add_response(
        "create_role",
        a_role(),
        {
            "RoleName": NAME,
            "AssumeRolePolicyDocument": resources.TRUST_POLICY,
            "Tags": ROLE_MARK,
        },
    )
    stubs.iam.add_response(
        "put_role_policy",
        {},
        {"RoleName": NAME, "PolicyName": EC2, "PolicyDocument": json.dumps(EC2_POLICY)},
    )
    stubs.iam.add_response(
        "create_instance_profile", a_profile([]), {"InstanceProfileName": NAME, "Tags": MARK}
    )
    # waiter は HTTP の状態で判断するので、応答にそれを入れる
    stubs.iam.add_response(
        "get_instance_profile",
        {**a_profile([]), "ResponseMetadata": {"HTTPStatusCode": 200}},
        {"InstanceProfileName": NAME},
    )
    stubs.iam.add_response(
        "add_role_to_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
    )


def group_made(stubs: Stubs, group_id: str = GROUP, vpc_id: str = VPC) -> None:
    """手順 5 の更新で、セキュリティグループを作る。"""
    stubs.ec2.add_response(
        "create_security_group",
        {"GroupId": group_id},
        {
            "GroupName": NAME,
            "Description": resources.GROUP_DESCRIPTION,
            "VpcId": vpc_id,
            "TagSpecifications": [{"ResourceType": "security-group", "Tags": GROUP_TAGS}],
        },
    )


def template_made(stubs: Stubs, data: dict[str, Any]) -> None:
    """手順 5 の更新で、中身が data の起動テンプレートを作る。"""
    stubs.ec2.add_response(
        "create_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE, "DefaultVersionNumber": 1}},
        {
            "LaunchTemplateName": TEMPLATE,
            "LaunchTemplateData": data,
            "TagSpecifications": [{"ResourceType": "launch-template", "Tags": MARK}],
        },
    )


def made_from_scratch(stubs: Stubs, data: dict[str, Any] | None = None) -> None:
    """設定ファイルも AWS のものも無いところからの apply が、読むものと作るものを積む。

    data は作る起動テンプレートの中身で、省けば template_data()。
    """
    chosen(stubs)
    built(stubs, group=None, read_image=False)
    validated(stubs, EC2_POLICY)
    nothing_there(stubs, template=False)
    iam_made(stubs)
    group_made(stubs)
    template_made(stubs, template_data() if data is None else data)


def scaffold_in_place(stubs: Stubs) -> None:
    """雛形の設定ファイルで、AWS のものがすべて揃っているときに読むものを積む。

    雛形は ImageId も SubnetId も書かないので、SubnetId はそのたびに AWS から選び、AMI は
    起動テンプレートのものを使う。
    """
    kept(stubs, template_data())
    built(stubs, read_image=False)
    validated(stubs, EC2_POLICY)
    iam_in_place(stubs)
    group_in_place(stubs)
    template_tags(stubs)


def test_everything_is_made_when_nothing_exists(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """設定ファイルも AWS のものも無いところから、すべてを作る。

    手順 2 は、設定ファイルの代わりに雛形を読み、書かれていない項目を決める。雛形は、
    temt のインスタンスプロファイルを指す IamInstanceProfile と [[role.ec2]] の既定を持つ。
    サービスの AMI はまだ無いので、Ubuntu の公開 AMI を使う。手順 5 は、ロール、その
    ポリシー、インスタンスプロファイル、セキュリティグループ、起動テンプレートを、この順に
    作る。起動テンプレートには、作ったセキュリティグループの ID を入れる。更新では、まず
    雛形を書く。
    """
    made_from_scratch(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == SCAFFOLD
    # 手順 3。サービスのコマンドが書かれる
    assert service.command_path.read_text(encoding="utf-8").endswith(
        'exec temt --svc dev-main "$@"\n'
    )
    # 手順 6。既定の鍵ができていて、Host ブロックがそれを指す
    assert keys.default_path().exists()
    assert keys.public_path(keys.default_path()).exists()
    block = (ssh_dir() / "config").read_text(encoding="utf-8")
    assert f"Host {NAME}\n" in block
    assert "User               ubuntu\n" in block
    assert "Port               22\n" in block
    assert f"IdentityFile       {KEY}\n" in block
    assert f"ProxyCommand       {PLACES} temt proxy --svc dev-main --user '%r' %p\n" in block
    user_config = (Path.home() / ".ssh" / "config").read_text(encoding="utf-8")
    assert user_config.startswith(f"{home.MARK}\nInclude ~/.ssh/temt/config\n")
    out = capsys.readouterr().out
    # 確認の前に、変えるものを手順の順に見せる
    shown = out[: out.index(f"wrote {CONF_SHOWN}")]
    assert heads(shown) == [
        ["+", "conf", "launch-template.IamInstanceProfile.Name"],
        ["+", "conf", "role.ec2"],
        ["+", "dir", "~/.ssh"],
        ["+", "dir", "~/.ssh/temt"],
        ["+", "dir", "~/.ssh/temt/keys"],
        ["+", "command", f"~/.local/bin/{NAME}"],
        ["+", "role", NAME],
        ["+", "role-policy", EC2],
        ["+", "instance-profile", NAME],
        ["+", "security-group", NAME],
        ["+", "launch-template", TEMPLATE],
        ["+", "key", KEY],
        ["+", "ssh-host", NAME],
        ["+", "ssh-include", "~/.ssh/config"],
    ]
    assert details(shown, "conf") == ['+ "${temt:name}"', "+ (default)"]
    assert details(shown, "security-group") == [f"+ in {VPC}"]
    # サービスの AMI がまだ無いので Ubuntu 24.04 を選び、そのわけを添える
    assert details(shown, "launch-template") == [f"+ ImageId: {IMAGE} {UBUNTU_NOTE}"]
    # 更新では、書いたものと作ったものを、この順に知らせる
    reports = [
        f"wrote {CONF_SHOWN}",
        "created ~/.ssh/temt (mode 700)",
        f"wrote ~/.local/bin/{NAME}",
        f"role {NAME}: created",
        f"role policy {EC2}: created",
        f"instance profile {NAME}: created",
        f"security group {NAME}: created ({GROUP})",
        f"launch template {TEMPLATE}: created (version 1)",
        f"created the key {KEY}",
        f"wrote the Host {NAME} block in ~/.ssh/temt/config",
    ]
    places = [out.index(one) for one in reports]
    assert places == sorted(places)


def test_a_new_config_file_has_the_documented_layout(aws_stubs: Stubs, service: Service) -> None:
    """apply が新しく作る設定ファイルは、docs/conf.md の雛形と同じもの。"""
    made_from_scratch(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    text = service.conf_path.read_text(encoding="utf-8")
    assert text == SCAFFOLD
    docs = (Path(__file__).parent.parent / "docs" / "conf.md").read_text(encoding="utf-8")
    assert f"```toml\n{text}```\n" in docs


def test_a_second_apply_finds_no_differences(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """作ったものを読んで比べ直しても、違いは無い。確認も求めず、何も書かない。

    雛形に書いていない値は、そのたびに同じく決まる。
    """
    made_from_scratch(aws_stubs)
    scaffold_in_place(aws_stubs)
    user_config = Path.home() / ".ssh" / "config"

    apply.run(service, yes=True)
    after_first = (
        (ssh_dir() / "config").read_text(),
        user_config.read_text(),
        service.conf_path.read_text(),
    )
    _ = capsys.readouterr()
    apply.run(service, yes=True)

    aws_stubs.done()
    assert (
        (ssh_dir() / "config").read_text(),
        user_config.read_text(),
        service.conf_path.read_text(),
    ) == after_first
    captured = capsys.readouterr()
    assert captured.out.endswith(f"{NAME}: no differences\n")
    assert len(captured.out.splitlines()) == 1
    assert captured.err == ""


def test_a_file_written_ahead_is_left_as_it_is(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """先に作ったファイルには、何も書かない。書かれていない値は、そのたびに決める。

    IamInstanceProfile はファイルが無いときにしか書かないので、インスタンスプロファイル
    無しで起動するテンプレートになり、そのことを警告する。[role] も無いので、インスタンス
    ロールとインスタンスプロファイルは、手放すものが無いことを読むだけである。サービスの
    AMI があるので、それを使う。
    """
    text = '# mine\n[launch-template]\nInstanceType = "t3.small"\n'
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(text, encoding="utf-8")
    public_subnet(aws_stubs)
    subnet(aws_stubs)
    no_template(aws_stubs)
    own_image(aws_stubs)
    built(aws_stubs, group=None, read_image=False)
    aws_stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": []}, THE_GROUP)
    no_iam(aws_stubs)
    group_made(aws_stubs)
    template_made(aws_stubs, template_data(InstanceType="t3.small", IamInstanceProfile=None))

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    captured = capsys.readouterr()
    assert NO_ROLE_WARNING in captured.err
    assert "conf" not in [head[1] for head in heads(captured.out)]
    assert details(captured.out, "launch-template") == [f"+ ImageId: {IMAGE} ({IMAGE_NAME})"]


# IamInstanceProfile が書かれていないときの警告
NO_ROLE_WARNING = (
    "temt: warning: no instance role (no launch-template.IamInstanceProfile); "
    "start may wait for the SSM agent until its deadline\n"
)


def test_nothing_changes_when_everything_matches(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    before = write_conf(service)
    make_key()
    in_place(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == before
    # 設定ファイルに書かれたプロファイルとリージョンで AWS につなぐ
    assert (aws_stubs.passed[0].AwsProfile, aws_stubs.passed[0].AwsRegion) == (None, REGION)
    # AWS には読むことしか求めない（書き込みを積んでいないので、呼べば Stubber が止める）。
    # 変えなかったものは知らせず、手元の ssh の設定とサービスのコマンドだけを作る
    captured = capsys.readouterr()
    assert "unchanged" not in captured.out
    assert f"role {NAME}" not in captured.out
    assert f"wrote the Host {NAME} block" in captured.out
    assert captured.err == ""


def jupyter_in_place(stubs: Stubs, *findings: dict[str, Any]) -> None:
    """WRITTEN に [[role.jupyter]] を足した設定ファイルで読むもの。temt-jupyter は
    ロールにまだ無く、ほかは揃っている。findings は temt-jupyter の検査の指摘。
    """
    subnet(stubs)
    built(stubs)
    validated(stubs, EC2_POLICY)
    validated(stubs, ROLE_POLICY, *findings)
    iam_in_place(stubs, absent=(JUPYTER,))
    group_in_place(stubs)
    template_in_place(stubs, template_data())


def jupyter_put(stubs: Stubs) -> None:
    """手順 5 で、temt-jupyter をロールに付ける。"""
    stubs.iam.add_response(
        "put_role_policy",
        {},
        {"RoleName": NAME, "PolicyName": JUPYTER, "PolicyDocument": json.dumps(ROLE_POLICY)},
    )


def test_a_role_policy_becomes_an_inline_policy(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """変数を置き換え、ValidatePolicy にかけてから、ロールに付ける。"""
    _ = write_conf(service, WRITTEN + STATEMENT)
    make_key()
    jupyter_in_place(aws_stubs)
    jupyter_put(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert f"role policy {JUPYTER}: created" in capsys.readouterr().out


def test_a_warning_on_the_role_statements_goes_on(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ERROR でない指摘は警告として挙げ、ロールに付けるところまで進む。"""
    _ = write_conf(service, WRITTEN + STATEMENT)
    make_key()
    jupyter_in_place(aws_stubs, a_finding("SECURITY_WARNING", "SOME_WARNING"))
    jupyter_put(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err == (
        "temt: warning: temt-jupyter: SECURITY_WARNING SOME_WARNING: details of SOME_WARNING\n"
    )
    assert f"role policy {JUPYTER}: created" in captured.out


def test_an_error_in_the_role_statements_stops_before_aws_changes(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """検査は手順 4 で行うので、ロールに触れずに止まる。"""
    _ = write_conf(service, WRITTEN + STATEMENT)
    subnet(aws_stubs)
    built(aws_stubs)
    validated(aws_stubs, EC2_POLICY)
    finding = {
        **a_finding("ERROR", "INVALID_ACTION"),
        "findingDetails": "The action ssm:GetParameter does not exist.",
        "locations": [
            {
                "path": [{"value": "Statement"}, {"index": 0}, {"value": "Action"}],
                "span": {
                    "start": {"line": 0, "column": 0, "offset": 0},
                    "end": {"line": 0, "column": 1, "offset": 1},
                },
            }
        ],
    }
    validated(aws_stubs, ROLE_POLICY, finding)

    with pytest.raises(TemtError, match=r"found 1 error\(s\) in the role policy temt-jupyter$"):
        apply.run(service, yes=True)

    # ロールの応答は積んでいないので、触れていれば Stubber が失敗する
    aws_stubs.done()
    assert (
        f"temt: {CONF_SHOWN}: role.jupyter[0].Action: ERROR INVALID_ACTION: "
        "The action ssm:GetParameter does not exist."
    ) in capsys.readouterr().err
    assert not (ssh_dir() / "config").exists()


def test_every_role_policy_is_validated_before_stopping(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ERROR が 1 つ目にあっても、最後まで検査して合わせて挙げ、AWS に何も書かない。"""
    _ = write_conf(service, WRITTEN + STATEMENT + STATEMENT.replace("jupyter", "idle"))
    subnet(aws_stubs)
    built(aws_stubs)
    validated(aws_stubs, EC2_POLICY)
    validated(aws_stubs, ROLE_POLICY, a_finding("ERROR", "INVALID_ACTION"))
    validated(aws_stubs, ROLE_POLICY, a_finding("ERROR", "A"), a_finding("ERROR", "B"))

    with pytest.raises(
        TemtError,
        match=r"found 3 error\(s\) in the role policies temt-jupyter, temt-idle$",
    ):
        apply.run(service, yes=True)

    aws_stubs.done()
    err = capsys.readouterr().err
    assert "temt: temt-jupyter: ERROR INVALID_ACTION" in err
    assert "temt: temt-idle: ERROR B" in err
    assert not (ssh_dir() / "config").exists()


def test_a_new_vpc_gets_a_new_group_and_the_template_follows(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """SubnetId を別の VPC のサブネットに変えたときの一続き。

    apply は新しい VPC にセキュリティグループを作り、起動テンプレートをそれに向ける。
    まだ無いセキュリティグループの ID は (new) と見せる。古い VPC のものはそのまま残す。
    delete-security-group を積んでいないので、消していれば Stubber が失敗する。
    """
    other_vpc = "vpc-0fedcba9876543210"
    other_subnet = "subnet-0fedcba9876543210"
    other_group = "sg-0fedcba9876543210"
    text = write_conf(service, edited(f'SubnetId = "{SUBNET}"', f'SubnetId = "{other_subnet}"'))
    make_key()
    subnet(aws_stubs, subnet_id=other_subnet, vpc_id=other_vpc)
    built(aws_stubs, group=None, subnet_id=other_subnet, vpc_id=other_vpc)
    validated(aws_stubs, EC2_POLICY)
    iam_in_place(aws_stubs)
    # 同じ名前のセキュリティグループは古い VPC にしか無い
    aws_stubs.ec2.add_response(
        "describe_security_groups", {"SecurityGroups": [a_group()]}, THE_GROUP
    )
    # 起動テンプレートは、まだ古いサブネットと古いセキュリティグループを指している
    template_in_place(aws_stubs, template_data(), number=1)
    group_made(aws_stubs, other_group, other_vpc)
    new_version(
        aws_stubs,
        template_data(primary={"SubnetId": other_subnet, "Groups": [other_group]}),
        number=2,
    )

    apply.run(service, yes=True)

    aws_stubs.done()
    # 設定ファイルは揃っているので書かない
    assert service.conf_path.read_text(encoding="utf-8") == text
    out = capsys.readouterr().out
    assert details(out, "security-group") == [f"+ in {other_vpc}"]
    assert details(out, "launch-template") == [
        f"~ NetworkInterfaces[DeviceIndex=0].SubnetId: {SUBNET} -> {other_subnet}",
        f"~ NetworkInterfaces[DeviceIndex=0].Groups: {GROUP} -> (new)",
    ]
    assert f"security group {NAME}: created ({other_group})" in out
    assert f"launch template {TEMPLATE}: updated (version 2)" in out


def test_groups_name_security_groups(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """Groups に名前で書いたセキュリティグループは、SubnetId の VPC の中で ID に直す。

    ${temt:name} は temt のセキュリティグループで、無ければ作る。diff はまだ無いものの ID を
    (new) と見せ、更新では作ったものの ID を、書いた順のまま起動テンプレートに入れる。
    """
    text = write_conf(service, in_primary('Groups = ["${temt:name}", "web"]\n'))
    make_key()
    subnet(aws_stubs)
    built(aws_stubs, group=None)
    aws_stubs.ec2.add_response(
        "describe_security_groups", {"SecurityGroups": [a_group(WEB, name="web")]}, in_vpc("web")
    )
    validated(aws_stubs, EC2_POLICY)
    iam_in_place(aws_stubs)
    aws_stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": []}, THE_GROUP)
    template_in_place(aws_stubs, template_data(primary={"Groups": [WEB]}))
    group_made(aws_stubs)
    new_version(aws_stubs, template_data(primary={"Groups": [GROUP, WEB]}))

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    out = capsys.readouterr().out
    assert details(out, "security-group") == [f"+ in {VPC}"]
    assert details(out, "launch-template") == [
        f"~ NetworkInterfaces[DeviceIndex=0].Groups: {WEB} -> (new),{WEB}"
    ]
    assert f"security group {NAME}: created ({GROUP})" in out


def test_a_group_name_that_is_not_in_the_vpc_stops(aws_stubs: Stubs, service: Service) -> None:
    """名前の見つからないセキュリティグループは、AWS に何も書かずに止まる。"""
    _ = write_conf(service, in_primary('Groups = ["web"]\n'))
    subnet(aws_stubs)
    built(aws_stubs)
    aws_stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": []}, in_vpc("web"))

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"no security group named web in {VPC}; fix Groups in {tilde(service.conf_path)}"
    )
    assert not service.command_path.exists()


def test_a_template_without_the_group_of_temt_leaves_it_alone(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """Groups に temt のものが無ければ、temt のセキュリティグループは作りも揃えもしない。

    VPC にあっても、受信規則もタグも読まない（応答を積んでいないので、読めば Stubber が
    失敗する）。
    """
    _ = write_conf(service, in_primary(f'Groups = ["{WEB}"]\n'))
    make_key()
    subnet(aws_stubs)
    built(aws_stubs)
    validated(aws_stubs, EC2_POLICY)
    iam_in_place(aws_stubs)
    template_in_place(aws_stubs, template_data(primary={"Groups": [WEB]}))

    apply.run(service, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert "security-group" not in out
    assert "launch-template" not in out
    assert "security group" not in out


@pytest.mark.parametrize("common", [False, True], ids=["in the config file", "in the common file"])
def test_ingress_rules_without_the_group_of_temt_stop(
    aws_stubs: Stubs, service: Service, common: bool
) -> None:
    """受信規則があるのに起動テンプレートが temt のセキュリティグループを使わなければ、
    規則のあるファイルを挙げ、${temt:name} を Groups に足すよう告げて止まる。
    """
    text = in_primary(f'Groups = ["{WEB}"]\n')
    if common:
        write_defaults(INGRESS)
    else:
        text += INGRESS
    _ = write_conf(service, text)
    subnet(aws_stubs)
    built(aws_stubs)

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    shown = DEFAULTS_SHOWN if common else CONF_SHOWN
    assert str(caught.value) == (
        f"the launch template uses no security group of temt, so the ingress rules in {shown} "
        "cannot be used; add ${temt:name} to Groups in launch-template.NetworkInterfaces, or "
        "remove the rules"
    )
    assert not service.command_path.exists()


def test_the_group_of_temt_written_by_its_id_gets_the_rules(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """temt のセキュリティグループを ID で書いても、temt のものなので受信規則を揃える。"""
    _ = write_conf(service, in_primary(f'Groups = ["{GROUP}"]\n') + INGRESS)
    make_key()
    in_place(aws_stubs)
    aws_stubs.ec2.add_response(
        "authorize_security_group_ingress",
        {"Return": True},
        {
            "GroupId": GROUP,
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "203.0.113.10/32", "Description": "home"}],
                }
            ],
        },
    )

    apply.run(service, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert details(out, "ingress") == ["+ 203.0.113.10/32   home"]
    assert f"security group {NAME}: added 22/tcp 203.0.113.10/32 home" in out
    assert f"security group {NAME}: updated ({GROUP})" in out


def test_an_instance_profile_of_another_leaves_iam_alone(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """[role] が無く、IamInstanceProfile が temt のものを指さなければ、IAM は、手放す
    ロールとインスタンスプロファイルが無いことを読むだけである。

    ValidatePolicy の応答は積んでいないので、呼べば Stubber が失敗する。
    """
    text = write_conf(
        service, edited(PROFILE_LINE, 'IamInstanceProfile = { Name = "other" }\n', PINNED)
    )
    make_key()
    subnet(aws_stubs)
    built(aws_stubs)
    group_in_place(aws_stubs)
    template_in_place(aws_stubs, template_data(IamInstanceProfile={"Name": "other"}))
    no_iam(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    captured = capsys.readouterr()
    assert aws_heads(captured.out) == []
    assert captured.err == ""


# ほかのリージョンのタグ
THERE_TAG = "temt:region:us-west-2"


@pytest.fixture
def calls(ec2: EC2Client, iam: IAMClient) -> list[str]:
    """EC2 と IAM のクライアントが呼んだ API を、呼ばれた順に集める。

    Stubber が見るのはクライアントごとの順序だけなので、クライアントをまたぐ順序は
    これで確かめる。
    """
    made: list[str] = []

    def note(model: Any, **kwargs: Any) -> None:
        _ = kwargs
        made.append(model.name)

    for client in (ec2, iam):
        client.meta.events.register("provide-client-params", note)
    return made


def test_without_role_the_tag_of_the_region_comes_off_and_another_region_keeps_the_role(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str], calls: list[str]
) -> None:
    """[role] が無ければ、ロールからこのリージョンのタグを外す。ほかのリージョンのタグが
    残れば、ロールもインスタンスプロファイルも残す。diff は、外すことと残すことを、使う
    リージョンを添えて見せる。タグを外すのは、起動テンプレートを揃えた後である。
    """
    _ = write_conf(
        service, edited(PROFILE_LINE, 'IamInstanceProfile = { Name = "other" }\n', PINNED)
    )
    set_up_here(service)
    tags = [*ROLE_MARK, {"Key": THERE_TAG, "Value": ""}]
    for _ in range(2):
        subnet(aws_stubs)
        built(aws_stubs)
        group_in_place(aws_stubs)
        template_in_place(
            aws_stubs,
            template_data(IamInstanceProfile={"Name": "other"}, InstanceType="t3.small"),
        )
        aws_stubs.iam.add_response("get_role", a_role(tags), {"RoleName": NAME})

    apply.diff(service)

    out = capsys.readouterr().out
    assert details(out, "role") == [f"~ untag {REGION_TAG}; kept: used in us-west-2"]
    assert aws_heads(out)[-1] == ["~", "role", NAME]
    new_version(aws_stubs, template_data(IamInstanceProfile={"Name": "other"}))
    aws_stubs.iam.add_response("get_role", a_role(tags), {"RoleName": NAME})
    aws_stubs.iam.add_response("untag_role", {}, {"RoleName": NAME, "TagKeys": [REGION_TAG]})
    calls.clear()

    apply.run(service, yes=True)

    aws_stubs.done()
    assert calls[-5:] == [
        "CreateLaunchTemplateVersion",
        "ModifyLaunchTemplate",
        "DeleteLaunchTemplateVersions",
        "GetRole",
        "UntagRole",
    ]
    assert [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()[-3:]] == [
        f"launch template {TEMPLATE}: updated (version 4)",
        f"role {NAME}: untagged {REGION_TAG}",
        f"role {NAME}: kept (used in us-west-2)",
    ]


def test_without_role_the_last_region_deletes_the_role_and_the_instance_profile(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """[role] が無く、ほかのリージョンのタグも無ければ、ロールとインスタンスプロファイルを
    消す。diff は、消すロールに付いたポリシーと、インスタンスプロファイルを見せる。
    """
    _ = write_conf(
        service, edited(PROFILE_LINE, 'IamInstanceProfile = { Name = "other" }\n', PINNED)
    )
    set_up_here(service)
    subnet(aws_stubs)
    built(aws_stubs)
    group_in_place(aws_stubs)
    template_in_place(aws_stubs, template_data(IamInstanceProfile={"Name": "other"}))
    aws_stubs.iam.add_response("get_role", a_role(), {"RoleName": NAME})
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": [EC2]}, {"RoleName": NAME})
    aws_stubs.iam.add_response(
        "list_attached_role_policies", attached(SSM_CORE), {"RoleName": NAME}
    )
    aws_stubs.iam.add_response(
        "get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME}
    )

    apply.diff(service)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert aws_heads(out) == [["-", "role", NAME], ["-", "instance-profile", NAME]]
    assert details(out, "role") == [
        f"- inline policies: {EC2}; managed policies: {SSM_CORE}",
    ]


def test_an_empty_role_releases_the_role_as_no_role_does(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """中身の無い [role] は書かれていないのと同じで、ロールを揃えずに手放す側に回る。"""
    _ = write_conf(
        service,
        edited(PROFILE_LINE, 'IamInstanceProfile = { Name = "other" }\n', PINNED)
        + "\n[role]\nManagedPolicyArns = []\n",
    )
    set_up_here(service)
    subnet(aws_stubs)
    built(aws_stubs)
    group_in_place(aws_stubs)
    template_in_place(aws_stubs, template_data(IamInstanceProfile={"Name": "other"}))
    aws_stubs.iam.add_response("get_role", a_role(), {"RoleName": NAME})
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
    aws_stubs.iam.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
    missing(
        aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME
    )

    apply.diff(service)

    aws_stubs.done()
    assert aws_heads(capsys.readouterr().out) == [["-", "role", NAME]]


def test_without_role_a_role_that_cannot_be_deleted_stops_after_the_template(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """消せるかは確認の前には確かめない。消せないロールなら、apply は起動テンプレートを
    揃えた後で、destroy と同じ誤りで止まる。
    """
    _ = write_conf(
        service, edited(PROFILE_LINE, 'IamInstanceProfile = { Name = "other" }\n', PINNED)
    )
    set_up_here(service)
    subnet(aws_stubs)
    built(aws_stubs)
    group_in_place(aws_stubs)
    template_in_place(
        aws_stubs, template_data(IamInstanceProfile={"Name": "other"}, InstanceType="t3.small")
    )
    aws_stubs.iam.add_response("get_role", a_role(), {"RoleName": NAME})
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
    aws_stubs.iam.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
    missing(
        aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME
    )
    new_version(aws_stubs, template_data(IamInstanceProfile={"Name": "other"}))
    aws_stubs.iam.add_response("get_role", a_role(), {"RoleName": NAME})
    aws_stubs.iam.add_response("untag_role", {}, {"RoleName": NAME, "TagKeys": [REGION_TAG]})
    aws_stubs.iam.add_response(
        "list_instance_profiles_for_role",
        {
            "InstanceProfiles": [
                {**a_profile([NAME])["InstanceProfile"], "InstanceProfileName": "shared"}
            ]
        },
        {"RoleName": NAME},
    )

    with pytest.raises(
        TemtError, match=r"also in the instance profile shared; .* run apply again$"
    ):
        apply.run(service, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert aws_heads(out) == [["~", "launch-template", TEMPLATE], ["-", "role", NAME]]
    assert f"launch template {TEMPLATE}: updated (version 4)" in out


@pytest.mark.parametrize(
    ("profile", "data"),
    [
        ('IamInstanceProfile = { Name = "other" }\n', {"Name": "other"}),
        ("", None),
    ],
    ids=["another instance profile", "no instance profile"],
)
def test_the_role_is_made_whatever_the_instance_profile_points_at(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    data: dict[str, str] | None,
) -> None:
    """[role] があれば、IamInstanceProfile がどこを指しても（書いていなくても）、temt の
    インスタンスロールとインスタンスプロファイルを揃える。インスタンスに付けるかは
    IamInstanceProfile で決まる。書いていなければ、インスタンスロールが付かないことを警告する。
    """
    _ = write_conf(service, edited(PROFILE_LINE, profile))
    make_key()
    in_place(aws_stubs, template_data(IamInstanceProfile=data))

    apply.run(service, yes=True)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert aws_heads(captured.out) == []
    assert captured.err == ("" if profile else NO_ROLE_WARNING)


@pytest.mark.parametrize("command", ["apply", "diff"])
@pytest.mark.parametrize(
    "profile",
    [
        pytest.param(PROFILE_LINE, id="variable"),
        pytest.param(f'IamInstanceProfile.Name = "{NAME}"\n', id="name"),
        pytest.param(
            f'IamInstanceProfile.Arn = "arn:aws:iam::123456789012:instance-profile/{NAME}"\n',
            id="arn",
        ),
    ],
)
def test_the_instance_profile_of_temt_without_role_stops(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    command: str,
    profile: str,
) -> None:
    """temt のインスタンスプロファイルを指すのに、どのファイルにも [role] が無ければ、AWS を
    読む前に、IamInstanceProfile を書いたファイルを挙げて止まる。
    """
    text = write_conf(service, edited(PROFILE_LINE, profile, PINNED))

    with pytest.raises(TemtError) as caught:
        if command == "apply":
            apply.run(service, yes=True)
        else:
            apply.diff(service)

    # 応答を 1 つも積んでいないので、AWS を読んでいれば Stubber が失敗する
    aws_stubs.done()
    assert str(caught.value) == (
        f"{CONF_SHOWN}: launch-template.IamInstanceProfile points at {NAME}, but there is no [role]"
    )
    assert service.conf_path.read_text(encoding="utf-8") == text
    assert capsys.readouterr().out == ""


def test_the_role_check_names_the_common_file(aws_stubs: Stubs, service: Service) -> None:
    """IamInstanceProfile を共通ファイルに書いていれば、共通ファイルを挙げる。"""
    write_defaults('[launch-template]\nIamInstanceProfile.Name = "${temt:name}"\n')
    _ = write_conf(service, edited(PROFILE_LINE, "", PINNED))

    with pytest.raises(TemtError) as caught:
        apply.diff(service)

    aws_stubs.done()
    assert str(caught.value) == (
        f"{DEFAULTS_SHOWN}: launch-template.IamInstanceProfile points at {NAME}, "
        "but there is no [role]"
    )


@pytest.mark.parametrize(
    ("top", "role"),
    [
        pytest.param("", "\n[role]\nManagedPolicyArns = []\n", id="empty role"),
        pytest.param("Defaults = false\n", "", id="role only in the common file"),
    ],
)
def test_a_role_that_does_not_count_stops_the_instance_profile_of_temt(
    aws_stubs: Stubs, service: Service, top: str, role: str
) -> None:
    """中身の無い [role] は書かれていないのと同じ。Defaults = false なら共通ファイルの
    [role] は効かない。どちらも temt のインスタンスプロファイルを指していれば止まる。"""
    if top:
        write_defaults(f'[role]\nManagedPolicyArns = ["{S3_READ}"]\n')
    _ = write_conf(service, top + PINNED + role)

    with pytest.raises(TemtError, match=r"points at temt-dev-main, but there is no \[role\]$"):
        apply.run(service, yes=True)

    aws_stubs.done()


@pytest.mark.parametrize("wait", [True, False])
def test_diff_warns_without_an_instance_profile_unless_ssm_is_not_awaited(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str], wait: bool
) -> None:
    """IamInstanceProfile が無ければ、diff も警告する。WaitForSsm = false なら警告しない。"""
    top = "" if wait else "WaitForSsm = false\n"
    _ = write_conf(service, top + edited(PROFILE_LINE, "", PINNED))
    set_up_here(service)
    subnet(aws_stubs)
    built(aws_stubs)
    group_in_place(aws_stubs)
    template_in_place(aws_stubs, template_data(IamInstanceProfile=None))
    no_iam(aws_stubs)

    apply.diff(service)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (NO_ROLE_WARNING in captured.err) is wait


def test_a_role_in_the_common_file_is_joined(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """共通ファイルの同じ名前のポリシーは、置き換えずにつなげる。誤りは、Statement を書いた
    ファイルと、その中での位置で挙げる。"""
    write_defaults(STATEMENT)
    _ = write_conf(service, edited(PROFILE_LINE, PROFILE_LINE, PINNED) + STATEMENT)
    subnet(aws_stubs)
    built(aws_stubs)
    joined = {**ROLE_POLICY, "Statement": ROLE_POLICY["Statement"] * 2}

    def at(index: int) -> dict[str, Any]:
        path = [{"value": "Statement"}, {"index": index}, {"value": "Action"}]
        span = {
            "start": {"line": 0, "column": 0, "offset": 0},
            "end": {"line": 0, "column": 1, "offset": 1},
        }
        return {**a_finding("ERROR", "X"), "locations": [{"path": path, "span": span}]}

    validated(aws_stubs, joined, at(0), at(1))

    with pytest.raises(TemtError, match=r"found 2 error\(s\) in the role policy temt-jupyter$"):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert capsys.readouterr().err.splitlines() == [
        f"temt: {DEFAULTS_SHOWN}: role.jupyter[0].Action: ERROR X: details of X",
        f"temt: {CONF_SHOWN}: role.jupyter[0].Action: ERROR X: details of X",
    ]


def test_managed_policies_are_attached_and_detached(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ManagedPolicyArns の管理ポリシーを付け、無いものを外す。付けてから外す。

    diff は付けるものを +、外すものを - で見せる。
    """
    _ = write_conf(service, before_role(f'\n[role]\nManagedPolicyArns = ["{S3_READ}"]\n'))
    make_key()
    for _ in range(2):
        subnet(aws_stubs)
        built(aws_stubs)
        validated(aws_stubs, EC2_POLICY)
        iam_in_place(aws_stubs, managed=(SSM_CORE,))
        group_in_place(aws_stubs)
        template_in_place(aws_stubs, template_data())

    apply.diff(service)

    assert aws_heads(capsys.readouterr().out) == [
        ["+", "managed-policy", S3_READ],
        ["-", "managed-policy", SSM_CORE],
    ]
    aws_stubs.iam.add_response("attach_role_policy", {}, {"RoleName": NAME, "PolicyArn": S3_READ})
    aws_stubs.iam.add_response("detach_role_policy", {}, {"RoleName": NAME, "PolicyArn": SSM_CORE})

    apply.run(service, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert f"role {NAME}: attached {S3_READ}" in out
    assert f"role {NAME}: detached {SSM_CORE}" in out


@pytest.mark.parametrize("what", ["role", "instance profile"])
def test_a_role_or_instance_profile_of_another_service_stops(
    aws_stubs: Stubs, service: Service, what: str
) -> None:
    """同じ名前のロールかインスタンスプロファイルに、別のサービスの temt:service が付いて
    いれば、AWS に何も書かずに止まる。
    """
    _ = write_conf(service)
    subnet(aws_stubs)
    built(aws_stubs)
    validated(aws_stubs, EC2_POLICY)
    if what == "role":
        aws_stubs.iam.add_response("get_role", a_role(OTHER_MARK), {"RoleName": NAME})
    else:
        iam_in_place(aws_stubs, profile_tags=OTHER_MARK)

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the {what} {NAME} has temt:service=other, so it belongs to another service; fix the "
        "tag or delete it by hand"
    )
    assert not service.command_path.exists()


def test_the_ipv6_of_the_subnet_goes_to_the_template(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ipv6AddressCount が無く、サブネットに IPv6 の CIDR が結び付いていれば、1 を起動
    テンプレートに入れる。設定ファイルには書かない。
    """
    text = write_conf(service)
    make_key()
    subnet(aws_stubs, ipv6=True)
    built(aws_stubs)
    validated(aws_stubs, EC2_POLICY)
    iam_in_place(aws_stubs)
    group_in_place(aws_stubs)
    template_in_place(aws_stubs, template_data())
    new_version(aws_stubs, template_data(primary={"Ipv6AddressCount": 1}))

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    out = capsys.readouterr().out
    assert details(out, "conf") == []
    assert details(out, "launch-template") == [
        "~ NetworkInterfaces[DeviceIndex=0].Ipv6AddressCount: - -> 1"
    ]
    assert f"launch template {TEMPLATE}: updated (version 4)" in out


def test_a_written_ipv6_address_count_is_kept(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ipv6AddressCount を書いていれば、IPv6 を決めるためにはサブネットを見ず、その値を使う。

    サブネットは、起動テンプレートを組み立てるときに読む。
    """
    text = write_conf(service, in_primary("Ipv6AddressCount = 0\n"))
    make_key()
    built(aws_stubs, read_subnet=True)
    validated(aws_stubs, EC2_POLICY)
    iam_in_place(aws_stubs)
    group_in_place(aws_stubs)
    template_in_place(aws_stubs, template_data(primary={"Ipv6AddressCount": 0}))

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    assert "launch-template" not in capsys.readouterr().out


def test_a_deleted_credit_specification_is_filled(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """T 系の InstanceType で CreditSpecification の行を消せば、unlimited で埋める。
    起動テンプレートは変わらず、設定ファイルにも書かない。"""
    table = '\n[launch-template.CreditSpecification]\nCpuCredits = "unlimited"\n'
    text = write_conf(service, edited(table, ""))
    make_key()
    in_place(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    assert aws_heads(capsys.readouterr().out) == []


def test_a_credit_specification_left_on_another_type_is_kept(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """T 系でない InstanceType に変えても、書いてある CreditSpecification は残し、起動
    テンプレートにも入れる。AWS が起動を断るので、利用者が行を消す。
    """
    _ = write_conf(service, edited('InstanceType = "t3.micro"', 'InstanceType = "m7i.large"'))
    make_key()
    in_place(aws_stubs)
    new_version(aws_stubs, template_data(InstanceType="m7i.large"))

    apply.run(service, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert details(out, "conf") == []
    assert details(out, "launch-template") == ["~ InstanceType: t3.micro -> m7i.large"]


def test_an_inline_table_is_read_as_it_is(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """インラインテーブルで書いた項目にも、書かれていない値を埋める。ファイルには書かない。"""
    text = write_conf(
        service,
        edited(
            PROFILE_LINE,
            PROFILE_LINE + 'MetadataOptions = { HttpTokens = "required" }\n',
            edited(METADATA_TABLE, ""),
        ),
    )
    make_key()
    in_place(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    assert aws_heads(capsys.readouterr().out) == []


def test_a_new_service_reads_the_scaffold_over_the_common_file(
    aws_stubs: Stubs, service: Service
) -> None:
    """雛形は共通ファイルの上に重ねて読む。共通ファイルの値は雛形に写さない。"""
    write_defaults('AwsProfile = ""\n[launch-template]\nInstanceType = "t3.small"\n')
    made_from_scratch(aws_stubs, template_data(InstanceType="t3.small"))

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == SCAFFOLD


def test_the_instance_profile_of_the_common_file_is_left_out_of_the_scaffold(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """共通ファイルに IamInstanceProfile があれば、雛形にその行を書かず、共通ファイルの値を
    使う。temt のインスタンスプロファイルを指さないので、[[role.ec2]] の既定も書かず、IAM
    は、手放すものが無いことを読むだけである。
    """
    write_defaults('[launch-template]\nIamInstanceProfile = { Name = "shared" }\n')
    chosen(aws_stubs)
    built(aws_stubs, group=None, read_image=False)
    aws_stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": []}, THE_GROUP)
    no_iam(aws_stubs)
    group_made(aws_stubs)
    template_made(aws_stubs, template_data(IamInstanceProfile={"Name": "shared"}))

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == conf.scaffold(profile=False, ec2=False)
    assert "conf" not in [head[1] for head in heads(capsys.readouterr().out)]


def test_the_scaffold_is_written_even_without_other_differences(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """雛形が書く値が無く、AWS も手元も揃っていても、設定ファイルが無ければ雛形を書く。"""
    write_defaults('[launch-template]\nIamInstanceProfile = { Name = "shared" }\n')
    set_up_here(service)
    kept(aws_stubs, template_data(IamInstanceProfile={"Name": "shared"}))
    built(aws_stubs, read_image=False)
    group_in_place(aws_stubs)
    template_tags(aws_stubs)
    no_iam(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == conf.scaffold(profile=False, ec2=False)
    out = capsys.readouterr().out
    assert "no differences" not in out
    assert f"wrote {CONF_SHOWN}" in out


def test_the_scaffold_has_the_ec2_default_for_the_common_instance_profile_of_temt(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """共通ファイルの IamInstanceProfile が temt のインスタンスプロファイルを指していれば、
    雛形にその行は書かず、[[role.ec2]] の既定だけを書く。"""
    write_defaults('[launch-template]\nIamInstanceProfile.Name = "${temt:name}"\n')
    made_from_scratch(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == conf.scaffold(profile=False, ec2=True)
    assert [head for head in heads(capsys.readouterr().out) if head[1] == "conf"] == [
        ["+", "conf", "role.ec2"]
    ]


def test_the_ec2_policy_of_the_common_file_is_joined_to_the_scaffold(
    aws_stubs: Stubs, service: Service
) -> None:
    """共通ファイルの [[role.ec2]] の後ろに、雛形の既定がつながる。"""
    write_defaults(STATEMENT.replace("jupyter", "ec2"))
    joined = {
        **EC2_POLICY,
        "Statement": [*ROLE_POLICY["Statement"], *EC2_POLICY["Statement"]],
    }
    chosen(aws_stubs)
    built(aws_stubs, group=None, read_image=False)
    validated(aws_stubs, joined)
    nothing_there(aws_stubs, template=False)
    aws_stubs.iam.add_response(
        "create_role",
        a_role(),
        {
            "RoleName": NAME,
            "AssumeRolePolicyDocument": resources.TRUST_POLICY,
            "Tags": ROLE_MARK,
        },
    )
    aws_stubs.iam.add_response(
        "put_role_policy",
        {},
        {"RoleName": NAME, "PolicyName": EC2, "PolicyDocument": json.dumps(joined)},
    )
    aws_stubs.iam.add_client_error(
        "create_instance_profile", service_error_code="AccessDenied", http_status_code=403
    )

    with pytest.raises(ClientError):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == SCAFFOLD


def test_nothing_is_written_when_detecting_stops(aws_stubs: Stubs, service: Service) -> None:
    """検出の途中で止まれば、確認の前なので、雛形も手元のファイルも書かない。"""
    aws_stubs.ec2.add_response("describe_vpcs", {"Vpcs": []}, DEFAULT_VPC)

    with pytest.raises(TemtError, match="no default VPC"):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not service.conf_path.exists()
    assert not service.command_path.exists()


def test_a_config_file_made_meanwhile_is_not_overwritten(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """確かめてから書くまでに設定ファイルができていれば、雛形で上書きせずに止まる。"""
    chosen(aws_stubs)
    built(aws_stubs, group=None, read_image=False)
    validated(aws_stubs, EC2_POLICY)
    nothing_there(aws_stubs, template=False)
    confirm = apply.msg.confirm
    asked: list[None] = []

    def made_meanwhile(question: Callable[[], str], *, yes: bool) -> None:
        confirm(question, yes=yes)
        asked.append(None)
        # 1 度めはサービスを作るか、2 度めは変更を反映するかの確認
        if len(asked) == 2:
            _ = write_conf(service, "# mine\n")

    monkeypatch.setattr(apply.msg, "confirm", made_meanwhile)

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == f"{CONF_SHOWN} was made while running apply; it is left as it is"
    assert service.conf_path.read_text(encoding="utf-8") == "# mine\n"
    assert not service.command_path.exists()


def test_it_stops_before_writing_to_aws_when_the_subnet_is_gone(
    aws_stubs: Stubs, service: Service
) -> None:
    """サブネットが無ければ、検出の途中なので、AWS にも手元にも何も作らない。"""
    _ = write_conf(service)
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": []},
        {"Filters": [{"Name": "subnet-id", "Values": [SUBNET]}]},
    )

    with pytest.raises(TemtError, match=f"^no such subnet: {SUBNET}; for another account, "):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not service.command_path.exists()
    assert not keys.default_path().exists()
    assert not (ssh_dir() / "config").exists()


def test_it_stops_before_writing_to_aws_when_the_image_is_gone(
    aws_stubs: Stubs, service: Service
) -> None:
    """ImageId の AMI が無ければ、行を消すよう案内して止まる。"""
    _ = write_conf(service)
    subnet(aws_stubs)
    aws_stubs.ec2.add_response("describe_images", {"Images": []}, {"ImageIds": [IMAGE]})

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"no such AMI: {IMAGE}; write another AMI in launch-template.ImageId of "
        f"{tilde(service.conf_path)}, or remove it and choose one with --image-id"
    )
    assert not keys.default_path().exists()


def sized(size: int) -> str:
    """WRITTEN に、DeviceName を省いた BlockDeviceMappings の要素で VolumeSize を書いたもの。"""
    return before_role(f"\n[[launch-template.BlockDeviceMappings]]\nEbs.VolumeSize = {size}\n")


# ルートボリュームの大きさの位置。省いた DeviceName は AMI のルートデバイスで埋まる
VOLUME_SIZE = f"BlockDeviceMappings[DeviceName={ROOT_DEVICE}].Ebs.VolumeSize"


def test_the_volume_size_goes_to_the_root_device_of_the_ami(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """DeviceName を省いた要素は、AMI のルートデバイスを指す。埋めた値は設定ファイルに
    書かない。diff はその位置で違いを見せ、apply は同じものを起動テンプレートに入れる。
    """
    text = write_conf(service, sized(30))
    make_key()
    for _ in range(2):
        subnet(aws_stubs)
        built(aws_stubs, image=an_image(size=8))
        validated(aws_stubs, EC2_POLICY)
        iam_in_place(aws_stubs)
        group_in_place(aws_stubs)
        template_in_place(aws_stubs, template_data())

    apply.diff(service)

    assert details(capsys.readouterr().out, "launch-template") == [f"~ {VOLUME_SIZE}: - -> 30"]
    mappings = [{"Ebs": {"VolumeSize": 30}, "DeviceName": ROOT_DEVICE}]
    new_version(aws_stubs, template_data(BlockDeviceMappings=mappings))

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    assert f"launch template {TEMPLATE}: updated (version 4)" in capsys.readouterr().out


@pytest.mark.parametrize("common", [False, True], ids=["in the config file", "in the common file"])
def test_it_stops_before_writing_to_aws_when_the_volume_size_is_too_small(
    aws_stubs: Stubs, service: Service, common: bool
) -> None:
    """AMI より小さい VolumeSize は、AWS が起動するときに断るので、その前に止まる。

    直す先として、VolumeSize を書いたファイルを挙げる。
    """
    if common:
        _ = write_conf(service)
        write_defaults("[[launch-template.BlockDeviceMappings]]\nEbs.VolumeSize = 6\n")
    else:
        _ = write_conf(service, sized(6))
    subnet(aws_stubs)
    built(aws_stubs, image=an_image(size=8), stop=True)

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    place = defaults_path() if common else service.conf_path
    assert str(caught.value) == (
        f"launch-template.{VOLUME_SIZE} 6 is smaller than the root volume of {IMAGE} (8 GiB); "
        f"raise it or remove it in {tilde(place)}"
    )
    assert not keys.default_path().exists()


def test_it_stops_when_the_image_has_no_ebs_root_volume(aws_stubs: Stubs, service: Service) -> None:
    """AMI に EBS のルートボリュームが無ければ、大きさを決められないので止まる。"""
    _ = write_conf(service, sized(30))
    subnet(aws_stubs)
    built(aws_stubs, image=an_image(size=None), stop=True)

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the AMI {IMAGE} has no EBS root volume; remove Ebs.VolumeSize from "
        f"{tilde(service.conf_path)}"
    )


def text_data(text: str) -> str:
    """UserData の書き方。文書を base64 にしたもの。"""
    return base64.b64encode(text.encode()).decode()


def test_diff_shows_each_item_of_the_template_that_differs(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """起動テンプレートの違いを、項目の位置ごとに 1 行で見せる。設定ファイルに無い項目は
    消すものとして見せる。UserData は戻した文書の違いを続く行に出す。apply は設定ファイル
    のとおりの中身で新しいバージョンを作る。
    """
    new = text_data("#!/bin/sh\necho new\n")
    _ = write_conf(
        service,
        edited('InstanceType = "t3.micro"\n', f'InstanceType = "t3.micro"\nUserData = "{new}"\n'),
    )
    set_up_here(service)
    current = template_data(
        InstanceType="t3.small",
        KeyName="mykey",
        UserData=text_data("#!/bin/sh\necho old\n"),
        primary={"SubnetId": "subnet-0fedcba9876543210"},
    )
    in_place(aws_stubs, current)
    in_place(aws_stubs, current)

    apply.diff(service)

    out = capsys.readouterr().out
    assert out == (
        f"~   launch-template   {TEMPLATE}   InstanceType: t3.small -> t3.micro\n"
        f"~   launch-template   {TEMPLATE}   UserData: (text) -> (text)\n"
        "    @@ -1,2 +1,2 @@\n"
        "     #!/bin/sh\n"
        "    -echo old\n"
        "    +echo new\n"
        f"~   launch-template   {TEMPLATE}   NetworkInterfaces[DeviceIndex=0].SubnetId: "
        f"subnet-0fedcba9876543210 -> {SUBNET}\n"
        f"~   launch-template   {TEMPLATE}   KeyName: mykey -> -\n"
    )
    new_version(aws_stubs, template_data(UserData=new))

    apply.run(service, yes=True)

    aws_stubs.done()
    assert f"launch template {TEMPLATE}: updated (version 4)" in capsys.readouterr().out


def test_it_stops_before_writing_when_ssh_keygen_is_missing(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定の鍵を作るときだけ ssh-keygen が要る。無ければ、確認の前の検出で止まる。"""
    _ = write_conf(service)
    in_place(aws_stubs)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(TemtError, match=f"command not found: {keys.KEYGEN}"):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not service.command_path.exists()
    assert not ssh_dir().exists()


def test_apply_needs_no_ssh_keygen_when_the_key_is_in_place(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    monkeypatch.setenv("PATH", "")

    apply.run(service, yes=True)

    aws_stubs.done()
    assert f"IdentityFile       {KEY}\n" in (ssh_dir() / "config").read_text(encoding="utf-8")


def test_it_stops_when_another_conf_dir_owns_the_service(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同じサービスの設定ファイルが別の場所に残っていれば、何も書かずに止まる。"""
    _ = write_conf(service)
    home.prepare_dirs()
    assert home.write_edit(home.detect_host_block(service)) is True
    monkeypatch.setenv(CONF_DIR_ENV, str(service.command_path.parent / "elsewhere"))

    with pytest.raises(TemtError, match="set up from another config directory"):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not service.conf_path.exists()
    assert not service.command_path.exists()
    # ssh の設定にも触れていない
    assert f"{CONF_DIR_ENV}='~/.config/temt'" in (ssh_dir() / "config").read_text(encoding="utf-8")
    assert not (Path.home() / ".ssh" / "config").exists()


def test_a_service_moved_to_another_conf_dir_is_set_up_from_there(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前の場所に設定ファイルが無ければ、移したものとして進む。

    ProxyCommand は新しい場所を渡すようになる。
    """
    home.prepare_dirs()
    assert home.write_edit(home.detect_host_block(service)) is True
    monkeypatch.setenv(CONF_DIR_ENV, str(service.command_path.parent / "elsewhere"))
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    block = (ssh_dir() / "config").read_text(encoding="utf-8")
    assert f"env {CONF_DIR_ENV}='~/.local/bin/elsewhere' {SSH_DIR_ENV}='~/.ssh/temt' " in block


def test_moving_the_ssh_dir_warns_about_the_hosts_left_behind(
    aws_stubs: Stubs,
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """手順 6 で Include を張り替えると、前の ssh ディレクトリの Host が見えなくなる。"""
    _ = write_conf(service)
    home.prepare_dirs()
    (ssh_dir() / "config").write_text("Host temt-dev-old\n    User ubuntu\n", encoding="utf-8")
    _ = home.write_include(home.detect_include(), home.detect_host_block(service))
    monkeypatch.setenv(SSH_DIR_ENV, str(Path.home() / "elsewhere"))
    make_key()
    in_place(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert (
        "warning: ~/.ssh/config no longer includes ~/.ssh/temt/config; "
        "ssh cannot reach temt-dev-old there"
    ) in capsys.readouterr().err


def test_the_option_is_not_written_as_the_profile(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--profile はその実行のあいだだけ効き、設定ファイルには書かない。"""
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    before = write_conf(service)
    make_key()
    in_place(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert "AwsProfile" not in capsys.readouterr().out
    # ProxyCommand にも --profile は入らない
    assert "--profile" not in (ssh_dir() / "config").read_text(encoding="utf-8")


def test_the_scaffold_stays_when_a_resource_fails(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """雛形は更新の最初に書くので、手順 5 の更新で止まっても残る。AwsProfile も AwsRegion も
    書かない。鍵は手順 6 で作るので、まだ作らない。
    """
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    chosen(aws_stubs)
    built(aws_stubs, group=None, read_image=False)
    validated(aws_stubs, EC2_POLICY)
    nothing_there(aws_stubs, template=False)
    iam_made(aws_stubs)
    group_made(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "create_launch_template", service_error_code="AccessDenied", http_status_code=403
    )

    with pytest.raises(ClientError):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not keys.default_path().exists()
    assert not (ssh_dir() / "config").exists()
    assert service.conf_path.read_text(encoding="utf-8") == SCAFFOLD


def test_the_written_profile_is_left_alone_without_the_option(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """--profile が無ければ、AwsProfile は書き足しも書き換えもしない。"""
    before = write_conf(service, 'AwsProfile = "work"\n' + WRITTEN)
    make_key()
    in_place(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert aws_stubs.passed[0].AwsProfile == "work"
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert "wrote AwsProfile" not in capsys.readouterr().out


# InstanceType を t3.small に変えた設定ファイル
RESIZED = edited('InstanceType = "t3.micro"', 'InstanceType = "t3.small"')


def test_the_ssh_config_is_left_alone_when_a_resource_fails(
    aws_stubs: Stubs, service: Service
) -> None:
    """手順 6 は手順 5 の後なので、手順 5 の更新で止まれば ssh の設定は書かれない。"""
    _ = write_conf(service, RESIZED)
    make_key()
    in_place(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "create_launch_template_version", service_error_code="AccessDenied", http_status_code=403
    )

    with pytest.raises(ClientError):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not (ssh_dir() / "config").exists()


def test_diff_shows_nothing_after_apply(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """apply の直後は、標準出力に何も書かず、違いが無いことを標準エラー出力に知らせる。"""
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    in_place(aws_stubs)
    apply.run(service, yes=True)
    _ = capsys.readouterr()

    apply.diff(service)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"{NAME}: no differences" in captured.err


def test_diff_shows_what_apply_makes_and_writes_nothing(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """何も無いところでは、apply が作るものを手順の順に + で出す。

    AWS には読むことしか求めず（書き込みを積んでいないので、呼べば Stubber が止める）、
    手元のファイルも作らない。
    """
    before = write_conf(service)
    subnet(aws_stubs)
    built(aws_stubs, group=None)
    validated(aws_stubs, EC2_POLICY)
    nothing_there(aws_stubs)

    apply.diff(service)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert heads(out) == [
        ["+", "dir", "~/.ssh"],
        ["+", "dir", "~/.ssh/temt"],
        ["+", "dir", "~/.ssh/temt/keys"],
        ["+", "command", f"~/.local/bin/{NAME}"],
        ["+", "role", NAME],
        ["+", "role-policy", EC2],
        ["+", "instance-profile", NAME],
        ["+", "security-group", NAME],
        ["+", "launch-template", TEMPLATE],
        # 既定の鍵は、それを指す Host ブロックの直前に出す
        ["+", "key", KEY],
        ["+", "ssh-host", NAME],
        ["+", "ssh-include", "~/.ssh/config"],
    ]
    # 足すポリシーと Host ブロックは、中身を + の行で続けて出す
    assert '    +  "Version": "2012-10-17"' in out
    assert f"    +Host {NAME}" in out
    assert f"    +    IdentityFile       {KEY}" in out
    assert [line.split() for line in out.splitlines() if line.startswith("+   key ")] == [
        ["+", "key", KEY, "ssh-keygen", "-t", "ed25519"]
    ]
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert not service.command_path.exists()
    assert not ssh_dir().exists()
    assert not (Path.home() / ".ssh" / "config").exists()
    assert not keys.default_path().exists()


def test_diff_fills_what_is_not_written(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """書かれていない値は、apply と同じく決めて比べる。設定ファイルも --profile も書かない。

    SubnetId が書かれていなければ、apply と同じく公開サブネットを選ぶ。選んだものが
    テンプレートのものと同じなので、違いは出ない。
    """
    before = write_conf(service, edited(f'SubnetId = "{SUBNET}"\n', ""))
    set_up_here(service)
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    public_subnet(aws_stubs)
    in_place(aws_stubs)

    apply.diff(service)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith(f"{NAME}: no differences\n")
    assert service.conf_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("kept", "message"),
    [
        pytest.param(
            "private",
            f"{KEY}.pub does not exist; make it with 'ssh-keygen -y -f {KEY} > {KEY}.pub'",
            id="only the private key",
        ),
        pytest.param(
            "public",
            f"{KEY}.pub exists without {KEY}; remove it to create a new key",
            id="only the public key",
        ),
    ],
)
def test_diff_and_apply_stop_when_only_one_half_of_the_key_exists(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    kept: str,
    message: str,
) -> None:
    """既定の鍵の片方だけがあれば、作り直すともう片方を失うか食い違うので、確認の前に止まる。

    diff は、それまでに検出した変更を出してから、apply と同じエラーで終わる。apply は
    何も見せず、確認も求めず、何も書かない。
    """
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    apply.run(service, yes=True)
    gone = keys.public_path(keys.default_path()) if kept == "private" else keys.default_path()
    gone.unlink()
    _ = write_conf(service, RESIZED)
    in_place(aws_stubs)
    in_place(aws_stubs)
    monkeypatch.setattr(sys, "stdin", Terminal("y\n"))
    _ = capsys.readouterr()

    with pytest.raises(TemtError) as caught:
        apply.diff(service)
    assert str(caught.value) == message
    assert capsys.readouterr().out == (
        f"~   launch-template   {TEMPLATE}   InstanceType: t3.micro -> t3.small\n"
    )
    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=False)
    assert str(caught.value) == message

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[y/N]" not in captured.err
    assert not gone.exists()


def test_diff_asks_for_apply_without_the_config_file(aws_stubs: Stubs, service: Service) -> None:
    """設定ファイルが無ければ、何も作らずに apply を促す。"""
    with pytest.raises(TemtError, match="no config file: .*run 'temt apply --svc dev-main' first"):
        apply.diff(service)

    aws_stubs.done()
    assert not service.conf_path.exists()


def test_diff_after_moving_the_ssh_dir_lists_what_apply_warns_about(
    aws_stubs: Stubs,
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Include の張り替えで見えなくなる Host は、apply が警告するものと同じだけを挙げる。

    このサービスの Host は、apply が新しい ssh ディレクトリへ先に書くので、挙げない。
    """
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    apply.run(service, yes=True)
    with (ssh_dir() / "config").open("a", encoding="utf-8") as file:
        _ = file.write("\nHost temt-dev-old\n    User ubuntu\n")
    old_keys = keys_dir()
    monkeypatch.setenv(SSH_DIR_ENV, str(Path.home() / "elsewhere"))
    # 新しい ssh ディレクトリに書く Host ブロックは、その下の既定の鍵を指すので、同じ鍵を
    # 移しておく
    keys_dir().mkdir(parents=True)
    for name in ("temt", "temt.pub"):
        _ = shutil.copy2(old_keys / name, keys_dir() / name)
    in_place(aws_stubs)
    _ = capsys.readouterr()

    apply.diff(service)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert ["~", "ssh-include", "~/.ssh/config"] in heads(out)
    assert "    ssh cannot reach temt-dev-old in ~/.ssh/temt/config\n" in out
    assert f"cannot reach {NAME}" not in out


def test_diff_and_apply_agree_on_a_new_instance_type(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """同じ状態で、diff が出す変更と、apply が書くものが対応する。

    InstanceType を変えると、diff は起動テンプレートの項目の行だけを出し、apply は
    新しいバージョンを作って既定にし、古いバージョンを消す。それ以外には書かない。
    """
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    apply.run(service, yes=True)
    _ = write_conf(service, RESIZED)
    # diff。テンプレートは前の InstanceType のまま
    in_place(aws_stubs)
    _ = capsys.readouterr()

    apply.diff(service)

    out = capsys.readouterr().out
    assert out == f"~   launch-template   {TEMPLATE}   InstanceType: t3.micro -> t3.small\n"
    # apply。同じ応答に、テンプレートの書き込みだけを積む
    in_place(aws_stubs)
    new_version(aws_stubs, template_data(InstanceType="t3.small"))

    apply.run(service, yes=True)

    aws_stubs.done()
    shown = capsys.readouterr().out
    assert f"launch template {TEMPLATE}: updated (version 4)" in shown
    assert "wrote" not in shown


def test_diff_writes_what_it_found_before_it_stops(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """apply が止まるところでは、それまでに検出した変更を出してから、同じエラーで終わる。

    既定の鍵の秘密鍵しか無ければ、手順 6 で止まる。
    """
    _ = write_conf(service, edited('InstanceType = "t3.micro"', 'InstanceType = "t3.small"'))
    keys_dir().mkdir(parents=True)
    keys.default_path().write_text("a private key\n", encoding="utf-8")
    in_place(aws_stubs)

    with pytest.raises(TemtError, match=f"^{KEY}.pub does not exist; "):
        apply.diff(service)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert details(out, "launch-template") == ["~ InstanceType: t3.micro -> t3.small"]


def no_default_vpc(stubs: Stubs) -> str:
    """手順 2 で SubnetId を選ぼうとして、既定 VPC が無い。設定ファイルの中身を返す。"""
    stubs.ec2.add_response("describe_vpcs", {"Vpcs": []}, DEFAULT_VPC)
    return edited(f'SubnetId = "{SUBNET}"\n', "")


def no_subnet(stubs: Stubs) -> str:
    """手順 2 で IPv6 を見ようとして、SubnetId のサブネットが無い。"""
    stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": []},
        {"Filters": [{"Name": "subnet-id", "Values": [SUBNET]}]},
    )
    return WRITTEN


@pytest.mark.parametrize(
    ("stop", "message"),
    [
        pytest.param(no_default_vpc, f"^no default VPC in {REGION}; ", id="no default VPC"),
        pytest.param(no_subnet, f"^no such subnet: {SUBNET}; ", id="no subnet"),
    ],
)
def test_diff_stops_in_step_2_without_writing_anything(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    stop: Any,
    message: str,
) -> None:
    """手順 2 で書かれていない値を決められなければ、何も出さずに止まる。設定ファイルは
    変えない。"""
    text = write_conf(service, stop(aws_stubs))

    with pytest.raises(TemtError, match=message):
        apply.diff(service)

    aws_stubs.done()
    assert capsys.readouterr().out == ""
    assert service.conf_path.read_text(encoding="utf-8") == text


def test_a_file_in_place_of_the_command_is_left_alone(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """サービスのコマンドの場所に何かあれば、中身にかかわらず差分に挙げず、書かない。"""
    _ = write_conf(service)
    service.command_path.parent.mkdir(parents=True)
    service.command_path.write_text("mine\n", encoding="utf-8")
    make_key()
    in_place(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert "command" not in capsys.readouterr().out
    assert service.command_path.read_text(encoding="utf-8") == "mine\n"


class Terminal(io.StringIO):
    """端末につながった標準入力のふり。"""

    def isatty(self) -> bool:
        return True


def test_apply_shows_the_changes_and_writes_nothing_when_declined(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """変えるものを diff と同じ形で標準出力に見せてから、標準エラー出力で確認を求める。
    y と答えなければ、設定ファイルにも AWS にも手元にも書かない。"""
    identified(aws_stubs)
    before = write_conf(service)
    make_key()
    in_place(aws_stubs)
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))

    with pytest.raises(TemtError, match="cancelled"):
        apply.run(service, yes=False)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert f"+   command       ~/.local/bin/{NAME}" in captured.out
    assert "[y/N]" not in captured.out
    assert captured.err == f"apply these changes {WHERE}? [y/N] "
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert not service.command_path.exists()
    assert not (ssh_dir() / "config").exists()


def test_apply_needs_yes_without_a_terminal(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = write_conf(service)
    make_key()
    in_place(aws_stubs)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

    with pytest.raises(TemtError, match="pass --yes"):
        apply.run(service, yes=False)

    aws_stubs.done()
    assert not service.command_path.exists()
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert not (ssh_dir() / "config").exists()


def test_apply_asks_nothing_without_differences(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """違いが無ければ、確認を求めずに終わる。--yes が無く、端末でなくても止まらない。"""
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    in_place(aws_stubs)
    apply.run(service, yes=True)
    _ = capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    apply.run(service, yes=False)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert f"{NAME}: no differences" in captured.out
    assert captured.err == ""


def test_a_first_apply_that_is_declined_makes_nothing(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定ファイルが無い初回の apply でも、y と答えなければ、設定ファイルも鍵も
    ディレクトリもサービスのコマンドも作らない。"""
    # サービスを作るかと、変更を反映するかの、2 度の確認に添える
    identified(aws_stubs)
    identified(aws_stubs)
    chosen(aws_stubs)
    built(aws_stubs, group=None, read_image=False)
    validated(aws_stubs, EC2_POLICY)
    nothing_there(aws_stubs, template=False)
    monkeypatch.setattr(sys, "stdin", Terminal("y\n\n"))

    with pytest.raises(TemtError, match="cancelled"):
        apply.run(service, yes=False)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err == (
        f"no config file: {tilde(service.conf_path)}; create service {NAME} {WHERE}? [y/N] "
        f"apply these changes {WHERE}? [y/N] "
    )
    out = captured.out
    assert "+   conf " in out
    assert ["+", "key", KEY] in heads(out)
    assert not service.conf_path.exists()
    assert not keys_dir().exists()
    assert not ssh_dir().exists()
    assert not service.command_path.exists()
    assert not (Path.home() / ".ssh").exists()


def test_a_first_apply_asks_before_reading_aws(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定ファイルが無ければ、AWS を読む前に、サービスを作るかを聞く。y と答えなければ、
    何も表示せず、何も作らない。"""
    identified(aws_stubs)
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))

    with pytest.raises(TemtError, match="cancelled"):
        apply.run(service, yes=False)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"no config file: {tilde(service.conf_path)}; create service {NAME} {WHERE}? [y/N] "
    )
    assert not service.conf_path.exists()
    assert not ssh_dir().exists()
    assert not service.command_path.exists()


def test_a_first_apply_needs_yes_without_a_terminal(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """設定ファイルが無く、標準入力が端末でなければ、--yes が無い限り AWS を読まずに止まる。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

    with pytest.raises(TemtError, match="pass --yes"):
        apply.run(service, yes=False)

    aws_stubs.done()
    assert not service.conf_path.exists()


def test_apply_goes_on_when_the_answer_is_yes(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端末で y と答えれば、検出した差分で更新する。"""
    identified(aws_stubs)
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    monkeypatch.setattr(sys, "stdin", Terminal("y\n"))

    apply.run(service, yes=False)

    aws_stubs.done()
    assert service.command_path.exists()
    assert f"wrote ~/.local/bin/{NAME}" in capsys.readouterr().out


def test_half_a_key_stops_apply_before_showing_anything(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """鍵の片方だけがあれば検出で止まるので、ほかに違いがあっても表示も確認もせず、
    何も書かない。"""
    before = write_conf(service)
    public = keys.public_path(keys.default_path())
    public.parent.mkdir(parents=True)
    public.write_text("ssh-ed25519 AAAA temt\n", encoding="utf-8")
    in_place(aws_stubs)

    with pytest.raises(TemtError, match=f"^{KEY}.pub exists without {KEY}; remove it"):
        apply.run(service, yes=False)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert "command" not in captured.out
    assert "[y/N]" not in captured.err
    assert not service.command_path.exists()
    assert not (ssh_dir() / "config").exists()
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert public.read_text(encoding="utf-8") == "ssh-ed25519 AAAA temt\n"


def test_a_removed_default_key_is_made_again(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host ブロックは変わらなくても、それが指す既定の鍵が無ければ、作る。"""
    identified(aws_stubs)
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    apply.run(service, yes=True)
    block = (ssh_dir() / "config").read_text(encoding="utf-8")
    old = keys.public_path(keys.default_path()).read_text(encoding="utf-8")
    keys.default_path().unlink()
    keys.public_path(keys.default_path()).unlink()
    in_place(aws_stubs)
    in_place(aws_stubs)
    _ = capsys.readouterr()

    apply.diff(service)

    assert heads(capsys.readouterr().out) == [["+", "key", KEY]]
    assert not keys.default_path().exists()
    monkeypatch.setattr(sys, "stdin", Terminal("y\n"))

    apply.run(service, yes=False)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert f"created the key {KEY}" in out
    assert "wrote the Host" not in out
    assert keys.public_path(keys.default_path()).read_text(encoding="utf-8") != old
    assert (ssh_dir() / "config").read_text(encoding="utf-8") == block


def test_a_key_the_user_chose_is_neither_made_nor_checked(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host ブロックの IdentityFile を別の鍵に替えれば、apply はその鍵を見ず、既定の鍵も
    作らない。ssh-keygen も要らない。IdentityFile の行は書かれたまま残す。"""
    _ = write_conf(service)
    make_key()
    in_place(aws_stubs)
    apply.run(service, yes=True)
    config = ssh_dir() / "config"
    mine = "    IdentityFile ~/.ssh/no-such-key\n"
    config.write_text(
        config.read_text(encoding="utf-8").replace(f"    IdentityFile       {KEY}\n", mine),
        encoding="utf-8",
    )
    edited_config = config.read_text(encoding="utf-8")
    keys.default_path().unlink()
    keys.public_path(keys.default_path()).unlink()
    monkeypatch.setenv("PATH", "")
    in_place(aws_stubs)
    in_place(aws_stubs)
    _ = capsys.readouterr()

    apply.diff(service)
    apply.run(service, yes=True)

    aws_stubs.done()
    captured = capsys.readouterr()
    # diff は違いが無いことを標準エラー出力に、apply は標準出力に知らせる
    assert captured.err.endswith(f"{NAME}: no differences\n")
    assert captured.out.endswith(f"{NAME}: no differences\n")
    assert len(captured.out.splitlines()) == 1
    assert not keys.default_path().exists()
    assert not keys.public_path(keys.default_path()).exists()
    assert config.read_text(encoding="utf-8") == edited_config


class Editing(Terminal):
    """y と答える前に、利用者が ssh の設定を手で書き足す端末。"""

    def readline(self, size: int = -1, /) -> str:
        with (ssh_dir() / "config").open("a", encoding="utf-8") as file:
            _ = file.write("\nHost mine\n    User root\n")
        return super().readline(size)


def test_an_ssh_config_edited_while_asking_is_not_overwritten(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """確認を待つ間に ssh の設定が書き換えられれば、手順 6 で書かずに止まる。

    手で書いたものを、検出したときの中身で上書きしないためである。それより前の手順は
    更新し終えている。
    """
    identified(aws_stubs)
    _ = write_conf(service, RESIZED)
    home.prepare_dirs()
    (ssh_dir() / "config").write_text("# mine\n", encoding="utf-8")
    make_key()
    in_place(aws_stubs)
    new_version(aws_stubs, template_data(InstanceType="t3.small"))
    monkeypatch.setattr(sys, "stdin", Editing("y\n"))

    with pytest.raises(TemtError, match="~/.ssh/temt/config changed after temt read it"):
        apply.run(service, yes=False)

    aws_stubs.done()
    assert (ssh_dir() / "config").read_text(encoding="utf-8") == (
        "# mine\n\nHost mine\n    User root\n"
    )


# --- 使う AMI ---

# ImageId を書いていない設定ファイル。AMI は起動テンプレートが持つ
UNPINNED = CONFIGURED + conf.EC2_DEFAULT
# 起動テンプレートを替える先の AMI
OTHER_IMAGE = "ami-0fedcba9876543210"
OTHER_NAME = "temt-dev-main-2609100900"
OTHER = {**an_image(), "ImageId": OTHER_IMAGE, "Name": OTHER_NAME}
OTHER_BY_NAME = {"Owners": ["self"], "Filters": [{"Name": "name", "Values": [OTHER_NAME]}]}


def unpinned_in_place(stubs: Stubs, data: dict[str, Any], image: dict[str, Any]) -> None:
    """UNPINNED の設定ファイルで、AWS のものが揃っているときに読むもの。手順 4 で起動
    テンプレート（既定バージョンの中身 data）を読み、その AMI image を ID で引く。
    """
    subnet(stubs)
    template_versions(stubs, data)
    stubs.ec2.add_response("describe_images", {"Images": [image]}, {"ImageIds": [data["ImageId"]]})
    built(stubs, read_image=False)
    validated(stubs, EC2_POLICY)
    iam_in_place(stubs)
    group_in_place(stubs)
    template_tags(stubs)


def test_apply_keeps_the_image_of_the_template(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ImageId を書いていなければ、bake などが替えた起動テンプレートの AMI をそのまま使い、
    違いにしない。
    """
    _ = write_conf(service, UNPINNED)
    set_up_here(service)
    unpinned_in_place(aws_stubs, template_data(ImageId=OTHER_IMAGE), OTHER)

    apply.diff(service)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith(f"{NAME}: no differences\n")


def switched(stubs: Stubs) -> None:
    """--image-id OTHER_NAME で、起動テンプレートの AMI を IMAGE から替えるときに読むもの。

    --image-id の AMI を名前で引くので、手順 4 では起動テンプレートを読まない。
    """
    subnet(stubs)
    stubs.ec2.add_response("describe_images", {"Images": [OTHER]}, OTHER_BY_NAME)
    built(stubs, read_image=False)
    validated(stubs, EC2_POLICY)
    iam_in_place(stubs)
    group_in_place(stubs)
    template_in_place(stubs, template_data())


def test_image_id_switches_the_image_of_the_template(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """--image-id を付ければ、起動テンプレートがあっても、その AMI に替える。diff は、同じ
    --image-id の apply が変えるものを、AMI の名前を添えて出す。
    """
    _ = write_conf(service, UNPINNED)
    set_up_here(service)
    switched(aws_stubs)
    switched(aws_stubs)
    new_version(aws_stubs, template_data(ImageId=OTHER_IMAGE))

    apply.diff(service, image=OTHER_NAME)
    assert capsys.readouterr().out == (
        f"~   launch-template   {TEMPLATE}   ImageId: {IMAGE} -> {OTHER_IMAGE} ({OTHER_NAME})\n"
    )
    apply.run(service, image=OTHER_NAME, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == UNPINNED


def test_image_id_chooses_the_image_of_a_new_template(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """起動テンプレートを作るときも、--image-id の AMI を使う。ID でも引ける。"""
    _ = write_conf(service, UNPINNED)
    set_up_here(service)
    subnet(aws_stubs)
    aws_stubs.ec2.add_response("describe_images", {"Images": [OTHER]}, {"ImageIds": [OTHER_IMAGE]})
    built(aws_stubs, group=None, read_image=False)
    validated(aws_stubs, EC2_POLICY)
    nothing_there(aws_stubs)

    apply.diff(service, image=OTHER_IMAGE)

    aws_stubs.done()
    assert details(capsys.readouterr().out, "launch-template") == [
        f"+ ImageId: {OTHER_IMAGE} ({OTHER_NAME})"
    ]


@pytest.mark.parametrize(
    ("common", "where"),
    [
        pytest.param(False, CONF_SHOWN, id="config file"),
        pytest.param(True, DEFAULTS_SHOWN, id="common"),
    ],
)
def test_image_id_is_a_usage_error_while_image_id_is_written(
    aws_stubs: Stubs, service: Service, common: bool, where: str
) -> None:
    """ImageId を書けば AMI は固定なので、--image-id は AWS を呼ぶ前に引数の誤りにする。"""
    if common:
        write_defaults(f'[launch-template]\nImageId = "{IMAGE}"\n')
        _ = write_conf(service, UNPINNED)
    else:
        _ = write_conf(service)

    with pytest.raises(UsageError) as caught:
        apply.run(service, image=OTHER_NAME, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"--image-id is not allowed; launch-template.ImageId in {where} fixes the AMI"
    )


def test_image_id_is_a_usage_error_before_asking_to_create(
    aws_stubs: Stubs, service: Service
) -> None:
    """設定ファイルが無く共通ファイルに ImageId があれば、--image-id は作るかを聞く前（アカウントを
    引く前）に引数の誤りにする。
    """
    write_defaults(f'[launch-template]\nImageId = "{IMAGE}"\n')

    with pytest.raises(UsageError) as caught:
        apply.run(service, image=OTHER_NAME, yes=False)

    aws_stubs.done()
    assert str(caught.value) == (
        f"--image-id is not allowed; launch-template.ImageId in {DEFAULTS_SHOWN} fixes the AMI"
    )
    assert not service.conf_path.exists()


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        pytest.param({"Images": []}, f"no such AMI: {OTHER_NAME}", id="missing"),
        pytest.param(
            {"Images": [{**OTHER, "State": "failed"}]},
            f"the AMI {OTHER_NAME} is failed",
            id="failed",
        ),
    ],
)
def test_an_unusable_image_id_stops(
    aws_stubs: Stubs, service: Service, answer: dict[str, Any], message: str
) -> None:
    _ = write_conf(service, UNPINNED)
    subnet(aws_stubs)
    aws_stubs.ec2.add_response("describe_images", answer, OTHER_BY_NAME)

    with pytest.raises(TemtError) as caught:
        apply.diff(service, image=OTHER_NAME)

    aws_stubs.done()
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        pytest.param({"Images": []}, "is not found", id="missing"),
        pytest.param(
            {"Images": [{**an_image(), "State": "deregistered"}]},
            "is not found",
            id="deregistered",
        ),
        pytest.param({"Images": [{**an_image(), "State": "failed"}]}, "is failed", id="failed"),
        pytest.param({"Images": [{**an_image(), "State": "pending"}]}, "is pending", id="pending"),
    ],
)
def test_apply_stops_when_the_image_of_the_template_is_lost(
    aws_stubs: Stubs, service: Service, answer: dict[str, Any], why: str
) -> None:
    """ImageId を書いていないのに、起動テンプレートの AMI が無いか available でなければ、
    黙って選び直さず、--image-id で選ぶよう告げて止まる。
    """
    _ = write_conf(service, UNPINNED)
    subnet(aws_stubs)
    template_versions(aws_stubs, template_data())
    aws_stubs.ec2.add_response("describe_images", answer, {"ImageIds": [IMAGE]})

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the AMI {IMAGE} of the launch template {TEMPLATE} {why}; choose one with --image-id"
    )


def test_a_new_template_waits_for_a_pending_newest_image(
    aws_stubs: Stubs, service: Service
) -> None:
    """起動テンプレートを作るとき、サービスの最新の AMI が pending（コピーの途中など）なら
    止まる。古い AMI に戻らない。
    """
    _ = write_conf(service, UNPINNED)
    subnet(aws_stubs)
    no_template(aws_stubs)
    own_image(aws_stubs, state="pending", older=True)

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the newest AMI of {NAME}, {IMAGE_NAME} ({IMAGE}), is pending; run again when it is "
        "available"
    )
