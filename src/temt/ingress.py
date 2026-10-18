"""sg list、sg add、sg remove の本体。設定ファイルの受信規則を見て、書き換える。

AWS は呼ばない。AWS 側を変えるのは apply だけで、ここは設定ファイルの
[[security-group.ingress]] だけを読み書きする。boto3 を使わないので、cli.run は
botocore を読み込む前にこれを呼ぶ。cli はこの module を、これらのコマンドを実行する
ときにだけ読み込む。問い合わせに使う urllib の読み込みを、ほかのコマンドの起動に
乗せないためである。

CIDR を省いたときは、手元のグローバル IP アドレスを調べて CIDR にする（own_cidrs）。
"""

import http.client
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

from temt import conf, msg, table
from temt.conf import RULE_PORT, Conf, Ingress
from temt.msg import TemtError
from temt.names import PREFIX, Service, defaults_path, tilde

COLUMNS = table.Columns(defaults=("PORT", "CIDR", "DESCRIPTION"))


@dataclass(frozen=True)
class AddressService:
    """グローバル IP アドレスの 1 つの種類と、それを教えてくれる問い合わせ先。"""

    version: int
    url: str
    # 規則にする CIDR の長さ
    prefix: int


# 問い合わせ先は、その種類のアドレスしか DNS に持たないので、必ずその種類で問い合わせる
ADDRESS_SERVICES = (
    AddressService(4, "https://checkip.amazonaws.com", 32),
    AddressService(6, "https://api6.ipify.org", 64),
)
# 接続と読み取りのそれぞれの待ちの上限（秒）
ADDRESS_TIMEOUT = 5
# 応答として読む長さの上限。IPv6 のアドレスと改行が収まればよい
ADDRESS_LIMIT = 100


def _label(rule: Ingress) -> str:
    """知らせる行に書く規則。"""
    return f"{conf.port_text(rule.Protocol, rule.Port)} {rule.Cidr}"


def list_rules(service: Service, spec: str | None) -> None:
    """受信規則を、共通ファイル、設定ファイルの順に、書かれた順で 1 つ 1 行の表で挙げる。"""
    chosen = COLUMNS.choose(spec)
    values = conf.load(service)
    table.write(
        chosen,
        [
            {
                "PORT": conf.port_text(one.Protocol, one.Port),
                "CIDR": one.Cidr,
                "DESCRIPTION": one.Description,
            }
            for one in values.security_group.ingress
        ],
    )


def _fetch(url: str) -> str:
    """url に GET で問い合わせ、応答の本文を返す。"""
    request = urllib.request.Request(url, headers={"User-Agent": PREFIX})
    with urllib.request.urlopen(request, timeout=ADDRESS_TIMEOUT) as response:
        body: bytes = response.read(ADDRESS_LIMIT)
    return body.decode("ascii", "replace")


# 問い合わせる関数。テストが差し替える
fetch: Callable[[str], str] = _fetch


def _reason(error: Exception) -> str:
    """問い合わせに失敗した理由。urllib の URLError は、元の理由を reason に持つ。

    HTTPError には状態コードを添える。
    """
    reason = getattr(error, "reason", None)
    text = str(reason if reason is not None else error) or type(error).__name__
    if isinstance(error, urllib.error.HTTPError):
        text = f"HTTP {error.code} {text}"
    return table.printable(text)


def _usable(address: IPv4Address | IPv6Address) -> bool:
    """規則にできるグローバル IP アドレスか。

    ゾーン ID の付いたアドレスと、手元の外から来ることの無いアドレス（ループバック、
    未指定、リンクローカル、マルチキャスト）は使わない。
    """
    if isinstance(address, IPv6Address) and address.scope_id:
        return False
    return not (
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
    )


def own_cidrs() -> list[str]:
    """手元のグローバル IP アドレスを調べ、規則にする CIDR を IPv4、IPv6 の順に返す。

    取れなかった種類は理由を 1 行で知らせる。どちらも取れなければ終了する。応答が
    規則にできる IP アドレスでなければ、その中身は出さない。
    """
    found: list[str] = []
    for one in ADDRESS_SERVICES:
        try:
            answer = fetch(one.url)
        except (OSError, ValueError, http.client.HTTPException) as e:
            msg.info(f"no IPv{one.version} egress address: {_reason(e)} ({one.url})")
            continue
        try:
            address = ip_address(answer.strip())
        except ValueError:
            address = None
        if address is None or not _usable(address):
            msg.info(f"no IPv{one.version} egress address: {one.url} did not answer an address")
            continue
        if address.version != one.version:
            msg.info(f"no IPv{one.version} egress address: {one.url} answered {address}")
            continue
        network = str(ip_network(f"{address}/{one.prefix}", strict=False))
        msg.info(f"egress IPv{one.version} address: {address} ({one.url}); using {network}")
        found.append(network)
    if not found:
        raise TemtError("could not find the egress IP address; give the CIDR")
    return found


def _rules(protocol: str, port: RULE_PORT, cidr: str | None, description: str) -> list[Ingress]:
    """指した規則。cidr が省かれていれば、グローバル IP アドレスから作る。"""
    cidrs = [cidr] if cidr is not None else own_cidrs()
    return [
        Ingress(Protocol=protocol, Port=port, Cidr=one, Description=description) for one in cidrs
    ]


def _positions(values: Conf) -> dict[tuple[str, RULE_PORT, str], int]:
    """規則を見分ける組から、設定ファイルでの位置への対応。"""
    return {conf.rule_key(one): index for index, one in enumerate(values.security_group.ingress)}


def _own(service: Service) -> Conf:
    """書き換える設定ファイルを、共通ファイルを重ねずに読む。

    重ねて読めない設定ファイル（共通ファイルと同じ規則を書いたものなど）は、ほかの
    コマンドと同じく、その誤りで止まる。
    """
    _ = conf.load(service)
    return conf.load(service, common=False)


def _shared(service: Service) -> dict[tuple[str, RULE_PORT, str], Ingress]:
    """共通ファイルの受信規則を、見分ける組から引けるようにしたもの。

    sg add と sg remove は、サービスの設定ファイルだけを書き換える。共通ファイルの規則は
    全サービスに効くので、そちらを直すよう告げる。Defaults = false のサービスには効かない
    ので、無いものとする。
    """
    if conf.written(service, conf.DEFAULTS) is False:
        return {}
    return {conf.rule_key(one): one for one in conf.common_rules()}


def _defaults() -> str:
    """知らせる行に書く共通ファイル。"""
    return table.printable(tilde(defaults_path()))


def _apply_hint(service: Service) -> None:
    """設定ファイルを書き換えたあと、AWS に反映する手を示す。"""
    msg.info(f"run '{PREFIX} apply --svc {service.svc}' to update the security group")


def add(
    service: Service,
    protocol: str,
    port: RULE_PORT,
    cidr: str | None,
    description: str | None,
) -> None:
    """設定ファイルに規則を足す。同じ規則があれば、description があるときだけ説明を書き換える。

    共通ファイルにある規則は足さない。その説明を変えるときは、共通ファイルを直すよう
    告げて終了する。

    cidr は正規化したもの。省かれていれば、グローバル IP アドレスから作る。
    """
    values = _own(service)
    path = table.printable(tilde(service.conf_path))
    positions = _positions(values)
    shared = _shared(service)
    rules = values.security_group.ingress
    added: list[Ingress] = []
    described: dict[int, str] = {}
    lines: list[str] = []
    for rule in _rules(protocol, port, cidr, description or ""):
        index = positions.get(conf.rule_key(rule))
        found = shared.get(conf.rule_key(rule))
        if found is not None:
            if description is not None and found.Description != description:
                raise TemtError(f"{_label(rule)} is in {_defaults()}; edit its Description there")
            lines.append(f"{_label(rule)} is already in {_defaults()}")
        elif index is None:
            added.append(rule)
            lines.append(f"added {_label(rule)} to {path}")
        elif description is not None and rules[index].Description != description:
            described[index] = description
            lines.append(f"updated the description of {_label(rule)} in {path}")
        else:
            lines.append(f"{_label(rule)} is already in {path}")
    if added or described:
        conf.edit_ingress(service, values, added=added, described=described)
    for line in lines:
        msg.info(line)
    if added or described:
        _apply_hint(service)


def remove(service: Service, protocol: str, port: RULE_PORT, cidr: str | None) -> None:
    """設定ファイルから規則を消す。1 つも消せなければ終了する。

    共通ファイルにある規則を指せば、何も消さずに、共通ファイルを直すよう告げて終了する。

    cidr は正規化したもの。省かれていれば、グローバル IP アドレスから作る。
    """
    values = _own(service)
    path = table.printable(tilde(service.conf_path))
    positions = _positions(values)
    shared = _shared(service)
    removed: list[int] = []
    missing: list[str] = []
    for rule in _rules(protocol, port, cidr, ""):
        index = positions.get(conf.rule_key(rule))
        if conf.rule_key(rule) in shared:
            raise TemtError(f"{_label(rule)} is in {_defaults()}; remove it there")
        if index is None:
            missing.append(_label(rule))
        else:
            removed.append(index)
    if not removed:
        raise TemtError(f"no rule {' or '.join(missing)} in {path}")
    labels = [_label(values.security_group.ingress[index]) for index in removed]
    conf.edit_ingress(service, values, removed=removed)
    for label in labels:
        msg.info(f"removed {label} from {path}")
    for label in missing:
        msg.info(f"no rule {label} in {path}")
    _apply_hint(service)
