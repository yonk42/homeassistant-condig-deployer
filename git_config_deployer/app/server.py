#!/usr/bin/env python3
"""Git Config Deployer — Home Assistant add-on backend.

Stdlib only. Serves the ingress UI and a small JSON API:

  GET  /                  -> UI
  GET  /api/status        -> fetch remote, report pending commits / files
  GET  /api/diff?path=... -> unified diff for one file (HEAD..remote)
  POST /api/apply         -> start apply job {"backup": bool}
  GET  /api/apply/status  -> poll the running/finished apply job

Apply pipeline: [backup via Supervisor] -> git merge --ff-only ->
core config check -> homeassistant.reload_all.
"""

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------- options

with open("/data/options.json", encoding="utf-8") as f:
    OPTIONS = json.load(f)

REPO = OPTIONS.get("repo_path") or "/homeassistant"
REMOTE = OPTIONS.get("remote") or "origin"
BRANCH_OPT = (OPTIONS.get("branch") or "").strip()
SSH_KEY = (OPTIONS.get("ssh_key") or "").strip()

SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
INGRESS_GATEWAY = "172.30.32.2"
PORT = 8099
STATE_FILE = "/data/state.json"
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- helpers


def log(msg):
    print(f"[git-config-deployer] {msg}", flush=True)


def git_env():
    env = dict(os.environ)
    env["HOME"] = "/data"  # writable home for git config / known_hosts
    if SSH_KEY:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile=/data/known_hosts"
        )
    else:
        env["GIT_SSH_COMMAND"] = (
            "ssh -o StrictHostKeyChecking=accept-new "
            "-o UserKnownHostsFile=/data/known_hosts"
        )
    return env


def git(*args, check=True, timeout=120):
    """Run a git command in the repo, return CompletedProcess."""
    proc = subprocess.run(
        ["git", "-C", REPO, *args],
        capture_output=True,
        text=True,
        env=git_env(),
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed "
            f"(rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


class GitError(Exception):
    pass


def ensure_safe_directory():
    """Repo files may be owned by a different uid than the add-on runs as."""
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", REPO],
        env=git_env(),
        capture_output=True,
    )


def current_branch():
    if BRANCH_OPT:
        return BRANCH_OPT
    proc = git("symbolic-ref", "--short", "HEAD", check=False)
    if proc.returncode != 0:
        raise GitError(
            "Could not determine the current branch (detached HEAD?). "
            "Set the 'branch' option in the add-on configuration."
        )
    return proc.stdout.strip()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def supervisor_call(method, path, payload=None, timeout=60):
    """Call the Supervisor API (also proxies the Core API under /core/api)."""
    req = urllib.request.Request(
        f"{SUPERVISOR}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc


# ---------------------------------------------------------------- status

US = "\x1f"  # unit separator for parsing git log output


def commit_info(ref):
    proc = git("log", "-1", f"--format=%h{US}%H{US}%an{US}%ad{US}%s",
               "--date=iso-strict", ref, check=False)
    if proc.returncode != 0:
        return None
    short, full, author, date, subject = proc.stdout.strip().split(US, 4)
    return {"short": short, "sha": full, "author": author,
            "date": date, "subject": subject}


def pending_commits(target):
    proc = git("log", f"--format=%h{US}%an{US}%ad{US}%s", "--date=iso-strict",
               f"HEAD..{target}")
    commits = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        short, author, date, subject = line.split(US, 3)
        commits.append({"short": short, "author": author,
                        "date": date, "subject": subject})
    return commits  # newest first


def changed_files(target):
    """Combine --name-status and --numstat for the HEAD..target range."""
    status = {}
    for line in git("diff", "--name-status", "-M", f"HEAD..{target}").stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].startswith("R"):
            status[parts[2]] = {"kind": "renamed", "from": parts[1]}
        elif len(parts) >= 2:
            kind = {"A": "added", "M": "modified", "D": "deleted"}.get(
                parts[0][:1], parts[0])
            status[parts[1]] = {"kind": kind}

    files = []
    for line in git("diff", "--numstat", "-M", f"HEAD..{target}").stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, rem, path = parts[0], parts[1], parts[2]
        # numstat rename syntax: "old => new" or {a => b} style; normalize
        meta = status.get(path) or next(
            (v for k, v in status.items() if k in path), {"kind": "modified"})
        files.append({
            "path": path,
            "kind": meta.get("kind", "modified"),
            "renamed_from": meta.get("from"),
            "additions": None if add == "-" else int(add),
            "deletions": None if rem == "-" else int(rem),
        })
    return files


def build_status(do_fetch=True):
    ensure_safe_directory()
    if not os.path.isdir(os.path.join(REPO, ".git")):
        return {"error": f"{REPO} is not a git repository."}

    branch = current_branch()
    target = f"{REMOTE}/{branch}"

    fetch_error = None
    if do_fetch:
        proc = git("fetch", REMOTE, "--prune", check=False, timeout=120)
        if proc.returncode != 0:
            fetch_error = proc.stderr.strip()

    if git("rev-parse", "--verify", target, check=False).returncode != 0:
        return {"error": f"Remote branch {target} not found. "
                         f"Check the 'remote' and 'branch' options.",
                "fetch_error": fetch_error}

    behind = int(git("rev-list", "--count", f"HEAD..{target}").stdout.strip())
    ahead = int(git("rev-list", "--count", f"{target}..HEAD").stdout.strip())
    dirty = [l for l in git("status", "--porcelain").stdout.splitlines() if l.strip()]

    state = load_state()
    status = {
        "repo": REPO,
        "branch": branch,
        "remote": REMOTE,
        "target": target,
        "head": commit_info("HEAD"),
        "remote_head": commit_info(target),
        "behind": behind,
        "ahead": ahead,
        "diverged": behind > 0 and ahead > 0,
        "dirty": bool(dirty),
        "dirty_files": [l[3:] for l in dirty][:20],
        "fetch_error": fetch_error,
        "last_apply": state.get("last_apply"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "commits": [],
        "files": [],
    }
    if behind > 0:
        status["commits"] = pending_commits(target)
        status["files"] = changed_files(target)
    return status


def file_diff(path, target):
    proc = git("diff", f"HEAD..{target}", "--", path, timeout=60)
    return proc.stdout


# ---------------------------------------------------------------- apply job

JOB_LOCK = threading.Lock()
JOB = None  # dict: {"running": bool, "steps": [...], "result": {...}}

STEP_BACKUP = "backup"
STEP_PULL = "pull"
STEP_CHECK = "check"
STEP_RELOAD = "reload"


def new_job(with_backup):
    steps = []
    if with_backup:
        steps.append({"id": STEP_BACKUP, "label": "Create full backup",
                      "status": "pending", "detail": ""})
    steps += [
        {"id": STEP_PULL, "label": "Pull changes", "status": "pending", "detail": ""},
        {"id": STEP_CHECK, "label": "Check configuration", "status": "pending", "detail": ""},
        {"id": STEP_RELOAD, "label": "Reload Home Assistant configuration",
         "status": "pending", "detail": ""},
    ]
    return {"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
            "steps": steps, "result": None}


def set_step(job, step_id, status, detail=None):
    for step in job["steps"]:
        if step["id"] == step_id:
            step["status"] = status
            if detail is not None:
                step["detail"] = detail


def run_apply(with_backup):
    global JOB
    job = JOB
    backup_ref = None
    try:
        branch = current_branch()
        target = f"{REMOTE}/{branch}"
        old = commit_info("HEAD")
        remote_head = commit_info(target)
        n_commits = int(git("rev-list", "--count", f"HEAD..{target}").stdout.strip())

        if n_commits == 0:
            raise RuntimeError("Nothing to apply — local config is up to date.")
        if int(git("rev-list", "--count", f"{target}..HEAD").stdout.strip()) > 0:
            raise RuntimeError(
                "Local branch has diverged from the remote. "
                "Resolve this in the repository before applying.")
        if git("status", "--porcelain").stdout.strip():
            raise RuntimeError(
                "Working tree has uncommitted changes. "
                "Commit or stash them before applying.")

        # 1. Backup ---------------------------------------------------
        if with_backup:
            set_step(job, STEP_BACKUP, "running")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            name = (f"Before git pull {old['short']}..{remote_head['short']} "
                    f"({n_commits} commit{'s' if n_commits != 1 else ''}, {stamp})")
            log(f"Creating full backup: {name}")
            resp = supervisor_call("POST", "/backups/new/full",
                                   {"name": name}, timeout=3600)
            slug = (resp.get("data") or {}).get("slug")
            if resp.get("result") != "ok" or not slug:
                raise RuntimeError(f"Backup failed: {resp}")
            backup_ref = {"slug": slug, "name": name}
            set_step(job, STEP_BACKUP, "done", f"{name} (slug {slug})")

        # 2. Pull (fast-forward only) ---------------------------------
        set_step(job, STEP_PULL, "running")
        git("merge", "--ff-only", target, timeout=120)
        new = commit_info("HEAD")
        set_step(job, STEP_PULL, "done",
                 f"{old['short']} → {new['short']} ({n_commits} commit"
                 f"{'s' if n_commits != 1 else ''})")
        log(f"Pulled {old['short']}..{new['short']}")

        # 3. Config check ---------------------------------------------
        set_step(job, STEP_CHECK, "running")
        check = supervisor_call("POST", "/core/api/config/core/check_config",
                                timeout=300)
        if check.get("result") != "valid":
            errors = check.get("errors") or "unknown error"
            set_step(job, STEP_CHECK, "error", str(errors))
            set_step(job, STEP_RELOAD, "skipped",
                     "Skipped because the configuration is invalid.")
            job["result"] = {
                "ok": False,
                "message": ("Changes were pulled, but the configuration is now "
                            "invalid and was NOT reloaded. Fix the configuration "
                            "and retry, or restore the backup. To roll back the "
                            f"files: git reset --hard {old['short']}"),
                "backup": backup_ref,
                "old": old, "new": new,
            }
            return
        set_step(job, STEP_CHECK, "done", "Configuration valid")

        # 4. Reload ----------------------------------------------------
        set_step(job, STEP_RELOAD, "running")
        supervisor_call("POST", "/core/api/services/homeassistant/reload_all",
                        timeout=300)
        set_step(job, STEP_RELOAD, "done",
                 "All reloadable YAML configuration reloaded")
        log("reload_all triggered")

        save_state({"last_apply": {
            "at": datetime.now(timezone.utc).isoformat(),
            "from": old["short"], "to": new["short"],
            "commits": n_commits,
            "backup": backup_ref,
        }})
        job["result"] = {
            "ok": True,
            "message": (f"Applied {n_commits} commit"
                        f"{'s' if n_commits != 1 else ''} "
                        f"({old['short']} → {new['short']}) and reloaded the "
                        "configuration."
                        + (f" Restore point: backup “{backup_ref['name']}”."
                           if backup_ref else "")),
            "backup": backup_ref,
            "old": old, "new": new,
        }
    except Exception as exc:  # surfaced to the UI
        log(f"Apply failed: {exc}")
        for step in job["steps"]:
            if step["status"] == "running":
                step["status"] = "error"
                step["detail"] = str(exc)
            elif step["status"] == "pending":
                step["status"] = "skipped"
        job["result"] = {"ok": False, "message": str(exc), "backup": backup_ref}
    finally:
        job["running"] = False


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "GitConfigDeployer/1.0"

    # -- plumbing -----------------------------------------------------
    def _authorized(self):
        # Only the Supervisor ingress gateway may talk to us.
        if self.client_address[0] != INGRESS_GATEWAY:
            self.send_error(403, "Ingress only")
            return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, relpath, ctype):
        try:
            with open(os.path.join(APP_DIR, relpath), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter logs
        pass

    # -- routes -------------------------------------------------------
    def do_GET(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                self._file("index.html", "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(build_status())
            elif path == "/api/diff":
                qs = parse_qs(url.query)
                fpath = (qs.get("path") or [""])[0]
                if not fpath or fpath.startswith("/") or ".." in fpath.split("/"):
                    self._json({"error": "invalid path"}, 400)
                    return
                target = f"{REMOTE}/{current_branch()}"
                self._json({"path": fpath, "diff": file_diff(fpath, target)})
            elif path == "/api/apply/status":
                with JOB_LOCK:
                    self._json(JOB or {"running": False, "steps": [],
                                       "result": None})
            else:
                self.send_error(404)
        except (GitError, RuntimeError) as exc:
            self._json({"error": str(exc)}, 500)
        except Exception as exc:  # noqa: BLE001
            log(f"Unhandled error on {path}: {exc}")
            self._json({"error": "internal error, see add-on log"}, 500)

    def do_POST(self):
        global JOB
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path != "/api/apply":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        with_backup = bool(payload.get("backup", True))
        with JOB_LOCK:
            if JOB and JOB.get("running"):
                self._json({"error": "An apply is already in progress."}, 409)
                return
            JOB = new_job(with_backup)
            threading.Thread(target=run_apply, args=(with_backup,),
                             daemon=True).start()
        self._json({"started": True})


def main():
    if not TOKEN:
        log("WARNING: SUPERVISOR_TOKEN is not set — backup/reload will fail.")
    ensure_safe_directory()
    log(f"Serving on :{PORT} — repo={REPO} remote={REMOTE} "
        f"branch={BRANCH_OPT or '(current)'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
