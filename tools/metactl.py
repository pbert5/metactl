"""Compatibility shim for the package-owned :mod:`cli` implementation."""
from __future__ import annotations

try:
    from .. import cli as _canonical
except ImportError:  # direct loading from the extracted checkout
    # Some release/layout checks load this compatibility path by filename
    # rather than importing the installed ``metactl`` package.  Bootstrap the
    # package namespace so the canonical module's relative imports still work.
    import importlib.util
    from pathlib import Path
    import sys
    import types

    package_root = Path(__file__).resolve().parents[1]
    package = sys.modules.get("metactl")
    if package is None or not hasattr(package, "__path__"):
        package = types.ModuleType("metactl")
        package.__path__ = [str(package_root)]
        sys.modules["metactl"] = package
    spec = importlib.util.spec_from_file_location("metactl.cli", package_root / "cli.py")
    if spec is None or spec.loader is None:
        raise ImportError("cannot load canonical metactl CLI")
    _canonical = importlib.util.module_from_spec(spec)
    sys.modules["metactl.cli"] = _canonical
    spec.loader.exec_module(_canonical)

__all__ = [name for name in dir(_canonical) if not name.startswith("__")]
globals().update({name: getattr(_canonical, name) for name in __all__})


if __name__ == "__main__":
    raise SystemExit(_canonical.main())
