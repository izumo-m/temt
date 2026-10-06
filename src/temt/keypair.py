"""EC2 のキーペアと手元の鍵を揃える。apply の手順 5。

インスタンスにログインできる鍵を、このキーペアの 1 つだけにする。EC2 にキーペアが
無ければ手元の鍵を登録して temt:keypair タグを付ける。あって食い違えば、設定
ファイルの誤りとして止める。EC2 にあるキーペアそのものも、設定ファイルの
IdentityFile も変えない。detect が差分を検出し、ensure が検出した差分で揃える。

botocore の例外は、キーペアが無いことを表す誤りだけをここで扱い、他は apply に
そのまま渡す。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from temt import aws, keys, msg
from temt.aws import error_code
from temt.change import ADD, Change
from temt.msg import TemtError
from temt.names import tilde
from temt.table import printable

if TYPE_CHECKING:
    from types_boto3_ec2.client import EC2Client
    from types_boto3_ec2.type_defs import TagSpecificationTypeDef

    from temt.conf import Conf
    from temt.names import Service

# その名前のキーペアが無いときに describe-key-pairs が返す誤りの名前
NOT_FOUND = "InvalidKeyPair.NotFound"

# 差分の種類。change.Change の kind
KEY = "key"
KEYPAIR = "keypair"


@dataclass(frozen=True)
class KeyDiff:
    """キーペアと手元の鍵の差分。

    path は IdentityFile が指す鍵。want は EC2 のキーペアのフィンガープリントで、
    キーペアが無ければ None。mine は手元の鍵のフィンガープリントで、鍵が無ければ None。
    キーペアがあって食い違うときは、detect が止めるので、ここには来ない。
    """

    key_name: str
    path: Path
    want: str | None
    mine: str | None

    @property
    def register(self) -> bool:
        """手元の鍵をキーペアとして登録するか。"""
        return self.want is None

    @property
    def generate(self) -> bool:
        """登録する前に、手元の鍵を作るか。"""
        return self.register and self.mine is None

    def changes(self) -> list[Change]:
        """diff に出す形にする。"""
        path = tilde(self.path)
        found: list[Change] = []
        if self.generate:
            found.append(Change(ADD, KEY, path, f"ssh-keygen -t {keys.KEY_TYPE}"))
        if self.register:
            found.append(Change(ADD, KEYPAIR, self.key_name, f"from {path}"))
        return found


def _fix(service: Service) -> str:
    """鍵が揃わないときに伝えること。

    初回の apply は、確認の前に止まるので、設定ファイルをまだ作っていない。
    """
    if not service.conf_path.exists():
        return (
            f"write IdentityFile or KeyName in a new {printable(str(service.conf_path))} "
            f"and run apply again"
        )
    return (
        f"edit IdentityFile or KeyName in {printable(str(service.conf_path))} and run apply again"
    )


def _mine(service: Service, path: Path) -> str:
    """手元の秘密鍵から公開鍵を取り出す。読めなければ、直す場所を添えて伝える。"""
    try:
        return keys.public_key(path)
    except TemtError as e:
        raise TemtError(f"{e}; {_fix(service)}") from None


def _remote_public_key(ec2: EC2Client, key_name: str) -> str | None:
    """EC2 のキーペアの公開鍵。そのキーペアが無ければ None。"""
    try:
        found = ec2.describe_key_pairs(KeyNames=[key_name], IncludePublicKey=True)
    except ClientError as e:
        if error_code(e) == NOT_FOUND:
            return None
        raise
    pairs = found.get("KeyPairs", [])
    if not pairs:
        return None
    material = pairs[0].get("PublicKey")
    if not material:
        raise TemtError(f"AWS did not return the public key of the key pair {key_name}")
    return material


def _remote_fingerprint(public_key: str, key_name: str) -> str:
    """EC2 のキーペアのフィンガープリント。公開鍵が読めない形なら、名前を添えて伝える。"""
    try:
        return keys.fingerprint(public_key)
    except TemtError as e:
        raise TemtError(f"the key pair {key_name} in EC2: {e}") from None


def tags(key_name: str) -> list[TagSpecificationTypeDef]:
    """temt が作るキーペアに付けるタグ。temt:keypair=<キーペア名>。"""
    return [{"ResourceType": "key-pair", "Tags": [{"Key": aws.TAG_KEYPAIR, "Value": key_name}]}]


def _register(ec2: EC2Client, service: Service, values: Conf, found: KeyDiff) -> None:
    """手元の鍵を、検出したとおり無ければ作ってから、キーペアとして登録する。"""
    path = found.path
    if found.generate:
        keys.generate(path, values.KeyName)
        msg.info(f"created the key {printable(tilde(path))}")
    _ = ec2.import_key_pair(
        KeyName=values.KeyName,
        PublicKeyMaterial=_mine(service, path).encode(),
        TagSpecifications=tags(values.KeyName),
    )
    msg.info(f"imported the key pair {values.KeyName}")


def _mismatch(service: Service, values: Conf, path: Path, mine: str | None, want: str) -> None:
    """食い違いを、キーペアの名前と両方のフィンガープリントで知らせ、止める。

    直し方の手がかりとして、キーペアと一致する手元の鍵を挙げる。
    """
    if mine is None:
        msg.warn(f"the key pair {values.KeyName} exists, but {printable(tilde(path))} does not")
    else:
        msg.warn(f"the key pair {values.KeyName} does not match {printable(tilde(path))}")
        msg.tell(f"  local: {mine}\n")
    msg.tell(f"  EC2:   {want}\n")
    found = keys.matching(want)
    if found:
        msg.tell(f"keys that match the key pair {values.KeyName}:\n")
        for one in found:
            msg.tell(f"  {printable(tilde(one))}\n")
    else:
        msg.tell(f"no key on this machine matches the key pair {values.KeyName}\n")
    raise TemtError(f"the key pair {values.KeyName} does not match; {_fix(service)}")


def detect(ec2: EC2Client, service: Service, values: Conf) -> KeyDiff:
    """キーペアと手元の鍵の差分を検出する。

    設定ファイルの IdentityFile が指す鍵と、EC2 のキーペア KeyName とを、同じ方法で
    計算したフィンガープリントで比べる。手元の鍵があって読めなければ止める。キーペアが
    あるのに、手元の鍵が無いか食い違えば、設定ファイルの誤りとして止める。
    """
    path = Path(values.IdentityFile).expanduser()
    remote = _remote_public_key(ec2, values.KeyName)
    want = None if remote is None else _remote_fingerprint(remote, values.KeyName)
    mine = keys.fingerprint(_mine(service, path)) if path.exists() else None
    if want is not None and mine != want:
        _mismatch(service, values, path, mine, want)
    return KeyDiff(values.KeyName, path, want, mine)


def ensure(ec2: EC2Client, service: Service, values: Conf, found: KeyDiff) -> None:
    """detect が検出した差分でキーペアと手元の鍵を揃える。

    キーペアが無ければ、手元の鍵を（無ければ作ってから）登録する。あれば何もしない。
    """
    if found.register:
        _register(ec2, service, values, found)
