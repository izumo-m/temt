"""destroy の本体。設計の手順 1〜6 を順に行う。

インスタンスを終了し、apply が作った AWS リソースとサービスのコマンドを消す。
設定ファイル、ログインの鍵、ssh の Host ブロック、AMI は残し、次の apply がそのまま
使う。--async は無く、つねに完了を待つ。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from temt import aws, conf, home, instances, lock, msg, resources, terminate
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import ssh_dir, tilde
from temt.table import aws_error, printable

if TYPE_CHECKING:
    from types_boto3_ec2.client import EC2Client

    from temt.instances import Instance
    from temt.names import Service


def _values(service: Service) -> Conf:
    """設定ファイルを読む。無ければ、それを告げて終了する。

    destroy が要るのは、どのリージョンとプロファイルで片づけるかを決めるこのファイル
    だけである。
    起動テンプレートも SSM パラメータも消す相手なので、apply は促さない。
    """
    if not service.conf_path.exists():
        raise TemtError(
            f"no config file: {printable(str(service.conf_path))}; destroy needs AwsRegion from it"
        )
    return conf.load(service)


def _listed(service: Service, found: list[Instance], inline: list[str], managed: list[str]) -> str:
    """手順 2 で挙げる、これから消そうとするもの。

    inline はロールに付いたインラインポリシーの名前で、ロールと一緒に消す。手で
    付けたものも消えるので、名前を挙げる。managed は付いた管理ポリシーの ARN で、外す。
    あるかどうかと、temt のタグが付いているかは消すときに確かめるので、ここに並ぶものが
    今あるとは限らない。
    """
    role = f"role {service.name}"
    held = [
        *([f"inline policies: {', '.join(inline)}"] if inline else []),
        *([f"managed policies: {', '.join(managed)}"] if managed else []),
    ]
    if held:
        role += f" ({'; '.join(held)})"
    lines = [
        "instances: " + (", ".join(f"{one.id} ({one.state})" for one in found) or "none"),
        f"launch template {service.launch_template}",
        f"parameters under {service.parameter_path}/",
        f"security group {service.name}",
        f"instance profile {service.name}",
        role,
        f"{printable(tilde(service.known_hosts_dir))}/",
        printable(tilde(service.command_path)),
    ]
    return f"destroy {service.name}:\n" + "".join(f"  {line}\n" for line in lines)


def _forget(service: Service) -> None:
    """手順 5。known_hosts のディレクトリと、サービスのコマンドを消す。"""
    # ディレクトリはロックを取るときに作るので、ふつうは absent にならない
    state = resources.DELETED if home.forget_service(service) else resources.ABSENT
    msg.info(f"{printable(tilde(service.known_hosts_dir))}: {state}")
    state = resources.DELETED if home.remove_command(service) else resources.ABSENT
    msg.info(f"{printable(tilde(service.command_path))}: {state}")


def _ami(ec2: EC2Client, svc: str) -> aws.Ami | None:
    """報告に添えるサービスの AMI。読めなければ警告して、挙げない。

    手順 6 は報告だけなので、ここで destroy を失敗させない。
    """
    try:
        found = aws.own_ami(ec2, svc)
    except ClientError as e:
        msg.warn(f"cannot tell which AMIs are tagged {aws.TAG_SERVICE}={svc}: {aws_error(e)}")
        return None
    return found


def _left(ec2: EC2Client, service: Service) -> None:
    """手順 6。次の apply がそのまま使うものを知らせる。"""
    lines = [
        printable(tilde(service.conf_path)),
        f"the Host {service.name} block in {printable(tilde(ssh_dir() / 'config'))}",
    ]
    found = _ami(ec2, service.svc)
    if found is not None:
        lines.append(f"AMI {found.name} ({found.image_id}, {aws.TAG_SERVICE}={service.svc})")
    msg.tell("left for the next apply:\n" + "".join(f"  {line}\n" for line in lines))


def run(service: Service, *, yes: bool) -> None:
    """サービスのインスタンスを終了し、AWS リソースとサービスのコマンドを消す。

    yes なら確認しない。
    """
    values = _values(service)
    session = aws.session(values)
    ec2 = session.client("ec2")
    iam = session.client("iam")
    ssm = session.client("ssm")
    with lock.hold(service):
        # 手順 1
        found = instances.find(ec2, service)
        # 手順 2。インスタンスが無くても、リソースを消すので確認する
        msg.tell(
            _listed(
                service,
                found,
                resources.inline_policies(iam, service),
                resources.attached_policies(iam, service),
            )
        )
        msg.confirm(f"destroy {service.name}?", yes=yes)
        # 手順 3
        if found:
            terminate.shut_down(ec2, service, found)
            instances.wait_terminated(ec2, found)
        # 手順 4
        resources.remove(ec2, iam, ssm, service)
        # 手順 5
        _forget(service)
        # 手順 6
        _left(ec2, service)
