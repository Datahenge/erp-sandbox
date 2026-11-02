# Requirements

What the tool must do, and what it deliberately does not do.
Decisions that settled these are recorded in [decisions.md](decisions.md).

## 1. Hard requirements

1. **Docker Compose configuration.**
2. **ERPNext v12 is the first configuration, not a limit of the tool.** The
   whole toolchain — base image, Python, Node, wkhtmltopdf, bench, MariaDB,
   Redis, nginx — is pinned in `config.toml`. A v13 or v14 environment is a
   different TOML, not a different tool.
3. **Not production.** An environment serves development, integration testing,
   and preview and testing by power users. Production for the client is
   handled separately by other tooling, outside this repository.
4. **Source code editable on the host** with ordinary tools: VSCode, Claude
   Code, grep, git.
5. **Bind mounts preferred over named volumes**, to make requirement 4 true.
6. **Network cloning is the supported path.** The tool must never require the
   developer to already have the app repositories on disk. Every app is fetched
   from a remote named in the TOML.
7. **Host Python 3.11 or newer**, guaranteeing `tomllib` in the standard
   library so the renderer needs no third-party TOML parser. (The floor was
   3.12 until decision #18 was revised on 2026-08-23; `erp-sandbox` itself is
   installed with `pipx`/`uv`, per decision #27.)
8. **Linux only.** Docker or rootless podman.
9. **Laptop or shared VPS.** An environment runs on a developer's own machine,
   or beside other developers' environments on one Linux VPS, served over
   HTTPS at its own subdomain (#40–#51).

## 2. Developer prerequisites

A developer needs these before `erp-sandbox build` will run. All are verified by
preflight, which fails with the required value, the value found, and where it
looked.

| Prerequisite | Requirement | Why |
|---|---|---|
| OS | Linux, x86_64 | Requirement 8 |
| Python | **>= 3.11** | `tomllib` for the renderer — see §2.1 |
| Container engine | Docker **or** rootless podman, with Compose | Requirement 1 |
| git | any current version | Apps are cloned host-side |
| Network access | to the git hosts named in the TOML | Requirement 6 |
| Credentials | a PAT per private git host | Client apps are private repos |
| Disk | roughly 20 GB free | Images, app repos, database, built assets, node_modules |

### 2.1 Which distributions satisfy the Python floor

The interpreter version is the prerequisite most likely to block a new
developer, because it depends on their distribution rather than on anything
they chose. The floor is deliberately as low as the code allows, which is why
it was lowered from 3.12 to 3.11: `tomllib` arrived in 3.11 (PEP 680), and a
floor stricter than its own justification blocked developers for no gain.

| Distribution | System Python | Status |
|---|---|---|
| Debian 13 (trixie) | 3.13 | ready |
| Debian 12 (bookworm) | 3.11 | ready |
| Ubuntu 24.04 LTS | 3.12 | ready |
| Fedora 39+ | 3.12+ | ready |
| **Ubuntu 22.04 LTS** | **3.10** | **needs a newer Python** |

Any of the usual routes works — `deadsnakes` on Ubuntu, backports, `pyenv`, or
a distribution upgrade — and only the *host* interpreter is affected. Nothing
inside the containers changes: the bench still runs Python 3.9 for ERPNext v12.

**The floor bites through the installer, not through the code.** `uv tool
install` picks an interpreter that satisfies `requires-python`; `pipx` builds
each app's venv with the interpreter *pipx itself* runs under, so a pipx
installed under an older Python refuses with a message that names the
constraint but not the remedy. `pipx install --python python3.13 …` is the fix.
See [functional.md](functional.md) §1.

Two places state the floor and must not drift: `requires-python` in
`pyproject.toml` and `MIN_PYTHON` in `erp_sandbox/preflight.py`. `erp-sandbox
selftest` asserts they agree.

## 3. Out of scope

Stated explicitly so these are decisions rather than omissions.

- **macOS and WSL2.** Different bind-mount semantics and, on macOS, materially
  slower file I/O on a multi-gigabyte bench tree.
- **Production deployment.** This builds non-production environments only
  (§1.3).
- **Distributing client data.** The tool restores a database dump from a local
  path the developer supplies. It does not host, ship, or sanitize dumps.
  Obtaining a dump is an out-of-band process. Restoring real client data onto
  a VPS-hosted environment is in scope; neutralising the restored copy is
  Cofferdam's job, not erp-sandbox's (#48).
- **Managing the apps themselves.** The tool clones and installs apps at a
  given ref. Branching, merging, and releasing them is ordinary git work.
- **A host-side Python interpreter or virtualenv.** erp-sandbox installs no Python
  on the host beyond the one it runs under, and builds no second venv there.
  The only venv is `bench/env`, built inside the container and bind-mounted for
  reading (#22). A host interpreter is not needed for editor analysis, which
  resolves imports from the bind-mounted sources without executing anything;
  it is not needed for debugging, which runs inside the container; and frappe's
  tests need a site and a database, so they run there too. An interpreter that
  cannot `import frappe` is worse than none, because selecting it makes every
  frappe symbol unresolved.

## 4. Why containerize at all

Any current Linux distribution has drifted far past what ERPNext v12 supports.
Brian's workstation is the worked example:

| Component | Host (Debian 13 trixie) | What v12 bench needs |
|-----------|-------------------------|----------------------|
| Python    | 3.13.5                  | 3.9.x                |
| Node      | v24.13.0                | v12-era toolchain    |
| MariaDB   | not installed on host   | 10.4.6               |

Keeping a Python 3.9 + Node 12 + MariaDB 10.4 stack alive directly on a modern
host is impractical, and asking each new developer to do it is worse. Containers
pin those versions; bind mounts keep the editing experience native.
