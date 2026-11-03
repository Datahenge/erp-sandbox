#!/usr/bin/env python3
"""
selftest.py — prove the config layer accepts what it should and rejects what it
should, with a message that says why.

Phase 1's checkpoint is "preflight rejects a deliberately broken TOML with a
useful message". That is only worth anything if it is repeatable, so the broken
TOMLs live in erp_sandbox/fixtures/ and here rather than being retyped by hand
each time somebody wonders.

Standard library only, like everything else in the package -- the default
answer per decision #18, which decision #27 relaxed from a prohibition to a
preference. No pytest:

    erp-sandbox selftest

Two kinds of rejection case, and erp_sandbox/fixtures/README.md explains why:
standalone files for configurations that cannot be derived from the sample, and
MUTATIONS below -- one-line edits applied to config.sample.toml at run time --
for everything else, so the schema keeps exactly one home.
"""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


from erp_sandbox import fetch_apps  # noqa: E402
from erp_sandbox import render  # noqa: E402
from erp_sandbox.preflight import (  # noqa: E402
    ConfigError,
    load_config,
)

from erp_sandbox import project  # noqa: E402
from erp_sandbox.project import FIXTURE_DIR, SAMPLE_CONFIG as SAMPLE  # noqa: E402

# The sample's two build-group secrets. Set explicitly in every test that
# renders, so the result does not depend on whether the developer running the
# suite happens to have a secrets.env or an exported shell variable.
BUILD_SECRETS = {"DB_ROOT_PASSWORD": "not-a-real-password",
                 "ADMIN_PASSWORD": "not-a-real-password"}

# Names that must NOT be set while rendering, or the point of the first case is
# lost: render.py must not need a git credential it never presents.
FETCH_SECRETS = ("GIT_PAT", "GITLAB_PAT")


# ---------------------------------------------------------------------------
# Cases: (name, old_text, new_text, expected message substrings)
#
# old_text is replaced once in config.sample.toml. If it is not found the case
# fails loudly -- see fixtures/README.md on why that is the right failure.
# ---------------------------------------------------------------------------

_FRAPPE_ENTRY = '''name                 = "frappe"
repo                 = "https://github.com/frappe/frappe.git"
ref                  = "version-12"
requires_credentials = false'''

MUTATIONS = [
    (
        "ssh remote with requires_credentials = true",
        _FRAPPE_ENTRY,
        '''name                 = "frappe"
repo                 = "git@github.com:acme/frappe.git"
ref                  = "version-12"
requires_credentials = true''',
        ["ssh remote but requires_credentials is true",
         "presented over https only"],
    ),
    (
        "https remote needing a credential with no [credentials] entry",
        _FRAPPE_ENTRY,
        _FRAPPE_ENTRY.replace("requires_credentials = false",
                              "requires_credentials = true"),
        ['requires_credentials = true but there is no [credentials."github.com"] entry'],
    ),
    (
        # The sample already sets blobless on frappe (decision #31), so asking
        # for a credential is enough to make the combination.
        "blobless on an app that needs a credential",
        _FRAPPE_ENTRY,
        _FRAPPE_ENTRY.replace("requires_credentials = false",
                              "requires_credentials = true"),
        ["blobless = true is refused for an app that needs a credential",
         "long after the token has left the environment"],
    ),
    (
        "a pip constraint written with spaces",
        'pip_constraints = ["numpy<1.24"]',
        'pip_constraints = ["numpy < 1.24"]',
        ["stack.pip_constraints entry 'numpy < 1.24'", "not \"numpy < 1.24\""],
    ),
    (
        "a pip constraint with no version specifier",
        'pip_constraints = ["numpy<1.24"]',
        'pip_constraints = ["numpy"]',
        ["stack.pip_constraints entry 'numpy'",
         "a constraint without a specifier caps nothing"],
    ),
    (
        "pip_constraints given a string instead of a list",
        'pip_constraints = ["numpy<1.24"]',
        'pip_constraints = "numpy<1.24"',
        ["stack.pip_constraints must be a list of pip constraint strings"],
    ),
    (
        "blobless given something that is not a boolean",
        "blobless             = true       # most of this clone",
        'blobless             = "yes"      # most of this clone',
        ["apps.erpnext.blobless must be true or false"],
    ),
    (
        "the pre-#24 key name",
        "requires_credentials = false\ninstall              = true",
        "private              = false\ninstall              = true",
        ["private has been renamed to requires_credentials"],
    ),
    (
        "a local path where a remote belongs",
        'repo                 = "https://github.com/frappe/frappe.git"',
        'repo                 = "/home/someone/frappe"',
        ["is not a recognised git remote", "user@host:path"],
    ),
    (
        "frappe not first",
        _FRAPPE_ENTRY,
        _FRAPPE_ENTRY.replace('"frappe"', '"erpnext"', 1)
                     .replace("frappe/frappe.git", "frappe/erpnext.git"),
        ["the first [[apps]] entry must be frappe"],
    ),
    (
        "an app listed twice",
        'name                 = "erpnext"',
        'name                 = "frappe"',
        ["is listed more than once"],
    ),
    (
        "an app name that is not a python module name",
        'name                 = "erpnext"',
        'name                 = "Acme-App"',
        ["does not match"],
    ),
    (
        "an unfilled placeholder",
        'site     = "site_acme1"',
        'site     = "FILL-ME"',
        ["still has blanks to fill in", "project.site is still FILL-ME"],
    ),
    (
        "both host ports the same",
        "mariadb = 3316",
        "mariadb = 8101",
        ["ports.http and ports.mariadb are both 8101"],
    ),
    (
        "two internal services on one port",
        "socketio     = 9000",
        "socketio     = 8000",
        ["share port 8000"],
    ),
    (
        "a seed mode that does not exist",
        'seed           = "new_site"',
        'seed           = "restore_from_prod"',
        ["is not one of", "new_site"],
    ),
    (
        "a dump restore with no dump and no encryption key",
        'seed           = "new_site"',
        'seed           = "sql_dump"',
        ["requires database.dump_path",
         "requires database.encryption_key",
         "silently unreadable"],
    ),
    (
        "a port outside the valid range",
        "http    = 8101",
        "http    = 99999",
        ["is above the maximum of 65535"],
    ),
    (
        # The scp-style colon left in an https URL. The host still parses, so
        # without a dedicated check this reaches git and fails there.
        "an https url with the ssh colon left in",
        'repo                 = "https://github.com/frappe/erpnext.git"',
        'repo                 = "https://github.com:frappe/erpnext.git"',
        ["where a port number belongs",
         "Did you mean 'https://github.com/frappe/erpnext.git'?"],
    ),
    (
        "[credentials] keyed by app name instead of host",
        "[database]",
        '[credentials."erpnext"]\nusername = "someone"\ntoken = "env:GIT_PAT"\n\n[database]',
        ["is not keyed by a git host",
         "That is an app name",
         "here 'github.com'"],
    ),
    (
        "[credentials] keyed by something that is not a hostname",
        "[database]",
        '[credentials."work-laptop"]\nusername = "someone"\ntoken = "env:GIT_PAT"\n\n[database]',
        ["is not keyed by a git host", "keyed by hostname"],
    ),
    (
        # An environment says which erp-sandbox it needs, because one installed
        # tool now serves every environment on the machine.
        "a config that needs a newer erp-sandbox",
        'min_version = "0.1.0"',
        'min_version = "9.9.9"',
        ["needs erp-sandbox 9.9.9 or newer", "Upgrade the tool"],
    ),
    (
        "a min_version that is not a version",
        'min_version = "0.1.0"',
        'min_version = "latest"',
        ["is not a three-part version"],
    ),
    (
        # Decision #28. The tool refuses a Frappe series nobody has analysed,
        # rather than guessing at what its requirements need.
        "an unsupported frappe version",
        "frappe_version = 12",
        "frappe_version = 99",
        ["frappe_version = 99 is not one this tool knows how to build",
         "Adding a version means doing that analysis"],
    ),
    (
        "a frappe version that is not an integer",
        "frappe_version = 12",
        'frappe_version = "v12"',
        ["stack.frappe_version must be an integer"],
    ),
    (
        # Decision #57: the base image decides the Python.
        "a leftover stack.python",
        'node          = "12.22.12"',
        'python        = "3.9"\nnode          = "12.22.12"',
        ["stack.python was removed", "Delete the line"],
    ),
    (
        "a stack node that contradicts the version registry",
        'node          = "12.22.12"',
        'node          = "24.13.0"',
        ["but Frappe v12 needs Node 12", "One of the two is wrong"],
    ),
    (
        # R1: the Dockerfile asks for wkhtmltox_<release>.<codename>_<arch>.deb.
        "a wkhtmltopdf release with no .deb for the base image's Debian",
        'base_image    = "docker.io/library/debian:bullseye-slim"',
        'base_image    = "docker.io/library/debian:bookworm-slim"',
        ["'0.12.6.1-2' has no .deb for bookworm", "Use '0.12.6.1-3'"],
    ),
    (
        "a bench CLI older than the series has been verified with",
        'bench         = "5.24.1"',
        'bench         = "5.20.0"',
        ["stack.bench = '5.20.0' is older than 5.24.1"],
    ),
    (
        "an extra_packages entry that is not a package name",
        "extra_packages = []",
        'extra_packages = ["libfoo-dev; rm -rf /"]',
        ["is not a Debian package name", "apt-get argument"],
    ),
    (
        "a boolean written as a string",
        "install              = true\nblobless             = true       # history",
        "install              = \"yes\"\nblobless             = true       # history",
        ["install must be true or false"],
    ),
    (
        # Decision #46. It becomes the host half of a compose `ports:` entry.
        "a bind address that is not an IPv4 address",
        '# bind  = "127.0.0.1"',
        'bind    = "localhost"',
        ["ports.bind = 'localhost' is not an IPv4 address"],
    ),
    (
        # Decision #41: Frappe cannot be served under a path.
        "a public_url with a path",
        '# public_url = "https://acme.dev.example.com"',
        'public_url = "https://dev.example.com/acme"',
        ["site.public_url", "must be https://<hostname>", "cannot be served under a path"],
    ),
    (
        "a public_url over plain http",
        '# public_url = "https://acme.dev.example.com"',
        'public_url = "http://acme.dev.example.com"',
        ["site.public_url", "must be https://<hostname>"],
    ),
    (
        "a db_name that would need quoting",
        '# db_name = "acme_dev"',
        'db_name = "acme-dev"',
        ["site.db_name = 'acme-dev'", "letters, digits or _"],
    ),
    (
        "a db_name past MariaDB's 64-character limit",
        '# db_name = "acme_dev"',
        'db_name = "{}"'.format("a" * 65),
        ["site.db_name", "1-64"],
    ),
    (
        "a db_name starting with a digit",
        '# db_name = "acme_dev"',
        'db_name = "1acme"',
        ["site.db_name = '1acme'"],
    ),
]

# Remotes that must be ACCEPTED. Decision #24: ssh is in scope, in both
# spellings git hosts hand out.
ACCEPTED_REMOTES = [
    ("scp-style ssh", 'git@github.com:acme/frappe.git'),
    ("ssh:// url", 'ssh://git@github.com/acme/frappe.git'),
    ("https", 'https://github.com/acme/frappe.git'),
    ("https with a user", 'https://someone@github.com/acme/frappe.git'),
]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Results:
    def __init__(self):
        self.passed = 0
        self.failures = []

    def ok(self, name, detail=""):
        self.passed += 1
        print("[ ok ] {}".format(name))
        if detail:
            print("       {}".format(detail))

    def fail(self, name, detail):
        self.failures.append(name)
        print("[FAIL] {}".format(name))
        for line in str(detail).splitlines():
            print("       {}".format(line))


@contextlib.contextmanager
def environment(**overrides):
    """Set and unset variables around a block. None means 'remove'."""
    saved = {name: os.environ.get(name) for name in overrides}
    try:
        for name, value in overrides.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def build_secrets_only():
    """The build-group secrets present, the fetch-group absent."""
    overrides = dict(BUILD_SECRETS)
    overrides.update({name: None for name in FETCH_SECRETS})
    with environment(**overrides):
        yield


def write_config(directory, text, name="config.toml"):
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path


def expect_rejected(results, name, config_text, expected, directory):
    """The config must raise ConfigError, mentioning every expected substring."""
    path = write_config(directory, config_text)
    try:
        load_config(path)
    except ConfigError as exc:
        combined = "\n".join(exc.problems)
        missing = [want for want in expected if want not in combined]
        if missing:
            results.fail(name, "rejected, but not for the stated reason.\n"
                               "missing from the message: {}\n"
                               "actual:\n  {}".format(
                                   "; ".join(repr(m) for m in missing),
                                   "\n  ".join(exc.problems)))
        else:
            results.ok(name)
        return
    results.fail(name, "accepted a configuration that must be rejected")


# ---------------------------------------------------------------------------
# The suites
# ---------------------------------------------------------------------------

def run_fixture_files(results, workdir):
    """Standalone fixtures. '# expect:' lines carry the required substrings."""
    files = sorted(FIXTURE_DIR.glob("*.toml"))
    if not files:
        results.fail("fixture files", "no *.toml in {}".format(FIXTURE_DIR))
        return
    for path in files:
        text = path.read_text(encoding="utf-8")
        expected = [line.split("# expect:", 1)[1].strip()
                    for line in text.splitlines() if line.startswith("# expect:")]
        if not expected:
            results.fail("fixtures/{}".format(path.name),
                         "no '# expect:' line; every fixture must state what "
                         "message it is testing for")
            continue
        expect_rejected(results, "fixtures/{}".format(path.name),
                        text, expected, workdir)


def run_mutations(results, sample_text, workdir):
    for name, old, new, expected in MUTATIONS:
        if old not in sample_text:
            results.fail(name, "the mutation no longer applies to "
                               "config.sample.toml; update the case.\n"
                               "looked for:\n  {}".format(old.replace("\n", "\n  ")))
            continue
        expect_rejected(results, name, sample_text.replace(old, new, 1),
                        expected, workdir)


def run_accepted_remotes(results, sample_text, workdir):
    original = 'repo                 = "https://github.com/frappe/frappe.git"'
    for name, remote in ACCEPTED_REMOTES:
        label = "accepts a {} remote".format(name)
        if original not in sample_text:
            results.fail(label, "config.sample.toml no longer contains the "
                                "frappe repo line this case edits")
            continue
        text = sample_text.replace(original,
                                   'repo                 = "{}"'.format(remote), 1)
        path = write_config(workdir, text, name="remote.toml")
        try:
            load_config(path)
        except ConfigError as exc:
            results.fail(label, "\n".join(exc.problems))
        else:
            results.ok(label, remote)


def run_missing_file(results, workdir):
    missing = Path(workdir) / "nowhere" / "config.toml"
    try:
        load_config(missing)
    except ConfigError as exc:
        if any("does not exist" in p and "config.sample.toml" in p for p in exc.problems):
            results.ok("a missing config.toml says where to get one")
        else:
            results.fail("a missing config.toml says where to get one",
                         "\n".join(exc.problems))
    else:
        results.fail("a missing config.toml says where to get one",
                     "loaded a file that is not there")


def run_render_roundtrip(results, workdir):
    """
    The sample must render on a clean machine, and re-render identically.

    The absent GIT_PAT/GITLAB_PAT are the point of the first assertion: render
    consumes the build-group secrets only, so a checkout with no git credential
    is still renderable. That property regressed once already.
    """
    root = Path(workdir) / "rendered"
    with build_secrets_only():
        try:
            code = render.main(["--config", str(SAMPLE), "--root", str(root), "--quiet"])
        except SystemExit as exc:            # argparse only; render returns ints
            code = exc.code
        if code != 0:
            results.fail("config.sample.toml renders with no git credential set",
                         "render.py exited {}".format(code))
            return
        results.ok("config.sample.toml renders with no git credential set")

        code = render.main(["--config", str(SAMPLE), "--root", str(root),
                            "--check", "--quiet"])
        if code == 0:
            results.ok("a second render finds no drift")
        else:
            results.fail("a second render finds no drift",
                         "render.py --check exited {}".format(code))

        # Drift must be detected, or --check is decoration.
        site_conf = root / "conf" / "nginx" / "site.conf"
        site_conf.write_text(site_conf.read_text(encoding="utf-8") + "\n# hand-edited\n",
                             encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):   # the diff is the point, not the output
            code = render.main(["--config", str(SAMPLE), "--root", str(root),
                                "--check", "--quiet"])
        if code != 0:
            results.ok("--check catches a hand-edited generated file")
        else:
            results.fail("--check catches a hand-edited generated file",
                         "a modified site.conf was reported as clean")


def run_debug_cases(results, workdir):
    """
    [debug], and specifically the ways it fails *silently*.

    Every case here corresponds to a way the developer attaches a debugger, the
    editor reports a healthy connection, and no breakpoint ever fires. None of
    them would show up as an error anywhere.
    """
    sample = SAMPLE.read_text(encoding="utf-8")
    if "enabled = false" not in sample:
        results.fail("[debug] is in config.sample.toml",
                     "no 'enabled = false' to switch; the sample lost its [debug] section")
        return
    results.ok("[debug] is in config.sample.toml")

    def outputs_for(text, name):
        path = write_config(workdir, text, name=name)
        cfg = load_config(path)
        return render.build_outputs(cfg, {"database.root_password": "x",
                                          "site.admin_password": "y"}, 1000, 1000)

    off = outputs_for(sample, "debug-off.toml")
    on = outputs_for(sample.replace("enabled = false", "enabled = true"), "debug-on.toml")

    web_off = [ln for ln in off["bench/Procfile"][0].splitlines() if ln.startswith("web:")][0]
    web_on = [ln for ln in on["bench/Procfile"][0].splitlines() if ln.startswith("web:")][0]

    # Opting out must change nothing at all, or every existing environment
    # drifts the moment this feature ships.
    if "debugpy" not in web_off and "PORT_DEBUG" not in off[".env"][0] \
            and "ports:" not in off["compose.podman.yml"][0]:
        results.ok("[debug].enabled = false generates nothing")
    else:
        results.fail("[debug].enabled = false generates nothing",
                     "a disabled section still reached a generated file")

    if "debugpy" in web_on:
        results.ok("[debug].enabled wraps the web line in debugpy", web_on)
    else:
        results.fail("[debug].enabled wraps the web line in debugpy", web_on)

    # The one that looks like it works: with the reloader on, the debugger
    # attaches to a watchdog process that never serves a request, and the
    # -m debugpy prefix is dropped at the first reload anyway.
    if "--noreload" in web_on:
        results.ok("the debugged web server runs with --noreload")
    else:
        results.fail("the debugged web server runs with --noreload",
                     "werkzeug's reloader would re-exec the server and drop the debugger")

    # bench runs under its own venv and dispatches to bench/env; wrapping the
    # wrong one instruments a process that never imports frappe.
    if "env/bin/python" in web_on and not web_on.startswith("web: bench "):
        results.ok("debugpy wraps bench/env's interpreter, not the bench CLI")
    else:
        results.fail("debugpy wraps bench/env's interpreter, not the bench CLI", web_on)

    if "PORT_DEBUG" in on[".env"][0]:
        results.ok("[debug].enabled writes PORT_DEBUG to .env")
    else:
        results.fail("[debug].enabled writes PORT_DEBUG to .env", "not written")

    for engine in ("podman", "docker"):
        overlay = on["compose.{}.yml".format(engine)][0]
        # Loopback only: a debug port executes arbitrary code by design.
        if "PORT_DEBUG" in overlay and "127.0.0.1:" in overlay:
            results.ok("{}: the debug port is published on loopback only".format(engine))
        else:
            results.fail("{}: the debug port is published on loopback only".format(engine),
                         overlay.splitlines()[-3:])

    expect_rejected(
        results, "a debug port colliding with ports.http is refused",
        sample.replace("enabled = false", "enabled = true")
              .replace("port    = 5678", "port    = 8101"),
        ["debug.port and ports.http"], workdir,
    )
    expect_rejected(
        results, "a worker debug target is refused",
        sample.replace('target  = "web"', 'target  = "worker:short"'),
        ["debug.target"], workdir,
    )
    expect_rejected(
        results, "an unknown [debug] key is refused",
        sample.replace("enabled = false", "enabled = false\nwait    = true"),
        ["debug.wait"], workdir,
    )


def run_briefing_cases(results, workdir):
    """
    The agent briefing: where it lives, and what must stay true of it.

    The location is the case worth having. An `@import` from CLAUDE.md was
    tested and rejected -- an import resolving outside the working directory is
    dropped, so a session started in bench/apps/<app>/ loses the briefing
    silently. `.claude/rules/` loads from an ancestor directory.
    """
    from erp_sandbox.cli import ENV_CLAUDE_MD, GITIGNORE

    sample = SAMPLE.read_text(encoding="utf-8")

    def outputs_for(text, name):
        cfg = load_config(write_config(workdir, text, name=name))
        return render.build_outputs(cfg, {"database.root_password": "x",
                                          "site.admin_password": "y"}, 1000, 1000)

    off = outputs_for(sample, "briefing-off.toml")
    on = outputs_for(sample.replace("enabled = false", "enabled = true"),
                     "briefing-on.toml")

    rule = ".claude/rules/erp-sandbox-environment.md"
    name = "the agent briefing renders to .claude/rules/"
    if rule in off:
        results.ok(name, rule)
    else:
        results.fail(name, "not a render output; an @import would be dropped "
                           "in bench/apps/<app>/")

    # If render ever owned CLAUDE.md, the developer's own notes would be
    # overwritten on the next render, which is the whole reason for two files.
    name = "render never writes the developer's CLAUDE.md"
    if "CLAUDE.md" not in off:
        results.ok(name)
    else:
        results.fail(name, "render would overwrite notes seeded by init")

    text = off[rule][0]

    name = "the briefing names erp-sandbox restart, not bench restart"
    if "erp-sandbox restart" in text and "Never `bench restart`" in text:
        results.ok(name)
    else:
        results.fail(name, "the supervisor trap is not warned about")

    name = "the briefing warns against activating bench/env"
    if "Do not activate `bench/env`" in text:
        results.ok(name)
    else:
        results.fail(name, "decision #22's consequence is unstated")

    # Everything loaded this way sits in every session's context from launch.
    # 200 lines is Claude Code's own guidance, not a count of our own things.
    lines = len(text.splitlines())
    name = "the briefing stays inside the 200-line guidance"
    if lines <= 200:
        results.ok(name, "{} lines".format(lines))
    else:
        results.fail(name, "{} lines; it is loaded into every session".format(lines))

    name = "[debug].enabled = false leaves the briefing silent about debugging"
    if "debugpy" not in text:
        results.ok(name)
    else:
        results.fail(name, "a disabled section reached the briefing")

    name = "[debug].enabled tells the briefing the port"
    if "5678" in on[rule][0]:
        results.ok(name)
    else:
        results.fail(name, "the debug port is not reported to the agent")

    # Ignoring the seed would quietly drop the developer's own notes out of
    # version control, which is the opposite of what init writes it for.
    name = "the generated briefing is gitignored and CLAUDE.md is not"
    ignored = [ln.strip() for ln in GITIGNORE.splitlines() if ln.strip()
               and not ln.startswith("#")]
    if "/" + rule in ignored and not any(ln.endswith("CLAUDE.md") for ln in ignored):
        results.ok(name)
    else:
        results.fail(name, ignored)

    name = "the seeded CLAUDE.md points at the generated briefing"
    if rule in ENV_CLAUDE_MD:
        results.ok(name)
    else:
        results.fail(name, "a developer reading it would not find the rules file")


def strip_jsonc(text):
    """Drop whole-line // comments so a JSONC file can be parsed."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def run_workspace_cases(results, workdir):
    """
    The generated VSCode workspace file.

    Two cases here guard findings that were wrong in the first draft and were
    only caught by testing: the analyzer setting must exist in both spellings
    because Pylance is unavailable on VSCodium, and a launch configuration in
    a workspace file must scope its folder variable or the path mapping does
    not resolve.
    """
    sample = SAMPLE.read_text(encoding="utf-8")

    def outputs_for(text, name):
        cfg = load_config(write_config(workdir, text, name=name))
        return render.build_outputs(cfg, {"database.root_password": "x",
                                          "site.admin_password": "y"}, 1000, 1000)

    off = outputs_for(sample, "workspace-off.toml")
    on = outputs_for(sample.replace("enabled = false", "enabled = true"),
                     "workspace-on.toml")

    key = [k for k in off if k.endswith(".code-workspace")]
    name = "a .code-workspace is generated, named from [project].name"
    if len(key) == 1:
        results.ok(name, key[0])
    else:
        results.fail(name, key or "not generated")
        return
    key = key[0]

    for label, outputs in (("debugging off", off), ("debugging on", on)):
        case = "the workspace file is valid JSONC with {}".format(label)
        try:
            parsed = json.loads(strip_jsonc(outputs[key][0]))
        except ValueError as exc:
            results.fail(case, str(exc))
            continue
        results.ok(case)

        settings = parsed.get("settings", {})

        # An interpreter that cannot import frappe is worse than none: it makes
        # every frappe symbol unresolved. requirements.md section 3.
        sub = "no interpreter is named ({})".format(label)
        if "python.defaultInterpreterPath" not in settings:
            results.ok(sub)
        else:
            results.fail(sub, settings["python.defaultInterpreterPath"])

        # Pylance is Marketplace-only; VSCodium reads the autoComplete spelling.
        sub = "both extraPaths spellings are written ({})".format(label)
        pylance = settings.get("python.analysis.extraPaths")
        jedi = settings.get("python.autoComplete.extraPaths")
        if pylance and pylance == jedi:
            results.ok(sub)
        else:
            results.fail(sub, "Pylance-only would be inert on VSCodium")

        # One entry per app: the package is at bench/apps/<app>/<app>/, so
        # bench/apps alone resolves nothing.
        sub = "every app is on the analysis path ({})".format(label)
        missing = [a for a in ("frappe", "erpnext")
                   if "bench/apps/{}".format(a) not in (pylance or [])]
        if not missing and any("site-packages" in p for p in (pylance or [])):
            results.ok(sub)
        else:
            results.fail(sub, missing or "site-packages absent")

    name = "[debug].enabled = false writes no launch configuration"
    if "launch" not in json.loads(strip_jsonc(off[key][0])):
        results.ok(name)
    else:
        results.fail(name, "a disabled section reached the workspace file")

    launch = json.loads(strip_jsonc(on[key][0])).get("launch", {})
    config = (launch.get("configurations") or [{}])[0]

    name = "[debug].enabled writes a debugpy attach configuration"
    if config.get("type") == "debugpy" and config.get("request") == "attach" \
            and config.get("connect", {}).get("port") == 5678:
        results.ok(name, config.get("name"))
    else:
        results.fail(name, config)

    # The bare ${workspaceFolder} does not resolve in a workspace file, and an
    # unresolved mapping binds breakpoints to nothing, silently.
    name = "the path mapping scopes its folder variable"
    local = (config.get("pathMappings") or [{}])[0].get("localRoot", "")
    if local.startswith("${workspaceFolder:") and "}" in local:
        results.ok(name, local)
    else:
        results.fail(name, local or "no localRoot")


def run_guard_cases(results, workdir):
    """
    The edit guard (decision #35): what it refuses, and everything it must not.

    Two properties matter more than the refusal itself. The generated file is
    **strict JSON**, because Claude Code owns that schema and a settings file
    it cannot parse takes the whole configuration down, not just the hook. And
    the guard **fails open** on every error path -- a guard that refuses
    because it could not read its own input would block real work for a reason
    the developer never sees, which is worse than the hand-edit it prevents.
    """
    from erp_sandbox import guard

    cfg = load_config(write_config(workdir, SAMPLE.read_text(encoding="utf-8"),
                                   name="guard.toml"))
    outputs = render.build_outputs(
        cfg, {"database.root_password": "x", "site.admin_password": "y"}, 1000, 1000)

    settings_path = ".claude/settings.json"
    name = "guard: .claude/settings.json is generated"
    if settings_path not in outputs:
        results.fail(name, "not generated")
        return
    results.ok(name)

    # Strict json.loads, not the JSONC reader the workspace file needs. If
    # anyone adds the usual "GENERATED" header to this one, this case is how
    # they find out it cannot have one.
    name = "guard: the settings file is strict JSON, with no comments"
    try:
        settings = json.loads(outputs[settings_path][0])
    except ValueError as exc:
        results.fail(name, str(exc))
        return
    results.ok(name)

    hook = ((settings.get("hooks", {}).get("PreToolUse") or [{}])[0])
    name = "guard: the hook matches Edit and Write"
    if hook.get("matcher") == "Edit|Write":
        results.ok(name, hook.get("matcher"))
    else:
        results.fail(name, hook.get("matcher"))

    command = ((hook.get("hooks") or [{}])[0]).get("command", "")
    # A bare `erp-sandbox` is inert wherever the PATH that started the editor does
    # not carry the console script, and inert looks exactly like allowed.
    name = "guard: the hook command is an absolute path"
    if command.split()[0:1] and Path(command.split()[0]).is_absolute() \
            and command.endswith("guard-edit"):
        results.ok(name, command)
    else:
        results.fail(name, command or "no command")

    # The paths are a property of the config; the contents need secrets. The
    # guard runs whenever Claude Code writes a file, which is not a moment when
    # a password can be assumed to be in the environment.
    name = "guard: the generated list needs no secret"
    saved = {key: os.environ.pop(key, None) for key in BUILD_SECRETS}
    try:
        paths = render.output_paths(cfg)
    except Exception as exc:            # broad on purpose: the case is that it does not
        paths = None
        detail = "{}: {}".format(type(exc).__name__, exc)
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    expected = set(outputs) | set(render.asset_outputs())
    if paths is not None and set(paths) == expected:
        results.ok(name, "{} path(s)".format(len(paths)))
    else:
        results.fail(name, detail if paths is None else "list disagrees with render")

    # A real environment on disk: the guard finds it by walking up from the
    # file being written, not from anybody's working directory.
    root = Path(workdir) / "guard-env"
    (root / project.MARKER).mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(SAMPLE.read_text(encoding="utf-8"),
                                      encoding="utf-8")

    def decide(file_path, cwd=None):
        return guard.decide({"tool_name": "Edit",
                             "tool_input": {"file_path": str(file_path)}}, cwd=cwd)

    for relative in ("bench/Procfile", ".env", "docker-compose.yml",
                     "conf/nginx/site.conf", ".claude/settings.json"):
        case = "guard: refuses an edit to {}".format(relative)
        reason = decide(root / relative)
        if reason and relative in reason:
            results.ok(case)
        else:
            results.fail(case, reason or "allowed")

    # The refusal has to be usable instruction, not an obstruction: a denial
    # that does not name the remedy sends the model looking for a way around.
    name = "guard: the refusal names config.toml and the render"
    reason = decide(root / ".env") or ""
    if "config.toml" in reason and "erp-sandbox render" in reason:
        results.ok(name)
    else:
        results.fail(name, reason or "allowed")

    for relative in ("config.toml", "CLAUDE.md", ".erp-sandbox/secrets.env",
                     "bench/apps/erpnext/erpnext/hooks.py", ".vscode/settings.json",
                     "conf/nginx/something-a-developer-added.conf"):
        case = "guard: allows an edit to {}".format(relative)
        reason = decide(root / relative)
        if reason is None:
            results.ok(case)
        else:
            results.fail(case, "refused: " + reason)

    name = "guard: a relative path resolves against the session's directory"
    if decide("bench/Procfile", cwd=str(root)):
        results.ok(name)
    else:
        results.fail(name, "allowed")

    name = "guard: a file outside any environment is not ours to refuse"
    outside = Path(workdir) / "not-an-environment.txt"
    if decide(outside) is None:
        results.ok(name)
    else:
        results.fail(name, "refused a file in no environment")

    # Fail-open, both halves. A config.toml that does not parse is exactly when
    # a developer needs to edit files, including config.toml itself.
    name = "guard: an unreadable config.toml refuses nothing"
    broken = Path(workdir) / "guard-broken"
    (broken / project.MARKER).mkdir(parents=True, exist_ok=True)
    (broken / "config.toml").write_text("this is not [ valid toml\n", encoding="utf-8")
    if decide(broken / ".env") is None:
        results.ok(name)
    else:
        results.fail(name, "refused while the config was unreadable")

    for label, payload in (("junk on stdin", "not json at all"),
                           ("an empty payload", "{}"),
                           ("no file_path", '{"tool_name": "Edit", "tool_input": {}}'),
                           ("a payload that is not an object", '["Edit"]')):
        case = "guard: {} refuses nothing".format(label)
        out = io.StringIO()
        code = guard.main(stdin=io.StringIO(payload), stdout=out)
        if code == 0 and out.getvalue() == "":
            results.ok(case)
        else:
            results.fail(case, "exit {}, wrote {!r}".format(code, out.getvalue()))

    name = "guard: a refusal is written as a PreToolUse deny"
    out = io.StringIO()
    code = guard.main(
        stdin=io.StringIO(json.dumps(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(root / "bench/Procfile")}})),
        stdout=out)
    try:
        emitted = json.loads(out.getvalue())["hookSpecificOutput"]
    except (ValueError, KeyError):
        emitted = {}
    if code == 0 and emitted.get("permissionDecision") == "deny" \
            and emitted.get("hookEventName") == "PreToolUse" \
            and emitted.get("permissionDecisionReason"):
        results.ok(name)
    else:
        results.fail(name, out.getvalue() or "wrote nothing")


def run_gitignore_coverage(results, workdir):
    """
    Every generated file is ignored by the .gitignore `init` seeds.

    Asked of `git check-ignore` rather than by reading the file, because the
    patterns are directory prefixes and a substring test would answer the
    wrong question. This exists because `/images/` was missing: two render
    outputs would have been committed into a developer's environment
    repository and then drifted from config.toml.
    """
    from erp_sandbox.cli import GITIGNORE

    name = "every generated file is gitignored"
    root = Path(workdir) / "gitignore-coverage"
    root.mkdir(exist_ok=True)
    if subprocess.run(["git", "init", "-q", str(root)],
                      capture_output=True).returncode != 0:
        results.ok(name, "skipped: git unavailable")
        return
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

    cfg = load_config(write_config(workdir, SAMPLE.read_text(encoding="utf-8"),
                                   name="gitignore-coverage.toml"))
    outputs = render.build_outputs(
        cfg, {"database.root_password": "x", "site.admin_password": "y"}, 1000, 1000)
    outputs.update(render.asset_outputs())

    escaped = [path for path in outputs
               if subprocess.run(["git", "-C", str(root), "check-ignore", "-q", path],
                                 capture_output=True).returncode != 0]
    if not escaped:
        results.ok(name, "{} output(s)".format(len(outputs)))
    else:
        results.fail(name, "would be committed: " + ", ".join(sorted(escaped)))

    # The other half: the seed init writes is the developer's, and ignoring it
    # would drop their notes out of version control.
    name = "the developer's CLAUDE.md is not gitignored"
    if subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "CLAUDE.md"],
                      capture_output=True).returncode != 0:
        results.ok(name)
    else:
        results.fail(name, "init seeds it for the developer to keep")


def run_compose_context(results, workdir):
    """
    Everything docker-compose.yml refers to by relative path must be rendered.

    The compose file is materialized into the environment, so its relative
    paths resolve against the environment -- and anything it names that render
    does not produce is a file the engine will look for and not find. That
    failure surfaces only on a *fresh* environment, because an existing one has
    already built or created the thing, which is exactly how it was missed.
    """
    import re

    root = Path(workdir) / "compose-context"
    with build_secrets_only():
        render.main(["--config", str(SAMPLE), "--root", str(root), "--quiet"])

    compose = root / "docker-compose.yml"
    if not compose.is_file():
        results.fail("compose: every referenced path is rendered", "no docker-compose.yml")
        return

    text = compose.read_text(encoding="utf-8")
    contexts = set(re.findall(r"context:\s*\./(\S+)", text))
    mounts = set(re.findall(r"^\s*-\s*\./([^:\s]+):", text, flags=re.M))

    missing = []
    for relative in sorted(contexts | mounts):
        # bench/ and data/ are created by later build steps, not by render.
        if relative.split("/")[0] in ("bench", "data"):
            continue
        if not (root / relative).exists():
            missing.append(relative)

    if missing:
        results.fail("compose: every referenced path is rendered",
                     "docker-compose.yml names paths render did not create:\n  "
                     + "\n  ".join(missing))
    else:
        results.ok("compose: every referenced path is rendered",
                   "{} path(s) checked".format(len(contexts | mounts)))

    for relative in sorted(contexts):
        if (root / relative / "Dockerfile").is_file():
            results.ok("compose: build context has a Dockerfile", relative)
        else:
            results.fail("compose: build context has a Dockerfile",
                         "{} has no Dockerfile".format(relative))


def run_cli_cases(results, workdir):
    """
    The verbs, tested where they can be: argument construction and discovery.

    `erp-sandbox bench migrate` is not a wrapper worth much if it mangles its own
    arguments, and it did -- `erp-sandbox bench --site x execute y` failed with
    "unrecognized arguments: --site" until argv was split before parsing. That
    bug was found by typing it, which is not a strategy. These cases cover the
    part that has actually been wrong; what remains untestable offline is
    whether the engine likes the argv, which the onboarding run answers.
    """
    from erp_sandbox import cli

    # 1. Passthrough. The whole point of the `bench` verb.
    for argv, ours, theirs in (
        (["bench", "console"], ["bench"], ["console"]),
        (["bench", "--site", "x", "execute", "y"], ["bench"], ["--site", "x", "execute", "y"]),
        (["bench", "--version"], ["bench"], ["--version"]),
        (["--project", "/tmp/p", "bench", "migrate"],
         ["--project", "/tmp/p", "bench"], ["migrate"]),
        (["up", "-d"], ["up", "-d"], []),          # not a passthrough verb
        (["--version"], ["--version"], []),
        # "bench" as the *value* of an option, not as the verb. An earlier
        # version split here, handed --force to a bench command that never ran,
        # and dropped it from the build -- which then refused to replace a site
        # while advising --force, the flag it had just discarded.
        (["build", "--from", "bench", "--force"],
         ["build", "--from", "bench", "--force"], []),
        (["build", "--only", "bench"], ["build", "--only", "bench"], []),
        (["--project", "/tmp/bench", "up"], ["--project", "/tmp/bench", "up"], []),
    ):
        got_ours, got_theirs = cli.split_passthrough(list(argv))
        if [got_ours, got_theirs] == [ours, theirs]:
            results.ok("cli: {!r} splits correctly".format(" ".join(argv)))
        else:
            results.fail("cli: {!r} splits correctly".format(" ".join(argv)),
                         "got {!r} / {!r}, expected {!r} / {!r}".format(
                             got_ours, got_theirs, ours, theirs))

    # 1b. The same thing, through main().
    #
    # Testing split_passthrough alone is not enough, and this is not a
    # hypothetical: the bug was reintroduced deliberately by removing the *call*
    # to it, and every case above still passed while `erp-sandbox bench --site x`
    # was broken. A test that covers the function but not the wiring gives
    # confidence it has not earned.
    #
    # build_parser resolves the handlers as globals when it runs, so replacing
    # one here reaches the parser main() builds.
    captured = {}

    def capture(args):
        captured["rest"] = list(getattr(args, "rest", []))
        return 0

    original = cli.cmd_bench
    cli.cmd_bench = capture
    try:
        cli.main(["bench", "--site", "x", "execute", "y"])
    except SystemExit as exc:
        captured["exit"] = exc.code
    finally:
        cli.cmd_bench = original

    if captured.get("rest") == ["--site", "x", "execute", "y"]:
        results.ok("cli: main() hands bench its arguments untouched")
    else:
        results.fail("cli: main() hands bench its arguments untouched",
                     "handler received {!r}".format(captured.get("rest")))

    # 2. Every verb parses and has help. A verb that cannot be reached is not a
    #    verb, and argparse subcommands are easy to declare and never wire up.
    parser = cli.build_parser()
    verbs = sorted(parser._subparsers._group_actions[0].choices)
    broken = []
    for verb in verbs:
        try:
            parser.parse_args([verb, "--help"])
        except SystemExit as exc:
            if exc.code not in (0, None):
                broken.append(verb)
        except Exception as exc:                       # noqa: BLE001
            broken.append("{} ({})".format(verb, exc))
    if broken:
        results.fail("cli: every verb parses", ", ".join(broken))
    else:
        results.ok("cli: every verb parses", "{} verbs: {}".format(len(verbs), " ".join(verbs)))

    # 3. A flag declared on a module's own argparse is not reachable from the
    #    CLI until cmd_* forwards it. `render --list` was documented in four
    #    places while `erp-sandbox render --list` exited on "unrecognized
    #    arguments", because only render.py knew about it.
    env = Path(workdir) / "render-flags"
    (env / project.MARKER).mkdir(parents=True, exist_ok=True)
    (env / "config.toml").write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    for flag in ("--list", "--check"):
        seen = {}

        def capture(argv, _seen=seen):
            _seen["argv"] = list(argv)
            return 0

        original = cli.render.main
        cli.render.main = capture
        try:
            cli.main(["--project", str(env), "render", flag])
        except SystemExit as exc:
            seen["exit"] = exc.code
        finally:
            cli.render.main = original

        name = "cli: `render {}` reaches the renderer".format(flag)
        if flag in seen.get("argv", []):
            results.ok(name)
        else:
            results.fail(name, "cmd_render forwarded {!r}".format(seen.get("argv")))

    # 3. Every verb is dispatchable -- declared *and* wired to a function.
    unwired = [v for v in verbs
               if not hasattr(parser.parse_args([v] if v not in ("bench",) else [v, "x"]),
                              "func")]
    if unwired:
        results.fail("cli: every verb is wired to a handler", ", ".join(unwired))
    else:
        results.ok("cli: every verb is wired to a handler")

    # 4. exec_command: the shape `erp-sandbox bench` produces.
    argv = cli.exec_command("podman", "acme-bench", ["bench", "migrate"], tty=False)
    if argv == ["podman", "exec", "-i", "acme-bench", "bench", "migrate"]:
        results.ok("cli: bench runs inside the named container")
    else:
        results.fail("cli: bench runs inside the named container", repr(argv))
    if cli.exec_command("podman", "c", ["sh"], tty=True)[2] == "-it":
        results.ok("cli: a tty is requested when there is one")
    else:
        results.fail("cli: a tty is requested when there is one", "no -it")

    # 4b. mariadb: a query reaches the client, and the password never reaches
    # argv. Going through `bench --site X mariadb` could not do either -- that
    # command is an os.execv with a hardcoded argv.
    site_conf = {"db_name": "_abc123", "db_password": "s3cret"}
    common = {"db_host": "mariadb", "db_port": 3306}

    argv, env = cli.mariadb_command(site_conf, common, ["-e", "select 1"], interactive=False)
    if argv is not None and argv[-2:] == ["-e", "select 1"] and argv[0] == "mariadb":
        results.ok("cli: mariadb passes a query through to the client")
    else:
        results.fail("cli: mariadb passes a query through to the client", repr(argv))

    if "s3cret" not in " ".join(argv) and env.get("MYSQL_PWD") == "s3cret":
        results.ok("cli: the db password reaches mysql by environment, not argv",
                   "MYSQL_PWD; -p<password> would show in the process list")
    else:
        results.fail("cli: the db password reaches mysql by environment, not argv",
                     repr(argv))

    # --pager mangles -e output and there is no terminal to page to in a script.
    scripted, _ = cli.mariadb_command(site_conf, common, ["-e", "select 1"], interactive=True)
    human, _ = cli.mariadb_command(site_conf, common, [], interactive=True)
    if not any(a.startswith("--pager") for a in scripted) \
            and any(a.startswith("--pager") for a in human):
        results.ok("cli: the pager is for a human session only")
    else:
        results.fail("cli: the pager is for a human session only", repr(scripted))

    if cli.mariadb_command({}, common, [], interactive=False)[0] is None:
        results.ok("cli: a site with no db_name is refused rather than guessed")
    else:
        results.fail("cli: a site with no db_name is refused rather than guessed")

    # 5. compose files, in the order that makes the overlay win.
    base, overlay = cli.compose_files("/tmp/env", "podman")
    if base.name == "docker-compose.yml" and overlay.name == "compose.podman.yml":
        results.ok("cli: the overlay is applied on top of the base")
    else:
        results.fail("cli: the overlay is applied on top of the base",
                     "{} then {}".format(base.name, overlay.name))

    # 6. Discovery: found from below, refused from outside.
    env = Path(workdir) / "cli-env"
    (env / project.MARKER).mkdir(parents=True)
    deep = env / "a" / "b" / "c"
    deep.mkdir(parents=True)
    found = project.find_project_root(deep)
    if found == env.resolve():
        results.ok("cli: the environment is found from a nested directory")
    else:
        results.fail("cli: the environment is found from a nested directory", str(found))

    outside = Path(workdir) / "not-an-env"
    outside.mkdir()
    try:
        project.find_project_root(outside)
    except project.ProjectNotFound as exc:
        if "erp-sandbox init" in str(exc):
            results.ok("cli: outside an environment, it says how to make one")
        else:
            results.fail("cli: outside an environment, it says how to make one", str(exc))
    else:
        results.fail("cli: outside an environment, it says how to make one",
                     "found an environment that is not there")


def run_vps_cases(results, workdir):
    """
    Shared-VPS hosting (decisions #40-#51): what a laptop must not notice, and
    what a VPS environment must get right without anyone checking by hand.
    """
    from erp_sandbox import cli, preflight

    sample = SAMPLE.read_text(encoding="utf-8")
    served = sample.replace('# public_url = "https://acme.dev.example.com"',
                            'public_url = "https://acme.dev.example.com"')

    def outputs_for(text, name):
        cfg = load_config(write_config(workdir, text, name=name))
        return cfg, render.build_outputs(cfg, {"database.root_password": "x",
                                               "site.admin_password": "y"}, 1000, 1000)

    laptop_cfg, laptop = outputs_for(sample, "vps-laptop.toml")
    vps_cfg, vps = outputs_for(served, "vps-served.toml")

    # --- #46: loopback by default ------------------------------------------
    compose = (project.ASSET_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    if "BIND_HTTP=127.0.0.1" in laptop[".env"][0] \
            and '"${BIND_HTTP:?' in compose and ':${PORT_HTTP:?' in compose:
        results.ok("ports.http is published on 127.0.0.1 unless bind says otherwise")
    else:
        results.fail("ports.http is published on 127.0.0.1 unless bind says otherwise",
                     "BIND_HTTP missing from .env or from the nginx ports entry")

    open_cfg, _ = outputs_for(sample.replace('# bind  = "127.0.0.1"', 'bind    = "0.0.0.0"'),
                              "vps-open.toml")
    if preflight.http_probe_host(open_cfg) == "127.0.0.1" \
            and preflight.http_probe_host(laptop_cfg) == "127.0.0.1":
        results.ok("a 0.0.0.0 bind is still probed and proxied on loopback")
    else:
        results.fail("a 0.0.0.0 bind is still probed and proxied on loopback",
                     preflight.http_probe_host(open_cfg))

    # --- #47: restart policy on every service ------------------------------
    services = ("mariadb", "redis", "bench", "nginx")
    restarts = compose.count("restart: unless-stopped")
    if restarts == len(services):
        results.ok("every service carries restart: unless-stopped")
    else:
        results.fail("every service carries restart: unless-stopped",
                     "{} service(s) have it".format(restarts))

    # podman-restart.service starts only restart-policy=always containers at
    # boot, so the podman overlay must override every service, and the Docker
    # overlay must override none.
    podman_overlay = laptop["compose.podman.yml"][0]
    docker_overlay = laptop["compose.docker.yml"][0]
    def policy_lines(text, policy):
        return sum(1 for line in text.splitlines() if line.strip() == "restart: " + policy)

    if policy_lines(podman_overlay, "always") == len(services) \
            and not policy_lines(docker_overlay, "always") \
            and not policy_lines(docker_overlay, "unless-stopped"):
        results.ok("the podman overlay makes every service restart: always; Docker keeps unless-stopped")
    else:
        results.fail("the podman overlay makes every service restart: always; Docker keeps unless-stopped",
                     "podman {} service(s), docker overlay {}".format(
                         policy_lines(podman_overlay, "always"),
                         "overrides it" if "restart:" in docker_overlay else "clean"))

    # --- host_name ----------------------------------------------------------
    try:
        on = json.loads(vps["bench/sites/common_site_config.json"][0])
        off = json.loads(laptop["bench/sites/common_site_config.json"][0])
    except ValueError as exc:
        results.fail("common_site_config.json stays valid JSON with and without host_name",
                     str(exc))
    else:
        results.ok("common_site_config.json stays valid JSON with and without host_name")
        if on.get("host_name") == "https://acme.dev.example.com" and "host_name" not in off:
            results.ok("public_url becomes host_name, and only when set")
        else:
            results.fail("public_url becomes host_name, and only when set",
                         "on={!r} off={!r}".format(on.get("host_name"), off.get("host_name")))

    # --- behind a TLS-terminating proxy -------------------------------------
    nginx = laptop["conf/nginx/site.conf"][0]
    if "map $http_x_forwarded_proto $erp_sandbox_forwarded_proto" in nginx \
            and "Origin $erp_sandbox_forwarded_proto://" in nginx \
            and "X-Forwarded-Proto $scheme" not in nginx:
        results.ok("nginx passes the proxy's X-Forwarded-Proto through, and builds Origin from it")
    else:
        results.fail("nginx passes the proxy's X-Forwarded-Proto through, and builds Origin from it",
                     "socketio's callback would go to http:// behind Caddy")

    # --- #43: the Caddy block -----------------------------------------------
    root = Path("/srv/example/acme-dev")
    if render.proxy_snippet(laptop_cfg, root, "bob") is None:
        results.ok("proxy-snippet has nothing to print without a public_url")
    else:
        results.fail("proxy-snippet has nothing to print without a public_url",
                     "printed a block for an environment nobody serves")
    snippet = render.proxy_snippet(vps_cfg, root, "bob") or ""
    if "acme.dev.example.com {" in snippet \
            and "reverse_proxy 127.0.0.1:8101" in snippet:
        results.ok("proxy-snippet serves the public host from the loopback port",
                   snippet.splitlines()[2])
    else:
        results.fail("proxy-snippet serves the public host from the loopback port", snippet)

    # --- #50: the registry ----------------------------------------------------
    registry = Path(workdir) / "registry.toml"
    registry.write_text(
        '[users.bob]\nports = [8101, 3316]\nhosts = ["acme.dev.example.com"]\n\n'
        '[users.sally]\nports = [8201, 3326]\nhosts = ["sally.dev.example.com"]\n',
        encoding="utf-8")

    def registry_rows(user, cfg=vps_cfg, path=registry):
        report = preflight.Report()
        preflight.check_registry(report, cfg, user=user, path=path)
        return report.rows

    rows = registry_rows("bob")
    if rows and all(status == "ok" for status, _, _ in rows):
        results.ok("registry: the owner's own ports and host pass")
    else:
        results.fail("registry: the owner's own ports and host pass", rows)

    rows = registry_rows("sally")
    if any(status == "fail" and "belongs to bob" in label for status, label, _ in rows):
        results.ok("registry: another user's port is refused, naming the owner")
    else:
        results.fail("registry: another user's port is refused, naming the owner", rows)

    rows = registry_rows("carol")
    if rows and all(status == "fail" for status, _, _ in rows):
        results.ok("registry: a user it does not list cannot take an assigned port")
    else:
        results.fail("registry: a user it does not list cannot take an assigned port", rows)

    rows = registry_rows("bob", path=Path(workdir) / "no-registry-here.toml")
    if len(rows) == 1 and rows[0][0] == "ok":
        results.ok("registry: absent on a laptop, and silent about it")
    else:
        results.fail("registry: absent on a laptop, and silent about it", rows)

    clash = Path(workdir) / "registry-clash.toml"
    clash.write_text('[users.bob]\nports = [8101]\n\n[users.sally]\nports = [8101]\n',
                     encoding="utf-8")
    try:
        preflight.load_registry(clash)
    except ConfigError as exc:
        if any("8101 is assigned to both bob and sally" in p for p in exc.problems):
            results.ok("registry: a port listed under two users is itself refused")
        else:
            results.fail("registry: a port listed under two users is itself refused",
                         exc.problems)
    else:
        results.fail("registry: a port listed under two users is itself refused",
                     "accepted")

    # --- #45: rootless Docker ---------------------------------------------
    original = preflight.docker_is_rootless
    preflight.docker_is_rootless = lambda: True
    try:
        report = preflight.Report()
        preflight.check_engine_mode(report, laptop_cfg, "docker")
    finally:
        preflight.docker_is_rootless = original
    if report.failed and "rootless Docker" in report.rows[0][1]:
        results.ok("rootless Docker is refused before it writes subuid-owned files")
    else:
        results.fail("rootless Docker is refused before it writes subuid-owned files",
                     report.rows)

    # --- #51: where init puts an environment ------------------------------
    home = Path(workdir) / "home"
    home.mkdir()
    with environment(HOME=str(home)):
        expected = {
            "acme-dev": home / "erp-sandboxes" / "acme-dev",
            "./acme-dev": Path("./acme-dev"),
            "~/elsewhere/acme": home / "elsewhere" / "acme",
            "/srv/acme": Path("/srv/acme"),
        }
        wrong = {name: str(project.init_target(name)) for name, want in expected.items()
                 if project.init_target(name) != want}
        if not wrong:
            results.ok("init: a bare name goes under ~/erp-sandboxes/, a path is a path")
        else:
            results.fail("init: a bare name goes under ~/erp-sandboxes/, a path is a path",
                         wrong)

        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["init", "acme-dev"])
        made = home / "erp-sandboxes" / "acme-dev"
        if code == 0 and project.is_project_root(made) and (made / "config.toml").is_file():
            results.ok("init: creates ~/erp-sandboxes/ and the environment in it")
        else:
            results.fail("init: creates ~/erp-sandboxes/ and the environment in it",
                         "exit {}, {}".format(code, sorted(p.name for p in home.rglob("*"))))

        occupied = home / "projects"
        occupied.mkdir()
        (occupied / "notes.txt").write_text("not an environment\n", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            try:
                cli.main(["init", "~/projects"])
                code = 0
            except SystemExit as exc:
                code = exc.code
        if code != 0 and "not empty" in stderr.getvalue() \
                and not (occupied / "config.toml").exists():
            results.ok("init: refuses a non-empty directory and writes nothing into it")
        else:
            results.fail("init: refuses a non-empty directory and writes nothing into it",
                         "exit {}: {}".format(code, stderr.getvalue().strip()))

        cwd = os.getcwd()
        os.chdir(occupied)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["init", "--here"])
        finally:
            os.chdir(cwd)
        if code == 0 and project.is_project_root(occupied) \
                and (occupied / "notes.txt").is_file():
            results.ok("init: --here is the explicit way into a directory that has files")
        else:
            results.fail("init: --here is the explicit way into a directory that has files",
                         "exit {}".format(code))


def run_series_cases(results, workdir):
    """
    Decisions #55-#57: a second Frappe series, and what init writes for it.

    Nothing here builds an image -- that is the acceptance run. What is
    checked is that v16's config is coherent, renders what the image build
    reads, and that the pairings the build would otherwise find minutes in are
    caught first.
    """
    from erp_sandbox import cli, known_versions, preflight

    sample = SAMPLE.read_text(encoding="utf-8")
    v16_text = cli.seed_series(sample, 16)

    def load(text, name):
        return load_config(write_config(workdir, text, name=name))

    def outputs(cfg):
        return render.build_outputs(cfg, {"database.root_password": "x",
                                          "site.admin_password": "y"}, 1000, 1000)

    try:
        v16 = load(v16_text, "series-16.toml")
    except ConfigError as exc:
        results.fail("init --frappe 16 writes a config that validates", exc.problems)
        return
    results.ok("init --frappe 16 writes a config that validates")

    # Through init itself: the constraint seed is written after seed_series.
    made = Path(workdir) / "series-init-16"
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["init", "--frappe", "16", str(made)])
    initialised = load_config(project.config_path(made))
    stack = initialised["stack"]
    want = dict(known_versions.get(16)["stack_defaults"], frappe_version=16)
    wrong = {k: stack.get(k) for k, v in want.items() if stack.get(k) != v}
    refs = {a["name"]: a["ref"] for a in initialised["apps"]}
    if not wrong and refs == {"frappe": "version-16", "erpnext": "version-16"} \
            and stack["pip_constraints"] == []:
        results.ok("init --frappe 16 writes every v16 pin, the version-16 refs, "
                   "and v16's empty constraint seed")
    else:
        results.fail("init --frappe 16 writes every v16 pin, the version-16 refs, "
                     "and v16's empty constraint seed", {"stack": wrong, "refs": refs})

    if cli.seed_series(sample, 12) == sample:
        results.ok("init's default series leaves the v12 sample exactly as it is")
    else:
        results.fail("init's default series leaves the v12 sample exactly as it is",
                     "seeding v12 changed the sample")

    env16 = outputs(v16)[".env"][0]
    env12 = outputs(load(sample, "series-12.toml"))[".env"][0]
    if "PYTHON_VERSION=3.14" in env16 and "pip<21" not in env16 \
            and "PYTHON_VERSION=3.9" in env12 and "BENCH_CLI_PINS=pip<21" in env12:
        results.ok("the image gets its Python and bench-CLI installer from the series")
    else:
        results.fail("the image gets its Python and bench-CLI installer from the series",
                     [ln for ln in env16.splitlines() if ln.startswith(("PYTHON", "BENCH_CLI"))])

    legacy, modern, _cli = known_versions.installer_pins(known_versions.get(16))
    if legacy == "" and "flit_core" in modern:
        results.ok("v16 has no legacy installer, and its modern one carries flit_core",
                   "pyproject apps install with --no-build-isolation")
    else:
        results.fail("v16 has no legacy installer, and its modern one carries flit_core",
                     (legacy, modern))

    if preflight.series_warnings(v16) == [] \
            and preflight.series_warnings(load(sample, "series-12b.toml")) == []:
        results.ok("both series' own defaults draw no series warning")
    else:
        results.fail("both series' own defaults draw no series warning",
                     preflight.series_warnings(v16))

    stale = load(v16_text.replace('ref                  = "version-16"',
                                  'ref                  = "version-12"', 1)
                         .replace("mariadb:11.8", "mariadb:10.4.6"), "series-stale.toml")
    warnings = preflight.series_warnings(stale)
    if any("apps.frappe.ref = 'version-12'" in w for w in warnings) \
            and any("MariaDB 10.4.6 but Frappe v16 supports 10.6 to 11.8" in w for w in warnings):
        results.ok("a v12 ref and a MariaDB outside v16's range are both warned about")
    else:
        results.fail("a v12 ref and a MariaDB outside v16's range are both warned about",
                     warnings)

    # R6: mariadb and redis run in containers of their own.
    try:
        load(sample.replace("mariadb      = 3306", "mariadb      = 8000"), "series-ports.toml")
    except ConfigError as exc:
        results.fail("an internal mariadb port equal to web's is not a collision", exc.problems)
    else:
        results.ok("an internal mariadb port equal to web's is not a collision")

    # R5: accepted, and nothing reads it.
    legacy_cfg = load(sample.replace("[frappe]\n", "[frappe]\ngunicorn_workers   = 2\n", 1),
                      "series-gunicorn.toml")
    site_config = outputs(legacy_cfg)["bench/sites/common_site_config.json"][0]
    if "gunicorn_workers" not in site_config:
        results.ok("gunicorn_workers is accepted and no longer rendered")
    else:
        results.fail("gunicorn_workers is accepted and no longer rendered", site_config)

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        try:
            cli.main(["init", "--frappe", "99", str(Path(workdir) / "series-99")])
            code = 0
        except SystemExit as exc:
            code = exc.code
    if code != 0 and "frappe_version = 99" in stderr.getvalue() \
            and not (Path(workdir) / "series-99").exists():
        results.ok("init --frappe refuses an unanalysed series before writing anything")
    else:
        results.fail("init --frappe refuses an unanalysed series before writing anything",
                     stderr.getvalue().strip())


def run_build_fixes(results, workdir):
    """
    Found by the first v16 build (decision #58, and the build-requires fix).

    Each case is something that went wrong on a real machine: a resume that
    ignored a config.toml fix, an image tag that did not exist, and a client
    app whose build backend was not in the venv.
    """
    from erp_sandbox import build_bench, cli, preflight

    # --- A: resuming re-renders ------------------------------------------
    cases = {
        ("infra", None): ["render", "infra", "bench", "site", "start"],
        ("bench", None): ["render", "bench", "site", "start"],
        (None, "site"): ["render", "site"],
        (None, "preflight"): ["preflight"],
        (None, None): list(cli.BUILD_STEPS),
    }
    wrong = {k: cli.build_steps(*k) for k, v in cases.items() if cli.build_steps(*k) != v}
    if not wrong:
        results.ok("build: a resumed build re-renders first, so a config.toml fix reaches it")
    else:
        results.fail("build: a resumed build re-renders first, so a config.toml fix reaches it",
                     wrong)

    # --- B: image tags exist ----------------------------------------------
    parsed = {
        "docker.io/library/redis:8.0-trixie": ("library/redis", "8.0-trixie"),
        "mariadb": ("library/mariadb", "latest"),
        "docker.io/acme/tool:1": ("acme/tool", "1"),
        "ghcr.io/acme/tool:1": None,
        "docker.io/library/python@sha256:abc": None,
    }
    wrong = {i: preflight.hub_repository(i) for i, want in parsed.items()
             if preflight.hub_repository(i) != want}
    if not wrong:
        results.ok("images: Docker Hub references parse; other registries are left alone")
    else:
        results.fail("images: Docker Hub references parse; other registries are left alone", wrong)

    cfg = load_config(write_config(workdir, SAMPLE.read_text(encoding="utf-8"),
                                   name="images.toml"))
    answers = {"library/redis": 404, "library/nginx": None}

    report = preflight.Report()
    preflight.check_images(report, cfg,
                           lookup=lambda repo, tag: answers.get(repo, 200))
    status = {label.split()[0]: kind for kind, label, _ in report.rows}
    if status == {"stack.base_image": "ok", "stack.mariadb_image": "ok",
                  "stack.redis_image": "fail", "stack.nginx_image": "warn"}:
        results.ok("images: a missing tag is refused before any work; an unreachable "
                   "registry only warns")
    else:
        results.fail("images: a missing tag is refused before any work; an unreachable "
                     "registry only warns", report.rows)

    # --- C: the BuildKit warning ------------------------------------------
    dockerfile = (project.ASSET_DIR / "images" / "bench" / "Dockerfile").read_text(encoding="utf-8")
    if dockerfile.splitlines()[0] == "# check=skip=InvalidDefaultArgInFrom":
        results.ok("Dockerfile: the parser directive silencing InvalidDefaultArgInFrom is line 1")
    else:
        results.fail("Dockerfile: the parser directive silencing InvalidDefaultArgInFrom is line 1",
                     "a BuildKit directive anywhere but the top is an ordinary comment")

    # --- each app's build backend reaches the venv --------------------------
    script = build_bench.BUILD_SCRIPT
    start = script.index("install_build_requires() {")
    helper = script.index("install_requirements() {")
    function = script[start:script.index("\n}\n", helper) + 3]
    bench = Path(workdir) / "build-requires"
    (bench / "env" / "bin").mkdir(parents=True)
    record = bench / "pip-requests.txt"
    stub = bench / "env" / "bin" / "python"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
        "    cat /tmp/erp-sandbox-build-requires.txt >> {}\n"
        "    exit 0\n"
        "fi\n"
        'exec {} "$@"\n'.format(record, sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    # hatch_app stands in for hatchling: an in-tree backend that, like
    # hatchling, asks for editables only through the PEP 660 hook -- and asks
    # for frappe too, which must still be left out.
    apps = {
        "hatch_app": ('[build-system]\nrequires = ["hatchling>=1.25"]\n'
                      'build-backend = "fake_backend"\nbackend-path = ["backend"]\n'),
        "needs_frappe": '[build-system]\nrequires = ["frappe>=16", "flit_core >=3.4,<4"]\n',
    }
    for name, text in apps.items():
        (bench / "apps" / name).mkdir(parents=True)
        (bench / "apps" / name / "pyproject.toml").write_text(text, encoding="utf-8")
    (bench / "apps" / "hatch_app" / "backend").mkdir()
    (bench / "apps" / "hatch_app" / "backend" / "fake_backend.py").write_text(
        "def get_requires_for_build_editable(config_settings=None):\n"
        "    return ['editables~=0.3', 'frappe']\n", encoding="utf-8")
    try:
        import pip._vendor.pyproject_hooks  # noqa: F401
        hooks = True
    except ImportError:                     # an interpreter without pip, as in some uv venvs
        hooks = False
    (bench / "apps" / "setup_only").mkdir()
    (bench / "apps" / "setup_only" / "setup.py").write_text("", encoding="utf-8")

    for name in ("hatch_app", "needs_frappe", "setup_only"):
        subprocess.run(
            ["bash", "-c", "set -euo pipefail; constraint_args=''; " + function
             + "\ninstall_build_requires " + name],
            cwd=str(bench), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    requested = record.read_text(encoding="utf-8").split("\n") if record.exists() else []
    requested = [line for line in requested if line]
    pyproject_branch = script[script.index('echo "== installing $app (pyproject)"') - 120:
                              script.index('echo "== installing $app (pyproject)"')]
    if 'install_build_requires "$app"' not in pyproject_branch:
        requested = ["install_build_requires is not called before the pyproject install"]
    expected = (["hatchling>=1.25", "editables~=0.3", "flit_core >=3.4,<4"] if hooks
                else None)
    if hooks and requested == expected:
        results.ok("build: each app's own build backend is installed before its "
                   "--no-build-isolation build; frappe itself is not",
                   "static requires, then the backend's editable hook (editables); "
                   "nothing for a setup.py-only app")
    elif not hooks:
        results.ok("build: build-requires case skipped",
                   "this interpreter has no pip to call the backend hook with")
    else:
        results.fail("build: each app's own build backend is installed before its "
                     "--no-build-isolation build; frappe itself is not", requested)

    # --- assets: yarn in every app, and a "Build failed" that exits 0 -------
    start = script.index('if [ "${SKIP_ASSETS:-}" = "1" ]; then')
    assets = script[start:script.index("\nfi\n", start) + 4]
    for label, bench_output in (("ok", "Done in 1s"), ("failed", "Error: Build failed with 2 errors")):
        bench = Path(workdir) / ("assets-" + label)
        tools = bench / "stub-bin"
        tools.mkdir(parents=True)
        for app in ("frappe", "erpnext", "plain_app"):
            (bench / "apps" / app).mkdir(parents=True)
        for app in ("frappe", "erpnext"):
            (bench / "apps" / app / "package.json").write_text("{}", encoding="utf-8")
        (bench / "apps" / "frappe" / "node_modules").mkdir()
        stubs = {
            "node": "echo v24.0.0",
            "yarn": 'if [ "$1" = "--version" ]; then echo 1.22.22; '
                    'else case " $* " in *" --pure-lockfile "*) basename "$PWD" >> {};; esac; fi'
                    .format(bench / "yarn-ran-in.txt"),
            # Exits 0 either way, as bench build does when esbuild reports errors.
            "bench": "echo '{}'".format(bench_output),
        }
        for name, body in stubs.items():
            (tools / name).write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
            (tools / name).chmod(0o755)
        run = subprocess.run(
            ["bash", "-c", "set -euo pipefail; " + assets],
            cwd=str(bench), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            env=dict(os.environ, PATH="{}:{}".format(tools, os.environ.get("PATH", ""))))
        ran_in = (bench / "yarn-ran-in.txt").read_text(encoding="utf-8").split() \
            if (bench / "yarn-ran-in.txt").exists() else []
        if label == "ok":
            if run.returncode == 0 and ran_in == ["erpnext"]:
                results.ok("build: yarn install runs in every app with a package.json "
                           "and no node_modules yet", "with --pure-lockfile, so yarn.lock "
                           "is never rewritten")
            else:
                results.fail("build: yarn install runs in every app with a package.json "
                             "and no node_modules yet",
                             "exit {}, yarn ran in {}".format(run.returncode, ran_in))
        elif run.returncode != 0 and (bench / "logs" / "erp-sandbox-bench-build.log").exists():
            results.ok("build: a bench build that prints \"Build failed\" fails the step, "
                       "even when it exits 0", "the log is kept in bench/logs/")
        else:
            results.fail("build: a bench build that prints \"Build failed\" fails the step, "
                         "even when it exits 0", run.stdout.decode("utf-8", "replace")[-300:])

    # --- MariaDB 11.6+ snapshot isolation, off and safe on 10.4 -----------
    cnf = (project.ASSET_DIR / "conf" / "mariadb" / "erpnext.cnf").read_text(encoding="utf-8")
    lines = [line.strip() for line in cnf.splitlines()]
    if "loose-innodb_snapshot_isolation = OFF" in lines \
            and "innodb_snapshot_isolation = OFF" not in lines:
        results.ok("mariadb: innodb_snapshot_isolation is OFF, with loose- so 10.4 still starts")
    else:
        results.fail("mariadb: innodb_snapshot_isolation is OFF, with loose- so 10.4 still starts",
                     "without loose-, a server that lacks the variable refuses to start")

    # --- MariaDB 11 images have no mysql* client names ---------------------
    compose = (project.ASSET_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    healthcheck = compose[compose.index("healthcheck:"):compose.index("interval:")]
    if "mariadb-admin ping" in healthcheck and "|| mysqladmin ping" in healthcheck:
        results.ok("mariadb: the healthcheck uses mariadb-admin, falling back to mysqladmin",
                   "MariaDB 11 images have only the first, the oldest only the second")
    else:
        results.fail("mariadb: the healthcheck uses mariadb-admin, falling back to mysqladmin",
                     healthcheck.strip())


def run_db_name_cases(results, workdir):
    """[site].db_name reaches bench new-site only when it is set."""
    from erp_sandbox import seed_db

    sample = SAMPLE.read_text(encoding="utf-8")
    named = sample.replace('# db_name = "acme_dev"', 'db_name = "acme_dev"')
    problems = []
    for label, text in (("unset", sample), ("set", named)):
        try:
            load_config(write_config(workdir, text, name="db-name-{}.toml".format(label)))
        except ConfigError as exc:
            problems.append("{}: {}".format(label, exc.problems))
    if problems:
        results.fail("site.db_name: valid when unset or a plain identifier", problems)
    else:
        results.ok("site.db_name: valid when unset or a plain identifier")

    for label, db_name, expected in (("set", "acme_dev", ["--db-name", "acme_dev"]),
                                     ("unset", "", [])):
        bench = Path(workdir) / ("db-name-" + label)
        tools = bench / "stub-bin"
        tools.mkdir(parents=True)
        record = bench / "new-site-args.txt"
        (tools / "bench").write_text(
            '#!/bin/sh\nfor a in "$@"; do echo "$a"; done >> {}\n'.format(record),
            encoding="utf-8")
        (tools / "bench").chmod(0o755)
        run = subprocess.run(
            ["bash", "-s", "--", "site1", "admin-pw", "root-pw", db_name, "0"],
            input=seed_db.NEW_SITE.split('for app in "$@"; do')[0].encode("utf-8"),
            cwd=str(bench), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            env=dict(os.environ, BENCH_DIR=str(bench),
                     PATH="{}:{}".format(tools, os.environ.get("PATH", ""))))
        args = record.read_text(encoding="utf-8").split("\n") if record.exists() else []
        passed = [a for a in args if a in ("--db-name", "acme_dev")]
        name = "site.db_name {}: bench new-site {}".format(
            label, "gets --db-name acme_dev" if expected else "gets no --db-name")
        if run.returncode == 0 and "new-site" in args and passed == expected:
            results.ok(name)
        else:
            results.fail(name, "exit {}, args {}; {}".format(
                run.returncode, args, run.stdout.decode("utf-8", "replace")[-300:]))


def run_rename_cases(results, workdir):
    """
    Decision #52: the rename is a clean break, and the break explains itself.

    The old names are refused rather than accepted, but each refusal names the
    migration -- otherwise the first thing an existing environment sees after
    an upgrade is "no environment here", which reads as data loss.
    """
    from erp_sandbox import cli

    old = Path(workdir) / "pre-rename-env"
    (old / project.LEGACY_MARKER).mkdir(parents=True)
    try:
        project.find_project_root(old)
    except project.ProjectNotFound as exc:
        text = str(exc)
        if "mv {}/.devstack {}/.erp-sandbox".format(old.resolve(), old.resolve()) in text \
                and "[erp-sandbox]" in text:
            results.ok("rename: a .devstack/ environment is refused, with the mv that migrates it")
        else:
            results.fail("rename: a .devstack/ environment is refused, with the mv that migrates it",
                         text)
    else:
        results.fail("rename: a .devstack/ environment is refused, with the mv that migrates it",
                     "the legacy marker was accepted; #52 is a clean break")

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        try:
            cli.main(["--project", str(old), "ps"])
            code = 0
        except SystemExit as exc:
            code = exc.code
    if code != 0 and "mv " in stderr.getvalue():
        results.ok("rename: --project on a .devstack/ environment names the migration too")
    else:
        results.fail("rename: --project on a .devstack/ environment names the migration too",
                     stderr.getvalue().strip())

    sample = SAMPLE.read_text(encoding="utf-8")
    expect_rejected(
        results, "rename: a leftover [devstack] section is refused, not ignored",
        sample.replace("[erp-sandbox]", "[devstack]"),
        ["[devstack] was renamed [erp-sandbox]"], workdir,
    )

    pyproject = project.PACKAGE_ROOT.parent / "pyproject.toml"
    if pyproject.is_file():
        import tomllib
        scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
        if scripts.get("erp-sandbox") == scripts.get("sandbox") == "erp_sandbox.cli:main":
            results.ok("rename: `erp-sandbox` and its `sandbox` alias are the same entry point")
        else:
            results.fail("rename: `erp-sandbox` and its `sandbox` alias are the same entry point",
                         scripts)
    else:
        results.ok("rename: alias check skipped", "not a source checkout")


def run_env_hygiene(results, workdir):
    """.env holds resolved passwords: it must be private, and hold no token."""
    root = Path(workdir) / "hygiene"
    with build_secrets_only():
        render.main(["--config", str(SAMPLE), "--root", str(root), "--quiet"])

    env_file = root / ".env"
    mode = stat.S_IMODE(env_file.stat().st_mode)
    if mode == 0o600:
        results.ok(".env is written 0600")
    else:
        results.fail(".env is written 0600", "mode is {:o}".format(mode))

    body = env_file.read_text(encoding="utf-8")
    values = [line.split("=", 1)[1].strip()
              for line in body.splitlines()
              if "=" in line and not line.lstrip().startswith("#")]
    leaked = [v for v in values if v in ("env:GIT_PAT", "env:GITLAB_PAT")]
    if leaked:
        results.fail(".env carries no git credential",
                     "found {}".format(", ".join(leaked)))
    else:
        results.ok(".env carries no git credential")


def run_single_version(results):
    """
    The version is declared in exactly one place: erp_sandbox/__init__.py.

    pyproject derives it with `dynamic = ["version"]`, so a literal creeping
    back into [project] would silently reintroduce the drift this replaced --
    `erp-sandbox --version` and `min_version` read the module, while uv and pipx
    report the metadata, and the two can then disagree without anything failing.
    """
    import tomllib
    from erp_sandbox import __version__

    name = "version: declared once, in erp_sandbox/__init__.py"
    pyproject = project.PACKAGE_ROOT.parent / "pyproject.toml"
    if not pyproject.is_file():
        results.ok(name, "not a source checkout")
        return

    data = tomllib.load(pyproject.open("rb"))
    declared = data["project"]
    if "version" in declared:
        results.fail(name, "pyproject [project] carries a literal version = {!r}. "
                           "It must stay dynamic, or the two numbers drift."
                           .format(declared["version"]))
        return
    if "version" not in declared.get("dynamic", []):
        results.fail(name, 'pyproject does not declare dynamic = ["version"]')
        return
    attr = data.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version", {})
    if attr.get("attr") != "erp_sandbox.__version__":
        results.fail(name, "pyproject reads the version from {!r}, not from "
                           "erp_sandbox.__version__".format(attr))
        return
    results.ok(name, __version__)


def run_python_floor(results):
    """
    pyproject's `requires-python` and preflight's MIN_PYTHON must agree.

    They are enforced in different places -- one by the installer, one at run
    time -- so a disagreement is silent in the direction that matters: the
    install succeeds and the tool then refuses to start, or the tool runs on an
    interpreter the metadata said it would not.
    """
    import tomllib

    pyproject = project.PACKAGE_ROOT.parent / "pyproject.toml"
    if not pyproject.is_file():
        # Installed rather than run from a checkout; nothing to compare against.
        results.ok("python floor: pyproject and MIN_PYTHON agree", "not a source checkout")
        return

    declared = tomllib.load(pyproject.open("rb"))["project"]["requires-python"]
    expected = ">={}.{}".format(*preflight_min_python())
    if declared == expected:
        results.ok("python floor: pyproject and MIN_PYTHON agree", declared)
    else:
        results.fail("python floor: pyproject and MIN_PYTHON agree",
                     "pyproject says {!r}, preflight says {!r}".format(declared, expected))


def preflight_min_python():
    from erp_sandbox import preflight
    return preflight.MIN_PYTHON


# Names preflight re-exports so the rest of the package can reach them from the
# one module that is safe to import before the interpreter check has run.
PREFLIGHT_REEXPORTS = (
    "PACKAGE_ROOT", "ProjectNotFound", "config_path", "find_project_root",
    "state_dir",
)


def run_reexports(results):
    """
    preflight's re-exported names are importable.

    Not hypothetical: `ruff --fix` deleted the whole import block on
    2026-08-23, because nothing *inside* preflight.py uses those names and F401
    called them unused. This suite passed all 80 cases afterwards; `render` and
    `doctor` were what broke, on an ImportError, which is a slow and confusing
    way to find out.

    A `# noqa: F401` at the import stops ruff specifically. This stops anything
    -- a refactor, a merge, a linter nobody has run yet.
    """
    from erp_sandbox import preflight

    missing = [n for n in PREFLIGHT_REEXPORTS if not hasattr(preflight, n)]
    if missing:
        results.fail("preflight re-exports what other modules import",
                     "gone from erp_sandbox.preflight: {}. Other modules import "
                     "these from there; restore the `from erp_sandbox.project "
                     "import (...)` block.".format(", ".join(missing)))
    else:
        results.ok("preflight re-exports what other modules import",
                   " ".join(PREFLIGHT_REEXPORTS))


def run_hooks_installed(results):
    """
    In a source checkout, git's hooks should point at the committed directory.

    git never clones hooks -- deliberately, since a cloned hook is arbitrary
    code execution on `git clone`. So `core.hooksPath` has to be set once per
    clone and cannot be made automatic. What *can* be automatic is noticing,
    and this suite is the thing that runs constantly.

    Skipped when not a source checkout: an installed erp-sandbox has no repository
    and its user has nothing to fix.
    """
    import subprocess

    root = project.PACKAGE_ROOT.parent
    if not (root / "pyproject.toml").is_file() or not (root / ".git").exists():
        results.ok("git hooks are installed in this checkout", "not a source checkout")
        return

    try:
        done = subprocess.run(["git", "-C", str(root), "config", "core.hooksPath"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        results.ok("git hooks are installed in this checkout",
                   "could not ask git: {}".format(exc))
        return

    configured = done.stdout.strip()
    if configured == "tools":
        results.ok("git hooks are installed in this checkout", "core.hooksPath = tools")
    else:
        results.fail(
            "git hooks are installed in this checkout",
            "core.hooksPath is {!r}, so tools/pre-commit does not run: no "
            "compileall, no selftest, no staged-secret check on commit. "
            "Fix with:\n    tools/install-hooks.sh".format(configured or "unset"))


def run_genericity(results):
    """
    Decision #17, applied to this suite's own files.

    erp-sandbox doctor will grep the whole tree in Phase 7. Until it exists, at least
    the fixtures police themselves -- they are the files most likely to be
    written by copying a real config.
    """
    banned = ("FILL-ME",)   # placeholders that mean 'unfinished', not 'generic'
    offenders = []
    for path in sorted(FIXTURE_DIR.glob("*.toml")):
        text = path.read_text(encoding="utf-8")
        # A fixture may test FILL-ME deliberately, but must say so.
        if any(b in text for b in banned) and "# expect:" not in text:
            offenders.append(path.name)
    if offenders:
        results.fail("fixtures carry no unexplained placeholder",
                     ", ".join(offenders))
    else:
        results.ok("fixtures carry no unexplained placeholder")


# ---------------------------------------------------------------------------
# fetch_apps, against local repositories
#
# Everything below runs offline. A real clone needs a network and a credential,
# which makes it a poor thing to depend on for a suite meant to run constantly;
# what matters here is the decision-making around the clone -- when to leave a
# tree alone, when to refuse -- and that is testable against a bare repo in a
# temporary directory.
# ---------------------------------------------------------------------------

def local_repo(directory):
    """A bare repo with two commits on 'main' and a tag on the first."""
    work = Path(directory) / "work"
    bare = Path(directory) / "origin.git"
    work.mkdir(parents=True)

    def run(*args, cwd=work):
        import subprocess
        subprocess.run(["git"] + list(args), cwd=str(cwd), check=True,
                       capture_output=True, text=True)

    run("init", "--quiet", "--initial-branch", "main")
    run("config", "user.email", "selftest@example.com")
    run("config", "user.name", "selftest")
    (work / "README.md").write_text("one\n", encoding="utf-8")
    run("add", "README.md")
    run("commit", "--quiet", "-m", "one")
    run("tag", "v1")
    (work / "README.md").write_text("two\n", encoding="utf-8")
    run("commit", "--quiet", "-am", "two")
    run("clone", "--quiet", "--bare", str(work), str(bare), cwd=directory)
    return bare


def app_entry(repo, ref, name="probe"):
    return {"name": name, "repo": str(repo), "ref": ref,
            "requires_credentials": False, "install": True}


def run_fetch_cases(results, workdir):
    import subprocess

    base = Path(workdir) / "fetch"
    base.mkdir()
    origin = local_repo(base)
    target = base / "apps" / "probe"
    git = fetch_apps.Git([], askpass_path=Path(workdir) / "unused-askpass")

    def fetch(ref, dry_run=False):
        return fetch_apps.fetch_one(git, app_entry(origin, ref), target,
                                    None, dry_run=dry_run)

    def check(label, call, want_state):
        try:
            state, _detail = call()
        except fetch_apps.FetchError as exc:
            results.fail(label, str(exc))
            return None
        if state == want_state:
            results.ok(label)
        else:
            results.fail(label, "state was {!r}, expected {!r}".format(state, want_state))
        return state

    def check_refused(label, call, expected):
        try:
            call()
        except fetch_apps.FetchError as exc:
            missing = [w for w in expected if w not in str(exc)]
            if missing:
                results.fail(label, "refused, but not for the stated reason.\n"
                                    "missing: {}\nactual: {}".format(missing, exc))
            else:
                results.ok(label)
            return
        results.fail(label, "did not refuse")

    check("fetch: a dry run touches nothing",
          lambda: fetch("main", dry_run=True), "would-clone")
    if target.exists():
        results.fail("fetch: a dry run touches nothing", "it created {}".format(target))

    check("fetch: clones an app that is not there", lambda: fetch("main"), "cloned")
    check("fetch: a second run changes nothing", lambda: fetch("main"), "unchanged")
    check("fetch: checks out a tag as a detached head", lambda: fetch("v1"), "updated")
    check("fetch: a tag already checked out is unchanged", lambda: fetch("v1"), "unchanged")
    check("fetch: returns to a branch", lambda: fetch("main"), "updated")

    # The behaviour worth guarding hardest: never check out over a dirty tree.
    readme = target / "README.md"
    readme.write_text("edited by the developer\n", encoding="utf-8")
    check("fetch: a dirty tree at the right ref is left alone",
          lambda: fetch("main"), "unchanged")
    check_refused("fetch: a dirty tree at the wrong ref is refused",
                  lambda: fetch("v1"),
                  ["uncommitted changes", "Refusing to check out over your work"])
    if readme.read_text(encoding="utf-8") != "edited by the developer\n":
        results.fail("fetch: the developer's edit survives a refusal",
                     "the working tree was modified")
    else:
        results.ok("fetch: the developer's edit survives a refusal")
    readme.write_text("two\n", encoding="utf-8")

    subprocess.run(["git", "remote", "set-url", "origin", "https://example.com/other.git"],
                   cwd=str(target), check=True, capture_output=True)
    check_refused("fetch: a remote pointing elsewhere is refused",
                  lambda: fetch("main"),
                  ["but config.toml says", "remote set-url"])
    subprocess.run(["git", "remote", "set-url", "origin", str(origin)],
                   cwd=str(target), check=True, capture_output=True)

    check_refused("fetch: a ref that does not exist is refused",
                  lambda: fetch("no-such-ref"), ["no-such-ref"])

    # A directory that is not a repo must be reported, never deleted.
    stranger = base / "apps" / "stranger"
    stranger.mkdir(parents=True)
    (stranger / "notes.txt").write_text("someone's files\n", encoding="utf-8")
    check_refused(
        "fetch: a non-repo directory is reported, not deleted",
        lambda: fetch_apps.fetch_one(git, app_entry(origin, "main", "stranger"),
                                     stranger, None),
        ["is not a git repository", "will not delete"])
    if (stranger / "notes.txt").exists():
        results.ok("fetch: the non-repo directory still has its files")
    else:
        results.fail("fetch: the non-repo directory still has its files", "deleted")

    # blobless, decision #31. Two things are worth proving offline: the filter
    # actually reaches git, and it is off unless asked for.
    #
    # `git clone --filter` against a local *path* silently ignores the filter
    # and hardlinks instead, so the origin is addressed as file:// -- which
    # makes it a server, and a server has to be willing to filter.
    subprocess.run(["git", "-C", str(origin), "config", "uploadpack.allowfilter", "true"],
                   check=True, capture_output=True)

    def filter_of(path):
        done = subprocess.run(["git", "-C", str(path), "config", "--get",
                               "remote.origin.partialclonefilter"],
                              capture_output=True, text=True)
        return done.stdout.strip()

    if filter_of(target):
        results.fail("fetch: a clone is not partial unless the app asks",
                     "an app without blobless got {!r}".format(filter_of(target)))
    else:
        results.ok("fetch: a clone is not partial unless the app asks")

    label = "fetch: blobless clones with --filter=blob:none"
    thrifty = base / "apps" / "thrifty"
    app = app_entry("file://{}".format(origin), "main", name="thrifty")
    app["blobless"] = True
    try:
        state, detail = fetch_apps.fetch_one(git, app, thrifty, None)
    except fetch_apps.FetchError as exc:
        results.fail(label, str(exc))
    else:
        got = filter_of(thrifty)
        if got != "blob:none":
            results.fail(label, "remote.origin.partialclonefilter is {!r}".format(got))
        elif state != "cloned" or "(blobless)" not in detail:
            results.fail(label, "reported {!r} {!r}".format(state, detail))
        elif not (thrifty / "README.md").is_file():
            results.fail(label, "the working tree was not checked out")
        else:
            results.ok(label)


def run_fetch_secrecy(results):
    """The parts of the credential path that can be checked without a network."""
    token = "ghp_selftest_not_a_real_token"

    scrubbed = fetch_apps.redact(
        "fatal: could not read from https://x:{}@example.com/a.git".format(token),
        [token])
    if token in scrubbed:
        results.fail("fetch: git output is redacted before printing", scrubbed)
    elif "<redacted>" in scrubbed:
        results.ok("fetch: git output is redacted before printing")
    else:
        results.fail("fetch: git output is redacted before printing",
                     "nothing was substituted")

    if token in fetch_apps.ASKPASS_SCRIPT or "ERP_SANDBOX_GIT_TOKEN" not in fetch_apps.ASKPASS_SCRIPT:
        results.fail("fetch: the askpass helper names variables, not values",
                     "the helper script does not read the token from the environment")
    else:
        results.ok("fetch: the askpass helper names variables, not values")

    git = fetch_apps.Git([token], askpass_path=Path("/nonexistent/askpass"))
    env = git.env_for(None)
    if env.get("GIT_TERMINAL_PROMPT") != "0":
        results.fail("fetch: prompts are disabled so a build cannot hang",
                     "GIT_TERMINAL_PROMPT is {!r}".format(env.get("GIT_TERMINAL_PROMPT")))
    elif "GIT_ASKPASS" in env or token in env.values():
        results.fail("fetch: an app needing no credential is given none",
                     "the environment carries a credential anyway")
    else:
        results.ok("fetch: prompts are disabled so a build cannot hang")
        results.ok("fetch: an app needing no credential is given none")

    env = git.env_for({"username": "someone", "token": token, "host": "example.com"})
    if env.get("ERP_SANDBOX_GIT_TOKEN") == token and env.get("GIT_ASKPASS"):
        results.ok("fetch: a credential reaches git through the environment only")
    else:
        results.fail("fetch: a credential reaches git through the environment only",
                     "askpass or token missing from the environment")

    # The likeliest real-world failure must explain itself.
    app = app_entry("https://example.com/acme/private.git", "main")
    hint = fetch_apps.hint_for(
        "fatal: could not read Username for 'https://example.com': terminal prompts disabled",
        app, None)
    if hint and "requires_credentials = true" in hint:
        results.ok("fetch: a private https repo with no credential says so")
    else:
        results.fail("fetch: a private https repo with no credential says so",
                     "hint was {!r}".format(hint))


def run_bind_mount_owners(results, workdir):
    """
    Every host path the compose file bind-mounts must be created host-side.

    The engines disagree about a missing bind-mount source: rootless podman
    creates it as the invoking user, a rootful Docker daemon creates it as
    root. MariaDB runs as the host uid (decision #25), so a root-owned datadir
    stops it dead -- which is how the Docker path failed while podman passed.
    A mount added here later without a creator would fail the same way, on
    somebody else's machine, weeks later.
    """
    import re as _re
    from erp_sandbox import project, render

    name = "every compose bind-mount source is created host-side"
    compose = (project.ASSET_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    sources = set()
    for line in compose.splitlines():
        match = _re.match(r"\s*-\s+\./([^:]+):", line)
        if match:
            sources.add(match.group(1).strip())
    if not sources:
        results.fail(name, "parsed no bind mounts out of docker-compose.yml")
        return

    with build_secrets_only():
        cfg = load_config(write_config(workdir, SAMPLE.read_text(encoding="utf-8")))
    rendered = set(render.output_paths(cfg))

    unowned = []
    for source in sorted(sources):
        if source in rendered:
            continue                      # erp-sandbox render writes it
        if source in project.DATA_DIRS:
            continue                      # project.ensure_data_dirs creates it
        if source == "bench" or source.startswith("bench/"):
            continue                      # erp-sandbox fetch, two steps before infra
        unowned.append(source)

    if unowned:
        results.fail(name, "nothing creates {} before a container mounts it, so "
                           "the engine will -- as root under Docker. Add it to "
                           "project.DATA_DIRS or render it."
                           .format(", ".join(repr(u) for u in unowned)))
    else:
        results.ok(name, "{} mount(s)".format(len(sources)))

    name = "ensure_data_dirs creates the datadir, and is idempotent"
    root = Path(workdir) / "datadirs"
    root.mkdir(exist_ok=True)
    first = project.ensure_data_dirs(root)
    second = project.ensure_data_dirs(root)
    made = project.data_dir(root)
    if first == list(project.DATA_DIRS) and second == [] and made.is_dir():
        results.ok(name, ", ".join(first))
    else:
        results.fail(name, "first={!r} second={!r} exists={}".format(
            first, second, made.is_dir()))

    name = "the chown remedy matches the engine in use"
    from erp_sandbox.doctor import chown_remedy
    podman = chown_remedy("podman", 1000, 1000)
    docker = chown_remedy("docker", 1000, 1000)
    if podman.startswith("podman unshare chown") and docker.startswith("sudo chown"):
        results.ok(name, "{} / {}".format(podman, docker))
    else:
        results.fail(name, "podman -> {!r}, docker -> {!r}".format(podman, docker))


def run_version_reporting(results):
    """
    `--version` names the package path, not only the number.

    An editable install and a copied one report the same version and behave
    differently; the path is the only thing that separates them, and telling
    them apart is step 3 of contributing.md.
    """
    from erp_sandbox import __version__, cli, project

    name = "cli: --version reports the package path"
    parser = cli.build_parser()
    action = next((a for a in parser._actions if "--version" in a.option_strings), None)
    if action is None:
        results.fail(name, "no --version action on the parser")
        return
    text = action.version
    if __version__ in text and str(project.PACKAGE_ROOT) in text:
        results.ok(name, text)
    else:
        results.fail(name, "version string does not carry both the number and "
                           "the path: {!r}".format(text))


def run_constraints_cases(results, workdir):
    """
    Decision #36: pip constraints live in config.toml, seeded once by init.

    The cases that matter are the two ends of it -- that the seed is really
    there for a new environment, and that an environment which empties or omits
    the list is not quietly given one back.
    """
    from erp_sandbox import build_bench, known_versions
    from erp_sandbox.preflight import constraints_warning, pip_constraints

    sample = SAMPLE.read_text(encoding="utf-8")
    template = known_versions.constraints_template(12)

    # The sample is what `erp-sandbox init` copies, so a drift between it and the
    # series template ships an environment seeded with the wrong ceilings.
    name = "config.sample.toml carries the v12 constraints template"
    with build_secrets_only():
        cfg = load_config(write_config(workdir, sample))
    declared = tuple(pip_constraints(cfg))
    if declared == tuple(template):
        results.ok(name, ", ".join(declared))
    else:
        results.fail(name, "sample has {!r}, known_versions.py has {!r}. One of "
                           "them was edited without the other.".format(
                               declared, tuple(template)))

    name = "a declared list silences the missing-key warning"
    if constraints_warning(cfg) is None:
        results.ok(name)
    else:
        results.fail(name, "warned about a key that is present")

    # An empty list is a choice, and choices are respected.
    name = "an empty pip_constraints list means no constraints"
    empty = sample.replace('pip_constraints = ["numpy<1.24"]', "pip_constraints = []")
    with build_secrets_only():
        cfg_empty = load_config(write_config(workdir, empty))
    if pip_constraints(cfg_empty) == [] and constraints_warning(cfg_empty) is None:
        results.ok(name)
    else:
        results.fail(name, "an explicit empty list was not respected, or warned")

    # Absence is not a choice -- it predates the key -- so it warns.
    name = "a config predating the key is accepted and warned about"
    absent = "\n".join(line for line in sample.splitlines()
                       if not line.startswith("pip_constraints ="))
    with build_secrets_only():
        cfg_absent = load_config(write_config(workdir, absent))
    warning = constraints_warning(cfg_absent)
    if pip_constraints(cfg_absent) != []:
        results.fail(name, "a missing key produced constraints from somewhere")
    elif warning and "numpy<1.24" in warning and "pip_constraints = []" in warning:
        results.ok(name, "names the template and the way to silence it")
    else:
        results.fail(name, "no warning, or one that does not name the remedy:\n"
                           "{}".format(warning))

    # The failure that started all this, as pip actually reports it.
    name = "a constraint-caused resolution failure is explained"
    pip_output = (
        "ERROR: Could not find a version that satisfies the requirement "
        "numpy==1.26.4 (from erpnext)\n"
        "ERROR: No matching distribution found for numpy==1.26.4\n"
    )
    package, entry = build_bench.constraint_blamed(pip_output, ["numpy<1.24"])
    if package == "numpy" and entry == "numpy<1.24":
        results.ok(name, "numpy==1.26.4 vs numpy<1.24")
    else:
        results.fail(name, "blamed {!r}/{!r}".format(package, entry))

    # A package nobody capped must not be blamed on a constraint.
    name = "an unconstrained package is not blamed on a constraint"
    package, _ = build_bench.constraint_blamed(
        "ERROR: No matching distribution found for nosuchpkg==9.9\n", ["numpy<1.24"])
    if package is None:
        results.ok(name)
    else:
        results.fail(name, "invented a constraint explanation for {!r}".format(package))

    # PEP 503 names: a constraint on Foo_Bar must match a failure for foo-bar.
    name = "constraint blame normalises package names"
    package, _ = build_bench.constraint_blamed(
        "ERROR: No matching distribution found for Foo_Bar==2.0\n", ["foo-bar<1.0"])
    if package == "Foo_Bar":
        results.ok(name)
    else:
        results.fail(name, "did not match Foo_Bar against foo-bar")


def main():
    results = Results()
    sample_text = SAMPLE.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="erp-sandbox-selftest-") as workdir:
        print("rejection cases")
        run_fixture_files(results, workdir)
        run_mutations(results, sample_text, workdir)
        run_missing_file(results, workdir)

        print("\nacceptance cases")
        run_accepted_remotes(results, sample_text, workdir)
        run_render_roundtrip(results, workdir)
        run_constraints_cases(results, workdir)
        run_version_reporting(results)
        run_bind_mount_owners(results, workdir)

        print("\nshared VPS")
        run_vps_cases(results, workdir)
        run_rename_cases(results, workdir)
        run_db_name_cases(results, workdir)

        print("\nfrappe series")
        run_series_cases(results, workdir)
        run_build_fixes(results, workdir)

        print("\nfetch_apps")
        run_fetch_cases(results, workdir)
        run_fetch_secrecy(results)

        print("\nhygiene")
        run_debug_cases(results, workdir)
        run_briefing_cases(results, workdir)
        run_workspace_cases(results, workdir)
        run_guard_cases(results, workdir)
        run_gitignore_coverage(results, workdir)
        run_compose_context(results, workdir)
        run_cli_cases(results, workdir)
        run_env_hygiene(results, workdir)
        run_genericity(results)
        run_single_version(results)
        run_python_floor(results)
        run_reexports(results)
        run_hooks_installed(results)

    total = results.passed + len(results.failures)
    print()
    if results.failures:
        print("selftest FAILED: {} of {} case(s)".format(len(results.failures), total))
        for name in results.failures:
            print("  - {}".format(name))
        return 1
    print("selftest passed: {} case(s)".format(total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
