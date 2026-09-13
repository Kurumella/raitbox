# Contributing to RaitBox

Thanks for helping build RaitBox! This project is an E2B-compatible sandbox SDK
over AWS compute. The bar: changes keep the E2B surface intact, stay typed, and
ship with tests that run without AWS credentials.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate   # or: conda activate <env>
make install                                    # pip install -e '.[dev,server]'
```

## Before you push

```bash
make check   # ruff (lint + format), mypy, pytest — exactly what CI runs
```

- **Lint/format:** `ruff` (`make lint` / `make format`). Line length 100.
- **Types:** `mypy raitbox` must pass. RaitBox ships `py.typed`.
- **Tests:** `pytest` runs unit tests with in-memory fakes — no AWS needed.
  Live tests are marked `@pytest.mark.integration` and are skipped by default;
  run them explicitly with `pytest -m integration` (they bill short sessions).

## Design guidelines

- **Keep the E2B surface.** Public methods, kwargs, and return types on
  `Sandbox`/`AsyncSandbox`/`files`/`commands` should match `e2b` /
  `e2b_code_interpreter`. When a backend can't support something, raise
  `NotSupportedError` with the supported alternative — don't silently no-op.
- **Isolate AWS specifics in `src/raitbox/backends/`.** The facades in
  `src/raitbox/client/` stay backend-agnostic and delegate to a `Backend`.
- **Validate AWS shapes against the service model**, not memory:
  `botocore.session.get_session().get_service_model("lambda-microvms")`.
- Add a test for every behavior change; put fakes in `tests/conftest.py`.

## Adding a backend

Subclass `raitbox.backends.base.Backend`, implement `open`/`kill`/`run_command`
/`file_*`, and register it in `src/raitbox/backends/__init__.py` and the aliases in
`src/raitbox/core/settings.py`.
