from __future__ import annotations

import pytest

from govcheck.checks.llm.f03_assessment import _rubric_lines
from govcheck.prompts.rubrics import load_rubrics


def test_rubric_yaml_loads_and_caps_each_item_at_eight_lines(tmp_path):
    path = tmp_path / "rubrics.yaml"
    lines = "\n".join(f"      - 判準 {index}" for index in range(1, 11))
    path.write_text(
        f"schema_version: 1\nitems:\n  1-1:\n    lines:\n{lines}\n",
        encoding="utf-8",
    )

    result = load_rubrics(path)

    assert result == {"1-1": [f"判準 {index}" for index in range(1, 9)]}


def test_missing_rubric_yaml_fails_closed(tmp_path):
    assert load_rubrics(tmp_path / "missing.yaml") == {}


@pytest.mark.parametrize(
    "body",
    [
        "schema_version: [\n",
        "- schema_version: 1\n",
        "schema_version: 2\nitems: {}\n",
        "schema_version: 1\nitems: []\n",
        "schema_version: 1\nitems:\n  bad-id:\n    lines: [判準]\n",
        "schema_version: 1\nitems:\n  1-1: not-a-map\n",
        "schema_version: 1\nitems:\n  1-1:\n    lines: []\n",
        "schema_version: 1\nitems:\n  1-1:\n    lines: [判準, 123]\n",
        "schema_version: 1\nitems:\n  1-1:\n    lines: [判準, '  ']\n",
    ],
)
def test_any_rubric_schema_error_discards_the_whole_map(tmp_path, body):
    path = tmp_path / "rubrics.yaml"
    path.write_text(body, encoding="utf-8")

    assert load_rubrics(path) == {}


def test_non_utf8_rubric_file_never_raises(tmp_path):
    path = tmp_path / "rubrics.yaml"
    path.write_bytes(b"\xff\xfe\xfa")

    assert load_rubrics(path) == {}


def test_rubric_result_length_mismatch_discards_all_lines():
    assert _rubric_lines(["判準一", "判準二"], [True]) == []
