#!/usr/bin/env python3
"""
render.py — config.toml is the source of truth; this turns it into everything else.

Every generated file carries a header saying so, with one exception noted at
the output itself.  ``--check`` re-renders to memory and diffs against disk,
which is how erp-sandbox doctor proves that nobody has hand-edited a generated
file or changed config.toml without re-rendering.

Standard library only: tomllib plus string.Template (decision #18).  Templates
live in erp_sandbox/templates/.

Nothing in this file hardcodes a port, password, path, image tag, site name,
app name, or repo URL.  If you find yourself about to type one, it belongs in
config.toml.
"""

import argparse
import difflib
import json
import os
import shlex
import shutil
import string
import sys
from pathlib import Path


from erp_sandbox.preflight import (  # noqa: E402  (the interpreter check in preflight must run first)
    SECRETS_BUILD,
    ConfigError,
    debug_settings,
    detect_ids,
    http_bind,
    http_probe_host,
    load_config,
    public_host,
    public_url,
    resolve_all_secrets,
)

from erp_sandbox import known_versions  # noqa: E402
from erp_sandbox.project import ASSET_DIR, TEMPLATE_DIR  # noqa: E402

HEADER_LINES = [
    "GENERATED from config.toml by erp-sandbox render -- do not edit.",
    "Edit config.toml and re-render; this file is overwritten every time.",
]

# Fixed by the architecture (decision #3), not by any client's configuration.
# These are compose service names, which double as hostnames on the container
# network.  They are not candidates for the TOML.
SERVICE_BENCH = "bench"
SERVICE_MARIADB = "mariadb"
SERVICE_REDIS = "redis"

# Redis logical databases (decision #6): one container, three databases, in
# place of production's three containers.
REDIS_DB_CACHE = 0
REDIS_DB_QUEUE = 1
REDIS_DB_SOCKETIO = 2

# Stock bench queue names.  Frappe defines them; config.toml does not get a say.
QUEUES = ("short", "long", "default")


def web_command(internal_web, debug_port):
    """
    The Procfile's ``web:`` line.

    Without debugging this is plain ``bench serve``, which is what a stock bench
    runs.  With debugging it becomes the same thing under debugpy, with two
    changes that are not cosmetic:

    ``--noreload`` -- werkzeug's reloader re-executes the server in a child
    process and watches from the parent.  Both processes carry an identical
    command line, only the child serves, and ``_get_args_for_reloading`` rebuilds
    argv from ``sys.executable`` -- which drops the ``-m debugpy`` prefix at the
    first reload.  Leaving the reloader on therefore fails two ways, both
    silently.  See technical.md section 10.

    ``env/bin/python`` directly, from ``sites/`` -- debugpy has to wrap the
    interpreter that actually imports frappe.  ``bench`` runs under a different
    venv and dispatches to this one, so wrapping ``bench`` would instrument the
    wrong process.  The chdir reproduces what bench does before it dispatches.
    """
    if debug_port is None:
        return "bench serve --port {}".format(internal_web)
    return (
        "cd sites && ../env/bin/python -m debugpy --listen 0.0.0.0:{} "
        "-m frappe.utils.bench_helper frappe serve --noreload --port {}"
    ).format(debug_port, internal_web)


def debug_env_block(debug_port):
    """The .env lines for the debug port, or nothing at all when disabled."""
    if debug_port is None:
        return ""
    return (
        "\n# --- debugpy, published only when [debug].enabled (decision #29) -------------\n"
        "PORT_DEBUG={}\n".format(debug_port)
    )


def debug_ports_block(debug_port):
    """
    The bench service's ``ports:`` entry for the debug port, or nothing.

    Loopback only, for the same reason as mariadb: the point is a debugger on
    this machine, not exposing a debug port -- which executes arbitrary code by
    design -- to the local network.
    """
    if debug_port is None:
        return ""
    return (
        "\n    ports:\n"
        "      - \"127.0.0.1:${PORT_DEBUG:?run-erp-sandbox-render-first}"
        ":${PORT_DEBUG}\""
    )


def host_name_entry(url):
    """
    The ``host_name`` line of common_site_config.json, or nothing.

    frappe.utils.get_url() builds every absolute link the site sends out --
    email, webhook payloads, OAuth callbacks, print links -- from host_name
    when it is set, and from the incoming request otherwise. Behind Caddy the
    request says http and an internal port, so without this the links point
    somewhere nobody outside can reach. Unset on a laptop, where the request
    is already the right answer.
    """
    if not url:
        return ""
    return '    "host_name": {},\n'.format(json.dumps(url))


def apps_block(apps):
    """The app list for the agent briefing: name and ref, nothing derived."""
    return "\n".join(
        "- `{}` at `{}`".format(app["name"], app["ref"]) for app in apps
    )


def bench_env_file_for_compose(cfg):
    """[bench].env_file as compose should see it, or None.

    Compose resolves a relative env_file against the compose file's directory,
    which is the environment root -- where config.toml is, and so where
    [bench].env_file is relative to. A relative path is therefore passed
    through unchanged, and a `~` path expanded.
    """
    raw = (cfg.get("bench") or {}).get("env_file")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    if raw.startswith("~"):
        return str(Path(raw).expanduser())
    if Path(raw).is_absolute() or raw.startswith("./"):
        return raw
    return "./" + raw


def bench_env_file_block(cfg):
    """The bench service's ``env_file:`` entry in the overlay, or nothing."""
    path = bench_env_file_for_compose(cfg)
    if path is None:
        return ""
    # json.dumps: a JSON string is a valid YAML double-quoted scalar, so any
    # path is quoted correctly.
    return (
        "\n    # [bench].env_file: runtime secrets for the apps, read when the\n"
        "    # container is created. `erp-sandbox up --recreate bench` applies a change.\n"
        "    env_file:\n"
        "      - {}".format(json.dumps(path))
    )


def bench_env_briefing_block(cfg):
    """A note on runtime secrets for the agent briefing, or nothing."""
    raw = (cfg.get("bench") or {}).get("env_file")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return (
        "\nRuntime secrets for the apps come from `{}`, loaded into the bench\n"
        "container only. A change takes effect with `erp-sandbox up --recreate bench`;\n"
        "`erp-sandbox restart` keeps the old values. Never print a value from it --\n"
        "`erp-sandbox doctor` lists which names reach the container.\n".format(raw.strip())
    )


def debug_briefing_block(debug_port):
    """One line about debugging for the agent briefing, or nothing at all."""
    if debug_port is None:
        return ""
    return (
        "\nDebugging is **on**: the web process runs under debugpy on\n"
        "`127.0.0.1:{}`, and the reloader is off, so a code change needs\n"
        "`erp-sandbox restart bench`.\n".format(debug_port)
    )


def analysis_paths(apps, python_version):
    """
    What an editor's analyzer must add to resolve `import frappe`.

    One entry per app, because the importable package sits at
    bench/apps/<app>/<app>/ rather than at bench/apps/. Plus the container's
    site-packages, which is readable on the host because bench/env is
    bind-mounted (decision #22). Relative to the workspace folder; no
    interpreter is involved.
    """
    paths = ["bench/apps/{}".format(app["name"]) for app in apps]
    paths.append("bench/env/lib/python{}/site-packages".format(python_version))
    return ",\n".join('      "{}"'.format(path) for path in paths)


def launch_block(project_name, site_name, debug_port):
    """
    The workspace file's ``launch`` section, or nothing when debugging is off.

    Two things here are not stylistic. ``localRoot`` is written
    ``${workspaceFolder:<name>}`` because a launch configuration living in a
    workspace file must scope its variables to a named folder -- the bare form
    does not resolve, and an unresolved path mapping binds breakpoints to
    nothing without reporting anything. And ``justMyCode`` is off, because
    stepping into frappe's pinned dependencies is the case this exists for.
    """
    if debug_port is None:
        return ""
    return """,
  "launch": {
    "version": "0.2.0",
    "configurations": [
      {
        "name": "erp-sandbox: attach to %(site)s (web)",
        "type": "debugpy",
        "request": "attach",
        "connect": { "host": "127.0.0.1", "port": %(port)s },
        "pathMappings": [
          {
            "localRoot": "${workspaceFolder:%(project)s}/bench/apps",
            "remoteRoot": "/workspace/bench/apps"
          }
        ],
        "justMyCode": false
      }
    ]
  }""" % {"site": site_name, "port": debug_port, "project": project_name}


def guard_command():
    """
    The command Claude Code runs before every Edit and Write (decision #35).

    Written as an **absolute path**, resolved at render time, rather than the
    bare word `erp-sandbox`. A hook whose command is not on the PATH of whatever
    started the editor does not refuse anything -- it reports an error and the
    write proceeds, which is the failure this guard exists to prevent, wearing
    a disguise. The path is machine-local, which is why the file carrying it is
    gitignored like every other generated file.

    ``sys.executable -m erp_sandbox`` is the fallback for an install with no
    console script on the PATH; it is the interpreter that is running this
    render, so it can always import erp_sandbox.
    """
    found = shutil.which("erp-sandbox")
    head = (shlex.quote(found) if found
            else "{} -m erp_sandbox".format(shlex.quote(sys.executable)))
    return "{} guard-edit".format(head)


class Tmpl(string.Template):
    """
    Placeholders are ``@@{name}``.

    The default ``$`` delimiter is unusable here: nginx configuration is full
    of ``$host`` and ``$http_upgrade``, and compose files are full of
    ``${VAR:?err}``.  Both are literal output, not substitutions.  Changing the
    delimiter keeps ``substitute`` strict -- an unknown placeholder raises
    instead of being silently passed through, which is what catches typos.
    """

    delimiter = "@@"


def comment_header(prefix="#"):
    return "\n".join("{} {}".format(prefix, line) for line in HEADER_LINES)


def render_template(name, mapping):
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    return Tmpl(text).substitute(mapping)


# ---------------------------------------------------------------------------
# The outputs
# ---------------------------------------------------------------------------

def build_outputs(cfg, secrets, uid, gid):
    """
    Returns an ordered dict of {relative_path: (content, mode)}.

    mode is None for the ordinary 0644 case and an octal int where it matters.
    """
    project = cfg["project"]
    ports = cfg["ports"]
    internal = cfg["internal_ports"]
    stack = cfg["stack"]
    frappe = cfg["frappe"]
    apps = cfg["apps"]

    # Disabled is the default, and disabled means debug_port is None -- which is
    # what every block below tests. Nothing about a disabled [debug] section
    # reaches a generated file.
    _enabled, _target, debug_port = debug_settings(cfg)

    # The series decides the Python and the bench CLI's installer; config.toml
    # names the series (decision #57). Validation has already refused a series
    # the registry does not know.
    series = known_versions.get(stack["frappe_version"])
    _legacy, _modern, bench_cli_pins = known_versions.installer_pins(series)

    outputs = {}

    # --- .env --------------------------------------------------------------
    outputs[".env"] = (
        render_template("env.tmpl", {
            "header": comment_header(),
            "project_name": project["name"],
            "site_name": project["site"],
            "timezone": project["timezone"],
            "host_uid": uid,
            "host_gid": gid,
            "bind_http": http_bind(cfg),
            "port_http": ports["http"],
            "port_mariadb": ports["mariadb"],
            "internal_web": internal["web"],
            "internal_socketio": internal["socketio"],
            "internal_file_watcher": internal["file_watcher"],
            "internal_mariadb": internal["mariadb"],
            "internal_redis": internal["redis"],
            "base_image": stack["base_image"],
            "python_version": series["python"],
            "bench_cli_pins": bench_cli_pins,
            "node_version": stack["node"],
            "yarn_version": stack["yarn"],
            "extra_packages": " ".join(all_extra_packages(stack)),
            "wkhtmltopdf_release": stack["wkhtmltopdf"],
            "bench_version": stack["bench"],
            "mariadb_image": stack["mariadb_image"],
            "redis_image": stack["redis_image"],
            "nginx_image": stack["nginx_image"],
            "db_root_password": secrets["database.root_password"],
            "admin_password": secrets["site.admin_password"],
            "debug_env": debug_env_block(debug_port),
        }),
        # Resolved passwords live here.  Nobody but the developer needs to read it.
        0o600,
    )

    # --- engine overlays ---------------------------------------------------
    for engine in ("podman", "docker"):
        outputs["compose.{}.yml".format(engine)] = (
            render_template("compose.{}.tmpl".format(engine), {
                "header": comment_header(),
                "bench_debug_ports": debug_ports_block(debug_port),
                "bench_env_file": bench_env_file_block(cfg),
            }),
            None,
        )

    # --- nginx -------------------------------------------------------------
    outputs["conf/nginx/site.conf"] = (
        render_template("nginx.site.conf.tmpl", {
            "header": comment_header(),
            "site_name": project["site"],
            "bench_service": SERVICE_BENCH,
            "internal_web": internal["web"],
            "internal_socketio": internal["socketio"],
            "port_http": ports["http"],
        }),
        None,
    )

    # --- common_site_config.json -------------------------------------------
    # Values are substituted pre-encoded so the template stays readable while
    # the quoting stays correct.
    def redis_url(db):
        return "redis://{}:{}/{}".format(SERVICE_REDIS, internal["redis"], db)

    outputs["bench/sites/common_site_config.json"] = (
        render_template("common_site_config.json.tmpl", {
            "header_json": json.dumps(" ".join(HEADER_LINES)),
            "db_host": json.dumps(SERVICE_MARIADB),
            "db_port": json.dumps(internal["mariadb"]),
            "redis_cache": json.dumps(redis_url(REDIS_DB_CACHE)),
            "redis_queue": json.dumps(redis_url(REDIS_DB_QUEUE)),
            "redis_socketio": json.dumps(redis_url(REDIS_DB_SOCKETIO)),
            "socketio_port": json.dumps(internal["socketio"]),
            "webserver_port": json.dumps(internal["web"]),
            "file_watcher_port": json.dumps(internal["file_watcher"]),
            "default_site": json.dumps(project["site"]),
            "host_name_entry": host_name_entry(public_url(cfg)),
            "developer_mode": json.dumps(frappe["developer_mode"]),
            "background_workers": json.dumps(frappe["background_workers"]),
        }),
        None,
    )

    # --- apps.txt ----------------------------------------------------------
    # Only the installed apps, in config.toml order.  An app with
    # install = false is cloned and importable but not on the site.
    installed = [app["name"] for app in apps if app["install"]]
    outputs["bench/sites/apps.txt"] = (
        render_template("apps.txt.tmpl", {"apps": "\n".join(installed)}),
        None,
    )

    # --- Procfile ----------------------------------------------------------
    outputs["bench/Procfile"] = (
        render_template("Procfile.tmpl", {
            "header": comment_header(),
            "web": web_command(internal["web"], debug_port),
            "workers": worker_lines(frappe["background_workers"]),
        }),
        None,
    )

    # --- the agent briefing -------------------------------------------------
    # .claude/rules/ rather than an @import from CLAUDE.md: an import whose
    # path resolves outside the working directory is dropped, so a session
    # started in bench/apps/<app>/ would silently lose it. Rules load from an
    # ancestor directory the way CLAUDE.md does. Verified 2026-08-23.
    outputs[".claude/rules/erp-sandbox-environment.md"] = (
        render_template("claude_rule.md.tmpl", {
            "header": comment_header(">"),
            "project_name": project["name"],
            "site_name": project["site"],
            "web_host": briefing_web_host(cfg),
            "port_http": ports["http"],
            "port_mariadb": ports["mariadb"],
            "bench_container": "{}-{}".format(project["name"], SERVICE_BENCH),
            "apps_block": apps_block(apps),
            "debug_block": debug_briefing_block(debug_port),
            "bench_env_block": bench_env_briefing_block(cfg),
        }),
        None,
    )

    # --- the VSCode workspace -----------------------------------------------
    # A workspace file rather than .vscode/settings.json: opening it pins the
    # workspace root, which is what the debugger's path mapping resolves
    # against, and it leaves .vscode/ entirely to the developer -- folder
    # settings override these, and folder launch configurations appear
    # alongside them.
    outputs["{}.code-workspace".format(project["name"])] = (
        render_template("code_workspace.tmpl", {
            "header": comment_header("//"),
            "project_name": project["name"],
            "extra_paths": analysis_paths(apps, series["python"]),
            "launch_block": launch_block(
                project["name"], project["site"], debug_port),
        }),
        None,
    )

    # --- the edit guard --------------------------------------------------
    # Decision #35. Every other generated file says "do not edit" and trusts
    # the reader; this one makes Claude Code unable to. Strict JSON, and
    # therefore the one generated file with no "GENERATED" header: Claude Code
    # owns this schema, erp-sandbox does not, and a comment it turned out not to
    # accept would take the whole settings file down with it. `render --list`
    # names the file; the refusal names the tool.
    outputs[".claude/settings.json"] = (
        render_template("claude_settings.json.tmpl", {
            "guard_command": json.dumps(guard_command()),
        }),
        None,
    )

    return outputs


CADDY_IMPORT_DIR = "/etc/caddy/erp-sandbox.d"


def proxy_snippet(cfg, root, user):
    """
    The Caddyfile block that serves this environment, or None without a public_url.

    Not a generated file: it belongs in the VPS's Caddy configuration, which no
    environment owns (decisions #42, #43). Caddy's reverse_proxy already sets
    X-Forwarded-For and X-Forwarded-Proto, passes Host through, and proxies
    websockets, so the block needs nothing beyond the upstream -- nginx in the
    environment does the rest, as it does on a laptop.
    """
    host = public_host(cfg)
    if host is None:
        return None
    return (
        "# erp-sandbox environment {project} at {root}, owned by {user}.\n"
        "# Install as {dir}/{host}.caddy, then: sudo systemctl reload caddy\n"
        "{host} {{\n"
        "\treverse_proxy {upstream}:{port}\n"
        "}}\n"
    ).format(project=cfg["project"]["name"], root=root, user=user,
             dir=CADDY_IMPORT_DIR, host=host,
             upstream=http_probe_host(cfg), port=cfg["ports"]["http"])


class _UnresolvedSecrets(dict):
    """Stands in for any secret, for a caller that wants paths, not contents."""

    def __missing__(self, key):
        return "(not resolved)"


def output_paths(cfg):
    """
    Every relative path a render would write, without resolving one secret.

    Which files are generated is a property of the configuration; what is
    *in* them is not. The edit guard needs only the list, and must not go
    quiet because the developer's shell happens not to be carrying a password
    at the moment Claude Code tries to write a file. Derived from
    ``build_outputs`` rather than kept beside it, because a second list is a
    list that disagrees.
    """
    outputs = build_outputs(cfg, _UnresolvedSecrets(), 0, 0)
    outputs.update(asset_outputs())
    return list(outputs)


def briefing_web_host(cfg):
    """The host in the briefing's web URL: where nginx answers on this machine.

    localhost for the loopback default, which reads better and is the same
    address; otherwise [ports].bind, since localhost is refused there.
    """
    host = http_probe_host(cfg)
    return "localhost" if host == "127.0.0.1" else host


def all_extra_packages(stack):
    """
    Debian packages the bench image needs, from two sources.

    The Frappe series contributes what its own requirements need to install
    (known_versions.py, decision #28); the environment contributes what its
    *apps* need ([stack].extra_packages). Order is series first, then config,
    with duplicates dropped -- apt does not care, but a stable order keeps the
    image layer cache stable.
    """
    known = known_versions.get(stack.get("frappe_version"))
    packages = list(known["system_packages"]) if known else []
    for name in stack.get("extra_packages", []):
        if name not in packages:
            packages.append(name)
    return packages


def worker_lines(count):
    """
    One worker process per queue, times background_workers.

    At the default of 1 this produces exactly the three lines a stock bench
    Procfile carries, so the common case looks unsurprising.
    """
    lines = []
    for queue in QUEUES:
        for n in range(1, count + 1):
            suffix = "" if count == 1 else "_{}".format(n)
            lines.append("worker_{}{}: bench worker --queue {}".format(queue, suffix, queue))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Writing and checking
# ---------------------------------------------------------------------------

# Files the tool ships that an environment needs verbatim. They are copied
# rather than templated -- nothing in them varies per client -- but they are
# still *generated*, in the sense that the copy in the environment is a
# derivative that render.py overwrites and --check verifies. Before decision
# #27 they simply sat in the repository, because the repository was the
# environment.
STATIC_ASSETS = {
    "docker-compose.yml": "docker-compose.yml",
    "conf/mariadb/erpnext.cnf": "conf/mariadb/erpnext.cnf",
    "conf/redis/redis.conf": "conf/redis/redis.conf",
    "conf/nginx/00-resolver.sh": "conf/nginx-00-resolver.sh",
    # The bench image's build context. docker-compose.yml names it as a
    # relative path (`context: ./images/bench`), which compose resolves against
    # the compose file -- so it has to exist in the *environment*, not only in
    # the package. Missing this is what the first onboarding run found, and an
    # existing environment never noticed: its image was already built, so the
    # image step never ran again.
    "images/bench/Dockerfile": "images/bench/Dockerfile",
    "images/bench/entrypoint.sh": "images/bench/entrypoint.sh",
}

# Copied with the executable bit, because something runs them.
EXECUTABLE_ASSETS = {"conf/nginx/00-resolver.sh", "images/bench/entrypoint.sh"}


def asset_outputs():
    """The static assets, as the same {relative: (content, mode)} shape."""
    outputs = {}
    for relative, source in STATIC_ASSETS.items():
        content = (ASSET_DIR / source).read_text(encoding="utf-8")
        mode = 0o755 if relative in EXECUTABLE_ASSETS else None
        outputs[relative] = (content, mode)
    return outputs


def write_outputs(outputs, root, quiet=False):
    written = []
    for relative, (content, mode) in outputs.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.is_file() else None
        target.write_text(content, encoding="utf-8")
        if mode is not None:
            os.chmod(target, mode)
        state = "unchanged" if existing == content else ("updated" if existing else "created")
        written.append((relative, state))
        if not quiet:
            print("  {:<9} {}".format(state, relative))
    return written


def check_outputs(outputs, root):
    """Render to memory, diff against disk.  Returns a list of drift reports."""
    drift = []
    for relative, (content, _mode) in outputs.items():
        target = root / relative
        if not target.is_file():
            drift.append("{}: missing; run `erp-sandbox render`".format(relative))
            continue
        on_disk = target.read_text(encoding="utf-8")
        if on_disk == content:
            continue
        diff = difflib.unified_diff(
            on_disk.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile="{} (on disk)".format(relative),
            tofile="{} (from config.toml)".format(relative),
        )
        # A diff of .env would print resolved passwords into the terminal.
        if relative == ".env":
            drift.append(
                ".env: differs from config.toml; diff suppressed because it "
                "contains resolved secrets. Run erp-sandbox render."
            )
        else:
            drift.append("".join(diff).rstrip())
    return drift


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render every generated file from config.toml.",
    )
    parser.add_argument("--config", required=True,
                        help="path to config.toml")
    parser.add_argument("--root", required=True,
                        help="the project root: where generated files are written")
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit non-zero if disk differs from config.toml")
    parser.add_argument("--list", action="store_true",
                        help="print the generated file paths and exit")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()

    try:
        cfg = load_config(args.config)
        # Secrets come from the shell first, then the secrets file named in
        # [secrets].file. render.py never reads .env for input -- it writes it.
        #
        # Only the SECRETS_BUILD group: the two passwords that end up in .env.
        # Git credentials belong to the fetch step, and demanding them here
        # would make a fresh checkout unrenderable over a token render never
        # presents to anything.
        secrets, warnings, _sources = resolve_all_secrets(
            cfg, args.config, groups=(SECRETS_BUILD,),
        )
    except ConfigError as exc:
        sys.stderr.write("config.toml is not usable:\n")
        for problem in exc.problems:
            sys.stderr.write("  - {}\n".format(problem))
        return 2

    uid, gid = detect_ids()
    outputs = build_outputs(cfg, secrets, uid, gid)
    outputs.update(asset_outputs())

    if args.list:
        for relative in outputs:
            print(relative)
        return 0

    if args.check:
        drift = check_outputs(outputs, root)
        if drift:
            sys.stderr.write("generated files have drifted from config.toml:\n\n")
            for report in drift:
                sys.stderr.write(report + "\n\n")
            return 1
        if not args.quiet:
            print("render --check: {} generated file(s) match config.toml".format(len(outputs)))
        return 0

    if not args.quiet:
        print("rendering {} file(s) from {}".format(len(outputs), args.config))
    write_outputs(outputs, root, quiet=args.quiet)
    for warning in warnings:
        sys.stderr.write("warning: {}\n".format(warning))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
