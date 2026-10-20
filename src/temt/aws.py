"""AWS から読み取る。boto3 を使う。

書き込みはしない。ここにあるのは、設定ファイルの既定を解決するための読み取り、
apply が AWS に何かを作る前に確かめる読み取り、AMI を引く読み取りである。AMI を引く
find_image は、apply が ImageId、--image-id、起動テンプレートの AMI を、show がテンプレートの
AMI を、ami delete が引数の AMI を引くのに使う。サービスの最新の AMI を探す newest_image は、
apply が起動テンプレートを作るときと、show と destroy が見せるときに使う。Session を
作るのもここで、--svc を付けたコマンドには設定ファイルから、無いコマンドには
--profile と --region から作る（env_session）。後者が知らせるアカウントも STS から読み、
environment がそれを 1 行で知らせる。AWS が返した時刻や deploy の記録の時刻を表示の
形にする when と cell_time、応答からタグの値を引く tag もここに置く。

botocore の例外は、AMI が無いことを表す誤りのほかは、ここでは捕まえない。cli.run が
ClientError と BotoCoreError の両方を一度だけ包む。プロファイルが無いときの
ProfileNotFound は後者である。誤りの名前を取り出す error_code は、そのリソースが
無いことを見分ける他の module も使う。NO_SUCH_IMAGE は bake も使う。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError, ProfileNotFound

from temt import conf, msg
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import PREFIX, defaults_path
from temt.table import printable, tilde

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

# プロファイルを指す環境変数。boto3 はこの順に読み、aws-cli v2 は逆の順に読む
PROFILE_ENV = ("AWS_DEFAULT_PROFILE", "AWS_PROFILE")
# temt が管理していることを示すタグのキー。値はサービス名で、AMI では焼いたサービス
TAG_SERVICE = "temt:service"
# インスタンスロールに付ける、リージョンのタグのキーの頭。後ろにリージョンが続き、値は空。
# そのリージョンの設定に [role] があり、apply したことを表す
TAG_REGION = "temt:region:"
# プロファイルを選んでいないときの、環境を知らせる行の表示
NO_PROFILE = "(none)"
# 読めない時刻の表示
UNKNOWN = "(unknown)"
# 表示する時刻の形。手元のローカル時刻で、秒まで
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# その AMI が無いか、まだ見えないときに describe-images が返す誤りの名前
NO_SUCH_IMAGE = "InvalidAMIID.NotFound"
# ID の形が AWS に合わないときに describe-images が返す誤りの名前
MALFORMED_IMAGE = "InvalidAMIID.Malformed"
# 登録を解除された AMI の状態。解除の直後は、しばらく describe-images に見える
DEREGISTERED = "deregistered"
# 起動テンプレートに入れてよい AMI の状態。ImageId や --image-id が作っている途中の AMI を
# 指していても受け付ける。テンプレートは、起動するまで AMI を使わない
USABLE_IMAGE_STATES = ("available", "pending")
AVAILABLE = "available"
PENDING = "pending"
# サービスの AMI の名前の、temt-<svc>- に続く時刻の桁数（YYMMDDHHMM）
IMAGE_TIME_DIGITS = 10
# Canonical が公開している Ubuntu 24.04 の AMI ID
UBUNTU_AMI_PARAMETER = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
)
# AMI の ID の形。16 進数 8 桁か 17 桁。設定ファイルの ImageId がこれに合わなければ、
# AMI の名前とみなす
AMI_ID_RE = re.compile(r"ami-(?:[0-9a-f]{8}|[0-9a-f]{17})")
# 名前で AMI を探すときに使えない文字。describe-images の絞り込みはこれらを展開する
WILDCARDS = ("*", "?")
# サブネット ID の形。絞り込みの値は * と ? を展開するので、渡す前に確かめる
SUBNET_ID_RE = re.compile(r"subnet-[0-9a-f]{8,17}")
# IPv6 の CIDR が結び付いている状態
IPV6_ASSOCIATED = "associated"


def profile(values: Conf) -> str | None:
    """使うプロファイル。設定ファイルの AwsProfile、--profile、共通ファイルの AwsProfile の
    順に決まる。設定ファイルに書いた AwsProfile と違う --profile は、conf.load が引数の誤りに
    している。

    どれも無ければ None で、そのときは boto3 にも aws-cli にもプロファイルを渡さず、
    資格情報は AWS の既定の規則で決まる。aws-cli の環境は proxy が boto3 に揃える。
    """
    return conf.OPTION_PROFILE or values.AwsProfile


def session(values: Conf) -> boto3.Session:
    """プロファイルとリージョンを渡して Session を作る。

    boto3 は profile_name が None のとき何も設定しない（Session.__init__ の
    `if profile_name is not None`）。リージョンは、設定ファイルの AwsRegion、--region、
    共通ファイルの AwsRegion の順に conf.load が決めている。どれも無ければ、ここで
    環境変数、AWS のプロファイルの region、conf.REGION_DEFAULT の順に決め、values にも
    入れる。環境変数は main が取り除く前に conf.ENV_REGION に控えてある。
    """
    resolve_region(values)
    return boto3.Session(profile_name=profile(values), region_name=values.AwsRegion)


def resolve_region(values: Conf) -> None:
    """設定ファイルからリージョンが決まらなかったとき（AwsRegion が UNSET）、環境変数、
    AWS のプロファイルの region、conf.REGION_DEFAULT の順に決めて values に入れる。
    """
    if values.AwsRegion is not conf.UNSET:
        return
    region = conf.ENV_REGION
    if not region:
        try:
            region = boto3.Session(profile_name=profile(values)).region_name
        except ProfileNotFound:
            # 無いプロファイルは、続けて作る Session がその誤りで止める
            region = None
    values.AwsRegion = region or conf.REGION_DEFAULT


def _env_profile(common: Mapping[str, Any]) -> str | None:
    """--svc の無いコマンドのプロファイル。--profile、共通ファイルの AwsProfile の順。"""
    return conf.OPTION_PROFILE or common.get("AwsProfile")


def env_session(region: str | None) -> boto3.Session:
    """--svc の無いコマンドの Session。サービスの設定ファイルは読まず、共通ファイルを読む。

    プロファイルは --profile、共通ファイルの AwsProfile の順で、どちらも無ければ AWS の
    既定の規則で決まる。リージョンは region（--region）、共通ファイルの AwsRegion、
    AWS_REGION、AWS_DEFAULT_REGION、プロファイルの region の順に探す。共通ファイルを
    環境変数より先にするのは、作業中のプロジェクトが設定した環境変数に temt がつられない
    ようにするためである。環境変数は main が取り除く前に conf.ENV_REGION に控えてある。
    boto3 は AWS_REGION を読まないので、temt が読んで渡す。決まらなければエラーにする。
    """
    common = conf.common_values()
    session = boto3.Session(
        profile_name=_env_profile(common),
        region_name=region or common.get("AwsRegion") or conf.ENV_REGION,
    )
    if not session.region_name:
        raise TemtError(
            f"no region; give --region, write AwsRegion in "
            f"{printable(tilde(defaults_path()))}, set AWS_REGION, or write region in the "
            f"AWS profile"
        )
    return session


def account(session: boto3.Session) -> str:
    """資格情報のアカウント。"""
    return field(session.client("sts").get_caller_identity(), "Account")


def confirm(session: boto3.Session, question: str, *, yes: bool) -> None:
    """--svc を付けたコマンドの確認。question（? を除いた問い）に、扱うリージョンと
    アカウントを添えて問う。

    --profile や --region を付け忘れて、別のリージョンやアカウントを扱おうとしていれば、
    ここで気づける。アカウントは問うときだけ STS から引く。yes なら問わないので引かない。
    """
    msg.confirm(
        lambda: (
            f"{question} in {printable(str(session.region_name))} (account {account(session)})?"
        ),
        yes=yes,
    )


def environment(region: str | None) -> boto3.Session:
    """--svc の無いコマンドの Session を作り、使う環境を 1 行で知らせる。"""
    session = env_session(region)
    # boto3 はプロファイルを選んでいなくても default と答えるので、選んだときだけ出す。
    # 選んでいなければ、資格情報は環境変数の鍵などから来ていることがある
    chosen = _env_profile(conf.common_values()) or any(os.environ.get(name) for name in PROFILE_ENV)
    # 表だけを標準出力に残すよう、標準エラー出力に書く
    msg.tell(
        f"account {account(session)}, region {printable(session.region_name)}, "
        f"profile {printable(session.profile_name) if chosen else NO_PROFILE}\n",
        side=True,
    )
    return session


def cell_time(value: Any) -> str:
    """時刻を、手元のローカル時刻の YYYY-MM-DD HH:MM:SS で表す。読めなければ空。

    boto3 は多くの時刻を datetime で返すが、AMI の CreationDate は文字列のまま返す
    ので、どちらも受ける。deploy の記録の時刻（UTC の ISO 8601）も文字列で受ける。
    list の表は空を - と出す。
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return ""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return ""
    try:
        return value.astimezone().strftime(TIME_FORMAT)
    except (OverflowError, ValueError, OSError):
        # 手で書き換えた deploy の記録の、手元の時刻に直せない年など
        return ""


def when(value: Any) -> str:
    """AWS が返した時刻。cell_time と同じ形で表す。読めなければ (unknown)。"""
    return cell_time(value) or UNKNOWN


def tag(item: Mapping[str, Any], key: str) -> str:
    """AWS の応答の、そのキーのタグの値。無ければ空。"""
    return next(
        (str(one.get("Value") or "") for one in item.get("Tags") or [] if one.get("Key") == key),
        "",
    )


def region_tag(region: str) -> str:
    """リージョン region のタグのキー。"""
    return f"{TAG_REGION}{region}"


def tagged_regions(item: Mapping[str, Any]) -> list[str]:
    """AWS の応答の、リージョンのタグが付いたリージョン。付いた順に並べる。"""
    return [
        key.removeprefix(TAG_REGION)
        for one in item.get("Tags") or []
        if (key := str(one.get("Key") or "")).startswith(TAG_REGION) and key != TAG_REGION
    ]


def field(item: Mapping[str, Any], key: str) -> str:
    """AWS の応答から文字列の項目を取り出す。無ければエラーにする。"""
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise TemtError(f"AWS did not return {key}")
    return value


def error_code(e: ClientError) -> str:
    """botocore の例外が持つ、AWS が返した誤りの名前。

    そのリソースが無いことを表す誤りだけを見分けたい module が使う。
    """
    return e.response.get("Error", {}).get("Code", "")


def _write_subnet(conf_path: Path) -> str:
    """既定のサブネットが決まらないときに伝えること。"""
    return (
        f"write {conf.primary_key('SubnetId')} in {printable(tilde(conf_path))} and run apply again"
    )


def public_subnet(ec2: EC2Client, region: str, conf_path: Path) -> str:
    """既定 VPC のサブネットのうち、パブリック IP が付き、AZ 名が辞書順で最も小さいもの。

    同じ AZ に複数あるときは ID の辞書順で選ぶ。AWS は応答の順序を保証しないので、
    何度実行しても同じ結果になるようにする。
    """
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}]).get("Vpcs", [])
    if not vpcs:
        raise TemtError(f"no default VPC in {printable(region)}; {_write_subnet(conf_path)}")
    subnets = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [field(vpcs[0], "VpcId")]},
            {"Name": "map-public-ip-on-launch", "Values": ["true"]},
        ]
    ).get("Subnets", [])
    if not subnets:
        raise TemtError(
            f"no public subnet in the default VPC in {printable(region)}; "
            f"{_write_subnet(conf_path)}"
        )
    first = min(
        subnets,
        key=lambda subnet: (field(subnet, "AvailabilityZone"), field(subnet, "SubnetId")),
    )
    return field(first, "SubnetId")


@dataclass(frozen=True)
class Subnet:
    """サブネットのうち、起動テンプレートを組むのに要るもの。"""

    vpc_id: str
    # IPv6 の CIDR が結び付いているかどうか
    ipv6: bool


def _has_ipv6(item: Mapping[str, Any]) -> bool:
    """サブネットに IPv6 の CIDR が結び付いているか。

    結び付けの途中や外れかけのものは、まだ使えないので数えない。
    """
    return any(
        (one.get("Ipv6CidrBlockState") or {}).get("State") == IPV6_ASSOCIATED
        for one in item.get("Ipv6CidrBlockAssociationSet") or []
    )


def subnet(ec2: EC2Client, subnet_id: str, conf_path: Path) -> Subnet:
    """サブネットがあることを確かめ、その VPC と IPv6 の CIDR の有無を返す。

    IPv4 の CIDR を持たないサブネット（IPv6 だけのサブネット）は扱わない。見つから
    なければ、別のアカウントで作ろうとしていることもあるので、選び直す方法を添える。
    """
    if not SUBNET_ID_RE.fullmatch(subnet_id):
        raise TemtError(f"not a subnet id: {printable(subnet_id)}")
    found = ec2.describe_subnets(Filters=[{"Name": "subnet-id", "Values": [subnet_id]}])
    subnets = found.get("Subnets", [])
    if not subnets:
        raise TemtError(
            f"no such subnet: {subnet_id}; for another account, remove "
            f"{conf.primary_key('SubnetId')} and {conf.launch_template_key('ImageId')}, and "
            f"the other items that point into this account (Groups, IamInstanceProfile, "
            f"KeyName, ...), from {printable(tilde(conf_path))} and run apply again"
        )
    item = subnets[0]
    if not item.get("CidrBlock"):
        raise TemtError(f"the subnet {subnet_id} has no IPv4 CIDR")
    return Subnet(vpc_id=field(item, "VpcId"), ipv6=_has_ipv6(item))


def _image_name_re(svc: str) -> re.Pattern[str]:
    """サービスの AMI の名前の形。temt-<svc>- に 10 桁の数字だけが続く。"""
    return re.compile(re.escape(f"{PREFIX}-{svc}-") + f"[0-9]{{{IMAGE_TIME_DIGITS}}}")


def newest_image(ec2: EC2Client, svc: str) -> Mapping[str, Any] | None:
    """自分のアカウントにある、そのサービスの最新の AMI。無ければ None。

    名前が temt-<svc>- に 10 桁の数字が続く形にちょうど合い、available か pending の
    もののうち、作成日時が最新のもの。タグでは探さない。コピーした AMI にはタグが無い
    ことがある。前方一致にしないのは、サービス dev に dev-main の AMI を混ぜないためである。
    並べるのは作成日時で、名前の時刻は使わない。コピーした AMI の作成日時はコピーした
    時刻で、名前の時刻は手元の時刻である。
    """
    images = ec2.describe_images(
        Owners=["self"],
        Filters=[
            # ? は 0 か 1 文字に合う。形にちょうど合うかは、受け取ってから確かめる
            {"Name": "name", "Values": [f"{PREFIX}-{svc}-" + "?" * IMAGE_TIME_DIGITS]},
            {"Name": "state", "Values": list(USABLE_IMAGE_STATES)},
        ],
    ).get("Images", [])
    pattern = _image_name_re(svc)
    images = [one for one in images if pattern.fullmatch(str(one.get("Name") or ""))]
    if not images:
        return None
    # 作成日時が同じなら ID の辞書順で選び、何度実行しても同じ結果にする。AWS が返す作成
    # 日時は ISO 8601 の文字列なので、新旧はそのまま比べられる
    return max(images, key=lambda image: (field(image, "CreationDate"), field(image, "ImageId")))


def ubuntu_image_id(ssm: SSMClient) -> str:
    """Ubuntu 24.04 の公開 AMI の ID。Canonical の SSM の公開パラメータから読む。

    公開 AMI は自分のアカウントのものではないので、名前では引けない。
    """
    return field(ssm.get_parameter(Name=UBUNTU_AMI_PARAMETER)["Parameter"], "Value")


def is_image_id(value: str) -> bool:
    """ImageId の値や ami delete の引数が、AMI の ID の形か。"""
    return AMI_ID_RE.fullmatch(value) is not None


def find_image(ec2: EC2Client, value: str, *, own: bool = False) -> Mapping[str, Any] | None:
    """ImageId の値や ami delete の引数（ID か名前）が指す AMI。見つからなければ None。

    名前は、自分のアカウントの AMI からだけ探す。名前はアカウントとリージョンの中で
    一意だが、持ち主をまたぐと重なることがあるからである。ID は、own なら自分の
    アカウントの AMI から、そうでなければ公開や共有の AMI も含めて探す。登録を解除
    された AMI は、しばらく見えていても、無いものとして扱う。
    """
    if is_image_id(value):
        try:
            if own:
                found = ec2.describe_images(ImageIds=[value], Owners=["self"]).get("Images") or []
            else:
                found = ec2.describe_images(ImageIds=[value]).get("Images") or []
        except ClientError as e:
            if error_code(e) not in (NO_SUCH_IMAGE, MALFORMED_IMAGE):
                raise
            return None
    else:
        if any(char in value for char in WILDCARDS):
            raise TemtError(f"not an AMI id or name: {printable(value)}")
        found = ec2.describe_images(
            Owners=["self"], Filters=[{"Name": "name", "Values": [value]}]
        ).get("Images", [])
    found = [one for one in found if one.get("State") != DEREGISTERED]
    if len(found) > 1:
        raise TemtError(f"more than one AMI is named {printable(value)}")
    return found[0] if found else None


def _pick_again(conf_path: Path) -> str:
    """ImageId の AMI が使えないときに伝えること。"""
    return (
        f"write another AMI in {conf.launch_template_key('ImageId')} of "
        f"{printable(tilde(conf_path))}, or remove it and choose one with --image-id"
    )


def image(ec2: EC2Client, value: str, conf_path: Path) -> Mapping[str, Any]:
    """設定ファイルの ImageId の値が指す AMI。使えなければエラーにする。

    AMI が登録解除されたか、別のアカウントで作ろうとしていることがあるので、選び直す
    方法を添える。
    """
    found = find_image(ec2, value)
    if found is None:
        raise TemtError(f"no such AMI: {printable(value)}; {_pick_again(conf_path)}")
    state = found.get("State")
    if state not in USABLE_IMAGE_STATES:
        raise TemtError(f"the AMI {printable(value)} is {state}; {_pick_again(conf_path)}")
    return found


@dataclass(frozen=True)
class RootVolume:
    """AMI のルートボリューム。起動テンプレートで大きさを指すのに要るもの。"""

    # AMI の RootDeviceName。起動テンプレートの BlockDeviceMappings の DeviceName にする
    device: str
    # GiB
    size: int


def root_volume(found: Mapping[str, Any]) -> RootVolume | None:
    """AMI のルートボリューム。EBS のルートボリュームが無ければ None。

    BlockDeviceMappings のうち、DeviceName が RootDeviceName のものを見る。
    """
    device = found.get("RootDeviceName")
    for mapping in found.get("BlockDeviceMappings") or []:
        size = (mapping.get("Ebs") or {}).get("VolumeSize")
        if device and mapping.get("DeviceName") == device and isinstance(size, int):
            return RootVolume(device=device, size=size)
    return None
