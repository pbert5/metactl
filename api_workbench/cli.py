"""CLI entry points; non-TUI checks remain usable without Textual."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from .adapters import repository_registry, live_registry, openapi_registry, read_document, audit_routes
from .model import WorkbenchError, compare
from .execution import HTTPClient, FixtureClient, Session, Policy
from .evidence import resolve_tests, run_tests, command_for


def main(argv=None, *, output=None):
    parser = argparse.ArgumentParser(prog="metactl api")
    parser.add_argument("command", choices=["tui", "check", "test"], nargs="?", default="tui")
    parser.add_argument("operation", nargs="?")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--server")
    parser.add_argument("--live", action="store_true", help="use metactl's configured central URL and authentication")
    parser.add_argument("--openapi", help="bundled OpenAPI 3.0/3.1 JSON or YAML file or URL")
    parser.add_argument("--fixture", type=Path, help="offline response fixture file; never opens HTTP")
    parser.add_argument("--app")
    parser.add_argument("--audit", action="store_true", help="best-effort static decorator route audit")
    parser.add_argument("--allow-mutations", action="store_true")
    parser.add_argument("--allow-hardware", action="store_true", help="requires --allow-mutations; every write still requires action-ID confirmation")
    parser.add_argument("--trust-tests", action="store_true", help="explicitly authorize local repository pytest execution")
    parser.add_argument("--dry-run", action="store_true", help="resolve and print test commands without executing")
    args = parser.parse_args(argv)
    out = output or sys.stdout
    try:
        if args.allow_hardware and not args.allow_mutations:
            raise WorkbenchError("--allow-hardware requires --allow-mutations")
        if args.fixture and (args.server or args.live or (args.openapi and args.openapi.startswith(("http://", "https://")))):
            raise WorkbenchError("fixture mode cannot use network discovery or execution")
        if args.openapi and (args.repo or args.live):
            raise WorkbenchError("choose OpenAPI import or repository/live catalog discovery")
        try:
            from ..metactl_transport import configured_transport
        except ImportError:
            from metactl_transport import configured_transport
        configured = configured_transport()
        server = args.server or (configured.base_url if args.live else None)
        # Reuse credentials only for the configured target. External APIs can
        # receive explicit headers in the request editor.
        headers = configured.headers if server and server.rstrip("/") == configured.base_url else {}
        client = HTTPClient(server, headers, configured.timeout) if server else None
        repo = args.repo
        local, live, drift = None, None, None
        if args.openapi:
            if args.openapi.startswith(("http://", "https://")):
                parsed = urlsplit(args.openapi)
                fetcher = HTTPClient(f"{parsed.scheme}://{parsed.netloc}")
                status, _, raw = fetcher.read(parsed.path + ("?" + parsed.query if parsed.query else ""))
                if status != 200:
                    raise WorkbenchError(f"OpenAPI fetch returned HTTP {status}")
                import yaml
                document = yaml.safe_load(raw)
            else:
                document = read_document(Path(args.openapi))
            registry = openapi_registry(document, args.openapi)
        else:
            if repo is not None or not server:
                repo = repo or Path.cwd()
                local = repository_registry(repo)
            if server:
                live = live_registry(client)
            registry = local or live
            if local is not None and live is not None:
                drift = compare(local, live)
                # Keep local evidence as the executable authority. Show live-only
                # endpoints without importing remote test commands.
                for identifier, endpoint in live.endpoints.items():
                    if identifier not in registry.endpoints:
                        registry.add(endpoint)
        if args.fixture:
            fixtures = read_document(args.fixture)
            client = FixtureClient(fixtures["responses"])
        if live and live is not registry:
            registry.diagnostics.extend(live.diagnostics)
        audit = audit_routes((repo or Path.cwd()).resolve(), registry) if args.audit else None
        if args.command == "check":
            out.write(json.dumps({"endpoints": [e.contract() for e in registry.select(application=args.app)],
                                  "drift": drift, "diagnostics": registry.diagnostics + (live.diagnostics if live and live is not registry else []), "audit": audit}, indent=2) + "\n")
            return 1 if drift or registry.diagnostics or (live and live.diagnostics) or (audit and any(r["undeclared"] for r in audit["routes"])) else 0
        if args.command == "test":
            endpoints = registry.select(application=args.app)
            if args.operation:
                endpoints = [e for e in endpoints if e.id == args.operation]
            if not endpoints:
                raise WorkbenchError("no matching endpoint")
            targets = []
            for endpoint in endpoints:
                if endpoint.evidence.get("tests"):
                    targets.extend(resolve_tests(endpoint))
            if not targets:
                raise WorkbenchError("no evidence tests declared for selection")
            targets = list(dict.fromkeys(targets))
            if args.dry_run:
                out.write(json.dumps([{"cwd": str(t.root), "argv": command_for(t)} for t in targets], indent=2) + "\n")
                return 0
            results = run_tests(targets, trusted=args.trust_tests, emit=out.write)
            out.write(json.dumps(results, indent=2) + "\n")
            return 0 if results and all(r["passed"] for r in results) else 1
        if args.app:
            registry.endpoints = {e.id: e for e in registry.select(application=args.app)}
        try:
            from .app import WorkbenchApp
        except ImportError as exc:
            raise WorkbenchError("TUI dependencies unavailable; install metactl[workbench] in the Server Dev Container") from exc
        blocked = [d["id"] for d in (drift or []) if d["kind"] in {"changed", "declared_only"}]
        WorkbenchApp(registry, Session(client, Policy(args.allow_mutations, args.allow_hardware), blocked=blocked), drift, audit).run()
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"api-workbench: {exc}", file=sys.stderr)
        return 2


def tui_main():
    arguments = sys.argv[1:]
    if arguments and not arguments[0].startswith("-"):
        arguments = ["--repo", arguments[0], *arguments[1:]]
    return main(["tui", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
