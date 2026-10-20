"""apply と diff の本体。設計の手順 1〜6 を順に呼ぶ。

手順 1 のうち、名前の検査は cli が Service を作るときに済んでいる。ここで行う設定ファイルの
場所の確認は、何も揃えず、先へ進んでよいかだけを決める。手順 2〜6 は、設定ファイルを正と
して対象の 1 つを揃える独立した処理で、対象ごとに、差分を検出する処理と、検出した
差分で更新する処理に分かれている。この module はその順序だけを持つ。

apply（run）は、すべての手順の差分を先に検出し（_detect）、表示して確認を求めてから、
検出した差分で更新する（_update）。diff は検出だけを行い、変更を標準出力に書く。どちらも
--image-id で、起動テンプレートの AMI を選び直せる。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from temt import aws, change, conf, home, keys, ltbuild, ltdata, msg, policy, resources
from temt.change import ADD, Change
from temt.conf import Conf
from temt.msg import TemtError, UsageError
from temt.names import ssh_dir, user_ssh_dir
from temt.table import printable, tilde

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import boto3
    from types_boto3_ec2.client import EC2Client
    from types_boto3_iam.client import IAMClient
    from types_boto3_ssm.client import SSMClient

    from temt.names import Service


# 差分の種類。change.Change の kind
CONF = "conf"
# 雛形の IamInstanceProfile の Name。temt が作るインスタンスプロファイルを指す
TEMT_PROFILE_NAME = "${temt:name}"


@dataclass(frozen=True)
class ConfDiff:
    """手順 2 の差分。設定ファイルが無いときに書く雛形。

    text は書く雛形の中身で、ファイルがあれば None。profile と ec2 は、雛形が
    IamInstanceProfile と [[role.ec2]] の既定を書くか。
    """

    text: str | None = None
    profile: bool = False
    ec2: bool = False

    def changes(self) -> list[Change]:
        """diff に出す形にする。雛形が書く値を、設定ファイルの位置で挙げる。"""
        found: list[Change] = []
        if self.profile:
            key = conf.launch_template_key(f"{ltdata.PROFILE}{conf.SEPARATOR}Name")
            found.append(Change(ADD, CONF, key, printable(conf.toml_value(TEMT_PROFILE_NAME))))
        if self.ec2:
            found.append(Change(ADD, CONF, f"{conf.ROLE}{conf.SEPARATOR}{conf.EC2}", "(default)"))
        return found


def _scaffold(service: Service) -> ConfDiff:
    """手順 2。設定ファイルの代わりに読む雛形を決める。

    共通ファイルに IamInstanceProfile があれば、その行は書かず、共通ファイルの値を使う。
    [[role.ec2]] の既定は、IamInstanceProfile が temt のインスタンスプロファイルを指すとき
    だけ書く。ほかのものを指していれば、使われない <名前> を作らないよう書かない。
    """
    common = conf.common_profile()
    ec2 = common is None or ltbuild.points_at_temt({ltdata.PROFILE: common}, service)
    text = conf.scaffold(profile=common is None, ec2=ec2)
    return ConfDiff(text, profile=common is None, ec2=ec2)


def _check_profile(service: Service, values: Conf) -> None:
    """手順 2。IamInstanceProfile と [role] の組み合わせを、AWS を呼ぶ前に確かめる。

    temt のインスタンスプロファイルを指しているのに、どのファイルにも [role] が無ければ
    止める。IamInstanceProfile が書かれておらず、WaitForSsm が false でなければ、
    インスタンスロールが付かないことを警告する。
    """
    key = conf.launch_template_key(ltdata.PROFILE)
    if ltbuild.points_at_temt(values.launch_template, service) and not values.role:
        raise TemtError(
            f"{printable(tilde(conf.source(service, key)))}: {key} points at {service.name}, "
            f"but there is no [{conf.ROLE}]"
        )
    if ltdata.PROFILE not in values.launch_template and values.WaitForSsm:
        msg.warn(
            f"no instance role (no {key}); start may wait for the SSM agent until its deadline"
        )


def _check_image_option(service: Service, values: Conf, given: str | None) -> None:
    """手順 2。ImageId を書いていれば AMI は固定なので、--image-id は引数の誤りにする。"""
    key = conf.launch_template_key(ltbuild.IMAGE_ID)
    if given is not None and ltbuild.IMAGE_ID in values.launch_template:
        raise UsageError(
            f"{ltbuild.IMAGE_ID_OPTION} is not allowed; {key} in "
            f"{printable(tilde(conf.source(service, key)))} fixes the AMI"
        )


def _image(
    clients: Clients, service: Service, values: Conf, given: str | None
) -> tuple[ltbuild.Image | None, list[Mapping[str, Any]] | None]:
    """手順 4。ImageId を書いていなければ、使う AMI を決める。書いていれば None で、build が
    書いた AMI を引く。

    --image-id があればその AMI、起動テンプレートがあればその AMI、無ければ起動テンプレートを
    作るときの選び方で選ぶ（design/topics/ami.md の Selecting）。起動テンプレートを読んだら、
    そのバージョンも返し、手順 5 で読み直さない。
    """
    ec2, ssm, _ = clients
    if ltbuild.IMAGE_ID in values.launch_template:
        return None, None
    if given is not None:
        return ltbuild.given_image(ec2, given), None
    versions = resources.template_versions(ec2, service)
    if versions:
        data = resources.default_version(versions, service).get("LaunchTemplateData") or {}
        return ltbuild.kept_image(ec2, service, data), versions
    return ltbuild.new_image(ec2, ssm, service, values.AwsRegion), versions


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
    """手順 4 の続き。[[role.<ポリシー名>]] から temt-<ポリシー名> の中身を作り、検査する。

    インラインポリシーの名前から中身への対応を返す。ERROR があれば、すべてのポリシーを
    検査してから止める。誤りは、Statement を書いたファイルと、そのファイルの中での位置で
    知らせる。共通ファイルと重ねたポリシーは、2 つのファイルの Statement がつながっている。
    """
    made: dict[str, dict[str, Any]] = {}
    places: dict[str, list[str]] = {}
    for name, statements in values.role.policies.items():
        places[name] = [
            _statement_place(name, *values.role.origin(name, index, service.conf_path))
            for index in range(len(statements))
        ]
    # 中身を作るところは AWS を呼ばないので、先にすべて作り、誤りがあれば検査の前に止める
    for name, statements in values.role.policies.items():
        made[policy.policy_name(name)] = policy.document(
            statements, policy.variables(service), places[name]
        )
    analyzer = session.client("accessanalyzer")
    errors = 0
    failed: list[str] = []
    for name in values.role.policies:
        found = policy.validate(
            analyzer,
            made[policy.policy_name(name)],
            policy.policy_name(name),
            f"{conf.ROLE}{conf.SEPARATOR}{name}",
            places[name],
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


def _statement_place(name: str, path: Path, index: int) -> str:
    """Statement の場所の表示。"<ファイル>: role.<ポリシー名>[<そのファイルの中での位置>]"。"""
    return f"{printable(tilde(path))}: {conf.ROLE}{conf.SEPARATOR}{name}[{index}]"


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

    conf は書く雛形（設定ファイルがあれば中身は無い）。command はサービスのコマンドを
    書くか。
    """

    conf: ConfDiff
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
    scaffold: ConfDiff,
    given: str | None,
) -> Plan:
    """手順 2〜6 の差分を、手順の順に検出する。何も書き込まない。

    values は設定ファイル（無ければ雛形 scaffold）を読んだもので、手順 2 で埋める値を
    入れて後の手順で使う。given は --image-id の値。見せる変更は、検出するたびに shown に
    足していく。手順の中で止まるときも、それまでに検出した変更は shown に残る。手順 1 は
    呼ぶ側が先に済ませる。
    """
    ec2, _, iam = clients
    # 手順 2
    shown += scaffold.changes()
    _check_image_option(service, values, given)
    _check_profile(service, values)
    known = ltbuild.fill(ec2, service, values)
    # 手順 3
    dirs = tuple(home.detect_dirs())
    shown += home.dir_changes(list(dirs))
    command = home.command_missing(service)
    shown += home.command_changes(service, command)
    # 手順 4
    image, versions = _image(clients, service, values, given)
    built = ltbuild.build(ec2, service, values, image=image, known=known)
    _check_ingress(service, values, built)
    policies = _role_policies(session, service, values) if values.role else None
    # 手順 5
    found = resources.detect(ec2, iam, service, values, built, policies=policies, versions=versions)
    shown += found.changes()
    # [role] が無ければ、起動テンプレートの後に、ロールを手放す差分を検出する。IAM を
    # 読むところで止まるときも、起動テンプレートまでの変更は shown に残る
    found = resources.with_release(iam, service, found, values.AwsRegion)
    if found.release is not None:
        shown += found.release.changes()
    host = home.detect_host_block(service)
    # Host ブロックが既定の鍵を使うなら、無ければ作る。利用者が替えた鍵には触らない
    path = home.default_key(host, service)
    key = None if path is None else keys.detect(path)
    if key is not None:
        shown += key.changes()
    shown += home.host_block_changes(service, host)
    include = home.detect_include()
    shown += home.include_changes(include, host)
    return Plan(scaffold, dirs, command, found, key, host, include)


def _update(service: Service, clients: Clients, plan: Plan) -> None:
    """_detect が検出した差分で、手順の順に更新する。変えたものだけを知らせる。"""
    ec2, _, iam = clients
    # 手順 2。確かめてから書くまでにファイルができていれば、上書きせずに止まる
    if plan.conf.text is not None:
        conf.create(service, plan.conf.text)
        msg.info(f"wrote {printable(tilde(service.conf_path))}")
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


def run(service: Service, *, image: str | None = None, yes: bool) -> None:
    """サービスの AWS リソース、ssh の設定と鍵を揃える。設定ファイルが無ければ雛形を書く。

    すべての差分を先に検出し、違いが無ければそう知らせて終わる。違いがあれば diff と同じ
    形で表示し、確認を求めてから更新する。確認の前には何も書かない。既にある設定ファイルには
    何も書かない。image は --image-id の値で、起動テンプレートの AMI をそれにする。
    """
    # 手順 1。同じサービスを別の場所から apply していれば、何かを書く前に止める。
    # AWS のリソースも同じ名前なので、別の定義で上書きしてしまう
    home.check_owner(service)
    scaffold = ConfDiff()
    if service.conf_path.exists():
        values = conf.load(service)
        session = aws.session(values)
    else:
        # 手順 2。雛形を設定ファイルの代わりに読み、書くのは更新のとき
        scaffold = _scaffold(service)
        values = conf.parse_text(scaffold.text or "", service.conf_path)
        # 共通ファイルの ImageId と --image-id の誤りは、作るかを聞く前に出す
        _check_image_option(service, values, image)
        session = aws.session(values)
        # 打ち間違えた名前でサービスを作り始めないよう、AWS のリソースを読む前に聞く
        aws.confirm(
            session,
            f"no config file: {printable(tilde(service.conf_path))}; create service {service.name}",
            yes=yes,
        )
    clients = _clients(session)
    shown: list[Change] = []
    plan = _detect(service, values, session, clients, shown, scaffold, image)
    if shown:
        msg.tell(change.shown_changes(shown))
        aws.confirm(session, "apply these changes", yes=yes)
    elif scaffold.text is None:
        msg.info(f"{service.name}: no differences")
        return
    # 雛形のほかに違いが無くても（雛形が書く値が無く、AWS も揃っている）、雛形は書く。
    # 作るかは、最初に聞いてある
    _update(service, clients, plan)


def diff(service: Service, *, image: str | None = None) -> None:
    """次の apply が何を変えるかを、1 つの変更を 1 行で標準出力に書く。

    差分を検出するだけで、何も書き込まない。apply が止まるところで止まるときは、
    それまでに検出した変更を書いてから、同じエラーで終わる。標準出力には変更だけを
    書くよう、途中の知らせは標準エラー出力に回す。image は --image-id の値で、同じ
    --image-id を付けた apply が変えるものを書く。
    """
    with msg.stderr_only():
        _diff(service, image)


def _diff(service: Service, image: str | None) -> None:
    """diff の本体。設定ファイルが無ければ、apply を促して止まる。"""
    shown: list[Change] = []
    try:
        home.check_owner(service)
        values = conf.load(service)
        session = aws.session(values)
        _ = _detect(service, values, session, _clients(session), shown, ConfDiff(), image)
    except Exception:
        change.write(shown)
        raise
    if not shown:
        # 標準出力が空なら違いが無いと分かるよう、標準エラー出力に書く
        msg.info(f"{service.name}: no differences", side=True)
        return
    change.write(shown)
