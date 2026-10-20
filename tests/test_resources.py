"""サービスの AWS リソースを揃える処理のテスト。AWS は Stubber で差し替える。"""

import base64
import copy
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from types_boto3_ec2.client import EC2Client
from types_boto3_iam.client import IAMClient
from types_boto3_ssm.client import SSMClient

from temt import ltbuild, policy, resources
from temt.change import Change
from temt.conf import Conf, Ingress, Role, SecurityGroup
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
REGION = "ap-northeast-1"

# temt が管理していることを示すタグ。4 つに付ける
MARK: list[dict[str, str]] = [{"Key": "temt:service", "Value": "dev-main"}]
# インスタンスロールに付けるタグ。temt のタグと、このリージョンのタグ（値は空）
HERE = f"temt:region:{REGION}"
ROLE_MARK: list[dict[str, str]] = [*MARK, {"Key": HERE, "Value": ""}]
# ほかのリージョンのタグのキー
THERE = "temt:region:us-west-2"
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

# [[role.jupyter]] から作った temt-jupyter の中身
STATEMENT: dict[str, Any] = {
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::bucket/temt-dev-main/*",
}
CONTENT: dict[str, Any] = {"Version": policy.VERSION, "Statement": [STATEMENT]}
JUPYTER = policy.policy_name("jupyter")
THE_POLICY = {"RoleName": NAME, "PolicyName": JUPYTER}
# 管理ポリシーの ARN
S3_READ = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
SSM_CORE = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
# 足した temt のタグの、diff での表示
MARK_SHOWN = "tags: temt:service=dev-main"

# 受信規則もポリシーも無い設定
VALUES = Conf(AwsRegion=REGION)

# 揃っているときに、detect が AWS を読む順。update が揃える順と同じである。インライン
# ポリシーは無い
READS = [
    "GetRole",
    "ListRolePolicies",
    "ListAttachedRolePolicies",
    "GetInstanceProfile",
    "DescribeSecurityGroups",
    "DescribeSecurityGroupRules",
    "DescribeLaunchTemplateVersions",
    "DescribeLaunchTemplates",
]


def a_role(tags: list[dict[str, str]] | None = ROLE_MARK) -> dict[str, Any]:
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


def tags(resource_type: str) -> dict[str, Any]:
    """1 つのリソース種別ぶんのタグ仕様。"""
    return {"ResourceType": resource_type, "Tags": TAGS}


# 起動テンプレートに入れる中身。ltbuild.build が設定ファイルから組み立てたものに当たる
WANTED: dict[str, Any] = {
    "ImageId": IMAGE,
    "InstanceType": "t3.micro",
    "InstanceInitiatedShutdownBehavior": "terminate",
    "IamInstanceProfile": {"Name": NAME},
    "NetworkInterfaces": [
        {"DeviceIndex": 0, "SubnetId": SUBNET, "AssociatePublicIpAddress": True, "Groups": [GROUP]}
    ],
    "MetadataOptions": {"HttpTokens": "required", "InstanceMetadataTags": "enabled"},
    "CreditSpecification": {"CpuCredits": "unlimited"},
    "TagSpecifications": [tags("instance"), tags("volume"), tags("network-interface")],
}


def wanted(**changes: Any) -> dict[str, Any]:
    """WANTED の写し。changes で最上位の項目を足すか替える。"""
    return {**copy.deepcopy(WANTED), **changes}


def echo(**changes: Any) -> dict[str, Any]:
    """WANTED を、AWS が読み出しで返す形にしたもの。changes で最上位の項目を足すか替える。

    配列の並びを変え、IamInstanceProfile を ARN で返し、空の配列と、読み出したときにだけ
    ある MetadataOptions.State を混ぜる。どれも違いと見なさない。
    """
    data = wanted()
    data["IamInstanceProfile"] = {"Arn": f"arn:aws:iam::123456789012:instance-profile/{NAME}"}
    data["MetadataOptions"] = {**data["MetadataOptions"], "State": "applied"}
    data["TagSpecifications"] = [
        {"ResourceType": one["ResourceType"], "Tags": list(reversed(one["Tags"]))}
        for one in reversed(data["TagSpecifications"])
    ]
    data["SecurityGroupIds"] = []
    return {**data, **changes}


def without(key: str) -> dict[str, Any]:
    """echo() から、最上位の項目 key を除いたもの。"""
    data = echo()
    del data[key]
    return data


def with_ingress(*ingress: Ingress) -> Conf:
    """受信規則を定めた設定。"""
    return Conf(security_group=SecurityGroup(ingress=list(ingress)))


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


def new_version(stub: Stubber, data: dict[str, Any], number: int = 2, *old: int) -> None:
    """中身 data の新しいバージョン number を作って既定にし、old（省けば 1）を消す応答を積む。"""
    stub.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": number}},
        {"LaunchTemplateName": TEMPLATE, "LaunchTemplateData": data},
    )
    stub.add_response(
        "modify_launch_template",
        {"LaunchTemplate": {"LaunchTemplateName": TEMPLATE}},
        {"LaunchTemplateName": TEMPLATE, "DefaultVersion": str(number)},
    )
    gone = old or (1,)
    stub.add_response(
        "delete_launch_template_versions",
        deleted(*gone),
        {"LaunchTemplateName": TEMPLATE, "Versions": [str(one) for one in gone]},
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
    resources.role(iam, service, resources.detect_role(iam, service, REGION))


def align_profile(iam: IAMClient, service: Service) -> None:
    """apply と同じく、インスタンスプロファイルの差分を検出してから、その差分で揃える。"""
    resources.instance_profile(iam, service, resources.detect_instance_profile(iam, service))


def align_group(ec2: EC2Client, service: Service, values: Conf = VALUES) -> str:
    """apply と同じく、セキュリティグループの差分を検出してから、その差分で揃える。"""
    found = resources.detect_security_group(ec2, service, values, VPC)
    return resources.security_group(ec2, service, found)


def align_template(
    ec2: EC2Client,
    service: Service,
    data: dict[str, Any] | None = None,
    *,
    group_id: str | None = None,
    managed: bool = False,
) -> None:
    """apply と同じく、起動テンプレートの差分を検出してから、その差分で揃える。

    data は揃える中身で、省けば wanted()。group_id は揃えたセキュリティグループの ID、
    managed は temt のインスタンスプロファイルを揃えたか。
    """
    found = resources.detect_template(ec2, service, wanted() if data is None else data)
    resources.template(ec2, service, found, group_id, managed=managed)


def iam_in_place(stub: Stubber) -> None:
    """ロールとインスタンスプロファイルが揃っていて、インラインポリシーも管理ポリシーも
    無いときに読む応答を積む。
    """
    stub.add_response("get_role", a_role(), {"RoleName": NAME})
    stub.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
    stub.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
    stub.add_response("get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME})


def template_in_place(stub: Stubber) -> None:
    """起動テンプレートが揃っているときに読む応答を積む。セキュリティグループは GROUP を指す。"""
    stub.add_response(
        "describe_launch_template_versions",
        listed(a_version(echo())),
        {"LaunchTemplateName": TEMPLATE},
    )
    template_tags(stub)


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
    """作るときは信頼ポリシーとタグだけを渡す。タグには、このリージョンのものも入る。権限は、
    インラインポリシーと管理ポリシーをそれぞれ別に揃える。
    """
    with Stubber(iam) as stub:
        missing(stub, "get_role", "NoSuchEntity", RoleName=NAME)
        stub.add_response(
            "create_role",
            a_role(),
            {
                "RoleName": NAME,
                "AssumeRolePolicyDocument": resources.TRUST_POLICY,
                "Tags": ROLE_MARK,
            },
        )
        align_role(iam, service)
        stub.assert_no_pending_responses()


def test_role_that_is_already_right_is_left_alone(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """タグが揃っていれば、何も書かず、知らせもしない。temt が付けないタグや、ほかの
    リージョンのタグがあってもよい。
    """
    with Stubber(iam) as stub:
        stub.add_response(
            "get_role",
            a_role([*ROLE_MARK, {"Key": THERE, "Value": ""}, *OTHERS]),
            {"RoleName": NAME},
        )
        align_role(iam, service)
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_a_role_without_any_tag_gets_it(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """タグを 1 つも持たないロールは、AWS が Tags を返さない。"""
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(None), {"RoleName": NAME})
        stub.add_response("tag_role", {}, {"RoleName": NAME, "Tags": ROLE_MARK})
        align_role(iam, service)
        stub.assert_no_pending_responses()
    assert f"role {NAME}: updated" in capsys.readouterr().out


def test_a_role_that_only_has_other_tags_gets_its_tag(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """temt のタグが無ければ足す。同じ値の Service のタグは、temt のタグと見なさない。"""
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(OTHERS), {"RoleName": NAME})
        stub.add_response("tag_role", {}, {"RoleName": NAME, "Tags": ROLE_MARK})
        align_role(iam, service)
        stub.assert_no_pending_responses()
    assert f"role {NAME}: updated" in capsys.readouterr().out


@pytest.mark.parametrize(
    "tags",
    [
        pytest.param(MARK, id="no region"),
        pytest.param([*MARK, {"Key": THERE, "Value": ""}], id="another region"),
        pytest.param([*MARK, {"Key": HERE, "Value": "x"}], id="another value"),
    ],
)
def test_a_role_gets_the_tag_of_its_region(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str], tags: list[dict[str, str]]
) -> None:
    """このリージョンのタグが無いか、値が空でなければ、空の値で付ける。ほかのリージョンの
    タグは残す。diff にも、足すタグとして出す。
    """
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(tags), {"RoleName": NAME})
        found = resources.detect_role(iam, service, REGION)
        stub.add_response("tag_role", {}, {"RoleName": NAME, "Tags": [{"Key": HERE, "Value": ""}]})
        resources.role(iam, service, found)
        stub.assert_no_pending_responses()
    assert found.changes() == [Change("~", resources.ROLE, NAME, f"tags: {HERE}=")]
    assert f"role {NAME}: updated" in capsys.readouterr().out


def test_other_iam_errors_are_left_to_apply(iam: IAMClient, service: Service) -> None:
    with Stubber(iam) as stub:
        stub.add_client_error("get_role", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(ClientError):
            _ = resources.detect_role(iam, service, REGION)


def test_the_role_is_detected_without_writing(iam: IAMClient, service: Service) -> None:
    """無いロールは作るものとして、タグの無いロールは足すタグを添えて見せる。"""
    with Stubber(iam) as stub:
        missing(stub, "get_role", "NoSuchEntity", RoleName=NAME)
        stub.add_response("get_role", a_role(OTHERS), {"RoleName": NAME})
        absent = resources.detect_role(iam, service, REGION)
        untagged = resources.detect_role(iam, service, REGION)
        stub.assert_no_pending_responses()
    assert absent.changes() == [Change("+", resources.ROLE, NAME)]
    assert untagged.changes() == [Change("~", resources.ROLE, NAME, f"{MARK_SHOWN}, {HERE}=")]


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


def test_the_managed_policies_are_detected_without_writing(
    iam: IAMClient, service: Service
) -> None:
    """ManagedPolicyArns に無いものを外し、付いていないものを付ける。重ねて書いたものは
    1 度だけ付ける。付けるもの、外すものの順に見せる。
    """
    other = "arn:aws:iam::123456789012:policy/other"
    with Stubber(iam) as stub:
        stub.add_response(
            "list_attached_role_policies", attached(SSM_CORE, other), {"RoleName": NAME}
        )
        found = resources.detect_managed_policies(iam, service, [other, S3_READ, S3_READ])
        stub.assert_no_pending_responses()
    assert found == resources.ManagedDiff(attach=(S3_READ,), detach=(SSM_CORE,))
    assert found.changes() == [
        Change("+", resources.MANAGED_POLICY, S3_READ),
        Change("-", resources.MANAGED_POLICY, SSM_CORE),
    ]


def test_the_managed_policies_of_a_role_that_is_not_there(iam: IAMClient, service: Service) -> None:
    """ロールが無ければ、管理ポリシーはどれも付いていないものとする。"""
    with Stubber(iam) as stub:
        missing(stub, "list_attached_role_policies", "NoSuchEntity", RoleName=NAME)
        found = resources.detect_managed_policies(iam, service, [S3_READ])
        stub.assert_no_pending_responses()
    assert found == resources.ManagedDiff(attach=(S3_READ,))


def test_other_errors_from_reading_the_managed_policies_are_left_to_apply(
    iam: IAMClient, service: Service
) -> None:
    with Stubber(iam) as stub:
        stub.add_client_error(
            "list_attached_role_policies", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(ClientError):
            _ = resources.detect_managed_policies(iam, service, [S3_READ])


def test_the_managed_policies_are_attached_before_detaching(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """付けてから外す。管理ポリシーそのものには触らない。"""
    calls = recorded(iam)
    with Stubber(iam) as stub:
        stub.add_response("attach_role_policy", {}, {"RoleName": NAME, "PolicyArn": S3_READ})
        stub.add_response("detach_role_policy", {}, {"RoleName": NAME, "PolicyArn": SSM_CORE})
        resources.managed_policies(
            iam, service, resources.ManagedDiff(attach=(S3_READ,), detach=(SSM_CORE,))
        )
        stub.assert_no_pending_responses()
    assert calls == ["AttachRolePolicy", "DetachRolePolicy"]
    out = capsys.readouterr().out
    assert f"role {NAME}: attached {S3_READ}\n" in out
    assert f"role {NAME}: detached {SSM_CORE}\n" in out


def test_a_role_or_instance_profile_of_another_service_stops(
    iam: IAMClient, service: Service
) -> None:
    """同じ名前でも、別のサービスの temt:service が付いていれば、temt のものではない。"""
    other = [{"Key": "temt:service", "Value": "other"}]
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(other), {"RoleName": NAME})
        stub.add_response(
            "get_instance_profile", a_profile([NAME], other), {"InstanceProfileName": NAME}
        )
        with pytest.raises(TemtError, match=f"^the role {NAME} has temt:service=other, so it "):
            _ = resources.detect_role(iam, service, REGION)
        with pytest.raises(
            TemtError,
            match=(
                f"^the instance profile {NAME} has temt:service=other, so it belongs to another "
                "service; fix the tag or delete it by hand$"
            ),
        ):
            _ = resources.detect_instance_profile(iam, service)
        stub.assert_no_pending_responses()


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


def test_the_launch_template_that_already_matches(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """AWS が読み出しで変える形の違いは、違いと見なさない。

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
        found = resources.detect_template(ec2, service, wanted())
        resources.template(ec2, service, found, None, managed=False)
        stub.assert_no_pending_responses()
    assert found.changes() == []
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
        new_version(stub, wanted())
        align_template(ec2, service)
        stub.assert_no_pending_responses()
    assert f"launch template {TEMPLATE}: updated (version 2)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "current",
    [
        pytest.param(echo(ImageId="ami-00000000000000002"), id="ImageId"),
        pytest.param(echo(InstanceType="t3.small"), id="InstanceType"),
        pytest.param(echo(InstanceInitiatedShutdownBehavior="stop"), id="shutdown"),
        pytest.param(echo(CreditSpecification={"CpuCredits": "standard"}), id="credits"),
        pytest.param(
            echo(MetadataOptions={"HttpTokens": "optional", "InstanceMetadataTags": "enabled"}),
            id="HttpTokens",
        ),
        pytest.param(
            echo(IamInstanceProfile={"Arn": "arn:aws:iam::123456789012:instance-profile/other"}),
            id="another instance profile",
        ),
        pytest.param(without("IamInstanceProfile"), id="no instance profile"),
        pytest.param(interface(SubnetId="subnet-00000000000000002"), id="SubnetId"),
        pytest.param(interface(Groups=["sg-00000000000000002"]), id="Groups"),
        pytest.param(interface(AssociatePublicIpAddress=False), id="public address"),
        pytest.param(interface(DeviceIndex=1), id="DeviceIndex"),
        pytest.param(interface(Ipv6AddressCount=1), id="IPv6 address"),
        pytest.param(interface(Ipv6AddressCount=0), id="no IPv6 address"),
        pytest.param(one_tag(Value="other"), id="tag value"),
        pytest.param(one_tag(Key="Other"), id="tag key"),
        pytest.param(
            echo(TagSpecifications=[tags("volume"), tags("network-interface")]), id="no instance"
        ),
        # 設定ファイルに無い項目は、AWS の既定と同じ値でも、手で入れたものでも外す
        pytest.param(echo(KeyName="temt"), id="KeyName"),
        pytest.param(echo(EbsOptimized=False), id="EbsOptimized"),
        pytest.param(
            echo(BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30}}]),
            id="BlockDeviceMappings",
        ),
        pytest.param(echo(UserData=base64.b64encode(b"#!/bin/sh\n").decode()), id="UserData"),
    ],
)
def test_every_item_is_compared(ec2: EC2Client, service: Service, current: dict[str, Any]) -> None:
    """中身全体を比べるので、どの項目が違っても、無くても、余分でも新しいバージョンになる。"""
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(current)),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub)
        new_version(stub, wanted())
        align_template(ec2, service)
        # 違いを見落とせば、新しいバージョンを作らずに応答が余る
        stub.assert_no_pending_responses()


def test_the_launch_template_is_detected_without_writing(ec2: EC2Client, service: Service) -> None:
    """足すタグと、違う項目を 1 つ 1 行で <位置>: <今> -> <新> の形で見せる。

    配列の要素は対応づけのキーで書く。今か新しい値が無ければ - と書く。設定ファイルに無い
    項目は、新しい値が無いものとして見せる。
    """
    current = interface(SubnetId="subnet-00000000000000002")
    current["InstanceType"] = "t3.small"
    current["KeyName"] = "mykey"
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(current)),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(stub, None)
        found = resources.detect_template(ec2, service, wanted())
        stub.assert_no_pending_responses()
    assert found.changes() == [
        Change("~", resources.LAUNCH_TEMPLATE, TEMPLATE, detail)
        for detail in [
            MARK_SHOWN,
            "InstanceType: t3.small -> t3.micro",
            f"NetworkInterfaces[DeviceIndex=0].SubnetId: subnet-00000000000000002 -> {SUBNET}",
            "KeyName: mykey -> -",
        ]
    ]


def test_a_missing_launch_template_is_detected_to_be_created(
    ec2: EC2Client, service: Service
) -> None:
    """無ければ作るものとして見せ、中身は組み立てたもの（NEW_GROUP を含む）のまま持つ。"""
    data = wanted(NetworkInterfaces=[{"DeviceIndex": 0, "Groups": [resources.NEW_GROUP]}])
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateName=TEMPLATE,
        )
        found = resources.detect_template(ec2, service, data)
        stub.assert_no_pending_responses()
    assert found.changes() == [
        Change("+", resources.LAUNCH_TEMPLATE, TEMPLATE, f"ImageId: {IMAGE}")
    ]
    assert found.wanted == data


@pytest.mark.parametrize(
    ("note", "shown"),
    [
        pytest.param("temt-dev-main-2609200900", "(temt-dev-main-2609200900)", id="name"),
        pytest.param(
            "Ubuntu 24.04; no temt-dev-main AMI in ap-northeast-1",
            "(Ubuntu 24.04; no temt-dev-main AMI in ap-northeast-1)",
            id="ubuntu",
        ),
    ],
)
def test_a_template_to_be_created_shows_the_chosen_image(
    ec2: EC2Client, service: Service, note: str, shown: str
) -> None:
    """作るときは項目に開かず、選んだ AMI だけを、名前か Ubuntu を選んだわけを添えて出す。"""
    with Stubber(ec2) as stub:
        found = resources.detect_template(ec2, service, wanted(), versions=[], note=note)
        stub.assert_no_pending_responses()
    assert [one.detail for one in found.changes()] == [f"ImageId: {IMAGE} {shown}"]


def test_a_new_image_is_shown_with_its_name(ec2: EC2Client, service: Service) -> None:
    """ImageId が変われば、新しい AMI の名前を添える。先に読んだバージョンは読み直さない。"""
    versions = [a_version(echo(ImageId="ami-0fedcba9876543210"), 3)]
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_launch_templates",
            {"LaunchTemplates": [{"LaunchTemplateId": TEMPLATE_ID, "Tags": MARK}]},
            {"LaunchTemplateNames": [TEMPLATE]},
        )
        found = resources.detect_template(
            ec2, service, wanted(), versions=versions, note="temt-dev-main-2609200900"
        )
        stub.assert_no_pending_responses()
    assert [one.detail for one in found.changes()] == [
        f"ImageId: ami-0fedcba9876543210 -> {IMAGE} (temt-dev-main-2609200900)"
    ]


def test_the_launch_template_gets_the_id_of_the_security_group_made_after_detecting(
    ec2: EC2Client, service: Service
) -> None:
    """NEW_GROUP で比べた起動テンプレートは、揃えるときに渡したセキュリティグループの ID で
    作る。ほかの ID はそのまま残す。
    """
    other = "sg-00000000000000077"
    primary = {"DeviceIndex": 0, "SubnetId": SUBNET, "AssociatePublicIpAddress": True}
    data = wanted(NetworkInterfaces=[{**primary, "Groups": [resources.NEW_GROUP, other]}])
    with Stubber(ec2) as stub:
        missing(
            stub,
            "describe_launch_template_versions",
            resources.NO_SUCH_TEMPLATE,
            LaunchTemplateName=TEMPLATE,
        )
        create_template(stub, wanted(NetworkInterfaces=[{**primary, "Groups": [GROUP, other]}]))
        align_template(ec2, service, data, group_id=GROUP)
        stub.assert_no_pending_responses()
    # 比べた中身は書き換えない
    assert data["NetworkInterfaces"][0]["Groups"] == [resources.NEW_GROUP, other]


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
            listed(a_version(wanted(), 1, default=False)),
            {"LaunchTemplateName": TEMPLATE},
        )
        with pytest.raises(TemtError, match="no single default version"):
            _ = resources.detect_template(ec2, service, wanted())


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


def built(
    data: dict[str, Any] | None = None,
    *,
    group_id: str | None = GROUP,
    uses: bool = True,
    vpc_id: str | None = VPC,
) -> ltbuild.Built:
    """手順 4 で組み立てた起動テンプレートの中身。

    data は中身で、省けば wanted()（temt のセキュリティグループ GROUP を指す）。group_id は
    VPC にある temt のセキュリティグループで、uses は起動テンプレートがそれを使うか。
    """
    return ltbuild.Built(
        wanted() if data is None else data,
        {"ImageId": IMAGE},
        vpc_id,
        group_id,
        uses and group_id is None,
        uses,
        None,
    )


# temt のセキュリティグループがまだ無いときの中身。その ID は NEW_GROUP
NEW_DATA = wanted(
    NetworkInterfaces=[
        {
            "DeviceIndex": 0,
            "SubnetId": SUBNET,
            "AssociatePublicIpAddress": True,
            "Groups": [resources.NEW_GROUP],
        }
    ]
)


def nothing_there(iam_stub: Stubber, ec2_stub: Stubber) -> None:
    """ロール、インラインポリシー temt-jupyter、管理ポリシー、インスタンスプロファイル、
    セキュリティグループ、起動テンプレートが無いときに、detect が読む応答を積む。
    """
    missing(iam_stub, "get_role", "NoSuchEntity", RoleName=NAME)
    missing(iam_stub, "get_role_policy", "NoSuchEntity", **THE_POLICY)
    missing(iam_stub, "list_role_policies", "NoSuchEntity", RoleName=NAME)
    missing(iam_stub, "list_attached_role_policies", "NoSuchEntity", RoleName=NAME)
    missing(iam_stub, "get_instance_profile", "NoSuchEntity", InstanceProfileName=NAME)
    groups(ec2_stub)
    missing(
        ec2_stub,
        "describe_launch_template_versions",
        resources.NO_SUCH_TEMPLATE,
        LaunchTemplateName=TEMPLATE,
    )


def nothing_to_release(stub: Stubber) -> None:
    """[role] が無いとき、手放すロールとインスタンスプロファイルが無いことを読む応答を積む。"""
    missing(stub, "get_role", "NoSuchEntity", RoleName=NAME)
    missing(stub, "get_instance_profile", "NoSuchEntity", InstanceProfileName=NAME)


# 受信規則を 1 つと管理ポリシーを 1 つ書いた設定
FULL = Conf(
    AwsRegion=REGION,
    security_group=SecurityGroup(
        ingress=[Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home")]
    ),
    role=Role(ManagedPolicyArns=[S3_READ]),
)


def test_update_goes_through_them_in_order(
    ec2: EC2Client, iam: IAMClient, service: Service
) -> None:
    """設計が決めた順に作る。

    Stubber の待ち行列はクライアントごとなので、それだけではサービスをまたいだ
    順序を確かめられない。2 つのクライアントの呼び出しを 1 つの並びに記録する。
    """
    calls = recorded(ec2, iam)
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        nothing_there(iam_stub, ec2_stub)
        found = resources.detect(
            ec2, iam, service, FULL, built(NEW_DATA, group_id=None), policies={JUPYTER: CONTENT}
        )
        calls.clear()
        iam_stub.add_response(
            "create_role",
            a_role(),
            {
                "RoleName": NAME,
                "AssumeRolePolicyDocument": resources.TRUST_POLICY,
                "Tags": ROLE_MARK,
            },
        )
        iam_stub.add_response(
            "put_role_policy", {}, {**THE_POLICY, "PolicyDocument": json.dumps(CONTENT)}
        )
        iam_stub.add_response("attach_role_policy", {}, {"RoleName": NAME, "PolicyArn": S3_READ})
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
        resources.update(ec2, iam, service, found)
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert calls == [
        "CreateRole",
        "PutRolePolicy",
        "AttachRolePolicy",
        "CreateInstanceProfile",
        # waiter instance_profile_exists が読む
        "GetInstanceProfile",
        "AddRoleToInstanceProfile",
        "CreateSecurityGroup",
        "AuthorizeSecurityGroupIngress",
        "CreateLaunchTemplate",
    ]


def test_update_puts_the_created_security_group_into_the_launch_template(
    ec2: EC2Client,
    iam: IAMClient,
    service: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """セキュリティグループだけが無ければ、作ってから、その ID で起動テンプレートの
    新しいバージョンを作る。

    検出したときは、起動テンプレートを NEW_GROUP で比べている。変えなかったものは
    知らせない。
    """
    old_group = "sg-00000000000000002"
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        iam_in_place(iam_stub)
        groups(ec2_stub)
        ec2_stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(interface(Groups=[old_group]))),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(ec2_stub)
        found = resources.detect(
            ec2, iam, service, VALUES, built(NEW_DATA, group_id=None), policies={}
        )
        created(ec2_stub)
        new_version(ec2_stub, wanted())
        resources.update(ec2, iam, service, found)
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert found.template.changes() == [
        Change(
            "~",
            resources.LAUNCH_TEMPLATE,
            TEMPLATE,
            f"NetworkInterfaces[DeviceIndex=0].Groups: {old_group} -> {resources.NEW_GROUP}",
        )
    ]
    assert [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()] == [
        f"security group {NAME}: created ({GROUP})",
        f"launch template {TEMPLATE}: updated (version 2)",
    ]


def test_detect_reads_them_in_the_order_of_update_and_finds_nothing(
    ec2: EC2Client, iam: IAMClient, service: Service
) -> None:
    """揃っていれば、update が揃える順に読むだけで、変更は空になる。"""
    calls = recorded(ec2, iam)
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        iam_in_place(iam_stub)
        groups(ec2_stub, a_group())
        rules(ec2_stub)
        template_in_place(ec2_stub)
        found = resources.detect(ec2, iam, service, VALUES, built(), policies={})
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert found.changes() == []
    assert calls == READS


def test_detect_shows_what_update_would_create_in_its_order(
    ec2: EC2Client, iam: IAMClient, service: Service
) -> None:
    """何も無ければ、ロール、インラインポリシー、管理ポリシー、インスタンスプロファイル、
    セキュリティグループとその受信規則、起動テンプレートを、作るものとして並べる。何も
    書かない。
    """
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        nothing_there(iam_stub, ec2_stub)
        found = resources.detect(
            ec2, iam, service, FULL, built(NEW_DATA, group_id=None), policies={JUPYTER: CONTENT}
        )
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert [(one.mark, one.kind, one.name, one.detail) for one in found.changes()] == [
        ("+", resources.ROLE, NAME, ""),
        ("+", resources.ROLE_POLICY, JUPYTER, ""),
        ("+", resources.MANAGED_POLICY, S3_READ, ""),
        ("+", resources.INSTANCE_PROFILE, NAME, ""),
        ("+", resources.SECURITY_GROUP, NAME, f"in {VPC}"),
        ("+", resources.INGRESS, "22/tcp", "203.0.113.10/32   home"),
        ("+", resources.LAUNCH_TEMPLATE, TEMPLATE, f"ImageId: {IMAGE}"),
    ]


def test_detect_compares_the_template_with_the_group_to_be_created(
    ec2: EC2Client, iam: IAMClient, service: Service
) -> None:
    """セキュリティグループがまだ無ければ、起動テンプレートは NEW_GROUP を指すものとして比べる。"""
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        iam_in_place(iam_stub)
        groups(ec2_stub)
        template_in_place(ec2_stub)
        found = resources.detect(
            ec2, iam, service, VALUES, built(NEW_DATA, group_id=None), policies={}
        )
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert found.changes() == [
        Change("+", resources.SECURITY_GROUP, NAME, f"in {VPC}"),
        Change(
            "~",
            resources.LAUNCH_TEMPLATE,
            TEMPLATE,
            f"NetworkInterfaces[DeviceIndex=0].Groups: {GROUP} -> {resources.NEW_GROUP}",
        ),
    ]


def test_detect_only_reads_what_to_release_without_role(
    ec2: EC2Client, iam: IAMClient, service: Service
) -> None:
    """policies が None（[role] が無い）なら、detect はロールもインスタンスプロファイルも
    読まない。with_release が、起動テンプレートの後に、手放すものがあるかだけを読む。
    """
    calls = recorded(ec2, iam)
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        groups(ec2_stub, a_group())
        rules(ec2_stub)
        template_in_place(ec2_stub)
        found = resources.detect(ec2, iam, service, VALUES, built(), policies=None)
        assert found.release is None
        nothing_to_release(iam_stub)
        found = resources.with_release(iam, service, found, REGION)
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert found.iam is None
    assert found.release == resources.Release(NAME, REGION)
    assert found.changes() == []
    assert calls == [*READS[4:], "GetRole", "GetInstanceProfile"]


@pytest.mark.parametrize(
    ("uses", "vpc_id"),
    [(False, VPC), (True, None)],
    ids=["the template does not use it", "no subnet"],
)
def test_detect_reads_no_security_group_that_the_template_does_not_use(
    ec2: EC2Client, iam: IAMClient, service: Service, uses: bool, vpc_id: str | None
) -> None:
    """起動テンプレートが temt のセキュリティグループを使わないか、VPC が決まらなければ、
    セキュリティグループを読まず、揃えもしない。
    """
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        template_in_place(ec2_stub)
        found = resources.detect(
            ec2, iam, service, VALUES, built(uses=uses, vpc_id=vpc_id), policies=None
        )
        resources.update(ec2, iam, service, found)
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert found.group is None


def test_the_changes_of_what_was_found_come_in_the_order_of_update() -> None:
    """ロール、インラインポリシー、管理ポリシー、インスタンスプロファイル、セキュリティ
    グループとその受信規則、起動テンプレートの順に並べる。インラインポリシーは検出した順
    である。
    """
    idle = policy.policy_name("idle")
    mark = (("temt:service", "dev-main"),)
    found = resources.Found(
        iam=resources.IamDiff(
            role=resources.RoleDiff(NAME, exists=True, tags=mark),
            policies=(
                resources.PolicyDiff(JUPYTER, None, CONTENT),
                resources.PolicyDiff(idle, CONTENT, None),
            ),
            managed=resources.ManagedDiff(attach=(S3_READ,), detach=(SSM_CORE,)),
            profile=resources.ProfileDiff(NAME, exists=True, roles=("other",)),
        ),
        group=resources.GroupDiff(
            NAME,
            VPC,
            GROUP,
            resources.Changes([resources.Rule("tcp", 22, 22, "203.0.113.10/32")], [], []),
            mark,
        ),
        template=resources.TemplateDiff(TEMPLATE, wanted()),
    )
    assert [(one.mark, one.kind, one.name) for one in found.changes()] == [
        ("~", resources.ROLE, NAME),
        ("+", resources.ROLE_POLICY, JUPYTER),
        ("-", resources.ROLE_POLICY, idle),
        ("+", resources.MANAGED_POLICY, S3_READ),
        ("-", resources.MANAGED_POLICY, SSM_CORE),
        ("~", resources.INSTANCE_PROFILE, NAME),
        ("~", resources.SECURITY_GROUP, NAME),
        ("+", resources.INGRESS, "22/tcp"),
        ("+", resources.LAUNCH_TEMPLATE, TEMPLATE),
    ]


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
        align_template(ec2, service, managed=True)
        stub.assert_no_pending_responses()
    shown = capsys.readouterr().err
    assert "InvalidType: no such type" in shown
    # 反映待ちの断りは、その誤りのときだけ添える
    assert resources.NOT_READY_NOTE not in shown


@pytest.mark.parametrize("managed", [True, False])
def test_the_instance_profile_warning_says_it_is_iam_propagation(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str], managed: bool
) -> None:
    """作った直後は EC2 がインスタンスプロファイルを見つけられないことがある。

    temt のインスタンスプロファイルなら、update はその前に揃えているので、これは反映待ち
    であり、そう添える。temt のものでなければ、待っても直らないので添えない。
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
        align_template(ec2, service, managed=managed)
        stub.assert_no_pending_responses()
    shown = capsys.readouterr().err
    assert f"{resources.PROFILE_NOT_READY}: The IamInstanceProfile.Name '{NAME}'" in shown
    assert (resources.NOT_READY_NOTE in shown) == managed


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
    """delete_role が先に読むもの。temt のタグの付いたロールで、ほかのインスタンス
    プロファイルにも入っておらず、管理ポリシーも付いていない。
    """
    stub.add_response("get_role", a_role(), {"RoleName": NAME})
    stub.add_response(
        "list_instance_profiles_for_role", {"InstanceProfiles": []}, {"RoleName": NAME}
    )
    stub.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})


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
        stub.add_response("get_role", a_role(), {"RoleName": NAME})
        if method == "list_attached_role_policies":
            stub.add_response(
                "list_instance_profiles_for_role", {"InstanceProfiles": []}, {"RoleName": NAME}
            )
        stub.add_client_error(method, service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(ClientError):
            resources.delete_role(iam, service)


@pytest.mark.parametrize(
    ("given", "command"),
    [
        pytest.param(None, "destroy", id="destroy"),
        pytest.param("role delete", "role delete", id="role delete"),
    ],
)
def test_a_role_that_gets_held_after_the_check_is_reported(
    iam: IAMClient, service: Service, given: str | None, command: str
) -> None:
    """確かめた後に付けられたときも、外したことが IAM にまだ映らないときも DeleteConflict に
    なる。どちらか分からないので、AWS の文を添えて両方の直し方を告げる。やり直すコマンドは
    呼んだ側が渡し、省けば destroy。"""
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
            if given is None:
                resources.delete_role(iam, service)
            else:
                resources.delete_role(iam, service, command=given)
    assert str(caught.value) == (
        f"cannot delete the role {NAME}: An error occurred (DeleteConflict) when calling the "
        "DeleteRole operation: Cannot delete entity, must remove roles from instance profile "
        f"first.; if nothing is attached to it, IAM may not show the change yet, so run "
        f"{command} again; otherwise detach it by hand and run {command} again"
    )


def held_by(stub: Stubber, *profiles: str) -> None:
    """delete_role が先に読むもの。temt のタグの付いたロールが、profiles のインスタンス
    プロファイルに入っている。
    """
    stub.add_response("get_role", a_role(), {"RoleName": NAME})
    stub.add_response(
        "list_instance_profiles_for_role",
        {
            "InstanceProfiles": [
                {**a_profile([NAME])["InstanceProfile"], "InstanceProfileName": one}
                for one in profiles
            ]
        },
        {"RoleName": NAME},
    )


def test_a_role_still_in_its_instance_profile_just_deleted_is_deleted(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """インスタンスプロファイル <名前> を消した直後は、IAM の反映が遅れて一覧に残ることが
    あるので数えない。
    """
    with Stubber(iam) as stub:
        held_by(stub, NAME)
        stub.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
        stub.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
        stub.add_response("delete_role", {}, {"RoleName": NAME})
        resources.delete_role(iam, service)
        stub.assert_no_pending_responses()
    assert f"role {NAME}: {resources.DELETED}" in capsys.readouterr().out


def test_a_role_in_the_instance_profile_left_alone_stops_before_anything(
    iam: IAMClient, service: Service
) -> None:
    """インスタンスプロファイル <名前> をタグが違って残したとき（own_gone が偽）は、それに
    入ったロールを、ほかのインスタンスプロファイルに入ったものと同じく扱う。管理ポリシーを
    外しもインラインポリシーを消しもせずに止まる。外せば、残したインスタンスプロファイルで
    動くインスタンスが権限を失う。
    """
    with Stubber(iam) as stub:
        held_by(stub, NAME)
        with pytest.raises(TemtError) as caught:
            resources.delete_role(iam, service, own_gone=False)
        # 外す、消す呼び出しは積んでいない
        stub.assert_no_pending_responses()
    assert str(caught.value) == (
        f"the role {NAME} is also in the instance profile {NAME}; "
        "remove it from there by hand and run destroy again"
    )


def test_delete_instance_profile_tells_whether_it_is_gone(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """消したか、もともと無ければ True。タグが違って残せば False。"""
    with Stubber(iam) as stub:
        stub.add_response("get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME})
        stub.add_response(
            "remove_role_from_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
        )
        stub.add_response("delete_instance_profile", {}, {"InstanceProfileName": NAME})
        missing(stub, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME)
        stub.add_response(
            "get_instance_profile", a_profile([NAME], WRONG), {"InstanceProfileName": NAME}
        )
        assert resources.delete_instance_profile(iam, service) is True
        assert resources.delete_instance_profile(iam, service) is True
        assert resources.delete_instance_profile(iam, service) is False
        stub.assert_no_pending_responses()
    assert [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()] == [
        f"instance profile {NAME}: {resources.DELETED}",
        f"instance profile {NAME}: {resources.ABSENT}",
        f"instance profile {NAME}: {resources.LEFT.format(svc='dev-main')}",
    ]


@pytest.mark.parametrize("command", ["destroy", "role delete"])
def test_an_instance_profile_holding_another_role_names_the_command(
    iam: IAMClient, service: Service, command: str
) -> None:
    """ほかのロールが入っていれば、何も外さずに、やり直すコマンドを告げて止まる。"""
    with Stubber(iam) as stub:
        stub.add_response(
            "get_instance_profile", a_profile([NAME, "other"]), {"InstanceProfileName": NAME}
        )
        with pytest.raises(TemtError) as caught:
            resources.delete_instance_profile(iam, service, command=command)
        stub.assert_no_pending_responses()
    assert str(caught.value) == (
        f"the instance profile {NAME} holds another role: other; "
        f"remove it by hand and run {command} again"
    )


def test_an_instance_profile_that_will_not_go_names_the_command(
    iam: IAMClient, service: Service
) -> None:
    with Stubber(iam) as stub:
        stub.add_response("get_instance_profile", a_profile([]), {"InstanceProfileName": NAME})
        stub.add_client_error(
            "delete_instance_profile",
            service_error_code=resources.DELETE_CONFLICT,
            service_message="Cannot delete entity, must remove roles from instance profile first.",
            http_status_code=409,
        )
        with pytest.raises(TemtError) as caught:
            resources.delete_instance_profile(iam, service, command="role delete")
    assert str(caught.value) == (
        f"cannot delete the instance profile {NAME}: An error occurred (DeleteConflict) when "
        "calling the DeleteInstanceProfile operation: Cannot delete entity, must remove roles "
        "from instance profile first.; IAM may not show the role removed yet, so run role "
        "delete again; if this persists, remove the role from it by hand"
    )


def test_profile_roles_lists_the_roles_in_the_instance_profile(
    iam: IAMClient, service: Service
) -> None:
    """インスタンスプロファイル <名前> が無ければ空。"""
    with Stubber(iam) as stub:
        stub.add_response(
            "get_instance_profile", a_profile([NAME, "other"]), {"InstanceProfileName": NAME}
        )
        missing(stub, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME)
        assert resources.profile_roles(iam, service) == [NAME, "other"]
        assert resources.profile_roles(iam, service) == []
        stub.assert_no_pending_responses()


def test_other_errors_from_reading_the_roles_of_the_instance_profile_are_left_to_the_caller(
    iam: IAMClient, service: Service
) -> None:
    with Stubber(iam) as stub:
        stub.add_client_error(
            "get_instance_profile", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(ClientError):
            resources.profile_roles(iam, service)


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


def shown_changes(current: dict[str, Any], data: dict[str, Any]) -> list[str]:
    """template_changes の違いの文。続く行は含めない。"""
    return [detail for detail, _ in resources.template_changes(current, data)]


def test_template_changes_ignore_the_forms_aws_changes_on_reading() -> None:
    """並び、ARN で返る IamInstanceProfile、空の値、読み出したときにだけある項目は、違いに
    しない。
    """
    assert resources.template_changes(echo(), wanted()) == []
    found = resources.TemplateDiff(TEMPLATE, wanted(), echo(), 1, (1,))
    assert not found.changed
    assert found.changes() == []


@pytest.mark.parametrize(
    ("current", "data", "shown"),
    [
        pytest.param(
            wanted(),
            wanted(BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30}}]),
            ["BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize: - -> 30"],
            id="an element added",
        ),
        pytest.param(
            wanted(BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30}}]),
            wanted(),
            ["BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize: 30 -> -"],
            id="an element removed",
        ),
        pytest.param(
            wanted(),
            wanted(BlockDeviceMappings=[{"DeviceName": "/dev/sdb", "NoDevice": ""}]),
            ['BlockDeviceMappings[DeviceName=/dev/sdb].NoDevice: - -> ""'],
            id="a device left out",
        ),
        pytest.param(
            wanted(),
            {key: value for key, value in wanted().items() if key != "MetadataOptions"},
            [
                "MetadataOptions.HttpTokens: required -> -",
                "MetadataOptions.InstanceMetadataTags: enabled -> -",
            ],
            id="a table removed whole",
        ),
        pytest.param(
            interface(Groups=["sg-b", "sg-a"]),
            wanted(),
            [f"NetworkInterfaces[DeviceIndex=0].Groups: sg-a,sg-b -> {GROUP}"],
            id="an array of strings",
        ),
        pytest.param(
            interface(AssociatePublicIpAddress=False),
            wanted(),
            ["NetworkInterfaces[DeviceIndex=0].AssociatePublicIpAddress: false -> true"],
            id="a boolean",
        ),
        pytest.param(
            wanted(),
            wanted(LicenseSpecifications=[{"LicenseConfigurationArn": "arn:aws:license:x"}]),
            ['LicenseSpecifications: - -> {"LicenseConfigurationArn":"arn:aws:license:x"}'],
            id="an array of tables without a key",
        ),
        pytest.param(
            wanted(LicenseSpecifications=[{"LicenseConfigurationArn": "arn:aws:license:x"}]),
            wanted(),
            ['LicenseSpecifications: {"LicenseConfigurationArn":"arn:aws:license:x"} -> -'],
            id="an array of tables without a key removed",
        ),
        pytest.param(
            wanted(
                LicenseSpecifications=[
                    {"LicenseConfigurationArn": "arn:aws:license:a"},
                    {"LicenseConfigurationArn": "arn:aws:license:b"},
                ]
            ),
            wanted(
                LicenseSpecifications=[
                    {"LicenseConfigurationArn": "arn:aws:license:c"},
                    {"LicenseConfigurationArn": "arn:aws:license:b"},
                ]
            ),
            [
                'LicenseSpecifications: {"LicenseConfigurationArn":"arn:aws:license:a"} -> -',
                'LicenseSpecifications: - -> {"LicenseConfigurationArn":"arn:aws:license:c"}',
            ],
            id="an element of an array of tables without a key replaced",
        ),
        pytest.param(
            echo(KeyName="mykey"), wanted(), ["KeyName: mykey -> -"], id="an item to remove"
        ),
    ],
)
def test_template_changes_show_one_line_per_leaf(
    current: dict[str, Any], data: dict[str, Any], shown: list[str]
) -> None:
    """違いは、葉の項目の位置ごとに 1 行で見せる。配列の要素は対応づけのキーで書き、
    無い側は - と書く。
    """
    assert shown_changes(current, data) == shown


def user_data(text: str) -> str:
    """UserData の書き方。文書を base64 にしたもの。"""
    return base64.b64encode(text.encode()).decode()


def test_template_changes_show_the_user_data_as_a_diff() -> None:
    """UserData は base64 を戻した文書の違いを、unified diff で続く行に出す。"""
    before = wanted(UserData=user_data("#!/bin/sh\necho old\n"))
    after = wanted(UserData=user_data("#!/bin/sh\necho new\n"))
    assert resources.template_changes(before, after) == [
        (
            "UserData: (text) -> (text)",
            ("@@ -1,2 +1,2 @@", " #!/bin/sh", "-echo old", "+echo new"),
        )
    ]


def test_template_changes_show_user_data_added_or_removed_as_a_diff() -> None:
    """UserData を足すときも消すときも、文書なら、戻した文書を続く行に出す。"""
    data = wanted(UserData=user_data("#!/bin/sh\necho new\n"))
    assert resources.template_changes(wanted(), data) == [
        ("UserData: - -> (text)", ("@@ -0,0 +1,2 @@", "+#!/bin/sh", "+echo new"))
    ]
    assert resources.template_changes(data, wanted()) == [
        ("UserData: (text) -> -", ("@@ -1,2 +0,0 @@", "-#!/bin/sh", "-echo new"))
    ]


def test_template_changes_show_user_data_that_is_not_text_by_its_size() -> None:
    """文字として読めない UserData は、バイト数だけを見せる。片側だけが文字でも同じ。"""
    data = wanted(UserData=base64.b64encode(b"\xff\xfe\x00\x01").decode())
    assert resources.template_changes(wanted(), data) == [("UserData: - -> 4 bytes", ())]
    assert resources.template_changes(data, wanted()) == [("UserData: 4 bytes -> -", ())]
    text = wanted(UserData=user_data("#!/bin/sh\n"))
    assert resources.template_changes(text, data) == [("UserData: 10 bytes -> 4 bytes", ())]


def test_template_differ_names_the_positions_that_differ() -> None:
    """違う項目の位置を、組み立てた側（wanted）の順に、続けていまの側にだけあるものを
    挙げる。AWS が読み出しで変える形は違いにしない。
    """
    assert resources.template_differ(echo(), wanted()) == []
    current = echo(KeyName="mykey", InstanceType="t3.small")
    data = wanted(UserData=user_data("#!/bin/sh\n"))
    assert resources.template_differ(current, data) == ["InstanceType", "UserData", "KeyName"]
    assert resources.template_differ(interface(SubnetId="subnet-other"), wanted()) == [
        "NetworkInterfaces[DeviceIndex=0].SubnetId"
    ]


def test_template_changes_show_a_new_version_when_no_item_differs() -> None:
    """同じ種類の TagSpecifications が 2 つあると、開いた項目では違いが見えない。
    それでも apply は新しいバージョンを作るので、そのことを出す。"""
    current = wanted()
    current["TagSpecifications"] = [
        *current["TagSpecifications"],
        {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "temt-dev-main"}]},
    ]
    found = resources.TemplateDiff(TEMPLATE, wanted(), current, 1, (1,))
    assert found.changed
    assert found.changes() == [Change("~", "launch-template", TEMPLATE, "new version")]


def test_template_changes_show_leftover_versions() -> None:
    """中身が同じでも、既定以外のバージョンが残っていれば、消すことを出す。"""
    found = resources.TemplateDiff(TEMPLATE, wanted(), echo(), 3, (1, 2, 3))
    assert found.changes() == [
        Change("~", "launch-template", TEMPLATE, "delete other versions: 1, 2")
    ]


def test_template_changes_show_versions_newer_than_the_default() -> None:
    """既定より新しいバージョンも、既定以外として消す。"""
    found = resources.TemplateDiff(TEMPLATE, wanted(), echo(), 3, (2, 3, 4))
    assert found.leftovers == (2, 4)


def test_template_changes_leave_leftover_versions_to_the_new_version() -> None:
    """新しいバージョンを作るときは、既定以外を消すことを別に出さない。"""
    found = resources.TemplateDiff(TEMPLATE, wanted(), echo(InstanceType="t3.small"), 3, (2, 3))
    assert found.leftovers == ()
    assert [one.detail for one in found.changes()] == ["InstanceType: t3.small -> t3.micro"]


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


@pytest.mark.parametrize(
    ("policies", "profile", "aligned"),
    [
        pytest.param({}, {"Name": NAME}, True, id="role and its instance profile"),
        pytest.param({}, {"Name": "other"}, False, id="role and another instance profile"),
        pytest.param(None, {"Name": NAME}, False, id="no role"),
    ],
)
def test_detect_tells_whether_the_template_points_at_the_profile_it_aligns(
    ec2: EC2Client,
    iam: IAMClient,
    service: Service,
    policies: dict[str, Any] | None,
    profile: dict[str, str],
    aligned: bool,
) -> None:
    """[role] があり（policies が None でない）、起動テンプレートの IamInstanceProfile が
    そのインスタンスプロファイルを指すときだけ、起動テンプレートの警告に注記を添える。
    [role] があれば、IamInstanceProfile がどこを指してもロールは揃える。
    """
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        if policies is not None:
            iam_in_place(iam_stub)
        groups(ec2_stub, a_group())
        rules(ec2_stub)
        template_in_place(ec2_stub)
        found = resources.detect(
            ec2, iam, service, VALUES, built(wanted(IamInstanceProfile=profile)), policies=policies
        )
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert found.aligned is aligned
    assert (found.iam is not None) is (policies is not None)


# ロール <名前> が IAM のロール名の上限より長いサービス
LONG = Service("a" * 60)


def in_profiles(stub: Stubber, *profiles: str) -> None:
    """ロール <名前> を入れたインスタンスプロファイル profiles を読む応答を積む。"""
    stub.add_response(
        "list_instance_profiles_for_role",
        {
            "InstanceProfiles": [
                {**a_profile([NAME])["InstanceProfile"], "InstanceProfileName": one}
                for one in profiles
            ]
        },
        {"RoleName": NAME},
    )


@pytest.mark.parametrize("here", [True, False], ids=["tagged here", "not tagged here"])
def test_release_keeps_the_role_that_another_region_uses(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str], here: bool
) -> None:
    """ほかのリージョンのタグが残っていれば、このリージョンのタグだけを外し、ロールも
    インスタンスプロファイルも残す。ポリシーもインスタンスプロファイルも読まない。タグが
    無ければ、何も変えない。
    """
    tags = [*MARK, *([{"Key": HERE, "Value": ""}] if here else []), {"Key": THERE, "Value": ""}]
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(tags), {"RoleName": NAME})
        found = resources.detect_release(iam, service, REGION)
        stub.assert_no_pending_responses()
        # 変えるものが無ければ、読み直しもしない
        if here:
            stub.add_response("get_role", a_role(tags), {"RoleName": NAME})
            stub.add_response("untag_role", {}, {"RoleName": NAME, "TagKeys": [HERE]})
        resources.release(iam, service, found)
        stub.assert_no_pending_responses()
    assert found.kept
    expected = [
        Change("~", resources.ROLE, NAME, f"untag {HERE}; kept: used in us-west-2"),
    ]
    assert found.changes() == (expected if here else [])
    out = [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    # 残すことも、ロールの 1 行で知らせる
    expected_out = [f"role {NAME}: untagged {HERE}", f"role {NAME}: kept (used in us-west-2)"]
    assert out == (expected_out if here else [])


@pytest.mark.parametrize("here", [True, False], ids=["tagged here", "no region tag"])
def test_release_deletes_the_last_use_after_the_tag(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str], here: bool
) -> None:
    """ほかのリージョンのタグが無ければ、このリージョンのタグを外して、インスタンス
    プロファイルとロールを消す。diff には、消すロールのポリシーと、インスタンスプロファイルを
    出す。変えないものは知らせない。
    """
    tags = [*MARK, *([{"Key": HERE, "Value": ""}] if here else [])]
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(tags), {"RoleName": NAME})
        stub.add_response("list_role_policies", {"PolicyNames": [JUPYTER]}, {"RoleName": NAME})
        stub.add_response("list_attached_role_policies", attached(S3_READ), {"RoleName": NAME})
        stub.add_response("get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME})
        found = resources.detect_release(iam, service, REGION)
        stub.assert_no_pending_responses()
        stub.add_response("get_role", a_role(tags), {"RoleName": NAME})
        if here:
            stub.add_response("untag_role", {}, {"RoleName": NAME, "TagKeys": [HERE]})
        in_profiles(stub, NAME)
        stub.add_response("get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME})
        stub.add_response(
            "remove_role_from_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
        )
        stub.add_response("delete_instance_profile", {}, {"InstanceProfileName": NAME})
        stub.add_response("list_attached_role_policies", attached(S3_READ), {"RoleName": NAME})
        stub.add_response("detach_role_policy", {}, {"RoleName": NAME, "PolicyArn": S3_READ})
        stub.add_response("list_role_policies", {"PolicyNames": [JUPYTER]}, {"RoleName": NAME})
        stub.add_response("delete_role_policy", {}, THE_POLICY)
        stub.add_response("delete_role", {}, {"RoleName": NAME})
        resources.release(iam, service, found)
        stub.assert_no_pending_responses()
    assert found.changes() == [
        Change(
            "-",
            resources.ROLE,
            NAME,
            f"inline policies: {JUPYTER}; managed policies: {S3_READ}",
        ),
        Change("-", resources.INSTANCE_PROFILE, NAME),
    ]
    out = [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    assert out == [
        *([f"role {NAME}: untagged {HERE}"] if here else []),
        f"instance profile {NAME}: {resources.DELETED}",
        f"role {NAME}: detached {S3_READ}",
        f"role policy {JUPYTER}: {resources.DELETED}",
        f"role {NAME}: {resources.DELETED}",
    ]


@pytest.mark.parametrize(
    "tags",
    [pytest.param(None, id="untagged"), pytest.param(WRONG, id="another service")],
)
def test_release_deletes_only_the_instance_profile_beside_a_role_of_another(
    iam: IAMClient, service: Service, tags: list[dict[str, str]] | None
) -> None:
    """ロールが無いか temt:service=<svc> が付いていなければ、ロールはそのままにし、
    temt:service=<svc> の付いたインスタンスプロファイルだけを消す。
    """
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(tags), {"RoleName": NAME})
        stub.add_response("get_instance_profile", a_profile([]), {"InstanceProfileName": NAME})
        found = resources.detect_release(iam, service, REGION)
        stub.assert_no_pending_responses()
    assert found.changes() == [Change("-", resources.INSTANCE_PROFILE, NAME)]


def test_release_deletes_the_instance_profile_and_leaves_a_role_of_another_service(
    iam: IAMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ロールが別のサービスのものなら、インスタンスプロファイルからロール <名前> を外して
    消し、ロールには触らない。残したロールも、destroy と同じく知らせる。
    """
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(WRONG), {"RoleName": NAME})
        stub.add_response("get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME})
        found = resources.detect_release(iam, service, REGION)
        stub.add_response("get_role", a_role(WRONG), {"RoleName": NAME})
        stub.add_response("get_instance_profile", a_profile([NAME]), {"InstanceProfileName": NAME})
        stub.add_response(
            "remove_role_from_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
        )
        stub.add_response("delete_instance_profile", {}, {"InstanceProfileName": NAME})
        resources.release(iam, service, found)
        stub.assert_no_pending_responses()
    assert found.changes() == [Change("-", resources.INSTANCE_PROFILE, NAME)]
    assert [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()] == [
        f"instance profile {NAME}: {resources.DELETED}",
        f"role {NAME}: {resources.LEFT.format(svc='dev-main')}",
    ]


def test_release_leaves_an_instance_profile_of_another_alone(
    iam: IAMClient, service: Service
) -> None:
    """temt:service=<svc> の無いインスタンスプロファイルは消さない。ロールも無ければ、何も
    変えない。
    """
    with Stubber(iam) as stub:
        missing(stub, "get_role", "NoSuchEntity", RoleName=NAME)
        stub.add_response(
            "get_instance_profile", a_profile(["other"], WRONG), {"InstanceProfileName": NAME}
        )
        found = resources.detect_release(iam, service, REGION)
        stub.assert_no_pending_responses()
    assert found.changes() == []


@pytest.mark.parametrize(
    ("roles", "profiles", "profile_tags", "error"),
    [
        pytest.param(
            [NAME, "other"],
            (NAME,),
            MARK,
            f"the instance profile {NAME} holds another role: other; remove it by hand and run "
            "apply again",
            id="the instance profile holds another role",
        ),
        pytest.param(
            [NAME],
            (NAME, "shared"),
            MARK,
            f"the role {NAME} is also in the instance profile shared; remove it from there by "
            "hand and run apply again",
            id="the role is in another instance profile",
        ),
        pytest.param(
            [NAME],
            (NAME,),
            WRONG,
            f"the role {NAME} is also in the instance profile {NAME}, which is left as it is not "
            "tagged temt:service=dev-main; remove the role from it by hand and run apply again",
            id="the role is in the instance profile left alone",
        ),
    ],
)
def test_release_stops_when_it_cannot_delete(
    iam: IAMClient,
    service: Service,
    roles: list[str],
    profiles: tuple[str, ...],
    profile_tags: list[dict[str, str]],
    error: str,
) -> None:
    """消せるかは検出するときには確かめず、消すときに確かめて、消せなければ、何も外さず
    消さずに、destroy と同じ誤りで止まる（やり直すコマンドは apply）。
    """
    with Stubber(iam) as stub:
        stub.add_response("get_role", a_role(MARK), {"RoleName": NAME})
        stub.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
        stub.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
        stub.add_response(
            "get_instance_profile", a_profile(roles, profile_tags), {"InstanceProfileName": NAME}
        )
        found = resources.detect_release(iam, service, REGION)
        stub.assert_no_pending_responses()
        stub.add_response("get_role", a_role(MARK), {"RoleName": NAME})
        in_profiles(stub, *profiles)
        if NAME in profiles and len(profiles) == 1:
            stub.add_response(
                "get_instance_profile",
                a_profile(roles, profile_tags),
                {"InstanceProfileName": NAME},
            )
        with pytest.raises(TemtError) as caught:
            resources.release(iam, service, found)
        # 外す、消す呼び出しは積んでいない
        stub.assert_no_pending_responses()
    assert str(caught.value) == error


def test_a_name_too_long_for_iam_counts_as_absent(
    iam: IAMClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """<名前> が IAM のロール名の上限より長ければ、IAM を読むだけのところは、呼ばずに
    ロールもインスタンスプロファイルも無いものとする。呼べば誤りになり、destroy が
    終わらない。応答は積んでいないので、呼べば Stubber が失敗する。
    """
    assert len(LONG.name) > resources.ROLE_NAME_MAX
    with Stubber(iam) as stub:
        found = resources.detect_release(iam, LONG, REGION)
        listed_role = resources.role_release(iam, LONG, REGION)
        resources.delete_iam(iam, LONG, REGION)
        stub.assert_no_pending_responses()
    assert found.changes() == []
    assert listed_role == resources.Release(LONG.name, REGION)
    assert [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()] == [
        f"instance profile {LONG.name}: {resources.ABSENT}",
        f"role {LONG.name}: {resources.ABSENT}",
    ]


def test_a_name_of_the_limit_is_read(iam: IAMClient, home_dir: Any) -> None:
    """上限ちょうどの <名前> は、IAM を読む。"""
    _ = home_dir
    service = Service("a" * (resources.ROLE_NAME_MAX - len("temt-")))
    with Stubber(iam) as stub:
        missing(stub, "get_role", "NoSuchEntity", RoleName=service.name)
        missing(stub, "get_instance_profile", "NoSuchEntity", InstanceProfileName=service.name)
        _ = resources.detect_release(iam, service, REGION)
        stub.assert_no_pending_responses()


def test_update_releases_the_role_after_the_launch_template(
    ec2: EC2Client, iam: IAMClient, service: Service
) -> None:
    """[role] が無ければ、起動テンプレートを揃えてから、タグを外し、インスタンス
    プロファイルとロールを消す。先に消すと、テンプレートの更新が失敗したとき、テンプレートが
    消えたインスタンスプロファイルを指したまま残る。
    """
    calls = recorded(ec2, iam)
    tags = [*MARK, {"Key": HERE, "Value": ""}]
    with Stubber(iam) as iam_stub, Stubber(ec2) as ec2_stub:
        ec2_stub.add_response(
            "describe_launch_template_versions",
            listed(a_version(echo(InstanceType="t3.small"))),
            {"LaunchTemplateName": TEMPLATE},
        )
        template_tags(ec2_stub)
        iam_stub.add_response("get_role", a_role(tags), {"RoleName": NAME})
        iam_stub.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
        iam_stub.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
        missing(iam_stub, "get_instance_profile", "NoSuchEntity", InstanceProfileName=NAME)
        found = resources.detect(ec2, iam, service, VALUES, built(uses=False), policies=None)
        found = resources.with_release(iam, service, found, REGION)
        calls.clear()
        new_version(ec2_stub, wanted())
        iam_stub.add_response("get_role", a_role(tags), {"RoleName": NAME})
        iam_stub.add_response("untag_role", {}, {"RoleName": NAME, "TagKeys": [HERE]})
        in_profiles(iam_stub)
        missing(iam_stub, "get_instance_profile", "NoSuchEntity", InstanceProfileName=NAME)
        iam_stub.add_response("list_attached_role_policies", attached(), {"RoleName": NAME})
        iam_stub.add_response("list_role_policies", {"PolicyNames": []}, {"RoleName": NAME})
        iam_stub.add_response("delete_role", {}, {"RoleName": NAME})
        resources.update(ec2, iam, service, found)
        iam_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()
    assert calls == [
        "CreateLaunchTemplateVersion",
        "ModifyLaunchTemplate",
        "DeleteLaunchTemplateVersions",
        "GetRole",
        "UntagRole",
        "ListInstanceProfilesForRole",
        "GetInstanceProfile",
        "ListAttachedRolePolicies",
        "ListRolePolicies",
        "DeleteRole",
    ]
