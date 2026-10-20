"""起動テンプレートの中身（LaunchTemplateData）を、設定ファイルの [launch-template] と同じ
形で扱う。design/topics/launch-template.md。

設定ファイルの [launch-template] は、get-launch-template-data の出力と同じ名前と入れ子の
TOML である。書ける項目は、temt が使う botocore の RequestLaunchTemplateData の形で決め、
temt のコードには項目の表を持たない（check）。ここには、形で確かめること、共通ファイル
との重ね方（merge）、AWS が読み出しで変える形を揃えて比べること（normalize、leaves）、
TOML で見せること（render）を置く。どれも AWS を呼ばない。

botocore は、AWS を呼ばないコマンド（service list など）では読み込めないことがある
ので、形が要るときにだけ読み込む（shape）。boto3 の Session と同じ読み込み器を使い、
EC2 の定義を 2 度読まない（aws.session）。
"""

from __future__ import annotations

import base64
import binascii
import datetime
import json
import math
from functools import cache
from typing import TYPE_CHECKING, Any

from temt import policy
from temt.table import printable

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from botocore.loaders import Loader
    from botocore.model import Shape

# 作るときに渡す形の名前
REQUEST_SHAPE = "RequestLaunchTemplateData"
# 配列の要素を対応づけるキー。比べるとき、重ねるとき、diff の位置で使う
MATCH_KEYS = {
    "NetworkInterfaces": "DeviceIndex",
    "BlockDeviceMappings": "DeviceName",
    "TagSpecifications": "ResourceType",
    "Tags": "Key",
}
# 読み出したときにだけある項目。書けず、比べない
READ_ONLY = (
    ("MetadataOptions", "State"),
    ("NetworkInterfaces", "Ipv6Addresses", "IsPrimaryIpv6"),
    ("Operator", "HiddenByDefault"),
    ("Operator", "Managed"),
)
# base64 を戻して中身を見せる項目
USER_DATA = "UserData"
# IamInstanceProfile の、名前と ARN の項目
PROFILE = "IamInstanceProfile"
# template show で、role import が書くのと同じくインラインテーブルにする項目
INLINE = (PROFILE,)
# 共通ファイルと重ねるとき、テーブルでもキーごとに重ねず、丸ごと置き換える項目。共通
# ファイルの Arn と設定ファイルの Name が混ざって、別のインスタンスプロファイルを指さない
# ようにする
WHOLE = (PROFILE,)


@cache
def loader() -> Loader:
    """botocore の読み込み器。読み込んだ定義を覚えるので、1 つを使い回す。"""
    from botocore.loaders import create_loader

    return create_loader()


@cache
def shape() -> Shape:
    """RequestLaunchTemplateData の形。"""
    import botocore.session

    session = botocore.session.get_session()
    session.register_component("data_loader", loader())
    return session.get_service_model("ec2").shape_for(REQUEST_SHAPE)


# 空文字列が意味を持つ項目。BlockDeviceMappings の要素の NoDevice は、AMI のデバイスを
# 外す指定で、値は空文字列だけである。空の値として落とさない
EMPTY_MEANS = ("NoDevice",)


def blank(value: Any, key: str = "") -> bool:
    """書いていないのと同じに扱う値か。空文字列、空の配列、空のテーブル。

    key はその値の項目の名前で、EMPTY_MEANS の項目の空文字列は空の値にしない。
    """
    if value == "" and key in EMPTY_MEANS:
        return False
    return value == "" or value == [] or value == {}


def without_blanks(value: Any) -> Any:
    """空の値を落としたもの。形を確かめないときと、共通ファイルと重ねる前に使う。"""
    if isinstance(value, dict):
        return {
            key: item
            for key, item in ((key, without_blanks(item)) for key, item in value.items())
            if not blank(item, key)
        }
    if isinstance(value, list):
        return [item for item in (without_blanks(item) for item in value) if not blank(item)]
    return value


def _number(value: Any) -> bool:
    """TOML の整数か小数か。真偽値は Python では整数の仲間だが、数ではない。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int)


def check(value: Any, where: str, kind: Shape | None = None) -> Any:
    """value を形 kind（省けば RequestLaunchTemplateData）で確かめ、空の値を落としたものを
    返す。

    where は誤りに添える位置（launch-template.NetworkInterfaces[0] など）。誤りは
    ValueError で、その位置と理由を持つ。文字列の中の知らない ${temt:...} も誤りにする。
    空の値を落とした結果が空になれば、書いていないのと同じなので空の値を返す。
    """
    kind = shape() if kind is None else kind
    type_name = kind.type_name
    if type_name == "structure":
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be a table")
        made: dict[str, Any] = {}
        members = kind.members  # pyright: ignore[reportAttributeAccessIssue]
        for key, item in value.items():
            if key not in members:
                raise ValueError(f"unknown key: {where}.{printable(key)}")
            checked = check(item, f"{where}.{key}", members[key])
            if not blank(checked, key):
                made[key] = checked
        return made
    if type_name == "list":
        if not isinstance(value, list):
            raise ValueError(f"{where} must be an array")
        member = kind.member  # pyright: ignore[reportAttributeAccessIssue]
        return [
            checked
            for index, item in enumerate(value)
            if not blank(checked := check(item, f"{where}[{index}]", member))
        ]
    if type_name == "map":
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be a table")
        made = {}
        for key, item in value.items():
            checked = check(item, f"{where}.{key}", kind.value)  # pyright: ignore[reportAttributeAccessIssue]
            if not blank(checked):
                made[key] = checked
        return made
    if type_name == "string":
        if not isinstance(value, str):
            raise ValueError(f"{where} must be a string")
        found = policy.unknown(value, policy.VARIABLES)
        if found is not None:
            raise ValueError(f"{where} uses an unknown variable: {printable(found)}")
        return value
    if type_name in ("integer", "long"):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{where} must be an integer")
        return value
    if type_name == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{where} must be true or false")
        return value
    if type_name in ("double", "float"):
        if not _number(value):
            raise ValueError(f"{where} must be a number")
        # botocore は小数で読み戻すので、整数で書いても小数にして比べる
        return float(value)
    if type_name == "timestamp":
        if not isinstance(value, datetime.datetime):
            raise ValueError(f"{where} must be a date-time")
        return value
    raise ValueError(f"{where} has a type temt does not handle: {type_name}")


def check_keys(data: Mapping[str, Any], where: str) -> None:
    """対応づけのキーが重なっていないことを確かめる。

    NetworkInterfaces の DeviceIndex、TagSpecifications の ResourceType、タグの Key が
    重なれば誤りにする。重なると、比べるときも共通ファイルと重ねるときも要素を取り
    違える。BlockDeviceMappings の DeviceName は 1 つの要素でだけ省ける（ルート
    ボリュームを指す）。ほかのキーが無い要素は、AWS が断るので、ここでは確かめない。
    誤りは ValueError。
    """
    for path, items in _arrays(data, where):
        name = path.rsplit(".", 1)[-1].split("[", 1)[0]
        key = MATCH_KEYS.get(name)
        if key is None:
            continue
        seen: set[str] = set()
        missing = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            if key not in item:
                missing += 1
                continue
            text = json.dumps(item[key])
            if text in seen:
                raise ValueError(f"{path} has two elements with {key} = {printable(text)}")
            seen.add(text)
        if key == "DeviceName" and missing > 1:
            raise ValueError(f"{path} has two or more elements without {key}")


def _arrays(value: Any, where: str) -> Iterator[tuple[str, list[Any]]]:
    """value の中の配列を、位置とともにすべて返す。"""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _arrays(item, f"{where}.{key}")
    elif isinstance(value, list):
        yield where, value
        for index, item in enumerate(value):
            yield from _arrays(item, f"{where}[{index}]")


def merge(base: Any, over: Any, name: str = "") -> Any:
    """共通ファイルの [launch-template] base に、設定ファイルの over を重ねる。

    テーブルはキーごとに重ね、両方にある値は over にする。ただし WHOLE の項目は、over に
    書けば base を丸ごと置き換える。MATCH_KEYS の配列は、キーが同じ要素どうしを要素の中で
    重ね、キーが違う要素は両方を並べる（base の要素が先）。ほかの配列は、over に書けば
    base を丸ごと置き換える。name は over の項目の名前。
    """
    if isinstance(base, dict) and isinstance(over, dict):
        made = dict(base)
        for key, item in over.items():
            made[key] = merge(base[key], item, key) if key in base and key not in WHOLE else item
        return made
    key = MATCH_KEYS.get(name)
    if key is not None and isinstance(base, list) and isinstance(over, list):
        made_list = list(base)
        for item in over:
            found = next(
                (
                    index
                    for index, one in enumerate(made_list)
                    if isinstance(one, dict)
                    and isinstance(item, dict)
                    and one.get(key) == item.get(key)
                ),
                None,
            )
            if found is None:
                made_list.append(item)
            else:
                made_list[found] = merge(made_list[found], item)
        return made_list
    return over


def substitute(data: Any, values: Mapping[str, str]) -> Any:
    """変数 ${temt:name}、${temt:service} を置き換えたもの。"""
    return policy.substitute(data, values)


def profile_name(profile: Mapping[str, Any]) -> str | None:
    """IamInstanceProfile の名前。Arn で書かれていれば、その最後の要素。"""
    name = profile.get("Name")
    if isinstance(name, str) and name:
        return name
    arn = profile.get("Arn")
    return arn.rpartition("/")[2] if isinstance(arn, str) and arn else None


def _sort_key(name: str, item: Any) -> str:
    """配列の要素を並べるキー。MATCH_KEYS の配列はそのキーで、ほかは中身全体で並べる。"""
    key = MATCH_KEYS.get(name)
    if key is not None and isinstance(item, dict) and key in item:
        return json.dumps(item[key], sort_keys=True, default=str)
    return json.dumps(item, sort_keys=True, default=str)


def normalize(data: Any, path: tuple[str, ...] = ()) -> Any:
    """AWS が読み出しで変える形を揃えたもの。比べる前に、両側に掛ける。

    空の値を落とし、読み出したときにだけある項目を落とし、IamInstanceProfile を名前だけに
    し、配列を並べ替える（AWS は並び順を保証しない）。
    """
    if isinstance(data, dict):
        if path == (PROFILE,):
            name = profile_name(data)
            return {} if name is None else {"Name": name}
        made: dict[str, Any] = {}
        for key, item in data.items():
            inner = (*path, key)
            if inner in READ_ONLY:
                continue
            value = normalize(item, inner)
            if not blank(value, key):
                made[key] = value
        return made
    if isinstance(data, list):
        name = path[-1] if path else ""
        items = [value for item in data if not blank(value := normalize(item, path))]
        return sorted(items, key=lambda item: _sort_key(name, item))
    return data


def same(left: Any, right: Any) -> bool:
    """normalize したもの同士が同じか。True と 1 を取り違えないよう、JSON で比べる。"""
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(
        right, sort_keys=True, default=str
    )


def leaves(data: Any, path: str = "", name: str = "") -> dict[str, Any]:
    """normalize したものを、項目の位置から値への対応に開く。diff と apply の表示に使う。

    テーブルは . でつなぎ、MATCH_KEYS の配列の要素は [<キー>=<値>] で書き（要素の中の
    キーそのものは出さない）、ほかの配列は値として 1 つにまとめる。タグ（Key と Value
    だけの要素）は Tags[Key=<キー>] を位置とし、その値を Value にする。
    """
    found: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, item in data.items():
            found.update(leaves(item, f"{path}.{key}" if path else key, key))
        return found
    key = MATCH_KEYS.get(name)
    if key is not None and isinstance(data, list) and all(isinstance(one, dict) for one in data):
        for item in data:
            mark = f"{path}[{key}={_shown(item.get(key))}]"
            rest = {inner: value for inner, value in item.items() if inner != key}
            if name == "Tags" and list(rest) == ["Value"]:
                found[mark] = rest["Value"]
            elif rest:
                found.update(leaves(rest, mark))
            else:
                found[mark] = {}
        return found
    found[path] = data
    return found


def _shown(value: Any) -> str:
    """位置や値の表示。文字列と数はそのまま、文字列の配列は , でつなぎ、ほかは 1 行の
    JSON にする。
    """
    if value is None:
        return "-"
    if value == "":
        # NoDevice の値。何も見えない表示にしない
        return '""'
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, list) and all(isinstance(one, str) for one in value):
        return ",".join(value)
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def shown(value: Any) -> str:
    """diff の行に出す値の表示。無ければ -。"""
    return _shown(value)


def user_data_lines(value: Any) -> list[str] | None:
    """UserData の base64 を戻した文書の行。文字として読めなければ None。"""
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True).decode("utf-8").splitlines()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def user_data_size(value: Any) -> int:
    """UserData のバイト数。base64 として読めなければ、書かれた文字数。"""
    if not isinstance(value, str):
        return 0
    try:
        return len(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        return len(value)


def readable(data: Mapping[str, Any]) -> dict[str, Any]:
    """読み出した中身から、読み出したときにだけある項目を落としたもの。template show が
    設定ファイルの形で見せる。
    """
    return _without_read_only(dict(data), ())


def _without_read_only(value: Any, path: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_read_only(item, (*path, key))
            for key, item in value.items()
            if (*path, key) not in READ_ONLY
        }
    if isinstance(value, list):
        return [_without_read_only(item, path) for item in value]
    return value


def render(data: Mapping[str, Any], notes: Mapping[str, str] | None = None) -> list[str]:
    """中身を、設定ファイルの [launch-template] と同じ TOML の行にする。template show が使う。

    そのまま設定ファイルに写せる形にする。タグの並び（Tags）と IamInstanceProfile は
    インラインにする。notes は値（サブネットの ID など）から、
    その値の行の後ろに添えるコメントへの対応。
    """
    import tomlkit

    document = tomlkit.document()
    document.add("launch-template", _toml(data))
    lines = tomlkit.dumps(document).splitlines()
    shown = notes or {}
    if shown:
        lines = [_noted(line, shown) for line in lines]
    return lines


def _toml(value: Any, name: str = "") -> Any:
    """値を tomlkit の値にする。テーブルの配列は、Tags のような小さなものだけインラインにする。"""
    import tomlkit

    if isinstance(value, dict):
        if name in INLINE and _flat(value):
            return inline(value)
        table = tomlkit.table()
        scalars = {key: item for key, item in value.items() if not _nested(item, key)}
        nested = {key: item for key, item in value.items() if _nested(item, key)}
        for key, item in scalars.items():
            table.add(key, _toml(item, key))
        for key, item in nested.items():
            table.add(key, _toml(item, key))
        return table
    if isinstance(value, list) and value and all(isinstance(one, dict) for one in value):
        if all(_flat(one) for one in value) and name == "Tags":
            array = tomlkit.array()
            for one in value:
                array.append(inline(one))
            return array
        aot = tomlkit.aot()
        for one in value:
            aot.append(_toml(one, name))
        return aot
    return value


def inline(value: Mapping[str, Any]) -> Any:
    """値がどれも文字列か数のテーブルの、TOML のインラインテーブル。

    中かっこの内側に空白を置く（{ Name = "..." }）。tomlkit の inline_table は空白を
    置かないので、書いた文字列を読んで作る。conf も、設定ファイルに書くのに使う。
    """
    import tomlkit

    parts = ", ".join(
        f"{tomlkit.key(key).as_string()} = {tomlkit.item(item).as_string()}"
        for key, item in value.items()
    )
    # 行末の改行も付ける。無いと、後ろの見出しやコメントが同じ行につながる
    return tomlkit.parse(f"x = {{ {parts} }}\n" if parts else "x = {}\n")["x"]


def _nested(value: Any, name: str = "") -> bool:
    """TOML でテーブルかテーブルの配列として、項目の後ろに書くものか。"""
    if isinstance(value, dict):
        return not (name in INLINE and _flat(value))
    return isinstance(value, list) and bool(value) and all(isinstance(one, dict) for one in value)


def _flat(value: Mapping[str, Any]) -> bool:
    """値がどれも文字列か数のテーブルか。"""
    return all(isinstance(item, str | int | float | bool) for item in value.values())


def _noted(line: str, notes: Mapping[str, str]) -> str:
    """notes に当たる行の後ろに、# で始まるコメントを添える。"""
    for value, note in notes.items():
        if line.endswith(f'= "{value}"'):
            return f"{line}  # {note}"
    return line


def element(items: Sequence[Any], key: str, value: Any) -> Mapping[str, Any] | None:
    """配列の中で、キー key が value の要素。無ければ None。"""
    return next(
        (one for one in items if isinstance(one, dict) and one.get(key) == value),
        None,
    )
