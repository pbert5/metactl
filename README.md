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
