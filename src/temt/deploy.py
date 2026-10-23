"""app deploy（短縮形 deploy）と app remove の本体。

deploy は、アプリのディレクトリを動いているインスタンスの /opt/temt/apps/<アプリID>/
に送って install を実行し、deploy の記録を書く。アプリがオプションを宣言していれば、
使う値を決めて記録に残し、temt-options.env をディレクトリに足して install に渡す
（options）。remove は uninstall を実行してディレクトリを消し、記録を消す。どちらも
ロールのポリシーには触らない。アプリが使う権限は、設定ファイルの
[[role.<ポリシー名>]] に書き、apply で付ける。どちらも --bake を付ければ、
終わったあとに、同じロックを持ったまま bake と同じ手順で AMI にする。

アプリは OpenSSH の ssh で、apply が用意した Host <名前> を通して運ぶ。ssh の
ProxyCommand の proxy には、このコマンドが決めたリージョンとプロファイルを環境変数で
渡す（proxy.passed_env）。ディレクトリを tar.gz にして標準入力に流し、インスタンスの上
では sudo で動く。途中で失敗しても巻き
戻さない。install は何度実行してもよいので、もう一度 deploy すれば揃う。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import difflib
import io
import os
import re
import shlex
import tarfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from temt import (
    app,
    aws,
    bake,
    conf,
    instances,
    lock,
    msg,
    options,
    proc,
    proxy,
    resources,
    table,
)
from temt.manifest import find_app, known_apps, read_manifest
from temt.msg import TemtError
from temt.names import (
    DEPLOY_KEY,
    PREFIX,
    ROOT_USER,
    STATUS_KEY,
    app_dir,
    is_user_name,
    user_apps_dir,
)
from temt.table import tilde

if TYPE_CHECKING:
    from collections.abc import Mapping

    import boto3
    from types_boto3_ec2.client import EC2Client
    from types_boto3_ssm.client import SSMClient

    from temt.app import Record
    from temt.instances import Instance
    from temt.manifest import Manifest
    from temt.names import Service
    from temt.options import Choice

SSH = "ssh"
# インスタンスの上で、cloud-init が自分のインスタンス ID を書くファイル
INSTANCE_ID_FILE = "/var/lib/cloud/data/instance-id"
INSTANCE_ID_RE = re.compile(r"\bi-[0-9a-f]+\b")
# インスタンスの上でアプリを置くディレクトリ
REMOTE_ROOT = "/opt/temt/apps"
INSTALL = "install"
UNINSTALL = "uninstall"
# 送るファイルのモードから落とすビット。グループと他人の書き込み、setuid、setgid、sticky
ALLOWED_OFF = 0o7022
# deploy の記録の時刻の形。UTC で書く
RECORD_TIME = "%Y-%m-%dT%H:%M:%SZ"
# 入れ直しても引き継ぐ、deploy の記録の項目。baked_options も引き継ぐ
BAKED_FIELDS = ("baked_version", "baked_image")
# 焼いたあとの記録の大きさを見積もるときの AMI の ID。いまの AMI の ID と同じ長さ
IMAGE_ID_SAMPLE = "ami-0123456789abcdef0"
# temt がディレクトリに足すファイル（temt-options.env）のモード
ADDED_MODE = 0o644

# 送った tar.gz を一時ディレクトリに展開してから、古いものと入れ替える。$1 は入れる先の
# 親ディレクトリ、$2 はアプリ ID。途中で失敗すれば一時ディレクトリを消し、古いものは
# 残る。古いものを退けたあとで新しいものを入れられなければ、退けた古いものを戻す。前に
# 止まって残った一時ディレクトリは、焼く前に片づくよう、はじめに消す。入れ替えの mv に
# -T を付けるのは、行き先がディレクトリとしてあったときに、その中へ入れてしまわない
# ためである
SEND_SCRIPT = """set -eu
mkdir -p "$1"
rm -rf "$1/.$2."*
tmp=$(mktemp -d "$1/.$2.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
tar -xzf - -C "$tmp" --no-same-owner --warning=no-unknown-keyword
chmod 755 "$tmp"
if [ -e "$1/$2" ]; then
    old=$(mktemp -d "$1/.$2.old.XXXXXX")
    mv "$1/$2" "$old/"
    if ! mv -T "$tmp" "$1/$2"; then
        mv -T "$old/$2" "$1/$2"
        rmdir "$old"
        exit 1
    fi
    rm -rf "$old" || true
else
    mv -T "$tmp" "$1/$2"
fi
trap - EXIT
"""
# app remove が先に確かめる。$1 はアプリのディレクトリ
CHECK_SCRIPT = f"""if [ ! -d "$1" ]; then exit {{absent}}; fi
if [ ! -f "$1/{UNINSTALL}" ]; then exit {{no_uninstall}}; fi
"""
# ssh が自分の失敗に使う終了コード
SSH_FAILED = 255
# CHECK_SCRIPT の終了コード。ssh が自分の失敗に使う 255 とも、0 とも重ならない
ABSENT_CODE = 3
NO_UNINSTALL_CODE = 4
# $1/$3 を $2 のユーザーで実行する。$1 はアプリのディレクトリ。root ならそのまま実行し、
# 外側の sudo が作った環境（SUDO_USER など）を変えない。ほかのユーザーなら sudo -H で
# HOME をそのユーザーのものにする。カレントディレクトリはアプリのディレクトリにし、
# スクリプトが自分のファイルを相対パスで指せるようにする。スクリプトを読めるユーザーは、
# そのディレクトリにも入れる
RUN_AS = f"""as_user() {{
    cd "$1" || return
    if [ "$2" = {ROOT_USER} ]; then
        bash "$1/$3"
    else
        sudo -n -H -u "$2" bash "$1/$3"
    fi
}}
"""
# install を $2 のユーザーで実行する。$1 はアプリのディレクトリ
INSTALL_SCRIPT = (
    RUN_AS
    + f"""as_user "$1" "$2" {INSTALL}
"""
)
# uninstall を $2 のユーザーで実行し、成功したらディレクトリを root で消す。$1 はアプリの
# ディレクトリ
UNINSTALL_SCRIPT = (
    RUN_AS
    + f"""as_user "$1" "$2" {UNINSTALL} || exit $?
rm -rf "$1"
"""
)

# 記録に書く時刻。テストから差し替える
gmtime = time.gmtime


def locate(target: str) -> tuple[str, Path]:
    """deploy の引数から、アプリ ID とディレクトリを決める。

    / を含めばパスとし、最後の要素をアプリ ID とする。その名前は cli が規則で確かめて
    ある。含まなければアプリ ID とし、conf ディレクトリの apps/<アプリID>/ を探し、
    無ければ temt と一緒に配るアプリを使う。apps/ に同じ名前のディレクトリでないもの
    があれば、警告して temt と一緒に配るアプリを使う。どちらにも無ければ、使える
    アプリ ID を挙げ、近い名前があれば添える。
    """
    if "/" in target:
        path = app_dir(target)
        if not path.is_dir():
            raise TemtError(f"no app directory: {table.printable(target)}")
        return path.name, path
    mine = user_apps_dir() / target
    path = find_app(target)
    if path is not None:
        if path != mine and os.path.lexists(mine):
            msg.warn(
                f"{table.printable(tilde(mine))} is not a directory (a broken link?); "
                f"using the app that comes with temt"
            )
        return target, path
    found = known_apps()
    place = table.printable(tilde(mine.parent))
    text = f"no app {target} in {place} or among the apps that come with temt"
    if os.path.lexists(mine):
        text += f"; {table.printable(tilde(mine))} is not a directory (a broken link?)"
    close = difflib.get_close_matches(target, found, n=1)
    if close:
        text += f"; did you mean {close[0]}?"
    if found:
        text += f" (apps: {', '.join(found)})"
    raise TemtError(text)


def _owned(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """tar の項目を root の持ち物にし、root 以外が書けないモードにする。

    root の tar はモードをそのまま使うので、手元でグループや他人が書けるファイル
    （WSL の /mnt/c の下は 777 に見える）が、インスタンスで誰でも書き換えられる root の
    スクリプトになる。setuid、setgid、sticky も落とす。
    """
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode &= ~ALLOWED_OFF
    return info


def archive(path: Path, added: Mapping[str, bytes] | None = None) -> bytes:
    """ディレクトリの中身を、隠しファイルも含めて tar.gz にする。

    path 自身がシンボリックリンクなら辿る。中のシンボリックリンクはリンクのまま入れる
    ので、アプリの外を指すものはインスタンスで切れる。added は、temt がディレクトリの
    すぐ下に足すファイルの名前と中身で、root の持ち物のモード 644 で入れる。
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(path.resolve(), arcname=".", filter=_owned)
        for name, data in (added or {}).items():
            info = _owned(tarfile.TarInfo(f"./{name}"))
            info.size = len(data)
            info.mode = ADDED_MODE
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _remote(script: str, *args: str) -> str:
    """インスタンスの上で、script を root の bash で実行するコマンドライン。"""
    return " ".join(
        ["sudo", "-n", "bash", "-c", shlex.quote(script), PREFIX, *map(shlex.quote, args)]
    )


def _ssh(
    service: Service, command: str, data: bytes | None = None, *, env: Mapping[str, str]
) -> int:
    """Host <名前> に ssh でつなぎ、command を実行して終了コードを返す。

    env は ssh の環境で、proxy に決めたリージョンとプロファイルを渡す（proxy.passed_env）。
    """
    return proc.stream([SSH, "-T", service.name, command], data, env=env)


def _reach(service: Service, one: Instance, env: Mapping[str, str]) -> None:
    """ssh でつながり、sudo が使え、つながった先が one であることを確かめる。

    何かを変える前に呼ぶ。初めてつなぐインスタンスのホスト鍵を確かめられないときなどに、
    何も変えずに止まれる。proxy には決めたリージョンとプロファイルを渡すが、利用者の
    ControlMaster が別のリージョンやアカウントへの前の接続を使い回すと、ssh は別の
    インスタンスにつながりうる。そのときも止める。ProxyCommand の待ちの知らせと ssh の
    誤りは、そのまま見せる。env は ssh の環境。
    """
    msg.info(f"checking that 'ssh {service.name}' reaches {one.id}")
    code, out = proc.read([SSH, "-T", service.name, f"sudo -n cat {INSTANCE_ID_FILE}"], env=env)
    if code != 0:
        raise TemtError(
            f"cannot reach {one.id} with 'ssh {service.name}' (exit {code}); nothing was changed"
        )
    # ログインシェルが何かを書いても、ID だけを取り出す
    found = INSTANCE_ID_RE.findall(out)
    if found[-1:] != [one.id]:
        raise TemtError(
            f"'ssh {service.name}' reached {found[-1] if found else 'an unknown instance'}, "
            f"not {one.id}; nothing was changed"
        )


def _has_user(
    service: Service, one: Instance, user: str, hint: str, env: Mapping[str, str]
) -> None:
    """install と uninstall を実行するユーザーが one にいることを確かめる。何かを変える前に呼ぶ。

    hint は、いなかったときに添える直し方。
    """
    if user == ROOT_USER:
        return
    code, _ = proc.read([SSH, "-T", service.name, f"id -u -- {shlex.quote(user)}"], env=env)
    if code == SSH_FAILED:
        raise TemtError(f"cannot reach {one.id} with 'ssh {service.name}'; nothing was changed")
    if code != 0:
        raise TemtError(f"no user {user} on {one.id} (exit {code}); nothing was changed; {hint}")


def _running(service: Service, found: list[Instance], command: str) -> Instance:
    """running のインスタンスが 1 台であることを確かめる。自分では起動しない。

    終了の途中のものは、無いものとして扱う。
    """
    found = instances.remaining(found)
    if not found:
        raise TemtError(
            f"no instance for {service.name}; run 'temt start --svc {service.svc}' "
            f"and {command} again"
        )
    if len(found) > 1:
        instances.too_many(service, found, command)
    one = found[0]
    if one.state == instances.RUNNING:
        return one
    if one.state == instances.PENDING:
        raise TemtError(
            f"{one.id} is {one.state}; wait for 'temt start --svc {service.svc}' to finish "
            f"and {command} again"
        )
    # 残るのは stopped と stopping。start は停止の途中でも再開を求める
    raise TemtError(
        f"{one.id} is {one.state}; run 'temt start --svc {service.svc}' and {command} again"
    )


def _confirm(
    session: boto3.Session, service: Service, one: Instance, what: str, *, yes: bool
) -> None:
    """--bake のとき、始める前に、することをすべて挙げて確認する。"""
    lines = [what, *bake.plan(service, one)]
    msg.tell(f"{what} and bake {service.name}:\n" + "".join(f"  {line}\n" for line in lines))
    aws.confirm(session, f"{what} and bake {one.id} ({one.state})", yes=yes)


def _previous(ssm: SSMClient, name: str) -> Record | None:
    """手順 2。前の deploy の記録を読む。無ければ None。読めなければ何も変えずに止める。"""
    try:
        found = app.read(ssm, name)
    except (ClientError, BotoCoreError) as e:
        raise TemtError(
            f"{table.aws_error(e)}; cannot read the deploy record {name}; nothing was changed"
        ) from None
    if found is None:
        return None
    return app.parse_record(name, str(found.get("Value") or ""))


def _same_user(app_id: str, name: str, before: Record | None, user: str) -> None:
    """記録の user と違うユーザーで入れ直さない。

    app remove は記録の user で uninstall を実行する。版の間でユーザーを変えると、焼いて
    いない版を失って AMI の古い版に戻ったとき、記録の user が AMI の版のものでなくなる。
    記録が残っている間（AMI に焼いてあって外した removed も含む）は、ユーザーを変えさせない。
    """
    recorded = before.fields.get("user") if before is not None else None
    if recorded is None or recorded == user:
        return
    raise TemtError(
        f"{app_id} was deployed as {table.printable(recorded)} ({name}), not {user}; deploy it "
        f"as {table.printable(recorded)}, or remove it first to change the user (and bake, if "
        "it is in the AMI); nothing was changed"
    )


def _choose(
    app_id: str,
    manifest: Manifest,
    given: Mapping[str, str | None],
    before: Record | None,
    *,
    reset: bool,
    record: str,
) -> tuple[dict[str, str], list[Choice]]:
    """手順 2。使うオプションの値を決め、1 つ 1 行で出どころとともに知らせる。

    --reset なら前の記録を引き継がない。オプションを宣言していないアプリでは、読めない
    記録から引き継ぐものは無いので止めない。読めない options は、宣言していない名前と
    同じく、警告して捨てる。
    """
    kept: dict[str, str] | None = {}
    if not reset and before is not None:
        kept = before.options
        if kept is None and not manifest.options:
            kept = {}
            if app.OPTIONS in before.unread:
                msg.warn(
                    f"dropping the options of the deploy record {record} that cannot be read; "
                    f"{app_id} {options.visible(manifest.version)} declares no options"
                )
    explicit, chosen = options.choose(
        app_id, manifest.version, manifest.options, given, kept, record=record
    )
    for one in chosen:
        msg.info(f"option {options.show(one.name, one.value)} ({one.source})")
    return explicit, chosen


def _new_record(
    manifest: Manifest, user: str, one: Instance, explicit: dict[str, str], before: Record | None
) -> Record:
    """書く記録を作る。deployed はいまの時刻で、手順 4 で install を終えた時刻に書き直す。

    install を実行したユーザーも書き、app remove が uninstall を同じユーザーで実行する。
    入れたインスタンスを instance に書き、まだ焼いていないことを表す。前の記録の
    焼いた版（baked_version、baked_image、baked_options）は引き継ぐ。AMI にはその版が
    残っているからである。読めない baked_options は、書かれていたままの値を引き継ぐ。
    """
    record = app.Record(
        fields={
            "name": manifest.name,
            "version": manifest.version,
            "user": user,
            "deployed": time.strftime(RECORD_TIME, gmtime()),
            "instance": one.id,
        },
        options=explicit,
    )
    if before is not None:
        baked = {key: before.fields[key] for key in BAKED_FIELDS if key in before.fields}
        record.fields.update(baked)
        if before.baked_options is None:
            record.baked_options = None
            if app.BAKED_OPTIONS in before.unread:
                record.unread[app.BAKED_OPTIONS] = before.unread[app.BAKED_OPTIONS]
        else:
            record.baked_options = dict(before.baked_options)
    return record


def _check_size(app_id: str, record: Record) -> None:
    """手順 2。記録が、いまも焼いたあとも、SSM パラメータに収まることを確かめる。

    焼くと、オプションが baked_options にもコピーされて大きくなる。収まらなければ、何も変えずに
    止める。
    """
    baked = app.Record(
        fields=dict(record.fields),
        options=record.options,
        baked_options=record.baked_options,
        unread=dict(record.unread),
    )
    app.mark_fresh(baked, IMAGE_ID_SAMPLE)
    for value, when in ((record, ""), (baked, " after bake")):
        size = app.record_size(value)
        if size > app.RECORD_MAX:
            raise TemtError(
                f"the deploy record of {app_id} would be {size} bytes{when}, over the "
                f"{app.RECORD_MAX} bytes that SSM allows; shorten the options; nothing was changed"
            )


def _record(ssm: SSMClient, service: Service, app_id: str, record: Record, one: Instance) -> None:
    """手順 4。deploy の記録を書く。install を終えたときの時刻を UTC で書く。"""
    name = service.app_parameter(app_id, DEPLOY_KEY)
    record.fields["deployed"] = time.strftime(RECORD_TIME, gmtime())
    try:
        state = app.put(ssm, name, app.record_value(record))
    except (ClientError, BotoCoreError) as e:
        raise TemtError(
            f"{table.aws_error(e)}; {app_id} is installed on {one.id} but not recorded; "
            f"deploy again"
        ) from None
    msg.info(f"parameter {name}: {state}")


def run(
    service: Service,
    target: str,
    *,
    user: str | None,
    baking: bool,
    yes: bool,
    given: Mapping[str, str | None] | None = None,
    reset: bool = False,
) -> None:
    """アプリを動いているインスタンスに入れる。baking なら、続けて AMI にする。

    target はアプリ ID かパス。user は --user の値で、省けば manifest.toml の user、
    それも無ければ root で install を実行する。yes なら --bake の確認をしない。given は
    コマンドラインで与えたアプリのオプションで、値が None なら既定に戻す。reset なら前の記録の
    オプションを引き継がない。
    """
    given = given or {}
    values = conf.load(service)
    # 手順 1。ImageId を書いていれば、焼けないので何もせずに止まる。ここからしばらくは、AWS
    # にもインスタンスにも書かない
    if baking:
        bake.check_unfixed(service, values)
    proc.require(SSH)
    app_id, path = locate(target)
    # 利用者がリンクで置いたアプリなら、その先も添える
    where = f"{tilde(path)} -> {tilde(path.resolve())}" if path.is_symlink() else tilde(path)
    msg.info(f"using {app_id} from {table.printable(where)}")
    manifest = read_manifest(path)
    if not (path / INSTALL).is_file():
        raise TemtError(f"no {INSTALL} in {table.printable(tilde(path))}")
    if os.path.lexists(path / options.FILE):
        raise TemtError(
            f"{table.printable(tilde(path / options.FILE))}: temt writes this file; rename it"
        )
    options.check_given(app_id, manifest.options, given)
    user = user or manifest.user or ROOT_USER
    session = aws.session(values)
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    env = proxy.passed_env(values)
    record_name = service.app_parameter(app_id, DEPLOY_KEY)
    with lock.hold(service):
        # 手順 2
        resources.require_template(ec2, service)
        one = _running(service, instances.find(ec2, service), "deploy")
        _reach(service, one, env)
        before = _previous(ssm, record_name)
        _same_user(app_id, record_name, before, user)
        _has_user(service, one, user, "make the user, or fix user in manifest.toml or --user", env)
        # このインスタンスから外したアプリは、外す前のオプションを引き継がない。焼いた版は
        # AMI に残っているので、_new_record は引き継ぐ
        removed = before is not None and before.fields.get("removed") == one.id
        explicit, chosen = _choose(
            app_id, manifest, given, None if removed else before, reset=reset, record=record_name
        )
        record = _new_record(manifest, user, one, explicit, before)
        _check_size(app_id, record)
        added = None
        if manifest.options:
            added = {options.FILE: options.env_file(app_id, manifest.version, chosen)}
        data = archive(path, added)
        if baking:
            bake.check(ec2, service, values, one, command="deploy")
            what = f"deploy {app_id} {options.visible(manifest.version)} to {one.id} as {user}"
            _confirm(session, service, one, what, yes=yes)
        # 手順 3
        msg.info(f"sending {app_id} to {one.id}")
        code = _ssh(service, _remote(SEND_SCRIPT, REMOTE_ROOT, app_id), data, env=env)
        if code != 0:
            raise TemtError(f"cannot send {app_id} to {one.id} (exit {code}); deploy again")
        msg.info(f"running {INSTALL} of {app_id} as {user} on {one.id}")
        code = _ssh(service, _remote(INSTALL_SCRIPT, f"{REMOTE_ROOT}/{app_id}", user), env=env)
        if code != 0:
            raise TemtError(
                f"{INSTALL} of {app_id} failed on {one.id} (exit {code}); the files are in "
                f"place; fix it and deploy again"
            )
        # 手順 4
        _record(ssm, service, app_id, record, one)
        if baking:
            bake.make(ec2, ssm, service, one)
            return
    msg.info(
        f"deployed {app_id} {options.visible(manifest.version)} to {one.id}; "
        f"run 'temt bake --svc {service.svc}' to keep it in the next instance"
    )


def _recorded_user(name: str, found: Record | None) -> str:
    """deploy の記録にある、install を実行したユーザー。記録か user が無ければ root。

    記録か user が無ければ、root で実行することを警告する。deploy が記録を書けずに
    終わったときなどで、install を別のユーザーで実行していたかもしれないからである。
    記録は SSM パラメータで誰でも書き換えられるので、名前の規則を確かめ、外れた値は
    端末に出さない。
    """
    hint = f"give the user with --user if {INSTALL} ran as another user"
    if found is None:
        msg.warn(f"no deploy record {name}; running {UNINSTALL} as {ROOT_USER}; {hint}")
        return ROOT_USER
    user = found.fields.get("user")
    if user is None:
        msg.warn(f"no user in the deploy record {name}; running {UNINSTALL} as {ROOT_USER}; {hint}")
        return ROOT_USER
    if not is_user_name(user):
        raise TemtError(f"the deploy record {name} has a bad user; give the user with --user")
    return user


def _left(
    ec2: EC2Client, service: Service, name: str, found: Record | None, one: Instance
) -> Record | None:
    """外したあとに残す記録。アプリが起動テンプレートの AMI に焼いてなければ None。

    焼いてあれば、外したインスタンス one を書く。そのまま残すと、status と app list が
    焼いてあるとしか出さず、AMI に残っていることが分からないからである。記録が無いか
    読めなければ、焼いてあるか分からないので None。書き換えた記録が SSM パラメータに
    収まらなければ、警告して None とする。残そうとすると、やり直しても書けないからである。
    """
    if found is None:
        return None
    record = found
    image_id = record.fields.get("baked_image")
    if not image_id or image_id != resources.default_image(ec2, service):
        return None
    app.mark_removed(record, one.id)
    size = app.record_size(record)
    if size > app.RECORD_MAX:
        msg.warn(
            f"the deploy record {name} would be {size} bytes, over the {app.RECORD_MAX} bytes "
            f"that SSM allows; deleting it, although the app stays in the AMI until baked"
        )
        return None
    return record


def remove(service: Service, app_id: str, *, user: str | None, baking: bool, yes: bool) -> None:
    """アプリを動いているインスタンスから外す。baking なら、続けて AMI にする。

    uninstall の無いアプリは外さない。中途半端に消すと、何が残ったか分からなくなる。
    インスタンスにディレクトリが無ければ、記録だけを扱う。user は --user の値で、省けば
    deploy の記録にある install を実行したユーザー、それも無ければ root で uninstall を
    実行する。記録は、アプリが起動テンプレートの AMI に焼いてあれば外したことを書いて
    残し（_left）、そうでなければ消す。
    """
    values = conf.load(service)
    # ImageId を書いていれば、焼けないので何もせずに止まる
    if baking:
        bake.check_unfixed(service, values)
    proc.require(SSH)
    session = aws.session(values)
    ec2 = session.client("ec2")
    directory = f"{REMOTE_ROOT}/{app_id}"
    ssm = session.client("ssm")
    env = proxy.passed_env(values)
    record = service.app_parameter(app_id, DEPLOY_KEY)
    # アプリが書いた状態。記録と一緒に消す
    state = service.app_parameter(app_id, STATUS_KEY)
    with lock.hold(service):
        resources.require_template(ec2, service)
        one = _running(service, instances.find(ec2, service), "app remove")
        # 確認の前に、読むだけで分かることを確かめる
        _reach(service, one, env)
        check = CHECK_SCRIPT.format(absent=ABSENT_CODE, no_uninstall=NO_UNINSTALL_CODE)
        code = _ssh(service, _remote(check, directory), env=env)
        if code == NO_UNINSTALL_CODE:
            raise TemtError(f"{app_id} has no {UNINSTALL} on {one.id}; temt cannot remove it")
        if code not in (0, ABSENT_CODE):
            raise TemtError(f"cannot check {directory} on {one.id} (exit {code})")
        present = code == 0
        parameter = app.read(ssm, record)
        # ディレクトリも記録も無ければ、アプリ ID の打ち間違いとみなす
        if not present and parameter is None:
            raise TemtError(
                f"no app {app_id} for {service.name}: no {directory} on {one.id}, "
                f"and no record {record}"
            )
        # 読めない記録の警告を 1 度だけ出すよう、ここで 1 度だけ読む
        found = None
        if parameter is not None:
            found = app.parse_record(record, str(parameter.get("Value") or ""))
        what = f"remove {app_id} from {one.id}"
        # uninstall を実行するユーザー。ディレクトリが無ければ実行しないので決めない
        runner = ROOT_USER
        if present:
            runner = user or _recorded_user(record, found)
            _has_user(service, one, runner, "give another user with --user", env)
            what += f" as {runner}"
        else:
            msg.warn(f"{directory} is not on {one.id}; updating only the record")
        if baking:
            bake.check(ec2, service, values, one, command="app remove")
            _confirm(session, service, one, what, yes=yes)
        if present:
            msg.info(f"running {UNINSTALL} of {app_id} as {runner} on {one.id}")
            code = _ssh(service, _remote(UNINSTALL_SCRIPT, directory, runner), env=env)
            if code != 0:
                raise TemtError(
                    f"{UNINSTALL} of {app_id} or deleting {directory} failed on {one.id} "
                    f"(exit {code}); the record is left; fix it and remove again"
                )
            msg.info(f"{directory} on {one.id}: {resources.DELETED}")
        # 状態も消す。残すと、status が外したアプリの古い状態を出し続ける。記録を最後に
        # 扱うのは、途中で失敗しても、記録が残っていればやり直しで続きから進めるため
        try:
            msg.info(f"parameter {state}: {app.delete(ssm, state)}")
            left = _left(ec2, service, record, found, one)
            if left is None:
                msg.info(f"parameter {record}: {app.delete(ssm, record)}")
            else:
                msg.info(f"parameter {record}: {app.put(ssm, record, app.record_value(left))}")
        except (ClientError, BotoCoreError) as e:
            raise TemtError(
                f"{table.aws_error(e)}; {app_id} is off {one.id} but its parameters are left; "
                "remove again"
            ) from None
        if baking:
            bake.make(ec2, ssm, service, one)
            return
    msg.info(
        f"removed {app_id} from {one.id}; run 'temt bake --svc {service.svc}' to remove it "
        f"from the next instance too"
    )
