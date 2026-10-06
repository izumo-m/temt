"""ssh の鍵の、手元側の扱い。ssh-keygen を呼ぶ。"""

import base64
import binascii
import hashlib
from pathlib import Path

from temt import msg, proc
from temt.msg import TemtError
from temt.names import keys_dir, make_dir, tilde, user_ssh_dir
from temt.table import printable

KEYGEN = "ssh-keygen"
KEY_TYPE = "ed25519"


def public_path(path: Path) -> Path:
    """秘密鍵の隣に置く公開鍵のパス。<path>.pub。"""
    return path.with_name(f"{path.name}.pub")


def generate(path: Path, comment: str) -> None:
    """パスフレーズ無しの鍵をその場所に作る。公開鍵は <path>.pub になる。"""
    if path.exists():
        raise TemtError(f"key already exists: {printable(str(path))}")
    make_dir(path.parent)
    _ = proc.run([KEYGEN, "-q", "-t", KEY_TYPE, "-N", "", "-C", comment, "-f", str(path)])


def public_key(path: Path) -> str:
    """秘密鍵から公開鍵の 1 行を取り出す。

    隣に <名前>.pub があっても、秘密鍵そのものから導く。-P '' を渡すのは、
    パスフレーズ付きの鍵を渡されたときに、問いかけを出さずその場で失敗させる
    ためである。temt が作る鍵にパスフレーズは付けない。
    """
    return proc.run([KEYGEN, "-y", "-P", "", "-f", str(path)]).strip()


def fingerprint(public_key_line: str) -> str:
    """公開鍵の 1 行から、SHA256 のフィンガープリントを計算する。

    ssh-keygen -l が表示するものと同じ SHA256:... の形で返す。手元の鍵も EC2 の
    キーペアも公開鍵の 1 行として得られるので、同じ計算で比べられる。秘密鍵から
    ssh-keygen -l で計算しないのは、隣に <名前>.pub があるとそちらを読むため、
    秘密鍵と食い違う .pub があると、秘密鍵のものではない値になるからである。
    """
    fields = public_key_line.split()
    if len(fields) < 2:
        raise TemtError(f"not a public key: {printable(public_key_line.strip()[:32])}")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except binascii.Error:
        raise TemtError(f"not a public key: {printable(fields[0])}") from None
    if not blob:
        raise TemtError(f"not a public key: {printable(fields[0])}")
    return f"SHA256:{base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')}"


def describe(public_key_line: str) -> tuple[str, str]:
    """公開鍵の 1 行の、ビット数と種類（ED25519、RSA など）。ssh-keygen -l に尋ねる。

    公開鍵を標準入力で渡すので、隣の .pub に左右されない。
    """
    shown = proc.run([KEYGEN, "-l", "-f", "-"], stdin=f"{public_key_line}\n").split()
    if len(shown) < 3 or not shown[-1].startswith("(") or not shown[-1].endswith(")"):
        raise TemtError(f"{KEYGEN}: unexpected output: {printable(' '.join(shown)[:64])}")
    return shown[0], shown[-1][1:-1]


def candidates() -> list[Path]:
    """EC2 のキーペアと突き合わせる候補になる秘密鍵。

    ssh ディレクトリの keys/ の鍵と ~/.ssh/id_* を集め、公開鍵 (*.pub) は除く。
    """
    found = sorted(keys_dir().glob("*")) + sorted(user_ssh_dir().glob("id_*"))
    return [path for path in found if path.is_file() and path.suffix != ".pub"]


def matching(want: str) -> list[Path]:
    """候補の秘密鍵のうち、フィンガープリントが want のもの。

    読めない鍵は外す。パスフレーズ付きの鍵もここで外れるので、黙って落とさず、
    ssh-keygen が言う理由をそのまま知らせる。
    """
    found: list[Path] = []
    for path in candidates():
        try:
            if fingerprint(public_key(path)) == want:
                found.append(path)
        except TemtError as e:
            msg.warn(f"skipped {printable(tilde(path))}: {e}")
    return found
