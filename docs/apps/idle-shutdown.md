# 自動終了（idle-shutdown）

誰も使っていないインスタンスを、自分で終了させるアプリ。temt と一緒に配り、
`deploy` で入れて `bake` で焼く。ssh の接続や端末などが無い状態が制限時間（既定は
1 時間）続くと、インスタンスは自分で電源を切り、そのまま終了する。閉じ忘れた
インスタンスが費用を生み続けないようにするためのものである。

前半は使う人向けの説明、後半は設計である。アプリの作り方は
[apps.md](../apps.md) に、ソースは
[`src/temt/apps/idle-shutdown/`](../../src/temt/apps/idle-shutdown/) にある。

## 使い方

### 入れる

動いているインスタンスに入れ、続けて焼く。

    temt-<svc> start
    temt-<svc> deploy idle-shutdown --bake

焼くまでは、次に起動するインスタンスには入らない。`apply` や `start` は入れないので、
自動終了させたいサービスごとに入れて焼く。入れた直後から働き、`deploy` の出力の
終わりに、いまの状態（`idle-shutdown status`）が出る。インスタンスに aws-cli が
無ければ、`install` が snap で入れる（[状態を知らせる](#状態を知らせる)）ので、
初めての `deploy` は 1 分ほど長くかかり、AMI もその分大きくなる。

制限時間は、`deploy` のオプション `limit` で、サービスごとに決められる。`90m`、
`4h`、`1d`、`3600`（秒）のように 365 日までの期間で書き、`off` なら電源を切らない。
省けば 1 時間である。焼けば AMI に残り、そのサービスで起動するインスタンスの制限時間に
なる。次の `deploy` は、与えた値を引き継ぐ（[usage.md](../usage.md#アプリを入れる)）。

    temt-<svc> deploy idle-shutdown limit=4h --bake
    temt-<svc> deploy idle-shutdown limit= --bake   # 1 時間に戻す

### 使用中とみなすもの

次のどれかがあるあいだは、使用中とみなして終了しない。

- ssh の接続。端末の無い接続（`ssh -N` のトンネル、VS Code の Remote-SSH、`deploy`）
  も含む
- 端末。デタッチした screen や tmux も、端末を持ち続けるので含む
- `systemd-inhibit --what=shutdown` のロック（モード `block`）。誰が取ったものでも
  数える。root 以外が取るには `sudo` が要る
- 使用中と答える判定。ほかのアプリが `/etc/idle-shutdown/in-use.d/` に置く
  （[アプリから使用中を知らせる](#アプリから使用中を知らせる)）

これらが無くなり、最後に使われた時刻から制限時間がたつと終了する。判定は 5 分おき
なので、終了は制限時間が過ぎてから最大 5 分ほど遅れる。つないだまま放っておいた
ssh や、忘れた tmux があるあいだは終了しないので、要らなくなったら閉じるか
`terminate` する。

手元のマシンが接続を閉じずに落ちたときは、接続がインスタンスに残って使用中になる。
`idle-shutdown` は sshd に、応答の無い接続を 10 分で切らせるので、残るのは 10 分
ほどである。その代わり、手元の PC を 10 分より長くスリープさせると、ssh が切れる。
tmux の中身は残るので、つなぎ直せば続けられる。

### 状態を見る、制限時間を変える

インスタンスの上で、状態を見たり、制限時間を変えたりできる。変えた制限時間は、
再起動するかインスタンスが終わるまで、`deploy` で決めた制限時間より先に効く。
`status` は、実行した自分の接続を数えない。

    ssh temt-<svc> idle-shutdown status               # 制限時間、最後に使われた時刻、使用中の理由
    ssh temt-<svc> sudo idle-shutdown limit 4h        # 4 時間にする（365 日まで）
    ssh temt-<svc> sudo idle-shutdown limit off       # 終了しない
    ssh temt-<svc> sudo idle-shutdown limit default   # deploy で決めた制限時間に戻す

root でない `status` は in-use.d の判定を見ない。見るには `sudo` を付ける。

    limit:        1h (set at deploy)
    last active:  2026-09-29 05:12:03 UTC (18m ago, by pam)
    in use:       no (not counting this session)
    in-use.d:     not checked; run sudo idle-shutdown status to include it
    power off:    1h after you log out

`limit` の括弧の中は、制限時間の出どころである。`set until reboot` は `limit` で
変えたもの、`set at deploy` は `deploy` で決めたもの、`default` はどちらも無いときの
1 時間である。

自分の接続のほかに使われていなければ、`power off` はログアウトから制限時間の後と
出る。ssh を通さずに実行したときは、電源を切る時刻と、それまでの時間が出る
（`2026-09-29 06:12:03 UTC (in 42m)`）。時刻を過ぎていれば、次の判定で切ると出る
（`2026-09-29 06:12:03 UTC (at next check)`）。時刻はインスタンスの時計のローカル
時刻で、タイムゾーンの名前を添える。Ubuntu の既定では UTC である。

### 手元から状態を見る

インスタンスにつながずに、手元から電源を切る予定を見られる。

    temt-<svc> status

`app status` の `idle-shutdown` の行に、5 分おきの判定の結果が出る
（[usage.md](../usage.md#状態を見る)）。

    app status:
      idle-shutdown  power off in 42m (at 2026-09-29 06:12:03 UTC; idle 18m since pam, limit 1h)  [3m ago]

- `power off in 42m (at ...; idle 18m since pam, limit 1h)`: 使われておらず、
  このままなら `at` の時刻に電源を切る。時刻はインスタンスのローカル時刻で、
  タイムゾーンの名前を添える。Ubuntu の既定では UTC で、[timezone-tokyo](timezone-tokyo.md)
  を入れれば日本時間になる。`in` は判定した時点からの時間
  なので、末尾の経過時間の分だけ進んでいる。`since` は最終利用時刻を更新したもの。
- `in use: 2 ssh session(s); 1 terminal(s) (limit 1h)`: 使用中で、電源を切らない。
  `limit off` でも、使用中ならこちらが出る。
- `never powers off (limit off; idle 18m since pam)`: `limit off` にしてある。
- `power off at next check (idle 1h2m since pam, limit 1h)`: 制限時間を過ぎて
  いて、次の判定で切る。`limit` で制限時間を短くしたときに出る。
- `powering off (idle 1h2m since pam, limit 1h)`: 電源を切った。インスタンスが
  終了した後も残るので、いつ、なぜ終了したかが分かる。動いているインスタンスが
  書いた値ではなくなるので、末尾は `[2h ago, stale]` のようになる。

判定は 5 分おきなので、末尾の経過時間は 5 分ほどまでである。インスタンスが動いて
いるのに 10 分を超えていれば、タイマーが動いていないか、書けていない
（`journalctl -u idle-shutdown` に警告が出る）。ssh でつなぐと、次の判定でこの行が
`in use` に変わる。それより前に手元から見ても、つなぐ前の判定の結果が出る。
`start` してから最初の判定（起動の 5 分後）までと、`stop` や `bake` で止めたあとは、
前に動いていたときの最後の値が、そのときからの経過時間とともに出る。末尾に
`stale` が付くので、今の状態ではないと分かる。インスタンスの中から `shutdown`
したときや、EC2 のコンソールから終了したときは、`powering off` を書かずに止まる
ので、最後の判定の値（`in use ...` など）が `stale` 付きで残る。

### ログインのメッセージ

コマンドを付けずに ssh でログインすると、ログインのメッセージ（ログインしたときに
出る案内。Ubuntu の motd）に、自動終了の状態が出る。

```
idle-shutdown (sudo idle-shutdown limit 4h to change):
  limit:        1h (set at deploy)
  in use:       no (not counting this session)
  power off:    1h after you log out
```

中身は `idle-shutdown status` から、最後に使われた時刻と、in-use.d を見ていない
ことを告げる行を除いたもので、ログインした自分の接続は数えない。ほかのアプリが
in-use.d に置いた判定は見ないので、それも含めた状態は `sudo idle-shutdown status`
で見る。

次のときは出ない。

- コマンドを付けた ssh。`ssh -t temt-<svc> tmux new -A` のように `-t` で端末を
  付けても出ない
- 端末を開かない接続（トンネル、scp、sftp、VS Code の Remote-SSH など）
- ControlMaster で 1 本の接続に相乗りした接続。ただし、親の接続を `-N` で開いた
  ときは、最初に相乗りしたログインに、親の接続を開いたときの状態が出る
- ホームに `~/.hushlogin` を置いたユーザー
- ssh を通さない Session Manager のシェル

### 気をつけること

ログアウトしても続けたい長い処理は、screen か tmux の中で動かしてデタッチする。
端末が残るので、処理が続くあいだは使用中とみなす。終わる時刻の見当がつくなら、
`sudo idle-shutdown limit` で制限時間を延ばしてもよい（[状態を見る、制限時間を
変える](#状態を見る制限時間を変える)）。

`bake` はインスタンスにつながないので、使用中には数えられない。最後に使ってから
制限時間が過ぎる間際に `bake` すると、確認を待つあいだに電源が切れ、焼く前の中身
ごと終了することがある。間際なら、先に `ssh temt-<svc> idle-shutdown touch` で
延ばす。

`systemd-inhibit` のロックがあるあいだは、`terminate`、`stop`、`bake` による停止も、
`reboot` による再起動も断られて遅れる。先に、ロックを取った処理を止める。

### アプリから使用中を知らせる

ssh の接続や端末は、アプリが何もしなくても使用中に数えられる。ssh のポート転送で
使うアプリなら、転送があるあいだは使用中である。次のようなアプリは、使われている
ことを自分で知らせる。

- セキュリティグループで開けたポートで直接使われるアプリ
- ジョブを処理するアプリ

HTTP のリクエストのように一瞬で終わる利用は、5 分おきの判定では見えない。アプリは、
利用者に使われるたびに `/run/idle-shutdown/touch/<アプリの名前>` の更新時刻を
いまにする。このディレクトリは誰でも書けるので、root でなくてよい。ファイルは
最初に作ったユーザーの持ち物になるので、同じ名前は同じユーザーだけが使う。
FastAPI ならミドルウェア（リクエストのたびに間に入って動く処理）で済む。

```python
KEEPALIVE = Path("/run/idle-shutdown/touch/myapp")


@app.middleware("http")
async def keepalive(request, call_next):
    if request.url.path != "/health":  # 死活監視は利用に数えない
        try:
            KEEPALIVE.touch()
        except FileNotFoundError:  # idle-shutdown が入っていない
            pass
        except OSError as e:  # ほかのユーザーのファイルなど。リクエストは止めない
            logger.warning("cannot touch %s: %s", KEEPALIVE, e)
    return await call_next(request)
```

シェルからは `idle-shutdown touch myapp` でもよい。死活監視（外からの定期的な生存
確認）や bot のアクセスで更新すると、インスタンスがいつまでも終了しないので、
利用者の操作だけで更新する。

キューに残ったジョブのように、続いている状態を使用中として知らせるときは、
`/etc/idle-shutdown/in-use.d/` に実行ファイルを置く。ファイルは 5 分おきに root で
実行される。終了コード 0 で何か出力すれば使用中とみなされ、出力は
`sudo idle-shutdown status` に理由として出る。ファイルは次のように置く。守らない
ものは実行されない。

- 名前は英数字、`_`、`-` だけにする（`.sh` を付けると実行されない）
- root の持ち物にして、グループと他人が書けないようにする

```sh
#!/bin/sh
# /etc/idle-shutdown/in-use.d/queue: キューにジョブが残っていれば使用中
n=$(ls /var/spool/myjobs | wc -l)
[ "$n" -eq 0 ] || echo "$n job(s) queued"
```

`install` を root で実行するなら（`manifest.toml` の `user` も
`deploy --user` も使わないなら）、`install` の中で `install -D -m 755` を使って
置けば、idle-shutdown がまだ入っていなくても失敗しない。`uninstall` で消す。root
以外で実行するなら、パスワードなしで sudo できるユーザー（`ubuntu` など）で
実行し、`sudo install -D -m 755` で置いて、`uninstall` では `sudo rm -f` で
消す。

### 外す

動いているインスタンスから外し、続けて焼く。

    temt-<svc> start
    temt-<svc> app remove idle-shutdown --bake

焼くまでは AMI に残り、次に起動するインスタンスにも入っている。ほかのアプリが
in-use.d に置いた判定は消さない。

## 設計

電源を切ると、起動テンプレートの `InstanceInitiatedShutdownBehavior` が `terminate`
なので、インスタンスは終了する。

### 置くもの

`install` は、はじめに `deploy` のオプション `limit` の値を、送られた本体の
`limit --deploy` で確かめて `/etc/idle-shutdown/limit` に書く。続けて、`aws` が、本体が
探す 3 か所（[状態を知らせる](#状態を知らせる)）のどこにも無ければ、
`snap wait system seed.loaded` で snapd の初期化（起動時に snap を配置する処理）を
待ってから、`snap install aws-cli --classic` で aws-cli を入れる。そのあと次の表の
ファイルを置き、PAM の設定を足して、タイマーを有効にし、sshd に設定を読み直させる。
次のときは失敗する。

- `limit` の値を本体が受け付けないとき（365 日を超えるなど）。aws-cli や表の
  ファイルを入れる前に失敗する。値を直して `deploy` する。
- aws-cli を入れられなかったとき（snap のストアに届かないなど）。もう一度
  `deploy` する。
- `pam-auth-update` が `/etc/pam.d/common-session` に行を足さなかったとき。手で直した
  ファイルがあると、`pam-auth-update` は書かずに成功で終わる。`install` は、手での
  変更を戻すよう告げる。
- sshd の設定が `sshd -t` を通らないとき。`install` は、置いた sshd の設定を外して
  から失敗する。そのまま焼くと、次のインスタンスで sshd が起動しないからである。

タイマーを有効にしたのが起動から 5 分を過ぎたあとなら、タイマーはその場で 1 度
判定する。`install` は最後に `idle-shutdown report` で判定の結果を SSM パラメータに
書き、`idle-shutdown status` を実行する。入れ直したときも、新しい制限時間が次の
タイマーを待たずに手元の `temt-<svc> status` に出る。

`uninstall` は、PAM の設定を外してから、タイマーを止め、表のファイルと
`/run/idle-shutdown/` を消し、sshd に設定を読み直させる。PAM の設定を外しても
`/etc/pam.d/common-session` に行が残っていれば、何も消さずに失敗する。行を残した
まま本体を消すと、ログインとログアウトのたびに無い本体を呼ぶからである。`install`
と同じく、手で直したファイルがあると `pam-auth-update` は書かずに成功で終わるので、
手での変更を戻すよう告げる。`/etc/idle-shutdown/in-use.d/` は、他のアプリが置いた
判定が残っていれば消さない。aws-cli は、他のアプリも使いうるので消さない。SSM
パラメータ `status` は、`uninstall` の後に `app remove` が消す。

| 場所 | 中身 |
| --- | --- |
| `/usr/local/bin/idle-shutdown` | 本体。インスタンスの `python3 -I` で動き、標準ライブラリだけを使う |
| `/etc/idle-shutdown/limit` | `deploy` のオプション `limit` で決めた制限時間。秒数か `off`。モード 644 で、`/etc/idle-shutdown/` が無ければモード 755 で作る |
| `/etc/systemd/system/idle-shutdown.service`、`.timer` | 本体を、起動の 5 分後から 5 分おきに root で実行する |
| `/etc/tmpfiles.d/idle-shutdown.conf` | 起動のたびに `/run/idle-shutdown/` と、その下の `touch/`（モード 1777）と `root/`（モード 755）を作る |
| `/usr/share/pam-configs/idle-shutdown` | `pam-auth-update` の設定。sshd などのログインのセッション（端末の無い ssh も含む）が開くときと閉じるときに、`idle-shutdown touch pam` を実行する |
| `/etc/update-motd.d/60-idle-shutdown` | ログインのメッセージに状態を出す（[ログインのメッセージを作る](#ログインのメッセージを作る)） |
| `/etc/ssh/sshd_config.d/idle-shutdown.conf` | sshd の `ClientAliveInterval 60` と `ClientAliveCountMax 10`。手元のマシンが接続を閉じずに落ちたとき、残ったセッションを 10 分で切る |
| `/etc/idle-shutdown/in-use.d/` | サービスごとの使用中の判定を、他のアプリが置くディレクトリ |

### 使用中の判定

次のどれかに当てはまれば使用中とする。

1. ログインを終えた sshd のセッションがある。`/proc` のコマンドライン（プロセスの
   名前として `ps` に出るもの）が `sshd: ` か `sshd-session: ` で始まり、続く部分が
   空白を含まない 1 語のものを数える。1 語は、`ubuntu`、`ubuntu@pts/0`、
   `ubuntu@notty` のようにユーザー名で始まる。トンネルだけの接続（`ssh -N`）は
   セッションを開かないので `ubuntu` のままだが、これも数える。認証前の名前は、
   空白を含むか `[` で始まるので数えない。
2. `/dev/pts/` に番号の付いた端末がある。screen や tmux は、デタッチしても
   ウィンドウごとに端末を持つので、ここで数える。
3. 対象（`what`）に `shutdown` を含む、モード `block` の
   シャットダウン抑止ロックがある。誰のロックでも数える。電源断を少し
   遅らせるだけのモード `delay` のロックは数えない。一覧は logind の
   `ListInhibitors` を `busctl` で呼んで得る。得られなければ、警告して
   使用中とする。
4. `/etc/idle-shutdown/in-use.d/` の実行ファイルを名前の順に実行し、終了コード 0 で
   何か出力したものがある。出力の各行を理由とする。本体を root で実行したときだけ
   見る。実行するのは、次の 2 つを満たすものに限る。

   - 名前が英数字、`_`、`-` だけでできている（`run-parts` と同じ）
   - 実体が root の持ち物で、グループと他人が書けない。満たさなければ警告する

   1 つを 30 秒で打ち切る。失敗したものは警告と標準エラー出力の 1 行目を、
   打ち切ったものは警告を出して、使用中とはしない。

手元のマシンが接続を閉じずに落ちると、セッションは sshd が切るまで残って使用中になる。
sshd は 60 秒おきに ssh の通信の中で手元に応答を求め、10 回続けて応答が無ければ
切る。Session Manager 経由では、sshd の TCP の相手はインスタンスの中の SSM
エージェントなので、手元のマシンが落ちてもこの TCP は切れず、TCP の keepalive（接続が
生きているかを定期的に確かめる仕組み）では気づけない。

### 最終利用時刻

最終利用時刻は、`/run/idle-shutdown/touch/` と `/run/idle-shutdown/root/` の
ファイルのうち、最も新しい更新時刻（mtime）とする。ファイルが無ければ、起動した
時刻とする。更新時刻が未来なら、いまの時刻とみなす。ただし `touch/` のファイルの
更新時刻が 60 秒を超えて未来なら、警告して無視する。いまの時刻とみなし続けると、
いつまでも電源が切れないからである。

ファイル名は、誰が更新したかを表す。

- 本体の判定: 使用中なら `root/in-use` を更新する。
- PAM: ログインとログアウトのたびに `root/pam` を更新する。判定と判定のあいだに
  収まる短いログインも、これで数える。
- アプリ: 使われるたびに、`touch/` の自分の名前のファイルを作るか、更新時刻を
  進める。ディレクトリはモード 1777 なので、root でなくても書けるが、ファイルは
  最初に作ったユーザーの持ち物になる。

### 判定と電源断

タイマーが引数なしで本体を実行する。使用中なら `root/in-use` を更新して終わる。
そうでなければ、最終利用時刻から制限時間（limit）が過ぎていれば
`systemctl poweroff` を実行する。判定の結果を 1 行、[状態を知らせる](#状態を知らせる)
の形で標準出力（journal）に書き、同じ内容を SSM パラメータにも書く。

制限時間は、次の順に探して、最初に見つかったものとする。どちらのファイルも、中身は
秒数か `off` で、`off` なら電源を切らない。読めないもの、秒数でも `off` でもないもの、
365 日を超えるものは、警告して無いものとする。

1. `/run/idle-shutdown/limit`: `idle-shutdown limit` で変えたもの。再起動するか
   インスタンスが終わるまで効く。
2. `/etc/idle-shutdown/limit`: `deploy` のオプション `limit` で決めたもの。`install` が
   書き、焼けば AMI に残る。
3. 1 時間。

`deploy` で入れ直しても、`/run/idle-shutdown/limit` は消さないので、`limit` で変えた
ものが先に効き続ける。そのときは、`limit --deploy` がそのことを出す。

### コマンド

| コマンド | root | 動き |
| --- | --- | --- |
| `idle-shutdown` | 要る | 判定して、制限時間を過ぎていれば電源を切る。結果を SSM パラメータに書く。タイマーが実行する |
| `idle-shutdown -n` | 要らない | 判定の結果を、タイマーと同じ形で出すだけで、何も書かず、電源も切らない。制限時間を過ぎていれば `power off at next check` と出す |
| `idle-shutdown status` | 要らない | 制限時間、最終利用時刻とそれを更新したもの、使用中の理由、電源を切る予定。自分のセッションが見つかり、ほかに使われていなければ、ログアウトから制限時間の後に切ると出す。自分のセッションが見つからなければ、電源を切る時刻と、それまでの時間（過ぎていれば `at next check`）を出す |
| `idle-shutdown status --login` | 要らない | ログインのメッセージ専用の `status`（[ログインのメッセージを作る](#ログインのメッセージを作る)） |
| `idle-shutdown limit` | 要らない | 制限時間を出す |
| `idle-shutdown limit <期間>` | 要る | 再起動するまでの制限時間を変える。`90m`、`4h`、`1d`、`3600`（秒）、`off`、`default`（再起動するまでの制限時間を消し、`deploy` で決めたものに戻す）。変えたあと、`-n` と同じ判定（自分のセッションは数える）の結果を SSM パラメータに書く |
| `idle-shutdown limit --deploy <期間>` | 要る | `deploy` で決めた制限時間を変える。`install` 専用。期間は `default` を除いて `limit <期間>` と同じ。判定はせず、SSM パラメータにも書かない |
| `idle-shutdown report` | 要る | `-n` と同じ判定（自分のセッションは数える）の結果を SSM パラメータに書く。`install` 専用 |
| `idle-shutdown touch [<名前>]` | 要らない | root なら `root/<名前>`、ほかのユーザーなら `touch/<名前>` を更新する。名前を省くと、実行したユーザーの名前 |

`limit --deploy` と `report` は `install` 専用なので、`--help` には出さない。

root でなければ、`status`（`--login` を除く）と `-n` は in-use.d を見ず、見ていない
ことを告げる。

`status` と `-n` は、ssh で実行されることが多いので、自分のセッションを数えない。
自分のセッションとは、次のものである。

- 自分と先祖のプロセスのうちの sshd のセッション。ControlMaster で 1 本の接続に
  相乗りした別のセッションも、同じ sshd なので一緒に除く
- 自分の標準入出力の端末
- 先祖の制御端末。`sudo` は新しい端末を作ってコマンドを動かすので、ログインの端末は
  先祖の制御端末からしか分からない

`status` が出す「ログアウトから制限時間の後に切る」という予定は、ログアウトのときに
PAM が最終利用時刻を進めることを前提にしている。PAM を通らない Session Manager の
シェルから実行したときは、予定より最大 5 分早く切れることがある。

`touch` の名前は、英数字か `_` で始まり、英数字・`.`・`_`・`-` が続く 64 文字以内と
する。シンボリックリンクは辿らず、ほかのユーザーのファイルで書けなければ失敗する。

### 状態を知らせる

判定の結果を、SSM パラメータ `/temt-<svc>/idle-shutdown/status` に 1 行の文字列で
書く（[apps.md](../apps.md#アプリの状態)）。手元の `temt-<svc> status` がこれを出す。
判定の結果は、次のどれかの形である。上にあるものを先に見る。時刻は、`status` と
同じくインスタンスのローカル時刻にタイムゾーンの名前を添えたもの
（`2026-09-29 06:12:03 UTC`）で、ssh で見る時刻と揃う。残りと経過は `1h2m` のような
形、出どころは最終利用時刻を更新したファイルの名前である。

| 判定 | 形 |
| --- | --- |
| 使用中 | `in use: <理由>; <理由> (limit <制限時間>)` |
| 制限時間が `off` | `never powers off (limit off; idle <経過> since <出どころ>)` |
| 制限時間より前 | `power off in <残り> (at <時刻>; idle <経過> since <出どころ>, limit <制限時間>)` |
| 制限時間を過ぎ、電源を切る | `powering off (idle <経過> since <出どころ>, limit <制限時間>)` |
| 制限時間を過ぎたが、切らない | `power off at next check (idle <経過> since <出どころ>, limit <制限時間>)` |

書くのは次のときである。

- タイマーの判定のたび。journal に書く行と同じ内容（200 文字を超えれば切ったもの）で
  ある。
- `systemctl poweroff` の直前。`powering off` の形で、インスタンスが終了しても
  残るので、後から終了の理由が分かる。
- `limit` で制限時間を変えたときと、`install` の最後（`report`）。`-n` と同じ判定
  （`root/in-use` を更新せず、電源も切らない）の結果を書く。ただし root で実行するので
  in-use.d も見る。自分のセッションは数えるので、ssh から実行していれば `in use` に
  なる。制限時間を過ぎていれば `power off at next check` になり、次のタイマーが切る。

`status`、`-n`、`touch` は書かない。値は 200 文字までとし、超えれば切って `...` を
付ける。in-use.d の出力が長いときのためである。

書くのは `aws ssm put-parameter --type String --overwrite` で、名前 `temt-<svc>` は
インスタンスメタデータのタグ `Name` から、リージョンは `placement/region` から
得る。権限は temt の `temt-ec2` の既定に入っている（[conf.md](../conf.md#role)）。
`aws` は `PATH` に頼らず、`/snap/bin`（aws-cli の snap の場所）、`/usr/local/bin`、
`/usr/bin` の順に探す。1 回に 15 秒までかける。aws-cli が無い、メタデータが取れ
ない、書けない（権限が無い、ネットワークが無い）ときは、警告を出すだけで、判定と
電源断は止めない。

### ログインのメッセージを作る

Ubuntu の `pam_motd` は、sshd がセッションを開くたびに、`/etc/update-motd.d/` の
実行ファイルを名前の順に root で実行し、出力をログインのメッセージにする。
コマンドの実行やトンネルの接続でも実行するが、sshd がメッセージを出すのは、
コマンドを付けずに端末を開いた接続だけで、ユーザーのホームに
`~/.hushlogin` があれば出さない。ControlMaster で 1 本の接続に相乗りした接続は、
PAM のセッションを開かないので、実行もしない。sshd は、接続で最初にセッション
（チャネル）を開いたときにメッセージを捨てるので、親の接続を `-N` で
開いたときだけ、最初に相乗りしたログインに、親の接続を開いたときのメッセージが
出る。Ubuntu の既定の `/etc/pam.d/sshd` では、`@include common-session` が
`pam_motd` の行より前にあるので、実行されるときには PAM がすでに `root/pam` を
更新している。

`/etc/update-motd.d/60-idle-shutdown` は、空行、見出しの行
`idle-shutdown (sudo idle-shutdown limit 4h to change):`、
`idle-shutdown status --login` の出力を、この順に出す。`status --login` の出力は、
標準エラー出力の警告も含め、各行を空白 2 つで字下げする。警告は状態の行より先に
出る。本体が無ければ何も出さない。

`pam_motd` が実行するとき、ログインしようとしている接続のプロセスは、sshd の特権側
（`sshd: ubuntu [priv]`。OpenSSH 9.8 からは `sshd: ` の代わりに `sshd-session: ` で
始まる）だけである。セッションとして数える `sshd: ubuntu` のプロセスも、端末も、
まだ無い。自分のセッションを除いても何も除かれないので、数えた sshd のセッションと
端末は、すべてほかの利用である。`status --login` は `status` と次の点が違う。

- 自分のセッションがあるものとして予定を出す。ほかに使われていなければ「ログアウト
  から制限時間の後に切る」と出し、「自分を数えていない」ことも出す。
- 最終利用時刻を読まず、その行を出さない。PAM が直前に更新しているので、いつも
  ログインした時刻になるからである。読まないので、`touch/` の未来の時刻の警告も
  出ない。
- in-use.d を見ず、見ていないことも告げない。`pam_motd` は接続のたびに実行する
  ので、1 つあたり 30 秒まで待たせうる判定を、ログインのたびに実行しないためである。

`--login` はログインのメッセージ専用なので、`--help` には出さない。
