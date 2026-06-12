# Git Config Deployer

A Home Assistant add-on that bridges a git-managed configuration and the
running instance: review pending commits from the sidebar, create a full
backup, fast-forward pull, validate, and reload — without restarting
Home Assistant.

## What it does

- **Fetches** the configured remote when opened and shows incoming commits,
  changed files, and per-file diffs (expandable).
- **Up to date?** Shows the current HEAD, the last apply through this add-on,
  and when the remote was last checked.
- **Apply** runs: optional **full backup** (named
  `Before git pull <old>..<new> (<n> commits, <timestamp>)`) via the
  Supervisor → `git merge --ff-only` → **core config check** →
  `homeassistant.reload_all`. Each step is reported live; if the config check
  fails, the reload is skipped and the UI tells you how to roll back.

Safety rails: applying is blocked while the working tree is dirty or the
local branch has diverged from the remote; pulls are fast-forward only.

## Install

1. Copy the `git_config_deployer/` folder into the `addons/` directory of
   your Home Assistant installation (e.g. via the Samba or SSH add-on, so it
   appears as a *local add-on*).
2. Settings → Add-ons → Add-on Store → ⋮ → *Check for updates*, then install
   **Git Config Deployer** from the Local add-ons section.
3. Start it. It appears in the sidebar as **Git Config**.

Your configuration directory must already be a git repository with the
remote configured.

## Options

| Option      | Default          | Notes                                              |
|-------------|------------------|----------------------------------------------------|
| `repo_path` | `/homeassistant` | Where the config repo is mounted inside the add-on. |
| `remote`    | `origin`         | Git remote to fetch from.                           |
| `branch`    | *(empty)*        | Empty = the currently checked-out branch.           |
| `ssh_key`   | *(empty)*        | Path to a private key for SSH remotes (see below).  |

### Remote authentication

- **HTTPS**: embed a read token in the remote URL
  (`https://oauth2:<token>@git.example.com/...`).
- **SSH**: place a deploy key somewhere inside the config directory
  (e.g. `/homeassistant/.deploy_key`, mode 600, and add it to `.gitignore`)
  and set `ssh_key` to that path. Host keys are accepted on first use and
  stored in the add-on's data volume.

### Older Supervisor versions

This add-on uses the `homeassistant_config` mapping (config at
`/homeassistant`). On older Supervisor versions, change the `map` entry in
`config.yaml` to `config:rw` and set `repo_path: /config`.

## Notes

- `homeassistant.reload_all` reloads everything Home Assistant can reload at
  runtime (automations, scripts, templates, most YAML platforms). Newly added
  integrations or changes to non-reloadable components still need a restart —
  the add-on deliberately never restarts Core for you.
- The backup is a normal full backup; restore it from
  Settings → System → Backups if anything goes wrong. The UI also prints the
  `git reset --hard <old-sha>` needed to roll back just the files.
- The web UI is only reachable through Home Assistant ingress (requests from
  anywhere but the ingress gateway are rejected), so it inherits your normal
  HA authentication.
