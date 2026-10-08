"""proxy の本体のテスト。AWS は Stubber で、exec と外部コマンドの確認と ssh -G は偽物に
差し替える。

端から端まで（proxy.run か cli.main）を通し、読む順序と、exec するコマンドラインか中継する
接続を確かめる。TCP の接続と中継も偽物に差し替え、実際のソケットは開かない。
instances.connect 自身は test_instances で、relay.run 自身は test_relay で、ssh -G の読み方は
test_keys で確かめる。
"""

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    API_CALL,
    ASSOCIATION,
    OLD_VERSION,
    SSHD,
    Clock,
    Stubs,
    an_instance,
    association_ran,
    listed_associations,
    reservations,
    taken,
)
from types_boto3_ec2_instance_connect.client import EC2InstanceConnectClient

from temt import aws, cli, conf, home, instances, keys, msg, proc, proxy, relay, wait
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
# 名前が同じ、別の関連付け <名前>-sshd の ID
OTHER_ASSOCIATION = "99999999-8888-7777-6666-555555555555"
ADDRESS = "203.0.113.10"
REGION = "ap-northeast-1"

# インスタンスの探索。探す状態の一覧は設計のとおりに固定する
SEARCH = {
    "Filters": [
        {"Name": "tag:Name", "Values": [NAME]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        },
    ]
}
BY_ID = {"InstanceIds": [INSTANCE]}
# SSM エージェントの問い合わせ
AGENT = {"Filters": [{"Key": "InstanceIds", "Values": [INSTANCE]}]}
# Session Manager 経由の、プロファイル無しのコマンドライン
SSM_ARGV = [
    "aws",
    "ssm",
    "start-session",
    "--target",
    INSTANCE,
    "--document-name",
    "AWS-StartSSHSession",
    "--parameters",
    "portNumber=22",
    "--region",
    REGION,
]

# ssh -G が出す、ログインするユーザーと鍵。鍵は temt の既定の鍵
LOGIN_USER = "ubuntu"
SSH_G = [f"user {LOGIN_USER}", "identityfile ~/.ssh/temt/keys/temt"]
# 既定の鍵の公開鍵。ファイルにはコメントも書くが、送るのは種類と本体だけである
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKfJ0gnyLzVY4oq0bQ1Zzv1g9f8bO4m7mI5x2kYpQXcP"
# EC2 Instance Connect に送るもの
SEND = {"InstanceId": INSTANCE, "InstanceOSUser": LOGIN_USER, "SSHPublicKey": PUBLIC_KEY}

# 起動した時刻。LOGIN_DEADLINE の内なら起動して間もないとみなし、つなげるまで待つ。
# LaunchTime が無いインスタンスは、起動から時間がたったものとして扱われる
JUST_NOW = timedelta(seconds=10)
LONG_AGO = timedelta(hours=1)

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(err: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in err.splitlines()]


def launched_at(item: dict[str, Any], ago: timedelta) -> dict[str, Any]:
    """describe-instances の応答の 1 台分に、今から ago だけ前の起動時刻を足す。"""
    return {**item, "LaunchTime": datetime.now(UTC) - ago}


def found(stubs: Stubs, *items: dict[str, Any]) -> None:
    """探索で見つかるインスタンスを積む。"""
    stubs.ec2.add_response("describe_instances", reservations(*items), SEARCH)


def agent(stubs: Stubs, status: str, pinged: datetime | None = None) -> None:
    """describe-instance-information の応答を積む。status が空なら未登録。

    最後の応答の時刻は pinged で、省けば今である。
    """
    listed = (
        [
            {
                "InstanceId": INSTANCE,
                "PingStatus": status,
                "LastPingDateTime": pinged or datetime.now(UTC),
            }
        ]
        if status
        else []
    )
    stubs.ssm.add_response(
        "describe_instance_information", {"InstanceInformationList": listed}, AGENT
    )


def sshd_ran(stubs: Stubs, *runs: dict[str, Any]) -> None:
    """describe-instance-associations-status の応答を積む。runs が無ければ一覧に無い。"""
    stubs.ssm.add_response(
        "describe_instance_associations_status",
        {"InstanceAssociationStatusInfos": list(runs)},
        {"InstanceId": INSTANCE},
    )


def sshd_applied(stubs: Stubs) -> None:
    """関連付け <名前>-sshd があり、今の版がそのインスタンスで Success だった応答を積む。"""
    listed_associations(stubs.ssm, ASSOCIATION)
    sshd_ran(stubs, association_ran("Success"))


def key_sent(stubs: Stubs, params: dict[str, Any] = SEND) -> None:
    """send-ssh-public-key の応答を積む。既定では、ssh -G が出したユーザーと既定の鍵を送る。"""
    stubs.connect.add_response(
        "send_ssh_public_key", {"RequestId": "11111111-2222", "Success": True}, params
    )


def ready(stubs: Stubs) -> None:
    """アドレスの付いた running のインスタンスと、Online のエージェントと、鍵を送った応答を
    積む。

    ssm の経路で、待たずにつなげる姿である。
    """
    found(stubs, an_instance(INSTANCE, "running", address=ADDRESS))
    agent(stubs, "Online")
    key_sent(stubs)


def running(stubs: Stubs) -> None:
    """アドレスの付いた running のインスタンスと、鍵を送った応答を積む。direct の経路で使う。"""
    found(stubs, an_instance(INSTANCE, "running", address=ADDRESS))
    key_sent(stubs)


# つながらなかったときの理由の例。待つ側から見れば、どれもまだつながらない
REFUSED = ConnectionRefusedError(111, "Connection refused")


class Opened:
    """instances.connect が返すソケットの代わり。with で閉じられたかを控える。"""

    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> "Opened":
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def sent(connect: EC2InstanceConnectClient) -> list[dict[str, Any]]:
    """EC2 Instance Connect に鍵を送った控え。送るたびに、渡した引数を足す。

    応答は aws_stubs に積んだものが返る。接続、中継、exec の代わりは、それまでに送った数を
    控える。
    """
    calls: list[dict[str, Any]] = []
    call = getattr(connect, API_CALL)

    def make_api_call(operation_name: str, api_params: dict[str, Any]) -> Any:
        calls.append(api_params)
        return call(operation_name, api_params)

    setattr(connect, API_CALL, make_api_call)
    return calls


@dataclass
class SshConfig:
    """proc.run の代わり。ssh -G に尋ねたコマンドラインを控え、lines を出す。"""

    lines: list[str]
    asked: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str], stdin: str | None = None) -> str:
        assert stdin is None
        self.asked.append(argv)
        return "".join(f"{line}\n" for line in self.lines)


@pytest.fixture(autouse=True)
def ssh_config(monkeypatch: pytest.MonkeyPatch, home_dir: Path) -> SshConfig:
    """ssh -G を差し替え、既定の鍵の公開鍵を置く。

    autouse なので、手元の ssh の設定は読まない。ssh -G は既定の鍵を出す。
    """
    _ = home_dir
    public = keys.public_path(keys.default_path())
    public.parent.mkdir(parents=True)
    public.write_text(f"{PUBLIC_KEY} temt\n", encoding="utf-8")
    fake = SshConfig(list(SSH_G))
    monkeypatch.setattr(proc, "run", fake)
    return fake


@dataclass
class Ports:
    """instances.connect の代わり。つながらなかった理由を順に上げ、尽きたらつなぐ。

    None もつながったことを表す。wait_accepting もこれを通る。つなごうとするたびに、それまでに
    送った鍵の数を控える。
    """

    sent: list[dict[str, Any]] = field(default_factory=list)
    answers: list[OSError | None] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)
    opened: list[Opened] = field(default_factory=list)
    keys_sent: list[int] = field(default_factory=list)

    def __call__(self, address: str, port: int) -> Opened:
        self.calls.append((address, port))
        self.keys_sent.append(len(self.sent))
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            raise answer
        connection = Opened()
        self.opened.append(connection)
        return connection


@pytest.fixture(autouse=True)
def ports(monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]) -> Ports:
    """TCP の接続を差し替える。autouse なので、実際のソケットは開かない。"""
    fake = Ports(sent)
    monkeypatch.setattr(instances, "connect", fake)
    return fake


@dataclass
class Relay:
    """relay.run の代わり。中継した接続を控える。"""

    service: Service
    sent: list[dict[str, Any]]
    calls: list[Opened] = field(default_factory=list)
    # 中継を始めた時点で、ロックが空いていたかどうかと、接続が開いていたかどうかと、
    # known_hosts が指していたインスタンスと、送った鍵の数
    lock_free: list[bool] = field(default_factory=list)
    alive: list[bool] = field(default_factory=list)
    known_hosts: list[Path | None] = field(default_factory=list)
    keys_sent: list[int] = field(default_factory=list)

    def __call__(self, connection: Opened) -> None:
        self.calls.append(connection)
        self.keys_sent.append(len(self.sent))
        self.lock_free.append(taken(self.service))
        self.alive.append(not connection.closed)
        current = self.service.known_hosts_current
        self.known_hosts.append(current.readlink() if current.is_symlink() else None)


@pytest.fixture(autouse=True)
def relays(monkeypatch: pytest.MonkeyPatch, service: Service, sent: list[dict[str, Any]]) -> Relay:
    """中継を差し替える。autouse なので、標準入出力には触れない。"""
    fake = Relay(service, sent)
    monkeypatch.setattr(relay, "run", fake)
    return fake


@dataclass
class Exec:
    """exec の代わり。呼ばれたコマンドラインと環境を控え、戻ってくる。"""

    service: Service
    sent: list[dict[str, Any]]
    calls: list[tuple[str, list[str], dict[str, str]]] = field(default_factory=list)
    # exec の時点でロックが空いていたかどうかと、送った鍵の数
    lock_free: list[bool] = field(default_factory=list)
    keys_sent: list[int] = field(default_factory=list)

    def __call__(self, file: str, argv: list[str], env: dict[str, str]) -> None:
        self.lock_free.append(taken(self.service))
        self.keys_sent.append(len(self.sent))
        self.calls.append((file, argv, env))


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch, service: Service, sent: list[dict[str, Any]]) -> Exec:
    """exec を差し替える。"""
    fake = Exec(service, sent)
    monkeypatch.setattr(proxy, "execvpe", fake)
    return fake


@pytest.fixture
def required(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """外部コマンドの確認を差し替え、確かめた名前を控える。"""
    seen: list[str] = []

    def require(command: str) -> str:
        seen.append(command)
        return f"/usr/bin/{command}"

    monkeypatch.setattr(proc, "require", require)
    return seen


@pytest.fixture(autouse=True)
def only_stderr(process_state: None) -> None:
    """cli.run が proxy の前にするのと同じく、すべてを標準エラー出力に回す。

    proxy.run を直に呼ぶテストも、本番と同じ出力先で確かめる。process_state がテストの
    後に戻す。
    """
    _ = process_state
    msg.use_stderr()


@pytest.fixture(autouse=True)
def no_login_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """手元の TEMT_LOGIN をテストに持ち込まない。無ければ ssm になる。"""
    monkeypatch.delenv(proxy.LOGIN_ENV, raising=False)


def login(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """環境変数 TEMT_LOGIN で経路を選ぶ。"""
    monkeypatch.setenv(proxy.LOGIN_ENV, value)


def profile(service: Service, name: str) -> None:
    """設定ファイルに AwsProfile を書き足す。最上位の項目なので先頭に置く。"""
    text = service.conf_path.read_text(encoding="utf-8")
    service.conf_path.write_text(f'AwsProfile = "{name}"\n{text}', encoding="utf-8")


def test_it_connects_through_session_manager(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    ssh_config: SshConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("AWS_PAGER", raising=False)
    ready(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    # ssm の経路は aws-cli と session-manager-plugin を使う。どちらの経路も、送る鍵を ssh -G で
    # 調べる
    assert required == ["ssh", "aws", "session-manager-plugin"]
    assert ssh_config.asked == [["ssh", "-G", NAME]]
    # 鍵は exec の前に 1 度だけ送る
    assert execs.keys_sent == [1]
    # プロファイルもリージョンも設定ファイルから渡される。AwsProfile が無ければ渡さない
    assert aws_stubs.passed[0].AwsProfile is None
    assert aws_stubs.passed[0].AwsRegion == REGION
    [(file, argv, env)] = execs.calls
    assert (file, argv) == ("aws", SSM_ARGV)
    # aws-cli の出力をページャに渡さない
    assert env["AWS_PAGER"] == ""
    assert env["HOME"] == os.environ["HOME"]
    # known_hosts は exec の前に用意する
    known_hosts = configured.known_hosts_dir / INSTANCE
    assert known_hosts.read_bytes() == b""
    assert known_hosts.stat().st_mode & 0o777 == 0o600
    assert configured.known_hosts_current.readlink() == Path(INSTANCE)
    # ロックは取らない
    assert execs.lock_free == [True]
    captured = capsys.readouterr()
    # 標準出力は ssh の通信路なので、何も書かない
    assert captured.out == ""
    # 待たなければ何も知らせない
    assert captured.err == ""


def test_an_empty_login_means_session_manager(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空の TEMT_LOGIN は、設定ファイルの空の項目と同じく、無いのと同じに扱う。"""
    _ = required
    login(monkeypatch, "")
    ready(aws_stubs)
    proxy.run(configured, 22)
    assert execs.calls[0][1] == SSM_ARGV


def test_an_unknown_login_is_shown_safely(
    aws_stubs: Stubs, configured: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """環境変数の値は、表示できない文字を ? にして誤りに入れる。"""
    login(monkeypatch, "\x1b[2J")
    with pytest.raises(TemtError) as caught:
        proxy.run(configured, 22)
    assert str(caught.value) == "TEMT_LOGIN must be one of ssm, direct: ?[2J"
    assert aws_stubs.passed == []


def test_an_unknown_login_stops_before_touching_aws(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(monkeypatch, "ssh")
    with pytest.raises(TemtError, match="TEMT_LOGIN must be one of ssm, direct: ssh"):
        proxy.run(configured, 22)
    assert required == []
    assert aws_stubs.passed == []
    assert not execs.calls


def test_it_connects_directly(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    ssh_config: SshConfig,
    ports: Ports,
    relays: Relay,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    login(monkeypatch, "direct")
    running(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    # direct の経路は、送る鍵を調べる ssh だけを使う
    assert required == ["ssh"]
    assert ssh_config.asked == [["ssh", "-G", NAME]]
    assert not execs.calls
    # 鍵は、TCP でつないだ後、中継の前に 1 度だけ送る
    assert ports.keys_sent == [0]
    assert relays.keys_sent == [1]
    # SSM エージェントは見ず、そのポートに 1 回だけつなぎ、その接続をそのまま中継する
    assert ports.calls == [(ADDRESS, 22)]
    [connection] = ports.opened
    assert relays.calls == [connection]
    assert relays.alive == [True]
    # 中継し終えたら閉じる
    assert connection.closed
    # known_hosts は中継の前に用意する
    assert relays.known_hosts == [Path(INSTANCE)]
    # ロックは取らない
    assert relays.lock_free == [True]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("ago", [JUST_NOW, LONG_AGO])
def test_it_closes_the_connection_when_the_known_hosts_fail(
    aws_stubs: Stubs,
    configured: Service,
    required: list[str],
    ports: Ports,
    relays: Relay,
    monkeypatch: pytest.MonkeyPatch,
    ago: timedelta,
) -> None:
    """known_hosts を用意できなければ、中継せずに接続を閉じる。

    起動して間もなければ待って得た接続を、時間がたっていれば 1 回でつないだ接続を閉じる。
    鍵は known_hosts の前に送っている。
    """
    _ = required
    login(monkeypatch, "direct")

    def broken(service: Service, instance_id: str) -> Path:
        _ = (service, instance_id)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(home, "use_known_hosts", broken)
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), ago))
    key_sent(aws_stubs)
    with pytest.raises(PermissionError):
        proxy.run(configured, 22)
    assert not relays.calls
    assert ports.opened[0].closed


def test_it_passes_the_port_to_session_manager(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
) -> None:
    _ = required
    ready(aws_stubs)
    proxy.run(configured, 2222)
    aws_stubs.done()
    assert execs.calls[0][1] == [*SSM_ARGV[:8], "portNumber=2222", *SSM_ARGV[9:]]


def test_it_connects_directly_to_the_port(
    aws_stubs: Stubs,
    configured: Service,
    required: list[str],
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = required
    login(monkeypatch, "direct")
    running(aws_stubs)
    proxy.run(configured, 2222)
    aws_stubs.done()
    assert ports.calls == [(ADDRESS, 2222)]


def test_it_passes_the_profile_from_the_config_file(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    _ = required
    profile(configured, "work")
    ready(aws_stubs)
    proxy.run(configured, 22)
    assert aws_stubs.passed[0].AwsProfile == "work"
    assert execs.calls[0][1] == [*SSM_ARGV, "--profile", "work"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"AWS_PROFILE": "work"}, "work"),
        ({"AWS_DEFAULT_PROFILE": "work"}, "work"),
        # boto3 は AWS_DEFAULT_PROFILE を先に読む。aws-cli v2 に任せると other になる
        ({"AWS_DEFAULT_PROFILE": "work", "AWS_PROFILE": "other"}, "work"),
    ],
)
def test_it_aligns_the_profile_in_the_environment(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    expected: str,
) -> None:
    """--profile も AwsProfile も無ければ、引数ではなく環境で boto3 と揃える。

    引数で渡すと、aws-cli は環境変数の鍵や web identity の環境変数を読まなくなる。
    """
    _ = required
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    ready(aws_stubs)
    proxy.run(configured, 22)
    [(_, argv, passed)] = execs.calls
    assert argv == SSM_ARGV
    assert (passed["AWS_DEFAULT_PROFILE"], passed["AWS_PROFILE"]) == (expected, expected)


def test_it_leaves_the_environment_without_a_profile(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    """どこにもプロファイルが無ければ、boto3 が返す default も入れない。"""
    _ = required
    ready(aws_stubs)
    proxy.run(configured, 22)
    [(_, argv, passed)] = execs.calls
    assert argv == SSM_ARGV
    assert not [name for name in aws.PROFILE_ENV if name in passed]


def test_a_chosen_profile_leaves_the_environment(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AwsProfile を引数で渡すときは、環境のプロファイルに触れない。"""
    _ = required
    profile(configured, "work")
    monkeypatch.setenv("AWS_PROFILE", "other")
    ready(aws_stubs)
    proxy.run(configured, 22)
    [(_, argv, passed)] = execs.calls
    assert argv == [*SSM_ARGV, "--profile", "work"]
    assert passed["AWS_PROFILE"] == "other"
    assert "AWS_DEFAULT_PROFILE" not in passed


def test_the_profile_option_wins(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    """--profile は、設定ファイルの AwsProfile より優先して aws-cli に渡る。

    boto3 には、aws.session が conf.OPTION_PROFILE から渡す。
    """
    _ = required
    profile(configured, "work")
    ready(aws_stubs)
    assert cli.main(["temt", "--profile", "other", "proxy", "--svc", "dev-main", "2222"]) == 0
    assert conf.OPTION_PROFILE == "other"
    argv = execs.calls[0][1]
    assert argv[-2:] == ["--profile", "other"]
    assert "portNumber=2222" in argv


def test_it_moves_current_from_the_previous_instance(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    """起動のたびにホスト鍵が変わるので、current は今のインスタンスのファイルを指す。"""
    _ = required
    old = home.use_known_hosts(configured, OTHER)
    old.write_text("old ssh-ed25519 AAAA\n", encoding="utf-8")
    ready(aws_stubs)
    proxy.run(configured, 22)
    assert configured.known_hosts_current.readlink() == Path(INSTANCE)
    assert (configured.known_hosts_dir / INSTANCE).read_bytes() == b""
    assert old.read_text(encoding="utf-8") == "old ssh-ed25519 AAAA\n"
    assert len(execs.calls) == 1


def test_it_stops_when_there_is_no_instance(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    _ = required
    found(aws_stubs)
    with pytest.raises(
        TemtError, match=f"no instance for {NAME}; run 'temt start --svc dev-main' first"
    ):
        proxy.run(configured, 22)
    aws_stubs.done()
    assert not execs.calls
    # known_hosts には触れない
    assert not configured.known_hosts_dir.exists()


def test_it_stops_when_more_than_one_instance_is_tagged(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """2 台以上あれば、挙げて終わる。どちらにもつながない。"""
    _ = required
    found(aws_stubs, an_instance(OTHER, "running"), an_instance(INSTANCE, "running"))
    with pytest.raises(
        TemtError,
        match=(
            f"2 instances for {NAME}; terminate the extra one with "
            "'temt terminate --svc dev-main <instance-id>' and connect again"
        ),
    ):
        proxy.run(configured, 22)
    aws_stubs.done()
    assert not execs.calls
    assert not configured.known_hosts_dir.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert told(captured.err) == [
        f"instances tagged Name={NAME}:",
        f"  {INSTANCE} (running)",
        f"  {OTHER} (running)",
    ]


def test_it_connects_to_the_one_that_is_not_shutting_down(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    """終了の途中のものは、無いものとして扱う。2 台とは数えない。"""
    _ = required
    found(
        aws_stubs,
        an_instance(OTHER, "shutting-down"),
        an_instance(INSTANCE, "running", address=ADDRESS),
    )
    agent(aws_stubs, "Online")
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    [(file, argv, _env)] = execs.calls
    assert (file, argv) == ("aws", SSM_ARGV)
    assert configured.known_hosts_current.readlink() == Path(INSTANCE)


def test_it_treats_an_instance_that_is_shutting_down_as_gone(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    _ = required
    found(aws_stubs, an_instance(INSTANCE, "shutting-down"))
    with pytest.raises(
        TemtError, match=f"no instance for {NAME}; run 'temt start --svc dev-main' first"
    ):
        proxy.run(configured, 22)
    aws_stubs.done()
    assert not execs.calls


@pytest.mark.parametrize("state", ["stopped", "stopping"])
def test_it_stops_when_the_instance_will_not_be_running(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    state: str,
) -> None:
    """running に向かわない状態では待たない。停止の途中でも、start で再開させる。"""
    _ = required
    found(aws_stubs, an_instance(INSTANCE, state))
    with pytest.raises(
        TemtError,
        match=re.escape(f"{INSTANCE} is {state}; run 'temt start --svc dev-main' first"),
    ):
        proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == []
    assert not execs.calls
    assert not configured.known_hosts_dir.exists()


@pytest.mark.parametrize("value", ["ssm", "direct"])
def test_it_waits_for_a_pending_instance(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    ports: Ports,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    """pending なら running になるまで待ち、待っていることを標準エラー出力に知らせる。"""
    _ = required
    login(monkeypatch, value)
    found(aws_stubs, an_instance(INSTANCE, "pending"))
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "pending")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running", address=ADDRESS)), BY_ID
    )
    if value == "ssm":
        agent(aws_stubs, "Online")
        sshd_applied(aws_stubs)
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [5]
    if value == "ssm":
        assert execs.calls[0][1] == SSM_ARGV
    else:
        # 待って得たインスタンスのアドレスにつなぐ
        assert ports.calls == [(ADDRESS, 22)]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert told(captured.err) == [
        f"{INSTANCE} is pending; waiting for it",
        f"{INSTANCE} is running",
    ]


def test_main_writes_the_notices_only_to_stderr(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cli.main から動かせば、待ちの知らせも標準エラー出力に書き、標準出力には何も書かない。

    標準出力は ssh の通信路だからである。
    """
    _ = required
    # cli.run が自ら標準エラー出力に回すことを確かめるため、only_stderr の設定を戻す
    monkeypatch.setattr(msg, "_only_stderr", False)
    found(aws_stubs, an_instance(INSTANCE, "pending"))
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running", address=ADDRESS)), BY_ID
    )
    agent(aws_stubs, "Online")
    sshd_applied(aws_stubs)
    key_sent(aws_stubs)
    assert cli.main(["temt", "proxy", "--svc", "dev-main"]) == 0
    assert execs.calls[0][1] == SSM_ARGV
    captured = capsys.readouterr()
    assert captured.out == ""
    assert told(captured.err) == [
        f"{INSTANCE} is pending; waiting for it",
        f"{INSTANCE} is running",
    ]


def test_main_passes_the_user_from_ssh(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    ssh_config: SshConfig,
) -> None:
    """ProxyCommand の --user '%r' は、cli から proxy まで届き、そのユーザーに鍵を送る。"""
    _ = (configured, required)
    found(aws_stubs, an_instance(INSTANCE, "running", address=ADDRESS))
    agent(aws_stubs, "Online")
    key_sent(aws_stubs, {**SEND, "InstanceOSUser": "admin"})
    assert cli.main(["temt", "proxy", "--svc", "dev-main", "--user", "admin", "22"]) == 0
    assert execs.keys_sent == [1]
    assert ssh_config.asked == [["ssh", "-G", "-l", "admin", NAME]]


def test_main_refuses_a_user_that_breaks_the_rule(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ユーザー名の規則に合わないユーザーは、AWS を呼ばずに引数の誤りにする。"""
    _ = configured
    assert cli.main(["temt", "proxy", "--svc", "dev-main", "--user", "Admin"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "proxy: the user must start with a lowercase letter" in captured.err
    assert aws_stubs.passed == []


def test_it_waits_for_the_agent_to_come_online(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """起動して間もなければ、ssm の経路ではエージェントが Online になるまで待つ。

    待っていることは、最初に Online でなかったときに 1 回だけ知らせる。
    """
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    agent(aws_stubs, "")
    agent(aws_stubs, "ConnectionLost")
    agent(aws_stubs, "Online")
    sshd_applied(aws_stubs)
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [5, 5]
    assert execs.calls[0][1] == SSM_ARGV
    captured = capsys.readouterr()
    assert captured.out == ""
    assert told(captured.err) == [f"waiting for the SSM agent on {INSTANCE}"]


def test_it_does_not_count_a_ping_from_before_the_launch(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
) -> None:
    """停止してすぐ再開すると、止まる前の Online が残る。起動より後の応答まで待つ。"""
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    agent(aws_stubs, "Online", datetime.now(UTC) - timedelta(minutes=1))
    agent(aws_stubs, "Online")
    sshd_applied(aws_stubs)
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [5]
    assert execs.calls[0][1] == SSM_ARGV


def test_it_waits_for_the_sshd_ports_after_the_agent(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """起動して間もなければ、ssm の経路では関連付けの今の版が実行し終わるまで待つ。

    今の版は 1 度だけ読む。一覧に無い状態と、前の版の状態は、まだ実行されていない
    ものとみなす。
    """
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    agent(aws_stubs, "Online")
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs)
    sshd_ran(aws_stubs, association_ran("Success", OLD_VERSION))
    sshd_ran(aws_stubs, association_ran("Success", OLD_VERSION), association_ran("Queued"))
    sshd_ran(aws_stubs, association_ran("InProgress"))
    sshd_ran(aws_stubs, association_ran("Pending"))
    sshd_ran(aws_stubs, association_ran("Success"))
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [5] * 5
    assert execs.calls[0][1] == SSM_ARGV
    captured = capsys.readouterr()
    assert captured.out == ""
    assert told(captured.err) == [f"waiting for {SSHD} to set the sshd ports on {INSTANCE}"]


def test_a_failed_run_of_the_previous_version_is_not_the_end(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """update-association の直後に残る前の版の Failed では、警告せずに待つ。"""
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    agent(aws_stubs, "Online")
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, association_ran("Failed", OLD_VERSION))
    sshd_ran(aws_stubs, association_ran("Success"))
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [5]
    assert execs.calls[0][1] == SSM_ARGV
    assert "warning" not in capsys.readouterr().err


def test_it_does_not_count_the_runs_of_another_association(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str], clock: Clock
) -> None:
    """名前で見つけた関連付けと ID の違うものの状態は、名前が同じでも数えない。

    見つけた関連付けの今の版が終わるまで待つ。
    """
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    agent(aws_stubs, "Online")
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(
        aws_stubs,
        association_ran("Pending"),
        association_ran("Success", association_id=OTHER_ASSOCIATION),
    )
    sshd_ran(aws_stubs, association_ran("Failed", association_id=OTHER_ASSOCIATION))
    sshd_ran(aws_stubs, association_ran("Success"))
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [5, 5]
    assert execs.calls[0][1] == SSM_ARGV


@pytest.mark.parametrize("state", ["Failed", "TimedOut", "Skipped"])
def test_it_connects_with_a_warning_when_the_sshd_ports_did_not_succeed(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    """実行を待つ状態でも Success でもなければ、終わったものとみなして警告する。

    前のポートのままなので、ssh -p で入って直せるようにそのままつなぐ。
    """
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    agent(aws_stubs, "Online")
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    sshd_ran(aws_stubs, association_ran(state))
    key_sent(aws_stubs)
    proxy.run(configured, 2222)
    aws_stubs.done()
    assert clock.slept == []
    assert execs.calls[0][1][-3] == "portNumber=2222"
    assert capsys.readouterr().err == (
        f"temt: warning: {SSHD} ended with {state} on {INSTANCE}; "
        "sshd stays on its previous ports; see 'temt status --svc dev-main'\n"
    )


def test_it_connects_with_a_warning_when_there_is_no_association(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """関連付けそのものが無ければ、待っても実行されないので、apply を促してつなぐ。

    インスタンスでの状態は読まない。積んでいないので、読めば失敗する。
    """
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    agent(aws_stubs, "Online")
    listed_associations(aws_stubs.ssm)
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == []
    assert execs.calls[0][1] == SSM_ARGV
    assert capsys.readouterr().err == (
        f"temt: warning: no association {SSHD}; sshd listens on the ports in the AMI; "
        "run 'temt apply --svc dev-main'\n"
    )


def test_it_gives_up_on_the_sshd_ports_after_five_minutes(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """締め切りを過ぎたら、status を見るよう添えて終了する。"""
    _ = required
    monkeypatch.setattr(wait, "INTERVAL", 60)
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    agent(aws_stubs, "Online")
    listed_associations(aws_stubs.ssm, ASSOCIATION)
    for _ in range(6):
        sshd_ran(aws_stubs, association_ran("Success", OLD_VERSION), association_ran("Pending"))
    with pytest.raises(TemtError) as caught:
        proxy.run(configured, 22)
    aws_stubs.done()
    assert str(caught.value) == (
        f"timed out after 5 minutes waiting for association {SSHD} to finish on {INSTANCE}; "
        "see 'temt status --svc dev-main'"
    )
    assert clock.slept == [60] * 5
    assert not execs.calls
    assert not configured.known_hosts_dir.exists()


def test_it_does_not_look_at_the_sshd_ports_of_an_old_instance(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    """起動から時間がたっていれば、関連付けは読まない。積んでいないので、読めば失敗する。"""
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), LONG_AGO))
    agent(aws_stubs, "Online")
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert execs.calls[0][1] == SSM_ARGV


def test_it_leaves_the_known_hosts_alone_when_the_agent_stays_offline(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """known_hosts を用意するのは、つなげるようになった後である。締め切りは 5 分。

    締め切りを過ぎたときも、待たずに確かめたときと同じ手がかりを添える。
    """
    _ = required
    monkeypatch.setattr(wait, "INTERVAL", 60)
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    for _ in range(6):
        agent(aws_stubs, "")
    with pytest.raises(
        TemtError,
        match=(
            f"timed out after 5 minutes waiting for the SSM agent on {INSTANCE} to come online; "
            "see 'temt status --svc dev-main' or run 'temt reboot --svc dev-main --async'"
        ),
    ):
        proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [60] * 5
    assert not execs.calls
    assert not configured.known_hosts_dir.exists()


def test_it_waits_for_a_public_address(
    aws_stubs: Stubs,
    configured: Service,
    required: list[str],
    clock: Clock,
    ports: Ports,
    relays: Relay,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """起動して間もなければ、direct の経路でアドレスがまだ無いとき、付くまで待つ。"""
    _ = required
    login(monkeypatch, "direct")
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running"), JUST_NOW))
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running", address=ADDRESS)), BY_ID
    )
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [5]
    # ポートはすぐにつながったので、それを待つ知らせは出ない
    assert ports.calls == [(ADDRESS, 22)]
    assert relays.calls == ports.opened
    captured = capsys.readouterr()
    assert captured.out == ""
    assert told(captured.err) == [f"waiting for a public address on {INSTANCE}"]


def test_it_waits_for_an_address_after_a_pending_instance_is_running(
    aws_stubs: Stubs,
    configured: Service,
    required: list[str],
    clock: Clock,
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """pending から running になっても、アドレスがまだ無ければ続けて待つ。"""
    _ = required
    login(monkeypatch, "direct")
    found(aws_stubs, an_instance(INSTANCE, "pending"))
    # running を待つ
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    # アドレスを待つ
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running", address=ADDRESS)), BY_ID
    )
    key_sent(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == [5]
    assert ports.calls == [(ADDRESS, 22)]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert told(captured.err) == [
        f"{INSTANCE} is pending; waiting for it",
        f"{INSTANCE} is running",
        f"waiting for a public address on {INSTANCE}",
    ]


def test_it_waits_until_the_port_accepts_connections(
    aws_stubs: Stubs,
    configured: Service,
    required: list[str],
    clock: Clock,
    ports: Ports,
    relays: Relay,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """起動して間もなければ、direct の経路では sshd がポートで受け付けるまで待つ。"""
    _ = required
    login(monkeypatch, "direct")
    ports.answers = [REFUSED, REFUSED, REFUSED, None]
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    key_sent(aws_stubs)
    proxy.run(configured, 2222)
    aws_stubs.done()
    assert clock.slept == [5, 5, 5]
    assert ports.calls == [(ADDRESS, 2222)] * 4
    # 鍵は 60 秒しか認められないので、つながってから送る
    assert ports.keys_sent == [0] * 4
    assert relays.keys_sent == [1]
    # つながった接続をそのまま中継する
    assert relays.calls == ports.opened
    captured = capsys.readouterr()
    assert captured.out == ""
    assert told(captured.err) == [f"waiting for {ADDRESS} to accept connections on port 2222"]
    assert configured.known_hosts_current.readlink() == Path(INSTANCE)


@pytest.mark.parametrize(
    ("last", "hint"),
    [
        # 最後に拒まれたなら、パケットは届いていて sshd が受け付けていない
        (REFUSED, "sshd is not accepting"),
        # 最後が時間切れなら、途中で落とされている
        (TimeoutError("timed out"), "check the security group ingress rules in the config file"),
    ],
)
def test_it_leaves_the_known_hosts_alone_when_the_port_stays_closed(
    aws_stubs: Stubs,
    configured: Service,
    relays: Relay,
    required: list[str],
    clock: Clock,
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
    last: OSError,
    hint: str,
) -> None:
    """待っても受け付けなければ、最後につながらなかった理由に合う手がかりを添えて終わる。

    known_hosts を用意するのは、つなげるようになった後である。
    """
    _ = required
    login(monkeypatch, "direct")
    monkeypatch.setattr(wait, "INTERVAL", 60)
    ports.answers = [REFUSED] * 5 + [last]
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), JUST_NOW))
    with pytest.raises(
        TemtError,
        match=(
            f"timed out after 5 minutes waiting for {re.escape(ADDRESS)} to accept connections "
            f"on port 22; {re.escape(hint)}$"
        ),
    ):
        proxy.run(configured, 22)
    assert clock.slept == [60] * 5
    assert not relays.calls
    assert not configured.known_hosts_dir.exists()


def test_it_does_not_wait_for_an_old_instance_whose_agent_is_offline(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """起動から時間がたっていれば待たず、1 回だけ確かめて手がかりを添えて終わる。"""
    _ = required
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), LONG_AGO))
    agent(aws_stubs, "ConnectionLost")
    with pytest.raises(
        TemtError,
        match=(
            f"the SSM agent on {INSTANCE} is not online; "
            "see 'temt status --svc dev-main' or run 'temt reboot --svc dev-main --async'"
        ),
    ):
        proxy.run(configured, 22)
    aws_stubs.done()
    assert clock.slept == []
    assert not execs.calls
    assert not configured.known_hosts_dir.exists()
    assert capsys.readouterr() == ("", "")


def test_it_does_not_wait_for_the_address_of_an_old_instance(
    aws_stubs: Stubs,
    configured: Service,
    relays: Relay,
    required: list[str],
    clock: Clock,
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起動から時間がたってもアドレスが無ければ、待っても付かないので終わる。"""
    _ = required
    login(monkeypatch, "direct")
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running"), LONG_AGO))
    with pytest.raises(TemtError, match=f"{INSTANCE} has no public IPv4 address"):
        proxy.run(configured, 22)
    # describe-instances は探索の 1 回しか積んでいないので、待てば Stubber が失敗する
    aws_stubs.done()
    assert clock.slept == []
    assert ports.calls == []
    assert not relays.calls
    assert not configured.known_hosts_dir.exists()


def test_it_tells_that_sshd_refuses_on_an_old_instance(
    aws_stubs: Stubs,
    configured: Service,
    relays: Relay,
    required: list[str],
    clock: Clock,
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """拒まれたならパケットは届いている。セキュリティグループではなく sshd を疑わせる。"""
    _ = required
    login(monkeypatch, "direct")
    ports.answers = [ConnectionRefusedError(111, "Connection refused")]
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), LONG_AGO))
    with pytest.raises(TemtError) as caught:
        proxy.run(configured, 22)
    assert str(caught.value) == (f"{ADDRESS} refused connections on port 22; sshd is not accepting")
    aws_stubs.done()
    assert clock.slept == []
    assert ports.calls == [(ADDRESS, 22)]
    assert not relays.calls
    assert not configured.known_hosts_dir.exists()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("reason", [TimeoutError("timed out"), OSError(113, "No route to host")])
def test_it_suspects_the_security_group_on_an_old_instance(
    aws_stubs: Stubs,
    configured: Service,
    relays: Relay,
    required: list[str],
    clock: Clock,
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reason: OSError,
) -> None:
    """拒まれずにつながらなければ、理由を添え、セキュリティグループを疑わせて終わる。"""
    _ = required
    login(monkeypatch, "direct")
    ports.answers = [reason]
    found(aws_stubs, launched_at(an_instance(INSTANCE, "running", address=ADDRESS), LONG_AGO))
    with pytest.raises(TemtError) as caught:
        proxy.run(configured, 22)
    assert str(caught.value) == (
        f"cannot connect to {ADDRESS} port 22: {reason}; "
        "check the security group ingress rules in the config file"
    )
    aws_stubs.done()
    assert clock.slept == []
    assert ports.calls == [(ADDRESS, 22)]
    assert not relays.calls
    assert not configured.known_hosts_dir.exists()
    assert capsys.readouterr() == ("", "")


def test_it_asks_for_apply_when_there_is_no_config_file(
    aws_stubs: Stubs, service: Service, execs: Exec, required: list[str], ssh_config: SshConfig
) -> None:
    with pytest.raises(TemtError, match="run 'temt apply --svc dev-main' first"):
        proxy.run(service, 22)
    # 設定ファイルが無ければ、経路も決まらず、ssh にも AWS にも触れない
    assert required == []
    assert ssh_config.asked == []
    assert aws_stubs.passed == []
    assert not execs.calls


@pytest.mark.parametrize(
    ("route", "missing"),
    [
        ("ssm", "ssh"),
        ("ssm", "aws"),
        ("ssm", "session-manager-plugin"),
        ("direct", "ssh"),
    ],
)
def test_it_checks_the_commands_before_touching_aws(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    relays: Relay,
    ssh_config: SshConfig,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    missing: str,
) -> None:
    """使う外部コマンドの有無は、ssh -G に尋ねる前に、経路ごとに確かめる。"""

    def which(command: str) -> str | None:
        return None if command == missing else f"/usr/bin/{command}"

    login(monkeypatch, route)
    monkeypatch.setattr(proc.shutil, "which", which)
    with pytest.raises(TemtError, match=f"command not found: {missing}"):
        proxy.run(configured, 22)
    assert ssh_config.asked == []
    assert aws_stubs.passed == []
    assert not execs.calls
    assert not relays.calls


@pytest.mark.parametrize("missing", ["aws", "session-manager-plugin"])
def test_the_direct_route_needs_only_ssh(
    aws_stubs: Stubs,
    configured: Service,
    relays: Relay,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """direct は aws-cli も session-manager-plugin も使わないので、無くてもつなぐ。"""

    def which(command: str) -> str | None:
        return None if command == missing else f"/usr/bin/{command}"

    login(monkeypatch, "direct")
    monkeypatch.setattr(proc.shutil, "which", which)
    running(aws_stubs)
    proxy.run(configured, 22)
    aws_stubs.done()
    assert len(relays.calls) == 1


def test_it_connects_with_stderr_closed(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """標準エラー出力が閉じていても（2>&-）、落ちずに aws-cli に引き継ぐ。"""
    _ = required
    monkeypatch.setattr(sys, "stderr", None)
    ready(aws_stubs)
    proxy.run(configured, 22)
    monkeypatch.undo()
    aws_stubs.done()
    [(file, argv, _)] = execs.calls
    assert (file, argv) == ("aws", SSM_ARGV)


def test_main_reports_a_failed_exec(
    aws_stubs: Stubs,
    configured: Service,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """exec に失敗しても、標準出力には何も書かず、プログラム名を付けて知らせる。"""
    _ = required

    def broken(file: str, argv: list[str], env: dict[str, str]) -> None:
        _ = (argv, env)
        raise FileNotFoundError(2, "No such file or directory", file)

    monkeypatch.setattr(proxy, "execvpe", broken)
    ready(aws_stubs)
    assert cli.main(["temt", "proxy", "--svc", "dev-main"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("temt: ")
    assert "No such file or directory" in err


# --- EC2 Instance Connect で送る鍵 ---


def connected(stubs: Stubs, route: str) -> None:
    """鍵を送る応答のほかに、その経路で待たずにつなげる応答を積む。"""
    found(stubs, an_instance(INSTANCE, "running", address=ADDRESS))
    if route == "ssm":
        agent(stubs, "Online")


@pytest.mark.parametrize("route", ["ssm", "direct"])
def test_it_sends_the_key_that_ssh_offers(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    relays: Relay,
    required: list[str],
    ssh_config: SshConfig,
    home_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """ssh -G が出すユーザーで、.pub のある最初の鍵を、コメントを除いて送る。

    既定の鍵でなくても、利用者が Host ブロックに書いた鍵を送る。
    """
    _ = required
    login(monkeypatch, route)
    public = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7" + "x" * 80
    (home_dir / "mine.pub").write_text(f"{public} me@laptop\n", encoding="utf-8")
    ssh_config.lines = ["user admin", "identityfile ~/missing", "identityfile ~/mine"]
    connected(aws_stubs, route)
    key_sent(aws_stubs, {"InstanceId": INSTANCE, "InstanceOSUser": "admin", "SSHPublicKey": public})
    proxy.run(configured, 22)
    aws_stubs.done()
    assert (execs.keys_sent, relays.keys_sent) == (([1], []) if route == "ssm" else ([], [1]))


def test_it_sends_the_key_for_the_user_ssh_logs_in_as(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    required: list[str],
    ssh_config: SshConfig,
) -> None:
    """ssh other@<名前> のユーザーは ProxyCommand の %r で届き、ssh -G のものより優先する。"""
    _ = (required, ssh_config)
    found(aws_stubs, an_instance(INSTANCE, "running", address=ADDRESS))
    agent(aws_stubs, "Online")
    key_sent(aws_stubs, {**SEND, "InstanceOSUser": "admin"})
    proxy.run(configured, 22, "admin")
    aws_stubs.done()
    assert execs.keys_sent == [1]


@pytest.mark.parametrize("route", ["ssm", "direct"])
def test_it_stops_before_aws_without_a_public_key(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    relays: Relay,
    ports: Ports,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """送る鍵が無ければ、AWS を呼ぶ前に、既定の鍵を作る apply を促して止まる。"""
    _ = required
    login(monkeypatch, route)
    keys.public_path(keys.default_path()).unlink()
    with pytest.raises(TemtError) as caught:
        proxy.run(configured, 22)
    assert str(caught.value) == (
        f"no public key for 'ssh {NAME}': no .pub next to its IdentityFile "
        "(~/.ssh/temt/keys/temt); "
        f"put the .pub next to the key, or delete IdentityFile from the Host {NAME} block "
        "and run 'temt apply' to use the default key"
    )
    assert aws_stubs.passed == []
    assert (execs.calls, relays.calls, ports.calls) == ([], [], [])
    assert not configured.known_hosts_dir.exists()


def test_it_stops_before_aws_when_the_public_key_is_broken(
    aws_stubs: Stubs, configured: Service, execs: Exec, required: list[str]
) -> None:
    _ = required
    keys.public_path(keys.default_path()).write_text("not-a-key\n", encoding="utf-8")
    with pytest.raises(TemtError) as caught:
        proxy.run(configured, 22)
    assert str(caught.value) == "not a public key: ~/.ssh/temt/keys/temt.pub"
    assert aws_stubs.passed == []
    assert not execs.calls


@pytest.mark.parametrize("route", ["ssm", "direct"])
def test_main_reports_an_error_from_sending_the_key(
    aws_stubs: Stubs,
    configured: Service,
    execs: Exec,
    relays: Relay,
    ports: Ports,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: str,
) -> None:
    """鍵を送れなければ、AWS の誤りをほかの誤りと同じく知らせ、つながずに終わる。

    known_hosts は送った後に用意するので、触れない。direct なら、つないだ接続を閉じる。
    """
    _ = required
    login(monkeypatch, route)
    connected(aws_stubs, route)
    aws_stubs.connect.add_client_error(
        "send_ssh_public_key",
        service_error_code="AuthException",
        service_message="not authorized to perform ec2-instance-connect:SendSSHPublicKey",
        http_status_code=403,
        expected_params=SEND,
    )
    assert cli.main(["temt", "proxy", "--svc", "dev-main"]) == 1
    aws_stubs.done()
    out, err = capsys.readouterr()
    assert out == ""
    assert err == (
        "temt: An error occurred (AuthException) when calling the SendSSHPublicKey operation: "
        "not authorized to perform ec2-instance-connect:SendSSHPublicKey\n"
    )
    assert (execs.calls, relays.calls) == ([], [])
    assert all(connection.closed for connection in ports.opened)
    assert len(ports.opened) == (1 if route == "direct" else 0)
    assert not configured.known_hosts_dir.exists()
