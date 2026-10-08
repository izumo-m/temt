# Glossary

English terms used in design/ and the Japanese terms the user docs (docs/usage.md,
docs/conf.md, docs/apps.md, docs/apps/, README.md, CONTRIBUTING.md) use for them.
When writing Japanese docs from the design, use the Japanese term in this table and
avoid the ones marked "not".

## Concepts

| English | Japanese | Notes |
| --- | --- | --- |
| service | サービス | One purpose, one config file, one instance |
| service name, `<svc>` | サービス名、`<svc>` | |
| `<name>` (`temt-<svc>`) | 名前、`<名前>` | Base of every resource name |
| config file (`<svc>.toml`) | 設定ファイル | |
| conf directory | `TEMT_CONF_DIR` のディレクトリ、設定ファイルとアプリを置くディレクトリ | `~/.config/temt` by default |
| ssh directory | ssh ディレクトリ、`TEMT_SSH_DIR` のディレクトリ | `~/.ssh/temt` by default |
| service command (`~/.local/bin/<name>`) | サービスのコマンド | |
| local, on the client | 手元 | The user's side, as opposed to the instance or AWS |
| machine (countable) | マシン | Not 手元 when counting (複数のマシン) |
| environment (account and region) | 環境 | |
| profile | プロファイル | AWS profile |
| region | リージョン | |
| location | 場所 | Not 置き場 |
| global IP address | グローバル IP アドレス | The client's address as seen from the Internet |
| lock | ロック | Per-service lock file |
| default | 既定 | |
| credentials | 資格情報 | |
| hierarchy (of SSM parameter names) | 階層 | |
| stdout, stderr | 標準出力、標準エラー出力 | |
| environment line (account, region, profile) | 環境の行 | Printed on stderr by commands without `--svc` |
| deadline | 締め切り | Timeout of one waiting stage |

## Command line

| English | Japanese | Notes |
| --- | --- | --- |
| command-line option (`--xxx`) | コマンドラインオプション | Not フラグ |
| app option (`<name>=<value>`) | オプション | |
| usage error | 引数の誤り | Exit code 2 |
| confirmation (`[y/N]`) | 確認 | |
| short form (`apply` for `service apply`) | 短縮形 | |
| list table | 一覧の表 | Output of the `list` commands |
| column, header | 列、見出し | |
| report, notify (a progress line) | 知らせる | Not 報せる |
| `--async` waits only until AWS accepts the request | `--async` は AWS が要求を受け付けるまでしか待たない | Describe `--async` by what it waits for, not by 戻る or 終わる |

## AWS and the instance

| English | Japanese | Notes |
| --- | --- | --- |
| instance | インスタンス | |
| SSM agent | SSM エージェント | |
| SSM parameter | SSM パラメータ | |
| inline policy, managed policy | インラインポリシー、管理ポリシー | |
| listening port (of sshd) | 待ち受けポート | |
| fingerprint | フィンガープリント | |
| snapshot | スナップショット | |
| root volume | ルートボリューム | |
| deregister (an AMI) | 登録を解除する | Not 登録解除 |
| Host block (in the ssh config) | Host ブロック | |
| route (`TEMT_LOGIN`) | 経路 | |
| IAM role | ロール、IAM のロール | |
| instance profile | インスタンスプロファイル | Not プロファイル alone, which is the AWS profile |
| start (launch or resume) | 起動する、再開する | 再開 is starting a stopped instance |
| stop | 停止する、止める | |
| terminate | 終了する | |
| reboot | 再起動する | |
| intermediate state (`pending`, `stopping`) | 起動の途中、停止の途中 | Name the state; 途中の状態 only as a general term |
| shutting down (`shutting-down`) | 終了の途中 | |
| launch template | 起動テンプレート | |
| default version | 既定バージョン | |
| security group | セキュリティグループ | Never shortened to グループ |
| ingress rule | 受信規則 | |
| association (State Manager) | 関連付け | |
| key pair | キーペア | EC2 key pair. temt registers none |
| login key | ログインの鍵 | The key ssh uses for the instance; `proxy` sends its public key with EC2 Instance Connect |
| EC2 Instance Connect | EC2 Instance Connect | |
| bake | 焼く、AMI にする | |
| tag | タグ | AWS resource tag only |
| git tag | git のタグ | Never just タグ |
| direct connection (`TEMT_LOGIN=direct`) | 直接接続 | |
| login message (motd) | ログインのメッセージ | |
| marker (`/run/motd.d/50-temt-sshd`) | ログインのメッセージ | The user docs name only the message it shows |
| marker comment (in `~/.ssh/config`) | 目印のコメント | |
| clean up, tear down | 片づける | |

## Apps

| English | Japanese | Notes |
| --- | --- | --- |
| app | アプリ | A directory with `manifest.toml` and `install` |
| app ID, `<app ID>` | アプリ ID、`<アプリID>` | No space inside the placeholder |
| bundled app | temt と一緒に配るアプリ | Under `src/temt/apps/` |
| deploy, remove (an app) | 入れる、外す | |
| removed without baking (`removed`) | 焼かずに外した | Still in the AMI until baked |
| deploy record | 記録、`deploy` の記録 | SSM parameter `/<name>/<app ID>/deploy` |
| app value | アプリの値 | SSM parameter `/<name>/<app ID>/<key>` |
| app status | アプリの状態 | SSM parameter `/<name>/<app ID>/status` |
| key (of an app value), `<key>` | キー、`<キー>` | Not 項目 |
| idle shutdown | 自動終了 | The bundled app `idle-shutdown` |
| idle limit | 制限時間 | |
| last activity | 最終利用時刻、最後に使われた時刻 | |
| in use | 使用中 | |
| check (by the timer), in-use check (in-use.d) | 判定、使用中の判定 | |
| power off | 電源を切る | |

## TOML

| English | Japanese | Notes |
| --- | --- | --- |
| table (`[x]`) | テーブル | Not セクション |
| array of tables (`[[x]]`) | テーブルの配列 | Not セクション |
| `<policy name>` (`[[role.policy.<policy name>]]`) | `<ポリシー名>` | |
| key (of the config file), value | 項目、値 | |
