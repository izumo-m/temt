"""TCP の接続と標準入出力のあいだで、バイト列を中継する。proxy の direct の経路が使う。

標準入力から読んだものを接続へ、接続から受けたものを標準出力へ流す。送る向きは
別のスレッドが受け持つ。どちらの向きも、相手が読まないあいだは書き込みで止まるだけで、
もう一方の向きを止めない。
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading

# 標準入出力の fd。Python のバッファを通さず、fd を直に読み書きする
STDIN_FD = 0
STDOUT_FD = 1
# 1 回に読む量（バイト）
CHUNK = 64 * 1024


def _write_all(fd: int, data: bytes) -> None:
    """data を fd へ書き切る。os.write は一部しか書かないことがある。"""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _read(fd: int) -> bytes:
    """標準入力から読む。読めなければ、ssh が先に終わっているので、終わりとみなす。"""
    try:
        return os.read(fd, CHUNK)
    except OSError:
        return b""


def run(connection: socket.socket, stdin: int = STDIN_FD, stdout: int = STDOUT_FD) -> None:
    """接続が閉じられるまで中継する。

    標準入力が終わったら、接続の送る側だけを閉じ、相手が閉じるまで受け続ける。
    標準入力が読めないときも、ssh が先に終わっているので、終わりとみなす。送る向きで
    接続の誤りを受け取ったら、送るのをやめ、受ける向きは続ける。

    相手が閉じたら、標準入力の終わりを待たずに中継をやめる。受ける向きで接続の誤りを
    受け取ったときも、相手が閉じたのと同じに扱い、黙ってやめる。切れたことは ssh が
    利用者に知らせる。標準出力に書けないときは、ssh が先に終わっているので、黙ってやめる。
    """

    def send() -> None:
        with contextlib.suppress(OSError):
            while data := _read(stdin):
                connection.sendall(data)
            connection.shutdown(socket.SHUT_WR)

    # やめるときに、標準入力を読んで止まっているスレッドを待たない
    threading.Thread(target=send, daemon=True).start()
    with contextlib.suppress(OSError):
        while data := connection.recv(CHUNK):
            _write_all(stdout, data)
