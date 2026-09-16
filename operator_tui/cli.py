"""Command entry point for the operator TUI."""
from __future__ import annotations

import argparse
import sys

from .app import OperatorTUI
try:
    from ..metactl_transport import configured_transport, resolve_operator_target
except ImportError:
    from metactl_transport import configured_transport, resolve_operator_target


def main(argv=None, *, transport=None, output=None) -> int:
    parser = argparse.ArgumentParser(prog="metactl tui", description="catalog-backed metactl operator TUI")
    parser.parse_args(argv or [])
    app = OperatorTUI(transport=transport or configured_transport(), target=resolve_operator_target())
    app.run()
    return 0


def tui_main() -> int:
    return main(sys.argv[1:])
