"""ロールに付けるインラインポリシーの中身を作り、IAM Access Analyzer で検査する。

中身は IAM のポリシーの JSON を Python の値にしたもので、temt の変数
${temt:<変数名>} を書ける。変数の置き換えと、知らない変数の検出は AWS を呼ばない。
検査は ValidatePolicy に任せ、temt は書式も中身も自分では確かめない。

boto3 は型の注釈にだけ使うので、この module は設定ファイルを読むときにも読み込める。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from temt import msg
from temt.msg import TemtError
from temt.names import PREFIX
from temt.table import printable

if TYPE_CHECKING:
    from types_boto3_accessanalyzer.client import AccessAnalyzerClient

    from temt.names import Service

# <svc>.toml の [[role.policy.<ポリシー名>]] から作るインラインポリシーの名前の頭。後ろに
# <ポリシー名> が付く。ロールに付いたインラインポリシーのうち、これで始まるものを
# temt が持つ
POLICY_PREFIX = f"{PREFIX}-"
# temt が中身に書くポリシーの言語の版
VERSION = "2012-10-17"
# ポリシーの中身の、Statement の並びのキー。ValidatePolicy の指摘の位置もここから始まる
STATEMENT = "Statement"
# Statement の Effect と、書かれていないときに補う値
EFFECT = "Effect"
ALLOW = "Allow"
# Statement に書ける変数
VARIABLES = ("name", "service")

# 変数の書き方。temt の変数と取り違えやすい ${TEMT:name} も拾い、unknown で止める。
# IAM は変数の名前の大文字小文字を区別しないが、temt は小文字だけを変数とする
VARIABLE_RE = re.compile(r"\$\{temt:([^}]*)\}", re.IGNORECASE)
VARIABLE_PREFIX = "${temt:"

# ValidatePolicy の指摘の種類。ERROR は IAM が受け付けないか効かないもので、止める。
# 他は警告として知らせ、続ける
ERROR = "ERROR"


def variables(service: Service) -> dict[str, str]:
    """Statement の変数と、その値。"""
    return {"name": service.name, "service": service.svc}


def policy_name(name: str) -> str:
    """[[role.policy.<ポリシー名>]] から作るインラインポリシーの名前 temt-<ポリシー名>。"""
    return f"{POLICY_PREFIX}{name}"


def _strings(value: Any) -> Sequence[str]:
    """value の中の文字列をすべて。表のキーも含める。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for key, item in value.items() for text in (*_strings(key), *_strings(item))]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def unknown(value: Any, known: Sequence[str]) -> str | None:
    """value の中にある、known に無い最初の変数。すべて知っていれば None。"""
    for text in _strings(value):
        for found in VARIABLE_RE.finditer(text):
            if not found.group(0).startswith(VARIABLE_PREFIX) or found.group(1) not in known:
                return found.group(0)
    return None


def substitute(value: Any, values: Mapping[str, str]) -> Any:
    """value の中の変数を values で置き換えたもの。表のキーも置き換える。

    知らない変数は、読むときに unknown で止めてあるものとする。置き換えた結果、同じ表の
    キーが重なれば ValueError で止める。黙って片方を捨てると、書いた条件が消えるから
    である。
    """
    if isinstance(value, str):
        return VARIABLE_RE.sub(lambda found: values[found.group(1)], value)
    if isinstance(value, Mapping):
        made: dict[str, Any] = {}
        for key, item in value.items():
            new = substitute(key, values)
            if new in made:
                raise ValueError(
                    f"has two keys that become {printable(new)} after substituting the variables"
                )
            made[new] = substitute(item, values)
        return made
    if isinstance(value, list):
        return [substitute(item, values) for item in value]
    return value


def document(
    statements: Sequence[Mapping[str, Any]], values: Mapping[str, str], prefix: str
) -> dict[str, Any]:
    """Statement の並びから、Effect を補い、変数を置き換えたポリシーの中身を作る。

    Effect の無い Statement には Allow を先頭に補う。prefix は誤りの位置の前に置く、
    利用者が書いた場所（"<ファイル>: role.policy.<ポリシー名>" など）。
    """
    made: list[Any] = []
    for index, one in enumerate(statements):
        try:
            statement = substitute(one, values)
        except ValueError as error:
            raise TemtError(f"{prefix}[{index}] {error}") from None
        if EFFECT not in statement:
            statement = {EFFECT: ALLOW, **statement}
        made.append(statement)
    return {"Version": VERSION, STATEMENT: made}


def _where(finding: Mapping[str, Any]) -> str | None:
    """指摘の位置。最初の位置だけを、Statement の中なら [0].Resource のような形にする。

    利用者が書くのは Statement の並びだけなので、先頭の Statement は省く。位置が
    Statement の並び全体なら空文字列、位置そのものが無ければ None。
    """
    locations = finding.get("locations") or []
    if not locations:
        return None
    where = ""
    for position, element in enumerate(locations[0].get("path") or []):
        if position == 0 and element.get("value") == STATEMENT:
            continue
        if "index" in element:
            where += f"[{element['index']}]"
        elif "value" in element:
            where += f".{element['value']}" if where else element["value"]
        elif "key" in element:
            where += f".{element['key']}" if where else element["key"]
    return where


def validate(
    analyzer: AccessAnalyzerClient, content: Mapping[str, Any], name: str, prefix: str
) -> int:
    """ポリシー name の中身 content を ValidatePolicy にかけ、ERROR の数を返す。

    ERROR はすべて挙げ、止めるのは呼ぶ側に任せる。他の指摘は警告として知らせる。prefix は
    指摘の位置の前に置く、利用者が書いた場所（"role.policy.<ポリシー名>" など）。位置の
    無い指摘はポリシーの名前を添える。
    """
    errors = 0
    for page in analyzer.get_paginator("validate_policy").paginate(
        policyDocument=json.dumps(content), policyType="IDENTITY_POLICY", locale="EN"
    ):
        for finding in page.get("findings", []):
            # 位置と指摘の文は、利用者が書いた Statement のキーや値を含みうる
            where = _where(finding)
            if where is None:
                place = name
            elif not where or where.startswith("["):
                place = f"{prefix}{printable(where)}"
            else:
                place = f"{prefix}.{printable(where)}"
            kind = finding["findingType"]
            line = f"{place}: {kind} {finding['issueCode']}: {printable(finding['findingDetails'])}"
            if kind == ERROR:
                msg.error(line)
                errors += 1
            else:
                msg.warn(line)
    return errors
