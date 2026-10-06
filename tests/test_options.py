"""アプリのオプション（options）のテスト。"""

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from temt import options
from temt.msg import TemtError
from temt.options import Choice, Option

PORT = Option("port", "the port", default="8888", pattern=re.compile("[0-9]+"))
TOKEN = Option("token", "the token")
DECLARED = {"port": PORT, "token": TOKEN}
RECORD = "/temt-dev-main/jupyter/deploy"


def test_variable_is_the_name_in_upper_case() -> None:
    assert options.variable("idle-limit") == "TEMT_OPTION_IDLE_LIMIT"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("4h", True),
        ("こんにちは 世界", True),
        ("", True),
        ("a\nb", False),
        ("a\tb", False),
        ("a\x1bb", False),
        ("a\x7fb", False),
        ("\udcff", False),
    ],
)
def test_is_value(value: str, ok: bool) -> None:
    assert options.is_value(value) is ok


def test_env_file_survives_the_shell(tmp_path: Path) -> None:
    """引用符で囲んで書くので、bash が . で読み込んでも展開されず、値がそのまま戻る。"""
    values = {
        "a": "it's",
        "b": "$(touch pwned) `touch pwned` $HOME",
        "c": "こんにちは 世界",
        "d": "",
        "e": "-n",
        "f": '"; touch pwned; "',
    }
    file = tmp_path / options.FILE
    file.write_bytes(
        options.env_file("jupyter", "1.2", [Choice(k, v, options.GIVEN) for k, v in values.items()])
    )
    script = f'. "{file}"\n' + "".join(
        f'printf "%s\\0" "${options.variable(name)}"\n' for name in values
    )

    out = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, check=True
    ).stdout

    assert out.decode().split("\0")[:-1] == list(values.values())
    assert not (tmp_path / "pwned").exists()


def test_choose_takes_given_kept_and_default_values_in_the_declared_order() -> None:
    explicit, chosen = options.choose(
        "jupyter",
        "1.2",
        DECLARED,
        {"token": "t"},
        {"port": "9999", "token": "old"},
        record=RECORD,
    )

    assert explicit == {"port": "9999", "token": "t"}
    assert chosen == [
        Choice("port", "9999", options.KEPT),
        Choice("token", "t", options.GIVEN),
    ]


def test_choose_puts_a_name_back_to_the_default() -> None:
    explicit, chosen = options.choose(
        "jupyter", "1.2", DECLARED, {"port": None, "token": "t"}, {"port": "9999"}, record=RECORD
    )

    assert explicit == {"token": "t"}
    assert chosen[0] == Choice("port", "8888", options.DEFAULT)


def test_choose_stops_on_options_it_cannot_read() -> None:
    with pytest.raises(TemtError, match=f"cannot read the options in the deploy record {RECORD}"):
        _ = options.choose("jupyter", "1.2", DECLARED, {"token": "t"}, None, record=RECORD)


def test_choose_tells_how_to_replace_a_kept_value_without_a_default() -> None:
    strict = {"token": Option("token", "the token", pattern=re.compile("[a-z]+"))}
    with pytest.raises(
        TemtError,
        match=r"^the deploy record has token=T1, which jupyter 2 does not accept; give "
        r"token=<value>; nothing was changed$",
    ):
        _ = options.choose("jupyter", "2", strict, {}, {"token": "T1"}, record=RECORD)


def test_check_given_accepts_the_declared_values() -> None:
    options.check_given("jupyter", DECLARED, {"port": "80", "token": "x"})
    options.check_given("jupyter", DECLARED, {"port": None})


def test_env_file_keeps_the_version_in_the_comment(tmp_path: Path) -> None:
    """manifest.toml の版に改行があっても、コメントの外に出ない。"""
    text = options.env_file("jupyter", "1\ntouch pwned", [Choice("a", "b", options.GIVEN)])

    assert text.decode().splitlines() == [
        "# Written by temt deploy: the options of jupyter 1?touch pwned.",
        "TEMT_OPTION_A=b",
    ]


def test_show_hides_what_the_terminal_should_not_get() -> None:
    assert options.show("a", "x\u202ey") == "a='x?y'"
    assert options.show("a", "こんにちは") == "a='こんにちは'"


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # 全角空白、改行しない空白（NBSP）、ゼロ幅接合子でつないだ絵文字、まだ割り当てのない
        # 文字はそのまま
        ("a\u3000b", "a\u3000b"),
        ("a\u00a0b", "a\u00a0b"),
        (
            "\U0001f468\u200d\U0001f469\u200d\U0001f467",
            "\U0001f468\u200d\U0001f469\u200d\U0001f467",
        ),
        ("\u0378", "\u0378"),
        # 制御文字、対にならないサロゲート、行と段落の区切り、文字の向きに関わる制御文字
        ("a\x1b[2Jb", "a?[2Jb"),
        ("a\udcffb", "a?b"),
        ("a\u2028b\u2029c", "a?b?c"),
        ("\u202a\u202b\u202c\u202d\u202e", "?????"),
        ("\u2066\u2067\u2068\u2069", "????"),
        ("\u061c\u200e\u200f", "???"),
    ],
)
def test_visible_hides_only_what_disturbs_the_terminal(text: str, shown: str) -> None:
    assert options.visible(text) == shown


def test_show_can_be_typed_back() -> None:
    """表示は、そのままコマンドラインに打てる。"""
    value = "全角\u3000空白 と 'quote'"
    shown = options.show("greeting", value)
    name, _, quoted = shown.partition("=")
    assert name == "greeting"
    assert shlex.split(quoted) == [value]


def test_check_given_hides_what_disturbs_the_terminal_in_the_description() -> None:
    odd = {"port": Option("port", "the\u202e port", pattern=re.compile("[0-9]+"))}
    with pytest.raises(TemtError, match=r"^jupyter: bad value for port: x \(the\? port\)$"):
        options.check_given("jupyter", odd, {"port": "x"})


def test_a_default_that_does_not_match_is_shown_without_what_disturbs_the_terminal() -> None:
    with pytest.raises(TemtError, match=r"the default 'a\?b' does not match the pattern$"):
        _ = options.declarations(
            {"port": {"description": "p", "default": "a\u202eb", "pattern": "[0-9]+"}}, "m"
        )
