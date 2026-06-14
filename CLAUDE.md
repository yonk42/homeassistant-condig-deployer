# homeassistant-config-deployer

A Home Assistant add-on that keeps your HA configuration in git. It watches a
remote branch for new commits, shows you a diff, and applies them via the ingress
UI — with an optional full backup first. It also converts UI-managed dashboards
and helpers to YAML files in the repository.

## Repository layout

```
repository.yaml               HA add-on repository manifest
git_config_deployer/
  config.yaml                 Add-on metadata (version, schema, ports)
  build.yaml                  Dockerfile build args
  Dockerfile                  Alpine image: Python 3 + py3-yaml + git + openssh
  README.md                   User-facing docs (install, setup, workflow)
  app/
    server.py                 Entry point: HTTP API + job runners (apply, setup)
    migrate.py                Coverage scan + dashboard/helper migration jobs
    index.html                Ingress UI (plain HTML + vanilla JS, no build step)
pyproject.toml                Ruff config (lint + format)
```

## Python tooling

**Ruff** is the linter and formatter for all Python code.

```bash
# Check for lint errors
ruff check .

# Auto-fix safe violations
ruff check --fix .

# Format
ruff format .

# Check formatting without writing (CI-style)
ruff format --check .
```

Both `ruff check .` and `ruff format --check .` must exit 0 before committing.
The configured rule sets are `E`, `F`, `W`, `B`, `ARG`, `BLE` (E501 is ignored —
`ruff format` enforces line length). There is no separate test runner; correctness
is verified with a simulated-environment script in `/tmp` during development.

## Development notes

- **No build step.** The add-on image is built by HA from the Dockerfile; locally
  you can `podman build` for a quick syntax check.
- **Supervisor dependency.** `server.py` calls Supervisor REST endpoints
  (`/backups/new/full`, `/core/api/config/core/check_config`, `/core/restart`).
  These only exist inside a real HA Supervised or HAOS installation.
- **Ingress-only.** The HTTP server only accepts connections from `172.30.32.2`
  (the Supervisor ingress gateway). Direct access returns HTTP 403.
- **`migrate.py` is glued to `server.py` via `migrate.init(server_module)`.** It
  reuses `git()`, `supervisor_call()`, `make_job()`, `set_step()`, and `log()`
  from the server rather than duplicating them.
- **Stdlib only in `server.py`.** `migrate.py` is the only file that imports
  PyYAML (`py3-yaml` in the Dockerfile).

## Intended workflow

1. Clone your HA config repo locally (or in a Claude Code session).
2. Edit YAML files — automations, scripts, scenes, dashboards (after migration),
   helpers (after migration).
3. Commit and push.
4. Open the add-on UI in HA → review the diff → Apply (creates a backup first).

The "Configuration coverage" card in the UI shows what is already git-managed,
what can be migrated (UI dashboards, UI helpers), and what stays in HA forever
(integrations, device registry, areas, users).
