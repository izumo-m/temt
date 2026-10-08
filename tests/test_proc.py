"""外部コマンドの確認と実行のテスト。"""

import os
import sys
from pathlib import Path

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


def test_run_puts_a_failure_on_one_line() -> None:
    """ssh-keygen の @ で囲んだ複数行の警告も、1 行にまとめて知らせる。"""
    noisy = (
        "import sys; sys.stderr.write('@@@@@@\\n@  WARNING: TOO OPEN  @\\n@@@@@@\\n\\n"
        "Load key: bad permissions\\n'); sys.exit(1)"
    )
    with pytest.raises(TemtError) as caught:
        proc.run([sys.executable, "-c", noisy])
    assert str(caught.value) == f"{sys.executable}: WARNING: TOO OPEN Load key: bad permissions"


def test_run_replaces_bytes_that_cannot_be_read() -> None:
    """鍵のパスに読めないバイトがあっても、ssh-keygen の言うことを ? にして知らせる。"""
    bad = "import sys; sys.stderr.buffer.write(b'Load key \"/k/a\\xffb\": bad'); sys.exit(1)"
    with pytest.raises(TemtError) as caught:
        proc.run([sys.executable, "-c", bad])
    assert str(caught.value) == f'{sys.executable}: Load key "/k/a?b": bad'


def test_stream_shows_the_output_as_it_is(capfd: pytest.CaptureFixture[str]) -> None:
    """子の標準出力は標準出力に、標準エラー出力は標準エラー出力に、そのまま見せる。"""
    code = proc.stream(["sh", "-c", "cat; echo out; echo err >&2; exit 3"], b"in\n")
    captured = capfd.readouterr()
    assert code == 3
    assert captured.out == "in\nout\n"
    assert captured.err == "err\n"


def test_stream_runs_the_command_to_the_end_when_the_reader_goes_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """標準出力の読み手（head など）が先に閉じても、install を途中で切らないよう子は止めない。"""
    read_end, write_end = os.pipe()
    os.close(read_end)
    monkeypatch.setattr(sys, "stdout", os.fdopen(write_end, "w", encoding="utf-8"))
    done = tmp_path / "done"
    script = f"for i in $(seq 2000); do echo line $i; done; touch '{done}'"
    assert proc.stream(["sh", "-c", script]) == 0
    assert done.exists()


def test_stream_closes_stdin_without_data(capfd: pytest.CaptureFixture[str]) -> None:
    assert proc.stream(["sh", "-c", "cat"]) == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_read_returns_the_output_and_shows_the_errors(capfd: pytest.CaptureFixture[str]) -> None:
    """ProxyCommand の待ちの知らせを見せるため、標準エラー出力は受け取らない。"""
    code, out = proc.read(["sh", "-c", "cat; echo i-0abc; echo waiting >&2; exit 2"])
    assert (code, out) == (2, "i-0abc\n")
    assert capfd.readouterr().err == "waiting\n"
