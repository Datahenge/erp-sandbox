#!/usr/bin/env python3
"""
fetch_apps.py — clone each app named in config.toml into bench/apps/, host-side.

Step 3 of the build (functional.md section 4). Nothing else in the tool decides
what to clone or from where; this reads [[apps]] and does exactly that, in
order, because clone order is install order.

Two things make this file more than a loop around `git clone`.

**Credentials never touch the disk or a container.** technical.md section 5.
For an app with `requires_credentials = true`, the token reaches `git` through
`GIT_ASKPASS` for the duration of that one command, in the environment of that
one child process. No `.netrc`, no credential store, no token in a URL, and
therefore none in `.git/config` afterwards. Nothing is passed on a command line,
where `ps` would show it to every user on the machine. What is written to disk
is a five-line helper script naming two environment variables -- it contains no
secret and is deleted when the run ends.

**A developer's uncommitted work outranks the config.** This runs on every
build, and `bench/apps/` is where the developer edits code all day. So a dirty
working tree is never checked out over: if the tree is dirty and already at the
right ref the app is left alone, and if the tree is dirty and the ref has
changed the app is refused with an explanation. Losing an afternoon's edits to
an idempotent re-run would be a far worse bug than a build that stops.

**An app may ask to be cloned without its file history.** `blobless = true` in
`[[apps]]` adds `--filter=blob:none`: every commit and tree arrives, so the log
and the branches are whole, but the contents of files at old commits are fetched
only when something asks for one. On frappe and erpnext that is the difference
between a gigabyte and tens of megabytes. It is off unless asked for, and
preflight refuses it for an app that needs a credential -- those later fetches
happen outside the one command the token exists for. technical.md section 5.

Standard library only (decision #18):

    erp-sandbox fetch [--only NAME] [--dry-run]
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


from erp_sandbox import project  # noqa: E402
from erp_sandbox.preflight import (  # noqa: E402
    SECRETS_FETCH,
    ConfigError,
    load_config,
    repo_remote,
    resolve_all_secrets,
)

# git must never stop and wait for a human. erp-sandbox build runs unattended
# (acceptance item 2), and a build that hangs on a password prompt at minute
# forty looks like a hang, not like a missing credential.
NO_PROMPT = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}

# BatchMode makes ssh fail with a message instead of prompting for a
# passphrase. The developer's own agent is expected to hold the key; see
# hint_for() for what they are told when it does not.
SSH_COMMAND = "ssh -o BatchMode=yes"

ASKPASS_SCRIPT = """#!/bin/sh
# Written by erp-sandbox fetch, deleted when the run ends.
# Holds no secret: it names two variables that exist only in the environment
# of the one git process that needs them.
case "$1" in
    Username*) printf '%s' "$ERP_SANDBOX_GIT_USERNAME" ;;
    *)         printf '%s' "$ERP_SANDBOX_GIT_TOKEN" ;;
esac
"""


class FetchError(Exception):
    """One app could not be fetched. Carries a message already fit to print."""


# ---------------------------------------------------------------------------
# Running git without leaking anything
# ---------------------------------------------------------------------------

def redact(text, secrets):
    """
    Remove every known secret from text before it is printed.

    Belt and braces. No token is ever put in a URL by this file, so git should
    have nothing to echo -- but git echoes remote URLs in several error paths,
    and a redaction that never fires costs nothing while the one that is
    missing costs a credential.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


class Git:
    """git, with the credential environment assembled per call."""

    def __init__(self, secrets, askpass_path):
        self.secrets = [s for s in secrets if s]
        self.askpass_path = askpass_path

    def env_for(self, credential):
        env = project.git_env()
        env.update(NO_PROMPT)
        env.setdefault("GIT_SSH_COMMAND", SSH_COMMAND)
        # An inherited askpass would otherwise answer prompts we did not mean
        # it to, using a credential we did not choose.
        env.pop("GIT_ASKPASS", None)
        env.pop("SSH_ASKPASS", None)
        if credential:
            env["GIT_ASKPASS"] = str(self.askpass_path)
            env["ERP_SANDBOX_GIT_USERNAME"] = credential["username"]
            env["ERP_SANDBOX_GIT_TOKEN"] = credential["token"]
        return env

    def run(self, args, cwd=None, credential=None, check=True):
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(cwd) if cwd else None,
            env=self.env_for(credential),
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise FetchError(redact(
                (result.stderr or result.stdout or "git exited {}".format(result.returncode)).strip(),
                self.secrets,
            ))
        return result

    def out(self, args, cwd=None, credential=None):
        return self.run(args, cwd=cwd, credential=credential).stdout.strip()

    def quiet(self, args, cwd=None):
        """True if the command succeeds. For questions, not for actions."""
        return self.run(args, cwd=cwd, check=False).returncode == 0

    def maybe(self, args, cwd=None):
        """
        stdout, or "" if the command fails.

        For questions whose "no" is an exit code rather than an error:
        `symbolic-ref HEAD` exits 1 on a detached HEAD, which is an answer, not
        a failure. Treating it as a failure broke every app pinned to a tag on
        its second run.
        """
        result = self.run(args, cwd=cwd, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# What the config asks for
# ---------------------------------------------------------------------------

def credential_for(app, cfg, secrets):
    """
    The (username, token) an app needs, or None if it needs none.

    preflight has already established that an app with requires_credentials has
    an entry for its host, and that no ssh remote claims to need one.
    """
    if not app.get("requires_credentials"):
        return None
    host, _kind = repo_remote(app["repo"])
    entry = cfg["credentials"][host]
    return {
        "username": entry["username"],
        "token": secrets['credentials."{}".token'.format(host)],
        "host": host,
    }


def hint_for(message, app, credential):
    """A second line for the failures whose cause is not in git's own message."""
    lowered = message.lower()
    _host, kind = repo_remote(app["repo"])

    if kind == "ssh" and ("permission denied" in lowered or "publickey" in lowered):
        return ("ssh could not authenticate. This tool does not manage keys: load "
                "yours first (ssh-add), or point the app at an https URL and set "
                "requires_credentials = true.")
    if "could not read from remote repository" in lowered and kind == "ssh":
        return ("ssh could not reach the repository -- an unloaded key looks like "
                "this. Try `ssh -T {}` to see what the host says."
                .format(app["repo"].split(":", 1)[0]))
    # The likeliest real failure: a private https repo the config never gave a
    # credential to. git asks for a username, prompts are disabled so that an
    # unattended build cannot hang, and the message names neither cause.
    if "could not read username" in lowered or "terminal prompts disabled" in lowered:
        if credential:
            return ("git asked for a username despite a [credentials.{}] entry, "
                    "which means the askpass helper did not answer. That is a bug "
                    "in fetch_apps.py, not in your configuration."
                    .format(credential["host"]))
        return ("That repository is private and this app has "
                "requires_credentials = false, so no token was offered. Add a "
                "[credentials.\"{}\"] entry and set requires_credentials = true, "
                "or use an ssh URL.".format(repo_remote(app["repo"])[0]))
    if "authentication failed" in lowered or "403" in lowered:
        if credential:
            return ("The token for {} was rejected. Check that it has read access "
                    "to this repository and has not expired."
                    .format(credential["host"]))
        return ("This repository needs a credential and the config does not give "
                "it one. Set requires_credentials = true and add a "
                "[credentials.<host>] entry, or use an ssh URL.")
    if "not found" in lowered or "repository not found" in lowered:
        return ("Either the repository does not exist or the credential cannot "
                "see it -- a private repository is 'not found' to a token that "
                "lacks access to it.")
    if ("remote branch" in lowered or "pathspec" in lowered
            or "couldn't find remote ref" in lowered):
        return "No branch or tag named {!r} on that remote.".format(app["ref"])
    return None


# ---------------------------------------------------------------------------
# One app
# ---------------------------------------------------------------------------

def fetch_one(git, app, target, credential, dry_run=False):
    """
    Bring target to the app's ref. Returns (state, detail).

    state is one of: cloned, updated, unchanged, skipped, would-clone,
    would-update.
    """
    if not target.exists():
        return clone(git, app, target, credential, dry_run)
    if not (target / ".git").exists():
        raise FetchError(
            "{} exists but is not a git repository. Move it aside; this tool "
            "will not delete a directory it did not create.".format(target)
        )
    return update(git, app, target, credential, dry_run)


def clone(git, app, target, credential, dry_run):
    # Said out loud in both the dry run and the result: a partial clone behaves
    # differently months later, when `git log -p` pauses to fetch, and the
    # developer should have been told which kind they have.
    kind = " (blobless)" if app.get("blobless") else ""
    if dry_run:
        return "would-clone", "{} at {}{}".format(app["repo"], app["ref"], kind)

    target.parent.mkdir(parents=True, exist_ok=True)
    # Clone to a temporary sibling and rename, so an interrupted clone never
    # leaves a half-populated app/ that the next run would treat as existing.
    staging = target.parent / (".{}.incomplete".format(target.name))
    if staging.exists():
        shutil.rmtree(staging)
    try:
        argv = ["clone", "--branch", app["ref"], "--origin", "origin"]
        if app.get("blobless"):
            argv.append("--filter=blob:none")
        argv += [app["repo"], str(staging)]
        git.run(argv, credential=credential)
        assert_no_token_on_disk(staging, git.secrets)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return "cloned", "{} at {}{}".format(app["ref"], short(git, target), kind)


def update(git, app, target, credential, dry_run):
    actual = git.out(["remote", "get-url", "origin"], cwd=target)
    if actual != app["repo"]:
        raise FetchError(
            "origin is {!r} but config.toml says {!r}. Repointing a remote could "
            "silently swap one fork for another, so this is left for you: "
            "`git -C {} remote set-url origin {}`."
            .format(actual, app["repo"], target, app["repo"])
        )

    if dry_run:
        return "would-update", "fetch {} and check out {}".format(app["repo"], app["ref"])

    # No filter argument here on purpose. `clone --filter` records the filter
    # in remote.origin.partialclonefilter, and every later fetch applies it, so
    # a blobless clone stays blobless without being told again -- and a full
    # clone is not converted into a partial one behind the developer's back by
    # a flag they flipped after the fact. doctor reports that mismatch.
    git.run(["fetch", "--tags", "--prune", "origin", app["ref"]],
            cwd=target, credential=credential)
    wanted = git.out(["rev-parse", "FETCH_HEAD"], cwd=target)
    head = git.out(["rev-parse", "HEAD"], cwd=target)

    # origin/<ref> exists only for a branch, which is what decides whether the
    # developer should end up on a branch or on a detached tag.
    is_branch = git.quiet(["rev-parse", "--verify", "--quiet",
                           "refs/remotes/origin/{}".format(app["ref"])], cwd=target)
    on_branch = git.maybe(["symbolic-ref", "--short", "HEAD"], cwd=target)
    in_place = head == wanted and (on_branch == app["ref"] if is_branch else True)

    if in_place:
        return "unchanged", "{} at {}".format(app["ref"], head[:9])

    dirty = git.out(["status", "--porcelain"], cwd=target)
    if dirty:
        raise FetchError(
            "{} has uncommitted changes and is not at {}. Refusing to check out "
            "over your work -- commit, stash, or discard it first.\n"
            "  currently: {}\n"
            "  wanted:    {} at {}\n"
            "  changed:   {} file(s)"
            .format(target, app["ref"], describe(git, target),
                    app["ref"], wanted[:9], len(dirty.splitlines()))
        )

    if is_branch:
        git.run(["checkout", app["ref"]], cwd=target)
        # --ff-only: if the local branch has commits the remote does not, that
        # is the developer's work and it is not this tool's to rebase or reset.
        git.run(["merge", "--ff-only", "FETCH_HEAD"], cwd=target)
    else:
        git.run(["checkout", "--detach", wanted], cwd=target)

    return "updated", "{} {} -> {}".format(app["ref"], head[:9], wanted[:9])


def short(git, target):
    return git.out(["rev-parse", "--short=9", "HEAD"], cwd=target)


def describe(git, target):
    branch = git.maybe(["symbolic-ref", "--short", "HEAD"], cwd=target)
    return branch if branch else "detached at {}".format(short(git, target))


def assert_no_token_on_disk(repo_dir, secrets):
    """
    Acceptance item 14, enforced where it can still be acted on.

    Nothing here writes a token into a URL, so this should never fire. It is
    here because "should never" is a claim, and the cost of being wrong about
    this particular claim is a credential in a file that gets committed.
    """
    config = Path(repo_dir) / ".git" / "config"
    if not config.is_file():
        return
    text = config.read_text(encoding="utf-8", errors="replace")
    for secret in secrets:
        if secret and secret in text:
            config.write_text(text.replace(secret, ""), encoding="utf-8")
            raise FetchError(
                "a credential was written into {} -- it has been removed, but "
                "this is a bug in fetch_apps.py and the token should be treated "
                "as exposed and rotated.".format(config)
            )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Clone every app in config.toml into bench/apps/.",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", required=True,
                        help="the project root, which holds bench/")
    parser.add_argument("--only", metavar="NAME",
                        help="fetch just this app, by its config.toml name")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would happen; touch nothing")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        secrets, warnings, _sources = resolve_all_secrets(
            cfg, args.config, groups=(SECRETS_FETCH,),
        )
    except ConfigError as exc:
        sys.stdout.flush()
        sys.stderr.write("config.toml is not usable:\n")
        for problem in exc.problems:
            sys.stderr.write("  - {}\n".format(problem))
        return 2

    for warning in warnings:
        sys.stderr.write("warning: {}\n".format(warning))

    apps = cfg["apps"]
    if args.only:
        apps = [a for a in apps if a["name"] == args.only]
        if not apps:
            sys.stderr.write("no app named {!r} in {}\n".format(args.only, args.config))
            return 2

    apps_dir = Path(args.root).resolve() / "bench" / "apps"
    token_values = [v for k, v in secrets.items() if k.endswith(".token")]

    print("fetching {} app(s) into {}".format(len(apps), apps_dir))
    if args.dry_run:
        print("(dry run: nothing will be written)")

    failures = []
    with tempfile.TemporaryDirectory(prefix="erp-sandbox-askpass-") as tmp:
        askpass = Path(tmp) / "askpass.sh"
        askpass.write_text(ASKPASS_SCRIPT, encoding="utf-8")
        askpass.chmod(0o700)
        git = Git(token_values, askpass)

        for app in apps:
            name = app["name"]
            target = apps_dir / name
            credential = credential_for(app, cfg, secrets)
            try:
                state, detail = fetch_one(git, app, target, credential, args.dry_run)
            except FetchError as exc:
                message = str(exc)
                print("  {:<12} {}".format("FAILED", name))
                for line in message.splitlines():
                    print("               {}".format(line))
                hint = hint_for(message, app, credential)
                if hint:
                    print("               -> {}".format(hint))
                failures.append(name)
            else:
                print("  {:<12} {:<12} {}".format(state, name, detail))

    if failures:
        sys.stdout.flush()
        sys.stderr.write("\n{} app(s) failed: {}\n".format(len(failures), ", ".join(failures)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
