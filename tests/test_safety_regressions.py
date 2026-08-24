import argparse
import errno
import importlib.util
import sqlite3
import tempfile
import unicodedata
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plex_nfkd_title_sort_api.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("plex_nfkd_title_sort_api", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_script_module()


def metadata_element(title, title_sort=None, locked=False):
    attributes = {"ratingKey": "1", "title": title, "type": "episode"}
    if title_sort is not None:
        attributes["titleSort"] = title_sort
    element = ET.Element("Video", attributes)
    if locked:
        ET.SubElement(element, "Field", {"name": "titleSort", "locked": "1"})
    return element


def candidate_item(title="한글", title_sort="한글", locked=False):
    item = {
        "metadata_id": "1",
        "library_section_id": "4",
        "library_name": "TV",
        "metadata_type": 4,
        "metadata_type_name": "Episode",
        "api_kind": "library",
        "title": title,
        "title_sort": title_sort,
        "title_sort_locked": locked,
        "parent_id": "",
        "parent_title": "",
        "grandparent_id": "",
        "grandparent_title": "",
        "index": "",
        "parent_index": "",
    }
    return MODULE.plan_candidate(item, ["the", "a", "an"])


class SortKeyTests(unittest.TestCase):
    def test_existing_full_title_is_sanitized(self):
        plan = MODULE.make_sort_key_plan("The 한글 쇼", "The 한글 쇼", ["the", "a"])
        self.assertEqual(plan["sort_base"], "한글 쇼")
        self.assertEqual(
            plan["target_title_sort"], unicodedata.normalize("NFKD", "한글 쇼")
        )
        self.assertEqual(plan["sort_key_source"], "SANITIZED_EXISTING_TITLE_SORT")
        self.assertEqual(plan["removed_prefix"], "The ")

    def test_leading_punctuation_in_existing_sort_is_sanitized(self):
        plan = MODULE.make_sort_key_plan("...좋겠다", "...좋겠다", ["the", "a"])
        self.assertEqual(plan["sort_base"], "좋겠다")
        self.assertEqual(plan["removed_prefix"], "...")

    def test_meaningful_custom_sort_is_preserved(self):
        plan = MODULE.make_sort_key_plan("The Matrix", "Matrix, The", ["the", "a"])
        self.assertEqual(plan["sort_base"], "Matrix, The")
        self.assertEqual(plan["sort_key_source"], "EXISTING_TITLE_SORT")


class RecordingLogger:
    def __init__(self):
        self.failures = []

    def failed(self, item, stage, error_code, error, **kwargs):
        self.failures.append((stage, error_code, error, kwargs))

    def put_succeeded(self, item):
        raise AssertionError("PUT must not be reported for a stale plan")

    def verified(self, item, diagnostics):
        raise AssertionError("A stale plan must not be verified")

    def event(self, *args, **kwargs):
        pass


class RecordingClient:
    def __init__(self):
        self.puts = []

    def put(self, path, params=None):
        self.puts.append((path, params))


class PreApplySafetyTests(unittest.TestCase):
    def test_redundant_omission_is_the_same_snapshot(self):
        candidate = candidate_item(title="한글", title_sort=None, locked=False)
        current, error_code, _, _ = MODULE.verify_planned_state(
            candidate, metadata_element("한글", title_sort=None, locked=False)
        )
        self.assertTrue(current)
        self.assertEqual(error_code, "")

    def test_changed_title_is_skipped_before_put(self):
        candidate = candidate_item()
        client = RecordingClient()
        logger = RecordingLogger()
        stats = Counter()
        changed = metadata_element("Changed title", "한글", locked=False)

        with mock.patch.object(MODULE, "fetch_candidate_detail", return_value=changed):
            MODULE.apply_batch(client, [candidate], None, stats, logger)

        self.assertEqual(client.puts, [])
        self.assertEqual(candidate["result"], "SKIPPED_STALE_PLAN")
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["skipped_before_put"], 1)
        self.assertEqual(stats["failure:STALE_PLAN"], 1)
        self.assertEqual(logger.failures[0][0:2], ("PRE_APPLY_CHECK", "STALE_PLAN"))


class RestoreReachabilityTests(unittest.TestCase):
    def test_connection_refused_is_treated_as_stopped(self):
        refusal = urllib.error.URLError(
            ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")
        )
        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=refusal):
            self.assertFalse(MODULE.plex_server_is_reachable("http://127.0.0.1:9"))

    def test_timeout_blocks_restore_instead_of_assuming_stopped(self):
        timeout = urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=timeout):
            with self.assertRaisesRegex(RuntimeError, "failed ambiguously"):
                MODULE.plex_server_is_reachable("http://127.0.0.1:32400")


class RestoreRollbackTests(unittest.TestCase):
    def create_database_pair(self, db_dir, value):
        db_dir.mkdir(parents=True, exist_ok=True)
        for name in (MODULE.MAIN_DB_NAME, MODULE.BLOBS_DB_NAME):
            connection = sqlite3.connect(str(db_dir / name))
            try:
                connection.execute("CREATE TABLE marker (value TEXT)")
                connection.execute("INSERT INTO marker VALUES (?)", (value,))
                connection.commit()
            finally:
                connection.close()

    def set_database_pair_value(self, db_dir, value):
        for name in (MODULE.MAIN_DB_NAME, MODULE.BLOBS_DB_NAME):
            connection = sqlite3.connect(str(db_dir / name))
            try:
                connection.execute("UPDATE marker SET value = ?", (value,))
                connection.commit()
            finally:
                connection.close()

    def read_database_pair(self, db_dir):
        values = []
        for name in (MODULE.MAIN_DB_NAME, MODULE.BLOBS_DB_NAME):
            connection = sqlite3.connect(str(db_dir / name))
            try:
                values.append(connection.execute("SELECT value FROM marker").fetchone()[0])
            finally:
                connection.close()
        return values

    def test_second_replace_failure_rolls_back_both_databases_and_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            db_dir = root / "plex-data" / "Plug-in Support" / "Databases"
            self.create_database_pair(db_dir, "backup")
            backup_args = argparse.Namespace(
                backup_dir=root / "backups",
                plex_url="http://127.0.0.1:32400",
            )
            bundle, _ = MODULE.create_backup_bundle(backup_args, db_dir, {})

            self.set_database_pair_value(db_dir, "current")
            sidecars = {}
            for database_name in (MODULE.MAIN_DB_NAME, MODULE.BLOBS_DB_NAME):
                for suffix in ("-wal", "-shm"):
                    name = database_name + suffix
                    payload = ("current-" + name).encode("utf-8")
                    (db_dir / name).write_bytes(payload)
                    sidecars[name] = payload

            restore_args = argparse.Namespace(
                plex_url="http://127.0.0.1:32400",
                timeout=1.0,
                restore_backup=bundle,
                db_dir=db_dir,
                preferences=None,
                allow_plex_url_mismatch=False,
            )
            original_replace = MODULE.os.replace
            failure_injected = {"value": False}

            def fail_once_on_blobs_install(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    not failure_injected["value"]
                    and source_path.name.endswith(".restore-partial")
                    and destination_path.name == MODULE.BLOBS_DB_NAME
                ):
                    failure_injected["value"] = True
                    raise OSError("simulated second database replace failure")
                return original_replace(source, destination)

            with mock.patch.object(MODULE, "plex_server_is_reachable", return_value=False):
                with mock.patch.object(MODULE.os, "replace", side_effect=fail_once_on_blobs_install):
                    with self.assertRaisesRegex(RuntimeError, "restored automatically"):
                        MODULE.restore_backup_bundle(restore_args)

            self.assertTrue(failure_injected["value"])
            for name, payload in sidecars.items():
                self.assertEqual((db_dir / name).read_bytes(), payload)
            self.assertEqual(self.read_database_pair(db_dir), ["current", "current"])
            self.assertEqual(list(db_dir.glob("*partial")), [])

    def test_staging_failure_leaves_active_databases_unchanged_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            db_dir = root / "plex-data" / "Plug-in Support" / "Databases"
            self.create_database_pair(db_dir, "backup")
            backup_args = argparse.Namespace(
                backup_dir=root / "backups",
                plex_url="http://127.0.0.1:32400",
            )
            bundle, _ = MODULE.create_backup_bundle(backup_args, db_dir, {})
            self.set_database_pair_value(db_dir, "current")
            restore_args = argparse.Namespace(
                plex_url="http://127.0.0.1:32400",
                timeout=1.0,
                restore_backup=bundle,
                db_dir=db_dir,
                preferences=None,
                allow_plex_url_mismatch=False,
            )
            original_copy = MODULE.copy_file_with_fsync
            failure_injected = {"value": False}

            def fail_after_second_stage_copy(source, destination):
                original_copy(source, destination)
                if (
                    not failure_injected["value"]
                    and Path(destination).name
                    == MODULE.BLOBS_DB_NAME + ".restore-partial"
                ):
                    failure_injected["value"] = True
                    raise OSError("simulated staging fsync failure")

            with mock.patch.object(MODULE, "plex_server_is_reachable", return_value=False):
                with mock.patch.object(
                    MODULE,
                    "copy_file_with_fsync",
                    side_effect=fail_after_second_stage_copy,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "before active databases were changed"
                    ):
                        MODULE.restore_backup_bundle(restore_args)

            self.assertTrue(failure_injected["value"])
            self.assertEqual(self.read_database_pair(db_dir), ["current", "current"])
            self.assertEqual(list(db_dir.glob("*partial")), [])

    def test_successful_restore_installs_both_backup_databases(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            db_dir = root / "plex-data" / "Plug-in Support" / "Databases"
            self.create_database_pair(db_dir, "backup")
            backup_args = argparse.Namespace(
                backup_dir=root / "backups",
                plex_url="http://127.0.0.1:32400",
            )
            bundle, _ = MODULE.create_backup_bundle(backup_args, db_dir, {})
            self.set_database_pair_value(db_dir, "current")
            restore_args = argparse.Namespace(
                plex_url="http://127.0.0.1:32400",
                timeout=1.0,
                restore_backup=bundle,
                db_dir=db_dir,
                preferences=None,
                allow_plex_url_mismatch=False,
            )

            with mock.patch.object(MODULE, "plex_server_is_reachable", return_value=False):
                MODULE.restore_backup_bundle(restore_args)

            self.assertEqual(self.read_database_pair(db_dir), ["backup", "backup"])
            self.assertEqual(list(db_dir.glob("*partial")), [])

    def test_manifest_url_mismatch_is_blocked_before_reachability_check(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            db_dir = root / "plex-data" / "Plug-in Support" / "Databases"
            self.create_database_pair(db_dir, "backup")
            backup_args = argparse.Namespace(
                backup_dir=root / "backups",
                plex_url="http://127.0.0.1:32400",
            )
            bundle, _ = MODULE.create_backup_bundle(backup_args, db_dir, {})
            restore_args = argparse.Namespace(
                plex_url="http://127.0.0.1:9999",
                timeout=1.0,
                restore_backup=bundle,
                db_dir=db_dir,
                preferences=None,
                allow_plex_url_mismatch=False,
            )

            with mock.patch.object(MODULE, "plex_server_is_reachable") as reachable:
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    MODULE.restore_backup_bundle(restore_args)
            reachable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
