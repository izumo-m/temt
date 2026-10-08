"""設定ファイルの読み書きのテスト。"""

import copy
import errno
import os
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from temt import conf
from temt.conf import Conf, Ingress, LaunchTemplate, SecurityGroup
from temt.msg import TemtError
from temt.names import LABEL_RULE, Service, defaults_path

TEMPLATE = conf.LAUNCH_TEMPLATE


def write(service: Service, text: str) -> None:
    """設定ファイルを用意する。"""
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(text, encoding="utf-8")


def read(service: Service) -> str:
    """設定ファイルの中身。"""
    return service.conf_path.read_text(encoding="utf-8")


def test_reads_items(service: Service) -> None:
    write(
        service,
        '# comment\nAwsRegion = "us-east-1"\n\n[launch-template]\nInstanceType="t3.small"\n'
        'SubnetId = "subnet-1"\n',
    )
    assert conf.load(service) == Conf(
        AwsRegion="us-east-1",
        launch_template=LaunchTemplate(InstanceType="t3.small", SubnetId="subnet-1"),
    )


def test_reads_a_table_written_with_dotted_keys(service: Service) -> None:
    """TOML では launch-template.InstanceType = ... もテーブルの項目である。"""
    write(service, 'launch-template.InstanceType = "t3.small"\n')
    assert conf.load(service).launch_template.InstanceType == "t3.small"


def test_unknown_key_reports_the_key(service: Service) -> None:
    write(service, 'AwsRegion = "us-east-1"\nFoo = "bar"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{service.conf_path}: unknown key: Foo"


def test_unknown_key_with_a_control_character_is_shown_safely(service: Service) -> None:
    write(service, '"\\u001b[2J" = "x"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{service.conf_path}: unknown key: ?[2J"


def test_unknown_key_in_a_table_reports_the_table_too(service: Service) -> None:
    write(service, '[launch-template]\nFoo = "bar"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{service.conf_path}: unknown key: launch-template.Foo"


@pytest.mark.parametrize("key", ["InstanceType", "SubnetId", "Login", "KeyName", "IdentityFile"])
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
    with pytest.raises(TemtError, match=str(service.conf_path)):
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


def test_empty_subnet_id_stays_unset(service: Service) -> None:
    """空の SubnetId は未設定として扱い、apply が選び直せるようにする。"""
    write(service, '[launch-template]\nSubnetId = ""\n')
    assert conf.load(service).launch_template.SubnetId is conf.UNSET


def test_empty_table_means_the_defaults(service: Service) -> None:
    write(service, "[launch-template]\n")
    assert conf.load(service) == Conf()


def test_load_without_a_file_tells_to_run_apply(service: Service) -> None:
    with pytest.raises(TemtError, match="temt apply --svc dev-main"):
        conf.load(service)


def test_load_shows_a_conf_dir_with_a_control_character_safely(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ホームから来るパスも、表示できない文字を ? にして誤りに入れる。

    TEMT_CONF_DIR は表示できない文字を断るので、ホームのほうに入れる。
    """
    home = home_dir / "\x1b[2J"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(TemtError) as caught:
        conf.load(Service("dev-main"))
    assert str(caught.value) == (
        f"no config file: {home_dir}/?[2J/.config/temt/dev-main.toml; "
        "run 'temt apply --svc dev-main' first"
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
    assert values.AwsRegion == "ap-northeast-1"
    assert values.launch_template.InstanceType == "t3.micro"
    assert values.launch_template.SubnetId is conf.UNSET
    assert values.SshdPorts == [22]


def test_each_conf_has_its_own_sshd_ports() -> None:
    """既定の並びを共有しない。"""
    first = Conf()
    first.SshdPorts.append(2222)
    assert Conf().SshdPorts == [22]


def test_reads_the_sshd_ports_in_their_order(service: Service) -> None:
    write(service, "SshdPorts = [2222, 443]\n")
    assert conf.load(service).SshdPorts == [2222, 443]


def test_empty_sshd_ports_mean_the_default(service: Service) -> None:
    write(service, "SshdPorts = []\n")
    assert conf.load(service) == Conf()


@pytest.mark.parametrize(
    ("written", "message"),
    [
        pytest.param("22", "must be an array of integers", id="not an array"),
        pytest.param('["22"]', "must be an array of integers", id="string"),
        pytest.param("[true]", "must be an array of integers", id="boolean"),
        pytest.param("[22.0]", "must be an array of integers", id="fraction"),
        pytest.param("[0]", "must hold ports within 1-65535: 0", id="zero"),
        pytest.param("[22, 65536]", "must hold ports within 1-65535: 65536", id="too large"),
        pytest.param("[2222, 443, 2222]", "repeats the port 2222", id="repeated"),
    ],
)
def test_sshd_ports_that_are_not_ports(service: Service, written: str, message: str) -> None:
    write(service, f"SshdPorts = {written}\n")
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{service.conf_path}: SshdPorts {message}"


def test_the_sshd_ports_are_not_in_a_table(service: Service) -> None:
    write(service, "[launch-template]\nSshdPorts = [22]\n")
    with pytest.raises(TemtError, match="unknown key: launch-template.SshdPorts$"):
        conf.load(service)


def test_reads_the_volume_size(service: Service) -> None:
    write(service, "[launch-template]\nVolumeSize = 30\n")
    assert conf.load(service).launch_template.VolumeSize == 30


def test_no_volume_size_means_the_size_of_the_ami(service: Service) -> None:
    write(service, "[launch-template]\n")
    assert conf.load(service).launch_template.VolumeSize is None


@pytest.mark.parametrize(
    ("written", "message"),
    [
        pytest.param('"30"', "must be an integer", id="string"),
        pytest.param('""', "must be an integer", id="empty string"),
        pytest.param("true", "must be an integer", id="boolean"),
        pytest.param("30.0", "must be an integer", id="fraction"),
        pytest.param("[30]", "must be an integer", id="array"),
        pytest.param("0", "must be 1 or more: 0", id="zero"),
        pytest.param("-8", "must be 1 or more: -8", id="negative"),
    ],
)
def test_volume_sizes_that_are_not_sizes(service: Service, written: str, message: str) -> None:
    write(service, f"[launch-template]\nVolumeSize = {written}\n")
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{service.conf_path}: launch-template.VolumeSize {message}"


def test_a_large_volume_size_is_left_to_aws(service: Service) -> None:
    """上限はボリュームの種類で違うので、temt は確かめない。"""
    write(service, "[launch-template]\nVolumeSize = 1000000\n")
    assert conf.load(service).launch_template.VolumeSize == 1000000


def test_the_volume_size_is_in_the_launch_template_table(service: Service) -> None:
    write(service, "VolumeSize = 30\n")
    with pytest.raises(TemtError, match="unknown key: VolumeSize$"):
        conf.load(service)


def test_each_conf_has_its_own_table() -> None:
    """既定のテーブルは Conf ごとに別のオブジェクトで、片方を変えても他方は変わらない。"""
    one, other = Conf(), Conf()
    one.launch_template.InstanceType = "t3.small"
    assert other.launch_template.InstanceType == "t3.micro"


def test_update_creates_the_file_with_a_header(service: Service) -> None:
    values = Conf()
    assert conf.update(service, values, "AwsRegion", "us-east-1") is True
    assert read(service) == conf.HEADER + 'AwsRegion = "us-east-1"\n'


def test_update_makes_the_directory_private(service: Service) -> None:
    """temt が作るディレクトリは、どれも他人に見せないので 700 で作る。"""
    conf.update(service, Conf(), "AwsRegion", "us-east-1")
    assert service.conf_path.parent.stat().st_mode & 0o777 == 0o700


def test_update_sets_the_field_too(service: Service) -> None:
    values = Conf()
    conf.update(service, values, "AwsProfile", "work")
    assert values.AwsProfile == "work"


def test_update_sets_the_field_in_the_table_too(service: Service) -> None:
    values = Conf()
    conf.update(service, values, f"{TEMPLATE}.SubnetId", "subnet-0123456789abcdef0")
    assert values.launch_template.SubnetId == "subnet-0123456789abcdef0"


def test_update_adds_the_key_when_it_is_absent(service: Service) -> None:
    write(service, '# mine\nAwsProfile = "work"\n')
    assert conf.update(service, Conf(), "AwsRegion", "us-east-1") is True
    assert read(service) == '# mine\nAwsProfile = "work"\nAwsRegion = "us-east-1"\n'


def test_update_adds_the_table_when_it_is_absent(service: Service) -> None:
    write(service, 'AwsProfile = "work"\n')
    assert conf.update(service, Conf(), f"{TEMPLATE}.InstanceType", "t3.micro") is True
    assert read(service) == 'AwsProfile = "work"\n\n[launch-template]\nInstanceType = "t3.micro"\n'


def test_update_adds_the_key_to_the_existing_table(service: Service) -> None:
    write(service, '[launch-template]\nInstanceType = "t3.micro"  # c\n')
    assert conf.update(service, Conf(), f"{TEMPLATE}.SubnetId", "subnet-1") is True
    assert (
        read(service)
        == '[launch-template]\nInstanceType = "t3.micro"  # c\nSubnetId = "subnet-1"\n'
    )


def test_update_adds_the_key_before_the_comments_of_the_next_table(service: Service) -> None:
    """後ろのコメントは次の見出しのものなので、その前に残す。例の見出しを戻しても、
    足した項目はそのテーブルのものにならない。"""
    write(
        service,
        '[launch-template]\nInstanceType = "t3.micro"\n\n# about the rules\n'
        "# [[security-group.ingress]]\n# Port = 22\n[x]\nA = 1\n",
    )
    assert conf.update(service, Conf(), f"{TEMPLATE}.ImageId", "ami-9") is True
    text = read(service)
    assert text == (
        '[launch-template]\nInstanceType = "t3.micro"\nImageId = "ami-9"\n\n'
        "# about the rules\n# [[security-group.ingress]]\n# Port = 22\n[x]\nA = 1\n"
    )
    uncommented = text.replace("# [[", "[[").replace("# Port", "Port")
    assert tomlkit.parse(uncommented)["launch-template"]["ImageId"] == "ami-9"


def test_update_adds_the_key_to_a_table_that_has_only_comments(service: Service) -> None:
    write(service, '[launch-template]\n# InstanceType = "t3.small"\n')
    conf.update(service, Conf(), f"{TEMPLATE}.ImageId", "ami-9")
    assert read(service) == '[launch-template]\nImageId = "ami-9"\n# InstanceType = "t3.small"\n'


def test_update_adds_the_key_next_to_a_dotted_key(service: Service) -> None:
    """ドット付きキーで書かれたテーブルには、同じ形で足す。読み直しても同じ内容になる。"""
    write(service, 'launch-template.InstanceType = "t3.small"\n')
    assert conf.update(service, Conf(), f"{TEMPLATE}.SubnetId", "subnet-1") is True
    assert read(service) == (
        'launch-template.InstanceType = "t3.small"\nlaunch-template.SubnetId = "subnet-1"\n'
    )
    assert conf.load(service).launch_template == LaunchTemplate(
        InstanceType="t3.small", SubnetId="subnet-1"
    )


def test_update_puts_a_top_level_key_before_the_tables(service: Service) -> None:
    """テーブルの後に足すと、そのテーブルの項目になってしまう。"""
    write(service, 'AwsProfile = "work"\n\n[launch-template]\nInstanceType = "t3.micro"\n')
    conf.update(service, Conf(), "AwsRegion", "us-east-1")
    assert read(service) == (
        'AwsProfile = "work"\nAwsRegion = "us-east-1"\n'
        '\n[launch-template]\nInstanceType = "t3.micro"\n'
    )
    assert conf.load(service).AwsRegion == "us-east-1"


def test_update_adds_a_newline_when_the_file_lacks_one(service: Service) -> None:
    write(service, 'AwsProfile = "work"')
    conf.update(service, Conf(), "AwsRegion", "us-east-1")
    assert read(service) == 'AwsProfile = "work"\nAwsRegion = "us-east-1"\n'


def test_update_replaces_the_value(service: Service) -> None:
    write(service, '# mine\nAwsProfile = "a"\nAwsRegion = "us-east-1"\n')
    assert conf.update(service, Conf(), "AwsProfile", "b") is True
    assert read(service) == '# mine\nAwsProfile = "b"\nAwsRegion = "us-east-1"\n'


def test_update_keeps_the_order_and_the_comments(service: Service) -> None:
    write(service, '# head\nAwsRegion = "us-east-1"  # trailing\n# middle\nAwsProfile = "work"\n')
    conf.update(service, Conf(), "AwsProfile", "other")
    assert read(service) == (
        '# head\nAwsRegion = "us-east-1"  # trailing\n# middle\nAwsProfile = "other"\n'
    )


def test_update_keeps_the_file_when_the_value_is_the_same(service: Service) -> None:
    """利用者が書いた書式をそのまま残す。"""
    write(service, '# mine\nAwsProfile   =   "work"\n')
    values = Conf()
    assert conf.update(service, values, "AwsProfile", "work") is False
    assert read(service) == '# mine\nAwsProfile   =   "work"\n'
    assert values.AwsProfile == "work"


def test_update_keeps_the_file_when_the_table_value_is_the_same(service: Service) -> None:
    write(service, '[launch-template]\nInstanceType   =   "t3.micro"\n')
    assert conf.update(service, Conf(), f"{TEMPLATE}.InstanceType", "t3.micro") is False
    assert read(service) == '[launch-template]\nInstanceType   =   "t3.micro"\n'


def test_update_changes_nothing_the_second_time(service: Service) -> None:
    conf.update(service, Conf(), f"{TEMPLATE}.InstanceType", "t3.micro")
    assert conf.update(service, Conf(), f"{TEMPLATE}.InstanceType", "t3.micro") is False


def test_update_replaces_an_empty_value(service: Service) -> None:
    write(service, 'AwsProfile = ""\n')
    assert conf.update(service, Conf(), "AwsProfile", "work") is True
    assert conf.load(service).AwsProfile == "work"


def test_update_writes_a_value_that_needs_quoting(service: Service) -> None:
    conf.update(service, Conf(), "AwsProfile", "a=b # c")
    assert conf.load(service).AwsProfile == "a=b # c"


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
    assert str(caught.value) == (f"{service.conf_path}: security-group.ingress[0].Port {message}")


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
    assert str(caught.value) == f"{service.conf_path}: security-group.ingress[0].Cidr {message}"


@pytest.mark.parametrize("missing", ["Port", "Cidr"])
def test_a_rule_without_a_port_or_a_cidr(service: Service, missing: str) -> None:
    """Description だけが省ける。"""
    written = {"Port": "22", "Cidr": '"203.0.113.10/32"'}
    del written[missing]
    write(service, INGRESS + "".join(f"{key} = {value}\n" for key, value in written.items()))
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{service.conf_path}: security-group.ingress[0].{missing} is missing"
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
        f"{service.conf_path}: security-group.ingress[0].Description {message}"
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
        f"{service.conf_path}: security-group.ingress[1] repeats the protocol, port and CIDR "
        "of ingress[0]"
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
        f"{service.conf_path}: security-group.ingress[0].Port must not be given with Protocol icmp"
    )


def test_a_udp_rule_without_a_port(service: Service) -> None:
    write(service, INGRESS + 'Protocol = "udp"\nCidr = "203.0.113.10/32"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{service.conf_path}: security-group.ingress[0].Port is missing"


def test_an_unknown_protocol(service: Service) -> None:
    write(service, INGRESS + 'Protocol = "TCP"\nPort = 22\nCidr = "203.0.113.10/32"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{service.conf_path}: security-group.ingress[0].Protocol must be one of tcp, udp, icmp"
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
    '[[role.policy.jupyter]]\nAction = "s3:*"\nResource = "*"\n'
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
    role = RULES[RULES.index("[[role.policy.jupyter]]") :]
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
POLICY = "[role.policy]\n"


@pytest.mark.parametrize(
    ("before", "expected"),
    [
        pytest.param(LAUNCH + "\n[role]\n\n", LAUNCH + "\n[role]\n" + AFTER, id="empty table"),
        pytest.param("[launch-template]\n\n", "[launch-template]\n" + AFTER, id="empty first"),
        pytest.param(
            '[[role.policy.x]]\nEffect = "Allow"\n\n',
            '[[role.policy.x]]\nEffect = "Allow"\n' + AFTER,
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


STATEMENT = "[[role.policy.jupyter]]\n"


def test_reads_the_role_policies_as_they_are(service: Service) -> None:
    """テーブルの中身は IAM の書式のまま持ち、変数も Effect も補わない。名前は書いた順に並べる。"""
    write(
        service,
        STATEMENT
        + 'Action = ["s3:GetObject", "s3:PutObject"]\n'
        + 'Resource = "arn:aws:s3:::bucket/${temt:name}/*"\n'
        + '[role.policy.jupyter.Condition.Bool]\n"aws:SecureTransport" = true\n'
        + STATEMENT
        + 'Sid = "Two"\nEffect = "Deny"\nNotAction = "iam:*"\nNotResource = "*"\n'
        + '[[role.policy.idle]]\nEffect = "Allow"\nAction = "ec2:Describe*"\n'
        + 'Resource = "*"\n',
    )
    policies = conf.load(service).role.policy
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
    assert conf.load(service, missing_ok=True).role.policy == {}
    write(service, "[role]\n")
    assert conf.load(service).role.policy == {}
    write(service, "[role.policy]\n")
    assert conf.load(service).role.policy == {}


def test_a_statement_key_that_iam_does_not_know_is_left_to_iam(service: Service) -> None:
    """書式は apply が ValidatePolicy で確かめるので、読むときには止めない。"""
    write(service, STATEMENT + 'Resources = "*"\n')
    assert conf.load(service).role.policy == {"jupyter": [{"Resources": "*"}]}


def test_a_statement_with_an_unknown_variable(service: Service) -> None:
    write(service, STATEMENT + 'Resource = "${temt:app}/*"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{service.conf_path}: role.policy.jupyter[0] uses an unknown variable: ${{temt:app}}"
    )


@pytest.mark.parametrize(
    "written",
    [
        "Sid = 2026-09-27\n",
        'Action = ["s3:GetObject", 12:00:00]\n',
        '[role.policy.jupyter.Condition.DateLessThan]\n"aws:CurrentTime" = 2026-09-27T00:00:00Z\n',
        '[role.policy.jupyter.Condition.NumericLessThan]\n"s3:max-keys" = nan\n',
        '[role.policy.jupyter.Condition.NumericLessThan]\n"s3:max-keys" = inf\n',
    ],
)
def test_a_statement_with_what_json_does_not_have(service: Service, written: str) -> None:
    """TOML の日時、nan、inf は JSON に無い。配列やテーブルの中にあっても止める。"""
    write(service, STATEMENT + 'Effect = "Allow"\n' + written)
    with pytest.raises(
        TemtError,
        match=(
            r"role\.policy\.jupyter\[0\] must hold only strings, finite numbers, "
            r"booleans, arrays and tables$"
        ),
    ):
        conf.load(service)


# 角かっこ 1 つのテーブルと、中に階層を作ったテーブルは、どちらも [[...]] と書くよう告げる
NOT_AN_ARRAY = (
    r"role\.policy\.jupyter must be an array of tables; "
    r"write \[\[role\.policy\.jupyter\]\] once per statement$"
)


@pytest.mark.parametrize(
    ("written", "message"),
    [
        ('[role]\npolicy = "x"\n', r"role\.policy must be a table$"),
        ('[role.policy]\njupyter = "x"\n', r"role\.policy\.jupyter must be an array of tables$"),
        ("[role.policy.jupyter]\n", NOT_AN_ARRAY),
        ('[role.policy.jupyter]\nVersion = "2012-10-17"\n', NOT_AN_ARRAY),
        ('[[role.policy.jupyter.Statement]]\nEffect = "Allow"\n', NOT_AN_ARRAY),
        ("[role.policy]\njupyter = []\n", r"role\.policy\.jupyter must have at least one table$"),
        ('[role.policy]\njupyter = ["Allow"]\n', r"role\.policy\.jupyter\[0\] must be a table$"),
    ],
)
def test_a_policy_in_a_wrong_shape(service: Service, written: str, message: str) -> None:
    write(service, written)
    with pytest.raises(TemtError, match=message):
        conf.load(service)


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("Jupyter", '"Jupyter"'),
        ("Statement", '"Statement"'),
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
    write(service, f'[[role.policy."{name}"]]\nEffect = "Allow"\n')
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == (
        f"{service.conf_path}: role.policy.{shown}: the policy name {LABEL_RULE}"
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
    assert list(conf.load(service).role.policy) == ["example"]


def test_an_unknown_key_in_the_role_table(service: Service) -> None:
    write(service, '[role]\nStatement = "x"\n')
    with pytest.raises(TemtError, match=r"unknown key: role\.Statement$"):
        conf.load(service)


# [[role.policy.ec2]] の既定


def test_the_ec2_default_is_two_statements_without_effect(service: Service) -> None:
    """既定は、SSM エージェントの権限と、自分の階層のパラメータの読み書き。Effect は省く。"""
    write(service, conf.EC2_DEFAULT)
    statements = conf.load(service).role.policy[conf.EC2]
    assert statements == conf.ec2_default()
    assert [list(one) for one in statements] == [["Action", "Resource"], ["Action", "Resource"]]
    assert "ssm:UpdateInstanceInformation" in statements[0]["Action"]
    assert statements[0]["Resource"] == "*"
    assert statements[1]["Resource"] == [
        "arn:aws:ssm:*:*:parameter/${temt:name}",
        "arn:aws:ssm:*:*:parameter/${temt:name}/*",
    ]


def test_the_ec2_default_is_written_when_it_is_missing(service: Service) -> None:
    """末尾に足し、Conf にも入れる。ほかのポリシーはそのまま残る。"""
    text = 'AwsRegion = "x"\n' + STATEMENT + 'Action = "s3:*"\nResource = "*"\n'
    write(service, text)
    values = conf.load(service)
    conf.write_ec2_default(service, values)
    assert read(service) == text + conf.EC2_DEFAULT
    assert list(values.role.policy) == ["jupyter", conf.EC2]
    assert values.role.policy[conf.EC2] == conf.ec2_default()
    assert conf.load(service) == values


def test_with_ec2_default_writes_nothing(service: Service) -> None:
    """足したあとの中身と、それを読んだ Conf を返すだけで、ファイルも Conf も変えない。

    diff が、apply が足せるかを確かめるのに使う。
    """
    text = 'AwsRegion = "x"\n'
    write(service, text)
    values = conf.load(service)
    added, done = conf.with_ec2_default(service, values)
    assert added == text + conf.EC2_DEFAULT
    assert done.role.policy == {conf.EC2: conf.ec2_default()}
    assert read(service) == text
    assert values.role.policy == {}


def test_the_ec2_default_gets_a_newline_before_it(service: Service) -> None:
    write(service, 'AwsRegion = "x"')
    values = conf.load(service)
    conf.write_ec2_default(service, values)
    assert read(service) == 'AwsRegion = "x"\n' + conf.EC2_DEFAULT


def test_the_ec2_default_cannot_be_added_to_an_inline_table(service: Service) -> None:
    """インラインテーブルの role には [[role.policy.ec2]] を足せない。書かずに止める。"""
    text = "role = { policy = {} }\n"
    write(service, text)
    values = conf.load(service)
    with pytest.raises(
        TemtError, match=r"cannot add the default \[\[role\.policy\.ec2\]\] to .*; add it by hand"
    ):
        conf.write_ec2_default(service, values)
    assert read(service) == text
    assert values.role.policy == {}


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


def test_assign_changes_only_the_conf(service: Service) -> None:
    """設定ファイルには書かない。diff が、apply が書く値を後の手順で使うのに使う。"""
    text = 'AwsProfile = "mine"\n'
    write(service, text)
    values = conf.load(service)
    conf.assign(values, "launch-template.SubnetId", "subnet-0")
    conf.assign(values, "AwsProfile", "other")
    assert values.launch_template.SubnetId == "subnet-0"
    assert values.AwsProfile == "other"
    assert read(service) == text


# --- 書き換えの置き換え ---

# 書き換える前の設定ファイル
BEFORE = 'AwsProfile = "work"\n'
# 設定ファイルを書き換える関数。どれも replace_text で置き換える
WRITERS = {
    "update": lambda service, values: conf.update(service, values, "AwsProfile", "other"),
    "ec2 default": lambda service, values: conf.write_ec2_default(service, values),
    "ingress": lambda service, values: conf.edit_ingress(service, values, added=[HOME]),
    "example": lambda service, values: conf.write_example(service),
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


def test_update_makes_a_new_file_with_the_usual_mode(service: Service) -> None:
    """無いファイルは、umask に従うふつうのモードで作る。"""
    before = os.umask(0o022)
    try:
        conf.update(service, Conf(), "AwsRegion", "us-east-1")
    finally:
        os.umask(before)
    assert service.conf_path.stat().st_mode & 0o777 == 0o644


def write_defaults(text: str) -> None:
    """共通ファイルを用意する。"""
    defaults_path().parent.mkdir(parents=True, exist_ok=True)
    defaults_path().write_text(text, encoding="utf-8")


EC2_COMMON = '[[role.policy.ec2]]\nAction = ["s3:ListBucket"]\nResource = "*"\n'
RULE = '[[security-group.ingress]]\nPort = 22\nCidr = "{}"\n'


def test_the_common_file_fills_what_the_service_does_not_write(service: Service) -> None:
    write_defaults(
        'AwsProfile = "work"\nAwsRegion = "us-east-1"\nSshdPorts = [2222]\n\n'
        '[launch-template]\nInstanceType = "t3.small"\nVolumeSize = 16\n'
    )
    write(service, 'AwsRegion = "eu-west-1"\n\n[launch-template]\nSubnetId = "subnet-1"\n')
    assert conf.load(service) == Conf(
        AwsProfile="work",
        AwsRegion="eu-west-1",
        SshdPorts=[2222],
        launch_template=LaunchTemplate(InstanceType="t3.small", SubnetId="subnet-1", VolumeSize=16),
    )


def test_a_blank_value_of_the_service_leaves_the_common_one(service: Service) -> None:
    """空の値は書かれていないものなので、共通ファイルの値が効く。"""
    write_defaults('AwsProfile = "work"\nSshdPorts = [2222]\n')
    write(service, 'AwsProfile = ""\nSshdPorts = []\n')
    loaded = conf.load(service)
    assert (loaded.AwsProfile, loaded.SshdPorts) == ("work", [2222])


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
        f"{service.conf_path}: security-group.ingress[1] repeats a rule in {defaults_path()}; "
        f"remove one of them"
    )


def test_policies_are_joined_by_name(service: Service) -> None:
    """同じ名前のポリシーは、サービスの設定ファイルのものを使う。"""
    write_defaults(
        '[[role.policy.ec2]]\nAction = ["s3:ListBucket"]\nResource = "*"\n\n'
        '[[role.policy.logs]]\nAction = ["logs:PutLogEvents"]\nResource = "*"\n'
    )
    write(service, '[[role.policy.ec2]]\nAction = ["ec2:DescribeTags"]\nResource = "*"\n')
    assert conf.load(service).role.policy == {
        "ec2": [{"Action": ["ec2:DescribeTags"], "Resource": "*"}],
        "logs": [{"Action": ["logs:PutLogEvents"], "Resource": "*"}],
    }


def test_an_error_in_the_common_file_names_it(service: Service) -> None:
    write_defaults("[launch-template]\nInstanceType = 3\n")
    write(service, "")
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{defaults_path()}: launch-template.InstanceType must be a string"


def test_without_a_config_file_the_common_file_still_counts(service: Service) -> None:
    """apply は、まだ無い設定ファイルを共通ファイルと既定から始める。"""
    write_defaults('[launch-template]\nInstanceType = "t3.small"\n')
    assert conf.load(service, missing_ok=True).launch_template.InstanceType == "t3.small"
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


def test_the_default_ec2_policy_is_added_beside_common_policies(service: Service) -> None:
    write_defaults('[[role.policy.logs]]\nAction = ["logs:PutLogEvents"]\nResource = "*"\n')
    write(service, 'AwsRegion = "us-east-1"\n')
    values = conf.load(service)
    conf.write_ec2_default(service, values)
    assert set(values.role.policy) == {"logs", conf.EC2}
    assert "role.policy.logs" not in read(service)


def test_a_broken_symlink_as_the_common_file_is_an_error(service: Service) -> None:
    """無いものとすると、環境変数のプロファイルへ黙って移ってしまう。"""
    defaults_path().parent.mkdir(parents=True, exist_ok=True)
    defaults_path().symlink_to(defaults_path().parent / "gone.toml")
    write(service, "")
    with pytest.raises(TemtError) as caught:
        conf.load(service)
    assert str(caught.value) == f"{defaults_path()}: broken symlink"
    with pytest.raises(TemtError, match="broken symlink"):
        conf.common_values()


def test_source_names_the_file_that_holds_the_value(service: Service) -> None:
    key = conf.launch_template_key("VolumeSize")
    write_defaults("[launch-template]\nVolumeSize = 16\n")
    write(service, 'AwsProfile = ""\n')
    assert conf.source(service, key) == defaults_path()
    assert conf.source(service, "AwsProfile") == service.conf_path
    write(service, "[launch-template]\nVolumeSize = 32\n")
    assert conf.source(service, key) == service.conf_path


def test_the_common_ec2_policy_keeps_apply_from_adding_the_default(service: Service) -> None:
    write_defaults(EC2_COMMON)
    write(service, 'AwsRegion = "us-east-1"\n')
    assert conf.EC2 in conf.load(service).role.policy
