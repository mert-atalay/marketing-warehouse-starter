.PHONY: setup data build test compile parity all clean

setup:    ## install Python dependencies
	uv sync

data:     ## generate the synthetic CSV and Parquet files into data/
	uv run python -m generator

build:    ## load data into DuckDB, build every model, run every assertion
	uv run python -m runner

test:     ## run the test suite
	uv run pytest

compile:  ## compile the Dataform project (needs Node, no cloud credentials)
	npx --yes @dataform/cli@3.1.7 compile

parity:   ## check the runner's SQL against the Dataform compiler's SQL
	RUN_DATAFORM_COMPILE=1 uv run pytest tests/test_dataform_parity.py

all: setup data build test

clean:
	rm -rf data warehouse.duckdb
