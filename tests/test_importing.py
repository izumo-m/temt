"""service import の本体のテスト。AWS は Stubber で、標準入力と時計と時刻は偽物に差し替える。

端から端まで（importing.run か cli.main）を通し、手順の順序、コピーする設定の直し方、書いた
設定ファイルの中身とそれが読めること、止まったときに何も書かないことを確かめる。
Stubber は積んだ順にしか応答しないので、応答を積む順が手順の順序を表す。
"""

import base64
import copy
import fcntl
import re
import sys
import time
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import WHERE, Clock, Stubs, identified
from types_boto3_ec2.client import EC2Client

from temt import bake, cli, conf, home, importing, lock, wait
from temt.msg import TemtError, UsageError
from temt.names import Service, defaults_path

NAME = "temt-dev-main"
# 取り込む元のインスタンスと、その ENI
SOURCE = "i-0123456789abcdef0"
ENI = "eni-0123456789abcdef0"
OTHER_ENI = "eni-0fedcba9876543210"
SUBNET = "subnet-0123456789abcdef0"
GROUP = "sg-0aaaaaaaaaaaaaaaa"
ROOT_VOLUME = "vol-0aaaaaaaaaaaaaaaa"
DATA_VOLUME = "vol-0bbbbbbbbbbbbbbbb"
LOG_VOLUME = "vol-0cccccccccccccccc"
SOURCE_PROFILE = "arn:aws:iam::123456789012:instance-profile/web-profile"
# 作る AMI
NEW = "ami-0fedcba9876543210"
# AMI の名前に入る時刻。2026-09-23 12:34（ローカル時刻）
AT = time.struct_time((2026, 9, 23, 12, 34, 0, 2, 266, -1))
IMAGE_NAME = "temt-dev-main-2609231234"
USER_DATA = base64.b64encode(b"#cloud-config\nruncmd:\n  - echo hello\n").decode()

BY_ID = {"InstanceIds": [SOURCE]}
THE_INSTANCE = {"InstanceId": SOURCE}
REGISTERED = {"Filters": [{"Key": "InstanceIds", "Values": [SOURCE]}]}
TAGS = [{"Key": "Name", "Value": IMAGE_NAME}, {"Key": "temt:service", "Value": "dev-main"}]
# ルートボリューム以外の EBS は NoDevice にし、ルートボリュームは終了とともに消す
CREATE = {
    "InstanceId": SOURCE,
    "Name": IMAGE_NAME,
    "BlockDeviceMappings": [
        {"DeviceName": "/dev/sdf", "NoDevice": ""},
        {"DeviceName": "/dev/sda1", "Ebs": {"DeleteOnTermination": True}},
    ],
    "TagSpecifications": [
        {"ResourceType": "image", "Tags": TAGS},
        {"ResourceType": "snapshot", "Tags": TAGS},
    ],
}
THE_IMAGE = {"ImageIds": [NEW]}
PROMPT = f"import {SOURCE} as {NAME} {WHERE}? [y/N] "
NOT_IN_AMI = (
    "not put in the AMI; to use one, detach it from the source instance and attach it from "
    "inside the instance"
)

# describe-instances が返す、止まっている元のインスタンス。データ用の EBS を 1 つ持つ
INSTANCE: dict[str, Any] = {
    "InstanceId": SOURCE,
    "State": {"Name": "stopped"},
    "Tags": [{"Key": "Name", "Value": "web-server"}],
    "RootDeviceName": "/dev/sda1",
    "BlockDeviceMappings": [
        {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": ROOT_VOLUME, "DeleteOnTermination": False}},
        {"DeviceName": "/dev/sdf", "Ebs": {"VolumeId": DATA_VOLUME, "DeleteOnTermination": False}},
    ],
    "NetworkInterfaces": [
        {
            "NetworkInterfaceId": ENI,
            "Attachment": {"DeviceIndex": 0},
            "PrivateIpAddresses": [
                {"PrivateIpAddress": "10.0.0.10", "Primary": True},
                {"PrivateIpAddress": "10.0.0.11", "Primary": False},
            ],
            "Ipv6Addresses": [{"Ipv6Address": "2001:db8::10"}],
        }
    ],
}

# get-launch-template-data が返す、元のインスタンスの設定
DATA: dict[str, Any] = {
    "ImageId": "ami-0aaaaaaaaaaaaaaaa",
    "InstanceType": "t3.small",
    "KeyName": "web-key",
    "UserData": USER_DATA,
    "BlockDeviceMappings": [
        {
            "DeviceName": "/dev/sda1",
            "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "DeleteOnTermination": False},
        },
        {
            "DeviceName": "/dev/sdf",
            "Ebs": {"VolumeSize": 100, "VolumeType": "gp3", "SnapshotId": ""},
        },
    ],
    "NetworkInterfaces": [
        {
            "DeviceIndex": 0,
            "NetworkInterfaceId": ENI,
            "SubnetId": SUBNET,
            "Groups": [GROUP],
            # 止まったインスタンスはパブリック IP を失っていて、false を返す
            "AssociatePublicIpAddress": False,
            "DeleteOnTermination": False,
            "Description": "",
            "PrivateIpAddress": "10.0.0.10",
            "PrivateIpAddresses": [
                {"Primary": True, "PrivateIpAddress": "10.0.0.10"},
                {"Primary": False, "PrivateIpAddress": "10.0.0.11"},
            ],
            "Ipv6Addresses": [{"Ipv6Address": "2001:db8::10", "IsPrimaryIpv6": False}],
            "Ipv4Prefixes": [],
            "Ipv6Prefixes": [],
            "InterfaceType": "interface",
            "NetworkCardIndex": 0,
        }
    ],
    "Placement": {
        "AvailabilityZone": "ap-northeast-1a",
        "AvailabilityZoneId": "apne1-az4",
        "GroupName": "",
        "Tenancy": "default",
    },
    "IamInstanceProfile": {"Arn": SOURCE_PROFILE},
    "MetadataOptions": {
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 2,
        "HttpEndpoint": "enabled",
        "State": "applied",
        "InstanceMetadataTags": "disabled",
    },
    "InstanceInitiatedShutdownBehavior": "stop",
    "TagSpecifications": [
        {
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": "web-server"},
                {"Key": "Env", "Value": "prod"},
            ],
        }
    ],
    "InstanceMarketOptions": {
        "MarketType": "spot",
        "SpotOptions": {"SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop"},
    },
    "DisableApiTermination": True,
    "DisableApiStop": False,
    "CreditSpecification": {"CpuCredits": "unlimited"},
    "CpuOptions": {"CoreCount": 1, "ThreadsPerCore": 2},
    "Operator": {"Managed": False, "HiddenByDefault": False},
}

# DATA からコピーする [launch-template]。design の What is copied の表のとおりに直したもの
COPIED: dict[str, Any] = {
    "ImageId": IMAGE_NAME,
    "InstanceType": "t3.small",
    "KeyName": "web-key",
    "UserData": USER_DATA,
    "NetworkInterfaces": [
        {
            "DeviceIndex": 0,
            "SubnetId": SUBNET,
            "Groups": [GROUP],
            # ENI の属性から読んだ、起動のときの値
            "AssociatePublicIpAddress": True,
            "DeleteOnTermination": True,
            "InterfaceType": "interface",
            "NetworkCardIndex": 0,
            "SecondaryPrivateIpAddressCount": 1,
            "Ipv6AddressCount": 1,
        }
    ],
    "Placement": {"AvailabilityZone": "ap-northeast-1a", "Tenancy": "default"},
    "IamInstanceProfile": {"Arn": SOURCE_PROFILE},
    "MetadataOptions": {
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 2,
        "HttpEndpoint": "enabled",
        "InstanceMetadataTags": "disabled",
    },
    "InstanceInitiatedShutdownBehavior": "terminate",
    "TagSpecifications": [
        {
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": "web-server"}, {"Key": "Env", "Value": "prod"}],
        }
    ],
    "CreditSpecification": {"CpuCredits": "unlimited"},
    "CpuOptions": {"CoreCount": 1, "ThreadsPerCore": 2},
}

# 書く設定ファイル。apply と同じく AwsRegion も AwsProfile も書かず、共通ファイルを読む
# ままにする（Defaults = false も書かない）。SSM に登録されているので WaitForSsm も書かない
WRITTEN = f"""\
# Written by temt service import. Edit and run apply to update.

[launch-template]
ImageId = "{IMAGE_NAME}"
InstanceType = "t3.small"
KeyName = "web-key"
UserData = "{USER_DATA}"
IamInstanceProfile = {{ Arn = "{SOURCE_PROFILE}" }}
InstanceInitiatedShutdownBehavior = "terminate"

[[launch-template.NetworkInterfaces]]
DeviceIndex = 0
SubnetId = "{SUBNET}"
Groups = ["{GROUP}"]
AssociatePublicIpAddress = true
DeleteOnTermination = true
InterfaceType = "interface"
NetworkCardIndex = 0
SecondaryPrivateIpAddressCount = 1
Ipv6AddressCount = 1

[launch-template.Placement]
AvailabilityZone = "ap-northeast-1a"
Tenancy = "default"

[launch-template.MetadataOptions]
HttpTokens = "required"
HttpPutResponseHopLimit = 2
HttpEndpoint = "enabled"
InstanceMetadataTags = "disabled"

[[launch-template.TagSpecifications]]
ResourceType = "instance"
Tags = [{{ Key = "Name", Value = "web-server" }}, {{ Key = "Env", Value = "prod" }}]

[launch-template.CreditSpecification]
CpuCredits = "unlimited"

[launch-template.CpuOptions]
CoreCount = 1
ThreadsPerCore = 2
"""

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(text: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in text.splitlines()]


def listing(*lines: str, written: str = WRITTEN) -> str:
    """手順 3 の確認で見せるもの。lines は取り込むものの行で、続けて書く設定ファイルを見せる。"""
    return (
        f"service import {NAME}:\n"
        + "".join(f"  {line}\n" for line in lines)
        + "config file ~/.config/temt/dev-main.toml:\n"
        + "".join(f"  {line}\n" if line else "\n" for line in written.splitlines())
    )


# INSTANCE を取り込むときの確認の、取り込むものの行
SHOWN = (
    f"instance: {SOURCE} (Name=web-server)",
    f"AMI to make: {IMAGE_NAME}",
    "private addresses: 10.0.0.10, 10.0.0.11, 2001:db8::10; they change at every start",
    f"EBS /dev/sdf {DATA_VOLUME} 100 GiB: {NOT_IN_AMI}",
)


class Terminal:
    """端末につながった標準入力の代わり。"""

    def __init__(self, answer: str, *, tty: bool = True) -> None:
        self.answer = answer
        self.tty = tty
        self.reads = 0

    def isatty(self) -> bool:
        return self.tty

    def readline(self) -> str:
        self.reads += 1
        return self.answer


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> Terminal:
    """問いかけに y と答える端末。"""
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)
    return terminal


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """AMI の名前に入る時刻を決める。"""
    monkeypatch.setattr(bake, "localtime", lambda: AT)


def reservations(*items: dict[str, Any]) -> dict[str, Any]:
    """describe-instances の応答。"""
    return {"Reservations": [{"Instances": list(items)}]}


def public(stubs: Stubs, eni: str = ENI, *, value: bool = True) -> None:
    """ENI の、起動のときの AssociatePublicIpAddress を読む。"""
    stubs.ec2.add_response(
        "describe_network_interface_attribute",
        {"NetworkInterfaceId": eni, "AssociatePublicIpAddress": value},
        {"NetworkInterfaceId": eni, "Attribute": "associatePublicIpAddress"},
    )


def registered(stubs: Stubs, *, yes: bool = True) -> None:
    """元のインスタンスが SSM に登録されているかを読む。"""
    found = [{"InstanceId": SOURCE, "PingStatus": "ConnectionLost"}] if yes else []
    stubs.ssm.add_response(
        "describe_instance_information", {"InstanceInformationList": found}, REGISTERED
    )


def read(
    stubs: Stubs,
    item: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    *,
    is_registered: bool = True,
) -> None:
    """手順 1 から 3 の前まで。元のインスタンスと、その設定を読む。"""
    stubs.ec2.add_response("describe_instances", reservations(item or INSTANCE), BY_ID)
    stubs.ec2.add_response(
        "get_launch_template_data", {"LaunchTemplateData": data or DATA}, THE_INSTANCE
    )
    public(stubs)
    registered(stubs, yes=is_registered)


def made(stubs: Stubs, params: dict[str, Any] | None = None) -> None:
    """手順 4 と 5。AMI を作り、pending を経て available になるのを見る。"""
    stubs.ec2.add_response("create_image", {"ImageId": NEW}, params or CREATE)
    image_state(stubs, "pending")
    image_state(stubs, "available")


def image_state(stubs: Stubs, state: str, **more: Any) -> None:
    """手順 5 の問い合わせの応答を 1 つ積む。"""
    stubs.ec2.add_response(
        "describe_images", {"Images": [{"ImageId": NEW, "State": state, **more}]}, THE_IMAGE
    )


def without(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    """data の深い複製から、最上位の keys を除いたもの。"""
    found = copy.deepcopy(data)
    for key in keys:
        found.pop(key, None)
    return found


def text_of(service: Service) -> str:
    return service.conf_path.read_text(encoding="utf-8")


def test_it_imports_a_stopped_instance(
    aws_stubs: Stubs,
    service: Service,
    answer: Terminal,
    clock: Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AMI を作り、available になってから、同じインスタンスを起動する設定ファイルを書く。

    確認の前に、作る AMI、コピーする設定、起動のたびに変わるアドレス、AMI に入れない EBS を
    見せる。元のインスタンスには何もしない。
    """
    identified(aws_stubs)
    read(aws_stubs)
    made(aws_stubs)

    importing.run(service, SOURCE, yes=False)

    # 元のインスタンスを止める、終了する、タグを付ける呼び出しは積んでいないので、呼べば
    # Stubber が失敗する
    aws_stubs.done()
    assert answer.reads == 1
    assert clock.slept == [wait.INTERVAL]
    assert text_of(service) == WRITTEN
    captured = capsys.readouterr()
    assert captured.err == PROMPT
    shown = listing(*SHOWN)
    assert captured.out.startswith(shown)
    assert told(captured.out.removeprefix(shown)) == [
        f"creating the AMI {NEW} ({IMAGE_NAME}) from {SOURCE}",
        f"the AMI {NEW} is available",
        "wrote ~/.config/temt/dev-main.toml",
        f"run 'temt apply --svc dev-main' to make the launch template; {SOURCE} is left as it was",
    ]


def test_the_config_file_it_writes_is_read_as_it_is(aws_stubs: Stubs, service: Service) -> None:
    """書いた設定ファイルはそのまま読め、ほかのサービスと同じく共通ファイルも読む。"""
    read(aws_stubs)
    made(aws_stubs)

    importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    values = conf.load(service)
    assert values.AwsRegion is conf.UNSET
    assert values.AwsProfile is None
    assert values.Defaults is True
    assert values.WaitForSsm is True
    assert values.launch_template == COPIED
    assert values.security_group.ingress == []
    assert not values.role
    # 取り込んだあとに置いた共通ファイルも読む。元に無い項目は足され、ある項目は元の値が勝つ
    defaults_path().write_text(
        '[launch-template]\nInstanceType = "t3.large"\nEbsOptimized = true\n'
        '\n[[security-group.ingress]]\nPort = 22\nCidr = "203.0.113.10/32"\n',
        encoding="utf-8",
    )
    values = conf.load(service)
    assert values.launch_template == {**COPIED, "EbsOptimized": True}
    assert len(values.security_group.ingress) == 1


def test_copied_fixes_the_items_as_the_design_says(aws_stubs: Stubs, ec2: EC2Client) -> None:
    """読み出したときにだけある項目、元の値で起動できない項目、同じインスタンスにならない
    項目を直し、ほかはそのままコピーする。

    アドレスは数にし、ENI はどれも終了とともに消し、AssociatePublicIpAddress は ENI ごとの
    属性から読む。
    """
    item = copy.deepcopy(INSTANCE)
    item["NetworkInterfaces"].append(
        {"NetworkInterfaceId": OTHER_ENI, "Attachment": {"DeviceIndex": 1}}
    )
    data = copy.deepcopy(DATA)
    data["NetworkInterfaces"][0]["Ipv4Prefixes"] = [
        {"Ipv4Prefix": "10.0.1.0/28"},
        {"Ipv4Prefix": "10.0.1.16/28"},
    ]
    data["NetworkInterfaces"][0]["Ipv6Prefixes"] = [{"Ipv6Prefix": "2001:db8:1::/80"}]
    data["NetworkInterfaces"].append(
        {
            "DeviceIndex": 1,
            "NetworkInterfaceId": OTHER_ENI,
            "SubnetId": SUBNET,
            "Groups": [GROUP],
            "DeleteOnTermination": False,
            "PrivateIpAddress": "10.0.0.20",
            "PrivateIpAddresses": [{"Primary": True, "PrivateIpAddress": "10.0.0.20"}],
        }
    )
    public(aws_stubs)
    public(aws_stubs, OTHER_ENI, value=False)

    found, removed = importing.copied(ec2, item, data)

    aws_stubs.done()
    assert removed == []
    assert found["NetworkInterfaces"] == [
        {
            **COPIED["NetworkInterfaces"][0],
            "Ipv4PrefixCount": 2,
            "Ipv6PrefixCount": 1,
        },
        {
            "DeviceIndex": 1,
            "SubnetId": SUBNET,
            "Groups": [GROUP],
            "DeleteOnTermination": True,
            # 副のアドレスも IPv6 アドレスも無い。IPv6 アドレスの数は 0 でも書く
            "Ipv6AddressCount": 0,
            "AssociatePublicIpAddress": False,
        },
    ]
    # ImageId は呼ぶ側が作る AMI の名前にするので、ここでは元のまま
    assert {**found, "NetworkInterfaces": None} == {
        **COPIED,
        "NetworkInterfaces": None,
        "ImageId": DATA["ImageId"],
    }
    # 元の設定は書き換えない
    assert data["InstanceInitiatedShutdownBehavior"] == "stop"
    assert "State" in data["MetadataOptions"]


def test_it_writes_the_aws_defaults_when_the_metadata_options_are_not_read(
    aws_stubs: Stubs, service: Service
) -> None:
    """補う項目は必ず書く。元の値が読めなければ、temt の既定ではなく AWS の既定を書く。

    temt の既定（required、enabled）では、黙って違うインスタンスになる。
    """
    read(aws_stubs, data=without(DATA, "MetadataOptions"))
    made(aws_stubs)

    importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert conf.load(service).launch_template["MetadataOptions"] == {
        "HttpTokens": "optional",
        "InstanceMetadataTags": "disabled",
    }


@pytest.mark.parametrize("read", [None, {}], ids=["absent", "empty"])
@pytest.mark.parametrize(
    ("instance_type", "credits"),
    [
        pytest.param("t2.micro", "standard", id="t2"),
        pytest.param("t3.small", "unlimited", id="t3"),
        pytest.param("t4g.nano", "unlimited", id="t4g"),
    ],
)
def test_it_writes_the_aws_default_of_the_cpu_credits(
    aws_stubs: Stubs,
    ec2: EC2Client,
    instance_type: str,
    credits: str,
    read: dict[str, str] | None,
) -> None:
    """T 系で CreditSpecification が読めなければ、AWS の既定を書く。t2 は standard、ほかの
    T 系は unlimited。temt の既定（unlimited）では、t2 が黙って違うインスタンスになる。

    空のテーブルで返っても、読めなかったものとする。
    """
    data = {**without(DATA, "CreditSpecification"), "InstanceType": instance_type}
    if read is not None:
        data["CreditSpecification"] = read
    public(aws_stubs)

    found, _ = importing.copied(ec2, INSTANCE, data)

    aws_stubs.done()
    assert found["CreditSpecification"] == {"CpuCredits": credits}


@pytest.mark.parametrize(
    ("instance_type", "written", "expected"),
    [
        pytest.param("t2.micro", {"CpuCredits": "unlimited"}, {"CpuCredits": "unlimited"}, id="t2"),
        pytest.param("t3.small", {"CpuCredits": "standard"}, {"CpuCredits": "standard"}, id="t3"),
        pytest.param("m5.large", None, None, id="not a T type"),
        pytest.param("t1.micro", None, None, id="t1"),
    ],
)
def test_it_copies_the_cpu_credits_as_they_are(
    aws_stubs: Stubs,
    ec2: EC2Client,
    instance_type: str,
    written: dict[str, str] | None,
    expected: dict[str, str] | None,
) -> None:
    """読めた CreditSpecification はそのままコピーする。T 系でなければ（t1 も）補わない。"""
    data = {**without(DATA, "CreditSpecification"), "InstanceType": instance_type}
    if written is not None:
        data["CreditSpecification"] = written
    public(aws_stubs)

    found, _ = importing.copied(ec2, INSTANCE, data)

    aws_stubs.done()
    assert found.get("CreditSpecification") == expected


def test_it_starts_the_config_file_with_its_own_heading(aws_stubs: Stubs, service: Service) -> None:
    """service import が書いたことと、書き換えたら apply を実行することを、先頭のコメントで示す。"""
    read(aws_stubs)
    made(aws_stubs)

    importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert text_of(service).startswith(
        "# Written by temt service import. Edit and run apply to update.\n"
    )


def test_it_leaves_out_the_instance_profile_the_source_does_not_have(
    aws_stubs: Stubs, service: Service
) -> None:
    """元にインスタンスプロファイルが無ければ、サービスにも無い。temt のものは付けない。"""
    read(aws_stubs, data=without(DATA, "IamInstanceProfile"))
    made(aws_stubs)

    importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert "IamInstanceProfile" not in text_of(service)
    assert "IamInstanceProfile" not in conf.load(service).launch_template


def test_it_removes_the_tags_aws_reserves_and_reports_them(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """aws: で始まるキーは書けないのでコピーせず、コピーしなかったことをキーごとに知らせる。

    タグが 1 つも残らない TagSpecifications の要素は、コピーしても何も付けないので書かない。
    """
    data = copy.deepcopy(DATA)
    data["TagSpecifications"] = [
        {
            "ResourceType": "instance",
            "Tags": [
                {"Key": "aws:cloudformation:stack-name", "Value": "web"},
                {"Key": "Name", "Value": "web-server"},
                {"Key": "Env", "Value": "prod"},
            ],
        },
        {
            "ResourceType": "volume",
            "Tags": [{"Key": "aws:ec2launchtemplate:id", "Value": "lt-0123456789abcdef0"}],
        },
    ]
    read(aws_stubs, data=data)
    made(aws_stubs)

    importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert 'Key = "aws:' not in text_of(service)
    assert conf.load(service).launch_template["TagSpecifications"] == [
        COPIED["TagSpecifications"][0]
    ]
    assert 'ResourceType = "volume"' not in text_of(service)
    assert capsys.readouterr().err == (
        "temt: warning: the tag aws:cloudformation:stack-name is not copied; AWS reserves "
        "keys starting with aws:\n"
        "temt: warning: the tag aws:ec2launchtemplate:id is not copied; AWS reserves keys "
        "starting with aws:\n"
    )


def test_it_warns_and_does_not_wait_for_ssm_when_the_source_is_not_registered(
    aws_stubs: Stubs, service: Service, answer: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    """SSM に登録されていなければ、確認の前に警告し、WaitForSsm = false を書く。"""
    identified(aws_stubs)
    read(aws_stubs, is_registered=False)
    made(aws_stubs)

    importing.run(service, SOURCE, yes=False)

    aws_stubs.done()
    written = WRITTEN.replace("update.\n", "update.\nWaitForSsm = false\n", 1)
    assert text_of(service) == written
    assert conf.load(service).WaitForSsm is False
    captured = capsys.readouterr()
    assert captured.err == (
        f"temt: warning: {SOURCE} is not registered with SSM; temt logs in through the SSM "
        "agent and EC2 Instance Connect (see docs/usage.md); start will not wait for it\n" + PROMPT
    )
    assert captured.out.startswith(listing(*SHOWN, written=written))


def test_it_lists_every_ebs_volume_left_out_of_the_ami(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """ルートボリューム以外の EBS を 1 行ずつ挙げ、どれも NoDevice にする。

    大きさは get-launch-template-data から取り、無ければ ? とする。インスタンスストアは
    EBS ではないので挙げない。名前のタグもアドレスも無ければ、その行は出さない。
    """
    item = copy.deepcopy(INSTANCE)
    del item["Tags"]
    item["NetworkInterfaces"][0]["PrivateIpAddresses"] = []
    item["NetworkInterfaces"][0]["Ipv6Addresses"] = []
    item["BlockDeviceMappings"] += [
        {"DeviceName": "/dev/sdg", "Ebs": {"VolumeId": LOG_VOLUME}},
        {"DeviceName": "/dev/sdh"},
    ]
    data = copy.deepcopy(DATA)
    data["BlockDeviceMappings"].append({"DeviceName": "/dev/sdh", "VirtualName": "ephemeral0"})
    read(aws_stubs, item, data)
    params = copy.deepcopy(CREATE)
    params["BlockDeviceMappings"] = [
        {"DeviceName": "/dev/sdf", "NoDevice": ""},
        {"DeviceName": "/dev/sdg", "NoDevice": ""},
        {"DeviceName": "/dev/sda1", "Ebs": {"DeleteOnTermination": True}},
    ]
    made(aws_stubs, params)

    importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert capsys.readouterr().out.startswith(
        listing(
            f"instance: {SOURCE}",
            f"AMI to make: {IMAGE_NAME}",
            f"EBS /dev/sdf {DATA_VOLUME} 100 GiB: {NOT_IN_AMI}",
            f"EBS /dev/sdg {LOG_VOLUME} ? GiB: {NOT_IN_AMI}",
        )
    )


def test_it_stops_when_the_config_file_exists(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """取り込むのは設定ファイルの無いサービスにだけである。AWS には触れない。"""
    before = text_of(configured)

    assert cli.main(["temt", "service", "import", "--svc", "dev-main", SOURCE, "--yes"]) == 1

    aws_stubs.done()
    assert aws_stubs.passed == []
    assert text_of(configured) == before
    assert capsys.readouterr().err == (
        "temt: ~/.config/temt/dev-main.toml exists; service import makes a new service only\n"
    )


@pytest.mark.parametrize("state", ["pending", "running", "stopping", "shutting-down", "terminated"])
def test_it_stops_when_the_instance_is_not_stopped(
    aws_stubs: Stubs, service: Service, answer: Terminal, state: str
) -> None:
    """止めるよう告げて終わる。temt は元のインスタンスを止めも開始もしない。"""
    item = copy.deepcopy(INSTANCE)
    item["State"] = {"Name": state}
    aws_stubs.ec2.add_response("describe_instances", reservations(item), BY_ID)

    with pytest.raises(
        TemtError,
        match=f"^{SOURCE} is {state}; stop it first \\(service import changes nothing on it\\)$",
    ):
        importing.run(service, SOURCE, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert not service.conf_path.exists()


def test_it_stops_when_the_instance_is_not_found(aws_stubs: Stubs, service: Service) -> None:
    aws_stubs.ec2.add_client_error(
        "describe_instances",
        service_error_code=importing.NO_SUCH_INSTANCE,
        http_status_code=400,
        expected_params=BY_ID,
    )

    with pytest.raises(TemtError, match=f"^no such instance: {SOURCE}$"):
        importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert not service.conf_path.exists()


def test_it_stops_when_nothing_is_returned(aws_stubs: Stubs, service: Service) -> None:
    """ID で引いて何も返らないときも、見つからないものとする。"""
    aws_stubs.ec2.add_response("describe_instances", {"Reservations": []}, BY_ID)

    with pytest.raises(TemtError, match=f"^no such instance: {SOURCE}$"):
        importing.run(service, SOURCE, yes=True)

    aws_stubs.done()


def test_the_error_code_is_the_one_aws_returns() -> None:
    """名前を間違えると、無いインスタンスを AWS の誤りのまま知らせてしまう。"""
    assert importing.NO_SUCH_INSTANCE == "InvalidInstanceID.NotFound"


def test_it_stops_when_a_setting_cannot_be_copied(
    aws_stubs: Stubs, service: Service, answer: Terminal
) -> None:
    """起動テンプレートに書けない設定があれば、AMI を作る前に止める。"""
    data = copy.deepcopy(DATA)
    data["TagSpecifications"][0]["Tags"].append({"Key": "Owner", "Value": "${temt:owner}"})
    aws_stubs.ec2.add_response("describe_instances", reservations(INSTANCE), BY_ID)
    aws_stubs.ec2.add_response(
        "get_launch_template_data", {"LaunchTemplateData": data}, THE_INSTANCE
    )
    public(aws_stubs)

    with pytest.raises(TemtError) as caught:
        importing.run(service, SOURCE, yes=False)

    # create_image は積んでいないので、AMI を作れば Stubber が失敗する
    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"cannot copy the settings of {SOURCE}: "
        "launch-template.TagSpecifications[0].Tags[2].Value uses an unknown variable: "
        "${temt:owner}"
    )
    assert not service.conf_path.exists()


def test_it_changes_nothing_when_the_answer_is_not_y(
    aws_stubs: Stubs,
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identified(aws_stubs)
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    read(aws_stubs)

    assert cli.main(["temt", "service", "import", "--svc", "dev-main", SOURCE]) == 1

    # create_image は積んでいないので、AMI を作れば Stubber が失敗する
    aws_stubs.done()
    assert not service.conf_path.exists()
    captured = capsys.readouterr()
    assert captured.out == listing(*SHOWN)
    assert captured.err == PROMPT + "temt: cancelled\n"


def test_it_needs_yes_when_stdin_is_not_a_terminal(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("y\n", tty=False))
    read(aws_stubs)

    with pytest.raises(TemtError, match="stdin is not a terminal; pass --yes"):
        importing.run(service, SOURCE, yes=False)

    aws_stubs.done()
    assert not service.conf_path.exists()


def test_it_writes_nothing_when_the_ami_cannot_be_made(aws_stubs: Stubs, service: Service) -> None:
    """作れなければ、AWS が返した理由を添えて終わる。設定ファイルは書かない。"""
    read(aws_stubs)
    aws_stubs.ec2.add_response("create_image", {"ImageId": NEW}, CREATE)
    image_state(
        aws_stubs, "failed", StateReason={"Code": "x", "Message": "the KMS key is disabled"}
    )

    with pytest.raises(TemtError, match=f"^the AMI {NEW} is failed: the KMS key is disabled$"):
        importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert not service.conf_path.exists()


def test_it_writes_nothing_on_an_aws_error_while_waiting(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """待つあいだの AWS の誤りは、そのまま上げる。その前に、まだ何も書いていないと知らせる。"""
    read(aws_stubs)
    aws_stubs.ec2.add_response("create_image", {"ImageId": NEW}, CREATE)
    aws_stubs.ec2.add_client_error(
        "describe_images",
        service_error_code="RequestExpired",
        http_status_code=400,
        expected_params=THE_IMAGE,
    )

    with pytest.raises(ClientError) as raised:
        importing.run(service, SOURCE, yes=True)

    assert raised.value.operation_name == "DescribeImages"
    aws_stubs.done()
    assert not service.conf_path.exists()
    assert capsys.readouterr().err == f"the AMI {NEW} is not available yet; nothing was written\n"


def test_it_stops_when_an_ami_with_the_name_exists(aws_stubs: Stubs, service: Service) -> None:
    """同じ分に作った AMI があれば、作れずに止まる。設定ファイルは書かない。"""
    read(aws_stubs)
    aws_stubs.ec2.add_client_error(
        "create_image",
        service_error_code=bake.DUPLICATE_NAME,
        http_status_code=400,
        expected_params=CREATE,
    )

    with pytest.raises(TemtError) as caught:
        importing.run(service, SOURCE, yes=True)

    assert str(caught.value) == (f"an AMI named {IMAGE_NAME} already exists")
    aws_stubs.done()
    assert not service.conf_path.exists()


def test_it_does_not_overwrite_a_config_file_made_while_waiting(
    aws_stubs: Stubs, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """書くのは新しく作るときだけである。待つあいだに作られていれば、残して止める。"""
    mine = 'AwsRegion = "us-east-1"\n'

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        service.conf_path.parent.mkdir(parents=True, exist_ok=True)
        service.conf_path.write_text(mine, encoding="utf-8")

    monkeypatch.setattr(wait, "sleep", sleep)
    read(aws_stubs)
    made(aws_stubs)

    with pytest.raises(
        TemtError,
        match="^~/.config/temt/dev-main.toml was made while importing; it is left as it is$",
    ):
        importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert text_of(service) == mine


def test_it_does_not_write_the_profile_and_the_region_given_on_the_command_line(
    aws_stubs: Stubs, service: Service
) -> None:
    """--profile と --region は、その実行で元のインスタンスを探すのに使い、設定ファイルには
    書かない。
    """
    read(aws_stubs)
    made(aws_stubs)

    assert (
        cli.main(
            [
                "temt",
                "service",
                "import",
                "--svc",
                "dev-main",
                SOURCE,
                "--profile",
                "work",
                "--region",
                "us-west-2",
                "--yes",
            ]
        )
        == 0
    )

    aws_stubs.done()
    assert aws_stubs.passed[0].AwsProfile == "work"
    assert aws_stubs.passed[0].AwsRegion == "us-west-2"
    assert text_of(service) == WRITTEN


def test_it_reads_the_defaults_file_and_writes_none_of_it(
    aws_stubs: Stubs, service: Service
) -> None:
    """共通ファイルの AwsProfile と AwsRegion で取り込み、どちらも書かない。

    書いた設定ファイルも共通ファイルを読むので、取り込んだサービスはそのまま同じ
    プロファイルとリージョンを使う。[launch-template] は、元のインスタンスの値が勝つ。
    """
    defaults_path().parent.mkdir(parents=True)
    defaults_path().write_text(
        'AwsProfile = "work"\nAwsRegion = "us-east-1"\n\n'
        '[launch-template]\nInstanceType = "t3.large"\n',
        encoding="utf-8",
    )
    read(aws_stubs)
    made(aws_stubs)

    importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert aws_stubs.passed[0].AwsProfile == "work"
    assert aws_stubs.passed[0].AwsRegion == "us-east-1"
    assert text_of(service) == WRITTEN
    values = conf.load(service)
    assert values.AwsProfile == "work"
    assert values.AwsRegion == "us-east-1"
    assert values.launch_template["InstanceType"] == "t3.small"


def test_it_does_not_write_the_profile_from_the_environment(
    aws_stubs: Stubs, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AWS_PROFILE は手元の環境のものなので、設定ファイルに書かない。"""
    monkeypatch.setenv("AWS_PROFILE", "from-env")
    read(aws_stubs)
    made(aws_stubs)

    importing.run(service, SOURCE, yes=True)

    aws_stubs.done()
    assert "AwsProfile" not in text_of(service)


def test_it_stops_when_another_command_holds_the_lock(aws_stubs: Stubs, service: Service) -> None:
    """ロックは AWS を読むより先に取る。取れなければ待たずに終わる。"""
    held = home.prepare_known_hosts(service) / lock.LOCK_NAME
    with held.open("a", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TemtError, match=f"another temt command is running for {NAME}"):
            importing.run(service, SOURCE, yes=True)

    # 応答を 1 つも積んでいないので、AWS を読んでいれば Stubber が失敗する
    aws_stubs.done()
    assert not service.conf_path.exists()


def test_service_import_takes_one_instance_id() -> None:
    request = cli.parse(["service", "import", "--svc", "dev-main", SOURCE, "--yes"])
    assert request.instance_ids == (SOURCE,)
    assert request.yes is True
    with pytest.raises(UsageError):
        cli.parse(["service", "import", "--svc", "dev-main"])
    with pytest.raises(UsageError, match="service import: the instance id "):
        cli.parse(["service", "import", "--svc", "dev-main", "web-server"])
    with pytest.raises(UsageError):
        cli.parse(["service", "import", "--svc", "dev-main", SOURCE, "i-0fedcba9876543210"])
