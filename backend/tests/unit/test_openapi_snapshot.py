"""The committed OpenAPI snapshot must match what the app actually serves.

``frontend/src/test/contract.test.ts`` checks the TypeScript types against this
snapshot. That check is only worth anything if the snapshot is true, so this test is
the half that keeps it honest: it regenerates the snapshot in memory and fails if the
committed file differs by a single byte.

Together the two make the frontend/backend contract a build failure rather than
something a reviewer notices in the UI.
"""

from __future__ import annotations

import json

from scripts.export_openapi import SNAPSHOT_PATH, render


def test_snapshot_file_exists() -> None:
    assert SNAPSHOT_PATH.is_file(), (
        f"{SNAPSHOT_PATH} is missing. Run `python -m scripts.export_openapi` from backend/."
    )


def test_snapshot_is_current() -> None:
    committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
    expected = render()

    if committed != expected:
        try:
            was = json.dumps(json.loads(committed), indent=2, sort_keys=True) + "\n"
            now = json.dumps(json.loads(expected), indent=2, sort_keys=True) + "\n"
        except json.JSONDecodeError:
            was, now = committed, expected

        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                was.splitlines(), now.splitlines(), "committed", "regenerated", lineterm="", n=1
            )
        )
        raise AssertionError(
            "frontend/src/api/openapi.snapshot.json is stale -- a mirrored response "
            "schema changed. Run `python -m scripts.export_openapi` from backend/ and "
            f"commit the result.\n\n{diff}"
        )


def test_snapshot_covers_the_types_under_contract() -> None:
    """Every mirrored backend schema must appear, and map to a distinct TS type."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    types = snapshot["typescript_types"]

    assert set(types) == set(snapshot["schemas"]), "a mirrored schema is missing from 'schemas'"
    assert len(set(types.values())) == len(types), "two schemas map to one TypeScript type"
    assert "Capabilities" not in types.values(), (
        "/capabilities is unmodelled by the backend; see scripts/export_openapi.py"
    )
