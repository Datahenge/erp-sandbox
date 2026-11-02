# Testing and Verification

## 0. `erp-sandbox selftest` — the config layer, on every run

```
erp-sandbox selftest              # or, from a source checkout: python3 -m erp_sandbox selftest
```

Stdlib only: it needs neither a container engine nor a network, which is what
makes it cheap enough to run constantly. **The suite prints its own case count
when it passes** — run it rather than trusting a number written here.

| Group | What it proves |
|-------|----------------|
| Rejection | Bad configurations are refused **for the stated reason** — a case that is rejected with the wrong message fails, which is the part that matters. Covers the ssh/`requires_credentials` contradiction, a missing `[credentials]` entry, the pre-#24 key name, a non-remote `repo`, frappe not first, duplicate apps, port collisions, an out-of-range port, a bad seed mode, `sql_dump` without its `encryption_key`, unfilled `FILL-ME`, malformed TOML, a missing `config.toml`, an https URL with the ssh colon left in, a `[credentials]` table keyed by anything other than a host, `blobless` on an app that needs a credential, `blobless` given something that is not a boolean, a `[ports].bind` that is not an IPv4 address, and a `public_url` with a path or over plain http |
| Acceptance | All four remote spellings load — scp-style ssh, `ssh://`, https, https with a user (decision #24) |
| Round trip | The sample renders **with no git credential in the environment**; a second render finds no drift; a hand-edited generated file *is* detected as drift |
| Fetch: policy | `fetch_apps.py` decides correctly against a bare repo built in a temp directory — clones what is absent, leaves what is current, moves between a branch and a tag, and **refuses** rather than checking out over a dirty tree, over a remote pointing elsewhere, or over a directory it did not create. One case asserts the developer's edit is still on disk after a refusal. Two more cover `blobless` (decision #31) against a `file://` origin: the filter reaches `git` and is recorded on the remote, and a clone without the key is not partial |
| Fetch: secrecy | git output is redacted before printing; the askpass helper names variables rather than values; an app needing no credential is given none; prompts are disabled so an unattended build cannot hang; a private https repo with no credential produces a hint naming the cause |
| CLI | every verb parses, every verb is wired to a handler, `bench` runs inside the named container, the engine overlay is applied on top of the base, and the environment is found from a nested directory. `mariadb` gets four of its own: a query reaches the client, the password travels by environment rather than argv, the pager is applied only to a human session, and a site with no `db_name` is refused |
| Guard | the edit guard (decision #35): the settings file is **strict JSON** — parsed with `json.loads`, not the JSONC reader, so adding the usual header is caught — the hook matches `Edit|Write`, and its command is an absolute path rather than a bare `erp-sandbox` that a missing PATH entry would make inert. Then the decisions: refuses each generated path, allows `config.toml`, `CLAUDE.md`, app source and a developer's own file under `conf/`, and **allows everything on every error path** — junk on stdin, a payload with no `file_path`, a file in no environment, and a `config.toml` that does not parse. One case asserts the generated list is derived with no secret in the environment |
| Shared VPS | decisions #40–#51: http is published on `127.0.0.1` by default and a `0.0.0.0` bind is still probed on loopback; every service carries the restart policy; `public_url` becomes `host_name` only when set, with valid JSON either way; nginx builds `Origin` and `X-Forwarded-Proto` from the proxy's header; `proxy-snippet` prints nothing without a `public_url` and the loopback upstream with one; the registry passes an owner, refuses another user and an unlisted user, is silent when absent, and is itself refused when one port has two owners; rootless Docker is refused; `init` puts a bare name under `~/erp-sandboxes/`, refuses a non-empty directory without writing into it, and accepts one with `--here` |
| Frappe series | decisions #55–#57: `init --frappe 16` writes a config that validates, with every v16 pin, the `version-16` refs and v16's empty constraint seed; the default series leaves the v12 sample untouched; the image's Python and bench-CLI installer come from the series; v16 has no legacy installer and its modern one carries `flit_core`; each series' own defaults draw no warning, while a v12 ref or an out-of-range MariaDB under v16 does; a leftover `python`, a Node from another series, an impossible wkhtmltopdf/Debian pair and a too-old bench are refused; `init --frappe` refuses an unanalysed series before writing anything |
| Layout | every relative path the compose file names — the bench build context included — was actually materialized into the environment |
| Hygiene | `.env` is written `0600` and carries no git credential; no fixture holds an unexplained placeholder; `requires-python` in `pyproject.toml` and `MIN_PYTHON` in `preflight.py` agree |
| Guards | `preflight` still re-exports the names other modules import from it — `ruff --fix` deleted that block once and every other case still passed; and, in a source checkout, `core.hooksPath` points at `tools/` so the pre-commit hook actually runs |

The broken configurations live in `erp_sandbox/fixtures/` and in the `MUTATIONS`
list; `erp_sandbox/fixtures/README.md` explains why they are split that way.

The fetch cases run against a local bare repository, never the network. A real
clone needs a credential and a remote host, which makes it a poor dependency for
a suite meant to run constantly; what is worth guarding is the decision-making
around the clone, and that is fully testable offline. The network paths — ssh
and https + PAT — are exercised by hand.

**`erp-sandbox doctor` does not call this, and the pre-commit hook does.** The
hook (`tools/pre-commit`, installed by `tools/install-hooks.sh`) runs
`compileall`, `selftest`, and a staged-secret grep, so the suite runs on every
commit to the tool. `doctor` deliberately does not: it diagnoses an
*environment*, and an installed erp-sandbox has no source tree to test.

## 1. Acceptance checklist

The environment is done when all of these pass.

| # | Check | Notes |
|---|---|---|
| 0 | `erp-sandbox selftest` passes | **verified 2026-08-23** — 122 cases that day; the suite prints the current count |
| 1 | `erp-sandbox render --check` is clean | **verified 2026-08-23** — 13 generated files match |
| 2 | `erp-sandbox build` completes from scratch, unattended | **verified 2026-08-23** — `erp-sandbox init` to a working site in **11m05s**, eight steps, single pass, no intervention |
| 3 | All four services healthy under **podman-compose** | **verified** — `erp-sandbox doctor` clean on two environments at once |
| 4 | Same under **docker compose** | verified by a Datahenge colleague on Docker Engine; Brian's workstation has podman only |
| 5 | Site loads on `ports.http`; login succeeds | **verified** — HTTP 200, login returns a session that authenticates a second request |
| 6 | **Realtime works** — a doc updated in one tab reflects in another | **verified in three parts**: a document insert publishes to the `events` channel; the socketio node process is the single subscriber to it; a websocket upgrade through nginx answers HTTP 101. Two browser tabs remain the only thing untried |
| 7 | A background job runs | **verified** — a job enqueued on the `short` queue reached `finished` and its effect is in the database, under `rq 1.8.1` against v12's pinned `0.12.0`. 4 workers online. The site scheduler is disabled, which is frappe's default and `doctor` now reports |
| 8 | A print format renders to PDF | **verified** — `frappe.get_print(as_pdf=True)` on a real document: 12.5 KB, `%PDF-1.3`, fonts embedded |
| 9 | Editing a `.py` on the **host** is picked up by the container | **verified** — a `.py` written on the host imports inside the container immediately, twice running |
| 10 | Files created **inside** the container in the bind mount are owned by the host user | **verified** — `doctor` writes a file inside the container and checks its owner |
| 11 | `git status` inside `bench/apps/<app>` works from the host | **verified** |
| 12 | `down` then `up` preserves the database | **verified 2026-08-23.** `doctor`'s Database group re-proves the grant this depends on |
| 13 | `erp-sandbox doctor` exits 0, and non-zero when a container is stopped deliberately | **verified both ways** — 0 healthy, 2 with a container stopped |
| 14 | No PAT in generated files, in `podman image history`, or in any `.git/config` | **verified** — token absent from image history, image env, and every file under `bench/` |
| 15 | The genericity grep finds no client string in any committed file | **verified** — `doctor`'s Genericity group |
| 16 | **A developer completes onboarding from the documentation alone** | see §3 |
| 17 | **`[debug]` gives a working breakpoint** in the running site from an editor | **partly verified 2026-08-23.** Verified mechanically: one web process rather than a reloader pair, `--noreload` on the generated line, the port published on loopback, the in-container adapter answering a DAP `initialize` with `supportsConditionalBreakpoints`, HTTP 200 still served, and `doctor`'s four Debug checks green. **Not verified: an actual breakpoint hit from a real editor.** Claude's hand-rolled DAP client never completed `attach`, and the same client failed identically against a trivial debuggee — so the gap is in the client, not the environment. Needs one attach from VSCode |

Items 10 and 14 are the most likely to fail first; item 8 is the most likely to
fail during the image build.

### Frappe v16 (decisions #55–#57)

**None verified yet.** `selftest` covers the configuration; only a real build
covers the image.

| # | Check | Notes |
|---|---|---|
| 26 | `erp-sandbox init --frappe 16` then `erp-sandbox build` completes unattended | not run. First risks: `libharfbuzz-subset0` and `default-libmysqlclient-dev` names on bookworm, mysqlclient's source build, `flit_core` under `--no-build-isolation` |
| 27 | The image uses the base image's Python 3.14, not Debian's | not run. The build log says "base image provides python 3.14" |
| 28 | Site loads, login works, realtime works (item 6's checks) | not run |
| 29 | A print format renders to PDF through wkhtmltopdf `0.12.6.1-3` | not run |
| 30 | `erp-sandbox doctor` is clean, including the image-label check against the series' Python | not run |
| 31 | An existing v12 environment rebuilds after this work | not run. Re-rendering changes it: `.env` gains `BENCH_CLI_PINS`, `gunicorn_workers` leaves `common_site_config.json`, and the Dockerfile changed, so the image step rebuilds. A `python` line in its `config.toml` must be deleted first |

When 26–30 pass, fill in `verified` in v16's `known_versions.py` entry.

### Shared VPS (decisions #40–#51)

Run on a real VPS under Docker, the default engine (#45). **None verified yet.**

| # | Check | Notes |
|---|---|---|
| 18 | Two environments, built under two Linux accounts, run at once | not run |
| 19 | Each answers over `https://` at its own subdomain with a valid certificate, and **realtime works through Caddy** — a document updated in one tab reflects in another | not run. The socketio `Origin` callback behind TLS is the open question (technical.md §11) |
| 20 | Logged into both sites in one browser, neither session replaces the other | not run |
| 21 | An email link or webhook payload carries the `public_url` | not run |
| 22 | A third party receives an outbound call, and an inbound call reaches `/api/method/...` | not run |
| 23 | `ports.http` is not reachable from outside the VPS | not run — test from another machine, since Docker bypasses ufw |
| 24 | After a VPS reboot, both stacks are back without anyone logging in | not run. Docker by restart policy; podman also needs lingering and `podman-restart.service` (technical.md §1) |
| 25 | `erp-sandbox build` on Docker: `compose run --rm` against the bench service is not refused over its restart policy | not run. Verified for podman-compose 1.6.0 by reading its source; Docker Compose is not installed on Brian's workstation |

## 2. `erp-sandbox doctor` checks

| Group | Checks |
|-------|--------|
| Host | **Python >= 3.11**; engine + compose version; git; free disk; uid/gid match the bind mounts |
| Config | `config.toml` parses and validates; `erp-sandbox render --check` shows no drift; every `env:` secret resolves |
| Images | bench image exists and its build-args match the current TOML; mariadb/redis/nginx present at pinned tags |
| Containers | all four running; healthchecks passing; restart-loop detection |
| Connectivity | mariadb answers on the published port; redis answers internally; nginx returns HTTP 200; `/socket.io` upgrades to a websocket |
| Layout | every relative path `docker-compose.yml` names exists in the environment — the bench build context is the one that was missed |
| Bench | `bench version` lists every expected app at the expected ref; the site exists in `sites/`; `bench/env` is the *container's* venv and must not be activated from the host; `pip check` inside `bench/env`; the scheduler's state; and whether each app's clone on disk is the kind `config.toml` asked for (decision #31's `blobless`), which is a host-side check and answers whether or not the stack is running |
| Database | the site user is granted at `'%'` only, so a container's changing address cannot lock it out |
| Ownership | a file created **inside** the container in the bind mount lands as the host user — the single most important check under rootless podman |
| Secrets | `config.toml` and `.erp-sandbox/` are gitignored in *this developer's* project |
| Genericity | no client-specific string in the tool's committed tree |

Eleven groups, **41 checks** in a healthy environment of four apps on
2026-08-23. The count moves with what applies — a stopped stack skips checks
that need a running container, and some are one per app — so treat it as an
observation, not a contract. Run `erp-sandbox doctor`
rather than trusting this number.

**Two checks report at `ok` even when they find something**, and both are
deliberate: `pip check`'s conflicts (decision #30 makes them an accepted
outcome) and the scheduler being disabled (frappe's default). Raising either
would leave a healthy environment permanently at exit 1 — the "warning that
fires on the normal case" the onboarding test already caught once.

The scheduler check is worth knowing about: `doctor` reports that the site
scheduler is **disabled**, which is frappe's default and which erp-sandbox does not
change. Enabling it on a site restored from client data would run that client's
scheduled jobs for real.

**Two genericity questions, deliberately split.** The check that matters to a
*tool developer* is whether a client value reached the committed tool tree; the
check that matters to a *user* is whether their own `config.toml` and
`.erp-sandbox/` are committed. An installed erp-sandbox has no source tree.

Exit codes: `0` healthy, `1` degraded, `2` broken.

## 3. The onboarding test

The one test that validates the actual product. Run the whole flow from
scratch, in a clean directory, following **only** [setup.md](setup.md) and what
it links to. The reader starts at the README and is sent there by its first
table.

Anything that requires knowledge not in those documents is a bug in the
documentation. Two things it must now cover that a builder never needs: logging
in after the build, and what to do when the tool is not yet installed.

Ideally performed by someone who did not build the tool. Brian running it on his
own machine proves less, because he cannot un-know the design.
