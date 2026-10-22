"""diff が出す変更の形のテスト。"""

import pytest

from temt import change
from temt.change import Change


def test_write_aligns_the_mark_the_kind_and_the_name(capsys: pytest.CaptureFixture[str]) -> None:
    """違いは揃えず、無ければ書かない。行末に空白を残さない。"""
    change.write(
        [
            Change("+", "role", "temt-web"),
            Change("~", "launch-template", "temt-web-template", "InstanceType: a -> b"),
        ]
    )
    assert capsys.readouterr().out == (
        "+   role              temt-web\n"
        "~   launch-template   temt-web-template   InstanceType: a -> b\n"
    )


def test_write_puts_the_body_under_its_line(capsys: pytest.CaptureFixture[str]) -> None:
    """続く行は字下げし、揃える幅には含めない。空の前後の行も字下げだけは残す。"""
    change.write(
        [
            Change("~", "role-policy", "temt-app", body=("@@ -1 +1 @@", "-a", "+b", " ")),
            Change("-", "role", "x"),
        ]
    )
    assert capsys.readouterr().out == (
        "~   role-policy   temt-app\n    @@ -1 +1 @@\n    -a\n    +b\n    \n-   role          x\n"
    )


def test_write_expands_the_tabs_in_the_body(capsys: pytest.CaptureFixture[str]) -> None:
    """利用者が Host ブロックにタブで書いた行も、? にせず空白で出す。"""
    change.write([Change("~", "ssh-host", "temt-web", body=(" \tLocalForward 8888",))])
    assert capsys.readouterr().out == "~   ssh-host   temt-web\n            LocalForward 8888\n"


def test_write_writes_nothing_without_changes(capsys: pytest.CaptureFixture[str]) -> None:
    change.write([])
    assert capsys.readouterr().out == ""


def test_write_hides_characters_that_cannot_be_shown(capsys: pytest.CaptureFixture[str]) -> None:
    """AWS やファイルから来る値の制御文字を、端末に届けない。"""
    change.write([Change("~", "conf", "a\tb", "x\x1by", ("p\nq",))])
    assert capsys.readouterr().out == "~   conf   a?b   x?y\n    p?q\n"


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (None, "-"),
        ("t3.micro", "t3.micro"),
        (22, "22"),
        (True, "true"),
        (["sg-1", "sg-2"], '["sg-1","sg-2"]'),
        ({"b": 1, "a": 2}, '{"a":2,"b":1}'),
    ],
)
def test_shown(value: object, shown: str) -> None:
    assert change.shown(value) == shown


def test_moved_and_tags() -> None:
    assert change.moved(None, "temt-web") == "- -> temt-web"
    assert change.tags([("Name", "temt-web"), ("temt:service", "web")]) == (
        "tags: Name=temt-web, temt:service=web"
    )


def test_lines_diff_leaves_out_the_file_headers() -> None:
    """unified diff の --- と +++ は付けず、前後 2 行を添える。"""
    before = ["a", "b", "c", "d", "e", "f"]
    after = ["a", "b", "c", "D", "e", "f"]
    assert change.lines_diff(before, after) == (
        "@@ -2,5 +2,5 @@",
        " b",
        " c",
        "-d",
        "+D",
        " e",
        " f",
    )


def test_lines_diff_keeps_lines_that_look_like_the_headers() -> None:
    """-- や ++ で始まる中身の行は、消す行と足す行で --- や +++ で始まるが、見出しではない。"""
    assert change.lines_diff(["a", "--x", "b"], ["a", "b", "++y"]) == (
        "@@ -1,3 +1,3 @@",
        " a",
        "---x",
        " b",
        "+++y",
    )


def test_lines_diff_is_empty_when_nothing_differs() -> None:
    assert change.lines_diff(["a"], ["a"]) == ()


def test_json_lines_sorts_the_keys_and_indents() -> None:
    assert change.json_lines({"b": [1], "a": True}) == [
        "{",
        '  "a": true,',
        '  "b": [',
        "    1",
        "  ]",
        "}",
    ]
    assert change.json_lines(None) == []
