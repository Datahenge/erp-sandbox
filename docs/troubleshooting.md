# Troubleshooting

**Run `erp-sandbox doctor` first.** It diagnoses without repairing and names the
group that failed. Most of what follows is a symptom `doctor` will already have
pointed at.

| Symptom | Jump to |
|---|---|
| `no erp-sandbox environment here` | [Wrong directory](#no-erp-sandbox-environment-here) |
| A port is already in use | [Port collision](#a-port-is-already-in-use) |
| `No module named 'X'` during the site step | [Missing app](#no-module-named-x-during-the-site-step) |
| `Expecting value: line 1 column 1` | [Non-JSON in custom/](#expecting-value-line-1-column-1) |
| Site loads, realtime does nothing | [socket.io route](#the-site-loads-but-realtime-does-nothing) |
| `SyntaxError: unexpected EOF` during the site step | [Old erp-sandbox](#syntaxerror-unexpected-eof-while-parsing-during-a-restore) |
| `ADMIN_PASSWORD` rejected after a restore | [The dump's password wins](#the-admin-password-does-not-work-after-restoring-a-dump) |
| Dependency conflicts after `build-bench` | [Conflicting pins](#dependency-conflicts-after-build-bench) |
| `doctor` reports drift | [Hand-edited generated file](#a-generated-file-was-hand-edited) |
| Breakpoints never fire | [Debugger not attached](#breakpoints-never-fire) |
| `import frappe` unresolved in the editor | [Editor setup](#import-frappe-is-unresolved-in-the-editor) |

---

## `no erp-sandbox environment here`

- **Cause:** you are outside a project directory.
- **Fix:** `cd` into one, or create one with `erp-sandbox init <name>`.
- **From elsewhere:** `erp-sandbox --project <dir> <verb>`.

## `already exists and is not empty` from `erp-sandbox init`

- **Cause:** the target directory holds files, and `init` will not scaffold
  among them (#51).
- **Fix:** choose another name, or `cd` into it and run `erp-sandbox init --here`
  if it really should become an environment.

## `belongs to <user>` or `is not in the registry`

- **Cause:** on a shared VPS, preflight checks your ports and `public_url`
  against `/etc/erp-sandbox/registry.toml` (#50).
- **Fix:** use the ports and subdomain the VPS admin allocated to you, or ask
  the admin to add yours. See [vps.md](vps.md).

## `stack.<key>_image does not exist`

- **Cause:** Docker Hub has no such tag. Tag names are not regular across
  images — `redis` publishes `8.0-bookworm` but its trixie tags start at
  `8.4-trixie`.
- **Fix:** pick a tag from the link in the message, then re-run. Offline,
  `erp-sandbox preflight --skip-images` skips the lookup.

## `stack.python was removed`

- **Cause:** `config.toml` predates decision #57. The base image decides the
  Python now.
- **Fix:** delete the `python = ...` line from `[stack]`.

## `stack.wkhtmltopdf = '<release>' has no .deb for <codename>`

- **Cause:** the image installs wkhtmltopdf's `.deb` for the base image's
  Debian release, and that release has none.
- **Fix:** use the release the message names — `0.12.6.1-2` for bullseye,
  `0.12.6.1-3` for bookworm. Nothing ships one for trixie.

## `apps.frappe.ref = 'version-12' but stack.frappe_version = 16`

- **Cause:** the bench is built for one series and the code checked out is
  another's — usually a v12 config edited by hand.
- **Fix:** create the environment with `erp-sandbox init --frappe 16`, or set
  both refs to `version-16`. A fork with its own branch names can ignore this.

## A port is already in use

- **Cause:** another environment is running on the same port.
- **Fix:** `erp-sandbox down` in the other environment, or change `ports.http` and
  `ports.mariadb` in this one and re-render.
- Each environment needs its own pair. `erp-sandbox preflight` checks both before
  doing any work.

## `No module named 'X'` during the site step

- **Cause:** an app imports another Frappe app that no `[[apps]]` entry names.
- **Fix:**
  1. Add the missing app to `[[apps]]`, **before** the app that imports it.
  2. `erp-sandbox fetch`
  3. `erp-sandbox build --from bench`
- The error names the file and line the import came from.

## `Expecting value: line 1 column 1`

- **Cause:** a file in an app's `custom/` folder is not JSON. Frappe v12 reads
  *every* file there and parses it, with no check on the extension — so a
  `README.md` beside the customization files stops the install and every later
  `bench migrate`.
- **Fix:** remove or relocate the offending file. The tool names it.

## The site loads but realtime does nothing

- **Cause:** the `/socket.io` route is missing, so notifications and list
  refresh die silently. It looks like an application bug.
- **Check:** `erp-sandbox doctor`, socket.io line.
- **Fix:** `erp-sandbox render && erp-sandbox restart` to rebuild the nginx config.

## MariaDB exits immediately, and `data/mariadb` is owned by root

- **Cause:** the container engine created the bind-mount source before erp-sandbox
  did. A rootful Docker daemon creates a missing one as `root`; rootless podman
  creates it as you. MariaDB runs as your uid (decision #25), so a root-owned
  datadir stops it dead — every later check then fails for a reason that is not
  its own fault.
- **Symptom:** `erp-sandbox doctor` shows `data/mariadb is owned by someone else`,
  `mariadb running: state is 'exited'`, and nothing on the published port. The
  bench and nginx containers never appear.
- **Fix:** take ownership, then bring the stack up.

  | Engine | Command |
  |---|---|
  | Docker | `sudo chown -R $(id -u):$(id -g) data/mariadb` |
  | rootless podman | `podman unshare chown -R $(id -u):$(id -g) data/mariadb` |

  ```sh
  erp-sandbox up
  erp-sandbox doctor
  ```
- `doctor` prints the right one for your engine. Running the podman form under
  Docker does nothing useful, and podman may not be installed.
- **If the datadir has no database in it yet**, deleting it is equally valid —
  `erp-sandbox build --from infra` recreates it correctly and reseeds.

## `SyntaxError: unexpected EOF while parsing` during a restore

The traceback ends in `frappe/installer.py`, `is_downgrade`, `frappe.safe_eval`.

- **Cause:** an old erp-sandbox. frappe's downgrade check reads the dump one line
  at a time and evaluates whatever follows `VALUES` on the line holding the
  `tabInstalled Application` INSERT. Current MariaDB dump tools put the rows on
  the *next* lines, so it evaluates an empty string — including for a backup
  `bench backup` produced.
- **Fix:** upgrade erp-sandbox. Since decision #39 the restore skips that check
  and erp-sandbox compares the two frappe versions itself. See
  [using.md](using.md#upgrading-erp-sandbox).
- Nothing was restored when this fires, so the existing site is intact despite
  the message about a half-built site.

## The admin password does not work after restoring a dump

- **Cause:** a restored site carries the dump's own users and passwords.
  `ADMIN_PASSWORD` from `config.toml` is applied only when Frappe first creates
  `Administrator`, and a restore skips that — the dump already has one.
- **Fix:** log in with the dump's Administrator password, or set your own:

  ```sh
  erp-sandbox bench set-admin-password '<new-password>'
  ```

- Re-rendering does not help. Only the seed step reads `[site].admin_password`,
  and only for a site it creates.
- Details: [using.md](using.md#resetting-the-administrator-password).

## `No matching distribution found for <package>`

- **Cause:** a `[stack].pip_constraints` entry forbids every version the apps
  will accept, so pip has no candidate left. The message reads as "not on PyPI";
  the package is usually there and perfectly installable.
- **Check:** the bench step names the constraint when the package is one this
  environment caps. Otherwise compare `pip_constraints` in `config.toml` against
  what the app asks for.
- **Fix:** loosen or remove the entry, then `erp-sandbox build --from bench`. An
  empty list is allowed and means no constraints.
- The v12 seed is `numpy<1.24`. It exists for a **vanilla** checkout, whose
  `openpyxl==2.6.4` reads `numpy.float`, removed in numpy 1.24. A fork that has
  modernised its pins does not need it, and one pinning `numpy==1.26.4` cannot
  build until it is emptied.
- Editing an app's `requirements.txt` instead leaves that clone dirty, and the
  next `erp-sandbox fetch` that has to move the ref stops rather than checking out
  over it.

## `this environment needs erp-sandbox <x> or newer`

- **Cause:** `[erp-sandbox].min_version` in `config.toml` names a tool newer than
  the one installed. One `erp-sandbox` serves every environment on the machine, so
  another environment's needs can be what moved it.
- **Fix:** `uv tool upgrade erp-sandbox`, then re-run.
- Do not lower `min_version` to get past it — the environment states that number
  because it depends on something the older tool does not have.
- Full procedure, including the re-render afterwards:
  [using.md](using.md#upgrading-erp-sandbox).

## `config.toml has no stack.pip_constraints key`

- **Cause:** the environment was created before the key existed, so no
  constraints are applied.
- **Fix:** add the key to `[stack]` — either the template the warning prints, or
  `pip_constraints = []` to accept none and silence it.
- Nothing is applied behind your back in the meantime; the warning says what a
  fresh `erp-sandbox init` would have seeded.

## Dependency conflicts after `build-bench`

- **Cause:** an app pins a package version the framework pins differently.
- **This is reported, never resolved.** The build does not stop, and `erp-sandbox
  doctor` reports it again on demand as a warning.
- **Fix:** read them and decide. erp-sandbox did not pick your apps and did not
  write their pins, so the conflict resolves the way a plain `bench` would —
  last install wins — and whether it matters is a judgement about the app.

## A generated file was hand-edited

- **Cause:** `.env`, the compose files, everything under `conf/`, the Procfile,
  the site config, the workspace file and the agent rule file are all outputs.
- **Fix:** `erp-sandbox render` puts it back. Never restore one by hand.
- **To change one:** edit `config.toml` and re-render. `erp-sandbox render --list`
  names every generated file.

## Breakpoints never fire

Work through these in order:

1. **Is `[debug].enabled = true` in `config.toml`?** Then `erp-sandbox render` and
   `erp-sandbox restart bench`.
2. **Did you open the workspace file, not the folder?** *File > Open Workspace
   from File* → `<name>.code-workspace`. The path mapping resolves against the
   workspace root; opening the folder leaves it unresolved and the breakpoint
   binds to nothing, silently.
3. **Is the breakpoint under `bench/apps/`?** Only that tree is mapped.
4. **Is it in background job code?** Workers cannot be debugged. `rq` forks a
   work horse per job and debugpy does not follow the fork, so the breakpoint
   never fires while the debugger looks healthy. `target = "web"` is the only
   accepted value.

Nothing reports an error in cases 2 and 3 — the debugger simply stays green.

## `import frappe` is unresolved in the editor

- **Cause:** you opened the folder rather than `<name>.code-workspace`, or you
  are on VSCodium and no language server is selected.
- **Fix:**
  1. Open `<name>.code-workspace`.
  2. On VSCodium, set `python.languageServer` to `Jedi` — Pylance is
     Marketplace-only and cannot be installed there. erp-sandbox writes the search
     paths for both and leaves the choice to you.
- **Do not select an interpreter.** There is no host interpreter that can
  import frappe, and selecting `bench/env` gives you the container's Python,
  which will not run here. Analysis does not need one — see
  [requirements.md](requirements.md) §3.
