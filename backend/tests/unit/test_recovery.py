"""The recovery command's wiring.

The command itself only reads pending runs, decides between reaping and resuming, and
disposes its engine. What is worth pinning here is the decision it makes from its
arguments and the guarantee that a connection is never leaked -- the database work it
does is covered against a real server in ``tests/integration``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app import recovery
from app.config import get_settings


def _engine(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    engine = MagicMock()
    monkeypatch.setattr(recovery, "create_engine_for_url", lambda url: engine)
    return engine


def test_main_reaps_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no flags the command resolves abandoned runs rather than re-running them."""
    captured: dict[str, object] = {}

    def fake_reap(engine, settings, *, older_than, dry_run) -> int:  # type: ignore[no-untyped-def]
        captured["older_than"] = older_than
        captured["dry_run"] = dry_run
        return 0

    monkeypatch.setattr(recovery, "_reap", fake_reap)
    monkeypatch.setattr(recovery, "_resume", lambda *a, **k: 0)
    engine = _engine(monkeypatch)

    assert recovery.main([]) == 0

    assert engine.dispose.called
    assert captured["dry_run"] is False
    assert isinstance(captured["older_than"], datetime)
    assert captured["older_than"].tzinfo is timezone.utc


def test_ttl_hours_overrides_the_configured_window(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_reap(engine, settings, *, older_than, dry_run) -> int:  # type: ignore[no-untyped-def]
        captured["older_than"] = older_than
        return 0

    monkeypatch.setattr(recovery, "_reap", fake_reap)
    monkeypatch.setattr(recovery, "_resume", lambda *a, **k: 0)
    _engine(monkeypatch)
    before = datetime.now(timezone.utc)

    recovery.main(["--ttl-hours", "5", "--dry-run"])

    delta = before - captured["older_than"]  # type: ignore[operator]
    # Five hours back, allowing for the clock ticking between the two reads.
    assert abs(delta - timedelta(hours=5)) < timedelta(seconds=5)


def test_resume_mode_passes_the_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_resume(engine, settings, *, prompt_version, dry_run) -> int:  # type: ignore[no-untyped-def]
        captured["prompt_version"] = prompt_version
        captured["dry_run"] = dry_run
        return 0

    monkeypatch.setattr(recovery, "_reap", lambda *a, **k: 0)
    monkeypatch.setattr(recovery, "_resume", fake_resume)
    _engine(monkeypatch)

    recovery.main(["--resume", "--prompt-version", "prompt-x", "--dry-run"])

    assert captured == {"prompt_version": "prompt-x", "dry_run": True}


def test_resume_defaults_to_the_configured_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(recovery, "_reap", lambda *a, **k: 0)
    monkeypatch.setattr(
        recovery,
        "_resume",
        lambda engine, settings, *, prompt_version, dry_run: captured.update(
            prompt_version=prompt_version
        ),
    )
    _engine(monkeypatch)

    recovery.main(["--resume"])

    assert captured["prompt_version"] == get_settings().prompt_version


def test_the_engine_is_disposed_even_when_recovery_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked pooled connection would outlive the process's one job."""

    def boom(*a, **k) -> int:  # type: ignore[no-untyped-def]
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(recovery, "_reap", boom)
    monkeypatch.setattr(recovery, "_resume", lambda *a, **k: 0)
    engine = _engine(monkeypatch)

    with pytest.raises(RuntimeError, match="unreachable"):
        recovery.main([])

    assert engine.dispose.called
