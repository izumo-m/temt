"""sg list、sg add、sg remove の本体のテスト。設定ファイルだけを読み書きする。"""

import http.client
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from temt import conf, ingress
from temt.conf import Ingress
from temt.msg import TemtError, UsageError
from temt.names import Service, defaults_path

INGRESS = "[[security-group.ingress]]\n"
HOME = INGRESS + 'Port = 22\nCidr = "203.0.113.10/32"\nDescription = "home"\n'


def write(service: Service, text: str) -> None:
    """設定ファイルを用意する。"""
    service.conf_path.parent.mkdir(parents=True, exist_ok=True)
    service.conf_path.write_text(text, encoding="utf-8")


def answers(monkeypatch: pytest.MonkeyPatch, **found: str | Exception) -> list[str]:
    """グローバル IP アドレスの問い合わせに、用意した答えを返す。問い合わせた URL を控える。

    found のキーは v4 と v6。無いほうは、つながらないものとする。
    """
    asked: list[str] = []
    urls = {"v4": ingress.ADDRESS_SERVICES[0].url, "v6": ingress.ADDRESS_SERVICES[1].url}

    def fetch(url: str) -> str:
        asked.append(url)
        for key, known in urls.items():
            if url == known:
                answer = found.get(key, URLError(OSError(101, "Network is unreachable")))
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(url)

    monkeypatch.setattr(ingress, "fetch", fetch)
    return asked


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("22", ("tcp", (22, 22))),
        ("22/tcp", ("tcp", (22, 22))),
        ("8000-8010", ("tcp", (8000, 8010))),
        ("60000-61000/udp", ("udp", (60000, 61000))),
        ("0-65535", ("tcp", (0, 65535))),
        ("icmp", ("icmp", None)),
    ],
)
def test_parse_port(text: str, parsed: tuple[str, conf.RULE_PORT]) -> None:
    assert conf.parse_port(text) == parsed
    assert conf.port_text(*parsed) == (text if "/" in text or text == "icmp" else f"{text}/tcp")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("ssh", conf.PORT_RULE),
        ("22/icmp", conf.PORT_RULE),
        ("22/sctp", conf.PORT_RULE),
        ("22/TCP", conf.PORT_RULE),
        ("icmp/tcp", conf.PORT_RULE),
        ("-22", conf.PORT_RULE),
        ("65536", "must be a port range within 0-65535"),
        ("23-22", "must be a port range within 0-65535"),
    ],
)
def test_parse_port_rejected(text: str, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        _ = conf.parse_port(text)
    assert str(caught.value) == message


def test_list_shows_the_rules_in_order(
    service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        service,
        HOME
        + "\n"
        + INGRESS
        + 'Protocol = "udp"\nPort = [60000, 61000]\nCidr = "2001:db8::/64"\n'
        + "\n"
        + INGRESS
        + 'Protocol = "icmp"\nCidr = "203.0.113.10/32"\nDescription = "ping from home"\n',
    )
    ingress.list_rules(service, None)
    out, err = capsys.readouterr()
    assert err == ""
    assert out.splitlines() == [
        "PORT              CIDR              DESCRIPTION",
        "22/tcp            203.0.113.10/32   home",
        "60000-61000/udp   2001:db8::/64     -",
        "icmp              203.0.113.10/32   ping from home",
    ]


def test_list_without_rules_shows_the_header(
    service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    write(service, "")
    ingress.list_rules(service, "cidr,port")
    assert capsys.readouterr().out == "CIDR   PORT\n"


def test_list_rejects_an_unknown_column_first(service: Service) -> None:
    """設定ファイルが無くても、列の誤りを先に知らせる。"""
    with pytest.raises(UsageError, match="unknown column in --format: id"):
        ingress.list_rules(service, "id")


def test_list_without_a_config_file(service: Service) -> None:
    with pytest.raises(TemtError, match="no config file"):
        ingress.list_rules(service, None)


def test_add_a_rule(service: Service, capsys: pytest.CaptureFixture[str]) -> None:
    write(service, HOME)
    ingress.add(service, "udp", (53, 53), "198.51.100.0/24", "dns")
    assert conf.load(service).security_group.ingress[1] == Ingress(
        Protocol="udp", Port=(53, 53), Cidr="198.51.100.0/24", Description="dns"
    )
    out = capsys.readouterr().out
    assert "added 53/udp 198.51.100.0/24 to ~/.config/temt/dev-main.toml\n" in out
    assert "run 'temt apply --svc dev-main' to update the security group\n" in out


def test_add_a_rule_that_is_there(service: Service, capsys: pytest.CaptureFixture[str]) -> None:
    """説明を与えなければ、同じ規則には触らず、apply も促さない。"""
    write(service, HOME)
    ingress.add(service, "tcp", (22, 22), "203.0.113.10/32", None)
    assert service.conf_path.read_text(encoding="utf-8") == HOME
    out = capsys.readouterr().out
    assert "22/tcp 203.0.113.10/32 is already in ~/.config/temt/dev-main.toml\n" in out
    assert "apply" not in out


def test_add_rewrites_the_description(service: Service, capsys: pytest.CaptureFixture[str]) -> None:
    write(service, HOME)
    ingress.add(service, "tcp", (22, 22), "203.0.113.10/32", "office")
    assert conf.load(service).security_group.ingress[0].Description == "office"
    out = capsys.readouterr().out
    assert "updated the description of 22/tcp 203.0.113.10/32 in" in out
    assert "run 'temt apply --svc dev-main'" in out


def test_add_without_a_cidr_uses_both_egress_addresses(
    service: Service, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IPv4 は /32、IPv6 は /64 にする。"""
    write(service, "")
    asked = answers(monkeypatch, v4="198.51.100.7\n", v6="2001:db8:1:2:a:b:c:d\n")
    ingress.add(service, "icmp", None, None, "home")
    assert asked == [one.url for one in ingress.ADDRESS_SERVICES]
    assert conf.load(service).security_group.ingress == [
        Ingress(Protocol="icmp", Cidr="198.51.100.7/32", Description="home"),
        Ingress(Protocol="icmp", Cidr="2001:db8:1:2::/64", Description="home"),
    ]
    out = capsys.readouterr().out
    assert (
        "egress IPv4 address: 198.51.100.7 (https://checkip.amazonaws.com); using 198.51.100.7/32\n"
    ) in out
    assert "using 2001:db8:1:2::/64\n" in out


def test_an_egress_address_that_is_not_there(
    service: Service, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """取れなかった種類は、理由を知らせて飛ばす。違う種類のアドレスも使わない。"""
    write(service, "")
    _ = answers(monkeypatch, v4="198.51.100.7", v6="198.51.100.7")
    ingress.add(service, "tcp", (22, 22), None, None)
    assert [one.Cidr for one in conf.load(service).security_group.ingress] == ["198.51.100.7/32"]
    out = capsys.readouterr().out
    assert "no IPv6 egress address: https://api6.ipify.org answered 198.51.100.7\n" in out


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(URLError(OSError(101, "Network is unreachable")), id="unreachable"),
        pytest.param(TimeoutError("timed out"), id="timeout"),
        pytest.param("<html>\u3042", id="not an address"),
    ],
)
def test_no_egress_address_at_all(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str | Exception,
) -> None:
    write(service, "")
    _ = answers(monkeypatch, v4=failure)
    with pytest.raises(TemtError, match="could not find the egress IP address; give the CIDR"):
        ingress.add(service, "tcp", (22, 22), None, None)
    assert service.conf_path.read_text(encoding="utf-8") == ""
    out = capsys.readouterr().out
    assert "no IPv4 egress address: " in out
    assert out.isascii()


def test_remove_a_rule(service: Service, capsys: pytest.CaptureFixture[str]) -> None:
    write(service, HOME + "\n" + INGRESS + 'Port = 80\nCidr = "203.0.113.10/32"\n')
    ingress.remove(service, "tcp", (22, 22), "203.0.113.10/32")
    assert conf.load(service).security_group.ingress == [
        Ingress(Port=(80, 80), Cidr="203.0.113.10/32")
    ]
    out = capsys.readouterr().out
    assert "removed 22/tcp 203.0.113.10/32 from ~/.config/temt/dev-main.toml\n" in out
    assert "run 'temt apply --svc dev-main' to update the security group\n" in out


def test_remove_a_rule_that_is_not_there(service: Service) -> None:
    """プロトコルが違えば別の規則である。"""
    write(service, HOME)
    with pytest.raises(TemtError, match=r"no rule 22/udp 203\.0\.113\.10/32 in "):
        ingress.remove(service, "udp", (22, 22), "203.0.113.10/32")
    assert service.conf_path.read_text(encoding="utf-8") == HOME


def test_remove_without_a_cidr(
    service: Service, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """グローバル IP アドレスの規則を消す。片方だけ無ければ、それを知らせる。"""
    write(service, HOME)
    _ = answers(monkeypatch, v4="203.0.113.10", v6="2001:db8::1")
    ingress.remove(service, "tcp", (22, 22), None)
    assert conf.load(service).security_group.ingress == []
    assert "no rule 22/tcp 2001:db8::/64 in" in capsys.readouterr().out


def test_add_needs_the_config_file(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    """設定ファイルが無ければ、グローバル IP アドレスを調べる前に止まる。"""
    asked = answers(monkeypatch, v4="198.51.100.7")
    with pytest.raises(TemtError, match="no config file"):
        ingress.add(service, "tcp", (22, 22), None, None)
    assert asked == []
    assert not Path(service.conf_path).exists()


def test_add_with_an_empty_description_clears_it(service: Service) -> None:
    write(service, HOME)
    ingress.add(service, "tcp", (22, 22), "203.0.113.10/32", "")
    assert conf.load(service).security_group.ingress == [
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32")
    ]


def test_remove_an_icmp_rule(service: Service) -> None:
    write(service, HOME + "\n" + INGRESS + 'Protocol = "icmp"\nCidr = "203.0.113.10/32"\n')
    ingress.remove(service, "icmp", None, "203.0.113.10/32")
    assert service.conf_path.read_text(encoding="utf-8") == HOME


def test_remove_without_a_cidr_and_no_rule(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """グローバル IP アドレスの 2 つとも無ければ、両方を挙げて止まる。"""
    write(service, HOME)
    _ = answers(monkeypatch, v4="198.51.100.7", v6="2001:db8::1")
    with pytest.raises(TemtError) as caught:
        ingress.remove(service, "tcp", (22, 22), None)
    assert str(caught.value) == (
        "no rule 22/tcp 198.51.100.7/32 or 22/tcp 2001:db8::/64 in ~/.config/temt/dev-main.toml"
    )


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        pytest.param(
            HTTPError("https://checkip.amazonaws.com", 503, "Service Unavailable", Message(), None),
            "HTTP 503 Service Unavailable",
            id="http error",
        ),
        pytest.param(http.client.BadStatusLine("x"), "x", id="bad status line"),
        pytest.param(URLError("\u3042"), "\u3042", id="not ascii"),
        pytest.param(URLError("\x1b[31mBad\x07"), "?[31mBad?", id="control characters"),
    ],
)
def test_the_reason_of_a_failed_question(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    reason: str,
) -> None:
    write(service, "")
    _ = answers(monkeypatch, v4=failure, v6="2001:db8::1")
    ingress.add(service, "tcp", (22, 22), None, None)
    out = capsys.readouterr().out
    assert f"no IPv4 egress address: {reason} (https://checkip.amazonaws.com)\n" in out


def test_remove_stops_when_the_file_changed_while_asking(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """グローバル IP アドレスを調べている間にファイルが直されたら、書かずに止まる。"""
    write(service, HOME)
    changed = HOME.replace("home", "office")

    def fetch(url: str) -> str:
        write(service, changed)
        return "203.0.113.10" if "checkip" in url else "2001:db8::1"

    monkeypatch.setattr(ingress, "fetch", fetch)
    with pytest.raises(TemtError, match="changed while running"):
        ingress.remove(service, "tcp", (22, 22), None)
    assert service.conf_path.read_text(encoding="utf-8") == changed


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("fe80::1%\u3042\x1b[31m", id="zone id"),
        pytest.param("::1", id="loopback"),
        pytest.param("fe80::1", id="link local"),
        pytest.param("::", id="unspecified"),
        pytest.param("ff02::1", id="multicast"),
    ],
)
def test_an_address_that_cannot_be_a_rule(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
) -> None:
    """規則にできないアドレスは使わず、応答の中身も出さない。"""
    write(service, "")
    _ = answers(monkeypatch, v4="198.51.100.7", v6=answer)
    ingress.add(service, "tcp", (22, 22), None, None)
    assert [one.Cidr for one in conf.load(service).security_group.ingress] == ["198.51.100.7/32"]
    out = capsys.readouterr().out
    assert "no IPv6 egress address: https://api6.ipify.org did not answer an address\n" in out
    assert out.isascii()
    assert "\x1b" not in out


def write_defaults(text: str) -> None:
    """共通ファイルを用意する。"""
    defaults_path().parent.mkdir(parents=True, exist_ok=True)
    defaults_path().write_text(text, encoding="utf-8")


OFFICE = INGRESS + 'Port = 22\nCidr = "198.51.100.0/24"\nDescription = "office"\n'


def test_list_shows_the_common_rules_first(
    service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    write_defaults(OFFICE)
    write(service, HOME)
    ingress.list_rules(service, "cidr")
    assert capsys.readouterr().out.splitlines() == ["CIDR", "198.51.100.0/24", "203.0.113.10/32"]


def test_add_leaves_a_common_rule(service: Service, capsys: pytest.CaptureFixture[str]) -> None:
    write_defaults(OFFICE)
    write(service, HOME)
    ingress.add(service, "tcp", (22, 22), "198.51.100.0/24", None)
    assert service.conf_path.read_text(encoding="utf-8") == HOME
    out = capsys.readouterr().out
    assert "22/tcp 198.51.100.0/24 is already in ~/.config/temt/defaults.toml\n" in out
    assert "apply" not in out


def test_add_does_not_rewrite_the_description_of_a_common_rule(service: Service) -> None:
    write_defaults(OFFICE)
    write(service, HOME)
    with pytest.raises(TemtError) as caught:
        ingress.add(service, "tcp", (22, 22), "198.51.100.0/24", "branch")
    assert str(caught.value) == (
        "22/tcp 198.51.100.0/24 is in ~/.config/temt/defaults.toml; edit its Description there"
    )
    assert service.conf_path.read_text(encoding="utf-8") == HOME


def test_add_writes_only_the_rule_of_the_service(service: Service) -> None:
    """共通ファイルの規則は、サービスの設定ファイルに写さない。"""
    write_defaults(OFFICE)
    write(service, "")
    ingress.add(service, "tcp", (22, 22), "203.0.113.10/32", "home")
    assert conf.load(service, common=False).security_group.ingress == [
        Ingress(Port=(22, 22), Cidr="203.0.113.10/32", Description="home")
    ]
    assert [one.Cidr for one in conf.load(service).security_group.ingress] == [
        "198.51.100.0/24",
        "203.0.113.10/32",
    ]


def test_remove_refuses_a_common_rule(service: Service) -> None:
    """共通ファイルの規則を指せば、ほかの規則も消さずに止まる。"""
    write_defaults(OFFICE)
    write(service, HOME)
    with pytest.raises(TemtError) as caught:
        ingress.remove(service, "tcp", (22, 22), "198.51.100.0/24")
    assert str(caught.value) == (
        "22/tcp 198.51.100.0/24 is in ~/.config/temt/defaults.toml; remove it there"
    )
    assert service.conf_path.read_text(encoding="utf-8") == HOME


def test_remove_a_rule_beside_common_rules(service: Service) -> None:
    write_defaults(OFFICE)
    write(service, HOME)
    ingress.remove(service, "tcp", (22, 22), "203.0.113.10/32")
    assert conf.load(service, common=False).security_group.ingress == []


@pytest.mark.parametrize("command", ["add", "remove"])
def test_a_rule_in_both_files_stops_the_edit(service: Service, command: str) -> None:
    """共通ファイルと同じ規則を書いた設定ファイルは、ほかのコマンドと同じく読めずに止まる。"""
    write_defaults(OFFICE)
    write(service, OFFICE)
    with pytest.raises(TemtError, match="repeats a rule in .*; remove one of them$"):
        if command == "add":
            ingress.add(service, "tcp", (22, 22), "198.51.100.0/24", None)
        else:
            ingress.remove(service, "tcp", (22, 22), "198.51.100.0/24")
    assert service.conf_path.read_text(encoding="utf-8") == OFFICE
