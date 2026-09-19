"""Deterministically wrap a server export as a non-authoritative client snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from operator_contract import ContractError, _validate_contract


def build_snapshot(contract: Mapping[str, Any], *, source: str, source_revision: str) -> dict[str, Any]:
    validated = _validate_contract(contract, "server export")
    if not source or not source_revision:
        raise ContractError("snapshot provenance requires source and source revision")
    return {
        "snapshot": "metactl.operator-contract.v1",
        "provenance": {"generator": "metactl.contract_snapshot", "source": source, "source_revision": source_revision},
        "contract": validated,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="generate a non-authoritative metactl contract snapshot")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", default="evolver_server.control.contract")
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args(argv)
    document = json.loads(args.input.read_text(encoding="utf-8"))
    snapshot = build_snapshot(document, source=args.source, source_revision=args.source_revision)
    args.output.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
