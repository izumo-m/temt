"""keypair list、show、create、delete の本体のテスト。

ssh-keygen は実際に呼び、AWS は Stubber で差し替える。環境は aws.env_session が決める
ので、それを用意したクライアントを返すものに差し替える。
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from botocore.stub import ANY, Stubber
from conftest import REGION, offline, watched
from types_boto3_ec2.client import EC2Client

from temt import aws, keypair, keypairs, keys
from temt.msg import TemtError, UsageError
from temt.names import keys_dir

ACCOUNT = "123456789012"
ENVIRONMENT = f"account {ACCOUNT}, region {REGION}, profile (none)"
CREATED = datetime(2026, 9, 20, 3, tzinfo=UTC)
# CREATED を手元の時刻（日本時間）で表したもの
SHOWN = "2026-09-20 12:00:00"
TAG = {"Key": "temt:keypair", "Value": "temt"}
LIVE = ["pending", "running", "stopping", "stopped"]


class Session:
    """aws.env_session の代わり。用意したクライアントを返す。"""

    region_name = REGION
    profile_name = "default"

    def __init__(self, ec2: EC2Client, sts: Any) -> None:
        self.clients: dict[str, Any] = {"ec2": ec2, "sts": sts}

    def client(self, name: str) -> Any:
        return self.clients[name]


@dataclass(frozen=True)
class Stubs:
    ec2: Stubber
    sts: Stubber
    # aws.env_session に渡されたリージョン
    regions: list[str | None]

    def done(self) -> None:
        self.ec2.assert_no_pending_responses()
        self.sts.assert_no_pending_responses()


@pytest.fixture
def stubs(
    monkeypatch: pytest.MonkeyPatch, ec2: EC2Client, stub_failures: list[BotoCoreError]
) -> Iterator[Stubs]:
    """aws.env_session が返すクライアントを Stubber で包む。

    環境を決めたコマンドが知らせる資格情報のアカウントは、env_session が呼ばれたときに
    積む。teardown で、積んだ応答がすべて使われたことを確かめる。
    """
    sts = watched(offline().client("sts"), stub_failures)
    session = Session(ec2, sts)
    regions: list[str | None] = []
    sts_stub = Stubber(sts)

    def env_session(region: str | None) -> Session:
        regions.append(region)
        sts_stub.add_response(
            "get_caller_identity",
            {"UserId": "u", "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/u"},
            {},
        )
        return session

    monkeypatch.setattr(aws, "env_session", env_session)
    with Stubber(ec2) as ec2_stub, sts_stub:
        made = Stubs(ec2_stub, sts_stub, regions)
        yield made
        made.done()


class Terminal:
    """端末につながった標準入力の代わり。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self.answer


def make_key(path: Path, comment: str = "temt") -> str:
    """鍵を 1 つ作り、その公開鍵の 1 行を返す。"""
    keys.generate(path, comment)
    return keys.public_key(path)


def locked_key(path: Path) -> None:
    """パスフレーズ付きの鍵を作る。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "secret", "-C", "locked", "-f", str(path)],
        capture_output=True,
        check=True,
    )


def a_pair(
    name: str, public_key: str, *, tags: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """describe-key-pairs が返すキーペア 1 つ分。"""
    return {
        "KeyName": name,
        "KeyPairId": f"key-{name}",
        "KeyType": "ed25519",
        "KeyFingerprint": "aws-fingerprint=",
        "PublicKey": public_key,
        "CreateTime": CREATED,
        "Tags": [{"Key": "temt:keypair", "Value": name}] if tags is None else tags,
    }


def looked_up(stubs: Stubs, name: str, *found: dict[str, Any]) -> None:
    """名前でキーペアを引いた応答を積む。何も渡さなければ、無いときの応答にする。"""
    params = {"KeyNames": [name], "IncludePublicKey": True}
    if found:
        stubs.ec2.add_response("describe_key_pairs", {"KeyPairs": list(found)}, params)
    else:
        stubs.ec2.add_client_error("describe_key_pairs", keypair.NOT_FOUND, expected_params=params)


def templates(stubs: Stubs, **found: list[tuple[int, str]]) -> None:
    """起動テンプレートと、そのバージョンが使うキーペアを積む。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": name} for name in found]},
        {},
    )
    for name, versions in found.items():
        stubs.ec2.add_response(
            "describe_launch_template_versions",
            {
                "LaunchTemplateVersions": [
                    {"VersionNumber": number, "LaunchTemplateData": {"KeyName": key_name}}
                    for number, key_name in versions
                ]
            },
            {"LaunchTemplateName": name},
        )


def instances(stubs: Stubs, name: str, *found: tuple[str, str]) -> None:
    """そのキーペアで起動した、終了していないインスタンスを積む。"""
    stubs.ec2.add_response(
        "describe_instances",
        {
            "Reservations": [
                {"Instances": [{"InstanceId": one, "State": {"Name": state}}]}
                for one, state in found
            ]
        },
        {
            "Filters": [
                {"Name": "key-name", "Values": [name]},
                {"Name": "instance-state-name", "Values": LIVE},
            ]
        },
    )


@pytest.fixture
def in_home(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """今のディレクトリをホームにする。"""
    monkeypatch.chdir(home_dir)
    return home_dir


def test_list_shows_the_tagged_key_pairs(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = make_key(keys_dir() / "temt")
    theirs = make_key(home_dir / "elsewhere")
    _ = make_key(keys_dir() / "old")
    broken = keys_dir() / "broken"
    broken.write_text("not a key\n", encoding="utf-8")
    broken.chmod(0o600)
    stubs.ec2.add_response(
        "describe_key_pairs",
        {
            "KeyPairs": [
                a_pair("work", theirs),
                a_pair("temt", mine),
                a_pair("old", theirs),
                a_pair("broken", theirs),
            ]
        },
        {"Filters": [{"Name": "tag-key", "Values": ["temt:keypair"]}], "IncludePublicKey": True},
    )
    templates(stubs, **{"temt-dev-main-template": [(1, "temt"), (2, "temt")], "mine": [(3, "old")]})

    keypairs.list_keypairs("us-east-1", None)

    stubs.done()
    assert stubs.regions == ["us-east-1"]
    ours = keys.fingerprint(mine)
    other = keys.fingerprint(theirs)
    captured = capsys.readouterr()
    err = captured.err.splitlines()
    assert err[0] == ENVIRONMENT
    assert err[1].startswith("temt: warning: cannot read ~/.ssh/temt/keys/broken: ssh-keygen")
    assert len(err) == 2
    assert captured.out.splitlines() == [
        f"NAME     {'FINGERPRINT':<50}   CREATED               LOCAL        USED",
        f"broken   {other}   {SHOWN}   unreadable   -",
        f"old      {other}   {SHOWN}   differs      mine:3",
        f"temt     {ours}   {SHOWN}   matches      "
        "temt-dev-main-template:1,temt-dev-main-template:2",
        f"work     {other}   {SHOWN}   -            -",
    ]


def test_list_without_key_pairs(stubs: Stubs, capsys: pytest.CaptureFixture[str]) -> None:
    stubs.ec2.add_response("describe_key_pairs", {"KeyPairs": []}, None)

    keypairs.list_keypairs(None, None)

    stubs.done()
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [ENVIRONMENT]
    assert captured.out.splitlines() == ["NAME   FINGERPRINT   CREATED   LOCAL   USED"]


def test_list_reads_only_what_the_columns_need(
    stubs: Stubs,
    home_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ID と TYPE だけなら、起動テンプレートも手元の鍵も読まず、ssh-keygen も要らない。"""
    _ = home_dir
    monkeypatch.setenv("PATH", "")
    stubs.ec2.add_response("describe_key_pairs", {"KeyPairs": [a_pair("temt", "not a key")]}, None)

    keypairs.list_keypairs(None, "type,ID")

    stubs.done()
    assert capsys.readouterr().out.splitlines() == ["TYPE      ID", "ed25519   key-temt"]


def test_list_checks_the_columns_before_aws(stubs: Stubs) -> None:
    with pytest.raises(UsageError, match="repeated column in --format: NAME"):
        keypairs.list_keypairs(None, "name,Name")
    assert stubs.regions == []


def test_show_a_key_pair(stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """管理下でないキーペアも表示し、一致する手元の鍵を挙げる。"""
    public_key = make_key(home_dir / ".ssh" / "id_ed25519")
    _ = make_key(keys_dir() / "other")
    # AWS は公開鍵の末尾に改行を付けて返す
    looked_up(stubs, "legacy", a_pair("legacy", f"{public_key}\n", tags=[]))
    templates(stubs, mine=[(1, "legacy")])

    keypairs.show("legacy", None)

    stubs.done()
    assert capsys.readouterr().err.splitlines() == [
        ENVIRONMENT,
        "key pair legacy:",
        "  id:             key-legacy",
        "  type:           ed25519",
        f"  created:        {SHOWN}",
        "  tags:           (none)",
        f"  fingerprint:    {keys.fingerprint(public_key)}",
        "  KeyFingerprint: aws-fingerprint=",
        f"  public key:     {public_key}",
        "  used by:        mine version 1",
        "  local keys:     ~/.ssh/id_ed25519",
    ]


def test_show_a_key_pair_that_does_not_exist(stubs: Stubs) -> None:
    looked_up(stubs, "nothing")
    with pytest.raises(TemtError, match="^no key pair named nothing$"):
        keypairs.show("nothing", None)
    stubs.done()


def test_show_a_private_key_file(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = home_dir / ".ssh" / "id_ed25519"
    path.parent.mkdir(parents=True)
    public_key = make_key(path, "me@host")
    stubs.ec2.add_response(
        "describe_key_pairs",
        {
            "KeyPairs": [
                a_pair("b", public_key),
                a_pair("x", "ssh-ed25519 AAAA"),
                a_pair("a", public_key),
            ]
        },
        {"IncludePublicKey": True},
    )

    keypairs.show(str(path), None)

    stubs.done()
    assert capsys.readouterr().err.splitlines() == [
        ENVIRONMENT,
        "key file ~/.ssh/id_ed25519:",
        "  kind:        private key",
        "  type:        ED25519, 256 bits",
        "  comment:     me@host",
        f"  fingerprint: {keys.fingerprint(public_key)}",
        f"  public key:  {public_key}",
        "  .pub:        matches ~/.ssh/id_ed25519.pub",
        "  key pairs:   a, b",
    ]


def test_show_a_private_key_whose_pub_differs(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = home_dir / "k"
    _ = make_key(path)
    other = make_key(home_dir / "other")
    keys.public_path(path).write_text(f"{other}\n", encoding="utf-8")
    stubs.ec2.add_response("describe_key_pairs", {"KeyPairs": []}, {"IncludePublicKey": True})

    keypairs.show(str(path), None)

    err = capsys.readouterr().err
    assert "  .pub:        differs from ~/k.pub\n" in err
    assert "  key pairs:   (none)\n" in err


def test_show_a_public_key_file(
    stubs: Stubs, in_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """今のディレクトリの鍵は ./ を付けて指す。表示は ~ で始まる形にする。"""
    public_key = make_key(in_home / "k", "")
    stubs.ec2.add_response(
        "describe_key_pairs", {"KeyPairs": [a_pair("a", public_key)]}, {"IncludePublicKey": True}
    )

    keypairs.show("./k.pub", None)

    stubs.done()
    assert capsys.readouterr().err.splitlines() == [
        ENVIRONMENT,
        "key file ~/k.pub:",
        "  kind:        public key",
        "  type:        ED25519, 256 bits",
        "  comment:     (none)",
        f"  fingerprint: {keys.fingerprint(public_key)}",
        f"  public key:  {public_key}",
        "  key pairs:   a",
    ]


def test_show_a_locked_key_uses_the_pub_next_to_it(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = home_dir / "locked"
    locked_key(path)
    public_key = keys.public_path(path).read_text(encoding="utf-8").strip()
    stubs.ec2.add_response(
        "describe_key_pairs", {"KeyPairs": [a_pair("a", public_key)]}, {"IncludePublicKey": True}
    )

    keypairs.show(str(path), None)

    stubs.done()
    err = capsys.readouterr().err.splitlines()
    assert f"  fingerprint: {keys.fingerprint(public_key)}" in err
    assert "  comment:     locked" in err
    assert any(
        line.startswith("  .pub:        used instead; cannot read the private key: ssh-keygen")
        for line in err
    )
    assert "  key pairs:   a" in err


def test_show_a_locked_key_without_a_pub(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """公開鍵が得られなければ、キーペアとは突き合わせず、取り出せない理由だけを出す。"""
    path = home_dir / "locked"
    locked_key(path)
    keys.public_path(path).unlink()

    keypairs.show(str(path), None)

    stubs.done()
    err = capsys.readouterr().err.splitlines()
    assert err[:3] == [ENVIRONMENT, "key file ~/locked:", "  kind:       private key"]
    assert err[3].startswith("  public key: cannot read: ssh-keygen")
    assert len(err) == 4


def test_show_a_file_that_is_not_a_key(stubs: Stubs, home_dir: Path) -> None:
    path = home_dir / "notes"
    path.write_text("hello world\n", encoding="utf-8")
    with pytest.raises(
        TemtError, match=f"^not an ssh key: {re.escape(str(path))}: not a public key"
    ):
        keypairs.show(str(path), None)


def test_show_a_file_that_does_not_exist(stubs: Stubs, home_dir: Path) -> None:
    """ファイルがあることは、AWS に問い合わせる前に確かめる。"""
    path = home_dir / "nothing"
    with pytest.raises(TemtError, match=f"^no such file: {re.escape(str(path))}$"):
        keypairs.show(str(path), None)
    assert stubs.regions == []


def test_show_a_directory(stubs: Stubs, home_dir: Path) -> None:
    with pytest.raises(TemtError, match=f"^not a file: {re.escape(str(home_dir))}$"):
        keypairs.show(f"{home_dir}/", None)


def test_create_makes_a_key_and_imports_it(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home_dir
    looked_up(stubs, "work")
    # 作る鍵の中身は先に決まらないので、送る公開鍵そのものはここでは照合しない
    stubs.ec2.add_response(
        "import_key_pair",
        {"KeyName": "work", "KeyPairId": "key-0", "KeyFingerprint": "x"},
        {
            "KeyName": "work",
            "PublicKeyMaterial": ANY,
            "TagSpecifications": [
                {"ResourceType": "key-pair", "Tags": [{"Key": "temt:keypair", "Value": "work"}]}
            ],
        },
    )

    keypairs.create("work", "us-east-1")

    stubs.done()
    path = keys_dir() / "work"
    assert path.stat().st_mode & 0o777 == 0o600
    public_key = keys.public_key(path)
    assert public_key.endswith(" work")
    err = capsys.readouterr().err
    assert "created the key ~/.ssh/temt/keys/work\n" in err
    assert f"imported the key pair work (key-0), {keys.fingerprint(public_key)}\n" in err


@pytest.mark.parametrize(
    ("existing", "hint"),
    [
        ("work", ", or set KeyName to work and run apply to register the existing key"),
        # 公開鍵だけでは apply は登録できないので、名前を変えるよう告げるだけにする
        ("work.pub", ""),
    ],
)
def test_create_stops_when_a_local_key_exists(
    stubs: Stubs, home_dir: Path, existing: str, hint: str
) -> None:
    _ = home_dir
    keys_dir().mkdir(parents=True)
    (keys_dir() / existing).write_text("x\n", encoding="utf-8")
    with pytest.raises(TemtError) as caught:
        keypairs.create("work", None)
    assert str(caught.value) == (
        f"~/.ssh/temt/keys/{existing} already exists; choose another name{hint}"
    )
    stubs.done()


def test_create_stops_when_the_key_pair_exists(stubs: Stubs, home_dir: Path) -> None:
    _ = home_dir
    looked_up(stubs, "work", a_pair("work", "ssh-ed25519 AAAA"))
    with pytest.raises(TemtError, match="^the key pair work already exists in EC2$"):
        keypairs.create("work", None)
    stubs.done()
    assert not (keys_dir() / "work").exists()


def test_create_removes_the_key_when_aws_refuses(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """断られたら作った鍵を消し、もう一度 create できるようにする。消したことを知らせる。"""
    _ = home_dir
    looked_up(stubs, "work")
    stubs.ec2.add_client_error("import_key_pair", "InvalidKeyPair.Duplicate")
    with pytest.raises(ClientError):
        keypairs.create("work", None)
    stubs.done()
    assert not (keys_dir() / "work").exists()
    assert not (keys_dir() / "work.pub").exists()
    assert "removed the key ~/.ssh/temt/keys/work\n" in capsys.readouterr().err


def test_delete_removes_the_key_pair_and_the_local_key(
    stubs: Stubs,
    home_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """使われていても止めず、警告してから確かめる。"""
    _ = home_dir
    public_key = make_key(keys_dir() / "temt")
    looked_up(stubs, "temt", a_pair("temt", public_key))
    templates(stubs, **{"temt-dev-main-template": [(4, "temt")]})
    instances(stubs, "temt", ("i-1", "running"), ("i-2", "stopped"))
    stubs.ec2.add_response("delete_key_pair", {}, {"KeyPairId": "key-temt"})
    monkeypatch.setattr(sys, "stdin", Terminal("y\n"))

    keypairs.delete("temt", "us-east-1", yes=False)

    stubs.done()
    assert not (keys_dir() / "temt").exists()
    assert not (keys_dir() / "temt.pub").exists()
    err = capsys.readouterr().err.splitlines()
    assert err[:8] == [
        ENVIRONMENT,
        "temt: warning: the key pair temt is used by temt-dev-main-template version 4",
        "temt: warning: instances launched with the key pair temt: i-1 (running), "
        "i-2 (stopped); you may be unable to log in to them",
        "temt: warning: ~/.ssh/temt/keys/temt is shared by every region and account; "
        "if the key is also registered elsewhere, run 'temt keypair delete temt' there "
        "too, or apply there stops with a key mismatch",
        "delete:",
        "  key pair temt (key-temt)",
        "  ~/.ssh/temt/keys/temt",
        "  ~/.ssh/temt/keys/temt.pub",
    ]
    assert err[8].startswith("delete the key pair temt and its local key? [y/N] ")
    assert err[8].endswith("deleted the key pair temt (key-temt)")
    assert err[9].endswith("deleted ~/.ssh/temt/keys/temt")
    assert err[10].endswith("deleted ~/.ssh/temt/keys/temt.pub")


def test_delete_without_a_local_key(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home_dir
    looked_up(stubs, "temt", a_pair("temt", "ssh-ed25519 AAAA"))
    templates(stubs)
    instances(stubs, "temt")
    stubs.ec2.add_response("delete_key_pair", {}, {"KeyPairId": "key-temt"})

    keypairs.delete("temt", None, yes=True)

    stubs.done()
    err = capsys.readouterr().err.splitlines()
    assert err[1:3] == ["delete:", "  key pair temt (key-temt)"]
    assert err[3].endswith("deleted the key pair temt (key-temt)")
    assert len(err) == 4


def test_delete_leaves_a_key_pair_without_the_tag(stubs: Stubs, home_dir: Path) -> None:
    _ = home_dir
    public_key = make_key(keys_dir() / "legacy")
    looked_up(stubs, "legacy", a_pair("legacy", public_key, tags=[TAG | {"Key": "other"}]))
    with pytest.raises(
        TemtError,
        match="^the key pair legacy has no temt:keypair tag; "
        "temt deletes only key pairs that it created$",
    ):
        keypairs.delete("legacy", None, yes=True)
    stubs.done()
    assert (keys_dir() / "legacy").exists()


def test_delete_a_key_pair_that_does_not_exist(stubs: Stubs) -> None:
    looked_up(stubs, "nothing")
    with pytest.raises(TemtError, match="^no key pair named nothing$"):
        keypairs.delete("nothing", None, yes=True)
    stubs.done()


def test_delete_keeps_everything_when_not_confirmed(
    stubs: Stubs, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = home_dir
    public_key = make_key(keys_dir() / "temt")
    looked_up(stubs, "temt", a_pair("temt", public_key))
    templates(stubs)
    instances(stubs, "temt")
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    with pytest.raises(TemtError, match="^cancelled$"):
        keypairs.delete("temt", None, yes=False)
    stubs.done()
    assert (keys_dir() / "temt").exists()


def test_delete_keeps_the_local_key_when_aws_refuses(stubs: Stubs, home_dir: Path) -> None:
    _ = home_dir
    public_key = make_key(keys_dir() / "temt")
    looked_up(stubs, "temt", a_pair("temt", public_key))
    templates(stubs)
    instances(stubs, "temt")
    stubs.ec2.add_client_error("delete_key_pair", "UnauthorizedOperation")
    with pytest.raises(ClientError):
        keypairs.delete("temt", None, yes=True)
    stubs.done()
    assert (keys_dir() / "temt").exists()
    assert (keys_dir() / "temt.pub").exists()


def test_show_a_tagged_key_pair_without_a_public_key(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = a_pair("temt", "")
    del pair["PublicKey"]
    looked_up(stubs, "temt", pair)
    templates(stubs)

    keypairs.show("temt", None)

    stubs.done()
    err = capsys.readouterr().err.splitlines()
    assert "  tags:           temt:keypair=temt" in err
    assert "  fingerprint:    (unknown)" in err
    assert "  public key:     (unknown)" in err
    assert "  local keys:     (none)" in err


def test_list_without_a_public_key_does_not_compare(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """公開鍵が得られなければ、手元の鍵と一致するかは分からないとする。"""
    _ = home_dir
    _ = make_key(keys_dir() / "temt")
    pair = a_pair("temt", "")
    del pair["PublicKey"]
    stubs.ec2.add_response("describe_key_pairs", {"KeyPairs": [pair]}, None)
    templates(stubs)

    keypairs.list_keypairs(None, None)

    stubs.done()
    assert capsys.readouterr().out.splitlines()[-1].split() == [
        "temt",
        "-",
        *SHOWN.split(),
        "unknown",
        "-",
    ]


def test_list_does_not_look_outside_the_keys_directory(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """手でタグを付けたキーペアの名前が規則に合わなければ、手元の鍵を探さない。"""
    public_key = make_key(home_dir / "outside")
    stubs.ec2.add_response(
        "describe_key_pairs", {"KeyPairs": [a_pair("../../../outside", public_key)]}, None
    )
    templates(stubs)

    keypairs.list_keypairs(None, None)

    stubs.done()
    assert capsys.readouterr().out.splitlines()[-1].split()[-2:] == ["-", "-"]


def test_show_a_private_key_whose_pub_is_not_a_public_key(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = home_dir / "k"
    _ = make_key(path)
    keys.public_path(path).unlink()
    keys.public_path(path).mkdir()
    stubs.ec2.add_response("describe_key_pairs", {"KeyPairs": []}, {"IncludePublicKey": True})

    keypairs.show(str(path), None)

    stubs.done()
    assert "  .pub:        ~/k.pub is not a readable public key\n" in capsys.readouterr().err


def test_show_a_public_key_whose_comment_mentions_a_private_key(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """秘密鍵かどうかは、コメントでなく -----BEGIN ... PRIVATE KEY----- の行で見る。"""
    public_key = make_key(home_dir / "k", "not a PRIVATE KEY")
    stubs.ec2.add_response("describe_key_pairs", {"KeyPairs": []}, {"IncludePublicKey": True})

    keypairs.show(str(home_dir / "k.pub"), None)

    stubs.done()
    err = capsys.readouterr().err
    assert "  kind:        public key\n" in err
    assert f"  public key:  {public_key}\n" in err


def test_show_names_the_pub_when_it_is_broken(stubs: Stubs, home_dir: Path) -> None:
    path = home_dir / "locked"
    locked_key(path)
    keys.public_path(path).write_text("garbage here\n", encoding="utf-8")
    with pytest.raises(TemtError, match=f"^not an ssh key: {re.escape(str(path))}\\.pub: "):
        keypairs.show(str(path), None)


def test_create_keeps_the_key_when_aws_does_not_answer(
    stubs: Stubs, home_dir: Path, monkeypatch: pytest.MonkeyPatch, ec2: EC2Client
) -> None:
    """通信が切れたときは登録されたか分からないので、作った鍵を残す。"""
    _ = home_dir
    looked_up(stubs, "work")

    def unreachable(**kwargs: Any) -> Any:
        _ = kwargs
        raise EndpointConnectionError(endpoint_url="https://ec2")

    monkeypatch.setattr(ec2, "import_key_pair", unreachable)
    with pytest.raises(EndpointConnectionError):
        keypairs.create("work", None)
    assert (keys_dir() / "work").exists()
    assert (keys_dir() / "work.pub").exists()


def test_users_skip_a_template_that_goes_away(
    stubs: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    looked_up(stubs, "temt", a_pair("temt", "ssh-ed25519 AAAA"))
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": "gone"}, {"LaunchTemplateName": "here"}]},
        {},
    )
    stubs.ec2.add_client_error(
        "describe_launch_template_versions",
        "InvalidLaunchTemplateName.NotFoundException",
        expected_params={"LaunchTemplateName": "gone"},
    )
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {"VersionNumber": 1, "LaunchTemplateData": {"KeyName": "temt"}}
            ]
        },
        {"LaunchTemplateName": "here"},
    )

    keypairs.show("temt", None)

    stubs.done()
    assert "  used by:        here version 1\n" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_show_a_locked_key_whose_pub_cannot_be_read(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """代わりの .pub も読めなければ、秘密鍵から取り出せない理由を出す。"""
    path = home_dir / "locked"
    locked_key(path)
    keys.public_path(path).chmod(0)
    try:
        keypairs.show(str(path), None)
    finally:
        keys.public_path(path).chmod(0o644)

    err = capsys.readouterr().err.splitlines()
    assert err[1:3] == ["key file ~/locked:", "  kind:       private key"]
    assert err[3].startswith("  public key: cannot read: ssh-keygen")


def test_list_compares_the_local_key_without_the_fingerprint_column(
    stubs: Stubs, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """LOCAL だけを選んでも、フィンガープリントを計算して照合する。"""
    _ = home_dir
    mine = make_key(keys_dir() / "temt")
    stubs.ec2.add_response("describe_key_pairs", {"KeyPairs": [a_pair("temt", mine)]}, None)

    keypairs.list_keypairs(None, "local")

    stubs.done()
    assert capsys.readouterr().out.splitlines() == ["LOCAL", "matches"]
