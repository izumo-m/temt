"""アプリのオプション。manifest.toml の宣言、deploy で使う値の決め方、install への渡し方。

アプリは、deploy で値を変えられるオプションを manifest.toml の [options.<名前>] で宣言
する。値はコマンドラインの <名前>=<値> で与え、deploy の記録に残す。次の deploy は、記録に
残った値を引き継ぎ、コマンドラインの値で名前ごとに上書きする。<名前>= はその名前を既定に
戻し、--reset は何も引き継がない。

使う値は、宣言したすべてのオプションについて既定で埋め、アプリのディレクトリに足す
ファイル temt-options.env で install に渡す。値はシェルの引用符で囲んで書くので、
install が . で読み込んでも展開されない。値はコマンドラインを通らず、tar の中身として
運ばれる。

boto3 を使わないので、cli からも読み込める。
"""

from __future__ import annotations

import difflib
import re
import shlex
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from temt import msg
from temt.msg import TemtError
from temt.names import LABEL_RULE, is_label

# temt がアプリのディレクトリに足すファイル。アプリは同じ名前のファイルを置けない
FILE = "temt-options.env"
# ファイルに書く変数の名前の頭。続けて、オプションの名前を大文字にし、- を _ にしたもの
VARIABLE_PREFIX = "TEMT_OPTION_"
# 使う値の出どころ。コマンドライン、前の deploy の記録、宣言の既定
GIVEN = "given"
KEPT = "kept"
DEFAULT = "default"
# [options.<名前>] に書ける項目
KEYS = ("description", "default", "pattern")
# 値の規則。制御文字（Unicode の Cc）を除けば何でも使える
CONTROL = "Cc"
VALUE_RULE = "must not contain control characters such as newlines and tabs"
# 表示で置き換える文字の種類。制御文字、対にならないサロゲート、行と段落の区切り
HIDDEN_CATEGORIES = (CONTROL, "Cs", "Zl", "Zp")
# 表示で置き換える、文字の向きに関わる制御文字（U+061C、U+200E、U+200F、U+202A〜202E、
# U+2066〜2069）
BIDI_CONTROLS = frozenset(
    map(chr, (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)))
)
# 置き換えた文字の代わり
HIDDEN = "?"


@dataclass(frozen=True)
class Option:
    """manifest.toml で宣言した 1 つのオプション。"""

    name: str
    description: str
    # 既定。None なら既定が無く、deploy のたびに、コマンドラインか記録から値が要る
    default: str | None = None
    # 値の全体が一致すべき正規表現。None なら値の規則だけを確かめる
    pattern: re.Pattern[str] | None = None

    def accepts(self, value: str) -> bool:
        """値の全体が pattern に一致するか。pattern が無ければ何でも受け付ける。"""
        return self.pattern is None or self.pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class Choice:
    """deploy で使う 1 つの値と、その出どころ（GIVEN、KEPT、DEFAULT）。"""

    name: str
    value: str
    source: str


def has_control(value: str) -> bool:
    """値に制御文字（改行、タブ、ESC など）を含むか。"""
    return any(unicodedata.category(char) == CONTROL for char in value)


def is_utf8(value: str) -> bool:
    """UTF-8 にできる値か。UTF-8 でないバイト列は、Python が対にならないサロゲートとして読む。"""
    try:
        _ = value.encode()
    except UnicodeEncodeError:
        return False
    return True


def is_value(value: str) -> bool:
    """オプションの値として使える文字列か。記録から読んだ値を確かめる。"""
    return is_utf8(value) and not has_control(value)


def visible(text: str) -> str:
    """端末を乱す文字だけを HIDDEN にした表示。

    制御文字、対にならないサロゲート、行と段落の区切り、文字の向きに関わる制御文字を
    置き換える。全角空白や、ゼロ幅接合子でつないだ絵文字、Python が知らない新しい文字は
    そのまま出す。値は記録や manifest.toml から来ることがあるので、表示の前に通す。
    """
    return "".join(
        HIDDEN if unicodedata.category(char) in HIDDEN_CATEGORIES or char in BIDI_CONTROLS else char
        for char in text
    )


def quoted(value: str) -> str:
    """値の表示。そのままコマンドラインに打てるようシェルの引用符で囲み、visible を通す。"""
    return visible(shlex.quote(value))


def show(name: str, value: str) -> str:
    """<名前>=<値> の表示。値は quoted で表す。"""
    return f"{name}={quoted(value)}"


def show_all(values: Mapping[str, str]) -> str:
    """値の並びを、空白で区切った <名前>=<値> の並びにする。"""
    return " ".join(show(name, value) for name, value in values.items())


def variable(name: str) -> str:
    """temt-options.env に書く変数の名前。名前の規則に _ は無いので、重ならない。"""
    return VARIABLE_PREFIX + name.upper().replace("-", "_")


def declarations(found: object, where: str) -> dict[str, Option]:
    """manifest.toml の options の値から、宣言の並びを作る。並びは書かれた順。

    where は誤りの頭に付ける、manifest.toml の場所で、表示できない文字は置き換えてある。
    誤りは TemtError で止める。
    """
    if not isinstance(found, dict):
        raise TemtError(f"{where}: options must be tables such as [options.<name>]")
    declared: dict[str, Option] = {}
    for name, items in found.items():
        if not isinstance(name, str) or not is_label(name):
            raise TemtError(f"{where}: the option name {LABEL_RULE}: {visible(str(name))}")
        if not isinstance(items, dict):
            raise TemtError(f"{where}: options.{name} must be a table")
        declared[name] = _declaration(name, items, f"{where}: options.{name}")
    return declared


def _declaration(name: str, items: dict[str, object], where: str) -> Option:
    """[options.<名前>] の 1 つを読む。"""
    for key in items:
        if key not in KEYS:
            raise TemtError(f"{where}: unknown key: {visible(key)}")
    description = items.get("description")
    if description is None:
        raise TemtError(f"{where}: description is missing")
    if not isinstance(description, str) or not description:
        raise TemtError(f"{where}: description must be a non-empty string")
    if has_control(description):
        raise TemtError(f"{where}: description {VALUE_RULE}")
    pattern = None
    text = items.get("pattern")
    if text is not None:
        if not isinstance(text, str) or not text:
            raise TemtError(f"{where}: pattern must be a non-empty string")
        try:
            pattern = re.compile(text)
        except re.error as e:
            raise TemtError(f"{where}: bad pattern: {visible(str(e))}") from None
    default = items.get("default")
    if default is not None:
        if not isinstance(default, str):
            raise TemtError(f"{where}: default must be a string")
        if has_control(default):
            raise TemtError(f"{where}: default {VALUE_RULE}")
    option = Option(name=name, description=description, default=default, pattern=pattern)
    if default is not None and not option.accepts(default):
        raise TemtError(f"{where}: the default {quoted(default)} does not match the pattern")
    return option


def _unknown(app_id: str, name: str, declared: Mapping[str, Option]) -> TemtError:
    """宣言していない名前の誤り。近い名前と、宣言している名前を添える。"""
    if not declared:
        return TemtError(f"{app_id} has no options; it does not take {name}")
    text = f"{app_id} has no option {name}"
    close = difflib.get_close_matches(name, list(declared), n=1)
    if close:
        text += f"; did you mean {close[0]}?"
    return TemtError(f"{text} (options: {', '.join(declared)})")


def check_given(
    app_id: str, declared: Mapping[str, Option], given: Mapping[str, str | None]
) -> None:
    """コマンドラインの値を宣言と照らす。AWS にもインスタンスにも触る前に呼ぶ。

    given は名前から値への対応で、値が None なら <名前>=（既定に戻す）である。名前と値の
    形は cli が確かめてある。
    """
    for name, value in given.items():
        option = declared.get(name)
        if option is None:
            raise _unknown(app_id, name, declared)
        if value is None:
            if option.default is None:
                raise TemtError(f"{app_id}: {name} has no default; give {name}=<value>")
        elif not option.accepts(value):
            raise TemtError(
                f"{app_id}: bad value for {name}: {quoted(value)} ({visible(option.description)})"
            )


def choose(
    app_id: str,
    version: str,
    declared: Mapping[str, Option],
    given: Mapping[str, str | None],
    kept: Mapping[str, str] | None,
    *,
    record: str,
) -> tuple[dict[str, str], list[Choice]]:
    """deploy で使う値を決める。記録に書く値と、使う値の並びを返す。

    kept は前の deploy の記録に残った値で、引き継がない（--reset）か記録が無ければ空、
    記録が読めなければ None である。読めないまま引き継ぐと黙って既定に戻るので止める。
    宣言から消えた名前は、警告して外す。引き継いだ値が新しい版の pattern に合わなければ
    止める。記録に書く値は、コマンドラインか記録から来た値だけで、既定で埋めたものは書かない。
    record は記録の名前で、誤りに添える。check_given を済ませてあるものとする。
    """
    if kept is None:
        raise TemtError(
            f"cannot read the options in the deploy record {record}; give the options again "
            f"with --reset; nothing was changed"
        )
    chosen: dict[str, str] = {}
    for name, value in kept.items():
        option = declared.get(name)
        if option is None:
            msg.warn(
                f"dropping {show(name, value)} of the deploy record; {app_id} "
                f"{visible(version)} does not declare it"
            )
            continue
        if name in given:
            continue
        if not option.accepts(value):
            back = "" if option.default is None else f", or {name}= for the default"
            raise TemtError(
                f"the deploy record has {show(name, value)}, which {app_id} "
                f"{visible(version)} does not accept; give {name}=<value>{back}; nothing was "
                f"changed"
            )
        chosen[name] = value
    for name, value in given.items():
        if value is not None:
            chosen[name] = value
    explicit: dict[str, str] = {}
    choices: list[Choice] = []
    for name, option in declared.items():
        if name in chosen:
            explicit[name] = chosen[name]
            source = GIVEN if given.get(name) is not None else KEPT
            choices.append(Choice(name, chosen[name], source))
        elif option.default is not None:
            choices.append(Choice(name, option.default, DEFAULT))
        else:
            raise TemtError(
                f"{app_id} needs the option {name}; give {name}=<value>; nothing was changed"
            )
    return explicit, choices


def env_file(app_id: str, version: str, choices: list[Choice]) -> bytes:
    """temt-options.env の中身。1 行に 1 つ、変数に値を引用符で囲んで代入する。"""
    # 版は manifest.toml から来るので、改行などでコメントの外に出ないようにする
    lines = [f"# Written by temt deploy: the options of {app_id} {visible(version)}.\n"]
    lines += [f"{variable(one.name)}={shlex.quote(one.value)}\n" for one in choices]
    return "".join(lines).encode()
