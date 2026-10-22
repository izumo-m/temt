"""role import と role delete の本体のテスト。AWS は Stubber で、標準入力は偽物に差し替える。

端から端まで（role.import_role、role.delete か cli.main）を通す。role import は AWS を
読むだけで書き換えるのは設定ファイルだけなので、書いた中身と、それが読めることを確かめる。
role delete は、消してよいことを確かめる手順と、消す順序を確かめる。
"""

import json
import re
import sys
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import REGION, WHERE, Stubs, an_instance, identified, reservations

from temt import cli, conf, ltbuild, resources, role
from temt.msg import TemtError, UsageError
from temt.names import Service, defaults_path
from temt.table import tilde

NAME = "temt-dev-main"
# 取り込むインスタンスロールと、それを入れた元のインスタンスプロファイル
SOURCE = "web-role"
SOURCE_ARN = f"arn:aws:iam::123456789012:role/{SOURCE}"
SOURCE_PROFILE = "web-profile"
SOURCE_PROFILE_ARN = f"arn:aws:iam::123456789012:instance-profile/{SOURCE_PROFILE}"
WHEN = datetime(2026, 9, 20, tzinfo=UTC)
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
# ロールに付いた管理ポリシー。AWS 管理のものと、顧客管理のもの
MANAGED = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
CUSTOMER = "arn:aws:iam::123456789012:policy/web-data"
OURS = [{"Key": "temt:service", "Value": "dev-main"}]

THE_SOURCE = {"RoleName": SOURCE}
THE_ROLE = {"RoleName": NAME}
THE_PROFILE = {"InstanceProfileName": NAME}
# role delete が、インスタンスプロファイル <名前> を使うインスタンスを探す条件。止めている
# ものも含む
USERS = {
    "Filters": [
        {"Name": "iam-instance-profile.arn", "Values": [f"*:instance-profile/{NAME}"]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        },
    ]
}

# service import が書いた形の設定ファイル。元のインスタンスプロファイルを指していて、temt の
# [role] は無い
IMPORTED = f"""\
[launch-template]
ImageId = "temt-dev-main-2609231234"
InstanceType = "t3.small"
IamInstanceProfile = {{ Arn = "arn:aws:iam::123456789012:instance-profile/{SOURCE_PROFILE}" }}
InstanceInitiatedShutdownBehavior = "terminate"

[[launch-template.NetworkInterfaces]]
DeviceIndex = 0
SubnetId = "subnet-0123456789abcdef0"
Groups = ["sg-0123456789abcdef0"]
AssociatePublicIpAddress = true
DeleteOnTermination = true
Ipv6AddressCount = 0
"""

# S3 を読むインラインポリシー。Statement が 2 つある
S3_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "Read",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:ListBucket"],
            "Resource": ["arn:aws:s3:::web-data", "arn:aws:s3:::web-data/*"],
        },
        {"Effect": "Deny", "Action": "s3:DeleteObject", "Resource": "*"},
    ],
}
# KMS のインラインポリシー。Statement を並びにせず 1 つだけ書き、Condition を持つ
KMS_POLICY = {
    "Version": "2012-10-17",
    "Statement": {
        "Effect": "Allow",
        "Action": "kms:Decrypt",
        "Resource": "arn:aws:kms:ap-northeast-1:123456789012:key/0123abcd",
        "Condition": {"StringEquals": {"kms:ViaService": "s3.ap-northeast-1.amazonaws.com"}},
    },
}

# role import が IMPORTED を書き換えた中身。[role] は最後に足し、IamInstanceProfile は
# その場で書き換える
REWRITTEN = f"""\
[launch-template]
ImageId = "temt-dev-main-2609231234"
InstanceType = "t3.small"
IamInstanceProfile = {{ Name = "${{temt:name}}" }}
InstanceInitiatedShutdownBehavior = "terminate"

[[launch-template.NetworkInterfaces]]
DeviceIndex = 0
SubnetId = "subnet-0123456789abcdef0"
Groups = ["sg-0123456789abcdef0"]
AssociatePublicIpAddress = true
DeleteOnTermination = true
Ipv6AddressCount = 0

[role]
ManagedPolicyArns = ["{MANAGED}", "{CUSTOMER}"]

[[role.s3access]]
Sid = "Read"
Effect = "Allow"
Action = ["s3:GetObject", "s3:ListBucket"]
Resource = ["arn:aws:s3:::web-data", "arn:aws:s3:::web-data/*"]

[[role.s3access]]
Effect = "Deny"
Action = "s3:DeleteObject"
Resource = "*"

[[role.kms-decrypt]]
Effect = "Allow"
Action = "kms:Decrypt"
Resource = "arn:aws:kms:ap-northeast-1:123456789012:key/0123abcd"

[role.kms-decrypt.Condition.StringEquals]
"kms:ViaService" = "s3.ap-northeast-1.amazonaws.com"
"""

IMPORT_PROMPT = "rewrite ~/.config/temt/dev-main.toml? [y/N] "
DELETE_PROMPT = f"delete them for {NAME} {WHERE}? [y/N] "
LEFT_HINT = (
    "fix by hand the resource policies that name the role web-role (S3 bucket policies, "
    "KMS key policies, ...); then run 'temt diff --svc dev-main' and "
    "'temt apply --svc dev-main'"
)

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(text: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in text.splitlines()]


class Terminal:
    """端末につながった標準入力の代わり。答える前に before を呼ぶ。"""

    def __init__(self, answer: str, *, tty: bool = True, before: Any = None) -> None:
        self.answer = answer
        self.tty = tty
        self.before = before
        self.reads = 0

    def isatty(self) -> bool:
        return self.tty

    def readline(self) -> str:
        self.reads += 1
        if self.before is not None:
            self.before()
        return self.answer


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> Terminal:
    """問いかけに y と答える端末。"""
    terminal = Terminal("y\n")
    monkeypatch.setattr(sys, "stdin", terminal)
    return terminal


@pytest.fixture
def imported(service: Service) -> Service:
    """service import が書いた設定ファイルのあるサービス。"""
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(IMPORTED, encoding="utf-8")
    return service


def missing(stub: Any, method: str, code: str, **params: Any) -> None:
    """そのリソースが無いときの誤りを積む。"""
    stub.add_client_error(
        method, service_error_code=code, http_status_code=404, expected_params=params or None
    )


def a_role(name: str = NAME, *, tags: list[dict[str, str]] | None = OURS) -> dict[str, Any]:
    """get-role が返す形。tags が None なら、タグを 1 つも持たない（AWS は Tags を返さない）。"""
    found: dict[str, Any] = {
        "Path": "/",
        "RoleName": name,
        "RoleId": "AROAEXAMPLE000000",
        "Arn": f"arn:aws:iam::123456789012:role/{name}",
        "CreateDate": WHEN,
    }
    if tags is not None:
        found["Tags"] = tags
    return {"Role": found}


def a_profile(
    roles: list[str], *, name: str = NAME, tags: list[dict[str, str]] | None = OURS
) -> dict[str, Any]:
    """get-instance-profile が返す形。tags は a_role と同じ。"""
    found: dict[str, Any] = {
        "Path": "/",
        "InstanceProfileName": name,
        "InstanceProfileId": "AIPAEXAMPLE000000",
        "Arn": f"arn:aws:iam::123456789012:instance-profile/{name}",
        "CreateDate": WHEN,
        "Roles": [a_role(one, tags=None)["Role"] for one in roles],
    }
    if tags is not None:
        found["Tags"] = tags
    return {"InstanceProfile": found}


def attached(*arns: str) -> dict[str, Any]:
    """list-attached-role-policies が返す形。"""
    return {
        "AttachedPolicies": [
            {"PolicyName": arn.rsplit("/", 1)[-1], "PolicyArn": arn} for arn in arns
        ]
    }


def encoded(document: dict[str, Any]) -> str:
    """IAM が返すポリシーの文書。JSON を URL エンコードしたもの。boto3 が戻して渡す。"""
    return urllib.parse.quote(json.dumps(document))


def source_read(
    stubs: Stubs,
    policies: dict[str, dict[str, Any]],
    managed: tuple[str, ...] = (),
    *,
    name: str = SOURCE,
) -> None:
    """role import が読む、取り込むロール、そのインラインポリシー、管理ポリシー。"""
    stubs.iam.add_response("get_role", a_role(name, tags=None), {"RoleName": name})
    stubs.iam.add_response(
        "list_role_policies", {"PolicyNames": list(policies)}, {"RoleName": name}
    )
    for policy, document in policies.items():
        stubs.iam.add_response(
            "get_role_policy",
            {"RoleName": name, "PolicyName": policy, "PolicyDocument": encoded(document)},
            {"RoleName": name, "PolicyName": policy},
        )
    stubs.iam.add_response("list_attached_role_policies", attached(*managed), {"RoleName": name})


def text_of(service: Service) -> str:
    return service.conf_path.read_text(encoding="utf-8")


# role import


def test_import_rewrites_role_and_the_instance_profile(
    aws_stubs: Stubs,
    imported: Service,
    answer: Terminal,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """インラインポリシーは Statement ごとに [[role.<ポリシー名>]] に、管理ポリシーは
    ARN のまま ManagedPolicyArns にコピーし、IamInstanceProfile を temt のものにする。

    名前は規則に合わせて小文字にし、使えない文字をハイフンにする。AWS には何も書かない。
    """
    source_read(aws_stubs, {"S3Access": S3_POLICY, "kms_decrypt": KMS_POLICY}, (MANAGED, CUSTOMER))

    role.import_role(imported, SOURCE, yes=False)

    # 書き込む呼び出しは積んでいないので、AWS を書き換えれば Stubber が失敗する
    aws_stubs.done()
    assert answer.reads == 1
    assert text_of(imported) == REWRITTEN
    captured = capsys.readouterr()
    assert captured.err == IMPORT_PROMPT
    assert told(captured.out) == [
        "role import web-role into ~/.config/temt/dev-main.toml (replacing [role]):",
        "  [[role.s3access]] (2 statements)",
        "  [[role.kms-decrypt]] (1 statement)",
        f"  ManagedPolicyArns: {MANAGED}",
        f"  ManagedPolicyArns: {CUSTOMER}",
        f'  launch-template.IamInstanceProfile: {{ Arn = "{SOURCE_PROFILE_ARN}" }} -> '
        '{ Name = "${temt:name}" }',
        "wrote [role] and launch-template.IamInstanceProfile in ~/.config/temt/dev-main.toml",
        LEFT_HINT,
    ]


def test_import_writes_a_config_file_that_apply_reads(aws_stubs: Stubs, imported: Service) -> None:
    """書いた設定ファイルは読めて、IamInstanceProfile が temt のインスタンスプロファイルを
    指す。apply はそれで temt-<svc> を作り、[role] を反映する。"""
    source_read(aws_stubs, {"S3Access": S3_POLICY, "kms_decrypt": KMS_POLICY}, (MANAGED, CUSTOMER))

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    values = conf.load(imported)
    assert ltbuild.points_at_temt(values.launch_template, imported)
    assert values.role.ManagedPolicyArns == [MANAGED, CUSTOMER]
    assert values.role.policies == {
        "s3access": S3_POLICY["Statement"],
        "kms-decrypt": [KMS_POLICY["Statement"]],
    }
    # ほかの項目は変わらない
    before = conf.parse_text(IMPORTED, imported.conf_path)
    assert {
        key: value for key, value in values.launch_template.items() if key != "IamInstanceProfile"
    } == {
        key: value for key, value in before.launch_template.items() if key != "IamInstanceProfile"
    }


def test_import_replaces_the_whole_role_table(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """[role] は丸ごと置き換える。前に書いていたポリシーも管理ポリシーも残さない。"""
    imported.conf_path.write_text(
        IMPORTED
        + '\n[role]\nManagedPolicyArns = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]\n'
        + '\n[[role.old]]\nAction = "s3:GetObject"\nResource = "*"\n',
        encoding="utf-8",
    )
    source_read(aws_stubs, {"S3Access": S3_POLICY}, ())

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    values = conf.load(imported)
    assert values.role.policies == {"s3access": S3_POLICY["Statement"]}
    assert values.role.ManagedPolicyArns == []
    assert "old" not in text_of(imported)
    assert "ReadOnlyAccess" not in text_of(imported)
    assert "ManagedPolicyArns" not in capsys.readouterr().out


# [role] の前後にコメントがあり、後ろに別のテーブルが続く設定ファイル
COMMENTED = (
    IMPORTED
    + """
# 権限
[role]
ManagedPolicyArns = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]

[[role.old]]
Action = "s3:GetObject"
Resource = "*"

# 受け付ける通信
[[security-group.ingress]]
Port = 443
Cidr = "203.0.113.0/24"
"""
)


@pytest.mark.parametrize(
    ("policies", "role_table"),
    [
        pytest.param(
            {"S3Access": S3_POLICY},
            "# 権限\n[role]\n\n[[role.s3access]]\n",
            id="replaced",
        ),
        pytest.param({}, None, id="removed"),
    ],
)
def test_import_keeps_the_comments_around_role(
    aws_stubs: Stubs,
    imported: Service,
    policies: dict[str, dict[str, Any]],
    role_table: str | None,
) -> None:
    """[role] は書いてあった場所で置き換え、前のコメントも、次のテーブルの説明のコメントも
    残す。消すときも、利用者が書いたコメントは消さない。"""
    imported.conf_path.write_text(COMMENTED, encoding="utf-8")
    source_read(aws_stubs, policies)

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    text = text_of(imported)
    assert text.endswith(
        '\n\n# 受け付ける通信\n[[security-group.ingress]]\nPort = 443\nCidr = "203.0.113.0/24"\n'
    )
    if role_table is None:
        assert "[role" not in text
        assert "Ipv6AddressCount = 0\n\n# 権限\n\n# 受け付ける通信\n" in text
    else:
        assert role_table in text
        assert text.index("[[role.s3access]]") < text.index("# 受け付ける通信")
    assert conf.load(imported).security_group.ingress[0].Port == (443, 443)


def test_import_drops_role_when_the_role_has_no_policy(aws_stubs: Stubs, imported: Service) -> None:
    """ポリシーが 1 つも無いロールなら、[role] を消し、IamInstanceProfile だけを書き換える。

    [role] が無く IamInstanceProfile が temt を指すので、次の apply が止まって知らせる。
    """
    imported.conf_path.write_text(
        IMPORTED + '\n[[role.old]]\nAction = "s3:GetObject"\nResource = "*"\n',
        encoding="utf-8",
    )
    source_read(aws_stubs, {})

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    assert "[role" not in text_of(imported)
    values = conf.load(imported)
    assert not values.role
    assert ltbuild.points_at_temt(values.launch_template, imported)


def test_import_adds_the_instance_profile_when_none_is_written(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """元のインスタンスにインスタンスプロファイルが無ければ、取り込んだ設定ファイルにも
    IamInstanceProfile は無い。そのときも [launch-template] に足し、確認には、無かったことを
    - で示す。"""
    imported.conf_path.write_text(
        IMPORTED.replace(
            f'IamInstanceProfile = {{ Arn = "arn:aws:iam::123456789012:instance-profile/'
            f'{SOURCE_PROFILE}" }}\n',
            "",
        ),
        encoding="utf-8",
    )
    source_read(aws_stubs, {}, (MANAGED,))

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    # 雛形と同じく、ドット付きキーで書く
    assert 'IamInstanceProfile.Name = "${temt:name}"\n' in text_of(imported)
    values = conf.load(imported)
    assert values.launch_template["IamInstanceProfile"] == {"Name": "${temt:name}"}
    assert values.role.ManagedPolicyArns == [MANAGED]
    assert told(capsys.readouterr().out)[1:3] == [
        f"  ManagedPolicyArns: {MANAGED}",
        '  launch-template.IamInstanceProfile: - -> { Name = "${temt:name}" }',
    ]


def test_import_adds_the_instance_profile_before_a_table_that_follows_at_once(
    aws_stubs: Stubs, imported: Service
) -> None:
    """[launch-template] の項目の直後に空行を置かずにテーブルが続いても、足した行は改行で
    終え、後ろのテーブルはそのまま残る。"""
    imported.conf_path.write_text(
        '[launch-template]\nImageId = "temt-dev-main-2609231234"\n'
        "[[launch-template.NetworkInterfaces]]\nDeviceIndex = 0\n",
        encoding="utf-8",
    )
    source_read(aws_stubs, {})

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    assert text_of(imported) == (
        '[launch-template]\nImageId = "temt-dev-main-2609231234"\n'
        'IamInstanceProfile.Name = "${temt:name}"\n'
        "[[launch-template.NetworkInterfaces]]\nDeviceIndex = 0\n"
    )


def test_import_counts_the_statements_of_each_policy(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """確認には、ポリシーごとの Statement の数を、1 つなら statement、2 つ以上なら
    statements と添える。"""
    source_read(aws_stubs, {"S3Access": S3_POLICY, "kms_decrypt": KMS_POLICY})

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[1:3] == [
        "  [[role.s3access]] (2 statements)",
        "  [[role.kms-decrypt]] (1 statement)",
    ]


def test_import_rewrites_an_instance_profile_written_as_a_table(
    aws_stubs: Stubs, imported: Service
) -> None:
    """手で書いた設定ファイルなどで、IamInstanceProfile がテーブルの見出しで書かれていても
    書き換えられ、後ろのテーブルの中身は変わらない。"""
    imported.conf_path.write_text(
        f'AwsRegion = "{REGION}"\nDefaults = false\n\n'
        '[launch-template]\nImageId = "temt-dev-main-2609231234"\n\n'
        "[launch-template.IamInstanceProfile]\n"
        f'Arn = "arn:aws:iam::123456789012:instance-profile/{SOURCE_PROFILE}"\n\n'
        '[launch-template.MetadataOptions]\nHttpTokens = "required"\n',
        encoding="utf-8",
    )
    source_read(aws_stubs, {"S3Access": S3_POLICY})

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    values = conf.load(imported)
    assert values.launch_template == {
        "ImageId": "temt-dev-main-2609231234",
        "IamInstanceProfile": {"Name": "${temt:name}"},
        "MetadataOptions": {"HttpTokens": "required"},
    }
    assert values.role.policies == {"s3access": S3_POLICY["Statement"]}


# 書き換える IamInstanceProfile の後ろに置いた、次の見出しの説明のコメントと、その見出し
RULES = '\n# about the rules\n[[security-group.ingress]]\nPort = 22\nCidr = "192.0.2.1/32"\n'


@pytest.mark.parametrize(
    ("written", "rewritten"),
    [
        pytest.param(
            f'IamInstanceProfile = {{ Arn = "{SOURCE_PROFILE_ARN}" }}\n',
            'IamInstanceProfile = { Name = "${temt:name}" }\n',
            id="inline table",
        ),
        pytest.param(
            f'\n[launch-template.IamInstanceProfile]\nArn = "{SOURCE_PROFILE_ARN}"\n',
            '\n[launch-template.IamInstanceProfile]\nName = "${temt:name}"\n',
            id="table",
        ),
        pytest.param(
            f'IamInstanceProfile.Arn = "{SOURCE_PROFILE_ARN}"\n',
            'IamInstanceProfile.Name = "${temt:name}"\n',
            id="dotted key",
        ),
    ],
)
def test_import_rewrites_the_instance_profile_where_it_is_written(
    aws_stubs: Stubs,
    imported: Service,
    capsys: pytest.CaptureFixture[str],
    written: str,
    rewritten: str,
) -> None:
    """インラインテーブルは置き換え、テーブルかドット付きキーで書かれていれば、その書き方の
    まま Name を temt のものにして Arn を消す。

    後ろのコメントは次の見出しの説明なので、書き換えた行の後ろ、次の見出しの前に残す。
    """
    head = f'AwsRegion = "{REGION}"\nDefaults = false\n\n[launch-template]\nImageId = "a"\n'
    imported.conf_path.write_text(head + written + RULES, encoding="utf-8")
    source_read(aws_stubs, {})

    role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    assert text_of(imported) == head + rewritten + RULES
    assert told(capsys.readouterr().out)[1] == (
        f'  launch-template.IamInstanceProfile: {{ Arn = "{SOURCE_PROFILE_ARN}" }} -> '
        '{ Name = "${temt:name}" }'
    )


@pytest.mark.parametrize(
    "given",
    [
        pytest.param(SOURCE_ARN, id="arn"),
        pytest.param(f"arn:aws:iam::123456789012:role/service-role/{SOURCE}", id="arn-with-path"),
    ],
)
def test_import_takes_the_role_by_arn(aws_stubs: Stubs, imported: Service, given: str) -> None:
    """ARN で指せば、その名前のロールを読む。IAM のロール名はパスを問わず一意である。
    ARN のアカウントは、資格情報のアカウントと同じでなければならない。
    """
    aws_stubs.sts.add_response(
        "get_caller_identity",
        {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/x", "UserId": "x"},
        {},
    )
    source_read(aws_stubs, {"S3Access": S3_POLICY})

    role.import_role(imported, given, yes=True)

    aws_stubs.done()
    assert conf.load(imported).role.policies == {"s3access": S3_POLICY["Statement"]}


def test_import_refuses_a_role_of_another_account(
    aws_stubs: Stubs, imported: Service, answer: Terminal
) -> None:
    """ARN のアカウントが資格情報のものと違えば、読まずに止める。同じ名前の別のロールを
    コピーしないためである。"""
    aws_stubs.sts.add_response(
        "get_caller_identity",
        {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/x", "UserId": "x"},
        {},
    )
    given = f"arn:aws:iam::999999999999:role/{SOURCE}"

    with pytest.raises(TemtError) as raised:
        role.import_role(imported, given, yes=False)

    aws_stubs.done()
    assert str(raised.value) == (
        f"the role {given} is in the account 999999999999, not the one of the credentials; "
        "role import reads roles of that account only"
    )
    assert answer.reads == 0
    assert text_of(imported) == IMPORTED


def test_import_stops_when_the_role_is_not_found(
    aws_stubs: Stubs, imported: Service, answer: Terminal
) -> None:
    missing(aws_stubs.iam, "get_role", resources.NO_SUCH_ENTITY, **THE_SOURCE)

    with pytest.raises(TemtError, match=f"^no such role: {SOURCE}$"):
        role.import_role(imported, SOURCE, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert text_of(imported) == IMPORTED


@pytest.mark.parametrize(
    ("policy", "label"),
    [
        pytest.param("1st-policy", "1st-policy", id="leading-digit"),
        pytest.param("_private", "-private", id="leading-symbol"),
        pytest.param("policy_", "policy-", id="trailing-symbol"),
        pytest.param("p" * 65, "p" * 65, id="too-long"),
    ],
)
def test_import_stops_when_a_policy_name_cannot_follow_the_rule(
    aws_stubs: Stubs, imported: Service, answer: Terminal, policy: str, label: str
) -> None:
    """小文字にし、使えない文字をハイフンにしても規則に合わなければ、名前を直すよう告げて
    止める。設定ファイルには触らない。"""
    aws_stubs.iam.add_response("get_role", a_role(SOURCE, tags=None), THE_SOURCE)
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": [policy]}, THE_SOURCE)

    with pytest.raises(TemtError) as caught:
        role.import_role(imported, SOURCE, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"the inline policy {policy} becomes {label}, but the policy name must start with a "
        "lowercase letter, use only lowercase letters, digits, and '-', end with a letter or "
        "digit, and be at most 64 characters; rename it in IAM and run role import again"
    )
    assert text_of(imported) == IMPORTED


@pytest.mark.parametrize(
    ("first", "second", "label"),
    [
        pytest.param("S3Access", "s3access", "s3access", id="case"),
        pytest.param("web_data", "web.data", "web-data", id="symbols"),
    ],
)
def test_import_stops_when_policy_names_collide(
    aws_stubs: Stubs, imported: Service, first: str, second: str, label: str
) -> None:
    """直すと同じ名前になるインラインポリシーがあれば、両方の名前を挙げて止める。"""
    aws_stubs.iam.add_response("get_role", a_role(SOURCE, tags=None), THE_SOURCE)
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": [first, second]}, THE_SOURCE)
    aws_stubs.iam.add_response(
        "get_role_policy",
        {"RoleName": SOURCE, "PolicyName": first, "PolicyDocument": encoded(S3_POLICY)},
        {"RoleName": SOURCE, "PolicyName": first},
    )

    with pytest.raises(TemtError) as caught:
        role.import_role(imported, SOURCE, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"the inline policies {first} and {second} both become {label}; rename one in IAM "
        "and run role import again"
    )
    assert text_of(imported) == IMPORTED


def test_import_changes_nothing_when_the_answer_is_not_y(
    aws_stubs: Stubs,
    imported: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    source_read(aws_stubs, {"S3Access": S3_POLICY})

    assert cli.main(["temt", "role", "import", "--svc", "dev-main", SOURCE]) == 1

    aws_stubs.done()
    assert text_of(imported) == IMPORTED
    assert capsys.readouterr().err == IMPORT_PROMPT + "temt: cancelled\n"


def test_import_needs_yes_when_stdin_is_not_a_terminal(
    aws_stubs: Stubs, imported: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal("y\n", tty=False))
    source_read(aws_stubs, {"S3Access": S3_POLICY})

    with pytest.raises(TemtError, match="stdin is not a terminal; pass --yes"):
        role.import_role(imported, SOURCE, yes=False)

    aws_stubs.done()
    assert text_of(imported) == IMPORTED


def test_import_stops_when_the_config_file_changes_while_asking(
    aws_stubs: Stubs, imported: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """確認を待つあいだに設定ファイルが書き換えられたら、上書きせずに止める。"""
    changed = IMPORTED.replace('"t3.small"', '"t3.large"')

    def edit() -> None:
        imported.conf_path.write_text(changed, encoding="utf-8")

    monkeypatch.setattr(sys, "stdin", Terminal("y\n", before=edit))
    source_read(aws_stubs, {"S3Access": S3_POLICY})

    with pytest.raises(
        TemtError,
        match="^~/.config/temt/dev-main.toml changed while running; run role import again$",
    ):
        role.import_role(imported, SOURCE, yes=False)

    aws_stubs.done()
    assert text_of(imported) == changed


def test_import_needs_the_config_file(
    aws_stubs: Stubs, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """role import は設定ファイルを書き換えるだけなので、無ければ AWS を読まずに止める。"""
    assert cli.main(["temt", "role", "import", "--svc", "dev-main", SOURCE, "--yes"]) == 1

    aws_stubs.done()
    assert aws_stubs.passed == []
    assert not service.conf_path.exists()
    assert capsys.readouterr().err == (
        f"temt: no config file: {tilde(service.conf_path)}; run 'temt apply --svc dev-main' first\n"
    )


def test_import_stops_when_it_cannot_write_the_instance_profile(
    aws_stubs: Stubs, imported: Service, answer: Terminal
) -> None:
    """[launch-template] をインラインテーブルで書いていれば、書き足せないので、確認の前に
    止める。"""
    imported.conf_path.write_text(
        'launch-template = { InstanceType = "t3.small" }\n', encoding="utf-8"
    )
    before = text_of(imported)
    source_read(aws_stubs, {"S3Access": S3_POLICY})

    with pytest.raises(TemtError) as caught:
        role.import_role(imported, SOURCE, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert text_of(imported) == before
    # 書けない設定ファイルと、手で書いてからやり直す role import を告げる
    key = "launch-template.IamInstanceProfile.Name"
    assert str(caught.value) == (
        f"cannot write {key} in ~/.config/temt/dev-main.toml, as it would go into an inline "
        f'table or array; write {key} = "${{temt:name}}" there by hand and run role import again'
    )


def test_import_runs_from_the_command_line(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    source_read(aws_stubs, {"S3Access": S3_POLICY, "kms_decrypt": KMS_POLICY}, (MANAGED, CUSTOMER))

    assert cli.main(["temt", "role", "import", "--svc", "dev-main", SOURCE, "--yes"]) == 0

    aws_stubs.done()
    assert text_of(imported) == REWRITTEN
    assert told(capsys.readouterr().out)[-1] == LEFT_HINT


def test_import_takes_one_role() -> None:
    assert cli.parse(["role", "import", "--svc", "dev-main", SOURCE]).app == SOURCE
    assert cli.parse(["role", "import", "--svc", "dev-main", SOURCE, "--yes"]).yes is True
    with pytest.raises(UsageError):
        cli.parse(["role", "import", "--svc", "dev-main"])
    with pytest.raises(UsageError):
        cli.parse(["role", "import", "--svc", "dev-main", SOURCE, "other"])


# role delete


def found_pair(
    stubs: Stubs,
    *,
    role_tags: list[dict[str, str]] | None = OURS,
    profile_tags: list[dict[str, str]] | None = OURS,
) -> None:
    """role delete の手順 1。ロールとインスタンスプロファイル <名前> のタグを読む。"""
    stubs.iam.add_response("get_role", a_role(tags=role_tags), THE_ROLE)
    stubs.iam.add_response(
        "get_instance_profile", a_profile([NAME], tags=profile_tags), THE_PROFILE
    )


def no_users(stubs: Stubs) -> None:
    """手順 2。インスタンスプロファイルを使うインスタンスは無い。"""
    stubs.ec2.add_response("describe_instances", reservations(), USERS)


def profile_held(stubs: Stubs, roles: tuple[str, ...] = (NAME,)) -> None:
    """手順 2。インスタンスプロファイル <名前> に入っているロールを読む。"""
    stubs.iam.add_response("get_instance_profile", a_profile(list(roles)), THE_PROFILE)


def role_held(stubs: Stubs, inline: tuple[str, ...] = (), managed: tuple[str, ...] = ()) -> None:
    """手順 3 の確認に挙げる、ロールのインラインポリシーの名前と管理ポリシーの ARN を読む。"""
    stubs.iam.add_response("list_role_policies", {"PolicyNames": list(inline)}, THE_ROLE)
    stubs.iam.add_response("list_attached_role_policies", attached(*managed), THE_ROLE)


def in_profiles(stubs: Stubs, *names: str) -> None:
    """ロール <名前> を入れたインスタンスプロファイルの一覧。"""
    stubs.iam.add_response(
        "list_instance_profiles_for_role",
        {"InstanceProfiles": [a_profile([NAME], name=one)["InstanceProfile"] for one in names]},
        THE_ROLE,
    )


def profile_deleted(stubs: Stubs) -> None:
    """手順 4。インスタンスプロファイルからロールを外して消す。"""
    stubs.iam.add_response("get_instance_profile", a_profile([NAME]), THE_PROFILE)
    stubs.iam.add_response(
        "remove_role_from_instance_profile", {}, {"InstanceProfileName": NAME, "RoleName": NAME}
    )
    stubs.iam.add_response("delete_instance_profile", {}, THE_PROFILE)


def role_deleted(stubs: Stubs, inline: tuple[str, ...] = (), managed: tuple[str, ...] = ()) -> None:
    """手順 4。管理ポリシーを外し、インラインポリシーを消してから、ロールを消す。"""
    stubs.iam.add_response("get_role", a_role(), THE_ROLE)
    in_profiles(stubs)
    stubs.iam.add_response("list_attached_role_policies", attached(*managed), THE_ROLE)
    for arn in managed:
        stubs.iam.add_response("detach_role_policy", {}, {"RoleName": NAME, "PolicyArn": arn})
    stubs.iam.add_response("list_role_policies", {"PolicyNames": list(inline)}, THE_ROLE)
    for name in inline:
        stubs.iam.add_response("delete_role_policy", {}, {"RoleName": NAME, "PolicyName": name})
    stubs.iam.add_response("delete_role", {}, THE_ROLE)


def listing(*lines: str) -> str:
    """手順 3 で挙げる、消すもの。"""
    return f"role delete {NAME}:\n" + "".join(f"  {line}\n" for line in lines)


# 手順 3 で挙げる、インスタンスプロファイルと、ポリシーの付いていないロール
PROFILE_LINE = f"instance profile {NAME}"
ROLE_LINE = f"role {NAME}"


@pytest.fixture
def calls(aws_stubs: Stubs) -> list[str]:
    """IAM と EC2 のクライアントが呼んだ API を、呼ばれた順に集める。"""
    made: list[str] = []

    def note(model: Any, **kwargs: Any) -> None:
        _ = kwargs
        made.append(model.name)

    for stub in (aws_stubs.iam, aws_stubs.ec2):
        stub.client.meta.events.register("provide-client-params", note)
    return made


def test_delete_deletes_the_pair_tagged_for_the_service(
    aws_stubs: Stubs,
    imported: Service,
    answer: Terminal,
    calls: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """確かめてから確認し、インスタンスプロファイル、ロールの順に消す。ロールは、管理
    ポリシーを外し（ポリシーそのものは残す）、インラインポリシーを消してから消す。

    確認には、ロールと一緒に消すインラインポリシーと、外す管理ポリシーも挙げる。
    """
    identified(aws_stubs)
    found_pair(aws_stubs)
    no_users(aws_stubs)
    profile_held(aws_stubs)
    in_profiles(aws_stubs)
    role_held(aws_stubs, ("temt-ec2", "temt-s3access"), (MANAGED,))
    profile_deleted(aws_stubs)
    role_deleted(aws_stubs, ("temt-ec2", "temt-s3access"), (MANAGED,))

    role.delete(imported, yes=False)

    aws_stubs.done()
    assert answer.reads == 1
    assert calls == [
        "GetRole",
        "GetInstanceProfile",
        "DescribeInstances",
        "GetInstanceProfile",
        "ListInstanceProfilesForRole",
        "ListRolePolicies",
        "ListAttachedRolePolicies",
        "GetInstanceProfile",
        "RemoveRoleFromInstanceProfile",
        "DeleteInstanceProfile",
        "GetRole",
        "ListInstanceProfilesForRole",
        "ListAttachedRolePolicies",
        "DetachRolePolicy",
        "ListRolePolicies",
        "DeleteRolePolicy",
        "DeleteRolePolicy",
        "DeleteRole",
    ]
    captured = capsys.readouterr()
    assert captured.err == DELETE_PROMPT
    shown = listing(
        PROFILE_LINE,
        f"role {NAME} (inline policies: temt-ec2, temt-s3access; managed policies: {MANAGED})",
    )
    assert captured.out.startswith(shown)
    assert told(captured.out.removeprefix(shown)) == [
        f"instance profile {NAME}: deleted",
        f"role {NAME}: detached {MANAGED}",
        "role policy temt-ec2: deleted",
        "role policy temt-s3access: deleted",
        f"role {NAME}: deleted",
    ]
    # 設定ファイルには触らない
    assert text_of(imported) == IMPORTED


def test_delete_stops_while_the_config_file_has_role(
    aws_stubs: Stubs, imported: Service, answer: Terminal
) -> None:
    """[role] があれば次の apply がまた作るので、先に [role] を消すよう告げる。AWS は読まない。"""
    imported.conf_path.write_text(
        IMPORTED + '\n[[role.app]]\nAction = "s3:GetObject"\nResource = "*"\n',
        encoding="utf-8",
    )

    with pytest.raises(TemtError) as caught:
        role.delete(imported, yes=False)

    # 応答を 1 つも積んでいないので、AWS を読んでいれば Stubber が失敗する
    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"[role] in ~/.config/temt/dev-main.toml makes {NAME}, and the next apply would make "
        "it again; remove [role] first"
    )


# このリージョンと、ほかのリージョンのタグ
HERE = {"Key": f"temt:region:{REGION}", "Value": ""}
THERE = {"Key": "temt:region:us-west-2", "Value": ""}


def test_delete_stops_while_another_region_has_role(
    aws_stubs: Stubs, imported: Service, answer: Terminal
) -> None:
    """ロールにほかのリージョンのタグが残っていれば、そこの設定にはまだ [role] があるので、
    何も消さずに止まる。このリージョンのタグは数えない。
    """
    found_pair(aws_stubs, role_tags=[*OURS, HERE, THERE])

    with pytest.raises(TemtError) as caught:
        role.delete(imported, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"the role {NAME} is used in us-west-2 (tagged temt:region:us-west-2); remove [role] "
        "and run apply there first"
    )


def test_delete_deletes_a_role_with_only_the_tag_of_its_region(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """このリージョンのタグしか無ければ、ほかに使うリージョンは無いので消す。"""
    found_pair(aws_stubs, role_tags=[*OURS, HERE])
    no_users(aws_stubs)
    profile_held(aws_stubs)
    in_profiles(aws_stubs)
    role_held(aws_stubs)
    profile_deleted(aws_stubs)
    role_deleted(aws_stubs)

    role.delete(imported, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out)[-2:] == [
        f"instance profile {NAME}: deleted",
        f"role {NAME}: deleted",
    ]


def test_delete_counts_a_name_too_long_for_iam_as_absent(
    aws_stubs: Stubs, home_dir: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """<名前> が IAM のロール名の上限より長ければ、IAM を呼ばずに、どちらも無いものとする。

    応答を積んでいないので、呼べば Stubber が失敗する。
    """
    _ = home_dir
    service = Service("a" * 60)
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text(f'AwsRegion = "{REGION}"\n', encoding="utf-8")

    role.delete(service, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [
        f"role {service.name}: absent",
        f"instance profile {service.name}: absent",
    ]


def test_delete_names_the_defaults_file_that_has_role(aws_stubs: Stubs, service: Service) -> None:
    """[role] が共通ファイルにあれば、そのファイルの名前を挙げる。"""
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text(f'AwsRegion = "{REGION}"\n', encoding="utf-8")
    defaults_path().write_text(f'[role]\nManagedPolicyArns = ["{MANAGED}"]\n', encoding="utf-8")

    with pytest.raises(TemtError, match=r"^\[role\] in ~/\.config/temt/defaults\.toml makes "):
        role.delete(service, yes=True)

    aws_stubs.done()


def test_delete_names_the_defaults_file_beside_an_empty_role(
    aws_stubs: Stubs, service: Service
) -> None:
    """設定ファイルの中身の無い [role] は書かれていないのと同じなので、共通ファイルを挙げる。"""
    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text(f'AwsRegion = "{REGION}"\n\n[role]\n', encoding="utf-8")
    defaults_path().write_text(f'[role]\nManagedPolicyArns = ["{MANAGED}"]\n', encoding="utf-8")

    with pytest.raises(TemtError, match=r"^\[role\] in ~/\.config/temt/defaults\.toml makes "):
        role.delete(service, yes=True)

    aws_stubs.done()


def test_delete_does_not_look_at_the_instance_profile_pointed_at(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """[role] が無ければ、IamInstanceProfile が <名前> を指していても止めない。次の apply が
    止まるので分かる。
    """
    imported.conf_path.write_text(
        IMPORTED.replace(
            f'{{ Arn = "arn:aws:iam::123456789012:instance-profile/{SOURCE_PROFILE}" }}',
            '{ Name = "${temt:name}" }',
        ),
        encoding="utf-8",
    )
    missing(aws_stubs.iam, "get_role", resources.NO_SUCH_ENTITY, RoleName=NAME)
    missing(
        aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, InstanceProfileName=NAME
    )

    role.delete(imported, yes=True)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [
        f"role {NAME}: absent",
        f"instance profile {NAME}: absent",
    ]


def test_delete_stops_while_an_instance_uses_the_instance_profile(
    aws_stubs: Stubs, imported: Service, answer: Terminal
) -> None:
    """止めているインスタンスも数える。消すと資格情報を失う。"""
    found_pair(aws_stubs)
    aws_stubs.ec2.add_response(
        "describe_instances",
        reservations(an_instance(OTHER, "running"), an_instance(INSTANCE, "stopped")),
        USERS,
    )

    with pytest.raises(TemtError) as caught:
        role.delete(imported, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"the instance profile {NAME} is used by {INSTANCE}, {OTHER}; terminate them first"
    )


@pytest.mark.parametrize(
    "tags",
    [
        pytest.param(None, id="untagged"),
        pytest.param([{"Key": "temt:service", "Value": "dev-other"}], id="other-service"),
    ],
)
@pytest.mark.parametrize("which", ["role", "instance profile"])
def test_delete_stops_when_one_is_not_tagged_for_the_service(
    aws_stubs: Stubs,
    imported: Service,
    answer: Terminal,
    tags: list[dict[str, str]] | None,
    which: str,
) -> None:
    """temt:service=<svc> の無いものは消さず、手で消すよう告げて止める。どちらも消さない。"""
    if which == "role":
        found_pair(aws_stubs, role_tags=tags)
    else:
        found_pair(aws_stubs, profile_tags=tags)

    with pytest.raises(TemtError) as caught:
        role.delete(imported, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"the {which} {NAME} is not tagged temt:service=dev-main; delete it by hand"
    )


def test_delete_stops_when_the_role_is_in_another_instance_profile(
    aws_stubs: Stubs, imported: Service, answer: Terminal
) -> None:
    """ほかのインスタンスプロファイルにも入っていれば、確認の前に止める。

    インスタンスプロファイル <名前> そのものは数えない。
    """
    found_pair(aws_stubs)
    no_users(aws_stubs)
    profile_held(aws_stubs)
    in_profiles(aws_stubs, NAME, SOURCE_PROFILE)

    with pytest.raises(TemtError) as caught:
        role.delete(imported, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"the role {NAME} is also in the instance profile {SOURCE_PROFILE}; remove it from "
        "there by hand and run role delete again"
    )


def test_delete_stops_when_the_instance_profile_holds_another_role(
    aws_stubs: Stubs, imported: Service, answer: Terminal
) -> None:
    """インスタンスプロファイル <名前> にほかのロールが入っていれば、確認の前に止める。

    確認の後で止まると、確認で挙げたものを何も消さずに終わる。
    """
    found_pair(aws_stubs)
    no_users(aws_stubs)
    profile_held(aws_stubs, (NAME, SOURCE))

    with pytest.raises(TemtError) as caught:
        role.delete(imported, yes=False)

    # ロールを外す、消す呼び出しは積んでいない
    aws_stubs.done()
    assert answer.reads == 0
    assert str(caught.value) == (
        f"the instance profile {NAME} holds another role: {SOURCE}; remove it by hand and run "
        "role delete again"
    )


def test_delete_deletes_only_the_role_when_the_instance_profile_is_absent(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """片方だけあれば、それだけを消す。インスタンスプロファイルが無ければ、使うインスタンスも
    探さない。"""
    aws_stubs.iam.add_response("get_role", a_role(), THE_ROLE)
    missing(aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, **THE_PROFILE)
    in_profiles(aws_stubs)
    role_held(aws_stubs, ("temt-ec2",))
    role_deleted(aws_stubs, ("temt-ec2",))

    role.delete(imported, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    shown = listing(f"role {NAME} (inline policies: temt-ec2)")
    assert out.startswith(shown)
    assert told(out.removeprefix(shown)) == [
        "role policy temt-ec2: deleted",
        f"role {NAME}: deleted",
    ]


def test_delete_deletes_only_the_instance_profile_when_the_role_is_absent(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    missing(aws_stubs.iam, "get_role", resources.NO_SUCH_ENTITY, **THE_ROLE)
    aws_stubs.iam.add_response("get_instance_profile", a_profile([]), THE_PROFILE)
    no_users(aws_stubs)
    profile_held(aws_stubs, ())
    aws_stubs.iam.add_response("get_instance_profile", a_profile([]), THE_PROFILE)
    aws_stubs.iam.add_response("delete_instance_profile", {}, THE_PROFILE)

    role.delete(imported, yes=True)

    aws_stubs.done()
    out = capsys.readouterr().out
    shown = listing(PROFILE_LINE)
    assert out.startswith(shown)
    assert told(out.removeprefix(shown)) == [f"instance profile {NAME}: deleted"]


def test_delete_reports_when_neither_exists(
    aws_stubs: Stubs, imported: Service, answer: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    """どちらも無ければ、問いかけずに absent と知らせて終わる。エラーにはしない。"""
    missing(aws_stubs.iam, "get_role", resources.NO_SUCH_ENTITY, **THE_ROLE)
    missing(aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, **THE_PROFILE)

    role.delete(imported, yes=False)

    aws_stubs.done()
    assert answer.reads == 0
    assert told(capsys.readouterr().out) == [
        f"role {NAME}: absent",
        f"instance profile {NAME}: absent",
    ]


def test_delete_changes_nothing_when_the_answer_is_not_y(
    aws_stubs: Stubs,
    imported: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identified(aws_stubs)
    monkeypatch.setattr(sys, "stdin", Terminal("n\n"))
    found_pair(aws_stubs)
    no_users(aws_stubs)
    profile_held(aws_stubs)
    in_profiles(aws_stubs)
    role_held(aws_stubs)

    assert cli.main(["temt", "role", "delete", "--svc", "dev-main"]) == 1

    # 消す呼び出しは積んでいないので、消していれば Stubber が失敗する
    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out == listing(PROFILE_LINE, ROLE_LINE)
    assert captured.err == DELETE_PROMPT + "temt: cancelled\n"


def test_delete_runs_from_the_command_line(
    aws_stubs: Stubs, imported: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    found_pair(aws_stubs)
    no_users(aws_stubs)
    profile_held(aws_stubs)
    in_profiles(aws_stubs)
    role_held(aws_stubs)
    profile_deleted(aws_stubs)
    role_deleted(aws_stubs)

    assert cli.main(["temt", "role", "delete", "--svc", "dev-main", "--yes"]) == 0

    aws_stubs.done()
    assert told(capsys.readouterr().out)[-2:] == [
        f"instance profile {NAME}: deleted",
        f"role {NAME}: deleted",
    ]


def test_delete_tells_to_run_role_delete_again(aws_stubs: Stubs, imported: Service) -> None:
    """消す途中で IAM に断られたときは、role delete をやり直すよう告げる。"""
    aws_stubs.iam.add_response("get_role", a_role(), THE_ROLE)
    missing(aws_stubs.iam, "get_instance_profile", resources.NO_SUCH_ENTITY, **THE_PROFILE)
    in_profiles(aws_stubs)
    role_held(aws_stubs)
    aws_stubs.iam.add_response("get_role", a_role(), THE_ROLE)
    in_profiles(aws_stubs)
    aws_stubs.iam.add_response("list_attached_role_policies", attached(), THE_ROLE)
    aws_stubs.iam.add_response("list_role_policies", {"PolicyNames": []}, THE_ROLE)
    aws_stubs.iam.add_client_error(
        "delete_role",
        service_error_code=resources.DELETE_CONFLICT,
        service_message="Cannot delete entity, must detach all policies first.",
        http_status_code=409,
        expected_params=THE_ROLE,
    )

    with pytest.raises(TemtError) as caught:
        role.delete(imported, yes=True)

    aws_stubs.done()
    assert str(caught.value) == (
        f"cannot delete the role {NAME}: An error occurred (DeleteConflict) when calling the "
        "DeleteRole operation: Cannot delete entity, must detach all policies first.; if "
        "nothing is attached to it, IAM may not show the change yet, so run role delete "
        "again; otherwise detach it by hand and run role delete again"
    )


def test_delete_takes_no_argument() -> None:
    assert cli.parse(["role", "delete", "--svc", "dev-main", "--yes"]).yes is True
    with pytest.raises(UsageError, match="role delete: unexpected argument: web-role"):
        cli.parse(["role", "delete", "--svc", "dev-main", SOURCE])
