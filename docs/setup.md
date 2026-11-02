# Setup — first run

From nothing to a site you can log into. **About 30 minutes**, 11 of which is
the build running unattended.

Do this once per client environment. Day-to-day work is in
[using.md](using.md).

---

## 1. Check your machine — 2 min

| Need | Check | Expect |
|---|---|---|
| Linux | `uname -s` | `Linux` — no macOS, no WSL2 |
| Python 3.11+ | `python3 -V` | `3.11` or newer |
| `uv` **or** `pipx` | `uv --version` / `pipx --version` | either one — see step 2 |
| podman **or** Docker | `podman --version` / `docker --version` | either one |
| its compose | `podman-compose --version` / `docker compose version` | matching the above |
| `git` | `git --version` | any recent |
| ~20 GB free | `df -h .` | images, a bench tree, a database |

The container images bring their own Python, Node and wkhtmltopdf. Your host
Python only runs `erp-sandbox` itself.

## 2. Install erp-sandbox — 2 min

**Use `uv tool install`** — it picks an interpreter that satisfies the tool's
requirements:

1. If `uv` is missing, install it
   ([Astral's instructions](https://docs.astral.sh/uv/getting-started/installation/)):
   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   Open a new shell afterwards, so `~/.local/bin` is on `PATH`.
2. Install erp-sandbox:
   ```sh
   uv tool install git+https://github.com/Datahenge/erp-sandbox.git
   erp-sandbox --version
   ```
   - **`sandbox` is a short alias** for `erp-sandbox`: `sandbox build` is the
     same command. The docs spell it out in full.

`pipx` also works:

```sh
pipx install git+https://github.com/Datahenge/erp-sandbox.git
```

- **`pipx` installs under the interpreter `pipx` itself runs on.** If that is
  older than 3.11 the install fails with `requires a different Python`,
  followed by a misleading `Cannot determine package name from spec`. The spec
  is fine.
- **Fix:** name an interpreter — `pipx install --python python3.13 …` — or
  install pipx from your distribution (`sudo apt install pipx`) so it runs
  under the system Python, and remove any pip-installed copy shadowing it on
  `PATH`.
- **Not `pip install`.** Debian and Ubuntu mark the system interpreter
  externally managed (PEP 668), and pip refuses.
- **Upgrading later:** `uv tool upgrade erp-sandbox`. One installed tool
  serves every environment on the machine, so it changes all of them at once —
  see [using.md](using.md#upgrading-erp-sandbox).

## 3. Create the environment — instant

```sh
erp-sandbox init acme-dev
cd ~/erp-sandboxes/acme-dev
```

- **A bare name goes under `~/erp-sandboxes/`** (#51). Give a path —
  `./acme-dev`, `/srv/acme-dev` — to put it elsewhere.
- **`init` refuses a directory that already has files in it.** Run
  `erp-sandbox init --here` inside it if it really should become an environment.
- **On a shared VPS**, the admin allocates your ports and subdomain first —
  see [vps.md](vps.md).

```
~/erp-sandboxes/acme-dev/
├── config.toml              # the only file you edit
├── CLAUDE.md                # yours; notes for this client
├── .erp-sandbox/
│   └── secrets.env          # 0600; passwords and any git token
└── .gitignore               # knows what is generated
```

## 4. Edit `config.toml` — 10 min

The single input. It is short and commented; the sample explains every field.

- **Unedited, it builds a stock ERPNext v12** — a quick way to prove the
  machine works. Skip to step 6.
- **For a client environment**, these are the parts you change:

```toml
[project]
name     = "acme"                 # container prefix and compose project name
site     = "site_acme1"           # the Frappe site name

[ports]
http     = 8101                   # the URL you open
mariadb  = 3316                   # for DBeaver / DataGrip / mysql

[stack]
frappe_version = 12               # must be a series the tool supports

[[apps]]                          # ordered: clone order is install order
name = "frappe"                   # frappe must be first
repo = "https://github.com/frappe/frappe.git"
ref  = "version-12"
requires_credentials = false
install = true
```

Three things to know:

- **Both ports must be free**, and each environment needs its own pair. See
  [Running more than one environment](using.md#running-more-than-one-environment).
- **`frappe_version` must be one the tool knows.** Supporting a series means
  reading its own `requirements.txt`; that analysis is recorded in
  `erp_sandbox/known_versions.py`. An unknown version is refused rather than
  attempted on guesswork.
- **frappe and erpnext are cloned `blobless`**: full history, old file
  contents fetched on demand. Remove the line for a full clone — detail in
  [technical.md](technical.md) §3.

## 5. Set up private repositories

Chosen per app by `requires_credentials`.

**ssh** — `requires_credentials = false`. Your own key authenticates; the tool
supplies and stores nothing. Run `ssh-add` first: clones use `BatchMode=yes`,
so a missing key fails in seconds rather than hanging an unattended build.

```toml
repo = "git@github.com:acme/erpnext.git"
requires_credentials = false
```

**https + token** — `requires_credentials = true`, plus a
`[credentials.<host>]` entry keyed by **hostname**, so one token covers every
app on that host.

```toml
repo = "https://github.com/acme/erpnext.git"
requires_credentials = true

[credentials."github.com"]
username = "your-username"
token    = "env:GIT_PAT"
```

**Your token never enters a container, an image layer, or a build argument.**
Cloning happens on the host, the token reaches `git` through `GIT_ASKPASS` for
one command, and `.git/config` is checked afterwards to prove none was written.

## 6. Fill in `.erp-sandbox/secrets.env`

```sh
$EDITOR .erp-sandbox/secrets.env
```

- `DB_ROOT_PASSWORD` and `ADMIN_PASSWORD` are required. **Remember
  `ADMIN_PASSWORD` — it is how you log in at step 8.**
- `GIT_PAT` only if an app uses https + token.
- Every `env:NAME` in `config.toml` is looked up in your shell first, then
  here, so you can override for a single run.
- The file is `0600` and gitignored.

## 7. Build — 11 min, unattended

```sh
erp-sandbox preflight     # host, config, ports — before anything is created
erp-sandbox build         # everything
erp-sandbox doctor        # is it healthy, and if not, why
```

`build` runs eight steps in order:

| Step | What |
|---|---|
| `preflight` | validate the host and `config.toml` |
| `render` | `config.toml` → `.env`, compose files, nginx conf, Procfile, site config |
| `fetch` | clone each app, host-side, at its configured ref |
| `image` | build the bench image at the versions the config names |
| `infra` | start MariaDB and Redis, wait for their healthchecks |
| `bench` | create `bench/env`, install every app, build assets |
| `site` | create the site and seed the database |
| `start` | bring up the whole stack |

- **It resumes.** Fix a failure and continue with `erp-sandbox build --from bench`
  rather than starting over — the image step alone is most of the time.
  `--only <step>` runs one step; `--dry-run` prints the plan.
- **`site` is the only destructive step.** It refuses to touch an existing site
  without `--force`, which drops and recreates the database.
- Allow longer than 11 minutes on a slow network or a cold image cache.

## 8. Log in

1. Open `http://localhost:<ports.http>` — `http://localhost:8101` with the
   sample's ports.
2. Username: **`Administrator`**
3. Password: the **`ADMIN_PASSWORD`** you set in step 6.

- If you seeded from a dump, that is **not** your password: a restored site keeps
  the dump's own `Administrator`. See
  [using.md](using.md#resetting-the-administrator-password).

You do not need to use the site name in the URL. nginx accepts any host and the
site is configured as the default.

If the page does not load, run `erp-sandbox doctor` and see
[troubleshooting.md](troubleshooting.md).

## 9. Next

- Day-to-day work, editor setup, debugging: [using.md](using.md)
- Something broken: [troubleshooting.md](troubleshooting.md)
