"""destroy の本体。設計の手順 1〜5 を順に行う。

インスタンスを終了し、apply がそのリージョンに作った AWS リソースを消す。インスタンス
ロールとインスタンスプロファイルは、ほかのリージョンのタグがロールに残っていなければ
消す。手元のファイルは、終了したインスタンスの known_hosts のファイルだけを消す。設定
ファイル、ログインの鍵、ssh の Host ブロック、known_hosts のディレクトリ、サービスの
コマンド、AMI は残し、次の apply がそのまま使う。ほかのリージョンのクローンも使うかも
しれないからである。--async は無く、つねに完了を待つ。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from temt import aws, conf, instances, lock, msg, resources, terminate
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import ssh_dir
from temt.table import aws_error, printable, tilde

if TYPE_CHECKING:
    from types_boto3_ec2.client import EC2Client

    from temt.instances import Instance
    from temt.names import Service


def _values(service: Service) -> Conf:
    """設定ファイルを読む。無ければ、それを告げて終了する。

    打ち間違えたサービス名で、すぐ止まるようにする。起動テンプレートも SSM パラメータも
    消す相手なので、apply は促さない。
    """
    if not service.conf_path.exists():
        raise TemtError(f"no config file: {printable(tilde(service.conf_path))}")
    return conf.load(service)


def _listed(service: Service, found: list[Instance], release: resources.Release) -> str:
    """手順 2 で挙げる、これから消そうとするもの。

    ロールは、消すなら、付いたインラインポリシーの名前と管理ポリシーの ARN を添える。
    インラインポリシーは手で付けたものも消え、管理ポリシーは外れる。ほかのリージョンの
    タグが残っていれば、インスタンスプロファイルとともに残すことを、そのリージョンを添えて
    挙げる。あるかどうかと、temt のタグが付いているかは消すときに確かめるので、ここに
    並ぶものが今あるとは限らない。
    """
    profile = f"instance profile {service.name}"
    role = f"role {service.name}"
    if release.kept:
        profile += f" ({resources.KEPT})"
        role += f" ({release.used})"
    elif held := resources.held(release.inline, release.managed):
        role += f" ({held})"
    lines = [
        "instances: " + (", ".join(f"{one.id} ({one.state})" for one in found) or "none"),
        f"launch template {service.launch_template}",
        f"parameters under {service.parameter_path}/",
        f"security group {service.name}",
        profile,
        role,
    ]
    return f"destroy {service.name}:\n" + "".join(f"  {line}\n" for line in lines)


def _ami(ec2: EC2Client, service: Service) -> str | None:
    """報告に添える、次の apply が起動テンプレートを作るときに選ぶサービスの AMI の行。
    無ければ None。読めなければ警告して、挙げない。

    手順 5 は報告だけなので、ここで destroy を失敗させない。
    """
    try:
        found = aws.newest_image(ec2, service.svc)
    except ClientError as e:
        msg.warn(f"cannot tell the newest AMI of {service.name}: {aws_error(e)}")
        return None
    if found is None:
        return None
    pending = f", {aws.PENDING}" if found.get("State") == aws.PENDING else ""
    return (
        f"AMI {printable(str(found.get('Name') or ''))} "
        f"({printable(str(found.get('ImageId') or ''))}{pending})"
    )


def _left(ec2: EC2Client, service: Service, values: Conf) -> None:
    """手順 5。次の apply がそのまま使うものを知らせる。

    ImageId を書いていれば、次の apply はその AMI を使い、選ばない。それは設定ファイルに
    あるので、AMI は挙げない。
    """
    lines = [
        printable(tilde(service.conf_path)),
        f"the Host {service.name} block in {printable(tilde(ssh_dir() / 'config'))}",
        f"{printable(tilde(service.known_hosts_dir))}/",
        printable(tilde(service.command_path)),
    ]
    found = None if "ImageId" in values.launch_template else _ami(ec2, service)
    if found is not None:
        lines.append(found)
    msg.tell("left for the next apply:\n" + "".join(f"  {line}\n" for line in lines))


def run(service: Service, *, yes: bool) -> None:
    """サービスのインスタンスを終了し、そのリージョンの AWS リソースを消す。

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
        msg.tell(_listed(service, found, resources.role_release(iam, service, values.AwsRegion)))
        aws.confirm(session, f"destroy {service.name}", yes=yes)
        # 手順 3。known_hosts は、終了したインスタンスのファイルだけを消す
        if found:
            terminate.shut_down(ec2, service, found)
            instances.wait_terminated(ec2, found)
        # 手順 4
        resources.remove(ec2, iam, ssm, service, values.AwsRegion)
        # 手順 5
        _left(ec2, service, values)
