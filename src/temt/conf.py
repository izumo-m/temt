"""サービスの設定ファイル <conf ディレクトリ>/<svc>.toml。

形式は TOML 1.0.0。リソースをまたぐ項目は最上位のキーに、リソースごとの項目はその
リソースのテーブルに置く。値は文字列を原則とし、Defaults と WaitForSsm が真偽値、
受信規則の Port が整数か整数の配列である。[launch-template] は起動テンプレートの中身を
そのままの形で持ち、項目と型は botocore の RequestLaunchTemplateData で決める（ltdata）。
[[role.policy.<ポリシー名>]] は例外で、IAM のポリシーの Statement をテーブルのまま持つ。
値が空文字列の項目と、配列を書ける項目の空の配列は、書かれていないものとして扱う。
項目は「書かれている」か「無い」かのどちらかとする。

conf ディレクトリの共通ファイル defaults.toml は、同じ形で全サービスに共通の値を持つ。
load は設定ファイルを共通ファイルの上に重ねて読む（_merge）。書き換えるのは設定ファイル
だけで、共通ファイルには書かない。

書ける項目と既定は Conf の定義がすべてである。テーブルは dataclass のフィールドで、
フィールド名はテーブル名のハイフンをアンダースコアにしたもの（launch-template なら
launch_template）。同じ名前のテーブルを繰り返し書くテーブルの配列
（[[security-group.ingress]]）は、その dataclass の list のフィールドにする。項目名は
設定ファイルのキーと同じ。読んだ内容はそのまま Conf に
渡して作り、値が決まった項目は update でその都度 Conf とファイルの両方に書く。
空や書きかけのファイルは既定値で読めてしまい、AwsRegion などが黙って既定に戻るので、
どの書き換えも replace_text で置き換える。
書き換えるときは、既に書かれている項目とテーブルの順序、項目に付いたコメントを
変えない。最上位の項目を足すときは最初のテーブルの前に置くので、テーブルの前に
置かれたコメントは、足した項目の前に残る。受信規則は sg のコマンドが edit_ingress で
足し、書き換え、消す。

この module では注釈を文字列にしない（from __future__ import annotations を書かない）。
テーブルの判定が fields() の type に実物のクラスが入っていることに依存する。
"""

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from ipaddress import ip_network
from pathlib import Path
from typing import Any, Final, get_args, get_origin

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT, Comment, InlineTable, Null, Table, Whitespace

from temt import ltdata, policy
from temt.msg import TemtError, UsageError
from temt.names import (
    LABEL_RULE,
    Service,
    defaults_path,
    is_label,
    make_dir,
    replace_text,
)
from temt.table import printable, tilde


class _Unset:
    """未定義を意味する値。どの型の項目にも置ける。

    判定は is で行うので、複製しても同じオブジェクトのままにする。
    """

    def __repr__(self) -> str:
        return "UNSET"

    def __copy__(self) -> "_Unset":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "_Unset":
        return self


# 注釈を Any にして、どの型のフィールドの既定にも書けるようにする。判定は is UNSET で
# 行う。偽値で判じると、将来の真偽値や数値の項目で False や 0 と区別できなくなる
UNSET: Final[Any] = _Unset()

HEADER = "# Written by temt apply. Edit and run apply again to update.\n"

# テーブル名。設定ファイルのキーと、update に渡す位置に使う
LAUNCH_TEMPLATE = "launch-template"
# 位置の区切り。"launch-template.InstanceType" のように書く
SEPARATOR = "."
# 配列の要素を、その対応づけのキーで指す位置の書き方。
# "launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId" のように書く
SELECTOR_RE = re.compile(r"([^.\[\]]+)(?:\[([^=\]]+)=([^\]]*)\])?")
# [launch-template] の中の、temt が値を決めて書く要素。NetworkInterfaces のうち
# DeviceIndex が 0 のもの
PRIMARY = "NetworkInterfaces[DeviceIndex=0]"


def launch_template_key(name: str) -> str:
    """[launch-template] の項目 name の位置。update に渡し、メッセージにも書く。

    name は NetworkInterfaces[DeviceIndex=0].SubnetId のように、テーブルの中や配列の
    要素の中を指してもよい。
    """
    return f"{LAUNCH_TEMPLATE}{SEPARATOR}{name}"


def primary_key(name: str) -> str:
    """NetworkInterfaces の DeviceIndex が 0 の要素の、項目 name の位置。"""
    return launch_template_key(f"{PRIMARY}{SEPARATOR}{name}")


# 位置の 1 つの段。名前と、配列の要素を指すときは（キー、値）
SEGMENT = tuple[str, tuple[str, Any] | None]


def segments(key: str) -> list[SEGMENT]:
    """位置を段に分ける。要素を指す値は、数字だけなら整数とする。"""
    found: list[SEGMENT] = []
    for part in _split_key(key):
        matched = SELECTOR_RE.fullmatch(part)
        if matched is None:
            raise ValueError(f"not a key position: {key}")
        name, inner, value = matched.groups()
        selector = None
        if inner is not None:
            selector = (inner, int(value) if value.isdigit() and value.isascii() else value)
        found.append((name, selector))
    return found


def _split_key(key: str) -> list[str]:
    """位置を . で分ける。[] の中の . では分けない。"""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in key:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == SEPARATOR and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


# ポートの範囲の型。項目の型から、整数か整数の配列として読むことを決める
PORT = tuple[int, int]
# [[security-group.ingress]] の Port の型。icmp の規則ではポートが無く、None になる
RULE_PORT = PORT | None
PORT_MAX = 65535
# InstanceType の既定。書かれていなければ apply が書く。service list も既定として出す
INSTANCE_TYPE = "t3.micro"
# [launch-template] の型。起動テンプレートの中身を、TOML のままの辞書で持つ（ltdata）
LAUNCH_TEMPLATE_DATA = dict[str, Any]
# 文字列の並びの型。[role] の ManagedPolicyArns
STRINGS = list[str]

# 受信規則のプロトコル。名前は aws-cli の --protocol に合わせる
TCP = "tcp"
UDP = "udp"
ICMP = "icmp"
PROTOCOLS = (TCP, UDP, ICMP)

# Statement の並びの型。IAM のポリシーの Statement 1 つを、TOML のテーブルのまま持つ
STATEMENTS = list[dict[str, Any]]
# [role.policy] の型。<ポリシー名> から、そのポリシーの Statement の並びへの対応
POLICIES = dict[str, STATEMENTS]
# アプリに結びつかない、インスタンスそのものの権限のポリシー名。apply が、無ければ
# 既定 EC2_DEFAULT を設定ファイルに書く
EC2 = "ec2"
# [[role.policy.ec2]] の既定。SSM エージェントの最小権限（Session Manager、Run Command）と、
# 自分の階層の SSM パラメータの読み書き。docs/conf.md の [role]
EC2_DEFAULT = """
# Permissions of the instance role that are not tied to one app. temt needs
# the first two statements (SSM Agent; the parameters of this service). Keep
# them and add whatever else this instance may do. apply writes
# [[role.policy.ec2]] only when there is none.
[[role.policy.ec2]]
Action = [
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
]
Resource = "*"

[[role.policy.ec2]]
Action = [
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
    "ssm:PutParameter",
    "ssm:DeleteParameter",
]
Resource = [
    "arn:aws:ssm:*:*:parameter/${temt:name}",
    "arn:aws:ssm:*:*:parameter/${temt:name}/*",
]
"""

# 受信規則の説明に AWS が課す制約。EC2 の API の資料にある IpRange.Description のもの
DESCRIPTION_MAX = 255
DESCRIPTION_CHARS = "a-z, A-Z, 0-9, spaces and ._-:/()#,@[]+=&;{}!$*"
DESCRIPTION_RE = re.compile(r"[a-zA-Z0-9 ._\-:/()#,@\[\]+=&;{}!$*]*")

# apply が新しく作るファイルの末尾に置く、受信規則とロールの権限の書き方の例。apply は
# これを書き換えない
EXAMPLE = """
# Inbound rules of the security group. Uncomment and edit, or run
# 'temt sg add --svc <svc>', to allow direct access; see docs/conf.md.
# [[security-group.ingress]]
# Port = 22
# Cidr = "203.0.113.10/32"
# Description = "home"

# Permissions for an app, as a named IAM policy. Uncomment and edit; see
# docs/conf.md.
# [[role.policy.example]]
# Action = ["s3:GetObject"]
# Resource = "arn:aws:s3:::example-bucket/${temt:name}/*"
"""

# リージョンを指す環境変数。読む順に並べる。temt は動き始めるときに取り除く
REGION_ENV = ("AWS_REGION", "AWS_DEFAULT_REGION")
# --profile で指定されたプロファイル。cli が入れる。設定ファイルの AwsProfile より優先する
OPTION_PROFILE: str | None = None
# --region で指定されたリージョン。cli が入れる。設定ファイルに AwsRegion が無いときだけ
# 使い、あれば同じ値でなければならない
OPTION_REGION: str | None = None
# 取り除く前の環境変数が指していたリージョン。AwsRegion も --region も共通ファイルの
# AwsRegion も無いときに使う
ENV_REGION: str | None = None
# どれでもリージョンが決まらないときのリージョン。--svc を付けたコマンドだけが使う
REGION_DEFAULT = "ap-northeast-1"
# 最上位の項目で、共通ファイルを読むかどうか。共通ファイルには書けない
DEFAULTS = "Defaults"


def cidr(value: str) -> str:
    """送信元の CIDR。AWS が持つ形に揃える。

    AWS は CIDR を正規化して持つので、書かれたままでは、揃っているかどうかを
    比べられない。ホスト部にビットが立っていれば、書き誤りとして知らせる。ゾーン ID
    の付いたアドレス（fe80::1%eth0）は ip_network が通すが、AWS は受けない。
    """
    if not value.isprintable():
        # ip_network は、ゾーン ID の中の制御文字を通し、誤りの文にそのまま書く。誤りの
        # 文は repr で値を書くこともあり、UTF-8 として読めないバイトは \udcff の形で出る
        raise ValueError(f"Cidr must be an IPv4 or IPv6 CIDR: {printable(value)}")
    try:
        network = ip_network(value)
    except ValueError as error:
        raise ValueError(f"Cidr must be an IPv4 or IPv6 CIDR: {printable(str(error))}") from None
    if "%" in str(network):
        raise ValueError(f"Cidr must not carry a zone id: {printable(value)}")
    return str(network)


# コマンドラインの <ポート> の形。ポート 1 つか範囲に、/tcp か /udp を付けたもの
PORT_RE = re.compile(r"([0-9]{1,5})(?:-([0-9]{1,5}))?(?:/([a-z]+))?")
PORT_RULE = (
    f"must be a port or a range such as 22 or 8000-8010, optionally followed by /{TCP} or "
    f"/{UDP}, or {ICMP}"
)


def parse_port(text: str) -> tuple[str, RULE_PORT]:
    """コマンドラインの <ポート> を、プロトコルとポートの範囲に読む。icmp のポートは None。

    読めなければ、PORT_RULE を添えて ValueError にする。
    """
    if text == ICMP:
        return ICMP, None
    found = PORT_RE.fullmatch(text)
    if found is None:
        raise ValueError(PORT_RULE)
    first, last, protocol = found.groups()
    protocol = protocol or TCP
    if protocol not in (TCP, UDP):
        raise ValueError(PORT_RULE)
    low, high = int(first), int(last if last is not None else first)
    if not low <= high <= PORT_MAX:
        raise ValueError(f"must be a port range within 0-{PORT_MAX}")
    return protocol, (low, high)


def port_text(protocol: str, port: RULE_PORT) -> str:
    """規則の PORT の表示。sg add と sg remove の <ポート> にそのまま渡せる形にする。

    sg list と sg diff が使う。port が None なら icmp。
    """
    if port is None:
        return ICMP
    low, high = port
    ports = str(low) if low == high else f"{low}-{high}"
    return f"{ports}/{protocol}"


def check_description(value: str) -> None:
    """受信規則の説明が、AWS が受けるものかを確かめる。"""
    if len(value) > DESCRIPTION_MAX:
        raise ValueError(f"Description must be at most {DESCRIPTION_MAX} characters")
    if not DESCRIPTION_RE.fullmatch(value):
        raise ValueError(f"Description must use only {DESCRIPTION_CHARS}")


@dataclass
class Ingress:
    """[[security-group.ingress]]。セキュリティグループの受信規則 1 つ。

    Cidr は書かなければならない。Port は tcp と udp では書かなければならず、icmp では
    書けない。icmp の Port は None にする。
    """

    Protocol: str = TCP
    Port: RULE_PORT = UNSET
    Cidr: str = UNSET
    Description: str = ""

    def __post_init__(self) -> None:
        """AWS が受ける値かどうかまで、読むときに確かめる。

        apply は足してから消すので、AWS に断られても受信規則が空になることは無いが、
        設定ファイルの値で決まる誤りは、AWS を呼ぶ前に知らせる。
        """
        if self.Protocol not in PROTOCOLS:
            raise ValueError(f"Protocol must be one of {', '.join(PROTOCOLS)}")
        if self.Protocol == ICMP:
            if self.Port is not UNSET and self.Port is not None:
                raise ValueError(f"Port must not be given with Protocol {ICMP}")
            self.Port = None
        elif self.Port is UNSET or self.Port is None:
            raise ValueError("Port is missing")
        if self.Cidr is not UNSET:
            self.Cidr = cidr(self.Cidr)
        check_description(self.Description)


def rule_key(rule: Ingress) -> tuple[str, RULE_PORT, str]:
    """規則を見分ける組。プロトコル、ポートの範囲、CIDR。説明は含めない。"""
    return rule.Protocol, rule.Port, rule.Cidr


@dataclass
class SecurityGroup:
    """[security-group]。セキュリティグループの項目。"""

    ingress: list[Ingress] = field(default_factory=list)

    def __post_init__(self) -> None:
        """同じプロトコル、ポート、CIDR の規則を 2 つ書いていないことを確かめる。

        AWS は規則をプロトコル・ポート・送信元で見分け、説明はその規則に 1 つだけ
        付く。説明違いで 2 つ書くと、AWS に断られるか、片方しか入らずに apply の
        たびに規則が入れ替わる。
        """
        seen: dict[tuple[str, RULE_PORT, str], int] = {}
        for index, one in enumerate(self.ingress):
            key = rule_key(one)
            first = seen.get(key)
            if first is not None:
                raise ValueError(
                    f"ingress[{index}] repeats the protocol, port and CIDR of ingress[{first}]"
                )
            seen[key] = index


@dataclass
class Role:
    """[role]。temt が作るインスタンスロール <名前> の項目。

    policy は、<ポリシー名> から、インラインポリシー temt-<ポリシー名> の Statement の並びへの
    対応。[[role.policy.<ポリシー名>]] の 1 つが Statement 1 つ。テーブルの中身は IAM の
    書式のまま持ち、Effect を補うのも確かめるのも apply に任せる。ManagedPolicyArns は、
    付ける管理ポリシーの ARN の並び。
    """

    policy: POLICIES = field(default_factory=dict)
    ManagedPolicyArns: STRINGS = field(default_factory=list)

    def __bool__(self) -> bool:
        """何か書かれているか。"""
        return bool(self.policy or self.ManagedPolicyArns)


@dataclass
class Conf:
    """設定ファイルの内容。

    並びは docs/conf.md の表と同じにする。初めから既定が決まらない項目には UNSET を
    入れておき、決まった時点で update が Conf とファイルの両方に書く。
    """

    # 使うプロファイル。書かれていなければ None とし、そのときはプロファイルを渡さず、
    # 資格情報は AWS の既定の規則で決まる
    AwsProfile: str | None = None
    # 書かれていなければ、--region か共通ファイルの値（load が入れる）、無ければ UNSET の
    # ままで、aws.session が環境変数、AWS のプロファイル、REGION_DEFAULT の順に決める
    AwsRegion: str = UNSET
    # false なら共通ファイルを読まない
    Defaults: bool = True
    # false なら start は SSM エージェントを待たない
    WaitForSsm: bool = True
    # 起動テンプレートの中身。書かれていない項目は、apply が値を決めて書くもの（ImageId、
    # InstanceType など）も無い。何を決めるかは apply と resources が持つ
    launch_template: LAUNCH_TEMPLATE_DATA = field(default_factory=dict)
    security_group: SecurityGroup = field(default_factory=SecurityGroup)
    role: Role = field(default_factory=Role)


def take_region_env() -> None:
    """リージョンを指す環境変数を、環境から取り除く。

    どのリージョンを使うかは temt が決めて、boto3 と aws-cli に明示的に渡す。環境に
    残っていると、boto3 と aws-cli がそれぞれ解決してしまう。boto3 は
    AWS_DEFAULT_REGION だけを読み、aws-cli は AWS_REGION を先に読むので、片方だけを
    設定していると食い違う。決める順の中で環境変数も使うので、取り除く前に aws-cli と
    同じ順で読んだ値を ENV_REGION に控える。
    """
    global ENV_REGION
    ENV_REGION = next((os.environ[name] for name in REGION_ENV if os.environ.get(name)), None)
    for name in REGION_ENV:
        os.environ.pop(name, None)


def _attribute(key: str) -> str:
    """テーブル名に当たるフィールド名。"""
    return key.replace("-", "_")


def _table_key(attribute: str) -> str:
    """テーブルのフィールド名に当たる、設定ファイルのキー。_attribute の逆。"""
    return attribute.replace("_", "-")


def _table_class(kind: Any) -> type | None:
    """kind がテーブルなら、その dataclass。"""
    return kind if is_dataclass(kind) and isinstance(kind, type) else None


def _element(kind: Any) -> type | None:
    """kind がテーブルの配列なら、その要素の dataclass。"""
    if get_origin(kind) is not list:
        return None
    (argument,) = get_args(kind)
    return _table_class(argument)


def _keys(cls: type) -> dict[str, Any]:
    """cls に書けるキーと、そのフィールドの型。

    テーブルとテーブルの配列のキーはフィールド名のアンダースコアをハイフンにしたもの。
    項目のキーはフィールド名そのもの。
    """
    keys: dict[str, Any] = {}
    for one in fields(cls):
        is_table = (
            _table_class(one.type) is not None
            or _element(one.type) is not None
            or one.type == LAUNCH_TEMPLATE_DATA
        )
        keys[_table_key(one.name) if is_table else one.name] = one.type
    return keys


def _port(value: Any) -> PORT:
    """ポートの範囲。整数は 1 つのポート、長さ 2 の整数の配列は範囲とする。

    真偽値は Python では整数の仲間だが、ポートではないので受けない。
    """
    numbers = value if isinstance(value, list) else [value, value]
    if len(numbers) != 2 or not all(
        isinstance(one, int) and not isinstance(one, bool) for one in numbers
    ):
        raise ValueError("must be an integer or an array of two integers")
    low, high = numbers
    if not 0 <= low <= high <= PORT_MAX:
        raise ValueError(f"must be a port range within 0-{PORT_MAX}")
    return low, high


def _json(value: Any) -> bool:
    """value が JSON にできる値か。TOML の日時、nan、inf は JSON に無いので受けない。"""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str | bool | int):
        return True
    if isinstance(value, list):
        return all(_json(item) for item in value)
    if isinstance(value, dict):
        return all(_json(item) for item in value.values())
    return False


def _statements(value: Any) -> STATEMENTS:
    """Statement の並び。空でないテーブルの配列で、JSON にでき、知らない変数が無いこと。"""
    if not isinstance(value, list):
        raise ValueError(" must be an array of tables")
    if not value:
        raise ValueError(" must have at least one table")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"[{index}] must be a table")
        if not _json(item):
            raise ValueError(
                f"[{index}] must hold only strings, finite numbers, booleans, arrays and tables"
            )
        found = policy.unknown(item, policy.VARIABLES)
        if found is not None:
            raise ValueError(f"[{index}] uses an unknown variable: {printable(found)}")
    return value


def _policies(value: Any, path: Path, where: str) -> POLICIES:
    """[role.policy] のテーブル。<ポリシー名> ごとに、Statement のテーブルの配列であること。

    where は誤りに添える位置 "role.policy"。角かっこ 1 つの [role.policy.<ポリシー名>]
    はテーブルになるので、[[...]] と書くよう告げる。
    """
    if not isinstance(value, dict):
        raise TemtError(f"{printable(tilde(path))}: {where} must be a table")
    made: POLICIES = {}
    for name, statements in value.items():
        place = f"{where}{SEPARATOR}{name}"
        if not is_label(name):
            # 名前は利用者が書いたまま出さず、引用して ASCII にする。. や改行を含む
            # 名前も、階層と取り違えずに読める
            raise TemtError(
                f"{printable(tilde(path))}: {where}{SEPARATOR}{json.dumps(name)}: "
                f"the policy name {LABEL_RULE}"
            )
        if isinstance(statements, dict):
            raise TemtError(
                f"{printable(tilde(path))}: {place} must be an array of tables; "
                f"write [[{place}]] once per statement"
            )
        try:
            made[name] = _statements(statements)
        except ValueError as error:
            raise TemtError(f"{printable(tilde(path))}: {place}{error}") from None
    return made


def _make(cls: type, table: Any, path: Path, prefix: str, *, complete: bool = False) -> Any:
    """テーブルの中身から cls を作る。誤りはファイル名と位置を添えて知らせる。

    complete なら、書かれていない項目が残っていることを誤りとする。テーブルの配列の
    要素は、既定の無い項目をすべて書かなければならない。
    """
    if not isinstance(table, dict):
        raise TemtError(
            f"{printable(tilde(path))}: {prefix.removesuffix(SEPARATOR)} must be a table"
        )
    try:
        made = cls(**_read(table, cls, path, prefix))
    except ValueError as error:
        raise TemtError(f"{printable(tilde(path))}: {prefix}{error}") from None
    if complete:
        for one in fields(made):
            if getattr(made, one.name) is UNSET:
                raise TemtError(f"{printable(tilde(path))}: {prefix}{one.name} is missing")
    return made


def _read(table: dict[str, Any], cls: type, path: Path, prefix: str) -> dict[str, Any]:
    """テーブルの中身を cls のフィールドの対応に変える。誤りはファイル名を添えて知らせる。

    prefix は誤りに添える位置で、最上位なら空、テーブルなら "launch-template." の
    ようにテーブル名と区切り。
    """
    keys = _keys(cls)
    values: dict[str, Any] = {}
    for key, value in table.items():
        if key not in keys:
            raise TemtError(f"{printable(tilde(path))}: unknown key: {prefix}{printable(key)}")
        kind = keys[key]
        where = f"{prefix}{key}"
        table_class = _table_class(kind)
        if table_class is not None:
            values[_attribute(key)] = _make(table_class, value, path, f"{where}{SEPARATOR}")
            continue
        element = _element(kind)
        if element is not None:
            if not isinstance(value, list):
                raise TemtError(f"{printable(tilde(path))}: {where} must be an array of tables")
            values[_attribute(key)] = [
                _make(element, item, path, f"{where}[{index}]{SEPARATOR}", complete=True)
                for index, item in enumerate(value)
            ]
            continue
        if kind == POLICIES:
            values[key] = _policies(value, path, where)
            continue
        if kind == LAUNCH_TEMPLATE_DATA:
            values[_attribute(key)] = _launch_template(value, path)
            continue
        if kind == STRINGS:
            if not isinstance(value, list) or not all(
                isinstance(one, str) and one for one in value
            ):
                raise TemtError(
                    f"{printable(tilde(path))}: {where} must be an array of non-empty strings"
                )
            if value:
                values[key] = list(value)
            continue
        if kind == RULE_PORT and value == []:
            # 配列を書ける項目の空の配列は、書かれていないものとする
            continue
        if kind == RULE_PORT:
            try:
                values[key] = _port(value)
            except ValueError as error:
                raise TemtError(f"{printable(tilde(path))}: {where} {error}") from None
            continue
        if kind is bool:
            if not isinstance(value, bool):
                raise TemtError(f"{printable(tilde(path))}: {where} must be true or false")
            values[key] = value
            continue
        if not isinstance(value, str):
            raise TemtError(f"{printable(tilde(path))}: {where} must be a string")
        if not value:
            continue
        values[key] = value
    return values


def _launch_template(value: Any, path: Path) -> dict[str, Any]:
    """[launch-template] の中身。botocore の RequestLaunchTemplateData の形で確かめ、空の値を
    落とす（ltdata.check）。対応づけのキーが重なっていないことも確かめる。

    AWS を呼ばないコマンドは boto3 が無くても動くので、botocore が読み込めなければ、
    テーブルであることだけを確かめ、空の値を落とす。
    """
    if not isinstance(value, dict):
        raise TemtError(f"{printable(tilde(path))}: {LAUNCH_TEMPLATE} must be a table")
    try:
        checked = ltdata.check(value, LAUNCH_TEMPLATE)
    except ImportError:
        return ltdata.without_blanks(value)
    except ValueError as error:
        raise TemtError(f"{printable(tilde(path))}: {error}") from None
    try:
        ltdata.check_keys(checked, LAUNCH_TEMPLATE)
    except ValueError as error:
        raise TemtError(f"{printable(tilde(path))}: {error}") from None
    return checked


def _document(text: str, path: Path) -> dict[str, Any]:
    """設定ファイルの中身を、TOML のまま辞書に読む。項目は確かめない。"""
    try:
        return tomlkit.parse(text).unwrap()
    except TOMLKitError as error:
        raise TemtError(f"{printable(tilde(path))}: {printable(str(error))}") from None


def _built(document: dict[str, Any], path: Path) -> Conf:
    """_document で読んだ中身から Conf を作る。誤りはファイル名を添えて知らせる。"""
    try:
        return Conf(**_read(document, Conf, path, ""))
    except ValueError as error:
        raise TemtError(f"{printable(tilde(path))}: {error}") from None


def _text(path: Path) -> str | None:
    """ファイルの中身。無ければ None。UTF-8 として読めなければ TemtError。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as error:
        raise TemtError(f"{printable(tilde(path))}: not valid UTF-8: {error.reason}") from None


def _common() -> tuple[dict[str, Any], Conf] | None:
    """共通ファイルの中身と、それだけを読んだ Conf。ファイルが無ければ None。

    中身の誤りは、共通ファイルの名前を添えて知らせる。行き先の無いシンボリックリンクも
    誤りとする。
    """
    path = defaults_path()
    text = _text(path)
    if text is None:
        # 行き先の無いシンボリックリンクを無いものとすると、共通ファイルの AwsProfile を
        # 使わずに環境変数のプロファイルへ黙って移ってしまう
        if path.is_symlink():
            raise TemtError(f"{printable(tilde(path))}: broken symlink")
        return None
    document = _document(text, path)
    if DEFAULTS in document:
        # 共通ファイルを読むかどうかは、サービスごとに設定ファイルで決める
        raise TemtError(f"{printable(tilde(path))}: {DEFAULTS} cannot be written in this file")
    return document, _built(document, path)


def common_values() -> dict[str, Any]:
    """共通ファイルの最上位に書かれている項目の値。既定は補わない。

    --svc の無いコマンドが AwsProfile と AwsRegion を読む。ファイルが無ければ空。
    """
    found = _common()
    if found is None:
        return {}
    return _read(found[0], Conf, defaults_path(), "")


def common_rules() -> list[Ingress]:
    """共通ファイルの受信規則。ファイルが無ければ空。"""
    found = _common()
    return [] if found is None else found[1].security_group.ingress


def _blank(value: Any) -> bool:
    """書かれていないものとして扱う値か。空文字列と空の配列。"""
    return value == "" or value == []


def _merge(cls: type, base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """cls のテーブルの中身 base に over を重ねる。どちらも確かめ済みの、TOML のままの辞書。

    項目は over に書かれていれば over の値にする。テーブルは中の項目ごとに重ね、テーブルの
    配列（受信規則）は base の後ろに over を足し、[role.policy] はポリシー名ごとに over
    の方を使う。
    """
    keys = _keys(cls)
    merged = dict(base)
    for key, value in over.items():
        kind = keys[key]
        table_class = _table_class(kind)
        if table_class is not None:
            merged[key] = _merge(table_class, base.get(key, {}), value)
        elif kind == LAUNCH_TEMPLATE_DATA:
            merged[key] = ltdata.merge(
                ltdata.without_blanks(base.get(key, {})), ltdata.without_blanks(value)
            )
        elif _element(kind) is not None:
            merged[key] = [*base.get(key, []), *value]
        elif kind == POLICIES:
            merged[key] = {**base.get(key, {}), **value}
        elif not _blank(value):
            merged[key] = value
    return merged


def _combined(document: dict[str, Any], path: Path) -> Conf:
    """設定ファイル path の中身を、共通ファイルの上に重ねて読む。

    どちらのファイルも、まず単独で確かめる。誤りをそれぞれのファイル名で知らせるためで
    ある。共通ファイルと同じ受信規則を書いていれば、重ねると 2 つになるので誤りにする。
    設定ファイルが Defaults = false なら、共通ファイルを読まない。
    """
    own = _built(document, path)
    if not own.Defaults:
        return own
    common = _common()
    if common is None:
        return own
    base, shared = common
    keys = {rule_key(one) for one in shared.security_group.ingress}
    for index, one in enumerate(own.security_group.ingress):
        if rule_key(one) in keys:
            raise TemtError(
                f"{printable(tilde(path))}: {SECURITY_GROUP}{SEPARATOR}{INGRESS}[{index}] "
                f"repeats a rule in {printable(tilde(defaults_path()))}; remove one of them"
            )
    return _built(_merge(Conf, base, document), path)


def _conf_in(text: str, path: Path, *, common: bool = True) -> Conf:
    """設定ファイル path の中身 text を読む。common なら共通ファイルの上に重ねる。

    読めなければ TemtError。
    """
    document = _document(text, path)
    values = _combined(document, path) if common else _built(document, path)
    _option_region(values, document, path)
    return values


def _option_region(values: Conf, document: dict[str, Any], path: Path) -> None:
    """--region を values に映す。

    設定ファイルに AwsRegion が書かれていれば、その値だけを使い、違う --region は引数の
    誤りにする。書かれていなければ、--region を共通ファイルの AwsRegion より先にする。
    最初の apply と ami import が、決まった値を AwsRegion に書く。
    """
    if OPTION_REGION is None:
        return
    written = document.get("AwsRegion")
    if isinstance(written, str) and written:
        if written != OPTION_REGION:
            raise UsageError(
                f"{path.stem} is in {printable(written)}; --region {printable(OPTION_REGION)} "
                f"differs"
            )
        return
    values.AwsRegion = OPTION_REGION


def write_example(service: Service) -> None:
    """設定ファイルの末尾に、受信規則とアプリの権限の書き方の例をコメントで足す。

    apply が新しく作ったファイルにだけ足す。既定ではどちらも無いので、一から
    書かずに済むようにする。
    """
    path = service.conf_path
    replace_text(path, path.read_text(encoding="utf-8") + EXAMPLE)


def ec2_default() -> STATEMENTS:
    """EC2_DEFAULT を読んだ、[[role.policy.ec2]] の既定の Statement の並び。"""
    return list(tomlkit.parse(EC2_DEFAULT).unwrap()["role"]["policy"][EC2])


def with_ec2_default(service: Service, values: Conf) -> tuple[str, Conf]:
    """設定ファイルの末尾に [[role.policy.ec2]] の既定を足した中身と、それを読んだ Conf。

    values は、いまの設定ファイルを読んだものでなければならない。足したあとの中身を
    読んで照合し、読めないか（role をインラインテーブルで書いてあるなど）ほかの項目が
    変わっていれば、手で足すよう告げて止める。書き込みはしない。
    """
    path = service.conf_path
    text = path.read_text(encoding="utf-8")
    added = text + ("" if text.endswith("\n") else "\n") + EC2_DEFAULT
    try:
        done: Conf | None = _conf_in(added, path)
    except TemtError:
        done = None
    if (
        done is None
        or done.role.policy != {**values.role.policy, EC2: ec2_default()}
        # AwsRegion は、書かれていなければ aws.session が決めて values に入れている
        or replace(done, role=values.role, AwsRegion=values.AwsRegion) != values
    ):
        raise TemtError(
            f"cannot add the default [[role.policy.{EC2}]] to {printable(tilde(path))}; "
            f"add it by hand (see docs/conf.md)"
        )
    return added, done


def write_ec2_default(service: Service, values: Conf) -> None:
    """[[role.policy.ec2]] の既定を設定ファイルの末尾に足し、Conf にも入れる。

    足すかどうかは呼ぶ側が決める。足せなければ with_ec2_default が止める。
    """
    added, done = with_ec2_default(service, values)
    replace_text(service.conf_path, added)
    values.role.policy[EC2] = done.role.policy[EC2]


def load(service: Service, *, missing_ok: bool = False, common: bool = True) -> Conf:
    """設定ファイルを、共通ファイルの上に重ねて読む。

    missing_ok なら、ファイルが無いとき共通ファイルと既定だけの Conf を返す。common が
    偽なら、共通ファイルを重ねず、設定ファイルに書かれたものだけを読む。
    """
    path = service.conf_path
    text = _text(path)
    if text is None:
        if not missing_ok:
            raise TemtError(
                f"no config file: {printable(tilde(path))}; "
                f"run 'temt apply --svc {service.svc}' first"
            )
        text = ""
    return _conf_in(text, path, common=common)


def written(service: Service, key: str) -> Any:
    """設定ファイルに書かれている項目の値。ファイルか項目が無ければ None。

    key は update と同じく項目の位置。書かれたままの値を返し、既定は補わない。
    """
    try:
        text = service.conf_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return _lookup(tomlkit.parse(text).unwrap(), key)


def _lookup(document: dict[str, Any], key: str) -> Any:
    """TOML のままの辞書 document の、位置 key の値。無ければ None。"""
    holder: Any = document
    for name, selector in segments(key):
        if not isinstance(holder, dict):
            return None
        holder = holder.get(name)
        if selector is not None:
            holder = _element_of(holder, selector)
    return holder


def _element_of(items: Any, selector: tuple[str, Any]) -> Any:
    """配列 items の中で、キーと値が selector の要素。無ければ None。"""
    if not isinstance(items, list):
        return None
    inner, value = selector
    return next((one for one in items if isinstance(one, dict) and one.get(inner) == value), None)


def source(service: Service, key: str) -> Path:
    """項目 key の値を書いたファイル。直すよう告げるときに名前を出す。

    設定ファイルに無く（空の値を含む）、共通ファイルにあれば、共通ファイルとする。
    どちらにも無ければ、設定ファイルとする。Defaults = false のサービスは、共通ファイルを
    読まないので、設定ファイルとする。
    """
    value = written(service, key)
    if value is not None and not _blank(value):
        return service.conf_path
    if written(service, DEFAULTS) is False:
        return service.conf_path
    common = _common()
    if common is not None:
        value = _lookup(common[0], key)
        if value is not None and not _blank(value):
            return defaults_path()
    return service.conf_path


def replace_table(document: Any, name: str, table: Any | None) -> None:
    """最上位のテーブル name の中身を table に置き換える。table が None なら消す。

    書いてあった場所に置き、最後の要素の後ろにあったコメントと空行（次の見出しの説明で
    あることが多い）は残す。消すときは、前のテーブルの後ろに戻す。role import が [role] を
    丸ごと置き換えるのに使う。
    """
    if name not in document:
        if table is not None:
            document[name] = table
        return
    old = document[name]
    moved = _trailing(_last_table(old))
    if table is None:
        before = _previous_table(document, name)
        del document[name]
        if before is not None:
            for item in moved:
                before.value.append(None, item)
        return
    for key in list(old.keys()):
        del old[key]
    for key, item in table.items():
        old[key] = item
    last = _last_table(old)
    for item in moved:
        last.value.append(None, item)


def lookup_data(data: dict[str, Any], key: str) -> Any:
    """TOML のままの辞書 data の、位置 key の値。無ければ None。"""
    return _lookup(data, key)


def lookup(values: Conf, key: str) -> Any:
    """Conf の、位置 key の値。無ければ None（最上位の項目では UNSET のこともある）。"""
    holder: Any = values
    for name, selector in segments(key):
        if is_dataclass(holder):
            holder = getattr(holder, name if hasattr(holder, name) else _attribute(name), None)
        elif isinstance(holder, dict):
            holder = holder.get(name)
        else:
            return None
        if selector is not None:
            holder = _element_of(holder, selector)
    return holder


def assign(values: Conf, key: str, value: Any) -> None:
    """Conf の項目を value にする。設定ファイルには書かない。key は update と同じ。

    配列の要素を指す段は、要素が無ければ、そのキーと値だけを持つ要素を足す。
    """
    parts = segments(key)
    holder: Any = values
    for index, (name, selector) in enumerate(parts):
        last = index == len(parts) - 1
        if is_dataclass(holder):
            if last:
                setattr(holder, name if hasattr(holder, name) else _attribute(name), value)
                return
            holder = getattr(holder, _attribute(name))
            continue
        if last and selector is None:
            holder[name] = value
            return
        if selector is None:
            holder = holder.setdefault(name, {})
            continue
        items = holder.setdefault(name, [])
        found = _element_of(items, selector)
        if found is None:
            found = {selector[0]: selector[1]}
            items.append(found)
        holder = found


class _Inline(Exception):
    """書く先がインラインテーブルか配列の中にある。tomlkit では形を保って書き足せない。"""


def _item(value: Any) -> Any:
    """設定ファイルに書く値。テーブルはインラインテーブルにする（IamInstanceProfile）。"""
    if isinstance(value, dict):
        return ltdata.inline(value)
    return value


def _new_table(parent: Any, name: str, table: Any) -> None:
    """テーブル parent に、テーブル（かテーブルの配列）name を足す。

    parent の中で最後に書かれたテーブルの後ろにあるコメントと空行は、次の見出しの説明で
    あることが多いので、足したものの後ろに移す。前のテーブルとの間には空行を置く。
    """
    moved = _trailing(_last_table(parent))
    head = table[0] if isinstance(table, AoT) else table
    # 中身の無いテーブルの見出しの後ろには、空行を足さない
    head.trivia.indent = "\n" if len(parent) else ""
    parent.append(name, table)
    body = table[-1] if isinstance(table, AoT) else table
    for item in moved:
        body.value.append(None, item)


def _write_into(document: Any, key: str, value: Any) -> bool:
    """tomlkit の文書 document の位置 key に value を書く。書き換えたら True。

    無いテーブルとテーブルの配列の要素は足す。書く先がインラインテーブルかインラインの
    配列の中なら _Inline を上げる。ただし、既に同じ値が書かれていれば、どこにあっても
    書き換えない（利用者が手で書いたあとは止めない）。
    """
    if _lookup(document.unwrap(), key) == value:
        return False
    parts = segments(key)
    table: Any = document
    for index, (name, selector) in enumerate(parts):
        last = index == len(parts) - 1
        if isinstance(table, InlineTable):
            raise _Inline
        if last and selector is None:
            if table.get(name) == value:
                return False
            _put(table, name, _item(value))
            return True
        if selector is None:
            if name not in table:
                if isinstance(table, Table):
                    _new_table(table, name, tomlkit.table())
                else:
                    table[name] = tomlkit.table()
            table = table[name]
            continue
        inner, wanted = selector
        items = table.get(name)
        if items is None:
            element = tomlkit.table()
            element[inner] = wanted
            aot = tomlkit.aot()
            aot.append(element)
            if isinstance(table, Table):
                _new_table(table, name, aot)
            else:
                table.append(name, aot)
            table = table[name][-1]
            continue
        if not isinstance(items, AoT):
            raise _Inline
        found = next((one for one in items if one.get(inner) == wanted), None)
        if found is None:
            moved = _trailing(_last_table(items[-1]))
            found = tomlkit.table()
            found[inner] = wanted
            found.trivia.indent = "\n"
            items.append(found)
            found = items[-1]
            for item in moved:
                found.value.append(None, item)
        table = found
    return False


def toml_value(value: Any) -> str:
    """値の TOML の書き方。テーブルはインラインテーブルにする。"""
    return tomlkit.dumps({"x": _item(value)}).removeprefix("x = ").strip()


def _cannot_write(key: str, value: Any, path: Path, command: str = "apply") -> TemtError:
    """インラインテーブルの中などに書けないときの誤り。手で書くよう告げる。"""
    shown = toml_value(value)
    return TemtError(
        f"cannot write {key} in {printable(tilde(path))}, as it would go into an inline table "
        f"or array; write {key} = {printable(shown)} there by hand and run {command} again"
    )


def write_document(document: Any, key: str, value: Any, path: Path, command: str) -> bool:
    """tomlkit の文書 document（設定ファイル path の中身）の位置 key に value を書く。
    書き換えたら True。

    書く先がインラインテーブルの中などで書けなければ、手で書いてから command をやり直す
    よう告げて止める。role import が、設定ファイルを書き換える前に中身を作るのに使う。
    """
    try:
        return _write_into(document, key, value)
    except _Inline:
        raise _cannot_write(key, value, path, command) from None


def parse_text(text: str, path: Path) -> Conf:
    """設定ファイル path の中身 text を、共通ファイルの上に重ねて読む。読めなければ止める。"""
    return _conf_in(text, path)


def check_writable(service: Service, key: str, value: Any) -> None:
    """位置 key に書けることを、書かずに確かめる。apply が確認の前に呼ぶ。"""
    path = service.conf_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    try:
        _ = _write_into(tomlkit.parse(text), key, value)
    except _Inline:
        raise _cannot_write(key, value, path) from None


def update(service: Service, values: Conf, key: str, value: Any) -> bool:
    """Conf と設定ファイルの項目を更新する。ファイルを書き換えたら True を返す。

    key は項目の位置で、最上位なら "AwsRegion"、テーブルの中なら
    "launch-template.InstanceType" のようにテーブル名と区切りを前に置く。配列の要素の中は
    "launch-template.NetworkInterfaces[DeviceIndex=0].SubnetId" のように、要素を対応づけの
    キーで指す。書かれている値が既に同じなら、ファイルには触らない。書き換えるときも、
    既に書かれている項目の順序と、項目に付いたコメントは変えない。最上位の項目は最初の
    テーブルの前に置く。テーブルに足す項目は、その最後の項目の次に置く（_put）。ファイルが
    無ければ見出しのコメントを付けて作り、テーブルや要素が無ければ足す。書く先が
    インラインテーブルの中なら、手で書くよう告げて止める。
    """
    path = service.conf_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        make_dir(path.parent)
        text = HEADER
    document = tomlkit.parse(text)
    try:
        changed = _write_into(document, key, value)
    except _Inline:
        raise _cannot_write(key, value, path) from None
    if changed:
        replace_text(path, tomlkit.dumps(document))
    # 書けてから Conf に映す。書けずに止まったときは、Conf も元のまま
    assign(values, key, value)
    return changed


# テーブルの中で、項目の間に置かれるコメントと空行
TRIVIA = (Comment, Whitespace)
# 受信規則のテーブルの配列の位置
SECURITY_GROUP = "security-group"
INGRESS = "ingress"
# tomlkit が思わぬ形で書き換えに失敗したときに上げる例外。書いた結果の照合とあわせて、
# 書き換えられないファイルとして扱う
EDIT_ERRORS = (TOMLKitError, AttributeError, KeyError, IndexError, TypeError, ValueError)


def _trailing(table: Any) -> list[Any]:
    """テーブル（テーブルの配列の要素も）の、最後の項目より後ろにあるコメントと空行を
    取り外して返す。

    tomlkit は、次の見出しの前にあるコメントと空行を、前のテーブルの中身として持つ。
    テーブルを消すときや、後ろに要素や項目を足すときは、それらを次のものの前に残す
    ために取り外す。
    """
    body = table.value.body
    count = len(body)
    while count and body[count - 1][0] is None and isinstance(body[count - 1][1], (*TRIVIA, Null)):
        count -= 1
    # 消した項目の跡に tomlkit が残す Null は、書き出しても何も出ないので捨てる
    items = [item for _, item in body[count:] if not isinstance(item, Null)]
    del body[count:]
    return items


def _has_comment(items: Sequence[Any]) -> bool:
    """取り外したものにコメントがあるか。空行だけなら、残さなくてよい。"""
    return any(isinstance(item, Comment) for item in items)


def _joined(before: Sequence[Any], after: Sequence[Any]) -> list[Any]:
    """消した規則の前後にあったコメントと空行をつなぐ。

    空行が続けば 1 つにする。コメントが無ければ、後ろ（消した規則の次に来るもの）の
    空行だけを残し、後ろに無ければ前の空行を残す。規則を消すたびに空行が溜まらず、
    次のテーブルの前の空行も消えないようにするためである。ファイルの末尾に余った
    空行は edit_ingress が落とす。
    """
    if not _has_comment(before) and not _has_comment(after):
        return list(after or before)
    joined: list[Any] = []
    for item in [*before, *after]:
        if isinstance(item, Whitespace) and joined and isinstance(joined[-1], Whitespace):
            continue
        joined.append(item)
    return joined


def _put(table: Any, name: str, value: Any) -> None:
    """テーブルの項目を書く。

    無い項目を足すときは、テーブルの後ろにあるコメントと空行より前、最後の項目の次に
    置く。そのコメントは次の見出しの説明か、コメントにした例であることが多い。後ろに
    足すと、例の見出しを戻したときに、足した項目がそのテーブルのものになってしまう。
    """
    if name in table or not isinstance(table, Table):
        table[name] = value
        return
    moved = _trailing(table)
    table[name] = value
    for item in moved:
        table.value.append(None, item)


def _rule_table(rule: Ingress, inline: bool) -> Any:
    """規則 1 つを設定ファイルに書くテーブルにする。

    項目は conf.md の表の順に並べる。Protocol は、既定の tcp なら書かない。
    """
    table: Any = tomlkit.inline_table() if inline else tomlkit.table()
    if rule.Protocol != TCP:
        table["Protocol"] = rule.Protocol
    if rule.Port is not None:
        low, high = rule.Port
        table["Port"] = low if low == high else [low, high]
    table["Cidr"] = rule.Cidr
    if rule.Description:
        table["Description"] = rule.Description
    return table


def _followed(document: Any) -> bool:
    """[security-group] の後ろに、ほかのテーブルがあるか。"""
    keys = [key for key, _ in document.body if key is not None]
    return bool(keys) and keys[-1] != SECURITY_GROUP


def _append(document: Any, rule: Ingress) -> None:
    """規則を足す。

    最後の規則の後ろに足す。規則が無ければ [security-group] の末尾に、それも無ければ
    ファイルの末尾に足す。最後の規則の後ろにあったコメントは、足した規則の後ろに移し、
    2 つの規則の間には空行を置く。
    """
    if SECURITY_GROUP not in document:
        group = tomlkit.table(is_super_table=True)
        group.append(INGRESS, tomlkit.aot())
        document.append(SECURITY_GROUP, group)
    group = document[SECURITY_GROUP]
    rules = group.get(INGRESS)
    if rules is None:
        rules = tomlkit.aot()
        group.append(INGRESS, rules)
    if not isinstance(rules, AoT):
        # ingress = [{ ... }] と書かれた配列。配列の中のコメントは tomlkit に任せる
        rules.append(_rule_table(rule, inline=True))
        return
    table = _rule_table(rule, inline=False)
    if not len(rules):
        # 後ろにテーブルが続くなら、その前に空行を置く
        if _followed(document):
            table.value.append(None, Whitespace("\n"))
        rules.append(table)
        return
    last = rules[-1]
    moved = _trailing(last)
    # ファイルの末尾に改行が無ければ、改行も補う
    last.value.append(None, Whitespace("\n" if last.as_string().endswith("\n") else "\n\n"))
    for item in moved:
        table.value.append(None, item)
    rules.append(table)
    # tomlkit は、この実行で作った配列に足すとき、見出しの前に自分で改行を置く。空行は
    # 上で置いたので、重ねない
    added = rules[-1]
    added.trivia.indent = added.trivia.indent.removeprefix("\n")


def _remove(document: Any, index: int) -> None:
    """index 番めの規則を消す。

    その規則の前後にあったコメントは、消した場所に残す。空行は重ねない。規則が無く
    なれば ingress を消す。[security-group] の見出しを書いてあれば、見出しとその下の
    コメントを残す。見出しが無ければ、書き足さずに [security-group] ごと消し、残す
    コメントは前のテーブルの後ろに戻す。前にテーブルが無ければ、コメントを残すために見出しを書く。
    """
    group = document[SECURITY_GROUP]
    rules = group[INGRESS]
    if not isinstance(rules, AoT):
        del rules[index]
        return
    moved = _trailing(rules[index])
    del rules[index]
    if index:
        # 前の規則の後ろ、次の規則か次のテーブルの前に置く
        before = rules[index - 1]
        for item in _joined(_trailing(before), moved):
            before.value.append(None, item)
    elif len(rules):
        # 次の規則の見出しの前に置く。前の空行は、前のテーブルが持っている
        if _has_comment(moved):
            while moved and isinstance(moved[0], Whitespace):
                moved = moved[1:]
            following = rules[0]
            text = "".join(item.as_string() for item in moved)
            following.trivia.indent = text + following.trivia.indent
    elif not group.is_super_table() or len(group.value.body) > 1:
        # 見出しを書いた [security-group] は、見出しとその下のコメントを残す
        del group[INGRESS]
        for item in _joined(_trailing(group), moved):
            group.value.append(None, item)
    else:
        # 見出しは書かれていないので書き足さず、コメントは前のテーブルの後ろに戻す
        before = _previous_table(document)
        if before is None and _has_comment(moved):
            del group[INGRESS]
            for item in moved:
                group.value.append(None, item)
            return
        del document[SECURITY_GROUP]
        if before is not None:
            for item in _joined(_trailing(before), moved):
                before.value.append(None, item)


def _leaf(item: Any) -> Any:
    """item の中で最後に書かれたテーブル。見出しの無いテーブルは、中の最後のテーブルをたどる。"""
    if isinstance(item, AoT):
        return _leaf(item[-1]) if len(item) else None
    if not isinstance(item, Table):
        return None
    if not item.is_super_table():
        return item
    inner = [one for key, one in item.value.body if key is not None]
    return _leaf(inner[-1]) if inner else None


def _last_table(table: Any) -> Any:
    """table の中で最後に書かれたテーブル。中のテーブルとテーブルの配列の要素もたどる。

    中にテーブルが無ければ table そのもの。その後ろにあるコメントと空行は、次の見出しの
    前に置かれたものである。
    """
    inner = [item for key, item in table.value.body if isinstance(item, (Table, AoT))]
    if not inner:
        return table
    last = inner[-1]
    if isinstance(last, AoT):
        return _last_table(last[-1]) if len(last) else table
    return _last_table(last)


def _previous_table(document: Any, name: str = SECURITY_GROUP) -> Any:
    """ファイルの中で、テーブル name（既定は [security-group]）の直前に書かれたテーブル。
    無ければ None。
    """
    before = None
    for key, item in document.body:
        if key is not None and key.key == name:
            return before
        leaf = _leaf(item) if key is not None else None
        # 中身の無いテーブルは偽になるので、None と比べる
        if leaf is not None:
            before = leaf
    return None


def edit_ingress(
    service: Service,
    values: Conf,
    *,
    added: Sequence[Ingress] = (),
    described: Mapping[int, str] | None = None,
    removed: Sequence[int] = (),
) -> None:
    """設定ファイルの受信規則を書き換え、Conf にも同じ変更を映す。

    values は、共通ファイルを重ねずに読んだ設定ファイル（load の common=False）である。

    described は規則の位置から新しい説明への対応で、空の説明は Description を消す。
    removed は消す規則の位置。位置はどちらも書き換える前の values の並びで数える。
    added は末尾に足す規則。説明を書き換えてから消し、最後に足す。1 回で読み書きし、
    既に書かれている項目の順序とコメントは変えない。

    位置は values で数えるので、読み直したファイルの規則が values と違えば（読んでから
    書くまでの間に直されたなど）、書かずに終了する。書き換えた結果も読み直し、規則が
    期待と違うか、規則のほかの項目が変わっていれば、書かずに終了する。規則を他のテーブルを挟んで分けて書いたファイル
    などは、tomlkit が思うように書き換えられないからである。
    """
    path = service.conf_path
    text = path.read_text(encoding="utf-8")
    rules = values.security_group.ingress
    current = _conf_in(text, path, common=False)
    if current.security_group.ingress != rules:
        raise TemtError(f"{printable(tilde(path))} changed while running; run the command again")
    wanted = [replace(one) for one in rules]
    for index, description in (described or {}).items():
        wanted[index].Description = description
    for index in sorted(removed, reverse=True):
        del wanted[index]
    wanted.extend(added)
    document = tomlkit.parse(text)
    try:
        for index, description in (described or {}).items():
            table = document[SECURITY_GROUP][INGRESS][index]
            if description:
                _put(table, "Description", description)
            elif "Description" in table:
                del table["Description"]
        for index in sorted(removed, reverse=True):
            _remove(document, index)
        for rule in added:
            _append(document, rule)
        written = tomlkit.dumps(document)
        # 足したテーブルを消し切ると、その前に tomlkit が置いた空行が末尾に残る。元に
        # 無かった末尾の空行は落とす
        if written.endswith("\n\n") and not text.endswith("\n\n"):
            written = written.rstrip("\n") + "\n"
        done: Conf | None = _conf_in(written, path, common=False)
    except (TemtError, *EDIT_ERRORS):
        written, done = "", None
    if (
        done is None
        or done.security_group.ingress != wanted
        or replace(done, security_group=current.security_group) != current
    ):
        raise TemtError(
            f"cannot edit the rules in {printable(tilde(path))}; "
            f"edit [[{SECURITY_GROUP}.{INGRESS}]] by hand"
        )
    replace_text(path, written)
    rules[:] = wanted
