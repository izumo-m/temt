"""--svc を省いた app list のテスト。manifest.toml の読み込みは test_deploy.py にある。"""

import re
from pathlib import Path

import pytest

from temt import manifest
from temt.msg import UsageError

TIME_RE = re.compile(r"\d\d:\d\d:\d\d ")


def told(err: str) -> list[str]:
    """知らせた行から時刻を外したもの。"""
    return [TIME_RE.sub("", line) for line in err.splitlines()]


def a_local_app(home: Path, app_id: str, text: str | None) -> Path:
    """~/.config/temt/apps/ にアプリを置く。text が None なら manifest.toml を置かない。"""
    where = home / ".config" / "temt" / "apps" / app_id
    where.mkdir(parents=True)
    if text is not None:
        (where / "manifest.toml").write_text(text, encoding="utf-8")
    return where


def test_list_shows_the_apps_in_the_apps_directory(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """アプリ ID の順に並べる。ディレクトリでないものと、規則に外れた名前は挙げない。"""
    a_local_app(home_dir, "mise", 'name = "mise"\nversion = "1"\n')
    a_local_app(home_dir, "jupyter", 'name = "Jupyter Lab"\nversion = "1.3"\n')
    a_local_app(home_dir, "Bad", 'name = "bad"\nversion = "1"\n')
    apps = home_dir / ".config" / "temt" / "apps"
    (apps / "file").write_text("", encoding="utf-8")
    (apps / "gone").symlink_to(home_dir / "nothing")
    elsewhere = a_local_app(home_dir / "src", "linked", 'name = "Linked"\nversion = "2"\n')
    (apps / "linked").symlink_to(elsewhere)

    manifest.list_apps(None)

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "ID        NAME          LOCAL   PATH",
        "jupyter   Jupyter Lab   1.3     ~/.config/temt/apps/jupyter",
        "linked    Linked        2       ~/.config/temt/apps/linked",
        "mise      mise          1       ~/.config/temt/apps/mise",
    ]
    assert captured.err == ""


def test_list_shows_the_apps_that_come_with_temt(
    home_dir: Path, no_bundled_apps: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """apps/ に同じアプリ ID があれば、deploy と同じくそちらを挙げる。"""
    (no_bundled_apps / "swapfile").mkdir(parents=True)
    a_local_app(home_dir, "swapfile", 'name = "my swap"\nversion = "9"\n')
    a_local_app(home_dir, "mise", 'name = "mise"\nversion = "1"\n')
    bundled = no_bundled_apps / "idle-shutdown"
    bundled.mkdir()
    (bundled / "manifest.toml").write_text('name = "idle"\nversion = "0.2"\n', encoding="utf-8")
    (no_bundled_apps / "__pycache__").mkdir()

    manifest.list_apps(None)

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "ID              NAME      LOCAL   PATH",
        "idle-shutdown   idle      0.2     ~/bundled/idle-shutdown",
        "mise            mise      1       ~/.config/temt/apps/mise",
        "swapfile        my swap   9       ~/.config/temt/apps/swapfile",
    ]
    assert captured.err == ""


def test_list_shows_the_description_when_chosen(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_local_app(
        home_dir, "jupyter", 'name = "Jupyter"\nversion = "1"\ndescription = "Notebooks\\tand"\n'
    )
    a_local_app(home_dir, "mise", 'name = "mise"\nversion = "1"\n')

    manifest.list_apps("id,description,local")

    assert capsys.readouterr().out.splitlines() == [
        "ID        DESCRIPTION     LOCAL",
        "jupyter   Notebooks?and   1",
        "mise      -               1",
    ]


def test_list_warns_about_a_manifest_it_cannot_read(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """読めない manifest.toml は、警告してアプリ ID だけを出し、残りを並べる。"""
    a_local_app(home_dir, "broken", 'name = "broken"\n')
    a_local_app(home_dir, "empty", None)
    a_local_app(home_dir, "mise", 'name = "mise"\nversion = "1"\n')

    manifest.list_apps("id,name,local,description")

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "ID       NAME   LOCAL   DESCRIPTION",
        "broken   -      -       -",
        "empty    -      -       -",
        "mise     mise   1       -",
    ]
    assert told(captured.err) == [
        "temt: warning: cannot read the manifest of broken: "
        "~/.config/temt/apps/broken/manifest.toml: version is missing",
        "temt: warning: cannot read the manifest of empty: "
        "no manifest.toml in ~/.config/temt/apps/empty",
    ]


def test_list_without_the_apps_directory_shows_only_the_header(
    home_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home_dir

    manifest.list_apps(None)

    assert capsys.readouterr().out.splitlines() == ["ID   NAME   LOCAL   PATH"]


@pytest.mark.parametrize("spec", ["version", "deployed", "state", "baked", "bogus"])
def test_list_rejects_the_columns_of_a_service(home_dir: Path, spec: str) -> None:
    """VERSION などはサービスごとの値なので、--svc を省いたときは選べない。"""
    _ = home_dir
    with pytest.raises(UsageError, match=f"unknown column in --format: {spec}; use ID,NAME,"):
        manifest.list_apps(spec)
