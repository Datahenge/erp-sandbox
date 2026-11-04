# CLAUDE.md — erp-sandbox

A tool that builds a containerized ERPNext development environment from a single
`config.toml`. A developer **installs the tool** (`uv tool install` or `pipx`),
runs `erp-sandbox init`, edits one file, runs one command, and gets a working bench
with all source editable from the host. Cloning this repository is what someone
does to work on `erp-sandbox` itself — see [docs/contributing.md](docs/contributing.md).

**Repository:** `git@github.com:Datahenge/erp-sandbox.git`, branch `main`.

**Two roots, and confusing them is the likeliest bug.** The *package* is this
repository, installed. A *project* is one client environment, found by walking
up for `.erp-sandbox/`. The table is in the [README](README.md#two-roots--understand-this-first);
the code is `erp_sandbox/project.py`.

**No status here, on purpose.** It lives in `internal/status.md`
and nowhere else. A status line duplicated into this file goes stale first and
misdirects every session that reads it before anything else.

**`internal/` is a separate, private repository** (decision #53), checked out
inside this one and gitignored here. It holds `lessons.md`, `status.md`, and
`releasing.md` — how to build and publish to PyPI. It
exists only in a maintainer's checkout; if it is absent, there is no status or
history to read, and nothing in `docs/` depends on it. Commit to it with
`git -C internal`, never from this repository.

---

## Where things are

This file holds **rules and an index**. Beyond the orientation above, project
facts live in `docs/` — put them there, not here.

| Need | Read |
|---|---|
| Setting up an environment for the first time | [docs/setup.md](docs/setup.md) |
| Working in one day to day | [docs/using.md](docs/using.md) |
| Something in an environment is broken | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Running environments on a shared VPS — admin and developer | [docs/vps.md](docs/vps.md) |
| Working on `erp-sandbox` itself — setup, rules, why editable | [docs/contributing.md](docs/contributing.md) |
| What must be true, what is out of scope | [docs/requirements.md](docs/requirements.md) |
| **What has actually been agreed** | [docs/decisions.md](docs/decisions.md) |
| What the tool does, from the user's side | [docs/functional.md](docs/functional.md) |
| Architecture, `config.toml` schema, credentials, seeding | [docs/technical.md](docs/technical.md) |
| Acceptance checklist, `erp-sandbox selftest`, `erp-sandbox doctor` checks | [docs/testing.md](docs/testing.md) |
| **Where the project is, and what is next** | `internal/status.md` — the only status |
| How it was built, what broke, recommendations, retired risks | `internal/lessons.md` — **do not read by default** |
| What changed and when | [CHANGELOG.md](CHANGELOG.md) — rarely needed |

`docs/decisions.md` is the authority. If another document contradicts it, the
other document is wrong — fix it, and say so.

## Where to write new information

| Kind of information | Goes in |
|---|---|
| A decision Brian confirmed | `docs/decisions.md` — the table |
| Something Brian deferred, or a blocking unknown | `docs/decisions.md` — deferred / open items |
| A design choice or mechanism | `docs/technical.md` |
| A requirement or non-goal | `docs/requirements.md` |
| An observed fact about an existing environment | nowhere in this repo — it is client-specific (#17). Keep it in `local/`, which is gitignored |
| A rule that constrains future work | this file |
| An unconfirmed recommendation | `internal/lessons.md` — labelled as one and never as agreed |
| **What a failure taught**, once it is fixed | `internal/lessons.md`. The constraint it leaves behind goes in `technical.md`, present tense |
| Where the project is | `internal/status.md`, and nowhere else |
| A user-visible change | `CHANGELOG.md` |

One fact, one home. Cross-reference instead of restating — duplicated facts
drift, and then neither copy can be trusted.

---

## Rules — how to work

### Authority and recording

- **Never write a recommendation, judgment call, or inference into any document
  as though it were agreed, decided, authorized, or already rejected.** Label it
  as a recommendation and get Brian's confirmation before it is recorded as
  settled. This holds even when the recommendation is obviously sound —
  soundness is not authorization.
- Only Brian's confirmations go in the decisions table.
- When a decision changes, revise the entry and note what it superseded. Do not
  silently overwrite a decision's history.

### Committing

- **Commit to `main`.** Never branch, never ask which branch. This overrides
  any default habit of branching before committing.
- **Commit only when Brian asks.** The rule above governs where a commit lands,
  not whether to make one.
- **Never push.**

### Writing docs for Brian's team

Applies to reader-facing documents — `README.md` and `docs/setup.md`,
`using.md`, `troubleshooting.md`, `contributing.md`, `functional.md`,
`vps.md`. Not to `lessons.md`, which is narrative by design.

- **Concise and simple.** Steps, bullets, numbered lists, tables. Not a lot of
  prose.
- **A procedure is a numbered list**, one action per step, in the order it is
  performed.
- **A warning is a bullet**, not a paragraph. State the consequence in one
  sentence and link to the detail rather than restating it.
- Keep the *reason* only where a reader would otherwise undo the advice — a
  silent failure mode earns its sentence; background does not.

### Two kinds of document, and never mix them

Documentation splits in two, because a session that must read the whole history
to learn one current fact pays for that history in context it could have spent
on the work.

| | **Current facts** | **Lessons** |
|---|---|---|
| Answers | "what is true now" | "how we got here, and what it cost" |
| Files | `requirements.md` `decisions.md` `functional.md` `technical.md` `testing.md` `internal/status.md` | `internal/lessons.md`, `CHANGELOG.md` |
| Loaded | every session | only when *why* matters |
| Voice | present tense, no narrative | prose, as long as it needs to be |

**The test is not past-versus-present. It is: does this change what I do now?**
A pitfall that will bite again is a current fact, however it was found — state
it in `technical.md`, present tense, without the story. How long it took to
find, and what it looked like before anyone understood it, is a lesson. "It was
broken and we fixed it" is a lesson: nothing about it changes what happens next.

Rules that follow:

- **Never write history into a current-facts file.** No "used to be", no
  "originally", no "we then discovered". Correct the statement and move the
  story to `lessons.md`.
- **Never write a status line outside `status.md`.** One file, one home.
- When a lesson yields a rule, the rule goes in this file and the story stays in
  `lessons.md`. Cross-reference; do not restate.
- Deleting from a current-facts file is not losing it. `lessons.md`, the
  CHANGELOG and `git log` are all still there.

### Counts are observations, not contracts

- **Never write a bare count into prose as though it were a fact that holds.**
  The number of `selftest` cases, `doctor` checks, and generated files changes
  whenever anything is added, and a session spent correcting `50 / 31 / 54 / 55
  / 74 / 76 / 80` across five documents is a session not spent building.
- Where a count genuinely helps a reader, **date it and say what produced it** —
  `"<n> cases as of <date>"` — and point at the command that answers
  authoritatively. `doctor`'s count in particular moves with what applies: a
  stopped stack skips checks that need a container.
- The same goes for lists that mirror code: the `doctor` group names, the build
  steps, the CLI verbs. Prefer "run `erp-sandbox doctor`" to a table that has to be
  maintained in parallel with the thing it describes.

### This repository is source only

**No erp-sandbox runtime is written here — ever.** No `config.toml`, no
`.erp-sandbox/`, no `bench/`, `data/`, `conf/`, `images/`, `.env` or compose file
at this root. Environments live in their own directories outside the checkout,
and each carries its own `CLAUDE.md` for the people working inside it. Decision
#32. Running `erp-sandbox doctor` here is *supposed* to fail with "looked for a
`.erp-sandbox/` directory" — that refusal is the separation working, not a bug to
fix by scaffolding an environment on top of the source.

To act on an environment from here, pass **`erp-sandbox --project <dir> <verb>`**
rather than `cd`-ing into it. It is shorter, and it cannot leave the shell
pointing somewhere later commands will silently inherit.

- **Install the tool editable, and the hooks through `core.hooksPath`.** Both
  fail silently when done wrong — checks downgrade to "not a source checkout"
  and still exit 0. Setup and the full explanation:
  [docs/contributing.md](docs/contributing.md).
- **Inside an environment, `bench/apps/<app>/` are independent git
  repositories.** A `git status`, `git check-ignore` or `cat .gitignore` run
  from inside one answers about *that* repository, not this one. The shell's
  working directory persists between commands, so a single earlier `cd` is
  enough to make every later answer quietly wrong. **Use absolute paths when
  inspecting git state**, or `git -C`.
- **Generated files in an environment are disposable.** `erp-sandbox render` puts
  them all back. Never restore one by hand.

## Rules — what the code must do

### Configuration

- **`config.toml` is the source of truth.** Never hardcode a port, password,
  path, image tag, site name, app name, or repo URL into a script, template,
  Dockerfile, or compose file. It comes from the TOML via the renderer.
- **Generated files are never hand-edited.** Everything `erp-sandbox render
  --list` names is an output in the *environment*, overwritten on the next
  render; `erp-sandbox render --check` detects drift. Their canonical copies live
  in `erp_sandbox/assets/` and `erp_sandbox/templates/` inside the package (#27).
  Do not enumerate them here — the list moves.
- **Dependencies are few and deliberate.** `erp-sandbox` is an installed package
  (decision #27), so it *may* have dependencies — but the stdlib-only discipline
  that preceded it was doing real work, and the default answer is still the
  standard library. Adding one is a choice to justify, not a reflex. `tomllib`
  specifically is never replaced; that is what the 3.11 floor buys (#18).
- The base compose file stays inside the Docker Compose / podman-compose common
  subset; engine-specific syntax goes in the generated overlay.
- **Bind mounts over named volumes.** A named volume is acceptable only where
  the content is genuinely disposable and never hand-edited, and must be called
  out explicitly when proposed.

### Genericity

- **Nothing may assume the developer's machine is Brian's.** No hardcoded uid,
  gid, home directory, absolute path, or pre-existing repository on disk.
- **No client-specific string in any committed file** — scripts, templates,
  Dockerfiles, compose files, `config.sample.toml`, and the prose in `docs/`
  alike. Use placeholders (`example.com`, `acme`). Real client values live only
  in `config.toml` and `local/`, both gitignored. Decision #17.
- Brian's own name, "Datahenge", and his git handles are **not** client-specific
  and may be committed. Decision #21.

### Secrets

- **A PAT never enters a container, an image layer, or a build argument.** All
  cloning happens host-side; credentials reach `git` through `GIT_ASKPASS` for
  the duration of one clone, and remotes are scrubbed of the token afterward.
  A build arg would be visible in `image history` permanently.
- Secrets in `config.toml` default to `env:NAME` indirection, so the value lives
  in the developer's shell rather than in a file.
- Commit `config.sample.toml`; `config.toml` and `.env` stay gitignored.
- Never print a real password or token into a transcript, log, or document.
- **No secret on a command line** — the tool's, the engine client's, or one
  inside a container. Every one is in `ps` for every user. Pass secrets by
  environment or stdin; the mechanisms are in
  [docs/technical.md](docs/technical.md) §5, "No secret on a command line".
- **The bench container gets runtime secrets from `[bench].env_file` only**
  (#61). Never load the `[secrets]` file into a container.

### Subprocesses

- **A git started by the tool runs without git's repository variables.** Pass
  `env=project.git_env()` to every git subprocess, or go through
  `fetch_apps.Git` or doctor's `_git`, which do. Inherited from a git hook, they point the child at the
  outer repository's index. Mechanism: [docs/technical.md](docs/technical.md) §5.
