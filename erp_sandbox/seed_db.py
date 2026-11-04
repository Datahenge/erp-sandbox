#!/usr/bin/env python3
"""
seed_db.py — create the site and put something in it.

Step 7 of the build (functional.md section 4), and the only destructive one.
technical.md section 6 defines the three modes; `[database].seed` picks one.

  new_site       bench new-site, then bench install-app per app. Empty but
                 working, and the only mode that needs no client data.
  sql_dump       a raw mysqldump, streamed into a fresh database.
  frappe_backup  a frappe-native tarball, through bench restore.

**This is the step that can destroy a developer's work**, so it refuses to touch
an existing site without --force. Everything else in this tool is idempotent; a
re-run here means dropping a database.

The work happens inside the container: `bench` is the thing that knows how to
build a site.
"""

import argparse
import subprocess
import sys
from pathlib import Path


from erp_sandbox.preflight import (  # noqa: E402
    SECRETS_BUILD,
    SECRETS_SEED,
    ConfigError,
    detect_engine,
    load_config,
    resolve_all_secrets,
)

SERVICE_MARIADB = "mariadb"


# ---------------------------------------------------------------------------
# The scripts that run inside the container
#
# Ordinary values arrive as positional arguments, so none is interpolated into
# a shell string. Secrets do not: an argument is on the command line, and every
# command line on the host -- the compose client's included -- is in `ps` for
# any user to read, which on a shared VPS (#48) is other developers. They
# arrive instead in secret_preamble(), on the script's stdin, and are passed on
# by environment: MYSQL_PWD for the mariadb client, and bench_with_secrets for
# frappe, which takes a password only as an option or from site_config.json.
# ---------------------------------------------------------------------------

# The environment variables the scripts read their secrets from.
SECRET_ENV = {
    "database.root_password": "DB_ROOT_PASSWORD",
    "site.admin_password": "ADMIN_PASSWORD",
    "database.encryption_key": "ENCRYPTION_KEY",
}


def secret_preamble(secrets):
    """`export NAME=value` lines for the top of a script fed to bash on stdin.

    shlex.quote makes each value one literal word whatever it contains. stdin,
    unlike argv, is visible to no other process.
    """
    import shlex

    lines = []
    for field, name in SECRET_ENV.items():
        value = secrets.get(field)
        if value is not None:
            lines.append("export {}={}".format(name, shlex.quote(value)))
    return "\n".join(lines) + "\n"


COMMON = r"""
set -euo pipefail

cd "${BENCH_DIR:?BENCH_DIR is not set; this is not the bench image}"

site="$1"; shift
# [site].db_name, or empty to let frappe choose. bench new-site takes it in
# both v12 and v16, and a restore keeps whatever the empty site was created with.
db_name="$1"; shift
db_name_args=()
if [ -n "$db_name" ]; then
    db_name_args=(--db-name "$db_name")
fi

site_exists() { [ -d "sites/$site" ]; }

# `bench <args> <option> <secret>...` without the secret in any command line.
#
#   bench_with_secrets --mariadb-root-password=DB_ROOT_PASSWORD -- --site s restore f
#
# bench runs a frappe command as `cd sites; env/bin/python -m
# frappe.utils.bench_helper frappe <args>` (bench/cli.py, frappe_cmd), in v12
# and v16 alike. This runs the same module, with each option's value appended
# to sys.argv from the named environment variable inside the process, where
# /proc/<pid>/cmdline never sees it.
bench_with_secrets() {
    ( cd sites && ../env/bin/python - "$@" <<'PYEND'
import os, runpy, sys
args = sys.argv[1:]
split = args.index("--")
rest = args[split + 1:]
for spec in args[:split]:
    option, _, variable = spec.partition("=")
    rest += [option, os.environ[variable]]
sys.argv = ["frappe.utils.bench_helper", "frappe"] + rest
runpy.run_module("frappe.utils.bench_helper", run_name="__main__", alter_sys=True)
PYEND
    )
}

# The mariadb client as root. MYSQL_PWD rather than -p, for the same reason.
root_sql() {
    MYSQL_PWD="$DB_ROOT_PASSWORD" mariadb -h "${MARIADB_HOST:-mariadb}" \
        -P "${INTERNAL_MARIADB_PORT:?}" -uroot "$@"
}

# Leave exactly one grant for the site's database user: the one at '%'.
#
# frappe creates the user again on every new-site and restore, and the row it
# adds is keyed to whatever address the client is connecting from. Container
# addresses are recycled, and MariaDB prefers the most specific host match -- so
# after a few re-seeds a bench that lands on an address some earlier run used
# authenticates against that run's row, with that run's password, and gets
# "Access denied" for a database it owns. `down` then `up` is enough to trigger
# it, which is a daily operation.
#
# Verified in an environment that had accumulated eighteen rows for one user.
prune_stale_grants() {
    db_name="$(python3 -c "import json;print(json.load(open('sites/$site/site_config.json'))['db_name'])" 2>/dev/null || true)"
    [ -n "$db_name" ] || return 0
    root_sql -e "
              delete from mysql.user where user='$db_name' and host<>'%';
              flush privileges;" 2>/dev/null || true
    remaining="$(root_sql -N -B -e "
                     select count(*) from mysql.user where user='$db_name';" 2>/dev/null || echo "?")"
    echo "   grants for $db_name: $remaining (expect 1, at '%')"
}

# site_config.json is JSON in a bind mount, so it is edited as JSON.
#
# `bench set-config` is not usable for this. In bench 5.24.1 it rejects --site
# both before and after the subcommand, and -- worse -- it passes the VALUE
# through click, so any value beginning with "-" is read as an option. A Fernet
# encryption key is base64url and can begin with anything at all, which makes
# that a bug that shows up on one key in sixty-four and never on the others.
set_site_config() {
    python3 - "$site" "$1" "$2" <<'PYEND'
import json, sys
site, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
path = "sites/%s/site_config.json" % site
with open(path) as handle:
    config = json.load(handle)
config[key] = int(value) if value.lstrip("-").isdigit() else value
with open(path, "w") as handle:
    json.dump(config, handle, indent=1, sort_keys=True)
print("   site_config.json: %s set" % key)
PYEND
}

# The same, with the value read from an environment variable rather than argv.
set_site_config_from_env() {
    python3 - "$site" "$1" "$2" <<'PYEND'
import json, os, sys
site, key, variable = sys.argv[1], sys.argv[2], sys.argv[3]
path = "sites/%s/site_config.json" % site
with open(path) as handle:
    config = json.load(handle)
config[key] = os.environ[variable]
with open(path, "w") as handle:
    json.dump(config, handle, indent=1, sort_keys=True)
print("   site_config.json: %s set" % key)
PYEND
}

drop_site() {
    bench_with_secrets --root-password=DB_ROOT_PASSWORD -- \
        drop-site "$site" --no-backup --force
}

create_site() {
    bench_with_secrets --mariadb-root-password=DB_ROOT_PASSWORD \
                       --admin-password=ADMIN_PASSWORD -- \
        new-site "$site" "${db_name_args[@]}" --no-mariadb-socket
}
"""

NEW_SITE = COMMON + r"""
force="$1"; shift
# The remaining arguments are the apps to install, in config.toml order.

if site_exists; then
    if [ "$force" != "1" ]; then
        echo "FATAL: sites/$site already exists." >&2
        echo "  Seeding drops and recreates its database. Re-run with --force if" >&2
        echo "  that is what you want; everything in it will be gone." >&2
        exit 1
    fi
    echo "== dropping the existing site (--force)"
    drop_site
fi

echo "== creating $site"
create_site

for app in "$@"; do
    echo "== installing $app on $site"
    bench --site "$site" install-app "$app"
done

echo "== pruning host-specific database grants"
prune_stale_grants

echo "== site config"
set_site_config developer_mode "${DEVELOPER_MODE:-1}"
"""

RESTORE = COMMON + r"""
force="$1"; shift
mode="$1"; shift
dump="$1"; shift
# Optional tarballs of public and private files, or empty.
public_files="$1"; shift
private_files="$1"; shift

[ -f "$dump" ] || { echo "FATAL: $dump is not readable inside the container." >&2; exit 1; }
files_args=()
if [ -n "$public_files" ]; then
    files_args+=(--with-public-files "$public_files")
fi
if [ -n "$private_files" ]; then
    files_args+=(--with-private-files "$private_files")
fi

if site_exists && [ "$force" != "1" ]; then
    echo "FATAL: sites/$site already exists. Re-run with --force to replace it." >&2
    exit 1
fi

# Drop and recreate rather than restoring over the top.
#
# frappe reinstalls the database on every restore -- `_new_site` is called with
# force=True whatever the flags say -- and the DROP USER it issues names the
# user at the address the *client* is connecting from, while the grant was made
# at '%'. A container's address changes every start, so prune_stale_grants
# below cleans up after it. drop-site first removes the database, the user and
# the site directory cleanly, and is the same path new_site mode uses.
if site_exists; then
    echo "== dropping the existing site before restoring (--force)"
    drop_site
fi

echo "== creating an empty $site to restore into"
create_site

# Risk 6. The key has to be in place before anything reads an encrypted field.
# Setting it afterwards is not the same: whatever was read in between was read
# wrongly, and nothing says so.
echo "== installing the encryption key that matches this dump"
set_site_config_from_env encryption_key ENCRYPTION_KEY

# --force skips frappe's is_downgrade() check, and skipping it is the point.
# That check parses the dump by scanning for the tabInstalled Application
# INSERT and eval()ing whatever follows VALUES *on the same line*. MariaDB's
# dump tools now put the rows on the lines after it, so it eval()s an empty
# string and the restore dies before touching anything. erp-sandbox compares the
# two frappe versions itself, host-side, and says so (decision #39).
#
# It skips nothing else: in this frappe, restore's --force has exactly that
# one effect.
echo "== restoring $mode from $dump"
bench_with_secrets --mariadb-root-password=DB_ROOT_PASSWORD -- \
    --site "$site" restore "$dump" "${files_args[@]}" --force

# The restore can rewrite site_config.json from the dump's own, which may carry
# a different key or none at all. Put ours back afterwards.
set_site_config_from_env encryption_key ENCRYPTION_KEY

echo "== pruning host-specific database grants"
prune_stale_grants

echo "== migrate"
bench --site "$site" migrate
"""


def compose_prefix(engine, compose_cmd, root):
    overlay = Path(root) / "compose.{}.yml".format(engine)
    if not overlay.is_file():
        raise SystemExit("{} is missing. Run `erp-sandbox render` first.".format(overlay))
    return compose_cmd.split() + [
        "-f", str(Path(root) / "docker-compose.yml"),
        "-f", str(overlay),
    ]


def require_healthy_mariadb(engine, project):
    """
    Seeding needs the database, but `compose run` without --no-deps *recreates*
    the dependency containers rather than reusing running ones -- which under
    podman-compose also trips over nginx still holding a reference to the old
    mariadb. Checking is both gentler and more honest than recreating.
    """
    container = "{}-mariadb".format(project)
    try:
        result = subprocess.run(
            [engine, "inspect", "--format", "{{.State.Health.Status}}", container],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(
            "could not ask {} about {}: {}".format(engine, container, exc)) from exc

    status = result.stdout.strip()
    if result.returncode != 0 or not status:
        raise SystemExit(
            "{} is not running. Seeding writes to the database, so it has to be "
            "up first:\n  <engine> compose -f docker-compose.yml -f compose.{}.yml "
            "up -d mariadb".format(container, engine)
        )
    if status != "healthy":
        raise SystemExit(
            "{} is {!r}, not healthy. The healthcheck is an authenticated-free "
            "TCP ping (decision #26); until it passes the server is still "
            "initialising and any bench command would fail confusingly."
            .format(container, status)
        )


def explain_failure(output, cfg, root):
    """
    Turn the failure modes that are really configuration problems into a
    sentence about configuration.
    """
    import re

    # A Frappe app importing another Frappe app that nobody listed. The error
    # names a Python module, which looks like a packaging problem and is
    # actually a missing [[apps]] entry -- an app is only importable if it was
    # cloned and pip-installed, and only that if config.toml names it.
    match = re.search(r"Module import failed for .*?No module named '([A-Za-z0-9_]+)'", output)
    if match:
        missing = match.group(1)
        known = {a["name"] for a in cfg["apps"]}
        sys.stderr.write(
            "\n-> An app being installed imports {!r}, which is not in this "
            "bench.\n".format(missing)
        )
        if missing not in known:
            sites = find_import_sites(missing, root)
            for where, line in sites[:3]:
                sys.stderr.write("   {}\n     {}\n".format(where, line))
            looks_frappe = any(".doctype." in line or ".doctype " in line
                               for _w, line in sites)
            sys.stderr.write(
                "   {!r} is not among the [[apps]] in config.toml ({}).\n"
                .format(missing, ", ".join(sorted(known)))
            )
            if looks_frappe:
                sys.stderr.write(
                    "   That import path has a '.doctype.' segment, which is "
                    "Frappe app layout rather than an ordinary Python package -- "
                    "so this is very likely another Frappe app.\n"
                )
            sys.stderr.write(
                "   Add it to [[apps]] *before* the app that imports it -- clone "
                "order is install order -- then re-run erp-sandbox fetch "
                "and erp-sandbox build-bench. If it is an ordinary Python "
                "package instead, it belongs in the importing app's own "
                "dependencies.\n"
            )
        else:
            sys.stderr.write(
                "   It IS listed in config.toml, so it was cloned but not "
                "installed into bench/env. Re-run erp-sandbox build-bench.\n"
            )
        return

    # frappe v12's sync_customizations reads *every* file in an app's custom/
    # folder and json.loads it -- no extension filter (modules/utils.py). A
    # README beside the customization files is enough to stop an install, and
    # the CLI catches the exception and prints only "Expecting value: line 1
    # column 1 (char 0)", which names neither the file nor the folder.
    if "Expecting value: line 1 column 1" in output:
        offenders = find_unparseable_customizations(root)
        sys.stderr.write(
            "\n-> A file in an app's custom/ folder is not JSON.\n"
            "   frappe v12 reads every file in that folder and parses it as "
            "JSON, with no check on the extension, so anything else there "
            "stops the install -- and stops `bench migrate` later for the same "
            "reason.\n"
        )
        if offenders:
            sys.stderr.write("   Not parseable as JSON:\n")
            for path in offenders:
                sys.stderr.write("     {}\n".format(path))
            sys.stderr.write("   Move them out of custom/; that folder is only "
                             "for customization JSON.\n")
        else:
            sys.stderr.write("   Every file under bench/apps/*/*/custom/ parses "
                             "now, so the app may have changed since the run.\n")
        return

    if "Access denied" in output or "OperationalError" in output:
        sys.stderr.write(
            "\n-> The database refused the connection. Check that "
            "database.root_password in config.toml matches the password the "
            "mariadb container was created with -- changing it in config.toml "
            "does not change it in an existing data directory.\n"
        )


def find_import_sites(module_name, root):
    """
    Where an app imports a module, as (location, source line) pairs.

    The error names a module; the useful question is which file asked for it.
    """
    import re

    pattern = re.compile(r"^\s*(?:from\s+{0}[.\s]|import\s+{0}[.\s,]?)"
                         .format(re.escape(module_name)))
    found = []
    apps_dir = Path(root) / "bench" / "apps"
    for path in sorted(apps_dir.glob("*/**/*.py")):
        if "node_modules" in path.parts or ".git" in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if pattern.match(line):
                found.append(("{}:{}".format(path.relative_to(root), number),
                              line.strip()))
                if len(found) >= 5:
                    return found
    return found


def find_unparseable_customizations(root):
    """Every file under bench/apps/*/*/custom/ that is not valid JSON."""
    import json

    bad = []
    apps_dir = Path(root) / "bench" / "apps"
    for custom in sorted(apps_dir.glob("*/*/*/custom")):
        if not custom.is_dir():
            continue
        for path in sorted(custom.iterdir()):
            if not path.is_file():
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, UnicodeDecodeError):
                bad.append(path.relative_to(root))
    return bad


def check_encryption_key(cfg, secrets, seed=None):
    """
    Risk 6, checked before anything is dropped.

    validate() already refuses a dump mode with an empty encryption_key, so
    this catches the subtler case: a key that resolves to an empty string
    through an `env:` reference that is set but blank -- and `erp-sandbox
    restore`, which restores a dump whatever [database].seed says, so validate()
    never asked for a key at all.
    """
    seed = seed or cfg["database"]["seed"]
    if seed == "new_site":
        return None
    key = secrets.get("database.encryption_key", "")
    if not key.strip():
        raise SystemExit(
            "database.encryption_key is empty or unset, and this restores a "
            "dump ({}).\n"
            "Without the key that matches this dump, every stored password in "
            "the restored site is unreadable. Nothing announces it: the site "
            "loads, logins work, and then an integration cannot authenticate "
            "weeks later. Set database.encryption_key in config.toml to the "
            "dump's key. Refusing to restore.".format(seed)
        )
    return key


# ---------------------------------------------------------------------------
# The version comparison frappe cannot do for itself (decision #39)
# ---------------------------------------------------------------------------

INSTALLED_APPS_MARKER = b"INSERT INTO `tabInstalled Application` VALUES"


def dump_frappe_version(dump_path):
    """
    The frappe version a dump records, or None if it does not say.

    Read here rather than left to frappe, whose is_downgrade() scans for the
    same statement and eval()s whatever follows VALUES on that one line. Every
    MariaDB dump tool now writes the rows on the *following* lines, so frappe
    evaluates an empty string and raises SyntaxError before restoring anything.
    Reading the whole statement instead costs about a second on a gigabyte.

    The last three columns of the row are (app name, version, branch), which is
    the shape frappe itself relies on.
    """
    import gzip
    import re

    frappe_row = re.compile(r"'frappe'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")
    opener = gzip.open if str(dump_path).endswith(".gz") else open
    try:
        with opener(str(dump_path), "rb") as handle:
            for line in handle:
                if INSTALLED_APPS_MARKER not in line:
                    continue
                statement = [line]
                # An old dump puts the whole statement on this line; a new one
                # ends it several lines later.
                while not line.rstrip().endswith(b";"):
                    line = handle.readline()
                    if not line:
                        break
                    statement.append(line)
                text = b" ".join(part.strip() for part in statement)
                match = frappe_row.search(text.decode("utf-8", errors="replace"))
                if not match:
                    return None
                # frappe records "12.27.0" or "v12.20.1 (abc) (version-12)".
                # Everything after the first space is provenance, not version.
                return match.group(1).split(" ")[0]
    except (OSError, EOFError):
        # An unreadable or truncated dump is the restore's problem to report,
        # with a better message than this function could give.
        return None
    return None


def bench_frappe_version(root):
    """The frappe version in the bench, read off the bind mount."""
    import re

    init = Path(root) / "bench" / "apps" / "frappe" / "frappe" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
    return match.group(1) if match else None


def version_tuple(text):
    """
    A comparable form of a version string, or None if it is not comparable.

    Numeric leading components only. A fork's version can carry anything after
    them -- "12.27.0", "v12.27.0", "12.27.0-sf.3" -- and only the numbers order
    reliably.
    """
    if not text:
        return None
    head = text.strip().lstrip("vV").replace("-", " ").replace("+", " ").split()[0]
    parts = []
    for piece in head.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) or None


def report_versions(dump_path, root):
    """
    Say what is being restored onto what, and warn if it is a downgrade.

    frappe's own check aborts; this one reports. The abort is unreachable
    anyway -- see the --force comment in RESTORE -- and a restore stopped
    because a fork's version string does not sort against upstream's is worth
    less than a developer who can see both numbers.
    """
    in_dump = dump_frappe_version(dump_path)
    in_bench = bench_frappe_version(root)

    if in_dump is None:
        print("  frappe version: the dump does not record one")
    else:
        print("  frappe: dump {} -> bench {}".format(in_dump, in_bench or "unknown"))

    left, right = version_tuple(in_dump), version_tuple(in_bench)
    if left and right and left > right:
        sys.stderr.write(
            "\nwarning: the dump's frappe ({}) is NEWER than this bench's ({}).\n"
            "         Restoring it does not downgrade the schema, and `bench "
            "migrate`\n         has no patches to undo what the newer version "
            "already did.\n         Check the branch in config.toml before "
            "trusting this site.\n".format(in_dump, in_bench)
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create the site named in config.toml and seed its database.",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", required=True,
                        help="the project root")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing site. Destroys its database.")
    parser.add_argument("--dry-run", action="store_true")
    # `erp-sandbox restore`: a dump given on the command line, restored whatever
    # [database].seed says, through the same path as a frappe_backup seed.
    parser.add_argument("--dump", help="restore this dump instead of seeding per config.toml")
    parser.add_argument("--public-files", help="with --dump: a public files tarball")
    parser.add_argument("--private-files", help="with --dump: a private files tarball")
    args = parser.parse_args(argv)
    if (args.public_files or args.private_files) and not args.dump:
        parser.error("--public-files and --private-files need --dump")

    try:
        cfg = load_config(args.config)
        secrets, warnings, _sources = resolve_all_secrets(
            cfg, args.config, groups=(SECRETS_BUILD, SECRETS_SEED),
        )
    except ConfigError as exc:
        sys.stderr.write("config.toml is not usable:\n")
        for problem in exc.problems:
            sys.stderr.write("  - {}\n".format(problem))
        return 2

    for warning in warnings:
        sys.stderr.write("warning: {}\n".format(warning))

    site = cfg["project"]["site"]
    seed = "frappe_backup" if args.dump else cfg["database"]["seed"]
    # frappe is excluded: `bench new-site` installs the framework itself, and
    # `install-app frappe` on top of that is an error. apps.txt still lists it,
    # because that file describes what is on the site rather than what has to be
    # installed onto it.
    apps = [a["name"] for a in cfg["apps"] if a["install"] and a["name"] != "frappe"]
    key = check_encryption_key(cfg, secrets, seed)

    engine, compose_cmd = detect_engine()
    if engine is None or compose_cmd is None:
        sys.stderr.write("no usable container engine; run erp-sandbox preflight\n")
        return 2

    if not args.dry_run:
        require_healthy_mariadb(engine, cfg["project"]["name"])

    print("seeding {} using mode {!r}".format(site, seed))
    print("  database: {}".format(cfg["site"].get("db_name") or "(frappe chooses)"))
    if seed == "new_site":
        print("  apps to install: {}".format(" -> ".join(apps) or "(none)"))
        script = NEW_SITE
        script_args = ["1" if args.force else "0"] + apps
    else:
        dump_host = Path(args.dump or cfg["database"]["dump_path"]).expanduser()
        if not dump_host.is_file():
            sys.stderr.write("{} {} does not exist\n".format(
                "the dump" if args.dump else "database.dump_path", dump_host))
            return 2
        files = {}
        for label, value in (("public", args.public_files), ("private", args.private_files)):
            if value:
                path = Path(value).expanduser()
                if not path.is_file():
                    sys.stderr.write("the {} files tarball {} does not exist\n"
                                     .format(label, path))
                    return 2
                # A name of its own under /tmp, so the two cannot collide with
                # each other or with the dump.
                files[label] = (path, "/tmp/{}-files-{}".format(label, path.name))
                print("  {} files: {} ({:.1f} MB)".format(label, path,
                                                         path.stat().st_size / 1e6))
        print("  dump: {} ({:.1f} MB)".format(dump_host, dump_host.stat().st_size / 1e6))
        print("  encryption_key: present ({} characters)".format(len(key)))
        # Also on --dry-run: it only reads files, and checking what a dump
        # carries before committing to a restore is most of why one is run.
        report_versions(dump_host, args.root)
        script = RESTORE
        # Mounted read-only -- nothing should be able to write to a developer's
        # only copy of a client dump -- but under its own filename, not a fixed
        # one. frappe v12 decides whether to decompress by looking at the
        # suffix (`sql_file_path.endswith('sql.gz')` in commands/site.py), so a
        # dump mounted as "/seed/dump" is read as though it were plain SQL and
        # fails on the gzip magic bytes.
        # Under /tmp, not a directory of its own. frappe decompresses a .sql.gz
        # by writing the .sql *beside it*, and a directory podman creates for a
        # single-file mount belongs to root -- so the container user, which is
        # the host user (decision #25), cannot write there. /tmp is 1777.
        #
        # The decompressed copy lives in the container's writable layer and
        # goes away with it, but it does mean a large dump needs its own size
        # again in free space while restoring.
        container_dump = "/tmp/{}".format(dump_host.name)
        script_args = ["1" if args.force else "0", seed, container_dump,
                       files["public"][1] if "public" in files else "",
                       files["private"][1] if "private" in files else ""]

    command = compose_prefix(engine, compose_cmd, args.root) + [
        "run", "--rm", "--no-deps", "-T",
        "-e", "DEVELOPER_MODE={}".format(cfg["frappe"]["developer_mode"]),
    ]
    if seed != "new_site":
        command += ["-v", "{}:{}:ro".format(dump_host.resolve(), container_dump)]
        for host_path, container_path in files.values():
            command += ["-v", "{}:{}:ro".format(host_path.resolve(), container_path)]
    # No secret in this list: it is the compose client's command line, which
    # `ps` shows to every user for as long as the seed runs.
    command += ["bench", "bash", "-s", "--",
                site,
                cfg["site"].get("db_name", "")] + script_args

    if args.dry_run:
        print("\nwould run:\n  {}".format(" ".join(command)))
        return 0

    # Bytes, not text. A restore streams a gzipped dump and some of it reaches
    # stderr; decoding that as UTF-8 raises and loses the entire log, including
    # whatever the real failure was.
    script_input = secret_preamble(secrets) + script
    result = subprocess.run(command, input=script_input.encode("utf-8"),
                            cwd=str(args.root), capture_output=True)
    # bench redraws progress bars with \r thousands of times; keeping them
    # turns a useful log into 70KB of one line.
    output = result.stdout.decode("utf-8", errors="replace").replace("\r", "\n")
    sys.stdout.write("\n".join(
        line for line in output.splitlines()
        if line.strip() and "Updated DocTypes for" not in line
    ) + "\n")
    sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))

    if result.returncode != 0:
        explain_failure(output, cfg, args.root)
        sys.stderr.write("\nseeding failed. The site may be half-built; re-run "
                         "with --force to start from a clean database.\n")
        return 1

    print("\n{} is seeded.".format(site))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
