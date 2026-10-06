"""ロールのインラインポリシーの中身と検査のテスト。ValidatePolicy は Stubber で差し替える。"""

import json
from typing import Any

import pytest
from botocore.stub import Stubber
from types_boto3_accessanalyzer.client import AccessAnalyzerClient

from temt import policy
from temt.msg import TemtError
from temt.names import Service

CONTENT: dict[str, Any] = {
    "Version": policy.VERSION,
    "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resources": "*"}],
}
REQUEST = {
    "policyDocument": json.dumps(CONTENT),
    "policyType": "IDENTITY_POLICY",
    "locale": "EN",
}


def a_finding(kind: str, code: str, path: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """validate-policy の指摘 1 つ。path は指摘の位置で、None なら位置を持たない。"""
    return {
        "findingDetails": f"details of {code}",
        "findingType": kind,
        "issueCode": code,
        "learnMoreLink": "https://example.com/",
        "locations": []
        if path is None
        else [
            {
                "path": path,
                "span": {
                    "start": {"line": 1, "column": 0, "offset": 0},
                    "end": {"line": 1, "column": 1, "offset": 1},
                },
            }
        ],
    }


def test_variables_are_the_name_and_the_service() -> None:
    assert policy.variables(Service("web")) == {"name": "temt-web", "service": "web"}


def test_a_policy_name_starts_with_the_prefix() -> None:
    assert policy.policy_name("jupyter") == "temt-jupyter"


def test_unknown_finds_the_first_variable_it_does_not_know() -> None:
    value = {"Resource": ["${temt:name}/*", "${temt:app}/*"], "${temt:bad}": "x"}
    assert policy.unknown(value, policy.VARIABLES) == "${temt:app}"


def test_unknown_looks_at_the_keys_of_tables_too() -> None:
    value = {"Condition": {"StringEquals": {"aws:ResourceTag/${temt:nope}": "x"}}}
    assert policy.unknown(value, policy.VARIABLES) == "${temt:nope}"


@pytest.mark.parametrize("written", ["${TEMT:name}", "${Temt:service}", "${temt:Name}"])
def test_unknown_does_not_take_another_case(written: str) -> None:
    """IAM は変数の名前の大文字小文字を区別しないので、取り違えると黙って効かない。"""
    assert policy.unknown({"Resource": f"{written}/*"}, policy.VARIABLES) == written


def test_document_stops_when_two_keys_become_one() -> None:
    statements = [
        {
            "Effect": "Allow",
            "Condition": {
                "StringEquals": {"aws:ResourceTag/${temt:service}": "a", "aws:ResourceTag/web": "b"}
            },
        }
    ]
    with pytest.raises(
        TemtError,
        match=(
            r"^web\.toml: role\.policy\.jupyter\[0\] has two keys that become "
            r"aws:ResourceTag/web after substituting the variables$"
        ),
    ):
        _ = policy.document(
            statements, policy.variables(Service("web")), "web.toml: role.policy.jupyter"
        )


def test_unknown_leaves_the_iam_variables_alone() -> None:
    value = {"Resource": "arn:aws:s3:::b/${aws:username}/*", "Effect": True}
    assert policy.unknown(value, policy.VARIABLES) is None


def test_document_substitutes_the_variables_in_values_and_keys() -> None:
    statements = [
        {
            "Effect": "Allow",
            "Action": ["ssm:GetParameter"],
            "Resource": "arn:aws:ssm:*:*:parameter/${temt:name}/*",
            "Condition": {"StringEquals": {"aws:ResourceTag/${temt:service}": "${temt:service}"}},
        }
    ]
    made = policy.document(statements, policy.variables(Service("web")), "role.")
    assert made == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["ssm:GetParameter"],
                "Resource": "arn:aws:ssm:*:*:parameter/temt-web/*",
                "Condition": {"StringEquals": {"aws:ResourceTag/web": "web"}},
            }
        ],
    }
    # 書かれた並びは書き換えない
    assert statements[0]["Resource"] == "arn:aws:ssm:*:*:parameter/${temt:name}/*"


def test_document_adds_allow_when_the_effect_is_missing() -> None:
    """Effect を省いた Statement には Allow を先頭に補う。書いたものはそのまま。"""
    statements = [{"Action": "s3:GetObject", "Resource": "*"}, {"Effect": "Deny", "Action": "*"}]
    made = policy.document(statements, policy.variables(Service("web")), "role.policy.x")
    assert made["Statement"] == [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"},
        {"Effect": "Deny", "Action": "*"},
    ]
    assert list(made["Statement"][0]) == ["Effect", "Action", "Resource"]
    assert "Effect" not in statements[0]


def test_validate_passes_when_there_is_no_finding(
    analyzer: AccessAnalyzerClient, capsys: pytest.CaptureFixture[str]
) -> None:
    with Stubber(analyzer) as stub:
        stub.add_response("validate_policy", {"findings": []}, REQUEST)
        assert policy.validate(analyzer, CONTENT, "temt-jupyter", "role.policy.jupyter") == 0
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err == ""


def test_validate_warns_and_goes_on_for_what_is_not_an_error(
    analyzer: AccessAnalyzerClient, capsys: pytest.CaptureFixture[str]
) -> None:
    findings = [
        a_finding(
            "SECURITY_WARNING",
            "PASS_ROLE_WITH_STAR_IN_RESOURCE",
            [{"value": "Statement"}, {"index": 0}, {"value": "Resource"}],
        ),
        a_finding("SUGGESTION", "EMPTY_ARRAY_ACTION"),
    ]
    with Stubber(analyzer) as stub:
        stub.add_response("validate_policy", {"findings": findings}, REQUEST)
        assert policy.validate(analyzer, CONTENT, "temt-jupyter", "role.policy.jupyter") == 0
    assert capsys.readouterr().err.splitlines() == [
        "temt: warning: role.policy.jupyter[0].Resource: SECURITY_WARNING "
        "PASS_ROLE_WITH_STAR_IN_RESOURCE: details of PASS_ROLE_WITH_STAR_IN_RESOURCE",
        "temt: warning: temt-jupyter: SUGGESTION EMPTY_ARRAY_ACTION: details of EMPTY_ARRAY_ACTION",
    ]


def test_validate_lists_every_error_and_counts_them(
    analyzer: AccessAnalyzerClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """ERROR は、すべて挙げて数を返す。止めるのは呼ぶ側。ページをまたいでも挙げる。"""
    first = [
        a_finding(
            "ERROR",
            "INVALID_POLICY_ELEMENT",
            [{"value": "Statement"}, {"index": 0}, {"value": "Resources"}],
        )
    ]
    second = [
        a_finding(
            "ERROR",
            "INVALID_ACTION",
            [{"value": "Statement"}, {"index": 1}, {"value": "Action"}, {"index": 2}],
        ),
        a_finding("WARNING", "MISSING_VERSION"),
    ]
    with Stubber(analyzer) as stub:
        stub.add_response("validate_policy", {"findings": first, "nextToken": "t"}, REQUEST)
        stub.add_response("validate_policy", {"findings": second}, {**REQUEST, "nextToken": "t"})
        assert policy.validate(analyzer, CONTENT, "temt-jupyter", "role.policy.jupyter") == 2
        stub.assert_no_pending_responses()
    assert capsys.readouterr().err.splitlines() == [
        "temt: role.policy.jupyter[0].Resources: ERROR INVALID_POLICY_ELEMENT: "
        "details of INVALID_POLICY_ELEMENT",
        "temt: role.policy.jupyter[1].Action[2]: ERROR INVALID_ACTION: details of INVALID_ACTION",
        "temt: warning: temt-jupyter: WARNING MISSING_VERSION: details of MISSING_VERSION",
    ]


def test_a_location_without_a_path_falls_back_to_the_name(
    analyzer: AccessAnalyzerClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """位置そのものが無ければポリシー名を出す。"""
    with Stubber(analyzer) as stub:
        stub.add_response("validate_policy", {"findings": [a_finding("WARNING", "X")]}, REQUEST)
        policy.validate(analyzer, CONTENT, "temt-jupyter", "role.policy.jupyter")
    assert "temt: warning: temt-jupyter: WARNING X" in capsys.readouterr().err


def test_a_key_in_the_path_is_written_like_a_value(
    analyzer: AccessAnalyzerClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Condition の中の位置は、テーブルのキーとして返る。"""
    path = [
        {"value": "Statement"},
        {"index": 0},
        {"value": "Condition"},
        {"key": "StringEquals"},
    ]
    with Stubber(analyzer) as stub:
        stub.add_response(
            "validate_policy", {"findings": [a_finding("WARNING", "X", path)]}, REQUEST
        )
        policy.validate(analyzer, CONTENT, "temt-jupyter", "role.policy.jupyter")
    assert "role.policy.jupyter[0].Condition.StringEquals: WARNING X" in capsys.readouterr().err


def test_a_finding_outside_the_statements_keeps_its_place(
    analyzer: AccessAnalyzerClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Statement の中でない位置は、[[role.policy.<名前>]] の位置の後ろに . でつなぐ。"""
    with Stubber(analyzer) as stub:
        stub.add_response(
            "validate_policy",
            {"findings": [a_finding("WARNING", "X", [{"value": "Version"}])]},
            REQUEST,
        )
        policy.validate(analyzer, CONTENT, "temt-jupyter", "role.policy.jupyter")
    assert "temt: warning: role.policy.jupyter.Version: WARNING X" in capsys.readouterr().err


def test_a_finding_on_the_statements_as_a_whole_names_the_policy_table(
    analyzer: AccessAnalyzerClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """位置が Statement の並び全体なら、[[role.policy.<名前>]] の位置として出す。"""
    with Stubber(analyzer) as stub:
        stub.add_response(
            "validate_policy",
            {"findings": [a_finding("WARNING", "X", [{"value": "Statement"}])]},
            REQUEST,
        )
        policy.validate(analyzer, CONTENT, "temt-jupyter", "role.policy.jupyter")
    assert "temt: warning: role.policy.jupyter: WARNING X" in capsys.readouterr().err
