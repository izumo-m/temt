"""サービスの AWS リソースを揃える・消す。apply の手順 5 と destroy の手順 4。

起動テンプレートについては、bake が AMI だけを替えるための point_image、show と
bake が既定バージョンを読むための関数と、bake がそれを設定ファイルと比べるための
関数も持つ。all_versions は、ami のコマンドが、テンプレートが指す AMI を集めるのに
使う。

detect は、IAM ロール、そのインラインポリシー temt-<ポリシー名>、インスタンス
プロファイル、セキュリティグループ、起動テンプレートの差分を、この順に検出する。
update は、検出した差分で、同じ順に、無ければ作り、あれば設定ファイルに合わせる。
インラインポリシー以外のどれにも temt のタグを付け、無いか値が違えば書く。設定
ファイルに無い temt- で始まるインラインポリシーは消す。作ったもの、更新したもの、
消したものを、それぞれ知らせる。リソースごとには、detect_<リソース> が AWS を読んで差分を検出し、
リソースの名前の関数（role、role_policies など）が検出した差分で揃える。

remove は、起動テンプレート、/<名前> の下の SSM パラメータ、セキュリティ
グループ、インスタンスプロファイル、IAM ロールを、この順に消す。ロールは、付いた
インラインポリシーをすべて消してから消す。消したもの、既に無かったものを、それぞれ
知らせる。

セキュリティグループについては、sg diff と sg show が AWS と設定ファイルを比べて
見せるための groups、current_rules、wanted_rules、changes も持つ。

起動テンプレートがあることは、サービスが apply されていて、destroy されていない
ことの目印でもある。require_template がそれを確かめる。

botocore の例外は、そのリソースが無いことを表す誤りだけをここで扱い、他は呼んだ側
にそのまま渡す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from ipaddress import ip_network
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import ClientError

from temt import aws, change, conf, ltbuild, ltdata, msg, policy, table
from temt.aws import error_code, field
from temt.change import ADD, ARROW, REMOVE, UPDATE, Change
from temt.msg import TemtError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ec2.type_defs import (
        IpPermissionTypeDef,
        SecurityGroupTypeDef,
        TagTypeDef,
    )
    from types_boto3_iam.client import IAMClient
    from types_boto3_ssm.client import SSMClient

    from temt.conf import Conf
    from temt.names import Service

# そのリソースが無いときに返る誤りの名前
NO_SUCH_ENTITY = "NoSuchEntity"
NO_SUCH_TEMPLATE = "InvalidLaunchTemplateName.NotFoundException"
NO_SUCH_GROUP = "InvalidGroup.NotFound"
# まだ何かが付いていて消せないときに IAM が返す誤りの名前
DELETE_CONFLICT = "DeleteConflict"
# セキュリティグループにまだネットワークインターフェースが付いているか、他から参照されて
# いるときに、削除が返す誤りの名前
DEPENDENCY_VIOLATION = "DependencyViolation"
# delete-launch-template-versions に 1 回で渡せるバージョンの数の上限
VERSIONS_PER_CALL = 200
# EC2 が、作ったばかりのインスタンスプロファイルをまだ見つけられないときの誤り。
# update は起動テンプレートより先にインスタンスプロファイルを揃えるので、この誤りは
# IAM の反映待ちであり、待てば消える
PROFILE_NOT_READY = "InvalidIamInstanceProfile.NotFound"
NOT_READY_NOTE = "IAM has not propagated it to EC2 yet; the instance profile exists"

# ロールを引き受けられるのは EC2 だけとする
TRUST_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)
GROUP_DESCRIPTION = "Managed by temt"
# AWS が番号で返すプロトコルの名前。設定ファイルと比べられるよう、名前に揃える
PROTOCOL_NAMES = {"6": "tcp", "17": "udp", "1": "icmp", "58": "icmpv6"}
ICMPV6 = "icmpv6"
# IPv6 の送信元を持つ IPv4 の ICMP の規則の表示。設定ファイルには書けない
ICMPV4 = "icmpv4"
# すべてのプロトコルを表す AWS の値と、その表示
ALL_PROTOCOLS = "-1"
ALL = "all"
# ICMP の規則で、すべての種類を表すポートの値。ポートを持たないプロトコルでもこの値
ANY_TYPE = -1
# 送信元が読めない規則の表示
UNKNOWN_SOURCE = "?"

# CPU クレジットを持つ T 系のインスタンスの型。ltbuild が持つ
BURSTABLE = ltbuild.BURSTABLE
# サブネットに IPv6 の CIDR が結び付いているときに付ける IPv6 アドレスの数
IPV6_ADDRESSES = 1
DEFAULT_VERSION = "$Default"
# delete-parameters が 1 回に受け付ける名前の数
PARAMETER_BATCH = 10

CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
DELETED = "deleted"
ABSENT = "absent"

# 差分の種類。change.Change の kind
ROLE = "role"
ROLE_POLICY = "role-policy"
MANAGED_POLICY = "managed-policy"
INSTANCE_PROFILE = "instance-profile"
SECURITY_GROUP = "security-group"
INGRESS = "ingress"
LAUNCH_TEMPLATE = "launch-template"
# まだ作っていないセキュリティグループの ID の表示。diff が起動テンプレートの差分に書く
NEW_GROUP = ltbuild.NEW_GROUP


def _mark(service: Service) -> list[tuple[str, str]]:
    """temt が管理していることを示すタグ。キーと値の組で持つ。

    AWS のサービスごとにタグの型が違うので、渡すときにそれぞれの形にする。
    """
    return [(aws.TAG_SERVICE, service.svc)]


def _report(what: str, state: str, note: str = "") -> None:
    """揃えた結果を知らせる。変えなかったもの（unchanged、absent）は知らせない。

    apply は、何が変わるかを確認の前に見せ終わっている。note は状態の後ろに添える。
    """
    if state not in (UNCHANGED, ABSENT):
        msg.info(f"{what}: {state}{note}")


def _missing(
    present: Sequence[Mapping[str, Any]] | None, wanted: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """wanted のうち、present に無いか値の違うタグ。

    タグを 1 つも持たないリソースは、AWS が Tags を返さないので None を受け付ける。
    """
    have = {(tag.get("Key"), tag.get("Value")) for tag in present or []}
    return [pair for pair in wanted if pair not in have]


@dataclass(frozen=True)
class RoleDiff:
    """IAM ロールの差分。exists でなければ作る。tags は足すタグ。"""

    name: str
    exists: bool
    tags: tuple[tuple[str, str], ...] = ()

    def changes(self) -> list[Change]:
        """diff に出す形にする。"""
        if not self.exists:
            return [Change(ADD, ROLE, self.name)]
        if self.tags:
            return [Change(UPDATE, ROLE, self.name, change.tags(self.tags))]
        return []


def _owned(what: str, tags: Sequence[Mapping[str, Any]] | None, service: Service) -> None:
    """temt:service のタグが別のサービスのものなら止める。無ければ、足して使う。"""
    other = aws.tag({"Tags": tags or []}, aws.TAG_SERVICE)
    if other and other != service.svc:
        raise TemtError(
            f"the {what} {service.name} has {aws.TAG_SERVICE}={table.printable(other)}, so it "
            f"belongs to another service; fix the tag or delete it by hand"
        )


def detect_role(iam: IAMClient, service: Service) -> RoleDiff:
    """インスタンスロール <名前> の差分を検出する。別のサービスのタグが付いていれば止める。"""
    try:
        found = iam.get_role(RoleName=service.name)["Role"]
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        return RoleDiff(service.name, exists=False)
    _owned("role", found.get("Tags"), service)
    return RoleDiff(
        service.name, exists=True, tags=tuple(_missing(found.get("Tags"), _mark(service)))
    )


def role(iam: IAMClient, service: Service, found: RoleDiff) -> None:
    """インスタンスロールを揃える。無ければ作り、タグが無ければ付ける。

    権限は、インラインポリシー temt-<ポリシー名>（role_policies）と、管理ポリシー
    （managed_policies）で付ける。
    """
    if not found.exists:
        _ = iam.create_role(
            RoleName=service.name,
            AssumeRolePolicyDocument=TRUST_POLICY,
            Tags=[{"Key": key, "Value": value} for key, value in _mark(service)],
        )
        state = CREATED
    elif found.tags:
        _ = iam.tag_role(
            RoleName=service.name, Tags=[{"Key": key, "Value": value} for key, value in found.tags]
        )
        state = UPDATED
    else:
        state = UNCHANGED
    _report(f"role {service.name}", state)


@dataclass(frozen=True)
class PolicyDiff:
    """インラインポリシー 1 つの差分。

    current は今の中身で、無ければ None。wanted は揃える中身で、消すなら None。
    get-role-policy の中身は、boto3 が JSON を読んで返す。
    """

    name: str
    current: Any
    wanted: Any

    @property
    def changed(self) -> bool:
        """書くか消すか。"""
        if self.wanted is None:
            return self.current is not None
        # dict のまま比べると True と 1 が等しくなり、直した値が IAM に届かない
        return json.dumps(self.current, sort_keys=True) != json.dumps(self.wanted, sort_keys=True)

    def changes(self) -> list[Change]:
        """diff に出す形にする。中身の違いを続く行に出す。"""
        if not self.changed:
            return []
        if self.current is None:
            mark = ADD
        elif self.wanted is None:
            mark = REMOVE
        else:
            mark = UPDATE
        body = change.lines_diff(change.json_lines(self.current), change.json_lines(self.wanted))
        return [Change(mark, ROLE_POLICY, self.name, body=body)]


def _role_policy(iam: IAMClient, service: Service, name: str) -> Any:
    """ロールのインラインポリシー name の中身。無ければ None。"""
    try:
        return iam.get_role_policy(RoleName=service.name, PolicyName=name)["PolicyDocument"]
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        return None


def detect_role_policies(
    iam: IAMClient, service: Service, policies: Mapping[str, Mapping[str, Any]]
) -> list[PolicyDiff]:
    """ロールのインラインポリシー temt-* と policies の差分を検出する。

    policies はインラインポリシーの名前から中身への対応。policies の順に並べ、その後に
    消すもの（policies に無い temt-*）を並べる。temt- で始まらないインライン
    ポリシーは見ない。ロールが無ければ、どれも無いものとする。
    """
    found = [
        PolicyDiff(name, _role_policy(iam, service, name), content)
        for name, content in policies.items()
    ]
    found += [
        PolicyDiff(name, _role_policy(iam, service, name), None)
        for name in inline_policies(iam, service)
        if name.startswith(policy.POLICY_PREFIX) and name not in policies
    ]
    return found


def inline_policy(iam: IAMClient, service: Service, found: PolicyDiff) -> None:
    """ロールのインラインポリシー 1 つを、検出した差分で揃える。"""
    name = found.name
    if found.wanted is None:
        if found.current is None:
            state = ABSENT
        else:
            _ = iam.delete_role_policy(RoleName=service.name, PolicyName=name)
            state = DELETED
    elif not found.changed:
        state = UNCHANGED
    else:
        _ = iam.put_role_policy(
            RoleName=service.name, PolicyName=name, PolicyDocument=json.dumps(found.wanted)
        )
        state = CREATED if found.current is None else UPDATED
    _report(f"role policy {name}", state)


def role_policies(iam: IAMClient, service: Service, found: Sequence[PolicyDiff]) -> None:
    """ロールのインラインポリシー temt-* を、検出した差分の順に揃える。

    detect_role_policies が消すものを後ろに並べるので、名前を替えて移るときに、
    アプリが権限を失う間を作らないよう、書いてから消すことになる。
    """
    for one in found:
        inline_policy(iam, service, one)


@dataclass(frozen=True)
class ManagedDiff:
    """インスタンスロールに付ける管理ポリシーの差分。attach は付けるもの、detach は外すもの。"""

    attach: tuple[str, ...] = ()
    detach: tuple[str, ...] = ()

    def changes(self) -> list[Change]:
        """diff に出す形にする。付けるもの、外すものの順。"""
        return [Change(ADD, MANAGED_POLICY, arn) for arn in self.attach] + [
            Change(REMOVE, MANAGED_POLICY, arn) for arn in self.detach
        ]


def attached_policies(iam: IAMClient, service: Service) -> list[str]:
    """インスタンスロール <名前> に付いた管理ポリシーの ARN。ロールが無ければ空。"""
    return attached_policies_of(iam, service.name)


def attached_policies_of(iam: IAMClient, name: str) -> list[str]:
    """ロール name に付いた管理ポリシーの ARN。ロールが無ければ空。role import も使う。"""
    try:
        return [
            field(one, "PolicyArn")
            for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=name)
            for one in page.get("AttachedPolicies", [])
        ]
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        return []


def read_tags(iam: IAMClient, what: str, service: Service) -> list[Mapping[str, Any]] | None:
    """インスタンスロール（what が role）かインスタンスプロファイル <名前> のタグ。無ければ
    None。role delete が、消してよいかを確かめるのに使う。
    """
    try:
        if what == "role":
            return list(iam.get_role(RoleName=service.name)["Role"].get("Tags") or [])
        found = iam.get_instance_profile(InstanceProfileName=service.name)["InstanceProfile"]
        return list(found.get("Tags") or [])
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        return None


def detect_managed_policies(iam: IAMClient, service: Service, wanted: Sequence[str]) -> ManagedDiff:
    """管理ポリシーの差分を検出する。ManagedPolicyArns に無いものは外す。"""
    current = attached_policies(iam, service)
    return ManagedDiff(
        attach=tuple(arn for arn in dict.fromkeys(wanted) if arn not in current),
        detach=tuple(arn for arn in current if arn not in wanted),
    )


def managed_policies(iam: IAMClient, service: Service, found: ManagedDiff) -> None:
    """管理ポリシーを、検出した差分で揃える。付けてから外す。"""
    for arn in found.attach:
        _ = iam.attach_role_policy(RoleName=service.name, PolicyArn=arn)
        msg.info(f"role {service.name}: attached {arn}")
    for arn in found.detach:
        _ = iam.detach_role_policy(RoleName=service.name, PolicyArn=arn)
        msg.info(f"role {service.name}: detached {arn}")


@dataclass(frozen=True)
class ProfileDiff:
    """インスタンスプロファイルの差分。

    exists でなければ作る。tags は足すタグ、roles は今入っているロール。
    """

    name: str
    exists: bool
    tags: tuple[tuple[str, str], ...] = ()
    roles: tuple[str, ...] = ()

    @property
    def others(self) -> tuple[str, ...]:
        """入っている別のロール。外して、ロール <名前> と入れ替える。"""
        return tuple(one for one in self.roles if one != self.name)

    def changes(self) -> list[Change]:
        """diff に出す形にする。"""
        if not self.exists:
            return [Change(ADD, INSTANCE_PROFILE, self.name)]
        found: list[Change] = []
        if self.tags:
            found.append(Change(UPDATE, INSTANCE_PROFILE, self.name, change.tags(self.tags)))
        if self.name not in self.roles:
            before = ",".join(self.others) or None
            detail = f"role: {change.moved(before, self.name)}"
            found.append(Change(UPDATE, INSTANCE_PROFILE, self.name, detail))
        return found


def detect_instance_profile(iam: IAMClient, service: Service) -> ProfileDiff:
    """インスタンスプロファイル <名前> の差分を検出する。別のサービスのタグが付いていれば
    止める。
    """
    try:
        found = iam.get_instance_profile(InstanceProfileName=service.name)["InstanceProfile"]
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        return ProfileDiff(service.name, exists=False)
    _owned("instance profile", found.get("Tags"), service)
    return ProfileDiff(
        service.name,
        exists=True,
        tags=tuple(_missing(found.get("Tags"), _mark(service))),
        roles=tuple(field(item, "RoleName") for item in found["Roles"]),
    )


def instance_profile(iam: IAMClient, service: Service, found: ProfileDiff) -> None:
    """インスタンスプロファイルを揃える。上のロールを 1 つだけ入れ、タグを付ける。

    別のロールが入っていれば、外してから入れる。インスタンスプロファイルに入れられる
    ロールは 1 つだけである。
    """
    if not found.exists:
        _ = iam.create_instance_profile(
            InstanceProfileName=service.name,
            Tags=[{"Key": key, "Value": value} for key, value in _mark(service)],
        )
        # IAM の反映を待つ。それでも直後の start が失敗することがある
        iam.get_waiter("instance_profile_exists").wait(InstanceProfileName=service.name)
        state = CREATED
    elif found.tags:
        _ = iam.tag_instance_profile(
            InstanceProfileName=service.name,
            Tags=[{"Key": key, "Value": value} for key, value in found.tags],
        )
        state = UPDATED
    else:
        state = UNCHANGED
    if service.name not in found.roles:
        for other in found.others:
            _ = iam.remove_role_from_instance_profile(
                InstanceProfileName=service.name, RoleName=other
            )
            msg.info(f"instance profile {service.name}: removed the role {other}")
        _ = iam.add_role_to_instance_profile(
            InstanceProfileName=service.name, RoleName=service.name
        )
        state = CREATED if state == CREATED else UPDATED
    _report(f"instance profile {service.name}", state)


def groups(ec2: EC2Client, service: Service) -> list[SecurityGroupTypeDef]:
    """名前が <名前> のセキュリティグループ。VPC は問わない。"""
    return list(
        ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [service.name]}]
        ).get("SecurityGroups", [])
    )


def _aws_cidr(value: str) -> str:
    """AWS が返した CIDR を、設定ファイルから読んだものと比べられる形に揃える。

    設定ファイルの値は読むときに揃えてある（conf の cidr）。AWS も CIDR を正規化して
    持つが、書き方が違っても同じものを指せるので、比べる前に両方を同じ形にする。
    読めない値は、設定ファイルの側と違って、そのまま別のものとして扱う。消す相手に
    なるだけなので、ここで止める理由が無い。
    """
    try:
        return str(ip_network(value))
    except ValueError:
        return value


def _is_ipv6(source: str) -> bool:
    """送信元が IPv6 の CIDR か。正規化した CIDR では、コロンを含むのは IPv6 だけである。"""
    return ":" in source


@dataclass(frozen=True)
class Rule:
    """受信規則 1 つ。設定ファイルの規則も AWS の規則も、AWS の表し方で持つ。

    protocol は tcp、udp、icmp、icmpv6 か、AWS が返したままのプロトコル（すべては -1）。
    ICMP のすべての種類は、low と high を -1 にして表す。source は正規化した CIDR か、
    他のセキュリティグループやプレフィックスリストの ID。rule_id は AWS の規則の ID で、
    設定ファイルの規則では空。
    """

    protocol: str
    low: int
    high: int
    source: str
    description: str = ""
    rule_id: str = ""

    @property
    def key(self) -> tuple[str, int, int, str]:
        """規則を見分ける組。AWS と同じく、説明は含めない。"""
        return self.protocol, self.low, self.high, self.source

    def port(self) -> str:
        """PORT 列の表示。設定ファイルに書ける規則は、sg list と同じ conf.port_text の形にする。

        書けない規則は <プロトコル>:<ポートの下限か ICMP の種類>:<上限かコード> と書く。
        すべてのプロトコルは all、IPv6 の送信元を持つ IPv4 の ICMP は icmpv4 と書き、設定
        ファイルの icmp と見分ける。ポートを持たないプロトコルは名前だけにする。
        """
        if self.protocol in (conf.TCP, conf.UDP):
            return conf.port_text(self.protocol, (self.low, self.high))
        written = self.protocol == _icmp(self.source)
        if written and self.low == self.high == ANY_TYPE:
            return conf.port_text(conf.ICMP, None)
        if self.protocol == ALL_PROTOCOLS:
            name = ALL
        elif written:
            name = conf.ICMP
        elif self.protocol == conf.ICMP:
            name = ICMPV4
        else:
            name = self.protocol
        if self.low == self.high == ANY_TYPE:
            return name
        return f"{name}:{self.low}:{self.high}"


def _icmp(source: str) -> str:
    """送信元に合う ICMP のプロトコル。IPv6 なら icmpv6。"""
    return ICMPV6 if _is_ipv6(source) else conf.ICMP


def wanted_rules(values: Conf) -> list[Rule]:
    """設定ファイルが定める受信規則。設定ファイルの順に並べる。"""
    rules: list[Rule] = []
    for one in values.security_group.ingress:
        if one.Port is None:
            rules.append(Rule(_icmp(one.Cidr), ANY_TYPE, ANY_TYPE, one.Cidr, one.Description))
        else:
            low, high = one.Port
            rules.append(Rule(one.Protocol, low, high, one.Cidr, one.Description))
    return rules


def _source(item: Mapping[str, Any]) -> str:
    """AWS の規則の送信元。CIDR、プレフィックスリスト、他のセキュリティグループの順に探す。"""
    for key in ("CidrIpv4", "CidrIpv6"):
        value = item.get(key)
        if value:
            return _aws_cidr(value)
    prefix_list = item.get("PrefixListId")
    if prefix_list:
        return prefix_list
    group = item.get("ReferencedGroupInfo") or {}
    return group.get("GroupId") or UNKNOWN_SOURCE


def current_rules(ec2: EC2Client, group_id: str) -> list[Rule]:
    """セキュリティグループの受信規則。AWS から読んだ順に並べる。送信規則は含めない。"""
    rules: list[Rule] = []
    pages = ec2.get_paginator("describe_security_group_rules").paginate(
        Filters=[{"Name": "group-id", "Values": [group_id]}]
    )
    for page in pages:
        for item in page.get("SecurityGroupRules", []):
            if item.get("IsEgress"):
                continue
            protocol = str(item.get("IpProtocol", ""))
            rules.append(
                Rule(
                    PROTOCOL_NAMES.get(protocol, protocol),
                    item.get("FromPort", ANY_TYPE),
                    item.get("ToPort", ANY_TYPE),
                    _source(item),
                    item.get("Description") or "",
                    field(item, "SecurityGroupRuleId"),
                )
            )
    return rules


@dataclass(frozen=True)
class Changes:
    """受信規則を設定ファイルに揃えるための変更。

    described は、説明だけが違う規則の、AWS の規則と設定ファイルの規則の組。added と
    described は設定ファイルの順、removed は AWS から読んだ順に並べる。
    """

    added: list[Rule]
    described: list[tuple[Rule, Rule]]
    removed: list[Rule]

    def __bool__(self) -> bool:
        return bool(self.added or self.described or self.removed)


def changes(wanted: Sequence[Rule], current: Sequence[Rule]) -> Changes:
    """設定ファイルの規則と AWS の規則を比べ、揃えるための変更を求める。"""
    found: dict[tuple[str, int, int, str], Rule] = {}
    for one in current:
        _ = found.setdefault(one.key, one)
    keys = {one.key for one in wanted}
    return Changes(
        added=[one for one in wanted if one.key not in found],
        described=[
            (found[one.key], one)
            for one in wanted
            if one.key in found and found[one.key].description != one.description
        ],
        removed=[one for one in current if one.key not in keys],
    )


def _permission(rule: Rule) -> IpPermissionTypeDef:
    """規則を authorize-security-group-ingress に渡す形にする。"""
    entry: dict[str, str] = {"CidrIpv6" if _is_ipv6(rule.source) else "CidrIp": rule.source}
    if rule.description:
        entry["Description"] = rule.description
    one: IpPermissionTypeDef = {
        "IpProtocol": rule.protocol,
        "FromPort": rule.low,
        "ToPort": rule.high,
    }
    if _is_ipv6(rule.source):
        one["Ipv6Ranges"] = [cast("Any", entry)]
    else:
        one["IpRanges"] = [cast("Any", entry)]
    return one


def _ingress(ec2: EC2Client, service: Service, group_id: str, found: Changes) -> bool:
    """受信規則を、検出した変更で揃える。変えたら True を返す。

    足してから説明を直し、最後に消す。揃えている間も、設定ファイルにある規則は通り
    続ける。変えた規則を 1 つ 1 行で知らせる。
    """
    name = service.name
    if found.added:
        _ = ec2.authorize_security_group_ingress(
            GroupId=group_id, IpPermissions=[_permission(one) for one in found.added]
        )
        for one in found.added:
            msg.info(f"security group {name}: added {_label(one)}")
    if found.described:
        entries: list[Any] = []
        for current_rule, wanted in found.described:
            entry = {"SecurityGroupRuleId": current_rule.rule_id}
            # 説明を省けば、その規則の説明は消える
            if wanted.description:
                entry["Description"] = wanted.description
            entries.append(entry)
        _ = ec2.update_security_group_rule_descriptions_ingress(
            GroupId=group_id, SecurityGroupRuleDescriptions=entries
        )
        for current_rule, wanted in found.described:
            before, after = shown_description(current_rule), shown_description(wanted)
            msg.info(
                f"security group {name}: updated the description of {current_rule.port()} "
                f"{current_rule.source}: {before} -> {after}"
            )
    if found.removed:
        _ = ec2.revoke_security_group_ingress(
            GroupId=group_id, SecurityGroupRuleIds=[one.rule_id for one in found.removed]
        )
        for one in found.removed:
            msg.info(f"security group {name}: removed {_label(one)}")
    return bool(found)


def shown_description(rule: Rule) -> str:
    """説明の表示。無ければ table.EMPTY。"""
    return rule.description or table.EMPTY


def _label(rule: Rule) -> str:
    """知らせる行に書く規則。PORT、送信元、説明。"""
    return f"{rule.port()} {rule.source} {shown_description(rule)}"


def rule_changes(found: Changes) -> list[Change]:
    """受信規則の変更を、diff に出す形にする。

    名前に PORT を、違いに送信元と説明を書く。送信元は幅を揃える。並びは changes と
    同じく、足す、説明を直す、消すの順である。
    """
    rules = [*found.added, *(now for now, _ in found.described), *found.removed]
    width = max((len(one.source) for one in rules), default=0)

    def line(mark: str, rule: Rule, description: str) -> Change:
        return Change(
            mark, INGRESS, rule.port(), f"{rule.source.ljust(width)}{table.GAP}{description}"
        )

    return [
        *(line(ADD, one, shown_description(one)) for one in found.added),
        *(
            line(UPDATE, now, f"{shown_description(now)}{ARROW}{shown_description(wanted)}")
            for now, wanted in found.described
        ),
        *(line(REMOVE, one, shown_description(one)) for one in found.removed),
    ]


@dataclass(frozen=True)
class GroupDiff:
    """セキュリティグループの差分。

    group_id は vpc_id にある <名前> のセキュリティグループで、無ければ None で、作る。
    tags は足すタグ、rules は受信規則の変更。
    """

    name: str
    vpc_id: str
    group_id: str | None
    rules: Changes
    tags: tuple[tuple[str, str], ...] = ()

    def changes(self) -> list[Change]:
        """diff に出す形にする。"""
        found: list[Change] = []
        if self.group_id is None:
            found.append(Change(ADD, SECURITY_GROUP, self.name, f"in {self.vpc_id}"))
        elif self.tags:
            found.append(Change(UPDATE, SECURITY_GROUP, self.name, change.tags(self.tags)))
        return found + rule_changes(self.rules)


def detect_security_group(ec2: EC2Client, service: Service, values: Conf, vpc_id: str) -> GroupDiff:
    """セキュリティグループの差分を検出する。

    設定ファイルの SubnetId が属する VPC のものを比べる。同じ名前は VPC ごとに 1 つ
    までなので、別の VPC にある同じ名前は別のセキュリティグループであり、見ない。消す
    のは destroy である。セキュリティグループが無ければ、すべての規則を足す。
    """
    wanted = wanted_rules(values)
    found = [group for group in groups(ec2, service) if field(group, "VpcId") == vpc_id]
    if not found:
        return GroupDiff(service.name, vpc_id, None, changes(wanted, []))
    group = found[0]
    group_id = field(group, "GroupId")
    return GroupDiff(
        service.name,
        vpc_id,
        group_id,
        changes(wanted, current_rules(ec2, group_id)),
        tuple(_missing(group.get("Tags"), _pairs(service))),
    )


def security_group(ec2: EC2Client, service: Service, found: GroupDiff) -> str:
    """セキュリティグループを、検出した差分で揃え、その ID を返す。"""
    if found.group_id is None:
        group_id = field(
            ec2.create_security_group(
                GroupName=service.name,
                Description=GROUP_DESCRIPTION,
                VpcId=found.vpc_id,
                TagSpecifications=[{"ResourceType": "security-group", "Tags": _tags(service)}],
            ),
            "GroupId",
        )
        _ = _ingress(ec2, service, group_id, found.rules)
        msg.info(f"security group {service.name}: {CREATED} ({group_id})")
        return group_id
    group_id = found.group_id
    state = UNCHANGED
    # 無いタグと値の違うタグを書く。create_tags は同じキーの値を上書きする。他のタグには
    # 触らない
    if found.tags:
        _ = ec2.create_tags(
            Resources=[group_id], Tags=[{"Key": key, "Value": value} for key, value in found.tags]
        )
        state = UPDATED
    if _ingress(ec2, service, group_id, found.rules):
        state = UPDATED
    _report(f"security group {service.name}", state, f" ({group_id})")
    return group_id


def _pairs(service: Service) -> list[tuple[str, str]]:
    """インスタンスとそのボリュームとネットワークインターフェース、セキュリティ
    グループに付けるタグ。キーと値の組で持つ。
    """
    return [("Name", service.name), *_mark(service)]


def _tags(service: Service) -> list[TagTypeDef]:
    """_pairs を EC2 の形にしたもの。"""
    return [{"Key": key, "Value": value} for key, value in _pairs(service)]


def _warnings(result: Mapping[str, Any], name: str, *, profile_aligned: bool) -> None:
    """AWS が返した起動テンプレートの警告を伝える。

    AWS は作るときに中身を最後まで検査せず、問題があれば Warning で返す。捨てると
    次の start まで分からないので、そのまま見せる。profile_aligned（apply の update が
    直前にインスタンスプロファイルを揃えた）なら、プロファイルが無いという警告は、
    揃えたものが見えていないだけなので、そう添える。bake はプロファイルを確かめない
    ので添えない。手で消されていれば、待っても直らないからである。
    """
    warning: Mapping[str, Any] = result.get("Warning") or {}
    for error in warning.get("Errors") or []:
        code = error.get("Code")
        note = f" ({NOT_READY_NOTE})" if profile_aligned and code == PROFILE_NOT_READY else ""
        msg.warn(
            f"launch template {name}: {code}: {table.printable(str(error.get('Message')))}{note}"
        )


def _version(number: int | None, name: str) -> int:
    """作ったバージョンの番号。AWS が返さなければエラーにする。"""
    if number is None:
        raise TemtError(f"AWS did not return the version number of the launch template {name}")
    return number


def _delete_versions(ec2: EC2Client, name: str, numbers: list[str]) -> None:
    """バージョンを消す。消せなかったものは理由を添えて知らせ、処理は続ける。

    ここに来る時点でテンプレートは新しい既定バージョンに切り替わっている。消し残しは
    次の start にも ssh の設定にも響かないので、呼び出しそのものを断られたときも
    知らせるだけにして、apply や bake の残りの手順を続ける。1 回に渡せる数には上限が
    あるので、VERSIONS_PER_CALL ずつに分けて頼む。
    """
    for start in range(0, len(numbers), VERSIONS_PER_CALL):
        _delete_some_versions(ec2, name, numbers[start : start + VERSIONS_PER_CALL])


def _delete_some_versions(ec2: EC2Client, name: str, numbers: list[str]) -> None:
    """_delete_versions の 1 回分。"""
    try:
        deleted = ec2.delete_launch_template_versions(LaunchTemplateName=name, Versions=numbers)
    except ClientError as e:
        msg.warn(
            f"launch template {name}: cannot delete the old versions {', '.join(numbers)}: "
            f"{table.aws_error(e)}"
        )
        return
    for one in deleted.get("UnsuccessfullyDeletedLaunchTemplateVersions") or []:
        error: Mapping[str, Any] = one.get("ResponseError") or {}
        msg.warn(
            f"launch template {name}: cannot delete version {one.get('VersionNumber')}: "
            f"{error.get('Code')}: {table.printable(str(error.get('Message')))}"
        )


def not_applied(service: Service) -> TemtError:
    """起動テンプレートが無いときの誤り。apply されていないか、destroy 済みである。"""
    return TemtError(
        f"no launch template {service.launch_template}; run 'temt apply --svc {service.svc}' first"
    )


def has_template(ec2: EC2Client, service: Service) -> bool:
    """起動テンプレートがあるか。"""
    try:
        _ = ec2.describe_launch_templates(LaunchTemplateNames=[service.launch_template])
    except ClientError as e:
        if error_code(e) != NO_SUCH_TEMPLATE:
            raise
        return False
    return True


def require_template(ec2: EC2Client, service: Service) -> None:
    """起動テンプレートがあることを確かめる。無ければ apply を促して終わる。

    apply 済みを前提とするコマンドが、AWS に対する最初の操作の前に呼ぶ。
    """
    if not has_template(ec2, service):
        raise not_applied(service)


def versions(ec2: EC2Client, service: Service) -> list[Mapping[str, Any]]:
    """起動テンプレートのすべてのバージョン。無ければ apply を促して終わる。"""
    name = service.launch_template
    try:
        return [
            one
            for page in ec2.get_paginator("describe_launch_template_versions").paginate(
                LaunchTemplateName=name
            )
            for one in page.get("LaunchTemplateVersions", [])
        ]
    except ClientError as e:
        if error_code(e) != NO_SUCH_TEMPLATE:
            raise
        raise not_applied(service) from None


@dataclass(frozen=True)
class TemplateVersion:
    """起動テンプレートの 1 つのバージョン。AMI を使っているものとして挙げる。"""

    template: str
    number: Any

    def __str__(self) -> str:
        """メッセージや詳細表示での書き方。"""
        return f"{self.template} version {self.number}"

    @property
    def cell(self) -> str:
        """list の表での書き方。表を awk などで扱えるよう、空白を含めない。

        起動テンプレートの名前には : も , も使えないので、区切りを取り違えない。
        """
        return f"{self.template}:{self.number}"


def cells(versions: Sequence[TemplateVersion] | None) -> str:
    """list の表の USED の値。空白を含めず , でつなぐ。無ければ空。"""
    return ",".join(one.cell for one in versions or [])


def all_versions(ec2: EC2Client) -> Iterator[tuple[str, Sequence[Mapping[str, Any]]]]:
    """そのアカウントとリージョンにあるすべての起動テンプレートの、すべてのバージョン。

    temt のものに限らない。テンプレートごとに、名前とバージョンの組を返す。読むあいだに
    消えたテンプレートは飛ばす。
    """
    for page in ec2.get_paginator("describe_launch_templates").paginate():
        for template in page.get("LaunchTemplates", []):
            name = field(template, "LaunchTemplateName")
            try:
                found = [
                    one
                    for versions in ec2.get_paginator("describe_launch_template_versions").paginate(
                        LaunchTemplateName=name
                    )
                    for one in versions.get("LaunchTemplateVersions", [])
                ]
            except ClientError as e:
                if error_code(e) != NO_SUCH_TEMPLATE:
                    raise
                continue
            yield name, found


def default_version(found: Sequence[Mapping[str, Any]], service: Service) -> Mapping[str, Any]:
    """既定バージョン。1 つに決まらなければエラーにする。"""
    defaults = [one for one in found if one.get("DefaultVersion")]
    if len(defaults) != 1:
        raise TemtError(
            f"the launch template {service.launch_template} has no single default version"
        )
    return defaults[0]


def default_image(ec2: EC2Client, service: Service) -> str:
    """起動テンプレートの既定バージョンの ImageId。無ければ apply を促して終わる。"""
    data = default_version(versions(ec2, service), service).get("LaunchTemplateData") or {}
    return str(data.get("ImageId") or "")


def point_image(ec2: EC2Client, service: Service, image_id: str) -> None:
    """起動テンプレートの既定バージョンの ImageId だけを替える。

    既定バージョンを元に ImageId だけを替えた新しいバージョンを作って既定にし、
    それ以外のバージョンをすべて消す。既定バージョンとは比べない。
    """
    name = service.launch_template
    found = versions(ec2, service)
    number = _version(default_version(found, service).get("VersionNumber"), name)
    # 消す相手は、作る前に読んだもの
    others = [str(_version(one.get("VersionNumber"), name)) for one in found]
    added = ec2.create_launch_template_version(
        LaunchTemplateName=name,
        SourceVersion=str(number),
        LaunchTemplateData={"ImageId": image_id},
    )
    made = _version(added.get("LaunchTemplateVersion", {}).get("VersionNumber"), name)
    _ = ec2.modify_launch_template(LaunchTemplateName=name, DefaultVersion=str(made))
    msg.info(f"launch template {name}: {UPDATED} (version {made}, {image_id})")
    _warnings(added, name, profile_aligned=False)
    _delete_versions(ec2, name, others)


@dataclass(frozen=True)
class TemplateDiff:
    """起動テンプレートの差分。

    wanted は揃える中身（ltbuild.build）。current は既定バージョンの中身で、テンプレートが
    無ければ None で、作る。number は既定バージョンの番号、versions は今あるバージョンの
    番号。tags は、テンプレート自身に足すタグで、template_id はそのテンプレートの ID。
    """

    name: str
    wanted: Mapping[str, Any]
    current: Mapping[str, Any] | None = None
    number: int | None = None
    versions: tuple[int, ...] = ()
    template_id: str = ""
    tags: tuple[tuple[str, str], ...] = ()

    @property
    def changed(self) -> bool:
        """新しいバージョンを作るか。中身全体を、AWS が読み出しで変える形を揃えて比べる。"""
        return self.current is None or not ltdata.same(
            ltdata.normalize(self.current), ltdata.normalize(self.wanted)
        )

    @property
    def leftovers(self) -> tuple[int, ...]:
        """新しいバージョンを作らないときに消す、既定以外のバージョン。

        既定を切り替えた直後に中断したときや、消せなかったときに残る。temt 以外が
        作ったものも含む。新しいバージョンを作るときは、既定を切り替えた後に、これも
        含めて既定以外をすべて消す。
        """
        if self.changed:
            return ()
        return tuple(one for one in self.versions if one != self.number)

    def changes(self) -> list[Change]:
        """diff に出す形にする。違う項目を 1 つ 1 行で出す。"""
        if self.current is None:
            return [Change(ADD, LAUNCH_TEMPLATE, self.name)]
        found: list[Change] = []
        if self.tags:
            found.append(Change(UPDATE, LAUNCH_TEMPLATE, self.name, change.tags(self.tags)))
        if not self.changed:
            if self.leftovers:
                numbers = ", ".join(str(one) for one in self.leftovers)
                found.append(
                    Change(UPDATE, LAUNCH_TEMPLATE, self.name, f"delete other versions: {numbers}")
                )
            return found
        items = [
            Change(UPDATE, LAUNCH_TEMPLATE, self.name, detail, body)
            for detail, body in template_changes(self.current, self.wanted)
        ]
        # 開いた項目では違いが見えないときも、新しいバージョンを作ることは出す
        return found + (items or [Change(UPDATE, LAUNCH_TEMPLATE, self.name, "new version")])


def template_differ(current: Mapping[str, Any], wanted: Mapping[str, Any]) -> list[str]:
    """2 つの中身で違う項目の位置。bake が、揃っていない項目を挙げるのに使う。"""
    now = ltdata.leaves(ltdata.normalize(current))
    then = ltdata.leaves(ltdata.normalize(wanted))
    return [
        key
        for key in [*then, *(key for key in now if key not in then)]
        if not ltdata.same(now.get(key), then.get(key))
    ]


def _tables(value: Any) -> bool:
    """値がテーブルの配列か。対応づけのキーの無いもの（LicenseSpecifications など）。"""
    return isinstance(value, list) and bool(value) and all(isinstance(one, dict) for one in value)


def template_changes(
    current: Mapping[str, Any], wanted: Mapping[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    """2 つの中身の違いを、項目の位置ごとに、違いの文と続く行の組で返す。

    位置は ltdata.leaves の書き方。UserData は base64 を戻した文書を unified diff で
    続く行に出し、文字として読めなければバイト数だけを出す。対応づけのキーの無い
    テーブルの配列は、要素を中身全体で比べ、消える要素と足される要素を 1 つ 1 行で出す。
    """
    now = ltdata.leaves(ltdata.normalize(current))
    then = ltdata.leaves(ltdata.normalize(wanted))
    found: list[tuple[str, tuple[str, ...]]] = []
    for key in [*then, *(key for key in now if key not in then)]:
        before, after = now.get(key), then.get(key)
        if ltdata.same(before, after):
            continue
        if key == ltdata.USER_DATA:
            found.append(_user_data_change(key, before, after))
            continue
        if _tables(before) or _tables(after):
            old = before if isinstance(before, list) else []
            new = after if isinstance(after, list) else []
            found += [
                (f"{key}: {ltdata.shown(one)}{ARROW}-", ())
                for one in old
                if not any(ltdata.same(one, other) for other in new)
            ]
            found += [
                (f"{key}: -{ARROW}{ltdata.shown(one)}", ())
                for one in new
                if not any(ltdata.same(one, other) for other in old)
            ]
            continue
        found.append((f"{key}: {ltdata.shown(before)}{ARROW}{ltdata.shown(after)}", ()))
    return found


def _user_data_change(key: str, before: Any, after: Any) -> tuple[str, tuple[str, ...]]:
    """UserData の違い。両側とも（無い側は空の文書として）文字なら unified diff にする。"""
    lines = [[] if one is None else ltdata.user_data_lines(one) for one in (before, after)]
    if lines[0] is not None and lines[1] is not None:
        body = change.lines_diff(lines[0], lines[1])
        return f"{key}: {_presence(before)}{ARROW}{_presence(after)}", body
    sizes = [
        f"{ltdata.user_data_size(one)} bytes" if one is not None else "-" for one in (before, after)
    ]
    return f"{key}: {sizes[0]}{ARROW}{sizes[1]}", ()


def _presence(value: Any) -> str:
    """UserData の有無の表示。中身は続く行に出す。"""
    return "-" if value is None else "(text)"


def detect_template(ec2: EC2Client, service: Service, wanted: Mapping[str, Any]) -> TemplateDiff:
    """起動テンプレートの差分を検出する。

    wanted は ltbuild.build が組み立てた中身で、temt のセキュリティグループがまだ無ければ
    その ID は ltbuild.NEW_GROUP になっている。既定バージョンの中身全体と、テンプレート
    自身のタグを比べる。バージョンの中の TagSpecifications は起動したインスタンスに
    付くもので、テンプレート自身のタグとは別である。
    """
    name = service.launch_template
    try:
        versions = [
            one
            for page in ec2.get_paginator("describe_launch_template_versions").paginate(
                LaunchTemplateName=name
            )
            for one in page.get("LaunchTemplateVersions", [])
        ]
    except ClientError as e:
        if error_code(e) != NO_SUCH_TEMPLATE:
            raise
        return TemplateDiff(name, wanted)
    current = default_version(versions, service)
    found = ec2.describe_launch_templates(LaunchTemplateNames=[name]).get("LaunchTemplates")
    # 名前で引いて無ければ AWS は誤りを返すので、空の応答は ID が無いものとして扱う
    described = (found or [{}])[0]
    differ = tuple(_missing(described.get("Tags"), _mark(service)))
    return TemplateDiff(
        name,
        wanted,
        current.get("LaunchTemplateData") or {},
        current.get("VersionNumber"),
        tuple(_version(one.get("VersionNumber"), name) for one in versions),
        field(described, "LaunchTemplateId") if differ else "",
        differ,
    )


def template(
    ec2: EC2Client, service: Service, found: TemplateDiff, group_id: str | None, *, managed: bool
) -> None:
    """起動テンプレートを、検出した差分で揃える。違いがあれば新しいバージョンを作り、
    既定にする。

    新しいバージョンを既定にしたら、それ以外のバージョンをすべて消す。作らないときも、
    既定以外のバージョンが残っていれば消す。group_id は揃えた temt のセキュリティ
    グループの ID で、検出したときにまだ無く NEW_GROUP で比べていれば、それをこの ID に
    置き換えて書く。managed は、temt のインスタンスプロファイルを直前に揃えたか。
    """
    name = found.name
    wanted: Any = ltbuild.with_group(found.wanted, group_id) if group_id else dict(found.wanted)
    if found.current is None:
        made = ec2.create_launch_template(
            LaunchTemplateName=name,
            LaunchTemplateData=wanted,
            TagSpecifications=[
                {
                    "ResourceType": "launch-template",
                    "Tags": [{"Key": key, "Value": value} for key, value in _mark(service)],
                }
            ],
        )
        number = _version(made["LaunchTemplate"].get("DefaultVersionNumber"), name)
        msg.info(f"launch template {name}: {CREATED} (version {number})")
        _warnings(made, name, profile_aligned=managed)
        return
    if found.tags:
        _ = ec2.create_tags(
            Resources=[found.template_id],
            Tags=[{"Key": key, "Value": value} for key, value in found.tags],
        )
    if not found.changed:
        state = UPDATED if found.tags or found.leftovers else UNCHANGED
        _report(f"launch template {name}", state, f" (version {found.number})")
        if found.leftovers:
            _delete_versions(ec2, name, [str(one) for one in found.leftovers])
        return
    added = ec2.create_launch_template_version(LaunchTemplateName=name, LaunchTemplateData=wanted)
    number = _version(added.get("LaunchTemplateVersion", {}).get("VersionNumber"), name)
    _ = ec2.modify_launch_template(LaunchTemplateName=name, DefaultVersion=str(number))
    msg.info(f"launch template {name}: {UPDATED} (version {number})")
    _warnings(added, name, profile_aligned=managed)
    # 消す相手は、検出したときに控えたもの
    _delete_versions(ec2, name, [str(one) for one in found.versions if one != number])


@dataclass(frozen=True)
class IamDiff:
    """temt が管理するインスタンスロールとインスタンスプロファイル <名前> の差分。"""

    role: RoleDiff
    policies: tuple[PolicyDiff, ...]
    managed: ManagedDiff
    profile: ProfileDiff

    def changes(self) -> list[Change]:
        """diff に出す形にする。揃える順に並べる。"""
        found = self.role.changes()
        for one in self.policies:
            found += one.changes()
        return found + self.managed.changes() + self.profile.changes()


@dataclass(frozen=True)
class Found:
    """手順 5 のリソースの差分。

    iam は、IamInstanceProfile が temt のものを指すときだけある。group は、起動
    テンプレートが temt のセキュリティグループを使うときだけある。
    """

    iam: IamDiff | None
    group: GroupDiff | None
    template: TemplateDiff

    def changes(self) -> list[Change]:
        """diff に出す形にする。揃える順に並べる。"""
        found = [] if self.iam is None else self.iam.changes()
        if self.group is not None:
            found += self.group.changes()
        return found + self.template.changes()


def detect(
    ec2: EC2Client,
    iam: IAMClient,
    service: Service,
    values: Conf,
    built: ltbuild.Built,
    *,
    policies: Mapping[str, Mapping[str, Any]] | None,
) -> Found:
    """リソースの差分を、揃える順に検出する。

    built は手順 4 で組み立てた起動テンプレートの中身。policies は、手順 4 で検査した、
    インラインポリシーの名前から中身への対応で、temt のインスタンスプロファイルを
    使わないときは None。そのときインスタンスロールとインスタンスプロファイルは見ない。
    セキュリティグループは、起動テンプレートがそれを使うときだけ見る。
    """
    iam_found = None
    if policies is not None:
        iam_found = IamDiff(
            detect_role(iam, service),
            tuple(detect_role_policies(iam, service, policies)),
            detect_managed_policies(iam, service, values.role.ManagedPolicyArns),
            detect_instance_profile(iam, service),
        )
    group = None
    if built.uses_group and built.vpc_id is not None:
        group = detect_security_group(ec2, service, values, built.vpc_id)
    return Found(iam_found, group, detect_template(ec2, service, built.data))


def update(ec2: EC2Client, iam: IAMClient, service: Service, found: Found) -> None:
    """detect が検出した差分で、リソースを順に揃える。

    起動テンプレートには、揃えた temt のセキュリティグループの ID を入れる。
    """
    if found.iam is not None:
        role(iam, service, found.iam.role)
        role_policies(iam, service, found.iam.policies)
        managed_policies(iam, service, found.iam.managed)
        instance_profile(iam, service, found.iam.profile)
    group_id = None if found.group is None else security_group(ec2, service, found.group)
    template(ec2, service, found.template, group_id, managed=found.iam is not None)


def delete_template(ec2: EC2Client, service: Service) -> None:
    """起動テンプレートを消す。無ければ absent と知らせる。"""
    name = service.launch_template
    try:
        _ = ec2.delete_launch_template(LaunchTemplateName=name)
    except ClientError as e:
        if error_code(e) != NO_SUCH_TEMPLATE:
            raise
        msg.info(f"launch template {name}: {ABSENT}")
        return
    msg.info(f"launch template {name}: {DELETED}")


def delete_parameters(ssm: SSMClient, service: Service) -> None:
    """/<名前> の下の SSM パラメータをすべて消す。無ければ absent と知らせる。

    アプリの値と記録は temt のタグを持たないことがあるので、タグではなくパスで探す。
    """
    path = service.parameter_path
    names = [
        field(one, "Name")
        for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=path, Recursive=True)
        for one in page.get("Parameters", [])
    ]
    if not names:
        msg.info(f"parameters under {path}/: {ABSENT}")
        return
    deleted = 0
    for start in range(0, len(names), PARAMETER_BATCH):
        # 読んでから消すまでに消えたものは InvalidParameters に返る。消えていれば
        # よいので誤りにはせず、消した数にも入れない
        done = ssm.delete_parameters(Names=names[start : start + PARAMETER_BATCH])
        deleted += len(done.get("DeletedParameters") or [])
    msg.info(f"parameters under {path}/: {DELETED} ({deleted})")


def _delete_group(ec2: EC2Client, service: Service, group_id: str) -> None:
    """セキュリティグループを 1 つ消す。

    destroy はインスタンスが terminated になるまで待つので、そのネットワーク
    インターフェースはその時点で外れている（実機で確かめた）。外れていなければ削除は
    DependencyViolation で断られる。他のセキュリティグループの規則などが参照していても
    同じ誤りになり、AWS の文では見分けられない。どちらも待たずに、AWS の文に両方の
    直し方を添えて止める。destroy はやり直すと続きから進む。
    """
    try:
        _ = ec2.delete_security_group(GroupId=group_id)
    except ClientError as e:
        code = error_code(e)
        if code == DEPENDENCY_VIOLATION:
            raise TemtError(
                f"cannot delete the security group {group_id}: {table.aws_error(e)}; "
                "if its instances were just terminated, run destroy again; otherwise remove "
                "what uses it and run destroy again"
            ) from None
        if code != NO_SUCH_GROUP:
            raise
        state = ABSENT
    else:
        state = DELETED
    msg.info(f"security group {service.name} ({group_id}): {state}")


def delete_security_groups(ec2: EC2Client, service: Service) -> None:
    """名前が <名前> のセキュリティグループを、VPC を問わずすべて消す。

    SubnetId を別の VPC に変えたあと古い VPC に残ったものも、ここで消える。
    1 つも無ければ absent と知らせる。
    """
    group_ids = sorted(field(one, "GroupId") for one in groups(ec2, service))
    if not group_ids:
        msg.info(f"security group {service.name}: {ABSENT}")
        return
    for group_id in group_ids:
        _delete_group(ec2, service, group_id)


# temt のタグが無いか別のサービスのもので、消さなかったときの表示
LEFT = "left (not tagged temt:service={svc})"


def _ours(tags: Sequence[Mapping[str, Any]] | None, service: Service) -> bool:
    """temt:service=<svc> のタグが付いているか。消してよいのはそれだけである。"""
    return aws.tag({"Tags": tags or []}, aws.TAG_SERVICE) == service.svc


def profile_roles(iam: IAMClient, service: Service) -> list[str]:
    """インスタンスプロファイル <名前> に入っているロール。無ければ空。"""
    try:
        found = iam.get_instance_profile(InstanceProfileName=service.name)
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        return []
    return [field(item, "RoleName") for item in found["InstanceProfile"]["Roles"]]


def delete_instance_profile(iam: IAMClient, service: Service, *, command: str = "destroy") -> bool:
    """インスタンスプロファイル <名前> を消す。無ければ absent と知らせる。

    temt:service=<svc> が付いているものだけを消す。取り込んだ元のインスタンスプロファイル
    など、ほかの名前のものは、そもそも対象にならない。入れたロール <名前> だけを外す。
    他のロールが入っていれば、それを挙げて終了する。command はやり直すよう告げる
    コマンド。消したか、もともと無ければ True、タグが違って残せば False を返す。
    """
    try:
        found = iam.get_instance_profile(InstanceProfileName=service.name)
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        msg.info(f"instance profile {service.name}: {ABSENT}")
        return True
    if not _ours(found["InstanceProfile"].get("Tags"), service):
        msg.info(f"instance profile {service.name}: {LEFT.format(svc=service.svc)}")
        return False
    roles = [field(item, "RoleName") for item in found["InstanceProfile"]["Roles"]]
    others = [name for name in roles if name != service.name]
    if others:
        raise TemtError(
            f"the instance profile {service.name} holds another role: {others[0]}; "
            f"remove it by hand and run {command} again"
        )
    if service.name in roles:
        _ = iam.remove_role_from_instance_profile(
            InstanceProfileName=service.name, RoleName=service.name
        )
    try:
        _ = iam.delete_instance_profile(InstanceProfileName=service.name)
    except ClientError as e:
        # ロールを外した直後は、IAM に反映されるまで同じ誤りになることがある。読んだ後に
        # 足されたときもここに来る。どちらか分からないので、AWS の文を添える
        if error_code(e) != DELETE_CONFLICT:
            raise
        raise TemtError(
            f"cannot delete the instance profile {service.name}: {table.aws_error(e)}; "
            f"IAM may not show the role removed yet, so run {command} again; if this persists, "
            "remove the role from it by hand"
        ) from None
    msg.info(f"instance profile {service.name}: {DELETED}")
    return True


def inline_policies(iam: IAMClient, service: Service) -> list[str]:
    """ロールに付いたインラインポリシーの名前。ロールが無ければ空。"""
    try:
        return [
            name
            for page in iam.get_paginator("list_role_policies").paginate(RoleName=service.name)
            for name in page["PolicyNames"]
        ]
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        return []


def other_profiles(iam: IAMClient, service: Service, *, own_gone: bool = True) -> list[str]:
    """インスタンスロール <名前> を入れた、ほかのインスタンスプロファイル。

    ロールが無ければ空。インスタンスプロファイル <名前> は、own_gone（直前に消したか、
    もともと無い）なら数えない。IAM の反映が遅れて一覧に残ることがあるからである。
    タグが違って残したときは数える。ロールを消せないので、何も外さずに止めるためである。
    """
    try:
        return [
            field(one, "InstanceProfileName")
            for page in iam.get_paginator("list_instance_profiles_for_role").paginate(
                RoleName=service.name
            )
            for one in page["InstanceProfiles"]
            if not own_gone or one.get("InstanceProfileName") != service.name
        ]
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        return []


def delete_role(
    iam: IAMClient, service: Service, *, command: str = "destroy", own_gone: bool = True
) -> None:
    """インスタンスロール <名前> を消す。無ければ absent と知らせる。

    temt:service=<svc> が付いているものだけを消す。別のインスタンスプロファイルにも
    入っていれば、何も外さず消さずに終了する。管理ポリシーを外し（管理ポリシーそのものは
    temt が作ったものではないので残る）、インラインポリシーは、temt-<ポリシー名> か手で
    付けたものかを問わず、すべて消してから消す。IAM の DeleteRole は、どちらかが残って
    いると DeleteConflict で断る。
    """
    try:
        found = iam.get_role(RoleName=service.name)["Role"]
    except ClientError as e:
        if error_code(e) != NO_SUCH_ENTITY:
            raise
        msg.info(f"role {service.name}: {ABSENT}")
        return
    if not _ours(found.get("Tags"), service):
        msg.info(f"role {service.name}: {LEFT.format(svc=service.svc)}")
        return
    others = other_profiles(iam, service, own_gone=own_gone)
    if others:
        raise TemtError(
            f"the role {service.name} is also in the instance profile {others[0]}; "
            f"remove it from there by hand and run {command} again"
        )
    _strip_role(iam, service, command)


def _strip_role(iam: IAMClient, service: Service, command: str) -> None:
    """インスタンスロール <名前> の管理ポリシーを外し、インラインポリシーを消してから、
    ロールを消す。消してよいことは、呼ぶ側が確かめてある。
    """
    for arn in attached_policies(iam, service):
        _ = iam.detach_role_policy(RoleName=service.name, PolicyArn=arn)
        msg.info(f"role {service.name}: detached {arn}")
    for name in inline_policies(iam, service):
        try:
            _ = iam.delete_role_policy(RoleName=service.name, PolicyName=name)
        except ClientError as e:
            # 読んでから消すまでに消えていれば、消えていればよいので続ける
            if error_code(e) != NO_SUCH_ENTITY:
                raise
            continue
        msg.info(f"role policy {name}: {DELETED}")
    try:
        _ = iam.delete_role(RoleName=service.name)
    except ClientError as e:
        code = error_code(e)
        if code == NO_SUCH_ENTITY:
            msg.info(f"role {service.name}: {ABSENT}")
            return
        # インスタンスプロファイルから外した直後は、IAM に反映されるまで同じ誤りになる
        # ことがある。読んだ後に付けられたときもここに来る。どちらか分からないので、AWS
        # の文を添える
        if code != DELETE_CONFLICT:
            raise
        raise TemtError(
            f"cannot delete the role {service.name}: {table.aws_error(e)}; if nothing is "
            f"attached to it, IAM may not show the change yet, so run {command} again; "
            f"otherwise detach it by hand and run {command} again"
        ) from None
    msg.info(f"role {service.name}: {DELETED}")


def delete_iam(iam: IAMClient, service: Service) -> None:
    """destroy の、インスタンスプロファイルとインスタンスロール <名前> を消すところ。

    どちらも temt:service=<svc> が付いているものだけを消す。インスタンスロールが
    ほかのインスタンスプロファイルにも入っていれば、何も外さず消さずに、インスタンス
    プロファイルも消さずに止める。タグが違って残したインスタンスプロファイル <名前> に
    入っているときも、ロールに手を付けずに止める。
    """
    tags = read_tags(iam, "role", service)
    ours = tags is not None and _ours(tags, service)
    profiles = other_profiles(iam, service, own_gone=False) if ours else []
    others = [name for name in profiles if name != service.name]
    if others:
        raise TemtError(
            f"the role {service.name} is also in the instance profile {others[0]}; "
            f"remove it from there by hand and run destroy again"
        )
    gone = delete_instance_profile(iam, service)
    if tags is None:
        msg.info(f"role {service.name}: {ABSENT}")
        return
    if not ours:
        msg.info(f"role {service.name}: {LEFT.format(svc=service.svc)}")
        return
    if not gone and service.name in profiles:
        raise TemtError(
            f"the role {service.name} is also in the instance profile {service.name}, which "
            f"is left as it is not tagged {aws.TAG_SERVICE}={service.svc}; remove the role "
            f"from it by hand and run destroy again"
        )
    _strip_role(iam, service, "destroy")


def remove(ec2: EC2Client, iam: IAMClient, ssm: SSMClient, service: Service) -> None:
    """起動テンプレート、/<名前> の下の SSM パラメータ、セキュリティグループ、
    インスタンスプロファイル、IAM ロールを、この順に消す。

    無いものは飛ばして absent と知らせ、エラーにしない。途中で失敗すればそこで止まる
    ので、もう一度実行すれば、消えたものを飛ばして続きから進む。
    """
    delete_template(ec2, service)
    delete_parameters(ssm, service)
    delete_security_groups(ec2, service)
    delete_iam(iam, service)
