"""
doctor — is this environment healthy, and if not, why.

testing.md section 2. Read-only diagnosis: it reports and prints the exact
remediation command, and never attempts a repair. An auto-fixing doctor becomes
a second, undocumented build path that drifts from the real one.

Exit codes: 0 healthy, 1 degraded, 2 broken.

The distinction between degraded and broken is about what the developer can
still do. A stopped container is broken — nothing works. A drifted generated
file, or a bench image built from versions the config has since changed, is
degraded: the environment runs, but it is not the environment the config
describes, and that gap is where a confusing afternoon comes from.
"""

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from erp_sandbox import known_versions, project, render
from erp_sandbox.preflight import (
    MIN_FREE_GB,
    MIN_PYTHON,
    ConfigError,
    SECRETS_BUILD,
    SECRETS_FETCH,
    SECRETS_SEED,
    constraints_warning,
    debug_settings,
    detect_engine,
    detect_ids,
    free_gb,
    http_probe_host,
    load_config,
    pip_constraints,
    resolve_all_secrets,
)

HEALTHY, DEGRADED, BROKEN = 0, 1, 2

SERVICES = ("mariadb", "redis", "bench", "nginx")


class Doctor:
    """Collects findings and decides the exit code."""

    def __init__(self):
        self.rows = []
        self.worst = HEALTHY

    def _add(self, level, group, name, detail, fix):
        self.rows.append((level, group, name, detail, fix))
        self.worst = max(self.worst, level)

    def ok(self, group, name, detail=""):
        self._add(HEALTHY, group, name, detail, None)

    def warn(self, group, name, detail="", fix=None):
        self._add(DEGRADED, group, name, detail, fix)

    def bad(self, group, name, detail="", fix=None):
        self._add(BROKEN, group, name, detail, fix)

    def report(self):
        mark = {HEALTHY: "[ ok ]", DEGRADED: "[warn]", BROKEN: "[FAIL]"}
        group = None
        for level, grp, name, detail, fix in self.rows:
            if grp != group:
                print("\n{}".format(grp))
                group = grp
            print("  {} {}".format(mark[level], name))
            for line in str(detail).splitlines():
                if line:
                    print("         {}".format(line))
            if fix:
                # A remedy that takes more than one command says so on more
                # than one line, and each has to land under the arrow to be
                # copyable.
                first, *rest = str(fix).splitlines()
                print("         -> {}".format(first))
                for line in rest:
                    print("            {}".format(line))

        print()
        if self.worst == HEALTHY:
            print("healthy: {} check(s) passed".format(len(self.rows)))
        else:
            counts = {
                "degraded": sum(1 for r in self.rows if r[0] == DEGRADED),
                "broken": sum(1 for r in self.rows if r[0] == BROKEN),
            }
            print("{}: {} broken, {} degraded, out of {} check(s)".format(
                "broken" if self.worst == BROKEN else "degraded",
                counts["broken"], counts["degraded"], len(self.rows)))
        return self.worst


# ---------------------------------------------------------------------------
# Engine helpers
# ---------------------------------------------------------------------------

def _run(command, timeout=30):
    """(returncode, stdout) with stderr folded in. Never raises."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _inspect(engine, name, fmt):
    code, out = _run([engine, "inspect", "--format", fmt, name])
    return out.strip() if code == 0 else None


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def check_host(doc, root):
    version = sys.version_info
    if version[:2] >= MIN_PYTHON:
        doc.ok("Host", "python {}.{}.{}".format(*version[:3]),
               "minimum is {}.{}".format(*MIN_PYTHON))
    else:                                    # unreachable via an installed tool
        doc.bad("Host", "python too old", "{}.{}.{}".format(*version[:3]))

    engine, compose = detect_engine()
    if engine is None:
        doc.bad("Host", "container engine", "neither podman nor docker found",
                "install podman or docker")
        return None, None
    if compose is None:
        doc.bad("Host", "compose front end", "{} has none".format(engine),
                "install podman-compose, or docker compose")
        return engine, None
    _, ver = _run([engine, "--version"])
    doc.ok("Host", "container engine", "{} with {!r}\n{}".format(engine, compose, ver.strip()))

    gb = free_gb(root)
    if gb >= MIN_FREE_GB:
        doc.ok("Host", "disk space", "{:.1f} GB free at {}".format(gb, root))
    else:
        doc.warn("Host", "disk space",
                 "{:.1f} GB free at {}, below the {} GB minimum".format(gb, root, MIN_FREE_GB))
    return engine, compose


def check_config(doc, root, config_path):
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        doc.bad("Config", "config.toml validates", "\n".join(exc.problems),
                "fix config.toml, then re-run")
        return None
    doc.ok("Config", "config.toml validates", str(config_path))

    known = known_versions.get(cfg["stack"]["frappe_version"])
    doc.ok("Config", "frappe series", "{} (erp_sandbox/known_versions.py)".format(known["label"]))

    # Decision #36. An environment whose constraints differ from the series
    # template is a legitimate state, not a fault -- a fork with modernised pins
    # is supposed to empty this. What doctor owes the reader is the effective
    # list, so a resolution that surprised them is traceable to a line they own.
    warning = constraints_warning(cfg)
    if warning:
        doc.warn("Config", "pip constraints not declared", warning,
                 "add stack.pip_constraints to config.toml")
    else:
        declared = pip_constraints(cfg)
        template = known_versions.constraints_template(cfg["stack"]["frappe_version"])
        if declared:
            detail = ", ".join(declared)
        else:
            detail = "none -- stack.pip_constraints is empty, so pip resolves freely"
        if tuple(declared) != tuple(template):
            detail += "\n(differs from the {} template: {})".format(
                known["label"], ", ".join(template) or "empty")
        doc.ok("Config", "pip constraints", detail)

    # Drift: the environment on disk versus what the config now says.
    try:
        code = render.main(["--config", str(config_path), "--root", str(root),
                            "--check", "--quiet"])
    except SystemExit as exc:
        code = exc.code
    if code == 0:
        doc.ok("Config", "generated files match config.toml")
    else:
        doc.warn("Config", "generated files have drifted",
                 "something was hand-edited, or config.toml changed since the last render",
                 "erp-sandbox render")

    try:
        _, warnings, sources = resolve_all_secrets(
            cfg, config_path, groups=(SECRETS_BUILD, SECRETS_SEED, SECRETS_FETCH))
    except ConfigError as exc:
        doc.bad("Config", "secrets resolve", "\n".join(exc.problems),
                "set the missing variables in .erp-sandbox/secrets.env or the shell")
        return cfg
    doc.ok("Config", "secrets resolve",
           "\n".join("{} <- {}".format(k, v) for k, v in sorted(sources.items())))
    for warning in warnings:
        doc.warn("Config", "secret stored literally", warning)
    return cfg


def check_layout(doc, root, cfg, engine):
    """
    Does the environment contain the files it is going to need?

    Added after an onboarding run failed at the image step with "Dockerfile not
    found": the environment was missing `images/bench/`, and *doctor said it was
    healthy*, because the image had been built before the file went missing.
    Everything downstream looked fine and the environment could not rebuild
    itself.

    Two questions, and the second is the one worth having:

      1. Are the files render is supposed to produce actually there?
      2. Does everything docker-compose.yml *refers to* exist?

    (2) is independent of (1). render knows what it was told to write; the
    compose file knows what the engine will go looking for. When those two
    disagree, the gap only shows up on a step that has not run recently -- which
    is the worst time to find out.
    """
    import re

    # 1. The environment skeleton.
    for relative, why in (
        (project.MARKER, "the marker that makes this an environment"),
        (project.CONFIG_NAME, "the configuration"),
        (".env", "generated: what compose reads"),
        ("docker-compose.yml", "generated: the services"),
        ("compose.{}.yml".format(engine), "generated: the {} overlay".format(engine)),
    ):
        if (Path(root) / relative).exists():
            doc.ok("Layout", "{} present".format(relative))
        else:
            doc.bad("Layout", "{} missing".format(relative), why, "erp-sandbox render")

    # 2. What compose will actually go looking for.
    compose = Path(root) / "docker-compose.yml"
    if not compose.is_file():
        return
    text = compose.read_text(encoding="utf-8", errors="replace")
    contexts = set(re.findall(r"context:\s*\./(\S+)", text))
    mounts = set(re.findall(r"^\s*-\s*\./([^:\s]+):", text, flags=re.M))

    missing = []
    for relative in sorted(contexts | mounts):
        target = Path(root) / relative
        if target.exists():
            continue
        # bench/ and data/ are produced by build steps, and have their own
        # checks further down that can say something more useful than "absent".
        if relative.split("/")[0] in ("bench", "data"):
            continue
        missing.append(relative)

    if missing:
        doc.bad("Layout", "everything docker-compose.yml refers to exists",
                "the engine will look for these and not find them:\n  "
                + "\n  ".join(missing),
                "erp-sandbox render")
    else:
        doc.ok("Layout", "everything docker-compose.yml refers to exists",
               "{} path(s) checked".format(len(contexts | mounts)))

    for relative in sorted(contexts):
        context_dir = Path(root) / relative
        if not context_dir.is_dir():
            continue                     # already reported as missing above
        if (context_dir / "Dockerfile").is_file():
            doc.ok("Layout", "the bench build context has a Dockerfile", relative)
        else:
            doc.bad("Layout", "the bench build context has no Dockerfile",
                    "{} exists but has no Dockerfile, so `erp-sandbox build --only image` "
                    "cannot run".format(relative),
                    "erp-sandbox render")

    # 3. The database directory. With --user set, the image entrypoint can no
    # longer chown it (decision #25), so it has to exist and be ours.
    data = project.data_dir(root)
    if not data.is_dir():
        doc.warn("Layout", "data/mariadb missing",
                 "created by the infra step; MariaDB cannot start without it",
                 "erp-sandbox build --only infra")
    else:
        stat = data.stat()
        uid, gid = detect_ids()
        if (stat.st_uid, stat.st_gid) == (uid, gid):
            doc.ok("Layout", "data/mariadb is yours", "{}:{}".format(uid, gid))
        else:
            detail = ("owned by {}:{}, not {}:{} -- the entrypoint cannot chown it "
                      "because the container runs as you (decision #25)".format(
                          stat.st_uid, stat.st_gid, uid, gid))
            if (stat.st_uid, stat.st_gid) == (0, 0):
                # The engine created the bind-mount source itself. erp-sandbox now
                # creates it first (project.DATA_DIRS), so this is a tree built
                # before that, or one where the directory was removed by hand.
                detail += ("\nroot owns it because the container engine created "
                           "the bind mount before erp-sandbox could -- a rootful "
                           "Docker daemon does that; rootless podman does not")
            doc.bad("Layout", "data/mariadb is owned by someone else", detail,
                    chown_remedy(engine, uid, gid))


def chown_remedy(engine, uid, gid, relative="data/mariadb"):
    """
    The command that fixes ownership, for the engine actually in use (#38).

    `podman unshare` enters the rootless user namespace, where the mapped
    owner can be changed without privilege. It is the wrong answer under
    rootful Docker -- the ids are the host's real ones, so it takes root, and
    podman may not even be installed on that machine.
    """
    if engine == "podman":
        return "podman unshare chown -R {}:{} {}".format(uid, gid, relative)
    return "sudo chown -R {}:{} {}".format(uid, gid, relative)


def check_images(doc, engine, cfg, root):
    project_name = cfg["project"]["name"]
    bench_image = "{}-bench".format(project_name)

    labels = _inspect(engine, bench_image, "{{json .Config.Labels}}")
    if not labels:
        doc.bad("Images", "bench image exists", "{} not found".format(bench_image),
                "erp-sandbox build --only image")
    else:
        try:
            got = json.loads(labels) or {}
        except ValueError:
            got = {}
        stack = cfg["stack"]
        expected = {
            "erp-sandbox.python": stack["python"],
            "erp-sandbox.node": stack["node"],
            "erp-sandbox.yarn": stack["yarn"],
            "erp-sandbox.wkhtmltopdf": stack["wkhtmltopdf"],
            "erp-sandbox.bench": stack["bench"],
        }
        stale = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
        if stale:
            doc.warn("Images", "bench image matches config.toml",
                     "\n".join("{}: image has {!r}, config says {!r}".format(k, a, b)
                               for k, (a, b) in sorted(stale.items())),
                     "erp-sandbox build --only image")
        else:
            doc.ok("Images", "bench image matches config.toml", bench_image)

    for key, service in (("mariadb_image", "mariadb"), ("redis_image", "redis"),
                         ("nginx_image", "nginx")):
        image = cfg["stack"][key]
        if _inspect(engine, image, "{{.Id}}"):
            doc.ok("Images", "{} image present".format(service), image)
        else:
            doc.warn("Images", "{} image missing".format(service), image,
                     "erp-sandbox up  (it will be pulled)")


def check_containers(doc, engine, cfg):
    project_name = cfg["project"]["name"]
    for service in SERVICES:
        name = "{}-{}".format(project_name, service)
        state = _inspect(engine, name, "{{.State.Status}}")
        if state is None:
            doc.bad("Containers", "{} exists".format(service), "no container named " + name,
                    "erp-sandbox up")
            continue
        if state != "running":
            doc.bad("Containers", "{} running".format(service), "state is {!r}".format(state),
                    "erp-sandbox up   (then: erp-sandbox logs {})".format(service))
            continue

        health = _inspect(engine, name, "{{.State.Health.Status}}") or ""
        restarts = _inspect(engine, name, "{{.RestartCount}}") or "0"
        detail = name
        if health and health not in ("", "<no value>"):
            detail += "  health={}".format(health)
        if health and health not in ("healthy", "", "<no value>"):
            doc.warn("Containers", "{} healthy".format(service), detail,
                     "erp-sandbox logs {}".format(service))
            continue
        # A container that keeps coming back is not the same as one that is up.
        try:
            count = int(restarts)
        except ValueError:
            count = 0
        if count > 2:
            doc.warn("Containers", "{} is restarting".format(service),
                     "{} restarts".format(count), "erp-sandbox logs {}".format(service))
            continue
        doc.ok("Containers", "{} up".format(service), detail)


def check_connectivity(doc, cfg, root):
    ports = cfg["ports"]

    if _port_open("127.0.0.1", ports["mariadb"]):
        doc.ok("Connectivity", "mariadb answers on the published port",
               "127.0.0.1:{}".format(ports["mariadb"]))
    else:
        doc.bad("Connectivity", "mariadb answers on the published port",
                "nothing listening on 127.0.0.1:{}".format(ports["mariadb"]),
                "erp-sandbox up mariadb")

    web = "{}:{}".format(http_probe_host(cfg), ports["http"])
    url = "http://{}/".format(web)
    status = _http_status(url)
    if status == 200:
        doc.ok("Connectivity", "nginx serves the site", "{} -> 200".format(url))
    elif status == 502:
        doc.bad("Connectivity", "nginx is up but the bench is not",
                "{} -> 502".format(url),
                "erp-sandbox logs bench")
    elif status is None:
        doc.bad("Connectivity", "nginx serves the site", "no answer from " + url,
                "erp-sandbox up nginx")
    else:
        doc.warn("Connectivity", "nginx serves the site",
                 "{} -> {}".format(url, status))

    # Risk 8: the route that is easy to get subtly wrong, and whose failure
    # looks like an application bug.
    sio = "http://{}/socket.io/?EIO=3&transport=polling".format(web)
    body = _http_body(sio)
    if body and '"sid"' in body:
        doc.ok("Connectivity", "socket.io answers through nginx", "a session id was issued")
    else:
        doc.warn("Connectivity", "socket.io answers through nginx",
                 "no session id in the response -- realtime will be dead and it "
                 "will look like an application bug",
                 "erp-sandbox logs bench")


def _port_open(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _http_status(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def _http_body(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read(400).decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def check_bench(doc, engine, cfg, root):
    project_name = cfg["project"]["name"]
    container = "{}-bench".format(project_name)
    site = cfg["project"]["site"]

    site_dir = Path(root) / "bench" / "sites" / site
    if site_dir.is_dir():
        doc.ok("Bench", "the site exists", str(site_dir))
    else:
        doc.bad("Bench", "the site exists", "{} is missing".format(site_dir),
                "erp-sandbox seed")

    # lexists, not exists: bench/env/bin/python points at the *container's*
    # interpreter, so following the symlink from the host always fails
    # (decision #22).
    env_python = Path(root) / "bench" / "env" / "bin" / "python"
    if os.path.lexists(env_python):
        doc.ok("Bench", "bench/env is built", str(env_python))
    else:
        doc.bad("Bench", "bench/env is built", "{} is missing".format(env_python),
                "erp-sandbox build-bench")

    code, out = _run([engine, "exec", container, "bench", "version"], timeout=120)
    if code != 0:
        doc.warn("Bench", "bench version answers", out.strip()[:400],
                 "erp-sandbox logs bench")
        return
    installed = {}
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) == 2:
            installed[parts[0]] = parts[1]
    missing = [a["name"] for a in cfg["apps"] if a["name"] not in installed]
    if missing:
        doc.warn("Bench", "every app in config.toml is installed",
                 "missing from the bench: {}".format(", ".join(missing)),
                 "erp-sandbox fetch && erp-sandbox build-bench")
    else:
        doc.ok("Bench", "every app in config.toml is installed",
               "  ".join("{} {}".format(k, v) for k, v in sorted(installed.items())))

    # The refs are a host-side question: the apps are bind-mounted.
    for app in cfg["apps"]:
        path = Path(root) / "bench" / "apps" / app["name"]
        if not (path / ".git").exists():
            continue
        code, out = _run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
        actual = out.strip() if code == 0 else "?"
        if actual not in (app["ref"], "HEAD"):
            doc.warn("Bench", "{} is on the configured ref".format(app["name"]),
                     "checked out {!r}, config.toml says {!r}".format(actual, app["ref"]),
                     "erp-sandbox fetch --only {}".format(app["name"]))


def check_debug(doc, engine, cfg, root):
    """
    Only when [debug].enabled. A disabled section publishes nothing, wraps
    nothing, and has nothing to be wrong about, so it contributes no checks.

    Each of these catches a way the feature fails *silently* -- the developer
    attaches, the editor reports a connection, and no breakpoint ever fires.
    """
    enabled, target, port = debug_settings(cfg)
    if not enabled:
        return

    container = "{}-bench".format(cfg["project"]["name"])

    # The Procfile is generated; if it still says plain `bench serve`, config.toml
    # was edited and never rendered, and the debugger has nothing to attach to.
    procfile = Path(root) / "bench" / "Procfile"
    text = procfile.read_text(encoding="utf-8") if procfile.is_file() else ""
    if "debugpy" in text:
        doc.ok("Debug", "the Procfile web line is wrapped in debugpy",
               "target {}, port {}".format(target, port))
    else:
        doc.bad("Debug", "the Procfile web line is wrapped in debugpy",
                "{} still starts the server unwrapped".format(procfile),
                "erp-sandbox render && erp-sandbox restart bench")

    # --noreload travels with the wrapper. Separately checked because losing it
    # is the failure that looks like it works: the debugger attaches to the
    # reloader's parent, which never serves a request.
    if "debugpy" in text and "--noreload" in text:
        doc.ok("Debug", "the reloader is off while debugging",
               "--noreload is on the web line")
    elif "debugpy" in text:
        doc.bad("Debug", "the reloader is off while debugging",
                "the web line wraps debugpy but keeps werkzeug's reloader; the "
                "debugger will attach to the watchdog process, which never "
                "serves a request",
                "erp-sandbox render")

    code, out = _run([engine, "exec", container,
                      "/workspace/bench/env/bin/python", "-c",
                      "import debugpy; print(debugpy.__version__)"], timeout=60)
    if code == 0:
        doc.ok("Debug", "debugpy is installed in bench/env", out.strip())
    else:
        doc.bad("Debug", "debugpy is installed in bench/env", out.strip()[:300],
                "erp-sandbox build-bench")

    if _port_open("127.0.0.1", port):
        doc.ok("Debug", "the debug port accepts a connection",
               "127.0.0.1:{}".format(port))
    else:
        doc.bad("Debug", "the debug port accepts a connection",
                "nothing listening on 127.0.0.1:{}".format(port),
                "erp-sandbox up bench")


def check_dependency_conflicts(doc, engine, cfg):
    """
    Report pip's view of the venv, and never fail on it.

    An environment installs whatever apps its config.toml names, and those apps
    pin what they pin. Two of them disagreeing about a shared dependency is a
    normal outcome of that freedom, not a defect in this tool -- erp-sandbox obeys
    the apps, and lets the disagreement resolve the way a plain `bench` would.
    Brian, 2026-08-23: "Some combination of apps may disagree on things like
    rq. It's going to happen."

    So this is `warn` at worst, and it exists because pip mentions a conflict
    once, in the middle of a long install log, and then exits 0. build_bench.py
    says it at install time; this says it on demand, weeks later, when a
    background job has quietly stopped working and nobody remembers the log.
    """
    container = "{}-bench".format(cfg["project"]["name"])
    code, out = _run(
        [engine, "exec", container, "env/bin/python", "-m", "pip", "check"],
        timeout=120)
    out = out.strip()
    if code == 0:
        doc.ok("Bench", "pip reports no dependency conflicts")
        return

    # Reported as ok, deliberately. A conflict is an accepted outcome under
    # decision #30, so raising it to a warning would put a healthy environment
    # permanently at exit 1 -- and the Phase 7 onboarding test already found
    # what that teaches: a warning that fires on the normal case teaches
    # developers to ignore warnings. The scheduler check reports the same way.
    lines = [ln for ln in out.split("\n") if ln.strip()] or [
        "pip check exited {} and said nothing".format(code)]
    doc.ok("Bench", "pip reports dependency conflicts",
           "{}\nerp-sandbox does not choose between them: it picked neither your "
           "apps nor their pins, so this resolves the way a plain bench would "
           "(decision #30). Whether it matters is a judgement about the "
           "app.".format("\n".join(lines[:6])))


def check_scheduler(doc, engine, cfg):
    """
    Report whether the site's scheduler is enabled. Deliberately not a failure.

    `bench new-site` leaves the scheduler disabled, and this tool does not turn
    it on. That is the safe default rather than an oversight: restoring a client
    dump into a development environment and having the scheduler immediately
    start running that client's scheduled jobs means real emails to real
    addresses and real calls to real integrations. The developer should decide.

    But a developer who does not *know* it is off will conclude their scheduled
    task is broken, so it is reported either way.
    """
    container = "{}-bench".format(cfg["project"]["name"])
    code, out = _run([engine, "exec", container, "bash", "-lc",
                      "cd \"$BENCH_DIR\" && bench --site {} doctor".format(cfg["project"]["site"])],
                     timeout=120)
    if code != 0:
        return
    workers = ""
    for line in out.splitlines():
        if "Workers online" in line:
            workers = line.strip()
    if "Scheduler disabled" in out or "Scheduler inactive" in out:
        doc.ok("Bench", "the scheduler is disabled for this site",
               "{}\nscheduled jobs will not fire. This is frappe's default and "
               "erp-sandbox does not change it -- enabling it on a site restored "
               "from client data would run that client's scheduled jobs for "
               "real. Enable with:\n"
               "  erp-sandbox bench --site {} enable-scheduler".format(
                   workers, cfg["project"]["site"]))
    else:
        doc.ok("Bench", "the scheduler is enabled for this site", workers)


def check_database_grants(doc, engine, cfg, root, config_path):
    """
    The site's database user should be granted at '%' and nowhere else.

    frappe re-creates the user on every new-site and restore, keyed to the
    address the client connected from. Container addresses get recycled, and
    MariaDB prefers the most specific host match -- so a bench that comes up on
    an address an earlier run used authenticates against that run's row, with
    that run's password, and is denied access to its own database.

    Nothing looks wrong until it happens, and what triggers it is `erp-sandbox
    down` followed by `erp-sandbox up`. An environment can therefore be perfectly
    healthy and one restart away from a 500.
    """
    site_config = Path(root) / "bench" / "sites" / cfg["project"]["site"] / "site_config.json"
    if not site_config.is_file():
        return
    try:
        db_name = json.loads(site_config.read_text(encoding="utf-8")).get("db_name")
    except ValueError:
        return
    if not db_name:
        return

    try:
        secrets, _warnings, _sources = resolve_all_secrets(
            load_config(config_path), config_path, groups=(SECRETS_BUILD,))
    except ConfigError:
        return

    container = "{}-mariadb".format(cfg["project"]["name"])
    # MYSQL_PWD rather than -p on the command line, which would be visible in
    # the container's process list for the life of the call.
    code, out = _run([
        engine, "exec", "-e", "MYSQL_PWD=" + secrets["database.root_password"], container,
        "mysql", "-uroot", "-N", "-B", "-e",
        "select host from mysql.user where user='{}' order by host".format(db_name),
    ])
    if code != 0:
        doc.warn("Database", "grants for the site user could not be read",
                 out.strip()[:200])
        return

    hosts = [h.strip() for h in out.split("\n") if h.strip()]
    extra = [h for h in hosts if h != "%"]
    if not hosts:
        doc.warn("Database", "the site's database user exists",
                 "no grant found for {}".format(db_name), "erp-sandbox seed --force")
    elif extra:
        doc.warn("Database", "the site user has host-specific grants",
                 "{} row(s) besides '%': {}\n"
                 "one of these will win over '%' when the bench lands on that "
                 "address, with whatever password it was created with"
                 .format(len(extra), ", ".join(extra[:6])),
                 "erp-sandbox seed --force, or delete them: "
                 "delete from mysql.user where user='{}' and host<>'%'".format(db_name))
    else:
        doc.ok("Database", "the site user is granted at '%' only",
               "{} -- survives a container address change".format(db_name))


def check_ownership(doc, engine, cfg, root):
    """
    The single most important check under rootless podman.

    A file created *inside* the container in a bind mount must land as the host
    user. When it does not, the developer cannot edit their own source, which
    defeats the entire point of the tool (risk 5).
    """
    container = "{}-bench".format(cfg["project"]["name"])
    probe_dir = Path(root) / "bench"
    if not probe_dir.is_dir():
        doc.warn("Ownership", "container writes land as the host user",
                 "bench/ is not there to test with", "erp-sandbox build")
        return

    name = ".erp-sandbox-doctor-probe"
    code, out = _run([engine, "exec", container, "sh", "-c",
                      "touch /workspace/bench/{} 2>&1".format(name)])
    probe = probe_dir / name
    try:
        if code != 0 or not probe.exists():
            doc.bad("Ownership", "container writes land as the host user",
                    "the container could not write into the bind mount:\n" + out.strip()[:300],
                    "check userns_mode/user in compose.{}.yml".format(
                        "podman" if engine == "podman" else "docker"))
            return
        stat = probe.stat()
        uid, gid = detect_ids()
        if (stat.st_uid, stat.st_gid) == (uid, gid):
            doc.ok("Ownership", "container writes land as the host user",
                   "{}:{}".format(uid, gid))
        else:
            doc.bad("Ownership", "container writes land as the host user",
                    "a file written inside the container is owned by {}:{} on the host, "
                    "not {}:{} -- you cannot edit your own source".format(
                        stat.st_uid, stat.st_gid, uid, gid),
                    "check userns_mode/user in the engine overlay (decision #25)")
    finally:
        _run([engine, "exec", container, "rm", "-f", "/workspace/bench/" + name])
        if probe.exists():
            try:
                probe.unlink()
            except OSError:
                pass


def check_venv_not_host_usable(doc, root):
    """
    Decision #22's documented consequence, stated before it bites.

    bench/env is built inside the container, so its interpreter and console
    shebangs are the base image's Python. Activating it from the host produces
    an error nobody reads as "wrong interpreter".
    """
    env_python = Path(root) / "bench" / "env" / "bin" / "python"
    if not os.path.lexists(env_python):
        return
    if env_python.exists():
        doc.warn("Bench", "bench/env is the container's venv",
                 "its interpreter resolves on this host, which is unexpected -- "
                 "it should point at the container's Python")
    else:
        doc.ok("Bench", "bench/env is the container's venv",
               "its interpreter exists only inside the container; do not activate "
               "it from the host")


def check_app_clones(doc, cfg, root):
    """
    Whether each app on disk is the kind of clone config.toml asked for.

    Host-side, so it answers whether or not anything is running. `blobless`
    (decision #31) is applied by `git clone`, which means it applies to a clone
    that has not happened yet: flipping the key does not convert a repository
    that is already on disk, in either direction, and nothing here will convert
    one either. What that leaves is a mismatch the developer cannot see -- the
    config says one thing, the .git directory is another -- and an unexplained
    pause the first time `git log -p` reaches for a blob it does not have.
    """
    for app in cfg["apps"]:
        name = app["name"]
        path = Path(root) / "bench" / "apps" / name
        if not (path / ".git").exists():
            continue
        label = "{} clone kind".format(name)
        wanted = bool(app.get("blobless"))
        code, out = _run(["git", "-C", str(path), "config", "--get",
                          "remote.origin.partialclonefilter"])
        actual = out.strip() if code == 0 else ""
        _, sizes = _run(["git", "-C", str(path), "count-objects", "-vH"])
        pack = "size unknown"
        for line in sizes.splitlines():
            if line.startswith("size-pack:"):
                pack = "pack {}".format(line.split(":", 1)[1].strip())

        if bool(actual) == wanted:
            doc.ok("Bench", label,
                   "{}, {}".format("partial ({})".format(actual) if actual
                                   else "full history", pack))
        elif wanted:
            doc.warn("Bench", label,
                     "config.toml sets blobless = true, but this clone has full "
                     "history ({}). It predates the setting, and a clone is "
                     "never converted in place.".format(pack),
                     "with no uncommitted work there: rm -rf {} && erp-sandbox fetch "
                     "--only {}".format(path, name))
        else:
            doc.warn("Bench", label,
                     "this is a partial clone ({}) but config.toml does not ask "
                     "for one ({}). History operations fetch from the network as "
                     "they need file contents.".format(actual, pack),
                     "to hydrate it in place, keeping your branches:\n"
                     "git -C {p} config --unset remote.origin.partialclonefilter\n"
                     "git -C {p} config --unset remote.origin.promisor\n"
                     "git -C {p} fetch --refetch origin".format(p=path))


def check_secrets_not_committed(doc, root):
    """
    The check that matters to whoever is *using* the tool.

    An environment is a plausible thing to keep in git -- config.toml describes
    a client's stack and is worth versioning somewhere private. What must never
    be committed is `.erp-sandbox/`, which holds the resolved secrets, and in most
    cases config.toml itself, which may hold literals.
    """
    code, _ = _run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"])
    if code != 0:
        doc.ok("Secrets", "environment is not in git", "nothing here can be committed")
        return

    for name in (project.MARKER, project.CONFIG_NAME):
        target = Path(root) / name
        if not target.exists():
            continue
        code, out = _run(["git", "-C", str(root), "ls-files", "--error-unmatch", name])
        if code == 0:
            doc.bad("Secrets", "{} is tracked by git".format(name),
                    "it holds credentials or client values and is committed",
                    "git rm --cached -r {} && echo '/{}' >> .gitignore".format(name, name))
            continue
        code, _ = _run(["git", "-C", str(root), "check-ignore", "-q", name])
        if code == 0:
            doc.ok("Secrets", "{} is gitignored".format(name))
        else:
            doc.warn("Secrets", "{} is not gitignored".format(name),
                     "it is untracked, but nothing stops the next `git add -A`",
                     "echo '/{}' >> .gitignore".format(name))


def check_genericity(doc, root):
    """
    Decision #17, enforced by grep rather than by good intentions.

    This one only applies when erp-sandbox is being *developed*: it asks whether
    the tool's own committed tree has a client's values in it. An installed
    erp-sandbox has no committed tree to grep, and that is not a fault -- so it
    reports and moves on rather than warning about something the user cannot
    act on.
    """
    repo_root = project.PACKAGE_ROOT.parent
    code, _ = _run(["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"])
    if code != 0:
        doc.ok("Genericity", "not applicable",
               "erp-sandbox is installed rather than run from a checkout, so there "
               "is no committed tool tree to check")
        return

    code, out = _run(["git", "-C", str(repo_root), "ls-files"], timeout=30)
    if code != 0:
        doc.ok("Genericity", "not applicable", "no committed tool tree to check")
        return

    try:
        cfg = load_config(project.config_path(root))
    except ConfigError:
        return

    # The values this environment happens to hold. Any of them appearing in a
    # committed file is the leak decision #17 exists to prevent.
    needles = {cfg["project"]["name"], cfg["project"]["site"]}
    for app in cfg["apps"]:
        needles.add(app["name"])
        needles.add(app["repo"])
    needles = {n for n in needles if n and len(n) > 3
               and n not in ("frappe", "erpnext", "bench", "site")}

    offenders = []
    for relative in out.split("\n"):
        relative = relative.strip()
        if not relative:
            continue
        try:
            text = (repo_root / relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for needle in needles:
            if needle in text:
                offenders.append("{}: {!r}".format(relative, needle))

    if offenders:
        doc.bad("Genericity", "no client value in a committed file",
                "\n".join(sorted(set(offenders))[:10]),
                "replace with a placeholder; real values belong in config.toml only")
    else:
        doc.ok("Genericity", "no client value in a committed file",
               "{} value(s) checked against the committed tool tree".format(len(needles)))


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def run(root, config_path, skip_containers=False):
    doc = Doctor()
    root = Path(root)

    engine, _compose = check_host(doc, root)
    cfg = check_config(doc, root, config_path)
    if cfg is None or engine is None:
        return doc.report()

    check_layout(doc, root, cfg, engine)
    check_images(doc, engine, cfg, root)
    if not skip_containers:
        check_containers(doc, engine, cfg)
        running = _inspect(engine, "{}-bench".format(cfg["project"]["name"]),
                           "{{.State.Status}}") == "running"
        check_connectivity(doc, cfg, root)
        if running:
            check_bench(doc, engine, cfg, root)
            check_dependency_conflicts(doc, engine, cfg)
            check_debug(doc, engine, cfg, root)
            check_scheduler(doc, engine, cfg)
            check_database_grants(doc, engine, cfg, root, config_path)
            check_ownership(doc, engine, cfg, root)
    check_app_clones(doc, cfg, root)
    check_venv_not_host_usable(doc, root)
    check_secrets_not_committed(doc, root)
    check_genericity(doc, root)
    return doc.report()
