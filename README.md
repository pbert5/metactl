# metactl

Operator CLI and HTTP client for `evolver-server`. It has no database, direct
EdgeStore access, SSH transport, or duplicated server domain logic.

The deployment index and application catalogs under `applications/` are the
CLI's packaged vocabulary and are loaded at runtime. The `api` section of the
server-owned eVOLVER catalog supplies the HTTP method/path contract; metactl
does not maintain a second route table.

Install it with `python -m pip install .` (or `uv pip install .`) and use
`metactl actions list` to inspect the available catalog. Human-oriented
grouped commands such as `metactl controllers show <id>` are presentation
aliases for stable catalog action IDs. `metactl interactive` offers the same
catalog in a prompt-driven shell; it is intended for a terminal and does not
persist credentials or state.
# API Workbench

Use `metactl api tui --repo .` to browse this checkout, or add `--live` to
compare it with the configured server. Install the `workbench` extra for Textual
and JSON Schema support. The umbrella Server Dev Container wrapper enables it.

`metactl api check`, `metactl api test --dry-run` and `meta-api-tui .` are also
available. The workbench supports explicit catalog composition, full live
discovery, OpenAPI import, fixture execution, response inspection, drift,
read-only polling, sanitized history/export, and local pytest evidence.

SAFE is the default. Writes require explicit session options and confirmation.
The full usage and safety contract is in the umbrella's `docs/api-workbench.md`.

