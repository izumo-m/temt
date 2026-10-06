# known_hosts と ssh の設定

`apply` の手順 7 が ssh の設定を書き、`proxy` が接続のたびに known_hosts を用意する。

## known_hosts

インスタンスは起動のたびに新しいホスト鍵を持つ。同じ Host 名に対して鍵だけが変わ
るので、known_hosts はインスタンスごとに分ける。

```
~/.ssh/temt/known_hosts.d/<名前>/<instance-id>   実体
~/.ssh/temt/known_hosts.d/<名前>/current         <instance-id> へのシンボリックリンク
~/.ssh/temt/known_hosts.d/<名前>/.lock           ロックファイル
```

ディレクトリのモードは 700、ファイルのモードは 600 とする。

`proxy` は、接続のたびに、ディレクトリが無ければ作り、`<instance-id>` のファイルを
追記モードで開いて閉じ、`current` をそれへ張り替える。追記モードなので、無ければ
空のファイルができ、あれば中身に触れない。同時に走った別の接続がホスト鍵を書き
込んだ直後でも、それを消さない。ssh は ProxyCommand を起動した後に known_hosts を
読むので、この張り替えは間に合う。

ホスト鍵を事前に取得することはしない。初めて見る `instance-id` のファイルは空なの
で、ssh はホストを未知として扱い、利用者の確認を経て鍵をそこに記録する。

ssh は、つなぐポート（`Port`、または `-p` の値）が 22 でなければ、known_hosts の
ホスト名を `[<名前>]:<ポート>` と書く。ファイルはインスタンスごとなので
支障はないが、動いているインスタンスのポートが変わると、次の接続で改めて
ホスト鍵の確認を求める。

## ssh ディレクトリの `config`

`apply` は、自分のサービスの `Host <名前>` ブロックの中で、temt が管理する次の
6 項目だけを書き換える。ブロックが無ければ足し、ファイルが無ければモード 600 で
作る。管理しない項目、他のブロック、コメントには手を付けない。

`Host` 行を空白で分けた語のどれかが `<名前>` と一致すれば、そのブロックをサービスの
ブロックとして扱う。`Host temt-dev-main dev` のように別名を並べて書いてもよい。
そうした `Host` 行が 2 つ以上あれば、ssh と同じく先頭のものを採る。

```
Host temt-dev-main
    User               ubuntu
    Port               22
    IdentityFile       ~/.ssh/temt/keys/temt
    IdentitiesOnly     yes
    UserKnownHostsFile ~/.ssh/temt/known_hosts.d/temt-dev-main/current
    ProxyCommand       env TEMT_CONF_DIR='~/.config/temt' TEMT_SSH_DIR='~/.ssh/temt' temt proxy --svc dev-main %p
```

- `User` は `ubuntu` で固定する。
- `Port` は設定ファイルの `SshdPorts` の先頭のポート（既定は 22）で、`ProxyCommand`
  の `%p` になる（[sshd-ports.md](sshd-ports.md)）。
- `IdentityFile` は設定ファイルの `IdentityFile`（既定は `KeyName` から導く）で
  ある。ssh は `%` をトークンとして展開するので、`%` は `%%` にして書く。ssh は
  語を空白で分けるので、空白を含む値は二重引用符で囲む。空白の無い値は囲まない。
  二重引用符の中でも ssh が解釈する文字は、設定ファイルを読むときに断る
  （[conf.md](../conf.md#最上位)）。`IdentitiesOnly yes` により、ssh はこの鍵だけを
  使い、ssh-agent の他の鍵を試さない。
- `UserKnownHostsFile` は `current` という固定の名前を指す。ssh_config には
  instance-id に展開されるトークンが無い。
- `ProxyCommand` は、`temt` をパスでなく名前で呼び、`proxy --svc <svc> %p` を渡す。
  `env` が ssh から受け継いだ PATH で `temt` を探すので、`temt` の入れ方や版が
  変わっても書き直さずに済む。`proxy` が呼ぶ aws-cli も、同じ PATH で探す。`%p` は
  `Port` の値で、`ssh -p` を付ければその値になる。`temt` の前に、`TEMT_CONF_DIR` と
  `TEMT_SSH_DIR` を、既定のままでも、`env` に渡す形でこの順に置く。ssh は
  ProxyCommand に自分の環境変数を渡すので、書かなければ `proxy` は ssh を打った
  シェルの値を読む。値はシングルクォートで囲む。ホームの下なら `~` で始まる形で
  書き、`proxy` が自分で展開する。ホームのパスそのものは ssh の設定に出さない。
  ssh は `ProxyCommand` を利用者のシェルの `exec` に渡すので、シェルの代入
  （`VAR=値 コマンド`）は使えない。
- `UpdateHostKeys` は書かない。書くと ssh が一時ファイルと改名で known_hosts を
  書き直し、`current` がシンボリックリンクでなくなる。`UserKnownHostsFile` を
  設定していれば、既定で無効である。

利用者がサービスに足したい設定（`LocalForward` など）は、このブロックに書き足せば
よい。temt が管理する 6 項目を手で変えても、次の `apply` で temt が決めた値に戻る。
`apply` は管理する項目の行を、書かれた値を読まずにいつも同じ形で書き直し、ファイルの
中身を比べて変わるときだけ書く。手で引用符を付けるなど、ssh には同じ値に読める行も
temt の形に戻る。

### conf ディレクトリをまたぐサービス

ssh ディレクトリは conf ディレクトリをまたいで 1 つなので、`Host <名前>` のブロックも
1 つしか持てない。`apply` は手順 1 で、既にあるブロックの `ProxyCommand` から、それを
書いた `apply` の conf ディレクトリを読む。`env` の `TEMT_CONF_DIR` の値があればそれ
（先頭の `~/` はホームに展開し、空なら既定、絶対パスにならなければ確かめない）、
無ければ既定の場所である。その場所が今の場所と違い（`..` とシンボリック
リンクを解いて比べる）、そこにこのサービスの設定ファイルがまだあれば、その場所を
挙げて何も書かずに終了する。同じ名前の定義が 2 つあると、ssh の接続先も AWS の
リソースも後の `apply` のもので上書きされるからである。設定ファイルが無ければ、
場所を移したものとみなして進む。今の場所のファイルと実体が同じ（シンボリック
リンクなど）なら、定義は 1 つなので進む。前の場所を調べられなければ終了する。
ブロックか `ProxyCommand` が無ければ確かめない。

## `~/.ssh/config` の `Include`

`~/.ssh/config` の先頭に、次の 2 行を置く。`Host` や `Match` のブロックより前に
無いと、その後の設定として読まれてしまう。

```
# ===== temt ===== do not remove; 'temt apply' owns the next line
Include ~/.ssh/temt/config
```

`apply` は、1 行目の目印を手がかりに 2 行目を自分のものと見なし、ssh ディレクトリが
変われば書き換える。書き換える前の行が指していたファイルがまだあり、`Host` 行を
持っていれば、ssh はそこを読まなくなるので、そのファイルと `Host` の名前を挙げて
警告する。新しい `config` にも同じ名前がある `Host` と、ワイルドカード（`*`、`?`）や
否定（`!`）のパターンは挙げない。挙げるものが無ければ警告しない。前のファイルを
調べられないか読めないときも、警告せずに書き換える。

目印が無い `Include` は利用者のものとして残す。最初の `Host` か `Match` より後ろの
目印は、そこでは `Include` がそのブロックの設定としてしか読まれないので、無いものと
して扱う。目印が無ければ、目印とその行を先頭に足す。`~/.ssh/config` が
無ければモード 600 で作る。

ssh は `Include` のパスを glob（`*` などでファイル名を合わせるパターン）として
読むので、`*`、`?`、`[` はバックスラッシュで守って書く。`Host` ブロックの
`IdentityFile` などは glob にならないので、この 3 文字を守らない。
