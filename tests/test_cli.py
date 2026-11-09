"""コマンドラインのテスト。引数の読み方、使い方、誤りの出し方、実装の関数への振り分けを
確かめる。

実装の関数は temt fixture がすり替え、呼ばれた関数と引数を記録する。
"""

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

import click
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
    ProfileNotFound,
)
from conftest import run_temt

from temt import (
    app,
    apply,
    bake,
    cli,
    conf,
    deploy,
    destroy,
    home,
    images,
    importing,
    ingress,
    manifest,
    proxy,
    reboot,
    role,
    services,
    setport,
    sg,
    show,
    start,
    status,
    stop,
    terminate,
)
from temt.names import INSTANCE_ID_RULE, LABEL_RULE, USER_NAME_RULE, Service, defaults_path
from temt.table import tilde

SVC = ["--svc", "dev-main"]
DEV_MAIN = Service("dev-main")
# 取り込むインスタンスの ID と、temt の管理に移すロール
SOURCE = "i-0123456789abcdef0"
ANOTHER_SOURCE = "i-0fedcba9876543210"
ROLE = "work-role"

# コマンドと、--svc のほかに付ける位置引数と、呼ぶ実装の関数
COMMANDS = {
    "service apply": ([], "apply.run"),
    "service diff": ([], "apply.diff"),
    "service destroy": ([], "destroy.run"),
    "service list": ([], "services.list_services"),
    "service import": ([SOURCE], "importing.run"),
    "instance start": ([], "start.run"),
    "instance stop": ([], "stop.run"),
    "instance terminate": ([], "terminate.run"),
    "instance reboot": ([], "reboot.run"),
    "instance show": ([], "status.run"),
    "instance list": ([], "status.list_instances"),
    "ami bake": ([], "bake.run"),
    "ami list": ([], "images.list_images"),
    "ami prune": ([], "images.prune"),
    "ami delete": (["temt-a"], "images.delete"),
    "template show": ([], "show.template"),
    "role import": ([ROLE], "role.import_role"),
    "role delete": ([], "role.delete"),
    "sg list": ([], "ingress.list_rules"),
    "sg add": (["22"], "ingress.add"),
    "sg remove": (["22"], "ingress.remove"),
    "sg diff": ([], "sg.diff"),
    "sg show": ([], "sg.show"),
    "app list": ([], "manifest.list_apps"),
    "app show": (["jupyter"], "app.show"),
    "app get": (["jupyter", "token"], "app.get"),
    "app set": (["jupyter", "token=abc"], "app.set_value"),
    "app unset": (["jupyter", "token"], "app.unset"),
    "app deploy": (["jupyter"], "deploy.run"),
    "app remove": (["jupyter"], "deploy.remove"),
    "ssh proxy": ([], "proxy.run"),
    "ssh set-port": (["2222"], "setport.run"),
}
# --svc を省けるコマンド
OPTIONAL = ("service list", "instance list", "ami list", "ami prune", "ami delete", "app list")
# AWS を呼ばないコマンド。--profile も --region も取らない
LOCAL = ("service list", "sg list", "sg add", "sg remove", "app list")
# すり替える実装の関数
IMPLEMENTATIONS: tuple[tuple[ModuleType, str], ...] = (
    (apply, "run"),
    (apply, "diff"),
    (destroy, "run"),
    (services, "list_services"),
    (importing, "run"),
    (start, "run"),
    (stop, "run"),
    (terminate, "run"),
    (reboot, "run"),
    (status, "run"),
    (status, "list_instances"),
    (bake, "run"),
    (images, "list_images"),
    (images, "prune"),
    (images, "delete"),
    (show, "template"),
    (role, "import_role"),
    (role, "delete"),
    (ingress, "list_rules"),
    (ingress, "add"),
    (ingress, "remove"),
    (sg, "diff"),
    (sg, "show"),
    (manifest, "list_apps"),
    (app, "show"),
    (app, "get"),
    (app, "set_value"),
    (app, "unset"),
    (deploy, "run"),
    (deploy, "remove"),
    (proxy, "run"),
    (setport, "run"),
)


@dataclass(frozen=True)
class Call:
    """呼ばれた実装の関数と、そのときの引数、--profile と --region の値。"""

    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    profile: str | None
    region: str | None


@dataclass
class Temt:
    """実装の関数をすり替えた temt。"""

    calls: list[Call]
    capsys: pytest.CaptureFixture[str]

    def call(self, *args: str) -> Call:
        """args で動かし、0 で終わって、実装の関数をちょうど 1 つ呼んだことを確かめる。"""
        assert run_temt(list(args)) == 0
        assert len(self.calls) == 1
        return self.calls.pop()

    def reject(self, *args: str) -> str:
        """args で動かし、引数の誤りで終わって、何も呼ばなかったことを確かめる。
        標準エラー出力の最後の行（Error: の行）を返す。
        """
        assert run_temt(list(args)) == 2
        assert self.calls == []
        out, err = self.capsys.readouterr()
        assert out == ""
        return err.splitlines()[-1]


def _recorder(name: str, seen: list[Call]) -> Callable[..., None]:
    def record(*args: Any, **kwargs: Any) -> None:
        seen.append(Call(name, args, kwargs, conf.OPTION_PROFILE, conf.OPTION_REGION))

    return record


@pytest.fixture
def temt(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Temt:
    seen: list[Call] = []
    for module, function in IMPLEMENTATIONS:
        name = f"{module.__name__.removeprefix('temt.')}.{function}"
        monkeypatch.setattr(module, function, _recorder(name, seen))
    return Temt(seen, capsys)


def test_the_table_has_every_command() -> None:
    found = {
        f"{target} {verb}"
        for target, group in cli.cli.commands.items()
        if isinstance(group, click.Group)
        for verb in group.commands
    }
    assert found == set(COMMANDS)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_calls_its_implementation(temt: Temt, command: str) -> None:
    """どのコマンドも --svc を取る。省けるコマンドは、省けばすべてのサービスのものを扱う。"""
    args, implementation = COMMANDS[command]
    call = temt.call(*command.split(), *SVC, *args)
    assert call.name == implementation
    if command != "app list":
        assert DEV_MAIN in call.args
    if command in OPTIONAL:
        call = temt.call(*command.split(), *args)
        assert call.name == implementation
        assert DEV_MAIN not in call.args
    else:
        assert temt.reject(*command.split(), *args) == "Error: Missing option '--svc'."


# 短縮形ごとに、取る引数を付けて比べる
SHORT_FORM_ARGS = {
    "apply": ["--image-id", "ami-0123"],
    "diff": [],
    "destroy": ["--yes"],
    "start": ["--async"],
    "stop": ["--async", "--yes"],
    "terminate": [SOURCE, "--async", "--yes"],
    "reboot": ["--async", "--yes"],
    "status": ["--apps"],
    "bake": ["--yes"],
    "deploy": ["./jupyter", "a=1", "--bake", "--yes"],
    "proxy": ["2222", "--user", "ubuntu"],
}


@pytest.mark.parametrize(("short", "command"), cli.SHORT_FORMS.items())
def test_a_short_form_is_the_same_command(temt: Temt, short: str, command: click.Command) -> None:
    args = SHORT_FORM_ARGS[short]
    target = next(
        name
        for name, group in cli.cli.commands.items()
        if isinstance(group, click.Group) and command in group.commands.values()
    )
    assert command.name is not None
    assert temt.call(short, *SVC, *args) == temt.call(target, command.name, *SVC, *args)


def test_short_form_args_cover_every_short_form() -> None:
    assert set(SHORT_FORM_ARGS) == set(cli.SHORT_FORMS)


@pytest.mark.parametrize(
    "args",
    [
        ["--svc", "dev-main", "--profile", "work", "--region", "us-east-1", "start"],
        ["start", "--svc", "dev-main", "--profile", "work", "--region", "us-east-1"],
        ["--profile", "work", "start", "--region", "us-east-1", "--svc", "dev-main"],
        ["--region", "us-east-1", "instance", "start", "--profile", "work", *SVC],
    ],
)
def test_common_options_go_before_or_after_the_command(temt: Temt, args: list[str]) -> None:
    """--svc、--profile、--region は、temt の直後にも、コマンドの後ろにも置ける。"""
    call = temt.call(*args)
    assert (call.name, call.args[0], call.profile, call.region) == (
        "start.run",
        DEV_MAIN,
        "work",
        "us-east-1",
    )


def test_the_service_command_puts_the_service_first(temt: Temt) -> None:
    """サービスのコマンドは temt --svc <svc> "$@" で、省けるコマンドも、そのサービスを扱う。"""
    assert temt.call("--svc", "dev-main", "ami", "list").args[0] == DEV_MAIN
    assert temt.call("--svc", "dev-main", "app", "list").name == "manifest.list_apps"


@pytest.mark.parametrize(
    ("args", "flag"),
    [
        (["--svc", "web", "start", *SVC], "--svc"),
        (["--profile", "a", "start", *SVC, "--profile", "b"], "--profile"),
        (["--region", "a", "ami", "list", "--region", "b"], "--region"),
    ],
)
def test_a_common_option_on_both_sides_is_rejected(temt: Temt, args: list[str], flag: str) -> None:
    assert temt.reject(*args) == f"Error: {flag} is given both before and after the command."


def test_the_service_command_takes_no_service(temt: Temt) -> None:
    """サービスのコマンドが temt の直後に置いた --svc に、利用者の --svc を重ねれば誤り。
    コマンドの前に置いても、後ろに置いても、別のサービスを黙って扱わない。
    """
    assert temt.reject("--svc", "dev-main", "--svc", "web", "status") == (
        "Error: --svc is given more than once."
    )
    assert temt.reject("--svc", "dev-main", "status", "--svc", "web") == (
        "Error: --svc is given both before and after the command."
    )


def test_other_options_go_after_the_command(temt: Temt) -> None:
    assert temt.reject("--yes", "destroy", *SVC).startswith("Error: No such option '--yes'")
    assert temt.reject("start", *SVC, "--version").startswith("Error: No such option '--version'")


@pytest.mark.parametrize("command", LOCAL)
@pytest.mark.parametrize("flag", ["--profile", "--region"])
def test_a_local_command_takes_no_profile_or_region(temt: Temt, command: str, flag: str) -> None:
    """AWS を呼ばないコマンドは、--profile も --region も取らない。temt の直後に置いても誤り。"""
    args = [*command.split(), *SVC, *COMMANDS[command][0]]
    assert temt.reject(flag, "x", *args) == f"Error: {flag} is not allowed; it does not call AWS."
    assert temt.reject(*args, flag, "x") == f"Error: No such option '{flag}'."


def test_a_value_is_the_next_word_even_if_it_starts_with_a_dash(temt: Temt) -> None:
    call = temt.call("apply", "--profile", "--svc", *SVC)
    assert (call.profile, call.args[0]) == ("--svc", DEV_MAIN)
    call = temt.call("sg", "add", "--description", "-home", "22", *SVC)
    assert call.args[-1] == "-home"


def test_the_last_of_a_repeated_option_wins(temt: Temt) -> None:
    assert temt.call("start", "--svc", "web", *SVC).args[0] == DEV_MAIN


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["start", "--svc"], "Error: Option '--svc' requires an argument."),
        (["start", "--svc", ""], "Error: Invalid value for '--svc': invalid service name: "),
        (["start", "--svc", "Dev-main"], "Error: Invalid value for '--svc': invalid service name"),
        (["--svc", "dev-"], "Error: Invalid value for '--svc': invalid service name: dev-"),
        (["apply", *SVC, "--profile", ""], "Error: Invalid value for '--profile': must not be"),
        (["--region", "", "ami", "list"], "Error: Invalid value for '--region': must not be"),
        (["ami", "list", "--format", ""], "Error: Invalid value for '--format': must not be"),
        (["apply", *SVC, "--image-id", ""], "Error: Invalid value for '--image-id': must not"),
        (
            ["diff", *SVC, "--image-id", "temt-\udcff"],
            "Error: Invalid value for '--image-id': 'temt-?' is not valid UTF-8",
        ),
        (["pause", *SVC], "Error: No such command 'pause'."),
        (["instance", "pause", *SVC], "Error: No such command 'pause'."),
        (["template", "apply", *SVC], "Error: No such command 'apply'."),
        (["instance", "dev-main", "start"], "Error: No such command 'dev-main'."),
        (["--svc", "dev-main"], "Error: Missing command."),
        # サービス名は位置引数では取らない
        (["instance", "start", "dev-main", *SVC], "Error: Got unexpected extra argument"),
        (["start", "--async", "extra", *SVC], "Error: Got unexpected extra argument (extra)"),
        # 1 文字のコマンドラインオプションは無い
        (["start", "-s", "dev-main"], "Error: No such option '-s'"),
        # bake と destroy はつねに完了を待つ
        (["bake", "--async", *SVC], "Error: No such option '--async'"),
        (["destroy", "--async", *SVC], "Error: No such option '--async'"),
        (["start", *SVC, "--yes"], "Error: No such option '--yes'"),
        (["status", *SVC, "--format", "ID"], "Error: No such option '--format'"),
        # 取り込みは service import で、ami import も短縮形も無い
        (["ami", "import", *SVC, SOURCE], "Error: No such command 'import'."),
        (["import", *SVC, SOURCE], "Error: No such command 'import'."),
    ],
)
def test_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject(*args).startswith(message)


def test_proxy_takes_a_port(temt: Temt) -> None:
    assert temt.call("proxy", *SVC).args[1] == 22
    assert temt.call("ssh", "proxy", "2222", *SVC).args[1] == 2222
    assert temt.call("proxy", "65535", *SVC).args[1] == 65535


@pytest.mark.parametrize("port", ["0", "65536", "22x", ""])
def test_a_port_out_of_range_is_rejected(temt: Temt, port: str) -> None:
    assert temt.reject("proxy", port, *SVC).startswith("Error: Invalid value for '[PORT]': ")


def test_a_negative_port_is_read_as_an_option(temt: Temt) -> None:
    """- で始まる語はコマンドラインオプションとして読む。"""
    assert temt.reject("proxy", "-1", *SVC) == "Error: No such option '-1'."


def test_set_port_takes_ports(temt: Temt) -> None:
    """ssh set-port は sshd のポートを 1 つ以上取る。並びは打たれた順。"""
    call = temt.call("ssh", "set-port", *SVC, "2222", "443")
    assert (call.args, call.kwargs) == ((DEV_MAIN, (2222, 443)), {"yes": False})
    call = temt.call("ssh", "set-port", "443", *SVC, "--yes", "2222")
    assert (call.args, call.kwargs) == ((DEV_MAIN, (443, 2222)), {"yes": True})


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "Error: Missing argument 'PORT...'."),
        (["0"], "Error: Invalid value for 'PORT...': 0 is not in the range 1<=x<=65535."),
        (["2222", "22x"], "Error: Invalid value for 'PORT...': '22x' is not a valid integer"),
        (["2222", "443", "2222"], "Error: Invalid value for 'PORT...': '2222' is given twice"),
        # 書き方が違っても、値が同じなら重ねたとみなす
        (["22", "022"], "Error: Invalid value for 'PORT...': '22' is given twice"),
    ],
)
def test_set_port_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject("ssh", "set-port", *SVC, *args).startswith(message)


def test_service_import_takes_an_instance_id(temt: Temt) -> None:
    call = temt.call("service", "import", *SVC, SOURCE)
    assert (call.args, call.kwargs) == ((DEV_MAIN, SOURCE), {"yes": False})
    call = temt.call("--region", "us-east-1", "service", "import", "--yes", "i-01234567", *SVC)
    assert (call.args, call.kwargs, call.region) == (
        (DEV_MAIN, "i-01234567"),
        {"yes": True},
        "us-east-1",
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "Error: Missing argument 'INSTANCE_ID'."),
        (["web"], f"Error: Invalid value for 'INSTANCE_ID': 'web' {INSTANCE_ID_RULE}"),
        (["i-0123456789ABCDEF0"], "Error: Invalid value for 'INSTANCE_ID': 'i-0123456789ABCDEF0'"),
        (["i-\x1b[2J"], "Error: Invalid value for 'INSTANCE_ID': 'i-?[2J'"),
        ([SOURCE, ANOTHER_SOURCE], f"Error: Got unexpected extra argument ({ANOTHER_SOURCE})"),
        ([SOURCE, "--async"], "Error: No such option '--async'"),
    ],
)
def test_service_import_arguments_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject("service", "import", *SVC, *args).startswith(message)


@pytest.mark.parametrize("named", [ROLE, "arn:aws:iam::123456789012:role/service-role/Work_Role"])
def test_role_import_takes_a_role_name_or_arn(temt: Temt, named: str) -> None:
    """ロールの名前か ARN をちょうど 1 つ取る。書かれたまま渡す。"""
    call = temt.call("role", "import", *SVC, named)
    assert (call.args, call.kwargs) == ((DEV_MAIN, named), {"yes": False})
    assert temt.call("role", "import", "--yes", named, *SVC).kwargs == {"yes": True}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["role", "import", *SVC], "Error: Missing argument 'ROLE'."),
        (["role", "import", *SVC, ""], "Error: Invalid value for 'ROLE': must not be empty"),
        (["role", "import", *SVC, ROLE, "other"], "Error: Got unexpected extra argument (other)"),
        (
            # UTF-8 として読めないバイトを含む名前は、AWS に送れないので先に断る
            ["role", "import", *SVC, "work\udcff"],
            "Error: Invalid value for 'ROLE': 'work?' is not valid UTF-8",
        ),
        (["role", "delete", *SVC, ROLE], f"Error: Got unexpected extra argument ({ROLE})"),
        (["role", "delete", *SVC, "--force"], "Error: No such option '--force'"),
    ],
)
def test_role_arguments_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject(*args).startswith(message)


@pytest.mark.parametrize("command", ["instance list", "ami list", "service list", "app list"])
def test_a_list_takes_a_format(temt: Temt, command: str) -> None:
    """列の名前は各コマンドが確かめる。"""
    call = temt.call(*command.split(), "--format", "id,Name")
    assert call.args[-1] == "id,Name"
    assert temt.call(*command.split()).args[-1] is None
    assert temt.call("sg", "list", *SVC, "--format", "PORT").args == (DEV_MAIN, "PORT")


def test_instance_list_takes_a_service_or_a_region(temt: Temt) -> None:
    call = temt.call("--region", "us-east-1", "instance", "list")
    assert (call.args, call.region) == ((None, "us-east-1", None), "us-east-1")
    call = temt.call("instance", "list", "--format", "ID", *SVC)
    assert (call.args, call.region) == ((DEV_MAIN, None, "ID"), None)


def test_ami_commands_take_a_service_or_a_region(temt: Temt) -> None:
    call = temt.call("--region", "us-east-1", "ami", "list")
    assert (call.args, call.region) == ((None, "us-east-1", None), "us-east-1")
    call = temt.call("ami", "prune", "--yes")
    assert (call.args, call.kwargs) == ((None, None), {"yes": True})
    call = temt.call("ami", "prune", *SVC)
    assert (call.args, call.kwargs) == ((DEV_MAIN, None), {"yes": False})
    call = temt.call("ami", "delete", "--force", "ami-0123", "--yes", "--region", "us-east-1")
    assert (call.args, call.kwargs) == (
        ("ami-0123", None, "us-east-1"),
        {"force": True, "yes": True},
    )
    call = temt.call("ami", "delete", "ami-0123", *SVC)
    assert (call.args, call.kwargs) == (
        ("ami-0123", DEV_MAIN, None),
        {"force": False, "yes": False},
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["ami", "delete"], "Error: Missing argument 'AMI'."),
        (["ami", "delete", "a", "b"], "Error: Got unexpected extra argument (b)"),
        (["ami", "prune", "--force"], "Error: No such option '--force'"),
        (["ami", "list", "--yes"], "Error: No such option '--yes'"),
        (["ami", "prune", "dev-main"], "Error: Got unexpected extra argument (dev-main)"),
        (
            # UTF-8 として読めないバイトを含む名前は、AWS に送れないので先に断る
            ["ami", "delete", "temt-a\udcff"],
            "Error: Invalid value for 'AMI': 'temt-a?' is not valid UTF-8",
        ),
    ],
)
def test_ami_arguments_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject(*args).startswith(message)


def test_instance_terminate_takes_instance_ids(temt: Temt) -> None:
    """インスタンス ID は省いてもよく、いくつでも取る。並びは打たれた順。"""
    assert temt.call("terminate", *SVC).kwargs["ids"] == ()
    call = temt.call("instance", "terminate", ANOTHER_SOURCE, *SVC, "--yes", SOURCE)
    assert call.kwargs == {"ids": (ANOTHER_SOURCE, SOURCE), "is_async": False, "yes": True}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["web"], f"'web' {INSTANCE_ID_RULE}"),
        # 桁数は 8 か 17 だけで、その間も、17 を超えるものも取らない
        (["i-012345678"], f"'i-012345678' {INSTANCE_ID_RULE}"),
        (["i-0123456789abcdef01"], f"'i-0123456789abcdef01' {INSTANCE_ID_RULE}"),
        ([SOURCE, ANOTHER_SOURCE, SOURCE], f"'{SOURCE}' is given twice"),
    ],
)
def test_instance_ids_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject("terminate", *SVC, *args) == (
        f"Error: Invalid value for '[INSTANCE_ID]...': {message}"
    )


@pytest.mark.parametrize("command", ["stop", "reboot"])
def test_only_terminate_takes_instance_ids(temt: Temt, command: str) -> None:
    assert temt.reject(command, *SVC, SOURCE) == f"Error: Got unexpected extra argument ({SOURCE})"


def test_app_commands_take_an_app_id_a_key_and_a_value(temt: Temt) -> None:
    # 値は - で始まっても、空白や改行を含んでも、そのまま受け取る
    call = temt.call("app", "set", *SVC, "jupyter", "token=-x y\n")
    assert call.args == (DEV_MAIN, "jupyter", "token", "-x y\n")
    # 最初の = で分ける
    assert temt.call("app", "set", "jupyter", "opts=a=b", *SVC).args[2:] == ("opts", "a=b")
    assert temt.call("app", "get", "jupyter", "deploy", *SVC).args == (
        DEV_MAIN,
        "jupyter",
        "deploy",
    )
    assert temt.call("app", "show", *SVC, "jupyter").args == (DEV_MAIN, "jupyter")
    assert temt.call("app", "unset", *SVC, "jupyter", "token").args == (
        DEV_MAIN,
        "jupyter",
        "token",
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["app", "show", *SVC], "Error: Missing argument 'APP_ID'."),
        (["app", "get", *SVC, "jupyter"], "Error: Missing argument 'KEY'."),
        (["app", "set", *SVC, "jupyter"], "Error: Missing argument 'KEY=VALUE'."),
        (
            ["app", "set", *SVC, "jupyter", "token"],
            "Error: Invalid value for 'KEY=VALUE': 'token' must be KEY=VALUE",
        ),
        (["app", "set", *SVC, "jupyter", "token", "abc"], "Error: Invalid value for 'KEY=VALUE'"),
        # - で始まる語はコマンドラインオプションとして読む
        (["app", "set", *SVC, "jupyter", "-5"], "Error: No such option '-5'"),
        (["app", "show", *SVC, "a", "b"], "Error: Got unexpected extra argument (b)"),
        (["app", "list", *SVC, "a"], "Error: Got unexpected extra argument (a)"),
        (
            ["app", "show", *SVC, "Jupyter"],
            f"Error: Invalid value for 'APP_ID': 'Jupyter' {LABEL_RULE}",
        ),
        (["app", "get", *SVC, "a" * 65, "k"], "Error: Invalid value for 'APP_ID': "),
        (
            ["app", "get", *SVC, "jupyter", "a/b"],
            f"Error: Invalid value for 'KEY': 'a/b' {LABEL_RULE}",
        ),
        (
            ["app", "set", *SVC, "jupyter", "=abc"],
            f"Error: Invalid value for 'KEY=VALUE': '' {LABEL_RULE}",
        ),
        (
            ["app", "set", *SVC, "jupyter", "token="],
            "Error: Invalid value for 'KEY=VALUE': the value must not be empty",
        ),
        (
            # UTF-8 でないバイト列は、Python が対にならないサロゲートとして読む
            ["app", "set", *SVC, "jupyter", "token=" + b"\xff".decode(errors="surrogateescape")],
            "Error: Invalid value for 'KEY=VALUE': the value is not valid UTF-8",
        ),
        (["app", "list", "--apps"], "Error: No such option '--apps'"),
    ],
)
def test_app_arguments_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject(*args).startswith(message)


def test_deploy_and_remove_take_an_app_and_options(temt: Temt) -> None:
    call = temt.call("deploy", *SVC, "~/src/Apps/jupyter/", "--yes", "--bake")
    # パスは、最後の要素をアプリ ID の規則で確かめたうえで、書かれたまま渡す
    assert (call.args, call.kwargs) == (
        (DEV_MAIN, "~/src/Apps/jupyter/"),
        {"user": None, "baking": True, "yes": True, "given": {}, "reset": False},
    )
    call = temt.call("app", "remove", "jupyter", *SVC, "--user", "ubuntu")
    assert (call.args, call.kwargs) == (
        (DEV_MAIN, "jupyter"),
        {"user": "ubuntu", "baking": False, "yes": False},
    )


def test_deploy_takes_app_options_after_the_app(temt: Temt) -> None:
    """値は最初の = で分け、= や - で始まる値、日本語もそのまま取る。空の値は既定に戻す。"""
    call = temt.call(
        "deploy",
        "--reset",
        "idle-shutdown",
        "limit=4h",
        "a=b=c",
        "n=-1",
        "m=こんにちは",
        "x=",
        *SVC,
    )
    assert call.args[1] == "idle-shutdown"
    assert call.kwargs["given"] == {
        "limit": "4h",
        "a": "b=c",
        "n": "-1",
        "m": "こんにちは",
        "x": None,
    }
    assert call.kwargs["reset"] is True
    assert temt.call("deploy", "./apps/a=b/jupyter", *SVC).args[1] == "./apps/a=b/jupyter"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["deploy", *SVC], "Error: Missing argument 'APP'."),
        (["app", "remove", *SVC], "Error: Missing argument 'APP_ID'."),
        (["deploy", *SVC, "Jupyter"], f"Error: Invalid value for 'APP': 'Jupyter' {LABEL_RULE}"),
        (
            ["deploy", *SVC, "~/src/Jupyter/"],
            "Error: Invalid value for 'APP': 'Jupyter' (the last part of '~/src/Jupyter/')",
        ),
        (["deploy", *SVC, "/"], "Error: Invalid value for 'APP': '' (the last part of '/')"),
        (["app", "remove", *SVC, "./j"], f"Error: Invalid value for 'APP_ID': './j' {LABEL_RULE}"),
        (
            ["deploy", *SVC, "j", "k"],
            "Error: Invalid value for '[NAME=VALUE]...': 'k' must be NAME=VALUE",
        ),
        (
            ["deploy", *SVC, "j", "Limit=1"],
            f"Error: Invalid value for '[NAME=VALUE]...': 'Limit' {LABEL_RULE}",
        ),
        (
            ["deploy", *SVC, "j", "a=1", "a="],
            "Error: Invalid value for '[NAME=VALUE]...': 'a' is given twice",
        ),
        (
            ["deploy", *SVC, "j", "a=x\ty"],
            "Error: Invalid value for '[NAME=VALUE]...': the value of a must not contain control",
        ),
        (
            ["deploy", *SVC, "j", "a=\udcff"],
            "Error: Invalid value for '[NAME=VALUE]...': the value of a is not valid UTF-8",
        ),
        # アプリより先にオプションを書いた
        (
            ["deploy", *SVC, "a=1", "j"],
            "Error: Invalid value for 'APP': give the app id or path before the app options: 'a=1'",
        ),
        (["app", "remove", *SVC, "j", "a=1"], "Error: Got unexpected extra argument (a=1)"),
        (["app", "remove", *SVC, "j", "--reset"], "Error: No such option '--reset'"),
        (["bake", *SVC, "--reset"], "Error: No such option '--reset'"),
        (["deploy", *SVC, "j", "--async"], "Error: No such option '--async'"),
        (["bake", *SVC, "--bake"], "Error: No such option '--bake'"),
        (["deploy", *SVC, "j", "--user"], "Error: Option '--user' requires an argument."),
        (
            ["deploy", *SVC, "j", "--user", ""],
            f"Error: Invalid value for '--user': '' {USER_NAME_RULE}",
        ),
        (
            ["deploy", *SVC, "j", "--user", "Ubuntu"],
            f"Error: Invalid value for '--user': 'Ubuntu' {USER_NAME_RULE}",
        ),
        # 値を取るコマンドラインオプションは、直後の語を値とする
        (["deploy", *SVC, "j", "--user", "--bake"], "Error: Invalid value for '--user': '--bake'"),
        (["bake", *SVC, "--user", "ubuntu"], "Error: No such option '--user'"),
    ],
)
def test_deploy_arguments_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject(*args).startswith(message)


def test_sg_add_and_remove_take_a_rule(temt: Temt) -> None:
    call = temt.call("sg", "add", *SVC, "8000-8010/udp", "2001:DB8::/64", "--description", "")
    assert call.args == (DEV_MAIN, "udp", (8000, 8010), "2001:db8::/64", "")
    call = temt.call("sg", "add", "--description", "home", "icmp", *SVC)
    assert call.args == (DEV_MAIN, "icmp", None, None, "home")
    call = temt.call("sg", "remove", *SVC, "22", "203.0.113.10")
    assert call.args == (DEV_MAIN, "tcp", (22, 22), "203.0.113.10/32")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["sg", "add", *SVC], "Error: Missing argument 'PORT'."),
        (["sg", "add", *SVC, "ssh"], f"Error: Invalid value for 'PORT': 'ssh' {conf.PORT_RULE}"),
        (
            ["sg", "add", *SVC, "22", "203.0.113.10/24"],
            "Error: Invalid value for '[CIDR]': Cidr must",
        ),
        (
            ["sg", "add", *SVC, "22", "203.0.113.10", "b"],
            "Error: Got unexpected extra argument (b)",
        ),
        (["sg", "add", *SVC, "22", "--yes"], "Error: No such option '--yes'"),
        (["sg", "add", *SVC, "22", "--description"], "Error: Option '--description' requires"),
        (
            ["sg", "add", *SVC, "22", "--description", "<a>"],
            "Error: Invalid value for '--description': Description must",
        ),
        (
            ["sg", "remove", *SVC, "22", "--description", "a"],
            "Error: No such option '--description'",
        ),
        (["sg", "list", *SVC, "22"], "Error: Got unexpected extra argument (22)"),
        (["sg", "diff", *SVC, "--format", "PORT"], "Error: No such option '--format'"),
    ],
)
def test_sg_arguments_rejected(temt: Temt, args: list[str], message: str) -> None:
    assert temt.reject(*args).startswith(message)


def test_status_and_instance_show_take_apps(temt: Temt) -> None:
    """--apps を付ければ、入っているアプリも出す。"""
    assert temt.call("status", *SVC).kwargs == {"apps": False}
    assert temt.call("instance", "show", *SVC, "--apps").kwargs == {"apps": True}


def test_apply_and_diff_take_an_image_id(temt: Temt) -> None:
    call = temt.call("apply", *SVC, "--image-id", "temt-dev-main-2609200900")
    assert call.kwargs == {"image": "temt-dev-main-2609200900", "yes": False}
    assert temt.call("diff", *SVC).kwargs == {"image": None}
    assert temt.reject("bake", *SVC, "--image-id", "ami-0").startswith("Error: No such option")


def test_the_proxy_command_in_the_ssh_config_is_parsed(temt: Temt, home_dir: Path) -> None:
    """apply が ssh の設定に書く ProxyCommand は、そのまま proxy を呼ぶ。"""
    _ = home_dir
    words = home._proxy_command(DEV_MAIN).split()
    at = words.index("temt")
    # シェルが外す引用符も外して渡す
    tokens = {"%p": "2222", "'%r'": "ubuntu"}
    call = temt.call(*(tokens.get(word, word) for word in words[at + 1 :]))
    assert (call.name, call.args) == ("proxy.run", (DEV_MAIN, 2222, "ubuntu"))


def test_arguments_are_shown_safely(temt: Temt) -> None:
    """誤りに入れる引数は、表示できない文字を ? にするか、click が repr の形で書く。"""
    assert temt.reject("deploy", "./\x1b[2J", *SVC) == (
        f"Error: Invalid value for 'APP': '?[2J' (the last part of './?[2J') {LABEL_RULE}"
    )
    assert temt.reject("start", *SVC, "a\x1b[2J\rb") == (
        "Error: Got unexpected extra argument (a?[2J?b)"
    )
    assert temt.reject("\x1b[2J") == "Error: No such command '\\x1b[2J'."
    assert temt.reject("start", *SVC, "--\x1b") == "Error: No such option '--\\x1b'."


@pytest.fixture
def wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """使い方の幅を、端末によらず 80 桁にする。"""
    monkeypatch.setenv("COLUMNS", "80")


def help_of(capsys: pytest.CaptureFixture[str], *args: str) -> str:
    """--help で標準出力に書いた使い方。"""
    assert run_temt([*args, "--help"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    return out


def test_the_help_lists_targets_verbs_and_short_forms(
    wide: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = wide
    text = help_of(capsys)
    assert text.startswith("Usage: temt [OPTIONS] COMMAND [ARGS]...\n")
    lines = text.splitlines()
    for target, group in cli.cli.commands.items():
        if isinstance(group, click.Group):
            assert f"  {target:<9} {' '.join(group.commands)}" in lines
    for option in ("--svc SVC", "--profile PROFILE", "--region REGION", "--version", "--help"):
        assert f"  {option}" in text
    short = " ".join(text.split("Short forms:\n", 1)[1].split("\n\n", 1)[0].split())
    assert short == (
        "apply diff destroy (service), start stop terminate reboot (instance), bake (ami), "
        "deploy (app), proxy (ssh), status (instance show)"
    )


def test_the_help_of_a_target_lists_every_option_of_its_verbs(
    wide: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = wide
    text = " ".join(help_of(capsys, "app").split())
    assert "list [--svc SVC] [--format COLUMNS] show --svc SVC APP_ID" in text
    assert (
        "deploy --svc SVC APP [NAME=VALUE]... [--user USER] [--bake] [--reset] [--yes] "
        "[--profile PROFILE] [--region REGION]"
    ) in text
    text = " ".join(help_of(capsys, "sg").split())
    assert "add --svc SVC PORT [CIDR] [--description TEXT] remove" in text
    text = " ".join(help_of(capsys, "instance").split())
    assert "terminate --svc SVC [INSTANCE_ID]... [--async] [--yes] [--profile" in text


def test_the_help_of_a_command_lists_its_options(
    wide: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = wide
    text = help_of(capsys, "app", "deploy")
    assert text.startswith("Usage: temt app deploy [OPTIONS] APP [NAME=VALUE]...\n")
    options = [line.split()[0] for line in text.splitlines() if line.startswith("  --")]
    assert options == [
        "--svc",
        "--user",
        "--bake",
        "--reset",
        "--yes",
        "--profile",
        "--region",
        "--help",
    ]
    assert "[required]" in text
    # 短縮形は、打った名前で使い方を出す
    assert help_of(capsys, "status").startswith("Usage: temt status [OPTIONS]\n")


@pytest.mark.parametrize(
    "args", [[], *([t] for t in cli.cli.commands), ["app", "deploy"], ["ssh", "set-port"]]
)
def test_every_help_ends_with_the_link_to_the_docs(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    assert help_of(capsys, *args).endswith(f"\n\n  {cli.EPILOG}\n")
    path = cli.DOCS_URL.split("/blob/main/", 1)[1]
    assert (Path(__file__).parents[1] / path).is_file()


def test_the_help_comes_first_and_wins(capsys: pytest.CaptureFixture[str]) -> None:
    """--help は、ほかの引数の誤りより先に読む。--version より前なら、こちら。"""
    assert help_of(capsys, "start", "--svc", "Bad").startswith("Usage: temt start")
    assert run_temt(["--help", "--version"]) == 0
    assert capsys.readouterr().out.startswith("Usage: temt [OPTIONS]")


def test_version_prints_the_version_of_temt(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_temt(["--version"]) == 0
    assert capsys.readouterr() == (f"temt, version {metadata.version('temt')}\n", "")


@pytest.mark.parametrize("args", [[], ["app"]])
def test_a_missing_command_prints_the_help_as_an_error(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    assert run_temt(args) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(f"Usage: temt {' '.join([*args, ''])}[OPTIONS] COMMAND [ARGS]...\n")
    assert "Commands:" in err


def test_a_usage_error_names_the_command_and_its_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_temt(["start"]) == 2
    assert capsys.readouterr() == (
        "",
        "Usage: temt start [OPTIONS]\n"
        "Try 'temt start --help' for help.\n"
        "\n"
        "Error: Missing option '--svc'.\n",
    )


def test_main_names_the_program_temt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """呼ばれた名前によらず、使い方には temt と書く。サービスも決めない。"""
    monkeypatch.setattr(sys, "argv", ["/opt/bin/temt-dev-main", "status"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    assert capsys.readouterr().err.startswith("Usage: temt status [OPTIONS]\n")


def test_main_wraps_an_error_from_aws(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(service: Service, *, image: str | None, yes: bool) -> None:
        _ = service
        raise ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "not authorized"}}, "CreateRole"
        )

    monkeypatch.setattr(apply, "run", broken)
    assert run_temt(["apply", *SVC]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: An error occurred (UnauthorizedOperation)")
    assert "not authorized" in err


def test_main_wraps_a_failure_to_reach_aws(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """接続の失敗など、AWS の答えが無い誤り（BotoCoreError）も、1 行で知らせて 1 で終わる。"""

    def broken(service: Service, *, image: str | None, yes: bool) -> None:
        _ = service
        raise EndpointConnectionError(endpoint_url="https://ec2.ap-northeast-1.amazonaws.com/")

    monkeypatch.setattr(apply, "run", broken)
    assert run_temt(["apply", *SVC]) == 1
    assert capsys.readouterr().err == (
        'Error: Could not connect to the endpoint URL: "https://ec2.ap-northeast-1.amazonaws.com/"\n'
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

    def broken(service: Service, *, image: str | None, yes: bool) -> None:
        _ = service
        raise error

    monkeypatch.setattr(apply, "run", broken)
    assert run_temt(["apply", *SVC]) == 1
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

    def broken(service: Service, *, image: str | None, yes: bool) -> None:
        _ = service
        raise error

    monkeypatch.setattr(apply, "run", broken)
    assert run_temt(["apply", *SVC]) == 1
    err = capsys.readouterr().err
    assert "give --profile, set AWS_PROFILE" in err
    assert f"or write AwsProfile in {tilde(service.conf_path)}" in err


def test_main_points_at_the_written_profile(
    service: Service, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """設定ファイルに書いた AwsProfile は固定なので、--profile や AWS_PROFILE は勧めない。"""

    def broken(service: Service, *, image: str | None, yes: bool) -> None:
        _ = service
        raise ProfileNotFound(profile="work")

    service.conf_path.parent.mkdir(parents=True)
    service.conf_path.write_text('AwsProfile = "work"\n', encoding="utf-8")
    monkeypatch.setattr(apply, "run", broken)
    assert run_temt(["apply", *SVC]) == 1
    assert capsys.readouterr().err.endswith(f"; check AwsProfile in {tilde(service.conf_path)}\n")


def test_main_says_where_the_profile_can_be_chosen_without_a_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(service: Service | None, region: str | None, *, yes: bool) -> None:
        raise NoCredentialsError()

    monkeypatch.setattr(images, "prune", fail)
    assert run_temt(["ami", "prune"]) == 1
    assert capsys.readouterr().err.endswith(
        f"; give --profile, write AwsProfile in {tilde(defaults_path())}, or set AWS_PROFILE\n"
    )


def test_main_shows_a_file_error_like_other_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ファイルの読み書きの失敗は、パスの表示できない文字を ? にして知らせる。"""

    def fail(service: Service | None, spec: str | None) -> None:
        raise PermissionError(13, "Permission denied", "/srv/a\udcff\x1bb")

    monkeypatch.setattr(services, "list_services", fail)
    assert run_temt(["service", "list"]) == 1
    assert capsys.readouterr().err == "Error: Permission denied: /srv/a??b\n"


def test_main_reports_an_interrupt(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C で中断されたときは、^C の行を閉じてから、中断したと述べて 1 で終わる。"""

    def broken(service: Service, *, image: str | None, yes: bool) -> None:
        _ = service
        raise KeyboardInterrupt

    monkeypatch.setattr(apply, "run", broken)
    assert run_temt(["apply", *SVC]) == 1
    assert capsys.readouterr().err == "\nAborted!\n"


def test_main_reports_a_missing_module(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(service: Service, *, image: str | None, yes: bool) -> None:
        _ = service
        raise ImportError("No module named 'boto3'")

    monkeypatch.setattr(apply, "run", broken)
    assert run_temt(["apply", *SVC]) == 1
    assert capsys.readouterr().err == "Error: No module named 'boto3'; reinstall temt\n"


def test_main_takes_the_region_out_of_the_environment(
    service: Service, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """リージョンの環境変数だけを取り除く。プロファイルは AWS の既定の規則に任せる。"""
    monkeypatch.setenv("AWS_PROFILE", "work")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # 設定ファイルが無いので、AWS に触れる前に 1 で終わる
    assert run_temt(["status", *SVC]) == 1
    assert capsys.readouterr().err == (
        f"Error: no config file: {tilde(service.conf_path)}; "
        "run 'temt apply --svc dev-main' first\n"
    )
    assert not [name for name in conf.REGION_ENV if name in os.environ]
    assert os.environ["AWS_PROFILE"] == "work"


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
    temt: Temt, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str | None
) -> None:
    """--svc の無いコマンドのため、取り除く前に aws-cli と同じ順で控える。"""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    temt.call("ami", "prune")
    assert expected == conf.ENV_REGION
    assert not [name for name in conf.REGION_ENV if name in os.environ]


def test_main_ends_service_list_with_1_after_skipping(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home_dir
    Service("web").conf_path.parent.mkdir(parents=True)
    (Service("web").conf_path.parent / "Odd.toml").write_text("", encoding="utf-8")
    Service("web").conf_path.write_text("", encoding="utf-8")
    assert run_temt(["service", "list", "--format", "service"]) == 1
    out, err = capsys.readouterr()
    assert out == "SERVICE\nweb\n"
    assert err.endswith("Error: skipped 1 of 2 config files\n")


def test_a_usage_error_found_while_running_names_the_command(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """引数を読んだあとで分かる引数の誤りも、そのコマンドの使い方を添えて 2 で終わる。"""
    _ = home_dir
    assert run_temt(["service", "list", "--format", "color"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Usage: temt service list [OPTIONS]\nTry 'temt service list --help'")
    assert "\nError: unknown column in --format: color; use SERVICE," in err


@pytest.mark.parametrize("command", ["service list", "app list"])
def test_a_local_command_runs_without_boto3(
    temt: Temt, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """AWS を呼ばないコマンドは、botocore を読み込めなくても動く。"""
    monkeypatch.setitem(sys.modules, "botocore.exceptions", None)
    temt.call(*command.split(), *SVC)
    temt.call("sg", "add", "22", "--description", "home", *SVC)


def run_python(code: str, home_dir: Path) -> subprocess.CompletedProcess[str]:
    """新しいインタプリタで code を動かす。"""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": str(home_dir)},
        timeout=60,
    )


@pytest.mark.parametrize("args", [["no-such-command"], ["start"], ["start", "--svc", "dev-main"]])
def test_main_with_stderr_closed_still_exits_with_the_code(home_dir: Path, args: list[str]) -> None:
    """起動したときから標準エラー出力が閉じていても（2>&-）、落ちずに終了コードを返す。
    エラーは捨て、標準出力には書かない。
    """
    script = f"import sys; sys.argv = ['temt', *{args!r}]; from temt import cli; cli.main()"
    done = subprocess.run(
        ["sh", "-c", '"$0" -c "$1" 2>&-', sys.executable, script],
        capture_output=True,
        check=False,
        env={**os.environ, "HOME": str(home_dir)},
    )
    assert (done.returncode, done.stdout) == (1 if len(args) > 1 else 2, b"")


def test_the_egress_lookup_is_not_imported_by_every_command(home_dir: Path) -> None:
    """ほかのコマンドは、sg list、sg add、sg remove が使う ingress を読み込まない。

    ingress は urllib を読み込むので、ssh の接続のたびに走る proxy などの起動を遅らせ
    ないためである。ここでは、AWS を呼ばない service list で確かめる。
    """
    code = (
        "import sys\n"
        "from temt import cli\n"
        "cli.cli.main(['service', 'list'], prog_name='temt', standalone_mode=False)\n"
        "sys.stderr.write(str('temt.ingress' in sys.modules))\n"
    )
    done = run_python(code, home_dir)
    assert (done.returncode, done.stderr) == (0, "False")


@pytest.mark.parametrize(("command", "head"), [("service list", "SERVICE "), ("app list", "ID ")])
def test_a_local_command_does_not_import_boto3(home_dir: Path, command: str, head: str) -> None:
    """import の連鎖が boto3 を読まないことを、新しいインタプリタで確かめる。"""
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None\n"
        "sys.modules['botocore'] = None\n"
        "from temt import cli\n"
        f"cli.cli.main({command.split()!r}, prog_name='temt')\n"
    )
    done = run_python(code, home_dir)
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.startswith(head)


def test_service_list_stops_quietly_when_the_reader_goes_away(home_dir: Path) -> None:
    """読み手が先に閉じても、誤りを出さずに 0 で終わる。"""
    code = "from temt import cli\ncli.cli.main(['service', 'list'], prog_name='temt')\n"
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
