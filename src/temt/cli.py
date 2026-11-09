"""コマンドラインの入口。click で temt <対象> <動詞> [引数] [コマンドラインオプション] を読む。

対象は click のグループ、動詞はコマンドで、短縮形は、表すコマンドを別の名前で temt に
足したものである。--svc、--profile、--region だけは、temt の直後にも、コマンドの後ろにも
置ける。サービスのコマンドは --svc を temt の直後に置き、ssh の ProxyCommand はコマンドの
後ろに置く。

コマンドの関数には docstring を書かない。click が使い方に出すので、コメントで書く。
"""

import functools
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click
from click.core import ParameterSource

from temt import conf, msg, options
from temt.msg import TemtError, UsageError
from temt.names import (
    INSTANCE_ID_RULE,
    LABEL_RULE,
    PREFIX,
    USER_NAME_RULE,
    Service,
    app_dir,
    defaults_path,
    is_instance_id,
    is_label,
    is_user_name,
)
from temt.table import aws_error, os_error, printable, tilde

DOCS_URL = "https://github.com/izumo-m/temt/blob/main/docs/usage.md"
# 使い方の末尾に書く、詳しい使い方の文書の場所
EPILOG = f"More: {DOCS_URL}"
DEFAULT_PORT = 22
PORT_MAX = 65535
PORT = click.IntRange(1, PORT_MAX)

F = TypeVar("F", bound=Callable[..., Any])


class _Text(click.ParamType):
    """空でない値。utf8 なら、AWS に送れるよう、UTF-8 であることも求める。"""

    name = "text"

    def __init__(self, *, utf8: bool = False) -> None:
        self.utf8 = utf8

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if value == "":
            self.fail("must not be empty", param, ctx)
        if self.utf8 and not options.is_utf8(value):
            self.fail(f"'{printable(value)}' is not valid UTF-8", param, ctx)
        return value


class _ServiceName(click.ParamType):
    """--svc。サービス名の規則で確かめ、サービスにする。"""

    name = "svc"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if isinstance(value, Service):
            return value
        try:
            return Service(value)
        except UsageError as e:
            self.fail(e.message, param, ctx)


class _Label(click.ParamType):
    """アプリ ID とキー。"""

    name = "label"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if not is_label(value):
            self.fail(f"'{printable(value)}' {LABEL_RULE}", param, ctx)
        return value


class _UserName(click.ParamType):
    """--user。install と uninstall を実行するインスタンスのユーザー。"""

    name = "user"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if not is_user_name(value):
            self.fail(f"'{printable(value)}' {USER_NAME_RULE}", param, ctx)
        return value


class _InstanceId(click.ParamType):
    """インスタンス ID。"""

    name = "instance_id"

    def get_metavar(self, param: click.Parameter, ctx: click.Context) -> str | None:
        return "INSTANCE_ID"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if not is_instance_id(value):
            self.fail(f"'{printable(value)}' {INSTANCE_ID_RULE}", param, ctx)
        return value


class _RulePort(click.ParamType):
    """sg add と sg remove の <ポート>。プロトコルとポートの範囲（icmp では None）にする。"""

    name = "port"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if isinstance(value, tuple):
            return value
        try:
            return conf.parse_port(value)
        except ValueError as e:
            self.fail(f"'{printable(value)}' {e}", param, ctx)


class _Cidr(click.ParamType):
    """sg add と sg remove の <CIDR>。正規化する。"""

    name = "cidr"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        try:
            return conf.cidr(value)
        except ValueError as e:
            self.fail(str(e), param, ctx)


class _Description(click.ParamType):
    """sg add の --description。空なら説明を消す。"""

    name = "text"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        try:
            conf.check_description(value)
        except ValueError as e:
            self.fail(str(e), param, ctx)
        return value


class _KeyValue(click.ParamType):
    """app set の <キー>=<値>。最初の = で分ける。キーの規則に = は無いので、値は = を
    含んでもよい。
    """

    name = "key_value"

    def get_metavar(self, param: click.Parameter, ctx: click.Context) -> str | None:
        return "KEY=VALUE"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if isinstance(value, tuple):
            return value
        key, sep, text = value.partition("=")
        if not sep:
            self.fail(f"'{printable(value)}' must be KEY=VALUE", param, ctx)
        if not is_label(key):
            self.fail(f"'{printable(key)}' {LABEL_RULE}", param, ctx)
        if not text:
            # SSM は空の値を受け付けない
            self.fail("the value must not be empty", param, ctx)
        if not options.is_utf8(text):
            # そのまま書くと、app get が出せない値になる
            self.fail("the value is not valid UTF-8", param, ctx)
        return key, text


class _AppOption(click.ParamType):
    """app deploy のアプリの後ろの <名前>=<値>。

    最初の = で分ける。名前の規則に = は無いので、値は = を含んでもよい。値は加工せず、
    - で始まる値もそのまま受け取る。空の値は、その名前を既定に戻す指定で None とする。
    名前が宣言されているかと、値が pattern に合うかは、deploy が manifest.toml と照らす。
    """

    name = "app_option"

    def get_metavar(self, param: click.Parameter, ctx: click.Context) -> str | None:
        return "NAME=VALUE"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if isinstance(value, tuple):
            return value
        name, sep, text = value.partition("=")
        if not sep:
            self.fail(f"'{printable(value)}' must be NAME=VALUE", param, ctx)
        if not is_label(name):
            self.fail(f"'{printable(name)}' {LABEL_RULE}", param, ctx)
        if not options.is_utf8(text):
            # UTF-8 でないバイト列は、Python が対にならないサロゲートとして読む
            self.fail(f"the value of {name} is not valid UTF-8", param, ctx)
        if options.has_control(text):
            self.fail(f"the value of {name} {options.VALUE_RULE}", param, ctx)
        return name, text or None


class _DeployApp(click.ParamType):
    """app deploy のアプリ。/ を含めばパスとし、最後の要素をアプリ ID として規則で確かめる。"""

    name = "app"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if "/" in value:
            name = app_dir(value).name
            if not is_label(name):
                self.fail(
                    f"'{printable(name)}' (the last part of '{printable(value)}') {LABEL_RULE}",
                    param,
                    ctx,
                )
        elif "=" in value:
            self.fail(
                f"give the app id or path before the app options: '{printable(value)}'", param, ctx
            )
        elif not is_label(value):
            self.fail(f"'{printable(value)}' {LABEL_RULE}", param, ctx)
        return value


TEXT = _Text()
AWS_TEXT = _Text(utf8=True)
SERVICE = _ServiceName()
LABEL = _Label()


def _unique(ctx: click.Context, param: click.Parameter, values: tuple[Any, ...]) -> Any:
    """並びの引数のコールバック。同じものを 2 度与えれば誤りとする。<名前>=<値> は名前で比べる。"""
    seen: list[Any] = []
    for value in values:
        key = value[0] if isinstance(value, tuple) else value
        if key in seen:
            raise click.BadParameter(f"'{key}' is given twice", ctx, param)
        seen.append(key)
    return values


def _single(ctx: click.Context, param: click.Parameter, values: tuple[Any, ...]) -> Any:
    """temt の直後の --svc のコールバック。重ねれば誤りとする。

    サービスのコマンドは temt --svc <svc> "$@" なので、利用者が --svc を付けると重なる。
    click のとおり後の値を使うと、黙って別のサービスを扱ってしまう。
    """
    if len(values) > 1:
        raise click.UsageError(f"{param.opts[0]} is given more than once.", ctx)
    return values[0] if values else None


def _from_root(name: str) -> Callable[[], Any]:
    """コマンドの後ろの --svc、--profile、--region の既定。temt の直後に置いた値とする。"""
    return lambda: click.get_current_context().find_root().params.get(name)


def _once(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
    """コマンドの後ろの --svc、--profile、--region のコールバック。temt の直後にも置いて
    いれば誤りとする。

    既定は temt の直後に置いた値で、どちらにも無ければ None になる。click は None を値が
    無いとはみなさないので、要るかどうかはここで確かめる。
    """
    name = param.name or ""
    given = ctx.get_parameter_source(name) is ParameterSource.COMMANDLINE
    if given and ctx.find_root().params.get(name) is not None:
        raise click.UsageError(f"{param.opts[0]} is given both before and after the command.", ctx)
    if param.required and value is None:
        raise click.MissingParameter(ctx=ctx, param=param)
    return value


def _svc(*, required: bool = True) -> Callable[[F], F]:
    """--svc。required でなければ省け、省けばすべてのサービスのものを扱う。"""
    return click.option(
        "--svc",
        "service",
        type=SERVICE,
        required=required,
        default=_from_root("service"),
        callback=_once,
    )


def _profile_hint(service: Service | None) -> str:
    """資格情報でつまずいたときに、プロファイルを選べる場所を伝える。"""
    if service is None:
        return (
            f"give --profile, write AwsProfile in {printable(tilde(defaults_path()))}, "
            f"or set AWS_PROFILE"
        )
    if conf.written(service, "AwsProfile"):
        # 設定ファイルに書いた AwsProfile は固定で、--profile でも環境変数でも替わらない
        return f"check AwsProfile in {printable(tilde(service.conf_path))}"
    return (
        f"give --profile, set AWS_PROFILE, or write AwsProfile in "
        f"{printable(tilde(conf.source(service, 'AwsProfile')))}"
    )


def _aws(f: Callable[..., None]) -> Callable[..., None]:
    """AWS を呼ぶコマンド。--profile と --region を足し、AWS の誤りを包む。

    使うプロファイルとリージョンは、--profile、--region と設定ファイルで決めるので、値は
    conf に渡し、f には渡さない。AWS を使う module は f の中で読み込む。module の先頭で
    読み込むと、boto3 が無いときに AWS を呼ばないコマンドまで動かなくなる。
    """

    @functools.wraps(f)
    def run(*, profile: str | None, region: str | None, **kwargs: Any) -> None:
        conf.OPTION_PROFILE = profile
        conf.OPTION_REGION = region
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            ProfileNotFound,
        )

        try:
            f(**kwargs)
        except (ProfileNotFound, NoCredentialsError) as e:
            raise TemtError(f"{aws_error(e)}; {_profile_hint(kwargs.get('service'))}") from e
        except (ClientError, BotoCoreError) as e:
            raise TemtError(aws_error(e)) from e

    # click は、下に書いたオプションほど後ろに並べる
    for flag, metavar in (("--region", "REGION"), ("--profile", "PROFILE")):
        name = flag.removeprefix("--")
        run = click.option(
            flag, type=TEXT, metavar=metavar, default=_from_root(name), callback=_once
        )(run)
    return run


def _local(f: Callable[..., None]) -> Callable[..., None]:
    """AWS を呼ばないコマンド。temt の直後に置いた --profile と --region も誤りとする。"""

    @functools.wraps(f)
    def run(**kwargs: Any) -> None:
        ctx = click.get_current_context()
        for flag in ("--profile", "--region"):
            if ctx.find_root().params.get(flag.removeprefix("--")) is not None:
                raise click.UsageError(f"{flag} is not allowed; it does not call AWS.", ctx)
        f(**kwargs)

    return run


_yes = click.option("--yes", is_flag=True)
_async = click.option("--async", "is_async", is_flag=True)
_format = click.option("--format", "columns", type=TEXT, metavar="COLUMNS")
_user = click.option("--user", type=_UserName())
_bake = click.option("--bake", is_flag=True)


def _epilog(formatter: click.HelpFormatter) -> None:
    """使い方の末尾に、詳しい使い方の文書の場所を書く。"""
    formatter.write_paragraph()
    with formatter.indentation():
        formatter.write_text(EPILOG)


def _synopsis(command: click.Command, ctx: click.Context) -> str:
    """対象の使い方に書く、動詞の後ろに取る引数とコマンドラインオプション。"""
    pieces: list[str] = []
    for param in command.params:
        if isinstance(param, click.Argument):
            pieces.append(param.make_metavar(ctx))
        elif isinstance(param, click.Option) and not param.hidden:
            word = param.opts[0]
            if not param.is_flag:
                word += f" {param.make_metavar(ctx)}"
            pieces.append(word if param.required else f"[{word}]")
    return " ".join(pieces)


class _Command(click.Command):
    """コマンド。"""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as e:
            # click は余分な引数を、表示できない文字もそのまま誤りに入れる。値の誤り
            # （BadParameter など）は、temt の文か、click が repr で書いた文で、そのままでよい
            if type(e) is not click.UsageError:
                raise
            raise click.UsageError(printable(e.message), e.ctx) from None

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        _epilog(formatter)


class _Target(click.Group):
    """対象。動詞を登録した順に、取る引数とコマンドラインオプションを添えて並べる。"""

    command_class = _Command

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.commands)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows = [(name, _synopsis(command, ctx)) for name, command in self.commands.items()]
        with formatter.section("Commands"):
            formatter.write_dl(rows)

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        _epilog(formatter)


class _Root(click.Group):
    """temt。対象ごとに動詞を並べ、短縮形を添える。"""

    group_class = _Target

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        targets = {n: c for n, c in self.commands.items() if isinstance(c, click.Group)}
        with formatter.section("Commands"):
            formatter.write_dl([(name, " ".join(t.commands)) for name, t in targets.items()])
        # 動詞と同じ名前の短縮形は対象ごとにまとめ、違うものは表すコマンドを添える
        same: dict[str, list[str]] = {}
        others: list[str] = []
        for form, command in self.commands.items():
            target = next((n for n, t in targets.items() if command in t.commands.values()), None)
            if target is None:
                continue
            if form == command.name:
                same.setdefault(target, []).append(form)
            else:
                others.append(f"{form} ({target} {command.name})")
        with formatter.section("Short forms"):
            formatter.write_text(
                ", ".join([*(f"{' '.join(f)} ({t})" for t, f in same.items()), *others])
            )

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        _epilog(formatter)

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except BrokenPipeError:
            # 読み手が先に閉じた。click が何も言わずに終える
            raise
        except OSError as e:
            # ファイルやディレクトリの読み書きの失敗
            raise TemtError(os_error(e)) from e
        except ImportError as e:
            # boto3 などが入っていないとき。取り込みは各コマンドの中で行う
            raise TemtError(f"{e}; reinstall temt") from e


@click.group(cls=_Root, name=PREFIX)
@click.option("--svc", "service", type=SERVICE, multiple=True, callback=_single)
@click.option("--profile", type=TEXT, metavar="PROFILE")
@click.option("--region", type=TEXT, metavar="REGION")
@click.version_option(package_name=PREFIX)
def cli(**_options: Any) -> None:
    # どのリージョンを使うかは temt が決めて渡す
    conf.take_region_env()


@cli.group("service")
def service_group() -> None:
    pass


@service_group.command("apply")
@_svc()
@click.option("--image-id", "image", type=AWS_TEXT, metavar="AMI")
@_yes
@_aws
def service_apply(service: Service, image: str | None, yes: bool) -> None:
    from temt import apply

    apply.run(service, image=image, yes=yes)


@service_group.command("diff")
@_svc()
@click.option("--image-id", "image", type=AWS_TEXT, metavar="AMI")
@_aws
def service_diff(service: Service, image: str | None) -> None:
    from temt import apply

    apply.diff(service, image=image)


@service_group.command("destroy")
@_svc()
@_yes
@_aws
def service_destroy(service: Service, yes: bool) -> None:
    from temt import destroy

    destroy.run(service, yes=yes)


@service_group.command("list")
@_svc(required=False)
@_format
@_local
def service_list(service: Service | None, columns: str | None) -> None:
    from temt import services

    services.list_services(service, columns)


@service_group.command("import")
@_svc()
@click.argument("instance_id", type=_InstanceId())
@_yes
@_aws
def service_import(service: Service, instance_id: str, yes: bool) -> None:
    from temt import importing

    importing.run(service, instance_id, yes=yes)


@cli.group("instance")
def instance_group() -> None:
    pass


@instance_group.command("start")
@_svc()
@_async
@_aws
def instance_start(service: Service, is_async: bool) -> None:
    from temt import start

    start.run(service, is_async=is_async)


@instance_group.command("stop")
@_svc()
@_async
@_yes
@_aws
def instance_stop(service: Service, is_async: bool, yes: bool) -> None:
    from temt import stop

    stop.run(service, is_async=is_async, yes=yes)


@instance_group.command("terminate")
@_svc()
@click.argument("instance_ids", nargs=-1, type=_InstanceId(), callback=_unique)
@_async
@_yes
@_aws
def instance_terminate(
    service: Service, instance_ids: tuple[str, ...], is_async: bool, yes: bool
) -> None:
    from temt import terminate

    terminate.run(service, ids=instance_ids, is_async=is_async, yes=yes)


@instance_group.command("reboot")
@_svc()
@_async
@_yes
@_aws
def instance_reboot(service: Service, is_async: bool, yes: bool) -> None:
    from temt import reboot

    reboot.run(service, is_async=is_async, yes=yes)


@instance_group.command("show")
@_svc()
@click.option("--apps", is_flag=True)
@_aws
def instance_show(service: Service, apps: bool) -> None:
    from temt import status

    status.run(service, apps=apps)


@instance_group.command("list")
@_svc(required=False)
@_format
@_aws
def instance_list(service: Service | None, columns: str | None) -> None:
    from temt import status

    status.list_instances(service, conf.OPTION_REGION, columns)


@cli.group("ami")
def ami_group() -> None:
    pass


@ami_group.command("bake")
@_svc()
@_yes
@_aws
def ami_bake(service: Service, yes: bool) -> None:
    from temt import bake

    bake.run(service, yes=yes)


@ami_group.command("list")
@_svc(required=False)
@_format
@_aws
def ami_list(service: Service | None, columns: str | None) -> None:
    from temt import images

    images.list_images(service, conf.OPTION_REGION, columns)


@ami_group.command("prune")
@_svc(required=False)
@_yes
@_aws
def ami_prune(service: Service | None, yes: bool) -> None:
    from temt import images

    images.prune(service, conf.OPTION_REGION, yes=yes)


@ami_group.command("delete")
@_svc(required=False)
@click.argument("ami", type=AWS_TEXT)
@click.option("--force", is_flag=True)
@_yes
@_aws
def ami_delete(service: Service | None, ami: str, force: bool, yes: bool) -> None:
    from temt import images

    images.delete(ami, service, conf.OPTION_REGION, force=force, yes=yes)


@cli.group("template")
def template_group() -> None:
    pass


@template_group.command("show")
@_svc()
@_aws
def template_show(service: Service) -> None:
    from temt import show

    show.template(service)


@cli.group("role")
def role_group() -> None:
    pass


@role_group.command("import")
@_svc()
@click.argument("role", type=AWS_TEXT)
@_yes
@_aws
def role_import(service: Service, role: str, yes: bool) -> None:
    from temt import role as roles

    roles.import_role(service, role, yes=yes)


@role_group.command("delete")
@_svc()
@_yes
@_aws
def role_delete(service: Service, yes: bool) -> None:
    from temt import role as roles

    roles.delete(service, yes=yes)


@cli.group("sg")
def sg_group() -> None:
    pass


@sg_group.command("list")
@_svc()
@_format
@_local
def sg_list(service: Service, columns: str | None) -> None:
    from temt import ingress

    ingress.list_rules(service, columns)


@sg_group.command("add")
@_svc()
@click.argument("port", type=_RulePort())
@click.argument("cidr", type=_Cidr(), required=False)
@click.option("--description", type=_Description())
@_local
def sg_add(
    service: Service,
    port: tuple[str, conf.RULE_PORT],
    cidr: str | None,
    description: str | None,
) -> None:
    from temt import ingress

    ingress.add(service, *port, cidr, description)


@sg_group.command("remove")
@_svc()
@click.argument("port", type=_RulePort())
@click.argument("cidr", type=_Cidr(), required=False)
@_local
def sg_remove(service: Service, port: tuple[str, conf.RULE_PORT], cidr: str | None) -> None:
    from temt import ingress

    ingress.remove(service, *port, cidr)


@sg_group.command("diff")
@_svc()
@_aws
def sg_diff(service: Service) -> None:
    from temt import sg

    sg.diff(service)


@sg_group.command("show")
@_svc()
@_aws
def sg_show(service: Service) -> None:
    from temt import sg

    sg.show(service)


@cli.group("app")
def app_group() -> None:
    pass


# app list は --svc を付けても同じ手元のアプリを挙げる。付けられるのは、サービスの
# コマンドから打てるようにするためである
@app_group.command("list")
@_svc(required=False)
@_format
@_local
def app_list(service: Service | None, columns: str | None) -> None:
    from temt import manifest

    manifest.list_apps(columns)


@app_group.command("show")
@_svc()
@click.argument("app_id", type=LABEL)
@_aws
def app_show(service: Service, app_id: str) -> None:
    from temt import app

    app.show(service, app_id)


@app_group.command("get")
@_svc()
@click.argument("app_id", type=LABEL)
@click.argument("key", type=LABEL)
@_aws
def app_get(service: Service, app_id: str, key: str) -> None:
    from temt import app

    app.get(service, app_id, key)


@app_group.command("set")
@_svc()
@click.argument("app_id", type=LABEL)
@click.argument("pair", type=_KeyValue())
@_aws
def app_set(service: Service, app_id: str, pair: tuple[str, str]) -> None:
    from temt import app

    app.set_value(service, app_id, *pair)


@app_group.command("unset")
@_svc()
@click.argument("app_id", type=LABEL)
@click.argument("key", type=LABEL)
@_aws
def app_unset(service: Service, app_id: str, key: str) -> None:
    from temt import app

    app.unset(service, app_id, key)


@app_group.command("deploy")
@_svc()
@click.argument("app", type=_DeployApp())
@click.argument("app_options", nargs=-1, type=_AppOption(), callback=_unique)
@_user
@_bake
@click.option("--reset", is_flag=True)
@_yes
@_aws
def app_deploy(
    service: Service,
    app: str,
    app_options: tuple[tuple[str, str | None], ...],
    user: str | None,
    bake: bool,
    reset: bool,
    yes: bool,
) -> None:
    from temt import deploy

    deploy.run(service, app, user=user, baking=bake, yes=yes, given=dict(app_options), reset=reset)


@app_group.command("remove")
@_svc()
@click.argument("app_id", type=LABEL)
@_user
@_bake
@_yes
@_aws
def app_remove(service: Service, app_id: str, user: str | None, bake: bool, yes: bool) -> None:
    from temt import deploy

    deploy.remove(service, app_id, user=user, baking=bake, yes=yes)


@cli.group("ssh")
def ssh_group() -> None:
    pass


# ssh の ProxyCommand として動く。ssh は temt proxy --svc <svc> --user '%r' %p を呼ぶ
@ssh_group.command("proxy")
@_svc()
@click.argument("port", type=PORT, default=DEFAULT_PORT, required=False)
@_user
@_aws
def ssh_proxy(service: Service, port: int, user: str | None) -> None:
    from temt import proxy

    # ProxyCommand の標準出力は ssh の通信路なので、何も書かない
    msg.use_stderr()
    proxy.run(service, port, user)


@ssh_group.command("set-port")
@_svc()
@click.argument("ports", nargs=-1, required=True, type=PORT, metavar="PORT...", callback=_unique)
@_yes
@_aws
def ssh_set_port(service: Service, ports: tuple[int, ...], yes: bool) -> None:
    from temt import setport

    setport.run(service, ports, yes=yes)


# 短縮形と、それが表すコマンド
SHORT_FORMS = {
    "apply": service_apply,
    "diff": service_diff,
    "destroy": service_destroy,
    "start": instance_start,
    "stop": instance_stop,
    "terminate": instance_terminate,
    "reboot": instance_reboot,
    "status": instance_show,
    "bake": ami_bake,
    "deploy": app_deploy,
    "proxy": ssh_proxy,
}
for _form, _command in SHORT_FORMS.items():
    cli.add_command(_command, _form)


def main() -> None:
    """temt の入口。呼ばれた名前によらず、使い方には temt と書く。

    起動したときから標準エラー出力が閉じていれば（2>&-）、click はエラーを標準出力に
    書いてしまうので、/dev/null に回して捨てる。
    """
    if sys.stderr is None:
        sys.stderr = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115
    cli.main(prog_name=PREFIX)
