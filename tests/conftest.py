"""テストで共通に使う下ごしらえ。"""

import fcntl
import signal
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ParamValidationError, StubResponseError
from botocore.stub import Stubber
from types_boto3_accessanalyzer.client import AccessAnalyzerClient
from types_boto3_ec2.client import EC2Client
from types_boto3_iam.client import IAMClient
from types_boto3_ssm.client import SSMClient

from temt import aws, conf, ingress, lock, names, sshd, wait
from temt.conf import Conf
from temt.names import Service

# AWS につながないクライアントに渡すリージョン
REGION = "ap-northeast-1"
# テストの手元のタイムゾーン。tzdata に頼らない POSIX の形で書く
LOCAL_TZ = "JST-9"


@pytest.fixture(autouse=True)
def process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """main が書き換えるモジュール変数と環境変数を、テストごとに初期値へ戻す。

    main はリージョンの環境変数を os.environ から直に取り除くので、先に monkeypatch で
    取り除いておき、テストの後に手元の値へ戻させる。
    """
    monkeypatch.setattr(conf, "OPTION_PROFILE", None)
    monkeypatch.setattr(conf, "ENV_REGION", None)
    for name in conf.REGION_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def local_time_zone() -> Iterator[None]:
    """手元のタイムゾーンを日本時間（UTC+9）に固定する。表示する時刻はこれで決まる。"""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TZ", LOCAL_TZ)
        time.tzset()
        yield
    time.tzset()


@pytest.fixture(autouse=True)
def temporary_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """ホームディレクトリを一時ディレクトリに向け、場所を変える環境変数を断つ。

    autouse なので、どのテストも利用者のホームを読み書きしない。手元で
    TEMT_CONF_DIR や TEMT_SSH_DIR を設定していても、テストには持ち込まない。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(names.CONF_DIR_ENV, raising=False)
    monkeypatch.delenv(names.SSH_DIR_ENV, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def no_bundled_apps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """temt と一緒に配るアプリの場所を、空のディレクトリに向ける。

    autouse なので、app list の表や deploy の探索が、パッケージに入っているアプリの
    増減で変わらない。配るアプリを使うテストは、そのアプリを置くか、場所を戻す。
    """
    place = tmp_path / "bundled"
    monkeypatch.setattr(names, "BUNDLED_APPS", place)
    return place


@pytest.fixture
def home_dir(temporary_home: Path) -> Path:
    """一時的なホームディレクトリ。"""
    return temporary_home


@pytest.fixture
def service(home_dir: Path) -> Service:
    """一時的なホームの下で使うサービス。"""
    _ = home_dir
    return Service("dev-main")


@pytest.fixture(autouse=True)
def no_aws_env(temporary_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS の設定を環境から断つ。開発環境の ~/.aws や AWS_* に左右されないようにする。

    autouse なので、Stubber を使わずに main やコマンドの本体を呼ぶテストも、手元の
    資格情報で AWS につながらない。~/.aws はホームとともに一時ディレクトリに向く。
    EC2 やコンテナの上で走らせても、そこのロールの資格情報を取りに行かない。
    リージョンの環境変数は process_state が断つ。
    """
    _ = temporary_home
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture(autouse=True)
def no_egress_lookup(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """グローバル IP アドレスの問い合わせを塞ぐ。差し替え忘れたテストは teardown で落ちる。

    ingress は問い合わせの失敗を知らせて先へ進むので、呼ばれたことを控えて確かめる。
    """
    asked: list[str] = []

    def fetch(url: str) -> str:
        asked.append(url)
        raise OSError("the egress address lookup is not replaced in this test")

    monkeypatch.setattr(ingress, "fetch", fetch)
    yield
    assert not asked, f"the egress address lookup was not replaced: {asked}"


@pytest.fixture
def stub_failures() -> Iterator[list[BotoCoreError]]:
    """Stubber とクライアントが起こした失敗の控え。teardown で空であることを確かめる。

    積んでいない呼び出しや、引数の違う呼び出しで Stubber が起こす失敗
    （StubResponseError）と、型の誤った引数でクライアントが起こす失敗
    （ParamValidationError）は BotoCoreError の仲間なので、製品のコードが AWS の失敗
    として受け、警告して先へ進むか、エラーにして終了コード 1 で終わることがある。
    expected_params を省いたか ANY で積んだ呼び出しでは、引数の型の誤りは後者になる。
    そうして見逃さないよう、クライアントの fixture がここに控える。
    """
    failures: list[BotoCoreError] = []
    yield failures
    assert not failures, f"the stubber failed: {failures}"


# botocore のクライアントが API を呼ぶメソッドの名前。型の宣言には無い
API_CALL = "_make_api_call"


def offline() -> boto3.Session:
    """資格情報とリージョンを明示した、何も探しに行かない Session。"""
    return boto3.Session(region_name=REGION, aws_access_key_id="x", aws_secret_access_key="y")


def watched[T](client: T, failures: list[BotoCoreError]) -> T:
    """Stubber とクライアントが起こした失敗を failures に控えるよう、client を包んで返す。

    botocore のクライアントの API 呼び出しは、ページ送りと waiter のものも含めて、
    すべて API_CALL を通る。
    """
    call = getattr(client, API_CALL)

    def make_api_call(operation_name: str, api_params: dict[str, Any]) -> Any:
        try:
            return call(operation_name, api_params)
        except (StubResponseError, ParamValidationError) as e:
            failures.append(e)
            raise

    setattr(client, API_CALL, make_api_call)
    return client


@pytest.fixture
def ec2(stub_failures: list[BotoCoreError]) -> EC2Client:
    """AWS につながない EC2 のクライアント。"""
    return watched(offline().client("ec2"), stub_failures)


@pytest.fixture
def ssm(stub_failures: list[BotoCoreError]) -> SSMClient:
    """AWS につながない SSM のクライアント。"""
    return watched(offline().client("ssm"), stub_failures)


@pytest.fixture
def iam(stub_failures: list[BotoCoreError]) -> IAMClient:
    """AWS につながない IAM のクライアント。"""
    return watched(offline().client("iam"), stub_failures)


@pytest.fixture
def analyzer(stub_failures: list[BotoCoreError]) -> AccessAnalyzerClient:
    """AWS につながない IAM Access Analyzer のクライアント。"""
    return watched(offline().client("accessanalyzer"), stub_failures)


class Clock:
    """待ち合わせの時計と休みの代わり。休んだ分だけ進む。"""

    def __init__(self) -> None:
        self.now: float = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """待ち合わせの時計と休みを、実際には待たないものに差し替える。

    autouse なので、取り忘れたテストも実際には休まない。
    """
    fake = Clock()
    monkeypatch.setattr(wait, "clock", fake.time)
    monkeypatch.setattr(wait, "sleep", fake.sleep)
    return fake


class Session:
    """aws.session の代わり。用意したクライアントを返す。"""

    def __init__(
        self, ec2: EC2Client, ssm: SSMClient, iam: IAMClient, analyzer: AccessAnalyzerClient
    ) -> None:
        self.clients: dict[str, Any] = {
            "ec2": ec2,
            "ssm": ssm,
            "iam": iam,
            "accessanalyzer": analyzer,
        }

    def client(self, name: str) -> Any:
        return self.clients[name]


@dataclass(frozen=True)
class Stubs:
    """コマンドが使うクライアントの Stubber。"""

    ec2: Stubber
    ssm: Stubber
    iam: Stubber
    analyzer: Stubber
    # aws.session に渡された設定。設定ファイルから決まる
    passed: list[Conf]

    def done(self) -> None:
        """積んだ応答がすべて使われたことを確かめる。"""
        for stub in (self.ec2, self.ssm, self.iam, self.analyzer):
            stub.assert_no_pending_responses()


@pytest.fixture
def aws_stubs(
    monkeypatch: pytest.MonkeyPatch,
    ec2: EC2Client,
    ssm: SSMClient,
    iam: IAMClient,
    analyzer: AccessAnalyzerClient,
) -> Iterator[Stubs]:
    """コマンドが Session から作るクライアントを、Stubber で包んだものに差し替える。

    teardown で、積んだ応答がすべて使われたことを確かめる。
    """
    passed: list[Conf] = []

    def session(values: Conf) -> Session:
        # Conf は後の手順が書き換えるので、渡された時点の姿を控える。テーブルも
        # 書き換わるので、深い複製にする
        passed.append(deepcopy(values))
        return Session(ec2, ssm, iam, analyzer)

    monkeypatch.setattr(aws, "session", session)
    with (
        Stubber(ec2) as ec2_stub,
        Stubber(ssm) as ssm_stub,
        Stubber(iam) as iam_stub,
        Stubber(analyzer) as analyzer_stub,
    ):
        stubs = Stubs(ec2_stub, ssm_stub, iam_stub, analyzer_stub, passed)
        yield stubs
        stubs.done()


@pytest.fixture
def configured(service: Service) -> Service:
    """設定ファイルのあるサービス。apply が書いたものに当たる。"""
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(
        f'AwsRegion = "{REGION}"\n'
        'KeyName = "temt"\n'
        "\n"
        "[launch-template]\n"
        'ImageId = "ami-0123456789abcdef0"\n'
        'InstanceType = "t3.micro"\n'
        'SubnetId = "subnet-0123456789abcdef0"\n',
        encoding="utf-8",
    )
    return service


def an_instance(
    instance_id: str, state: str, *, address: str = "", kept: tuple[str, ...] = ()
) -> dict[str, Any]:
    """describe-instances の応答の 1 台分。kept は終了しても消えないボリューム。"""
    item: dict[str, Any] = {"InstanceId": instance_id, "State": {"Name": state}}
    if address:
        item["PublicIpAddress"] = address
    if kept:
        item["BlockDeviceMappings"] = [
            {"DeviceName": device, "Ebs": {"DeleteOnTermination": False}} for device in kept
        ]
    return item


def reservations(*items: dict[str, Any]) -> dict[str, Any]:
    """describe-instances の応答。1 台ずつ別の予約に入れる。"""
    return {"Reservations": [{"Instances": [item]} for item in items]}


# サービス dev-main の関連付け <名前>-sshd と、その ID
SSHD = "temt-dev-main-sshd"
ASSOCIATION = "11111111-2222-3333-4444-555555555555"
# 関連付けの今の版と、その前の版
VERSION = "2"
OLD_VERSION = "1"
# 関連付けに付ける temt のタグ
ASSOCIATION_MARK = [{"Key": aws.TAG_SERVICE, "Value": "dev-main"}]


def listed_associations(stub: Stubber, *ids: str, version: str = VERSION) -> None:
    """list-associations の応答を積む。

    名前が <名前>-sshd の関連付けが ids だけあり、どれも今の版は version である。
    """
    stub.add_response(
        "list_associations",
        {
            "Associations": [
                {"AssociationId": one, "AssociationName": SSHD, "AssociationVersion": version}
                for one in ids
            ]
        },
        {"AssociationFilterList": [{"key": "AssociationName", "value": SSHD}]},
    )


def association_ran(
    status: str, version: str = VERSION, association_id: str = ASSOCIATION
) -> dict[str, Any]:
    """describe-instance-associations-status が返す、<名前>-sshd の 1 つの版の状態。"""
    return {
        "AssociationId": association_id,
        "AssociationName": SSHD,
        "AssociationVersion": version,
        "Status": status,
    }


def association_content(ports: list[int]) -> dict[str, Any]:
    """関連付けの中身。ports は SshdPorts。"""
    return {
        "Name": sshd.DOCUMENT,
        "Targets": [{"Key": "tag:Name", "Values": ["temt-dev-main"]}],
        "Parameters": {sshd.COMMANDS: sshd.commands(ports, "dev-main")},
    }


def described_association(stub: Stubber, ports: list[int] | None = None) -> None:
    """名前で見つけた関連付け（ASSOCIATION）の中身を読む describe-association の応答。

    中身は ports（既定は [22]）に合っている。
    """
    stub.add_response(
        "describe_association",
        {
            "AssociationDescription": {
                "AssociationId": ASSOCIATION,
                "AssociationName": SSHD,
                **association_content(ports or [22]),
            }
        },
        {"AssociationId": ASSOCIATION},
    )


def association_in_place(stub: Stubber, ports: list[int] | None = None) -> None:
    """apply が揃える関連付けが、既にあって ports（既定は [22]）に合っているときの応答。

    中身もタグも揃っていれば、apply は読むだけで、実行も求めない。
    """
    listed_associations(stub, ASSOCIATION)
    described_association(stub, ports)
    stub.add_response(
        "list_tags_for_resource",
        {"TagList": ASSOCIATION_MARK},
        {"ResourceType": "Association", "ResourceId": ASSOCIATION},
    )


def association_started(stub: Stubber) -> None:
    """start-associations-once の応答を積む。"""
    stub.add_response("start_associations_once", {}, {"AssociationIds": [ASSOCIATION]})


def taken(service: Service) -> bool:
    """そのサービスのロックを別に取れるかどうか。他が持っていれば False。

    flock は開いたファイルごとに効くので、同じプロセスからでも競合する。
    """
    with (service.known_hosts_dir / lock.LOCK_NAME).open("a", encoding="utf-8") as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        return True


class TooSlow(Exception):
    """打ち切りの合図。

    OSError の仲間にしないのは、ロックの except OSError に捕まって、ロックが待ちに
    入ったことが成功に見えてしまうからである。
    """


@contextmanager
def deadline(seconds: float) -> Generator[None]:
    """テストが固まらないよう打ち切りを置く。ロックが待ちに入れば、ここで落ちる。"""

    def ring(number: int, frame: FrameType | None) -> NoReturn:
        _ = (number, frame)
        raise TooSlow("the test did not finish in time")

    previous = signal.signal(signal.SIGALRM, ring)
    _ = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        _ = signal.setitimer(signal.ITIMER_REAL, 0)
        _ = signal.signal(signal.SIGALRM, previous)
