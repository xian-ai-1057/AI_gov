# ts_parser source provenance

- Source snapshot: `/Users/xian/Documents/00_CODE/Taishin/arkkb/src/ts_parser`
- Imported: 2026-07-28
- Upstream Git commit: unavailable — the supplied source directory has no `.git` metadata.
- Snapshot SHA-256 manifest: `docs/ts_parser_source.sha256`
- Integration path: `src/ts_parser/`

The upstream checkout currently cannot be used by `git subtree` because it is not a Git working
tree. The source is kept at the final subtree prefix so a future upstream split branch can replace
the snapshot without changing application imports. AI-governance-specific behavior must remain in
`govcheck.parsers.supporting`; shared parser fixes should be contributed upstream before a future
subtree sync.

