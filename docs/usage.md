# temt の使い方

temt のコマンドの使い方を、扱う対象ごとに説明する。はじめて使うときの流れは
[README.md](../README.md#使い方の例) にある。

## 用意するもの

- Linux（Ubuntu 24.04 / 26.04 で動きを確かめている）
- mise か uv。temt を入れる（[入れ方](#入れ方)）
- aws-cli v2。Session Manager 経由の ssh に使う
- session-manager-plugin。ssh は既定で Session Manager 経由でつなぐ
- ssh と ssh-keygen（openssh-client）

ssh の設定に書かれる ProxyCommand は `temt` を、`temt` は `ssh`、`aws`、
`session-manager-plugin` を、PATH から探す。ssh を打つシェルにも、ssh を呼ぶ
エディター（VS Code の Remote-SSH など）にも、これらが PATH にあるようにしておく。

AWS の資格情報は、EC2、IAM、SSM を操作できる権限と、インスタンスロールに付ける権限を確かめる
IAM Access Analyzer の `access-analyzer:ValidatePolicy`、ログインの鍵をインスタンスに
送る EC2 Instance Connect の `ec2-instance-connect:SendSSHPublicKey` を持つ AWS のプロファイルとして
`~/.aws/` に設定しておく。どのリージョンを使うかは、設定ファイルに `AwsRegion` を
書かなければ、`--region` や環境変数 `AWS_REGION` などで、コマンドのたびに決まる
（[共通のコマンドラインオプション](#共通のコマンドラインオプション)）。

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

AWS リソースを残さないなら、消す前にサービスごとに、クローンを作ったリージョンでも
`destroy` する（[片づける](#片づける)）。AMI は `destroy` でも残るので、要らなければ
これも消す前に `ami prune` か `ami delete` で消す。

設定ファイルの場所 `~/.config/temt/`、ssh の設定と鍵の場所 `~/.ssh/temt/`、
サービスのコマンド `~/.local/bin/temt-<svc>`、`~/.ssh/config` の先頭に足された目印の
コメントと `Include` の 2 行は残る。要らなければ手で消す。

## 考え方

### サービス

*サービス* はひとつの用途のために立てる EC2 インスタンスの定義である。1 つのサービスは
1 つの設定ファイルで定め、リージョンごとに 1 台のインスタンスを持つ。同じ設定ファイルを
別のリージョンで `apply` すると、そこにクローンができる
（[別のリージョンにクローンを作る](#別のリージョンにクローンを作る)）。同じ用途を
設定違いで複数立てるときは、設定ファイルをコピーして別のサービスにする
（[同じ用途を設定違いで立てる](#同じ用途を設定違いで立てる)）。

ssh の経路（[直接接続](#直接接続)）は手元のネットワークで変わるので、設定ファイル
ではなく環境変数 `TEMT_LOGIN` で与える。

### 名前

サービスの名前は利用者が決める。サービス名は英小文字で始まり、英小文字・数字・
ハイフンが続き、英小文字か数字で終わる（`dev-main` など）。`temt-<svc>` が 64 文字に
収まる長さにする。`defaults` は共通ファイルの名前なので使えない。

以下、サービス名を `<svc>` と書く。ssh の Host 名や、temt が作る AWS のリソースの名前は
`temt-<svc>` になる。

### ファイルの場所

手元に置くものは次のとおり。設定ファイル（自分のアプリを含む）の場所と、ssh の設定と
鍵の場所は、環境変数で変えられる
（[ファイルの場所を変える](#ファイルの場所を変える)）。

- 設定ファイル `~/.config/temt/<svc>.toml`（[conf.md](conf.md)）
- 全サービスに共通の値を書く共通ファイル `~/.config/temt/defaults.toml`
  （[conf.md](conf.md#全サービスに共通の値)）
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
ただし `app list` は、付けても省いても、アプリ ID で指せる手元のアプリを表示する。

| コマンド | 短縮形 | すること |
| --- | --- | --- |
| `temt service apply --svc <svc> [--image-id <AMI の ID または名前>] [--yes]` | `apply` | サービスを用意する、設定を反映する |
| `temt service diff --svc <svc> [--image-id <AMI の ID または名前>]` | `diff` | 次の `apply` が何を変えるかを見る |
| `temt service destroy --svc <svc> [--yes]` | `destroy` | サービスを片づける |
| `temt service import --svc <svc> <インスタンスID> [--yes]` | | 止まっている EC2 インスタンスを取り込む |
| `temt service list [--svc <svc>] [--format <列>]` | | 手元の設定ファイルのサービスの一覧を見る |
| `temt instance start --svc <svc> [--async]` | `start` | インスタンスを起動する。止めたものは再開する |
| `temt instance stop --svc <svc> [--async] [--yes]` | `stop` | インスタンスを終了せずに止める |
| `temt instance terminate --svc <svc> [<インスタンスID>...] [--async] [--yes]` | `terminate` | インスタンスを終了する。ID を指せば、それだけを終了する |
| `temt instance reboot --svc <svc> [--async] [--yes]` | `reboot` | インスタンスを終了せずに再起動する |
| `temt instance show --svc <svc> [--apps]` | `status` | インスタンスの状態を見る。`--apps` で、入っているアプリも |
| `temt instance list [--svc <svc>] [--format <列>]` | | インスタンスの一覧を見る |
| `temt ami bake --svc <svc> [--yes]` | `bake` | インスタンスを AMI にする |
| `temt ami list [--svc <svc>] [--format <列>]` | | AMI の一覧を見る |
| `temt ami prune [--svc <svc>] [--yes]` | | どのテンプレートも指していない AMI を消す |
| `temt ami delete [--svc <svc>] <AMI の ID または名前> [--force] [--yes]` | | 指した AMI を 1 つ消す |
| `temt template show --svc <svc>` | | 起動テンプレートを見る |
| `temt role import --svc <svc> <IAM ロール名または ARN> [--yes]` | | インスタンスロールを temt の管理に移す |
| `temt role delete --svc <svc> [--yes]` | | temt のインスタンスロールとインスタンスプロファイルを消す |
| `temt sg list --svc <svc> [--format <列>]` | | 設定ファイルの受信規則の一覧を見る |
| `temt sg add --svc <svc> <ポート> [<CIDR>] [--description <説明>]` | | 設定ファイルに受信規則を足す |
| `temt sg remove --svc <svc> <ポート> [<CIDR>]` | | 設定ファイルから受信規則を消す |
| `temt sg diff --svc <svc>` | | 次の `apply` が受信規則をどう変えるかを見る |
| `temt sg show --svc <svc>` | | セキュリティグループを見る |
| `temt app list [--svc <svc>] [--format <列>]` | | 入れられるアプリの一覧を見る |
| `temt app show --svc <svc> <アプリID>` | | アプリの記録、オプション、キーの一覧を見る |
| `temt app get --svc <svc> <アプリID> <キー>` | | アプリの値を 1 つ読む |
| `temt app set --svc <svc> <アプリID> <キー>=<値>` | | アプリの値を 1 つ書く |
| `temt app unset --svc <svc> <アプリID> <キー>` | | アプリの値を 1 つ消す |
| `temt app deploy --svc <svc> <アプリIDまたはパス> [<名前>=<値>...] [--reset] [--user <ユーザー>] [--bake] [--yes]` | `deploy` | アプリを入れる |
| `temt app remove --svc <svc> <アプリID> [--user <ユーザー>] [--bake] [--yes]` | | アプリを外す |
| `temt ssh proxy --svc <svc> [ポート] [--user <ユーザー>]` | `proxy` | ssh の ProxyCommand として動く。手では打たない |
| `temt ssh set-port --svc <svc> <ポート>... [--yes]` | | sshd の待ち受けポートを変える |

警告、エラー、確認の問いかけと、それらに添える行は標準エラー出力に、それ以外は標準
出力に出る。ただし、表や `diff` の変更だけを標準出力に残すよう、`--svc` を付けない
コマンドが最初に出す環境の行と、`diff` と `sg diff` の知らせも標準エラー出力に出る。

### サービスのコマンド

`apply` は、サービスごとにコマンド `~/.local/bin/temt-<svc>` を置く。中身は、PATH に
ある `temt` に `--svc <svc>` を付けて実行するシェルスクリプトで、`temt-<svc> ...` は
`temt ... --svc <svc>` と同じである（`temt-<svc> start` は `temt start --svc <svc>`
と同じ）。サービス名をシェルの補完で打てる。`~/.local/bin` は PATH に入れておく。

`--svc` を省けるコマンドも、このコマンドから打てば、そのサービスのものだけを扱う。
どのコマンドも、`--svc` を重ねると引数の誤りになる。

`apply` は、`~/.local/bin/temt-<svc>` が無いときだけ書く。何かあれば、中身に
かかわらず触れない。`destroy` しても残る。

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

AWS のプロファイルを選ぶ `--profile <プロファイル>` と、リージョンを選ぶ
`--region <リージョン>` は、AWS を呼ぶどのコマンドにも付けられる。AWS に問い合わせない
`service list`、`sg list`、`sg add`、`sg remove`、`app list` は、どちらも付けられない。

`--svc` を付けたコマンドのリージョンは、設定ファイルの `AwsRegion`、`--region`、
共通ファイルの `AwsRegion`、`AWS_REGION`、`AWS_DEFAULT_REGION`、AWS のプロファイルの
`region` の順で決まり、どれも無ければ `ap-northeast-1` になる。AWS のプロファイルは、
設定ファイルの `AwsProfile`、`--profile`、共通ファイルの `AwsProfile`、AWS の既定の規則
（`AWS_PROFILE` など）の順で決まる。設定ファイルに書いた値はそのサービスに固定され、
違う `--region` や `--profile` を付けると引数の誤りになる。設定ファイルが無いときの
`apply` と `service import` は、`--profile` か環境変数で AWS のプロファイルを選んで
いれば、それを新しく書く設定ファイルの `AwsProfile` に書く。リージョンは書かない。

確認を求めるコマンド（`apply`、`destroy`、`terminate`、`bake` など）は、扱う
リージョンとアカウント ID を確認に添える。`--profile` や `--region` の付け忘れに、
そこで気づける（`role import` は手元のファイルだけを書き換えるので添えない）。

`--svc` を付けない `ami list`、`instance list`、`ami prune`、`ami delete` は
サービスの設定ファイルを読まないので、アカウントとリージョンを `--profile` と
`--region` で選ぶ。省けば、共通ファイルの `AwsProfile` と `AwsRegion`
（[conf.md](conf.md#全サービスに共通の値)）、AWS の既定の規則（`AWS_PROFILE`、
`AWS_REGION`、AWS のプロファイルの `region` など）の順で決まる。どの環境を使ったかは、
最初の行に出る。`profile` は、`--profile`、共通ファイル、`AWS_PROFILE`、
`AWS_DEFAULT_PROFILE` のどれかで選んだときだけ名前が出る。選んでいなければ
`(none)` と出て、資格情報は AWS の `[default]` プロファイルや環境変数の鍵などから来ている。

    temt --profile work --region us-east-1 ami prune

### 一覧の表

`list` のコマンドは、1 行目に列の名前（見出し）を、2 行目から 1 件を 1 行で
表示する。値の無いところは `-` と表示し、1 件も無ければ見出しだけを表示する。

    temt instance list | awk 'NR > 1 && $3 == "running" { print $1 }'

`--format <列>[,<列>...]` を付けると、表示する列とその順を選べる。列は見出しの名前で、
大文字でも小文字でもよい。既定では表示しない列も、ここで選べる。知らない名前を
書くと、使える名前が表示される。時刻（`2026-09-20 12:00:00` の形の手元の時刻）と、
AMI の名前や `ImageId` など AWS や設定ファイルから来る値は空白を含むことがあるので、
`awk` で列を切り分けるなら、そういう列は最後に置く。

    temt ami list --format id,used,created

### 完了を待たない

`start`、`stop`、`reboot`、`terminate` に `--async` を付けると、AWS が要求（起動、
再開、停止、再起動、終了）を受け付けるまでしか待たない。

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
設定ファイルが無いときは、その前に、新しくサービスを作るかを `[y/N]` で聞く。

- 設定ファイル `~/.config/temt/<svc>.toml`。無ければ雛形が書かれる。雛形には、
  `--profile` か環境変数で AWS のプロファイルを選んでいればその `AwsProfile`、temt の
  インスタンスロールを付ける `IamInstanceProfile` の行、既定の `[[role.ec2]]`、受信規則と
  ポリシーの例のコメントが入る（[conf.md](conf.md#雛形)）。
  すでにある設定ファイルには、`apply` は何も書かない
- AWS リソース。セキュリティグループ、起動テンプレートと、設定ファイルか共通ファイルに
  `[role]` があればインスタンスロールとインスタンスプロファイル `temt-<svc>`。インスタンスロールには、
  `[[role.<ポリシー名>]]` ごとにインラインポリシー `temt-<ポリシー名>` が付く
  （[conf.md](conf.md#role)）
- ssh の設定。`~/.ssh/temt/config` に `Host temt-<svc>` のブロックが
  足され、`~/.ssh/config` の先頭に、目印のコメントと
  `Include ~/.ssh/temt/config` の 2 行が足される
- ログインの鍵 `~/.ssh/temt/keys/temt`。Host ブロックがこの鍵を使っていて、無ければ
  作られる（[ログインの鍵](#ログインの鍵)）
- サービスのコマンド `temt-<svc>`。`~/.local/bin` に無ければ書かれる
  （[サービスのコマンド](#サービスのコマンド)）

途中で失敗したら、同じコマンドをもう一度実行すればよい。

### 設定を変える

設定ファイルを直して、もう一度 `apply` を実行する。起動テンプレート、
セキュリティグループの受信規則、インスタンスロールの権限、ssh の設定が追従する。動いて
いるインスタンスは変わらず、次の `start` から反映される。受信規則はすぐに、インスタンス
ロールの権限は IAM の反映を待てば、動いているインスタンスにも効く。項目と既定は
[conf.md](conf.md) にある。書いていない項目は、`apply` と `diff` のたびに既定で埋まり、
サブネットなどはそのリージョンで探して決まる。temt の版で既定が変われば、次の
`diff` に出る。

`apply` の前に、何が変わるかを `diff` で確かめられる。

    temt-<svc> diff

変更は 1 つにつき 1 行で表示される。頭の `+` は作る・足す、`~` は直す、`-` は消す
もので、続けて種類、名前、違いが並ぶ。起動テンプレートは項目ごとに 1 行で、
`NetworkInterfaces[DeviceIndex=0].SubnetId` のように項目の位置を書く。インスタンス
ロールのポリシーの中身、ユーザーデータ、ssh の Host ブロックは、違う行が続けて字下げ
して表示される。違いが無ければ、標準出力には何も表示されない。`apply` も、確認を求める前に同じ形で表示する。

### 同じ用途を設定違いで立てる

設定ファイルを別の名前でコピーし、直してから `apply` を実行する。コピーしたファイルは別の
サービスで、AWS リソースもインスタンスも、インスタンスロール（`temt-<コピーした先の svc>`）も
別に持つ。元のサービスの AMI から始めるには、`--image-id` でその AMI を指す
（[使う AMI を選ぶ](#使う-ami-を選ぶ)）。指さなければ、コピーした先のサービスから焼いた
AMI か、無ければ Ubuntu 24.04 から始まる。

    cp ~/.config/temt/dev-main.toml ~/.config/temt/dev-sub.toml
    temt apply --svc dev-sub --image-id temt-dev-main-2609201200

そのあとコピーした先で焼けば、コピーした先のサービスの AMI になる。`template show` の
`latest` はコピーした先のサービスから焼いた AMI だけを見るので、元のサービスが新しい AMI を
焼いても表示されない。元のサービスの新しい AMI を使うときは、また `--image-id` で
指して `apply` を実行する。

### 別のリージョンにクローンを作る

同じ設定ファイルのまま、別のリージョンを選んで `apply` すると、そのリージョンに同じ
サービスのクローンができる。起動テンプレート、セキュリティグループ、インスタンス、AMI は
リージョンごとに持ち、インスタンスロールとインスタンスプロファイル `temt-<svc>` は
すべてのリージョンで 1 つを使う。`[role]` を変えれば、すべてのリージョンに効く。

リージョンは `--region` で選ぶ。`<svc>.toml` に `AwsRegion` を書いていれば、それを
書き替える。書き替えると、前のリージョンのクローンは、戻すまでそのサービスのコマンドでは
扱えない。`SubnetId`、ID で書いた `Groups` や `ImageId`、`KeyName` のキーペアのように、
リージョンに依る値を書いたサービスは、そのままではクローンにできない（キーペアは
各リージョンに同じ名前で入れておく）。

焼いた AMI を、temt はコピーしない。`apply` の前に、元のリージョンの AMI を同じ名前で
コピーしておく。タグもコピーする（`--copy-image-tags`）。タグが無いと、`apply` は名前で見つけて
使うが、`ami list` と `ami prune` には出ない。

    aws ec2 copy-image --region us-west-2 --source-region ap-northeast-1 \
        --source-image-id ami-0123456789abcdef0 --name temt-<svc>-2609201200 \
        --copy-image-tags
    temt-<svc> diff --region us-west-2
    temt-<svc> apply --region us-west-2

コピーしている途中（`pending`）は、`apply` が止まるので、終わってからやり直す。`diff` と
`apply` の `ImageId` の行には選んだ AMI の名前が出て、コピーし忘れて Ubuntu 24.04 を
選んだときはそのことも出る。

クローンでは、次のことに気をつける。

- アプリの値（[アプリの値](#アプリの値)）はリージョンごとにあるので、`app set` した値は、
  コピーした先でも `app set` し直す。焼いたアプリは、記録と一緒に AMI に入っているので、
  コピーした先でもそのまま動き、`status --apps` と `app show` に出る。
- コマンドは、そのときのリージョンのクローンを扱う。`AWS_REGION` などの環境変数で
  切り替えるなら、設定ファイルにも共通ファイルにも `AwsRegion` を書かない。どちらかに
  書いてあれば、環境変数では切り替わらない。
- `ssh temt-<svc>` は `--region` を知らない。`start --region us-west-2` のあとでも、
  ふだんのリージョンにつながる。別のリージョンには `AWS_REGION=us-west-2 ssh temt-<svc>`
  のようにつなぐ。VS Code の Remote-SSH は起動したときの環境を使うので、
  `AWS_REGION=us-west-2 code .` のように起動し直す。
- ssh の `ControlMaster` を使っていると、リージョンを切り替えても前の接続が使い回され、
  前のリージョンのインスタンスにつながる。切り替える前に `ssh -O exit temt-<svc>` で
  閉じる。
- ssh の Host ブロックの `Port` はリージョンをまたいで 1 つなので、`set-port` で
  ポートを変えたら、ほかのリージョンでも `set-port` する。しないと、そのリージョンの
  インスタンスに ssh がつながらない。
- `StrictHostKeyChecking accept-new` を使っていて、2 つのリージョンのインスタンスに
  同時に初めてつなぐと、ホスト鍵がもう一方のインスタンスの known_hosts に書かれ、次の
  接続でホスト鍵が変わったとして止まることがある。
- temt は、クローンを作ったリージョンを覚えない。自分で覚えておき、`--region` を付けて
  確かめる（`temt-<svc> status --region us-west-2`）。`<svc>.toml` の `AwsRegion` を
  書き替えたときも、前のリージョンのクローンは残っている。

片づけるときは、リージョンごとに `destroy` する（[片づける](#片づける)）。

### 既存のインスタンスを取り込む

    temt service import --svc <svc> <インスタンスID> --region <リージョン>
    temt apply --svc <svc> --region <リージョン>

ふだんは止めておき、要るときに開始している EC2 インスタンスを、temt のサービスに
できる。`service import` は、止まっているインスタンスのルートボリュームから AMI を作り、
同じインスタンスを起動できる設定ファイルを書く。続けて `apply` を実行すると起動
テンプレートができ、ほかのサービスと同じく、要るときに `start` で起動し、使い終わったら
`terminate` で終了して使う。

- 取り込めるのは止まっている（`stopped`）インスタンスだけで、設定ファイルの無い
  サービスにだけ取り込める。確認の前に、作る AMI とコピーする設定を表示する。
- 元のインスタンスと、それが使うセキュリティグループやインスタンスプロファイルには
  何もしない。うまくいかなければ、元のインスタンスに戻れる。
- リージョンは、元のインスタンスのあるリージョンを `--region` で指す。リージョンは
  設定ファイルに書かれないので、取り込んだあとのコマンドも同じリージョンで実行する。
  共通ファイルの `AwsRegion` が元と違うときは、毎回 `--region` を付けるか、設定ファイルに
  `AwsRegion` を書いて固定する。`--profile` か環境変数で選んだ AWS のプロファイルは、
  `AwsProfile` に書かれる。
- 設定ファイルの `[launch-template]` には、元のインスタンスの設定がコピーされる。セキュリティ
  グループ、インスタンスプロファイル、キーペア、ユーザーデータも元のものを使い、temt の
  セキュリティグループは作らない。変えたいときは、取り込んだあとに設定ファイルを直して
  `apply` する。
- `ImageId` には作った AMI の名前が書かれ、AMI はそれに固定される。そのままでは
  `bake` など AMI を焼くコマンドは使えないので、焼くなら `ImageId` の行を消す
  （[使う AMI を選ぶ](#使う-ami-を選ぶ)）。
- 共通ファイルに書いた値は、取り込んだサービスにも効く。`[launch-template]` の項目は、元のインスタンスに無ければ足される。共通ファイルに
  受信規則があると、取り込んだサービスには temt のセキュリティグループが無いので、
  `apply` が止まる。共通ファイルに `[role]` があれば、インスタンスロールとインスタンス
  プロファイル `temt-<svc>` が作られる。インスタンスに付くのは、元のインスタンスに
  インスタンスプロファイルが無く、共通ファイルの `IamInstanceProfile` が temt のものを
  指しているときだけである（`diff` に出る）。そう指していて共通ファイルに `[role]` が
  無ければ、`apply` が止まる。共通ファイルの値を効かせたくなければ、設定ファイルに
  `Defaults = false` を書く（[conf.md](conf.md#最上位)）。

取り込んだサービスは、`start` のたびに新しいインスタンスになる。インスタンス ID、
MAC アドレス、ホスト名が変わり、それらに結び付いたもの（ライセンス、送信元/送信先
チェックの設定、ボリュームと ENI のタグ）は引き継がれない。プライベート IP アドレスも
起動のたびに変わる。固定したいときは、元のインスタンスを終了してから、
`NetworkInterfaces` に `PrivateIpAddress = "<元の IP アドレス>"` を書いて `apply`
する。ユーザーデータも `start` のたびに動くので、最初の起動だけでよい処理があれば、
`UserData` の行を消す。

ルートボリューム以外の EBS は AMI に入れない。使うなら、元のインスタンスから外し、
インスタンスの中で付ける。

temt のログインには、インスタンスに SSM エージェントと `ec2-instance-connect` が
入っていて、インスタンスロールに SSM エージェントの権限があることが要る
（[ログインの鍵](#ログインの鍵)、[conf.md](conf.md#role)）。元のインスタンスが SSM に
登録されていなければ、取り込むときに警告し、`start` が SSM エージェントを待たない
よう `WaitForSsm = false` を書く。ssh の Host ブロックには `User ubuntu` と `Port 22`
が書かれるので、Ubuntu でないインスタンスや、sshd が 22 以外で待ち受けるインスタンス
では、`~/.ssh/temt/config` の `Host temt-<svc>` ブロックを直す。

temt のセキュリティグループも使うなら、`[launch-template]` の `Groups` に
`${temt:name}` を足して `apply` する。そうすれば、受信規則を `sg add` などで書ける。
ネットワークインターフェースに付けられるセキュリティグループの数に上限（既定は 5）が
あり、temt のセキュリティグループの送信規則（すべての送信先を許す）も加わる。

### インスタンスロールを temt の管理に移す

    temt-<svc> role import <IAM ロール名または ARN>
    temt-<svc> diff
    temt-<svc> apply

取り込んだサービスのインスタンスロールの権限を temt で扱うには、`role import` で、
そのインスタンスロールのコピーを temt の管理に移す。設定ファイルの `[role]` を、指した
インスタンスロールのポリシーで丸ごと書き換え（インラインポリシーは
`[[role.<ポリシー名>]]` に、管理ポリシーは `ManagedPolicyArns` に）、
`IamInstanceProfile` を temt のもの（`IamInstanceProfile.Name = "${temt:name}"`）に
書き換える。ポリシーは元のままコピーし、既定の `[[role.ec2]]` は足さないので、SSM
エージェントの権限などが要るなら自分で書く（[conf.md](conf.md#role)）。AWS には何もしない
ので、`diff` で確かめてから `apply` する。`apply` が temt のインスタンスロールと
インスタンスプロファイル `temt-<svc>` を作り、起動テンプレートを切り替える。動いている
インスタンスは、`terminate` して `start` するまで前のインスタンスプロファイルのままである。
元のインスタンスロールの ARN を名指ししているもの（S3 のバケットポリシー、KMS の
キーポリシーなど）は、手で直す。

    temt-<svc> role delete

`role delete` は、temt が作ったインスタンスロールとインスタンスプロファイル
`temt-<svc>` を消す。設定ファイルか共通ファイルに `[role]` があるあいだ、ほかの
リージョンで `[role]` のある設定を `apply` したままのあいだ、それを使うインスタンスが
あるあいだは、消さずに止まる。

### インスタンスロールを外す

インスタンスからインスタンスロールを外して確かめるには、設定ファイルの
`IamInstanceProfile` の行を消して `apply` する。次の `start` から、インスタンスロール
無しで起動する。`[role]` が残っていれば、`temt-<svc>` も残るので、行を戻せばまた
付く。共通ファイルの `IamInstanceProfile` で付けているサービスは、設定ファイルで別の
インスタンスプロファイルを指すか、`Defaults = false` を書いて外す。

`[role]` も消して `apply` すると、インスタンスロールとインスタンスプロファイル
`temt-<svc>` も消える（`diff` と確認に出る）。動いているインスタンスがあっても消す。
ほかのリージョンで、`[role]` のある設定を `apply` したままなら、残る。

消したあと `[role]` を戻して `apply` すると、インスタンスプロファイルは作り直しになる。
動いているインスタンスは消えた古いものを指したままで、資格情報が戻らないので、
`terminate` して `start` し直す（[起動する](#起動する)）。また、消したインスタンス
ロールの資格情報が、しばらく使えることもある。外した状態や戻した状態は、新しく起動した
インスタンスで確かめる。

### サービスの一覧を見る

    temt service list

手元の設定ファイル（`~/.config/temt/*.toml`。共通ファイルを除く）を 1 つ
1 サービスとして、一覧を表示する。値は共通ファイルを重ねたものである。AWS には問い合わせない。
`temt-<svc> service list`（`temt service list --svc
<svc>` と同じ）は、そのサービスの行だけを表示する。

    SERVICE    PROFILE   REGION      TYPE
    dev-main   -         -           t3.micro
    web        work      us-east-1   t3.small

`PROFILE` と `REGION` は、書いてあればその値で、書いていなければ `-` になる（そのときの
`--region` や環境変数で決まる）。ほかの既定のある項目は、書いていなくても既定を
表示する。`--format` で `INGRESS`（受信規則の数）も表示できる（[一覧の表](#一覧の表)）。

    temt service list --format service,type,ingress

読めない設定ファイルと、名前がサービス名の規則に合わないファイル
（`Web.toml` など）は、警告して飛ばし、残りを表示してから終了コード 1 で終わる。

### 片づける

    temt-<svc> destroy

そのリージョンで `apply` が作った AWS リソースを消す。ほかのリージョンのクローンには
触らないので、それぞれのリージョンで `destroy` する。消す前に、対象を表示して `[y/N]` で
確認を求める。`--yes` を付けると確認しない。標準入力が端末でないときは `--yes` が要る。
設定ファイルが無ければ止まる。設定ファイルを消したあとで片づけるなら、空の設定ファイルを
作ってから `destroy` する。

インスタンスがあれば、まず `terminate` と同じように終了し、`terminated` に
なるまで待ってから消す。`destroy` はつねに完了を待つので、`--async` は無い。

消えるものは次のとおり。

- 起動テンプレート、`/temt-<svc>` の階層の下にある SSM パラメータ、セキュリティ
  グループ、インスタンスプロファイル、インスタンスロール。この順に消す。確認の一覧に
  名前が出る
- 終了したインスタンスの known_hosts のファイル

インスタンスロールとインスタンスプロファイル `temt-<svc>` は、ほかのリージョンで
`[role]` のある設定を `apply` したままなら残し、確認にそう出る。消すのは temt が作った `temt-<svc>`
だけで、取り込んだサービスが使うほかのものには触らない。消すときは、インスタンス
ロールに付いたインラインポリシーは、手で付けたものも含めてすべて消える。管理ポリシーは
外す（管理ポリシーそのものは残る）。

セキュリティグループは、同じ名前のものを VPC を問わずすべて消す。設定ファイルの
`SubnetId` を別の VPC に変えたあとに残った古いセキュリティグループも、ここで
消える。

残るものは次のとおり。もう一度 `apply` を実行すれば、同じ設定で作り直せる。

- 設定ファイル `~/.config/temt/<svc>.toml`
- ログインの鍵 `~/.ssh/temt/keys/temt`（どのサービスにも共通）
- `~/.ssh/temt/config` の `Host temt-<svc>` ブロック
- known_hosts のディレクトリ `~/.ssh/temt/known_hosts.d/temt-<svc>/`
- サービスのコマンド `~/.local/bin/temt-<svc>`
- そのサービスの AMI

作り直すときの AMI は、はじめて作るときと同じく選び直す
（[使う AMI を選ぶ](#使う-ami-を選ぶ)）。`--image-id` で前の AMI に戻していても、
それは引き継がれない。インスタンスロールを消したあとの `apply` は作り直しになるので、
直後の起動は [起動する](#起動する) のとおりに気をつける。

途中で失敗したら、同じコマンドをもう一度実行すればよい。既に消えたものは飛ばして、
続きから進む。

## インスタンス

### 起動する

    temt-<svc> start

`start` は、インスタンスが動き出し、SSM エージェントが応答するまで待つ。ふつう
1〜2 分かかる。段階が進むたびに 1 行ずつ表示する。設定ファイルに
`WaitForSsm = false` があれば、動き出したところで終わる（[conf.md](conf.md#最上位)）。
`apply` でインスタンスロールとインスタンスプロファイルを作った（作り直した）直後は、
IAM の反映が追いつかずに、`start` が止まったり、SSM エージェントを締め切りまで待って
止まったりすることがある。数分のうちに起動したインスタンスには消えた古いインスタンス
プロファイルが付くことがあり、`reboot` しても直らない。`terminate` して数分あけてから
`start` し直す。起動そのものを断られたときは、インスタンスはできていないので、1 分ほど
おいて `start` し直す。`--async` で起動してつながらないときも同じである。既に動いていれば、
2 台目を起動せず、ログインできることを確かめる。
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

何かの手違いで同じサービスのインスタンスが 2 台以上あるときは、`start` は起動せず、
それらを表示して止まる
（[インスタンスが 2 台になったとき](#インスタンスが-2-台になったとき)）。
同じサービスで `start`、`stop`、`terminate`、`reboot`、`bake`、`destroy`、
`service import`、`set-port`、`deploy`、`app remove` のどれかが既に動いていれば、その終わりを待たず、動いて
いると表示して終わる。

### 状態を見る

    temt-<svc> status
    temt-<svc> status --apps

サービスの今の状態を表示する。`status` は `instance show` の短縮形で、
`temt-<svc> instance show` と書いても同じである。`--apps` を付けると、インスタンスに
入っているアプリも表示する。

    template:   temt-<svc>-template
    instance:   i-0123456789abcdef0 (running)
    ipv4:       203.0.113.10
    ssm-agent:  Online
    apps:
      ID              VERSION  BAKED  DEPLOYED
      idle-shutdown   2        yes    2026-10-20 12:00:00
      timezone-tokyo  1*       no     2026-10-23 14:10:00
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
- `apps` は、`--apps` を付けたときだけ出る、インスタンスに入っているアプリ。動いている
  インスタンスから SSM の Run Command で読むので、数秒かかり、手元の資格情報に `reboot`
  と同じ権限が要る。ログインとは数えないので、`idle-shutdown` の自動終了は延びない。インスタンスが動いていないか、SSM エージェントが
  `Online` でなければ、読まずに理由を表示する（`(not read; i-0123456789abcdef0 is stopped)`）。
  - `VERSION` は入っている版。後ろに `*` があれば、アプリ ID で指したときに使われる
    手元のアプリの版がそれと違う。もう一度 `deploy` して焼けば更新になる。
  - `BAKED` は焼いてあるか。`yes` は AMI から来たもの、`no` はこのインスタンスに入れて
    まだ焼いていないもので、焼く前にインスタンスが終了すると消える（`idle-shutdown` の
    自動終了も含む）。同じ版を入れ直しても、焼くまでは `no` になる。前の版の temt で
    入れたアプリは、`deploy` し直すまで `-` になる。
  - `DEPLOYED` は入れた時刻。
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
表示する。`temt-<svc> instance list`（`temt instance list --svc
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

インスタンスを終了せずに再起動する。対象のインスタンスの ID と状態を表示して
`[y/N]` で確認を求める。`--yes` を付けると確認しない。

再起動して、またログインできるようになるまで待つ。ふつう 1 分ほどかかり、SSM
エージェントの戻りを待っていること、戻ったことを 1 行ずつ表示する。見分けには、
インスタンスの中で起動のたびに変わる ID（boot_id）を Run Command で読んで比べるので、
手元の資格情報に `ssm:SendCommand` と `ssm:GetCommandInvocation` の権限が要る。
SSM エージェントが応答していない（`status` の `ssm-agent` が `Online` でない）と
見分けられないので、再起動せずに終わる。

`--async` を付けると、AWS が再起動の要求を受け付けるまでしか待たない。boot_id は
読まないので、SSM エージェントが応答していなくても再起動できる。

インスタンスが無いか、止まっている（`stopped`）ときは、`start` するよう促して
終わる。終了の途中（`shutting-down`）のものは無いものとして扱う。起動や停止の
途中（`pending`、`stopping`）でも、そのまま再起動を求める。AWS が断れば、その誤りを
表示し、`status` を見てやり直すよう促して終わる。`--async` を付けなければ先に SSM
エージェントを確かめ、boot_id を読むので、起動や停止の途中ではふつう再起動せずに、
`--async` を試すよう促して終わる。

インスタンスの中で `sudo poweroff` や `sudo shutdown -h` で電源を切ると、インスタンスは止まるのではなく終了する。止めて
続きから使いたいときは `stop` を使う（次の節）。

再起動すると、インスタンスの上で `idle-shutdown limit` コマンドで変えた時間は元に
戻る（[自動終了](apps/idle-shutdown.md)）。

### 止めて再開する

    temt-<svc> stop
    temt-<svc> start

`stop` は、インスタンスを終了せずに止める。ディスクの中身、インスタンス ID、ssh の
ホスト鍵はそのまま残り、次の `start` が新しく起動する代わりにそれを再開する。作業を
中断して、続きから使いたいときに使う。ふだんは、使い終わったら `terminate` する
（[終了する](#終了する)）。

`stop` は、対象のインスタンスの ID と状態を表示して `[y/N]` で確認を求め、止まる
（`stopped`）まで待つ。
`--yes` を付けると確認しない。`--async` を付けると、AWS が停止の要求を受け付ける
までしか待たない。インスタンスが無いときと、既に止まっているときは、そう表示して終わる。
終了の途中（`shutting-down`）のものは無いものとして扱う。停止の途中（`stopping`）
なら、停止を求めずに止まるのを待つ。起動の途中（`pending`）でも、そのまま停止を
求める。

再開した `start` も、ログインできるようになるまで待つ。`--async` を付けると、AWS が
再開の要求を受け付けるまでしか待たない。止まった直後など、AWS が再開や停止を断ることが
ある。そのときは、AWS の誤りを表示し、`status` を見てやり直すよう促して終わる。止めて
いるあいだについて:

- 起動テンプレートは使わずに再開するので、止めている間に設定ファイルを変えて
  `apply` しても、AMI やインスタンスタイプなどの変更は、そのインスタンスには効かない。
  効かせるには、`terminate` してから `start` する。受信規則とインスタンスロールの
  権限は効く（`SubnetId` を別の VPC のものに変えたときを除く。そのときは古い VPC の
  まま再開する）。
- 再起動と同じく、インスタンスの上で `idle-shutdown limit` コマンドで変えた時間は
  元に戻る。
- 止まっているインスタンスも、そのまま `bake` で焼ける（[AMI にする](#ami-にする)）。

`idle-shutdown` が電源を切ったときは、止まるのではなく終了する
（[自動終了](apps/idle-shutdown.md)）。再開したまま使わずにおくと、制限時間（既定は
1 時間）で終了し、続きから使うために残した中身も消える。

### 終了する

    temt-<svc> terminate

対象のインスタンスの ID と状態を表示して `[y/N]` で確認を求める。`y` と答えると、
インスタンスをルートボリュームごと終了し、known_hosts のファイルを消す。次に `start` すると、起動テンプレートの AMI から新しい
インスタンスが立ち上がる。残したいものがあれば、終了する代わりに `bake` で AMI に
する（[AMI にする](#ami-にする)）。入れてまだ焼いていないアプリは、終了すると消える。
どんな状態のインスタンスでも終了できるよう、`terminate` はインスタンスの中を見ないので、
そのことを警告しない。先に `status --apps` で確かめる。

`--yes` を付けると確認しない。標準入力が端末でないときは `--yes` が要る。

終了が完了するまで待つ。`--async` を付けると、AWS が終了の要求を受け付けるまでしか
待たない。どちらでも、known_hosts は片づける。インスタンスが
1 台も無くても、エラーにはならない。何かの手違いで同じサービスのインスタンスが 2 台
あるときは、両方とも終了する（1 台だけ残すときは
[インスタンスが 2 台になったとき](#インスタンスが-2-台になったとき)）。

インスタンス ID を指すと、そのインスタンスだけを終了する。確認、known_hosts の
片づけ、完了の待ち合わせも、指したものだけに行う。指した
ID がそのサービスのインスタンスに無ければ、見つかったものを表示し、何もせずに終わる。

    temt-<svc> terminate i-0fedcba9876543210

temt と一緒に配るアプリ `idle-shutdown` を入れて焼いておけば、インスタンスは
使われなくなってから制限時間（既定は 1 時間）で自分で終了する
（[自動終了](apps/idle-shutdown.md)）。
焼く前に自分で終了したときは警告が出ないので、入れたアプリを残したいときは、
離れる前に `status` の `apps` を見て焼いておく。

作った AWS リソース（インスタンスロール、インスタンスプロファイル、セキュリティ
グループ、起動テンプレート）はそのまま残る。これらには費用はかからない。
消すには `destroy` を使う（[片づける](#片づける)）。

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

インスタンスのルートボリュームの大きさは、設定ファイルの `[launch-template]` に
`BlockDeviceMappings` の要素を足し、その `Ebs.VolumeSize`（GiB）で決める。
`DeviceName` を省けば、AMI のルートボリュームを指す。書かなければ AMI と同じ大きさで、
素の Ubuntu なら 8 GiB である。書いて `apply` する。

    [[launch-template.BlockDeviceMappings]]
    Ebs.VolumeSize = 30

    temt-<svc> apply

広がるのは、次の `start` で起動テンプレートから起動したインスタンスである。Ubuntu
の AMI（とそこから焼いた AMI）では、起動するとインスタンスの中の cloud-init が
パーティションとファイルシステムを広げるので、`ssh` して手を動かす必要はない。

動いているインスタンスと止めてあるインスタンスは、`apply` しても広がらない。
中身を残すなら、焼いてから起動する。

    temt-<svc> bake
    temt-<svc> start

中身を残さなくてよければ、`terminate` してから `start` する。

EBS は縮められない。大きくして焼いた AMI は、そのあと `Ebs.VolumeSize` の行を消しても、
焼いたときの大きさのまま起動する。AMI のルートボリュームより小さい `Ebs.VolumeSize` は
書けず、`apply` が止まる。AMI の大きさは `template show` の `root volume` で
分かる。大きくして起動したインスタンスを、その大きさより小さい `Ebs.VolumeSize` に
直してから焼こうとすると、`bake` は焼かずに止まる。焼くと、次の `start` を AWS が
断る AMI ができるからである。`Ebs.VolumeSize` をインスタンスの大きさ以上に戻すか、
行を消すかし、どちらも `apply` してから焼く。

## AMI

### AMI にする

    temt-<svc> bake

インスタンスのディスクの中身を AMI にし、次の `start` からその AMI で起動する
ようにする。止まって
いる（`stopped`）インスタンスも、そのまま焼ける。

対象のインスタンスの ID と状態を表示し、何をするかを示して `[y/N]` で確認を求める。
`y` と答えると、次を順に行う。

1. インスタンスが動いていれば停止する。
2. 止まったインスタンスのルートボリュームから AMI を作る。ほかの EBS は AMI に
   入れない。名前は `temt-<svc>-<YYMMDDHHMM>`（時刻は手元の時刻）で、
   `temt:service=<svc>` のタグが付く。インスタンスの中には手を加えない。
3. AMI が使える状態（`available`）になるまで待つ。ディスクの大きさによって、数分から
   それ以上かかる。締め切りは無い。
4. そのリージョンの起動テンプレートの `ImageId` を新しい AMI に替える。設定ファイルには
   触らない。入れたアプリは、`deploy` の記録と一緒に AMI に入るので、次のインスタンス
   では `status --apps` の `BAKED` が `yes` になる。
5. インスタンスを `terminate` と同じように終了し、終了が完了するまで待つ。

`--yes` を付けると確認しない。標準入力が端末でないときは `--yes` が要る。`bake` は
つねに完了を待つので、`--async` は無い。

設定ファイルを直して `apply` をまだ実行していないときは、焼かずに、先に
`apply` を実行するよう促して終わる。インスタンスが無いとき、停止や
終了の途中（`stopping`、`shutting-down`）や起動の途中（`pending`）のとき、2 台以上
あるとき、インスタンスのルートボリュームが `Ebs.VolumeSize` より大きいとき
（[ディスクを広げる](#ディスクを広げる)）も、焼かずにその旨を
表示して終わる。設定ファイルに `ImageId` を書いていれば、AMI はそれに固定されて
いるので、何もせずに引数の誤りで止まる（[使う AMI を選ぶ](#使う-ami-を選ぶ)）。

替わるのは、焼いたサービスのそのリージョンの起動テンプレートだけである。ほかの
リージョンのクローンや、設定ファイルをコピーした別のサービスが同じ AMI を使っていても、
そちらは変わらない。新しい AMI を使うなら、そちらで `apply --image-id` を実行する。
古い AMI は消えずに残る。消すには `ami prune` か `ami delete` を使う（次の節）。

AMI が作れなかったときは、AWS が返した理由を表示して終わる。起動テンプレートは変わっていない。インスタンスは止まったまま残る
ので、`start` で再開して使い続けられる。同じサービスを同じ分のうちに 2 回焼くと、AMI の名前が重なって失敗する。1 分おいてやり直す。

途中で止まったとき（Ctrl-C、締め切り、AWS のエラー）は、どこまで進んだかで
直し方が変わる。

- AMI ができる前: インスタンスが止まっていることがある。もう一度 `bake` を実行
  する。AMI を作る最中に止めたときは、AMI ができていることがある。
- AMI を作った後、`available` になるのを待つ間: 何も変わっておらず、インスタンスは
  止まったまま残る。作った AMI の ID と名前が表示されている。`ami list` で AMI が
  `available` になったのを確かめたら、もう一度 `bake` を実行する。AMI がもう 1 つ
  できるが、余分な方は `ami prune` が消す。または、その名前を `--image-id` に付けて
  `apply` を実行し、`terminate` を実行する。`failed` になっていれば、作れなかったときと
  同じである。
- 起動テンプレートを替えた後: `terminate` を実行する。入れたアプリは AMI に入っている。

起動テンプレートが新しい AMI を指す前に止まったときは、どのテンプレートからも
指されていないので、`ami prune` を打つと新しい AMI は消える。その AMI を使うなら、
`ami prune` より先に `apply --image-id` で起動テンプレートに入れる。もう一度 `bake`
するなら、消えても困らない。

### 使う AMI を選ぶ

ふだんは、設定ファイルに `ImageId` を書かない。使う AMI は起動テンプレートが覚えて
いて、`bake` がそれを新しい AMI に替えていく。

起動テンプレートを作る `apply`（はじめての `apply` と、`destroy` のあとの `apply`）は、
名前が `temt-<svc>-<時刻>` の自分の AMI のうち作成日時が最新のものを選び、無ければ
Ubuntu 24.04 の公開 AMI を選ぶ。`diff` と `apply` は、選んだ AMI の名前を表示する。

`--image-id` を付けると、起動テンプレートがあっても、指した AMI に替える。前の AMI に
戻すときや、ほかのサービスの AMI から始めるときに使う。`diff` に付ければ、替えたときの
違いを先に見られる。

    temt-<svc> diff --image-id temt-<svc>-2609201200
    temt-<svc> apply --image-id temt-<svc>-2609201200

設定ファイルに `ImageId`（AMI の ID か名前）を書くと、AMI はそれに固定される。
`--image-id` は使えず、AMI を焼くコマンド（`bake`、`set-port`、`deploy --bake`、
`app remove --bake`）は何もせずに引数の誤りで止まる。焼くなら `ImageId` の行を消す。
共通ファイルに書けば、すべてのサービスが固定される（[conf.md](conf.md#imageid)）。

### AMI を見る、片づける

    temt-<svc> ami list
    temt ami list
    temt-<svc> ami prune
    temt ami prune
    temt ami delete <AMI の ID または名前>

`ami list` は temt の AMI（`temt:service` タグを持つ AMI）の一覧を表示する。
`temt-<svc> ami list`（`temt ami list --svc <svc>` と同じ）はそのサービスの
AMI を、`temt ami list` はすべてのサービスの AMI を表示する。
`--svc` を付けないときの環境の選び方は、[共通のコマンドラインオプション](#共通のコマンドラインオプション)にある。

    account 123456789012, region ap-northeast-1, profile (none)
    ID                      NAME                    STATE       CREATED               USED
    ami-0123456789abcdef0   temt-<svc>-2609011200   available   2026-09-01 12:00:00   -
    ami-0fedcba9876543210   temt-<svc>-2609201200   available   2026-09-20 12:00:00   temt-<svc>-template:3

`USED` は、その AMI を指している起動テンプレートのバージョン
（`<テンプレート名>:<バージョン>`、複数あれば `,` でつなぐ）で、無ければ `-` と
出る。`--format` で、`temt:service` の値（`SERVICE`）も表示できる。`ami prune` は、
`USED` が `-` の AMI のうち `bake` と `service import` が作ったもの（状態が `available` か
`failed`）の登録を解除し、スナップショットも消す。消すものだけを表示し、`[y/N]` で確認を
求める。`--yes` を付けると確認しない。他のコマンド（`bake` など）が
そのサービスを扱っていたときと、消す直前に起動テンプレートがその AMI を指して
いたときは、その AMI を消さずに残りを続け、最後に残したものをまとめて表示して、
エラーで終わる。

`ami delete` は、ID か名前で指した temt の AMI 1 つの登録を解除し、スナップショットも
消す。探すのは自分のアカウントの AMI だけである。`ami prune` と違い、`bake` が
作った名前でなくても、`temt:service` タグがあれば消せる。起動テンプレートの
バージョンが指している AMI と、`pending`（`bake` の途中）の AMI は、`--force` を
付けないと消さずにエラーにする。`resolve:ssm:` のバージョンを解決できないときも、
そのバージョンが指しているかもしれないので、同じく `--force` が要る。消す前に
`[y/N]` で確認を求め、`--yes` を付けると確認しない。

`temt-<svc> ami prune`（`temt ami prune --svc <svc>` と同じ）は、そのサービスの
AMI だけを消す候補にする。指されているかは、他のサービスのテンプレートも見て
確かめる。`--svc` を付けない `ami prune` は、どのサービスの AMI かを問わない。
`destroy` したサービスの AMI は、テンプレートが消えているので、他の
テンプレートが指していない限り消える。`destroy` したサービスの AMI を、次の
`apply` のために残しておきたいときは、片づけるサービスごとに `--svc` を付ける。
`ami delete` も `--svc` を付ければ、そのサービスの AMI でないものは
見つからないとして消さない。`ami prune` も `ami delete` も、設定ファイルの
`ImageId` は見ない。`ImageId` に書いた AMI を消すと、次の `apply` が止まるので、
`ImageId` を書き直すか、行を消して `--image-id` で選ぶ。起動テンプレートが指す AMI を `--force` で消したときも
`apply` が止まるので、`--image-id` で選び直す。

起動テンプレートの `ImageId` に SSM パラメータ（`resolve:ssm:`）を書いたものは、
AWS がパラメータから引いた AMI を指すものとして扱う。パラメータが無いなどで
引けなければ、`ami list` はそのバージョンを表示して警告し、`ami prune` は何も
消さずに止まる。`ami delete` も、`--force` が無ければ止まる。そのバージョンを
直すか消してから、もう一度実行する。

## 起動テンプレート

    temt-<svc> template show

`start` がインスタンスを起こすのに使う起動テンプレートを表示する。
次の `apply` が何を変えるかは、`diff` で分かる（[設定を変える](#設定を変える)）。

    launch template temt-<svc>-template:
      version: 3 (created 2026-09-20 18:00:00)
      [launch-template]
      ImageId = "ami-0123456789abcdef0"
      InstanceType = "t3.micro"
      InstanceInitiatedShutdownBehavior = "terminate"
      IamInstanceProfile = { Name = "temt-<svc>" }

      [[launch-template.NetworkInterfaces]]
      DeviceIndex = 0
      SubnetId = "subnet-0123456789abcdef0"  # vpc-0123456789abcdef0
      AssociatePublicIpAddress = true
      Groups = ["sg-0123456789abcdef0"]

      [launch-template.MetadataOptions]
      HttpTokens = "required"
      InstanceMetadataTags = "enabled"

      [launch-template.CreditSpecification]
      CpuCredits = "unlimited"

      [[launch-template.TagSpecifications]]
      ResourceType = "instance"
      Tags = [{ Key = "Name", Value = "temt-<svc>" }, { Key = "temt:service", Value = "<svc>" }]
      ...
    image ami-0123456789abcdef0:
      name:        temt-<svc>-2609201800
      created:     2026-09-20 18:00:00
      state:       available
      root volume: 8 GiB
      latest:      temt-<svc>-2609211800 (ami-0fedcba9876543210) created 2026-09-21 18:00:00, newer
    versions (* is the default):
      3 * 2026-09-20 18:00:00 ami-0123456789abcdef0 t3.micro subnet-0123456789abcdef0

- 1 つめは、既定バージョンの中身。temt が足したセキュリティグループやタグも含めて、
  起動テンプレートにあるものを、設定ファイルの `[launch-template]` と同じ形ですべて
  表示するので、そのまま設定ファイルにコピーできる。`SubnetId` には、そのサブネットが属する
  VPC を添える。
- 2 つめは、`ImageId` が指す AMI。`root volume` はそのルートボリュームの大きさで、
  `Ebs.VolumeSize` はこれより小さくできない。EBS のルートボリュームが無い AMI では
  `(no EBS root volume)` と出て、`Ebs.VolumeSize` は書けない。登録が解除されていれば
  その旨を表示する。
  `latest` は、名前が `temt-<svc>-<時刻>` の自分の AMI のうち作成日時が最新のもので、
  テンプレートのものより新しければ `newer` と添える。他のサービスの AMI を指していても、
  `latest` はこのサービスから焼いた AMI から選ぶ。新しいものに替えるには、
  `apply --image-id` を実行する（[使う AMI を選ぶ](#使う-ami-を選ぶ)）。
- 3 つめは、バージョンの一覧。`*` が既定バージョンである。`apply` は既定以外の
  バージョンを消すので、ふつうは 1 行だけになる。

## セキュリティグループ

temt が作るセキュリティグループの受信規則は、設定ファイルに書く。`sg` のコマンドで、
設定ファイルの規則を足し引きし、AWS との違いを確かめられる。取り込んだサービスの
ように、temt のセキュリティグループを使っていないサービスでは、受信規則を書くと
`apply` がエラーにする（[既存のインスタンスを取り込む](#既存のインスタンスを取り込む)）。

    temt-<svc> sg add 22 --description home

CIDR を省くと、temt がグローバル IP アドレスを調べ、取れたアドレスごとに、IPv4 なら
`/32`、IPv6 なら `/64` の規則を足す。問い合わせ先は、IPv4 が `https://checkip.amazonaws.com`、
IPv6 が `https://api6.ipify.org` である。IPv6 のアドレスは、手元に IPv6 の
経路があるときだけ取れる。どちらも使った CIDR が表示される。
[直接接続](#直接接続)はインスタンスのパブリック IPv4 アドレスへつなぐので、
ssh に効くのは IPv4 の規則である。CIDR を自分で決めるなら、`sg add 22 198.51.100.0/24` のように書く。
ポートは `8000-8010` のように範囲でも書け、`53/udp` と付ければ udp、`icmp` と
書けば ICMP（ping など）になる。

`sg add` が書き換えるのは設定ファイルだけである。次の `apply` が何を変えるかを
確かめてから、`apply` で AWS に反映する。

    temt-<svc> sg diff
    temt-<svc> apply

`apply` を実行すると、セキュリティグループの受信規則が設定ファイルと共通ファイルの
規則になる。手で足した規則は消えるので、許したい
ものはすべてどちらかのファイルに書く。規則は `sg list` で見られ、手で直してもよい。
書き方は [conf.md](conf.md#security-group) にある。共通ファイルの規則は全サービスに
効き、`sg add` と `sg remove` では変えられないので、共通ファイルを直す。

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
代わりに、セキュリティグループの受信規則と、sshd のポート（既定は 22）で
外に出られるネットワークが要る。使えない環境でも自動では切り替わらない。

まず、自分のグローバル IP アドレスから sshd のポートへの受信規則を、設定ファイルに足して
`apply` する（[セキュリティグループ](#セキュリティグループ)）。`set-port` でポートを
変えているなら（[sshd のポートを変える](#sshd-のポートを変える)）、22 の代わりに
そのポートを、複数ならポートごとに足す。

    temt-<svc> sg add 22 --description home
    temt-<svc> apply

次に、環境変数 `TEMT_LOGIN` を `direct` にする。ssh の設定は変わらず、次の ssh
から直接つなぐ。ssh は ProxyCommand に自分の環境変数を渡すので、シェルの初期化で
`export TEMT_LOGIN=direct` としておくことも、`TEMT_LOGIN=direct ssh
temt-<svc>` のようにその接続だけ変えることもできる。

グローバル IP アドレスは変わることがある。つながらなくなったら、`sg list` で古い規則の
CIDR を確かめて、IPv6 のものも含めてすべて消し、新しいアドレスで足し直してから
`apply` を実行する。共通ファイルの規則なら、共通ファイルを直してから、各サービスで
`apply` を実行する。

    temt-<svc> sg remove 22 203.0.113.10/32
    temt-<svc> sg add 22 --description home
    temt-<svc> apply

Session Manager 経由に戻すには、`TEMT_LOGIN` を消すか `ssm` にする。受信規則は
残るので、消したければ `sg remove` で設定ファイルから外して `apply` を実行する。

## ログインの鍵

temt は EC2 にキーペアを登録しない。ssh がつなぐたびに、temt が ssh の使う鍵の公開鍵を
EC2 Instance Connect でインスタンスに送る。送った鍵が認められるのは 60 秒だけで、
ssh はその間にログインする。AWS に鍵を残さない。

初めてつなぐインスタンスでは、ssh がホスト鍵の確認を求める。答えるまでに 60 秒を
超えると、送った鍵が切れていて `Permission denied (publickey)` で止まる。もう一度
つなげば入れる。

使う鍵は、`~/.ssh/temt/config` の `Host temt-<svc>` ブロックの `IdentityFile` で
決まる。`apply` は、ブロックに `IdentityFile` が無いときだけ、既定の鍵
`~/.ssh/temt/keys/temt` を書く。この鍵はどのサービスにも共通で、無ければ `apply` が
作る。自分の鍵を使うには、`IdentityFile` を書き換える。`apply` はそれを書き換えない。

送るのは、鍵の隣にある `.pub` である。`.pub` があれば、パスフレーズ付きの
鍵も使える。秘密鍵を ssh-agent に置き、`IdentityFile` に `.pub` を書いたときは、
その `.pub` を送る。

インスタンスの AMI には `ec2-instance-connect` が入っている必要がある。既定の
Ubuntu の AMI には入っている。入っていない AMI では、ssh が
`Permission denied (publickey)` で止まる。

インスタンスの `~/.ssh/authorized_keys` にある鍵でも、そのまま入れる。ただし、その鍵でも
隣に `.pub` があり、鍵を送れる必要がある。送る鍵は、`authorized_keys` には書かれない。

## アプリ

### アプリを入れる

アプリは、インスタンスの中で動くものを 1 つのディレクトリにまとめたものである。
作り方は [apps.md](apps.md) にある。

`deploy` はインスタンスロールに触らない。アプリが AWS を使うなら、要る権限を設定ファイルに
書き、`deploy` より先に `apply` する（[apps.md](apps.md#aws-を使う)）。

動いているインスタンスに入れ、確かめてから AMI に焼く。

    temt-<svc> start
    temt-<svc> deploy ./jupyter     # / を含めばパス。含まなければアプリ ID
    temt-<svc> bake                 # 焼くまで次のインスタンスには入らない

`deploy --bake` とすれば、入れ終えたあとに続けて焼く。確認は始める前に出る。
アプリ ID で指すと、`~/.config/temt/apps/<アプリID>/` を探し、無ければ temt と
一緒に配るアプリを使う。同じアプリ ID で置けば、temt と一緒に配るアプリの代わりに
自分のものが使われる。
入れたアプリは、インスタンスの `/opt/temt/apps/<アプリID>/` に、`deploy` の記録
`temt-deploy.json`（入れた時刻、入れた先のインスタンス、ユーザー、与えたオプション）と
一緒に置かれる。焼けば、記録ごと AMI に入る。入っているアプリは `status --apps` で
見られる（[状態を見る](#状態を見る)）。途中で失敗しても巻き戻さないので、直してもう一度
`deploy` する。

`app list` は、アプリ ID で指せる手元のアプリを表示する。AWS には問い合わせない。

    ID               NAME             LOCAL   PATH
    emacs            Emacs            2       ~/.config/temt/apps/emacs
    idle-shutdown    idle-shutdown    2       ~/.local/share/uv/tools/temt/lib/python3.14/site-packages/temt/apps/idle-shutdown
    jupyter          Jupyter Lab      1.2     ~/.config/temt/apps/jupyter

- `LOCAL` は `manifest.toml` の版である。
- `PATH` はアプリのディレクトリで、temt と一緒に配るアプリは temt の中を指す。
- `--format` で `DESCRIPTION`（`manifest.toml` の説明）も表示できる。

    temt app list --format id,local,description

`install` は、アプリの `manifest.toml` の `user` が指すユーザーで実行され、書いて
いなければ root で実行される。`--user` を付ければ、`manifest.toml` の `user` より
優先して、そのユーザーで実行する。入れたあとにユーザーを変えるには、いったん外してから
入れ直す。

    temt-<svc> deploy ./jupyter --user ubuntu

アプリがオプションを宣言していれば（[apps.md](apps.md#オプション)）、アプリの後ろに
`<名前>=<値>` を並べて値を与える。
宣言していない名前や、アプリが受け付けない値は、何も変えずに止まる。宣言している
オプションとその既定は、`app show` で見られる。

    temt-<svc> deploy hello port=8080
    temt-<svc> deploy ./hello port=8080 motd=on

`deploy` は、使う値を 1 つ 1 行で、出どころとともに表示する。`given` はコマンドラインで
与えた値、`kept` は前の `deploy` で与えて、インスタンスの記録に残った値、`default` は
アプリの既定である。

    10:20:15 option port=8080 (given)
    10:20:15 option motd=off (default)

与えた値は記録に残り、次の `deploy` は、与えなかったオプションにその値を引き継ぐ。
版を上げるときも、値を与え直さなくてよい。`<名前>=` と値を空にすれば、その
オプションを既定に戻す。`--reset` を付ければ、前の値を何も引き継がず、与えたもの
だけを使う。AMI から来たアプリは、焼いたときの値を引き継ぐ。`app remove` で外して
から入れ直すときは、既定から始まる。

    temt-<svc> deploy hello              # 前の port=8080 のまま入れ直す
    temt-<svc> deploy hello port=        # port を既定に戻す
    temt-<svc> deploy hello --reset      # どのオプションも既定に戻す

`install` が失敗しても、記録は新しいファイルと一緒に置かれるので、やり直すときに
同じオプションを付け直さなくてよい。記録の中身が壊れていて前の値が読めないときは、
何も変えずに止まるので、`--reset` を付けて値を与え直す。

記録した値は `app show` で見られる。`status --apps` と同じく、動いているインスタンスから
Run Command で記録を読み、読めなければ理由を表示する。記録はインスタンスの上で誰でも
読め、AMI にも残るので、オプションには秘密の値を渡さない。

    temt-<svc> app show hello

    app hello on i-0123456789abcdef0:
      name:     hello
      version:  2
      user:     root
      deployed: 2026-10-01 10:40:20
      baked:    no
      options:  port=8080
    options in i-0123456789abcdef0:/opt/temt/apps/hello (version 2):
      port  8000  the port to listen on in the instance
      motd  off   show the URL in the login message: on or off
    keys: none

`app remove <アプリID>` は、`uninstall` を実行してディレクトリを記録ごと消し、状態
（キー `status`）を消す。焼くまで AMI には残るので、焼かずにインスタンスが終了すると、
次のインスタンスで AMI の版に戻る。アプリの値は残るので、要らなければ `app unset` で
消す。`uninstall` は、記録にある、`install` を実行したユーザーで実行される。`--user` で
変えられる。

`deploy` と `app remove` はインスタンスに ssh でつなぐ。この ssh も、`--profile` と
`--region` で選んだ AWS のプロファイルとリージョンを使う。別のインスタンスにつながった
ときは、何も変えずに止まる。Ctrl-C で止めても、インスタンスの上の `install` や
`uninstall` は走り続けることがある。

### アプリの値

インスタンスの中で動くアプリは、設定や状態を SSM パラメータ
`/temt-<svc>/<アプリID>/<キー>` に置く。手元からは `app` のコマンドで扱う。
`apply` していないサービスでは、どれも `apply` を促して止まる。

    temt-<svc> app show jupyter            # 記録、宣言したオプション、キーと更新時刻の一覧。値は表示しない
    temt-<svc> app get jupyter token       # 値をそのまま標準出力に書く。改行は足さない
    temt-<svc> app set jupyter limit=2h    # 値を書く
    temt-<svc> app unset jupyter limit     # 値を消す。確認は取らない

アプリ ID とキーは、英小文字で始まり、英小文字・数字・ハイフンが続き、英小文字か
数字で終わる 64 文字以内の名前である。`set` は、アプリが入っているかを確かめない。
アプリ ID を打ち間違えた値は `app show` のキーの一覧に出るので、`unset` で消す。

値は、暗号化されない `String` のパラメータとして書かれる。同じアカウントの資格情報を
持つ者と、パラメータを読める権限を持つ同じアカウントのインスタンスからは読める。
temt のインスタンスは、雛形のままでは自分の
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
セキュリティグループに受信規則が無くても、sshd のポート（既定は 22）で外に出られない
ネットワーク（https しか通さないファイアウォールの内側など）からでも入れる。

インスタンスは `start` で新しく起動するたびに新しいホスト鍵を持つので、`ssh` は
そのつどホスト鍵の確認を求める。`reboot` で再起動しても、止めて再開しても、ホスト鍵は
変わらない。フィンガープリントを受け入れると、そのインスタンス専用の
known_hosts に記録される。

### sshd のポートを変える

    temt-<svc> ssh set-port 2222

インスタンスの sshd が待ち受けるポートは、`ssh set-port` で変える。空白で区切って
複数のポートを書ける（`ssh set-port 2222 443`）。

`set-port` は、何をするかを表示して `[y/N]` で確認を求めてから、次を行う。
`--yes` を付けると確認しない。

1. インスタンスが無ければ起動し、止まっていれば再開する。
2. SSM の Run Command で、インスタンスの sshd の設定を書き換えて、sshd を再起動する。
   ssh を経由しないので、つながらないポートにしてしまっても、もう一度 `set-port` で
   直せる。
3. 焼く（[AMI にする](#ami-にする)）。インスタンスは終了し、まだ焼いていない
   アプリも一緒に焼かれる。ポートが今と同じでも焼く。
4. ssh の設定の `Host temt-<svc>` ブロックの `Port` を、先頭のポートに書き換える。
   ほかのポートは `ssh -p 443 temt-<svc>` のように指す。

直接接続で使うなら、セキュリティグループの受信規則も新しいポートに合わせる
（[直接接続](#直接接続)）。`set-port` は終わったときに、`sg` での変え方を表示する。
temt は受信規則を変えない。

    temt-<svc> sg add 2222 --description home
    temt-<svc> sg remove 22
    temt-<svc> apply

**失敗したとき。** `set-port` は元に戻さず、焼かず、`Port` も書き換えずに、
インスタンスを動かしたまま終わる。スクリプトの出力が表示される。

- sshd を再起動する前に失敗したとき（sshd が設定を受け付けなかった、ほかのファイルにも
  `Port` 行がある）は、sshd は前のポートで動いている。`ssh -p <前のポート> temt-<svc>`
  で入って直す。直すまで、`reboot`、`stop`、`bake` はしない。取り込んだ
  インスタンスで `/etc/ssh/sshd_config` に `Port` 行があるときも、ここで失敗する。
- sshd を再起動した後に失敗したとき（新しいポートで待ち受けられない）は、どのポートでも
  待ち受けていないことがある。`aws ssm start-session --target <インスタンスID>` で
  シェルを開いて直すか、`terminate` してから `start` する（焼いていない分は失う）。

直したら、もう一度 `set-port` を実行する。

**`Port` と実際のポートが違うとき。** Host ブロックの `Port` は、起動テンプレートの
AMI を替えたとき（`--image-id` で古い AMI や他のサービスの AMI にしたとき、
`destroy` のあとの `apply` が Ubuntu 24.04 を選んだときなど）と、`set-port` が
失敗したとき、ほかのリージョンのクローンで `set-port` していないときに、インスタンスの
実際のポートと違ってしまう。つながらなければ、`~/.ssh/temt/config` の Host ブロックの `Port` を手で
直す（`deploy` と `app remove` もこれに従う）。その場だけなら `ssh -p <ポート>
temt-<svc>` でつなぐ。直接接続では、つながらなかったポートと、`Port` が違うかも
しれないことが表示される。Session Manager 経由では、session-manager-plugin が、
宛先のポートにつなげなかったと表示して終わる。

### `~/.ssh/temt/` の中身

```
config                    ssh の設定。apply がサービスの Host ブロックを足し、更新する
keys/                     ログインの鍵。apply が作った temt と temt.pub
known_hosts.d/<名前>/     インスタンスごとの known_hosts
```

サービスに ssh の設定を足したいとき（`LocalForward` など）は、`config` の
`Host temt-<svc>` ブロックに書き足す。`apply` は、そのサービスのブロックの
中の temt が管理する項目（`UserKnownHostsFile`、`ProxyCommand`）だけを書き換える。
`User`（`ubuntu`）、`Port`（`22`）、`IdentityFile` は、無いときだけ書く。他の項目、
他のブロック、コメントには触れない。`Host temt-<svc> <別名>` のように Host 行に別名を並べてもよく、`apply` は
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
止まる。

`TEMT_SSH_DIR` を変えたら、`apply` をやり直す。新しい場所に鍵が無ければ `apply` が
作り、そのまま使える。`apply` で、ssh の設定に書かれるパスが新しい場所に変わり、
`~/.ssh/config` の `Include` も書き換わる。ただし、前の場所の `config` を手で移した
ときは、Host ブロックの `IdentityFile` が前の場所を指したまま残る（`apply` は
`IdentityFile` を書き換えない）。その行を消してから `apply` すれば、新しい場所の鍵に
なる。前の場所にある Host には
ssh でつながらなくなるので、`apply` はそれらを表示して警告する。temt が鍵や
known_hosts を動かすことはないので、要るものは手で移す。

どちらの場所も ProxyCommand に書かれるので、環境変数の無いシェルからでも、別の
値を設定したシェルからでも、`ssh temt-<svc>` は `apply` のときと同じ場所を使う。
