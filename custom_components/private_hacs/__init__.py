"""Private HACS (signed) — install custom integrations from private GitHub repos.

Only releases whose annotated `vX.Y.Z` tag is signed by one of the user's trusted SSH
keys are installed. The integration registers no services and **stores no GitHub token**:
the token is typed in the Configure dialog (administrators only) for each action that
talks to GitHub, and lives in memory for the duration of that action.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_SIGNERS, DOMAIN, SHARED_KEY
from .manager import PrivateHacsManager
from .sigverify import SignatureError, parse_public_keys
from .store import RepositoryStore

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Private HACS from a config entry (no network access at startup)."""
    try:
        trusted = parse_public_keys("\n".join(entry.data.get(CONF_SIGNERS, [])))
    except SignatureError as err:
        _LOGGER.error("Trusted signing keys are invalid, installs are disabled: %s", err)
        trusted = []

    # The lock and the store outlive reloads of the entry (e.g. after changing the keys), so an
    # operation still running on the previous manager can never overlap with the new one.
    shared = hass.data.setdefault(
        SHARED_KEY, {"lock": asyncio.Lock(), "store": RepositoryStore(hass)}
    )
    await shared["store"].async_load()

    manager = PrivateHacsManager(hass, shared["store"], trusted, lock=shared["lock"])
    await manager.async_recover()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"manager": manager}
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
