"""manifest.toml の読み込み、アプリ ID で使うアプリの場所、app list の本体。

アプリ ID で指したアプリは、conf ディレクトリの apps/ から探し、無ければ temt と一緒に
配るアプリを使う。app list は、--svc の有無にかかわらず、そうして使えるアプリだけを
挙げ、AWS は呼ばない。boto3 を使わないので、cli.run は botocore を読み込む前にこれを呼ぶ。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from temt import msg, names, options, table
from temt.msg import TemtError
from temt.names import USER_NAME_RULE, app_ids, is_user_name, user_apps_dir
from temt.table import tilde

if TYPE_CHECKING:
    from pathlib import Path

    from temt.options import Option

MANIFEST = "manifest.toml"
# manifest.toml の項目。書かなければならないものと、省けるもの
REQUIRED = ("name", "version")
# user は install を実行するユーザーの既定。--user で変えられる
OPTIONAL = ("description", "user")
# オプションの宣言を置くテーブル [options.<名前>]
OPTIONS_TABLE = "options"


@dataclass(frozen=True)
class Manifest:
    """manifest.toml の中身。"""

    name: str
    version: str
    description: str | None = None
    # install を実行するユーザーの既定。None なら root
    user: str | None = None
    # 宣言したオプション。並びは書かれた順
    options: dict[str, Option] = field(default_factory=dict)


def read_manifest(path: Path) -> Manifest:
    """手元のアプリ path の manifest.toml を読む。"""
    file = path / MANIFEST
    # 誤りの頭に付ける場所。パスは利用者の手元から来るので、表示できない文字を置き換える
    where = table.printable(tilde(file))
    try:
        data = file.read_bytes()
    except FileNotFoundError:
        raise TemtError(f"no {MANIFEST} in {table.printable(tilde(path))}") from None
    except OSError as e:
        raise TemtError(f"{where}: {e.strerror}") from None
    return parse_manifest(data, where)


def parse_manifest(data: bytes, where: str) -> Manifest:
    """manifest.toml の中身 data を読む。where は誤りの頭に付ける場所で、表示できる形にしてある。

    options を除くどの項目も空でない文字列で、user はユーザー名の規則に従う。options は
    [options.<名前>] のテーブルで、options.declarations が確かめる。インスタンスの上の
    manifest.toml も、これで読む。
    """
    try:
        found = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise TemtError(f"{where}: not valid UTF-8: {e.reason}") from None
    except tomllib.TOMLDecodeError as e:
        raise TemtError(f"{where}: {e}") from None
    texts: dict[str, str] = {}
    for key, value in found.items():
        if key == OPTIONS_TABLE:
            continue
        if key not in REQUIRED + OPTIONAL:
            raise TemtError(f"{where}: unknown key: {options.visible(key)}")
        if not isinstance(value, str) or not value:
            raise TemtError(f"{where}: {key} must be a non-empty string")
        texts[key] = value
    for key in REQUIRED:
        if key not in texts:
            raise TemtError(f"{where}: {key} is missing")
    user = texts.get("user")
    if user is not None and not is_user_name(user):
        raise TemtError(f"{where}: user {USER_NAME_RULE}: {options.visible(user)}")
    declared = {}
    if OPTIONS_TABLE in found:
        declared = options.declarations(found[OPTIONS_TABLE], where)
    return Manifest(
        name=texts["name"],
        version=texts["version"],
        description=texts.get("description"),
        user=user,
        options=declared,
    )


# app list の列。LOCAL は manifest.toml の版
COLUMNS = table.Columns(defaults=("ID", "NAME", "LOCAL", "PATH"), extras=("DESCRIPTION",))


def find_app(app_id: str) -> Path | None:
    """アプリ ID で使うアプリのディレクトリ。どこにも無ければ None。

    conf ディレクトリの apps/<アプリID>/ を先に見て、無ければ temt と一緒に配るアプリを
    使う。利用者は同じ名前で置けば、temt と一緒に配るアプリの代わりに使える。
    ディレクトリでないもの（壊れたリンクを含む）は無いものとみなす。
    """
    for place in (user_apps_dir(), names.BUNDLED_APPS):
        path = place / app_id
        if path.is_dir():
            return path
    return None


def known_apps() -> list[str]:
    """アプリ ID で使えるアプリの ID を、名前の順に返す。"""
    return sorted({*app_ids(user_apps_dir()), *app_ids(names.BUNDLED_APPS)})


def local_apps() -> list[tuple[str, Path, Manifest | None]]:
    """アプリ ID で使えるアプリと、そのディレクトリと manifest.toml を、アプリ ID の順に返す。

    manifest.toml が読めなければ、警告して None とする。
    """
    found: list[tuple[str, Path, Manifest | None]] = []
    for app_id in known_apps():
        path = find_app(app_id)
        if path is None:
            continue
        try:
            manifest = read_manifest(path)
        except TemtError as e:
            msg.warn(f"cannot read the manifest of {app_id}: {e}")
            manifest = None
        found.append((app_id, path, manifest))
    return found


def list_apps(spec: str | None) -> None:
    """アプリ ID で使えるアプリを、1 つ 1 行の表で挙げる。"""
    chosen = COLUMNS.choose(spec)
    rows = [
        {
            "ID": app_id,
            "NAME": manifest.name if manifest else "",
            "LOCAL": manifest.version if manifest else "",
            "PATH": tilde(path),
            "DESCRIPTION": (manifest.description or "") if manifest else "",
        }
        for app_id, path, manifest in local_apps()
    ]
    table.write(chosen, rows)
