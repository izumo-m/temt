"""app list、show、get、set、unset のテスト。AWS は Stubber で差し替える。"""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, ParamValidationError
from conftest import Stubs, an_instance, reservations
from types_boto3_ssm.client import SSMClient

from temt import app, instances, names, resources
from temt.msg import TemtError, UsageError
from temt.names import Service

NAME = "temt-dev-main"
TEMPLATE = f"{NAME}-template"
TEMPLATES = {"LaunchTemplateNames": [TEMPLATE]}
PATH = f"/{NAME}"
RECORD = f"{PATH}/jupyter/deploy"
TOKEN = f"{PATH}/jupyter/token"
WHEN = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")
# 起動テンプレートが指す AMI と、ほかの AMI
IMAGE = "ami-0123456789abcdef0"
OTHER_IMAGE = "ami-0fedcba9876543210"
# サービスのインスタンスと、もう無いインスタンス
INSTANCE = "i-0123456789abcdef0"
GONE = "i-0fedcba9876543210"
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


def placed(stubs: Stubs, *items: dict[str, Any], image: str = IMAGE) -> None:
    """app list が読む、起動テンプレートの既定バージョンの AMI と、インスタンスの探索。"""
    stubs.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {
                    "VersionNumber": 1,
                    "DefaultVersion": True,
                    "LaunchTemplateData": {"ImageId": image},
                }
            ]
        },
        {"LaunchTemplateName": TEMPLATE},
    )
    stubs.ec2.add_response("describe_instances", reservations(*items), SEARCH)


# PATH を除くすべての列
ALL_BUT_PATH = "id,name,version,deployed,state,baked,local"


def a_record(name: str = "Jupyter Lab", version: str = "1.2") -> str:
    """deploy の記録の値。"""
    return json.dumps(
        {"name": name, "version": version, "user": "root", "deployed": "2026-09-26T03:00:00Z"}
    )


def a_parameter(name: str, value: str, kind: str = "String") -> dict[str, Any]:
    """get-parameter と get-parameters-by-path が返すパラメータ 1 つ。"""
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
        lambda service: app.list_apps(service, None),
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


def test_list_shows_the_deploy_records_by_app_id(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """アプリの値と、/<名前>/<アプリID>/deploy でない記録は挙げない。"""
    applied(aws_stubs)
    placed(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(RECORD, a_record()),
                a_parameter(TOKEN, "secret"),
                a_parameter(f"{PATH}/deploy", a_record("top")),
                a_parameter(f"{PATH}/other/sub/deploy", a_record("deep")),
            ],
            "NextToken": "t",
        },
        {"Path": PATH, "Recursive": True},
    )
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": [a_parameter(f"{PATH}/idle-shutdown/deploy", a_record("Idle", "0.1"))]},
        {"Path": PATH, "Recursive": True, "NextToken": "t"},
    )

    app.list_apps(configured, ALL_BUT_PATH)

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "ID              NAME          VERSION   DEPLOYED              STATE   BAKED   LOCAL",
        "idle-shutdown   Idle          0.1       2026-09-26 12:00:00   -       -       -",
        "jupyter         Jupyter Lab   1.2       2026-09-26 12:00:00   -       -       -",
    ]


def a_local_app(home: Path, app_id: str, manifest: str | None) -> None:
    """~/.config/temt/apps/ にアプリを置く。manifest が None なら manifest.toml を置かない。"""
    where = home / ".config" / "temt" / "apps" / app_id
    where.mkdir(parents=True)
    if manifest is not None:
        (where / "manifest.toml").write_text(manifest, encoding="utf-8")


def test_list_adds_the_apps_in_the_apps_directory(
    aws_stubs: Stubs, configured: Service, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """入っていないアプリも挙げ、LOCAL に手元のアプリの版を出す。名前は記録のものを優先する。"""
    applied(aws_stubs)
    placed(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(RECORD, a_record()),
                a_parameter(f"{PATH}/by-path/deploy", a_record("By Path", "3")),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    a_local_app(home_dir, "jupyter", 'name = "Jupyter"\nversion = "1.3"\n')
    a_local_app(home_dir, "mise", 'name = "mise"\nversion = "1"\n')
    a_local_app(home_dir, "Bad", 'name = "bad"\nversion = "1"\n')
    (home_dir / ".config" / "temt" / "apps" / "file").write_text("", encoding="utf-8")

    app.list_apps(configured, ALL_BUT_PATH)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "ID        NAME          VERSION   DEPLOYED              STATE   BAKED   LOCAL",
        "by-path   By Path       3         2026-09-26 12:00:00   -       -       -",
        "jupyter   Jupyter Lab   1.2       2026-09-26 12:00:00   -       -       1.3",
        "mise      mise          -         -                     -       -       1",
    ]
    assert captured.err == ""


def test_list_adds_the_apps_that_come_with_temt(
    aws_stubs: Stubs,
    configured: Service,
    home_dir: Path,
    no_bundled_apps: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """temt と一緒に配るアプリも挙げる。PATH を選べば、どこのものかが分かる。"""
    applied(aws_stubs)
    placed(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path", {"Parameters": []}, {"Path": PATH, "Recursive": True}
    )
    a_local_app(home_dir, "mise", 'name = "mise"\nversion = "1"\n')
    bundled = no_bundled_apps / "swapfile"
    bundled.mkdir(parents=True)
    (bundled / "manifest.toml").write_text('name = "swap"\nversion = "0.1"\n', encoding="utf-8")

    app.list_apps(configured, "id,local,path")

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "ID         LOCAL   PATH",
        "mise       1       ~/.config/temt/apps/mise",
        "swapfile   0.1     ~/bundled/swapfile",
    ]


def test_list_warns_about_a_manifest_it_cannot_read(
    aws_stubs: Stubs, configured: Service, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """読めない manifest.toml は、警告してアプリ ID だけを出し、残りを並べる。"""
    applied(aws_stubs)
    placed(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path", {"Parameters": []}, {"Path": PATH, "Recursive": True}
    )
    a_local_app(home_dir, "broken", 'name = "broken"\n')
    a_local_app(home_dir, "empty", None)
    a_local_app(home_dir, "folder", None)
    (home_dir / ".config" / "temt" / "apps" / "folder" / "manifest.toml").mkdir()
    a_local_app(home_dir, "mise", 'name = "mise"\nversion = "1"\n')

    app.list_apps(configured, ALL_BUT_PATH)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "ID       NAME   VERSION   DEPLOYED   STATE   BAKED   LOCAL",
        "broken   -      -         -          -       -       -",
        "empty    -      -         -          -       -       -",
        "folder   -      -         -          -       -       -",
        "mise     mise   -         -          -       -       1",
    ]
    assert told(captured.err) == [
        "temt: warning: cannot read the manifest of broken: "
        "~/.config/temt/apps/broken/manifest.toml: version is missing",
        "temt: warning: cannot read the manifest of empty: "
        "no manifest.toml in ~/.config/temt/apps/empty",
        "temt: warning: cannot read the manifest of folder: "
        "~/.config/temt/apps/folder/manifest.toml: Is a directory",
    ]


def test_list_follows_links_and_takes_the_name_of_an_unreadable_record(
    aws_stubs: Stubs, configured: Service, home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ディレクトリへのリンクは挙げ、壊れたリンクは挙げない。

    読めない記録の名前は manifest から取る。manifest も無ければ - と出す。
    """
    applied(aws_stubs)
    placed(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(RECORD, "[1]"),
                a_parameter(f"{PATH}/other/deploy", "[2]"),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    a_local_app(home_dir / "elsewhere", "jupyter", 'name = "Jupyter"\nversion = "2"\n')
    apps = home_dir / ".config" / "temt" / "apps"
    apps.mkdir(parents=True)
    (apps / "jupyter").symlink_to(home_dir / "elsewhere" / ".config" / "temt" / "apps" / "jupyter")
    (apps / "gone").symlink_to(home_dir / "nothing")

    app.list_apps(configured, "id,name,local")

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "ID        NAME      LOCAL",
        "jupyter   Jupyter   2",
        "other     -         -",
    ]
    assert told(captured.err) == [
        f"temt: warning: cannot read the deploy record {RECORD}: not a JSON object",
        f"temt: warning: cannot read the deploy record {PATH}/other/deploy: not a JSON object",
    ]


def test_list_and_show_keep_a_deploy_time_they_cannot_read(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """記録の時刻が読めなければ、書かれたまま出す。"""
    record = json.dumps({"name": "J", "version": "1", "deployed": "yesterday"})
    applied(aws_stubs)
    placed(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": [a_parameter(RECORD, record)]},
        {"Path": PATH, "Recursive": True},
    )
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(RECORD, record)}, {"Name": RECORD}
    )
    keys(aws_stubs, RECORD)

    app.list_apps(configured, "id,deployed")
    app.show(configured, "jupyter")

    aws_stubs.done()
    captured = capsys.readouterr()
    shown = captured.out.splitlines()
    assert shown[:3] == ["ID        DEPLOYED", "jupyter   yesterday", "app jupyter:"]
    assert "  deployed:      yesterday" in shown[3:]
    assert captured.err == ""


def test_list_checks_the_columns_before_aws(configured: Service) -> None:
    with pytest.raises(UsageError, match="unknown column in --format: bogus"):
        app.list_apps(configured, "bogus")


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


def test_show_prints_the_record_and_the_keys_but_no_value(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(RECORD, a_record())}, {"Name": RECORD}
    )
    keys(aws_stubs, RECORD, TOKEN, f"{PATH}/jupyter/URL")

    app.show(configured, "jupyter")

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "app jupyter:",
        "  name:          Jupyter Lab",
        "  version:       1.2",
        "  user:          root",
        "  deployed:      2026-09-26 12:00:00",
        "  instance:      -",
        "  removed:       -",
        "  options:       -",
        "  baked_version: -",
        "  baked_image:   -",
        "  baked_options: -",
        "keys:",
        # 規則に外れたキーも、書かれたまま挙げる
        "  URL    2026-09-26 12:00:00",
        "  token  2026-09-26 12:00:00",
    ]


def test_show_tells_an_app_that_is_not_deployed(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """外したあとに残った値も見られる。"""
    applied(aws_stubs)
    not_found(aws_stubs, "get_parameter", Name=RECORD)
    keys(aws_stubs, TOKEN)

    app.show(configured, "jupyter")

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "app jupyter: not deployed",
        "keys:",
        "  token  2026-09-26 12:00:00",
    ]


def test_show_tells_that_there_is_no_key(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter",
        {"Parameter": a_parameter(RECORD, json.dumps({"name": "J"}))},
        {"Name": RECORD},
    )
    keys(aws_stubs, RECORD)

    app.show(configured, "jupyter")

    assert capsys.readouterr().out.splitlines() == [
        "app jupyter:",
        "  name:          J",
        "  version:       -",
        "  user:          -",
        "  deployed:      -",
        "  instance:      -",
        "  removed:       -",
        "  options:       -",
        "  baked_version: -",
        "  baked_image:   -",
        "  baked_options: -",
        "keys: none",
    ]


def test_show_stops_when_there_is_nothing(aws_stubs: Stubs, configured: Service) -> None:
    """記録も値も無ければ、アプリ ID の打ち間違いとみなす。"""
    applied(aws_stubs)
    not_found(aws_stubs, "get_parameter", Name=RECORD)
    keys(aws_stubs)

    with pytest.raises(TemtError, match=f"no app jupyter in {NAME}: nothing under {PATH}/jupyter/"):
        app.show(configured, "jupyter")


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


def test_get_can_read_the_deploy_record(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(RECORD, a_record())}, {"Name": RECORD}
    )

    app.get(configured, "jupyter", "deploy")

    assert capsys.readouterr().out == a_record()


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
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(RECORD, a_record())}, {"Name": RECORD}
    )
    aws_stubs.ssm.add_response(
        "put_parameter",
        {"Version": version},
        {"Name": TOKEN, "Value": "a b\n", "Type": "String", "Overwrite": True},
    )

    app.set_value(configured, "jupyter", "token", "a b\n")

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"parameter {TOKEN}: {state}"]


def test_set_stops_when_the_app_is_not_deployed(aws_stubs: Stubs, configured: Service) -> None:
    """アプリ ID の打ち間違いで、誰も読まない値を作らない。"""
    applied(aws_stubs)
    not_found(aws_stubs, "get_parameter", Name=RECORD)

    with pytest.raises(
        TemtError, match=f"no deploy record {RECORD}; the app jupyter is not deployed"
    ):
        app.set_value(configured, "jupyter", "token", "x")

    # put-parameter は積んでいないので、書けば Stubber が失敗する
    aws_stubs.done()


def test_unset_deletes_without_asking(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """記録が無くても消せる。外したあとに残った値を片づけるためである。"""
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


def test_show_reads_every_page_and_hides_control_characters(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """記録とキーはインスタンスからも書けるので、制御文字を端末に届けない。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter",
        {"Parameter": a_parameter(RECORD, a_record(name="J\x1b[2J"))},
        {"Name": RECORD},
    )
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

    app.show(configured, "jupyter")

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "app jupyter:",
        "  name:          J?[2J",
        "  version:       1.2",
        "  user:          root",
        "  deployed:      2026-09-26 12:00:00",
        "  instance:      -",
        "  removed:       -",
        "  options:       -",
        "  baked_version: -",
        "  baked_image:   -",
        "  baked_options: -",
        "keys:",
        "  a?b    2026-09-26 12:00:00",
        "  token  2026-09-26 12:00:00",
    ]


def test_show_warns_about_a_record_it_cannot_read(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(RECORD, "{")}, {"Name": RECORD}
    )
    keys(aws_stubs, RECORD)

    app.show(configured, "jupyter")

    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        f"temt: warning: cannot read the deploy record {RECORD}: not a JSON object",
    ]
    assert captured.out.splitlines() == [
        "app jupyter:",
        "  name:          -",
        "  version:       -",
        "  user:          -",
        "  deployed:      -",
        "  instance:      -",
        "  removed:       -",
        "  options:       -",
        "  baked_version: -",
        "  baked_image:   -",
        "  baked_options: -",
        "keys: none",
    ]


def test_set_stops_when_the_value_has_another_type(aws_stubs: Stubs, configured: Service) -> None:
    """階層の下のパラメータは、上書きで型を変えられない。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(RECORD, a_record())}, {"Name": RECORD}
    )
    aws_stubs.ssm.add_client_error(
        "put_parameter", service_error_code=app.TYPE_MISMATCH, http_status_code=400
    )

    with pytest.raises(
        TemtError,
        match=f"{TOKEN} is not a String; run 'temt app unset --svc dev-main jupyter token' first",
    ):
        app.set_value(configured, "jupyter", "token", "x")


def test_other_errors_are_left_to_the_caller(aws_stubs: Stubs, configured: Service) -> None:
    """ParameterNotFound 以外の誤りは、cli が包む。"""
    for run, method in (
        (lambda: app.set_value(configured, "jupyter", "token", "x"), "get_parameter"),
        (lambda: app.unset(configured, "jupyter", "token"), "delete_parameter"),
        (lambda: app.list_apps(configured, None), "get_parameters_by_path"),
        (lambda: app.show(configured, "jupyter"), "get_parameter"),
        (lambda: app.get(configured, "jupyter", "token"), "get_parameter"),
    ):
        applied(aws_stubs)
        aws_stubs.ssm.add_client_error(
            method, service_error_code="AccessDeniedException", http_status_code=400
        )
        with pytest.raises(ClientError):
            run()
    aws_stubs.done()


def test_set_leaves_other_errors_from_writing_to_the_caller(
    aws_stubs: Stubs, configured: Service
) -> None:
    """値が大きすぎるときなどの誤りは、cli が包む。"""
    applied(aws_stubs)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(RECORD, a_record())}, {"Name": RECORD}
    )
    aws_stubs.ssm.add_client_error(
        "put_parameter", service_error_code="ValidationException", http_status_code=400
    )

    with pytest.raises(ClientError):
        app.set_value(configured, "jupyter", "token", "x")


def a_full_record(version: str = "1", **more: str) -> str:
    """deploy の記録の値。more に instance、baked_version、baked_image を渡す。"""
    record = {"name": "J", "version": version, "user": "root", "deployed": "2026-09-26T03:00:00Z"}
    return json.dumps({**record, **more})


def test_list_shows_whether_each_app_is_baked(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """STATE は焼いたかどうか、BAKED は起動テンプレートの AMI に入っている版。"""
    applied(aws_stubs)
    placed(aws_stubs, an_instance(INSTANCE, "running"))
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(
                    f"{PATH}/baked/deploy", a_full_record(baked_version="1", baked_image=IMAGE)
                ),
                a_parameter(f"{PATH}/fresh/deploy", a_full_record(instance=INSTANCE)),
                a_parameter(
                    f"{PATH}/newer/deploy",
                    a_full_record("2", instance=GONE, baked_version="1", baked_image=IMAGE),
                ),
                a_parameter(
                    f"{PATH}/moved/deploy",
                    a_full_record(baked_version="1", baked_image=OTHER_IMAGE),
                ),
                a_parameter(f"{PATH}/old/deploy", a_full_record()),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )

    app.list_apps(configured, "id,version,state,baked")

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "ID      VERSION   STATE     BAKED",
        "baked   1         baked     1",
        "fresh   1         unbaked   -",
        "moved   1         lost      -",
        "newer   2         lost      1",
        "old     1         -         -",
    ]


def test_list_marks_a_baked_version_that_differs_from_the_local_app(
    aws_stubs: Stubs,
    configured: Service,
    home_dir: Path,
    no_bundled_apps: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """既定の列は ID、BAKED、PATH。手元のアプリの版が焼いた版と違えば、BAKED に印を付ける。

    版は同じかどうかだけを比べるので、手元のほうが古くても付ける。焼いていなければ、
    手元のアプリが無ければ、manifest.toml が読めなければ、付けない。
    """
    applied(aws_stubs)
    placed(aws_stubs)
    baked = {"baked_image": IMAGE}
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(f"{PATH}/newer/deploy", a_full_record(baked_version="1", **baked)),
                a_parameter(f"{PATH}/older/deploy", a_full_record(baked_version="3", **baked)),
                a_parameter(f"{PATH}/same/deploy", a_full_record(baked_version="2", **baked)),
                a_parameter(f"{PATH}/fresh/deploy", a_full_record(instance=GONE)),
                a_parameter(f"{PATH}/gone/deploy", a_full_record(baked_version="1", **baked)),
                a_parameter(f"{PATH}/broken/deploy", a_full_record(baked_version="1", **baked)),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    for app_id in ("newer", "older", "same", "fresh"):
        a_local_app(home_dir, app_id, f'name = "{app_id}"\nversion = "2"\n')
    a_local_app(home_dir, "broken", None)

    app.list_apps(configured, None)

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines() == [
        "ID       BAKED   PATH",
        "broken   1       ~/.config/temt/apps/broken",
        "fresh    -       ~/.config/temt/apps/fresh",
        "gone     1       -",
        "newer    1*      ~/.config/temt/apps/newer",
        "older    3*      ~/.config/temt/apps/older",
        "same     2       ~/.config/temt/apps/same",
    ]


def a_place(*alive: str) -> app.Place:
    """起動テンプレートが IMAGE を指し、alive のインスタンスがある。"""
    return app.Place(image_id=IMAGE, alive=frozenset(alive))


@pytest.mark.parametrize(
    ("record", "where", "state", "baked"),
    [
        pytest.param({"instance": INSTANCE}, a_place(INSTANCE), app.UNBAKED, "", id="unbaked"),
        pytest.param({"instance": GONE}, a_place(INSTANCE), app.LOST, "", id="instance gone"),
        pytest.param(
            {"instance": INSTANCE, "baked_version": "1", "baked_image": IMAGE},
            a_place(INSTANCE),
            app.UNBAKED,
            "1",
            id="newer version unbaked",
        ),
        pytest.param(
            {"instance": GONE, "baked_version": "1", "baked_image": IMAGE},
            a_place(),
            app.LOST,
            "1",
            id="newer version lost",
        ),
        pytest.param(
            {"baked_version": "1", "baked_image": IMAGE}, a_place(), app.BAKED, "1", id="baked"
        ),
        pytest.param(
            {"baked_version": "1", "baked_image": OTHER_IMAGE},
            a_place(),
            app.LOST,
            "",
            id="another image",
        ),
        pytest.param({}, a_place(INSTANCE), "", "", id="unknown"),
        pytest.param(
            {"removed": INSTANCE, "baked_version": "1", "baked_image": IMAGE},
            a_place(INSTANCE),
            app.REMOVED,
            "1",
            id="removed",
        ),
        # apply で別の AMI に替えたので、外したアプリは次のインスタンスに入らない
        pytest.param(
            {"removed": INSTANCE, "baked_version": "1", "baked_image": OTHER_IMAGE},
            a_place(INSTANCE),
            app.LOST,
            "",
            id="removed from another image",
        ),
        # 外したインスタンスが無ければ、AMI の版が次のインスタンスに戻っている
        pytest.param(
            {"removed": GONE, "baked_version": "1", "baked_image": IMAGE},
            a_place(INSTANCE),
            app.BAKED,
            "1",
            id="removal lost",
        ),
        # 版だけあって AMI の無い記録は、どの AMI にも入っていない
        pytest.param({"baked_version": "1"}, a_place(), "", "", id="no image"),
    ],
)
def test_state_and_baked_version(
    record: dict[str, str], where: app.Place, state: str, baked: str
) -> None:
    assert app.state(record, where) == state
    assert app.baked_version(record, where) == baked


def test_place_leaves_out_an_instance_that_is_shutting_down() -> None:
    """終了しかけているインスタンスは、もう無いものとする。"""
    found = [
        instances.Instance(id=INSTANCE, state="stopped", public_ipv4="", kept_devices=(), age=None),
        instances.Instance(
            id=GONE, state="shutting-down", public_ipv4="", kept_devices=(), age=None
        ),
    ]

    where = app.place(IMAGE, found)

    assert where == app.Place(image_id=IMAGE, alive=frozenset({INSTANCE}))


# bake が AMI にするインスタンスと、その起動元の AMI、作った AMI
BAKED_FROM = instances.Instance(
    id=INSTANCE, state="stopped", public_ipv4="", kept_devices=(), age=None, image_id=IMAGE
)
NEW_IMAGE = "ami-00000000000000001"


def written(stubs: Stubs, app_id: str, record: dict[str, Any]) -> None:
    """mark_baked が記録を 1 つ書き直す。"""
    stubs.ssm.add_response(
        "put_parameter",
        {"Version": 2},
        {
            "Name": f"{PATH}/{app_id}/deploy",
            "Value": json.dumps(record),
            "Type": "String",
            "Overwrite": True,
        },
    )


BASE = {"name": "J", "version": "2", "user": "root", "deployed": "2026-09-26T03:00:00Z"}


def test_mark_baked_records_what_the_instance_has(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """入れてまだ焼いていない記録は版ごと、起動元の AMI に焼いてあった記録は AMI だけ書く。"""
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(
                    f"{PATH}/fresh/deploy",
                    a_full_record("2", instance=INSTANCE, baked_version="1", baked_image=IMAGE),
                ),
                a_parameter(
                    f"{PATH}/kept/deploy", a_full_record("2", baked_version="2", baked_image=IMAGE)
                ),
                # 新しい版は失われたが、起動元の AMI の古い版は引き継がれている
                a_parameter(
                    f"{PATH}/lost/deploy",
                    a_full_record("2", instance=GONE, baked_version="1", baked_image=IMAGE),
                ),
                a_parameter(
                    f"{PATH}/other/deploy",
                    a_full_record("2", baked_version="1", baked_image=OTHER_IMAGE),
                ),
                a_parameter(f"{PATH}/unknown/deploy", a_full_record("2")),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    written(aws_stubs, "fresh", {**BASE, "baked_version": "2", "baked_image": NEW_IMAGE})
    written(aws_stubs, "kept", {**BASE, "baked_version": "2", "baked_image": NEW_IMAGE})
    written(
        aws_stubs,
        "lost",
        {**BASE, "instance": GONE, "baked_version": "1", "baked_image": NEW_IMAGE},
    )

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()
    # 焼いたと知らせるのは、このインスタンスに入れたものだけ
    assert told(capsys.readouterr().out) == [f"recorded fresh as baked in {NEW_IMAGE}"]


def test_mark_baked_deletes_the_records_of_apps_removed_from_the_instance(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """このインスタンスから外した記録は消す。外したことが失われた記録は、removed を消す。"""
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(
                    f"{PATH}/gone/deploy",
                    a_full_record(removed=INSTANCE, baked_version="1", baked_image=IMAGE),
                ),
                # 外したインスタンスが終了し、起動元の AMI からこのインスタンスに戻っている
                a_parameter(
                    f"{PATH}/back/deploy",
                    a_full_record(removed=GONE, baked_version="1", baked_image=IMAGE),
                ),
                # 起動元でない AMI に焼いた記録は触らない
                a_parameter(
                    f"{PATH}/apart/deploy",
                    a_full_record(removed=GONE, baked_version="1", baked_image=OTHER_IMAGE),
                ),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    written(
        aws_stubs,
        "back",
        {**BASE, "version": "1", "baked_version": "1", "baked_image": NEW_IMAGE},
    )
    aws_stubs.ssm.add_response("delete_parameter", {}, {"Name": f"{PATH}/gone/deploy"})

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()
    assert told(capsys.readouterr().out) == [f"recorded gone as removed from {NEW_IMAGE}"]


def test_mark_baked_goes_on_when_a_removed_record_cannot_be_deleted(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """消せない記録は警告して飛ばす。"""
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(
                    f"{PATH}/gone/deploy",
                    a_full_record(removed=INSTANCE, baked_version="1", baked_image=IMAGE),
                )
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    aws_stubs.ssm.add_client_error(
        "delete_parameter", service_error_code="AccessDeniedException", http_status_code=400
    )

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"temt: warning: cannot delete {PATH}/gone/deploy: ")
    assert captured.out == ""


def test_mark_removed_turns_the_record_into_the_baked_version() -> None:
    """外したインスタンスを書き、最後に入れた版とオプションを焼いたものにする。"""
    record = app.Record(
        fields={**BASE, "version": "3", "instance": INSTANCE, "baked_version": "2"},
        options={"limit": "4h"},
        baked_options={"limit": "2h"},
    )

    app.mark_removed(record, INSTANCE)

    # 焼いた版を入れた時刻は分からないので、deployed は消す
    assert record.fields == {
        "name": "J",
        "version": "2",
        "user": "root",
        "baked_version": "2",
        "removed": INSTANCE,
    }
    assert record.options == {"limit": "2h"}
    assert record.baked_options == {"limit": "2h"}


def test_mark_removed_keeps_baked_options_it_cannot_read() -> None:
    """読めない baked_options は、書かれていたままの値を options にもコピーする。"""
    record = app.Record(
        fields=dict(BASE),
        options={"limit": "4h"},
        baked_options=None,
        unread={app.BAKED_OPTIONS: ["x"]},
    )

    app.mark_removed(record, INSTANCE)

    assert record.options is None
    assert record.unread == {app.OPTIONS: ["x"], app.BAKED_OPTIONS: ["x"]}


def test_mark_baked_keeps_the_older_images_without_a_source_image(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """起動元の AMI が分からなければ、焼いてあった記録の AMI は付け替えない。

    記録の baked_image も空なので、空どうしを同じ AMI とみなせば書きに行く。
    put-parameter は積んでいないので、書けば Stubber が失敗する。
    """
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(
                    f"{PATH}/kept/deploy", a_full_record("2", baked_version="2", baked_image="")
                )
            ]
        },
        {"Path": PATH, "Recursive": True},
    )

    app.mark_baked(ssm, configured, replace(BAKED_FROM, image_id=""), NEW_IMAGE)

    aws_stubs.done()
    # 書けずに警告して飛ばしたのでも、焼いたと知らせたのでもない
    assert capsys.readouterr() == ("", "")


def test_mark_baked_goes_on_when_a_record_cannot_be_written(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """書けない記録は警告して飛ばし、残りを書く。"""
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(f"{PATH}/a/deploy", a_full_record("2", instance=INSTANCE)),
                a_parameter(f"{PATH}/b/deploy", a_full_record("2", instance=INSTANCE)),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    aws_stubs.ssm.add_client_error(
        "put_parameter", service_error_code="AccessDeniedException", http_status_code=400
    )
    written(aws_stubs, "b", {**BASE, "baked_version": "2", "baked_image": NEW_IMAGE})

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"temt: warning: cannot mark {PATH}/a/deploy as baked: ")
    assert told(captured.out) == [f"recorded b as baked in {NEW_IMAGE}"]


def test_mark_baked_shows_a_multiline_error_on_one_line(
    aws_stubs: Stubs,
    ssm: SSMClient,
    configured: Service,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """複数行の botocore の例外の文は、読める形で 1 行につなぎ、表示できない文字を ? にする。"""

    def refuse(ssm: SSMClient, name: str, value: str) -> str:
        _ = (ssm, name, value)
        raise ParamValidationError(report="Invalid length for parameter Value\nvalue: \x1b[2J")

    monkeypatch.setattr(app, "put", refuse)
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": [a_parameter(f"{PATH}/a/deploy", a_full_record("2", instance=INSTANCE))]},
        {"Path": PATH, "Recursive": True},
    )

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        f"temt: warning: cannot mark {PATH}/a/deploy as baked: Parameter validation failed: "
        "Invalid length for parameter Value; value: ?[2J"
    ]
    assert captured.out == ""


# --- オプション ---

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


def test_show_lists_the_options_and_the_declared_ones(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """記録のオプションは打ち直せる形で、手元のアプリが宣言するオプションも出す。"""
    local_app()
    applied(aws_stubs)
    record = json.loads(
        a_full_record("1.3", instance=INSTANCE, baked_version="1.2", baked_image=IMAGE)
    )
    record |= {
        "options": {"port": "9999", "token": "こんにちは 世界"},
        "baked_options": {"port": "8000"},
    }
    value = json.dumps(record, ensure_ascii=False)
    aws_stubs.ssm.add_response(
        "get_parameter", {"Parameter": a_parameter(RECORD, value)}, {"Name": RECORD}
    )
    keys(aws_stubs, RECORD)

    app.show(configured, "jupyter")

    aws_stubs.done()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "app jupyter:",
        "  name:          J",
        "  version:       1.3",
        "  user:          root",
        "  deployed:      2026-09-26 12:00:00",
        f"  instance:      {INSTANCE}",
        "  removed:       -",
        "  options:       port=9999 token='こんにちは 世界'",
        "  baked_version: 1.2",
        f"  baked_image:   {IMAGE}",
        "  baked_options: port=8000",
        "options in ~/.config/temt/apps/jupyter (version 1.3):",
        "  port   8888        the port to listen on",
        "  token  (required)  the token to log in with",
        "keys: none",
    ]


def test_show_lists_a_local_app_that_is_not_deployed(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """記録も値も無くても、手元にあれば打ち間違いとはせず、宣言を出す。"""
    local_app('name = "J"\nversion = "1"\n')
    applied(aws_stubs)
    not_found(aws_stubs, "get_parameter", Name=RECORD)
    keys(aws_stubs)

    app.show(configured, "jupyter")

    assert capsys.readouterr().out.splitlines() == [
        "app jupyter: not deployed",
        "options in ~/.config/temt/apps/jupyter (version 1): none",
        "keys: none",
    ]


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
    not_found(aws_stubs, "get_parameter", Name=RECORD)
    keys(aws_stubs)

    app.show(configured, "jupyter")

    assert capsys.readouterr().out.splitlines() == [
        "app jupyter: not deployed",
        "options in ~/bundled/jupyter (version 2): none",
        "keys: none",
    ]


def test_show_warns_about_a_local_manifest_it_cannot_read(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    local_app('name = "J"\n')
    applied(aws_stubs)
    not_found(aws_stubs, "get_parameter", Name=RECORD)
    keys(aws_stubs)

    app.show(configured, "jupyter")

    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["app jupyter: not deployed", "keys: none"]
    assert captured.err.splitlines() == [
        "temt: warning: cannot read the manifest of jupyter: "
        "~/.config/temt/apps/jupyter/manifest.toml: version is missing",
    ]


@pytest.mark.parametrize(
    "options",
    [
        '"port"',
        '{"port": 1}',
        '{"port": "a\\nb"}',
        '{"Port": "1"}',
        '["port"]',
    ],
)
def test_parse_record_leaves_options_it_cannot_read(
    options: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """形の違う options は読めないものとし、deploy が引き継がずに止められるようにする。"""
    value = f'{{"name": "J", "options": {options}, "baked_options": {options}}}'

    record = app.parse_record(RECORD, value)

    assert record.fields == {"name": "J"}
    assert record.options is None
    assert record.baked_options is None
    assert told(capsys.readouterr().err) == [
        f"temt: warning: cannot read options in the deploy record {RECORD}: "
        "not a JSON object of option names and values",
        f"temt: warning: cannot read baked_options in the deploy record {RECORD}: "
        "not a JSON object of option names and values",
    ]


def test_parse_record_of_a_record_that_is_not_an_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = app.parse_record(RECORD, "[]")

    assert record == app.Record(options=None, baked_options=None)
    assert "not a JSON object" in capsys.readouterr().err


def test_record_value_keeps_the_order_and_writes_japanese_as_it_is() -> None:
    record = app.Record(
        fields={"baked_image": IMAGE, "version": "1", "name": "J", "instance": INSTANCE},
        options={"greeting": "こんにちは"},
    )

    value = app.record_value(record)

    assert value == (
        f'{{"name": "J", "version": "1", "instance": "{INSTANCE}", '
        f'"options": {{"greeting": "こんにちは"}}, "baked_image": "{IMAGE}"}}'
    )
    assert app.record_size(record) == len(value.encode())


def test_mark_baked_bakes_the_options_too(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service
) -> None:
    """焼いた版のオプションを baked_options にコピーする。付け替えるだけの記録では変えない。"""
    fresh = json.loads(a_full_record("2", instance=INSTANCE, baked_version="1", baked_image=IMAGE))
    fresh |= {"options": {"port": "9999"}, "baked_options": {"port": "8000"}}
    kept = json.loads(a_full_record("2", baked_version="2", baked_image=IMAGE))
    kept |= {"options": {"port": "1"}, "baked_options": {"port": "2"}}
    # オプションを指定せずに入れ直したので、前に焼いたオプションは消える
    plain = json.loads(a_full_record("3", instance=INSTANCE, baked_version="2", baked_image=IMAGE))
    plain |= {"baked_options": {"port": "8000"}}
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(f"{PATH}/fresh/deploy", json.dumps(fresh)),
                a_parameter(f"{PATH}/kept/deploy", json.dumps(kept)),
                a_parameter(f"{PATH}/plain/deploy", json.dumps(plain)),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    written(
        aws_stubs,
        "fresh",
        {
            **BASE,
            "options": {"port": "9999"},
            "baked_version": "2",
            "baked_image": NEW_IMAGE,
            "baked_options": {"port": "9999"},
        },
    )
    written(
        aws_stubs,
        "kept",
        {
            **BASE,
            "options": {"port": "1"},
            "baked_version": "2",
            "baked_image": NEW_IMAGE,
            "baked_options": {"port": "2"},
        },
    )
    written(
        aws_stubs, "plain", {**BASE, "version": "3", "baked_version": "3", "baked_image": NEW_IMAGE}
    )

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()


def test_mark_baked_keeps_options_it_cannot_read(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """読めないオプションは、書かれていたままの値で書き戻し、失わない。

    焼いた版にする記録では、読めない options も書かれたまま baked_options にコピーする。次の
    deploy はそれを引き継げずに止まるので、--reset を促す。baked_options だけが読めない
    ときは、焼いた版にすれば options で置き換わり、付け替えるだけなら書かれたまま残る。
    """
    broken = json.loads(a_full_record("2", instance=INSTANCE))
    broken |= {"options": {"port": 1}}
    baked = json.loads(a_full_record("2", instance=INSTANCE))
    baked |= {"options": {"port": "1"}, "baked_options": "x"}
    kept = json.loads(a_full_record("2", baked_version="2", baked_image=IMAGE))
    kept |= {"baked_options": ["x"]}
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {
            "Parameters": [
                a_parameter(f"{PATH}/baked/deploy", json.dumps(baked)),
                a_parameter(f"{PATH}/broken/deploy", json.dumps(broken)),
                a_parameter(f"{PATH}/kept/deploy", json.dumps(kept)),
            ]
        },
        {"Path": PATH, "Recursive": True},
    )
    written(
        aws_stubs,
        "baked",
        {
            **BASE,
            "options": {"port": "1"},
            "baked_version": "2",
            "baked_image": NEW_IMAGE,
            "baked_options": {"port": "1"},
        },
    )
    written(
        aws_stubs,
        "broken",
        {
            **BASE,
            "options": {"port": 1},
            "baked_version": "2",
            "baked_image": NEW_IMAGE,
            "baked_options": {"port": 1},
        },
    )
    written(
        aws_stubs,
        "kept",
        {**BASE, "baked_version": "2", "baked_image": NEW_IMAGE, "baked_options": ["x"]},
    )

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()
    hints = [line for line in told(capsys.readouterr().err) if "--reset" in line]
    assert hints == [
        f"temt: warning: the options in {PATH}/broken/deploy cannot be read; baking them as "
        "they are; deploy broken again with --reset to give them"
    ]


def test_record_value_breaks_double_braces() -> None:
    """SSM は {{ を含む値を断るので、文字列の中で続く { の前のものを \\u007b にする。"""
    record = app.Record(
        fields={"name": "{{J}}"}, options={"greeting": "Hello {{{user}}"}, baked_options={"x": "}}"}
    )

    value = app.record_value(record)

    assert "{{" not in value
    assert value == (
        '{"name": "\\u007b{J}}", "options": {"greeting": "Hello \\u007b\\u007b{user}}"}, '
        '"baked_options": {"x": "}}"}}'
    )
    assert app.parse_record(RECORD, value) == record


def test_parse_record_takes_a_record_that_utf_8_cannot_hold_as_unreadable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """対にならないサロゲートを表す \\u の形があれば、書き直せないので読めない記録とする。"""
    record = app.parse_record(RECORD, '{"name": "J\\udcff", "version": "1"}')

    assert record == app.Record(options=None, baked_options=None)
    assert app.record_size(record) == 2
    assert told(capsys.readouterr().err) == [
        f"temt: warning: cannot read the deploy record {RECORD}: not valid UTF-8"
    ]


def test_parse_record_does_not_look_at_fields_it_does_not_keep(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """RECORD_FIELDS にない項目は書き直すときに捨てるので、UTF-8 で書けなくても読める。"""
    record = app.parse_record(RECORD, '{"name": "J", "version": "1", "note": "\\udcff"}')

    assert record == app.Record(fields={"name": "J", "version": "1"})
    assert capsys.readouterr().err == ""


def test_record_value_writes_back_options_it_cannot_read() -> None:
    value = '{"name": "J", "options": "port=1", "baked_options": {"Port": "1"}}'

    record = app.parse_record(RECORD, value)

    assert (record.options, record.baked_options) == (None, None)
    assert app.record_value(record) == value


def test_show_marks_options_it_cannot_read_and_keeps_wide_spaces(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """全角空白は ? にせず、そのまま打てる形で出す。読めないオプションは (unreadable)。"""
    applied(aws_stubs)
    record = json.loads(a_full_record("1.3"))
    record |= {"options": {"greeting": "こんにちは\u3000世界"}, "baked_options": "x"}
    aws_stubs.ssm.add_response(
        "get_parameter",
        {"Parameter": a_parameter(RECORD, json.dumps(record, ensure_ascii=False))},
        {"Name": RECORD},
    )
    keys(aws_stubs, RECORD)

    app.show(configured, "jupyter")

    aws_stubs.done()
    lines = capsys.readouterr().out.splitlines()
    assert "  options:       greeting='こんにちは\u3000世界'" in lines
    assert "  baked_options: (unreadable)" in lines


def test_show_hides_what_disturbs_the_terminal_in_the_declarations(
    aws_stubs: Stubs, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """manifest.toml の版、既定、説明も、端末を乱す文字だけを ? にする。"""
    local_app(
        'name = "J"\nversion = "1\\u001b[2J"\n\n[options.greeting]\n'
        'default = "a\\u202eb\\u3000c"\ndescription = "the\\u2066 greeting"\n'
    )
    applied(aws_stubs)
    not_found(aws_stubs, "get_parameter", Name=RECORD)
    keys(aws_stubs)

    app.show(configured, "jupyter")

    aws_stubs.done()
    assert capsys.readouterr().out.splitlines()[1:3] == [
        "options in ~/.config/temt/apps/jupyter (version 1?[2J):",
        "  greeting  'a?b\u3000c'  the? greeting",
    ]


def test_mark_baked_replaces_unreadable_baked_options_with_unreadable_options(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service
) -> None:
    """どちらも読めなければ、焼いた版の baked_options は、読んだままの options になる。"""
    both = json.loads(a_full_record("2", instance=INSTANCE))
    both |= {"options": "new", "baked_options": "old"}
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": [a_parameter(f"{PATH}/both/deploy", json.dumps(both))]},
        {"Path": PATH, "Recursive": True},
    )
    written(
        aws_stubs,
        "both",
        {
            **BASE,
            "options": "new",
            "baked_version": "2",
            "baked_image": NEW_IMAGE,
            "baked_options": "new",
        },
    )

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()


def test_mark_baked_leaves_a_record_that_utf_8_cannot_hold(
    aws_stubs: Stubs, ssm: SSMClient, configured: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """読めない記録は、どのインスタンスの記録かも分からないので、書き換えない。

    put-parameter は積んでいないので、書けば Stubber が失敗する。
    """
    value = a_full_record("2", instance=INSTANCE).replace('"J"', '"J\\udcff"')
    aws_stubs.ssm.add_response(
        "get_parameters_by_path",
        {"Parameters": [a_parameter(f"{PATH}/odd/deploy", value)]},
        {"Path": PATH, "Recursive": True},
    )

    app.mark_baked(ssm, configured, BAKED_FROM, NEW_IMAGE)

    aws_stubs.done()
    # 読めないと警告するだけで、書けずに飛ばしたのでも、焼いたと知らせたのでもない
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        f"temt: warning: cannot read the deploy record {PATH}/odd/deploy: not valid UTF-8"
    ]
    assert captured.out == ""
