"""コマンドラインの入口。temt <対象> <動詞> [引数] [コマンドラインオプション] を受け取る。"""

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from importlib import metadata

from temt import conf, msg, options
from temt.conf import RULE_PORT
from temt.msg import TemtError, UsageError
from temt.names import (
    DEPLOY_KEY,
    INSTANCE_ID_RULE,
    KEY_NAME_RULE,
    LABEL_RULE,
    PREFIX,
    USER_NAME_RULE,
    Service,
    app_dir,
    is_instance_id,
    is_key_name,
    is_label,
    is_user_name,
)
from temt.table import aws_error, printable

# コマンドは <対象> <動詞> の 2 語で表す
SERVICE_APPLY = "service apply"
SERVICE_DIFF = "service diff"
SERVICE_DESTROY = "service destroy"
SERVICE_LIST = "service list"
INSTANCE_START = "instance start"
INSTANCE_STOP = "instance stop"
INSTANCE_TERMINATE = "instance terminate"
INSTANCE_REBOOT = "instance reboot"
INSTANCE_SHOW = "instance show"
INSTANCE_LIST = "instance list"
AMI_BAKE = "ami bake"
AMI_LIST = "ami list"
AMI_PRUNE = "ami prune"
AMI_DELETE = "ami delete"
TEMPLATE_SHOW = "template show"
SG_LIST = "sg list"
SG_ADD = "sg add"
SG_REMOVE = "sg remove"
SG_DIFF = "sg diff"
SG_SHOW = "sg show"
KEYPAIR_LIST = "keypair list"
KEYPAIR_SHOW = "keypair show"
KEYPAIR_CREATE = "keypair create"
KEYPAIR_DELETE = "keypair delete"
APP_LIST = "app list"
APP_SHOW = "app show"
APP_GET = "app get"
APP_SET = "app set"
APP_UNSET = "app unset"
APP_DEPLOY = "app deploy"
APP_REMOVE = "app remove"
SSH_PROXY = "ssh proxy"
COMMANDS = (
    SERVICE_APPLY,
    SERVICE_DIFF,
    SERVICE_DESTROY,
    SERVICE_LIST,
    INSTANCE_START,
    INSTANCE_STOP,
    INSTANCE_TERMINATE,
    INSTANCE_REBOOT,
    INSTANCE_SHOW,
    INSTANCE_LIST,
    AMI_BAKE,
    AMI_LIST,
    AMI_PRUNE,
    AMI_DELETE,
    TEMPLATE_SHOW,
    SG_LIST,
    SG_ADD,
    SG_REMOVE,
    SG_DIFF,
    SG_SHOW,
    KEYPAIR_LIST,
    KEYPAIR_SHOW,
    KEYPAIR_CREATE,
    KEYPAIR_DELETE,
    APP_LIST,
    APP_SHOW,
    APP_GET,
    APP_SET,
    APP_UNSET,
    APP_DEPLOY,
    APP_REMOVE,
    SSH_PROXY,
)
# 対象の名前。コマンドの 1 語め
TARGETS = tuple(dict.fromkeys(command.split()[0] for command in COMMANDS))
# 短縮形と、それが表すコマンド
SHORT_FORMS = {
    "apply": SERVICE_APPLY,
    "diff": SERVICE_DIFF,
    "destroy": SERVICE_DESTROY,
    "start": INSTANCE_START,
    "stop": INSTANCE_STOP,
    "terminate": INSTANCE_TERMINATE,
    "reboot": INSTANCE_REBOOT,
    "status": INSTANCE_SHOW,
    "bake": AMI_BAKE,
    "deploy": APP_DEPLOY,
    "proxy": SSH_PROXY,
}
# 完了を待たない --async を取るコマンド。bake と destroy はつねに完了を待つ
ASYNC_COMMANDS = (INSTANCE_START, INSTANCE_STOP, INSTANCE_TERMINATE, INSTANCE_REBOOT)
# 確認を省く --yes を取るコマンド
YES_COMMANDS = (
    SERVICE_APPLY,
    INSTANCE_STOP,
    INSTANCE_TERMINATE,
    INSTANCE_REBOOT,
    AMI_BAKE,
    SERVICE_DESTROY,
    AMI_PRUNE,
    AMI_DELETE,
    KEYPAIR_DELETE,
    APP_DEPLOY,
    APP_REMOVE,
)
# 終わったあとに AMI にする --bake を取るコマンド
BAKE_COMMANDS = (APP_DEPLOY, APP_REMOVE)
# 指されているか、指されているか分からないか、pending でも消す --force を取るコマンド
FORCE_COMMANDS = (AMI_DELETE,)
# アプリ ID（app deploy はパスも）を 1 つ取り、--user を取るコマンド
TARGET_COMMANDS = BAKE_COMMANDS
# アプリの後ろに <名前>=<値> のオプションを取り、前の記録のオプションを引き継がない
# --reset を取るコマンド
APP_OPTION_COMMANDS = (APP_DEPLOY,)
# 接続先のポートを取るコマンド
PORT_COMMANDS = (SSH_PROXY,)
# --svc を取らないコマンド
NO_SERVICE_COMMANDS = (KEYPAIR_LIST, KEYPAIR_SHOW, KEYPAIR_CREATE, KEYPAIR_DELETE)
# --svc を省けるコマンド。付ければ、そのサービスのものだけを扱う。ほかのコマンドは
# --svc が要る
OPTIONAL_SERVICE_COMMANDS = (SERVICE_LIST, INSTANCE_LIST, AMI_LIST, AMI_PRUNE, AMI_DELETE, APP_LIST)
# AWS を呼ばないコマンド。--profile も --region も取らない
LOCAL_COMMANDS = (SERVICE_LIST, SG_LIST, SG_ADD, SG_REMOVE)
# --svc を省いたときだけ AWS を呼ばないコマンド。省けば --profile も --region も取らない
LOCAL_WITHOUT_SERVICE_COMMANDS = (APP_LIST,)
# 受信規則の <ポート> と <CIDR> を取るコマンド
RULE_COMMANDS = (SG_ADD, SG_REMOVE)
# 規則の説明を与える --description を取るコマンド
DESCRIPTION_COMMANDS = (SG_ADD,)
# 動詞の後ろにキーペア名を取るコマンド。keypair show はファイルも取る
KEY_COMMANDS = (KEYPAIR_SHOW, KEYPAIR_CREATE, KEYPAIR_DELETE)
# 動詞の後ろに AMI の ID か名前を取るコマンド
IMAGE_COMMANDS = (AMI_DELETE,)
# 動詞の後ろにインスタンス ID を 0 個以上取るコマンド
INSTANCE_ID_COMMANDS = (INSTANCE_TERMINATE,)
# 取ったキーペア名を手元の鍵のファイル名にするコマンド。名前を names の規則で確かめる
KEY_NAME_COMMANDS = (KEYPAIR_CREATE, KEYPAIR_DELETE)
# 表を出し、出す列を選ぶ --format を取るコマンド
LIST_COMMANDS = (SERVICE_LIST, INSTANCE_LIST, AMI_LIST, KEYPAIR_LIST, APP_LIST, SG_LIST)
# 動詞の後ろにアプリ ID、キー、値を取るコマンドと、取る引数の名前。並びは引数の順。
# app set はキーと値を <キー>=<値> の 1 語で取る
KEY_VALUE = "<key>=<value>"
APP_ARGS = {
    APP_SHOW: ("app id",),
    APP_GET: ("app id", "key"),
    APP_SET: ("app id", KEY_VALUE),
    APP_UNSET: ("app id", "key"),
}
# キー deploy を扱えないコマンド。deploy の記録は temt が書く
RECORD_KEEPING = (APP_SET, APP_UNSET)
SVC_OPTION = "--svc"
# 使うプロファイルを選ぶコマンドラインオプション。AWS を呼ぶコマンドはどれも取る
PROFILE_OPTION = "--profile"
# 使うリージョンを選ぶコマンドラインオプション。--svc の無いときだけ使える
REGION_OPTION = "--region"
FORMAT_OPTION = "--format"
# install と uninstall を実行するインスタンスのユーザーを選ぶコマンドラインオプション
USER_OPTION = "--user"
DESCRIPTION_OPTION = "--description"
ASYNC_OPTION = "--async"
YES_OPTION = "--yes"
BAKE_OPTION = "--bake"
FORCE_OPTION = "--force"
RESET_OPTION = "--reset"
# 使い方を出して終わるコマンドラインオプション。ほかの引数は読まない。対象より後ろなら、
# その対象の使い方を出す
HELP_OPTION = "--help"
# temt のバージョンを出して終わるコマンドラインオプション。ほかの引数は読まない
VERSION_OPTION = "--version"
# 先に見つけたほうで終わるコマンドラインオプション
EARLY_OPTIONS = (HELP_OPTION, VERSION_OPTION)
# 直後の語を値とするコマンドラインオプション
VALUE_OPTIONS = (
    SVC_OPTION,
    PROFILE_OPTION,
    REGION_OPTION,
    FORMAT_OPTION,
    USER_OPTION,
    DESCRIPTION_OPTION,
)
# 値を取らないコマンドラインオプション
SWITCH_OPTIONS = (ASYNC_OPTION, YES_OPTION, BAKE_OPTION, FORCE_OPTION, RESET_OPTION)
# 値を空にできるコマンドラインオプション。空の --description は説明を消す
EMPTY_OK_OPTIONS = (DESCRIPTION_OPTION,)
# 一部のコマンドだけが取るコマンドラインオプションと、それを取るコマンド。--svc、--profile、
# --region は、parse がサービスと AWS を呼ぶかどうかから確かめる
COMMAND_OPTIONS = {
    FORMAT_OPTION: LIST_COMMANDS,
    USER_OPTION: TARGET_COMMANDS,
    DESCRIPTION_OPTION: DESCRIPTION_COMMANDS,
    ASYNC_OPTION: ASYNC_COMMANDS,
    YES_OPTION: YES_COMMANDS,
    BAKE_OPTION: BAKE_COMMANDS,
    FORCE_OPTION: FORCE_COMMANDS,
    RESET_OPTION: APP_OPTION_COMMANDS,
}
# 使い方に書く、動詞より後ろの引数と、そのコマンドだけが取るコマンドラインオプション。
# --svc は区分から足す
SYNOPSES = {
    SERVICE_APPLY: "[--yes]",
    SERVICE_DIFF: "",
    SERVICE_DESTROY: "[--yes]",
    SERVICE_LIST: "[--format <columns>]",
    INSTANCE_START: "[--async]",
    INSTANCE_STOP: "[--async] [--yes]",
    INSTANCE_TERMINATE: "[<id>...] [<options>]",
    INSTANCE_REBOOT: "[--async] [--yes]",
    INSTANCE_SHOW: "",
    INSTANCE_LIST: "[--format <columns>]",
    AMI_BAKE: "[--yes]",
    AMI_LIST: "[--format <columns>]",
    AMI_PRUNE: "[--yes]",
    AMI_DELETE: "<ami id or name> [--force] [--yes]",
    TEMPLATE_SHOW: "",
    SG_LIST: "[--format <columns>]",
    SG_ADD: "<port> [<cidr>] [--description <text>]",
    SG_REMOVE: "<port> [<cidr>]",
    SG_DIFF: "",
    SG_SHOW: "",
    KEYPAIR_LIST: "[--format <columns>]",
    KEYPAIR_SHOW: "<name or file>",
    KEYPAIR_CREATE: "<name>",
    KEYPAIR_DELETE: "<name> [--yes]",
    APP_LIST: "[--format <columns>]",
    APP_SHOW: "<app id>",
    APP_GET: "<app id> <key>",
    APP_SET: f"<app id> {KEY_VALUE}",
    APP_UNSET: "<app id> <key>",
    APP_DEPLOY: "<app> [<name>=<value>...] [<options>]",
    APP_REMOVE: "<app id> [<options>]",
    SSH_PROXY: "[port]",
}
# 使い方で、短縮形の代わりにコマンドの行へ添える書き添え
REMARKS = {SERVICE_LIST: "(no --profile/--region)", KEYPAIR_SHOW: "(a file if it has a '/')"}
# 使い方の末尾に書く注意と、それに関わるコマンド。対象の使い方には、その対象のコマンド
# に関わる注意だけを書く
NOTES = (
    (
        tuple(c for c in OPTIONAL_SERVICE_COMMANDS if c not in LOCAL_WITHOUT_SERVICE_COMMANDS),
        "[--svc <svc>] may be left out; with it, the command deals only with the\nservice.",
    ),
    (LIST_COMMANDS, "<columns> is a comma-separated list of column names, such as ID,STATE."),
    (
        (SG_LIST, *RULE_COMMANDS),
        """\
<port> of sg add and sg remove is a port or a range with an optional /tcp or
/udp, such as 22, 8000-8010 or 53/udp, or icmp. Without <cidr>, they use the
egress IP address of this machine. sg list reads, and sg add and sg remove edit,
only the config file, and take no --profile or --region; run apply to update the
security group.""",
    ),
    (
        INSTANCE_ID_COMMANDS,
        """\
<id> of instance terminate is an instance id, such as i-0123456789abcdef0;
without any, it terminates every instance of the service. Its <options> are
[--async] [--yes].""",
    ),
    (
        (AMI_DELETE,),
        """\
ami delete refuses an AMI that a launch template uses or that is still pending,
and any AMI while a launch template version cannot be resolved, unless --force
is given.""",
    ),
    (
        LOCAL_WITHOUT_SERVICE_COMMANDS,
        """\
[--svc <svc>] of app list may be left out; without it, app list lists only the
apps that app deploy finds by id (in the apps directory beside the config files,
and those that come with temt), and takes no --profile or --region.""",
    ),
    ((APP_SET,), "<value> of app set may contain '=' and start with '-'."),
    (
        TARGET_COMMANDS,
        """\
<app> of app deploy is an app id, or a path if it has a '/'. An app id names a
directory in the apps directory beside the config files, or else an app that
comes with temt.
<name>=<value> of app deploy sets an app option, which the app declares in its
manifest.toml; <name>= sets it back to the default. app deploy keeps the app
options of the last deploy unless --reset is given; app show lists them.
<options> of app deploy and app remove are [--user <user>] [--bake] [--yes], and
[--reset] for app deploy. --user runs install or uninstall as that user on the
instance; app deploy defaults to user in manifest.toml, app remove to the user
that ran install, and both to root.""",
    ),
)
# 使い方の、コマンドラインオプションの説明の行
HELP_LINE = "  --help              print this message; after <target>, the help for it"
VERSION_LINE = "  --version           print the version of temt"
SVC_LINE = "  --svc <svc>         the service; its config file gives the region and profile"
PROFILE_LINE = "  --profile <name>    the AWS profile, over AwsProfile in the config file"
REGION_LINE = "  --region <region>   the AWS region; not with --svc"


def _synopsis(command: str) -> str:
    """コマンドの行の左側を作る。--svc は、区分に合わせて動詞の直後に書く。"""
    words = [command]
    if command not in NO_SERVICE_COMMANDS:
        svc = f"{SVC_OPTION} <svc>"
        words.append(f"[{svc}]" if command in OPTIONAL_SERVICE_COMMANDS else svc)
    if SYNOPSES[command]:
        words.append(SYNOPSES[command])
    return "  " + " ".join(words)


def _table(groups: list[list[str]]) -> list[str]:
    """コマンドの行を、組ごとに空行で区切って並べる。短縮形か書き添えは、すべての行で
    桁を揃えて右に書く。短縮形があれば、見出しの右に列の見出しを書く。
    """
    short = {command: form for form, command in SHORT_FORMS.items()}
    tables = [
        [(_synopsis(c), short.get(c) or REMARKS.get(c, "")) for c in group] for group in groups
    ]
    width = max((len(left) for rows in tables for left, right in rows if right), default=0) + 2
    has_short = any(c in short for group in groups for c in group)
    lines = [f"{'commands:':<{width}} short form:" if has_short else "commands:"]
    for i, rows in enumerate(tables):
        if i:
            lines.append("")
        lines += [f"{left:<{width}} {right}".rstrip() for left, right in rows]
    return lines


def _notes(commands: list[str]) -> list[str]:
    """そのコマンドに関わる注意を並べる。"""
    return [note for related, note in NOTES if any(c in related for c in commands)]


def usage(target: str | None = None) -> str:
    """使い方を作る。target を与えれば、その対象のコマンドだけを書く。"""
    if target is None:
        lines = [
            "usage: temt <target> <verb> [args] [options]",
            "       temt <short form> [args] [options]",
            "       temt-<svc> <target> <verb> [args] [options]   (temt ... --svc <svc>)",
            "       temt-<svc> <short form> [args] [options]      (temt ... --svc <svc>)",
            "",
            "options may come anywhere after the program name:",
            HELP_LINE,
            VERSION_LINE,
            SVC_LINE,
            PROFILE_LINE,
            REGION_LINE,
            "",
            *_table([[c for c in COMMANDS if c.split()[0] == t] for t in TARGETS]),
            "",
            *_notes(list(COMMANDS)),
        ]
        return "\n".join(lines) + "\n"
    commands = [c for c in COMMANDS if c.split()[0] == target]
    with_service = any(c not in NO_SERVICE_COMMANDS for c in commands)
    lines = [f"usage: temt {target} <verb> [args] [options]"]
    if with_service:
        lines.append(f"       temt-<svc> {target} <verb> [args] [options]   (temt ... --svc <svc>)")
    lines += ["", "options may come anywhere after the program name:", HELP_LINE, VERSION_LINE]
    if with_service:
        lines += [SVC_LINE, PROFILE_LINE, REGION_LINE]
    else:
        lines += ["  --profile <name>    the AWS profile", "  --region <region>   the AWS region"]
    lines += ["", *_table([commands])]
    notes = _notes(commands)
    if notes:
        lines += ["", *notes]
    return "\n".join(lines) + "\n"


USAGE = usage()
DEFAULT_PORT = 22
PORT_MAX = 65535
PORT_RE = re.compile(r"[0-9]{1,5}")


@dataclass(frozen=True)
class Request:
    """実行する 1 つのコマンド。"""

    # COMMANDS のどれか。短縮形で呼ばれても、それが表すコマンドになる
    command: str
    # --svc で選ばれたサービス。省かれていれば None
    service: Service | None
    # --profile で選ばれたプロファイル。省かれていれば None
    profile: str | None = None
    # --region で選ばれたリージョン。サービスの無いときだけ取る
    region: str | None = None
    # keypair の show、create、delete が取るキーペア名。show ではファイルのこともある
    key: str | None = None
    # ami delete が取る AMI の ID か名前
    image: str | None = None
    # INSTANCE_ID_COMMANDS が取るインスタンス ID。並びは打たれた順。省かれていれば空
    instance_ids: tuple[str, ...] = ()
    # --format の値。LIST_COMMANDS だけが取り、列の名前は各コマンドが確かめる
    format: str | None = None
    # APP_ARGS のコマンドが取るアプリ ID、キー、値。app は、TARGET_COMMANDS では
    # 取ったアプリ ID で、app deploy ではパスのこともある
    app: str | None = None
    app_key: str | None = None
    value: str | None = None
    is_async: bool = False
    yes: bool = False
    bake: bool = False
    force: bool = False
    # --user の値。TARGET_COMMANDS だけが取る。省かれていれば None
    user: str | None = None
    # APP_OPTION_COMMANDS が取るオプションの名前と値。並びは打たれた順。値が None なら、
    # <名前>= で既定に戻す
    options: tuple[tuple[str, str | None], ...] = ()
    # --reset。APP_OPTION_COMMANDS だけが取る
    reset: bool = False
    port: int = DEFAULT_PORT
    # RULE_COMMANDS が取る受信規則。プロトコルとポートの範囲（icmp では None）、正規化した
    # CIDR。CIDR を省けば None で、グローバル IP アドレスから作る
    protocol: str | None = None
    rule_port: RULE_PORT = None
    cidr: str | None = None
    # sg add の --description の値。省かれていれば None で、空なら説明を消す
    description: str | None = None


def _words(args: list[str]) -> Iterator[str]:
    """コマンドラインオプションを除いた語と、--help と --version を、並んだ順に返す。
    値を取るコマンドラインオプションの直後の語は、値として読み飛ばす。
    """
    rest = iter(args)
    for arg in rest:
        if arg in VALUE_OPTIONS:
            _ = next(rest, None)
        elif arg in EARLY_OPTIONS or not arg.startswith("-"):
            yield arg


def _usage_for(first: str | None) -> str:
    """コマンドラインオプションを除いた 1 語め first から決まる使い方。対象の名前か
    短縮形なら、その対象の使い方とし、そうでなければすべてのコマンドの使い方とする。
    """
    if first in SHORT_FORMS:
        return usage(SHORT_FORMS[first].split()[0])
    return usage(first) if first in TARGETS else USAGE


def version() -> str:
    """--version で出す行。番号は、入れた temt の pyproject.toml の version である。"""
    return f"{PREFIX} {metadata.version('temt')}\n"


def _early(args: list[str]) -> str | None:
    """--help か --version があれば、先に見つけたほうで出す文を返す。無ければ None。

    --help が対象か短縮形より後ろにあれば、その対象の使い方とする。
    """
    first: str | None = None
    for word in _words(args):
        if word == VERSION_OPTION:
            return version()
        if word == HELP_OPTION:
            return _usage_for(first)
        if first is None:
            first = word
    return None


def _split(args: list[str]) -> tuple[dict[str, str], list[str], str | None]:
    """引数をコマンドラインオプションと位置引数に分ける。コマンドラインオプションは
    どこに置いてもよく、それぞれ 1 回まで受け付ける。

    値を取るコマンドラインオプションは、直後の語を値とする。それ以外で - で始まる語は
    コマンドラインオプションとして読み、知らないものは誤りとする。返すのは、
    コマンドラインオプションごとの値（値を取らないものは空）、位置引数の並び、最初に
    見つけた誤りである。誤りは、parse がコマンドの名前を添えて知らせられるよう、ここでは
    上げずに読み進める。
    """
    cli_options: dict[str, str] = {}
    words: list[str] = []
    problems: list[str] = []
    rest = iter(args)
    for arg in rest:
        if not arg.startswith("-"):
            words.append(arg)
        elif arg not in VALUE_OPTIONS and arg not in SWITCH_OPTIONS:
            problems.append(f"unknown option: {printable(arg)}")
        elif arg in cli_options:
            problems.append(f"repeated argument: {arg}")
            if arg in VALUE_OPTIONS:
                _ = next(rest, None)
        elif arg in SWITCH_OPTIONS:
            cli_options[arg] = ""
        else:
            value = next(rest, None)
            if value is None or (not value and arg not in EMPTY_OK_OPTIONS):
                problems.append(f"missing value for {arg}")
            else:
                cli_options[arg] = value
    return cli_options, words, next(iter(problems), None)


def _command(words: list[str]) -> tuple[str, str, list[str]]:
    """先頭のコマンドを読み、コマンド、打たれたとおりの名前、残りの位置引数を返す。

    打たれたとおりの名前は、引数の誤りを知らせるときに使う。
    """
    if not words:
        raise UsageError("missing command")
    word = words[0]
    if word in SHORT_FORMS:
        return SHORT_FORMS[word], word, words[1:]
    if word not in TARGETS:
        raise UsageError(f"unknown command: {printable(word)}")
    if len(words) < 2 or not words[1]:
        raise UsageError(f"{word}: missing verb")
    command = f"{word} {words[1]}"
    if command not in COMMANDS:
        raise UsageError(f"{word}: unknown verb: {printable(words[1])}")
    return command, command, words[2:]


def _service(command: str, typed: str, svc: str | None) -> Service | None:
    """--svc の値から、サービスを決める。取らないか省いたときは None。"""
    if svc is None:
        if command in NO_SERVICE_COMMANDS or command in OPTIONAL_SERVICE_COMMANDS:
            return None
        raise UsageError(f"{typed}: missing {SVC_OPTION}")
    if command in NO_SERVICE_COMMANDS:
        raise UsageError(f"{typed}: {SVC_OPTION} is not allowed; key pairs belong to no service")
    return Service(svc)


def _is_local(command: str, service: Service | None) -> bool:
    """AWS を呼ばない実行か。--svc を省いたかどうかで変わるコマンドもある。"""
    return command in LOCAL_COMMANDS or (
        service is None and command in LOCAL_WITHOUT_SERVICE_COMMANDS
    )


def _check_options(
    command: str, typed: str, cli_options: dict[str, str], service: Service | None
) -> None:
    """そのコマンドが取らないコマンドラインオプションを誤りとする。"""
    if REGION_OPTION in cli_options and service is not None:
        raise UsageError(f"{typed}: {REGION_OPTION} is not allowed with a service")
    if _is_local(command, service):
        why = "it does not call AWS"
        if command in LOCAL_WITHOUT_SERVICE_COMMANDS:
            why += f" without {SVC_OPTION}"
        for option in (PROFILE_OPTION, REGION_OPTION):
            if option in cli_options:
                raise UsageError(f"{typed}: {option} is not allowed; {why}")
    for option in cli_options:
        if option in COMMAND_OPTIONS and command not in COMMAND_OPTIONS[option]:
            raise UsageError(f"{typed}: unexpected argument: {option}")


def _one(typed: str, words: list[str], what: str) -> str:
    """位置引数をちょうど 1 つ読む。空の語は無いものとする。"""
    if not words or not words[0]:
        raise UsageError(f"{typed}: missing {what}")
    if len(words) > 1:
        raise UsageError(f"{typed}: unexpected argument: {printable(words[1])}")
    return words[0]


def _port(typed: str, words: list[str]) -> int:
    """接続先のポートを返す。省かれていれば 22 とする。"""
    if not words:
        return DEFAULT_PORT
    if len(words) > 1:
        raise UsageError(f"{typed}: unexpected argument: {printable(words[1])}")
    if not PORT_RE.fullmatch(words[0]) or not 1 <= int(words[0]) <= PORT_MAX:
        raise UsageError(
            f"{typed}: port must be a number from 1 to {PORT_MAX}: {printable(words[0])}"
        )
    return int(words[0])


def _rule(
    typed: str, words: list[str], description: str | None
) -> tuple[str, RULE_PORT, str | None]:
    """sg add と sg remove の <ポート> と <CIDR> を読み、sg add の --description を確かめる。

    プロトコル、ポートの範囲、正規化した CIDR（省かれていれば None）を返す。
    """
    if not words or not words[0]:
        raise UsageError(f"{typed}: missing port")
    if len(words) > 2:
        raise UsageError(f"{typed}: unexpected argument: {printable(words[2])}")
    try:
        protocol, port = conf.parse_port(words[0])
    except ValueError as e:
        raise UsageError(f"{typed}: the port {e}: {printable(words[0])}") from None
    cidr = None
    if len(words) > 1:
        try:
            cidr = conf.cidr(words[1])
        except ValueError as e:
            raise UsageError(f"{typed}: {e}") from None
    if description is not None:
        try:
            conf.check_description(description)
        except ValueError as e:
            raise UsageError(f"{typed}: {e}") from None
    return protocol, port, cidr


def _instance_ids(typed: str, words: list[str]) -> tuple[str, ...]:
    """instance terminate のインスタンス ID を読む。省いてもよい。"""
    for i, word in enumerate(words):
        if not is_instance_id(word):
            raise UsageError(f"{typed}: the instance id {INSTANCE_ID_RULE}: {printable(word)}")
        if word in words[:i]:
            raise UsageError(f"{typed}: repeated instance id: {word}")
    return tuple(words)


def _key(command: str, typed: str, words: list[str]) -> str:
    """キーペア名（keypair show ではファイルも）を読む。"""
    what = "key pair name or file" if command == KEYPAIR_SHOW else "key pair name"
    key = _one(typed, words, what)
    if command in KEY_NAME_COMMANDS and not is_key_name(key):
        raise UsageError(f"{typed}: the key pair name {KEY_NAME_RULE}: {printable(key)}")
    return key


def _app(command: str, typed: str, words: list[str]) -> tuple[str, str | None, str | None]:
    """アプリ ID、キー、値を読む。APP_ARGS の数だけ取り、それより多ければ誤りとする。

    app set は <キー>=<値> を最初の = で分ける。キーの規則に = は無いので、値は = を
    含んでもよい。
    """
    wanted = APP_ARGS[command]
    if len(words) < len(wanted):
        raise UsageError(f"{typed}: missing {wanted[len(words)]}")
    if len(words) > len(wanted):
        raise UsageError(f"{typed}: unexpected argument: {printable(words[len(wanted)])}")
    app = words[0]
    if not is_label(app):
        raise UsageError(f"{typed}: the app id {LABEL_RULE}: {printable(app)}")
    if len(words) < 2:
        return app, None, None
    key, value = words[1], None
    if command == APP_SET:
        key, sep, value = words[1].partition("=")
        if not sep:
            raise UsageError(f"{typed}: give the value as {KEY_VALUE}: {printable(words[1])}")
    if not is_label(key):
        raise UsageError(f"{typed}: the key {LABEL_RULE}: {printable(key)}")
    if command in RECORD_KEEPING and key == DEPLOY_KEY:
        raise UsageError(f"{typed}: the key {DEPLOY_KEY} holds the deploy record of temt")
    if value is not None:
        if not value:
            # SSM は空の値を受け付けない
            raise UsageError(f"{typed}: the value must not be empty")
        if not options.is_utf8(value):
            # そのまま書くと、app get が出せない値になる
            raise UsageError(f"{typed}: the value is not valid UTF-8")
    return app, key, value


def _target(command: str, typed: str, words: list[str]) -> str:
    """app deploy と app remove が取るアプリ ID を読む。app deploy は / を含めばパスとし、
    最後の要素をアプリ ID として規則で確かめる。

    app deploy はアプリの後ろにオプションを取るので、1 語めだけを読む。
    """
    if command in APP_OPTION_COMMANDS:
        target = _one(typed, words[:1], "app id or path")
        if "/" not in target and "=" in target:
            raise UsageError(
                f"{typed}: give the app id or path before the app options: {printable(target)}"
            )
    else:
        target = _one(typed, words, "app id")
    if command == APP_DEPLOY and "/" in target:
        name = app_dir(target).name
        if not is_label(name):
            raise UsageError(
                f"{typed}: the app id {LABEL_RULE}: {printable(name)} "
                f"(the last part of {printable(target)})"
            )
    elif not is_label(target):
        raise UsageError(f"{typed}: the app id {LABEL_RULE}: {printable(target)}")
    return target


def _app_options(typed: str, words: list[str]) -> tuple[tuple[str, str | None], ...]:
    """app deploy のアプリの後ろの <名前>=<値> を読む。

    最初の = で分ける。名前の規則に = は無いので、値は = を含んでもよい。値は加工せず、
    - で始まる値もそのまま受け取る。空の値は、その名前を既定に戻す指定で None とする。
    名前が宣言されているかと、値が pattern に合うかは、deploy が manifest.toml と照らす。
    """
    found: dict[str, str | None] = {}
    for word in words:
        name, sep, value = word.partition("=")
        if not sep:
            raise UsageError(f"{typed}: give the app option as <name>=<value>: {printable(word)}")
        if not is_label(name):
            raise UsageError(f"{typed}: the app option name {LABEL_RULE}: {printable(name)}")
        if name in found:
            raise UsageError(f"{typed}: repeated app option: {name}")
        if not options.is_utf8(value):
            # UTF-8 でないバイト列は、Python が対にならないサロゲートとして読む
            raise UsageError(f"{typed}: the value of {name} is not valid UTF-8")
        if options.has_control(value):
            raise UsageError(f"{typed}: the value of {name} {options.VALUE_RULE}")
        found[name] = value or None
    return tuple(found.items())


def _user(typed: str, user: str | None) -> str | None:
    """--user の値をユーザー名の規則で確かめる。"""
    if user is not None and not is_user_name(user):
        raise UsageError(f"{typed}: the user {USER_NAME_RULE}: {printable(user)}")
    return user


def parse(args: list[str]) -> Request:
    """引数から、実行する要求を作る。"""
    cli_options, words, problem = _split(args)
    try:
        command, typed, words = _command(words)
    except UsageError:
        # コマンドラインオプションの誤りは、コマンドが読めなくても知らせる。
        # コマンドの誤りより先に起きている
        if problem is not None:
            raise UsageError(problem) from None
        raise
    if problem is not None:
        raise UsageError(f"{typed}: {problem}")
    service = _service(command, typed, cli_options.get(SVC_OPTION))
    _check_options(command, typed, cli_options, service)
    request = Request(
        command,
        service,
        profile=cli_options.get(PROFILE_OPTION),
        region=cli_options.get(REGION_OPTION),
        format=cli_options.get(FORMAT_OPTION),
        is_async=ASYNC_OPTION in cli_options,
        yes=YES_OPTION in cli_options,
        bake=BAKE_OPTION in cli_options,
        force=FORCE_OPTION in cli_options,
        user=_user(typed, cli_options.get(USER_OPTION)),
        reset=RESET_OPTION in cli_options,
    )
    if command in KEY_COMMANDS:
        return replace(request, key=_key(command, typed, words))
    if command in IMAGE_COMMANDS:
        return replace(request, image=_one(typed, words, "AMI id or name"))
    if command in INSTANCE_ID_COMMANDS:
        return replace(request, instance_ids=_instance_ids(typed, words))
    if command in TARGET_COMMANDS:
        request = replace(request, app=_target(command, typed, words))
        if command in APP_OPTION_COMMANDS:
            request = replace(request, options=_app_options(typed, words[1:]))
        return request
    if command in APP_ARGS:
        app, key, value = _app(command, typed, words)
        return replace(request, app=app, app_key=key, value=value)
    if command in RULE_COMMANDS:
        description = cli_options.get(DESCRIPTION_OPTION)
        protocol, port, cidr = _rule(typed, words, description)
        return replace(
            request, protocol=protocol, rule_port=port, cidr=cidr, description=description
        )
    if command in PORT_COMMANDS:
        return replace(request, port=_port(typed, words))
    if words:
        raise UsageError(f"{typed}: unexpected argument: {printable(words[0])}")
    return request


def _profile_hint(service: Service | None) -> str:
    """資格情報でつまずいたときに、プロファイルを選べる場所を伝える。"""
    if service is None:
        return f"give {PROFILE_OPTION} or set AWS_PROFILE"
    return (
        f"give {PROFILE_OPTION}, set AWS_PROFILE, or write AwsProfile in "
        f"{printable(str(service.conf_path))}"
    )


def _run_service(request: Request) -> None:
    """--svc が要るコマンドを実行する。"""
    from temt import apply, bake, destroy, proxy, reboot, sg, show, start, status, stop, terminate

    service = request.service
    if service is None:
        # parse がサービスを必ず決めるので、振り分けを書き忘れない限り来ない
        raise TemtError(f"{request.command}: missing service")
    if request.command == APP_LIST or request.command in (*APP_ARGS, *TARGET_COMMANDS):
        _run_app(request, service)
    elif request.command == SERVICE_APPLY:
        apply.run(service, yes=request.yes)
    elif request.command == SERVICE_DIFF:
        apply.diff(service)
    elif request.command == SERVICE_DESTROY:
        destroy.run(service, yes=request.yes)
    elif request.command == INSTANCE_START:
        start.run(service, is_async=request.is_async)
    elif request.command == INSTANCE_STOP:
        stop.run(service, is_async=request.is_async, yes=request.yes)
    elif request.command == INSTANCE_TERMINATE:
        terminate.run(service, ids=request.instance_ids, is_async=request.is_async, yes=request.yes)
    elif request.command == INSTANCE_REBOOT:
        reboot.run(service, is_async=request.is_async, yes=request.yes)
    elif request.command == INSTANCE_SHOW:
        status.run(service)
    elif request.command == AMI_BAKE:
        bake.run(service, yes=request.yes)
    elif request.command == TEMPLATE_SHOW:
        show.template(service)
    elif request.command == SG_DIFF:
        sg.diff(service)
    elif request.command == SG_SHOW:
        sg.show(service)
    elif request.command == SSH_PROXY:
        proxy.run(service, request.port)
    else:
        # parse が COMMANDS の中だけを通すので、振り分けを書き忘れない限り来ない
        raise TemtError(f"not implemented: {request.command}")


def _run_app(request: Request, service: Service) -> None:
    """app のコマンドを実行する。"""
    from temt import app, deploy

    if request.command == APP_LIST:
        app.list_apps(service, request.format)
        return
    app_id, key, value = request.app, request.app_key, request.value
    if app_id is None:
        # parse がアプリ ID を必ず読むので、振り分けを書き忘れない限り来ない
        raise TemtError(f"{request.command}: missing app id")
    if request.command == APP_SHOW:
        app.show(service, app_id)
    elif request.command == APP_GET and key is not None:
        app.get(service, app_id, key)
    elif request.command == APP_SET and key is not None and value is not None:
        app.set_value(service, app_id, key, value)
    elif request.command == APP_UNSET and key is not None:
        app.unset(service, app_id, key)
    elif request.command == APP_DEPLOY:
        deploy.run(
            service,
            app_id,
            user=request.user,
            baking=request.bake,
            yes=request.yes,
            given=dict(request.options),
            reset=request.reset,
        )
    elif request.command == APP_REMOVE:
        deploy.remove(service, app_id, user=request.user, baking=request.bake, yes=request.yes)
    else:
        raise TemtError(f"not implemented: {request.command}")


def _run_local(request: Request) -> None:
    """AWS を呼ばないコマンドを実行する。"""
    if request.command == SERVICE_LIST:
        from temt import services

        services.list_services(request.service, request.format)
        return
    if request.command == APP_LIST:
        from temt import manifest

        manifest.list_apps(request.format)
        return
    from temt import ingress

    service = request.service
    if service is None:
        # parse がサービスを必ず決めるので、振り分けを書き忘れない限り来ない
        raise TemtError(f"{request.command}: missing service")
    if request.command == SG_LIST:
        ingress.list_rules(service, request.format)
        return
    protocol = request.protocol
    if protocol is None:
        # parse が RULE_COMMANDS の <ポート> を必ず読むので、振り分けを書き忘れない限り来ない
        raise TemtError(f"{request.command}: missing port")
    if request.command == SG_ADD:
        ingress.add(service, protocol, request.rule_port, request.cidr, request.description)
    elif request.command == SG_REMOVE:
        ingress.remove(service, protocol, request.rule_port, request.cidr)
    else:
        raise TemtError(f"not implemented: {request.command}")


def _run_ami_delete(request: Request) -> None:
    """ami delete を実行する。"""
    from temt import images

    if request.image is None:
        # parse が IMAGE_COMMANDS の ID か名前を必ず読むので、振り分けを書き忘れない限り来ない
        raise TemtError(f"{request.command}: missing AMI id or name")
    images.delete(
        request.image, request.service, request.region, force=request.force, yes=request.yes
    )


def _run_keypair(request: Request) -> None:
    """keypair のコマンドを実行する。"""
    from temt import keypairs

    if request.command == KEYPAIR_LIST:
        keypairs.list_keypairs(request.region, request.format)
        return
    key = request.key
    if key is None:
        # parse が KEY_COMMANDS の名前を必ず読むので、振り分けを書き忘れない限り来ない
        raise TemtError(f"{request.command}: missing key pair name")
    if request.command == KEYPAIR_SHOW:
        keypairs.show(key, request.region)
    elif request.command == KEYPAIR_CREATE:
        keypairs.create(key, request.region)
    elif request.command == KEYPAIR_DELETE:
        keypairs.delete(key, request.region, yes=request.yes)
    else:
        raise TemtError(f"not implemented: {request.command}")


def run(request: Request) -> int:
    """要求を実行し、終了コードを返す。

    AWS を使う module は、この中で読み込む。module の先頭で読み込むと、boto3 が
    無いときに main が動く前に落ちる。botocore の例外も、ここで一度だけ包む。
    """
    # どのプロファイルを使うかは、--profile と設定ファイルで決める
    conf.OPTION_PROFILE = request.profile
    if _is_local(request.command, request.service):
        # AWS を呼ばないので、boto3 が無くても動くよう、読み込む前に済ませる
        _run_local(request)
        return 0

    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        ProfileNotFound,
    )

    from temt import images, status

    try:
        if request.command == INSTANCE_LIST:
            status.list_instances(request.service, request.region, request.format)
        elif request.command == AMI_LIST:
            images.list_images(request.service, request.region, request.format)
        elif request.command == AMI_PRUNE:
            images.prune(request.service, request.region, yes=request.yes)
        elif request.command == AMI_DELETE:
            _run_ami_delete(request)
        elif request.command in (KEYPAIR_LIST, *KEY_COMMANDS):
            _run_keypair(request)
        else:
            _run_service(request)
    except (ProfileNotFound, NoCredentialsError) as e:
        raise TemtError(f"{aws_error(e)}; {_profile_hint(request.service)}") from e
    except (ClientError, BotoCoreError) as e:
        raise TemtError(aws_error(e)) from e
    return 0


def main(argv: list[str] | None = None) -> int:
    """引数を解釈してコマンドを実行し、終了コードを返す。argv は sys.argv と同じ形。"""
    argv = list(sys.argv) if argv is None else argv
    # --svc を付けたコマンドは、どのリージョンを使うかを設定ファイルだけで決める
    conf.take_region_env()
    text = _early(argv[1:])
    if text is not None:
        sys.stdout.write(text)
        return 0
    try:
        return run(parse(argv[1:]))
    except UsageError as e:
        msg.error(str(e))
        # --help も --version も無いので、_words の最初の語は、
        # コマンドラインオプションを除いた 1 語めである
        sys.stderr.write(_usage_for(next(_words(argv[1:]), None)))
        return e.exit_code
    except TemtError as e:
        msg.error(str(e))
        return e.exit_code
    except ImportError as e:
        # boto3 などが入っていないとき。取り込みは run の中で行う
        msg.error(f"{e}; reinstall temt")
        return TemtError.exit_code
    except OSError as e:
        # ファイルやディレクトリの読み書きの失敗。文はパスを含むので、表示できない文字を
        # 置き換え、プログラム名を付けて知らせる
        msg.error(printable(str(e)))
        return TemtError.exit_code
    except KeyboardInterrupt:
        # Ctrl-C は端末に ^C だけを出すので、行を閉じてから知らせる
        msg.tell("\n")
        msg.error("interrupted")
        return TemtError.exit_code
