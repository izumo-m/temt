"""app show、app get、app set、app unset の本体。

アプリの値は SSM パラメータ /<名前>/<アプリID>/<キー> に置く。値は String で書き、加工
しない。インスタンスに何が入っているかと deploy の記録は、インスタンスの上にある
（remote）。app show は、動いているインスタンスがあれば Run Command で読む。

どのコマンドも、apply 済みであること（起動テンプレートがあること）を先に確かめる。
表示するだけか、値 1 つを書き換えるだけなので、ロックは取らない。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from temt import aws, conf, instances, msg, options, remote, resources, table
from temt.aws import error_code
from temt.manifest import find_app, read_manifest
from temt.msg import TemtError
from temt.table import tilde

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_ssm.client import SSMClient

    from temt.instances import Instance
    from temt.manifest import Manifest
    from temt.names import Service
    from temt.remote import Installed

# 名前の SSM パラメータが無いときに返る誤りの名前
NOT_FOUND = "ParameterNotFound"
# 既にある別の型のパラメータを、上書きで String にしようとしたときの誤りの名前
TYPE_MISMATCH = "HierarchyTypeMismatchException"
# app set が書く型。SecureString は扱わない
STRING = "String"
SECURE_STRING = "SecureString"
# app show が出す deploy の記録の項目。並びは出す順
SHOWN_FIELDS = ("name", "version", "user", "deployed", "baked", "options")


def _ssm(service: Service) -> SSMClient:
    """apply 済みであることを確かめ、SSM のクライアントを返す。"""
    session = aws.session(conf.load(service))
    resources.require_template(session.client("ec2"), service)
    return session.client("ssm")


def read(ssm: SSMClient, name: str) -> Mapping[str, Any] | None:
    """SSM パラメータ name を読む。無ければ None。"""
    try:
        return ssm.get_parameter(Name=name)["Parameter"]
    except ClientError as e:
        if error_code(e) != NOT_FOUND:
            raise
        return None


def with_key(ssm: SSMClient, service: Service, key: str) -> list[tuple[str, Mapping[str, Any]]]:
    """/<名前>/<アプリID>/<key> の形の名前のパラメータを、(アプリ ID, パラメータ) の並びで。

    /<名前> の下を再帰的に読み、ほかのキーと、階層の深いものは除く。アプリ ID の
    規則は確かめない。並びは AWS が返した順のまま。status が使う。
    """
    found: list[tuple[str, Mapping[str, Any]]] = []
    for page in ssm.get_paginator("get_parameters_by_path").paginate(
        Path=service.parameter_path, Recursive=True
    ):
        for one in page.get("Parameters", []):
            parts = aws.field(one, "Name").removeprefix(f"{service.parameter_path}/").split("/")
            if len(parts) == 2 and parts[1] == key:
                found.append((parts[0], one))
    return found


def _show_record(app: str, instance_id: str, installed: Installed) -> None:
    """インスタンスにあるアプリの manifest.toml の名前と版、deploy の記録の項目を出す。

    インスタンスから来た値なので、表示できない文字を置き換える。版とオプションの値は、
    端末を乱す文字だけを置き換え、打ち直せる形にする。
    """
    manifest = installed.manifest
    record = installed.record
    fields = {}
    if manifest is not None:
        fields["name"] = table.printable(manifest.name)
        fields["version"] = options.visible(manifest.version)
    if record is not None and record.readable:
        for key, value in record.fields.items():
            fields[key] = table.printable(value)
        if "deployed" in fields:
            fields["deployed"] = aws.cell_time(fields["deployed"]) or fields["deployed"]
        if record.options is None:
            fields["options"] = remote.UNREADABLE
        else:
            fields["options"] = options.show_all(record.options) or table.EMPTY
    fields["baked"] = remote.baked(record, instance_id)
    msg.tell(f"app {app} on {instance_id}:\n")
    msg.rows([(key, fields.get(key) or table.EMPTY) for key in SHOWN_FIELDS])


def _show_declared(manifest: Manifest, place: str) -> None:
    """manifest.toml が宣言するオプションを出す。place はその manifest.toml のあるアプリ。"""
    head = f"options in {place} (version {options.visible(manifest.version)})"
    if not manifest.options:
        msg.tell(f"{head}: none\n")
        return
    msg.tell(f"{head}:\n")
    msg.columns(
        [
            [
                option.name,
                "(required)" if option.default is None else options.quoted(option.default),
                options.visible(option.description),
            ]
            for option in manifest.options.values()
        ]
    )


def _read_installed(
    ssm: SSMClient, app: str, found: list[Instance]
) -> tuple[Instance | None, Installed | None, str]:
    """動いているインスタンスから、Run Command でアプリを読む。

    (読んだインスタンス, そのアプリ, 読めなかった理由) を返す。インスタンスが無いか
    読めなければ、インスタンスは None で、理由を添える。読めてアプリが無ければ、
    アプリが None で理由は空。
    """
    one, why = remote.readable(ssm, found)
    if one is None:
        return None, None, why
    try:
        installed = remote.read_apps_by_command(ssm, one, app).get(app)
    except TemtError as e:
        msg.warn(str(e))
        return None, None, f"cannot read {one.id}"
    return one, installed, ""


def show(service: Service, app: str) -> None:
    """アプリの deploy の記録と、キーの一覧（キー、更新日時）を表示する。値は出さない。

    記録は、running のインスタンスがちょうど 1 台あれば、Run Command で読む。無いか
    読めなければ、その理由を出して続ける。宣言したオプションは、インスタンスから読めれば
    その manifest.toml から、読めなければアプリ ID で使える手元のアプリから出す。
    インスタンスに無いか読めず、値も無く、手元のアプリも無ければ、アプリ ID の打ち間違いと
    みなして止める。
    """
    values = conf.load(service)
    session = aws.session(values)
    ec2 = session.client("ec2")
    resources.require_template(ec2, service)
    ssm = session.client("ssm")
    path = service.app_path(app)
    keys = sorted(
        (aws.field(one, "Name").rsplit("/", 1)[-1], aws.when(one.get("LastModifiedDate")))
        for page in ssm.get_paginator("describe_parameters").paginate(
            ParameterFilters=[{"Key": "Path", "Option": "OneLevel", "Values": [path]}]
        )
        for one in page.get("Parameters", [])
    )
    place = find_app(app)
    one, installed, why = _read_installed(ssm, app, instances.find(ec2, service))
    if installed is None and not keys and place is None:
        where = f"not on {one.id}" if one is not None else why
        raise TemtError(f"no app {app} in {service.name}: {where}, and nothing under {path}/")
    if one is None:
        msg.tell(f"app {app}: deploy record not read ({why})\n")
    elif installed is None:
        msg.tell(f"app {app}: not deployed on {one.id}\n")
    else:
        _show_record(app, one.id, installed)
    if one is not None and installed is not None and installed.manifest is not None:
        where = table.printable(f"{one.id}:{remote.REMOTE_ROOT}/{app}")
        _show_declared(installed.manifest, where)
    elif place is not None:
        try:
            manifest = read_manifest(place)
        except TemtError as e:
            msg.warn(f"cannot read the manifest of {app}: {e}")
        else:
            _show_declared(manifest, table.printable(tilde(place)))
    if not keys:
        msg.tell("keys: none\n")
        return
    msg.tell("keys:\n")
    lines = [[table.printable(key), when] for key, when in keys]
    msg.columns(lines)


def get(service: Service, app: str, key: str) -> None:
    """値を 1 つ、そのまま標準出力に書く。改行を足さない。無ければ止める。"""
    ssm = _ssm(service)
    name = service.app_parameter(app, key)
    found = read(ssm, name)
    if found is None:
        raise TemtError(f"no value {name}")
    if found.get("Type") == SECURE_STRING:
        raise TemtError(f"{name} is a {SECURE_STRING}; temt does not read {SECURE_STRING} values")
    table.out(str(found.get("Value") or ""))


def set_value(service: Service, app: str, key: str, value: str) -> None:
    """値を 1 つ String で書く。無ければ作り、あれば上書きする。

    アプリが入っているかは確かめない。動いているインスタンスでしか分からないからである。
    アプリ ID を打ち間違えた値は、app show のキーの一覧に出るので、app unset で消せる。
    """
    ssm = _ssm(service)
    name = service.app_parameter(app, key)
    try:
        state = put(ssm, name, value)
    except ClientError as e:
        # 階層の下のパラメータは、上書きで型を変えられない
        if error_code(e) != TYPE_MISMATCH:
            raise
        raise TemtError(
            f"{name} is not a {STRING}; run 'temt app unset --svc {service.svc} {app} {key}' first"
        ) from None
    msg.info(f"parameter {name}: {state}")


def unset(service: Service, app: str, key: str) -> None:
    """値を 1 つ消す。確認は取らない。無ければ absent と知らせ、誤りにしない。"""
    ssm = _ssm(service)
    name = service.app_parameter(app, key)
    msg.info(f"parameter {name}: {delete(ssm, name)}")


def put(ssm: SSMClient, name: str, value: str) -> str:
    """SSM パラメータ name に value を String で書き、created か updated を返す。"""
    version = ssm.put_parameter(Name=name, Value=value, Type=STRING, Overwrite=True).get("Version")
    # 消してから作り直しても版は 1 から始まるので、1 なら作ったものとする
    return resources.CREATED if version == 1 else resources.UPDATED


def delete(ssm: SSMClient, name: str) -> str:
    """SSM パラメータ name を消し、deleted を返す。無ければ absent を返す。

    app unset と、app remove が状態を消すときに使う。
    """
    try:
        _ = ssm.delete_parameter(Name=name)
    except ClientError as e:
        if error_code(e) != NOT_FOUND:
            raise
        return resources.ABSENT
    return resources.DELETED
