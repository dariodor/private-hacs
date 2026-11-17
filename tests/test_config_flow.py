"""Config and options flows, including that every form can be serialized for the UI."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import probatio
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.private_hacs.const import CONF_SIGNERS, DOMAIN
from tests.conftest import TOKEN


def serializable(result) -> None:
    """The frontend gets the schema as JSON: this fails if a field cannot be serialized."""
    if result.get("data_schema") is not None:
        probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)


def field_names(result) -> set[str]:
    return {f["name"] for f in probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)}


async def test_user_flow_asks_only_for_the_signing_keys_and_stores_no_token(hass, repo):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    serializable(result)
    assert field_names(result) == {CONF_SIGNERS}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SIGNERS: f"# mine\n{repo.pubkey()}\n"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SIGNERS: [repo.pubkey()]}


@pytest.mark.parametrize("signers", ["", "ssh-rsa AAAA", "not a key"])
async def test_user_flow_rejects_bad_signing_keys(hass, signers):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SIGNERS: signers})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SIGNERS: "invalid_signers"}


@pytest.fixture
async def entry(hass, hass_storage, repo, client, tmp_path):
    hass.config.config_dir = str(tmp_path / "ha")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Private HACS", data={CONF_SIGNERS: [repo.pubkey()]}
    )
    entry.add_to_hass(hass)
    seen: list[str] = []
    entry.seen_tokens = seen

    def build(session, token):
        seen.append(token)
        return client

    with patch("custom_components.private_hacs.manager.GitHubClient", side_effect=build):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


async def configure(hass, flow_id, data):
    return await hass.config_entries.options.async_configure(flow_id, data)


async def test_setup_needs_no_network_and_no_token(hass, repo):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={CONF_SIGNERS: [repo.pubkey()]})
    entry.add_to_hass(hass)
    with patch("custom_components.private_hacs.manager.GitHubClient", side_effect=AssertionError):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_options_flow_adds_installs_updates_and_rolls_back(hass, hass_storage, repo, client, entry):
    repo.release("1.0.0")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"add_repository", "manage_repository", "update_signers"}

    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    serializable(result)
    assert "github_token" in field_names(result)
    result = await configure(
        hass, result["flow_id"],
        {"full_name": "https://github.com/acme/demo", "github_token": TOKEN,
         "integration_name": "demo_integration", "install_now": True},
    )
    assert result["step_id"] == "installed_confirmation"
    assert result["description_placeholders"]["version"] == "1.0.0"
    assert Path(hass.config.path("custom_components", "demo_integration", "manifest.json")).is_file()
    result = await configure(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # update to 1.1.0: the token is typed again, it was not kept
    repo.release("1.1.0")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "manage_repository"})
    serializable(result)
    result = await configure(hass, result["flow_id"], {"full_name": "acme/demo"})
    assert result["step_id"] == "repository_action"
    serializable(result)
    result = await configure(
        hass, result["flow_id"],
        {"action": "install_or_update", "github_token": TOKEN, "tag": "", "allow_downgrade": False},
    )
    assert result["description_placeholders"]["version"] == "1.1.0"
    await configure(hass, result["flow_id"], {})

    # roll back: no token needed
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "manage_repository"})
    result = await configure(hass, result["flow_id"], {"full_name": "acme/demo"})
    result = await configure(hass, result["flow_id"], {"action": "rollback"})
    assert result["description_placeholders"]["version"] == "1.0.0"

    # a token was used for add+install (1 client) and for the update (1 client), never saved
    assert entry.seen_tokens == [TOKEN, TOKEN, TOKEN]
    assert TOKEN not in str(entry.as_dict())
    assert TOKEN not in str(hass_storage)


async def test_installing_without_a_token_is_refused_with_a_reason(hass, repo, client, entry):
    repo.release("1.0.0")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    result = await configure(
        hass, result["flow_id"], {"full_name": "acme/demo", "integration_name": "demo_integration", "install_now": True}
    )
    assert result["errors"] == {"base": "add_failed"}
    assert "token is needed" in result["description_placeholders"]["detail"]
    assert entry.seen_tokens == []


async def test_options_flow_shows_the_reason_when_install_is_refused(hass, repo, client, entry):
    repo.release("1.0.0", signer="evil")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    result = await configure(
        hass, result["flow_id"],
        {"full_name": "acme/demo", "github_token": TOKEN, "integration_name": "demo_integration", "install_now": True},
    )
    assert result["errors"] == {"base": "install_failed"}
    assert "not valid" in result["description_placeholders"]["detail"]
    assert TOKEN not in str(result)
    assert not Path(hass.config.path("custom_components", "demo_integration")).exists()


async def test_a_stale_error_is_not_shown_on_the_next_form(hass, repo, client, entry):
    repo.release("1.0.0", signer="evil")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    result = await configure(
        hass, result["flow_id"],
        {"full_name": "acme/demo", "github_token": TOKEN, "integration_name": "demo_integration", "install_now": True},
    )
    assert result["description_placeholders"]["detail"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    assert result["description_placeholders"]["detail"] == ""


async def test_options_flow_signing_keys_can_be_replaced(hass, repo, client, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "update_signers"})
    serializable(result)
    result = await configure(hass, result["flow_id"], {CONF_SIGNERS: "garbage"})
    assert result["errors"] == {CONF_SIGNERS: "invalid_signers"}
    with patch("custom_components.private_hacs.manager.GitHubClient", return_value=client):
        result = await configure(hass, result["flow_id"], {CONF_SIGNERS: repo.pubkey("evil")})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SIGNERS] == [repo.pubkey("evil")]


async def test_the_menu_is_not_offered_when_the_integration_is_not_running(hass, repo, entry):
    assert await hass.config_entries.async_unload(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_loaded"


async def test_the_integration_name_is_a_required_field(hass, repo, client, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    fields = {
        f["name"]: f
        for f in probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    }
    assert fields["integration_name"]["required"] is True
    assert fields["full_name"]["required"] is True


async def test_after_a_failed_install_the_same_form_can_be_resubmitted(hass, repo, client, entry):
    repo.release("1.0.0", signer="evil")  # nothing installable yet
    form = {"full_name": "acme/demo", "github_token": TOKEN, "integration_name": "demo_integration",
            "install_now": True}
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    result = await configure(hass, result["flow_id"], form)
    assert result["errors"] == {"base": "install_failed"}  # added, install refused

    repo.release("1.1.0")  # the maintainer fixes it
    result = await configure(hass, result["flow_id"], form)  # same dialog, same values
    assert result["step_id"] == "installed_confirmation"
    assert result["description_placeholders"]["version"] == "1.1.0"


async def test_a_different_folder_name_for_a_tracked_repo_is_refused(hass, repo, client, entry):
    repo.release("1.0.0")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    base = {"full_name": "acme/demo", "github_token": TOKEN, "install_now": False}
    result = await configure(hass, result["flow_id"], {**base, "integration_name": "demo_integration"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(hass, result["flow_id"], {"next_step_id": "add_repository"})
    result = await configure(hass, result["flow_id"], {**base, "integration_name": "something_else"})
    assert result["errors"] == {"base": "add_failed"}
    assert "already tracked" in result["description_placeholders"]["detail"]
