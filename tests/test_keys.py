"""鍵の手元側のテスト。ssh-keygen を実際に呼ぶ。"""

import os
import subprocess
from pathlib import Path

import pytest

from temt import keys
from temt.msg import TemtError
from temt.names import keys_dir


def make(path: Path, comment: str = "temt") -> Path:
    """鍵を 1 つ作る。"""
    keys.generate(path, comment)
    return path


def test_generate_makes_a_key_pair(home_dir: Path) -> None:
    path = make(keys_dir() / "temt")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_name("temt.pub").exists()
    assert keys.public_key(path).startswith("ssh-ed25519 ")
    assert keys.public_key(path).endswith(" temt")
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


def fingerprint_of(path: Path) -> str:
    """秘密鍵から、その鍵のフィンガープリントを得る。"""
    return keys.fingerprint(keys.public_key(path))


def test_fingerprint_is_what_ssh_keygen_shows(home_dir: Path) -> None:
    _ = home_dir
    path = make(keys_dir() / "temt")
    shown = subprocess.run(
        ["ssh-keygen", "-l", "-E", "sha256", "-f", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[1]
    assert fingerprint_of(path) == shown
    assert shown.startswith("SHA256:")


def test_fingerprint_follows_the_private_key_not_a_stale_public_key(home_dir: Path) -> None:
    """隣の .pub が別の鍵のものでも、秘密鍵の側の値になる。"""
    _ = home_dir
    mine = make(keys_dir() / "temt")
    other = make(keys_dir() / "other")
    want = fingerprint_of(mine)
    mine.with_name("temt.pub").write_text(
        other.with_name("other.pub").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert fingerprint_of(mine) == want
    assert fingerprint_of(mine) != fingerprint_of(other)


def test_fingerprints_of_different_keys_differ(home_dir: Path) -> None:
    _ = home_dir
    one = fingerprint_of(make(keys_dir() / "one"))
    assert one != fingerprint_of(make(keys_dir() / "two"))


def test_public_key_of_a_key_with_a_passphrase(home_dir: Path) -> None:
    """パスフレーズ付きの鍵は、問いかけずにその場で失敗する。"""
    path = home_dir / "locked"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "secret", "-f", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    with pytest.raises(TemtError, match="ssh-keygen"):
        _ = keys.public_key(path)


def test_candidates_gathers_private_keys(home_dir: Path) -> None:
    make(keys_dir() / "temt")
    make(home_dir / ".ssh" / "id_ed25519")
    (home_dir / ".ssh" / "known_hosts").write_text("", encoding="utf-8")
    assert keys.candidates() == [
        keys_dir() / "temt",
        home_dir / ".ssh" / "id_ed25519",
    ]


def test_fingerprint_of_something_that_is_not_a_public_key() -> None:
    for line in ("", "hello", "ssh-ed25519 not-base64!!"):
        with pytest.raises(TemtError, match="not a public key"):
            _ = keys.fingerprint(line)


def test_candidates_skips_directories(home_dir: Path) -> None:
    _ = home_dir
    (keys_dir() / "sub").mkdir(parents=True)
    make(keys_dir() / "temt")
    assert keys.candidates() == [keys_dir() / "temt"]


def test_describe_tells_the_bits_and_the_type(home_dir: Path) -> None:
    path = make(home_dir / "k")
    assert keys.describe(keys.public_key(path)) == ("256", "ED25519")


def test_a_comment_that_is_not_utf8_goes_to_aws_as_it_is(home_dir: Path) -> None:
    """コメントの UTF-8 として読めないバイトは、ssh-keygen が出したまま EC2 に渡す。"""
    path = make(home_dir / "k", comment=os.fsdecode(b"user@\xff\xfehost"))
    line = keys.public_key(path)
    assert keys.material(line).endswith(b" user@\xff\xfehost")
    assert keys.describe(line) == ("256", "ED25519")


@pytest.mark.parametrize("body", ["\u3042", "AA\udcff"], ids=["not ascii", "not utf8"])
def test_fingerprint_of_a_body_that_is_not_ascii(body: str) -> None:
    with pytest.raises(TemtError, match="^not a public key: ssh-ed25519$"):
        _ = keys.fingerprint(f"ssh-ed25519 {body} c")


def test_describe_of_something_that_is_not_a_public_key() -> None:
    with pytest.raises(TemtError, match="ssh-keygen"):
        _ = keys.describe("ssh-ed25519 AAAA")


def test_public_path_is_next_to_the_private_key() -> None:
    assert keys.public_path(Path("/a/b.key")) == Path("/a/b.key.pub")


def test_matching_skips_keys_that_cannot_be_read(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wanted = make(keys_dir() / "temt")
    _ = make(home_dir / ".ssh" / "id_ed25519", "other")
    (keys_dir() / "broken").write_text("not a key\n", encoding="utf-8")
    assert keys.matching(keys.fingerprint(keys.public_key(wanted))) == [wanted]
    assert "warning: skipped ~/.ssh/temt/keys/broken: ssh-keygen" in capsys.readouterr().err
