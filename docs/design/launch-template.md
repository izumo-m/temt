# 起動テンプレート

`apply` の手順 6 で揃え、`bake` の手順 8 で `ImageId` を替え、`destroy` の手順 4 で
消す。名前は `<名前>-template`。`start` はその既定バージョンから起動する。

## 設定

- `ImageId`: 設定ファイルの `ImageId` が指す AMI の ID。名前で書かれていれば
  `apply` が ID を引く（[ami.md](ami.md)）
- `InstanceType`: 設定ファイルの `InstanceType`
- `KeyName`: 設定ファイルの `KeyName`
- `BlockDeviceMappings`: 設定ファイルに `VolumeSize` があるときだけ入れる。1 つ
  だけで、`DeviceName` は `ImageId` の AMI の `RootDeviceName`、`Ebs.VolumeSize` は
  `VolumeSize`。ほかの項目（ボリュームの種類、スナップショット、
  `DeleteOnTermination` など）は入れず、AMI のものを使う
- `InstanceInitiatedShutdownBehavior=terminate`
- `CreditSpecification.CpuCredits=unlimited`: `InstanceType` が T 系（`t` と 2 から 9
  の数字で始まる型。`t3.micro`、`t4g.small` など）のときだけ入れる。CPU クレジット
  は T 系でしか意味を持たないので、ほかの型と、CPU クレジットを持たない旧世代の
  `t1` では項目を入れない
- ネットワークインターフェース（`DeviceIndex=0`）: 設定ファイルの `SubnetId`、
  セキュリティグループ `<名前>`、`AssociatePublicIpAddress=true`。サブネットに
  IPv6 の CIDR が結び付いていれば（`Ipv6CidrBlockAssociationSet` に `associated` の
  ものがあれば）`Ipv6AddressCount=1` を加え、無ければ加えない
- `MetadataOptions.HttpTokens=required`、
  `MetadataOptions.InstanceMetadataTags=enabled`
- `IamInstanceProfile`: `<名前>`
- インスタンス、ボリューム、ネットワークインターフェースのタグ: `Name`、
  `temt:service`

設定ファイルから来るのは `ImageId`、`InstanceType`、`KeyName`、`SubnetId`、
`VolumeSize` の 5 つで、残りは temt が決める。temt が知らない項目をテンプレートへ
渡す手段は無い。

## バージョン

テンプレートが無ければ、この内容でバージョン 1 を作り、既定バージョンにする。
あれば、既定バージョンの内容と比べ、違いがあれば新しいバージョンを作って既定に
する。比べるのは上に挙げた項目だけで、AWS が補った項目は見ない。同じなら新しい
バージョンは作らない。テンプレート自身のタグ（`temt:service`）は、バージョンとは
別に揃える。無いか値が違えば書き、そのときは中身が同じでも更新したと知らせる。
`bake` は比べずに、既定バージョンの内容の `ImageId` だけを替えた新しいバージョンを
作って既定にする。`create-launch-template-version` に、`SourceVersion` として既定
バージョンを、`LaunchTemplateData` として `ImageId` だけを渡す。

新しいバージョンを既定にしたら、それ以外のバージョンをすべて消し、テンプレートには
1 バージョンだけを残す。消せなければ警告して続ける。`bake` も同じである。`bake` は
新しい AMI が `available` になってからテンプレートを替える
（[design.md](../design.md#クライアントのコマンド) の `bake` の手順 7、8）。
既定を切り替えた直後に中断したときや、消せなかったときは既定以外のバージョンが
残るので、`apply` は新しいバージョンを作らないときも、temt 以外が作ったものも
含めて既定以外のバージョンを消す。前のバージョンへ戻す手段は無く、戻すなら
設定ファイルを戻して `apply` する。動いているインスタンスには影響せず、次の
`start` から新しい既定バージョンが使われる。
