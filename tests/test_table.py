"""list コマンドの表のテスト。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from temt import table
from temt.msg import UsageError

COLUMNS = table.Columns(defaults=("ID", "NAME"), extras=("SIZE",))


def test_choose_takes_the_defaults_without_a_format() -> None:
    assert COLUMNS.choose(None) == ("ID", "NAME")


def test_choose_follows_the_order_of_the_format() -> None:
    """大文字小文字を問わず、前後の空白も除く。"""
    assert COLUMNS.choose("size, id") == ("SIZE", "ID")


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("id,color", "unknown column in --format: color; use ID,NAME,SIZE"),
        ("id,,name", r"unknown column in --format: \(empty\); use ID,NAME,SIZE"),
        ("id,Id", "repeated column in --format: ID; use ID,NAME,SIZE"),
    ],
)
def test_choose_rejects(spec: str, message: str) -> None:
    with pytest.raises(UsageError, match=message):
        COLUMNS.choose(spec)


def test_write_aligns_the_columns(capsys: pytest.CaptureFixture[str]) -> None:
    """最後の列は揃えず、値の無いところは - と出す。表は標準出力に書く。"""
    table.write(
        ("ID", "NAME", "SIZE"),
        [
            {"ID": "a", "NAME": "long-name", "SIZE": "1"},
            {"ID": "bbbb", "NAME": "", "SIZE": ""},
        ],
    )
    out, err = capsys.readouterr()
    assert err == ""
    assert out.splitlines() == [
        "ID     NAME        SIZE",
        "a      long-name   1",
        "bbbb   -           -",
    ]


def test_write_only_the_header_without_rows(capsys: pytest.CaptureFixture[str]) -> None:
    table.write(("ID", "NAME"), [])
    assert capsys.readouterr().out == "ID   NAME\n"


class ClosedPipe:
    """読み手が先に閉じた標準出力の代わり。書くと BrokenPipeError を上げる。"""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        # 表は UTF-8 のバイト列で、buffer に書く
        self.buffer = self

    def write(self, text: bytes) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return self.fd


def test_write_stops_quietly_when_the_reader_goes_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """誤りを上げず、標準出力を /dev/null に付け替える。"""
    target = tmp_path / "out"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT)
    try:
        monkeypatch.setattr(sys, "stdout", ClosedPipe(fd))
        table.write(("ID",), [{"ID": "a"}])
        _ = os.write(fd, b"after")
    finally:
        os.close(fd)
    assert target.read_bytes() == b""


def test_write_replaces_what_cannot_be_shown(capsys: pytest.CaptureFixture[str]) -> None:
    """改行やタブを含む値でも、1 件 1 行を崩さない。"""
    table.write(("ID", "NAME"), [{"ID": "a\nb", "NAME": "c\td"}])
    assert capsys.readouterr().out.splitlines() == ["ID    NAME", "a?b   c?d"]


def test_out_writes_utf8_after_what_was_written_before() -> None:
    """ASCII しか書けないロケールでも UTF-8 で書き、先に書いた文字列の後ろに置く。"""
    script = (
        "import sys\n"
        "from temt import table\n"
        "sys.stdout.write('before ')\n"
        "table.out('\\u3042\\n')\n"
        "sys.stdout.write('after')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
        check=True,
    )
    assert done.stdout == "before \u3042\nafter".encode()


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        pytest.param("one line", "one line", id="one"),
        pytest.param("first\n\n  \nsecond", "first; second", id="blank"),
        pytest.param(
            "Parameter validation failed:\nInvalid length",
            "Parameter validation failed: Invalid length",
            id="colon",
        ),
        pytest.param("a: \n  b  \nc", "a: b; c", id="spaces"),
        pytest.param("bad \x1b[2J\tvalue", "bad ?[2J?value", id="control"),
    ],
)
def test_aws_error_makes_one_line(text: str, shown: str) -> None:
    """行の頭と末尾の空白を落とし、空の行を飛ばし、: で終わる行の後ろは空白で、ほかは
    "; " でつなぐ。表示できない文字は ? にする。
    """
    assert table.aws_error(Exception(text)) == shown
