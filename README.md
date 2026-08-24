# Plex NFKD Sort Title

A safety-focused, standard-library Python tool that normalizes Plex Sort Title
values to Unicode NFKD and locks the Sort Title field through the Plex Media
Server HTTP API.

This project is not affiliated with or endorsed by Plex.

## Overview

The tool reads media metadata from a running Plex Media Server. In apply mode,
it sends the following field update through the Plex API:

~~~text
titleSort.value = unicodedata.normalize("NFKD", sort_base)
titleSort.locked = 1
~~~

The default operation is a dry run limited to titles that contain Korean
characters. A dry run sends zero HTTP PUT requests and does not open either
Plex database.

When Plex already supplies a non-empty titleSort, the tool first removes any
leading punctuation, symbols, whitespace, or configured grammatical article,
then applies NFKD. Meaningful custom keys such as `Matrix, The` remain intact.
If titleSort is missing or empty, the tool derives the same Plex-style sort key
from title before normalization.

## Important Safety Notice

Always review a dry run before using --apply.

- Metadata is read and changed only through the Plex API.
- The tool does not execute SQL UPDATE, INSERT, DELETE, REPLACE, or schema
  changes against the Plex databases.
- Dry-run mode never opens the Plex databases.
- Apply mode creates a consistent online backup of both Plex databases before
  sending API changes, unless --no-backup is explicitly supplied.
- The backup directory must be outside the Plex Media Server data directory.
- Every applied value and lock is read back from the API and verified.
- Every candidate is read again immediately before PUT. If title, titleSort, or
  its lock changed after the scan, the candidate is skipped without a PUT.
- Apply failures receive per-run diagnostic logs and saved API responses.
- Database restore is refused while Plex Media Server is reachable or when its
  stopped state cannot be confirmed unambiguously.
- A failed restore automatically rolls back both databases and all WAL/SHM
  sidecars from the pre-restore safety copies.

The database files are used only for backup and guarded restore. They are never
used as the metadata source.

## Requirements

- Python 3
- A running Plex Media Server for scan and apply operations
- Read access to Preferences.xml for automatic token discovery
- Read access to both Plex database files when using the default apply backup
- No third-party Python packages

The script uses only Python standard-library modules.

## Installation

Copy plex_nfkd_title_sort_api.py to the Synology NAS and make it executable:

~~~bash
chmod 700 plex_nfkd_title_sort_api.py
./plex_nfkd_title_sort_api.py --help
~~~

It can also be run explicitly with Python:

~~~bash
python3 plex_nfkd_title_sort_api.py --help
~~~

The default server URL is the local Plex server:

~~~text
http://127.0.0.1:32400
~~~

## Quick Start

### 1. Run a dry run

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --dry-run \
  --csv nfkd_title_sort_dry_run.csv
~~~

The --dry-run option is optional because dry run is the default mode.

Add Unicode code points to the terminal preview:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --dry-run \
  --show-codepoints
~~~

Print detailed information for every candidate without requiring CSV output:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --dry-run \
  --console
~~~

Limit detailed terminal output when there are many candidates:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --dry-run \
  --console \
  --console-limit 100 \
  --show-codepoints
~~~

### 2. Review the plan

The summary separates:

- rows scanned and rows selected by scope
- already-compliant explicit titleSort values
- already-compliant Plex title fallback values
- titleSort value edits
- Sort Title lock additions
- candidates skipped before PUT because their state changed or could not be read
- set-and-lock, set-only, and lock-only actions
- current titleSort status
- existing versus derived sort-key sources
- existing titleSort values that required Plex-style prefix sanitization
- candidates by library and metadata type

The default preview shows up to 20 candidates. Use --preview-limit to change
that number.

### 3. Apply after review

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --apply \
  --backup-dir /volume1/Backups/Plex-NFKD \
  --csv nfkd_title_sort_applied.csv
~~~

For a remote server, nonstandard port, or nonstandard database location:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --plex-url http://192.168.0.10:32400 \
  --apply \
  --db-dir '/volume1/PlexMediaServer/AppData/Plex Media Server/Plug-in Support/Databases' \
  --backup-dir /volume1/Backups/Plex-NFKD \
  --csv nfkd_title_sort_applied.csv
~~~

Apply mode does not begin API updates until the pre-apply database backup
succeeds. The backup can be disabled only with the explicit --no-backup option.

## How Sort Keys Are Built

The target is selected in this order:

1. Start with a non-empty titleSort returned by Plex, or title when titleSort is
   missing or empty.
2. Remove leading whitespace, punctuation, and symbols from that value.
3. Remove a leading article listed in Plex ArticleStrings.
4. Preserve the remainder, including meaningful custom suffixes or ordering.
5. Normalize the resulting sort base with Unicode NFKD.
6. Set and lock the target through the Plex API.

Conceptually:

~~~python
if current_title_sort not in (None, ""):
    sort_base, _ = derive_plex_sort_base(current_title_sort, article_strings)
else:
    sort_base, _ = derive_plex_sort_base(title, article_strings)

target_title_sort = unicodedata.normalize("NFKD", sort_base)
~~~

This differs intentionally from applying NFKD directly to title in every case.
For example, if Plex has already removed an article or uses a custom key such as
`Matrix, The`, that meaningful ordering remains intact. A redundant key such as
`The Matrix` is sanitized to `Matrix` before normalization.

ArticleStrings is read from Preferences.xml. If the setting is unavailable,
the following Plex defaults are used:

~~~text
the, das, der, a, an, el, la
~~~

Override the list when necessary:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --article-strings 'the,a,an' \
  --dry-run
~~~

## Plex May Omit a Redundant titleSort

Plex can omit the titleSort attribute from API responses when titleSort is
identical to title. The tool treats this as the same effective sort value only
when all of the following conditions are true:

~~~text
API titleSort is missing
API title equals target_title_sort
Field name="titleSort" locked="1" is present
~~~

This prevents an already-correct item from failing verification or repeatedly
appearing in later scans. A candidate that still needs only the lock is
reported as TITLE_SORT_OMITTED_EQUIVALENT.

## Plex API and Database Responsibilities

Metadata operations use these API areas:

- Library listing: /library/sections/{section_id}/all
- Library detail: /library/metadata/{ids}
- Playlist listing: /playlists
- Playlist detail: /playlists/{id}
- Apply parameters: titleSort.value and titleSort.locked

The databases are used only for:

- SQLite online backup before apply
- checksum-validated restore while Plex is stopped

Both of these files are backed up together:

~~~text
com.plexapp.plugins.library.db
com.plexapp.plugins.library.blobs.db
~~~

## Automatic Plex Token Discovery

The token is resolved in the following order:

1. --token
2. --token-file
3. PLEX_TOKEN environment variable
4. Preferences.xml supplied with --preferences
5. Known Synology DSM, Linux, and Docker Preferences.xml locations

Common Synology locations include:

~~~text
DSM 7:
/volume*/PlexMediaServer/AppData/Plex Media Server/Preferences.xml

DSM 6:
/volume*/Plex/Library/Application Support/Plex Media Server/Preferences.xml
~~~

Preferences.xml is read without modification. PlexOnlineToken is sent only in
the X-Plex-Token HTTP header. The token value is not written to the URL, CSV,
console, or diagnostic logs.

If Preferences.xml is not readable, run with appropriate permissions or use a
restricted token file:

~~~bash
chmod 600 plex-token.txt
python3 plex_nfkd_title_sort_api.py \
  --token-file plex-token.txt \
  --dry-run
~~~

Do not copy or publish the complete Preferences.xml file.

## Scope and Filters

The default scope is every non-empty title containing Korean characters.

Limit a scan to one library section:

~~~bash
python3 plex_nfkd_title_sort_api.py --section 4 --dry-run
~~~

Limit a scan to one metadata type:

~~~bash
python3 plex_nfkd_title_sort_api.py --metadata-type 4 --dry-run
~~~

Combine both filters:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --section 4 \
  --metadata-type 4 \
  --dry-run
~~~

Common metadata type numbers:

| Number | Type |
|---:|---|
| 1 | Movie |
| 2 | Show |
| 3 | Season |
| 4 | Episode |
| 8 | Artist |
| 9 | Album |
| 10 | Track |
| 12 | Clip |
| 13 | Photo |
| 14 | Photo Album |
| 15 | Playlist |
| 18 | Collection |

Playlist items have no library section, so --metadata-type 15 cannot be
combined with --section.

Exclude playlists:

~~~bash
python3 plex_nfkd_title_sort_api.py --no-playlists --dry-run
~~~

### All Unicode Titles

Use --all-unicode to select every non-empty title, including ASCII titles:

~~~bash
python3 plex_nfkd_title_sort_api.py --all-unicode --dry-run
~~~

This option can lock a large number of Sort Title fields. Review the dry run
carefully before applying it.

## Terminal and JSON Output

Long operations report phases to stderr:

~~~text
[1/4] Connected to Plex 1.x.x
[2/4] Scanning metadata
[3/4] Creating the database backup
[4/4] Applying and verifying changes
~~~

Hide phase and progress messages with --quiet-progress.

### ANSI Color Safety

The default --color auto mode emits ANSI colors only when the destination
stream is an interactive terminal. stdout and stderr are evaluated separately.
This means terminal output can be colored while redirected files, pipelines,
Synology Task Scheduler logs, and other non-TTY destinations remain free of
ANSI escape sequences.

Color modes:

- --color auto: color only on a TTY; this is the default.
- --color always: force ANSI color even through a pipe or redirection.
- --color never: disable ANSI color everywhere.

Auto mode also disables color when NO_COLOR is present or TERM is set to dumb.
JSON output disables color unconditionally, including progress on stderr, even
when --color always is supplied.

~~~bash
# Interactive terminals use color automatically.
python3 plex_nfkd_title_sort_api.py --dry-run

# No ANSI control sequences are written to this log.
python3 plex_nfkd_title_sort_api.py --dry-run > result.log 2>&1

# Explicitly disable color.
python3 plex_nfkd_title_sort_api.py --dry-run --color never

# Use the standard environment convention to disable automatic color.
NO_COLOR=1 python3 plex_nfkd_title_sort_api.py --dry-run
~~~

Interactive progress uses a carriage return only when stderr is a terminal.
When stderr is redirected, every progress update is written as an ordinary
line without carriage-return control behavior. Avoid --color always when the
destination is a file unless ANSI sequences are intentionally required.

Every successful run prints a clear result and exit code:

~~~text
RESULT: CHANGES PLANNED
HTTP PUT requests: 0
Exit code: 0
~~~

Possible result names include:

- NO CHANGES REQUIRED
- CHANGES PLANNED
- APPLY SUCCEEDED
- APPLY PARTIALLY FAILED
- APPLY FAILED
- RESTORE SUCCEEDED
- ERROR

For machine-readable output:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --dry-run \
  --output-format json \
  > nfkd_report.json
~~~

JSON is written to stdout. Progress remains on stderr, so the redirected JSON
document stays valid.

Use --verbose to show the server machine identifier, token discovery source,
and ArticleStrings source. The token value itself is never shown.

### Exit Codes

| Code | Meaning |
|---:|---|
| 0 | Completed normally |
| 2 | Argument, token, or output-file error |
| 3 | Plex connection, authentication, or scan error |
| 4 | One or more apply items failed |
| 5 | Backup, restore, logging, or unexpected apply error |

## CSV Output

CSV files use UTF-8 with a BOM for compatibility with spreadsheet software.
The candidate/result export includes:

~~~text
metadata_id
library_section_id
library_name
metadata_type
metadata_type_name
api_kind
parent_id
parent_title
grandparent_id
grandparent_title
show_title
season_index
episode_index
title
title_sort
nfkd_title
sort_base
target_title_sort
sort_key_source
removed_prefix
title_is_nfkd
title_sort_matches_nfkd
title_sort_matches_target
effective_title_sort_matches_target
title_sort_omitted_as_redundant
title_sort_status
title_sort_locked
contains_korean
action
result
error
~~~

Action values:

- SET_VALUE_AND_LOCK: change the value and add the lock
- SET_VALUE: change the value while preserving an existing lock
- LOCK_ONLY: keep the effective value and add only the lock

Result values:

- DRY_RUN: planned without sending changes
- VERIFIED: value and lock confirmed after PUT
- APPLY_FAILED: API PUT failed
- PRE_APPLY_FAILED: the fresh state could not be read, so no PUT was sent
- SKIPPED_STALE_PLAN: metadata changed after scanning, so no PUT was sent
- VERIFY_FAILED: expected value or lock could not be confirmed

nfkd_title is NFKD(title) for comparison. The actual applied value is
target_title_sort, which may preserve a Plex or custom sort base.

## Apply Logs and Failure Diagnostics

Each apply run creates a dedicated log directory. By default it is stored
inside the backup bundle:

~~~text
plex-db-backup-YYYYMMDD-HHMMSS/
  logs/
    apply-run-YYYYMMDD-HHMMSS/
      run.log
      failures.csv
      summary.json
      responses/
        library-12345.xml
~~~

- run.log records PUT and verification events in time order.
- failures.csv records IDs, stages, error codes, expected and actual values,
  and lock states.
- summary.json contains scan/apply totals, failure counts, and representative
  failure samples.
- responses contains the Plex XML response for each verification failure.

Tokens are replaced with REDACTED before logs or response files are written.

With --no-backup, logs default to ./plex_nfkd_logs. Set a custom parent:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --apply \
  --no-backup \
  --log-dir /volume1/homes/user/plex_nfkd_logs
~~~

The terminal prints failure counts and up to five representative failures.
Inspect failures.csv and responses before retrying.

Common failure codes:

- PRE_APPLY_FETCH_ERROR
- ITEM_NOT_FOUND_BEFORE_APPLY
- STALE_PLAN
- APPLY_REQUEST_ERROR
- VERIFY_FETCH_ERROR
- ITEM_NOT_FOUND
- TITLE_CHANGED
- TITLE_SORT_MISMATCH
- TITLE_SORT_LOCK_NOT_CONFIRMED

## Backup Restore

Stop Plex Media Server completely before restore. The script refuses to
restore while the Plex identity endpoint is reachable. A timeout, DNS failure,
TLS error, or other ambiguous identity-check failure also blocks restore; only
an explicit connection refusal is accepted as an automatic stopped-state
signal. The explicit --confirm-plex-stopped option is always required.

Example for Synology DSM 7:

~~~bash
sudo synopkg stop PlexMediaServer

python3 plex_nfkd_title_sort_api.py \
  --restore-backup /volume1/Backups/Plex-NFKD/plex-db-backup-YYYYMMDD-HHMMSS \
  --confirm-plex-stopped

sudo synopkg start PlexMediaServer
~~~

Before replacement, the current main database, blobs database, and any WAL/SHM
sidecars are copied to a pre-restore-current-db-* safety directory. Restore
files are verified against the SHA-256 backup manifest, copied through staging
files, and installed while preserving existing mode and ownership when
possible. WAL/SHM sidecars are removed from the active directory before the
restored databases are installed. If either database installation fails, the
tool automatically restores both original databases and every original
sidecar. If automatic rollback itself fails, the tool prints a critical warning
not to start Plex and identifies the safety-copy directory.

The Plex URL used for restore must match the URL recorded in the backup
manifest. If the server address intentionally changed, verify the target and
use the explicit override:

~~~bash
python3 plex_nfkd_title_sort_api.py \
  --restore-backup /volume1/Backups/Plex-NFKD/plex-db-backup-YYYYMMDD-HHMMSS \
  --confirm-plex-stopped \
  --allow-plex-url-mismatch \
  --plex-url http://127.0.0.1:32400
~~~

## Performance and Implementation Details

- Uses Plex pagination headers.
- Processes API listings page by page.
- Fetches details and verification responses in bounded batches.
- Does not load all Plex metadata into memory.
- Finishes the complete scan before starting any update.
- Streams candidates through a temporary JSONL file.
- Opens CSV output before starting API changes.
- Backs up both databases before the first PUT.
- Re-reads every candidate immediately before PUT and skips stale plans.
- Records database size, SQLite page count, and SHA-256 in the manifest.
- Refuses backup destinations inside the Plex data directory.
- Verifies successful library changes with batched API reads.
- Uses idempotent planning so already-correct locked items are skipped.

## Validation

The repository includes standard-library unit and regression tests under
`tests/`, plus a GitHub Actions matrix for Python 3.9, 3.11, and 3.13. The
project has also been tested end to end with a mock Plex HTTP server and
synthetic SQLite databases.

Run the tracked tests locally:

~~~bash
python3 -m py_compile plex_nfkd_title_sort_api.py
python3 -m unittest discover -s tests -v
~~~

~~~text
SYNTAX_IMPORT=OK
AUTO_TOKEN_FROM_PREFERENCES=OK
PAGINATION=OK
PLAYLIST_ONLY_FILTER=OK
PLEX_AWARE_SORT_KEYS=OK
CONSOLE_SUMMARY_AND_DETAILS=OK
JSON_OUTPUT=OK
ANSI_COLOR_MODES=OK
NO_COLOR_AND_TERM_DUMB=OK
JSON_ANSI_DISABLED=OK
REDIRECTED_OUTPUT_NO_CONTROL_SEQUENCES=OK
PHASE_AND_PROGRESS_OUTPUT=OK
COMPACT_ZERO_CHANGE_OUTPUT=OK
DRY_RUN_ZERO_PUT=OK
PRE_APPLY_ONLINE_BACKUP=OK
LIBRARY_AND_PLAYLIST_APPLY=OK
NFKD_AND_LOCK_VERIFY=OK
PLEX_OMITTED_REDUNDANT_TITLE_SORT=OK
APPLY_FAILURE_DIAGNOSTIC_LOGS=OK
LIVE_SERVER_RESTORE_REFUSAL=OK
GUARDED_DATABASE_RESTORE=OK
UTF8_BOM_CSV=OK
IDEMPOTENT_RERUN=OK
~~~

The current verification logic was also replayed against 128 captured Plex API
responses from a real apply run:

~~~text
verified: 128
titleSort omitted as redundant: 128
remaining candidates after replanning: 0
~~~

Those responses represented locked values for which Plex omitted a redundant
titleSort attribute. They are now verified through the effective title fallback
and excluded from later candidate plans.

No automated test writes to a live Plex server.

## References

- Plex Media Server API: https://developer.plex.tv/pms/
- Finding a Plex authentication token:
  https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/
- Plex Media Server data directory:
  https://support.plex.tv/articles/202915258-where-is-the-plex-media-server-data-directory-located/
- Plex Edit Details and field locks:
  https://support.plex.tv/articles/201272763-edit-details/
- Plex ArticleStrings:
  https://support.plex.tv/articles/201105343-advanced-hidden-server-settings/
- Restoring a Plex database backup:
  https://support.plex.tv/articles/202485658-restore-a-database-backed-up-via-scheduled-tasks/
