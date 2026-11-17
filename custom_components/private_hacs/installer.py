"""Hardened extraction of one integration from a GitHub tarball into custom_components/.

The tarball is untrusted input (its *commit* was chosen by a verified signature, but
its bytes come from the network), so nothing is written until the whole archive has
been validated, and the live folder is replaced only at the very end with two renames,
keeping the previous version for rollback.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import logging
import os
import secrets
import shutil
import tarfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .const import (
    ALLOWED_EXTENSIONS,
    INTEGRATION_RE,
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_INTEGRATION_BYTES,
    MAX_MEMBERS,
    PATH_PART_RE,
    PROVENANCE_FILE,
    SHA_RE,
    VERSION_RE,
)

_LOGGER = logging.getLogger(__name__)


class InstallerError(Exception):
    """Raised on any extraction or validation problem."""


@dataclass(frozen=True)
class InstallResult:
    integration: str
    path: Path
    requirements: list[str]


class _CappedReader:
    """read()-only wrapper that stops a decompression bomb before it fills memory.

    tarfile reads whole extended headers into memory before any size check of ours can
    run, so the cap has to be on the decompressed stream itself.
    """

    def __init__(self, raw: gzip.GzipFile, limit: int) -> None:
        self._raw = raw
        self._left = limit

    def read(self, size: int | None = -1) -> bytes:
        data = self._raw.read(size if size and size > 0 else 1 << 20)
        self._left -= len(data)
        if self._left < 0:
            raise InstallerError("Archive is too large")
        return data


def _open(tarball: bytes) -> tarfile.TarFile:
    """Open the tarball as a forward-only stream, with the decompressed size capped."""
    limit = MAX_ARCHIVE_BYTES + MAX_ARCHIVE_BYTES // 10 + (1 << 20)
    capped = _CappedReader(gzip.GzipFile(fileobj=io.BytesIO(tarball)), limit)
    return tarfile.open(fileobj=capped, mode="r|")


def _check_name(name: str) -> None:
    if not INTEGRATION_RE.fullmatch(name or ""):
        raise InstallerError("Invalid integration name")


def _scan(tf: tarfile.TarFile) -> tuple[str, list[tarfile.TarInfo]]:
    """Walk the archive once: bound its size and make sure every path is sane."""
    members: list[tarfile.TarInfo] = []
    declared = 0
    top: str | None = None
    while (member := tf.next()) is not None:
        members.append(member)
        if len(members) > MAX_MEMBERS:
            raise InstallerError("Archive has too many entries")
        declared += max(member.size, 0)
        if declared > MAX_ARCHIVE_BYTES:
            raise InstallerError("Archive is too large")
        parts = PurePosixPath(member.name).parts
        if not parts or member.name.startswith("/") or ".." in parts:
            raise InstallerError("Unsafe path in archive")
        if top is None:
            top = parts[0]
        elif parts[0] != top:
            raise InstallerError("Unexpected archive structure")
    if top is None:
        raise InstallerError("Archive is empty")
    return top, members


def _plan(
    top: str,
    members: list[tarfile.TarInfo],
    integration_name: str,
    is_reserved: Callable[[str], bool],
) -> dict[str, tuple[str, ...]]:
    """Check the integration folder and map member name -> relative path parts (files only).

    Everything outside `custom_components/<integration_name>/` is ignored and never extracted;
    inside it, links, special files, odd names and file types are refused.
    """
    _check_name(integration_name)
    if is_reserved(integration_name):
        raise InstallerError("That integration name is reserved or already in use")
    base = (top, "custom_components", integration_name)
    if not any(PurePosixPath(m.name).parts[:3] == base for m in members):
        raise InstallerError("The repository has no custom_components/<that name>/ folder")

    files: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    total = 0
    for m in members:
        parts = PurePosixPath(m.name).parts
        if parts[:3] != base or len(parts) == 3:
            continue
        rel = parts[3:]
        if any(p.startswith(".") for p in rel):
            raise InstallerError("Hidden file or folder not allowed in the integration")
        if not all(PATH_PART_RE.fullmatch(p) for p in rel):
            raise InstallerError("Unusual file name in the integration (ASCII letters, digits, _ . - only)")
        folded = "/".join(rel).casefold()  # `A.py` and `a.py` would collide on some filesystems
        if folded in seen:
            raise InstallerError("Duplicate entry in the integration")
        seen.add(folded)
        if m.isdir():
            continue
        if not m.isreg() or m.issparse():
            raise InstallerError("Links and special files are not allowed in the integration")
        if PurePosixPath(rel[-1]).suffix not in ALLOWED_EXTENSIONS:
            raise InstallerError("File type not allowed in the integration")
        if m.size > MAX_FILE_BYTES:
            raise InstallerError("A file of the integration is too large")
        total += m.size
        if total > MAX_INTEGRATION_BYTES:
            raise InstallerError("Integration is too large")
        files[m.name] = rel
        if len(files) > MAX_FILES:
            raise InstallerError("Integration has too many files")
    if ("manifest.json",) not in set(files.values()):
        raise InstallerError("Integration has no manifest.json")
    return files


def read_manifest(folder: Path) -> dict:
    try:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise InstallerError(f"Cannot read manifest.json: {err}") from err
    if not isinstance(data, dict):
        raise InstallerError("manifest.json is not an object")
    return data


def read_provenance(folder: Path) -> dict[str, str] | None:
    """Commit / key fingerprint / version recorded when `folder` was installed, if valid."""
    try:
        data = json.loads((folder / PROVENANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        isinstance(data, dict)
        and isinstance(data.get("commit"), str) and SHA_RE.fullmatch(data["commit"])
        and isinstance(data.get("fingerprint"), str) and data["fingerprint"].startswith("SHA256:")
        and isinstance(data.get("version"), str) and VERSION_RE.fullmatch(data["version"])
    ):
        return {k: data[k] for k in ("commit", "fingerprint", "version")}
    return None


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    """Flush files and folders to disk so a power cut cannot leave truncated files after a swap."""
    for current, dirs, files in os.walk(root):
        for name in files:
            _fsync(Path(current, name))
        for name in dirs:
            _fsync(Path(current, name))
    _fsync(root)


def install_from_tarball(
    tarball: bytes,
    dest_root: Path,
    backup_root: Path,
    integration_name: str,
    *,
    expected_version: str,
    expected_commit: str,
    fingerprint: str,
    is_reserved: Callable[[str], bool] = lambda name: False,
) -> InstallResult:
    """Validate the archive, then install `custom_components/<integration_name>/` from it.

    The archive must carry, in its pax header, exactly `expected_commit` (GitHub's tarballs are
    made by `git archive`, which records the full commit id there); `expected_version` (no
    leading v) must equal the manifest version. `is_reserved(name)` vetoes names that must not
    be installed. `fingerprint` (the signing key) is recorded next to the code.
    """
    try:
        with _open(tarball) as tf:
            top, members = _scan(tf)
            commit_in_archive = tf.pax_headers.get("comment")
        if commit_in_archive != expected_commit or not top.endswith(expected_commit[:7]):
            raise InstallerError("The archive does not belong to the signed commit")
        files = _plan(top, members, integration_name, is_reserved)

        dest_root.mkdir(parents=True, exist_ok=True)
        staging = dest_root / f".ph-stage-{secrets.token_hex(8)}"
        staging.mkdir()  # exclusive, and the mode follows the umask like any other folder
        try:
            _extract(tarball, staging, files)
            manifest = read_manifest(staging)
            if manifest.get("domain") != integration_name:
                raise InstallerError("manifest domain differs from the integration folder")
            if manifest.get("version") != expected_version:
                raise InstallerError("manifest version differs from the signed tag")
            requirements = manifest.get("requirements", [])
            if not isinstance(requirements, list) or not all(isinstance(r, str) for r in requirements):
                raise InstallerError("manifest requirements must be a list of strings")
            (staging / PROVENANCE_FILE).write_text(
                json.dumps(
                    {"commit": expected_commit, "fingerprint": fingerprint, "version": expected_version}
                )
            )
            _fsync_tree(staging)
            _swap(staging, dest_root / integration_name, backup_root / integration_name)
            _fsync(dest_root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    except (tarfile.TarError, EOFError, OSError, UnicodeError, ValueError, zlib.error) as err:
        raise InstallerError(f"Cannot read the archive: {err}") from err
    return InstallResult(integration_name, dest_root / integration_name, requirements)


def _extract(tarball: bytes, staging: Path, files: dict[str, tuple[str, ...]]) -> None:
    """Second, sequential pass: write the planned files into `staging`."""
    written = 0
    with _open(tarball) as tf:
        while (member := tf.next()) is not None:
            rel = files.get(member.name)
            if rel is None:
                continue
            out = staging.joinpath(*rel)
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                raise InstallerError("Cannot read a file of the archive")
            size = 0
            with src, open(out, "wb") as dst:
                while chunk := src.read(65536):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise InstallerError("A file of the integration is too large")
                    dst.write(chunk)
            written += 1
    if written != len(files):
        raise InstallerError("Archive changed between validation and extraction")


def _swap(staging: Path, dest: Path, backup: Path) -> None:
    """Put `staging` in place of `dest`, keeping the previous version in `backup`.

    Two renames: for an instant in between `dest` does not exist. A `.pending` marker next to the
    backup tells `recover()` that a swap was interrupted.
    """
    backup.parent.mkdir(parents=True, exist_ok=True)
    marker = backup.parent / f"{backup.name}.pending"
    moved = False
    try:
        if dest.exists():
            marker.write_text("swap in progress\n")
            if backup.exists():
                shutil.rmtree(backup)
            os.rename(dest, backup)
            moved = True
        os.rename(staging, dest)
    except OSError as err:
        if moved and not dest.exists():
            try:
                os.rename(backup, dest)
            except OSError:
                _LOGGER.error("Could not restore %s from %s after a failed update", dest, backup)
        marker.unlink(missing_ok=True)
        raise InstallerError(f"Cannot put the new version in place: {err}") from err
    marker.unlink(missing_ok=True)


def installed_version(dest_root: Path, name: str) -> str | None:
    """Version in the manifest of the folder on disk: None if absent, '?' if unreadable."""
    _check_name(name)
    if not (dest_root / name).exists():
        return None
    try:
        version = read_manifest(dest_root / name).get("version")
    except InstallerError:
        return "?"
    return version if isinstance(version, str) else "?"


def rollback_integration(
    dest_root: Path, backup_root: Path, name: str
) -> tuple[str, dict[str, str] | None]:
    """Swap the installed version with the previous one.

    Returns the restored version ('?' if its manifest is odd) and its recorded provenance.
    """
    _check_name(name)
    dest, backup = dest_root / name, backup_root / name
    if not (backup / "manifest.json").is_file():
        raise InstallerError("No previous version to roll back to")
    parked = backup_root / f"{name}.swap"
    try:
        if parked.exists():
            shutil.rmtree(parked)
        if dest.exists():
            os.rename(dest, parked)
        os.rename(backup, dest)
        if parked.exists():
            os.rename(parked, backup)
    except OSError as err:
        with contextlib.suppress(OSError):
            if not backup.exists() and dest.exists() and parked.exists():
                os.rename(dest, backup)  # undo the second step
            if parked.exists() and not dest.exists():
                os.rename(parked, dest)  # undo the first step
        raise InstallerError(f"Rollback failed and was undone: {err}") from err
    try:
        version = read_manifest(dest).get("version")
    except InstallerError:
        version = None
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        version = "?"
    return version, read_provenance(dest)


def remove_integration(dest_root: Path, backup_root: Path, name: str) -> bool:
    """Delete the installed integration, its backup and recovery markers.

    The backup goes first (see `recover`). Raises InstallerError if anything could not be deleted.
    """
    _check_name(name)
    existed = (dest_root / name).exists()
    failures: list[str] = []

    def collect(_func, path, exc) -> None:
        failures.append(f"{path}: {exc}")

    for target in (backup_root / name, dest_root / name):
        if target.exists():
            shutil.rmtree(target, onexc=collect)
    for marker in (backup_root / f"{name}.pending", backup_root / f"{name}.swap"):
        if marker.is_dir():
            shutil.rmtree(marker, onexc=collect)
        else:
            try:
                marker.unlink(missing_ok=True)
            except OSError as err:
                failures.append(f"{marker}: {err}")
    if failures:
        raise InstallerError(f"Could not delete everything ({len(failures)} problems): {failures[0]}")
    return existed


def recover(dest_root: Path, backup_root: Path) -> list[str]:
    """Clean up after a crash in the middle of an install or rollback; return what was done."""
    done: list[str] = []
    if dest_root.is_dir():
        for leftover in dest_root.glob(".ph-stage-*"):
            shutil.rmtree(leftover, ignore_errors=True)
            done.append(f"removed leftover {leftover.name}")
    if not backup_root.is_dir():
        return done
    for marker in backup_root.glob("*.pending"):  # interrupted install swap
        name = marker.name.removesuffix(".pending")
        if INTEGRATION_RE.fullmatch(name) and not (dest_root / name).exists() and (backup_root / name).is_dir():
            with contextlib.suppress(OSError):
                os.rename(backup_root / name, dest_root / name)
                done.append(f"restored {name} from backup after an interrupted update")
        marker.unlink(missing_ok=True)
    for parked in backup_root.glob("*.swap"):  # interrupted rollback
        name = parked.name.removesuffix(".swap")
        if not INTEGRATION_RE.fullmatch(name):
            continue
        with contextlib.suppress(OSError):
            if not (dest_root / name).exists():
                os.rename(parked, dest_root / name)
            elif not (backup_root / name).exists():
                os.rename(parked, backup_root / name)
            else:
                shutil.rmtree(parked)
            done.append(f"settled an interrupted rollback of {name}")
    return done
