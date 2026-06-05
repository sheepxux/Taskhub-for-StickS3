# Host adapter tests

Regression tests for the accuracy-critical parsing in `host/task_hub.py`:
status derivation, WAIT detection, case-insensitive process matching, token
accounting, transcript/session scan memoisation, and the external ingest API.

Pure stdlib (`unittest`), no third-party dependencies.

```bash
python3 -m unittest discover -s host/tests -v
```

These run automatically on every push/PR via `.github/workflows/ci.yml`,
alongside a firmware compile check.

When you change an adapter's status or title logic, add or update a fixture
here so the behaviour stays pinned — this is what guards against silent
breakage when an upstream app changes its on-disk format (the class of bug
behind the earlier `/Claude.app/` vs `/claude.app/` casing fix).
