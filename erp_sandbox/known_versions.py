#!/usr/bin/env python3
"""
known_versions.py — the Frappe releases this tool knows how to build.

Decision #28. Getting a Frappe version to install is not something the tool can
work out at runtime: it took reading v12's own requirements.txt to learn that it
pins `ldap3` and `PyMySQL` rather than the compiled alternatives, and that its
era predates the setuptools release which removed `use_2to3`. That is human
analysis, done once per Frappe series, and its output is a small table.

So the table is the tool's supported range. A configuration naming a version
that is not in here is **refused**, because the alternative is a build that
proceeds on assumptions nobody has checked and fails somewhere in the middle
with a message about a package nobody has heard of.

Adding a version means doing the analysis, not adding a number.
"""

# Each entry answers: what does this Frappe series need from the machinery that
# installs it?
#
#   python              the Python the series needs, major.minor. Not a
#                       config.toml key: the base image decides it, and the
#                       image build checks that it does (decision #57)
#   node                the Node major the series needs; [stack].node is
#                       checked against it
#   legacy_installer    the pip and setuptools this era's *framework* needs, or
#                       None. v12's requirements predate setuptools 58, which
#                       removed use_2to3, and several packages still declare
#                       it. None means a setup.py app gets the modern installer
#                       too -- pip<21 cannot even run on v16's Python
#   modern_installer    what a current pyproject-only app needs. Both are
#                       needed in one venv for v12: see the two-phase install
#                       in build_bench.py. Pyproject apps are installed with
#                       --no-build-isolation, so their build backend must be in
#                       here too (v16: flit_core)
#   bench_cli_installer pip and setuptools for the bench CLI's own venv in the
#                       image, which runs on the same Python
#   bench_min           the oldest bench CLI this series is known to work with
#   mariadb_range       (low, high) MariaDB versions frappe supports, or None
#                       when nobody recorded one. Outside it preflight warns
#   app_ref             the branch frappe and erpnext use for this series
#   stack_defaults      what `erp-sandbox init --frappe <series>` writes into
#                       [stack]. Seeds, like constraints_template: the build
#                       reads config.toml, never these (decision #56)
#   system_packages     Debian packages the image needs for this series' own
#                       requirements to install. Merged with the environment's
#                       [stack].extra_packages, which covers what its *apps*
#                       need
#   constraints_template  what `erp-sandbox init` writes into a new config.toml as
#                       [stack].pip_constraints. A seed, not a default: the
#                       build never reads it (decision #36)
#   verified            when someone actually built it and watched it work
FRAPPE_VERSIONS = {
    12: {
        "label": "v12",
        "python": "3.9",
        "node": "12",
        "legacy_installer": ("pip<21", "setuptools<58"),
        "bench_cli_installer": ("pip<21", "setuptools<58"),
        "bench_min": "5.24.1",
        "mariadb_range": None,
        "app_ref": "version-12",
        "stack_defaults": {
            "base_image": "docker.io/library/debian:bullseye-slim",
            "node": "12.22.12",
            "yarn": "1.22.22",
            "wkhtmltopdf": "0.12.6.1-2",
            "bench": "5.24.1",
            "mariadb_image": "docker.io/library/mariadb:10.4.6",
            "redis_image": "docker.io/library/redis:6.2-bullseye",
            "nginx_image": "docker.io/library/nginx:1.24-bullseye",
        },
        # Capped rather than open-ended: v12 code still reaches for
        # pkg_resources, which newer setuptools has been steadily deprecating.
        # Raise the cap when someone has run a v12 site against a newer one.
        "modern_installer": ("pip>=23.1", "setuptools>=64,<70"),
        # Nothing series-specific. libpq-dev was briefly listed here, then moved
        # into the base image: every Frappe version pins psycopg2 something, so
        # it is a property of the framework rather than of a series, and 2.2 MB
        # is not worth deciding per version. The mechanism stays for a series
        # that genuinely needs a package the others do not.
        "system_packages": (),
        # The pip constraints `erp-sandbox init` SEEDS INTO config.toml for this
        # series. Never read at build time -- the build reads
        # [stack].pip_constraints and nothing else (decision #36).
        #
        # A requirements.txt written in 2019 pins its direct dependencies and
        # says nothing about theirs, so pip is free to resolve a transitive
        # dependency to something released years later. Vanilla v12 pins
        # openpyxl==2.6.4, whose compat layer reads numpy.float -- removed in
        # numpy 1.24 -- and nothing in the tree caps numpy, so pip installed
        # 2.0.2 and the site build died importing openpyxl.
        #
        # A fork that has modernised its pins (openpyxl 3.x) does not need this
        # and is actively broken by it: one pinning numpy==1.26.4 has no
        # candidate left under the cap, and pip reports only "No matching
        # distribution found". Seeding it into config.toml rather than applying
        # it from here is what lets that environment answer back.
        #
        # Entries carry no whitespace; see PIP_CONSTRAINT_RE in preflight.py.
        "constraints_template": ("numpy<1.24",),
        # What was verified, precisely. "v12" is not one thing: a fork whose
        # pins have been modernised and a vanilla checkout need different
        # treatment, and both are legitimately frappe_version = 12.
        "verified": "2026-08-23, vanilla frappe/erpnext version-12 and a fork "
                    "with modernised pins",
        "notes": (
            "Framework and ERPNext are setup.py based and need the legacy "
            "installer. Client apps written recently are pyproject-only and "
            "need the modern one. Both appear in the same bench."
        ),
    },
    16: {
        "label": "v16",
        # frappe and erpnext both say requires-python ">=3.14,<3.15" / ">=3.14".
        # No Debian release ships 3.14, so the base image is the official
        # python image, which provides it in /usr/local (decision #55).
        "python": "3.14",
        # frappe's package.json: "engines": {"node": ">=24"}.
        "node": "24",
        # pip<21 cannot run on 3.14, and neither app has a setup.py.
        "legacy_installer": None,
        # flit_core because frappe and erpnext both build with it, and
        # pyproject apps are installed with --no-build-isolation.
        "modern_installer": ("pip>=24", "setuptools>=70", "flit_core>=3.4,<4"),
        "bench_cli_installer": ("pip>=24", "setuptools>=70"),
        # The bench CLI the local v16 bench was built with.
        "bench_min": "5.29.0",
        # frappe/database/mariadb/setup_db.py warns below 10.6 and above 11.8.
        "mariadb_range": ("10.6", "11.8"),
        "app_ref": "version-16",
        # mysqlclient has no Linux wheel and builds from source; its own README
        # names default-libmysqlclient-dev (plus pkg-config and a compiler, both
        # in the image) for Debian. WeasyPrint needs Pango and HarfBuzz at run
        # time. `bench restore` shells out to `file` to detect an encrypted
        # backup (frappe/commands/site.py) and fails without it; the slim
        # base image does not carry it. Package names are bookworm's.
        "system_packages": (
            "default-libmysqlclient-dev",
            "libpango-1.0-0", "libpangoft2-1.0-0", "libharfbuzz-subset0",
            "file",
        ),
        # Nothing known to need capping yet. A seed, so an environment can add
        # its own.
        "constraints_template": (),
        "stack_defaults": {
            # bookworm, not trixie: wkhtmltopdf's patched-qt .deb exists for
            # bookworm and not for trixie (decision #55).
            "base_image": "docker.io/library/python:3.14-slim-bookworm",
            "node": "24.13.0",
            "yarn": "1.22.22",
            "wkhtmltopdf": "0.12.6.1-3",
            "bench": "5.29.0",
            "mariadb_image": "docker.io/library/mariadb:11.8",
            "redis_image": "docker.io/library/redis:7.4-bookworm",
            "nginx_image": "docker.io/library/nginx:1.28-bookworm",
        },
        # Read from a local v16 bench (frappe 16.20.0, 2026-06-02), not yet
        # built by this tool. Fill in from the first real build.
        "verified": "",
        "notes": (
            "Python 3.14 from the official python image, not Debian. Both "
            "framework and ERPNext are flit_core pyproject builds; no legacy "
            "installer."
        ),
    },
}

# wkhtmltopdf/packaging releases, and the Debian releases each ships a patched-qt
# .deb for. The Dockerfile builds the download name from the base image's own
# codename, so a release missing its codename fails the build at the download.
# Checked 2026-11-02 against the project's GitHub releases; 0.12.6.1-3
# (2023-05-22) is the last one published.
WKHTMLTOPDF_DEBS = {
    "0.12.6.1-2": ("bullseye",),
    "0.12.6.1-3": ("bullseye", "bookworm"),
}

DEBIAN_CODENAMES = ("bullseye", "bookworm", "trixie")

SUPPORTED = tuple(sorted(FRAPPE_VERSIONS))


def is_supported(version):
    return version in FRAPPE_VERSIONS


def get(version):
    """The entry for a version, or None."""
    return FRAPPE_VERSIONS.get(version)


def supported_list():
    """Human-readable, for error messages."""
    return ", ".join(FRAPPE_VERSIONS[v]["label"] for v in SUPPORTED)


def unsupported_message(version):
    return (
        "frappe_version = {!r} is not one this tool knows how to build. "
        "Supported: {}. Each entry in erp_sandbox/known_versions.py records what "
        "that Frappe series needs from the installer, which is human analysis "
        "of its requirements rather than something detectable at run time "
        "(decision #28). Adding a version means doing that analysis."
        .format(version, supported_list())
    )


def installer_pins(entry):
    """(legacy, modern, bench_cli) as space-joined strings; legacy may be ''."""
    legacy = entry["legacy_installer"]
    return (" ".join(legacy) if legacy else "",
            " ".join(entry["modern_installer"]),
            " ".join(entry["bench_cli_installer"]))


def constraints_template(version):
    """
    The pip constraints `erp-sandbox init` seeds into a new config.toml.

    A template, not a default: once an environment exists, [stack].pip_constraints
    is the only answer, and an environment that empties it gets no constraints
    (decision #36).
    """
    entry = FRAPPE_VERSIONS.get(version)
    return tuple(entry["constraints_template"]) if entry else ()
