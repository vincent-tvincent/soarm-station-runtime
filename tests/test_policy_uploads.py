"""Uploads are exercised without model, ROS, serial, camera, or motor imports."""
import hashlib
import io
import os
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from soarm_station.policy_common import PolicyError
from soarm_station.policy_uploads import CHUNK_SIZE, SPACE_MARGIN, UploadStore, _rename_noreplace


def sha(data):
    return hashlib.sha256(data).hexdigest()


def workflow_rename(source_fd, name, destination_fd, destination):
    """Only emulate the primitive on macOS for orchestration unit tests.

    Native atomic/no-overwrite semantics are tested separately on Linux below.
    This helper is never production code and is not used for race tests.
    """
    try:
        os.stat(destination, dir_fd=destination_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(name, destination, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
    else:
        raise PolicyError("DESTINATION_EXISTS", "Destination already exists", 409)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "models"
        self.root.mkdir()
        self.source = {"id": "models", "path": str(self.root), "resolved_path": str(self.root), "identity": "disk-one", "state": "online", "online": True, "enabled": True, "allow_upload": True, "writable": True}
        self.scans = []
        self.inspection = None
        self.store = self.open_store()
        if sys.platform not in ("linux", "darwin"):
            self.rename_patch = patch("soarm_station.policy_uploads._rename_noreplace", workflow_rename)
            self.rename_patch.start()
        else:
            self.rename_patch = None

    def tearDown(self):
        self.store.close()
        if self.rename_patch:
            self.rename_patch.stop()
        self.temp.cleanup()

    def inspect(self, root):
        if self.inspection:
            return self.inspection(root)
        complete = (root / "config.json").is_file() and (root / "model.safetensors").is_file()
        return {"id": "model-" + root.name, "artifact_revision": "sha256:artifact", "complete": complete, "missing_files": [] if complete else ["config.json", "model.safetensors"], "state": "available" if complete else "incomplete"}

    def scan(self, source):
        self.scans.append(source)
        return {"id": "scan-1", "state": "queued"}

    def open_store(self):
        return UploadStore(self.base / "cache.sqlite3", source_lookup=lambda source: self.source if source == "models" else None, inspect_package=self.inspect, on_publish=self.scan)

    def create(self, files=None, **kwargs):
        files = files if files is not None else [{"path": "config.json", "size": 2}]
        return self.store.create({"source_id": "models", "destination_name": "new-model", "kind": "folder", "files": files, **kwargs})

    def chunk(self, record, index, offset, body):
        return self.store.put_chunk(record["id"], index, offset, body, sha(body))

    def stage(self, record):
        return self.root / ".station-uploads" / record["id"]

    def wait(self, record):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.store.get(record["id"])
            if current["state"] != "verifying":
                return current
            time.sleep(.01)
        self.fail("Background upload verification timed out")

    def complete_folder(self, name="new-model", wrapper="", expected_hash=None):
        files = [{"path": wrapper + "config.json", "size": 2}, {"path": wrapper + "model.safetensors", "size": 4}]
        if expected_hash:
            files[1]["sha256"] = expected_hash
        record = self.create(files, destination_name=name)
        self.chunk(record, 0, 0, b"{}")
        self.chunk(record, 1, 0, b"data")
        return record

    def upload_archive(self, data, filename="weights.zip", name="archive-model"):
        record = self.create([{"path": filename, "size": len(data)}], kind="archive", destination_name=name)
        for offset in range(0, len(data), CHUNK_SIZE):
            self.chunk(record, 0, offset, data[offset:offset + CHUNK_SIZE])
        self.store.finalize(record["id"])
        return self.wait(record)

    def test_journal_resume_and_idempotent_replay(self):
        body = b"abcd"
        record = self.create([{"path": "nested/file", "size": 8}])
        received = self.chunk(record, 0, 4, body)
        self.assertEqual(received["received_bytes"], 4)
        self.store.close()
        self.store = self.open_store()
        resumed = self.store.get(record["id"])
        self.assertEqual(resumed["kind"], "folder")
        self.assertEqual(resumed["files"][0]["chunks"], [{"offset": 4, "size": 4, "sha256": sha(body)}])
        self.assertEqual(self.chunk(record, 0, 4, body)["received_bytes"], 4)
        final = self.chunk(record, 0, 0, b"1234")
        self.assertEqual(final["received_bytes"], 8)
        self.assertNotIn("files", final)
        self.assertEqual([c["offset"] for c in self.store.get(record["id"])["files"][0]["chunks"]], [0, 4])
        self.assertNotIn("files", self.store.list()["uploads"][0])
        self.assertEqual(self.store.list()["uploads"][0]["kind"], "folder")
        with sqlite3.connect(self.base / "cache.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM policy_uploads").fetchone()[0], 1)

    def test_chunk_conflicts_hash_and_range(self):
        record = self.create([{"path": "file", "size": 8}])
        for body, digest in [(b"data", None), (b"data", "z" * 64), (b"data", sha(b"other"))]:
            with self.assertRaises(PolicyError):
                self.store.put_chunk(record["id"], 0, 0, body, digest)
        self.chunk(record, 0, 0, b"data")
        for offset, body in [(0, b"else"), (1, b"at"), (0, b"dat"), (3, b"zz")]:
            with self.assertRaises(PolicyError) as error:
                self.chunk(record, 0, offset, body)
            self.assertEqual(error.exception.status, 409)
        for index, offset, body in [(1, 0, b"x"), (0, -1, b"x"), (0, 8, b"x"), (0, 0, b""), (0, 0, b"x" * (CHUNK_SIZE + 1))]:
            with self.assertRaises(PolicyError):
                self.store.put_chunk(record["id"], index, offset, body, sha(body))

    def test_replay_detects_modified_disk_bytes(self):
        record = self.create([{"path": "file", "size": 4}])
        self.chunk(record, 0, 0, b"data")
        (self.stage(record) / "files/file").write_bytes(b"xxxx")
        with self.assertRaises(PolicyError) as error:
            self.chunk(record, 0, 0, b"data")
        self.assertEqual(error.exception.code, "CHUNK_CONFLICT")

    def test_incomplete_transfer_cannot_finalize(self):
        record = self.create([{"path": "file", "size": 8}])
        self.chunk(record, 0, 4, b"data")
        with self.assertRaises(PolicyError) as error:
            self.store.finalize(record["id"])
        self.assertEqual(error.exception.code, "UPLOAD_INCOMPLETE")
        self.assertFalse((self.root / "new-model").exists())

    def test_publish_complete_package_and_scan(self):
        record = self.complete_folder()
        response = self.store.finalize(record["id"])
        self.assertEqual(response["state"], "verifying")
        done = self.wait(record)
        self.assertEqual(done["state"], "cataloged", done)
        self.assertEqual(done["artifact_revision"], "sha256:artifact")
        self.assertEqual(done["model_id"], "model-new-model")
        self.assertEqual(done["scan_job"]["id"], "scan-1")
        self.assertEqual(self.scans, ["models"])
        self.assertEqual((self.root / "new-model/model.safetensors").read_bytes(), b"data")
        self.assertFalse(self.stage(record).exists())
        self.assertEqual(self.store.finalize(record["id"])["state"], "cataloged")
        self.assertEqual(self.store.cancel(record["id"])["state"], "cataloged")
        self.assertTrue((self.root / "new-model").exists())

    def test_folder_wrapper_and_zero_length_file(self):
        record = self.complete_folder(wrapper="download/")
        self.store.finalize(record["id"])
        self.assertEqual(self.wait(record)["state"], "cataloged")
        self.assertTrue((self.root / "new-model/config.json").exists())
        self.inspection = lambda root: {"complete": (root / "empty").is_file(), "artifact_revision": "sha256:empty"}
        record = self.create([{"path": "empty", "size": 0}], destination_name="empty-model")
        self.store.finalize(record["id"])
        self.assertEqual(self.wait(record)["state"], "cataloged")
        self.assertEqual((self.root / "empty-model/empty").stat().st_size, 0)

    def test_unsupported_but_complete_package_can_publish(self):
        record = self.complete_folder()
        self.inspection = lambda root: {"complete": True, "state": "unsupported", "supported": False, "selectable": False, "artifact_revision": "sha256:unsupported"}
        self.store.finalize(record["id"])
        self.assertEqual(self.wait(record)["state"], "cataloged")

    def test_complete_package_integrity_failure_is_not_publishable(self):
        for extra in [{"integrity_valid": False}, {"issues": [{"code": "MANIFEST_DIGEST_MISMATCH"}]}, {"issues": [{"code": "ARTIFACT_CHANGING"}]}, {"state": "invalid"}]:
            name = "invalid-" + str(len(self.store.list()["uploads"]))
            record = self.complete_folder(name=name)
            self.inspection = lambda root, extra=extra: {"complete": True, **extra}
            self.store.finalize(record["id"])
            done = self.wait(record)
            self.assertEqual(done["state"], "failed", done)
            self.assertEqual(done["error_code"], "PACKAGE_INTEGRITY_FAILED")
            self.assertTrue(self.stage(record).exists())
            self.assertFalse((self.root / name).exists())

    def test_chunk_receipt_inventory_is_bounded(self):
        record = self.create([{"path": "file", "size": 3}])
        with patch("soarm_station.policy_uploads.MAX_VERIFIED_CHUNKS", 1):
            self.chunk(record, 0, 0, b"x")
            self.assertEqual(self.chunk(record, 0, 0, b"x")["received_bytes"], 1)
            with self.assertRaises(PolicyError) as error:
                self.chunk(record, 0, 1, b"y")
            self.assertEqual(error.exception.code, "CHUNK_RECEIPT_LIMIT")

    def test_whole_file_and_durable_chunk_hashes_rechecked(self):
        record = self.complete_folder(expected_hash=sha(b"wrong"))
        self.store.finalize(record["id"])
        done = self.wait(record)
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["error_code"], "FILE_HASH_MISMATCH")
        record = self.complete_folder(name="modified")
        (self.stage(record) / "files/model.safetensors").write_bytes(b"xxxx")
        self.store.finalize(record["id"])
        done = self.wait(record)
        self.assertEqual(done["error_code"], "CHUNK_HASH_MISMATCH")
        self.assertFalse((self.root / "modified").exists())

    def test_incomplete_package_stays_hidden(self):
        record = self.create()
        self.chunk(record, 0, 0, b"{}")
        self.store.finalize(record["id"])
        done = self.wait(record)
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["error_code"], "INCOMPLETE_PACKAGE")
        self.assertFalse((self.root / "new-model").exists())
        self.assertTrue(self.stage(record).exists())

    def test_source_disabled_removed_or_identity_changed_pauses(self):
        record = self.create()
        changes = [{"enabled": False}, {"online": False}, {"state": "offline"}, {"allow_upload": False}, {"writable": False}, {"identity": "other-disk"}, {"path": str(self.base / "other")}]
        for change in changes:
            original = self.source.copy()
            self.source.update(change)
            with self.assertRaises(PolicyError):
                self.chunk(record, 0, 0, b"{}")
            self.assertEqual(self.store.get(record["id"])["state"], "paused")
            self.source = original
        self.assertEqual(self.chunk(record, 0, 0, b"{}")["state"], "uploading")
        self.source = None
        self.assertEqual(self.store.get(record["id"])["state"], "paused")

    def test_same_path_replaced_physical_directory_pauses(self):
        record = self.create()
        moved = self.base / "old-models"
        self.root.rename(moved)
        self.root.mkdir()
        with self.assertRaises(PolicyError):
            self.chunk(record, 0, 0, b"{}")
        self.assertEqual(self.store.get(record["id"])["state"], "paused")
        self.root.rmdir()
        moved.rename(self.root)
        self.assertEqual(self.chunk(record, 0, 0, b"{}")["state"], "uploading")

    def test_source_change_during_inspection_cannot_publish_and_can_resume(self):
        record = self.complete_folder()
        def pause(root):
            self.source["enabled"] = False
            return {"complete": True}
        self.inspection = pause
        self.store.finalize(record["id"])
        self.assertEqual(self.wait(record)["state"], "paused")
        self.assertFalse((self.root / "new-model").exists())
        self.inspection = None
        self.source["enabled"] = True
        self.store.finalize(record["id"])
        self.assertEqual(self.wait(record)["state"], "cataloged")

    def test_available_space_checked_at_create_chunk_and_verify(self):
        from collections import namedtuple
        Space = namedtuple("Space", "total used free")
        with patch("soarm_station.policy_uploads.shutil.disk_usage", return_value=Space(100, 100, 0)):
            with self.assertRaises(PolicyError) as error:
                self.create()
            self.assertEqual(error.exception.code, "INSUFFICIENT_SPACE")
        record = self.create()
        with patch("soarm_station.policy_uploads.shutil.disk_usage", return_value=Space(100, 100, SPACE_MARGIN)):
            with self.assertRaises(PolicyError):
                self.chunk(record, 0, 0, b"{}")
        self.assertEqual(self.store.get(record["id"])["received_bytes"], 0)
        record = self.complete_folder(name="space-model")
        self.inspection = lambda root: {"complete": True}
        original = self.store._space
        calls = []
        def disk_fills(root, needed=0):
            calls.append(needed)
            if len(calls) > 1:
                raise PolicyError("INSUFFICIENT_SPACE", "Disk filled", 409)
            return original(root, needed)
        with patch.object(self.store, "_space", disk_fills):
            self.store.finalize(record["id"])
            done = self.wait(record)
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["error_code"], "INSUFFICIENT_SPACE")
        self.assertFalse((self.root / "space-model").exists())

    def test_inventory_traversal_and_conflicts_rejected(self):
        for path in ["../file", "/file", "C:/file", "a\\b", "a/../file", "a//b", "./file", "a/", "."]:
            with self.subTest(path=path), self.assertRaises(PolicyError):
                self.create([{"path": path, "size": 1}])
        for name in ["../x", "a/b", "a\\b", ".", "..", ".station-uploads", ""]:
            with self.subTest(name=name), self.assertRaises(PolicyError):
                self.create(destination_name=name)
        for files in [[{"path": "a", "size": 1}, {"path": "a/b", "size": 1}], [{"path": "a", "size": 1}, {"path": "a", "size": 1}], [{"path": "a", "size": True}]]:
            with self.assertRaises(PolicyError):
                self.create(files)

    def test_symlink_parent_cannot_receive_bytes(self):
        record = self.create([{"path": "nested/file", "size": 4}])
        outside = self.base / "outside"
        outside.mkdir()
        (self.stage(record) / "files/nested").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, PolicyError)):
            self.chunk(record, 0, 0, b"data")
        self.assertFalse((outside / "file").exists())

    def test_symlink_staging_container_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / ".station-uploads").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.create()
        self.assertEqual(list(outside.iterdir()), [])

    def test_hard_link_in_staging_cannot_modify_existing_artifact(self):
        record = self.create([{"path": "file", "size": 4}])
        original = self.base / "existing-model"
        original.write_bytes(b"keep")
        os.link(original, self.stage(record) / "files/file")
        with self.assertRaises(PolicyError):
            self.chunk(record, 0, 0, b"data")
        self.assertEqual(original.read_bytes(), b"keep")

    def test_cancel_parent_swap_does_not_follow_foreign_tree(self):
        record = self.create()
        container = self.root / ".station-uploads"
        original_container = self.root / ".station-uploads-original"
        outside = self.base / "outside"
        outside.mkdir()
        foreign = outside / record["id"]
        foreign.mkdir()
        (foreign / "keep").write_text("foreign")
        remover = self.store._remove_tree_at
        swapped = []
        def swap(parent_fd, name, expected=None):
            if not swapped:
                swapped.append(True)
                container.rename(original_container)
                container.symlink_to(outside, target_is_directory=True)
            return remover(parent_fd, name, expected)
        with patch.object(self.store, "_remove_tree_at", swap):
            done = self.store.cancel(record["id"])
        self.assertEqual(done["state"], "canceled")
        self.assertEqual((foreign / "keep").read_text(), "foreign")
        self.assertFalse((original_container / record["id"]).exists())

    def test_cancel_cleans_only_owned_staging_and_is_durable(self):
        record = self.create()
        existing = self.root / "new-model"
        existing.mkdir()
        (existing / "keep").write_text("original")
        self.chunk(record, 0, 0, b"{}")
        done = self.store.cancel(record["id"])
        self.assertEqual(done["state"], "canceled")
        self.assertFalse(self.stage(record).exists())
        self.assertEqual((existing / "keep").read_text(), "original")
        self.store.close()
        self.store = self.open_store()
        self.assertEqual(self.store.get(record["id"])["state"], "canceled")

    def test_cancel_replaced_staging_reports_cleanup_error(self):
        record = self.create()
        original = self.stage(record)
        original.rename(original.with_name(original.name + "-original"))
        original.mkdir()
        (original / "keep").write_text("foreign")
        done = self.store.cancel(record["id"])
        self.assertIn("cleanup_error", done)
        self.assertEqual((original / "keep").read_text(), "foreign")

    def test_cancel_interrupts_verifier_before_publication(self):
        record = self.complete_folder()
        entered, released = threading.Event(), threading.Event()
        def inspect(root):
            entered.set()
            released.wait(2)
            return {"complete": True}
        self.inspection = inspect
        self.store.finalize(record["id"])
        self.assertTrue(entered.wait(2))
        result = []
        cancel = threading.Thread(target=lambda: result.append(self.store.cancel(record["id"])))
        cancel.start()
        self.assertTrue(self.store._cancel[record["id"]].wait(2))
        released.set()
        cancel.join(2)
        self.assertEqual(result[0]["state"], "canceled")
        self.assertFalse((self.root / "new-model").exists())
        self.assertFalse(self.stage(record).exists())

    def test_existing_destination_never_changes(self):
        existing = self.root / "new-model"
        existing.mkdir()
        (existing / "keep").write_text("original")
        with self.assertRaises(PolicyError) as error:
            self.complete_folder()
        self.assertEqual(error.exception.status, 409)
        record = self.complete_folder(name="race-model")
        destination = self.root / "race-model"
        destination.mkdir()
        (destination / "keep").write_text("race winner")
        self.store.finalize(record["id"])
        done = self.wait(record)
        self.assertEqual(done["error_code"], "DESTINATION_EXISTS")
        self.assertEqual((destination / "keep").read_text(), "race winner")
        self.assertEqual((existing / "keep").read_text(), "original")

    def test_valid_zip_tar_and_tgz_wrappers(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("download/config.json", b"{}")
            archive.writestr("download/model.safetensors", b"data")
        done = self.upload_archive(data.getvalue())
        self.assertEqual(done["state"], "cataloged", done)
        self.assertEqual(done["kind"], "archive")
        self.assertTrue((self.root / "archive-model/config.json").exists())
        for compression, filename in [("w", "weights.tar"), ("w:gz", "weights.tgz")]:
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode=compression) as archive:
                for path, body in [("download/config.json", b"{}"), ("download/model.safetensors", b"data")]:
                    member = tarfile.TarInfo(path)
                    member.size = len(body)
                    archive.addfile(member, io.BytesIO(body))
            done = self.upload_archive(data.getvalue(), filename, filename.replace(".", "-"))
            self.assertEqual(done["state"], "cataloged", done)

    def test_zip_traversal_symlink_and_duplicate_rejected(self):
        for name, mode in [("../escaped", None), ("/escaped", None), ("a/../../escaped", None), ("link", stat.S_IFLNK | 0o777)]:
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                info = zipfile.ZipInfo(name)
                if mode:
                    info.create_system = 3
                    info.external_attr = mode << 16
                archive.writestr(info, b"../outside")
            done = self.upload_archive(data.getvalue(), name="bad-" + str(len(self.store.list()["uploads"])))
            self.assertEqual(done["state"], "failed", done)
            self.assertFalse((self.base / "escaped").exists())
            self.assertFalse((self.root / done["destination_name"]).exists())
        data = io.BytesIO()
        import warnings
        with warnings.catch_warnings(), zipfile.ZipFile(data, "w") as archive:
            warnings.simplefilter("ignore")
            archive.writestr("config.json", "{}")
            archive.writestr("config.json", "{}")
        done = self.upload_archive(data.getvalue(), name="duplicate")
        self.assertEqual(done["error_code"], "UNSAFE_ARCHIVE")

    def test_tar_traversal_links_special_sparse_and_large_metadata_rejected(self):
        for path, kind in [("../escaped", tarfile.REGTYPE), ("symlink", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE), ("device", tarfile.CHRTYPE), ("fifo", tarfile.FIFOTYPE), ("sparse", tarfile.GNUTYPE_SPARSE)]:
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as archive:
                member = tarfile.TarInfo(path)
                member.type = kind
                member.linkname = "../../outside"
                archive.addfile(member)
            done = self.upload_archive(data.getvalue(), "bad.tar", "bad-tar-" + str(len(self.store.list()["uploads"])))
            self.assertEqual(done["state"], "failed", done)
            self.assertFalse((self.root / done["destination_name"]).exists())
        # A forged PAX size must be rejected before tarfile tries to read it.
        member = tarfile.TarInfo("pax")
        member.type = tarfile.XHDTYPE
        member.size = 128 * 1024 * 1024
        done = self.upload_archive(member.tobuf(), "metadata.tar", "metadata")
        self.assertEqual(done["error_code"], "ARCHIVE_LIMIT")

    def test_archive_disk_expansion_and_metadata_inventory_limits(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large", b"0" * (CHUNK_SIZE * 2))
        with patch("soarm_station.policy_uploads.MAX_PACKAGE_BYTES", CHUNK_SIZE):
            done = self.upload_archive(data.getvalue())
        self.assertEqual(done["error_code"], "ARCHIVE_LIMIT")
        with patch("soarm_station.policy_uploads.MAX_MEMBERS", 0):
            done = self.upload_archive(data.getvalue(), name="many")
        self.assertEqual(done["error_code"], "ARCHIVE_LIMIT")

    def test_interrupted_verifier_resumes_from_durable_journal(self):
        record = self.complete_folder()
        # Simulate process loss after finalize persisted its state but before
        # the work queue ran. Construction must recover pending verification.
        internal = self.store._load(record["id"])
        internal["state"] = "verifying"
        self.store._save(internal)
        self.store.close()
        self.store = self.open_store()
        done = self.wait(record)
        self.assertEqual(done["state"], "cataloged", done)

    def test_crash_after_rename_recovers_only_matching_inode(self):
        record = self.complete_folder()
        internal = self.store._load(record["id"])
        package = self.stage(record) / "files"
        info = package.stat()
        internal["_publication"] = {"physical": [info.st_dev, info.st_ino], "descriptor": {"complete": True, "artifact_revision": "sha256:artifact"}}
        internal["state"] = "verifying"
        self.store._save(internal)
        package.rename(self.root / "new-model")
        self.store.close()
        self.store = self.open_store()
        self.assertEqual(self.wait(record)["state"], "cataloged")
        self.assertTrue((self.root / "new-model/config.json").exists())


class NativePublicationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform in ("linux","darwin"), "Native atomic rename required; no unsafe fallback")
    def test_atomic_no_overwrite_preserves_concurrent_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "incoming").mkdir()
            (root / "incoming/new").write_text("new")
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                # An external writer wins after admission, immediately before
                # the primitive. Even an empty directory must not be replaced.
                (root / "destination").mkdir()
                existing = (root / "destination").stat().st_ino
                with self.assertRaises(PolicyError) as error:
                    _rename_noreplace(fd, "incoming", fd, "destination")
                self.assertEqual(error.exception.status, 409)
                self.assertEqual((root / "destination").stat().st_ino, existing)
                self.assertTrue((root / "incoming/new").exists())
                (root / "destination").rmdir()
                _rename_noreplace(fd, "incoming", fd, "destination")
                self.assertEqual((root / "destination/new").read_text(), "new")
            finally:
                os.close(fd)

    def test_platform_without_primitive_is_explicitly_unsupported(self):
        with patch("soarm_station.policy_uploads.sys.platform", "unsupported-test"):
            with self.assertRaises(PolicyError) as error:
                _rename_noreplace(-1, "a", -1, "b")
        self.assertEqual(error.exception.code, "ATOMIC_PUBLISH_UNSUPPORTED")
        self.assertEqual(error.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
