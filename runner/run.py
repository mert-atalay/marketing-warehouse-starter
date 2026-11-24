"""Load the generated data into DuckDB, build the models, run the assertions."""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from runner.sqlx import Action, load_project

PROJECT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class AssertionResult:
    name: str
    failing_rows: int
    sample: list  # up to five failing rows, for the error message

    @property
    def passed(self) -> bool:
        return self.failing_rows == 0


def load_raw(con: duckdb.DuckDBPyConnection, data_dir: Path, sources: list[Action]) -> None:
    """Create one table per declared source from <data_dir>/<name>.parquet."""
    for source in sources:
        path = Path(data_dir) / f"{source.name}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing. Run `uv run python -m generator` first.")
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {source.schema}")
        con.execute(f"CREATE OR REPLACE TABLE {source.relation} AS SELECT * FROM read_parquet('{path}')")


def build_models(con: duckdb.DuckDBPyConnection, actions: list[Action]) -> list[Action]:
    """Create every view and table. `actions` must already be in dependency order."""
    models = [a for a in actions if a.type in ("view", "table")]
    for model in models:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {model.schema}")
        # A model may have changed type since the last run, so drop both kinds.
        for kind in ("VIEW", "TABLE"):
            try:
                con.execute(f"DROP {kind} IF EXISTS {model.relation}")
            except duckdb.CatalogException:
                pass  # it exists as the other kind
        con.execute(f"CREATE {model.type.upper()} {model.relation} AS\n{model.sql}")
    return models


def run_assertions(con: duckdb.DuckDBPyConnection, actions: list[Action]) -> list[AssertionResult]:
    """Run every assertion query. An assertion passes when it returns no rows."""
    results = []
    for assertion in (a for a in actions if a.type == "assertion"):
        rows = con.execute(assertion.sql).fetchall()
        results.append(AssertionResult(assertion.name, len(rows), rows[:5]))
    return results


def run(con: duckdb.DuckDBPyConnection, data_dir: Path, project_dir: Path = PROJECT_DIR) -> list[AssertionResult]:
    """The whole flow on an open connection. Returns the assertion results."""
    actions = load_project(project_dir)
    load_raw(con, data_dir, [a for a in actions if a.type == "declaration"])
    build_models(con, actions)
    return run_assertions(con, actions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data", help="directory with the generated Parquet files")
    parser.add_argument("--db", default="warehouse.duckdb", help="DuckDB file to build")
    args = parser.parse_args()

    actions = load_project(PROJECT_DIR)
    con = duckdb.connect(args.db)

    sources = [a for a in actions if a.type == "declaration"]
    load_raw(con, Path(args.data), sources)
    print(f"Loaded {len(sources)} source tables into {args.db}")

    for model in build_models(con, actions):
        rows = con.execute(f"SELECT COUNT(*) FROM {model.relation}").fetchone()[0]
        print(f"  built {model.type:<5} {model.relation:<44}{rows:>8} rows")

    results = run_assertions(con, actions)
    for result in results:
        print(f"  {'pass' if result.passed else 'FAIL'}  {result.name}")
        for row in result.sample if not result.passed else []:
            print(f"        {row}")
    con.close()

    failed = [r for r in results if not r.passed]
    print(f"{len(results) - len(failed)} of {len(results)} assertions passed")
    if failed:
        sys.exit(1)
