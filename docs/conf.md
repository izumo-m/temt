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

このファイルには、既定から変えたいものだけを書く。既定のある項目は、書いていなければ
`apply` と `diff` のたびに既定で埋める（[書かなければ既定で埋める項目](#書かなければ既定で埋める項目)）。
`apply` は、すでにあるファイルを書き換えない。ファイルが無いときにだけ、雛形を作る
（[雛形](#雛形)）。

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
- ポリシー（`[[role.<ポリシー名>]]`）も、共通ファイルの Statement の後ろに
  `<svc>.toml` の同じ名前の Statement をつなげる。`ManagedPolicyArns` は、`<svc>.toml` に
  書けば共通ファイルのものを置き換える。
- `[launch-template]` は項目ごとに重ねる。`NetworkInterfaces` は `DeviceIndex`、
  `BlockDeviceMappings` は `DeviceName`、`TagSpecifications` は `ResourceType`、タグは
  `Key` が同じ要素どうしを、要素の中の項目ごとに重ね、違う要素は両方を使う。たとえば、
  共通ファイルに書いたインスタンスのタグ `Owner` と、`<svc>.toml` に書いたインスタンスの
  タグ `Env` は、両方付く。ほかの配列（`Groups` など）は、`<svc>.toml` に書けば共通
  ファイルのものを丸ごと置き換える。`IamInstanceProfile` も、`<svc>.toml` に書けば
  共通ファイルのものを丸ごと置き換える。
- 空の値は書かれていないのと同じなので、共通ファイルの値を `<svc>.toml` で消すことは
  できない。共通ファイルを使わないサービスは、`<svc>.toml` に `Defaults = false` と
  書く（[最上位](#最上位)）。
- 共通ファイルを直せば、すべてのサービスに効く。AWS に反映するのは、サービスごとの
  `apply` である。共通ファイルに `ImageId` を書くと、すべてのサービスの AMI が固定
  される（[`ImageId`](#imageid)）。`[role]` を書くと、取り込んだサービスも含めて
  `Defaults = false` でないすべてのサービスにインスタンスロール `temt-<svc>` ができる。インスタンスに付くかは、
  サービスごとの `IamInstanceProfile` で決まる（[`[role]`](#role)）。
- `--svc` を付けない `instance list`、`ami list`、`ami prune`、`ami delete` も、
  共通ファイルの `AwsProfile` と `AwsRegion` を使う。環境変数 `AWS_PROFILE` や
  `AWS_REGION` より優先する。

## 項目

### 最上位

表の「読むコマンド」は、その項目の値を読むコマンドである。このほか `service list` が、
表示のためにすべての項目を読む。

| 項目 | 読むコマンド | 既定 |
| --- | --- | --- |
| `AwsProfile` | `service list`、`sg list`、`sg add`、`sg remove` を除く、`--svc` を付けたすべて。共通ファイルのものは、`--svc` を付けない `instance list`、`ami list`、`ami prune`、`ami delete` も読む | `--profile`、共通ファイルの `AwsProfile` の順に探す。どれも無ければ AWS のプロファイルを指定せず、資格情報は AWS の既定の規則で決まる |
| `AwsRegion` | `service list`、`sg list`、`sg add`、`sg remove` を除く、`--svc` を付けたすべて。共通ファイルのものは、`--svc` を付けない `instance list`、`ami list`、`ami prune`、`ami delete` も読む | `--region`、共通ファイルの `AwsRegion`、環境変数 `AWS_REGION`、`AWS_DEFAULT_REGION`、AWS のプロファイルの `region` の順に探し、どれも無ければ `ap-northeast-1` |
| `Defaults` | `--svc` を付けたすべて | `true`。`false` なら共通ファイルを読まない。共通ファイルには書けない |
| `WaitForSsm` | `start` | `true`。`false` なら、`start` は SSM エージェントを待たない |

**`AwsProfile`。** `<svc>.toml` に書けば、そのサービスはいつもその AWS のプロファイルを
使う。違う値の `--profile` を付けると、引数の誤りになる。書かなければ、コマンドの
たびに上の順で決める。`--profile` はその実行のあいだだけ効き、`apply` も
`service import` もこの項目を書かない。

環境変数 `AWS_PROFILE` は、`--profile` も `AwsProfile` も無いときに使われる。
`AWS_DEFAULT_PROFILE` と両方に別の値を入れると、temt は `AWS_DEFAULT_PROFILE` を使う。
aws-cli v2 を単独で使うときは `AWS_PROFILE` が先なので、両方を使うなら同じ値にする。

**`AwsRegion`。** `<svc>.toml` に書けば、そのサービスはそのリージョンに固定される。
違う値の `--region` を付けると、引数の誤りになる。書かなければ、コマンドのたびに
上の順で決める。`apply` も `service import` もこの項目を書かない。同じ設定ファイルを
別のリージョンで `apply` すると、そのリージョンにクローンができる
（[usage.md](usage.md#別のリージョンにクローンを作る)）。

`<svc>.toml` か共通ファイルに `AwsRegion` を書いてあれば、環境変数ではリージョンが
切り替わらない。環境変数で切り替えるなら、どちらにも書かない。`<svc>.toml` の
`AwsRegion` を書き換えると、前のリージョンのクローンは、戻すまでそのサービスのコマンドでは
扱えない（`--svc` を付けない `instance list` や `ami list` に `--region` を付ければ見える）。
共通ファイルに書いたときは、`--region` を付ければ扱える。

ログインに使う鍵は、このファイルには書かない。手元の ssh の設定の
`IdentityFile` で決まる（[usage.md](usage.md#ログインの鍵)）。

**`Defaults`。** `Defaults = false` と書けば、このサービスは共通ファイルを読まず、
このファイルだけで決まる。取り込んだサービスを元のインスタンスと同じに保ちたいときなど、
共通ファイルの値を効かせたくないサービスに書く。

**`WaitForSsm`。** `WaitForSsm = false` と書けば、`start` はインスタンスが動き出した
（`running` になった）ところで終わり、SSM エージェントを待たない。SSM エージェントが
登録されないインスタンスのためのもので、`service import` は、元のインスタンスが SSM に
登録されていなければこれを書く。Session Manager 経由の `ssh`、`reboot`、`set-port` は
SSM エージェントを使うので、そうしたインスタンスでは失敗するか、締め切りで止まる。

### `[launch-template]`

起動テンプレートの中身。AWS の起動テンプレートの項目を、`aws ec2
get-launch-template-data` の出力（`create-launch-template` の `--launch-template-data`
に渡す JSON）と同じ名前と入れ子で書く。変えたら `apply` を実行する。動いている
インスタンスは変わらず、次の `start` から反映される。

起動テンプレートの中身は、ここと共通ファイルに書いた項目、書いていない項目の既定（下）、
temt が足すもの（下）だけになる。行を消せば（共通ファイルにも無ければ）、次の `apply` で
起動テンプレートからも消えるか、既定に戻る。AWS のコンソールなどで起動テンプレートを
手で変えても、次の `apply` でここに書いたとおりに戻る。`ImageId` だけは例外で、
書いていなければ起動テンプレートの値をそのまま使う（[`ImageId`](#imageid)）。

```toml
[launch-template]
InstanceType = "t3.small"
IamInstanceProfile.Name = "${temt:name}"

[[launch-template.BlockDeviceMappings]]
Ebs.VolumeSize = 30    # DeviceName を省いたので、ルートボリューム

[launch-template.MetadataOptions]
HttpPutResponseHopLimit = 2
```

AWS の項目の値は、次の TOML の型で書く。値は書いたまま AWS に渡る（`UserData` も
base64 のまま書く）。知らない項目名と型の違う値はエラーになる。書ける項目は、temt が
使う boto3 の版で決まる。

- まとまり（`MetadataOptions` など）はテーブル。ドット付きのキー
  （`Ebs.VolumeSize = 30`、`IamInstanceProfile.Name = "..."`）や、インラインテーブル
  （`{ Name = "..." }`）で書いてもよい。
- まとまりの並び（`NetworkInterfaces` など）はテーブルの配列、文字列の並び（`Groups`
  など）は文字列の配列。
- 文字列、整数、真偽値、小数、日時は、TOML の同じ型。

AWS が読み出しのときにだけ返す 4 つの項目（`MetadataOptions.State` など）は書けない。
項目どうしの組み合わせ（`SecurityGroupIds` と `NetworkInterfaces` の `Groups` など）は
temt では確かめず、AWS が断る。

#### 書かなければ既定で埋める項目

次の項目は、書いていなければ、`apply` と `diff` のたびに既定で埋めて起動テンプレートに
入れる。`apply` はこのファイルに書かない（`service import` は元のインスタンスの値を書く）。temt の版が上がって既定が変われば、次の `apply` の
差分に出る。入れたくない値は、別の値で書く（`CpuCredits = "standard"`、
`Ipv6AddressCount = 0` など）。

| 項目 | 既定 |
| --- | --- |
| `InstanceType` | `t3.micro` |
| `NetworkInterfaces` の `DeviceIndex = 0` の要素の `SubnetId` | そのリージョンの既定 VPC のサブネットのうち、`MapPublicIpOnLaunch` が true で、アベイラビリティゾーン名が辞書順で最も小さいもの |
| 同じ要素の `AssociatePublicIpAddress` | `true` |
| 同じ要素の `Ipv6AddressCount` | `1`。サブネットに IPv6 の CIDR が結び付いているときだけ入れる |
| `MetadataOptions.HttpTokens` | `required` |
| `MetadataOptions.InstanceMetadataTags` | `enabled`。アプリが、インスタンスメタデータのタグからサービスの名前を読めるようにする（[apps.md](apps.md#aws-を使う)） |
| `InstanceInitiatedShutdownBehavior` | `terminate`。インスタンスの中で電源を切ると、止まるのではなく終了する |
| `CreditSpecification.CpuCredits` | `unlimited`。`InstanceType` が T 系（`t3.micro` など）のときだけ入れる |

`SubnetId` と `Ipv6AddressCount` は、そのたびにそのリージョンの AWS を見て決める。
既定 VPC が無いか、パブリックなサブネットが無ければ、`apply` や `diff` は止まる。
そのときは `SubnetId` を書く。

`NetworkInterfaces` に `DeviceIndex = 0` の要素が無ければ、要素を足して入れる。その要素に
`NetworkInterfaceId` があれば、`SubnetId`、`AssociatePublicIpAddress`、
`Ipv6AddressCount` は入れない。

書いた `CpuCredits` と `Ipv6AddressCount` は、`InstanceType` やサブネットを変えても
そのまま残る。T 系でない型や IPv6 の無いサブネットと組み合わさると、次の `start` を
AWS が断る（[項目を消す](#項目を消す)）。

`ImageId` は、ここには入らない。書いていなければ起動テンプレートの値を保つ
（[`ImageId`](#imageid)）。

どの項目も、`apply`、`diff`、`bake`（`--bake` を付けた `deploy` と `app remove`、
`set-port` を含む）が読む。`SubnetId` は `sg diff` と `sg show` も読む。

#### temt が足すもの

次のものは、書いていなければ temt が `apply` のたびに決めて、起動テンプレートに入れる。
このファイルには書かない。ファイルをコピーして別のサービスを作っても、元のサービスの名前を
引き継がないようにするためである。

- インスタンス、ボリューム、ネットワークインターフェースの `Name` タグ。`temt-<svc>`。
- `DeviceIndex = 0` の要素の `Groups`。temt が作るセキュリティグループ `temt-<svc>`。
  最上位に `SecurityGroupIds` か `SecurityGroups` を書いたときと、要素に
  `NetworkInterfaceId` を書いたときは入れない。

インスタンス、ボリューム、ネットワークインターフェースには、書いても書かなくても、
タグ `temt:service=<svc>` が付く。temt はこのタグでサービスのインスタンスを見分ける。

#### `ImageId`

ふつうは書かない。書いていなければ、`apply` は起動テンプレートの `ImageId` をそのまま
使い、差分にしない。`bake`（`--bake` を付けた `deploy` と `app remove`、`set-port` を
含む）は、そのリージョンの起動テンプレートの `ImageId` だけを焼いた AMI に替え、
このファイルには触らない。

起動テンプレートを作る `apply` は、次の順に AMI を選ぶ。`apply` と `diff` は、選んだ
AMI の名前を表示し、Ubuntu 24.04 を選んだときはこのサービスの AMI が無いことも添える。

- `--image-id` に渡した AMI（名前か ID）。
- 自分のアカウントの AMI のうち、名前が `temt-<svc>-` に 10 桁の数字が続き、
  `available` か `pending` のもので、作成日時が最も新しいもの。それがまだ `pending`（コピーの途中など）なら、`apply` は
  止まる。終わってから実行し直す。
- Canonical の Ubuntu 24.04。

起動テンプレートがあっても、`--image-id` を付ければその AMI に替わる。前の AMI に
戻すときや、他のサービスの AMI から始めるときに使う。`diff` にも付けられる。
`destroy` すると起動テンプレートとともに選んだ AMI も忘れられ、次の `apply` は上の順で
選び直す。起動テンプレートが指す AMI の登録を解除すると（`ami delete --force` など）、
次の `start` が失敗し、`apply` も止まる。`--image-id` で選び直す。

`ImageId` を書けば、その AMI に固定される。値は AMI の ID か名前で、`ami-` に続けて
小文字の 16 進数が 8 桁か 17 桁なら ID、それ以外は名前として扱う。名前で指せるのは
自分のアカウントの AMI だけで、`*` と `?` は使えない。指している AMI の登録が解除されて
いるか使えない状態なら、`apply` はそう告げて止まる。固定しているあいだは、AMI を焼く
コマンド（`bake`、`set-port`、`--bake` を付けた `deploy` と `app remove`）は、何も
せずに引数の誤りで止まり、`--image-id` を付けても引数の誤りになる。焼くには、行を消す。
行を消すと、起動テンプレートの AMI をそのまま使う。`service import` は焼いた AMI の
名前を書くので、取り込んだサービスで焼くときも、この行を消す。

AMI の ID はリージョンごとに違う。別のリージョンにクローンを作るなら、`ImageId` を
書かないか名前で書き、AMI を同じ名前でそのリージョンにコピーする
（[usage.md](usage.md#別のリージョンにクローンを作る)）。

#### 項目ごとの説明

**`SubnetId`。** サブネットに IPv6 の CIDR が結び付いていれば、インスタンスには
IPv4 のパブリックアドレスに加えて IPv6 アドレスも付く。IPv4 の CIDR を持たない
サブネットは使えない。

**`Groups`。** セキュリティグループの ID のほか、名前も書ける。`sg-` で始まれば ID、
それ以外は名前として扱い、`apply` が `SubnetId` の VPC の中で ID に直す。その VPC に
無い名前はエラーになる。`${temt:name}` か `temt-<svc>` と書けば temt のセキュリティ
グループで、無ければ `apply` が作る。`Groups` を書けば、temt のセキュリティグループは
そこに書いたときだけ付く（[`[security-group]`](#security-group)）。

**`IamInstanceProfile`。** インスタンスに付けるインスタンスプロファイル。書いたまま
起動テンプレートに入る。書かなければ、インスタンスロール無しで起動する。そのとき
`WaitForSsm = false` でなければ、SSM エージェントが登録されず `start` が締め切りまで
待つことがあるので、`apply` と `diff` が警告する。

temt が作るインスタンスロールを付けるには、次のように書く。雛形と `role import` も
この形で書く。`"temt-<svc>"` と書いても同じものを指すが、ファイルを別のサービスに
コピーすると、コピーした先で元のサービスのインスタンスロールが付く。

```toml
[launch-template]
IamInstanceProfile.Name = "${temt:name}"
```

ほかのインスタンスプロファイルは、`IamInstanceProfile.Name = "my-role"` か
`IamInstanceProfile.Arn = "arn:aws:iam::123456789012:instance-profile/my-role"` と書く。
`[role]` とは別に決まり、`[role]` を書いていてもほかのインスタンスプロファイルを指せる
（[`[role]`](#role)）。temt のインスタンスプロファイルを指しているのに、どのファイルにも
`[role]` が無ければ、`apply` と `diff` は AWS に何もせずにエラーにする。

共通ファイルの `IamInstanceProfile` は `<svc>.toml` から消せない。そのサービスでだけ
外すには、`<svc>.toml` で別のインスタンスプロファイルを指すか、`Defaults = false` を
書く。

**`KeyName`。** ログインの鍵は temt が接続のたびに送るので、要らない。書けば（`service
import` は元のインスタンスの値をコピーする）、そのまま起動テンプレートに入り、そのキーペアの
鍵でもログインできる。キーペアはリージョンごとにあるので、クローンを作るリージョンにも
同じ名前で入れる。

**ルートボリュームの大きさ。** `BlockDeviceMappings` の要素の `DeviceName` を省けば、
temt が起動テンプレートの AMI のルートボリュームのデバイス名を入れる（このファイルには
書かない）。AMI を別の OS のものに替えても、ルートボリュームを指し続ける。
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

起動テンプレートの AMI のルートボリュームより小さい値は、AWS が起動を断るので書けない。
`apply` と `diff` は、そう告げて何も変えずに止まる。AMI に EBS のルートボリュームが
無ければ、大きさを決められないので、`Ebs.VolumeSize` は書けない。EBS は縮められない
ので、大きくして焼いた AMI は、そのあと行を消しても、焼いたときの大きさのまま
起動する。

**変数。** 値の中に `${temt:name}`（`temt-<svc>`）と `${temt:service}`（`<svc>`）を
書ける。`apply` が置き換えてから起動テンプレートに入れる（[`[role]`](#role) の変数と
同じ）。

#### 項目を消す

起動テンプレートから項目を消すには、行を消して `apply` する。共通ファイルにある項目は、
共通ファイルから消すか、このファイルに `Defaults = false` を書く。[書かなければ既定で
埋める項目](#書かなければ既定で埋める項目)は、行を消すと既定に戻るので、入れたくない
ときは別の値で書く。

`apply` や `start` を AWS が断るときは、AWS の誤りから、どの項目かが分かる。その行を
直すか消し、`diff` で確かめてから `apply` する。

#### 別のアカウントやリージョンで使うとき

ファイルにアカウントは書かないので、別のアカウントの AWS のプロファイルを `--profile` に
付けて `apply` を実行すれば、そのアカウントにサービスを新しく作る。`AwsRegion` を
書いていなければ、別のリージョンで `apply` すると、そのリージョンにクローンができる。

どちらの場合も、アカウントやリージョンの中のリソースを指す項目（`SubnetId`、ID で書いた
`ImageId` と `Groups`、`KeyName` など）は、そのままでは使えない。先に消すか書き換える。
別のアカウントでは、名前で書いた `ImageId` と、`${temt:name}` でない
`IamInstanceProfile` も使えない。`IamInstanceProfile` はリージョンには依らない。

`AwsProfile` を `<svc>.toml` に書いたサービスは、そのアカウントに固定される。別の
アカウントにも置くなら、別の `<svc>` の名前でファイルをコピーし、コピーした方の `AwsProfile` を
直す。

### `[security-group]`

temt が作るセキュリティグループ `temt-<svc>` の受信規則。`[[security-group.ingress]]` を
規則の数だけ繰り返して書く。`apply` が AWS 側の受信規則をここに書いたものに揃え、書いて
いない規則（手で足したものを含む）は消す。`[launch-template]` の `Groups` を書いて、
temt のセキュリティグループを使っていないサービス（`service import` で取り込んだサービス
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
プロファイルに入っている IAM ロールで、インスタンスはその権限で AWS を呼べる。
`<svc>.toml` か共通ファイルにポリシーか `ManagedPolicyArns` があれば、`apply` は
インスタンスロールとインスタンスプロファイル `temt-<svc>` を作り、ここに書いたとおりに
揃える。中身の無い `[role]` は、書いていないのと同じである。インスタンスに付けるかは
`[launch-template]` の `IamInstanceProfile` で決まり、付けるには
`IamInstanceProfile.Name = "${temt:name}"` と書く（[`IamInstanceProfile`](#項目ごとの説明)）。
ほかのインスタンスロールの権限を temt で扱うには、`role import` で temt の管理に移す
（[usage.md](usage.md#インスタンスロールを-temt-の管理に移す)）。

インスタンスロールとインスタンスプロファイルは、どのリージョンのクローンでも同じ
`temt-<svc>` を使う。`[role]` を変えて `apply` すると、すべてのリージョンのクローンに
一度に効く。リージョンで違うリソースを指すときは、ARN のリージョンを `*` にするか、
リージョンごとの ARN を並べる。

どのファイルからも `[role]` を消すときは、`IamInstanceProfile` が temt の
インスタンスプロファイルを指したままだとエラーになるので、その行も消すか替えてから
`apply` する。ほかのリージョンで `[role]` のある設定を `apply` したままでなければ、
インスタンスロールとインスタンスプロファイルも消える。どうなるかは `diff` と確認に出る。
外し方と、戻すときの注意は [usage.md](usage.md#インスタンスロールを外す) にある。

名前を付けたポリシーを `[[role.<ポリシー名>]]` に書く。`[[role.<ポリシー名>]]` の
1 つが IAM のポリシーの Statement 1 つで、同じ名前で繰り返した並びが 1 つのポリシーに
なる。共通ファイルにも同じ名前があれば、共通ファイルの Statement の後ろにつながる。
`apply` が、名前ごとにインスタンスロールのインラインポリシー `temt-<ポリシー名>` を
作る。名前が `temt-` で始まるインラインポリシーは temt のものとみなし、設定ファイルに
無ければ、手で付けたものでも `apply` が消す。変えたら `apply` を実行する。動いている
インスタンスにも、IAM の反映を待てば効く。

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
使う。雛形は、temt が動くのに要る次の既定を書く。SSM エージェントの権限と、自分の階層
`/temt-<svc>/` の SSM パラメータを読み書きする権限で、そのインスタンスで何をしたいかに
応じて足す。temt がこの既定を足すことはなく、インスタンスロールに付くのは書いたポリシーだけである。

```toml
[[role.ec2]]
Action = [
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
]
Resource = "*"

[[role.ec2]]
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
  ので、`/*` と並べて書く。リージョンを `*` にしてあるので、どのリージョンの
  クローンでも使える。`SecureString` を使うなら、既定の鍵 `aws/ssm` で
  暗号化したものは KMS の権限を足さずに読み書きできる。自分で作った鍵を使うときは、
  その鍵への `kms:Encrypt` と `kms:Decrypt` をここに足し、鍵ポリシーでもインスタンス
  ロールに許す。パラメータの層（tier）が Advanced なら、`kms:GenerateDataKey` も足す。

足すときは、既定の 2 つの Statement を残す。1 つ目が無ければ、SSM エージェントが
登録されないことがあり、そのときは `start` が締め切りで止まり、Session Manager 経由の
ssh も `reboot` も `set-port` も効かない。2 つ目が無ければ、アプリが値を読めず、状態を書けない。

アプリが使う権限は、アプリごとに別の名前で書く。`deploy` はインスタンスロールに触らないので、
権限を変える時点とアプリを入れ替える時点を分けられる。例えば、先に `apply` で
新旧のアプリの両方が使える権限にし、アプリを `deploy` してから、古い権限を消して
`apply` する。

<ポリシー名> は、英小文字で始まり、英小文字・数字・ハイフンが続き、英小文字か数字で
終わる、64 文字までの名前とする。

`[[role.<ポリシー名>]]` の中の項目名は、IAM の Statement のものをそのまま使う。

| 項目 | 読むコマンド | 値 |
| --- | --- | --- |
| `Effect` | `apply`、`diff` | `Allow` か `Deny`。省けば `Allow` |
| `Action`、`NotAction` | `apply`、`diff` | 操作。文字列か、文字列の配列 |
| `Resource`、`NotResource` | `apply`、`diff` | 対象の ARN。文字列か、文字列の配列 |
| `Condition` | `apply`、`diff` | 条件。IAM の書式のテーブル |
| `Sid` | `apply`、`diff` | Statement の名前。任意 |

```toml
[[role.jupyter]]
Action = ["s3:GetObject", "s3:PutObject"]
Resource = "arn:aws:s3:::my-bucket/${temt:name}/*"

[[role.jupyter]]
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
`role.<ポリシー名>` が空でないテーブルの配列であること、値が文字列、数、真偽値、
配列、テーブルのいずれかであること（日時、`nan`、`inf` は書けない）、知らない変数が無い
ことだけである。`[role.<ポリシー名>]` と角かっこ 1 つで書いたテーブル
（`[[role.<ポリシー名>.Statement]]` のように中に階層を作ったものも同じ）は、
`[[role.<ポリシー名>]]` と書くよう告げて誤りにする。書式と中身は、`apply` が
`Effect` の無い Statement に `Allow` を補い、変数を置き換えたあと、インスタンスロールに付ける
前に IAM Access Analyzer の `ValidatePolicy` にかけて確かめる。`ERROR` の指摘が
1 つでもあれば、すべてを、どのファイルのどのポリシーの何番目の Statement の項目かと
ともに挙げ、AWS に何も書かずに終了する。ほかの指摘（`SECURITY_WARNING`、`WARNING`、
`SUGGESTION`）は警告として挙げ、そのまま続ける。`[role]` があれば、`apply` を実行する
資格情報には `access-analyzer:ValidatePolicy` の権限が要る。

`Version` は temt が `2012-10-17` に決めるので書かない。インスタンスロールのインラインポリシー
は、分けても合わせて 10,240 文字までで、超えると `apply` は IAM の誤りで止まる。
`apply` は、新しいポリシーを書いてから、設定ファイルに無いポリシーを消す。消す
ポリシーと書くポリシーを合わせて上限を超えるなら、先に消すものだけを設定ファイル
から消して一度 `apply` してから、書き足す。

## 雛形

設定ファイルが無いとき、`apply` はサービスを作るかを確かめてから、雛形の設定ファイルを
作る。書くのは次のものだけで、ほかの項目は既定で埋まる。

- 先頭の説明のコメント。
- temt のインスタンスロールを付ける `IamInstanceProfile.Name = "${temt:name}"`。
- [`ec2`](#role) の既定。
- 受信規則とポリシーの例。コメントにしてある。

共通ファイルに `IamInstanceProfile` があれば、雛形にはその行を書かず、共通ファイルの値を
使う。共通ファイルの値が temt のインスタンスプロファイルを指していれば `ec2` の既定を
書き、ほかのものを指していれば書かない。

```toml
# Written by temt apply. Edit and run apply again to update.

[launch-template]
IamInstanceProfile.Name = "${temt:name}"

# Permissions of the instance role that are not tied to one app. temt needs
# the first two statements (SSM Agent; the parameters of this service). Keep
# them and add whatever else this instance may do.
[[role.ec2]]
Action = [
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
]
Resource = "*"

[[role.ec2]]
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
# [[role.example]]
# Action = ["s3:GetObject"]
# Resource = "arn:aws:s3:::example-bucket/${temt:name}/*"
```

最初の `apply` の前に決めておきたい項目があるときは、ファイルを先に作っておく。
そのときは雛形を作らないので、temt のインスタンスロールを付けるなら、
`IamInstanceProfile.Name = "${temt:name}"` と `ec2` の既定も自分で書く。

    mkdir -p ~/.config/temt
    cat > ~/.config/temt/<svc>.toml <<'EOF'
    [launch-template]
    InstanceType = "t3.small"
    IamInstanceProfile.Name = "${temt:name}"
    EOF

このあと、上の雛形の `[[role.ec2]]` をコピーして足す。

## 設定ファイルに書かないもの

次のものは、設定ファイルではなく環境変数で与える。`TEMT_LOGIN` は空なら無いのと同じに
扱う。

| 名前 | 読むコマンド | 値 |
| --- | --- | --- |
| `TEMT_LOGIN` | `proxy` | ssh でインスタンスへつなぐ経路。`ssm`（Session Manager 経由）か `direct`（パブリック IP へ直接）。無ければ `ssm`。それ以外の値はエラー |
| `TEMT_CONF_DIR` | すべて | 設定ファイルとアプリを置くディレクトリ。無ければ `~/.config/temt` |
| `TEMT_SSH_DIR` | `--svc` を付けたもののうち ssh の設定、鍵、ロックを使うもの（`apply`、`proxy` など）と、`ami prune` | ssh の設定・鍵・known_hosts を置くディレクトリ。無ければ `~/.ssh/temt` |
| `AWS_PROFILE` | AWS を使うすべて（`proxy` を含む） | `--profile` も `AwsProfile` も無いときに使う AWS のプロファイル（[最上位](#最上位)） |
| `AWS_REGION`、`AWS_DEFAULT_REGION` | AWS を使うすべて（`proxy` を含む） | `--region` も `AwsRegion` も無いときに使うリージョン（[最上位](#最上位)） |

`TEMT_LOGIN` は `proxy` が接続のたびに読む。ssh は ProxyCommand に自分の環境変数を
渡すので、シェルの初期化で決めることも、`TEMT_LOGIN=direct ssh <名前>`
のようにその接続だけ変えることもできる。`apply` は `TEMT_LOGIN` を見ない。
`TEMT_LOGIN`、`AWS_PROFILE`、`AWS_REGION` は ProxyCommand に書かれないので、ssh を
呼ぶエディター（VS Code の Remote-SSH など）にも渡るように設定する。

`--profile` と `--region` は、自分で打つ ssh の ProxyCommand には届かない。ssh でつなぐ先の
リージョンは、`AwsRegion` か環境変数で決まる（`AWS_REGION=us-west-2 ssh <名前>`）。

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

無ければ temt が作る。`TEMT_CONF_DIR` のディレクトリは、`apply` が雛形を
書くときと、`service import` が設定ファイルを書くときに作る。
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
