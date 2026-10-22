# temt の開発

temt を直す人に向けた決まりと手順。使い方は [README.md](README.md) と
[docs/usage.md](docs/usage.md) にある。

## 開発環境

python と uv は mise が用意する（[mise.toml](mise.toml)）。リポジトリのディレクトリに
入ると、mise が `.venv` を作って有効にする。次を実行すると、python と uv が入り、
`.venv` に temt と開発に使う道具が入る。

    mise trust && mise install
    uv sync

## テスト

commit する前と、Pull Request を出す前に、次を通す。

    mise run check

中身は、`ruff check`、`ruff format --check`、`basedpyright`、`pytest` である。
python 3.14 でのテストは、別の環境 `.venv314` で動かす。

    mise run check-py314

### 実機での確認

AWS とのやり取りを本物で通しで確かめるときは、自分の `~/.ssh` と `~/.config/temt` に
触れないよう、仮のホームで試験用のサービスを 1 つ立てる。

リポジトリのディレクトリで、仮のホームを作り、それを `HOME` にしたシェルを開く。
ログインの鍵は、仮のホームの中に作られる。

    home=$(mktemp -d)
    ln -s ~/.aws "$home/.aws"
    mkdir -p "$home/.config/temt" "$home/bin"
    printf 'AwsRegion = "ap-northeast-1"\n' > "$home/.config/temt/verify.toml"
    cat > "$home/bin/ssh" <<EOF
    #!/bin/sh
    sed "s|~/|$home/|g" "$home/.ssh/temt/config" > "$home/.ssh/verify-config"
    exec $(command -v ssh) -F "$home/.ssh/verify-config" "\$@"
    EOF
    chmod +x "$home/bin/ssh"
    HOME="$home" PATH="$home/bin:$PWD/.venv/bin:$PATH" bash

ssh は、`~/.ssh/config` をログインユーザーのホームから読み、設定ファイルの `~` も
そこで展開する。`$home/bin/ssh` は、仮のホームの ssh の設定の `~/` を書き換えたコピーを
`-F` で読ませる。`deploy` などで temt が呼ぶ ssh も、これを使う。

開いたシェルで試す。

    temt apply --svc verify --yes
    temt start --svc verify
    ssh temt-verify

終わったら、作ったものを消す。`bake` したなら、AMI も消す。

    temt destroy --svc verify --yes
    temt ami delete <AMI の ID> --svc verify --yes
    exit
    rm -rf "$home"

## 手元の temt の入れ替え

直した temt を、ふだん使う `temt` として試すには、次を実行する。

    mise run install

uv が temt を `~/.local/bin/temt` に入れる。mise で入れたリリース版とは両方入る。
uv で入れたリリース版は、これに置き換わる。直したら、もう一度実行する。

## commit メッセージ

Conventional Commits の形（`type(scope): subject`、scope は省いてよい）で書く。
`type` と `scope` は Conventional Commits の語をそのまま使い、subject（件名）と本文は
日本語で書く。

    feat(service)!: サービスのコマンドをシェルスクリプトにし、temt を PATH から呼ぶ

## 言語

- 文書（README、CONTRIBUTING、SECURITY、CLAUDE.md、docs/）とコード内のコメントは日本語で
  書く。設計書（design/）は英語で書く。
- コンソールに出す文は英語で、ASCII 文字だけを使う。
- Issue と Pull Request は、日本語でも英語でもよい。

## 設計書

設計書は [design/](design/) にある。選択肢が分かれたときに何を優先するかは
[design/principles.md](design/principles.md) にある。

## Issue

不具合には、次を書く。

- `temt --version` の出力
- OS とその版
- 打ったコマンドと、その出力

## Pull Request

向け先は `develop` にする。`main` はリリースにだけ使う。大きな変更や、設計に
関わる変更は、先に Issue で相談する。

## リリース

1. `develop` で `pyproject.toml` の `version` を上げ、`uv lock` で `uv.lock` の
   temt の番号も揃える。`mise run check` を通してから、`chore(release): <番号>` で
   commit する。前のリリースから破壊的変更（`!`）を含めば 1 桁目を、そうでなく
   `feat` を含めば 2 桁目を、それ以外だけなら 3 桁目を上げ、上げた桁より下は 0 に
   戻す。
2. `main` で `git merge --no-ff develop` する。
3. マージした commit に、軽量の git のタグ `v<番号>` を付ける。
4. `git push origin main develop v<番号>` で push し、`develop` に戻る。
5. `gh release create v<番号> --verify-tag --generate-notes` で GitHub Release を
   作る。mise の `pypi` バックエンドは、`latest` を最新の GitHub Release に解決する。

番号を上げるのは、`src/temt/` の下か、`pyproject.toml` の依存が変わったときである。
それ以外（`docs/`、`design/`、`README.md`、`tests/`、`mise.toml`、`uv.lock` など）だけのときは、
番号を上げず、git のタグも付けない。`main` へのマージと、
`git push origin main develop` だけを行う。
