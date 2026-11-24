from pathlib import Path

import pytest

from runner.sqlx import js_object_to_dict, load_project, split_config

PROJECT_DIR = Path(__file__).resolve().parent.parent


def test_config_block_is_stripped_and_parsed():
    text = """
    config {
      type: "table",
      schema: 'marts',
      description: "Braces { } and a colon: inside a string are fine",
      tags: ["daily", "marts",],
    }

    SELECT 1 AS x
    """
    config, body = split_config(text)
    assert config == {
        "type": "table",
        "schema": "marts",
        "description": "Braces { } and a colon: inside a string are fine",
        "tags": ["daily", "marts"],
    }
    assert body == "SELECT 1 AS x"


def test_js_object_with_nested_object():
    assert js_object_to_dict('{schema: "raw", name: "x", extra: {a: 1, b: true}}') == {
        "schema": "raw",
        "name": "x",
        "extra": {"a": 1, "b": True},
    }


def _write_project(root: Path, files: dict) -> Path:
    (root / "definitions").mkdir()
    (root / "workflow_settings.yaml").write_text(
        'defaultDataset: marts\ndefaultAssertionDataset: assertions\nvars:\n  cutoff: "2025-01-31"\n'
    )
    for name, text in files.items():
        (root / "definitions" / name).write_text(text)
    return root


def test_refs_vars_and_run_order(tmp_path):
    project = _write_project(
        tmp_path,
        {
            # Named so that alphabetical order is the wrong run order.
            "a_final.sqlx": 'config { type: "table" }\n'
            'SELECT * FROM ${ref("m_middle")} JOIN ${ref({schema: "raw", name: "src"})} USING (id)\n'
            "WHERE d <= '${dataform.projectConfig.vars.cutoff}'",
            "m_middle.sqlx": 'config { type: "view", schema: "staging" }\nSELECT * FROM ${ref("raw", "src")}',
            "src.sqlx": 'config { type: "declaration", schema: "raw" }',
            "z_check.sqlx": 'config { type: "assertion" }\nSELECT * FROM ${ ref("a_final") } WHERE id IS NULL;',
        },
    )
    actions = load_project(project)
    assert [a.relation for a in actions] == ["raw.src", "staging.m_middle", "marts.a_final", "assertions.z_check"]
    by_name = {a.name: a for a in actions}
    assert by_name["a_final"].sql == (
        "SELECT * FROM staging.m_middle JOIN raw.src USING (id)\nWHERE d <= '2025-01-31'"
    )
    assert by_name["z_check"].sql == "SELECT * FROM marts.a_final WHERE id IS NULL"


@pytest.mark.parametrize(
    "body",
    [
        'SELECT * FROM ${ref("does_not_exist")}',  # unknown ref
        "SELECT ${self()}",  # JavaScript the runner does not handle
        "SELECT '${dataform.projectConfig.vars.missing}'",  # unknown var
    ],
)
def test_unsupported_sqlx_fails_loudly(tmp_path, body):
    project = _write_project(tmp_path, {"model.sqlx": 'config { type: "view" }\n' + body})
    with pytest.raises(ValueError):
        load_project(project)


def test_real_project_has_no_bigquery_only_syntax():
    """The models are meant to run unchanged on DuckDB and BigQuery."""
    banned = ("SAFE_DIVIDE", "DATE_TRUNC", "SAFE_CAST", "DATE_DIFF", "DATE_SUB", "DATE_ADD", "QUALIFY", "`")
    for action in load_project(PROJECT_DIR):
        for word in banned:
            assert word not in action.body.upper(), f"{action.path.name} uses {word}"
