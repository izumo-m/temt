"""利用者へのメッセージ、問いかけ、終了につながるエラー。

メッセージはすべて標準エラー出力に書く。proxy は ssh の ProxyCommand として動き、
標準出力は ssh の通信路だからである。
"""

import sys
import time
from collections.abc import Sequence

# エラーの行に付けるプログラム名
PROG = "temt"


class TemtError(Exception):
    """利用者に見せて終了するエラー。"""

    exit_code: int = 1


class UsageError(TemtError):
    """引数の誤り。"""

    exit_code: int = 2


def info(text: str) -> None:
    """進行を知らせる。時刻を付ける。"""
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} {text}\n")


def tell(text: str) -> None:
    """そのまま伝える。一覧や問いかけに使う。改行は呼ぶ側が付ける。"""
    sys.stderr.write(text)
    sys.stderr.flush()


def rows(pairs: Sequence[tuple[str, str]]) -> None:
    """ラベルを揃えて、1 行に 1 項目を書く。詳細表示に使う。"""
    width = max(len(label) for label, _ in pairs)
    for label, value in pairs:
        tell(f"  {label + ':':<{width + 1}} {value}\n")


def columns(lines: Sequence[Sequence[str]]) -> None:
    """一覧の 1 行に 1 つずつ書く。列の幅は最も広い行に揃え、最後の列は揃えない。"""
    sizes = [max(len(line[column]) for line in lines) for column in range(len(lines[0]) - 1)]
    for line in lines:
        cells = [cell.ljust(size) for cell, size in zip(line[:-1], sizes, strict=True)]
        tell(f"  {'  '.join(cells)}  {line[-1]}\n")


def confirm(question: str, *, yes: bool) -> None:
    """[y/N] で問いかけ、y と答えなければ終了する。yes なら問いかけない。

    プロンプトは標準エラー出力に書き、答えは標準入力から読む。input() はプロンプト
    を標準出力に出すので使わない。
    """
    if yes:
        return
    if sys.stdin is None or not sys.stdin.isatty():
        raise TemtError("cannot ask for confirmation: stdin is not a terminal; pass --yes")
    tell(f"{question} [y/N] ")
    answer = sys.stdin.readline()
    if not answer:
        # Ctrl-D は端末に何も出さないので、行を閉じる
        tell("\n")
    if answer.strip() not in ("y", "Y"):
        raise TemtError("cancelled")


def warn(text: str) -> None:
    """注意を知らせる。プログラム名を付ける。"""
    sys.stderr.write(f"{PROG}: warning: {text}\n")


def error(text: str) -> None:
    """エラーを知らせる。プログラム名を付ける。"""
    sys.stderr.write(f"{PROG}: {text}\n")
