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
#   python / node       what the base image must provide; config.toml's
#                       [stack] values are checked against these
#   legacy_installer    the pip and setuptools this era's *framework* needs.
#                       v12's requirements predate setuptools 58, which removed
#                       use_2to3, and several packages still declare it
#   modern_installer    what a current pyproject-only app needs instead. Both
#                       are needed in one venv: see the two-phase install in
#                       build_bench.py
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
}

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


def constraints_template(version):
    """
    The pip constraints `erp-sandbox init` seeds into a new config.toml.

    A template, not a default: once an environment exists, [stack].pip_constraints
    is the only answer, and an environment that empties it gets no constraints
    (decision #36).
    """
    entry = FRAPPE_VERSIONS.get(version)
    return tuple(entry["constraints_template"]) if entry else ()
