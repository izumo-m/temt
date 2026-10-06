"""ami list、ami prune、ami delete の本体。temt の AMI を挙げ、消す。

temt の AMI は、自分のアカウントにあって temt:service タグを持つ AMI である。AMI が
使われているかは、そのアカウントとリージョンにあるすべての起動テンプレートの、
すべてのバージョンが指しているかで決める。ami list はそれを添えて挙げ、ami prune は
どこからも指されていない AMI の登録を解除し、スナップショットも消す。SSM パラメータ
（resolve:ssm:）で AMI を指すバージョンは、AWS に解決させる。どの AMI を指すか
分からないバージョンがあれば、ami prune は何も消さない。ami delete は指した 1 つを消す。
指されているか、指されているか分からないか、bake の途中なら、force が要る。

サービスを与えれば、環境はその設定ファイルで決まり、扱う AMI はそのサービスのものに
限る。無ければ、--profile と --region で決まる環境の、すべての temt の AMI を扱う。
どちらでも、AMI が指されているかは、環境のすべてのテンプレートで確かめる。

ami prune と force なしの ami delete は、bake が待っている pending の AMI を消さない。
bake は AMI が available になってからテンプレートを向けるので、その短い間の AMI は
どのテンプレートからも指されない。ami prune は、確認の後、AMI を 1 つ消すたびに
その AMI のサービスのロックを取り、テンプレートを読み直してから消すので、同じマシンで
並行する bake の AMI は消さない。取れなければ、その AMI を残して次へ進む。ami delete
はロックを取らない。

ami list の表は table が標準出力に、ami prune と ami delete の一覧と確認は標準エラー
出力に書く。boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。
botocore の例外もそこで包む。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from temt import aws, conf, lock, msg, resources, table
from temt.aws import error_code, field
from temt.msg import TemtError, UsageError
from temt.names import PREFIX, Service, ssh_dir
from temt.resources import TemplateVersion

if TYPE_CHECKING:
    from types_boto3_ec2.client import EC2Client
    from types_boto3_ec2.type_defs import FilterTypeDef


# bake の途中の AMI の状態
PENDING = "pending"
# ami prune が候補にする状態。pending は bake の途中なので除く
PRUNE_STATES = ("available", "failed")
# ami prune と ami delete の一覧とメッセージで、名前の無い AMI の表示
NO_NAME = "(no name)"
# スナップショットを消せたときに AWS が返す値
SNAPSHOT_DELETED = "success"
# 起動テンプレートの ImageId が SSM パラメータを指すときの前置き
SSM_ALIAS = "resolve:ssm:"

COLUMNS = table.Columns(
    defaults=("ID", "NAME", "STATE", "CREATED", "USED"),
    extras=("SERVICE",),
)


@dataclass(frozen=True)
class Image:
    """temt の AMI のうち、挙げるのと消すのに要るもの。"""

    image_id: str
    name: str
    state: str
    # AWS が返す作成日時。ISO 8601 の文字列
    created: str
    # temt:service タグの値。焼いたサービス
    service: str


def _image(item: Mapping[str, Any]) -> Image:
    """describe-images が返す AMI 1 つから Image を作る。"""
    return Image(
        image_id=field(item, "ImageId"),
        name=str(item.get("Name") or ""),
        state=str(item.get("State") or ""),
        created=str(item.get("CreationDate") or ""),
        service=aws.tag(item, aws.TAG_SERVICE),
    )


def temt_images(ec2: EC2Client, svc: str | None) -> list[Image]:
    """自分のアカウントにある temt の AMI。svc があれば、そのサービスのものだけ。

    サービス名、作成日時、ID の順に並べる。登録を解除した直後に見える deregistered の
    AMI は除く。
    """
    wanted: FilterTypeDef = (
        {"Name": "tag-key", "Values": [aws.TAG_SERVICE]}
        if svc is None
        else {"Name": f"tag:{aws.TAG_SERVICE}", "Values": [svc]}
    )
    found = [
        _image(item)
        for page in ec2.get_paginator("describe_images").paginate(Owners=["self"], Filters=[wanted])
        for item in page.get("Images", [])
        if item.get("State") != aws.DEREGISTERED
    ]
    return sorted(found, key=lambda one: (one.service, one.created, one.image_id))


@dataclass
class References:
    """起動テンプレートのバージョンが指す AMI。"""

    # AMI の ID ごとに、それを指すバージョン
    used: dict[str, list[TemplateVersion]]
    # どの AMI を指すか分からなかったバージョンと、その ImageId と理由
    unknown: list[str]


def _resolve(ec2: EC2Client, name: str, number: Any) -> tuple[str | None, str]:
    """SSM パラメータで AMI を指すバージョンの、今の AMI の ID。

    分からなければ ID の代わりに None を返し、理由を添える。読むあいだにテンプレートが
    消えたときの誤りは、references がテンプレートごと飛ばせるよう、そのまま上げる。
    """
    try:
        found = ec2.describe_launch_template_versions(
            LaunchTemplateName=name, Versions=[str(number)], ResolveAlias=True
        ).get("LaunchTemplateVersions", [])
    except ClientError as e:
        if error_code(e) == resources.NO_SUCH_TEMPLATE:
            raise
        return None, error_code(e)
    image_id = (found[0].get("LaunchTemplateData") or {}).get("ImageId") if found else None
    if isinstance(image_id, str) and aws.is_image_id(image_id):
        return image_id, ""
    return None, f"resolved to {table.printable(image_id) if image_id else 'nothing'}"


def references(ec2: EC2Client) -> References:
    """起動テンプレートのバージョンが指す AMI を集める。

    temt のものに限らず、そのアカウントとリージョンにあるすべてのテンプレートの、
    すべてのバージョンを見る。読むあいだに消えたテンプレートは飛ばす。
    """
    found = References(used={}, unknown=[])
    for name, versions in resources.all_versions(ec2):
        try:
            for version in versions:
                number = version.get("VersionNumber")
                image_id = (version.get("LaunchTemplateData") or {}).get("ImageId")
                if not image_id:
                    continue
                label = TemplateVersion(name, number)
                if image_id.startswith(SSM_ALIAS):
                    resolved, reason = _resolve(ec2, name, number)
                    if resolved is None:
                        found.unknown.append(f"{label} ({image_id}: {reason})")
                        continue
                    image_id = resolved
                found.used.setdefault(image_id, []).append(label)
        except ClientError as e:
            if error_code(e) != resources.NO_SUCH_TEMPLATE:
                raise
    return found


def _row(image: Image) -> tuple[str, str, str, str]:
    """ami prune と ami delete の一覧で、AMI 1 つの列。"""
    return (image.image_id, image.name or NO_NAME, image.state, aws.when(image.created))


def _table_row(image: Image, refs: References | None) -> dict[str, str]:
    """ami list の表の、AMI 1 つの行。refs は USED を出すときだけ渡す。"""
    return {
        "ID": image.image_id,
        "NAME": image.name,
        "STATE": image.state,
        "CREATED": aws.cell_time(image.created),
        "USED": resources.cells(refs.used.get(image.image_id)) if refs else "",
        "SERVICE": image.service,
    }


def _ec2(service: Service | None, region: str | None) -> tuple[EC2Client, str | None]:
    """扱う環境の EC2 と、扱う AMI のサービス名。

    service があれば、その設定ファイルで環境が決まり、そのサービスの AMI だけを扱う。
    無ければ、--profile と --region で決まる環境の、すべての temt の AMI を扱う。
    """
    if service is None:
        return aws.environment(region).client("ec2"), None
    return aws.session(conf.load(service)).client("ec2"), service.svc


def list_images(service: Service | None, region: str | None, spec: str | None) -> None:
    """temt の AMI を、指しているテンプレートのバージョンとともに表で挙げる。

    service があれば、そのサービスの AMI だけを挙げる。すべての起動テンプレートを
    読むのは、USED を出すときだけである。
    """
    chosen = COLUMNS.choose(spec)
    ec2, svc = _ec2(service, region)
    images = temt_images(ec2, svc)
    refs = None
    if images and "USED" in chosen:
        refs = references(ec2)
        for one in refs.unknown:
            msg.warn(f"cannot resolve the AMI of {one}")
    table.write(chosen, [_table_row(one, refs) for one in images])


def _prunable(image: Image) -> bool:
    """ami prune が候補にする AMI か。bake が作った名前と、消してよい状態のもの。"""
    name = rf"{re.escape(PREFIX)}-{re.escape(image.service)}-[0-9]{{10}}"
    return image.state in PRUNE_STATES and re.fullmatch(name, image.name) is not None


def _count(number: int) -> str:
    """AMI の数の表示。"""
    return f"{number} AMI" if number == 1 else f"{number} AMIs"


def _deregister(ec2: EC2Client, image: Image) -> None:
    """AMI の登録を解除し、スナップショットも消す。消せなかったスナップショットは警告する。"""
    result = ec2.deregister_image(ImageId=image.image_id, DeleteAssociatedSnapshots=True)
    msg.info(f"deregistered {image.image_id} ({image.name or NO_NAME})")
    for snapshot in result.get("DeleteSnapshotResults") or []:
        code = snapshot.get("ReturnCode")
        if code != SNAPSHOT_DELETED:
            msg.warn(f"snapshot {snapshot.get('SnapshotId')} of {image.image_id}: {code}")


def _owner(image: Image) -> Service | None:
    """AMI の temt:service のサービス。サービス名の規則に合わなければ None。

    規則に合わない名前のサービスは、temt のコマンドが扱えないので、ロックも無い。
    """
    try:
        return Service(image.service)
    except UsageError:
        return None


def _prune_one(ec2: EC2Client, image: Image) -> str | None:
    """ami prune で AMI 1 つを消す。消せたら None、残したらその理由を返す。

    その AMI のサービスのロックを取り、取れなければ残す。取ったら、すべてのテンプレートを
    読み直し、指されていれば残す。確認を待つあいだに、並行する bake がテンプレートを
    向けたかもしれないからである。ロックは消し終えたら離す。AWS に断られたら残す。
    接続の失敗などの BotoCoreError は捕まえず、そこで止める。AWS 側では解除が済んで
    いることがあり、続けても同じ失敗を重ねるだけだからである。
    """
    owner = _owner(image)
    holding = nullcontext(True) if owner is None else lock.try_hold(owner)
    with holding as taken:
        if not taken:
            return f"another temt command is running for {PREFIX}-{image.service}"
        refs = references(ec2)
        used = refs.used.get(image.image_id)
        if used:
            return f"used by {', '.join(map(str, used))}"
        if refs.unknown:
            return f"cannot resolve the AMI of: {'; '.join(refs.unknown)}"
        try:
            _deregister(ec2, image)
        except ClientError as e:
            return f"cannot deregister it: {table.aws_error(e)}"
    return None


def prune(service: Service | None, region: str | None, *, yes: bool) -> None:
    """どの起動テンプレートのどのバージョンからも指されていない temt の AMI を消す。

    service があれば、そのサービスの AMI だけを候補にする。他のサービスが ImageId で
    指していることがあるので、指されているかは、すべてのテンプレートで確かめる。
    消すものだけを挙げて確認する。yes なら確認しない。1 つずつ _prune_one で消し、
    残したものは警告して残りを続け、最後にまとめて知らせる。
    """
    # ロックの場所は ssh ディレクトリの下にある。TEMT_SSH_DIR の誤りは、確認の後で
    # AMI を消す途中に止まらないよう、ここで先に出す
    _ = ssh_dir()
    ec2, svc = _ec2(service, region)
    # AMI を先に集め、テンプレートを後で読む。逆にすると、テンプレートを読んだ後に
    # available になり、bake が向けた AMI を、指されていないものとして挙げうる
    candidates = [one for one in temt_images(ec2, svc) if _prunable(one)]
    refs = references(ec2)
    if refs.unknown:
        raise TemtError(
            f"cannot resolve the AMI of: {'; '.join(refs.unknown)}; nothing is deregistered"
        )
    doomed = [one for one in candidates if one.image_id not in refs.used]
    if not doomed:
        msg.tell("no AMIs to deregister\n")
        return
    rows = [_row(one) for one in doomed]
    msg.tell("deregister:\n")
    msg.columns(rows)
    msg.confirm(f"deregister {_count(len(doomed))} and their snapshots?", yes=yes)
    left: list[str] = []
    for one in doomed:
        why = _prune_one(ec2, one)
        if why is not None:
            msg.warn(f"keeping {one.image_id}: {why}")
            left.append(one.image_id)
    if left:
        raise TemtError(f"kept {len(left)} of {_count(len(doomed))}: {', '.join(left)}")


def _reasons_to_keep(image: Image, refs: References) -> list[str]:
    """ami delete が force なしでは消さない理由。無ければ空。"""
    reasons: list[str] = []
    used = refs.used.get(image.image_id)
    if used:
        reasons.append(f"used by {', '.join(map(str, used))}")
    if refs.unknown:
        reasons.append(f"cannot resolve the AMI of: {'; '.join(refs.unknown)}")
    if image.state == PENDING:
        reasons.append("pending; bake may be creating it")
    return reasons


def delete(
    target: str, service: Service | None, region: str | None, *, force: bool, yes: bool
) -> None:
    """ID か名前で指した temt の AMI の登録を解除し、スナップショットも消す。

    ID でも名前でも、自分のアカウントの AMI からだけ探す。service があれば、そのサービスの
    AMI でないものは、見つからないものとする。起動テンプレートのバージョンが
    指しているか、どの AMI を指すか分からないバージョンがあるか、bake の途中（pending）
    なら、force が無ければ何も消さずにエラーにする。force なら、その理由を警告として
    挙げてから消す。消す前に確認する。yes なら確認しない。
    """
    ec2, svc = _ec2(service, region)
    item = aws.find_image(ec2, target, own=True)
    if item is None:
        raise TemtError(f"no such AMI in this account: {table.printable(target)}")
    if svc is not None and aws.tag(item, aws.TAG_SERVICE) != svc:
        raise TemtError(f"no such AMI of the service {svc}: {table.printable(target)}")
    if not any(tag.get("Key") == aws.TAG_SERVICE for tag in item.get("Tags") or []):
        raise TemtError(
            f"the AMI {table.printable(target)} has no {aws.TAG_SERVICE} tag; "
            "temt deregisters only AMIs that it created"
        )
    image = _image(item)
    reasons = _reasons_to_keep(image, references(ec2))
    if reasons and not force:
        raise TemtError(
            f"{image.image_id} ({image.name or NO_NAME}): {'; '.join(reasons)}; "
            "pass --force to deregister it anyway"
        )
    for reason in reasons:
        msg.warn(f"{image.image_id}: {reason}")
    msg.tell("deregister:\n")
    msg.columns([_row(image)])
    msg.confirm(f"deregister {image.image_id} and its snapshots?", yes=yes)
    _deregister(ec2, image)
