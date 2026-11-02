#!/bin/sh
# Point this checkout's hooks at tools/, so the hook is version-controlled
# rather than living only in one person's .git directory.
set -e
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath tools
echo "core.hooksPath = tools"
echo "pre-commit will now run: compileall, selftest, and a staged-secret check."
echo "Bypass a single commit with: git commit --no-verify"
