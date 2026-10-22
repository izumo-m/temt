"""利用者へのメッセージ、問いかけ、終了につながるエラー。

進行の行や詳細の表示は標準出力に、警告、エラー、問いかけは標準エラー出力に書く。
side を付けたものも標準エラー出力に書く。表だけを標準出力に残すための環境の行、diff と
sg diff の no differences、警告やエラーに添える行である。標準出力が閉じていれば、
標準出力に書くものも標準エラー出力に書く。proxy は ssh の ProxyCommand として動き、標準出力は
ssh の通信路なので、use_stderr を呼んで、すべてを標準エラー出力に書く。diff と
sg diff は、変更だけを標準出力に残すよう、stderr_only のあいだ同じようにする。

順序を保つため、どちらかに書く前に、もう一方を書き出す。標準出力がパイプでも長い
処理の進行がすぐ見えるよう、書くたびに書き出す。
"""

import os
import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, suppress
from typing import TextIO

# 真なら、標準出力に書くものも標準エラー出力に書く。proxy が use_stderr で、diff と
# sg diff が stderr_only のあいだ立てる
_only_stderr = False

# エラーの行に付けるプログラム名
PROG = "temt"


class TemtError(Exception):
    """利用者に見せて終了するエラー。"""

    exit_code: int = 1


class UsageError(TemtError):
    """引数の誤り。"""

    exit_code: int = 2


def use_stderr() -> None:
    """これより後、標準出力に書くものも標準エラー出力に書く。proxy が最初に呼ぶ。"""
    global _only_stderr
    _only_stderr = True


@contextmanager
def stderr_only() -> Generator[None]:
    """このあいだだけ、標準出力に書くものも標準エラー出力に書く。

    diff と sg diff が、標準出力に変更だけを残すために使う。変更は table.out で書くので、
    これに関わらず標準出力に出る。
    """
    global _only_stderr
    before = _only_stderr
    _only_stderr = True
    try:
        yield
    finally:
        _only_stderr = before


def _drop(stream: TextIO) -> None:
    """stream の読み手（head など）が先に閉じた。残りを捨てる。

    終わるときに Python が書き出し直して同じ誤りを出さないよう、/dev/null に付け替える。
    標準エラー出力も、2>&1 | head のように標準出力と同じ読み手なら先に閉じうる。その
    ときも、標準出力と同じく、コマンドは止めずに最後まで進める。
    """
    os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())


def _flush(stream: TextIO | None) -> None:
    """stream を書き出す。閉じた出力（None）は飛ばし、読み手が閉じていれば捨てる。"""
    if stream is None:
        return
    try:
        stream.flush()
    except BrokenPipeError:
        _drop(stream)


def flush() -> None:
    """標準出力と標準エラー出力を書き出す。外部コマンドに端末を渡す前に呼ぶ。"""
    _flush(sys.stdout)
    _flush(sys.stderr)


def _write(stream: TextIO | None, text: str) -> None:
    """text を stream に書いて書き出す。先に、もう一方の出力を書き出して順序を保つ。

    ロケールの文字コードで書けない文字（ASCII しか書けないロケールの日本語など）は、
    標準エラー出力の既定と同じく、バックスラッシュの形にする。
    """
    if stream is None:
        # 起動したときから閉じている（2>&-）。書く先が無い
        return
    _flush(sys.stderr if stream is sys.stdout else sys.stdout)
    encoding = stream.encoding or "utf-8"
    # 読み手が先に閉じていれば、続く書き出しも失敗するので、そこで捨てる
    with suppress(BrokenPipeError):
        _ = stream.write(text.encode(encoding, "backslashreplace").decode(encoding))
    _flush(stream)


def _out() -> TextIO | None:
    """標準出力に書くものの書き先。proxy と diff の中か、標準出力が閉じていれば標準エラー出力。

    標準エラー出力も閉じていれば None。
    """
    stdout: TextIO | None = sys.stdout
    stderr: TextIO | None = sys.stderr
    return stderr if _only_stderr or stdout is None else stdout


def _say(text: str, *, side: bool) -> None:
    """標準出力に書く。side なら標準エラー出力に書く。"""
    _write(sys.stderr if side else _out(), text)


def raw(data: bytes) -> None:
    """外部コマンドの出力 data を、そのままのバイト列で標準出力に書く。

    proc.stream が、install と uninstall の出力を流すのに使う。読み手が先に閉じても、
    残りを捨てるだけで、外部コマンドは止めない。
    """
    stream = _out()
    if stream is None:
        return
    _flush(sys.stderr if stream is sys.stdout else sys.stdout)
    _flush(stream)
    try:
        stream.buffer.write(data)
        stream.buffer.flush()
    except BrokenPipeError:
        _drop(stream)


def info(text: str, *, side: bool = False) -> None:
    """進行を知らせる。時刻を付ける。"""
    _say(f"{time.strftime('%H:%M:%S')} {text}\n", side=side)


def tell(text: str, *, side: bool = False) -> None:
    """そのまま伝える。一覧や詳細の表示に使う。改行は呼ぶ側が付ける。"""
    _say(text, side=side)


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


def confirm(question: str | Callable[[], str], *, yes: bool) -> None:
    """[y/N] で問いかけ、y と答えなければ終了する。yes なら問いかけない。

    question は問いの文か、問う直前に文を作る関数である。関数は、問いかけないとき（yes の
    ときと、標準入力が端末でないとき）には呼ばない。文に AWS から引く値を入れるのに使う。
    プロンプトは、標準出力をファイルやパイプに回しても見えるよう、標準エラー出力に書き、
    答えは標準入力から読む。input() はプロンプトを標準出力に出すので使わない。
    """
    if yes:
        return
    if sys.stdin is None or not sys.stdin.isatty():
        raise TemtError("cannot ask for confirmation: stdin is not a terminal; pass --yes")
    if callable(question):
        question = question()
    _write(sys.stderr, f"{question} [y/N] ")
    try:
        answer = sys.stdin.readline()
    except UnicodeDecodeError:
        # 端末の文字コードで読めない答えは、y ではない
        answer = "?\n"
    if not answer:
        # Ctrl-D は端末に何も出さないので、行を閉じる
        _write(sys.stderr, "\n")
    if answer.strip() not in ("y", "Y"):
        raise TemtError("cancelled")


def warn(text: str) -> None:
    """注意を知らせる。プログラム名を付ける。"""
    _write(sys.stderr, f"{PROG}: warning: {text}\n")


def error(text: str) -> None:
    """エラーを知らせる。プログラム名を付ける。"""
    _write(sys.stderr, f"{PROG}: {text}\n")
