"""Scaffolding for __main__._run tests. Full coverage lands in Task 4.1
once _run() is refactored for DI."""

from __future__ import annotations

import inspect

from openwrt_presence import __main__ as eve_main


def test_main_module_imports():
    assert hasattr(eve_main, "_run")
    assert hasattr(eve_main, "main")


def test_run_uses_get_running_loop():
    """Guard against asyncio.get_event_loop() regression (H8)."""
    src = inspect.getsource(eve_main._run)
    assert "get_running_loop" in src
    assert "get_event_loop()" not in src
