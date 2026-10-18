"""--svc を付けた app list と、app show、app get、app set、app unset の本体。

アプリの値と記録は SSM パラメータ /<名前>/<アプリID>/<キー> に置く。キー deploy は
temt がアプリの記録に使い、値は RECORD_FIELDS の項目を持つ JSON である。値は String で
書き、加工しない。

記録は、最後に入れたもの（version、deployed、指定したオプションの options と、まだ
焼いていなければ入れたインスタンスの instance）と、AMI に焼いたもの（baked_version、
baked_image、baked_options）を別に持つ。deploy が前者を書き、bake が mark_baked で後者を
書く。AMI に焼いたアプリを焼かずに app remove で外すと、mark_removed で外した
インスタンスを removed に書いて残し、bake が記録を消す。app list、status、terminate は、
これと起動テンプレートの AMI、いまあるインスタンスから、焼いたか（state）を決める。

どのコマンドも、apply 済みであること（起動テンプレートがあること）を先に確かめる。
表示するだけか、値 1 つを書き換えるだけなので、ロックは取らない。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from temt import aws, conf, instances, msg, options, resources, table
from temt.aws import error_code
from temt.manifest import find_app, local_apps, read_manifest
from temt.msg import TemtError
from temt.names import DEPLOY_KEY, is_label
from temt.table import tilde

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.instances import Instance
    from temt.names import Service

# 名前の SSM パラメータが無いときに返る誤りの名前
NOT_FOUND = "ParameterNotFound"
# 既にある別の型のパラメータを、上書きで String にしようとしたときの誤りの名前
TYPE_MISMATCH = "HierarchyTypeMismatchException"
# app set が書く型。SecureString は扱わない
STRING = "String"
SECURE_STRING = "SecureString"
# deploy の記録の項目。表示し、書く順に並べる。instance は、最後に入れた版をまだ焼いて
# いないときだけ持つ。removed は、AMI に焼いたアプリを焼かずに外したときだけ、外した
# インスタンスの ID を持つ。OPTION_FIELDS はオプションの名前から値への対応（JSON の
# オブジェクト）で、ほかは文字列である
OPTIONS = "options"
BAKED_OPTIONS = "baked_options"
OPTION_FIELDS = (OPTIONS, BAKED_OPTIONS)
RECORD_FIELDS = (
    "name",
    "version",
    "user",
    "deployed",
    "instance",
    "removed",
    OPTIONS,
    "baked_version",
    "baked_image",
    BAKED_OPTIONS,
)
# SSM パラメータ（標準）の値の大きさの上限。deploy は記録がこれに収まるかを先に確かめる
RECORD_MAX = 4096
# 焼いたかどうか。最後に入れた版が、起動テンプレートの AMI にある、いまある
# インスタンスにだけある、どこにも無い。REMOVED は、いまあるインスタンスからは外したが、
# 起動テンプレートの AMI には残っている
BAKED = "baked"
UNBAKED = "unbaked"
LOST = "lost"
REMOVED = "removed"

COLUMNS = table.Columns(
    defaults=("ID", "NAME", "VERSION", "DEPLOYED", "STATE", "BAKED", "LOCAL"), extras=("PATH",)
)


def _ssm(service: Service) -> SSMClient:
    """apply 済みであることを確かめ、SSM のクライアントを返す。"""
    session = aws.session(conf.load(service))
    resources.require_template(session.client("ec2"), service)
    return session.client("ssm")


@dataclass
class Record:
    """deploy の記録。"""

    # OPTION_FIELDS を除く項目。値はどれも文字列
    fields: dict[str, str] = field(default_factory=dict)
    # 最後に入れたときに指定したオプション。記録か options が読めなければ None
    options: dict[str, str] | None = field(default_factory=dict)
    # AMI に焼いた版のオプション。記録か baked_options が読めなければ None
    baked_options: dict[str, str] | None = field(default_factory=dict)
    # 読めなかった options と baked_options の、JSON として読んだままの値。
    # record_value がそのまま書き戻すので、書き直しても失われない
    unread: dict[str, Any] = field(default_factory=dict)


# 記録の options と baked_options が読めないときの理由と、app show の表示
_SHAPE = "not a JSON object of option names and values"
UNREADABLE = "(unreadable)"
# 後ろに { が続く {。record_value がエスケープする
_BRACES_RE = re.compile(r"\{(?=\{)")


def _option_values(found: object) -> dict[str, str] | None:
    """記録の options か baked_options を読む。形が違えば None。"""
    if found is None:
        return {}
    if not isinstance(found, dict):
        return None
    values: dict[str, str] = {}
    for key, value in found.items():
        if not isinstance(key, str) or not is_label(key):
            return None
        if not isinstance(value, str) or not options.is_value(value):
            return None
        values[key] = value
    return values


def parse_record(name: str, value: str) -> Record:
    """deploy の記録を読む。読めなければ警告して、空の記録とする。

    記録を書くのは temt だが、SSM パラメータは誰でも書き換えられるので、形を確かめる。
    JSON のオブジェクトでないか、RECORD_FIELDS の項目に UTF-8 で書けない値（対に
    ならないサロゲートを表す \\u の形）を含む記録は、読めない記録として空にし、options
    と baked_options を None とする。ほかの項目は書き直すときに捨てるので、確かめない。
    options か baked_options の形が違えば、それを None とし、書かれていたままの値を
    unread に残す。deploy は、読めない options を引き継がずに止める。
    """
    try:
        found = json.loads(value)
    except ValueError:
        found = None
    if not isinstance(found, dict):
        msg.warn(f"cannot read the deploy record {name}: not a JSON object")
        return Record(options=None, baked_options=None)
    kept = {key: found[key] for key in RECORD_FIELDS if key in found}
    if not options.is_utf8(json.dumps(kept, ensure_ascii=False)):
        msg.warn(f"cannot read the deploy record {name}: not valid UTF-8")
        return Record(options=None, baked_options=None)
    record = Record(
        fields={
            key: str(found[key])
            for key in RECORD_FIELDS
            if key not in OPTION_FIELDS and found.get(key) is not None
        }
    )
    record.options = _option_values(found.get(OPTIONS))
    if record.options is None:
        msg.warn(f"cannot read {OPTIONS} in the deploy record {name}: {_SHAPE}")
        record.unread[OPTIONS] = found[OPTIONS]
    record.baked_options = _option_values(found.get(BAKED_OPTIONS))
    if record.baked_options is None:
        msg.warn(f"cannot read {BAKED_OPTIONS} in the deploy record {name}: {_SHAPE}")
        record.unread[BAKED_OPTIONS] = found[BAKED_OPTIONS]
    return record


def record_value(record: Record) -> str:
    """記録を、SSM パラメータに書く JSON にする。項目は RECORD_FIELDS の順に並べる。

    日本語などの値もエスケープせずに、そのまま書く。空のオプションは書かない。読めなかった
    オプションは、書かれていたままの値を書く。SSM は {{ を含む値を、ほかのパラメータの
    参照として断るので、文字列の中の { が続くところは、前の { を \\u007b にする。JSON の
    構造の { の後ろには " か } しか来ないので、{ が続くのは文字列の中だけである。
    """
    found: dict[str, Any] = {}
    for key in RECORD_FIELDS:
        if key in OPTION_FIELDS:
            values = record.options if key == OPTIONS else record.baked_options
            if values is None and key in record.unread:
                found[key] = record.unread[key]
            elif values:
                found[key] = values
        elif key in record.fields:
            found[key] = record.fields[key]
    return _BRACES_RE.sub(r"\\u007b", json.dumps(found, ensure_ascii=False))


def record_size(record: Record) -> int:
    """記録を SSM パラメータに書いたときの大きさ（UTF-8 のバイト数）。"""
    return len(record_value(record).encode())


def read(ssm: SSMClient, name: str) -> Mapping[str, Any] | None:
    """SSM パラメータ name を読む。無ければ None。app remove も使う。"""
    try:
        return ssm.get_parameter(Name=name)["Parameter"]
    except ClientError as e:
        if error_code(e) != NOT_FOUND:
            raise
        return None


def with_key(ssm: SSMClient, service: Service, key: str) -> list[tuple[str, Mapping[str, Any]]]:
    """/<名前>/<アプリID>/<key> の形の名前のパラメータを、(アプリ ID, パラメータ) の並びで。

    /<名前> の下を再帰的に読み、ほかのキーと、階層の深いものは除く。アプリ ID の
    規則は確かめない。並びは AWS が返した順のまま。status も使う。
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


def records(ssm: SSMClient, service: Service) -> list[tuple[str, Record]]:
    """deploy の記録を、(アプリ ID, 記録) の並びでアプリ ID の順に返す。

    読めない記録は、警告して空の記録とする。
    """
    found = [
        (app_id, parse_record(aws.field(one, "Name"), str(one.get("Value") or "")))
        for app_id, one in with_key(ssm, service, DEPLOY_KEY)
    ]
    return sorted(found, key=lambda item: item[0])


@dataclass(frozen=True)
class Place:
    """焼いたかどうかを決めるのに使う、起動テンプレートの AMI といまあるインスタンス。"""

    # 起動テンプレートの既定バージョンの ImageId
    image_id: str
    # 終了しかけていないインスタンスの ID
    alive: frozenset[str]


def place(image_id: str, found: list[Instance]) -> Place:
    """起動テンプレートの AMI と、instances.find で見つけたインスタンスから Place を作る。"""
    alive = frozenset(one.id for one in found if one.state != instances.SHUTTING_DOWN)
    return Place(image_id=image_id, alive=alive)


def find_place(ec2: EC2Client, service: Service) -> Place:
    """起動テンプレートの AMI と、いまあるインスタンスを読む。"""
    return place(resources.default_image(ec2, service), instances.find(ec2, service))


def state(record: Mapping[str, str], where: Place) -> str:
    """最後に入れた版を焼いたかどうか。BAKED、UNBAKED、LOST、REMOVED、分からなければ空。

    removed のインスタンスがあり、焼いた AMI がいまの起動テンプレートの AMI なら、
    そこから外したが、まだ焼いていないので REMOVED。インスタンスが無ければ、外したことは
    失われ、AMI に焼いた版が次のインスタンスに戻るので、焼いた記録として扱う。
    instance があれば、まだ焼いていない。そのインスタンスがあれば
    UNBAKED、無ければ LOST。無ければ焼いてある。焼いた AMI がいまの起動テンプレートの
    AMI なら BAKED、apply で別の AMI に替えたなどで違えば LOST。どちらも無い記録
    （焼いたかどうかを記録する前の deploy が書いたもの）は分からない。
    """
    if record.get("removed") in where.alive and record.get("baked_image") == where.image_id:
        return REMOVED
    instance = record.get("instance")
    if instance:
        return UNBAKED if instance in where.alive else LOST
    image_id = record.get("baked_image")
    if not image_id:
        return ""
    return BAKED if image_id == where.image_id else LOST


def baked_version(record: Mapping[str, str], where: Place) -> str:
    """起動テンプレートの AMI に入っている版。入っていなければ空。"""
    image_id = record.get("baked_image")
    return record.get("baked_version", "") if image_id and image_id == where.image_id else ""


def mark_baked(ssm: SSMClient, service: Service, one: Instance, image_id: str) -> None:
    """bake が one から AMI image_id を作り、テンプレートを向けたときに、記録をそれに揃える。

    one に入れてまだ焼いていない記録は、その版とオプションを焼いた版とオプションにして
    instance を消す。one の起動元の AMI に焼いてあった記録は、焼いた AMI だけを image_id
    にする。one にはその版が引き継がれているからである。外したことが失われた removed も
    消す。one から外した記録は、image_id にアプリが無いので消す。読めなかったオプションは、
    書かれていたままの値で書く。書けない記録と消せない記録は警告して飛ばす。
    """
    marked: list[str] = []
    dropped: list[str] = []
    found = sorted(with_key(ssm, service, DEPLOY_KEY), key=lambda item: item[0])
    for app_id, parameter in found:
        name = service.app_parameter(app_id, DEPLOY_KEY)
        record = parse_record(name, str(parameter.get("Value") or ""))
        fields = record.fields
        if fields.get("removed") == one.id:
            try:
                _ = delete(ssm, name)
            except (ClientError, BotoCoreError) as e:
                msg.warn(f"cannot delete {name}: {table.aws_error(e)}")
                continue
            dropped.append(app_id)
            continue
        fresh = fields.get("instance") == one.id
        if fresh:
            mark_fresh(record, image_id)
        elif one.image_id and fields.get("baked_image") == one.image_id:
            fields["baked_image"] = image_id
            _ = fields.pop("removed", None)
        else:
            continue
        if fresh and record.options is None:
            # 読めないオプションのまま焼いた。次の deploy は、それを引き継げずに止まる
            msg.warn(
                f"the options in {name} cannot be read; baking them as they are; deploy "
                f"{app_id} again with --reset to give them"
            )
        try:
            _ = put(ssm, name, record_value(record))
        except (ClientError, BotoCoreError) as e:
            msg.warn(f"cannot mark {name} as baked: {table.aws_error(e)}")
            continue
        if fresh:
            marked.append(app_id)
    if marked:
        msg.info(f"recorded {', '.join(marked)} as baked in {image_id}")
    if dropped:
        msg.info(f"recorded {', '.join(dropped)} as removed from {image_id}")


def mark_fresh(record: Record, image_id: str) -> None:
    """最後に入れた版を AMI image_id に焼いたものとして、記録を書き換える。

    版とオプションを焼いた版とオプションに写し、instance を消す。deploy が、焼いたあとの
    記録の大きさを見積もるのにも使う。
    """
    fields = record.fields
    fields["baked_version"] = fields.get("version", "")
    fields["baked_image"] = image_id
    _ = fields.pop("instance", None)
    # 読めない options は、書かれていたままの値を写す
    _ = record.unread.pop(BAKED_OPTIONS, None)
    if record.options is None:
        record.baked_options = None
        if OPTIONS in record.unread:
            record.unread[BAKED_OPTIONS] = record.unread[OPTIONS]
    else:
        record.baked_options = dict(record.options)


def mark_removed(record: Record, instance_id: str) -> None:
    """AMI に焼いたアプリを、焼かずにインスタンス instance_id から外したものとして書き換える。

    外したインスタンスを removed に書き、instance を消す。最後に入れた版とオプションは、
    焼いた版とオプションにする。bake の前にインスタンスが終了すると、外したことは
    失われ、焼いた版が次のインスタンスに戻るからである。焼いた版を入れた時刻は記録に
    無いので、deployed は消す。
    """
    fields = record.fields
    fields["version"] = fields.get("baked_version", "")
    _ = fields.pop("instance", None)
    _ = fields.pop("deployed", None)
    fields["removed"] = instance_id
    # 読めない baked_options は、書かれていたままの値を写す
    _ = record.unread.pop(OPTIONS, None)
    if record.baked_options is None:
        record.options = None
        if BAKED_OPTIONS in record.unread:
            record.unread[OPTIONS] = record.unread[BAKED_OPTIONS]
    else:
        record.options = dict(record.baked_options)


def list_apps(service: Service, spec: str | None) -> None:
    """deploy の記録のあるアプリと、アプリ ID で使えるアプリを、1 つ 1 行の表で挙げる。

    アプリ ID の順に並べる。STATE は焼いたかどうか、BAKED は起動テンプレートの AMI に
    入っている版。LOCAL は手元の manifest.toml の版で、入っている版と比べられる
    ようにする。PATH はそのディレクトリ。manifest.toml が読めなければ、警告して空とする。
    """
    chosen = COLUMNS.choose(spec)
    session = aws.session(conf.load(service))
    ec2 = session.client("ec2")
    resources.require_template(ec2, service)
    ssm = session.client("ssm")
    rows: dict[str, dict[str, str]] = {}
    found = records(ssm, service)
    where = find_place(ec2, service)
    for app_id, record in found:
        fields = record.fields
        # 記録は UTC で書いてある。読めない時刻は書かれたまま出す
        deployed = fields.get("deployed", "")
        rows[app_id] = {
            "ID": app_id,
            "NAME": fields.get("name", ""),
            "VERSION": fields.get("version", ""),
            "DEPLOYED": aws.cell_time(deployed) or deployed,
            "STATE": state(fields, where),
            "BAKED": baked_version(fields, where),
        }
    for app_id, path, manifest in local_apps():
        row = rows.setdefault(app_id, {"ID": app_id})
        if not row.get("NAME"):
            row["NAME"] = manifest.name if manifest else ""
        row["LOCAL"] = manifest.version if manifest else ""
        row["PATH"] = tilde(path)
    table.write(chosen, [rows[app_id] for app_id in sorted(rows)])


def _shown_options(record: Record, key: str) -> str:
    """記録のオプション key の表示。<名前>=<値> を空白で区切って並べる。

    形が違って読めなければ UNREADABLE とする。記録そのものが読めなければ、ほかの項目と
    同じく EMPTY とする。
    """
    values = record.options if key == OPTIONS else record.baked_options
    if values is None:
        return UNREADABLE if key in record.unread else table.EMPTY
    return options.show_all(values) or table.EMPTY


def _show_record(app: str, found: Mapping[str, Any]) -> None:
    """deploy の記録の項目を、RECORD_FIELDS の順に 1 行ずつ出す。

    オプションの行は options.show_all が表示できる形にするので、ほかの項目と違って
    table.printable を通さない。
    """
    record = parse_record(aws.field(found, "Name"), str(found.get("Value") or ""))
    fields = {key: table.printable(value) for key, value in record.fields.items()}
    if "deployed" in fields:
        fields["deployed"] = aws.cell_time(fields["deployed"]) or fields["deployed"]
    fields[OPTIONS] = _shown_options(record, OPTIONS)
    fields[BAKED_OPTIONS] = _shown_options(record, BAKED_OPTIONS)
    msg.tell(f"app {app}:\n")
    msg.rows([(key, fields.get(key) or table.EMPTY) for key in RECORD_FIELDS])


def _show_declared(app: str, place: Path) -> None:
    """手元のアプリ place の manifest.toml が宣言するオプションを出す。

    manifest.toml が読めなければ、警告して宣言は出さない。
    """
    try:
        manifest = read_manifest(place)
    except TemtError as e:
        msg.warn(f"cannot read the manifest of {app}: {e}")
        return
    head = (
        f"options in {table.printable(tilde(place))} (version {options.visible(manifest.version)})"
    )
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


def show(service: Service, app: str) -> None:
    """アプリの deploy の記録と、キーの一覧（キー、更新日時）を表示する。値は出さない。

    アプリ ID で使えるアプリがあれば、その manifest.toml が宣言するオプションも出す。
    記録も値も無く、使えるアプリも無ければ、アプリ ID の打ち間違いとみなして止める。
    """
    ssm = _ssm(service)
    path = service.app_path(app)
    found = read(ssm, service.app_parameter(app, DEPLOY_KEY))
    keys = sorted(
        (aws.field(one, "Name").rsplit("/", 1)[-1], aws.when(one.get("LastModifiedDate")))
        for page in ssm.get_paginator("describe_parameters").paginate(
            ParameterFilters=[{"Key": "Path", "Option": "OneLevel", "Values": [path]}]
        )
        for one in page.get("Parameters", [])
    )
    keys = [(key, when) for key, when in keys if key != DEPLOY_KEY]
    place = find_app(app)
    if found is None and not keys and place is None:
        raise TemtError(f"no app {app} in {service.name}: nothing under {path}/")
    if found is None:
        msg.tell(f"app {app}: not deployed\n")
    else:
        _show_record(app, found)
    if place is not None:
        _show_declared(app, place)
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

    deploy の記録が無いアプリには書かない。アプリ ID の打ち間違いで、誰も読まない値が
    できるのを防ぐ。
    """
    ssm = _ssm(service)
    record = service.app_parameter(app, DEPLOY_KEY)
    if read(ssm, record) is None:
        raise TemtError(f"no deploy record {record}; the app {app} is not deployed")
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
    """SSM パラメータ name に value を String で書き、created か updated を返す。

    app set と、deploy が記録を書くときに使う。
    """
    version = ssm.put_parameter(Name=name, Value=value, Type=STRING, Overwrite=True).get("Version")
    # 消してから作り直しても版は 1 から始まるので、1 なら作ったものとする
    return resources.CREATED if version == 1 else resources.UPDATED


def delete(ssm: SSMClient, name: str) -> str:
    """SSM パラメータ name を消し、deleted を返す。無ければ absent を返す。

    app unset と、app remove が記録と状態を消すときに使う。
    """
    try:
        _ = ssm.delete_parameter(Name=name)
    except ClientError as e:
        if error_code(e) != NOT_FOUND:
            raise
        return resources.ABSENT
    return resources.DELETED
