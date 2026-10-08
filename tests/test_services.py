"""service list の本体のテスト。手元の設定ファイルだけを読む。"""

import os
from pathlib import Path

import pytest

from temt import names, services
from temt.msg import TemtError, UsageError
from temt.names import Service, conf_dir

HEADER = "SERVICE    PROFILE   REGION           IMAGE                   TYPE"


def written(svc: str, text: str) -> Path:
    """設定ファイルを書く。"""
    path = Service(svc).conf_path if names.SVC_RE.fullmatch(svc) else conf_dir() / f"{svc}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_list_shows_every_config_file(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """サービス名の順に並べる。既定のある項目は既定を、値の無い項目は - を出す。"""
    _ = home_dir
    written(
        "web",
        'AwsProfile = "work"\n'
        'AwsRegion = "us-east-1"\n'
        "[launch-template]\n"
        'ImageId = "ami-0123456789abcdef0"\n'
        'InstanceType = "t3.small"\n',
    )
    written("dev-main", "")
    (conf_dir() / "notes.txt").write_text("", encoding="utf-8")

    services.list_services(None, None)

    out, err = capsys.readouterr()
    assert err == ""
    assert out.splitlines() == [
        HEADER,
        "dev-main   -         ap-northeast-1   -                       t3.micro",
        "web        work      us-east-1        ami-0123456789abcdef0   t3.small",
    ]


def test_list_shows_the_chosen_columns(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ = home_dir
    written(
        "web",
        "[launch-template]\n"
        'SubnetId = "subnet-0123456789abcdef0"\n'
        "[[security-group.ingress]]\n"
        "Port = 22\n"
        'Cidr = "203.0.113.10/32"\n'
        "[[security-group.ingress]]\n"
        "Port = 443\n"
        'Cidr = "203.0.113.10/32"\n',
    )

    services.list_services(None, "service,subnet,ingress")

    assert capsys.readouterr().out.splitlines() == [
        "SERVICE   SUBNET                     INGRESS",
        "web       subnet-0123456789abcdef0   2",
    ]


def test_list_shows_the_sshd_ports(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """SshdPorts を書かれた順に , でつなぐ。書かれていなければ既定の 22。"""
    _ = home_dir
    written("web", "SshdPorts = [2222, 443]\n")
    written("dev-main", "")

    services.list_services(None, "service,sshdports")

    assert capsys.readouterr().out.splitlines() == [
        "SERVICE    SSHDPORTS",
        "dev-main   22",
        "web        2222,443",
    ]


def test_list_shows_the_volume_size(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """VolumeSize を出す。書かれていなければ -。"""
    _ = home_dir
    written("web", "[launch-template]\nVolumeSize = 30\n")
    written("dev-main", "")

    services.list_services(None, "service,volumesize")

    assert capsys.readouterr().out.splitlines() == [
        "SERVICE    VOLUMESIZE",
        "dev-main   -",
        "web        30",
    ]


def test_list_without_config_files(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """conf ディレクトリが無ければ、見出しだけを出す。"""
    _ = home_dir
    services.list_services(None, "SERVICE")
    assert capsys.readouterr().out == "SERVICE\n"


def test_list_skips_what_it_cannot_read(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """読めないファイルと規則に合わない名前は、警告して飛ばし、残りを並べてから止まる。"""
    _ = home_dir
    written("web", "")
    broken = written("broken", "AwsRegion = [\n")
    odd = written("Odd", "")

    with pytest.raises(TemtError, match="skipped 2 of 3 config files"):
        services.list_services(None, "SERVICE")

    out, err = capsys.readouterr()
    assert out.splitlines() == ["SERVICE", "web"]
    warnings = err.splitlines()
    assert warnings[0] == f"temt: warning: skipped: {odd}: invalid service name: Odd"
    assert warnings[1].startswith(f"temt: warning: skipped: {broken}: ")
    assert len(warnings) == 2


def test_list_checks_the_columns_first(home_dir: Path) -> None:
    _ = home_dir
    written("broken", "AwsRegion = [\n")
    with pytest.raises(UsageError, match="unknown column in --format: account"):
        services.list_services(None, "account")


def test_list_follows_the_service_names(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """ファイル名でなくサービス名の順。- は . より前なので、ファイル名の順では崩れる。"""
    _ = home_dir
    for svc in ("web-2", "dev-main", "web", "dev"):
        written(svc, "")
    services.list_services(None, "SERVICE")
    assert capsys.readouterr().out.splitlines() == ["SERVICE", "dev", "dev-main", "web", "web-2"]


def test_list_ignores_hidden_files(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """. で始まるファイル（エディタのロックファイルなど）は、警告せずに飛ばす。"""
    _ = home_dir
    written("web", "")
    (conf_dir() / ".#web.toml").symlink_to("nowhere")
    (conf_dir() / ".toml").write_text("", encoding="utf-8")
    services.list_services(None, "SERVICE")
    out, err = capsys.readouterr()
    assert (out.splitlines(), err) == (["SERVICE", "web"], "")


def test_list_skips_a_file_that_is_not_utf8(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home_dir
    written("web", "")
    bad = conf_dir() / "bad.toml"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(TemtError, match="skipped 1 of 2 config files"):
        services.list_services(None, "SERVICE")
    out, err = capsys.readouterr()
    assert out.splitlines() == ["SERVICE", "web"]
    assert err.startswith(f"temt: warning: skipped: {bad}: not valid UTF-8: ")


def test_list_skips_a_directory(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ = home_dir
    written("web", "")
    odd = conf_dir() / "odd.toml"
    odd.mkdir()
    with pytest.raises(TemtError, match="skipped 1 of 2 config files"):
        services.list_services(None, "SERVICE")
    out, err = capsys.readouterr()
    assert out.splitlines() == ["SERVICE", "web"]
    assert err.startswith("temt: warning: skipped: ")
    assert str(odd) in err


def test_list_skips_a_broken_symlink(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """行き先の無いリンクには、apply を促さず、リンクが切れていると告げる。"""
    _ = home_dir
    written("web", "")
    broken = conf_dir() / "gone.toml"
    broken.symlink_to(conf_dir() / "nowhere.toml")
    with pytest.raises(TemtError, match="skipped 1 of 2 config files"):
        services.list_services(None, "SERVICE")
    out, err = capsys.readouterr()
    assert out.splitlines() == ["SERVICE", "web"]
    assert err == f"temt: warning: skipped: {broken}: broken symlink\n"


def test_list_with_a_conf_dir_that_is_a_file(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """conf ディレクトリを読めなければ、飛ばさずに止まる。main が OSError を知らせる。"""
    place = home_dir / "conf"
    place.write_text("", encoding="utf-8")
    monkeypatch.setenv(names.CONF_DIR_ENV, str(place))
    with pytest.raises(NotADirectoryError):
        services.list_services(None, None)


def test_list_skips_a_symlink_loop(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """調べられないファイルがあっても、全体を止めずに飛ばし、残りを並べる。"""
    _ = home_dir
    written("web", "")
    (conf_dir() / "loop.toml").symlink_to("loop.toml")
    with pytest.raises(TemtError, match="skipped 1 of 2 config files"):
        services.list_services(None, "SERVICE")
    out, err = capsys.readouterr()
    assert out.splitlines() == ["SERVICE", "web"]
    assert err.startswith("temt: warning: skipped: cannot check ~/.config/temt/loop.toml: ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_list_skips_an_unreadable_file(home_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ = home_dir
    written("web", "")
    locked = written("locked", "")
    locked.chmod(0)
    with pytest.raises(TemtError, match="skipped 1 of 2 config files"):
        services.list_services(None, "SERVICE")
    out, err = capsys.readouterr()
    assert out.splitlines() == ["SERVICE", "web"]
    assert err == f"temt: warning: skipped: {locked}: Permission denied\n"


def test_list_of_a_service_shows_only_its_row(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--svc を付ければ、その設定ファイルだけを読む。他の読めないファイルでは止まらない。"""
    _ = home_dir
    written("web", "")
    written("dev-main", "")
    written("Odd", "")
    services.list_services(Service("web"), "SERVICE")
    out, err = capsys.readouterr()
    assert (out, err) == ("SERVICE\nweb\n", "")


def test_list_of_a_service_without_its_config_file(home_dir: Path) -> None:
    _ = home_dir
    written("dev-main", "")
    with pytest.raises(TemtError, match="no config file: .*; run 'temt apply --svc web' first"):
        services.list_services(Service("web"), None)
