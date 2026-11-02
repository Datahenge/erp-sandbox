# Documentation

**Current facts** — "what is true now". The project's history and status are
kept in the maintainers' private notes, not here.

## Current facts

| Document | Contents |
|---|---|
| [requirements.md](requirements.md) | Hard requirements, prerequisites, explicit non-goals |
| [decisions.md](decisions.md) | **The authority** on what has been agreed |
| [functional.md](functional.md) | What the tool does, from the developer's side |
| [technical.md](technical.md) | Architecture, the `config.toml` contract, credentials, seeding, and the constraints that bite |
| [testing.md](testing.md) | Acceptance checklist, `erp-sandbox selftest`, `erp-sandbox doctor` |
| [setup.md](setup.md) | First run: install to a site you can log into |
| [using.md](using.md) | Day-to-day work, editor, debugging, teardown |
| [troubleshooting.md](troubleshooting.md) | Symptom → cause → fix, for a broken environment |
| [contributing.md](contributing.md) | Working on `erp-sandbox` itself: setup, commands, rules |

`setup.md`, `using.md`, `troubleshooting.md` and `contributing.md` are
task-oriented and written for a reader in a hurry: steps, bullets, no
narrative.

Start with `functional.md`. `technical.md` is the design;
`decisions.md` is the authority. If another document contradicts `decisions.md`,
the other document is wrong — fix it, and say so.

[../CHANGELOG.md](../CHANGELOG.md) has timestamped changes. Rarely needed.

A pitfall that will bite again is a current fact, and it belongs in
`technical.md` in the present tense.
