# Repository Workflow

## Repository Guidance

- Read `README.md` and the relevant source and tests before making changes.
- Treat `README.md` as the user-facing behavior and safety contract. Keep it in sync with changes to CLI options, output, exit codes, backup or restore behavior, and Plex API handling.
- This repository intentionally ships as a small, standard-library-only Python tool. Keep it free of third-party runtime dependencies unless a change explicitly requires one.
- Preserve unrelated working-tree changes and generated local output. Never commit live Plex tokens, real `Preferences.xml` files, real Plex databases, generated backup bundles, apply logs or CSV reports, or unredacted API responses.

## Repository Layout

- `plex_nfkd_title_sort_api.py` contains the CLI, Plex HTTP client, scan and planning logic, apply verification, diagnostics, database backup, and guarded restore implementation.
- `tests/test_safety_regressions.py` contains standard-library unit and regression tests using mocks, temporary files, and synthetic SQLite databases.
- `.github/workflows/tests.yml` is the authoritative CI matrix and runs compilation plus the full unit-test suite on Python 3.9, 3.11, and 3.13.
- `README.md` documents installation, operational safety, CLI usage, outputs, exit codes, and recovery procedures.

## Compatibility and Implementation Style

- Keep the script compatible with every Python version in the CI matrix; Python 3.9 is the minimum compatibility target unless the workflow and documentation are deliberately updated together.
- Use only the Python standard library unless an external dependency is explicitly approved.
- Follow the existing code style and naming patterns. Prefer focused helpers and explicit error handling over broad rewrites of the single-file application.
- Preserve cross-platform behavior where practical, especially path handling, output encoding, file metadata handling, and Windows versus POSIX filesystem differences.
- Preserve pagination, bounded detail and verification batches, the temporary JSONL candidate spool, and the rule that scanning completes before updates begin. Do not replace these with an approach that loads all Plex metadata into memory without a documented reason.

## Sort-Key Behavior

- Build a sort key from a non-empty Plex `titleSort`; fall back to `title` only when `titleSort` is missing or empty.
- Remove leading whitespace, punctuation, symbols, and a configured Plex article before NFKD normalization.
- Preserve meaningful custom ordering such as `Matrix, The` rather than rebuilding every sort key from `title`.
- Preserve the effective-value rule for Plex responses that omit a redundant `titleSort`: omission is equivalent only when the returned `title` equals the target and the `titleSort` field lock is explicitly present.
- Keep the default scope limited to non-empty Korean titles. Treat `--all-unicode` as a high-impact opt-in because it can lock Sort Title for every non-empty title.
- Keep playlist behavior distinct from library-section behavior; playlists have no library section and cannot be combined with `--section` when metadata type 15 is selected.

## Safety Invariants

- Read and change metadata only through the Plex HTTP API. Never add SQL metadata updates or use either Plex database as a metadata source.
- Dry-run mode must send zero HTTP PUT requests and must not open either Plex database.
- Apply mode must finish the scan and open requested output files before any change. Unless `--no-backup` is explicitly supplied, it must successfully back up both Plex databases before the first PUT.
- Keep backup destinations outside the Plex Media Server data directory. Retain SQLite online backup, integrity metadata, and SHA-256 manifest verification for both database files.
- Re-read every candidate immediately before PUT. If `title`, `titleSort`, or the Sort Title lock changed since planning, skip it without sending a PUT and record the failure.
- Verify every successful PUT by reading the item back through the API. Do not report success unless the effective target value and lock are confirmed.
- Preserve per-run diagnostics for apply failures, including redaction of Plex tokens from logs and saved responses.
- Never expose a live Plex token in URLs, console output, JSON, CSV, diagnostics, tests, or committed files. Preserve the documented token-discovery precedence.
- Restore must always require `--confirm-plex-stopped`. Treat only an explicit connection refusal as automatic evidence that Plex is stopped; timeouts, DNS failures, TLS failures, and other ambiguous errors must block restore.
- Before restore, validate the manifest, checksums, required database pair, and recorded Plex URL. Preserve current databases and WAL/SHM sidecars in safety copies, use staged durable writes, and roll back both databases and all sidecars if installation fails.
- Do not weaken a safety check or destructive-operation guard merely to make a failing test pass. Document and test any intentional change to these invariants.

## Output Contracts

- Keep machine-readable JSON valid on stdout. Progress belongs on stderr, and JSON mode must suppress ANSI escapes even when color is forced.
- In automatic color mode, emit ANSI sequences only to an interactive stream, honoring `NO_COLOR` and `TERM=dumb`; evaluate stdout and stderr independently.
- Do not emit carriage-return progress control behavior to redirected stderr.
- Preserve UTF-8-with-BOM CSV output and the documented field meanings unless an intentional compatibility change is documented.
- Keep documented result names and exit-code meanings stable. Report only operations and validation that actually occurred.

## Validation

- Run the tracked checks after code changes:

  ```bash
  python3 -m py_compile plex_nfkd_title_sort_api.py
  python3 -m unittest discover -s tests -v
  ```

- On platforms where the interpreter is named `python`, use the equivalent `python` commands.
- Add or update regression tests for changed planning, API, output, backup, restore, or failure-handling behavior. Prefer mocks and temporary synthetic SQLite databases.
- Automated tests must not require a live Plex server, use a real Plex token, modify a real Plex database, or send changes to a live library.
- Manual Plex validation is optional unless explicitly requested. Default to dry-run and do not perform live `--apply` or `--restore-backup` validation without explicit authorization and an identified safe target.
- Re-run the full tracked checks after material changes or base updates and before a pull request is merged.

## Git Workflow

- Start each change from an up-to-date `origin/main`.
- If `origin/main` advances after a pull request is opened, report the drift before final review or merge.
- Do not commit or push directly to `main`.
- Create a dedicated branch for each logical change.
- Keep unrelated changes in separate branches and pull requests.
- Preserve unrelated working-tree changes.
- Review the complete diff before committing or opening a pull request.
- Create a pull request only when requested or when the agreed workflow explicitly requires one.
- Before merging, confirm that the pull request is not a draft, is mergeable, has passed required checks, has no unresolved review conversations, and reports current validation results.
- Do not merge a pull request without explicit user approval.
- Use a merge commit unless another merge strategy is explicitly requested.
- Delete the source branch after merging only when requested.

## Commits

- Follow the style established by the recent commit history.
- Keep each commit focused on one logical change.
- Use a concise, imperative subject that describes the result.
- Include motivation, significant implementation details, safety impact, and validation results in the body when useful.
- Preserve pull-request commit history; do not rebase, amend, squash, force-push, or otherwise rewrite commits unless explicitly requested.
- Do not include credentials, personal paths, sensitive data, or identifying sample data.

## Pull Requests

- Use a concise title that describes the primary result.
- Include `Summary` and `Validation` sections in the description. Add `Safety`, `Compatibility`, `Changes`, or `Scope` when relevant.
- Report only checks that were actually performed, and keep results current as the pull request changes.
- Clearly state untested behavior, Plex compatibility concerns, operational risks, and any use of `--no-backup` or live-server validation.
- Exclude unrelated changes and sensitive or identifying data.

## Versioning

- `VERSION` in `plex_nfkd_title_sort_api.py` is the canonical program version.
- Change it only for an intentional release-level behavior change or when explicitly requested, and update corresponding README output or examples when relevant.
- Apply a version change once per logical release change. Do not change the version for documentation-only or test-only work unless explicitly required.

## Code Review

- Check correctness, regressions, security, Unicode normalization, Plex API compatibility, and unintended behavior changes.
- Give extra scrutiny to dry-run isolation, stale-plan detection, token redaction, backup ordering, restore reachability decisions, checksum validation, rollback completeness, and output stream separation.
- Confirm that error handling and fallback behavior remain fail-safe and that tests cover changed behavior where practical.
- Treat missing validation, weakened safety guarantees, undocumented output changes, and unresolved operational risks as review findings.
- Keep feedback focused, actionable, and supported by code, tests, or documented behavior.
