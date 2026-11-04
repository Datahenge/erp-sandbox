# Technical Design

How the tool works. What it does for the developer is in
[functional.md](functional.md).

## 1. Architecture

```
                     developer's Linux host (uid/gid detected)
   ┌───────────────────────────────────────────────────────────────────┐
   │  <project>/                                                       │
   │    config.toml  ──────►  erp-sandbox build ─────► erp-sandbox render   │
   │      │                          │                                 │
   │      │ PAT (host only)          ├──►  .env                        │
   │      ▼                          ├──►  compose.<engine>.yml        │
   │    git clone over network       ├──►  conf/nginx/site.conf        │
   │      │                          ├──►  bench/sites/common_site_config.json
   │      ▼                          ├──►  bench/sites/apps.txt        │
   │    bench/apps/{frappe,erpnext,…}└──►  bench/Procfile              │
   │                                                                   │
   │    bench/          ◄── bind mount ──┐                             │
   │    data/mariadb/   ◄── bind mount ──┼──┐                          │
   │    conf/           ◄── bind mount ──┼──┼──┐                       │
   └─────────────────────────────────────┼──┼──┼──────────────────────┘
                                         │  │  │
   host :http_port ─► ┌──────────┐       │  │  │
                      │  nginx   │       │  │  │
                      └────┬─────┘       │  │  │
                     ┌─────┴──────┐      │  │  │
                     │  / ──► web:8000   │  │  │
                     │  /socket.io ──► socketio:9000
                     └─────┬──────┘      │  │  │
   ┌───────────────────────┴──┐   ┌──────┴─┐ │  │
   │  bench container         │   │mariadb │ │  │   host :mariadb_port
   │  <base image per TOML>   │   │10.4.6  │◄┘  │   ─────────────────►
   │  python + node + wkhtml  │   └────────┘    │
   │  honcho ─► Procfile      │   ┌────────┐    │
   │   web / socketio         │   │ redis  │◄───┘   (not published)
   │   schedule / 3 workers   │   │DB 0,1,2│
   └──────────────────────────┘   └────────┘
```

### Services

| Service | Image | Purpose | Host port | Persistent state |
|---------|-------|---------|-----------|------------------|
| `nginx` | pinned per TOML | single entry point; proxies `/` to web, `/socket.io` to socketio | **yes** — `ports.http`, on `[ports].bind` (default `127.0.0.1`, #46) | none |
| `bench` | built from `images/bench/Dockerfile` | honcho: web, socketio, scheduler, 3 workers | no | bind mount `./bench` |
| `mariadb` | per TOML (default 10.4.6) | the site database | **yes** — `ports.mariadb` | bind mount `./data/mariadb` |
| `redis` | pinned per TOML | cache / queue / socketio as logical DBs 0/1/2 | no | disposable |

**Why nginx earns its place.** In Frappe v12 dev without a proxy, the browser is
handed `socketio_port` from the boot info and connects to `host:9000` directly.
Publishing only the web port would leave realtime silently broken — no
notifications, no progress bars, no list auto-refresh. nginx multiplexes both
onto one port, which satisfies the "only web and MariaDB on the host" goal
*better* than publishing two ports would, and makes dev behave like production.

**Why MariaDB is published.** So developers can attach DBeaver, DataGrip, or
`mysql`. That is the only reason; nothing in the stack needs it.

**Every service restarts after a reboot** (#47): `unless-stopped` in the base
compose file, which Docker honours, and `always` in the podman overlay, because
`podman-restart.service` starts only `restart-policy=always` containers at boot
(podman 5.4.2). `erp-sandbox down` removes the containers, so neither policy
resurrects a stack taken down. Rootless podman also needs lingering and the
per-user `podman-restart.service` ([vps.md](vps.md)). `compose run --rm` drops
the policy for one-off containers: podman-compose 1.6.0 deletes it explicitly.

## 2. Bind mounts under the two runtimes

Below, `$UID`/`$GID` are the detected values, never literals.

- **Docker (rootful daemon):** run the container process as `$UID:$GID`;
  bind-mounted files are then plainly owned by the developer on the host.
- **Rootless podman:** the container's uid 0 maps to the host user by default,
  and container uid 1000 maps into the subuid range. Use `--userns=keep-id`
  (or `userns_mode: keep-id`, or an idmap mount option) so files written inside
  land as the host user.

`keep-id` is not valid Docker syntax. That is precisely why engine-specific bits
belong in a generated overlay rather than the base `docker-compose.yml`. The
podman overlay also carries `x-podman: in_pod: false`: podman-compose puts every
service in one shared pod by default, and podman rejects `--userns` together
with `--pod`, which would make `keep-id` fatal rather than merely unnecessary.

**Every service runs as the host uid** (decision #25), including MariaDB. The
alternative — letting the image drop privileges to its own internal user and
remapping that uid with `keep-id:uid=999,gid=999` — also works, but needs a
number that belongs to whatever image `[stack].mariadb_image` names. Running as
the host user needs no such number and gives the two engines one rule.

The consequence: with `--user` set, an image entrypoint can no longer `chown`
its data directory, so **the `infra` step creates `data/mariadb/` as the host user
before first start**, and so does `erp-sandbox up`. A directory owned by anyone else
then fails loudly rather than silently producing files the developer cannot
read — which is the failure this whole section exists to prevent.

**Nothing may rely on the engine to create a bind-mount source** (decision
#38). The two
disagree, and only in a way that shows up on one of them: rootless podman
creates a missing source as the invoking user, while a rootful Docker daemon
creates it as `root`. Since MariaDB runs as the host uid, a root-owned datadir
means it cannot write its own files and the container exits — on Docker only.
Every host path the compose file mounts is therefore written host-side first:
the `conf/` files by `render`, `bench/` by `fetch` (two steps before `infra`),
and `data/mariadb` by `project.ensure_data_dirs`. `selftest` parses the compose
file and fails if a mount appears with nothing to create it.

## 3. The `config.toml` contract

**The schema lives in `erp_sandbox/assets/config.sample.toml`, and only there.**

That file is executable documentation: `selftest` renders it, mutates it to
check that each bad shape is refused for the right reason, and asserts that
everything the compose file references was produced from it. A copy of the
schema in this document would be prose that nothing executes, and would be wrong
within a week.

`erp-sandbox init` copies the sample into a new environment, so a developer reads
the schema in the file they are editing.

### What this document adds

Only the reasoning the sample has no room for.

**`[stack]` pins a toolchain, not preferences.** `base_image`, `python`, `node`,
`yarn`, `wkhtmltopdf` and `bench` describe an era. bullseye is there because its
system Python is 3.9, which is what v12 needs; Node 12 because that is what v12's
asset build expects. Change the Frappe series and all of them change together,
which is why `frappe_version` selects an entry in `known_versions.py` and
preflight refuses a series nobody has analysed (decision #28).

**`[[apps]]` order is clone order and install order**, and `frappe` must be
first because every other app depends on it. `requires_credentials` asks whether
*this tool* must present a token — not whether the repository is private, which
is a different question with a different answer for ssh (decision #24).

**`blobless` is a property of the clone, not of the repository** (decision #31).
The key is false when absent; `erp-sandbox init` writes `true` for frappe and
erpnext, which is where the disk goes.
It adds `--filter=blob:none`, which is a *partial* clone rather than a shallow
one: every commit, tree and branch arrives, and only the contents of files at
old commits are left on the server until something asks for one. So `git log`
and `git bisect` behave normally, `git blame` on an old file pauses for the
network, and an offline machine cannot do either. Two consequences worth
knowing before the key is set:

- **It applies when the app is cloned.** Flipping it on does not shrink a
  repository already in `bench/apps/`, and flipping it off does not fill one in;
  `git clone` records the filter in `remote.origin.partialclonefilter` and every
  later `fetch` inherits it. `erp-sandbox doctor` reports the mismatch and prints
  the command that resolves it, in whichever direction, and converts nothing
  itself — that repository is where the developer works.
- **Preflight refuses it together with `requires_credentials = true`**, for the
  reason in §5: the token exists for the duration of one command, and a partial
  clone's later fetches are not that command.

**`[stack].pip_constraints` is seeded once and then belongs to the
environment** (decision #36). It is a pip *constraints* file (`-c`), not a
requirements file: nothing in it is installed, and a package is capped only if
some app already asks for it. One list applies to every app's install.

`erp-sandbox init` writes it from the series template in `known_versions.py`, and
that is the only time the template is read — the build reads `config.toml` and
nothing else. So a list a developer empties stays empty, and erp-sandbox never puts
a ceiling back.

- **An absent key means no constraints**, and is warned about by `preflight`,
  `doctor` and the bench step. Absence is not a choice — it is a `config.toml`
  written before the key existed — so it is said out loud rather than acted on
  silently. An explicit `[]` is a choice, and says nothing.
- **The warning is repeated at the bench step** because `erp-sandbox build --from
  bench` skips preflight, and that is the command a failed build prints.
- **Entries carry no whitespace** (`"numpy<1.24"`, never `"numpy < 1.24"`), and
  a bare package name is refused: in a constraints file it parses happily and
  caps nothing, so it is the silent form of the typo it looks like. The build
  transports the list as one space-separated argument and splits on whitespace
  to write the file, so relaxing the rule means changing the transport first.
- **A constraint that leaves no candidate is explained.** pip reports it as
  `No matching distribution found for <pkg>`, which reads as "not on PyPI"; when
  the package is one this environment capped, the bench step says so and names
  the key. The v12 seed collides this way with any fork pinning numpy ≥ 1.24.

**`[ports]` versus `[internal_ports]`.** Two ports reach the host: HTTP through
nginx, and MariaDB solely so a GUI client can attach (decision #16). Everything
else is container-network only. Two environments on one machine need different
`[ports]` and can share `[internal_ports]` — which is what makes running a
client and a vanilla bench side by side work.

**`[database].encryption_key` exists to be impossible to forget.** Restoring a
dump without the key that matches it leaves every stored password silently
unreadable, and the failure surfaces weeks later as integrations that cannot
authenticate. It is a required field in the dump modes, checked twice
(§6, risk 6).

**Secrets are `env:NAME` by default**, resolved from the shell first and then
from `.erp-sandbox/secrets.env`. The shell winning is the useful direction: it lets
a developer override one value for one run without editing a file. Secrets are
grouped by the step that consumes them, so `render` does not demand a git token
it never presents (§5).

**`[ports].bind` is an IPv4 address and nothing else.** It becomes the host
half of nginx's `ports:` entry, where an IPv6 literal needs brackets the two
compose front ends do not agree on. Preflight tests the http port for freedom
on that address; mariadb and debug are always loopback.

**`[site].db_name` is optional and read once.** It becomes `bench new-site
--db-name` in every seed mode — a restore runs into the empty site that call
creates, and keeps its name. Frappe also uses it as the database user. Unset,
frappe generates `_` plus 16 hex digits. Preflight accepts
`^[A-Za-z_][A-Za-z0-9_]{0,63}$`: 64 is MariaDB's limit for a database name, and
the character set needs no quoting. Changing it on an existing site changes
nothing until the site is recreated; `erp-sandbox doctor` warns while the two
disagree.

**`[site].public_url` is `https://<hostname>`**, with no path and no port.
Frappe builds root-relative URLs and sets its session cookie on path `/`, so it
cannot be served under a prefix (#41), and an http `host_name` would put
http links into every email and webhook. When set it renders as `host_name`
in `common_site_config.json`, which `frappe.utils.get_url()` prefers over the
incoming request; unset, the key is absent and nothing changes.

## 4. Rendering

`erp_sandbox/render.py`, standard library only — `tomllib` plus `string.Template`.
Decision #18's Python floor is what keeps the renderer free of a third-party
TOML parser. Templates live in `erp_sandbox/templates/`.

`render` does two different jobs, and the distinction matters because decision
#27 split the package from the environment:

- **Templates** (`erp_sandbox/templates/*.tmpl`) are substituted against the TOML.
- **Assets** (`erp_sandbox/assets/`) are copied verbatim out of the package into
  the environment, because `docker-compose.yml` names its paths *relative to
  itself* and they have to resolve where compose runs.

Both are outputs. Both are overwritten. **16 files** as of 2026-08-23 — run `erp-sandbox render --list` for the authoritative set:

| Output | Source | Consumer | Notes |
|--------|--------|----------|-------|
| `.env` | template | Compose | ports, image tags, detected uid/gid, project name, passwords. Written `0600` |
| `compose.podman.yml` / `compose.docker.yml` | template | Compose | engine overlay: `userns_mode: keep-id` + `x-podman: in_pod: false` vs plain `user: "$UID:$GID"` |
| `conf/nginx/site.conf` | template | nginx | upstreams plus the `/socket.io` location |
| `bench/sites/common_site_config.json` | template | Frappe | db host/port, three `redis://redis:6379/N` URLs, socketio port, developer_mode |
| `bench/sites/apps.txt` | template | Frappe | the `install = true` apps, in order |
| `bench/Procfile` | template | honcho | no redis lines (own container); ports from TOML |
| `.claude/rules/erp-sandbox-environment.md` | template | Claude Code | the agent briefing — site, ports, apps, verbs, and the traps. See §4.1 |
| `<project>.code-workspace` | template | VSCode | analysis paths, and the debugpy attach configuration when `[debug]` is on. See §4.2 |
| `.claude/settings.json` | template | Claude Code | the `PreToolUse` hook that refuses an edit to a generated file. See §4.3 |
| `docker-compose.yml` | asset | Compose | the base file, common subset |
| `conf/mariadb/erpnext.cnf`, `conf/redis/redis.conf`, `conf/nginx/00-resolver.sh` | asset | services | tuning and the per-request DNS resolver |
| `images/bench/{Dockerfile,entrypoint.sh}` | asset | the image build | the build context `docker-compose.yml` names — the build context `docker-compose.yml` names. `selftest` and `doctor` both check it exists |

Every generated file carries a header: *"GENERATED from config.toml — do not
edit."* — except `.claude/settings.json`, which is strict JSON and cannot
(§4.3). `erp-sandbox render --check` renders to memory and diffs against disk,
exiting non-zero on drift; `erp-sandbox doctor` calls it, and `selftest` asserts
that every relative path the compose file names was actually produced.

The base `docker-compose.yml` stays in the Docker Compose / podman-compose
common subset and uses `${VAR:?err}` throughout, following Catalyst's idiom.

### 4.1 The agent briefing, and why it is a rule file

Two files, with two owners. `erp-sandbox init` seeds `CLAUDE.md` at the
environment root and never touches it again, so a developer's notes about the
client survive every render. `render` owns
`.claude/rules/erp-sandbox-environment.md`, which is overwritten like any other
generated file — so upgrading the tool upgrades the briefing in every
environment, while the notes beside it are untouched.

**It is not an `@import` from `CLAUDE.md`, and that is load-bearing.** Claude
Code drops an import whose path resolves outside the working directory, so a
session started in `bench/apps/<app>/` — the ordinary way to work on one app —
would load `CLAUDE.md` and silently lose the file it imports. `.claude/rules/`
is loaded from an ancestor directory the way `CLAUDE.md` is, so it survives the
subdirectory case. Anything the briefing must reach an agent with goes in the
rule file, never behind an import.

Everything loaded this way enters the context window at launch and stays for
the session, so the briefing is kept short — `selftest` fails it above 200
lines, which is Claude Code's own guidance. It states what wastes time and
points at `erp-sandbox --help` and `erp-sandbox render --list` rather than
duplicating them; a list mirroring code goes stale where a pointer does not.

### 4.2 The VSCode workspace file

A workspace file rather than `.vscode/settings.json`, for three reasons that
are mechanical rather than stylistic. Opening it **pins the workspace root**,
which is what the debugger's path mapping resolves against — open
`bench/apps/<app>/` instead and the mapping points at nothing. It carries
`settings` and `launch` in one artifact. And it leaves `.vscode/` entirely to
the developer: folder settings override workspace settings, and folder launch
configurations appear alongside the generated one, so erp-sandbox never has to
reconcile with a developer's own configuration.

Only **resource**-scoped settings can be overridden at folder level. That is
why `python.languageServer`, which is window-scoped, is left unset rather than
written: a generated value could not be taken back.

**No interpreter is named.** `bench/env` is the container's venv and no host
interpreter can import frappe (§9, requirements.md §3). An interpreter that
resolves but cannot import frappe is worse than none, because selecting it
marks every frappe symbol unresolved. Analysis needs no interpreter: it comes
from `extraPaths`, and an analyzer reads source rather than executing it.

`extraPaths` is written in **both spellings** — `python.analysis.*` for
Pylance and `python.autoComplete.*` for Jedi. Pylance is Marketplace-only and
cannot be installed on VSCodium, which uses the Jedi server bundled with the
Python extension; a setting whose extension is absent is ignored, so writing
both is safe. Each carries **one entry per app**, because the importable
package is at `bench/apps/<app>/<app>/` and `bench/apps` alone resolves
nothing, plus the container's `site-packages`.

Note that VSCode auto-discovers virtualenvs in the workspace, so it will find
`bench/env` and offer it in the interpreter picker.
`python.venvFolders`/`python.venvPath` are machine-scoped and only *add*
discovery locations, so there is no way to hide it. `doctor`'s warning and
`python.terminal.activateEnvironment: false` are the available defences, and
the latter stops auto-activation rather than selection.

### 4.3 The edit guard

Decision #35. Every other generated file says *do not edit* and trusts the
reader. This one makes Claude Code unable to: `render` writes a `PreToolUse`
hook into `.claude/settings.json`, matching `Edit|Write`, running `erp-sandbox
guard-edit`. The hook reads the tool call on stdin and prints a
`permissionDecision: "deny"` when the target is a file `render` writes.

**The reason is shown to the model**, which is what makes it a correction
rather than an obstruction: the refusal names `config.toml` and `erp-sandbox
render`, and asks for a gap in the tool to be reported rather than worked
around by hand. A denial with no remedy in it sends an agent looking for
another way to write the same file.

Four properties are load-bearing.

- **It fails open, on every error path.** Junk on stdin, a payload it does not
  recognise, a `config.toml` that does not parse — all allow the write,
  silently. A guard that refuses because it could not read its own input blocks
  real work for a reason the developer never sees, and a broken `config.toml`
  is exactly when files need editing. The cost of failing open is one
  hand-edit that `render --check` catches later, which is the situation this
  improves on rather than replaces.
- **The environment is found from the file being written**, not from the
  session's working directory — an edit to a generated file is by definition
  inside an environment, whatever directory the session started in. A file in
  no environment is not erp-sandbox's to refuse.
- **The generated list is derived without resolving a secret.**
  `render.output_paths()` builds the same outputs `build_outputs` does and
  keeps only the keys; which files are generated is a property of the
  configuration, what is in them is not. The hook runs whenever Claude Code
  writes a file, which is not a moment when a password can be assumed to be in
  the environment.
- **The command is written as an absolute path**, resolved at render time from
  `shutil.which("erp-sandbox")`, falling back to the rendering interpreter's
  `-m erp_sandbox`. A hook whose command is not on the PATH that started the
  editor does not refuse anything: it reports an error and the write proceeds,
  which is this guard's own failure mode wearing a disguise. That absolute path
  is machine-local, which is why the file is gitignored like every other
  generated output.

The file is **strict JSON with no header comment**, alone among the generated
files. Claude Code owns that schema, erp-sandbox does not, and a settings file it
cannot parse takes the whole configuration down rather than just the hook.
`selftest` parses it with `json.loads` rather than the JSONC reader the
workspace file needs, so adding the usual header is caught immediately.

**It binds Claude Code only.** A human at a terminal, VSCode, or any other
agent is unaffected, so the guard complements the briefing in §4.1 rather than
replacing it. Scope is deliberately narrow: files, not commands. Matching
strings in a shell command to catch `bench restart` or a `bench/env` activation
would both over- and under-match, and a hook that blocks legitimate work is
worse than no hook.

## 5. Credential handling

Two ways to reach a private repository, chosen per app by
`requires_credentials`:

- **ssh** (`requires_credentials = false`) — the developer's own key and agent
  authenticate the clone. The tool supplies nothing and manages nothing; it
  needs no `[credentials]` entry and holds no secret.
  Preflight **rejects** `requires_credentials = true` on an ssh remote rather
  than ignoring it: a token cannot be presented over ssh, and a silently
  ignored flag is worse than a refused one. *(Claude's implementation choice,
  confirmed by Brian 2026-08-21.)*
- **https + PAT** (`requires_credentials = true`) — the tool presents the token
  from the `[credentials.<host>]` entry, under the rules below.

A PAT with read access to private client repos is a real secret. Three rules:

1. **The PAT never enters a container, an image layer, or a build arg.** All
   cloning happens **on the host**, in `erp_sandbox/fetch_apps.py`, writing into
   `bench/apps/` — which is bind-mounted anyway, so the result is exactly where
   it needs to be. A build arg would be visible in `image history` forever; this
   avoids the problem rather than mitigating it.
2. **The PAT is never written to disk by the tool.** No `.netrc`, no rewritten
   `.git/config` with an embedded token, no credential store. It reaches `git`
   for the duration of one clone via `GIT_ASKPASS` pointing at a helper that
   reads it from the environment; the resulting remote is then rewritten to the
   clean, token-free form.
3. **`config.toml` is gitignored**, and `env:` indirection is the documented
   default, so normally the secret exists only in the developer's shell.

Rule 2 is why `blobless` and `requires_credentials` cannot both be true
(decision #31). A partial clone defers fetches to whenever a command first
wants an old file's contents, which is days later, in the developer's own
shell, with no `GIT_ASKPASS` in sight. Preflight refuses the combination rather
than shipping an app whose history works until it doesn't.

This makes `git` and network reachability to the private host **host-side**
prerequisites, checked in preflight.

### No secret on a command line

Every command line on a host is in `ps` for every user, and on a shared VPS
(#48) that is other developers. A container's processes are host processes, so
that covers the container's command lines too. Secrets therefore travel by
environment or stdin:

- **The seed and restore scripts** receive secrets as `export` lines prepended
  to the script, which goes to `bash -s` on stdin (`seed_db.secret_preamble`).
  Positional arguments carry only non-secret values.
- **frappe** takes a root or admin password only as an option, from
  `site_config.json`, or from a prompt. `bench_with_secrets` runs the module
  bench itself runs — `cd sites; env/bin/python -m frappe.utils.bench_helper
  frappe <args>` — and appends each option's value to `sys.argv` from an
  environment variable inside the process, where `/proc/<pid>/cmdline` does not
  see it.
- **The mariadb client** reads `MYSQL_PWD`.
- **`docker exec` / `podman exec`** get `-e NAME` with no value, which both
  engines fill from the caller's environment. `-e NAME=value` would put the
  value on the engine client's command line.

### Runtime secrets for the apps

The bench container receives no build or host secret. Apps that read their
own credentials from the environment get them from **`[bench].env_file`**, a
file of their own (decision #61):

- **render** adds `env_file:` to the bench service in the generated overlay,
  and to no other service. A relative path passes through unchanged: compose
  resolves it against the compose file's directory, which is where
  `config.toml` is.
- **preflight** fails when the file is missing, does not parse, is
  group- or world-readable, would be committed, is the `[secrets]` file, or
  sets any name that `config.toml` reads its own secrets from or that the
  `[secrets]` file sets. It warns on a `$` in a value, which a compose front
  end may expand. It reports names only.
- **doctor** runs `printenv NAME >/dev/null` per name inside the container,
  so it reports which names arrived without any value leaving the container.
- **An env_file is read when the container is created.** `erp-sandbox up
  --recreate bench` passes `--force-recreate`; `restart` keeps the old values.
- `docker inspect` / `podman inspect` show the values to anyone who can run
  the engine as the developer — the same access as a shell in the container.

### How the token reaches git

`GIT_ASKPASS` points at a five-line `sh` script written to a temporary
directory for the run and deleted when it ends. The script contains **no
secret** — it names two environment variables, and git's own prompt text
("Username for …") selects which one to print. The values exist only in the
environment of the one `git` child process that needs them.

Two alternatives were rejected: `-c credential.helper='!echo …'` puts the token
on a command line, where `ps` shows it to every user on the machine, and a
token embedded in the clone URL ends up in `.git/config` permanently. After
each clone, `.git/config` is checked for the token anyway — a claim this
important is worth verifying rather than asserting.

`GIT_TERMINAL_PROMPT=0` and `ssh -o BatchMode=yes` guarantee that a missing
credential fails in seconds rather than hanging an unattended build on a prompt
nobody is there to answer. Failures that git describes badly — a private https
repo with no `[credentials]` entry produces only "could not read Username" —
get a second line naming the actual cause.

**Every git the tool starts drops git's repository variables** — the list
`git rev-parse --local-env-vars` prints. A git hook exports them to everything
it runs; `git commit -a` sets `GIT_INDEX_FILE` to its temporary index. A git
inheriting them reads and writes the outer repository's index, not the one in
its working directory, and `git -C` does not override them. Every git call runs
with `project.git_env()`: `fetch_apps.Git`, doctor's `_git`, and preflight's
`git_would_commit`. `selftest` also drops them from its own environment before
building scratch repositories, because it runs from the pre-commit hook.

### Re-running over a working tree the developer has been editing

`bench/apps/` is where an ERP modification is written, and `fetch` runs on every
build. The rule is that **uncommitted work outranks `config.toml`**:

| State | What happens |
|---|---|
| Directory absent | clone at `ref` |
| At `ref`, clean | nothing |
| At `ref`, dirty | nothing — the edits are the point of the environment |
| Not at `ref`, clean | check out; fast-forward only, never a reset or a rebase |
| Not at `ref`, dirty | **refused**, naming the file count and both refs |
| `origin` points elsewhere | **refused** — repointing could silently swap one fork for another |
| Present but not a git repo | **refused** — the tool does not delete directories it did not create |

A clone goes to a `.<name>.incomplete` sibling and is renamed on success, so an
interrupted clone cannot leave a half-populated app that the next run mistakes
for a finished one.

*(Claude's implementation choice, confirmed by Brian 2026-08-21: a ref change
over a dirty tree stops the build rather than warning and continuing. Continuing
would mean building against a ref the config did not ask for, which fails later
and less clearly.)*

## 6. Database seeding

`erp_sandbox/seed_db.py`, honoring `[database].seed`:

- **`new_site`** (default) — `bench new-site` with the TOML admin password, then
  `bench install-app` for each `install = true` app in order. Produces an empty
  but working ERPNext. No client data, no secrets.
- **`sql_dump`** — create the database and user, stream the dump in, then write
  `site_config.json` including the **`encryption_key` that matches the dump**.
  Without it, every stored password in the restored database is silently
  unreadable, and the failure surfaces much later as integrations that cannot
  authenticate. The key is a TOML field precisely so it cannot be forgotten, and
  the script **aborts** if `seed = "sql_dump"` with an empty `encryption_key`.
  Finish with `bench migrate`.
- **`frappe_backup`** — `bench restore` a frappe-native tarball, same
  encryption_key handling, then `bench migrate`.

Re-runnable: drops and recreates the database, so a bad restore is one command
from a clean retry. It is the one destructive step in the tool, so it refuses an
existing site without `--force`.

### `[site].admin_password` does not survive a restore

It reaches `bench new-site --admin-password`, which stores it in
`site_config.json` for `frappe`'s `after_install` hook to apply. That hook runs
inside `install_app("frappe")`, and `install_app` returns early for an app the
database already lists as installed — which a restored dump does. So the
password that answers for `Administrator` on a restored site is the dump's,
carried in its `__Auth` table, and the seed step has no way to change that
without writing to a client's user table.

The build does not reset it. A developer does, with `bench
set-admin-password` — [using.md](using.md#resetting-the-administrator-password).

Verified against frappe 12.28.2: `frappe/installer.py` `install_app`,
`frappe/utils/install.py` `after_install`.

### The frappe version is compared here, not by frappe

`bench restore` is called with `--force`, which in this frappe skips exactly
one thing: `is_downgrade()`. That check locates the `tabInstalled Application`
INSERT by scanning the dump line by line, then `eval()`s whatever follows
`VALUES` **on that same line**. Current MariaDB dump tools put the rows on the
lines after it, so it evaluates an empty string and raises
`SyntaxError: unexpected EOF while parsing` before anything is restored — for a
backup `bench backup` produced. It is unreachable, not merely strict.

`seed_db.report_versions` does the comparison instead, on the host, where the
whole statement can be read. It prints `frappe: dump <a> -> bench <b>` and
**warns** when the dump is newer; it does not abort. Only the numeric leading
components are compared, because a fork's version string carries anything after
them. Decision #39.

### Restoring inside a container

`erp-sandbox restore <dump>` runs this same restore with the dump, and
optionally the files tarballs, named on the command line instead of in
`config.toml`. It needs `[database].encryption_key` whatever `[database].seed`
says.

Constraints on `bench restore` that apply only inside a container:

- **The sequence is `drop-site` → `new-site` → `restore`.** frappe reinstalls
  the database on every restore — `_new_site` receives `force=True` whatever
  the flags say — and the `DROP USER` it issues names the user *at the address
  the client is connecting from*, while the grant was made at `'%'`. A
  container's address changes every restart, so `prune_stale_grants` cleans up
  afterwards. Dropping the site first removes the database, the user and the
  site directory cleanly.
- **The dump keeps its own filename.** v12 decides whether to decompress with
  `sql_file_path.endswith('sql.gz')`, so a dump mounted at a fixed path with no
  suffix is read as plain SQL and fails on the gzip magic bytes.
- **It is mounted under `/tmp`.** frappe writes the decompressed `.sql` beside
  the source, and the directory an engine invents for a single-file mount
  belongs to root — the container runs as the host user (decision #25). A large
  dump therefore needs its own size again in free space while restoring.
- **Its output is captured as bytes.** The restore streams gzip past stderr, and
  decoding that as UTF-8 raises and takes the whole log with it.
- **The image carries the `file` command.** v15 and v16 run `file` on the
  backup to detect encryption, and fail with `CommandFailedError` when it is
  absent. Slim base images do not include it, so it is in the Dockerfile's base
  package list, for every series.

`site_config.json` is written **as JSON, directly**, not through
`bench set-config`: that command rejects `--site` in either position, and passes
the value through click, so any value beginning with `-` is read as an option —
which a base64url encryption key is, about one time in sixty-four.

## 7. Keeping it generic

Decision #17 is a property that decays silently unless something checks it.

- No client name, site name, port, password, repo URL, image tag, or app name
  appears in any `.sh`, `.py`, `Dockerfile`, template, or `docker-compose.yml`.
- **No committed file carries real client values** — not `config.sample.toml`,
  not the prose in `docs/`. Placeholders (`acme`, `example.com`) stand in
  everywhere. Real values live in `config.toml` and `local/`, both gitignored.
- **A `doctor` check greps the tool's committed tree for the client's values and
  fails if it finds one**, skipping `config.toml` and `local/`. Cheap to write,
  and it is what converts an intention into a guarantee.
- **A second, separate check asks the user's question** — whether their own
  `config.toml` and `.erp-sandbox/` are committed. An installed erp-sandbox has no
  source tree, so conflating the two asks users something they cannot act on.
- **The pre-commit hook is the earlier line of defence**, since `doctor` needs a
  running environment and a commit does not. `tools/pre-commit`, installed by
  `tools/install-hooks.sh`, wired through `core.hooksPath`.
- Directory names derive from `[project].name`, not literals.

## 8. File tree

Decision #27 puts these in two trees.

### The package — installed, shared, replaced on upgrade

```
erp_sandbox/
├── __init__.py  __main__.py
├── cli.py                        # the verbs
├── project.py                    # the two roots; .erp-sandbox/ discovery
├── preflight.py                  # validation, shared with doctor
├── render.py  fetch_apps.py  build_bench.py  seed_db.py
├── known_versions.py             # the supported Frappe series (#28)
├── selftest.py  fixtures/
├── templates/                    # *.tmpl -> generated files
└── assets/                       # shipped verbatim into an environment
    ├── config.sample.toml  secrets.sample.env
    ├── docker-compose.yml
    ├── conf/{mariadb,redis}/…  conf/nginx-00-resolver.sh
    └── images/bench/{Dockerfile,entrypoint.sh}
```

### A client environment — created by `erp-sandbox init`, never upgraded

`erp-sandbox init <name>` creates it at `~/erp-sandboxes/<name>/`; a name that
reads as a path (`./x`, `~/x`, `/srv/x`) is used as given (#51). Location
does not matter afterwards: every verb finds the environment by walking up
for `.erp-sandbox/`.

```
<name>/
├── config.toml                   # THE input. The only file a developer edits
├── CLAUDE.md                     # seeded once by init; yours, never rewritten
├── .claude/rules/                # GENERATED — the agent briefing (§4.1)
├── <name>.code-workspace         # GENERATED — open this, not the folder (§4.2)
├── .erp-sandbox/                    # the marker, and machine-local state
│   └── secrets.env               # 0600
├── .env                          # GENERATED
├── docker-compose.yml            # GENERATED (asset, copied from the package)
├── compose.{podman,docker}.yml   # GENERATED
├── conf/                         # GENERATED
├── images/bench/                 # GENERATED (asset) — the image build context
├── bench/                        # cloned apps, the venv, the site
│   ├── apps/  env/  sites/       #   Procfile, sites/apps.txt and
│   └── Procfile                  #   sites/common_site_config.json are GENERATED
└── data/mariadb/                 # the database
```

Everything in the environment except `config.toml` and `.erp-sandbox/` is
generated, cloned or downloaded — which is what makes an environment cheap to
throw away and rebuild.

## 9. The bench image

`images/bench/Dockerfile`, every version supplied as a build arg from `.env`:

- Python — the base image's own when it is the series' version, otherwise
  Debian's `python3` + `python3-dev` (below)
- build toolchain and `pkg-config`
- Node from the official tarball, installed so `/usr/local/bin/node` exists —
  the Procfile's socketio line hardcodes that path
- Yarn 1.x
- wkhtmltopdf patched-qt, plus fonts. The `.deb` is chosen by the base image's
  Debian codename, so `[stack].wkhtmltopdf` must name a release that ships one
  for it — preflight refuses a known-impossible pair (decision #57)
- `mariadb-client` (for restores), `redis-tools` (for debugging)
- a non-root user matching the detected host uid/gid
- `bench` pinned, in its own venv, installed with the series'
  `bench_cli_installer` (`pip<21` for v12, which cannot run on v16's 3.14)

**`bench/env` is bind-mounted, not a named volume** (decision #22). It comes
along with the `./bench` mount rather than needing one of its own. Two
consequences worth stating: the venv is built *inside* the container, so
`bench/env/bin/` holds the image's interpreter (3.9 for v12, 3.14 for v16) and
shebangs from the base image, none of which will execute on a modern host — `doctor` warns about this
and [using.md](using.md) says not to activate the venv from the host. And the host can
read the installed third-party sources directly, which is the point.

`entrypoint.sh` waits for MariaDB and Redis to answer, then `exec honcho start`.

### Python comes from the image, and the series says which

`config.toml` has no `python` key (decision #57). The series entry in
`known_versions.py` names the Python, render writes it to `.env` as
`PYTHON_VERSION`, and the image build:

1. uses the base image's `python3` when it already is that version — the
   official `python:3.14-slim-bookworm` (v16) carries 3.14 in `/usr/local`;
2. otherwise installs Debian's `python3` — `debian:bullseye-slim` (v12) gets
   3.9 this way;
3. fails with the version it found if the result is still not the series'.

No Debian release ships 3.14, which is why v16's base image is the official
python image rather than plain Debian (decision #55). It is **bookworm** and not
trixie because wkhtmltopdf/packaging's last release, `0.12.6.1-3` (2023),
ships `.deb`s for bullseye and bookworm only.

### Preflight asks Docker Hub whether each image tag exists

`base_image`, `mariadb_image`, `redis_image` and `nginx_image` are each looked up
at `hub.docker.com/v2/repositories/<repo>/tags/<tag>` (decision #58). A 404 is
refused, before the image build spends minutes and the infra step finds out;
no answer is a warning, so preflight still works offline. An image on another
registry, or pinned by `@sha256:`, is reported as not checked. `--skip-images`
turns the lookup off.

### What preflight checks against the series

Refused, because the build would fail minutes in (decision #57):

- `node`'s major is not the series'.
- `bench` is older than the series' `bench_min`.
- `wkhtmltopdf` names a release with no `.deb` for the Debian codename in the
  `base_image` tag (`WKHTMLTOPDF_DEBS`).
- `python` is present at all.

Warned about by preflight and `doctor`, because frappe itself only warns or a
fork may be deliberate:

- frappe or erpnext has a `version-N` ref with N ≠ `frappe_version`.
- `mariadb_image`'s version is outside the series' `mariadb_range`
  (v16: 10.6–11.8, from frappe's own `setup_db.py`).

`[internal_ports]` collisions are checked only among `web`, `socketio` and
`file_watcher`, the three that share the bench container; `mariadb` and `redis`
listen in containers of their own. `[frappe].gunicorn_workers` is accepted and
ignored: `bench serve` is werkzeug, and only bench's production supervisor
config reads it.

### Two installers in one venv

The framework and ERPNext are `setup.py` based and predate setuptools 58, which
removed `use_2to3`; they need `pip<21`, `setuptools<58`. A client app written any
time recently is pyproject-only, and an editable install of one needs PEP 660,
which arrived in pip 21.3. Both kinds live in the same bench.

That is v12. `build_bench.py` switches installers mid-run, in `config.toml`
order, at the boundary where the first pyproject-only app appears. The versions
come from `known_versions.py` (decision #28), not from the script — they are a
property of the Frappe series. A `setup.py` app listed *after* a pyproject one
is refused rather than silently downgrading the installer.

**v16 has no legacy installer**: `pip<21` cannot run on Python 3.14, and frappe
and erpnext are both `flit_core` pyproject builds. A `setup.py` app on v16 is
installed by the modern pip, with build isolation left on, since it declares no
backend to find in the venv.

Pyproject apps are installed `--no-build-isolation`. They commonly declare
`frappe` among their build requirements, and an isolated build would fetch a
package of that name from PyPI instead of using the bench's own editable
install. Everything the build needs must therefore already be in the venv,
build backend included. Before each pyproject app, `build_bench.py` reads that
app's own `[build-system].requires` and installs it into `bench/env` —
`flit_core` for frappe and erpnext, `hatchling` for an app that names it, and so
on — leaving out `frappe` and `erpnext`, which the bench's editable installs
already satisfy. Then it asks the backend itself, through PEP 660's
`get_requires_for_build_editable` hook, for anything it needs only at build
time — hatchling answers `editables` there and nowhere in `[build-system]`.
With isolation on, pip makes both calls; with it off, nothing does. The hook is
called through the `pyproject_hooks` that pip vendors, and on v12's 3.9 venv the
TOML is read with pip's vendored `tomli`, since `tomllib` arrived in 3.11.
Build backends therefore end up installed in `bench/env`, which is harmless.

**Apps can need system libraries**, and the image cannot guess which — an app
wrapping a system service needs that service's `-dev` package to build its
bindings. `[stack].extra_packages` is a list of Debian package names installed
into the bench image, empty by default. Hardcoding any particular app's need
would make the image specific to that app (decision #17).

**bench decides what is a bench** by the presence of `apps`, `sites`, `config`,
`logs` and `config/pids`. `bench init` would create them; this tool builds the
tree itself, so `build_bench.py` creates them. Without them every `bench`
command reports "Command not being executed in bench directory" and then hides
most of its own subcommands.

**The series decides the system packages too.** vanilla frappe v12 pins
`psycopg2-binary==2.8.4`, which predates Python 3.9 and ships no cp39 wheel, so
pip builds it from source and needs `pg_config` — to build a PostgreSQL driver
a MariaDB bench never loads. The client's fork pins a version that has a wheel
and needs nothing. So the image's package list is the **union of two different
questions**: what the *series* needs (`known_versions.py`, decision #28) and
what this environment's *apps* need (`[stack].extra_packages`). Conflating them
worked only for as long as one config answered both.

---

## 10. Debugging with debugpy

Off unless `config.toml` says otherwise (decision #29):

```toml
[debug]
enabled = false
target  = "web"
port    = 5678
```

The section is optional — an environment without one has debugging off, which
is what an environment created before the section existed means by saying
nothing. Present means all three keys are required, so a half-written section
cannot half-apply. Disabled reaches no generated file at all: no `PORT_DEBUG`
in `.env`, no `ports:` entry in the overlays, and a `web:` line identical to the
one a bench without the feature produces.

`debugpy` is installed into `bench/env` by `build_bench.py` **regardless of the
flag**. It is a pure-Python wheel with no dependencies of its own, so it cannot
join the conflicts of decision #30, and it does nothing unless the Procfile
wraps the server in it. Installing it conditionally would turn flipping
`enabled` into a `ModuleNotFoundError` in a process nobody is watching.

### The web server is re-executed, and only one of the two processes serves

`bench serve` runs werkzeug's reloader unless `--noreload` is passed. This is
not tied to `developer_mode`: `frappe/app.py` sets `use_reloader = not
no_reload`, and nothing consults `developer_mode` on that path.

The reloader is not a `fork`. It re-executes the whole command in a subprocess
with `WERKZEUG_RUN_MAIN=true`, keeps the listening socket in the parent, and
hands it to the child on fd 3. **Both processes carry an identical command
line.** The parent has one thread and never serves a request; the child has the
application and every worker thread. `ps` gives you nothing to tell them apart.

Two consequences, and both fail silently:

- An unguarded `debugpy.listen()` runs in both. The parent binds the debug port
  first, the child gets `EADDRINUSE` and exits, and the reloader restarts it
  forever.
- A `-m debugpy` prefix does not survive a reload. `_get_args_for_reloading()`
  rebuilds argv as `[sys.executable] + sys.argv[1:]`, so the wrapper is dropped
  the first time a file is saved — the debugger works, then quietly stops
  existing.

So **the reloader is off whenever debugging is on**, which is what `--noreload`
on the generated `web:` line is for. The cost is real and is the feature's one
tradeoff: while debugging, a code change needs `erp-sandbox restart bench` rather
than being picked up automatically.

### The wrapper wraps `bench/env`'s interpreter, not `bench`

`bench` runs under `/opt/bench-cli` and dispatches to `bench/env/bin/python -m
frappe.utils.bench_helper`, from `bench/sites/`. debugpy has to wrap the
interpreter that actually imports frappe, so the generated line reproduces that
dispatch instead of calling `bench`:

```
web: cd sites && ../env/bin/python -m debugpy --listen 0.0.0.0:<port> \
       -m frappe.utils.bench_helper frappe serve --noreload --port <internal>
```

### `target` is `"web"`, and the others are not merely unimplemented

Every other long-running frappe process reaches the code you would want to stop
in through a `fork` that debugpy does not follow.

`rq.Worker.work()` forks a work horse per job (`rq/worker.py`), and the job
executes in the fork. debugpy does not patch `os.fork`: the child inherits a
trace function through the copied address space but has none of pydevd's I/O
threads and opens no connection of its own, so a breakpoint in job code never
fires while the debugger reports a healthy session on the parent. `bench
schedule` enqueues through the same machinery.

`preflight` therefore refuses any other `target` rather than accepting one that
would appear to work.

### The port

One published port, and only when enabled, so decision #16's default of two
still holds. It is bound to `127.0.0.1` for the same reason as MariaDB, only
more so: a debug port executes arbitrary code by design. `preflight` checks it
is free alongside the other two, and refuses a value that collides with them.

`debugpy.listen()` spawns **its own adapter process inside the container**,
which is what holds the port. An editor connects straight to it; no adapter runs
on the host.

**`pathMappings` is not optional.** The editor sees host paths and the
interpreter reports container paths; without the mapping, breakpoints bind to
nothing and never fire.

`render` writes the configuration into `<project>.code-workspace` when
`[debug].enabled` (§4.2), so it is not hand-written:

```json
{ "type": "debugpy", "request": "attach",
  "connect": { "host": "127.0.0.1", "port": 5678 },
  "pathMappings": [ { "localRoot": "${workspaceFolder:<project>}/bench/apps",
                      "remoteRoot": "/workspace/bench/apps" } ] }
```

**The folder variable must be scoped.** A launch configuration living in a
workspace file has to name its folder — `${workspaceFolder:<project>}` — or the
variable does not resolve, and an unresolved `localRoot` fails exactly the way
a missing mapping does: silently.

## 11. Shared-VPS hosting

What decisions #40–#51 add. The admin and developer procedures are in
[vps.md](vps.md).

### Behind a TLS-terminating proxy

Caddy terminates TLS and forwards plain http to the environment's nginx, so
nginx's `$scheme` is `http` while the browser is on `https`. Two things read
the scheme:

- **Frappe**, through `X-Forwarded-Proto`, when it builds URLs.
- **Frappe's `socketio.js`**, which compares the hostname of `Host` with that
  of `Origin` before accepting a connection, then authorises document
  subscriptions by calling `Origin + path` from the node process. Read in a
  v13-beta tree; v12 is assumed to behave the same until item 19 proves it.

`nginx.site.conf.tmpl` therefore maps `$http_x_forwarded_proto` to
`$erp_sandbox_forwarded_proto`, falling back to `$scheme` when nginx is the first
hop, and builds both `X-Forwarded-Proto` and `Origin` from it. Behind Caddy the
socketio callback goes to the public `https://` URL, which needs the bench
container to resolve and reach the VPS's own public address. **Realtime behind
Caddy is unverified** until a document updates live in a second tab through
`https://` (testing.md §1, item 19).

### `erp-sandbox proxy-snippet`

Prints a Caddyfile block for the environment: the `public_url` hostname,
`reverse_proxy` to `http_probe_host:ports.http`. `http_probe_host` is the bind
address, or `127.0.0.1` when the bind is `0.0.0.0`. Caddy's `reverse_proxy`
already sets `X-Forwarded-For` and `X-Forwarded-Proto`, passes `Host` through
and proxies websockets, so the block needs nothing else. Printed, never
installed (#43): `/etc/caddy` belongs to the VPS. Not a generated file, so not
in `render --list`.

### The allocation registry

`/etc/erp-sandbox/registry.toml`, or the path in `ERP_SANDBOX_REGISTRY` (#50). One
`[users.<login>]` table per Linux user, with `ports` and `hosts` lists. erp-sandbox
only reads it.

- **Preflight** checks every published port (`http`, `mariadb`, `debug.port`
  when enabled) and the `public_url` hostname: assigned to the current user is
  ok, assigned to another user fails, unassigned warns.
- **A port or host listed under two users** makes the registry itself invalid.
- **Absent** — the laptop case — is a single ok line and no checks.
- Keyed by login name from `pwd.getpwuid(os.getuid())`, so it follows the
  account, not the environment directory.

### Engine checks

- **Rootless Docker is refused** by preflight (`docker info` lists `rootless`
  in `SecurityOptions`). `compose.docker.yml` sets `user: UID:GID`, which
  rootless Docker remaps into the subuid range, so bind-mounted files would
  land owned by a uid that is not the developer's.
- **Lingering** is checked under podman, only for an environment with a
  `public_url`: without it, rootless containers stop at the user's last logout.
  On a laptop that is expected behaviour, so it is not warned about.

