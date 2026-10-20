# temt

temt は、使い捨ての EC2 インスタンスにサービスを載せるツールである。インスタンスは
用途ごとに 1 台ずつ用意し、必要なときだけ手元のクライアントから立てて、使い終わったら
畳む。一緒に配るアプリ `idle-shutdown` を入れて焼けば、誰も使わない時間が続いた
インスタンスは自分で終了するので、使っていない間はインスタンスの費用がかからない。

用途のひとつひとつを *サービス* と呼ぶ。他のサーバーへ入るための踏み台、使い捨ての
Ubuntu 作業環境、使っている間だけ動かして状態は S3 や DynamoDB に置くアプリケー
ションなどがそれにあたる。サービスごとに設定ファイル、起動テンプレート、インスタンスを
1 つずつ持ち、クライアントは名前でそれを選ぶ。同じ用途を設定違いで複数立てるときは、
設定ファイルをコピーして別の名前のサービスにする。

## 必要なもの

- Linux（Ubuntu 24.04 / 26.04 で動きを確かめている）
- mise か uv
- aws-cli v2 と session-manager-plugin
- ssh と ssh-keygen（openssh-client）
- EC2、IAM、SSM と EC2 Instance Connect を操作できる AWS の資格情報

詳しくは [usage.md](docs/usage.md#用意するもの) にある。

## 導入

mise で入れるときは、次を実行する。python（3.12 以上）と uv も mise で入れる。

    mise use -g python uv
    mise use -g pypi:izumo-m/temt

uv で入れるときは、次を実行する。

    uv tool install git+https://github.com/izumo-m/temt

更新と消し方は [usage.md](docs/usage.md#入れ方) にある。

## 使い方の例

サービス `dev` を用意し、起動してログインし、使い終わったら終了する。

    temt apply --svc dev    # 設定ファイル、鍵、AWS リソース、ssh の設定、コマンド temt-dev を用意する
    temt-dev start          # インスタンスを起動する
    ssh temt-dev            # Session Manager 経由でログインする
    temt-dev terminate      # インスタンスを終了する

`temt-dev` は、`apply` が `~/.local/bin` に置くコマンドで、`temt <操作> --svc dev`
と同じである。`~/.local/bin` は PATH に入れておく。続きから使うなら、`terminate` の
代わりに `temt-dev stop` で止めておき、次の `temt-dev start` で再開する。

## できること

- 設定ファイルから、インスタンスロール、セキュリティグループ、起動テンプレートを揃える。
  `apply` の前に、何が変わるかを `diff` で確かめられる
- Session Manager 経由で、受信規則を足さずに ssh でログインする。パブリック IP
  アドレスへの直接接続もできる
- 整えたインスタンスを AMI に焼き、次の起動から使う。使わなくなった AMI も片づける
- ふだんは止めておく既存の EC2 インスタンスを取り込み、使うときだけ起動する
- アプリをインスタンスに入れ、アプリが使う値を手元から読み書きする。誰も使わない
  時間が続けばインスタンスを終了するアプリ `idle-shutdown` を一緒に配る
- サービス、インスタンス、AMI を表で一覧する

## 費用について

temt は自分の AWS アカウントにリソースを作るので、動いているインスタンス、
ディスク（EBS）、AMI のスナップショットなどに AWS の料金がかかる。

## 文書

- 使い方: [docs/usage.md](docs/usage.md)
- 設定ファイル: [docs/conf.md](docs/conf.md)
- アプリの作り方: [docs/apps.md](docs/apps.md)
- temt と一緒に配るアプリ: [docs/apps/](docs/apps/)
  - 自動終了 `idle-shutdown`: [docs/apps/idle-shutdown.md](docs/apps/idle-shutdown.md)
  - swap を足す `swapfile`: [docs/apps/swapfile.md](docs/apps/swapfile.md)
  - タイムゾーンを日本時間にする `timezone-tokyo`:
    [docs/apps/timezone-tokyo.md](docs/apps/timezone-tokyo.md)

## 開発に参加する

[CONTRIBUTING.md](CONTRIBUTING.md) にある。

## ライセンス

[MIT](LICENSE)
