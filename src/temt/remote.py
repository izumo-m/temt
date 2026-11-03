"""インスタンスに ssh でつなぐ道具と、インスタンスの上のアプリと deploy の記録を読む本体。

deploy と app remove は、apply が用意した Host <名前> に OpenSSH の ssh でつなぐ。ssh の
ProxyCommand の proxy には、コマンドが決めたリージョンとプロファイルを環境変数で渡す
（proxy.passed_env）。インスタンスの上では sudo で動く。表示するだけの app show と
status --apps は、ssh ではなく SSM の Run Command で読む。

deploy の記録は、インスタンスのアプリのディレクトリにある temt-deploy.json である。
deploy がアプリと一緒に送り、ディレクトリと一緒に入れ替わり、焼けば AMI に入る。記録の
instance が読んだインスタンスでなければ、そのアプリは AMI から来た、つまり焼いてある。
アプリ名と版は、同じディレクトリの manifest.toml にある。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。
"""

from __future__ import annotations

import base64
import io
import json
import re
import shlex
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from temt import instances, msg, options, proc, table, wait
from temt.aws import error_code
from temt.aws import field as aws_field
from temt.manifest import MANIFEST, parse_manifest
from temt.msg import TemtError
from temt.names import PREFIX, is_label

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_ssm.client import SSMClient

    from temt.instances import Instance
    from temt.manifest import Manifest
    from temt.names import Service

SSH = "ssh"
# インスタンスの上で、cloud-init が自分のインスタンス ID を書くファイル
INSTANCE_ID_FILE = "/var/lib/cloud/data/instance-id"
INSTANCE_ID_RE = re.compile(r"\bi-[0-9a-f]+\b")
# インスタンスの上でアプリを置くディレクトリ
REMOTE_ROOT = "/opt/temt/apps"
# アプリのディレクトリに temt が足す deploy の記録
RECORD_FILE = "temt-deploy.json"
# ssh が自分の失敗に使う終了コード
SSH_FAILED = 255
# deploy の記録の項目。書く順に並べる。options はオプションの名前から値への対応
# （JSON のオブジェクト）で、ほかは文字列である
OPTIONS = "options"
TEXT_FIELDS = ("user", "deployed", "instance")
# 焼いてあるかの表示。記録が無いか読めなければ table.EMPTY
BAKED_YES = "yes"
BAKED_NO = "no"
# 記録の options が読めないときの理由と、app show の表示
_SHAPE = "not a JSON object of option names and values"
UNREADABLE = "(unreadable)"

# アプリのディレクトリの manifest.toml と記録を、ディレクトリと一緒に tar.gz にして標準出力に
# 書く。$1 はアプリを置く親ディレクトリ、$2 はアプリ ID で、省けばすべてのアプリ。親か
# そのアプリのディレクトリが無ければ、何も書かずに終わる。名前は NUL で区切って tar に
# 渡すので、改行を含む名前でも崩れない。ディレクトリは中身を入れずに入れ、ある
# ことだけを伝える
READ_SCRIPT = f"""set -eu
[ -d "$1" ] || exit 0
cd "$1"
top=.
depth=1
if [ $# -gt 1 ]; then
    [ -d "$2" ] || exit 0
    top=./$2
    depth=0
fi
{{
    find "$top" -mindepth "$depth" -maxdepth "$depth" -type d -print0
    find "$top" -mindepth "$((depth + 1))" -maxdepth "$((depth + 1))" -type f \\
        \\( -name {MANIFEST} -o -name {RECORD_FILE} \\) -print0
}} | tar -czf - --null --no-recursion -T -
"""


# READ_SCRIPT の出力を、Run Command が返せる文字にする。途中で失敗すれば失敗で終わる
ENCODED_READ_SCRIPT = "set -o pipefail\n" + READ_SCRIPT.rstrip("\n") + " | base64 -w 0\n"

# Run Command。受け取りと実行の締め切り（秒）は send-command が受け付ける最小で、待つのは
# その和に間隔を足したところまで
DOCUMENT = "AWS-RunShellScript"
DELIVERY_TIMEOUT = 30
EXECUTION_TIMEOUT = 30
COMMAND_DEADLINE = DELIVERY_TIMEOUT + EXECUTION_TIMEOUT + wait.INTERVAL
# get-command-invocation が返す標準出力の上限（文字）。これを超えると切られる
OUTPUT_MAX = 24000
# Run Command の状態
SUCCESS = "Success"
UNFINISHED = ("Pending", "InProgress", "Delayed", "Cancelling")
# SSM エージェントが受け取れないインスタンスを指したときの誤りの名前
NOT_REACHABLE = "InvalidInstanceId"
# send-command の直後に、まだ実行の記録が無いときの誤りの名前
NO_INVOCATION = "InvocationDoesNotExist"


def remote(script: str, *args: str) -> str:
    """インスタンスの上で、script を root の bash で実行するコマンドライン。"""
    return " ".join(
        ["sudo", "-n", "bash", "-c", shlex.quote(script), PREFIX, *map(shlex.quote, args)]
    )


def ssh(
    service: Service, command: str, data: bytes | None = None, *, env: Mapping[str, str]
) -> int:
    """Host <名前> に ssh でつなぎ、command を実行して終了コードを返す。

    env は ssh の環境で、proxy に決めたリージョンとプロファイルを渡す（proxy.passed_env）。
    """
    return proc.stream([SSH, "-T", service.name, command], data, env=env)


def reach(service: Service, one: Instance, env: Mapping[str, str], *, note: str = "") -> None:
    """ssh でつながり、sudo が使え、つながった先が one であることを確かめる。

    何かを変える前や、何かを読む前に呼ぶ。初めてつなぐインスタンスのホスト鍵を
    確かめられないときなどに、何も変えずに止まれる。proxy には決めたリージョンと
    プロファイルを渡すが、利用者の ControlMaster が別のリージョンやアカウントへの前の
    接続を使い回すと、ssh は別のインスタンスにつながりうる。そのときも止める。
    ProxyCommand の待ちの知らせと ssh の誤りは、そのまま見せる。env は ssh の環境で、
    note は止めるときの誤りに添える文。
    """
    msg.info(f"checking that 'ssh {service.name}' reaches {one.id}")
    code, out = proc.read([SSH, "-T", service.name, f"sudo -n cat {INSTANCE_ID_FILE}"], env=env)
    if code != 0:
        raise TemtError(f"cannot reach {one.id} with 'ssh {service.name}' (exit {code}){note}")
    # ログインシェルが何かを書いても、ID だけを取り出す
    found = INSTANCE_ID_RE.findall(out)
    if found[-1:] != [one.id]:
        raise TemtError(
            f"'ssh {service.name}' reached {found[-1] if found else 'an unknown instance'}, "
            f"not {one.id}{note}"
        )


def running_one(found: list[Instance]) -> tuple[Instance | None, str]:
    """読むために、running のインスタンスがちょうど 1 台あるかを見る。

    終了の途中のものは数えない。あればそれと空の文を、無ければ None と読まない理由を返す。
    """
    found = instances.remaining(found)
    if not found:
        return None, "no instance"
    if len(found) > 1:
        return None, f"{len(found)} instances"
    one = found[0]
    if one.state != instances.RUNNING:
        return None, f"{one.id} is {one.state}"
    return one, ""


@dataclass
class Record:
    """deploy の記録。"""

    # TEXT_FIELDS の項目。値はどれも文字列
    fields: dict[str, str] = field(default_factory=dict)
    # 入れたときに指定したオプション。記録か options が読めなければ None
    options: dict[str, str] | None = field(default_factory=dict)
    # 記録そのものが読めたか
    readable: bool = True
    # options が読めなかったか。読めなければ app show が UNREADABLE と出す
    unread_options: bool = False


def _option_values(found: object) -> dict[str, str] | None:
    """記録の options を読む。形が違えば None。"""
    if found is None:
        return {}
    if not isinstance(found, dict):
        return None
    values: dict[str, str] = {}
    for key, value in found.items():
        if not isinstance(key, str) or not is_label(key):
            return None
        if not isinstance(value, str) or not options.is_value(value):
            return None
        values[key] = value
    return values


def parse_record(where: str, data: bytes) -> Record:
    """deploy の記録を読む。読めなければ警告して、空の記録とする。

    記録を書くのは temt だが、インスタンスの root は誰でも書き換えられるので、形を
    確かめる。JSON のオブジェクトでないか、TEXT_FIELDS の項目が文字列でないか、UTF-8 で
    書けない値（対にならないサロゲートを表す \\u の形）を含む記録は、読めない記録として
    空にし、options を None とする。ほかの項目は見ない。options の形が違えば、それだけを
    None とする。deploy は、読めない options を引き継がずに止める。where は警告に添える場所。
    """
    try:
        found: Any = json.loads(data.decode("utf-8"))
    except ValueError:
        found = None
    if not isinstance(found, dict):
        msg.warn(f"cannot read the deploy record {where}: not a JSON object")
        return Record(options=None, readable=False)
    texts = {key: found[key] for key in TEXT_FIELDS if found.get(key) is not None}
    if any(not isinstance(value, str) for value in texts.values()):
        msg.warn(f"cannot read the deploy record {where}: {', '.join(TEXT_FIELDS)} must be strings")
        return Record(options=None, readable=False)
    kept = {**texts, OPTIONS: found.get(OPTIONS)}
    if not options.is_utf8(json.dumps(kept, ensure_ascii=False)):
        msg.warn(f"cannot read the deploy record {where}: not valid UTF-8")
        return Record(options=None, readable=False)
    record = Record(fields=texts, options=_option_values(found.get(OPTIONS)))
    if record.options is None:
        msg.warn(f"cannot read {OPTIONS} in the deploy record {where}: {_SHAPE}")
        record.unread_options = True
    return record


def record_data(user: str, deployed: str, instance: str, values: Mapping[str, str]) -> bytes:
    """書く deploy の記録。日本語などの値もエスケープせずに、そのまま書く。

    空のオプションは書かない。
    """
    found: dict[str, Any] = {"user": user, "deployed": deployed, "instance": instance}
    if values:
        found[OPTIONS] = dict(values)
    return (json.dumps(found, ensure_ascii=False, indent=2) + "\n").encode()


def baked(record: Record | None, instance_id: str) -> str:
    """アプリを焼いてあるか。記録の instance が instance_id でなければ AMI から来た。

    記録が無いか、読めないか、instance が無ければ分からないので table.EMPTY。
    """
    if record is None or not record.readable or "instance" not in record.fields:
        return table.EMPTY
    return BAKED_NO if record.fields["instance"] == instance_id else BAKED_YES


@dataclass
class Installed:
    """インスタンスに入っているアプリ 1 つ。"""

    app_id: str
    # manifest.toml。無いか読めなければ None
    manifest: Manifest | None = None
    # deploy の記録。無ければ None。読めなければ readable が偽
    record: Record | None = None


def _member_parts(name: str) -> list[str]:
    """tar の項目の名前を、アプリを置く親ディレクトリから見た部分に分ける。"""
    return [part for part in name.split("/") if part not in ("", ".")]


def _parse(one: Instance, data: bytes) -> dict[str, Installed]:
    """READ_SCRIPT が出した tar.gz を読み、アプリ ID から Installed への対応を返す。

    名前がアプリ ID の規則に外れるディレクトリは除く。manifest.toml が読めなければ警告して
    None とし、記録が読めなければ警告して readable を偽とする。空なら、アプリは無い。
    """
    found: dict[str, Installed] = {}
    if not data:
        return found
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                parts = _member_parts(member.name)
                if not parts or not is_label(parts[0]):
                    continue
                app = found.setdefault(parts[0], Installed(parts[0]))
                if len(parts) != 2 or not member.isfile():
                    continue
                content = tar.extractfile(member)
                body = content.read() if content is not None else b""
                where = table.printable(f"{one.id}:{REMOTE_ROOT}/{parts[0]}/{parts[1]}")
                if parts[1] == MANIFEST:
                    try:
                        app.manifest = parse_manifest(body, where)
                    except TemtError as e:
                        msg.warn(f"cannot read the manifest of {parts[0]}: {e}")
                elif parts[1] == RECORD_FILE:
                    app.record = parse_record(where, body)
    except (tarfile.TarError, EOFError, OSError) as e:
        raise TemtError(f"cannot read the apps in {REMOTE_ROOT} on {one.id}: {e}") from None
    return dict(sorted(found.items()))


def _args(app_id: str | None) -> tuple[str, ...]:
    """READ_SCRIPT に渡す引数。app_id があれば、そのアプリだけを読む。"""
    return (REMOTE_ROOT,) if app_id is None else (REMOTE_ROOT, app_id)


def read_apps(
    service: Service, one: Instance, env: Mapping[str, str], app_id: str | None = None
) -> dict[str, Installed]:
    """ssh でつないで one の上のアプリを読む。deploy と app remove が使う。

    app_id を与えれば、そのアプリだけを読む。ssh か読み取りに失敗すれば止める。呼ぶ側が
    先に reach でつながる先を確かめておく。
    """
    command = remote(READ_SCRIPT, *_args(app_id))
    code, data = proc.capture([SSH, "-T", service.name, command], env=env)
    if code != 0:
        raise TemtError(f"cannot read the apps in {REMOTE_ROOT} on {one.id} (exit {code})")
    return _parse(one, data)


def readable(ssm: SSMClient, found: list[Instance]) -> tuple[Instance | None, str]:
    """Run Command で読むために、running のインスタンスがちょうど 1 台あり、その SSM
    エージェントが Online かを見る。読めるならそれと空の文を、読めないなら None と理由を返す。
    """
    one, why = running_one(found)
    if one is None:
        return None, why
    try:
        online = instances.is_online(ssm, one.id)
    except (ClientError, BotoCoreError) as e:
        return None, f"cannot check the SSM agent on {one.id}: {table.aws_error(e)}"
    if not online:
        return None, f"the SSM agent on {one.id} is not online"
    return one, ""


def _send(ssm: SSMClient, one: Instance, app_id: str | None) -> str:
    """READ_SCRIPT の出力を base64 で書く Run Command を送り、その ID を返す。"""
    script = shlex.quote(ENCODED_READ_SCRIPT)
    command = " ".join(["bash", "-c", script, PREFIX, *map(shlex.quote, _args(app_id))])
    try:
        sent = ssm.send_command(
            InstanceIds=[one.id],
            DocumentName=DOCUMENT,
            Parameters={"commands": [command], "executionTimeout": [str(EXECUTION_TIMEOUT)]},
            TimeoutSeconds=DELIVERY_TIMEOUT,
            Comment="temt: read the apps",
        )
    except ClientError as e:
        if error_code(e) != NOT_REACHABLE:
            raise
        raise TemtError(f"the SSM agent on {one.id} cannot take a command") from None
    return aws_field(sent.get("Command") or {}, "CommandId")


def read_apps_by_command(
    ssm: SSMClient, one: Instance, app_id: str | None = None
) -> dict[str, Installed]:
    """SSM の Run Command で one の上のアプリを読む。app show と status --apps が使う。

    ssh のセッションを開かないので、idle-shutdown の最終利用時刻を進めない。表示するだけの
    コマンドで、自動終了を先に延ばさないためである。app_id を与えれば、そのアプリだけを
    読む。送れないか、失敗するか、締め切りまでに終わらなければ止める。AWS の誤りも
    TemtError にして、呼ぶ側が警告して続けられるようにする。呼ぶ側が先に readable で
    確かめておく。
    """
    try:
        return _read_by_command(ssm, one, app_id)
    except (ClientError, BotoCoreError) as e:
        raise TemtError(f"cannot read the apps on {one.id}: {table.aws_error(e)}") from None


def _read_by_command(ssm: SSMClient, one: Instance, app_id: str | None) -> dict[str, Installed]:
    """read_apps_by_command の本体。AWS の誤りはそのまま上げる。"""
    command_id = _send(ssm, one, app_id)

    def look() -> str | None:
        try:
            got = ssm.get_command_invocation(CommandId=command_id, InstanceId=one.id)
        except ClientError as e:
            # 送った直後は、まだ実行の記録が無い
            if error_code(e) not in (NO_INVOCATION, NOT_REACHABLE):
                raise
            return None
        status = got.get("Status") or ""
        if status in UNFINISHED:
            return None
        output = got.get("StandardOutputContent") or ""
        if status != SUCCESS:
            detail = status or "(no status)"
            error = (got.get("StandardErrorContent") or "").strip()
            if error:
                detail += f": {error.splitlines()[0].strip()}"
            raise TemtError(f"cannot read the apps on {one.id}: {table.printable(detail)}")
        if len(output) >= OUTPUT_MAX:
            raise TemtError(
                f"cannot read the apps on {one.id}: the output is over the {OUTPUT_MAX} "
                "characters that Run Command returns"
            )
        return output

    output = wait.until(COMMAND_DEADLINE, f"Run Command {command_id} on {one.id}", look)
    try:
        data = base64.b64decode(output.strip(), validate=True)
    except ValueError:
        raise TemtError(f"cannot read the apps on {one.id}: not base64") from None
    return _parse(one, data)
