"""sshd の待ち受けポートを SshdPorts に揃える仕組み。design/topics/sshd-ports.md。

State Manager の関連付け <名前>-sshd の中身（インスタンスで動くスクリプト）を作り、
関連付けとインスタンスでの実行の状態を読む。関連付けは名前で探す。関連付けを揃えて
消すのは resources、実行を待つのは instances、表示するのは status である。

botocore の例外は、SSM がそのインスタンスを知らないことを表す誤りだけをここで扱い、
他は呼んだ側にそのまま渡す。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from botocore.exceptions import ClientError

from temt.aws import error_code, field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from types_boto3_ssm.client import SSMClient
    from types_boto3_ssm.type_defs import TargetTypeDef

    from temt.names import Service

# 関連付けが実行する SSM ドキュメント。AWS が用意しているもので、既定の版を使う
DOCUMENT = "AWS-RunShellScript"
# スクリプトを渡す、ドキュメントの引数
COMMANDS = "commands"
# 関連付けの対象を選ぶタグ。temt がインスタンスを探すときと同じ目印
TARGET_TAG = "tag:Name"
# インスタンスでの実行の状態のうち、うまく終わったことを表すもの
SUCCESS = "Success"
# インスタンスでの実行の状態のうち、実行を待っていることを表すもの。ほかはすべて
# 終わったものとする
WAITING = frozenset({"Queued", "Pending", "InProgress"})
# SSM エージェントが一度も登録していないか、応答していないインスタンスについて、
# describe-instance-associations-status が返す誤りの名前
NO_SUCH_INSTANCE = "InvalidInstanceId"

# インスタンスの上の場所。design/topics/sshd-ports.md の Script
CONF_FILE = "/etc/ssh/sshd_config.d/temt-sshd-ports.conf"
STATE_DIR = "/run/temt-sshd"
MOTD_DIR = "/run/motd.d"
MARK_FILE = f"{MOTD_DIR}/50-temt-sshd"
# commands の中の、SshdPorts を埋め込んだ行
WANT_RE = re.compile(r'want="([0-9 ]*)"')
# スクリプトの出力の各行の頭
LOG_PREFIX = "temt-sshd:"
# 読み直させた sshd がすべてのポートで待ち受けるまで待つ回数と間隔（秒）
LISTEN_TRIES = 20
LISTEN_INTERVAL = "0.5"


def commands(ports: Sequence[int], svc: str) -> list[str]:
    """関連付けの commands に渡すスクリプト。1 行 1 要素。

    root で動く。SSM エージェントは commands を行としてつないだファイルを sh で
    実行するので、Ubuntu の dash でも動く POSIX の sh で書く。ports は SshdPorts、
    svc はサービス名で、どちらも設定ファイルとサービス名の規則で確かめた値なので、
    そのまま埋め込める。スクリプトは AWS のコンソールにもそのまま出るので、ASCII
    だけで書き、説明はここのコメントに置く。手順の番号は design/topics/sshd-ports.md の
    Script のもの。
    """
    want = " ".join(str(port) for port in ports)
    return [
        "set -u",
        f'want="{want}"',
        f'svc="{svc}"',
        f"conf={CONF_FILE}",
        f"state={STATE_DIR}",
        f"mark={MARK_FILE}",
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
        # 切り替える前のファイルに戻す。控えが無ければ、temt のファイルは無かった
        'restore() { if [ -f "$state/backup" ]; then mv "$state/backup" "$conf"; '
        'else rm -f "$conf"; fi; }',
        # sshd に読み直させる。ソケット起動（Ubuntu 24.04）では、daemon-reload が
        # sshd_config.d の Port を ssh.socket の待ち受けに写すので、先に必ず行う。
        # ssh.socket を使うかは、動いているかでなく有効かで決める。再起動に失敗して
        # 止まった ssh.socket を、戻したあとに立て直すためである
        "reload() {",
        "    systemctl daemon-reload",
        "    if systemctl is-enabled --quiet ssh.socket; then",
        "        systemctl restart ssh.socket",
        "    else",
        "        systemctl restart ssh.service",
        "    fi",
        "}",
        # 1. sshd が一度も起動していないうちは無く、sshd -t と sshd -T が失敗する
        "[ -d /run/sshd ] || install -d -m 0755 /run/sshd",
        # 2. 揃っていて、すべてで待ち受けていれば sshd に触れない
        "if ! before=$(effective); then",
        '    log "sshd -T failed; left the configuration as it is"',
        "    exit 1",
        "fi",
        'if [ "$(norm "$before")" = "$(norm "$want")" ] && listening "$want"; then',
        '    log "unchanged $(csv "$want")"',
        "    exit 0",
        "fi",
        # 3. 起動してから最初に切り替える前のポートを、AMI のポートとして控える
        'mkdir -p "$state"',
        '[ -e "$state/ami-ports" ] || echo "$before" > "$state/ami-ports"',
        # 4. 今のファイルを控えてから、一時ファイルに書いて置き換える
        'rm -f "$state/backup"',
        '[ -f "$conf" ] && cp -p "$conf" "$state/backup"',
        'mkdir -p "$(dirname "$conf")"',
        ': > "$conf.tmp"',
        'for p in $want; do echo "Port $p" >> "$conf.tmp"; done',
        'mv "$conf.tmp" "$conf"',
        # 5. sshd が受け付ける設定か
        "if ! sshd -t; then",
        "    restore",
        '    log "sshd -t rejected the ports $(csv "$want"); restored the previous configuration"',
        "    exit 1",
        "fi",
        # 6. ほかのファイルにも Port の行があれば、sshd はそれらを足し合わせて待ち受ける
        "if ! after=$(effective); then",
        "    restore",
        '    log "sshd -T failed with the ports $(csv "$want"); '
        'restored the previous configuration"',
        "    exit 1",
        "fi",
        'if [ "$(norm "$after")" != "$(norm "$want")" ]; then',
        "    restore",
        '    log "sshd would listen on $(csv "$after"), as another file also sets Port; '
        'restored the previous configuration"',
        "    exit 1",
        "fi",
        # 7. 読み直させる。いまつながっている接続は切れない
        "reload",
        # 8. すべてのポートで待ち受けるようになったか。ならなければ戻して読み直させる
        "tries=0",
        'until listening "$want"; do',
        "    tries=$((tries + 1))",
        f'    if [ "$tries" -ge {LISTEN_TRIES} ]; then',
        "        restore",
        "        reload",
        '        log "sshd did not listen on $(csv "$want"); restored the previous configuration"',
        "        exit 1",
        "    fi",
        f"    sleep {LISTEN_INTERVAL}",
        "done",
        # 9. AMI のポートと違えば、ログインのメッセージに出す印を書き、同じなら消す
        'ami=$(cat "$state/ami-ports")',
        'if [ "$(norm "$ami")" = "$(norm "$want")" ]; then',
        '    rm -f "$mark"',
        "else",
        f"    mkdir -p {MOTD_DIR}",
        "    echo \"temt: sshd ports were changed from the AMI's "
        '($(csv "$ami") -> $(csv "$want")); '
        'run \'temt bake --svc $svc\' to keep them in the AMI" > "$mark"',
        "fi",
        # 10.
        'log "changed $(csv "$before") -> $(csv "$want")"',
    ]


def ports(lines: Sequence[str]) -> list[int] | None:
    """commands に埋め込んだ SshdPorts。temt が書いた形の行が無ければ None。

    diff が、今の関連付けのポートを見せるのに使う。
    """
    for line in lines:
        found = WANT_RE.fullmatch(line)
        if found:
            return [int(one) for one in found.group(1).split()]
    return None


def targets(service: Service) -> list[TargetTypeDef]:
    """関連付けの対象。Name タグが <名前> のインスタンス。"""
    return [{"Key": TARGET_TAG, "Values": [service.name]}]


class Association(NamedTuple):
    """関連付けの ID と、その今の版。版は update-association のたびに増える。"""

    association_id: str
    version: str


class Run(NamedTuple):
    """インスタンスでの、関連付けのある版の実行の状態（Success、Failed、Pending など）。"""

    association: Association
    status: str


def named(ssm: SSMClient, service: Service) -> list[Association]:
    """名前が <名前>-sshd の関連付けの ID と今の版。関連付けの名前は AWS では一意でない。

    temt はサービスごとに 1 つだけ作るので、ふつうは 1 つである。destroy はすべてを
    消す。フィルタが名前の一部でも一致させる場合に備え、名前が同じものだけを取る。
    2 つ以上あっても、どの呼び出しでも同じ 1 つを「最初」に選ぶよう、ID の順に並べる。
    """
    name = service.sshd_association
    found = [
        Association(field(one, "AssociationId"), field(one, "AssociationVersion"))
        for page in ssm.get_paginator("list_associations").paginate(
            AssociationFilterList=[{"key": "AssociationName", "value": name}]
        )
        for one in page.get("Associations", [])
        if one.get("AssociationName") == name
    ]
    return sorted(found)


def find(ssm: SSMClient, service: Service) -> Association | None:
    """名前が <名前>-sshd の関連付けの ID と今の版。無ければ None。

    2 つ以上あれば（手で作ったときなど。ふつうは起きない）、ID の順で最初の 1 つを使う。
    """
    found = named(ssm, service)
    return found[0] if found else None


def instance_status(ssm: SSMClient, current: Association, instance_id: str) -> list[Run]:
    """そのインスタンスでの関連付け current の実行の状態を、どの版のものかと合わせて返す。

    状態の一覧に無ければ空。SSM がまだそのインスタンスを知らないときも、実行されて
    いないので空とする。update-association の直後は、前の版の状態が残っていることが
    ある。
    """
    try:
        return [
            Run(
                Association(field(one, "AssociationId"), field(one, "AssociationVersion")),
                str(one.get("Status") or ""),
            )
            for page in ssm.get_paginator("describe_instance_associations_status").paginate(
                InstanceId=instance_id
            )
            for one in page.get("InstanceAssociationStatusInfos", [])
            if one.get("AssociationId") == current.association_id
        ]
    except ClientError as e:
        if error_code(e) != NO_SUCH_INSTANCE:
            raise
        return []


def outcome(runs: Sequence[Run], current: Association) -> str | None:
    """今の版の実行が終わっていれば、その状態を返す。まだ終わっていなければ None。

    current は find で読んだ今の版。前の版の状態と、実行を待っている状態は、まだ
    終わっていないものとみなす。
    """
    done = [run.status for run in runs if run.association == current and run.status not in WAITING]
    return done[0] if done else None
