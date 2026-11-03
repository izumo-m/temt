"""app deploy と app remove のテスト。

AWS は Stubber で、ssh は偽物に差し替える。インスタンスの上で動くスクリプトは、手元の
bash で一時ディレクトリに対して動かして確かめる。
"""

import fcntl
import io
import json
import re
import shutil
import subprocess
import tarfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    REGION,
    WHERE,
    Stubs,
    an_instance,
    apps_archive,
    deadline,
    identified,
    reservations,
    taken,
)

from temt import bake, conf, deploy, home, lock, proc, proxy, remote
from temt.instances import Instance
from temt.manifest import Manifest, read_manifest
from temt.msg import TemtError, UsageError
from temt.names import Service

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
TEMPLATES = {"LaunchTemplateNames": [TEMPLATE]}
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
SEARCH = {
    "Filters": [
        {"Name": "tag:temt:service", "Values": ["dev-main"]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        },
    ]
}
STATUS = f"/{NAME}/jupyter/status"
# インスタンスの上の deploy の記録の場所
RECORD = f"{INSTANCE}:/opt/temt/apps/jupyter/temt-deploy.json"
# deploy が書く記録の、オプションを除く項目
WRITTEN = {"user": "root", "deployed": "2026-09-27T03:04:05Z", "instance": INSTANCE}
# 記録に書く時刻
DEPLOYED = time.struct_time((2026, 9, 27, 3, 4, 5, 6, 270, 0))
MANIFEST = 'name = "Jupyter Lab"\nversion = "1.2"\ndescription = "notebooks"\n'
TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(err: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in err.splitlines()]


def make_app(where: Path, *, uninstall: bool = True) -> Path:
    """アプリのディレクトリを作る。"""
    where.mkdir(parents=True)
    (where / "manifest.toml").write_text(MANIFEST, encoding="utf-8")
    (where / "install").write_text("echo install\n", encoding="utf-8")
    if uninstall:
        (where / "uninstall").write_text("echo uninstall\n", encoding="utf-8")
    (where / "files").mkdir()
    (where / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    return where


@dataclass
class Ssh:
    """proc.stream、proc.read、proc.capture の代わり。呼ばれたコマンドを控え、決めた結果を返す。

    proc.read はつながった先のインスタンス ID を読むときに使い、reached を返す。
    reached が None なら、つながらなかったものとする。proc.capture はインスタンスの上の
    アプリを読むときに使い、apps を返す。
    """

    codes: list[int] = field(default_factory=list)
    calls: list[tuple[list[str], bytes | None]] = field(default_factory=list)
    reached: str | None = INSTANCE
    asked: list[list[str]] = field(default_factory=list)
    # インスタンスにいるユーザー。id -u で確かめられる
    users: tuple[str, ...] = ("ubuntu",)
    # 呼ばれたときの環境。呼ばれた順に並ぶ
    envs: list[Mapping[str, str] | None] = field(default_factory=list)
    # インスタンスの上のアプリ（apps_archive）と、読み取りの終了コード
    apps: dict[str, dict[str, str | bytes]] = field(default_factory=dict)
    read_code: int = 0
    # 読み取りのコマンドライン
    reads: list[list[str]] = field(default_factory=list)

    def stream(
        self, argv: list[str], data: bytes | None = None, *, env: Mapping[str, str] | None = None
    ) -> int:
        self.calls.append((argv, data))
        self.envs.append(env)
        return self.codes.pop(0) if self.codes else 0

    def read(self, argv: list[str], *, env: Mapping[str, str] | None = None) -> tuple[int, str]:
        self.asked.append(argv)
        self.envs.append(env)
        if argv[3].startswith("id -u -- "):
            return (0, "1000\n") if argv[3].split()[-1] in self.users else (1, "")
        if self.reached is None:
            return 255, ""
        # ログインシェルが何かを書いても、ID だけを取り出す
        return 0, f"Welcome\n{self.reached}\n"

    def capture(
        self, argv: list[str], *, env: Mapping[str, str] | None = None
    ) -> tuple[int, bytes]:
        self.reads.append(argv)
        self.envs.append(env)
        if self.reached is None:
            return 255, b""
        if self.read_code:
            return self.read_code, b""
        return 0, apps_archive(self.apps)


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> Ssh:
    fake = Ssh()
    monkeypatch.setattr(proc, "stream", fake.stream)
    monkeypatch.setattr(proc, "read", fake.read)
    monkeypatch.setattr(proc, "capture", fake.capture)
    monkeypatch.setattr(proc, "require", lambda command: command)
    monkeypatch.setattr(deploy, "gmtime", lambda: DEPLOYED)
    return fake


def installed(
    ssh: Ssh, record: dict[str, Any] | str | None = None, manifest: str = MANIFEST
) -> None:
    """インスタンスに前のアプリを置く。record は記録で、文字列ならそのまま書き、None なら
    記録が無い（前の temt が入れた）。
    """
    files: dict[str, str | bytes] = {"manifest.toml": manifest}
    if record is not None:
        if not isinstance(record, str):
            record = json.dumps(record, ensure_ascii=False)
        files["temt-deploy.json"] = record
    ssh.apps["jupyter"] = files


def sent(ssh: Ssh, name: str) -> tarfile.TarInfo | None:
    """送った tar.gz のすぐ下のファイル name の項目。無ければ None。"""
    (_, data), *_ = ssh.calls
    assert data is not None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        try:
            return tar.getmember(f"./{name}")
        except KeyError:
            return None


def sent_text(ssh: Ssh, name: str) -> str:
    """送った tar.gz のすぐ下のファイル name の中身。"""
    (_, data), *_ = ssh.calls
    assert data is not None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        found = tar.extractfile(f"./{name}")
        assert found is not None
        return found.read().decode()


def sent_record(ssh: Ssh) -> dict[str, Any]:
    """送った deploy の記録。"""
    return json.loads(sent_text(ssh, "temt-deploy.json"))


def ready(stubs: Stubs, state: str = "running") -> None:
    """手順 2。テンプレートがあり、インスタンスが 1 台ある。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )
    stubs.ec2.add_response("describe_instances", reservations(an_instance(INSTANCE, state)), SEARCH)


# --- アプリの在り処 ---


def test_locate_takes_a_path_when_it_has_a_slash(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = make_app(home_dir / "src" / "jupyter")
    monkeypatch.chdir(home_dir / "src")
    # 知らせに使うので、絶対パスにして返す
    assert deploy.locate("./jupyter") == ("jupyter", made)
    assert deploy.locate("./jupyter/") == ("jupyter", made)
    assert deploy.locate("~/src/jupyter") == ("jupyter", made)
    assert deploy.locate("../src/./jupyter") == ("jupyter", made)


def test_locate_names_a_symlink_by_its_own_name(home_dir: Path) -> None:
    made = make_app(home_dir / "jupyter-2.0")
    (home_dir / "jupyter").symlink_to(made)
    assert deploy.locate(f"{home_dir}/jupyter")[0] == "jupyter"


def test_locate_stops_when_the_path_is_not_a_directory(home_dir: Path) -> None:
    with pytest.raises(TemtError, match="no app directory: ./nothing"):
        _ = deploy.locate("./nothing")


def test_locate_uses_the_app_that_comes_with_temt(home_dir: Path, no_bundled_apps: Path) -> None:
    bundled = make_app(no_bundled_apps / "idle-shutdown")
    mine = make_app(home_dir / ".config" / "temt" / "apps" / "jupyter")
    assert deploy.locate("jupyter") == ("jupyter", mine)
    assert deploy.locate("idle-shutdown") == ("idle-shutdown", bundled)


def test_locate_prefers_the_app_in_the_conf_directory(
    home_dir: Path, no_bundled_apps: Path
) -> None:
    _ = make_app(no_bundled_apps / "idle-shutdown")
    mine = make_app(home_dir / ".config" / "temt" / "apps" / "idle-shutdown")
    assert deploy.locate("idle-shutdown") == ("idle-shutdown", mine)


def test_locate_skips_a_broken_link_for_the_app_that_comes_with_temt(
    home_dir: Path, no_bundled_apps: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundled = make_app(no_bundled_apps / "idle-shutdown")
    link = home_dir / ".config" / "temt" / "apps" / "idle-shutdown"
    link.parent.mkdir(parents=True)
    link.symlink_to(home_dir / "gone")
    assert deploy.locate("idle-shutdown") == ("idle-shutdown", bundled)
    assert capsys.readouterr().err == (
        "temt: warning: ~/.config/temt/apps/idle-shutdown is not a directory "
        "(a broken link?); using the app that comes with temt\n"
    )


def test_locate_tells_a_broken_link(home_dir: Path) -> None:
    link = home_dir / ".config" / "temt" / "apps" / "hello"
    link.parent.mkdir(parents=True)
    link.symlink_to(home_dir / "gone")
    with pytest.raises(TemtError) as caught:
        _ = deploy.locate("hello")
    assert str(caught.value) == (
        "no app hello in ~/.config/temt/apps or among the apps that come with temt; "
        "~/.config/temt/apps/hello is not a directory (a broken link?)"
    )


def test_locate_follows_a_link_in_the_conf_directory(home_dir: Path) -> None:
    made = make_app(home_dir / "dotfiles" / "jupyter")
    link = home_dir / ".config" / "temt" / "apps" / "jupyter"
    link.parent.mkdir(parents=True)
    link.symlink_to(made)
    assert deploy.locate("jupyter") == ("jupyter", link)


def test_locate_suggests_a_close_app_id(home_dir: Path, no_bundled_apps: Path) -> None:
    _ = make_app(no_bundled_apps / "idle-shutdown")
    _ = make_app(home_dir / ".config" / "temt" / "apps" / "jupyter")
    with pytest.raises(TemtError) as caught:
        _ = deploy.locate("idel-shutdown")
    assert str(caught.value) == (
        "no app idel-shutdown in ~/.config/temt/apps or among the apps that come with temt; "
        "did you mean idle-shutdown? (apps: idle-shutdown, jupyter)"
    )


def test_locate_lists_no_apps_when_there_are_none(home_dir: Path) -> None:
    with pytest.raises(TemtError) as caught:
        _ = deploy.locate("hello")
    assert str(caught.value) == (
        "no app hello in ~/.config/temt/apps or among the apps that come with temt"
    )


# --- manifest.toml ---


def test_read_manifest(home_dir: Path) -> None:
    made = make_app(home_dir / "jupyter")
    assert read_manifest(made) == Manifest(
        name="Jupyter Lab", version="1.2", description="notebooks"
    )
    (made / "manifest.toml").write_text('name = "J"\nversion = "1"\n', encoding="utf-8")
    assert read_manifest(made) == Manifest(name="J", version="1")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (None, r"no manifest\.toml in ~/jupyter$"),
        ('name = "J"\n', r"manifest\.toml: version is missing$"),
        ('name = "J"\nversion = 1\n', r"manifest\.toml: version must be a non-empty string$"),
        ('name = ""\nversion = "1"\n', r"manifest\.toml: name must be a non-empty string$"),
        ('name = "J"\nversion = "1"\nid = "j"\n', r"manifest\.toml: unknown key: id$"),
        # 端末を乱す文字は、誤りの文言でも ? に置き換える
        ('name = "J"\nversion = "1"\n"k\\u001b" = "j"\n', r"manifest\.toml: unknown key: k\?$"),
        ('name = "J"\nversion = "1"\nuser = "a\\u001bb"\n', r"manifest\.toml: user .*: a\?b$"),
        ("name = \n", r"manifest\.toml: "),
    ],
)
def test_read_manifest_rejects(home_dir: Path, text: str | None, message: str) -> None:
    made = make_app(home_dir / "jupyter")
    if text is None:
        (made / "manifest.toml").unlink()
    else:
        (made / "manifest.toml").write_text(text, encoding="utf-8")
    with pytest.raises(TemtError, match=message):
        _ = read_manifest(made)


def test_archive_holds_the_directory_as_root(home_dir: Path) -> None:
    made = make_app(home_dir / "jupyter")
    with tarfile.open(fileobj=io.BytesIO(deploy.archive(made)), mode="r:gz") as tar:
        members = {info.name: info for info in tar.getmembers()}
    assert set(members) == {
        ".",
        "./manifest.toml",
        "./install",
        "./uninstall",
        "./files",
        "./files/a.conf",
    }
    assert {(info.uid, info.gid, info.uname, info.gname) for info in members.values()} == {
        (0, 0, "root", "root")
    }


# --- インスタンスの上のスクリプト。手元の bash で動かす ---


# sudo の代わり。-n、-H と -u <ユーザー> を捨て、残りをそのまま実行する
FAKE_SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
    case "$1" in
        -u) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
exec "$@"
"""


def bash(script: str, *args: str, data: bytes | None = None) -> int:
    """スクリプトを手元の bash で動かし、終了コードを返す。sudo は通さない。"""
    return subprocess.run(
        ["bash", "-c", script, "temt", *args], input=data, capture_output=True, check=False
    ).returncode


def test_send_script_replaces_the_old_directory(home_dir: Path) -> None:
    root = home_dir / "opt"
    old = root / "jupyter"
    old.mkdir(parents=True)
    (old / "stale").write_text("x", encoding="utf-8")
    made = make_app(home_dir / "jupyter")

    assert bash(deploy.SEND_SCRIPT, str(root), "jupyter", data=deploy.archive(made)) == 0

    assert sorted(path.name for path in root.iterdir()) == ["jupyter"]
    assert sorted(path.name for path in old.iterdir()) == [
        "files",
        "install",
        "manifest.toml",
        "uninstall",
    ]
    assert old.stat().st_mode & 0o777 == 0o755


def test_send_script_keeps_the_old_directory_when_it_fails(home_dir: Path) -> None:
    root = home_dir / "opt"
    old = root / "jupyter"
    old.mkdir(parents=True)
    (old / "stale").write_text("x", encoding="utf-8")

    assert bash(deploy.SEND_SCRIPT, str(root), "jupyter", data=b"not a tar.gz") != 0

    # 一時ディレクトリは残らない
    assert sorted(path.name for path in root.iterdir()) == ["jupyter"]
    assert [path.name for path in old.iterdir()] == ["stale"]


def test_send_script_puts_the_old_directory_back_when_it_cannot_move_the_new_one(
    home_dir: Path,
) -> None:
    """古いものを退けたあとで新しいものを入れられなければ、古いものを戻す。

    戻さないと、退けた先は次の deploy がはじめに消す一時ディレクトリなので、古いものが
    失われる。新しいものを入れる mv だけを失敗させる。
    """
    bin_dir = home_dir / "bin"
    bin_dir.mkdir()
    fake_mv = bin_dir / "mv"
    fake_mv.write_text(
        '#!/bin/sh\ncase "$1 $2" in\n"-T "*.old.*) ;;\n"-T "*) exit 1 ;;\nesac\n'
        f'exec {shutil.which("mv")} "$@"\n',
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    root = home_dir / "opt"
    old = root / "jupyter"
    old.mkdir(parents=True)
    (old / "stale").write_text("x", encoding="utf-8")
    made = make_app(home_dir / "jupyter")

    done = subprocess.run(
        ["bash", "-c", deploy.SEND_SCRIPT, "temt", str(root), "jupyter"],
        input=deploy.archive(made),
        capture_output=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
    )

    assert done.returncode == 1
    # 一時ディレクトリも退けた先も残らない
    assert sorted(path.name for path in root.iterdir()) == ["jupyter"]
    assert [path.name for path in old.iterdir()] == ["stale"]


def test_send_script_makes_the_first_directory(home_dir: Path) -> None:
    root = home_dir / "opt"
    made = make_app(home_dir / "jupyter")
    assert bash(deploy.SEND_SCRIPT, str(root), "jupyter", data=deploy.archive(made)) == 0
    assert (root / "jupyter" / "install").is_file()


def test_check_script_tells_what_is_there(home_dir: Path) -> None:
    check = deploy.CHECK_SCRIPT.format(
        absent=deploy.ABSENT_CODE, no_uninstall=deploy.NO_UNINSTALL_CODE
    )
    with_uninstall = make_app(home_dir / "a")
    without = make_app(home_dir / "b", uninstall=False)
    assert bash(check, str(with_uninstall)) == 0
    assert bash(check, str(without)) == deploy.NO_UNINSTALL_CODE
    assert bash(check, str(home_dir / "c")) == deploy.ABSENT_CODE


@pytest.mark.parametrize("user", ["root", "ubuntu"])
@pytest.mark.parametrize("code", [0, 7])
def test_scripts_pass_on_the_exit_code(
    home_dir: Path, shell: dict[str, str], user: str, code: int
) -> None:
    """install と uninstall の終了コードが ssh の側まで届き、uninstall が失敗すれば
    ディレクトリを残す。"""
    directory = make_app(home_dir / "jupyter")
    for script in ("install", "uninstall"):
        (directory / script).write_text(f"exit {code}\n", encoding="utf-8")
    assert over_ssh(remote.remote(deploy.INSTALL_SCRIPT, str(directory), user), shell) == code
    assert over_ssh(remote.remote(deploy.UNINSTALL_SCRIPT, str(directory), user), shell) == code
    assert directory.exists() == (code != 0)


# --- deploy の流れ ---


@pytest.fixture
def jupyter(home_dir: Path) -> Path:
    """conf ディレクトリに置いた利用者のアプリ。"""
    return make_app(home_dir / ".config" / "temt" / "apps" / "jupyter")


def test_deploy_sends_installs_and_records(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ready(aws_stubs)

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    (send, data), (install, _) = ssh.calls
    assert ssh.asked == [["ssh", "-T", NAME, "sudo -n cat /var/lib/cloud/data/instance-id"]]
    assert send[:3] == ["ssh", "-T", NAME]
    assert "tar -xzf -" in send[3]
    assert send[3].endswith(" temt /opt/temt/apps jupyter")
    # 送るのはアプリのディレクトリの tar.gz である。gzip は時刻を含むので、中身で比べる
    assert data is not None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert "./install" in tar.getnames()
    assert install == ["ssh", "-T", NAME, install[3]]
    assert install[3].startswith("sudo -n bash -c ")
    assert install[3].endswith(" temt /opt/temt/apps/jupyter root")
    # 前の記録はインスタンスから、そのアプリだけを読む
    (read,) = ssh.reads
    assert read[3].endswith(" temt /opt/temt/apps jupyter")
    # 記録はディレクトリと一緒に、root の持ち物として送る
    assert sent_record(ssh) == WRITTEN
    info = sent(ssh, "temt-deploy.json")
    assert info is not None
    assert (info.mode, info.uid, info.gid, info.uname) == (0o644, 0, 0, "root")
    assert told(capsys.readouterr().out) == [
        "using jupyter from ~/.config/temt/apps/jupyter",
        f"checking that 'ssh {NAME}' reaches {INSTANCE}",
        f"sending jupyter to {INSTANCE}",
        f"running install of jupyter as root on {INSTANCE}",
        f"deployed jupyter 1.2 to {INSTANCE}; run 'temt bake --svc dev-main' to keep it in the "
        "next instance",
    ]


def test_deploy_and_remove_pass_the_region_and_the_profile_to_proxy(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """temt が実行する ssh には、決めたリージョンとプロファイルを proxy 向けの環境変数で渡す。

    利用者の環境はそのまま渡し、プロファイルを選んでいなければ渡さない。
    """
    monkeypatch.setenv("TEMT_LOGIN", "direct")
    monkeypatch.setenv(proxy.PASSED_PROFILE_ENV, "stale")
    ready(aws_stubs)
    deploy.run(configured, "jupyter", user=None, baking=False, yes=False)
    aws_stubs.done()
    # つながる先を確かめる、前の記録を読む、送る、install を実行する
    assert len(ssh.envs) == 4
    for env in ssh.envs:
        assert env is not None
        assert env[proxy.PASSED_REGION_ENV] == REGION
        assert proxy.PASSED_PROFILE_ENV not in env
        assert env["TEMT_LOGIN"] == "direct"

    ssh.envs.clear()
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    monkeypatch.setattr(conf, "OPTION_REGION", "us-west-2")
    ready(aws_stubs)
    installed(ssh, {"user": "root"})
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})
    deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)
    aws_stubs.done()
    assert len(ssh.envs) == 4
    for env in ssh.envs:
        assert env is not None
        assert env[proxy.PASSED_REGION_ENV] == "us-west-2"
        assert env[proxy.PASSED_PROFILE_ENV] == "work"


def test_deploy_writes_the_record_of_this_instance(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    """AMI から来たアプリを入れ直すと、記録の instance はこのインスタンスになる。"""
    ready(aws_stubs)
    installed(ssh, {"user": "root", "deployed": "2026-09-01T00:00:00Z", "instance": OTHER})

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert sent_record(ssh) == WRITTEN


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (
            "pending",
            f"{INSTANCE} is pending; wait for 'temt start --svc dev-main' to finish "
            "and deploy again",
        ),
        (
            "stopped",
            f"{INSTANCE} is stopped; run 'temt start --svc dev-main' and deploy again",
        ),
        ("stopping", f"{INSTANCE} is stopping; run 'temt start --svc dev-main' and deploy again"),
    ],
)
def test_deploy_needs_a_running_instance(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, state: str, message: str
) -> None:
    ready(aws_stubs, state)

    with pytest.raises(TemtError, match=re.escape(message)):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert ssh.calls == []


def test_deploy_does_not_start_an_instance(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    aws_stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)

    with pytest.raises(
        TemtError, match=f"no instance for {NAME}; run 'temt start --svc dev-main' and deploy again"
    ):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert ssh.calls == []


def test_deploy_does_not_count_an_instance_that_is_shutting_down(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    """終了の途中のものは、無いものとして扱う。2 台とは数えず、running の方へ送る。"""
    aws_stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "shutting-down"), an_instance(INSTANCE, "running")),
        SEARCH,
    )
    ssh.codes = [255]

    with pytest.raises(TemtError, match=f"cannot send jupyter to {INSTANCE} "):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()


def test_deploy_stops_when_it_cannot_send(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    ready(aws_stubs)
    ssh.codes = [255]

    with pytest.raises(
        TemtError,
        match=f"cannot send jupyter to {INSTANCE} \\(exit 255\\); deploy again$",
    ):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    # install も記録も行わない
    aws_stubs.done()
    assert len(ssh.calls) == 1


def test_deploy_leaves_the_new_files_when_install_fails(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    ready(aws_stubs)
    ssh.codes = [0, 3]

    with pytest.raises(
        TemtError,
        match=f"install of jupyter failed on {INSTANCE} \\(exit 3\\); the files are in "
        "place; fix it and deploy again$",
    ):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=True)

    aws_stubs.done()


@dataclass
class Baking:
    """bake.check と bake.make の代わり。呼ばれたことを控える。"""

    made: list[str] = field(default_factory=list)

    def check(
        self, ec2: Any, service: Service, values: Any, one: Instance, *, command: str
    ) -> None:
        self.made.append("check")

    def make(self, ec2: Any, service: Service, one: Instance) -> None:
        self.made.append(f"make {one.id}")


@pytest.fixture
def baking(monkeypatch: pytest.MonkeyPatch) -> Baking:
    fake = Baking()
    monkeypatch.setattr(bake, "check", fake.check)
    monkeypatch.setattr(bake, "make", fake.make)
    return fake


def test_deploy_with_bake_asks_first_and_bakes_last(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    baking: Baking,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        "temt.msg.confirm", lambda question, *, yes: asked.append(f"{question()} {yes}")
    )
    ready(aws_stubs)
    identified(aws_stubs)

    deploy.run(configured, "jupyter", user=None, baking=True, yes=True)

    aws_stubs.done()
    assert baking.made == ["check", f"make {INSTANCE}"]
    assert asked == [
        f"deploy jupyter 1.2 to {INSTANCE} as root and bake {INSTANCE} (running) {WHERE}? True"
    ]
    out = capsys.readouterr().out
    assert f"deploy jupyter 1.2 to {INSTANCE} as root and bake {NAME}:\n" in out
    assert f"  stop {INSTANCE}\n" in out
    assert "run 'temt bake" not in out


def test_deploy_with_bake_does_not_bake_when_install_fails(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, baking: Baking
) -> None:
    ready(aws_stubs)
    ssh.codes = [0, 1]

    with pytest.raises(TemtError, match="install of jupyter failed"):
        deploy.run(configured, "jupyter", user=None, baking=True, yes=True)

    aws_stubs.done()
    assert baking.made == ["check"]


@pytest.mark.parametrize(
    ("manifest_user", "option", "user"),
    [
        (None, None, "root"),
        ("ubuntu", None, "ubuntu"),
        ("ubuntu", "app", "app"),
        (None, "app", "app"),
        ("ubuntu", "root", "root"),
    ],
)
def test_deploy_runs_install_as_the_user(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    manifest_user: str | None,
    option: str | None,
    user: str,
) -> None:
    """--user が manifest.toml の user に勝ち、どちらも無ければ root で実行する。"""
    if manifest_user is not None:
        (jupyter / "manifest.toml").write_text(
            MANIFEST + f'user = "{manifest_user}"\n', encoding="utf-8"
        )
    ssh.users = ("ubuntu", "app")
    ready(aws_stubs)

    deploy.run(configured, "jupyter", user=option, baking=False, yes=False)

    aws_stubs.done()
    (_, _), (install, _) = ssh.calls
    assert install[3].endswith(f" temt /opt/temt/apps/jupyter {user}")
    assert sent_record(ssh)["user"] == user
    # root でなければ、そのユーザーがいることを先に確かめる
    checked = [argv[3] for argv in ssh.asked if argv[3].startswith("id ")]
    assert checked == ([] if user == "root" else [f"id -u -- {user}"])


def test_deploy_tells_when_ssh_fails_while_checking_the_user(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = ssh.read

    def failing(argv: list[str], *, env: Mapping[str, str] | None = None) -> tuple[int, str]:
        return (255, "") if argv[3].startswith("id ") else read(argv, env=env)

    monkeypatch.setattr(proc, "read", failing)
    ready(aws_stubs)
    with pytest.raises(TemtError, match=f"^cannot reach {INSTANCE} with 'ssh {NAME}'; nothing"):
        deploy.run(configured, "jupyter", user="ubuntu", baking=False, yes=False)
    aws_stubs.done()
    assert ssh.calls == []


def test_deploy_stops_before_sending_when_the_user_is_not_there(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    ready(aws_stubs)
    with pytest.raises(TemtError, match=f"^no user nobody2 on {INSTANCE} \\(exit 1\\); nothing"):
        deploy.run(configured, "jupyter", user="nobody2", baking=False, yes=False)
    aws_stubs.done()
    assert ssh.calls == []


@pytest.mark.parametrize("given", ["root", None], ids=["option", "manifest"])
def test_deploy_refuses_another_user_while_a_record_is_left(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, given: str | None
) -> None:
    """インスタンスに記録が残っている間は、記録の user と違うユーザーで入れ直さない。

    新しいユーザーの uninstall は、前のユーザーの install がしたことを片づけない。
    manifest.toml の user を変えた版も同じ。
    """
    if given is None:
        (jupyter / "manifest.toml").write_text(MANIFEST + 'user = "nobody"\n', encoding="utf-8")
    ready(aws_stubs)
    installed(ssh, {"user": "ubuntu", "instance": OTHER})
    shown = given or "nobody"
    with pytest.raises(TemtError) as caught:
        deploy.run(configured, "jupyter", user=given, baking=False, yes=False)
    aws_stubs.done()
    assert str(caught.value) == (
        f"jupyter was deployed as ubuntu ({RECORD}), not {shown}; deploy it as ubuntu, or "
        "remove it first to change the user; nothing was changed"
    )
    assert ssh.calls == []


def test_read_manifest_checks_the_user(home_dir: Path) -> None:
    made = make_app(home_dir / "jupyter")
    (made / "manifest.toml").write_text(MANIFEST + 'user = "Ubuntu"\n', encoding="utf-8")
    with pytest.raises(TemtError, match="user must start with a lowercase letter"):
        _ = read_manifest(made)


# --- app remove の流れ ---


def test_remove_uninstalls_and_deletes_the_directory(
    aws_stubs: Stubs, configured: Service, ssh: Ssh, capsys: pytest.CaptureFixture[str]
) -> None:
    ready(aws_stubs)
    installed(ssh, {"instance": INSTANCE})
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})

    deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    (check, _), (uninstall, _) = ssh.calls
    assert check[3].endswith(" temt /opt/temt/apps/jupyter")
    # user の無い記録なら root で実行する
    assert uninstall[3].endswith(" temt /opt/temt/apps/jupyter root")
    captured = capsys.readouterr()
    assert captured.err == (
        f"temt: warning: no user in the deploy record {RECORD}; running uninstall of jupyter as "
        "root; give the user with --user if install ran as another user\n"
    )
    # 記録はディレクトリと一緒に消えるので、SSM では状態だけを消す
    assert told(captured.out) == [
        f"checking that 'ssh {NAME}' reaches {INSTANCE}",
        f"running uninstall of jupyter as root on {INSTANCE}",
        f"/opt/temt/apps/jupyter on {INSTANCE}: deleted",
        f"parameter {STATUS}: deleted",
        f"removed jupyter from {INSTANCE}; run 'temt bake --svc dev-main' to remove it from the "
        "next instance too",
    ]


@pytest.mark.parametrize(
    ("recorded_user", "option", "user"), [("ubuntu", None, "ubuntu"), ("ubuntu", "app", "app")]
)
def test_remove_runs_uninstall_as_the_user(
    aws_stubs: Stubs,
    configured: Service,
    ssh: Ssh,
    recorded_user: str,
    option: str | None,
    user: str,
) -> None:
    """--user が、記録にある install を実行したユーザーに勝つ。"""
    ssh.users = ("ubuntu", "app")
    ready(aws_stubs)
    installed(ssh, {"user": recorded_user})
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})

    deploy.remove(configured, "jupyter", user=option, baking=False, yes=False)

    aws_stubs.done()
    (_, _), (uninstall, _) = ssh.calls
    assert uninstall[3].endswith(f" temt /opt/temt/apps/jupyter {user}")
    assert [argv[3] for argv in ssh.asked if argv[3].startswith("id ")] == [f"id -u -- {user}"]


def test_remove_stops_on_a_bad_user_in_the_record(
    aws_stubs: Stubs, configured: Service, ssh: Ssh
) -> None:
    ready(aws_stubs)
    installed(ssh, {"user": "x\x1b[2J; rm -rf /"})
    with pytest.raises(TemtError) as caught:
        deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)
    # 記録の値は端末に出さない
    assert str(caught.value) == (
        f"the deploy record {RECORD} has a bad user; give the user with --user"
    )
    aws_stubs.done()
    assert len(ssh.calls) == 1


@pytest.mark.parametrize(("manifest_user", "user"), [(None, "root"), ("ubuntu", "ubuntu")])
def test_remove_without_a_record_runs_uninstall_as_the_user_of_the_manifest(
    aws_stubs: Stubs,
    configured: Service,
    ssh: Ssh,
    capsys: pytest.CaptureFixture[str],
    manifest_user: str | None,
    user: str,
) -> None:
    """前の temt が入れたアプリには記録が無い。インスタンスの manifest.toml の user、それも
    無ければ root で実行し、そのことを警告する。
    """
    manifest = MANIFEST + (f'user = "{manifest_user}"\n' if manifest_user else "")
    installed(ssh, manifest=manifest)
    ready(aws_stubs)
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})
    deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)
    aws_stubs.done()
    (_, _), (uninstall, _) = ssh.calls
    assert uninstall[3].endswith(f" temt /opt/temt/apps/jupyter {user}")
    assert capsys.readouterr().err == (
        f"temt: warning: no deploy record {RECORD}; running uninstall of jupyter as {user}; "
        "give the user with --user if install ran as another user\n"
    )


def test_remove_warns_once_about_an_unreadable_record(
    aws_stubs: Stubs, configured: Service, ssh: Ssh, capsys: pytest.CaptureFixture[str]
) -> None:
    """記録は 1 度だけ読むので、読めない記録の警告も 1 度だけ出す。"""
    installed(ssh, "not json")
    ready(aws_stubs)
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})
    deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)
    aws_stubs.done()
    (_, _), (uninstall, _) = ssh.calls
    assert uninstall[3].endswith(" temt /opt/temt/apps/jupyter root")
    assert capsys.readouterr().err.count(f"cannot read the deploy record {RECORD}") == 1


def test_remove_tells_how_to_delete_the_status_it_cannot_delete(
    aws_stubs: Stubs, configured: Service, ssh: Ssh
) -> None:
    """アプリは外れているので、残った状態は app unset で消すよう伝える。"""
    ready(aws_stubs)
    installed(ssh, {"user": "root"})
    aws_stubs.ssm.add_client_error(
        "delete_parameter",
        service_error_code="AccessDeniedException",
        http_status_code=400,
        expected_params={"Name": STATUS},
    )
    with pytest.raises(
        TemtError,
        match=f"; jupyter is off {INSTANCE} but {STATUS} is left; delete it with "
        "'temt app unset --svc dev-main jupyter status'$",
    ):
        deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)
    aws_stubs.done()


def test_remove_with_a_user_does_not_read_the_user_in_the_record(
    aws_stubs: Stubs, configured: Service, ssh: Ssh, capsys: pytest.CaptureFixture[str]
) -> None:
    ssh.users = ("app",)
    ready(aws_stubs)
    installed(ssh, {"instance": INSTANCE})
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})
    deploy.remove(configured, "jupyter", user="app", baking=False, yes=False)
    aws_stubs.done()
    (_, _), (uninstall, _) = ssh.calls
    assert uninstall[3].endswith(" temt /opt/temt/apps/jupyter app")
    assert "warning" not in capsys.readouterr().err


def test_remove_stops_when_the_recorded_user_is_not_there(
    aws_stubs: Stubs, configured: Service, ssh: Ssh
) -> None:
    ssh.users = ()
    ready(aws_stubs)
    installed(ssh, {"user": "ubuntu"})
    with pytest.raises(TemtError, match=f"^no user ubuntu on {INSTANCE} \\(exit 1\\); nothing"):
        deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)
    aws_stubs.done()
    assert len(ssh.calls) == 1


def test_remove_with_bake_tells_the_user_in_the_question(
    aws_stubs: Stubs,
    configured: Service,
    ssh: Ssh,
    baking: Baking,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setattr("temt.msg.confirm", lambda question, *, yes: asked.append(question()))
    ready(aws_stubs)
    identified(aws_stubs)
    installed(ssh, {"user": "ubuntu"})
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})
    deploy.remove(configured, "jupyter", user=None, baking=True, yes=True)
    aws_stubs.done()
    assert asked == [
        f"remove jupyter from {INSTANCE} as ubuntu and bake {INSTANCE} (running) {WHERE}?"
    ]


def test_remove_leaves_everything_when_uninstall_fails(
    aws_stubs: Stubs, configured: Service, ssh: Ssh
) -> None:
    ready(aws_stubs)
    installed(ssh, {"user": "root"})
    ssh.codes = [0, 2]

    with pytest.raises(
        TemtError,
        match=f"uninstall of jupyter or deleting /opt/temt/apps/jupyter failed on {INSTANCE} "
        "\\(exit 2\\); fix it and remove again$",
    ):
        deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()


def test_remove_with_bake_bakes_after_removing(
    aws_stubs: Stubs,
    configured: Service,
    ssh: Ssh,
    baking: Baking,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("temt.msg.confirm", lambda question, *, yes: None)
    ready(aws_stubs)
    installed(ssh, {"user": "root"})
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})

    deploy.remove(configured, "jupyter", user=None, baking=True, yes=True)

    aws_stubs.done()
    assert baking.made == ["check", f"make {INSTANCE}"]


def test_deploy_stops_before_any_change_when_ssh_cannot_connect(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, baking: Baking
) -> None:
    """初めてつなぐインスタンスのホスト鍵を確かめられないときなど。確認も出さない。"""
    ready(aws_stubs)
    ssh.reached = None

    with pytest.raises(
        TemtError,
        match=f"cannot reach {INSTANCE} with 'ssh {NAME}' \\(exit 255\\); nothing was changed",
    ):
        deploy.run(configured, "jupyter", user=None, baking=True, yes=False)

    aws_stubs.done()
    assert baking.made == []
    assert ssh.calls == []


@pytest.mark.parametrize("command", ["deploy", "remove"])
def test_it_stops_when_ssh_reaches_another_instance(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ssh の proxy には --profile が届かないので、別のアカウントのものにつながりうる。"""
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    ready(aws_stubs)
    ssh.reached = "i-0fedcba9876543210"

    with pytest.raises(
        TemtError,
        match=f"^'ssh {NAME}' reached i-0fedcba9876543210, not {INSTANCE}; nothing was changed$",
    ):
        if command == "deploy":
            deploy.run(configured, "jupyter", user=None, baking=False, yes=False)
        else:
            deploy.remove(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert ssh.calls == []


def _start(command: str, service: Service, *, baking: bool) -> None:
    """deploy か app remove を、確認を求めずに動かす。"""
    if command == "deploy":
        deploy.run(service, "jupyter", user=None, baking=baking, yes=True)
    else:
        deploy.remove(service, "jupyter", user=None, baking=baking, yes=True)


@pytest.mark.parametrize("command", ["deploy", "remove"])
def test_it_stops_when_another_command_holds_the_lock(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, command: str
) -> None:
    """ロックはテンプレートを確かめるより先に取る。取れなければ待たずに終わる。"""
    held = home.prepare_known_hosts(configured) / lock.LOCK_NAME
    with held.open("a", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            deadline(5.0),
            pytest.raises(TemtError, match=f"another temt command is running for {NAME}$"),
        ):
            _start(command, configured, baking=False)

    # 応答を 1 つも積んでいないので、AWS を読んでいれば Stubber が失敗する
    aws_stubs.done()
    assert ssh.calls == []
    assert ssh.asked == []


@pytest.mark.parametrize("command", ["deploy", "remove"])
def test_bake_stops_before_anything_when_image_id_is_written(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """--bake で ImageId を書いていれば、AMI は固定で焼けないので、ロックも取らず、AWS も
    インスタンスにも触れずに引数の誤りで止める。"""
    monkeypatch.setattr(lock, "hold", lambda service: pytest.fail("the lock was taken"))
    text = configured.conf_path.read_text(encoding="utf-8")
    configured.conf_path.write_text(
        text.replace(
            "[launch-template]\n", '[launch-template]\nImageId = "ami-0123456789abcdef0"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(UsageError) as caught:
        _start(command, configured, baking=True)

    aws_stubs.done()
    assert str(caught.value) == (
        "launch-template.ImageId in ~/.config/temt/dev-main.toml fixes the AMI; "
        "remove the line to bake"
    )
    assert ssh.calls == []


@pytest.mark.parametrize("command", ["deploy", "remove"])
def test_it_holds_the_lock_until_it_has_baked(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    baking: Baking,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """ssh でインスタンスに触れている間も、--bake で焼く間も、ロックを持っている。"""
    held: list[bool] = []
    stream = ssh.stream
    make = baking.make

    def probe(
        argv: list[str], data: bytes | None = None, *, env: Mapping[str, str] | None = None
    ) -> int:
        held.append(not taken(configured))
        return stream(argv, data, env=env)

    def making(ec2: Any, service: Service, one: Instance) -> None:
        held.append(not taken(configured))
        make(ec2, service, one)

    monkeypatch.setattr(proc, "stream", probe)
    monkeypatch.setattr(bake, "make", making)
    monkeypatch.setattr("temt.msg.confirm", lambda question, *, yes: None)
    ready(aws_stubs)
    if command == "remove":
        installed(ssh, {"user": "root"})
        aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": STATUS})

    with deadline(5.0):
        _start(command, configured, baking=True)

    aws_stubs.done()
    # 送ると install（app remove なら、確かめると uninstall）の ssh と、焼く手順
    assert held == [True, True, True]
    assert baking.made == ["check", f"make {INSTANCE}"]
    # 終わればロックは離す
    assert taken(configured)


@pytest.mark.parametrize("command", ["deploy", "remove"])
def test_bake_stops_before_any_change_when_the_template_does_not_match(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    no_confirm: None,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """--bake で bake の確かめが通らなければ、確認も、送ることも uninstall もせずに止める。"""

    def check(ec2: Any, service: Service, values: Any, one: Instance, *, command: str) -> None:
        raise TemtError("the launch template does not match")

    monkeypatch.setattr(bake, "check", check)
    ready(aws_stubs)
    if command == "remove":
        installed(ssh, {"user": "root"})

    with pytest.raises(TemtError, match="^the launch template does not match$"):
        _start(command, configured, baking=True)

    aws_stubs.done()
    # app remove は、ディレクトリを確かめただけ
    assert len(ssh.calls) == (0 if command == "deploy" else 1)


def test_deploy_with_bake_changes_nothing_when_cancelled(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    baking: Baking,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cancel(question: str, *, yes: bool) -> None:
        raise TemtError("cancelled")

    monkeypatch.setattr("temt.msg.confirm", cancel)
    ready(aws_stubs)

    with pytest.raises(TemtError, match="cancelled"):
        deploy.run(configured, "jupyter", user=None, baking=True, yes=False)

    # ssh はつながった先を確かめ、前の記録を読んだだけで、何も送らない
    aws_stubs.done()
    assert ssh.calls == []
    assert len(ssh.asked) == 1
    assert len(ssh.reads) == 1


@pytest.mark.parametrize("reset", [False, True])
def test_deploy_stops_when_it_cannot_read_the_record(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, reset: bool
) -> None:
    """インスタンスから前の記録が読めなければ、--reset でも、送る前に止める。

    記録が無いのか分からないまま入れ替えると、前のユーザーを確かめられないからである。
    """
    ready(aws_stubs)
    ssh.read_code = 2

    with pytest.raises(
        TemtError,
        match=f"^cannot read the apps in /opt/temt/apps on {INSTANCE} \\(exit 2\\); nothing was "
        "changed$",
    ):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False, reset=reset)

    aws_stubs.done()
    assert ssh.calls == []


def test_deploy_lists_two_instances(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    aws_stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(INSTANCE, "running"), an_instance("i-1", "pending")),
        SEARCH,
    )

    with pytest.raises(TemtError, match="2 instances for temt-dev-main; .* and deploy again"):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False)
    aws_stubs.done()
    assert ssh.calls == []


def test_archive_keeps_only_the_owner_able_to_write(home_dir: Path) -> None:
    """WSL の /mnt/c の下のファイルは 777 に見える。root の持ち物として誰でも書けないようにする。"""
    made = make_app(home_dir / "jupyter")
    (made / "install").chmod(0o4777)
    (made / "files").chmod(0o777)
    with tarfile.open(fileobj=io.BytesIO(deploy.archive(made)), mode="r:gz") as tar:
        modes = {info.name: info.mode for info in tar.getmembers()}
    assert modes["./install"] == 0o755
    assert modes["./files"] == 0o755


def test_archive_follows_every_link(home_dir: Path) -> None:
    """アプリのディレクトリ自身も中のリンクも辿り、その先の中身を普通のファイルとして入れる。"""
    made = make_app(home_dir / "jupyter-2.0")
    shared = home_dir / "shared"
    shared.mkdir()
    (shared / "b.conf").write_text("b\n", encoding="utf-8")
    (made / "manifest.toml").rename(home_dir / "shared.toml")
    (made / "manifest.toml").symlink_to(home_dir / "shared.toml")
    (made / "link").symlink_to("files/a.conf")
    (made / "outside").symlink_to(shared)
    (home_dir / "jupyter").symlink_to(made)
    with tarfile.open(fileobj=io.BytesIO(deploy.archive(home_dir / "jupyter")), mode="r:gz") as tar:
        members = {info.name: info for info in tar.getmembers()}
        assert not any(info.issym() for info in members.values())
        assert members["."].isdir()
        assert members["./outside"].isdir()
        assert members["./outside"].uname == "root"
        contents = {
            name: extracted.read()
            for name in ("./manifest.toml", "./link", "./outside/b.conf")
            if (extracted := tar.extractfile(members[name])) is not None
        }
    assert contents == {
        "./manifest.toml": MANIFEST.encode(),
        "./link": b"a\n",
        "./outside/b.conf": b"b\n",
    }


def test_archive_stops_on_a_broken_link(home_dir: Path) -> None:
    made = make_app(home_dir / "jupyter")
    (made / "gone").symlink_to("nowhere")
    with pytest.raises(OSError):
        _ = deploy.archive(made)


def test_send_script_clears_what_an_earlier_run_left(home_dir: Path) -> None:
    root = home_dir / "opt"
    (root / ".jupyter.abc123").mkdir(parents=True)
    (root / ".jupyter.old.def456").mkdir()
    (root / ".jupyter-lab.xyz").mkdir()
    made = make_app(home_dir / "jupyter")

    assert bash(deploy.SEND_SCRIPT, str(root), "jupyter", data=deploy.archive(made)) == 0

    # 他のアプリ（名前の頭が同じもの）の残骸には触れない
    assert sorted(path.name for path in root.iterdir()) == [".jupyter-lab.xyz", "jupyter"]


def test_remote_commands_pass_through_the_shell(home_dir: Path) -> None:
    """ssh はコマンドラインをログインシェルに渡す。引用符や $ を含むパスでも崩れない。"""
    bin_dir = home_dir / "bin"
    bin_dir.mkdir()
    fake_sudo = bin_dir / "sudo"
    fake_sudo.write_text(FAKE_SUDO, encoding="utf-8")
    fake_sudo.chmod(0o755)
    root = home_dir / "o'p $x"
    made = make_app(home_dir / "jupyter")
    done = subprocess.run(
        ["bash", "-c", remote.remote(deploy.SEND_SCRIPT, str(root), "jupyter")],
        input=deploy.archive(made),
        capture_output=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert (root / "jupyter" / "install").is_file()


@pytest.fixture
def no_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """確認を呼べば失敗させる。確認の前に止まることを確かめる。"""

    def fail(question: str, *, yes: bool) -> None:
        raise AssertionError(f"asked: {question}")

    monkeypatch.setattr("temt.msg.confirm", fail)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (
            deploy.NO_UNINSTALL_CODE,
            f"jupyter has no uninstall on {INSTANCE}; temt cannot remove it$",
        ),
        (deploy.ABSENT_CODE, f"jupyter is not deployed on {INSTANCE}: no /opt/temt/apps/jupyter$"),
        (255, f"cannot check /opt/temt/apps/jupyter on {INSTANCE} \\(exit 255\\)$"),
    ],
)
def test_remove_stops_before_asking(
    aws_stubs: Stubs,
    configured: Service,
    ssh: Ssh,
    baking: Baking,
    no_confirm: None,
    code: int,
    message: str,
) -> None:
    """uninstall が無いとき、ディレクトリが無いとき、確かめられないときは、確認も焼くことも
    せず、何も消さずに止める。

    読むだけの確かめは、bake の確かめより先に済ませる。
    """
    ready(aws_stubs)
    ssh.codes = [code]

    with pytest.raises(TemtError, match=message):
        deploy.remove(configured, "jupyter", user=None, baking=True, yes=False)

    aws_stubs.done()
    assert baking.made == []
    # 確かめただけで、uninstall は実行しない
    assert len(ssh.calls) == 1


@pytest.fixture
def shell(home_dir: Path) -> dict[str, str]:
    """ssh が渡すコマンドラインを、手元の bash で動かすための環境。sudo は -n を捨てて実行する。"""
    bin_dir = home_dir / "bin"
    bin_dir.mkdir()
    fake_sudo = bin_dir / "sudo"
    fake_sudo.write_text(FAKE_SUDO, encoding="utf-8")
    fake_sudo.chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin"}


def over_ssh(command: str, env: dict[str, str], data: bytes | None = None) -> int:
    """ssh のログインシェルの代わりに bash でコマンドラインを動かす。"""
    return subprocess.run(
        ["bash", "-c", command], input=data, capture_output=True, env=env, check=False
    ).returncode


@pytest.mark.parametrize(("user", "sudo"), [("root", None), ("ubuntu", "-n -H -u ubuntu bash")])
def test_install_script_runs_in_the_app_directory_as_the_user(
    home_dir: Path, shell: dict[str, str], user: str, sudo: str | None
) -> None:
    """root なら内側の sudo を通さず、ほかのユーザーなら sudo -n -H -u で実行する。
    カレントディレクトリはアプリのディレクトリにする。
    """
    log = home_dir / "sudo.log"
    inner = home_dir / "bin" / "sudo"
    # 外側の sudo（-n bash -c ...）のほかは、受け取った引数を控える
    inner.write_text(
        f'#!/bin/sh\nif [ "$1" != -n ] || [ "$2" != bash ]; then echo "$*" >> "{log}"; fi\n'
        + FAKE_SUDO.split("\n", 1)[1],
        encoding="utf-8",
    )
    directory = make_app(home_dir / "jupyter")
    (directory / "install").write_text(f'pwd > "{home_dir}/pwd"\n', encoding="utf-8")
    assert over_ssh(remote.remote(deploy.INSTALL_SCRIPT, str(directory), user), shell) == 0
    assert (home_dir / "pwd").read_text(encoding="utf-8") == f"{directory}\n"
    logged = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert logged == ([] if sudo is None else [f"{sudo} {directory}/install"])


def test_install_check_and_uninstall_commands_pass_through_the_shell(
    home_dir: Path, shell: dict[str, str]
) -> None:
    directory = make_app(home_dir / "o'p $x" / "jupyter")
    (directory / "install").write_text("touch installed\n", encoding="utf-8")
    check = deploy.CHECK_SCRIPT.format(
        absent=deploy.ABSENT_CODE, no_uninstall=deploy.NO_UNINSTALL_CODE
    )
    assert over_ssh(remote.remote(deploy.INSTALL_SCRIPT, str(directory), "root"), shell) == 0
    assert (directory / "installed").is_file()
    assert over_ssh(remote.remote(check, str(directory)), shell) == 0
    assert over_ssh(remote.remote(deploy.UNINSTALL_SCRIPT, str(directory), "root"), shell) == 0
    assert not directory.exists()
    assert over_ssh(remote.remote(check, str(directory)), shell) == deploy.ABSENT_CODE


# --- オプション ---

OPTIONS_MANIFEST = (
    MANIFEST
    + """
[options.port]
default = "8888"
description = "the port to listen on"
pattern = '[0-9]+'

[options.greeting]
default = "hello"
description = "the greeting"
"""
)


@pytest.fixture
def jupyter_options(jupyter: Path) -> Path:
    """オプションを宣言する、conf ディレクトリに置いた利用者のアプリ。"""
    (jupyter / "manifest.toml").write_text(OPTIONS_MANIFEST, encoding="utf-8")
    return jupyter


def test_deploy_passes_the_options_to_install(
    aws_stubs: Stubs,
    configured: Service,
    jupyter_options: Path,
    ssh: Ssh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """宣言したすべてのオプションを既定で埋めて渡し、記録には指定した値だけを書く。"""
    ready(aws_stubs)

    deploy.run(
        configured,
        "jupyter",
        user=None,
        baking=False,
        yes=False,
        given={"greeting": "こんにちは 世界"},
    )

    aws_stubs.done()
    assert sent_text(ssh, "temt-options.env") == (
        "# Written by temt deploy: the options of jupyter 1.2.\n"
        "TEMT_OPTION_PORT=8888\n"
        "TEMT_OPTION_GREETING='こんにちは 世界'\n"
    )
    info = sent(ssh, "temt-options.env")
    assert info is not None
    assert (info.mode, info.uid, info.gid, info.uname) == (0o644, 0, 0, "root")
    assert sent_record(ssh) == {**WRITTEN, "options": {"greeting": "こんにちは 世界"}}
    lines = told(capsys.readouterr().out)
    assert lines[2:4] == ["option port=8888 (default)", "option greeting='こんにちは 世界' (given)"]
    assert lines[4] == f"sending jupyter to {INSTANCE}"


def test_deploy_sends_no_option_file_for_an_app_without_options(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    ready(aws_stubs)

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert sent(ssh, "temt-options.env") is None


def test_deploy_keeps_the_options_of_the_last_deploy(
    aws_stubs: Stubs,
    configured: Service,
    jupyter_options: Path,
    ssh: Ssh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """インスタンスの記録のオプションを引き継ぎ、<名前>= はその名前を既定に戻す。

    AMI から来たアプリなら、焼いたときのオプションを引き継ぐことになる。
    """
    ready(aws_stubs)
    installed(
        ssh, {"user": "root", "instance": OTHER, "options": {"port": "9999", "greeting": "やあ"}}
    )

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False, given={"greeting": None})

    aws_stubs.done()
    assert "TEMT_OPTION_PORT=9999\nTEMT_OPTION_GREETING=hello\n" in sent_text(
        ssh, "temt-options.env"
    )
    lines = told(capsys.readouterr().out)
    assert lines[2:4] == ["option port=9999 (kept)", "option greeting=hello (default)"]
    assert sent_record(ssh) == {**WRITTEN, "options": {"port": "9999"}}


def test_deploy_with_reset_keeps_no_option(
    aws_stubs: Stubs, configured: Service, jupyter_options: Path, ssh: Ssh
) -> None:
    ready(aws_stubs)
    installed(ssh, {"user": "root", "options": {"port": "9999"}})

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False, reset=True)

    aws_stubs.done()
    assert "TEMT_OPTION_PORT=8888\n" in sent_text(ssh, "temt-options.env")


def test_deploy_drops_an_option_the_app_no_longer_declares(
    aws_stubs: Stubs,
    configured: Service,
    jupyter_options: Path,
    ssh: Ssh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ready(aws_stubs)
    installed(ssh, {"user": "root", "options": {"old": "x y", "port": "9999"}})

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert (
        "temt: warning: dropping old='x y' of the deploy record; jupyter 1.2 does not declare it"
        in told(capsys.readouterr().err)
    )
    assert sent_record(ssh)["options"] == {"port": "9999"}


def test_deploy_stops_on_a_kept_value_the_app_does_not_accept(
    aws_stubs: Stubs, configured: Service, jupyter_options: Path, ssh: Ssh
) -> None:
    ready(aws_stubs)
    installed(ssh, {"user": "root", "options": {"port": "http"}})

    with pytest.raises(
        TemtError,
        match=r"^the deploy record has port=http, which jupyter 1\.2 does not accept; give "
        r"port=<value>, or port= for the default; nothing was changed$",
    ):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert ssh.calls == []


def test_deploy_takes_a_new_value_over_a_kept_one_it_does_not_accept(
    aws_stubs: Stubs, configured: Service, jupyter_options: Path, ssh: Ssh
) -> None:
    ready(aws_stubs)
    installed(ssh, {"user": "root", "options": {"port": "http"}})

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False, given={"port": "80"})

    aws_stubs.done()
    assert sent_record(ssh)["options"] == {"port": "80"}


@pytest.mark.parametrize("reset", [False, True])
def test_deploy_stops_when_it_cannot_read_the_options(
    aws_stubs: Stubs, configured: Service, jupyter_options: Path, ssh: Ssh, reset: bool
) -> None:
    """読めない記録から引き継ぐと黙って既定に戻るので止める。--reset なら引き継がずに進む。"""
    ready(aws_stubs)
    installed(ssh, {"user": "root", "options": "port=9999"})
    if reset:
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False, reset=True)
        aws_stubs.done()
        return

    with pytest.raises(
        TemtError,
        match=f"^cannot read the options in the deploy record {RECORD}; give the options again "
        "with --reset; nothing was changed$",
    ):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    assert ssh.calls == []


def test_deploy_of_an_app_without_options_goes_on_with_an_unreadable_record(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    """オプションを宣言しないアプリでは、引き継ぐものが無いので止めない。"""
    ready(aws_stubs)
    installed(ssh, "[]")

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ({"prot": "1"}, r"^jupyter has no option prot; did you mean port\? \(options: port, "),
        ({"port": "http"}, r"^jupyter: bad value for port: http \(the port to listen on\)$"),
        ({"port": "a b"}, r"^jupyter: bad value for port: 'a b' "),
    ],
)
def test_deploy_checks_the_options_before_any_change(
    aws_stubs: Stubs,
    configured: Service,
    jupyter_options: Path,
    ssh: Ssh,
    given: dict[str, str | None],
    message: str,
) -> None:
    """宣言と照らすのは、AWS にもインスタンスにも触る前である。"""
    with pytest.raises(TemtError, match=message):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False, given=given)

    aws_stubs.done()
    assert ssh.asked == []


def test_deploy_tells_an_app_without_options(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh
) -> None:
    with pytest.raises(TemtError, match="^jupyter has no options; it does not take port$"):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False, given={"port": "1"})


@pytest.mark.parametrize("given", [{}, {"token": None}])
def test_deploy_needs_an_option_without_a_default(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, given: dict[str, str | None]
) -> None:
    (jupyter / "manifest.toml").write_text(
        MANIFEST + '[options.token]\ndescription = "the token"\n', encoding="utf-8"
    )
    if not given:
        ready(aws_stubs)
        message = "^jupyter needs the option token; give token=<value>; nothing was changed$"
    else:
        message = "^jupyter: token has no default; give token=<value>$"

    with pytest.raises(TemtError, match=message):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False, given=given)

    aws_stubs.done()
    assert ssh.calls == []


@pytest.mark.parametrize("name", ["temt-options.env", "temt-deploy.json"])
def test_deploy_refuses_an_app_with_a_file_temt_writes(
    aws_stubs: Stubs, configured: Service, jupyter: Path, ssh: Ssh, name: str
) -> None:
    (jupyter / name).write_text("", encoding="utf-8")

    with pytest.raises(
        TemtError,
        match=rf"^~/\.config/temt/apps/jupyter/{re.escape(name)}: temt writes this file; "
        "rename it$",
    ):
        deploy.run(configured, "jupyter", user=None, baking=False, yes=False)


def test_read_manifest_reads_the_options(home_dir: Path) -> None:
    made = make_app(home_dir / "jupyter")
    (made / "manifest.toml").write_text(OPTIONS_MANIFEST, encoding="utf-8")

    found = read_manifest(made).options

    assert list(found) == ["port", "greeting"]
    assert found["port"].default == "8888"
    assert found["port"].description == "the port to listen on"
    assert found["port"].accepts("80")
    # 値の全体が一致すること
    assert not found["port"].accepts("80a")
    assert found["greeting"].pattern is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('options = "x"\n', r"options must be tables such as \[options\.<name>\]$"),
        ("[options]\nport = 1\n", r"options\.port must be a table$"),
        ('[options.Port]\ndescription = "p"\n', r"the option name must start .*: Port$"),
        ("[options.port]\n", r"options\.port: description is missing$"),
        ('[options.port]\ndescription = ""\n', r"options\.port: description must be a non-empty"),
        (
            '[options.port]\ndescription = "a\\nb"\n',
            r"description must not contain control characters",
        ),
        ('[options.port]\ndescription = "p"\nx = 1\n', r"options\.port: unknown key: x$"),
        # 端末を乱す文字は、誤りの文言でも ? に置き換える
        ('[options."a\\u001bb"]\ndescription = "p"\n', r"the option name must start .*: a\?b$"),
        (
            '[options.port]\ndescription = "p"\n"x\\u001b" = 1\n',
            r"options\.port: unknown key: x\?$",
        ),
        ('[options.port]\ndescription = "p"\ndefault = 1\n', r"default must be a string$"),
        (
            '[options.port]\ndescription = "p"\ndefault = "a\\tb"\n',
            r"default must not contain control characters",
        ),
        ('[options.port]\ndescription = "p"\npattern = ""\n', r"pattern must be a non-empty"),
        ('[options.port]\ndescription = "p"\npattern = "("\n', r"options\.port: bad pattern: "),
        (
            '[options.port]\ndescription = "p"\npattern = "[0-9]+"\ndefault = "x"\n',
            r"options\.port: the default x does not match the pattern$",
        ),
    ],
)
def test_read_manifest_rejects_options(home_dir: Path, text: str, message: str) -> None:
    made = make_app(home_dir / "jupyter")
    (made / "manifest.toml").write_text(MANIFEST + text, encoding="utf-8")

    with pytest.raises(TemtError, match=r"^~/jupyter/manifest\.toml: .*" + message):
        _ = read_manifest(made)


def test_deploy_drops_unreadable_options_of_an_app_without_options(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ready(aws_stubs)
    installed(ssh, {"user": "root", "options": "x"})

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert (
        f"temt: warning: dropping the options of the deploy record {RECORD} that cannot be "
        "read; jupyter 1.2 declares no options"
    ) in told(capsys.readouterr().err)


def test_deploy_hides_what_disturbs_the_terminal_in_the_version(
    aws_stubs: Stubs,
    configured: Service,
    jupyter: Path,
    ssh: Ssh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """manifest.toml の版は、TOML の \\u の形で ESC も書けるので、知らせでは ? にする。"""
    (jupyter / "manifest.toml").write_text(
        'name = "Jupyter Lab"\nversion = "1.2\\u001b[2J"\n', encoding="utf-8"
    )
    ready(aws_stubs)

    deploy.run(configured, "jupyter", user=None, baking=False, yes=False)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[-1].startswith("deployed jupyter 1.2?[2J to ")
