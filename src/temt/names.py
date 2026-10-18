"""サービス名の検査と、そこから導く名前・パス。

設定ファイルの場所（conf ディレクトリ）と、ssh の設定・鍵・known_hosts の場所
（ssh ディレクトリ）は、それぞれ環境変数で変えられる。環境変数は起動のたびに読み、
保存しない。
"""

import errno
import os
import re
from dataclasses import dataclass
from pathlib import Path

from temt.msg import TemtError, UsageError
from temt.table import printable, tilde

# コマンドの名前。リソースの名前とサービスのコマンド名 temt-<svc> の頭にも付く
PREFIX = "temt"
# <名前> の長さの上限。IAM ロール名の上限に合わせる
NAME_MAX = 64

# サービス名は、英小文字で始まり、英小文字・数字・ハイフンが続き、英小文字か数字で終わる
SVC_RE = re.compile(r"[a-z]([a-z0-9-]*[a-z0-9])?")

# アプリ ID、アプリの値のキー、ロールのポリシーの名前の規則。サービス名と同じ形で、
# 長さに上限を置く
LABEL_MAX = 64
LABEL_RULE = (
    "must start with a lowercase letter, use only lowercase letters, digits, and '-', "
    f"end with a letter or digit, and be at most {LABEL_MAX} characters"
)
# temt がアプリの記録に使うキー。app set と app unset からは扱えない
DEPLOY_KEY = "deploy"
# アプリがインスタンスから自分の状態を書くキー。status が表示し、app remove が消す
STATUS_KEY = "status"
# アプリの install と uninstall を実行するインスタンスのユーザーの名前の規則。shadow の
# useradd の上流の既定の規則から、末尾の $ を除いたもの。Debian 系の useradd はこれより緩い
USER_NAME_RE = re.compile(r"[a-z_][a-z0-9_-]*")
USER_NAME_MAX = 32
USER_NAME_RULE = (
    "must start with a lowercase letter or '_', use only lowercase letters, digits, '_', "
    f"and '-', and be at most {USER_NAME_MAX} characters"
)
# 指定が無いときに install と uninstall を実行するユーザー
ROOT_USER = "root"


# インスタンス ID。i- と、8 桁か 17 桁の小文字の 16 進数
INSTANCE_ID_RE = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})")
INSTANCE_ID_RULE = "must be 'i-' followed by 8 or 17 lowercase hexadecimal digits"

# 場所を変える環境変数と、その既定
CONF_DIR_ENV = "TEMT_CONF_DIR"
CONF_DIR_DEFAULT = f"~/.config/{PREFIX}"
SSH_DIR_ENV = "TEMT_SSH_DIR"
SSH_DIR_DEFAULT = f"~/.ssh/{PREFIX}"
# 利用者のアプリを置く、conf ディレクトリの下のディレクトリ
USER_APPS = "apps"
# 全サービスに共通の値を書く、conf ディレクトリの共通ファイル <DEFAULTS>.toml。この名前は
# サービス名に使えない
DEFAULTS = "defaults"
# temt と一緒に配るアプリの場所
BUNDLED_APPS = Path(__file__).parent / "apps"
# 場所を表すパスに使えない文字。ssh の設定は空白で語を分け、% をトークンとして、
# ${NAME} を環境変数として展開し、引用符と \ を自前で解釈する
FORBIDDEN = "\"'\\%$"


def expand(value: str) -> Path:
    """先頭の ~ と ~/ だけをホームに展開する。~user は展開しない。"""
    if value == "~":
        return Path.home()
    if value.startswith("~/"):
        return Path.home() / value[2:].lstrip("/")
    return Path(value)


def _place(env: str, default: str) -> Path:
    """場所を変える環境変数を読む。無いか空なら既定を使う。

    相対パスを許さないのは、ssh から呼ばれる proxy のカレントディレクトリが利用者の
    シェルのものと違うからである。
    """
    value = os.environ.get(env) or default
    # 表示できない文字には、UTF-8 として読めないバイトも入る。ssh の設定に書けない
    if any(char.isspace() or char in FORBIDDEN or not char.isprintable() for char in value):
        raise TemtError(
            f"{env} must not contain whitespace, a quote, a backslash, a percent sign, "
            f"a dollar sign, or an unprintable character: {printable(value)}"
        )
    path = expand(value)
    if not path.is_absolute():
        raise TemtError(f"{env} must be an absolute path: {printable(value)}")
    return path


def conf_dir() -> Path:
    """設定ファイルを置くディレクトリ。"""
    return _place(CONF_DIR_ENV, CONF_DIR_DEFAULT)


def defaults_path() -> Path:
    """共通ファイルのパス。"""
    return conf_dir() / f"{DEFAULTS}.toml"


def default_conf_dir() -> Path:
    """環境変数が無いときの conf ディレクトリ。"""
    return expand(CONF_DIR_DEFAULT)


def resolved(path: Path) -> Path:
    """.. とシンボリックリンクを解いたパス。同じ場所かどうかを比べるときに使う。

    リンクの循環は、どの版でも利用者に見せるエラーにする。Python 3.12 の resolve は
    循環で RuntimeError を上げるが、3.13 からは strict でなければ上げずに解けたところ
    までを返すので、strict で解いて ELOOP を見る。無いパスなど、ほかの理由で strict が
    失敗したときは、strict でなく解き、その結果をもう一度 strict で解いて ELOOP を見る。
    無い要素の後ろの循環（missing/../loop など）は、そこで見つかる。
    """
    try:
        found = path.resolve(strict=True)
    except RuntimeError:
        found = None
    except OSError as e:
        found = None if e.errno == errno.ELOOP else _loose(path)
    if found is None:
        raise TemtError(f"symlink loop in {printable(tilde(path))}")
    return found


def _loose(path: Path) -> Path | None:
    """strict でなく解いたパス。循環を含めば None。"""
    try:
        found = path.resolve()
    except RuntimeError:
        return None
    try:
        _ = found.resolve(strict=True)
    except OSError as e:
        if e.errno == errno.ELOOP:
            return None
    return found


def exists(path: Path) -> bool:
    """そのパスがあるかどうか。調べられなければ、パスを挙げて終了する。

    Path.exists は、調べられないとき Python 3.12 では例外を上げ、3.14 では False を
    返す。版で動きが変わらないよう、ここで決める。
    """
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as e:
        raise TemtError(f"cannot check {printable(tilde(path))}: {e.strerror}") from None
    return True


def ssh_dir() -> Path:
    """ssh の設定・鍵・known_hosts を置くディレクトリ。

    ~/.ssh そのものは使えない。temt の config が ~/.ssh/config と同じファイルになり、
    自分自身を Include してしまう。
    """
    path = _place(SSH_DIR_ENV, SSH_DIR_DEFAULT)
    if resolved(path) == resolved(user_ssh_dir()):
        raise TemtError(
            f"{SSH_DIR_ENV} must not be {printable(tilde(user_ssh_dir()))} itself: "
            f"{printable(tilde(path))}"
        )
    return path


def user_ssh_dir() -> Path:
    """利用者の ssh の設定を置くディレクトリ。"""
    return Path.home() / ".ssh"


def bin_dir() -> Path:
    """サービスのコマンドを置くディレクトリ。"""
    return Path.home() / ".local" / "bin"


def keys_dir() -> Path:
    """鍵を置くディレクトリ。"""
    return ssh_dir() / "keys"


def is_label(name: str) -> bool:
    """アプリ ID、アプリの値のキー、ロールのポリシーの名前として使える名前か。"""
    return len(name) <= LABEL_MAX and SVC_RE.fullmatch(name) is not None


def is_instance_id(text: str) -> bool:
    """インスタンス ID の形をしているか。"""
    return INSTANCE_ID_RE.fullmatch(text) is not None


def is_user_name(name: str) -> bool:
    """install と uninstall を実行するユーザーとして指せる名前か。"""
    return len(name) <= USER_NAME_MAX and USER_NAME_RE.fullmatch(name) is not None


def app_dir(target: str) -> Path:
    """deploy にパスで渡したアプリのディレクトリ。絶対パスにして . と .. を除く。

    シンボリックリンクは解かない。最後の要素（リンクならリンクの名前）がアプリ ID に
    なる。字面とカレントディレクトリだけで決まるので、引数を読むときに確かめられる。
    """
    return Path(os.path.normpath(expand(target).absolute()))


def user_apps_dir() -> Path:
    """利用者のアプリの場所。conf ディレクトリの apps/。"""
    return conf_dir() / USER_APPS


def app_ids(directory: Path) -> list[str]:
    """そのディレクトリにあるアプリの ID を名前の順に返す。

    アプリ ID の規則に合う名前のディレクトリ（ディレクトリへのリンクを含む）だけを
    挙げる。ディレクトリが無ければ空である。
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(path.name for path in entries if is_label(path.name) and path.is_dir())


def make_dir(path: Path) -> None:
    """そのディレクトリを、無い親ごとモード 700 で用意する。

    temt が作るディレクトリは、どれも他人に見せない。mkdir のモードは最後の要素にしか
    効かないので、上から順に作る。既にあるディレクトリのモードは変えない。
    """
    for parent in reversed(path.parents):
        if not parent.exists():
            parent.mkdir(mode=0o700, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)


def replace_text(path: Path, text: str, *, mode: int = 0o666) -> None:
    """ファイルの中身を text にする。途中で止まっても、空や書きかけのファイルを残さない。

    同じディレクトリの一時ファイルに書いてディスクに届けてから、置き換える。置き換え先は
    シンボリックリンクを解いた先にして、リンクを普通のファイルに変えてしまわないように
    する。ファイルがあればそのモードを引き継ぎ、無ければ mode に umask を掛けたモードで
    作る。引き継ぐのはモードの下位 9 ビットだけで、別の inode（ファイルの実体）に
    なるので、所有者、特殊ビット、ハードリンクは保たない。一時名にはプロセス ID を入れ、
    同時に走った別の temt と同じ名前を使わない。書けなければ一時ファイルを消す。
    """
    target = resolved(path)
    try:
        kept: int | None = target.stat().st_mode & 0o777
    except FileNotFoundError:
        kept = None
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        # 書き終えるまでは、元のモードがどれほど開いていても他人に見せない
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode if kept is None else 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            _ = file.write(text)
            file.flush()
            os.fsync(file.fileno())
        if kept is not None:
            temporary.chmod(kept)
        _ = temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def places() -> dict[str, Path]:
    """2 つの場所を、環境変数の名前ごとに返す。並びは conf、ssh の順。

    apply が ProxyCommand の env に書き、ssh から呼ばれる proxy に場所を伝える。
    既定のままでも書くのは、ssh が ProxyCommand に自分の環境変数を渡すからである。
    書かなければ、proxy は ssh を打ったシェルの値を受け継ぎ、別の場所を読む。
    """
    return {CONF_DIR_ENV: conf_dir(), SSH_DIR_ENV: ssh_dir()}


@dataclass(frozen=True)
class Service:
    """サービス。作るときに名前を検査する。

    セキュリティグループ、IAM ロール、インスタンスプロファイル、ssh の Host、
    インスタンスの Name タグは、どれも name をそのまま使う。
    """

    svc: str

    def __post_init__(self) -> None:
        if not SVC_RE.fullmatch(self.svc):
            raise UsageError(f"invalid service name: {printable(self.svc)}")
        if self.svc == DEFAULTS:
            raise UsageError(
                f"invalid service name: {DEFAULTS} (reserved for the file {DEFAULTS}.toml)"
            )
        if len(self.name) > NAME_MAX:
            raise UsageError(f"name longer than {NAME_MAX} characters: {self.name}")

    @property
    def name(self) -> str:
        """<名前>。AWS のリソースと ssh の Host の名前の元になる。"""
        return f"{PREFIX}-{self.svc}"

    @property
    def launch_template(self) -> str:
        """起動テンプレートの名前。"""
        return f"{self.name}-template"

    @property
    def parameter_path(self) -> str:
        """このサービスの SSM パラメータを置く階層。アプリの値と記録はこの下に置く。"""
        return f"/{self.name}"

    def app_path(self, app: str) -> str:
        """アプリの値と記録を置く階層 /<名前>/<アプリID>。"""
        return f"{self.parameter_path}/{app}"

    def app_parameter(self, app: str, key: str) -> str:
        """アプリの値 1 つの SSM パラメータの名前 /<名前>/<アプリID>/<キー>。"""
        return f"{self.app_path(app)}/{key}"

    @property
    def conf_name(self) -> str:
        """設定ファイルの名前。場所はここに含まない。"""
        return f"{self.svc}.toml"

    @property
    def conf_path(self) -> Path:
        """設定ファイルのパス。"""
        return conf_dir() / self.conf_name

    @property
    def command_path(self) -> Path:
        """サービスのコマンドのパス。"""
        return bin_dir() / self.name

    @property
    def known_hosts_dir(self) -> Path:
        """インスタンスごとの known_hosts を置くディレクトリ。"""
        return ssh_dir() / "known_hosts.d" / self.name

    @property
    def known_hosts_current(self) -> Path:
        """ssh に渡す known_hosts。今のインスタンスのファイルへのシンボリックリンク。"""
        return self.known_hosts_dir / "current"
