"""Decide which release to install, and prove it was signed by a trusted key.

Trust chain: the *signed payload* of an annotated tag names the tag and the commit.
Nothing else that GitHub returns (tag lists, ref objects, tarballs) is trusted to
choose what gets installed: the commit comes from the signed payload only.
"""
from __future__ import annotations

from dataclasses import dataclass

from .const import SHA_RE, SIGNATURE_NAMESPACE, TAG_RE, VERSION_RE
from .github import GitHubClient
from .sigverify import SignatureError, TrustedKey, verify_ssh_signature


class ReleaseError(Exception):
    """The release cannot be trusted or does not exist."""


@dataclass(frozen=True)
class VerifiedRelease:
    tag: str
    version: str  # "1.2.3"
    commit: str  # full sha, taken from the signed payload
    fingerprint: str  # of the key that signed


def version_tuple(version: str) -> tuple[int, int, int]:
    """'1.2.3' or 'v1.2.3' -> (1, 2, 3); ValueError for anything else."""
    match = VERSION_RE.fullmatch(version.removeprefix("v")) if isinstance(version, str) else None
    if not match:
        raise ValueError(f"not a X.Y.Z version: {version!r}")
    return int(match[1]), int(match[2]), int(match[3])


def newest_tags(names: list[str], limit: int) -> list[str]:
    """The `limit` highest vX.Y.Z tags, newest first (other names are ignored)."""
    valid = {n for n in names if TAG_RE.fullmatch(n)}
    return sorted(valid, key=version_tuple, reverse=True)[:limit]


def latest_tag(names: list[str]) -> str | None:
    return next(iter(newest_tags(names, 1)), None)


def _signed_fields(payload: str) -> dict[str, str]:
    """Header lines of a git tag object (`object`, `type`, `tag`, `tagger`)."""
    header = payload.split("\n\n", 1)[0]
    fields: dict[str, str] = {}
    for line in header.split("\n"):
        key, _, value = line.partition(" ")
        if key in fields:
            raise ReleaseError(f"signed tag has a duplicate '{key}' header")
        fields[key] = value
    return fields


async def verify_tag(
    client: GitHubClient, full_name: str, tag: str, trusted: list[TrustedKey]
) -> VerifiedRelease:
    """Return the verified release for `tag`, or raise ReleaseError."""
    if not TAG_RE.fullmatch(tag):
        raise ReleaseError("the tag name is not a vX.Y.Z tag")
    if not trusted:
        raise ReleaseError("no trusted signing key is configured")

    ref = await client.get_tag_ref(full_name, tag)
    if ref is None:
        raise ReleaseError(f"tag {tag} does not exist in {full_name}")
    ref_object = ref.get("object") or {}
    if ref_object.get("type") != "tag" or not SHA_RE.fullmatch(str(ref_object.get("sha", ""))):
        raise ReleaseError(f"tag {tag} is not an annotated tag (lightweight tags are refused)")

    tag_object = await client.get_tag_object(full_name, ref_object["sha"])
    verification = tag_object.get("verification") or {}
    payload = verification.get("payload")
    signature = verification.get("signature")
    if not isinstance(payload, str) or not isinstance(signature, str) or not signature:
        reason = verification.get("reason")
        raise ReleaseError(f"tag {tag} is not signed (GitHub reason: {reason})")

    try:
        signer = verify_ssh_signature(
            payload.encode(), signature, trusted, namespace=SIGNATURE_NAMESPACE
        )
    except SignatureError as err:
        raise ReleaseError(f"signature of {tag} is not valid: {err}") from err

    fields = _signed_fields(payload)
    if fields.get("tag") != tag:
        declared = fields.get("tag", "")
        shown = declared if TAG_RE.fullmatch(declared) else "(not a release tag name)"
        raise ReleaseError(
            f"the signature is valid but declares the tag name {shown}, not {tag}"
        )
    if fields.get("type") != "commit":
        raise ReleaseError(f"tag {tag} does not point at a commit")
    commit = fields.get("object", "")
    if not SHA_RE.fullmatch(commit):
        raise ReleaseError(f"tag {tag} has no valid commit id")

    return VerifiedRelease(
        tag=tag,
        version=tag.removeprefix("v"),
        commit=commit,
        fingerprint=signer.fingerprint,
    )
