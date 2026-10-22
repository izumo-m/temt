"""start の本体。設計の手順 1〜3 を順に行う。

ロックを取って起動テンプレートがあることを確かめてからインスタンスを探し、無ければ
起動し、停止しているか停止の途中なら再開を求め、--async でなければログインできるように
なるまで待つ。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temt import aws, conf, instances, lock, msg, resources

if TYPE_CHECKING:
    import boto3

    from temt.names import Service


def run(service: Service, *, is_async: bool) -> None:
    """サービスのインスタンスを起動する。停止したインスタンスがあれば再開する。

    is_async なら、AWS が起動や再開の要求を受け付けるまでしか待たない。そうでなければ、
    running になり、ログインできるようになるまで待つ。設定ファイルが WaitForSsm = false
    なら、running で終わる。
    """
    values = conf.load(service)
    session = aws.session(values)
    with lock.hold(service):
        _ = bring_up(
            session, service, is_async=is_async, wait_ssm=values.WaitForSsm, command="start"
        )


def bring_up(
    session: boto3.Session, service: Service, *, is_async: bool, wait_ssm: bool, command: str
) -> str:
    """手順 1〜3。ロックを持って呼ぶ。起動するか再開を求め、そのインスタンスの ID を返す。

    set-port も、ロックを持ったまま、SSM エージェントを待つ形で呼ぶ。command は、
    インスタンスの状態が合わずに AWS が断ったときに、やり直すよう添えるコマンド。
    """
    ec2 = session.client("ec2")
    # 手順 1。テンプレートが無ければ、ここで apply を促して終わる
    resources.require_template(ec2, service)
    # 終了の途中のものは、無いものとして扱う
    found = instances.remaining(instances.find(ec2, service))
    if len(found) > 1:
        instances.too_many(service, found, command)
    # 手順 2。既に running なら、そのインスタンスを running に持っておく
    running = None
    # 再開を求めたときの、見つけた状態
    resumed: str | None = None
    if not found:
        instance_id = instances.launch(ec2, service)
    elif found[0].state in instances.RESUMABLE_STATES:
        # 停止の途中でも止まるのを待たず、再開を求める。断るかは AWS が決める
        instance_id = found[0].id
        with instances.refused(service, command):
            instances.resume(ec2, instance_id)
        resumed = found[0].state
    else:
        one = found[0]
        msg.info(f"{one.id} is already {one.state}")
        instance_id = one.id
        if one.state == instances.RUNNING:
            running = one
    # 手順 3。--async が省くのはこの待ち合わせである
    if is_async:
        return instance_id
    if resumed == instances.STOPPING:
        # AWS が停止の途中の再開を受け付けたなら、止まり終えてから動き出すことも
        # ある。止まるまでと同じだけ待つ
        instances.wait_leaving(
            ec2,
            instance_id,
            instances.STOPPING,
            instances.STOPPED_DEADLINE,
            f"{instance_id} is {instances.STOPPING}; waiting for it to stop",
        )
    if resumed is not None:
        # describe-instances は start-instances から数秒遅れる。古い状態が見える
        # うちに running を待つと、止まったものと取り違える
        instances.wait_leaving(ec2, instance_id, instances.STOPPED)
    if running is None:
        running = instances.wait_running(ec2, instance_id)
    if not wait_ssm:
        # SSM エージェントが登録されないインスタンスでは、待っても Online にならない。
        # running になったことは wait_running が知らせている
        return instance_id
    # 停止してすぐ再開したときに残っている、止まる前の Online の報告を数えない。待ち切れ
    # なくても、何が起きたかだけを知らせ、どうするかは添えない
    instances.wait_online(session.client("ssm"), instance_id, since=running.launched)
    msg.info(f"{instance_id} is ready for login")
    return instance_id
