"""list コマンドの表。docker に倣い、見出しの行と 1 件 1 行の表を出す。

表は標準出力に書き、grep や awk に渡せるようにする。見出しを付けずに列を揃える
write_lines は sg diff が使う。標準出力に書く out は、app get の値と、diff の変更
（change.write）にも使う。環境の行や警告は、他のメッセージと同じく msg が標準エラー
出力に書く。proxy は表を出さないので、ssh の通信路は乱さない。
"""

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from temt.msg import UsageError

# 値の無いところの表示
EMPTY = "-"
# 表示できない文字（改行やタブなど）の代わり
UNPRINTABLE = "?"
# 列の間の空白
GAP = "   "
# --format の列の区切り
SEPARATOR = ","


@dataclass(frozen=True)
class Columns:
    """1 つの list コマンドの列。名前は見出しそのもので、空白を含まない英大文字の 1 語。"""

    # 既定で出す列。並びは表の並び
    defaults: tuple[str, ...]
    # --format で選んだときだけ出す列
    extras: tuple[str, ...] = ()

    def choose(self, spec: str | None) -> tuple[str, ...]:
        """--format の値から、出す列を並べる。無ければ既定の列。

        列は見出しの名前で、大文字小文字を問わない。知らない名前、重なった名前、空の
        名前は誤りとし、使える名前を挙げる。AWS を呼ぶ前に確かめられるよう、呼ぶ側は
        最初にこれを呼ぶ。
        """
        if spec is None:
            return self.defaults
        known = self.defaults + self.extras
        use = f"use {SEPARATOR.join(known)}"
        chosen: list[str] = []
        for word in spec.split(SEPARATOR):
            name = word.strip().upper()
            if name not in known:
                raise UsageError(
                    f"unknown column in --format: {printable(word.strip() or '(empty)')}; {use}"
                )
            if name in chosen:
                raise UsageError(f"repeated column in --format: {name}; {use}")
            chosen.append(name)
        return tuple(chosen)


def printable(value: str) -> str:
    """表示できない文字（改行、タブ、ESC など）を UNPRINTABLE にした値。

    AWS、インスタンス、外部コマンドから来る値と、利用者の手元から来る値（設定ファイル、
    引数、環境変数、パス）を端末に出すときに、誤り、警告、進行の行の区別なく使い、
    制御文字を端末に届けない。
    """
    return "".join(char if char.isprintable() else UNPRINTABLE for char in value)


def aws_error(e: Exception) -> str:
    """botocore の例外の文を 1 行にしたもの。

    プロファイル名など利用者が書いた値を含むので、表示できない文字を置き換える。引数の
    検査の誤りのように複数行のものは、各行の頭と末尾の空白を落とし、空の行を飛ばして、
    行を "; " でつなぐ。ただし ":" で終わる行には、その続きとして " " でつなぐ。
    """
    text = ""
    for line in str(e).splitlines():
        line = line.strip()
        if not line:
            continue
        if text:
            text += " " if text.endswith(":") else "; "
        text += printable(line)
    return text


def _cell(value: str | None) -> str:
    """表に書く値。空なら EMPTY、表示できない文字は UNPRINTABLE にする。"""
    if not value:
        return EMPTY
    return printable(value)


def write(chosen: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    """見出しと行を、列を揃えて標準出力に書く。最後の列は揃えない。

    rows は列の名前から値への対応で、chosen に無い列は見ない。空の値は EMPTY にする。
    改行やタブなど、表示できない文字は UNPRINTABLE にして、1 件 1 行を崩さない。
    """
    lines = [list(chosen)] + [[_cell(row.get(name)) for name in chosen] for row in rows]
    out(_aligned(lines))


def write_lines(lines: Sequence[Sequence[str]]) -> None:
    """見出しを付けずに、行を列を揃えて標準出力に書く。最後の列は揃えない。

    sg diff が使う。値の扱いは write と同じで、空の値は EMPTY にし、表示できない文字は
    UNPRINTABLE にする。
    """
    if lines:
        out(_aligned([[_cell(cell) for cell in line] for line in lines]))


def _aligned(lines: Sequence[Sequence[str]]) -> str:
    """行の並びを、列を揃えた文字列にする。行はどれも同じ数の列を持つ。"""
    sizes = [max(len(line[column]) for line in lines) for column in range(len(lines[0]))]
    return "".join(
        GAP.join(
            [*(cell.ljust(size) for cell, size in zip(line[:-1], sizes, strict=False)), line[-1]]
        )
        + "\n"
        for line in lines
    )


def out(text: str) -> None:
    """text をそのまま UTF-8 で標準出力に書く。読み手が先に閉じれば、何も言わずにやめる。

    表と、app get の値と、diff の変更に使う。ロケールの文字コードで書くと、ASCII しか書けない
    ロケールでは値を出せないので、UTF-8 のバイト列で書く。
    """
    try:
        sys.stdout.flush()
        sys.stdout.buffer.write(text.encode())
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        # 読み手（head など）が先に閉じた。残りは捨て、終わるときに Python が書き出し
        # 直して同じ誤りを出さないよう、標準出力を /dev/null に付け替える
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
