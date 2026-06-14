#!/usr/bin/env python3
"""Migrate UI-managed Home Assistant config (.storage) to YAML in the repo.

Used by server.py for three things:

  scan_coverage()                 -> data for the "Configuration coverage" card
  new_dashboards_job/run_migrate_dashboards -> storage dashboards -> YAML mode
  new_helpers_job/run_migrate_helpers       -> UI helpers -> YAML includes

The module is glued to the server via init(): it reuses the server's git(),
supervisor_call(), make_job()/set_step() and logging so behaviour matches
the apply pipeline (and tests can inject a stand-in).
"""

import json
import os
import re
import shutil
from datetime import datetime, timezone

import yaml

S = None  # the server module; set via init()


def init(server_module):
    global S
    S = server_module


# Storage-collection helpers that have an equivalent YAML schema where the
# storage item id doubles as the YAML object_id (entity ids stay identical).
HELPER_DOMAINS = [
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "counter",
    "timer",
    "schedule",
]

# Helpers created as config entries — these have no YAML equivalent and are
# only counted for the report card.
CONFIG_ENTRY_HELPERS = {
    "template",
    "derivative",
    "threshold",
    "integration",
    "utility_meter",
    "min_max",
    "statistics",
    "trend",
    "random",
    "history_stats",
    "switch_as_x",
    "group",
    "tod",
    "generic_thermostat",
    "generic_hygrostat",
}

GENERATED_HEADER = (
    "# Exported from the Home Assistant UI (.storage) by the Git Config "
    "Deployer add-on.\n"
    "# This file is the source of truth now — edit it in the repository.\n"
)

MIGRATION_BACKUP_DIR = "/data/migration_backup"


# ---------------------------------------------------------------- storage IO


def _storage_path(name):
    return os.path.join(S.REPO, ".storage", name)


def read_storage(name):
    try:
        with open(_storage_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def storage_items(name):
    data = read_storage(name) or {}
    return (data.get("data") or {}).get("items") or []


# ---------------------------------------------------------------- YAML utils


class _HALoader(yaml.SafeLoader):
    """SafeLoader that swallows HA-specific tags (!include, !secret, …)."""


def _stub_tag(loader, suffix, node):  # noqa: ARG001 — PyYAML callback shape
    return None


_HALoader.add_multi_constructor("!", _stub_tag)


def config_yaml_path():
    return os.path.join(S.REPO, "configuration.yaml")


def config_top_keys():
    """Top-level keys of configuration.yaml (regex + parse, best effort)."""
    try:
        with open(config_yaml_path(), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return set()
    keys = set(re.findall(r"(?m)^([A-Za-z_][A-Za-z0-9_]*)\s*:", text))
    try:
        parsed = yaml.load(text, Loader=_HALoader)
        if isinstance(parsed, dict):
            keys |= {str(k) for k in parsed}
    except yaml.YAMLError:
        pass
    return keys


def dump_yaml(data):
    return yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
    )


def write_new_file(path, content, created):
    """Write a generated file; identical re-runs pass, anything else refuses."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if f.read() == content:
                return  # re-run after a partial failure: already exported
        raise RuntimeError(
            f"Refusing to overwrite existing file "
            f"'{os.path.relpath(path, S.REPO)}' — move it aside and retry."
        )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    created.append(path)


def append_to_config(lines, comment):
    """Append top-level lines to configuration.yaml (idempotent)."""
    path = config_yaml_path()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    block = "".join(line + "\n" for line in lines)
    if block in text:
        return False
    prefix = "" if (not text or text.endswith("\n")) else "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{prefix}\n# {comment}\n{block}")
    return True


# ---------------------------------------------------------------- coverage


def scan_coverage():
    """Classify the configuration: git-managed / migratable / HA-only."""
    git_ready = os.path.isdir(os.path.join(S.REPO, ".git"))
    tracked = set()
    if git_ready:
        proc = S.git("ls-files", check=False)
        if proc.returncode == 0:
            tracked = set(proc.stdout.splitlines())
    keys = config_top_keys()

    git_part = {
        "tracked_yaml": sum(1 for p in tracked if p.endswith((".yaml", ".yml"))),
        "core_files": [
            {
                "file": fn,
                "exists": os.path.exists(os.path.join(S.REPO, fn)),
                "tracked": fn in tracked,
            }
            for fn in (
                "configuration.yaml",
                "automations.yaml",
                "scripts.yaml",
                "scenes.yaml",
            )
        ],
    }

    yaml_mode = "lovelace" in keys
    storage_dash = []
    if read_storage("lovelace") is not None:
        storage_dash.append(
            {"title": "Default dashboard (Overview)", "url_path": None, "default": True}
        )
    for item in storage_items("lovelace_dashboards"):
        if item.get("mode") == "storage":
            storage_dash.append(
                {
                    "title": item.get("title") or item.get("url_path"),
                    "url_path": item.get("url_path"),
                    "default": False,
                }
            )
    resources = len(storage_items("lovelace_resources"))
    dashboards = {
        "yaml_mode": yaml_mode,
        "storage": storage_dash,
        "resources": resources,
        "migratable": not yaml_mode and bool(storage_dash or resources),
    }

    domains = []
    for domain in HELPER_DOMAINS:
        count = len(storage_items(domain))
        in_yaml = domain in keys
        if count or in_yaml:
            domains.append({"domain": domain, "count": count, "in_yaml": in_yaml})
    helpers = {
        "domains": domains,
        "storage_total": sum(d["count"] for d in domains),
        "migratable": any(d["count"] and not d["in_yaml"] for d in domains),
        "blocked": [d["domain"] for d in domains if d["count"] and d["in_yaml"]],
    }

    entries = (
        (read_storage("core.config_entries") or {}).get("data", {}).get("entries", [])
    )
    fixed = {
        "config_entries": len(entries),
        "helper_entries": sum(
            1 for e in entries if e.get("domain") in CONFIG_ENTRY_HELPERS
        ),
        "devices": len(
            (read_storage("core.device_registry") or {})
            .get("data", {})
            .get("devices", [])
        ),
        "entities": len(
            (read_storage("core.entity_registry") or {})
            .get("data", {})
            .get("entities", [])
        ),
        "areas": len(
            (read_storage("core.area_registry") or {}).get("data", {}).get("areas", [])
        ),
        "users": sum(
            1
            for u in (read_storage("auth") or {}).get("data", {}).get("users", [])
            if not u.get("system_generated")
        ),
    }

    return {
        "git_ready": git_ready,
        "git": git_part,
        "dashboards": dashboards,
        "helpers": helpers,
        "fixed": fixed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------- job utils


def _require_ready_repo():
    if (
        not os.path.isdir(os.path.join(S.REPO, ".git"))
        or S.git("rev-parse", "--verify", "HEAD", check=False).returncode != 0
    ):
        raise RuntimeError(
            "The configuration repository is not set up yet — "
            "finish the git setup first."
        )
    if S.git("status", "--porcelain").stdout.strip():
        raise RuntimeError(
            "Working tree has uncommitted changes. Commit or stash them "
            "first so the migration can be committed (and rolled back) "
            "cleanly."
        )


def _backup_step(job, label):
    S.set_step(job, "backup", "running")
    name = f"{label} ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
    S.log(f"Creating full backup: {name}")
    resp = S.supervisor_call("POST", "/backups/new/full", {"name": name}, timeout=3600)
    slug = (resp.get("data") or {}).get("slug")
    if resp.get("result") != "ok" or not slug:
        raise RuntimeError(f"Backup failed: {resp}")
    S.set_step(job, "backup", "done", f"{name} (slug {slug})")
    return {"slug": slug, "name": name}


def _check_step(job):
    """Run the core config check; return None if valid, error string if not."""
    S.set_step(job, "check", "running")
    check = S.supervisor_call("POST", "/core/api/config/core/check_config", timeout=300)
    if check.get("result") != "valid":
        return str(check.get("errors") or "unknown error")
    S.set_step(job, "check", "done", "Configuration valid")
    return None


def _commit_paths(paths, message):
    rel = [os.path.relpath(p, S.REPO) if os.path.isabs(p) else p for p in paths]
    if not rel:
        return False
    S.git("add", "--", *rel)
    if S.git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    S.git("commit", "-m", message, timeout=120)
    return True


def _rollback_files(created):
    for path in created:
        try:
            os.remove(path)
        except OSError:
            pass
    for sub in ("dashboards", "helpers"):
        try:
            os.rmdir(os.path.join(S.REPO, sub))
        except OSError:
            pass  # not empty / never created
    S.git("checkout", "--", "configuration.yaml", check=False)


def _skip_pending(job, note):
    for step in job["steps"]:
        if step["status"] == "pending":
            step["status"] = "skipped"
            step["detail"] = note


def _fail_job(job, exc, backup_ref):
    S.log(f"Migration failed: {exc}")
    for step in job["steps"]:
        if step["status"] == "running":
            step["status"] = "error"
            step["detail"] = str(exc)
        elif step["status"] == "pending":
            step["status"] = "skipped"
    job["result"] = {"ok": False, "message": str(exc), "backup": backup_ref}


def _restart_step(job):
    S.set_step(job, "restart", "running")
    S.supervisor_call("POST", "/core/restart", timeout=600)
    S.set_step(
        job,
        "restart",
        "done",
        "Home Assistant is restarting — reload this page in a minute",
    )


# ---------------------------------------------------------------- dashboards


def new_dashboards_job(restart):
    steps = [
        ("backup", "Create full backup"),
        ("export", "Export dashboards to YAML files"),
        ("configure", "Switch Lovelace to YAML mode in configuration.yaml"),
        ("check", "Check configuration"),
        ("commit", "Commit to git"),
    ]
    if restart:
        steps.append(("restart", "Restart Home Assistant"))
    return S.make_job("migrate_dashboards", steps)


def _export_dashboards(job, created):
    """Write ui-lovelace.yaml, dashboards/*.yaml and lovelace.yaml."""
    default = read_storage("lovelace")
    if default is not None:
        cfg = (default.get("data") or {}).get("config") or {}
    else:
        # The default dashboard was never edited: keep it auto-generated.
        cfg = {"strategy": {"type": "original-states"}}
    write_new_file(
        os.path.join(S.REPO, "ui-lovelace.yaml"),
        GENERATED_HEADER + dump_yaml(cfg),
        created,
    )
    n_dash = 1

    dash_cfg = {}
    for item in storage_items("lovelace_dashboards"):
        if item.get("mode") != "storage":
            continue
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", item.get("url_path") or item["id"])
        stored = read_storage(f"lovelace.{item['id']}")
        cfg = ((stored or {}).get("data") or {}).get("config") or {
            "strategy": {"type": "original-states"}
        }
        rel = f"dashboards/{slug}.yaml"
        write_new_file(
            os.path.join(S.REPO, rel), GENERATED_HEADER + dump_yaml(cfg), created
        )
        n_dash += 1
        entry = {
            "mode": "yaml",
            "title": item.get("title") or slug,
            "filename": rel,
            "show_in_sidebar": bool(item.get("show_in_sidebar", True)),
            "require_admin": bool(item.get("require_admin", False)),
        }
        if item.get("icon"):
            entry["icon"] = item["icon"]
        dash_cfg[item.get("url_path") or slug] = entry

    lovelace = {"mode": "yaml"}
    resources = [
        {"url": r["url"], "type": r.get("type") or r.get("res_type") or "module"}
        for r in storage_items("lovelace_resources")
        if r.get("url")
    ]
    if resources:
        lovelace["resources"] = resources
    if dash_cfg:
        lovelace["dashboards"] = dash_cfg
    write_new_file(
        os.path.join(S.REPO, "lovelace.yaml"),
        GENERATED_HEADER + dump_yaml(lovelace),
        created,
    )

    S.set_step(
        job,
        "export",
        "done",
        f"{n_dash} dashboard(s) + lovelace.yaml"
        + (f", {len(resources)} resource(s)" if resources else ""),
    )
    return n_dash


def run_migrate_dashboards(job, restart):
    created = []
    backup_ref = None
    try:
        _require_ready_repo()
        cov = scan_coverage()["dashboards"]
        if cov["yaml_mode"] and not (cov["storage"] or cov["resources"]):
            raise RuntimeError(
                "Dashboards are already in YAML mode — nothing to migrate."
            )
        if not (cov["storage"] or cov["resources"]):
            raise RuntimeError(
                "No UI-managed dashboards or resources found — nothing to migrate."
            )

        backup_ref = _backup_step(job, "Before dashboard YAML migration")

        S.set_step(job, "export", "running")
        _export_dashboards(job, created)

        # configuration.yaml -----------------------------------------
        S.set_step(job, "configure", "running")
        snippet = "lovelace: !include lovelace.yaml"
        if "lovelace" in config_top_keys():
            # Conflict: keep the export (committed), let the user merge.
            _commit_paths(
                created, "Export UI dashboards to YAML (manual lovelace merge required)"
            )
            S.set_step(
                job,
                "configure",
                "error",
                "configuration.yaml already has a 'lovelace:' "
                "section — merge manually (see below).",
            )
            _skip_pending(job, "Waiting for the manual merge.")
            job["result"] = {
                "ok": False,
                "message": (
                    "The dashboards were exported and committed, but "
                    "configuration.yaml already contains a 'lovelace:' section, "
                    "so it was not changed automatically. Merge the contents of "
                    "lovelace.yaml into that section (or replace it with "
                    f"'{snippet}'), commit, and restart Home Assistant."
                ),
                "backup": backup_ref,
            }
            return
        append_to_config(
            [snippet], "Dashboards exported from the UI — now managed as YAML in git"
        )
        S.set_step(job, "configure", "done", snippet)

        # check -------------------------------------------------------
        errors = _check_step(job)
        if errors is not None:
            _rollback_files(created)
            S.set_step(job, "check", "error", errors)
            _skip_pending(job, "Rolled back — nothing was committed.")
            job["result"] = {
                "ok": False,
                "message": (
                    "The exported configuration did not pass the config check; "
                    "all changes were rolled back (dashboards are unchanged). "
                    f"Error: {errors}"
                ),
                "backup": backup_ref,
            }
            return

        # commit --------------------------------------------------------
        S.set_step(job, "commit", "running")
        _commit_paths(
            created + ["configuration.yaml"], "Migrate dashboards to Lovelace YAML mode"
        )
        head = S.commit_info("HEAD")
        S.set_step(
            job,
            "commit",
            "done",
            f"{head['short']} — {len(created)} file(s) + configuration.yaml",
        )

        if restart:
            _restart_step(job)

        job["result"] = {
            "ok": True,
            "message": (
                "Dashboards are now YAML files in the repository — edit them "
                "via git from now on (the UI dashboard editor is disabled in "
                "YAML mode). "
                + (
                    "Home Assistant is restarting to activate YAML mode; reload "
                    "this page in a minute."
                    if restart
                    else "Restart Home Assistant to activate YAML mode."
                )
                + (
                    f" Restore point: backup “{backup_ref['name']}”."
                    if backup_ref
                    else ""
                )
            ),
            "backup": backup_ref,
        }
        S.log("Dashboard migration finished")
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI
        _fail_job(job, exc, backup_ref)
    finally:
        job["running"] = False


# ---------------------------------------------------------------- helpers


def new_helpers_job():
    return S.make_job(
        "migrate_helpers",
        [
            ("backup", "Create full backup"),
            ("export", "Export helpers to YAML files"),
            ("configure", "Reference helper files in configuration.yaml"),
            ("check", "Check configuration"),
            ("commit", "Commit to git"),
            ("storage", "Remove migrated helpers from UI storage"),
            ("restart", "Restart Home Assistant"),
        ],
    )


def run_migrate_helpers(job):
    created = []
    backup_ref = None
    try:
        _require_ready_repo()
        keys = config_top_keys()
        domains, skipped, total = [], [], 0
        for domain in HELPER_DOMAINS:
            items = storage_items(domain)
            if not items:
                continue
            if domain in keys:
                skipped.append(domain)
                continue
            domains.append((domain, items))
            total += len(items)
        if not domains:
            raise RuntimeError(
                "No migratable UI helpers found."
                + (
                    f" Skipped domains already present in configuration.yaml:"
                    f" {', '.join(skipped)} — merge those manually."
                    if skipped
                    else ""
                )
            )

        backup_ref = _backup_step(job, "Before helper YAML migration")

        # export ------------------------------------------------------
        S.set_step(job, "export", "running")
        for domain, items in domains:
            data = {}
            for item in items:
                data[item["id"]] = {
                    k: v for k, v in item.items() if k != "id" and v is not None
                }
            write_new_file(
                os.path.join(S.REPO, f"helpers/{domain}.yaml"),
                GENERATED_HEADER + dump_yaml(data),
                created,
            )
        S.set_step(
            job,
            "export",
            "done",
            f"{total} helper(s): " + ", ".join(f"{d} ×{len(i)}" for d, i in domains),
        )

        # configuration.yaml -------------------------------------------
        S.set_step(job, "configure", "running")
        lines = [f"{d}: !include helpers/{d}.yaml" for d, _ in domains]
        append_to_config(
            lines, "Helpers exported from the UI — now managed as YAML in git"
        )
        S.set_step(job, "configure", "done", "; ".join(lines))

        # check ---------------------------------------------------------
        errors = _check_step(job)
        if errors is not None:
            _rollback_files(created)
            S.set_step(job, "check", "error", errors)
            _skip_pending(job, "Rolled back — nothing was changed.")
            job["result"] = {
                "ok": False,
                "message": (
                    "The exported helpers did not pass the config check; all "
                    "changes were rolled back (helpers are unchanged). "
                    f"Error: {errors}"
                ),
                "backup": backup_ref,
            }
            return

        # commit --------------------------------------------------------
        S.set_step(job, "commit", "running")
        _commit_paths(created + ["configuration.yaml"], "Migrate UI helpers to YAML")
        head = S.commit_info("HEAD")
        S.set_step(
            job,
            "commit",
            "done",
            f"{head['short']} — {len(created)} file(s) + configuration.yaml",
        )

        # remove from storage (originals are kept in /data) --------------
        S.set_step(job, "storage", "running")
        bdir = os.path.join(
            MIGRATION_BACKUP_DIR, datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        os.makedirs(bdir, exist_ok=True)
        moved = 0
        for domain, _ in domains:
            src = _storage_path(domain)
            if os.path.exists(src):
                shutil.move(src, os.path.join(bdir, domain + ".json"))
                moved += 1
        S.set_step(
            job,
            "storage",
            "done",
            f"Moved {moved} storage file(s) to {bdir} (kept as a safety net)",
        )

        # restart (mandatory: HA must drop the old in-memory helpers) ----
        _restart_step(job)

        job["result"] = {
            "ok": True,
            "message": (
                f"Migrated {total} helper(s) "
                f"({', '.join(d for d, _ in domains)}) to YAML files under "
                "helpers/ and committed them. Entity IDs are unchanged, so "
                "automations and history keep working. Home Assistant is "
                "restarting to load them — reload this page in a minute. "
                "From now on these helpers are edited via git, not the UI."
                + (
                    f" Skipped (already defined in configuration.yaml): "
                    f"{', '.join(skipped)} — merge those manually."
                    if skipped
                    else ""
                )
                + (
                    f" Restore point: backup “{backup_ref['name']}”."
                    if backup_ref
                    else ""
                )
            ),
            "backup": backup_ref,
        }
        S.log("Helper migration finished")
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI
        _fail_job(job, exc, backup_ref)
    finally:
        job["running"] = False
