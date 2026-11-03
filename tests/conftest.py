"""テストで共通に使う下ごしらえ。"""

import base64
import fcntl
import io
import json
import signal
import tarfile
import time
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ParamValidationError, StubResponseError
from botocore.stub import Stubber
from types_boto3_accessanalyzer.client import AccessAnalyzerClient
from types_boto3_ec2.client import EC2Client
from types_boto3_ec2_instance_connect.client import EC2InstanceConnectClient
from types_boto3_iam.client import IAMClient
from types_boto3_ssm.client import SSMClient
from types_boto3_sts.client import STSClient

from temt import aws, conf, ingress, lock, msg, names, proc, wait
from temt.conf import Conf
from temt.names import Service

# AWS につながないクライアントに渡すリージョン
REGION = "ap-northeast-1"
# 確認に出す、資格情報のアカウント
ACCOUNT = "123456789012"
# テストの手元のタイムゾーン。tzdata に頼らない POSIX の形で書く
LOCAL_TZ = "JST-9"


@pytest.fixture(autouse=True)
def process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """main が書き換えるモジュール変数と環境変数を、テストごとに初期値へ戻す。

    main はリージョンの環境変数を os.environ から直に取り除くので、先に monkeypatch で
    取り除いておき、テストの後に手元の値へ戻させる。
    """
    monkeypatch.setattr(conf, "OPTION_PROFILE", None)
    monkeypatch.setattr(conf, "OPTION_REGION", None)
    monkeypatch.setattr(conf, "ENV_REGION", None)
    monkeypatch.setattr(msg, "_only_stderr", False)
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


@cache
def offline() -> boto3.Session:
    """資格情報とリージョンを明示した、何も探しに行かない Session。

    テストのあいだ 1 つを使い回す。Session を作るたびに botocore が API の定義を読み
    直し、テストが遅くなるからである。クライアントはテストごとに作るので、Stubber は
    テストをまたがない。
    """
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


@pytest.fixture
def connect(stub_failures: list[BotoCoreError]) -> EC2InstanceConnectClient:
    """AWS につながない EC2 Instance Connect のクライアント。"""
    return watched(offline().client("ec2-instance-connect"), stub_failures)


@pytest.fixture
def sts(stub_failures: list[BotoCoreError]) -> STSClient:
    """AWS につながない STS のクライアント。"""
    return watched(offline().client("sts"), stub_failures)


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
    """aws.session の代わり。用意したクライアントを返す。region_name は、決まったリージョン。"""

    def __init__(
        self,
        ec2: EC2Client,
        ssm: SSMClient,
        iam: IAMClient,
        analyzer: AccessAnalyzerClient,
        connect: EC2InstanceConnectClient,
        sts: STSClient,
        region_name: str = REGION,
    ) -> None:
        self.region_name = region_name
        self.clients: dict[str, Any] = {
            "ec2": ec2,
            "ssm": ssm,
            "iam": iam,
            "accessanalyzer": analyzer,
            "ec2-instance-connect": connect,
            "sts": sts,
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
    connect: Stubber
    sts: Stubber
    # aws.session に渡された設定。設定ファイルから決まる
    passed: list[Conf]

    def done(self) -> None:
        """積んだ応答がすべて使われたことを確かめる。"""
        for stub in (self.ec2, self.ssm, self.iam, self.analyzer, self.connect, self.sts):
            stub.assert_no_pending_responses()


@pytest.fixture
def aws_stubs(
    monkeypatch: pytest.MonkeyPatch,
    ec2: EC2Client,
    ssm: SSMClient,
    iam: IAMClient,
    analyzer: AccessAnalyzerClient,
    connect: EC2InstanceConnectClient,
    sts: STSClient,
) -> Iterator[Stubs]:
    """コマンドが Session から作るクライアントを、Stubber で包んだものに差し替える。

    teardown で、積んだ応答がすべて使われたことを確かめる。
    """
    passed: list[Conf] = []
    resolve_region = aws.resolve_region

    def session(values: Conf) -> Session:
        # aws.session と同じく、設定ファイルで決まらなかったリージョンを決める
        resolve_region(values)
        # Conf は後の手順が書き換えるので、渡された時点の姿を控える。テーブルも
        # 書き換わるので、深い複製にする
        passed.append(deepcopy(values))
        return Session(ec2, ssm, iam, analyzer, connect, sts, values.AwsRegion)

    monkeypatch.setattr(aws, "session", session)
    with (
        Stubber(ec2) as ec2_stub,
        Stubber(ssm) as ssm_stub,
        Stubber(iam) as iam_stub,
        Stubber(analyzer) as analyzer_stub,
        Stubber(connect) as connect_stub,
        Stubber(sts) as sts_stub,
    ):
        stubs = Stubs(ec2_stub, ssm_stub, iam_stub, analyzer_stub, connect_stub, sts_stub, passed)
        yield stubs
        stubs.done()


def identified(stubs: Stubs) -> None:
    """確認に添えるアカウントを、STS から引く。"""
    stubs.sts.add_response("get_caller_identity", {"Account": ACCOUNT}, {})


# 確認の問いに添える、リージョンとアカウント
WHERE = f"in {REGION} (account {ACCOUNT})"

# 書かなければ apply が埋める値も、すべて書いた設定ファイル。[role] は無い。AwsRegion と
# ImageId はふつうどおり書かない。リージョンは環境から決まる既定の REGION になり、AMI は
# 起動テンプレートが持つ
CONFIGURED = """\
[launch-template]
InstanceType = "t3.micro"
InstanceInitiatedShutdownBehavior = "terminate"
IamInstanceProfile = { Name = "${temt:name}" }

[[launch-template.NetworkInterfaces]]
DeviceIndex = 0
SubnetId = "subnet-0123456789abcdef0"
AssociatePublicIpAddress = true

[launch-template.MetadataOptions]
HttpTokens = "required"
InstanceMetadataTags = "enabled"

[launch-template.CreditSpecification]
CpuCredits = "unlimited"
"""


@pytest.fixture
def configured(service: Service) -> Service:
    """設定ファイルのあるサービス。中身は CONFIGURED。"""
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(CONFIGURED, encoding="utf-8")
    return service


def an_instance(
    instance_id: str, state: str, *, address: str = "", kept: tuple[str, ...] = ()
) -> dict[str, Any]:
    """describe-instances の応答の 1 台分。kept は終了しても消えないボリュームのデバイス名で、
    最初のものをルートボリュームとする。
    """
    item: dict[str, Any] = {"InstanceId": instance_id, "State": {"Name": state}}
    if address:
        item["PublicIpAddress"] = address
    if kept:
        item["RootDeviceName"] = kept[0]
        item["BlockDeviceMappings"] = [
            {"DeviceName": device, "Ebs": {"DeleteOnTermination": False}} for device in kept
        ]
    return item


def reservations(*items: dict[str, Any]) -> dict[str, Any]:
    """describe-instances の応答。1 台ずつ別の予約に入れる。"""
    return {"Reservations": [{"Instances": [item]} for item in items]}


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


# インスタンスの上のアプリを読むテストで、ssh がつながる先
REACHED = "i-0123456789abcdef0"


def apps_archive(apps: Mapping[str, Mapping[str, str | bytes]]) -> bytes:
    """インスタンスの上のアプリを読む remote.READ_SCRIPT の出力の代わり。

    apps はアプリ ID から、そのディレクトリのファイル名と中身への対応。ディレクトリも
    項目として入れる。空なら、アプリが 1 つも無いときの空の出力とする。
    """
    if not apps:
        return b""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for app_id, files in apps.items():
            folder = tarfile.TarInfo(f"./{app_id}")
            folder.type = tarfile.DIRTYPE
            tar.addfile(folder)
            for name, body in files.items():
                data = body.encode() if isinstance(body, str) else body
                info = tarfile.TarInfo(f"./{app_id}/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def a_manifest(version: str, *, user: str | None = None) -> str:
    """インスタンスの上の manifest.toml。"""
    text = f'name = "Jupyter Lab"\nversion = "{version}"\n'
    return text + (f'user = "{user}"\n' if user else "")


def a_record(instance: str, **more: Any) -> str:
    """インスタンスの上の deploy の記録。more は足すか置き換える項目。"""
    found = {"user": "root", "deployed": "2026-09-27T03:04:05Z", "instance": instance, **more}
    return json.dumps(found, ensure_ascii=False)


@dataclass
class Remote:
    """インスタンスの上のアプリを読む ssh の代わり。

    reached はつながる先のインスタンス ID で、None ならつながらない。apps はインスタンスの
    上のアプリ（apps_archive）で、code が 0 でなければ、読み取りがその終了コードで失敗する。
    """

    apps: dict[str, dict[str, str | bytes]] = field(default_factory=dict)
    reached: str | None = REACHED
    code: int = 0
    # 読み取りで渡されたコマンドライン
    reads: list[list[str]] = field(default_factory=list)

    def read(self, argv: list[str], *, env: Mapping[str, str] | None = None) -> tuple[int, str]:
        _ = env
        if self.reached is None:
            return 255, ""
        return 0, f"{self.reached}\n"

    def capture(
        self, argv: list[str], *, env: Mapping[str, str] | None = None
    ) -> tuple[int, bytes]:
        _ = env
        self.reads.append(argv)
        if self.code:
            return self.code, b""
        return 0, apps_archive(self.apps)


@pytest.fixture
def remote_apps(monkeypatch: pytest.MonkeyPatch) -> Remote:
    """ssh を Remote に差し替える。"""
    fake = Remote()
    monkeypatch.setattr(proc, "read", fake.read)
    monkeypatch.setattr(proc, "capture", fake.capture)
    monkeypatch.setattr(proc, "require", lambda command: command)
    return fake


# Run Command で読むときの、送ったコマンドの ID
COMMAND_ID = "11111111-2222-3333-4444-555555555555"


def online(stubs: Stubs, instance: str, ping: str = "Online") -> None:
    """Run Command で読む前に確かめる、SSM エージェントの状態。"""
    stubs.ssm.add_response(
        "describe_instance_information",
        {"InstanceInformationList": [{"InstanceId": instance, "PingStatus": ping}]},
        {"Filters": [{"Key": "InstanceIds", "Values": [instance]}]},
    )


def commanded(
    stubs: Stubs,
    instance: str,
    apps: Mapping[str, Mapping[str, str | bytes]],
    *,
    status: str = "Success",
    error: str = "",
    output: str | None = None,
) -> None:
    """Run Command でインスタンスの上のアプリ（apps_archive）を読む応答を積む。

    SSM エージェントが Online で、送ったコマンドが status で終わる。output を与えれば、
    標準出力をそれにする。
    """
    online(stubs, instance)
    stubs.ssm.add_response("send_command", {"Command": {"CommandId": COMMAND_ID}})
    if output is None:
        output = base64.b64encode(apps_archive(apps)).decode()
    stubs.ssm.add_response(
        "get_command_invocation",
        {"Status": status, "StandardOutputContent": output, "StandardErrorContent": error},
        {"CommandId": COMMAND_ID, "InstanceId": instance},
    )
