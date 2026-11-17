"""Fixtures: a real git repo with SSH-signed tags, and a fake GitHub client that serves it.

The signatures are made by `git tag -s` (gpg.format=ssh), so the verifier is tested
against what a real developer machine produces, not against its own output.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from custom_components.private_hacs.github import GitHubError

OWNER_REPO = "acme/demo"
DEFAULT_COMMIT = "abcdef0123456789abcdef0123456789abcdef01"
TOKEN = "github_pat_" + "A1b2C3d4E5" * 4  # shaped like a fine-grained token
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load custom_components/private_hacs from the repository root."""


def _run(args: list[str], cwd: Path, *, binary: bool = False):
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )
    return result.stdout if binary else result.stdout.decode()


class SignedRepo:
    """A git repository with an integration `demo_integration`, released with signed tags."""

    def __init__(self, root: Path) -> None:
        self.root = root / "src"
        self.keys = root / "keys"
        self.keys.mkdir()
        self.root.mkdir()
        for name in ("good", "evil"):
            _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.keys / name)], root)
        _run(["git", "init", "-q", "-b", "main"], self.root)
        self.integration = self.root / "custom_components" / "demo_integration"
        self.integration.mkdir(parents=True)
        (self.integration / "__init__.py").write_text("# demo\n")
        (self.integration / "translations").mkdir()
        (self.integration / "translations" / "en.json").write_text("{}\n")
        self.tags: dict[str, dict] = {}
        self.revisions = 0

    def retag(self, tag: str) -> None:
        """Delete a tag locally so the same name can be released again (a moved tag)."""
        _run(["git", "tag", "-d", tag], self.root)

    def pubkey(self, name: str = "good") -> str:
        return (self.keys / f"{name}.pub").read_text().strip()

    def _commit(self, version: str, requirements: list[str]) -> str:
        manifest = {"domain": "demo_integration", "name": "Demo", "version": version,
                    "requirements": requirements}
        (self.integration / "manifest.json").write_text(json.dumps(manifest))
        self.revisions += 1  # every release is a new commit, even with identical content
        (self.integration / "rev.txt").write_text(f"{self.revisions}\n")
        _run(["git", "add", "-A"], self.root)
        _run(["git", "commit", "-q", "-m", f"demo {version}"], self.root)
        return _run(["git", "rev-parse", "HEAD"], self.root).strip()

    def release(
        self,
        version: str,
        *,
        signer: str | None = "good",
        annotated: bool = True,
        requirements: list[str] | None = None,
        manifest_version: str | None = None,
    ) -> str:
        """Commit and tag `v<version>`; returns the tag name."""
        tag = f"v{version}"
        commit = self._commit(manifest_version or version, requirements or [])
        if not annotated:
            _run(["git", "tag", tag], self.root)
        elif signer is None:
            _run(["git", "tag", "-a", tag, "-m", f"demo {version}"], self.root)
        else:
            key = str(self.keys / f"{signer}.pub")
            _run(["git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}",
                  "tag", "-s", tag, "-m", f"demo {version}"], self.root)
        self.tags[tag] = self._describe(tag, commit, annotated)
        return tag

    def _describe(self, tag: str, commit: str, annotated: bool) -> dict:
        archive = _run(
            ["git", "archive", "--format=tar.gz", f"--prefix={OWNER_REPO.replace('/', '-')}-{commit[:7]}/", tag],
            self.root,
            binary=True,
        )
        info = {"commit": commit, "archive": archive, "annotated": annotated}
        if annotated:
            raw = _run(["git", "cat-file", "tag", tag], self.root)
            sha = _run(["git", "rev-parse", tag], self.root).strip()
            marker = "-----BEGIN SSH SIGNATURE-----"
            payload, sep, sig = raw.partition(marker)
            info["sha"] = sha
            info["verification"] = (
                {"verified": False, "reason": "unknown_key", "payload": payload,
                 "signature": marker + sig}
                if sep
                else {"verified": False, "reason": "unsigned", "payload": None, "signature": None}
            )
        return info


@pytest.fixture
def repo(tmp_path) -> SignedRepo:
    return SignedRepo(tmp_path)


class FakeClient:
    """Serves a SignedRepo through the same methods as GitHubClient."""

    def __init__(self, repo: SignedRepo, full_name: str = OWNER_REPO) -> None:
        self.repo = repo
        self.full_name = full_name
        self.aliases: dict[str, str] = {}  # served tag name -> real tag (for replay tests)
        self.downloads: list[str] = []
        self.tarball_override: bytes | None = None

    async def get_repo(self, full_name):
        if full_name != self.full_name:
            raise GitHubError("not found")
        return {"full_name": full_name}

    async def list_tag_names(self, full_name):
        return list(self.repo.tags)

    def _real(self, tag):
        return self.aliases.get(tag, tag)

    async def get_tag_ref(self, full_name, tag):
        info = self.repo.tags.get(self._real(tag))
        if info is None:
            return None
        kind = "tag" if info["annotated"] else "commit"
        return {"object": {"type": kind, "sha": info.get("sha", info["commit"])}}

    async def get_tag_object(self, full_name, sha):
        for info in self.repo.tags.values():
            if info.get("sha") == sha:
                return {"sha": sha, "verification": info["verification"]}
        raise GitHubError("no such tag object")

    async def download_tarball(self, full_name, commit):
        self.downloads.append(commit)
        if self.tarball_override is not None:
            return self.tarball_override
        for info in self.repo.tags.values():
            if info["commit"] == commit:
                return info["archive"]
        raise GitHubError("no such commit")


@pytest.fixture
def client(repo) -> FakeClient:
    return FakeClient(repo)


def make_tar(
    top: str, entries: list[tuple], *, gz: bool = True, commit: str | None = DEFAULT_COMMIT
) -> bytes:
    """Build a tarball like GitHub's (a pax global header carries the full commit id).

    entries: (name, bytes) file | (name, None) dir | (name, ('sym'|'hard', target));
    a name starting with '=' is used verbatim (no top folder prefix).
    """
    buf = io.BytesIO()
    with tarfile.open(
        fileobj=buf,
        mode="w:gz" if gz else "w",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": commit} if commit else None,
    ) as tf:
        for name, content in entries:
            info = tarfile.TarInfo(name[1:] if name.startswith("=") else f"{top}/{name}")
            if content is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif isinstance(content, tuple):
                info.type = tarfile.SYMTYPE if content[0] == "sym" else tarfile.LNKTYPE
                info.linkname = content[1]
                tf.addfile(info)
            else:
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def manifest_bytes(version: str = "1.0.0", domain: str = "demo_integration", **extra) -> bytes:
    return json.dumps({"domain": domain, "name": "Demo", "version": version, **extra}).encode()
