# SPDX-License-Identifier: MIT

.DEFAULT_GOAL := all

.PHONY: all format lint test

all: lint test

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check

test:
	uv run pytest
