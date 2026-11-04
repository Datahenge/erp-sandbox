"""
erp-sandbox — the command.

Decision #27, and the ergonomic half of R1: a developer's muscle memory is
`bench console`, not

    <engine> exec -it <project>-bench bench console

Every verb here either drives one of the bootstrap steps or generates the
engine invocation a developer would otherwise have to remember. Which client
environment is in play comes from the working directory, the way `bench` finds
its bench -- see project.py for the two roots that keeps separate.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from erp_sandbox import (
    __version__, build_bench, doctor, fetch_apps, guard, known_versions, project,
    render, seed_db, selftest,
)
from erp_sandbox.preflight import (
    ConfigError,
    ProjectNotFound,
    current_user,
    detect_engine,
    load_config,
)


def fail(message, code=2):
    sys.stderr.write("erp-sandbox: {}\n".format(message))
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# Locating the environment
# ---------------------------------------------------------------------------

def require_project(args):
    """The project root, from --project or by walking up from here."""
    if getattr(args, "project", None):
        root = Path(args.project).resolve()
        if not project.is_project_root(root):
            fail("{} is not an erp-sandbox environment (no {}/ in it){}".format(
                root, project.MARKER, project.legacy_hint(root)))
        return root
    try:
        return project.find_project_root()
    except ProjectNotFound as exc:
        fail(str(exc))


def require_config(root):
    path = project.config_path(root)
    if not path.is_file():
        fail("{} has a {}/ but no config.toml.\n"
             "  Copy the sample:  erp-sandbox init --here".format(root, project.MARKER))
    return path


def engine_or_fail():
    engine, compose = detect_engine()
    if engine is None:
        fail("no container engine found. Install podman or docker, then run "
             "`erp-sandbox preflight`.")
    if compose is None:
        fail("{} is installed but its compose front end is not.".format(engine))
    return engine, compose


def compose_files(root, engine):
    """(base, overlay) for an engine. Order matters: the overlay is applied on top."""
    return Path(root) / "docker-compose.yml", Path(root) / "compose.{}.yml".format(engine)


def compose_args(root, engine, compose):
    base, overlay = compose_files(root, engine)
    for path in (base, overlay):
        if not path.is_file():
            fail("{} is missing. Run `erp-sandbox render` first.".format(path.name))
    return compose.split() + ["-f", str(base), "-f", str(overlay)]


def container_name(root, service):
    """
    <project>-<service>, matching container_name in the compose file.

    Read from config.toml rather than guessed, since [project].name is what the
    compose file interpolates.
    """
    try:
        cfg = load_config(require_config(root))
    except ConfigError as exc:
        fail("config.toml is not usable:\n  - " + "\n  - ".join(exc.problems))
    return "{}-{}".format(cfg["project"]["name"], service)


def run(command, **kwargs):
    """Replace this process where possible, so signals and TTYs behave."""
    if kwargs.pop("exec_", True) and shutil.which(command[0]):
        sys.stdout.flush()
        os.execvp(command[0], command)
    return subprocess.run(command, **kwargs).returncode


# ---------------------------------------------------------------------------
# Verbs that drive the bootstrap steps
# ---------------------------------------------------------------------------

def cmd_preflight(args):
    from erp_sandbox import preflight
    root = require_project(args)
    return preflight.main(["--config", str(require_config(root))]
                          + (["--skip-engine"] if args.skip_engine else [])
                          + (["--skip-ports"] if args.skip_ports else [])
                          + (["--skip-images"] if args.skip_images else []))


def cmd_render(args):
    root = require_project(args)
    argv = ["--config", str(require_config(root)), "--root", str(root)]
    if args.check:
        argv.append("--check")
    if args.list:
        argv.append("--list")
    if args.quiet:
        argv.append("--quiet")
    return render.main(argv)


def cmd_fetch(args):
    root = require_project(args)
    argv = ["--config", str(require_config(root)), "--root", str(root)]
    if args.only:
        argv += ["--only", args.only]
    if args.dry_run:
        argv.append("--dry-run")
    return fetch_apps.main(argv)


def cmd_build_bench(args):
    root = require_project(args)
    argv = ["--config", str(require_config(root)), "--root", str(root)]
    if args.skip_assets:
        argv.append("--skip-assets")
    if args.dry_run:
        argv.append("--dry-run")
    return build_bench.main(argv)


def cmd_seed(args):
    root = require_project(args)
    argv = ["--config", str(require_config(root)), "--root", str(root)]
    if args.force:
        argv.append("--force")
    if args.dry_run:
        argv.append("--dry-run")
    return seed_db.main(argv)


def cmd_restore(args):
    """
    Restore a dump given on the command line, replacing the site.

    The site step's restore path -- drop, recreate, restore, re-key, prune the
    grants the restore leaves, migrate -- with the dump named here rather than
    in config.toml. Running `bench restore` directly skips the grant pruning
    and the version comparison (#39), and needs the root password typed or on a
    command line.
    """
    root = require_project(args)
    argv = ["--config", str(require_config(root)), "--root", str(root),
            "--dump", args.dump]
    if args.with_public_files:
        argv += ["--public-files", args.with_public_files]
    if args.with_private_files:
        argv += ["--private-files", args.with_private_files]
    if args.force:
        argv.append("--force")
    if args.dry_run:
        argv.append("--dry-run")
    return seed_db.main(argv)


def backup_command(engine, container, site, with_files):
    """The argv for `bench backup` in the running bench container."""
    command = [engine, "exec", container, "bench", "--site", site, "backup"]
    if with_files:
        command.append("--with-files")
    return command


def cmd_backup(args):
    """`bench backup` for this environment's site, and where the files landed."""
    import time

    root = require_project(args)
    site = _site(root)
    engine, _compose = engine_or_fail()
    started = time.time()
    code = run(backup_command(engine, container_name(root, "bench"), site, args.with_files),
               exec_=False)
    if code != 0:
        return code
    # bench/sites is bind-mounted, so the backup is already on the host.
    backups = Path(root) / "bench" / "sites" / site / "private" / "backups"
    new = sorted(p for p in backups.glob("*") if p.is_file() and p.stat().st_mtime >= started - 1)
    print("\nbackup files, on the host:")
    for path in new:
        print("  {}  ({:.1f} MB)".format(path, path.stat().st_size / 1e6))
    if not new:
        print("  none found in {}; bench may have written elsewhere".format(backups))
    return 0


def cmd_doctor(args):
    root = require_project(args)
    return doctor.run(root, require_config(root), skip_containers=args.no_containers)


def cmd_selftest(_args):
    return selftest.main()


def cmd_guard_edit(_args):
    """
    The Claude Code PreToolUse hook (decision #35).

    Deliberately does not call `require_project`: which environment is in play
    comes from the file being written, not from the working directory, and a
    hook that exits 2 because it was run outside an environment would look to
    Claude Code exactly like a refusal. Everything here fails open -- see
    guard.py.
    """
    return guard.main()


# ---------------------------------------------------------------------------
# Verbs that drive the engine
# ---------------------------------------------------------------------------

def cmd_up(args):
    root = require_project(args)
    engine, compose = engine_or_fail()
    # Also here, not only in the infra step: every doctor remedy for a stopped
    # stack says `erp-sandbox up`, and following that advice on a tree whose
    # data/mariadb is missing is exactly when the engine would create it.
    project.ensure_data_dirs(root)
    command = compose_args(root, engine, compose) + ["up", "-d"]
    if args.recreate:
        # The way to apply a changed [bench].env_file: an env_file is read when
        # a container is created, so `restart` keeps the old values.
        command.append("--force-recreate")
    command += list(args.services)
    return run(command, cwd=str(root), exec_=False)


def cmd_down(args):
    root = require_project(args)
    engine, compose = engine_or_fail()
    return run(compose_args(root, engine, compose) + ["down"], cwd=str(root), exec_=False)


def cmd_proxy_snippet(args):
    """
    The Caddyfile block for this environment, on stdout (decision #43).

    Printed, never installed: Caddy belongs to the VPS, not to any developer,
    so the admin decides where it goes. Needs [site].public_url, which is the
    hostname Caddy answers for.
    """
    root = require_project(args)
    try:
        cfg = load_config(require_config(root))
    except ConfigError as exc:
        fail("config.toml is not usable:\n  - " + "\n  - ".join(exc.problems))
    snippet = render.proxy_snippet(cfg, root, current_user())
    if snippet is None:
        fail("[site].public_url is not set in {}. It names the subdomain Caddy "
             "serves this environment on, e.g. \"https://acme.dev.example.com\"."
             .format(project.config_path(root)))
    sys.stdout.write(snippet)
    return 0


def cmd_restart(args):
    root = require_project(args)
    engine, compose = engine_or_fail()
    services = list(args.services) or ["bench"]
    return run(compose_args(root, engine, compose) + ["restart"] + services,
               cwd=str(root), exec_=False)


def cmd_logs(args):
    root = require_project(args)
    engine, compose = engine_or_fail()
    command = compose_args(root, engine, compose) + ["logs"]
    if args.follow:
        command.append("-f")
    command += list(args.services) or ["bench"]
    return run(command, cwd=str(root), exec_=False)


def cmd_ps(args):
    root = require_project(args)
    engine, compose = engine_or_fail()
    return run(compose_args(root, engine, compose) + ["ps"], cwd=str(root), exec_=False)


def exec_command(engine, name, inner, tty=True):
    """
    The argv for running something inside a container.

    Split out from _exec_in so it can be tested without an engine, a container,
    or a terminal. The verbs that shell out are mostly *argument construction*,
    and that is the part that has actually been wrong.
    """
    flags = ["-it"] if tty else ["-i"]
    return [engine, "exec"] + flags + [name] + list(inner)


def _exec_in(root, service, inner, tty=True):
    engine, _compose = engine_or_fail()
    name = container_name(root, service)
    return run(exec_command(engine, name, inner, tty and sys.stdin.isatty()))


def cmd_bench(args):
    """`erp-sandbox bench <anything>` -- the verb this whole phase exists for."""
    root = require_project(args)
    if not args.rest:
        fail("erp-sandbox bench needs a bench command, e.g. `erp-sandbox bench console`")
    return _exec_in(root, "bench", ["bench"] + list(args.rest))


def cmd_shell(args):
    root = require_project(args)
    return _exec_in(root, args.service, [args.shell])


def mariadb_command(site_config, common_config, rest, interactive):
    """
    The argv for a mysql client on the site database, and the environment it
    needs.

    Deliberately not `bench --site X mariadb`. That command is an os.execv with
    a *hardcoded* argv (frappe/commands/utils.py), so it can never take an
    argument -- `erp-sandbox mariadb -e "select 1"` through bench dies on
    "no such option: -e". Going straight to the client is what makes a scripted
    query possible.

    It also fixes a smaller thing: frappe passes the password as -p<password>,
    which the container's process list shows to anything running in it for the
    life of the call. MYSQL_PWD is read from the environment instead, the same
    way doctor's grant check does it.

    Returned rather than run so it can be tested without a container.
    """
    db_name = site_config.get("db_name")
    if not db_name:
        return None, None
    argv = [
        "mariadb",
        "-u", db_name,
        "-h", str(common_config.get("db_host", "mariadb")),
        "-P", str(common_config.get("db_port", 3306)),
    ]
    if interactive and not rest:
        # frappe's own niceties, but only for a session a human is sitting at.
        # --pager mangles the output of -e, and there is no terminal to page to
        # when the caller is a script.
        argv += ["--pager=less -SFX", "--safe-updates", "-A"]
    argv.append(db_name)
    argv += list(rest)
    return argv, {"MYSQL_PWD": site_config.get("db_password", "")}


def cmd_mariadb(args):
    root = require_project(args)
    site = _site(root)
    site_config = Path(root) / "bench" / "sites" / site / "site_config.json"
    if not site_config.is_file():
        fail("no site_config.json for {!r}. Has the site been created?\n"
             "  erp-sandbox build --from site".format(site))
    try:
        conf = json.loads(site_config.read_text(encoding="utf-8"))
        common = json.loads(
            (Path(root) / "bench" / "sites" / "common_site_config.json")
            .read_text(encoding="utf-8"))
    except ValueError as exc:
        fail("could not read the site configuration: {}".format(exc))

    tty = sys.stdin.isatty()
    argv, env = mariadb_command(conf, common, args.rest, tty)
    if argv is None:
        fail("{} names no db_name. The site is not usable.".format(site_config))

    engine, _compose = engine_or_fail()
    # `-e NAME` with no value: docker and podman both take the value from this
    # process's environment, so the password is in no command line -- not the
    # container's, and not the engine client's, which `ps` shows to every user.
    command = [engine, "exec"] + (["-it"] if tty else ["-i"])
    for key, value in env.items():
        command += ["-e", key]
        os.environ[key] = value
    command += [container_name(root, "bench")] + argv
    return run(command)


def _site(root):
    try:
        return load_config(require_config(root))["project"]["site"]
    except ConfigError as exc:
        fail("config.toml is not usable:\n  - " + "\n  - ".join(exc.problems))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args):
    """
    Scaffold a client environment: the marker, a config to edit, a secrets file.

    Deliberately does not run anything. A developer's next step is to edit
    config.toml, and a command that had already started cloning would be
    working from a config nobody had filled in.
    """
    if not args.here and not args.name:
        fail("erp-sandbox init needs a directory name, or --here")
    if not known_versions.is_supported(args.frappe):
        fail(known_versions.unsupported_message(args.frappe))
    target = Path.cwd() if args.here else project.init_target(args.name)
    target = target.resolve()

    if project.is_project_root(target):
        fail("{} is already an erp-sandbox environment.".format(target))
    # Decision #51. Scaffolding into a directory that already holds something
    # writes config.toml, .erp-sandbox/ and .gitignore among files that are not
    # an environment's. --here is the explicit way to say that is intended.
    if target.exists() and not target.is_dir():
        fail("{} exists and is not a directory.".format(target))
    if not args.here and target.is_dir() and any(target.iterdir()):
        fail("{} already exists and is not empty. Choose another name, or run "
             "`erp-sandbox init --here` inside it if it really should become an "
             "environment.".format(target))

    target.mkdir(parents=True, exist_ok=True)
    state = project.state_dir(target)
    state.mkdir(exist_ok=True)

    created = [".erp-sandbox/"]

    config = project.config_path(target)
    if not config.exists():
        text = seed_series(project.SAMPLE_CONFIG.read_text(encoding="utf-8"),
                           args.frappe)
        # Record the version that created this environment, so a later downgrade
        # of the shared tool is refused rather than discovered halfway through.
        text = re.sub(r'^min_version = "[^"]*"',
                      'min_version = "{}"'.format(__version__),
                      text, count=1, flags=re.M)
        # Seed the pip constraints for the series the sample names (decision
        # #36). The value lives in known_versions.py as a template and is
        # written here, into the file the developer owns -- so it is visible on
        # day one and editable, rather than an invisible default that only
        # announces itself as a pip resolution failure mid-build.
        #
        # Substituted rather than left to the sample's literal so that a series
        # added later seeds its own constraints without the sample having to
        # carry every series' answer. selftest asserts the two agree today.
        series = re.search(r"^frappe_version\s*=\s*(\d+)", text, flags=re.M)
        if series:
            template = known_versions.constraints_template(int(series.group(1)))
            text = re.sub(
                r"^pip_constraints = \[[^\]]*\]",
                "pip_constraints = [{}]".format(
                    ", ".join('"{}"'.format(c) for c in template)),
                text, count=1, flags=re.M)
        config.write_text(text, encoding="utf-8")
        created.append("config.toml")

    secrets = project.default_secrets_path(target)
    if not secrets.exists():
        shutil.copyfile(project.SAMPLE_SECRETS, secrets)
        os.chmod(secrets, 0o600)
        created.append(".erp-sandbox/secrets.env  (0600)")

    # A sample, not the file: [bench].env_file is optional, and an empty file
    # loaded into the container would only be noise.
    bench_sample = project.state_dir(target) / project.SAMPLE_BENCH_ENV.name
    if not bench_sample.exists():
        shutil.copyfile(project.SAMPLE_BENCH_ENV, bench_sample)
        created.append(".erp-sandbox/{}".format(project.SAMPLE_BENCH_ENV.name))

    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE, encoding="utf-8")
        created.append(".gitignore")

    # Seeded once and never rewritten, so notes about this client survive every
    # render. What erp-sandbox has to say lives in the rendered rule file beside
    # it, which is how the briefing stays current as the tool changes.
    claude_md = target / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(ENV_CLAUDE_MD, encoding="utf-8")
        created.append("CLAUDE.md")

    print("created {} in {}".format(", ".join(created), target))
    print()
    print("Next:")
    print("  1. cd {}".format(target))
    print("  2. edit {}  -- repos, refs, ports, versions".format(
        project.config_path(target)))
    print("  3. edit {}  -- passwords and any git token".format(
        project.default_secrets_path(target)))
    print("  4. erp-sandbox preflight          -- check the host and the config")
    print("  5. erp-sandbox build              -- everything else")
    return 0


def seed_series(text, series):
    """
    The sample config with one Frappe series' toolchain written in (#56).

    The sample carries v12's values. For another series every [stack] pin,
    frappe_version and the frappe/erpnext refs are replaced from that series'
    entry in known_versions.py, so a new environment starts from a set that
    builds rather than from v12 edited by hand. Written into the file, not
    applied later: config.toml stays the whole answer (decision #9).
    """
    known = known_versions.get(series)
    text = re.sub(r"^frappe_version = \d+", "frappe_version = {}".format(series),
                  text, count=1, flags=re.M)
    for key, value in known["stack_defaults"].items():
        text, count = re.subn(r'^({}\s*=\s*)"[^"]*"'.format(re.escape(key)),
                              lambda m, value=value: '{}"{}"'.format(m.group(1), value),
                              text, count=1, flags=re.M)
        if count != 1:
            raise AssertionError("config.sample.toml has no [stack].{} to seed".format(key))
    # Only uncommented refs: the commented example apps keep theirs.
    return re.sub(r'^(ref\s*=\s*)"version-\d+"',
                  lambda m: '{}"{}"'.format(m.group(1), known["app_ref"]),
                  text, flags=re.M)


GITIGNORE = """\
# An erp-sandbox environment. Everything here except config.toml is either
# generated, downloaded, or machine-local.

# Machine-local state and secrets.
/.erp-sandbox/

# Generated by `erp-sandbox render`. CLAUDE.md beside it is yours -- keep it,
# and put your own editor settings in .vscode/, which erp-sandbox never writes.
/.claude/rules/erp-sandbox-environment.md
/.claude/settings.json
/*.code-workspace

# Generated by `erp-sandbox render` -- never hand-edited.
/.env
/docker-compose.yml
/compose.podman.yml
/compose.docker.yml
/conf/
/images/
/bench/Procfile
/bench/sites/common_site_config.json
/bench/sites/apps.txt

# Cloned source and runtime data.
/bench/
/data/
*.log
"""


ENV_CLAUDE_MD = """\
# Notes for this environment

This file is yours. erp-sandbox seeds it once and never rewrites it, so anything
you record here survives every `erp-sandbox render`.

What erp-sandbox has to say about working in this environment -- the verbs, and
the things that waste an afternoon -- is generated into
`.claude/rules/erp-sandbox-environment.md`, which loads automatically alongside
this file. Do not edit that one; edit `config.toml` and re-render.

## About this client
"""


# ---------------------------------------------------------------------------
# build: the one command
# ---------------------------------------------------------------------------

BUILD_STEPS = ("preflight", "render", "fetch", "image", "infra", "bench", "site", "start")


def build_steps(from_step=None, only=None):
    """
    The steps a build runs, render always among them once it is past render.

    A resumed build re-renders first (decision #58). The failure message says
    "fix it, then resume", and the usual fix is an edit to config.toml -- which
    reaches the engine only through the files render writes. Skipping render on
    resume retried the old value and failed the same way. Rendering is fast and
    idempotent, so there is no case for skipping it.
    """
    steps = list(BUILD_STEPS)
    for name in (from_step, only):
        if name and name not in BUILD_STEPS:
            fail("unknown step {!r}. Steps: {}".format(name, ", ".join(BUILD_STEPS)))
    if from_step:
        steps = steps[steps.index(from_step):]
    if only:
        steps = [only]
    if "render" not in steps and BUILD_STEPS.index(steps[0]) > BUILD_STEPS.index("render"):
        steps.insert(0, "render")
    return steps


def cmd_build(args):
    root = require_project(args)
    config = require_config(root)
    engine, compose = engine_or_fail()

    steps = build_steps(args.from_step, args.only)

    print("building {} -- steps: {}".format(root.name, " -> ".join(steps)))
    if args.dry_run:
        return 0

    from erp_sandbox import preflight

    for step in steps:
        print("\n=== {} ===".format(step))
        if step == "preflight":
            code = preflight.main(["--config", str(config)])
        elif step == "render":
            code = render.main(["--config", str(config), "--root", str(root)])
        elif step == "fetch":
            code = fetch_apps.main(["--config", str(config), "--root", str(root)])
        elif step == "image":
            code = subprocess.run(compose_args(root, engine, compose) + ["build", "bench"],
                                  cwd=str(root)).returncode
        elif step == "infra":
            # Before the engine can invent it as root. See project.DATA_DIRS.
            for relative in project.ensure_data_dirs(root):
                print("  created {}".format(relative))
            code = subprocess.run(compose_args(root, engine, compose) + ["up", "-d", "mariadb", "redis"],
                                  cwd=str(root)).returncode
        elif step == "bench":
            code = build_bench.main(["--config", str(config), "--root", str(root)])
        elif step == "site":
            code = seed_db.main(["--config", str(config), "--root", str(root)]
                                + (["--force"] if args.force else []))
        elif step == "start":
            code = subprocess.run(compose_args(root, engine, compose) + ["up", "-d"],
                                  cwd=str(root)).returncode
        if code != 0:
            sys.stderr.write("\nbuild stopped at step {!r}.\n"
                             "Fix it, then resume with:  erp-sandbox build --from {}\n"
                             .format(step, step))
            return code
    print("\nbuild complete.")
    return 0


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="erp-sandbox",
        description="Build and drive a containerized ERPNext development environment.",
        epilog="Every verb except `init` acts on the environment containing the "
               "working directory. Override with --project.",
    )
    # The package path, not just the number. An editable install and a copied
    # one report the same version and behave differently, and the only way to
    # tell them apart used to be importing erp-sandbox from a neutral directory
    # under the tool venv's own interpreter -- which is not a thing anyone
    # remembers. `import erp_sandbox` under the *system* python cannot answer it at
    # all: a pipx or uv tool venv is not on its path.
    parser.add_argument("--version", action="version",
                        version="erp-sandbox {} ({})".format(
                            __version__, project.PACKAGE_ROOT))
    parser.add_argument("--project", metavar="DIR",
                        help="act on this environment instead of the one above the "
                             "working directory")
    sub = parser.add_subparsers(dest="verb", metavar="<verb>")

    def add(name, func, help_text, **kwargs):
        p = sub.add_parser(name, help=help_text, description=help_text, **kwargs)
        p.set_defaults(func=func)
        return p

    p = add("init", cmd_init, "scaffold a new environment")
    p.add_argument("name", nargs="?",
                   help="a bare name goes under ~/{}/; a path is used as "
                        "given".format(project.ENVS_DIRNAME))
    p.add_argument("--here", action="store_true", help="use the current directory")
    p.add_argument("--frappe", type=int, default=12, metavar="SERIES",
                   help="the Frappe series to write a toolchain for: {} "
                        "(default 12)".format(", ".join(
                            str(v) for v in known_versions.SUPPORTED)))

    p = add("build", cmd_build, "run the whole build, or resume it")
    p.add_argument("--from", dest="from_step", metavar="STEP",
                   help="resume from this step: " + ", ".join(BUILD_STEPS))
    p.add_argument("--only", metavar="STEP", help="run just this step")
    p.add_argument("--force", action="store_true", help="let the site step replace an existing site")
    p.add_argument("--dry-run", action="store_true")

    p = add("preflight", cmd_preflight, "check the host and validate config.toml")
    p.add_argument("--skip-engine", action="store_true")
    p.add_argument("--skip-ports", action="store_true")
    p.add_argument("--skip-images", action="store_true",
                   help="do not ask Docker Hub whether the image tags exist")

    p = add("render", cmd_render, "regenerate every generated file from config.toml")
    p.add_argument("--check", action="store_true", help="report drift instead of writing")
    p.add_argument("--list", action="store_true",
                   help="print the generated file paths and exit, writing nothing")
    p.add_argument("--quiet", action="store_true")

    p = add("fetch", cmd_fetch, "clone or update the apps named in config.toml")
    p.add_argument("--only", metavar="NAME")
    p.add_argument("--dry-run", action="store_true")

    p = add("build-bench", cmd_build_bench, "create bench/env and install the apps")
    p.add_argument("--skip-assets", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = add("seed", cmd_seed, "create the site and seed its database")
    p.add_argument("--force", action="store_true", help="replace an existing site")
    p.add_argument("--dry-run", action="store_true")

    p = add("restore", cmd_restore,
            "replace the site with a dump given here: erp-sandbox restore <dump.sql.gz> --force")
    p.add_argument("dump", help="the database dump, .sql or .sql.gz")
    p.add_argument("--with-public-files", metavar="TAR", help="a public files tarball")
    p.add_argument("--with-private-files", metavar="TAR", help="a private files tarball")
    p.add_argument("--force", action="store_true",
                   help="replace the existing site. Destroys its database.")
    p.add_argument("--dry-run", action="store_true")

    p = add("backup", cmd_backup, "run bench backup for the site; files land under bench/sites/")
    p.add_argument("--with-files", action="store_true",
                   help="include the public and private files")

    p = add("doctor", cmd_doctor, "diagnose the environment: 0 healthy, 1 degraded, 2 broken")
    p.add_argument("--no-containers", action="store_true",
                   help="skip everything that needs the stack running")

    add("selftest", cmd_selftest, "run the config-layer test suite")

    add("guard-edit", cmd_guard_edit,
        "decide whether a file may be edited; reads a Claude Code hook "
        "payload on stdin (decision #35)")

    p = add("bench", cmd_bench,
            "run a bench command in the bench container: erp-sandbox bench console")
    p.add_argument("rest", nargs=argparse.REMAINDER, metavar="...")

    p = add("shell", cmd_shell, "open a shell in a container")
    p.add_argument("service", nargs="?", default="bench")
    p.add_argument("--shell", default="bash")

    p = add("mariadb", cmd_mariadb,
            "open a mariadb client on the site database, or run a query: "
            'erp-sandbox mariadb -e "select 1"')
    p.add_argument("rest", nargs=argparse.REMAINDER, metavar="...")

    p = add("up", cmd_up, "start the stack")
    p.add_argument("services", nargs="*")
    p.add_argument("--recreate", action="store_true",
                   help="recreate the containers, which applies a changed [bench].env_file")

    add("down", cmd_down, "stop the stack and remove its containers")

    p = add("restart", cmd_restart, "restart a service (default: bench)")
    p.add_argument("services", nargs="*")

    p = add("logs", cmd_logs, "show container logs")
    p.add_argument("services", nargs="*")
    p.add_argument("-f", "--follow", action="store_true")

    add("ps", cmd_ps, "list the stack's containers")

    add("proxy-snippet", cmd_proxy_snippet,
        "print the Caddyfile block that serves this environment on a VPS")

    return parser


# Verbs whose arguments belong to something else entirely and must not be
# parsed here. argparse.REMAINDER is not enough on its own: a first token that
# looks like a flag -- `erp-sandbox bench --site x execute y`, or
# `erp-sandbox mariadb -e "select 1"` -- is matched against this parser's own
# options before REMAINDER ever applies, and the developer gets "unrecognized
# arguments: --site" for a flag that was never ours.
PASSTHROUGH_VERBS = {"bench", "mariadb"}

# Global options that take a value, so their value is not mistaken for a verb.
GLOBAL_OPTIONS_WITH_VALUE = {"--project"}


def split_passthrough(argv):
    """
    (argv for us, argv for the inner command).

    The verb is the *first positional* -- not merely the first token that spells
    one. `erp-sandbox build --from bench --force` contains "bench" as the value of
    --from, and an earlier version split there, handing --force to a bench
    command that was never going to run and silently dropping it from the build.
    The build then refused to replace an existing site while reporting that
    --force would fix it, which it had just discarded.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            if token in GLOBAL_OPTIONS_WITH_VALUE and "=" not in token:
                index += 2          # skip the option and its value
            else:
                index += 1
            continue
        # The first positional is the verb, whatever it is.
        if token in PASSTHROUGH_VERBS:
            return argv[:index + 1], argv[index + 1:]
        return argv, []
    return argv, []


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, passthrough = split_passthrough(argv)

    parser = build_parser()
    args = parser.parse_args(argv)
    if passthrough:
        args.rest = passthrough
    if not getattr(args, "verb", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
