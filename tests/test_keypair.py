"""キーペアを揃える処理のテスト。ssh-keygen は実際に呼び、AWS は Stubber で差し替える。"""

import io
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from types_boto3_ec2.client import EC2Client

from temt import keypair, keys
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import Service, keys_dir

KEY_NAME = "temt"
LOOKUP = {"KeyNames": [KEY_NAME], "IncludePublicKey": True}
# temt が作るキーペアに付けるタグ
TAGS = [{"ResourceType": "key-pair", "Tags": [{"Key": "temt:keypair", "Value": KEY_NAME}]}]
IMPORTED = {"KeyName": KEY_NAME, "KeyPairId": "key-0123456789abcdef0", "KeyFingerprint": "x"}


class Terminal(io.StringIO):
    """端末につながった標準入力のふり。"""

    def isatty(self) -> bool:
        return True


def make_conf(service: Service, *, identity_file: str = "") -> Conf:
    """設定ファイルを置き、その内容を映した Conf を返す。"""
    text = f'KeyName = "{KEY_NAME}"\n'
    if identity_file:
        text += f'IdentityFile = "{identity_file}"\n'
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(text, encoding="utf-8")
    if identity_file:
        return Conf(KeyName=KEY_NAME, IdentityFile=identity_file)
    return Conf(KeyName=KEY_NAME)


def make_key(path: Path) -> str:
    """鍵を 1 つ作り、その公開鍵の 1 行を返す。"""
    keys.generate(path, KEY_NAME)
    return keys.public_key(path)


def spare() -> Path:
    """手がかりとして挙がる場所にある、もう 1 つの鍵のパス。"""
    return Path.home() / ".ssh" / "id_ed25519"


def having(public_key: str) -> dict[str, object]:
    """その公開鍵を持つキーペアが 1 つある応答。"""
    return {"KeyPairs": [{"KeyName": KEY_NAME, "KeyPairId": "key-0", "PublicKey": public_key}]}


def ensure(ec2: EC2Client, service: Service, values: Conf) -> None:
    """apply と同じく、差分を検出してから、その差分で揃える。"""
    keypair.ensure(ec2, service, values, keypair.detect(ec2, service, values))


def stopped(service: Service) -> str:
    """鍵が食い違って止まるときのエラーの全文。"""
    return (
        f"the key pair {KEY_NAME} does not match; "
        f"edit IdentityFile or KeyName in {service.conf_path} and run apply again"
    )


def not_found(stub: Stubber) -> None:
    """キーペアが無いときの応答を積む。"""
    stub.add_client_error(
        "describe_key_pairs",
        service_error_code=keypair.NOT_FOUND,
        service_message=f"The key pair '{KEY_NAME}' does not exist",
        http_status_code=400,
        expected_params=LOOKUP,
    )


def test_creates_and_imports_when_nothing_exists(ec2: EC2Client, service: Service) -> None:
    values = make_conf(service)
    path = keys_dir() / KEY_NAME
    with Stubber(ec2) as stub:
        not_found(stub)
        # 作る鍵の中身は先に決まらないので、送る公開鍵そのものはここでは照合しない
        stub.add_response(
            "import_key_pair",
            IMPORTED,
            {"KeyName": KEY_NAME, "PublicKeyMaterial": ANY, "TagSpecifications": TAGS},
        )
        ensure(ec2, service, values)
        stub.assert_no_pending_responses()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_suffix(".pub").exists()


def test_imports_the_key_that_is_already_here(ec2: EC2Client, service: Service) -> None:
    values = make_conf(service)
    path = keys_dir() / KEY_NAME
    public_key = make_key(path)
    before = path.read_text(encoding="utf-8")
    with Stubber(ec2) as stub:
        not_found(stub)
        stub.add_response(
            "import_key_pair",
            IMPORTED,
            {
                "KeyName": KEY_NAME,
                "PublicKeyMaterial": public_key.encode(),
                "TagSpecifications": TAGS,
            },
        )
        ensure(ec2, service, values)
        stub.assert_no_pending_responses()
    # 既にある鍵は作り直さない
    assert path.read_text(encoding="utf-8") == before


def test_does_nothing_when_the_fingerprints_agree(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    values = make_conf(service)
    path = keys_dir() / KEY_NAME
    before = service.conf_path.read_text(encoding="utf-8")
    with Stubber(ec2) as stub:
        # 登録の応答は積まないので、登録しようとすれば Stubber が失敗する
        stub.add_response("describe_key_pairs", having(make_key(path)), LOOKUP)
        ensure(ec2, service, values)
        stub.assert_no_pending_responses()
    # 一致したときは、設定ファイルを書き換えず、何も知らせない
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert capsys.readouterr() == ("", "")


def test_the_comment_does_not_take_part_in_the_comparison(ec2: EC2Client, service: Service) -> None:
    """EC2 が返す公開鍵の末尾のコメントが違っても、同じ鍵なら一致とする。"""
    values = make_conf(service)
    path = keys_dir() / KEY_NAME
    fields = make_key(path).split()
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(f"{fields[0]} {fields[1]}"), LOOKUP)
        ensure(ec2, service, values)
        stub.assert_no_pending_responses()


def test_a_mismatch_stops_and_names_the_matching_key(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """食い違えば、両方のフィンガープリントと、キーペアと一致する手元の鍵を挙げて止める。

    設定ファイルの IdentityFile は書き換えない。
    """
    values = make_conf(service)
    mine = keys.fingerprint(make_key(keys_dir() / KEY_NAME))
    remote = make_key(spare())
    before = service.conf_path.read_text(encoding="utf-8")
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(remote), LOOKUP)
        with pytest.raises(TemtError) as caught:
            ensure(ec2, service, values)
        stub.assert_no_pending_responses()
    assert str(caught.value) == stopped(service)
    out, err = capsys.readouterr()
    # 手がかりも標準エラー出力に書く
    assert out == ""
    assert err.splitlines() == [
        f"temt: warning: the key pair {KEY_NAME} does not match ~/.ssh/temt/keys/{KEY_NAME}",
        f"  local: {mine}",
        f"  EC2:   {keys.fingerprint(remote)}",
        f"keys that match the key pair {KEY_NAME}:",
        "  ~/.ssh/id_ed25519",
    ]
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert values.IdentityFile == f"~/.ssh/temt/keys/{KEY_NAME}"


def test_a_mismatch_does_not_ask_even_on_a_terminal(
    ec2: EC2Client, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """標準入力が端末でも問いかけず、一致する鍵があっても選ばずに止める。"""
    values = make_conf(service)
    make_key(keys_dir() / KEY_NAME)
    answers = Terminal("1\n")
    monkeypatch.setattr(sys, "stdin", answers)
    before = service.conf_path.read_text(encoding="utf-8")
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(make_key(spare())), LOOKUP)
        with pytest.raises(TemtError, match="does not match; edit IdentityFile or KeyName in"):
            ensure(ec2, service, values)
    # 標準入力は読まない
    assert answers.read() == "1\n"
    assert service.conf_path.read_text(encoding="utf-8") == before


def test_a_missing_local_key_is_a_mismatch(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """キーペアがあるのに手元の鍵が無ければ、鍵を作らずに止める。"""
    values = make_conf(service)
    remote = make_key(Path.home() / "other")
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(remote), LOOKUP)
        with pytest.raises(TemtError) as caught:
            ensure(ec2, service, values)
    assert str(caught.value) == stopped(service)
    # 手元の鍵のフィンガープリントは無いので出さない。一致する鍵が無いことも知らせる
    assert capsys.readouterr().err.splitlines() == [
        f"temt: warning: the key pair {KEY_NAME} exists, but ~/.ssh/temt/keys/{KEY_NAME} does not",
        f"  EC2:   {keys.fingerprint(remote)}",
        f"no key on this machine matches the key pair {KEY_NAME}",
    ]
    assert not (keys_dir() / KEY_NAME).exists()


def test_a_missing_local_key_names_the_matching_key_without_writing_it(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """手元の鍵が無く、一致する鍵が別の場所にあっても、IdentityFile に書かずに止める。"""
    values = make_conf(service)
    before = service.conf_path.read_text(encoding="utf-8")
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(make_key(spare())), LOOKUP)
        with pytest.raises(TemtError) as caught:
            ensure(ec2, service, values)
    assert str(caught.value) == stopped(service)
    assert capsys.readouterr().err.splitlines()[-2:] == [
        f"keys that match the key pair {KEY_NAME}:",
        "  ~/.ssh/id_ed25519",
    ]
    assert service.conf_path.read_text(encoding="utf-8") == before
    assert values.IdentityFile == f"~/.ssh/temt/keys/{KEY_NAME}"
    assert not (keys_dir() / KEY_NAME).exists()


def test_keys_that_cannot_be_read_are_not_named(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """読めない鍵は手がかりに挙げず、読める鍵のうち一致するものだけを挙げる。"""
    values = make_conf(service)
    make_key(keys_dir() / KEY_NAME)
    (keys_dir() / "notes.txt").write_text("hello\n", encoding="utf-8")
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(make_key(keys_dir() / "spare")), LOOKUP)
        with pytest.raises(TemtError, match="does not match"):
            ensure(ec2, service, values)
    assert capsys.readouterr().err.splitlines()[-2:] == [
        f"keys that match the key pair {KEY_NAME}:",
        "  ~/.ssh/temt/keys/spare",
    ]


def test_every_matching_key_is_named_in_order(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """一致する鍵はすべて挙げる。keys/ の鍵が先、~/.ssh/id_* が後。"""
    values = make_conf(service)
    make_key(keys_dir() / KEY_NAME)
    public_key = make_key(keys_dir() / "spare")
    # 同じ鍵を 2 か所に置く
    spare().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    spare().write_bytes((keys_dir() / "spare").read_bytes())
    spare().chmod(0o600)
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(public_key), LOOKUP)
        with pytest.raises(TemtError, match="does not match"):
            ensure(ec2, service, values)
    assert capsys.readouterr().err.splitlines()[-3:] == [
        f"keys that match the key pair {KEY_NAME}:",
        "  ~/.ssh/temt/keys/spare",
        "  ~/.ssh/id_ed25519",
    ]


def test_the_identity_file_in_the_config_is_used(ec2: EC2Client, service: Service) -> None:
    values = make_conf(service, identity_file="~/.ssh/id_ed25519")
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(make_key(spare())), LOOKUP)
        ensure(ec2, service, values)
        stub.assert_no_pending_responses()
    # 既定の場所には鍵を作らない
    assert not (keys_dir() / KEY_NAME).exists()


def test_the_identity_file_is_used_as_written_in_the_config(
    ec2: EC2Client, service: Service
) -> None:
    """ssh の設定には % を %% にして書くが、鍵は設定ファイルに書いたパスに作る。"""
    values = make_conf(service, identity_file="~/my keys/50%off")
    with Stubber(ec2) as stub:
        not_found(stub)
        stub.add_response(
            "import_key_pair",
            IMPORTED,
            {"KeyName": KEY_NAME, "PublicKeyMaterial": ANY, "TagSpecifications": TAGS},
        )
        ensure(ec2, service, values)
        stub.assert_no_pending_responses()
    assert (Path.home() / "my keys" / "50%off").exists()


def test_a_key_pair_without_a_public_key(ec2: EC2Client, service: Service) -> None:
    values = make_conf(service)
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", {"KeyPairs": [{"KeyName": KEY_NAME}]}, LOOKUP)
        with pytest.raises(TemtError, match="did not return the public key"):
            ensure(ec2, service, values)


def test_other_aws_errors_are_left_to_apply(ec2: EC2Client, service: Service) -> None:
    values = make_conf(service)
    with Stubber(ec2) as stub:
        stub.add_client_error(
            "describe_key_pairs", service_error_code="UnauthorizedOperation", http_status_code=403
        )
        with pytest.raises(ClientError):
            ensure(ec2, service, values)


def test_a_key_that_cannot_be_read_is_reported(
    ec2: EC2Client, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """手がかりから外した鍵は、理由とともに知らせる。"""
    values = make_conf(service)
    make_key(keys_dir() / KEY_NAME)
    (keys_dir() / "notes.txt").write_text("hello\n", encoding="utf-8")
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(make_key(Path.home() / "other")), LOOKUP)
        with pytest.raises(TemtError, match="does not match"):
            ensure(ec2, service, values)
    shown = capsys.readouterr().err
    assert "skipped ~/.ssh/temt/keys/notes.txt: ssh-keygen:" in shown
    assert f"no key on this machine matches the key pair {KEY_NAME}" in shown


def test_an_identity_file_that_cannot_be_read_says_where_to_fix_it(
    ec2: EC2Client, service: Service
) -> None:
    values = make_conf(service)
    (keys_dir()).mkdir(parents=True)
    (keys_dir() / KEY_NAME).write_text("not a key\n", encoding="utf-8")
    with Stubber(ec2) as stub:
        not_found(stub)
        with pytest.raises(TemtError, match="edit IdentityFile or KeyName in"):
            ensure(ec2, service, values)


def test_a_public_key_that_ec2_returns_in_an_unknown_form(ec2: EC2Client, service: Service) -> None:
    values = make_conf(service)
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having("---- BEGIN SSH2 PUBLIC KEY ----"), LOOKUP)
        with pytest.raises(TemtError, match=f"the key pair {KEY_NAME} in EC2: not a public key"):
            ensure(ec2, service, values)


def test_detect_writes_nothing_and_shows_the_key_and_the_key_pair_to_make(
    ec2: EC2Client, service: Service
) -> None:
    """手元の鍵も EC2 のキーペアも無ければ、作る鍵と登録するキーペアを出す。どちらも作らない。"""
    values = make_conf(service)
    with Stubber(ec2) as stub:
        not_found(stub)
        found = keypair.detect(ec2, service, values)
        stub.assert_no_pending_responses()
    assert not (keys_dir() / KEY_NAME).exists()
    assert (found.register, found.generate) == (True, True)
    assert [(one.mark, one.kind, one.name, one.detail) for one in found.changes()] == [
        ("+", keypair.KEY, f"~/.ssh/temt/keys/{KEY_NAME}", "ssh-keygen -t ed25519"),
        ("+", keypair.KEYPAIR, KEY_NAME, f"from ~/.ssh/temt/keys/{KEY_NAME}"),
    ]


def test_detect_shows_only_the_key_pair_when_the_key_is_here(
    ec2: EC2Client, service: Service
) -> None:
    values = make_conf(service)
    make_key(keys_dir() / KEY_NAME)
    with Stubber(ec2) as stub:
        not_found(stub)
        found = keypair.detect(ec2, service, values)
    assert [(one.mark, one.kind) for one in found.changes()] == [("+", keypair.KEYPAIR)]


def test_detect_shows_nothing_when_the_fingerprints_agree(ec2: EC2Client, service: Service) -> None:
    values = make_conf(service)
    public_key = make_key(keys_dir() / KEY_NAME)
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(public_key), LOOKUP)
        assert keypair.detect(ec2, service, values).changes() == []


@pytest.mark.parametrize("local", [True, False], ids=["differs", "missing"])
def test_detect_stops_on_a_mismatch(ec2: EC2Client, service: Service, *, local: bool) -> None:
    """食い違いは検出の段階で止める。diff も同じエラーで終わる。鍵は作らない。"""
    values = make_conf(service)
    if local:
        make_key(keys_dir() / KEY_NAME)
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(make_key(spare())), LOOKUP)
        with pytest.raises(TemtError) as caught:
            _ = keypair.detect(ec2, service, values)
        stub.assert_no_pending_responses()
    assert str(caught.value) == stopped(service)
    assert (keys_dir() / KEY_NAME).exists() == local


def test_a_mismatch_before_the_config_file_exists_asks_for_a_new_file(
    ec2: EC2Client, service: Service
) -> None:
    """初回の apply は確認の前に止まるので、設定ファイルはまだ無い。直すファイルでは
    なく、書いて用意するファイルとして告げる。"""
    values = Conf(KeyName=KEY_NAME)
    with Stubber(ec2) as stub:
        stub.add_response("describe_key_pairs", having(make_key(spare())), LOOKUP)
        with pytest.raises(
            TemtError,
            match=f"write IdentityFile or KeyName in a new {service.conf_path} and run apply",
        ):
            _ = keypair.detect(ec2, service, values)
    assert not service.conf_path.exists()
