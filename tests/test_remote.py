"""インスタンスの上のアプリと deploy の記録を読む remote のテスト。

インスタンスの上で動く読み取りのスクリプトは、手元の bash で一時ディレクトリに対して
動かして確かめる。
"""

import base64
import json
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import (
    COMMAND_ID,
    REACHED,
    Remote,
    Stubs,
    a_manifest,
    a_record,
    apps_archive,
    commanded,
    online,
)
from types_boto3_ssm.client import SSMClient

from temt import proc, remote
from temt.instances import Instance
from temt.msg import TemtError
from temt.names import Service

OTHER = "i-0fedcba9876543210"


def an_instance(instance_id: str, state: str) -> Instance:
    return Instance(instance_id, state, "", (), None)


ONE = an_instance(REACHED, "running")

# sudo の代わり。-n を捨て、残りをそのまま実行する
FAKE_SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
    case "$1" in
        -*) shift ;;
        *) break ;;
    esac
done
exec "$@"
"""


@pytest.fixture
def on_disk(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """ssh の代わりに、手元の bash で読み取りのスクリプトを動かす。アプリはこの下に置く。"""
    root = home_dir / "opt" / "apps"
    bin_dir = home_dir / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text(FAKE_SUDO, encoding="utf-8")
    sudo.chmod(0o755)

    def capture(argv: list[str], *, env: Mapping[str, str] | None = None) -> tuple[int, bytes]:
        _ = env
        done = subprocess.run(
            ["bash", "-c", argv[-1]],
            capture_output=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            check=False,
        )
        return done.returncode, done.stdout

    monkeypatch.setattr(proc, "capture", capture)
    monkeypatch.setattr(remote, "REMOTE_ROOT", str(root))
    return root


def put_app(root: Path, app_id: str, files: Mapping[str, str]) -> Path:
    """インスタンスの上のアプリのディレクトリを置く。"""
    path = root / app_id
    path.mkdir(parents=True)
    for name, body in files.items():
        (path / name).write_text(body, encoding="utf-8")
    return path


def test_read_apps_reads_the_manifest_and_the_record_of_each_app(
    on_disk: Path, service: Service
) -> None:
    """アプリ ID の規則に外れるディレクトリ（入れ替えの途中の隠しディレクトリなど）と、
    ディレクトリでないものは除く。ほかのファイルは読まない。
    """
    put_app(
        on_disk,
        "jupyter",
        {"manifest.toml": a_manifest("1.2"), "temt-deploy.json": a_record(REACHED), "x": "x"},
    )
    put_app(on_disk, "emacs", {"manifest.toml": a_manifest("2")})
    put_app(on_disk, ".jupyter.abc123", {"manifest.toml": a_manifest("9")})
    put_app(on_disk, "Bad_ID", {"manifest.toml": a_manifest("9")})
    (on_disk / "file").write_text("x", encoding="utf-8")
    found = remote.read_apps(service, ONE, {})
    assert list(found) == ["emacs", "jupyter"]
    jupyter = found["jupyter"]
    assert jupyter.manifest is not None
    assert jupyter.manifest.version == "1.2"
    assert jupyter.record is not None
    assert jupyter.record.fields == {
        "user": "root",
        "deployed": "2026-09-27T03:04:05Z",
        "instance": REACHED,
    }
    assert found["emacs"].record is None


def test_read_apps_reads_only_the_app_it_is_given(on_disk: Path, service: Service) -> None:
    put_app(on_disk, "jupyter", {"manifest.toml": a_manifest("1.2")})
    put_app(on_disk, "emacs", {"manifest.toml": a_manifest("2")})
    assert list(remote.read_apps(service, ONE, {}, "emacs")) == ["emacs"]
    assert remote.read_apps(service, ONE, {}, "mise") == {}


def test_read_apps_finds_an_app_without_its_files(on_disk: Path, service: Service) -> None:
    """manifest.toml も記録も無いディレクトリも、入っているものとして返す。"""
    put_app(on_disk, "jupyter", {})
    found = remote.read_apps(service, ONE, {}, "jupyter")
    assert list(found) == ["jupyter"]
    assert found["jupyter"].manifest is None


def test_read_apps_without_the_directory(on_disk: Path, service: Service) -> None:
    """アプリを置く親ディレクトリが無いか空なら、アプリは無い。"""
    assert remote.read_apps(service, ONE, {}) == {}
    on_disk.mkdir(parents=True)
    assert remote.read_apps(service, ONE, {}) == {}


def test_read_apps_warns_about_a_manifest_it_cannot_read(
    on_disk: Path, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    put_app(on_disk, "jupyter", {"manifest.toml": "name = \n"})
    found = remote.read_apps(service, ONE, {})
    assert found["jupyter"].manifest is None
    assert capsys.readouterr().err.startswith(
        f"temt: warning: cannot read the manifest of jupyter: {REACHED}:{on_disk}/jupyter/"
        "manifest.toml: "
    )


def test_read_apps_stops_when_ssh_fails(remote_apps: Remote, service: Service) -> None:
    remote_apps.code = 255
    with pytest.raises(TemtError) as caught:
        _ = remote.read_apps(service, ONE, {})
    assert str(caught.value) == (
        f"cannot read the apps in {remote.REMOTE_ROOT} on {REACHED} (exit 255)"
    )


def test_read_apps_stops_on_an_output_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, service: Service
) -> None:
    monkeypatch.setattr(proc, "capture", lambda argv, env=None: (0, b"not a tar"))
    with pytest.raises(TemtError) as caught:
        _ = remote.read_apps(service, ONE, {})
    assert str(caught.value).startswith(
        f"cannot read the apps in {remote.REMOTE_ROOT} on {REACHED}: "
    )


def test_read_apps_reads_names_with_a_newline(on_disk: Path, service: Service) -> None:
    """名前は NUL で区切るので、改行を含む名前があっても、ほかのアプリは崩れない。"""
    put_app(on_disk, "a\nb", {"manifest.toml": a_manifest("9")})
    put_app(on_disk, "jupyter", {"manifest.toml": a_manifest("1.2")})
    assert list(remote.read_apps(service, ONE, {})) == ["jupyter"]


def test_parse_record_reads_the_options() -> None:
    record = remote.parse_record("x", a_record(REACHED, options={"port": "9000"}).encode())
    assert record.readable
    assert record.options == {"port": "9000"}


@pytest.mark.parametrize(
    ("data", "warning"),
    [
        (b"[]", "cannot read the deploy record x: not a JSON object"),
        (b"{", "cannot read the deploy record x: not a JSON object"),
        (b"\xff", "cannot read the deploy record x: not a JSON object"),
        (
            b'{"user": 1}',
            "cannot read the deploy record x: user, deployed, instance must be strings",
        ),
        (b'{"user": "\\ud800"}', "cannot read the deploy record x: not valid UTF-8"),
    ],
)
def test_parse_record_warns_about_a_record_it_cannot_read(
    data: bytes, warning: str, capsys: pytest.CaptureFixture[str]
) -> None:
    record = remote.parse_record("x", data)
    assert not record.readable
    assert record.options is None
    assert capsys.readouterr().err == f"temt: warning: {warning}\n"


@pytest.mark.parametrize("found", [[], {"Bad": "1"}, {"port": 1}, {"port": "a\nb"}])
def test_parse_record_reads_around_options_it_cannot_read(
    found: object, capsys: pytest.CaptureFixture[str]
) -> None:
    record = remote.parse_record("x", json.dumps({"user": "root", "options": found}).encode())
    assert record.readable
    assert record.fields == {"user": "root"}
    assert record.options is None
    assert record.unread_options
    assert capsys.readouterr().err.startswith("temt: warning: cannot read options in the deploy")


def test_record_data_is_read_back() -> None:
    data = remote.record_data("ubuntu", "2026-09-27T03:04:05Z", REACHED, {"greeting": "こんにちは"})
    assert "こんにちは" in data.decode()
    record = remote.parse_record("x", data)
    assert record.fields == {
        "user": "ubuntu",
        "deployed": "2026-09-27T03:04:05Z",
        "instance": REACHED,
    }
    assert record.options == {"greeting": "こんにちは"}
    # 空のオプションは書かない
    assert "options" not in json.loads(remote.record_data("root", "t", REACHED, {}))


def test_baked() -> None:
    """記録の instance が読んだインスタンスでなければ、AMI から来たので焼いてある。"""
    assert remote.baked(remote.parse_record("x", a_record(OTHER).encode()), REACHED) == "yes"
    assert remote.baked(remote.parse_record("x", a_record(REACHED).encode()), REACHED) == "no"
    assert remote.baked(None, REACHED) == "-"
    assert remote.baked(remote.Record(), REACHED) == "-"


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        ([], (None, "no instance")),
        ([an_instance(REACHED, "shutting-down")], (None, "no instance")),
        ([an_instance(REACHED, "stopped")], (None, f"{REACHED} is stopped")),
        ([an_instance(REACHED, "running"), an_instance(OTHER, "running")], (None, "2 instances")),
        (
            [an_instance(REACHED, "running"), an_instance(OTHER, "shutting-down")],
            (an_instance(REACHED, "running"), ""),
        ),
    ],
)
def test_running_one(found: list[Instance], expected: tuple[Instance | None, str]) -> None:
    assert remote.running_one(found) == expected


# --- Run Command で読む ---


def test_the_encoded_script_prints_the_archive_in_base64(on_disk: Path, home_dir: Path) -> None:
    """Run Command は文字しか返せないので、tar.gz を base64 の 1 行にして出す。"""
    put_app(on_disk, "jupyter", {"manifest.toml": a_manifest("1.2")})
    done = subprocess.run(
        ["bash", "-c", remote.ENCODED_READ_SCRIPT, "temt", str(on_disk)],
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0
    assert b"\n" not in done.stdout
    found = remote._parse(ONE, base64.b64decode(done.stdout))
    assert list(found) == ["jupyter"]
    # 親ディレクトリが無ければ、何も出さない
    done = subprocess.run(
        ["bash", "-c", remote.ENCODED_READ_SCRIPT, "temt", str(home_dir / "none")],
        capture_output=True,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, b"")


def test_read_apps_by_command_sends_the_script_and_reads_its_output(
    aws_stubs: Stubs, ssm: SSMClient
) -> None:
    command = " ".join(
        ["bash", "-c", shlex.quote(remote.ENCODED_READ_SCRIPT), "temt", "/opt/temt/apps", "jupyter"]
    )
    aws_stubs.ssm.add_response(
        "send_command",
        {"Command": {"CommandId": COMMAND_ID}},
        {
            "InstanceIds": [REACHED],
            "DocumentName": "AWS-RunShellScript",
            "Parameters": {"commands": [command], "executionTimeout": ["30"]},
            "TimeoutSeconds": 30,
            "Comment": "temt: read the apps",
        },
    )
    # 送った直後は、まだ実行の記録が無い
    aws_stubs.ssm.add_client_error(
        "get_command_invocation", service_error_code="InvocationDoesNotExist", http_status_code=400
    )
    aws_stubs.ssm.add_response("get_command_invocation", {"Status": "InProgress"})
    output = base64.b64encode(apps_archive({"jupyter": {"manifest.toml": a_manifest("1.2")}}))
    aws_stubs.ssm.add_response(
        "get_command_invocation",
        {"Status": "Success", "StandardOutputContent": output.decode() + "\n"},
    )
    found = remote.read_apps_by_command(ssm, ONE, "jupyter")
    aws_stubs.done()
    assert list(found) == ["jupyter"]


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not base64!", "not base64"),
        ("A" * 24000, "the output is over the 24000 characters that Run Command returns"),
    ],
)
def test_read_apps_by_command_stops_on_an_output_it_cannot_take(
    aws_stubs: Stubs, ssm: SSMClient, output: str, message: str
) -> None:
    aws_stubs.ssm.add_response("send_command", {"Command": {"CommandId": COMMAND_ID}})
    aws_stubs.ssm.add_response(
        "get_command_invocation", {"Status": "Success", "StandardOutputContent": output}
    )
    with pytest.raises(TemtError) as caught:
        _ = remote.read_apps_by_command(ssm, ONE)
    assert str(caught.value) == f"cannot read the apps on {REACHED}: {message}"


def test_read_apps_by_command_gives_up_at_the_deadline(aws_stubs: Stubs, ssm: SSMClient) -> None:
    aws_stubs.ssm.add_response("send_command", {"Command": {"CommandId": COMMAND_ID}})
    for _ in range(remote.COMMAND_DEADLINE // 5 + 1):
        aws_stubs.ssm.add_response("get_command_invocation", {"Status": "InProgress"})
    with pytest.raises(TemtError, match="^timed out after 1 minute waiting for Run Command"):
        _ = remote.read_apps_by_command(ssm, ONE)


def test_read_apps_by_command_when_the_agent_cannot_take_it(
    aws_stubs: Stubs, ssm: SSMClient
) -> None:
    aws_stubs.ssm.add_client_error(
        "send_command", service_error_code="InvalidInstanceId", http_status_code=400
    )
    with pytest.raises(TemtError, match=f"^the SSM agent on {REACHED} cannot take a command$"):
        _ = remote.read_apps_by_command(ssm, ONE)


def test_read_apps_by_command_turns_aws_errors_into_temt_errors(
    aws_stubs: Stubs, ssm: SSMClient
) -> None:
    """権限が無いなどの AWS の誤りも、呼ぶ側が警告して続けられるよう TemtError にする。"""
    aws_stubs.ssm.add_client_error(
        "send_command", service_error_code="AccessDeniedException", http_status_code=400
    )
    with pytest.raises(TemtError, match=f"^cannot read the apps on {REACHED}: .*AccessDenied"):
        _ = remote.read_apps_by_command(ssm, ONE)
    aws_stubs.ssm.add_response("send_command", {"Command": {"CommandId": COMMAND_ID}})
    aws_stubs.ssm.add_client_error(
        "get_command_invocation", service_error_code="ThrottlingException", http_status_code=400
    )
    with pytest.raises(TemtError, match=f"^cannot read the apps on {REACHED}: .*Throttling"):
        _ = remote.read_apps_by_command(ssm, ONE)
    aws_stubs.done()


def test_readable_when_the_agent_cannot_be_checked(aws_stubs: Stubs, ssm: SSMClient) -> None:
    aws_stubs.ssm.add_client_error(
        "describe_instance_information",
        service_error_code="AccessDeniedException",
        http_status_code=400,
    )
    one, why = remote.readable(ssm, [ONE])
    assert one is None
    assert why.startswith(f"cannot check the SSM agent on {REACHED}: ")
    assert "AccessDenied" in why
    aws_stubs.done()


def test_readable_needs_an_online_agent(aws_stubs: Stubs, ssm: SSMClient) -> None:
    online(aws_stubs, REACHED, "ConnectionLost")
    assert remote.readable(ssm, [ONE]) == (None, f"the SSM agent on {REACHED} is not online")
    online(aws_stubs, REACHED)
    assert remote.readable(ssm, [ONE]) == (ONE, "")
    # running でなければ、エージェントを尋ねない
    stopped = an_instance(REACHED, "stopped")
    assert remote.readable(ssm, [stopped]) == (None, f"{REACHED} is stopped")
    aws_stubs.done()


def test_commanded_helper_matches_the_reader(aws_stubs: Stubs, ssm: SSMClient) -> None:
    """テストが使う commanded の応答で、本物の読み手が読める。"""
    commanded(aws_stubs, REACHED, {"jupyter": {"manifest.toml": a_manifest("1.2")}})
    one, _ = remote.readable(ssm, [ONE])
    assert one is not None
    assert list(remote.read_apps_by_command(ssm, one)) == ["jupyter"]
    aws_stubs.done()
