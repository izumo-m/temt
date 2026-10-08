"""ssh の鍵の、手元側の扱い。

インスタンスに入る鍵は EC2 に登録しない。proxy がつなぐたびに、ssh がそのホストに使う
鍵の公開鍵を EC2 Instance Connect でインスタンスに送る（offered で探す）。temt が
持つのは、ssh の設定の IdentityFile に書く既定の鍵だけで、apply は無ければそれを作る
（detect と ensure）。
"""

import os
import pwd
from dataclasses import dataclass
from pathlib import Path

from temt import msg, proc
from temt.change import ADD, Change
from temt.msg import TemtError
from temt.names import exists, keys_dir, make_dir, tilde
from temt.table import printable

KEYGEN = "ssh-keygen"
SSH = "ssh"
KEY_TYPE = "ed25519"
# 既定の鍵のファイル名とコメント。keys/ に置き、全サービスで共有する
DEFAULT_NAME = "temt"
# 差分の種類。change.Change の kind
KEY = "key"
# ssh -G が出す項目の名前
USER_OPTION = "user"
IDENTITY_OPTION = "identityfile"


def default_path() -> Path:
    """既定の鍵の場所。"""
    return keys_dir() / DEFAULT_NAME


def public_path(path: Path) -> Path:
    """秘密鍵の隣に置く公開鍵のパス。<path>.pub。"""
    return path.with_name(f"{path.name}.pub")


def generate(path: Path, comment: str) -> None:
    """パスフレーズ無しの鍵をその場所に作る。公開鍵は <path>.pub になる。"""
    if path.exists():
        raise TemtError(f"key already exists: {printable(str(path))}")
    make_dir(path.parent)
    _ = proc.run([KEYGEN, "-q", "-t", KEY_TYPE, "-N", "", "-C", comment, "-f", str(path)])


@dataclass(frozen=True)
class KeyDiff:
    """既定の鍵を作るという差分。path はその場所。"""

    path: Path

    def changes(self) -> list[Change]:
        """diff に出す形にする。"""
        return [Change(ADD, KEY, tilde(self.path), f"ssh-keygen -t {KEY_TYPE}")]


def detect(path: Path) -> KeyDiff | None:
    """既定の鍵 path を作るかを検出する。作るなら、ssh-keygen があることも確かめる。

    秘密鍵と公開鍵の両方があれば何もしない。片方だけなら、作り直すともう片方を失うか
    食い違うので止める。公開鍵が無いと、proxy はインスタンスに送る鍵を持たない。
    """
    shown = printable(tilde(path))
    private = exists(path)
    public = exists(public_path(path))
    if private and public:
        return None
    if private:
        raise TemtError(
            f"{shown}.pub does not exist; make it with 'ssh-keygen -y -f {shown} > {shown}.pub'"
        )
    if public:
        raise TemtError(f"{shown}.pub exists without {shown}; remove it to create a new key")
    _ = proc.require(KEYGEN)
    return KeyDiff(path)


def ensure(found: KeyDiff | None) -> None:
    """detect が検出したとおり、既定の鍵を作る。"""
    if found is None:
        return
    generate(found.path, DEFAULT_NAME)
    msg.info(f"created the key {printable(tilde(found.path))}")


def _expand(value: str, user: str) -> Path:
    """ssh -G が出した IdentityFile を、ssh と同じくパスに展開する。

    ssh -G は ~ もトークンも展開せずに出す。先頭の ~、%d（ホーム）、%u（手元の
    ユーザー）、%r（ログインするユーザー）、%% を展開する。ほかのトークンと
    ${NAME} は展開できないので、書き直すよう伝えて止める。%u は、ssh と同じく
    パスワードデータベースの名前にする。~ と %d は、temt のほかの場所と同じく HOME に
    する。
    """
    cannot = TemtError(f"temt cannot expand IdentityFile {printable(value)}; write it as a path")
    if "${" in value:
        raise cannot
    tokens = {"d": str(Path.home()), "u": pwd.getpwuid(os.getuid()).pw_name, "r": user, "%": "%"}
    expanded = ""
    rest = value
    while "%" in rest:
        before, _, after = rest.partition("%")
        token = after[:1]
        if token not in tokens:
            raise cannot
        expanded += before + tokens[token]
        rest = after[1:]
    expanded += rest
    if expanded == "~" or expanded.startswith("~/"):
        # ~//x も、ssh と同じくホームの下とする
        return Path.home() / expanded[2:].lstrip("/")
    return Path(expanded)


def offered(host: str, user: str | None = None) -> tuple[str, Path]:
    """ssh が host にログインするユーザーと、送る公開鍵のファイル。ssh -G に尋ねる。

    user は ssh から受け取ったユーザーで、あれば ssh -G のものより優先する。ssh は
    IdentityFile を並んだ順に試すので、その順で、隣に .pub がある最初の鍵を使う。
    パスフレーズ付きの鍵でも、.pub があれば使える。IdentityFile が .pub そのものを
    指していれば（秘密鍵を ssh-agent やハードウェアの鍵に置く使い方）、それを使う。
    """
    shown = proc.run([SSH, "-G", host])
    found = ""
    files: list[str] = []
    for line in shown.splitlines():
        name, _, value = line.partition(" ")
        if name == USER_OPTION:
            found = value
        elif name == IDENTITY_OPTION:
            files.append(value)
    user = user or found
    if not user:
        raise TemtError(f"'ssh -G {host}' did not tell the user")
    for value in files:
        path = _expand(value, user)
        if not path.name.endswith(".pub"):
            path = public_path(path)
        if exists(path):
            return user, path
    tried = ", ".join(printable(value) for value in files) or "none"
    raise TemtError(
        f"no public key for 'ssh {host}': no .pub next to its IdentityFile ({tried}); "
        f"put the .pub next to the key, or delete IdentityFile from the Host {host} block "
        "and run 'temt apply' to use the default key"
    )


def read_public(path: Path) -> str:
    """公開鍵のファイルから、種類と本体だけの 1 行を読む。

    コメントは送らない。ログインに要らず、UTF-8 として読めないバイトを含みうるからで
    ある。
    """
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    fields = next((line.split() for line in text.splitlines() if line.strip()), [])
    if len(fields) < 2 or not (fields[0] + fields[1]).isascii():
        raise TemtError(f"not a public key: {printable(tilde(path))}")
    return f"{fields[0]} {fields[1]}"
