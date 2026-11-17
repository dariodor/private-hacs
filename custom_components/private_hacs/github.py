"""GitHub API client for Private HACS (read-only, GET requests only)."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from yarl import URL

from .const import (
    DOWNLOAD_HOST,
    FULL_NAME_RE,
    GITHUB_API_BASE,
    MAX_DOWNLOAD_BYTES,
    MAX_TAG_PAGES,
    REQUEST_TIMEOUT,
    SHA_RE,
    TAG_RE,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

_REDIRECTS = (301, 302, 303, 307, 308)


class GitHubError(Exception):
    """Raised on any GitHub API failure."""


class GitHubAuthError(GitHubError):
    """Raised on 401/403 from GitHub."""


class GitHubNotFoundError(GitHubError):
    """Raised on 404 from GitHub."""


def _check_full_name(full_name: str) -> None:
    if not FULL_NAME_RE.fullmatch(full_name):
        raise GitHubError("Invalid repository name")


class GitHubClient:
    """Thin authenticated wrapper around the GitHub REST API."""

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    def _headers(self, *, authenticated: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _raise_for_status(status: int, what: str) -> None:
        if status in (401, 403):
            raise GitHubAuthError(
                f"GitHub refused the request ({status}): token rejected, permission missing or "
                f"rate limit, for {what}"
            )
        if status == 404:
            raise GitHubNotFoundError(f"Not found: {what}")
        if status >= 400:
            raise GitHubError(f"GitHub error {status} for {what}")

    async def _get_json(self, path: str) -> Any:
        url = f"{GITHUB_API_BASE}{path}"
        try:
            async with self._session.get(
                url,
                headers=self._headers(),
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status in _REDIRECTS:
                    raise GitHubError(
                        "GitHub redirected the request (repository renamed or moved?): "
                        "use its current owner/name"
                    )
                self._raise_for_status(resp.status, path)
                if resp.status != 200:
                    raise GitHubError(f"Unexpected status {resp.status} for {path}")
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise GitHubError(f"Request failed for {path}: {err}") from err

    async def get_repo(self, full_name: str) -> dict[str, Any]:
        _check_full_name(full_name)
        data = await self._get_json(f"/repos/{full_name}")
        if not isinstance(data, dict):
            raise GitHubError("Unexpected repository payload")
        return data

    async def list_tag_names(self, full_name: str) -> list[str]:
        """Names of the `vX.Y.Z` tags (other tags are ignored)."""
        _check_full_name(full_name)
        names: list[str] = []
        for page in range(1, MAX_TAG_PAGES + 1):
            data = await self._get_json(f"/repos/{full_name}/tags?per_page=100&page={page}")
            if not isinstance(data, list):
                raise GitHubError("Unexpected tag list payload")
            names += [
                t["name"]
                for t in data
                if isinstance(t, dict) and isinstance(t.get("name"), str)
                and TAG_RE.fullmatch(t["name"])
            ]
            if len(data) < 100:
                return names
        raise GitHubError(
            f"{full_name} has more than {MAX_TAG_PAGES * 100} tags: name the tag to install"
        )

    async def get_tag_ref(self, full_name: str, tag: str) -> dict[str, Any] | None:
        """The git ref of a tag, or None if it does not exist."""
        _check_full_name(full_name)
        if not TAG_RE.fullmatch(tag):
            raise GitHubError("Invalid tag name")
        try:
            data = await self._get_json(f"/repos/{full_name}/git/ref/tags/{tag}")
        except GitHubNotFoundError:
            return None
        if not isinstance(data, dict):
            raise GitHubError("Unexpected tag ref payload")
        return data

    async def get_tag_object(self, full_name: str, sha: str) -> dict[str, Any]:
        """The annotated tag object, including GitHub's `verification` block."""
        _check_full_name(full_name)
        if not SHA_RE.fullmatch(sha):
            raise GitHubError("Invalid object id")
        data = await self._get_json(f"/repos/{full_name}/git/tags/{sha}")
        if not isinstance(data, dict):
            raise GitHubError("Unexpected tag object payload")
        return data

    async def download_tarball(self, full_name: str, commit: str) -> bytes:
        """Download the gzipped tarball of a commit, capped at MAX_DOWNLOAD_BYTES.

        The API redirects to a signed codeload URL. The redirect is followed by hand so
        that the token is never sent to the redirect target.
        """
        _check_full_name(full_name)
        if not SHA_RE.fullmatch(commit):
            raise GitHubError("Invalid commit id")
        what = f"{full_name} tarball"
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT * 4)
        try:
            async with self._session.get(
                f"{GITHUB_API_BASE}/repos/{full_name}/tarball/{commit}",
                headers=self._headers(),
                allow_redirects=False,
                timeout=timeout,
            ) as resp:
                if resp.status in _REDIRECTS:
                    try:
                        target = URL(resp.headers.get("Location", ""))
                    except ValueError as err:
                        raise GitHubError("Refusing redirect to an invalid address") from err
                    if (
                        target.scheme != "https"
                        or target.host != DOWNLOAD_HOST
                        or target.user
                        or target.explicit_port not in (None, 443)
                    ):
                        raise GitHubError(f"Refusing redirect to {target.host!r}")
                    location = str(target)
                else:
                    self._raise_for_status(resp.status, what)
                    if resp.status != 200:
                        raise GitHubError(f"Unexpected status {resp.status} for {what}")
                    return await self._read_capped(resp)
            async with self._session.get(
                location,
                headers=self._headers(authenticated=False),
                allow_redirects=False,
                timeout=timeout,
            ) as resp:
                self._raise_for_status(resp.status, what)
                if resp.status != 200:
                    raise GitHubError(f"Unexpected status {resp.status} for {what}")
                return await self._read_capped(resp)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GitHubError(f"Download failed for {what}: {err}") from err

    @staticmethod
    async def _read_capped(resp: aiohttp.ClientResponse) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.content.iter_chunked(65536):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise GitHubError(f"Download exceeds {MAX_DOWNLOAD_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
