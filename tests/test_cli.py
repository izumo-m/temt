"""引数の解釈と終了コードのテスト。"""

import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
    ProfileNotFound,
)

from temt import (
    app,
    apply,
    cli,
    conf,
    deploy,
    home,
    images,
    ingress,
    manifest,
    services,
    sg,
    status,
    terminate,
)
from temt.msg import UsageError
from temt.names import INSTANCE_ID_RULE, LABEL_RULE, USER_NAME_RULE, Service, defaults_path

SVC = ["--svc", "dev-main"]


def positional(command: str) -> list[str]:
    """動詞の後ろに付ける位置引数。AMI、アプリ ID、キー、値、受信規則のポートを取るものには、
    それを付ける。
    """
    if command in cli.IMAGE_COMMANDS:
        return ["temt"]
    if command in cli.TARGET_COMMANDS:
        return ["jupyter"]
    if command in cli.RULE_COMMANDS:
        return ["22"]
    if command == cli.APP_SET:
        return ["jupyter", "token=abc"]
    return ["jupyter", "token"][: len(cli.APP_ARGS.get(command, ()))]


def needs_service(command: str) -> bool:
    """--svc が要るコマンドか。"""
    return command not in cli.OPTIONAL_SERVICE_COMMANDS


def service_args(command: str) -> list[str]:
    """--svc が要るコマンドにだけ付ける --svc。"""
    return SVC if needs_service(command) else []


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (["service", "apply", *SVC], cli.SERVICE_APPLY),
        (["service", "diff", *SVC], cli.SERVICE_DIFF),
        (["service", "destroy", *SVC], cli.SERVICE_DESTROY),
        (["instance", "start", *SVC], cli.INSTANCE_START),
        (["instance", "stop", *SVC], cli.INSTANCE_STOP),
        (["instance", "terminate", *SVC], cli.INSTANCE_TERMINATE),
        (["instance", "reboot", *SVC], cli.INSTANCE_REBOOT),
        (["instance", "show", *SVC], cli.INSTANCE_SHOW),
        (["ami", "bake", *SVC], cli.AMI_BAKE),
        (["template", "show", *SVC], cli.TEMPLATE_SHOW),
        (["ssh", "proxy", *SVC], cli.SSH_PROXY),
    ],
)
def test_command_form(args: list[str], command: str) -> None:
    request = cli.parse(args)
    assert request.command == command
    assert request.service == Service("dev-main")


@pytest.mark.parametrize("command", cli.COMMANDS)
def test_every_command_has_a_form(command: str) -> None:
    """表の漏れを見つける。どのコマンドも <対象> <動詞> で打てる。

    --svc が要るか、省けるかで、打ち方が決まる。どのコマンドも --svc を付けられる。
    """
    args = [*command.split(), *positional(command)]
    if command in cli.OPTIONAL_SERVICE_COMMANDS:
        assert cli.parse(args).service is None
    else:
        with pytest.raises(UsageError, match=f"{command}: missing --svc"):
            cli.parse(args)
    assert cli.parse([*args, *SVC]).service == Service("dev-main")


def test_the_commands_that_may_leave_out_the_service_are_commands() -> None:
    assert set(cli.OPTIONAL_SERVICE_COMMANDS) <= set(cli.COMMANDS)


def test_short_forms_and_targets_do_not_overlap() -> None:
    """短縮形を先に見るので、名前が重なると対象が隠れる。"""
    assert not set(cli.SHORT_FORMS) & set(cli.TARGETS)
    assert set(cli.SHORT_FORMS.values()) <= set(cli.COMMANDS)


# 短縮形ごとに、取る引数を付けて比べる
SHORT_FORM_ARGS = {
    "apply": [],
    "diff": [],
    "destroy": ["--yes"],
    "start": ["--async"],
    "stop": ["--async", "--yes"],
    "terminate": ["i-0123456789abcdef0", "--async", "--yes"],
    "reboot": ["--async", "--yes"],
    "status": [],
    "bake": ["--yes"],
    "deploy": ["./jupyter", "--bake", "--yes"],
    "proxy": ["2222"],
}


@pytest.mark.parametrize(("short", "command"), cli.SHORT_FORMS.items())
def test_short_form(short: str, command: str) -> None:
    """短縮形は、コマンドラインオプションの位置にかかわらず、表すコマンドと同じ要求になる。"""
    args = SHORT_FORM_ARGS[short]
    expected = cli.parse(["--profile", "work", *command.split(), *SVC, *args])
    assert expected.command == command
    assert cli.parse(["--profile", "work", short, *SVC, *args]) == expected
    assert cli.parse(["--profile", "work", short, *args, *SVC]) == expected
    assert cli.parse(["--profile", "work", *command.split(), *args, *SVC]) == expected


def test_short_form_args_cover_every_short_form() -> None:
    assert set(SHORT_FORM_ARGS) == set(cli.SHORT_FORMS)


@pytest.mark.parametrize(
    "args",
    [
        (["--svc", "dev-main", "sg", "add", "443", "--description", "home"]),
        (["sg", "--svc", "dev-main", "add", "443", "--description", "home"]),
        (["sg", "add", "--description", "home", "443", "--svc", "dev-main"]),
        (["sg", "add", "443", "--svc", "dev-main", "--description", "home"]),
        (["--description", "home", "sg", "add", "443", *SVC]),
    ],
)
def test_options_go_anywhere(args: list[str]) -> None:
    """コマンドラインオプションは、プログラム名より後ろのどこに置いてもよい。"""
    assert cli.parse(args) == cli.parse(["sg", "add", *SVC, "443", "--description", "home"])


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["instance"], "instance: missing verb"),
        (["instance", ""], "instance: missing verb"),
        (["instance", *SVC], "instance: missing verb"),
        (["template", *SVC], "template: missing verb"),
        (["instance", "pause", *SVC], "instance: unknown verb: pause"),
        # 別の対象の動詞
        (["template", "apply", *SVC], "template: unknown verb: apply"),
        # 動詞は対象の直後に置く
        (["instance", "dev-main", "start"], "instance: unknown verb: dev-main"),
        (["instance", "start"], "instance start: missing --svc"),
        # サービス名は位置引数では取らない
        (["instance", "start", "dev-main"], "instance start: missing --svc"),
        (
            ["instance", "start", "dev-main", *SVC],
            "instance start: unexpected argument: dev-main",
        ),
        # 枠としては決まっているが、まだ実装していない対象
        (["role", "show", *SVC], "unknown command: role"),
        # keypair という対象は無い
        (["keypair", "list"], "unknown command: keypair"),
        (["keypair", "list", *SVC], "unknown command: keypair"),
        # トップレベルの show は無い
        (["show", "template", *SVC], "unknown command: show"),
        (["show", "instance", *SVC], "unknown command: show"),
        # 短縮形は 1 語で、次の語は位置引数になる
        (["start", "instance", *SVC], "start: unexpected argument: instance"),
        # 誤りは打たれたとおりの名前で知らせる
        (["template", "show", *SVC, "x"], "template show: unexpected argument: x"),
        (["instance", "start", *SVC, "--yes"], "instance start: unexpected argument"),
        (["ssh", "proxy", "0", *SVC], "ssh proxy: port must be a number"),
    ],
)
def test_target_and_verb_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value).startswith(message)


@pytest.mark.parametrize("args", [["status", *SVC], ["instance", "show", *SVC]])
def test_main_runs_instance_show(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    """status と instance show は、同じ表示を通す。"""
    seen: list[Service] = []

    def remember(service: Service) -> None:
        seen.append(service)

    monkeypatch.setattr(status, "run", remember)
    assert cli.main(["temt", *args]) == 0
    assert seen == [Service("dev-main")]


def test_no_profile_option() -> None:
    assert cli.parse(["apply", *SVC]).profile is None


@pytest.mark.parametrize(
    "args",
    [
        (["--profile", "work", "apply", *SVC]),
        (["apply", *SVC, "--profile", "work"]),
        (["apply", "--profile", "work", *SVC]),
    ],
)
def test_profile_option_goes_anywhere(args: list[str]) -> None:
    request = cli.parse(args)
    assert request.profile == "work"
    assert request.command == cli.SERVICE_APPLY
    assert request.service == Service("dev-main")


def test_profile_option_is_for_any_command() -> None:
    request = cli.parse(["--profile", "work", "terminate", "--yes", *SVC])
    assert (request.profile, request.command, request.yes) == ("work", cli.INSTANCE_TERMINATE, True)
    request = cli.parse(["ssh", "proxy", "2222", "--profile", "work", *SVC])
    assert (request.profile, request.command, request.port) == ("work", cli.SSH_PROXY, 2222)


@pytest.mark.parametrize(
    "args",
    [["--profile"], ["--profile", ""], ["--profile", "", "apply"], ["apply", "--profile"]],
)
def test_profile_option_without_a_value_rejected(args: list[str]) -> None:
    with pytest.raises(UsageError, match="missing value for --profile"):
        cli.parse([*SVC, *args])


def test_main_passes_the_profile_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """--profile は、設定ファイルの AwsProfile より優先される。"""
    seen: list[str | None] = []

    def remember(service: Service, *, yes: bool) -> None:
        _ = service
        seen.append(conf.OPTION_PROFILE)

    monkeypatch.setattr(apply, "run", remember)
    assert cli.main(["temt", "--profile", "work", "apply", *SVC]) == 0
    assert seen == ["work"]


@pytest.mark.parametrize("command", cli.ASYNC_COMMANDS)
def test_async_option(command: str) -> None:
    assert cli.parse([*command.split(), *SVC]).is_async is False
    assert cli.parse([*command.split(), *SVC, "--async"]).is_async is True
    assert cli.parse(["--async", *command.split(), *SVC]).is_async is True


def test_options_come_in_any_order() -> None:
    for options in (["--async", "--yes"], ["--yes", "--async"]):
        request = cli.parse(["terminate", *SVC, *options])
        assert (request.is_async, request.yes) == (True, True)
        request = cli.parse([*options, "terminate", *SVC])
        assert (request.is_async, request.yes) == (True, True)


@pytest.mark.parametrize("command", cli.YES_COMMANDS)
def test_yes_option(command: str) -> None:
    args = [*command.split(), *service_args(command), *positional(command)]
    assert cli.parse([*args, "--yes"]).yes is True
    assert cli.parse(args).yes is False


def test_yes_is_not_for_a_command_that_does_not_ask() -> None:
    with pytest.raises(UsageError, match="start: unexpected argument: --yes"):
        cli.parse(["start", *SVC, "--yes"])


@pytest.mark.parametrize(
    ("args", "option"),
    [
        (["terminate", "--async", "--async"], "--async"),
        (["terminate", "--yes", "--yes"], "--yes"),
        (["--yes", "terminate", "--yes"], "--yes"),
    ],
)
def test_a_repeated_option_is_rejected(args: list[str], option: str) -> None:
    with pytest.raises(UsageError, match=f"repeated argument: {option}"):
        cli.parse([*args, *SVC])


def test_a_repeated_service_is_rejected() -> None:
    with pytest.raises(UsageError, match="repeated argument: --svc"):
        cli.parse(["start", *SVC, "--svc", "web"])


@pytest.mark.parametrize("command", cli.PORT_COMMANDS)
def test_port(command: str) -> None:
    assert cli.parse([*command.split(), *SVC]).port == 22
    assert cli.parse([*command.split(), *SVC, "2222"]).port == 2222
    assert cli.parse([*command.split(), "2222", *SVC]).port == 2222
    assert cli.parse([*command.split(), "1", *SVC]).port == 1
    assert cli.parse([*command.split(), "65535", *SVC]).port == 65535


# ２２ は str.isdigit が真になるが、ポート番号としては受け付けない
@pytest.mark.parametrize("port", ["0", "65536", "999999", "22x", "", " 22", "２２"])
def test_port_rejected(port: str) -> None:
    with pytest.raises(
        UsageError, match=f"^proxy: port must be a number from 1 to 65535: {re.escape(port)}$"
    ):
        cli.parse(["proxy", port, *SVC])


def test_a_negative_port_is_read_as_an_option() -> None:
    """- で始まる位置引数は無いので、-1 はポートの誤りでなく、
    知らないコマンドラインオプションになる。
    """
    with pytest.raises(UsageError, match="^proxy: unknown option: -1$"):
        cli.parse(["proxy", "-1", *SVC])


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "missing command"),
        (SVC, "missing command"),
        (["apply"], "apply: missing --svc"),
        (["apply", "--svc"], "apply: missing value for --svc"),
        (["apply", "--svc", ""], "apply: missing value for --svc"),
        # コマンドが読めなくても、コマンドラインオプションの誤りは知らせる
        (["--svc"], "missing value for --svc"),
        (["pause", "--now"], "unknown option: --now"),
        (["pause", *SVC], "unknown command: pause"),
        (["apply", "--svc", "Dev-main"], "invalid service name: Dev-main"),
        (["apply", "--svc", "dev-"], "invalid service name: dev-"),
        (["apply", "--svc", "a" * 60], "name longer than 64 characters: "),
        # サービス名を位置引数に書いた
        (["apply", "dev-main"], "apply: missing --svc"),
        (["apply", *SVC, "extra"], "apply: unexpected argument: extra"),
        (["status", *SVC, "extra"], "status: unexpected argument: extra"),
        (["start", "--now", *SVC], "start: unknown option: --now"),
        (["start", "-", *SVC], "start: unknown option: -"),
        # 1 文字のコマンドラインオプションは無い
        (["start", "-s", "dev-main"], "start: unknown option: -s"),
        (["start", "--async", "extra", *SVC], "start: unexpected argument: extra"),
        (["proxy", "22", "extra", *SVC], "proxy: unexpected argument: extra"),
        # bake はつねに完了を待つ
        (["bake", "--async", *SVC], "bake: unexpected argument: --async"),
        # 値を取るコマンドラインオプションは、- で始まる語も値とする
        (["apply", "--svc", "--yes"], "invalid service name: --yes"),
    ],
)
def test_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value).startswith(message)


def test_a_value_is_the_next_word_even_if_it_starts_with_a_dash() -> None:
    request = cli.parse(["apply", "--profile", "--svc", *SVC])
    assert (request.profile, request.service) == ("--svc", Service("dev-main"))


def test_the_proxy_command_in_the_ssh_config_is_parsed(home_dir: Path) -> None:
    """apply が ssh の設定に書く ProxyCommand は、そのまま proxy の要求になる。"""
    _ = home_dir
    words = home._proxy_command(Service("dev-main")).split()
    at = words.index("temt")
    # シェルが外す引用符も外して渡す
    tokens = {"%p": "2222", "'%r'": "ubuntu"}
    request = cli.parse([tokens.get(word, word) for word in words[at + 1 :]])
    assert (request.command, request.service, request.port, request.user) == (
        cli.SSH_PROXY,
        Service("dev-main"),
        2222,
        "ubuntu",
    )


def test_unknown_command_is_reported_before_the_service_name() -> None:
    with pytest.raises(UsageError, match="unknown command: pause"):
        cli.parse(["pause", "--svc", "Dev-main"])
    with pytest.raises(UsageError, match="instance: unknown verb: pause"):
        cli.parse(["instance", "pause", "--svc", "Dev-main"])


def test_main_without_args_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["temt"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("temt: missing command\n")
    assert "usage: temt" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["temt", "--help"],
        ["temt", "--help", "instance", "start"],
        ["temt", "--svc", "dev-main", "--help", "start"],
        ["temt", "--help", "pause", "--now"],
        ["temt", "pause", "--help"],
    ],
)
def test_help_before_a_target_prints_the_whole_usage(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert out == cli.USAGE
    assert err == ""


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["temt", "instance", "--help"], "instance"),
        (["temt", "instance", "pause", "--help", "--now"], "instance"),
        (["temt", "start", "--svc", "dev-main", "--help"], "instance"),
        (["temt", "--svc", "dev-main", "sg", "add", "--help"], "sg"),
    ],
)
def test_help_after_a_target_prints_its_usage(
    argv: list[str], target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert out == cli.usage(target)
    assert out.startswith(f"usage: temt {target} <verb>")
    assert err == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["temt", "--version"],
        ["temt", "instance", "pause", "--version", "--now"],
        ["temt", "--svc", "dev-main", "--version", "--help"],
    ],
)
def test_version_prints_the_version_of_temt(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """どこにあっても、ほかの引数を読まずにバージョンを出す。--help より前なら、こちら。"""
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert out == f"temt {metadata.version('temt')}\n"
    assert err == ""


def test_help_before_version_prints_the_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["temt", "--help", "--version"]) == 0
    assert capsys.readouterr().out == cli.USAGE


def test_version_after_a_value_option_is_its_value() -> None:
    request = cli.parse(["sg", "add", "22", "--svc", "dev-main", "--description", "--version"])
    assert request.description == "--version"


def _listed(text: str) -> list[str]:
    """使い方の、コマンドの行に書かれたコマンドを並べる。"""
    heads = (" ".join(line.split()[:2]) for line in text.splitlines() if line.startswith("  "))
    return [head for head in heads if head in cli.COMMANDS]


def test_the_usage_lists_every_command_once() -> None:
    assert sorted(_listed(cli.USAGE)) == sorted(cli.COMMANDS)


@pytest.mark.parametrize("target", cli.TARGETS)
def test_the_usage_of_a_target_has_only_its_commands(target: str) -> None:
    assert _listed(cli.usage(target)) == [c for c in cli.COMMANDS if c.split()[0] == target]


def test_the_usage_of_a_target_marks_the_service_option() -> None:
    assert "  instance start --svc <svc> [--async]" in cli.usage("instance")
    assert "  instance list [--svc <svc>] [--format <columns>]" in cli.usage("instance")
    assert "  app list [--svc <svc>] [--format <columns>]" in cli.usage("app")


def test_the_usage_of_a_target_has_only_its_notes() -> None:
    text = cli.usage("sg")
    assert "<port> of sg add" in text
    assert "<options> of app" not in text
    assert "ami delete refuses" not in text
    assert "<columns> is" not in cli.usage("template")
    # app list は --svc を省くと扱うものが変わるので、ほかの省けるコマンドの注意は書かない
    text = cli.usage("app")
    assert "[--svc <svc>] of app list may be left out" in text
    assert "with it, the command deals only with" not in text
    assert "[--svc <svc>] of app list" not in cli.usage("instance")


def test_help_after_a_value_option_is_its_value() -> None:
    request = cli.parse(["sg", "add", "22", "--svc", "dev-main", "--description", "--help"])
    assert request.description == "--help"


@pytest.mark.parametrize(
    ("argv", "error", "target"),
    [
        (["temt", "app", *SVC], "app: missing verb", "app"),
        (
            ["temt", "--svc", "dev-main", "instance", "pause"],
            "instance: unknown verb: pause",
            "instance",
        ),
        (["temt", "deploy", *SVC], "deploy: missing app id or path", "app"),
        (["temt", "instance", "--format"], "missing value for --format", "instance"),
        (
            ["temt", "service", "list", "--format", "NOPE"],
            "unknown column in --format: NOPE; use "
            + ",".join(services.COLUMNS.defaults + services.COLUMNS.extras),
            "service",
        ),
    ],
)
def test_usage_error_after_a_target_prints_its_usage(
    argv: list[str], error: str, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"{argv[0]}: {error}\n{cli.usage(target)}"


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["temt", "pause", "--svc", "dev-main"], "unknown command: pause"),
        (["temt", "keypair", "list"], "unknown command: keypair"),
        (["temt", "--svc"], "missing value for --svc"),
        (["temt", "--svc", "app"], "missing command"),
    ],
)
def test_usage_error_without_a_target_prints_the_whole_usage(
    argv: list[str], error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"temt: {error}\n{cli.USAGE}"


def test_main_does_not_read_the_called_name(capsys: pytest.CaptureFixture[str]) -> None:
    """呼ばれた名前からサービスを決めない。前の版のリンクから呼ばれても --svc が要る。"""
    assert cli.main(["/opt/bin/temt-dev-main", "status"]) == 2
    assert capsys.readouterr().err.startswith("temt: status: missing --svc\n")


def test_main_passes_yes_to_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply は確認を求めるので、--yes を取り、確認を省くかどうかを渡す。"""
    seen: list[bool] = []

    def remember(service: Service, *, yes: bool) -> None:
        _ = service
        seen.append(yes)

    monkeypatch.setattr(apply, "run", remember)
    assert cli.main(["temt", "apply", *SVC]) == 0
    assert cli.main(["temt", "apply", *SVC, "--yes"]) == 0
    assert seen == [False, True]


def test_main_runs_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Service] = []
    monkeypatch.setattr(apply, "diff", seen.append)
    assert cli.main(["temt", "service", "diff", *SVC]) == 0
    assert cli.main(["temt", "diff", *SVC]) == 0
    assert seen == [Service("dev-main"), Service("dev-main")]


def test_main_wraps_an_error_from_aws(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(service: Service, *, yes: bool) -> None:
        _ = service
        raise ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "not authorized"}}, "CreateRole"
        )

    monkeypatch.setattr(apply, "run", broken)
    assert cli.main(["temt", "apply", *SVC]) == 1
    err = capsys.readouterr().err
    assert err.startswith("temt: An error occurred (UnauthorizedOperation)")
    assert "not authorized" in err


def test_main_wraps_a_failure_to_reach_aws(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """接続の失敗など、AWS の答えが無い誤り（BotoCoreError）も、1 行で知らせて 1 で終わる。"""

    def broken(service: Service, *, yes: bool) -> None:
        _ = service
        raise EndpointConnectionError(endpoint_url="https://ec2.ap-northeast-1.amazonaws.com/")

    monkeypatch.setattr(apply, "run", broken)
    assert cli.main(["temt", "apply", *SVC]) == 1
    assert capsys.readouterr().err == (
        'temt: Could not connect to the endpoint URL: "https://ec2.ap-northeast-1.amazonaws.com/"\n'
    )


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        pytest.param(
            ProfileNotFound(profile="\x1b[2J"),
            "The config profile (?[2J) could not be found; give --profile",
            id="profile",
        ),
        pytest.param(
            ParamValidationError(report="Invalid length for parameter Value\nvalue: \x1b[2J"),
            "Parameter validation failed: Invalid length for parameter Value; value: ?[2J\n",
            id="lines",
        ),
    ],
)
def test_main_shows_an_aws_error_safely(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    shown: str,
) -> None:
    """botocore の例外の文は利用者の値を含みうるので、表示できない文字を ? にし、行をつなぐ。"""

    def broken(service: Service, *, yes: bool) -> None:
        _ = service
        raise error

    monkeypatch.setattr(apply, "run", broken)
    assert cli.main(["temt", "apply", *SVC]) == 1
    assert shown in capsys.readouterr().err


@pytest.mark.parametrize(
    "error", [ProfileNotFound(profile="work"), NoCredentialsError()], ids=["profile", "credentials"]
)
def test_main_says_where_the_profile_can_be_chosen(
    service: Service,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """資格情報でつまずいたら、プロファイルを選べる 3 つの場所を添える。"""

    def broken(service: Service, *, yes: bool) -> None:
        _ = service
        raise error

    monkeypatch.setattr(apply, "run", broken)
    assert cli.main(["temt", "apply", *SVC]) == 1
    err = capsys.readouterr().err
    assert "give --profile, set AWS_PROFILE" in err
    assert f"or write AwsProfile in {service.conf_path}" in err


def test_main_reports_a_file_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(request: cli.Request) -> int:
        raise IsADirectoryError(21, "Is a directory", str(Service("dev-main").conf_path))

    monkeypatch.setattr(cli, "run", broken)
    assert cli.main(["temt", "apply", *SVC]) == 1
    err = capsys.readouterr().err
    assert err.startswith("temt: ")
    assert "Is a directory" in err


def test_main_reports_an_interrupt(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C で中断されたときも、その旨を述べて 1 で終わる。"""

    def broken(service: Service, *, yes: bool) -> None:
        _ = service
        raise KeyboardInterrupt

    monkeypatch.setattr(apply, "run", broken)
    assert cli.main(["temt", "apply", *SVC]) == 1
    assert capsys.readouterr().err == "\ntemt: interrupted\n"


def test_main_reports_a_missing_module(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(request: cli.Request) -> int:
        raise ImportError(f"No module named 'boto3' for {request.command}")

    monkeypatch.setattr(cli, "run", broken)
    assert cli.main(["temt", "apply", *SVC]) == 1
    err = capsys.readouterr().err
    assert err.startswith("temt: No module named 'boto3'")
    assert err.endswith("; reinstall temt\n")


def test_main_takes_the_region_out_of_the_environment(
    service: Service, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """リージョンの環境変数だけを取り除く。プロファイルは AWS の既定の規則に任せる。"""
    monkeypatch.setenv("AWS_PROFILE", "work")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # 設定ファイルが無いので、AWS に触れる前に 1 で終わる
    assert cli.main(["temt", "status", *SVC]) == 1
    assert capsys.readouterr().err == (
        f"temt: no config file: {service.conf_path}; run 'temt apply --svc dev-main' first\n"
    )
    assert not [name for name in conf.REGION_ENV if name in os.environ]
    assert os.environ["AWS_PROFILE"] == "work"


@pytest.mark.parametrize("command", cli.OPTIONAL_SERVICE_COMMANDS)
@pytest.mark.parametrize(
    ("args", "service"),
    [([], None), (SVC, Service("dev-main"))],
)
def test_an_optional_service(command: str, args: list[str], service: Service | None) -> None:
    request = cli.parse([*command.split(), *positional(command), *args])
    assert (request.command, request.service, request.region) == (command, service, None)


@pytest.mark.parametrize("command", cli.LIST_COMMANDS)
def test_a_list_takes_a_format(command: str) -> None:
    """列の名前は各コマンドが確かめる。"""
    args = [*command.split(), *service_args(command)]
    assert cli.parse(args).format is None
    request = cli.parse([*args, "--format", "id,Name"])
    assert (request.command, request.format) == (command, "id,Name")


def test_a_format_goes_anywhere() -> None:
    request = cli.parse(["instance", "list", "--format", "ID", *SVC])
    assert (request.service, request.format) == (Service("dev-main"), "ID")
    request = cli.parse(["--format", "ID", "ami", "list", *SVC])
    assert (request.service, request.format) == (Service("dev-main"), "ID")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["service", "list", "--format"], "service list: missing value for --format"),
        (["ami", "list", "--format", ""], "ami list: missing value for --format"),
        (
            ["ami", "list", "--format", "ID", "--format", "NAME"],
            "ami list: repeated argument: --format",
        ),
        (["instance", "list", "dev-main"], "instance list: unexpected argument: dev-main"),
        (["status", *SVC, "--format", "ID"], "status: unexpected argument: --format"),
        (["ami", "prune", "--format", "ID"], "ami prune: unexpected argument: --format"),
        (["service", "list", "dev-main"], "service list: unexpected argument: dev-main"),
        (
            ["--profile", "work", "service", "list"],
            "service list: --profile is not allowed; it does not call AWS",
        ),
        (
            ["service", "list", "--profile", "work", *SVC],
            "service list: --profile is not allowed; it does not call AWS",
        ),
        (
            ["--region", "us-east-1", "service", "list"],
            "service list: --region is not allowed; it does not call AWS",
        ),
        (
            ["--region", "us-east-1", "instance", "list", *SVC],
            "instance list: --region is not allowed with a service",
        ),
        (
            ["app", "list", "--profile", "work"],
            "app list: --profile is not allowed; it does not call AWS without --svc",
        ),
        (
            ["app", "list", "--region", "us-east-1"],
            "app list: --region is not allowed; it does not call AWS without --svc",
        ),
        (
            ["app", "list", "--region", "us-east-1", *SVC],
            "app list: --region is not allowed with a service",
        ),
    ],
)
def test_list_arguments_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    "args",
    [
        ["--region", "us-east-1", "--profile", "work", "ami", "list"],
        ["--profile", "work", "--region", "us-east-1", "ami", "list"],
        ["ami", "list", "--region", "us-east-1", "--profile", "work"],
    ],
)
def test_region_and_profile_come_in_any_order(args: list[str]) -> None:
    request = cli.parse(args)
    assert (request.command, request.service, request.profile, request.region) == (
        cli.AMI_LIST,
        None,
        "work",
        "us-east-1",
    )


def test_ami_prune_takes_a_service_or_a_region() -> None:
    request = cli.parse(["--region", "us-east-1", "ami", "prune", "--yes"])
    assert (request.command, request.service, request.region, request.yes) == (
        cli.AMI_PRUNE,
        None,
        "us-east-1",
        True,
    )
    request = cli.parse(["ami", "prune", *SVC])
    assert (request.service, request.region, request.yes) == (Service("dev-main"), None, False)


def test_ami_delete_takes_an_image() -> None:
    request = cli.parse(["--region", "us-east-1", "ami", "delete", "temt-a-2609011200"])
    assert (request.command, request.service, request.region, request.image) == (
        cli.AMI_DELETE,
        None,
        "us-east-1",
        "temt-a-2609011200",
    )
    assert (request.force, request.yes) == (False, False)
    request = cli.parse(["ami", "delete", "--force", "ami-0123", "--yes"])
    assert (request.image, request.force, request.yes) == ("ami-0123", True, True)
    request = cli.parse(["ami", "delete", "ami-0123", *SVC])
    assert (request.service, request.image) == (Service("dev-main"), "ami-0123")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["ami", "delete"], "ami delete: missing AMI id or name"),
        (["ami", "delete", "--force"], "ami delete: missing AMI id or name"),
        (["ami", "delete", "a", "b"], "ami delete: unexpected argument: b"),
        (
            ["ami", "delete", "a", "--force", "--force"],
            "ami delete: repeated argument: --force",
        ),
        (["ami", "prune", "--force"], "ami prune: unexpected argument: --force"),
        (
            ["--region", "us-east-1", "ami", "delete", "a", *SVC],
            "ami delete: --region is not allowed with a service",
        ),
    ],
)
def test_ami_delete_arguments_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value) == message


INSTANCE = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"


def test_instance_terminate_takes_instance_ids() -> None:
    """インスタンス ID は省いてもよく、いくつでも取る。並びは打たれた順。"""
    assert cli.parse(["terminate", *SVC]).instance_ids == ()
    request = cli.parse(["instance", "terminate", OTHER, *SVC, "--yes", INSTANCE])
    assert (request.instance_ids, request.yes) == ((OTHER, INSTANCE), True)
    request = cli.parse(["terminate", "i-01234567", *SVC])
    assert (request.service, request.instance_ids) == (Service("dev-main"), ("i-01234567",))


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["terminate", *SVC, "web"],
            f"terminate: the instance id {INSTANCE_ID_RULE}: web",
        ),
        (
            ["terminate", *SVC, "i-0123456789ABCDEF0"],
            f"terminate: the instance id {INSTANCE_ID_RULE}: i-0123456789ABCDEF0",
        ),
        (
            ["instance", "terminate", *SVC, "i-0123"],
            f"instance terminate: the instance id {INSTANCE_ID_RULE}: i-0123",
        ),
        # 桁数は 8 か 17 だけで、その間も、17 を超えるものも取らない
        (
            ["terminate", *SVC, "i-012345678"],
            f"terminate: the instance id {INSTANCE_ID_RULE}: i-012345678",
        ),
        (
            ["terminate", *SVC, "i-0123456789abcdef"],
            f"terminate: the instance id {INSTANCE_ID_RULE}: i-0123456789abcdef",
        ),
        (
            ["terminate", *SVC, "i-0123456789abcdef01"],
            f"terminate: the instance id {INSTANCE_ID_RULE}: i-0123456789abcdef01",
        ),
        (
            ["terminate", *SVC, INSTANCE, OTHER, INSTANCE],
            f"terminate: repeated instance id: {INSTANCE}",
        ),
        # ID を取るのは terminate だけ
        (["stop", *SVC, INSTANCE], f"stop: unexpected argument: {INSTANCE}"),
        (["reboot", *SVC, INSTANCE], f"reboot: unexpected argument: {INSTANCE}"),
    ],
)
def test_instance_ids_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value) == message


def test_main_passes_the_instance_ids_to_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Service, tuple[str, ...], bool, bool]] = []

    def terminated(
        service: Service, *, ids: tuple[str, ...] = (), is_async: bool, yes: bool
    ) -> None:
        seen.append((service, ids, is_async, yes))

    monkeypatch.setattr(terminate, "run", terminated)
    assert cli.main(["temt", "terminate", *SVC, OTHER, "--async"]) == 0
    assert cli.main(["temt", "instance", "terminate", "--yes", *SVC]) == 0
    assert seen == [
        (Service("dev-main"), (OTHER,), True, False),
        (Service("dev-main"), (), False, True),
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["ami", "prune", "dev-main"], "ami prune: unexpected argument: dev-main"),
        (["ami", "list", "--yes"], "ami list: unexpected argument: --yes"),
        (
            ["--region", "us-east-1", "ami", "list", *SVC],
            "ami list: --region is not allowed with a service",
        ),
        (
            ["--region", "us-east-1", "ami", "list", *SVC],
            "ami list: --region is not allowed with a service",
        ),
        (
            ["ami", "prune", *SVC, "--region", "us-east-1"],
            "ami prune: --region is not allowed with a service",
        ),
        (
            ["--region", "us-east-1", "status", *SVC],
            "status: --region is not allowed with a service",
        ),
        (["--region"], "missing value for --region"),
        (["--region", "", "ami", "prune"], "ami prune: missing value for --region"),
        (
            ["--region", "a", "--region", "b", "ami", "prune"],
            "ami prune: repeated argument: --region",
        ),
        (
            ["--profile", "a", "ami", "prune", "--profile", "b"],
            "ami prune: repeated argument: --profile",
        ),
    ],
)
def test_services_and_regions_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value) == message


def test_main_runs_ami_list_prune_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Service | None, str | None, bool | str | None]] = []

    def listed(service: Service | None, region: str | None, spec: str | None) -> None:
        seen.append(("list", service, region, spec))

    def pruned(service: Service | None, region: str | None, *, yes: bool) -> None:
        seen.append(("prune", service, region, yes))

    def deleted(
        target: str, service: Service | None, region: str | None, *, force: bool, yes: bool
    ) -> None:
        seen.append(("delete", service, region, f"{target} force={force} yes={yes}"))

    monkeypatch.setattr(images, "list_images", listed)
    monkeypatch.setattr(images, "prune", pruned)
    monkeypatch.setattr(images, "delete", deleted)
    assert cli.main(["temt", "--region", "us-east-1", "ami", "list"]) == 0
    assert cli.main(["temt", "ami", "list", "--format", "ID", *SVC]) == 0
    assert cli.main(["temt", "ami", "prune", "--yes"]) == 0
    assert cli.main(["temt", "ami", "prune", *SVC]) == 0
    assert cli.main(["temt", "--region", "us-east-1", "ami", "delete", "ami-0123", "--force"]) == 0
    assert cli.main(["temt", "ami", "delete", "ami-0123", *SVC]) == 0
    assert seen == [
        ("list", None, "us-east-1", None),
        ("list", Service("dev-main"), None, "ID"),
        ("prune", None, None, True),
        ("prune", Service("dev-main"), None, False),
        ("delete", None, "us-east-1", "ami-0123 force=True yes=False"),
        ("delete", Service("dev-main"), None, "ami-0123 force=False yes=False"),
    ]


def test_main_runs_instance_list(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Service | None, str | None, str | None]] = []

    def listed(service: Service | None, region: str | None, spec: str | None) -> None:
        seen.append((service, region, spec))

    monkeypatch.setattr(status, "list_instances", listed)
    assert cli.main(["temt", "--region", "us-east-1", "instance", "list"]) == 0
    assert cli.main(["temt", "instance", "list", "--format", "ID", *SVC]) == 0
    assert cli.main(["temt", "instance", "list", *SVC]) == 0
    assert seen == [
        (None, "us-east-1", None),
        (Service("dev-main"), None, "ID"),
        (Service("dev-main"), None, None),
    ]


def test_main_runs_service_list_without_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """service list は AWS を呼ばないので、botocore を読み込めなくても動く。"""
    seen: list[tuple[Service | None, str | None]] = []
    monkeypatch.setattr(
        services, "list_services", lambda service, spec: seen.append((service, spec))
    )
    monkeypatch.setitem(sys.modules, "botocore.exceptions", None)
    assert cli.main(["temt", "service", "list", "--format", "SERVICE"]) == 0
    assert cli.main(["temt", "service", "list", *SVC]) == 0
    assert seen == [(None, "SERVICE"), (Service("dev-main"), None)]


def test_main_runs_app_list_without_a_service_and_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """--svc を省いた app list は AWS を呼ばないので、botocore を読み込めなくても動く。

    --svc を付ければ、そのサービスの記録も挙げる app.list_apps を呼び、--profile も取る。
    """
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(manifest, "list_apps", lambda spec: seen.append(("local", spec)))
    monkeypatch.setattr(app, "list_apps", lambda service, spec: seen.append((service.svc, spec)))
    assert cli.main(["temt", "app", "list", *SVC, "--profile", "work"]) == 0
    assert cli.main(["temt", "app", "list", *SVC]) == 0
    monkeypatch.setitem(sys.modules, "botocore.exceptions", None)
    assert cli.main(["temt", "app", "list", "--format", "ID"]) == 0
    assert seen == [("dev-main", None), ("dev-main", None), ("local", "ID")]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"AWS_REGION": "us-west-2", "AWS_DEFAULT_REGION": "us-east-1"}, "us-west-2"),
        ({"AWS_DEFAULT_REGION": "us-east-1"}, "us-east-1"),
        ({"AWS_REGION": "", "AWS_DEFAULT_REGION": "us-east-1"}, "us-east-1"),
        ({}, None),
    ],
)
def test_main_keeps_the_region_of_the_environment(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str | None
) -> None:
    """--svc の無いコマンドのため、取り除く前に aws-cli と同じ順で控える。"""
    for name in conf.REGION_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(images, "prune", lambda service, region, *, yes: None)
    assert cli.main(["temt", "ami", "prune"]) == 0
    kept = conf.ENV_REGION
    assert kept == expected
    assert not [name for name in conf.REGION_ENV if name in os.environ]


def test_main_says_where_the_profile_can_be_chosen_without_a_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(service: Service | None, region: str | None, *, yes: bool) -> None:
        raise NoCredentialsError()

    monkeypatch.setattr(images, "prune", fail)
    assert cli.main(["temt", "ami", "prune"]) == 1
    assert capsys.readouterr().err.endswith(
        f"; give --profile, write AwsProfile in {defaults_path()}, or set AWS_PROFILE\n"
    )


def test_ami_delete_refuses_what_aws_cannot_take() -> None:
    """UTF-8 として読めないバイトを含む名前は、AWS に送れないので先に断る。"""
    with pytest.raises(UsageError) as caught:
        _ = cli.parse(["ami", "delete", "temt-a\udcff"])
    assert str(caught.value) == "ami delete: the AMI id or name is not valid UTF-8: temt-a?"


def test_main_shows_a_file_error_like_other_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ファイルの読み書きの失敗は、パスの表示できない文字を ? にして知らせる。"""

    def fail(_: object) -> int:
        raise PermissionError(13, "Permission denied", "/srv/a\udcff\x1bb")

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["temt", "service", "list"]) == 1
    assert capsys.readouterr().err == "temt: Permission denied: /srv/a??b\n"


def test_main_with_stderr_closed_still_exits_with_the_code() -> None:
    """起動したときから標準エラー出力が閉じていても（2>&-）、落ちずに終了コードを返す。"""
    script = "import sys; from temt import cli; sys.exit(cli.main(['temt', 'no-such-command']))"
    done = subprocess.run(
        ["sh", "-c", '"$0" -c "$1" 2>&-', sys.executable, script], capture_output=True, check=False
    )
    assert done.returncode == 2


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["\x1b[2J"], "unknown command: ?[2J"),
        (["start", "--\x1b[2J", *SVC], "start: unknown option: --?[2J"),
        (
            ["app", "deploy", "./\x1b[2J", *SVC],
            f"app deploy: the app id {LABEL_RULE}: ?[2J (the last part of ./?[2J)",
        ),
    ],
)
def test_arguments_are_shown_safely(args: list[str], message: str) -> None:
    """コマンドの引数は、表示できない文字を ? にして誤りに入れる。"""
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value) == message


def test_app_commands_take_an_app_id_a_key_and_a_value() -> None:
    request = cli.parse(["app", "set", *SVC, "jupyter", "token=-x y\n"])
    assert (request.command, request.service, request.app, request.app_key, request.value) == (
        cli.APP_SET,
        Service("dev-main"),
        "jupyter",
        "token",
        # 値は - で始まっても、空白や改行を含んでも、そのまま受け取る
        "-x y\n",
    )
    # 最初の = で分ける
    request = cli.parse(["app", "set", "jupyter", "opts=a=b", *SVC])
    assert (request.app_key, request.value) == ("opts", "a=b")
    request = cli.parse(["app", "get", "jupyter", "deploy", *SVC])
    assert (request.app, request.app_key, request.value) == ("jupyter", "deploy", None)
    request = cli.parse(["app", "show", *SVC, "jupyter"])
    assert (request.app, request.app_key) == ("jupyter", None)
    assert cli.parse(["app", "list", *SVC]).app is None
    assert cli.parse(["app", "list"]).service is None


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["app", "show", *SVC], "app show: missing app id"),
        (["app", "get", *SVC, "jupyter"], "app get: missing key"),
        (["app", "set", *SVC, "jupyter"], "app set: missing <key>=<value>"),
        (
            ["app", "set", *SVC, "jupyter", "token"],
            "app set: give the value as <key>=<value>: token",
        ),
        (["app", "set", *SVC, "jupyter", "token", "abc"], "app set: unexpected argument"),
        # - で始まる語はコマンドラインオプションとして読む
        (["app", "set", *SVC, "jupyter", "token", "-5"], "app set: unknown option: -5"),
        (["app", "unset", "jupyter", *SVC], "app unset: missing key"),
        (["app", "show", *SVC, "a", "b"], "app show: unexpected argument: b"),
        (["app", "get", *SVC, "a", "b", "c"], "app get: unexpected argument: c"),
        (["app", "list", *SVC, "a"], "app list: unexpected argument: a"),
        (
            ["app", "show", *SVC, "Jupyter"],
            f"app show: the app id {LABEL_RULE}: Jupyter",
        ),
        (
            ["app", "get", *SVC, "a" * 65, "k"],
            f"app get: the app id {LABEL_RULE}: {'a' * 65}",
        ),
        (
            ["app", "get", *SVC, "jupyter", "a/b"],
            f"app get: the key {LABEL_RULE}: a/b",
        ),
        (
            ["app", "unset", *SVC, "jupyter", "token-"],
            f"app unset: the key {LABEL_RULE}: token-",
        ),
        (
            ["app", "set", *SVC, "jupyter", "=abc"],
            f"app set: the key {LABEL_RULE}: ",
        ),
        (
            ["app", "set", *SVC, "jupyter", "deploy=x"],
            "app set: the key deploy holds the deploy record of temt",
        ),
        (
            ["app", "unset", *SVC, "jupyter", "deploy"],
            "app unset: the key deploy holds the deploy record of temt",
        ),
        (
            ["app", "set", *SVC, "jupyter", "token="],
            "app set: the value must not be empty",
        ),
        (
            # UTF-8 でないバイト列は、Python が対にならないサロゲートとして読む
            ["app", "set", *SVC, "jupyter", "token=" + b"\xff".decode(errors="surrogateescape")],
            "app set: the value is not valid UTF-8",
        ),
    ],
)
def test_app_arguments_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value).startswith(message)


def test_main_runs_the_app_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(app, "list_apps", lambda service, spec: seen.append(("list", spec)))
    monkeypatch.setattr(app, "show", lambda service, app: seen.append(("show", app)))
    monkeypatch.setattr(app, "get", lambda service, app, key: seen.append(("get", app, key)))
    monkeypatch.setattr(
        app, "set_value", lambda service, app, key, value: seen.append(("set", app, key, value))
    )
    monkeypatch.setattr(app, "unset", lambda service, app, key: seen.append(("unset", app, key)))
    assert cli.main(["temt", "app", "list", "--svc", "web", "--format", "ID"]) == 0
    assert cli.main(["temt", "app", "show", "jupyter", "--svc", "web"]) == 0
    assert cli.main(["temt", "app", "get", "--svc", "web", "jupyter", "token"]) == 0
    assert cli.main(["temt", "app", "set", "--svc", "web", "jupyter", "token=abc"]) == 0
    assert cli.main(["temt", "app", "unset", "--svc", "web", "jupyter", "token"]) == 0
    assert seen == [
        ("list", "ID"),
        ("show", "jupyter"),
        ("get", "jupyter", "token"),
        ("set", "jupyter", "token", "abc"),
        ("unset", "jupyter", "token"),
    ]


def test_deploy_and_remove_take_an_app_and_options() -> None:
    request = cli.parse(["deploy", *SVC, "~/src/Apps/jupyter/", "--yes", "--bake"])
    assert (request.command, request.app, request.bake, request.yes) == (
        cli.APP_DEPLOY,
        # パスは、最後の要素をアプリ ID の規則で確かめたうえで、書かれたまま渡す
        "~/src/Apps/jupyter/",
        True,
        True,
    )
    assert request.user is None
    request = cli.parse(["app", "remove", "jupyter", *SVC])
    assert (request.command, request.app, request.bake, request.yes) == (
        cli.APP_REMOVE,
        "jupyter",
        False,
        False,
    )


@pytest.mark.parametrize(
    "args",
    [
        ["deploy", *SVC, "mise", "--user", "ubuntu", "--bake"],
        ["deploy", *SVC, "--bake", "--user", "ubuntu", "mise"],
        ["deploy", "--user", "ubuntu", *SVC, "--bake", "mise"],
        ["app", "remove", *SVC, "mise", "--user", "ubuntu", "--bake"],
    ],
)
def test_deploy_and_remove_take_a_user(args: list[str]) -> None:
    request = cli.parse(args)
    assert (request.app, request.user, request.bake) == ("mise", "ubuntu", True)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["deploy", *SVC], "deploy: missing app id or path"),
        (["app", "remove", *SVC], "app remove: missing app id"),
        (["deploy", *SVC, "Jupyter"], f"deploy: the app id {LABEL_RULE}: Jupyter"),
        (
            ["deploy", *SVC, "~/src/Jupyter/"],
            f"deploy: the app id {LABEL_RULE}: Jupyter (the last part of ~/src/Jupyter/)",
        ),
        (
            ["deploy", *SVC, "/"],
            f"deploy: the app id {LABEL_RULE}:  (the last part of /)",
        ),
        (["app", "remove", *SVC, "./j"], f"app remove: the app id {LABEL_RULE}: ./j"),
        (["deploy", *SVC, "j", "k"], "deploy: give the app option as <name>=<value>: k"),
        (["deploy", *SVC, "j", "Limit=1"], f"deploy: the app option name {LABEL_RULE}: Limit"),
        (["deploy", *SVC, "j", "=1"], f"deploy: the app option name {LABEL_RULE}: "),
        (["deploy", *SVC, "j", "a=1", "a="], "deploy: repeated app option: a"),
        (
            ["deploy", *SVC, "j", "a=x\ty"],
            "deploy: the value of a must not contain control characters such as newlines and tabs",
        ),
        (["deploy", *SVC, "j", "a=\udcff"], "deploy: the value of a is not valid UTF-8"),
        # アプリより先にオプションを書いた
        (
            ["deploy", *SVC, "a=1", "j"],
            "deploy: give the app id or path before the app options: a=1",
        ),
        (["deploy", *SVC, "j", "--reset", "--reset"], "deploy: repeated argument: --reset"),
        (["app", "remove", *SVC, "j", "a=1"], "app remove: unexpected argument: a=1"),
        (["app", "remove", *SVC, "j", "--reset"], "app remove: unexpected argument: --reset"),
        (["bake", *SVC, "--reset"], "bake: unexpected argument: --reset"),
        (["deploy", *SVC, "--profile", "p"], "deploy: missing app id or path"),
        (["deploy", *SVC, "j", "--async"], "deploy: unexpected argument: --async"),
        (["deploy", *SVC, "j", "--bake", "--bake"], "deploy: repeated argument: --bake"),
        (["bake", *SVC, "--bake"], "bake: unexpected argument: --bake"),
        (["deploy", *SVC, "j", "--user"], "deploy: missing value for --user"),
        (["deploy", *SVC, "j", "--user", ""], "deploy: missing value for --user"),
        (
            ["deploy", *SVC, "j", "--user", "a", "--user", "b"],
            "deploy: repeated argument: --user",
        ),
        (["deploy", *SVC, "j", "--user", "Ubuntu"], f"deploy: the user {USER_NAME_RULE}: Ubuntu"),
        # 値を取るコマンドラインオプションは、直後の語を値とする
        (["deploy", *SVC, "j", "--user", "--bake"], f"deploy: the user {USER_NAME_RULE}: --bake"),
        (["bake", *SVC, "--user", "ubuntu"], "bake: unexpected argument: --user"),
    ],
)
def test_deploy_arguments_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value) == message


def test_deploy_takes_app_options_after_the_app() -> None:
    """値は最初の = で分け、= や - で始まる値、日本語もそのまま取る。空の値は既定に戻す。"""
    request = cli.parse(
        [
            "deploy",
            "--reset",
            "idle-shutdown",
            "limit=4h",
            "a=b=c",
            "n=-1",
            "m=こんにちは",
            "x=",
            "--svc",
            "web",
        ],
    )
    assert request.app == "idle-shutdown"
    assert request.options == (
        ("limit", "4h"),
        ("a", "b=c"),
        ("n", "-1"),
        ("m", "こんにちは"),
        ("x", None),
    )
    assert request.reset
    plain = cli.parse(["deploy", "./apps/a=b/jupyter", "--svc", "web"])
    assert plain.app == "./apps/a=b/jupyter"
    assert plain.options == ()
    assert not plain.reset


def test_main_runs_deploy_and_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        deploy,
        "run",
        lambda service, target, *, user, baking, yes, given, reset: seen.append(
            ("deploy", target, user, baking, yes, given, reset)
        ),
    )
    monkeypatch.setattr(
        deploy,
        "remove",
        lambda service, app_id, *, user, baking, yes: seen.append(
            ("remove", app_id, user, baking, yes)
        ),
    )
    assert (
        cli.main(["temt", "deploy", "--svc", "web", "./jupyter", "--bake", "--user", "ubuntu"]) == 0
    )
    assert (
        cli.main(["temt", "deploy", "jupyter", "port=8888", "token=", "--reset", "--svc", "web"])
        == 0
    )
    assert cli.main(["temt", "app", "remove", "jupyter", "--yes", "--svc", "web"]) == 0
    assert seen == [
        ("deploy", "./jupyter", "ubuntu", True, False, {}, False),
        ("deploy", "jupyter", None, False, False, {"port": "8888", "token": None}, True),
        ("remove", "jupyter", None, False, True),
    ]


def test_main_ends_service_list_with_1_after_skipping(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    Service("web").conf_path.parent.mkdir(parents=True)
    (Service("web").conf_path.parent / "Odd.toml").write_text("", encoding="utf-8")
    Service("web").conf_path.write_text("", encoding="utf-8")
    assert cli.main(["temt", "service", "list", "--format", "service"]) == 1
    out, err = capsys.readouterr()
    assert out == "SERVICE\nweb\n"
    assert err.endswith("temt: skipped 1 of 2 config files\n")


def test_main_rejects_an_unknown_column_with_2(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home_dir
    assert cli.main(["temt", "service", "list", "--format", "color"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("temt: unknown column in --format: color; use SERVICE,")
    assert "usage: temt" in err


def test_sg_add_and_remove_take_a_rule() -> None:
    request = cli.parse(["sg", "add", *SVC, "8000-8010/udp", "2001:DB8::/64", "--description", ""])
    assert (
        request.command,
        request.protocol,
        request.rule_port,
        request.cidr,
        request.description,
    ) == (cli.SG_ADD, "udp", (8000, 8010), "2001:db8::/64", "")
    request = cli.parse(["sg", "add", "--description", "-home", "icmp", *SVC])
    assert (request.protocol, request.rule_port, request.cidr, request.description) == (
        "icmp",
        None,
        None,
        # 値を取るコマンドラインオプションの値は、- で始まってもよい
        "-home",
    )
    request = cli.parse(["sg", "remove", *SVC, "22", "203.0.113.10"])
    assert (request.protocol, request.rule_port, request.cidr, request.description) == (
        "tcp",
        (22, 22),
        "203.0.113.10/32",
        None,
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["sg", "add", *SVC], "sg add: missing port"),
        (["sg", "add", *SVC, "ssh"], f"sg add: the port {conf.PORT_RULE}: ssh"),
        (["sg", "add", *SVC, "22", "203.0.113.10/24"], "sg add: Cidr must be an IPv4"),
        (["sg", "add", *SVC, "22", "a", "b"], "sg add: unexpected argument: b"),
        (["sg", "add", *SVC, "22", "--yes"], "sg add: unexpected argument: --yes"),
        (["sg", "add", *SVC, "22", "--description"], "sg add: missing value for --description"),
        (
            ["sg", "add", *SVC, "22", "--description", "a", "--description", "b"],
            "sg add: repeated argument: --description",
        ),
        (["sg", "add", *SVC, "22", "--description", "<a>"], "sg add: Description must"),
        (["sg", "remove", *SVC, "22", "--description", "a"], "sg remove: unexpected"),
        (["sg", "list", *SVC, "22"], "sg list: unexpected argument: 22"),
        (["sg", "diff", *SVC, "--format", "PORT"], "sg diff: unexpected argument"),
        (["--profile", "work", "sg", "add", *SVC, "22"], "sg add: --profile is not allowed"),
        (["--profile", "work", "sg", "list", *SVC], "sg list: --profile is not allowed"),
        (["--profile", "work", "sg", "remove", *SVC, "22"], "sg remove: --profile is not"),
        (["--region", "us-east-1", "sg", "list", *SVC], "sg list: --region is not allowed"),
    ],
)
def test_sg_arguments_rejected(args: list[str], message: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse(args)
    assert str(caught.value).startswith(message)


def test_main_runs_the_sg_commands_without_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """sg list、sg add、sg remove は AWS を呼ばないので、botocore を読み込めなくても動く。"""
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(ingress, "list_rules", lambda *args: seen.append(("list", *args)))
    monkeypatch.setattr(ingress, "add", lambda *args: seen.append(("add", *args)))
    monkeypatch.setattr(ingress, "remove", lambda *args: seen.append(("remove", *args)))
    monkeypatch.setitem(sys.modules, "botocore.exceptions", None)
    service = Service("dev-main")
    assert cli.main(["temt", "sg", "list", "--format", "PORT", *SVC]) == 0
    assert cli.main(["temt", "sg", "add", "22", "--description", "home", *SVC]) == 0
    assert cli.main(["temt", "sg", "remove", "icmp", "203.0.113.10/32", *SVC]) == 0
    assert seen == [
        ("list", service, "PORT"),
        ("add", service, "tcp", (22, 22), None, "home"),
        ("remove", service, "icmp", None, "203.0.113.10/32"),
    ]


def test_main_runs_sg_diff_and_show(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Service]] = []
    monkeypatch.setattr(sg, "diff", lambda service: seen.append(("diff", service)))
    monkeypatch.setattr(sg, "show", lambda service: seen.append(("show", service)))
    assert cli.main(["temt", "sg", "diff", *SVC]) == 0
    assert cli.main(["temt", "sg", "show", *SVC]) == 0
    assert seen == [("diff", Service("dev-main")), ("show", Service("dev-main"))]


def test_the_egress_lookup_is_not_imported_by_every_command(home_dir: Path) -> None:
    """ほかのコマンドは、sg list、sg add、sg remove が使う ingress を読み込まない。

    ingress は urllib を読み込むので、ssh の接続のたびに走る proxy などの起動を遅らせ
    ないためである。ここでは、AWS を呼ばない service list で確かめる。
    """
    code = (
        "import sys\n"
        "from temt import cli\n"
        "cli.main(['temt', 'service', 'list'])\n"
        "sys.stderr.write(str('temt.ingress' in sys.modules))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "HOME": str(home_dir)},
        timeout=60,
    )
    assert done.stderr == "False"


def test_service_list_does_not_import_boto3(home_dir: Path) -> None:
    """services の import の連鎖が boto3 を読まないことを、新しいインタプリタで確かめる。"""
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None\n"
        "sys.modules['botocore'] = None\n"
        "from temt import cli\n"
        "sys.exit(cli.main(['temt', 'service', 'list']))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": str(home_dir)},
        timeout=60,
    )
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.startswith("SERVICE ")


def test_app_list_without_a_service_does_not_import_boto3(home_dir: Path) -> None:
    """--svc を省いた app list の import の連鎖が boto3 を読まないことを、新しい
    インタプリタで確かめる。
    """
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None\n"
        "sys.modules['botocore'] = None\n"
        "from temt import cli\n"
        "sys.exit(cli.main(['temt', 'app', 'list']))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": str(home_dir)},
        timeout=60,
    )
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.startswith("ID ")


def test_service_list_stops_quietly_when_the_reader_goes_away(home_dir: Path) -> None:
    """読み手が先に閉じても、誤りを出さずに 0 で終わる。"""
    code = "import sys\nfrom temt import cli\nsys.exit(cli.main(['temt', 'service', 'list']))\n"
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HOME": str(home_dir)},
    )
    assert process.stdout is not None
    process.stdout.close()
    _, err = process.communicate(timeout=60)
    assert (process.returncode, err) == (0, b"")
