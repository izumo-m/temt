"""外部コマンドの確認と実行。"""

import os
import shutil
import subprocess
import threading
from contextlib import suppress
from typing import IO

from temt import msg
from temt.msg import TemtError
from temt.table import printable

# stream が子の標準出力を 1 回に読む大きさ
CHUNK = 65536


def require(command: str) -> str:
    """コマンドが PATH にあることを確かめ、その場所を返す。"""
    found = shutil.which(command)
    if found is None:
        raise TemtError(f"command not found: {command}")
    return found


def _one_line(text: str) -> str:
    """外部コマンドの出力を 1 行にする。

    ssh-keygen は、鍵のファイルの権限が広いと @ で囲んだ複数行の警告を出す。そのまま
    では警告の行や表示の列が崩れるので、囲みの @ を除き、行を空白でつなぐ。
    """
    lines = (line.strip().strip("@").strip() for line in text.splitlines())
    return " ".join(line for line in lines if line)


def run(argv: list[str], stdin: str | None = None) -> str:
    """コマンドを実行し、標準出力を返す。失敗すれば標準エラーを添えて終了する。

    標準出力を受け取るのは、ProxyCommand として動くときに外部コマンドの出力が
    ssh の通信路へ流れ出ないようにするためでもある。stdin を渡せば標準入力に書き、
    渡さなければ標準入力を閉じる。閉じるのは、問いかけを待って止まらないように
    するためである。
    """
    done = subprocess.run(
        argv,
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        detail = _one_line(done.stderr) or _one_line(done.stdout) or f"exit {done.returncode}"
        raise TemtError(f"{argv[0]}: {printable(detail)}")
    return done.stdout


def _feed(pipe: IO[bytes], data: bytes) -> None:
    """data を子の標準入力に書いて閉じる。子が先に閉じれば、残りは捨てる。"""
    try:
        pipe.write(data)
    except BrokenPipeError:
        pass
    finally:
        with suppress(BrokenPipeError):
            pipe.close()


def stream(argv: list[str], data: bytes | None = None) -> int:
    """コマンドを実行し、終了コードを返す。出力はそのまま利用者に見せる。

    標準出力は受け取って、msg.raw で標準出力に流す。標準出力の読み手（head など）が先に
    閉じても、子は止めずに最後まで動かす。install を途中で切らないためである。標準エラー
    出力はそのまま見せる。data を渡せば標準入力に書き、渡さなければ標準入力を閉じる。
    """
    msg.flush()
    with subprocess.Popen(
        argv, stdin=subprocess.DEVNULL if data is None else subprocess.PIPE, stdout=subprocess.PIPE
    ) as child:
        # 子の出力を読みながら書くよう、標準入力は別のスレッドで書く
        feeder = None
        if data is not None and child.stdin is not None:
            feeder = threading.Thread(target=_feed, args=(child.stdin, data), daemon=True)
            feeder.start()
        if child.stdout is not None:
            while chunk := os.read(child.stdout.fileno(), CHUNK):
                msg.raw(chunk)
        if feeder is not None:
            feeder.join()
        return child.wait()


def read(argv: list[str]) -> tuple[int, str]:
    """コマンドを実行し、終了コードと標準出力を返す。標準エラー出力はそのまま見せる。

    ssh の ProxyCommand が出す待ちの知らせや、ssh 自身の誤りを利用者に見せるため、
    標準エラー出力は受け取らない。標準入力は閉じる。
    """
    msg.flush()
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, check=False)
    return done.returncode, done.stdout.decode(errors="replace")
