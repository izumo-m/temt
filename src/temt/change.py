"""apply が検出する差分の 1 つ 1 つと、それを見せる形。

apply の各手順は、対象ごとに差分を検出し、それを Change の並びにして見せられる。
diff は標準出力に、apply は確認を求める前に標準エラー出力に、同じ形で並べて出す。
何を変えるかは各手順が決める。
"""

import difflib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from temt import table

# 行の頭の印。作る・足す、直す、消す
ADD = "+"
UPDATE = "~"
REMOVE = "-"
# 続く行の字下げ
INDENT = "    "
# 値の違いの書き方の矢印
ARROW = " -> "


@dataclass(frozen=True)
class Change:
    """変更 1 つ。kind は対象の種類、name はその名前、detail は違い。

    body は続けて字下げして出す行で、中身の違い（unified diff）などを入れる。
    """

    mark: str
    kind: str
    name: str
    detail: str = ""
    body: tuple[str, ...] = ()


def shown(value: Any) -> str:
    """値の表示。文字列と数はそのまま、無ければ table.EMPTY、ほかは詰めた JSON にする。"""
    if value is None:
        return table.EMPTY
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def moved(before: Any, after: Any) -> str:
    """値の違い。<今の値> -> <新しい値>。"""
    return f"{shown(before)}{ARROW}{shown(after)}"


def tags(pairs: Sequence[tuple[str, str]]) -> str:
    """足すタグの表示。tags: <キー>=<値>, ...。"""
    return "tags: " + ", ".join(f"{key}={value}" for key, value in pairs)


def lines_diff(before: Sequence[str], after: Sequence[str]) -> tuple[str, ...]:
    """行の並びの違いを、unified diff の形で返す。ファイル名の見出しは付けない。

    見出しは、違いがあるときに先頭に来る --- と +++ の 2 行だけである。中身の行も
    - や + で始まるので、頭の文字では見分けず、先頭の 2 行を落とす。
    """
    found = difflib.unified_diff(list(before), list(after), lineterm="", n=2)
    return tuple(found)[2:]


def json_lines(document: Any) -> list[str]:
    """JSON の中身を、キーを並べ替えて字下げした行にする。無ければ空。"""
    if document is None:
        return []
    return json.dumps(document, indent=2, sort_keys=True).split("\n")


def write(changes: Sequence[Change]) -> None:
    """変更を、shown_changes の形で標準出力に書く。diff が使う。"""
    table.out(shown_changes(changes))


def shown_changes(changes: Sequence[Change]) -> str:
    """変更を 1 つ 1 行で、印、種類、名前、違いを揃えた文字列にする。

    違いは揃えず、無ければ書かない。body の行は、その変更の行に続けて字下げして書く。
    body の行はどれも字下げで始まるので、空の行にはならない。body のタブは空白に開き、
    ほかの表示できない文字は table.UNPRINTABLE にする。
    """
    if not changes:
        return ""
    heads = [[change.mark, change.kind, table.printable(change.name)] for change in changes]
    sizes = [max(len(head[column]) for head in heads) for column in range(len(heads[0]))]
    text = ""
    for change, head in zip(changes, heads, strict=True):
        cells = [cell.ljust(size) for cell, size in zip(head, sizes, strict=True)]
        if change.detail:
            cells.append(table.printable(change.detail))
        text += table.GAP.join(cells).rstrip() + "\n"
        text += "".join(
            f"{INDENT}{table.printable(line.expandtabs().rstrip())}\n" for line in change.body
        )
    return text
