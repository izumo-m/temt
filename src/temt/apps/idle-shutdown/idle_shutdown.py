#!/usr/bin/python3 -I
"""idle-shutdown: 誰も使っていなければ、インスタンスの電源を切る。

temt と一緒に配るアプリ idle-shutdown の本体。install が /usr/local/bin/idle-shutdown
として置き、idle-shutdown.timer が 5 分おきに引数なしで root で実行する。PAM（pam_exec
と、ログインのメッセージを作る pam_motd）からも root で呼ばれるので、python3 の -I で
環境変数とユーザーの site-packages を無視する。
インスタンスの python3 で動くので、標準ライブラリだけを使う。

次のどれかがあれば使用中とみなす。

- ログインを終えた sshd のセッション。端末の無い接続（トンネル、コマンドの実行）も
  数える。認証前の接続は数えない
- /dev/pts の端末。screen や tmux は、デタッチしてもウィンドウごとに端末を持つ
- モード block のシャットダウン抑止ロック。誰のものでも数える
- /etc/idle-shutdown/in-use.d/ の実行ファイルが、成功して何か出力したこと

使用中なら最終利用時刻を更新する。最終利用時刻は、/run/idle-shutdown/touch/ と
/run/idle-shutdown/root/ のファイルのうち最も新しい更新時刻（mtime）で、無ければ
起動した時刻とする。touch/ は誰でもファイルを作れるので、アプリは使われるたびに
自分の名前のファイルの更新時刻を進めればよい。root/ は root だけが書ける。root の
touch（使用中の判定の in-use と、ログインとログアウトのたびの PAM の pam）はこちらに
書き、ほかのユーザーが touch/ に置いたファイルに邪魔されないようにする。最終利用
時刻から制限時間（limit）が過ぎれば、systemctl poweroff で電源を切る。

制限時間は、temt の deploy のオプション limit で決めたものを /etc に置く（install が
limit --deploy で書く）。焼けば AMI に残る。idle-shutdown limit で変えたものは /run に
置くので、再起動するかインスタンスが終わるまで、deploy で決めたものより先に効く。
どちらも無ければ 1 時間とする。

判定の結果は、journal に書く 1 行と同じものを、SSM パラメータ
/temt-<svc>/idle-shutdown/status にも書く（report）。手元の temt status がこれを出す。
書くのは aws-cli で、サービスの名前とリージョンはインスタンスメタデータから得る。
書けなくても警告するだけで、判定と電源断は止めない。
"""

from __future__ import annotations

import http.client
import json
import os
import pwd
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROG = "idle-shutdown"
RUN_DIR = Path("/run/idle-shutdown")
TOUCH_DIR = RUN_DIR / "touch"
ROOT_TOUCH_DIR = RUN_DIR / "root"
LIMIT_FILE = RUN_DIR / "limit"
# deploy のオプション limit で決めた制限時間。install が limit --deploy で書く
DEPLOY_LIMIT_FILE = Path("/etc/idle-shutdown/limit")
IN_USE_DIR = Path("/etc/idle-shutdown/in-use.d")
PTS_DIR = Path("/dev/pts")
PROC_DIR = Path("/proc")
# どちらの制限時間のファイルも無いときの制限時間
DEFAULT_LIMIT = 3600
# 制限時間の上限。limit はこれより長いものを断り、制限時間のファイルにあれば無視する
MAX_LIMIT = 365 * 86400
OFF = "off"
DEFAULT = "default"
# 使用中と判定したときに更新する root/ のファイルの名前
IN_USE_NAME = "in-use"
# in-use.d の実行ファイル 1 つにかける時間の上限（秒）
IN_USE_TIMEOUT = 30
# root で実行する in-use.d のファイルの持ち主。テストから差し替える
IN_USE_OWNER = 0
# 更新時刻が先の時刻でも、時計のずれとして受け入れる幅（秒）。これを超えるものは無視する
FUTURE_SLACK = 60
# ログインを終えた sshd のセッションのプロセス名。認証を終えると「sshd: ubuntu」、
# セッションを開くと「sshd: ubuntu@pts/0」か「sshd: ubuntu@notty」になる。
# OpenSSH 9.8 からは sshd-session が名乗る。認証前の名前（[accepted]、ubuntu [net]、
# ubuntu [priv]、listener など）は、どれも空白か [ を含む
SSHD_SESSION_RE = re.compile(r"sshd(?:-session)?: [^\s\[]\S*")
# touch のファイルの名前。ドットで始まる名前と / を含む名前を除く
TOUCH_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
# in-use.d で実行するファイルの名前。run-parts と同じく、バックアップ（foo~、
# foo.dpkg-old など）を実行しない
IN_USE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
DURATION_RE = re.compile(r"([0-9]+)([smhd]?)")
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
BUSCTL = (
    "busctl",
    "call",
    "--json=short",
    "org.freedesktop.login1",
    "/org/freedesktop/login1",
    "org.freedesktop.login1.Manager",
    "ListInhibitors",
)
# 制限時間のファイルを置くディレクトリと、ファイルのモード
DIR_MODE = 0o755
FILE_MODE = 0o644
# 制限時間の出どころ。status と limit が添える
SET_FOR_THIS_BOOT = "set until reboot"
SET_AT_DEPLOY = "set at deploy"

# 判定の種類。judge が返す
IN_USE = "in use"
WAITING = "waiting"
NEVER = "never"
PAST = "past"
# 制限時間を過ぎているときの行の頭。タイマーは電源を切り、-n と limit は次のタイマーに
# 任せる
POWERING_OFF = "powering off"
AT_NEXT_CHECK = "power off at next check"

# 状態を書く SSM パラメータ /<名前>/<アプリID>/<キー> のアプリ ID とキー。キー status は
# temt の約束で、temt status が表示する
APP_ID = "idle-shutdown"
STATUS_KEY = "status"
# 状態の値の長さの上限。in-use.d の出力が長いときのため。超えれば切って ELLIPSIS を付ける
STATUS_MAX = 200
ELLIPSIS = "..."
# aws-cli を探す場所。PATH に頼らない。/snap/bin は aws-cli の snap の場所
AWS_CLI_DIRS = ("/snap/bin", "/usr/local/bin", "/usr/bin")
AWS_CLI = "aws"
# 状態の書き込み 1 回にかける時間の上限（秒）
PUT_TIMEOUT = 15
# インスタンスメタデータ（IMDSv2）。temt の起動テンプレートは、既定でタグを読めるように
# してある。install が、読めることを先に確かめる（service）
IMDS = "http://169.254.169.254/latest"
IMDS_TOKEN_TTL = "60"
IMDS_TIMEOUT = 2
# サービス名 <svc> を持つインスタンスのタグ。temt が起動テンプレートでかならず付ける
SERVICE_TAG = "tags/instance/temt:service"
REGION = "placement/region"
INSTANCE_ID = "instance-id"
# サービスの SSM パラメータの階層の名前 temt-<svc> の頭
NAME_PREFIX = "temt-"

USAGE = f"""\
Usage: {PROG} [-n]            check now; power off if idle (the timer runs this)
                              -n: report the decision, touch nothing, never power off
       {PROG} status          show the limit, the last activity and what keeps it up
       {PROG} limit           show the idle limit
       {PROG} limit DURATION  set the idle limit until the instance reboots or terminates
                              (90m, 4h, 1d, 3600; at most 365d; "off" never powers
                              off; "default" restores the limit given at deploy)
       {PROG} touch [NAME]    mark the instance as in use now, as NAME
                              (default: your user name)
"""


class Fail(Exception):
    """利用者に告げて終了する誤り。"""


class UsageError(Exception):
    """引数の誤り。使い方を出して終了する。"""


def _warn(text: str) -> None:
    print(f"{PROG}: {text}", file=sys.stderr)


def _printable(text: str) -> str:
    """外から来た文字列を、表示できる ASCII だけにする。ほかの文字は ? に置き換える。"""
    return "".join(c if " " <= c <= "~" else "?" for c in text)


def _is_root() -> bool:
    return os.geteuid() == 0


def parse_duration(text: str) -> int:
    """90m、4h、1d、3600、3600s を秒にする。0 と 365 日を超えるものは許さない。"""
    found = DURATION_RE.fullmatch(text)
    if found is None or int(found[1]) == 0:
        raise Fail(f"bad duration: {_printable(text)} (try 90m, 4h, 1d, or 3600)")
    seconds = int(found[1]) * UNITS[found[2]]
    if seconds > MAX_LIMIT:
        raise Fail(f"too long: {_printable(text)} (at most 365d; use off to never power off)")
    return seconds


def fmt_duration(seconds: int) -> str:
    """秒を 1d2h、1h30m、45s の形にする。"""
    out = ""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            out += f"{seconds // size}{unit}"
            seconds %= size
    if seconds or not out:
        out += f"{seconds}s"
    return out


def _fmt_time(when: float) -> str:
    """インスタンスのローカル時刻。タイムゾーンの名前を添えて、曖昧にしない。"""
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(when))


def _my_pid() -> int:
    return os.getpid()


def _pts_number(tty_nr: int) -> str | None:
    """/proc/<pid>/stat の tty_nr（制御端末の番号）が /dev/pts の端末なら、その番号。

    /dev/pts の端末は major 136 で、minor が番号になる。
    """
    major = (tty_nr >> 8) & 0xFFF
    minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
    return str(minor) if major == 136 else None


def _lineage() -> tuple[set[int], set[str]]:
    """自分と先祖のプロセスの ID と、それらの制御端末の /dev/pts の番号。

    ssh で実行されたときの自分の sshd と、ログインの端末を含む。sudo は新しい端末を
    作ってコマンドを動かすので、ログインの端末は先祖の制御端末からしか分からない。
    """
    pids: set[int] = set()
    ttys: set[str] = set()
    pid = _my_pid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            # stat は「pid (comm) state ppid pgrp session tty_nr ...」。comm は空白や ) を
            # 含みうるので、最後の ) の後ろを区切る
            stat = (PROC_DIR / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
            fields = stat.rsplit(")", 1)[1].split()
            tty = _pts_number(int(fields[4]))
            if tty is not None:
                ttys.add(tty)
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    return pids, ttys


def _my_terminals() -> set[str]:
    """自分の標準入出力がつながっている /dev/pts の端末の番号。"""
    found: set[str] = set()
    for fd in (0, 1, 2):
        try:
            name = os.ttyname(fd)
        except OSError:
            continue
        if name.startswith("/dev/pts/"):
            found.add(name.removeprefix("/dev/pts/"))
    return found


def _mine() -> tuple[set[int], set[str]]:
    """自分のセッションとみなすプロセスの ID と、端末の番号。"""
    pids, ttys = _lineage()
    return pids, ttys | _my_terminals()


def ssh_sessions() -> set[int]:
    """ログインを終えた sshd のセッションのプロセスの ID。"""
    found: set[int] = set()
    for entry in PROC_DIR.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        title = raw.replace(b"\0", b" ").decode("ascii", "replace").strip()
        if SSHD_SESSION_RE.fullmatch(title):
            found.add(int(entry.name))
    return found


def terminals(skip: set[str]) -> int:
    """/dev/pts にある端末の数。skip の番号の端末は数えない。"""
    return sum(1 for entry in PTS_DIR.iterdir() if entry.name.isdigit() and entry.name not in skip)


def _busctl() -> str:
    return subprocess.run(BUSCTL, capture_output=True, text=True, check=True).stdout


def inhibitors() -> list[str]:
    """shutdown を含む、モード block のシャットダウン抑止ロックを 1 つ 1 行で挙げる。

    モード delay のロックは電源断を少し遅らせるだけなので数えない。unattended-upgrades
    が常に持っているのはこちらである。一覧が取れなければ、確かめられないので使用中と
    する。
    """
    try:
        # ListInhibitors は、ロックごとに [what, who, why, mode, uid, pid] を返す。
        # what は shutdown:sleep のように : で区切った一覧
        locks = json.loads(_busctl())["data"][0]
        found: list[str] = []
        for what, who, why, mode, uid, pid in locks:
            if mode == "block" and "shutdown" in what.split(":"):
                found.append(
                    f"shutdown blocked by {_printable(who)} (uid {uid}, pid {pid}): "
                    f"{_printable(why)}"
                )
    except (OSError, subprocess.CalledProcessError, ValueError, LookupError, TypeError) as e:
        _warn(f"cannot list the inhibitor locks: {_printable(str(e))}")
        return ["the inhibitor locks are unknown"]
    return found


def _first_line(text: str) -> str:
    lines = [line for line in (_printable(x).strip() for x in text.splitlines()) if line]
    return f": {lines[0]}" if lines else ""


def in_use_d() -> list[str]:
    """in-use.d の実行ファイルを名前の順に実行し、出力を 1 行ずつ理由として返す。

    失敗したもの、時間切れのもの、root の持ち物でないものとグループや他人が書ける
    ものは、警告して、使用中とはみなさない。
    """
    try:
        entries = sorted(IN_USE_DIR.iterdir())
    except FileNotFoundError:
        return []
    except OSError as e:
        _warn(f"cannot read {IN_USE_DIR}: {_printable(str(e))}")
        return []
    found: list[str] = []
    for path in entries:
        if not IN_USE_NAME_RE.fullmatch(path.name):
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        if info.st_uid != IN_USE_OWNER or info.st_mode & 0o022:
            _warn(f"{path} must belong to root and be writable only by root; ignoring it")
            continue
        try:
            done = subprocess.run(
                [str(path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=IN_USE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _warn(f"{path} took more than {IN_USE_TIMEOUT}s; ignoring it")
            continue
        except OSError as e:
            _warn(f"cannot run {path}: {_printable(str(e))}")
            continue
        if done.returncode != 0:
            _warn(f"{path} failed (exit {done.returncode}){_first_line(done.stderr)}; ignoring it")
            continue
        for line in done.stdout.splitlines():
            line = _printable(line).strip()
            if line:
                found.append(f"{path.name}: {line}")
    return found


def reasons(*, skip_self: bool, with_in_use_d: bool = True) -> list[str]:
    """使用中である理由を挙げる。使われていなければ空。

    skip_self なら、自分を実行している sshd のセッションと、自分の端末を数えない。
    in-use.d は、with_in_use_d が真で、root で実行したときだけ見る。スクリプトは root で
    動くことを前提に書かれ、ほかのユーザーではタイマーと違う答えになりうるからである。
    """
    found: list[str] = []
    pids, ttys = _mine() if skip_self else (set[int](), set[str]())
    count = len(ssh_sessions() - pids)
    if count:
        found.append(f"{count} ssh session(s)")
    count = terminals(ttys)
    if count:
        found.append(f"{count} terminal(s)")
    found += inhibitors()
    if with_in_use_d and _is_root():
        found += in_use_d()
    return found


def _limit_in(path: Path, source: str) -> tuple[int | None, str] | None:
    """制限時間のファイル path を読む。無いか読めなければ None。off なら秒数は None。

    読めないものと、中身が秒数でも off でもないものは、警告して無いものとする。
    """
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        _warn(f"cannot read {path}: {_printable(str(e))}; ignoring it")
        return None
    if text == OFF:
        return None, source
    if text.isdigit() and 0 < int(text) <= MAX_LIMIT:
        return int(text), source
    _warn(f"bad limit in {path}: {_printable(text)}; ignoring it")
    return None


def read_limit() -> tuple[int | None, str]:
    """制限時間の秒数と、その出どころを返す。off なら秒数は None。

    再起動するまでの制限時間（/run）、deploy で決めた制限時間（/etc）、DEFAULT_LIMIT の
    順に探す。
    """
    found = _limit_in(LIMIT_FILE, SET_FOR_THIS_BOOT)
    if found is None:
        found = _limit_in(DEPLOY_LIMIT_FILE, SET_AT_DEPLOY)
    return found or (DEFAULT_LIMIT, DEFAULT)


def _boot_time() -> float:
    uptime = (PROC_DIR / "uptime").read_text(encoding="ascii").split()[0]
    return time.time() - float(uptime)


def last_active() -> tuple[float, str]:
    """最終利用時刻と、それを更新したファイルの名前を返す。無ければ起動した時刻。

    先の時刻は、いまの時刻とする。ただし touch/ のファイルが少しの幅を超えて先の
    時刻なら、いつまでも使用中になるので、警告して無視する。root/ のものは root が
    書いた時刻なので、時計が戻っても捨てない。
    """
    now = time.time()
    when, by = _boot_time(), "boot"
    for where in (ROOT_TOUCH_DIR, TOUCH_DIR):
        try:
            entries = list(where.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                mtime = entry.lstat().st_mtime
            except OSError:
                continue
            if where == TOUCH_DIR and mtime > now + FUTURE_SLACK:
                _warn(f"{entry} has a time in the future; ignoring it")
                continue
            mtime = min(mtime, now)
            if mtime > when:
                when, by = mtime, _printable(entry.name)
    return when, by


def touch(name: str) -> None:
    """<name> のファイルの更新時刻をいまにする。無ければ作る。

    root なら root/ に、ほかのユーザーなら touch/ に書く。touch/ は誰でも書けるので、
    シンボリックリンクは辿らず、FIFO を開いて止まらないようにする。
    """
    if not TOUCH_NAME_RE.fullmatch(name):
        raise Fail(
            f"bad name: {_printable(name)} (letters, digits, '.', '_' and '-', "
            "starting with a letter, a digit or '_')"
        )
    path = (ROOT_TOUCH_DIR if _is_root() else TOUCH_DIR) / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o644)
    except FileNotFoundError:
        raise Fail(f"no {path.parent}; is {PROG} installed?") from None
    except PermissionError:
        raise Fail(f"cannot touch {path}; it belongs to another user") from None
    except OSError as e:
        raise Fail(f"cannot touch {path}: {_printable(e.strerror or str(e))}") from None
    try:
        os.utime(fd)
    finally:
        os.close(fd)


def _poweroff() -> None:
    try:
        subprocess.run(("systemctl", "poweroff"), check=True)
    except subprocess.CalledProcessError as e:
        raise Fail(f"systemctl poweroff failed (exit {e.returncode})") from None


def _aws_cli() -> str | None:
    """aws-cli の実行ファイル。AWS_CLI_DIRS の順に探し、無ければ None。"""
    for directory in AWS_CLI_DIRS:
        path = Path(directory) / AWS_CLI
        if os.access(path, os.X_OK):
            return str(path)
    return None


def _fetch(request: urllib.request.Request) -> str:
    """メタデータを 1 つ読む。プロキシの環境変数には従わず、IMDS へ直接つなぐ。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=IMDS_TIMEOUT) as response:
        return response.read().decode("ascii", "replace").strip()


def _imds_token() -> str:
    """IMDSv2 のトークン。起動テンプレートがトークンを必須にしている。"""
    request = urllib.request.Request(
        f"{IMDS}/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": IMDS_TOKEN_TTL},
    )
    return _fetch(request)


def _imds(path: str, token: str) -> str:
    """インスタンスメタデータ path を読む。"""
    request = urllib.request.Request(
        f"{IMDS}/meta-data/{path}", headers={"X-aws-ec2-metadata-token": token}
    )
    return _fetch(request)


def _instance() -> tuple[str, str]:
    """このインスタンスのサービスの名前 temt-<svc>（temt:service タグから作る）と
    リージョン。
    """
    token = _imds_token()
    svc = _imds(SERVICE_TAG, token)
    region = _imds(REGION, token)
    if not svc or not region:
        raise ValueError("the tag temt:service or the region is empty")
    return f"{NAME_PREFIX}{svc}", region


def check_service() -> None:
    """インスタンスメタデータからタグ temt:service を読めることを確かめる。install 専用。

    読めなければ、状態を SSM パラメータに書けない。直し方を告げて失敗させ、install に
    何も入れさせない。設定ファイルでタグを読めなくしたサービス（取り込んだものなど）で
    起きる。
    """
    try:
        token = _imds_token()
    except (OSError, http.client.HTTPException) as e:
        raise Fail(f"cannot read the instance metadata: {_printable(str(e))}") from None
    try:
        svc = _imds(SERVICE_TAG, token)
    except (OSError, http.client.HTTPException):
        svc = ""
    if svc:
        return
    try:
        instance_id = _imds(INSTANCE_ID, token) or "<id>"
    except (OSError, http.client.HTTPException):
        instance_id = "<id>"
    raise Fail(
        "cannot read the tag temt:service from the instance metadata; write "
        'MetadataOptions.InstanceMetadataTags = "enabled" in [launch-template] of the config '
        "file and run temt apply; for this instance, run 'aws ec2 "
        f"modify-instance-metadata-options --instance-id {_printable(instance_id)} "
        "--instance-metadata-tags enabled'; then deploy again"
    )


def report(text: str) -> None:
    """判定の結果 text を、SSM パラメータ /<名前>/idle-shutdown/status に書く。

    aws-cli が無い、メタデータが取れない、書けない（権限が無い、ネットワークが無い）
    ときは警告するだけで、呼ぶ側の判定と電源断は止めない。
    """
    if len(text) > STATUS_MAX:
        text = text[: STATUS_MAX - len(ELLIPSIS)] + ELLIPSIS
    aws_cli = _aws_cli()
    if aws_cli is None:
        _warn(f"cannot report the status: no {AWS_CLI} in {', '.join(AWS_CLI_DIRS)}")
        return
    try:
        name, region = _instance()
    except (OSError, ValueError, http.client.HTTPException) as e:
        # urllib の誤りは OSError だが、壊れた応答は http.client の例外になる
        _warn(f"cannot report the status: cannot read the instance metadata: {_printable(str(e))}")
        return
    argv = (
        aws_cli,
        "ssm",
        "put-parameter",
        "--region",
        region,
        "--name",
        f"/{name}/{APP_ID}/{STATUS_KEY}",
        "--type",
        "String",
        "--overwrite",
        "--value",
        text,
    )
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PUT_TIMEOUT,
            env={**os.environ, "AWS_PAGER": ""},
        )
    except subprocess.TimeoutExpired:
        _warn(f"cannot report the status: {AWS_CLI} took more than {PUT_TIMEOUT}s")
        return
    except OSError as e:
        _warn(f"cannot report the status: cannot run {aws_cli}: {_printable(str(e))}")
        return
    if done.returncode != 0:
        _warn(
            f"cannot report the status: {AWS_CLI} failed (exit {done.returncode})"
            f"{_first_line(done.stderr)}"
        )


def judge(*, skip_self: bool, past: str) -> tuple[str, str]:
    """判定し、その種類（IN_USE、NEVER、WAITING、PAST）と、結果の 1 行を返す。

    1 行は journal と SSM パラメータに書く形で、design/apps/idle-shutdown.md の
    Reporting にある。past は、制限時間を過ぎているときの行の頭。
    """
    found = reasons(skip_self=skip_self)
    limit, _ = read_limit()
    shown = OFF if limit is None else fmt_duration(limit)
    if found:
        return IN_USE, f"in use: {'; '.join(found)} (limit {shown})"
    when, by = last_active()
    idle = int(time.time() - when)
    since = f"idle {fmt_duration(idle)} since {by}"
    if limit is None:
        return NEVER, f"never powers off (limit off; {since})"
    if idle < limit:
        remaining = fmt_duration(limit - idle)
        at = _fmt_time(when + limit)
        return WAITING, f"power off in {remaining} (at {at}; {since}, limit {shown})"
    return PAST, f"{past} ({since}, limit {shown})"


def check(*, dry_run: bool) -> None:
    """使われていれば最終利用時刻を更新し、制限時間を過ぎていれば電源を切る。

    判定の結果を標準出力（タイマーなら journal）に書き、SSM パラメータにも書く。
    -n（dry_run）は ssh で実行されることが多いので、自分のセッションを数えない。
    結果を出すだけで、何も書かず、電源も切らない。
    """
    if not dry_run and not _is_root():
        raise Fail(f"checking needs root; try {PROG} -n or {PROG} status")
    kind, text = judge(skip_self=dry_run, past=AT_NEXT_CHECK if dry_run else POWERING_OFF)
    if not _is_root():
        _warn(f"not checking in-use.d; run sudo {PROG} -n to include it")
    print(text)
    if dry_run:
        return
    if kind == IN_USE:
        touch(IN_USE_NAME)
    # 報告が思わぬ誤りで落ちても、電源断は止めない
    try:
        report(text)
    finally:
        if kind == PAST:
            _poweroff()


def status(*, login: bool = False) -> None:
    """制限時間、最終利用時刻、使用中の理由、電源を切る予定を出す。

    自分のセッションは数えない。自分のほかに使われていなければ、ログアウトしたときに
    PAM が最終利用時刻を進めるので、そこから制限時間の後に電源を切る。

    login は、ログインのメッセージを作る pam_motd から呼ばれたとき。ログインしようと
    している接続には、セッションとして数える sshd のプロセスも端末もまだ無いので、
    自分のセッションがあるものとして予定を出す。最終利用時刻は PAM が直前に更新して
    いていつもいまなので出さない。in-use.d は、接続のたびに待たせないよう見ず、見て
    いないことも告げない。警告が状態の行より先に出るよう、すべて調べてから出力する。
    """
    found = reasons(skip_self=True, with_in_use_d=not login)
    if login:
        mine = True
    else:
        pids, ttys = _mine()
        mine = bool(pids & ssh_sessions()) or bool(ttys)
    limit, source = read_limit()
    last = None if login else last_active()
    if limit is None:
        plan = "never"
    elif found:
        plan = "not while in use"
    elif mine or last is None:
        plan = f"{fmt_duration(limit)} after you log out"
    else:
        # 電源を切る時刻と、それまでの時間。過ぎていれば次のタイマーが切る
        due = last[0] + limit
        remaining = int(due - time.time())
        left = f"in {fmt_duration(remaining)}" if remaining > 0 else "at next check"
        plan = f"{_fmt_time(due)} ({left})"
    shown = OFF if limit is None else fmt_duration(limit)
    note = " (not counting this session)" if mine else ""
    print(f"limit:        {shown} ({source})")
    if last is not None:
        when, by = last
        idle = int(time.time() - when)
        print(f"last active:  {_fmt_time(when)} ({fmt_duration(idle)} ago, by {by})")
    print(f"in use:       {'; '.join(found) if found else 'no'}{note}")
    if not login and not _is_root():
        print(f"in-use.d:     not checked; run sudo {PROG} status to include it")
    print(f"power off:    {plan}")


def _write_limit(path: Path, text: str) -> None:
    """制限時間の期間 text を、秒数か off にしてファイル path に書く。

    ディレクトリが無ければモード 755 で作り、ファイルはモード 644 にする。umask に任せない。
    """
    value = OFF if text == OFF else str(parse_duration(text))
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True)
        path.parent.chmod(DIR_MODE)
    # 書きかけを読まれないよう、一時ファイルに書いてから入れ替える
    temp = path.with_name(f".{path.name}.{os.getpid()}")
    temp.write_text(f"{value}\n", encoding="ascii")
    temp.chmod(FILE_MODE)
    temp.replace(path)


def _need_root(what: str, command: str) -> None:
    """root でなければ、what に root が要ることと、sudo を付けた command を告げて止める。"""
    if not _is_root():
        raise Fail(f"{what} needs root: sudo {PROG} {command}")


def send_status() -> None:
    """判定の結果を SSM パラメータに書く。

    判定は -n と同じで、root/in-use は更新せず、電源も切らない。自分のセッションは
    数えるので、ssh から実行していれば in use になる。root で実行するので in-use.d も見る。
    """
    _, line = judge(skip_self=False, past=AT_NEXT_CHECK)
    report(line)


def set_limit(text: str) -> None:
    """再起動するまでの制限時間を変え、変えたあとの判定の結果を SSM パラメータに書く。

    default は、再起動するまでの制限時間を消して、deploy で決めたものに戻す。
    """
    _need_root("setting the limit", f"limit {_printable(text)}")
    if text == DEFAULT:
        LIMIT_FILE.unlink(missing_ok=True)
    else:
        _write_limit(LIMIT_FILE, text)
    show_limit()
    send_status()


def set_deploy_limit(text: str) -> None:
    """deploy で決めた制限時間を変える。install が、deploy のオプション limit の値で実行する。

    期間を確かめて書くだけで、判定はしない。判定の結果は、install が入れ終えたあとに
    report で書く。再起動するまでの制限時間が残っていれば、与えた制限時間が効いていない
    ように見えるので、その理由を添える。
    """
    _need_root("setting the limit", f"limit --deploy {_printable(text)}")
    _write_limit(DEPLOY_LIMIT_FILE, text)
    show_limit()
    if LIMIT_FILE.exists():
        print("the limit set until reboot still applies;")
        print(f"run sudo {PROG} limit default to use the limit given at deploy")


def show_limit() -> None:
    limit, source = read_limit()
    shown = OFF if limit is None else fmt_duration(limit)
    print(f"limit: {shown} ({source})")


def run(args: list[str]) -> None:
    match args:
        case []:
            check(dry_run=False)
        case ["-n"]:
            check(dry_run=True)
        case ["status"]:
            status()
        # ログインのメッセージ専用。USAGE には出さない
        case ["status", "--login"]:
            status(login=True)
        case ["limit"]:
            show_limit()
        # limit --deploy、report、service は install 専用。USAGE には出さない
        case ["service"]:
            check_service()
        case ["limit", "--deploy", text]:
            set_deploy_limit(text)
        case ["report"]:
            _need_root("reporting", "report")
            send_status()
        case ["limit", text]:
            set_limit(text)
        case ["touch"]:
            touch(pwd.getpwuid(os.getuid()).pw_name)
        case ["touch", name]:
            touch(name)
        case ["-h" | "--help"]:
            print(USAGE, end="")
        case _:
            raise UsageError


def main(args: list[str]) -> int:
    try:
        run(args)
    except UsageError:
        print(USAGE, end="", file=sys.stderr)
        return 2
    except Fail as e:
        _warn(str(e))
        return 1
    except OSError as e:
        _warn(_printable(str(e)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
