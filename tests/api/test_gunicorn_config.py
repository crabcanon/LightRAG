"""Regression tests for Gunicorn worker logging initialization."""

from __future__ import annotations

import pytest

from lightrag.api import gunicorn_config


def test_post_fork_keeps_uvicorn_errors_visible_and_snapshots_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[tuple[str, str, bool]] = []

    def fake_setup_logger(
        name: str,
        level: str,
        *,
        add_filter: bool,
        log_file_path: str,
    ) -> None:
        del log_file_path
        configured.append((name, level, add_filter))
        # Reproduce the mutation that made iterating loggerDict directly unsafe.
        gunicorn_config.logging.root.manager.loggerDict[f"created.{name}"] = object()

    monkeypatch.setattr(gunicorn_config, "setup_logger", fake_setup_logger)
    monkeypatch.setattr(gunicorn_config, "loglevel", "warning")
    monkeypatch.setattr(
        gunicorn_config.logging.root.manager,
        "loggerDict",
        {"lightrag.catalog": object(), "unrelated": object()},
    )

    gunicorn_config.post_fork(None, None)

    assert ("uvicorn.error", "WARNING", False) in configured
    assert ("lightrag.catalog", "WARNING", True) in configured
    assert not any(name == "unrelated" for name, _level, _filter in configured)


def test_post_fork_reports_and_reraises_logger_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_setup(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("logger setup failed")

    monkeypatch.setattr(gunicorn_config, "setup_logger", fail_setup)

    with pytest.raises(RuntimeError, match="logger setup failed"):
        gunicorn_config.post_fork(None, None)

    assert "Gunicorn post_fork initialization failed" in capsys.readouterr().err
