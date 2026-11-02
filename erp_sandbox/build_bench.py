#!/usr/bin/env python3
"""
build_bench.py — create bench/env and install every app into it.

Step 6 of the build (functional.md section 4), and the one that turns a
directory of cloned source into a working bench.

Everything happens **inside the container**, in a one-off `compose run`. That is
not a stylistic choice: bench/env is bind-mounted (decision #22), so the
interpreter and console-script shebangs written into it are the base image's
Python 3.9. Building it on the host would produce a venv pointing at a host
interpreter that does not exist in the container, and the failure would surface
much later as a confusing ImportError.

The bench service itself cannot be used, because its entrypoint refuses to start
honcho until bench/env exists -- which is what this creates. Hence `run --rm
--no-deps`, a container that lives for the length of this job.

Risk 2 is the reason for the pins. ERPNext v12's requirements predate
setuptools 58, which removed `use_2to3`; several v12-era packages still declare
it. pip and setuptools are pinned in the venv **before anything else is
installed**, because pinning them afterwards would be too late.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


from erp_sandbox import known_versions  # noqa: E402
from erp_sandbox.preflight import (  # noqa: E402
    ConfigError,
    constraints_warning,
    detect_engine,
    load_config,
    pip_constraints,
)

# Risk 2, and the other half of it that Phase 5 discovered.
#
# The framework and ERPNext are setup.py based and pre-date setuptools 58,
# which removed use_2to3 -- so they need an old installer. Client apps written
# any time recently are pyproject-only, and an editable install of one needs
# PEP 660, which arrived in pip 21.3. Both kinds live in the same bench.
#
# So the installer is switched mid-run, in config.toml order, at the boundary
# where the first modern app appears. The values come from
# known_versions.py rather than from here, because they are a property of the
# Frappe series (decision #28).
#
# The script runs inside the container with the app names as positional
# arguments, and works out for itself which kind each app is -- it has the
# filesystem in front of it, so there is nothing for the host to guess.
BUILD_SCRIPT = r"""
set -euo pipefail

cd "${BENCH_DIR:?BENCH_DIR is not set; this is not the bench image}"

legacy_pins="$1"; shift
modern_pins="$1"; shift
constraints="$1"; shift

# Constraints bound what pip may resolve *transitive* dependencies to. A 2019
# requirements.txt pins what it names and nothing beneath it, so without these
# pip is free to install a dependency-of-a-dependency released years later.
constraint_args=""
if [ -n "$constraints" ]; then
    printf "%s\n" $constraints > /tmp/erp-sandbox-constraints.txt
    constraint_args="-c /tmp/erp-sandbox-constraints.txt"
    echo "== constraints: $constraints"
fi

echo "== python: $(python3 --version 2>&1), $(command -v python3)"

# bench decides whether a directory is a bench by looking for all of these
# (bench.utils.paths_in_bench). `bench init` would create them; this tool
# builds the tree itself, so it creates them itself. Without them every bench
# command answers "Command not being executed in bench directory" and then
# hides most of its own subcommands, which is a confusing way to learn that a
# directory is empty.
echo "== bench skeleton"
mkdir -p apps sites config logs config/pids

if [ -x env/bin/python ]; then
    echo "== env/ exists, reusing it"
else
    echo "== creating env/"
    python3 -m venv env
fi

installer=""

use_installer() {
    want="$1"; pins="$2"
    [ "$installer" = "$want" ] && return 0
    if [ "$want" = "legacy" ] && [ "$installer" = "modern" ]; then
        echo "FATAL: a setup.py app is listed after a pyproject-only one." >&2
        echo "  The installer would have to be downgraded mid-build. Put the" >&2
        echo "  framework and any other setup.py apps first in [[apps]]." >&2
        exit 1
    fi
    echo "== switching to the $want installer: $pins"
    # shellcheck disable=SC2086
    env/bin/python -m pip install --quiet --upgrade $pins wheel
    echo "   pip $(env/bin/python -m pip --version | cut -d' ' -f2), setuptools $(env/bin/python -c 'import setuptools; print(setuptools.__version__)')"
    installer="$want"
}

for app in "$@"; do
    [ -d "apps/$app" ] || {
        echo "FATAL: apps/$app is missing. Run `erp-sandbox fetch` on the host." >&2
        exit 1
    }
    if [ -f "apps/$app/setup.py" ]; then
        use_installer legacy "$legacy_pins"
        echo "== installing $app (setup.py)"
        # shellcheck disable=SC2086
        env/bin/python -m pip install --no-cache-dir $constraint_args -e "apps/$app"
    else
        use_installer modern "$modern_pins"
        echo "== installing $app (pyproject)"
        # --no-build-isolation on purpose. These apps declare frappe among
        # their build requirements, and an isolated build would go to PyPI for
        # a package named "frappe" that is not this bench's frappe. Everything
        # the build needs -- setuptools, wheel, frappe -- is already in the venv.
        # shellcheck disable=SC2086
        env/bin/python -m pip install --no-cache-dir --no-build-isolation $constraint_args -e "apps/$app"
    fi
done

echo "== installed:"
env/bin/python -m pip list --format=freeze 2>/dev/null \
    | grep -iE "^(frappe|erpnext)" || true

# Apps declare their own dependencies, and nothing stops one of them from
# requiring a version the framework has pinned to something else. pip reports
# the conflict once, in the middle of a long install log, and then exits 0.
# Ask it again at the end, where the answer can be seen.
if [ "${SKIP_ASSETS:-}" = "1" ]; then
    echo "== assets: skipped"
else
    # yarn install is only needed once, and it is the slow half. bench build
    # itself is quick, and is what turns rebuilt sources into the bundles nginx
    # serves straight off the bind mount.
    if [ -d "apps/frappe/node_modules" ]; then
        echo "== node_modules present, skipping yarn install"
    else
        echo "== yarn install (node $(node --version), yarn $(yarn --version))"
        ( cd apps/frappe && yarn install --non-interactive ) 2>&1 | tail -3
    fi
    echo "== bench build"
    bench build 2>&1 | grep -E "Built|Done|error|Error" | tail -20
fi

# debugpy, so that [debug].enabled is a render-and-restart away rather than a
# rebuild. Installed unconditionally and on purpose: it is a 5 MB pure-Python
# wheel with no dependencies of its own, so it cannot participate in the
# conflicts of risk 16, and it does nothing at all unless the Procfile's web
# line wraps the server in it. The alternative -- installing only when the flag
# is on -- turns flipping that flag into a ModuleNotFoundError in a process the
# developer is not watching.
#
# Last, after the apps, so it cannot disturb the pinned-installer sequence
# above, and so a failure here cannot cost a completed app install.
echo "== debugpy (for [debug], decision #29)"
env/bin/python -m pip install --no-cache-dir --quiet debugpy \
    && echo "   $(env/bin/python -c 'import debugpy; print("debugpy", debugpy.__version__)')" \
    || echo "   WARNING: debugpy did not install; [debug].enabled will not work"

echo "== dependency check"
if env/bin/python -m pip check > /tmp/pipcheck 2>&1; then
    echo "   no conflicts"
else
    echo "DEPENDENCY-CONFLICTS-FOLLOW"
    sed "s/^/   /" /tmp/pipcheck
fi
"""


# pip's own report of an unsatisfiable requirement names the package and
# nothing about *why* no candidate was left. When the reason is a constraint
# erp-sandbox itself applied, that difference is forty minutes of a developer's
# afternoon -- decision #36 exists because it cost exactly that once.
_NO_CANDIDATE_RE = re.compile(r"No matching distribution found for ([A-Za-z0-9._-]+)")
_CONSTRAINT_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def canonical(name):
    """PEP 503 normalisation, so numpy, NumPy and numpy_ are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def constraint_blamed(output, constraints):
    """
    (package, constraint) when a constraint explains a resolution failure.

    Only fires when both are true: pip found no candidate for a package, and
    that package is one this build capped. A failure for a package nobody
    constrained is a real "not on PyPI" and gets no invented explanation.
    """
    by_package = {}
    for entry in constraints:
        match = _CONSTRAINT_NAME_RE.match(entry)
        if match:
            by_package[canonical(match.group(1))] = entry
    for package in _NO_CANDIDATE_RE.findall(output):
        entry = by_package.get(canonical(package))
        if entry:
            return package, entry
    return None, None


def compose_command(engine, compose_cmd, root):
    """The `<engine> compose -f base -f overlay` prefix, as a list."""
    overlay = Path(root) / "compose.{}.yml".format(engine)
    if not overlay.is_file():
        raise SystemExit(
            "{} is missing. Run `erp-sandbox render` first.".format(overlay)
        )
    return compose_cmd.split() + [
        "-f", str(Path(root) / "docker-compose.yml"),
        "-f", str(overlay),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create bench/env and install every app from config.toml into it.",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", required=True,
                        help="the project root")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the app order and the command; run nothing")
    parser.add_argument("--skip-assets", action="store_true",
                        help="install the apps but do not build the frontend bundles")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        sys.stderr.write("config.toml is not usable:\n")
        for problem in exc.problems:
            sys.stderr.write("  - {}\n".format(problem))
        return 2

    # Every app is installed into the venv, in config.toml order, including
    # those with install = false. That flag governs whether the app is
    # installed on the *site* (apps.txt), not whether it is importable --
    # a v12 app that is present but not pip-installed is not importable at all.
    apps = [app["name"] for app in cfg["apps"]]

    engine, compose_cmd = detect_engine()
    if engine is None:
        sys.stderr.write("no container engine found; preflight explains what is needed\n")
        return 2
    if compose_cmd is None:
        sys.stderr.write("{} is installed but its compose front end is not\n".format(engine))
        return 2

    known = known_versions.get(cfg["stack"]["frappe_version"])
    if known is None:                       # preflight refuses this first
        sys.stderr.write(known_versions.unsupported_message(
            cfg["stack"]["frappe_version"]) + "\n")
        return 2

    constraints = pip_constraints(cfg)

    # Repeated from preflight on purpose: `erp-sandbox build --from bench` skips
    # preflight, and that is the command a failed build tells the developer to
    # run. A warning only the skipped step prints is a warning nobody sees.
    warning = constraints_warning(cfg)
    if warning:
        sys.stderr.write("warning: " + warning + "\n\n")

    command = compose_command(engine, compose_cmd, args.root) + [
        "run", "--rm", "--no-deps", "-T",
        "-e", "SKIP_ASSETS={}".format("1" if args.skip_assets else "0"),
        "bench",
        "bash", "-s", "--",
        " ".join(known["legacy_installer"]),
        " ".join(known["modern_installer"]),
        # config.toml and nothing else (decision #36). known_versions.py seeds
        # this list at `erp-sandbox init` and is not consulted again, so an
        # environment that empties it gets no constraints. The space join is
        # safe because preflight forbids whitespace inside an entry.
        " ".join(constraints),
    ] + apps

    print("installing {} app(s) into bench/env, in config.toml order:".format(len(apps)))
    print("  {}".format(" -> ".join(apps)))
    print("frappe {}: legacy {}, modern {}".format(
        known["label"], " ".join(known["legacy_installer"]),
        " ".join(known["modern_installer"])))
    print("pip constraints: {}".format(
        " ".join(constraints) if constraints
        else "none (stack.pip_constraints)"))
    print("engine: {} ({})".format(engine, compose_cmd))

    if args.dry_run:
        print("\nwould run:\n  {}".format(" ".join(command)))
        return 0

    # Bytes for the same reason as seed_db: a build can emit non-UTF-8 on its
    # way past, and losing the whole log to a decode error is a bad trade.
    result = subprocess.run(command, input=BUILD_SCRIPT.encode("utf-8"),
                            cwd=str(args.root), capture_output=True)
    stdout = result.stdout.decode("utf-8", errors="replace")
    sys.stdout.write(stdout)
    sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
    conflicts = "DEPENDENCY-CONFLICTS-FOLLOW" in stdout
    if result.returncode != 0:
        package, entry = constraint_blamed(stdout, constraints)
        if package:
            sys.stderr.write(
                "\npip found no version of {pkg} it was allowed to install, and "
                "the reason is a constraint this environment applies:\n"
                "\n"
                "    pip_constraints = [... \"{entry}\" ...]     in config.toml, [stack]\n"
                "\n"
                "An app asked for a {pkg} that {entry} forbids, so the candidate "
                "list was empty. pip reports that as \"no matching distribution\", "
                "which looks like the package is missing from PyPI; it is not.\n"
                "\n"
                "Either loosen or remove that entry -- an empty pip_constraints "
                "list is allowed and means no constraints -- or change what the "
                "app asks for. docs/troubleshooting.md explains why the seeded "
                "v12 entry exists and when a fork does not need it.\n"
                .format(pkg=package, entry=entry)
            )
        sys.stderr.write(
            "\nbench/env was not built. The venv is bind-mounted, so whatever "
            "was installed before the failure is still on disk and a re-run "
            "resumes rather than starting over.\n"
        )
        return 1

    # os.path.lexists, not Path.exists(). bench/env/bin/python is a symlink to
    # the *container's* interpreter (decision #22), so following it from the
    # host always fails -- exists() returns False for a venv that is perfectly
    # fine. Anything else inspecting this tree, erp-sandbox doctor included, has to take
    # the same care.
    env_python = Path(args.root) / "bench" / "env" / "bin" / "python"
    if not os.path.lexists(env_python):
        sys.stderr.write("the build reported success but {} does not exist\n".format(env_python))
        return 1
    print("\nbench/env built. The host can read it, but must not activate it: "
          "its interpreter is the container's Python (decision #22).")
    if conflicts:
        sys.stderr.write(
            "\nWARNING: the venv has dependency conflicts, listed above. An app "
            "has pulled a package to a version the framework pins differently. "
            "The build did not stop, because whether that matters depends on the "
            "app -- but it is not something to discover later from a background "
            "job that silently stopped working.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
