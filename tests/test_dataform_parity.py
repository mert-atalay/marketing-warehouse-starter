"""Check the local runner against the real Dataform compiler.

Skipped unless RUN_DATAFORM_COMPILE=1, because it needs Node and downloads
@dataform/cli from npm. It needs no cloud credentials.
"""

import json
import os
import re
import subprocess

import pytest

from runner.sqlx import load_project, load_settings
from tests.conftest import PROJECT_DIR

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATAFORM_COMPILE") != "1", reason="set RUN_DATAFORM_COMPILE=1 to run (needs Node and npm)"
)


def test_runner_sql_matches_dataform_compiled_sql():
    version = load_settings(PROJECT_DIR)["dataformCoreVersion"]
    output = subprocess.run(
        ["npx", "--yes", f"@dataform/cli@{version}", "compile", "--json"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    compiled = json.loads(output[output.index("{") :])
    assert not compiled.get("graphErrors")

    local = {a.relation: a for a in load_project(PROJECT_DIR)}
    compiled_actions = compiled["tables"] + compiled["assertions"]
    assert len(compiled_actions) + len(compiled["declarations"]) == len(local)

    for action in compiled_actions:
        target = action["target"]
        mine = local[f"{target['schema']}.{target['name']}"]
        # Dataform writes `project.schema.name`. The runner writes schema.name.
        query = re.sub(r"`[^`.]+\.([^`.]+)\.([^`.]+)`", r"\1.\2", action["query"]).strip()
        assert query == mine.sql
        dependencies = {f"{d['schema']}.{d['name']}" for d in action.get("dependencyTargets", [])}
        assert dependencies == {d.relation for d in mine.depends_on}
