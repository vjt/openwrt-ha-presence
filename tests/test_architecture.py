"""AST-based rules preventing structural regression.

Uses AST parsing, not string matching — so comments and docstrings
can't accidentally trigger (or silence) a rule."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src" / "openwrt_presence"


def _all_modules() -> list[tuple[str, ast.Module]]:
    modules = []
    for p in _SRC.rglob("*.py"):
        tree = ast.parse(p.read_text(), filename=str(p))
        modules.append((str(p.relative_to(_SRC)), tree))
    return modules


def test_engine_does_not_import_io_modules():
    """engine.py must stay pure — no paho, no aiohttp, no mqtt."""
    engine = (_SRC / "engine.py").read_text()
    tree = ast.parse(engine)
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    for imp in imports:
        if isinstance(imp, ast.ImportFrom):
            mod = imp.module or ""
            assert not mod.startswith("paho"), f"engine imports {mod}"
            assert "aiohttp" not in mod, f"engine imports {mod}"
            assert "openwrt_presence.mqtt" not in mod
            assert "openwrt_presence.sources" not in mod


def test_sources_do_not_import_engine():
    """Sources must depend on domain, not engine (reverse-layering guard)."""
    for fname in (_SRC / "sources").glob("*.py"):
        tree = ast.parse(fname.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod != "openwrt_presence.engine", (
                    f"{fname.name} imports from engine"
                )


def test_no_datetime_now_in_engine():
    """Engine receives `now` injected — never calls datetime.now() itself."""
    engine = (_SRC / "engine.py").read_text()
    tree = ast.parse(engine)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Match datetime.now() or datetime.datetime.now()
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "now":
                pytest.fail(f"engine.py calls .now() at line {node.lineno}")


_BOUNDARY = {"config.py"}  # where Any is tolerated (YAML parse)


def test_no_any_in_public_src_signatures():
    """`dict[str, Any]` is boundary-only (Config.from_dict); Any absent elsewhere."""
    for rel, tree in _all_modules():
        if rel in _BOUNDARY:
            continue
        for node in ast.walk(tree):
            # Detect a bare `: Any` or `-> Any` in function signatures
            if isinstance(node, ast.FunctionDef):
                if node.name.startswith("_"):
                    continue  # private
                annots = []
                for a in node.args.args:
                    if a.annotation is not None:
                        annots.append(a.annotation)
                if node.returns is not None:
                    annots.append(node.returns)
                for annot in annots:
                    if _is_any(annot):
                        pytest.fail(
                            f"{rel}:{node.lineno} {node.name}: Any in public signature"
                        )


def _is_any(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "Any":
        return True
    return isinstance(node, ast.Attribute) and node.attr == "Any"


_FROZEN_REQUIRED = {
    "domain.py": {"StationReading", "HomeState", "AwayState", "PersonState"},
    "config.py": {"Config", "NodeConfig", "PersonConfig", "MqttConfig"},
}


def test_frozen_dataclasses_for_value_objects():
    """StationReading, StateChange, HomeState, AwayState, PersonState, Config
    must all be frozen dataclasses."""
    for fname, classes in _FROZEN_REQUIRED.items():
        tree = ast.parse((_SRC / fname).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in classes:
                # Must have @dataclass(frozen=True)
                frozen = False
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if (
                                kw.arg == "frozen"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is True
                            ):
                                frozen = True
                assert frozen, f"{fname}::{node.name} must be @dataclass(frozen=True)"
