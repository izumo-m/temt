"""メッセージの形と、書き出す先のテスト。"""

import io
import os
import re
import sys
from pathlib import Path

import pytest

from temt import msg
from temt.msg import TemtError


def test_info_has_a_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    msg.info("starting")
    shown = capsys.readouterr()
    assert re.fullmatch(r"\d\d:\d\d:\d\d starting\n", shown.out)
    assert shown.err == ""


def test_error_has_the_program_name(capsys: pytest.CaptureFixture[str]) -> None:
    msg.error("broken")
    assert capsys.readouterr().err == "temt: broken\n"


def test_warn_has_the_program_name(capsys: pytest.CaptureFixture[str]) -> None:
    msg.warn("check this")
    assert capsys.readouterr().err == "temt: warning: check this\n"


def test_tell_writes_what_it_is_given(capsys: pytest.CaptureFixture[str]) -> None:
    msg.tell("  1) key\n")
    msg.tell("pick one: ")
    shown = capsys.readouterr()
    assert shown.out == "  1) key\npick one: "
    assert shown.err == ""


def test_rows_align_the_labels(capsys: pytest.CaptureFixture[str]) -> None:
    msg.rows([("id", "key-0"), ("public key", "ssh-ed25519 AAAA")])
    assert capsys.readouterr().out == "  id:         key-0\n  public key: ssh-ed25519 AAAA\n"


def test_columns_follow_the_widest_line(capsys: pytest.CaptureFixture[str]) -> None:
    """列は最も広い行に揃える。最後の列は揃えない。"""
    msg.columns([("a", "bb", "last"), ("aaa", "b", "x")])
    assert capsys.readouterr().out == "  a    bb  last\n  aaa  b   x\n"


def test_side_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """side を付けたものは、標準出力をデータだけにするため、標準エラー出力に書く。"""
    msg.info("no differences", side=True)
    msg.tell("account 123456789012\n", side=True)
    shown = capsys.readouterr()
    assert shown.out == ""
    assert re.fullmatch(r"\d\d:\d\d:\d\d no differences\naccount 123456789012\n", shown.err)


def test_use_stderr_sends_everything_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """proxy の標準出力は ssh の通信路なので、use_stderr の後は何も書かない。"""
    msg.use_stderr()
    msg.info("waiting")
    msg.tell("told\n")
    msg.rows([("id", "key-0")])
    msg.columns([("a", "b")])
    msg.warn("check this")
    shown = capsys.readouterr()
    assert shown.out == ""
    lines = shown.err.splitlines()
    assert re.fullmatch(r"\d\d:\d\d:\d\d waiting", lines[0])
    assert lines[1:] == ["told", "  id: key-0", "  a  b", "temt: warning: check this"]


class Terminal(io.StringIO):
    """端末につながった標準入力のふり。"""

    def isatty(self) -> bool:
        return True


def test_confirm_writes_the_prompt_to_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """標準出力をファイルやパイプに回しても見えるよう、問いかけは標準エラー出力に書く。"""
    monkeypatch.setattr(sys, "stdin", Terminal("y\n"))
    msg.confirm("destroy dev-main?", yes=False)
    shown = capsys.readouterr()
    assert shown.out == ""
    assert shown.err == "destroy dev-main? [y/N] "


def test_confirm_closes_the_line_on_end_of_input(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-D で答えなければ、標準エラー出力の問いかけの行を閉じて取りやめる。"""
    monkeypatch.setattr(sys, "stdin", Terminal(""))
    with pytest.raises(TemtError, match="cancelled"):
        msg.confirm("destroy dev-main?", yes=False)
    shown = capsys.readouterr()
    assert shown.out == ""
    assert shown.err == "destroy dev-main? [y/N] \n"


class ClosedStdout:
    """読み手が先に閉じた標準出力の代わり。書き出すときに BrokenPipeError を上げる。

    on_write なら、書くときにも上げる。書けなかった分が残るので、書き出しも失敗する。
    """

    encoding: str = "utf-8"

    def __init__(self, fd: int, *, on_write: bool) -> None:
        self.fd = fd
        self.on_write = on_write

    def write(self, text: str) -> int:
        if self.on_write:
            raise BrokenPipeError(32, "Broken pipe")
        return len(text)

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    def fileno(self) -> int:
        return self.fd


@pytest.mark.parametrize("on_write", [True, False], ids=["write", "flush"])
def test_a_closed_stdout_is_dropped_quietly(
    on_write: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """標準出力の読み手が先に閉じても誤りを上げず、標準エラー出力には書き続ける。"""
    target = tmp_path / "out"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT)
    try:
        monkeypatch.setattr(sys, "stdout", ClosedStdout(fd, on_write=on_write))
        msg.info("starting")
        msg.tell("told\n")
        msg.warn("check this")
        msg.info("again")
        msg.error("broken")
        _ = os.write(fd, b"after")
    finally:
        os.close(fd)
    assert capsys.readouterr().err == "temt: warning: check this\ntemt: broken\n"
    # 終わるときに Python が書き出し直して誤りを出さないよう、/dev/null に付け替えている
    assert target.read_bytes() == b""


def test_a_closed_stdout_sends_everything_to_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動したときから標準出力が閉じていれば（>&-）、落ちずに標準エラー出力に書く。"""
    monkeypatch.setattr(sys, "stdout", None)
    msg.info("starting")
    msg.warn("check this")
    msg.flush()
    monkeypatch.undo()
    assert re.fullmatch(
        r"\d\d:\d\d:\d\d starting\ntemt: warning: check this\n", capsys.readouterr().err
    )


def test_raw_writes_the_bytes_as_they_are(capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    msg.raw("é\n".encode())
    assert capsysbinary.readouterr() == ("é\n".encode(), b"")
