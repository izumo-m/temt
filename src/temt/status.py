"""instance show（短縮形 status）と instance list の本体。

instance show は、起動テンプレートの有無、サービスのインスタンス、そのパブリック
IPv4 アドレス、SSM エージェントの状態、アプリが SSM パラメータ
/<名前>/<アプリID>/status に書いた状態を表示する。状態のうち、今動いている
インスタンスが書いたとみなせないものには印を付ける。--apps を付ければ、動いている
インスタンスから Run Command で読み、入っているアプリも表示する（remote）。数秒かかる
ので、付けなければ読まない。インスタンスが 2 台以上あれば警告する。表示するだけなので、
ロックは取らない。テンプレートが無くても止めず、apply されていないことを表示する。

instance list は、temt のインスタンス（temt:service タグを持ち、terminated でない
もの）を 1 台 1 行の表で挙げる。docker の ps にあたる。SSM は見ない。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from temt import app, aws, conf, instances, msg, remote, resources, table
from temt.manifest import find_app, read_manifest
from temt.msg import TemtError
from temt.names import STATUS_KEY, is_label

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_ec2.client import EC2Client
    from types_boto3_ec2.type_defs import FilterTypeDef
    from types_boto3_ssm.client import SSMClient

    from temt.instances import Instance
    from temt.names import Service
    from temt.remote import Installed

# エージェントが一度も登録していないインスタンスの表示
NOT_REGISTERED = "(not registered)"
# パブリック IPv4 アドレスが付いていないインスタンスの表示
NO_ADDRESS = "(none)"
# 入っているアプリの行のラベルと、1 つも無いときの表示、表の見出し
APPS = "apps"
NO_APPS = "none"
APP_COLUMNS = ("ID", "VERSION", "BAKED", "DEPLOYED")
# VERSION の後ろに付ける、アプリ ID で使える手元のアプリの版がそれと違うことの印
CHANGED = "*"
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
    """表示の 1 行。値の頭を、いちばん長いラベル app status: に揃える。"""
    msg.tell(f"{label + ':':<12}{value}\n")


def _show_template(ec2: EC2Client, service: Service) -> None:
    """起動テンプレートの有無を表示する。無ければ apply されていない。"""
    if resources.has_template(ec2, service):
        _line("template", service.launch_template)
        return
    _line("template", f"none (not applied; run 'temt apply --svc {service.svc}')")


def _show_instances(ssm: SSMClient, found: list[Instance]) -> None:
    """インスタンスとそのアドレス、エージェントの状態を表示する。"""
    if not found:
        _line("instance", "none")
        return
    pings = _ping_statuses(ssm, [one.id for one in found])
    for one in found:
        _line("instance", f"{one.id} ({one.state})")
        _line("ipv4", one.public_ipv4 or NO_ADDRESS)
        _line("ssm-agent", pings.get(one.id, NOT_REGISTERED))


def _local_version(app_id: str) -> str | None:
    """アプリ ID で使える手元のアプリの版。無いか読めなければ None。"""
    path = find_app(app_id)
    if path is None:
        return None
    try:
        return read_manifest(path).version
    except TemtError as e:
        msg.warn(f"cannot read the manifest of {app_id}: {e}")
        return None


def app_rows(found: dict[str, Installed], instance_id: str) -> list[list[str]]:
    """入っているアプリの表の行。見出しは含まない。

    VERSION は、アプリ ID で使える手元のアプリの版と違えば CHANGED を付ける。deploy して
    焼けば更新になることが分かる。版は同じかどうかだけを比べる。BAKED は記録の instance
    から決める（remote.baked）。値はインスタンスから来るので、表示できない文字を置き換える。
    """
    rows: list[list[str]] = []
    for app_id, installed in found.items():
        version = installed.manifest.version if installed.manifest is not None else None
        shown = table.printable(version) if version else table.EMPTY
        local = _local_version(app_id)
        if version and local is not None and local != version:
            shown += CHANGED
        record = installed.record
        deployed = ""
        if record is not None and record.readable:
            deployed = record.fields.get("deployed", "")
        deployed = aws.cell_time(deployed) or table.printable(deployed) or table.EMPTY
        rows.append([app_id, shown, remote.baked(record, instance_id), deployed])
    return rows


def _show_apps(ssm: SSMClient, found: list[Instance]) -> None:
    """--apps のとき、running のインスタンスがちょうど 1 台あれば、入っているアプリを表で出す。

    Run Command で読む（remote）。読めなければ、読まない理由を出す。読み取りに失敗すれば
    警告して続ける。
    """
    one, why = remote.readable(ssm, found)
    if one is None:
        _line(APPS, f"(not read; {why})")
        return
    try:
        installed = remote.read_apps_by_command(ssm, one)
    except TemtError as e:
        msg.warn(str(e))
        _line(APPS, f"(not read; cannot read {one.id})")
        return
    if not installed:
        _line(APPS, NO_APPS)
        return
    msg.tell(f"{APPS}:\n")
    msg.columns([list(APP_COLUMNS), *app_rows(installed, one.id)])


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


def run(service: Service, *, apps: bool = False) -> None:
    """サービスの今の状態を表示する。apps なら、入っているアプリも表示する。"""
    values = conf.load(service)
    session = aws.session(values)
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    _show_template(ec2, service)
    found = instances.find(ec2, service)
    _show_instances(ssm, found)
    # 終了の途中のものは数えない。余分なものは ID を指して終了させる
    count = len(instances.remaining(found))
    if count > 1:
        msg.warn(instances.extra(service, count))
    if apps:
        _show_apps(ssm, found)
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
