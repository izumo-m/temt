"""stop の本体。設計の手順 1〜4 を順に行う。

止めたインスタンスは、次の start が再開する。ボリュームは残るので、DeleteOnTermination
には触れない。

起動テンプレートの有無は確かめない。停止はテンプレートを使わないからである。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temt import aws, conf, instances, lock, msg

if TYPE_CHECKING:
    from temt.names import Service


def run(service: Service, *, is_async: bool, yes: bool) -> None:
    """サービスのインスタンスを停止する。

    is_async なら、AWS が停止の要求を受け付けるまでしか待たない。yes なら確認しない。
    """
    values = conf.load(service)
    ec2 = aws.session(values).client("ec2")
    with lock.hold(service):
        # 手順 1。終了の途中のものは、無いものとして扱う
        found = instances.remaining(instances.find(ec2, service))
        if not found:
            # 無いことはエラーにしない
            msg.info(f"no instance for {service.name}")
            return
        if len(found) > 1:
            instances.too_many(service, found, "stop")
        one = found[0]
        # 手順 2
        if one.state == instances.STOPPED:
            msg.info(f"{one.id} is already {one.state}")
            return
        if one.state == instances.STOPPING:
            msg.info(f"{one.id} is already {one.state}")
        else:
            # 手順 3。pending でも停止を求める。断るかは AWS が決める
            msg.confirm(f"stop {one.id} ({one.state})?", yes=yes)
            with instances.refused(service, "stop"):
                instances.stop(ec2, one.id)
        # 手順 4
        if is_async:
            return
        # 古い running が見えても、stopped を待つ間は取り違えない
        instances.wait_stopped(
            ec2,
            one.id,
            f"see 'temt status --svc {service.svc}'; "
            "a systemd-inhibit lock on the instance can hold the stop back",
        )
