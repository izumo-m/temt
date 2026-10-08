"""apply と diff の本体。設計の手順 1〜6 を順に呼ぶ。

手順 1 のうち、名前の検査は cli が Service を作るときに済んでいる。ここで行う設定ファイルの
場所の確認は、何も揃えず、先へ進んでよいかだけを決める。手順 2〜6 は、設定ファイルを正と
して対象の 1 つを揃える独立した処理で、対象ごとに、差分を検出する処理と、検出した
差分で更新する処理に分かれている。この module はその順序だけを持つ。

apply（run）は、すべての手順の差分を先に検出し（_detect）、表示して確認を求めてから、
検出した差分で更新する（_update）。diff は検出だけを行い、変更を標準出力に書く。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from temt import aws, change, conf, home, keys, msg, policy, resources
from temt.aws import field
from temt.change import ADD, ARROW, UPDATE, Change
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import ssh_dir, tilde, user_ssh_dir
from temt.table import printable

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    import boto3
    from types_boto3_ec2.client import EC2Client
    from types_boto3_iam.client import IAMClient
    from types_boto3_ssm.client import SSMClient

    from temt.names import Service


# 差分の種類。change.Change の kind
CONF = "conf"


def _quoted(value: Any) -> str:
    """設定ファイルの値の表示。TOML の文字列と同じく " で囲む。"""
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class ConfDiff:
    """手順 2 の差分。設定ファイルに書く項目。

    items は、項目の位置、書かれている値（無ければ None）、書く値の組を、書く順に
    並べたもの。ec2_default は、[[role.policy.ec2]] の既定を足すかどうか。
    """

    items: tuple[tuple[str, Any, str], ...]
    ec2_default: bool = False

    def changes(self) -> list[Change]:
        """diff に出す形にする。"""
        found = [
            Change(ADD, CONF, key, _quoted(value))
            if now is None
            else Change(UPDATE, CONF, key, f"{_quoted(now)}{ARROW}{_quoted(value)}")
            for key, now, value in self.items
        ]
        if self.ec2_default:
            found.append(Change(ADD, CONF, f"role.policy.{conf.EC2}", "(default)"))
        return found


def _conf_items(
    ec2: EC2Client, ssm: SSMClient, service: Service, values: Conf
) -> Iterator[tuple[str, str]]:
    """手順 2 で設定ファイルに書く項目の位置と値を、1 つずつ決めて返す。

    --profile で選ばれたプロファイルを AwsProfile として先頭に、残りを conf.md の表の
    順に返す。AWS を読むのは ImageId と SubnetId が書かれていないときだけで、それぞれ
    最新の AMI と公開サブネットを選ぶ。更新でもこの順に書くので、更新の途中で止まって
    も、AwsProfile は書かれていて、次の apply が同じアカウントを向く。
    """
    if conf.OPTION_PROFILE:
        yield "AwsProfile", conf.OPTION_PROFILE
    yield "AwsRegion", values.AwsRegion
    template = values.launch_template
    image = template.ImageId
    if image is conf.UNSET:
        image = aws.latest_ami(ec2, ssm, service.svc)
    yield conf.launch_template_key("ImageId"), image
    yield conf.launch_template_key("InstanceType"), template.InstanceType
    subnet = template.SubnetId
    if subnet is conf.UNSET:
        subnet = aws.public_subnet(ec2, values.AwsRegion, service.conf_path)
    yield conf.launch_template_key("SubnetId"), subnet


def _conf_diffs(
    ec2: EC2Client, ssm: SSMClient, service: Service, values: Conf
) -> Iterator[tuple[str, Any, str]]:
    """手順 2 の項目の差分を、項目を 1 つ決めるたびに検出して返す。

    書かれていないか値の違う項目の、位置、書かれている値（無ければ None）、書く値を返す。
    """
    for key, value in _conf_items(ec2, ssm, service, values):
        now = conf.written(service, key)
        if now != value:
            yield key, now, value


def _needs_ec2_default(values: Conf) -> bool:
    """[[role.policy.ec2]] の既定を足すか。1 つも書かれていなければ足す。"""
    return conf.EC2 not in values.role.policy


def _detect_conf(ec2: EC2Client, ssm: SSMClient, service: Service, values: Conf) -> ConfDiff:
    """手順 2 の差分を検出する。書かれていないか値の違う項目と、既定を足すか。

    既定を足すなら、足せるかも確かめる。確かめるのはいまの設定ファイルを読んだ values
    なので、項目を書いたものとする前に呼ぶ。ファイルがまだ無ければ、作ったファイルに
    足すので、確かめるまでもない。
    """
    items = tuple(_conf_diffs(ec2, ssm, service, values))
    ec2_default = _needs_ec2_default(values)
    if ec2_default and service.conf_path.exists():
        _ = conf.with_ec2_default(service, values)
    return ConfDiff(items, ec2_default=ec2_default)


class Resolved(NamedTuple):
    """手順 4 で設定ファイルから解いた、この後の手順が使う値。

    root_device は、VolumeSize が書かれているときの AMI のルートボリュームのデバイス
    名で、書かれていなければ None。
    """

    image_id: str
    subnet_id: str
    subnet: aws.Subnet
    root_device: str | None


def _root_device(service: Service, values: Conf, found: Mapping[str, Any]) -> str | None:
    """手順 4 の続き。VolumeSize が AMI のルートボリュームより小さくないことを確かめる。

    書かれていれば、起動テンプレートに入れるルートボリュームのデバイス名を返す。
    """
    size = values.launch_template.VolumeSize
    if size is None:
        return None
    key = conf.launch_template_key("VolumeSize")
    image_id = field(found, "ImageId")
    root = aws.root_volume(found)
    if root is None:
        raise TemtError(
            f"the AMI {image_id} has no EBS root volume; remove {key} from "
            f"{printable(str(service.conf_path))}"
        )
    if size < root.size:
        raise TemtError(
            f"{key} {size} is smaller than the root volume of {image_id} ({root.size} GiB); "
            f"raise it or remove it in {printable(str(service.conf_path))}"
        )
    return root.device


def _resolve(ec2: EC2Client, service: Service, values: Conf) -> Resolved:
    """手順 4。サブネットと AMI があることを確かめて、VPC と IPv6 の CIDR の有無を得る。

    この後の手順が使う AMI とサブネットの ID も、サブネットの性質とともに返す。
    ImageId が AMI の名前なら、ここで ID を引く。どちらの項目も手順 2 が必ず決めて
    values に入れているので、ふつうは値がある。サブネットを先に確かめるのは、別の
    アカウントで作ろうとしているときに、両方の行を消すよう案内するためである。
    """
    template = values.launch_template
    subnet_id = template.SubnetId
    if subnet_id is conf.UNSET:
        raise TemtError(
            f"no {conf.launch_template_key('SubnetId')} in {printable(str(service.conf_path))}"
        )
    image_id = template.ImageId
    if image_id is conf.UNSET:
        raise TemtError(
            f"no {conf.launch_template_key('ImageId')} in {printable(str(service.conf_path))}"
        )
    subnet = aws.subnet(ec2, subnet_id, service.conf_path)
    found = aws.image(ec2, image_id, service.conf_path)
    return Resolved(
        field(found, "ImageId"), subnet_id, subnet, _root_device(service, values, found)
    )


def _role_policies(
    session: boto3.Session, service: Service, values: Conf
) -> dict[str, dict[str, Any]]:
    """手順 4 の続き。[[role.policy.<ポリシー名>]] から temt-<ポリシー名> の中身を作り、
    検査する。

    インラインポリシーの名前から中身への対応を返す。ERROR があれば、すべてのポリシーを
    検査してから止める。手順 2 が ec2 の既定を足したものとして values に入れているので、
    ポリシーはつねにある。
    """
    made: dict[str, dict[str, Any]] = {}
    # 中身を作るところは AWS を呼ばないので、先にすべて作り、誤りがあれば検査の前に止める
    for name, statements in values.role.policy.items():
        made[policy.policy_name(name)] = policy.document(
            statements,
            policy.variables(service),
            f"{printable(str(service.conf_path))}: role.policy.{name}",
        )
    analyzer = session.client("accessanalyzer")
    errors = 0
    failed: list[str] = []
    for name in values.role.policy:
        found = policy.validate(
            analyzer,
            made[policy.policy_name(name)],
            policy.policy_name(name),
            f"role.policy.{name}",
        )
        if found:
            errors += found
            failed.append(policy.policy_name(name))
    if errors:
        noun = "policy" if len(failed) == 1 else "policies"
        raise TemtError(
            f"IAM Access Analyzer found {errors} error(s) in the role {noun} {', '.join(failed)}"
        )
    return made


class Clients(NamedTuple):
    """apply と diff が使う AWS のクライアント。"""

    ec2: EC2Client
    ssm: SSMClient
    iam: IAMClient


def _clients(session: boto3.Session) -> Clients:
    """Session から、apply と diff が使うクライアントを作る。"""
    return Clients(session.client("ec2"), session.client("ssm"), session.client("iam"))


@dataclass(frozen=True)
class Plan:
    """_detect が検出した差分。_update はこれだけを見て更新する。

    new は設定ファイルを新しく作るか。command はサービスのコマンドを書くか。
    """

    conf: ConfDiff
    new: bool
    dirs: tuple[home.Dir, ...]
    command: bool
    found: resources.Found
    key: keys.KeyDiff | None
    host: home.Edit
    include: home.Edit


def _detect(
    service: Service,
    values: Conf,
    session: boto3.Session,
    clients: Clients,
    shown: list[Change],
) -> Plan:
    """手順 2〜6 の差分を、手順の順に検出する。何も書き込まない。

    手順 2 で書く値は、values に書いたものとして後の手順で使う。見せる変更は、検出する
    たびに shown に足していく。手順の中で止まるときも、それまでに検出した変更は shown に
    残る。手順 1 は呼ぶ側が先に済ませる。
    """
    ec2, ssm, iam = clients
    new = not service.conf_path.exists()
    configured = _detect_conf(ec2, ssm, service, values)
    for key, _, value in configured.items:
        conf.assign(values, key, value)
    if configured.ec2_default:
        values.role.policy[conf.EC2] = conf.ec2_default()
    shown += configured.changes()
    dirs = tuple(home.detect_dirs())
    shown += home.dir_changes(list(dirs))
    command = home.command_missing(service)
    shown += home.command_changes(service, command)
    resolved = _resolve(ec2, service, values)
    policies = _role_policies(session, service, values)
    found = resources.detect(
        ec2,
        iam,
        ssm,
        service,
        values,
        resolved.image_id,
        resolved.subnet_id,
        resolved.subnet.vpc_id,
        ipv6=resolved.subnet.ipv6,
        root_device=resolved.root_device,
        policies=policies,
    )
    shown += found.changes()
    host = home.detect_host_block(service, values)
    # Host ブロックが既定の鍵を使うなら、無ければ作る。利用者が替えた鍵には触らない
    path = home.default_key(host, service)
    key = None if path is None else keys.detect(path)
    if key is not None:
        shown += key.changes()
    shown += home.host_block_changes(service, host)
    include = home.detect_include()
    shown += home.include_changes(include, host)
    return Plan(configured, new, dirs, command, found, key, host, include)


def _write(service: Service, values: Conf, key: str, value: str) -> None:
    """設定ファイルの項目を書き、知らせる。"""
    if conf.update(service, values, key, value):
        msg.info(f'wrote {key} = "{printable(value)}" in {printable(str(service.conf_path))}')


def _update(service: Service, values: Conf, clients: Clients, plan: Plan) -> None:
    """_detect が検出した差分で、手順の順に更新する。変えたものだけを知らせる。"""
    ec2, ssm, iam = clients
    # 手順 2。項目を書き終えてから既定のポリシーを足し、新しいファイルには最後に書き方の
    # 例（conf.EXAMPLE）を足す。例のコメントを末尾に残すためである。途中で止まった
    # ときは足さない。後から項目を足すと、それは最後のテーブルの中、例のコメントより
    # 後ろに置かれるからである
    for key, _, value in plan.conf.items:
        _write(service, values, key, value)
    if plan.conf.ec2_default:
        conf.write_ec2_default(service, values)
        msg.info(
            f"wrote the default [[role.policy.{conf.EC2}]] in {printable(str(service.conf_path))}"
        )
    if plan.new:
        conf.write_example(service)
    # 手順 3
    if plan.dirs:
        home.prepare_dirs()
        for one in plan.dirs:
            done = "created" if one.mode is None else "set the mode of"
            msg.info(f"{done} {printable(tilde(one.path))} (mode {home.DIR_MODE:o})")
    if plan.command:
        home.write_command(service)
        msg.info(f"wrote {printable(tilde(service.command_path))}")
    # 手順 5
    resources.update(ec2, iam, ssm, service, values, plan.found)
    # 手順 6。鍵を先に作り、ssh の設定が無い鍵を指す時間を作らない
    keys.ensure(plan.key)
    if home.write_edit(plan.host):
        msg.info(f"wrote the Host {service.name} block in {printable(tilde(ssh_dir() / 'config'))}")
    if home.write_include(plan.include, plan.host):
        msg.info(
            f"wrote '{printable(home.include_line())}' in "
            f"{printable(tilde(user_ssh_dir() / 'config'))}"
        )


def run(service: Service, *, yes: bool) -> None:
    """サービスの設定ファイル、AWS リソース、ssh の設定と鍵を揃える。

    すべての差分を先に検出し、違いが無ければそう知らせて終わる。違いがあれば diff と同じ
    形で表示し、確認を求めてから更新する。確認の前には何も書かない。
    """
    # 手順 1。同じサービスを別の場所から apply していれば、何かを書く前に止める。
    # AWS のリソースも同じ名前なので、別の定義で上書きしてしまう
    home.check_owner(service)
    # 手順 2 は書かれていない ImageId と SubnetId を解くのに AWS を読むので、まず読めた
    # 分の設定で Session を作る。リージョンは設定ファイルか既定で決まっているので、
    # 向き先は動かない
    values = conf.load(service, missing_ok=True)
    session = aws.session(values)
    clients = _clients(session)
    shown: list[Change] = []
    plan = _detect(service, values, session, clients, shown)
    if not shown:
        msg.info(f"{service.name}: no differences")
        return
    msg.tell(change.shown_changes(shown))
    msg.confirm("apply these changes?", yes=yes)
    _update(service, values, clients, plan)


def diff(service: Service) -> None:
    """次の apply が何を変えるかを、1 つの変更を 1 行で標準出力に書く。

    差分を検出するだけで、何も書き込まない。apply が止まるところで止まるときは、
    それまでに検出した変更を書いてから、同じエラーで終わる。標準出力には変更だけを
    書くよう、途中の知らせは標準エラー出力に回す。
    """
    with msg.stderr_only():
        _diff(service)


def _diff(service: Service) -> None:
    """diff の本体。"""
    shown: list[Change] = []
    try:
        home.check_owner(service)
        values = conf.load(service)
        session = aws.session(values)
        _ = _detect(service, values, session, _clients(session), shown)
    except Exception:
        change.write(shown)
        raise
    if not shown:
        # 標準出力が空なら違いが無いと分かるよう、標準エラー出力に書く
        msg.info(f"{service.name}: no differences", side=True)
        return
    change.write(shown)
