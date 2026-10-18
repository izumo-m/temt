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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from temt import aws, change, conf, home, keys, ltbuild, ltdata, msg, policy, resources
from temt.change import ADD, ARROW, UPDATE, Change
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import ssh_dir, user_ssh_dir
from temt.table import printable, tilde

if TYPE_CHECKING:
    from collections.abc import Iterator

    import boto3
    from types_boto3_ec2.client import EC2Client
    from types_boto3_iam.client import IAMClient
    from types_boto3_ssm.client import SSMClient

    from temt.names import Service


# 差分の種類。change.Change の kind
CONF = "conf"


def _quoted(value: Any) -> str:
    """設定ファイルの値の表示。TOML の書き方にする（文字列は " で囲み、テーブルは
    インラインテーブル）。表示できない文字は置き換える。
    """
    return printable(conf.toml_value(value))


@dataclass(frozen=True)
class ConfDiff:
    """手順 2 の差分。設定ファイルに書く項目。

    items は、項目の位置、書かれている値（無ければ None）、書く値の組を、書く順に
    並べたもの。ec2_default は、[[role.policy.ec2]] の既定を足すかどうか。
    """

    items: tuple[tuple[str, Any, Any], ...]
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


# 書いていなければ apply が決めて書く値。design/topics/launch-template.md の Filled when
# missing
SHUTDOWN_BEHAVIOR = "terminate"
HTTP_TOKENS = "required"
METADATA_TAGS = "enabled"
CPU_CREDITS = "unlimited"
# サービスを作るときに書く IamInstanceProfile。temt が作るインスタンスプロファイルを指す
TEMT_PROFILE = {"Name": "${temt:name}"}


def _conf_items(
    ec2: EC2Client, ssm: SSMClient, service: Service, values: Conf, *, new: bool
) -> Iterator[tuple[str, Any]]:
    """手順 2 で設定ファイルに書く項目の位置と値を、1 つずつ決めて返す。

    --profile で選ばれたプロファイルを AwsProfile として先頭に、AwsRegion、続けて
    docs/conf.md の「書かなければ apply が書く項目」の表の順に返す。書かれていれば（共通
    ファイルに書かれていても）その値を、無ければ既定を返す。AWS を読むのは、ImageId と
    SubnetId が書かれていないときと、IPv6 アドレスの数を決めるときだけである。new
    （設定ファイルが無い）なら、最後に IamInstanceProfile を返す。更新でもこの順に
    書くので、更新の途中で止まっても、AwsProfile は書かれていて、次の apply が同じ
    アカウントを向く。
    """
    if conf.OPTION_PROFILE:
        yield "AwsProfile", conf.OPTION_PROFILE
    yield "AwsRegion", values.AwsRegion
    data = values.launch_template
    image = data.get("ImageId") or aws.latest_ami(ec2, ssm, service.svc)
    yield conf.launch_template_key("ImageId"), image
    instance_type = data.get("InstanceType") or conf.INSTANCE_TYPE
    yield conf.launch_template_key("InstanceType"), instance_type
    first = ltbuild.primary(data) or {}
    if "NetworkInterfaceId" not in first:
        subnet = first.get("SubnetId") or aws.public_subnet(
            ec2, values.AwsRegion, service.conf_path
        )
        yield conf.primary_key("SubnetId"), subnet
        public = first.get("AssociatePublicIpAddress")
        yield conf.primary_key("AssociatePublicIpAddress"), True if public is None else public
        count = first.get("Ipv6AddressCount")
        if count is not None:
            yield conf.primary_key("Ipv6AddressCount"), count
        elif aws.subnet(ec2, subnet, conf.source(service, conf.primary_key("SubnetId"))).ipv6:
            yield conf.primary_key("Ipv6AddressCount"), resources.IPV6_ADDRESSES
    metadata = data.get("MetadataOptions") or {}
    yield (
        conf.launch_template_key("MetadataOptions.HttpTokens"),
        (metadata.get("HttpTokens") or HTTP_TOKENS),
    )
    yield (
        conf.launch_template_key("MetadataOptions.InstanceMetadataTags"),
        (metadata.get("InstanceMetadataTags") or METADATA_TAGS),
    )
    yield (
        conf.launch_template_key("InstanceInitiatedShutdownBehavior"),
        (data.get("InstanceInitiatedShutdownBehavior") or SHUTDOWN_BEHAVIOR),
    )
    credits = (data.get("CreditSpecification") or {}).get("CpuCredits")
    if credits:
        yield conf.launch_template_key("CreditSpecification.CpuCredits"), credits
    elif resources.BURSTABLE.match(instance_type):
        yield conf.launch_template_key("CreditSpecification.CpuCredits"), CPU_CREDITS
    if new:
        yield conf.launch_template_key(ltdata.PROFILE), data.get(ltdata.PROFILE) or TEMT_PROFILE


def _conf_diffs(
    ec2: EC2Client, ssm: SSMClient, service: Service, values: Conf, *, new: bool
) -> Iterator[tuple[str, Any, Any]]:
    """手順 2 の項目の差分を、項目を 1 つ決めるたびに検出して返す。

    書かれていないか値の違う項目の、位置、書かれている値（無ければ None）、書く値を返す。
    書く先がインラインテーブルの中などで書けなければ、ここで止める。
    """
    for key, value in _conf_items(ec2, ssm, service, values, new=new):
        now = conf.written(service, key)
        if now != value:
            conf.check_writable(service, key, value)
            yield key, now, value


def _managed(service: Service, values: Conf) -> bool:
    """IamInstanceProfile が temt のインスタンスプロファイル <名前> を指すか。"""
    return ltbuild.points_at_temt(values.launch_template, service)


def _needs_ec2_default(service: Service, values: Conf) -> bool:
    """[[role.policy.ec2]] の既定を足すか。temt のインスタンスプロファイルを指していて、
    1 つも書かれていなければ足す。
    """
    return _managed(service, values) and conf.EC2 not in values.role.policy


def _detect_conf(
    ec2: EC2Client, ssm: SSMClient, service: Service, values: Conf, shown: list[Change]
) -> ConfDiff:
    """手順 2 の差分を検出する。書かれていないか値の違う項目と、既定を足すか。

    見せる変更は、項目を 1 つ検出するたびに shown に足す。途中で止まっても、それまでに
    検出した変更は残る。既定を足すなら、足せるかも確かめる。確かめるのはいまの設定
    ファイルを読んだ values なので、項目を書いたものとする前に呼ぶ。ファイルがまだ無ければ、
    作ったファイルに足すので、確かめるまでもない。
    """
    new = not service.conf_path.exists()
    found: list[tuple[str, Any, Any]] = []
    for item in _conf_diffs(ec2, ssm, service, values, new=new):
        found.append(item)
        shown += ConfDiff((item,)).changes()
    items = tuple(found)
    profile = dict((key, value) for key, _, value in items).get(
        conf.launch_template_key(ltdata.PROFILE)
    )
    planned = {**values.launch_template, **({ltdata.PROFILE: profile} if profile else {})}
    ec2_default = ltbuild.points_at_temt(planned, service) and conf.EC2 not in values.role.policy
    if ec2_default and service.conf_path.exists():
        _ = conf.with_ec2_default(service, values)
    if ec2_default:
        shown += ConfDiff((), ec2_default=True).changes()
    return ConfDiff(items, ec2_default=ec2_default)


def _check_role(service: Service, values: Conf) -> None:
    """手順 4 の続き。temt のインスタンスプロファイルを指していないのに [role] が書かれて
    いれば止める。どのファイルの項目かを告げる。
    """
    if _managed(service, values) or not values.role:
        return
    keys = [f"role.policy.{name}" for name in values.role.policy]
    if values.role.ManagedPolicyArns:
        keys.append("role.ManagedPolicyArns")
    places = ", ".join(f"{key} in {printable(tilde(conf.source(service, key)))}" for key in keys)
    raise TemtError(
        f"{conf.launch_template_key(ltdata.PROFILE)} does not point at the instance profile "
        f"{service.name} that temt manages, so [role] cannot be used: {places}; remove them, "
        f"or move the instance role under temt with 'temt role import --svc {service.svc}'"
    )


def _check_ingress(service: Service, values: Conf, built: ltbuild.Built) -> None:
    """手順 4 の続き。受信規則があるのに、起動テンプレートが temt のセキュリティグループを
    使わなければ止める。どのファイルの規則かを告げる。
    """
    if built.uses_group or not values.security_group.ingress:
        return
    key = f"{conf.SECURITY_GROUP}{conf.SEPARATOR}{conf.INGRESS}"
    raise TemtError(
        f"the launch template uses no security group of temt, so the ingress rules in "
        f"{printable(tilde(conf.source(service, key)))} cannot be used; add ${{temt:name}} to "
        f"Groups in {conf.launch_template_key('NetworkInterfaces')}, or remove the rules"
    )


def _role_policies(
    session: boto3.Session, service: Service, values: Conf
) -> dict[str, dict[str, Any]]:
    """手順 4 の続き。[[role.policy.<ポリシー名>]] から temt-<ポリシー名> の中身を作り、
    検査する。

    インラインポリシーの名前から中身への対応を返す。ERROR があれば、すべてのポリシーを
    検査してから止める。手順 2 が ec2 の既定を足したものとして values に入れているので、
    temt のインスタンスプロファイルを指していれば、ポリシーはつねにある。
    """
    made: dict[str, dict[str, Any]] = {}
    # 中身を作るところは AWS を呼ばないので、先にすべて作り、誤りがあれば検査の前に止める
    for name, statements in values.role.policy.items():
        made[policy.policy_name(name)] = policy.document(
            statements,
            policy.variables(service),
            f"{printable(tilde(service.conf_path))}: role.policy.{name}",
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
    configured = _detect_conf(ec2, ssm, service, values, shown)
    for key, _, value in configured.items:
        conf.assign(values, key, value)
    if configured.ec2_default:
        values.role.policy[conf.EC2] = conf.ec2_default()
    dirs = tuple(home.detect_dirs())
    shown += home.dir_changes(list(dirs))
    command = home.command_missing(service)
    shown += home.command_changes(service, command)
    # 手順 4
    built = ltbuild.build(ec2, service, values)
    _check_role(service, values)
    _check_ingress(service, values, built)
    policies = _role_policies(session, service, values) if _managed(service, values) else None
    # 手順 5
    found = resources.detect(ec2, iam, service, values, built, policies=policies)
    shown += found.changes()
    host = home.detect_host_block(service)
    # Host ブロックが既定の鍵を使うなら、無ければ作る。利用者が替えた鍵には触らない
    path = home.default_key(host, service)
    key = None if path is None else keys.detect(path)
    if key is not None:
        shown += key.changes()
    shown += home.host_block_changes(service, host)
    include = home.detect_include()
    shown += home.include_changes(include, host)
    return Plan(configured, new, dirs, command, found, key, host, include)


def _write(service: Service, values: Conf, key: str, value: Any) -> None:
    """設定ファイルの項目を書き、知らせる。"""
    if conf.update(service, values, key, value):
        msg.info(f"wrote {key} = {_quoted(value)} in {printable(tilde(service.conf_path))}")


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
            f"wrote the default [[role.policy.{conf.EC2}]] in {printable(tilde(service.conf_path))}"
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
    resources.update(ec2, iam, service, plan.found)
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
    # 打ち間違えた名前でサービスを作り始めないよう、AWS を読む前に聞く
    if not service.conf_path.exists():
        msg.confirm(
            f"no config file: {printable(tilde(service.conf_path))}; "
            f"create service {service.name}?",
            yes=yes,
        )
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
