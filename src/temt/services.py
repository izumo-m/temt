"""service list の本体。手元の設定ファイルを、1 つ 1 サービスとして並べる。

AWS は呼ばない。docker の image ls が手元のイメージを挙げるのと同じく、定義を見る
ためのコマンドである。boto3 を使わないので、cli.run は botocore を読み込む前に
これを呼ぶ。
"""

from pathlib import Path
from typing import Any

from temt import conf, ltdata, msg, table
from temt.conf import INSTANCE_TYPE, UNSET, Conf
from temt.msg import TemtError
from temt.names import Service, conf_dir, defaults_path, exists

# 設定ファイルの拡張子
SUFFIX = ".toml"
# 読まないファイルの名前の頭
HIDDEN = "."

COLUMNS = table.Columns(
    defaults=("SERVICE", "PROFILE", "REGION", "IMAGE", "TYPE"),
    extras=("SUBNET", "INGRESS"),
)


def _value(value: Any) -> str:
    """設定ファイルの値の表示。値が無ければ空にし、table が EMPTY と出す。"""
    return "" if value is None or value is UNSET else str(value)


def _row(service: Service, values: Conf) -> dict[str, str]:
    """サービス 1 つの行。値は読んだときに効くもので、InstanceType は書かれていなければ
    既定を出す。
    """
    template = values.launch_template
    first = ltdata.element(template.get("NetworkInterfaces") or [], "DeviceIndex", 0) or {}
    return {
        "SERVICE": service.svc,
        "PROFILE": _value(values.AwsProfile),
        "REGION": _value(values.AwsRegion),
        "IMAGE": _value(template.get("ImageId")),
        "TYPE": _value(template.get("InstanceType") or INSTANCE_TYPE),
        "SUBNET": _value(first.get("SubnetId")),
        "INGRESS": str(len(values.security_group.ingress)),
    }


def _files(place: Path) -> list[Path]:
    """conf ディレクトリの設定ファイル。サービス名の順。ディレクトリが無ければ空。

    . で始まるファイルは、シェルの * と同じく含めない。エディタのロックファイル
    （.#<svc>.toml）などで警告しないためである。共通ファイルも含めない。
    """
    if not exists(place):
        return []
    return sorted(
        (
            path
            for path in place.iterdir()
            if path.name.endswith(SUFFIX)
            and not path.name.startswith(HIDDEN)
            and path.name != defaults_path().name
        ),
        key=lambda path: path.name.removesuffix(SUFFIX),
    )


def list_services(service: Service | None, spec: str | None) -> None:
    """設定ファイルのあるサービスを、その中身とともに並べる。

    service があれば、その設定ファイルだけを読み、1 行を出す。無ければ、読めない
    ファイルと、名前がサービス名の規則に合わないファイルは、警告して飛ばし、残りを
    並べてから誤りとして終える。共通ファイルが読めなければ、何も並べずに終える。
    """
    chosen = COLUMNS.choose(spec)
    if service is not None:
        # 無いか読めなければ、他の --svc のコマンドと同じく、apply を促すか理由を告げて止まる
        table.write(chosen, [_row(service, conf.load(service))])
        return
    # どのファイルも共通ファイルを重ねて読むので、共通ファイルが読めなければ、ファイル
    # ごとに同じ警告を並べずに、ここで止まる
    _ = conf.common_values()
    rows: list[dict[str, str]] = []
    files = _files(conf_dir())
    skipped = 0
    for path in files:
        try:
            found = Service(path.name.removesuffix(SUFFIX))
        except TemtError as e:
            msg.warn(f"skipped: {table.printable(str(path))}: {e}")
            skipped += 1
            continue
        try:
            # 行き先の無いシンボリックリンクは、conf.load に渡すと apply を促してしまう。
            # 循環するリンクなどは、exists が理由とパスを添えて TemtError にする
            if path.is_symlink() and not exists(path):
                raise TemtError(f"{table.printable(str(path))}: broken symlink")
            rows.append(_row(found, conf.load(found)))
        except TemtError as e:
            # ここで上がる TemtError の文言は、どれもパスを含む
            msg.warn(f"skipped: {e}")
            skipped += 1
        except OSError as e:
            msg.warn(f"skipped: {table.printable(str(path))}: {e.strerror}")
            skipped += 1
    table.write(chosen, rows)
    if skipped:
        raise TemtError(f"skipped {skipped} of {len(files)} config files")
