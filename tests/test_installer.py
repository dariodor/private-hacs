"""The tarball is untrusted: every way of making the installer write something wrong."""
from __future__ import annotations

import gzip
import tarfile
from pathlib import Path

import pytest

from custom_components.private_hacs import installer
from custom_components.private_hacs.installer import (
    InstallerError,
    install_from_tarball,
    installed_version,
    read_provenance,
    recover,
    remove_integration,
    rollback_integration,
)
from tests.conftest import make_tar, manifest_bytes

COMMIT = "abcdef0123456789abcdef0123456789abcdef01"
TOP = "acme-demo-abcdef0"
BASE = "custom_components/demo_integration"


def good_entries(version="1.0.0", **extra):
    return [
        ("custom_components/", None),
        (f"{BASE}/", None),
        (f"{BASE}/__init__.py", b"# ok\n"),
        (f"{BASE}/manifest.json", manifest_bytes(version, **extra)),
        (f"{BASE}/translations/", None),
        (f"{BASE}/translations/en.json", b"{}"),
        ("README.md", b"not installed"),
        ("tests/test_x.py", b"not installed"),
    ]


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "custom_components", tmp_path / "backups"


FPR = "SHA256:" + "A" * 43


def install(tar, dirs, name="demo_integration", version="1.0.0", commit=COMMIT, **kwargs):
    return install_from_tarball(
        tar, dirs[0], dirs[1], name,
        expected_version=version, expected_commit=commit, fingerprint=FPR, **kwargs,
    )


def names(path: Path) -> list[str]:
    """Entries of a folder; a folder that was never created is empty."""
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def tree(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")}


def test_installs_only_the_integration_folder(dirs):
    res = install(make_tar(TOP, good_entries()), dirs)
    assert res.integration == "demo_integration"
    assert tree(dirs[0] / "demo_integration") == {
        "__init__.py", "manifest.json", "translations", "translations/en.json", ".private_hacs.json"
    }
    assert names(dirs[0]) == ["demo_integration"]  # no staging left behind


def test_requirements_are_reported(dirs):
    res = install(make_tar(TOP, good_entries(requirements=["numpy>=1", "pillow"])), dirs)
    assert res.requirements == ["numpy>=1", "pillow"]


@pytest.mark.parametrize("bad", [5, "numpy", ["numpy", 5], {"a": 1}])
def test_malformed_requirements_are_refused(dirs, bad):
    with pytest.raises(InstallerError, match="requirements"):
        install(make_tar(TOP, good_entries(requirements=bad)), dirs)
    assert names(dirs[0]) == []


def test_real_git_archive_installs(repo, dirs):
    tag = repo.release("1.4.0")
    info = repo.tags[tag]
    res = install(info["archive"], dirs, version="1.4.0", commit=info["commit"])
    assert (dirs[0] / res.integration / "manifest.json").is_file()


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param([(f"{BASE}/evil.json", ("sym", "/etc/passwd"))], id="symlink"),
        pytest.param([(f"{BASE}/evil.json", ("hard", f"{TOP}/README.md"))], id="hardlink"),
        pytest.param([(f"{BASE}/run.sh", b"#!/bin/sh\n")], id="shell script"),
        pytest.param([(f"{BASE}/native.so", b"\x7fELF")], id="binary"),
        pytest.param([(f"{BASE}/noext", b"x")], id="no extension"),
        pytest.param([(f"{BASE}/.hidden.py", b"x")], id="hidden file"),
        pytest.param([(f"{BASE}/.git/config.json", b"x")], id="hidden folder"),
        pytest.param([(f"{BASE}/__init__.py", b"# second\n")], id="duplicate entry"),
    ],
)
def test_dangerous_entries_abort_the_whole_install(dirs, bad):
    with pytest.raises(InstallerError):
        install(make_tar(TOP, good_entries() + bad), dirs)
    assert not (dirs[0] / "demo_integration").exists()
    assert names(dirs[0]) == []


def test_symlink_outside_the_integration_is_ignored(dirs):
    entries = good_entries() + [("docs/link", ("sym", "/etc/passwd"))]
    install(make_tar(TOP, entries), dirs)
    assert (dirs[0] / "demo_integration" / "manifest.json").is_file()


@pytest.mark.parametrize(
    "name", ["=../evil.py", f"{TOP}/../evil.py", "=/abs/evil.py", "custom_components/../../x.py"]
)
def test_path_traversal_is_refused(dirs, name):
    with pytest.raises(InstallerError, match="Unsafe path"):
        install(make_tar(TOP, good_entries() + [(name, b"x")]), dirs)


def test_two_top_level_folders_are_refused(dirs):
    with pytest.raises(InstallerError, match="structure"):
        install(make_tar(TOP, good_entries() + [("=other/x.py", b"x")]), dirs)


def test_manifest_domain_must_match_folder(dirs):
    entries = [e for e in good_entries() if not e[0].endswith("manifest.json")]
    entries.append((f"{BASE}/manifest.json", manifest_bytes(domain="other")))
    with pytest.raises(InstallerError, match="domain"):
        install(make_tar(TOP, entries), dirs)


def test_manifest_version_must_match_the_signed_tag(dirs):
    with pytest.raises(InstallerError, match="signed tag"):
        install(make_tar(TOP, good_entries("9.9.9")), dirs, version="1.0.0")


def test_manifest_is_required(dirs):
    entries = [e for e in good_entries() if not e[0].endswith("manifest.json")]
    with pytest.raises(InstallerError, match="manifest"):
        install(make_tar(TOP, entries), dirs)


def test_archive_of_another_commit_is_refused(dirs):
    with pytest.raises(InstallerError, match="signed commit"):
        install(make_tar(TOP, good_entries()), dirs, commit="1" * 40)


def test_missing_custom_components_is_refused(dirs):
    with pytest.raises(InstallerError, match="no custom_components"):
        install(make_tar(TOP, [("README.md", b"x")]), dirs)


def test_the_named_integration_must_exist_and_be_valid(dirs):
    entries = good_entries() + [
        ("custom_components/second/manifest.json", manifest_bytes(domain="second")),
        ("custom_components/second/__init__.py", b""),
    ]
    tar = make_tar(TOP, entries)
    assert install(tar, dirs, name="demo_integration").integration == "demo_integration"
    with pytest.raises(InstallerError, match="no custom_components"):
        install(tar, dirs, name="missing")
    for bad in ("", "../x", "_x", "__pycache__", "A", "9x", "a b", "x" * 65, None):
        with pytest.raises(InstallerError, match="Invalid integration name"):
            install(tar, dirs, name=bad)


@pytest.mark.parametrize("data", [b"", b"not a tarball", b"\x1f\x8b\x08\x00garbage"])
def test_garbage_is_a_clean_error(dirs, data):
    with pytest.raises(InstallerError):
        install(data, dirs)


def test_truncated_gzip_is_a_clean_error(dirs):
    tar = make_tar(TOP, good_entries())
    with pytest.raises(InstallerError):
        install(tar[: len(tar) // 2], dirs)


def test_limits(dirs, monkeypatch):
    tar = make_tar(TOP, good_entries())
    monkeypatch.setattr(installer, "MAX_FILE_BYTES", 3)
    with pytest.raises(InstallerError, match="too large"):
        install(tar, dirs)
    monkeypatch.undo()
    monkeypatch.setattr(installer, "MAX_MEMBERS", 3)
    with pytest.raises(InstallerError, match="too many entries"):
        install(tar, dirs)
    monkeypatch.undo()
    monkeypatch.setattr(installer, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(InstallerError, match="Archive is too large"):
        install(tar, dirs)
    monkeypatch.undo()
    monkeypatch.setattr(installer, "MAX_FILES", 1)
    with pytest.raises(InstallerError, match="too many files"):
        install(tar, dirs)
    monkeypatch.undo()
    monkeypatch.setattr(installer, "MAX_INTEGRATION_BYTES", 5)
    with pytest.raises(InstallerError, match="Integration is too large"):
        install(tar, dirs)
    assert not (dirs[0] / "demo_integration").exists()


def test_gzip_bomb_is_stopped_by_declared_size(dirs):
    info = tarfile.TarInfo(f"{TOP}/huge.bin")
    info.size = 300 * 1024 * 1024  # declared; larger than MAX_ARCHIVE_BYTES, data is just 1 KB
    bomb = gzip.compress(info.tobuf(tarfile.GNU_FORMAT) + b"\0" * 1024)
    with pytest.raises(InstallerError, match="Archive is too large"):
        install(bomb, dirs)
    assert names(dirs[0]) == []


def test_failed_update_leaves_the_installed_version_untouched(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    before = (dirs[0] / "demo_integration" / "manifest.json").read_bytes()
    with pytest.raises(InstallerError):
        install(make_tar(TOP, good_entries("2.0.0") + [(f"{BASE}/x.sh", b"x")]), dirs, version="2.0.0")
    assert (dirs[0] / "demo_integration" / "manifest.json").read_bytes() == before
    assert not dirs[1].exists() or not any(dirs[1].iterdir())  # no backup taken for a failed install
    assert names(dirs[0]) == ["demo_integration"]


def test_update_keeps_a_backup_and_rollback_restores_it(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    install(make_tar(TOP, good_entries("2.0.0")), dirs, version="2.0.0")
    assert b'"2.0.0"' in (dirs[0] / "demo_integration" / "manifest.json").read_bytes()
    assert b'"1.0.0"' in (dirs[1] / "demo_integration" / "manifest.json").read_bytes()

    assert rollback_integration(dirs[0], dirs[1], "demo_integration")[0] == "1.0.0"
    assert b'"1.0.0"' in (dirs[0] / "demo_integration" / "manifest.json").read_bytes()
    # and a second rollback goes forward again: the two versions are swapped
    assert rollback_integration(dirs[0], dirs[1], "demo_integration")[0] == "2.0.0"


def test_rollback_without_backup_fails(dirs):
    install(make_tar(TOP, good_entries()), dirs)
    with pytest.raises(InstallerError, match="No previous version"):
        rollback_integration(dirs[0], dirs[1], "demo_integration")
    with pytest.raises(InstallerError, match="Invalid integration name"):
        rollback_integration(dirs[0], dirs[1], "../../etc")


def test_remove_deletes_folder_and_backup(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    install(make_tar(TOP, good_entries("2.0.0")), dirs, version="2.0.0")
    assert remove_integration(dirs[0], dirs[1], "demo_integration") is True
    assert not (dirs[0] / "demo_integration").exists()
    assert not (dirs[1] / "demo_integration").exists()
    assert remove_integration(dirs[0], dirs[1], "demo_integration") is False
    with pytest.raises(InstallerError):
        remove_integration(dirs[0], dirs[1], "../x")


def test_reserved_names_are_refused(dirs):
    with pytest.raises(InstallerError, match="reserved"):
        install(make_tar(TOP, good_entries()), dirs, is_reserved=lambda n: n == "demo_integration")
    assert names(dirs[0]) == []


def test_installed_folder_has_normal_permissions(dirs):
    install(make_tar(TOP, good_entries()), dirs)
    assert (dirs[0] / "demo_integration").stat().st_mode & 0o777 == 0o755


def test_extended_header_bomb_is_capped_while_decompressing(dirs, monkeypatch):
    """tarfile reads a whole pax header into memory: the cap must be on the decompressed stream."""
    monkeypatch.setattr(installer, "MAX_ARCHIVE_BYTES", 1024 * 1024)
    info = tarfile.TarInfo("pax")
    info.type = tarfile.XHDTYPE
    info.size = 16 * 1024 * 1024  # 16 MB of zeros, ~16 KB once gzipped
    bomb = gzip.compress(info.tobuf(tarfile.GNU_FORMAT) + b"\0" * info.size)
    assert len(bomb) < 100_000
    with pytest.raises(InstallerError, match="Archive is too large"):
        install(bomb, dirs)
    assert names(dirs[0]) == []


def test_failed_swap_restores_the_previous_version(dirs, monkeypatch):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    real_rename = installer.os.rename

    def failing(src, dst):
        if Path(src).name.startswith(".ph-stage-"):  # the last step: staging -> live folder
            raise OSError("disk full")
        return real_rename(src, dst)

    monkeypatch.setattr(installer.os, "rename", failing)
    with pytest.raises(InstallerError, match="disk full"):
        install(make_tar(TOP, good_entries("2.0.0")), dirs, version="2.0.0")
    monkeypatch.undo()
    assert b'"1.0.0"' in (dirs[0] / "demo_integration" / "manifest.json").read_bytes()
    assert names(dirs[0]) == ["demo_integration"]
    assert not list(dirs[1].glob("*.pending"))


def test_failed_rollback_is_undone(dirs, monkeypatch):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    install(make_tar(TOP, good_entries("2.0.0")), dirs, version="2.0.0")
    real_rename = installer.os.rename
    calls = []

    def failing(src, dst):
        calls.append(src)
        if len(calls) == 2:  # backup -> live folder
            raise OSError("boom")
        return real_rename(src, dst)

    monkeypatch.setattr(installer.os, "rename", failing)
    with pytest.raises(InstallerError, match="failed and was undone"):
        rollback_integration(dirs[0], dirs[1], "demo_integration")
    monkeypatch.undo()
    assert b'"2.0.0"' in (dirs[0] / "demo_integration" / "manifest.json").read_bytes()
    assert b'"1.0.0"' in (dirs[1] / "demo_integration" / "manifest.json").read_bytes()


def test_installed_version_reports_what_is_on_disk(dirs):
    assert installed_version(dirs[0], "demo_integration") is None
    install(make_tar(TOP, good_entries("1.4.2")), dirs, version="1.4.2")
    assert installed_version(dirs[0], "demo_integration") == "1.4.2"
    manifest = dirs[0] / "demo_integration" / "manifest.json"
    manifest.write_text('{"version": 5}')
    assert installed_version(dirs[0], "demo_integration") == "?"
    manifest.write_text("not json")
    assert installed_version(dirs[0], "demo_integration") == "?"
    with pytest.raises(InstallerError):
        installed_version(dirs[0], "../x")


def test_recover_removes_leftover_staging_folders(dirs):
    (dirs[0] / ".ph-stage-abc").mkdir(parents=True)
    (dirs[0] / ".ph-stage-abc" / "x.py").write_text("x")
    (dirs[0] / "other_integration").mkdir()
    assert recover(dirs[0], dirs[1]) == ["removed leftover .ph-stage-abc"]
    assert names(dirs[0]) == ["other_integration"]


def test_recover_restores_an_interrupted_update(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    install(make_tar(TOP, good_entries("2.0.0")), dirs, version="2.0.0")
    # crash between "live -> backup" and "staging -> live": live folder gone, marker present
    installer.shutil.rmtree(dirs[0] / "demo_integration")
    (dirs[1] / "demo_integration.pending").write_text("swap in progress\n")
    notes = recover(dirs[0], dirs[1])
    assert any("restored demo_integration" in n for n in notes)
    assert b'"1.0.0"' in (dirs[0] / "demo_integration" / "manifest.json").read_bytes()
    assert not (dirs[1] / "demo_integration.pending").exists()


def test_recover_does_not_resurrect_what_the_user_deleted(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    install(make_tar(TOP, good_entries("2.0.0")), dirs, version="2.0.0")
    installer.shutil.rmtree(dirs[0] / "demo_integration")  # no marker: deliberate removal
    assert recover(dirs[0], dirs[1]) == []
    assert not (dirs[0] / "demo_integration").exists()


def test_recover_settles_an_interrupted_rollback(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    install(make_tar(TOP, good_entries("2.0.0")), dirs, version="2.0.0")
    # crash after "live -> parked": live folder missing, backup (1.0.0) and parked (2.0.0) exist
    installer.os.rename(dirs[0] / "demo_integration", dirs[1] / "demo_integration.swap")
    recover(dirs[0], dirs[1])
    assert (dirs[0] / "demo_integration").exists()
    assert not (dirs[1] / "demo_integration.swap").exists()


# --- binding of the archive to the signed commit -------------------------------------------------


def test_the_full_commit_in_the_archive_must_match(dirs):
    """A 7-hex prefix collision on the folder name is not enough: the pax header is checked."""
    other = COMMIT[:7] + "0" * 33  # same short id, another commit
    with pytest.raises(InstallerError, match="signed commit"):
        install(make_tar(TOP, good_entries(), commit=other), dirs)
    assert names(dirs[0]) == []


def test_an_archive_without_the_commit_header_is_refused(dirs):
    with pytest.raises(InstallerError, match="signed commit"):
        install(make_tar(TOP, good_entries(), commit=None), dirs)


def test_a_wrong_top_folder_is_refused_even_with_the_right_header(dirs):
    with pytest.raises(InstallerError, match="signed commit"):
        install(make_tar("acme-demo-1111111", good_entries()), dirs)


# --- names and types inside the integration ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["naïve.py", "sp ace.py", "back\\slash.py", "col:on.py", "x" * 256 + ".py", "tab\t.py", "ünï/x.py"],
)
def test_unusual_file_names_are_refused(dirs, name):
    with pytest.raises(InstallerError):
        install(make_tar(TOP, good_entries() + [(f"{BASE}/{name}", b"x")]), dirs)
    assert names(dirs[0]) == []


def test_names_that_differ_only_by_case_are_refused(dirs):
    entries = good_entries() + [(f"{BASE}/Helper.py", b"a"), (f"{BASE}/helper.py", b"b")]
    with pytest.raises(InstallerError, match="Duplicate"):
        install(make_tar(TOP, entries), dirs)


@pytest.mark.parametrize("ext", [".PY", ".Json", ".JSON"])
def test_extensions_are_matched_exactly(dirs, ext):
    with pytest.raises(InstallerError, match="File type"):
        install(make_tar(TOP, good_entries() + [(f"{BASE}/x{ext}", b"x")]), dirs)


def test_sparse_files_are_refused(dirs):
    info = tarfile.TarInfo(f"{TOP}/{BASE}/sparse.py")
    info.type = tarfile.GNUTYPE_SPARSE
    raw = info.tobuf(tarfile.GNU_FORMAT) + b"\0" * 1024
    with pytest.raises(InstallerError):
        install(gzip.compress(raw), dirs)
    assert names(dirs[0]) == []


# --- provenance, rollback, remove ----------------------------------------------------------------


def test_provenance_is_recorded_and_read_back(dirs):
    install(make_tar(TOP, good_entries()), dirs)
    prov = read_provenance(dirs[0] / "demo_integration")
    assert prov == {"commit": COMMIT, "fingerprint": FPR, "version": "1.0.0"}


@pytest.mark.parametrize(
    "content",
    [
        "not json", "[]", "{}", '{"commit": "x", "fingerprint": "SHA256:a", "version": "1.0.0"}',
        f'{{"commit": "{COMMIT}", "fingerprint": "abc", "version": "1.0.0"}}',
        f'{{"commit": "{COMMIT}", "fingerprint": "SHA256:a", "version": "1.0"}}',
        '{"commit": 5, "fingerprint": "SHA256:a", "version": "1.0.0"}',
    ],
)
def test_invalid_provenance_is_ignored(dirs, content):
    install(make_tar(TOP, good_entries()), dirs)
    (dirs[0] / "demo_integration" / ".private_hacs.json").write_text(content)
    assert read_provenance(dirs[0] / "demo_integration") is None


def test_rollback_returns_the_provenance_of_the_restored_version(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    install(
        make_tar("acme-demo-1111111", good_entries("2.0.0"), commit="1" * 40),
        dirs, version="2.0.0", commit="1" * 40,
    )
    version, prov = rollback_integration(dirs[0], dirs[1], "demo_integration")
    assert (version, prov["commit"]) == ("1.0.0", COMMIT)


def test_remove_reports_what_it_could_not_delete(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    install(make_tar(TOP, good_entries("2.0.0")), dirs, version="2.0.0")
    locked = dirs[1] / "demo_integration"
    locked.chmod(0o500)  # its files can no longer be deleted
    try:
        with pytest.raises(InstallerError, match="Could not delete"):
            remove_integration(dirs[0], dirs[1], "demo_integration")
    finally:
        locked.chmod(0o700)


def test_remove_also_clears_recovery_markers(dirs):
    install(make_tar(TOP, good_entries("1.0.0")), dirs)
    dirs[1].mkdir(exist_ok=True)
    (dirs[1] / "demo_integration.pending").write_text("x")
    (dirs[1] / "demo_integration.swap").mkdir()
    assert remove_integration(dirs[0], dirs[1], "demo_integration") is True
    assert names(dirs[1]) == []
