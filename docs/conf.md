# temt の設定ファイル

`~/.config/temt/<svc>.toml`。サービスの定義で、`--svc <svc>` を付けたすべての
コマンドが読む。`--svc` を付けない `service list` は、すべての `<svc>.toml` を
読んで並べる。`--svc` を付けない `instance list`、`ami list`、`ami prune`、
`ami delete`、`app list` と、`keypair` のコマンドは読まない。`apply` はこの
ファイルを正として、AWS リソースと手元の環境を揃える。複数のマシンから同じサービスを
使うときは、同じ内容のファイルを各マシンに置く。ssh の経路はマシンごとに違うので、
このファイルではなく環境変数で与える（[設定ファイルに
書かないもの](#設定ファイルに書かないもの)）。AWS のプロファイルは書けるが、
マシンごとに違うなら書かずに、各マシンで環境変数 `AWS_PROFILE` を設定する。場所は環境変数 `TEMT_CONF_DIR` で
変えられる（[場所を変える環境変数](#場所を変える環境変数)）。

## 形式

TOML 1.0.0。リソースをまたぐ項目は最上位に、リソースごとの項目はそのリソース
のテーブルに置く。テーブル名は aws-cli のリソース名（`launch-template` など）、
テーブルの中の項目名は AWS の API か aws-cli の項目名（`InstanceType`、`Cidr` など）に
合わせる。項目の値は文字列を原則とし、`SshdPorts` が整数の配列、`VolumeSize` が整数、
`[security-group]` の `Port` が整数か整数の配列である。`[role]` のポリシーは例外で、
IAM の Statement の書式を TOML で書く（[`[role]`](#role)）。

下の表にない項目やテーブル、値の型が表と違う項目、テーブルを書くべき名前に別の型の値を書いたもの、
TOML として読めない記述があれば、エラーにして終了する。表にある項目が無ければ既定を
使う。文字列の項目で値が空文字列のものと、`SshdPorts` と `[security-group]` の
`Port` で値が空の配列のものは、書かれていないものとして扱う。

## 項目

### 最上位

表の「読むコマンド」は、その項目の値を読むコマンドである。このほか `service list` が、
表示のためにすべての項目を読む。

| 項目 | 読むコマンド | 既定 |
| --- | --- | --- |
| `AwsProfile` | `service list`、`sg list`、`sg add`、`sg remove` を除く、`--svc` を付けたすべて | 無し。書かなければプロファイルを指定せず、資格情報は AWS の既定の規則で決まる |
| `AwsRegion` | `service list`、`sg list`、`sg add`、`sg remove` を除く、`--svc` を付けたすべて | `ap-northeast-1` |
| `KeyName` | `apply`、`diff`、`bake`、`--bake` を付けた `deploy` と `app remove`、`destroy` | `temt`。EC2 のキーペアの名前。`IdentityFile` の既定と、起動テンプレートの `KeyName` になる。英数字で始まり、英数字、`.`、`_`、`-` だけが続き、`.pub` で終わらない、251 文字以内の名前 |
| `IdentityFile` | `apply`、`diff`、`destroy` | `~/.ssh/temt/keys/<KeyName>`（`TEMT_SSH_DIR` を設定していれば、その下の `keys/<KeyName>`）。ssh がインスタンスへの認証に使う秘密鍵。`apply` が ssh の設定に書く。`~/` で始まるか、絶対パスで書き、ファイル名で終える。空白（半角スペース）と `%` は使えるが、`"`、`'`、`\`、`$`、表示できない文字（タブ、改行、全角空白、制御文字など）は使えない |
| `SshdPorts` | `apply`、`diff` | `[22]`。インスタンスの sshd が待ち受けるポートの配列（`[2222]`、`[2222, 443]`）。1 から 65535 の整数で、重ねて書けない。先頭が ssh の Host ブロックの `Port` になる |

**`AwsProfile`。** 書かなければ、temt はプロファイルを指定しない。資格情報は AWS の
既定の規則（環境変数 `AWS_PROFILE` や `AWS_DEFAULT_PROFILE`、環境変数の鍵、
`~/.aws/` の `[default]` プロファイルなど）で決まる。プロファイルを固定するには
次の 3 つの方法がある。

- `--profile <プロファイル>` を付ける（`temt apply --svc <svc> --profile work`）。
  `apply` はその値を `AwsProfile` に書くので、次からは付けなくてよい。`apply` 以外の
  コマンドでは、その実行のあいだだけ効く。
- `AwsProfile = "work"` と書く。
- 環境変数 `AWS_PROFILE` を設定する。`AWS_DEFAULT_PROFILE` と両方に別の値を
  入れると、temt は `AWS_DEFAULT_PROFILE` を使う。aws-cli v2 を単独で使うときは
  `AWS_PROFILE` が先なので、両方を使うなら同じ値にする。

固定をやめるときは、`AwsProfile` の行を消すか、値を空文字列にする。`--profile` を
付けずに `apply` を実行しても、この項目が書き直されることはない。複数のマシンで
同じファイルを使うときは、各マシンでプロファイル名を揃える。揃えられないなら
`AwsProfile` を書かずに、各マシンで `AWS_PROFILE` を設定する。ssh は環境変数を
ProxyCommand に渡すので、ログインにも効く。`--profile` は ssh の ProxyCommand には
届かず、`apply` に付けると `AwsProfile` に書かれるので、この使い方には向かない。

**`AwsRegion`。** 無ければ `ap-northeast-1` になり、最初の `apply` がその値を書く。
どのリージョンを使うかはこのファイルだけで決まり、`--svc` を付けたコマンドは、環境
変数 `AWS_REGION` と `AWS_DEFAULT_REGION` を見ない。1 つのファイルは 1 つの
インスタンスに対応するので、別のリージョンを使うときは `AwsRegion` を書いてから
最初の `apply` を実行するか、別の `<svc>` として立てる。

**`KeyName` と `IdentityFile`。** インスタンスにログインできる鍵は、EC2 のキーペア
`KeyName` の鍵だけである。`IdentityFile` はそれと同じ鍵でなければならず、
パスフレーズ付きの鍵は使えない。`apply` が確かめる手順は
[usage.md](usage.md#キーペア) にある。`KeyName` は `IdentityFile` の既定の
ファイル名になるので、`/` などは使えない。

`IdentityFile` は、`~/` で始まるか、絶対パスで書く。相対パスは、temt と ssh で
起点のディレクトリが違うので使えない。`~user/` の形も、展開しないので使えない。
末尾の `/` や、`.`、`..` で終わる形も使えない。temt はそれを畳んで鍵のファイルと
読むが、ssh は書かれたまま開いて読めないからである。空白（半角スペース）と `%` は
使える。`apply` は ssh の設定に、`%` を `%%` にし、空白を含めば値を二重引用符で
囲んで書く。ssh は、二重引用符の中でも引用符と `\` を自前で解釈し、`${NAME}` を
環境変数として展開するので、`"`、`'`、`\`、`$` は使えない。表示できない文字
（タブ、改行、全角空白、制御文字、表示の向きを変える文字など）も、ssh の設定の
行を壊すか、端末の表示を乱すので使えない。temt が鍵を探したり作ったりするときは、
書いたパスをそのまま使う。

`KeyName` と `IdentityFile` が規則に合わなければ、`--svc` を付けたすべてのコマンドが、
設定ファイルを読むところでエラーにする。前から持っているキーペアの名前が規則に
合わなければ、`keypair create` で新しいキーペアを作って使う。

**`SshdPorts`。** インスタンスの sshd は、ここに書いたポートだけで待ち受ける。
`apply` が、State Manager の関連付けを通してインスタンスの sshd の設定を揃え、
ssh の Host ブロックの `Port` に先頭のポートを書く
（[usage.md](usage.md#sshd-のポートを変える)）。動いているインスタンスにも、
`apply` の数秒後に効く。この値と違うポートで焼いた AMI（素の Ubuntu を含む）から
起動すると、起動のたびに AMI のポートから切り替わり、ログインの
メッセージがそのことを告げる。変えたら `apply` して、つながることを確かめ、
`bake` する。直接接続（`TEMT_LOGIN=direct`）で使うなら、`[security-group]` の
受信規則も同じポートに合わせる。temt は合わせない。セキュリティグループは
インスタンスの外の仕組みなので、Session Manager 経由のログインには関わらない。
2 つ目以降のポートは `ssh -p <ポート> temt-<svc>` で指す。

### `[launch-template]`

起動テンプレートの項目。変えたら `apply` を実行する。動いているインスタンスは
変わらず、次の `start` から反映される。

| 項目 | 読むコマンド | 既定 |
| --- | --- | --- |
| `ImageId` | `apply`、`diff`、`bake`、`--bake` を付けた `deploy` と `app remove` | このサービスから焼いた（`temt:service=<svc>` のタグを持つ）`available` な AMI のうち、`CreationDate` が最も新しいもの。無ければ Canonical の Ubuntu 24.04 |
| `InstanceType` | `apply`、`diff`、`bake`、`--bake` を付けた `deploy` と `app remove` | `t3.micro` |
| `SubnetId` | `apply`、`diff`、`bake`、`--bake` を付けた `deploy` と `app remove`、`sg diff`、`sg show` | 既定 VPC のサブネットのうち、`MapPublicIpOnLaunch` が true で、アベイラビリティゾーン名が辞書順で最も小さいもの |
| `VolumeSize` | `apply`、`diff`、`bake`、`--bake` を付けた `deploy` と `app remove` | 無し（AMI のルートボリュームと同じ大きさで起動する）。インスタンスのルートボリュームの大きさ（GiB）。1 以上の整数。上限はボリュームの種類で違うので temt は確かめず、超えれば `start` で AWS が断る |

**`ImageId`。** AMI の ID か名前を書く。`ami-` に続けて小文字の 16 進数が 8 桁か
17 桁なら ID、それ以外は名前として扱う。名前で指せるのは自分のアカウントの AMI
だけで、`*` と `?` は使えない。書かれていれば、`apply` はその値をそのまま使う。
このサービスの最新の AMI に選び直すには、行を消すか空にして `apply` を実行する。
指している AMI の登録が解除されているか使えない状態なら、`apply` はそう告げて止まる。
テンプレートが指している AMI の登録を解除すると、次の `start` が失敗する。どちらも
同じ手順で直る。他のサービスから焼いた AMI を使うときは、その名前か ID を
書く。そのときに行を消すと、他のサービスの AMI ではなく、このサービスから焼いた
AMI（無ければ Ubuntu 24.04）から選び直す。`bake` は、この項目を焼いた AMI の名前に
書き換える。書き換え方は `apply` と同じで、項目の順序とコメントを保つ。ファイルが
無ければ作らない。複数のマシンで同じサービスを使うときは、AMI を変えたあとに各マシンの
ファイルを揃える。

**`SubnetId`。** サブネットに IPv6 の CIDR が結び付いていれば、インスタンスには
IPv4 のパブリックアドレスに加えて IPv6 アドレスも付く。IPv4 の CIDR を持たない
サブネットは使えない。

**`VolumeSize`。** 起動するインスタンスのルートボリュームを、この大きさにする。
Ubuntu の AMI（とそこから焼いた AMI）では、起動するとインスタンスの中の cloud-init
がパーティションとファイルシステムを広げるので、インスタンスの中で手を動かす必要は
ない。広がるのは次の `start` で起動テンプレートから起動したインスタンスで、
動いているインスタンスと止めてあるインスタンスは変わらない。広げ方は
[usage.md](usage.md#ディスクを広げる) にある。

`ImageId` が指す AMI のルートボリュームより小さい値は、AWS が起動を断るので書けない。
`apply` と `diff` は、そう告げて何も変えずに止まる。`ImageId` を書かず、`apply` が
このサービスの最新の AMI を選ぶときも同じである。AMI に EBS のルートボリュームが
無ければ、大きさを決められないので、`VolumeSize` は書けない。EBS は縮められない
ので、大きくして焼いた AMI は、そのあと行を消しても、焼いたときの大きさのまま
起動する。

**別のアカウントで作るとき。** ファイルにアカウントは書かないので、別のアカウントの
プロファイルを `--profile` に付けて `apply` を実行すれば、そのアカウントにサービスを
新しく作る。ただし
`SubnetId` と `ImageId` はアカウントの中のリソースを指すので、先にこれらの行を
消し、`apply` に選び直させる。消さなければ、`apply` はサブネットが無いと告げて
止まる。そのときは何も書かないので、行を消してから、同じ `--profile` を付けて
やり直す。
元のアカウントも使い続けるなら、別の `<svc>` の名前でファイルを写し、写した方
から行を消して、`AwsProfile` も直す。

### `[security-group]`

セキュリティグループの受信規則。`[[security-group.ingress]]` を規則の数だけ繰り返して
書く。`apply` が AWS 側の受信規則をここに書いたものに揃え、書いていない規則（手で
足したものを含む）は消す。1 つも書かなければ受信規則は無く、ログインは Session
Manager 経由だけになる。変えたら `apply` を実行する。動いているインスタンスにも
すぐ効く。手で書くほか、`sg add` と `sg remove` でも書き換えられる
（[usage.md](usage.md#セキュリティグループ)）。`sg add` と `sg remove` を使うなら、規則は
他のテーブルを挟まずにまとめて書き、`[security-group]` の見出しを書くなら規則の
すぐ前に置く。分けて書いたファイルは書き換えられないことが
あり、そのときは手で直すよう告げて止まる。

| 項目 | 読むコマンド | 値 |
| --- | --- | --- |
| `Protocol` | `apply`、`diff`、`sg list`、`sg add`、`sg remove`、`sg diff` | `tcp`、`udp`、`icmp` のどれか。既定は `tcp` |
| `Port` | `apply`、`diff`、`sg list`、`sg add`、`sg remove`、`sg diff` | ポート。整数（`22`）か、範囲を表す整数 2 つの配列（`[22, 23]`）。0 から 65535。`tcp` と `udp` では必ず書き、`icmp` では書かない |
| `Cidr` | `apply`、`diff`、`sg list`、`sg add`、`sg remove`、`sg diff` | 送信元の IPv4 か IPv6 の CIDR（`203.0.113.10/32`） |
| `Description` | `apply`、`diff`、`sg list`、`sg add`、`sg remove`、`sg diff` | 規則の説明。省略できる。AWS コンソールの規則の Description に表示される |

`Protocol`、`Port`、`Cidr` が同じ規則を 2 つ書くことはできない。AWS は規則を
プロトコル・ポート・送信元で見分け、説明はその規則に 1 つだけ付くからである。
書き方が違っても、指すものが同じなら同じ規則である。

```toml
[[security-group.ingress]]
Port = 22
Cidr = "203.0.113.10/32"
Description = "home"

[[security-group.ingress]]
Protocol = "udp"
Port = [60000, 61000]
Cidr = "2001:db8:1::/64"
Description = "home (IPv6)"

[[security-group.ingress]]
Protocol = "icmp"
Cidr = "203.0.113.10/32"
Description = "home"
```

**`Protocol`。** `icmp` は ICMP のすべての種類（ping の echo など）を許す。`Cidr` が
IPv6 なら、AWS には ICMPv6 として渡す。

**`Port`。** すべてのポートは `[0, 65535]` と書く。長さが 2 でない配列、下限が上限より
大きい範囲、0 から 65535 の外の値は、エラーになる。

**`Cidr`。** IPv4 か IPv6 かは書式から決まる。ネットワーク部より下にビットが立った
値（`203.0.113.10/24`）は、書き誤りとしてエラーになる。IPv6 の規則は、
`SubnetId` のサブネットに IPv6 の CIDR が結び付いていて、インスタンスに IPv6
アドレスが付くときに効く。

**`Description`。** AWS が受けるのは 255 文字までの `a-z`、`A-Z`、`0-9`、空白と
`._-:/()#,@[]+=&;{}!$*` である。日本語は書けない。

**揃え方。** `apply` は AWS 側と規則ごとに比べ、足りない規則を足し、説明の違う規則の
説明を直し、ここに無い規則を消す。足してから消すので、揃えている間も、ここに書いた
規則は通り続ける。同じなら触らない。次の `apply` が何を変えるかは `sg diff` で
見られる。グローバル IP アドレスが変わったら、`sg remove` で古い規則を（IPv6 のものも）消し、`sg add` で
新しい規則を足してから `apply` を実行する
（[usage.md](usage.md#直接接続)）。

**`SubnetId` の VPC を変えたとき。** `apply` は新しい VPC にセキュリティグループを
用意し、起動テンプレートをそれに向ける。動いているインスタンスは古い
セキュリティグループのまま動き続け、`terminate` して `start` したときから新しい
VPC で立つ。古い VPC に残ったセキュリティグループは `destroy` が消す。

### `[role]`

インスタンスのロールの権限。名前を付けたポリシーを `[[role.policy.<ポリシー名>]]` に
書く。`[[role.policy.<ポリシー名>]]` の 1 つが IAM のポリシーの Statement 1 つで、
同じ名前で繰り返した並びが 1 つのポリシーになる。`apply` が、名前ごとにロールの
インラインポリシー `temt-<ポリシー名>` を作る。名前が `temt-` で始まるインライン
ポリシーは temt のものとみなし、設定ファイルに無ければ、手で付けたものでも
`apply` が消す。管理ポリシーは付けない。変えたら `apply` を実行する。動いている
インスタンスにも、IAM の反映を待てば効く。

**`ec2`。** ポリシー名 `ec2` は、アプリに結びつかない、インスタンスそのものの権限に
使う。temt が動くのに要る SSM エージェントの権限と、自分の階層 `/temt-<svc>/` の SSM
パラメータを読み書きする権限が入り、そのインスタンスで何をしたいかに応じて足す。
`apply` は、設定ファイルに `[[role.policy.ec2]]` が 1 つも無ければ、次の既定を書いて
から付ける。あれば、書かれたものをそのまま使うので、絞ることも足すこともできる。

```toml
# Permissions of the instance role that are not tied to one app. temt needs
# the first two statements (SSM Agent; the parameters of this service). Keep
# them and add whatever else this instance may do. apply writes
# [[role.policy.ec2]] only when there is none.
[[role.policy.ec2]]
Action = [
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
    "ssm:ListInstanceAssociations",
    "ssm:UpdateInstanceAssociationStatus",
    "ssm:GetDocument",
    "ssm:PutComplianceItems",
]
Resource = "*"

[[role.policy.ec2]]
Action = [
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
    "ssm:PutParameter",
    "ssm:DeleteParameter",
]
Resource = [
    "arn:aws:ssm:*:*:parameter/${temt:name}",
    "arn:aws:ssm:*:*:parameter/${temt:name}/*",
]
```

- 1 つ目は SSM エージェントの権限。Session Manager 経由の ssh、`reboot` が使う
  Run Command、sshd の待ち受けポートを揃える State Manager の関連付けに要る
  最小のものである。`PutComplianceItems` は、関連付けの実行結果をエージェントが
  報告するもので、無くても動くが、拒否がエージェントのログに毎回残るので入れて
  ある。`ec2messages` は要らない。SSM エージェント 3.3.40.0 からは `ssmmessages`
  を使い、Ubuntu の snap のエージェントは自動で更新される。起動直後の 1 分ほどは、
  エージェントが `ec2messages:GetMessages` も試して拒否され、エージェントのログに
  残るが、動きには関わらない。
- 2 つ目は自分の階層の SSM パラメータ。アプリが値を読み
  （[apps.md](apps.md#アプリの値)）、状態を書く（[apps.md](apps.md#アプリの状態)）。
  `GetParametersByPath` は階層そのもの（`parameter/${temt:name}`）で権限を調べる
  ので、`/*` と並べて書く。`SecureString` を使うなら、既定の鍵 `aws/ssm` で
  暗号化したものは KMS の権限を足さずに読み書きできる。自分で作った鍵を使うときは、
  その鍵への `kms:Encrypt` と `kms:Decrypt` をここに足し、鍵ポリシーでもロールに
  許す。パラメータの層（tier）が Advanced（大きな値を置ける有料の層。AWS の日本語の
  資料では「高度」）なら、`kms:GenerateDataKey` も足す。

足すときは、既定の 2 つの Statement を残す。消すと、ssh も `reboot` も sshd の
ポートの同期も効かなくなる。`[[role.policy.ec2]]` をすべて消して `apply` すれば、
既定に戻る。

アプリが使う権限は、アプリごとに別の名前で書く。`deploy` はロールに触らないので、
権限を変える時点とアプリを入れ替える時点を分けられる。例えば、先に `apply` で
新旧のアプリの両方が使える権限にし、アプリを `deploy` してから、古い権限を消して
`apply` する。ポリシーを分ける単位は、アプリごとでも、使う AWS のリソースごとでも
よい。

<ポリシー名> は、英小文字で始まり、英小文字・数字・ハイフンが続き、英小文字か数字で
終わる、64 文字までの名前とする。

`[[role.policy.<ポリシー名>]]` の中の項目名は、IAM の Statement のものをそのまま使う。

| 項目 | 読むコマンド | 値 |
| --- | --- | --- |
| `Effect` | `apply`、`diff` | `Allow` か `Deny`。省けば `Allow` |
| `Action`、`NotAction` | `apply`、`diff` | 操作。文字列か、文字列の配列 |
| `Resource`、`NotResource` | `apply`、`diff` | 対象の ARN。文字列か、文字列の配列 |
| `Condition` | `apply`、`diff` | 条件。IAM の書式のテーブル |
| `Sid` | `apply`、`diff` | Statement の名前。任意 |

```toml
[[role.policy.jupyter]]
Action = ["s3:GetObject", "s3:PutObject"]
Resource = "arn:aws:s3:::my-bucket/${temt:name}/*"

[[role.policy.jupyter]]
Effect = "Deny"
Action = "s3:DeleteObject"
Resource = "arn:aws:s3:::my-bucket/*"
```

**変数。** 文字列の中に次の変数を書ける。テーブルのキーの中にも書ける。`apply` が置き換えて
からロールに付ける。ほかの `${temt:...}` を書くと、読むときにエラーになる。
`${TEMT:name}` のように大文字を混ぜたものも同じである。IAM は変数の名前の大文字
小文字を区別しないので、取り違えると黙って効かなくなるからである。置き換えた結果、
同じテーブルのキーが重なれば、`apply` はエラーにして終了する。
`${aws:username}` のような IAM のポリシー変数は、そのまま IAM に渡す。

| 変数 | 値（サービス `web` の例） |
| --- | --- |
| `${temt:name}` | `temt-web` |
| `${temt:service}` | `web` |

**確かめ方。** temt が読むときに確かめるのは、<ポリシー名> が上の規則に従うこと、
`role.policy.<ポリシー名>` が空でないテーブルの配列であること、値が文字列、数、真偽値、
配列、テーブルのいずれかであること（日時、`nan`、`inf` は書けない）、知らない変数が無い
ことだけである。`[role.policy.<ポリシー名>]` と角かっこ 1 つで書いたテーブル
（`[[role.policy.<ポリシー名>.Statement]]` のように中に階層を作ったものも同じ）は、
`[[role.policy.<ポリシー名>]]` と書くよう告げて誤りにする。書式と中身は、`apply` が
`Effect` の無い Statement に `Allow` を補い、変数を置き換えたあと、ロールに付ける
前に IAM Access Analyzer の `ValidatePolicy` にかけて確かめる。`ERROR` の指摘が
1 つでもあれば、すべてを `role.policy.jupyter[0].Resource` のような位置とともに
挙げ、AWS に何も書かずに終了する。ほかの指摘（`SECURITY_WARNING`、`WARNING`、
`SUGGESTION`）は警告として挙げ、そのまま続ける。`ec2` の既定も確かめるので、
`apply` を実行する資格情報には `access-analyzer:ValidatePolicy` の権限が要る。

`Version` は temt が `2012-10-17` に決めるので書かない。ロールのインラインポリシー
は、分けても合わせて 10,240 文字までで、超えると `apply` は IAM の誤りで止まる。
`apply` は、新しいポリシーを書いてから、設定ファイルに無いポリシーを消す。消す
ポリシーと書くポリシーを合わせて上限を超えるなら、先に消すものだけを設定ファイル
から消して一度 `apply` してから、書き足す。

ロールに手で付けたインラインポリシーのうち、名前が `temt-` で始まらないものと、
管理ポリシーには、`apply` は触らない。

## `apply` が書くこと

`apply` は、書く項目を検出して表示し、確認のあとの更新で、このファイルに書く。
ファイルが無ければ作り、テーブルが無ければ足す。書かれている値が決まった値と同じ
であれば、その行には触らない。書き換えるとき、既に書かれている項目とテーブルの
順序、項目に付いたコメントは変えない。最上位の項目は最初のテーブルの前に足すので、
テーブルの前に置いたコメントは、足した項目の前に残る。テーブルの項目は、そのテーブルの
最後の項目の次に足すので、その後ろのコメントは、次の見出しの前に残る。以後はこの
ファイルの内容を正として進める。

- `AwsRegion`、`KeyName`、`InstanceType` は、既定を使ったときもその値を書く。
- `ImageId` には選んだ AMI の名前を書く。Ubuntu 24.04 の公開 AMI は自分の
  アカウントのものではなく名前では指せないので、ID を書く。
- `SubnetId` には選んだサブネットの ID を書く。既定のサブネットが見つからなければ
  （既定 VPC が無い、パブリックなサブネットが無い）、`SubnetId` を書いたファイルを
  用意するよう告げて、何も書かずに終了する。
- `IdentityFile` は、既定なら書かず、`KeyName` から導く。
- `SshdPorts` と `VolumeSize` は書かない。
- `AwsProfile` は、`--profile` が指定されたときだけその値を書く。
- `[[role.policy.ec2]]` は、1 つも無ければ既定を、コメントとともに書く
  （[`[role]`](#role)）。項目を書き終えたあとに書くので、新しく作るファイルでは
  項目の次、例のコメントより前に、既にあるファイルでは末尾に置かれる。書いたら
  `wrote the default [[role.policy.ec2]] in <ファイル>` と知らせる。足したあとの中身を
  読んで照合し、読めなければ（`role` をインラインテーブルで書いてあるなど）、書かずに、
  手で足すよう告げて止まる。ほかのポリシーには触らない。
- 新しく作るファイルの先頭にはコメントで、`apply` が書いたこと、直して `apply` を
  もう一度実行すれば反映されることを英語で記す。項目と `[[role.policy.ec2]]` を
  書き終えたあと、末尾には `[[security-group.ingress]]` と
  `[[role.policy.<ポリシー名>]]` の例をコメントアウトして置く。`apply` はこの例を
  書き換えない。

空でない値を `apply` が書き換えるのは、`--profile` で選ばれた `AwsProfile` だけで
ある。空の値は書かれていないものとして読み、決めた値で埋める。

初回の `apply` が書くファイルは次のようになる。サービスの AMI がまだ無いので、
`ImageId` は Ubuntu 24.04 の ID になる。

```toml
# Written by temt apply. Edit and run apply again to update.
AwsRegion = "ap-northeast-1"
KeyName = "temt"

[launch-template]
ImageId = "ami-0123456789abcdef0"
InstanceType = "t3.micro"
SubnetId = "subnet-0123456789abcdef0"

# Permissions of the instance role that are not tied to one app. temt needs
# the first two statements (SSM Agent; the parameters of this service). Keep
# them and add whatever else this instance may do. apply writes
# [[role.policy.ec2]] only when there is none.
[[role.policy.ec2]]
Action = [
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
    "ssm:ListInstanceAssociations",
    "ssm:UpdateInstanceAssociationStatus",
    "ssm:GetDocument",
    "ssm:PutComplianceItems",
]
Resource = "*"

[[role.policy.ec2]]
Action = [
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
    "ssm:PutParameter",
    "ssm:DeleteParameter",
]
Resource = [
    "arn:aws:ssm:*:*:parameter/${temt:name}",
    "arn:aws:ssm:*:*:parameter/${temt:name}/*",
]

# Inbound rules of the security group. Uncomment and edit, or run
# 'temt sg add --svc <svc>', to allow direct access; see docs/conf.md.
# [[security-group.ingress]]
# Port = 22
# Cidr = "203.0.113.10/32"
# Description = "home"

# Permissions for an app, as a named IAM policy. Uncomment and edit; see
# docs/conf.md.
# [[role.policy.example]]
# Action = ["s3:GetObject"]
# Resource = "arn:aws:s3:::example-bucket/${temt:name}/*"
```

最初の `apply` の前に決めておきたい項目があるときは、ファイルを先に作っておく。
残りの項目は `apply` が埋める。

    mkdir -p ~/.config/temt
    printf '[launch-template]\nInstanceType = "t3.small"\n' > ~/.config/temt/<svc>.toml

## 設定ファイルに書かないもの

マシンごとに違うものは、環境変数で与える。`TEMT_LOGIN` は空なら無いのと同じに扱う。

| 名前 | 読むコマンド | 値 |
| --- | --- | --- |
| `TEMT_LOGIN` | `proxy` | ssh でインスタンスへつなぐ経路。`ssm`（Session Manager 経由）か `direct`（パブリック IP へ直接）。無ければ `ssm`。それ以外の値はエラー |
| `TEMT_CONF_DIR` | `--svc` を付けたすべてと、`service list`、`app list` | 設定ファイルとアプリを置くディレクトリ。無ければ `~/.config/temt` |
| `TEMT_SSH_DIR` | `--svc` を付けたすべてと、`service list`、`keypair` のコマンド、`ami prune` | ssh の設定・鍵・known_hosts を置くディレクトリ。無ければ `~/.ssh/temt` |
| `AWS_PROFILE` | AWS を使うすべて（`proxy` を含む） | `--profile` も `AwsProfile` も無いときに使うプロファイル（[最上位](#最上位)） |

`TEMT_LOGIN` は `proxy` が接続のたびに読む。ssh は ProxyCommand に自分の環境変数を
渡すので、シェルの初期化でマシンごとに決めることも、`TEMT_LOGIN=direct ssh <名前>`
のようにその接続だけ変えることもできる。`apply` は `TEMT_LOGIN` を見ない。
`TEMT_LOGIN` と `AWS_PROFILE` は ProxyCommand に書かれないので、ssh を呼ぶエディター
（VS Code の Remote-SSH など）にも渡るように設定する。

`--profile` は、その実行だけ使うプロファイルで、`AwsProfile` より優先する。ssh の
ProxyCommand には届かず、`apply` に付けると `AwsProfile` に書かれるので、マシンごとの
違いを与えるのには使わない。

## 場所を変える環境変数

`TEMT_CONF_DIR` は設定ファイルの場所、`TEMT_SSH_DIR` は ssh の設定・鍵・
known_hosts の場所である。どちらにも次の規則を当て、破っていればエラーにする。

- 無いときと空文字列は、既定の場所を使う。
- 先頭の `~` と `~/` はホームディレクトリに展開する。`~user` は展開しない。
- 相対パスは書けない。`proxy` は ssh から呼ばれるので、カレントディレクトリが
  シェルのものと違う。
- 空白・タブと `"`、`'`、`\`、`%`、`$` は使えない。ssh の設定は空白で語を分け、
  `%` をトークンとして、`${NAME}` を環境変数として展開し、引用符と `\` を自前で
  解釈する。`Include`、`IdentityFile`、`UserKnownHostsFile` はどれも `${NAME}` を
  展開するので、`$` はバックスラッシュでも守れない。
- 表示できない文字（制御文字や、UTF-8 として読めないバイトなど）も使えない。
  ssh の設定に書けないからである。
- 末尾のスラッシュは無視する。シンボリックリンクは解決せず、書かれたまま使う。
  ただし、同じ場所かどうかの判定（`TEMT_SSH_DIR` が `~/.ssh` でないか、サービスを
  `apply` した場所が今と同じか）では、`..` とシンボリックリンクを解いてから
  比べる。
- `*`、`?`、`[` は使えるが、ssh の `Include` は glob（`*` などでファイル名を
  合わせるパターン）として読むので、`apply` はその行にだけバックスラッシュを
  付けて書く。
- `TEMT_SSH_DIR` は `~/.ssh` そのものを指せない。temt の `config` が
  `~/.ssh/config` と同じファイルになり、自分自身を `Include` してしまう。

無ければ temt が作る。`TEMT_CONF_DIR` のディレクトリは、`apply` が設定ファイルを
書くときに作る。
`TEMT_SSH_DIR` のディレクトリと、その中の `keys/`、`known_hosts.d/<名前>/` は、
使うコマンドが作る。どれもモード 700 になり、無い親ディレクトリも 700 で
作られる。既にあるディレクトリのモードは、ssh ディレクトリと `keys/` を除いて
変えない。

どちらの変数も、それを読むコマンドが起動のたびに読む。`apply` は、ssh の設定に
書く `IdentityFile`、`UserKnownHostsFile`、`Include` と、`ProxyCommand` が
渡す場所を、その実行のときの値で決める。ssh から呼ばれる `proxy` に場所を
伝えるため、`apply` は 2 つとも、既定のままでも `env` で渡す。ホームの下の
場所は `~` で始まる形で書き、`proxy` が自分で展開する。ssh は ProxyCommand に
自分の環境変数を渡すので、書かなければ `proxy` は ssh を打ったシェルの値を
読んでしまう。書いておけば、シェルに変数が無くても、別の値があっても、ssh の
接続は `apply` のときと同じ場所を使う。

    ProxyCommand env TEMT_CONF_DIR='/srv/temt/conf' TEMT_SSH_DIR='/srv/temt/ssh' temt proxy --svc dev-main %p

`TEMT_CONF_DIR` は、行き来してよい。ssh ディレクトリは 1 つなので、どの場所で
`apply` したサービスにも ssh でつながる。ただし、同じサービスの設定ファイルを
2 つの場所に置くことはできない。別の場所にまだあれば、`apply` はその場所を
挙げて止まる。場所を移すときは、設定ファイルを新しい場所へ動かしてから `apply`
を実行する。設定ファイルには鍵が入らないので、他のマシンと同期するディレクトリや、
バージョン管理下のディレクトリにも置ける。`apps/` に置いたアプリも同じである。

`TEMT_SSH_DIR` を変えるときは、先に前の場所の `keys/` を新しい場所へ移してから
`apply` をやり直す（`IdentityFile` を書いていなければ、移さないと鍵が無いと告げて止まる）。`~/.ssh/config` の `Include` が新しい
場所を指すように書き換わり、前の場所にある Host には ssh でつながらなくなる。
`apply` は、そのうち新しい場所に無い Host を挙げて警告する。前の場所にある鍵や
known_hosts を temt が動かすことはなく、`destroy` もそのとき変数が指す場所だけを
片づけるので、要るものは手で移す。鍵を置く場所なので、他のマシンと同期するディレクトリ
や、バージョン管理下のディレクトリは指さない。
