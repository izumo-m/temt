"""名前の検査と、名前から導く名前・パスのテスト。"""

from collections.abc import Callable
from pathlib import Path

import pytest

from temt import names
from temt.msg import TemtError, UsageError
from temt.names import Service


@pytest.mark.parametrize("svc", ["a", "dev", "web2", "a-b-c", "x9-9y", "dev-main"])
def test_service_name_accepted(svc: str) -> None:
    assert Service(svc).svc == svc


@pytest.mark.parametrize("svc", ["", "A", "Dev", "1a", "-a", "a-", "a_b", "a b", "a.b"])
def test_service_name_rejected(svc: str) -> None:
    with pytest.raises(UsageError):
        Service(svc)


def test_name_of_64_characters_accepted() -> None:
    # temt- で 5 文字を使う
    assert len(Service("a" * 59).name) == names.NAME_MAX


def test_longer_name_rejected() -> None:
    with pytest.raises(UsageError):
        Service("a" * 60)


def test_derived_names() -> None:
    service = Service("dev-main")
    assert service.name == "temt-dev-main"
    assert service.launch_template == "temt-dev-main-template"
    assert service.parameter_path == "/temt-dev-main"


def test_derived_paths(home_dir: Path) -> None:
    service = Service("dev-main")
    assert service.conf_path == home_dir / ".config/temt/dev-main.toml"
    assert service.known_hosts_dir == home_dir / ".ssh/temt/known_hosts.d/temt-dev-main"
    assert names.ssh_dir() == home_dir / ".ssh/temt"
    assert names.keys_dir() == home_dir / ".ssh/temt/keys"


def test_tilde(home_dir: Path) -> None:
    assert names.tilde(home_dir / ".ssh" / "config") == "~/.ssh/config"
    assert names.tilde(Path("/etc/ssh/ssh_config")) == "/etc/ssh/ssh_config"


def test_the_conf_dir_moves_only_the_config_files(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(names.CONF_DIR_ENV, "/srv/temt/conf")
    service = Service("dev-main")
    assert service.conf_path == Path("/srv/temt/conf/dev-main.toml")
    assert names.ssh_dir() == home_dir / ".ssh/temt"


def test_the_ssh_dir_moves_the_rest(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(names.SSH_DIR_ENV, "/srv/temt/ssh")
    service = Service("dev-main")
    assert service.conf_path == home_dir / ".config/temt/dev-main.toml"
    assert names.ssh_dir() == Path("/srv/temt/ssh")
    assert names.keys_dir() == Path("/srv/temt/ssh/keys")
    assert service.known_hosts_dir == Path("/srv/temt/ssh/known_hosts.d/temt-dev-main")


PLACES = [
    pytest.param(names.CONF_DIR_ENV, names.conf_dir, id="conf"),
    pytest.param(names.SSH_DIR_ENV, names.ssh_dir, id="ssh"),
]


@pytest.mark.parametrize(("env", "place"), PLACES)
def test_an_empty_value_uses_the_default(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, env: str, place: Callable[[], Path]
) -> None:
    before = place()
    monkeypatch.setenv(env, "")
    assert place() == before
    assert place().is_relative_to(home_dir)


@pytest.mark.parametrize(("env", "place"), PLACES)
@pytest.mark.parametrize(
    ("value", "relative"),
    [("~/elsewhere/conf", "elsewhere/conf"), ("~//elsewhere", "elsewhere"), ("~", ".")],
)
def test_a_leading_tilde_expands(
    home_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
    place: Callable[[], Path],
    value: str,
    relative: str,
) -> None:
    monkeypatch.setenv(env, value)
    assert place() == (home_dir / relative if relative != "." else home_dir)


@pytest.mark.parametrize(("env", "place"), PLACES)
def test_a_trailing_slash_is_dropped(
    monkeypatch: pytest.MonkeyPatch, env: str, place: Callable[[], Path]
) -> None:
    monkeypatch.setenv(env, "/srv/temt/place/")
    assert place() == Path("/srv/temt/place")


@pytest.mark.parametrize(("env", "place"), PLACES)
@pytest.mark.parametrize("value", ["temt", "./temt", "../temt", "~user/temt"])
def test_a_relative_value_rejected(
    monkeypatch: pytest.MonkeyPatch, env: str, place: Callable[[], Path], value: str
) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(TemtError, match=f"{env} must be an absolute path"):
        place()


@pytest.mark.parametrize(("env", "place"), PLACES)
@pytest.mark.parametrize(
    "value",
    [
        "/srv/a b",
        "/srv/a\tb",
        "/srv/a\nb",
        "/srv/a'b",
        '/srv/a"b',
        "/srv/a\\b",
        "/srv/a%b",
        "/srv/a$b",
        "/srv/a\x1bb",
        # UTF-8 として読めないバイト。Python は対にならないサロゲートとして読む
        "/srv/a\udcffb",
    ],
)
def test_a_value_ssh_cannot_hold_rejected(
    monkeypatch: pytest.MonkeyPatch, env: str, place: Callable[[], Path], value: str
) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(TemtError, match=f"{env} must not contain"):
        place()


@pytest.mark.parametrize(("env", "place"), PLACES)
@pytest.mark.parametrize(
    ("value", "rule"),
    [
        (
            "/srv/\x1b[2J a",
            "must not contain whitespace, a quote, a backslash, a percent sign, a dollar sign, "
            "or an unprintable character",
        ),
    ],
)
def test_a_rejected_value_is_shown_safely(
    monkeypatch: pytest.MonkeyPatch, env: str, place: Callable[[], Path], value: str, rule: str
) -> None:
    """利用者が設定した値は、表示できない文字を ? にして誤りに入れる。"""
    monkeypatch.setenv(env, value)
    with pytest.raises(TemtError) as caught:
        place()
    assert str(caught.value) == f"{env} {rule}: {value.replace(chr(0x1B), '?')}"


def test_a_service_name_with_a_control_character_is_shown_safely() -> None:
    with pytest.raises(UsageError) as caught:
        Service("\x1b[2J")
    assert str(caught.value) == "invalid service name: ?[2J"


@pytest.mark.parametrize("relative", [".ssh", ".ssh/", ".ssh/x/.."])
def test_the_ssh_dir_must_not_be_the_users_own(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    """~/.ssh そのものを指すと、temt の config が ~/.ssh/config になってしまう。"""
    (home_dir / ".ssh" / "x").mkdir(parents=True)
    monkeypatch.setenv(names.SSH_DIR_ENV, f"~/{relative}")
    with pytest.raises(TemtError, match=f"{names.SSH_DIR_ENV} must not be ~/.ssh itself"):
        names.ssh_dir()


def test_the_ssh_dir_must_not_reach_the_users_own_through_a_symlink(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home_dir / ".ssh").mkdir()
    (home_dir / "link").symlink_to(home_dir / ".ssh")
    monkeypatch.setenv(names.SSH_DIR_ENV, str(home_dir / "link"))
    with pytest.raises(TemtError, match="must not be ~/.ssh itself"):
        names.ssh_dir()


def test_places_lists_both_even_at_their_defaults(home_dir: Path) -> None:
    places = names.places()
    assert places == {
        names.CONF_DIR_ENV: home_dir / ".config/temt",
        names.SSH_DIR_ENV: home_dir / ".ssh/temt",
    }
    # ProxyCommand に並べる順は conf、ssh
    assert list(places) == [names.CONF_DIR_ENV, names.SSH_DIR_ENV]


def test_places_follows_the_environment(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(names.SSH_DIR_ENV, "~/elsewhere/ssh")
    monkeypatch.setenv(names.CONF_DIR_ENV, "~/elsewhere/conf")
    assert names.places() == {
        names.CONF_DIR_ENV: home_dir / "elsewhere/conf",
        names.SSH_DIR_ENV: home_dir / "elsewhere/ssh",
    }


def test_a_symlink_loop_in_the_ssh_dir_is_an_error(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve は、Python 3.12 ではリンクの循環で RuntimeError を上げ、3.13 からは上げずに
    解けたところまでを返す。どの版でも終了する。"""
    (home_dir / "loop").symlink_to(home_dir / "loop")
    monkeypatch.setenv(names.SSH_DIR_ENV, str(home_dir / "loop" / "temt"))
    with pytest.raises(TemtError, match="^symlink loop in "):
        names.ssh_dir()


def test_resolved_finds_a_loop_after_a_missing_part(home_dir: Path) -> None:
    """strict では無い要素で止まり、循環まで辿らないパスも、どの版でも終了する。"""
    (home_dir / "loop").symlink_to(home_dir / "loop")
    with pytest.raises(TemtError, match="^symlink loop in "):
        names.resolved(home_dir / "missing" / ".." / "loop")


def test_resolved_takes_a_path_that_does_not_exist(home_dir: Path) -> None:
    """無いパスは、循環とせず、解けるところまで解く。"""
    (home_dir / "real").mkdir()
    (home_dir / "link").symlink_to(home_dir / "real")
    expected = (home_dir / "real").resolve() / "none" / "x"
    assert names.resolved(home_dir / "link" / "none" / "x") == expected


def test_app_parameters_sit_under_the_parameter_path() -> None:
    service = Service("dev-main")
    assert service.app_path("jupyter") == "/temt-dev-main/jupyter"
    assert service.app_parameter("jupyter", "token") == "/temt-dev-main/jupyter/token"


@pytest.mark.parametrize("name", ["a", "idle-shutdown", "app2", "a" * names.LABEL_MAX])
def test_labels_that_are_taken(name: str) -> None:
    assert names.is_label(name)


@pytest.mark.parametrize(
    "name", ["", "2app", "-a", "a-", "App", "a_b", "a/b", "a.b", "a" * (names.LABEL_MAX + 1)]
)
def test_labels_that_are_not_taken(name: str) -> None:
    assert not names.is_label(name)


def test_app_ids_lists_directories_named_by_the_rule(tmp_path: Path) -> None:
    for name in ("web", "idle-shutdown", "Bad", "__pycache__"):
        (tmp_path / name).mkdir()
    (tmp_path / "file").touch()
    (tmp_path / "linked").symlink_to(tmp_path / "web")
    (tmp_path / "broken").symlink_to(tmp_path / "nothing")
    assert names.app_ids(tmp_path) == ["idle-shutdown", "linked", "web"]
    assert names.app_ids(tmp_path / "nothing") == []


@pytest.mark.parametrize("name", ["ubuntu", "_x", "a-b", "a_1", "a" * 32])
def test_user_name_accepted(name: str) -> None:
    assert names.is_user_name(name)


@pytest.mark.parametrize("name", ["", "a" * 33, "1a", "-a", "Ubuntu", "a$", "a b", "a.b"])
def test_user_name_rejected(name: str) -> None:
    assert not names.is_user_name(name)
