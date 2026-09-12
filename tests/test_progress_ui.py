import contextlib
import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from unittest import mock

from test_safety_regressions import MODULE as M, candidate_item, metadata_element


class TtyStream(io.StringIO):
    def isatty(self):
        return True


class ProgressDisplayTests(unittest.TestCase):
    def reporter(self, stream=None, color=False, output_format="text"):
        self.now = 0.0
        return M.ProgressReporter(stream=stream if stream is not None else io.StringIO(),
                                  color=color, output_format=output_format,
                                  clock=lambda: self.now)

    def test_plain_logs_throttle_but_flush_latest_snapshot_at_boundary(self):
        progress = self.reporter()
        progress.start_task("TV · Episode")
        progress.scan(Counter(total_api_rows=1), 1)
        self.now = 1.0
        progress.set_context("Movies · Movie")
        progress.scan(Counter(total_api_rows=2, candidates=1), 2)
        self.assertNotIn("Candidates 1", progress.stream.getvalue())
        self.assertNotIn("Movies", progress.stream.getvalue())
        self.now = 5.0
        progress.scan(Counter(total_api_rows=3, candidates=2), 3)
        self.now = 5.1
        progress.set_context("Playlists · Playlist")
        progress.scan(Counter(total_api_rows=4, candidates=3), 4)
        progress.phase(3, 4, "Creating backup")
        output = progress.stream.getvalue()
        self.assertIn("Candidates 3", output)
        self.assertLess(output.index("Candidates 3"), output.index("[3/4]"))
        self.assertEqual(len(output.splitlines()), 4)
        self.assertNotIn("\r", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("%", output)

    def test_scan_timing_and_counters_span_context_changes(self):
        progress = self.reporter()
        with mock.patch.object(progress, "_width", return_value=100):
            progress.start_task("TV · Episode")
            self.now = 10
            progress.scan(Counter(total_api_rows=100, candidates=10), 100, 100)
            progress.set_context("Movies · Movie")
            self.now = 20
            progress.scan(Counter(total_api_rows=150, candidates=20), 50, 100)
        last = progress.stream.getvalue().splitlines()[-1]
        self.assertIn("Total scanned 150 · Candidates 20 | Elapsed", last)
        self.assertIn("Library   Movies · Movie", last)
        self.assertIn("50% · 50/100 items", last)
        self.assertIn("Elapsed 00:20 · 7.5/s", last)
        self.assertNotIn("ETA", last)

    def test_live_redraw_rate_and_narrow_layout(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            progress = self.reporter(TtyStream(), color=True)
        progress.start_task("한글 라이브러리 · Episode")
        with mock.patch.object(progress, "_width", return_value=45):
            progress.applying(Counter(), 10, "Applying")
            first = progress.stream.getvalue()
            self.now = 0.1
            progress.applying(Counter(put_succeeded=1), 10, "Awaiting verification")
            self.assertEqual(first, progress.stream.getvalue())
            self.now = 0.21
            progress.applying(Counter(put_succeeded=2, verified=1, failed=1), 10, "Verifying")
            progress.finish_transient()
        output = progress.stream.getvalue()
        self.assertIn("\x1b[4A", output)
        self.assertIn("Verified 1 · Failed 1", output)
        self.assertIn("20% · 2/10 items", output)
        self.assertIn("Skipped 0 · PUT OK 2", output)
        self.assertNotIn("ETA", output)
        plain = re.sub(r"\x1b\[[0-9;]*[mAK]", "", output)
        for row in plain.splitlines():
            self.assertLessEqual(M.display_width(row), 45)

    def test_json_no_color_and_dumb_terminal_never_emit_cursor_controls(self):
        for output_format, environment, color in [
            ("json", {}, True), ("text", {"NO_COLOR": ""}, False),
            ("text", {"TERM": "dumb"}, False), ("text", {}, False),
        ]:
            with self.subTest(output_format=output_format, environment=environment, color=color), \
                    mock.patch.dict(os.environ, environment, clear=True):
                progress = self.reporter(TtyStream(), color=color, output_format=output_format)
                progress.phase(2, 2, "Scanning")
                progress.start_task("TV")
                progress.scan(Counter(total_api_rows=3), 3, 10)
                progress.finish_transient()
                self.assertNotIn("\x1b", progress.stream.getvalue())
                self.assertNotIn("\r", progress.stream.getvalue())

    def test_resize_does_not_rewind_reflowed_output(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            progress = self.reporter(TtyStream(), color=True)
        with mock.patch.object(progress, "_width", return_value=100):
            progress.render(["A", "B"], force=True)
        before = len(progress.stream.getvalue())
        with mock.patch.object(progress, "_width", return_value=40):
            progress.render(["C", "D"], force=True)
        self.assertNotIn("\x1b[1A", progress.stream.getvalue()[before:])
        progress.finish_transient()

    def test_percent_is_query_local_and_does_not_round_up_to_completion(self):
        progress = self.reporter()
        progress.start_task("TV · Episode")
        progress.scan(Counter(total_api_rows=8500, candidates=240), 6000, 10000, force=True)
        output = progress.stream.getvalue()
        self.assertIn("60% · 6,000/10,000", output)
        self.assertIn("Total scanned 8,500", output)
        self.assertIn("99%", progress._meter(9999, 10000, "items"))
        self.assertNotIn("%", progress._meter(10001, 10000, "items"))

    def test_eta_uses_completed_items_and_resets_between_tasks(self):
        progress = self.reporter()
        with mock.patch.object(progress, "_width", return_value=100):
            progress.start_task("Apply")
            self.now = 10
            progress.applying(Counter(put_succeeded=10), 20, "Awaiting verification", force=True)
            self.assertNotIn("ETA", progress.stream.getvalue())
            progress.applying(Counter(put_succeeded=10, verified=8, failed=2), 20, "Verifying", force=True)
            self.assertIn("50%", progress.stream.getvalue())
            self.assertIn("ETA ~00:10", progress.stream.getvalue())
            progress.start_task("Next task")
            self.assertEqual(progress._timing(0), "Elapsed 00:00")

    def test_library_meter_is_separate_from_query_progress_in_narrow_panel(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            progress = self.reporter(TtyStream(), color=True)
        with mock.patch.object(progress, "_width", return_value=45):
            progress.start_task("TV · Episode")
            progress.set_library_progress(2, 4, 1, skipped=1)
            progress.scan(Counter(total_api_rows=8500, candidates=240), 6000, 10000)
            progress.finish_transient()
        plain = re.sub(r"\x1b\[[0-9;]*[mAK]", "", progress.stream.getvalue())
        self.assertIn("25% · 1/4 processed · Skipped 1", plain)
        self.assertIn("Library   2/4 · TV · Episode", plain)
        self.assertIn("60% · 6,000/10,000 items", plain)
        for row in plain.splitlines():
            self.assertLessEqual(M.display_width(row), 45)

    def test_scan_groups_aligned_meters_above_counters(self):
        for width in (100, 64, 45):
            for total in (10000, None):
                for index in (2, 0):
                    with self.subTest(width=width, total=total, index=index):
                        progress = self.reporter()
                        progress.start_task("TV · Episode" if index else "Playlists · Playlist")
                        progress.set_library_progress(index, 4, 1, skipped=1)
                        with mock.patch.object(progress, "_width", return_value=width), \
                                mock.patch.object(progress, "render") as render:
                            progress.scan(Counter(total_api_rows=8500, candidates=240), 6000, total)
                        lines = render.call_args[0][0]
                        self.assertTrue(lines[1].startswith("Library   2/4" if index else "Current   "))
                        self.assertTrue(lines[2].startswith("Libraries "))
                        self.assertTrue(lines[3].startswith("Query     "))
                        self.assertTrue(lines[4].startswith("Total scanned" if width >= 64 else "Scanned"))
                        self.assertTrue(lines[-1].startswith("Elapsed"))
                        for line in lines[2:4]:
                            self.assertLessEqual(M.display_width(line), width)
                        if total is not None:
                            self.assertEqual(lines[2].find("["), lines[3].find("["))
                            self.assertEqual(lines[2].find("]"), lines[3].find("]"))
                            self.assertIn("60%", lines[3])
                        else:
                            self.assertEqual(lines[3], "Query     6,000 items")
                        self.assertIn("25%", lines[2])
                        self.assertIn("Skipped 1", lines[2])

    def test_quiet_mode_and_control_characters(self):
        stream = io.StringIO()
        quiet = M.ProgressReporter(enabled=False, stream=stream)
        quiet.phase(2, 4, "Scanning")
        quiet.start_task("TV")
        quiet.scan(Counter(), 0)
        quiet.finish_transient()
        self.assertEqual(stream.getvalue(), "")
        progress = self.reporter()
        progress.start_task("Library\x1b[2J\r\nInjected")
        progress.scan(Counter(), 0)
        self.assertNotIn("\x1b", progress.stream.getvalue())
        self.assertEqual(len(progress.stream.getvalue().splitlines()), 1)


class SyntheticClient:
    """In-memory Plex API with paginated library rows and one playlist."""

    def __init__(self):
        self.puts = []
        self.calls = []
        self.titles = {"1": "한글", "2": "한글 둘", "3": "한국 음악"}
        self.sorts = {}
        self.before_put = lambda: None

    def element(self, key, playlist=False):
        element = ET.Element("Playlist" if playlist else "Video", ratingKey=key,
                             title=self.titles[key], type="playlist" if playlist else "episode")
        if key in self.sorts:
            element.set("titleSort", self.sorts[key])
            ET.SubElement(element, "Field", name="titleSort", locked="1")
        return element

    def get(self, path, params=None, extra_headers=None):
        self.calls.append(path)
        root = ET.Element("MediaContainer")
        if path == "/identity":
            root.set("version", "synthetic")
        elif path == "/library/sections":
            ET.SubElement(root, "Directory", key="4", title="TV", type="show")
        elif path == "/library/sections/4":
            pass
        elif path == "/library/sections/4/all":
            if params["type"] == 4:
                offset = int(extra_headers["X-Plex-Container-Start"])
                root.set("totalSize", "2")
                root.set("offset", str(offset))
                if offset < 2:
                    root.append(self.element(str(offset + 1)))
        elif path.startswith("/library/metadata/"):
            for key in path.rsplit("/", 1)[1].split(","):
                root.append(self.element(key))
        elif path == "/playlists":
            root.set("totalSize", "1")
            root.append(self.element("3", playlist=True))
        elif path == "/playlists/3":
            root.append(self.element("3", playlist=True))
        else:
            raise AssertionError("Unexpected API call: " + path)
        root.set("size", str(len(root)))
        return root, {}

    def put(self, path, params=None):
        self.before_put()
        self.puts.append(path)
        key = "3" if path.startswith("/playlists/") else params["id"]
        self.sorts[key] = params["titleSort.value"]


class ProgressIntegrationTests(unittest.TestCase):
    def run_main(self, client, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(M, "load_preferences", return_value=(None, {}, [])), \
                mock.patch.object(M, "PlexClient", return_value=client), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = M.main(["--token", "synthetic-audit-token", "--output-format", "json"] + arguments)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def create_databases(self, root):
        db_dir = root / "data" / "Plug-in Support" / "Databases"
        db_dir.mkdir(parents=True)
        for name in (M.MAIN_DB_NAME, M.BLOBS_DB_NAME):
            connection = sqlite3.connect(str(db_dir / name))
            try:
                connection.execute("CREATE TABLE marker(value TEXT)")
                connection.commit()
            finally:
                connection.close()
        return db_dir

    def test_dry_run_preserves_json_csv_and_database_isolation(self):
        client = SyntheticClient()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dry.csv"
            with mock.patch.object(M.sqlite3, "connect", side_effect=AssertionError("Dry-run DB access")):
                code, report, stderr = self.run_main(client, ["--dry-run", "--color", "always",
                    "--csv", str(output), "--page-size", "1"])
            self.assertEqual(code, 0)
            self.assertEqual(report["planned_changes"]["total"], 3)
            self.assertEqual(client.puts, [])
            self.assertEqual(client.calls.count("/library/sections/4/all"), 7)
            self.assertIn("Current   Playlists · Playlist", stderr)
            self.assertIn("100% · 1/1 items", stderr)
            self.assertIn("Total scanned 3 · Candidates 3", stderr)
            self.assertNotIn("\x1b", stderr)
            self.assertNotIn("\r", stderr)
            self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
            with output.open(encoding="utf-8-sig", newline="") as handle:
                self.assertTrue(all(row["result"] == "DRY_RUN" for row in csv.DictReader(handle)))

    def test_scan_uses_one_panel_across_libraries_types_and_playlists(self):
        for live in (True, False):
            for playlist_supported in (True, False):
                with self.subTest(live=live, playlist_supported=playlist_supported), \
                        tempfile.TemporaryDirectory() as temp, \
                        mock.patch.dict(os.environ, {}, clear=True):
                    client = SyntheticClient()
                    client.titles["4"] = "한국 영화"
                    now = [0.0]
                    original_get = client.get

                    def get(path, params=None, extra_headers=None):
                        now[0] += 1
                        if path == "/library/sections/4/all":
                            self.assertEqual(progress.library_progress[:3], (1, 2, 0))
                        elif path == "/library/sections/5/all":
                            self.assertEqual(progress.library_progress[:3], (2, 2, 1))
                        elif path == "/playlists":
                            self.assertEqual(progress.library_progress, (0, 2, 2, 0))
                        if path.endswith("/all") and params["type"] == 18:
                            raise M.PlexApiError("GET", path, 404, "Optional type unavailable")
                        if path == "/playlists" and not playlist_supported:
                            raise M.PlexApiError("GET", path, 404, "Playlists unavailable")
                        if path == "/library/sections/5/all":
                            root = ET.Element("MediaContainer", size="1", totalSize="1")
                            root.append(client.element("4"))
                            return root, {}
                        return original_get(path, params=params, extra_headers=extra_headers)

                    client.get = get
                    stream = TtyStream() if live else io.StringIO()
                    progress = M.ProgressReporter(stream=stream, color=live, clock=lambda: now[0])
                    sections = [{"id": 4, "title": "TV", "type": "show"},
                                {"id": 5, "title": "Movies", "type": "show"}]
                    args = M.parse_args(["--page-size", "1"])
                    with mock.patch.object(M, "section_type_plan", return_value=([4, 18], {18})), \
                            mock.patch.object(progress, "_width", return_value=100):
                        stats, _, _, warnings = M.scan_api(
                            client, args, sections, Path(temp) / "plan.jsonl", [], progress)
                    expected = 4 if playlist_supported else 3
                    self.assertEqual(stats["total_api_rows"], expected)
                    self.assertEqual(stats["candidates"], expected)
                    self.assertEqual(len(warnings), 2 if playlist_supported else 3)
                    self.assertEqual(client.puts, [])
                    self.assertEqual(progress.task_started_at, 0)
                    output = stream.getvalue()
                    self.assertIn("Total scanned {0} · Candidates {0}".format(expected), output)
                    self.assertIn("100% · 2/2 processed", output)
                    if live:
                        self.assertIn("Library   1/2 · TV · Episode", output)
                        self.assertIn("Library   2/2 · Movies · Episode", output)
                        self.assertIn("Playlists · Playlist", output)
                        # Net cursor movement is exactly one six-row panel,
                        # including its closing newline, not one per query.
                        up = sum(int(n) for n in re.findall(r"\x1b\[(\d+)A", output))
                        self.assertEqual(output.count("\n") - up, 6)
                    else:
                        self.assertNotIn("\x1b", output)
                        self.assertNotIn("\r", output)
                        self.assertLessEqual(len(output.splitlines()), int(now[0] // 5) + 2)

    def test_scan_api_failure_flushes_pending_panel(self):
        client = SyntheticClient()
        original_get = client.get
        now = [0.0]

        def get(path, params=None, extra_headers=None):
            if path.endswith("/all") and extra_headers["X-Plex-Container-Start"] == "1":
                raise M.PlexApiError("GET", path, 500, "Synthetic scan failure")
            return original_get(path, params=params, extra_headers=extra_headers)

        client.get = get
        progress = M.ProgressReporter(stream=io.StringIO(), clock=lambda: now[0])
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(M.PlexApiError):
            M.scan_api(client, M.parse_args(["--metadata-type", "4", "--page-size", "1"]),
                       [{"id": 4, "title": "TV", "type": "show"}],
                       Path(temp) / "plan.jsonl", [], progress)
        self.assertIn("Total scanned 1 · Candidates 1", progress.stream.getvalue())
        self.assertEqual(progress.library_progress, (1, 1, 0, 0))
        self.assertNotIn("100% · 1/1 processed", progress.stream.getvalue())
        self.assertIsNone(progress.pending)
        self.assertEqual(client.puts, [])

    def test_library_total_respects_section_and_playlist_only_filters(self):
        cases = [(["--section", "4", "--metadata-type", "4"], 1, 2),
                 (["--metadata-type", "15"], 0, 1)]
        sections = [{"id": 9, "title": "Excluded", "type": "movie"},
                    {"id": 4, "title": "TV", "type": "show"}]
        for options, total, candidates in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                progress = M.ProgressReporter(stream=io.StringIO())
                client = SyntheticClient()
                stats, _, _, _ = M.scan_api(client, M.parse_args(options), sections,
                    Path(temp) / "plan.jsonl", [], progress)
                self.assertEqual(progress.library_progress[1:3], (total, total))
                self.assertEqual(stats["candidates"], candidates)
                self.assertFalse(any("/sections/9" in path for path in client.calls))
                output = progress.stream.getvalue()
                if total:
                    self.assertIn("Library   1/1 · TV", output)
                    self.assertIn("100% · 1/1 processed", output)
                    self.assertNotIn("/playlists", client.calls)
                else:
                    self.assertIn("Libraries 0 selected", output)
                    self.assertIn("Current   Playlists", output)
                    self.assertNotIn("1/0", output)
                    self.assertFalse(any("/library/" in path for path in client.calls))

    def test_unknown_and_unsupported_libraries_are_processed_but_marked_skipped(self):
        sections = [{"id": 4, "title": "Unknown", "type": "unknown"},
                    {"id": 5, "title": "Unsupported", "type": "unknown"}]
        client = mock.Mock()
        client.get.side_effect = M.PlexApiError("GET", "/synthetic", 404, "Unsupported")
        progress = M.ProgressReporter(stream=io.StringIO())
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
                M, "section_type_plan", side_effect=[([], set()), ([18], {18})]):
            stats, _, _, warnings = M.scan_api(client, M.parse_args(["--no-playlists"]),
                sections, Path(temp) / "plan.jsonl", [], progress)
        self.assertEqual(progress.library_progress, (2, 2, 2, 2))
        self.assertEqual(len(warnings), 2)
        self.assertEqual(stats["total_api_rows"], 0)
        self.assertIn("100% · 2/2 processed · Skipped 2", progress.stream.getvalue())
        client.put.assert_not_called()

    def test_no_selected_libraries_does_not_report_completion_percentage(self):
        progress = M.ProgressReporter(stream=io.StringIO())
        client = mock.Mock()
        with tempfile.TemporaryDirectory() as temp:
            M.scan_api(client, M.parse_args(["--no-playlists"]), [],
                       Path(temp) / "plan.jsonl", [], progress)
        self.assertIn("Libraries 0 selected", progress.stream.getvalue())
        self.assertNotIn("%", progress.stream.getvalue())
        client.get.assert_not_called()
        client.put.assert_not_called()

    def test_backup_progress_finishes_before_any_put_and_all_updates_verify(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_dir = self.create_databases(root)
            backups = root / "backups"
            client = SyntheticClient()

            def check_backup():
                manifests = list(backups.glob("*/" + M.BACKUP_MANIFEST_NAME))
                self.assertEqual(len(manifests), 1)
                M.load_backup_manifest(manifests[0])

            client.before_put = check_backup
            code, report, stderr = self.run_main(client, ["--apply", "--db-dir", str(db_dir),
                "--backup-dir", str(backups), "--color", "always"])
            self.assertEqual(code, 0)
            self.assertEqual(report["apply"]["verified"], 3)
            self.assertEqual(report["apply"]["put_succeeded"], 3)
            self.assertIn("Copying pages", stderr)
            self.assertIn("Checking SHA-256", stderr)
            self.assertLess(stderr.index("File 2/2 verified"), stderr.index("[4/4]"))
            self.assertIn("100% · 3/3 items", stderr)
            self.assertNotIn("\x1b", stderr)
            code, rerun, _ = self.run_main(client, ["--dry-run", "--quiet-progress"])
            self.assertEqual(code, 0)
            self.assertEqual(rerun["planned_changes"]["total"], 0)

    def test_individual_puts_update_progress_before_batch_verification(self):
        client = SyntheticClient()
        items = [candidate_item()]
        observed = []
        stats = Counter()
        M.apply_batch(client, items, None, stats, mock.Mock(),
                      on_progress=lambda stage: observed.append((stage, dict(stats))))
        sent = next(values for stage, values in observed if "Awaiting verification" in stage)
        self.assertEqual(sent["put_succeeded"], 1)
        self.assertEqual(sent.get("verified", 0), 0)
        self.assertEqual(observed[-1][1]["verified"], 1)

    def test_failure_and_stale_skip_are_completed_not_successful(self):
        stats = Counter()
        snapshots = []
        client = mock.Mock()
        client.put.side_effect = M.PlexApiError("PUT", "/synthetic", 500, "synthetic failure")
        with mock.patch.object(M, "fetch_candidate_detail", side_effect=[
            metadata_element("Changed"), metadata_element("한글", "한글")
        ]):
            M.apply_batch(client, [candidate_item(), candidate_item()], None, stats, mock.Mock(),
                          on_progress=lambda stage: snapshots.append(dict(stats)))
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["skipped_before_put"], 1)
        self.assertEqual(stats["verified"], 0)
        progress = M.ProgressReporter(stream=io.StringIO())
        progress.applying(stats, 2, "Finished")
        self.assertIn("100%", progress.stream.getvalue())
        self.assertIn("Verified 0 · Failed 2", progress.stream.getvalue())
        self.assertNotEqual(M.apply_result_name(2, stats), "APPLY SUCCEEDED")

    def test_aborted_apply_retains_counters_in_saved_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            client = SyntheticClient()
            writer = mock.Mock()
            writer.writerow.side_effect = OSError("synthetic CSV full")
            with mock.patch.object(M, "open_csv_writer", return_value=(None, writer)):
                code, report, stderr = self.run_main(client, ["--apply", "--no-backup",
                    "--log-dir", temp, "--metadata-type", "4", "--section", "4"])
            self.assertEqual(code, 5)
            summary_file = next(Path(temp).glob("*/summary.json"))
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
            self.assertEqual(summary["apply_stats"]["put_succeeded"], 2)
            self.assertEqual(summary["apply_stats"]["verified"], 1)
            self.assertEqual(summary["apply_stats"]["run_errors"], 1)
            self.assertIn("PUT succeeded=2", report["error"])
            self.assertNotIn("APPLY SUCCEEDED", stderr)

    def test_logger_failure_after_put_does_not_miscount_request_failure(self):
        stats = Counter()
        logger = mock.Mock()
        logger.put_succeeded.side_effect = OSError("synthetic log full")
        with self.assertRaises(OSError):
            M.apply_batch(SyntheticClient(), [candidate_item()], None, stats, logger)
        self.assertEqual(stats["put_succeeded"], 1)
        self.assertEqual(stats["failed"], 0)

    def test_logging_failure_still_reports_actual_put_count_as_json(self):
        with tempfile.TemporaryDirectory() as temp:
            logger = mock.Mock()
            logger.failure_counts = Counter()
            logger.put_succeeded.side_effect = OSError("synthetic log full")
            logger.event.side_effect = OSError("synthetic log full")
            with mock.patch.object(M, "ApplyRunLogger", return_value=logger):
                code, report, _ = self.run_main(SyntheticClient(), ["--apply", "--no-backup",
                    "--log-dir", temp, "--metadata-type", "4", "--section", "4"])
            self.assertEqual(code, 5)
            self.assertIn("PUT succeeded=1", report["error"])
            self.assertIn("Diagnostic output also failed", report["error"])

    def test_backup_failure_with_progress_never_sends_a_put(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_dir = self.create_databases(root)
            client = SyntheticClient()
            with mock.patch.object(M, "online_backup_database", side_effect=OSError("synthetic copy failure")):
                code, report, stderr = self.run_main(client, ["--apply", "--db-dir", str(db_dir),
                    "--backup-dir", str(root / "backups")])
            self.assertEqual(code, 5)
            self.assertEqual(client.puts, [])
            self.assertNotIn("Backup completed", stderr)
            self.assertIn("no API changes", report["error"])

    def test_quiet_no_candidate_run_and_text_summary(self):
        client = SyntheticClient()
        client.titles = {key: "English" for key in client.titles}
        code, report, stderr = self.run_main(client, ["--dry-run", "--quiet-progress"])
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "NO CHANGES REQUIRED")
        self.assertEqual(stderr, "")
        output = io.StringIO()
        logger = mock.Mock(run_dir=Path("synthetic-logs"), failure_samples=[])
        with contextlib.redirect_stdout(output):
            M.print_apply_result(Counter(candidates=3), Counter(verified=2, failed=1),
                                 logger, None, None, elapsed=65)
        text = output.getvalue()
        self.assertIn("RESULT: APPLY PARTIALLY FAILED", text)
        self.assertIn("Elapsed: 01:05", text)
        self.assertLess(text.index("Artifacts"), text.index("Apply summary"))

    def test_header_totals_are_case_insensitive_and_unknown_totals_remain_unknown(self):
        root = ET.Element("MediaContainer", size="1")
        root.append(metadata_element("한글"))
        client = mock.Mock()
        client.get.return_value = (root, {"x-plex-container-total-size": "1"})
        info = {}
        self.assertEqual(len(list(M.iter_paged_elements(client, "/synthetic", {}, 50, info))), 1)
        self.assertEqual(info["total"], 1)
        client.get.return_value = (root, {})
        list(M.iter_paged_elements(client, "/synthetic", {}, 50, info))
        self.assertIsNone(info["total"])


if __name__ == "__main__":
    unittest.main()
