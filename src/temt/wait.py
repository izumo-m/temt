"""締め切りを置いた待ち合わせ。

boto3 の waiter を使わず、知らせる文言と締め切りを自分で持つ。時計と休みは module の
属性なので、テストから差し替えられる。インスタンスの状態も、destroy のセキュリティ
グループも、同じ時計と間隔で待つ。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from temt.msg import TemtError

if TYPE_CHECKING:
    from collections.abc import Callable

# 問い合わせの間隔（秒）
INTERVAL = 5

# 待ち合わせの時計と休み。テストから差し替える
clock = time.monotonic
sleep = time.sleep


def until[T](
    seconds: int, stage: str, look: Callable[[], T | None], *, hint: str | None = None
) -> T:
    """look が値を返すまで、締め切りまで繰り返す。

    締め切りを過ぎたら、どの段階が終わらなかったかを述べて終了する。hint があれば、
    その後ろに手がかりとして添える。
    """
    end = clock() + seconds
    while True:
        found = look()
        if found is not None:
            return found
        if clock() >= end:
            minutes = seconds // 60
            unit = "minute" if minutes == 1 else "minutes"
            text = f"timed out after {minutes} {unit} waiting for {stage}"
            raise TemtError(f"{text}; {hint}" if hint else text)
        sleep(INTERVAL)
