"""利用者のホームに置くものを用意する。

ssh ディレクトリ、サービスのコマンド、ssh の設定を扱う。
"""

import fcntl
import os
import re
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from temt import msg
from temt.change import ADD, ARROW, UPDATE, Change, lines_diff
from temt.conf import Conf
from temt.msg import TemtError
from temt.names import (
    CONF_DIR_ENV,
    PREFIX,
    Service,
    conf_dir,
    default_conf_dir,
    exists,
    expand,
    keys_dir,
    make_dir,
    places,
    replace_text,
    resolved,
    ssh_dir,
    tilde,
    user_ssh_dir,
)
from temt.table import printable

# インスタンスに入るときの利用者。AMI が Ubuntu なので固定する
LOGIN_USER = "ubuntu"
# temt が管理する項目。ブロックに足すときはこの順に並べる
MANAGED = ("User", "Port", "IdentityFile", "IdentitiesOnly", "UserKnownHostsFile", "ProxyCommand")
# ~/.ssh/config に置く目印。この次の行が temt の Include である
MARK = "# ===== temt ===== do not remove; 'temt apply' owns the next line"
# ssh は Include のパスを glob として読む。パスそのものに一致させるため \ を付ける文字。
# {} は展開されず、] も単独では特別扱いされないので、守るのはこの 3 つだけでよい
GLOB = "*?["

_WIDTH = max(len(keyword) for keyword in MANAGED)
_LOWER = {keyword.lower(): keyword for keyword in MANAGED}
# ssh ディレクトリと keys/ のモード
DIR_MODE = 0o700
# サービスのコマンドのモード。umask が掛かる
COMMAND_MODE = 0o755
# ssh の設定を書き換えるあいだ取るロックのファイル。ssh ディレクトリに置く。設定の
# ファイルは置き換えるので、ロックはそれとは別の、置き換えないファイルに掛ける
EDIT_LOCK_NAME = ".config.lock"

# 差分の種類。change.Change の kind
DIR = "dir"
COMMAND = "command"
SSH_HOST = "ssh-host"
SSH_INCLUDE = "ssh-include"


@dataclass(frozen=True)
class Dir:
    """prepare_dirs が作るか締めるディレクトリ。mode は今のモードで、無ければ None。"""

    path: Path
    mode: int | None


def detect_dirs() -> list[Dir]:
    """ssh ディレクトリと keys/ のうち、無いものとモードが 700 でないもの。

    無い親ディレクトリも、prepare_dirs が作るので、上から順に挙げる。
    """
    found: list[Dir] = []
    for path in (ssh_dir(), keys_dir()):
        for parent in reversed(path.parents):
            if not parent.exists() and all(one.path != parent for one in found):
                found.append(Dir(parent, None))
        if not path.exists():
            if all(one.path != path for one in found):
                found.append(Dir(path, None))
        elif path.stat().st_mode & 0o777 != DIR_MODE:
            found.append(Dir(path, path.stat().st_mode & 0o777))
    return found


def dir_changes(found: list[Dir]) -> list[Change]:
    """detect_dirs の結果を、diff に出す形にする。"""
    return [
        Change(ADD, DIR, tilde(one.path), f"mode {DIR_MODE:o}")
        if one.mode is None
        else Change(UPDATE, DIR, tilde(one.path), f"mode: {one.mode:o}{ARROW}{DIR_MODE:o}")
        for one in found
    ]


def prepare_dirs() -> None:
    """ssh ディレクトリと keys/ をモード 700 で用意する。

    既にあって緩ければ締める。無い親ディレクトリも 700 で作るが、既にある親の
    モードは変えない。
    """
    for path in (ssh_dir(), keys_dir()):
        make_dir(path)
        path.chmod(DIR_MODE)


def prepare_known_hosts(service: Service) -> Path:
    """サービスの known_hosts のディレクトリをモード 700 で用意し、その場所を返す。

    mkdir のモードは最後の要素にしか効かないので、ssh ディレクトリまでは
    prepare_dirs に用意させる。ロックファイルもこのディレクトリに置くので、
    ロックを取る前に呼ぶ。
    """
    prepare_dirs()
    path = service.known_hosts_dir
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def use_known_hosts(service: Service, instance_id: str) -> Path:
    """そのインスタンスの known_hosts を用意し、current をそれへ張り替える。

    ファイルは追記モードで開いて閉じる。無ければモード 600 の空のファイルができ、
    あれば中身に触れない。同時に走った別の接続がホスト鍵を書き込んだ直後でも、
    それを消さない。current は一時名のリンクを作ってから改名で置き換える。消して
    から作り直すと、その間に known_hosts を読んだ ssh がホストを未知として扱う。
    一時名にはプロセス ID を入れ、同時に走った接続と同じ名前を使わないようにする。
    """
    directory = prepare_known_hosts(service)
    path = directory / instance_id
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    path.chmod(0o600)
    fresh = directory / f".current.{os.getpid()}"
    fresh.unlink(missing_ok=True)
    fresh.symlink_to(instance_id)
    fresh.replace(service.known_hosts_current)
    return path


def forget_known_hosts(service: Service, instance_id: str) -> None:
    """そのインスタンスの known_hosts を消す。無ければ何もしない。

    current がそれを指していてリンク切れになったら、current も消す。
    """
    (service.known_hosts_dir / instance_id).unlink(missing_ok=True)
    current = service.known_hosts_current
    if current.is_symlink() and not current.exists():
        current.unlink()


def forget_service(service: Service) -> bool:
    """サービスの known_hosts のディレクトリを、中身ごと消す。消したら True を返す。

    destroy はロックを持ったままこれを呼ぶ。ロックファイルもこのディレクトリに
    あるが、Linux のロックは開いたファイルに付くので、消してもロックは離れない。
    利用者がここをシンボリックリンクにしていれば、その先まで消さずに終了する。
    """
    directory = service.known_hosts_dir
    if directory.is_symlink():
        raise TemtError(
            f"{printable(tilde(directory))} is a symlink; remove it by hand and run destroy again"
        )
    if not directory.exists():
        return False
    shutil.rmtree(directory)
    return True


def _command_script(service: Service) -> str:
    """サービスのコマンドの中身。PATH にある temt を、--svc を付けて実行する。"""
    return (
        "#!/bin/sh\n"
        f"# Written by temt apply for the service {service.svc}; temt destroy removes it.\n"
        f'exec {PREFIX} --svc {service.svc} "$@"\n'
    )


def _present(path: Path) -> bool:
    """そのパスに何かあるか。行き先の無いシンボリックリンクも、あるとみなす。"""
    return path.is_symlink() or path.exists()


def command_missing(service: Service) -> bool:
    """サービスのコマンドの場所に何も無いか。何かあれば、中身にかかわらず触れない。"""
    return not _present(service.command_path)


def command_changes(service: Service, missing: bool) -> list[Change]:
    """command_missing の結果を、diff に出す形にする。"""
    if not missing:
        return []
    return [Change(ADD, COMMAND, tilde(service.command_path))]


def write_command(service: Service) -> None:
    """サービスのコマンドを書く。置くディレクトリが無ければ作る。"""
    path = service.command_path
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_text(path, _command_script(service), mode=COMMAND_MODE)


def remove_command(service: Service) -> bool:
    """サービスのコマンドを、中身にかかわらず消す。消したら True を返す。"""
    path = service.command_path
    if not _present(path):
        return False
    path.unlink()
    return True


def _keyword(line: str) -> tuple[str, list[str]]:
    """行の先頭の語を小文字にして、残りの語とともに返す。空行とコメントは空の語になる。

    ssh と同じく、= で分けるのはキーワードと最初の引数の間だけにする。引数どうしは
    空白で分けるので、= を含むパスも 1 つの語のまま残る。
    """
    item = line.strip()
    if not item or item.startswith("#"):
        return "", []
    words = re.split(r"[ \t=]+", item, maxsplit=1)
    return words[0].lower(), words[1].split() if len(words) > 1 else []


def _item(keyword: str, value: str) -> str:
    """ブロックの中の 1 行。"""
    return f"    {keyword.ljust(_WIDTH)} {value}"


def _ssh_path(value: str) -> str:
    """ssh の設定に書くパス。% を %% にし、半角スペースを含めば二重引用符で囲む。

    ssh は % をトークンの目印として展開し、語を空白で分ける。二重引用符の中の空白は
    語を分けない。空白の無い値は囲まず、これまでに書いた行を変えない。二重引用符の
    中でも ssh が解釈する引用符、\\、$ と、行を壊す空白類や制御文字は、設定ファイルを
    読むときに断ってある（conf.check_identity_file）。
    """
    escaped = value.replace("%", "%%")
    return f'"{escaped}"' if " " in escaped else escaped


def _proxy_command(service: Service) -> str:
    """ProxyCommand の値。2 つの場所を、既定のままでも env で proxy に渡す。

    シェルに環境変数が無くても、別の値があっても、ssh の接続は apply のときと同じ
    場所を使う。ssh は ProxyCommand を利用者のシェルの exec に渡すので、シェルの
    代入 (VAR=値 コマンド) は使えない。exec の後ろでは、代入がコマンド名として
    扱われる。値はシングルクォートで囲み、; や & を含むパスでも壊れないようにする。
    ホームの下なら ~ で始まる形で書き、proxy が自分で展開する。ホームのパスそのもの
    （空白や % を含みうる）を ssh の設定に出さないためである。temt はパスでなく名前で
    書き、env が PATH から探す。入れ方や版で temt の場所が変わっても、書き直さずに済む。
    """
    assigned = " ".join(f"{env}='{tilde(path)}'" for env, path in places().items())
    return f"env {assigned} {PREFIX} proxy --svc {service.svc} %p"


def _managed(service: Service, values: Conf) -> dict[str, str]:
    """temt が管理する項目の値。ProxyCommand はサービスのコマンドでなく temt を呼ぶ。

    Port は SshdPorts の先頭で、ProxyCommand の %p になる。
    """
    return {
        "User": LOGIN_USER,
        "Port": str(values.SshdPorts[0]),
        "IdentityFile": _ssh_path(values.IdentityFile),
        "IdentitiesOnly": "yes",
        "UserKnownHostsFile": tilde(service.known_hosts_current),
        "ProxyCommand": _proxy_command(service),
    }


@dataclass(frozen=True)
class Edit:
    """ssh の設定ファイル 1 つの書き換え。before は読んだ中身、after は書く中身。

    replaced は、Include の行を張り替えるときの前の行。
    """

    path: Path
    before: str
    after: str
    replaced: str | None = None

    @property
    def changed(self) -> bool:
        """書き換えるか。"""
        return self.after != self.before


def _joined(lines: list[str]) -> str:
    """行をつないでファイルの中身にする。"""
    return "\n".join(lines) + "\n"


def write_edit(edit: Edit) -> bool:
    """検出した書き換えを書く。中身が変わらなければ何もしない。書いたら True を返す。

    検出してから書くまでにファイルが変わっていれば、書かずに止める。利用者が手で
    書いた ~/.ssh/config を失わないよう、replace_text で置き換える。無ければモード
    600 で作る。確かめてから置き換えるまでは、ssh の設定を書き換えるロックを持つ。
    別のサービスの apply が同時に書いても、片方の書き換えを消さない。
    """
    if not edit.changed:
        return False
    with _edit_lock():
        _replace(edit)
    return True


def _replace(edit: Edit) -> None:
    """確かめてから置き換える。ssh の設定を書き換えるロックを持って呼ぶ。"""
    _check_unchanged(edit)
    replace_text(edit.path, edit.after, mode=0o600)


@contextmanager
def _edit_lock() -> Generator[None]:
    """ssh の設定を書き換えるロックを取る。持っているものがあれば、離すまで待つ。

    ロックは ssh ディレクトリごとに 1 つで、サービスによらないので、書き換えは 1 つずつに
    なる。TEMT_SSH_DIR を変えて同時に打った apply どうしは、~/.ssh/config の書き換えで
    守り合わない。持つのは確かめてから置き換えるまでの一瞬なので、待っても長くはならない。
    ssh ディレクトリが無ければモード 700 で作る。
    """
    path = ssh_dir() / EDIT_LOCK_NAME
    make_dir(path.parent)
    path.touch(mode=0o600, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        yield


def _check_unchanged(edit: Edit) -> None:
    """検出したときから、ファイルの中身が変わっていないことを確かめる。"""
    if _read(edit.path) != edit.before:
        raise TemtError(
            f"{printable(tilde(edit.path))} changed after temt read it; run apply again"
        )


def _read(path: Path) -> str:
    """ssh の設定ファイルを読む。無ければ空とする。UTF-8 で読めなければ終了する。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        raise TemtError(f"{printable(tilde(path))}: not UTF-8") from None


def _lines(text: str) -> list[str]:
    """行に分ける。末尾の改行が作る空の要素は落とす。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _block(lines: list[str], service: Service) -> tuple[int, int] | None:
    """Host <名前> ブロックの範囲。Host 行の位置と、ブロックの終わりの次の位置を返す。

    Host 行を空白で分けた語のどれかがサービスの名前と一致するブロックが、temt の
    ものである。他の名前と並べて書いた Host 行でもよい。そうした Host 行が 2 つ
    あれば、ssh と同じく先頭を採る。無ければ None を返す。
    """
    for start, line in enumerate(lines):
        keyword, patterns = _keyword(line)
        if keyword != "host" or service.name not in patterns:
            continue
        for index in range(start + 1, len(lines)):
            if _keyword(lines[index])[0] in ("host", "match"):
                return start, index
        return start, len(lines)
    return None


def _recorded_conf_dir(service: Service) -> Path | None:
    """既にある Host ブロックを書いた apply の conf ディレクトリ。

    ProxyCommand の env に TEMT_CONF_DIR があればその値、無ければ既定の場所である。
    ブロックか ProxyCommand が無ければ None を返す。apply が書く値は空白を含まない
    ので、語に分けて探せる。先頭の ~/ は proxy と同じくホームに展開する。
    """
    path = ssh_dir() / "config"
    lines = _lines(_read(path))
    found = _block(lines, service)
    if found is None:
        return None
    start, end = found
    for line in lines[start + 1 : end]:
        keyword, words = _keyword(line)
        if keyword != "proxycommand":
            continue
        for word in words:
            if word.startswith(f"{CONF_DIR_ENV}="):
                # proxy と同じく、空なら既定とし、絶対パスにならない値は手がかりにしない
                value = word.removeprefix(f"{CONF_DIR_ENV}=").strip("'")
                if not value:
                    return default_conf_dir()
                path = expand(value)
                return path if path.is_absolute() else None
        return default_conf_dir()
    return None


def check_owner(service: Service) -> None:
    """そのサービスを別の conf ディレクトリから apply していれば、エラーにする。

    ssh ディレクトリは conf ディレクトリをまたいで 1 つなので、同じ <名前> の Host ブロックも
    1 つしか持てない。ブロックを書いた場所が今と違い、そこにサービスの設定ファイルが
    まだあれば、同じサービスの定義が 2 つあることになるので止める。設定ファイルが
    無ければ、場所を移したものとみなして進む。今の場所のファイルと実体が同じ
    （シンボリックリンクなど）なら、定義は 1 つなので進む。前の場所を調べられなければ
    止める。
    """
    recorded = _recorded_conf_dir(service)
    if recorded is None or resolved(recorded) == resolved(conf_dir()):
        return
    other = recorded / service.conf_name
    if not exists(other) or resolved(other) == resolved(service.conf_path):
        return
    where = (
        f"{service.name} is set up from another config directory: {printable(tilde(other))} exists"
    )
    if exists(service.conf_path):
        raise TemtError(
            f"{where}, and so does {printable(tilde(service.conf_path))}; "
            "remove one of them and run apply again"
        )
    raise TemtError(
        f"{where}; run apply with {CONF_DIR_ENV}={printable(tilde(recorded))}, "
        f"or move that file to {printable(tilde(conf_dir()))}"
    )


def detect_host_block(service: Service, values: Conf) -> Edit:
    """ssh ディレクトリの config の Host <名前> ブロックを足すか、管理する項目を書き換える
    中身を求める。書くのは write_edit である。

    管理しない項目、他のブロック、コメントには手を付けない。管理する項目が 2 行
    あれば先頭の行だけを残す。IdentityFile は ssh が並んだ行をすべて使うので、
    temt の鍵だけを使わせるために後の行を落とす。管理する項目の行は、書かれた値を
    読まずに、いつも temt が書く形で作り直し、ファイルの中身どうしを比べる。手で
    引用符を付けた行など、ssh には同じ値に読める行も、temt が書く形に戻す。
    """
    path = ssh_dir() / "config"
    text = _read(path)
    lines = _lines(text)
    wanted = _managed(service, values)

    found = _block(lines, service)
    if found is None:
        block = [f"Host {service.name}", *(_item(key, wanted[key]) for key in MANAGED)]
        return Edit(path, text, _joined([*lines, "", *block] if lines else block))

    start, end = found
    kept: list[str] = []
    seen: list[str] = []
    for line in lines[start + 1 : end]:
        keyword, _ = _keyword(line)
        name = _LOWER.get(keyword)
        if name is None:
            kept.append(line)
        elif name not in seen:
            seen.append(name)
            kept.append(_item(name, wanted[name]))
    missing = [_item(key, wanted[key]) for key in MANAGED if key not in seen]
    return Edit(path, text, _joined(lines[: start + 1] + missing + kept + lines[end:]))


def _block_lines(text: str, service: Service) -> list[str]:
    """中身のうち、Host <名前> ブロックの行。無ければ空。

    次のブロックとの間の空行は、ブロックの中身ではないので含めない。
    """
    lines = _lines(text)
    found = _block(lines, service)
    if found is None:
        return []
    start, end = found
    block = lines[start:end]
    while block and not block[-1].strip():
        block.pop()
    return block


def host_block_changes(service: Service, edit: Edit) -> list[Change]:
    """detect_host_block の結果を、diff に出す形にする。ブロックの違いを続く行に出す。"""
    if not edit.changed:
        return []
    before = _block_lines(edit.before, service)
    after = _block_lines(edit.after, service)
    mark = UPDATE if before else ADD
    return [Change(mark, SSH_HOST, service.name, tilde(edit.path), lines_diff(before, after))]


def _escaped(path: str) -> str:
    r"""Include に書くパス。glob の特別な文字を \ で守る。"""
    return "".join(f"\\{char}" if char in GLOB else char for char in path)


def include_line() -> str:
    """~/.ssh/config に置く Include の行。ssh ディレクトリの config を指す。"""
    return f"Include {_escaped(tilde(ssh_dir() / 'config'))}"


def _included(line: str) -> list[Path]:
    """Include の行が指すファイル。

    temt が書いた行を読むためのもので、glob の特別な文字を守る \\ を外し、先頭の ~/
    をホームに、相対パスを ~/.ssh からのものとして解く。glob は展開しない。
    """
    paths: list[Path] = []
    for word in _keyword(line)[1]:
        plain = re.sub(r"\\([*?\[])", r"\1", word)
        if plain.startswith("~/"):
            paths.append(Path.home() / plain[2:])
        elif plain.startswith("/"):
            paths.append(Path(plain))
        else:
            paths.append(user_ssh_dir() / plain)
    return paths


def _hosts(path: Path) -> list[str]:
    """ssh の設定ファイルの Host 行に書かれた名前。_host_names を見よ。

    読めなければ空を返す。知らせるために読むだけなので、ここで apply を止めない。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return _host_names(text)


def _host_names(text: str) -> list[str]:
    """ssh の設定の Host 行に書かれた名前。重複は除き、書かれた順に並べる。

    ワイルドカードと否定のパターンは、つなぐ先の名前ではないので除く。
    """
    names: list[str] = []
    for line in _lines(text):
        keyword, patterns = _keyword(line)
        if keyword != "host":
            continue
        names.extend(
            one for one in patterns if not any(c in one for c in "*?!") and one not in names
        )
    return names


def _dropped(line: str, kept: set[str]) -> list[tuple[Path, list[str]]]:
    """張り替える前の Include が指していたファイルと、そこにあって ssh から見えなくなる Host。

    張り替えた後、ssh はそのファイルを読まなくなるので、その Host にはつながらない。
    kept は新しい config の Host で、そこにも同じ名前があれば、そちらでつながるので
    挙げない。
    """
    current = ssh_dir() / "config"
    found: list[tuple[Path, list[str]]] = []
    for path in _included(line):
        # 知らせるために見るだけなので、調べられないファイルは飛ばして張り替えを続ける
        try:
            if not path.is_file() or path.resolve() == current.resolve():
                continue
        except (OSError, RuntimeError):
            continue
        hosts = [one for one in _hosts(path) if one not in kept]
        if hosts:
            found.append((path, hosts))
    return found


def detect_include() -> Edit:
    """~/.ssh/config の目印の次の行を、今の ssh ディレクトリを指す Include にする中身を
    求める。書くのは write_include である。

    目印が無ければ、目印とその行を先頭に足す。目印の次の行が既に同じなら、書き換え
    ない。目印の次が Include でなければ、その行は動かさず、間に足す。最初の Host か
    Match より後ろの目印は、そこでは Include がそのブロックの設定としてしか読まれない
    ので、無いものとして扱う。
    """
    path = user_ssh_dir() / "config"
    text = _read(path)
    lines = _lines(text)
    wanted = include_line()
    for index, line in enumerate(lines):
        if _keyword(line)[0] in ("host", "match"):
            break
        if line.strip() != MARK:
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if following.strip() == wanted:
            return Edit(path, text, text)
        if _keyword(following)[0] == "include":
            lines[index + 1] = wanted
            return Edit(path, text, _joined(lines), replaced=following.strip())
        lines.insert(index + 1, wanted)
        return Edit(path, text, _joined(lines))
    return Edit(path, text, _joined([MARK, wanted, *lines]))


def include_changes(edit: Edit, host: Edit) -> list[Change]:
    """detect_include の結果を、diff に出す形にする。

    張り替えで ssh から見えなくなる Host があれば、続く行に挙げる。apply は Host
    ブロックを書いてから Include を張り替えるので、新しい config の Host は、
    detect_host_block が求めた書いたあとの中身（host）から数える。
    """
    if not edit.changed:
        return []
    name, wanted = tilde(edit.path), include_line()
    if edit.replaced is None:
        return [Change(ADD, SSH_INCLUDE, name, wanted)]
    body = tuple(
        f"ssh cannot reach {', '.join(hosts)} in {tilde(path)}"
        for path, hosts in _dropped(edit.replaced, set(_host_names(host.after)))
    )
    return [Change(UPDATE, SSH_INCLUDE, name, f"{edit.replaced}{ARROW}{wanted}", body)]


def write_include(edit: Edit, host: Edit) -> bool:
    """detect_include が求めた Include の行を書く。書いたら True を返す。

    Include を張り替えるときは、前の行が指していたファイルに Host があれば、ssh から
    見えなくなることを知らせる。新しい config の Host は、include_changes と同じく、
    detect_host_block が求めた書いたあとの中身（host）から数える。~/.ssh/ が無ければ
    モード 700 で作る。確かめてから置き換えるまで、警告も含めて write_edit と同じ
    ロックを持つ。書かずに止まったのに、張り替えの警告だけが残ることはない。
    """
    if not edit.changed:
        return False
    with _edit_lock():
        _check_unchanged(edit)
        ssh = edit.path.parent
        if not ssh.exists():
            ssh.mkdir(mode=DIR_MODE)
        if edit.replaced is not None:
            for path, hosts in _dropped(edit.replaced, set(_host_names(host.after))):
                msg.warn(
                    f"{printable(tilde(edit.path))} no longer includes {printable(tilde(path))}; "
                    f"ssh cannot reach {printable(', '.join(hosts))} there"
                )
        _replace(edit)
    return True
