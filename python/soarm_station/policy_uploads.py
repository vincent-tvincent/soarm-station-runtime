"""Durable, hardware-free model uploads and no-overwrite publication.

The database is a transfer journal, never a catalog or a model loader. Bytes stay
inside a source's hidden staging directory until the package inspector accepts
them. All sizes are bounded and file bodies are streamed.
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from .policy_common import PolicyError, identifier, relative_path

CHUNK_SIZE = 128 * 1024
MAX_FILES = 4096
MAX_MEMBERS = 8192
MAX_PACKAGE_BYTES = 256 * 1024**3
MAX_VERIFIED_CHUNKS = MAX_PACKAGE_BYTES // CHUNK_SIZE + MAX_FILES
MAX_ARCHIVE_METADATA = 16 * 1024**2
SPACE_MARGIN = 16 * 1024**2
_HEX = re.compile(r"[0-9a-fA-F]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class _Paused(Exception):
    pass


class _Canceled(Exception):
    pass


class _BoundedTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # tarfile processes PAX/GNU metadata before yielding a member. Bound
        # those headers here, before its parser can read an attacker-sized one.
        archive._station_header_count = getattr(archive, "_station_header_count", 0) + 1
        if archive._station_header_count > MAX_MEMBERS or self.size < 0 or self.size > MAX_PACKAGE_BYTES:
            raise PolicyError("ARCHIVE_LIMIT", "TAR header inventory exceeds package limits", 413)
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise PolicyError("UNSAFE_ARCHIVE", "Sparse TAR files are not accepted")
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK) and self.size > 65536:
            raise PolicyError("ARCHIVE_LIMIT", "TAR extended metadata exceeds 64 KiB", 413)
        return super()._proc_member(archive)


def _path(value):
    value = relative_path(value)
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise PolicyError("INVALID_PATH", "File paths must contain normal relative components")
    return value


def _digest(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise PolicyError("INVALID_SHA256", "SHA256 must be 64 hexadecimal characters")
    return value.lower()


def _physical(info):
    return [info.st_dev, info.st_ino]


def _json_identity(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _rename_noreplace(source_fd, source_name, destination_fd, destination_name):
    """Atomic directory publication; deliberately no unsafe rename fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    # Darwin's checked SDK sys/stdio.h declares renameatx_np + RENAME_EXCL=4;
    # this supports the hardware-free macOS preview with the same no-overwrite guarantee.
    name, flag = ("renameat2", 1) if sys.platform == "linux" else ("renameatx_np", 4) if sys.platform == "darwin" else ("", 0)
    rename = getattr(libc, name, None) if name else None
    if rename is None:
        raise PolicyError("ATOMIC_PUBLISH_UNSUPPORTED", "This platform has no renameat2 no-overwrite support", 503)
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source_name), destination_fd, os.fsencode(destination_name), flag):
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise PolicyError("DESTINATION_EXISTS", "The destination already exists; uploads never overwrite artifacts", 409)
        if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise PolicyError("ATOMIC_PUBLISH_UNSUPPORTED", "The source filesystem does not support atomic no-overwrite publication", 503)
        raise OSError(code, os.strerror(code))


class UploadStore:
    def __init__(self, cache_db: Path, *, source_lookup, inspect_package, on_publish=None):
        self.source_lookup = source_lookup
        self.inspect_package = inspect_package
        self.on_publish = on_publish
        self._db_lock = threading.RLock()
        self._locks = {}
        self._cancel = {}
        self._stopping = threading.Event()
        self._closed = False
        cache_db = Path(cache_db)
        cache_db.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(cache_db, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS policy_uploads (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS policy_upload_chunks (upload_id TEXT NOT NULL, file_index INTEGER NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY (upload_id,file_index,offset))")
        self._db.commit()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-upload")
        self._futures = {}
        for record in self._all():
            self._locks[record["id"]] = threading.RLock()
            self._cancel[record["id"]] = threading.Event()
            if record["state"] == "verifying":
                self._futures[record["id"]] = self._executor.submit(self._verify, record["id"])

    def _all(self):
        with self._db_lock:
            return [json.loads(row[0]) for row in self._db.execute("SELECT record FROM policy_uploads ORDER BY rowid")]

    def _load(self, upload_id, *, include_chunks=True):
        identifier(upload_id, "Upload ID")
        with self._db_lock:
            row = self._db.execute("SELECT record FROM policy_uploads WHERE id=?", (upload_id,)).fetchone()
        if row is None:
            raise PolicyError("UPLOAD_NOT_FOUND", "Upload was not found", 404)
        record = json.loads(row[0])
        if include_chunks:
            with self._db_lock:
                for index, offset, size, digest in self._db.execute("SELECT file_index,offset,size,sha256 FROM policy_upload_chunks WHERE upload_id=? ORDER BY file_index,offset", (upload_id,)):
                    record["files"][index]["chunks"].append({"offset": offset, "size": size, "sha256": digest})
        return record

    def _save(self, record, *, new_chunk=None):
        record["updated_at_ns"] = time.time_ns()
        metadata = {**record, "files": [{**file, "chunks": []} for file in record["files"]]}
        with self._db_lock:
            with self._db:
                if new_chunk is not None:
                    index, chunk = new_chunk
                    self._db.execute("INSERT INTO policy_upload_chunks VALUES (?,?,?,?,?)", (record["id"], index, chunk["offset"], chunk["size"], chunk["sha256"]))
                self._db.execute("INSERT OR REPLACE INTO policy_uploads VALUES (?,?)", (record["id"], json.dumps(metadata, separators=(",", ":"))))

    @staticmethod
    def _public(record):
        return {key: value for key, value in record.items() if not key.startswith("_")}

    @classmethod
    def _summary(cls, record):
        result = cls._public(record)
        files = result.pop("files", [])
        result["file_count"] = len(files)
        result["verified_chunk_count"] = record.get("verified_chunk_count", sum(len(file["chunks"]) for file in files))
        result["progress"] = {"done": record["received_bytes"], "total": record["total_bytes"]}
        return result

    def _source(self, source_id):
        try:
            source = self.source_lookup(source_id)
        except (KeyError, OSError, PolicyError) as exc:
            raise _Paused("The admitted source is unavailable") from exc
        if not source or not source.get("enabled", True):
            raise _Paused("The admitted source was removed or disabled")
        if source.get("state", "online") != "online" or not source.get("online", True):
            raise _Paused("The admitted source is offline")
        if not source.get("allow_upload", False) or not source.get("writable", False):
            raise _Paused("Uploads are not permitted on the admitted source")
        try:
            configured = Path(source.get("resolved_path") or source["path"])
            root = configured.resolve(strict=True)
            if configured.absolute() != root:
                raise _Paused("The upload source path contains a symbolic link")
            info = root.stat()
            if not stat.S_ISDIR(info.st_mode) or not os.access(root, os.W_OK | os.X_OK):
                raise _Paused("The admitted source is not a writable directory")
        except OSError as exc:
            raise _Paused("The admitted source is unavailable: " + str(exc)) from exc
        return root, {"path": str(root), "configured_path": str(source.get("path", root)), "identity": _json_identity(source.get("identity")), "physical": _physical(info)}

    def _eligible(self, record):
        root, admission = self._source(record["source_id"])
        if admission != record["_source"]:
            raise _Paused("The source path or physical identity changed; restore the admitted source to resume")
        return root

    @staticmethod
    def _space(root, needed=0):
        try:
            free = shutil.disk_usage(root).free
        except OSError as exc:
            raise _Paused("The admitted source became unavailable") from exc
        if free < needed + SPACE_MARGIN:
            raise PolicyError("INSUFFICIENT_SPACE", "The source has insufficient free space", 409, {"required_bytes": needed + SPACE_MARGIN})

    def _root_fd(self, record):
        root = self._eligible(record)
        try:
            fd = os.open(root, _DIR_FLAGS)
        except OSError as exc:
            raise _Paused("The admitted source became unavailable") from exc
        if _physical(os.fstat(fd)) != record["_source"]["physical"]:
            os.close(fd)
            raise _Paused("The source physical identity changed")
        return fd

    def _stage_fd(self, record):
        root_fd = self._root_fd(record)
        try:
            container = os.open(".station-uploads", _DIR_FLAGS, dir_fd=root_fd)
            try:
                stage = os.open(record["id"], _DIR_FLAGS, dir_fd=container)
            finally:
                os.close(container)
            if _physical(os.fstat(stage)) != record["_stage_identity"]:
                os.close(stage)
                raise PolicyError("STAGING_CHANGED", "Upload staging identity changed", 409)
            return stage
        finally:
            os.close(root_fd)

    @staticmethod
    def _file_fd(parent, relative, *, write=False, create=False):
        """Walk with openat/O_NOFOLLOW so parent symlink swaps cannot escape."""
        parts = _path(relative).split("/")
        current = os.dup(parent)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                        os.fsync(current)
                    except FileExistsError:
                        pass
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
                os.close(current)
                current = child
            flags = os.O_NOFOLLOW | (os.O_RDWR if write else os.O_RDONLY)
            if create:
                flags |= os.O_CREAT
            fd = os.open(parts[-1], flags, 0o600, dir_fd=current)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                os.close(fd)
                raise PolicyError("INVALID_FILE", "Upload entries must be regular files with no hard links")
            if create:
                os.fsync(current)
            return fd
        finally:
            os.close(current)

    def create(self, body):
        if self._stopping.is_set():
            raise PolicyError("UPLOADS_CLOSED", "Upload service is stopping", 503)
        if not isinstance(body, dict):
            raise PolicyError("INVALID_UPLOAD", "An upload object is required")
        source_id = identifier(body.get("source_id"), "Source ID")
        name = body.get("destination_name")
        if not isinstance(name, str) or not name or len(name) > 128 or name.startswith(".") or any(c in name for c in ("/", "\\", "\0")):
            raise PolicyError("INVALID_DESTINATION", "Destination must be one new, visible directory name")
        kind = body.get("kind")
        inventory = body.get("files")
        if kind not in ("folder", "archive") or not isinstance(inventory, list) or not 1 <= len(inventory) <= MAX_FILES:
            raise PolicyError("INVALID_INVENTORY", "A folder/archive and a bounded nonempty file inventory are required")
        if kind == "archive" and len(inventory) != 1:
            raise PolicyError("INVALID_ARCHIVE", "Archive uploads require exactly one ZIP, TAR, or TGZ file")
        files, seen, total = [], set(), 0
        for entry in inventory:
            if not isinstance(entry, dict):
                raise PolicyError("INVALID_INVENTORY", "Each file must have a path and size")
            raw = entry.get("path")
            if not isinstance(raw, str) or any(part in ("", ".", "..") for part in raw.split("/")):
                raise PolicyError("INVALID_PATH", "File paths must contain normal relative components")
            path = _path(raw)
            size = entry.get("size")
            if type(size) is not int or not 0 <= size <= MAX_PACKAGE_BYTES:
                raise PolicyError("INVALID_SIZE", "File sizes must be bounded nonnegative integers")
            if path in seen or any(path.startswith(other + "/") or other.startswith(path + "/") for other in seen):
                raise PolicyError("INVALID_INVENTORY", "Inventory paths conflict")
            seen.add(path)
            total += size
            files.append({"path": path, "size": size, "sha256": _digest(entry.get("sha256"), optional=True), "chunks": []})
        if total > MAX_PACKAGE_BYTES:
            raise PolicyError("UPLOAD_LIMIT", "Upload exceeds the bounded package size", 413)
        if kind == "archive" and not files[0]["path"].lower().endswith((".zip", ".tar", ".tgz", ".tar.gz")):
            raise PolicyError("INVALID_ARCHIVE", "Only ZIP, TAR, and TGZ archives are supported")
        try:
            root, admission = self._source(source_id)
        except _Paused as exc:
            raise PolicyError("SOURCE_UNAVAILABLE", str(exc), 409) from exc
        self._space(root, total)
        root_fd = os.open(root, _DIR_FLAGS)
        stage_fd = None
        upload_id = "u" + uuid.uuid4().hex
        try:
            if _physical(os.fstat(root_fd)) != admission["physical"]:
                raise PolicyError("SOURCE_CHANGED", "Source changed during admission", 409)
            try:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PolicyError("DESTINATION_EXISTS", "The destination already exists", 409)
            try:
                os.mkdir(".station-uploads", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            container = os.open(".station-uploads", _DIR_FLAGS, dir_fd=root_fd)
            try:
                os.mkdir(upload_id, mode=0o700, dir_fd=container)
                stage_fd = os.open(upload_id, _DIR_FLAGS, dir_fd=container)
                os.mkdir("files", mode=0o700, dir_fd=stage_fd)
                os.fsync(stage_fd)
                os.fsync(container)
            finally:
                os.close(container)
            record = {"schema_version": 1, "id": upload_id, "name": name, "chunk_size": CHUNK_SIZE, "source_id": source_id, "destination_name": name, "kind": kind, "state": "uploading", "total_bytes": total, "received_bytes": 0, "verified_chunk_count": 0, "files": files, "reason": None, "created_at_ns": time.time_ns(), "_source": admission, "_stage_identity": _physical(os.fstat(stage_fd))}
            self._locks[upload_id] = threading.RLock()
            self._cancel[upload_id] = threading.Event()
            self._save(record)
            return self._public(record)
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
            os.close(root_fd)

    def _observe_source(self, record):
        upload_id = record["id"]
        if record["state"] in ("uploading", "paused"):
            try:
                self._eligible(record)
            except _Paused as exc:
                if record["state"] != "paused" or record["reason"] != str(exc):
                    with self._locks[upload_id]:
                        record = self._load(upload_id)
                        if record["state"] in ("uploading", "paused"):
                            record.update(state="paused", reason=str(exc))
                            self._save(record)
        return record

    def get(self, upload_id):
        return self._public(self._observe_source(self._load(upload_id)))

    def list(self):
        return {"schema_version": 1, "uploads": [self._summary(self._observe_source(record)) for record in self._all()]}

    def put_chunk(self, upload_id, file_index, offset, data, sha256):
        record = self._load(upload_id, include_chunks=False)
        if type(file_index) is not int or not 0 <= file_index < len(record["files"]):
            raise PolicyError("INVALID_FILE_INDEX", "File index is outside the upload inventory")
        if type(offset) is not int or offset < 0:
            raise PolicyError("INVALID_OFFSET", "Chunk offset must be a nonnegative integer")
        if not isinstance(data, bytes) or not 1 <= len(data) <= CHUNK_SIZE:
            raise PolicyError("CHUNK_LIMIT", "Raw chunks must contain 1 to 131072 bytes", 413)
        digest = _digest(sha256)
        if hashlib.sha256(data).hexdigest() != digest:
            raise PolicyError("CHUNK_HASH_MISMATCH", "Chunk SHA256 does not match its exact bytes", 409)
        with self._locks[upload_id]:
            record = self._load(upload_id, include_chunks=False)
            if record["state"] not in ("uploading", "paused"):
                raise PolicyError("UPLOAD_STATE", "This upload is not accepting chunks", 409)
            try:
                root = self._eligible(record)
            except _Paused as exc:
                record.update(state="paused", reason=str(exc)); self._save(record)
                raise PolicyError("SOURCE_UNAVAILABLE", str(exc), 409) from exc
            file = record["files"][file_index]
            end = offset + len(data)
            if end > file["size"]:
                raise PolicyError("CHUNK_RANGE", "Chunk extends beyond the declared file size")
            replay = None
            with self._db_lock:
                previous = self._db.execute("SELECT offset,size,sha256 FROM policy_upload_chunks WHERE upload_id=? AND file_index=? AND offset<=? ORDER BY offset DESC LIMIT 1", (upload_id, file_index, offset)).fetchone()
                following = self._db.execute("SELECT offset,size,sha256 FROM policy_upload_chunks WHERE upload_id=? AND file_index=? AND offset>? AND offset<? ORDER BY offset LIMIT 1", (upload_id, file_index, offset, end)).fetchone()
            for existing in (previous, following):
                if existing is None:
                    continue
                chunk = {"offset": existing[0], "size": existing[1], "sha256": existing[2]}
                if offset < chunk["offset"] + chunk["size"] and chunk["offset"] < end:
                    if chunk == {"offset": offset, "size": len(data), "sha256": digest}:
                        replay = chunk
                    else:
                        raise PolicyError("CHUNK_CONFLICT", "Chunk overlaps a previously verified interval", 409)
            if not replay and record["verified_chunk_count"] >= MAX_VERIFIED_CHUNKS:
                raise PolicyError("CHUNK_RECEIPT_LIMIT", "Upload contains too many small chunks; use the advertised chunk size", 413)
            self._space(root, 0 if replay else len(data))
            try:
                stage_fd = self._stage_fd(record)
            except _Paused as exc:
                record.update(state="paused", reason=str(exc)); self._save(record)
                raise PolicyError("SOURCE_UNAVAILABLE", str(exc), 409) from exc
            try:
                files_fd = os.open("files", _DIR_FLAGS, dir_fd=stage_fd)
                try:
                    fd = self._file_fd(files_fd, file["path"], write=True, create=not replay)
                    try:
                        if replay:
                            if os.pread(fd, len(data), offset) != data:
                                raise PolicyError("CHUNK_CONFLICT", "The previously verified chunk bytes changed", 409)
                        else:
                            written = 0
                            while written < len(data):
                                count = os.pwrite(fd, data[written:], offset + written)
                                if count <= 0:
                                    raise OSError("Short chunk write")
                                written += count
                            os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    os.close(files_fd)
            finally:
                os.close(stage_fd)
            new_chunk = None
            if not replay:
                new_chunk = (file_index, {"offset": offset, "size": len(data), "sha256": digest})
                record["received_bytes"] += len(data)
                record["verified_chunk_count"] += 1
            record.update(state="uploading", reason=None)
            self._save(record, new_chunk=new_chunk)
            return self._summary(record)

    def finalize(self, upload_id):
        self._load(upload_id)
        with self._locks[upload_id]:
            record = self._load(upload_id)
            if record["state"] in ("cataloged", "verifying"):
                return self._public(record)
            if record["state"] not in ("uploading", "paused"):
                raise PolicyError("UPLOAD_STATE", "This upload cannot be finalized", 409)
            try:
                root = self._eligible(record)
            except _Paused as exc:
                record.update(state="paused", reason=str(exc)); self._save(record)
                raise PolicyError("SOURCE_UNAVAILABLE", str(exc), 409) from exc
            for file in record["files"]:
                next_offset = 0
                for chunk in file["chunks"]:
                    if chunk["offset"] != next_offset:
                        raise PolicyError("UPLOAD_INCOMPLETE", "The upload still has missing byte intervals", 409)
                    next_offset += chunk["size"]
                if next_offset != file["size"]:
                    raise PolicyError("UPLOAD_INCOMPLETE", "The upload still has missing byte intervals", 409)
            self._space(root)
            record.update(state="verifying", reason=None)
            self._save(record)
            self._futures[upload_id] = self._executor.submit(self._verify, upload_id)
            return self._public(record)

    def _checkpoint(self, record, needed=0):
        if self._cancel[record["id"]].is_set():
            raise _Canceled()
        if self._stopping.is_set():
            raise _Paused("Server stopped during verification; finalize again to resume")
        root = self._eligible(record)
        self._space(root, needed)
        return root

    def _verify_files(self, record, stage_fd):
        files_fd = os.open("files", _DIR_FLAGS, dir_fd=stage_fd)
        try:
            for file in record["files"]:
                self._checkpoint(record)
                fd = self._file_fd(files_fd, file["path"], write=file["size"] == 0, create=file["size"] == 0)
                try:
                    if os.fstat(fd).st_size != file["size"]:
                        raise PolicyError("FILE_SIZE_MISMATCH", "Staged file size does not match its inventory", 409)
                    whole = hashlib.sha256()
                    for chunk in file["chunks"]:
                        self._checkpoint(record)
                        data = os.pread(fd, chunk["size"], chunk["offset"])
                        if len(data) != chunk["size"] or hashlib.sha256(data).hexdigest() != chunk["sha256"]:
                            raise PolicyError("CHUNK_HASH_MISMATCH", "A durable chunk no longer matches its verified SHA256", 409)
                        whole.update(data)
                    digest = whole.hexdigest()
                    if file["sha256"] and digest != file["sha256"]:
                        raise PolicyError("FILE_HASH_MISMATCH", "Complete file SHA256 does not match its inventory", 409)
                    file["sha256"] = digest
                finally:
                    os.close(fd)
        finally:
            os.close(files_fd)
        self._save(record)

    @staticmethod
    def _archive_path(name, is_dir=False):
        if not isinstance(name, str):
            raise PolicyError("INVALID_ARCHIVE_PATH", "Archive entry has no valid path")
        if is_dir:
            name = name.rstrip("/")
        if not name or any(part in ("", ".", "..") for part in name.split("/")):
            raise PolicyError("INVALID_ARCHIVE_PATH", "Archive paths must stay inside the package")
        return _path(name)

    def _extract(self, record, stage_fd, stage_path):
        unpack = stage_path / "unpacked"
        try:
            os.stat("unpacked", dir_fd=stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            self._remove_tree_at(stage_fd, "unpacked")
        os.mkdir("unpacked", mode=0o700, dir_fd=stage_fd)
        output_fd = os.open("unpacked", _DIR_FLAGS, dir_fd=stage_fd)
        files_fd = os.open("files", _DIR_FLAGS, dir_fd=stage_fd)
        archive_fd = None
        try:
            entry = record["files"][0]
            archive_fd = self._file_fd(files_fd, entry["path"])
            with os.fdopen(archive_fd, "rb") as stream:
                archive_fd = None
                if entry["path"].lower().endswith(".zip"):
                    # Bound the central directory before ZipFile allocates it;
                    # this stdlib parser also handles ZIP64 end records.
                    end = zipfile._EndRecData(stream)
                    if end is None or end[zipfile._ECD_ENTRIES_TOTAL] > MAX_MEMBERS or end[zipfile._ECD_SIZE] > MAX_ARCHIVE_METADATA:
                        raise PolicyError("ARCHIVE_LIMIT", "ZIP metadata exceeds the bounded archive inventory", 413)
                    stream.seek(0)
                    with zipfile.ZipFile(stream) as archive:
                        members = archive.infolist()
                        total = sum(member.file_size for member in members)
                        if len(members) > MAX_MEMBERS or total > MAX_PACKAGE_BYTES:
                            raise PolicyError("ARCHIVE_LIMIT", "Archive contents exceed the bounded package inventory", 413)
                        self._checkpoint(record, total)
                        seen, count, remaining = set(), 0, total
                        for member in members:
                            mode = member.external_attr >> 16
                            directory = member.is_dir()
                            if stat.S_IFMT(mode) not in (0, stat.S_IFDIR if directory else stat.S_IFREG) or member.flag_bits & 1:
                                raise PolicyError("UNSAFE_ARCHIVE", "ZIP links, special files, and encrypted entries are not accepted")
                            path = self._archive_path(member.filename, directory)
                            if path in seen:
                                raise PolicyError("UNSAFE_ARCHIVE", "Duplicate archive paths are not accepted")
                            seen.add(path)
                            if directory:
                                self._mkdir_relative(output_fd, path)
                                continue
                            count += 1
                            if count > MAX_FILES:
                                raise PolicyError("ARCHIVE_LIMIT", "Archive contains too many files", 413)
                            with archive.open(member) as source:
                                self._extract_file(record, output_fd, path, member.file_size, source, remaining)
                            remaining -= member.file_size
                else:
                    # Stream TAR entries; never let tarfile cache an unbounded
                    # member list or allocate an attacker-sized PAX/GNU header.
                    with tarfile.open(fileobj=stream, mode="r|*", tarinfo=_BoundedTarInfo) as archive:
                        seen, count, total, members = set(), 0, 0, 0
                        for member in archive:
                            members += 1
                            if members > MAX_MEMBERS or len(archive.members) > MAX_MEMBERS:
                                raise PolicyError("ARCHIVE_LIMIT", "Archive contains too many entries", 413)
                            if not member.isdir() and not member.isreg():
                                raise PolicyError("UNSAFE_ARCHIVE", "TAR links and special files are not accepted")
                            if member.sparse is not None or member.size < 0:
                                raise PolicyError("UNSAFE_ARCHIVE", "Sparse TAR files are not accepted")
                            path = self._archive_path(member.name, member.isdir())
                            if path in seen:
                                raise PolicyError("UNSAFE_ARCHIVE", "Duplicate archive paths are not accepted")
                            seen.add(path)
                            if member.isdir():
                                self._mkdir_relative(output_fd, path)
                                continue
                            count += 1
                            total += member.size
                            if count > MAX_FILES or total > MAX_PACKAGE_BYTES:
                                raise PolicyError("ARCHIVE_LIMIT", "Archive contents exceed the bounded package inventory", 413)
                            self._checkpoint(record, member.size)
                            source = archive.extractfile(member)
                            if source is None:
                                raise PolicyError("INVALID_ARCHIVE", "TAR regular file has no body")
                            with source:
                                self._extract_file(record, output_fd, path, member.size, source, member.size)
        finally:
            if archive_fd is not None:
                os.close(archive_fd)
            os.close(files_fd)
            os.close(output_fd)
        return unpack

    @staticmethod
    def _mkdir_relative(root_fd, path):
        current = os.dup(root_fd)
        try:
            for part in _path(path).split("/"):
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
                os.close(current)
                current = child
        finally:
            os.close(current)

    def _extract_file(self, record, output_fd, path, size, source, remaining):
        fd = self._file_fd(output_fd, path, write=True, create=True)
        try:
            written = 0
            while written < size:
                self._checkpoint(record, max(remaining - written, 0))
                data = source.read(min(CHUNK_SIZE, size - written))
                if not data:
                    raise PolicyError("INVALID_ARCHIVE", "Archive file body is truncated")
                count = 0
                while count < len(data):
                    amount = os.write(fd, data[count:])
                    if amount <= 0:
                        raise OSError("Short archive write")
                    count += amount
                written += len(data)
            if source.read(1):
                raise PolicyError("INVALID_ARCHIVE", "Archive file exceeds its declared size")
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _assert_tree(path):
        count, total, members = 0, 0, 0
        for parent, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                members += 1
                entry = Path(parent) / name
                info = entry.lstat()
                if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise PolicyError("UNSAFE_PACKAGE", "Packages cannot contain links or special files")
                if stat.S_ISREG(info.st_mode):
                    count += 1
                    total += info.st_size
                if count > MAX_FILES or members > MAX_MEMBERS or total > MAX_PACKAGE_BYTES:
                    raise PolicyError("PACKAGE_LIMIT", "Package exceeds bounded file/byte limits", 413)
                if not entry.resolve().is_relative_to(path.resolve()):
                    raise PolicyError("UNSAFE_PACKAGE", "Package path escaped its staging root")

    def _package(self, path):
        self._assert_tree(path)
        descriptor = None
        for _ in range(9):
            descriptor = self.inspect_package(path)
            if isinstance(descriptor, dict) and descriptor.get("complete") is True:
                issues = descriptor.get("issues", [])
                invalid = {"MANIFEST_DIGEST_MISMATCH", "INVALID_PACKAGE", "ARTIFACT_CHANGING"}
                if descriptor.get("integrity_valid") is False or descriptor.get("state") in ("invalid", "copying") or any(isinstance(issue, dict) and issue.get("code") in invalid for issue in issues):
                    raise PolicyError("PACKAGE_INTEGRITY_FAILED", "The complete package failed artifact integrity verification", 409, {"issues": issues})
                return path, descriptor
            children = list(path.iterdir())
            if len(children) != 1 or not children[0].is_dir() or children[0].is_symlink():
                break
            path = children[0]
        raise PolicyError("INCOMPLETE_PACKAGE", "The upload does not contain a complete model checkpoint/config/processor package", 400, descriptor)

    def _recover_publication(self, record, root):
        intent = record.get("_publication")
        if not intent:
            return False
        destination = root / record["destination_name"]
        try:
            info = destination.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(info.st_mode) and _physical(info) == intent["physical"]:
            self._published(record, destination, intent["descriptor"])
            return True
        raise PolicyError("DESTINATION_EXISTS", "The destination already exists", 409)

    def _published(self, record, destination, descriptor):
        # The persisted publication intent already identifies the renamed
        # inode for crash recovery. Keep verifying until the final metadata
        # and cleanup result are visible together to polling clients.
        record.update(reason=None, model_id=descriptor.get("id", descriptor.get("model_id")), artifact_revision=descriptor.get("artifact_revision"), scan_job=None)
        record["_publication_done"] = True
        self._save(record)
        try:
            resolved = self.inspect_package(destination)
            if isinstance(resolved, dict):
                record["model_id"] = resolved.get("id", resolved.get("model_id", record["model_id"]))
                record["artifact_revision"] = resolved.get("artifact_revision", record["artifact_revision"])
            if self.on_publish:
                scan = self.on_publish(record["source_id"])
                record["scan_job"] = scan.get("job", scan) if isinstance(scan, dict) else scan
        except Exception as exc:
            record["reason"] = "Published; catalog refresh needs retry: " + str(exc)
        self._cleanup(record)
        record["state"] = "cataloged"
        self._save(record)

    def _verify(self, upload_id):
        with self._locks[upload_id]:
            record = self._load(upload_id)
            if record["state"] != "verifying":
                return
            stage_fd = None
            try:
                root = self._checkpoint(record)
                if self._recover_publication(record, root):
                    return
                stage_fd = self._stage_fd(record)
                stage_path = root / ".station-uploads" / upload_id
                self._verify_files(record, stage_fd)
                payload = self._extract(record, stage_fd, stage_path) if record["kind"] == "archive" else stage_path / "files"
                package, descriptor = self._package(payload)
                self._checkpoint(record)
                self._assert_tree(package)
                record["_publication"] = {"physical": _physical(package.stat()), "descriptor": descriptor}
                self._save(record)
                source_parent = os.open(package.parent, _DIR_FLAGS)
                destination_fd = self._root_fd(record)
                try:
                    self._checkpoint(record)
                    _rename_noreplace(source_parent, package.name, destination_fd, record["destination_name"])
                    os.fsync(destination_fd)
                    os.fsync(source_parent)
                finally:
                    os.close(destination_fd)
                    os.close(source_parent)
                self._published(record, root / record["destination_name"], descriptor)
            except _Canceled:
                pass  # cancel() owns the terminal state and staging cleanup.
            except _Paused as exc:
                record.update(state="paused", reason=str(exc)); self._save(record)
            except Exception as exc:
                record.update(state="failed", reason=str(exc), error_code=getattr(exc, "code", "UPLOAD_VERIFICATION_FAILED"))
                if isinstance(exc, PolicyError) and exc.details is not None:
                    record["details"] = exc.details
                self._save(record)
            finally:
                if stage_fd is not None:
                    os.close(stage_fd)

    @classmethod
    def _remove_tree_at(cls, parent_fd, name, expected=None):
        """Remove through held directory descriptors, never a resolved path.

        In particular, a swapped .station-uploads parent cannot redirect cancel
        into another directory between the ownership check and tree removal.
        """
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (expected is not None and _physical(info) != expected):
            raise PolicyError("STAGING_CHANGED", "Refusing to remove a replaced staging directory", 409)
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            if _physical(os.fstat(fd)) != _physical(info):
                raise PolicyError("STAGING_CHANGED", "Staging changed during cleanup", 409)
            for child in os.listdir(fd):
                child_info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(child_info.st_mode):
                    cls._remove_tree_at(fd, child, _physical(child_info))
                else:
                    # unlink removes links themselves; it never follows them.
                    os.unlink(child, dir_fd=fd)
            if _physical(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _physical(info):
                raise PolicyError("STAGING_CHANGED", "Staging changed during cleanup", 409)
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(fd)

    def _cleanup(self, record):
        try:
            root_fd = self._root_fd(record)
            try:
                container = os.open(".station-uploads", _DIR_FLAGS, dir_fd=root_fd)
                try:
                    try:
                        os.stat(record["id"], dir_fd=container, follow_symlinks=False)
                    except FileNotFoundError:
                        return
                    self._remove_tree_at(container, record["id"], record["_stage_identity"])
                finally:
                    os.close(container)
            finally:
                os.close(root_fd)
        except Exception as exc:
            record["cleanup_error"] = str(exc)
            self._save(record)

    def cancel(self, upload_id):
        self._load(upload_id)
        self._cancel[upload_id].set()
        with self._locks[upload_id]:
            record = self._load(upload_id)
            if record["state"] == "cataloged":
                return self._public(record)
            record.update(state="canceled", reason=None)
            self._save(record)
            self._cleanup(record)
            return self._public(record)

    def close(self):
        if self._closed:
            return
        self._stopping.set()
        self._executor.shutdown(wait=True)
        with self._db_lock:
            self._db.close()
        self._closed = True
