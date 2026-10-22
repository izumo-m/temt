"""relay のテスト。標準入出力の代わりにパイプを、接続の代わりに socketpair を使う。

中継は終わらないことがあるので、別のスレッドで動かし、締め切りを置いて待つ。中継の送る
スレッドで上がった例外は、警告ではなく失敗にする。
"""

import os
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from temt import relay

# 中継やパイプの読み手が終わるのを待つ締め切り（秒）
JOIN_TIMEOUT = 10

pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")


@dataclass
class Running:
    """別のスレッドで動かしたもの。終わるまで待ち、上げた例外を控える。"""

    thread: threading.Thread
    raised: list[BaseException] = field(default_factory=list)

    def join(self) -> None:
        self.thread.join(JOIN_TIMEOUT)
        assert not self.thread.is_alive()


def in_thread(target: Callable[[], None]) -> Running:
    """target を別のスレッドで動かす。"""
    running = Running(threading.Thread())

    def body() -> None:
        try:
            target()
        except BaseException as e:
            running.raised.append(e)

    running.thread = threading.Thread(target=body, daemon=True)
    running.thread.start()
    return running


@dataclass
class Pipes:
    """標準入力と標準出力の代わり。stdout に書かれたものは、読み手が控える。"""

    stdin_read: int
    stdin_write: int
    stdout_read: int
    stdout_write: int
    received: bytearray = field(default_factory=bytearray)
    # 閉じた fd。同じ番号が他で使われていても、二度閉じないため
    closed: set[int] = field(default_factory=set)
    # 中継が作った、送るスレッドと、それを start したことの合図
    senders: list[threading.Thread] = field(default_factory=list)
    started: threading.Event = field(default_factory=threading.Event)

    def close(self, fd: int) -> None:
        if fd not in self.closed:
            self.closed.add(fd)
            os.close(fd)

    def join_senders(self) -> None:
        """中継の送るスレッドが終わるのを待つ。"""
        for thread in self.senders:
            thread.join(JOIN_TIMEOUT)
            assert not thread.is_alive()

    def drain(self) -> Running:
        """stdout に書かれたものを、書く側が閉じられるまで読む。"""

        def read() -> None:
            while data := os.read(self.stdout_read, relay.CHUNK):
                self.received.extend(data)

        return in_thread(read)


class Sender(threading.Thread):
    """中継の送るスレッド。start したら started を立てる。"""

    def __init__(self, target: Callable[[], None], daemon: bool, started: threading.Event) -> None:
        super().__init__(target=target, daemon=daemon)
        self.started = started

    def start(self) -> None:
        super().start()
        self.started.set()


@dataclass
class Threads:
    """relay が使う threading の代わり。中継が作ったスレッドを pipes に控える。"""

    pipes: Pipes

    def Thread(self, target: Callable[[], None], daemon: bool) -> threading.Thread:
        thread = Sender(target, daemon, self.pipes.started)
        self.pipes.senders.append(thread)
        return thread


@pytest.fixture
def pipes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Pipes]:
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    made = Pipes(stdin_read, stdin_write, stdout_read, stdout_write)
    monkeypatch.setattr(relay, "threading", Threads(made))
    yield made
    # 標準入力の書く側を閉じ、中継の送るスレッドが終わるのを待ってから、読む側を閉じる。
    # 残ったスレッドが、閉じた後に同じ番号を得た次のテストの fd を読まないためである
    made.close(stdin_write)
    made.join_senders()
    for fd in (stdout_write, stdin_read, stdout_read):
        made.close(fd)


@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    """中継する側と、インスタンスの側の接続。

    インスタンスの側の受け取りには締め切りを置き、中継が終わりを伝えなくても止まらない。
    """
    ours, theirs = socket.socketpair()
    theirs.settimeout(JOIN_TIMEOUT)
    yield ours, theirs
    ours.close()
    theirs.close()


def relaying(connection: socket.socket, pipes: Pipes) -> Running:
    return in_thread(lambda: relay.run(connection, pipes.stdin_read, pipes.stdout_write))


def receive_all(connection: socket.socket) -> bytes:
    """相手が送る側を閉じるまで受ける。"""
    received = bytearray()
    while data := connection.recv(relay.CHUNK):
        received.extend(data)
    return bytes(received)


def test_it_relays_both_ways_until_the_peer_closes(
    pipes: Pipes, pair: tuple[socket.socket, socket.socket]
) -> None:
    """標準入力が終わったら送る側だけを閉じ、相手が閉じるまで受け続ける。"""
    ours, theirs = pair
    reader = pipes.drain()
    running = relaying(ours, pipes)
    _ = os.write(pipes.stdin_write, b"from ssh")
    pipes.close(pipes.stdin_write)
    # 相手には、送ったものに続いて終わりが届く
    assert receive_all(theirs) == b"from ssh"
    # 送る側を閉じた後も、相手からは受ける
    theirs.sendall(b"from sshd")
    theirs.close()
    running.join()
    assert running.raised == []
    pipes.close(pipes.stdout_write)
    reader.join()
    assert bytes(pipes.received) == b"from sshd"


def test_it_returns_when_the_peer_closes_while_the_input_is_open(
    pipes: Pipes, pair: tuple[socket.socket, socket.socket]
) -> None:
    """相手が閉じたら、標準入力の終わりを待たずに中継をやめる。"""
    ours, theirs = pair
    reader = pipes.drain()
    running = relaying(ours, pipes)
    theirs.sendall(b"bye")
    theirs.close()
    running.join()
    assert running.raised == []
    pipes.close(pipes.stdout_write)
    reader.join()
    assert bytes(pipes.received) == b"bye"


def test_it_moves_much_data_both_ways_at_once(
    pipes: Pipes, pair: tuple[socket.socket, socket.socket]
) -> None:
    """両向きに、バッファに収まらない量を同時に流しても止まらない。"""
    ours, theirs = pair
    up = os.urandom(relay.CHUNK * 16 + 1)
    down = os.urandom(relay.CHUNK * 16 + 3)
    reader = pipes.drain()
    running = relaying(ours, pipes)

    def write_input() -> None:
        relay._write_all(pipes.stdin_write, up)
        pipes.close(pipes.stdin_write)

    writer = in_thread(write_input)
    sender = in_thread(lambda: theirs.sendall(down))
    assert receive_all(theirs) == up
    sender.join()
    theirs.close()
    running.join()
    writer.join()
    assert running.raised == writer.raised == sender.raised == []
    pipes.close(pipes.stdout_write)
    reader.join()
    assert bytes(pipes.received) == down


class Broken(socket.socket):
    """送ると、相手に切られた誤りになる接続。送る側を閉じると、つながっていない
    誤りになる。"""

    def sendall(self, data: object, flags: int = 0, /) -> None:
        _ = (data, flags)
        raise ConnectionResetError(104, "Connection reset by peer")

    def shutdown(self, how: int, /) -> None:
        _ = how
        raise OSError(107, "Transport endpoint is not connected")


@pytest.mark.parametrize("ended", [False, True], ids=["sending", "closing"])
def test_it_keeps_receiving_quietly_after_an_error_in_sending(
    pipes: Pipes, pair: tuple[socket.socket, socket.socket], ended: bool
) -> None:
    """送る向きで接続の誤りを受け取ったら、送るのをやめ、受ける向きは続ける。

    送るときの誤りも、標準入力が終わって送る側を閉じるときの誤りも、上げない。
    切れたことは ssh が知らせる。
    """
    ours, theirs = pair
    broken = Broken(fileno=ours.detach())
    reader = pipes.drain()
    running = relaying(broken, pipes)
    if ended:
        pipes.close(pipes.stdin_write)
    else:
        _ = os.write(pipes.stdin_write, b"from ssh")
    # 送る向きが誤りを受け取って終わってから、相手が送って閉じる
    assert pipes.started.wait(JOIN_TIMEOUT)
    pipes.join_senders()
    theirs.sendall(b"from sshd")
    theirs.close()
    running.join()
    broken.close()
    assert running.raised == []
    pipes.close(pipes.stdout_write)
    reader.join()
    assert bytes(pipes.received) == b"from sshd"


def test_it_ends_sending_when_the_input_cannot_be_read(
    pipes: Pipes, pair: tuple[socket.socket, socket.socket]
) -> None:
    """標準入力が読めなければ、ssh が先に終わっているので、終わりとみなす。

    送る側を閉じ、誤りは上げない。
    """
    ours, theirs = pair
    # 書く側の fd は読めない
    running = in_thread(lambda: relay.run(ours, pipes.stdout_write, pipes.stdin_write))
    assert receive_all(theirs) == b""
    theirs.close()
    running.join()
    assert running.raised == []


class Reset(socket.socket):
    """受けると、相手に切られた誤りになる接続。"""

    def recv(self, bufsize: int, flags: int = 0, /) -> bytes:
        _ = (bufsize, flags)
        raise ConnectionResetError(104, "Connection reset by peer")


def test_it_returns_quietly_when_the_connection_is_reset(
    pipes: Pipes, pair: tuple[socket.socket, socket.socket]
) -> None:
    """受ける向きで接続の誤りを受け取ったら、相手が閉じたのと同じく黙ってやめる。"""
    ours, _ = pair
    reset = Reset(fileno=ours.detach())
    running = relaying(reset, pipes)
    running.join()
    reset.close()
    assert running.raised == []


def test_it_returns_quietly_when_the_output_is_closed(
    pipes: Pipes, pair: tuple[socket.socket, socket.socket]
) -> None:
    """標準出力に書けなければ、ssh が先に終わっているので、誤りを上げずにやめる。"""
    ours, theirs = pair
    pipes.close(pipes.stdout_read)
    running = relaying(ours, pipes)
    theirs.sendall(b"from sshd")
    running.join()
    assert running.raised == []


def test_write_all_repeats_partial_writes(pipes: Pipes, monkeypatch: pytest.MonkeyPatch) -> None:
    """os.write が一部しか書かなくても、書き切るまでくり返す。"""
    write = os.write
    sizes: list[int] = []

    def one_byte(fd: int, data: bytes | memoryview) -> int:
        sizes.append(len(data))
        return write(fd, data[:1])

    monkeypatch.setattr(os, "write", one_byte)
    relay._write_all(pipes.stdout_write, b"abc")
    monkeypatch.undo()
    assert sizes == [3, 2, 1]
    assert os.read(pipes.stdout_read, 3) == b"abc"
