# sshd の待ち受けポート

設定ファイルの `SshdPorts` を、インスタンスの sshd の待ち受けポートと、ssh の
Host ブロックの `Port` の両方に揃える。インスタンスの側は Systems Manager の
State Manager の関連付け `<名前>-sshd` で揃える。関連付けとは、「タグで選んだ
インスタンスで、この SSM ドキュメントをこの引数で実行する」という AWS への登録で、
インスタンスの SSM エージェントが受け取って実行する。`apply` の手順 6 で揃え、
`destroy` の手順 4 で消す。ssh を経由しないので、sshd がつながらないポートで
待ち受けていても届く。

`SshdPorts` は 1 つ以上のポートの配列で、既定は `[22]` である
（[conf.md](../conf.md#最上位)）。ssh の Host ブロックの `Port` には先頭のポートを
書く（[ssh.md](ssh.md)）。2 つ目以降は `ssh -p <ポート> <名前>` で指す。

## 関連付け

| 項目 | 値 |
| --- | --- |
| 名前（`AssociationName`） | `<名前>-sshd` |
| SSM ドキュメント | AWS が用意している `AWS-RunShellScript`。既定の版 |
| 対象 | タグ `Name` が `<名前>` のインスタンス（temt がインスタンスを探すときと同じ目印） |
| 引数 | `commands` に、下の「スクリプト」を 1 行 1 要素で渡す |
| タグ | `temt:service=<svc>` |
| スケジュール | 無し。`ApplyOnlyAtCronInterval` は既定（false）のまま |

スケジュールを持たないので、State Manager は次のときに実行する。関連付けを作った
とき、更新したとき、対象に合うインスタンスが新しく起動したとき、
`start-associations-once` で求めたときである。止めている間に更新したときは、
インスタンスを再開したときに実行される。

`apply` は次のように揃える。

1. 名前が `<名前>-sshd` の関連付けを `list-associations` で探す。無ければ、作るものと
   して検出する。関連付けの名前は AWS では一意でないが、temt はサービスごとに 1 つ
   だけ作るので、ふつうは 1 つである。2 つ以上あれば（手で作ったときなど）、ID の
   順で最初の 1 つを揃え、ほかには触らない。`proxy` と `status` も、同じ 1 つを見る。
2. あれば、`describe-association` で中身を読み、ドキュメントの名前、対象、
   `commands` の 3 つだけを比べる。表のほかの項目（ドキュメントの版、スケジュール、
   `ApplyOnlyAtCronInterval`）は比べない。違えば、更新するものとして検出する。
   `temt:service` のタグが無いか値が違えば、足すものとして検出する。
3. 更新では、無ければ上の表のとおり作り、作ったと知らせる。違えば、
   `update-association` に temt が決める項目をすべて渡して新しい版を作り、更新した
   と知らせる。`update-association` は渡さない項目を既定に戻すので、比べない項目も
   このときに表の値に戻る。タグは足すか直し、タグだけを直したときも、更新したと
   知らせる。
4. 更新したときと、`[22]` 以外のポートで作ったときは、動いているインスタンスには
   まもなく効くことと、`bake` で AMI に残ることを 1 行添える。タグだけを直したときは
   添えない。
5. 作ったときと、中身を更新したときだけ、`start-associations-once` で実行を求める。
   作っただけでは、動いているインスタンスで実行されないことがある。求められなければ
   警告して続ける。次の起動では実行される。中身が同じなら、何もしない。起動する
   インスタンスには、State Manager が自分で実行する。

`destroy` は、名前が `<名前>-sshd` の関連付けをすべて消し、消したか既に無かったかを
知らせる。

スクリプトの出力は、temt は表示しない。Systems Manager の State Manager にある
`<名前>-sshd` の実行履歴（Run Command の出力）で見る。

`apply` を実行する資格情報には、関連付けを扱う SSM の権限（`ListAssociations`、
`DescribeAssociation`、`CreateAssociation`、`UpdateAssociation`、
`ListTagsForResource`、`AddTagsToResource`、`StartAssociationsOnce`）が要る。
`destroy` には `ListAssociations` と `DeleteAssociation`、`proxy` と `status` には
`ListAssociations` と `DescribeInstanceAssociationsStatus` が要る。インスタンスの
ロールには、`temt-ec2` の既定（[conf.md](../conf.md#role)）で、関連付けの実行に要る
`ssm:ListInstanceAssociations`、`ssm:UpdateInstanceAssociationStatus`、
`ssm:GetDocument` と、実行結果の報告に使う `ssm:PutComplianceItems` が付いている。

## スクリプト

temt が `SshdPorts` と `<svc>` から作り、関連付けの `commands` に渡す。root で動く。
SSM エージェントは `sh`（Ubuntu では dash）で実行するので、POSIX の sh で書く。
スクリプトが知らせる行は `temt-sshd:` で始め、ポートの並びは `,` でつなぐ
（`22 -> 2222,443`）。次の場所を使う。

| 場所 | 中身 |
| --- | --- |
| `/etc/ssh/sshd_config.d/temt-sshd-ports.conf` | `Port <ポート>` の行を、`SshdPorts` の順に並べたもの |
| `/run/temt-sshd/ami-ports` | 起動してから最初に切り替える前に効いていたポート。AMI のポートとみなす |
| `/run/temt-sshd/backup` | 切り替える前の `temt-sshd-ports.conf` の控え |
| `/run/motd.d/50-temt-sshd` | ログインのメッセージに出す印（[印とログインのメッセージ](#印とログインのメッセージ)） |

以下、「効いているポート」は `sshd -T` が出す `port` の並び、「待ち受けている
ポート」は `ss -ltnp` で見た待ち受けのうち、持ち主が `sshd` か `systemd`（ソケット
起動で待ち受けを持つ）のものである。他のプロセスの待ち受けは数えない。並びの
比較は、順序を問わない。

1. `/run/sshd` が無ければ作る。sshd が一度も起動していないうちは無く、`sshd -t` と
   `sshd -T` が失敗する。
2. 効いているポートが `SshdPorts` と同じで、そのすべてで待ち受けていれば、
   `unchanged <ポート>` と知らせて 0 で終わる。sshd には触れない。`sshd -T` が
   失敗すれば、何も変えずに 1 で終わる。
3. 効いているポートを控える。`ami-ports` が無ければ、そこに書く。
4. 前の `backup` があれば消す。`temt-sshd-ports.conf` があれば `backup` に控え、
   `Port` の行を一時ファイルに書いてから置き換える。`sshd_config.d` が無ければ作る。
5. `sshd -t` で確かめる。通らなければ、`backup` から戻し（無ければファイルを消し）、
   その旨を知らせて 1 で終わる。
6. 効いているポートが `SshdPorts` と同じかを確かめる。違えば、ほかのファイルにも
   `Port` の行があり、sshd はそれらを足し合わせて待ち受けてしまう。5 と同じく戻し、
   効いているポートを添えて 1 で終わる。`sshd -T` が失敗したときも戻し、そのことを
   知らせて 1 で終わる。
7. sshd に読み直させる。まず `systemctl daemon-reload` を行う。`ssh.socket` の
   待ち受けは、このときの `sshd_config` から作られる。次に、`ssh.socket` が有効なら
   （`systemctl is-enabled`。Ubuntu 24.04 のソケット起動）`ssh.socket` を、そうで
   なければ `ssh.service` を再起動する。動いているかどうかでは分岐しない。再起動に
   失敗して止まった `ssh.socket` も、有効なら再起動して立て直す。いまつながっている
   接続は切れない。
8. `SshdPorts` のすべてで待ち受けるようになったことを確かめる。0.5 秒おきに
   20 回見てもならなければ、5 と同じく戻して 7 をやり直し、1 で終わる。
9. `SshdPorts` が `ami-ports` と同じなら印を消し、違えば印を書く。
10. `changed <前のポート> -> <今のポート>` と知らせる。

Ubuntu 24.04 のソケット起動では、`sshd_config.d` の `Port` を `daemon-reload` が
`ssh.socket` の待ち受けに写す。`sshd_config.d` の `Port` は足し合わされ、
Ubuntu の `sshd_config` は `Port` を書いていないので、このファイルの
`Port` だけが効く。アプリは `sshd_config.d` に `Port` を書かない
（[apps.md](../apps.md#install-と-uninstall)）。

## 印とログインのメッセージ

印は、`SshdPorts` が `ami-ports` と違うこと、つまり sshd が AMI のポートと違う
ポートで待ち受けていることを表す。ファイルの中身がそのままメッセージで、Ubuntu の
`pam_motd` が、コマンドを付けずに端末を開いた ssh のログインのたびに、`/run/motd.d/`
の中身を出す。出る条件は、自動終了の
[ログインのメッセージ](../apps/idle-shutdown.md#ログインのメッセージ) と同じである。

```
temt: sshd ports were changed from the AMI's (22 -> 2222); run 'temt bake --svc <svc>' to keep them in the AMI
```

- 今の `SshdPorts` と違うポートで焼いた AMI（素の Ubuntu を含む）から起動して
  起動後に切り替わったとき、動いているインスタンスで `apply` によってポートが
  変わったときに書かれ、焼くまでログインのたびに出る。
- `/run` はメモリの上にあり、`bake` は焼く前にインスタンスを停止するので、印も
  `ami-ports` も AMI には入らない。今の `SshdPorts` で焼いた AMI から起動した
  インスタンスでは、スクリプトが手順 2 で終わるので、印は書かれない。
- 同じインスタンスで AMI のポートに戻したときは、手順 9 が印を消す。
- 再起動するか、止めて再開すると（`reboot`、インスタンスの中での再起動、`stop` の
  後の `start`）`/run` が消えるので、印と `ami-ports` も消える。`/etc` の設定は
  残るので、その後は AMI と違うポートのまま印が出ない。
- `ssh -T` のように端末を開かない接続（`deploy` と `app remove` の ssh）では出ない。
- 印は `/run` にだけ置き、`/etc/motd` や `/etc/update-motd.d/` には置かない。

## 起動直後

インスタンスは、AMI に入っている設定で sshd を立ててから、SSM エージェントが
起動し、関連付けを受け取って実行する。素の Ubuntu や、違うポートで焼いた AMI から
起動すると、SSM エージェントが `Online` になってから実行が終わるまで 10 秒ほど
かかり、その間 sshd は AMI のポートで待ち受ける。`SshdPorts` のポートで焼いた
AMI から起動すれば、初めからそのポートで待ち受けるが、関連付けはやはり実行され、
`unchanged` で終わる。

`start` が待つのは SSM エージェントが `Online` になるまでで、関連付けの実行は
待たない。待つのは `proxy` で、起動して間もないとき（`pending` だったか、起動から
5 分の内。[design.md](../design.md#クライアントのコマンド) の `proxy` の節）に限る。

- **`ssm`。** SSM エージェントが `Online` になったあと、`list-associations` で
  `<名前>-sshd` の今の版を読む。関連付けが無ければ、`apply` を促す警告を出して
  そのままつなぐ。あれば、`describe-instance-associations-status` でその
  インスタンスでの状態を読み、今の版の状態が終わるまで待つ。前の版の状態は、
  まだ実行されていないものとみなす。終わった状態が `Success` でなければ
  （`Failed` など）、スクリプトが前のポートに戻していることと `status` を見るよう
  警告して、そのままつなぐ。締め切り（[instance.md](instance.md#待ち合わせ)）を
  過ぎたら、`status` を見るよう添えて終了する。起動から時間がたっていれば、
  関連付けは見ない。そのため、`apply` でポートを変えた直後の接続は、切り替わる前の
  sshd へ向かって失敗することがある。

  状態のうち `Queued`、`Pending`、`InProgress` は実行を待っているものとし、
  それ以外はすべて終わったものとする。
- **`direct`。** 関連付けは見ず、そのポートに TCP でつながるまで待つ。切り替わる
  前はつながらないので、それで足りる。関連付けが失敗していれば、つながらないまま
  締め切りを過ぎ、sshd が受け付けていないという手がかりだけを添えて終了する。

`Failed` でもつなぐので、`ssh -p <前のポート> <名前>` で入って直せる。ポートの
引数は `Port` に関わらず `%p` で `proxy` に届く。

## `status` の表示

`status` は、インスタンスごとに `<名前>-sshd` の状態を 1 行で出す。今の版の
`Success` は `applied`、`Queued`、`Pending`、`InProgress` と前の版の状態は
`pending`、一覧に無ければ `(not run)`、それ以外は AWS が返した状態を小文字で
（`failed` など）出す。関連付けそのものが無ければ `none` と出し、`apply` を促す。
