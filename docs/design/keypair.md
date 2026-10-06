# キーペアと手元の鍵

`apply` の手順 5。EC2 のキーペア `<KeyName>` と、設定ファイルの `IdentityFile` が
指す手元の鍵を揃える。`destroy` はキーペアを消さない。キーペアを挙げる、見る、
作る、消すのは `keypair` のコマンドである
（[design.md](../design.md#keypair-のコマンド)）。

キーペア `<KeyName>` はサービスのリージョンに登録する。インスタンスにログイン
できる鍵は、EC2 が起動時に入れるこのキーペアの鍵だけとする。手元の鍵は設定ファイル
の `IdentityFile`（既定 `~/.ssh/temt/keys/<KeyName>`）で、このキーペアと同じ鍵で
なければならない。パスフレーズ付きの鍵は扱わない。秘密鍵から公開鍵を取り出せない
ので、手がかりにも挙げず、`IdentityFile` に書かれていればエラーにする。`apply` は
次の 2 つを順に確かめ、差分を検出する。作るのと登録するのは、確認のあとの更新で
行う。

1. EC2 にキーペア `<KeyName>` があること。無ければ、`IdentityFile` の鍵の公開鍵を
   `ssh-keygen -y` で取り出し、`import-key-pair` でその名前で登録して、
   `temt:keypair=<KeyName>` を付ける。`IdentityFile` の鍵も無ければ、
   `ssh-keygen -t ed25519` でパスフレーズ無しの鍵をその場所に作ってから登録する。
   既定のままなら、初回の `apply` で `~/.ssh/temt/keys/temt` と `temt.pub` ができる。
   既にあるキーペアにはタグを足さない。
2. `IdentityFile` の鍵と EC2 のキーペア `<KeyName>` のフィンガープリントが一致する
   こと。手元は `ssh-keygen -y` で秘密鍵から取り出した公開鍵、EC2 は
   `describe-key-pairs --include-public-key` で取った公開鍵を使い、どちらも
   `ssh-keygen -l` が表示するものと同じ SHA256（`SHA256:` で始まる形）で計算する。
   `describe-key-pairs` の `KeyFingerprint` は使わない。一致しなければ、また
   `IdentityFile` の鍵が無ければ、「鍵の不一致」として扱う。

## 鍵の不一致

設定ファイルの `KeyName` と `IdentityFile` が互いに合っていない、設定ファイルの誤り
として扱う。`apply` は EC2 のキーペアを変えず、秘密鍵をキーペアから作り直すことも
できないので、揃えられない。検出の段階で、表示と確認の前に終了する。`diff` も同じ
エラーで終わる。

終了する前に、キーペアの名前と両方のフィンガープリントを表示する。直し方の手がかりと
して、`~/.ssh/temt/keys/` にあるすべての秘密鍵と `~/.ssh/id_*`（`.pub` を除く）の
うち、EC2 のキーペアとフィンガープリントが一致するものを挙げる。エラーでは、設定
ファイルの `IdentityFile` をそのどれかに直すか、`KeyName` を新しい名前にして
`apply` をやり直すよう告げる。`KeyName` を新しい名前にすれば、上の 1 で新しい鍵が
作られて登録され、`terminate` して次に `start` したときから使われる。`apply` が
`IdentityFile` を書き換えることはない。初回の `apply` は確認の前に止まるので、設定
ファイルはまだ無い。そのときは、`IdentityFile` か `KeyName` を書いたファイルを用意
するよう告げる。
