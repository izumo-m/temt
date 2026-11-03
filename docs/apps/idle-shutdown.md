# 自動終了（idle-shutdown）

誰も使っていないインスタンスを、自分で終了させるアプリ。temt と一緒に配り、
`deploy` で入れて `bake` で焼く。ssh の接続や端末などが無い状態が制限時間（既定は
1 時間）続くと、インスタンスは自分で電源を切り、そのまま終了する。閉じ忘れた
インスタンスが費用を生み続けないようにするためのものである。

アプリの作り方は
[apps.md](../apps.md) に、ソースは
[`src/temt/apps/idle-shutdown/`](../../src/temt/apps/idle-shutdown/) にある。

## 入れる

動いているインスタンスに入れ、続けて焼く。

    temt-<svc> start
    temt-<svc> deploy idle-shutdown --bake

`apply` や `start` は入れないので、
自動終了させたいサービスごとに入れて焼く。入れた直後から働き、`deploy` の出力の
終わりに、いまの状態（`idle-shutdown status`）が出る。インスタンスに aws-cli が
無ければ、手元に状態を知らせるために `install` が snap で入れるので、初めての
`deploy` は 1 分ほど長くかかり、AMI もその分大きくなる。

制限時間は、`deploy` のオプション `limit` で、サービスごとに決められる。`90m`、
`4h`、`1d`、`3600`（秒）のように 365 日までの期間で書き、`off` なら電源を切らない。
省けば 1 時間である。焼けば AMI に残り、そのサービスで起動するインスタンスの制限時間に
なる。次の `deploy` は、与えた値を引き継ぐ（[usage.md](../usage.md#アプリを入れる)）。

    temt-<svc> deploy idle-shutdown limit=4h --bake
    temt-<svc> deploy idle-shutdown limit= --bake   # 1 時間に戻す

## 使用中とみなすもの

次のどれかがあるあいだは、使用中とみなして終了しない。

- ssh の接続。端末の無い接続（`ssh -N` のトンネル、VS Code の Remote-SSH、`deploy`）
  も含む
- 端末。デタッチした screen や tmux も、端末を持ち続けるので含む
- `systemd-inhibit --what=shutdown` のロック（モード `block`）。誰が取ったものでも
  数える。root 以外が取るには `sudo` が要る
- 使用中と答える判定。ほかのアプリが `/etc/idle-shutdown/in-use.d/` に置く
  （[アプリから使用中を知らせる](#アプリから使用中を知らせる)）

これらが無くなり、最後に使われた時刻から制限時間がたつと終了する。判定は 5 分おき
なので、終了は制限時間が過ぎてから最大 5 分ほど遅れる。

手元のマシンが接続を閉じずに落ちたときは、接続がインスタンスに残って使用中になる。
`idle-shutdown` は sshd に、応答の無い接続を 10 分で切らせるので、残るのは 10 分
ほどである。その代わり、手元の PC を 10 分より長くスリープさせると、ssh が切れる。

## 状態を見る、制限時間を変える

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
出る。ssh を通さない Session Manager のシェルでは、ログアウトしても最後に使われた
時刻が進まないので、出た予定より最大 5 分早く切れることがある。ssh の接続も端末も
無いところ（Run Command など）から実行したときは、電源を切る時刻と、それまでの時間が
出る（`2026-09-29 06:12:03 UTC (in 42m)`）。時刻を過ぎていれば、次の判定で切ると出る
（`2026-09-29 06:12:03 UTC (at next check)`）。時刻はインスタンスの時計のローカル
時刻で、タイムゾーンの名前を添える。Ubuntu の既定では UTC である。

## 手元から状態を見る

インスタンスにつながずに、手元から電源を切る予定を見られる。

    temt-<svc> status

`app status` の `idle-shutdown` の行に、5 分おきの判定の結果が出る
（[usage.md](../usage.md#状態を見る)）。

    app status:
      idle-shutdown  power off in 42m (at 2026-09-29 06:12:03 UTC; idle 18m since pam, limit 1h)  [3m ago]

- `power off in 42m (at ...; idle 18m since pam, limit 1h)`: 使われておらず、
  このままなら `at` の時刻に電源を切る。[timezone-tokyo](timezone-tokyo.md)
  を入れれば日本時間になる。`in` は判定した時点からの時間
  なので、末尾の経過時間の分だけ進んでいる。`since` は最終利用時刻を更新したもの。
- `in use: 2 ssh session(s); 1 terminal(s) (limit 1h)`: 使用中で、電源を切らない。
  `limit off` でも、使用中ならこちらが出る。
- `never powers off (limit off; idle 18m since pam)`: `limit off` にしてある。
- `power off at next check (idle 1h2m since pam, limit 1h)`: 制限時間を過ぎて
  いて、次の判定で切る。ssh の接続も端末も無いところ（Run Command など）から `limit` で
  制限時間を短くしたときに出る。ssh から変えたときは、その接続を数えて `in use` になる。
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

## ログインのメッセージ

コマンドを付けずに ssh でログインすると、ログインのメッセージ（ログインしたときに
出る案内。Ubuntu の motd）に、自動終了の状態が出る。

```
idle-shutdown (idle-shutdown --help for usage):
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

## 気をつけること

長い処理の終わる時刻の見当がつくなら、
`sudo idle-shutdown limit` で制限時間を延ばしてもよい（[状態を見る、制限時間を
変える](#状態を見る制限時間を変える)）。

`bake` はインスタンスにつながないので、使用中には数えられない。最後に使ってから
制限時間が過ぎる間際に `bake` すると、確認を待つあいだに電源が切れ、焼く前の中身
ごと終了することがある。間際なら、先に `ssh temt-<svc> idle-shutdown touch` で
延ばす。

`systemd-inhibit` のロックがあるあいだは、`terminate`、`stop`、`bake` による停止も、
`reboot` による再起動も断られて遅れる。

## アプリから使用中を知らせる

ssh の接続や端末は、アプリが何もしなくても使用中に数えられる。次のようなアプリは、使われている
ことを自分で知らせる。

- セキュリティグループで開けたポートで直接使われるアプリ
- ジョブを処理するアプリ

HTTP のリクエストのように一瞬で終わる利用は、5 分おきの判定では見えない。アプリは、
利用者に使われるたびに `/run/idle-shutdown/touch/<アプリの名前>` の更新時刻を
いまにする。このディレクトリは誰でも書けるので、root でなくてよい。ファイルは
最初に作ったユーザーの持ち物になるので、同じ名前は同じユーザーだけが使う。
FastAPI ならミドルウェアで済む。

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

シェルからは `idle-shutdown touch myapp` でもよい。死活監視や bot のアクセスで更新すると、インスタンスがいつまでも終了しないので、
利用者の操作だけで更新する。

キューに残ったジョブのように、続いている状態を使用中として知らせるときは、
`/etc/idle-shutdown/in-use.d/` に実行ファイルを置く。ファイルは 5 分おきに root で
実行される。終了コード 0 で何か出力すれば使用中とみなされ、出力は
`sudo idle-shutdown status` に理由として出る。ファイルは次のように置く。守らない
ものは実行されない。

- 名前は英数字、`_`、`-` だけにする（`.sh` を付けると実行されない）
- root の持ち物にして、グループと他人が書けないようにする

1 つが 30 秒を超えると打ち切られる。打ち切られたものや、0 以外で終わったものは、
警告が出るだけで、使用中とはみなされない。

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

## 外す

動いているインスタンスから外し、続けて焼く。

    temt-<svc> start
    temt-<svc> app remove idle-shutdown --bake

ほかのアプリが
in-use.d に置いた判定と、`install` が入れた aws-cli は消さない。
