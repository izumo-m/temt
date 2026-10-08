# タイムゾーン（timezone-tokyo）

インスタンスのタイムゾーンを Asia/Tokyo（日本時間）にするアプリ。temt と一緒に配り、
`deploy` で入れて `bake` で焼く。Ubuntu の AMI の既定は Etc/UTC なので、
入れないと `date`、ログ、cron の予定の時刻は日本時間より 9 時間遅れて見える。

アプリの作り方は
[apps.md](../apps.md) に、ソースは
[`src/temt/apps/timezone-tokyo/`](../../src/temt/apps/timezone-tokyo/) にある。

## 入れる

動いているインスタンスに入れ、続けて焼く。

    temt-<svc> start
    temt-<svc> deploy timezone-tokyo --bake

`apply` や `start` は入れないので、
日本時間にしたいサービスごとに入れて焼く。入れた直後から働き、`deploy` の出力の
終わりに、タイムゾーンといまの時刻が出る。

    timezone-tokyo: Asia/Tokyo, now 2026-09-29 16:00:00 JST

## 変わるもの

インスタンスの中で時刻を日付と時刻に直すものが、日本時間になる。temt に関わるものでは、
次の時刻が変わる。

- [idle-shutdown](idle-shutdown.md) の `status` に出る時刻
- 手元の `temt-<svc> status` に出る `idle-shutdown` の電源を切る時刻。アプリが書いた
  状態をそのまま出すので、次の判定（5 分以内）から日本時間になる

手元の temt が組み立てて出す時刻（AMI の名前や `app list` などの時刻）は変わらない。
インスタンスではなく手元のタイムゾーンで表す。

## 気をつけること

- 動いているプロセスには、起動したときのタイムゾーンを使い続けるものがある。cron と
  rsyslog は `install` が起動し直す。ほかのサービスは、起動し直すか、インスタンスを
  再起動すると日本時間になる。ログインしている端末では、ログインし直す。

## 外す

動いているインスタンスから外し、続けて焼く。

    temt-<svc> start
    temt-<svc> app remove timezone-tokyo --bake

タイムゾーンは Etc/UTC に戻る。入れる前のタイムゾーンは覚えていないので、ほかの
タイムゾーンにしていたときも Etc/UTC になる。
