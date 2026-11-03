# Changelog

## Unreleased

### Breaking changes

- `deploy` の記録が、SSM パラメータではなく、インスタンスの
  `/opt/temt/apps/<アプリID>/temt-deploy.json` に置かれるようになった。焼けば記録ごと
  AMI に入る。
  - インスタンスに入っているアプリは `status --apps` で表示される。`status` の `apps` の
    行は、`--apps` を付けたときだけ出る。
  - `app list` は手元のアプリだけを表示し、`--svc` を付けても AWS に問い合わせない。
    既定の列は `ID`、`NAME`、`LOCAL`、`PATH` になり、`VERSION`、`DEPLOYED`、`STATE`、
    `BAKED` の列は無くなった。`--profile` と `--region` は付けられない。
  - `app show` は、動いているインスタンスから Run Command で記録を読む。手元の資格情報に、
    `reboot` と同じ権限が要る。
  - `terminate` は、焼いていないアプリについて警告しなくなった。
  - `app set` は、まだ入れていないアプリにも書ける。
  - 上げるときにすること: 前の版で入れたアプリは、`status --apps` の `BAKED` が `-` に
    なり、前に与えたオプションの値と、`install` を実行したユーザーも引き継がれない。
    前と同じオプションと `--user` を付けて `deploy` し直し、焼く。`deploy` し直す前に
    `app remove` で外すときも、`--user` を付ける。
- `deploy` は、アプリの中のシンボリックリンクを辿り、リンク先の中身を送るようになった。
  手元で切れているリンクがあれば、`deploy` は止まる。
- `apply` と `service import` が設定ファイルを新しく書くとき、`--profile` か環境変数で
  選んだ AWS のプロファイルが `AwsProfile` に書かれるようになった。書かれたサービスに
  違う `--profile` を付けると引数の誤りになるので、別のアカウントに作るときは
  `AwsProfile` を消す。

### Changed

- アプリの `install` と `uninstall` は、アプリのディレクトリで実行される。中のファイルは
  名前だけで指せる。
- `idle-shutdown` のログインのメッセージに、`idle-shutdown --help` が案内される。

### Fixed

- 初回の `apply` で、作ったばかりのインスタンスプロファイルについての警告が出なくなった。
- `role import` が、`[role]` を他のテーブルを挟んで分けて書いた設定ファイルなどで異常終了
  しなくなった。書き直し方を告げて止まる。ドット付きキーの `role.` の後ろにある最上位の項目を、
  ポリシーの中に移してしまうことも無くなった。
- インスタンスが 2 台以上見つかったときなどの一覧の見出しが、実際に探すタグ `temt:service`
  を示すようになった。
- `swapfile` を入れたインスタンスで、使っているうちに `/swapfile` を消しても、`deploy` と
  `app remove` が失敗しなくなった。

## 1.0.0 - 2026-10-22

最初のリリース。
