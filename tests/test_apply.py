"""apply の本体のテスト。ssh-keygen は実際に呼び、AWS は Stubber で差し替える。

クライアントの差し替えは conftest の aws_stubs が行う。ホームは一時ディレクトリに
向く。
"""

import io
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from conftest import (
    ASSOCIATION,
    ASSOCIATION_MARK,
    SSHD,
    Stubs,
    association_content,
    association_in_place,
    association_started,
    described_association,
    listed_associations,
)
from types_boto3_ec2.client import EC2Client

from temt import apply, aws, conf, home, keys, policy, resources
from temt.conf import Conf, LaunchTemplate, Role
from temt.msg import TemtError
from temt.names import CONF_DIR_ENV, SSH_DIR_ENV, Service, keys_dir, ssh_dir

REGION = "ap-northeast-1"
NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
VPC = "vpc-0123456789abcdef0"
SUBNET = "subnet-0123456789abcdef0"
GROUP = "sg-0123456789abcdef0"
IMAGE = "ami-0123456789abcdef0"
IMAGE_NAME = "temt-dev-main-2609200900"
# 既定の鍵の、diff での表示
KEY = "~/.ssh/temt/keys/temt"
WHEN = datetime(2026, 9, 20, tzinfo=UTC)

# apply が書く 4 項目が書かれている状態。キーは update に渡す位置で、並びは書く順。
# AwsProfile は --profile のときだけ書かれる
WRITTEN = {
    "AwsRegion": REGION,
    "launch-template.ImageId": IMAGE_NAME,
    "launch-template.InstanceType": "t3.micro",
    "launch-template.SubnetId": SUBNET,
}

DEFAULT_VPC = {"Filters": [{"Name": "is-default", "Values": ["true"]}]}
PUBLIC_SUBNETS = {
    "Filters": [
        {"Name": "vpc-id", "Values": [VPC]},
        {"Name": "map-public-ip-on-launch", "Values": ["true"]},
    ]
}
THE_SUBNET = {"Filters": [{"Name": "subnet-id", "Values": [SUBNET]}]}
SERVICE_IMAGES = {
    "Owners": ["self"],
    "Filters": [
        {"Name": "tag:temt:service", "Values": ["dev-main"]},
        {"Name": "state", "Values": ["available"]},
    ],
}
THE_IMAGE = {"Owners": ["self"], "Filters": [{"Name": "name", "Values": [IMAGE_NAME]}]}
# 手順 4 で引いた AMI
FOUND_IMAGE = {"Images": [{"ImageId": IMAGE, "Name": IMAGE_NAME, "State": "available"}]}
THE_GROUP = {"Filters": [{"Name": "group-name", "Values": [NAME]}]}
# temt が管理していることを示すタグ
MARK = [{"Key": "temt:service", "Value": "dev-main"}]
GROUP_TAGS = [{"Key": "Name", "Value": NAME}, *MARK]
THE_TEMPLATE = {"LaunchTemplateName": TEMPLATE}


# ProxyCommand の先頭の env。既定の場所を ~ で始まる形で書く
PLACES = f"env {CONF_DIR_ENV}='~/.config/temt' {SSH_DIR_ENV}='~/.ssh/temt'"


def conf_text(values: dict[str, str]) -> str:
    """位置と値の対応を、tomlkit が書くのと同じ形の設定ファイルの中身にする。

    最上位の項目を先に、テーブルはその後に、空行を挟んで置く。
    """
    top = "".join(f'{key} = "{value}"\n' for key, value in values.items() if "." not in key)
    tables: dict[str, str] = {}
    for key, value in values.items():
        if "." in key:
            table, name = key.split(".")
            tables[table] = tables.get(table, "") + f'{name} = "{value}"\n'
    return top + "".join(f"\n[{table}]\n{body}" for table, body in tables.items())


def to_conf(values: dict[str, str]) -> Conf:
    """位置と値の対応を映した Conf。[[role.policy.ec2]] は既定とする。

    知らないテーブルがあれば失敗する。
    """
    top: dict[str, Any] = {key: value for key, value in values.items() if "." not in key}
    template: dict[str, Any] = {}
    for key, value in values.items():
        if "." in key:
            table, name = key.split(".")
            assert table == conf.LAUNCH_TEMPLATE, key
            template[name] = value
    return Conf(
        **top,
        launch_template=LaunchTemplate(**template),
        role=Role(policy={conf.EC2: conf.ec2_default()}),
    )


def write_conf(service: Service, **changes: str) -> dict[str, str]:
    """4 項目と [[role.policy.ec2]] の既定が書かれた設定ファイルを置き、項目を返す。

    changes は最上位の項目。
    """
    values = {**WRITTEN, **changes}
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(conf_text(values) + conf.EC2_DEFAULT, encoding="utf-8")
    return values


def make_key() -> None:
    """既定の鍵を、前の apply が作ったものとして置く。"""
    keys.generate(keys.default_path(), keys.DEFAULT_NAME)


def a_role() -> dict[str, Any]:
    """get-role と create-role が返す形。temt のタグが付いている。"""
    return {
        "Role": {
            "Path": "/",
            "RoleName": NAME,
            "RoleId": "AROAEXAMPLE000000",
            "Arn": f"arn:aws:iam::123456789012:role/{NAME}",
            "CreateDate": WHEN,
            "Tags": MARK,
        }
    }


def a_profile(roles: list[str]) -> dict[str, Any]:
    """get-instance-profile と create-instance-profile が返す形。temt のタグが付いている。"""
    return {
        "InstanceProfile": {
            "Path": "/",
            "InstanceProfileName": NAME,
            "InstanceProfileId": "AIPAEXAMPLE000000",
            "Arn": f"arn:aws:iam::123456789012:instance-profile/{NAME}",
            "CreateDate": WHEN,
            "Tags": MARK,
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


def missing(stub: Stubber, method: str, code: str, **params: Any) -> None:
    """そのリソースが無いときの誤りを積む。IAM の waiter に合わせて 404 で揃える。"""
    stub.add_client_error(
        method, service_error_code=code, http_status_code=404, expected_params=params or None
    )


# [[role.policy.jupyter]] から作るインラインポリシーの名前
JUPYTER = policy.policy_name("jupyter")
# [[role.policy.ec2]] の既定から作るインラインポリシー temt-ec2 の名前と中身
EC2 = policy.policy_name(conf.EC2)
EC2_POLICY = policy.document(conf.ec2_default(), policy.variables(Service("dev-main")), "")


def inline_policies(stubs: Stubs, *names: str) -> None:
    """手順 5 のインラインポリシーの一覧。ロールには names が付いている。"""
    stubs.iam.add_response("list_role_policies", {"PolicyNames": list(names)}, {"RoleName": NAME})


def validated(stubs: Stubs, content: dict[str, Any], *findings: dict[str, Any]) -> None:
    """手順 4 の ValidatePolicy。content を検査し、findings を返す。"""
    stubs.analyzer.add_response(
        "validate_policy",
        {"findings": list(findings)},
        {"policyDocument": json.dumps(content), "policyType": "IDENTITY_POLICY", "locale": "EN"},
    )


def ec2_policy(stubs: Stubs, *, new: bool = False) -> None:
    """手順 5 のインラインポリシー temt-ec2 の読み取り。new なら無く、でなければ揃っている。

    無いときに付けるのは、一覧を読んだあとの ec2_policy_put である。
    """
    if new:
        missing(
            stubs.iam, "get_role_policy", resources.NO_SUCH_ENTITY, RoleName=NAME, PolicyName=EC2
        )
        return
    # 中身は URL エンコードした JSON で返る
    stubs.iam.add_response(
        "get_role_policy",
        {"RoleName": NAME, "PolicyName": EC2, "PolicyDocument": quote(json.dumps(EC2_POLICY))},
        {"RoleName": NAME, "PolicyName": EC2},
    )


def ec2_policy_put(stubs: Stubs) -> None:
    """手順 5 で、インラインポリシー temt-ec2 を付ける。"""
    stubs.iam.add_response(
        "put_role_policy",
        {},
        {"RoleName": NAME, "PolicyName": EC2, "PolicyDocument": json.dumps(EC2_POLICY)},
    )


def template_data(values: dict[str, str], *, ipv6: bool = False) -> dict[str, Any]:
    """設定ファイルのとおりの起動テンプレートの中身。"""
    return dict(
        resources.template_data(
            Service("dev-main"), to_conf(values), IMAGE, SUBNET, GROUP, ipv6=ipv6
        )
    )


def step2_ami(stubs: Stubs) -> None:
    """手順 2 の AMI の選択。サービスの AMI が 1 つあり、それを選ぶ。"""
    stubs.ec2.add_response(
        "describe_images",
        {
            "Images": [
                {
                    "ImageId": IMAGE,
                    "Name": IMAGE_NAME,
                    "CreationDate": "2026-09-20T00:00:00.000Z",
                }
            ]
        },
        SERVICE_IMAGES,
    )


def step4(
    stubs: Stubs,
    *,
    ipv6: bool = False,
    image: str = IMAGE_NAME,
    found_image: dict[str, Any] = FOUND_IMAGE,
) -> None:
    """手順 4 の読み取り。設定ファイルのサブネットの VPC と IPv6 の CIDR の有無と、
    設定ファイルの ImageId に書かれた名前の AMI（found_image）。
    """
    found: dict[str, Any] = {"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "10.0.0.0/24"}
    if ipv6:
        found["Ipv6CidrBlockAssociationSet"] = [
            {"Ipv6CidrBlock": "2001:db8::/64", "Ipv6CidrBlockState": {"State": "associated"}}
        ]
    stubs.ec2.add_response("describe_subnets", {"Subnets": [found]}, THE_SUBNET)
    params = {"ImageIds": [IMAGE]} if image == IMAGE else THE_IMAGE
    stubs.ec2.add_response("describe_images", found_image, params)
    validated(stubs, EC2_POLICY)


def template_tags(stubs: Stubs) -> None:
    """手順 5 の、起動テンプレート自身のタグ。temt のタグが付いている。"""
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


def step5(
    stubs: Stubs,
    values: dict[str, str],
    role_policy: dict[str, Any] | None = None,
    *,
    sshd_ports: list[int] | None = None,
    new_ec2: bool = False,
    associated: bool = True,
    data: dict[str, Any] | None = None,
) -> None:
    """手順 5。5 つとも、あって設定ファイルに合っているときに読むものを積む。

    temt-ec2 は付いていて既定と同じ。new_ec2 なら、まだ付いていないので付ける。
    role_policy は [[role.policy.jupyter]] から作った temt-jupyter の中身。ロールには
    まだ付いていないので、あれば付ける。付けるのは、IAM のものをすべて読んだあとで
    ある。sshd_ports を渡せば、関連付けはそのポートの中身で読むところまでを積み、揃える
    のは呼ぶ側に任せる。associated でなければ、関連付けは名前で探しても無い。作るのは
    呼ぶ側に任せる（association_created）。data を渡せば、起動テンプレートの既定
    バージョンはその中身で、揃えるのは呼ぶ側に任せる。
    """
    stubs.iam.add_response("get_role", a_role(), {"RoleName": NAME})
    ec2_policy(stubs, new=new_ec2)
    if role_policy is not None:
        missing(
            stubs.iam,
            "get_role_policy",
            resources.NO_SUCH_ENTITY,
            RoleName=NAME,
            PolicyName=JUPYTER,
        )
    inline_policies(stubs, *([] if new_ec2 else [EC2]))
    stubs.iam.add_response("get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME})
    if new_ec2:
        ec2_policy_put(stubs)
    if role_policy is not None:
        stubs.iam.add_response(
            "put_role_policy",
            {},
            {
                "RoleName": NAME,
                "PolicyName": JUPYTER,
                "PolicyDocument": json.dumps(role_policy),
            },
        )
    stubs.ec2.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                {"GroupId": GROUP, "GroupName": NAME, "VpcId": VPC, "Tags": GROUP_TAGS}
            ]
        },
        THE_GROUP,
    )
    stubs.ec2.add_response(
        "describe_security_group_rules",
        {"SecurityGroupRules": []},
        {"Filters": [{"Name": "group-id", "Values": [GROUP]}]},
    )
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {
                    "LaunchTemplateName": TEMPLATE,
                    "VersionNumber": 3,
                    "DefaultVersion": True,
                    "LaunchTemplateData": template_data(values) if data is None else data,
                }
            ]
        },
        THE_TEMPLATE,
    )
    template_tags(stubs)
    if not associated:
        listed_associations(stubs.ssm)
    elif sshd_ports is None:
        association_in_place(stubs.ssm)
    else:
        listed_associations(stubs.ssm, ASSOCIATION)
        described_association(stubs.ssm, sshd_ports)


def association_created(stubs: Stubs, ports: list[int] | None = None) -> None:
    """関連付けを ports（既定は [22]）で作り、実行を求める。"""
    stubs.ssm.add_response(
        "create_association",
        {"AssociationDescription": {"AssociationId": ASSOCIATION, "AssociationName": SSHD}},
        {**association_content(ports or [22]), "AssociationName": SSHD, "Tags": ASSOCIATION_MARK},
    )
    association_started(stubs.ssm)


def nothing_to_do(
    stubs: Stubs,
    values: dict[str, str],
    *,
    new_ec2: bool = False,
    associated: bool = True,
) -> None:
    """AWS のものがすべて揃っていて、設定ファイルとも一致しているときの応答をすべて積む。

    new_ec2 なら、設定ファイルに [[role.policy.ec2]] が無く、この apply が書いて付ける。
    associated でなければ、関連付けが無いので、この apply が作る。
    """
    step4(stubs)
    step5(stubs, values, new_ec2=new_ec2, associated=associated)
    if not associated:
        association_created(stubs)


def test_everything_is_made_when_nothing_exists(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    # 手順 2。書かれていない ImageId と SubnetId を解決する。サービスの AMI はまだ
    # 無いので、Ubuntu の公開 AMI になり、名前ではなく ID で書かれる
    written = {**WRITTEN, conf.launch_template_key("ImageId"): IMAGE}
    aws_stubs.ec2.add_response("describe_images", {"Images": []}, SERVICE_IMAGES)
    aws_stubs.ssm.add_response(
        "get_parameter",
        {"Parameter": {"Name": aws.UBUNTU_AMI_PARAMETER, "Value": IMAGE}},
        {"Name": aws.UBUNTU_AMI_PARAMETER},
    )
    aws_stubs.ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": VPC}]}, DEFAULT_VPC)
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "AvailabilityZone": f"{REGION}a", "VpcId": VPC}]},
        PUBLIC_SUBNETS,
    )
    # 手順 4。公開 AMI は ID のまま書かれている
    step4(aws_stubs, image=IMAGE)
    # 手順 5。5 つとも無い
    missing(aws_stubs.iam, "get_role", resources.NO_SUCH_ENTITY, RoleName=NAME)
    ec2_policy(aws_stubs, new=True)
    inline_policies(aws_stubs)
    missing(
        aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME
    )
    aws_stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": []}, THE_GROUP)
    missing(
        aws_stubs.ec2,
        "describe_launch_template_versions",
        resources.NO_SUCH_TEMPLATE,
        **THE_TEMPLATE,
    )
    listed_associations(aws_stubs.ssm)
    # 更新。手順 5 で 5 つを順に作る。EC2 にキーペアは登録しない
    aws_stubs.iam.add_response(
        "create_role",
        a_role(),
        {"RoleName": NAME, "AssumeRolePolicyDocument": resources.TRUST_POLICY, "Tags": MARK},
    )
    ec2_policy_put(aws_stubs)
    aws_stubs.iam.add_response(
        "create_instance_profile", a_profile([]), {"InstanceProfileName": NAME, "Tags": MARK}
    )
    # waiter は HTTP の状態で判断するので、応答にそれを入れる
    aws_stubs.iam.add_response(
        "get_instance_profile",
        {**a_profile([]), "ResponseMetadata": {"HTTPStatusCode": 200}},
        {"InstanceProfileName": NAME},
    )
    aws_stubs.iam.add_response(
        "add_role_to_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
    )
    aws_stubs.ec2.add_response(
        "create_security_group",
        {"GroupId": GROUP},
        {
            "GroupName": NAME,
            "Description": resources.GROUP_DESCRIPTION,
            "VpcId": VPC,
            "TagSpecifications": [{"ResourceType": "security-group", "Tags": GROUP_TAGS}],
        },
    )
    aws_stubs.ec2.add_response(
        "create_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE, "DefaultVersionNumber": 1}},
        {
            "LaunchTemplateName": TEMPLATE,
            "LaunchTemplateData": template_data(written),
            "TagSpecifications": [{"ResourceType": "launch-template", "Tags": MARK}],
        },
    )
    association_created(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    # 手順 2。解決した 4 項目が、表の順で、見出しの付いた新しいファイルに書かれる
    assert (
        service.conf_path.read_text(encoding="utf-8")
        == conf.HEADER + conf_text(written) + conf.EC2_DEFAULT + conf.EXAMPLE
    )
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
    assert f"IdentityFile       {KEY}\n" in block
    assert f"ProxyCommand       {PLACES} temt proxy --svc dev-main --user '%r' %p\n" in block
    user_config = (Path.home() / ".ssh" / "config").read_text(encoding="utf-8")
    assert user_config.startswith(f"{home.MARK}\nInclude ~/.ssh/temt/config\n")
    # 手順 5 と 6 の報告
    out = capsys.readouterr().out
    for made in (
        *(f'wrote {key} = "{value}" in {service.conf_path}' for key, value in written.items()),
        f"wrote the default [[role.policy.{conf.EC2}]] in {service.conf_path}",
        f"role {NAME}: created",
        "created ~/.ssh/temt (mode 700)",
        "created ~/.ssh/temt/keys (mode 700)",
        f"role policy {EC2}: created",
        f"instance profile {NAME}: created",
        f"security group {NAME}: created ({GROUP})",
        f"launch template {TEMPLATE}: created (version 1)",
        f"association {SSHD}: created",
        f"created the key {KEY}",
        f"wrote the Host {NAME} block in ~/.ssh/temt/config",
    ):
        assert made in out
    # 既定の 22 で作ったときは、ポートは変わらないので、焼くよう添えない
    assert "take effect" not in out
    # 関連付けは起動テンプレートの後に揃える
    assert out.index(f"launch template {TEMPLATE}: created") < out.index(f"association {SSHD}:")
    # 鍵は AWS のものを揃えた後、それを指す Host ブロックを書く前に作る
    assert out.index(f"association {SSHD}:") < out.index(f"created the key {KEY}")
    assert out.index(f"created the key {KEY}") < out.index(f"wrote the Host {NAME} block")
    # 手順 6。Port は既定の 22
    assert "Port               22\n" in block


def test_a_file_written_ahead_with_only_the_table_is_completed(
    aws_stubs: Stubs, service: Service
) -> None:
    """conf.md の勧めどおりテーブルだけ先に書いたファイルを、apply が完成させる。

    最上位の項目はテーブルの前に、SubnetId はテーブルの中に足され、書かれていた
    値は残る。
    """
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(
        '# mine\n[launch-template]\nInstanceType = "t3.small"\n', encoding="utf-8"
    )
    # 手順 2。書かれていない ImageId と SubnetId を解決する
    step2_ami(aws_stubs)
    aws_stubs.ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": VPC}]}, DEFAULT_VPC)
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "AvailabilityZone": f"{REGION}a", "VpcId": VPC}]},
        PUBLIC_SUBNETS,
    )
    values = {**WRITTEN, f"{conf.LAUNCH_TEMPLATE}.InstanceType": "t3.small"}
    nothing_to_do(aws_stubs, values, new_ec2=True, associated=False)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == (
        "# mine\n"
        f'AwsRegion = "{REGION}"\n'
        "\n"
        "[launch-template]\n"
        'InstanceType = "t3.small"\n'
        f'ImageId = "{IMAGE_NAME}"\n'
        f'SubnetId = "{SUBNET}"\n' + conf.EC2_DEFAULT
    )
    assert conf.load(service) == to_conf(values)


def test_nothing_changes_when_everything_matches(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    values = write_conf(service)
    make_key()
    before = service.conf_path.read_text(encoding="utf-8")
    nothing_to_do(aws_stubs, values)

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
    assert f"association {SSHD}" not in captured.out
    assert f"wrote the Host {NAME} block" in captured.out
    assert "take effect" not in captured.out
    assert captured.err == ""


def test_new_sshd_ports_update_the_association_and_the_host_block(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """SshdPorts を変えると、関連付けが新しい版になり、Host ブロックの Port が先頭になる。

    apply は SshdPorts を設定ファイルに書かない。
    """
    values = write_conf(service)
    text = "SshdPorts = [2222, 443]\n" + service.conf_path.read_text(encoding="utf-8")
    service.conf_path.write_text(text, encoding="utf-8")
    make_key()
    step4(aws_stubs)
    step5(aws_stubs, values, sshd_ports=[22])
    # 関連付けは 22 のまま。中身を新しいポートで書き直す
    aws_stubs.ssm.add_response(
        "list_tags_for_resource",
        {"TagList": ASSOCIATION_MARK},
        {"ResourceType": "Association", "ResourceId": ASSOCIATION},
    )
    aws_stubs.ssm.add_response(
        "update_association",
        {"AssociationDescription": {"AssociationId": ASSOCIATION}},
        {
            "AssociationId": ASSOCIATION,
            "AssociationName": SSHD,
            **association_content([2222, 443]),
        },
    )
    association_started(aws_stubs.ssm)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    out = capsys.readouterr().out
    assert f"association {SSHD}: updated" in out
    assert (
        "sshd ports 2222,443 take effect on a running instance shortly; "
        "run 'temt bake --svc dev-main' to keep them in the AMI"
    ) in out
    block = (ssh_dir() / "config").read_text(encoding="utf-8")
    assert "    Port               2222\n" in block


# [[role.policy.jupyter]] を書いた設定ファイルの末尾と、それから作る temt-jupyter の中身。
# Effect は temt が補う
STATEMENT = (
    "\n[[role.policy.jupyter]]\n"
    'Action = "ssm:GetParameter"\n'
    'Resource = "arn:aws:ssm:*:*:parameter/${temt:name}/*"\n'
)
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
VALIDATE = {
    "policyDocument": json.dumps(ROLE_POLICY),
    "policyType": "IDENTITY_POLICY",
    "locale": "EN",
}


def test_a_role_policy_becomes_an_inline_policy(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """変数を置き換え、ValidatePolicy にかけてから、ロールに付ける。"""
    values = write_conf(service)
    with service.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write(STATEMENT)
    make_key()
    step4(aws_stubs)
    aws_stubs.analyzer.add_response("validate_policy", {"findings": []}, VALIDATE)
    step5(aws_stubs, values, ROLE_POLICY)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert f"role policy {JUPYTER}: created" in capsys.readouterr().out


def test_a_warning_on_the_role_statements_goes_on(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ERROR でない指摘は警告として挙げ、ロールに付けるところまで進む。"""
    values = write_conf(service)
    with service.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write(STATEMENT)
    make_key()
    step4(aws_stubs)
    aws_stubs.analyzer.add_response(
        "validate_policy",
        {
            "findings": [
                {
                    "findingDetails": "Use a narrower resource.",
                    "findingType": "SECURITY_WARNING",
                    "issueCode": "SOME_WARNING",
                    "learnMoreLink": "https://example.com/",
                    "locations": [],
                }
            ]
        },
        VALIDATE,
    )
    step5(aws_stubs, values, ROLE_POLICY)

    apply.run(service, yes=True)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err == (
        "temt: warning: temt-jupyter: SECURITY_WARNING SOME_WARNING: Use a narrower resource.\n"
    )
    assert f"role policy {JUPYTER}: created" in captured.out


def test_an_error_in_the_role_statements_stops_before_aws_changes(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """検査は手順 4 で行うので、ロールに触れずに止まる。"""
    _ = write_conf(service)
    with service.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write(STATEMENT)
    step4(aws_stubs)
    aws_stubs.analyzer.add_response(
        "validate_policy",
        {
            "findings": [
                {
                    "findingDetails": "The action ssm:GetParameter does not exist.",
                    "findingType": "ERROR",
                    "issueCode": "INVALID_ACTION",
                    "learnMoreLink": "https://example.com/",
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
            ]
        },
        VALIDATE,
    )

    with pytest.raises(TemtError, match=r"found 1 error\(s\) in the role policy temt-jupyter$"):
        apply.run(service, yes=True)

    # ロールの応答は積んでいないので、触れていれば Stubber が失敗する
    aws_stubs.done()
    assert (
        "temt: role.policy.jupyter[0].Action: ERROR INVALID_ACTION: "
        "The action ssm:GetParameter does not exist."
    ) in capsys.readouterr().err
    assert not (ssh_dir() / "config").exists()


def a_finding(kind: str, code: str) -> dict[str, Any]:
    """ValidatePolicy の指摘 1 つ。位置は持たない。"""
    return {
        "findingDetails": f"details of {code}",
        "findingType": kind,
        "issueCode": code,
        "learnMoreLink": "https://example.com/",
        "locations": [],
    }


def test_every_role_policy_is_validated_before_stopping(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ERROR が 1 つ目にあっても、2 つ目まで検査して合わせて挙げ、AWS に何も書かない。"""
    _ = write_conf(service)
    with service.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write(STATEMENT + STATEMENT.replace("jupyter", "idle"))
    step4(aws_stubs)
    aws_stubs.analyzer.add_response(
        "validate_policy", {"findings": [a_finding("ERROR", "INVALID_ACTION")]}, VALIDATE
    )
    aws_stubs.analyzer.add_response(
        "validate_policy",
        {"findings": [a_finding("ERROR", "A"), a_finding("ERROR", "B")]},
        VALIDATE,
    )

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


def test_a_second_run_leaves_the_local_files_alone(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """同じコマンドをもう一度実行しても、手元のファイルは書き換わらない。"""
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    nothing_to_do(aws_stubs, values)
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
    # 2 度目は違いが無いので、確認も求めずに終わる
    captured = capsys.readouterr()
    assert f"{NAME}: no differences" in captured.out
    assert "wrote" not in captured.out
    assert captured.err == ""


def test_a_new_vpc_gets_a_new_group_and_the_template_follows(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """SubnetId を別の VPC のサブネットに変えたときの一続き。

    apply は新しい VPC にセキュリティグループを作り、起動テンプレートをそれに向ける。
    古い VPC のものはそのまま残す。delete-security-group を積んでいないので、消していれば
    Stubber が失敗する。
    """
    other_vpc = "vpc-0fedcba9876543210"
    other_subnet = "subnet-0fedcba9876543210"
    other_group = "sg-0fedcba9876543210"
    values = write_conf(service)
    values[conf.launch_template_key("SubnetId")] = other_subnet
    text = conf_text(values) + conf.EC2_DEFAULT
    service.conf_path.write_text(text, encoding="utf-8")
    make_key()
    # 手順 4。新しいサブネットは別の VPC にある
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": other_subnet, "VpcId": other_vpc, "CidrBlock": "10.0.0.0/24"}]},
        {"Filters": [{"Name": "subnet-id", "Values": [other_subnet]}]},
    )
    aws_stubs.ec2.add_response("describe_images", FOUND_IMAGE, THE_IMAGE)
    validated(aws_stubs, EC2_POLICY)
    aws_stubs.iam.add_response("get_role", a_role(), {"RoleName": NAME})
    ec2_policy(aws_stubs)
    inline_policies(aws_stubs, EC2)
    aws_stubs.iam.add_response(
        "get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME}
    )
    # 手順 5。同じ名前のセキュリティグループは古い VPC にしか無いので、新しい VPC に作る
    aws_stubs.ec2.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                {"GroupId": GROUP, "GroupName": NAME, "VpcId": VPC, "Tags": GROUP_TAGS}
            ]
        },
        THE_GROUP,
    )
    # 起動テンプレートは、まだ古いサブネットと古いセキュリティグループを指している
    aws_stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {
                    "LaunchTemplateName": TEMPLATE,
                    "VersionNumber": 1,
                    "DefaultVersion": True,
                    "LaunchTemplateData": template_data(WRITTEN),
                }
            ]
        },
        THE_TEMPLATE,
    )
    template_tags(aws_stubs)
    association_in_place(aws_stubs.ssm)
    # 更新。新しい VPC にセキュリティグループを作り、その ID を起動テンプレートに入れる
    aws_stubs.ec2.add_response(
        "create_security_group",
        {"GroupId": other_group},
        {
            "GroupName": NAME,
            "Description": resources.GROUP_DESCRIPTION,
            "VpcId": other_vpc,
            "TagSpecifications": [{"ResourceType": "security-group", "Tags": GROUP_TAGS}],
        },
    )
    wanted = dict(
        resources.template_data(
            Service("dev-main"), to_conf(values), IMAGE, other_subnet, other_group, ipv6=False
        )
    )
    aws_stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": 2}},
        {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted},
    )
    aws_stubs.ec2.add_response(
        "modify_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "2"},
    )
    aws_stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": 1}]},
        {"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
    )

    apply.run(service, yes=True)

    aws_stubs.done()
    # 設定ファイルは揃っているので書かない
    assert service.conf_path.read_text(encoding="utf-8") == text
    out = capsys.readouterr().out
    assert f"security group {NAME}: created ({other_group})" in out
    assert f"launch template {TEMPLATE}: updated (version 2)" in out


def test_the_template_follows_the_ipv6_of_the_subnet(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """サブネットに IPv6 の CIDR が結び付いていれば、テンプレートに IPv6 アドレスを足す。"""
    values = write_conf(service)
    make_key()
    step4(aws_stubs, ipv6=True)
    # 手順 5。テンプレートはまだ IPv6 アドレスを付けていないので、新しいバージョンになる
    step5(aws_stubs, values)
    wanted = template_data(values, ipv6=True)
    assert wanted["NetworkInterfaces"][0]["Ipv6AddressCount"] == 1
    aws_stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": 4}},
        {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted},
    )
    aws_stubs.ec2.add_response(
        "modify_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "4"},
    )
    aws_stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": 3}]},
        {"LaunchTemplateName": TEMPLATE, "Versions": ["3"]},
    )

    apply.run(service, yes=True)

    aws_stubs.done()
    assert f"launch template {TEMPLATE}: updated (version 4)" in capsys.readouterr().out


def test_the_example_is_written_once(aws_stubs: Stubs, service: Service) -> None:
    """例を足すのは、apply がファイルを作ったときだけである。2 回目は足さない。"""
    step2_ami(aws_stubs)
    aws_stubs.ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": VPC}]}, DEFAULT_VPC)
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "AvailabilityZone": f"{REGION}a", "VpcId": VPC}]},
        PUBLIC_SUBNETS,
    )
    make_key()
    nothing_to_do(aws_stubs, WRITTEN, associated=False)
    nothing_to_do(aws_stubs, WRITTEN)

    apply.run(service, yes=True)
    apply.run(service, yes=True)

    aws_stubs.done()
    written = service.conf_path.read_text(encoding="utf-8")
    assert written == conf.HEADER + conf_text(WRITTEN) + conf.EC2_DEFAULT + conf.EXAMPLE


def test_nothing_is_written_when_detecting_stops(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """検出の途中で止まれば、確認の前なので、設定ファイルも手元のファイルも書かない。

    手順 2 が SubnetId を解けずに止まると、--profile の AwsProfile も書かれない。
    """
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    step2_ami(aws_stubs)
    aws_stubs.ec2.add_response("describe_vpcs", {"Vpcs": []}, DEFAULT_VPC)

    with pytest.raises(TemtError, match="no default VPC"):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not service.conf_path.exists()
    assert not service.command_path.exists()


def test_it_stops_before_aws_when_the_ec2_default_cannot_be_added(
    aws_stubs: Stubs, service: Service
) -> None:
    """手順 2 で [[role.policy.ec2]] を足せなければ、AWS には何も作らず、例も足さない。"""
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    text = "role = { policy = {} }\n" + conf_text(WRITTEN)
    service.conf_path.write_text(text, encoding="utf-8")

    with pytest.raises(TemtError, match=r"cannot add the default \[\[role\.policy\.ec2\]\] to "):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text
    assert not service.command_path.exists()


def test_it_stops_before_writing_to_aws_when_the_subnet_is_gone(
    aws_stubs: Stubs, service: Service
) -> None:
    """手順 4 で止まれば、検出の途中なので、AWS にも手元にも何も作らない。"""
    _ = write_conf(service)
    aws_stubs.ec2.add_response("describe_subnets", {"Subnets": []}, THE_SUBNET)

    with pytest.raises(TemtError, match=f"no such subnet: {SUBNET}"):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not service.command_path.exists()
    assert not keys.default_path().exists()
    assert not (ssh_dir() / "config").exists()


def test_it_stops_before_writing_to_aws_when_the_image_is_gone(
    aws_stubs: Stubs, service: Service
) -> None:
    """ImageId の名前の AMI が無ければ、行を消すよう案内して止まる。"""
    _ = write_conf(service)
    subnet = {"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "10.0.0.0/24"}
    aws_stubs.ec2.add_response("describe_subnets", {"Subnets": [subnet]}, THE_SUBNET)
    aws_stubs.ec2.add_response("describe_images", {"Images": []}, THE_IMAGE)

    with pytest.raises(
        TemtError,
        match=f"no such AMI: {IMAGE_NAME}; remove launch-template.ImageId from ",
    ):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not keys.default_path().exists()


ROOT_DEVICE = "/dev/sda1"


def sized_image(size: int) -> dict[str, Any]:
    """手順 4 で引く、ルートボリュームが size GiB の AMI。"""
    return {
        "Images": [
            {
                **FOUND_IMAGE["Images"][0],
                "RootDeviceName": ROOT_DEVICE,
                "BlockDeviceMappings": [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": size}}],
            }
        ]
    }


def write_volume_size(service: Service, size: int) -> None:
    """設定ファイルの [launch-template] に VolumeSize を書く。"""
    text = service.conf_path.read_text(encoding="utf-8")
    service.conf_path.write_text(
        text.replace("[launch-template]\n", f"[launch-template]\nVolumeSize = {size}\n"),
        encoding="utf-8",
    )


def test_it_stops_before_writing_to_aws_when_the_volume_size_is_too_small(
    aws_stubs: Stubs, service: Service
) -> None:
    """AMI より小さい VolumeSize は、AWS が起動するときに断るので、その前に止まる。"""
    _ = write_conf(service)
    write_volume_size(service, 6)
    subnet = {"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "10.0.0.0/24"}
    aws_stubs.ec2.add_response("describe_subnets", {"Subnets": [subnet]}, THE_SUBNET)
    aws_stubs.ec2.add_response("describe_images", sized_image(8), THE_IMAGE)

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"launch-template.VolumeSize 6 is smaller than the root volume of {IMAGE} (8 GiB); "
        f"raise it or remove it in {service.conf_path}"
    )
    assert not keys.default_path().exists()


def test_it_stops_when_the_image_has_no_ebs_root_volume(aws_stubs: Stubs, service: Service) -> None:
    _ = write_conf(service)
    write_volume_size(service, 30)
    subnet = {"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "10.0.0.0/24"}
    aws_stubs.ec2.add_response("describe_subnets", {"Subnets": [subnet]}, THE_SUBNET)
    aws_stubs.ec2.add_response("describe_images", FOUND_IMAGE, THE_IMAGE)

    with pytest.raises(TemtError) as caught:
        apply.run(service, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the AMI {IMAGE} has no EBS root volume; remove launch-template.VolumeSize from "
        f"{service.conf_path}"
    )


@pytest.mark.parametrize(
    ("size", "device"),
    [
        pytest.param(None, None, id="not written"),
        pytest.param(8, ROOT_DEVICE, id="the size of the image"),
        pytest.param(30, ROOT_DEVICE, id="larger"),
    ],
)
def test_it_resolves_the_root_device_for_the_volume_size(
    ec2: EC2Client, service: Service, size: int | None, device: str | None
) -> None:
    """VolumeSize が書かれていれば、AMI のルートデバイスを起動テンプレートのために得る。"""
    values = to_conf(WRITTEN)
    values.launch_template.VolumeSize = size
    subnet = {"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "10.0.0.0/24"}
    with Stubber(ec2) as stub:
        stub.add_response("describe_subnets", {"Subnets": [subnet]}, THE_SUBNET)
        stub.add_response("describe_images", sized_image(8), THE_IMAGE)
        resolved = apply._resolve(ec2, service, values)
        stub.assert_no_pending_responses()
    assert resolved.image_id == IMAGE
    assert resolved.root_device == device


def test_it_stops_before_writing_when_ssh_keygen_is_missing(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定の鍵を作るときだけ ssh-keygen が要る。無ければ、確認の前の検出で止まる。"""
    values = write_conf(service)
    step4(aws_stubs)
    step5(aws_stubs, values)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(TemtError, match=f"command not found: {keys.KEYGEN}"):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not service.command_path.exists()
    assert not ssh_dir().exists()


def test_apply_needs_no_ssh_keygen_when_the_key_is_in_place(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
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
    assert home.write_edit(home.detect_host_block(service, Conf())) is True
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
    assert home.write_edit(home.detect_host_block(service, Conf())) is True
    monkeypatch.setenv(CONF_DIR_ENV, str(service.command_path.parent / "elsewhere"))
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)

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
    values = write_conf(service)
    home.prepare_dirs()
    (ssh_dir() / "config").write_text("Host temt-dev-old\n    User ubuntu\n", encoding="utf-8")
    _ = home.write_include(home.detect_include(), home.detect_host_block(service, Conf()))
    monkeypatch.setenv(SSH_DIR_ENV, str(Path.home() / "elsewhere"))
    make_key()
    nothing_to_do(aws_stubs, values)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert (
        "warning: ~/.ssh/config no longer includes ~/.ssh/temt/config; "
        "ssh cannot reach temt-dev-old there"
    ) in capsys.readouterr().err


def test_the_option_is_written_as_the_profile(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--profile で選んだプロファイルは、次からのコマンドも使うよう設定ファイルに書く。"""
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert conf.load(service).AwsProfile == "work"
    assert f'wrote AwsProfile = "work" in {service.conf_path}' in capsys.readouterr().out
    # ssh の設定は設定ファイルを正とするので、ProxyCommand に --profile は入らない
    assert "--profile" not in (ssh_dir() / "config").read_text(encoding="utf-8")


def test_a_new_config_file_gets_the_option_as_the_profile(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """設定ファイルが無いときは、AwsProfile を先に書いてから既定の 4 項目を書く。

    更新の途中（手順 5 の関連付けの作成）で止まっても、手順 2 は書き終えているので、
    書いた項目と受信規則の例は残る。鍵は手順 6 で作るので、まだ作らない。
    """
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    step2_ami(aws_stubs)
    aws_stubs.ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": VPC}]}, DEFAULT_VPC)
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "AvailabilityZone": f"{REGION}a", "VpcId": VPC}]},
        PUBLIC_SUBNETS,
    )
    step4(aws_stubs)
    step5(aws_stubs, WRITTEN, associated=False)
    aws_stubs.ssm.add_client_error(
        "create_association", service_error_code="AccessDeniedException", http_status_code=400
    )

    with pytest.raises(ClientError):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not keys.default_path().exists()
    assert not (ssh_dir() / "config").exists()
    assert (
        service.conf_path.read_text(encoding="utf-8")
        == conf.HEADER
        + conf_text({"AwsProfile": "work", **WRITTEN})
        + conf.EC2_DEFAULT
        + conf.EXAMPLE
    )


def test_the_option_replaces_the_written_profile(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """書かれていたプロファイルと違えば、その行を書き換える。"""
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    values = write_conf(service, AwsProfile="other")
    make_key()
    nothing_to_do(aws_stubs, values)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert conf.load(service).AwsProfile == "work"
    assert service.conf_path.read_text(encoding="utf-8").count("AwsProfile") == 1


def test_the_same_profile_is_not_written_again(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--profile が書かれている値と同じなら、設定ファイルは変えない。"""
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    values = write_conf(service, AwsProfile="work")
    before = service.conf_path.read_text(encoding="utf-8")
    make_key()
    nothing_to_do(aws_stubs, values)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert "wrote AwsProfile" not in capsys.readouterr().out


def test_the_written_profile_is_left_alone_without_the_option(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """--profile が無ければ、AwsProfile は書き足しも書き換えもしない。"""
    values = write_conf(service, AwsProfile="work")
    before = service.conf_path.read_text(encoding="utf-8")
    make_key()
    nothing_to_do(aws_stubs, values)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert aws_stubs.passed[0].AwsProfile == "work"
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert "wrote AwsProfile" not in capsys.readouterr().out


def test_the_ssh_config_is_left_alone_when_a_resource_fails(
    aws_stubs: Stubs, service: Service
) -> None:
    """手順 6 は手順 5 の後なので、手順 5 の更新で止まれば ssh の設定は書かれない。"""
    values = write_conf(service)
    changed = {**values, conf.launch_template_key("InstanceType"): "t3.small"}
    service.conf_path.write_text(conf_text(changed) + conf.EC2_DEFAULT, encoding="utf-8")
    make_key()
    nothing_to_do(aws_stubs, values)
    aws_stubs.ec2.add_client_error(
        "create_launch_template_version", service_error_code="AccessDenied", http_status_code=403
    )

    with pytest.raises(ClientError):
        apply.run(service, yes=True)

    aws_stubs.done()
    assert not (ssh_dir() / "config").exists()


def tags_listed(stubs: Stubs) -> None:
    """関連付けのタグ。temt のタグが付いている。diff は関連付けの実行を求めない。"""
    stubs.ssm.add_response(
        "list_tags_for_resource",
        {"TagList": ASSOCIATION_MARK},
        {"ResourceType": "Association", "ResourceId": ASSOCIATION},
    )


def nothing_to_show(stubs: Stubs, values: dict[str, str]) -> None:
    """すべて揃っているときに diff が読むものの応答を積む。書き込みは積まない。"""
    step4(stubs)
    step5(stubs, values, sshd_ports=[22])
    tags_listed(stubs)


def heads(out: str) -> list[list[str]]:
    """diff の出力のうち、変更の行の印、種類、名前。続く字下げした行は除く。"""
    return [line.split()[:3] for line in out.splitlines() if not line.startswith(" ")]


def test_diff_shows_nothing_after_apply(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """apply の直後は、標準出力に何も書かず、違いが無いことを標準エラー出力に知らせる。"""
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    nothing_to_show(aws_stubs, values)
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
    _ = write_conf(service)
    before = service.conf_path.read_text(encoding="utf-8")
    step4(aws_stubs)
    missing(aws_stubs.iam, "get_role", resources.NO_SUCH_ENTITY, RoleName=NAME)
    ec2_policy(aws_stubs, new=True)
    missing(aws_stubs.iam, "list_role_policies", resources.NO_SUCH_ENTITY, RoleName=NAME)
    missing(
        aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME
    )
    aws_stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": []}, THE_GROUP)
    missing(
        aws_stubs.ec2,
        "describe_launch_template_versions",
        resources.NO_SUCH_TEMPLATE,
        **THE_TEMPLATE,
    )
    listed_associations(aws_stubs.ssm)

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
        ["+", "association", SSHD],
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


def test_diff_shows_the_items_that_apply_writes_in_the_config_file(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手順 2 で書く項目を出し、書いたものとしてその後の手順で使う。設定ファイルは変えない。

    SubnetId が書かれていなければ、apply と同じく公開サブネットを選ぶ。選んだものが
    テンプレートのものと同じなので、テンプレートの違いは出ない。
    """
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    apply.run(service, yes=True)
    subnet_key = conf.launch_template_key("SubnetId")
    text = conf.HEADER + conf_text({k: v for k, v in values.items() if k != subnet_key})
    service.conf_path.write_text(text + conf.EC2_DEFAULT, encoding="utf-8")
    before = service.conf_path.read_text(encoding="utf-8")
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    aws_stubs.ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": VPC}]}, DEFAULT_VPC)
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "AvailabilityZone": f"{REGION}a", "VpcId": VPC}]},
        PUBLIC_SUBNETS,
    )
    nothing_to_show(aws_stubs, values)
    _ = capsys.readouterr()

    apply.diff(service)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert heads(out) == [["+", "conf", "AwsProfile"], ["+", "conf", subnet_key]]
    assert out.splitlines()[1].endswith(f'"{SUBNET}"')
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
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    apply.run(service, yes=True)
    gone = keys.public_path(keys.default_path()) if kept == "private" else keys.default_path()
    gone.unlink()
    changed = {**values, conf.launch_template_key("InstanceType"): "t3.small"}
    service.conf_path.write_text(conf_text(changed) + conf.EC2_DEFAULT, encoding="utf-8")
    nothing_to_show(aws_stubs, values)
    nothing_to_show(aws_stubs, values)
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
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
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
    nothing_to_show(aws_stubs, values)
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
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    apply.run(service, yes=True)
    changed = {**values, conf.launch_template_key("InstanceType"): "t3.small"}
    service.conf_path.write_text(conf_text(changed) + conf.EC2_DEFAULT, encoding="utf-8")
    # diff。テンプレートは前の InstanceType のまま
    nothing_to_show(aws_stubs, values)
    _ = capsys.readouterr()

    apply.diff(service)

    out = capsys.readouterr().out
    assert out == (f"~   launch-template   {TEMPLATE}   InstanceType: t3.micro -> t3.small\n")
    # apply。同じ応答に、テンプレートの書き込みだけを積む
    step4(aws_stubs)
    step5(aws_stubs, values, sshd_ports=[22])
    aws_stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": 4}},
        {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": template_data(changed)},
    )
    aws_stubs.ec2.add_response(
        "modify_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "4"},
    )
    aws_stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": 3}]},
        {"LaunchTemplateName": TEMPLATE, "Versions": ["3"]},
    )
    tags_listed(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    shown = capsys.readouterr().out
    assert f"launch template {TEMPLATE}: updated (version 4)" in shown
    assert "wrote" not in shown


def test_diff_and_apply_agree_on_a_new_volume_size(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """VolumeSize を書くと、diff はその行だけを出し、apply は AMI のルートデバイスの
    大きさとして起動テンプレートに入れる。
    """
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    apply.run(service, yes=True)
    write_volume_size(service, 30)
    # diff。テンプレートにはまだ大きさが無い
    step4(aws_stubs, found_image=sized_image(8))
    step5(aws_stubs, values, sshd_ports=[22])
    tags_listed(aws_stubs)
    _ = capsys.readouterr()

    apply.diff(service)

    assert capsys.readouterr().out == f"~   launch-template   {TEMPLATE}   VolumeSize: - -> 30\n"
    # apply。同じ応答に、テンプレートの書き込みだけを積む
    step4(aws_stubs, found_image=sized_image(8))
    step5(aws_stubs, values, sshd_ports=[22])
    sized = to_conf(values)
    sized.launch_template.VolumeSize = 30
    wanted = resources.template_data(
        Service("dev-main"), sized, IMAGE, SUBNET, GROUP, ipv6=False, root_device=ROOT_DEVICE
    )
    assert wanted.get("BlockDeviceMappings") == [
        {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30}}
    ]
    aws_stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": 4}},
        {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted},
    )
    aws_stubs.ec2.add_response(
        "modify_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "4"},
    )
    aws_stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": 3}]},
        {"LaunchTemplateName": TEMPLATE, "Versions": ["3"]},
    )
    tags_listed(aws_stubs)

    apply.run(service, yes=True)

    aws_stubs.done()
    assert f"launch template {TEMPLATE}: updated (version 4)" in capsys.readouterr().out


def test_diff_shows_the_ec2_default_that_apply_writes(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """[[role.policy.ec2]] が無ければ、既定を書くことを出し、その後の手順は既定で比べる。

    ロールのポリシーは既定と同じなので、違いとしては出ない。設定ファイルは変えない。
    """
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    apply.run(service, yes=True)
    service.conf_path.write_text(conf_text(values), encoding="utf-8")
    nothing_to_show(aws_stubs, values)
    _ = capsys.readouterr()

    apply.diff(service)

    aws_stubs.done()
    assert capsys.readouterr().out == f"+   conf   role.policy.{conf.EC2}   (default)\n"
    assert service.conf_path.read_text(encoding="utf-8") == conf_text(values)


def test_diff_stops_like_apply_when_the_ec2_default_cannot_be_added(
    aws_stubs: Stubs, service: Service
) -> None:
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    text = "role = { policy = {} }\n" + conf_text(WRITTEN)
    service.conf_path.write_text(text, encoding="utf-8")

    with pytest.raises(TemtError, match=r"cannot add the default \[\[role\.policy\.ec2\]\] to "):
        apply.diff(service)

    aws_stubs.done()
    assert service.conf_path.read_text(encoding="utf-8") == text


def test_diff_writes_what_it_found_before_it_stops(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """apply が止まるところでは、それまでに検出した変更を出してから、同じエラーで終わる。

    空の InstanceType は書かれていないものとして読むので、apply は既定を書き直す。
    既定の鍵の秘密鍵しか無ければ、手順 6 で止まる。
    """
    values = write_conf(service)
    text = service.conf_path.read_text(encoding="utf-8")
    service.conf_path.write_text(
        text.replace('InstanceType = "t3.micro"', 'InstanceType = ""'), encoding="utf-8"
    )
    keys_dir().mkdir(parents=True)
    keys.default_path().write_text("a private key\n", encoding="utf-8")
    nothing_to_show(aws_stubs, values)

    with pytest.raises(TemtError, match=f"^{KEY}.pub does not exist; "):
        apply.diff(service)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert ["~", "conf", conf.launch_template_key("InstanceType")] in heads(out)
    assert '"" -> "t3.micro"' in out


def test_a_file_in_place_of_the_command_is_left_alone(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """サービスのコマンドの場所に何かあれば、中身にかかわらず差分に挙げず、書かない。"""
    values = write_conf(service)
    service.command_path.parent.mkdir(parents=True)
    service.command_path.write_text("mine\n", encoding="utf-8")
    make_key()
    nothing_to_do(aws_stubs, values)

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
    values = write_conf(service)
    before = service.conf_path.read_text(encoding="utf-8")
    make_key()
    nothing_to_do(aws_stubs, values)
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))

    with pytest.raises(TemtError, match="cancelled"):
        apply.run(service, yes=False)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert f"+   command       ~/.local/bin/{NAME}" in captured.out
    assert "[y/N]" not in captured.out
    assert captured.err == "apply these changes? [y/N] "
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert not service.command_path.exists()
    assert not (ssh_dir() / "config").exists()


def test_apply_needs_yes_without_a_terminal(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

    before = service.conf_path.read_text(encoding="utf-8")

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
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    nothing_to_do(aws_stubs, values)
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
    step2_ami(aws_stubs)
    aws_stubs.ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": VPC}]}, DEFAULT_VPC)
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "AvailabilityZone": f"{REGION}a", "VpcId": VPC}]},
        PUBLIC_SUBNETS,
    )
    step4(aws_stubs)
    step5(aws_stubs, WRITTEN, associated=False)
    monkeypatch.setattr(sys, "stdin", Terminal("\n"))

    with pytest.raises(TemtError, match="cancelled"):
        apply.run(service, yes=False)

    aws_stubs.done()
    out = capsys.readouterr().out
    assert "+   conf " in out
    assert ["+", "key", KEY] in heads(out)
    assert not service.conf_path.exists()
    assert not keys_dir().exists()
    assert not ssh_dir().exists()
    assert not service.command_path.exists()
    assert not (Path.home() / ".ssh").exists()


def test_apply_goes_on_when_the_answer_is_yes(
    aws_stubs: Stubs,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端末で y と答えれば、検出した差分で更新する。"""
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
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
    values = write_conf(service)
    before = service.conf_path.read_text(encoding="utf-8")
    public = keys.public_path(keys.default_path())
    public.parent.mkdir(parents=True)
    public.write_text("ssh-ed25519 AAAA temt\n", encoding="utf-8")
    nothing_to_show(aws_stubs, values)

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
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    apply.run(service, yes=True)
    block = (ssh_dir() / "config").read_text(encoding="utf-8")
    old = keys.public_path(keys.default_path()).read_text(encoding="utf-8")
    keys.default_path().unlink()
    keys.public_path(keys.default_path()).unlink()
    nothing_to_show(aws_stubs, values)
    nothing_to_do(aws_stubs, values)
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
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    apply.run(service, yes=True)
    config = ssh_dir() / "config"
    mine = "    IdentityFile ~/.ssh/no-such-key\n"
    config.write_text(
        config.read_text(encoding="utf-8").replace(f"    IdentityFile       {KEY}\n", mine),
        encoding="utf-8",
    )
    edited = config.read_text(encoding="utf-8")
    keys.default_path().unlink()
    keys.public_path(keys.default_path()).unlink()
    monkeypatch.setenv("PATH", "")
    nothing_to_show(aws_stubs, values)
    nothing_to_do(aws_stubs, values)
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
    assert config.read_text(encoding="utf-8") == edited


def test_a_template_with_a_key_name_gets_a_new_version_without_it(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """前の temt が入れたか手で入れた KeyName は、違いとして出し、新しいバージョンで外す。"""
    values = write_conf(service)
    make_key()
    nothing_to_do(aws_stubs, values)
    apply.run(service, yes=True)
    old = {**template_data(values), "KeyName": "temt"}
    step4(aws_stubs)
    step5(aws_stubs, values, sshd_ports=[22], data=old)
    tags_listed(aws_stubs)
    _ = capsys.readouterr()

    apply.diff(service)

    assert capsys.readouterr().out == f"~   launch-template   {TEMPLATE}   KeyName: temt -> -\n"
    step4(aws_stubs)
    step5(aws_stubs, values, data=old)
    wanted = template_data(values)
    assert "KeyName" not in wanted
    aws_stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": 4}},
        {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted},
    )
    aws_stubs.ec2.add_response(
        "modify_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "4"},
    )
    aws_stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": 3}]},
        {"LaunchTemplateName": TEMPLATE, "Versions": ["3"]},
    )

    apply.run(service, yes=True)

    aws_stubs.done()
    assert f"launch template {TEMPLATE}: updated (version 4)" in capsys.readouterr().out


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
    values = write_conf(service)
    changed = {**values, conf.launch_template_key("InstanceType"): "t3.small"}
    service.conf_path.write_text(conf_text(changed) + conf.EC2_DEFAULT, encoding="utf-8")
    home.prepare_dirs()
    (ssh_dir() / "config").write_text("# mine\n", encoding="utf-8")
    make_key()
    nothing_to_do(aws_stubs, values)
    aws_stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": 4}},
        {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": template_data(changed)},
    )
    aws_stubs.ec2.add_response(
        "modify_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "4"},
    )
    aws_stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": 3}]},
        {"LaunchTemplateName": TEMPLATE, "Versions": ["3"]},
    )
    monkeypatch.setattr(sys, "stdin", Editing("y\n"))

    with pytest.raises(TemtError, match="~/.ssh/temt/config changed after temt read it"):
        apply.run(service, yes=False)

    aws_stubs.done()
    assert (ssh_dir() / "config").read_text(encoding="utf-8") == (
        "# mine\n\nHost mine\n    User root\n"
    )
