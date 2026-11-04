#!/usr/bin/env python3
"""
preflight.py — load, validate, and check.

Two jobs, deliberately in one file (technical.md section 8 lists no separate
config module):

  1. A library.  render.py and erp-sandbox doctor both need to parse config.toml and
     apply exactly the same validation rules.  Duplicating that logic is how
     two copies of a schema drift apart.
  2. A command.  ``erp-sandbox preflight`` runs every check in
     functional.md section 4 step 1 and exits non-zero if the environment
     cannot support a build.

Standard library only (decision #18).  There is no install step.
"""

# ---------------------------------------------------------------------------
# Risk 9: the interpreter check happens before anything else, including the
# import of tomllib, which does not exist before 3.11.  An ImportError here
# would be a confusing way to learn that your Python is too old.
# ---------------------------------------------------------------------------
import sys

# Must stay equal to `requires-python` in pyproject.toml. If they drift, an
# install succeeds and then the tool refuses to run, or the reverse.
MIN_PYTHON = (3, 11)

if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "erp-sandbox requires Python {}.{} or newer; this interpreter is "
        "{}.{}.{} at {}\n".format(
            MIN_PYTHON[0], MIN_PYTHON[1],
            sys.version_info[0], sys.version_info[1], sys.version_info[2],
            sys.executable,
        )
    )
    sys.stderr.write(
        "The tool reads its own configuration with tomllib, which entered the "
        "standard library in 3.11.\n"
        "If this is a pipx install, pipx used its own interpreter: reinstall "
        "with `pipx install --python python3.13 ...`, or use `uv tool install`, "
        "which picks a compatible interpreter itself.\n"
    )
    raise SystemExit(2)

import argparse
import ipaddress
import os
import pwd
import re
import shutil
import socket
import subprocess
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from erp_sandbox import known_versions, project
from urllib.parse import urlsplit

# Re-exported deliberately. render, fetch_apps, seed_db, doctor and cli all
# import these *from preflight*, which is the one module that is safe to import
# before the interpreter check above has run. Nothing in this file uses them,
# so F401 wants them gone -- and `ruff --fix` did exactly that once, breaking
# every other module while selftest still passed.
from erp_sandbox.project import (  # noqa: F401
    PACKAGE_ROOT,
    ProjectNotFound,
    config_path,
    find_project_root,
    state_dir,
)

# Free space needed for images, a bench tree, and a database.  A full v12
# bench with built assets is several GB before any client data lands in it.
MIN_FREE_GB = 20

# Once bench/env exists those are already on disk, and what is left to need
# space is growth: a rebuilt image layer, a restore (which needs its dump's size
# again while it decompresses), logs. Below this, warn; never fail.
MIN_FREE_GB_BUILT = 10

SEED_MODES = ("new_site", "sql_dump", "frappe_backup")

# What [debug].target may name. Only the web process is debuggable: every other
# long-running frappe process reaches the code you care about through a fork
# that debugpy does not follow, so a breakpoint there never fires (decision
# #29, technical.md section 10).
DEBUG_TARGETS = ("web",)

# Where nginx's port is published when [ports].bind says nothing (decision
# #46). Loopback, on a laptop and a VPS alike: on a VPS Caddy reaches it there,
# and Docker's own iptables rules sit ahead of ufw, so a port published on every
# interface is reachable from outside even when the firewall denies it.
DEFAULT_HTTP_BIND = "127.0.0.1"

# The VPS-wide allocation registry (decision #50). Owned by the admin, readable
# by everyone, and only ever read by erp-sandbox. Absent on a laptop. The variable
# exists so the registry can be pointed elsewhere -- by selftest, and by an
# admin who keeps it somewhere other than /etc.
REGISTRY_ENV = "ERP_SANDBOX_REGISTRY"
DEFAULT_REGISTRY = Path("/etc/erp-sandbox/registry.toml")

# Compose project names, and therefore the directories and container names
# derived from them, are restricted to this.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Frappe app names are imported as Python modules.
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# A Frappe site name becomes a directory under bench/sites/.
SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# [credentials] keys are hostnames. Requiring a dot is crude but it catches the
# mistake that actually happens -- keying the table by app name -- while still
# accepting every real git host. "localhost" is the one dotless name worth
# allowing, for a self-hosted forge reached through a tunnel.
HOSTNAME_RE = re.compile(r"^(localhost|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)$")

# One entry of [stack].pip_constraints: a package name and at least one version
# specifier, with no whitespace anywhere.
#
# The specifier is required. A bare name in a pip constraints file parses
# happily and constrains nothing, so "numpy" -- the obvious typo for
# "numpy<1.24" -- would otherwise be accepted and silently do nothing.
#
# The no-whitespace rule is not cosmetic: build_bench.py transports these as a
# space-separated argv element and relies on word splitting to put one per
# line. Relax it there before relaxing it here. It also rules out environment
# markers, which is a deliberate limit rather than an oversight -- see the
# message in the validator.
PIP_CONSTRAINT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"                       # package name
    r"(?:(?:===|==|!=|<=|>=|~=|<|>)[A-Za-z0-9][A-Za-z0-9._*+!-]*)"   # first specifier
    r"(?:,(?:===|==|!=|<=|>=|~=|<|>)[A-Za-z0-9][A-Za-z0-9._*+!-]*)*$"  # and any more
)


class ConfigError(Exception):
    """config.toml is unusable.  Carries every problem found, not just the first."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("{} problem(s) in config.toml".format(len(self.problems)))


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

def load_config(path):
    """Parse and validate.  Returns the raw dict; raises ConfigError."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError([
            "{} does not exist. Copy config.sample.toml to config.toml and "
            "edit it.".format(path)
        ])
    try:
        with path.open("rb") as handle:
            cfg = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(["{} is not valid TOML: {}".format(path, exc)]) from exc

    problems = validate(cfg)
    if problems:
        raise ConfigError(problems)
    return cfg


def validate(cfg):
    """Every rule the schema imposes.  Returns a list of human-readable problems."""
    # An unfilled placeholder would also trip half the format rules below --
    # FILL-ME is not a URL, not an app name, not a site name.  Reporting both
    # sets at once buries the one instruction that matters.
    placeholders = find_placeholders(cfg)
    if placeholders:
        return ["config.toml still has blanks to fill in:"] + placeholders

    problems = []

    def need_table(name):
        value = cfg.get(name)
        if value is None:
            problems.append("[{}] section is missing".format(name))
            return {}
        if not isinstance(value, dict):
            problems.append("[{}] must be a table".format(name))
            return {}
        return value

    def need_str(table, section, key, pattern=None, allow_empty=False):
        value = table.get(key)
        if value is None:
            problems.append("{}.{} is missing".format(section, key))
            return None
        if not isinstance(value, str):
            problems.append("{}.{} must be a string".format(section, key))
            return None
        if not value.strip() and not allow_empty:
            problems.append("{}.{} is empty".format(section, key))
            return None
        if pattern and value and not pattern.match(value):
            problems.append(
                "{}.{} = {!r} does not match {}".format(section, key, value, pattern.pattern)
            )
            return None
        return value

    def need_int(table, section, key, low=None, high=None):
        value = table.get(key)
        if value is None:
            problems.append("{}.{} is missing".format(section, key))
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append("{}.{} must be an integer".format(section, key))
            return None
        if low is not None and value < low:
            problems.append("{}.{} = {} is below the minimum of {}".format(section, key, value, low))
            return None
        if high is not None and value > high:
            problems.append("{}.{} = {} is above the maximum of {}".format(section, key, value, high))
            return None
        return value

    def need_bool(table, section, key):
        value = table.get(key)
        if value is None:
            problems.append("{}.{} is missing".format(section, key))
            return None
        if not isinstance(value, bool):
            problems.append("{}.{} must be true or false".format(section, key))
            return None
        return value

    def optional_bool(table, section, key, default):
        """A key that may be omitted. Absent is the default; present must be a bool."""
        if key not in table:
            return default
        value = table[key]
        if not isinstance(value, bool):
            problems.append("{}.{} must be true or false".format(section, key))
            return default
        return value

    # --- [project] ---------------------------------------------------------
    project = need_table("project")
    need_str(project, "project", "name", NAME_RE)
    need_str(project, "project", "site", SITE_RE)
    need_str(project, "project", "timezone")

    # --- [ports] : published to the host (decision #16) ---------------------
    ports = need_table("ports")
    http_port = need_int(ports, "ports", "http", 1, 65535)
    db_port = need_int(ports, "ports", "mariadb", 1, 65535)
    if http_port is not None and http_port == db_port:
        problems.append("ports.http and ports.mariadb are both {}".format(http_port))
    # Optional: absent is loopback (decision #46). An IPv4 address only -- it
    # becomes the host half of a compose `ports:` entry, where an IPv6 literal
    # needs bracketing that the two compose front ends do not agree on.
    if "bind" in ports:
        bind = ports["bind"]
        try:
            if not isinstance(bind, str):
                raise ValueError
            ipaddress.IPv4Address(bind)
        except ValueError:
            problems.append(
                "ports.bind = {!r} is not an IPv4 address. Use \"127.0.0.1\" "
                "(the default) to keep the site on this machine, or "
                "\"0.0.0.0\" to publish it on every interface.".format(bind)
            )

    # --- [internal_ports] : container network only --------------------------
    internal = need_table("internal_ports")
    seen_internal = {}
    # Only the three that share the bench container can collide. mariadb and
    # redis each listen in a container of their own (decision #57).
    for key in ("web", "socketio", "file_watcher", "mariadb", "redis"):
        value = need_int(internal, "internal_ports", key, 1, 65535)
        if value is not None and key in BENCH_CONTAINER_PORTS:
            seen_internal.setdefault(value, []).append(key)
    for value, keys in sorted(seen_internal.items()):
        if len(keys) > 1:
            problems.append(
                "internal_ports {} share port {}".format(
                    " and ".join(sorted(keys)), value)
            )

    # --- [erp-sandbox] ---------------------------------------------------------
    # Optional: an environment created before this field existed simply has no
    # opinion, and that is not an error.
    if "devstack" in cfg:
        # Decision #52. Not merged into [erp-sandbox]: silently ignoring it
        # would drop min_version, and accepting it would undo the clean break.
        problems.append(
            "[devstack] was renamed [erp-sandbox] (decision #52). Change the "
            "section header in config.toml; the keys under it are unchanged."
        )
    erp_sandbox_section = cfg.get("erp-sandbox")
    if erp_sandbox_section is not None:
        if not isinstance(erp_sandbox_section, dict):
            problems.append("[erp-sandbox] must be a table")
        else:
            required = erp_sandbox_section.get("min_version")
            if required is not None:
                problems.extend(check_min_version(required))

    # --- [stack] -----------------------------------------------------------
    stack = need_table("stack")
    for key in ("base_image", "node", "yarn", "wkhtmltopdf", "bench",
                "mariadb_image", "redis_image", "nginx_image"):
        need_str(stack, "stack", key)
    if "python" in stack:
        # Decision #57. Refused rather than ignored, like [devstack]: a value
        # nothing reads looks like a setting, and the next person edits it.
        problems.append(
            "stack.python was removed: the base image decides the Python, and "
            "the image build checks it is the one the Frappe series needs. "
            "Delete the line."
        )

    # Apps can need system libraries the base image has no way to predict --
    # a Frappe app wrapping systemd needs libsystemd-dev to build pystemd, and
    # the next app will need something else. The image cannot carry every
    # possibility, and hardcoding one app's need would make it un-generic
    # (decision #17), so the list is configuration like everything else.
    extra = stack.get("extra_packages", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        problems.append("stack.extra_packages must be a list of Debian package names")
    else:
        for name in extra:
            if not re.match(r"^[a-z0-9][a-z0-9+.-]*$", name):
                problems.append(
                    "stack.extra_packages entry {!r} is not a Debian package name. "
                    "It becomes an apt-get argument in the image build.".format(name)
                )

    # Ceilings pip may not resolve past while installing the apps (decision #36).
    # Optional, and absent is NOT an error: an environment created before this
    # key existed has no opinion, gets no constraints, and is warned by
    # constraints_warning() rather than refused. What it is not is a hidden
    # default -- whatever config.toml says, including nothing, is what runs.
    constraints = stack.get("pip_constraints", [])
    if not isinstance(constraints, list) or not all(isinstance(x, str) for x in constraints):
        problems.append(
            "stack.pip_constraints must be a list of pip constraint strings, "
            'e.g. ["numpy<1.24"]. An empty list means no constraints.'
        )
    else:
        for entry in constraints:
            if not PIP_CONSTRAINT_RE.match(entry):
                problems.append(
                    "stack.pip_constraints entry {!r} is not a package name followed "
                    "by a version specifier, with no spaces. Write \"numpy<1.24\", "
                    "not \"numpy < 1.24\" and not a bare \"numpy\" -- a constraint "
                    "without a specifier caps nothing. Environment markers and URLs "
                    "are not supported here.".format(entry)
                )

    # Decision #28: the tool refuses a Frappe series nobody has analysed.
    frappe_version = stack.get("frappe_version")
    if frappe_version is None:
        problems.append(
            "stack.frappe_version is missing. It selects the entry in "
            "erp_sandbox/known_versions.py that says what this Frappe series "
            "needs from the installer. Supported: {}.".format(known_versions.supported_list())
        )
    elif isinstance(frappe_version, bool) or not isinstance(frappe_version, int):
        problems.append("stack.frappe_version must be an integer, e.g. 12")
    elif not known_versions.is_supported(frappe_version):
        problems.append("stack." + known_versions.unsupported_message(frappe_version))
    else:
        # The registry knows what this series needs; config.toml must agree.
        known = known_versions.get(frappe_version)
        problems.extend(series_problems(stack, known))

    # --- [[apps]] ----------------------------------------------------------
    apps = cfg.get("apps")
    app_hosts = []          # (label, host) for apps that need a credential
    host_by_app = {}        # app name -> host, for diagnosing a mis-keyed table
    if apps is None:
        problems.append("[[apps]] is missing; at least frappe must be listed")
        apps = []
    elif not isinstance(apps, list) or not all(isinstance(a, dict) for a in apps):
        problems.append("[[apps]] must be an array of tables")
        apps = []
    elif not apps:
        problems.append("[[apps]] is empty; at least frappe must be listed")
    else:
        # Install order is clone order, and frappe is every other app's
        # dependency, so its position is not a matter of taste.
        first = apps[0].get("name")
        if first != "frappe":
            problems.append(
                "the first [[apps]] entry must be frappe, not {!r}; the array is "
                "clone and install order".format(first)
            )
        names = []
        for index, app in enumerate(apps):
            label = "apps[{}]".format(index)
            name = need_str(app, label, "name", APP_NAME_RE)
            if name:
                names.append(name)
                label = "apps.{}".format(name)
            repo = need_str(app, label, "repo")
            need_str(app, label, "ref")
            if "private" in app and "requires_credentials" not in app:
                problems.append(
                    "{}.private has been renamed to requires_credentials. The old "
                    "name asked whether the repository was private; the flag "
                    "actually says whether this clone needs a [credentials] entry, "
                    "which an ssh remote does not even when the repository is "
                    "private.".format(label)
                )
                needs_credentials = None
            else:
                needs_credentials = need_bool(app, label, "requires_credentials")
            need_bool(app, label, "install")
            # Optional, and false when absent. erp-sandbox init writes true for
            # frappe and erpnext, the two clones that cost the disk; an app
            # left without the key gets a full clone (decision #31).
            blobless = optional_bool(app, label, "blobless", False)
            if blobless and needs_credentials:
                problems.append(
                    "{}.blobless = true is refused for an app that needs a "
                    "credential. A blobless clone fetches file contents on "
                    "demand, at whatever later moment a command asks for one -- "
                    "long after the token has left the environment. `git log -p` "
                    "in that app would prompt for a password or fail. Set "
                    "blobless = false; an app small enough to be private is "
                    "rarely the one costing the disk.".format(label)
                )
            if repo:
                host, kind = repo_remote(repo)
                authority_problem = (_https_authority_problem(repo)
                                     if kind == "https" else None)
                # Recorded even when the URL has other problems: a mis-keyed
                # [credentials] table is diagnosed from it, and both mistakes
                # tend to be made in the same edit.
                if host and name:
                    host_by_app[name] = host
                if host is None:
                    problems.append(
                        "{}.repo = {!r} is not a recognised git remote; expected "
                        "https://host/path, ssh://user@host/path, or "
                        "user@host:path".format(label, repo)
                    )
                elif authority_problem:
                    problems.append("{}.repo {}".format(label, authority_problem))
                elif kind == "ssh" and needs_credentials:
                    problems.append(
                        "{}.repo is an ssh remote but requires_credentials is true. "
                        "A [credentials] token is presented over https only; ssh "
                        "authenticates with the developer's own key. Set "
                        "requires_credentials = false and make sure the key is "
                        "loaded before fetching.".format(label)
                    )
                elif needs_credentials:
                    app_hosts.append((label, host))
        duplicates = sorted({n for n in names if names.count(n) > 1})
        for name in duplicates:
            problems.append("app {!r} is listed more than once".format(name))

    # --- [credentials.<host>] ----------------------------------------------
    credentials = cfg.get("credentials", {})
    if not isinstance(credentials, dict):
        problems.append("[credentials] must be a table keyed by git host")
        credentials = {}
    for host, entry in credentials.items():
        section = 'credentials."{}"'.format(host)
        if not isinstance(entry, dict):
            problems.append("[{}] must be a table".format(section))
            continue
        need_str(entry, section, "username")
        need_str(entry, section, "token")
        if not HOSTNAME_RE.match(str(host)):
            hint = ""
            if host in host_by_app:
                # The mistake this catches: keying the table by the app it is
                # for rather than by the host it authenticates to. One PAT
                # commonly covers several apps on one host, which is why the
                # table is keyed the way it is.
                hint = (" That is an app name. Use the git host from its repo "
                        "URL, here {!r}.".format(host_by_app[host]))
            problems.append(
                "[{}] is not keyed by a git host.{} [credentials] is keyed by "
                "hostname so one entry covers every app on that host."
                .format(section, hint)
            )
    for label, host in app_hosts:
        if host not in credentials:
            problems.append(
                '{} sets requires_credentials = true but there is no '
                '[credentials."{}"] entry'.format(label, host)
            )

    # --- [database] --------------------------------------------------------
    database = need_table("database")
    need_str(database, "database", "root_password")
    seed = need_str(database, "database", "seed")
    if seed is not None and seed not in SEED_MODES:
        problems.append(
            "database.seed = {!r} is not one of {}".format(seed, ", ".join(SEED_MODES))
        )
    dump_path = database.get("dump_path", "")
    encryption_key = database.get("encryption_key", "")
    if not isinstance(dump_path, str):
        problems.append("database.dump_path must be a string")
        dump_path = ""
    if not isinstance(encryption_key, str):
        problems.append("database.encryption_key must be a string")
        encryption_key = ""
    if seed in ("sql_dump", "frappe_backup"):
        if not dump_path.strip():
            problems.append("database.seed = {!r} requires database.dump_path".format(seed))
        # Risk 6.  seed_db.sh aborts on this too, but failing here costs
        # seconds instead of failing after a restore that looked fine.
        if not encryption_key.strip():
            problems.append(
                "database.seed = {!r} requires database.encryption_key; without the "
                "key that matches the dump, every stored password in the restored "
                "site is silently unreadable".format(seed)
            )

    # --- [site] ------------------------------------------------------------
    site = need_table("site")
    need_str(site, "site", "admin_password")
    # Optional. Set on a VPS, where the site is reached through Caddy at its
    # own subdomain (decisions #41, #42). https only: that is the reason the
    # proxy exists, and a plain http host_name would put http:// links into
    # every email and webhook payload the site sends.
    if "public_url" in site:
        url = site["public_url"]
        if not isinstance(url, str) or public_host_of(url) is None:
            problems.append(
                "site.public_url = {!r} must be https://<hostname>, with no path "
                "and no port -- for example \"https://acme.dev.example.com\". "
                "Frappe cannot be served under a path (decision #41).".format(url)
            )

    # Optional. Absent, frappe names the database itself (_ and 16 hex digits).
    # 64 is MariaDB's limit for a database name; frappe also makes the name the
    # database user, whose limit (80) is looser. Letters, digits and _ only,
    # not starting with a digit, so the name never needs quoting anywhere.
    if "db_name" in site:
        name = site["db_name"]
        if not isinstance(name, str) or not DB_NAME_RE.match(name):
            problems.append(
                "site.db_name = {!r} must be 1-64 letters, digits or _, not starting "
                "with a digit -- for example \"acme_dev\". Remove the key to let "
                "frappe choose the name.".format(name)
            )

    # --- [frappe] ----------------------------------------------------------
    frappe = need_table("frappe")
    need_int(frappe, "frappe", "developer_mode", 0, 1)
    need_int(frappe, "frappe", "background_workers", 1)
    optional_bool(frappe, "frappe", "watch", True)
    # gunicorn_workers is accepted and ignored (decision #57): `bench serve` is
    # werkzeug, and only bench's production supervisor config reads the key.

    # --- [debug] : optional ------------------------------------------------
    # Absent means disabled. An environment created before this section existed
    # has no opinion, and that is not an error. Present means all three keys are
    # required, so that a half-written section cannot silently half-apply.
    debug = cfg.get("debug")
    if debug is not None:
        if not isinstance(debug, dict):
            problems.append("[debug] must be a table")
        else:
            for key in sorted(set(debug) - {"enabled", "target", "port"}):
                problems.append("debug.{} is not a recognised key".format(key))
            enabled = need_bool(debug, "debug", "enabled")
            debug_port = need_int(debug, "debug", "port", 1, 65535)
            target = need_str(debug, "debug", "target")
            if target is not None and target not in DEBUG_TARGETS:
                problems.append(
                    "debug.target = {!r} is not one of: {}".format(
                        target, ", ".join(DEBUG_TARGETS))
                )
            # Only when enabled: a disabled section publishes nothing, so a
            # port it happens to share with nginx is not yet a problem, and
            # refusing the build over it would be noise.
            if enabled and debug_port is not None:
                for key, value in (("http", http_port), ("mariadb", db_port)):
                    if value is not None and value == debug_port:
                        problems.append(
                            "debug.port and ports.{} are both {}".format(key, debug_port)
                        )

    # --- [secrets] : optional --------------------------------------------
    secrets = cfg.get("secrets")
    if secrets is not None:
        if not isinstance(secrets, dict):
            problems.append("[secrets] must be a table")
        else:
            unknown = sorted(set(secrets) - {"file"})
            for key in unknown:
                problems.append("secrets.{} is not a recognised key".format(key))
            if "file" in secrets and not isinstance(secrets["file"], str):
                problems.append("secrets.file must be a string")
            elif isinstance(secrets.get("file"), str) and secrets["file"].strip() == ".env":
                # render.py generates .env for the compose front end and
                # overwrites it on every render. Pointing the secrets file at
                # it means the first render destroys the secrets.
                problems.append(
                    "secrets.file cannot be '.env'; render.py generates that file "
                    "for compose and overwrites it on every render. Use a "
                    "different name, such as the default {!r}."
                    .format(DEFAULT_SECRETS_FILE)
                )

    # --- [bench] : optional ------------------------------------------------
    bench_section = cfg.get("bench")
    if bench_section is not None:
        if not isinstance(bench_section, dict):
            problems.append("[bench] must be a table")
        else:
            for key in sorted(set(bench_section) - {"env_file"}):
                problems.append("bench.{} is not a recognised key".format(key))
            env_file = bench_section.get("env_file")
            if env_file is not None and not isinstance(env_file, str):
                problems.append("bench.env_file must be a string")
            elif isinstance(env_file, str):
                if not env_file.strip():
                    problems.append("bench.env_file is empty; remove it to load no file")
                elif Path(env_file.strip()).name == ".env":
                    problems.append(
                        "bench.env_file cannot be '.env'; render.py generates that file "
                        "for compose and overwrites it on every render. Use a different "
                        "name, such as {!r}.".format(DEFAULT_BENCH_ENV_FILE))

    # --- unfilled placeholders ---------------------------------------------
    problems.extend(find_placeholders(cfg))

    return problems


PLACEHOLDER = "FILL-ME"


# The [internal_ports] that share the bench container, and so can collide.
BENCH_CONTAINER_PORTS = ("web", "socketio", "file_watcher")


def _version_tuple(text):
    """Leading numeric parts of a version string: '11.8' -> (11, 8)."""
    parts = []
    for piece in str(text).split("."):
        digits = re.match(r"\d+", piece)
        if not digits:
            break
        parts.append(int(digits.group(0)))
    return tuple(parts)


def image_tag(image):
    """The tag of an image reference, '' when it has none."""
    last = image.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else ""


def image_codename(image):
    """The Debian codename an image tag names, or None (decision #57)."""
    tag = image_tag(image)
    for codename in known_versions.DEBIAN_CODENAMES:
        if codename in tag:
            return codename
    return None


def series_problems(stack, known):
    """
    Pairings the build would otherwise discover, minutes in (decision #57).

    Only what is known to fail is refused here. What frappe merely warns about
    is in series_warnings().
    """
    problems = []
    label = known["label"]

    node = stack.get("node")
    if isinstance(node, str) and node.split(".")[0] != known["node"]:
        problems.append(
            "stack.node = {!r} but Frappe {} needs Node {} "
            "(erp_sandbox/known_versions.py). One of the two is wrong, and the "
            "build would fail well after this point.".format(node, label, known["node"])
        )

    bench = stack.get("bench")
    if isinstance(bench, str) and _version_tuple(bench) \
            and _version_tuple(bench) < _version_tuple(known["bench_min"]):
        problems.append(
            "stack.bench = {!r} is older than {}, the oldest bench CLI known to "
            "work with Frappe {}.".format(bench, known["bench_min"], label)
        )

    release = stack.get("wkhtmltopdf")
    image = stack.get("base_image")
    codename = image_codename(image) if isinstance(image, str) else None
    shipped = known_versions.WKHTMLTOPDF_DEBS.get(release)
    if codename and shipped is not None and codename not in shipped:
        fitting = sorted(r for r, names in known_versions.WKHTMLTOPDF_DEBS.items()
                         if codename in names)
        problems.append(
            "stack.wkhtmltopdf = {!r} has no .deb for {}, which base_image names; "
            "the image build would fail at the download. {}".format(
                release, codename,
                "Use {!r}.".format(fitting[-1]) if fitting
                else "No wkhtmltopdf release ships one for {}.".format(codename))
        )
    return problems


def series_warnings(cfg):
    """
    What frappe itself only warns about, or what is merely suspicious.

    Said by preflight and doctor, never refused: a fork may use other branch
    names, and a MariaDB outside frappe's range is a frappe warning, not a
    failure (decision #57).
    """
    stack = cfg.get("stack") or {}
    known = known_versions.get(stack.get("frappe_version"))
    if not known:
        return []
    warnings = []

    for app in cfg.get("apps") or []:
        ref = app.get("ref", "")
        match = re.fullmatch(r"version-(\d+)", str(ref))
        if app.get("name") in ("frappe", "erpnext") and match \
                and match.group(1) != str(stack.get("frappe_version")):
            warnings.append(
                "apps.{}.ref = {!r} but stack.frappe_version = {}. The bench "
                "is built for one series and the code is another's.".format(
                    app["name"], ref, stack.get("frappe_version"))
            )

    window = known.get("mariadb_range")
    image = stack.get("mariadb_image", "")
    version = _version_tuple(image_tag(image))
    if window and len(version) >= 2:
        low, high = (_version_tuple(v) for v in window)
        if not low <= version[:2] <= high:
            warnings.append(
                "stack.mariadb_image is MariaDB {} but Frappe {} supports {} to "
                "{}; frappe warns outside that range.".format(
                    ".".join(map(str, version)), known["label"], *window)
            )
    return warnings


def parse_version(text):
    """(major, minor, patch) from "X.Y.Z", or None if it is not that."""
    parts = str(text).split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def check_min_version(required):
    """Problems with [erp-sandbox].min_version, if any."""
    from erp_sandbox import __version__

    wanted = parse_version(required)
    if wanted is None:
        return ["erp-sandbox.min_version = {!r} is not a three-part version "
                "like \"0.2.0\"".format(required)]
    have = parse_version(__version__)
    if have is None:                       # cannot happen from a release
        return []
    if have < wanted:
        return [
            "this environment needs erp-sandbox {} or newer; this is {}. "
            "Upgrade the tool rather than editing the environment:\n"
            "      uv tool upgrade erp-sandbox   (or: pipx upgrade erp-sandbox)"
            .format(required, __version__)
        ]
    return []


def find_placeholders(node, path=""):
    """
    Every remaining FILL-ME, by dotted path.

    A config handed to someone else to complete is more useful when the blanks
    announce themselves. Without this, an unfilled repo URL surfaces as a git
    clone failure several minutes into a build.
    """
    problems = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = "{}.{}".format(path, key).lstrip(".")
            if PLACEHOLDER in str(key):
                problems.append("{} -- the table name itself is {}".format(here, PLACEHOLDER))
            problems.extend(find_placeholders(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            problems.extend(find_placeholders(value, "{}[{}]".format(path, index)))
    elif isinstance(node, str) and PLACEHOLDER in node:
        problems.append("{} is still {}".format(path or "a value", PLACEHOLDER))
    return problems


# https://host/path, ssh://user@host/path, and the scp-style user@host:path
# that GitHub and GitLab hand out by default.
_HTTPS_REMOTE_RE = re.compile(r"^https?://(?:[^@/]+@)?([^/:]+)")
_SSH_URL_RE = re.compile(r"^ssh://(?:[^@/]+@)?([^/:]+)")
_SSH_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?([^/:\s]+):(?!/)[^\s]+$")


def _https_authority_problem(repo):
    """
    ``https://host:something/path`` where *something* is not a port number.

    The regex below happily reads the host out of such a URL, so without this
    check a typo like ``https://github.com:owner/repo.git`` -- the scp-style
    form with its colon left in -- validates, and then fails several minutes
    later inside git with a message about a port.
    """
    parts = urlsplit(repo)
    try:
        # Accessing .port is the test: it is a property that parses the
        # authority and raises on a port that is not a number. The value is
        # not wanted, only whether getting it succeeds.
        _ = parts.port
    except ValueError:
        authority = parts.netloc.rsplit("@", 1)[-1]
        host, _, junk = authority.partition(":")
        return (
            "{!r} has {!r} where a port number belongs. In an https URL the "
            "separator after the host is '/', not ':' -- the ':' form is ssh. "
            "Did you mean {!r}?".format(
                repo, junk, "{}://{}/{}{}".format(parts.scheme, host, junk, parts.path))
        )
    return None


def repo_remote(repo):
    """
    ``(host, kind)`` for a clone URL, where kind is ``"https"`` or ``"ssh"``.

    ``(None, None)`` if the string is not a git remote this tool can classify.
    The kind matters because credentials reach https clones through
    GIT_ASKPASS (technical.md section 5) while ssh clones authenticate with
    the developer's own key, which this tool neither supplies nor manages.
    """
    match = _HTTPS_REMOTE_RE.match(repo)
    if match:
        return match.group(1), "https"
    match = _SSH_URL_RE.match(repo)
    if match:
        return match.group(1), "ssh"
    match = _SSH_SCP_RE.match(repo)
    if match:
        return match.group(1), "ssh"
    return None, None


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
#
# An ``env:NAME`` reference is looked up in two places, in this order:
#
#   1. the process environment  -- what `export NAME=...` puts there
#   2. the secrets file         -- KEY=value lines, gitignored
#
# The shell wins.  That is the same precedence Docker Compose applies to its
# own .env, and it is the useful direction: a developer can override one value
# for one run without editing a file.  Because a silent override is still a
# surprise, preflight prints which source each secret came from.
#
# The secrets file is NOT named .env.  render.py *generates* .env for the
# compose front end, and would overwrite anything a developer put there.  The
# default is secrets.env, and [secrets].file can point anywhere -- including
# outside the repository, which is the safest place for it.

DEFAULT_SECRETS_FILE = ".erp-sandbox/secrets.env"

# [bench].env_file: runtime secrets for the bench container, and only those
# (technical.md section 5). Never the secrets file above, which holds the build
# and host secrets -- the PAT, the MariaDB root password -- that would otherwise
# sit in the environment of every bench process, `bench console` included.
DEFAULT_BENCH_ENV_FILE = ".erp-sandbox/bench.env"

SOURCE_SHELL = "shell"
SOURCE_LITERAL = "config.toml"

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def secrets_file_path(cfg, config_path):
    """
    Where to look for the secrets file, as (path_or_None, was_explicit).

    A relative path resolves against the directory holding config.toml, so the
    two travel together.  ``file = ""`` disables the file entirely.
    """
    section = cfg.get("secrets") or {}
    explicit = isinstance(section, dict) and "file" in section
    raw = section.get("file", DEFAULT_SECRETS_FILE) if isinstance(section, dict) else DEFAULT_SECRETS_FILE
    if not isinstance(raw, str) or not raw.strip():
        return None, explicit
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    return path, explicit


def bench_env_file_path(cfg, config_path):
    """[bench].env_file as an absolute Path, or None when it is not set.

    Relative to the directory holding config.toml, like [secrets].file.
    """
    raw = (cfg.get("bench") or {}).get("env_file")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    return path


def config_env_names(cfg):
    """The NAMEs of every env:NAME secret reference in config.toml."""
    names = set()
    for _name, value in secret_fields(cfg):
        if isinstance(value, str) and value.startswith("env:") and value[4:].strip():
            names.add(value[4:].strip())
    return names


def parse_env_file(text):
    """
    Parse KEY=value lines.  Returns (mapping, problems).

    Deliberately not a shell.  ``$OTHER`` is not expanded and an inline ``#``
    is not a comment -- both are perfectly legal characters in a password, and
    a parser that quietly ate half a secret would be worse than no parser.
    A leading ``export`` is tolerated so the same file can also be sourced.
    """
    values = {}
    problems = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            problems.append("line {}: no '=' in {!r}".format(number, raw.strip()))
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not ENV_KEY_RE.match(key):
            problems.append("line {}: {!r} is not a valid variable name".format(number, key))
            continue
        value = value.strip()
        # Only a matched pair of surrounding quotes is stripped.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values, problems


def load_secrets_file(cfg, config_path):
    """
    Read the secrets file if there is one.

    Returns (values, path_or_None).  Raises ConfigError when the file was named
    explicitly but is missing, or when it does not parse -- a config that
    points at a file that is not there is a mistake, not a preference.
    """
    path, explicit = secrets_file_path(cfg, config_path)
    if path is None:
        return {}, None
    if not path.is_file():
        if explicit:
            raise ConfigError([
                "[secrets].file points at {} but there is no such file".format(path)
            ])
        return {}, None
    values, problems = parse_env_file(path.read_text(encoding="utf-8"))
    if problems:
        raise ConfigError(["{}: {}".format(path, problem) for problem in problems])
    return values, path


class SecretSource:
    """The two places an env: reference can resolve, and which one answered."""

    def __init__(self, file_values=None, path=None, shell=None):
        self.file_values = dict(file_values or {})
        self.path = path
        self.shell = os.environ if shell is None else shell

    def lookup(self, name):
        """(value_or_None, source_label)."""
        if name in self.shell:
            return self.shell[name], SOURCE_SHELL
        if name in self.file_values:
            return self.file_values[name], self.label
        return None, None

    @property
    def label(self):
        return self.path.name if self.path else DEFAULT_SECRETS_FILE

    @property
    def places(self):
        if self.path:
            return "not exported in this shell and not in {}".format(self.path)
        return "not exported in this shell, and there is no secrets file"


def resolve_secret(value, where, source=None):
    """
    Resolve one secret field.

    ``env:NAME`` is the documented default because it keeps the value out of
    config.toml.  A literal is accepted and warned about.

    Returns (value, warning_or_None, source_label).  Raises ConfigError when an
    env: reference does not resolve -- an unset variable must fail the build,
    not quietly produce an empty password.
    """
    source = source or SecretSource()
    if not isinstance(value, str):
        raise ConfigError(["{} must be a string".format(where)])
    if value.startswith("env:"):
        name = value[4:].strip()
        if not name:
            raise ConfigError(["{} is 'env:' with no variable name".format(where)])
        resolved, label = source.lookup(name)
        if resolved is None:
            raise ConfigError([
                "{} refers to ${} but it is {}".format(where, name, source.places)
            ])
        if not resolved:
            raise ConfigError([
                "{} refers to ${} but it is empty (from {})".format(where, name, label)
            ])
        return resolved, None, label
    return value, (
        "{} holds a literal secret rather than an env: reference. config.toml is "
        "gitignored, so this is allowed, but the value now exists on disk."
        .format(where)
    ), SOURCE_LITERAL


# Secrets are grouped by which step of the build consumes them, so a step can
# resolve what it uses and no more.  render.py needs two passwords and no git
# credential; failing it over an unset PAT it will never present is how a
# clean checkout stops being renderable.
SECRETS_BUILD = "build"    # rendered into generated files
SECRETS_SEED = "seed"      # read by the seeding step
SECRETS_FETCH = "fetch"    # presented to git hosts when cloning
SECRET_GROUPS = (SECRETS_BUILD, SECRETS_SEED, SECRETS_FETCH)


def secret_fields(cfg, groups=None):
    """
    Fields in the schema that carry a secret, as ``(dotted_name, value)``.

    ``groups`` restricts the result to the named SECRETS_* groups; the default
    of None returns all of them, which is what preflight wants -- it checks the
    whole configuration, not one step of it.
    """
    if groups is None:
        groups = SECRET_GROUPS
    unknown = set(groups) - set(SECRET_GROUPS)
    if unknown:
        raise ValueError("unknown secret group(s): {}".format(", ".join(sorted(unknown))))

    fields = [
        (SECRETS_BUILD, "database.root_password",
         cfg.get("database", {}).get("root_password")),
        (SECRETS_BUILD, "site.admin_password",
         cfg.get("site", {}).get("admin_password")),
        (SECRETS_SEED, "database.encryption_key",
         cfg.get("database", {}).get("encryption_key")),
    ]
    for host, entry in (cfg.get("credentials") or {}).items():
        if isinstance(entry, dict):
            fields.append((SECRETS_FETCH, 'credentials."{}".token'.format(host),
                           entry.get("token")))
    # encryption_key is optional and empty unless a dump is being restored;
    # validate() has already insisted on it in the modes that need it.
    return [(name, value) for group, name, value in fields
            if value and group in groups]


def resolve_all_secrets(cfg, config_path, source=None, groups=None):
    """
    Resolve secrets, by default every one in the configuration.

    ``groups`` is passed through to secret_fields, so a caller resolves only
    the secrets its own step consumes.

    Returns (values_by_dotted_name, warnings, sources_by_dotted_name).
    """
    if source is None:
        file_values, path = load_secrets_file(cfg, config_path)
        source = SecretSource(file_values, path)

    resolved = {}
    warnings = []
    sources = {}
    problems = []
    for name, raw in secret_fields(cfg, groups):
        try:
            value, warning, label = resolve_secret(raw, name, source)
        except ConfigError as exc:
            problems.extend(exc.problems)
            continue
        resolved[name] = value
        sources[name] = label
        if warning:
            warnings.append(warning)
    if problems:
        raise ConfigError(problems)
    return resolved, warnings, sources


# ---------------------------------------------------------------------------
# Host checks
# ---------------------------------------------------------------------------

def pip_constraints(cfg):
    """
    The pip constraints this environment asks for.

    config.toml, and nothing else. There is deliberately no fallback to
    known_versions.py: its table seeds a *new* config.toml at `erp-sandbox init`
    and is never consulted again, so an environment that empties the list gets
    no constraints and erp-sandbox does not add one back (decision #36).
    """
    return list(cfg.get("stack", {}).get("pip_constraints", []))


def constraints_declared(cfg):
    """True if config.toml has an opinion, including an explicit empty list."""
    return "pip_constraints" in cfg.get("stack", {})


def constraints_warning(cfg):
    """
    Warning text for a config.toml that predates [stack].pip_constraints, or None.

    Absence is not a choice a developer made -- the key did not exist when the
    file was written -- so it gets said out loud rather than acted on silently.
    An explicit empty list IS a choice, and says nothing.
    """
    if constraints_declared(cfg):
        return None
    template = known_versions.constraints_template(
        cfg.get("stack", {}).get("frappe_version"))
    if not template:
        return None
    rendered = ", ".join('"{}"'.format(entry) for entry in template)
    return (
        "config.toml has no stack.pip_constraints key, so NO pip constraints "
        "are applied.\n"
        "  This environment was created before the key existed. The template "
        "for this Frappe series is:\n"
        "\n"
        "      pip_constraints = [{}]\n"
        "\n"
        "  Paste it into [stack] to keep the old behaviour, or add an empty "
        "list to accept none and silence this:\n"
        "\n"
        "      pip_constraints = []\n"
        "\n"
        "  Why the v12 template exists: vanilla v12 pins openpyxl==2.6.4, whose "
        "compat layer reads numpy.float, removed in numpy 1.24. A fork with "
        "modernised pins does not need it -- and one pinning numpy==1.26.4 is "
        "broken by it.".format(rendered)
    )


def debug_settings(cfg):
    """
    The [debug] section, normalised, for every caller that needs it.

    Returns (enabled, target, port). A missing section is disabled, which is
    what an environment created before the section existed means by saying
    nothing. port is None when disabled, so that nothing can publish or check a
    port the developer never asked for.
    """
    debug = cfg.get("debug") or {}
    enabled = bool(debug.get("enabled"))
    if not enabled:
        return False, None, None
    return True, debug.get("target"), debug.get("port")


def http_bind(cfg):
    """The address nginx's port is published on (decision #46)."""
    return cfg["ports"].get("bind", DEFAULT_HTTP_BIND)


def http_probe_host(cfg):
    """Where this machine reaches nginx: the bind address, or loopback for 0.0.0.0."""
    bind = http_bind(cfg)
    return "127.0.0.1" if bind == "0.0.0.0" else bind


_PUBLIC_URL_RE = re.compile(r"^https://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)/?$")


def public_host_of(url):
    """The hostname of an acceptable public_url, or None if it is not one."""
    match = _PUBLIC_URL_RE.match(url)
    return match.group(1).lower() if match else None


def public_url(cfg):
    """[site].public_url without a trailing slash, or None when unset."""
    url = (cfg.get("site") or {}).get("public_url")
    return url.rstrip("/") if url else None


def public_host(cfg):
    url = public_url(cfg)
    return public_host_of(url) if url else None


def current_user():
    """The login name of the uid running erp-sandbox; the registry is keyed by it."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def registry_path():
    override = os.environ.get(REGISTRY_ENV)
    return Path(override).expanduser() if override else DEFAULT_REGISTRY


def load_registry(path):
    """
    Parse the allocation registry. Returns {user: {"ports": set, "hosts": set}}.

    Raises ConfigError with every problem found. The format is one table per
    Linux user:

        [users.bob]
        ports = [8101, 3316]
        hosts = ["bob.dev.example.com"]

    A port or host listed under two users is itself a problem: the registry is
    the one place that is supposed to rule a collision out.
    """
    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(["{} cannot be read: {}".format(path, exc)]) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(["{} is not valid TOML: {}".format(path, exc)]) from exc

    problems = []
    users = data.get("users")
    if not isinstance(users, dict):
        raise ConfigError(["{} has no [users.<name>] tables".format(path)])
    registry = {}
    port_owner, host_owner = {}, {}
    for user, entry in users.items():
        if not isinstance(entry, dict):
            problems.append("users.{} must be a table".format(user))
            continue
        for key in sorted(set(entry) - {"ports", "hosts"}):
            problems.append("users.{}.{} is not a recognised key".format(user, key))
        ports = entry.get("ports", [])
        hosts = entry.get("hosts", [])
        if not isinstance(ports, list) or not all(
                isinstance(p, int) and not isinstance(p, bool) for p in ports):
            problems.append("users.{}.ports must be a list of port numbers".format(user))
            ports = []
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            problems.append("users.{}.hosts must be a list of hostnames".format(user))
            hosts = []
        hosts = [h.lower() for h in hosts]
        for port in ports:
            if port in port_owner and port_owner[port] != user:
                problems.append("port {} is assigned to both {} and {}".format(
                    port, port_owner[port], user))
            port_owner.setdefault(port, user)
        for host in hosts:
            if host in host_owner and host_owner[host] != user:
                problems.append("host {} is assigned to both {} and {}".format(
                    host, host_owner[host], user))
            host_owner.setdefault(host, user)
        registry[user] = {"ports": set(ports), "hosts": set(hosts)}
    if problems:
        raise ConfigError(["{}: {}".format(path, p) for p in problems])
    return registry


def wanted_ports(cfg):
    """[(label, port)] for every port this environment publishes."""
    wanted = [("ports.{}".format(key), cfg["ports"][key]) for key in ("http", "mariadb")]
    # The third published port, and only when asked for (decision #29).
    # Decision #16's default of two ports is what makes this conditional.
    enabled, _target, debug_port = debug_settings(cfg)
    if enabled:
        wanted.append(("debug.port", debug_port))
    return wanted


def check_registry(report, cfg, user=None, path=None):
    """
    Decision #50: refuse a port or host the registry assigns to someone else.

    Preflight's own port check only sees a port as taken while its owner's
    stack is running, so two developers can be handed the same port and find
    out the first time both are up. The registry is what makes that visible
    while one of them is down.
    """
    path = path or registry_path()
    user = user or current_user()
    if not path.is_file():
        report.ok("allocation registry", "none at {}; allocations not checked".format(path))
        return
    try:
        registry = load_registry(path)
    except ConfigError as exc:
        report.fail("allocation registry", "\n".join(exc.problems))
        return

    def owner_of(kind, value):
        return sorted(u for u, entry in registry.items() if value in entry[kind])

    claims = [(label, "ports", port) for label, port in wanted_ports(cfg)]
    host = public_host(cfg)
    if host:
        claims.append(("site.public_url", "hosts", host))
    for label, kind, value in claims:
        owners = owner_of(kind, value)
        if user in owners:
            report.ok("{} = {} is assigned to {}".format(label, value, user))
        elif owners:
            report.fail(
                "{} = {} belongs to {}".format(label, value, owners[0]),
                "{} assigns it to another user. Ask the VPS admin which {} are "
                "yours.".format(path, kind),
            )
        else:
            report.warn(
                "{} = {} is not in the registry".format(label, value),
                "nothing stops another user taking it. Ask the VPS admin to "
                "add it to users.{} in {}".format(user, path),
            )


IMAGE_KEYS = ("base_image", "mariadb_image", "redis_image", "nginx_image")


def hub_repository(image):
    """
    (repository, tag) for a Docker Hub image reference, or None for any other.

    docker.io/library/redis:7.4 and redis:7.4 are both ('library/redis',
    '7.4'). An image on another registry, or pinned by digest, is not checked.
    """
    if "@" in image:
        return None
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        if first not in ("docker.io", "index.docker.io", "registry-1.docker.io"):
            return None
        image = rest
    name, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "")
    if "/" not in name:
        name = "library/" + name
    return name, tag or "latest"


def _hub_tag_status(repository, tag, timeout=8):
    url = "https://hub.docker.com/v2/repositories/{}/tags/{}".format(repository, tag)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def check_images(report, cfg, lookup=_hub_tag_status):
    """
    Decision #58: an image tag that does not exist, found before any work.

    Without this the first sign is the engine failing to pull it -- after the
    image build, for mariadb, redis and nginx. Not reachable is a warning, so
    preflight still works offline; only a definite 404 is refused.
    """
    for key in IMAGE_KEYS:
        image = cfg["stack"][key]
        parsed = hub_repository(image)
        if parsed is None:
            report.ok("stack.{} not checked".format(key),
                      "{} is not a Docker Hub tag reference".format(image))
            continue
        status = lookup(*parsed)
        if status == 200:
            report.ok("stack.{} exists".format(key), image)
        elif status == 404:
            report.fail(
                "stack.{} does not exist".format(key),
                "Docker Hub has no tag {!r} for {}. Check the tags at "
                "https://hub.docker.com/_/{}/tags".format(
                    parsed[1], parsed[0], parsed[0].split("/", 1)[-1]),
            )
        else:
            report.warn("stack.{} not checked".format(key),
                        "Docker Hub did not answer ({}); is this machine offline?".format(
                            status if status else "no connection"))


def docker_is_rootless():
    """True under rootless Docker, False under a rootful daemon, None if unknown."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .SecurityOptions}}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return "rootless" in result.stdout


def linger_enabled(user=None):
    """True if systemd keeps this user's services after logout; None if unknown."""
    user = user or current_user()
    try:
        result = subprocess.run(
            ["loginctl", "show-user", user, "--property=Linger", "--value"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() == "yes"


def check_engine_mode(report, cfg, engine):
    """The engine configurations that build fine and then misbehave."""
    if engine == "docker" and docker_is_rootless():
        # compose.docker.yml sets user: UID:GID, which under rootless Docker is
        # remapped into the subuid range -- bind-mounted files land owned by a
        # uid that is not the developer's.
        report.fail(
            "rootless Docker is not supported",
            "files written into bench/ and data/ would be owned by a subuid, not "
            "by you. Use a rootful Docker daemon, or rootless podman.",
        )
    if engine == "podman" and public_url(cfg) and linger_enabled() is False:
        # Only said for an environment that is served (public_url set): on a
        # laptop, a stack that stops at logout is what the developer expects.
        report.warn(
            "lingering is off for {}".format(current_user()),
            "rootless podman containers stop when your last session ends and do "
            "not start at boot. Ask the VPS admin to run: loginctl enable-linger "
            "{}".format(current_user()),
        )


def detect_ids():
    """uid/gid are detected, never configured (decision #15)."""
    return os.getuid(), os.getgid()


def port_is_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def detect_engine():
    """(engine_name, compose_command) or (None, None)."""
    if shutil.which("podman"):
        if shutil.which("podman-compose"):
            return "podman", "podman-compose"
        if _has_compose_subcommand("podman"):
            return "podman", "podman compose"
        return "podman", None
    if shutil.which("docker"):
        if _has_compose_subcommand("docker"):
            return "docker", "docker compose"
        if shutil.which("docker-compose"):
            return "docker", "docker-compose"
        return "docker", None
    return None, None


def _has_compose_subcommand(engine):
    try:
        result = subprocess.run(
            [engine, "compose", "version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def free_gb(path):
    usage = shutil.disk_usage(str(path))
    return usage.free / (1024 ** 3)


def is_built(root):
    """True once the bench step has run: bench/env exists.

    lexists, because bench/env/bin/python links to the container's interpreter
    and does not resolve on the host (decision #22).
    """
    return os.path.lexists(Path(root) / "bench" / "env" / "bin" / "python")


def check_disk(report, root, gb):
    """20 GB before the first build; once built, a warning below 10 GB."""
    if not is_built(root):
        if gb >= MIN_FREE_GB:
            report.ok("disk space", "{:.1f} GB free at {}".format(gb, root))
        else:
            report.fail("disk space",
                        "{:.1f} GB free at {}; at least {} GB is needed for images, "
                        "the bench tree, and the database".format(gb, root, MIN_FREE_GB))
    elif gb >= MIN_FREE_GB_BUILT:
        report.ok("disk space", "{:.1f} GB free at {} (already built)".format(gb, root))
    else:
        report.warn("disk space",
                    "{:.1f} GB free at {}; the environment is built, but a restore "
                    "needs its dump's size again while it decompresses, and below "
                    "{} GB that is easy to run out of".format(gb, root, MIN_FREE_GB_BUILT))


# "127.0.0.1:8101->80/tcp" and "[::]:8101->80/tcp", from `ps --format {{.Ports}}`.
_PUBLISHED_PORT = re.compile(r":(\d+)->")


def parse_port_holders(ps_output, project_name):
    """{host port: container name} for this project's containers.

    ps_output is `<engine> ps --format '{{.Names}}\t{{.Ports}}'`. A container is
    this project's when its name is `<project>-<service>`, the container_name
    every service in the compose file sets.
    """
    holders = {}
    prefix = project_name + "-"
    for line in ps_output.split("\n"):
        name, _, ports = line.partition("\t")
        name = name.strip()
        if not name.startswith(prefix):
            continue
        for port in _PUBLISHED_PORT.findall(ports):
            holders[int(port)] = name
    return holders


def own_port_holders(engine, project_name):
    """parse_port_holders for the running engine; empty if it cannot be asked."""
    try:
        result = subprocess.run(
            [engine, "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    return parse_port_holders(result.stdout, project_name)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

class Report:
    """Collects results so every check runs, rather than stopping at the first."""

    OK, WARN, FAIL = "ok", "warn", "fail"

    def __init__(self):
        self.rows = []

    def add(self, status, label, detail=""):
        self.rows.append((status, label, detail))

    def ok(self, label, detail=""):
        self.add(self.OK, label, detail)

    def warn(self, label, detail=""):
        self.add(self.WARN, label, detail)

    def fail(self, label, detail=""):
        self.add(self.FAIL, label, detail)

    @property
    def failed(self):
        return any(status == self.FAIL for status, _, _ in self.rows)

    def render(self, stream):
        marks = {self.OK: "[ ok ]", self.WARN: "[warn]", self.FAIL: "[FAIL]"}
        for status, label, detail in self.rows:
            stream.write("{} {}\n".format(marks[status], label))
            for line in str(detail).splitlines():
                if line.strip():
                    stream.write("       {}\n".format(line))


def check_private_file(report, label, path):
    """
    Mode 600, and nothing git would commit.

    A file of secrets is only as private as its permissions and its gitignore
    status. Both failures are silent until they are not.
    """
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        report.fail(
            "{} is readable by others".format(label),
            "{} is mode {:o}; run: chmod 600 {}".format(path, mode, path),
        )
    else:
        report.ok("{} permissions".format(label), "mode {:o}".format(mode))

    tracked = git_would_commit(path)
    if tracked is None:
        # Not a git working tree. There is nothing here that could commit
        # the file, so this is a correct state rather than an unknown one.
        # Warning about it in a freshly created environment -- which is the
        # normal case -- teaches developers to ignore warnings.
        report.ok("{} cannot be committed".format(label),
                  "this environment is not a git working tree")
    elif tracked:
        report.fail(
            "{} is not gitignored".format(label),
            "git would commit {}. Add it to .gitignore before doing anything "
            "else.".format(path),
        )
    else:
        report.ok("{} is gitignored".format(label))


def check_bench_env_file(report, cfg, config_path):
    """
    [bench].env_file: private, parseable, and holding none of the build secrets.

    Names only, in everything reported. The values are read to be parsed and
    for nothing else.
    """
    path = bench_env_file_path(cfg, config_path)
    if path is None:
        report.ok("bench env file", "none; the bench container receives no secrets")
        return
    label = "bench env file"
    if not path.is_file():
        report.fail(label, "[bench].env_file points at {} but there is no such file. "
                           "Start from {}.".format(path, project.SAMPLE_BENCH_ENV.name))
        return
    secrets_path, _explicit = secrets_file_path(cfg, config_path)
    if secrets_path is not None and secrets_path.resolve() == path.resolve():
        report.fail(label, "[bench].env_file is the [secrets] file, {}. That file "
                           "holds build and host secrets -- the PAT, the MariaDB root "
                           "password -- and must not reach the container. Use a "
                           "file of its own.".format(path))
        return
    values, problems = parse_env_file(path.read_text(encoding="utf-8"))
    if problems:
        report.fail(label, "{}:\n{}".format(path, "\n".join(problems)))
        return
    report.ok(label, "{}: {}".format(path, ", ".join(sorted(values)) or "no variables"))
    check_private_file(report, label, path)

    try:
        build_names, _path = load_secrets_file(cfg, config_path)
    except ConfigError:
        build_names = {}
    shared = sorted((set(build_names) | config_env_names(cfg)) & set(values))
    if shared:
        report.fail("bench env file holds build secrets",
                    "{}: also a name config.toml reads its own secrets from. Build "
                    "and host secrets stay out of the container; remove from {}."
                    .format(", ".join(shared), path))
    else:
        report.ok("bench env file holds no build secrets")

    # parse_env_file never expands $, but a compose front end reading env_file
    # may; then the container would see a different value than this file says.
    dollar = sorted(name for name, value in values.items() if "$" in value)
    if dollar:
        report.warn("bench env file: a value contains '$'",
                    "{}: a compose front end may expand it. Compare its length "
                    "inside the container, never the value: erp-sandbox shell, "
                    "then printenv NAME | wc -c"
                    .format(", ".join(dollar)))


def check_secrets(report, cfg, config_path):
    """Resolve every secret, and make sure the file holding them is safe to keep."""
    try:
        file_values, path = load_secrets_file(cfg, config_path)
    except ConfigError as exc:
        report.fail("secrets file", "\n".join(exc.problems))
        return

    if path is None:
        report.ok("secrets file", "none in use; env: references come from the shell")
    else:
        report.ok("secrets file", "{} ({} variable(s))".format(path, len(file_values)))
        check_private_file(report, "secrets file", path)

    source = SecretSource(file_values, path)
    try:
        _, warnings, sources = resolve_all_secrets(cfg, config_path, source)
    except ConfigError as exc:
        report.fail("secrets", "\n".join(exc.problems))
        return

    # Which source answered, never the value itself.
    detail = "\n".join(
        "{} <- {}".format(name, label) for name, label in sorted(sources.items())
    )
    report.ok("secrets resolve", detail)
    for warning in warnings:
        report.warn("literal secret", warning)


def git_would_commit(path):
    """
    True if git would track this path, False if ignored, None if unknowable.

    `git check-ignore` exits 0 when a path is ignored and 1 when it is not,
    which is the cheapest reliable answer available.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=str(path.parent), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20, env=project.git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    return None


def run_checks(config_path, skip_engine=False, skip_ports=False, skip_images=False):
    report = Report()

    # The environment is wherever config.toml is, not wherever the tool is
    # installed (decision #27). The disk-space check in particular must look at
    # the filesystem that will hold bench/ and data/.
    root = Path(config_path).resolve().parent

    # Interpreter first; if this file is running at all it already passed.
    report.ok("python {}.{}.{}".format(*sys.version_info[:3]),
              "minimum is {}.{}".format(*MIN_PYTHON))

    uid, gid = detect_ids()
    report.ok("host uid/gid detected", "uid={} gid={}".format(uid, gid))

    if shutil.which("git"):
        report.ok("git present", shutil.which("git"))
    else:
        report.fail("git not found", "apps are cloned host-side; git is required")

    if skip_engine:
        report.warn("container engine check skipped", "--skip-engine")
        engine = None
    else:
        engine, compose = detect_engine()
        if engine is None:
            report.fail("no container engine found", "install podman or docker")
        elif compose is None:
            report.fail("{} found but no compose front end".format(engine),
                        "install podman-compose or the compose plugin")
        else:
            report.ok("container engine", "{} with '{}'".format(engine, compose))

    check_disk(report, root, free_gb(root))

    # --- config ------------------------------------------------------------
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        report.fail("config.toml", "\n".join(exc.problems))
        return report, None
    report.ok("config.toml parses and validates", str(config_path))

    for warning in series_warnings(cfg):
        report.warn("frappe series", warning)

    # Decision #36. Said here and again at the bench step, because
    # `erp-sandbox build --from bench` skips preflight entirely and that is exactly
    # the command a failed build tells the developer to run.
    warning = constraints_warning(cfg)
    if warning:
        report.warn("pip constraints", warning)
    else:
        declared = pip_constraints(cfg)
        report.ok("pip constraints",
                  ", ".join(declared) if declared
                  else "none (stack.pip_constraints is empty)")

    check_secrets(report, cfg, config_path)
    check_bench_env_file(report, cfg, config_path)

    if not skip_engine and engine is not None:
        check_engine_mode(report, cfg, engine)

    # --- ports -------------------------------------------------------------
    if skip_ports:
        report.warn("port check skipped", "--skip-ports")
    else:
        # Risk 12: check before any work, not partway through a 30 minute build.
        holders = None
        for label, port in wanted_ports(cfg):
            host = http_bind(cfg) if label == "ports.http" else "127.0.0.1"
            if port_is_free(port, host):
                report.ok("{} = {} is free".format(label, port))
                continue
            # A running environment holds its own ports; that is not a collision.
            if holders is None:
                holders = (own_port_holders(engine, cfg["project"]["name"])
                           if engine else {})
            if port in holders:
                report.ok("{} = {} is held by this environment".format(label, port),
                          holders[port])
            else:
                report.fail(
                    "{} = {} is already in use".format(label, port),
                    "choose another port in config.toml, or stop whatever is listening",
                )

    # Not skipped with the ports: it reads one file, and a port another user
    # owns is exactly the case the free-port test above cannot see.
    check_registry(report, cfg)

    if skip_images:
        report.warn("image check skipped", "--skip-images")
    else:
        check_images(report, cfg)

    return report, cfg


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate config.toml and the host it will build on.",
    )
    parser.add_argument("--config", default=None,
                        help="path to config.toml (default: %(default)s)")
    parser.add_argument("--skip-engine", action="store_true",
                        help="do not look for podman or docker")
    parser.add_argument("--skip-ports", action="store_true",
                        help="do not test whether the published ports are free")
    parser.add_argument("--skip-images", action="store_true",
                        help="do not ask Docker Hub whether the image tags exist")
    args = parser.parse_args(argv)

    report, _ = run_checks(args.config, args.skip_engine, args.skip_ports,
                           args.skip_images)
    report.render(sys.stdout)
    if report.failed:
        sys.stdout.write("\npreflight failed\n")
        return 1
    sys.stdout.write("\npreflight passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
