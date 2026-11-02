# erp-sandbox

Build a containerized ERPNext development environment from a single
`config.toml`. Install the tool once, run `erp-sandbox init`, edit one file, run
one command, and get a working bench with **all source editable from the host**.

Linux only, podman or Docker, no root required. The command is `erp-sandbox`,
or `sandbox` for short.

---

## Start here

| | |
|---|---|
| **Setting one up for the first time** | [docs/setup.md](docs/setup.md) — ~30 min, 11 of it unattended |
| Working in one day to day | [docs/using.md](docs/using.md) |
| Something is broken | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Running environments on a shared VPS | [docs/vps.md](docs/vps.md) |
| Working on `erp-sandbox` itself | [docs/contributing.md](docs/contributing.md) |

The short version, if you have done this before:

```sh
uv tool install git+https://github.com/Datahenge/erp-sandbox.git
erp-sandbox init acme-dev && cd ~/erp-sandboxes/acme-dev
$EDITOR config.toml .erp-sandbox/secrets.env
erp-sandbox build
```

Then open `http://localhost:<ports.http>` and log in as `Administrator` with
the `ADMIN_PASSWORD` you set.

Already installed? `uv tool upgrade erp-sandbox` — one tool serves every
environment on the machine ([upgrading](docs/using.md#upgrading-erp-sandbox)).

---

## What you get

Four containers, one HTTP port:

| Service | What it does | Reachable from the host |
|---|---|---|
| `nginx` | the only entry point — `/` to web, `/socket.io` to realtime | **yes**, `ports.http` |
| `bench` | honcho running the Procfile: web, socketio, scheduler, workers | no |
| `mariadb` | the site database | **yes**, `ports.mariadb`, on `127.0.0.1` |
| `redis` | cache, queue and socketio as logical DBs 0/1/2 | no |

Everything is **bind-mounted from your project directory** — `bench/apps/` the
cloned source, `bench/env/` the virtualenv, `data/mariadb/` the database. So:

- Edit a `.py` on the host and the reloader picks it up.
- `git status` works in every app.
- No `docker cp`, no rebuild to change a line.

You can run several client environments side by side, each with its own ports.

---

## What you need

Linux, Python 3.11+ on the host, podman (rootless) or Docker Engine with its
compose plugin, `git`, and ~20 GB free. The container images bring their own
Python, Node and wkhtmltopdf.

The full list, with a command to check each one, is
[step 1 of setup.md](docs/setup.md#1-check-your-machine--2-min).

---

## Two roots — understand this first

Almost every confusing moment with this tool comes from mixing these up.

| | **The package** | **A project** |
|---|---|---|
| What | `erp-sandbox` itself | one client's environment |
| Holds | templates, compose file, bench `Dockerfile` | `config.toml`, `.erp-sandbox/`, `bench/`, `data/` |
| Installed | once per machine, shared | created by you, as many as you like |
| Upgrades | replaced wholesale | never touched |

Every verb except `init` acts on **the environment containing your working
directory**, found by walking up for `.erp-sandbox/` — the same way `bench` finds
its bench. So `erp-sandbox bench console` works anywhere inside a project. Use
`--project DIR` to act on one from outside.

---

## Documentation

**Using the tool**

| | |
|---|---|
| First run, end to end | [docs/setup.md](docs/setup.md) |
| Day-to-day work, editor, debugging, teardown | [docs/using.md](docs/using.md) |
| Symptom → cause → fix | [docs/troubleshooting.md](docs/troubleshooting.md) |

**Working on the tool**

| | |
|---|---|
| Setup, commands, rules | [docs/contributing.md](docs/contributing.md) |
| What is decided, and why | [docs/decisions.md](docs/decisions.md) |
| What the tool does, from the developer's side | [docs/functional.md](docs/functional.md) |
| Architecture, `config.toml` schema, credentials, seeding | [docs/technical.md](docs/technical.md) |
| Requirements and non-goals | [docs/requirements.md](docs/requirements.md) |
| Acceptance checklist and the test suite | [docs/testing.md](docs/testing.md) |
| What changed and when | [CHANGELOG.md](CHANGELOG.md) |

---

## License

MIT — see [LICENSE](LICENSE). Copyright Datahenge LLC.

erp-sandbox is an independent project, built for ERPNext. It is not affiliated
with or endorsed by Frappe Technologies. ERPNext and Frappe are trademarks of
Frappe Technologies Pvt. Ltd.
