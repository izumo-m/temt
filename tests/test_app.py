"""app show、get、set、unset のテスト。AWS は Stubber で、ssh は偽物に差し替える。"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import Stubs, a_manifest, a_record, an_instance, commanded, reservations

from temt import app, names, resources
from temt.msg import TemtError
from temt.names import Service

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
TEMPLATES = {"LaunchTemplateNames": [TEMPLATE]}
PATH = f"/{NAME}"
TOKEN = f"{PATH}/jupyter/token"
WHEN = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")
# サービスのインスタンスと、ほかのインスタンス
INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
SEARCH = {
    "Filters": [
        {"Name": "tag:temt:service", "Values": ["dev-main"]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
        },
    ]
}


def applied(stubs: Stubs) -> None:
    """どのコマンドも最初に確かめる起動テンプレートがある。"""
    stubs.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateName": TEMPLATE}]},
        TEMPLATES,
    )


def a_parameter(name: str, value: str, kind: str = "String") -> dict[str, Any]:
    """get-parameter が返すパラメータ 1 つ。"""
    return {"Name": name, "Type": kind, "Value": value, "Version": 1}


def not_found(stubs: Stubs, method: str, **params: Any) -> None:
    stubs.ssm.add_client_error(
        method, service_error_code=app.NOT_FOUND, http_status_code=400, expected_params=params
    )


def told(err: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in err.splitlines()]


@pytest.mark.parametrize(
    "run",
    [
        lambda service: app.show(service, "jupyter"),
        lambda service: app.get(service, "jupyter", "token"),
        lambda service: app.set_value(service, "jupyter", "token", "x"),
        lambda service: app.unset(service, "jupyter", "token"),
    ],
)
def test_every_command_asks_for_apply_first(
    aws_stubs: Stubs, configured: Service, run: Any
) -> None:
    """起動テンプレートが無ければ、SSM を読みもせず apply を促す。"""
    aws_stubs.ec2.add_client_error(
        "describe_launch_templates",
        service_error_code=resources.NO_SUCH_TEMPLATE,
        http_status_code=400,
        expected_params=TEMPLATES,
    )

    with pytest.raises(
        TemtError, match=f"no launch template {TEMPLATE}; run 'temt apply --svc dev-main' first"
    ):
        run(configured)

    aws_stubs.done()


# --- app show ---


def keys(stubs: Stubs, *found: str) -> None:
    """describe-parameters が返す、アプリの階層のすぐ下のパラメータ。"""
    stubs.ssm.add_response(
        "describe_parameters",
        {"Parameters": [{"Name": name, "LastModifiedDate": WHEN} for name in found]},
        {
            "ParameterFilters": [
                {"Key": "Path", "Option": "OneLevel", "Values": [f"{PATH}/jupyter"]}
            ]
        },
    )


def found(
    stubs: Stubs,
    state: str = "running",
    apps: dict[str, dict[str, str | bytes]] | None = None,
    **more: Any,
) -> None:
    """記録を読むのに探す、サービスのインスタンス。state が空なら 1 台も無い。

    running なら、Run Command でインスタンスの上のアプリ apps を読む。more は commanded に渡す。
    """
    items = [an_instance(INSTANCE, state)] if state else []
    stubs.ec2.add_response("describe_instances", reservations(*items), SEARCH)
    if state == "running":
        commanded(stubs, INSTANCE, apps or {}, **more)


def shown(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """app show の表示の行。つながる先を確かめる知らせは除く。"""
    return [line for line in capsys.readouterr().out.splitlines() if "checking that" not in line]


OPTIONS_MANIFEST = """\
name = "Jupyter Lab"
version = "1.3"

[options.port]
default = "8888"
description = "the port to listen on"
pattern = '[0-9]+'

[options.token]
description = "the token to log in with"
"""


def local_app(manifest: str = OPTIONS_MANIFEST) -> Path:
    """conf ディレクトリの apps/ に置いた、オプションを宣言するアプリ。"""
    place = names.user_apps_dir() / "jupyter"
    place.mkdir(parents=True)
    (place / "manifest.toml").write_text(manifest, encoding="utf-8")
    return place


def test_show_prints_the_record_and_the_declarations(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """記録はインスタンスから読み、オプションは打ち直せる形で出す。宣言は、手元の
    アプリより、インスタンスの manifest.toml から出す。キーは、前の temt の記録 deploy も
    含めて挙げ、値は出さない。
    """
    local_app('name = "J"\nversion = "9"\n')
    apps: dict[str, dict[str, str | bytes]] = {
        "jupyter": {
            "manifest.toml": OPTIONS_MANIFEST,
            "temt-deploy.json": a_record(
                INSTANCE, options={"port": "9999", "token": "こんにちは 世界"}
            ),
        }
    }
    applied(aws_stubs)
    keys(aws_stubs, f"{PATH}/jupyter/deploy", TOKEN, f"{PATH}/jupyter/URL")
    found(aws_stubs, apps=apps)

    app.show(configured, "jupyter")

    aws_stubs.done()
    assert shown(capsys) == [
        f"app jupyter on {INSTANCE}:",
        "  name:     Jupyter Lab",
        "  version:  1.3",
        "  user:     root",
        "  deployed: 2026-09-27 12:04:05",
        "  baked:    no",
        "  options:  port=9999 token='こんにちは 世界'",
        f"options in {INSTANCE}:/opt/temt/apps/jupyter (version 1.3):",
        "  port   8888        the port to listen on",
        "  token  (required)  the token to log in with",
        "keys:",
        # 規則に外れたキーも、書かれたまま挙げる
        "  URL     2026-09-26 12:00:00",
        "  deploy  2026-09-26 12:00:00",
        "  token   2026-09-26 12:00:00",
    ]


def test_show_tells_an_app_from_the_ami_as_baked(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """記録の instance がほかのインスタンスなら、AMI から来たので焼いてある。"""
    apps: dict[str, dict[str, str | bytes]] = {
        "jupyter": {"manifest.toml": a_manifest("1.2"), "temt-deploy.json": a_record(OTHER)}
    }
    applied(aws_stubs)
    keys(aws_stubs)
    found(aws_stubs, apps=apps)

    app.show(configured, "jupyter")

    assert "  baked:    yes" in shown(capsys)


def test_show_of_an_app_without_a_record(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """前の temt が入れたアプリには記録が無いので、manifest.toml から分かることだけを出す。"""
    apps: dict[str, dict[str, str | bytes]] = {"jupyter": {"manifest.toml": a_manifest("1.2")}}
    applied(aws_stubs)
    keys(aws_stubs)
    found(aws_stubs, apps=apps)

    app.show(configured, "jupyter")

    assert shown(capsys) == [
        f"app jupyter on {INSTANCE}:",
        "  name:     Jupyter Lab",
        "  version:  1.2",
        "  user:     -",
        "  deployed: -",
        "  baked:    -",
        "  options:  -",
        f"options in {INSTANCE}:/opt/temt/apps/jupyter (version 1.2): none",
        "keys: none",
    ]


def test_show_tells_an_app_that_is_not_deployed(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """外したあとに残った値も見られる。宣言は手元のアプリから出す。"""
    local_app()
    applied(aws_stubs)
    keys(aws_stubs, TOKEN)
    found(aws_stubs)

    app.show(configured, "jupyter")

    assert shown(capsys) == [
        f"app jupyter: not deployed on {INSTANCE}",
        "options in ~/.config/temt/apps/jupyter (version 1.3):",
        "  port   8888        the port to listen on",
        "  token  (required)  the token to log in with",
        "keys:",
        "  token  2026-09-26 12:00:00",
    ]


@pytest.mark.parametrize(
    ("state", "why"), [("stopped", f"{INSTANCE} is stopped"), ("", "no instance")]
)
def test_show_without_a_running_instance(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
    state: str,
    why: str,
) -> None:
    """動いていなければ、つながずに、記録を読めない理由を出して続ける。"""
    applied(aws_stubs)
    keys(aws_stubs, TOKEN)
    found(aws_stubs, state)

    app.show(configured, "jupyter")

    assert shown(capsys) == [
        f"app jupyter: deploy record not read ({why})",
        "keys:",
        "  token  2026-09-26 12:00:00",
    ]


def test_show_goes_on_when_the_command_fails(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local_app('name = "J"\nversion = "1"\n')
    applied(aws_stubs)
    keys(aws_stubs)
    found(aws_stubs, status="Failed")

    app.show(configured, "jupyter")

    captured = capsys.readouterr()
    assert told(captured.out) == [
        f"app jupyter: deploy record not read (cannot read {INSTANCE})",
        "options in ~/.config/temt/apps/jupyter (version 1): none",
        "keys: none",
    ]
    assert captured.err == f"temt: warning: cannot read the apps on {INSTANCE}: Failed\n"


@pytest.mark.parametrize("state", ["running", "stopped"])
def test_show_stops_when_there_is_nothing(
    aws_stubs: Stubs, configured: Service, state: str
) -> None:
    """インスタンスにも無く、値も手元のアプリも無ければ、アプリ ID の打ち間違いとみなす。"""
    applied(aws_stubs)
    keys(aws_stubs)
    found(aws_stubs, state)
    where = f"not on {INSTANCE}" if state == "running" else f"{INSTANCE} is stopped"

    with pytest.raises(TemtError) as caught:
        app.show(configured, "jupyter")

    assert str(caught.value) == (
        f"no app jupyter in {NAME}: {where}, and nothing under {PATH}/jupyter/"
    )


def test_show_lists_the_options_of_the_app_that_comes_with_temt(
    aws_stubs: Stubs,
    configured: Service,
    no_bundled_apps: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """apps/ に無くても、temt と一緒に配るアプリなら、その宣言を出す。"""
    bundled = no_bundled_apps / "jupyter"
    bundled.mkdir(parents=True)
    (bundled / "manifest.toml").write_text('name = "J"\nversion = "2"\n', encoding="utf-8")
    applied(aws_stubs)
    keys(aws_stubs)
    found(aws_stubs)

    app.show(configured, "jupyter")

    assert shown(capsys) == [
        f"app jupyter: not deployed on {INSTANCE}",
        "options in ~/bundled/jupyter (version 2): none",
        "keys: none",
    ]


def test_show_warns_about_a_local_manifest_it_cannot_read(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local_app('name = "J"\n')
    applied(aws_stubs)
    keys(aws_stubs)
    found(aws_stubs)

    app.show(configured, "jupyter")

    captured = capsys.readouterr()
    assert told(captured.out) == [f"app jupyter: not deployed on {INSTANCE}", "keys: none"]
    assert captured.err.splitlines() == [
        "temt: warning: cannot read the manifest of jupyter: "
        "~/.config/temt/apps/jupyter/manifest.toml: version is missing",
    ]


def test_show_hides_control_characters_from_the_instance(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """記録とキーはインスタンスから書けるので、制御文字を端末に届けない。全角空白は ? に
    せず、そのまま打てる形で出す。読めないオプションは (unreadable)。キーは、すべての
    ページを読む。
    """
    apps: dict[str, dict[str, str | bytes]] = {
        "jupyter": {
            "manifest.toml": a_manifest("1\\u001b[2J"),
            "temt-deploy.json": a_record(INSTANCE, user="r\x1bx", options={"port": 1}),
        }
    }
    applied(aws_stubs)
    where = {
        "ParameterFilters": [{"Key": "Path", "Option": "OneLevel", "Values": [f"{PATH}/jupyter"]}]
    }
    aws_stubs.ssm.add_response(
        "describe_parameters",
        {"Parameters": [{"Name": TOKEN, "LastModifiedDate": WHEN}], "NextToken": "t"},
        where,
    )
    aws_stubs.ssm.add_response(
        "describe_parameters",
        {"Parameters": [{"Name": f"{PATH}/jupyter/a\tb", "LastModifiedDate": WHEN}]},
        {**where, "NextToken": "t"},
    )
    found(aws_stubs, apps=apps)

    app.show(configured, "jupyter")

    aws_stubs.done()
    lines = shown(capsys)
    assert "  version:  1?[2J" in lines
    assert "  user:     r?x" in lines
    assert "  options:  (unreadable)" in lines
    assert lines[-2:] == ["  a?b    2026-09-26 12:00:00", "  token  2026-09-26 12:00:00"]


def test_show_keeps_wide_spaces_in_the_options(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apps: dict[str, dict[str, str | bytes]] = {
        "jupyter": {
            "manifest.toml": a_manifest("1"),
            "temt-deploy.json": a_record(INSTANCE, options={"greeting": "こんにちは\u3000世界"}),
        }
    }
    applied(aws_stubs)
    keys(aws_stubs)
    found(aws_stubs, apps=apps)

    app.show(configured, "jupyter")

    assert "  options:  greeting='こんにちは\u3000世界'" in shown(capsys)


def test_show_hides_what_disturbs_the_terminal_in_the_declarations(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """manifest.toml の版、既定、説明も、端末を乱す文字だけを ? にする。"""
    local_app(
        'name = "J"\nversion = "1\\u001b[2J"\n\n[options.greeting]\n'
        'default = "a\\u202eb\\u3000c"\ndescription = "the\\u2066 greeting"\n'
    )
    applied(aws_stubs)
    keys(aws_stubs)
    found(aws_stubs)

    app.show(configured, "jupyter")

    aws_stubs.done()
    assert shown(capsys)[1:3] == [
        "options in ~/.config/temt/apps/jupyter (version 1?[2J):",
        "  greeting  'a?b\u3000c'  the? greeting",
    ]


# --- app get、set、unset ---


@pytest.mark.parametrize("value", ["abc", "line 1\nline 2\n", "\u3042 -x"])
def test_get_writes_the_value_as_it_is(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """改行を足さず、改行や ASCII 以外の文字もそのまま UTF-8 で書く。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(TOKEN, value)}, {"Name": TOKEN}
    )

    app.get(configured, "jupyter", "token")

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out == value
    assert captured.err == ""


def test_get_stops_when_there_is_no_value(aws_stubs: Stubs, configured: Service) -> None:
    applied(aws_stubs)
    not_found(aws_stubs, "get_parameter", Name=TOKEN)

    with pytest.raises(TemtError, match=f"no value {TOKEN}$"):
        app.get(configured, "jupyter", "token")


def test_get_does_not_read_a_secure_string(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """復号しないで読むと暗号文が返るので、それを値として出さない。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter",
        {"Parameter": a_parameter(TOKEN, "AQICAHg...", "SecureString")},
        {"Name": TOKEN},
    )

    with pytest.raises(
        TemtError, match=f"{TOKEN} is a SecureString; temt does not read SecureString values"
    ):
        app.get(configured, "jupyter", "token")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("version", "state"), [(1, "created"), (3, "updated")])
def test_set_writes_a_string(
    aws_stubs: Stubs,
    configured: Service,
    capsys: pytest.CaptureFixture[str],
    version: int,
    state: str,
) -> None:
    """アプリが入っているかは確かめずに書く。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "put_parameter",
        {"Version": version},
        {"Name": TOKEN, "Value": "a b\n", "Type": "String", "Overwrite": True},
    )

    app.set_value(configured, "jupyter", "token", "a b\n")

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"parameter {TOKEN}: {state}"]


def test_set_stops_when_the_value_has_another_type(aws_stubs: Stubs, configured: Service) -> None:
    """階層の下のパラメータは、上書きで型を変えられない。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_client_error(
        "put_parameter", service_error_code=app.TYPE_MISMATCH, http_status_code=400
    )

    with pytest.raises(
        TemtError,
        match=f"{TOKEN} is not a String; run 'temt app unset --svc dev-main jupyter token' first",
    ):
        app.set_value(configured, "jupyter", "token", "x")


def test_set_leaves_other_errors_from_writing_to_the_caller(
    aws_stubs: Stubs, configured: Service
) -> None:
    """値が大きすぎるときなどの誤りは、cli が包む。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_client_error(
        "put_parameter", service_error_code="ValidationException", http_status_code=400
    )

    with pytest.raises(ClientError):
        app.set_value(configured, "jupyter", "token", "x")


def test_unset_deletes_without_asking(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """アプリが入っていなくても消せる。外したあとに残った値を片づけるためである。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": TOKEN})

    app.unset(configured, "jupyter", "token")

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"parameter {TOKEN}: deleted"]


def test_unset_of_a_missing_value_is_not_an_error(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    not_found(aws_stubs, "delete_parameter", Name=TOKEN)

    app.unset(configured, "jupyter", "token")

    assert told(capsys.readouterr().out) == [f"parameter {TOKEN}: absent"]


def test_other_errors_are_left_to_the_caller(aws_stubs: Stubs, configured: Service) -> None:
    """ParameterNotFound 以外の誤りは、cli が包む。"""
    for run, method in (
        (lambda: app.set_value(configured, "jupyter", "token", "x"), "put_parameter"),
        (lambda: app.unset(configured, "jupyter", "token"), "delete_parameter"),
        (lambda: app.show(configured, "jupyter"), "describe_parameters"),
        (lambda: app.get(configured, "jupyter", "token"), "get_parameter"),
    ):
        applied(aws_stubs)
        aws_stubs.ssm.add_client_error(
            method, service_error_code="AccessDeniedException", http_status_code=400
        )
        with pytest.raises(ClientError):
            run()
    aws_stubs.done()
