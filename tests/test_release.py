"""Which tag is trusted: the attacks on the tag-verification logic."""
from __future__ import annotations

import pytest

from custom_components.private_hacs.release import (
    ReleaseError,
    latest_tag,
    newest_tags,
    verify_tag,
    version_tuple,
)
from custom_components.private_hacs.sigverify import parse_public_keys

NAME = "acme/demo"


def trusted(repo, *names):
    return parse_public_keys("\n".join(repo.pubkey(n) for n in (names or ("good",))))


async def test_signed_annotated_tag_is_accepted(repo, client):
    tag = repo.release("1.2.3")
    rel = await verify_tag(client, NAME, tag, trusted(repo))
    assert (rel.tag, rel.version) == ("v1.2.3", "1.2.3")
    assert rel.commit == repo.tags[tag]["commit"]


async def test_commit_comes_from_the_signed_payload(repo, client):
    tag = repo.release("1.0.0")
    other = repo.release("1.0.1")
    # GitHub (or a MITM) serves the tag object of 1.0.1 but claims it is the one of 1.0.0:
    client.aliases[tag] = other
    with pytest.raises(ReleaseError, match="declares the tag name"):
        await verify_tag(client, NAME, tag, trusted(repo))


async def test_replaying_a_valid_signed_tag_under_another_name_is_refused(repo, client):
    repo.release("1.0.0")
    client.aliases["v9.9.9"] = "v1.0.0"
    repo.tags["v9.9.9"] = repo.tags["v1.0.0"]
    with pytest.raises(ReleaseError, match="declares the tag name v1.0.0, not v9.9.9"):
        await verify_tag(client, NAME, "v9.9.9", trusted(repo))


async def test_lightweight_tag_is_refused(repo, client):
    tag = repo.release("1.0.0", annotated=False)
    with pytest.raises(ReleaseError, match="annotated"):
        await verify_tag(client, NAME, tag, trusted(repo))


async def test_unsigned_annotated_tag_is_refused(repo, client):
    tag = repo.release("1.0.0", signer=None)
    with pytest.raises(ReleaseError, match="not signed"):
        await verify_tag(client, NAME, tag, trusted(repo))


async def test_tag_signed_by_unknown_key_is_refused(repo, client):
    tag = repo.release("1.0.0", signer="evil")
    with pytest.raises(ReleaseError, match="not valid"):
        await verify_tag(client, NAME, tag, trusted(repo, "good"))


async def test_tampered_payload_is_refused(repo, client):
    tag = repo.release("1.0.0")
    v = repo.tags[tag]["verification"]
    v["payload"] = v["payload"].replace("object ", "object 0", 1)  # change the signed commit id
    with pytest.raises(ReleaseError, match="not valid"):
        await verify_tag(client, NAME, tag, trusted(repo))


async def test_missing_tag_is_refused(repo, client):
    with pytest.raises(ReleaseError, match="does not exist"):
        await verify_tag(client, NAME, "v7.7.7", trusted(repo))


@pytest.mark.parametrize(
    "tag",
    [
        "main", "1.0.0", "v1.0", "v1.0.0-rc1", "v1.0.0/../x", "V1.0.0",
        "v1.2.3\n",  # `$` would accept a trailing newline
        "v١.٢.٣",  # Arabic-Indic digits: \d would accept them
        "v01.2.3",  # leading zero: would rank equal to v1.2.3
    ],
)
async def test_non_release_tag_names_are_refused(repo, client, tag):
    with pytest.raises(ReleaseError, match="not a vX.Y.Z tag"):
        await verify_tag(client, NAME, tag, trusted(repo))


async def test_no_trusted_keys_means_nothing_is_installed(repo, client):
    tag = repo.release("1.0.0")
    with pytest.raises(ReleaseError, match="no trusted"):
        await verify_tag(client, NAME, tag, [])


def test_latest_tag_uses_numeric_order_and_ignores_other_names():
    assert latest_tag(["v1.9.0", "v1.10.0", "v1.2.0", "nightly", "v2.0.0-rc1"]) == "v1.10.0"
    assert latest_tag(["nightly"]) is None
    assert latest_tag([]) is None


def test_newest_tags_are_sorted_and_limited():
    names = ["v1.0.0", "v1.10.0", "v1.2.0", "nightly", "v01.2.3", "v3.0.0"]
    assert newest_tags(names, 3) == ["v3.0.0", "v1.10.0", "v1.2.0"]
    assert newest_tags(names, 1) == ["v3.0.0"]


def test_version_tuple():
    assert version_tuple("v1.2.3") == (1, 2, 3)
    assert version_tuple("0.10.0") > version_tuple("0.9.9")


@pytest.mark.parametrize("bad", ["1.2", "1.2.3.4", "1.2.3\n", "١.٢.٣", "01.2.3", "1.2.3-beta", "", 5, None])
def test_version_tuple_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        version_tuple(bad)
