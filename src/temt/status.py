"""instance show（短縮形 status）と instance list の本体。

instance show は、起動テンプレートの有無、サービスのインスタンス、そのパブリック
IPv4 アドレス、SSM エージェントの状態、sshd の待ち受けポートを揃える関連付けが
実行し終えたか、焼いていないアプリ、アプリが SSM パラメータ
/<名前>/<アプリID>/status に書いた状態を表示する。状態のうち、今動いている
インスタンスが書いたとみなせないものには印を付ける。インスタンスが 2 台以上あれば
警告する。表示するだけなので、ロックは取らない。テンプレートが無くても止めず、
apply されていないことを表示する。

instance list は、temt のインスタンス（temt:service タグを持ち、terminated でない
もの）を 1 台 1 行の表で挙げる。docker の ps にあたる。SSM は見ない。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from temt import app, aws, conf, instances, msg, resources, sshd, table
from temt.names import STATUS_KEY, is_label

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ec2.type_defs import FilterTypeDef
    from types_boto3_ssm.client import SSMClient

    from temt.instances import Instance
    from temt.names import Service

# エージェントが一度も登録していないインスタンスの表示
NOT_REGISTERED = "(not registered)"
# パブリック IPv4 アドレスが付いていないインスタンスの表示
NO_ADDRESS = "(none)"
# 関連付け <名前>-sshd のインスタンスでの状態の表示。今の版が Success なら applied、
# 実行を待っているか前の版の状態しか無ければ pending。ほかは AWS の状態を小文字で出す
SSHD_APPLIED = "applied"
SSHD_PENDING = "pending"
# 状態の一覧に関連付けが無いインスタンスの表示
SSHD_NOT_RUN = "(not run)"
# 焼いていないアプリの行の、すべて焼いてあるときと、記録が 1 つも無いときの表示
ALL_BAKED = "all baked"
NO_APPS = "none"
# アプリの状態の行のラベルと、1 つも無いときの表示
APP_STATUS = "app status"
NO_APP_STATUS = "none"
# 復号せずに読むと暗号文になる SecureString の値の代わりに出すもの
SECURE_STRING = "SecureString"
HIDDEN = "(SecureString)"
# 更新からの経過時間。これより短ければ、時計のずれで負のときも含めて JUST_NOW と出す
JUST_NOW = "just now"
AGE_UNITS = (("d", 86400), ("h", 3600), ("m", 60))
# 更新時刻が読めないときの経過時間の表示
UNKNOWN_AGE = "?"
# 今動いているインスタンスが書いたとみなせない状態の、経過時間に添える印
STALE = "stale"

COLUMNS = table.Columns(
    defaults=("ID", "SERVICE", "STATE", "TYPE", "LAUNCHED"),
    extras=("IMAGE", "IP"),
)


def _ping_statuses(ssm: SSMClient, ids: list[str]) -> dict[str, str]:
    """SSM エージェントの状態を ID ごとに返す。一度も登録していないものは入らない。

    1 サービスにつき 1 台なので、ページは繰らない。
    """
    found = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": ids}]).get(
        "InstanceInformationList"
    )
    return {
        item.get("InstanceId") or "": item.get("PingStatus") or NOT_REGISTERED
        for item in found or []
    }


def _line(label: str, value: str) -> None:
    """表示の 1 行。値の頭を、いちばん長いラベル sshd-ports: に揃える。"""
    msg.tell(f"{label + ':':<12}{value}\n")


def _show_template(ec2: EC2Client, service: Service) -> bool:
    """起動テンプレートの有無を表示し、あるかを返す。無ければ apply されていない。"""
    if resources.has_template(ec2, service):
        _line("template", service.launch_template)
        return True
    _line("template", f"none (not applied; run 'temt apply --svc {service.svc}')")
    return False


def _sshd_ports(ssm: SSMClient, instance_id: str, current: sshd.Association) -> str:
    """そのインスタンスで、関連付け <名前>-sshd の今の版が実行し終えたかの表示。

    current は sshd.find で読んだ今の版。
    """
    runs = sshd.instance_status(ssm, current, instance_id)
    if not runs:
        return SSHD_NOT_RUN
    found = sshd.outcome(runs, current)
    if found is None:
        return SSHD_PENDING
    return SSHD_APPLIED if found == sshd.SUCCESS else found.lower()


def _show_instances(ssm: SSMClient, service: Service, found: list[Instance]) -> None:
    """インスタンスとそのアドレス、エージェントの状態、関連付けの状態を表示する。

    関連付けそのものが無ければ、インスタンスでの状態は読まずに apply を促す。
    """
    if not found:
        _line("instance", "none")
        return
    pings = _ping_statuses(ssm, [one.id for one in found])
    current = sshd.find(ssm, service)
    for one in found:
        _line("instance", f"{one.id} ({one.state})")
        _line("ipv4", one.public_ipv4 or NO_ADDRESS)
        _line("ssm-agent", pings.get(one.id, NOT_REGISTERED))
        if current is not None:
            _line("sshd-ports", _sshd_ports(ssm, one.id, current))
        else:
            _line(
                "sshd-ports",
                f"none (no association {service.sshd_association}; "
                f"run 'temt apply --svc {service.svc}')",
            )


def apps_line(found: list[tuple[str, dict[str, str]]], where: app.Place) -> str:
    """焼いていないアプリの行の値。unbaked と lost のアプリを、その順に挙げる。

    どちらも無ければ、焼いたアプリがあれば ALL_BAKED、無ければ NO_APPS。焼いたかどうか
    分からない記録は数えない。
    """
    states = [(app_id, app.state(record, where)) for app_id, record in found]
    groups = [
        f"{label} {', '.join(app_id for app_id, one in states if one == label)}"
        for label in (app.UNBAKED, app.LOST)
        if any(one == label for _, one in states)
    ]
    if groups:
        return "; ".join(groups)
    return ALL_BAKED if any(one == app.BAKED for _, one in states) else NO_APPS


def age(modified: Any, now: datetime) -> str:
    """更新時刻 modified から now までの経過時間の表示。

    60 秒未満（時計のずれで負のときも含む）は just now。それ以上は、いちばん大きい単位
    1 つで切り捨てて 3m ago、2h ago、5d ago のように出す。時刻が読めなければ ?。
    """
    if not isinstance(modified, datetime):
        return UNKNOWN_AGE
    seconds = int((now - modified).total_seconds())
    for unit, size in AGE_UNITS:
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return JUST_NOW


def stale(modified: Any, found: list[Instance]) -> bool:
    """更新時刻 modified の状態が、今動いているインスタンスが書いたとみなせないものか。

    pending か running のインスタンスが無いか、どれも modified より後に起動していれば
    真。停止したものを再開すると起動した時刻が進むので、停止する前の状態も古いとみなす。
    更新時刻や起動した時刻が読めず、比べられなければ偽。
    """
    live = [one for one in found if one.state in instances.USABLE_STATES]
    if not live:
        return True
    if not isinstance(modified, datetime):
        return False
    return all(one.launched is not None and modified < one.launched for one in live)


def app_statuses(
    ssm: SSMClient, service: Service, found: list[Instance]
) -> list[tuple[str, str, str]]:
    """アプリが書いた状態を、(アプリ ID、値、経過時間) の並びでアプリ ID の順に返す。

    名前が /<名前>/<アプリID>/status の形で、アプリ ID が規則に従うものだけを使う。
    値は表示できない文字を ? にする。SecureString は復号せずに読むと暗号文になるので、
    値の代わりに HIDDEN を出す。found はサービスのインスタンスで、そのどれも書いたと
    みなせない状態は、経過時間に STALE を添える。
    """
    now = datetime.now(UTC)
    shown: list[tuple[str, str, str]] = []
    for app_id, one in app.with_key(ssm, service, STATUS_KEY):
        if not is_label(app_id):
            continue
        if one.get("Type") == SECURE_STRING:
            value = HIDDEN
        else:
            value = table.printable(str(one.get("Value") or ""))
        modified = one.get("LastModifiedDate")
        when = age(modified, now)
        if stale(modified, found):
            when = f"{when}, {STALE}"
        shown.append((app_id, value, when))
    shown.sort(key=lambda item: item[0])
    return shown


def _show_app_statuses(ssm: SSMClient, service: Service, found: list[Instance]) -> None:
    """アプリの状態を、アプリ 1 つ 1 行で表示する。1 つも無ければ none。

    行は、空白 2 つ、いちばん長いアプリ ID の幅に揃えたアプリ ID、空白 2 つ、値、
    空白 2 つ、[経過時間] の順。値は揃えない。古い状態は [経過時間, stale] になる。
    """
    shown = app_statuses(ssm, service, found)
    if not shown:
        _line(APP_STATUS, NO_APP_STATUS)
        return
    msg.tell(f"{APP_STATUS}:\n")
    width = max(len(app_id) for app_id, _, _ in shown)
    for app_id, value, when in shown:
        msg.tell(f"  {app_id:<{width}}  {value}  [{when}]\n")


def run(service: Service) -> None:
    """サービスの今の状態を表示する。"""
    values = conf.load(service)
    session = aws.session(values)
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    applied = _show_template(ec2, service)
    found = instances.find(ec2, service)
    _show_instances(ssm, service, found)
    # 終了の途中のものは数えない。余分なものは ID を指して終了させる
    count = len(instances.remaining(found))
    if count > 1:
        msg.warn(instances.extra(service, count))
    # 焼いたかは起動テンプレートの AMI と比べて決めるので、テンプレートが無ければ出さない
    if applied:
        where = app.place(resources.default_image(ec2, service), found)
        deployed = [(app_id, record.fields) for app_id, record in app.records(ssm, service)]
        _line("apps", apps_line(deployed, where))
    _show_app_statuses(ssm, service, found)


def _instance_row(item: Mapping[str, Any]) -> dict[str, str]:
    """インスタンス 1 台の行。"""
    return {
        "ID": str(item.get("InstanceId") or ""),
        "SERVICE": aws.tag(item, aws.TAG_SERVICE),
        "STATE": str((item.get("State") or {}).get("Name") or ""),
        "TYPE": str(item.get("InstanceType") or ""),
        "LAUNCHED": aws.cell_time(item.get("LaunchTime")),
        "IMAGE": str(item.get("ImageId") or ""),
        "IP": str(item.get("PublicIpAddress") or ""),
    }


def _launched(item: Mapping[str, Any]) -> datetime:
    """並べ替えに使う起動時刻。読めなければ、いちばん前に置く。"""
    value = item.get("LaunchTime")
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value
    return datetime.min.replace(tzinfo=UTC)


def list_instances(service: Service | None, region: str | None, spec: str | None) -> None:
    """temt のインスタンスを、1 台 1 行の表で挙げる。

    service があれば、その設定ファイルで環境が決まり、そのサービスのものだけを挙げる。
    無ければ、--profile と --region で決まる環境の、すべてのサービスのものを挙げる。
    サービス名、起動時刻、ID の順に並べる。
    """
    chosen = COLUMNS.choose(spec)
    if service is None:
        ec2 = aws.environment(region).client("ec2")
        wanted: FilterTypeDef = {"Name": "tag-key", "Values": [aws.TAG_SERVICE]}
    else:
        ec2 = aws.session(conf.load(service)).client("ec2")
        wanted = {"Name": f"tag:{aws.TAG_SERVICE}", "Values": [service.svc]}
    found = [
        item
        for page in ec2.get_paginator("describe_instances").paginate(
            Filters=[
                wanted,
                {"Name": "instance-state-name", "Values": list(instances.SEARCH_STATES)},
            ]
        )
        for reservation in page.get("Reservations", [])
        for item in reservation.get("Instances", [])
    ]
    # 表示はローカル時刻で、夏時間の切り替わりで前後しうるので、起動時刻そのもので並べる
    found.sort(
        key=lambda item: (
            aws.tag(item, aws.TAG_SERVICE),
            _launched(item),
            str(item.get("InstanceId") or ""),
        )
    )
    table.write(chosen, [_instance_row(item) for item in found])
