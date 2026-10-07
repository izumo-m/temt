"""terminate の本体。設計の手順 1〜5 を順に行う。

手順 4（選んだインスタンスを終了し、known_hosts を消す）は shut_down に分けて
ある。destroy も、確認の後でこれと手順 5 の待ち合わせを使う。bake も、AMI ができた
後でこれを使う。

起動テンプレートの有無は確かめない。テンプレートが無くても、取り残された
インスタンスを消せるようにするためである。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from temt import app, aws, conf, home, instances, lock, msg, resources, table
from temt.msg import TemtError

if TYPE_CHECKING:
    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.instances import Instance
    from temt.names import Service


def shut_down(ec2: EC2Client, service: Service, found: list[Instance]) -> None:
    """手順 4。選んだインスタンスのそれぞれを終了し、known_hosts を消す。

    2 台以上あってもすべてに行う。
    """
    for one in found:
        instances.delete_volumes(ec2, one)
        instances.terminate(ec2, one.id)
        # 要求が受け付けられてから消す。EC2 はインスタンス ID を再利用しないので、
        # 終了を待たなくてよい
        home.forget_known_hosts(service, one.id)


def _warn_unbaked(ec2: EC2Client, ssm: SSMClient, service: Service, found: list[Instance]) -> None:
    """手順 3 の確認の前に、終了すると消える、焼いていないアプリを警告する。

    焼かずに外したアプリが起動テンプレートの AMI に焼いてあれば、次のインスタンスに
    戻ることも警告する。記録やテンプレートが読めなくても、終了は止めない。
    """
    ids = {one.id for one in found}
    try:
        found_records = app.records(ssm, service)
    except (ClientError, BotoCoreError, TemtError) as e:
        msg.warn(f"cannot read the deploy records: {table.aws_error(e)}")
        return
    unbaked = [app_id for app_id, record in found_records if record.fields.get("instance") in ids]
    removed = [
        (app_id, record.fields.get("baked_image"))
        for app_id, record in found_records
        if record.fields.get("removed") in ids
    ]
    if removed:
        # テンプレートが無いか読めなければ、戻るかどうか分からないので挙げない
        try:
            image_id = resources.default_image(ec2, service)
        except (ClientError, BotoCoreError, TemtError):
            image_id = None
        removed = [(app_id, baked) for app_id, baked in removed if baked and baked == image_id]
    if unbaked:
        msg.warn(f"unbaked apps will be lost: {', '.join(unbaked)}")
    if removed:
        found_ids = ", ".join(app_id for app_id, _ in removed)
        msg.warn(f"apps removed without baking come back from the AMI: {found_ids}")


def _chosen(service: Service, found: list[Instance], ids: tuple[str, ...]) -> list[Instance]:
    """手順 2。ids で指したインスタンスを選ぶ。ids が空なら、見つかったものすべて。

    指した ID が見つかったものに無ければ、見つかったものを挙げて終了する。何も
    終了しない。
    """
    if not ids:
        return found
    known = {one.id for one in found}
    missing = [instance_id for instance_id in ids if instance_id not in known]
    if missing:
        if found:
            instances.tell_found(service, found)
        raise TemtError(
            f"no instance {', '.join(missing)} for {service.name}; nothing was terminated; "
            f"see 'temt status --svc {service.svc}' and terminate again"
        )
    return [one for one in found if one.id in ids]


def run(service: Service, *, ids: tuple[str, ...] = (), is_async: bool, yes: bool) -> None:
    """サービスのインスタンスを終了する。

    ids があれば、そのインスタンスだけを終了する。is_async なら、terminated になるのを
    待たない。yes なら確認しない。
    """
    values = conf.load(service)
    session = aws.session(values)
    ec2 = session.client("ec2")
    with lock.hold(service):
        # 手順 1
        found = instances.find(ec2, service)
        if not found and not ids:
            # 手順 2。無いことはエラーにしない
            msg.info(f"no instance for {service.name}")
            return
        chosen = _chosen(service, found, ids)
        # 手順 3
        _warn_unbaked(ec2, session.client("ssm"), service, chosen)
        listed = ", ".join(f"{one.id} ({one.state})" for one in chosen)
        msg.confirm(f"terminate {listed}?", yes=yes)
        # 手順 4
        shut_down(ec2, service, chosen)
        # 手順 5
        if is_async:
            return
        instances.wait_terminated(ec2, chosen)
