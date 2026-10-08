# アプリの作り方

アプリは、インスタンスの中で動くものを 1 つのディレクトリにまとめたものである。
手元でディレクトリを作り、`deploy` で動いているインスタンスに入れて確かめ、`bake` で
AMI に焼く。焼けば、そのサービスで次に起動するインスタンスにも入っている。

temt がするのは、ディレクトリをインスタンスへ送って `install` を実行し、
入れた記録を SSM パラメータに書くことだけである。何をどう入れるかは `install` に
書く。入れ方と外し方のコマンドは [usage.md](usage.md#アプリを入れる) にある。

## 最小のアプリ

インスタンスの中だけで待ち受ける Web サーバーを、systemd のサービスとして動かす例。

    hello/
        manifest.toml
        install
        uninstall
        hello.service
        www/index.html

`manifest.toml`:

```toml
name = "hello"
version = "1"
description = "インスタンスの中で index.html を返す"
```

`install`:

```bash
#!/bin/bash
# hello を入れる。deploy が root で実行する。何度実行してもよい
set -euo pipefail

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")

install -m 644 "$here/hello.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable hello.service
systemctl restart hello.service
```

`uninstall`:

```bash
#!/bin/bash
# hello を外す。app remove が root で実行する。何度実行してもよい
set -euo pipefail

systemctl disable --now hello.service 2>/dev/null || true
rm -f /etc/systemd/system/hello.service
systemctl daemon-reload
```

`hello.service`。`/opt/temt/apps/hello/` の `hello` はアプリ ID なので、ディレクトリの
名前を変えたら、ここも直す。

```ini
[Unit]
Description=hello

[Service]
User=ubuntu
ExecStart=/usr/bin/python3 -m http.server 8000 --bind 127.0.0.1 --directory /opt/temt/apps/hello/www
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`www/index.html`:

```html
<p>hello from temt</p>
```

入れて、ssh のポート転送で見る。ブラウザで `http://localhost:8000/` を開くと
`index.html` の中身が出る。

    temt-<svc> start
    temt-<svc> deploy ./hello
    ssh -N -L 8000:127.0.0.1:8000 temt-<svc>

動かないときは、インスタンスのログを見る。

    ssh temt-<svc> journalctl -u hello

確かめ終えたら、残すなら焼き、残さないなら外す。`bake` は
インスタンスを終了する。

    temt-<svc> bake                 # 残すなら
    temt-<svc> app remove hello     # 残さないなら

## ディレクトリ

    <アプリID>/
        manifest.toml   必須。アプリ名と版（任意で install を実行するユーザーと、オプション）
        install         必須。導入の bash スクリプト
        uninstall       任意。外す bash スクリプト。無ければ app remove で外せない
        ...             install が使うファイル

- **アプリ ID**: ディレクトリの名前。インスタンスの上のディレクトリ名、SSM パラメータのパス、
  コマンドの引数に使う。英小文字で始まり、英小文字・数字・ハイフンが続き、英小文字か
  数字で終わる 64 文字以内の名前にする。
- **場所**: どこに置いてもよい。`deploy` の引数が `/` を含めばパスとして扱うので、
  自分のリポジトリに置いたまま `temt-<svc> deploy ./hello` で入れられる。
  `~/.config/temt/apps/<アプリID>/`（`TEMT_CONF_DIR` を設定していれば、その下の
  `apps/<アプリID>/`）に置けば、`temt-<svc> deploy hello` のようにアプリ ID だけで
  指せる。ここに無いアプリ ID は、temt と一緒に配るアプリから探す。ここに置いた
  アプリと temt と一緒に配るアプリは、入れていなくても `app list` に出る。`--svc` を
  付けない `temt app list` は、これらだけを挙げる。temt と一緒に配るアプリと同じ
  ID のディレクトリを自分で置けば、自分のものが選ばれる。
- **送られるもの**: ディレクトリは、`.git` などの隠しファイルも含めてまるごと
  送られ、焼けば AMI に残る。送らないファイルを指定する仕組みは無いので、秘密の鍵、
  `.venv` や `node_modules`、大きなファイルは置かない。中のシンボリックリンクは
  リンクのまま送られるので、アプリの外を指すものはインスタンスで切れる。
  `temt-options.env` は temt が足すファイル（[オプション](#オプション)）なので、
  同じ名前のファイルは置けない。
- **持ち主とモード**: ファイルとディレクトリは root の持ち物になる。モードは手元の
  ものから、グループと他人の書き込み、setuid、setgid、sticky ビットを落としたものに
  なる。読み取りの許可はそのままなので、手元で 600 のファイルや 700 のディレクトリは、
  `User=ubuntu` のサービスや、root 以外で実行する `install` からは読めない。root
  以外で実行する `install` は、アプリのディレクトリに書けない。

## `manifest.toml`

| 項目 | 必須 | 中身 |
| --- | --- | --- |
| `name` | 必須 | 表示するアプリ名。空白や大文字を含んでよい。`app list` に出る |
| `version` | 必須 | 版。`app list` に出る（最後に入れた版は `VERSION`、AMI に焼いた版は `BAKED`、手元にある版は `LOCAL` の列）。temt は比べないので、同じ版でも `deploy` すれば入れ直す |
| `description` | 任意 | 説明。`--svc` を付けない `app list` で、`--format` に `DESCRIPTION` を選ぶと出る |
| `user` | 任意 | `install` を実行するインスタンスのユーザー。省けば root。`deploy --user` で変えられる。`app remove` は、記録を通して同じユーザーで `uninstall` を実行する |
| `[options.<名前>]` | 任意 | `deploy` で値を変えられるオプションの宣言（[オプション](#オプション)） |

`[options.<名前>]` のほかは、値をどれも空でない文字列で書く。表にない項目があると、
`deploy` はエラーにして止まる。

## オプション

`deploy` のたびに値を変えて入れたいもの（待ち受けるポート、制限時間など）は、
オプションとして宣言する。利用者は `deploy` のコマンドラインで値を与える。

    temt-<svc> deploy ./hello port=8080

`manifest.toml` に、オプションごとに `[options.<名前>]` のテーブルを書く。名前は、
アプリ ID と同じく、英小文字で始まり、英小文字・数字・ハイフンが続き、英小文字か数字で
終わる 64 文字以内にする。

```toml
name = "hello"
version = "2"

[options.port]
default = "8000"
description = "the port to listen on in the instance"
pattern = '[0-9]+'

[options.motd]
default = "off"
description = "show the URL in the login message: on or off"
pattern = 'on|off'
```

| 項目 | 必須 | 中身 |
| --- | --- | --- |
| `description` | 必須 | 説明。`temt-<svc> app show <アプリID>` と、値の誤りに出る。受け付ける値も書いておく。改行などの制御文字は使えない |
| `default` | 任意 | 既定の値。空の文字列でもよい。省くと、前の値が記録に無い `deploy`（初めての `deploy`、`--reset` や `app remove` の後）で、利用者が値を与えなければならない |
| `pattern` | 任意 | Python の正規表現。値の全体がこれに一致しなければ、`deploy` は何も変えずに止まる。`default` も一致させる |

値は文字列である。制御文字（改行、タブなど）を除けば、日本語を含めて何でも使える。
入れるか入れないかのような切り替えも、値の無い名前だけの形は無いので、
`pattern = 'on|off'` のオプションにする。コマンドラインの `<名前>=` は既定に戻す指定
なので、利用者は空の値を与えられない。「無し」を選べるようにするなら、`none` などの
語を受け付ける。

temt は、宣言したすべてのオプションの値を、利用者が与えなかったものは既定で埋めて、
アプリのディレクトリにファイル `temt-options.env` として足してから送る。1 行に 1 つ、
`TEMT_OPTION_<名前>=<値>` の形で、名前は大文字にして `-` を `_` にしたもの、値は
シェルの引用符で囲んだものである。`install` は、これを `.` で読み込む。

```bash
here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
. "$here/temt-options.env"

# hello.service の @PORT@ を、与えられたポートにして置く
sed "s/@PORT@/$TEMT_OPTION_PORT/" "$here/hello.service" >/etc/systemd/system/hello.service
```

`pattern` で形を決めていない値は何が来るか分からないので、使うときは
`"$TEMT_OPTION_<名前>"` のように引用符で囲み、`sed` の置き換えの式などにそのまま
埋め込まない。

オプションを宣言しないアプリには、`temt-options.env` を置かない。

`temt-options.env` はアプリのディレクトリと一緒に入れ替わり、焼けば AMI に残る。
`uninstall` も同じファイルを読める。インスタンスの上で、`deploy` と同じく、カレント
ディレクトリを `/` にして、`install` を実行したユーザーで実行すれば、最後に `deploy`
したときと同じ値で入れ直せる。root なら 1 行め、ほかのユーザー（`manifest.toml` の
`user` か `deploy --user`）なら 2 行めの形である。

    cd / && sudo bash /opt/temt/apps/<アプリID>/install
    cd / && sudo -H -u <ユーザー> bash /opt/temt/apps/<アプリID>/install

次の `deploy` は、利用者が前に与えた値を引き継ぐ（[usage.md](usage.md#アプリを入れる)）。
版を上げて既定を変えれば、利用者が値を与えていないオプションは新しい既定になる。
オプションを宣言から消せば、前に与えた値は警告とともに捨てられる。`pattern` を
狭めて前に与えた値が合わなくなると、利用者が値を与え直すまで `deploy` は止まる。

`deploy` は与えた値を暗号化せずに記録に残し、`temt-options.env` は AMI に残って
インスタンスの誰からも読めるので、パスワードや鍵のような秘密の値はオプションにしない。

## `install` と `uninstall`

インスタンスの `/opt/temt/apps/<アプリID>/` に置かれたあと、`deploy` が
`bash /opt/temt/apps/<アプリID>/install` を、次に決めるユーザーで実行する。
`uninstall` は `app remove` が、`install` を実行したのと同じユーザーで実行する。
どちらも bash で実行されるので、実行の許可（`chmod +x`）は要らない。

ユーザーは、`deploy --user` の値、無ければ `manifest.toml` の `user`、それも無ければ
root である。`HOME` はそのユーザーのホームになるので、mise のようにホームに入る
ものは、`user = "ubuntu"` にして入れる。

root 以外で実行するときは、`sudo -n -H -u <ユーザー>` を通すので、次に気をつける。

- ubuntu ユーザーはパスワードなしで `sudo` を使える。ほかのユーザーは、sudoers に書かない
  限り使えない。
- `PATH` は sudo の `secure_path` で、`~/.local/bin` は入らず、`.profile` や
  `.bashrc` も読まれない。入れたばかりのコマンドは、`"$HOME/.local/bin/mise"` の
  ようにフルパスで呼ぶ。
- `systemctl --user` は使えない。
- `user` を変えて `deploy` し直しても、前のユーザーで入れたものは残る。変えるときは、
  先に `app remove` する。

たとえば、mise を ubuntu のホームに入れるアプリは次のようになる。

```toml
name = "dev-tools"
version = "1"
user = "ubuntu"
```

```bash
#!/bin/bash
# mise を ubuntu のホームに入れる。deploy が ubuntu で実行する。何度実行してもよい
set -euo pipefail

if [ ! -x "$HOME/.local/bin/mise" ]; then
    curl -fsSL https://mise.run | sh
fi
```

次のように書く。

- **何度実行してもよいようにする。** `deploy` をやり直すと、`install` はもう一度
  実行される。アプリを更新するときも同じで、前の版の `uninstall` は実行されない。
  前の版が入れて、新しい版では要らなくなったもの（名前を変えた前のサービスなど）は、
  新しい `install` が消す。
- **自分のファイルは、スクリプトのあるディレクトリから探す。** カレントディレクトリは `/` なので、
  `here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")` のように求める。
- **問いかけるコマンドを使わない。** 端末は無く、標準入力は空である。出力は手元に
  そのまま出る。
- **apt のロックを待つ。** `start` の直後は、起動時の自動更新が apt のロックを
  持っていることがある。`apt-get install` には `-o DPkg::Lock::Timeout=300` を
  付けて待たせる。`apt-get update` はロックを待たずに失敗するので、失敗したら少し
  待ってやり直すように書くか、`deploy` し直す。
- **常駐するものは systemd などで起動する。** `install` の出力を引き継いだ
  プロセスが残ると、ssh が終わらず、`deploy` が終わらない。`install` から
  バックグラウンドに回すなら、出力を `/dev/null` へ向ける。
- **状態を `/opt/temt/apps/<アプリID>/` に書かない。** このディレクトリは `deploy` の
  たびに新しいものと入れ替わる。
- **`/etc/ssh/sshd_config.d/` に `Port` を書かない。** sshd の待ち受けポートは設定
  ファイルの `SshdPorts` が決める（[usage.md](usage.md#sshd-のポートを変える)）。
  アプリが `Port` を足すと、揃える仕組みが失敗して前のポートに戻る。
- **失敗したら 0 以外で終わる。** `install` が失敗すると `deploy` も失敗し、記録は書かれず、
  `--bake` を付けていても焼かれない。更新に失敗したときは、ファイルは新しい版に
  なっているが、記録は前の版のままである。

`deploy` は途中で失敗しても巻き戻さない。直してもう一度 `deploy` する。Ctrl-C で
止めても、インスタンスの上の `install` は走り続けることがある。

`/opt/temt/apps/<アプリID>/` は、
`uninstall` が成功したあとに temt が消すので、消さなくてよい。実行されるのは
インスタンスに入っている版の `uninstall` なので、手元で後から足したときは、
先に `deploy` し直す。

## データの場所

インスタンスのディスクは、`terminate` でも自動終了でも消える。残したい状態は、
S3 などインスタンスの外に置く。インスタンスの中のデータは `/var/lib/<アプリID>/`
などに置くが、焼けば AMI に残り、そこから起動したインスタンスに持ち越される。
焼きたくないものは、焼く前に消すか `/run` に置く。

`install` が入れたものも、焼けば AMI に残り、そこから起動したインスタンスでは
`install` は実行されない。
インスタンスごとに変わるもの（IP アドレス、起動時刻、一時的な鍵など）は `install` で
決めず、起動のたびにサービスが求めるようにする。

## AWS を使う

インスタンスは、サービスの IAM ロールの権限で AWS を呼べる。アプリが要る権限は、
設定ファイルの `[[role.policy.<ポリシー名>]]` に名前を付けて書き、`deploy` より先に
`apply` する（[conf.md](conf.md#role)）。`deploy` はロールに触らない。S3 のバケット
など、アプリが使う AWS のリソースは、temt の外で用意する。インスタンスで aws-cli を
使うなら、`install` で入れておく（`snap install aws-cli --classic` など）。

付けたばかりの権限は、IAM に反映されるまで数秒かかることがある。`apply` の直後に
`deploy` して `install` がすぐその権限を使うなら、少し待ってから `deploy` するか、
`install` が失敗したときに少し待ってやり直すように書く。

### アプリの値

アプリの設定や小さな状態は、SSM パラメータ `/temt-<svc>/<アプリID>/<キー>` に
置ける。手元からは `app set` と `app get` で読み書きできる
（[usage.md](usage.md#アプリの値)）。キーは、アプリ ID と同じ規則の名前にする。
キー `deploy` は temt が記録に使うので、アプリは書かない。値は 4 KB までで、
`destroy` で消える。

自分のサービスの階層 `/temt-<svc>` の値は、権限を足さずに読み書きできる。temt が
既定で付ける `temt-ec2` が許している（[conf.md](conf.md#role)）。既定では、ほかの
階層のパラメータは読めない。インスタンスの中では、自分のサービスの名前 `temt-<svc>` を
インスタンスメタデータのタグ `Name` から、リージョンを `placement/region` から
得る。temt の起動テンプレートは、メタデータからタグを読めるようにしてある。次は
キー `greeting` を読む例で、サービスの起動スクリプトなどに書く。

```bash
imds() {
    local token
    token=$(curl -sf -X PUT http://169.254.169.254/latest/api/token \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
    curl -sf -H "X-aws-ec2-metadata-token: $token" \
        "http://169.254.169.254/latest/meta-data/$1"
}
name=$(imds tags/instance/Name)   # temt-<svc>
region=$(imds placement/region)
greeting=$(aws ssm get-parameter --region "$region" --name "/$name/hello/greeting" \
    --query Parameter.Value --output text 2>/dev/null) || greeting="hello"   # 無ければ既定
```

手元から値を書けるのは、`deploy` が記録を書いたあとである。`app set` は、記録の
無いアプリには書かない。記録は `install` が成功してから書かれるので、初めて
`deploy` するときの `install` と、そのとき起動するサービスは、値が無いまま動く。
`install` は値に頼らず、サービスは値が無ければ既定で動くか、待って読み直すように
書く。

    temt-<svc> app set hello greeting=hi

インスタンスからも、同じ階層になら書ける。`String` で書く。

```bash
aws ssm put-parameter --region "$region" --name "/$name/hello/last-run" \
    --type String --overwrite --value "$(date -u +%FT%TZ)"
```

値は暗号化されない。秘密の値を置くときの注意は [usage.md](usage.md#アプリの値) に
ある。

### アプリの状態

アプリは、いまの状態を 1 行の文字列でキー `status`（`/temt-<svc>/<アプリID>/status`）
に書ける。手元の `temt-<svc> status` が、アプリごとに値と、最後に書かれてからの
経過時間を出す（[usage.md](usage.md#状態を見る)）。改行や制御文字は `?` に置き換えられるので、1 行で書く。
時刻を含めるときは、インスタンスのローカル時刻にタイムゾーンの名前を添える
（`date '+%Y-%m-%d %H:%M:%S %Z'`）。temt は値を書き換えずに出すので、ssh で見る
時刻と揃い、[timezone-tokyo](apps/timezone-tokyo.md) を入れれば日本時間になる。

```bash
aws ssm put-parameter --region "$region" --name "/$name/myapp/status" \
    --type String --overwrite --value "3 job(s) queued"
```

インスタンスが終了しても値は残る（`app remove` と `destroy` で消える）ので、最後の
状態が後から見える。今動いているインスタンスが書いたとみなせない値には、`temt-<svc>
status` が `stale` を添える。`idle-shutdown` は、これで電源を切る予定を知らせる
（[apps/idle-shutdown.md](apps/idle-shutdown.md#手元から状態を見る)）。

## 自動終了と組み合わせる

サービスに `idle-shutdown` を入れていると、誰も使っていない状態が制限時間（既定は
1 時間）続いたインスタンスは終了する。ssh の接続や端末は、アプリが何もしなくても
使用中に数えられる。セキュリティグループで開けたポートで直接使われるアプリや、
ジョブを処理するアプリは、使われていることを自分で知らせる。やり方は
[apps/idle-shutdown.md](apps/idle-shutdown.md#アプリから使用中を知らせる) にある。

## 手本

temt と一緒に配る `idle-shutdown` も同じ形のアプリである。systemd のタイマー、PAM、
sshd の設定、ログインのメッセージを入れて外す `install` と `uninstall` がある。
入っている temt の中のディレクトリは、`temt app list` の `PATH` に出る。土台にするなら、写してから直す。

    temt app list
    mkdir -p ~/src/my-apps
    cp -r <PATH に出たディレクトリ> ~/src/my-apps/myapp

ソースは [`src/temt/apps/idle-shutdown/`](../src/temt/apps/idle-shutdown/) にある。
