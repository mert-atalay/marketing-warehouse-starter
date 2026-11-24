import csv
from pathlib import Path

import duckdb
import pytest

from generator.generate import generate
from runner.run import run

PROJECT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    """Default synthetic data, generated once per test run."""
    out = tmp_path_factory.mktemp("data")
    generate(out)
    return out


@pytest.fixture(scope="session")
def raw(data_dir) -> dict:
    """The generated CSV files as lists of dicts, read without any SQL.

    Known-answer tests compute their expected values from these in plain Python,
    so they do not share logic with the models they check.
    """
    tables = {}
    for path in data_dir.glob("*.csv"):
        with path.open(newline="") as handle:
            tables[path.stem] = list(csv.DictReader(handle))
    return tables


@pytest.fixture(scope="session")
def built(data_dir):
    """(connection, assertion results) after the full build on the default data."""
    con = duckdb.connect()
    results = run(con, data_dir, PROJECT_DIR)
    yield con, results
    con.close()


@pytest.fixture
def con(built):
    """A cursor on the built warehouse. Tests only read from it."""
    return built[0].cursor()
