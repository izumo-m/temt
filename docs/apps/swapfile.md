# スワップファイル（swapfile）

インスタンスに swap を足すアプリ。temt と一緒に配り、`deploy` で入れて `bake` で
焼く。Ubuntu のクラウド向け AMI には swap が無いので、メモリの少ない t3.micro などで
大きなビルドを走らせると、OOM killer に止められやすい。入れると、`/swapfile` を swap に使い、起動のたびに使い直す。

アプリの作り方は
[apps.md](../apps.md) に、ソースは
[`src/temt/apps/swapfile/`](../../src/temt/apps/swapfile/) にある。

## 入れる

動いているインスタンスに入れ、続けて焼く。

    temt-<svc> start
    temt-<svc> deploy swapfile --bake

`apply` や `start` は入れないので、
swap を使いたいサービスごとに入れて焼く。入れた直後から働き、`deploy` の出力の
終わりに、大きさといま swap に出ている量が出る。

    swapfile: /swapfile 1024M, active, 0B used

大きさは、`deploy` のオプション `size` で決める。`512m`（MiB）や `4g`（GiB）のように
書き、省けば 1 GiB である。次の `deploy` は、与えた値を引き継ぐ
（[usage.md](../usage.md#アプリを入れる)）。

    temt-<svc> deploy swapfile size=4g --bake
    temt-<svc> deploy swapfile size= --bake   # 1 GiB に戻す

## 失敗するとき

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

## 気をつけること

- 焼くと、`/swapfile` の中身も AMI に入る。swap に出たメモリの中身（動いていた
  プロセスが持っていたデータ）が含まれ、書かれた分だけスナップショットも大きくなる。
  気になるなら、再起動して swap がほぼ空のうちに焼く。
- 前からある `/swapfile` は、このアプリのものとして扱う。同じ大きさで swap に使って
  いればそのまま使い、そうでなければ作り直す。どちらでも、持ち主を root、読み書きを
  root だけにする。`/etc/fstab` の `/swapfile` の行も、このアプリの行に置き換える。
- `vm.swappiness` は、既定の 60 のままである。

## 外す

動いているインスタンスから外し、続けて焼く。

    temt-<svc> start
    temt-<svc> app remove swapfile --bake

swap を止め、`/etc/fstab` の行と `/swapfile` を消す。swap に出ているものがメモリに
戻しきれないときは、入れるときと同じく、何も変えずに失敗する。
