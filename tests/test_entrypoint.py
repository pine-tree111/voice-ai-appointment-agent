"""Packaging and console scripts entrypoint smoke test.

Verifies that all console scripts defined in pyproject.toml under [project.scripts]
resolve to callable functions without import crashes or missing dependencies.
"""

import importlib
import tomllib
from pathlib import Path


def test_project_scripts_entrypoints_resolve() -> None:
    """Verify all [project.scripts] resolve without import crashes."""
    pyproject_path = Path(__file__).parents[1] / "pyproject.toml"
    assert pyproject_path.is_file(), f"pyproject.toml not found at {pyproject_path}"

    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    scripts = pyproject.get("project", {}).get("scripts", {})
    assert len(scripts) > 0, "No console scripts defined in pyproject.toml"

    expected_scripts = {"voice-agent", "simulate-call", "sync-assistant"}
    assert expected_scripts.issubset(scripts.keys()), (
        f"Missing expected scripts in pyproject.toml. Found: {list(scripts.keys())}, "
        f"expected: {expected_scripts}"
    )

    for script_name, target in scripts.items():
        assert ":" in target, (
            f"Entrypoint '{script_name}' target '{target}' lacks ':' delimiter"
        )
        module_path, func_name = target.split(":", 1)
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name, None)
        assert func is not None, (
            f"Entrypoint '{script_name}' targets missing attribute '{target}'"
        )
        assert callable(func), f"Entrypoint '{target}' is not callable"
