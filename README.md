# metactl

Operator CLI and HTTP client for `evolver-server`. It has no database, direct
EdgeStore access, SSH transport, or duplicated server domain logic.

Transport code is copied from the reference `tools/` layer; standalone
action-catalog packaging remains an explicit follow-up boundary.
