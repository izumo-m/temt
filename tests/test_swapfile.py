"""temt と一緒に配るアプリ swapfile のテスト。

install と uninstall はインスタンスの上で root で動くので、アプリを一時ディレクトリにコピーし、
common.sh の /swapfile、/etc/fstab、/proc/meminfo をその中に差し替える。swapon、swapoff、
mkswap、fallocate、df、systemctl、chown は、呼ばれた引数を書き留めるものに差し替える。swapon は
使っている swap を状態のファイルに覚えて、--show に答える。
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from temt import deploy, names, options
from temt.manifest import read_manifest

APP = names.BUNDLED_APPS / "swapfile"
MIB = 1024 * 1024
GIB = 1024 * MIB
FSTAB = (
    "# /etc/fstab: static file system information.\nLABEL=cloudimg-rootfs\t/\text4\tdefaults\t0 1\n"
)


@dataclass
class Box:
    """差し替えた場所と、差し替えたコマンドの記録。"""

    root: Path
    swapfile: Path
    fstab: Path
    meminfo: Path
    state: Path
    log: Path
    env: dict[str, str]

    def line(self) -> str:
        """install が fstab に書く行。"""
        return f"{self.swapfile} none swap sw,nofail 0 0\n"

    def activate(self, size: int, used: int = 0) -> None:
        """size バイトの swapfile を作り、used バイトが swap に出ている状態にする。"""
        with self.swapfile.open("wb") as f:
            _ = f.truncate(size)
        self.swapfile.chmod(0o600)
        _ = self.state.write_text(
            f"{self.swapfile} {size // MIB}M {used // MIB}M {used}\n", encoding="ascii"
        )

    def calls(self) -> list[str]:
        """呼ばれたコマンドの行。swapon --show は数えない。"""
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="ascii").splitlines()

    def run(self, script: str, size: str = "1g") -> subprocess.CompletedProcess[str]:
        """script を実行する。install には size を deploy のオプションとして渡す。"""
        _ = (self.root / "app" / options.FILE).write_text(
            f"{options.variable('size')}={size}\n", encoding="ascii"
        )
        return subprocess.run(
            ["bash", str(self.root / "app" / script)],
            capture_output=True,
            text=True,
            check=False,
            env=self.env,
            cwd="/",
        )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    copy = tmp_path / "app"
    _ = shutil.copytree(APP, copy)
    swapfile = tmp_path / "swapfile"
    fstab = tmp_path / "fstab"
    meminfo = tmp_path / "meminfo"
    _ = fstab.write_text(FSTAB, encoding="ascii")
    fstab.chmod(0o644)
    _ = meminfo.write_text(
        f"MemTotal: 1000000 kB\nMemAvailable: {GIB // 1024} kB\n", encoding="ascii"
    )
    common = copy / "common.sh"
    text = common.read_text(encoding="utf-8")
    for name, path in (("SWAPFILE", swapfile), ("FSTAB", fstab), ("MEMINFO", meminfo)):
        text, count = re.subn(rf"^{name}=.*$", f"{name}={path}", text, flags=re.MULTILINE)
        assert count == 1
    _ = common.write_text(text, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    state = tmp_path / "state"
    state.touch()
    fakes = {
        "swapon": f"""case "$*" in
    "--show=NAME --noheadings --raw") awk '{{print $1}}' "{state}" ;;
    "--show=NAME,USED --bytes --noheadings --raw") awk '{{print $1, $4}}' "{state}" ;;
    "--show=NAME,SIZE,USED --noheadings --raw") awk '{{print $1, $2, $3}}' "{state}" ;;
    --*) echo "swapon: unexpected arguments: $*" >&2; exit 2 ;;
    *)
        echo "swapon $*" >> "{log}"
        [ -n "${{UNLISTED:-}}" ] || echo "$1 $(($(stat -c %s "$1") / {MIB}))M 0B 0" >> "{state}"
        ;;
esac""",
        "swapoff": f'echo "swapoff $*" >> "{log}"; : > "{state}"',
        "mkswap": f'echo "mkswap $*" >> "{log}"; echo "Setting up swapspace"',
        "fallocate": f'echo "fallocate $* umask=$(umask)" >> "{log}"; truncate -s "$2" "$3"',
        "df": f'echo "df $*" >> "{log}"; echo Avail; echo " $AVAIL"',
        "systemctl": f'echo "systemctl $*" >> "{log}"',
        "chown": f'echo "chown $*" >> "{log}"',
    }
    for name, body in fakes.items():
        fake = bin_dir / name
        _ = fake.write_text(f"#!/bin/bash\n{body}\n", encoding="ascii")
        fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AVAIL": str(6 * GIB),
    }
    return Box(tmp_path, swapfile, fstab, meminfo, state, log, env)


def test_the_app_comes_with_temt(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """conf ディレクトリの apps/ に無くても、deploy がアプリ ID でパッケージから見つける。"""
    _ = home_dir
    monkeypatch.setattr(names, "BUNDLED_APPS", APP.parent)
    app_id, path = deploy.locate("swapfile")
    assert app_id == "swapfile"
    assert path == APP
    manifest = read_manifest(path)
    assert manifest.name == "swapfile"
    assert manifest.options["size"].default == "1g"


@pytest.mark.parametrize("value", ["512m", "1g", "4g", "10g"])
def test_size_takes_mib_and_gib(value: str) -> None:
    assert read_manifest(APP).options["size"].accepts(value)


@pytest.mark.parametrize("value", ["0g", "01g", "1", "1G", "1gb", "1.5g", "1k", "-1g", "off"])
def test_size_refuses_other_forms(value: str) -> None:
    assert not read_manifest(APP).options["size"].accepts(value)


@pytest.mark.parametrize("script", ["install", "uninstall", "common.sh"])
def test_the_scripts_are_valid_bash(script: str) -> None:
    _ = subprocess.run(["bash", "-n", str(APP / script)], check=True)


def test_install_makes_the_swapfile_and_adds_it_to_fstab(box: Box) -> None:
    done = box.run("install")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert box.calls() == [
        f"df --output=avail -B1 {box.root}",
        f"fallocate -l {GIB} {box.swapfile} umask=0077",
        f"mkswap {box.swapfile}",
        f"swapon {box.swapfile}",
        f"chown root:root {box.swapfile}",
        "systemctl daemon-reload",
    ]
    assert box.swapfile.stat().st_size == GIB
    assert box.swapfile.stat().st_mode & 0o777 == 0o600
    assert box.fstab.read_text(encoding="ascii") == FSTAB + box.line()
    assert box.fstab.stat().st_mode & 0o777 == 0o644
    assert done.stdout == f"swapfile: {box.swapfile} 1024M, active, 0B used\n"


def test_install_takes_the_size_in_mib(box: Box) -> None:
    done = box.run("install", size="512m")
    assert done.returncode == 0, done.stderr
    assert f"fallocate -l {512 * MIB} {box.swapfile} umask=0077" in box.calls()


def test_install_keeps_the_swapfile_in_use_with_the_same_size(box: Box) -> None:
    """再 deploy や、焼いた AMI から起動したインスタンスでは、ファイルに触らない。"""
    box.activate(GIB, used=100 * MIB)
    _ = box.fstab.write_text(FSTAB + box.line(), encoding="ascii")
    done = box.run("install")
    assert done.returncode == 0, done.stderr
    assert box.calls() == [f"chown root:root {box.swapfile}"]
    assert done.stdout == f"swapfile: {box.swapfile} 1024M, active, 100M used\n"


def test_install_remakes_a_swapfile_deleted_in_use(box: Box) -> None:
    """使っているうちに消されたファイルは、/proc/swaps で名前に (deleted) が付く。

    --raw では空白が \\x20 になるので、/swapfile を使っているとはみなさずに作る。
    """
    _ = box.state.write_text(f"{box.swapfile}\\x20(deleted) 1024M 0B 0\n", encoding="ascii")
    done = box.run("install")
    assert done.returncode == 0, done.stderr
    assert box.calls()[:4] == [
        f"df --output=avail -B1 {box.root}",
        f"fallocate -l {GIB} {box.swapfile} umask=0077",
        f"mkswap {box.swapfile}",
        f"swapon {box.swapfile}",
    ]
    assert done.stdout == f"swapfile: {box.swapfile} 1024M, active, 0B used\n"


def test_install_says_why_when_swapon_does_not_take_the_file(box: Box) -> None:
    box.env["UNLISTED"] = "1"
    done = box.run("install")
    assert done.returncode == 1
    assert done.stderr == f"install: {box.swapfile} is not in use as swap after swapon\n"


def test_install_resizes_the_swapfile_in_use(box: Box) -> None:
    box.activate(GIB, used=100 * MIB)
    _ = box.fstab.write_text(FSTAB + box.line(), encoding="ascii")
    done = box.run("install", size="2g")
    assert done.returncode == 0, done.stderr
    assert box.calls() == [
        f"df --output=avail -B1 {box.root}",
        f"swapoff {box.swapfile}",
        f"fallocate -l {2 * GIB} {box.swapfile} umask=0077",
        f"mkswap {box.swapfile}",
        f"swapon {box.swapfile}",
        f"chown root:root {box.swapfile}",
    ]
    assert box.swapfile.stat().st_size == 2 * GIB


def test_install_remakes_a_swapfile_not_in_use(box: Box) -> None:
    """同じ大きさでも、使っていなければ swap の形か分からないので作り直す。"""
    with box.swapfile.open("wb") as f:
        _ = f.truncate(GIB)
    done = box.run("install")
    assert done.returncode == 0, done.stderr
    assert f"fallocate -l {GIB} {box.swapfile} umask=0077" in box.calls()
    assert "swapoff" not in " ".join(box.calls())


def test_install_does_not_turn_off_swap_that_memory_cannot_hold(box: Box) -> None:
    """swap に出ている量と余裕の 256 MiB が MemAvailable を超えれば、何も変えない。"""
    box.activate(GIB, used=800 * MIB)
    done = box.run("install", size="2g")
    assert done.returncode == 1
    assert done.stderr == (
        f"install: {box.swapfile} has 800 MiB swapped out, but only 1024 MiB of memory is "
        "available; reboot the instance, then deploy again\n"
    )
    assert box.calls() == [f"df --output=avail -B1 {box.root}"]
    assert box.swapfile.stat().st_size == GIB
    assert box.fstab.read_text(encoding="ascii") == FSTAB


def test_install_turns_off_swap_that_memory_can_hold(box: Box) -> None:
    box.activate(GIB, used=768 * MIB)
    done = box.run("install", size="2g")
    assert done.returncode == 0, done.stderr
    assert f"swapoff {box.swapfile}" in box.calls()


def test_install_leaves_1_gib_of_disk_space(box: Box) -> None:
    box.env["AVAIL"] = str(2 * GIB - 1)
    done = box.run("install")
    assert done.returncode == 1
    assert done.stderr == (
        "install: not enough disk space for a 1g swap file: 2047 MiB free, "
        "2048 MiB needed to leave 1 GiB\n"
    )
    assert box.calls() == [f"df --output=avail -B1 {box.root}"]
    assert not box.swapfile.exists()
    assert box.fstab.read_text(encoding="ascii") == FSTAB


def test_install_counts_the_space_of_the_old_swapfile(box: Box) -> None:
    """作り直すときは、いまのファイルを消せば空く分も空きに数える。"""
    _ = box.swapfile.write_bytes(b"\1" * MIB)
    freed = box.swapfile.stat().st_blocks * 512
    box.env["AVAIL"] = str(2 * GIB - freed)
    done = box.run("install")
    assert done.returncode == 0, done.stderr
    box.env["AVAIL"] = str(2 * GIB - freed - 1)
    _ = box.swapfile.write_bytes(b"\1" * MIB)
    _ = box.state.write_text("", encoding="ascii")
    done = box.run("install")
    assert done.returncode == 1
    assert "not enough disk space" in done.stderr


def test_install_refuses_a_size_too_large(box: Box) -> None:
    done = box.run("install", size="1234567890g")
    assert done.returncode == 1
    assert done.stderr == "install: size is too large: 1234567890g\n"
    assert box.calls() == []


def test_install_does_not_touch_what_is_not_a_regular_file(box: Box) -> None:
    box.swapfile.mkdir()
    done = box.run("install")
    assert done.returncode == 1
    assert done.stderr == (
        f"install: {box.swapfile} is not a regular file; move it away and deploy again\n"
    )
    assert box.calls() == []
    assert box.swapfile.is_dir()


def test_install_does_not_follow_a_symbolic_link(box: Box) -> None:
    target = box.root / "target"
    target.touch()
    box.swapfile.symlink_to(target)
    done = box.run("install")
    assert done.returncode == 1
    assert "is not a regular file" in done.stderr
    assert box.swapfile.is_symlink()


def test_install_replaces_the_line_of_fstab_and_restricts_the_file(box: Box) -> None:
    """前からあった /swapfile の行を書き換え、ファイルを root だけが読めるようにする。"""
    box.activate(GIB)
    box.swapfile.chmod(0o644)
    _ = box.fstab.write_text(
        f"{box.swapfile} none swap sw 0 0\n# {box.swapfile} kept\n" + FSTAB, encoding="ascii"
    )
    done = box.run("install")
    assert done.returncode == 0, done.stderr
    assert box.calls() == [f"chown root:root {box.swapfile}", "systemctl daemon-reload"]
    assert box.fstab.read_text(encoding="ascii") == f"# {box.swapfile} kept\n" + FSTAB + box.line()
    assert box.swapfile.stat().st_mode & 0o777 == 0o600


def test_uninstall_turns_off_and_removes_the_swapfile(box: Box) -> None:
    box.activate(GIB, used=100 * MIB)
    _ = box.fstab.write_text(FSTAB + box.line(), encoding="ascii")
    done = box.run("uninstall")
    assert done.returncode == 0, done.stderr
    assert (done.stdout, done.stderr) == ("", "")
    assert box.calls() == [f"swapoff {box.swapfile}", "systemctl daemon-reload"]
    assert not box.swapfile.exists()
    assert box.fstab.read_text(encoding="ascii") == FSTAB


def test_uninstall_does_nothing_without_the_swapfile(box: Box) -> None:
    done = box.run("uninstall")
    assert done.returncode == 0, done.stderr
    assert box.calls() == []
    assert box.fstab.read_text(encoding="ascii") == FSTAB
    assert list(box.root.glob("fstab.*")) == []


def test_uninstall_does_not_turn_off_swap_that_memory_cannot_hold(box: Box) -> None:
    box.activate(GIB, used=800 * MIB)
    _ = box.fstab.write_text(FSTAB + box.line(), encoding="ascii")
    done = box.run("uninstall")
    assert done.returncode == 1
    assert done.stderr == (
        f"uninstall: {box.swapfile} has 800 MiB swapped out, but only 1024 MiB of memory is "
        "available; reboot the instance, then run app remove again\n"
    )
    assert box.calls() == []
    assert box.swapfile.exists()
    assert box.fstab.read_text(encoding="ascii") == FSTAB + box.line()


def test_uninstall_leaves_what_is_not_a_regular_file(box: Box) -> None:
    box.swapfile.mkdir()
    done = box.run("uninstall")
    assert done.returncode == 0, done.stderr
    assert box.swapfile.is_dir()


def test_uninstall_leaves_a_symbolic_link_and_its_target(box: Box) -> None:
    """install が作るのは普通のファイルだけなので、リンクは辿らず、リンクも先も消さない。"""
    target = box.root / "target"
    _ = target.write_text("mine\n", encoding="ascii")
    box.swapfile.symlink_to(target)
    done = box.run("uninstall")
    assert done.returncode == 0, done.stderr
    assert box.swapfile.is_symlink()
    assert target.read_text(encoding="ascii") == "mine\n"
