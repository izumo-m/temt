# temt の使い方

temt のコマンドの使い方を、扱う対象ごとに説明する。はじめて使うときの流れは
[README.md](../README.md#使い方の例) にある。

## 用意するもの

- Linux（Ubuntu 24.04 / 26.04 で動きを確かめている）
- mise か uv。temt を入れる（[入れ方](#入れ方)）
- aws-cli v2。Session Manager 経由の ssh に使う
- session-manager-plugin。ssh は既定で Session Manager 経由でつなぐ
- ssh-keygen（openssh-client）

ssh の設定に書かれる ProxyCommand は `temt` を、`temt` は `aws` と
`session-manager-plugin` を、PATH から探す。ssh を打つシェルにも、ssh を呼ぶ
エディター（VS Code の Remote-SSH など）にも、これらが PATH にあるようにしておく。

AWS の資格情報は、EC2、IAM、SSM を操作できる権限と、ロールに付ける権限を確かめる
IAM Access Analyzer の `access-analyzer:ValidatePolicy` を持つプロファイルとして
`~/.aws/` に設定しておく。サービスのリージョンは環境変数では与えられない。設定
ファイルで指定する（[conf.md](conf.md)）。

## 入れ方

mise と uv のどちらで入れてもよい。

### mise

mise の `pypi` バックエンドで、GitHub の最新の Release を入れる。mise の python
（3.12 以上）と uv を使うので、mise に無ければ先に入れる。

    mise use -g python uv
    mise use -g pypi:izumo-m/temt

更新と、消し方は次のとおり。

    mise upgrade pypi:izumo-m/temt
    mise unuse -g pypi:izumo-m/temt

### uv

GitHub のリポジトリの `main` ブランチを入れる。

    uv tool install git+https://github.com/izumo-m/temt

更新は `uv tool upgrade temt` で、`main` の先頭を取り直す。消すには
`uv tool uninstall temt` を実行する。

### 消すとき

temt を消すと、temt のサービスに ssh でつながらなくなる。AWS リソースとサービスの
コマンドを残さないなら、消す前にサービスごとに `destroy` する
（[片づける](#片づける)）。AMI とキーペアは `destroy` でも残るので、要らなければ
これらも消す前に `ami prune` か `ami delete`、`keypair delete` で消す。

設定ファイルの場所 `~/.config/temt/`、ssh の設定と鍵の場所 `~/.ssh/temt/`、
`~/.ssh/config` の先頭に足された目印のコメントと `Include` の 2 行は残る。要らなければ
手で消す。

## 考え方

### サービス

*サービス* はひとつの用途のために立てる EC2 インスタンスの定義で、設定ファイル 1 つ
とインスタンス 1 台が対応する。同じ用途を設定違いで複数立てるときは、設定ファイルを
写して別のサービスにする（[同じ用途を設定違いで立てる](#同じ用途を設定違いで立てる)）。

複数のマシンから同じサービスを使うときは、同じ設定ファイルを各マシンに置く。ssh の
経路（[直接接続](#直接接続)）はマシンごとに違うので、設定ファイルではなく環境変数
`TEMT_LOGIN` で与える。AWS のプロファイルは設定ファイルに書けるが、マシンごとに
違うなら `--profile` で上書きする。

### 名前

サービスの名前は利用者が決める。サービス名は英小文字で始まり、英小文字・数字・
ハイフンが続き、英小文字か数字で終わる（`dev-main` など）。

以下、サービス名を `<svc>` と書く。ssh の Host 名や AWS のリソース名はすべて
`temt-<svc>` になる。

### ファイルの場所

手元に置くものは次のとおり。設定ファイル（自分のアプリを含む）の場所と、ssh の設定と
鍵の場所は、環境変数で変えられる
（[ファイルの場所を変える](#ファイルの場所を変える)）。

- 設定ファイル `~/.config/temt/<svc>.toml`（[conf.md](conf.md)）
- 自分のアプリ `~/.config/temt/apps/<アプリID>/`（[アプリを入れる](#アプリを入れる)）
- ssh の設定、鍵、known_hosts `~/.ssh/temt/`（[`~/.ssh/temt/` の中身](#sshtemt-の中身)）
- サービスのコマンド `~/.local/bin/temt-<svc>`（[サービスのコマンド](#サービスのコマンド)）

## コマンドの形

コマンドは `temt <対象> <動詞>` の形で、`temt instance start` なら `instance` が
対象、`start` が動詞である。一部のコマンドには 1 語の短縮形がある。
短縮形は、それが表すコマンドと同じ引数とコマンドラインオプションを取る
（`temt terminate --svc <svc> --yes` など）。以下の説明は、短縮形とサービスの
コマンド `temt-<svc>` で書く。サービスはコマンドラインオプション `--svc <svc>` で選ぶ。
下の表で `--svc <svc>` と書いたコマンドは `--svc` が要り、
`[--svc <svc>]` と書いたコマンドは省ける。省けば、すべてのサービスのものを扱う。
ただし `app list` は、省けばアプリ ID で指せるアプリだけを表示する。
`keypair` のコマンドは `--svc` を取らない。

| コマンド | 短縮形 | すること |
| --- | --- | --- |
| `temt service apply --svc <svc> [--yes]` | `apply` | サービスを用意する、設定を反映する |
| `temt service diff --svc <svc>` | `diff` | 次の `apply` が何を変えるかを見る |
| `temt service destroy --svc <svc> [--yes]` | `destroy` | サービスを片づける |
| `temt service list [--svc <svc>] [--format <列>]` | | 手元の設定ファイルのサービスの一覧を見る |
| `temt instance start --svc <svc> [--async]` | `start` | インスタンスを起動する。止めたものは再開する |
| `temt instance stop --svc <svc> [--async] [--yes]` | `stop` | インスタンスを終了せずに止める |
| `temt instance terminate --svc <svc> [<インスタンスID>...] [--async] [--yes]` | `terminate` | インスタンスを終了する。ID を指せば、それだけを終了する |
| `temt instance reboot --svc <svc> [--async] [--yes]` | `reboot` | インスタンスを終了せずに再起動する |
| `temt instance show --svc <svc>` | `status` | インスタンスの状態を見る |
| `temt instance list [--svc <svc>] [--format <列>]` | | インスタンスの一覧を見る |
| `temt ami bake --svc <svc> [--yes]` | `bake` | インスタンスを AMI にする |
| `temt ami list [--svc <svc>] [--format <列>]` | | AMI の一覧を見る |
| `temt ami prune [--svc <svc>] [--yes]` | | どのテンプレートも指していない AMI を消す |
| `temt ami delete [--svc <svc>] <AMI の ID または名前> [--force] [--yes]` | | 指した AMI を 1 つ消す |
| `temt template show --svc <svc>` | | 起動テンプレートを見る |
| `temt sg list --svc <svc> [--format <列>]` | | 設定ファイルの受信規則の一覧を見る |
| `temt sg add --svc <svc> <ポート> [<CIDR>] [--description <説明>]` | | 設定ファイルに受信規則を足す |
| `temt sg remove --svc <svc> <ポート> [<CIDR>]` | | 設定ファイルから受信規則を消す |
| `temt sg diff --svc <svc>` | | 次の `apply` が受信規則をどう変えるかを見る |
| `temt sg show --svc <svc>` | | セキュリティグループを見る |
| `temt keypair list [--format <列>]` | | temt が作ったキーペアの一覧を見る |
| `temt keypair show <キーペア名またはファイル>` | | キーペアか手元の鍵を見る |
| `temt keypair create <キーペア名>` | | 鍵を作り、キーペアとして登録する |
| `temt keypair delete <キーペア名> [--yes]` | | キーペアと手元の鍵を消す |
| `temt app list [--svc <svc>] [--format <列>]` | | 入っているアプリと、入れられるアプリの一覧を見る。`--svc` を省けば、入れられるアプリだけ |
| `temt app show --svc <svc> <アプリID>` | | アプリの記録、オプション、キーの一覧を見る |
| `temt app get --svc <svc> <アプリID> <キー>` | | アプリの値を 1 つ読む |
| `temt app set --svc <svc> <アプリID> <キー>=<値>` | | アプリの値を 1 つ書く |
| `temt app unset --svc <svc> <アプリID> <キー>` | | アプリの値を 1 つ消す |
| `temt app deploy --svc <svc> <アプリIDまたはパス> [<名前>=<値>...] [--reset] [--user <ユーザー>] [--bake] [--yes]` | `deploy` | アプリを入れる |
| `temt app remove --svc <svc> <アプリID> [--user <ユーザー>] [--bake] [--yes]` | | アプリを外す |
| `temt ssh proxy --svc <svc> [ポート]` | `proxy` | ssh の ProxyCommand として動く。手では打たない |

警告、エラー、確認の問いかけは標準エラー出力に、それ以外は標準出力に出る。ただし、表や
`diff` の変更だけを標準出力に残すよう、一覧の表の上の環境の行と、`diff` の知らせも
標準エラー出力に出る。

### サービスのコマンド

`apply` は、サービスごとにコマンド `~/.local/bin/temt-<svc>` を置く。中身は、PATH に
ある `temt` に `--svc <svc>` を付けて実行するシェルスクリプトで、`temt-<svc> ...` は
`temt ... --svc <svc>` と同じである（`temt-<svc> start` は `temt start --svc <svc>`
と同じ）。サービス名をシェルの補完で打てる。`~/.local/bin` は PATH に入れておく。

`--svc` を省けるコマンドも、このコマンドから打てば、そのサービスのものだけを扱う。
`--svc` を取らない `keypair` のコマンドは、このコマンドからは打てない。どのコマンドも、
`--svc` を重ねると引数の誤りになる。

`apply` は、`~/.local/bin/temt-<svc>` が無いときだけ書く。何かあれば、中身に
かかわらず触れない。`destroy` は、中身にかかわらず消す。

### 共通のコマンドラインオプション

コマンドラインオプションは、`temt` より後ろのどこに置いてもよい。
`temt --svc web start` も `temt start --svc web` も同じである。`-` で始まる語は
すべてコマンドラインオプションとして読むので、`-` で始まる値は、`app set` の
`<キー>=<値>` や `deploy` の `<名前>=<値>` の形か、値を取る
コマンドラインオプションの直後に置く。

`temt --help` で、すべてのコマンドの使い方が表示される。`temt instance --help` の
ように対象の後ろに置くと、その対象のコマンドだけの使い方が表示される。`temt app` の
ように引数を誤ったときも、対象が分かれば、その対象のコマンドだけの使い方が表示される。
`temt --version` で、temt のバージョンが表示される。

AWS のプロファイルを選ぶ `--profile <プロファイル>` は、AWS を呼ぶどの
コマンドにも付けられる。リージョンを選ぶ `--region <リージョン>` は、`--svc` を
付けないときだけ使える。`--svc` を付けたコマンドのリージョンは、設定ファイルで
決まる。AWS に問い合わせない `service list`、`sg list`、`sg add`、
`sg remove` と、`--svc` を付けない `app list` は、どちらも付けられない。

`--svc` を付けない `ami list`、`instance list`、`ami prune`、`ami delete` は
設定ファイルを読まないので、アカウントとリージョンを `--profile` と
`--region` で選ぶ。省けば、AWS の既定の規則（`AWS_PROFILE`、`AWS_REGION`、
プロファイルの `region` など）で決まる。どの環境を使ったかは、最初の行に出る。
`profile` は、`--profile` か `AWS_PROFILE`、`AWS_DEFAULT_PROFILE` で
選んだときだけ名前が出る。選んでいなければ `(none)` と出て、資格情報は
`[default]` プロファイルや環境変数の鍵などから来ている。

    temt --profile work --region us-east-1 ami prune

### 一覧の表

`list` のコマンドは、1 行目に列の名前（見出し）を、2 行目から 1 件を 1 行で
表示する。値の無いところは `-` と表示し、1 件も無ければ見出しだけを表示する。表は
標準出力に、環境の行は標準エラー出力に出るので、表だけを `grep` や `awk` に渡せる。

    temt instance list | awk 'NR > 1 && $3 == "running" { print $1 }'

`--format <列>[,<列>...]` を付けると、表示する列とその順を選べる。列は見出しの名前で、
大文字でも小文字でもよい。既定では表示しない列も、ここで選べる。知らない名前を
書くと、使える名前が表示される。時刻（`2026-09-20 12:00:00` の形の手元の時刻）と、
AMI の名前や `ImageId` など AWS や設定ファイルから来る値は空白を含むことがあるので、
`awk` で列を切り分けるなら、そういう列は最後に置く。

    temt ami list --format id,used,created

### 完了を待たない

`start`、`stop`、`reboot`、`terminate` に `--async` を付けると、AWS が要求（起動、
再開、停止、再起動、終了）を受け付けるまでしか待たない。要求した操作は AWS の側で
続き、AWS のコンソールと同じく、その終わりを待たずに次のことができる。

AWS の一覧（`describe-instances`）は、要求から数秒遅れて変わる。要求した直後は、
まだ前の状態が見えることがある。次の操作の前に、`status` で落ち着いた（`running`
や `stopped` になった、など）ことを確かめる。確かめずに状態を変えるコマンド
（`apply`、`bake`、`deploy` など）を打ったときの振る舞いは保証しない。

`start`、`stop`、`reboot`、`terminate` は、古い状態を見て取り違えても、やり直せば
揃うように作ってある。起動や停止の途中のインスタンスにも、temt は先回りして断らずに
要求を送る。AWS が断れば、その誤り（`IncorrectInstanceState` など）を表示し、
`status` を見てやり直すよう促して終わる。

`start --async` の直後に、もう一度 `start` すると、2 台目を起動することがある。
前の `start` が起動したものが、まだ AWS の一覧に見えないからである。そのときは、
[インスタンスが 2 台になったとき](#インスタンスが-2-台になったとき)のとおりに片づける。

## サービス

### 用意する

    temt apply --svc <svc>

`apply` は、作るもの、変えるもの、消すものを表示してから、`[y/N]` で確認を求める。
`y` と答えると、次がそろう。確認を省くには `--yes` を付ける。標準入力が端末でなく
`--yes` も無ければ、何もせずに止まる。違いが無ければ、確認を求めずに終わる。

- 設定ファイル `~/.config/temt/<svc>.toml`。無ければ既定値で書かれる
- キーペア `temt`。無ければ `~/.ssh/temt/keys/temt` に作られ、公開鍵が EC2 に
  登録される
- AWS リソース。IAM ロールとインスタンスプロファイル、セキュリティグループ、起動
  テンプレート、sshd の待ち受けポートを揃える State Manager の関連付け。ロールには、
  設定ファイルの `[[role.policy.<ポリシー名>]]` ごとにインラインポリシー
  `temt-<ポリシー名>` が付く。`[[role.policy.ec2]]` は、無ければ既定が設定ファイルに
  書かれる（[conf.md](conf.md#role)）
- ssh の設定。`~/.ssh/temt/config` に `Host temt-<svc>` のブロックが
  足され、`~/.ssh/config` の先頭に、目印のコメントと
  `Include ~/.ssh/temt/config` の 2 行が足される
- サービスのコマンド `temt-<svc>`。`~/.local/bin` に無ければ書かれる
  （[サービスのコマンド](#サービスのコマンド)）

途中で失敗したら、同じコマンドをもう一度実行すればよい。

### 設定を変える

設定ファイルを直して、もう一度 `apply` を実行する。起動テンプレート、
セキュリティグループの受信規則、ロールの権限、ssh の設定が追従する。動いている
インスタンスは変わらず、次の `start` から反映される。受信規則はすぐに、sshd の
待ち受けポートは数秒で、ロールの権限は IAM の反映を待てば、動いている
インスタンスにも効く。項目と既定、`apply` が書き足す規則、最初の `apply` の前に
項目を決めておく方法は [conf.md](conf.md) にある。

`apply` の前に、何が変わるかを `diff` で確かめられる。

    temt-<svc> diff

変更は 1 つにつき 1 行で表示される。頭の `+` は作る・足す、`~` は直す、`-` は消す
もので、続けて種類、名前、違いが並ぶ。ロールのポリシーの中身、関連付けのスクリプト、
ssh の Host ブロックは、違う行が続けて字下げして表示される。違いが無ければ、標準出力
には何も表示されない。`apply` も、確認を求める前に同じ形で表示する。

### 同じ用途を設定違いで立てる

設定ファイルを別の名前で写し、直してから `apply` を実行する。写したファイルは別の
サービスで、AWS リソースもインスタンスも別に持つ。`ImageId` も写るので、同じ AMI
から起動する。`ImageId` は `apply` が書くので、写す前に元のサービスで一度 `apply`
を実行しておく。

    cp ~/.config/temt/dev-main.toml ~/.config/temt/dev-sub.toml
    temt apply --svc dev-sub

写した先で `ImageId` の行を消すと、元のサービスの AMI ではなく、写した先の
サービスから焼いた AMI（無ければ Ubuntu 24.04）から選び直す。`template show` の
`latest` も写した先のサービスから焼いた AMI だけを見るので、元のサービスが新しい
AMI を焼いても表示されない。元のサービスの新しい AMI を使うときは、その名前を
`ImageId` に書いて `apply` を実行する。

### サービスの一覧を見る

    temt service list

手元の設定ファイル（`~/.config/temt/*.toml`）を 1 つ 1 サービスとして、一覧を
表示する。設定ファイルを読むだけで、AWS には問い合わせない。`--profile` と
`--region` は付けられない。`temt-<svc> service list`（`temt service list --svc
<svc>` と同じ）は、そのサービスの行だけを表示する。

    SERVICE    PROFILE   REGION           IMAGE                   TYPE
    dev-main   -         ap-northeast-1   ami-0123456789abcdef0   t3.micro
    web        work      us-east-1        temt-web-2609201200     t3.small

`-` は、その項目に値が無いことを表す。`apply` する前の `ImageId` などである。
既定のある項目は、書いていなくても既定を表示する。`--format` で `KEYNAME`、
`IDENTITYFILE`、`SUBNET`、`INGRESS`（受信規則の数）、`SSHDPORTS`、`VOLUMESIZE` も
表示できる（[一覧の表](#一覧の表)）。

    temt service list --format service,keyname,identityfile

読めない設定ファイルと、名前がサービス名の規則に合わないファイル
（`Web.toml` など）は、警告して飛ばし、残りを表示してから終了コード 1 で終わる。

### 片づける

    temt-<svc> destroy

そのサービスのために `apply` が作った AWS リソースと、サービスのコマンドを消す。
消す前に、対象を表示して `[y/N]` で確認を求める。`--yes` を付けると確認しない。標準
入力が端末でないときは `--yes` が要る。

インスタンスが動いていれば、まず `terminate` と同じように終了し、`terminated` に
なるまで待ってから消す。`destroy` はつねに完了を待つので、`--async` は無い。

消えるものは次のとおり。

- 起動テンプレート、sshd の待ち受けポートを揃える関連付け、`/temt-<svc>` の
  階層の下にある SSM パラメータ、セキュリティグループ、
  インスタンスプロファイル、IAM ロール。この順に消す。ロールに付いた
  インラインポリシーは、手で付けたものも含めてすべて消える。確認の一覧に名前が
  出る。手で付けた管理ポリシーがあれば、ロールを消さずに止まる
- known_hosts のディレクトリ `~/.ssh/temt/known_hosts.d/temt-<svc>/`
- サービスのコマンド `~/.local/bin/temt-<svc>`。中身にかかわらず消す

セキュリティグループは、同じ名前のものを VPC を問わずすべて消す。設定ファイルの
`SubnetId` を別の VPC に変えたあとに残った古いセキュリティグループも、ここで
消える。

残るものは次のとおり。もう一度 `apply` を実行すれば、同じ設定で作り直せる。

- 設定ファイル `~/.config/temt/<svc>.toml`
- 手元の鍵 `~/.ssh/temt/keys/<KeyName>` と、EC2 のキーペア `<KeyName>`
- `~/.ssh/temt/config` の `Host temt-<svc>` ブロック
- そのサービスの AMI

サービスのコマンドは消えているので、次の `apply` は `temt apply --svc <svc>` の
形で打つ。

途中で失敗したら、同じコマンドをもう一度実行すればよい。既に消えたものは飛ばして、
続きから進む。

## インスタンス

### 起動する

    temt-<svc> start

`start` は、インスタンスが動き出し、SSM エージェントが応答するまで待つ。ふつう
1〜2 分かかる。段階が進むたびに 1 行ずつ表示する。既に動いていれば、2 台目を起動せず、
ログインできることを確かめる。
起動したインスタンスには、`ssh temt-<svc>` でログインする（[つなぐ](#つなぐ)）。

`--async` を付けると、起動を要求し、AWS が受け付けるまでしか待たない
（[完了を待たない](#完了を待たない)）。続けて `ssh` すれば、つなげるようになるまで
待つ。待つあいだは、そのことを 1 行ずつ表示する。ただし、要求した直後の数秒は、起動を
求めたインスタンスが AWS の一覧にまだ見えないことがある。そのとき `ssh` は、
インスタンスが無い、または止まっていると表示して終わる。`start` し直さず（2 台目を
起動しうる）、`status` で見えたことを確かめてからつなぎ直す。また、ssh の
`ConnectTimeout` を設定していると（VS Code の Remote-SSH などは自分で付ける）、
その秒数で待ち合わせが切れて ssh が失敗する。そのときは `--async` を付けずに
`start` するか、しばらくしてからつなぐ。

`apply` していないサービス（`destroy` したものを含む）では、`start` は起動
テンプレートが無いと表示して、先に `apply` するよう促す。

止まっている（`stopped`）か停止の途中（`stopping`）のインスタンスがあれば、`start`
は新しく起動せず、その再開を求める（[止めて再開する](#止めて再開する)）。停止の途中
では AWS が断ることがあり、そのときは `status` を見てやり直す。終了の途中
（`shutting-down`）のものは無いものとして扱い、新しく起動する。

何かの手違いで同じ名前のインスタンスが 2 台以上あるときは、`start` は起動せず、
それらを表示して止まる
（[インスタンスが 2 台になったとき](#インスタンスが-2-台になったとき)）。
同じサービスで `start`、`stop`、`terminate`、`reboot`、`bake`、`destroy`、
`deploy`、`app remove` のどれかが既に動いていれば、その終わりを待たず、動いて
いると表示して終わる。

### 状態を見る

    temt-<svc> status

サービスの今の状態を表示する。何も変えない。`status` は `instance show` の短縮形で、
`temt-<svc> instance show` と書いても同じである。

    template:   temt-<svc>-template
    instance:   i-0123456789abcdef0 (running)
    ipv4:       203.0.113.10
    ssm-agent:  Online
    sshd-ports: applied
    apps:       unbaked timezone-tokyo
    app status:
      idle-shutdown  power off in 42m (at 2026-09-29 06:12:03 UTC; idle 18m since pam, limit 1h)  [3m ago]

- `template` は、起動テンプレートの名前。無ければ `none` と、`apply` されていない
  ことが表示される。`destroy` した後もこうなる。
- `instance` は、インスタンスの ID と状態。無ければ `none`。2 台以上あれば全部を
  表示する。終了の途中のものを除いて 2 台以上あれば、余分なものを終了するよう警告する
  （[インスタンスが 2 台になったとき](#インスタンスが-2-台になったとき)）。
- `ipv4` は、そのインスタンスのパブリック IPv4 アドレス。直接接続
  （`TEMT_LOGIN=direct`）でつなぐ先である。付いていなければ `(none)`。
- `ssm-agent` は、そのインスタンスの SSM エージェントの状態。`Online` なら
  Session Manager 経由でログインできる。起動してからエージェントが応答するまでは
  `(not registered)` で、`start` はこれが `Online` になるまで待つ。
- `sshd-ports` は、sshd の待ち受けポートを設定ファイルの `SshdPorts` に揃える
  関連付け `temt-<svc>-sshd`（[sshd のポートを
  変える](#sshd-のポートを変える)）が、そのインスタンスで済んだか。見るのは
  最後に `apply` した内容で、設定ファイルを直して `apply` していなければ、前の
  内容についての表示である。`applied` なら済んでいる。`pending` は
  起動直後などでまだ済んでいない。`failed` は、sshd の設定を確かめて
  通らなかったか、新しいポートで待ち受けられずに、前のポートのまま戻したことを
  表す。`(not run)` はまだ実行が始まっていない。`none` は関連付けそのものが
  AWS に無く、`apply` が要る。ほかに AWS の状態がそのまま小文字で
  出ることがある。
- `apps` は、入れたり外したりして、まだ焼いていないアプリ。`unbaked` はインスタンスに
  だけあり、焼く前にインスタンスが終了すると消える。`lost` は、焼く前にインスタンスが
  終了して消えたか、`apply` で別の AMI に替えたもので、もう一度 `deploy` する。
  `removed` は、`app remove` でインスタンスからは外したが、AMI には残っているもので、
  焼けば消える（`removed swapfile (still in the AMI; bake to drop it)`）。どれも
  無ければ `all baked`、焼いたアプリも無ければ（焼いたかどうかを記録していない
  記録は数えない）`none`。決め方は `app list` の `STATE`
  （[アプリを入れる](#アプリを入れる)）と同じである。起動テンプレートが無ければ
  出ない。
- `app status` は、アプリが自分で知らせている状態（[アプリの値](#アプリの値)）。
  アプリごとに 1 行で、値と、最後に書かれてからの経過時間を表示する。インスタンスが
  終了した後も最後の値が残るので、`instance` が `none` でも出る。今動いている
  （`pending` か `running` の）インスタンスが書いたとみなせない値は、経過時間に
  `stale` を添えて `[4m ago, stale]` のように表示する。動いているインスタンスが無いとき
  と、インスタンスが起動する（停止したものは再開する）より前に書かれたときである。
  インスタンスの中から `shutdown` したときなどは、最後に書いた値が書き換わらずに
  残るので、`stale` で見分ける。1 つも無ければ `none`。`idle-shutdown` を入れて
  いれば、電源を切る予定がここに出る（[自動終了](#自動終了)）。

### インスタンスの一覧を見る

    temt-<svc> instance list
    temt instance list

temt のインスタンス（`temt:service` タグを持ち、`terminated` でないもの）の一覧を
表示する。何も変えない。`temt-<svc> instance list`（`temt instance list --svc
<svc>` と同じ）はそのサービスのものを、`temt instance list` はすべてのサービスの
ものを表示する。設定ファイルの無いサービスのものも出るので、消し忘れに気づける。

    account 123456789012, region ap-northeast-1, profile (none)
    ID                    SERVICE    STATE     TYPE       LAUNCHED
    i-0123456789abcdef0   dev-main   running   t3.micro   2026-09-26 09:12:34
    i-0fedcba9876543210   web        stopped   t3.small   2026-09-20 12:00:00

`--format` で、起動に使った AMI の ID（`IMAGE`）とパブリック IPv4 アドレス（`IP`）
も表示できる。SSM エージェントの状態は表示しないので、ログインできるかは `status` で
見る。`--svc` を付けないときの環境の選び方は、[共通のコマンドラインオプション](#共通のコマンドラインオプション)にある。

### 再起動する

    temt-<svc> reboot

インスタンスを終了せずに再起動する。ディスクの中身もインスタンス ID もそのまま
なので、ssh のホスト鍵の確認もやり直さない。対象のインスタンスの ID と状態を表示して
`[y/N]` で確認を求める。`--yes` を付けると確認しない。

再起動して、またログインできるようになるまで待つ。ふつう 1 分ほどかかり、SSM
エージェントの戻りを待っていること、戻ったことを 1 行ずつ表示する。見分けには、
インスタンスの中で起動のたびに変わる ID（boot_id）を Run Command で読んで比べるので、
手元の資格情報に `ssm:SendCommand` と `ssm:GetCommandInvocation` の権限が要る。
SSM エージェントが応答していない（`status` の `ssm-agent` が `Online` でない）と
見分けられないので、再起動せずに終わる。

`--async` を付けると、AWS が再起動の要求を受け付けるまでしか待たない。boot_id は
読まないので、SSM エージェントが応答していなくても再起動できる。すぐに `ssh` すると、
再起動の途中でつながらずに失敗することがある。そのときは少し待ってつなぎ直す。

インスタンスが無いか、止まっている（`stopped`）ときは、`start` するよう促して
終わる。終了の途中（`shutting-down`）のものは無いものとして扱う。起動や停止の
途中（`pending`、`stopping`）でも、そのまま再起動を求める。AWS が断れば、その誤りを
表示し、`status` を見てやり直すよう促して終わる。`--async` を付けなければ先に SSM
エージェントを確かめ、boot_id を読むので、起動や停止の途中ではふつう再起動せずに、
`--async` を試すよう促して終わる。

インスタンスの中で `sudo reboot` しても同じように再起動する。一方、`sudo poweroff`
や `sudo shutdown -h` で電源を切ると、インスタンスは止まるのではなく終了する。止めて
続きから使いたいときは `stop` を使う（次の節）。

再起動すると、インスタンスの上で `idle-shutdown limit` コマンドで変えた時間は元に
戻る（[自動終了](apps/idle-shutdown.md)）。sshd の待ち受けポートを変えたことを知らせる
ログインのメッセージも、焼くまでは出るはずのものが出なくなる。

### 止めて再開する

    temt-<svc> stop
    temt-<svc> start

`stop` は、インスタンスを終了せずに止める。ディスクの中身、インスタンス ID、ssh の
ホスト鍵はそのまま残り、次の `start` が新しく起動する代わりにそれを再開する。作業を
中断して、続きから使いたいときに使う。ふだんは、使い終わったら `terminate` する
（[終了する](#終了する)）。

`stop` は、対象のインスタンスの ID と状態を表示して `[y/N]` で確認を求め、止まる
（`stopped`）まで待つ。ログイン中のセッションや、保存していない作業は失われる。
`--yes` を付けると確認しない。`--async` を付けると、AWS が停止の要求を受け付ける
までしか待たない。インスタンスが無いときと、既に止まっているときは、そう表示して終わる。
終了の途中（`shutting-down`）のものは無いものとして扱う。停止の途中（`stopping`）
なら、停止を求めずに止まるのを待つ。起動の途中（`pending`）でも、そのまま停止を
求める。

再開した `start` も、ログインできるようになるまで待つ。`--async` を付けると、AWS が
再開の要求を受け付けるまでしか待たない。止まった直後など、AWS が再開や停止を断ることが
ある。そのときは、AWS の誤りを表示し、`status` を見てやり直すよう促して終わる。止めて
いるあいだについて:

- インスタンスの料金はかからないが、ディスク（EBS）の料金はかかり続ける。
- 起動テンプレートは使わずに再開するので、止めている間に設定ファイルを変えて
  `apply` しても、AMI やインスタンスタイプなどの変更は、そのインスタンスには効かない。
  効かせるには、`terminate` してから `start` する。受信規則とロールの権限は効く
  （`SubnetId` を別の VPC のものに変えたときを除く。そのときは古い VPC のまま
  再開する）。`SshdPorts` を変えたときは、再開すると揃う（再開した直後の `ssh` は、
  揃うのを待ってからつながる）。
- パブリック IPv4 アドレスは、再開すると変わる。
- 再起動と同じく、インスタンスの上で `idle-shutdown limit` コマンドで変えた時間は
  元に戻り、sshd の待ち受けポートを変えたことを知らせるログインのメッセージも出なく
  なる。
- 止まっているインスタンスも、そのまま `bake` で焼ける（[AMI にする](#ami-にする)）。

`idle-shutdown` が電源を切ったときは、止まるのではなく終了する
（[自動終了](apps/idle-shutdown.md)）。再開したまま使わずにおくと、制限時間（既定は
1 時間）で終了し、続きから使うために残した中身も消える。

### 終了する

    temt-<svc> terminate

対象のインスタンスの ID と状態を表示して `[y/N]` で確認を求める。`y` と答えると、
インスタンスをボリュームごと終了し、known_hosts のファイルを消す。インスタンスに
残したものは消える。次に `start` すると、起動テンプレートの AMI から新しい
インスタンスが立ち上がる。残したいものがあれば、終了する代わりに `bake` で AMI に
する（[AMI にする](#ami-にする)）。入れてまだ焼いていないアプリがあれば、終了すると
消えることを確認の前に警告する（`unbaked apps will be lost: timezone-tokyo`）。外して
まだ焼いていないアプリがあれば、次のインスタンスに AMI から戻ることを警告する
（`apps removed without baking come back from the AMI: swapfile`）。

`--yes` を付けると確認しない。標準入力が端末でないとき（スクリプトの中など）は、
確認できないので `--yes` が要る。

終了が完了するまで待つ。`--async` を付けると、AWS が終了の要求を受け付けるまでしか
待たない。どちらでも、known_hosts は片づける。インスタンスが
1 台も無くても、エラーにはならない。何かの手違いで同じ名前のインスタンスが 2 台
あるときは、両方とも終了する（1 台だけ残すときは
[インスタンスが 2 台になったとき](#インスタンスが-2-台になったとき)）。

インスタンス ID を指すと、そのインスタンスだけを終了する。確認、known_hosts の
片づけ、焼いていないアプリの警告、完了の待ち合わせも、指したものだけに行う。指した
ID がそのサービスのインスタンスに無ければ、見つかったものを表示し、何もせずに終わる。

    temt-<svc> terminate i-0fedcba9876543210

temt と一緒に配るアプリ `idle-shutdown` を入れて焼いておけば、インスタンスは
使われなくなってから制限時間（既定は 1 時間）で自分で終了する
（[自動終了](apps/idle-shutdown.md)）。
焼く前に自分で終了したときは警告が出ないので、入れたアプリを残したいときは、
離れる前に `status` の `apps` を見て焼いておく。

作った AWS リソース（IAM ロール、インスタンスプロファイル、セキュリティグループ、
起動テンプレート、関連付け）はそのまま残る。これらには費用はかからない。
消すには `destroy` を使う（[片づける](#片づける)）。キーペアは `destroy` でも残る。

### インスタンスが 2 台になったとき

同じサービスのインスタンスが 2 台以上あると、`status` はすべてを表示し、余分なものを
終了するよう警告する。2 台以上かどうかは、すべてのコマンドで、終了の途中
（`shutting-down`）のものを除いて数える。終了の途中のものしか無ければ、無いものと
して扱う。

    temt: warning: 2 instances for temt-<svc>; terminate the extra one with 'temt terminate --svc <svc> <instance-id>'

`start`、`stop`、`reboot`、`bake`、`deploy`、`ssh` などは、どれを扱えばよいか
決められないので、それらを表示して止まる。残さないほうのインスタンス ID を指して
`terminate` し、止まったコマンドをやり直す。

    temt-<svc> terminate i-0fedcba9876543210

ID を省いて `terminate` すると、すべてを終了する。

### ディスクを広げる

インスタンスのルートボリュームの大きさは、設定ファイルの `VolumeSize`（GiB）で
決める。書かなければ AMI と同じ大きさで、素の Ubuntu なら 8 GiB である。
`[launch-template]` に書いて `apply` する。

    [launch-template]
    VolumeSize = 30

    temt-<svc> apply

広がるのは、次の `start` で起動テンプレートから起動したインスタンスである。Ubuntu
の AMI（とそこから焼いた AMI）では、起動するとインスタンスの中の cloud-init が
パーティションとファイルシステムを広げるので、`ssh` して手を動かす必要はない。`df -h /` で確かめられる。

動いているインスタンスと止めてあるインスタンスは、`apply` しても広がらない。
中身を残すなら、焼いてから起動する。AMI ができるまで（ディスクの大きさによって
数分からそれ以上）は使えない。

    temt-<svc> bake
    temt-<svc> start

中身を残さなくてよければ、`terminate` してから `start` する。

EBS は縮められない。大きくして焼いた AMI は、そのあと `VolumeSize` の行を消しても、
焼いたときの大きさのまま起動する。AMI のルートボリュームより小さい `VolumeSize` は
書けず、`apply` が止まる。AMI の大きさは `template show` の `root volume` で
分かる。大きくして起動したインスタンスを、その大きさより小さい `VolumeSize` に
直してから焼こうとすると、`bake` は焼かずに止まる。焼くと、次の `start` を AWS が
断る AMI ができるからである。`VolumeSize` をインスタンスの大きさ以上に戻すか、
行を消すかし、どちらも `apply` してから焼く。

## AMI

### AMI にする

    temt-<svc> bake

インスタンスのディスクの中身を AMI にし、次の `start` からその AMI で起動する
ようにする。ssh でパッケージを入れるなどしてインスタンスを整えてから打つ。止まって
いる（`stopped`）インスタンスも、そのまま焼ける。

対象のインスタンスの ID と状態を表示し、何をするかを示して `[y/N]` で確認を求める。
`y` と答えると、次を順に行う。

1. インスタンスが動いていれば停止する。ログイン中のセッションや、保存していない
   作業は失われる。
2. 止まったインスタンスから AMI を作る。名前は `temt-<svc>-<YYMMDDHHMM>`（時刻は
   手元の時刻）で、`temt:service=<svc>` のタグが付く。インスタンスの中には手を
   加えない。
3. AMI が使える状態（`available`）になるまで待つ。ディスクの大きさによって、数分から
   それ以上かかる。締め切りは無い。
4. 設定ファイルの `ImageId` を新しい AMI の名前に書き換え、起動テンプレートの
   `ImageId` も替える。元の値と新しい名前が表示される。`deploy` の記録も新しい
   AMI に向け、入れてまだ焼いていなかったアプリを焼いたと記録する（`app list` の
   `STATE` が `baked` になる）。
5. インスタンスを `terminate` と同じように終了し、終了が完了するまで待つ。

次に `start` すると、新しい AMI からインスタンスが立ち上がる。

`--yes` を付けると確認しない。標準入力が端末でないときは `--yes` が要る。`bake` は
つねに完了を待つので、`--async` は無い。

設定ファイルを直して `apply` をまだ実行していないときは、焼かずに、先に
`apply` を実行するよう促して終わる。確かめるのは起動テンプレートの項目だけで、
`SshdPorts` は確かめない。`SshdPorts` を変えたら、`apply` して `status` の
`sshd-ports` が `applied` になってから焼く
（[sshd のポートを変える](#sshd-のポートを変える)）。インスタンスが無いとき、停止や
終了の途中（`stopping`、`shutting-down`）や起動の途中（`pending`）のとき、2 台以上
あるとき、インスタンスのルートボリュームが `VolumeSize` より大きいとき
（[ディスクを広げる](#ディスクを広げる)）も、焼かずにその旨を
表示して終わる。

書き換えるのは、焼いたサービスの設定ファイルと起動テンプレートだけである。設定
ファイルを写した別のサービスが同じ AMI を使っていても、そちらは変わらない。新しい
AMI を使うなら、そのサービスの `ImageId` を書き換えて `apply` を実行する。複数の
マシンで同じサービスを使うときは、他のマシンの設定ファイルの `ImageId` も書き換える。
古い AMI は消えずに残る。消すには `ami prune` か `ami delete` を使う（次の節）。

AMI が作れなかったときは、AWS が返した理由を表示して終わる。設定ファイル、起動
テンプレート、`deploy` の記録は何も変わっていない。インスタンスは止まったまま残る
ので、`start` で再開して使い続けられる。作れない原因には、AWS の一時的な誤りの
ほか、暗号化した EBS の KMS の鍵が使えないなど、自分で直すものもある。原因を
直してから、もう一度 `bake` を実行する。同じサービスを同じ
分のうちに 2 回焼くと、AMI の名前が重なって失敗する。1 分おいてやり直す。

途中で止まったとき（Ctrl-C、締め切り、AWS のエラー）は、どこまで進んだかで
直し方が変わる。

- AMI ができる前: インスタンスが止まっていることがある。もう一度 `bake` を実行
  する。AMI を作る最中に止めたときは、AMI ができていることがある。
- AMI を作った後、`available` になるのを待つ間: 何も変わっておらず、インスタンスは
  止まったまま残る。作った AMI の ID と名前が表示されている。`ami list` で AMI が
  `available` になったのを確かめたら、もう一度 `bake` を実行する。AMI がもう 1 つ
  できるが、余分な方は `ami prune` が消す。または、その名前を `ImageId` に書いて
  `apply` を実行し、`terminate` を実行する。こちらは、入れたアプリを焼いたと記録
  しないので、`app list` で `lost` と出る。`deploy` し直して焼けば揃う。`failed` に
  なっていれば、作れなかったときと同じである。
- 設定ファイルを書き換えた後: `apply` を実行してから、もう一度 `bake` を実行する
  （`deploy` の記録も揃う）か、`terminate` を実行する（`app list` で `lost` と出る。
  `deploy` し直して焼けば揃う）。
- インスタンスの終了: `terminate` を実行する。

起動テンプレートが新しい AMI を指す前に止まったときは、どのテンプレートからも
指されていないので、`ami prune` を打つと新しい AMI は消える。設定ファイルの
`ImageId` がその AMI を指すとき（設定ファイルを書き換えた後に止まったときと、
その名前を自分で書いたとき）は、`ami prune` より先に `apply` を実行する。設定
ファイルを書き換える前に止まり、もう一度 `bake` するなら、消えても困らない。

### AMI を見る、片づける

    temt-<svc> ami list
    temt ami list
    temt-<svc> ami prune
    temt ami prune
    temt ami delete <AMI の ID または名前>

`ami list` は temt の AMI（`temt:service` タグを持つ AMI）の一覧を表示する。何も
変えない。`temt-<svc> ami list`（`temt ami list --svc <svc>` と同じ）はそのサービスの
AMI を、`temt ami list` はすべてのサービスの AMI を表示する。
`--svc` を付けないときの環境の選び方は、[共通のコマンドラインオプション](#共通のコマンドラインオプション)にある。

    account 123456789012, region ap-northeast-1, profile (none)
    ID                      NAME                    STATE       CREATED               USED
    ami-0123456789abcdef0   temt-<svc>-2609011200   available   2026-09-01 12:00:00   -
    ami-0fedcba9876543210   temt-<svc>-2609201200   available   2026-09-20 12:00:00   temt-<svc>-template:3

`USED` は、その AMI を指している起動テンプレートのバージョン
（`<テンプレート名>:<バージョン>`、複数あれば `,` でつなぐ）で、無ければ `-` と
出る。`--format` で、`temt:service` の値（`SERVICE`）も表示できる。`ami prune` は、
`USED` が `-` の AMI のうち `bake` が作ったもの（状態が `available` か `failed`）の
登録を解除し、スナップショットも消す。消すものだけを表示し、`[y/N]` で確認を
求める。`--yes` を付けると確認しない。同じマシンで他のコマンド（`bake` など）が
そのサービスを扱っていたときと、消す直前に起動テンプレートがその AMI を指して
いたときは、その AMI を消さずに残りを続け、最後に残したものをまとめて表示して、
エラーで終わる。

`ami delete` は、ID か名前で指した temt の AMI 1 つの登録を解除し、スナップショットも
消す。探すのは自分のアカウントの AMI だけである。`ami prune` と違い、`bake` が
作った名前でなくても、`temt:service` タグがあれば消せる。起動テンプレートの
バージョンが指している AMI と、`pending`（`bake` の途中）の AMI は、`--force` を
付けないと消さずにエラーにする。`resolve:ssm:` のバージョンを解決できないときも、
そのバージョンが指しているかもしれないので、同じく `--force` が要る。`--force` で
テンプレートが指す AMI を消すと、そのバージョンからは起動できなくなる。消す前に
`[y/N]` で確認を求め、`--yes` を付けると確認しない。

`temt-<svc> ami prune`（`temt ami prune --svc <svc>` と同じ）は、そのサービスの
AMI だけを消す候補にする。指されているかは、他のサービスのテンプレートも見て
確かめる。`--svc` を付けない `ami prune` は、どのサービスの AMI かを問わない。
`destroy` したサービスの AMI は、テンプレートが消えているので、他の
テンプレートが指していない限り消える。`destroy` したサービスの AMI を、次の
`apply` のために残しておきたいときは、片づけるサービスごとに `--svc` を付ける。
`ami delete` も `--svc` を付ければ、そのサービスの AMI でないものは
見つからないとして消さない。`ami prune` も `ami delete` も、設定ファイルの
`ImageId` は見ない。テンプレートが指していない AMI を `ImageId` に書いていれば、
消えた後の `apply` は、行を消して選び直すよう促して止まる。他のマシンで使っている
サービスでも、テンプレートは AWS にあるので、その AMI は消えない。

起動テンプレートの `ImageId` に SSM パラメータ（`resolve:ssm:`）を書いたものは、
AWS がパラメータから引いた AMI を指すものとして扱う。パラメータが無いなどで
引けなければ、`ami list` はそのバージョンを表示して警告し、`ami prune` は何も
消さずに止まる。`ami delete` も、`--force` が無ければ止まる。そのバージョンを
直すか消してから、もう一度実行する。

## 起動テンプレート

    temt-<svc> template show

`start` がインスタンスを起こすのに使う起動テンプレートを表示する。何も変えない。
次の `apply` が何を変えるかは、`diff` で分かる（[設定を変える](#設定を変える)）。

    launch template temt-<svc>-template:
      version:                3 (created 2026-09-20 18:00:00)
      ImageId:                ami-0123456789abcdef0
      InstanceType:           t3.micro
      KeyName:                temt
      VolumeSize:             (not set)
      SubnetId:               subnet-0123456789abcdef0 (vpc-0123456789abcdef0)
      security groups:        sg-0123456789abcdef0
      instance profile:       temt-<svc>
      shutdown behavior:      terminate
      cpu credits:            unlimited
      public ipv4:            true
      ipv6 addresses:         1
      http tokens:            required
      metadata tags:          enabled
      instance tags:          Name=temt-<svc> temt:service=<svc>
      network-interface tags: Name=temt-<svc> temt:service=<svc>
      volume tags:            Name=temt-<svc> temt:service=<svc>
    image ami-0123456789abcdef0:
      name:        temt-<svc>-2609201800
      created:     2026-09-20 18:00:00
      state:       available
      root volume: 8 GiB
      latest:      temt-<svc>-2609211800 (ami-0fedcba9876543210, temt:service=<svc>) created 2026-09-21 18:00:00, newer
    versions (* is the default):
      3 * 2026-09-20 18:00:00 ami-0123456789abcdef0 t3.micro subnet-0123456789abcdef0

- 1 つめは、既定バージョンの内容。temt が管理する項目だけを表示する。`SubnetId` には
  そのサブネットが属する VPC を添える。`VolumeSize` の `(not set)` は、AMI の
  ルートボリュームと同じ大きさで起動することを表す。
- 2 つめは、`ImageId` が指す AMI。`root volume` はそのルートボリュームの大きさで、
  `VolumeSize` はこれより小さくできない。EBS のルートボリュームが無い AMI では
  `(no EBS root volume)` と出て、`VolumeSize` は書けない。登録解除されていれば
  その旨を表示する。
  `latest` はこのサービスの `temt:service=<svc>` のタグを持つ最新の AMI で、
  テンプレートのものより新しければ `newer` と添える。他のサービスの AMI を指していても、
  `latest` はこのサービスから焼いた AMI から選ぶ。新しいものに替えるには、設定
  ファイルの `ImageId` を書き替えるか、行を消して `apply` を実行する。
- 3 つめは、バージョンの一覧。`*` が既定バージョンである。`apply` は既定以外の
  バージョンを消すので、ふつうは 1 行だけになる。

## セキュリティグループ

サービスのセキュリティグループの受信規則は、設定ファイルに書く。`sg` のコマンドで、
設定ファイルの規則を足し引きし、AWS との違いを確かめられる。

    temt-<svc> sg add 22 --description home

CIDR を省くと、temt がグローバル IP アドレスを調べ、IPv4 なら `/32`、IPv6 なら
`/64` の規則にする。問い合わせ先は、IPv4 が `https://checkip.amazonaws.com`、
IPv6 が `https://api6.ipify.org` である。IPv6 のアドレスは、手元に IPv6 の
経路があるときだけ取れる。どちらも使った CIDR が表示される。VPN を通していれば、
VPN の出口のアドレスで規則ができる。
[直接接続](#直接接続)はインスタンスのパブリック IPv4 アドレスへつなぐので、
ssh に効くのは IPv4 の規則である。CIDR を自分で決めるなら、`sg add 22 198.51.100.0/24` のように書く。
ポートは `8000-8010` のように範囲でも書け、`53/udp` と付ければ udp、`icmp` と
書けば ICMP（ping など）になる。

`sg add` が書き換えるのは設定ファイルだけである。次の `apply` が何を変えるかを
確かめてから、`apply` で AWS に反映する。

    temt-<svc> sg diff
    temt-<svc> apply

`apply` を実行すると、セキュリティグループの受信規則が設定ファイルの内容になる。
動いているインスタンスにもすぐ効く。手で足した規則は消えるので、許したいものは
すべて設定ファイルに書く。設定ファイルの規則は `sg list` で見られ、手で直しても
よい。書き方は [conf.md](conf.md#security-group) にある。

    temt-<svc> sg list

`sg remove` は、ポートと CIDR で指した規則を設定ファイルから消す。`sg remove 22` の
ように CIDR を省けば、今のグローバル IP アドレスの規則を消す。`sg add` と同じく、
`apply` で AWS に反映する。

    temt-<svc> sg remove 22 203.0.113.10/32

次を実行すると、AWS にあるセキュリティグループの ID、VPC、説明、タグ、受信規則の
数が表示される。

    temt-<svc> sg show

`SubnetId` を別の VPC に変えたあとに残った、同じ名前のセキュリティグループも
挙がる。これらは `destroy` で消える。

### 直接接続

Session Manager を通さず、インスタンスのパブリック IP アドレスへ直接つなぐと速い。
代わりに、セキュリティグループの受信許可と、sshd のポート（`SshdPorts`。既定は 22）で
外に出られるネットワークが要る。使えない環境でも自動では切り替わらない。

まず、自分のグローバル IP アドレスから sshd のポートへの受信許可を、設定ファイルに足して
`apply` する（[セキュリティグループ](#セキュリティグループ)）。`SshdPorts` でポートを
変えているなら（[sshd のポートを変える](#sshd-のポートを変える)）、22 の代わりに
そのポートを、複数ならポートごとに足す。

    temt-<svc> sg add 22 --description home
    temt-<svc> apply

次に、環境変数 `TEMT_LOGIN` を `direct` にする。ssh の設定は変わらず、次の ssh
から直接つなぐ。ssh は ProxyCommand に自分の環境変数を渡すので、シェルの初期化で
`export TEMT_LOGIN=direct` としておくことも、`TEMT_LOGIN=direct ssh
temt-<svc>` のようにその接続だけ変えることもできる。どちらの経路が使える
かは手元のネットワーク（ファイアウォール、VPN）で変わるので、設定ファイルには
書かない。

グローバル IP アドレスは変わることがある。つながらなくなったら、`sg list` で古い規則の
CIDR を確かめて消し、新しいアドレスで足し直してから `apply` を実行する。

    temt-<svc> sg remove 22 203.0.113.10/32
    temt-<svc> sg add 22 --description home
    temt-<svc> apply

Session Manager 経由に戻すには、`TEMT_LOGIN` を消すか `ssm` にする。受信規則は
残るので、消したければ `sg remove` で設定ファイルから外して `apply` を実行する。

## キーペア

`apply` は、EC2 にキーペア `KeyName` があることと、手元の秘密鍵（設定ファイルの
`IdentityFile`。既定は `~/.ssh/temt/keys/<KeyName>`）がそれと一致することを
確かめる。EC2 に無ければ手元の鍵の公開鍵を登録して `temt:keypair` タグを付け、
手元にも無ければ鍵を作ってから登録する。初回は `~/.ssh/temt/keys/temt` と
`temt.pub` ができる。

手元の鍵と EC2 のキーペアが一致しないとき（手元に無いときも含む）、`apply` は何も
変えずに止まる。両方のフィンガープリントを示し、`~/.ssh/temt/keys/` と
`~/.ssh/id_*` から EC2 のキーペアに一致する鍵を探して、手がかりとして表示する。
設定ファイルの `IdentityFile` をそのどれかに直すか、`KeyName` を新しい名前にして
`apply` をやり直す。`IdentityFile` に別の鍵を書けるのは、それが EC2 のキーペア
`KeyName` と同じ鍵のときだけである。`KeyName` を新しい名前にすると、`IdentityFile` の
鍵がその名前で登録され（鍵が無ければ作られる）、`terminate` して次に `start` したときから
使われる。`IdentityFile` を書いていなければ、鍵の場所も `~/.ssh/temt/keys/<新しい名前>` に
変わるので、ふつうは新しい鍵が作られる。`apply` が EC2 のキーペア
そのものや `IdentityFile` を書き換えることはない。初回の `apply` なら、設定ファイルは
まだ作られていないので、`IdentityFile` か `KeyName` を書いたファイルを用意してから
やり直す（[conf.md](conf.md)）。

使えるのはパスフレーズの無い鍵だけである。パスフレーズ付きの鍵は秘密鍵から公開鍵を
取り出せないので、一致していても手がかりに表示されず（外した理由は表示される）、
`IdentityFile` に書くとエラーになる。

2 台のマシンから同じサービスを使うには、秘密鍵と設定ファイルを両方に置く必要が
ある。

### キーペアを見る、作る、消す

    temt keypair list
    temt keypair show <キーペア名>
    temt keypair show ~/.ssh/id_ed25519
    temt keypair create <キーペア名>
    temt keypair delete <キーペア名>

キーペアはどのサービスにも属さないので、どれも `--svc` を取らず、`--svc` を付けない
`ami prune` と同じく `--profile` と `--region` で環境を選ぶ（[共通のコマンドラインオプション](#共通のコマンドラインオプション)）。
サービスのコマンド `temt-<svc>` からは打てない。フィンガープリントは
`ssh-keygen -l` と同じ `SHA256:` の形で出るので、手元で `ssh-keygen -l -f <鍵>` の
結果と見比べられる。

`keypair list` は、temt が作ったキーペア（`temt:keypair` タグの付いたもの）の一覧を
表示する。

    account 123456789012, region ap-northeast-1, profile (none)
    NAME   FINGERPRINT                                          CREATED               LOCAL     USED
    temt   SHA256:6WY4Q/IMpvszygUuoDFWa5HtIuuWpuHXV7YT0kJaNlQ   2026-09-20 12:00:00   matches   temt-<svc>-template:3

`LOCAL` は、手元の `~/.ssh/temt/keys/<キーペア名>` と一致するか（`matches`、
`differs`、読めなければ `unreadable`、EC2 から公開鍵が得られなければ `unknown`、
手元に無ければ `-`）で、`USED` はそのキー
ペアを使っている起動テンプレートのバージョン（無ければ `-`）である。`--format` で、
キーペアの ID（`ID`）と鍵の種類（`TYPE`）も表示できる。

`keypair show` は、キーペア名を渡せば、temt が作ったかどうかを問わず、そのキー
ペアの詳細と、一致する手元の鍵（`~/.ssh/temt/keys/` と `~/.ssh/id_*` から探す）を
表示する。EC2 のコンソールに出るフィンガープリントも `KeyFingerprint` として
並べるが、RSA の鍵や取り込んだ鍵では、計算のしかたが違うので `fingerprint` とは
一致しない。`/` を含む引数は手元の鍵のファイルとみなし、その鍵の種類や
フィンガープリントと、一致する EC2 のキーペアを表示する。今のディレクトリの鍵は
`./<ファイル>` と打つ。秘密鍵でも公開鍵でもよい。

`keypair create` は、`~/.ssh/temt/keys/<キーペア名>` に新しい鍵を作り、EC2 に
登録する。手元か EC2 に同じ名前のものが既にあれば、何もせずに止まる。作った
キーペアを使うには、設定ファイルの `KeyName` をその名前にして `apply` する。
キーペア名には、英数字で始まり、英数字、`.`、`_`、`-` だけが続き、`.pub` で
終わらない名前を使う。

`keypair delete` は、EC2 のキーペアと、手元の `~/.ssh/temt/keys/<キーペア名>`、
`<キーペア名>.pub` を消す。鍵を作り直すときに使う。消す前に `[y/N]` で確認を
求め、`--yes` を付けると確認しない。消せるのは temt が作ったキーペア
（`temt:keypair` タグの付いたもの）だけで、タグの無いキーペアは消せない。
起動テンプレートや、終了していないインスタンスがそのキーペアを使っていれば
警告するが、止めはしない。そのキーペアで起動したインスタンスには、消した後は
ログインできなくなる。サービスの `KeyName` がそのキーペアを指していて
`IdentityFile` が既定のままなら、次の `apply` が新しい鍵を作って同じ名前で登録し
直し、次に `start` したインスタンスから使われる。

手元の鍵は、どのリージョンとアカウントにも共通の 1 つである。`apply` は既定の
`KeyName` のまま、サービスのリージョンごとに同じ鍵を登録する。手元の鍵が消えると、
どのリージョンのインスタンスにもログインできなくなる。temt は他のリージョンを
調べないので、同じ鍵を登録したリージョンごとに `keypair delete` する。しなかった
リージョンでは、`apply` が鍵の不一致で止まる。

鍵を作り直す手順は次のとおり（既定の `KeyName` と `IdentityFile` のまま使って
いるとき）。`apply` から先は、その鍵を使うサービスごとに行う。

    temt --region <リージョン> keypair delete temt   # 鍵を登録したリージョンごとに
    temt-<svc> apply                                # 新しい鍵を作って登録する
    temt-<svc> terminate
    temt-<svc> start                                # 新しい鍵で起動する

## アプリ

### アプリを入れる

アプリは、インスタンスの中で動くものを 1 つのディレクトリにまとめたものである。
作り方は [apps.md](apps.md) にある。

`deploy` はロールに触らない。アプリが AWS を使うなら、要る権限を設定ファイルに
書き、`deploy` より先に `apply` する（[apps.md](apps.md#aws-を使う)）。

動いているインスタンスに入れ、確かめてから AMI に焼く。

    temt-<svc> start
    temt-<svc> deploy ./jupyter     # / を含めばパス。含まなければアプリ ID
    temt-<svc> bake                 # 焼くまで次のインスタンスには入らない

`deploy --bake` とすれば、入れ終えたあとに続けて焼く。確認は始める前に出る。
アプリ ID で指すと、`~/.config/temt/apps/<アプリID>/` を探し、無ければ temt と
一緒に配るアプリを使う。同じアプリ ID で置けば、temt と一緒に配るアプリの代わりに
自分のものが使われる。
入れたアプリは、インスタンスの `/opt/temt/apps/<アプリID>/` に置かれ、記録が
`app list` に出る。途中で失敗しても巻き戻さないので、直してもう一度 `deploy` する。

    ID               NAME             VERSION   DEPLOYED              STATE     BAKED   LOCAL
    emacs            emacs            2         2026-09-28 09:58:31   baked     2       2
    idle-shutdown    idle-shutdown    2         2026-09-28 09:41:02   baked     2       2
    jupyter          Jupyter Lab      -         -                     -         -       1.2
    mise             mise             3         2026-10-01 10:20:13   lost      2       3
    swapfile         swapfile         -         -                     -         -       1
    timezone-tokyo   timezone-tokyo   1         2026-10-01 10:26:57   unbaked   -       1

- `VERSION` は最後に入れた版、`BAKED` は起動テンプレートの AMI に焼いてある版
  （次の `start` で入っている版）、`LOCAL` はアプリ ID で指したときに使われるもの
  の版である。アプリ ID で指せるアプリは、入れていなくても表示する。`LOCAL` が `VERSION`
  と違えば、もう一度 `deploy` すれば更新になる。
- `STATE` は、最後に入れた版を焼いたかどうか。`baked` は AMI に入っている。
  `unbaked` は、いまのインスタンスにだけあり、焼く前にインスタンスが終了すると
  消える（`idle-shutdown` の自動終了も含む）。`lost` は、焼く前にインスタンスが
  終了して消えたか、`apply` で別の AMI に替えたもので、もう一度 `deploy` して
  焼く。`removed` は、いまのインスタンスからは外したが、AMI には残っている。焼けば
  記録ごと消える。焼く前にインスタンスが終了すると、次のインスタンスには AMI から
  戻るので、`baked` に戻る。`-` は入れていないか、焼いたかどうかを記録していない。
  同じ版を入れ直しても、焼くまでは `unbaked` になる。

`--svc` を付けない `temt app list` は、アプリ ID で指せるアプリだけを表示する。
AWS には問い合わせないので、`--profile` と `--region` は付けられない。`LOCAL` は
`manifest.toml` の版、`PATH` はアプリのディレクトリで、temt と一緒に配るアプリは
temt の中を指す。`--format` で `DESCRIPTION`（`manifest.toml` の説明）も表示できる。
`--svc` を付けたときは、`--format id,version,local,path` のように列を選べば `PATH` を
表示できる。

    temt app list --format id,local,description

`install` は、アプリの `manifest.toml` の `user` が指すユーザーで実行され、書いて
いなければ root で実行される。`--user` を付ければ、`manifest.toml` の `user` より
優先して、そのユーザーで実行する。ホームに入れるもの（mise など）は、root で入れると
`/root` の下に入る。

    temt-<svc> deploy ./jupyter --user ubuntu

アプリがオプションを宣言していれば（[apps.md](apps.md#オプション)）、アプリの後ろに
`<名前>=<値>` を並べて値を与える。値に空白などを含むときは、シェルの引用符で囲む。
宣言していない名前や、アプリが受け付けない値は、何も変えずに止まる。宣言している
オプションとその既定は、`app show` で見られる。

    temt-<svc> deploy hello port=8080
    temt-<svc> deploy ./hello port=8080 motd=on

`deploy` は、使う値を 1 つ 1 行で、出どころとともに表示する。`given` はコマンドラインで
与えた値、`kept` は前の `deploy` で与えて記録に残った値、`default` はアプリの既定で
ある。

    10:20:15 option port=8080 (given)
    10:20:15 option motd=off (default)

与えた値は記録に残り、次の `deploy` は、与えなかったオプションにその値を引き継ぐ。
版を上げるときも、値を与え直さなくてよい。`<名前>=` と値を空にすれば、その
オプションを既定に戻す。`--reset` を付ければ、前の値を何も引き継がず、与えたもの
だけを使う。`app remove` で外したインスタンスに入れ直すときは、既定から始まる。

    temt-<svc> deploy hello              # 前の port=8080 のまま入れ直す
    temt-<svc> deploy hello port=        # port を既定に戻す
    temt-<svc> deploy hello --reset      # どのオプションも既定に戻す

`install` が失敗すると記録は書かれないので、記録の値は前のままである。やり直す
ときは、同じオプションを付け直す。記録の中身が壊れていて前の値が読めないときは、
何も変えずに止まるので、`--reset` を付けて値を与え直す。

記録した値と、AMI に焼いた値は `app show` で見られる。記録は暗号化されず、値は
AMI にも残るので、オプションには秘密の値を渡さない。

    temt-<svc> app show hello

    app hello:
      name:          hello
      version:       2
      user:          root
      deployed:      2026-10-01 10:40:20
      instance:      i-0123456789abcdef0
      options:       port=8080
      baked_version: 2
      baked_image:   ami-0123456789abcdef0
      baked_options: port=9000
    options in ~/.config/temt/apps/hello (version 2):
      port  8000  the port to listen on in the instance
      motd  off   show the URL in the login message: on or off
    keys: none

`app remove <アプリID>` は、`uninstall` を実行してディレクトリを消し、状態
（キー `status`）を消す。
焼くまで AMI には残る。AMI に焼いてあったアプリなら、そのあいだ記録が残り、
`app list` の `STATE` は `removed` になる。焼くと記録も消える。アプリの値は残るので、要らなければ `app unset` で消す。
アプリのために書いた権限も残るので、要らなければ設定ファイルから消して
`apply` する。`uninstall` は、`install` を実行したのと同じユーザーで実行される。
`--user` で変えられる。

`destroy` のあとに `apply` し直したときは、`start` してから同じアプリを `deploy`
し直すと、記録が戻る。

`deploy` と `app remove` はインスタンスに ssh でつなぐ。ssh の `ProxyCommand` には
`--profile` が届かないので、マシンごとにプロファイルが違うときは、`--profile` では
なく設定ファイルの `AwsProfile` を直す。届かずに別のインスタンスにつながったときは、
何も変えずに止まる。Ctrl-C で止めても、インスタンスの上の `install` や `uninstall`
は走り続けることがある。

### アプリの値

インスタンスの中で動くアプリは、設定や状態を SSM パラメータ
`/temt-<svc>/<アプリID>/<キー>` に置く。手元からは `app` のコマンドで扱う。
`apply` していないサービスでは、どれも `apply` を促して止まる。

    temt-<svc> app list                    # 入っているアプリと、入れられるアプリの一覧
    temt-<svc> app show jupyter            # 記録、宣言したオプション、キーと更新時刻の一覧。値は表示しない
    temt-<svc> app get jupyter token       # 値をそのまま標準出力に書く。改行は足さない
    temt-<svc> app set jupyter limit=2h    # 値を書く
    temt-<svc> app unset jupyter limit     # 値を消す。確認は取らない

アプリ ID とキーは、英小文字で始まり、英小文字・数字・ハイフンが続き、英小文字か
数字で終わる 64 文字以内の名前である。キー `deploy` はアプリを入れたときの記録で、
`get` で読めるが、`set` と `unset` では扱えない。`set` は、記録の無いアプリには
書かない。アプリ ID の打ち間違いを防ぐためである。`get` は改行を足さないので、
`temt-<svc> app get jupyter token > token.txt` のように、値をそのままファイルや
コマンドに渡せる。

値は、暗号化されない `String` のパラメータとして書かれる。同じアカウントの資格情報を
持つ者と、パラメータを読める権限を持つ同じアカウントのインスタンスからは読めるので、
秘密の値を置くときはそれを承知しておく。temt のインスタンスは、既定では自分の
サービスの階層だけを読み書きできる（[conf.md](conf.md#role)）。

キー `status` は、アプリがインスタンスから書く、いまの状態である。
`temt-<svc> status` の `app status` に、アプリごとに出る（[状態を見る](#状態を見る)）。

### 自動終了

temt と一緒に配るアプリ `idle-shutdown` を入れて焼くと、そのサービスのインスタンスは、
誰も使っていない状態が制限時間（既定は 1 時間）続けば自分で電源を切って終了する。
制限時間は、`deploy` のオプション `limit` で、サービスごとに決められる。電源を切る
予定は、手元から `temt-<svc> status` の `app status` で見られる。

    temt-<svc> start
    temt-<svc> deploy idle-shutdown --bake
    temt-<svc> deploy idle-shutdown limit=4h --bake   # 制限時間を 4 時間にする

使用中とみなすもの、制限時間の変え方、ログインのメッセージ、アプリから使用中を
知らせる方法、外し方は [apps/idle-shutdown.md](apps/idle-shutdown.md) にある。

## ssh

### つなぐ

    temt-<svc> start
    ssh temt-<svc>

`ssh` は既定で Session Manager 経由でつなぐ。通信は https で AWS へ向かうので、
セキュリティグループに受信許可が無くても、sshd のポート（既定は 22）で外に出られない
ネットワーク（https しか通さないファイアウォールの内側など）からでも入れる。

インスタンスは `start` で新しく起動するたびに新しいホスト鍵を持つので、`ssh` は
そのつどホスト鍵の確認を求める。`reboot` で再起動しても、止めて再開しても、ホスト鍵は
変わらない。フィンガープリントを受け入れると、そのインスタンス専用の
known_hosts に記録される。

### sshd のポートを変える

インスタンスの sshd が待ち受けるポートは、設定ファイルの `SshdPorts` で決める。
書かなければ 22 である。変えるには、設定ファイルに書いて `apply` する。最上位の
項目なので、`[launch-template]` などのテーブルより前に書く。

    SshdPorts = [2222]

    temt-<svc> apply

`apply` は、関連付け `temt-<svc>-sshd`（インスタンスの sshd の設定を揃えるよう、
Systems Manager の State Manager に登録する設定）を AWS に置き、ssh の設定の
`Host temt-<svc>` ブロックに `Port 2222` を書く。関連付けは ssh を経由せずに
インスタンスへ届き、動いているインスタンスにも数秒で効く。`status` の
`sshd-ports` が `applied` になれば、`ssh temt-<svc>` は 2222 でつながる。`apply` の
直後はまだ切り替わっておらず、つながらないことがある。Session Manager 経由でも
直接接続でも同じである。sshd は、書いたポートだけで待ち受け、22 では受けなくなる。
複数のポートを書けば（`[2222, 443]`）、そのすべてで待ち受け、先頭が ssh の設定の
`Port` になる。ほかのポートは `ssh -p 443 temt-<svc>` のように指す。

動いているインスタンスのポートを変えたときと、`-p` で別のポートに初めてつなぐ
ときは、ホスト鍵の確認をもう一度求められる。ssh がポートごとに別のホストとして
扱うためで、インスタンスは同じである。確認のとき ssh は、同じ鍵を記録している
ほかの名前（`temt-<svc>` など）を表示するので、それで同じインスタンスだと分かる。

直接接続で使うなら、セキュリティグループの受信規則も新しいポートに合わせる
（[直接接続](#直接接続)）。ポートごとに規則が要る。temt は合わせない。

    temt-<svc> sg add 2222 --description home
    temt-<svc> sg remove 22
    temt-<svc> apply

**焼くまでの振る舞い。** ポートの設定はインスタンスの中にあるので、焼かなければ
次のインスタンスには残らない。今の `SshdPorts` と違うポートで焼いた AMI（素の
Ubuntu を含む）から `start` で起動したインスタンスは、SSM エージェントが
応答してから 10 秒ほどのあいだ AMI のポート（素の Ubuntu なら 22）で待ち受け、
そのあと `SshdPorts` に切り替わる。`start` は切り替わりを待たない。この間に
`ssh temt-<svc>` すると、切り替わるまで待ってからつながる。今の `SshdPorts` で
焼いた AMI でも、起動直後に Session Manager 経由でつなぐと、関連付けが確かめ
終わるまで（数秒）待つことがある。

AMI と違うポートで待ち受けているインスタンスには、ログインのたび（出る条件は
自動終了の [ログインのメッセージ](apps/idle-shutdown.md#ログインのメッセージ) と
同じ）に次のメッセージが出る。焼き忘れに気づくためのもので、今の `SshdPorts` で
焼いた AMI から起動したインスタンスには出ない。

    temt: sshd ports were changed from the AMI's (22 -> 2222); run 'temt bake --svc <svc>' to keep them in the AMI

つながることを確かめ、`status` の `sshd-ports` が `applied` なら、焼く。`bake` は
`SshdPorts` が揃っているかを確かめないので、`apply` し忘れや `failed` のままでも
焼いてしまう。

    temt-<svc> bake

**間違えたとき。** つながらないポートにしてしまっても（受信規則が無い、手元の
ネットワークが通さない）、関連付けは ssh を経由しないので、設定ファイルを直して
`apply` すれば直る。sshd が受け付けない設定と、他のプログラムが使っていて待ち受け
られないポートは、関連付けが前のポートに戻し、`status` の `sshd-ports` に `failed` と
出る。理由は temt には出ないので、AWS の Systems Manager の State Manager にある
`temt-<svc>-sshd` の実行履歴で見る。前のポートは、起動直後なら AMI のポート（素の
Ubuntu なら 22）、そうでなければ直前の `SshdPorts` の値である。そのポートには
`ssh -p <前のポート> temt-<svc>` で入れる。直接接続でそのポートの受信規則を消して
いれば、`TEMT_LOGIN=ssm ssh -p <前のポート> temt-<svc>` で入る。直接接続は関連付けの
結果を見ないので、失敗していても sshd が受け付けていないと出るだけである。`status` で
確かめる。

`apply` が関連付けの実行を求めるのは、関連付けを作ったときと中身を変えたときだけ
である。ポートを使っていたプログラムを止めるなど、インスタンスの中で原因を直した
あとに同じポートでやり直すには、aws-cli で実行を求める（`--profile` と `--region` は
サービスのものに合わせる）。関連付けの ID は、1 行目で名前から引く。

    aws ssm list-associations --association-filter-list key=AssociationName,value=temt-<svc>-sshd --query "Associations[?AssociationName=='temt-<svc>-sshd'].AssociationId" --output text
    aws ssm start-associations-once --association-ids <上で出た ID>

### `~/.ssh/temt/` の中身

```
config                    ssh の設定。apply がサービスの Host ブロックを足し、更新する
keys/                     鍵。apply が作った temt と temt.pub
                          （KeyName を変えたときはその名前）と、
                          keypair create が作った鍵
known_hosts.d/<名前>/     インスタンスごとの known_hosts
```

サービスに ssh の設定を足したいとき（`LocalForward` など）は、`config` の
`Host temt-<svc>` ブロックに書き足す。`apply` は、そのサービスのブロックの
中の temt が管理する項目（`User`、`Port`、`IdentityFile`、`IdentitiesOnly`、
`UserKnownHostsFile`、`ProxyCommand`）だけを書き換える。他の項目、他のブロック、
コメントには触れない。管理する項目を手で変えても、次の `apply` で temt が決めた
値に戻る。`Host temt-<svc> <別名>` のように Host 行に別名を並べてもよく、`apply` は
そのブロックをサービスのブロックとして扱う。

## ファイルの場所を変える

設定ファイルの場所 `~/.config/temt/` と、ssh の設定・鍵・known_hosts の場所
`~/.ssh/temt/` は、それぞれ環境変数で変えられる。絶対パスで書き、設定しなければ
既定の場所を使う。値の規則は [conf.md](conf.md#場所を変える環境変数) にある。

    export TEMT_CONF_DIR=/srv/temt/conf
    export TEMT_SSH_DIR=/srv/temt/ssh

`TEMT_CONF_DIR` は、設定ファイルの組を切り替えるのに使える。変えれば、`temt` は
次からその場所の設定ファイルを読む。ssh の設定は場所をまたいで 1 つなので、
どの場所で `apply` したサービスにも `ssh temt-<svc>` でつながる。同じサービスの
設定ファイルを 2 つの場所に置くことはできず、別の場所にまだあれば `apply` が
止まる。場所を分けても、サービス名は全体で重ならないように付ける。設定ファイル
には鍵が入らないので、他のマシンと同期するディレクトリにも置ける。

`TEMT_SSH_DIR` を変えたら `apply` をやり直す。ssh の設定に書かれるパスが新しい
場所に変わり、`~/.ssh/config` の `Include` も書き換わる。前の場所にある Host には
ssh でつながらなくなるので、`apply` はそれらを表示して警告する。前の場所にある鍵や
known_hosts を temt が動かすことはないので、要るものは手で移す。鍵を置く場所なので、
他のマシンと同期するディレクトリや、バージョン管理下のディレクトリは指さない。

どちらの場所も ProxyCommand に書かれるので、環境変数の無いシェルからでも、別の
値を設定したシェルからでも、`ssh temt-<svc>` は `apply` のときと同じ場所を使う。
