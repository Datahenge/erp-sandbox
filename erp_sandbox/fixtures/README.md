# fixtures

Configurations that must be **rejected**, and the message each one must
produce. `erp-sandbox selftest` runs them.

Two kinds live here, for one reason:

- **`*.toml` in this directory** — configurations that cannot be expressed as
  an edit to `config.sample.toml`: malformed TOML, a config missing whole
  sections, a config that is empty. They are standalone files because there is
  nothing to derive them from.
- **The `MUTATIONS` list in `selftest.py`** — everything else. Each case is a
  one-line edit applied to `config.sample.toml` at run time, so the schema has
  exactly one home. Copying the whole sample once per case would give the
  schema eleven homes, and ten of them would drift.

The trade-off of the mutation approach is that a case breaks if the sample's
formatting changes under it. That failure is loud and the fix is a one-line
edit, which is the right way round: a stale copy of a schema fails silently.

**Nothing here may carry a client value** (decision #17). Use `acme` and
`example.com`, as the sample does.
