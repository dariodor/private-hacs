"""Config flow + options flow.

The config flow runs once and only collects the trusted signing keys: no token is stored.
The options flow ("Configure" button) is the day-to-day UI; every action that talks to GitHub
asks for a fine-grained read-only token, used for that action only.
"""
from __future__ import annotations

import logging
from typing import Any

try:  # Home Assistant 2026.10+ ships (and requires) probatio; voluptuous is no longer a given
    import probatio as vol
except ImportError:  # older Home Assistant
    import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ALLOW_DOWNGRADE,
    CONF_FULL_NAME,
    CONF_GITHUB_TOKEN,
    CONF_INTEGRATION_NAME,
    CONF_SIGNERS,
    CONF_TAG,
    DOMAIN,
)
from .manager import get_manager
from .sigverify import SignatureError, parse_public_keys
from .util import plain

_LOGGER = logging.getLogger(__name__)

TOKEN_PLACEHOLDERS = {"fine_url": "https://github.com/settings/personal-access-tokens/new"}

# `new-password` keeps browsers and password managers from filling or offering to save it
_TOKEN_FIELD = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="new-password")
)
_SIGNERS_FIELD = TextSelector(TextSelectorConfig(multiline=True))


def _signer_lines(text: str) -> list[str]:
    """Normalized key lines to store (one per key), raising SignatureError if invalid."""
    parse_public_keys(text)
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


class PrivateHacsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: collect the trusted signing keys."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            try:
                signers = _signer_lines(user_input[CONF_SIGNERS])
            except SignatureError:
                errors[CONF_SIGNERS] = "invalid_signers"
            else:
                return self.async_create_entry(
                    title="Private HACS", data={CONF_SIGNERS: signers}
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_SIGNERS): _SIGNERS_FIELD}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PrivateHacsOptionsFlow()


class PrivateHacsOptionsFlow(OptionsFlow):
    """Configure-button UI: add/remove/install/update/rollback repos and signing keys."""

    _error_detail: str = ""
    _selected_full_name: str = ""
    _installed_repo_name: str = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self.hass.data.get(DOMAIN):
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_repository", "manage_repository", "update_signers"],
        )

    # --- Add ----------------------------------------------------------------

    async def async_step_add_repository(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        self._error_detail = ""  # never show the text of an earlier error

        if user_input is not None:
            manager = get_manager(self.hass)
            token = user_input.get(CONF_GITHUB_TOKEN, "")
            try:
                full_name = manager.parse_repo_input(user_input[CONF_FULL_NAME])
                name = (user_input.get(CONF_INTEGRATION_NAME) or "").strip()
                existing = manager.store.get(full_name)
                if existing and existing.integration_name == name:
                    repo = existing  # added earlier, then the install failed: go on from there
                else:
                    repo = await manager.add_repository(full_name, name, token=token)
            except HomeAssistantError as err:
                _LOGGER.warning("add_repository failed: %s", err)
                errors["base"] = "add_failed"
                self._error_detail = plain(err)
            else:
                if user_input.get("install_now", True):
                    try:
                        await manager.install_or_update(repo.full_name, token=token)
                    except HomeAssistantError as err:
                        _LOGGER.warning("install failed for %s: %s", repo.full_name, err)
                        errors["base"] = "install_failed"
                        self._error_detail = plain(err)
                    else:
                        self._installed_repo_name = repo.full_name
                        return await self.async_step_installed_confirmation()
                if not errors:
                    return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Required(CONF_FULL_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Optional(CONF_GITHUB_TOKEN, default=""): _TOKEN_FIELD,
                vol.Required(CONF_INTEGRATION_NAME): str,
                vol.Optional("install_now", default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="add_repository",
            data_schema=schema,
            errors=errors,
            description_placeholders={**TOKEN_PLACEHOLDERS, "detail": self._error_detail},
        )

    # --- Manage existing ----------------------------------------------------

    async def async_step_manage_repository(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        repos = get_manager(self.hass).store.all()
        if not repos:
            return self.async_abort(reason="no_repositories")

        if user_input is not None:
            self._selected_full_name = user_input[CONF_FULL_NAME]
            return await self.async_step_repository_action()

        schema = vol.Schema(
            {
                vol.Required(CONF_FULL_NAME): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=r.full_name, label=_repo_label(r))
                            for r in repos
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="manage_repository", data_schema=schema)

    async def async_step_repository_action(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        self._error_detail = ""  # never show the text of an earlier error
        manager = get_manager(self.hass)
        full_name = self._selected_full_name

        if user_input is not None:
            action = user_input["action"]
            try:
                if action == "install_or_update":
                    await manager.install_or_update(
                        full_name,
                        (user_input.get(CONF_TAG) or "").strip() or None,
                        token=user_input.get(CONF_GITHUB_TOKEN, ""),
                        allow_downgrade=user_input.get(CONF_ALLOW_DOWNGRADE, False),
                    )
                elif action == "rollback":
                    await manager.rollback(full_name)
                elif action == "remove":
                    await manager.remove_repository(full_name)
            except HomeAssistantError as err:
                errors["base"] = "action_failed"
                self._error_detail = plain(err)
            else:
                if action in ("install_or_update", "rollback"):
                    self._installed_repo_name = full_name
                    return await self.async_step_installed_confirmation()
                return self.async_create_entry(title="", data={})

        repo = manager.store.get(full_name)
        schema = vol.Schema(
            {
                vol.Required("action", default="install_or_update"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value="install_or_update", label="Install / update (signed tags only)"
                            ),
                            SelectOptionDict(
                                value="rollback", label="Roll back to the previous version"
                            ),
                            SelectOptionDict(
                                value="remove", label="Remove (delete files + stop tracking)"
                            ),
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(CONF_GITHUB_TOKEN, default=""): _TOKEN_FIELD,
                vol.Optional(CONF_TAG, default=""): str,
                vol.Optional(CONF_ALLOW_DOWNGRADE, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="repository_action",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                **TOKEN_PLACEHOLDERS,
                "repo": plain(_repo_label(repo) if repo else full_name),
                "detail": self._error_detail,
            },
        )

    # --- Post-install confirmation -----------------------------------------

    async def async_step_installed_confirmation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        full_name = self._installed_repo_name
        repo = get_manager(self.hass).store.get(full_name) if full_name else None
        return self.async_show_form(
            step_id="installed_confirmation",
            data_schema=vol.Schema({}),
            description_placeholders={
                "repo": plain(full_name or "?"),
                "integration": plain((repo.integration_name if repo else None) or "?"),
                "version": plain((repo.installed_version if repo else None) or "?"),
            },
            last_step=True,
        )

    # --- Trusted signing keys ----------------------------------------------

    async def async_step_update_signers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                signers = _signer_lines(user_input[CONF_SIGNERS])
            except SignatureError:
                errors[CONF_SIGNERS] = "invalid_signers"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**self.config_entry.data, CONF_SIGNERS: signers}
                )
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title="", data={})

        current = "\n".join(self.config_entry.data.get(CONF_SIGNERS, []))
        return self.async_show_form(
            step_id="update_signers",
            data_schema=vol.Schema(
                {vol.Required(CONF_SIGNERS, default=current): _SIGNERS_FIELD}
            ),
            errors=errors,
        )


def _repo_label(repo) -> str:
    if not repo:
        return "?"
    label = repo.full_name
    if repo.installed_version:
        label += f" @ {repo.installed_version}"
    return label
