"""bake の本体。設計の手順 1〜9 を順に行う。

インスタンスを停止して AMI にし、AMI が使えるようになってから、設定ファイルの
ImageId、起動テンプレート、deploy の記録を新しい AMI に向け、インスタンスを終了
する。AMI が作れないか、待つあいだに AWS の誤りが出れば、何も向けずに、その誤りと
今の状態を知らせて終了する。

app deploy と app remove の --bake も、同じロックを持ったまま、手順 3 の check、
手順 4 の確認に並べる plan、手順 5〜9 の make を使う。ssh set-port も、同じロックを
持ったまま、手順 3 の check_template と check_size、手順 5〜9 の make を使う。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from temt import app, aws, conf, instances, lock, ltbuild, ltdata, msg, resources, terminate, wait
from temt.aws import error_code, field
from temt.msg import TemtError
from temt.names import PREFIX, tilde
from temt.table import aws_error, printable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ec2.type_defs import BlockDeviceMappingTypeDef, TagTypeDef
    from types_boto3_ssm.client import SSMClient

    from temt.conf import Conf
    from temt.instances import Instance
    from temt.names import Service

# 同じ名前の AMI が既にあるときに create-image が返す誤りの名前
DUPLICATE_NAME = "InvalidAMIName.Duplicate"
PENDING = "pending"
AVAILABLE = "available"
# 作った AMI が describe-images に見えるようになるまでの締め切り（秒）
UNSEEN_DEADLINE = 300
# AMI の名前に入れる時刻の形。クライアントのローカル時刻で書く
NAME_TIME = "%y%m%d%H%M"

# AMI の名前に使う時刻。テストから差し替える
localtime = time.localtime


def image_name(svc: str) -> str:
    """AMI の名前 temt-<svc>-<YYMMDDHHMM>。"""
    return f"{PREFIX}-{svc}-{time.strftime(NAME_TIME, localtime())}"


def _one(service: Service, found: list[Instance]) -> Instance:
    """手順 2。焼けるインスタンスを 1 台に決める。決まらなければ終了する。

    終了の途中のものは、無いものとして扱う。
    """
    found = instances.remaining(found)
    if not found:
        raise TemtError(f"no instance for {service.name} to bake")
    if len(found) > 1:
        instances.too_many(service, found, "bake")
    one = found[0]
    if one.state not in (instances.RUNNING, instances.STOPPED):
        raise TemtError(f"{one.id} is {one.state}; wait for it to finish and bake again")
    return one


def check(
    ec2: EC2Client, service: Service, values: Conf, one: Instance, *, command: str = "bake"
) -> None:
    """手順 3。テンプレートが設定ファイルと揃っていることを確かめる（check_template）。
    続けて、焼くインスタンス one のルートボリュームが、ルートボリュームの要素の
    Ebs.VolumeSize より大きくないことを確かめる（check_size）。command は、直したあとに
    やり直すよう告げるコマンド。
    """
    built = check_template(ec2, service, values)
    check_size(ec2, service, built, one, command=command)


def check_template(ec2: EC2Client, service: Service, values: Conf) -> ltbuild.Built:
    """手順 3 のうち、テンプレートが設定ファイルと揃っていることを確かめ、設定ファイルから
    組み立てた中身を返す。

    diff と同じく、設定ファイルから組み立てた中身（ltbuild.build）と既定バージョンの中身の
    全体を比べる。apply がまだ書いていない項目や、まだ無い temt のセキュリティグループが
    あれば揃っていない。ImageId が無いか、指す AMI が使えなければ、ImageId が揃って
    いないとして apply を促す（apply が行を消すよう案内する）。set-port も、sshd を変える
    前に呼ぶ。
    """
    current = resources.default_version(resources.versions(ec2, service), service)
    data = current.get("LaunchTemplateData") or {}
    built = None
    try:
        built = ltbuild.build(ec2, service, values)
    except ltbuild.UnusableImage:
        differ = ["ImageId"]
    else:
        # apply がまだ書いていない項目があれば、次の apply が書いて中身も変えうる
        differ = [
            *ltbuild.unfilled(values, ipv6=built.ipv6),
            *resources.template_differ(data, built.data),
        ]
        differ = list(dict.fromkeys(differ))
        if not differ and not ltdata.same(ltdata.normalize(data), ltdata.normalize(built.data)):
            # 位置に開くと違いが見えない（同じ ResourceType の要素が 2 つあるなど）
            differ = ["the content"]
    if differ or built is None:
        raise TemtError(
            f"the launch template {service.launch_template} does not match "
            f"{printable(tilde(service.conf_path))} in {', '.join(differ)}; "
            f"run 'temt apply --svc {service.svc}' first"
        )
    return built


def check_size(
    ec2: EC2Client, service: Service, built: ltbuild.Built, one: Instance, *, command: str = "bake"
) -> None:
    """手順 3 の続き。インスタンスのルートボリュームが、ルートボリュームの要素の
    Ebs.VolumeSize より大きければ止める。

    焼いた AMI はインスタンスのルートボリュームの大きさになり、それより小さい
    Ebs.VolumeSize のテンプレートからは、AWS が起動を断るからである。ルートボリュームの
    要素は、組み立てた中身（built）の、DeviceName が AMI の RootDeviceName のもの。
    ルートボリュームが応答に無ければ、比べられないので止めない。
    """
    wanted = None if built.root is None else (built.root.get("Ebs") or {}).get("VolumeSize")
    if not isinstance(wanted, int) or not one.root_volume:
        return
    volumes = ec2.describe_volumes(VolumeIds=[one.root_volume]).get("Volumes") or []
    size = volumes[0].get("Size") if volumes else None
    if not isinstance(size, int) or size <= wanted:
        return
    device = built.root.get("DeviceName") if built.root is not None else None
    key = conf.launch_template_key(f"{ltbuild.BLOCK_DEVICES}[DeviceName={device}].Ebs.VolumeSize")
    where = conf.source(service, conf.launch_template_key(ltbuild.BLOCK_DEVICES))
    raise TemtError(
        f"the root volume of {one.id} is {size} GiB, larger than {key} {wanted}; "
        f"raise it to {size} or more or remove it in {printable(tilde(where))}, "
        f"then run 'temt apply --svc {service.svc}' and {command} again"
    )


def plan(service: Service, one: Instance) -> list[str]:
    """手順 4 で挙げる、これからすることの行。"""
    lines = [f"stop {one.id}"] if one.state == instances.RUNNING else []
    return [
        *lines,
        f"make an AMI of {one.id}",
        f"point ImageId in {printable(tilde(service.conf_path))} and the launch template at "
        "the AMI when it is available",
        f"terminate {one.id}",
    ]


def _confirm(service: Service, one: Instance, *, yes: bool) -> None:
    """手順 4。何をするかを挙げて確認する。"""
    msg.tell(f"bake {service.name}:\n" + "".join(f"  {line}\n" for line in plan(service, one)))
    msg.confirm(f"bake {one.id} ({one.state})?", yes=yes)


def _tags(service: Service, name: str) -> list[TagTypeDef]:
    """AMI とスナップショットに付けるタグ。"""
    return [
        {"Key": "Name", "Value": name},
        {"Key": aws.TAG_SERVICE, "Value": service.svc},
    ]


def create(
    ec2: EC2Client,
    service: Service,
    instance_id: str,
    others: Sequence[str],
    mappings: Sequence[BlockDeviceMappingTypeDef] = (),
    name: str | None = None,
    command: str | None = None,
) -> tuple[str, str]:
    """手順 6。停止したインスタンスから AMI を作り、その ID と名前を返す。

    ルートボリューム以外の EBS（デバイス名 others）は NoDevice にして、AMI に入れない。
    create-image は、既定では付いているすべての EBS をスナップショットにして入れる。
    mappings は足す指定で、ami import がルートボリュームの DeleteOnTermination に使う。
    name は AMI の名前で、省けば今の時刻で決める。ami import は確認に出した名前を渡す。
    command は、同じ名前の AMI があるときにやり直すよう告げるコマンドで、省けば bake。
    """
    name = name or image_name(service.svc)
    again = command or "bake"
    tags = _tags(service, name)
    try:
        made = ec2.create_image(
            InstanceId=instance_id,
            Name=name,
            BlockDeviceMappings=[
                *({"DeviceName": device, "NoDevice": ""} for device in others),
                *mappings,
            ],
            TagSpecifications=[
                {"ResourceType": "image", "Tags": tags},
                {"ResourceType": "snapshot", "Tags": tags},
            ],
        )
    except ClientError as e:
        if error_code(e) != DUPLICATE_NAME:
            raise
        raise TemtError(
            f"an AMI named {name} already exists; wait a minute and run {again} again"
        ) from None
    image_id = field(made, "ImageId")
    msg.info(f"creating the AMI {image_id} ({name}) from {instance_id}")
    return image_id, name


def _state(ec2: EC2Client, image_id: str, unseen_until: float) -> str | None:
    """AMI の状態を 1 度読む。見えなければ None。

    作った直後の AMI は describe-images に見えないことがあるので、見えないうちは
    unseen_until まで None を返す。それを過ぎても見えなければ、AMI が消えたもの
    として、作れなかったことを表す文を返す。
    """
    try:
        found = ec2.describe_images(ImageIds=[image_id]).get("Images") or []
    except ClientError as e:
        if error_code(e) != aws.NO_SUCH_IMAGE:
            raise
        found = []
    if not found:
        return f"the AMI {image_id} is not found" if wait.clock() >= unseen_until else None
    state = str(found[0].get("State"))
    if state in (AVAILABLE, PENDING):
        return state
    reason = (found[0].get("StateReason") or {}).get("Message")
    return f"the AMI {image_id} is {state}" + (f": {printable(reason)}" if reason else "")


def wait_available(ec2: EC2Client, image_id: str) -> str | None:
    """手順 7。AMI が available になるまで待つ。作れなければ、その理由を返す。

    締め切りは置かない。AMI の大きさによっては長くかかり、待ち切れずに止めても、
    何も変えていないので、やり直せば済むからである。見えないまま UNSEEN_DEADLINE を
    過ぎたときと、available でも pending でもない状態になったときを、作れなかった
    ものとする。
    """
    unseen_until = wait.clock() + UNSEEN_DEADLINE
    while True:
        state = _state(ec2, image_id, unseen_until)
        if state == AVAILABLE:
            msg.info(f"the AMI {image_id} is {AVAILABLE}")
            return None
        if state not in (None, PENDING):
            return state
        wait.sleep(wait.INTERVAL)


def _left(service: Service, one: Instance) -> None:
    """手順 7 で止まるときに、今の状態を知らせる。

    bake はまだ設定ファイル、テンプレート、記録に何も書いていない。--bake を付けた
    deploy と app remove は記録を書き換えているので、bake の範囲に限って知らせる。
    続くエラーに添えるので、標準エラー出力に書く。
    """
    msg.tell(
        f"bake has not changed {printable(tilde(service.conf_path))}, the launch template "
        f"or the deploy records; {one.id} is left {instances.STOPPED}\n",
        side=True,
    )


def _point(ec2: EC2Client, service: Service, name: str, image_id: str) -> None:
    """手順 8。設定ファイルの ImageId に AMI の名前 name を書き、テンプレートを向ける。

    image_id はその AMI の ID。設定ファイルは読み直してから書く。無ければ作らずに
    終了する。テンプレートには ImageId だけを替えたバージョンを作って既定にし、他の
    バージョンを消す。
    """
    values = conf.load(service)
    was = values.launch_template.get("ImageId")
    key = conf.launch_template_key("ImageId")
    if conf.update(service, values, key, name):
        # 待つあいだに行が消されていれば、元の値は無い
        note = "" if was is None else f" (was {printable(str(was))})"
        msg.info(f"wrote {key} = {name} in {printable(str(service.conf_path))}{note}")
    resources.point_image(ec2, service, image_id)


def _mark(ssm: SSMClient, service: Service, one: Instance, image_id: str) -> None:
    """手順 8。deploy の記録を、テンプレートに続けて新しい AMI に向ける。

    テンプレートの AMI と記録が食い違わないようにするためである。記録が読めなくても、
    焼くことは止めない。
    """
    try:
        app.mark_baked(ssm, service, one, image_id)
    except (ClientError, BotoCoreError, TemtError) as e:
        msg.warn(
            f"cannot read the deploy records: {aws_error(e)}; app list may show the apps as lost"
        )


def make(ec2: EC2Client, ssm: SSMClient, service: Service, one: Instance) -> None:
    """手順 5〜9。インスタンスを止めて AMI にし、向け直し、インスタンスを終了する。

    呼ぶ側がロックを持ち、手順 1〜4 を済ませてあるものとする。
    """
    # 手順 5
    if one.state == instances.RUNNING:
        # 確認を待つ間に止まったり終了したりしていれば、AWS が断る
        with instances.refused(service, "bake"):
            instances.stop(ec2, one.id)
        instances.wait_stopped(ec2, one.id)
    # 手順 6
    image_id, name = create(ec2, service, one.id, one.other_devices)
    # 手順 7。ここで止まれば、設定ファイルもテンプレートも記録も元のまま
    try:
        why = wait_available(ec2, image_id)
    except (ClientError, BotoCoreError):
        _left(service, one)
        msg.tell(f"the AMI {image_id} is not {AVAILABLE} yet\n", side=True)
        raise
    if why is not None:
        _left(service, one)
        raise TemtError(why)
    # 手順 8
    _point(ec2, service, name, image_id)
    _mark(ssm, service, one, image_id)
    # 手順 9。手順 1 で読んだ状態は running のことがあるので、止めた後の状態から待つ
    halted = replace(one, state=instances.STOPPED)
    terminate.shut_down(ec2, service, [halted])
    instances.wait_terminated(ec2, [halted])
    msg.info(f"baked {name} ({image_id}); the next start boots from it")


def run(service: Service, *, yes: bool) -> None:
    """インスタンスを AMI にし、次の start がその AMI から起動するようにする。

    yes なら確認しない。
    """
    values = conf.load(service)
    session = aws.session(values)
    ec2 = session.client("ec2")
    with lock.hold(service):
        # 手順 1。テンプレートが無ければ、ここで apply を促して終わる
        resources.require_template(ec2, service)
        found = instances.find(ec2, service)
        # 手順 2
        one = _one(service, found)
        # 手順 3
        check(ec2, service, values, one)
        # 手順 4
        _confirm(service, one, yes=yes)
        make(ec2, session.client("ssm"), service, one)
