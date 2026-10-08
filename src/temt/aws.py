"""AWS から読み取る。boto3 を使う。

書き込みはしない。ここにあるのは、設定ファイルの既定を解決するための読み取り、
apply が AWS に何かを作る前に確かめる読み取り、設定ファイルの ImageId が指す AMI を
引く読み取りである。3 つめは、bake もテンプレートと比べるのに使う。AMI を引く find_image は、
show がテンプレートの AMI を、ami delete が引数の AMI を引くのにも使う。Session を
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
from botocore.exceptions import ClientError

from temt import conf, msg
from temt.conf import Conf
from temt.msg import TemtError
from temt.table import printable

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

# プロファイルを指す環境変数。boto3 はこの順に読み、aws-cli v2 は逆の順に読む
PROFILE_ENV = ("AWS_DEFAULT_PROFILE", "AWS_PROFILE")
# temt が管理していることを示すタグのキー。値はサービス名で、AMI では焼いたサービス
TAG_SERVICE = "temt:service"
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
# 起動テンプレートに入れてよい AMI の状態。手で書いた ImageId が作っている途中の AMI を
# 指していても受け付ける。テンプレートは、起動するまで AMI を使わない
USABLE_IMAGE_STATES = ("available", "pending")
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
    """使うプロファイル。--profile、設定ファイルの AwsProfile の順に決まる。

    どちらも無ければ None で、そのときは boto3 にも aws-cli にもプロファイルを渡さず、
    資格情報は AWS の既定の規則で決まる。aws-cli の環境は proxy が boto3 に揃える。
    """
    return conf.OPTION_PROFILE or values.AwsProfile


def session(values: Conf) -> boto3.Session:
    """プロファイルとリージョンを渡して Session を作る。

    boto3 は profile_name が None のとき何も設定しない（Session.__init__ の
    `if profile_name is not None`）。リージョンは設定ファイルの値が必ず決まっている
    ので、そのまま渡す。
    """
    return boto3.Session(profile_name=profile(values), region_name=values.AwsRegion)


def env_session(region: str | None) -> boto3.Session:
    """--svc の無いコマンドの Session。設定ファイルは読まない。

    プロファイルは --profile で、省けば AWS の既定の規則で決まる。リージョンは、
    aws-cli と同じく region（--region）、AWS_REGION、AWS_DEFAULT_REGION、プロファイルの
    region の順に探す。環境変数は main が取り除く前に conf.ENV_REGION に控えてある。
    boto3 は AWS_REGION を読まないので、temt が読んで渡す。決まらなければエラーにする。
    """
    session = boto3.Session(profile_name=conf.OPTION_PROFILE, region_name=region or conf.ENV_REGION)
    if not session.region_name:
        raise TemtError(
            "no region; give --region, set AWS_REGION, or write region in the AWS profile"
        )
    return session


def account(session: boto3.Session) -> str:
    """資格情報のアカウント。"""
    return field(session.client("sts").get_caller_identity(), "Account")


def environment(region: str | None) -> boto3.Session:
    """--svc の無いコマンドの Session を作り、使う環境を 1 行で知らせる。"""
    session = env_session(region)
    # boto3 はプロファイルを選んでいなくても default と答えるので、選んだときだけ出す。
    # 選んでいなければ、資格情報は環境変数の鍵などから来ていることがある
    chosen = conf.OPTION_PROFILE or any(os.environ.get(name) for name in PROFILE_ENV)
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
        f"write {conf.launch_template_key('SubnetId')} in {printable(str(conf_path))} "
        f"and run apply again"
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
            f"{conf.launch_template_key('SubnetId')} and {conf.launch_template_key('ImageId')} "
            f"from {printable(str(conf_path))} and run apply again"
        )
    item = subnets[0]
    if not item.get("CidrBlock"):
        raise TemtError(f"the subnet {subnet_id} has no IPv4 CIDR")
    return Subnet(vpc_id=field(item, "VpcId"), ipv6=_has_ipv6(item))


@dataclass(frozen=True)
class Ami:
    """AMI のうち、選ぶのと見せるのに要るもの。"""

    image_id: str
    name: str
    # AWS が返す作成日時。ISO 8601 の文字列なので、新旧はそのまま比べられる
    created: str


def own_ami(ec2: EC2Client, svc: str) -> Ami | None:
    """自分のアカウントにある、そのサービスの最新の AMI。1 つも無ければ None。"""
    images = ec2.describe_images(
        Owners=["self"],
        Filters=[
            {"Name": f"tag:{TAG_SERVICE}", "Values": [svc]},
            {"Name": "state", "Values": ["available"]},
        ],
    ).get("Images", [])
    if not images:
        return None
    # 作られた時刻が同じなら ID の辞書順で選び、何度実行しても同じ結果にする
    newest = max(images, key=lambda image: (field(image, "CreationDate"), field(image, "ImageId")))
    return Ami(
        image_id=field(newest, "ImageId"),
        name=field(newest, "Name"),
        created=field(newest, "CreationDate"),
    )


def latest_ami(ec2: EC2Client, ssm: SSMClient, svc: str) -> str:
    """設定ファイルの ImageId に書く、サービスの最新の AMI。

    サービスの AMI があれば、その名前を返す。1 つも無ければ Ubuntu 24.04 の公開 AMI
    の ID を返す。公開 AMI は自分のアカウントのものではないので、名前では引けない。
    """
    found = own_ami(ec2, svc)
    if found is not None:
        return found.name
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
        f"remove {conf.launch_template_key('ImageId')} from {printable(str(conf_path))} "
        "and run apply again to pick one"
    )


def image_id(ec2: EC2Client, value: str) -> str | None:
    """設定ファイルの ImageId の値が指す、使える AMI の ID。

    見つからないとき、使える状態でないとき、名前に使えない文字を含むとき、同じ名前の
    AMI が複数あるときは None。テンプレートと比べるだけの bake が使い、揃っていない
    ものとして apply を促す。ここでは誤りにしない。何がいけないかは、apply の image が告げる。
    """
    try:
        found = find_image(ec2, value)
    except TemtError:
        return None
    if found is None or found.get("State") not in USABLE_IMAGE_STATES:
        return None
    return field(found, "ImageId")


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
