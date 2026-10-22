"""起動テンプレートの中身（ltdata）のテスト。どれも AWS を呼ばない。"""

import base64
import datetime
import math
from typing import Any

import pytest
import tomlkit

from temt import ltdata

# 誤りに添える位置の始まり。設定ファイルのテーブル名
WHERE = "launch-template"
# 時刻の例
WHEN = datetime.datetime(2026, 10, 18, tzinfo=datetime.UTC)


def encoded(text: str) -> str:
    """UserData に書く base64。"""
    return base64.b64encode(text.encode()).decode()


# --- check ---


def test_check_returns_what_is_written() -> None:
    """形に合う中身は、そのまま返す。小数の項目には整数も書ける。"""
    data = {
        "ImageId": "${temt:name}-2610101230",
        "EbsOptimized": False,
        "IamInstanceProfile": {"Name": "${temt:name}"},
        "NetworkInterfaces": [
            {"DeviceIndex": 0, "SubnetId": "subnet-1", "Groups": ["sg-1", "${temt:name}"]}
        ],
        "BlockDeviceMappings": [{"Ebs": {"VolumeSize": 30, "Encrypted": True}}],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": "${temt:service}"}]}
        ],
        "InstanceRequirements": {"MemoryGiBPerVCpu": {"Min": 1, "Max": 2.5}},
        "InstanceMarketOptions": {"SpotOptions": {"ValidUntil": WHEN}},
    }
    assert ltdata.check(data, WHERE) == data


def test_check_drops_blank_values() -> None:
    """空文字列、空の配列、空のテーブルを落とす。落として空になったものも落とす。"""
    data = {
        "ImageId": "",
        "SecurityGroupIds": ["", "sg-1"],
        "SecurityGroups": [],
        "MetadataOptions": {"HttpTokens": ""},
        "Monitoring": {},
        "NetworkInterfaces": [{}, {"DeviceIndex": 0, "Groups": [], "SubnetId": ""}],
        "TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": ""}]}],
    }
    assert ltdata.check(data, WHERE) == {
        "SecurityGroupIds": ["sg-1"],
        "NetworkInterfaces": [{"DeviceIndex": 0}],
        "TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": "Env"}]}],
    }
    assert ltdata.check({"ImageId": "", "Placement": {"Tenancy": ""}}, WHERE) == {}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        pytest.param({"Foo": "x"}, "unknown key: launch-template.Foo", id="unknown key"),
        pytest.param({"\x1b[2J": "x"}, "unknown key: launch-template.?[2J", id="control character"),
        pytest.param(
            {"NetworkInterfaces": [{"DeviceIndex": 0}, {"DeviceIndex": 1, "SubnetIds": "s"}]},
            "unknown key: launch-template.NetworkInterfaces[1].SubnetIds",
            id="unknown key in the second element",
        ),
        pytest.param(
            {"MetadataOptions": "required"},
            "launch-template.MetadataOptions must be a table",
            id="structure",
        ),
        pytest.param(
            {"SecurityGroupIds": "sg-1"},
            "launch-template.SecurityGroupIds must be an array",
            id="list",
        ),
        pytest.param(
            {"NetworkInterfaces": ["eni-1"]},
            "launch-template.NetworkInterfaces[0] must be a table",
            id="element of a list",
        ),
        pytest.param(
            {"SecurityGroupIds": ["sg-1", 2]},
            "launch-template.SecurityGroupIds[1] must be a string",
            id="string in a list",
        ),
        pytest.param({"KeyName": 1}, "launch-template.KeyName must be a string", id="string"),
        pytest.param(
            {"KeyName": "${temt:app}"},
            "launch-template.KeyName uses an unknown variable: ${temt:app}",
            id="unknown variable",
        ),
        pytest.param(
            {"KeyName": "${temt:\x1b}"},
            "launch-template.KeyName uses an unknown variable: ${temt:?}",
            id="unknown variable with a control character",
        ),
        pytest.param(
            {"NetworkInterfaces": [{"DeviceIndex": True}]},
            "launch-template.NetworkInterfaces[0].DeviceIndex must be an integer",
            id="boolean as an integer",
        ),
        pytest.param(
            {"NetworkInterfaces": [{"DeviceIndex": 0.0}]},
            "launch-template.NetworkInterfaces[0].DeviceIndex must be an integer",
            id="fraction as an integer",
        ),
        pytest.param(
            {"NetworkInterfaces": [{"DeviceIndex": "0"}]},
            "launch-template.NetworkInterfaces[0].DeviceIndex must be an integer",
            id="string as an integer",
        ),
        pytest.param(
            {"EbsOptimized": 1},
            "launch-template.EbsOptimized must be true or false",
            id="integer as a boolean",
        ),
        pytest.param(
            {"EbsOptimized": "true"},
            "launch-template.EbsOptimized must be true or false",
            id="string as a boolean",
        ),
        pytest.param(
            {"InstanceRequirements": {"MemoryGiBPerVCpu": {"Min": True}}},
            "launch-template.InstanceRequirements.MemoryGiBPerVCpu.Min must be a number",
            id="boolean as a number",
        ),
        pytest.param(
            {"InstanceRequirements": {"MemoryGiBPerVCpu": {"Min": math.nan}}},
            "launch-template.InstanceRequirements.MemoryGiBPerVCpu.Min must be a number",
            id="nan",
        ),
        pytest.param(
            {"InstanceRequirements": {"MemoryGiBPerVCpu": {"Max": math.inf}}},
            "launch-template.InstanceRequirements.MemoryGiBPerVCpu.Max must be a number",
            id="inf",
        ),
        pytest.param(
            {"InstanceMarketOptions": {"SpotOptions": {"ValidUntil": WHEN.date()}}},
            "launch-template.InstanceMarketOptions.SpotOptions.ValidUntil must be a date-time",
            id="date",
        ),
        pytest.param(
            {"InstanceMarketOptions": {"SpotOptions": {"ValidUntil": "2026-10-18T00:00:00Z"}}},
            "launch-template.InstanceMarketOptions.SpotOptions.ValidUntil must be a date-time",
            id="string as a date-time",
        ),
    ],
)
def test_check_names_the_position(data: dict[str, Any], message: str) -> None:
    """配列の要素は、落とす前の番号で指す。ファイルに書いた順と同じになる。"""
    with pytest.raises(ValueError) as caught:
        ltdata.check(data, WHERE)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("data", "position"),
    [
        pytest.param({"MetadataOptions": {"State": "applied"}}, "MetadataOptions.State"),
        pytest.param({"Operator": {"Managed": True}}, "Operator.Managed"),
        pytest.param({"Operator": {"HiddenByDefault": False}}, "Operator.HiddenByDefault"),
        pytest.param(
            {
                "NetworkInterfaces": [
                    {
                        "DeviceIndex": 0,
                        "Ipv6Addresses": [{"Ipv6Address": "::1", "IsPrimaryIpv6": True}],
                    }
                ]
            },
            "NetworkInterfaces[0].Ipv6Addresses[0].IsPrimaryIpv6",
        ),
    ],
)
def test_the_read_only_items_cannot_be_written(data: dict[str, Any], position: str) -> None:
    """読み出したときにだけある 4 つの項目は、作るときの形に無い。"""
    with pytest.raises(ValueError) as caught:
        ltdata.check(data, WHERE)
    assert str(caught.value) == f"unknown key: launch-template.{position}"


def test_every_read_only_item_is_dropped_on_reading_and_cannot_be_written() -> None:
    """READ_ONLY の位置は、どれも template show で落とし、作るときの形には無い。"""
    for path in ltdata.READ_ONLY:
        data: Any = True
        for name in reversed(path):
            data = {name: [data] if name in ("NetworkInterfaces", "Ipv6Addresses") else data}
        assert ltdata.readable(data) != data
        with pytest.raises(ValueError, match="^unknown key: "):
            ltdata.check(data, WHERE)


# --- check_keys ---


def test_check_keys_passes_distinct_keys() -> None:
    """タグの Key は、ResourceType の要素ごとに見る。DeviceName は 1 つの要素でだけ省ける。"""
    ltdata.check_keys(
        {
            "NetworkInterfaces": [{"DeviceIndex": 0}, {"DeviceIndex": 1}],
            "BlockDeviceMappings": [{"Ebs": {"VolumeSize": 30}}, {"DeviceName": "/dev/sdb"}],
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": [{"Key": "Env"}, {"Key": "Owner"}]},
                {"ResourceType": "volume", "Tags": [{"Key": "Env"}]},
            ],
            "SecurityGroupIds": ["sg-1", "sg-1"],
        },
        WHERE,
    )


@pytest.mark.parametrize(
    ("data", "message"),
    [
        pytest.param(
            {"NetworkInterfaces": [{"DeviceIndex": 0}, {"DeviceIndex": 0}]},
            "launch-template.NetworkInterfaces has two elements with DeviceIndex = 0",
            id="DeviceIndex",
        ),
        pytest.param(
            {"BlockDeviceMappings": [{"DeviceName": "/dev/sda1"}, {"DeviceName": "/dev/sda1"}]},
            'launch-template.BlockDeviceMappings has two elements with DeviceName = "/dev/sda1"',
            id="DeviceName",
        ),
        pytest.param(
            {"BlockDeviceMappings": [{"Ebs": {"VolumeSize": 30}}, {"Ebs": {"VolumeSize": 40}}]},
            "launch-template.BlockDeviceMappings has two or more elements without DeviceName",
            id="two without DeviceName",
        ),
        pytest.param(
            {"TagSpecifications": [{"ResourceType": "instance"}, {"ResourceType": "instance"}]},
            'launch-template.TagSpecifications has two elements with ResourceType = "instance"',
            id="ResourceType",
        ),
        pytest.param(
            {
                "TagSpecifications": [
                    {"ResourceType": "volume", "Tags": [{"Key": "Env"}]},
                    {"ResourceType": "instance", "Tags": [{"Key": "Env"}, {"Key": "Env"}]},
                ]
            },
            'launch-template.TagSpecifications[1].Tags has two elements with Key = "Env"',
            id="tag Key",
        ),
    ],
)
def test_check_keys_rejects_repeated_keys(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        ltdata.check_keys(data, WHERE)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"NetworkInterfaces": [{"SubnetId": "subnet-1"}]}, id="DeviceIndex"),
        pytest.param(
            {"NetworkInterfaces": [{"SubnetId": "subnet-1"}, {"SubnetId": "subnet-2"}]},
            id="two without DeviceIndex",
        ),
        pytest.param(
            {"TagSpecifications": [{"Tags": [{"Key": "Env"}]}, {"Tags": [{"Key": "Env"}]}]},
            id="ResourceType",
        ),
        pytest.param(
            {"TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Value": "x"}]}]},
            id="Key",
        ),
        pytest.param({"BlockDeviceMappings": [{"Ebs": {"VolumeSize": 30}}]}, id="one DeviceName"),
    ],
)
def test_check_keys_leaves_an_element_without_its_key_to_aws(data: dict[str, Any]) -> None:
    """キーの無い要素は、AWS が断るので誤りにしない。BlockDeviceMappings の DeviceName
    だけは、2 つ以上で省くとどちらもルートボリュームを指すので誤りにする（上）。
    """
    ltdata.check_keys(data, WHERE)


# --- without_blanks ---


def test_without_blanks_drops_blanks_at_every_depth() -> None:
    """形を確かめないので、知らない項目もそのまま残す。"""
    assert ltdata.without_blanks(
        {
            "ImageId": "",
            "Foo": {"Bar": [], "Baz": 0},
            "NetworkInterfaces": [{}, {"Groups": [""]}, {"DeviceIndex": 0, "SubnetId": ""}],
            "EbsOptimized": False,
        }
    ) == {"Foo": {"Baz": 0}, "NetworkInterfaces": [{"DeviceIndex": 0}], "EbsOptimized": False}
    assert ltdata.without_blanks("x") == "x"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", True), ([], True), ({}, True), (0, False), (False, False), (" ", False), ([""], False)],
)
def test_blank(value: Any, expected: bool) -> None:
    """0 と false は値である。"""
    assert ltdata.blank(value) is expected


def test_the_empty_string_of_no_device_is_a_value() -> None:
    """NoDevice の値は空文字列だけで、AMI のデバイスを外す。ほかの項目の空文字列は空の値。"""
    assert ltdata.blank("", "NoDevice") is False
    assert ltdata.blank("", "DeviceName") is True
    assert ltdata.blank([], "NoDevice") is True


# AMI のデバイス /dev/sdb を外す BlockDeviceMappings
NO_DEVICE = {"BlockDeviceMappings": [{"DeviceName": "/dev/sdb", "NoDevice": ""}]}


def test_no_device_is_kept_everywhere() -> None:
    """確かめるときも、共通ファイルと重ねる前も、比べる前も落とさない。"""
    assert ltdata.check(NO_DEVICE, WHERE) == NO_DEVICE
    assert ltdata.without_blanks(NO_DEVICE) == NO_DEVICE
    assert ltdata.normalize(NO_DEVICE) == NO_DEVICE
    assert not ltdata.same(
        ltdata.normalize(NO_DEVICE),
        ltdata.normalize({"BlockDeviceMappings": [{"DeviceName": "/dev/sdb"}]}),
    )
    assert ltdata.leaves(ltdata.normalize(NO_DEVICE)) == {
        "BlockDeviceMappings[DeviceName=/dev/sdb].NoDevice": ""
    }


# --- merge ---


def test_merge_tables_key_by_key() -> None:
    """両方にある項目は、設定ファイル（over）の値にする。"""
    base = {
        "InstanceType": "t3.small",
        "KeyName": "common",
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
    }
    over = {"KeyName": "mine", "MetadataOptions": {"HttpPutResponseHopLimit": 1}}
    assert ltdata.merge(base, over) == {
        "InstanceType": "t3.small",
        "KeyName": "mine",
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
    }


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("NetworkInterfaces", "DeviceIndex"),
        ("BlockDeviceMappings", "DeviceName"),
        ("TagSpecifications", "ResourceType"),
    ],
)
def test_merge_arrays_element_by_element(name: str, key: str) -> None:
    """同じキーの要素は項目ごとに重ね、違うキーの要素は両方を並べる。共通ファイル（base）の
    要素が先。
    """
    base = {name: [{key: "a", "X": 1, "Y": 1}, {key: "b", "X": 1}]}
    over = {name: [{key: "c", "X": 3}, {key: "a", "Y": 2}]}
    assert ltdata.merge(base, over) == {
        name: [{key: "a", "X": 1, "Y": 2}, {key: "b", "X": 1}, {key: "c", "X": 3}]
    }


def test_merge_tags_inside_the_resource_types() -> None:
    base = {
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "Owner", "Value": "me"}]},
        ]
    }
    over = {
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Env", "Value": "dev"}, {"Key": "Owner", "Value": "you"}],
            },
            {"ResourceType": "volume", "Tags": [{"Key": "Env", "Value": "dev"}]},
        ]
    }
    assert ltdata.merge(base, over) == {
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Owner", "Value": "you"}, {"Key": "Env", "Value": "dev"}],
            },
            {"ResourceType": "volume", "Tags": [{"Key": "Env", "Value": "dev"}]},
        ]
    }


def test_merge_elements_without_the_device_name_as_one() -> None:
    """DeviceName を省いた要素どうしは、どちらもルートボリュームを指す。"""
    base = {"BlockDeviceMappings": [{"Ebs": {"VolumeSize": 16, "VolumeType": "gp3"}}]}
    over = {"BlockDeviceMappings": [{"Ebs": {"VolumeSize": 32}}, {"DeviceName": "/dev/sdb"}]}
    assert ltdata.merge(base, over) == {
        "BlockDeviceMappings": [
            {"Ebs": {"VolumeSize": 32, "VolumeType": "gp3"}},
            {"DeviceName": "/dev/sdb"},
        ]
    }


def test_merge_replaces_other_arrays_whole() -> None:
    """要素を対応づけるキーの無い配列（Groups など）は、丸ごと置き換える。"""
    base = {
        "SecurityGroupIds": ["sg-a", "sg-b"],
        "NetworkInterfaces": [{"DeviceIndex": 0, "Groups": ["sg-a", "sg-b"]}],
        "LicenseSpecifications": [{"LicenseConfigurationArn": "arn:a"}],
    }
    over = {
        "SecurityGroupIds": ["sg-c"],
        "NetworkInterfaces": [{"DeviceIndex": 0, "Groups": ["sg-c"]}],
        "LicenseSpecifications": [{"LicenseConfigurationArn": "arn:b"}],
    }
    assert ltdata.merge(base, over) == over


def test_merge_leaves_both_inputs_as_they_are() -> None:
    base = {"NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": "subnet-common"}]}
    over = {"NetworkInterfaces": [{"DeviceIndex": 0, "AssociatePublicIpAddress": False}]}
    kept = (repr(base), repr(over))
    merged = ltdata.merge(base, over)
    assert merged == {
        "NetworkInterfaces": [
            {"DeviceIndex": 0, "SubnetId": "subnet-common", "AssociatePublicIpAddress": False}
        ]
    }
    assert (repr(base), repr(over)) == kept


# --- substitute と profile_name ---


def test_substitute_replaces_the_variables_everywhere() -> None:
    data = {
        "ImageId": "${temt:name}-2610101230",
        "IamInstanceProfile": {"Name": "${temt:name}"},
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": "${temt:service}"}]}
        ],
        "EbsOptimized": True,
    }
    assert ltdata.substitute(data, {"name": "temt-web", "service": "web"}) == {
        "ImageId": "temt-web-2610101230",
        "IamInstanceProfile": {"Name": "temt-web"},
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": "web"}]}
        ],
        "EbsOptimized": True,
    }


@pytest.mark.parametrize(
    ("profile", "name"),
    [
        pytest.param({"Name": "temt-web"}, "temt-web", id="name"),
        pytest.param(
            {"Arn": "arn:aws:iam::123456789012:instance-profile/temt-web"}, "temt-web", id="arn"
        ),
        pytest.param(
            {"Arn": "arn:aws:iam::123456789012:instance-profile/team/temt-web"},
            "temt-web",
            id="arn with a path",
        ),
        pytest.param(
            {"Name": "temt-web", "Arn": "arn:aws:iam::123456789012:instance-profile/other"},
            "temt-web",
            id="name first",
        ),
        pytest.param(
            {"Name": "", "Arn": "arn:aws:iam::123456789012:instance-profile/temt-web"},
            "temt-web",
            id="empty name",
        ),
        pytest.param({}, None, id="neither"),
        pytest.param({"Name": "", "Arn": ""}, None, id="both empty"),
    ],
)
def test_profile_name(profile: dict[str, str], name: str | None) -> None:
    assert ltdata.profile_name(profile) == name


# --- normalize と same ---


def test_normalize_drops_blanks_and_read_only_items() -> None:
    """読み出したときにだけある項目を落として空になったものも落とす。"""
    data = {
        "ImageId": "ami-1",
        "KeyName": "",
        "SecurityGroupIds": [],
        "MetadataOptions": {"State": "applied", "HttpTokens": "required"},
        "Operator": {"Managed": False, "HiddenByDefault": False},
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "Ipv6Addresses": [{"IsPrimaryIpv6": True}],
                "Ipv6AddressCount": 1,
            }
        ],
    }
    assert ltdata.normalize(data) == {
        "ImageId": "ami-1",
        "MetadataOptions": {"HttpTokens": "required"},
        "NetworkInterfaces": [{"DeviceIndex": 0, "Ipv6AddressCount": 1}],
    }


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        pytest.param(
            {"Arn": "arn:aws:iam::123456789012:instance-profile/temt-web"},
            {"IamInstanceProfile": {"Name": "temt-web"}},
            id="arn",
        ),
        pytest.param(
            {"Name": "temt-web", "Arn": "arn:aws:iam::123456789012:instance-profile/temt-web"},
            {"IamInstanceProfile": {"Name": "temt-web"}},
            id="both",
        ),
        pytest.param({"Name": ""}, {}, id="blank"),
    ],
)
def test_normalize_names_the_instance_profile(
    profile: dict[str, str], expected: dict[str, Any]
) -> None:
    """AWS は名前で作っても ARN を返すことがある。"""
    assert ltdata.normalize({"IamInstanceProfile": profile}) == expected


def test_normalize_sorts_arrays() -> None:
    """対応づけるキーのある配列はそのキーで、ほかの配列は中身全体で並べる。"""
    data = {
        "SecurityGroupIds": ["sg-b", "sg-a"],
        "NetworkInterfaces": [
            {"DeviceIndex": 1, "Groups": ["sg-d", "sg-c"]},
            {"DeviceIndex": 0, "SubnetId": "subnet-z"},
        ],
        "BlockDeviceMappings": [{"DeviceName": "/dev/sdb"}, {"DeviceName": "/dev/sda1"}],
        "TagSpecifications": [
            {"ResourceType": "volume", "Tags": [{"Key": "Z", "Value": "a"}]},
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Z", "Value": "a"}, {"Key": "A", "Value": "z"}],
            },
        ],
        "LicenseSpecifications": [
            {"LicenseConfigurationArn": "arn:b"},
            {"LicenseConfigurationArn": "arn:a"},
        ],
    }
    assert ltdata.normalize(data) == {
        "SecurityGroupIds": ["sg-a", "sg-b"],
        "NetworkInterfaces": [
            {"DeviceIndex": 0, "SubnetId": "subnet-z"},
            {"DeviceIndex": 1, "Groups": ["sg-c", "sg-d"]},
        ],
        "BlockDeviceMappings": [{"DeviceName": "/dev/sda1"}, {"DeviceName": "/dev/sdb"}],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "A", "Value": "z"}, {"Key": "Z", "Value": "a"}],
            },
            {"ResourceType": "volume", "Tags": [{"Key": "Z", "Value": "a"}]},
        ],
        "LicenseSpecifications": [
            {"LicenseConfigurationArn": "arn:a"},
            {"LicenseConfigurationArn": "arn:b"},
        ],
    }


def test_what_aws_reads_back_is_the_same_as_the_config_file() -> None:
    """並び順、空の値、読み出しだけの項目、インスタンスプロファイルの書き方が違っても同じ。"""
    written = {
        "ImageId": "ami-1",
        "IamInstanceProfile": {"Name": "temt-web"},
        "SecurityGroupIds": [],
        "NetworkInterfaces": [
            {"DeviceIndex": 1, "SubnetId": "subnet-2"},
            {"DeviceIndex": 0, "SubnetId": "subnet-1", "Groups": ["sg-1", "sg-2"]},
        ],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": "dev"}]}
        ],
    }
    read = {
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": "dev"}]}
        ],
        "NetworkInterfaces": [
            {"DeviceIndex": 0, "Groups": ["sg-2", "sg-1"], "SubnetId": "subnet-1"},
            {"SubnetId": "subnet-2", "DeviceIndex": 1},
        ],
        "MetadataOptions": {"State": "applied"},
        "IamInstanceProfile": {"Arn": "arn:aws:iam::123456789012:instance-profile/temt-web"},
        "ImageId": "ami-1",
    }
    assert ltdata.same(ltdata.normalize(written), ltdata.normalize(read))
    changed = {**read, "ImageId": "ami-2"}
    assert not ltdata.same(ltdata.normalize(written), ltdata.normalize(changed))


def test_a_double_written_as_an_integer_is_the_same_as_aws_reads() -> None:
    """botocore は小数の項目を float で返す。設定ファイルの整数 1 と 1.0 は同じ値である。

    check が小数にして返すので、diff の表示も AWS から読んだものと同じ 1.0 になる。
    """
    written = ltdata.check({"InstanceRequirements": {"MemoryGiBPerVCpu": {"Min": 1}}}, WHERE)
    read = {"InstanceRequirements": {"MemoryGiBPerVCpu": {"Min": 1.0}}}
    assert ltdata.same(ltdata.normalize(written), ltdata.normalize(read))
    value = written["InstanceRequirements"]["MemoryGiBPerVCpu"]["Min"]
    assert type(value) is float
    assert ltdata.shown(value) == "1.0"


def test_an_integer_item_stays_an_integer() -> None:
    """小数にするのは小数の項目だけで、整数の項目はそのまま。"""
    written = ltdata.check({"NetworkInterfaces": [{"DeviceIndex": 0}]}, WHERE)
    assert type(written["NetworkInterfaces"][0]["DeviceIndex"]) is int


def test_same_tells_true_from_one() -> None:
    """Python では True == 1 だが、起動テンプレートの値としては違う。"""
    assert {"EbsOptimized": True} == {"EbsOptimized": 1}
    assert not ltdata.same({"EbsOptimized": True}, {"EbsOptimized": 1})
    assert not ltdata.same({"Ipv6AddressCount": 0}, {"Ipv6AddressCount": False})
    assert ltdata.same({"A": 1, "B": [True]}, {"B": [True], "A": 1})
    assert ltdata.same({"ValidUntil": WHEN}, {"ValidUntil": WHEN})


# --- leaves と shown ---


def test_leaves_name_the_positions() -> None:
    """テーブルは . でつなぎ、対応づけるキーのある配列の要素は [<キー>=<値>] で書く。

    要素の中のキーそのものは出さない。キーのほかに何も無い要素は、空のテーブルとする。
    """
    data = ltdata.normalize(
        {
            "ImageId": "ami-1",
            "MetadataOptions": {"HttpTokens": "required"},
            "NetworkInterfaces": [
                {"DeviceIndex": 0, "SubnetId": "subnet-1", "AssociatePublicIpAddress": True}
            ],
            "BlockDeviceMappings": [
                {"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30}},
                {"DeviceName": "/dev/sdb"},
            ],
        }
    )
    assert ltdata.leaves(data) == {
        "ImageId": "ami-1",
        "MetadataOptions.HttpTokens": "required",
        "NetworkInterfaces[DeviceIndex=0].SubnetId": "subnet-1",
        "NetworkInterfaces[DeviceIndex=0].AssociatePublicIpAddress": True,
        "BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize": 30,
        "BlockDeviceMappings[DeviceName=/dev/sdb]": {},
    }


def test_leaves_keep_other_arrays_as_one_value() -> None:
    """文字列の配列も、テーブルの配列も、1 つの値とする。表示は shown が決める。"""
    data = ltdata.normalize(
        {
            "SecurityGroupIds": ["sg-b", "sg-a"],
            "NetworkInterfaces": [
                {"DeviceIndex": 0, "Ipv6Addresses": [{"Ipv6Address": "2001:db8::1"}]}
            ],
        }
    )
    found = ltdata.leaves(data)
    assert found == {
        "SecurityGroupIds": ["sg-a", "sg-b"],
        "NetworkInterfaces[DeviceIndex=0].Ipv6Addresses": [{"Ipv6Address": "2001:db8::1"}],
    }
    assert ltdata.shown(found["SecurityGroupIds"]) == "sg-a,sg-b"
    assert ltdata.shown(found["NetworkInterfaces[DeviceIndex=0].Ipv6Addresses"]) == (
        '[{"Ipv6Address":"2001:db8::1"}]'
    )


def test_leaves_name_a_tag_by_its_key() -> None:
    """design/commands/service.md の diff の例のとおり、タグは Key で指し、値は Value。"""
    data = ltdata.normalize(
        {
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": [{"Key": "Env", "Value": "demo"}]}
            ]
        }
    )
    assert ltdata.leaves(data) == {"TagSpecifications[ResourceType=instance].Tags[Key=Env]": "demo"}


def test_leaves_of_a_tag_without_a_value() -> None:
    """Value の無いタグは、ほかの要素と同じく空のテーブルとする。"""
    data = ltdata.normalize(
        {"TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": "Env"}]}]}
    )
    assert ltdata.leaves(data) == {"TagSpecifications[ResourceType=instance].Tags[Key=Env]": {}}


@pytest.mark.parametrize(
    ("value", "text"),
    [
        pytest.param(None, "-", id="absent"),
        pytest.param("subnet-1", "subnet-1", id="string"),
        pytest.param("", '""', id="empty string"),
        pytest.param(True, "true", id="true"),
        pytest.param(False, "false", id="false"),
        pytest.param(0, "0", id="zero"),
        pytest.param(2.5, "2.5", id="fraction"),
        pytest.param(["sg-a", "sg-b"], "sg-a,sg-b", id="strings"),
        pytest.param([1, 2], "[1,2]", id="integers"),
        pytest.param([{"b": 1, "a": "x"}], '[{"a":"x","b":1}]', id="tables"),
        pytest.param({}, "{}", id="empty table"),
        pytest.param(WHEN, "2026-10-18T00:00:00+00:00", id="date-time"),
    ],
)
def test_shown(value: Any, text: str) -> None:
    """文字列と数はそのまま、文字列の配列は , でつなぎ、日時は ISO 8601 の形で引用符を
    付けず、ほかは 1 行の JSON にする。

    空文字列（NoDevice の値）は、何も見えない表示にせず "" とする。
    """
    assert ltdata.shown(value) == text


# --- UserData ---


def test_user_data_lines_decode_the_text() -> None:
    assert ltdata.user_data_lines(encoded("#!/bin/sh\necho hi\n")) == ["#!/bin/sh", "echo hi"]
    assert ltdata.user_data_lines("") == []


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("#!/bin/sh", id="not base64"),
        pytest.param(base64.b64encode(b"\xff\xfe").decode(), id="not UTF-8"),
        pytest.param(None, id="absent"),
        pytest.param(1, id="not a string"),
    ],
)
def test_user_data_lines_of_what_is_not_text(value: Any) -> None:
    assert ltdata.user_data_lines(value) is None


@pytest.mark.parametrize(
    ("value", "size"),
    [
        pytest.param(encoded("#!/bin/sh\n"), 10, id="base64"),
        pytest.param(base64.b64encode(b"\xff\xfe").decode(), 2, id="binary"),
        pytest.param("not base64!", 11, id="not base64"),
        pytest.param(None, 0, id="absent"),
    ],
)
def test_user_data_size(value: Any, size: int) -> None:
    """base64 として読めなければ、書かれた文字数とする。"""
    assert ltdata.user_data_size(value) == size


# --- readable と render ---


def test_readable_drops_only_the_read_only_items() -> None:
    """空の値も含め、ほかは読み出したまま残す。元の中身は変えない。"""
    data = {
        "ImageId": "ami-1",
        "SecurityGroupIds": [],
        "MetadataOptions": {"State": "applied", "HttpTokens": "required"},
        "Operator": {"Managed": True, "HiddenByDefault": False, "Principal": "x"},
        "NetworkInterfaces": [
            {"DeviceIndex": 0, "Ipv6Addresses": [{"Ipv6Address": "::1", "IsPrimaryIpv6": True}]}
        ],
    }
    kept = repr(data)
    assert ltdata.readable(data) == {
        "ImageId": "ami-1",
        "SecurityGroupIds": [],
        "MetadataOptions": {"HttpTokens": "required"},
        "Operator": {"Principal": "x"},
        "NetworkInterfaces": [{"DeviceIndex": 0, "Ipv6Addresses": [{"Ipv6Address": "::1"}]}],
    }
    assert repr(data) == kept


# template show が見せる中身の例
SHOWN = {
    "ImageId": "ami-0123456789abcdef0",
    "InstanceType": "t3.micro",
    "IamInstanceProfile": {"Name": "temt-web"},
    "NetworkInterfaces": [
        {
            "DeviceIndex": 0,
            "SubnetId": "subnet-1",
            "Groups": ["sg-1"],
            "AssociatePublicIpAddress": True,
        }
    ],
    "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30}}],
    "TagSpecifications": [
        {
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": "temt-web"}, {"Key": "temt:service", "Value": "web"}],
        }
    ],
    "MetadataOptions": {"HttpTokens": "required"},
    "UserData": encoded("#!/bin/sh\n"),
}


def test_render_writes_the_toml_of_the_config_file() -> None:
    """項目を先に、テーブルとテーブルの配列を後ろに書く。IamInstanceProfile とタグの並びは、
    apply が書くのと同じく、中かっこの内側に空白を置くインラインにする。
    """
    assert ltdata.render(SHOWN) == [
        "[launch-template]",
        'ImageId = "ami-0123456789abcdef0"',
        'InstanceType = "t3.micro"',
        'IamInstanceProfile = { Name = "temt-web" }',
        f'UserData = "{SHOWN["UserData"]}"',
        "",
        "[[launch-template.NetworkInterfaces]]",
        "DeviceIndex = 0",
        'SubnetId = "subnet-1"',
        'Groups = ["sg-1"]',
        "AssociatePublicIpAddress = true",
        "",
        "[[launch-template.BlockDeviceMappings]]",
        'DeviceName = "/dev/sda1"',
        "",
        "[launch-template.BlockDeviceMappings.Ebs]",
        "VolumeSize = 30",
        "",
        "[[launch-template.TagSpecifications]]",
        'ResourceType = "instance"',
        'Tags = [{ Key = "Name", Value = "temt-web" }, { Key = "temt:service", Value = "web" }]',
        "",
        "[launch-template.MetadataOptions]",
        'HttpTokens = "required"',
    ]


def test_render_can_be_copied_into_a_config_file() -> None:
    """読み直すと同じ中身になる。日時も TOML の日時で書く。"""
    data = {**SHOWN, "InstanceMarketOptions": {"SpotOptions": {"ValidUntil": WHEN}}}
    text = "\n".join(ltdata.render(data)) + "\n"
    assert tomlkit.parse(text).unwrap() == {"launch-template": data}
    assert ltdata.check(data, WHERE) == data


def test_render_adds_the_notes() -> None:
    """notes の値を書いた行の後ろに、コメントを添える。template show がサブネットの VPC に使う。"""
    lines = ltdata.render(SHOWN, {"subnet-1": "vpc-1"})
    assert 'SubnetId = "subnet-1"  # vpc-1' in lines
    assert [line for line in lines if "#" in line] == ['SubnetId = "subnet-1"  # vpc-1']


def test_render_of_nothing_is_only_the_heading() -> None:
    assert ltdata.render({}) == ["[launch-template]"]


def test_render_puts_a_blank_line_after_an_instance_profile_written_last() -> None:
    """IamInstanceProfile が項目の最後でも、テーブルの前には空行を置く。"""
    data = {"ImageId": "a", "IamInstanceProfile": {"Name": "x"}, "MetadataOptions": {"A": "b"}}
    assert ltdata.render(data) == [
        "[launch-template]",
        'ImageId = "a"',
        'IamInstanceProfile = { Name = "x" }',
        "",
        "[launch-template.MetadataOptions]",
        'A = "b"',
    ]


def test_render_writes_an_instance_profile_by_its_arn_inline_too() -> None:
    """AWS が ARN で返したものも、名前と同じくインラインにする。"""
    arn = "arn:aws:iam::123456789012:instance-profile/temt-web"
    assert ltdata.render({"IamInstanceProfile": {"Arn": arn}}) == [
        "[launch-template]",
        f'IamInstanceProfile = {{ Arn = "{arn}" }}',
    ]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        pytest.param({"Name": "${temt:name}"}, '{ Name = "${temt:name}" }', id="one"),
        pytest.param({"Key": "Env", "Value": 1}, '{ Key = "Env", Value = 1 }', id="two"),
        pytest.param({"temt:service": "web"}, '{ "temt:service" = "web" }', id="quoted key"),
        pytest.param({}, "{}", id="empty"),
    ],
)
def test_inline_puts_spaces_inside_the_braces(value: dict[str, Any], text: str) -> None:
    """読み直すと同じ中身になる。"""
    item = ltdata.inline(value)
    assert item.as_string() == text
    assert tomlkit.parse(f"x = {text}\n").unwrap() == {"x": value}


# --- element ---


def test_element_finds_the_element_by_its_key() -> None:
    items = ["x", {"DeviceIndex": 1, "SubnetId": "subnet-2"}, {"DeviceIndex": 0}]
    assert ltdata.element(items, "DeviceIndex", 0) == {"DeviceIndex": 0}
    assert ltdata.element(items, "DeviceIndex", 1) == {"DeviceIndex": 1, "SubnetId": "subnet-2"}
    assert ltdata.element(items, "DeviceIndex", 2) is None
    assert ltdata.element([], "DeviceIndex", 0) is None
