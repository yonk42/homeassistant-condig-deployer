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

- **Coverage report card**: shows what is git-managed, what could be
  (with one-click migration of dashboards and UI helpers to YAML — see
  below), and what stays in Home Assistant by design.

Safety rails: applying is blocked while the working tree is dirty or the
local branch has diverged from the remote; pulls are fast-forward only.

## Install

1. Copy the `git_config_deployer/` folder into the `addons/` directory of
   your Home Assistant installation (e.g. via the Samba or SSH add-on, so it
   appears as a *local add-on*).
2. Settings → Add-ons → Add-on Store → ⋮ → *Check for updates*, then install
   **Git Config Deployer** from the Local add-ons section.
3. Start it. It appears in the sidebar as **Git Config**.

## First-time setup

The configuration directory does **not** need to be a git repository yet.
If it isn't (or has no remote / was never pushed), the panel shows a guided
setup instead of an error:

- **Initialize**: runs `git init`, writes a `.gitignore` that keeps
  `secrets.yaml`, `.storage/` (UI-managed settings), databases, logs and
  backups out of the repository, and commits the current configuration
  as-is. Nothing is converted or changed — UI-managed settings keep
  working and simply stay local.
- **Connect a remote**: enter the URL of an (ideally empty) repository;
  the add-on configures the remote and pushes the branch.
- **Deploy key**: for SSH remotes, the setup screen can generate an
  ed25519 key pair (stored in the add-on's private data volume). Add the
  shown public key to the remote repository with write access — on GitHub
  under *Settings → Deploy keys*. The generated key is used automatically
  whenever the `ssh_key` option is empty.

Every setup step is idempotent: if e.g. the push fails because the deploy
key isn't registered yet, fix that and press the button again. The setup
screen also shows the equivalent manual `git` commands if you prefer the
terminal.

## Moving UI-managed config to YAML

The goal of this add-on is a configuration that lives in git: clone the
repository on your computer, edit it (e.g. with Claude Code), commit, push —
then review and apply from this panel. The **Configuration coverage** card
on the main screen shows how far you are and offers one-click migrations
for the two things that commonly still live outside the repository:

### Dashboards → Lovelace YAML mode

UI-edited dashboards are stored as JSON in `.storage/lovelace*`. The
**Migrate dashboards to YAML** button:

1. creates a **full backup**,
2. exports the default dashboard to `ui-lovelace.yaml` and every additional
   dashboard to `dashboards/<url-path>.yaml` (dashboard resources / custom
   cards are carried over too),
3. adds `lovelace: !include lovelace.yaml` to `configuration.yaml` (the
   include file holds `mode: yaml` plus the dashboard list) — if a
   `lovelace:` section already exists, nothing is changed automatically and
   the UI shows what to merge,
4. runs the core config check (rolls everything back if it fails), commits,
   and optionally restarts Home Assistant to activate YAML mode.

Trade-off: in YAML mode the UI dashboard editor is disabled — dashboards
are edited in the repository from then on. The `.storage` originals are
left in place (Home Assistant ignores them in YAML mode), so switching
back is just removing the `lovelace:` section again.

### UI helpers → YAML includes

Helpers created under *Settings → Devices & services → Helpers*
(`input_boolean`, `input_number`, `input_select`, `input_text`,
`input_datetime`, `input_button`, `counter`, `timer`, `schedule`) are
stored in `.storage/<domain>`. The **Migrate helpers to YAML** button:

1. creates a **full backup**,
2. exports each domain to `helpers/<domain>.yaml`, keyed by the helper's
   object id — **entity IDs do not change**, so automations, scripts and
   history keep working,
3. adds `<domain>: !include helpers/<domain>.yaml` lines to
   `configuration.yaml` (domains that already exist there are skipped and
   reported for manual merging),
4. runs the config check (rolls back on failure), commits, moves the
   `.storage/<domain>` originals to the add-on's data volume
   (`/data/migration_backup/…`, kept as a safety net), and **restarts Home
   Assistant** — required, otherwise the helpers would exist twice.

Helpers created as *config entries* (template, derivative, threshold,
utility meter, …) have no YAML form and stay in Home Assistant.

### What never moves to git

Integration config entries, device pairings, entity/device registries,
areas, users and similar runtime state live in `.storage/` by design and
have no YAML representation. They are covered by Home Assistant backups —
the coverage card lists them so the boundary is explicit.

Both migrations are also documented step-by-step in the panel
(“Do it manually instead”) if you prefer the terminal.

## Options

| Option      | Default          | Notes                                              |
|-------------|------------------|----------------------------------------------------|
| `repo_path` | `/homeassistant` | Where the config repo is mounted inside the add-on. |
| `remote`    | `origin`         | Git remote to fetch from.                           |
| `branch`    | *(empty)*        | Empty = the currently checked-out branch.           |
| `ssh_key`   | *(empty)*        | Path to a private key for SSH remotes (see below).  |

### Remote authentication

- **SSH (recommended)**: generate a deploy key from the setup screen — it
  is used automatically while `ssh_key` is empty. Alternatively place your
  own key somewhere inside the config directory (e.g.
  `/homeassistant/.deploy_key`, mode 600, add it to `.gitignore`) and set
  `ssh_key` to that path. Host keys are accepted on first use and stored
  in the add-on's data volume.
- **HTTPS**: embed a token in the remote URL
  (`https://oauth2:<token>@git.example.com/...`).

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
