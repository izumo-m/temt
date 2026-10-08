"""サービスの AWS リソースを揃える処理のテスト。AWS は Stubber で差し替える。"""

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from conftest import (
    ASSOCIATION,
    SSHD,
    association_content,
    association_in_place,
    association_started,
    described_association,
    listed_associations,
)
from types_boto3_ec2.client import EC2Client
from types_boto3_iam.client import IAMClient
from types_boto3_ssm.client import SSMClient

from temt import policy, resources, sshd
from temt.change import Change
from temt.conf import Conf, Ingress, LaunchTemplate, SecurityGroup
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
TEMPLATE = "temt-dev-main-template"
# SSM パラメータを置く階層
PATH = "/temt-dev-main"
VPC = "vpc-0123456789abcdef0"
SUBNET = "subnet-0123456789abcdef0"
GROUP = "sg-0123456789abcdef0"
IMAGE = "ami-0123456789abcdef0"
TEMPLATE_ID = "lt-0123456789abcdef0"
WHEN = datetime(2026, 9, 20, tzinfo=UTC)
# 同じ名前の、もう 1 つの関連付けの ID
OTHER_ASSOCIATION = "99999999-8888-7777-6666-555555555555"

# temt が管理していることを示すタグ。キーペアを除く 5 つに付ける
MARK: list[dict[str, str]] = [{"Key": "temt:service", "Value": "dev-main"}]
# インスタンスとそのボリュームとネットワークインターフェース、セキュリティグループに付けるタグ
TAGS: list[dict[str, str]] = [{"Key": "Name", "Value": NAME}, *MARK]
# temt が付けないタグ。temt は触らない。Service は、他のツールや手作業でも付けられる
# ありふれたキーの例
OTHERS: list[dict[str, str]] = [
    {"Key": "Owner", "Value": "me"},
    {"Key": "Service", "Value": "dev-main"},
]
# 値の違う temt のタグと、temt が付けないタグ
WRONG: list[dict[str, str]] = [{"Key": "temt:service", "Value": "dev"}, *OTHERS]

# [role.policy.jupyter] から作った temt-jupyter の中身
STATEMENT: dict[str, Any] = {
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::bucket/temt-dev-main/*",
}
CONTENT: dict[str, Any] = {"Version": policy.VERSION, "Statement": [STATEMENT]}
JUPYTER = policy.policy_name("jupyter")
THE_POLICY = {"RoleName": NAME, "PolicyName": JUPYTER}
# 足した temt のタグの、diff での表示
MARK_SHOWN = "tags: temt:service=dev-main"

VALUES = Conf(
    KeyName="temt", launch_template=LaunchTemplate(InstanceType="t3.micro", SubnetId=SUBNET)
)

# 揃っているときに、detect が AWS を読む順。update が揃える順と同じである。インライン
# ポリシーは無い
READS = [
    "GetRole",
    "ListRolePolicies",
    "GetInstanceProfile",
    "DescribeSecurityGroups",
    "DescribeSecurityGroupRules",
    "DescribeLaunchTemplateVersions",
    "DescribeLaunchTemplates",
    "ListAssociations",
    "DescribeAssociation",
    "ListTagsForResource",
]


def a_role(tags: list[dict[str, str]] | None = MARK) -> dict[str, Any]:
    """get-role と create-role が返す形。

    タグを 1 つも持たないロールは、AWS が Tags を返さない。tags が None のときがそれに
    当たる。
    """
    found: dict[str, Any] = {
        "Path": "/",
        "RoleName": NAME,
        "RoleId": "AROAEXAMPLE000000",
        "Arn": f"arn:aws:iam::123456789012:role/{NAME}",
        "CreateDate": WHEN,
    }
    if tags is not None:
        found["Tags"] = tags
    return {"Role": found}


def a_profile(roles: list[str], tags: list[dict[str, str]] | None = MARK) -> dict[str, Any]:
    """get-instance-profile と create-instance-profile が返す形。

    タグを 1 つも持たないインスタンスプロファイルは、AWS が Tags を返さない。tags が
    None のときがそれに当たる。
    """
    found: dict[str, Any] = {
        "Path": "/",
        "InstanceProfileName": NAME,
        "InstanceProfileId": "AIPAEXAMPLE000000",
        "Arn": f"arn:aws:iam::123456789012:instance-profile/{NAME}",
        "CreateDate": WHEN,
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
    if tags is not None:
        found["Tags"] = tags
    return {"InstanceProfile": found}


def ok(response: dict[str, Any]) -> dict[str, Any]:
    """waiter は HTTP の状態で判断するので、応答にそれを入れる。"""
    return {**response, "ResponseMetadata": {"HTTPStatusCode": 200}}


def attached(*arns: str) -> dict[str, Any]:
    """list-attached-role-policies が返す形。"""
    return {
        "AttachedPolicies": [
            {"PolicyName": arn.rpartition("/")[2], "PolicyArn": arn} for arn in arns
        ],
        "IsTruncated": False,
    }


def missing(stub: Stubber, method: str, code: str, **params: Any) -> None:
    """そのリソースが無いときの誤りを積む。

    実装は誤りの名前しか見ないが、IAM の waiter は HTTP の状態で判断するので、
    IAM に合わせて 404 で揃える。
    """
    stub.add_client_error(
        method, service_error_code=code, http_status_code=404, expected_params=params or None
    )


def wanted(*, ipv6: bool = False) -> dict[str, Any]:
    """temt が起動テンプレートに入れる設定。"""
    return dict(
        resources.template_data(Service("dev-main"), VALUES, IMAGE, SUBNET, GROUP, ipv6=ipv6)
    )


def echo(**changes: Any) -> dict[str, Any]:
    """AWS が返す形。並びを変え、名前は ARN で返し、temt が決めない項目を混ぜる。

    手で足された項目があっても動じないことを確かめるために EbsOptimized を、
    IPv6 アドレスを付けないことを 0 で表した応答も同じと見なすことを確かめるために
    Ipv6AddressCount = 0 を入れてある。
    """
    data = wanted()
    data["IamInstanceProfile"] = {"Arn": f"arn:aws:iam::123456789012:instance-profile/{NAME}"}
    data["NetworkInterfaces"] = [{**data["NetworkInterfaces"][0], "Ipv6AddressCount": 0}]
    data["TagSpecifications"] = [
        {"ResourceType": one["ResourceType"], "Tags": list(reversed(one["Tags"]))}
        for one in reversed(data["TagSpecifications"])
    ]
    data["EbsOptimized"] = False
    return {**data, **changes}


def tags(resource_type: str) -> dict[str, Any]:
    """1 つのリソース種別ぶんのタグ仕様。"""
    return {"ResourceType": resource_type, "Tags": TAGS}


def with_ingress(*ingress: Ingress) -> Conf:
    """受信規則を定めた設定。"""
    return Conf(
        KeyName="temt",
        launch_template=LaunchTemplate(InstanceType="t3.micro", SubnetId=SUBNET),
        security_group=SecurityGroup(ingress=list(ingress)),
    )


def a_group(*, group_id: str = GROUP, vpc_id: str = VPC, tagged: bool = True) -> dict[str, Any]:
    """describe-security-groups が返す 1 つ分。"""
    group: dict[str, Any] = {"GroupId": group_id, "GroupName": NAME, "VpcId": vpc_id}
    if tagged:
        group["Tags"] = TAGS
    return group


def groups(stub: Stubber, *found: dict[str, Any]) -> None:
    """名前で絞った describe-security-groups の応答を積む。"""
    stub.add_response(
        "describe_security_groups",
        {"SecurityGroups": list(found)},
        {"Filters": [{"Name": "group-name", "Values": [NAME]}]},
    )


def rules(stub: Stubber, *found: dict[str, Any]) -> None:
    """セキュリティグループで絞った describe-security-group-rules の応答を積む。"""
    stub.add_response(
        "describe_security_group_rules",
        {"SecurityGroupRules": list(found)},
        {"Filters": [{"Name": "group-id", "Values": [GROUP]}]},
    )


def a_rule(
    rule_id: str,
    low: int,
    high: int,
    cidr: str,
    description: str = "",
    *,
    protocol: str = "tcp",
    egress: bool = False,
) -> dict[str, Any]:
    """describe-security-group-rules が返す規則 1 つ分。AWS は説明の無い規則に
    Description を付けない。
    """
    item: dict[str, Any] = {
        "SecurityGroupRuleId": rule_id,
        "GroupId": GROUP,
        "IsEgress": egress,
        "IpProtocol": protocol,
        "FromPort": low,
        "ToPort": high,
        ("CidrIpv6" if ":" in cidr else "CidrIpv4"): cidr,
    }
    if description:
        item["Description"] = description
    return item


def permission(
    low: int, high: int, cidr: str, description: str = "", *, protocol: str = "tcp"
) -> dict[str, Any]:
    """authorize-security-group-ingress に渡す規則 1 つ分。"""
    entry: dict[str, str] = {"CidrIpv6" if ":" in cidr else "CidrIp": cidr}
    if description:
        entry["Description"] = description
    return {
        "IpProtocol": protocol,
        "FromPort": low,
        "ToPort": high,
        ("Ipv6Ranges" if ":" in cidr else "IpRanges"): [entry],
    }


def interface(**changes: Any) -> dict[str, Any]:
    """echo() の、ネットワークインターフェースを 1 項目だけ変えたもの。"""
    data = echo()
    data["NetworkInterfaces"] = [{**data["NetworkInterfaces"][0], **changes}]
    return data


def one_tag(**changes: Any) -> dict[str, Any]:
    """echo() の、タグを 1 つだけ変えたもの。"""
    data = echo()
    first, *rest = data["TagSpecifications"]
    head, *tail = first["Tags"]
    data["TagSpecifications"] = [{**first, "Tags": [{**head, **changes}, *tail]}, *rest]
    return data


def a_role_policy(content: dict[str, Any]) -> dict[str, Any]:
    """get-role-policy が返す形。中身は URL エンコードした JSON で返る。"""
    return {
        "RoleName": NAME,
        "PolicyName": JUPYTER,
        "PolicyDocument": quote(json.dumps(content)),
    }


def recorded(*clients: Any) -> list[str]:
    """呼ばれた API の名前を、クライアントをまたいで 1 つの並びに集める。"""
    calls: list[str] = []
    for client in clients:
        client.meta.events.register(
            "provide-client-params",
            lambda model, calls=calls, **kwargs: calls.append(model.name),  # noqa: B008
        )
    return calls


def a_version(data: dict[str, Any], number: int = 1, *, default: bool = True) -> dict[str, Any]:
    """describe-launch-template-versions が返すバージョン 1 つ分。"""
    return {
        "LaunchTemplateName": TEMPLATE,
        "VersionNumber": number,
        "DefaultVersion": default,
        "LaunchTemplateData": data,
    }


def listed(*found: dict[str, Any]) -> dict[str, Any]:
    """describe-launch-template-versions の応答。"""
    return {"LaunchTemplateVersions": list(found)}


def template_tags(stub: Stubber, tags: list[dict[str, str]] | None = MARK) -> None:
    """describe-launch-templates の応答を積む。起動テンプレート自身のタグを返す。

    タグを 1 つも持たないテンプレートは、AWS が Tags を返さない。tags が None のときが
    それに当たる。
    """
    found: dict[str, Any] = {"LaunchTemplateId": TEMPLATE_ID, "LaunchTemplateName": TEMPLATE}
    if tags is not None:
        found["Tags"] = tags
    stub.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [found]},
        {"LaunchTemplateNames": [TEMPLATE]},
    )


def create_template(stub: Stubber, data: dict[str, Any], **response: Any) -> None:
    """起動テンプレートを、temt のタグを付けて作る応答を積む。response は応答に足す項目。"""
    stub.add_response(
        "create_launch_template",
        {
            "LaunchTemplate": {"LaunchTemplateName": TEMPLATE, "DefaultVersionNumber": 1},
            **response,
        },
        {
            "LaunchTemplateName": TEMPLATE,
            "LaunchTemplateData": data,
            "TagSpecifications": [{"ResourceType": "launch-template", "Tags": MARK}],
        },
    )


def deleted(*numbers: int) -> dict[str, Any]:
    """delete-launch-template-versions が、すべて消せたときに返す形。"""
    return {
        "SuccessfullyDeletedLaunchTemplateVersions": [
            {"LaunchTemplateName": TEMPLATE, "VersionNumber": number} for number in numbers
        ]
    }


def align_role(iam: IAMClient, service: Service) -> None:
    """apply と同じく、IAM ロールの差分を検出してから、その差分で揃える。"""
    resources.role(iam, service, resources.detect_role(iam, service))


def align_profile(iam: IAMClient, service: Service) -> None:
    """apply と同じく、インスタンスプロファイルの差分を検出してから、その差分で揃える。"""
    resources.instance_profile(iam, service, resources.detect_instance_profile(iam, service))


def align_group(ec2: EC2Client, service: Service, values: Conf = VALUES) -> str:
    """apply と同じく、セキュリティグループの差分を検出してから、その差分で揃える。"""
    found = resources.detect_security_group(ec2, service, values, VPC)
    return resources.security_group(ec2, service, found)


def align_template(ec2: EC2Client, service: Service, *, ipv6: bool = False) -> None:
    """apply と同じく、起動テンプレートの差分を検出してから、その差分で揃える。

    セキュリティグループ GROUP は、既にあるものとする。
    """
    found = resources.detect_template(ec2, service, VALUES, IMAGE, SUBNET, GROUP, ipv6=ipv6)
    resources.template(ec2, service, found, GROUP)


def iam_in_place(stub: Stubber) -> None:
    """ロールとインスタンスプロファイルが揃っていて、インラインポリシーが無いときに
    読む応答を積む。
    """
    stub.add_response("get_role", a_role(), {"RoleName": NAME})
    stub.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
    stub.add_response("get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME})


def template_in_place(stub: Stubber) -> None:
    """起動テンプレートが揃っているときに読む応答を積む。セキュリティグループは GROUP を指す。"""
    stub.add_response(
        "describe_launch_template_versions",
        listed(a_version(echo())),
        {"LaunchTemplateName": TEMPLATE},
    )
    template_tags(stub)


def an_association(
    stub: Stubber, ports: list[int], tags: list[dict[str, str]], *others: str
) -> None:
    """関連付けが名前で見つかるときに、detect_association が読む応答を積む。

    最初に見つかるのは ASSOCIATION で、ports はそれに埋め込まれた SshdPorts、tags は
    それに付いたタグ。others は、その後に見つかる同じ名前の関連付けの ID で、読まれない。
    """
    listed_associations(stub, ASSOCIATION, *others)
    described_association(stub, ports)
    stub.add_response(
        "list_tags_for_resource",
        {"TagList": tags},
        {"ResourceType": "Association", "ResourceId": ASSOCIATION},
    )


def test_the_inline_policy_is_put_when_the_role_has_none(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(iam) as stub:
        stub.add_response(
            "put_role_policy", {}, {**THE_POLICY, "PolicyDocument": json.dumps(CONTENT)}
        )
        resources.inline_policy(iam, service, resources.PolicyDiff(JUPYTER, None, CONTENT))
        stub.assert_no_pending_responses()
    assert "role policy temt-jupyter: created" in capsys.readouterr().out


def test_the_inline_policy_that_is_already_right_is_left_alone(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """揃っていれば何も書かず、知らせもしない。"""
    with Stubber(iam) as stub:
        resources.inline_policy(iam, service, resources.PolicyDiff(JUPYTER, CONTENT, CONTENT))
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_the_inline_policy_that_differs_is_rewritten(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {**CONTENT, "Statement": [{**STATEMENT, "Action": "s3:PutObject"}]}
    with Stubber(iam) as stub:
        stub.add_response(
            "put_role_policy", {}, {**THE_POLICY, "PolicyDocument": json.dumps(CONTENT)}
        )
        resources.inline_policy(iam, service, resources.PolicyDiff(JUPYTER, before, CONTENT))
        stub.assert_no_pending_responses()
    assert "role policy temt-jupyter: updated" in capsys.readouterr().out


def test_the_inline_policy_tells_true_from_one(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """Python では True == 1 なので、dict のまま比べると直した値が IAM に届かない。"""
    condition = {"Bool": {"aws:SecureTransport": True}}
    content = {**CONTENT, "Statement": [{**STATEMENT, "Condition": condition}]}
    before = {
        **CONTENT,
        "Statement": [{**STATEMENT, "Condition": {"Bool": {"aws:SecureTransport": 1}}}],
    }
    with Stubber(iam) as stub:
        stub.add_response(
            "put_role_policy", {}, {**THE_POLICY, "PolicyDocument": json.dumps(content)}
        )
        resources.inline_policy(iam, service, resources.PolicyDiff(JUPYTER, before, content))
        stub.assert_no_pending_responses()
    assert "role policy temt-jupyter: updated" in capsys.readouterr().out


def test_the_inline_policy_ignores_the_order_of_keys(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {"Statement": [dict(reversed(STATEMENT.items()))], "Version": policy.VERSION}
    found = resources.PolicyDiff(JUPYTER, before, CONTENT)
    assert found.changes() == []
    with Stubber(iam) as stub:
        resources.inline_policy(iam, service, found)
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_the_inline_policy_is_deleted_when_no_content_is_given(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(iam) as stub:
        stub.add_response("delete_role_policy", {}, THE_POLICY)
        resources.inline_policy(iam, service, resources.PolicyDiff(JUPYTER, CONTENT, None))
        stub.assert_no_pending_responses()
    assert "role policy temt-jupyter: deleted" in capsys.readouterr().out


def test_the_inline_policy_is_absent_when_neither_has_it(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """消すものが既に無ければ、何もせず、知らせもしない。"""
    with Stubber(iam) as stub:
        resources.inline_policy(iam, service, resources.PolicyDiff(JUPYTER, None, None))
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_the_inline_policies_are_detected_without_writing(iam: IAMClient, service: Service) -> None:
    """設定ファイルの順に読み、その後に消すものを並べる。何も書かない。

    IAM は中身を URL エンコードして返し、boto3 がそれを読んで返す。temt- で始まらない
    インラインポリシーは、temt で始まっても見ない。
    """
    fresh = policy.policy_name("fresh")
    idle = policy.policy_name("idle")
    before = {**CONTENT, "Statement": [{**STATEMENT, "Action": "s3:PutObject"}]}
    with Stubber(iam) as stub:
        stub.add_response("get_role_policy", a_role_policy(before), THE_POLICY)
        missing(stub, "get_role_policy", "NoSuchEntity", RoleName=NAME, PolicyName=fresh)
        stub.add_response(
            "list_role_policies",
            {"PolicyNames": [JUPYTER, idle, "by-hand", "temtacle"]},
            {"RoleName": NAME},
        )
        stub.add_response(
            "get_role_policy",
            {**a_role_policy(CONTENT), "PolicyName": idle},
            {"RoleName": NAME, "PolicyName": idle},
        )
        found = resources.detect_role_policies(iam, service, {JUPYTER: CONTENT, fresh: CONTENT})
        stub.assert_no_pending_responses()
    assert found == [
        resources.PolicyDiff(JUPYTER, before, CONTENT),
        resources.PolicyDiff(fresh, None, CONTENT),
        resources.PolicyDiff(idle, CONTENT, None),
    ]


def test_the_inline_policies_of_a_role_that_is_not_there(iam: IAMClient, service: Service) -> None:
    """ロールが無ければ、読むとどれも NoSuchEntity になり、無いものとする。"""
    with Stubber(iam) as stub:
        missing(stub, "get_role_policy", "NoSuchEntity", **THE_POLICY)
        missing(stub, "list_role_policies", "NoSuchEntity", RoleName=NAME)
        found = resources.detect_role_policies(iam, service, {JUPYTER: CONTENT})
        stub.assert_no_pending_responses()
    assert found == [resources.PolicyDiff(JUPYTER, None, CONTENT)]


def test_other_errors_from_reading_the_inline_policy_are_left_to_apply(
    iam: IAMClient, service: Service
) -> None:
    with Stubber(iam) as stub:
        stub.add_client_error(
            "get_role_policy", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(ClientError):
            _ = resources.detect_role_policies(iam, service, {JUPYTER: CONTENT})


def test_the_changes_of_the_inline_policies_show_the_documents() -> None:
    """足す、書き換える、消すのそれぞれで、中身の違いを unified diff で続く行に出す。"""
    before = {**CONTENT, "Statement": [{**STATEMENT, "Action": "s3:PutObject"}]}
    lines = json.dumps(CONTENT, indent=2, sort_keys=True).split("\n")
    assert resources.PolicyDiff(JUPYTER, None, CONTENT).changes() == [
        Change(
            "+",
            resources.ROLE_POLICY,
            JUPYTER,
            body=("@@ -0,0 +1,10 @@", *(f"+{line}" for line in lines)),
        )
    ]
    assert resources.PolicyDiff(JUPYTER, before, CONTENT).changes() == [
        Change(
            "~",
            resources.ROLE_POLICY,
            JUPYTER,
            body=(
                "@@ -2,5 +2,5 @@",
                '   "Statement": [',
                "     {",
                '-      "Action": "s3:PutObject",',
                '+      "Action": "s3:GetObject",',
                '       "Effect": "Allow",',
                '       "Resource": "arn:aws:s3:::bucket/temt-dev-main/*"',
            ),
        )
    ]
    assert resources.PolicyDiff(JUPYTER, CONTENT, None).changes() == [
        Change(
            "-",
            resources.ROLE_POLICY,
            JUPYTER,
            body=("@@ -1,10 +0,0 @@", *(f"-{line}" for line in lines)),
        )
    ]


def test_role_policies_puts_each_and_deletes_the_temt_ones_that_are_gone(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """すべて読んでから書き、書いてから消す。temt- で始まらないインラインポリシーには、
    temt で始まっても触らない。"""
    calls = recorded(iam)
    idle = policy.policy_name("idle")
    with Stubber(iam) as stub:
        missing(stub, "get_role_policy", "NoSuchEntity", **THE_POLICY)
        stub.add_response(
            "list_role_policies",
            {"PolicyNames": [idle, "by-hand", "temtacle"]},
            {"RoleName": NAME},
        )
        stub.add_response(
            "get_role_policy",
            {**a_role_policy(CONTENT), "PolicyName": idle},
            {"RoleName": NAME, "PolicyName": idle},
        )
        stub.add_response(
            "put_role_policy", {}, {**THE_POLICY, "PolicyDocument": json.dumps(CONTENT)}
        )
        stub.add_response("delete_role_policy", {}, {"RoleName": NAME, "PolicyName": idle})
        found = resources.detect_role_policies(iam, service, {JUPYTER: CONTENT})
        resources.role_policies(iam, service, found)
        stub.assert_no_pending_responses()
    assert calls == [
        "GetRolePolicy",
        "ListRolePolicies",
        "GetRolePolicy",
        "PutRolePolicy",
        "DeleteRolePolicy",
    ]
    err = capsys.readouterr().out
    assert "role policy temt-jupyter: created" in err
    assert "role policy temt-idle: deleted" in err
    assert "by-hand" not in err
    assert "temtacle" not in err


def test_role_is_created_with_its_tag(iam: IAMClient, service: Service) -> None:
    """管理ポリシーは付けない。権限はインラインポリシーだけである。"""
    with Stubber(iam) as stub:
        missing(stub, "get_role", "NoSuchEntity", RoleName=NAME)
        stub.add_response(
            "create_role",
            a_role(),
            {"RoleName": NAME, "AssumeRolePolicyDocument": resources.TRUST_POLICY, "Tags": MARK},
        )
        align_role(iam, service)
        stub.assert_no_pending_responses()


def test_role_that_is_already_right_is_left_alone(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """タグが揃っていれば、何も書かず、知らせもしない。temt が付けないタグがあってもよい。"""
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role([*MARK, *OTHERS]), {"RoleName": NAME})
        align_role(iam, service)
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_a_role_without_any_tag_gets_it(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """タグを 1 つも持たないロールは、AWS が Tags を返さない。"""
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(None), {"RoleName": NAME})
        stub.add_response("tag_role", {}, {"RoleName": NAME, "Tags": MARK})
        align_role(iam, service)
        stub.assert_no_pending_responses()
    assert f"role {NAME}: updated" in capsys.readouterr().out


def test_a_role_tag_with_another_value_is_rewritten(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """値の違うタグは書き直す。temt が付けないタグには触らない。"""
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(WRONG), {"RoleName": NAME})
        stub.add_response("tag_role", {}, {"RoleName": NAME, "Tags": MARK})
        align_role(iam, service)
        stub.assert_no_pending_responses()
    assert f"role {NAME}: updated" in capsys.readouterr().out


def test_a_role_that_only_has_other_tags_gets_its_tag(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """temt のタグが無ければ足す。同じ値の Service のタグは、temt のタグと見なさない。"""
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(OTHERS), {"RoleName": NAME})
        stub.add_response("tag_role", {}, {"RoleName": NAME, "Tags": MARK})
        align_role(iam, service)
        stub.assert_no_pending_responses()
    assert f"role {NAME}: updated" in capsys.readouterr().out


def test_other_iam_errors_are_left_to_apply(iam: IAMClient, service: Service) -> None:
    with Stubber(iam) as stub:
        stub.add_client_error("get_role", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(ClientError):
            _ = resources.detect_role(iam, service)


def test_the_role_is_detected_without_writing(iam: IAMClient, service: Service) -> None:
    """無いロールは作るものとして、タグの無いロールは足すタグを添えて見せる。"""
    with Stubber(iam) as stub:
        missing(stub, "get_role", "NoSuchEntity", RoleName=NAME)
        stub.add_response("get_role", a_role(OTHERS), {"RoleName": NAME})
        absent = resources.detect_role(iam, service)
        untagged = resources.detect_role(iam, service)
        stub.assert_no_pending_responses()
    assert absent.changes() == [Change("+", resources.ROLE, NAME)]
    assert untagged.changes() == [Change("~", resources.ROLE, NAME, MARK_SHOWN)]


def test_the_instance_profile_is_created_with_its_tag_and_waited_for(
    iam: IAMClient, service: Service
) -> None:
    with Stubber(iam) as stub:
        missing(stub, "get_instance_profile", "NoSuchEntity", InstanceProfileName=NAME)
        stub.add_response(
            "create_instance_profile", a_profile([]), {"InstanceProfileName": NAME, "Tags": MARK}
        )
        # waiter instance_profile_exists がもう一度読む
        stub.add_response("get_instance_profile", ok(a_profile([])), {"InstanceProfileName": NAME})
        stub.add_response(
            "add_role_to_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
        )
        align_profile(iam, service)
        stub.assert_no_pending_responses()


def test_the_role_is_put_back_into_an_empty_instance_profile(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(iam) as stub:
        stub.add_response("get_instance_profile", a_profile([]), {"InstanceProfileName": NAME})
        stub.add_response(
            "add_role_to_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
        )
        align_profile(iam, service)
        stub.assert_no_pending_responses()
    assert f"instance profile {NAME}: updated" in capsys.readouterr().out


def test_an_instance_profile_that_is_already_right(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ロールもタグも揃っていれば、何も書かず、知らせもしない。
    temt が付けないタグがあってもよい。
    """
    with Stubber(iam) as stub:
        stub.add_response(
            "get_instance_profile",
            a_profile([NAME], [*MARK, *OTHERS]),
            {"InstanceProfileName": NAME},
        )
        align_profile(iam, service)
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_an_instance_profile_that_only_has_other_tags_gets_its_tag(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """temt のタグが無ければ足す。同じ値の Service のタグは、temt のタグと見なさない。"""
    with Stubber(iam) as stub:
        stub.add_response(
            "get_instance_profile", a_profile([NAME], OTHERS), {"InstanceProfileName": NAME}
        )
        stub.add_response("tag_instance_profile", {}, {"InstanceProfileName": NAME, "Tags": MARK})
        align_profile(iam, service)
        stub.assert_no_pending_responses()
    assert f"instance profile {NAME}: updated" in capsys.readouterr().out


def test_an_instance_profile_without_any_tag_gets_it(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """タグを 1 つも持たないインスタンスプロファイルは、AWS が Tags を返さない。"""
    with Stubber(iam) as stub:
        stub.add_response(
            "get_instance_profile", a_profile([NAME], None), {"InstanceProfileName": NAME}
        )
        stub.add_response("tag_instance_profile", {}, {"InstanceProfileName": NAME, "Tags": MARK})
        align_profile(iam, service)
        stub.assert_no_pending_responses()
    assert f"instance profile {NAME}: updated" in capsys.readouterr().out


def test_an_instance_profile_tag_with_another_value_is_rewritten(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """値の違うタグは書き直す。temt が付けないタグには触らない。"""
    with Stubber(iam) as stub:
        stub.add_response(
            "get_instance_profile", a_profile([NAME], WRONG), {"InstanceProfileName": NAME}
        )
        stub.add_response("tag_instance_profile", {}, {"InstanceProfileName": NAME, "Tags": MARK})
        align_profile(iam, service)
        stub.assert_no_pending_responses()
    assert f"instance profile {NAME}: updated" in capsys.readouterr().out


def test_another_role_in_the_instance_profile_is_swapped(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """別のロールが入っていれば、外してからロール <名前> を入れる。

    インスタンスプロファイルに入れられるロールは 1 つだけである。diff は入れ替えを
    role: <今のロール> -> <名前> で見せる。
    """
    with Stubber(iam) as stub:
        stub.add_response(
            "get_instance_profile", a_profile(["other"]), {"InstanceProfileName": NAME}
        )
        found = resources.detect_instance_profile(iam, service)
        stub.add_response(
            "remove_role_from_instance_profile",
            {},
            {"InstanceProfileName": NAME, "RoleName": "other"},
        )
        stub.add_response(
            "add_role_to_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
        )
        resources.instance_profile(iam, service, found)
        stub.assert_no_pending_responses()
    assert found.changes() == [
        Change("~", resources.INSTANCE_PROFILE, NAME, f"role: other -> {NAME}")
    ]
    err = capsys.readouterr().out
    assert f"instance profile {NAME}: removed the role other\n" in err
    assert f"instance profile {NAME}: updated\n" in err


def test_the_instance_profile_is_detected_without_writing(iam: IAMClient, service: Service) -> None:
    """無いものは作るものとして、あるものは足すタグと入れるロールを見せる。"""
    with Stubber(iam) as stub:
        missing(stub, "get_instance_profile", "NoSuchEntity", InstanceProfileName=NAME)
        stub.add_response(
            "get_instance_profile", a_profile([], OTHERS), {"InstanceProfileName": NAME}
        )
        absent = resources.detect_instance_profile(iam, service)
        empty = resources.detect_instance_profile(iam, service)
        stub.assert_no_pending_responses()
    assert absent.changes() == [Change("+", resources.INSTANCE_PROFILE, NAME)]
    assert empty.changes() == [
        Change("~", resources.INSTANCE_PROFILE, NAME, MARK_SHOWN),
        Change("~", resources.INSTANCE_PROFILE, NAME, f"role: - -> {NAME}"),
    ]


def created(stub: Stubber) -> None:
    """タグを付けてセキュリティグループを作る応答を積む。"""
    stub.add_response(
        "create_security_group",
        {"GroupId": GROUP},
        {
            "GroupName": NAME,
            "Description": resources.GROUP_DESCRIPTION,
            "VpcId": VPC,
            "TagSpecifications": [tags("security-group")],
        },
    )


def test_the_security_group_is_created_with_its_tags(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        groups(stub)
        created(stub)
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()


def test_the_security_group_that_is_already_there(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """規則もタグも揃っていれば、何も呼ばず、知らせもしない。"""
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(stub)
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_the_missing_tags_are_added(ec2: EC2Client, service: Service) -> None:
    """規則まで読んでから、タグを書く。"""
    with Stubber(ec2) as stub:
        groups(stub, {"GroupId": GROUP, "GroupName": NAME, "VpcId": VPC, "Tags": TAGS[:1]})
        rules(stub)
        stub.add_response("create_tags", {}, {"Resources": [GROUP], "Tags": TAGS[1:]})
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()


def test_a_group_without_any_tag_gets_them_all(ec2: EC2Client, service: Service) -> None:
    """タグを 1 つも持たないセキュリティグループは、AWS が Tags を返さない。"""
    with Stubber(ec2) as stub:
        groups(stub, a_group(tagged=False))
        rules(stub)
        stub.add_response("create_tags", {}, {"Resources": [GROUP], "Tags": TAGS})
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()


def test_a_tag_with_another_value_is_rewritten(ec2: EC2Client, service: Service) -> None:
    """値の違うタグは書き直す。temt が付けないタグには触らない。"""
    present = [TAGS[0], *WRONG]
    with Stubber(ec2) as stub:
        groups(stub, {"GroupId": GROUP, "GroupName": NAME, "VpcId": VPC, "Tags": present})
        rules(stub)
        stub.add_response("create_tags", {}, {"Resources": [GROUP], "Tags": TAGS[1:]})
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()


def test_a_group_with_the_same_name_in_another_vpc_is_left_alone(
    ec2: EC2Client, service: Service
) -> None:
    """同じ名前は VPC ごとに 1 つまでなので、別の VPC のものは別のセキュリティグループ。"""
    with Stubber(ec2) as stub:
        groups(stub, a_group(group_id="sg-00000000000000002", vpc_id="vpc-other"))
        created(stub)
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()


def test_the_rules_of_the_config_file_are_added(ec2: EC2Client, service: Service) -> None:
    """定義した規則を、作ったばかりのセキュリティグループに足す。icmp は CIDR で ICMPv6 になる。"""
    values = with_ingress(
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"),
        Ingress(Port=(80, 443), Cidr="2001:db8::/64"),
        Ingress(Protocol="udp", Port=(60000, 61000), Cidr="203.0.113.10/32"),
        Ingress(Protocol="icmp", Cidr="203.0.113.10/32"),
        Ingress(Protocol="icmp", Cidr="2001:db8::/64"),
    )
    with Stubber(ec2) as stub:
        groups(stub)
        created(stub)
        stub.add_response(
            "authorize_security_group_ingress",
            {"Return": True},
            {
                "GroupId": GROUP,
                "IpPermissions": [
                    permission(22, 22, "203.0.113.10/32", "home"),
                    permission(80, 443, "2001:db8::/64"),
                    permission(60000, 61000, "203.0.113.10/32", protocol="udp"),
                    permission(-1, -1, "203.0.113.10/32", protocol="icmp"),
                    permission(-1, -1, "2001:db8::/64", protocol="icmpv6"),
                ],
            },
        )
        assert align_group(ec2, service, values) == GROUP
        stub.assert_no_pending_responses()


def test_the_rules_that_already_match_are_left_alone(ec2: EC2Client, service: Service) -> None:
    """AWS が別の並びで返しても、同じ組なら触らない。送信規則は見ない。"""
    values = with_ingress(
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"),
        Ingress(Protocol="icmp", Cidr="2001:db8::10/128"),
    )
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(
            stub,
            a_rule("sgr-3", -1, -1, "0.0.0.0/0", protocol="-1", egress=True),
            a_rule("sgr-2", -1, -1, "2001:db8::10/128", protocol="icmpv6"),
            a_rule("sgr-1", 22, 22, "203.0.113.10/32", "home"),
        )
        assert align_group(ec2, service, values) == GROUP
        stub.assert_no_pending_responses()


def test_protocol_numbers_from_aws_are_names(ec2: EC2Client, service: Service) -> None:
    """AWS が番号で返したプロトコルも、名前に揃えてから比べる。"""
    values = with_ingress(
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32"),
        Ingress(Protocol="udp", Port=(53, 53), Cidr="203.0.113.10/32"),
        Ingress(Protocol="icmp", Cidr="203.0.113.10/32"),
        Ingress(Protocol="icmp", Cidr="2001:db8::/64"),
    )
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(
            stub,
            a_rule("sgr-1", 22, 22, "203.0.113.10/32", protocol="6"),
            a_rule("sgr-2", 53, 53, "203.0.113.10/32", protocol="17"),
            a_rule("sgr-3", -1, -1, "203.0.113.10/32", protocol="1"),
            a_rule("sgr-4", -1, -1, "2001:db8::/64", protocol="58"),
        )
        assert align_group(ec2, service, values) == GROUP
        stub.assert_no_pending_responses()


def test_only_the_differences_are_applied_adding_first(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """同じ規則には触らず、足りない規則を足してから、定義に無い規則を消す。"""
    values = with_ingress(
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"),
        Ingress(Port=(22, 22), Cidr="198.51.100.7/32", Description="office"),
    )
    calls = recorded(ec2)
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(
            stub,
            a_rule("sgr-1", 22, 22, "203.0.113.10/32", "home"),
            a_rule("sgr-2", 443, 443, "0.0.0.0/0"),
        )
        stub.add_response(
            "authorize_security_group_ingress",
            {"Return": True},
            {"GroupId": GROUP, "IpPermissions": [permission(22, 22, "198.51.100.7/32", "office")]},
        )
        stub.add_response(
            "revoke_security_group_ingress",
            {"Return": True},
            {"GroupId": GROUP, "SecurityGroupRuleIds": ["sgr-2"]},
        )
        assert align_group(ec2, service, values) == GROUP
        stub.assert_no_pending_responses()
    assert calls[-2:] == ["AuthorizeSecurityGroupIngress", "RevokeSecurityGroupIngress"]
    err = capsys.readouterr().out
    assert "security group temt-dev-main: added 22/tcp 198.51.100.7/32 office\n" in err
    assert "security group temt-dev-main: removed 443/tcp 0.0.0.0/0 -\n" in err
    assert f"security group temt-dev-main: updated ({GROUP})\n" in err


def test_adding_describing_and_removing_come_in_this_order(
    ec2: EC2Client, service: Service
) -> None:
    """足す、説明を直す、消すが揃えば、この順に呼ぶ。"""
    values = with_ingress(
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"),
        Ingress(Port=(80, 80), Cidr="203.0.113.10/32"),
    )
    calls = recorded(ec2)
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(
            stub,
            a_rule("sgr-1", 22, 22, "203.0.113.10/32", "office"),
            a_rule("sgr-2", 443, 443, "0.0.0.0/0"),
        )
        stub.add_response(
            "authorize_security_group_ingress",
            {"Return": True},
            {"GroupId": GROUP, "IpPermissions": [permission(80, 80, "203.0.113.10/32")]},
        )
        stub.add_response(
            "update_security_group_rule_descriptions_ingress",
            {"Return": True},
            {
                "GroupId": GROUP,
                "SecurityGroupRuleDescriptions": [
                    {"SecurityGroupRuleId": "sgr-1", "Description": "home"}
                ],
            },
        )
        stub.add_response(
            "revoke_security_group_ingress",
            {"Return": True},
            {"GroupId": GROUP, "SecurityGroupRuleIds": ["sgr-2"]},
        )
        assert align_group(ec2, service, values) == GROUP
        stub.assert_no_pending_responses()
    assert calls[-3:] == [
        "AuthorizeSecurityGroupIngress",
        "UpdateSecurityGroupRuleDescriptionsIngress",
        "RevokeSecurityGroupIngress",
    ]


def test_a_rule_that_is_not_in_the_config_file_is_removed(ec2: EC2Client, service: Service) -> None:
    """手で足した規則も、定義に無ければ消える。定義が空なら、足すものは無い。"""
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(
            stub,
            a_rule("sgr-1", 22, 22, "203.0.113.10/32"),
            a_rule("sgr-2", 22, 22, "2001:db8::/64"),
        )
        stub.add_response(
            "revoke_security_group_ingress",
            {"Return": True},
            {"GroupId": GROUP, "SecurityGroupRuleIds": ["sgr-1", "sgr-2"]},
        )
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()


def test_only_the_description_differs(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """説明だけの違いは、規則を入れ替えずに説明を書き換える。空の説明は省いて消す。"""
    values = with_ingress(
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"),
        Ingress(Port=(80, 80), Cidr="203.0.113.10/32"),
    )
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(
            stub,
            a_rule("sgr-1", 22, 22, "203.0.113.10/32"),
            a_rule("sgr-2", 80, 80, "203.0.113.10/32", "old"),
        )
        stub.add_response(
            "update_security_group_rule_descriptions_ingress",
            {"Return": True},
            {
                "GroupId": GROUP,
                "SecurityGroupRuleDescriptions": [
                    {"SecurityGroupRuleId": "sgr-1", "Description": "home"},
                    {"SecurityGroupRuleId": "sgr-2"},
                ],
            },
        )
        assert align_group(ec2, service, values) == GROUP
        stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert (
        "security group temt-dev-main: updated the description of 22/tcp 203.0.113.10/32: "
        "- -> home\n"
    ) in out


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param(a_rule("sgr-1", -1, -1, "203.0.113.10/32", protocol="-1"), id="all"),
        pytest.param(a_rule("sgr-1", 8, -1, "203.0.113.10/32", protocol="icmp"), id="icmp type"),
        pytest.param(a_rule("sgr-1", -1, -1, "203.0.113.10/32", protocol="50"), id="esp"),
        pytest.param(
            {
                "SecurityGroupRuleId": "sgr-1",
                "IsEgress": False,
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "ReferencedGroupInfo": {"GroupId": "sg-00000000000000009"},
            },
            id="another group",
        ),
        pytest.param(
            {
                "SecurityGroupRuleId": "sgr-1",
                "IsEgress": False,
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "PrefixListId": "pl-00000000",
            },
            id="prefix list",
        ),
    ],
)
def test_a_rule_that_temt_cannot_write_is_removed(
    ec2: EC2Client, service: Service, rule: dict[str, Any]
) -> None:
    """設定ファイルに書けない種類の規則も、定義に無いので消える。"""
    values = with_ingress(
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32"),
        Ingress(Protocol="icmp", Cidr="203.0.113.10/32"),
    )
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(
            stub,
            a_rule("sgr-2", 22, 22, "203.0.113.10/32"),
            a_rule("sgr-3", -1, -1, "203.0.113.10/32", protocol="icmp"),
            rule,
        )
        stub.add_response(
            "revoke_security_group_ingress",
            {"Return": True},
            {"GroupId": GROUP, "SecurityGroupRuleIds": ["sgr-1"]},
        )
        assert align_group(ec2, service, values) == GROUP
        stub.assert_no_pending_responses()


def test_a_cidr_that_aws_wrote_differently_is_the_same_rule(
    ec2: EC2Client, service: Service
) -> None:
    """IPv6 は書き方が何通りもあるので、AWS が返した形も揃えてから比べる。

    設定ファイルの値は読むときに揃うので、揃っていない側は AWS の応答である。
    """
    values = with_ingress(Ingress(Port=(22, 22), Cidr="2001:db8::10/128"))
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(stub, a_rule("sgr-1", 22, 22, "2001:0DB8:0000::10/128"))
        assert align_group(ec2, service, values) == GROUP
        stub.assert_no_pending_responses()


def test_a_cidr_that_cannot_be_read_is_another_rule(ec2: EC2Client, service: Service) -> None:
    """揃えられない値は、そのまま別のものとして扱い、消す相手にする。"""
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        rules(stub, a_rule("sgr-1", 22, 22, "not-a-cidr"))
        stub.add_response(
            "revoke_security_group_ingress",
            {"Return": True},
            {"GroupId": GROUP, "SecurityGroupRuleIds": ["sgr-1"]},
        )
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()


def test_the_security_group_is_detected_without_writing(ec2: EC2Client, service: Service) -> None:
    """足すタグと、受信規則を足す、説明を直す、消すを、この順に見せる。

    規則の名前は PORT で、違いには送信元を幅を揃えて書き、説明を添える。
    """
    values = with_ingress(
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"),
        Ingress(Port=(80, 80), Cidr="2001:db8::/64"),
    )
    with Stubber(ec2) as stub:
        groups(stub, a_group(tagged=False))
        rules(
            stub,
            a_rule("sgr-1", 22, 22, "203.0.113.10/32", "office"),
            a_rule("sgr-2", 443, 443, "0.0.0.0/0"),
        )
        found = resources.detect_security_group(ec2, service, values, VPC)
        stub.assert_no_pending_responses()
    assert found.changes() == [
        Change(
            "~",
            resources.SECURITY_GROUP,
            NAME,
            "tags: Name=temt-dev-main, temt:service=dev-main",
        ),
        Change("+", resources.INGRESS, "80/tcp", "2001:db8::/64     -"),
        Change("~", resources.INGRESS, "22/tcp", "203.0.113.10/32   office -> home"),
        Change("-", resources.INGRESS, "443/tcp", "0.0.0.0/0         -"),
    ]


def test_a_missing_security_group_is_detected_with_every_rule(
    ec2: EC2Client, service: Service
) -> None:
    """セキュリティグループが無ければ、作るものとして VPC を添え、すべての規則を足すものとする。"""
    values = with_ingress(Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"))
    with Stubber(ec2) as stub:
        groups(stub)
        found = resources.detect_security_group(ec2, service, values, VPC)
        stub.assert_no_pending_responses()
    assert found.group_id is None
    assert found.changes() == [
        Change("+", resources.SECURITY_GROUP, NAME, f"in {VPC}"),
        Change("+", resources.INGRESS, "22/tcp", "203.0.113.10/32   home"),
    ]


@pytest.mark.parametrize(
    ("rule", "shown"),
    [
        (resources.Rule("tcp", 22, 22, "203.0.113.10/32"), "22/tcp"),
        (resources.Rule("udp", 60000, 61000, "203.0.113.10/32"), "60000-61000/udp"),
        (resources.Rule("icmp", -1, -1, "203.0.113.10/32"), "icmp"),
        (resources.Rule("icmpv6", -1, -1, "2001:db8::/64"), "icmp"),
        (resources.Rule("icmpv6", -1, -1, "203.0.113.10/32"), "icmpv6"),
        (resources.Rule("icmp", -1, -1, "2001:db8::/64"), "icmpv4"),
        (resources.Rule("icmpv6", 128, -1, "2001:db8::/64"), "icmp:128:-1"),
        (resources.Rule("icmp", 8, -1, "203.0.113.10/32"), "icmp:8:-1"),
        (resources.Rule("-1", -1, -1, "203.0.113.10/32"), "all"),
        (resources.Rule("50", -1, -1, "203.0.113.10/32"), "50"),
    ],
)
def test_the_port_of_a_rule(rule: resources.Rule, shown: str) -> None:
    """設定ファイルに書ける規則は sg list と同じ形で、書けない規則はプロトコルを添える。"""
    assert rule.port() == shown


def test_not_applied_asks_for_apply() -> None:
    assert str(resources.not_applied(Service("dev-main"))) == (
        f"no launch template {TEMPLATE}; run 'temt apply --svc dev-main' first"
    )


def test_has_template_when_the_template_is_there(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        template_tags(stub)
        assert resources.has_template(ec2, service)
        stub.assert_no_pending_responses()


def test_has_template_when_the_template_is_gone(ec2: EC2Client, service: Service) -> None:
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_templates",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateNames=[TEMPLATE],
        )
        assert not resources.has_template(ec2, service)
        stub.assert_no_pending_responses()


def test_other_errors_from_reading_the_template_are_not_swallowed(
    ec2: EC2Client, service: Service
) -> None:
    """無いことを表す誤りだけを False にする。他は、あるかどうか分からないので上げる。"""
    with Stubber(ec2) as stub:
        stub.add_client_error(
            "describe_launch_templates",
            service_error_code="UnauthorizedOperation",
            http_status_code=403,
        )
        with pytest.raises(ClientError, match="UnauthorizedOperation"):
            _ = resources.has_template(ec2, service)


def test_require_template_passes_when_the_template_is_there(
    ec2: EC2Client, service: Service
) -> None:
    with Stubber(ec2) as stub:
        template_tags(stub)
        resources.require_template(ec2, service)
        stub.assert_no_pending_responses()


def test_require_template_asks_for_apply_when_the_template_is_gone(
    ec2: EC2Client, service: Service
) -> None:
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_templates",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateNames=[TEMPLATE],
        )
        with pytest.raises(
            TemtError, match=f"no launch template {TEMPLATE}; run 'temt apply --svc dev-main' first"
        ):
            resources.require_template(ec2, service)


def test_versions_asks_for_apply_when_the_template_is_gone(
    ec2: EC2Client, service: Service
) -> None:
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateName=TEMPLATE,
        )
        with pytest.raises(
            TemtError, match=f"no launch template {TEMPLATE}; run 'temt apply --svc dev-main' first"
        ):
            _ = resources.versions(ec2, service)


def test_the_launch_template_is_created_with_its_tag(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """テンプレート自身に付けるタグは temt:service だけで、Name は付けない。"""
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            "InvalidLaunchTemplateName.NotFoundException",
            LaunchTemplateName=TEMPLATE,
        )
        create_template(stub, wanted())
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    assert f"launch template {TEMPLATE}: created (version 1)" in capsys.readouterr().out


def test_the_instance_and_its_volumes_and_network_interfaces_are_tagged() -> None:
    """起動したインスタンスと、そのボリュームとネットワークインターフェースに、Name と
    temt:service を付ける。"""
    assert wanted()["TagSpecifications"] == [
        tags("instance"),
        tags("volume"),
        tags("network-interface"),
    ]


def test_the_launch_template_that_already_matches(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """AWS が補った項目や並びの違いは、違いと見なさない。

    テンプレート自身のタグも揃っていれば、何も書かず、知らせもしない。temt が付けない
    タグがあってもよい。
    """
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(), 3)),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub, [*MARK, *OTHERS])
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_a_launch_template_that_only_has_other_tags_gets_its_tag(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """temt のタグだけを足す。中身が同じでも、タグを書けば updated と知らせる。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(), 3)),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub, OTHERS)
        stub.add_response("create_tags", {}, {"Resources": [TEMPLATE_ID], "Tags": MARK})
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    assert f"launch template {TEMPLATE}: updated (version 3)" in capsys.readouterr().out


def test_a_launch_template_without_any_tag_gets_it(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """タグを 1 つも持たないテンプレートは、AWS が Tags を返さない。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(), 3)),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub, None)
        stub.add_response("create_tags", {}, {"Resources": [TEMPLATE_ID], "Tags": MARK})
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    assert f"launch template {TEMPLATE}: updated (version 3)" in capsys.readouterr().out


def test_a_launch_template_tag_with_another_value_is_rewritten(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """値の違うタグは書き直す。temt が付けないタグには触らない。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(), 3)),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub, WRONG)
        stub.add_response("create_tags", {}, {"Resources": [TEMPLATE_ID], "Tags": MARK})
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    assert f"launch template {TEMPLATE}: updated (version 3)" in capsys.readouterr().out


def test_the_tag_and_a_new_version_are_both_written(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """中身もタグも違えば、タグを書いてから新しいバージョンを作る。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(InstanceType="t3.small"))),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub, None)
        stub.add_response("create_tags", {}, {"Resources": [TEMPLATE_ID], "Tags": MARK})
        stub.add_response(
            "create_launch_template_version",
            {"LaunchTemplateVersion": {"VersionNumber": 2}},
            {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted()},
        )
        stub.add_response(
            "modify_launch_template",
            {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
            {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "2"},
        )
        stub.add_response(
            "delete_launch_template_versions",
            deleted(1),
            {"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
        )
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    assert f"launch template {TEMPLATE}: updated (version 2)" in capsys.readouterr().out


def test_every_item_that_temt_decides_is_compared(ec2: EC2Client, service: Service) -> None:
    """temt が決める項目のどれが変わっても、新しいバージョンになる。"""
    others: list[dict[str, Any]] = [
        echo(ImageId="ami-00000000000000002"),
        echo(InstanceType="t3.small"),
        echo(KeyName="other"),
        echo(InstanceInitiatedShutdownBehavior="stop"),
        echo(CreditSpecification={"CpuCredits": "standard"}),
        echo(
            MetadataOptions={
                "HttpTokens": "optional",
                "InstanceMetadataTags": resources.METADATA_TAGS,
            }
        ),
        echo(
            MetadataOptions={
                "HttpTokens": resources.HTTP_TOKENS,
                "InstanceMetadataTags": "disabled",
            }
        ),
        echo(IamInstanceProfile={"Arn": "arn:aws:iam::123456789012:instance-profile/other"}),
        interface(SubnetId="subnet-00000000000000002"),
        interface(Groups=["sg-00000000000000002"]),
        interface(AssociatePublicIpAddress=False),
        interface(DeviceIndex=1),
        interface(Ipv6AddressCount=1),
        one_tag(Value="other"),
        one_tag(Key="Other"),
        echo(TagSpecifications=[tags("volume"), tags("network-interface")]),
        echo(TagSpecifications=[tags("instance")]),
        # ネットワークインターフェースのタグを付けていなかったテンプレート
        echo(TagSpecifications=[tags("instance"), tags("volume")]),
    ]
    for change in others:
        with Stubber(ec2) as stub:
            stub.add_response(
                "describe_launch_template_versions",
                listed(a_version(change)),
                {"LaunchTemplateName": TEMPLATE},
            )
            template_tags(stub)
            stub.add_response(
                "create_launch_template_version",
                {"LaunchTemplateVersion": {"VersionNumber": 2}},
                {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted()},
            )
            stub.add_response(
                "modify_launch_template",
                {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
                {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "2"},
            )
            stub.add_response(
                "delete_launch_template_versions",
                deleted(1),
                {"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
            )
            align_template(ec2, service)
            # 違いを見落とせば、新しいバージョンを作らずに応答が余る
            stub.assert_no_pending_responses()


def test_the_launch_template_is_detected_without_writing(ec2: EC2Client, service: Service) -> None:
    """足すタグと、違う項目を 1 つ 1 行で <項目>: <今> -> <新> の形で見せる。

    ネットワークインターフェースの項目は番号を、インスタンスに付けるタグは種類を
    添えて書く。今の値が無ければ - と書く。
    """
    current = interface(SubnetId="subnet-00000000000000002")
    current["InstanceType"] = "t3.small"
    current["TagSpecifications"] = [tags("instance"), tags("volume")]
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(current)),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub, None)
        found = resources.detect_template(ec2, service, VALUES, IMAGE, SUBNET, GROUP, ipv6=False)
        stub.assert_no_pending_responses()
    assert found.changes() == [
        Change("~", resources.LAUNCH_TEMPLATE, TEMPLATE, detail)
        for detail in [
            MARK_SHOWN,
            "InstanceType: t3.small -> t3.micro",
            f"NetworkInterfaces.0.SubnetId: subnet-00000000000000002 -> {SUBNET}",
            "TagSpecifications.network-interface: - -> Name=temt-dev-main,temt:service=dev-main",
        ]
    ]


def test_a_missing_launch_template_is_detected_to_be_created(
    ec2: EC2Client, service: Service
) -> None:
    """セキュリティグループがまだ無ければ、NEW_GROUP を指すものとして比べる。"""
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateName=TEMPLATE,
        )
        found = resources.detect_template(ec2, service, VALUES, IMAGE, SUBNET, None, ipv6=False)
        stub.assert_no_pending_responses()
    assert found.changes() == [Change("+", resources.LAUNCH_TEMPLATE, TEMPLATE)]
    assert found.wanted == resources.template_data(
        service, VALUES, IMAGE, SUBNET, resources.NEW_GROUP, ipv6=False
    )


def test_the_launch_template_gets_the_id_of_the_security_group_made_after_detecting(
    ec2: EC2Client, service: Service
) -> None:
    """NEW_GROUP で比べた起動テンプレートは、揃えるときに渡したセキュリティグループの ID で作る。"""
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateName=TEMPLATE,
        )
        found = resources.detect_template(ec2, service, VALUES, IMAGE, SUBNET, None, ipv6=False)
        create_template(stub, wanted())
        resources.template(ec2, service, found, GROUP)
        stub.assert_no_pending_responses()


def test_the_subnet_with_ipv6_gets_an_address(ec2: EC2Client, service: Service) -> None:
    """サブネットに IPv6 の CIDR が結び付いていれば、IPv6 アドレスを 1 つ付ける。"""
    data = wanted(ipv6=True)
    assert data["NetworkInterfaces"][0]["Ipv6AddressCount"] == 1
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateName=TEMPLATE,
        )
        create_template(stub, data)
        align_template(ec2, service, ipv6=True)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("instance_type", "credits"),
    [
        ("t3.micro", "unlimited"),
        ("t4g.small", "unlimited"),
        ("t2.nano", "unlimited"),
        ("m7i.large", None),
        ("c7g.medium", None),
        ("t1.micro", None),
        ("trn1.2xlarge", None),
    ],
)
def test_cpu_credits_go_only_to_the_burstable_types(
    instance_type: str, credits: str | None
) -> None:
    """CPU クレジットは T 系の型でしか意味を持たないので、ほかの型には項目を入れない。

    t1 は CPU クレジットを持たない。trn1 のように t で始まっても、続くのが数字でなければ
    T 系ではない。
    """
    values = Conf(
        KeyName="temt", launch_template=LaunchTemplate(InstanceType=instance_type, SubnetId=SUBNET)
    )
    data = resources.template_data(Service("dev-main"), values, IMAGE, SUBNET, GROUP, ipv6=False)
    expected = None if credits is None else {"CpuCredits": credits}
    assert data.get("CreditSpecification") == expected
    assert resources.projection(data)["CpuCredits"] == credits


def test_cpu_credits_are_dropped_when_the_type_leaves_the_burstable_ones(
    ec2: EC2Client, service: Service
) -> None:
    """T 系から外れると CpuCredits の項目が無くなり、それも違いとして見せる。"""
    values = Conf(
        KeyName="temt", launch_template=LaunchTemplate(InstanceType="m7i.large", SubnetId=SUBNET)
    )
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo())),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub)
        found = resources.detect_template(ec2, service, values, IMAGE, SUBNET, GROUP, ipv6=False)
        stub.assert_no_pending_responses()
    assert found.changed
    assert found.changes() == [
        Change("~", resources.LAUNCH_TEMPLATE, TEMPLATE, detail)
        for detail in ["InstanceType: t3.micro -> m7i.large", "CpuCredits: unlimited -> -"]
    ]


ROOT_DEVICE = "/dev/sda1"


def sized(size: int) -> Conf:
    """VolumeSize を書いた設定。"""
    return Conf(
        KeyName="temt",
        launch_template=LaunchTemplate(InstanceType="t3.micro", SubnetId=SUBNET, VolumeSize=size),
    )


def test_the_volume_size_goes_to_the_root_device() -> None:
    """VolumeSize は、AMI のルートデバイスの大きさだけとして入れる。"""
    data = resources.template_data(
        Service("dev-main"), sized(30), IMAGE, SUBNET, GROUP, ipv6=False, root_device=ROOT_DEVICE
    )
    assert data.get("BlockDeviceMappings") == [
        {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30}}
    ]
    assert resources.projection(data)["BlockDeviceMappings"] == [(ROOT_DEVICE, 30)]


def test_no_volume_size_leaves_the_block_devices_to_the_ami() -> None:
    data = resources.template_data(
        Service("dev-main"), VALUES, IMAGE, SUBNET, GROUP, ipv6=False, root_device=ROOT_DEVICE
    )
    assert "BlockDeviceMappings" not in data


def test_the_volume_size_needs_the_root_device() -> None:
    with pytest.raises(ValueError, match="root_device"):
        _ = resources.template_data(
            Service("dev-main"), sized(30), IMAGE, SUBNET, GROUP, ipv6=False
        )


def detected(ec2: EC2Client, service: Service, values: Conf, data: dict[str, Any]) -> Any:
    """既定バージョンが data のテンプレートと values の差分。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(data)),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub)
        found = resources.detect_template(
            ec2, service, values, IMAGE, SUBNET, GROUP, ipv6=False, root_device=ROOT_DEVICE
        )
        stub.assert_no_pending_responses()
    return found


def test_a_new_volume_size_is_shown_by_its_name(ec2: EC2Client, service: Service) -> None:
    """diff は BlockDeviceMappings を、設定ファイルの項目名 VolumeSize で見せる。"""
    found = detected(ec2, service, sized(30), echo())
    assert found.changed
    assert found.changes() == [
        Change("~", resources.LAUNCH_TEMPLATE, TEMPLATE, "VolumeSize: - -> 30")
    ]


def test_a_removed_volume_size_is_shown_by_its_name(ec2: EC2Client, service: Service) -> None:
    mappings = [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30}}]
    found = detected(ec2, service, VALUES, echo(BlockDeviceMappings=mappings))
    assert found.changes() == [
        Change("~", resources.LAUNCH_TEMPLATE, TEMPLATE, "VolumeSize: 30 -> -")
    ]


def test_the_same_volume_size_is_no_change(ec2: EC2Client, service: Service) -> None:
    mappings = [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30}}]
    found = detected(ec2, service, sized(30), echo(BlockDeviceMappings=mappings))
    assert not found.changed


def test_another_root_device_makes_a_new_version(ec2: EC2Client, service: Service) -> None:
    """デバイス名だけが違っても新しいバージョンを作る。開いても違いは見えない。"""
    mappings = [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 30}}]
    found = detected(ec2, service, sized(30), echo(BlockDeviceMappings=mappings))
    assert found.changed
    assert found.changes() == [Change("~", resources.LAUNCH_TEMPLATE, TEMPLATE, "new version")]


@pytest.mark.parametrize(
    ("mappings", "shown"),
    [
        pytest.param([], None, id="none"),
        pytest.param([{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30}}], 30, id="one"),
        pytest.param(
            [
                {"DeviceName": "/dev/sdb", "Ebs": {"VolumeSize": 100}},
                {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30}},
            ],
            "30,100",
            id="added by hand",
        ),
        pytest.param(
            [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeType": "gp3"}}], "-", id="no size"
        ),
        pytest.param(
            [
                {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 30}},
                {"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeType": "gp3"}},
            ],
            "30,-",
            id="the same device twice",
        ),
    ],
)
def test_volume_size_shows_the_block_devices(mappings: list[dict[str, Any]], shown: object) -> None:
    data = resources.projection(echo(BlockDeviceMappings=mappings))
    assert resources.volume_size(data) == shown


def test_the_other_versions_are_deleted(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """既定を入れ替えたら、残りのバージョンをすべて消す。

    既定バージョンは全バージョンの中から印で見分けるので、応答が 2 ページに
    分かれていても、また既定が先頭になくても見つかる。
    """
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            {
                **listed(a_version(echo(), 1, default=False)),
                "NextToken": "next",
            },
            {"LaunchTemplateName": TEMPLATE},
        )
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(InstanceType="t3.small"), 2)),
            {"LaunchTemplateName": TEMPLATE, "NextToken": "next"},
        )
        template_tags(stub)
        stub.add_response(
            "create_launch_template_version",
            {"LaunchTemplateVersion": {"VersionNumber": 3}},
            {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted()},
        )
        stub.add_response(
            "modify_launch_template",
            {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
            {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "3"},
        )
        stub.add_response(
            "delete_launch_template_versions",
            deleted(1, 2),
            {"LaunchTemplateName": TEMPLATE, "Versions": ["1", "2"]},
        )
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    assert "warning" not in capsys.readouterr().err


def test_leftover_versions_are_deleted_without_a_new_version(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """既定バージョンが揃っていても、既定以外のバージョンが残っていれば消す。

    既定を切り替えた直後に apply が中断すると、こうなる。
    """
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(
                a_version(echo(InstanceType="t3.small"), 2, default=False), a_version(echo(), 3)
            ),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub)
        stub.add_response(
            "delete_launch_template_versions",
            deleted(2),
            {"LaunchTemplateName": TEMPLATE, "Versions": ["2"]},
        )
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    shown = capsys.readouterr().out
    assert f"launch template {TEMPLATE}: updated (version 3)" in shown
    assert "warning" not in shown


def test_a_version_that_cannot_be_deleted_is_a_warning(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """消せなかったバージョンは、番号と理由を添えて知らせ、処理は続ける。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(InstanceType="t3.small"))),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub)
        stub.add_response(
            "create_launch_template_version",
            {"LaunchTemplateVersion": {"VersionNumber": 2}},
            {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted()},
        )
        stub.add_response(
            "modify_launch_template",
            {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
            {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "2"},
        )
        stub.add_response(
            "delete_launch_template_versions",
            {
                "UnsuccessfullyDeletedLaunchTemplateVersions": [
                    {
                        "LaunchTemplateName": TEMPLATE,
                        "VersionNumber": 1,
                        "ResponseError": {
                            "Code": "launchTemplateVersionDoesNotExist",
                            "Message": "no such version",
                        },
                    }
                ]
            },
            {"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
        )
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    captured = capsys.readouterr()
    assert f"launch template {TEMPLATE}: updated (version 2)" in captured.out
    assert captured.err == (
        f"temt: warning: launch template {TEMPLATE}: cannot delete version 1: "
        "launchTemplateVersionDoesNotExist: no such version\n"
    )


def test_a_refused_delete_is_a_warning(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """消す呼び出しそのものを断られても、知らせるだけで apply は続ける。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(InstanceType="t3.small"))),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub)
        stub.add_response(
            "create_launch_template_version",
            {"LaunchTemplateVersion": {"VersionNumber": 2}},
            {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted()},
        )
        stub.add_response(
            "modify_launch_template",
            {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
            {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "2"},
        )
        stub.add_client_error(
            "delete_launch_template_versions",
            "UnauthorizedOperation",
            expected_params={"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
        )
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    captured = capsys.readouterr()
    assert f"launch template {TEMPLATE}: updated (version 2)" in captured.out
    assert captured.err.startswith(
        f"temt: warning: launch template {TEMPLATE}: cannot delete the old versions 1: "
    )


def test_a_template_without_a_default_version(ec2: EC2Client, service: Service) -> None:
    """既定バージョンが 1 つに決まらなければ、検出の段階で止まる。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(),
            {"LaunchTemplateName": TEMPLATE},
        )
        with pytest.raises(TemtError, match="no single default version"):
            _ = resources.detect_template(ec2, service, VALUES, IMAGE, SUBNET, GROUP, ipv6=False)


NEW_IMAGE = "ami-0fedcba9876543210"


def test_point_image_deletes_the_versions_it_read(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """既定バージョンから ImageId だけを替えたものを作って既定にし、読んだものをすべて消す。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(
                a_version({"ImageId": IMAGE}, 1, default=False), a_version({"ImageId": IMAGE}, 2)
            ),
            {"LaunchTemplateName": TEMPLATE},
        )
        stub.add_response(
            "create_launch_template_version",
            {"LaunchTemplateVersion": {"VersionNumber": 3}},
            {
                "LaunchTemplateName": TEMPLATE,
                "SourceVersion": "2",
                "LaunchTemplateData": {"ImageId": NEW_IMAGE},
            },
        )
        stub.add_response(
            "modify_launch_template",
            {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
            {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "3"},
        )
        stub.add_response(
            "delete_launch_template_versions",
            {
                "SuccessfullyDeletedLaunchTemplateVersions": [
                    {"VersionNumber": 1},
                    {"VersionNumber": 2},
                ]
            },
            {"LaunchTemplateName": TEMPLATE, "Versions": ["1", "2"]},
        )
        resources.point_image(ec2, service, NEW_IMAGE)
        stub.assert_no_pending_responses()
    assert f"launch template {TEMPLATE}: updated (version 3, {NEW_IMAGE})" in (
        capsys.readouterr().out
    )


def test_point_image_does_not_claim_the_instance_profile_exists(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """bake はプロファイルを確かめないので、プロファイルが無いという警告に何も添えない。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version({"ImageId": IMAGE}, 1)),
            {"LaunchTemplateName": TEMPLATE},
        )
        stub.add_response(
            "create_launch_template_version",
            {
                "LaunchTemplateVersion": {"VersionNumber": 2},
                "Warning": {
                    "Errors": [
                        {
                            "Code": resources.PROFILE_NOT_READY,
                            "Message": f"The IamInstanceProfile.Name '{NAME}' does not exist.",
                        }
                    ]
                },
            },
            {
                "LaunchTemplateName": TEMPLATE,
                "SourceVersion": "1",
                "LaunchTemplateData": {"ImageId": NEW_IMAGE},
            },
        )
        stub.add_response(
            "modify_launch_template",
            {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
            {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "2"},
        )
        stub.add_response(
            "delete_launch_template_versions",
            {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": 1}]},
            {"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
        )
        resources.point_image(ec2, service, NEW_IMAGE)
        stub.assert_no_pending_responses()
    shown = capsys.readouterr().err
    assert f"{resources.PROFILE_NOT_READY}: The IamInstanceProfile.Name '{NAME}'" in shown
    assert resources.NOT_READY_NOTE not in shown


def test_point_image_stops_without_a_default_version(ec2: EC2Client, service: Service) -> None:
    """既定バージョンが 1 つに決まらなければ、新しいバージョンを作らずに止まる。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version({"ImageId": IMAGE}, 1, default=False)),
            {"LaunchTemplateName": TEMPLATE},
        )
        with pytest.raises(TemtError, match="no single default version"):
            resources.point_image(ec2, service, NEW_IMAGE)
        stub.assert_no_pending_responses()


def nothing_there(iam_stub: Stubber, ec2_stub: Stubber) -> None:
    """ロール、インラインポリシー temt-jupyter、インスタンスプロファイル、
    セキュリティグループ、起動テンプレートが無いときに、detect が読む応答を積む。
    """
    missing(iam_stub, "get_role", "NoSuchEntity", RoleName=NAME)
    missing(iam_stub, "get_role_policy", "NoSuchEntity", **THE_POLICY)
    missing(iam_stub, "list_role_policies", "NoSuchEntity", RoleName=NAME)
    missing(iam_stub, "get_instance_profile", "NoSuchEntity", InstanceProfileName=NAME)
    groups(ec2_stub)
    missing(
        ec2_stub,
        "describe_launch_template_versions",
        resources.NO_SUCH_TEMPLATE,
        LaunchTemplateName=TEMPLATE,
    )


def test_update_goes_through_them_in_order(
    ec2: EC2Client,
    iam: IAMClient,
    ssm: SSMClient,
    service: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """設計が決めた順に作る。関連付けは名前で見つからないので作り、実行を求める。

    Stubber の待ち行列はクライアントごとなので、それだけではサービスをまたいだ
    順序を確かめられない。3 つのクライアントの呼び出しを 1 つの並びに記録する。
    """
    values = with_ingress(Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"))
    calls = recorded(ec2, iam, ssm)
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub, Stubber(ssm) as ssm_stub:
        nothing_there(iam_stub, ec2_stub)
        listed_associations(ssm_stub)
        found = resources.detect(
            ec2,
            iam,
            ssm,
            service,
            values,
            IMAGE,
            SUBNET,
            VPC,
            ipv6=False,
            policies={JUPYTER: CONTENT},
        )
        calls.clear()
        iam_stub.add_response(
            "create_role",
            a_role(),
            {"RoleName": NAME, "AssumeRolePolicyDocument": resources.TRUST_POLICY, "Tags": MARK},
        )
        iam_stub.add_response(
            "put_role_policy", {}, {**THE_POLICY, "PolicyDocument": json.dumps(CONTENT)}
        )
        iam_stub.add_response(
            "create_instance_profile", a_profile([]), {"InstanceProfileName": NAME, "Tags": MARK}
        )
        iam_stub.add_response(
            "get_instance_profile", ok(a_profile([])), {"InstanceProfileName": NAME}
        )
        iam_stub.add_response(
            "add_role_to_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
        )
        created(ec2_stub)
        ec2_stub.add_response(
            "authorize_security_group_ingress",
            {"Return": True},
            {"GroupId": GROUP, "IpPermissions": [permission(22, 22, "203.0.113.10/32", "home")]},
        )
        create_template(ec2_stub, wanted())
        ssm_stub.add_response(
            "create_association",
            {"AssociationDescription": {"AssociationId": ASSOCIATION}},
            {**association_content([22]), "AssociationName": SSHD, "Tags": MARK},
        )
        association_started(ssm_stub)
        resources.update(ec2, iam, ssm, service, values, found)
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
        ssm_stub.assert_no_pending_responses()
    assert calls == [
        "CreateRole",
        "PutRolePolicy",
        "CreateInstanceProfile",
        # waiter instance_profile_exists が読む
        "GetInstanceProfile",
        "AddRoleToInstanceProfile",
        "CreateSecurityGroup",
        "AuthorizeSecurityGroupIngress",
        "CreateLaunchTemplate",
        "CreateAssociation",
        "StartAssociationsOnce",
    ]
    assert f"association {SSHD}: created\n" in capsys.readouterr().out


def test_update_puts_the_created_security_group_into_the_launch_template(
    ec2: EC2Client,
    iam: IAMClient,
    ssm: SSMClient,
    service: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """セキュリティグループだけが無ければ、作ってから、その ID で起動テンプレートの
    新しいバージョンを作る。

    検出したときは、起動テンプレートを NEW_GROUP で比べている。変えなかったものは
    知らせない。関連付けは揃っているので、読むだけで、実行も求めない。
    """
    old_group = "sg-00000000000000002"
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub, Stubber(ssm) as ssm_stub:
        iam_in_place(iam_stub)
        groups(ec2_stub)
        ec2_stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(interface(Groups=[old_group]))),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(ec2_stub)
        association_in_place(ssm_stub)
        found = resources.detect(
            ec2, iam, ssm, service, VALUES, IMAGE, SUBNET, VPC, ipv6=False, policies={}
        )
        created(ec2_stub)
        ec2_stub.add_response(
            "create_launch_template_version",
            {"LaunchTemplateVersion": {"VersionNumber": 2}},
            {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": wanted()},
        )
        ec2_stub.add_response(
            "modify_launch_template",
            {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
            {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "2"},
        )
        ec2_stub.add_response(
            "delete_launch_template_versions",
            deleted(1),
            {"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
        )
        resources.update(ec2, iam, ssm, service, VALUES, found)
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
        ssm_stub.assert_no_pending_responses()
    assert found.template.changes() == [
        Change(
            "~",
            resources.LAUNCH_TEMPLATE,
            TEMPLATE,
            f'NetworkInterfaces.0.Groups: ["{old_group}"] -> ["{resources.NEW_GROUP}"]',
        )
    ]
    assert [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()] == [
        f"security group {NAME}: created ({GROUP})",
        f"launch template {TEMPLATE}: updated (version 2)",
    ]


def test_detect_reads_them_in_the_order_of_update_and_finds_nothing(
    ec2: EC2Client, iam: IAMClient, ssm: SSMClient, service: Service
) -> None:
    """揃っていれば、update が揃える順に読むだけで、変更は空になる。"""
    calls = recorded(ec2, iam, ssm)
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub, Stubber(ssm) as ssm_stub:
        iam_in_place(iam_stub)
        groups(ec2_stub, a_group())
        rules(ec2_stub)
        template_in_place(ec2_stub)
        association_in_place(ssm_stub)
        found = resources.detect(
            ec2, iam, ssm, service, VALUES, IMAGE, SUBNET, VPC, ipv6=False, policies={}
        )
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
        ssm_stub.assert_no_pending_responses()
    assert found.changes() == []
    assert calls == READS


def test_detect_shows_what_update_would_create_in_its_order(
    ec2: EC2Client, iam: IAMClient, ssm: SSMClient, service: Service
) -> None:
    """何も無ければ、ロール、インラインポリシー、インスタンスプロファイル、セキュリティグループと
    その受信規則、起動テンプレート、関連付けを、作るものとして並べる。何も書かない。

    関連付けは、名前で見つからなければ作るものとする。
    """
    values = with_ingress(Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"))
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub, Stubber(ssm) as ssm_stub:
        nothing_there(iam_stub, ec2_stub)
        listed_associations(ssm_stub)
        found = resources.detect(
            ec2,
            iam,
            ssm,
            service,
            values,
            IMAGE,
            SUBNET,
            VPC,
            ipv6=False,
            policies={JUPYTER: CONTENT},
        )
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
        ssm_stub.assert_no_pending_responses()
    assert [(one.mark, one.kind, one.name, one.detail) for one in found.changes()] == [
        ("+", resources.ROLE, NAME, ""),
        ("+", resources.ROLE_POLICY, JUPYTER, ""),
        ("+", resources.INSTANCE_PROFILE, NAME, ""),
        ("+", resources.SECURITY_GROUP, NAME, f"in {VPC}"),
        ("+", resources.INGRESS, "22/tcp", "203.0.113.10/32   home"),
        ("+", resources.LAUNCH_TEMPLATE, TEMPLATE, ""),
        ("+", resources.ASSOCIATION, SSHD, "SshdPorts: 22"),
    ]


def test_detect_compares_the_template_with_the_group_to_be_created(
    ec2: EC2Client, iam: IAMClient, ssm: SSMClient, service: Service
) -> None:
    """セキュリティグループがまだ無ければ、起動テンプレートは NEW_GROUP を指すものとして比べる。"""
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub, Stubber(ssm) as ssm_stub:
        iam_in_place(iam_stub)
        groups(ec2_stub)
        template_in_place(ec2_stub)
        association_in_place(ssm_stub)
        found = resources.detect(
            ec2, iam, ssm, service, VALUES, IMAGE, SUBNET, VPC, ipv6=False, policies={}
        )
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
        ssm_stub.assert_no_pending_responses()
    assert found.changes() == [
        Change("+", resources.SECURITY_GROUP, NAME, f"in {VPC}"),
        Change(
            "~",
            resources.LAUNCH_TEMPLATE,
            TEMPLATE,
            f'NetworkInterfaces.0.Groups: ["{GROUP}"] -> ["{resources.NEW_GROUP}"]',
        ),
    ]


def test_the_changes_of_what_was_found_come_in_the_order_of_update() -> None:
    """ロール、インラインポリシー、インスタンスプロファイル、セキュリティグループとその
    受信規則、起動テンプレート、関連付けの順に並べる。インラインポリシーは検出した順である。
    """
    service = Service("dev-main")
    idle = policy.policy_name("idle")
    mark = (("temt:service", "dev-main"),)
    association = {
        "Name": sshd.DOCUMENT,
        "Targets": sshd.targets(service),
        "Parameters": {sshd.COMMANDS: sshd.commands([22], "dev-main")},
    }
    found = resources.Found(
        role=resources.RoleDiff(NAME, exists=True, tags=mark),
        policies=(
            resources.PolicyDiff(JUPYTER, None, CONTENT),
            resources.PolicyDiff(idle, CONTENT, None),
        ),
        profile=resources.ProfileDiff(NAME, exists=True, roles=("other",)),
        group=resources.GroupDiff(
            NAME,
            VPC,
            GROUP,
            resources.Changes([resources.Rule("tcp", 22, 22, "203.0.113.10/32")], [], []),
            mark,
        ),
        template=resources.TemplateDiff(
            TEMPLATE, resources.template_data(service, VALUES, IMAGE, SUBNET, GROUP, ipv6=False)
        ),
        association=resources.AssociationDiff(SSHD, (22,), association),
    )
    assert [(one.mark, one.kind, one.name) for one in found.changes()] == [
        ("~", resources.ROLE, NAME),
        ("+", resources.ROLE_POLICY, JUPYTER),
        ("-", resources.ROLE_POLICY, idle),
        ("~", resources.INSTANCE_PROFILE, NAME),
        ("~", resources.SECURITY_GROUP, NAME),
        ("+", resources.INGRESS, "22/tcp"),
        ("+", resources.LAUNCH_TEMPLATE, TEMPLATE),
        ("+", resources.ASSOCIATION, SSHD),
    ]


def test_the_association_is_detected_without_writing(ssm: SSMClient, service: Service) -> None:
    """SshdPorts の違いを今と新しいポートで見せ、スクリプトの違いを続く行に出す。

    足すタグは別の行に見せる。
    """
    with Stubber(ssm) as stub:
        an_association(stub, [22], [])
        found = resources.detect_association(ssm, service, Conf(SshdPorts=[2222, 443]))
        stub.assert_no_pending_responses()
    ported, tagged = found.changes()
    assert (ported.mark, ported.kind, ported.name, ported.detail) == (
        "~",
        resources.ASSOCIATION,
        SSHD,
        "SshdPorts: 22 -> 2222,443",
    )
    assert ported.body[0].startswith("@@ ")
    assert '-want="22"' in ported.body
    assert '+want="2222 443"' in ported.body
    assert tagged == Change("~", resources.ASSOCIATION, SSHD, MARK_SHOWN)


def test_the_association_is_found_by_its_name(ssm: SSMClient, service: Service) -> None:
    """名前 <名前>-sshd で探し、見つけたものの中身とタグを読む。揃っていれば差分は空。"""
    calls = recorded(ssm)
    with Stubber(ssm) as stub:
        association_in_place(stub)
        found = resources.detect_association(ssm, service, VALUES)
        stub.assert_no_pending_responses()
    assert calls == ["ListAssociations", "DescribeAssociation", "ListTagsForResource"]
    assert found.association_id == ASSOCIATION
    assert found.current is not None
    assert found.changes() == []


def test_an_association_that_is_not_there_is_to_be_created(
    ssm: SSMClient, service: Service
) -> None:
    """名前で見つからなければ、作るものとする。中身もタグも読まない。"""
    calls = recorded(ssm)
    with Stubber(ssm) as stub:
        listed_associations(stub)
        found = resources.detect_association(ssm, service, Conf())
        stub.assert_no_pending_responses()
    assert calls == ["ListAssociations"]
    assert (found.association_id, found.current) == ("", None)
    assert found.changes() == [Change("+", resources.ASSOCIATION, SSHD, "SshdPorts: 22")]


def test_the_first_of_two_associations_with_the_name_is_compared(
    ssm: SSMClient, service: Service
) -> None:
    """同じ名前の関連付けが 2 つ以上あれば（手で作ったときなど）、最初の 1 つだけを読んで
    比べる。ほかのものは読まない。
    """
    with Stubber(ssm) as stub:
        an_association(stub, [2222], MARK, OTHER_ASSOCIATION)
        found = resources.detect_association(ssm, service, Conf(SshdPorts=[2222]))
        stub.assert_no_pending_responses()
    assert found.association_id == ASSOCIATION
    assert found.changes() == []


def test_the_security_group_is_in_this_vpc_and_another(ec2: EC2Client, service: Service) -> None:
    """このサービスのものは、SubnetId の VPC にあるほうである。"""
    with Stubber(ec2) as stub:
        groups(
            stub,
            a_group(group_id="sg-00000000000000002", vpc_id="vpc-other"),
            a_group(),
        )
        rules(stub)
        assert align_group(ec2, service) == GROUP
        stub.assert_no_pending_responses()


def test_a_warning_from_aws_is_shown(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """AWS は作るときに中身を最後まで検査せず、問題を Warning で返す。"""
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateName=TEMPLATE,
        )
        create_template(
            stub,
            wanted(),
            Warning={"Errors": [{"Code": "InvalidType", "Message": "no such type"}]},
        )
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    shown = capsys.readouterr().err
    assert "InvalidType: no such type" in shown
    # 反映待ちの断りは、その誤りのときだけ添える
    assert resources.NOT_READY_NOTE not in shown


def test_the_instance_profile_warning_says_it_is_iam_propagation(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """作った直後は EC2 がインスタンスプロファイルを見つけられないことがある。

    手順 6 はその前にインスタンスプロファイルを揃えているので、これは反映待ちである。
    """
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateName=TEMPLATE,
        )
        create_template(
            stub,
            wanted(),
            Warning={
                "Errors": [
                    {
                        "Code": resources.PROFILE_NOT_READY,
                        "Message": f"The IamInstanceProfile.Name '{NAME}' does not exist.",
                    }
                ]
            },
        )
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    shown = capsys.readouterr().err
    assert f"{resources.PROFILE_NOT_READY}: The IamInstanceProfile.Name '{NAME}'" in shown
    assert resources.NOT_READY_NOTE in shown


def test_an_instance_profile_arn_with_a_path(ec2: EC2Client, service: Service) -> None:
    """ARN にパスが入っていても、名前で比べる。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(
                a_version(
                    echo(
                        IamInstanceProfile={
                            "Arn": f"arn:aws:iam::123456789012:instance-profile/team/{NAME}"
                        }
                    )
                )
            ),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub)
        align_template(ec2, service)
        stub.assert_no_pending_responses()


def test_other_errors_from_deleting_the_template_are_left_to_destroy(
    ec2: EC2Client, service: Service
) -> None:
    """無いことを表す誤りだけをここで扱う。他はそのまま上げる。"""
    with Stubber(ec2) as stub:
        stub.add_client_error(
            "delete_launch_template",
            service_error_code="UnauthorizedOperation",
            http_status_code=403,
        )
        with pytest.raises(ClientError):
            resources.delete_template(ec2, service)


def test_other_errors_from_deleting_the_security_group_are_left_to_destroy(
    ec2: EC2Client, service: Service
) -> None:
    with Stubber(ec2) as stub:
        groups(stub, a_group())
        stub.add_client_error(
            "delete_security_group",
            service_error_code="UnauthorizedOperation",
            http_status_code=403,
        )
        with pytest.raises(ClientError):
            resources.delete_security_groups(ec2, service)


def test_every_group_with_the_name_is_deleted(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """SubnetId を別の VPC に変えたあと古い VPC に残ったものも消す。

    AWS は並びを保証しないので、ID の順に消す。
    """
    other = "sg-00000000000000002"
    with Stubber(ec2) as stub:
        groups(stub, a_group(), a_group(group_id=other, vpc_id="vpc-other"))
        stub.add_response("delete_security_group", {"Return": True}, {"GroupId": other})
        stub.add_response("delete_security_group", {"Return": True}, {"GroupId": GROUP})
        resources.delete_security_groups(ec2, service)
        stub.assert_no_pending_responses()
    shown = capsys.readouterr().out
    assert f"security group {NAME} ({GROUP}): {resources.DELETED}" in shown
    assert f"security group {NAME} ({other}): {resources.DELETED}" in shown


def test_no_group_to_delete(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(ec2) as stub:
        groups(stub)
        resources.delete_security_groups(ec2, service)
        stub.assert_no_pending_responses()
    assert f"security group {NAME}: {resources.ABSENT}" in capsys.readouterr().out


def test_other_errors_from_reading_the_instance_profile_are_left_to_destroy(
    iam: IAMClient, service: Service
) -> None:
    with Stubber(iam) as stub:
        stub.add_client_error(
            "get_instance_profile", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(ClientError):
            resources.delete_instance_profile(iam, service)


def test_other_errors_from_deleting_the_instance_profile_are_left_to_destroy(
    iam: IAMClient, service: Service
) -> None:
    with Stubber(iam) as stub:
        stub.add_response("get_instance_profile", a_profile([]), {"InstanceProfileName": NAME})
        stub.add_client_error(
            "delete_instance_profile", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(ClientError):
            resources.delete_instance_profile(iam, service)


def unheld(stub: Stubber) -> None:
    """delete_role が先に読む、ロールを消せなくするもの。何も無い。"""
    stub.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
    stub.add_response(
        "list_instance_profiles_for_role", {"InstanceProfiles": []}, {"RoleName": NAME}
    )


@pytest.mark.parametrize("method", ["list_role_policies", "delete_role_policy"])
def test_other_errors_from_the_inline_policies_are_left_to_destroy(
    iam: IAMClient, service: Service, method: str
) -> None:
    with Stubber(iam) as stub:
        unheld(stub)
        if method == "delete_role_policy":
            stub.add_response(
                "list_role_policies", {"PolicyNames": ["temt-jupyter"]}, {"RoleName": NAME}
            )
        stub.add_client_error(method, service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(ClientError):
            resources.delete_role(iam, service)


def test_other_errors_from_deleting_the_role_are_left_to_destroy(
    iam: IAMClient, service: Service
) -> None:
    with Stubber(iam) as stub:
        unheld(stub)
        stub.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
        stub.add_client_error(
            "delete_role", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(ClientError):
            resources.delete_role(iam, service)


@pytest.mark.parametrize(
    "method", ["list_attached_role_policies", "list_instance_profiles_for_role"]
)
def test_other_errors_from_reading_what_holds_the_role_are_left_to_destroy(
    iam: IAMClient, service: Service, method: str
) -> None:
    with Stubber(iam) as stub:
        if method == "list_instance_profiles_for_role":
            stub.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
        stub.add_client_error(method, service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(ClientError):
            resources.delete_role(iam, service)


def test_a_role_that_gets_held_after_the_check_is_reported(
    iam: IAMClient, service: Service
) -> None:
    """確かめた後に付けられたときも、外したことが IAM にまだ映らないときも DeleteConflict に
    なる。どちらか分からないので、AWS の文を添えて両方の直し方を告げる。"""
    with Stubber(iam) as stub:
        unheld(stub)
        stub.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
        stub.add_client_error(
            "delete_role",
            service_error_code=resources.DELETE_CONFLICT,
            service_message="Cannot delete entity, must remove roles from instance profile first.",
            http_status_code=409,
        )
        with pytest.raises(TemtError) as caught:
            resources.delete_role(iam, service)
    assert str(caught.value) == (
        f"cannot delete the role {NAME}: An error occurred (DeleteConflict) when calling the "
        "DeleteRole operation: Cannot delete entity, must remove roles from instance profile "
        "first.; if nothing is attached to it, IAM may not show the change yet, so run destroy "
        "again; otherwise detach it by hand and run destroy again"
    )


def names(count: int) -> list[str]:
    """/<名前> の下にある SSM パラメータの名前を count 個。"""
    return [f"{PATH}/app/key{number:02d}" for number in range(count)]


def parameters_under(stub: Stubber, found: list[str], *, token: str = "", then: str = "") -> None:
    """get-parameters-by-path の 1 ページ分を積む。

    token はこのページを読むのに渡す NextToken、then は次のページがあるときの NextToken。
    """
    response: dict[str, Any] = {"Parameters": [{"Name": name} for name in found]}
    if then:
        response["NextToken"] = then
    params: dict[str, Any] = {"Path": PATH, "Recursive": True}
    if token:
        params["NextToken"] = token
    stub.add_response("get_parameters_by_path", response, params)


def test_no_parameter_to_delete(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """/<名前> の下に 1 つも無ければ、delete-parameters は呼ばない。"""
    with Stubber(ssm) as stub:
        parameters_under(stub, [])
        resources.delete_parameters(ssm, service)
        stub.assert_no_pending_responses()
    assert f"parameters under {PATH}/: {resources.ABSENT}" in capsys.readouterr().out


def test_the_parameters_are_deleted_by_path(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """タグではなくパスで探す。アプリの値はタグを持たないことがある。"""
    found = names(3)
    with Stubber(ssm) as stub:
        parameters_under(stub, found)
        stub.add_response("delete_parameters", {"DeletedParameters": found}, {"Names": found})
        resources.delete_parameters(ssm, service)
        stub.assert_no_pending_responses()
    assert f"parameters under {PATH}/: {resources.DELETED} (3)" in capsys.readouterr().out


def test_the_parameters_are_deleted_ten_at_a_time(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """delete-parameters は 1 回に 10 個までしか受け付けないので、分けて消す。"""
    found = names(11)
    with Stubber(ssm) as stub:
        parameters_under(stub, found)
        stub.add_response(
            "delete_parameters", {"DeletedParameters": found[:10]}, {"Names": found[:10]}
        )
        stub.add_response(
            "delete_parameters", {"DeletedParameters": found[10:]}, {"Names": found[10:]}
        )
        resources.delete_parameters(ssm, service)
        stub.assert_no_pending_responses()
    assert resources.PARAMETER_BATCH == 10
    assert f"parameters under {PATH}/: {resources.DELETED} (11)" in capsys.readouterr().out


def test_every_page_of_parameters_is_read_before_deleting(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ページを繰ってすべて集めてから、10 個ずつに分けて消す。"""
    found = names(12)
    with Stubber(ssm) as stub:
        parameters_under(stub, found[:4], then="page2")
        parameters_under(stub, found[4:], token="page2")
        stub.add_response(
            "delete_parameters", {"DeletedParameters": found[:10]}, {"Names": found[:10]}
        )
        stub.add_response(
            "delete_parameters", {"DeletedParameters": found[10:]}, {"Names": found[10:]}
        )
        resources.delete_parameters(ssm, service)
        stub.assert_no_pending_responses()
    assert f"parameters under {PATH}/: {resources.DELETED} (12)" in capsys.readouterr().out


def test_a_parameter_that_went_away_in_between_is_not_an_error(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """読んでから消すまでに消えたものは InvalidParameters に返る。

    消えていればよいので誤りにはしないが、消した数には入れない。知らせる数は
    DeletedParameters の数である。
    """
    found = names(2)
    with Stubber(ssm) as stub:
        parameters_under(stub, found)
        stub.add_response(
            "delete_parameters",
            {"DeletedParameters": found[:1], "InvalidParameters": found[1:]},
            {"Names": found},
        )
        resources.delete_parameters(ssm, service)
        stub.assert_no_pending_responses()
    assert f"parameters under {PATH}/: {resources.DELETED} (1)" in capsys.readouterr().out


def test_other_errors_from_reading_the_parameters_are_left_to_destroy(
    ssm: SSMClient, service: Service
) -> None:
    with Stubber(ssm) as stub:
        stub.add_client_error(
            "get_parameters_by_path",
            service_error_code="AccessDeniedException",
            http_status_code=400,
        )
        with pytest.raises(ClientError):
            resources.delete_parameters(ssm, service)


def test_template_changes_show_a_new_version_when_no_item_differs() -> None:
    """同じ種類の TagSpecifications が 2 つあると、開いた項目では違いが見えない。
    それでも apply は新しいバージョンを作るので、そのことを出す。"""
    wanted = resources.template_data(Service("dev-main"), Conf(), IMAGE, SUBNET, GROUP, ipv6=False)
    current = dict(wanted)
    current["TagSpecifications"] = [
        *wanted.get("TagSpecifications", []),
        {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "temt-dev-main"}]},
    ]
    found = resources.TemplateDiff(TEMPLATE, wanted, current, 1, (1,))
    assert found.changed
    assert found.changes() == [Change("~", "launch-template", TEMPLATE, "new version")]


def test_template_changes_show_leftover_versions() -> None:
    """中身が同じでも、既定以外のバージョンが残っていれば、消すことを出す。"""
    wanted = resources.template_data(Service("dev-main"), Conf(), IMAGE, SUBNET, GROUP, ipv6=False)
    found = resources.TemplateDiff(TEMPLATE, wanted, dict(wanted), 3, (1, 2, 3))
    assert found.changes() == [
        Change("~", "launch-template", TEMPLATE, "delete other versions: 1, 2")
    ]


def test_template_changes_show_versions_newer_than_the_default() -> None:
    """既定より新しいバージョンも、既定以外として消す。"""
    wanted = resources.template_data(Service("dev-main"), Conf(), IMAGE, SUBNET, GROUP, ipv6=False)
    found = resources.TemplateDiff(TEMPLATE, wanted, dict(wanted), 3, (2, 3, 4))
    assert found.leftovers == (2, 4)


def test_template_changes_leave_leftover_versions_to_the_new_version() -> None:
    """新しいバージョンを作るときは、既定以外を消すことを別に出さない。"""
    wanted = resources.template_data(Service("dev-main"), Conf(), IMAGE, SUBNET, GROUP, ipv6=False)
    current = {**wanted, "InstanceType": "t3.small"}
    found = resources.TemplateDiff(TEMPLATE, wanted, current, 3, (2, 3))
    assert found.leftovers == ()
    assert [one.detail for one in found.changes()] == [
        f"InstanceType: t3.small -> {wanted.get('InstanceType')}"
    ]


def test_association_changes_show_a_script_that_temt_changed() -> None:
    """ポートが同じでスクリプトだけが違えば、script と書き、違いを続く行に出す。"""
    wanted = {
        "Name": sshd.DOCUMENT,
        "Targets": [{"Key": "tag:Name", "Values": [NAME]}],
        "Parameters": {sshd.COMMANDS: sshd.commands([22], "dev-main")},
    }
    current = {**wanted, "Parameters": {sshd.COMMANDS: ["set -u", 'want="22"', "old line"]}}
    found = resources.AssociationDiff(
        SSHD, (22,), wanted, association_id=ASSOCIATION, current=current
    )
    [line] = found.changes()
    assert (line.mark, line.kind, line.detail) == ("~", "association", "script")
    assert "-old line" in line.body


def test_old_versions_are_deleted_in_batches_aws_accepts(
    ec2: EC2Client, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 回に渡せる数を超えれば分けて頼み、断られた回があっても残りを続ける。"""
    numbers = [str(one) for one in range(2, resources.VERSIONS_PER_CALL + 3)]
    first, rest = numbers[: resources.VERSIONS_PER_CALL], numbers[resources.VERSIONS_PER_CALL :]
    with Stubber(ec2) as stub:
        stub.add_client_error(
            "delete_launch_template_versions",
            service_error_code="UnauthorizedOperation",
            service_message="not allowed",
            expected_params={"LaunchTemplateName": "temt-a", "Versions": first},
        )
        stub.add_response(
            "delete_launch_template_versions",
            {"SuccessfullyDeletedLaunchTemplateVersions": []},
            {"LaunchTemplateName": "temt-a", "Versions": rest},
        )
        resources._delete_versions(ec2, "temt-a", numbers)
        stub.assert_no_pending_responses()
    err = capsys.readouterr().err
    assert err.count("cannot delete the old versions") == 1
    assert "not allowed" in err
