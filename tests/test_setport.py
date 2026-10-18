"""ssh set-port の本体のテスト。AWS は Stubber で、標準入力と時計と時刻は偽物に差し替える。

端から端まで（setport.run か cli.main）を通し、手順の順序と、失敗したときに何を残すかを
確かめる。Stubber は積んだ順にしか応答しないので、応答を積む順が手順の順序を表す。
インスタンスで動くスクリプトそのものは test_sshd が確かめ、ここではその出力だけを積む。
"""

import re
import sys
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY
from conftest import Clock, Stubs, an_instance, reservations, taken

from temt import bake, cli, conf, home, setport, sshd, wait
from temt.msg import TemtError
from temt.names import Service, defaults_path, ssh_dir

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
# 設定ファイル（conftest の configured）とテンプレートが指している AMI と、焼いた AMI
OLD = "ami-0123456789abcdef0"
NEW = "ami-0fedcba9876543210"
SUBNET = "subnet-0123456789abcdef0"
VPC = "vpc-0123456789abcdef0"
GROUP = "sg-0123456789abcdef0"
# AMI のルートボリュームのデバイス名
ROOT_DEVICE = "/dev/sda1"
# temt のものでないセキュリティグループ
FOREIGN = "sg-0aaaaaaaaaaaaaaaa"
# AMI の名前に入る時刻。2026-10-18 12:34（ローカル時刻）
AT = time.struct_time((2026, 10, 18, 12, 34, 0, 6, 291, -1))
IMAGE_NAME = "temt-dev-main-2610181234"
WHEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
COMMAND = "11111111-0000-4000-8000-000000000001"
SECOND = "11111111-0000-4000-8000-000000000002"
HOME_CIDR = "198.51.100.7/32"
OFFICE_CIDR = "203.0.113.0/24"
INGRESS = "[[security-group.ingress]]\n"

SEARCH = {
    "Filters": [
        {"Name": "tag:temt:service", "Values": ["dev-main"]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        },
    ]
}
BY_ID = {"InstanceIds": [INSTANCE]}
TEMPLATES = {"LaunchTemplateNames": [TEMPLATE]}
THE_TEMPLATE = {"LaunchTemplateName": TEMPLATE}
THE_GROUPS = {"Filters": [{"Name": "group-name", "Values": [NAME]}]}
# 手順 1 で、設定ファイルの SubnetId と、その VPC にある temt のセキュリティグループを引く
THE_SUBNET = {"Filters": [{"Name": "subnet-id", "Values": [SUBNET]}]}
THE_GROUP = {
    "Filters": [
        {"Name": "group-name", "Values": [NAME]},
        {"Name": "vpc-id", "Values": [VPC]},
    ]
}
AGENT = {"Filters": [{"Key": "InstanceIds", "Values": [INSTANCE]}]}
INVOCATION = {"CommandId": COMMAND, "InstanceId": INSTANCE}
LAUNCH = {
    "LaunchTemplate": {"LaunchTemplateName": TEMPLATE, "Version": "$Default"},
    "MinCount": 1,
    "MaxCount": 1,
    "ClientToken": ANY,
}
TAGS = [{"Key": "Name", "Value": IMAGE_NAME}, {"Key": "temt:service", "Value": "dev-main"}]
CREATE = {
    "InstanceId": INSTANCE,
    "Name": IMAGE_NAME,
    "BlockDeviceMappings": [],
    "TagSpecifications": [
        {"ResourceType": "image", "Tags": TAGS},
        {"ResourceType": "snapshot", "Tags": TAGS},
    ],
}
# temt が付けるタグ
MARKS = [{"Key": "Name", "Value": NAME}, {"Key": "temt:service", "Value": "dev-main"}]
# 手順 2 の問いかけ。標準エラー出力に書く
PROMPT = f"set the sshd ports of {NAME} to 2222? [y/N] "
# スクリプトの出力。成功と、再起動の前と後の失敗
CHANGED = "temt-sshd: before 22\ntemt-sshd: listening 2222\ntemt-sshd: changed 22 -> 2222\n"
REJECTED = (
    "temt-sshd: before 22\n"
    "temt-sshd: listening 22\n"
    "temt-sshd: failed before the restart: sshd -t rejected the ports 2222\n"
)
NOT_LISTENING = (
    "temt-sshd: before 22\n"
    "temt-sshd: listening \n"
    "temt-sshd: failed after the restart: sshd did not listen on 2222\n"
)
LEFT = f"{INSTANCE} is left running and costs money"

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(out: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in out.splitlines()]


class Terminal:
    """端末につながった標準入力の代わり。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.reads = 0

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        self.reads += 1
        return self.answer


class Pipe:
    """端末につながっていない標準入力の代わり。読まれれば落ちる。"""

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        pytest.fail("stdin was read")


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """AMI の名前に入る時刻を決める。"""
    monkeypatch.setattr(bake, "localtime", lambda: AT)


@pytest.fixture
def ready(configured: Service) -> Service:
    """apply 済みのサービス。ssh の config に Host ブロックがある（Port は 22）。"""
    _ = home.write_edit(home.detect_host_block(configured))
    assert block_port(configured) == "22"
    return configured


def ssh_config() -> str:
    """ssh ディレクトリの config の中身。"""
    return (ssh_dir() / "config").read_text(encoding="utf-8")


def block_port(service: Service) -> str | None:
    """Host <名前> ブロックの最初の Port の値。無ければ None。"""
    inside = False
    for line in ssh_config().splitlines():
        words = line.split()
        if not words:
            continue
        keyword = words[0].lower()
        if keyword in ("host", "match"):
            inside = keyword == "host" and service.name in words[1:]
        elif inside and keyword == "port":
            return words[1]
    return None


def add_rules(service: Service, text: str) -> None:
    """設定ファイルの末尾に受信規則を足す。"""
    with service.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write(text)


def template_data(image_id: str, group: str = GROUP, **changes: Any) -> dict[str, Any]:
    """設定ファイル（conftest の configured）を apply したテンプレートの中身。

    group は、temt が Groups に入れたセキュリティグループ。changes で項目を替える。
    """
    return {
        "ImageId": image_id,
        "InstanceType": "t3.micro",
        "InstanceInitiatedShutdownBehavior": "terminate",
        "IamInstanceProfile": {"Name": NAME},
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "SubnetId": SUBNET,
                "AssociatePublicIpAddress": True,
                "Groups": [group],
            }
        ],
        "MetadataOptions": {"HttpTokens": "required", "InstanceMetadataTags": "enabled"},
        "CreditSpecification": {"CpuCredits": "unlimited"},
        "TagSpecifications": [
            {"ResourceType": kind, "Tags": MARKS}
            for kind in ("instance", "volume", "network-interface")
        ],
        **changes,
    }


def a_version(
    number: int,
    image_id: str,
    *,
    group: str = GROUP,
    default: bool = True,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """describe-launch-template-versions が返すバージョン 1 つ分。data を省けば apply が
    作った形。

    group は、テンプレートが使うセキュリティグループ。
    """
    return {
        "LaunchTemplateName": TEMPLATE,
        "VersionNumber": number,
        "DefaultVersion": default,
        "CreateTime": WHEN,
        "LaunchTemplateData": template_data(image_id, group) if data is None else data,
    }


def the_image() -> dict[str, Any]:
    """設定ファイルの ImageId の AMI。ルートボリュームは 8 GiB。"""
    return {
        "ImageId": OLD,
        "State": "available",
        "RootDeviceName": ROOT_DEVICE,
        "BlockDeviceMappings": [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 8}}],
    }


def compared(stubs: Stubs, *versions: dict[str, Any]) -> None:
    """手順 1 のうち、bake と同じくテンプレートを設定ファイルと比べる分を積む。

    テンプレートを読み、設定ファイルから中身を組み立てる（ImageId の AMI、SubnetId の VPC、
    その VPC にある temt のセキュリティグループを引く）。
    """
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": list(versions or (a_version(1, OLD),))},
        THE_TEMPLATE,
    )
    stubs.ec2.add_response("describe_images", {"Images": [the_image()]}, {"ImageIds": [OLD]})
    stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "172.31.0.0/20"}]},
        THE_SUBNET,
    )
    stubs.ec2.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": GROUP, "GroupName": NAME, "VpcId": VPC}]},
        THE_GROUP,
    )


def applied(stubs: Stubs) -> None:
    """起動テンプレートがある、という応答を積む。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )


def checked(stubs: Stubs, *items: dict[str, Any]) -> None:
    """手順 1。テンプレートがあり、設定ファイルと揃っていて、インスタンスを探す。"""
    applied(stubs)
    compared(stubs)
    stubs.ec2.add_response("describe_instances", reservations(*items), SEARCH)


def online(stubs: Stubs) -> None:
    """SSM エージェントが Online と報告する。"""
    stubs.ssm.add_response(
        "describe_instance_information",
        {"InstanceInformationList": [{"InstanceId": INSTANCE, "PingStatus": "Online"}]},
        AGENT,
    )


def brought_up(stubs: Stubs) -> None:
    """手順 3。動いているインスタンスを使い、エージェントを待つ。"""
    applied(stubs)
    stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), SEARCH
    )
    online(stubs)


def send(ports: list[int]) -> dict[str, Any]:
    """手順 4 の send-command の引数。"""
    return {
        "InstanceIds": [INSTANCE],
        "DocumentName": "AWS-RunShellScript",
        "Parameters": {"commands": sshd.script(ports), "executionTimeout": ["300"]},
        "TimeoutSeconds": 60,
        "Comment": "temt set-port: set the sshd ports",
    }


def sent(stubs: Stubs, ports: list[int], command_id: str = COMMAND) -> None:
    """スクリプトを受け付けた応答を積む。"""
    stubs.ssm.add_response("send_command", {"Command": {"CommandId": command_id}}, send(ports))


def invocation(
    stubs: Stubs, status: str, output: str = "", error: str = "", command_id: str = COMMAND
) -> None:
    """送った Run Command の状態を積む。"""
    response: dict[str, Any] = {"Status": status}
    if output:
        response["StandardOutputContent"] = output
    if error:
        response["StandardErrorContent"] = error
    stubs.ssm.add_response(
        "get_command_invocation",
        response,
        {"CommandId": command_id, "InstanceId": INSTANCE},
    )


def ran(stubs: Stubs, ports: list[int], status: str, output: str, error: str = "") -> None:
    """手順 4。スクリプトを送り、1 回待ってから結果を読む。"""
    sent(stubs, ports)
    invocation(stubs, status, output, error)


def baked(stubs: Stubs) -> None:
    """手順 5。bake と同じく、止め、AMI を作り、向け直し、終了する。"""
    stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    stubs.ec2.add_response(
        "stop_instances",
        {"StoppingInstances": [{"InstanceId": INSTANCE, "CurrentState": {"Name": "stopping"}}]},
        BY_ID,
    )
    stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopping")), BY_ID
    )
    stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopped")), BY_ID
    )
    stubs.ec2.add_response("create_image", {"ImageId": NEW}, CREATE)
    stubs.ec2.add_response(
        "describe_images", {"Images": [{"ImageId": NEW, "State": "available"}]}, {"ImageIds": [NEW]}
    )
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [a_version(1, OLD)]},
        THE_TEMPLATE,
    )
    stubs.ec2.add_response(
        "create_launch_template_version",
        {"LaunchTemplateVersion": {"VersionNumber": 2}},
        {
            "LaunchTemplateName": TEMPLATE,
            "SourceVersion": "1",
            "LaunchTemplateData": {"ImageId": NEW},
        },
    )
    stubs.ec2.add_response(
        "modify_launch_template", {}, {"LaunchTemplateName": TEMPLATE, "DefaultVersion": "2"}
    )
    stubs.ec2.add_response(
        "delete_launch_template_versions",
        {"SuccessfullyDeletedLaunchTemplateVersions": [{"VersionNumber": 1}]},
        {"LaunchTemplateName": TEMPLATE, "Versions": ["1"]},
    )
    stubs.ssm.add_response(
        "get_parameters_by_path", {"Parameters": []}, {"Path": f"/{NAME}", "Recursive": True}
    )
    stubs.ec2.add_response(
        "terminate_instances",
        {
            "TerminatingInstances": [
                {"InstanceId": INSTANCE, "CurrentState": {"Name": "shutting-down"}}
            ]
        },
        BY_ID,
    )
    stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "terminated")), BY_ID
    )


def guided(stubs: Stubs, *, group: str = GROUP, found: tuple[str, ...] = (GROUP,)) -> None:
    """手順 7。テンプレートの既定バージョンと、temt のセキュリティグループを読む。

    group はテンプレートが使うセキュリティグループ、found は temt の名前のもの。
    """
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [a_version(2, NEW, group=group)]},
        THE_TEMPLATE,
    )
    stubs.ec2.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": one, "GroupName": NAME} for one in found]},
        THE_GROUPS,
    )


def through(stubs: Stubs, ports: list[int], output: str) -> None:
    """動いているインスタンスで、スクリプトが成功し、焼き、受信規則を案内するまでを積む。"""
    checked(stubs, an_instance(INSTANCE, "running"))
    brought_up(stubs)
    ran(stubs, ports, "Success", output)
    baked(stubs)
    guided(stubs)


def guidance(out: str) -> list[str]:
    """受信規則の案内。直接接続の行から後ろ。"""
    lines = told(out)
    start = lines.index(
        "for direct connection (TEMT_LOGIN=direct), the ingress rules must allow the ports"
    )
    return lines[start + 1 :]


def test_it_sets_the_ports_bakes_and_writes_port(
    aws_stubs: Stubs,
    ready: Service,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)
    before = ssh_config()
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=False)

    aws_stubs.done()
    assert terminal.reads == 1
    # Host ブロックの Port の行だけを書き換える
    after = ssh_config()
    assert block_port(ready) == "2222"
    changed = [
        (old, new)
        for old, new in zip(before.splitlines(), after.splitlines(), strict=True)
        if old != new
    ]
    assert len(changed) == 1
    assert re.fullmatch(r"\s+Port\s+22", changed[0][0])
    assert re.fullmatch(r"\s+Port\s+2222", changed[0][1])
    # 焼いた AMI を設定ファイルが指す
    assert conf.load(ready).launch_template.get("ImageId") == IMAGE_NAME
    captured = capsys.readouterr()
    assert captured.err == PROMPT
    listing = (
        f"set-port {NAME}:\n"
        f"  start {NAME} if it is not running\n"
        "  set the ports sshd listens on to 2222 through Run Command\n"
        "  bake the instance, apps not yet baked included; the instance is terminated\n"
        f"  write Port 2222 in the Host {NAME} block of ~/.ssh/temt/config\n"
    )
    assert captured.out.startswith(listing)
    lines = told(captured.out.removeprefix(listing))
    # スクリプトの出力を見せ、焼いてから Port を書き換え、受信規則を案内する
    shown = lines.index("temt-sshd: before 22")
    assert lines[shown : shown + 3] == CHANGED.splitlines()
    assert lines.index(f"{INSTANCE} is ready for login") < shown
    wrote = lines.index(f"wrote Port 2222 in the Host {NAME} block of ~/.ssh/temt/config")
    assert lines.index(f"baked {IMAGE_NAME} ({NEW}); the next start boots from it") < wrote
    assert guidance(captured.out) == [
        f"  {NAME} sg add 2222",
        f"  {NAME} apply",
    ]
    # スクリプトの結果を 1 回、停止を 1 回待った
    assert clock.slept == [wait.INTERVAL, wait.INTERVAL]


def test_it_bakes_even_when_the_ports_are_unchanged(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """unchanged でも焼く。Port が既に揃っていれば書かない。"""
    _ = home.write_edit(home.detect_port(ready, 2222))
    before = ssh_config()
    unchanged = "temt-sshd: before 2222\ntemt-sshd: listening 2222\ntemt-sshd: unchanged 2222\n"
    through(aws_stubs, [2222], unchanged)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert ssh_config() == before
    lines = told(capsys.readouterr().out)
    assert f"baked {IMAGE_NAME} ({NEW}); the next start boots from it" in lines
    assert not any(line.startswith("wrote Port") for line in lines)


def test_it_points_port_at_the_first_port(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    output = (
        "temt-sshd: before 22\ntemt-sshd: listening 443,2222\ntemt-sshd: changed 22 -> 2222,443\n"
    )
    through(aws_stubs, [2222, 443], output)

    setport.run(ready, [2222, 443], yes=True)

    aws_stubs.done()
    assert block_port(ready) == "2222"
    out = capsys.readouterr().out
    assert "  set the ports sshd listens on to 2222,443 through Run Command\n" in out
    assert guidance(out) == [
        f"  {NAME} sg add 2222",
        f"  {NAME} sg add 443",
        f"  {NAME} apply",
    ]


def test_it_launches_and_resends_while_the_agent_cannot_take_it(
    aws_stubs: Stubs, ready: Service, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    """インスタンスが無ければ起動する。起動の直後に断られれば、次の回に送り直す。"""
    checked(aws_stubs)
    applied(aws_stubs)
    aws_stubs.ec2.add_response("describe_instances", reservations(), SEARCH)
    aws_stubs.ec2.add_response("run_instances", {"Instances": [{"InstanceId": INSTANCE}]}, LAUNCH)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    online(aws_stubs)
    aws_stubs.ssm.add_client_error(
        "send_command",
        service_error_code="InvalidInstanceId",
        service_message="Instances [[i-0123456789abcdef0]] not in a valid state",
        expected_params=send([2222]),
    )
    sent(aws_stubs, [2222])
    # 送った直後は、まだ実行の記録が無いことがある
    aws_stubs.ssm.add_client_error(
        "get_command_invocation",
        service_error_code="InvocationDoesNotExist",
        expected_params=INVOCATION,
    )
    invocation(aws_stubs, "InProgress")
    invocation(aws_stubs, "Success", CHANGED)
    baked(aws_stubs)
    guided(aws_stubs)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert block_port(ready) == "2222"
    lines = told(capsys.readouterr().out)
    assert f"launched {INSTANCE} from {TEMPLATE}" in lines
    # 送り直しに 1 回、記録を 2 回、停止を 1 回待った
    assert clock.slept == [wait.INTERVAL] * 5


def test_it_holds_the_lock_through_the_bake_and_port(
    aws_stubs: Stubs, ready: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """焼くときも Port を書くときも、サービスのロックを持ったままである。"""
    held: list[str] = []
    make, write_edit = bake.make, home.write_edit

    def baking(*args: Any) -> None:
        if not taken(ready):
            held.append("bake")
        make(*args)

    def writing(edit: home.Edit) -> bool:
        if not taken(ready):
            held.append("port")
        return write_edit(edit)

    monkeypatch.setattr(bake, "make", baking)
    monkeypatch.setattr(home, "write_edit", writing)
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert held == ["bake", "port"]
    assert taken(ready)


def test_it_resumes_a_stopped_instance(aws_stubs: Stubs, ready: Service) -> None:
    checked(aws_stubs, an_instance(INSTANCE, "stopped"))
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "stopped")), SEARCH
    )
    aws_stubs.ec2.add_response(
        "start_instances",
        {"StartingInstances": [{"InstanceId": INSTANCE, "CurrentState": {"Name": "pending"}}]},
        BY_ID,
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "pending")), BY_ID
    )
    aws_stubs.ec2.add_response(
        "describe_instances", reservations(an_instance(INSTANCE, "running")), BY_ID
    )
    online(aws_stubs)
    ran(aws_stubs, [2222], "Success", CHANGED)
    baked(aws_stubs)
    guided(aws_stubs)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert block_port(ready) == "2222"


# 手順 1。何かを変える前に止まる


def test_it_stops_without_the_launch_template(aws_stubs: Stubs, ready: Service) -> None:
    aws_stubs.ec2.add_client_error(
        "describe_launch_templates",
        service_error_code="InvalidLaunchTemplateName.NotFoundException",
        expected_params=TEMPLATES,
    )

    with pytest.raises(
        TemtError, match=f"no launch template {TEMPLATE}; run 'temt apply --svc dev-main' first"
    ):
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()


@pytest.mark.parametrize("text", [None, "Host other\n    Port 22\n"], ids=["no file", "no block"])
def test_it_stops_without_the_host_block(
    aws_stubs: Stubs, configured: Service, text: str | None
) -> None:
    if text is not None:
        ssh_dir().mkdir(parents=True)
        _ = (ssh_dir() / "config").write_text(text, encoding="utf-8")
    applied(aws_stubs)

    with pytest.raises(
        TemtError,
        match=(
            f"no Host {NAME} block in ~/.ssh/temt/config; run 'temt apply --svc dev-main' first"
        ),
    ):
        setport.run(configured, [2222], yes=True)

    aws_stubs.done()
    assert (ssh_dir() / "config").exists() == (text is not None)


def test_it_stops_when_the_template_differs(aws_stubs: Stubs, ready: Service) -> None:
    """焼くときに止まる条件は、sshd を変える前に確かめる。"""
    _ = conf.update(ready, conf.load(ready), conf.launch_template_key("InstanceType"), "t3.small")
    applied(aws_stubs)
    compared(aws_stubs)

    with pytest.raises(TemtError, match="does not match .* in InstanceType; run 'temt apply"):
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()


def test_it_stops_when_the_security_group_is_not_made(aws_stubs: Stubs, ready: Service) -> None:
    """設定ファイルの SubnetId の VPC に temt のセキュリティグループがまだ無ければ、テンプレートの
    Groups が揃っていないので、sshd を変える前に止まる。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [a_version(1, OLD)]},
        THE_TEMPLATE,
    )
    aws_stubs.ec2.add_response("describe_images", {"Images": [the_image()]}, {"ImageIds": [OLD]})
    aws_stubs.ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "172.31.0.0/20"}]},
        THE_SUBNET,
    )
    aws_stubs.ec2.add_response("describe_security_groups", {"SecurityGroups": []}, THE_GROUP)

    with pytest.raises(
        TemtError,
        match=re.escape(
            "in NetworkInterfaces[DeviceIndex=0].Groups; run 'temt apply --svc dev-main' first"
        ),
    ):
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()


def test_it_stops_when_the_image_is_gone(aws_stubs: Stubs, ready: Service) -> None:
    """ImageId の AMI が使えなければ、組み立てられないので、ImageId が揃っていないとして
    apply を促して止まる。行を消す案内は apply がする。"""
    applied(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_launch_template_versions",
        {"LaunchTemplateVersions": [a_version(1, OLD)]},
        THE_TEMPLATE,
    )
    aws_stubs.ec2.add_response("describe_images", {"Images": []}, {"ImageIds": [OLD]})

    with pytest.raises(TemtError) as caught:
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the launch template {TEMPLATE} does not match ~/.config/temt/dev-main.toml in "
        "ImageId; run 'temt apply --svc dev-main' first"
    )


def test_it_stops_when_the_root_volume_is_too_large(aws_stubs: Stubs, ready: Service) -> None:
    """インスタンスがあれば、そのルートボリュームと Ebs.VolumeSize も、sshd を変える前に比べる。

    ルートボリュームの要素は、テンプレートの確かめで組み立てた中身から決めるので、AMI を
    もう一度は引かない。DeviceName を省いた要素を、AMI の RootDeviceName のものとする。
    """
    with ready.conf_path.open("a", encoding="utf-8") as file:
        _ = file.write("\n[[launch-template.BlockDeviceMappings]]\nEbs = { VolumeSize = 8 }\n")
    mappings = [{"DeviceName": ROOT_DEVICE, "Ebs": {"VolumeSize": 8}}]
    applied(aws_stubs)
    compared(aws_stubs, a_version(1, OLD, data=template_data(OLD, BlockDeviceMappings=mappings)))
    volume = "vol-0123456789abcdef0"
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(
            {
                **an_instance(INSTANCE, "running"),
                "RootDeviceName": "/dev/sda1",
                "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": volume}}],
            }
        ),
        SEARCH,
    )
    aws_stubs.ec2.add_response(
        "describe_volumes", {"Volumes": [{"VolumeId": volume, "Size": 16}]}, {"VolumeIds": [volume]}
    )

    with pytest.raises(
        TemtError,
        match=re.escape(
            f"the root volume of {INSTANCE} is 16 GiB, larger than "
            "launch-template.BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize 8; "
        ),
    ):
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()


def test_it_stops_with_two_instances(aws_stubs: Stubs, ready: Service) -> None:
    checked(aws_stubs, an_instance(INSTANCE, "running"), an_instance(OTHER, "stopped"))

    with pytest.raises(TemtError, match=f"2 instances for {NAME}; .* and set-port again"):
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()


# 手順 2。確認


def test_it_needs_a_terminal_without_yes(
    aws_stubs: Stubs,
    ready: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", Pipe())
    checked(aws_stubs, an_instance(INSTANCE, "running"))

    with pytest.raises(TemtError, match="stdin is not a terminal; pass --yes"):
        setport.run(ready, [2222], yes=False)

    aws_stubs.done()
    # 何をするかは先に見せる
    assert capsys.readouterr().out.startswith(f"set-port {NAME}:\n")


def test_it_does_nothing_when_the_answer_is_no(
    aws_stubs: Stubs, ready: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    checked(aws_stubs, an_instance(INSTANCE, "running"))
    before = ssh_config()

    with pytest.raises(TemtError, match="cancelled"):
        setport.run(ready, [2222], yes=False)

    aws_stubs.done()
    assert ssh_config() == before


# 手順 4 の失敗。元に戻さず、焼かず、Port を書き換えない


def test_a_failure_before_the_restart(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    before = ssh_config()
    checked(aws_stubs, an_instance(INSTANCE, "running"))
    brought_up(aws_stubs)
    ran(aws_stubs, [2222], "Failed", REJECTED, "bad configuration\n")

    with pytest.raises(TemtError) as raised:
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert str(raised.value) == (
        "set-port did not change the sshd ports (failed before the restart: sshd -t rejected "
        f"the ports 2222); sshd still listens on 22, so log in with 'ssh -p 22 {NAME}' and fix "
        "the files with Port lines; until then, do not reboot, stop or bake it; then run "
        f"set-port again; {LEFT}"
    )
    assert ssh_config() == before
    assert conf.load(ready).launch_template.get("ImageId") == OLD
    captured = capsys.readouterr()
    # スクリプトの標準出力と標準エラー出力を見せる
    assert told(captured.out)[-3:] == REJECTED.splitlines()
    assert captured.err == "temt: warning: bad configuration\n"


def test_a_failure_before_the_restart_names_the_files(aws_stubs: Stubs, ready: Service) -> None:
    """ほかのファイルの Port の行で止まったときは、そのファイルの名前が誤りに入る。"""
    last = (
        "failed before the restart: sshd would listen on 2200,2222; "
        f"Port is set in /etc/ssh/sshd_config {sshd.CONF_FILE}"
    )
    output = f"temt-sshd: before 2200\ntemt-sshd: listening 2200\ntemt-sshd: {last}\n"
    checked(aws_stubs, an_instance(INSTANCE, "running"))
    brought_up(aws_stubs)
    ran(aws_stubs, [2222], "Failed", output)

    with pytest.raises(TemtError) as raised:
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    text = str(raised.value)
    assert f"({last})" in text
    assert f"sshd still listens on 2200, so log in with 'ssh -p 2200 {NAME}'" in text
    assert block_port(ready) == "22"


def test_a_failure_after_the_restart(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    before = ssh_config()
    checked(aws_stubs, an_instance(INSTANCE, "running"))
    brought_up(aws_stubs)
    ran(aws_stubs, [2222], "Failed", NOT_LISTENING)

    with pytest.raises(TemtError) as raised:
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert str(raised.value) == (
        "set-port changed the sshd ports, but failed after the restart: sshd did not listen on "
        f"2222; sshd may listen on no port, so open a shell with 'aws ssm start-session --target "
        f"{INSTANCE}' and fix it, or run 'temt terminate --svc dev-main' and start again (what "
        f"was not baked is lost); {LEFT}"
    )
    assert ssh_config() == before
    assert conf.load(ready).launch_template.get("ImageId") == OLD
    # 何も待ち受けていない行も、そのまま見せる
    assert told(capsys.readouterr().out)[-3:] == NOT_LISTENING.splitlines()


@pytest.mark.parametrize(
    ("status", "output"),
    [("Failed", ""), ("TimedOut", "temt-sshd: before 22\n"), ("Success", "")],
    ids=["no output", "timed out", "no last line"],
)
def test_a_run_without_an_outcome(
    aws_stubs: Stubs, ready: Service, status: str, output: str
) -> None:
    """スクリプトの結果の行が無ければ、Run Command の状態を告げる。"""
    checked(aws_stubs, an_instance(INSTANCE, "running"))
    brought_up(aws_stubs)
    ran(aws_stubs, [2222], status, output)

    with pytest.raises(TemtError) as raised:
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert str(raised.value) == (
        f"the Run Command that sets the sshd ports ended with {status}; {LEFT}"
    )
    assert block_port(ready) == "22"


def test_it_gives_up_waiting_for_the_run(aws_stubs: Stubs, ready: Service, clock: Clock) -> None:
    """手元の締め切りを過ぎれば、Run Command の ID を告げて終わる。"""
    checked(aws_stubs, an_instance(INSTANCE, "running"))
    brought_up(aws_stubs)
    sent(aws_stubs, [2222])
    for _ in range(setport.DEADLINE // wait.INTERVAL):
        invocation(aws_stubs, "InProgress")

    with pytest.raises(TemtError) as raised:
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert str(raised.value) == (
        f"timed out after 7 minutes waiting for the sshd ports to be set on {INSTANCE}; "
        f"the Run Command {COMMAND} was sent; see Run Command in Systems Manager; {LEFT}"
    )
    assert clock.now == setport.DEADLINE
    assert block_port(ready) == "22"


@pytest.mark.parametrize("operation", ["send_command", "get_command_invocation"])
def test_another_error_from_run_command_is_not_swallowed(
    aws_stubs: Stubs, ready: Service, operation: str
) -> None:
    checked(aws_stubs, an_instance(INSTANCE, "running"))
    brought_up(aws_stubs)
    if operation == "get_command_invocation":
        sent(aws_stubs, [2222])
    aws_stubs.ssm.add_client_error(operation, service_error_code="AccessDeniedException")

    with pytest.raises(ClientError, match="AccessDeniedException"):
        setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert block_port(ready) == "22"


# 手順 7。受信規則の案内


def test_the_guidance_moves_the_rules_of_the_previous_port(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """前のポートの規則があれば、同じ送信元で新しいポートを足し、前のものを消す。"""
    add_rules(
        ready,
        "\n" + INGRESS + f'Port = 22\nCidr = "{HOME_CIDR}"\n'
        "\n" + INGRESS + f'Port = 22\nCidr = "{OFFICE_CIDR}"\n'
        "\n" + INGRESS + f'Port = 80\nCidr = "{OFFICE_CIDR}"\n',
    )
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == [
        f"  {NAME} sg add 2222 {HOME_CIDR}",
        f"  {NAME} sg add 2222 {OFFICE_CIDR}",
        f"  {NAME} sg remove 22/tcp {HOME_CIDR}",
        f"  {NAME} sg remove 22/tcp {OFFICE_CIDR}",
        f"  {NAME} apply",
    ]


def test_the_guidance_keeps_the_rules_of_a_port_that_stays(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """前のポートを残すときも、その規則と同じ送信元で新しいポートを足し、規則は消さない。"""
    add_rules(ready, "\n" + INGRESS + f'Port = 22\nCidr = "{HOME_CIDR}"\n')
    through(
        aws_stubs,
        [22, 2222],
        "temt-sshd: before 22\ntemt-sshd: listening 22,2222\ntemt-sshd: changed 22 -> 22,2222\n",
    )

    setport.run(ready, [22, 2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == [
        f"  {NAME} sg add 2222 {HOME_CIDR}",
        f"  {NAME} apply",
    ]


def test_the_guidance_without_a_rule_gives_an_example(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """前のポートの規則が無ければ、新しいポートの sg add の例だけを挙げる。"""
    add_rules(ready, "\n" + INGRESS + f'Port = 80\nCidr = "{HOME_CIDR}"\n')
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == [f"  {NAME} sg add 2222", f"  {NAME} apply"]


def test_the_guidance_with_a_covering_range(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """既にある規則の範囲が新しいポートを含めば、規則は要らないと告げる。"""
    add_rules(
        ready,
        "\n" + INGRESS + f'Port = 22\nCidr = "{HOME_CIDR}"\n'
        "\n" + INGRESS + f'Port = [2000, 3000]\nCidr = "{HOME_CIDR}"\n',
    )
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == ["the rules already allow 2222; no rule is needed"]


@pytest.mark.parametrize(
    ("protocol", "port"),
    [("udp", "2222"), ("tcp", "[2000, 2221]")],
    ids=["udp", "outside the range"],
)
def test_a_rule_that_does_not_allow_the_port_is_not_covering(
    aws_stubs: Stubs,
    ready: Service,
    capsys: pytest.CaptureFixture[str],
    protocol: str,
    port: str,
) -> None:
    add_rules(
        ready, "\n" + INGRESS + f'Protocol = "{protocol}"\nPort = {port}\nCidr = "{HOME_CIDR}"\n'
    )
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == [f"  {NAME} sg add 2222", f"  {NAME} apply"]


def test_the_guidance_names_the_defaults_file(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """前のポートの規則が共通ファイルにあれば、sg remove では消せないと添える。"""
    defaults_path().write_text(INGRESS + f'Port = 22\nCidr = "{HOME_CIDR}"\n', encoding="utf-8")
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == [
        f"  {NAME} sg add 2222",
        f"  {NAME} apply",
        "the rules for 22 in ~/.config/temt/defaults.toml apply to every service; "
        "sg remove cannot remove them, so edit that file",
    ]


def test_the_guidance_ignores_the_defaults_file_with_defaults_false(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """Defaults = false なら、共通ファイルの規則は効かないので数えない。"""
    defaults_path().write_text(
        INGRESS + f'Port = [2000, 3000]\nCidr = "{HOME_CIDR}"\n', encoding="utf-8"
    )
    ready.conf_path.write_text(
        "Defaults = false\n" + ready.conf_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == [f"  {NAME} sg add 2222", f"  {NAME} apply"]


def test_the_guidance_covered_by_the_defaults_file(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    defaults_path().write_text(
        INGRESS + f'Port = [2000, 3000]\nCidr = "{HOME_CIDR}"\n', encoding="utf-8"
    )
    through(aws_stubs, [2222], CHANGED)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == ["the rules already allow 2222; no rule is needed"]


@pytest.mark.parametrize(
    ("group", "found"),
    [(FOREIGN, (GROUP,)), (FOREIGN, ())],
    ids=["another group", "no temt group"],
)
def test_the_guidance_without_the_temt_security_group(
    aws_stubs: Stubs,
    ready: Service,
    capsys: pytest.CaptureFixture[str],
    group: str,
    found: tuple[str, ...],
) -> None:
    """テンプレートが temt のセキュリティグループを使っていなければ、AWS で直すよう告げる。"""
    add_rules(ready, "\n" + INGRESS + f'Port = 22\nCidr = "{HOME_CIDR}"\n')
    checked(aws_stubs, an_instance(INSTANCE, "running"))
    brought_up(aws_stubs)
    ran(aws_stubs, [2222], "Success", CHANGED)
    baked(aws_stubs)
    guided(aws_stubs, group=group, found=found)

    setport.run(ready, [2222], yes=True)

    aws_stubs.done()
    assert guidance(capsys.readouterr().out) == [
        "the launch template uses no security group of temt, so sg does not apply; "
        "change the rules of its security groups in AWS"
    ]


# cli から


def test_main_runs_set_port(
    aws_stubs: Stubs, ready: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    through(aws_stubs, [2222, 443], CHANGED.replace("-> 2222", "-> 2222,443"))

    assert cli.main(["temt", "ssh", "set-port", "--svc", "dev-main", "2222", "443", "--yes"]) == 0

    aws_stubs.done()
    assert block_port(ready) == "2222"
    assert capsys.readouterr().err == ""


def test_main_refuses_without_a_terminal(
    aws_stubs: Stubs,
    ready: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", Pipe())
    checked(aws_stubs, an_instance(INSTANCE, "running"))

    assert cli.main(["temt", "ssh", "set-port", "--svc", "dev-main", "2222"]) == 1

    aws_stubs.done()
    assert capsys.readouterr().err == (
        "temt: cannot ask for confirmation: stdin is not a terminal; pass --yes\n"
    )
    assert block_port(ready) == "22"
