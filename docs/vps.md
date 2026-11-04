# A shared VPS

Several developers' environments on one Linux VPS, each served over HTTPS at
its own subdomain (decisions #40–#51). Each environment is built exactly as on
a laptop — [setup.md](setup.md) still applies. This page adds what the VPS
needs.

```
                 *.dev.example.com  (one wildcard DNS record)
                           │
                  ┌────────▼────────┐  :443, certificates automatic
                  │      Caddy      │  one per VPS, owned by the admin
                  └──┬───────────┬──┘
   bob.dev.example.com           sally.dev.example.com
       127.0.0.1:8101             127.0.0.1:8201
   ~bob/erp-sandboxes/acme-dev  ~sally/erp-sandboxes/acme-dev
```

---

## Read this first

- **No privacy between developers under Docker** (#45). Anyone in the `docker`
  group is effectively root: they can read every environment's database,
  `config.toml`, secrets and dump files. Rootless podman with one account per
  developer is the only setup that separates them.
- **These sites hold real client data** (#48), reachable from the internet
  behind the ERPNext login alone.
- **A restored copy behaves like production until it is neutralised.** Its
  stored credentials decrypt, its email accounts send, its scheduler runs, and
  its integrations call live endpoints. Neutralise it with
  [Cofferdam](https://github.com/Datahenge/cofferdam_app) (the Frappe app;
  library at [datahenge/cofferdam](https://github.com/datahenge/cofferdam))
  before anyone uses the site. erp-sandbox does none of this.
- **`developer_mode` is on** (#49). Error tracebacks are shown to whoever
  triggers them, third parties included.

---

## Order of work

Two roles, and each waits on the other once.

| # | Who | Does | Section |
|---|---|---|---|
| 1 | Admin | Prepare the VPS — once | [Once per VPS](#for-the-vps-admin--once-per-vps) |
| 2 | Admin | Create the account; allocate ports and a subdomain | [Once per developer](#for-the-vps-admin--once-per-developer), steps 1–3 |
| 3 | Developer | Install, configure, build; send the Caddy snippet | [For the developer](#for-the-developer), steps 1–4 |
| 4 | Admin | Install the snippet | [Once per developer](#for-the-vps-admin--once-per-developer), step 4 |
| 5 | Developer | Check it works | [For the developer](#for-the-developer), step 5 |

---

## For the VPS admin — once per VPS

1. **DNS.** Create one wildcard record, `*.dev.example.com`, pointing at the
   VPS. Adding a developer never needs another.
2. **Firewall.** Open 22, 80 and 443 only. Port 80 is for certificate issuance
   and the redirect to https.
3. **Engine.** Install Docker Engine with the compose plugin, following
   [Docker's instructions](https://docs.docker.com/engine/install/) for the
   distribution (the default, #45). Rootless podman is the alternative.
   - **Rootless Docker is not supported** — preflight refuses it.
   - Check: `docker compose version`.
4. **Caddy.** Install it as a system service, owned by no developer (#42):
   ```sh
   sudo apt install caddy
   ```
   - Packaged in Debian 12+ and Ubuntu 24.04+. Elsewhere, see
     [Caddy's instructions](https://caddyserver.com/docs/install).
5. **Point Caddy at the snippets.** Replace the contents of
   `/etc/caddy/Caddyfile` with:
   ```
   import /etc/caddy/erp-sandbox.d/*.caddy
   ```
   then:
   ```sh
   sudo mkdir -p /etc/caddy/erp-sandbox.d
   sudo systemctl reload caddy
   ```
6. **Create the registry** at `/etc/erp-sandbox/registry.toml`, mode 0644 (#50).
   ```toml
   [users.bob]
   ports = [8101, 3316]
   hosts = ["bob.dev.example.com"]

   [users.sally]
   ports = [8201, 3326]
   hosts = ["sally.dev.example.com"]
   ```
   - Keyed by Linux login name. List every published port: `http`, `mariadb`,
     and `debug.port` if debugging is on.
   - Preflight refuses a port or host assigned to someone else.
   - Real names and ports belong in the VPS's file, never in this repository.
7. **Install erp-sandbox for every user** (#54):
   ```sh
   sudo apt install pipx
   sudo pipx install --global erp-sandbox
   erp-sandbox --version          # path must be under /opt/pipx/
   ```
   - From PyPI, so the VPS runs published releases, not whatever is on `main`.
   - The environment goes in `/opt/pipx`, the commands in `/usr/local/bin`.
   - Needs the system Python to be 3.11 or newer: Debian 12+ and Ubuntu 24.04+
     qualify.
   - **Not `uv` here.** A uv-downloaded Python lands in root's home, which
     other users cannot read, and the command fails for them.
   - **Upgrading** changes every developer's tool at once. Tell them first:
     ```sh
     sudo pipx upgrade --global --pip-args='--no-cache-dir' erp-sandbox
     erp-sandbox --version          # confirm the new version
     ```
     - Keep `--no-cache-dir`. Without it pip can answer from its cached copy of
       the package index and report "already at latest version" for a release
       published minutes earlier.

## For the VPS admin — once per developer

1. **Create the account:** `sudo adduser bob`.
2. **Grant the engine.**
   - Docker: `sudo usermod -aG docker bob` — see the privacy warning above.
   - Rootless podman: `sudo loginctl enable-linger bob`, then as bob
     `systemctl --user enable podman-restart.service`. Without both, bob's
     stack stops at logout and does not come back after a reboot.
3. **Allocate** a port pair and a subdomain in the registry.
4. **Install the Caddy block** bob sends you (developer step 4, below):
   ```sh
   sudo cp bob.dev.example.com.caddy /etc/caddy/erp-sandbox.d/
   sudo systemctl reload caddy
   ```
   - Not `sudo -u bob erp-sandbox …`: erp-sandbox is installed in bob's
     `~/.local/bin`, which sudo's `secure_path` leaves off `PATH`.

## For the developer

1. **Create the environment** as in [setup.md](setup.md), skipping its
   install step — the admin has installed erp-sandbox for every user.
   `erp-sandbox init acme-dev` creates `~/erp-sandboxes/acme-dev/`.
   - **Do not install a second copy** with `uv tool install` or `pipx install`.
     `~/.local/bin` usually comes before `/usr/local/bin` on `PATH`, so your
     copy silently shadows the shared one. `erp-sandbox --version` shows which
     one runs; it should be under `/opt/pipx/`.
   - **Working on erp-sandbox itself** is the exception: an editable install
     ([contributing.md](contributing.md)) is meant to shadow it.
2. **Use the ports and subdomain the admin allocated** in `config.toml`:
   ```toml
   [ports]
   http    = 8101
   mariadb = 3316

   [site]
   public_url = "https://bob.dev.example.com"
   ```
   - `public_url` becomes Frappe's `host_name`, so emails and webhooks carry
     the public address.
   - Leave `[ports].bind` at its default of `127.0.0.1`. Caddy reaches it
     there.
3. **Build:** `erp-sandbox build`. Preflight checks your ports and host against
   the registry first.
4. **Send the admin your snippet:**
   `erp-sandbox proxy-snippet > bob.dev.example.com.caddy`.
5. **Check it**, once the admin has installed the snippet:
   1. `erp-sandbox doctor` on the VPS — healthy.
   2. Open `https://bob.dev.example.com` from your own machine. The
      certificate is valid and the login page loads.
   3. Log in as `Administrator`.
   4. From your own machine, `curl -m 5 http://<vps-address>:8101/` must
      **fail**. If it answers, `ports.http` is exposed — check `[ports].bind`.
6. **Restoring a client dump:** `erp-sandbox restore <dump> --force`
   ([using.md](using.md#restoring-a-sql-dump)), then neutralise it with
   Cofferdam before anyone uses the site.

### Working on the VPS

| Way in | Use it for |
|---|---|
| **VSCode Remote-SSH** | Recommended. Open `<project>.code-workspace` on the VPS; analysis and the debugger work unchanged |
| **Claude Code on the VPS** | Works as is. Run it inside the environment, as your own user |
| **SSHFS mount** | Avoid. The debugger's path mapping uses VPS paths, so breakpoints silently never fire; git and file watching are slow |

- **MariaDB GUI clients and debugpy** reach the VPS through a tunnel:
  `ssh -L 3316:127.0.0.1:3316 -L 5678:127.0.0.1:5678 bob@vps`.
- **Cloning over ssh** needs a key on the VPS or agent forwarding (`ssh -A`).
  Agent forwarding lets root on the VPS use your key while you are connected.

## Reboots

Every service comes back after a reboot (#47).

- **Docker** does it by itself (`restart: unless-stopped`).
- **Rootless podman** needs lingering and `podman-restart.service` —
  per-developer step 2. The podman overlay sets `restart: always`, the only
  policy that service starts. Preflight warns when lingering is off for an
  environment with a `public_url`.
- `erp-sandbox down` removes the containers, so a stack taken down stays down.
