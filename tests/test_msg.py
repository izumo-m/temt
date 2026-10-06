"""メッセージの形のテスト。"""

import re

import pytest

from temt import msg


def test_info_has_a_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    msg.info("starting")
    assert re.fullmatch(r"\d\d:\d\d:\d\d starting\n", capsys.readouterr().err)


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
    assert shown.err == "  1) key\npick one: "
    # 標準出力は ssh の通信路なので、何も書かない
    assert shown.out == ""


def test_rows_align_the_labels(capsys: pytest.CaptureFixture[str]) -> None:
    msg.rows([("id", "key-0"), ("public key", "ssh-ed25519 AAAA")])
    assert capsys.readouterr().err == "  id:         key-0\n  public key: ssh-ed25519 AAAA\n"


def test_columns_follow_the_widest_line(capsys: pytest.CaptureFixture[str]) -> None:
    """列は最も広い行に揃える。最後の列は揃えない。"""
    msg.columns([("a", "bb", "last"), ("aaa", "b", "x")])
    assert capsys.readouterr().err == "  a    bb  last\n  aaa  b   x\n"
