"""set-port がインスタンスで動かす、sshd の待ち受けポートを変えるスクリプト。

design/topics/sshd-ports.md の Script。スクリプトを作り、その出力を読む。Run Command で
送って待つのは setport である。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

# インスタンスの上の場所。temt が Port の行を書くファイルと、スクリプトを 1 つずつ
# 実行させるロックのファイル
CONF_FILE = "/etc/ssh/sshd_config.d/temt-sshd-ports.conf"
LOCK_FILE = "/run/temt-sshd.lock"
# スクリプトの出力の各行の頭
LOG_PREFIX = "temt-sshd:"
# 読み直させた sshd がすべてのポートで待ち受けるまで待つ回数と間隔（秒）
LISTEN_TRIES = 20
LISTEN_INTERVAL = "0.5"
# 最後の行の頭。成功は changed か unchanged、失敗は sshd を再起動する前か後か
CHANGED = "changed"
UNCHANGED = "unchanged"
FAILED_BEFORE = "failed before the restart:"
FAILED_AFTER = "failed after the restart:"
# 最初に知らせる、変える前の待ち受けポートの行と、最後に知らせる、実際に待ち受けている
# ポートの行の頭
BEFORE = "before"
LISTENING = "listening"
_PORTS_RE = re.compile(r"[0-9]+(?:,[0-9]+)*")


def script(ports: Sequence[int]) -> list[str]:
    """Run Command の commands に渡すスクリプト。1 行 1 要素。

    root で動く。SSM エージェントは commands を行としてつないだファイルを sh で実行
    するので、Ubuntu の dash でも動く POSIX の sh で書く。ports は引数の規則で確かめた
    値なので、そのまま埋め込める。スクリプトは AWS のコンソールにもそのまま出るので、
    ASCII だけで書き、説明はここのコメントに置く。手順の番号は
    design/topics/sshd-ports.md の Script のもの。元に戻さず、失敗はそのまま知らせる。
    最後の行は、いつも結果（CHANGED、UNCHANGED、FAILED_BEFORE、FAILED_AFTER）で、その
    前の行に、実際に待ち受けているポートを出す。
    """
    want = " ".join(str(port) for port in ports)
    return [
        "set -u",
        f'want="{want}"',
        f"conf={CONF_FILE}",
        f'log() {{ echo "{LOG_PREFIX} $*"; }}',
        # 並びを、順序を問わずに比べられる形にする
        "norm() { printf '%s\\n' $1 | sort -n | uniq | xargs; }",
        # メッセージに書く形。ポートを , でつなぐ
        "csv() { echo \"$1\" | tr ' ' ','; }",
        # 効いているポート。sshd -T が失敗すれば 1 を返す
        "effective() {",
        "    out=$(sshd -T) || return 1",
        "    printf '%s\\n' \"$out\" | awk '$1 == \"port\" { print $2 }' | xargs",
        "}",
        # 並びのすべてのポートで、sshd か systemd（ソケット起動で待ち受けを持つ）が
        # 待ち受けているか。ほかのプロセスの待ち受けは数えない
        "listening() { for p in $1; do "
        'ss -Hltnp "sport = :$p" | grep -Eq \'"(sshd|systemd)"\' || return 1; done; }',
        # 実際に待ち受けているポート。直し方を考える手がかりに、最後に必ず出す
        "now() { ss -Hltnp | grep -E '\"(sshd|systemd)\"' "
        "| awk '{ n = split($4, a, \":\"); print a[n] }' | sort -nu | xargs; }",
        # Port の行を書いているファイル。ほかのファイルと重なったときに名前を出す
        "portfiles() { grep -lis '^[[:space:]]*port[[:space:]]' /etc/ssh/sshd_config "
        "/etc/ssh/sshd_config.d/*.conf 2>/dev/null | xargs; }",
        # 待ち受けているポートの表示。無ければ none
        'shown() { p=$(now); csv "${p:-none}"; }',
        # 終わる。待ち受けているポートを出し、結果を最後の行にする
        'end() { code=$1; shift; log "listening $(shown)"; log "$*"; exit "$code"; }',
        # Ctrl-C で手元が止めてもコマンドは走り続けるので、1 つずつ実行させる
        f"exec 9> {LOCK_FILE}",
        "flock 9",
        # 1. sshd が一度も起動していないうちは無く、sshd -t と sshd -T が失敗する
        "[ -d /run/sshd ] || install -d -m 0755 /run/sshd",
        # 変える前に実際に待ち受けているポート。失敗したときにログインする先と、受信規則の
        # 案内の元になる。設定（sshd -T）とは、前に失敗したときなどに食い違う
        'log "before $(shown)"',
        # 2. 揃っていて、すべてで待ち受けていれば sshd に触れない
        "if ! before=$(effective); then",
        '    end 1 "failed before the restart: sshd -T failed"',
        "fi",
        'if [ "$(norm "$before")" = "$(norm "$want")" ] && listening "$want"; then',
        '    end 0 "unchanged $(csv "$want")"',
        "fi",
        # 3. 一時ファイルに書いて置き換える
        'mkdir -p "$(dirname "$conf")"',
        ': > "$conf.tmp"',
        'for p in $want; do echo "Port $p" >> "$conf.tmp"; done',
        'mv "$conf.tmp" "$conf"',
        # 4. sshd が受け付ける設定か
        "if ! sshd -t; then",
        '    end 1 "failed before the restart: sshd -t rejected the ports $(csv "$want")"',
        "fi",
        # 5. ほかのファイルにも Port の行があれば、sshd はそれらを足し合わせて待ち受ける
        "if ! after=$(effective); then",
        '    end 1 "failed before the restart: sshd -T failed with the ports $(csv "$want")"',
        "fi",
        'if [ "$(norm "$after")" != "$(norm "$want")" ]; then',
        '    end 1 "failed before the restart: sshd would listen on $(csv "$after"); '
        'Port is set in $(portfiles)"',
        "fi",
        # 6. 読み直させる。ソケット起動（Ubuntu 24.04）では、daemon-reload が
        # sshd_config.d の Port を ssh.socket の待ち受けに反映するので、先に必ず行う。
        # いまつながっている接続は切れない
        "systemctl daemon-reload",
        "if systemctl is-enabled --quiet ssh.socket; then",
        "    systemctl restart ssh.socket",
        "else",
        "    systemctl restart ssh.service",
        "fi",
        # 7. すべてのポートで待ち受けるようになったか
        "tries=0",
        'until listening "$want"; do',
        "    tries=$((tries + 1))",
        f'    if [ "$tries" -ge {LISTEN_TRIES} ]; then',
        '        end 1 "failed after the restart: sshd did not listen on $(csv "$want")"',
        "    fi",
        f"    sleep {LISTEN_INTERVAL}",
        "done",
        # 8.
        'end 0 "changed $(csv "$before") -> $(csv "$want")"',
    ]


class Outcome(NamedTuple):
    """スクリプトの出力から読んだもの。

    last は最後の行（頭の LOG_PREFIX を除く）、before は変える前に実際に待ち受けて
    いたポート（無いか読めなければ空）。
    """

    last: str
    before: tuple[int, ...]

    @property
    def succeeded(self) -> bool:
        """変えたか、もともと揃っていたか。"""
        return self.last.startswith((f"{CHANGED} ", f"{UNCHANGED} "))


def outcome(output: str) -> Outcome:
    """スクリプトの標準出力を読む。temt-sshd: で始まる行だけを見る。"""
    lines = [
        line.removeprefix(LOG_PREFIX).strip()
        for line in output.splitlines()
        if line.startswith(LOG_PREFIX)
    ]
    before: tuple[int, ...] = ()
    for line in lines:
        word, _, rest = line.partition(" ")
        if word == BEFORE and _PORTS_RE.fullmatch(rest):
            before = tuple(int(one) for one in rest.split(","))
    return Outcome(lines[-1] if lines else "", before)
