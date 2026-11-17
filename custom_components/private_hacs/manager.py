"""Repository management, used by the options flow (there are no services on purpose)."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BACKUP_DIR,
    DOMAIN,
    FULL_NAME_RE,
    INTEGRATION_RE,
    MAX_TAGS_TRIED,
    TAG_RE,
    TOKEN_RE,
    VERSION_RE,
)
from .github import GitHubClient, GitHubError, GitHubNotFoundError
from .installer import (
    InstallerError,
    install_from_tarball,
    installed_version,
    read_provenance,
    recover,
    remove_integration,
    rollback_integration,
)
from .release import ReleaseError, VerifiedRelease, newest_tags, verify_tag, version_tuple
from .repository import Repository
from .sigverify import TrustedKey
from .store import RepositoryStore
from .util import plain

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

# "owner/repo", "github.com/owner/repo", "https://github.com/owner/repo[.git]",
# "git@github.com:owner/repo[.git]". Trailing slashes / .git tolerated.
_REPO_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:github\.com[:/])?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"/"
    r"(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?",
    re.ASCII,
)


class PrivateHacsManager:
    """Operations on the repository registry + custom_components/ directory."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: RepositoryStore,
        trusted: list[TrustedKey],
        client_factory: Callable[[str], GitHubClient] | None = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self.hass = hass
        self.store = store
        self.trusted = trusted
        self._client_factory = client_factory or (
            lambda token: GitHubClient(async_get_clientsession(hass), token)
        )
        self._lock = lock or asyncio.Lock()  # one add/install/rollback/remove at a time

    def _client(self, token: str | None) -> GitHubClient:
        """A client for ONE action. The token is never stored, logged or kept after the action."""
        token = (token or "").strip()
        if not token:
            raise HomeAssistantError(
                "A GitHub token is needed for this action (it is not stored anywhere)"
            )
        if not TOKEN_RE.fullmatch(token):
            raise HomeAssistantError("That does not look like a GitHub token")
        return self._client_factory(token)

    async def _exclusive(self, work: Callable[[], Awaitable[_T]]) -> _T:
        """Run `work` under the lock, as a task of its own.

        The file operations run in an executor thread and cannot be interrupted; if the caller is
        cancelled (browser tab closed, reload, shutdown) the task still runs to its end, so disk
        and store are never left apart by a cancelled dialog.
        """

        async def run() -> _T:
            async with self._lock:
                return await work()

        task = self.hass.async_create_task(run())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # never "unretrieved"
        return await asyncio.shield(task)

    @property
    def _custom_components_dir(self) -> Path:
        return Path(self.hass.config.path("custom_components"))

    @property
    def _backup_dir(self) -> Path:
        # inside custom_components/: same filesystem as the integrations, so renames are atomic
        return self._custom_components_dir / BACKUP_DIR

    async def async_recover(self) -> None:
        """Settle what a crash may have left half done, and make the store match the disk."""
        async with self._lock:
            notes = await self.hass.async_add_executor_job(
                recover, self._custom_components_dir, self._backup_dir
            )
            for note in notes:
                _LOGGER.warning("Recovery after an interrupted operation: %s", note)
            changed = False
            for repo in self.store.all():
                if not repo.integration_name:
                    continue
                on_disk, provenance = await self.hass.async_add_executor_job(
                    self._disk_state, repo.integration_name
                )
                version = provenance["version"] if provenance else on_disk
                commit = provenance["commit"] if provenance else None
                fingerprint = provenance["fingerprint"] if provenance else None
                if on_disk is None:
                    version = None
                if (version, commit, fingerprint) != (
                    repo.installed_version, repo.installed_commit, repo.installed_fingerprint
                ):
                    _LOGGER.warning(
                        "%s: the stored state did not match the files on disk, using the disk",
                        repo.full_name,
                    )
                    repo.installed_version = version
                    repo.installed_commit = commit
                    repo.installed_fingerprint = fingerprint
                    self.store.upsert(repo)
                    changed = True
            if changed:
                await self.store.async_save()

    def _disk_state(self, name: str) -> tuple[str | None, dict[str, str] | None]:
        folder = self._custom_components_dir / name
        return installed_version(self._custom_components_dir, name), read_provenance(folder)

    @staticmethod
    def parse_repo_input(value: str) -> str:
        """Normalize user-supplied repo references to 'owner/repo'."""
        match = _REPO_URL_RE.fullmatch(value.strip())
        if not match:
            raise HomeAssistantError("Could not parse the repository reference")
        full_name = f"{match.group('owner')}/{match.group('repo')}"
        if not FULL_NAME_RE.fullmatch(full_name):
            raise HomeAssistantError("Invalid repository name")
        return full_name

    def _tracked(self, full_name_or_url: str) -> Repository:
        repo = self.store.get(self.parse_repo_input(full_name_or_url))
        if not repo:
            raise HomeAssistantError("That repository is not tracked")
        return repo

    def _is_reserved(self, owner: str | None) -> Callable[[str], bool]:
        """Names an integration of repo `owner` must not take: core, this one, other repos'."""
        # snapshot now, on the event loop: the check runs later in an executor thread
        taken = {
            r.integration_name
            for r in self.store.all()
            if r.integration_name and r.full_name.lower() != (owner or "").lower()
        }

        def check(name: str) -> bool:
            return name == DOMAIN or name in taken or _is_core_integration(name)

        return check

    @staticmethod
    async def _check_access(client: GitHubClient, full_name: str) -> None:
        try:
            await client.get_repo(full_name)
        except GitHubNotFoundError as err:
            raise HomeAssistantError(
                f"Cannot access {full_name} on GitHub: repository not found or the token has no "
                "access to it. With a fine-grained token, add this repository to its "
                "'Repository access' list."
            ) from err
        except GitHubError as err:
            raise HomeAssistantError(f"Cannot access {full_name} on GitHub: {err}") from err

    async def add_repository(
        self, full_name_or_url: str, integration_name: str, *, token: str
    ) -> Repository:
        """Track a repository. The integration folder name is required: it pins what the
        repository may install (the manifest `domain` must match it)."""
        full_name = self.parse_repo_input(full_name_or_url)
        if not INTEGRATION_RE.fullmatch(integration_name or ""):
            raise HomeAssistantError(
                "Invalid integration name (lowercase letters, digits and _, starting with a letter)"
            )
        client = self._client(token)

        async def work() -> Repository:
            if self.store.get(full_name):
                raise HomeAssistantError(f"{full_name} is already tracked")
            reserved = await self.hass.async_add_executor_job(
                self._is_reserved(full_name), integration_name
            )
            if reserved:
                raise HomeAssistantError(f"'{integration_name}' is reserved or already in use")
            await self._check_access(client, full_name)
            repo = Repository(full_name=full_name, integration_name=integration_name)
            self.store.upsert(repo)
            await self.store.async_save()
            return repo

        return await self._exclusive(work)

    async def remove_repository(self, full_name_or_url: str, *, delete_files: bool = True) -> None:
        async def work() -> None:
            repo = self._tracked(full_name_or_url)
            if delete_files and repo.integration_name:
                try:
                    await self.hass.async_add_executor_job(
                        remove_integration,
                        self._custom_components_dir,
                        self._backup_dir,
                        repo.integration_name,
                    )
                except InstallerError as err:
                    raise HomeAssistantError(f"{repo.full_name}: {err}") from err
            self.store.remove(repo.full_name)
            await self.store.async_save()

        await self._exclusive(work)

    async def install_or_update(
        self,
        full_name_or_url: str,
        tag: str | None = None,
        *,
        token: str,
        allow_downgrade: bool = False,
    ) -> Repository:
        """Install the newest verifiable (or the given) vX.Y.Z tag, signed by a trusted key."""
        if tag and not TAG_RE.fullmatch(tag):
            raise HomeAssistantError("The tag must look like v1.2.3")
        client = self._client(token)
        return await self._exclusive(
            lambda: self._install_or_update(client, full_name_or_url, tag, allow_downgrade)
        )

    async def _pick_release(
        self, client: GitHubClient, repo: Repository, tag: str | None
    ) -> tuple[VerifiedRelease, list[str]]:
        """The release to install and the (tag: reason) of newer tags that were skipped.

        An explicit tag must verify. Without one, the newest tags are tried in turn: a bogus
        newer tag (unsigned, wrong key) must not freeze updates, and skipping it is safe
        because every candidate must be signed by a trusted key and is checked against
        the downgrade guard afterwards.
        """
        if tag:
            return await verify_tag(client, repo.full_name, tag, self.trusted), []
        candidates = newest_tags(await client.list_tag_names(repo.full_name), MAX_TAGS_TRIED)
        if not candidates:
            raise HomeAssistantError(f"{repo.full_name} has no vX.Y.Z tags")
        skipped: list[str] = []
        for candidate in candidates:
            try:
                release = await verify_tag(client, repo.full_name, candidate, self.trusted)
            except ReleaseError as err:
                skipped.append(f"{candidate}: {err}")
            else:
                return release, skipped
        raise HomeAssistantError("; ".join(skipped))

    async def _install_or_update(
        self, client: GitHubClient, full_name_or_url: str, tag: str | None, allow: bool
    ) -> Repository:
        repo = self._tracked(full_name_or_url)
        name = repo.integration_name
        if not name:
            raise HomeAssistantError(f"{repo.full_name} has no integration name: remove and re-add it")
        if not self.trusted:
            raise HomeAssistantError(
                "No trusted signing key configured: add one in Configure → Trusted signing keys"
            )
        try:
            await self._check_access(client, repo.full_name)
            release, skipped = await self._pick_release(client, repo, tag)

            current, provenance = await self.hass.async_add_executor_job(self._disk_state, name)
            if current is not None and repo.installed_version is None and provenance is None:
                raise HomeAssistantError(
                    f"custom_components/{name} already exists and was not installed by Private "
                    "HACS: move or delete it first, it will not be overwritten"
                )
            problem = _downgrade_problem(
                release, current, provenance["commit"] if provenance else repo.installed_commit, allow
            )
            if problem:
                raise HomeAssistantError(problem)

            tarball = await client.download_tarball(repo.full_name, release.commit)
            result = await self.hass.async_add_executor_job(
                lambda: install_from_tarball(
                    tarball,
                    self._custom_components_dir,
                    self._backup_dir,
                    name,
                    expected_version=release.version,
                    expected_commit=release.commit,
                    fingerprint=release.fingerprint,
                    is_reserved=self._is_reserved(repo.full_name),
                )
            )
        except (ReleaseError, GitHubError, InstallerError) as err:
            raise HomeAssistantError(f"{repo.full_name} {tag or ''}: {err}") from err

        repo.installed_version = release.version
        repo.installed_commit = release.commit
        repo.installed_fingerprint = release.fingerprint
        self.store.upsert(repo)
        await self.store.async_save()

        _LOGGER.info(
            "Installed %s %s (commit %s, signed by %s) into custom_components/%s",
            repo.full_name,
            release.version,
            release.commit[:7],
            release.fingerprint,
            result.integration,
        )
        extra = ""
        if skipped:
            _LOGGER.warning("Newer tags of %s were skipped: %s", repo.full_name, "; ".join(skipped))
            extra += (
                "\n\n**Newer tags were skipped because they could not be verified:** "
                + plain("; ".join(skipped), 600)
            )
        if result.requirements:
            extra += (
                "\n\nThis release declares Python requirements that Home Assistant will "
                "install at startup: " + ", ".join(plain(r, 80) for r in result.requirements)
            )
        persistent_notification.async_create(
            self.hass,
            (
                f"**{repo.full_name}** {release.version} (signature verified, key "
                f"`{release.fingerprint}`) is installed as "
                f"`custom_components/{result.integration}/`.\n\n"
                "**Restart Home Assistant** to load it (do not just reload the integration)." + extra
            ),
            title=f"Private HACS: {result.integration} ready",
            notification_id=f"{DOMAIN}_pending_restart_{result.integration}",
        )
        return repo

    async def rollback(self, full_name_or_url: str) -> Repository:
        """Restore the version that was installed before the last install."""
        return await self._exclusive(lambda: self._rollback(full_name_or_url))

    async def _rollback(self, full_name_or_url: str) -> Repository:
        repo = self._tracked(full_name_or_url)
        name = repo.integration_name
        if not name:
            raise HomeAssistantError(f"{repo.full_name} is not installed")
        previous = await self.hass.async_add_executor_job(read_provenance, self._backup_dir / name)
        if previous is None:
            raise HomeAssistantError(
                f"{repo.full_name}: the previous version has no recorded signature, "
                "so it will not be restored"
            )
        if previous["fingerprint"] not in {k.fingerprint for k in self.trusted}:
            raise HomeAssistantError(
                f"{repo.full_name}: the previous version was signed by a key that is no "
                "longer trusted, so it will not be restored"
            )
        try:
            version, restored = await self.hass.async_add_executor_job(
                rollback_integration, self._custom_components_dir, self._backup_dir, name
            )
        except InstallerError as err:
            raise HomeAssistantError(f"{repo.full_name}: {err}") from err
        repo.installed_version = restored["version"] if restored else version
        repo.installed_commit = restored["commit"] if restored else None
        repo.installed_fingerprint = restored["fingerprint"] if restored else None
        self.store.upsert(repo)
        await self.store.async_save()
        return repo


def _is_core_integration(name: str) -> bool:
    """True if Home Assistant ships an integration with this name (never shadow it)."""
    try:
        return importlib.util.find_spec(f"homeassistant.components.{name}") is not None
    except (ImportError, ValueError):
        return True  # cannot tell: refuse


def _downgrade_problem(
    release: VerifiedRelease, current: str | None, current_commit: str | None, allow: bool
) -> str | None:
    """Why installing `release` over what is installed is refused (None if it is fine).

    Fails closed: if the installed version or commit cannot be compared the user must say so
    (`allow`). The previous version in the backup is only replaced by a strict upgrade, so a
    reinstall of the same code cannot destroy the rollback copy by accident.
    """
    if allow or current is None:
        return None
    if not VERSION_RE.fullmatch(current):
        return "The installed version cannot be compared: tick 'allow' to install anyway"
    new, installed = version_tuple(release.version), version_tuple(current)
    if new < installed:
        return (
            f"Refusing to go from {current} down to {release.version} "
            "(use the rollback action, or tick 'allow')"
        )
    if new == installed:
        if current_commit == release.commit:
            return (
                f"{release.version} is already installed (tick 'allow' to reinstall it; "
                "that replaces the rollback copy)"
            )
        if current_commit is None:
            return (
                f"{release.version} is installed but its commit is unknown: tick 'allow' to "
                "reinstall it"
            )
        return (
            f"{release.tag} now points at a different commit than the one installed "
            f"({release.commit[:7]} instead of {current_commit[:7]}): was the tag moved? "
            "Tick 'allow' to install it anyway"
        )
    return None


def get_manager(hass: HomeAssistant) -> PrivateHacsManager:
    """Return the manager for the (single) configured Private HACS entry."""
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data:
        raise HomeAssistantError("Private HACS is not configured")
    return next(iter(domain_data.values()))["manager"]
