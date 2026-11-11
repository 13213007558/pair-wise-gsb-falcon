# Feedback task starting environment

Upstream: https://github.com/falconry/falcon
Upstream commit: `ff96e13f45eb8c70c8b1ad9e928228d9cdee4d78`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC FALCON_DISABLE_CYTHON=1 UV_NO_CONFIG=1

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= tests/test_middleware.py tests/test_request_media.py
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-11): `  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))`
Whole-command wall time: 0.640 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
