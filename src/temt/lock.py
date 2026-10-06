"""サービスごとのロック。start、stop、terminate、reboot、bake、destroy、deploy、app remove が
動く前に取る。ami prune は、AMI を 1 つ消すたびに、その AMI のサービスのロックを取る。

known_hosts のディレクトリに置くロックファイル .lock に排他ロックを掛ける。取れな
ければ、他のコマンドが動いていると告げて終了し、待たない。ロックはサービスごとな
ので、別のサービスの操作は並行できる。
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from typing import TYPE_CHECKING

from temt import home
from temt.msg import TemtError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from temt.names import Service

LOCK_NAME = ".lock"


@contextmanager
def _locked(path: Path) -> Generator[bool]:
    """ロックファイル path にロックを掛けてみて、取れたかを持たせる。待たない。

    他の OSError（ファイルシステムがロックを扱えないときなど）はそのまま上げる。
    """
    path.touch(mode=0o600, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            taken = False
        else:
            taken = True
        yield taken


@contextmanager
def hold(service: Service) -> Generator[None]:
    """ロックを取り、処理が終わるまで持つ。他が持っていれば終了する。"""
    with _locked(home.prepare_known_hosts(service) / LOCK_NAME) as taken:
        if not taken:
            raise TemtError(f"another temt command is running for {service.name}")
        yield


@contextmanager
def try_hold(service: Service) -> Generator[bool]:
    """ロックを取れれば取って持ち、続けてよいかを持たせる。他が持っていれば False。

    ami prune が使う。known_hosts のディレクトリが無ければ、このマシンではそのサービスの
    コマンドが動いていないので、ロックを取らずに True とする。ディレクトリは作らない。
    ami prune は他のマシンで焼いた AMI も扱うので、知らないサービスのディレクトリを
    増やさないためである。
    """
    directory = service.known_hosts_dir
    if not directory.is_dir():
        yield True
        return
    with _locked(directory / LOCK_NAME) as taken:
        yield taken
