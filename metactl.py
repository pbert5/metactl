"""Compatibility shim for the package-owned :mod:`cli` implementation."""
from __future__ import annotations

try:
    from . import cli as _canonical
except ImportError:  # direct execution from the extracted checkout
    import cli as _canonical

__all__ = [name for name in dir(_canonical) if not name.startswith("__")]
globals().update({name: getattr(_canonical, name) for name in __all__})


if __name__ == "__main__":
    raise SystemExit(_canonical.main())
