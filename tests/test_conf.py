"""設定ファイルの読み書きのテスト。"""

import copy
import datetime
import errno
import os
import re
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from temt import conf, ltdata
from temt.conf import Conf, Ingress, Role, SecurityGroup
from temt.msg import TemtError, UsageError
from temt.names import LABEL_RULE, Service, defaults_path
from temt.table import tilde

TEMPLATE = conf.LAUNCH_TEMPLATE
# NetworkInterfaces の DeviceIndex が 0 の要素の見出し
PRIMARY = "[[launch-template.NetworkInterfaces]]\nDeviceIndex = 0\n"


def write(service: Service, text: str) -> None:
    """設定ファイルを用意する。"""
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(text, encoding="utf-8")


def rewrite(service: Service, key: str, value: Any) -> bool:
    """設定ファイルの位置 key に value を書く。書き換えたら True。

    role import と同じく、中身を write_document で書き換えてからファイルに書く。
    """
    path = service.conf_path
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    if not conf.write_document(document, key, value, path, "role import"):
        return False
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return True


def read(service: Service) -> str:
    """設定ファイルの中身。"""
    return service.conf_path.read_text(encoding="utf-8")


def test_reads_items(service: Service) -> None:
    write(
        service,
        '# comment\nAwsRegion = "us-east-1"\n\n[launch-template]\nInstanceType="t3.small"\n'
        "\n" + PRIMARY + 'SubnetId = "subnet-1"\n',
    )
    assert conf.load(service) == Conf(
        AwsRegion="us-east-1",
        launch_template={
            "InstanceType": "t3.small",
            "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": "subnet-1"}],
        },
    )


def test_reads_a_table_written_with_dotted_keys(service: Service) -> None:
    """TOML では launch-template.InstanceType = ... もテーブルの項目である。"""
    write(service, 'launch-template.InstanceType = "t3.small"\n')
    assert conf.load(service).launch_template == {"InstanceType": "t3.small"}


def test_unknown_key_reports_the_key(service: Service) -> None:
    write(service, 'AwsRegion = "us-east-1"\nFoo = "bar"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{tilde(service.conf_path)}: unknown key: Foo"


def test_unknown_key_with_a_control_character_is_shown_safely(service: Service) -> None:
    write(service, '"\\u001b[2J" = "x"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{tilde(service.conf_path)}: unknown key: ?[2J"


def test_unknown_key_in_a_table_reports_the_table_too(service: Service) -> None:
    write(service, '[launch-template]\nFoo = "bar"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{tilde(service.conf_path)}: unknown key: launch-template.Foo"


@pytest.mark.parametrize(
    "key", ["InstanceType", "SubnetId", "Login", "KeyName", "IdentityFile", "SshdPorts"]
)
def test_the_old_top_level_keys_are_unknown(service: Service, key: str) -> None:
    """古い形のファイル（テーブルに移った項目、廃した項目）は、未知のキーとして止まる。

    移行のコードは入れない。
    """
    write(service, f'{key} = "x"\n')
    with pytest.raises(TemtError, match=f"unknown key: {key}$"):
        conf.load(service)


def test_unknown_table_rejected(service: Service) -> None:
    write(service, '[key-pair]\nKeyName = "temt"\n')
    with pytest.raises(TemtError, match="unknown key: key-pair$"):
        conf.load(service)


def test_table_name_with_underscores_is_unknown(service: Service) -> None:
    """設定ファイルのテーブル名はハイフンで書く。フィールド名の形は受け付けない。"""
    write(service, '[launch_template]\nInstanceType = "t3.small"\n')
    with pytest.raises(TemtError, match="unknown key: launch_template$"):
        conf.load(service)


def test_value_that_is_not_a_string_rejected(service: Service) -> None:
    write(service, "AwsRegion = 1\n")
    with pytest.raises(TemtError, match="AwsRegion must be a string"):
        conf.load(service)


def test_value_in_a_table_that_is_not_a_string_rejected(service: Service) -> None:
    write(service, "[launch-template]\nInstanceType = 1\n")
    with pytest.raises(TemtError, match="launch-template.InstanceType must be a string"):
        conf.load(service)


def test_item_written_as_a_table_rejected(service: Service) -> None:
    write(service, '[AwsRegion]\nname = "us-east-1"\n')
    with pytest.raises(TemtError, match="AwsRegion must be a string"):
        conf.load(service)


def test_table_written_as_a_string_rejected(service: Service) -> None:
    write(service, 'launch-template = "t3.small"\n')
    with pytest.raises(TemtError, match="launch-template must be a table"):
        conf.load(service)


def test_broken_toml_rejected(service: Service) -> None:
    write(service, 'AwsProfile = "work"\nAwsRegion =\n')
    with pytest.raises(TemtError, match=re.escape(tilde(service.conf_path))):
        conf.load(service)


def test_duplicate_key_rejected(service: Service) -> None:
    write(service, 'AwsRegion = "us-east-1"\nAwsRegion = "us-west-2"\n')
    with pytest.raises(TemtError, match="already exists"):
        conf.load(service)


def test_duplicate_key_in_a_table_rejected(service: Service) -> None:
    """テーブルの中の重複キーは、TOML の構文の誤りとは別の例外で知らされる。"""
    write(service, "[launch-template]\nb = 1\nb = 2\n")
    with pytest.raises(TemtError, match="already exists"):
        conf.load(service)


@pytest.mark.parametrize("key", ["AwsProfile", "AwsRegion"])
def test_empty_value_means_unset(service: Service, key: str) -> None:
    write(service, f'{key} = ""\n')
    assert conf.load(service) == Conf()


def test_blank_values_in_the_launch_template_are_not_written(service: Service) -> None:
    """空文字列、空の配列、空のテーブルは書かれていないものとし、apply が選び直せるように
    する。中が空になったテーブルと配列の要素も落とす。
    """
    write(
        service,
        '[launch-template]\nImageId = ""\nSecurityGroupIds = []\nMetadataOptions = {}\n'
        'CreditSpecification.CpuCredits = ""\n'
        "\n" + PRIMARY + 'SubnetId = ""\n'
        "\n[[launch-template.BlockDeviceMappings]]\n",
    )
    assert conf.load(service).launch_template == {"NetworkInterfaces": [{"DeviceIndex": 0}]}


def test_empty_table_means_the_defaults(service: Service) -> None:
    write(service, "[launch-template]\n")
    assert conf.load(service) == Conf()


def test_load_without_a_file_tells_to_run_apply(service: Service) -> None:
    with pytest.raises(TemtError, match="temt apply --svc dev-main"):
        conf.load(service)


def test_load_shows_the_home_as_a_tilde(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ホームの下のパスは ~ で始めて出すので、ホームに表示できない文字があっても、誤りには
    入らない。"""
    home = home_dir / "\x1b[2J"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(TemtError) as caught:
        conf.load(Service("dev-main"))
    assert str(caught.value) == (
        "no config file: ~/.config/temt/dev-main.toml; run 'temt apply --svc dev-main' first"
    )


def test_load_without_a_file_may_be_the_defaults(service: Service) -> None:
    assert conf.load(service, missing_ok=True) == Conf()


def test_load_a_file_that_is_not_utf8(service: Service) -> None:
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_bytes(b"\xff\xfe")
    with pytest.raises(TemtError, match=r"dev-main\.toml: not valid UTF-8: invalid start byte"):
        conf.load(service)


def test_defaults() -> None:
    values = Conf()
    assert values.AwsProfile is None
    # aws.session が決める
    assert values.AwsRegion is conf.UNSET
    assert values.Defaults is True
    assert values.WaitForSsm is True
    # 書かれていない項目は、apply が値を決めて書くものも無い
    assert values.launch_template == {}
    assert conf.INSTANCE_TYPE == "t3.micro"
    assert values.role == Role()
    assert not values.role


def test_reads_the_launch_template_as_it_is_written(service: Service) -> None:
    """AWS の項目の名前と入れ子のまま持つ。変数は置き換えず、UserData も base64 のまま。

    テーブルはドット付きのキーやインラインテーブルで書いてもよい。小数の項目には整数も
    書ける。
    """
    write(
        service,
        '[launch-template]\nImageId = "temt-web-2610101230"\nKeyName = "mine"\n'
        'IamInstanceProfile = { Name = "${temt:name}" }\nUserData = "IyEvYmluL3NoCg=="\n'
        "EbsOptimized = true\nInstanceRequirements.MemoryGiBPerVCpu = { Min = 1, Max = 2.5 }\n"
        "\n" + PRIMARY + 'SubnetId = "subnet-1"\nGroups = ["${temt:name}", "sg-1"]\n'
        "\n[[launch-template.BlockDeviceMappings]]\nEbs.VolumeSize = 30\n"
        '\n[[launch-template.TagSpecifications]]\nResourceType = "instance"\n'
        'Tags = [{ Key = "Env", Value = "${temt:service}" }]\n'
        "\n[launch-template.InstanceMarketOptions.SpotOptions]\n"
        "ValidUntil = 2026-10-18T00:00:00Z\n",
    )
    data = conf.load(service).launch_template
    assert data == {
        "ImageId": "temt-web-2610101230",
        "KeyName": "mine",
        "IamInstanceProfile": {"Name": "${temt:name}"},
        "UserData": "IyEvYmluL3NoCg==",
        "EbsOptimized": True,
        "InstanceRequirements": {"MemoryGiBPerVCpu": {"Min": 1, "Max": 2.5}},
        "NetworkInterfaces": [
            {"DeviceIndex": 0, "SubnetId": "subnet-1", "Groups": ["${temt:name}", "sg-1"]}
        ],
        "BlockDeviceMappings": [{"Ebs": {"VolumeSize": 30}}],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": "${temt:service}"}]}
        ],
        "InstanceMarketOptions": {
            "SpotOptions": {"ValidUntil": datetime.datetime(2026, 10, 18, tzinfo=datetime.UTC)}
        },
    }


@pytest.mark.parametrize("key", ["SubnetId", "VolumeSize"])
def test_the_old_shorthand_keys_are_unknown(service: Service, key: str) -> None:
    """[launch-template] の最上位の SubnetId と VolumeSize は、起動テンプレートに無い項目である。

    NetworkInterfaces の要素の SubnetId と、BlockDeviceMappings の要素の Ebs.VolumeSize に
    書く。
    """
    write(service, f'[launch-template]\n{key} = "x"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{tilde(service.conf_path)}: unknown key: launch-template.{key}"


@pytest.mark.parametrize(
    ("written", "message"),
    [
        pytest.param(
            PRIMARY + 'SubnetIds = "subnet-1"\n',
            "unknown key: launch-template.NetworkInterfaces[0].SubnetIds",
            id="unknown key in an element",
        ),
        pytest.param(
            PRIMARY + "\n[[launch-template.NetworkInterfaces]]\nDeviceIndex = 1\nSubnetId = 1\n",
            "launch-template.NetworkInterfaces[1].SubnetId must be a string",
            id="wrong type in the second element",
        ),
        pytest.param(
            '[[launch-template.BlockDeviceMappings]]\nEbs.VolumeSize = "30"\n',
            "launch-template.BlockDeviceMappings[0].Ebs.VolumeSize must be an integer",
            id="volume size as a string",
        ),
        pytest.param(
            '[launch-template]\nMetadataOptions = "required"\n',
            "launch-template.MetadataOptions must be a table",
            id="table as a string",
        ),
        pytest.param(
            '[launch-template]\nKeyName = "${temt:app}"\n',
            "launch-template.KeyName uses an unknown variable: ${temt:app}",
            id="unknown variable",
        ),
        pytest.param(
            '[launch-template]\nMetadataOptions.State = "applied"\n',
            "unknown key: launch-template.MetadataOptions.State",
            id="read-only item",
        ),
    ],
)
def test_an_error_in_the_launch_template_names_the_file_and_the_position(
    service: Service, written: str, message: str
) -> None:
    """配列の要素は、ファイルに書いた順の番号で指す。"""
    write(service, written)
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{tilde(service.conf_path)}: {message}"


@pytest.mark.parametrize(
    ("written", "message"),
    [
        pytest.param(
            PRIMARY + "\n" + PRIMARY,
            "launch-template.NetworkInterfaces has two elements with DeviceIndex = 0",
            id="DeviceIndex",
        ),
        pytest.param(
            '[[launch-template.TagSpecifications]]\nResourceType = "instance"\n'
            '\n[[launch-template.TagSpecifications]]\nResourceType = "instance"\n',
            'launch-template.TagSpecifications has two elements with ResourceType = "instance"',
            id="ResourceType",
        ),
        pytest.param(
            '[[launch-template.TagSpecifications]]\nResourceType = "instance"\n'
            'Tags = [{ Key = "Env", Value = "a" }, { Key = "Env", Value = "b" }]\n',
            'launch-template.TagSpecifications[0].Tags has two elements with Key = "Env"',
            id="tag Key",
        ),
        pytest.param(
            "[[launch-template.BlockDeviceMappings]]\nEbs.VolumeSize = 30\n"
            "\n[[launch-template.BlockDeviceMappings]]\nEbs.VolumeSize = 40\n",
            "launch-template.BlockDeviceMappings has two or more elements without DeviceName",
            id="two without DeviceName",
        ),
    ],
)
def test_a_repeated_matching_key_is_an_error(service: Service, written: str, message: str) -> None:
    """要素はキーで対応づけるので、同じキーの要素を 2 つ書けない。"""
    write(service, written)
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{tilde(service.conf_path)}: {message}"


def test_one_block_device_may_omit_the_device_name(service: Service) -> None:
    """DeviceName を省いた要素は、apply が AMI のルートボリュームのデバイス名で埋める。"""
    write(
        service,
        "[[launch-template.BlockDeviceMappings]]\nEbs.VolumeSize = 30\n"
        '\n[[launch-template.BlockDeviceMappings]]\nDeviceName = "/dev/sdb"\n'
        "Ebs.VolumeSize = 100\n",
    )
    assert conf.load(service).launch_template["BlockDeviceMappings"] == [
        {"Ebs": {"VolumeSize": 30}},
        {"DeviceName": "/dev/sdb", "Ebs": {"VolumeSize": 100}},
    ]


def test_without_botocore_the_launch_template_is_only_cleaned(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """botocore が読み込めなければ、テーブルであることだけを確かめ、空の値を落とす。

    AWS を呼ばないコマンドは、botocore が無くても動く。
    """

    def missing() -> None:
        raise ImportError("botocore")

    monkeypatch.setattr(ltdata, "shape", missing)
    write(service, '[launch-template]\nImageId = ""\nFoo = "bar"\n')
    assert conf.load(service).launch_template == {"Foo": "bar"}
    write(service, 'launch-template = "x"\n')
    with pytest.raises(TemtError, match="launch-template must be a table$"):
        conf.load(service)


def test_each_conf_has_its_own_table() -> None:
    """既定のテーブルは Conf ごとに別のオブジェクトで、片方を変えても他方は変わらない。"""
    one, other = Conf(), Conf()
    one.launch_template["InstanceType"] = "t3.small"
    one.role.ManagedPolicyArns.append("arn:aws:iam::aws:policy/ReadOnlyAccess")
    assert other.launch_template == {}
    assert other.role.ManagedPolicyArns == []


def test_rewrite_adds_the_key_when_it_is_absent(service: Service) -> None:
    write(service, '# mine\nAwsProfile = "work"\n')
    assert rewrite(service, "AwsRegion", "us-east-1") is True
    assert read(service) == '# mine\nAwsProfile = "work"\nAwsRegion = "us-east-1"\n'


def test_rewrite_adds_the_table_when_it_is_absent(service: Service) -> None:
    write(service, 'AwsProfile = "work"\n')
    assert rewrite(service, f"{TEMPLATE}.InstanceType", "t3.micro") is True
    assert read(service) == 'AwsProfile = "work"\n\n[launch-template]\nInstanceType = "t3.micro"\n'


def test_rewrite_adds_the_key_to_the_existing_table(service: Service) -> None:
    write(service, '[launch-template]\nInstanceType = "t3.micro"  # c\n')
    assert rewrite(service, f"{TEMPLATE}.ImageId", "ami-9") is True
    assert read(service) == '[launch-template]\nInstanceType = "t3.micro"  # c\nImageId = "ami-9"\n'


def test_rewrite_adds_the_key_before_the_comments_of_the_next_table(service: Service) -> None:
    """後ろのコメントは次の見出しのものなので、その前に残す。例の見出しを戻しても、
    足した項目はそのテーブルのものにならない。"""
    write(
        service,
        '[launch-template]\nInstanceType = "t3.micro"\n\n# about the rules\n'
        "# [[security-group.ingress]]\n# Port = 22\n[x]\nA = 1\n",
    )
    assert rewrite(service, f"{TEMPLATE}.ImageId", "ami-9") is True
    text = read(service)
    assert text == (
        '[launch-template]\nInstanceType = "t3.micro"\nImageId = "ami-9"\n\n'
        "# about the rules\n# [[security-group.ingress]]\n# Port = 22\n[x]\nA = 1\n"
    )
    uncommented = text.replace("# [[", "[[").replace("# Port", "Port")
    assert tomlkit.parse(uncommented)["launch-template"]["ImageId"] == "ami-9"


def test_rewrite_adds_the_key_to_a_table_that_has_only_comments(service: Service) -> None:
    write(service, '[launch-template]\n# InstanceType = "t3.small"\n')
    rewrite(service, f"{TEMPLATE}.ImageId", "ami-9")
    assert read(service) == '[launch-template]\nImageId = "ami-9"\n# InstanceType = "t3.small"\n'


def test_rewrite_adds_the_key_next_to_a_dotted_key(service: Service) -> None:
    """ドット付きキーで書かれたテーブルには、同じ形で足す。読み直しても同じ内容になる。"""
    write(service, 'launch-template.InstanceType = "t3.small"\n')
    assert rewrite(service, f"{TEMPLATE}.ImageId", "ami-9") is True
    assert read(service) == (
        'launch-template.InstanceType = "t3.small"\nlaunch-template.ImageId = "ami-9"\n'
    )
    assert conf.load(service).launch_template == {"InstanceType": "t3.small", "ImageId": "ami-9"}


def test_rewrite_puts_a_top_level_key_before_the_tables(service: Service) -> None:
    """テーブルの後に足すと、そのテーブルの項目になってしまう。"""
    write(service, 'AwsProfile = "work"\n\n[launch-template]\nInstanceType = "t3.micro"\n')
    rewrite(service, "AwsRegion", "us-east-1")
    assert read(service) == (
        'AwsProfile = "work"\nAwsRegion = "us-east-1"\n'
        '\n[launch-template]\nInstanceType = "t3.micro"\n'
    )
    assert conf.load(service).AwsRegion == "us-east-1"


def test_rewrite_adds_a_newline_when_the_file_lacks_one(service: Service) -> None:
    write(service, 'AwsProfile = "work"')
    rewrite(service, "AwsRegion", "us-east-1")
    assert read(service) == 'AwsProfile = "work"\nAwsRegion = "us-east-1"\n'


def test_rewrite_replaces_the_value(service: Service) -> None:
    write(service, '# mine\nAwsProfile = "a"\nAwsRegion = "us-east-1"\n')
    assert rewrite(service, "AwsProfile", "b") is True
    assert read(service) == '# mine\nAwsProfile = "b"\nAwsRegion = "us-east-1"\n'


def test_rewrite_keeps_the_order_and_the_comments(service: Service) -> None:
    write(service, '# head\nAwsRegion = "us-east-1"  # trailing\n# middle\nAwsProfile = "work"\n')
    rewrite(service, "AwsProfile", "other")
    assert read(service) == (
        '# head\nAwsRegion = "us-east-1"  # trailing\n# middle\nAwsProfile = "other"\n'
    )


def test_rewrite_keeps_the_file_when_the_value_is_the_same(service: Service) -> None:
    """利用者が書いた書式をそのまま残す。"""
    write(service, '# mine\nAwsProfile   =   "work"\n')
    assert rewrite(service, "AwsProfile", "work") is False
    assert read(service) == '# mine\nAwsProfile   =   "work"\n'


def test_rewrite_keeps_the_file_when_the_table_value_is_the_same(service: Service) -> None:
    write(service, '[launch-template]\nInstanceType   =   "t3.micro"\n')
    assert rewrite(service, f"{TEMPLATE}.InstanceType", "t3.micro") is False
    assert read(service) == '[launch-template]\nInstanceType   =   "t3.micro"\n'


def test_rewrite_changes_nothing_the_second_time(service: Service) -> None:
    write(service, "")
    rewrite(service, f"{TEMPLATE}.InstanceType", "t3.micro")
    assert rewrite(service, f"{TEMPLATE}.InstanceType", "t3.micro") is False


def test_rewrite_replaces_an_empty_value(service: Service) -> None:
    write(service, 'AwsProfile = ""\n')
    assert rewrite(service, "AwsProfile", "work") is True
    assert conf.load(service).AwsProfile == "work"


def test_rewrite_writes_a_value_that_needs_quoting(service: Service) -> None:
    write(service, "")
    rewrite(service, "AwsProfile", "a=b # c")
    assert conf.load(service).AwsProfile == "a=b # c"


# --- [launch-template] の中の位置 ---


def test_key_positions_name_elements_by_their_matching_key() -> None:
    assert conf.launch_template_key("ImageId") == "launch-template.ImageId"
    assert conf.primary_key("SubnetId") == (
        "launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId"
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param("AwsRegion", [("AwsRegion", None)], id="top level"),
        pytest.param(
            "launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId",
            [
                ("launch-template", None),
                ("NetworkInterfaces", ("DeviceIndex", 0)),
                ("SubnetId", None),
            ],
            id="digits are an integer",
        ),
        pytest.param(
            "launch-template.BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize",
            [
                ("launch-template", None),
                ("BlockDeviceMappings", ("DeviceName", "/dev/sda1")),
                ("Ebs", None),
                ("VolumeSize", None),
            ],
            id="string",
        ),
        pytest.param(
            "TagSpecifications[ResourceType=instance].Tags[Key=a.b=c].Value",
            [
                ("TagSpecifications", ("ResourceType", "instance")),
                ("Tags", ("Key", "a.b=c")),
                ("Value", None),
            ],
            id="dot and equals sign in a value",
        ),
        pytest.param("Tags[Key=٣]", [("Tags", ("Key", "٣"))], id="non-ASCII digit"),
        pytest.param("Tags[Key=]", [("Tags", ("Key", ""))], id="empty value"),
    ],
)
def test_segments(key: str, expected: list[conf.SEGMENT]) -> None:
    """[] の中の . では分けない。値は ASCII の数字だけなら整数とする。"""
    assert conf.segments(key) == expected


@pytest.mark.parametrize("key", ["a..b", "a[b]", "[Key=x]", "a.", "a[Key=x]b"])
def test_segments_of_what_is_not_a_position(key: str) -> None:
    with pytest.raises(ValueError, match="not a key position"):
        conf.segments(key)


# 配列の要素を 2 つ持つ設定ファイル。DeviceIndex が 0 の要素は後ろにある
TWO_INTERFACES = (
    '[launch-template]\nImageId = "a"\n'
    "\n[[launch-template.NetworkInterfaces]]\nDeviceIndex = 1\n"
    'SubnetId = "subnet-2"  # second\n'
    "\n" + PRIMARY + 'SubnetId = "subnet-0"  # first\n'
)


def test_written_finds_the_element_by_its_key(service: Service) -> None:
    write(service, TWO_INTERFACES)
    assert conf.written(service, conf.primary_key("SubnetId")) == "subnet-0"
    other = conf.launch_template_key("NetworkInterfaces[DeviceIndex=1].SubnetId")
    assert conf.written(service, other) == "subnet-2"
    missing = conf.launch_template_key("NetworkInterfaces[DeviceIndex=2].SubnetId")
    assert conf.written(service, missing) is None
    assert conf.written(service, conf.launch_template_key("ImageId.Name")) is None


def test_written_reads_an_inline_array_too(service: Service) -> None:
    write(service, '[launch-template]\nNetworkInterfaces = [{ DeviceIndex = 0, SubnetId = "s" }]\n')
    assert conf.written(service, conf.primary_key("SubnetId")) == "s"


def test_rewrite_adds_a_missing_table_as_dotted_keys(service: Service) -> None:
    """テーブルの中の無いテーブルは、見出しを足さず、ドット付きキーで最後の項目の次に書く。

    雛形の IamInstanceProfile.Name = "..." と同じ形である。後ろのテーブルの配列やテーブル、
    コメントはそのまま残す。
    """
    head = '[launch-template]\nImageId = "a"\n'
    rest = (
        "\n" + PRIMARY + 'SubnetId = "subnet-0"\n'
        '\n[launch-template.MetadataOptions]\nHttpTokens = "required"\n'
        '\n# about the rules\n[[security-group.ingress]]\nPort = 22\nCidr = "192.0.2.1/32"\n'
    )
    write(service, head + rest)
    key = conf.launch_template_key("CreditSpecification.CpuCredits")
    rewrite(service, key, "unlimited")
    assert read(service) == head + 'CreditSpecification.CpuCredits = "unlimited"\n' + rest
    assert conf.load(service).launch_template["CreditSpecification"] == {"CpuCredits": "unlimited"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "",
            '[launch-template]\nMetadataOptions.HttpTokens = "required"\n',
            id="empty file",
        ),
        pytest.param(
            "[launch-template]\n",
            '[launch-template]\nMetadataOptions.HttpTokens = "required"\n',
            id="empty table",
        ),
        pytest.param(
            "[launch-template]\n\n# about the rules\n[x]\nA = 1\n",
            '[launch-template]\nMetadataOptions.HttpTokens = "required"\n'
            "\n# about the rules\n[x]\nA = 1\n",
            id="empty table before comments",
        ),
    ],
)
def test_rewrite_adds_dotted_keys_before_the_comments_of_the_next_table(
    service: Service, text: str, expected: str
) -> None:
    """後ろのコメントは、中身が無くても次の見出しの説明として、足した行の後ろに残す。"""
    write(service, text)
    rewrite(service, conf.launch_template_key("MetadataOptions.HttpTokens"), "required")
    assert read(service) == expected


def test_rewrite_writes_into_a_table_of_dotted_keys(service: Service) -> None:
    """ドット付きのキーで書いたテーブルには、同じ形で足す。"""
    text = '[launch-template]\nImageId = "a"\nMetadataOptions.HttpTokens = "optional"\n'
    write(service, text)
    key = conf.launch_template_key("MetadataOptions.InstanceMetadataTags")
    assert rewrite(service, key, "enabled") is True
    assert read(service) == text + 'MetadataOptions.InstanceMetadataTags = "enabled"\n'
    assert conf.load(service).launch_template["MetadataOptions"] == {
        "HttpTokens": "optional",
        "InstanceMetadataTags": "enabled",
    }


def test_rewrite_writes_a_table_as_an_inline_table(service: Service) -> None:
    """中かっこの内側に空白を置き、行を改行で終える。"""
    write(service, '[launch-template]\nImageId = "a"\n')
    key = conf.launch_template_key(ltdata.PROFILE)
    assert rewrite(service, key, {"Name": "${temt:name}"}) is True
    assert read(service) == (
        '[launch-template]\nImageId = "a"\nIamInstanceProfile = { Name = "${temt:name}" }\n'
    )


@pytest.mark.parametrize(
    "after",
    [
        pytest.param("[[launch-template.NetworkInterfaces]]\nDeviceIndex = 0\n", id="table"),
        pytest.param("# about x\n[x]\nA = 1\n", id="comment"),
    ],
)
def test_an_inline_table_added_before_what_follows_ends_its_line(
    service: Service, after: str
) -> None:
    """項目の直後に空行を置かずに見出しやコメントが続いても、足した行は改行で終える。

    つながると、見出しなら設定ファイルが TOML として読めなくなり、コメントなら足した行の
    コメントになってしまう。
    """
    write(service, '[launch-template]\nImageId = "a"\n' + after)
    key = conf.launch_template_key(ltdata.PROFILE)
    rewrite(service, key, {"Name": "${temt:name}"})
    assert read(service) == (
        '[launch-template]\nImageId = "a"\nIamInstanceProfile = { Name = "${temt:name}" }\n' + after
    )


@pytest.mark.xfail(
    strict=True,
    reason="テーブルかドット付きキーで書かれた項目をインラインテーブルで置き換えると、その後ろの"
    "コメントと空行が消えるか、足した行がコメントの後ろに入る",
)
@pytest.mark.parametrize(
    "written",
    [
        pytest.param('\n[launch-template.IamInstanceProfile]\nArn = "arn:x/web"\n', id="table"),
        pytest.param('IamInstanceProfile.Arn = "arn:x/web"\n', id="dotted key"),
    ],
)
def test_an_item_replaced_by_an_inline_table_keeps_the_comments_after_it(
    service: Service, written: str
) -> None:
    """後ろのコメントは次の見出しの説明なので、置き換えた行の後ろ、次の見出しの前に残す。

    いまはどのコマンドも通らない。role import は、テーブルかドット付きキーで書かれた
    IamInstanceProfile を、置き換えずにその場で書き換える。
    """
    rules = '\n# about the rules\n[[security-group.ingress]]\nPort = 22\nCidr = "192.0.2.1/32"\n'
    write(service, '[launch-template]\nImageId = "a"\n' + written + rules)
    key = conf.launch_template_key(ltdata.PROFILE)
    rewrite(service, key, {"Name": "${temt:name}"})
    assert read(service) == (
        '[launch-template]\nImageId = "a"\nIamInstanceProfile = { Name = "${temt:name}" }\n' + rules
    )


def test_rewrite_writes_an_inline_table_with_spaces_inside(service: Service) -> None:
    write(service, '[launch-template]\nImageId = "a"\n')
    key = conf.launch_template_key(ltdata.PROFILE)
    assert rewrite(service, key, {"Name": "${temt:name}"}) is True
    assert 'IamInstanceProfile = { Name = "${temt:name}" }' in read(service)
    assert conf.load(service).launch_template[ltdata.PROFILE] == {"Name": "${temt:name}"}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            '[launch-template]\nIamInstanceProfile = {Name = "${temt:name}"}\n',
            id="inline table without spaces",
        ),
        pytest.param(
            '[launch-template]\nIamInstanceProfile.Name = "${temt:name}"\n', id="dotted key"
        ),
        pytest.param(
            '[launch-template]\n\n[launch-template.IamInstanceProfile]\nName = "${temt:name}"\n',
            id="table",
        ),
    ],
)
def test_rewrite_keeps_a_table_written_in_another_form(service: Service, text: str) -> None:
    """書式が違っても、インラインテーブルでなく書かれていても、中身が同じなら触らない。"""
    write(service, text)
    key = conf.launch_template_key(ltdata.PROFILE)
    assert rewrite(service, key, {"Name": "${temt:name}"}) is False
    assert read(service) == text


@pytest.mark.parametrize(
    ("text", "key", "value"),
    [
        pytest.param(
            '[launch-template]\nMetadataOptions = { HttpTokens = "required" }\n',
            "launch-template.MetadataOptions.HttpTokens",
            "required",
            id="inline table",
        ),
        pytest.param(
            'launch-template = { ImageId = "a" }\n',
            "launch-template.ImageId",
            "a",
            id="inline launch-template",
        ),
    ],
)
def test_the_same_value_in_an_inline_table_is_left_alone(
    service: Service, text: str, key: str, value: Any
) -> None:
    """インラインテーブルの中でも、同じ値が既に書かれていれば止めずに触らない。

    手で書くよう告げたとおりに書いた後のコマンドが、また止まらないようにする。
    """
    write(service, text)
    assert rewrite(service, key, value) is False
    assert read(service) == text


@pytest.mark.parametrize(
    ("text", "key", "value"),
    [
        pytest.param(
            '[launch-template]\nMetadataOptions = { HttpTokens = "optional" }\n',
            "launch-template.MetadataOptions.InstanceMetadataTags",
            "enabled",
            id="inline table",
        ),
        pytest.param(
            'launch-template = { ImageId = "a" }\n',
            "launch-template.InstanceType",
            "t3.micro",
            id="inline launch-template",
        ),
        pytest.param(
            "[launch-template]\nCreditSpecification = {}\n",
            "launch-template.CreditSpecification.CpuCredits",
            "unlimited",
            id="empty inline table",
        ),
    ],
)
def test_rewrite_does_not_write_into_an_inline_table(
    service: Service, text: str, key: str, value: Any
) -> None:
    """tomlkit では形を保って書き足せないので、書かずに、手で書くよう告げて止める。"""
    write(service, text)
    shown = conf.toml_value(value)
    message = (
        f"cannot write {key} in {tilde(service.conf_path)}, as it would go into an inline "
        f"table or array; write {key} = {shown} there by hand and run role import again"
    )
    with pytest.raises(TemtError) as caught:
        _ = rewrite(service, key, value)
    assert str(caught.value) == message
    assert read(service) == text


def test_write_document_writes_into_the_document_only(service: Service) -> None:
    """role import が、設定ファイルを書き換える前に中身を作るのに使う。"""
    path = service.conf_path
    document = tomlkit.parse('[launch-template]\nImageId = "a"\n')
    key = conf.launch_template_key(ltdata.PROFILE)
    value = {"Name": "${temt:name}"}
    assert conf.write_document(document, key, value, path, "role import") is True
    assert document.unwrap() == {
        "launch-template": {"ImageId": "a", "IamInstanceProfile": {"Name": "${temt:name}"}}
    }
    assert conf.write_document(document, key, value, path, "role import") is False
    assert not path.exists()


def test_write_document_names_the_file_and_the_command(service: Service) -> None:
    """書けなければ、設定ファイルと、手で書いてからやり直すコマンドを告げる。"""
    inline = tomlkit.parse('launch-template = { ImageId = "a" }\n')
    key = conf.launch_template_key(ltdata.PROFILE)
    with pytest.raises(TemtError) as caught:
        conf.write_document(inline, key, {"Name": "${temt:name}"}, service.conf_path, "role import")
    assert str(caught.value) == (
        f"cannot write {key} in {tilde(service.conf_path)}, as it would go into an inline "
        f'table or array; write {key} = {{ Name = "${{temt:name}}" }} there by hand and '
        "run role import again"
    )


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("t3.micro", '"t3.micro"'),
        (True, "true"),
        (30, "30"),
        (["sg-1", "sg-2"], '["sg-1", "sg-2"]'),
        ({"Name": "${temt:name}"}, '{ Name = "${temt:name}" }'),
        ({"Name": "a", "Arn": "b"}, '{ Name = "a", Arn = "b" }'),
        ({}, "{}"),
    ],
)
def test_toml_value(value: Any, shown: str) -> None:
    """テーブルは、中かっこの内側に空白を置くインラインテーブルで書く。"""
    assert conf.toml_value(value) == shown


INGRESS = "[[security-group.ingress]]\n"


def test_reads_the_ingress_rules(service: Service) -> None:
    """テーブルの配列は、書いた数だけ規則になる。整数のポートは 1 つのポートである。"""
    write(
        service,
        INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\nDescription = "home"\n'
        "\n" + INGRESS + 'Port = [80, 443]\nCidr = "2001:db8::/64"\n',
    )
    assert conf.load(service).security_group == SecurityGroup(
        ingress=[
            Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home"),
            Ingress(Port=(80, 443), Cidr="2001:db8::/64"),
        ]
    )


def test_no_ingress_means_no_rule(service: Service) -> None:
    """既定は空である。テーブルだけ書いても変わらない。"""
    assert conf.load(service, missing_ok=True).security_group.ingress == []
    write(service, "[security-group]\n")
    assert conf.load(service).security_group.ingress == []


@pytest.mark.parametrize(
    ("written", "read_as"),
    [
        pytest.param("2001:0DB8:0000::10/128", "2001:db8::10/128", id="IPv6"),
        pytest.param("203.0.113.10", "203.0.113.10/32", id="one address"),
        pytest.param("0.0.0.0/0", "0.0.0.0/0", id="everywhere"),
    ],
)
def test_the_cidr_is_read_in_the_form_aws_keeps(
    service: Service, written: str, read_as: str
) -> None:
    """AWS は CIDR を正規化して持つので、読むときに同じ形に揃える。"""
    write(service, INGRESS + f'Port = 22\nCidr = "{written}"\n')
    assert conf.load(service).security_group.ingress[0].Cidr == read_as


@pytest.mark.parametrize(
    ("written", "message"),
    [
        pytest.param('"22"', "must be an integer or an array of two integers", id="string"),
        pytest.param("true", "must be an integer or an array of two integers", id="boolean"),
        pytest.param("22.5", "must be an integer or an array of two integers", id="fraction"),
        pytest.param("[22]", "must be an integer or an array of two integers", id="one element"),
        pytest.param("[]", "is missing", id="empty"),
        pytest.param(
            "[22, 23, 24]", "must be an integer or an array of two integers", id="three elements"
        ),
        pytest.param("[23, 22]", "must be a port range within 0-65535", id="upside down"),
        pytest.param("-1", "must be a port range within 0-65535", id="every protocol"),
        pytest.param("65536", "must be a port range within 0-65535", id="too large"),
    ],
)
def test_a_port_that_is_not_a_range(service: Service, written: str, message: str) -> None:
    write(service, INGRESS + f'Port = {written}\nCidr = "203.0.113.10/32"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: security-group.ingress[0].Port {message}"
    )


@pytest.mark.parametrize(
    "written",
    [
        pytest.param("203.0.113.300/32", id="not an address"),
        pytest.param("203.0.113.10/24", id="host bits set"),
        pytest.param("home", id="a name"),
        pytest.param("fe80::1%eth0/128", id="zone id"),
    ],
)
def test_a_cidr_that_is_not_one(service: Service, written: str) -> None:
    """ゾーン ID の付いたアドレスは ip_network が通すが、AWS は受けない。"""
    write(service, INGRESS + f'Port = 22\nCidr = "{written}"\n')
    with pytest.raises(TemtError, match=r"security-group\.ingress\[0\]\.Cidr must "):
        conf.load(service)


@pytest.mark.parametrize(
    ("written", "message"),
    [
        pytest.param("fe80::1%\\u001b[2J/128", "must be an IPv4 or IPv6 CIDR: fe80::1%?[2J/128"),
        pytest.param(
            "fe80::1%\\u001b[2J/64",
            "must be an IPv4 or IPv6 CIDR: fe80::1%?[2J/64",
            id="host bits set",
        ),
    ],
)
def test_a_cidr_with_a_control_character_is_shown_safely(
    service: Service, written: str, message: str
) -> None:
    """ip_network は、ゾーン ID の中の制御文字をそのまま通すので、先に断る。"""
    write(service, INGRESS + f'Port = 22\nCidr = "{written}"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert (
        str(caught.value) == f"{tilde(service.conf_path)}: security-group.ingress[0].Cidr {message}"
    )


@pytest.mark.parametrize("missing", ["Port", "Cidr"])
def test_a_rule_without_a_port_or_a_cidr(service: Service, missing: str) -> None:
    """Description だけが省ける。"""
    written = {"Port": "22", "Cidr": '"203.0.113.10/32"'}
    del written[missing]
    write(service, INGRESS + "".join(f"{key} = {value}\n" for key, value in written.items()))
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: security-group.ingress[0].{missing} is missing"
    )


@pytest.mark.parametrize(
    ("written", "message"),
    [
        pytest.param('"\u81ea\u5b85"', f"must use only {conf.DESCRIPTION_CHARS}", id="not ASCII"),
        pytest.param('"a<b>"', f"must use only {conf.DESCRIPTION_CHARS}", id="angle brackets"),
        pytest.param('"' + "a" * 256 + '"', "must be at most 255 characters", id="too long"),
    ],
)
def test_a_description_that_aws_will_not_take(service: Service, written: str, message: str) -> None:
    """AWS が受けない説明は、規則を消す前に、設定ファイルを指して知らせる。"""
    write(service, INGRESS + f'Port = 22\nCidr = "203.0.113.10/32"\nDescription = {written}\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: security-group.ingress[0].Description {message}"
    )


def test_a_description_of_every_allowed_character(service: Service) -> None:
    """AWS が許す文字は通す。"""
    allowed = "az AZ 09 ._-:/()#,@[]+=&;{}!$*"
    write(service, INGRESS + f'Port = 22\nCidr = "203.0.113.10/32"\nDescription = "{allowed}"\n')
    assert conf.load(service).security_group.ingress[0].Description == allowed


def test_two_rules_for_the_same_port_and_cidr(service: Service) -> None:
    """AWS は 1 つの規則に説明を 1 つしか持てない。書き分けようとしたら知らせる。"""
    write(
        service,
        INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\nDescription = "home"\n'
        "\n" + INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\nDescription = "office"\n',
    )
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: security-group.ingress[1] repeats the protocol, port and "
        "CIDR of ingress[0]"
    )


def test_the_same_rule_written_in_two_forms_is_one_rule(service: Service) -> None:
    """揃えた後の姿で見分けるので、書き方が違っても同じ規則である。"""
    write(
        service,
        INGRESS + 'Port = 22\nCidr = "203.0.113.10"\n'
        "\n" + INGRESS + 'Port = [22, 22]\nCidr = "203.0.113.10/32"\n',
    )
    with pytest.raises(TemtError, match=r"ingress\[1\] repeats the protocol, port and CIDR"):
        conf.load(service)


def test_the_same_port_for_two_cidrs_is_fine(service: Service) -> None:
    write(
        service,
        INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\n'
        "\n" + INGRESS + 'Port = 22\nCidr = "198.51.100.7/32"\n',
    )
    assert len(conf.load(service).security_group.ingress) == 2


def test_the_same_port_for_two_protocols_is_fine(service: Service) -> None:
    write(
        service,
        INGRESS + 'Port = 53\nCidr = "203.0.113.10/32"\n'
        "\n" + INGRESS + 'Protocol = "udp"\nPort = 53\nCidr = "203.0.113.10/32"\n',
    )
    assert [one.Protocol for one in conf.load(service).security_group.ingress] == ["tcp", "udp"]


def test_an_icmp_rule_has_no_port(service: Service) -> None:
    write(service, INGRESS + 'Protocol = "icmp"\nCidr = "203.0.113.10/32"\n')
    assert conf.load(service).security_group.ingress == [
        Ingress(Protocol="icmp", Port=None, Cidr="203.0.113.10/32")
    ]


def test_an_icmp_rule_with_a_port(service: Service) -> None:
    write(service, INGRESS + 'Protocol = "icmp"\nPort = 8\nCidr = "203.0.113.10/32"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: security-group.ingress[0].Port must not be given with "
        "Protocol icmp"
    )


def test_a_udp_rule_without_a_port(service: Service) -> None:
    write(service, INGRESS + 'Protocol = "udp"\nCidr = "203.0.113.10/32"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert (
        str(caught.value)
        == f"{tilde(service.conf_path)}: security-group.ingress[0].Port is missing"
    )


def test_an_unknown_protocol(service: Service) -> None:
    write(service, INGRESS + 'Protocol = "TCP"\nPort = 22\nCidr = "203.0.113.10/32"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: security-group.ingress[0].Protocol must be one of "
        "tcp, udp, icmp"
    )


def test_an_unknown_key_in_a_rule(service: Service) -> None:
    write(service, INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\nType = 8\n')
    with pytest.raises(TemtError, match=r"unknown key: security-group\.ingress\[0\]\.Type$"):
        conf.load(service)


def test_ingress_that_is_not_an_array_of_tables(service: Service) -> None:
    write(service, '[security-group]\ningress = "22"\n')
    with pytest.raises(TemtError, match="security-group.ingress must be an array of tables$"):
        conf.load(service)


def test_an_ingress_element_that_is_not_a_table(service: Service) -> None:
    write(service, "[security-group]\ningress = [22]\n")
    with pytest.raises(TemtError, match=r"security-group\.ingress\[0\] must be a table$"):
        conf.load(service)


HOME = Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home")
RULES = (
    INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"  # home\nDescription = "home"\n'
    "# about the office\n"
    "\n" + INGRESS + 'Port = [80, 443]\nCidr = "198.51.100.0/24"\n'
    "\n# about the role\n"
    '[[role.jupyter]]\nAction = "s3:*"\nResource = "*"\n'
)


def edited(service: Service, text: str, **changes: Any) -> str:
    """text の設定ファイルの受信規則を書き換え、ファイルの中身を返す。

    書き換えた後の Conf が、ファイルを読み直したものと同じであることも確かめる。
    """
    write(service, text)
    values = conf.load(service)
    conf.edit_ingress(service, values, **changes)
    assert conf.load(service) == values
    return read(service)


def test_a_rule_is_added_after_the_example(service: Service) -> None:
    """apply が書いた例のコメントの後ろ、ファイルの末尾に足す。項目は表の順に並べる。"""
    text = 'AwsRegion = "ap-northeast-1"\n' + conf.EXAMPLE
    rule = Ingress(Protocol="udp", Port=(60000, 61000), Cidr="2001:db8::/64", Description="home")
    icmp = Ingress(Protocol="icmp", Cidr="203.0.113.10/32")
    assert edited(service, text, added=[rule, icmp]) == (
        text + "\n" + INGRESS + 'Protocol = "udp"\nPort = [60000, 61000]\nCidr = "2001:db8::/64"\n'
        'Description = "home"\n'
        "\n" + INGRESS + 'Protocol = "icmp"\nCidr = "203.0.113.10/32"\n'
    )


def test_a_rule_is_added_after_the_last_rule(service: Service) -> None:
    """最後の規則の後ろのコメントは、足した規則の後ろに移る。"""
    rule = Ingress(Port=(8080, 8080), Cidr="203.0.113.10/32")
    assert edited(service, RULES, added=[rule]) == RULES.replace(
        "\n# about the role\n",
        "\n" + INGRESS + 'Port = 8080\nCidr = "203.0.113.10/32"\n\n# about the role\n',
    )


def test_a_description_is_rewritten_and_cleared(service: Service) -> None:
    assert edited(service, RULES, described={0: "", 1: "office"}) == RULES.replace(
        'Description = "home"\n', ""
    ).replace('Cidr = "198.51.100.0/24"\n', 'Cidr = "198.51.100.0/24"\nDescription = "office"\n')


@pytest.mark.parametrize(
    ("removed", "expected"),
    [
        pytest.param(
            [0],
            "# about the office\n"
            "\n" + INGRESS + 'Port = [80, 443]\nCidr = "198.51.100.0/24"\n'
            "\n# about the role\n",
            id="first",
        ),
        pytest.param(
            [1],
            INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"  # home\nDescription = "home"\n'
            "# about the office\n"
            "\n# about the role\n",
            id="last",
        ),
        pytest.param(
            [0, 1], "[security-group]\n# about the office\n\n# about the role\n", id="all"
        ),
    ],
)
def test_the_comments_after_a_removed_rule_are_kept(
    service: Service, removed: list[int], expected: str
) -> None:
    role = RULES[RULES.index("[[role.jupyter]]") :]
    assert edited(service, RULES, removed=removed) == expected + role


def test_the_last_rule_without_comments_takes_the_table_along(service: Service) -> None:
    text = 'AwsRegion = "ap-northeast-1"\n\n' + INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\n'
    assert edited(service, text, removed=[0]) == 'AwsRegion = "ap-northeast-1"\n'


def test_rules_written_as_an_array_of_inline_tables(service: Service) -> None:
    text = '[security-group]\ningress = [{Port = 22, Cidr = "203.0.113.10/32"}]\n'
    assert edited(service, text, removed=[0], added=[HOME]) == (
        "[security-group]\n"
        'ingress = [{Port = 22, Cidr = "203.0.113.10/32", Description = "home"}]\n'
    )


THREE = (
    INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\n'
    "\n" + INGRESS + 'Port = 23\nCidr = "203.0.113.10/32"\n'
    "\n" + INGRESS + 'Port = 24\nCidr = "203.0.113.10/32"\n'
)


def rule(port: int) -> str:
    """THREE の規則 1 つ分。"""
    return INGRESS + f'Port = {port}\nCidr = "203.0.113.10/32"\n'


@pytest.mark.parametrize(
    ("removed", "expected"),
    [
        pytest.param([0], rule(23) + "\n" + rule(24), id="first"),
        pytest.param([1], rule(22) + "\n" + rule(24), id="middle"),
        pytest.param([2], rule(22) + "\n" + rule(23), id="last"),
        pytest.param([0, 1], rule(24), id="two"),
    ],
)
def test_blank_lines_do_not_pile_up(service: Service, removed: list[int], expected: str) -> None:
    """規則の間の空行は 1 つのまま。消した規則の前後の空行を重ねない。"""
    assert edited(service, THREE, removed=removed) == expected


def test_removing_and_adding_again_gives_the_same_file(service: Service) -> None:
    """グローバル IP アドレスが変わったときの sg remove と sg add を繰り返しても、
    ファイルは同じ形に戻る。
    """
    text = 'AwsRegion = "ap-northeast-1"\n' + conf.EXAMPLE
    rules = [HOME, Ingress(Port=(22, 22), Cidr="2001:db8::/64", Description="home")]
    added = edited(service, text, added=rules)
    for _ in range(3):
        assert edited(service, added, removed=[0, 1]) == text
        assert edited(service, text, added=rules) == added


def test_a_rule_is_added_to_a_file_without_a_last_newline(service: Service) -> None:
    text = INGRESS + 'Port = 80\nCidr = "203.0.113.10/32"'
    assert edited(service, text, added=[HOME]) == (
        text + "\n\n" + INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\nDescription = "home"\n'
    )


def test_the_comments_of_a_written_table_are_kept(service: Service) -> None:
    """[security-group] の見出しを書いたファイルでは、最後の規則を消しても見出しとコメントを
    残し、足すときはその中に置く。
    """
    text = "[security-group]\n# my rules\n\n" + INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\n'
    emptied = edited(service, text, removed=[0])
    assert emptied == "[security-group]\n# my rules\n"
    after = '[launch-template]\nInstanceType = "t3.micro"\n'
    assert edited(service, emptied + "\n" + after, added=[HOME]) == (
        "[security-group]\n# my rules\n\n"
        + INGRESS
        + 'Port = 22\nCidr = "203.0.113.10/32"\nDescription = "home"\n\n'
        + after
    )


def test_a_description_and_a_rule_in_one_edit(service: Service) -> None:
    """CIDR を省いた sg add では、IPv4 の説明の書き換えと IPv6 の追加が 1 回で起きる。"""
    six = Ingress(Port=(22, 22), Cidr="2001:db8::/64", Description="office")
    assert edited(service, RULES, described={0: "office"}, added=[six]) == (
        RULES.replace('Description = "home"', 'Description = "office"').replace(
            "\n# about the role\n",
            "\n" + INGRESS + 'Port = 22\nCidr = "2001:db8::/64"\nDescription = "office"\n'
            "\n# about the role\n",
        )
    )


def test_the_example_rule_is_removed_without_a_new_heading(service: Service) -> None:
    """apply が置いた例のうち受信規則だけを外したファイル。最後の規則を消しても
    [security-group] の見出しを書き足さず、後ろのコメントは元の位置に残す。
    """
    top = conf.HEADER + 'AwsRegion = "ap-northeast-1"\n\n[launch-template]\nImageId = "a"\n'
    example = '# [[security-group.ingress]]\n# Port = 22\n# Cidr = "203.0.113.10/32"\n'
    example += '# Description = "home"\n'
    text = top + conf.EXAMPLE.replace(example, example.replace("# ", ""))
    assert edited(service, text, removed=[0]) == top + conf.EXAMPLE.replace(example, "")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "[security-group]\n\n" + rule(22) + "\n# note\n",
            "[security-group]\n\n# note\n",
            id="comment after the rule",
        ),
        pytest.param(
            "[security-group]\n" + rule(22) + '\n[launch-template]\nImageId = "a"\n',
            '[security-group]\n\n[launch-template]\nImageId = "a"\n',
            id="table after the rule",
        ),
    ],
)
def test_blank_lines_in_a_written_table(service: Service, text: str, expected: str) -> None:
    """見出しを書いた [security-group] でも、最後の規則を消したときに空行を重ねず、次の
    テーブルの前の空行も消さない。
    """
    assert edited(service, text, removed=[0]) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            'security-group = {ingress = [{Port = 80, Cidr = "192.0.2.0/24"}]}\n', id="inline"
        ),
        pytest.param("[security-group]\ningress = []\n", id="empty array"),
    ],
)
def test_other_forms_that_can_be_edited(service: Service, text: str) -> None:
    write(service, text)
    values = conf.load(service)
    conf.edit_ingress(service, values, added=[HOME])
    assert conf.load(service) == values
    assert HOME in values.security_group.ingress


AFTER = "\n# about the rules\n"
LAUNCH = '[launch-template]\nImageId = "a"\n'
POLICY = "[role]\n"


@pytest.mark.parametrize(
    ("before", "expected"),
    [
        pytest.param(LAUNCH + "\n[role]\n\n", LAUNCH + "\n[role]\n" + AFTER, id="empty table"),
        pytest.param("[launch-template]\n\n", "[launch-template]\n" + AFTER, id="empty first"),
        pytest.param(
            '[[role.x]]\nEffect = "Allow"\n\n',
            '[[role.x]]\nEffect = "Allow"\n' + AFTER,
            id="array of tables",
        ),
        pytest.param(POLICY + "\n", POLICY + AFTER, id="table in a table without a heading"),
        pytest.param(
            'AwsRegion = "x"\n\n', 'AwsRegion = "x"\n\n[security-group]\n' + AFTER, id="no table"
        ),
    ],
)
def test_the_comments_go_to_the_table_before(service: Service, before: str, expected: str) -> None:
    """見出しを書いていない [security-group] の最後の規則を消すと、後ろのコメントは直前の
    テーブルの後ろに置く。前にテーブルが無ければ、コメントを残すために見出しを書く。
    """
    assert edited(service, before + rule(22) + AFTER, removed=[0]) == expected


@pytest.mark.parametrize(
    ("text", "removed", "expected"),
    [
        pytest.param(
            POLICY + "\n" + rule(22) + LAUNCH, 0, POLICY + "\n" + LAUNCH, id="the last rule"
        ),
        pytest.param(
            "[role]\n\n" + rule(22) + "\n" + rule(23) + LAUNCH,
            1,
            "[role]\n\n" + rule(22) + "\n" + LAUNCH,
            id="a rule after another",
        ),
    ],
)
def test_the_blank_line_before_the_next_table_is_kept(
    service: Service, text: str, removed: int, expected: str
) -> None:
    """消した規則の後ろに空行が無くても、次のテーブルの前の空行は残す。"""
    assert edited(service, text, removed=[removed]) == expected


SPLIT = (
    INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\n'
    '\n[launch-template]\nInstanceType = "t3.micro"\n'
    "\n" + INGRESS + 'Port = 80\nCidr = "203.0.113.10/32"\n'
)
NEW_RULE = Ingress(Port=(443, 443), Cidr="192.0.2.0/24")


@pytest.mark.parametrize(
    ("text", "changes"),
    [
        pytest.param(SPLIT, {"added": [NEW_RULE]}, id="add to rules split by a table"),
        pytest.param(SPLIT, {"removed": [1]}, id="remove from rules split by a table"),
        pytest.param("security-group = {}\n", {"added": [NEW_RULE]}, id="add to an inline table"),
    ],
)
def test_a_file_that_tomlkit_cannot_edit(
    service: Service, text: str, changes: dict[str, Any]
) -> None:
    """書き換えた結果を読み直して、期待と違えば書かずに止まる。成功と知らせて何もしない、
    ファイルを壊す、ということが無いようにする。
    """
    write(service, text)
    values = conf.load(service)
    before = copy.deepcopy(values)
    with pytest.raises(TemtError, match=r"cannot edit the rules in .*; edit \[\[security-group"):
        conf.edit_ingress(service, values, **changes)
    assert read(service) == text
    assert values == before


def test_a_file_changed_while_running(service: Service) -> None:
    """位置は読んだときの並びで数えるので、その後に並びが変わっていれば書かずに止まる。"""
    write(service, THREE)
    values = conf.load(service)
    changed = rule(24) + "\n" + rule(22) + "\n" + rule(23)
    write(service, changed)
    with pytest.raises(TemtError, match="changed while running; run the command again"):
        conf.edit_ingress(service, values, removed=[0])
    assert read(service) == changed


STATEMENT = "[[role.jupyter]]\n"


def test_reads_the_role_policies_as_they_are(service: Service) -> None:
    """テーブルの中身は IAM の書式のまま持ち、変数も Effect も補わない。名前は書いた順に並べる。"""
    write(
        service,
        STATEMENT
        + 'Action = ["s3:GetObject", "s3:PutObject"]\n'
        + 'Resource = "arn:aws:s3:::bucket/${temt:name}/*"\n'
        + '[role.jupyter.Condition.Bool]\n"aws:SecureTransport" = true\n'
        + STATEMENT
        + 'Sid = "Two"\nEffect = "Deny"\nNotAction = "iam:*"\nNotResource = "*"\n'
        + '[[role.idle]]\nEffect = "Allow"\nAction = "ec2:Describe*"\n'
        + 'Resource = "*"\n',
    )
    policies = conf.load(service).role.policies
    assert policies == {
        "jupyter": [
            {
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": "arn:aws:s3:::bucket/${temt:name}/*",
                "Condition": {"Bool": {"aws:SecureTransport": True}},
            },
            {"Sid": "Two", "Effect": "Deny", "NotAction": "iam:*", "NotResource": "*"},
        ],
        "idle": [{"Effect": "Allow", "Action": "ec2:Describe*", "Resource": "*"}],
    }
    assert list(policies) == ["jupyter", "idle"]


def test_no_policy_means_no_role_policy(service: Service) -> None:
    assert conf.load(service, missing_ok=True).role.policies == {}
    write(service, "[role]\n")
    assert conf.load(service).role.policies == {}
    assert not conf.load(service).role


def test_a_statement_key_that_iam_does_not_know_is_left_to_iam(service: Service) -> None:
    """書式は apply が ValidatePolicy で確かめるので、読むときには止めない。"""
    write(service, STATEMENT + 'Resources = "*"\n')
    assert conf.load(service).role.policies == {"jupyter": [{"Resources": "*"}]}


def test_a_statement_with_an_unknown_variable(service: Service) -> None:
    write(service, STATEMENT + 'Resource = "${temt:app}/*"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: role.jupyter[0] uses an unknown variable: ${{temt:app}}"
    )


@pytest.mark.parametrize(
    "written",
    [
        "Sid = 2026-09-27\n",
        'Action = ["s3:GetObject", 12:00:00]\n',
        '[role.jupyter.Condition.DateLessThan]\n"aws:CurrentTime" = 2026-09-27T00:00:00Z\n',
        '[role.jupyter.Condition.NumericLessThan]\n"s3:max-keys" = nan\n',
        '[role.jupyter.Condition.NumericLessThan]\n"s3:max-keys" = inf\n',
    ],
)
def test_a_statement_with_what_json_does_not_have(service: Service, written: str) -> None:
    """TOML の日時、nan、inf は JSON に無い。配列やテーブルの中にあっても止める。"""
    write(service, STATEMENT + 'Effect = "Allow"\n' + written)
    with pytest.raises(
        TemtError,
        match=(
            r"role\.jupyter\[0\] must hold only strings, finite numbers, "
            r"booleans, arrays and tables$"
        ),
    ):
        conf.load(service)


# 角かっこ 1 つのテーブルと、中に階層を作ったテーブルは、どちらも [[role.<ポリシー名>]] と
# 書くよう告げる
NOT_AN_ARRAY = (
    r"role\.jupyter must be an array of tables; "
    r"write \[\[role\.<policy name>\]\] once per statement$"
)


@pytest.mark.parametrize(
    ("written", "message"),
    [
        ('role = "x"\n', r"role must be a table$"),
        ('[role]\njupyter = "x"\n', r"role\.jupyter must be an array of tables$"),
        ("[role.jupyter]\n", NOT_AN_ARRAY),
        ('[role.jupyter]\nVersion = "2012-10-17"\n', NOT_AN_ARRAY),
        ('[[role.jupyter.Statement]]\nEffect = "Allow"\n', NOT_AN_ARRAY),
        ("[role]\njupyter = []\n", r"role\.jupyter must have at least one table$"),
        ('[role]\njupyter = ["Allow"]\n', r"role\.jupyter\[0\] must be a table$"),
    ],
)
def test_a_policy_in_a_wrong_shape(service: Service, written: str, message: str) -> None:
    write(service, written)
    with pytest.raises(TemtError, match=message):
        conf.load(service)


def test_the_policy_table_is_not_a_policy_list(service: Service) -> None:
    """[[role.policy.<ポリシー名>]] は、role.policy というポリシーがテーブルになったものと
    して誤りにする。[[role.policy]] と書くよう案内せず、[[role.<ポリシー名>]] と書くよう
    告げる。
    """
    write(service, '[[role.policy.ec2]]\nAction = "ssm:*"\nResource = "*"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: role.policy must be an array of tables; "
        "write [[role.<policy name>]] once per statement"
    )


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("a_b", '"a_b"'),
        ("-a", '"-a"'),
        ("a" * 65, f'"{"a" * 65}"'),
        ("", '""'),
        ("a.b", '"a.b"'),
        ("\\u65e5", '"\\u65e5"'),
    ],
)
def test_a_policy_name_that_breaks_the_rule(service: Service, name: str, shown: str) -> None:
    """名前は引用して出す。. を含む名前も階層と取り違えず、ASCII でない名前も ASCII で出す。"""
    write(service, f'[[role."{name}"]]\nEffect = "Allow"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: role.{shown}: the policy name {LABEL_RULE}"
    )


def test_the_example_reads_as_a_policy(service: Service) -> None:
    """apply が書く例は、コメントを外せば正しい設定として読める。説明の行は外さない。"""
    text = "\n".join(
        line.removeprefix("# ")
        if line.startswith("# [") or (line.startswith("# ") and "=" in line)
        else line
        for line in conf.EXAMPLE.splitlines()
    )
    write(service, text)
    assert list(conf.load(service).role.policies) == ["example"]


@pytest.mark.parametrize("name", ["Statement", "Jupyter", "Version"])
def test_an_unknown_key_in_the_role_table(service: Service, name: str) -> None:
    """ポリシー名は英小文字で始まるので、大文字で始まる名前は知らない項目とする。"""
    write(service, f'[[role.{name}]]\nEffect = "Allow"\n')
    with pytest.raises(TemtError, match=rf"unknown key: role\.{name}$"):
        conf.load(service)


def test_reads_the_managed_policies(service: Service) -> None:
    write(
        service,
        '[role]\nManagedPolicyArns = ["arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess", '
        '"arn:aws:iam::123456789012:policy/mine"]\n',
    )
    role = conf.load(service).role
    assert role == Role(
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
            "arn:aws:iam::123456789012:policy/mine",
        ]
    )
    assert role


def test_no_managed_policy_means_none(service: Service) -> None:
    """空の配列は書かれていないものとする。"""
    write(service, "[role]\nManagedPolicyArns = []\n")
    role = conf.load(service).role
    assert role.ManagedPolicyArns == []
    assert not role


@pytest.mark.parametrize("written", ['"arn:aws:iam::aws:policy/ReadOnlyAccess"', "[1]", '[""]'])
def test_managed_policies_that_are_not_arns(service: Service, written: str) -> None:
    write(service, f"[role]\nManagedPolicyArns = {written}\n")
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: role.ManagedPolicyArns must be an array of non-empty strings"
    )


def test_a_role_is_written_when_it_has_policies_or_managed_policies() -> None:
    assert not Role()
    assert Role(policies={"ec2": [{"Action": "s3:*", "Resource": "*"}]})
    assert Role(ManagedPolicyArns=["arn:aws:iam::aws:policy/ReadOnlyAccess"])


# [[role.ec2]] の既定


def test_the_ec2_default_is_two_statements_without_effect(service: Service) -> None:
    """既定は、SSM エージェントの権限と、自分の階層のパラメータの読み書き。Effect は省く。"""
    write(service, conf.EC2_DEFAULT)
    statements = conf.load(service).role.policies[conf.EC2]
    assert statements == conf.ec2_default()
    assert [list(one) for one in statements] == [["Action", "Resource"], ["Action", "Resource"]]
    # State Manager の権限は含めない
    assert statements[0]["Action"] == [
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
    ]
    assert statements[0]["Resource"] == "*"
    assert statements[1]["Resource"] == [
        "arn:aws:ssm:*:*:parameter/${temt:name}",
        "arn:aws:ssm:*:*:parameter/${temt:name}/*",
    ]


def test_the_scaffold_has_the_documented_layout(service: Service) -> None:
    """雛形は、先頭の説明、IamInstanceProfile、[[role.ec2]] の既定、例のコメントの順。

    docs/conf.md の雛形の例と同じもの。読めば、temt のインスタンスプロファイルを指し、
    [[role.ec2]] の既定を持つ。
    """
    text = conf.scaffold(profile=True, ec2=True)
    assert text == conf.HEADER + conf.SCAFFOLD_PROFILE + conf.EC2_DEFAULT + conf.EXAMPLE
    assert text.startswith(
        "# Written by temt apply. Edit and run apply again to update.\n"
        "\n"
        "[launch-template]\n"
        'IamInstanceProfile.Name = "${temt:name}"\n'
        "\n"
        "# Permissions of the instance role that are not tied to one app."
    )
    docs = (Path(__file__).parent.parent / "docs" / "conf.md").read_text(encoding="utf-8")
    assert f"```toml\n{text}```\n" in docs
    values = conf.parse_text(text, service.conf_path)
    assert values.launch_template == {"IamInstanceProfile": {"Name": "${temt:name}"}}
    assert values.role.policies == {conf.EC2: conf.ec2_default()}


def test_the_scaffold_writes_the_aws_profile_first(service: Service) -> None:
    """AwsProfile は最上位の項目なので、先頭の説明のすぐ後、テーブルより前に書く。"""
    text = conf.scaffold(profile=True, ec2=True, aws_profile='my "work"')
    assert text.startswith(f'{conf.HEADER}\nAwsProfile = "my \\"work\\""\n\n[launch-template]\n')
    assert conf.parse_text(text, service.conf_path).AwsProfile == 'my "work"'


@pytest.mark.parametrize(("profile", "ec2"), [(False, True), (False, False)])
def test_the_scaffold_leaves_out_what_is_not_asked(profile: bool, ec2: bool) -> None:
    """AwsProfile、IamInstanceProfile、[[role.ec2]] は、書かないこともできる。例のコメントは
    残す。
    """
    text = conf.scaffold(profile=profile, ec2=ec2)
    assert "AwsProfile" not in text
    assert "IamInstanceProfile" not in text
    assert ("[[role.ec2]]" in text) is ec2
    assert text.startswith(conf.HEADER)
    assert text.endswith(conf.EXAMPLE)


def test_common_profile_is_the_instance_profile_of_the_common_file(home_dir: Path) -> None:
    """共通ファイルの IamInstanceProfile。ファイルか項目が無ければ None。"""
    _ = home_dir
    assert conf.common_profile() is None
    write_defaults('AwsRegion = "us-west-2"\n')
    assert conf.common_profile() is None
    write_defaults(
        '[launch-template]\nIamInstanceProfile.Arn = "arn:aws:iam::1:instance-profile/x"\n'
    )
    assert conf.common_profile() == {"Arn": "arn:aws:iam::1:instance-profile/x"}


def test_create_makes_a_new_file_only(service: Service) -> None:
    """無ければ作る。既にあれば、上書きせずに止める。"""
    conf.create(service, "# new\n")
    assert read(service) == "# new\n"
    with pytest.raises(TemtError) as caught:
        conf.create(service, "# other\n")
    assert str(caught.value) == (
        f"{tilde(service.conf_path)} was made while running apply; it is left as it is"
    )
    assert read(service) == "# new\n"


def test_take_region_env_removes_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    conf.take_region_env()
    assert not [name for name in conf.REGION_ENV if name in os.environ]


def test_take_region_env_leaves_the_profile_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """プロファイルの環境変数は取り除かない。資格情報は AWS の既定の規則で決まる。"""
    monkeypatch.setenv("AWS_PROFILE", "work")
    monkeypatch.setenv("AWS_DEFAULT_PROFILE", "old")
    conf.take_region_env()
    assert os.environ["AWS_PROFILE"] == "work"
    assert os.environ["AWS_DEFAULT_PROFILE"] == "old"


def test_take_region_env_without_any(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in conf.REGION_ENV:
        monkeypatch.delenv(name, raising=False)
    conf.take_region_env()
    assert not [name for name in conf.REGION_ENV if name in os.environ]


def test_written_reads_the_item_as_it_is_written(service: Service) -> None:
    """既定は補わない。空の文字列も書かれたまま返す。"""
    write(service, 'AwsProfile = "mine"\n\n[launch-template]\nInstanceType = ""\n')
    assert conf.written(service, "AwsProfile") == "mine"
    assert conf.written(service, "launch-template.InstanceType") == ""
    assert conf.written(service, "AwsRegion") is None
    assert conf.written(service, "launch-template.ImageId") is None
    assert conf.written(service, "security-group.Missing") is None


def test_written_is_none_without_the_file(service: Service) -> None:
    assert conf.written(service, "AwsRegion") is None


# --- 書き換えの置き換え ---

# 書き換える前の設定ファイル
BEFORE = 'AwsProfile = "work"\n'
# 設定ファイルを書き換える関数。どれも replace_text で置き換える
WRITERS = {
    "ingress": lambda service, values: conf.edit_ingress(service, values, added=[HOME]),
}


@pytest.mark.parametrize("writer", WRITERS.values(), ids=WRITERS.keys())
def test_a_failed_write_leaves_the_file_as_it_was(
    service: Service, monkeypatch: pytest.MonkeyPatch, writer: Any
) -> None:
    """ディスクが一杯などで書けなくても、設定ファイルは元のまま残り、一時ファイルも残らない。

    空や書きかけになると、空の TOML は既定値で読めてしまい、AwsRegion などが既定に戻る。
    一時ファイルは同じディレクトリに置き、名前にプロセス ID を入れる。
    """
    write(service, BEFORE)
    values = conf.load(service)
    directory = service.conf_path.parent
    seen: list[str] = []

    def full(fd: int) -> None:
        seen.extend(sorted(path.name for path in directory.iterdir()))
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError, match="No space left on device"):
        writer(service, values)

    assert seen == [f".dev-main.toml.{os.getpid()}.tmp", "dev-main.toml"]
    assert read(service) == BEFORE
    assert [path.name for path in directory.iterdir()] == ["dev-main.toml"]


@pytest.mark.parametrize("writer", WRITERS.values(), ids=WRITERS.keys())
def test_writing_keeps_the_mode_and_a_symlinked_file(
    service: Service, home_dir: Path, writer: Any
) -> None:
    """置き換えても、ファイルのモードと、dotfiles などを指すリンクはそのまま残る。"""
    real = home_dir / "dotfiles" / "dev-main.toml"
    real.parent.mkdir()
    real.write_text(BEFORE, encoding="utf-8")
    real.chmod(0o640)
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.symlink_to(real)

    writer(service, conf.load(service))

    assert service.conf_path.is_symlink()
    assert read(service) != BEFORE
    assert real.stat().st_mode & 0o777 == 0o640
    assert [path.name for path in real.parent.iterdir()] == ["dev-main.toml"]


def write_defaults(text: str) -> None:
    """共通ファイルを用意する。"""
    defaults_path().parent.mkdir(parents=True, exist_ok=True)
    defaults_path().write_text(text, encoding="utf-8")


EC2_COMMON = '[[role.ec2]]\nAction = ["s3:ListBucket"]\nResource = "*"\n'
RULE = '[[security-group.ingress]]\nPort = 22\nCidr = "{}"\n'


def test_the_common_file_fills_what_the_service_does_not_write(service: Service) -> None:
    """[launch-template] は、中のテーブルも項目ごとに重ねる。"""
    write_defaults(
        'AwsProfile = "work"\nAwsRegion = "us-east-1"\n\n'
        '[launch-template]\nInstanceType = "t3.small"\nKeyName = "common"\n'
        'MetadataOptions.HttpTokens = "required"\n'
    )
    write(
        service,
        'AwsRegion = "eu-west-1"\n\n[launch-template]\nKeyName = "mine"\n'
        'MetadataOptions.InstanceMetadataTags = "enabled"\n',
    )
    assert conf.load(service) == Conf(
        AwsProfile="work",
        AwsRegion="eu-west-1",
        launch_template={
            "InstanceType": "t3.small",
            "KeyName": "mine",
            "MetadataOptions": {"HttpTokens": "required", "InstanceMetadataTags": "enabled"},
        },
    )


def test_a_blank_value_of_the_service_leaves_the_common_one(service: Service) -> None:
    """空の値は書かれていないものなので、共通ファイルの値が効く。空のテーブルと配列、
    要素の中の空の値も同じ。
    """
    write_defaults(
        'AwsProfile = "work"\n\n[launch-template]\nInstanceType = "t3.small"\n'
        'SecurityGroupIds = ["sg-common"]\nMetadataOptions.HttpTokens = "required"\n'
        "\n" + PRIMARY + 'SubnetId = "subnet-common"\n'
    )
    write(
        service,
        'AwsProfile = ""\n\n[launch-template]\nInstanceType = ""\nSecurityGroupIds = []\n'
        "MetadataOptions = {}\n\n" + PRIMARY + 'SubnetId = ""\n',
    )
    loaded = conf.load(service)
    assert loaded.AwsProfile == "work"
    assert loaded.launch_template == {
        "InstanceType": "t3.small",
        "SecurityGroupIds": ["sg-common"],
        "MetadataOptions": {"HttpTokens": "required"},
        "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": "subnet-common"}],
    }


def test_network_interfaces_of_both_files_are_merged_by_device_index(service: Service) -> None:
    """同じ DeviceIndex の要素は項目ごとに重ね、違う要素は両方を使う（共通ファイルが先）。
    要素の中の Groups は、設定ファイルに書けば丸ごと置き換える。

    設定ファイルの要素に SubnetId を書いても、共通ファイルの要素の項目は効き続ける。
    """
    write_defaults(
        '[[launch-template.NetworkInterfaces]]\nDeviceIndex = 1\nSubnetId = "subnet-common"\n'
        "\n" + PRIMARY + 'AssociatePublicIpAddress = false\nGroups = ["sg-a", "sg-b"]\n'
    )
    write(
        service,
        PRIMARY + 'SubnetId = "subnet-1"\nGroups = ["sg-c"]\n'
        '\n[[launch-template.NetworkInterfaces]]\nDeviceIndex = 2\nSubnetId = "subnet-2"\n',
    )
    assert conf.load(service).launch_template == {
        "NetworkInterfaces": [
            {"DeviceIndex": 1, "SubnetId": "subnet-common"},
            {
                "DeviceIndex": 0,
                "AssociatePublicIpAddress": False,
                "Groups": ["sg-c"],
                "SubnetId": "subnet-1",
            },
            {"DeviceIndex": 2, "SubnetId": "subnet-2"},
        ]
    }


def test_tags_of_both_files_are_both_put_on(service: Service) -> None:
    """インスタンスのタグ Owner を共通ファイルに、Env を設定ファイルに書けば、両方付く。
    同じ Key のタグは設定ファイルの値にする。
    """
    write_defaults(
        '[[launch-template.TagSpecifications]]\nResourceType = "instance"\n'
        'Tags = [{ Key = "Owner", Value = "me" }, { Key = "Env", Value = "common" }]\n'
    )
    write(
        service,
        '[[launch-template.TagSpecifications]]\nResourceType = "volume"\n'
        'Tags = [{ Key = "Backup", Value = "daily" }]\n'
        '\n[[launch-template.TagSpecifications]]\nResourceType = "instance"\n'
        'Tags = [{ Key = "Env", Value = "dev" }, { Key = "Team", Value = "web" }]\n',
    )
    assert conf.load(service).launch_template == {
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Owner", "Value": "me"},
                    {"Key": "Env", "Value": "dev"},
                    {"Key": "Team", "Value": "web"},
                ],
            },
            {"ResourceType": "volume", "Tags": [{"Key": "Backup", "Value": "daily"}]},
        ]
    }


def test_the_root_volume_of_both_files_is_one_element(service: Service) -> None:
    """DeviceName を省いた要素どうしは、どちらもルートボリュームを指すので重ねる。"""
    write_defaults(
        '[[launch-template.BlockDeviceMappings]]\nEbs = { VolumeSize = 16, VolumeType = "gp3" }\n'
    )
    write(service, "[[launch-template.BlockDeviceMappings]]\nEbs.VolumeSize = 32\n")
    assert conf.load(service).launch_template == {
        "BlockDeviceMappings": [{"Ebs": {"VolumeSize": 32, "VolumeType": "gp3"}}]
    }


def test_other_arrays_of_the_service_replace_the_common_ones(service: Service) -> None:
    """要素を対応づけるキーの無い配列は、設定ファイルに書けば丸ごと置き換える。"""
    write_defaults(
        '[launch-template]\nSecurityGroupIds = ["sg-a", "sg-b"]\n'
        '\n[[launch-template.LicenseSpecifications]]\nLicenseConfigurationArn = "arn:a"\n'
        '\n[role]\nManagedPolicyArns = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]\n'
    )
    write(
        service,
        '[launch-template]\nSecurityGroupIds = ["sg-c"]\n'
        '\n[[launch-template.LicenseSpecifications]]\nLicenseConfigurationArn = "arn:b"\n'
        '\n[role]\nManagedPolicyArns = ["arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"]\n',
    )
    loaded = conf.load(service)
    assert loaded.launch_template == {
        "SecurityGroupIds": ["sg-c"],
        "LicenseSpecifications": [{"LicenseConfigurationArn": "arn:b"}],
    }
    assert loaded.role.ManagedPolicyArns == ["arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"]


def test_a_repeated_key_in_the_common_file_names_it(service: Service) -> None:
    write_defaults(PRIMARY + "\n" + PRIMARY)
    write(service, "")
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(defaults_path())}: launch-template.NetworkInterfaces has two elements with "
        "DeviceIndex = 0"
    )


def test_rules_of_both_files_are_joined(service: Service) -> None:
    write_defaults(RULE.format("198.51.100.0/24"))
    write(service, RULE.format("203.0.113.10/32"))
    assert [one.Cidr for one in conf.load(service).security_group.ingress] == [
        "198.51.100.0/24",
        "203.0.113.10/32",
    ]


def test_a_rule_written_in_both_files_is_an_error(service: Service) -> None:
    write_defaults(RULE.format("198.51.100.0/24"))
    write(service, RULE.format("203.0.113.10/32") + RULE.format("198.51.100.0/24"))
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{tilde(service.conf_path)}: security-group.ingress[1] repeats a rule in "
        f"{tilde(defaults_path())}; remove one of them"
    )


def test_policies_are_joined_by_name(service: Service) -> None:
    """同じ名前のポリシーは、置き換えずに、共通ファイルの Statement の後ろに設定ファイルの
    Statement をつなげる。各 Statement を書いたファイルと、その中での位置を覚える。
    """
    write_defaults(
        '[[role.ec2]]\nAction = ["s3:ListBucket"]\nResource = "*"\n\n'
        '[[role.logs]]\nAction = ["logs:PutLogEvents"]\nResource = "*"\n'
    )
    write(
        service,
        '[[role.ec2]]\nAction = ["ec2:DescribeTags"]\nResource = "*"\n\n'
        '[[role.ec2]]\nAction = ["ec2:DescribeTags"]\nResource = "*"\n\n'
        '[[role.app]]\nAction = ["s3:GetObject"]\nResource = "*"\n',
    )
    role = conf.load(service).role
    assert role.policies == {
        "ec2": [
            {"Action": ["s3:ListBucket"], "Resource": "*"},
            {"Action": ["ec2:DescribeTags"], "Resource": "*"},
            {"Action": ["ec2:DescribeTags"], "Resource": "*"},
        ],
        "logs": [{"Action": ["logs:PutLogEvents"], "Resource": "*"}],
        "app": [{"Action": ["s3:GetObject"], "Resource": "*"}],
    }
    path = service.conf_path
    assert [role.origin("ec2", index, path) for index in range(3)] == [
        (defaults_path(), 0),
        (path, 0),
        (path, 1),
    ]
    assert role.origin("logs", 0, path) == (defaults_path(), 0)
    assert role.origin("app", 0, path) == (path, 0)


def test_policies_of_one_file_remember_it(service: Service) -> None:
    """共通ファイルを読まなくても、各 Statement を書いたファイルを覚える。"""
    write(service, "Defaults = false\n\n" + EC2_COMMON + "\n" + EC2_COMMON)
    write_defaults(EC2_COMMON)
    role = conf.load(service).role
    assert [role.origin("ec2", index, defaults_path()) for index in range(2)] == [
        (service.conf_path, 0),
        (service.conf_path, 1),
    ]


def test_managed_policies_of_the_service_replace_the_common_ones(service: Service) -> None:
    """ManagedPolicyArns はほかの配列と同じく、設定ファイルに書けば置き換える。"""
    write_defaults('[role]\nManagedPolicyArns = ["arn:aws:iam::aws:policy/A"]\n')
    write(service, "")
    assert conf.load(service).role.ManagedPolicyArns == ["arn:aws:iam::aws:policy/A"]
    write(service, '[role]\nManagedPolicyArns = ["arn:aws:iam::aws:policy/B"]\n')
    assert conf.load(service).role.ManagedPolicyArns == ["arn:aws:iam::aws:policy/B"]
    write(service, "[role]\nManagedPolicyArns = []\n")
    assert conf.load(service).role.ManagedPolicyArns == ["arn:aws:iam::aws:policy/A"]


def test_the_instance_profile_of_the_service_replaces_the_common_one(service: Service) -> None:
    """IamInstanceProfile はテーブルでも項目ごとに重ねず、丸ごと置き換える。共通ファイルの
    Arn と設定ファイルの Name が混ざらない。
    """
    write_defaults(
        '[launch-template]\nIamInstanceProfile.Arn = "arn:aws:iam::1:instance-profile/x"\n'
    )
    write(service, '[launch-template]\nIamInstanceProfile.Name = "${temt:name}"\n')
    assert conf.load(service).launch_template == {"IamInstanceProfile": {"Name": "${temt:name}"}}


def test_an_error_in_the_common_file_names_it(service: Service) -> None:
    write_defaults("[launch-template]\nInstanceType = 3\n")
    write(service, "")
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert (
        str(caught.value)
        == f"{tilde(defaults_path())}: launch-template.InstanceType must be a string"
    )


def test_without_a_config_file_the_common_file_still_counts(service: Service) -> None:
    """apply は、まだ無い設定ファイルを共通ファイルと既定から始める。"""
    write_defaults('[launch-template]\nInstanceType = "t3.small"\n')
    assert conf.load(service, missing_ok=True).launch_template == {"InstanceType": "t3.small"}
    with pytest.raises(TemtError, match="no config file"):
        conf.load(service)


def test_load_can_leave_the_common_file_out(service: Service) -> None:
    write_defaults('AwsProfile = "work"\n' + RULE.format("198.51.100.0/24"))
    write(service, RULE.format("203.0.113.10/32"))
    loaded = conf.load(service, common=False)
    assert loaded.AwsProfile is None
    assert [one.Cidr for one in loaded.security_group.ingress] == ["203.0.113.10/32"]


def test_common_values_are_only_what_is_written() -> None:
    assert conf.common_values() == {}
    write_defaults('AwsProfile = "work"\nAwsRegion = ""\n')
    assert conf.common_values() == {"AwsProfile": "work"}


def test_a_broken_symlink_as_the_common_file_is_an_error(service: Service) -> None:
    """無いものとすると、環境変数のプロファイルへ黙って移ってしまう。"""
    defaults_path().parent.mkdir(parents=True, exist_ok=True)
    defaults_path().symlink_to(defaults_path().parent / "gone.toml")
    write(service, "")
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{tilde(defaults_path())}: broken symlink"
    with pytest.raises(TemtError, match="broken symlink"):
        conf.common_values()


def test_source_names_the_file_that_holds_the_value(service: Service) -> None:
    """空の値は書かれていないものとする。Defaults = false なら、共通ファイルを指さない。"""
    key = conf.primary_key("SubnetId")
    write_defaults(PRIMARY + 'SubnetId = "subnet-common"\n')
    write(service, 'AwsProfile = ""\n\n' + PRIMARY + 'SubnetId = ""\n')
    assert conf.source(service, key) == defaults_path()
    assert conf.source(service, "AwsProfile") == service.conf_path
    write(service, PRIMARY + 'SubnetId = "subnet-1"\n')
    assert conf.source(service, key) == service.conf_path
    write(service, "Defaults = false\n")
    assert conf.source(service, key) == service.conf_path


def test_a_role_in_the_common_file_counts_for_the_service(service: Service) -> None:
    """共通ファイルにだけ [role] があっても、そのサービスの [role] として扱う。"""
    write_defaults(EC2_COMMON)
    write(service, 'AwsRegion = "us-east-1"\n')
    assert conf.load(service).role
    write(service, "Defaults = false\n")
    assert not conf.load(service).role


def test_the_region_option_comes_before_the_common_file(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AwsRegion が書かれていなければ、--region を共通ファイルの AwsRegion より先にする。"""
    write_defaults('AwsRegion = "us-east-1"\n')
    write(service, "[launch-template]\n")
    assert conf.load(service).AwsRegion == "us-east-1"
    monkeypatch.setattr(conf, "OPTION_REGION", "eu-west-1")
    assert conf.load(service).AwsRegion == "eu-west-1"


def test_the_region_option_must_match_the_written_region(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """書かれた AwsRegion と同じ --region は通り、違えば引数の誤りにする。"""
    write(service, 'AwsRegion = "us-east-1"\n')
    monkeypatch.setattr(conf, "OPTION_REGION", "us-east-1")
    assert conf.load(service).AwsRegion == "us-east-1"
    monkeypatch.setattr(conf, "OPTION_REGION", "eu-west-1")
    with pytest.raises(
        UsageError, match=r"^dev-main has AwsRegion = us-east-1; --region eu-west-1 differs$"
    ):
        conf.load(service)


def test_the_profile_option_comes_before_the_common_file(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AwsProfile が書かれていなければ、--profile を共通ファイルの AwsProfile より先にする。"""
    write_defaults('AwsProfile = "common"\n')
    write(service, "[launch-template]\n")
    assert conf.load(service).AwsProfile == "common"
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    assert conf.load(service).AwsProfile == "work"


def test_the_profile_option_must_match_the_written_profile(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """書かれた AwsProfile は固定で、同じ --profile は通り、違えば引数の誤りにする。"""
    write_defaults('AwsProfile = "common"\n')
    write(service, 'AwsProfile = "mine"\n')
    monkeypatch.setattr(conf, "OPTION_PROFILE", "mine")
    assert conf.load(service).AwsProfile == "mine"
    monkeypatch.setattr(conf, "OPTION_PROFILE", "work")
    with pytest.raises(
        UsageError, match=r"^dev-main has AwsProfile = mine; --profile work differs$"
    ):
        conf.load(service)


def test_defaults_false_reads_no_common_file(service: Service) -> None:
    """Defaults = false のサービスは、共通ファイルの値も受信規則も使わない。"""
    write_defaults(
        'AwsProfile = "work"\nAwsRegion = "us-east-1"\n\n[launch-template]\nInstanceType = '
        '"t3.small"\n\n' + RULE.format("198.51.100.0/24")
    )
    write(service, "Defaults = false\n")
    loaded = conf.load(service)
    assert loaded.Defaults is False
    assert (loaded.AwsProfile, loaded.AwsRegion) == (None, conf.UNSET)
    assert loaded.launch_template == {}
    assert loaded.security_group.ingress == []
    assert conf.source(service, "AwsProfile") == service.conf_path


def test_defaults_cannot_be_written_in_the_common_file(service: Service) -> None:
    write_defaults("Defaults = false\n")
    write(service, "")
    with pytest.raises(TemtError, match=r"defaults\.toml: Defaults cannot be written in this file"):
        conf.load(service)


@pytest.mark.parametrize("key", ["Defaults", "WaitForSsm"])
def test_switches_must_be_booleans(service: Service, key: str) -> None:
    write(service, f'{key} = "false"\n')
    with pytest.raises(TemtError, match=rf"dev-main\.toml: {key} must be true or false"):
        conf.load(service)


def test_wait_for_ssm_is_read(service: Service) -> None:
    write(service, "WaitForSsm = false\n")
    assert conf.load(service).WaitForSsm is False
