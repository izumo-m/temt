"""keypair list、show、create、delete の本体。EC2 のキーペアと手元の鍵を扱う。

どのコマンドも --svc を取らないので、環境は --profile と --region で決まる。temt が
作ったキーペアには temt:keypair タグを付け、list はそれを挙げ、delete はそれだけを
消す。手元の鍵は ssh ディレクトリの keys/<キーペア名> で、create はそこに作り、delete は
そこから消す。

フィンガープリントは、公開鍵から keys.fingerprint で計算する（ssh-keygen -l と同じ
SHA256）。EC2 の KeyFingerprint は、鍵の種類と登録のしかたで計算が変わるので比べず、
show に並べるだけにする。キーペアが使われているかは、そのアカウントとリージョンにある
すべての起動テンプレートの、すべてのバージョンの KeyName で決める。

list の表は table が、他の出力は status と同じく msg が標準出力に書く。
boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from temt import aws, keypair, keys, msg, proc, resources, table
from temt.aws import error_code, field
from temt.msg import TemtError
from temt.names import PATH_MARK, exists, is_key_name, keys_dir, tilde
from temt.resources import TemplateVersion

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_ec2.client import EC2Client

# どの起動テンプレートからも使われていないときの表示
UNUSED = "(unused)"
# 無いときの表示
NONE = "(none)"
# 公開鍵やフィンガープリントが得られないときの表示
UNKNOWN = "(unknown)"
# keypair list の LOCAL の値。手元の鍵との照合。手元に無ければ空で、table が EMPTY と出す
LOCAL_MATCHES = "matches"
LOCAL_DIFFERS = "differs"
LOCAL_UNREADABLE = "unreadable"
LOCAL_UNKNOWN = "unknown"
NO_LOCAL_KEY = ""
# 終了していないインスタンスの状態。delete が挙げる
LIVE_STATES = ["pending", "running", "stopping", "stopped"]
# 秘密鍵のファイルの、空でない最初の行（-----BEGIN ... PRIVATE KEY-----）の頭と尻
PRIVATE_BEGIN = "-----BEGIN "
PRIVATE_END = "PRIVATE KEY-----"

COLUMNS = table.Columns(
    defaults=("NAME", "FINGERPRINT", "CREATED", "LOCAL", "USED"),
    extras=("ID", "TYPE"),
)


def _lookup(ec2: EC2Client, name: str) -> Mapping[str, Any] | None:
    """その名前のキーペアを、公開鍵とともに引く。無ければ None。"""
    try:
        found = ec2.describe_key_pairs(KeyNames=[name], IncludePublicKey=True)
    except ClientError as e:
        if error_code(e) == keypair.NOT_FOUND:
            return None
        raise
    pairs = found.get("KeyPairs", [])
    return pairs[0] if pairs else None


def _tagged(pair: Mapping[str, Any]) -> bool:
    """temt が作ったキーペアか。temt:keypair タグのキーの有無で見分け、値は見ない。"""
    return any(tag.get("Key") == aws.TAG_KEYPAIR for tag in pair.get("Tags") or [])


def _fingerprint(public_key: Any) -> str | None:
    """EC2 が返した公開鍵のフィンガープリント。公開鍵が無いか読めなければ None。"""
    if not isinstance(public_key, str) or not public_key:
        return None
    try:
        return keys.fingerprint(public_key)
    except TemtError:
        return None


def _users(ec2: EC2Client) -> dict[str, list[TemplateVersion]]:
    """キーペア名ごとに、それを使う起動テンプレートのバージョン。"""
    found: dict[str, list[TemplateVersion]] = {}
    for name, versions in resources.all_versions(ec2):
        for version in versions:
            key_name = (version.get("LaunchTemplateData") or {}).get("KeyName")
            if key_name:
                label = TemplateVersion(name, version.get("VersionNumber"))
                found.setdefault(key_name, []).append(label)
    return found


def _instances(ec2: EC2Client, name: str) -> list[str]:
    """そのキーペアで起動し、まだ終了していないインスタンスの表示。"""
    found: list[str] = []
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[
            {"Name": "key-name", "Values": [name]},
            {"Name": "instance-state-name", "Values": LIVE_STATES},
        ]
    ):
        for reservation in page.get("Reservations", []):
            for item in reservation.get("Instances", []):
                state = (item.get("State") or {}).get("Name") or UNKNOWN
                found.append(f"{item.get('InstanceId')} ({state})")
    return found


def _local_key(name: str) -> Path:
    """キーペアに対応する、ssh ディレクトリの keys/ の秘密鍵。"""
    return keys_dir() / name


def _local_state(name: str, want: str | None) -> str:
    """keypair list の LOCAL の値。手元の keys/<キーペア名> との照合。

    手でタグを付けたキーペアの名前は、ファイル名の規則に合わないことがある。keys/ の
    外を指さないよう、そのときは手元の鍵を探さない。
    """
    if not is_key_name(name):
        return NO_LOCAL_KEY
    path = _local_key(name)
    if not exists(path):
        return NO_LOCAL_KEY
    if want is None:
        return LOCAL_UNKNOWN
    try:
        mine = keys.fingerprint(keys.public_key(path))
    except TemtError as e:
        msg.warn(f"cannot read {table.printable(tilde(path))}: {e}")
        return LOCAL_UNREADABLE
    return LOCAL_MATCHES if mine == want else LOCAL_DIFFERS


def list_keypairs(region: str | None, spec: str | None) -> None:
    """temt:keypair タグの付いたキーペアを、1 行に 1 つずつ表で挙げる。

    すべての起動テンプレートを読むのは USED を出すときだけ、ssh-keygen を使うのは
    FINGERPRINT か LOCAL を出すときだけである。
    """
    chosen = COLUMNS.choose(spec)
    needs_fingerprint = "FINGERPRINT" in chosen or "LOCAL" in chosen
    if needs_fingerprint:
        _ = proc.require(keys.KEYGEN)
    ec2 = aws.environment(region).client("ec2")
    pairs = ec2.describe_key_pairs(
        Filters=[{"Name": "tag-key", "Values": [aws.TAG_KEYPAIR]}], IncludePublicKey=True
    ).get("KeyPairs", [])
    users = _users(ec2) if pairs and "USED" in chosen else {}
    rows: list[dict[str, str]] = []
    for pair in sorted(pairs, key=lambda one: str(one.get("KeyName") or "")):
        name = field(pair, "KeyName")
        want = _fingerprint(pair.get("PublicKey")) if needs_fingerprint else None
        rows.append(
            {
                "NAME": name,
                "FINGERPRINT": want or "",
                "CREATED": aws.cell_time(pair.get("CreateTime")),
                "LOCAL": _local_state(name, want) if "LOCAL" in chosen else "",
                "USED": resources.cells(users.get(name)),
                "ID": str(pair.get("KeyPairId") or ""),
                "TYPE": str(pair.get("KeyType") or ""),
            }
        )
    table.write(chosen, rows)


def show(target: str, region: str | None) -> None:
    """キーペア 1 つか、手元の鍵のファイル 1 つを詳しく表示する。

    target に / があればファイル、無ければキーペア名とする。キーペア名には / を
    使えない（names.KEY_NAME_RE）ので、今のディレクトリの鍵は ./ を付けて指す。
    """
    _ = proc.require(keys.KEYGEN)
    if PATH_MARK in target:
        path = Path(target)
        # 打ち誤りで AWS に問い合わせないよう、ファイルを先に確かめる
        if not exists(path):
            raise TemtError(f"no such file: {table.printable(str(path))}")
        if not path.is_file():
            raise TemtError(f"not a file: {table.printable(str(path))}")
        _show_file(aws.environment(region).client("ec2"), path)
    else:
        _show_pair(aws.environment(region).client("ec2"), target)


def _show_pair(ec2: EC2Client, name: str) -> None:
    """キーペア 1 つを、管理下かどうかを問わず表示する。"""
    pair = _lookup(ec2, name)
    if pair is None:
        raise TemtError(f"no key pair named {table.printable(name)}")
    # AWS は公開鍵の末尾に改行を付けて返す
    public_key = str(pair.get("PublicKey") or "").strip()
    want = _fingerprint(public_key)
    tags = " ".join(f"{tag.get('Key')}={tag.get('Value')}" for tag in pair.get("Tags") or [])
    local = keys.matching(want) if want is not None else []
    msg.tell(f"key pair {table.printable(name)}:\n")
    msg.rows(
        [
            ("id", str(pair.get("KeyPairId") or UNKNOWN)),
            ("type", str(pair.get("KeyType") or UNKNOWN)),
            ("created", aws.when(pair.get("CreateTime"))),
            ("tags", table.printable(tags) or NONE),
            ("fingerprint", want or UNKNOWN),
            ("KeyFingerprint", str(pair.get("KeyFingerprint") or UNKNOWN)),
            ("public key", table.printable(public_key) or UNKNOWN),
            ("used by", ", ".join(map(str, _users(ec2).get(name, []))) or UNUSED),
            ("local keys", ", ".join(table.printable(tilde(path)) for path in local) or NONE),
        ]
    )


def _first_line(path: Path) -> str:
    """ファイルの、空でない最初の行。

    UTF-8 として読めないバイト（鍵のコメントなど）は、ssh-keygen の出力と同じく
    対にならないサロゲートとして読み、表示するときに printable が置き換える。
    """
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _is_private(line: str) -> bool:
    """ファイルの空でない最初の行が、秘密鍵の始まりか。"""
    return line.startswith(PRIVATE_BEGIN) and line.endswith(PRIVATE_END)


def _pair_state(public_key: str, pub: Path) -> str:
    """秘密鍵と隣の .pub とが一致するかの表示。"""
    if not exists(pub):
        return NONE
    try:
        theirs = keys.fingerprint(_first_line(pub))
    except (TemtError, OSError):
        return f"{table.printable(tilde(pub))} is not a readable public key"
    if theirs == keys.fingerprint(public_key):
        return f"matches {table.printable(tilde(pub))}"
    return f"differs from {table.printable(tilde(pub))}"


def _show_file(ec2: EC2Client, path: Path) -> None:
    """手元の鍵のファイル 1 つと、それと一致するキーペアを表示する。

    秘密鍵なら公開鍵を取り出す。パスフレーズ付きなどで取り出せなければ、隣の .pub が
    あればそれを使い、無ければ取り出せない理由だけを出す。
    """
    shown = table.printable(tilde(path.absolute()))
    rows: list[tuple[str, str]] = []
    # 公開鍵を読んだファイル。壊れていたときに名前を挙げる
    source = path
    if _is_private(_first_line(path)):
        rows.append(("kind", "private key"))
        pub = keys.public_path(path)
        try:
            public_key = keys.public_key(path)
            rows_after = [(".pub", _pair_state(public_key, pub))]
        except TemtError as e:
            try:
                public_key = _first_line(pub) if exists(pub) and pub.is_file() else ""
            except OSError:
                public_key = ""
            if not public_key:
                msg.tell(f"key file {shown}:\n")
                msg.rows([*rows, ("public key", f"cannot read: {e}")])
                return
            source = pub
            rows_after = [(".pub", f"used instead; cannot read the private key: {e}")]
    else:
        rows.append(("kind", "public key"))
        public_key = _first_line(path)
        rows_after = []
    try:
        want = keys.fingerprint(public_key)
        bits, kind = keys.describe(public_key)
    except TemtError as e:
        raise TemtError(f"not an ssh key: {table.printable(str(source))}: {e}") from None
    comment = " ".join(public_key.split()[2:])
    pairs = ec2.describe_key_pairs(IncludePublicKey=True).get("KeyPairs", [])
    matched = sorted(
        str(pair.get("KeyName")) for pair in pairs if _fingerprint(pair.get("PublicKey")) == want
    )
    msg.tell(f"key file {shown}:\n")
    msg.rows(
        [
            *rows,
            ("type", f"{kind}, {bits} bits"),
            ("comment", table.printable(comment) or NONE),
            ("fingerprint", want),
            ("public key", table.printable(public_key)),
            *rows_after,
            ("key pairs", table.printable(", ".join(matched)) or NONE),
        ]
    )


def create(name: str, region: str | None) -> None:
    """手元の keys/<名前> の鍵を、キーペア <名前> として登録し、temt:keypair を付ける。

    手元の鍵も EC2 のキーペアも、あればそのまま使い、無ければ作る。途中で止まっても、
    やり直せば残りを続けられるよう、作った鍵は消さない。EC2 に登録できたか分からない
    まま止まったとき（botocore が送り直した要求が InvalidKeyPair.Duplicate で返るときも）
    も、やり直せば一致するキーペアとして終わる。EC2 にあるのに手元に秘密鍵が無いとき、
    両方あって食い違うときは、何も変えずに止める。
    """
    _ = proc.require(keys.KEYGEN)
    ec2 = aws.environment(region).client("ec2")
    path = _local_key(name)
    shown = table.printable(tilde(path))
    pair = _lookup(ec2, name)
    if exists(path):
        try:
            public_key = keys.public_key(path)
        except TemtError as e:
            raise TemtError(f"cannot read {shown}: {e}") from None
    elif pair is not None:
        raise TemtError(
            f"the key pair {name} exists in EC2, but {shown} does not; put its private key "
            f"there, or run 'temt keypair delete {name}' and create it again"
        )
    else:
        pub = keys.public_path(path)
        if exists(pub):
            raise TemtError(
                f"{table.printable(tilde(pub))} exists without {shown}; "
                "remove it to create a new key"
            )
        keys.generate(path, name)
        msg.info(f"created the key {shown}")
        public_key = keys.public_key(path)
    mine = keys.fingerprint(public_key)
    if pair is None:
        result = ec2.import_key_pair(
            KeyName=name,
            PublicKeyMaterial=keys.material(public_key),
            TagSpecifications=keypair.tags(name),
        )
        msg.info(f"imported the key pair {name} ({result.get('KeyPairId')}), {mine}")
        return
    want = _fingerprint(pair.get("PublicKey"))
    if want is None:
        raise TemtError(f"AWS did not return a readable public key of the key pair {name}")
    if want != mine:
        raise TemtError(
            f"the key pair {name} in EC2 does not match {shown}; "
            f"'temt keypair show {name}' lists the local keys that match it"
        )
    msg.info(f"the key pair {name} ({pair.get('KeyPairId')}) already matches {shown}, {mine}")


def delete(name: str, region: str | None, *, yes: bool) -> None:
    """temt:keypair の付いたキーペアと、手元の keys/<名前> と <名前>.pub を消す。

    鍵を作り直しやすくするためのコマンドである。どちらも、あれば消し、無ければ
    飛ばす。どちらも無ければ、そう知らせて終わる。タグの無いキーペアがあれば、手元の
    鍵も消さずに止める。

    使っている起動テンプレートのバージョンと、終了していないインスタンスを警告として
    挙げるが、止めはしない。サービスの KeyName が指していて IdentityFile が既定の
    ままなら、次の apply が新しい鍵を作って同じ名前で登録し直す。手元の鍵は、設定
    ファイルや ssh の設定が指していても消す。手元の鍵は環境をまたいで 1 つなので、
    他のリージョンやアカウントに同じ鍵を登録していても消える。調べはせず、そこでも
    消すよう警告するだけにする。EC2 のキーペアを先に消し、消せなければ手元の鍵は残す。
    """
    ec2 = aws.environment(region).client("ec2")
    pair = _lookup(ec2, name)
    if pair is not None and not _tagged(pair):
        raise TemtError(
            f"the key pair {name} has no {aws.TAG_KEYPAIR} tag; "
            "temt deletes only key pairs that it created"
        )
    path = _local_key(name)
    local = [one for one in (path, keys.public_path(path)) if exists(one)]
    if pair is None and not local:
        msg.info(f"no key pair {name} in EC2 and no local key; nothing to delete")
        return
    pair_id = field(pair, "KeyPairId") if pair is not None else ""
    if pair is not None:
        users = _users(ec2).get(name)
        if users:
            msg.warn(f"the key pair {name} is used by {', '.join(map(str, users))}")
        live = _instances(ec2, name)
        if live:
            msg.warn(
                f"instances launched with the key pair {name}: {', '.join(live)}; "
                "you may be unable to log in to them"
            )
    if local:
        msg.warn(
            f"{table.printable(tilde(path))} is shared by every region and account; if the key is "
            f"also registered elsewhere, run 'temt keypair delete {name}' there too, or "
            "apply there stops with a key mismatch"
        )
    msg.tell("delete:\n")
    if pair is not None:
        msg.tell(f"  key pair {name} ({pair_id})\n")
    for one in local:
        msg.tell(f"  {table.printable(tilde(one))}\n")
    if pair is None:
        question = f"delete the local key of {name}?"
    else:
        also = " and its local key" if local else ""
        question = f"delete the key pair {name}{also}?"
    msg.confirm(question, yes=yes)
    if pair is not None:
        _ = ec2.delete_key_pair(KeyPairId=pair_id)
        msg.info(f"deleted the key pair {name} ({pair_id})")
    for one in local:
        one.unlink(missing_ok=True)
        msg.info(f"deleted {table.printable(tilde(one))}")
