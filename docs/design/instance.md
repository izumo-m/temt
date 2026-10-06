# インスタンス

`start` が起動し、`terminate`、`bake`、`destroy` が終了する。`bake` は終了の前に
停止する。`stop` は終了せずに停止し、止めたものは `start` が再開する。`reboot` は
終了せずに再起動する。コマンドごとの手順は
[design.md](../design.md#クライアントのコマンド) にあり、ここにはコマンドをまたいで
使う仕組みを書く。

## 探す

Name タグが `<名前>` で状態が `pending`、`running`、`stopping`、`stopped`、
`shutting-down` のいずれかのインスタンスを探す。`start`、`stop`、`terminate`、
`reboot`、`bake`、`destroy`、`deploy`、`app remove` は、ロックを取った後で探す。
`proxy` と `status` は、ロックを取らずに探す。`proxy` は、つなぐ先もこの答えから
決める。

`describe-instances` は `run-instances` から数秒遅れる。`start --async` は、AWS が
起動の要求を受け付けたところでロックを離すので、直後の `start` には起動した
インスタンスがまだ見えず、2 台目を起動しうる。これは許容する。`status` は 2 台以上を
警告し、`terminate` はインスタンス ID を指して余分なものだけを終了できる。
`--async` でない `start` は、ログインできるようになるまでロックを持つ。

## 途中の状態

`start`、`stop`、`reboot` は、インスタンスが途中の状態（`pending`、`stopping`）でも
先回りして断らず、該当する要求（`start-instances`、`stop-instances`、
`reboot-instances`）をそのまま AWS に送る。どの状態で何を受け付けるかは AWS が
決め、temt はどちらの振る舞いでも正しく動くように作る。AWS がインスタンスの状態の
誤り（`IncorrectInstanceState`、`IncorrectState`）で断れば、その誤りのコードと
メッセージに、`status` を見てやり直すよう添えて終了する。要求を送るまでもない
もの（`start` が見つけた `running` と `pending`、`stop` が見つけた `stopped` と
`stopping`）には、送らない。

終了の途中（`shutting-down`）のものは、`terminated` と同じく無いものとして扱う。
2 台以上かどうかは、すべてのコマンドで、これを除いて数える。これしか無ければ、
無いときの扱いになる。`start` は新しく起動し、`stop` は無いと知らせ、`reboot`、
`deploy`、`app remove`、`proxy` は `start` するよう告げ、`bake` は焼くものが無いと
告げる。`status` と `terminate` は、これも挙げる。`terminate` と `destroy` は、これも
終了する（終わるまで待つ）。

## 終了する

`terminate` の手順 4。選んだインスタンスのそれぞれについて、次を順に行う。
2 台以上あってもすべてに行う。

1. `DeleteOnTermination` が false の EBS ボリュームがあれば true にする。失敗して
   も続ける（ボリュームが残ると知らせる）。
2. `terminate-instances` を呼ぶ。
3. 要求が受け付けられたら、そのインスタンスの known_hosts ファイルを消す。
   `current` がそれを指していてリンク切れになったら、`current` も消す。
   EC2 はインスタンス ID を再利用しないので、待たずに消してよい。

## 待ち合わせ

締め切りを置く。ただし `bake` が AMI を待つときは置かない（下に書く）。

| 段階 | 締め切り |
| --- | --- |
| インスタンスが `running` になるまで | 10 分 |
| インスタンスが `stopped` になるまで | 10 分 |
| パブリック IPv4 アドレスが付くまで（`proxy` の `direct`） | 10 分 |
| 起動後にログインできるようになるまで（SSM エージェントが起動より後に `Online` と報告する） | 5 分 |
| 起動後に sshd が接続を受け付けるまで（`proxy` の `direct`。1 回の接続の試みは 5 秒まで） | 5 分 |
| 起動後に sshd の待ち受けポートを揃える関連付けが実行し終わるまで（`proxy` の `ssm`。[sshd-ports.md](sshd-ports.md#起動直後)） | 5 分 |
| 再起動の前に boot_id を読むまで（`reboot`） | 1 分 |
| 再起動を求めてから、boot_id が変わったと読めるまで（`reboot`） | 10 分 |
| 再開を求めてから、`stopping` が見えなくなるまで（`start`） | 10 分 |
| 再開を求めてから、`stopped` が見えなくなるまで（`start`） | 1 分 |
| 終了するまで | 10 分 |
| 終了したインスタンスのネットワークインターフェースが外れて、セキュリティグループを消せるようになるまで | 2 分 |

締め切りは、その段階を待ち始めたときから数える。締め切りを過ぎたら、どの段階が
終わらなかったかを述べて終了する。`running` を待つ
間にインスタンスが `stopping`、`stopped`、`shutting-down`、`terminated` のいずれかに
なったら、締め切りを待たずにその状態を述べて終了する。`stopped` を待つ間に
`shutting-down` か `terminated` になったときも同じである。パブリック IPv4 アドレスを
待つ間に `running` を待つときと同じ状態になったとき、`reboot` が再起動の終わりを
待つ間に `running` を待つときと同じ状態になったときも同じである。
`reboot` は、SSM エージェントが `Online` でない回にだけ状態を見る。状態を述べる
ときは、状態が変わった理由（`StateReason` の `Message`）があれば添える。段階が
進むたびに 1 回ずつ知らせる。

起動からの時間は、手元の時計ではなく、`describe-instances` の応答の時刻から数える。
起動時刻（`LaunchTime`）は、停止したものを再開すると再開した時刻になる。

`bake` が AMI が `available` になるまで待つときは、締め切りを置かない。待つ間に、
AMI が `pending` でも `available` でもない状態になったか、作ってから 5 分たっても
`describe-images` に見つからなければ、作れなかったものとする。

### 前の状態が見えなくなるまで

`--async` でない `start` が再開を求めたあと、`describe-instances` が前の状態を
返さなくなるまで、ID で引いて待ってから `running` を待つ。`describe-instances` は
`start-instances` から数秒遅れるので、前の状態が見えるうちに `running` を待つと、
古い状態を、止まって `running` にならないものと取り違える。`--async` では待たない。

見つけた状態が `stopping` なら、まず `stopping` が見えなくなるまでを、`stopped` に
なるまでと同じ締め切りで待つ。AWS が停止の途中の再開を受け付けて、止まり終えてから
動き出すこともあるからである。続けて、どちらのときも `stopped` が見えなくなるまでを
待つ。締め切りを過ぎたら、最後に見えた状態が変わった理由を添えて終了する。再開が
すぐ失敗して `stopped` に戻ったとき（暗号化した EBS の鍵が使えないときなど）も、
ここで終わる。

`stop` は待たない。`stopped` を待つ間に古い `running` が見えても、待ち続ければ
よいからである。

## ロック

`start`、`stop`、`terminate`、`reboot`、`bake`、`destroy`、`deploy`、`app remove`
は、known_hosts のディレクトリに置くロックファイル `.lock` に排他ロックを掛けてから
動く。取れなければ、他のコマンドが動いていると告げて終了し、待たない。ロックは
サービスごとなので、別のサービスの操作は並行できる。ロックを取る前に、known_hosts
のディレクトリが無ければ作る。

`ami prune` は、始める前ではなく、AMI を 1 つ消すたびに、その AMI の `temt:service`
のサービスのロックを取り、消し終えたら離す。取れなければ、待たずにその AMI を
残して次へ進む。known_hosts のディレクトリが無ければ、作らずに、ロックを取らずに
進める（[design.md](../design.md#クライアントのコマンド) の `ami prune`）。
