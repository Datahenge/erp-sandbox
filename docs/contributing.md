# Contributing to erp-sandbox

For working on `erp-sandbox` **itself**. To *use* it — set up an environment and
develop in it — see [setup.md](setup.md) and [using.md](using.md).

---

## Setup

1. **Clone the source.**

   ```sh
   git clone https://github.com/Datahenge/erp-sandbox.git
   cd erp-sandbox
   ```

2. **Install editable.** The `-e` / `--editable` flag is required — see
   [Why editable](#why-editable) below.

   ```sh
   uv tool install --force -e .        # or: pipx install --force --editable .
   ```

3. **Confirm the install points at your checkout.**

   ```sh
   cd / && erp-sandbox --version
   ```

   ```
   erp-sandbox 0.1.1 (/home/you/erp-sandbox/erp_sandbox)      <- editable
   erp-sandbox 0.1.1 (~/.local/share/uv/tools/.../site-packages/erp_sandbox)   <- not
   ```

   The path must be inside your clone. If it is not, step 2 did not take.

   - **The version number cannot tell you this** — both installs report the
     same one. Only the path differs.
   - **Do not check it with `python3 -c "import erp_sandbox"`.** A `uv tool` or
     `pipx` venv is not on the system interpreter's path, so that fails with
     `ModuleNotFoundError` even when the install is perfect — and run from
     inside the clone it succeeds by finding the source through the working
     directory, whether or not anything is installed at all. It is wrong in
     both directions.

   **You do not upgrade an editable install** — `git pull` is the upgrade, and
   your next command runs the new code. `uv tool upgrade erp-sandbox`
   answers `Nothing to upgrade` and leaves the install editable, so it is
   harmless but pointless. The upgrade path in
   [using.md](using.md#upgrading-erp-sandbox) is for people who installed from the
   git URL.

4. **Install the git hooks**, once per clone.

   ```sh
   tools/install-hooks.sh
   ```

   This sets `core.hooksPath = tools`, so the hook is version-controlled rather
   than living in `.git/hooks/`. Verify with `git config --get core.hooksPath`.

5. **Run the suite.**

   ```sh
   erp-sandbox selftest
   ```

---

## Commands

| Command | What it does | Needs |
|---|---|---|
| `erp-sandbox selftest` | the whole suite: config layer, fetch logic, CLI, rendering | nothing — no containers, no network |
| `erp-sandbox --project <dir> doctor` | diagnose an environment from here | a built environment elsewhere |
| `uvx ruff check erp_sandbox/` | lint | nothing installed |

`selftest` needs neither a container engine nor a network, so run it constantly.

`ruff` is deliberately **not** a dependency of this package. Linting must never
become a prerequisite of a build.

---

## Rules

- **Never create an environment inside this checkout.** It holds source only.
  Run `erp-sandbox init` somewhere else, and drive it from here with
  `erp-sandbox --project <dir> <verb>`.
- **`erp-sandbox doctor` run in this checkout is supposed to fail** with "looked
  for a `.erp-sandbox/` directory". That refusal is the separation working
  (decision #32), not a bug.
- **`config.toml` is the source of truth.** Never hardcode a port, password,
  path, image tag, site name, app name, or repo URL into a script, template,
  Dockerfile, or compose file.
- **No client-specific string in any committed file.** Use placeholders
  (`example.com`, `acme`). Real values live only in `config.toml` and `local/`,
  both gitignored. The pre-commit hook and `doctor`'s genericity grep enforce
  this.
- **Bump the version in `erp_sandbox/__init__.py`, and nowhere else** (#37).
  `pyproject.toml` derives it with `dynamic = ["version"]`, so that one literal
  is the whole release number. `selftest` refuses a literal `version` in
  `[project]`.
- **Commit to `main`.** No branch by default.

The full set is in [CLAUDE.md](../CLAUDE.md).

---

## Why editable

Without `-e`, the installer copies the source into its own `site-packages`. The
`erp-sandbox` on your `PATH` is then a snapshot that stops matching your checkout
at your next commit.

Nothing reports this. Worse, the checks that need to read a source checkout
degrade silently:

| Check | What it does instead | Exit code |
|---|---|---|
| `selftest` Python-floor and git-hook cases | report "not a source checkout" | still 0 |
| `doctor` genericity grep — the enforcement half of decision #17 | reports "not applicable" | still 0 |

A suite that skips and passes is worse than one that fails. When this happened,
`selftest` reported 76 passing cases against the stale snapshot and 97 against
the source.

---

## Where documentation goes

| Kind of information | File |
|---|---|
| A decision Brian confirmed | [decisions.md](decisions.md) |
| A design choice or mechanism | [technical.md](technical.md) |
| A requirement or non-goal | [requirements.md](requirements.md) |
| Where the project is | `internal/status.md` — maintainers only, and nowhere else |
| What a failure taught, once fixed | `internal/lessons.md` — maintainers only |
| A user-visible change | [CHANGELOG.md](../CHANGELOG.md) |
| A rule constraining future work | [CLAUDE.md](../CLAUDE.md) |

One fact, one home. Cross-reference instead of restating.
