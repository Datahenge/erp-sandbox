# Functional Specification

What the tool does, from the developer's point of view. How it does it is in
[technical.md](technical.md).

## 1. The whole product

```
  1. Install the tool -- once per machine, not once per client
     $ uv tool install erp-sandbox
     — or —
     $ pipx install erp-sandbox

  2. Create an environment
     $ erp-sandbox init acme-dev         # creates ~/erp-sandboxes/acme-dev/
     $ cd ~/erp-sandboxes/acme-dev

  3. Configure
     $ $EDITOR config.toml            # repos, refs, versions, two ports
     $ $EDITOR .erp-sandbox/secrets.env  # passwords, any git token;
                                      #   inside the environment, beside config.toml

  4. Build
     $ erp-sandbox build                 # unattended; 11 min measured

  5. Verify
     $ erp-sandbox doctor                # green across the board

  6. Work
     $ code .                         # or claude, or vim
     http://localhost:<http_port>
```

Steps 1–5 are one-time. Step 6 is every day thereafter, and step 1 is not
repeated for a second client — `erp-sandbox init` again, in another directory, is
the whole of it.

**Not `pip install`.** Modern Debian marks the system interpreter externally
managed (PEP 668) and `pip` refuses outright.

**`uv tool install` is listed first deliberately.** It picks an interpreter that
satisfies the package's `requires-python`. `pipx` does not: it builds each app's
venv with **the interpreter pipx itself runs under**, so a pipx that was
installed under an old Python installs everything under that old Python. When
the versions do not line up, the error names the constraint but not the remedy:

```
ERROR: Package 'erp-sandbox' requires a different Python: 3.10.x not in '>=3.11'
Cannot determine package name from spec '...'
```

(That is the shape of it. The instance actually hit was `3.11.4 not in '>=3.12'`,
against the older floor — which is what prompted lowering it.)

That second line is actively misleading — the spec is fine. The fix is to name
an interpreter explicitly:

```sh
pipx install --python python3.13 erp-sandbox
```

or, permanently, to make pipx's own interpreter a current one — see
[setup.md](setup.md) step 2.
The floor is deliberately as low as the code allows (3.11, decision #18) so that
this comes up as rarely as possible.

## 3. `config.toml` — the only file a developer edits

The single input. **Its schema lives in `config.sample.toml`**, which
`erp-sandbox init` copies into every new environment — so the developer reads it in
the file they are editing, and there is no second copy to drift.
[technical.md](technical.md) §3 covers the reasoning behind the fields rather
than restating them.

What matters functionally is that it is short, commented, and the only thing
standing between `erp-sandbox init` and a working environment. An unedited copy
builds a stock ERPNext, which is how a developer confirms the machine works
before involving a client's repositories.

## 4. `erp-sandbox build` — the one command

| # | Step | What it does | Idempotent? |
|---|------|--------------|-------------|
| 1 | `preflight` | **Python >= 3.11** (checked first); engine + compose; git; disk space (20 GB before the first build; once built, a warning below 10 GB, #60); the two TOML ports are free or held by this environment's own containers; detect uid/gid; validate `config.toml`; resolve every `env:` secret and fail early if unset | yes |
| 2 | `render` | TOML → `.env`, engine overlay, nginx conf, site config, Procfile, apps.txt | yes |
| 3 | `fetch` | clone each app into `bench/apps/`, host-side, credentials injected; an app with `blobless = true` is cloned without the contents of old commits | yes — skips an app already at the right ref |
| 4 | `image` | build the bench image with build-args from `.env` | yes (layer cache) |
| 5 | `infra` | start MariaDB + Redis; wait for healthchecks | yes |
| 6 | `bench` | create `bench/env`, `pip install -e` each app, build assets | mostly |
| 7 | `site` | seed the database; install each `install = true` app | **no** — guarded |
| 8 | `start` | full `up -d` including nginx and the bench honcho stack; print the URL | yes |

### Behavior

- **Resumable.** A failure is fixed and resumed with `erp-sandbox build --from bench`,
  not by starting over. Step 4
  alone can run 20 minutes, so this is not a nicety.
- **`--only <step>`** runs one step. **`--dry-run`** prints the plan.
  The steps are exactly the eight above: `preflight render fetch image infra
  bench site start`. Verification is not a build step — run `erp-sandbox doctor`
  afterwards.
- **The `site` step is destructive** and refuses to run against an existing site without
  `--force`, which drops and recreates. It is the one step where a silent re-run
  could destroy a developer's work.
- **Secrets are never echoed.** Logs print `token=<redacted>`; command tracing is
  disabled around any credential-bearing call.

## 5. `erp-sandbox doctor` — is it healthy, and if not, why

Read-only diagnosis. It reports and prints the exact remediation command; it
does not attempt repairs. *(Claude recommendation, pending confirmation: an
auto-fixing doctor is how a diagnostic tool becomes a second, undocumented build
path that drifts from `erp-sandbox build`.)*

Exit codes: `0` healthy, `1` degraded, `2` broken.

The full list of checks is in [testing.md](testing.md).

## 6. `erp-sandbox bench` — the day-to-day wrapper

`erp-sandbox bench migrate` instead of a long `exec` incantation. Decision #27
made this a verb of the installed command rather than a script in a repository;
deferred decision B is what remains open, and it is about editor integration
rather than about this.

### The commands a developer actually runs

Every one of these is a `bench` invocation inside the running bench container,
so the wrapper is thin: it resolves the engine, names the container, and passes
everything through. The container name is fixed by `container_name` in
`docker-compose.yml` (`<project>-bench`), which is why that is set explicitly
rather than left to the engines to spell differently.

```sh
erp-sandbox bench <anything>   ≡  <engine> exec -it <project>-bench bench <anything>
```

Arguments after `bench` are passed through untouched, including ones that
collide with erp-sandbox's own: `erp-sandbox bench --version` reports bench's version,
`erp-sandbox --version` reports the tool's.

| Task | Command | Notes |
|---|---|---|
| Run a snippet against the site | `erp-sandbox bench execute frappe.utils.now` | Dotted path to any callable. `--args` / `--kwargs` for parameters |
| Interactive shell with `frappe` loaded | `erp-sandbox bench console` | Needs a TTY, hence `exec -it`. The site is already `frappe.init`-ed |
| Apply schema and patches | `erp-sandbox bench migrate` | After pulling app changes. Watch for it failing halfway — v12 patches are not all idempotent |
| Clear the cache | `erp-sandbox bench clear-cache` | Redis DB 0. `clear-website-cache` as well when a web page is stale |
| Restart the web server | see below | Not `bench restart` — that is a production/supervisor command |

**Restarting.** `bench restart` talks to supervisor, which this environment does
not run: honcho runs the Procfile as PID 1 in the bench container (decision #3).
So restarting the web process means restarting its container:

```sh
erp-sandbox restart                     # everything in the Procfile
```

Restarting is rarely what is wanted, though. `developer_mode = 1` and
`bench watch` mean Python changes are picked up by the reloader and asset
changes are rebuilt on save. The cases that genuinely need a restart are a
changed `site_config.json`, a changed `Procfile`, and a new app installed into
the venv. A changed `[bench].env_file` needs more than a restart:
`erp-sandbox up --recreate bench`.

**Three things that are not bench commands** and so are not `erp-sandbox bench`:

```sh
erp-sandbox shell                       # a shell in the bench container
erp-sandbox logs -f                     # what honcho's processes are printing
erp-sandbox mariadb                     # a mariadb client on the site database
erp-sandbox mariadb -e "select 1"       # ...or a query; arguments pass through
```

…which are verbs precisely *because* they are not bench commands.

`mariadb` is the sharpest case. `bench --site X mariadb` exists, but it is an
`os.execv` with a hardcoded argv, so it can never take an argument — a query
through it dies on `no such option: -e`. `erp-sandbox mariadb` execs the client
itself, reading the credentials out of `site_config.json`, and passes your
arguments through. The password travels in `MYSQL_PWD`, handed to the engine
as `-e MYSQL_PWD` with no value, so it is on no command line — the
container's or the engine client's.

**Why not run `bench` on the host?** `bench/env` is bind-mounted and built
*inside* the container (decision #22), so its interpreter and shebangs are the
base image's Python — 3.9 for v12, 3.14 for v16. They will not execute on a modern host. The venv is
mounted so it can be **read** — for IntelliSense and for reading the pinned
v12-era dependencies whose failure modes you end up debugging — never activated
from the host. `erp-sandbox doctor` warns if it detects an attempt.

---

## 6.5 What an editor and an agent are told

`erp-sandbox init` writes a `CLAUDE.md` at the environment root. It is yours:
erp-sandbox writes it once, and no later command rewrites it, so notes about this
client survive every render.

`erp-sandbox render` generates `.claude/rules/erp-sandbox-environment.md` beside it,
which Claude Code loads automatically alongside `CLAUDE.md`. It carries the
site name, published ports, apps and refs, the container name, whether
debugging is on, the four verbs worth knowing, and the mistakes that cost an
afternoon — activating `bench/env`, reaching for a host `bench`, running `bench
restart`, or trusting a `git status` run from inside an app clone.

Both files load from anywhere in the tree, including a session started in
`bench/apps/<app>/`. Because the rule file is regenerated on every render,
upgrading `erp-sandbox` upgrades the briefing in every environment on the machine.
Do not edit it — edit `config.toml` and re-render. Run `/context` in Claude
Code to confirm what actually loaded.

`render` generates **`.claude/settings.json`** too, carrying a hook that
refuses Claude Code an Edit or Write to any generated file and answers with the
remedy: change `config.toml` and re-render. It binds Claude Code only — you and
your editor are unaffected — and it covers files, not commands. Everything
about it fails open, so a `config.toml` that will not parse leaves every file
writable, including `config.toml`.

`erp-sandbox render` also writes **`<name>.code-workspace`** at the environment
root. Open it with *File > Open Workspace from File* rather than opening the
folder: it pins the workspace root, which is what the debugger's path mapping
resolves against, and it sets the analysis paths so `import frappe` resolves
in the editor. Your own settings and launch configurations go in `.vscode/`,
which erp-sandbox never writes — folder settings override the generated ones, and
your launch configurations appear alongside the generated one.

There is deliberately **no host Python interpreter** to point an editor at, and
the workspace file names none; see [requirements.md](requirements.md) §3.
Analysis does not need one. On VSCodium, where Pylance is unavailable, select
the bundled Jedi language server once — erp-sandbox writes the paths for both and
leaves the choice to you, because that setting is window-scoped and a generated
value could not be overridden.

## 7. Debugging — real breakpoints in the running site

Off by default. Turn it on in `config.toml`, re-render, restart:

```toml
[debug]
enabled = true
target  = "web"
port    = 5678
```

```sh
erp-sandbox render
erp-sandbox restart bench
```

Then attach from the editor. **The attach configuration is generated for
you** — re-rendering with `[debug].enabled` writes it into
`<name>.code-workspace`, so open that workspace and pick *erp-sandbox: attach to
&lt;site&gt; (web)* from the debug dropdown. Nothing to copy, and the two
details that fail silently when hand-written — the path mapping and its
folder-scoped variable — are already right.

Set a breakpoint anywhere under `bench/apps` — your own app, or the framework
whose pinned v12-era behaviour you are trying to understand — and load the page.

`pathMappings` is what makes it work. The editor speaks in host paths and the
interpreter answers in container paths; without the mapping the breakpoint binds
to nothing and never fires, with no error anywhere.

**Auto-reload is off while debugging.** This is unavoidable rather than a
choice: the reloader re-executes the web server, which drops the debugger — see
[technical.md](technical.md) §10. Pick up a code change with `erp-sandbox restart
bench`, and turn `enabled` back to `false` when you are done.

**`target` is `"web"`.** Workers cannot be debugged this way, and the reason is
worth knowing before you try: `rq` forks a work horse for each job and debugpy
does not follow the fork, so the debugger reports a healthy session and no
breakpoint in job code ever fires. `preflight` refuses any other target rather
than let that look like it works.

Enabling this publishes a **third** host port, bound to `127.0.0.1` — a debug
port runs arbitrary code by design. `erp-sandbox doctor` grows a **Debug** group
when it is on, which checks the wrapper is rendered, the reloader is off,
`debugpy` is installed, and the port answers.
