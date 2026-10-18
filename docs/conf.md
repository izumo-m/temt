# temt の設定ファイル

`~/.config/temt/<svc>.toml`。サービスの定義で、`--svc <svc>` を付けたすべての
コマンドが読む。`--svc` を付けない `service list` は、すべての `<svc>.toml` を
読んで並べる。`--svc` を付けない `instance list`、`ami list`、`ami prune`、
`ami delete`、`app list` は読まない。`apply` はこの
ファイルを正として、AWS リソースと手元の環境を揃える。ssh の経路は手元のネットワークで
変わるので、このファイルではなく環境変数で与える（[設定ファイルに
書かないもの](#設定ファイルに書かないもの)）。場所は環境変数 `TEMT_CONF_DIR` で
変えられる（[場所を変える環境変数](#場所を変える環境変数)）。全サービスに共通の値は、
同じ場所の `defaults.toml` に書ける（[全サービスに共通の値](#全サービスに共通の値)）。

## 形式

TOML 1.0.0。リソースをまたぐ項目は最上位に、リソースごとの項目はそのリソース
のテーブルに置く。テーブル名は aws-cli のリソース名（`launch-template` など）、
テーブルの中の項目名は AWS の API か aws-cli の項目名（`InstanceType`、`Cidr` など）に
合わせる。項目の値は文字列を原則とし、`Defaults` と `WaitForSsm` が真偽値、
`[security-group]` の `Port` が整数か整数の配列である。`[launch-template]` と `[role]` の
ポリシーは例外で、それぞれ起動テンプレートの項目と IAM の Statement の書式を TOML で書く
（[`[launch-template]`](#launch-template)、[`[role]`](#role)）。

下の表にない項目やテーブル（`[launch-template]` では起動テンプレートに無い項目）、値の型が
表と違う項目、テーブルを書くべき名前に別の型の値を書いたもの、
TOML として読めない記述があれば、エラーにして終了する。表にある項目が無ければ既定を
使う。空文字列、空の配列、`[launch-template]` の中の空のテーブルは、書かれていない
ものとして扱う。ただし、AMI のデバイスを外す `NoDevice = ""` は、空文字列のまま
起動テンプレートに入る。

## 全サービスに共通の値

同じ場所の `defaults.toml`（共通ファイル）に、全サービスに共通の値を書ける。形式と
書ける項目は設定ファイルと同じで、`<svc>.toml` に書いていない項目には共通ファイルの
値が使われる。この名前のため、`defaults` はサービス名に使えない。

```toml
AwsProfile = "work"

[launch-template]
InstanceType = "t3.small"

[[security-group.ingress]]
Port = 22
Cidr = "203.0.113.10/32"
Description = "home"
```

- 受信規則は、共通ファイルの規則の後ろに `<svc>.toml` の規則を足したものになる。同じ
  規則を両方に書くとエラーになる。`sg add` と `sg remove` が書き換えるのは
  `<svc>.toml` だけで、共通ファイルの規則を消すときや説明を変えるときは、共通ファイルを
  直すよう告げて止まる。
- ポリシーは名前ごとに重ねる。同じ名前のポリシーは、`<svc>.toml` のものだけを使う。
- `[launch-template]` は項目ごとに重ねる。`NetworkInterfaces` は `DeviceIndex`、
  `BlockDeviceMappings` は `DeviceName`、`TagSpecifications` は `ResourceType`、タグは
  `Key` が同じ要素どうしを、要素の中の項目ごとに重ね、違う要素は両方を使う。たとえば、
  共通ファイルに書いたインスタンスのタグ `Owner` と、`<svc>.toml` に書いたインスタンスの
  タグ `Env` は、両方付く。ほかの配列（`Groups` など）は、`<svc>.toml` に書けば共通
  ファイルのものを丸ごと置き換える。
- 空の値は書かれていないのと同じなので、共通ファイルの値を `<svc>.toml` で消すことは
  できない。共通ファイルを使わないサービスは、`<svc>.toml` に `Defaults = false` と
  書く（[最上位](#最上位)）。
- `apply` は、共通ファイルの値も既定と同じように `<svc>.toml` に書く
  （[`apply` が書くこと](#apply-が書くこと)）。`AwsRegion` と、`[launch-template]` の
  [書かなければ `apply` が書く項目](#書かなければ-apply-が書く項目)は最初の `apply` で
  書かれるので、あとで共通ファイルを直しても、作ってあるサービスは変わらない。
  書かれない `AwsProfile`、`[launch-template]` のほかの項目、受信規則、ポリシーは、直せば
  すべてのサービスに効く。AWS に反映するのは、サービスごとの `apply` である。
- `--svc` を付けない `instance list`、`ami list`、`ami prune`、`ami delete` も、
  共通ファイルの `AwsProfile` と `AwsRegion` を使う。環境変数 `AWS_PROFILE` や
  `AWS_REGION` より優先する。

## 項目

### 最上位

表の「読むコマンド」は、その項目の値を読むコマンドである。このほか `service list` が、
表示のためにすべての項目を読む。

| 項目 | 読むコマンド | 既定 |
| --- | --- | --- |
| `AwsProfile` | `service list`、`sg list`、`sg add`、`sg remove` を除く、`--svc` を付けたすべて。共通ファイルのものは、`--svc` を付けない `instance list`、`ami list`、`ami prune`、`ami delete` も読む | 無し。書かなければ AWS のプロファイルを指定せず、資格情報は AWS の既定の規則で決まる |
| `AwsRegion` | `service list`、`sg list`、`sg add`、`sg remove` を除く、`--svc` を付けたすべて。共通ファイルのものは、`--svc` を付けない `instance list`、`ami list`、`ami prune`、`ami delete` も読む | `--region`、共通ファイルの `AwsRegion`、環境変数 `AWS_REGION`、`AWS_DEFAULT_REGION`、AWS のプロファイルの `region` の順に探し、どれも無ければ `ap-northeast-1` |
| `Defaults` | `--svc` を付けたすべて | `true`。`false` なら共通ファイルを読まない。共通ファイルには書けない |
| `WaitForSsm` | `start` | `true`。`false` なら、`start` は SSM エージェントを待たない |

**`AwsProfile`。** AWS のプロファイルを固定するには、次の 3 つの方法がある。

- `--profile <プロファイル>` を付ける（`temt apply --svc <svc> --profile work`）。
  `apply` と `ami import` はその値を `AwsProfile` に書くので、次からは付けなくてよい。
  ほかのコマンドでは、その実行のあいだだけ効く。
- `AwsProfile = "work"` と書く。共通ファイルに書けば、全サービスに効く。
- 環境変数 `AWS_PROFILE` を設定する。`AWS_DEFAULT_PROFILE` と両方に別の値を
  入れると、temt は `AWS_DEFAULT_PROFILE` を使う。aws-cli v2 を単独で使うときは
  `AWS_PROFILE` が先なので、両方を使うなら同じ値にする。

`--profile` を付けずに `apply` を実行しても、この項目が書き直されることはない。

**`AwsRegion`。** 無ければ、上の順に探したリージョンを使い、最初の `apply` と
`ami import` がその値を書く。書いたあとは、この値だけを使う。違う値の `--region` を
付けると、引数の誤りになる。1 つのファイルは 1 つのインスタンスに対応するので、別の
リージョンを使うときは、別の `<svc>` として立てる。

ログインに使う鍵は、このファイルには書かない。手元の ssh の設定の
`IdentityFile` で決まる（[usage.md](usage.md#ログインの鍵)）。

**`Defaults`。** `Defaults = false` と書けば、このサービスは共通ファイルを読まず、
このファイルだけで決まる。`ami import` がこれを書く。共通ファイルの値が入ると、取り込んだ
サービスが元のインスタンスと違うものになるからである。共通ファイルを使うなら、この行を
消す。

**`WaitForSsm`。** `WaitForSsm = false` と書けば、`start` はインスタンスが動き出した
（`running` になった）ところで終わり、SSM エージェントを待たない。SSM エージェントが
登録されないインスタンスのためのもので、`ami import` は、元のインスタンスが SSM に
登録されていなければこれを書く。Session Manager 経由の `ssh`、`reboot`、`set-port` は
SSM エージェントを使うので、そうしたインスタンスでは失敗するか、締め切りで止まる。

### `[launch-template]`

起動テンプレートの中身。AWS の起動テンプレートの項目を、`aws ec2
get-launch-template-data` の出力（`create-launch-template` の `--launch-template-data`
に渡す JSON）と同じ名前と入れ子で書く。変えたら `apply` を実行する。動いている
インスタンスは変わらず、次の `start` から反映される。

起動テンプレートの中身は、ここと共通ファイルに書いた項目と、temt が足すもの（下）
だけになる。行を消せば（共通ファイルにも無ければ）、次の `apply` で起動テンプレート
からも消える。AWS のコンソールなどで起動テンプレートを手で変えても、次の `apply` で
ここに書いたとおりに戻る。

```toml
[launch-template]
ImageId = "temt-web-2610101230"
InstanceType = "t3.small"
InstanceInitiatedShutdownBehavior = "terminate"
IamInstanceProfile = { Name = "${temt:name}" }

[[launch-template.NetworkInterfaces]]
DeviceIndex = 0
SubnetId = "subnet-0123456789abcdef0"
AssociatePublicIpAddress = true

[[launch-template.BlockDeviceMappings]]
Ebs.VolumeSize = 30    # DeviceName を省いたので、ルートボリューム

[launch-template.MetadataOptions]
HttpTokens = "required"
InstanceMetadataTags = "enabled"

[launch-template.CreditSpecification]
CpuCredits = "unlimited"
```

AWS の項目の値は、次の TOML の型で書く。値は書いたまま AWS に渡る（`UserData` も
base64 のまま書く）。知らない項目名と型の違う値はエラーになる。書ける項目は、temt が
使う boto3 の版で決まる。

- まとまり（`MetadataOptions` など）はテーブル。ドット付きのキー
  （`Ebs.VolumeSize = 30`）や、インラインテーブル（`{ Name = "..." }`）で書いてもよい。
- まとまりの並び（`NetworkInterfaces` など）はテーブルの配列、文字列の並び（`Groups`
  など）は文字列の配列。
- 文字列、整数、真偽値、小数、日時は、TOML の同じ型。

AWS が読み出しのときにだけ返す 4 つの項目（`MetadataOptions.State` など）は書けない。
項目どうしの組み合わせ（`SecurityGroupIds` と `NetworkInterfaces` の `Groups` など）は
temt では確かめず、AWS が断る。

#### 書かなければ `apply` が書く項目

次の項目は、書いていなければ `apply` が値を決めて、このファイルに書く。行を消した
ときも、次の `apply` で書き戻す。入れたくない値は、行を消さずに別の値で書く
（`CpuCredits = "standard"`、`Ipv6AddressCount = 0` など）。

| 項目 | 既定 |
| --- | --- |
| `ImageId` | このサービスから焼いた（`temt:service=<svc>` のタグを持つ）`available` な AMI のうち、`CreationDate` が最も新しいもの。無ければ Canonical の Ubuntu 24.04 |
| `InstanceType` | `t3.micro` |
| `NetworkInterfaces` の `DeviceIndex = 0` の要素の `SubnetId` | 既定 VPC のサブネットのうち、`MapPublicIpOnLaunch` が true で、アベイラビリティゾーン名が辞書順で最も小さいもの |
| 同じ要素の `AssociatePublicIpAddress` | `true` |
| 同じ要素の `Ipv6AddressCount` | `1`。サブネットに IPv6 の CIDR が結び付いているときだけ書く |
| `MetadataOptions.HttpTokens` | `required` |
| `MetadataOptions.InstanceMetadataTags` | `enabled`。アプリが、インスタンスメタデータのタグからサービスの名前を読めるようにする（[apps.md](apps.md#aws-を使う)） |
| `InstanceInitiatedShutdownBehavior` | `terminate`。インスタンスの中で電源を切ると、止まるのではなく終了する |
| `CreditSpecification.CpuCredits` | `unlimited`。`InstanceType` が T 系（`t3.micro` など）のときだけ書く |

`NetworkInterfaces` に `DeviceIndex = 0` の要素が無ければ、要素を足して書く。その要素に
`NetworkInterfaceId` があれば、`SubnetId`、`AssociatePublicIpAddress`、
`Ipv6AddressCount` は書かない。書き足す先がインラインテーブルの中なら、書かずに、手で
書くよう告げて止まる。

`CreditSpecification` と `Ipv6AddressCount` は、書いたときの `InstanceType` と
サブネットで決める。あとで T 系でない型や IPv6 の無いサブネットに変えても残り、次の
`start` を AWS が断る。そのときは行を消して `apply` する（[項目を消す](#項目を消す)）。

どの項目も、`apply`、`diff`、`bake`（`--bake` を付けた `deploy` と `app remove`、
`set-port` を含む）が読む。`SubnetId` は `sg diff` と `sg show` も読む。

#### temt が足すもの

次のものは、書いていなければ temt が `apply` のたびに決めて、起動テンプレートに入れる。
このファイルには書かない。ファイルを写して別のサービスを作っても、元のサービスの名前を
引き継がないようにするためである。

- インスタンス、ボリューム、ネットワークインターフェースの `Name` タグ。`temt-<svc>`。
- `DeviceIndex = 0` の要素の `Groups`。temt が作るセキュリティグループ `temt-<svc>`。
  最上位に `SecurityGroupIds` か `SecurityGroups` を書いたときと、要素に
  `NetworkInterfaceId` を書いたときは入れない。

インスタンス、ボリューム、ネットワークインターフェースには、書いても書かなくても、
タグ `temt:service=<svc>` が付く。temt はこのタグでサービスのインスタンスを見分ける。

#### 項目ごとの説明

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
無ければ作らない。

**`SubnetId`。** サブネットに IPv6 の CIDR が結び付いていれば、インスタンスには
IPv4 のパブリックアドレスに加えて IPv6 アドレスも付く。IPv4 の CIDR を持たない
サブネットは使えない。

**`Groups`。** セキュリティグループの ID のほか、名前も書ける。`sg-` で始まれば ID、
それ以外は名前として扱い、`apply` が `SubnetId` の VPC の中で ID に直す。その VPC に
無い名前はエラーになる。`${temt:name}` か `temt-<svc>` と書けば temt のセキュリティ
グループで、無ければ `apply` が作る。`Groups` を書けば、temt のセキュリティグループは
そこに書いたときだけ付く（[`[security-group]`](#security-group)）。

**`IamInstanceProfile`。** インスタンスプロファイル。書かなければ、インスタンス
プロファイル無しで起動する。サービスを作る最初の `apply` が
`IamInstanceProfile = { Name = "${temt:name}" }` を書く。temt が作るインスタンス
プロファイル `temt-<svc>` を指しているあいだだけ、`apply` は `[role]` のとおりに
インスタンスロールとインスタンスプロファイルを作って揃える（[`[role]`](#role)）。
ほかのインスタンスプロファイルを書けば、temt は IAM に何もしない。それが無ければ、
`start` を AWS が断る。

**`KeyName`。** ログインの鍵は temt が接続のたびに送るので、要らない。書けば（`ami
import` は元のインスタンスの値を写す）、そのまま起動テンプレートに入り、そのキーペアの
鍵でもログインできる。

**ルートボリュームの大きさ。** `BlockDeviceMappings` の要素の `DeviceName` を省けば、
temt が `ImageId` の AMI のルートボリュームのデバイス名を入れる（このファイルには
書かない）。`ImageId` を別の OS の AMI に変えても、ルートボリュームを指し続ける。
省けるのは 1 つの要素だけである。その要素の `Ebs.VolumeSize`（GiB）で、起動する
インスタンスのルートボリュームの大きさを決める。書かなければ AMI と同じ大きさで
起動する。`DeviceName` を書くなら、AMI のルートボリュームのデバイス名（Ubuntu は
`/dev/sda1`）と同じにする。違う名前を書くと、AWS はルートボリュームを広げず、空の
ディスクを 1 台足す。

Ubuntu の AMI（とそこから焼いた AMI）では、起動するとインスタンスの中の cloud-init
がパーティションとファイルシステムを広げるので、インスタンスの中で手を動かす必要は
ない。広がるのは次の `start` で起動テンプレートから起動したインスタンスで、
動いているインスタンスと止めてあるインスタンスは変わらない。広げ方は
[usage.md](usage.md#ディスクを広げる) にある。

`ImageId` が指す AMI のルートボリュームより小さい値は、AWS が起動を断るので書けない。
`apply` と `diff` は、そう告げて何も変えずに止まる。`ImageId` を書かず、`apply` が
このサービスの最新の AMI を選ぶときも同じである。AMI に EBS のルートボリュームが
無ければ、大きさを決められないので、`Ebs.VolumeSize` は書けない。EBS は縮められない
ので、大きくして焼いた AMI は、そのあと行を消しても、焼いたときの大きさのまま
起動する。

**変数。** 値の中に `${temt:name}`（`temt-<svc>`）と `${temt:service}`（`<svc>`）を
書ける。`apply` が置き換えてから起動テンプレートに入れる（[`[role]`](#role) の変数と
同じ）。

#### 項目を消す

起動テンプレートから項目を消すには、行を消して `apply` する。共通ファイルにある項目は、
共通ファイルから消すか、このファイルに `Defaults = false` を書く。[書かなければ `apply` が
書く項目](#書かなければ-apply-が書く項目)は、消しても書き戻されるので、別の値で書く。

`apply` や `start` を AWS が断るときは、AWS の誤りから、どの項目かが分かる。その行を
直すか消し、`diff` で確かめてから `apply` する。

#### 別のアカウントで作るとき

ファイルにアカウントは書かないので、別のアカウントの AWS のプロファイルを `--profile` に
付けて `apply` を実行すれば、そのアカウントにサービスを新しく作る。ただし、
アカウントの中のリソースを指す項目（`SubnetId`、`ImageId`、書いていれば ID で書いた
`Groups`、`${temt:name}` でない `IamInstanceProfile`、`KeyName` など）は、先に消すか
書き換える。`SubnetId` と `ImageId` は、消せば `apply` が選び直す。消さなければ、`apply`
はサブネットが無いと告げて止まる。そのときは何も書かないので、行を消してから、同じ
`--profile` を付けてやり直す。
元のアカウントも使い続けるなら、別の `<svc>` の名前でファイルを写し、写した方
から行を消して、`AwsProfile` も直す。

### `[security-group]`

temt が作るセキュリティグループ `temt-<svc>` の受信規則。`[[security-group.ingress]]` を
規則の数だけ繰り返して書く。`apply` が AWS 側の受信規則をここに書いたものに揃え、書いて
いない規則（手で足したものを含む）は消す。`[launch-template]` の `Groups` を書いて、
temt のセキュリティグループを使っていないサービス（`ami import` で取り込んだサービス
など）では、受信規則があると `apply` がエラーにする。使うなら、`Groups` に
`${temt:name}` を足す。共通ファイルにも 1 つも無ければ受信規則は無く、ログインは Session
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
| `Description` | `apply`、`diff`、`sg list`、`sg add`、`sg remove`、`sg diff` | 規則の説明。省略できる |

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

**`Cidr`。** ネットワーク部より下にビットが立った
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

**`SubnetId` の VPC を変えたとき。** temt のセキュリティグループを使っていれば、
`apply` は新しい VPC にセキュリティグループを用意し、起動テンプレートをそれに向ける。動いているインスタンスは古い
セキュリティグループのまま動き続け、`terminate` して `start` したときから新しい
VPC で立つ。古い VPC に残ったセキュリティグループは `destroy` が消す。

### `[role]`

temt が作るインスタンスロール `temt-<svc>` の権限。インスタンスロールは、インスタンス
プロファイルに入っている IAM ロールで、インスタンスはその権限で AWS を呼べる。temt は、
`[launch-template]` の `IamInstanceProfile` が temt のインスタンスプロファイル
`temt-<svc>`（`${temt:name}`）を指しているときだけ、インスタンスロールとインスタンス
プロファイル `temt-<svc>` を作り、ここに書いたとおりに揃える。指していない（書いて
いないときも含む）のに `[role]` を書くと、`apply` がエラーにする。共通ファイルの
`[role]` も同じで、どのファイルの項目かを告げる。ほかのインスタンスロールの権限を
temt で扱うには、`role import` で temt の管理に移す
（[usage.md](usage.md#インスタンスロールを-temt-の管理に移す)）。

名前を付けたポリシーを `[[role.policy.<ポリシー名>]]` に書く。
`[[role.policy.<ポリシー名>]]` の 1 つが IAM のポリシーの Statement 1 つで、
同じ名前で繰り返した並びが 1 つのポリシーになる。`apply` が、名前ごとにインスタンス
ロールのインラインポリシー `temt-<ポリシー名>` を作る。名前が `temt-` で始まるインライン
ポリシーは temt のものとみなし、設定ファイルに無ければ、手で付けたものでも
`apply` が消す。変えたら `apply` を実行する。動いているインスタンスにも、IAM の反映を
待てば効く。

管理ポリシー（AWS 管理のものと、自分で作ったもの）は、ARN の配列を
`ManagedPolicyArns` に書く。`apply` は、ここに書いた管理ポリシーを付け、書いていない
管理ポリシーは外す。

| 項目 | 読むコマンド | 値 |
| --- | --- | --- |
| `ManagedPolicyArns` | `apply`、`diff` | 付ける管理ポリシーの ARN の配列。無ければ管理ポリシーは付けない |

```toml
[role]
ManagedPolicyArns = ["arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"]
```

**`ec2`。** ポリシー名 `ec2` は、アプリに結びつかない、インスタンスそのものの権限に
使う。temt が動くのに要る SSM エージェントの権限と、自分の階層 `/temt-<svc>/` の SSM
パラメータを読み書きする権限が入り、そのインスタンスで何をしたいかに応じて足す。
`apply` は、`IamInstanceProfile` が temt のインスタンスプロファイルを指していて、
設定ファイルにも共通ファイルにも `[[role.policy.ec2]]` が 1 つも無ければ、
次の既定を設定ファイルに書いてから付ける。あれば、書かれたものをそのまま使うので、絞ることも足すこともできる。

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

- 1 つ目は SSM エージェントの権限。Session Manager 経由の ssh と、`reboot` と
  `set-port` が使う Run Command に要る最小のものである。`ec2messages` は要らない。SSM エージェント 3.3.40.0 からは `ssmmessages`
  を使い、Ubuntu の snap のエージェントは自動で更新される。起動直後の 1 分ほどは、
  エージェントが `ec2messages:GetMessages` も試して拒否され、エージェントのログに
  残るが、動きには関わらない。
- 2 つ目は自分の階層の SSM パラメータ。アプリが値を読み
  （[apps.md](apps.md#アプリの値)）、状態を書く（[apps.md](apps.md#アプリの状態)）。
  `GetParametersByPath` は階層そのもの（`parameter/${temt:name}`）で権限を調べる
  ので、`/*` と並べて書く。`SecureString` を使うなら、既定の鍵 `aws/ssm` で
  暗号化したものは KMS の権限を足さずに読み書きできる。自分で作った鍵を使うときは、
  その鍵への `kms:Encrypt` と `kms:Decrypt` をここに足し、鍵ポリシーでもインスタンス
  ロールに許す。パラメータの層（tier）が Advanced なら、`kms:GenerateDataKey` も足す。

足すときは、既定の 2 つの Statement を残す。消すと、ssh も `reboot` も `set-port` も
効かなくなる。`[[role.policy.ec2]]` をすべて消して `apply` すれば、
既定に戻る（共通ファイルに `ec2` があれば、そちらになる）。

アプリが使う権限は、アプリごとに別の名前で書く。`deploy` はインスタンスロールに触らないので、
権限を変える時点とアプリを入れ替える時点を分けられる。例えば、先に `apply` で
新旧のアプリの両方が使える権限にし、アプリを `deploy` してから、古い権限を消して
`apply` する。

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
からインスタンスロールに付ける。ほかの `${temt:...}` を書くと、読むときにエラーになる。
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
`Effect` の無い Statement に `Allow` を補い、変数を置き換えたあと、インスタンスロールに付ける
前に IAM Access Analyzer の `ValidatePolicy` にかけて確かめる。`ERROR` の指摘が
1 つでもあれば、すべてを `role.policy.jupyter[0].Resource` のような位置とともに
挙げ、AWS に何も書かずに終了する。ほかの指摘（`SECURITY_WARNING`、`WARNING`、
`SUGGESTION`）は警告として挙げ、そのまま続ける。`ec2` の既定も確かめるので、
`apply` を実行する資格情報には `access-analyzer:ValidatePolicy` の権限が要る。

`Version` は temt が `2012-10-17` に決めるので書かない。インスタンスロールのインラインポリシー
は、分けても合わせて 10,240 文字までで、超えると `apply` は IAM の誤りで止まる。
`apply` は、新しいポリシーを書いてから、設定ファイルに無いポリシーを消す。消す
ポリシーと書くポリシーを合わせて上限を超えるなら、先に消すものだけを設定ファイル
から消して一度 `apply` してから、書き足す。

## `apply` が書くこと

`apply` は、書く項目を検出して表示し、確認のあとの更新で、このファイルに書く。
ファイルが無ければ作り、テーブルが無ければ足す。書かれている値が決まった値と同じ
であれば、その行には触らない。書き換えるとき、既に書かれている項目とテーブルの
順序、項目に付いたコメントは変えない。最上位の項目は最初のテーブルの前に足すので、
テーブルの前に置いたコメントは、足した項目の前に残る。テーブルの項目は、そのテーブルの
最後の項目の次に足すので、その後ろのコメントは、次の見出しの前に残る。

- `AwsRegion` と、[書かなければ `apply` が書く項目](#書かなければ-apply-が書く項目)は、
  既定や共通ファイルの値を使ったときもその値を書く。
- `ImageId` には選んだ AMI の名前を書く。Ubuntu 24.04 の公開 AMI は自分の
  アカウントのものではなく名前では指せないので、ID を書く。
- `SubnetId` には選んだサブネットの ID を書く。既定のサブネットが見つからなければ
  （既定 VPC が無い、パブリックなサブネットが無い）、`SubnetId` を書いたファイルを
  用意するよう告げて、何も書かずに終了する。
- ファイルが無いとき（サービスを作るとき）は、
  `IamInstanceProfile = { Name = "${temt:name}" }` を書く。
- `AwsProfile` は、`--profile` が指定されたときだけその値を書く。
- `[[role.policy.ec2]]` は、`IamInstanceProfile` が temt のインスタンスプロファイルを
  指していて、このファイルにも共通ファイルにも 1 つも無ければ既定を、
  コメントとともに書く
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
ある。

初回の `apply` が書くファイルは次のようになる。サービスの AMI がまだ無いので、
`ImageId` は Ubuntu 24.04 の ID になる。サブネットに IPv6 の CIDR が結び付いていれば、
`NetworkInterfaces` に `Ipv6AddressCount = 1` も書く。

```toml
# Written by temt apply. Edit and run apply again to update.
AwsRegion = "ap-northeast-1"

[launch-template]
ImageId = "ami-0123456789abcdef0"
InstanceType = "t3.micro"
InstanceInitiatedShutdownBehavior = "terminate"
IamInstanceProfile = { Name = "${temt:name}" }

[[launch-template.NetworkInterfaces]]
DeviceIndex = 0
SubnetId = "subnet-0123456789abcdef0"
AssociatePublicIpAddress = true

[launch-template.MetadataOptions]
HttpTokens = "required"
InstanceMetadataTags = "enabled"

[launch-template.CreditSpecification]
CpuCredits = "unlimited"

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
残りの項目は `apply` が埋める。`IamInstanceProfile` は、ファイルが無いときにしか
書かないので、先に作るファイルには自分で書く。書かなければ、インスタンスプロファイル
無しで起動する。

    mkdir -p ~/.config/temt
    cat > ~/.config/temt/<svc>.toml <<'EOF'
    [launch-template]
    InstanceType = "t3.small"
    IamInstanceProfile = { Name = "${temt:name}" }
    EOF

## 設定ファイルに書かないもの

次のものは、設定ファイルではなく環境変数で与える。`TEMT_LOGIN` は空なら無いのと同じに
扱う。

| 名前 | 読むコマンド | 値 |
| --- | --- | --- |
| `TEMT_LOGIN` | `proxy` | ssh でインスタンスへつなぐ経路。`ssm`（Session Manager 経由）か `direct`（パブリック IP へ直接）。無ければ `ssm`。それ以外の値はエラー |
| `TEMT_CONF_DIR` | すべて | 設定ファイルとアプリを置くディレクトリ。無ければ `~/.config/temt` |
| `TEMT_SSH_DIR` | `--svc` を付けたもののうち ssh の設定、鍵、ロックを使うもの（`apply`、`proxy` など）と、`ami prune` | ssh の設定・鍵・known_hosts を置くディレクトリ。無ければ `~/.ssh/temt` |
| `AWS_PROFILE` | AWS を使うすべて（`proxy` を含む） | `--profile` も `AwsProfile` も無いときに使う AWS のプロファイル（[最上位](#最上位)） |
| `AWS_REGION`、`AWS_DEFAULT_REGION` | AWS を使うすべて | `--region` も `AwsRegion` も無いときに使うリージョン（[最上位](#最上位)） |

`TEMT_LOGIN` は `proxy` が接続のたびに読む。ssh は ProxyCommand に自分の環境変数を
渡すので、シェルの初期化で決めることも、`TEMT_LOGIN=direct ssh <名前>`
のようにその接続だけ変えることもできる。`apply` は `TEMT_LOGIN` を見ない。
`TEMT_LOGIN` と `AWS_PROFILE` は ProxyCommand に書かれないので、ssh を呼ぶエディター
（VS Code の Remote-SSH など）にも渡るように設定する。

`--profile` は ssh の ProxyCommand には届かない。

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
- `*`、`?`、`[` は使えるが、ssh の `Include` は glob として読むので、`apply` はその行にだけバックスラッシュを
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
書く `UserKnownHostsFile`、`Include`、ブロックに無いときに足す `IdentityFile` と、
`ProxyCommand` が渡す場所を、その実行のときの値で決める。ssh から呼ばれる `proxy` に場所を
伝えるため、`apply` は 2 つとも、既定のままでも `env` で渡す。ホームの下の
場所は `~` で始まる形で書き、`proxy` が自分で展開する。ssh は ProxyCommand に
自分の環境変数を渡すので、書かなければ `proxy` は ssh を打ったシェルの値を
読んでしまう。

    ProxyCommand env TEMT_CONF_DIR='/srv/temt/conf' TEMT_SSH_DIR='/srv/temt/ssh' temt proxy --svc dev-main --user '%r' %p

`TEMT_CONF_DIR` は、行き来してよい。ssh ディレクトリは 1 つなので、どの場所で
`apply` したサービスにも ssh でつながる。ただし、同じサービスの設定ファイルを
2 つの場所に置くことはできない。別の場所にまだあれば、`apply` はその場所を
挙げて止まる。場所を移すときは、設定ファイルを新しい場所へ動かしてから `apply`
を実行する。

`TEMT_SSH_DIR` を変えたら、`apply` をやり直す。新しい場所に鍵が無ければ `apply` が
作る。前の場所の `config` を手で移したときは、Host ブロックの `IdentityFile` が前の
場所を指したまま残るので、その行を消してから `apply` する。`~/.ssh/config` の
`Include` が新しい場所を指すように書き換わり、前の場所にある Host には ssh でつながらなくなる。
`apply` は、そのうち新しい場所に無い Host を挙げて警告する。前の場所にある鍵や
known_hosts を temt が動かすことはなく、`destroy` もそのとき変数が指す場所だけを
片づけるので、要るものは手で移す。
