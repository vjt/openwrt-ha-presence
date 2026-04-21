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


def test_initial_query_failure_does_not_crash():
    """If first query raises, _run continues into the poll loop (H9).

    Full DI-based E2E test lands in Task 4.1. This is a source-inspection
    guard until then.
    """
    src = inspect.getsource(eve_main._run)
    assert src.count("initial_query_failed") >= 1


def test_on_connect_wrapped_in_try_except():
    """Guard against on_connected raising and being swallowed by paho (H7)."""
    src = inspect.getsource(eve_main._run)
    assert "on_connected_failed" in src


def test_paho_logger_enabled():
    """Guard that paho's internal logger is wired so errors surface (H7)."""
    src = inspect.getsource(eve_main._run)
    assert "enable_logger" in src


def test_on_connect_schedules_via_call_soon_threadsafe():
    """on_connected must run on asyncio loop, not paho thread (C2).

    publisher._last_state is mutated by publish_state() on the asyncio
    loop — if paho's on_connect thread calls on_connected() directly it
    races. The hop via loop.call_soon_threadsafe serialises back.
    """
    src = inspect.getsource(eve_main._run)
    assert "call_soon_threadsafe" in src
