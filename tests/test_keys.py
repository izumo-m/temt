"""鍵の手元側のテスト。ssh-keygen は実際に呼ぶ。ssh -G は偽物に差し替えるか、手元の ssh に
仮の設定を読ませる。
"""

import os
import pwd
import shlex
import shutil
from pathlib import Path

import pytest

from temt import home, keys, proc
from temt.change import Change
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import SSH_DIR_ENV, Service, keys_dir

# ssh -G に尋ねるホスト
HOST = "temt-dev-main"
# 公開鍵のファイルの 1 行の、種類と本体
PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKfJ0gnyLzVY4oq0bQ1Zzv1g9f8bO4m7mI5x2kYpQXcP"


def make(path: Path, comment: str = "temt") -> Path:
    """鍵を 1 つ作る。"""
    keys.generate(path, comment)
    return path


def put(path: Path, text: str = f"{PUBLIC} me@host\n") -> Path:
    """ファイルを置く。無い親も作る。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_generate_makes_a_key_pair(home_dir: Path) -> None:
    path = make(keys_dir() / "temt")
    assert path.stat().st_mode & 0o777 == 0o600
    public = path.with_name("temt.pub").read_text(encoding="utf-8")
    assert public.startswith("ssh-ed25519 ")
    assert public.endswith(" temt\n")
    assert home_dir in path.parents


def test_generate_makes_missing_parents_private(home_dir: Path) -> None:
    """鍵の場所は、途中の親も 700 で作る。umask を緩めて、環境に依らず確かめる。"""
    previous = os.umask(0o022)
    try:
        path = make(home_dir / "a" / "b" / "temt")
    finally:
        _ = os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o600
    for parent in (home_dir / "a", home_dir / "a" / "b"):
        assert parent.stat().st_mode & 0o777 == 0o700


def test_generate_refuses_to_overwrite(home_dir: Path) -> None:
    _ = home_dir
    path = make(keys_dir() / "temt")
    with pytest.raises(TemtError, match="key already exists"):
        keys.generate(path, "temt")


def test_public_path_is_next_to_the_private_key() -> None:
    assert keys.public_path(Path("/a/b.key")) == Path("/a/b.key.pub")


def test_the_default_key_is_in_the_keys_dir(home_dir: Path) -> None:
    assert keys.default_path() == home_dir / ".ssh" / "temt" / "keys" / "temt"


def test_the_default_key_follows_the_ssh_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SSH_DIR_ENV, "/srv/temt/ssh")
    assert keys.default_path() == Path("/srv/temt/ssh/keys/temt")


# --- 既定の鍵の検出と作成 ---


def test_detect_makes_the_key_when_neither_file_exists(home_dir: Path) -> None:
    """作ることを、diff と同じ形で見せる。検出だけでは何も作らない。"""
    _ = home_dir
    found = keys.detect(keys.default_path())
    assert found is not None
    assert found.path == keys.default_path()
    assert found.changes() == [Change("+", "key", "~/.ssh/temt/keys/temt", "ssh-keygen -t ed25519")]
    assert not keys_dir().exists()


def test_detect_needs_ssh_keygen_only_to_make_the_key(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh-keygen を確かめるのは、鍵を作るときだけである。"""
    _ = home_dir
    monkeypatch.setenv("PATH", "")
    with pytest.raises(TemtError, match=f"command not found: {keys.KEYGEN}"):
        _ = keys.detect(keys.default_path())
    put(keys.default_path(), "private\n")
    put(keys.public_path(keys.default_path()))
    assert keys.detect(keys.default_path()) is None


def test_detect_leaves_a_key_in_place(home_dir: Path) -> None:
    _ = home_dir
    path = make(keys.default_path())
    assert keys.detect(path) is None


def test_detect_stops_when_only_the_private_key_exists(home_dir: Path) -> None:
    """作り直すと秘密鍵を失うので、公開鍵の作り方を伝えて止まる。"""
    _ = home_dir
    path = make(keys.default_path())
    keys.public_path(path).unlink()
    with pytest.raises(TemtError) as caught:
        _ = keys.detect(path)
    assert str(caught.value) == (
        "~/.ssh/temt/keys/temt.pub does not exist; make it with "
        "'ssh-keygen -y -f ~/.ssh/temt/keys/temt > ~/.ssh/temt/keys/temt.pub'"
    )


def test_detect_stops_when_only_the_public_key_exists(home_dir: Path) -> None:
    """作り直すと公開鍵を黙って置き換えるので、消すよう伝えて止まる。"""
    _ = home_dir
    path = make(keys.default_path())
    path.unlink()
    with pytest.raises(TemtError) as caught:
        _ = keys.detect(path)
    assert str(caught.value) == (
        "~/.ssh/temt/keys/temt.pub exists without ~/.ssh/temt/keys/temt; "
        "remove it to create a new key"
    )


def test_ensure_makes_the_detected_key(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ = home_dir
    path = keys.default_path()
    keys.ensure(keys.detect(path))
    assert path.stat().st_mode & 0o777 == 0o600
    assert keys.public_path(path).read_text(encoding="utf-8").endswith(" temt\n")
    assert keys_dir().stat().st_mode & 0o777 == 0o700
    assert capsys.readouterr().out.endswith(" created the key ~/.ssh/temt/keys/temt\n")
    # 次の検出では、もう作らない
    assert keys.detect(path) is None


def test_ensure_without_a_difference_does_nothing(
    home_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = home_dir
    monkeypatch.setenv("PATH", "")
    keys.ensure(None)
    assert not keys_dir().exists()
    assert capsys.readouterr() == ("", "")


# --- ssh -G が出す IdentityFile の展開 ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("/srv/keys/temt", "/srv/keys/temt", id="absolute"),
        pytest.param("~/.ssh/id_ed25519", "{home}/.ssh/id_ed25519", id="tilde"),
        pytest.param("~", "{home}", id="tilde alone"),
        pytest.param("~//.ssh/k", "{home}/.ssh/k", id="tilde and two slashes"),
        pytest.param("%d/.ssh/id_ed25519", "{home}/.ssh/id_ed25519", id="home token"),
        pytest.param("/srv/keys/%u", "/srv/keys/{user}", id="local user"),
        pytest.param("/srv/keys/%r", "/srv/keys/ubuntu", id="remote user"),
        pytest.param("/srv/keys/50%%off", "/srv/keys/50%off", id="percent"),
        pytest.param("/srv/%%u/%u-%r.key", "/srv/%u/{user}-ubuntu.key", id="mixed"),
        # ~ を展開するのは先頭だけである
        pytest.param("/srv/~/k", "/srv/~/k", id="tilde inside"),
    ],
)
def test_expand_reads_what_ssh_reads(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    """%u は、ssh と同じくパスワードデータベースの名前にする。環境変数 USER には依らない。"""
    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("LOGNAME", "someone-else")
    user = pwd.getpwuid(os.getuid()).pw_name
    assert keys._expand(value, "ubuntu") == Path(expected.format(home=home_dir, user=user))


@pytest.mark.parametrize(
    "value",
    ["%h/key", "~/.ssh/%C", "/srv/%i/key", "/srv/key%", "${HOME}/.ssh/key", "/srv/${KEYS}"],
)
def test_expand_refuses_what_it_cannot_read(value: str) -> None:
    """ほかのトークンと ${NAME} は、temt には展開できないので、パスで書くよう伝える。"""
    with pytest.raises(TemtError) as caught:
        _ = keys._expand(value, "ubuntu")
    assert str(caught.value) == f"temt cannot expand IdentityFile {value}; write it as a path"


def test_expand_shows_a_value_safely() -> None:
    with pytest.raises(TemtError) as caught:
        _ = keys._expand("~/\x1b[2J%h", "ubuntu")
    assert str(caught.value) == "temt cannot expand IdentityFile ~/?[2J%h; write it as a path"


# --- ssh が使う鍵の公開鍵 ---


def answer(monkeypatch: pytest.MonkeyPatch, *lines: str) -> list[list[str]]:
    """ssh -G の出力を lines にし、渡されたコマンドラインを控える。"""
    called: list[list[str]] = []

    def run(argv: list[str], stdin: str | None = None) -> str:
        assert stdin is None
        called.append(argv)
        return "".join(f"{line}\n" for line in lines)

    monkeypatch.setattr(proc, "run", run)
    return called


def test_offered_takes_the_first_key_with_a_public_key(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh が試す順に見て、隣に .pub がある最初の鍵を使う。秘密鍵の有無は問わない。"""
    put(home_dir / "first")
    put(home_dir / "second.pub")
    put(home_dir / "third.pub")
    called = answer(
        monkeypatch,
        "hostname temt-dev-main",
        "identityfile ~/first",
        "user ubuntu",
        "identityfile ~/second",
        "identityfile ~/third",
        "port 22",
    )
    assert keys.offered(HOST) == ("ubuntu", home_dir / "second.pub")
    assert called == [["ssh", "-G", HOST]]


def test_offered_expands_the_login_user(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """%r は ssh -G が出すログインするユーザーにする。行の順に依らない。"""
    put(home_dir / "keys" / "admin.pub")
    _ = answer(monkeypatch, "identityfile ~/keys/%r", "user admin")
    assert keys.offered(HOST) == ("admin", home_dir / "keys" / "admin.pub")


def test_offered_prefers_the_user_given_by_ssh(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh のコマンドラインで変えたユーザー（%r）は ssh -G に見えないので、-l で渡す。"""
    put(home_dir / "keys" / "admin.pub")
    called = answer(monkeypatch, "identityfile ~/keys/%r", "user admin")
    assert keys.offered(HOST, "admin") == ("admin", home_dir / "keys" / "admin.pub")
    assert called == [["ssh", "-G", "-l", "admin", HOST]]


def test_offered_takes_an_identity_file_that_is_a_public_key(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """秘密鍵を ssh-agent に置き、IdentityFile に .pub を書く使い方では、それを送る。"""
    put(home_dir / "agent.pub")
    _ = answer(monkeypatch, "user ubuntu", "identityfile ~/agent.pub")
    assert keys.offered(HOST) == ("ubuntu", home_dir / "agent.pub")


def test_offered_takes_a_path_with_a_space(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh -G は値を引用符で囲まずに出す。行の最初の空白の後ろが値である。"""
    put(home_dir / "my keys" / "temt.pub")
    _ = answer(monkeypatch, "user ubuntu", "identityfile ~/my keys/temt")
    assert keys.offered(HOST) == ("ubuntu", home_dir / "my keys" / "temt.pub")


def test_offered_stops_without_a_public_key(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """秘密鍵があっても .pub が無ければ、送るものが無い。試した値を挙げて止まる。"""
    put(home_dir / "first")
    _ = answer(monkeypatch, "user ubuntu", "identityfile ~/first", "identityfile %d/second")
    with pytest.raises(TemtError) as caught:
        _ = keys.offered(HOST)
    assert str(caught.value) == (
        f"no public key for 'ssh {HOST}': no .pub next to its IdentityFile (~/first, %d/second); "
        f"put the .pub next to the key, or delete IdentityFile from the Host {HOST} block "
        "and run 'temt apply' to use the default key"
    )


def test_offered_skips_a_path_under_another_users_home(
    home_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """~user/ は展開しない。相対パスとして扱われ、.pub が無いので飛ばされる。"""
    monkeypatch.chdir(tmp_path)
    put(home_dir / "second.pub")
    _ = answer(monkeypatch, "user ubuntu", "identityfile ~root/key", "identityfile ~/second")
    assert keys.offered(HOST) == ("ubuntu", home_dir / "second.pub")


def test_offered_stops_without_an_identity_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = answer(monkeypatch, "user ubuntu")
    with pytest.raises(TemtError, match=r"no \.pub next to its IdentityFile \(none\); "):
        _ = keys.offered(HOST)


def test_offered_stops_without_the_user(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(home_dir / "first.pub")
    _ = answer(monkeypatch, "identityfile ~/first")
    with pytest.raises(TemtError) as caught:
        _ = keys.offered(HOST)
    assert str(caught.value) == f"'ssh -G {HOST}' did not tell the user"


def test_offered_stops_at_an_identity_file_it_cannot_expand(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """展開できない値は、後ろに使える鍵があっても、ssh と同じ順に読めないので止まる。"""
    put(home_dir / "second.pub")
    _ = answer(monkeypatch, "user ubuntu", "identityfile %h/first", "identityfile ~/second")
    with pytest.raises(TemtError, match="temt cannot expand IdentityFile %h/first"):
        _ = keys.offered(HOST)


def test_offered_tells_why_ssh_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh -G が失敗すれば、ssh が言う理由を添えて止まる。"""
    bin_dir = tmp_path / "bin"
    put(bin_dir / "ssh", "#!/bin/sh\necho 'bad configuration options' >&2\nexit 255\n")
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    with pytest.raises(TemtError) as caught:
        _ = keys.offered(HOST)
    assert str(caught.value) == "ssh: bad configuration options"


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs ssh")
def test_offered_reads_what_ssh_reads_from_the_host_block(
    home_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply が書く Host ブロックを、手元の ssh -G に読ませて、既定の鍵を得る。

    ssh は ~/.ssh/config を、HOME ではなくログインユーザーのホームから読むので、仮の設定を
    -F で渡す ssh を PATH の先頭に置く。ssh -G は ~ もトークンも展開せずに出すので、
    ブロックの前に置いた、展開して初めて無いと分かる鍵は飛ばされる。
    """
    home.prepare_dirs()
    edit = home.detect_host_block(Service("dev-main"), Conf())
    assert home.write_edit(edit)
    config = f"Host {HOST}\n    IdentityFile %d/.ssh/missing\n\n" + edit.after
    use_config(tmp_path, monkeypatch, config + "\nHost *\n    IdentityFile ~/.ssh/id_ed25519\n")
    _ = make(keys.default_path())
    _ = make(home_dir / ".ssh" / "id_ed25519")
    assert keys.offered(HOST) == ("ubuntu", keys.public_path(keys.default_path()))


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs ssh")
def test_offered_reads_the_block_for_the_user_given_by_ssh(
    home_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh のコマンド行で変えたユーザーの Match user ブロックも、ssh と同じく読む。"""
    use_config(
        tmp_path,
        monkeypatch,
        f"Host {HOST}\n    IdentityFile ~/a\n\nMatch user admin\n    IdentityFile ~/admin\n",
    )
    put(home_dir / "admin.pub")
    assert keys.offered(HOST, "admin") == ("admin", home_dir / "admin.pub")


def use_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str) -> None:
    """手元の ssh に、config を -F で読ませる ssh を PATH の先頭に置く。"""
    path = put(tmp_path / "ssh-config", config)
    real = shutil.which("ssh")
    assert real is not None
    bin_dir = tmp_path / "bin"
    put(bin_dir / "ssh", f'#!/bin/sh\nexec {shlex.quote(real)} -F {shlex.quote(str(path))} "$@"\n')
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


# --- 公開鍵のファイル ---


def test_read_public_drops_the_comment(home_dir: Path) -> None:
    path = put(home_dir / "k.pub", f"{PUBLIC} me@host and more\n")
    assert keys.read_public(path) == PUBLIC


def test_read_public_takes_the_first_line_that_is_not_blank(home_dir: Path) -> None:
    path = put(home_dir / "k.pub", f"\n  \n{PUBLIC}\nssh-rsa AAAA other\n")
    assert keys.read_public(path) == PUBLIC


def test_read_public_of_a_key_that_ssh_keygen_made(home_dir: Path) -> None:
    path = make(home_dir / "k", comment="user@host")
    line = keys.public_path(path).read_text(encoding="utf-8")
    assert keys.read_public(keys.public_path(path)) == " ".join(line.split()[:2])


def test_read_public_drops_a_comment_that_is_not_utf8(home_dir: Path) -> None:
    """コメントの UTF-8 として読めないバイトは、送らないので妨げにならない。"""
    path = home_dir / "k.pub"
    path.write_bytes(PUBLIC.encode() + b" user@\xff\xfehost\n")
    assert keys.read_public(path) == PUBLIC


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\n\n", id="blank"),
        pytest.param(b"ssh-ed25519\n", id="one word"),
        pytest.param("ssh-ed25519 あ c\n".encode(), id="not ascii"),
        pytest.param(b"ssh-ed25519 AA\xff c\n", id="not utf8"),
    ],
)
def test_read_public_of_something_that_is_not_a_public_key(home_dir: Path, content: bytes) -> None:
    _ = home_dir
    path = keys_dir() / "temt.pub"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with pytest.raises(TemtError) as caught:
        _ = keys.read_public(path)
    assert str(caught.value) == "not a public key: ~/.ssh/temt/keys/temt.pub"
