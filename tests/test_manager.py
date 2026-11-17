"""End to end: signed releases in a real git repo -> manager -> files in custom_components/."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.private_hacs.manager import PrivateHacsManager
from custom_components.private_hacs.sigverify import parse_public_keys
from custom_components.private_hacs.store import RepositoryStore
from tests.conftest import OWNER_REPO, TOKEN, make_tar, manifest_bytes


@pytest.fixture
async def manager(hass, hass_storage, repo, client, tmp_path):
    hass.config.config_dir = str(tmp_path / "ha")  # never touch the real/test config dir
    store = RepositoryStore(hass)
    await store.async_load()
    return PrivateHacsManager(
        hass, store, parse_public_keys(repo.pubkey()), client_factory=lambda token: client
    )


def installed(hass) -> Path:
    return Path(hass.config.path("custom_components", "demo_integration"))


def version_of(hass) -> str:
    import json

    return json.loads((installed(hass) / "manifest.json").read_text())["version"]


async def test_installs_the_newest_signed_release(hass, repo, client, manager):
    repo.release("1.0.0")
    repo.release("1.1.0", requirements=["somepkg>=1"])
    await manager.add_repository("https://github.com/acme/demo", "demo_integration", token=TOKEN)
    result = await manager.install_or_update(OWNER_REPO, token=TOKEN)

    assert (result.installed_version, version_of(hass)) == ("1.1.0", "1.1.0")
    assert result.installed_commit == repo.tags["v1.1.0"]["commit"]
    assert result.installed_fingerprint.startswith("SHA256:")
    assert client.downloads == [repo.tags["v1.1.0"]["commit"]]  # downloaded by commit, not by name
    assert not (installed(hass) / "tests").exists()
    persisted = manager.store.get(OWNER_REPO)
    assert persisted.integration_name == "demo_integration"


async def test_a_bogus_newer_tag_does_not_freeze_updates(hass, repo, client, manager):
    repo.release("1.0.0")
    repo.release("1.1.0", signer="evil")
    repo.release("999.0.0", signer=None)  # unsigned, to make "latest" unusable
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    result = await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert result.installed_version == "1.0.0"
    assert client.downloads == [repo.tags["v1.0.0"]["commit"]]  # nothing of the bogus tags fetched


async def test_an_explicit_bad_tag_is_refused_not_replaced(hass, repo, client, manager):
    repo.release("1.0.0")
    bad = repo.release("1.1.0", signer="evil")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    with pytest.raises(HomeAssistantError, match="not valid"):
        await manager.install_or_update(OWNER_REPO, bad, token=TOKEN)
    assert not installed(hass).exists()
    assert client.downloads == []


async def test_nothing_is_installed_when_no_tag_verifies(hass, repo, client, manager):
    repo.release("1.0.0", signer="evil")
    repo.release("1.1.0", signer=None)
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    with pytest.raises(HomeAssistantError, match="v1.1.0.*not signed.*v1.0.0.*not valid"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert not installed(hass).exists()
    assert client.downloads == []


async def test_a_moved_tag_with_the_same_version_is_refused(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    old_commit = repo.tags["v1.0.0"]["commit"]
    repo.retag("v1.0.0")
    repo.release("1.0.0")  # same name, same version, other (also signed) commit
    assert repo.tags["v1.0.0"]["commit"] != old_commit
    with pytest.raises(HomeAssistantError, match="different commit"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    await manager.install_or_update(OWNER_REPO, allow_downgrade=True, token=TOKEN)  # explicit ok
    assert manager.store.get(OWNER_REPO).installed_commit == repo.tags["v1.0.0"]["commit"]


async def test_what_is_on_disk_beats_what_we_remember(hass, repo, client, manager):
    repo.release("1.0.0")
    repo.release("2.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    manager.store.get(OWNER_REPO).installed_version = None  # e.g. removed and re-added
    manager.store.get(OWNER_REPO).installed_commit = None
    with pytest.raises(HomeAssistantError, match="Refusing to go from 2.0.0 down to 1.0.0"):
        await manager.install_or_update(OWNER_REPO, "v1.0.0", token=TOKEN)


@pytest.mark.parametrize("on_disk", ['"1.2.3-beta"', "5", "null", '"?"'])
async def test_an_incomparable_installed_version_fails_closed(hass, repo, client, manager, on_disk):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    manifest = installed(hass) / "manifest.json"
    manifest.write_text(
        manifest.read_text().replace('"version": "1.0.0"', f'"version": {on_disk}')
    )
    with pytest.raises(HomeAssistantError, match="cannot be compared"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    await manager.install_or_update(OWNER_REPO, allow_downgrade=True, token=TOKEN)
    assert version_of(hass) == "1.0.0"


async def test_reserved_and_taken_integration_names_are_refused(hass, repo, client, manager):
    await manager.add_repository(OWNER_REPO, integration_name="demo_integration", token=TOKEN)
    for name in ("private_hacs", "zha", "demo_integration"):
        with pytest.raises(HomeAssistantError, match="reserved or already in use"):
            await manager.add_repository("someone/else", integration_name=name, token=TOKEN)


async def test_nothing_is_downloaded_when_the_signature_fails(hass, repo, client, manager):
    tag = repo.release("1.0.0", signer=None)
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    with pytest.raises(HomeAssistantError, match="not signed"):
        await manager.install_or_update(OWNER_REPO, tag, token=TOKEN)
    assert client.downloads == []
    assert not installed(hass).exists()


async def test_a_swapped_tarball_is_refused(hass, repo, client, manager):
    tag = repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    # right commit signed, but the download is another commit's content / has other content
    client.tarball_override = make_tar(
        "acme-demo-1111111",
        [("custom_components/demo_integration/manifest.json", manifest_bytes("1.0.0")),
         ("custom_components/demo_integration/__init__.py", b"import os\n")],
    )
    with pytest.raises(HomeAssistantError, match="signed commit"):
        await manager.install_or_update(OWNER_REPO, tag, token=TOKEN)
    assert not installed(hass).exists()


async def test_downgrade_needs_an_explicit_ok(hass, repo, client, manager):
    repo.release("1.0.0")
    repo.release("2.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    with pytest.raises(HomeAssistantError, match="Refusing to go from 2.0.0 down to 1.0.0"):
        await manager.install_or_update(OWNER_REPO, "v1.0.0", token=TOKEN)
    assert version_of(hass) == "2.0.0"
    await manager.install_or_update(OWNER_REPO, "v1.0.0", allow_downgrade=True, token=TOKEN)
    assert version_of(hass) == "1.0.0"


async def test_reinstalling_the_same_code_needs_an_explicit_ok_and_repairs_the_files(
    hass, repo, client, manager
):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    (installed(hass) / "__init__.py").write_text("# tampered\n")
    with pytest.raises(HomeAssistantError, match="already installed"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert (installed(hass) / "__init__.py").read_text() == "# tampered\n"
    await manager.install_or_update(OWNER_REPO, token=TOKEN, allow_downgrade=True)
    assert (installed(hass) / "__init__.py").read_text() == "# demo\n"


async def test_reinstalling_does_not_destroy_the_rollback_copy_by_accident(hass, repo, client, manager):
    repo.release("1.0.0")
    repo.release("1.1.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, "v1.0.0", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)  # 1.1.0, backup is 1.0.0
    # e.g. an attacker deleted the newer tag so that 1.1.0 is again "the newest": nothing happens
    with pytest.raises(HomeAssistantError, match="already installed"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    repo_after = await manager.rollback(OWNER_REPO)
    assert (repo_after.installed_version, version_of(hass)) == ("1.0.0", "1.0.0")


async def test_rollback_restores_the_previous_version(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    repo.release("1.1.0")
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert version_of(hass) == "1.1.0"
    result = await manager.rollback(OWNER_REPO)
    assert (result.installed_version, version_of(hass)) == ("1.0.0", "1.0.0")


async def test_rollback_without_previous_version_fails(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    with pytest.raises(HomeAssistantError, match="no recorded signature"):
        await manager.rollback(OWNER_REPO)


async def test_without_trusted_keys_nothing_is_installed(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    manager.trusted = []
    with pytest.raises(HomeAssistantError, match="No trusted signing key"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert not installed(hass).exists()


async def test_remove_deletes_files_and_stops_tracking(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    await manager.remove_repository(OWNER_REPO)
    assert not installed(hass).exists()
    assert manager.store.get(OWNER_REPO) is None


async def test_repo_without_release_tags(hass, repo, client, manager):
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    with pytest.raises(HomeAssistantError, match="no vX.Y.Z tags"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)


@pytest.mark.parametrize("value", ["", "../x", "a/b/c", "owner/", "/repo", "ow ner/repo", "x/y;rm"])
async def test_bad_repository_references_are_refused(manager, value):
    with pytest.raises(HomeAssistantError):
        await manager.add_repository(value, "demo_integration", token=TOKEN)


async def test_unreachable_repository_is_a_clean_error(manager):
    with pytest.raises(HomeAssistantError, match="Cannot access"):
        await manager.add_repository("someone/else", "demo_integration", token=TOKEN)


async def test_bad_integration_name_is_refused(manager):
    with pytest.raises(HomeAssistantError, match="Invalid integration name"):
        await manager.add_repository(OWNER_REPO, integration_name="../evil", token=TOKEN)


async def test_adding_twice_and_acting_on_untracked_repos(hass, repo, manager):
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    with pytest.raises(HomeAssistantError, match="already tracked"):
        await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    with pytest.raises(HomeAssistantError, match="not tracked"):
        await manager.install_or_update("acme/other", token=TOKEN)


async def test_a_core_integration_name_cannot_be_shadowed(hass, repo, client, manager):
    tag = repo.release("1.0.0")
    # the same signed release, but the manager is told to put it under a core name
    await manager.add_repository(OWNER_REPO, integration_name="demo_integration", token=TOKEN)
    manager.store.get(OWNER_REPO).integration_name = "zha"
    with pytest.raises(HomeAssistantError, match="not found|reserved"):
        await manager.install_or_update(OWNER_REPO, tag, token=TOKEN)
    from custom_components.private_hacs.manager import _is_core_integration

    assert _is_core_integration("zha") and _is_core_integration("light")
    assert not _is_core_integration("demo_integration")


async def test_every_github_action_needs_a_token_and_never_keeps_it(
    hass, hass_storage, repo, client, manager
):
    repo.release("1.0.0")
    seen: list[str] = []
    manager._client_factory = lambda token: (seen.append(token), client)[1]

    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert seen == [TOKEN, TOKEN]  # a fresh client per action, built from what was typed

    for bad in ("", "   ", None, "short", "ghp_" + "a" * 30 + "\nX-Evil: 1", "tok en" + "a" * 30):
        with pytest.raises(HomeAssistantError, match="token"):
            await manager.install_or_update(OWNER_REPO, token=bad)
    assert seen == [TOKEN, TOKEN]  # nothing was built for the refused ones

    # the token is nowhere: not on the manager, not in the persisted store, not in any storage
    assert TOKEN not in repr(vars(manager))
    assert TOKEN not in repr(manager.store.__dict__)
    assert TOKEN not in str(hass_storage)


async def test_rollback_and_remove_do_not_need_a_token(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    repo.release("1.1.0")
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    await manager.rollback(OWNER_REPO)
    await manager.remove_repository(OWNER_REPO)
    assert not installed(hass).exists()


async def test_a_rejected_token_is_a_clean_error_without_the_token(hass, repo, client, manager):
    from custom_components.private_hacs.github import GitHubAuthError

    async def refuse(full_name):
        raise GitHubAuthError("Auth failed (401) for /repos/acme/demo")

    client.get_repo = refuse
    with pytest.raises(HomeAssistantError) as err:
        await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    assert "401" in str(err.value) and TOKEN not in str(err.value)


# --- review round: pinning, provenance, cancellation, reconciliation ----------------------------


async def test_the_pinned_integration_name_stops_a_release_of_another_integration(
    hass, repo, client, manager
):
    repo.release("1.0.0")  # contains custom_components/demo_integration
    await manager.add_repository(OWNER_REPO, "other_integration", token=TOKEN)
    with pytest.raises(HomeAssistantError, match="no custom_components"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert not installed(hass).exists()


async def test_the_integration_name_is_required(manager):
    for bad in ("", None, "../x", "_x", "Demo"):
        with pytest.raises(HomeAssistantError, match="Invalid integration name"):
            await manager.add_repository(OWNER_REPO, bad, token=TOKEN)


async def test_a_folder_not_installed_by_us_is_never_overwritten(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    mine = installed(hass)
    mine.mkdir(parents=True)
    (mine / "manifest.json").write_text('{"domain": "demo_integration", "version": "9.9.9"}')
    (mine / "__init__.py").write_text("# hand made\n")
    with pytest.raises(HomeAssistantError, match="not installed by Private HACS"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert (mine / "__init__.py").read_text() == "# hand made\n"
    assert client.downloads == []


async def test_rollback_keeps_the_provenance_so_the_guards_keep_working(hass, repo, client, manager):
    repo.release("1.0.0")
    repo.release("1.1.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, "v1.0.0", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    restored = await manager.rollback(OWNER_REPO)
    assert restored.installed_commit == repo.tags["v1.0.0"]["commit"]
    assert restored.installed_fingerprint.startswith("SHA256:")
    # and the moved-tag guard still sees the real commit after the rollback
    old = repo.tags["v1.0.0"]["commit"]
    repo.retag("v1.0.0")
    repo.release("1.0.0")
    assert repo.tags["v1.0.0"]["commit"] != old
    with pytest.raises(HomeAssistantError, match="different commit"):
        await manager.install_or_update(OWNER_REPO, "v1.0.0", token=TOKEN)


async def test_rollback_refuses_code_signed_by_a_key_that_is_no_longer_trusted(
    hass, repo, client, manager
):
    manager.trusted = parse_public_keys(repo.pubkey("good") + "\n" + repo.pubkey("evil"))
    repo.release("1.0.0", signer="evil")
    repo.release("1.1.0", signer="good")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, "v1.0.0", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    manager.trusted = parse_public_keys(repo.pubkey("good"))  # the key was revoked
    with pytest.raises(HomeAssistantError, match="no longer trusted"):
        await manager.rollback(OWNER_REPO)
    assert version_of(hass) == "1.1.0"


async def test_a_cancelled_dialog_cannot_leave_disk_and_store_apart(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    caller = asyncio.ensure_future(manager.install_or_update(OWNER_REPO, token=TOKEN))
    await asyncio.sleep(0)  # the work has started
    caller.cancel()  # browser tab closed
    with pytest.raises(asyncio.CancelledError):
        await caller
    await hass.async_block_till_done()
    async with manager._lock:  # whatever was running has finished
        pass
    assert version_of(hass) == "1.0.0"
    assert manager.store.get(OWNER_REPO).installed_version == "1.0.0"
    assert manager.store.get(OWNER_REPO).installed_commit == repo.tags["v1.0.0"]["commit"]


async def test_recovery_makes_the_store_match_the_disk(hass, hass_storage, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    tracked = manager.store.get(OWNER_REPO)
    commit = repo.tags["v1.0.0"]["commit"]

    # crash between the swap and the save: the store is behind
    tracked.installed_version = tracked.installed_commit = tracked.installed_fingerprint = None
    await manager.async_recover()
    assert (tracked.installed_version, tracked.installed_commit) == ("1.0.0", commit)
    assert tracked.installed_fingerprint.startswith("SHA256:")

    # the store claims something the disk does not have
    tracked.installed_version = "7.7.7"
    await manager.async_recover()
    assert tracked.installed_version == "1.0.0"

    # the folder was deleted by hand
    import shutil

    shutil.rmtree(installed(hass))
    await manager.async_recover()
    assert (tracked.installed_version, tracked.installed_commit) == (None, None)


async def test_remove_that_fails_keeps_the_repository_tracked(hass, repo, client, manager):
    repo.release("1.0.0")
    repo.release("1.1.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, "v1.0.0", token=TOKEN)
    await manager.install_or_update(OWNER_REPO, token=TOKEN)
    locked = manager._backup_dir / "demo_integration"
    locked.chmod(0o500)
    try:
        with pytest.raises(HomeAssistantError, match="Could not delete"):
            await manager.remove_repository(OWNER_REPO)
        assert manager.store.get(OWNER_REPO) is not None
    finally:
        locked.chmod(0o700)
    await manager.remove_repository(OWNER_REPO)
    assert manager.store.get(OWNER_REPO) is None


async def test_access_to_the_repository_is_checked_before_anything_else(hass, repo, client, manager):
    repo.release("1.0.0")
    await manager.add_repository(OWNER_REPO, "demo_integration", token=TOKEN)
    client.full_name = "someone/else"  # the token cannot see it any more
    with pytest.raises(HomeAssistantError, match="Cannot access"):
        await manager.install_or_update(OWNER_REPO, token=TOKEN)
    assert client.downloads == []


@pytest.mark.parametrize("field", ["tag", "repo"])
async def test_a_token_pasted_in_the_wrong_field_is_never_echoed(manager, field):
    with pytest.raises(HomeAssistantError) as err:
        if field == "tag":
            await manager.install_or_update(OWNER_REPO, TOKEN, token=TOKEN)
        else:
            await manager.add_repository(TOKEN, "demo_integration", token=TOKEN)
    assert TOKEN not in str(err.value)
