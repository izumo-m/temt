"""temt と一緒に配るアプリ timezone-tokyo のテスト。

install と uninstall はインスタンスの上で root で動くので、timedatectl、systemctl、date を
呼ばれた引数を書き留めるだけのものに差し替えて、何を呼ぶかを確かめる。
"""

import os
import subprocess
from pathlib import Path

import pytest

from temt import deploy, names
from temt.manifest import read_manifest

APP = names.BUNDLED_APPS / "timezone-tokyo"


def run(tmp_path: Path, script: str, active: tuple[str, ...]) -> tuple[list[str], str]:
    """script を、差し替えたコマンドで実行する。呼ばれたコマンドの行と標準出力を返す。

    active は、systemctl is-active が動いていると答えるユニット。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    units = "|".join(active)
    fakes = {
        "timedatectl": '[ "$1" = show ] || exit 0; echo Asia/Tokyo',
        "systemctl": f'[ "$1" = is-active ] || exit 0; case "$3" in {units}) ;; *) exit 3;; esac',
        "date": "echo '2026-09-29 16:00:00 JST'",
    }
    for name, body in fakes.items():
        fake = bin_dir / name
        fake.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n{body}\n', encoding="ascii")
        fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    done = subprocess.run(
        ["bash", str(APP / script)], capture_output=True, text=True, check=True, env=env, cwd="/"
    )
    assert done.stderr == ""
    return log.read_text(encoding="ascii").splitlines(), done.stdout


def test_the_app_comes_with_temt(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """conf ディレクトリの apps/ に無くても、deploy がアプリ ID でパッケージから見つける。"""
    _ = home_dir
    monkeypatch.setattr(names, "BUNDLED_APPS", APP.parent)
    app_id, path = deploy.locate("timezone-tokyo")
    assert app_id == "timezone-tokyo"
    assert path == APP
    assert read_manifest(path).name == "timezone-tokyo"


@pytest.mark.parametrize("script", ["install", "uninstall"])
def test_the_scripts_are_valid_bash(script: str) -> None:
    subprocess.run(["bash", "-n", str(APP / script)], check=True)


def test_install_sets_asia_tokyo_and_restarts_what_runs(tmp_path: Path) -> None:
    calls, out = run(tmp_path, "install", active=("cron.service",))
    assert calls == [
        "timedatectl set-timezone Asia/Tokyo",
        "systemctl is-active --quiet cron.service",
        "systemctl restart cron.service",
        "systemctl is-active --quiet rsyslog.service",
        "timedatectl show --property=Timezone --value",
        "date +%Y-%m-%d %H:%M:%S %Z",
    ]
    assert out == "timezone-tokyo: Asia/Tokyo, now 2026-09-29 16:00:00 JST\n"


def test_uninstall_goes_back_to_utc(tmp_path: Path) -> None:
    calls, out = run(tmp_path, "uninstall", active=("cron.service", "rsyslog.service"))
    assert calls == [
        "timedatectl set-timezone Etc/UTC",
        "systemctl is-active --quiet cron.service",
        "systemctl restart cron.service",
        "systemctl is-active --quiet rsyslog.service",
        "systemctl restart rsyslog.service",
    ]
    assert out == ""
