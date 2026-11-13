"""Tests for the parts of the doctor that differ between Windows and macOS.

Run with:  python -m unittest discover -s tests -v

Nothing here needs a Nextcloud client, a keychain, a server or the network: the
macOS keychain is exercised through a synthetic attribute table in the same
format the real keychain database uses, and the sync folder through temporary
directories.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
from unittest import mock

from ncsd import actions, config, creds, demo, detect, journal, names, platform


class PlatformTests(unittest.TestCase):
    def test_platform_name_matches_runtime(self):
        self.assertIn(platform.platform_name(), {"windows", "macos", "linux", "darwin"})

    def test_config_candidates_are_absolute(self):
        for candidate in platform.config_candidates():
            self.assertTrue(os.path.isabs(candidate.path), candidate)

    def test_tool_data_dir_is_created_outside_sync_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"NCSD_DATA_DIR": os.path.join(tmp, "data")}):
                self.assertTrue(os.path.isdir(platform.tool_data_dir()))

    def test_demo_paths_follow_the_platform_style(self):
        root = platform.demo_sync_root()
        if platform.is_windows():
            self.assertIn("\\", root)
        else:
            self.assertTrue(root.startswith("/"))

    def test_reveal_command_is_platform_specific(self):
        cmd = platform.reveal_command("/tmp/a.txt")
        if platform.is_macos():
            self.assertEqual(cmd[:2], ["open", "-R"])
        elif platform.is_windows():
            self.assertEqual(cmd[0], "explorer")


class FileProviderFolderTests(unittest.TestCase):
    """macOS FileProvider accounts store no localPath; the folder must be found."""

    def cloud_storage(self, *names: str) -> str:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        for name in names:
            os.makedirs(os.path.join(tmp, name))
        return tmp

    def test_folder_name_is_derived_from_server_and_user(self):
        self.assertEqual(
            config._fileprovider_folder_name("https://cloud.example.com/", "alex"),
            "Nextcloud-cloud.example.com-alex",
        )
        # a port is not part of the folder name
        self.assertEqual(
            config._fileprovider_folder_name("https://cloud.example.com:8443", "alex"),
            "Nextcloud-cloud.example.com-alex",
        )

    @unittest.skipUnless(platform.is_macos(), "FileProvider folders only exist on macOS")
    def test_display_name_folders_are_found(self):
        # The client may name the folder after the display name ("Mathias Gorf").
        root = self.cloud_storage("Nextcloud-cloud.example.com-Mathias Gorf", "iCloud Drive")
        account = config.Account(index=0, url="https://cloud.example.com/", user="alex",
                                 display_name="Mathias Gorf")
        with mock.patch.object(config, "MAC_CLOUD_STORAGE", root):
            folder = config.discover_fileprovider_folder(account, "some-domain")
        self.assertIsNotNone(folder)
        self.assertEqual(folder.local_path, os.path.join(root, "Nextcloud-cloud.example.com-Mathias Gorf"))
        self.assertEqual(folder.source, "fileprovider")
        self.assertEqual(folder.virtual_files_mode, "fileprovider")
        self.assertIn("some-domain", folder.note)

    @unittest.skipUnless(platform.is_macos(), "FileProvider folders only exist on macOS")
    def test_ambiguous_folders_are_not_guessed(self):
        root = self.cloud_storage(
            "Nextcloud-cloud.example.com-alex",
            "Nextcloud-cloud.example.com-alex-2",
        )
        account = config.Account(index=0, url="https://cloud.example.com/", user="someone-else")
        with mock.patch.object(config, "MAC_CLOUD_STORAGE", root):
            # Two folders for the same server and no exact match: refuse to guess.
            folder = config.discover_fileprovider_folder(account)
        self.assertIsNone(folder)

    @unittest.skipUnless(platform.is_macos(), "FileProvider folders only exist on macOS")
    def test_missing_folder_is_explained(self):
        root = self.cloud_storage("iCloud Drive")
        account = config.Account(index=0, url="https://cloud.example.com/", user="alex",
                                 file_provider_domain="abc")
        with mock.patch.object(config, "MAC_CLOUD_STORAGE", root):
            message = str(config.fileprovider_folder_missing_error(account))
        self.assertIn("Nextcloud-cloud.example.com-alex", message)
        self.assertIn("--account", message)

    def test_config_without_folders_falls_back_to_discovery(self):
        # The real macOS layout: account keys only, no Folders\1\localPath.
        text = (
            "[General]\nclientVersion=35.0.3\n\n[Accounts]\n"
            "0\\url=https://cloud.example.com/\n"
            "0\\dav_user=alex\n"
            "0\\displayName=Alex Example\n"
            "0\\fileProviderDomainIdentifier=domain-1\n"
            "0\\user=@Invalid()\n"
        )
        fd, path = tempfile.mkstemp(suffix=".cfg")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        account = config.load_config(path).account(0)
        self.assertEqual(account.user, "alex")
        self.assertEqual(account.file_provider_domain, "domain-1")
        if platform.is_macos():
            # Discovery may or may not find a real folder here; either way it must
            # never invent one, and the account must still load.
            for folder in account.folders:
                self.assertTrue(folder.exists)
        else:
            self.assertEqual(account.folders, [])


class KeychainTests(unittest.TestCase):
    """Parse the attribute table the way the keychain database stores it."""

    @staticmethod
    def record(service: str, account: str, pad: int = 0) -> bytes:
        svce = service.encode()
        acct = account.encode()
        return (
            b"20240827084733Z\x00"                     # created
            b"20261113070313Z\x00"                     # modified
            + bytes(pad)                                 # zeroed attribute(s)
            + struct.pack(">I", len(svce)) + svce + b"\x00"
            + bytes(4)                                   # empty attribute
            + struct.pack(">H", len(acct)) + acct + b"\x00"
        )

    def write_keychain(self, data: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".keychain-db")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"\x00" * 64 + data)
        self.addCleanup(os.unlink, path)
        return path

    def test_finds_every_layout(self):
        data = (
            self.record("Nextcloud", "alex_app-password:https://cloud.example.com/:0")
            + self.record("Nextcloud", "alex:https://cloud.example.com/:0", pad=4)
            + self.record("Nextcloud", "alex_app-password:https://other.example.com/:0", pad=8)
            + self.record("Other", "not-nextcloud")
        )
        found = creds._accounts_in_keychain(self.write_keychain(data), "Nextcloud")
        self.assertEqual(found, [
            "alex_app-password:https://cloud.example.com/:0",
            "alex:https://cloud.example.com/:0",
            "alex_app-password:https://other.example.com/:0",
        ])

    def test_ignores_other_services(self):
        data = self.record("Safari", "alex_app-password:https://cloud.example.com/:0")
        self.assertEqual(creds._accounts_in_keychain(self.write_keychain(data), "Nextcloud"), [])

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(creds._accounts_in_keychain("/nonexistent.keychain-db", "Nextcloud"), [])

    def test_app_password_outranks_login_credentials(self):
        host, user = "cloud.example.com", "alex"
        login = creds._rank("alex:https://cloud.example.com/:0", host, user)
        app = creds._rank("alex_app-password:https://cloud.example.com/:0", host, user)
        other_app = creds._rank("alex_app-password:https://other.example.com/:0", host, user)
        self.assertGreater(app, login)
        # Another server's app password must not win over this server's credentials.
        self.assertGreater(login, other_app)

    def test_explicit_password_skips_the_store(self):
        account = config.Account(index=0, url="https://cloud.example.com", user="alex")
        cred = creds.find_account_credential(account, explicit_password="secret")
        self.assertEqual((cred.token, cred.source), ("secret", "cli"))


class NameRuleTests(unittest.TestCase):
    def test_numbered_ds_store_copies_are_junk(self):
        for name in (".DS_Store", ".DS_Store 2", ".DS_Store 12", ".ds_store 3", "._.DS_Store", "._ 2.DS_Store"):
            self.assertTrue(names.is_junk_name(name), name)

    def test_conflict_copies_are_junk(self):
        self.assertTrue(names.is_junk_name("brief (conflicted copy 2026-11-13).md"))
        self.assertTrue(names.is_conflict_copy("brief.conflict"))

    def test_real_files_are_not_junk(self):
        self.assertFalse(names.is_junk_name("brief.md"))
        self.assertFalse(names.is_junk_name("DS_Store.pdf"))

    def test_mac_trash_paths(self):
        self.assertTrue(names.is_mac_trash_path(".Trash/report.pdf"))
        self.assertTrue(names.is_mac_trash_path("Documents/.Trash/Budget.xlsx"))
        self.assertFalse(names.is_mac_trash_path("Documents/Trash/report.pdf"))
        self.assertFalse(names.is_mac_trash_path(".Trash-1000/report.pdf"))

    def test_case_collisions_ignore_unicode_normalisation(self):
        composed = "M\u00fcller.pdf"          # "Müller.pdf" in NFC
        decomposed = "Mu\u0308ller.pdf"        # same name in NFD, as macOS stores it
        groups = names.case_collisions([composed, decomposed.lower()])
        self.assertEqual(len(groups), 1)

    def test_normalise_name_ignores_unicode_form(self):
        self.assertEqual(detect.normalise_name("M\u00fcller.pdf"), detect.normalise_name("Mu\u0308ller.PDF"))

    def test_windows_rules_still_apply(self):
        self.assertTrue(names.windows_problems("a*b.pdf"))
        self.assertTrue(names.windows_problems("CON.txt"))
        self.assertEqual(names.windows_problems("fine.pdf"), [])


class LocalCheckTests(unittest.TestCase):
    """The folder check must work without a journal, a log or a server."""

    def test_finds_names_that_would_break_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Documents", ".Trash"))
            for rel in ("Documents/report*.pdf", "Documents/CON.txt", "Documents/.DS_Store 3",
                        "Documents/.Trash/old.docx", "Documents/fine.pdf"):
                with open(os.path.join(tmp, rel), "wb") as fh:
                    fh.write(b"x")
            state = detect.local_name_problems(tmp)
            kinds = {p["path"]: p["kind"] for p in state["problems"]}
            self.assertEqual(kinds["Documents/report*.pdf"], "filename_invalid")
            self.assertEqual(kinds["Documents/CON.txt"], "filename_invalid")
            self.assertEqual(kinds["Documents/.DS_Store 3"], "junk")
            self.assertEqual(kinds["Documents/.Trash/old.docx"], "mac_trash")
            self.assertNotIn("Documents/fine.pdf", kinds)
            self.assertTrue(state["complete"])

    def test_ignored_dependency_folders_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Scripts", "app", "venv", "lib"))
            with open(os.path.join(tmp, "Scripts", "app", "venv", "lib", "bad*.py"), "wb") as fh:
                fh.write(b"x")
            os.makedirs(os.path.join(tmp, "web", "node_modules"))
            with open(os.path.join(tmp, "web", "node_modules", "CON.js"), "wb") as fh:
                fh.write(b"x")
            with open(os.path.join(tmp, "Scripts", "app", "run*.sh"), "wb") as fh:
                fh.write(b"x")
            state = detect.local_name_problems(tmp)
            paths = [p["path"] for p in state["problems"]]
            self.assertEqual(paths, ["Scripts/app/run*.sh"])
            self.assertEqual(state["files"], 1)

    def test_ignored_dir_names(self):
        for name in ("venv", ".venv", "node_modules", "__pycache__", ".git", "foo.egg-info"):
            self.assertTrue(names.is_ignored_dir_name(name), name)
        for name in ("Documents", "Photos", "Scripts", "venv-notes"):
            self.assertFalse(names.is_ignored_dir_name(name), name)

    def test_missing_folder_is_reported(self):
        state = detect.local_name_problems("/nonexistent/folder")
        self.assertFalse(state["complete"])
        self.assertEqual(state["problems"], [])


class JournalTests(unittest.TestCase):
    def test_missing_journal_explains_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            message = journal.journal_unavailable_reason(os.path.join(tmp, ".sync_none.db"))
            self.assertIn(".sync_none.db", message)
            if platform.is_macos():
                self.assertIn("Full Disk Access", message)
            with self.assertRaises(journal.JournalError):
                journal.load(os.path.join(tmp, ".sync_none.db"), os.path.join(tmp, "snap"))

    def test_snapshot_and_read(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, ".sync_test.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE metadata (path, type, fileid, filesize, modtime, md5, contentChecksum, remotePerm)")
            con.execute("INSERT INTO metadata VALUES ('a/b.txt', 0, '0001x', 12, 1700000000, 'etag', '', 'RGDNVW')")
            con.execute("CREATE TABLE blacklist (path, lastTryEtag, lastTryModtime, retrycount, errorstring, "
                        "lastTryTime, ignoreDuration, renameTarget, errorCategory, requestId)")
            con.execute("INSERT INTO blacklist VALUES ('a/b.txt', '', 0, 3, 'boom', 0, 0, '', 1, '')")
            con.commit()
            con.close()

            j = journal.load(db, os.path.join(tmp, "snap"))
            self.assertIn("a/b.txt", j.rows)
            self.assertEqual(j.blacklist[0].errorstring, "boom")
            self.assertEqual(j.blacklist[0].errorcategory, "NormalError")
            self.assertFalse(os.path.exists(j.snapshot_path), "snapshot should be removed")


class DetectionTests(unittest.TestCase):
    def make_folder(self, root: str) -> config.SyncFolder:
        return config.SyncFolder(index=0, local_path=root, target_path="/", journal_name=".sync_x.db")

    def test_local_side_reports_mac_attributes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.pdf")
            with open(path, "wb") as fh:
                fh.write(b"data")
            side = detect.local_side(tmp, "report.pdf")
            self.assertTrue(side.exists)
            if platform.is_macos():
                self.assertIsInstance(side.attributes, dict)
            else:
                self.assertEqual(side.attributes, {})

    def test_mac_trash_is_classified_with_local_actions(self):
        issue = detect.Issue(kind="mac_trash", key="k", path=".Trash/x.pdf")
        self.assertEqual(
            detect._actions_for("mac_trash", issue.local, None, has_remote=False),
            detect.MAC_TRASH_ACTIONS,
        )

    def test_journal_blacklist_in_trash_becomes_mac_trash(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = journal.Journal(db_path="", snapshot_path="")
            j.blacklist.append(journal.BlacklistRow(path=".Trash/old.pdf", errorstring="Not found"))
            folder = self.make_folder(tmp)
            options = detect.ScanOptions(include_logs=False, include_conflict_copies=False)
            issues = detect.collect(None, folder, j, None, options)
            kinds = {i.path: i.kind for i in issues}
            self.assertEqual(kinds.get(".Trash/old.pdf"), "mac_trash")

    def test_conflict_copy_is_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "brief (conflicted copy 2026-11-13).md"), "wb").close()
            j = journal.Journal(db_path="", snapshot_path="")
            j.rows["brief (conflicted copy 2026-11-13).md"] = journal.JournalRow(path="x")
            folder = self.make_folder(tmp)
            options = detect.ScanOptions(include_logs=False, include_journal=True)
            issues = detect.collect(None, folder, j, None, options)
            copies = [i for i in issues if "conflicted copy" in i.path]
            self.assertTrue(copies)
            self.assertEqual(copies[0].kind, "conflict_copy")
            self.assertEqual(copies[0].severity, "noise")


class ActionTests(unittest.TestCase):
    def make_resolver(self, root: str) -> actions.Resolver:
        folder = config.SyncFolder(index=0, local_path=root, target_path="/", journal_name="")
        store = type("Store", (), {"settings": {}, "dismiss": lambda *a, **k: None,
                                   "audit": lambda *a, **k: None})()
        dav = type("Dav", (), {"stat": lambda self, rel: None})()
        return actions.Resolver(None, folder, dav, store)

    def test_invalid_name_message_mentions_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = self.make_resolver(tmp)
            with self.assertRaises(actions.ActionError) as ctx:
                resolver._validate_new_name("a*b.pdf", "a*b.pdf")
            message = str(ctx.exception)
            self.assertIn("Windows", message)
            if platform.is_macos():
                self.assertIn("Mac", message)

    def test_delete_local_removes_empty_mac_trash(self):
        with tempfile.TemporaryDirectory() as tmp:
            trash = os.path.join(tmp, ".Trash")
            os.makedirs(trash)
            target = os.path.join(trash, "old.pdf")
            with open(target, "wb") as fh:
                fh.write(b"x")
            resolver = self.make_resolver(tmp)
            resolver._backup_local = lambda rel: ""
            outcome = resolver.apply("delete_local", ".Trash/old.pdf")
            self.assertFalse(os.path.exists(target))
            if platform.is_macos():
                self.assertFalse(os.path.exists(trash), "empty .Trash folder should be removed")
                self.assertTrue(outcome.details.get("removed_empty_trash"))


class DemoTests(unittest.TestCase):
    def test_demo_context_uses_platform_paths(self):
        cfg, account, folder = demo.build_context()
        self.assertEqual(folder.local_path, platform.demo_sync_root())
        self.assertEqual(folder.virtual_files_mode, platform.demo_vfs_mode())

    def test_demo_scan_hides_noise_by_default(self):
        session = demo.build_session()
        visible = session.scan()
        self.assertTrue(all(i.severity != "noise" for i in visible))
        with_junk = session.scan(include_junk=True)
        self.assertGreater(len(with_junk), len(visible))
        self.assertTrue(any(i.kind == "mac_trash" for i in visible))


if __name__ == "__main__":
    unittest.main()
