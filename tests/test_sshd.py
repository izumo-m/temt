"""sshd の待ち受けポートを揃える仕組みのテスト。

スクリプトは、文字列として確かめるほか、/etc と /run の場所を一時ディレクトリに
置き換え、sshd、ss、systemctl、sleep を仮のコマンドに差し替えて、実際に sh で流す。
関連付けを揃えて消す処理（resources）と状態を読む処理（sshd）は、AWS を Stubber で
差し替える。
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from conftest import (
    ASSOCIATION,
    ASSOCIATION_MARK,
    OLD_VERSION,
    SSHD,
    VERSION,
    association_content,
    association_in_place,
    association_ran,
    association_started,
    described_association,
    listed_associations,
)
from types_boto3_ssm.client import SSMClient

from temt import conf, resources, sshd
from temt.conf import Conf
from temt.names import Service

# 同じ名前の、もう 1 つの関連付けの ID
OTHER = "99999999-8888-7777-6666-555555555555"
INSTANCE = "i-0123456789abcdef0"
THE_ASSOCIATION = {"AssociationId": ASSOCIATION}
THE_TAGS = {"ResourceType": "Association", "ResourceId": ASSOCIATION}
# 名前で見つけた関連付けの、今の版
CURRENT = sshd.Association(ASSOCIATION, VERSION)
# 更新したときと、[22] 以外のポートで作ったときに添える行の一部
NOTE = "take effect on a running instance shortly"
# スクリプトを流す sh。SSM エージェントが使う Ubuntu の sh は dash なので、あればそれで
SHELL = shutil.which("dash") or "/bin/sh"


def script(ports: list[int], svc: str = "dev-main") -> str:
    """スクリプトを 1 つの文字列にする。"""
    return "\n".join(sshd.commands(ports, svc)) + "\n"


def test_the_script_is_one_line_per_element() -> None:
    lines = sshd.commands([2222, 443], "dev-main")
    assert all("\n" not in line for line in lines)
    assert lines[0] == "set -u"


def test_the_script_is_ascii() -> None:
    """AWS のコンソールにもそのまま出るので、説明は Python の側に置く。"""
    assert script([2222, 443]).isascii()


def test_the_script_is_valid_sh() -> None:
    """SSM エージェントは sh（Ubuntu では dash）で実行する。構文だけを確かめる。"""
    checked = subprocess.run(
        [SHELL, "-n"], input=script([2222, 443]), capture_output=True, text=True, check=False
    )
    assert (checked.returncode, checked.stderr) == (0, "")


def test_the_script_writes_the_ports_in_their_order() -> None:
    """ファイルには SshdPorts の順に Port の行を並べる。比べるときは順序を問わない。"""
    lines = sshd.commands([2222, 443], "dev-main")
    assert 'want="2222 443"' in lines
    assert 'for p in $want; do echo "Port $p" >> "$conf.tmp"; done' in lines
    assert f"conf={sshd.CONF_FILE}" in lines
    assert sshd.CONF_FILE == "/etc/ssh/sshd_config.d/temt-sshd-ports.conf"


def test_the_script_keeps_the_ports_of_the_ami() -> None:
    """起動してから最初に切り替える前のポートを、/run の ami-ports に控える。"""
    lines = sshd.commands([22], "dev-main")
    assert f"state={sshd.STATE_DIR}" in lines
    assert sshd.STATE_DIR == "/run/temt-sshd"
    assert '[ -e "$state/ami-ports" ] || echo "$before" > "$state/ami-ports"' in lines


def test_the_script_writes_the_mark_with_the_service() -> None:
    text = script([2222], "web")
    assert 'svc="web"' in text
    assert f"mark={sshd.MARK_FILE}" in text
    assert sshd.MARK_FILE == "/run/motd.d/50-temt-sshd"
    assert (
        "echo \"temt: sshd ports were changed from the AMI's "
        '($(csv "$ami") -> $(csv "$want")); '
        'run \'temt bake --svc $svc\' to keep them in the AMI" > "$mark"'
    ) in text
    assert 'rm -f "$mark"' in text


def test_the_script_reports_with_its_prefix() -> None:
    text = script([22])
    assert 'log() { echo "temt-sshd: $*"; }' in text
    assert 'log "unchanged $(csv "$want")"' in text
    assert 'log "changed $(csv "$before") -> $(csv "$want")"' in text


def test_the_script_reloads_through_the_socket_or_the_service() -> None:
    """daemon-reload を必ず先に行い、ssh.socket を使うかは有効かどうかで決める。"""
    lines = sshd.commands([22], "dev-main")
    start = lines.index("reload() {")
    assert lines[start + 1 : start + 8] == [
        "    systemctl daemon-reload",
        "    if systemctl is-enabled --quiet ssh.socket; then",
        "        systemctl restart ssh.socket",
        "    else",
        "        systemctl restart ssh.service",
        "    fi",
        "}",
    ]


def test_the_script_counts_only_the_listeners_of_sshd() -> None:
    """ss で持ち主を見て、sshd か systemd（ソケット起動）の待ち受けだけを数える。"""
    assert (
        "listening() { for p in $1; do "
        'ss -Hltnp "sport = :$p" | grep -Eq \'"(sshd|systemd)"\' || return 1; done; }'
    ) in sshd.commands([22], "dev-main")


def test_the_script_restores_when_sshd_rejects_it() -> None:
    """sshd -t が通らないときと、ほかの Port が足し合わされるときは、戻して 1 で終わる。"""
    lines = sshd.commands([22], "dev-main")
    rejected = lines.index("if ! sshd -t; then")
    assert lines[rejected + 1] == "    restore"
    failed = lines.index("if ! after=$(effective); then")
    assert lines[failed + 1] == "    restore"
    added = lines.index('if [ "$(norm "$after")" != "$(norm "$want")" ]; then')
    assert lines[added + 1] == "    restore"
    assert lines.index("reload") > added > failed


# 以下、スクリプトを実際に sh で流して確かめる。/etc と /run は一時ディレクトリの下に
# 置き換え、sshd、ss、systemctl、sleep は、状態をファイルで持つ仮のコマンドに差し替える。
# 仮のコマンドは、環境変数 ROOT（置き換え先）と SIM（状態を置くディレクトリ）を見る。

# 仮の sshd。sshd_config.d の Port を足し合わせたものが効いているポートで、無ければ 22。
# $SIM/reject があれば -t が失敗する。$SIM/dump_fails があるか、$SIM/dump_fails_on の
# ポートを書いていれば、-T が失敗する
FAKE_SSHD = r"""#!/bin/sh
[ -d "$ROOT/run/sshd" ] || { echo "Missing privilege separation directory" >&2; exit 255; }
ports() { cat "$ROOT"/etc/ssh/sshd_config.d/*.conf 2>/dev/null | awk '$1 == "Port" { print $2 }'; }
case "$1" in
-t)
    [ -e "$SIM/reject" ] && { echo "bad configuration" >&2; exit 255; }
    exit 0 ;;
-T)
    [ -e "$SIM/dump_fails" ] && exit 255
    p=$(ports)
    if [ -e "$SIM/dump_fails_on" ]; then
        printf '%s\n' $p | grep -qxF "$(cat "$SIM/dump_fails_on")" && exit 255
    fi
    [ -n "$p" ] || p=22
    for x in $p; do echo "port $x"; done
    echo "addressfamily any"
    exit 0 ;;
esac
exit 1
"""

# 仮の ss。$SIM/listening の「ポート 持ち主」の行から、そのポートの待ち受けを出す
FAKE_SS = r"""#!/bin/sh
[ "$1" = -Hltnp ] || { echo "ss: unexpected options: $*" >&2; exit 2; }
p=${2#sport = :}
while read -r port owner; do
    if [ "$port" = "$p" ]; then
        echo "LISTEN 0 4096 0.0.0.0:$port 0.0.0.0:* users:((\"$owner\",pid=100,fd=3))"
    fi
done < "$SIM/listening"
exit 0
"""

# 仮の systemctl。呼ばれた引数を $SIM/calls に書く。$SIM/socket があれば ssh.socket が
# 有効（ソケット起動）で、ssh.socket は daemon-reload のときの Port（$SIM/generated）で
# 待ち受ける。そのどれかを別のプロセスが使っていれば、再起動に失敗して何も待ち受けない。
# ソケット起動では ssh.service は ssh.socket を要するので、ssh.service の再起動も同じに
# なる。ソケット起動でなければ、sshd は -T のポートのうち空いているもので待ち受ける
FAKE_SYSTEMCTL = r"""#!/bin/sh
echo "$*" >> "$SIM/calls"
others() { awk '$2 != "sshd" && $2 != "systemd"' "$SIM/listening"; }
busy() { others | awk -v p="$1" '$1 == p { found = 1 } END { exit !found }'; }
serve() {
    owner=$1
    shift
    { others; for p in "$@"; do echo "$p $owner"; done; } > "$SIM/listening.new"
    mv "$SIM/listening.new" "$SIM/listening"
}
effective() { sshd -T | awk '$1 == "port" { print $2 }'; }
socket() {
    for p in $(cat "$SIM/generated"); do
        if busy "$p"; then
            serve systemd
            return 1
        fi
    done
    serve systemd $(cat "$SIM/generated")
}
case "$*" in
daemon-reload) effective > "$SIM/generated" ;;
"is-enabled --quiet ssh.socket") [ -e "$SIM/socket" ] ;;
"restart ssh.socket") socket ;;
"restart ssh.service")
    if [ -e "$SIM/socket" ]; then
        socket
        exit
    fi
    bound=""
    for p in $(effective); do busy "$p" || bound="$bound $p"; done
    serve sshd $bound
    [ -n "$bound" ] ;;
*)
    echo "systemctl: unexpected: $*" >&2
    exit 1 ;;
esac
"""

# 仮の sleep。待たずに、呼ばれた回数を $SIM/sleeps に書く
FAKE_SLEEP = r"""#!/bin/sh
echo "$*" >> "$SIM/sleeps"
"""

FAKES = {"sshd": FAKE_SSHD, "ss": FAKE_SS, "systemctl": FAKE_SYSTEMCTL, "sleep": FAKE_SLEEP}

# スクリプトの中の、インスタンスの上の場所。長いものから置き換える
PLACES = sorted(
    [sshd.CONF_FILE, sshd.STATE_DIR, sshd.MARK_FILE, sshd.MOTD_DIR, "/run/sshd"],
    key=len,
    reverse=True,
)
PLACES_RE = re.compile("|".join(re.escape(place) for place in PLACES))

MARK_2222 = (
    "temt: sshd ports were changed from the AMI's (22 -> 2222); "
    "run 'temt bake --svc dev-main' to keep them in the AMI\n"
)
# ssh.socket を使う構成と使わない構成で、1 回の読み直しに呼ぶ systemctl
SOCKET_RELOAD = ["daemon-reload", "is-enabled --quiet ssh.socket", "restart ssh.socket"]
SERVICE_RELOAD = ["daemon-reload", "is-enabled --quiet ssh.socket", "restart ssh.service"]


def localized(text: str, root: Path) -> str:
    """スクリプトの /etc と /run の場所を、root の下に置き換える。"""
    assert not re.search("/etc/|/run/", PLACES_RE.sub("", text)), "a place is not replaced"
    return PLACES_RE.sub(lambda found: f"{root}{found.group(0)}", text)


@dataclass
class Box:
    """スクリプトを流す、インスタンスに見立てた場所。"""

    root: Path
    sim: Path
    bin: Path

    def at(self, place: str) -> Path:
        """インスタンスの上の場所を、root の下の場所にする。"""
        return self.root / place.lstrip("/")

    @property
    def conf(self) -> Path:
        return self.at(sshd.CONF_FILE)

    @property
    def ami_ports(self) -> Path:
        return self.at(sshd.STATE_DIR) / "ami-ports"

    @property
    def mark(self) -> Path:
        return self.at(sshd.MARK_FILE)

    def socket_activated(self) -> None:
        """ssh.socket を有効にし、systemd が 22 で待ち受けている姿にする。"""
        (self.sim / "socket").touch()
        self.listen("22 systemd")

    def listen(self, *lines: str) -> None:
        """待ち受けを「ポート 持ち主」の行で書く。"""
        (self.sim / "listening").write_text("".join(f"{line}\n" for line in lines))

    def listening(self) -> list[str]:
        """いまの待ち受けの「ポート 持ち主」の行。並びは問わない。"""
        return sorted((self.sim / "listening").read_text().splitlines())

    def write(self, place: Path, text: str) -> None:
        place.parent.mkdir(parents=True, exist_ok=True)
        _ = place.write_text(text)

    def calls(self) -> list[str]:
        """直前の run で呼ばれた systemctl の引数。"""
        found = self.sim / "calls"
        return found.read_text().splitlines() if found.exists() else []

    def sleeps(self) -> int:
        found = self.sim / "sleeps"
        return len(found.read_text().splitlines()) if found.exists() else 0

    def run(self, ports: list[int]) -> subprocess.CompletedProcess[str]:
        """ports を SshdPorts にしたスクリプトを流す。呼ばれたものの記録は毎回消す。"""
        for name in ("calls", "sleeps"):
            (self.sim / name).unlink(missing_ok=True)
        path = self.root / "script.sh"
        _ = path.write_text(localized(script(ports, "dev-main"), self.root))
        return subprocess.run(
            [SHELL, str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={
                **os.environ,
                "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "ROOT": str(self.root),
                "SIM": str(self.sim),
            },
        )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    """起動したばかりの素の Ubuntu に見立てた場所。sshd が 22 で待ち受けている。

    /run/sshd と sshd_config.d はまだ無く、スクリプトが作る。ssh.socket は有効でない。
    """
    made = Box(root=tmp_path / "root", sim=tmp_path / "sim", bin=tmp_path / "bin")
    for directory in (made.root, made.sim, made.bin):
        directory.mkdir()
    for name, text in FAKES.items():
        fake = made.bin / name
        _ = fake.write_text(text)
        fake.chmod(0o755)
    made.listen("22 sshd")
    _ = (made.sim / "generated").write_text("22\n")
    return made


def test_the_first_switch(box: Box) -> None:
    """ソケット起動で 22 から 2222 に切り替え、AMI のポートと印を書く。"""
    box.socket_activated()
    done = box.run([2222])
    assert (done.returncode, done.stdout, done.stderr) == (
        0,
        "temt-sshd: changed 22 -> 2222\n",
        "",
    )
    assert box.conf.read_text() == "Port 2222\n"
    assert box.ami_ports.read_text() == "22\n"
    assert box.mark.read_text() == MARK_2222
    assert box.calls() == SOCKET_RELOAD
    assert box.listening() == ["2222 systemd"]
    assert box.at("/run/sshd").is_dir()


def test_the_second_run_leaves_sshd_alone(box: Box) -> None:
    box.socket_activated()
    _ = box.run([2222])
    done = box.run([2222])
    assert (done.returncode, done.stdout) == (0, "temt-sshd: unchanged 2222\n")
    assert box.calls() == []
    assert box.mark.read_text() == MARK_2222


def test_the_order_of_the_ports_does_not_matter(box: Box) -> None:
    box.socket_activated()
    first = box.run([2222, 443])
    assert first.stdout == "temt-sshd: changed 22 -> 2222,443\n"
    done = box.run([443, 2222])
    assert (done.returncode, done.stdout) == (0, "temt-sshd: unchanged 443,2222\n")
    assert box.calls() == []
    assert box.conf.read_text() == "Port 2222\nPort 443\n"


def test_going_back_to_the_ami_ports_removes_the_mark(box: Box) -> None:
    box.socket_activated()
    _ = box.run([2222])
    done = box.run([22])
    assert (done.returncode, done.stdout) == (0, "temt-sshd: changed 2222 -> 22\n")
    assert not box.mark.exists()
    # 起動してから最初に切り替える前のポートのまま
    assert box.ami_ports.read_text() == "22\n"
    assert box.conf.read_text() == "Port 22\n"
    assert box.listening() == ["22 systemd"]


def test_without_socket_activation_the_service_restarts(box: Box) -> None:
    done = box.run([2222])
    assert (done.returncode, done.stdout) == (0, "temt-sshd: changed 22 -> 2222\n")
    assert box.calls() == SERVICE_RELOAD
    assert box.listening() == ["2222 sshd"]


@pytest.mark.parametrize("previous", [None, "Port 2200\n"], ids=["no file", "a file"])
def test_the_script_restores_what_sshd_t_rejects(box: Box, previous: str | None) -> None:
    """sshd -t が通らなければ、控えから戻す。控えが無ければファイルを消す。"""
    if previous is not None:
        box.write(box.conf, previous)
        box.listen("2200 sshd")
    (box.sim / "reject").touch()
    done = box.run([2222])
    assert (done.returncode, done.stdout) == (
        1,
        "temt-sshd: sshd -t rejected the ports 2222; restored the previous configuration\n",
    )
    assert (box.conf.read_text() if box.conf.exists() else None) == previous
    assert box.calls() == []
    assert not box.mark.exists()


def test_the_script_restores_when_another_file_sets_port(box: Box) -> None:
    box.write(box.at("/etc/ssh/sshd_config.d/50-other.conf"), "Port 22\n")
    done = box.run([2222])
    assert (done.returncode, done.stdout) == (
        1,
        "temt-sshd: sshd would listen on 22,2222, as another file also sets Port; "
        "restored the previous configuration\n",
    )
    assert not box.conf.exists()
    assert box.calls() == []


def test_the_script_restores_when_sshd_t_fails_after_writing(box: Box) -> None:
    """手順 6 で sshd -T が失敗したときは、ポートが合わないときと別の文言で知らせる。"""
    _ = (box.sim / "dump_fails_on").write_text("2222\n")
    done = box.run([2222])
    assert (done.returncode, done.stdout) == (
        1,
        "temt-sshd: sshd -T failed with the ports 2222; restored the previous configuration\n",
    )
    assert not box.conf.exists()
    assert box.calls() == []


def test_the_script_restores_when_sshd_does_not_listen(box: Box) -> None:
    """読み直した sshd が待ち受けなければ、戻して読み直させ、前のポートに戻す。"""
    box.listen("22 sshd", "2222 nginx")
    done = box.run([2222])
    assert (done.returncode, done.stdout) == (
        1,
        "temt-sshd: sshd did not listen on 2222; restored the previous configuration\n",
    )
    assert not box.conf.exists()
    assert box.calls() == SERVICE_RELOAD * 2
    assert box.sleeps() == sshd.LISTEN_TRIES - 1
    assert box.listening() == ["22 sshd", "2222 nginx"]
    assert not box.mark.exists()


def test_a_failed_socket_is_brought_back_on_the_previous_ports(box: Box) -> None:
    """ssh.socket の再起動に失敗しても、戻したあと daemon-reload してから立て直す。

    443 を別のプロセスが使っていると、ssh.socket は再起動に失敗して止まる。止まった
    ssh.socket も有効なので、戻したあとは ssh.socket を再起動し、daemon-reload で
    前の Port を写してあるので 22 で待ち受ける。
    """
    box.socket_activated()
    box.listen("22 systemd", "443 nginx")
    done = box.run([2222, 443])
    assert (done.returncode, done.stdout) == (
        1,
        "temt-sshd: sshd did not listen on 2222,443; restored the previous configuration\n",
    )
    assert box.calls() == SOCKET_RELOAD * 2
    assert box.listening() == ["22 systemd", "443 nginx"]
    assert not box.conf.exists()


def test_another_process_on_a_port_is_not_sshd(box: Box) -> None:
    """効いているポートが揃っていても、別のプロセスの待ち受けは sshd のものと数えない。"""
    # スクリプトが書く順（SshdPorts の順）と違えておき、控えから戻したことを見分ける
    box.write(box.conf, "Port 443\nPort 2222\n")
    box.listen("2222 sshd", "443 nginx")
    done = box.run([2222, 443])
    assert done.returncode == 1
    assert "unchanged" not in done.stdout
    assert box.calls() == SERVICE_RELOAD * 2
    # 控えから戻すので、ファイルは前のまま
    assert box.conf.read_text() == "Port 443\nPort 2222\n"


def test_nothing_changes_when_sshd_t_fails(box: Box) -> None:
    (box.sim / "dump_fails").touch()
    done = box.run([2222])
    assert (done.returncode, done.stdout) == (
        1,
        "temt-sshd: sshd -T failed; left the configuration as it is\n",
    )
    assert not box.conf.exists()
    assert not box.at(sshd.STATE_DIR).exists()
    assert box.calls() == []


def values(ports: list[int] | None = None) -> Conf:
    """ports（既定は [22]）を SshdPorts に書いた設定。"""
    return Conf(SshdPorts=ports or [conf.SSHD_PORT])


def associate(ssm: SSMClient, service: Service, found: Conf) -> None:
    """apply と同じく、関連付けの差分を検出してから、その差分で揃える。"""
    resources.association(ssm, service, found, resources.detect_association(ssm, service, found))


def described(stub: Stubber, content: dict[str, Any]) -> None:
    """名前で見つけた関連付け（ASSOCIATION）を読む describe-association の応答を積む。

    中身は content にする。
    """
    stub.add_response(
        "describe_association",
        {
            "AssociationDescription": {
                "AssociationId": ASSOCIATION,
                "AssociationName": SSHD,
                **content,
            }
        },
        THE_ASSOCIATION,
    )


def created(stub: Stubber, ports: list[int]) -> None:
    """create-association を積む。AWS は ID を ASSOCIATION と振る。"""
    stub.add_response(
        "create_association",
        {"AssociationDescription": THE_ASSOCIATION},
        {**association_content(ports), "AssociationName": SSHD, "Tags": ASSOCIATION_MARK},
    )


def updated(stub: Stubber, ports: list[int]) -> None:
    """update-association を積む。"""
    stub.add_response(
        "update_association",
        {"AssociationDescription": THE_ASSOCIATION},
        {**THE_ASSOCIATION, "AssociationName": SSHD, **association_content(ports)},
    )


def tagged(stub: Stubber, tags: list[dict[str, str]]) -> None:
    """list-tags-for-resource の応答を積む。"""
    stub.add_response("list_tags_for_resource", {"TagList": tags}, THE_TAGS)


def test_the_association_is_created(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """名前で見つからなければ作り、AWS が振った ID で実行を求める。"""
    with Stubber(ssm) as stub:
        listed_associations(stub)
        created(stub, [2222])
        association_started(stub)
        associate(ssm, service, values([2222]))
        stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert f"association {SSHD}: created\n" in out
    assert f"sshd ports 2222 {NOTE}; run 'temt bake --svc dev-main' to keep them in the AMI" in out


def test_the_association_with_the_default_port_is_created_without_the_note(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """既定の [22] で作ったときは、ポートは変わらず焼く必要も無いので、添えない。"""
    with Stubber(ssm) as stub:
        listed_associations(stub)
        created(stub, [22])
        association_started(stub)
        associate(ssm, service, values())
        stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert f"association {SSHD}: created\n" in out
    assert NOTE not in out


def test_the_association_that_matches_is_left_alone(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """中身もタグも揃っていれば、読むだけで、実行も求めず、知らせもしない。"""
    with Stubber(ssm) as stub:
        association_in_place(stub, [2222, 443])
        associate(ssm, service, values([2222, 443]))
        stub.assert_no_pending_responses()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"Parameters": {sshd.COMMANDS: ["true"]}}, id="commands"),
        pytest.param({"Targets": [{"Key": "tag:Name", "Values": ["other"]}]}, id="targets"),
        pytest.param({"Name": "AWS-RunPowerShellScript"}, id="document"),
        pytest.param({"Parameters": {}}, id="no commands"),
    ],
)
def test_the_association_that_differs_is_updated(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str], change: dict[str, Any]
) -> None:
    """中身を更新したときは実行を求め、いつ効くかを添える。"""
    with Stubber(ssm) as stub:
        listed_associations(stub, ASSOCIATION)
        described(stub, {**association_content([22]), **change})
        tagged(stub, ASSOCIATION_MARK)
        updated(stub, [22])
        association_started(stub)
        associate(ssm, service, values())
        stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert f"association {SSHD}: updated\n" in out
    assert f"sshd ports 22 {NOTE}" in out


def test_new_ports_update_the_association(ssm: SSMClient, service: Service) -> None:
    with Stubber(ssm) as stub:
        listed_associations(stub, ASSOCIATION)
        described_association(stub, [22])
        tagged(stub, ASSOCIATION_MARK)
        updated(stub, [2222])
        association_started(stub)
        associate(ssm, service, values([2222]))
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "present",
    [
        pytest.param([], id="none"),
        pytest.param(
            [{"Key": "temt:service", "Value": "dev"}, {"Key": "Owner", "Value": "me"}],
            id="wrong value",
        ),
    ],
)
def test_the_tag_of_the_association_is_put_right(
    ssm: SSMClient,
    service: Service,
    capsys: pytest.CaptureFixture[str],
    present: list[dict[str, str]],
) -> None:
    """タグだけを直したときは updated と知らせる。中身は変わらないので、実行は求めず、
    効く時期の案内も添えない。
    """
    with Stubber(ssm) as stub:
        listed_associations(stub, ASSOCIATION)
        described_association(stub)
        tagged(stub, present)
        stub.add_response("add_tags_to_resource", {}, {**THE_TAGS, "Tags": ASSOCIATION_MARK})
        associate(ssm, service, values())
        stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert f"association {SSHD}: updated\n" in out
    assert NOTE not in out


def test_the_content_and_the_tag_of_the_association_are_both_put_right(
    ssm: SSMClient, service: Service
) -> None:
    """中身を更新してからタグを直し、最後に実行を求める。"""
    with Stubber(ssm) as stub:
        listed_associations(stub, ASSOCIATION)
        described_association(stub, [22])
        tagged(stub, [])
        updated(stub, [2222])
        stub.add_response("add_tags_to_resource", {}, {**THE_TAGS, "Tags": ASSOCIATION_MARK})
        association_started(stub)
        associate(ssm, service, values([2222]))
        stub.assert_no_pending_responses()


def test_the_first_of_two_associations_with_the_name_is_put_right(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """同じ名前の関連付けが 2 つ以上あれば（手で作ったときなど）、最初の 1 つだけを読んで
    揃え、その実行を求める。ほかのものには触らない。
    """
    with Stubber(ssm) as stub:
        listed_associations(stub, ASSOCIATION, OTHER)
        described_association(stub, [22])
        tagged(stub, ASSOCIATION_MARK)
        updated(stub, [2222])
        association_started(stub)
        associate(ssm, service, values([2222]))
        stub.assert_no_pending_responses()
    assert f"association {SSHD}: updated\n" in capsys.readouterr().out


def test_a_refused_run_is_a_warning(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    """次の起動では実行されるので、apply は続ける。"""
    with Stubber(ssm) as stub:
        listed_associations(stub)
        created(stub, [22])
        stub.add_client_error(
            "start_associations_once",
            service_error_code="AccessDenied",
            expected_params={"AssociationIds": [ASSOCIATION]},
        )
        associate(ssm, service, values())
        stub.assert_no_pending_responses()
    out, err = capsys.readouterr()
    assert f"association {SSHD}: created\n" in out
    assert f"temt: warning: association {SSHD}: cannot ask to run it now: " in err
    assert "it runs when an instance starts next time" in err


def test_find_returns_the_id_and_the_current_version(ssm: SSMClient, service: Service) -> None:
    """名前 <名前>-sshd で探す。"""
    with Stubber(ssm) as stub:
        listed_associations(stub, ASSOCIATION, version="5")
        assert sshd.find(ssm, service) == sshd.Association(ASSOCIATION, "5")
        stub.assert_no_pending_responses()


def test_find_is_none_without_the_association(ssm: SSMClient, service: Service) -> None:
    """名前で見つからなければ（まだ apply していないか、destroy のあと）、None。"""
    with Stubber(ssm) as stub:
        listed_associations(stub)
        assert sshd.find(ssm, service) is None
        stub.assert_no_pending_responses()


def test_find_takes_the_first_of_two(ssm: SSMClient, service: Service) -> None:
    """同じ名前の関連付けが 2 つ以上あれば（手で作ったときなど）、ID の順で最初の 1 つを
    使う。AWS が返す順によらず、どの呼び出しでも同じものを選ぶ。"""
    with Stubber(ssm) as stub:
        listed_associations(stub, OTHER, ASSOCIATION)
        listed_associations(stub, ASSOCIATION, OTHER)
        assert sshd.find(ssm, service) == CURRENT
        assert sshd.find(ssm, service) == CURRENT
        stub.assert_no_pending_responses()


def test_named_returns_every_association_with_the_name(ssm: SSMClient, service: Service) -> None:
    """関連付けの名前は AWS では一意でないので、同じ名前のものをすべて返す。destroy が使う。"""
    with Stubber(ssm) as stub:
        listed_associations(stub, ASSOCIATION, OTHER)
        assert sshd.named(ssm, service) == [CURRENT, sshd.Association(OTHER, VERSION)]
        stub.assert_no_pending_responses()


def test_an_association_with_a_longer_name_is_not_this_one(
    ssm: SSMClient, service: Service
) -> None:
    """名前が同じものだけを取る。"""
    with Stubber(ssm) as stub:
        stub.add_response(
            "list_associations",
            {
                "Associations": [
                    {
                        "AssociationId": OTHER,
                        "AssociationName": f"{SSHD}-old",
                        "AssociationVersion": "1",
                    },
                    {
                        "AssociationId": ASSOCIATION,
                        "AssociationName": SSHD,
                        "AssociationVersion": "3",
                    },
                ]
            },
            {"AssociationFilterList": [{"key": "AssociationName", "value": SSHD}]},
        )
        assert sshd.named(ssm, service) == [sshd.Association(ASSOCIATION, "3")]


def test_every_association_with_the_name_is_deleted(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(ssm) as stub:
        listed_associations(stub, ASSOCIATION, OTHER)
        stub.add_response("delete_association", {}, THE_ASSOCIATION)
        stub.add_client_error(
            "delete_association",
            service_error_code=resources.NO_SUCH_ASSOCIATION,
            expected_params={"AssociationId": OTHER},
        )
        resources.delete_associations(ssm, service)
        stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert f"association {SSHD} ({ASSOCIATION}): deleted\n" in out
    # 読んでから消すまでに消えたものは、既に無かったものとして知らせる
    assert f"association {SSHD} ({OTHER}): absent\n" in out


def test_no_association_to_delete(
    ssm: SSMClient, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(ssm) as stub:
        listed_associations(stub)
        resources.delete_associations(ssm, service)
        stub.assert_no_pending_responses()
    assert f"association {SSHD}: absent\n" in capsys.readouterr().out


def statuses(stub: Stubber, *found: dict[str, Any]) -> None:
    """describe-instance-associations-status の応答を積む。"""
    stub.add_response(
        "describe_instance_associations_status",
        {"InstanceAssociationStatusInfos": list(found)},
        {"InstanceId": INSTANCE},
    )


def test_the_status_on_an_instance(ssm: SSMClient) -> None:
    """ID の違う関連付けは、名前が同じでも見ない。その関連付けのものは、版ごとにすべて返す。"""
    with Stubber(ssm) as stub:
        statuses(
            stub,
            association_ran(sshd.SUCCESS, association_id=OTHER),
            association_ran("Failed", OLD_VERSION),
            association_ran(sshd.SUCCESS),
        )
        assert sshd.instance_status(ssm, CURRENT, INSTANCE) == [
            sshd.Run(sshd.Association(ASSOCIATION, OLD_VERSION), "Failed"),
            sshd.Run(sshd.Association(ASSOCIATION, VERSION), sshd.SUCCESS),
        ]


def test_the_status_is_empty_before_the_first_run(ssm: SSMClient) -> None:
    with Stubber(ssm) as stub:
        statuses(stub, association_ran(sshd.SUCCESS, association_id=OTHER))
        assert sshd.instance_status(ssm, CURRENT, INSTANCE) == []


def ran(status: str, version: str = VERSION, association_id: str = ASSOCIATION) -> sshd.Run:
    """インスタンスでの、ある版の実行の状態。"""
    return sshd.Run(sshd.Association(association_id, version), status)


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        pytest.param([ran("Success")], "Success", id="success"),
        pytest.param([ran("Failed")], "Failed", id="failed"),
        # 実行を待つ 3 つのほかは、すべて終わったものとする
        pytest.param([ran("TimedOut")], "TimedOut", id="timed out"),
        pytest.param([ran("Skipped")], "Skipped", id="skipped"),
        pytest.param([ran("Queued")], None, id="queued"),
        pytest.param([ran("Pending")], None, id="pending"),
        pytest.param([ran("InProgress")], None, id="in progress"),
        pytest.param([], None, id="not listed"),
        # 前の版の状態は、まだ実行されていないものとみなす
        pytest.param([ran("Success", OLD_VERSION)], None, id="previous version"),
        pytest.param(
            [ran("Failed", OLD_VERSION), ran("InProgress")], None, id="previous and waiting"
        ),
        pytest.param(
            [ran("Failed", OLD_VERSION), ran("Success")], "Success", id="previous and current"
        ),
        # ほかの関連付けの同じ版の番号とは取り違えない
        pytest.param([ran("Success", association_id=OTHER)], None, id="another association"),
    ],
)
def test_the_outcome_is_of_the_current_version(runs: list[sshd.Run], expected: str | None) -> None:
    assert sshd.outcome(runs, CURRENT) == expected


def test_another_association_with_the_name_does_not_end_it() -> None:
    """同じ名前の関連付けがほかにあっても、find で見つけた関連付けの今の版だけを見る。"""
    assert sshd.outcome([ran("Pending"), ran("Failed", "7", OTHER)], CURRENT) is None
    assert sshd.outcome([ran("Failed"), ran("Success", "7", OTHER)], CURRENT) == "Failed"


def test_the_status_is_empty_while_ssm_does_not_know_the_instance(ssm: SSMClient) -> None:
    """エージェントが一度も登録していないインスタンスは、まだ実行されていない。"""
    with Stubber(ssm) as stub:
        stub.add_client_error(
            "describe_instance_associations_status",
            service_error_code=sshd.NO_SUCH_INSTANCE,
            expected_params={"InstanceId": INSTANCE},
        )
        assert sshd.instance_status(ssm, CURRENT, INSTANCE) == []


def test_other_errors_on_the_status_are_not_swallowed(ssm: SSMClient) -> None:
    with Stubber(ssm) as stub:
        stub.add_client_error(
            "describe_instance_associations_status", service_error_code="AccessDeniedException"
        )
        with pytest.raises(ClientError):
            _ = sshd.instance_status(ssm, CURRENT, INSTANCE)


@pytest.mark.parametrize("ports", [[22], [2222, 443]])
def test_ports_reads_back_the_ports_in_the_commands(ports: list[int]) -> None:
    """diff が今の関連付けのポートを見せるのに使う。"""
    assert sshd.ports(sshd.commands(ports, "dev-main")) == ports


def test_ports_is_none_for_commands_that_temt_did_not_write() -> None:
    assert sshd.ports(["echo hello"]) is None
