"""temt と一緒に配るアプリ idle-shutdown のテスト。

本体はインスタンスの上で動くコマンドなので、ファイルから読み込み、/proc や /dev/pts、
/run/idle-shutdown などの場所を一時ディレクトリに差し替えて確かめる。busctl、
systemctl、root かどうかも差し替える。
"""

import http.client
import importlib.util
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from temt import deploy, names
from temt.manifest import read_manifest

APP = names.BUNDLED_APPS / "idle-shutdown"
NOW = 1_800_000_000.0
UPTIME = 7200.0
SELF = 500


def load() -> ModuleType:
    """本体を読み込む。アプリのディレクトリは丸ごと送るので、__pycache__ を作らない。"""
    spec = importlib.util.spec_from_file_location("idle_shutdown", APP / "idle_shutdown.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


@pytest.fixture
def idle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """場所を一時ディレクトリにした本体。root として動き、ロックは無い。

    判定の結果の SSM パラメータへの書き込みは reported に記録する。
    """
    module: Any = load()
    run_dir = tmp_path / "run"
    proc = tmp_path / "proc"
    pts = tmp_path / "pts"
    for where in (run_dir / "touch", run_dir / "root", proc, pts):
        where.mkdir(parents=True)
    (proc / "uptime").write_text(f"{UPTIME} 1234.5\n", encoding="ascii")
    monkeypatch.setattr(module, "RUN_DIR", run_dir)
    monkeypatch.setattr(module, "TOUCH_DIR", run_dir / "touch")
    monkeypatch.setattr(module, "ROOT_TOUCH_DIR", run_dir / "root")
    monkeypatch.setattr(module, "IN_USE_OWNER", os.getuid())
    # 自分は /proc に無いプロセスで、端末を持たない。ssh の下で動く形は、テストが置く
    monkeypatch.setattr(module, "_my_pid", lambda: SELF)
    monkeypatch.setattr(module, "_my_terminals", set)
    monkeypatch.setattr(module, "LIMIT_FILE", run_dir / "limit")
    monkeypatch.setattr(module, "DEPLOY_LIMIT_FILE", tmp_path / "etc" / "limit")
    monkeypatch.setattr(module, "IN_USE_DIR", tmp_path / "in-use.d")
    monkeypatch.setattr(module, "PTS_DIR", pts)
    monkeypatch.setattr(module, "PROC_DIR", proc)
    monkeypatch.setattr(module, "_is_root", lambda: True)
    monkeypatch.setattr(module, "_busctl", lambda: json.dumps({"type": "a(ssssuu)", "data": [[]]}))
    monkeypatch.setattr(time, "time", lambda: NOW)
    # 電源断と、SSM パラメータへの書き込み（書いた値）を、それぞれと、起きた順にも記録する
    module.powered_off = []
    module.reported = []
    module.events = []

    def poweroff() -> None:
        module.powered_off.append(True)
        module.events.append("poweroff")

    def report(text: str) -> None:
        module.reported.append(text)
        module.events.append(("report", text))

    monkeypatch.setattr(module, "_poweroff", poweroff)
    monkeypatch.setattr(module, "report", report)
    return module


def local(when: float) -> str:
    """状態の行に書く、インスタンスのローカル時刻。テストでは日本時間（JST）になる。"""
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(when))


def process(
    idle: Any, pid: int, title: str, *, parent: int = 1, pad: int = 0, pts: int | None = None
) -> None:
    """/proc にプロセスを 1 つ置く。title は setproctitle で書き換えた後の名前。

    setproctitle は元の引数の領域を上書きするので、pad 個の NUL が後ろに残ることがある。
    pts は制御端末の /dev/pts の番号。stat の tty_nr には major 136 で入る。
    """
    tty_nr = 0 if pts is None else (136 << 8) | pts
    entry = idle.PROC_DIR / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(title.encode("ascii") + b"\0" * (1 + pad))
    (entry / "stat").write_text(f"{pid} (x) y) S {parent} {pid} 0 {tty_nr}\n", encoding="ascii")


def touched(idle: Any, name: str, when: float, *, root: bool = False) -> None:
    path = (idle.ROOT_TOUCH_DIR if root else idle.TOUCH_DIR) / name
    path.touch()
    os.utime(path, (when, when))


def over_ssh(idle: Any, monkeypatch: pytest.MonkeyPatch, *, tty: bool) -> None:
    """自分を、ssh でログインしたセッションの下で動くプロセスにする。"""
    process(idle, 300, "sshd: ubuntu [priv]")
    process(idle, 400, f"sshd: ubuntu@{'pts/0' if tty else 'notty'}", parent=300)
    process(idle, 450, "-bash", parent=400, pts=0 if tty else None)
    process(
        idle,
        SELF,
        "/usr/bin/python3 -I /usr/local/bin/idle-shutdown status",
        parent=450,
        pts=0 if tty else None,
    )
    if tty:
        (idle.PTS_DIR / "0").touch()
        monkeypatch.setattr(idle, "_my_terminals", lambda: {"0"})


def in_use_script(idle: Any, name: str, body: str, *, mode: int = 0o755) -> None:
    idle.IN_USE_DIR.mkdir(exist_ok=True)
    path = idle.IN_USE_DIR / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="ascii")
    path.chmod(mode)


# 期間


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("3600", 3600), ("45s", 45), ("90m", 5400), ("4h", 14400), ("1d", 86400)],
)
def test_parse_duration(idle: Any, text: str, seconds: int) -> None:
    assert idle.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["0", "0h", "", "h", "1w", "1.5h", "-1", "1h30m", " 1h"])
def test_parse_duration_refuses_bad_ones(idle: Any, text: str) -> None:
    with pytest.raises(idle.Fail, match="bad duration"):
        idle.parse_duration(text)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (45, "45s"), (3600, "1h"), (5400, "1h30m"), (93784, "1d2h3m4s")],
)
def test_fmt_duration(idle: Any, seconds: int, text: str) -> None:
    assert idle.fmt_duration(seconds) == text


# 使用中の判定


def test_ssh_sessions_counts_logged_in_sessions_only(idle: Any) -> None:
    process(idle, 10, "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups")
    process(idle, 11, "sshd: ubuntu [priv]")
    process(idle, 12, "sshd: ubuntu@pts/0")
    process(idle, 13, "sshd: ubuntu@notty", pad=40)
    process(idle, 14, "sshd-session: ubuntu@pts/1")
    process(idle, 15, "sshd: [accepted]")
    process(idle, 16, "sshd: root [net]")
    process(idle, 17, "bash")
    # ssh -N のトンネルは、セッションを開かないので @ が付かない
    process(idle, 18, "sshd: ubuntu")
    process(idle, 19, "sshd-session: ubuntu")
    process(idle, 20, "sshd-auth: ubuntu [net]")
    (idle.PROC_DIR / "self").mkdir()
    assert idle.ssh_sessions() == {12, 13, 14, 18, 19}


def test_terminals_counts_the_numbered_entries(idle: Any) -> None:
    for name in ("ptmx", "0", "3", "4"):
        (idle.PTS_DIR / name).touch()
    assert idle.terminals(set()) == 3
    assert idle.terminals({"3"}) == 2


def test_inhibitors_counts_block_locks_on_shutdown_whoever_holds_them(
    idle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = [
        ["shutdown", "Unattended Upgrades Shutdown", "Stop ongoing upgrades", "delay", 0, 700],
        ["shutdown:sleep", "root", "nightly build", "block", 0, 800],
        ["sleep", "ubuntu", "no sleep", "block", 1000, 900],
        ["idle:shutdown", "ubuntu", "long job\x1b", "block", 1000, 901],
    ]
    monkeypatch.setattr(idle, "_busctl", lambda: json.dumps({"data": [locks]}))
    assert idle.inhibitors() == [
        "shutdown blocked by root (uid 0, pid 800): nightly build",
        "shutdown blocked by ubuntu (uid 1000, pid 901): long job?",
    ]


def test_inhibitors_counts_as_in_use_when_busctl_fails(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> str:
        raise subprocess.CalledProcessError(1, "busctl")

    monkeypatch.setattr(idle, "_busctl", fail)
    assert idle.inhibitors() == ["the inhibitor locks are unknown"]
    assert "cannot list the inhibitor locks" in capsys.readouterr().err


def test_inhibitors_warns_on_unexpected_output(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(idle, "_busctl", lambda: '{"data": []}')
    assert idle.inhibitors() == ["the inhibitor locks are unknown"]
    assert "cannot list the inhibitor locks" in capsys.readouterr().err


def test_in_use_d_runs_the_scripts_in_name_order(idle: Any) -> None:
    in_use_script(idle, "b-queue", "echo '3 jobs queued'")
    in_use_script(idle, "a_web", "printf '2 users\\n\\nlast \\033 x\\n'")
    in_use_script(idle, "c", ": nothing to say")
    assert idle.in_use_d() == ["a_web: 2 users", "a_web: last ? x", "b-queue: 3 jobs queued"]


def test_in_use_d_skips_backups_and_files_that_cannot_run(idle: Any) -> None:
    in_use_script(idle, "web~", "echo backup")
    in_use_script(idle, "web.dpkg-old", "echo backup")
    in_use_script(idle, ".hidden", "echo hidden")
    in_use_script(idle, "plain", "echo plain", mode=0o644)
    (idle.IN_USE_DIR / "dir").mkdir()
    assert idle.in_use_d() == []


def test_in_use_d_ignores_failures(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    in_use_script(idle, "broken", "echo half; echo 'no queue' >&2; exit 3")
    in_use_script(idle, "ok", "echo fine")
    assert idle.in_use_d() == ["ok: fine"]
    assert "broken failed (exit 3): no queue; ignoring it" in capsys.readouterr().err


def test_in_use_d_ignores_timeouts(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """締め切りを短くするのは、時間切れになるものだけを置いたここに限る。

    成功するものも同じ締め切りで動かすと、負荷が高いときに時間切れになりうる。
    """
    monkeypatch.setattr(idle, "IN_USE_TIMEOUT", 1)
    in_use_script(idle, "slow", "echo half; exec sleep 5")
    assert idle.in_use_d() == []
    assert "slow took more than 1s; ignoring it" in capsys.readouterr().err


def test_in_use_d_skips_files_others_can_change(
    idle: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    in_use_script(idle, "shared", "echo shared", mode=0o777)
    target = tmp_path / "writable"
    target.write_text("#!/bin/sh\necho linked\n", encoding="ascii")
    target.chmod(0o757)
    (idle.IN_USE_DIR / "linked").symlink_to(target)
    assert idle.in_use_d() == []
    err = capsys.readouterr().err
    assert "shared must belong to root and be writable only by root" in err
    assert "linked must belong to root and be writable only by root" in err


def test_in_use_d_skips_files_of_other_users(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    in_use_script(idle, "other", "echo other")
    monkeypatch.setattr(idle, "IN_USE_OWNER", os.getuid() + 1)
    assert idle.in_use_d() == []
    assert "other must belong to root" in capsys.readouterr().err


def test_in_use_d_is_optional(idle: Any) -> None:
    assert idle.in_use_d() == []


def test_in_use_d_that_is_not_a_directory(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    idle.IN_USE_DIR.write_text("", encoding="ascii")
    assert idle.in_use_d() == []
    assert "cannot read" in capsys.readouterr().err


def test_reasons_lists_everything_that_keeps_it_up(
    idle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    process(idle, 12, "sshd: ubuntu@pts/0")
    (idle.PTS_DIR / "0").touch()
    locks = [["shutdown", "ubuntu", "job", "block", 1000, 5]]
    monkeypatch.setattr(idle, "_busctl", lambda: json.dumps({"data": [locks]}))
    in_use_script(idle, "web", "echo busy")
    assert idle.reasons(skip_self=False) == [
        "1 ssh session(s)",
        "1 terminal(s)",
        "shutdown blocked by ubuntu (uid 1000, pid 5): job",
        "web: busy",
    ]


def test_reasons_is_empty_when_nobody_uses_it(idle: Any) -> None:
    assert idle.reasons(skip_self=False) == []


def test_reasons_can_skip_its_own_session(idle: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    over_ssh(idle, monkeypatch, tty=True)
    process(idle, 600, "sshd: ubuntu")
    assert idle.reasons(skip_self=False) == ["2 ssh session(s)", "1 terminal(s)"]
    assert idle.reasons(skip_self=True) == ["1 ssh session(s)"]


def test_reasons_skips_the_login_terminal_under_sudo(
    idle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sudo は新しい端末（pts/1）を作ってコマンドを動かす。ログインの端末 pts/0 は先祖の
    制御端末から分かる。"""
    process(idle, 400, "sshd: ubuntu@pts/0")
    process(idle, 450, "-bash", parent=400, pts=0)
    process(idle, 470, "sudo idle-shutdown status", parent=450, pts=0)
    process(
        idle, SELF, "/usr/bin/python3 -I /usr/local/bin/idle-shutdown status", parent=470, pts=1
    )
    for name in ("0", "1"):
        (idle.PTS_DIR / name).touch()
    monkeypatch.setattr(idle, "_my_terminals", lambda: {"1"})
    assert idle.reasons(skip_self=True) == []
    (idle.PTS_DIR / "2").touch()
    assert idle.reasons(skip_self=True) == ["1 terminal(s)"]


def test_pts_number(idle: Any) -> None:
    assert idle._pts_number((136 << 8) | 5) == "5"
    # minor が 255 を超えると、上の桁は 20 ビット目から入る
    assert idle._pts_number((136 << 8) | (1 << 20) | 4) == "260"
    assert idle._pts_number((4 << 8) | 1) is None
    assert idle._pts_number(0) is None


def test_reasons_leaves_in_use_d_to_root(idle: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    in_use_script(idle, "web", "echo busy")
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    assert idle.reasons(skip_self=False) == []


# 最終利用時刻と limit


def test_last_active_is_the_boot_time_when_nobody_touched(idle: Any) -> None:
    assert idle.last_active() == (NOW - UPTIME, "boot")


def test_last_active_takes_the_newest_touch(idle: Any) -> None:
    touched(idle, "pam", NOW - 600, root=True)
    touched(idle, "myapp", NOW - 60)
    touched(idle, "old", NOW - 99999)
    assert idle.last_active() == (NOW - 60, "myapp")
    touched(idle, "in-use", NOW - 30, root=True)
    assert idle.last_active() == (NOW - 30, "in-use")


def test_last_active_treats_a_slightly_future_time_as_now(idle: Any) -> None:
    touched(idle, "myapp", NOW + 60)
    assert idle.last_active() == (NOW, "myapp")


def test_last_active_ignores_a_far_future_time(
    idle: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    touched(idle, "broken", NOW + 61)
    assert idle.last_active() == (NOW - UPTIME, "boot")
    assert "broken has a time in the future; ignoring it" in capsys.readouterr().err


def test_last_active_keeps_a_future_time_written_by_root(idle: Any) -> None:
    touched(idle, "pam", NOW + 3600, root=True)
    assert idle.last_active() == (NOW, "pam")


def test_last_active_without_the_directories(idle: Any) -> None:
    idle.TOUCH_DIR.rmdir()
    idle.ROOT_TOUCH_DIR.rmdir()
    assert idle.last_active() == (NOW - UPTIME, "boot")


def test_read_limit(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert idle.read_limit() == (3600, "default")
    idle.LIMIT_FILE.write_text("7200\n", encoding="ascii")
    assert idle.read_limit() == (7200, "set until reboot")
    idle.LIMIT_FILE.write_text("off\n", encoding="ascii")
    assert idle.read_limit() == (None, "set until reboot")
    idle.LIMIT_FILE.write_text("0\n", encoding="ascii")
    assert idle.read_limit() == (3600, "default")
    idle.LIMIT_FILE.write_text(f"{366 * 86400}\n", encoding="ascii")
    assert idle.read_limit() == (3600, "default")
    assert "bad limit" in capsys.readouterr().err


def test_read_limit_falls_back_to_the_limit_given_at_deploy(
    idle: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """再起動するまでの制限時間が先に効き、無いか壊れていれば deploy で決めたものを使う。"""
    idle.DEPLOY_LIMIT_FILE.parent.mkdir()
    idle.DEPLOY_LIMIT_FILE.write_text("14400\n", encoding="ascii")
    assert idle.read_limit() == (14400, "set at deploy")
    idle.LIMIT_FILE.write_text("7200\n", encoding="ascii")
    assert idle.read_limit() == (7200, "set until reboot")
    idle.LIMIT_FILE.write_text("bad\n", encoding="ascii")
    assert idle.read_limit() == (14400, "set at deploy")
    assert f"bad limit in {idle.LIMIT_FILE}: bad; ignoring it" in capsys.readouterr().err
    idle.DEPLOY_LIMIT_FILE.write_text("off\n", encoding="ascii")
    assert idle.read_limit() == (None, "set at deploy")
    idle.DEPLOY_LIMIT_FILE.write_text("x\n", encoding="ascii")
    assert idle.read_limit() == (3600, "default")


# コマンド


def test_check_touches_while_in_use(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """判定の結果は journal（標準出力）と SSM パラメータに同じ行を書く。"""
    (idle.PTS_DIR / "0").touch()
    assert idle.main([]) == 0
    assert capsys.readouterr().out == "in use: 1 terminal(s) (limit 1h)\n"
    assert idle.reported == ["in use: 1 terminal(s) (limit 1h)"]
    assert (idle.ROOT_TOUCH_DIR / "in-use").exists()
    assert idle.powered_off == []


def test_check_fails_when_it_cannot_touch(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    idle.ROOT_TOUCH_DIR.rmdir()
    (idle.PTS_DIR / "0").touch()
    assert idle.main([]) == 1
    assert "is idle-shutdown installed?" in capsys.readouterr().err
    assert idle.powered_off == []
    assert idle.reported == []


def test_check_counts_its_own_session(idle: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    over_ssh(idle, monkeypatch, tty=False)
    assert idle.main([]) == 0
    assert idle.powered_off == []


def test_check_waits_until_the_limit(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """電源を切る時刻はインスタンスのローカル時刻で、タイムゾーンの名前を添えて書く。"""
    touched(idle, "pam", NOW - 3599, root=True)
    assert idle.main([]) == 0
    line = "power off in 1s (at 2027-01-15 17:00:01 JST; idle 59m59s since pam, limit 1h)"
    assert capsys.readouterr().out == line + "\n"
    assert idle.reported == [line]
    assert idle.powered_off == []


def test_check_powers_off_after_the_limit(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """電源を切る直前に書くので、終了した後にも理由が残る。"""
    touched(idle, "pam", NOW - 3600, root=True)
    assert idle.main([]) == 0
    assert capsys.readouterr().out == "powering off (idle 1h since pam, limit 1h)\n"
    assert idle.events == [("report", "powering off (idle 1h since pam, limit 1h)"), "poweroff"]


def test_check_powers_off_even_when_reporting_breaks(
    idle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """報告が思わぬ誤りで落ちても、電源断は止めない。"""

    def broken(text: str) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(idle, "report", broken)
    touched(idle, "pam", NOW - 3600, root=True)
    with pytest.raises(RuntimeError):
        idle.run([])
    assert idle.powered_off == [True]


def test_check_counts_from_boot(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert idle.main([]) == 0
    assert capsys.readouterr().out == "powering off (idle 2h since boot, limit 1h)\n"
    assert idle.powered_off == [True]


def test_check_never_powers_off_when_the_limit_is_off(
    idle: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    idle.LIMIT_FILE.write_text("off\n", encoding="ascii")
    assert idle.main([]) == 0
    assert capsys.readouterr().out == "never powers off (limit off; idle 2h since boot)\n"
    assert idle.reported == ["never powers off (limit off; idle 2h since boot)"]
    assert idle.powered_off == []


def test_dry_run_touches_nothing_and_never_powers_off(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    assert idle.main(["-n"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "power off at next check (idle 2h since boot, limit 1h)\n"
    assert "not checking in-use.d; run sudo idle-shutdown -n" in captured.err
    (idle.PTS_DIR / "1").touch()
    assert idle.main(["-n"]) == 0
    assert capsys.readouterr().out == "in use: 1 terminal(s) (limit 1h)\n"
    assert list(idle.TOUCH_DIR.iterdir()) == []
    assert list(idle.ROOT_TOUCH_DIR.iterdir()) == []
    assert idle.powered_off == []
    assert idle.reported == []


def test_dry_run_does_not_count_its_own_session(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    over_ssh(idle, monkeypatch, tty=True)
    assert idle.main(["-n"]) == 0
    assert capsys.readouterr().out.startswith("power off at next check")


def test_poweroff_runs_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[tuple[Any, ...]] = []

    def run(*args: Any, **kwargs: Any) -> None:
        ran.append((*args, kwargs))

    # fixture の本体は _poweroff を差し替えてあるので、読み込み直したものを使う
    module: Any = load()
    monkeypatch.setattr(subprocess, "run", run)
    module._poweroff()
    assert ran == [(("systemctl", "poweroff"), {"check": True})]


def test_poweroff_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, "systemctl")

    # fixture の本体は _poweroff を差し替えてあるので、読み込み直したものを使う
    module: Any = load()
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(module.Fail, match=r"systemctl poweroff failed \(exit 1\)"):
        module._poweroff()


def test_check_needs_root(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    assert idle.main([]) == 1
    assert "checking needs root" in capsys.readouterr().err
    assert idle.powered_off == []


def test_status_while_idle(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    touched(idle, "myapp", NOW - 600)
    assert idle.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "limit:        1h (default)"
    assert out[1].endswith(" (10m ago, by myapp)")
    assert out[2] == "in use:       no"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(NOW - 600 + 3600))
    assert out[3] == f"power off:    {stamp} (in 50m)"
    assert idle.reported == []


def test_status_after_the_limit_leaves_it_to_the_next_check(
    idle: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """制限時間を過ぎていれば、次のタイマーが切る。status は切らない。"""
    touched(idle, "myapp", NOW - 4000)
    assert idle.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(NOW - 4000 + 3600))
    assert out[3] == f"power off:    {stamp} (at next check)"
    assert idle.powered_off == []


def test_status_over_ssh_does_not_count_itself(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    over_ssh(idle, monkeypatch, tty=True)
    assert idle.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2] == "in use:       no (not counting this session)"
    assert out[3] == "power off:    1h after you log out"
    process(idle, 600, "sshd: ubuntu@notty")
    assert idle.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2] == "in use:       1 ssh session(s) (not counting this session)"
    assert out[3] == "power off:    not while in use"


def test_status_without_root_leaves_out_in_use_d(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    in_use_script(idle, "web", "echo busy")
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    assert idle.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2] == "in use:       no"
    assert out[3] == "in-use.d:     not checked; run sudo idle-shutdown status to include it"


def test_status_while_in_use(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    process(idle, 12, "sshd: ubuntu@notty")
    idle.LIMIT_FILE.write_text("14400\n", encoding="ascii")
    assert idle.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "limit:        4h (set until reboot)"
    assert out[2] == "in use:       1 ssh session(s)"
    assert out[3] == "power off:    not while in use"


def test_status_with_the_limit_off(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    idle.LIMIT_FILE.write_text("off\n", encoding="ascii")
    assert idle.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "limit:        off (set until reboot)"
    assert out[3] == "power off:    never"


def at_login(idle: Any, *, sshd: str = "sshd") -> None:
    """自分を、pam_motd がログインのメッセージを作るために実行したプロセスにする。

    ログインしようとしている接続は、sshd の特権側のプロセスだけで、端末もまだ無い。
    sshd は、特権側のプロセスの名前の頭。OpenSSH 9.8 からは sshd-session になる。
    """
    process(idle, 300, f"{sshd}: ubuntu [priv]")
    process(idle, 310, "sh -c -- /usr/bin/env -i PATH=... run-parts ...", parent=300)
    process(idle, 320, "run-parts --lsbsysinit /etc/update-motd.d", parent=310)
    process(idle, 330, "/bin/sh /etc/update-motd.d/60-idle-shutdown", parent=320)
    process(
        idle,
        SELF,
        "/usr/bin/python3 -I /usr/local/bin/idle-shutdown status --login",
        parent=330,
    )


@pytest.mark.parametrize("sshd", ["sshd", "sshd-session"])
def test_status_at_login_expects_this_session(
    idle: Any, sshd: str, capsys: pytest.CaptureFixture[str]
) -> None:
    at_login(idle, sshd=sshd)
    touched(idle, "pam", NOW, root=True)
    assert idle.main(["status", "--login"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "limit:        1h (default)",
        "in use:       no (not counting this session)",
        "power off:    1h after you log out",
    ]


def test_status_at_login_counts_everyone_else(
    idle: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    at_login(idle)
    process(idle, 600, "sshd: ubuntu@pts/0")
    (idle.PTS_DIR / "0").touch()
    assert idle.main(["status", "--login"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "in use:       1 ssh session(s); 1 terminal(s) (not counting this session)"
    assert out[2] == "power off:    not while in use"


def test_status_at_login_by_hand_does_not_count_itself(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    over_ssh(idle, monkeypatch, tty=True)
    assert idle.main(["status", "--login"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "limit:        1h (default)",
        "in use:       no (not counting this session)",
        "power off:    1h after you log out",
    ]


def test_status_at_login_when_busctl_fails(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> str:
        raise subprocess.CalledProcessError(1, "busctl")

    at_login(idle)
    monkeypatch.setattr(idle, "_busctl", fail)
    assert idle.main(["status", "--login"]) == 0
    captured = capsys.readouterr()
    assert "cannot list the inhibitor locks" in captured.err
    out = captured.out.splitlines()
    assert out[1] == "in use:       the inhibitor locks are unknown (not counting this session)"
    assert out[2] == "power off:    not while in use"


def test_status_at_login_does_not_read_the_last_activity(
    idle: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """最終利用時刻を出さないので、touch/ の未来の時刻の警告も出さない。"""
    at_login(idle)
    touched(idle, "broken", NOW + 61)
    assert idle.main(["status", "--login"]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("root", [True, False])
def test_status_at_login_leaves_out_in_use_d(
    idle: Any, root: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    at_login(idle)
    in_use_script(idle, "web", "echo busy")
    monkeypatch.setattr(idle, "_is_root", lambda: root)
    assert idle.main(["status", "--login"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "in use:       no (not counting this session)"
    assert len(out) == 3


def test_status_at_login_with_the_limit_off(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    at_login(idle)
    idle.LIMIT_FILE.write_text("off\n", encoding="ascii")
    assert idle.main(["status", "--login"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "limit:        off (set until reboot)"
    assert out[2] == "power off:    never"


def test_limit_sets_shows_and_restores(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """変えるたびに、変えたあとの判定の結果を SSM パラメータに書く。見るだけなら書かない。

    判定は電源を切らず、制限時間を過ぎていれば次のタイマーに任せる。
    """
    assert idle.main(["limit", "4h"]) == 0
    assert idle.LIMIT_FILE.read_text(encoding="ascii") == "14400\n"
    assert idle.main(["limit"]) == 0
    assert idle.main(["limit", "off"]) == 0
    assert idle.LIMIT_FILE.read_text(encoding="ascii") == "off\n"
    assert idle.main(["limit", "default"]) == 0
    assert not idle.LIMIT_FILE.exists()
    assert idle.main(["limit", "default"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "limit: 4h (set until reboot)",
        "limit: 4h (set until reboot)",
        "limit: off (set until reboot)",
        "limit: 1h (default)",
        "limit: 1h (default)",
    ]
    assert idle.reported == [
        f"power off in 2h (at {local(NOW - UPTIME + 14400)}; idle 2h since boot, limit 4h)",
        "never powers off (limit off; idle 2h since boot)",
        "power off at next check (idle 2h since boot, limit 1h)",
        "power off at next check (idle 2h since boot, limit 1h)",
    ]
    assert idle.powered_off == []


def test_limit_at_deploy_is_what_default_restores(
    idle: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """install が deploy の値で limit --deploy を実行する。default は deploy の値に戻す。

    limit --deploy は書くだけで、判定の結果は書かない。install が最後に report で書く。
    """
    assert idle.main(["limit", "--deploy", "2h"]) == 0
    assert idle.DEPLOY_LIMIT_FILE.read_text(encoding="ascii") == "7200\n"
    assert idle.reported == []
    assert idle.main(["limit", "4h"]) == 0
    assert idle.main(["limit", "default"]) == 0
    assert idle.main(["limit", "--deploy", "off"]) == 0
    assert idle.DEPLOY_LIMIT_FILE.read_text(encoding="ascii") == "off\n"
    assert capsys.readouterr().out.splitlines() == [
        "limit: 2h (set at deploy)",
        "limit: 4h (set until reboot)",
        "limit: 2h (set at deploy)",
        "limit: off (set at deploy)",
    ]
    assert idle.reported == [
        f"power off in 2h (at {local(NOW - UPTIME + 14400)}; idle 2h since boot, limit 4h)",
        "power off at next check (idle 2h since boot, limit 2h)",
    ]
    assert idle.main(["report"]) == 0
    assert idle.reported[-1] == "never powers off (limit off; idle 2h since boot)"
    assert idle.powered_off == []


def test_limit_at_deploy_makes_the_files_with_their_modes(idle: Any) -> None:
    """umask に任せず、ディレクトリは 755、ファイルは 644 で作る。"""
    saved = os.umask(0o077)
    try:
        assert idle.main(["limit", "--deploy", "2h"]) == 0
    finally:
        os.umask(saved)
    assert idle.DEPLOY_LIMIT_FILE.parent.stat().st_mode & 0o777 == 0o755
    assert idle.DEPLOY_LIMIT_FILE.stat().st_mode & 0o777 == 0o644


def test_report_needs_root(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    assert idle.main(["report"]) == 1
    assert "reporting needs root: sudo idle-shutdown report" in capsys.readouterr().err
    assert idle.reported == []


def test_limit_at_deploy_tells_that_the_limit_until_reboot_still_applies(
    idle: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """deploy は再起動するまでの制限時間を消さないので、与えた値が効かない理由を出す。"""
    idle.LIMIT_FILE.write_text("28800\n", encoding="ascii")
    assert idle.main(["limit", "--deploy", "2h"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "limit: 8h (set until reboot)",
        "the limit set until reboot still applies;",
        "run sudo idle-shutdown limit default to use the limit given at deploy",
    ]


def test_limit_at_deploy_refuses_a_bad_duration(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert idle.main(["limit", "--deploy", "366d"]) == 1
    assert "too long: 366d" in capsys.readouterr().err
    assert idle.main(["limit", "--deploy", "default"]) == 1
    assert "bad duration: default" in capsys.readouterr().err
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    assert idle.main(["limit", "--deploy", "4h"]) == 1
    assert "sudo idle-shutdown limit --deploy 4h" in capsys.readouterr().err
    assert not idle.DEPLOY_LIMIT_FILE.exists()
    assert idle.reported == []


def test_limit_counts_its_own_session(idle: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh から実行すれば、その接続は使用中に数える。"""
    over_ssh(idle, monkeypatch, tty=True)
    assert idle.main(["limit", "4h"]) == 0
    assert idle.reported == ["in use: 1 ssh session(s); 1 terminal(s) (limit 4h)"]


def test_limit_refuses_a_bad_duration(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert idle.main(["limit", "4w"]) == 1
    assert "bad duration: 4w" in capsys.readouterr().err
    assert idle.main(["limit", "366d"]) == 1
    assert "too long: 366d (at most 365d" in capsys.readouterr().err
    assert idle.main(["limit", "99999999999999d"]) == 1
    assert not idle.LIMIT_FILE.exists()
    assert idle.main(["limit", "365d"]) == 0


def test_setting_the_limit_needs_root(
    idle: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    assert idle.main(["limit", "4h"]) == 1
    assert "sudo idle-shutdown limit 4h" in capsys.readouterr().err
    assert not idle.LIMIT_FILE.exists()
    assert idle.reported == []


def test_touch_marks_the_instance_as_in_use(idle: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    # touch は本物の時計で mtime を書くので、差し替えた NOW ではなく古い時刻と比べる
    touched(idle, "myapp", 1000.0)
    assert idle.main(["touch", "myapp"]) == 0
    assert (idle.TOUCH_DIR / "myapp").stat().st_mtime > 1000.0
    assert idle.main(["touch", "new"]) == 0
    assert (idle.TOUCH_DIR / "new").exists()


def test_root_touches_its_own_directory(idle: Any) -> None:
    assert idle.main(["touch", "pam"]) == 0
    assert (idle.ROOT_TOUCH_DIR / "pam").exists()
    assert list(idle.TOUCH_DIR.iterdir()) == []


def test_touch_does_not_follow_links_or_open_fifos(
    idle: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(idle, "_is_root", lambda: False)
    (idle.TOUCH_DIR / "link").symlink_to(tmp_path / "elsewhere")
    os.mkfifo(idle.TOUCH_DIR / "fifo")
    assert idle.main(["touch", "link"]) == 1
    assert idle.main(["touch", "fifo"]) == 1
    assert capsys.readouterr().err.count("cannot touch") == 2
    assert not (tmp_path / "elsewhere").exists()


def test_touch_defaults_to_the_user_name(idle: Any) -> None:
    assert idle.main(["touch"]) == 0
    assert (idle.ROOT_TOUCH_DIR / pwd.getpwuid(os.getuid()).pw_name).exists()


@pytest.mark.parametrize("name", [".hidden", "-x", "a/b", "..", "", "x" * 65, "sp ace"])
def test_touch_refuses_bad_names(idle: Any, name: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert idle.main(["touch", name]) == 1
    assert "bad name" in capsys.readouterr().err
    assert list(idle.ROOT_TOUCH_DIR.iterdir()) == []


def test_touch_without_the_directory(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    idle.ROOT_TOUCH_DIR.rmdir()
    assert idle.main(["touch", "myapp"]) == 1
    assert "is idle-shutdown installed?" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["bogus"],
        ["status", "x"],
        ["status", "--login", "x"],
        ["limit", "1h", "2h"],
        ["touch", "a", "b"],
        ["-x"],
    ],
)
def test_bad_arguments_show_the_usage(
    idle: Any, args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert idle.main(args) == 2
    assert capsys.readouterr().err.startswith("Usage: idle-shutdown")


def test_help(idle: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert idle.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Usage: idle-shutdown")
    # ログインのメッセージ専用なので出さない
    assert "--login" not in out


# 状態を知らせる


@pytest.fixture
def reporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """report を差し替えていない本体。aws-cli とメタデータは差し替える。"""
    module: Any = load()
    monkeypatch.setattr(module, "_aws_cli", lambda: "/snap/bin/aws")
    monkeypatch.setattr(module, "_instance", lambda: ("temt-web", "ap-northeast-1"))
    module.calls = []

    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        module.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    return module


def test_report_writes_the_status_with_aws_cli(
    reporter: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    reporter.report("in use: 1 terminal(s) (limit 1h)")
    ((argv, kwargs),) = reporter.calls
    assert argv == (
        "/snap/bin/aws",
        "ssm",
        "put-parameter",
        "--region",
        "ap-northeast-1",
        "--name",
        "/temt-web/idle-shutdown/status",
        "--type",
        "String",
        "--overwrite",
        "--value",
        "in use: 1 terminal(s) (limit 1h)",
    )
    assert kwargs["timeout"] == reporter.PUT_TIMEOUT
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["env"]["AWS_PAGER"] == ""
    assert capsys.readouterr().err == ""


def test_report_cuts_a_long_status(reporter: Any) -> None:
    reporter.report("x" * 300)
    ((argv, _),) = reporter.calls
    assert len(argv[-1]) == reporter.STATUS_MAX
    assert argv[-1] == "x" * (reporter.STATUS_MAX - 3) + "..."


def test_report_warns_without_aws_cli(
    reporter: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(reporter, "_aws_cli", lambda: None)
    reporter.report("x")
    assert reporter.calls == []
    assert capsys.readouterr().err == (
        "idle-shutdown: cannot report the status: no aws in /snap/bin, /usr/local/bin, /usr/bin\n"
    )


def test_report_warns_when_the_metadata_cannot_be_read(
    reporter: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> tuple[str, str]:
        raise OSError("no route")

    monkeypatch.setattr(reporter, "_instance", fail)
    reporter.report("x")
    assert reporter.calls == []
    assert "cannot report the status: cannot read the instance metadata: no route" in (
        capsys.readouterr().err
    )


def test_report_warns_when_the_metadata_is_broken(
    reporter: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """壊れた応答は http.client の例外になる。OSError ではないが、同じく警告にする。"""

    def fail() -> tuple[str, str]:
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(reporter, "_instance", fail)
    reporter.report("x")
    assert reporter.calls == []
    assert "cannot report the status: cannot read the instance metadata: garbage" in (
        capsys.readouterr().err
    )


def test_report_warns_when_aws_cli_cannot_run(
    reporter: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise PermissionError("not executable")

    monkeypatch.setattr(subprocess, "run", run)
    reporter.report("x")
    assert (
        "cannot report the status: cannot run /snap/bin/aws: not executable"
        in capsys.readouterr().err
    )


def test_report_warns_when_aws_cli_fails(
    reporter: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 254, "", "\nAn error occurred (AccessDenied)\n")

    monkeypatch.setattr(subprocess, "run", run)
    reporter.report("x")
    assert capsys.readouterr().err == (
        "idle-shutdown: cannot report the status: aws failed (exit 254): "
        "An error occurred (AccessDenied)\n"
    )


def test_report_warns_when_aws_cli_takes_too_long(
    reporter: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    reporter.report("x")
    assert "cannot report the status: aws took more than 15s" in capsys.readouterr().err


def test_aws_cli_is_looked_up_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH に頼らず、決まった場所を順に探す。"""
    module: Any = load()
    first, second = tmp_path / "snap", tmp_path / "local"
    monkeypatch.setattr(module, "AWS_CLI_DIRS", (str(first), str(second)))
    assert module._aws_cli() is None
    second.mkdir()
    (second / "aws").write_text("#!/bin/sh\n", encoding="ascii")
    assert module._aws_cli() is None
    (second / "aws").chmod(0o755)
    assert module._aws_cli() == str(second / "aws")
    first.mkdir()
    (first / "aws").write_text("#!/bin/sh\n", encoding="ascii")
    (first / "aws").chmod(0o755)
    assert module._aws_cli() == str(first / "aws")


def test_instance_reads_the_name_and_the_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """IMDSv2 のトークンを取ってから、タグ temt:service とリージョンを読む。"""
    module: Any = load()
    answers = {
        "http://169.254.169.254/latest/meta-data/tags/instance/temt:service": "web\n",
        "http://169.254.169.254/latest/meta-data/placement/region": "ap-northeast-1",
    }

    def fetch(request: Any) -> str:
        if request.get_method() == "PUT":
            assert request.full_url == "http://169.254.169.254/latest/api/token"
            assert request.get_header("X-aws-ec2-metadata-token-ttl-seconds") == "60"
            return "TOKEN"
        assert request.get_header("X-aws-ec2-metadata-token") == "TOKEN"
        return answers[request.full_url].strip()

    monkeypatch.setattr(module, "_fetch", fetch)
    assert module._instance() == ("temt-web", "ap-northeast-1")
    answers["http://169.254.169.254/latest/meta-data/tags/instance/temt:service"] = ""
    with pytest.raises(ValueError, match="the tag temt:service or the region is empty"):
        module._instance()


def test_service_passes_when_the_tag_is_readable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """install の初めに、タグ temt:service が読めれば何も言わずに通る。"""
    module: Any = load()
    monkeypatch.setattr(module, "_imds_token", lambda: "TOKEN")
    monkeypatch.setattr(module, "_imds", lambda path, token: "web")
    assert module.main(["service"]) == 0
    assert capsys.readouterr().err == ""


def test_service_tells_how_to_enable_the_tags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """タグが読めなければ（IMDS は 404 を返す）、設定ファイルと今のインスタンスの直し方を
    告げて失敗する。"""
    module: Any = load()

    def imds(path: str, token: str) -> str:
        if path == "instance-id":
            return "i-0123456789abcdef0"
        raise urllib.error.HTTPError(path, 404, "Not Found", Message(), None)

    monkeypatch.setattr(module, "_imds_token", lambda: "TOKEN")
    monkeypatch.setattr(module, "_imds", imds)
    assert module.main(["service"]) == 1
    err = capsys.readouterr().err
    assert "cannot read the tag temt:service from the instance metadata" in err
    assert 'MetadataOptions.InstanceMetadataTags = "enabled"' in err
    assert "--instance-id i-0123456789abcdef0 --instance-metadata-tags enabled" in err
    assert "then deploy again" in err


def test_install_checks_the_tag_before_anything() -> None:
    """install は、タグ temt:service が読めることを、制限時間を書くより先に確かめる。"""
    install = (APP / "install").read_text(encoding="utf-8")
    checked = install.index('python3 -I "$here/idle_shutdown.py" service')
    assert checked < install.index("limit --deploy")


def test_install_puts_aws_cli_with_snap() -> None:
    """本体が探す 3 か所に aws が無ければ、snapd の初期化を待ってから snap で入れる。"""
    install = (APP / "install").read_text(encoding="utf-8")
    module: Any = load()
    tests = " && ".join(f"[ ! -x {directory}/aws ]" for directory in module.AWS_CLI_DIRS)
    assert f"if {tests}; then" in install
    assert "snap wait system seed.loaded" in install
    assert "snap install aws-cli --classic" in install


# アプリとしての形


def test_the_limit_option_matches_what_the_command_takes() -> None:
    """deploy のオプション limit の pattern は、limit --deploy が受け付ける形に合わせる。"""
    module: Any = load()
    option = read_manifest(APP).options["limit"]
    assert option.default == "1h"
    for text in ("90m", "4h", "1d", "3600", "3600s", "off"):
        assert option.accepts(text)
        if text != "off":
            assert module.parse_duration(text) > 0
    for text in ("4w", "default", "1h30m", "", " 4h", "0", "0h", "01h"):
        assert not option.accepts(text)
    # manifest.toml の既定と、どちらのファイルも無いときの本体の既定は揃える
    assert module.parse_duration(option.default or "") == module.DEFAULT_LIMIT


def test_install_gives_the_limit_before_placing_anything() -> None:
    """期間が通らなければ、aws-cli や本体などを入れる前に失敗する。

    判定の結果は、タイマーを有効にしたあと、最後に書く。
    """
    install = (APP / "install").read_text(encoding="utf-8")
    given = install.index('. "$here/temt-options.env"')
    limit = install.index('python3 -I "$here/idle_shutdown.py" limit --deploy "$TEMT_OPTION_LIMIT"')
    aws_cli = install.index("snap install aws-cli")
    placed = install.index("install -m 755")
    timer = install.index("systemctl enable --now idle-shutdown.timer")
    reported = install.index("idle-shutdown report")
    assert given < limit < aws_cli < placed < timer < reported


def test_the_app_comes_with_temt(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """conf ディレクトリの apps/ に無くても、deploy がアプリ ID でパッケージから見つける。"""
    _ = home_dir
    monkeypatch.setattr(names, "BUNDLED_APPS", APP.parent)
    app_id, path = deploy.locate("idle-shutdown")
    assert app_id == "idle-shutdown"
    assert path == APP
    assert read_manifest(path).name == "idle-shutdown"


def test_loading_the_command_writes_no_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """アプリのディレクトリは丸ごと送るので、テストが本体を読み込んでも __pycache__ を作らない。

    作業ツリーの __pycache__ の有無に左右されないよう、写したディレクトリで確かめる。
    """
    copied = tmp_path / "idle-shutdown"
    copied.mkdir()
    shutil.copy2(APP / "idle_shutdown.py", copied)
    monkeypatch.setattr(sys.modules[__name__], "APP", copied)
    _ = load()
    assert [path.name for path in copied.iterdir()] == ["idle_shutdown.py"]


@pytest.mark.parametrize("script", ["install", "uninstall"])
def test_the_scripts_are_valid_bash(script: str) -> None:
    subprocess.run(["bash", "-n", str(APP / script)], check=True)


def test_install_places_every_file_of_the_app() -> None:
    install = (APP / "install").read_text(encoding="utf-8")
    shipped = {p.name for p in APP.iterdir()} - {"manifest.toml", "install", "uninstall"}
    for name in shipped:
        assert f'"$here/{name}"' in install


def placed_by_install() -> tuple[list[str], list[str]]:
    """install が install コマンドで置くファイルと、作るディレクトリ。"""
    files: list[str] = []
    directories: list[str] = []
    for line in (APP / "install").read_text(encoding="utf-8").splitlines():
        if not line.startswith("install "):
            continue
        words = shlex.split(line)
        operands: list[str] = []
        is_directory = False
        rest = iter(words[1:])
        for word in rest:
            if word == "-m":
                _ = next(rest)
            elif word == "-d":
                is_directory = True
            elif not word.startswith("-"):
                operands.append(word)
        if is_directory:
            directories.extend(operands)
            continue
        *sources, target = operands
        if target.endswith("/"):
            files.extend(target + Path(source).name for source in sources)
        else:
            assert len(sources) == 1
            files.append(target)
    return files, directories


# uninstall を手元で動かすときに、一時ディレクトリの下へ移す絶対パスの頭
SYSTEM_DIRS = re.compile(r"(?<![\w.}-])/(?=(?:etc|usr|run)/)")
# 移したあとに残ってよい絶対パス
KEPT_PATH = re.compile(r"(?<![\w.}-])/(?!dev/null\b)[A-Za-z]")
# common-session に pam-auth-update が足す行
PAM_LINE = "session optional pam_exec.so quiet /usr/local/bin/idle-shutdown touch pam\n"


def run_uninstall(tmp_path: Path, *, pam_removes: bool) -> tuple[str, int, Path, list[str]]:
    """install が置くものを置いてから、uninstall を手元の bash で動かす。

    /etc、/usr、/run などは tmp_path/root の下に移す。systemctl と pam-auth-update は
    偽物にして、呼ばれた引数を控える。偽の pam-auth-update は、pam_removes なら
    common-session から行を消し、そうでなければ、手で直されたファイルのときのように何も
    書かずに 0 で終わる。標準エラー出力、終了コード、root、控えた呼び出しを返す。
    """
    root = tmp_path / "root"
    files, directories = placed_by_install()
    for where in [*files, str(load().DEPLOY_LIMIT_FILE), "/run/idle-shutdown/touch/ubuntu"]:
        path = root / where.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    for where in directories:
        (root / where.lstrip("/")).mkdir(parents=True, exist_ok=True)
    session = root / "etc" / "pam.d" / "common-session"
    session.parent.mkdir(parents=True)
    session.write_text(PAM_LINE, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    removing = f"sed -i '/idle-shutdown touch pam/d' {shlex.quote(str(session))}\n"
    for name, body in (("systemctl", ""), ("pam-auth-update", removing if pam_removes else "")):
        fake = bin_dir / name
        fake.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n{body}', encoding="utf-8")
        fake.chmod(0o755)
    script = SYSTEM_DIRS.sub(f"{root}/", (APP / "uninstall").read_text(encoding="utf-8"))
    # 移し漏れがあれば、手元のファイルを消しうるので動かさない。1 行目は #!/bin/bash
    assert not KEPT_PATH.search(script.replace(str(root), "ROOT").split("\n", 1)[1])
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
    )
    called = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done.stderr, done.returncode, root, called


def test_uninstall_removes_what_install_places(tmp_path: Path) -> None:
    """install が置くファイルと作るディレクトリ、limit --deploy が置く制限時間、
    /run/idle-shutdown/ を、どれも消す。"""
    files, directories = placed_by_install()
    assert len(files) == 7
    err, code, root, called = run_uninstall(tmp_path, pam_removes=True)
    assert (err, code) == ("", 0)
    for where in [*files, *directories, str(load().DEPLOY_LIMIT_FILE), "/run/idle-shutdown"]:
        assert not (root / where.lstrip("/")).exists(), where
    assert not (root / "etc" / "idle-shutdown").exists()
    assert (root / "etc" / "pam.d" / "common-session").read_text(encoding="utf-8") == ""
    assert called[0] == "pam-auth-update --remove idle-shutdown"


def test_uninstall_stops_when_pam_auth_update_leaves_the_line(tmp_path: Path) -> None:
    """手で直された common-* だと、pam-auth-update は書かずに 0 で終わる。行を残したまま
    本体を消すと、ログインのたびに無い本体を呼ぶので、何も消さずに止める。"""
    err, code, root, called = run_uninstall(tmp_path, pam_removes=False)
    assert code == 1
    # 文言の中のパスも移してあるので、戻して比べる
    assert err.replace(str(root), "") == (
        "uninstall: pam-auth-update did not remove idle-shutdown from "
        "/etc/pam.d/common-session; /etc/pam.d/common-* seem to be edited by hand; "
        "undo the edits and remove again\n"
    )
    assert called == ["pam-auth-update --remove idle-shutdown"]
    files, _ = placed_by_install()
    for where in files:
        assert (root / where.lstrip("/")).exists(), where
    assert (root / "etc" / "pam.d" / "common-session").read_text(encoding="utf-8") == PAM_LINE


def test_the_pam_profile_reads_as_pam_auth_update_reads_it() -> None:
    """pam-auth-update は「項目: 値」の行を読み、タブで始まる行を前の項目の続きとする。"""
    fields: dict[str, str] = {}
    last = ""
    for line in (APP / "pam-config").read_text(encoding="ascii").splitlines():
        if line.startswith("\t"):
            fields[last] += line.strip()
            continue
        key, value = line.split(":", 1)
        last = key
        fields[key] = value.strip()
    assert fields["Session-Type"] == "Additional"
    assert fields["Session-Interactive-Only"] == "yes"
    assert fields["Session"].split() == [
        "optional",
        "pam_exec.so",
        "quiet",
        "/usr/local/bin/idle-shutdown",
        "touch",
        "pam",
    ]


def motd(tmp_path: Path, body: str | None) -> str:
    """ログインのメッセージを作るスクリプトを、本体を差し替えて実行した出力。

    body は本体の代わりに置く sh の中身。None なら本体を置かない。
    """
    fake = tmp_path / "idle-shutdown"
    if body is not None:
        fake.write_text(f"#!/bin/sh\n{body}\n", encoding="ascii")
        fake.chmod(0o755)
    script = tmp_path / "motd"
    text = (APP / "motd").read_text(encoding="utf-8")
    assert text.count("/usr/local/bin/idle-shutdown") == 2
    script.write_text(text.replace("/usr/local/bin/idle-shutdown", str(fake)), encoding="utf-8")
    done = subprocess.run(["sh", str(script)], capture_output=True, text=True, check=True)
    assert done.stderr == ""
    return done.stdout


def test_the_login_message_indents_status_and_its_warnings(tmp_path: Path) -> None:
    body = '[ "$*" = "status --login" ] || exit 9; echo "idle-shutdown: warn" >&2; echo "limit: 1h"'
    assert motd(tmp_path, body) == (
        "\nidle-shutdown (sudo idle-shutdown limit 4h to change):\n"
        "  idle-shutdown: warn\n"
        "  limit: 1h\n"
    )


def test_the_login_message_shows_a_failure(tmp_path: Path) -> None:
    body = 'echo "idle-shutdown: broken" >&2; exit 1'
    assert motd(tmp_path, body) == (
        "\nidle-shutdown (sudo idle-shutdown limit 4h to change):\n  idle-shutdown: broken\n"
    )


def test_the_login_message_is_empty_without_the_command(tmp_path: Path) -> None:
    assert motd(tmp_path, None) == ""
