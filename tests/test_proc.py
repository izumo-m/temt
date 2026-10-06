"""外部コマンドの確認と実行のテスト。"""

import sys

import pytest

from temt import proc
from temt.msg import TemtError


def test_require_finds_a_command() -> None:
    assert proc.require("sh").endswith("/sh")


def test_require_reports_a_missing_command() -> None:
    with pytest.raises(TemtError, match="command not found"):
        proc.require("no-such-command-for-temt")


def test_run_returns_the_output() -> None:
    assert proc.run([sys.executable, "-c", "print('hi')"]) == "hi\n"


def test_run_reports_a_failure() -> None:
    with pytest.raises(TemtError, match="boom"):
        proc.run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"])


def test_run_closes_the_input() -> None:
    # 問いかけを待って止まらないよう、標準入力は閉じて渡す
    read_input = "import sys; print(sys.stdin.read() == '')"
    assert proc.run([sys.executable, "-c", read_input]) == "True\n"


def test_run_reports_the_exit_code_when_nothing_is_written() -> None:
    with pytest.raises(TemtError, match="exit 3"):
        proc.run([sys.executable, "-c", "raise SystemExit(3)"])


def test_run_writes_the_given_input() -> None:
    echo = "import sys; print(sys.stdin.read().upper(), end='')"
    assert proc.run([sys.executable, "-c", echo], stdin="key\n") == "KEY\n"


def test_run_puts_a_failure_on_one_line() -> None:
    """ssh-keygen の @ で囲んだ複数行の警告も、1 行にまとめて知らせる。"""
    noisy = (
        "import sys; sys.stderr.write('@@@@@@\\n@  WARNING: TOO OPEN  @\\n@@@@@@\\n\\n"
        "Load key: bad permissions\\n'); sys.exit(1)"
    )
    with pytest.raises(TemtError) as caught:
        proc.run([sys.executable, "-c", noisy])
    assert str(caught.value) == f"{sys.executable}: WARNING: TOO OPEN Load key: bad permissions"


def test_stream_sends_the_output_to_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    """標準出力は表と app get の値だけに使うので、子の出力は標準エラー出力に回す。"""
    code = proc.stream(["sh", "-c", "cat; echo out; echo err >&2; exit 3"], b"in\n")
    captured = capfd.readouterr()
    assert code == 3
    assert captured.out == ""
    assert captured.err == "in\nout\nerr\n"


def test_stream_closes_stdin_without_data(capfd: pytest.CaptureFixture[str]) -> None:
    assert proc.stream(["sh", "-c", "cat"]) == 0
    assert capfd.readouterr().err == ""


def test_read_returns_the_output_and_shows_the_errors(capfd: pytest.CaptureFixture[str]) -> None:
    """ProxyCommand の待ちの知らせを見せるため、標準エラー出力は受け取らない。"""
    code, out = proc.read(["sh", "-c", "cat; echo i-0abc; echo waiting >&2; exit 2"])
    assert (code, out) == (2, "i-0abc\n")
    assert capfd.readouterr().err == "waiting\n"
