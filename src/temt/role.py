"""role import と role delete の本体。design/commands/role.md。

role import は、指したインスタンスロールの写しを temt の管理（<名前>）に移すよう、設定
ファイルの [role] と IamInstanceProfile を書き換える。AWS には何もせず、diff で確かめて
apply で反映する。role delete は、temt のインスタンスロールとインスタンスプロファイル
<名前> を消す。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

import tomlkit
from botocore.exceptions import ClientError
from tomlkit.items import Table

from temt import aws, conf, instances, ltdata, msg, resources
from temt.aws import error_code, field
from temt.msg import TemtError
from temt.names import LABEL_RULE, PREFIX, defaults_path, is_label, replace_text
from temt.table import printable, tilde

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from types_boto3_ec2.client import EC2Client
    from types_boto3_iam.client import IAMClient

    from temt.names import Service

# ポリシー名に使えない文字。英小文字、数字、ハイフンのほかはハイフンにする
NOT_LABEL_RE = re.compile(r"[^a-z0-9-]")
# IAM のロールの ARN から名前を取り出す形。パスを持つことがある
ROLE_ARN_RE = re.compile(r"arn:aws[a-z-]*:iam::([0-9]{12}):role/(?:.*/)?([^/]+)")


def _role_name(value: str) -> tuple[str, str | None]:
    """引数のロール名か ARN から、ロール名と、ARN ならそのアカウントを取り出す。"""
    found = ROLE_ARN_RE.fullmatch(value)
    return (found.group(2), found.group(1)) if found else (value, None)


def _policy_name(name: str) -> str:
    """インラインポリシーの名前を、temt のポリシー名の規則に合わせる。"""
    return NOT_LABEL_RE.sub("-", name.lower())


def _document(raw: Any) -> Mapping[str, Any]:
    """get-role-policy の中身。boto3 は URL エンコードを解いて読んだ JSON を返すが、
    文字列で返ることにも備える。
    """
    if isinstance(raw, str):
        return json.loads(urllib.parse.unquote(raw))
    return raw


def _statements(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """ポリシーの Statement の並び。1 つだけのときは並びにする。"""
    statements = document.get("Statement") or []
    return [dict(one) for one in (statements if isinstance(statements, list) else [statements])]


def _read_role(iam: IAMClient, name: str) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """インスタンスロールのインラインポリシーと管理ポリシーを読む。

    インラインポリシーは、temt のポリシー名に直した名前から Statement の並びへの対応に
    する。直せない名前と、直すと重なる名前は止める。
    """
    try:
        _ = iam.get_role(RoleName=name)
    except ClientError as e:
        if error_code(e) != resources.NO_SUCH_ENTITY:
            raise
        raise TemtError(f"no such role: {printable(name)}") from None
    inline: dict[str, list[dict[str, Any]]] = {}
    origin: dict[str, str] = {}
    for page in iam.get_paginator("list_role_policies").paginate(RoleName=name):
        for one in page["PolicyNames"]:
            label = _policy_name(one)
            if not is_label(label):
                raise TemtError(
                    f"the inline policy {printable(one)} becomes {printable(label)}, but the "
                    f"policy name {LABEL_RULE}; rename it in IAM and run role import again"
                )
            if label in inline:
                raise TemtError(
                    f"the inline policies {printable(origin[label])} and {printable(one)} both "
                    f"become {label}; rename one in IAM and run role import again"
                )
            found = iam.get_role_policy(RoleName=name, PolicyName=one)
            inline[label] = _statements(_document(found["PolicyDocument"]))
            origin[label] = one
    managed = resources.attached_policies_of(iam, name)
    return inline, managed


def _role_table(inline: Mapping[str, list[dict[str, Any]]], managed: list[str]) -> Any:
    """[role] のテーブル。インラインポリシーは [[role.<ポリシー名>]] に、管理ポリシーは
    ManagedPolicyArns にする。
    """
    table = tomlkit.table()
    if managed:
        table[conf.MANAGED_POLICY_ARNS] = managed
    for label, statements in inline.items():
        aot = tomlkit.aot()
        for statement in statements:
            aot.append(statement)
        table[label] = aot
    return table


def _rewritten(text: str, path: Path, inline: Mapping[str, Any], managed: list[str]) -> str:
    """設定ファイルの中身の、[role] を丸ごと置き換え、IamInstanceProfile を temt のものに
    したもの。
    """
    document = tomlkit.parse(text)
    conf.replace_table(
        document, conf.ROLE, _role_table(inline, managed) if inline or managed else None
    )
    written = tomlkit.dumps(document)
    profile = conf.launch_template_key(ltdata.PROFILE)
    changed = tomlkit.parse(written)
    current = (changed.get(conf.LAUNCH_TEMPLATE) or {}).get(ltdata.PROFILE)
    if current is None or isinstance(current, Table):
        # テーブルかドット付きキーで書かれていれば、中の Name を書き換え、Arn を消す。
        # 丸ごと置き換えると、後ろのコメントが消える。無ければ、雛形と同じくドット付きキー
        # IamInstanceProfile.Name で書く
        if current is not None and "Arn" in current:
            del current["Arn"]
        _ = conf.write_document(
            changed, f"{profile}{conf.SEPARATOR}Name", "${temt:name}", path, "role import"
        )
        return tomlkit.dumps(changed)
    # インラインテーブルで書かれていれば、その場で値を置き換える
    if not conf.write_document(changed, profile, {"Name": "${temt:name}"}, path, "role import"):
        return written
    return tomlkit.dumps(changed)


def import_role(service: Service, role: str, *, yes: bool) -> None:
    """インスタンスロール role の写しを、temt の管理に移すよう設定ファイルを書き換える。

    AWS には何もしない。[role] を丸ごと置き換え、IamInstanceProfile を ${temt:name} に
    する。書き終えたら、元のインスタンスロールの ARN を名指ししているリソースポリシーを
    手で直すことと、diff と apply を実行することを告げる。
    """
    values = conf.load(service)
    session = aws.session(values)
    iam = session.client("iam")
    name, account = _role_name(role)
    if account is not None and account != aws.account(session):
        # 読むのは、資格情報のアカウントにある同じ名前のロールになる
        raise TemtError(
            f"the role {printable(role)} is in the account {account}, not the one of the "
            f"credentials; role import reads roles of that account only"
        )
    inline, managed = _read_role(iam, name)
    path = service.conf_path
    text = path.read_text(encoding="utf-8")
    written = _rewritten(text, path, inline, managed)
    key = conf.launch_template_key(ltdata.PROFILE)
    before = conf.written(service, key)
    lines = [
        *(
            f"[[{conf.ROLE}.{label}]] ({len(found)} "
            f"{'statement' if len(found) == 1 else 'statements'})"
            for label, found in inline.items()
        ),
        *(f"ManagedPolicyArns: {arn}" for arn in managed),
        f"{key}: {'-' if before is None else conf.toml_value(before)} -> "
        f"{conf.toml_value({'Name': '${temt:name}'})}",
    ]
    msg.tell(
        f"role import {printable(name)} into {printable(tilde(path))} (replacing [role]):\n"
        + "".join(f"  {printable(line)}\n" for line in lines)
    )
    msg.confirm(f"rewrite {printable(tilde(path))}?", yes=yes)
    if path.read_text(encoding="utf-8") != text:
        raise TemtError(f"{printable(tilde(path))} changed while running; run role import again")
    # 書いたものが読めることを確かめてから置き換える
    _ = conf.parse_text(written, path)
    replace_text(path, written)
    msg.info(
        f"wrote [role] and {conf.launch_template_key(ltdata.PROFILE)} in {printable(tilde(path))}"
    )
    msg.info(
        f"fix by hand the resource policies that name the role {printable(name)} (S3 bucket "
        f"policies, KMS key policies, ...); then run '{PREFIX} diff --svc {service.svc}' and "
        f"'{PREFIX} apply --svc {service.svc}'"
    )


def _users(ec2: EC2Client, service: Service) -> list[str]:
    """インスタンスプロファイル <名前> を使うインスタンス。止めているものも含む。"""
    found = ec2.describe_instances(
        Filters=[
            {"Name": "iam-instance-profile.arn", "Values": [f"*:instance-profile/{service.name}"]},
            {"Name": "instance-state-name", "Values": list(instances.SEARCH_STATES)},
        ]
    )
    return sorted(
        field(item, "InstanceId")
        for reservation in found.get("Reservations") or []
        for item in reservation.get("Instances") or []
    )


def delete(service: Service, *, yes: bool) -> None:
    """temt のインスタンスロールとインスタンスプロファイル <名前> を消す。

    temt:service=<svc> が付いているものだけを消す。[role] が書かれているあいだ（次の
    apply が作り直す）、ロールにほかのリージョンのタグが残っているあいだ（そこの設定には
    [role] がある）、このリージョンでそれを使うインスタンスがあるあいだ、ロールがほかの
    インスタンスプロファイルにも入っているあいだは、消さずに止める。[role] が無いのに
    IamInstanceProfile が <名前> を指していれば、次の apply が止まるので、ここでは見ない。
    <名前> が長すぎれば、IAM を呼ばずに、どちらも無いものとする。
    """
    values = conf.load(service)
    session = aws.session(values)
    iam = session.client("iam")
    ec2 = session.client("ec2")
    if values.role:
        # 中身の無い [role] は書かれていないのと同じなので、中身のあるファイルを挙げる
        own = conf.load(service, common=False).role
        where = service.conf_path if own else defaults_path()
        raise TemtError(
            f"[{conf.ROLE}] in {printable(tilde(where))} makes "
            f"{service.name}, and the next apply would make it again; remove [{conf.ROLE}] first"
        )
    too_long = resources.too_long(service)
    role = None if too_long else resources.read_tags(iam, "role", service)
    profile = None if too_long else resources.read_tags(iam, "instance profile", service)
    if role is None and profile is None:
        msg.info(f"role {service.name}: {resources.ABSENT}")
        msg.info(f"instance profile {service.name}: {resources.ABSENT}")
        return
    for what, tags in (("role", role), ("instance profile", profile)):
        if tags is not None and aws.tag({"Tags": tags}, aws.TAG_SERVICE) != service.svc:
            raise TemtError(
                f"the {what} {service.name} is not tagged {aws.TAG_SERVICE}={service.svc}; "
                f"delete it by hand"
            )
    if role is not None:
        regions = [one for one in aws.tagged_regions({"Tags": role}) if one != values.AwsRegion]
        if regions:
            raise TemtError(
                f"the role {service.name} is used in {', '.join(regions)} (tagged "
                f"{aws.region_tag(regions[0])}); remove [{conf.ROLE}] and run apply there first"
            )
    if profile is not None:
        used = _users(ec2, service)
        if used:
            raise TemtError(
                f"the instance profile {service.name} is used by {', '.join(used)}; "
                f"terminate them first"
            )
    lines: list[str] = []
    if profile is not None:
        held = [name for name in resources.profile_roles(iam, service) if name != service.name]
        if held:
            raise TemtError(
                f"the instance profile {service.name} holds another role: {held[0]}; "
                f"remove it by hand and run role delete again"
            )
        lines.append(f"instance profile {service.name}")
    if role is not None:
        others = resources.other_profiles(iam, service)
        if others:
            raise TemtError(
                f"the role {service.name} is also in the instance profile {others[0]}; "
                f"remove it from there by hand and run role delete again"
            )
        policies = resources.held(
            resources.inline_policies(iam, service), resources.attached_policies(iam, service)
        )
        lines.append(f"role {service.name}" + (f" ({policies})" if policies else ""))
    msg.tell(f"role delete {service.name}:\n" + "".join(f"  {line}\n" for line in lines))
    aws.confirm(session, f"delete them for {service.name}", yes=yes)
    if profile is not None:
        _ = resources.delete_instance_profile(iam, service, command="role delete")
    if role is not None:
        resources.delete_role(iam, service, command="role delete")
