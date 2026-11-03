# Using an environment

Day-to-day work in a built environment. First run is in [setup.md](setup.md).

---

## Commands

```sh
erp-sandbox bench console          # the frappe shell, site already initialised
erp-sandbox bench migrate          # after pulling app changes
erp-sandbox bench execute path.to.function
erp-sandbox bench clear-cache

erp-sandbox up / down / restart    # the stack, or one service
erp-sandbox logs -f                # what honcho's processes are printing
erp-sandbox shell                  # a shell in the bench container
erp-sandbox mariadb                # a mysql client on the site database
erp-sandbox mariadb -e "select 1"  # ...or a scripted query
erp-sandbox ps
erp-sandbox doctor
```

- `erp-sandbox --help` lists every verb.
- Everything after `erp-sandbox bench` is passed to `bench` untouched, including
  flags that collide with ours — `erp-sandbox bench --version` reports bench's
  version, `erp-sandbox --version` reports the tool's.
- Every verb except `init` acts on the environment containing your working
  directory. Use `--project DIR` to act on one from outside.

## Three things that catch people out

- **`erp-sandbox restart`, never `bench restart`.** The latter is a *production*
  command that talks to supervisor, which this environment does not run.
- **Restarting is rarely needed.** The web server reloads on save and `bench
  watch` rebuilds assets. Restart only after changing `site_config.json` or the
  Procfile, or installing an app. (The reloader is werkzeug's, not
  `developer_mode` — turning that off will not stop it. Enabling `[debug]`
  will.)
- **Do not activate `bench/env` from the host.** It is built inside the
  container, so its interpreter and shebangs will not run on your machine. It
  is mounted so you can *read* it. `erp-sandbox doctor` warns if this looks wrong.

## Restoring a SQL dump

A dump is restored by the **site step**, not by hand. It drops the site's
database, recreates it, streams the dump in, installs the encryption key and
runs `bench migrate`.

1. Put the dump on your own disk, named `*.sql` or `*.sql.gz`. Frappe decides
   whether to decompress from the suffix alone.
2. In `config.toml`:

   ```toml
   [database]
   seed           = "sql_dump"       # "frappe_backup" for a bench tarball
   dump_path      = "~/dumps/acme-2026-08-24.sql.gz"
   encryption_key = "env:ENCRYPTION_KEY"
   ```

3. ```sh
   erp-sandbox seed --force
   ```

   A first build runs the same step; `--force` is only needed once a site
   exists.

- **`--force` drops the current site's database.** It is the one destructive
  command in the tool, and it re-runs cleanly — a bad restore is one
  `erp-sandbox seed --force` from another attempt.
- **`encryption_key` must be the one the dump was taken with.** The step
  refuses to run without a key, but a *wrong* key is never reported: the site
  loads, logins work, and every stored password in it is unreadable.
- **Take the dump without `--databases` and `--all-databases`.** Those add
  `CREATE DATABASE` and `USE`, which point the import at the source server's
  database name rather than this site's.
- The dump is mounted read-only, so your only copy cannot be written to. A
  `.sql.gz` is decompressed inside the container — allow its uncompressed size
  again in free space.
- Before restoring, erp-sandbox prints `frappe: dump <a> -> bench <b>` and warns if
  the dump is the newer of the two. It does not stop — `bench migrate` has no
  patches to undo what a newer frappe already did, so the branch in
  `config.toml` is worth checking before you trust the result.

To load SQL into the site database *without* rebuilding the site — one table, a
patch, a partial export — use the client directly:

```sh
erp-sandbox mariadb < some.sql
gunzip -c some.sql.gz | erp-sandbox mariadb
```

That is a plain `mysql` session on the site's own database. It does not write
`site_config.json` and does not run `bench migrate`, so it is not a way to
restore a whole site.

## Resetting the Administrator password

```sh
erp-sandbox bench set-admin-password '<new-password>'
```

- **A restored site keeps the dump's users and passwords.** `ADMIN_PASSWORD`
  from `config.toml` is applied only when Frappe first creates `Administrator`
  — which a restore skips, because the dump already has one. After
  `seed = "sql_dump"` or `"frappe_backup"`, log in with the dump's own
  Administrator password, or set your own with the command above.
- Editing `[site].admin_password` and re-rendering changes **nothing** about an
  existing site. Only the seed step reads that value, and only for a site it
  creates.
- Add `--logout-all-sessions` to invalidate sessions that are already open.
- The environment has one site and it is the default, so `--site` is optional;
  `erp-sandbox bench --site <name> set-admin-password ...` works too.

## Editor setup

`erp-sandbox render` writes `<name>.code-workspace` at the environment root.

1. Open it with **File > Open Workspace from File** — not the folder. It pins
   the workspace root, which the debugger's path mapping resolves against.
2. On VSCodium, set `python.languageServer` to `Jedi`. Pylance is
   Marketplace-only; erp-sandbox writes the search paths for both and leaves the
   choice to you.
3. **Do not select a Python interpreter.** None on the host can import frappe,
   and `bench/env` is the container's. Analysis does not need one — the
   generated search paths resolve `import frappe` without it.

Your own settings go in `.vscode/`, which erp-sandbox never writes. Folder
settings override the generated ones, and your launch configurations appear
alongside.

Claude Code is briefed automatically: `render` generates
`.claude/rules/erp-sandbox-environment.md`, which loads beside the `CLAUDE.md`
that `init` seeded for your own notes.

`render` also generates `.claude/settings.json`, which stops Claude Code
editing a generated file — the edit is refused with a message pointing at
`config.toml`.

- It binds Claude Code only. You are not stopped from editing anything, and
  neither is VSCode.
- If a generated file has no way to express what you need, that is a gap in
  `config.toml`. Say so rather than editing around it.

## Debugging

Off by default. To turn it on:

1. In `config.toml`:

   ```toml
   [debug]
   enabled = true
   target  = "web"
   port    = 5678
   ```

2. ```sh
   erp-sandbox render
   erp-sandbox restart bench
   ```

3. Open `<name>.code-workspace` and pick **erp-sandbox: attach to &lt;site&gt;
   (web)** from the debug dropdown. The attach configuration is generated for
   you.

4. Set a breakpoint anywhere under `bench/apps/` and load a page.

Then:

- **Auto-reload is off while debugging**, unavoidably — the reloader
  re-executes the web server and drops the debugger. Pick up a change with
  `erp-sandbox restart bench`, and set `enabled = false` when you are done.
- **Workers cannot be debugged.** `rq` forks a work horse per job and debugpy
  does not follow the fork, so a breakpoint in job code never fires while the
  debugger looks healthy. `target = "web"` is the only accepted value.
- Enabling this publishes a third host port on `127.0.0.1` — a debug port runs
  arbitrary code by design. `erp-sandbox doctor` grows a **Debug** group.

Breakpoints not firing? See [troubleshooting.md](troubleshooting.md#breakpoints-never-fire).

## Running more than one environment

Several client environments can exist and run at the same time. Each needs:

1. **Its own directory**, created with `erp-sandbox init` — by default under
   `~/erp-sandboxes/`. Environments never share state.
2. **Its own `[ports]` pair.** `http` and `mariadb` must differ from every
   other environment you intend to run concurrently. `erp-sandbox preflight`
   checks both and refuses a collision.
3. **Its own `[project].name`**, which prefixes every container.

On a shared VPS, the admin's registry says which ports are yours — see
[vps.md](vps.md).

To work on one, `cd` into it — or stay where you are and use
`erp-sandbox --project <dir> <verb>`, which cannot leave your shell pointing
somewhere later commands silently inherit.

You do not have to stop one to use another, as long as the ports differ. If you
would rather run them one at a time, `erp-sandbox down` in the first is enough;
the environment is untouched and `erp-sandbox up` brings it back.

## Upgrading erp-sandbox

One installed `erp-sandbox` serves **every** environment on the machine, so an
upgrade changes all of them at once. Nothing inside an environment is touched —
`config.toml`, `bench/`, `data/` and the containers are unaffected.

```sh
uv tool upgrade --refresh erp-sandbox
erp-sandbox --version
```

- **It installs the newest release on PyPI.** `erp-sandbox --version` shows
  the version and the path it runs from.
- **Keep `--refresh`.** Without it uv may answer from its cached copy of the
  package index and miss a release published minutes earlier.
- **`pipx` equivalent:** `pipx upgrade --pip-args='--no-cache-dir' erp-sandbox`,
  for the same reason.
- **Installed from the git URL before?** `uv tool install --force erp-sandbox`
  switches it to PyPI releases.
- **Upgrade all your uv tools instead:** `uv tool upgrade --all --refresh`.

Re-render each environment afterwards, in whatever order suits you:

```sh
erp-sandbox --project ~/erp-sandboxes/acme-dev render
erp-sandbox --project ~/erp-sandboxes/acme-dev doctor
```

- **Generated files come from the installed tool**, so an upgrade that changes a
  template leaves them stale until you re-render. `erp-sandbox render --check`
  reports the drift and `erp-sandbox doctor` flags it.
- **Restart the stack** if the render changed anything the containers read:
  `erp-sandbox restart`.

### If an environment refuses to build

```
this environment needs erp-sandbox 0.2.0 or newer; this is 0.1.0.
```

`[erp-sandbox].min_version` in `config.toml` is how an environment states the
oldest tool that can build it. Upgrade the tool — do not lower the number.

### Downgrading

Install the older release explicitly:

```sh
uv tool install --force erp-sandbox==0.3.0
```

- **An unreleased commit** installs from git instead — a tag, branch or commit:
  `uv tool install --force "git+https://github.com/Datahenge/erp-sandbox.git@<ref>"`.

`uv tool upgrade` moves forward only.

## Stopping and removing

| Goal | Command |
|---|---|
| Stop for now, keep everything | `erp-sandbox down` |
| Start it again | `erp-sandbox up` |
| Rebuild the site from scratch, keep the clones | `erp-sandbox build --from site --force` |
| Remove the environment entirely | see below |

There is no `destroy` verb — removing an environment is deliberate and manual:

1. `erp-sandbox down` — stops the stack and removes its containers.
2. Remove the image: `podman image rm <project>-bench` (or `docker image rm`).
   Only that image is built locally; MariaDB, Redis and nginx are pulled and
   may be shared with other environments.
3. Delete the directory: `rm -rf <env>`. This removes the cloned apps, the
   virtualenv, the database, and `config.toml`.

**Step 3 destroys the database.** Back it up first if it holds anything you
want — `erp-sandbox bench backup` writes into `bench/sites/<site>/private/backups/`
inside the environment you are about to delete, so copy it out.

## Engines

Both podman (rootless) and Docker are supported. `config.toml` and the base
compose file are identical; the per-engine difference lives in a generated
overlay.

| | podman (rootless) | Docker |
|---|---|---|
| Containers run as | your uid, via `--user` + `userns_mode: keep-id` | your uid, via `--user` |
| Pods | disabled — podman refuses `--userns` with `--pod` | n/a |

Both land bind-mounted files as **you** on the host, which is the entire point.
`erp-sandbox doctor` verifies it by writing a file inside the container and
checking its owner. Detail is in [technical.md](technical.md) §2.
