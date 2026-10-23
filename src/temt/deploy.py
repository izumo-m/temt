"""app deploy（短縮形 deploy）と app remove の本体。

deploy は、アプリのディレクトリを動いているインスタンスの /opt/temt/apps/<アプリID>/
に送って install を実行する。ディレクトリには deploy の記録 temt-deploy.json を足して
一緒に送る（remote）。アプリがオプションを宣言していれば、使う値を決めて記録に残し、
temt-options.env をディレクトリに足して install に渡す（options）。remove は uninstall を
実行してディレクトリを記録ごと消す。どちらもロールのポリシーには触らない。アプリが使う
権限は、設定ファイルの [[role.<ポリシー名>]] に書き、apply で付ける。どちらも --bake を
付ければ、終わったあとに、同じロックを持ったまま bake と同じ手順で AMI にする。

アプリは OpenSSH の ssh で、apply が用意した Host <名前> を通して運ぶ（remote）。
ディレクトリを tar.gz にして標準入力に流し、インスタンスの上では sudo で動く。途中で
失敗しても巻き戻さない。install は何度実行してもよいので、もう一度 deploy すれば揃う。

boto3 を使うので、この module は cli.run が呼ばれたときに読み込まれる。botocore の
例外もそこで包む。
"""

from __future__ import annotations

import difflib
import io
import os
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
    remote,
    resources,
    table,
)
from temt.manifest import find_app, known_apps, read_manifest
from temt.msg import TemtError
from temt.names import ROOT_USER, STATUS_KEY, app_dir, is_user_name, user_apps_dir
from temt.remote import REMOTE_ROOT, SSH, SSH_FAILED
from temt.table import tilde

if TYPE_CHECKING:
    from collections.abc import Mapping

    import boto3

    from temt.instances import Instance
    from temt.manifest import Manifest
    from temt.names import Service
    from temt.options import Choice
    from temt.remote import Installed, Record

INSTALL = "install"
UNINSTALL = "uninstall"
# 送るファイルのモードから落とすビット。グループと他人の書き込み、setuid、setgid、sticky
ALLOWED_OFF = 0o7022
# deploy の記録の時刻の形。UTC で書く
RECORD_TIME = "%Y-%m-%dT%H:%M:%SZ"
# temt がディレクトリに足すファイル（temt-options.env と temt-deploy.json）のモード
ADDED_MODE = 0o644
# 止めるときの誤りに添える文
UNCHANGED = "; nothing was changed"

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

    シンボリックリンクは、path 自身も中のものもすべて辿り、その先の中身を入れる。
    インスタンスでは普通のファイルとディレクトリになる。切れたリンクやループは、tar が
    出す OSError で止まる。added は、temt がディレクトリのすぐ下に足すファイルの名前と
    中身で、root の持ち物のモード 644 で入れる。
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", dereference=True) as tar:
        tar.add(path, arcname=".", filter=_owned)
        for name, data in (added or {}).items():
            info = _owned(tarfile.TarInfo(f"./{name}"))
            info.size = len(data)
            info.mode = ADDED_MODE
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


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


def _previous(
    service: Service, one: Instance, app_id: str, env: Mapping[str, str]
) -> Installed | None:
    """手順 2。インスタンスに入っている前のアプリと記録を読む。無ければ None。

    読めなければ何も変えずに止める。--reset でも止める。記録が無いのか読めないのかが
    分からないまま入れ替えると、前のユーザーを確かめられないからである。
    """
    try:
        return remote.read_apps(service, one, env, app_id).get(app_id)
    except TemtError as e:
        raise TemtError(f"{e}{UNCHANGED}") from None


def _where(one: Instance, app_id: str) -> str:
    """インスタンスの上の deploy の記録の場所。誤りと警告に添える。"""
    return f"{one.id}:{REMOTE_ROOT}/{app_id}/{remote.RECORD_FILE}"


def _same_user(app_id: str, where: str, before: Record | None, user: str) -> None:
    """記録の user と違うユーザーで入れ直さない。

    app remove は記録の user で uninstall を実行する。版の間でユーザーを変えると、
    新しいユーザーの uninstall は、前のユーザーの install がしたことを片づけない。
    インスタンスに記録が残っている間は、ユーザーを変えさせない。
    """
    recorded = before.fields.get("user") if before is not None else None
    if recorded is None or recorded == user:
        return
    raise TemtError(
        f"{app_id} was deployed as {table.printable(recorded)} ({where}), not {user}; deploy it "
        f"as {table.printable(recorded)}, or remove it first to change the user{UNCHANGED}"
    )


def _choose(
    app_id: str,
    manifest: Manifest,
    given: Mapping[str, str | None],
    before: Record | None,
    *,
    reset: bool,
    where: str,
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
            if before.unread_options:
                msg.warn(
                    f"dropping the options of the deploy record {where} that cannot be read; "
                    f"{app_id} {options.visible(manifest.version)} declares no options"
                )
    explicit, chosen = options.choose(
        app_id, manifest.version, manifest.options, given, kept, record=where
    )
    for one in chosen:
        msg.info(f"option {options.show(one.name, one.value)} ({one.source})")
    return explicit, chosen


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
    for name in (options.FILE, remote.RECORD_FILE):
        if os.path.lexists(path / name):
            raise TemtError(
                f"{table.printable(tilde(path / name))}: temt writes this file; rename it"
            )
    options.check_given(app_id, manifest.options, given)
    user = user or manifest.user or ROOT_USER
    session = aws.session(values)
    ec2 = session.client("ec2")
    env = proxy.passed_env(values)
    with lock.hold(service):
        # 手順 2
        resources.require_template(ec2, service)
        one = _running(service, instances.find(ec2, service), "deploy")
        remote.reach(service, one, env, note=UNCHANGED)
        installed = _previous(service, one, app_id, env)
        before = installed.record if installed is not None else None
        record_at = _where(one, app_id)
        _same_user(app_id, record_at, before, user)
        _has_user(service, one, user, "make the user, or fix user in manifest.toml or --user", env)
        explicit, chosen = _choose(app_id, manifest, given, before, reset=reset, where=record_at)
        added = {
            remote.RECORD_FILE: remote.record_data(
                user, time.strftime(RECORD_TIME, gmtime()), one.id, explicit
            )
        }
        if manifest.options:
            added[options.FILE] = options.env_file(app_id, manifest.version, chosen)
        data = archive(path, added)
        if baking:
            bake.check(ec2, service, values, one, command="deploy")
            what = f"deploy {app_id} {options.visible(manifest.version)} to {one.id} as {user}"
            _confirm(session, service, one, what, yes=yes)
        # 手順 3
        msg.info(f"sending {app_id} to {one.id}")
        code = remote.ssh(service, remote.remote(SEND_SCRIPT, REMOTE_ROOT, app_id), data, env=env)
        if code != 0:
            raise TemtError(f"cannot send {app_id} to {one.id} (exit {code}); deploy again")
        msg.info(f"running {INSTALL} of {app_id} as {user} on {one.id}")
        command = remote.remote(INSTALL_SCRIPT, f"{REMOTE_ROOT}/{app_id}", user)
        code = remote.ssh(service, command, env=env)
        if code != 0:
            raise TemtError(
                f"{INSTALL} of {app_id} failed on {one.id} (exit {code}); the files are in "
                f"place; fix it and deploy again"
            )
        # 手順 4
        if baking:
            bake.make(ec2, service, one)
            return
    msg.info(
        f"deployed {app_id} {options.visible(manifest.version)} to {one.id}; "
        f"run 'temt bake --svc {service.svc}' to keep it in the next instance"
    )


def _recorded_user(app_id: str, where: str, installed: Installed) -> str:
    """uninstall を実行するユーザー。記録の user、無ければ manifest.toml の user、無ければ root。

    記録か user が無ければ、どのユーザーで実行するかを警告する。前の temt が入れたアプリ
    には記録が無く、install を別のユーザーで実行していたかもしれないからである。記録は
    インスタンスの root が書き換えられるので、名前の規則を確かめ、外れた値は端末に出さない。
    """
    record = installed.record
    found = record.fields.get("user") if record is not None else None
    if found is not None:
        if not is_user_name(found):
            raise TemtError(f"the deploy record {where} has a bad user; give the user with --user")
        return found
    manifest_user = installed.manifest.user if installed.manifest is not None else None
    user = manifest_user or ROOT_USER
    why = "no deploy record" if record is None else "no user in the deploy record"
    msg.warn(
        f"{why} {where}; running {UNINSTALL} of {app_id} as {user}; "
        f"give the user with --user if {INSTALL} ran as another user"
    )
    return user


def remove(service: Service, app_id: str, *, user: str | None, baking: bool, yes: bool) -> None:
    """アプリを動いているインスタンスから外す。baking なら、続けて AMI にする。

    uninstall の無いアプリは外さない。中途半端に消すと、何が残ったか分からなくなる。
    インスタンスにディレクトリが無ければ、入っていないとして止める。user は --user の値で、
    省けば deploy の記録にある install を実行したユーザー、それも無ければ manifest.toml の
    user、それも無ければ root で uninstall を実行する。ディレクトリは記録ごと消える。
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
    # アプリが書いた状態。アプリと一緒に消す
    state = service.app_parameter(app_id, STATUS_KEY)
    with lock.hold(service):
        resources.require_template(ec2, service)
        one = _running(service, instances.find(ec2, service), "app remove")
        # 確認の前に、読むだけで分かることを確かめる
        remote.reach(service, one, env, note=UNCHANGED)
        check = CHECK_SCRIPT.format(absent=ABSENT_CODE, no_uninstall=NO_UNINSTALL_CODE)
        code = remote.ssh(service, remote.remote(check, directory), env=env)
        if code == ABSENT_CODE:
            raise TemtError(f"{app_id} is not deployed on {one.id}: no {directory}")
        if code == NO_UNINSTALL_CODE:
            raise TemtError(f"{app_id} has no {UNINSTALL} on {one.id}; temt cannot remove it")
        if code != 0:
            raise TemtError(f"cannot check {directory} on {one.id} (exit {code})")
        runner = user
        if runner is None:
            installed = _previous(service, one, app_id, env)
            if installed is None:
                # 確かめたあとで消えた
                raise TemtError(f"{app_id} is not deployed on {one.id}: no {directory}")
            runner = _recorded_user(app_id, _where(one, app_id), installed)
        _has_user(service, one, runner, "give another user with --user", env)
        what = f"remove {app_id} from {one.id} as {runner}"
        if baking:
            bake.check(ec2, service, values, one, command="app remove")
            _confirm(session, service, one, what, yes=yes)
        msg.info(f"running {UNINSTALL} of {app_id} as {runner} on {one.id}")
        code = remote.ssh(service, remote.remote(UNINSTALL_SCRIPT, directory, runner), env=env)
        if code != 0:
            raise TemtError(
                f"{UNINSTALL} of {app_id} or deleting {directory} failed on {one.id} "
                f"(exit {code}); fix it and remove again"
            )
        msg.info(f"{directory} on {one.id}: {resources.DELETED}")
        # 状態も消す。残すと、status が外したアプリの古い状態を出し続ける
        try:
            msg.info(f"parameter {state}: {app.delete(ssm, state)}")
        except (ClientError, BotoCoreError) as e:
            raise TemtError(
                f"{table.aws_error(e)}; {app_id} is off {one.id} but {state} is left; delete it "
                f"with 'temt app unset --svc {service.svc} {app_id} status'"
            ) from None
        if baking:
            bake.make(ec2, service, one)
            return
    msg.info(
        f"removed {app_id} from {one.id}; run 'temt bake --svc {service.svc}' to remove it "
        f"from the next instance too"
    )
