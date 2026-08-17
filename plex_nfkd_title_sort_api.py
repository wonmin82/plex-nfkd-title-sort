#!/usr/bin/env python3
"""
Plex API-only NFKD Sort Title checker/updater.

This program reads and writes metadata only through Plex Media Server's HTTP
API. Before --apply, it can use SQLite's online backup API to make a consistent
backup of the two Plex database files; it never queries metadata from them.

Default behavior is a dry run limited to titles containing Korean characters.
"""

import argparse
import csv
import datetime
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path


PROGRAM = "plex_nfkd_title_sort_api"
VERSION = "1.3.0"
DEFAULT_PLEX_URL = "http://127.0.0.1:32400"
DEFAULT_ARTICLE_STRINGS = ("the", "das", "der", "a", "an", "el", "la")
MAIN_DB_NAME = "com.plexapp.plugins.library.db"
BLOBS_DB_NAME = "com.plexapp.plugins.library.blobs.db"
BACKUP_MANIFEST_NAME = "plex_nfkd_backup_manifest.json"

TYPE_NAMES = {
    1: "Movie",
    2: "Show",
    3: "Season",
    4: "Episode",
    5: "Trailer",
    6: "Comic",
    7: "Person",
    8: "Artist",
    9: "Album",
    10: "Track",
    11: "Picture",
    12: "Clip",
    13: "Photo",
    14: "Photo Album",
    15: "Playlist",
    16: "Playlist Folder",
    18: "Collection",
    42: "Optimized Version",
}

TYPE_IDS = {
    "movie": 1,
    "show": 2,
    "season": 3,
    "episode": 4,
    "trailer": 5,
    "comic": 6,
    "person": 7,
    "artist": 8,
    "album": 9,
    "track": 10,
    "picture": 11,
    "clip": 12,
    "photo": 13,
    "photoalbum": 14,
    "playlist": 15,
    "playlistfolder": 16,
    "collection": 18,
}

# Types normally addressable through /library/sections/{id}/all for each
# section kind.  Optional legacy/extra types are harmless read-only probes;
# servers that do not expose them commonly return an empty result or 400.
SECTION_TYPE_PLAN = {
    "movie": ([1], [5, 12, 18]),
    "show": ([2, 3, 4], [5, 12, 18]),
    "artist": ([8, 9, 10], [18]),
    "photo": ([13, 14], []),
}

CSV_FIELDS = [
    "metadata_id",
    "library_section_id",
    "library_name",
    "metadata_type",
    "metadata_type_name",
    "api_kind",
    "parent_id",
    "parent_title",
    "grandparent_id",
    "grandparent_title",
    "show_title",
    "season_index",
    "episode_index",
    "title",
    "title_sort",
    "nfkd_title",
    "sort_base",
    "target_title_sort",
    "sort_key_source",
    "removed_prefix",
    "title_is_nfkd",
    "title_sort_matches_nfkd",
    "title_sort_matches_target",
    "effective_title_sort_matches_target",
    "title_sort_omitted_as_redundant",
    "title_sort_status",
    "title_sort_locked",
    "contains_korean",
    "action",
    "result",
    "error",
]

FAILURE_FIELDS = [
    "timestamp_utc",
    "stage",
    "error_code",
    "metadata_id",
    "library_section_id",
    "library_name",
    "metadata_type",
    "metadata_type_name",
    "api_kind",
    "action",
    "title",
    "current_title_sort",
    "expected_title_sort",
    "actual_title",
    "actual_title_sort",
    "expected_locked",
    "actual_locked",
    "returned_fields",
    "error",
    "response_file",
]


class PlexApiError(RuntimeError):
    def __init__(self, method, path, status=None, message=""):
        self.method = method
        self.path = path
        self.status = status
        self.message = message
        label = "HTTP {0}".format(status) if status is not None else "network error"
        super().__init__("{0} {1}: {2}: {3}".format(method, path, label, message))


class PlexClient:
    def __init__(self, base_url, token, timeout):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request_xml(self, method, path, params=None, extra_headers=None):
        query = urllib.parse.urlencode(params or {}, doseq=True)
        request_path = path if path.startswith("/") else "/" + path
        url = self.base_url + request_path
        if query:
            url += ("&" if "?" in url else "?") + query

        headers = {
            "Accept": "application/xml",
            "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": "plex-nfkd-title-sort-api-v1",
            "X-Plex-Product": PROGRAM,
            "X-Plex-Version": VERSION,
        }
        if extra_headers:
            headers.update(extra_headers)
        data = b"" if method.upper() in ("PUT", "POST") else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(1000).decode("utf-8", "replace").strip()
            except Exception:
                body = ""
            raise PlexApiError(method.upper(), request_path, exc.code, body or exc.reason) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PlexApiError(method.upper(), request_path, None, str(exc)) from exc

        if not payload:
            return ET.Element("MediaContainer"), response_headers
        try:
            return ET.fromstring(payload), response_headers
        except ET.ParseError as exc:
            raise PlexApiError(method.upper(), request_path, None, "invalid XML response") from exc

    def get(self, path, params=None, extra_headers=None):
        return self.request_xml("GET", path, params=params, extra_headers=extra_headers)

    def put(self, path, params=None):
        return self.request_xml("PUT", path, params=params)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def redact_text(value, secrets=()):
    text = "" if value is None else str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "<REDACTED>")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme and parsed.netloc and parsed.query:
        pairs = []
        for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if "token" in key.lower():
                item = "<REDACTED>"
            pairs.append((key, item))
        text = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(pairs), parsed.fragment)
        )
    return text


def make_unique_directory(parent, prefix):
    parent = parent.expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    candidate = parent / prefix
    sequence = 1
    while candidate.exists():
        candidate = parent / "{0}-{1}".format(prefix, sequence)
        sequence += 1
    candidate.mkdir()
    return candidate


def create_apply_log_directory(args, backup_bundle):
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.log_dir is not None:
        parent = args.log_dir
    elif backup_bundle is not None:
        parent = backup_bundle / "logs"
    else:
        parent = Path("plex_nfkd_logs")
    return make_unique_directory(parent, "apply-run-{0}".format(timestamp))


class ApplyRunLogger:
    def __init__(self, run_dir, args, identity, stats, backup_bundle, token):
        self.run_dir = run_dir.resolve()
        self.responses_dir = self.run_dir / "responses"
        self.responses_dir.mkdir()
        self.secrets = (token,)
        self.started_at = utc_now()
        self.failure_counts = Counter()
        self.failure_samples = []
        self.run_log_path = self.run_dir / "run.log"
        self.failures_path = self.run_dir / "failures.csv"
        self.summary_path = self.run_dir / "summary.json"
        self.run_handle = self.run_log_path.open("w", encoding="utf-8", buffering=1)
        self.failure_handle = self.failures_path.open("w", encoding="utf-8-sig", newline="")
        self.failure_writer = csv.DictWriter(
            self.failure_handle,
            fieldnames=FAILURE_FIELDS,
            extrasaction="ignore",
        )
        self.failure_writer.writeheader()
        self.base_summary = {
            "program": PROGRAM,
            "version": VERSION,
            "mode": "apply",
            "started_at_utc": self.started_at.isoformat(),
            "plex_url": redact_text(args.plex_url, self.secrets),
            "server_version": identity.get("version"),
            "machine_identifier": identity.get("machine_identifier"),
            "backup": str(backup_bundle) if backup_bundle else None,
            "scan_stats": dict(stats),
            "log_directory": str(self.run_dir),
        }
        self.event(
            "INFO",
            "RUN_START",
            "Apply run started; candidates={0}, backup={1}".format(
                stats["candidates"], backup_bundle or "(disabled)"
            ),
        )

    def event(self, level, stage, message, item=None):
        timestamp = utc_now().isoformat()
        metadata_id = ""
        if item is not None:
            metadata_id = str(item.get("metadata_id") or "")
        line = "{0} {1:<5} {2:<20} id={3} {4}\n".format(
            timestamp,
            level,
            stage,
            metadata_id or "-",
            redact_text(message, self.secrets),
        )
        self.run_handle.write(line)

    def put_succeeded(self, item):
        self.event(
            "INFO",
            "APPLY_PUT",
            "PUT succeeded; action={0}; expected_title_sort={1!r}".format(
                item.get("action"), item.get("target_title_sort")
            ),
            item,
        )

    def verified(self, item, diagnostics):
        if diagnostics.get("title_sort_omitted_as_redundant"):
            message = (
                "Verification succeeded through Plex title fallback; titleSort was omitted as "
                "redundant; effective_title_sort={0!r}; actual_locked={1}"
            ).format(
                diagnostics.get("effective_title_sort"), diagnostics.get("actual_locked")
            )
        else:
            message = "Verification succeeded; actual_title_sort={0!r}; actual_locked={1}".format(
                diagnostics.get("actual_title_sort"), diagnostics.get("actual_locked")
            )
        self.event(
            "INFO",
            "VERIFY",
            message,
            item,
        )

    def _write_response(self, item, element):
        if element is None:
            return ""
        metadata_id = "".join(
            character if character.isalnum() or character in ("-", "_") else "_"
            for character in str(item.get("metadata_id") or "unknown")
        )
        filename = "{0}-{1}.xml".format(item.get("api_kind") or "metadata", metadata_id)
        path = self.responses_dir / filename
        xml_text = ET.tostring(element, encoding="unicode")
        path.write_text(redact_text(xml_text, self.secrets) + "\n", encoding="utf-8")
        return str(path.relative_to(self.run_dir))

    def failed(self, item, stage, error_code, error, diagnostics=None, element=None):
        diagnostics = diagnostics or {}
        response_file = self._write_response(item, element)
        returned_fields = diagnostics.get("returned_fields") or []
        if not isinstance(returned_fields, str):
            returned_fields = json.dumps(returned_fields, ensure_ascii=False, separators=(",", ":"))
        row = {
            "timestamp_utc": utc_now().isoformat(),
            "stage": stage,
            "error_code": error_code,
            "metadata_id": item.get("metadata_id"),
            "library_section_id": item.get("library_section_id"),
            "library_name": item.get("library_name"),
            "metadata_type": item.get("metadata_type"),
            "metadata_type_name": item.get("metadata_type_name"),
            "api_kind": item.get("api_kind"),
            "action": item.get("action"),
            "title": item.get("title"),
            "current_title_sort": item.get("title_sort"),
            "expected_title_sort": item.get("target_title_sort"),
            "actual_title": diagnostics.get("actual_title"),
            "actual_title_sort": diagnostics.get("actual_title_sort"),
            "expected_locked": True,
            "actual_locked": diagnostics.get("actual_locked"),
            "returned_fields": returned_fields,
            "error": redact_text(error, self.secrets),
            "response_file": response_file,
        }
        self.failure_writer.writerow(row)
        self.failure_handle.flush()
        self.failure_counts[error_code] += 1
        if len(self.failure_samples) < 5:
            self.failure_samples.append(
                {
                    "metadata_id": item.get("metadata_id"),
                    "title": item.get("title"),
                    "error_code": error_code,
                    "error": redact_text(error, self.secrets),
                    "expected_title_sort": item.get("target_title_sort"),
                    "actual_title_sort": diagnostics.get("actual_title_sort"),
                }
            )
        self.event(
            "ERROR",
            stage,
            (
                "{0}: {1}; expected_title_sort={2!r}; actual_title_sort={3!r}; "
                "expected_locked=true; actual_locked={4}; response={5}"
            ).format(
                error_code,
                error,
                item.get("target_title_sort"),
                diagnostics.get("actual_title_sort"),
                diagnostics.get("actual_locked"),
                response_file or "(not available)",
            ),
            item,
        )

    def finish(self, apply_stats):
        finished_at = utc_now()
        summary = dict(self.base_summary)
        summary.update(
            {
                "finished_at_utc": finished_at.isoformat(),
                "duration_seconds": round((finished_at - self.started_at).total_seconds(), 3),
                "apply_stats": dict(apply_stats),
                "failure_reasons": dict(sorted(self.failure_counts.items())),
                "failure_samples": self.failure_samples,
                "files": {
                    "run_log": str(self.run_log_path),
                    "failures_csv": str(self.failures_path),
                    "responses_directory": str(self.responses_dir),
                },
            }
        )
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.event(
            "INFO",
            "RUN_END",
            "Apply run finished; PUT succeeded={0}, verified={1}, failed={2}".format(
                apply_stats["put_succeeded"], apply_stats["verified"], apply_stats["failed"]
            ),
        )
        self.failure_handle.close()
        self.run_handle.close()


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def configure_output_encoding():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass


def terminal_width():
    return max(72, min(140, shutil.get_terminal_size(fallback=(100, 24)).columns))


def display_width(value):
    width = 0
    for character in str(value):
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
    return width


def truncate_display(value, width):
    value = str(value)
    if display_width(value) <= width:
        return value
    if width <= 3:
        return "." * width
    result = []
    used = 0
    target = width - 3
    for character in value:
        character_width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        )
        if used + character_width > target:
            break
        result.append(character)
        used += character_width
    return "".join(result) + "..."


def pad_display(value, width):
    value = truncate_display(value, width)
    return value + " " * max(0, width - display_width(value))


ANSI_RESET = "\033[0m"
STYLE_BOLD = "1"
STYLE_DIM = "2"
STYLE_RED = "31"
STYLE_YELLOW = "33"
STYLE_CYAN = "36"
STYLE_BOLD_RED = "1;31"
STYLE_BOLD_GREEN = "1;32"
STYLE_BOLD_YELLOW = "1;33"
STYLE_BOLD_CYAN = "1;36"


def color_enabled(mode, stream, output_format="text", environ=None):
    if output_format == "json":
        return False
    if mode == "never":
        return False
    if mode == "always":
        return True
    environment = os.environ if environ is None else environ
    try:
        is_terminal = bool(stream.isatty())
    except Exception:
        is_terminal = False
    return (
        is_terminal
        and environment.get("TERM", "").lower() != "dumb"
        and "NO_COLOR" not in environment
    )


def style_text(text, style, enabled):
    if not enabled or not style:
        return str(text)
    return "\033[{0}m{1}{2}".format(style, text, ANSI_RESET)


def result_style(result):
    if result in ("NO CHANGES REQUIRED", "APPLY SUCCEEDED", "RESTORE SUCCEEDED"):
        return STYLE_BOLD_GREEN
    if result in ("CHANGES PLANNED", "APPLY PARTIALLY FAILED"):
        return STYLE_BOLD_YELLOW
    if result in ("APPLY FAILED", "ERROR"):
        return STYLE_BOLD_RED
    return STYLE_BOLD_CYAN


def result_line(result, enabled):
    return style_text("RESULT: {0}".format(result), result_style(result), enabled)


class ProgressReporter:
    def __init__(self, enabled=True, color=False):
        self.enabled = enabled
        self.color = color
        self.started_at = time.monotonic()
        self.last_tty_length = 0

    def _elapsed(self):
        return time.monotonic() - self.started_at

    def line(self, message, transient=False, style=None):
        if not self.enabled:
            return
        plain_text = "{0}  ({1:.1f}s)".format(message, self._elapsed())
        if transient and sys.stderr.isatty():
            width = terminal_width()
            plain_text = truncate_display(plain_text, width - 1)
            padding = " " * max(0, self.last_tty_length - display_width(plain_text))
            rendered = style_text(plain_text, style, self.color)
            print("\r{0}{1}".format(rendered, padding), end="", file=sys.stderr, flush=True)
            self.last_tty_length = display_width(plain_text)
        else:
            self.finish_transient()
            print(style_text(plain_text, style, self.color), file=sys.stderr, flush=True)

    def phase(self, current, total, message):
        self.line("[{0}/{1}] {2}".format(current, total, message), style=STYLE_BOLD_CYAN)

    def update(self, current, total, message):
        self.line(
            "{0}: {1:,}/{2:,}".format(message, current, total),
            transient=True,
            style=STYLE_CYAN,
        )

    def count(self, current, message):
        self.line("{0}: {1:,}".format(message, current), transient=True, style=STYLE_CYAN)

    def detail(self, message):
        self.line("      {0}".format(message), style=STYLE_DIM)

    def finish_transient(self):
        if self.enabled and self.last_tty_length and sys.stderr.isatty():
            print(file=sys.stderr, flush=True)
        self.last_tty_length = 0


def emit_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def emit_error(args, exit_code, message):
    stderr_color = getattr(args, "stderr_color", False)
    stdout_color = getattr(args, "stdout_color", False)
    print(style_text("ERROR: {0}".format(message), STYLE_BOLD_RED, stderr_color), file=sys.stderr)
    if getattr(args, "output_format", "text") == "json":
        emit_json(
            {
                "program": PROGRAM,
                "version": VERSION,
                "result": "ERROR",
                "exit_code": exit_code,
                "error": str(message),
            }
        )
    else:
        print("\n{0}".format(result_line("ERROR", stdout_color)))
        print("Exit code: {0}".format(exit_code))
    return exit_code


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Read and edit metadata only through the Plex API, set a Plex-aware NFKD "
            "titleSort, and lock the Sort Title field. Before --apply, the two database "
            "files are backed up without being used as a metadata source. The default "
            "is a dry run limited to titles containing Korean characters."
        )
    )
    parser.add_argument(
        "--plex-url",
        default=DEFAULT_PLEX_URL,
        help="Plex Media Server URL (default: %(default)s)",
    )
    token_group = parser.add_argument_group("token")
    token_group.add_argument("--token", help="Plex token (not recommended because shell history may expose it)")
    token_group.add_argument("--token-file", type=Path, help="file containing the token on one line")
    token_group.add_argument(
        "--preferences",
        action="append",
        type=Path,
        help="Preferences.xml path; may be specified more than once",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show the plan without making changes (default)")
    mode.add_argument("--apply", action="store_true", help="apply through the Plex API and verify the result")
    mode.add_argument(
        "--restore-backup",
        type=Path,
        metavar="BACKUP_DIR",
        help="restore a backup bundle; Plex must be stopped and --confirm-plex-stopped is required",
    )

    parser.add_argument("--section", type=int, help="scan only this library_section_id")
    parser.add_argument(
        "--metadata-type",
        type=int,
        help="scan only this Plex metadata type number (for example, Movie=1 or Episode=4)",
    )
    parser.add_argument(
        "--all-unicode",
        "--all-titles",
        dest="all_titles",
        action="store_true",
        help="force every non-empty title to use NFKD sorting, without the Korean-only limit (use caution)",
    )
    parser.add_argument(
        "--article-strings",
        help=(
            "comma-separated grammatical articles to remove from derived sort keys; "
            "defaults to Preferences.xml ArticleStrings or Plex defaults"
        ),
    )
    parser.add_argument("--no-playlists", action="store_true", help="do not scan Playlist items (type 15)")
    parser.add_argument("--csv", type=Path, help="write all candidates and results to a UTF-8 BOM CSV file")
    parser.add_argument(
        "--console",
        action="store_true",
        help="print candidate details to the console; CSV is not required",
    )
    parser.add_argument(
        "--console-limit",
        type=int,
        default=0,
        help="maximum rows for --console; 0 means all rows (default: 0)",
    )
    parser.add_argument("--show-codepoints", action="store_true", help="show Unicode code points in the preview")
    parser.add_argument("--preview-limit", type=int, default=20, help="maximum terminal preview rows (default: 20)")
    parser.add_argument("--page-size", type=int, default=500, help="API listing page size (default: 500)")
    parser.add_argument("--detail-batch-size", type=int, default=50, help="detail/verification batch size (default: 50)")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds per request (default: 30)")
    parser.add_argument(
        "--quiet-progress",
        action="store_true",
        help="hide phase, scan, backup, and apply progress messages",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show detailed server, token-source, and sort-rule configuration",
    )
    parser.add_argument(
        "--output-format",
        choices=("text", "json"),
        default="text",
        help="terminal output format (default: text); progress remains on stderr",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help=(
            "ANSI color mode (default: auto); auto uses color only on a TTY and "
            "honors NO_COLOR and TERM=dumb; JSON always disables color"
        ),
    )

    parser.add_argument(
        "--log-dir",
        type=Path,
        help=(
            "parent directory for per-run apply logs; by default logs are stored under "
            "the backup bundle, or ./plex_nfkd_logs when --no-backup is used"
        ),
    )

    backup_group = parser.add_argument_group("backup and restore")
    backup_group.add_argument(
        "--db-dir",
        type=Path,
        help="directory containing the two Plex database files; normally auto-detected",
    )
    backup_group.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("plex_nfkd_backups"),
        help="parent directory for automatic pre-apply backups (default: ./plex_nfkd_backups)",
    )
    backup_group.add_argument(
        "--no-backup",
        action="store_true",
        help="apply without a database backup (explicit opt-out; not recommended)",
    )
    backup_group.add_argument(
        "--confirm-plex-stopped",
        action="store_true",
        help="required for --restore-backup after stopping Plex Media Server",
    )
    args = parser.parse_args(argv)

    if args.page_size < 1 or args.page_size > 5000:
        parser.error("--page-size must be between 1 and 5000")
    if args.detail_batch_size < 1 or args.detail_batch_size > 200:
        parser.error("--detail-batch-size must be between 1 and 200")
    if args.preview_limit < 0:
        parser.error("--preview-limit must be zero or greater")
    if args.console_limit < 0:
        parser.error("--console-limit must be zero or greater")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.section is not None and args.section < 0:
        parser.error("--section must be zero or greater")
    if args.metadata_type is not None and args.metadata_type < 1:
        parser.error("--metadata-type must be one or greater")
    if args.restore_backup and not args.confirm_plex_stopped:
        parser.error("--restore-backup requires --confirm-plex-stopped")
    if args.restore_backup and args.no_backup:
        parser.error("--no-backup cannot be used with --restore-backup")
    if args.no_backup and not args.apply:
        parser.error("--no-backup is only valid with --apply")
    if args.confirm_plex_stopped and not args.restore_backup:
        parser.error("--confirm-plex-stopped is only valid with --restore-backup")
    if args.log_dir is not None and not args.apply:
        parser.error("--log-dir is only valid with --apply")
    return args


def read_plain_token(path):
    value = path.read_text(encoding="utf-8-sig").strip()
    if not value:
        raise ValueError("token file is empty: {0}".format(path))
    return value


def read_preferences_token(path):
    root = ET.parse(str(path)).getroot()
    token = (root.attrib.get("PlexOnlineToken") or "").strip()
    if not token:
        raise ValueError("PlexOnlineToken attribute is missing or empty")
    return token


def auto_preferences_paths(explicit_paths=None):
    candidates = []
    if explicit_paths:
        candidates.extend(Path(p) for p in explicit_paths)

    support_dir = os.environ.get("PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR")
    if support_dir:
        root = Path(support_dir)
        candidates.append(root / "Preferences.xml")
        candidates.append(root / "Plex Media Server" / "Preferences.xml")

    patterns = [
        "/volume*/PlexMediaServer/AppData/Plex Media Server/Preferences.xml",
        "/volume*/Plex/Library/Application Support/Plex Media Server/Preferences.xml",
    ]
    for pattern in patterns:
        candidates.extend(Path(p) for p in sorted(glob.glob(pattern)))

    candidates.extend(
        [
            Path("/var/packages/PlexMediaServer/home/Plex Media Server/Preferences.xml"),
            Path("/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml"),
            Path("/config/Library/Application Support/Plex Media Server/Preferences.xml"),
        ]
    )

    unique = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def load_preferences(explicit_paths=None):
    errors = []
    for path in auto_preferences_paths(explicit_paths):
        try:
            if not path.is_file():
                continue
            root = ET.parse(str(path)).getroot()
            return path.resolve(), dict(root.attrib), errors
        except (OSError, ET.ParseError) as exc:
            errors.append("{0}: {1}".format(path, exc))
    return None, {}, errors


def parse_article_strings(value):
    if value is None:
        return list(DEFAULT_ARTICLE_STRINGS)
    return [part.strip() for part in value.split(",") if part.strip()]


def resolve_article_strings(args, preferences_path, preferences_attrs):
    if args.article_strings is not None:
        return parse_article_strings(args.article_strings), "--article-strings"
    if "ArticleStrings" in preferences_attrs:
        return (
            parse_article_strings(preferences_attrs.get("ArticleStrings", "")),
            "Preferences.xml: {0}".format(preferences_path),
        )
    return list(DEFAULT_ARTICLE_STRINGS), "Plex defaults"


def resolve_token(args):
    if args.token:
        return args.token.strip(), "--token"
    if args.token_file:
        return read_plain_token(args.token_file), "token file: {0}".format(args.token_file)

    env_token = (os.environ.get("PLEX_TOKEN") or "").strip()
    if env_token:
        return env_token, "PLEX_TOKEN environment variable"

    attempted = []
    errors = []
    for path in auto_preferences_paths(args.preferences):
        attempted.append(str(path))
        try:
            if not path.is_file():
                continue
            return read_preferences_token(path), "Preferences.xml: {0}".format(path)
        except (OSError, ValueError, ET.ParseError) as exc:
            errors.append("{0}: {1}".format(path, exc))

    message = [
        "Unable to find a Plex token.",
        "Run under an account that can read Synology's Preferences.xml,",
        "or use --preferences, --token-file, or PLEX_TOKEN.",
    ]
    if errors:
        message.append("Read errors: " + " | ".join(errors))
    elif attempted:
        message.append("Default paths checked: " + " | ".join(attempted))
    raise RuntimeError("\n".join(message))


def resolve_db_dir(args, preferences_path=None):
    if args.db_dir:
        return args.db_dir.expanduser().resolve()
    candidates = []
    if preferences_path:
        candidates.append(preferences_path.parent / "Plug-in Support" / "Databases")
    for path in auto_preferences_paths(args.preferences):
        candidates.append(path.parent / "Plug-in Support" / "Databases")
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if (candidate / MAIN_DB_NAME).is_file() and (candidate / BLOBS_DB_NAME).is_file():
            return candidate.resolve()
    return None


def path_is_within(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def sqlite_read_only_uri(path):
    normalized = str(path.resolve()).replace("\\", "/")
    return "file:{0}?mode=ro".format(urllib.parse.quote(normalized, safe="/:"))


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def online_backup_database(source, destination):
    partial = destination.with_name(destination.name + ".partial")
    if destination.exists() or partial.exists():
        raise RuntimeError("backup destination already exists: {0}".format(destination))
    source_connection = sqlite3.connect(sqlite_read_only_uri(source), uri=True, timeout=60.0)
    destination_connection = sqlite3.connect(str(partial), timeout=60.0)
    try:
        source_connection.execute("PRAGMA query_only = ON")
        source_connection.backup(destination_connection, pages=1024, sleep=0.05)
    finally:
        destination_connection.close()
        source_connection.close()
    os.replace(str(partial), str(destination))
    check = sqlite3.connect(sqlite_read_only_uri(destination), uri=True, timeout=60.0)
    try:
        page_count = int(check.execute("PRAGMA page_count").fetchone()[0])
    finally:
        check.close()
    if page_count <= 0 or destination.stat().st_size <= 0:
        raise RuntimeError("backup verification failed: {0}".format(destination))
    source_stat = source.stat()
    return {
        "name": destination.name,
        "size": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "page_count": page_count,
        "source_mode": stat.S_IMODE(source_stat.st_mode),
        "source_uid": getattr(source_stat, "st_uid", None),
        "source_gid": getattr(source_stat, "st_gid", None),
    }


def create_backup_bundle(args, db_dir, identity, progress=None):
    if db_dir is None:
        raise RuntimeError(
            "Plex database directory was not found. Use --db-dir or a readable Preferences.xml."
        )
    db_dir = db_dir.resolve()
    sources = [db_dir / MAIN_DB_NAME, db_dir / BLOBS_DB_NAME]
    for source in sources:
        if not source.is_file():
            raise RuntimeError("required Plex database file was not found: {0}".format(source))

    backup_parent = args.backup_dir.expanduser().resolve()
    app_data_dir = db_dir.parent.parent
    if path_is_within(backup_parent, app_data_dir):
        raise RuntimeError(
            "backup directory must be outside the Plex Media Server data directory: {0}".format(
                backup_parent
            )
        )
    backup_parent.mkdir(parents=True, exist_ok=True)
    required_bytes = sum(source.stat().st_size for source in sources) + 64 * 1024 * 1024
    free_bytes = shutil.disk_usage(str(backup_parent)).free
    if free_bytes < required_bytes:
        raise RuntimeError(
            "insufficient free space for backup: required at least {0:,} bytes, available {1:,} bytes".format(
                required_bytes, free_bytes
            )
        )

    timestamp = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    bundle = backup_parent / "plex-db-backup-{0}".format(timestamp)
    sequence = 1
    while bundle.exists():
        sequence += 1
        bundle = backup_parent / "plex-db-backup-{0}-{1}".format(timestamp, sequence)
    bundle.mkdir()

    manifest = {
        "format": 1,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_db_dir": str(db_dir),
        "plex_url": args.plex_url,
        "server_machine_identifier": identity.get("machine_identifier") or "",
        "server_version": identity.get("version") or "",
        "backup_method": "Python sqlite3 online backup API",
        "files": [],
    }
    try:
        for source in sources:
            if progress:
                progress.detail("Backing up {0}".format(source.name))
            else:
                print("Backing up: {0}".format(source))
            manifest["files"].append(online_backup_database(source, bundle / source.name))
        manifest_path = bundle / BACKUP_MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        print("Incomplete backup bundle retained for inspection: {0}".format(bundle), file=sys.stderr)
        raise
    return bundle, manifest


def load_backup_manifest(backup_path):
    backup_path = backup_path.expanduser().resolve()
    manifest_path = backup_path / BACKUP_MANIFEST_NAME if backup_path.is_dir() else backup_path
    if not manifest_path.is_file():
        raise RuntimeError("backup manifest was not found: {0}".format(manifest_path))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("unable to read backup manifest: {0}".format(exc)) from exc
    if manifest.get("format") != 1:
        raise RuntimeError("unsupported backup manifest format")
    files = {entry.get("name"): entry for entry in manifest.get("files", [])}
    if set(files) != {MAIN_DB_NAME, BLOBS_DB_NAME}:
        raise RuntimeError("backup manifest does not contain both required Plex databases")
    bundle = manifest_path.parent
    for name, entry in files.items():
        source = bundle / name
        if not source.is_file():
            raise RuntimeError("backup file is missing: {0}".format(source))
        actual_size = source.stat().st_size
        actual_hash = sha256_file(source)
        if actual_size != int(entry.get("size", -1)) or actual_hash != entry.get("sha256"):
            raise RuntimeError("backup file checksum validation failed: {0}".format(source))
    return bundle, manifest


def plex_server_is_reachable(base_url, timeout=3.0):
    url = base_url.rstrip("/") + "/identity"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def preserve_file_metadata(path, previous_stat, backup_entry):
    mode = (
        stat.S_IMODE(previous_stat.st_mode)
        if previous_stat is not None
        else int(backup_entry.get("source_mode", 0o600))
    )
    os.chmod(str(path), mode)
    if hasattr(os, "chown"):
        uid = (
            previous_stat.st_uid
            if previous_stat is not None
            else backup_entry.get("source_uid")
        )
        gid = (
            previous_stat.st_gid
            if previous_stat is not None
            else backup_entry.get("source_gid")
        )
        if uid is None or gid is None:
            return
        try:
            os.chown(str(path), int(uid), int(gid))
        except PermissionError:
            print("Warning: unable to restore file ownership for {0}".format(path), file=sys.stderr)


def restore_backup_bundle(args, preferences_path=None, progress=None):
    if plex_server_is_reachable(args.plex_url, timeout=min(args.timeout, 3.0)):
        raise RuntimeError(
            "Plex Media Server is still reachable. Stop it completely before restoring a database."
        )
    bundle, manifest = load_backup_manifest(args.restore_backup)
    manifest_files = {entry["name"]: entry for entry in manifest["files"]}
    db_dir = resolve_db_dir(args, preferences_path)
    if db_dir is None:
        source_dir = manifest.get("source_db_dir")
        db_dir = Path(source_dir).resolve() if source_dir else None
    if db_dir is None or not db_dir.is_dir():
        raise RuntimeError("target database directory was not found; use --db-dir")
    if path_is_within(bundle, db_dir.parent.parent):
        raise RuntimeError("the backup bundle must be outside the Plex Media Server data directory")

    timestamp = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    safety_dir = bundle.parent / "pre-restore-current-db-{0}".format(timestamp)
    sequence = 1
    while safety_dir.exists():
        sequence += 1
        safety_dir = bundle.parent / "pre-restore-current-db-{0}-{1}".format(timestamp, sequence)
    safety_dir.mkdir(parents=True)

    names = (MAIN_DB_NAME, BLOBS_DB_NAME)
    sidecar_names = tuple(name + suffix for name in names for suffix in ("-wal", "-shm"))
    backup_bytes = sum((bundle / name).stat().st_size for name in names)
    current_bytes = sum(
        (db_dir / name).stat().st_size
        for name in names + sidecar_names
        if (db_dir / name).exists()
    )
    if shutil.disk_usage(str(db_dir)).free < backup_bytes + 32 * 1024 * 1024:
        raise RuntimeError("insufficient free space in the target database directory for restore staging")
    if shutil.disk_usage(str(safety_dir)).free < current_bytes + 32 * 1024 * 1024:
        raise RuntimeError("insufficient free space for the pre-restore safety copy")
    previous_stats = {}
    staged = []
    if progress:
        progress.detail("Preserving current database files in {0}".format(safety_dir))
    else:
        print("Preserving current database files in: {0}".format(safety_dir))
    for name in names + sidecar_names:
        current = db_dir / name
        if current.exists():
            shutil.copy2(str(current), str(safety_dir / name))
        if name in names:
            previous_stats[name] = current.stat() if current.exists() else None

    try:
        for name in names:
            source = bundle / name
            stage = db_dir / (name + ".restore-partial")
            if stage.exists():
                raise RuntimeError("restore staging file already exists: {0}".format(stage))
            shutil.copy2(str(source), str(stage))
            if sha256_file(stage) != sha256_file(source):
                raise RuntimeError("restore staging checksum failed: {0}".format(stage))
            with stage.open("rb+") as stage_handle:
                os.fsync(stage_handle.fileno())
            staged.append((name, stage))
        for name, stage in staged:
            target = db_dir / name
            os.replace(str(stage), str(target))
            preserve_file_metadata(target, previous_stats.get(name), manifest_files[name])
        for name in sidecar_names:
            sidecar = db_dir / name
            if sidecar.exists():
                sidecar.unlink()
    except Exception:
        print(
            "Restore failed. The pre-restore database copies are available at: {0}".format(safety_dir),
            file=sys.stderr,
        )
        raise
    return db_dir, bundle, safety_dir


def contains_korean(text):
    if not text:
        return False
    for ch in text:
        code = ord(ch)
        if (
            0x1100 <= code <= 0x11FF
            or 0x3130 <= code <= 0x318F
            or 0xA960 <= code <= 0xA97F
            or 0xAC00 <= code <= 0xD7A3
            or 0xD7B0 <= code <= 0xD7FF
        ):
            return True
    return False


def is_sortable_character(character):
    return unicodedata.category(character)[0] in ("L", "N")


def strip_leading_non_sort_characters(value):
    index = 0
    while index < len(value) and not is_sortable_character(value[index]):
        index += 1
    if index == len(value):
        return value, ""
    return value[index:], value[:index]


def derive_plex_sort_base(title, article_strings):
    candidate, _ = strip_leading_non_sort_characters(title)
    for article in sorted(article_strings, key=len, reverse=True):
        article_length = len(article)
        if len(candidate) <= article_length:
            continue
        if candidate[:article_length].casefold() != article.casefold():
            continue
        if not candidate[article_length].isspace():
            continue
        remainder = candidate[article_length:].lstrip()
        remainder, _ = strip_leading_non_sort_characters(remainder)
        if remainder:
            candidate = remainder
        break
    if not candidate:
        candidate = title
    removed_length = len(title) - len(candidate)
    removed_prefix = title[:removed_length] if removed_length > 0 else ""
    return candidate, removed_prefix


def make_sort_key_plan(title, current_title_sort, article_strings):
    if current_title_sort not in (None, ""):
        sort_base = current_title_sort
        source = "EXISTING_TITLE_SORT"
        if title.endswith(sort_base):
            removed_prefix = title[: len(title) - len(sort_base)]
        else:
            removed_prefix = ""
    else:
        sort_base, removed_prefix = derive_plex_sort_base(title, article_strings)
        source = "DERIVED_PLEX_RULES"
    return {
        "sort_base": sort_base,
        "target_title_sort": unicodedata.normalize("NFKD", sort_base),
        "sort_key_source": source,
        "removed_prefix": removed_prefix,
    }


def effective_title_sort_matches(title, current_title_sort, target_title_sort):
    return current_title_sort == target_title_sort or (
        current_title_sort is None and title == target_title_sort
    )


def codepoints(text):
    if text is None:
        return "(NULL)"
    if text == "":
        return "(EMPTY)"
    return " ".join("U+{0:04X}".format(ord(ch)) for ch in text)


def bool_text(value):
    return "true" if value else "false"


def int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def item_from_element(element, section=None, queried_type=None, api_kind="library"):
    attrs = dict(element.attrib)
    metadata_id = attrs.get("ratingKey")
    if not metadata_id:
        return None

    type_id = queried_type
    element_type = (attrs.get("type") or "").lower()
    if type_id is None:
        type_id = TYPE_IDS.get(element_type)
    if api_kind == "playlist":
        type_id = 15

    item = {
        "metadata_id": str(metadata_id),
        "library_section_id": "",
        "library_name": "(Playlists)" if api_kind == "playlist" else "",
        "metadata_type": type_id,
        "metadata_type_name": TYPE_NAMES.get(type_id, element_type or "Unknown"),
        "api_kind": api_kind,
        "title": attrs.get("title"),
        "title_sort": attrs.get("titleSort"),
        "parent_id": attrs.get("parentRatingKey") or "",
        "parent_title": attrs.get("parentTitle") or "",
        "grandparent_id": attrs.get("grandparentRatingKey") or "",
        "grandparent_title": attrs.get("grandparentTitle") or "",
        "index": attrs.get("index") or "",
        "parent_index": attrs.get("parentIndex") or "",
        "title_sort_locked": any(
            child.attrib.get("name") == "titleSort" and child.attrib.get("locked") == "1"
            for child in element
            if local_name(child.tag) == "Field"
        ),
    }
    if section:
        item["library_section_id"] = str(section["id"])
        item["library_name"] = section["title"]
    else:
        item["library_section_id"] = attrs.get("librarySectionID") or ""
        item["library_name"] = attrs.get("librarySectionTitle") or item["library_name"]
    return item


def merge_detail(item, detail_element):
    attrs = detail_element.attrib
    for source, target in (
        ("title", "title"),
        ("titleSort", "title_sort"),
        ("parentRatingKey", "parent_id"),
        ("parentTitle", "parent_title"),
        ("grandparentRatingKey", "grandparent_id"),
        ("grandparentTitle", "grandparent_title"),
        ("index", "index"),
        ("parentIndex", "parent_index"),
    ):
        if source in attrs:
            item[target] = attrs.get(source)
    if not item.get("library_section_id") and attrs.get("librarySectionID"):
        item["library_section_id"] = attrs["librarySectionID"]
    if (not item.get("library_name") or item.get("library_name") == "(Playlists)") and attrs.get("librarySectionTitle"):
        item["library_name"] = attrs["librarySectionTitle"]
    item["title_sort_locked"] = any(
        child.attrib.get("name") == "titleSort" and child.attrib.get("locked") == "1"
        for child in detail_element
        if local_name(child.tag) == "Field"
    )
    return item


def direct_metadata_elements(root):
    for child in root:
        if child.attrib.get("ratingKey"):
            yield child


def get_identity(client):
    root, _ = client.get("/identity")
    return {
        "machine_identifier": root.attrib.get("machineIdentifier") or "",
        "version": root.attrib.get("version") or "",
        "claimed": root.attrib.get("claimed") or "",
    }


def get_sections(client):
    root, _ = client.get("/library/sections")
    sections = []
    for element in root:
        if local_name(element.tag) != "Directory":
            continue
        section_id = int_or_none(element.attrib.get("key"))
        if section_id is None:
            continue
        sections.append(
            {
                "id": section_id,
                "title": element.attrib.get("title") or "Section {0}".format(section_id),
                "type": (element.attrib.get("type") or "").lower(),
                "uuid": element.attrib.get("uuid") or "",
            }
        )
    return sections


def discover_section_types(client, section):
    found = set()
    try:
        root, _ = client.get(
            "/library/sections/{0}".format(section["id"]),
            params={"includeDetails": 1},
        )
    except PlexApiError:
        return found
    for element in root.iter():
        value = int_or_none(element.attrib.get("type"))
        key = element.attrib.get("key") or ""
        if value is not None and ("/all" in key or "all?" in key):
            found.add(value)
    return found


def section_type_plan(client, section, requested_type=None):
    if requested_type is not None:
        # A type filter can be requested without --section.  Mark it as an
        # optional probe so an unrelated section returning 400/404 does not
        # prevent the same type from being scanned in the correct section.
        return [requested_type], {requested_type}
    required, optional = SECTION_TYPE_PLAN.get(section["type"], ([], []))
    discovered = discover_section_types(client, section)
    if not required and discovered:
        required = sorted(discovered)
    all_types = []
    for value in list(required) + sorted(discovered) + list(optional):
        if value not in all_types and value != 15:
            all_types.append(value)
    return all_types, set(optional)


def iter_paged_elements(client, path, params, page_size):
    start = 0
    while True:
        headers = {
            "X-Plex-Container-Start": str(start),
            "X-Plex-Container-Size": str(page_size),
        }
        root, response_headers = client.get(path, params=params, extra_headers=headers)
        elements = list(direct_metadata_elements(root))
        body_size = int_or_none(root.attrib.get("size"))
        page_count = body_size if body_size is not None else len(elements)
        total = int_or_none(root.attrib.get("totalSize"))
        if total is None:
            total = int_or_none(response_headers.get("X-Plex-Container-Total-Size"))
        offset = int_or_none(root.attrib.get("offset"))
        if offset is None:
            offset = int_or_none(response_headers.get("X-Plex-Container-Start"))
        if offset is None:
            offset = start

        yield elements

        if page_count <= 0:
            break
        next_start = offset + page_count
        if total is not None and next_start >= total:
            break
        if total is None and page_count < page_size:
            break
        if next_start <= start:
            raise RuntimeError("API pagination did not advance: {0}".format(path))
        start = next_start


def chunks(items, size):
    chunk = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def fetch_library_details(client, items):
    if not items:
        return {}
    ids = ",".join(item["metadata_id"] for item in items)
    root, _ = client.get("/library/metadata/{0}".format(ids), params={"includeMeta": 1})
    return {element.attrib["ratingKey"]: element for element in direct_metadata_elements(root)}


def fetch_one_library_detail(client, metadata_id):
    root, _ = client.get("/library/metadata/{0}".format(metadata_id), params={"includeMeta": 1})
    return next(direct_metadata_elements(root), None)


def fetch_playlist_detail(client, metadata_id):
    root, _ = client.get("/playlists/{0}".format(metadata_id), params={"includeMeta": 1})
    return next(direct_metadata_elements(root), None)


def enrich_library_items(client, items):
    details = fetch_library_details(client, items)
    missing = []
    for item in items:
        detail = details.get(item["metadata_id"])
        if detail is None:
            missing.append(item)
        else:
            merge_detail(item, detail)
    for item in missing:
        detail = fetch_one_library_detail(client, item["metadata_id"])
        if detail is None:
            raise RuntimeError("metadata detail was not found: {0}".format(item["metadata_id"]))
        merge_detail(item, detail)
    return items


def enrich_playlist_item(client, item):
    detail = fetch_playlist_detail(client, item["metadata_id"])
    if detail is None:
        raise RuntimeError("playlist detail was not found: {0}".format(item["metadata_id"]))
    return merge_detail(item, detail)


def selected_by_scope(item, all_titles):
    title = item.get("title")
    if title is None or title == "":
        return False
    return all_titles or contains_korean(title)


def plan_candidate(item, article_strings):
    title = item.get("title")
    title_sort = item.get("title_sort")
    nfkd_title = unicodedata.normalize("NFKD", title)
    sort_plan = make_sort_key_plan(title, title_sort, article_strings)
    target_title_sort = sort_plan["target_title_sort"]
    effective_match = effective_title_sort_matches(title, title_sort, target_title_sort)
    value_change = not effective_match
    lock_change = not bool(item.get("title_sort_locked"))
    if not value_change and not lock_change:
        return None

    if title_sort is None and effective_match:
        status = "TITLE_SORT_OMITTED_EQUIVALENT"
    elif title_sort is None:
        status = "TITLE_SORT_NULL"
    elif title_sort == "":
        status = "TITLE_SORT_EMPTY"
    elif title_sort == target_title_sort:
        status = "MATCH"
    else:
        status = "MISMATCH"

    if value_change and lock_change:
        action = "SET_VALUE_AND_LOCK"
    elif value_change:
        action = "SET_VALUE"
    else:
        action = "LOCK_ONLY"

    type_id = item.get("metadata_type")
    item = dict(item)
    item.update(
        {
            "nfkd_title": nfkd_title,
            "sort_base": sort_plan["sort_base"],
            "target_title_sort": target_title_sort,
            "sort_key_source": sort_plan["sort_key_source"],
            "removed_prefix": sort_plan["removed_prefix"],
            "title_is_nfkd": title == nfkd_title,
            "title_sort_matches_nfkd": title_sort == nfkd_title,
            "title_sort_matches_target": title_sort == target_title_sort,
            "effective_title_sort_matches_target": effective_match,
            "title_sort_omitted_as_redundant": title_sort is None and effective_match,
            "title_sort_status": status,
            "contains_korean": contains_korean(title),
            "action": action,
            "metadata_type_name": TYPE_NAMES.get(type_id, item.get("metadata_type_name") or "Unknown"),
            "show_title": "",
            "season_index": "",
            "episode_index": "",
            "result": "PENDING",
            "error": "",
        }
    )
    if type_id == 3:
        item["show_title"] = item.get("parent_title") or ""
        item["season_index"] = item.get("index") or ""
    elif type_id == 4:
        item["show_title"] = item.get("grandparent_title") or ""
        item["season_index"] = item.get("parent_index") or ""
        item["episode_index"] = item.get("index") or ""
    return item


def csv_row(item):
    row = {}
    for key in CSV_FIELDS:
        value = item.get(key, "")
        if isinstance(value, bool):
            value = bool_text(value)
        if value is None:
            value = ""
        row[key] = value
    return row


def update_stats_for_item(stats, item):
    stats["total_api_rows"] += 1
    title = item.get("title")
    if title is None:
        stats["title_null"] += 1
    elif title == "":
        stats["title_empty"] += 1
    else:
        stats["title_present"] += 1


def update_stats_for_candidate(stats, candidate, by_library, by_type):
    stats["candidates"] += 1
    stats["action:{0}".format(candidate["action"])] += 1
    stats["status:{0}".format(candidate["title_sort_status"])] += 1
    stats["source:{0}".format(candidate["sort_key_source"])] += 1
    if candidate["action"] in ("SET_VALUE", "SET_VALUE_AND_LOCK"):
        stats["value_changes"] += 1
    if candidate["action"] in ("LOCK_ONLY", "SET_VALUE_AND_LOCK"):
        stats["lock_adds"] += 1
    if candidate["title_sort_status"] in (
        "TITLE_SORT_NULL",
        "TITLE_SORT_OMITTED_EQUIVALENT",
    ):
        stats["candidate_title_sort_null"] += 1
    if candidate["title_sort_status"] == "TITLE_SORT_EMPTY":
        stats["candidate_title_sort_empty"] += 1
    library_stats = by_library[candidate.get("library_name") or "(unknown)"]
    type_stats = by_type[candidate.get("metadata_type_name") or "Unknown"]
    for group in (library_stats, type_stats):
        group["candidates"] += 1
        group["value_changes"] += int(candidate["action"] in ("SET_VALUE", "SET_VALUE_AND_LOCK"))
        group["lock_adds"] += int(candidate["action"] in ("LOCK_ONLY", "SET_VALUE_AND_LOCK"))


def update_stats_for_compliant_item(stats, item, article_strings):
    sort_plan = make_sort_key_plan(item["title"], item.get("title_sort"), article_strings)
    target_title_sort = sort_plan["target_title_sort"]
    stats["already_correct_locked"] += 1
    if item.get("title_sort") == target_title_sort:
        stats["already_correct_explicit"] += 1
    elif item.get("title_sort") is None and item.get("title") == target_title_sort:
        stats["already_correct_title_fallback"] += 1
    else:
        stats["already_correct_other"] += 1


def write_spool(spool, candidate):
    spool.write(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_spool(path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def scan_api(client, args, sections, spool_path, article_strings, progress=None):
    stats = Counter()
    by_library = defaultdict(Counter)
    by_type = defaultdict(Counter)
    warnings = []
    seen = set()

    with spool_path.open("w", encoding="utf-8", newline="\n") as spool:
        for section in sections:
            if args.metadata_type == 15:
                continue
            if args.section is not None and section["id"] != args.section:
                continue
            types, optional_types = section_type_plan(client, section, args.metadata_type)
            if not types:
                warnings.append(
                    "Skipped section {0} ({1}): unable to determine its metadata types".format(
                        section["id"], section["title"]
                    )
                )
                continue
            for type_id in types:
                if progress:
                    progress.detail(
                        "Section {0} / {1} / type {2} ({3})".format(
                            section["id"], section["title"], type_id, TYPE_NAMES.get(type_id, "Unknown")
                        )
                    )
                try:
                    pages = iter_paged_elements(
                        client,
                        "/library/sections/{0}/all".format(section["id"]),
                        {"type": type_id, "includeExternalMedia": 1},
                        args.page_size,
                    )
                    for elements in pages:
                        selected = []
                        for element in elements:
                            item = item_from_element(element, section=section, queried_type=type_id)
                            if item is None:
                                continue
                            seen_key = ("library", item["metadata_id"])
                            if seen_key in seen:
                                stats["duplicates"] += 1
                                continue
                            seen.add(seen_key)
                            update_stats_for_item(stats, item)
                            by_library[section["title"]]["scanned"] += 1
                            by_type[item["metadata_type_name"]]["scanned"] += 1
                            if selected_by_scope(item, args.all_titles):
                                selected.append(item)
                        for batch in chunks(selected, args.detail_batch_size):
                            enrich_library_items(client, batch)
                            for item in batch:
                                stats["scope_rows"] += 1
                                candidate = plan_candidate(item, article_strings)
                                if candidate is None:
                                    update_stats_for_compliant_item(stats, item, article_strings)
                                    continue
                                update_stats_for_candidate(stats, candidate, by_library, by_type)
                                write_spool(spool, candidate)
                        if progress:
                            progress.count(stats["total_api_rows"], "Scanning metadata")
                except PlexApiError as exc:
                    if type_id in optional_types and exc.status in (400, 404):
                        warnings.append(
                            "Section {0} does not support optional type {1} endpoint (HTTP {2})".format(
                                section["id"], type_id, exc.status
                            )
                        )
                        continue
                    raise

        include_playlists = not args.no_playlists and args.section is None
        if args.metadata_type is not None and args.metadata_type != 15:
            include_playlists = False
        if include_playlists:
            if progress:
                progress.detail("Playlists / type 15")
            try:
                for elements in iter_paged_elements(client, "/playlists", {"type": 15}, args.page_size):
                    for element in elements:
                        item = item_from_element(element, queried_type=15, api_kind="playlist")
                        if item is None:
                            continue
                        seen_key = ("playlist", item["metadata_id"])
                        if seen_key in seen:
                            stats["duplicates"] += 1
                            continue
                        seen.add(seen_key)
                        update_stats_for_item(stats, item)
                        by_library["(Playlists)"]["scanned"] += 1
                        by_type["Playlist"]["scanned"] += 1
                        if not selected_by_scope(item, args.all_titles):
                            continue
                        enrich_playlist_item(client, item)
                        stats["scope_rows"] += 1
                        candidate = plan_candidate(item, article_strings)
                        if candidate is None:
                            update_stats_for_compliant_item(stats, item, article_strings)
                            continue
                        update_stats_for_candidate(stats, candidate, by_library, by_type)
                        write_spool(spool, candidate)
                    if progress:
                        progress.count(stats["total_api_rows"], "Scanning metadata")
            except PlexApiError as exc:
                if exc.status in (400, 404):
                    warnings.append("Playlist endpoint is not supported; skipped Playlist items")
                else:
                    raise

    if progress:
        progress.finish_transient()
    return stats, by_library, by_type, warnings


def apply_candidate(client, item):
    params = {
        "titleSort.value": item["target_title_sort"],
        "titleSort.locked": 1,
    }
    if item["api_kind"] == "playlist":
        client.put("/playlists/{0}".format(item["metadata_id"]), params=params)
    else:
        if not item.get("library_section_id"):
            raise RuntimeError("library_section_id is missing")
        params.update(
            {
                "type": item["metadata_type"],
                "id": item["metadata_id"],
                "includeExternalMedia": 1,
            }
        )
        client.put(
            "/library/sections/{0}/all".format(item["library_section_id"]),
            params=params,
        )


def verify_element(item, element):
    if element is None:
        return (
            False,
            "ITEM_NOT_FOUND",
            "item was not found in the verification response",
            {
                "actual_title": None,
                "actual_title_sort": None,
                "actual_locked": None,
                "returned_fields": [],
            },
        )
    current_title = element.attrib.get("title")
    current_sort = element.attrib.get("titleSort")
    expected = item.get("target_title_sort")
    returned_fields = [
        dict(child.attrib) for child in element if local_name(child.tag) == "Field"
    ]
    locked = any(
        field.get("name") == "titleSort" and field.get("locked") == "1"
        for field in returned_fields
    )
    omitted_as_redundant = current_sort is None and current_title == expected
    effective_sort = current_title if omitted_as_redundant else current_sort
    diagnostics = {
        "actual_title": current_title,
        "actual_title_sort": current_sort,
        "effective_title_sort": effective_sort,
        "title_sort_omitted_as_redundant": omitted_as_redundant,
        "actual_locked": locked,
        "returned_fields": returned_fields,
    }
    if current_title is None:
        return False, "TITLE_MISSING", "title is missing from the verification response", diagnostics
    if current_title != item.get("title"):
        return (
            False,
            "TITLE_CHANGED",
            "title changed between planning and verification: expected {0!r}, actual {1!r}".format(
                item.get("title"), current_title
            ),
            diagnostics,
        )
    if current_sort != expected and not omitted_as_redundant:
        return (
            False,
            "TITLE_SORT_MISMATCH",
            "titleSort mismatch: expected {0!r}, actual {1!r}".format(expected, current_sort),
            diagnostics,
        )
    if not locked:
        return (
            False,
            "TITLE_SORT_LOCK_NOT_CONFIRMED",
            "effective Sort Title matched, but the verification response did not confirm the lock",
            diagnostics,
        )
    return True, "", "", diagnostics


def apply_batch(client, batch, writer, apply_stats, run_logger):
    library_successes = []
    playlist_successes = []
    for item in batch:
        try:
            apply_candidate(client, item)
            item["result"] = "APPLIED_PENDING_VERIFY"
            if item["api_kind"] == "playlist":
                playlist_successes.append(item)
            else:
                library_successes.append(item)
            apply_stats["put_succeeded"] += 1
            run_logger.put_succeeded(item)
        except Exception as exc:
            item["result"] = "APPLY_FAILED"
            item["error"] = str(exc)
            apply_stats["failed"] += 1
            apply_stats["failure:APPLY_REQUEST_ERROR"] += 1
            run_logger.failed(
                item,
                "APPLY_PUT",
                "APPLY_REQUEST_ERROR",
                str(exc),
            )
            if writer:
                writer.writerow(csv_row(item))

    if library_successes:
        try:
            details = fetch_library_details(client, library_successes)
        except Exception as exc:
            details = {}
            shared_error = "batch verification failed: {0}".format(exc)
            run_logger.event("WARNING", "VERIFY_BATCH_FETCH", shared_error)
        else:
            shared_error = ""
        for item in library_successes:
            element = details.get(item["metadata_id"])
            if element is None:
                try:
                    element = fetch_one_library_detail(client, item["metadata_id"])
                except Exception as exc:
                    item["result"] = "VERIFY_FAILED"
                    errors = [value for value in (shared_error, "single-item verification failed: {0}".format(exc)) if value]
                    item["error"] = " | ".join(errors)
                    apply_stats["failed"] += 1
                    apply_stats["failure:VERIFY_FETCH_ERROR"] += 1
                    run_logger.failed(
                        item,
                        "VERIFY_FETCH",
                        "VERIFY_FETCH_ERROR",
                        item["error"],
                    )
                    if writer:
                        writer.writerow(csv_row(item))
                    continue
            ok, error_code, error, diagnostics = verify_element(item, element)
            item["result"] = "VERIFIED" if ok else "VERIFY_FAILED"
            item["error"] = error
            apply_stats["verified" if ok else "failed"] += 1
            if ok:
                if diagnostics.get("title_sort_omitted_as_redundant"):
                    apply_stats["verified_title_fallback"] += 1
                else:
                    apply_stats["verified_explicit"] += 1
                run_logger.verified(item, diagnostics)
            else:
                apply_stats["failure:{0}".format(error_code)] += 1
                run_logger.failed(
                    item,
                    "VERIFY_VALUE",
                    error_code,
                    error,
                    diagnostics=diagnostics,
                    element=element,
                )
            if writer:
                writer.writerow(csv_row(item))

    for item in playlist_successes:
        try:
            element = fetch_playlist_detail(client, item["metadata_id"])
            ok, error_code, error, diagnostics = verify_element(item, element)
        except Exception as exc:
            ok = False
            error_code = "VERIFY_FETCH_ERROR"
            error = str(exc)
            diagnostics = {}
            element = None
        item["result"] = "VERIFIED" if ok else "VERIFY_FAILED"
        item["error"] = error
        apply_stats["verified" if ok else "failed"] += 1
        if ok:
            if diagnostics.get("title_sort_omitted_as_redundant"):
                apply_stats["verified_title_fallback"] += 1
            else:
                apply_stats["verified_explicit"] += 1
            run_logger.verified(item, diagnostics)
        else:
            apply_stats["failure:{0}".format(error_code)] += 1
            run_logger.failed(
                item,
                "VERIFY_FETCH" if error_code == "VERIFY_FETCH_ERROR" else "VERIFY_VALUE",
                error_code,
                error,
                diagnostics=diagnostics,
                element=element,
            )
        if writer:
            writer.writerow(csv_row(item))


def open_csv_writer(path):
    if path is None:
        return None, None
    path = path.expanduser()
    parent = path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    return handle, writer


def output_dry_run(spool_path, writer):
    for item in read_spool(spool_path):
        item["result"] = "DRY_RUN"
        if writer:
            writer.writerow(csv_row(item))


def apply_spool(client, spool_path, writer, batch_size, run_logger, total=0, progress=None):
    apply_stats = Counter()
    processed = 0
    for batch in chunks(read_spool(spool_path), batch_size):
        apply_batch(client, batch, writer, apply_stats, run_logger)
        processed += len(batch)
        if progress:
            progress.update(processed, total, "Applying and verifying")
    if progress:
        progress.finish_transient()
    return apply_stats


def scope_label(args):
    return "all non-empty titles" if args.all_titles else "titles containing Korean characters"


def failure_reason_items(apply_stats):
    return sorted(
        (key.split(":", 1)[1], count)
        for key, count in apply_stats.items()
        if key.startswith("failure:") and count
    )


def group_report(groups):
    return {
        str(name): {
            "scanned": values["scanned"],
            "candidates": values["candidates"],
            "value_edits": values["value_changes"],
            "lock_additions": values["lock_adds"],
        }
        for name, values in sorted(groups.items())
    }


def candidate_samples(spool_path, limit):
    samples = []
    for item in read_spool(spool_path):
        if len(samples) >= limit:
            break
        samples.append(
            {
                "metadata_id": item.get("metadata_id"),
                "library_name": item.get("library_name"),
                "metadata_type_name": item.get("metadata_type_name"),
                "title": item.get("title"),
                "current_title_sort": item.get("title_sort"),
                "target_title_sort": item.get("target_title_sort"),
                "action": item.get("action"),
                "title_sort_status": item.get("title_sort_status"),
            }
        )
    return samples


def build_scan_report(
    args,
    identity,
    token_source,
    stats,
    by_library,
    by_type,
    warnings,
    article_strings,
    article_source,
    spool_path,
):
    return {
        "program": PROGRAM,
        "version": VERSION,
        "mode": "apply" if args.apply else "dry-run",
        "server": {
            "url": args.plex_url,
            "version": identity.get("version"),
            "machine_identifier": identity.get("machine_identifier"),
        },
        "scope": scope_label(args),
        "metadata_source": "Plex API only",
        "configuration": {
            "token_source": token_source,
            "article_strings": list(article_strings),
            "article_source": article_source,
        },
        "scan": {
            "metadata_rows": stats["total_api_rows"],
            "title_missing": stats["title_null"],
            "title_empty": stats["title_empty"],
            "title_present": stats["title_present"],
            "selected_rows": stats["scope_rows"],
            "duplicate_rows_ignored": stats["duplicates"],
            "already_compliant": {
                "total": stats["already_correct_locked"],
                "explicit_title_sort": stats["already_correct_explicit"],
                "plex_title_fallback": stats["already_correct_title_fallback"],
                "other": stats["already_correct_other"],
            },
        },
        "planned_changes": {
            "total": stats["candidates"],
            "actions": {
                "set_value_and_lock": stats["action:SET_VALUE_AND_LOCK"],
                "set_value_only": stats["action:SET_VALUE"],
                "add_lock_only": stats["action:LOCK_ONLY"],
            },
            "value_edits": stats["value_changes"],
            "lock_additions": stats["lock_adds"],
            "title_sort_status": {
                status: stats["status:{0}".format(status)]
                for status in (
                    "TITLE_SORT_NULL",
                    "TITLE_SORT_OMITTED_EQUIVALENT",
                    "TITLE_SORT_EMPTY",
                    "MISMATCH",
                    "MATCH",
                )
            },
            "sort_key_source": {
                "existing_title_sort": stats["source:EXISTING_TITLE_SORT"],
                "derived_plex_rules": stats["source:DERIVED_PLEX_RULES"],
            },
        },
        "by_library": group_report(by_library),
        "by_metadata_type": group_report(by_type),
        "candidate_samples": candidate_samples(spool_path, args.preview_limit),
        "warnings": list(warnings),
    }


def print_section(title, color):
    print("\n{0}".format(style_text(title, STYLE_BOLD_CYAN, color)))


def print_count_table(title, groups, color=False):
    groups = {name: values for name, values in groups.items() if values["candidates"]}
    if not groups:
        return
    available = terminal_width()
    name_width = max(16, min(36, available - 51))
    print_section(title, color)
    print(
        style_text("  {0}  {1:>9}  {2:>10}  {3:>11}  {4:>9}".format(
            pad_display("Name", name_width), "Scanned", "Candidates", "Value edits", "Lock adds"
        ), STYLE_BOLD, color)
    )
    print("  {0}  {1}  {2}  {3}  {4}".format(
        "-" * name_width, "-" * 9, "-" * 10, "-" * 11, "-" * 9
    ))
    for name in sorted(groups):
        values = groups[name]
        print(
            "  {0}  {1:>9,}  {2:>10,}  {3:>11,}  {4:>9,}".format(
                pad_display(name, name_width),
                values["scanned"],
                values["candidates"],
                values["value_changes"],
                values["lock_adds"],
            )
        )


def print_summary(
    args,
    identity,
    token_source,
    stats,
    by_library,
    by_type,
    warnings,
    article_strings,
    article_source,
):
    mode = "APPLY" if args.apply else "DRY-RUN"
    color = getattr(args, "stdout_color", False)
    print("\n{0}".format(style_text(
        "Plex NFKD Sort Title {0} [{1}]".format(VERSION, mode), STYLE_BOLD_CYAN, color
    )))
    print("Server : Plex {0} at {1}".format(identity.get("version") or "(unknown)", args.plex_url))
    print("Scope  : {0}".format(scope_label(args)))
    print("Source : Plex API only")
    if args.verbose:
        print("Machine: {0}".format(identity.get("machine_identifier") or "(unknown)"))
        print("Token  : {0}".format(token_source))
        print("Articles: {0}".format(", ".join(article_strings) if article_strings else "(none)"))
        print("Article source: {0}".format(article_source))
    print_section("Scan summary", color)
    print("  Metadata rows                     {0:>10,}".format(stats["total_api_rows"]))
    print("  Title missing                     {0:>10,}".format(stats["title_null"]))
    print("  Title empty                       {0:>10,}".format(stats["title_empty"]))
    print("  Selected rows                     {0:>10,}".format(stats["scope_rows"]))
    print("  Already compliant and locked      {0:>10,}".format(stats["already_correct_locked"]))
    print("    Explicit titleSort              {0:>10,}".format(stats["already_correct_explicit"]))
    print("    Plex title fallback             {0:>10,}".format(stats["already_correct_title_fallback"]))
    if stats["already_correct_other"]:
        print("    Other equivalent state          {0:>10,}".format(stats["already_correct_other"]))

    print_section("Planned changes", color)
    if not stats["candidates"]:
        print("  None")
        if stats["duplicates"]:
            print("\nDuplicate API rows ignored: {0:,}".format(stats["duplicates"]))
        if warnings:
            print_section("Warnings", color)
            for warning in warnings:
                print(style_text("  - {0}".format(warning), STYLE_YELLOW, color))
        return
    print("  Total candidates                  {0:>10,}".format(stats["candidates"]))
    action_labels = (
        ("SET_VALUE_AND_LOCK", "Set value and lock"),
        ("SET_VALUE", "Set value only"),
        ("LOCK_ONLY", "Add lock only"),
    )
    for key, label in action_labels:
        if stats["action:{0}".format(key)]:
            print("  {0:<34} {1:>10,}".format(label, stats["action:{0}".format(key)]))
    print("  titleSort value edits             {0:>10,}".format(stats["value_changes"]))
    print("  titleSort lock additions          {0:>10,}".format(stats["lock_adds"]))

    print_section("Current titleSort status among candidates", color)
    status_labels = (
        ("TITLE_SORT_NULL", "Missing titleSort"),
        ("TITLE_SORT_OMITTED_EQUIVALENT", "Plex title fallback (equivalent)"),
        ("TITLE_SORT_EMPTY", "Empty titleSort"),
        ("MISMATCH", "Different titleSort"),
        ("MATCH", "Value matches; lock needed"),
    )
    for status, label in status_labels:
        if stats["status:{0}".format(status)]:
            print("  {0:<34} {1:>10,}".format(label, stats["status:{0}".format(status)]))
    print_section("Sort-key source among candidates", color)
    if stats["source:EXISTING_TITLE_SORT"]:
        print("  Existing Plex/custom titleSort    {0:>10,}".format(stats["source:EXISTING_TITLE_SORT"]))
    if stats["source:DERIVED_PLEX_RULES"]:
        print("  Derived using Plex rules          {0:>10,}".format(stats["source:DERIVED_PLEX_RULES"]))
    if stats["duplicates"]:
        print("  duplicate API rows ignored        {0:>10,}".format(stats["duplicates"]))

    print_count_table("By library", by_library, color)
    print_count_table("By metadata type", by_type, color)
    if warnings:
        print_section("Warnings", color)
        for warning in warnings:
            print(style_text("  - {0}".format(warning), STYLE_YELLOW, color))


def print_candidate_preview(spool_path, limit, total, color=False):
    if limit <= 0 or total <= 0:
        return
    id_width = 9
    type_width = 12
    action_width = 18
    title_width = max(18, terminal_width() - id_width - type_width - action_width - 10)
    action_names = {
        "SET_VALUE_AND_LOCK": "SET VALUE + LOCK",
        "SET_VALUE": "SET VALUE",
        "LOCK_ONLY": "LOCK ONLY",
    }
    print_section("Candidate preview", color)
    print(
        style_text("  {0}  {1}  {2}  {3}".format(
            pad_display("ID", id_width),
            pad_display("Type", type_width),
            pad_display("Action", action_width),
            pad_display("Title", title_width),
        ), STYLE_BOLD, color)
    )
    print("  {0}  {1}  {2}  {3}".format(
        "-" * id_width, "-" * type_width, "-" * action_width, "-" * title_width
    ))
    shown = 0
    for item in read_spool(spool_path):
        if shown >= limit:
            break
        shown += 1
        action_style = STYLE_CYAN if item["action"] == "LOCK_ONLY" else STYLE_YELLOW
        action_cell = style_text(
            pad_display(action_names.get(item["action"], item["action"]), action_width),
            action_style,
            color,
        )
        print(
            "  {0}  {1}  {2}  {3}".format(
                pad_display(item["metadata_id"], id_width),
                pad_display(item["metadata_type_name"], type_width),
                action_cell,
                pad_display(item["title"], title_width),
            )
        )
        if item["title_sort"] is None and item.get("title_sort_omitted_as_redundant"):
            current = "(Plex title fallback)"
        elif item["title_sort"] is None:
            current = "(NULL)"
        elif item["title_sort"] == "":
            current = "(EMPTY)"
        else:
            current = item["title_sort"]
        change = "{0} -> {1}".format(current, item["target_title_sort"])
        print("  {0}".format(truncate_display("sort: " + change, terminal_width() - 2)))
    if shown < total:
        print("  ... {0:,} additional candidates; use --console to show details.".format(total - shown))


def print_candidates(spool_path, limit, total, show_codepoints, heading, color=False):
    if limit <= 0:
        return
    shown = 0
    print_section(heading, color)
    for item in read_spool(spool_path):
        if shown >= limit:
            break
        shown += 1
        location = "Playlist" if item["api_kind"] == "playlist" else "section {0} / {1}".format(
            item.get("library_section_id") or "?", item.get("library_name") or "?"
        )
        print("\n{0}".format("-" * terminal_width()))
        print("[{0}/{1}] ID {2} | {3} ({4}) | {5}".format(
            shown,
            total,
            item["metadata_id"],
            item["metadata_type_name"],
            item["metadata_type"],
            location,
        ))
        action_style = STYLE_CYAN if item["action"] == "LOCK_ONLY" else STYLE_YELLOW
        print("  Action       : {0}".format(style_text(item["action"], action_style, color)))
        print("  Title        : {0}".format(item["title"]))
        print("  Current sort : {0}".format("(NULL)" if item["title_sort"] is None else item["title_sort"]))
        print("  Sort base    : {0}".format(item["sort_base"]))
        print("  Target sort  : {0}".format(item["target_title_sort"]))
        print("  Rule         : {0}".format(item["sort_key_source"]))
        if item.get("removed_prefix"):
            print("  Prefix removed: {0!r}".format(item["removed_prefix"]))
        print("  Lock         : {0} -> true".format(bool_text(item["title_sort_locked"])))
        if show_codepoints:
            print("  TITLE CODEPOINTS: {0}".format(codepoints(item["title"])))
            print("  OLD SORT CODEPOINTS: {0}".format(codepoints(item["title_sort"])))
            print("  TARGET CODEPOINTS: {0}".format(codepoints(item["target_title_sort"])))
    if shown == 0:
        print("  (none)")
    elif shown < total:
        print("\n... {0:,} additional candidates not shown.".format(total - shown))


def dry_run_result_name(candidate_count):
    return "CHANGES PLANNED" if candidate_count else "NO CHANGES REQUIRED"


def apply_result_name(candidate_count, apply_stats):
    if not candidate_count:
        return "NO CHANGES REQUIRED"
    if not apply_stats["failed"]:
        return "APPLY SUCCEEDED"
    if apply_stats["verified"]:
        return "APPLY PARTIALLY FAILED"
    return "APPLY FAILED"


def print_dry_run_result(stats, csv_path, color=False):
    result = dry_run_result_name(stats["candidates"])
    print("\n{0}".format(result_line(result, color)))
    print("HTTP PUT requests: 0")
    if stats["candidates"]:
        print("Next step: review the preview or CSV, then rerun with --apply.")
    if csv_path:
        print_section("Artifacts", color)
        print("  CSV: {0}".format(csv_path.expanduser().resolve()))
    print("Exit code: 0")


def print_apply_result(stats, apply_stats, run_logger, backup_bundle, csv_path, color=False):
    result = apply_result_name(stats["candidates"], apply_stats)
    exit_code = 4 if apply_stats["failed"] else 0
    print("\n{0}".format(result_line(result, color)))
    print_section("Apply summary", color)
    print("  Planned                         {0:>10,}".format(stats["candidates"]))
    print("  PUT succeeded                   {0:>10,}".format(apply_stats["put_succeeded"]))
    print("  Verified                        {0:>10,}".format(apply_stats["verified"]))
    print("    Explicit titleSort            {0:>10,}".format(apply_stats["verified_explicit"]))
    print("    Plex title fallback           {0:>10,}".format(apply_stats["verified_title_fallback"]))
    print("  Failed                          {0:>10,}".format(apply_stats["failed"]))
    reasons = failure_reason_items(apply_stats)
    if reasons:
        print_section("Failure reasons", color)
        for reason, count in reasons:
            print(style_text("  {0:<36} {1:>10,}".format(reason, count), STYLE_RED, color))
    if run_logger.failure_samples:
        print_section("Failure examples", color)
        for sample in run_logger.failure_samples:
            heading = "ID {0} | {1}".format(sample.get("metadata_id") or "?", sample.get("title") or "(untitled)")
            print("  {0}".format(truncate_display(heading, terminal_width() - 2)))
            detail = "{0}: {1}".format(sample.get("error_code"), sample.get("error"))
            print("    {0}".format(truncate_display(detail, terminal_width() - 4)))
    print_section("Artifacts", color)
    if backup_bundle:
        print("  Backup  : {0}".format(backup_bundle))
    else:
        print("  Backup  : (not created)")
    print("  Logs    : {0}".format(run_logger.run_dir))
    print("    run.log")
    print("    failures.csv")
    print("    summary.json")
    if apply_stats["failed"]:
        print("    responses/")
    if csv_path:
        print("  CSV     : {0}".format(csv_path.expanduser().resolve()))
    if apply_stats["failed"]:
        print("\nNext step: inspect failures.csv and the saved API responses before retrying.")
    elif stats["candidates"]:
        print("\nNext step: rerun the dry run to confirm that no changes remain.")
    print("Exit code: {0}".format(exit_code))
    return exit_code


def remove_temp(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print("Temporary file cleanup warning: {0}".format(exc), file=sys.stderr)


def main(argv=None):
    configure_output_encoding()
    args = parse_args(argv)
    args.stdout_color = color_enabled(args.color, sys.stdout, args.output_format)
    args.stderr_color = color_enabled(args.color, sys.stderr, args.output_format)
    progress = ProgressReporter(
        enabled=not args.quiet_progress,
        color=args.stderr_color,
    )
    preferences_path, preferences_attrs, preferences_errors = load_preferences(args.preferences)

    if args.restore_backup:
        progress.phase(1, 1, "Restoring the Plex databases")
        try:
            db_dir, bundle, safety_dir = restore_backup_bundle(args, preferences_path, progress)
        except Exception as exc:
            return emit_error(args, 5, "database restore failed: {0}".format(exc))
        progress.detail("Restore completed")
        if args.output_format == "json":
            emit_json(
                {
                    "program": PROGRAM,
                    "version": VERSION,
                    "mode": "restore",
                    "result": "RESTORE SUCCEEDED",
                    "exit_code": 0,
                    "restored_from": str(bundle),
                    "target_database_directory": str(db_dir),
                    "previous_database_copy": str(safety_dir),
                    "next_step": "Start Plex Media Server and verify the libraries.",
                }
            )
        else:
            print("\n{0}".format(result_line("RESTORE SUCCEEDED", args.stdout_color)))
            print("  Restored from : {0}".format(bundle))
            print("  Target DB dir : {0}".format(db_dir))
            print("  Previous files: {0}".format(safety_dir))
            print("Next step: start Plex Media Server and verify the libraries.")
            print("Exit code: 0")
        return 0

    if args.all_titles:
        print(
            style_text(
                "WARNING: --all-unicode evaluates and locks Sort Title for every non-empty title.",
                STYLE_BOLD_YELLOW,
                args.stderr_color,
            ),
            file=sys.stderr,
        )

    try:
        token, token_source = resolve_token(args)
    except Exception as exc:
        return emit_error(args, 2, exc)

    article_strings, article_source = resolve_article_strings(
        args, preferences_path, preferences_attrs
    )
    if preferences_errors and preferences_path is None and args.article_strings is None:
        print(
            style_text(
                "Warning: Preferences.xml could not be read; Plex default ArticleStrings will be used.",
                STYLE_YELLOW,
                args.stderr_color,
            ),
            file=sys.stderr,
        )

    client = PlexClient(args.plex_url, token, args.timeout)
    phase_total = 4 if args.apply else 2
    try:
        identity = get_identity(client)
        sections = get_sections(client)
    except Exception as exc:
        return emit_error(
            args, 3, "Plex server connection or authentication failed: {0}".format(exc)
        )
    progress.phase(
        1,
        phase_total,
        "Connected to Plex {0}".format(identity.get("version") or "(unknown version)"),
    )

    if args.section is not None and not any(section["id"] == args.section for section in sections):
        return emit_error(
            args, 2, "section {0} was not found through the Plex API".format(args.section)
        )

    if args.metadata_type == 15 and args.section is not None:
        return emit_error(
            args,
            2,
            "Playlist items (type 15) have no library section and cannot be used with --section",
        )

    temp_handle = tempfile.NamedTemporaryFile(
        prefix="plex_nfkd_candidates_", suffix=".jsonl", delete=False
    )
    temp_handle.close()
    spool_path = Path(temp_handle.name)
    csv_handle = None
    writer = None
    try:
        progress.phase(2, phase_total, "Scanning metadata")
        try:
            stats, by_library, by_type, warnings = scan_api(
                client, args, sections, spool_path, article_strings, progress
            )
        except Exception as exc:
            return emit_error(
                args, 3, "API scan failed; no changes were started: {0}".format(exc)
            )
        progress.detail(
            "Scan complete: {0:,} rows, {1:,} candidates".format(
                stats["total_api_rows"], stats["candidates"]
            )
        )

        report = build_scan_report(
            args,
            identity,
            token_source,
            stats,
            by_library,
            by_type,
            warnings,
            article_strings,
            article_source,
            spool_path,
        )
        if args.output_format == "text":
            print_summary(
                args,
                identity,
                token_source,
                stats,
                by_library,
                by_type,
                warnings,
                article_strings,
                article_source,
            )
            if args.console or args.show_codepoints:
                console_limit = (
                    args.console_limit or stats["candidates"]
                    if args.console
                    else min(args.preview_limit, stats["candidates"])
                )
                print_candidates(
                    spool_path,
                    console_limit,
                    stats["candidates"],
                    args.show_codepoints,
                    "Candidate details",
                    args.stdout_color,
                )
            else:
                print_candidate_preview(
                    spool_path,
                    min(args.preview_limit, stats["candidates"]),
                    stats["candidates"],
                    args.stdout_color,
                )

        try:
            csv_handle, writer = open_csv_writer(args.csv)
        except OSError as exc:
            return emit_error(
                args, 2, "unable to open the CSV file; no changes were started: {0}".format(exc)
            )

        if args.apply:
            backup_bundle = None
            if stats["candidates"] and not args.no_backup:
                progress.phase(3, phase_total, "Creating the database backup")
                try:
                    db_dir = resolve_db_dir(args, preferences_path)
                    backup_bundle, _ = create_backup_bundle(args, db_dir, identity, progress)
                except Exception as exc:
                    return emit_error(
                        args,
                        5,
                        "pre-apply database backup failed; no API changes were started: {0}".format(
                            exc
                        ),
                    )
                progress.detail("Backup completed: {0}".format(backup_bundle))
            elif stats["candidates"] and args.no_backup:
                progress.phase(3, phase_total, "Database backup disabled")
                print(
                    style_text(
                        "WARNING: applying without a database backup because --no-backup was specified.",
                        STYLE_BOLD_YELLOW,
                        args.stderr_color,
                    ),
                    file=sys.stderr,
                )
            else:
                progress.phase(3, phase_total, "Database backup not required")

            try:
                run_dir = create_apply_log_directory(args, backup_bundle)
                run_logger = ApplyRunLogger(
                    run_dir,
                    args,
                    identity,
                    stats,
                    backup_bundle,
                    token,
                )
            except OSError as exc:
                return emit_error(
                    args,
                    5,
                    "unable to create the apply log directory; no API changes were started: {0}".format(
                        exc
                    ),
                )

            progress.phase(4, phase_total, "Applying and verifying changes")
            try:
                apply_stats = apply_spool(
                    client,
                    spool_path,
                    writer,
                    args.detail_batch_size,
                    run_logger,
                    stats["candidates"],
                    progress,
                )
            except Exception as exc:
                run_logger.event("ERROR", "RUN_ABORTED", "Unexpected apply error: {0}".format(exc))
                apply_stats = Counter({"failed": 1, "failure:UNEXPECTED_RUN_ERROR": 1})
                run_logger.failure_counts["UNEXPECTED_RUN_ERROR"] += 1
                run_logger.finish(apply_stats)
                print(
                    style_text("Apply logs: {0}".format(run_dir), STYLE_YELLOW, args.stderr_color),
                    file=sys.stderr,
                )
                return emit_error(args, 5, "apply stopped unexpectedly: {0}".format(exc))
            run_logger.finish(apply_stats)
            if csv_handle:
                csv_handle.flush()
            exit_code = 4 if apply_stats["failed"] else 0
            report["result"] = apply_result_name(stats["candidates"], apply_stats)
            report["exit_code"] = exit_code
            report["apply"] = {
                "planned": stats["candidates"],
                "put_succeeded": apply_stats["put_succeeded"],
                "verified": apply_stats["verified"],
                "verified_explicit_title_sort": apply_stats["verified_explicit"],
                "verified_plex_title_fallback": apply_stats["verified_title_fallback"],
                "failed": apply_stats["failed"],
                "failure_reasons": dict(failure_reason_items(apply_stats)),
                "failure_samples": run_logger.failure_samples,
            }
            report["artifacts"] = {
                "backup": str(backup_bundle) if backup_bundle else None,
                "log_directory": str(run_logger.run_dir),
                "run_log": str(run_logger.run_log_path),
                "failures_csv": str(run_logger.failures_path),
                "summary_json": str(run_logger.summary_path),
                "responses_directory": (
                    str(run_logger.responses_dir) if apply_stats["failed"] else None
                ),
                "csv": str(args.csv.expanduser().resolve()) if args.csv else None,
            }
            if args.output_format == "json":
                emit_json(report)
            else:
                print_apply_result(
                    stats,
                    apply_stats,
                    run_logger,
                    backup_bundle,
                    args.csv,
                    args.stdout_color,
                )
            return exit_code

        output_dry_run(spool_path, writer)
        if csv_handle:
            csv_handle.flush()
        report["result"] = dry_run_result_name(stats["candidates"])
        report["exit_code"] = 0
        report["http_put_requests"] = 0
        report["artifacts"] = {
            "csv": str(args.csv.expanduser().resolve()) if args.csv else None,
        }
        if args.output_format == "json":
            emit_json(report)
        else:
            print_dry_run_result(stats, args.csv, args.stdout_color)
        return 0
    finally:
        if csv_handle:
            csv_handle.close()
        remove_temp(spool_path)


if __name__ == "__main__":
    sys.exit(main())
