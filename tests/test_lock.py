"""サービスごとのロックのテスト。"""

import errno
import fcntl
from typing import Any

import pytest
from conftest import deadline, taken

from temt import home, lock
from temt.msg import TemtError
from temt.names import Service


def test_it_holds_the_lock_until_the_end(service: Service) -> None:
    with lock.hold(service):
        assert not taken(service)
    assert taken(service)


def test_it_stops_without_waiting_when_another_holds_it(service: Service) -> None:
    with (
        lock.hold(service),
        deadline(5.0),
        pytest.raises(TemtError, match=f"^another temt command is running for {service.name}$"),
        lock.hold(service),
    ):
        pass


def test_another_failure_is_not_taken_for_another_command(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ファイルシステムがロックを扱えないときなどは、他のコマンドのせいにせず、そのまま上げる。"""

    def unsupported(*args: Any) -> None:
        _ = args
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    with pytest.raises(OSError, match="No locks available"), lock.hold(service):
        pass


def test_try_hold_holds_the_lock_when_it_is_free(service: Service) -> None:
    home.prepare_known_hosts(service)
    with lock.try_hold(service) as held:
        assert held
        assert not taken(service)
    assert taken(service)


def test_try_hold_does_not_wait_when_another_holds_it(service: Service) -> None:
    with lock.hold(service), deadline(5.0), lock.try_hold(service) as held:
        assert not held


def test_try_hold_goes_on_without_the_known_hosts_directory(service: Service) -> None:
    """ディレクトリが無ければ、ロックを取らずに続けてよいとし、ディレクトリも作らない。"""
    with lock.try_hold(service) as held:
        assert held
    assert not service.known_hosts_dir.exists()
