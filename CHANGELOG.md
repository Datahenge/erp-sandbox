# Changelog

Timestamped changes to `erp-sandbox` itself. It was called `devstack` until
the rename (decision #52), and entries from before it use that name. Rarely
the file you want: current behaviour is in [docs/](docs/), and what has been
decided, and why, is in [docs/decisions.md](docs/decisions.md).

Dates are when the work landed on `main`.

## Unreleased

Nothing yet.

## 0.7.0 — 2026-11-04

### Added
- **`[frappe].watch`**: `false` leaves `bench watch` out of the Procfile, for
  environments where nobody edits JS or CSS and the watcher's memory is better
  spent elsewhere. Absent means `true`; existing environments render unchanged.
  Run `erp-sandbox render`, then `erp-sandbox restart`, after changing it.

## 0.6.0 — 2026-11-04

**After upgrading, run `erp-sandbox render` in each existing environment**,
then `erp-sandbox build --from image`. Three build args were renamed, and
until `.env` is re-rendered, `erp-sandbox up` stops with
"unset-in-.env-run-erp-sandbox-render". `build` re-renders by itself.

### Added
- **`[bench].env_file`: runtime secrets for the apps** (decision #61). A file
  loaded into the bench container only, for credentials apps read from their
  environment. preflight checks it is private and holds no build secret;
  `doctor` lists which names arrived. `erp-sandbox init` now writes
  `.erp-sandbox/bench.sample.env`; an existing environment can copy it from
  the package's `assets/`.
- **`erp-sandbox restore <dump>`**, with `--with-public-files` and
  `--with-private-files`: the site step's restore, with the dump named on the
  command line (decision #62).
- **`erp-sandbox backup [--with-files]`**: `bench backup` for the site, listing
  the files it wrote.
- **`erp-sandbox up --recreate`**, which applies a changed `env_file`. Given a
  service, it recreates that service only.

### Security
- **No secret on a command line any more** (decision #62). `seed`, the site
  step, `restore`, `erp-sandbox mariadb` and doctor's grant check passed the
  MariaDB root password, the admin password or the encryption key as
  arguments, where `ps` shows them to every user on the host. They now travel
  by stdin and environment.

### Changed
- **doctor's disk check follows decision #60** too: 10 GB once built.
- **`file` is in the bench image for every series**, not only v16 (decision
  #59). Picked up with `erp-sandbox render` then `erp-sandbox build --only image`.
- **preflight passes on a built, running environment.** A port held by the
  environment's own container is no longer a collision, and once `bench/env`
  exists, low disk space warns below 10 GB rather than failing below 20 GB
  (decision #60).

### Fixed
- **`erp-sandbox init` printed a traceback** when it could not create or write
  the directory. It now names the path and the reason in one line.
- **A v16 dump's frappe version read as "not recorded"**, so the downgrade
  warning (decision #39) never fired for v16. v16 added columns after
  `git_branch` in `tabInstalled Application`.
- **doctor flagged the upstream frappe and erpnext repo URLs** as client
  values in a committed file, and warned that `bench/env`'s interpreter
  resolved on a host that has its own Python at the same path.
- **The v16 image build failed** with "python3 is 3.14, but this Frappe series
  needs 3.14.10", after installing Debian's Python 3.11 beside the image's
  3.14. The official python base image sets `ENV PYTHON_VERSION`, which
  overrode the build arg of the same name; the arg is now
  `SERIES_PYTHON_VERSION`. `NODE_VERSION` and `YARN_VERSION`, which the
  official node images set, are now `STACK_NODE_VERSION` and
  `STACK_YARN_VERSION`. Picked up with `erp-sandbox render` then
  `erp-sandbox build --from image`.
- **doctor: every app reported missing on bench 5.31**, whose `bench version`
  prints four fields per app.
- **doctor: socket.io warned on a working v16 site.** The probe now tries
  Engine.IO 4, then 3, and shows the server's response when neither answers.
- **doctor: a restore's host-specific grant always warned.** A row with the
  same password and privileges as `'%'` is now `ok`; one that differs still
  warns.
- **The agent briefing's web URL ignored `[ports].bind`**, naming
  `localhost` where nothing listens. Picked up with `erp-sandbox render`.
- **doctor and preflight** now also run git without the repository variables
  a git hook passes on.
- **`git commit -a` in a checkout of erp-sandbox failed the pre-commit hook**,
  because selftest's scratch repositories inherited the commit's
  `GIT_INDEX_FILE` and worked on its index. Every git the tool starts, `fetch`
  included, now drops git's repository variables. The hook also runs selftest
  unbuffered, so a traceback appears in the lines it prints.

## 0.5.3 — 2026-11-03

### Fixed
- **`bench restore` failed in a v16 environment** with `file: not found` and
  a `CommandFailedError`: frappe v16 runs the system `file` command on every
  backup, and the slim base image does not include it. The v16 image now installs
  `file`. An existing v16 environment picks it up with `erp-sandbox render`
  then `erp-sandbox build --only image`.

## 0.5.2 — 2026-11-03

### Fixed
- **MariaDB 11.8 ran with `innodb_snapshot_isolation` on**, its default since
  11.6, under which concurrent Frappe background jobs fail with "Record has
  changed since last read". erp-sandbox's MariaDB config now turns it off,
  as `loose-innodb_snapshot_isolation = OFF` so that MariaDB 10.4, which has
  no such setting, still starts. An existing environment picks it up with
  `erp-sandbox render` and `erp-sandbox restart mariadb`.

## 0.5.1 — 2026-11-03

### Fixed
- **A build could leave `yarn.lock` modified** in an app whose lockfile was
  out of step with its `package.json`, and `erp-sandbox fetch` then refused
  to update that app. The bench step now runs `yarn install --pure-lockfile`,
  which never writes the lockfile. To recover an app already affected:
  `git -C bench/apps/<app> checkout -- yarn.lock`.

## 0.5.0 — 2026-11-02

### Added
- **`[site].db_name`** names the site's MariaDB database, which frappe also
  makes the database user. Optional; unset, frappe still picks a random name.
  It applies when the site is created, in every seed mode, and
  `erp-sandbox doctor` warns if it no longer matches the site.

## 0.4.2 — 2026-11-02

### Fixed
- **MariaDB 11.8 never became healthy**, so a v16 build stopped at the site
  step. MariaDB 11 images no longer ship `mysqladmin` or `mysql`. The
  healthcheck and doctor's grant check now use `mariadb-admin` and `mariadb`,
  falling back to the old names for older images. In the bench container,
  where every supported Debian release has the new names, they are used
  directly.
- **erpnext v16 assets failed to build** on `Could not resolve "onscan.js"`.
  `yarn install` ran only in frappe. It now runs in every app that has a
  `package.json`, as `bench setup requirements --node` does.
- **A failed `bench build` let the build carry on.** `bench build` can print
  `Build failed` and still exit 0. The build step now checks the log as well
  as the exit status, stops on either, and keeps the full log in
  `bench/logs/erp-sandbox-bench-build.log`.

## 0.4.1 — 2026-11-02

### Fixed
- **A hatchling app still failed to install**, now with `No module named
  'editables'`. hatchling asks for `editables` only through its build-time
  hook, which pip does not call under `--no-build-isolation`. erp-sandbox now
  asks each app's backend for its editable-build requirements and installs
  them too.

### Changed
- **The docs install from PyPI** — `uv tool install erp-sandbox`, or `sudo pipx
  install --global erp-sandbox` on a shared VPS — instead of the git URL.
  Upgrades use `uv tool upgrade --refresh` or `pipx upgrade
  --pip-args='--no-cache-dir'`; without those, a cached package index can miss
  a release published minutes earlier. `uv tool install --force erp-sandbox`
  moves an existing git install over.

## 0.4.0 — 2026-11-02

### Added
- **Preflight checks that every image tag exists on Docker Hub** (decision
  #58). A missing tag is refused before the image build; offline it warns.
  `--skip-images` turns it off.

### Changed
- **`erp-sandbox build --from <step>` re-renders first** (#58), so a fix made
  in `config.toml` reaches the resumed build.

### Fixed
- **A pyproject app with a build backend other than frappe's failed to
  install** — "Cannot import build backend 'hatchling.build'". Each app's own
  `[build-system].requires` is now installed before its build.
- **BuildKit's `InvalidDefaultArgInFrom` warning** no longer appears on every
  image build.

## 0.3.0 — 2026-11-02

### Added
- **Frappe/ERPNext v16** (decisions #55–#57). `erp-sandbox init --frappe 16`
  writes a v16 toolchain: `python:3.14-slim-bookworm`, Node 24.13.0,
  wkhtmltopdf `0.12.6.1-3`, bench 5.29.0, MariaDB 11.8. **Not yet built
  end to end** — see `docs/testing.md`.
- **Preflight checks the toolchain against the series**: Node major, bench
  minimum, and whether the wkhtmltopdf release ships a `.deb` for the base
  image's Debian release. It warns when frappe/erpnext refs or the MariaDB
  version do not match the series.

### Changed
- **`[stack].python` is gone** (#57). The base image decides it; a config that
  still has the line is refused with the fix named — delete it.
- **`[frappe].gunicorn_workers` is no longer rendered.** Nothing in a
  development bench reads it; an existing config may keep it.
- **`[internal_ports].mariadb` and `.redis` no longer count as collisions**
  with the bench's ports: they listen in containers of their own.
- **On a shared VPS, the admin installs erp-sandbox once for every user**
  with `sudo pipx install --global` (decision #54). `docs/vps.md` has the step;
  laptops still install per-user.
- **Renamed `erp-sandbox`** (decision #52), replacing `erpnext-devstack`. Frappe's
  trademark policy requires permission for a product name containing ERPNext,
  and PyPI already has an unrelated `devstack`.
  - Command: `erp-sandbox`, with **`sandbox`** as a short alias.
  - Install: `uv tool install git+https://github.com/Datahenge/erp-sandbox.git`
    — the repository is now public, so no SSH key or access is needed.
    Uninstall the old tool first: `uv tool uninstall erpnext-devstack`.
  - New environments go under `~/erp-sandboxes/`.
  - The registry is `/etc/erp-sandbox/registry.toml`; Caddy snippets go in
    `/etc/caddy/erp-sandbox.d/`.
- **Existing environments must be migrated by hand** — a clean break. In each:
  1. `mv .devstack .erp-sandbox`
  2. In `config.toml`, change `[devstack]` to `[erp-sandbox]`.
  3. `erp-sandbox render`. The generated briefing moves to
     `.claude/rules/erp-sandbox-environment.md`; delete the old
     `devstack-environment.md` beside it.

  Both the "not an environment" refusal and preflight name these steps when
  they find the old names.

## 0.2.0 — 2026-11-02

Shared-VPS hosting (decisions #40–#51). This section also holds the changes
released as 0.1.1 and 0.1.2, which were never given sections of their own.

### Fixed
- **A restore no longer dies on a backup `bench backup` produced** (decision
  #39). frappe's `is_downgrade()` locates the `tabInstalled Application` INSERT
  by scanning the dump line by line and `eval()`s whatever follows `VALUES` on
  that same line. Every current MariaDB dump tool writes the rows on the lines
  *after* it, so frappe evaluated an empty string and the restore ended in
  `SyntaxError: unexpected EOF while parsing` before touching the database —
  systematically, for any v12 backup taken on a current Debian or Ubuntu.
  `bench restore` is now called with `--force`, which in this frappe skips that
  check and nothing else, and devstack compares the two frappe versions itself:
  the site step prints `frappe: dump <a> -> bench <b>` and warns when the dump
  is the newer. The note in `seed_db.py` that blamed `--force` for the
  `DROP USER` churn was wrong — that happens on every restore regardless, and
  `prune_stale_grants` already handles it.

### Added
- **Shared-VPS hosting** (decisions #40–#51), alongside laptops. Several
  developers' environments on one VPS, each served over HTTPS by one shared
  Caddy at its own subdomain. See [docs/vps.md](docs/vps.md).
  - `[site].public_url` (optional, `https://<hostname>`) renders as Frappe's
    `host_name`, so emails, webhooks and OAuth callbacks carry the public
    address.
  - `devstack proxy-snippet` prints the Caddyfile block for the environment,
    for the VPS admin to install.
  - An admin-owned registry, `/etc/devstack/registry.toml`: preflight refuses
    a port or subdomain assigned to another Linux user.
  - nginx passes a proxy's `X-Forwarded-Proto` through and builds `Origin`
    from it, so URLs and socketio callbacks say `https` behind Caddy.
  - Preflight refuses rootless Docker, and warns when lingering is off under
    rootless podman for an environment with a `public_url`.

- **`[stack].pip_constraints` in `config.toml`** (decision #36). The pip
  ceilings applied while installing the apps are now the environment's, not the
  tool's. `devstack init` seeds the list from the Frappe series template in
  `known_versions.py`, and that is the last time the template is read — keep it,
  edit it, or empty it, and the build does what your `config.toml` says.
  Previously the v12 cap `numpy<1.24` was applied from Python to every v12
  environment with no way to answer back, which is fine for a vanilla checkout
  and unbuildable for a fork pinning `numpy==1.26.4`: pip reported only
  `No matching distribution found`, naming neither the constraint nor devstack.
  That failure is now annotated with the entry that caused it. An **absent**
  key means no constraints and is warned about by `preflight`, `doctor` and the
  bench step — including on `devstack build --from bench`, which skips
  preflight; an explicit `[]` is a choice and warns about nothing.

- **`devstack render` generates `.claude/settings.json`**, a Claude Code hook
  that refuses an Edit or Write to any generated file (decision #35). Until
  now "generated files are never hand-edited" was prose, and `render --check`
  found the violation afterwards. The refusal names the remedy — change
  `config.toml` and re-render — so an agent is corrected rather than merely
  blocked. It binds Claude Code only; you and your editor are unaffected. It
  covers files, not commands, and it fails open on every error path, so a
  `config.toml` that will not parse leaves every file writable. New verb:
  `devstack guard-edit`, which is what the hook runs.
- **`devstack render` generates a VSCode workspace file**, `<name>.code-workspace`
  (decision #34). Open it with *File > Open Workspace from File* rather than
  opening the folder. It sets the analysis paths so `import frappe` resolves in
  the editor — one entry per app plus the container's `site-packages`, in both
  the Pylance and Jedi spellings — stops VSCode auto-activating `bench/env` in
  new terminals, and keeps the database directory out of the file watcher.
  With `[debug].enabled` it also carries the **debugpy attach configuration**,
  so the `launch.json` that previously had to be copied out of the README by
  hand, with its path mapping transcribed correctly, is generated instead.
  No interpreter is named, deliberately: none on the host can import frappe.
  `.vscode/` is left entirely to you and devstack never writes it.
- **An environment now briefs Claude Code about itself.** `devstack init`
  seeds a `CLAUDE.md` at the environment root — yours, written once and never
  rewritten — and `devstack render` generates
  `.claude/rules/devstack-environment.md` beside it, carrying the site, ports,
  apps, container name, the four verbs that matter, and the handful of things
  that waste an afternoon. Both load automatically, including from a session
  started inside `bench/apps/<app>/`. The generated file is overwritten every
  render, so upgrading the tool upgrades the briefing in every environment;
  your notes in `CLAUDE.md` are untouched.
- **`[debug]` in `config.toml` — opt-in remote debugging with `debugpy`**
  (decision #29). Set `enabled = true`, re-render, restart, and attach an
  editor to the published port for real breakpoints in the running site.
  Off by default, and a disabled section reaches no generated file. Enabling it
  publishes a third host port, bound to `127.0.0.1`.
  - Turning it on **disables the web server's auto-reload**, unavoidably:
    werkzeug's reloader re-executes the server and drops the debugger.
    Use `devstack restart bench` to pick up a code change while debugging.
  - `target = "web"` is the only value. Workers fork a work horse per job and
    debugpy does not follow the fork, so `preflight` refuses any other target
    rather than accept one that silently never breaks.
  - `debugpy` is now installed into `bench/env` by `devstack build-bench`, so
    the flag is a render-and-restart rather than a rebuild.
  - `devstack doctor` grows a **Debug** group when the flag is on.
- **`blobless` in `[[apps]]` — clone an app without its file history**
  (decision #31). `blobless = true` adds `--filter=blob:none`, so commits,
  trees and branches arrive in full and the contents of files at old commits
  are fetched on demand. Measured on `version-12`: frappe and erpnext carry
  1.4 GiB of objects between them, about 70 MiB of which is everything but old
  file contents. Off unless set.
  - `git log` and `git bisect` are unaffected; `git blame` on an old file
    reaches the network, and cannot answer without it.
  - Refused for an app with `requires_credentials = true` — those later fetches
    happen long after the token is gone.
  - Applies at clone time. Setting it does not shrink a repository already in
    `bench/apps/`; `devstack doctor` now reports each app's clone kind and the
    command that changes it, either way.
- `devstack mariadb` accepts arguments and runs a query: `devstack mariadb -e "select 1"`.
- `devstack doctor` runs `pip check` inside `bench/env` and reports dependency
  conflicts between apps (decision #30 — reported, never resolved).
- `[devstack] min_version` in `config.toml`; `preflight` refuses an environment
  that demands a newer tool than the one installed.
- Pre-commit hook (`tools/pre-commit`, `tools/install-hooks.sh`): `compileall`,
  `selftest`, and a staged-secret check, wired through `core.hooksPath`.
- `selftest` guards: `preflight`'s re-exports stay importable, and a source
  checkout has its git hooks installed.

### Changed
- **`devstack init acme-dev` now creates `~/erpnext-envs/acme-dev/`** (#51),
  not `./acme-dev`. Give a path (`./acme-dev`) for the old behaviour.
  Existing environments need no move.
- **`devstack init` refuses a directory that already has files in it**,
  unless `--here` is given. It used to scaffold into it.
- **nginx's port is published on `127.0.0.1` by default** (#46), set by the new
  `[ports].bind`. After upgrading and re-rendering, an existing environment is
  no longer reachable from other machines on the network; set
  `bind = "0.0.0.0"` to restore that.
- **Every service restarts after a reboot** (#47): `restart: unless-stopped`
  under Docker, `restart: always` under podman, whose `podman-restart.service`
  starts nothing else. Rootless podman also needs lingering.
- **New environments clone frappe and erpnext `blobless`** (#31, revised).
  Existing clones are not converted.

- **`devstack --version` now prints the package path** beside the number:
  `devstack 0.1.1 (/home/you/erpnext-devstack/devstack)`. An editable install
  and a copied one report the same version and behave differently, and the path
  is the only thing that separates them. `docs/contributing.md` step 3 uses it;
  the check it replaces, `python3 -c "import devstack"`, could not answer the
  question — a `uv tool`/`pipx` venv is not on the system interpreter's path,
  and run from inside the clone it passed whether or not anything was installed.
- **The version is declared once**, in `devstack/__init__.py`. `pyproject.toml`
  reads it with `dynamic = ["version"]` rather than repeating the literal, which
  had already drifted once. `selftest` refuses a literal in `[project]`.
- **`docs/developing.md` is now `docs/contributing.md`.** Sitting in a folder
  beside `setup.md` and `using.md`, the old name read as "how to develop in an
  environment" — which is what most readers are doing — rather than "how to
  work on devstack itself".
- **The documentation is split by how often you read it.** `README.md` is now a
  111-line front door — what it is, what you get, the two roots, and an index.
  The first-run path moved to [docs/setup.md](docs/setup.md), numbered end to
  end with time estimates; day-to-day work, editor setup, debugging and
  teardown moved to [docs/using.md](docs/using.md).
  - **Three things nothing documented before:** how to log in after the build
    (`Administrator`, and the `ADMIN_PASSWORD` from `.devstack/secrets.env`);
    how to remove an environment and reclaim its disk; and how to run several
    environments side by side.
  - The quickstart now says the repository is private and names the access
    needed, so a teammate does not stall on line 1.
  - Acceptance item 16 is now "onboarding from the documentation alone" rather
    than "from the README alone" — the README delegates.
- **README restructured for a reader in a hurry.** Troubleshooting moved to
  [docs/troubleshooting.md](docs/troubleshooting.md) as symptom → cause → fix;
  developing the tool moved to [docs/contributing.md](docs/contributing.md) as
  numbered setup steps. The README keeps the path from install to a running
  site, gains an **Editor setup** section, and drops from 426 lines to 350.
  Corrected while reviewing: the build time (11 minutes measured, not the
  "20-40 min" the README and `functional.md` both claimed), the `devstack init`
  file tree (it omitted the seeded `CLAUDE.md`), and CLAUDE.md's opening claim
  that a developer clones this repository to use the tool — they install it.
- Host Python floor lowered from 3.12 to 3.11 (decision #18) — `tomllib`
  arrived in 3.11, so the higher floor was stricter than its own justification
  and blocked `pipx` installs for no gain.
- `devstack mariadb` execs the `mysql` client directly instead of going through
  `bench --site X mariadb`, which is an `os.execv` with a hardcoded argv and can
  never take an argument. The password now travels in `MYSQL_PWD` rather than
  on the command line.
- `ruff` configured and the tree made clean. It is **not** a dependency of this
  package: run `uvx ruff check devstack/`.
- Documentation split into current-facts and lessons (see `docs/README.md`).

### Fixed
- **`data/mariadb` is created by devstack**, in the `infra` step and in
  `devstack up`, instead of being left to the container engine. The engines
  disagree about a missing bind-mount source: rootless podman creates it as the
  invoking user, a rootful Docker daemon creates it as `root`. MariaDB runs as
  the host uid (decision #25), so on Docker it could not write its own datadir
  and exited, taking every later check with it. `technical.md` had described
  this as already happening; it had not been. `selftest` now parses the compose
  file and fails if any bind-mount source has nothing to create it.
- **`devstack doctor`'s chown remedy matches the engine.** It printed
  `podman unshare chown …` unconditionally — the wrong command under Docker,
  where the ids are the host's real ones and podman may not be installed.
- **`devstack render --list` did not exist.** The flag was implemented in
  `render.py`'s own argparse but never forwarded by `cmd_render`, so the CLI
  exited with `unrecognized arguments` — while four documents and the generated
  agent briefing told you to run it. Now plumbed through, and `selftest` checks
  that both `--list` and `--check` reach the renderer, because a flag declared
  on a module's parser is not reachable until the CLI forwards it.
- **The `.gitignore` that `devstack init` seeds did not cover `/images/`**, so
  a developer who versioned their environment directory would have committed
  two render outputs and watched them drift from `config.toml`. `selftest` now
  asks `git check-ignore` about every generated file rather than trusting the
  list, and separately asserts that the seeded `CLAUDE.md` is *not* ignored.
  Existing environments keep the `.gitignore` they were created with — `init`
  writes it once — so add `/images/` by hand there.
- `images/bench/` was tracked in git while being generated output. Now
  gitignored and untracked; the canonical copy is `devstack/assets/images/bench/`.
- The bench image build context is materialized into the environment, so a
  fresh `devstack init` can build. `selftest` and `doctor` both check it now.
- vanilla frappe v12 needs `libpq-dev`; recorded per-series in
  `known_versions.py` rather than in the image.

## 0.1.0 — 2026-08-23

First working tool. `devstack init` to a serving ERPNext v12 site in about
eleven minutes, unattended: eight build steps, four containers, all source
bind-mounted and editable on the host.
