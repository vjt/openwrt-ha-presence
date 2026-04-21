"""Scaffolding for __main__._run tests. Full coverage lands in Task 4.1
once _run() is refactored for DI."""

from __future__ import annotations

from openwrt_presence import __main__ as eve_main


def test_main_module_imports():
    assert hasattr(eve_main, "_run")
    assert hasattr(eve_main, "main")
