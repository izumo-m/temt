# スワップファイル（swapfile）

インスタンスに swap（メモリが足りないときに、使っていないメモリの中身を一時的に
ディスクへ逃がす領域）を足すアプリ。temt と一緒に配り、`deploy` で入れて `bake` で
焼く。Ubuntu のクラウド向け AMI には swap が無いので、メモリの少ない t3.micro などで
大きなビルドを走らせると、OOM killer（メモリ不足のときにプロセスを止める仕組み）に
止められやすい。入れると、`/swapfile` を swap に使い、起動のたびに使い直す。

前半は使う人向けの説明、後半は設計である。アプリの作り方は
[apps.md](../apps.md) に、ソースは
[`src/temt/apps/swapfile/`](../../src/temt/apps/swapfile/) にある。

## 使い方

### 入れる

動いているインスタンスに入れ、続けて焼く。

    temt-<svc> start
    temt-<svc> deploy swapfile --bake

焼くまでは、次に起動するインスタンスには入らない。`apply` や `start` は入れないので、
swap を使いたいサービスごとに入れて焼く。入れた直後から働き、`deploy` の出力の
終わりに、大きさといま swap に出ている量が出る。

    swapfile: /swapfile 1024M, active, 0B used

大きさは、`deploy` のオプション `size` で決める。`512m`（MiB）や `4g`（GiB）のように
書き、省けば 1 GiB である。次の `deploy` は、与えた値を引き継ぐ
（[usage.md](../usage.md#アプリを入れる)）。

    temt-<svc> deploy swapfile size=4g --bake
    temt-<svc> deploy swapfile size= --bake   # 1 GiB に戻す

いまの様子は、インスタンスの上の `swapon --show` や `free -h` で見る。

    ssh temt-<svc> swapon --show

### 失敗するとき

次のときは、何も変えずに失敗する。`deploy` の記録も前のままである。

- **ディスクの空きが足りない。** 作ったあとに 1 GiB の空きが残らない大きさは作らない。
  ルートボリュームは、設定ファイルの `VolumeSize` を書かなければ AMI の大きさ（素の
  Ubuntu なら 8 GiB）である。足りなければ広げる
  （[usage.md](../usage.md#ディスクを広げる)）。大きさを変えるときは、いまの
  `/swapfile` を消せば空く分も空きに数える。
- **swap に出ているものが、メモリに戻しきれない。** 大きさを変えるときは、いまの swap を
  止めて（`swapoff`）、swap に出ているものをメモリに戻す。戻した量に 256 MiB の余裕を
  足して、使えるメモリ（`/proc/meminfo` の MemAvailable）を超えるなら止めない。止めると
  OOM killer がほかのプロセスを止めかねないからである。インスタンスを再起動すると
  swap はほぼ空になるので、`temt-<svc> reboot` のあとで `deploy` し直す。
- **`/swapfile` が普通のファイルでない。** ディレクトリやシンボリックリンクには触らない。
  どかしてから `deploy` し直す。

### 気をつけること

- 焼くと、`/swapfile` の中身も AMI に入る。swap に出たメモリの中身（動いていた
  プロセスが持っていたデータ）が含まれ、書かれた分だけスナップショットも大きくなる。
  ホームディレクトリなどのディスクの中身が AMI に入るのと同じ扱いである。気になる
  なら、再起動して swap がほぼ空のうちに焼く。
- 前からある `/swapfile` は、このアプリのものとして扱う。同じ大きさで swap に使って
  いればそのまま使い、そうでなければ作り直す。`/etc/fstab` の `/swapfile` の行も、
  このアプリの行に置き換え、持ち主を root、読み書きを root だけにする。
- `vm.swappiness`（どれだけ早めに swap へ逃がすか）は、既定の 60 のままである。

### 外す

動いているインスタンスから外し、続けて焼く。

    temt-<svc> start
    temt-<svc> app remove swapfile --bake

swap を止め、`/etc/fstab` の行と `/swapfile` を消す。swap に出ているものがメモリに
戻しきれないときは、入れるときと同じく、何も変えずに失敗する。焼くまでは AMI に
残り、次に起動するインスタンスも swap を使う。

## 設計

`install` と `uninstall` は、使うファイルのパスと、共通の手順を `common.sh` から読み込む。

`install` は、何かを変える前に次を確かめ、通らなければ失敗する。

1. `size` をバイト数に直す。数が 9 桁を超えると bash の整数があふれるので止める。
2. `/swapfile` が、無いか普通のファイルであること。
3. `/swapfile` が同じ大きさで swap に使われていれば、ファイルに触らない。再 `deploy` や、
   焼いた AMI から起動したインスタンスはこれに当たる。使われていなければ、同じ大きさでも
   swap の形か分からないので作り直す。
4. 作り直すなら、ディスクの空きと、使っていれば止められるだけのメモリ。

作り直すときは、使っていれば `swapoff` で止めて消し、`fallocate` で場所だけ取って
作る（中身を書き込まないので、大きくてもすぐ終わる）。umask 077 で作るので、作った
ときから root だけが読める。`mkswap` で swap の形にし、`swapon` で使い始める。

続けて、前からあったファイルも含めて、持ち主を root にして `chmod 600` にする。
`/etc/fstab` の `/swapfile` の行は、次の 1 行にする。足すのは `swapon` が成功した
あとである。ほかの行はそのまま残し、変わったときだけ書き換えて
`systemctl daemon-reload` で systemd に読み直させる。

    /swapfile none swap sw,nofail 0 0

fstab の swap の行は、systemd が `swapfile.swap` というユニットにして、`swap.target` に
ぶら下げる。`swap.target` は `sysinit.target` から「あれば使う」（`Wants=`）でしか
引かれておらず、ファイルシステムの `local-fs.target` と違って、失敗しても緊急モードに
落ちない。そのため、`/swapfile` が無い、壊れているといったときも、起動は swap の無い
まま続く。`nofail` を付けると、`swap.target` も失敗の扱いにならず、失敗は
`swapfile.swap` にだけ残る。

`uninstall` は、使っていれば `install` と同じ確かめのあとで `swapoff` で止め、
`/etc/fstab` の `/swapfile` の行を消し、`/swapfile` が普通のファイルなら消す。
