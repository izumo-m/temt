"""set-port がインスタンスで動かす、sshd の待ち受けポートを変えるスクリプトのテスト。

スクリプトは、文字列として確かめるほか、/etc と /run の場所を一時ディレクトリに
置き換え、sshd、ss、systemctl、sleep を仮のコマンドに差し替えて、実際に sh で流す。
flock は本物を使う。スクリプトの出力を読む outcome も確かめる。
"""

import fcntl
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from temt import sshd

# スクリプトを流す sh。SSM エージェントが使う Ubuntu の sh は dash なので、あればそれで
SHELL = shutil.which("dash") or "/bin/sh"


def script(ports: list[int]) -> str:
    """スクリプトを 1 つの文字列にする。SSM エージェントも行をつないで流す。"""
    return "\n".join(sshd.script(ports)) + "\n"


def test_the_script_is_one_line_per_element() -> None:
    lines = sshd.script([2222, 443])
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


def test_the_script_writes_the_file_of_the_design() -> None:
    """Port の行を書くファイルと、ポートの埋め込み方。"""
    lines = sshd.script([2222, 443])
    assert sshd.CONF_FILE == "/etc/ssh/sshd_config.d/temt-sshd-ports.conf"
    assert f"conf={sshd.CONF_FILE}" in lines
    assert 'want="2222 443"' in lines


# 以下、スクリプトを実際に sh で流して確かめる。/etc と /run は一時ディレクトリの下に
# 置き換え、sshd、ss、systemctl、sleep は、状態をファイルで持つ仮のコマンドに差し替える。
# 仮のコマンドは、環境変数 ROOT（置き換え先）と SIM（状態を置くディレクトリ）を見る。

# 仮の sshd。sshd_config と sshd_config.d の Port を足し合わせたものが効いているポートで、
# 無ければ 22。コメントの行は数えない。/run/sshd が無ければ -t も -T も失敗する。
# $SIM/reject があれば -t が失敗する。$SIM/dump_fails があるか、$SIM/dump_fails_on の
# ポートが書かれていれば、-T が失敗する
FAKE_SSHD = r"""#!/bin/sh
[ -d "$ROOT/run/sshd" ] || { echo "Missing privilege separation directory" >&2; exit 255; }
ports() {
    cat "$ROOT/etc/ssh/sshd_config" "$ROOT"/etc/ssh/sshd_config.d/*.conf 2>/dev/null \
        | awk 'tolower($1) == "port" { print $2 }'
}
case "$1" in
-t)
    [ -e "$SIM/reject" ] && { echo "bad configuration" >&2; exit 255; }
    exit 0 ;;
-T)
    [ -e "$SIM/dump_fails" ] && { echo "cannot dump the configuration" >&2; exit 255; }
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

# 仮の ss。$SIM/listening の「ポート 持ち主」の行から、待ち受けを IPv4 と IPv6 の 2 行ずつ
# 出す。sport の条件があれば、そのポートのものだけを出す
FAKE_SS = r"""#!/bin/sh
[ "$1" = -Hltnp ] || { echo "ss: unexpected options: $*" >&2; exit 2; }
want=""
if [ $# -gt 1 ]; then
    want=${2#sport = :}
fi
while read -r port owner; do
    if [ -z "$want" ] || [ "$port" = "$want" ]; then
        echo "LISTEN 0 4096 0.0.0.0:$port 0.0.0.0:* users:((\"$owner\",pid=100,fd=3))"
        echo "LISTEN 0 4096 [::]:$port [::]:* users:((\"$owner\",pid=100,fd=4))"
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

# 仮の sleep。待たずに、渡された秒数を $SIM/sleeps に書く
FAKE_SLEEP = r"""#!/bin/sh
echo "$*" >> "$SIM/sleeps"
"""

FAKES = {"sshd": FAKE_SSHD, "ss": FAKE_SS, "systemctl": FAKE_SYSTEMCTL, "sleep": FAKE_SLEEP}

# スクリプトの中の、インスタンスの上の場所。/etc か /run で始まるもの
PLACE_RE = re.compile(r"(?<![\w.-])/(?:etc|run)/")
# 素の Ubuntu の sshd_config。Port の行はコメントにしてある
UBUNTU_SSHD_CONFIG = (
    "Include /etc/ssh/sshd_config.d/*.conf\n#Port 22\nKbdInteractiveAuthentication no\n"
)
# ssh.socket を使う構成と使わない構成で、1 回の読み直しに呼ぶ systemctl
SOCKET_RELOAD = ["daemon-reload", "is-enabled --quiet ssh.socket", "restart ssh.socket"]
SERVICE_RELOAD = ["daemon-reload", "is-enabled --quiet ssh.socket", "restart ssh.service"]


def localized(text: str, root: Path) -> str:
    """スクリプトの /etc と /run の場所を、root の下に置き換える。"""
    assert PLACE_RE.search(text), "no place to replace"
    return PLACE_RE.sub(lambda found: f"{root}{found.group(0)}", text)


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

    def socket_activated(self) -> None:
        """ssh.socket を有効にし、systemd が 22 で待ち受けている姿にする。"""
        (self.sim / "socket").touch()
        self.listen("22 systemd")

    def listen(self, *lines: str) -> None:
        """待ち受けを「ポート 持ち主」の行で書く。"""
        _ = (self.sim / "listening").write_text("".join(f"{line}\n" for line in lines))

    def listening(self) -> list[str]:
        """いまの待ち受けの「ポート 持ち主」の行。並びは問わない。"""
        return sorted((self.sim / "listening").read_text().splitlines())

    def write(self, place: str, text: str) -> None:
        """インスタンスの上の場所にファイルを書く。"""
        path = self.at(place)
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)

    def calls(self) -> list[str]:
        """直前の run で呼ばれた systemctl の引数。"""
        found = self.sim / "calls"
        return found.read_text().splitlines() if found.exists() else []

    def sleeps(self) -> list[str]:
        """直前の run で sleep に渡された秒数。"""
        found = self.sim / "sleeps"
        return found.read_text().splitlines() if found.exists() else []

    def command(self, ports: list[int]) -> tuple[list[str], dict[str, str]]:
        """ports にするスクリプトを書き、流すコマンドと環境変数を返す。記録は毎回消す。"""
        for name in ("calls", "sleeps"):
            (self.sim / name).unlink(missing_ok=True)
        path = self.root / "script.sh"
        _ = path.write_text(localized(script(ports), self.root))
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "ROOT": str(self.root),
            "SIM": str(self.sim),
        }
        return [SHELL, str(path)], env

    def run(self, ports: list[int]) -> subprocess.CompletedProcess[str]:
        """ports にするスクリプトを流す。"""
        args, env = self.command(ports)
        return subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=60, env=env
        )

    def report(self, done: subprocess.CompletedProcess[str]) -> list[str]:
        """スクリプトの標準出力の行。場所の置き換えを戻す。

        どの結果でも、最後の行が結果で、その前の行が実際に待ち受けているポートである
        ことも確かめる。出力は ASCII だけである。
        """
        lines = done.stdout.replace(str(self.root), "").splitlines()
        assert done.stdout.isascii()
        assert all(line.startswith(f"{sshd.LOG_PREFIX} ") for line in lines)
        assert lines[-2].startswith(f"{sshd.LOG_PREFIX} {sshd.LISTENING} ")
        return lines


@pytest.fixture
def box(tmp_path: Path) -> Box:
    """起動したばかりの素の Ubuntu に見立てた場所。sshd が 22 で待ち受けている。

    /run/sshd と sshd_config.d はまだ無く、スクリプトが作る。ssh.socket は有効でない。
    """
    made = Box(root=tmp_path / "root", sim=tmp_path / "sim", bin=tmp_path / "bin")
    for directory in (made.root / "run", made.sim, made.bin):
        directory.mkdir(parents=True)
    for name, text in FAKES.items():
        fake = made.bin / name
        _ = fake.write_text(text)
        fake.chmod(0o755)
    made.write("/etc/ssh/sshd_config", UBUNTU_SSHD_CONFIG)
    made.listen("22 sshd")
    _ = (made.sim / "generated").write_text("22\n")
    return made


def test_it_changes_the_ports_through_the_socket(box: Box) -> None:
    """ソケット起動で 22 から 2222 に切り替える。"""
    box.socket_activated()
    done = box.run([2222])
    assert (done.returncode, box.report(done), done.stderr) == (
        0,
        [
            "temt-sshd: before 22",
            "temt-sshd: listening 2222",
            "temt-sshd: changed 22 -> 2222",
        ],
        "",
    )
    assert box.conf.read_text() == "Port 2222\n"
    assert box.calls() == SOCKET_RELOAD
    assert box.listening() == ["2222 systemd"]
    # 手順 1 と 3 で作る
    assert box.at("/run/sshd").is_dir()
    assert sorted(path.name for path in box.conf.parent.iterdir()) == [box.conf.name]


def test_without_socket_activation_the_service_restarts(box: Box) -> None:
    """ポートは渡された順にファイルへ書く。"""
    done = box.run([2222, 443])
    assert (done.returncode, box.report(done)) == (
        0,
        [
            "temt-sshd: before 22",
            "temt-sshd: listening 443,2222",
            "temt-sshd: changed 22 -> 2222,443",
        ],
    )
    assert box.conf.read_text() == "Port 2222\nPort 443\n"
    assert box.calls() == SERVICE_RELOAD
    assert box.listening() == ["2222 sshd", "443 sshd"]


def test_it_waits_until_sshd_listens(box: Box) -> None:
    """再起動の直後にまだ待ち受けていなければ、間を置いて見直す。"""
    box.socket_activated()
    # 1 回目の sleep で、ssh.socket が待ち受けるようになる姿にする
    sleep = box.bin / "sleep"
    _ = sleep.write_text(
        FAKE_SLEEP + 'grep -q systemd "$SIM/listening" || echo "2222 systemd" >> "$SIM/listening"\n'
    )
    _ = (box.bin / "systemctl").write_text(
        FAKE_SYSTEMCTL.replace(
            '"restart ssh.socket") socket ;;', '"restart ssh.socket") serve systemd ;;'
        )
    )
    done = box.run([2222])
    assert (done.returncode, box.report(done)[-1]) == (0, "temt-sshd: changed 22 -> 2222")
    assert box.sleeps() == [sshd.LISTEN_INTERVAL]


def test_it_leaves_sshd_alone_when_the_ports_already_match(box: Box) -> None:
    box.socket_activated()
    _ = box.run([2222])
    written = box.conf.stat().st_mtime_ns
    done = box.run([2222])
    assert (done.returncode, box.report(done)) == (
        0,
        [
            "temt-sshd: before 2222",
            "temt-sshd: listening 2222",
            "temt-sshd: unchanged 2222",
        ],
    )
    assert box.calls() == []
    assert box.conf.stat().st_mtime_ns == written


def test_the_order_of_the_ports_does_not_matter(box: Box) -> None:
    box.socket_activated()
    _ = box.run([2222, 443])
    done = box.run([443, 2222])
    assert (done.returncode, box.report(done)[-1]) == (0, "temt-sshd: unchanged 443,2222")
    assert box.calls() == []
    # 書き直さないので、前の順のまま
    assert box.conf.read_text() == "Port 2222\nPort 443\n"


def test_an_ubuntu_without_a_port_line_is_unchanged_on_22(box: Box) -> None:
    """Port の行が無ければ 22 で、何も書かない。"""
    done = box.run([22])
    assert (done.returncode, box.report(done)[-1]) == (0, "temt-sshd: unchanged 22")
    assert not box.conf.exists()
    assert box.calls() == []


def test_ports_written_but_not_listening_are_restarted(box: Box) -> None:
    """ファイルが揃っていても、待ち受けていなければ読み直させる。

    before は、設定ではなく、実際に待ち受けているポートを出す。
    """
    box.write(sshd.CONF_FILE, "Port 2222\n")
    done = box.run([2222])
    assert (done.returncode, box.report(done)) == (
        0,
        [
            "temt-sshd: before 22",
            "temt-sshd: listening 2222",
            "temt-sshd: changed 2222 -> 2222",
        ],
    )
    assert box.calls() == SERVICE_RELOAD


def test_another_process_on_a_port_is_not_sshd(box: Box) -> None:
    """効いているポートが揃っていても、別のプロセスの待ち受けは sshd のものと数えない。"""
    box.write(sshd.CONF_FILE, "Port 2222\nPort 443\n")
    box.listen("2222 sshd", "443 nginx")
    done = box.run([2222, 443])
    lines = box.report(done)
    assert done.returncode == 1
    assert lines[-1] == "temt-sshd: failed after the restart: sshd did not listen on 2222,443"
    assert lines[-2] == "temt-sshd: listening 2222"
    assert box.calls() == SERVICE_RELOAD


def test_sshd_t_rejects_the_ports(box: Box) -> None:
    """sshd -t が通らなければ、再起動せずに 1 で終わる。書いたファイルは戻さない。"""
    (box.sim / "reject").touch()
    done = box.run([2222])
    assert (done.returncode, box.report(done)) == (
        1,
        [
            "temt-sshd: before 22",
            "temt-sshd: listening 22",
            "temt-sshd: failed before the restart: sshd -t rejected the ports 2222",
        ],
    )
    # sshd -t の理由は標準エラー出力に出る
    assert done.stderr == "bad configuration\n"
    assert box.conf.read_text() == "Port 2222\n"
    assert box.calls() == []
    assert box.listening() == ["22 sshd"]


@pytest.mark.parametrize(
    ("place", "text"),
    [
        ("/etc/ssh/sshd_config", f"{UBUNTU_SSHD_CONFIG}Port 22\n"),
        ("/etc/ssh/sshd_config.d/50-other.conf", "Port 22\n"),
    ],
    ids=["sshd_config", "sshd_config.d"],
)
def test_another_file_with_port_lines_is_named(box: Box, place: str, text: str) -> None:
    """ほかのファイルにも Port の行があれば、sshd はそれらを足し合わせるので止める。

    temt のファイルも含めて、Port の行のあるファイルを挙げる。
    """
    box.write(place, text)
    done = box.run([2222])
    files = f"{place} {sshd.CONF_FILE}"
    assert (done.returncode, box.report(done)) == (
        1,
        [
            "temt-sshd: before 22",
            "temt-sshd: listening 22",
            "temt-sshd: failed before the restart: sshd would listen on 22,2222; "
            f"Port is set in {files}",
        ],
    )
    assert box.calls() == []


def test_a_port_line_in_lower_case_is_found(box: Box) -> None:
    """sshd は項目名の大文字と小文字を区別しないので、port の行も挙げる。"""
    box.write("/etc/ssh/sshd_config.d/50-other.conf", "  port 2200\n")
    box.listen("2200 sshd")
    done = box.run([2222])
    assert done.returncode == 1
    assert box.report(done)[-1] == (
        "temt-sshd: failed before the restart: sshd would listen on 2200,2222; "
        f"Port is set in /etc/ssh/sshd_config.d/50-other.conf {sshd.CONF_FILE}"
    )


def test_sshd_t_capital_fails_at_first(box: Box) -> None:
    """最初の sshd -T が失敗すれば、何も書かずに 1 で終わる。"""
    (box.sim / "dump_fails").touch()
    done = box.run([2222])
    assert (done.returncode, box.report(done)) == (
        1,
        [
            "temt-sshd: before 22",
            "temt-sshd: listening 22",
            "temt-sshd: failed before the restart: sshd -T failed",
        ],
    )
    assert not box.conf.parent.exists()
    assert box.calls() == []


def test_sshd_t_capital_fails_with_the_new_ports(box: Box) -> None:
    _ = (box.sim / "dump_fails_on").write_text("2222\n")
    done = box.run([2222])
    assert (done.returncode, box.report(done)[-1]) == (
        1,
        "temt-sshd: failed before the restart: sshd -T failed with the ports 2222",
    )
    assert box.calls() == []


def test_sshd_does_not_listen_after_the_restart(box: Box) -> None:
    """読み直した sshd が待ち受けなければ、決まった回数だけ見直して 1 で終わる。"""
    box.listen("22 sshd", "2222 nginx")
    done = box.run([2222])
    assert (done.returncode, box.report(done)) == (
        1,
        [
            "temt-sshd: before 22",
            # 何も待ち受けていない
            "temt-sshd: listening none",
            "temt-sshd: failed after the restart: sshd did not listen on 2222",
        ],
    )
    assert box.calls() == SERVICE_RELOAD
    assert box.sleeps() == [sshd.LISTEN_INTERVAL] * (sshd.LISTEN_TRIES - 1)
    # 戻さない
    assert box.conf.read_text() == "Port 2222\n"
    assert box.listening() == ["2222 nginx"]


def test_a_failed_socket_listens_on_no_port(box: Box) -> None:
    """1 つでも待ち受けられないと、ssh.socket は全体が止まり、どのポートでも待ち受けない。"""
    box.socket_activated()
    box.listen("22 systemd", "443 nginx")
    done = box.run([2222, 443])
    lines = box.report(done)
    assert done.returncode == 1
    assert lines[-2:] == [
        "temt-sshd: listening none",
        "temt-sshd: failed after the restart: sshd did not listen on 2222,443",
    ]
    assert box.calls() == SOCKET_RELOAD
    assert box.listening() == ["443 nginx"]


def test_the_script_waits_for_another_run(box: Box) -> None:
    """Ctrl-C で手元が止めても前のコマンドは走り続けるので、ロックを待ってから動く。"""
    lock = box.at(sshd.LOCK_FILE)
    with lock.open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        args, env = box.command([2222])
        running = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        try:
            time.sleep(0.5)
            assert running.poll() is None
            assert not box.at("/run/sshd").exists()
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        out, _ = running.communicate(timeout=60)
    assert running.returncode == 0
    assert out.splitlines()[-1] == "temt-sshd: changed 22 -> 2222"


# スクリプトの出力を読む


@pytest.mark.parametrize(
    "last",
    ["changed 22 -> 2222,443", "unchanged 2222", "changed 2222 -> 2222"],
)
def test_the_outcome_of_a_success(last: str) -> None:
    found = sshd.outcome(f"temt-sshd: before 22\ntemt-sshd: listening 2222\ntemt-sshd: {last}\n")
    assert found == sshd.Outcome(last, (22,))
    assert found.succeeded


@pytest.mark.parametrize(
    "last",
    [
        "failed before the restart: sshd -t rejected the ports 2222",
        "failed after the restart: sshd did not listen on 2222",
        "listening 2222",
        # 結果の語の後ろに空白が無ければ、成功と読まない
        "changed",
        "unchangedX 22",
    ],
)
def test_the_outcome_of_a_failure(last: str) -> None:
    found = sshd.outcome(f"temt-sshd: before 2222,443\ntemt-sshd: {last}\n")
    assert found == sshd.Outcome(last, (2222, 443))
    assert not found.succeeded


def test_the_outcome_reads_only_the_lines_of_the_script() -> None:
    """temt-sshd: で始まらない行は、最後にあっても読まない。"""
    output = (
        "Warning: something from a command\n"
        "temt-sshd: before 22\n"
        "temt-sshd: listening 2222\n"
        "temt-sshd: changed 22 -> 2222\n"
        "done\n"
    )
    assert sshd.outcome(output) == sshd.Outcome("changed 22 -> 2222", (22,))


@pytest.mark.parametrize(
    "output",
    ["", "temt-sshd: before \n", "temt-sshd: before 22, 443\n", "temt-sshd: before x\n"],
    ids=["nothing", "empty", "spaced", "not a number"],
)
def test_the_outcome_without_the_previous_ports(output: str) -> None:
    found = sshd.outcome(output)
    assert found.before == ()
    assert not found.succeeded


def test_the_outcome_of_the_script(box: Box) -> None:
    """スクリプトが実際に出したものを読む。"""
    done = box.run([2222, 443])
    assert sshd.outcome(done.stdout) == sshd.Outcome("changed 22 -> 2222,443", (22,))
