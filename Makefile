.PHONY: install lint format typecheck test check

install:
	pip install -e '.[dev,server]'

lint:
	ruff check src tests

format:
	ruff format src tests

typecheck:
	mypy src/raitbox

test:
	pytest -q

# Everything CI runs.
check: lint typecheck test
	ruff format --check src tests
