"""
The two roots, which used to be one.

Before decision #27 this tool *was* the environment: you cloned the repository
and it became your client's bench. Installed, there are two separate places and
almost every path bug from here on will be confusing one for the other.

  PACKAGE ROOT   where erp-sandbox itself is installed. Templates, the compose
                 file, the Dockerfile, the sample config. Read-only, shared by
                 every client environment on the machine, and replaced wholesale
                 when the tool is upgraded.

  PROJECT ROOT   one client environment: its config.toml, its bench/, its
                 data/, its .erp-sandbox/. Owned by the developer, never touched
                 by an upgrade, and found by walking up from the working
                 directory the way `bench` finds its bench.

`.erp-sandbox/` is the marker. It is a directory rather than a file so that
machine-local state and secrets have somewhere to live that is obviously not
part of the configuration (decision #27).
"""

import os
import subprocess
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent

# Names inside a project root.
MARKER = ".erp-sandbox"
CONFIG_NAME = "config.toml"
SECRETS_NAME = "secrets.env"

# Bind-mount sources the engine would otherwise create for us (decision #38).
#
# Every other host path the compose file mounts is written host-side before a
# container ever sees it -- the conf/ files by `erp-sandbox render`, bench/ by
# `erp-sandbox fetch`, which runs two steps before infra. data/mariadb held no
# generated file and so had no owner, and the engines disagree about what
# happens then: rootless podman creates a missing bind-mount source as the
# invoking user, while a rootful Docker daemon creates it as root. MariaDB is
# pinned to the host uid (decision #25), so under Docker it could not write its
# own datadir and exited, and doctor reported a directory nobody had chowned.
#
# Creating it here makes the answer the same on both engines.
DATA_DIRS = ("data/mariadb",)

# Where `erp-sandbox init <name>` puts a new environment (decision #51): one
# directory per user that everything beneath belongs to, so a home directory
# has one name to collide with rather than one per environment. Not
# "erp-sandbox" -- that is the directory `git clone` of the source creates,
# and environments never live inside the source (#32).
ENVS_DIRNAME = "erp-sandboxes"


def default_envs_dir():
    return Path.home() / ENVS_DIRNAME


def init_target(name):
    """
    Where `erp-sandbox init <name>` creates the environment.

    A bare name goes under ~/erp-sandboxes/. Anything that already reads as a
    path -- it has a slash in it, starts with ~, or is . or .. -- is taken as
    one, so the old spelling `erp-sandbox init ./acme-dev` still means here.
    """
    if "/" in name or name.startswith("~") or name in (".", ".."):
        return Path(name).expanduser()
    return default_envs_dir() / name

# Where the tool's own files live inside the package.
TEMPLATE_DIR = PACKAGE_ROOT / "templates"
ASSET_DIR = PACKAGE_ROOT / "assets"
FIXTURE_DIR = PACKAGE_ROOT / "fixtures"
SAMPLE_CONFIG = ASSET_DIR / "config.sample.toml"
SAMPLE_SECRETS = ASSET_DIR / "secrets.sample.env"
SAMPLE_BENCH_ENV = ASSET_DIR / "bench.sample.env"


# The marker before the product was renamed (decision #52). It is not accepted
# -- the rename is a clean break -- but it is recognised, so that the refusal
# can say what to rename rather than only that nothing was found.
LEGACY_MARKER = ".devstack"


def legacy_hint(start):
    """A migration instruction if `start` or a parent is a pre-#52 environment."""
    here = Path(start).resolve()
    for candidate in [here] + list(here.parents):
        if (candidate / LEGACY_MARKER).is_dir():
            return (
                "\n  {root} is an environment from before the rename to erp-sandbox\n"
                "  (decision #52). To migrate it:\n"
                "      mv {root}/{old} {root}/{new}\n"
                "  then change [devstack] to [erp-sandbox] in its config.toml and run\n"
                "  erp-sandbox render.".format(root=candidate, old=LEGACY_MARKER, new=MARKER)
            )
    return ""


class ProjectNotFound(Exception):
    """No client environment at or above the working directory."""

    def __init__(self, start):
        self.start = Path(start)
        super().__init__(str(self.start))

    def __str__(self):
        return (
            "no erp-sandbox environment here.\n"
            "  Looked for a {}/ directory in {} and every directory above it.\n"
            "  Either cd into an environment, or create one with:\n"
            "      erp-sandbox init <name>{}"
            .format(MARKER, self.start, legacy_hint(self.start))
        )


def find_project_root(start=None, required=True):
    """
    Walk up from `start` looking for a directory containing `.erp-sandbox/`.

    Returns the Path, or None when required is False and there is none.
    """
    here = Path(start or os.getcwd()).resolve()
    for candidate in [here] + list(here.parents):
        if (candidate / MARKER).is_dir():
            return candidate
    if required:
        raise ProjectNotFound(here)
    return None


def is_project_root(path):
    return (Path(path) / MARKER).is_dir()


def config_path(project_root):
    return Path(project_root) / CONFIG_NAME


def state_dir(project_root):
    """`.erp-sandbox/` — machine-local, never committed, never part of the config."""
    return Path(project_root) / MARKER


def default_secrets_path(project_root):
    return state_dir(project_root) / SECRETS_NAME


_git_local_env_vars = None


def git_local_env_vars():
    """The variables that point git at a particular repository.

    A git hook exports them to everything it runs -- `git commit -a` sets
    GIT_INDEX_FILE to its temporary index -- and a git started underneath would
    then read and write the outer repository instead of its own. Git names its
    own list, so it cannot drift from the installed version. Empty when git is
    not installed, since then nothing will read them.
    """
    global _git_local_env_vars
    if _git_local_env_vars is None:
        try:
            _git_local_env_vars = subprocess.run(
                ["git", "rev-parse", "--local-env-vars"],
                capture_output=True, text=True).stdout.split()
        except OSError:
            _git_local_env_vars = []
    return _git_local_env_vars


def git_env():
    """os.environ without git's repository variables: the env for every git the tool starts."""
    env = dict(os.environ)
    for name in git_local_env_vars():
        env.pop(name, None)
    return env


def data_dir(root, relative="data/mariadb"):
    """One runtime data directory inside a project root."""
    return Path(root).joinpath(*relative.split("/"))


def ensure_data_dirs(root):
    """
    Create the bind-mount sources the engine must not invent.

    Idempotent, and never touches a directory that already exists -- an
    existing datadir belongs to MariaDB, and its ownership is the developer's
    to fix if it is wrong (doctor says how, per engine).
    """
    created = []
    for relative in DATA_DIRS:
        target = data_dir(root, relative)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(relative)
    return created
