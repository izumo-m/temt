"""ホームに置くものの用意のテスト。"""

import fcntl
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from temt import home
from temt.change import Change
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import CONF_DIR_ENV, SSH_DIR_ENV, Service, keys_dir, ssh_dir

# ~/.ssh/config に足す既定の Include の行
INCLUDE = "Include ~/.ssh/temt/config"


# 既定の場所で apply したときの ProxyCommand の値。場所は既定でも env で渡す。
# ホームの下の場所は ~ で始まる形で書くので、値はホームのパスに依らない
PROXY = (
    f"env {CONF_DIR_ENV}='~/.config/temt' {SSH_DIR_ENV}='~/.ssh/temt' temt proxy --svc dev-main %p"
)


# ProxyCommand の代わりに置く、proxy と同じく temt に場所を解かせて表示するスクリプト
SHOW_PLACES = (
    "#!/bin/sh\n"
    f"exec {shlex.quote(sys.executable)} -c "
    "'from temt import names; print(names.conf_dir()); print(names.ssh_dir())'\n"
)


def show_places(home_dir: Path) -> Path:
    """SHOW_PLACES を temt という名前で置いたディレクトリ。ProxyCommand は PATH から探す。"""
    path = home_dir / "show"
    path.mkdir()
    (path / "temt").write_text(SHOW_PLACES, encoding="utf-8")
    (path / "temt").chmod(0o755)
    return path


# 既定の設定で書いた Host ブロック
BLOCK = (
    "Host temt-dev-main\n"
    "    User               ubuntu\n"
    "    Port               22\n"
    "    IdentityFile       ~/.ssh/temt/keys/temt\n"
    "    IdentitiesOnly     yes\n"
    "    UserKnownHostsFile ~/.ssh/temt/known_hosts.d/temt-dev-main/current\n"
    f"    ProxyCommand       {PROXY}\n"
)


@pytest.fixture
def prepared(home_dir: Path) -> Path:
    """~/.ssh/temt/ を用意したホーム。"""
    home.prepare_dirs()
    return home_dir


def config() -> Path:
    """temt の ssh 設定ファイル（ssh ディレクトリの config）。"""
    return ssh_dir() / "config"


def read() -> str:
    """その中身。"""
    return config().read_text(encoding="utf-8")


def write(text: str) -> None:
    """そこに書く。"""
    config().write_text(text, encoding="utf-8")


def detect_block(home_dir: Path, values: Conf | None = None) -> home.Edit:
    """Host ブロックの書き換えを検出する。values を省けば既定の設定で求める。"""
    _ = home_dir
    return home.detect_host_block(Service("dev-main"), Conf() if values is None else values)


def write_block(home_dir: Path, values: Conf | None = None) -> bool:
    """apply と同じく、Host ブロックの書き換えを検出してから書く。"""
    return home.write_edit(detect_block(home_dir, values))


def test_prepare_dirs_makes_them_private(home_dir: Path) -> None:
    home.prepare_dirs()
    for path in (home_dir / ".ssh", ssh_dir(), keys_dir()):
        assert path.stat().st_mode & 0o777 == 0o700


def test_prepare_dirs_leaves_the_conf_dir_alone(home_dir: Path) -> None:
    """設定ファイルの場所は、設定ファイルを書くときに作る。"""
    home.prepare_dirs()
    assert not (home_dir / ".config").exists()


def test_prepare_dirs_may_run_again(prepared: Path) -> None:
    _ = prepared
    home.prepare_dirs()
    assert keys_dir().is_dir()


def test_dir_changes_adds_the_missing_dirs(home_dir: Path) -> None:
    """detect_dirs は調べるだけで、ディレクトリを作らない。無い親も上から挙げる。"""
    assert home.dir_changes(home.detect_dirs()) == [
        Change("+", "dir", "~/.ssh", "mode 700"),
        Change("+", "dir", "~/.ssh/temt", "mode 700"),
        Change("+", "dir", "~/.ssh/temt/keys", "mode 700"),
    ]
    assert not (home_dir / ".ssh").exists()


def test_dir_changes_tightens_an_open_directory(prepared: Path) -> None:
    _ = prepared
    ssh_dir().chmod(0o755)
    assert home.dir_changes(home.detect_dirs()) == [
        Change("~", "dir", "~/.ssh/temt", "mode: 755 -> 700")
    ]
    assert ssh_dir().stat().st_mode & 0o777 == 0o755


def test_dir_changes_is_empty_for_prepared_dirs(prepared: Path) -> None:
    _ = prepared
    assert home.dir_changes(home.detect_dirs()) == []


def test_the_command_runs_temt_with_the_service(home_dir: Path) -> None:
    """サービスのコマンドは、PATH にある temt を、--svc を付けて引数ごと実行する。"""
    service = Service("dev-main")
    assert home.command_missing(service)
    home.write_command(service)
    assert not home.command_missing(service)
    assert service.command_path == home_dir / ".local" / "bin" / "temt-dev-main"
    assert service.command_path.stat().st_mode & 0o111
    elsewhere = home_dir / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "temt").write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    (elsewhere / "temt").chmod(0o755)
    shown = subprocess.run(
        [str(service.command_path), "instance", "show", "a b"],
        env={**os.environ, "PATH": f"{elsewhere}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert shown.stdout.splitlines() == ["--svc", "dev-main", "instance", "show", "a b"]


def test_command_changes_adds_a_missing_command(home_dir: Path) -> None:
    _ = home_dir
    service = Service("dev-main")
    assert home.command_changes(service, home.command_missing(service)) == [
        Change("+", "command", "~/.local/bin/temt-dev-main")
    ]


@pytest.mark.parametrize("kind", ["file", "symlink", "broken symlink"])
def test_the_command_leaves_whatever_is_in_place(home_dir: Path, kind: str) -> None:
    """場所に何かあれば、中身にかかわらず書かない。行き先の無いリンクも残す。"""
    service = Service("dev-main")
    path = service.command_path
    path.parent.mkdir(parents=True)
    if kind == "file":
        path.write_text("mine\n", encoding="utf-8")
    else:
        target = home_dir / "target"
        if kind == "symlink":
            target.write_text("mine\n", encoding="utf-8")
        path.symlink_to(target)
    assert home.command_changes(service, home.command_missing(service)) == []


@pytest.mark.parametrize("kind", ["file", "symlink", "broken symlink"])
def test_remove_command_removes_whatever_is_in_place(home_dir: Path, kind: str) -> None:
    """シンボリックリンクなら、リンクだけを消し、行き先は残す。"""
    service = Service("dev-main")
    path = service.command_path
    path.parent.mkdir(parents=True)
    target = home_dir / "target"
    if kind == "file":
        path.write_text("mine\n", encoding="utf-8")
    else:
        if kind == "symlink":
            target.write_text("mine\n", encoding="utf-8")
        path.symlink_to(target)
    assert home.remove_command(service) is True
    assert not path.is_symlink()
    assert not path.exists()
    assert target.exists() == (kind == "symlink")


def test_remove_command_without_one(home_dir: Path) -> None:
    _ = home_dir
    assert home.remove_command(Service("dev-main")) is False


def test_detecting_writes_nothing(home_dir: Path) -> None:
    """差分を検出するだけでは、ホームに何も作らない。diff は検出だけをたどる。"""
    service = Service("dev-main")
    assert home.detect_dirs()
    assert home.command_missing(service)
    assert home.detect_host_block(service, Conf()).changed
    assert home.detect_include().changed
    assert list(home_dir.rglob("*")) == []


def test_write_host_block_creates_the_file(prepared: Path) -> None:
    assert write_block(prepared) is True
    assert read() == BLOCK
    assert config().stat().st_mode & 0o777 == 0o600


def test_write_host_block_changes_nothing_the_second_time(prepared: Path) -> None:
    write_block(prepared)
    assert write_block(prepared) is False
    assert read() == BLOCK


def test_write_host_block_leaves_the_rest_alone(prepared: Path) -> None:
    write(
        "# mine\n"
        "Host other\n"
        "    User root\n"
        "\n"
        "Host temt-dev-main\n"
        "    LocalForward 8080 localhost:8080\n"
        "    User root\n"
        "\n"
        "Host last\n"
        "    User me\n"
    )
    assert write_block(prepared) is True
    assert read() == (
        "# mine\n"
        "Host other\n"
        "    User root\n"
        "\n"
        "Host temt-dev-main\n"
        "    Port               22\n"
        "    IdentityFile       ~/.ssh/temt/keys/temt\n"
        "    IdentitiesOnly     yes\n"
        "    UserKnownHostsFile ~/.ssh/temt/known_hosts.d/temt-dev-main/current\n"
        f"    ProxyCommand       {PROXY}\n"
        "    LocalForward 8080 localhost:8080\n"
        "    User               ubuntu\n"
        "\n"
        "Host last\n"
        "    User me\n"
    )


def test_write_host_block_keeps_the_first_of_duplicates(prepared: Path) -> None:
    write("Host temt-dev-main\n    IdentityFile ~/a\n    IdentityFile ~/b\n")
    write_block(prepared)
    assert read().count("IdentityFile") == 1


def test_write_host_block_takes_a_host_line_with_other_names(
    prepared: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write("Host dev temt-dev-main\n")
    assert write_block(prepared) is True
    assert read() == BLOCK.replace("Host temt-dev-main", "Host dev temt-dev-main")
    assert "warning:" not in capsys.readouterr().err


def test_write_host_block_takes_the_first_host_line_with_the_name(prepared: Path) -> None:
    write("Host dev temt-dev-main\n\nHost temt-dev-main\n    User me\n")
    write_block(prepared)
    assert read() == (
        BLOCK.replace("Host temt-dev-main", "Host dev temt-dev-main")
        + "\nHost temt-dev-main\n    User me\n"
    )


def test_write_host_block_matches_whole_names_only(prepared: Path) -> None:
    write("Host temt-dev-main-old\n    User root\n")
    write_block(prepared)
    assert read() == "Host temt-dev-main-old\n    User root\n\n" + BLOCK


def test_write_host_block_goes_after_other_blocks(prepared: Path) -> None:
    write("Host other\n    User root\n")
    write_block(prepared)
    assert read() == "Host other\n    User root\n\n" + BLOCK


def test_host_block_changes_shows_only_the_new_block(prepared: Path, service: Service) -> None:
    """続く行には、足すブロックの行だけを出す。他のブロックは出さない。"""
    write("Host other\n    User root\n")
    assert home.host_block_changes(service, detect_block(prepared)) == [
        Change(
            "+",
            "ssh-host",
            "temt-dev-main",
            "~/.ssh/temt/config",
            ("@@ -0,0 +1,7 @@", *(f"+{line}" for line in BLOCK.splitlines())),
        )
    ]


def test_host_block_changes_shows_the_changed_lines(prepared: Path, service: Service) -> None:
    assert write_block(prepared) is True
    assert home.host_block_changes(service, detect_block(prepared, Conf(SshdPorts=[2222]))) == [
        Change(
            "~",
            "ssh-host",
            "temt-dev-main",
            "~/.ssh/temt/config",
            (
                "@@ -1,5 +1,5 @@",
                " Host temt-dev-main",
                "     User               ubuntu",
                "-    Port               22",
                "+    Port               2222",
                "     IdentityFile       ~/.ssh/temt/keys/temt",
                "     IdentitiesOnly     yes",
            ),
        )
    ]


def test_host_block_changes_is_empty_for_a_block_in_place(prepared: Path, service: Service) -> None:
    assert write_block(prepared) is True
    assert home.host_block_changes(service, detect_block(prepared)) == []


def test_detect_host_block_leaves_the_file_alone(prepared: Path) -> None:
    write("Host temt-dev-main\n    User root\n")
    assert detect_block(prepared).changed
    assert read() == "Host temt-dev-main\n    User root\n"


def read_user_config() -> str:
    """利用者の ssh 設定の中身。"""
    return (Path.home() / ".ssh" / "config").read_text(encoding="utf-8")


def write_user_config(text: str) -> Path:
    """利用者の ssh 設定を用意する。~/.ssh/ が無ければモード 700 で作る。"""
    path = Path.home() / ".ssh" / "config"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def include() -> str | None:
    """apply と同じく、Include の行の書き換えを検出してから書く。書いたらその行を返す。

    見えなくなる Host は、apply と同じく、書いたあとの Host ブロックを除いて数える。
    """
    edit = home.detect_include()
    return home.include_line() if home.write_include(edit, detect_block(Path.home())) else None


def test_detect_include_leaves_the_file_alone(prepared: Path) -> None:
    _ = prepared
    write_user_config(f"{home.MARK}\nInclude ~/old/ssh/config\n")
    assert home.detect_include().replaced == "Include ~/old/ssh/config"
    assert read_user_config() == f"{home.MARK}\nInclude ~/old/ssh/config\n"


def test_include_changes_adds_the_line(prepared: Path) -> None:
    assert home.include_changes(home.detect_include(), detect_block(prepared)) == [
        Change("+", "ssh-include", "~/.ssh/config", INCLUDE)
    ]


def test_include_changes_is_empty_for_the_line_in_place(prepared: Path) -> None:
    assert include() == INCLUDE
    assert home.include_changes(home.detect_include(), detect_block(prepared)) == []


def test_write_include_creates_the_file(prepared: Path) -> None:
    _ = prepared
    assert include() == INCLUDE
    path = Path.home() / ".ssh" / "config"
    assert path.read_text(encoding="utf-8") == f"{home.MARK}\n{INCLUDE}\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_include_goes_first(prepared: Path) -> None:
    _ = prepared
    write_user_config("Host other\n    User root\n")
    assert include() == INCLUDE
    assert read_user_config() == f"{home.MARK}\n{INCLUDE}\nHost other\n    User root\n"


def test_write_include_changes_nothing_the_second_time(prepared: Path) -> None:
    _ = prepared
    include()
    before = read_user_config()
    assert include() is None
    assert read_user_config() == before


def test_write_include_replaces_the_line_when_the_place_changed(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """場所を変えて apply をやり直すと、目印の次の行が書き換わる。古い行は残らない。"""
    home.prepare_dirs()
    include()
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere"))
    home.prepare_dirs()
    assert include() == "Include ~/elsewhere/config"
    assert read_user_config() == f"{home.MARK}\nInclude ~/elsewhere/config\n"


def test_write_include_does_not_follow_the_conf_dir(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """設定ファイルの場所を変えても、Include は変わらない。"""
    monkeypatch.setenv(CONF_DIR_ENV, str(home_dir / "elsewhere"))
    home.prepare_dirs()
    assert include() == INCLUDE


def test_write_include_warns_about_hosts_it_drops(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """張り替える前の config にある Host は ssh から見えなくなるので、名前を挙げる。

    このサービスの Host は、apply が新しい config に先に書くので、挙げない。
    """
    home.prepare_dirs()
    assert write_block(home_dir) is True
    with config().open("a", encoding="utf-8") as file:
        _ = file.write("\nHost other\n    User root\n")
    include()
    _ = capsys.readouterr()
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere"))
    home.prepare_dirs()
    assert include() == "Include ~/elsewhere/config"
    assert capsys.readouterr().err == (
        "temt: warning: ~/.ssh/config no longer includes ~/.ssh/temt/config; "
        "ssh cannot reach other there\n"
    )


@pytest.mark.parametrize("content", [None, "# no hosts\n"])
def test_write_include_is_quiet_when_it_drops_no_host(
    home_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
) -> None:
    """前の config が無いか、Host を持たなければ、黙って張り替える。"""
    home.prepare_dirs()
    include()
    if content is not None:
        write(content)
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere"))
    home.prepare_dirs()
    assert include() == "Include ~/elsewhere/config"
    assert capsys.readouterr().err == ""


def test_include_changes_lists_the_hosts_it_drops(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """張り替えで ssh から見えなくなる Host は、警告せずに続く行に挙げる。

    このサービスの Host は、apply が Include より先に新しい config へ書くので、挙げない。
    """
    home.prepare_dirs()
    assert write_block(home_dir) is True
    with config().open("a", encoding="utf-8") as file:
        _ = file.write("\nHost other\n    User root\n")
    assert include() == INCLUDE
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere"))
    home.prepare_dirs()
    assert home.include_changes(home.detect_include(), detect_block(home_dir)) == [
        Change(
            "~",
            "ssh-include",
            "~/.ssh/config",
            f"{INCLUDE} -> Include ~/elsewhere/config",
            ("ssh cannot reach other in ~/.ssh/temt/config",),
        )
    ]
    assert capsys.readouterr().err == ""


def test_include_changes_replaces_a_line_that_drops_no_host(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home.prepare_dirs()
    assert include() == INCLUDE
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere"))
    assert home.include_changes(home.detect_include(), detect_block(home_dir)) == [
        Change("~", "ssh-include", "~/.ssh/config", f"{INCLUDE} -> Include ~/elsewhere/config")
    ]


def test_write_include_reads_the_escaped_path_it_wrote(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """glob の文字を守った Include の行からも、前の config を見つける。"""
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "a*c"))
    home.prepare_dirs()
    write("Host other\n    User root\n")
    include()
    _ = capsys.readouterr()
    monkeypatch.delenv(SSH_DIR_ENV)
    home.prepare_dirs()
    assert include() == INCLUDE
    assert "ssh cannot reach other there" in capsys.readouterr().err


def test_write_include_keeps_what_follows_the_mark_if_it_is_not_an_include(
    prepared: Path,
) -> None:
    """目印の次が Include でなければ、その行は動かさず、間に足す。"""
    _ = prepared
    write_user_config(f"{home.MARK}\nHost other\n")
    assert include() == INCLUDE
    assert read_user_config() == f"{home.MARK}\n{INCLUDE}\nHost other\n"


def test_write_include_leaves_an_unmarked_line_alone(prepared: Path) -> None:
    """目印の無い Include は利用者のものとして扱い、自分の 2 行を先頭に足す。"""
    _ = prepared
    write_user_config(f"{INCLUDE}\n")
    assert include() == INCLUDE
    assert read_user_config() == f"{home.MARK}\n{INCLUDE}\n{INCLUDE}\n"


def test_write_include_ignores_a_mark_after_a_host(prepared: Path) -> None:
    """Host より後ろの Include はそのブロックの設定にしかならないので、目印も見ない。"""
    _ = prepared
    write_user_config(f"Host other\n{home.MARK}\nInclude ~/old/ssh/config\n")
    assert include() == INCLUDE
    assert read_user_config() == (
        f"{home.MARK}\n{INCLUDE}\nHost other\n{home.MARK}\nInclude ~/old/ssh/config\n"
    )


def test_write_include_ignores_a_commented_out_include(prepared: Path) -> None:
    _ = prepared
    write_user_config(f"# {INCLUDE}\n")
    assert include() == INCLUDE
    assert read_user_config() == f"{home.MARK}\n{INCLUDE}\n# {INCLUDE}\n"


def test_write_include_takes_the_first_mark(prepared: Path) -> None:
    """目印が 2 つあれば先頭のものを管理する。利用者が複製した残りには触らない。"""
    _ = prepared
    old = "Include ~/old/ssh/config"
    write_user_config(f"{home.MARK}\n{old}\n{home.MARK}\n{old}\n")
    assert include() == INCLUDE
    assert read_user_config() == f"{home.MARK}\n{INCLUDE}\n{home.MARK}\n{old}\n"


def test_write_include_adds_after_a_mark_at_the_end(prepared: Path) -> None:
    _ = prepared
    write_user_config(f"Include ~/mine/config\n{home.MARK}\n")
    assert include() == INCLUDE
    assert read_user_config() == f"Include ~/mine/config\n{home.MARK}\n{INCLUDE}\n"


def test_write_include_keeps_a_blank_line_after_the_mark(prepared: Path) -> None:
    """目印の次が Include でなければ、その行は動かさず、間に足す。"""
    _ = prepared
    old = "Include ~/old/ssh/config"
    write_user_config(f"{home.MARK}\n\n{old}\n")
    assert include() == INCLUDE
    assert read_user_config() == f"{home.MARK}\n{INCLUDE}\n\n{old}\n"


def test_write_include_replaces_the_whole_line_it_manages(prepared: Path) -> None:
    """目印の次の Include は temt のものなので、並んだ引数ごと書き換える。"""
    _ = prepared
    write_user_config(f"{home.MARK}\nInclude ~/old/ssh/config ~/mine/config\n")
    assert include() == INCLUDE
    assert read_user_config() == f"{home.MARK}\n{INCLUDE}\n"


def test_write_include_writes_a_tilde_path_for_a_moved_ssh_dir(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere"))
    home.prepare_dirs()
    assert include() == "Include ~/elsewhere/config"
    # 場所がどこでも、Include を足す先の ~/.ssh/ は用意する
    assert (home_dir / ".ssh").stat().st_mode & 0o777 == 0o700


def test_write_include_writes_an_absolute_path_for_an_ssh_dir_outside_the_user(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "temt"
    monkeypatch.setenv(SSH_DIR_ENV, str(outside))
    home.prepare_dirs()
    assert include() == f"Include {outside}/config"


@pytest.mark.parametrize(
    ("name", "written"),
    [("a[b]c", r"a\[b]c"), ("a*c", r"a\*c"), ("a?c", r"a\?c"), ("a{b,c}", "a{b,c}")],
)
def test_write_include_escapes_glob_characters(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, name: str, written: str
) -> None:
    """ssh は Include のパスを glob として読む。{} は展開しないので、守るのは * ? [ だけ。"""
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / name))
    home.prepare_dirs()
    assert include() == f"Include ~/{written}/config"
    assert include() is None


def test_write_host_block_carries_the_directories_in_the_proxy_command(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """場所を変えれば、ssh の設定のパスも ProxyCommand が渡す値も変わる。"""
    monkeypatch.setenv(CONF_DIR_ENV, str(home_dir / "elsewhere" / "conf"))
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere" / "ssh"))
    home.prepare_dirs()
    assert write_block(home_dir) is True
    assert read() == (
        "Host temt-dev-main\n"
        "    User               ubuntu\n"
        "    Port               22\n"
        "    IdentityFile       ~/elsewhere/ssh/keys/temt\n"
        "    IdentitiesOnly     yes\n"
        "    UserKnownHostsFile ~/elsewhere/ssh/known_hosts.d/temt-dev-main/current\n"
        f"    ProxyCommand       env {CONF_DIR_ENV}='~/elsewhere/conf' "
        f"{SSH_DIR_ENV}='~/elsewhere/ssh' "
        "temt proxy --svc dev-main %p\n"
    )


def test_the_proxy_command_carries_the_default_directories(prepared: Path) -> None:
    """既定のままの場所も渡す。渡さないと、proxy は ssh を打ったシェルの値を読む。"""
    assert write_block(prepared) is True
    line = next(one for one in read().splitlines() if "ProxyCommand" in one)
    assert line == f"    ProxyCommand       {PROXY}"


def test_the_proxy_command_wins_over_the_shell(home_dir: Path) -> None:
    """シェルに別の場所があっても、proxy には apply のときの場所が届く。"""
    home.prepare_dirs()
    bin_path = show_places(home_dir)
    assert home.write_edit(home.detect_host_block(Service("dev-main"), Conf())) is True
    line = next(one for one in read().splitlines() if "ProxyCommand" in one)
    command = line.split(maxsplit=1)[1]
    environment = {
        **os.environ,
        CONF_DIR_ENV: "/srv/other/conf",
        SSH_DIR_ENV: "/srv/other/ssh",
        "PATH": f"{bin_path}:{os.environ['PATH']}",
    }
    shown = subprocess.run(
        ["/bin/sh", "-c", f"exec {command}"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert shown.stdout == f"{home_dir}/.config/temt\n{home_dir}/.ssh/temt\n"


def test_the_proxy_command_hands_the_directories_to_the_shell(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh は ProxyCommand を利用者のシェルの exec に渡す。その形で場所が届く。"""
    monkeypatch.setenv(CONF_DIR_ENV, str(home_dir / "elsewhere" / "conf"))
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere" / "ssh"))
    home.prepare_dirs()
    bin_path = show_places(home_dir)
    assert home.write_edit(home.detect_host_block(Service("dev-main"), Conf())) is True
    line = next(one for one in read().splitlines() if "ProxyCommand" in one)
    command = line.split(maxsplit=1)[1]
    # シェルに場所を変える環境変数が無くても届くことを見るので、環境からは外して渡す
    environment = {
        key: value for key, value in os.environ.items() if key not in (CONF_DIR_ENV, SSH_DIR_ENV)
    }
    environment["PATH"] = f"{bin_path}:{environment['PATH']}"
    shown = subprocess.run(
        ["/bin/sh", "-c", f"exec {command}"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert shown.stdout == f"{home_dir}/elsewhere/conf\n{home_dir}/elsewhere/ssh\n"


def test_write_host_block_changes_the_identity_file_of_an_existing_block(prepared: Path) -> None:
    write_block(prepared)
    assert write_block(prepared, Conf(KeyName="work")) is True
    assert "    IdentityFile       ~/.ssh/temt/keys/work\n" in read()
    assert read().count("IdentityFile") == 1


def identity_line() -> str:
    """書いた Host ブロックの IdentityFile の行。"""
    return next(one for one in read().splitlines() if "IdentityFile" in one)


def test_write_host_block_quotes_an_identity_file_with_a_space(prepared: Path) -> None:
    """ssh は語を空白で分けるので、空白を含む値は二重引用符で囲む。"""
    assert write_block(prepared, Conf(IdentityFile="~/my keys/temt")) is True
    assert identity_line() == '    IdentityFile       "~/my keys/temt"'


def test_write_host_block_doubles_a_percent_sign(prepared: Path) -> None:
    """ssh は % をトークンとして展開するので、文字としての % は %% と書く。"""
    assert write_block(prepared, Conf(IdentityFile="~/keys/50%off")) is True
    assert identity_line() == "    IdentityFile       ~/keys/50%%off"


@pytest.mark.parametrize("value", ["~/my keys/50%off", "~/keys/50%off", "~/my keys/temt"])
def test_write_host_block_changes_nothing_the_second_time_for_a_guarded_path(
    prepared: Path, value: str
) -> None:
    """引用符と %% で書いた行も、次の apply では同じ行になり、書き換えない。"""
    write_block(prepared, Conf(IdentityFile=value))
    written = read()
    assert write_block(prepared, Conf(IdentityFile=value)) is False
    assert read() == written


def test_write_host_block_rewrites_a_hand_quoted_identity_file(prepared: Path) -> None:
    """手で引用符を付けた行は、ssh には同じ値でも、temt が書く形に戻す。"""
    write(BLOCK.replace("~/.ssh/temt/keys/temt", '"~/.ssh/temt/keys/temt"'))
    assert write_block(prepared) is True
    assert read() == BLOCK


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs ssh")
def test_ssh_reads_the_identity_file_as_it_is_in_the_config_file(
    prepared: Path, tmp_path: Path
) -> None:
    """ssh は、引用符を外し、%% を % に戻して、設定ファイルに書いたパスの鍵を探す。

    ProxyCommand を false に差し替えるので、ssh はどこにもつながらない。鍵を探すのは
    ProxyCommand を起こした後、鍵の交換の前である。
    """
    key = tmp_path / "my keys" / "50%off"
    write_block(prepared, Conf(IdentityFile=str(key)))
    done = subprocess.run(
        [
            "ssh",
            "-v",
            "-F",
            str(config()),
            "-o",
            "ProxyCommand=false",
            "-o",
            "BatchMode=yes",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "temt-dev-main",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert f"debug1: identity file {key} type -1" in done.stderr.splitlines()


def test_write_host_block_writes_the_first_sshd_port(prepared: Path) -> None:
    """Port は SshdPorts の先頭。2 つ目以降は ssh -p で指す。"""
    assert write_block(prepared, Conf(SshdPorts=[2222, 443])) is True
    assert read() == BLOCK.replace("Port               22\n", "Port               2222\n")


def test_write_host_block_changes_the_port_of_an_existing_block(prepared: Path) -> None:
    """手で書いた Port も、次の apply で SshdPorts の先頭に戻る。"""
    write("Host temt-dev-main\n    port=2200\n    Port 2201\n")
    assert write_block(prepared, Conf(SshdPorts=[443])) is True
    # = で書いた行も含めて、手で書いた 2 行は 1 行の Port 443 になる
    assert read() == BLOCK.replace("    Port               22\n", "") + (
        "    Port               443\n"
    )


def test_write_host_block_stops_at_a_match_block(prepared: Path) -> None:
    write("Host temt-dev-main\n    User root\nMatch host other\n    ForwardAgent yes\n")
    write_block(prepared)
    assert read().endswith(
        "    User               ubuntu\nMatch host other\n    ForwardAgent yes\n"
    )


def test_write_host_block_reads_equals_and_case(prepared: Path) -> None:
    write("HOST=temt-dev-main\n    identityfile=~/.ssh/other\n")
    assert write_block(prepared) is True
    assert read() == (
        "HOST=temt-dev-main\n"
        "    User               ubuntu\n"
        "    Port               22\n"
        "    IdentitiesOnly     yes\n"
        "    UserKnownHostsFile ~/.ssh/temt/known_hosts.d/temt-dev-main/current\n"
        f"    ProxyCommand       {PROXY}\n"
        "    IdentityFile       ~/.ssh/temt/keys/temt\n"
    )


def test_write_host_block_takes_a_file_without_a_last_newline(prepared: Path) -> None:
    write("Host temt-dev-main\n    User root")
    write_block(prepared)
    assert read().endswith("    User               ubuntu\n")


def test_write_host_block_keeps_comments_and_blank_lines(prepared: Path) -> None:
    write("Host temt-dev-main\n    # note\n\n    User root\n")
    write_block(prepared)
    assert read().endswith("    # note\n\n    User               ubuntu\n")


def test_write_edit_keeps_the_mode_of_an_existing_file(prepared: Path) -> None:
    config().write_text("", encoding="utf-8")
    config().chmod(0o644)
    write_block(prepared)
    assert config().stat().st_mode & 0o777 == 0o644


def test_write_edit_stops_when_the_file_changed_after_detecting(prepared: Path) -> None:
    """検出してから書くまでに書き換えられていれば、その中身を失わないよう書かずに止める。"""
    edit = detect_block(prepared)
    write("Host other\n")
    with pytest.raises(TemtError, match="~/.ssh/temt/config changed after temt read it"):
        home.write_edit(edit)
    assert read() == "Host other\n"


def test_write_edit_holds_the_lock_from_checking_to_replacing(
    prepared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """確かめてから置き換えるまで、ssh の設定を書き換えるロックを持つ。

    別のサービスの apply が、その間に書いて置き換えた中身を消さないようにするため。
    """
    edit = detect_block(prepared)
    held: list[bool] = []
    replace_text = home.replace_text

    def replacing(path: Path, text: str, *, mode: int = 0o666) -> None:
        with (ssh_dir() / home.EDIT_LOCK_NAME).open("a", encoding="utf-8") as file:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
        replace_text(path, text, mode=mode)

    monkeypatch.setattr(home, "replace_text", replacing)
    assert home.write_edit(edit) is True
    assert held == [True]


def test_write_edit_waits_for_the_lock(prepared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ほかが書き換えている間は、離されるまで待ってから確かめる。

    待つ間にほかが書いた中身は、確かめで見つけて消さずに止まる。書く側がロックを
    取りに行ったのを見てから書くので、確かめがロックの外にあれば、この中身を消して
    しまい落ちる。
    """
    edit = detect_block(prepared)
    raised: list[BaseException] = []
    reaching = threading.Event()
    flock = fcntl.flock

    def flocking(fd: int, operation: int) -> None:
        if threading.current_thread() is not threading.main_thread():
            reaching.set()
        flock(fd, operation)

    def writing() -> None:
        try:
            _ = home.write_edit(edit)
        except TemtError as e:
            raised.append(e)

    monkeypatch.setattr(fcntl, "flock", flocking)
    writer = threading.Thread(target=writing)
    with (ssh_dir() / home.EDIT_LOCK_NAME).open("a", encoding="utf-8") as file:
        flock(file.fileno(), fcntl.LOCK_EX)
        writer.start()
        assert reaching.wait(5)
        writer.join(0.2)
        assert writer.is_alive()
        write("Host other\n")
    writer.join(5)
    assert not writer.is_alive()
    assert [str(e) for e in raised] == [
        "~/.ssh/temt/config changed after temt read it; run apply again"
    ]
    assert read() == "Host other\n"


def test_write_include_holds_the_lock_from_checking_to_replacing(
    prepared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Include の行も、確かめてから置き換えるまで、ssh の設定を書き換えるロックを持つ。"""
    edit = home.detect_include()
    held: list[bool] = []
    replace_text = home.replace_text

    def replacing(path: Path, text: str, *, mode: int = 0o666) -> None:
        with (ssh_dir() / home.EDIT_LOCK_NAME).open("a", encoding="utf-8") as file:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
        replace_text(path, text, mode=mode)

    monkeypatch.setattr(home, "replace_text", replacing)
    assert home.write_include(edit, detect_block(prepared)) is True
    assert held == [True]


def test_write_include_waits_for_the_lock(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ほかが書き換えている間は、離されるまで待ってから確かめる。

    待つ間にほかが書いた中身は、確かめで見つけて消さずに止まる。張り替えで前の config の
    Host が見えなくなるときも、書かずに止まるので、その警告は出さない。
    """
    home.prepare_dirs()
    write("Host other\n    User root\n")
    include()
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere"))
    home.prepare_dirs()
    edit = home.detect_include()
    assert edit.replaced == INCLUDE
    host = detect_block(home_dir)
    _ = capsys.readouterr()
    raised: list[BaseException] = []
    reaching = threading.Event()
    flock = fcntl.flock

    def flocking(fd: int, operation: int) -> None:
        if threading.current_thread() is not threading.main_thread():
            reaching.set()
        flock(fd, operation)

    def writing() -> None:
        try:
            _ = home.write_include(edit, host)
        except TemtError as e:
            raised.append(e)

    monkeypatch.setattr(fcntl, "flock", flocking)
    writer = threading.Thread(target=writing)
    changed = f"{home.MARK}\n{INCLUDE}\nHost mine\n    User root\n"
    with (ssh_dir() / home.EDIT_LOCK_NAME).open("a", encoding="utf-8") as file:
        flock(file.fileno(), fcntl.LOCK_EX)
        writer.start()
        assert reaching.wait(5)
        writer.join(0.2)
        assert writer.is_alive()
        _ = write_user_config(changed)
    writer.join(5)
    assert not writer.is_alive()
    assert [str(e) for e in raised] == ["~/.ssh/config changed after temt read it; run apply again"]
    assert read_user_config() == changed
    assert capsys.readouterr().err == ""


def test_write_include_stops_when_the_file_was_removed_after_detecting(prepared: Path) -> None:
    _ = prepared
    path = write_user_config("Host other\n")
    edit = home.detect_include()
    path.unlink()
    with pytest.raises(TemtError, match="~/.ssh/config changed after temt read it"):
        home.write_include(edit, detect_block(prepared))
    assert not path.exists()


def test_write_edit_keeps_a_symlinked_config(prepared: Path) -> None:
    real = prepared / "dotfiles" / "ssh_config"
    real.parent.mkdir()
    real.write_text("Host other\n", encoding="utf-8")
    link = prepared / ".ssh" / "config"
    link.parent.mkdir(mode=0o700, exist_ok=True)
    link.symlink_to(real)
    assert include() == INCLUDE
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == f"{home.MARK}\n{INCLUDE}\nHost other\n"


def test_prepare_dirs_tightens_an_open_directory(home_dir: Path) -> None:
    _ = home_dir
    ssh_dir().mkdir(parents=True)
    ssh_dir().chmod(0o755)
    home.prepare_dirs()
    assert ssh_dir().stat().st_mode & 0o777 == 0o700


def test_prepare_dirs_leaves_an_existing_parent(home_dir: Path) -> None:
    (home_dir / ".ssh").mkdir()
    (home_dir / ".ssh").chmod(0o755)
    home.prepare_dirs()
    assert (home_dir / ".ssh").stat().st_mode & 0o777 == 0o755


def test_prepare_dirs_makes_missing_parents_private(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "a" / "b" / "temt"))
    home.prepare_dirs()
    for path in (home_dir / "a", home_dir / "a" / "b", ssh_dir(), keys_dir()):
        assert path.stat().st_mode & 0o777 == 0o700


def test_use_known_hosts_makes_an_empty_file_and_points_current_at_it(
    home_dir: Path, service: Service
) -> None:
    path = home.use_known_hosts(service, "i-0123456789abcdef0")
    assert path == service.known_hosts_dir / "i-0123456789abcdef0"
    assert path.read_bytes() == b""
    assert path.stat().st_mode & 0o777 == 0o600
    assert service.known_hosts_dir.stat().st_mode & 0o777 == 0o700
    assert (home_dir / ".ssh" / "temt").stat().st_mode & 0o777 == 0o700
    # current は同じディレクトリの中の相対リンクにする
    assert service.known_hosts_current.readlink() == Path("i-0123456789abcdef0")
    # 一時名のリンクは残さない
    assert not list(service.known_hosts_dir.glob(".current.*"))


def test_use_known_hosts_keeps_what_is_there(service: Service) -> None:
    """あれば中身に触れない。同時に走った別の接続が書いたホスト鍵を消さないため。"""
    path = home.use_known_hosts(service, "i-0123456789abcdef0")
    path.write_text("host ssh-ed25519 AAAA\n", encoding="utf-8")
    path.chmod(0o644)
    assert home.use_known_hosts(service, "i-0123456789abcdef0") == path
    assert path.read_text(encoding="utf-8") == "host ssh-ed25519 AAAA\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_use_known_hosts_moves_current_to_the_new_instance(service: Service) -> None:
    old = home.use_known_hosts(service, "i-0fedcba9876543210")
    _ = home.use_known_hosts(service, "i-0123456789abcdef0")
    assert service.known_hosts_current.readlink() == Path("i-0123456789abcdef0")
    # 前のインスタンスのファイルは terminate が消すまで残る
    assert old.exists()


def test_use_known_hosts_replaces_a_current_that_is_not_a_symlink(service: Service) -> None:
    """ssh が known_hosts を書き直して普通のファイルにしていても、リンクに戻す。"""
    home.prepare_known_hosts(service)
    service.known_hosts_current.write_text("host ssh-ed25519 AAAA\n", encoding="utf-8")
    _ = home.use_known_hosts(service, "i-0123456789abcdef0")
    assert service.known_hosts_current.is_symlink()
    assert service.known_hosts_current.readlink() == Path("i-0123456789abcdef0")


def test_use_known_hosts_never_leaves_current_missing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """張り替えの途中でも current は前のインスタンスを指す。

    消してから作り直すと、その間に known_hosts を読んだ ssh がホストを未知として扱う。
    """
    _ = home.use_known_hosts(service, "i-0fedcba9876543210")
    seen: list[Path] = []
    real = Path.symlink_to

    def watch(self: Path, target: str, target_is_directory: bool = False) -> None:
        seen.append(service.known_hosts_current.readlink())
        real(self, target, target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", watch)
    _ = home.use_known_hosts(service, "i-0123456789abcdef0")
    assert seen == [Path("i-0fedcba9876543210")]
    assert service.known_hosts_current.readlink() == Path("i-0123456789abcdef0")


def conf_file(directory: Path) -> Path:
    """その場所に dev-main の設定ファイルを置く。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "dev-main.toml"
    path.write_text('KeyName = "temt"\n', encoding="utf-8")
    return path


def test_check_owner_passes_without_a_block(service: Service) -> None:
    home.check_owner(service)


def test_check_owner_passes_for_the_same_conf_dir(prepared: Path, service: Service) -> None:
    conf_file(prepared / ".config" / "temt")
    assert write_block(prepared) is True
    home.check_owner(service)


def test_check_owner_stops_when_the_default_conf_dir_still_has_the_service(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定の場所で apply したブロックは、既定の場所のものとみなす。"""
    conf_file(prepared / ".config" / "temt")
    assert write_block(prepared) is True
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    with pytest.raises(TemtError, match=r"~/\.config/temt/dev-main\.toml exists"):
        home.check_owner(service)


def test_check_owner_stops_when_a_moved_conf_dir_still_has_the_service(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    conf_file(prepared / "elsewhere")
    assert write_block(prepared) is True
    monkeypatch.delenv(CONF_DIR_ENV)
    with pytest.raises(TemtError, match=f"run apply with {CONF_DIR_ENV}=~/elsewhere"):
        home.check_owner(service)


def test_check_owner_takes_a_missing_file_as_a_move(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前の場所にサービスの設定ファイルが無ければ、場所を移したものとして進む。"""
    assert write_block(prepared) is True
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    home.check_owner(service)


def test_check_owner_compares_resolved_paths(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定の場所を別の書き方で指しても、同じ場所とみなす。"""
    conf_file(prepared / ".config" / "temt")
    assert write_block(prepared) is True
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / ".config" / "x" / ".." / "temt"))
    home.check_owner(service)


def test_check_owner_ignores_other_blocks(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """見るのはそのサービスのブロックの ProxyCommand だけである。"""
    conf_file(prepared / ".config" / "temt")
    write(
        "Host temt-dev-other\n"
        f"    ProxyCommand env {CONF_DIR_ENV}='/srv/x' ~/.local/bin/temt proxy --svc dev-other %p\n"
    )
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    home.check_owner(service)


def test_check_owner_takes_a_block_without_env_as_the_default(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env の無い ProxyCommand（手で書いたブロックなど）は、既定の場所のものとみなす。"""
    conf_file(prepared / ".config" / "temt")
    write("Host temt-dev-main\n    ProxyCommand ~/.local/bin/temt proxy --svc dev-main %p\n")
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    with pytest.raises(TemtError, match=r"~/\.config/temt/dev-main\.toml exists"):
        home.check_owner(service)


def test_check_owner_reads_the_conf_dir_next_to_the_ssh_dir(
    prepared: Path, service: Service
) -> None:
    """env に 2 つの変数が並んでいても、TEMT_CONF_DIR を読む。"""
    conf_file(prepared / "elsewhere")
    write(
        "Host temt-dev-main\n"
        f"    ProxyCommand env {CONF_DIR_ENV}='{prepared}/elsewhere' {SSH_DIR_ENV}='/srv/ssh' "
        "~/.local/bin/temt proxy --svc dev-main %p\n"
    )
    with pytest.raises(TemtError, match="~/elsewhere/dev-main.toml exists"):
        home.check_owner(service)


def test_check_owner_takes_only_the_ssh_dir_in_env_as_the_default(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env に TEMT_SSH_DIR だけがあれば、conf ディレクトリは既定である。"""
    conf_file(prepared / ".config" / "temt")
    write(
        "Host temt-dev-main\n"
        f"    ProxyCommand env {SSH_DIR_ENV}='/srv/ssh' ~/.local/bin/temt proxy --svc dev-main %p\n"
    )
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    with pytest.raises(TemtError, match=r"~/\.config/temt/dev-main\.toml exists"):
        home.check_owner(service)


def test_check_owner_passes_without_a_proxy_command(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf_file(prepared / ".config" / "temt")
    write("Host temt-dev-main\n    User ubuntu\n")
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    home.check_owner(service)


def test_check_owner_reads_a_host_line_with_other_names(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """他の名前と並べた Host 行のブロックも、temt のものである。"""
    conf_file(prepared / ".config" / "temt")
    write("Host temt-dev-main other\n    ProxyCommand ~/.local/bin/temt proxy --svc dev-main %p\n")
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    with pytest.raises(TemtError, match="another config directory"):
        home.check_owner(service)


def test_check_owner_asks_to_remove_one_when_both_places_have_the_file(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """両方の場所にあるとき、移すよう勧めると今の場所のファイルを上書きさせてしまう。"""
    conf_file(prepared / ".config" / "temt")
    assert write_block(prepared) is True
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    conf_file(prepared / "elsewhere")
    with pytest.raises(TemtError, match="and so does ~/elsewhere/dev-main.toml; remove one"):
        home.check_owner(service)


def moved_include(home_dir: Path, monkeypatch: pytest.MonkeyPatch, old: str) -> None:
    """~/.ssh/config の目印の次に old の行を置き、ssh ディレクトリを elsewhere に移す。"""
    user = home_dir / ".ssh" / "config"
    user.parent.mkdir(mode=0o700, exist_ok=True)
    user.write_text(f"{home.MARK}\n{old}\n", encoding="utf-8")
    monkeypatch.setenv(SSH_DIR_ENV, str(home_dir / "elsewhere"))
    home.prepare_dirs()


def test_write_include_leaves_out_hosts_the_new_config_has(
    prepared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """新しい config にも同じ Host があれば、そちらでつながるので挙げない。"""
    write("Host temt-dev-main\n    User ubuntu\nHost temt-dev-gone\n    User ubuntu\n")
    moved_include(prepared, monkeypatch, INCLUDE)
    assert write_block(prepared) is True
    _ = capsys.readouterr()
    assert include() == "Include ~/elsewhere/config"
    err = capsys.readouterr().err
    assert "ssh cannot reach temt-dev-gone there" in err
    assert "temt-dev-main" not in err


def test_write_include_leaves_out_patterns(
    prepared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ワイルドカードや否定は、つなぐ先の名前ではない。"""
    write("Host * !bastion\n    User me\nHost web?\n    User me\nHost web1\n    User me\n")
    moved_include(prepared, monkeypatch, INCLUDE)
    assert include() == "Include ~/elsewhere/config"
    assert capsys.readouterr().err.endswith("ssh cannot reach web1 there\n")


def test_write_include_is_quiet_for_the_same_file_written_differently(
    prepared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """前の行が、新しい config を別の書き方で指していれば、何も失わない。"""
    elsewhere = prepared / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (elsewhere / "config").write_text("Host temt-dev-main\n", encoding="utf-8")
    moved_include(prepared, monkeypatch, f"Include {prepared}/x/../elsewhere/config")
    assert include() == "Include ~/elsewhere/config"
    assert capsys.readouterr().err == ""


def test_write_include_reads_absolute_and_several_paths(
    prepared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 行に並んだパスを、絶対パスも ~/.ssh からの相対パスも、それぞれ見る。"""
    (prepared / "a").mkdir()
    (prepared / "a" / "config").write_text("Host from-a\n", encoding="utf-8")
    (prepared / ".ssh" / "b").write_text("Host from-b\n", encoding="utf-8")
    moved_include(prepared, monkeypatch, f"Include {prepared}/a/config b")
    assert include() == "Include ~/elsewhere/config"
    err = capsys.readouterr().err
    assert "no longer includes ~/a/config; ssh cannot reach from-a there" in err
    assert "no longer includes ~/.ssh/b; ssh cannot reach from-b there" in err


def test_write_include_goes_on_when_the_old_file_cannot_be_read(
    prepared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """警告のために読むだけなので、読めなくても張り替える。"""
    config().write_bytes(b"Host \xff\n")
    moved_include(prepared, monkeypatch, INCLUDE)
    assert include() == "Include ~/elsewhere/config"
    assert capsys.readouterr().err == ""


def test_check_owner_passes_when_both_places_name_the_same_file(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前の場所のファイルが今の場所のファイルへのリンクなら、実体は 1 つである。"""
    real = conf_file(prepared / "elsewhere")
    (prepared / ".config" / "temt").mkdir(parents=True)
    (prepared / ".config" / "temt" / "dev-main.toml").symlink_to(real)
    assert write_block(prepared) is True
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    home.check_owner(service)


def test_check_owner_passes_for_the_same_conf_dir_through_a_symlink(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf_file(prepared / ".config" / "temt")
    assert write_block(prepared) is True
    (prepared / "link").symlink_to(prepared / ".config" / "temt")
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "link"))
    home.check_owner(service)


def test_check_owner_stops_on_a_config_that_is_not_utf8(prepared: Path, service: Service) -> None:
    config().write_bytes(b"Host \xff\n")
    with pytest.raises(TemtError, match="~/.ssh/temt/config: not UTF-8"):
        home.check_owner(service)


def test_a_home_with_a_space_stays_out_of_the_proxy_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ホームのパスは ~ で書くので、空白を含んでも ProxyCommand も check_owner も壊れない。"""
    spaced = tmp_path / "a b"
    spaced.mkdir()
    monkeypatch.setenv("HOME", str(spaced))
    home.prepare_dirs()
    service = Service("dev-main")
    conf_file(spaced / ".config" / "temt")
    assert write_block(spaced) is True
    assert f"    ProxyCommand       {PROXY}\n" in read()
    monkeypatch.setenv(CONF_DIR_ENV, "~/elsewhere")
    with pytest.raises(TemtError, match=r"~/\.config/temt/dev-main\.toml exists"):
        home.check_owner(service)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search any directory")
def test_write_include_goes_on_when_the_old_file_cannot_be_searched(
    prepared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """前の Include の先を調べられなくても、張り替える。"""
    closed = prepared / "closed"
    closed.mkdir()
    (closed / "config").write_text("Host gone\n", encoding="utf-8")
    closed.chmod(0)
    try:
        moved_include(prepared, monkeypatch, f"Include {closed}/config")
        assert include() == "Include ~/elsewhere/config"
    finally:
        closed.chmod(0o700)
    assert capsys.readouterr().err == ""


def test_write_host_block_stops_on_a_config_that_is_not_utf8(prepared: Path) -> None:
    config().write_bytes(b"Host \xff\n")
    with pytest.raises(TemtError, match="~/.ssh/temt/config: not UTF-8"):
        write_block(prepared)


def test_write_include_stops_on_a_user_config_that_is_not_utf8(prepared: Path) -> None:
    _ = prepared
    write_user_config("")
    (Path.home() / ".ssh" / "config").write_bytes(b"Host \xff\n")
    with pytest.raises(TemtError, match="~/.ssh/config: not UTF-8"):
        include()


def test_the_home_as_the_conf_dir_goes_round_trip(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEMT_CONF_DIR=~ は ~/. と書かれ、check_owner がホームに戻して読む。"""
    monkeypatch.setenv(CONF_DIR_ENV, "~")
    conf_file(prepared)
    assert write_block(prepared) is True
    assert f"env {CONF_DIR_ENV}='~/.' " in read()
    monkeypatch.delenv(CONF_DIR_ENV)
    with pytest.raises(TemtError, match=f"run apply with {CONF_DIR_ENV}=~/\\."):
        home.check_owner(service)


@pytest.mark.parametrize("value", ["", "relative/conf"])
def test_check_owner_reads_an_edited_value_like_proxy(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """空の値は既定とみなし、絶対パスにならない値は手がかりにしない。"""
    conf_file(prepared / ".config" / "temt")
    write(
        "Host temt-dev-main\n"
        f"    ProxyCommand env {CONF_DIR_ENV}='{value}' ~/.local/bin/temt proxy --svc dev-main %p\n"
    )
    monkeypatch.setenv(CONF_DIR_ENV, str(prepared / "elsewhere"))
    if value:
        home.check_owner(service)
    else:
        with pytest.raises(TemtError, match=r"~/\.config/temt/dev-main\.toml exists"):
            home.check_owner(service)


def test_check_owner_stops_when_the_old_place_cannot_be_searched(
    prepared: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前の場所を調べられなければ、移したとはみなさずに止める。"""
    if os.geteuid() == 0:
        pytest.skip("root can search any directory")
    closed = prepared / "closed"
    conf_file(closed)
    monkeypatch.setenv(CONF_DIR_ENV, str(closed))
    assert write_block(prepared) is True
    monkeypatch.delenv(CONF_DIR_ENV)
    closed.chmod(0)
    try:
        with pytest.raises(TemtError, match="cannot check ~/closed/dev-main.toml"):
            home.check_owner(service)
    finally:
        closed.chmod(0o700)
