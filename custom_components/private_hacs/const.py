"""Constants for the Private HACS integration."""
from __future__ import annotations

import re

DOMAIN = "private_hacs"

# the name of a form field, not a secret
CONF_GITHUB_TOKEN = "github_token"  # noqa: S105
CONF_SIGNERS = "signers"

# Per-repo / per-action keys
CONF_FULL_NAME = "full_name"
CONF_INTEGRATION_NAME = "integration_name"
CONF_TAG = "tag"
CONF_ALLOW_DOWNGRADE = "allow_downgrade"

STORAGE_KEY = f"{DOMAIN}.signed_repositories"
STORAGE_VERSION = 1
SHARED_KEY = f"{DOMAIN}_shared"  # lock + store shared by every setup of the entry (reloads)
PROVENANCE_FILE = ".private_hacs.json"  # commit/fingerprint of what is installed, inside the folder
BACKUP_DIR = ".private_hacs_backups"  # inside custom_components/: same filesystem, atomic renames

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "private-hacs-signed"
REQUEST_TIMEOUT = 30

# Input formats. Everything that ends up in a URL or a path is checked against these.
# Always use `.fullmatch()`: ASCII only, and `$` is not used (it would accept a trailing newline).
FULL_NAME_RE = re.compile(  # the repo part may not be "." or ".." (URL normalisation would eat it)
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,100}", re.ASCII
)
_NUM = r"(0|[1-9][0-9]{0,5})"  # no leading zeros: v01.2.3 and v1.2.3 must not both exist
VERSION_RE = re.compile(rf"{_NUM}\.{_NUM}\.{_NUM}", re.ASCII)
TAG_RE = re.compile(rf"v{_NUM}\.{_NUM}\.{_NUM}", re.ASCII)
SHA_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)
INTEGRATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)  # a Home Assistant domain
PATH_PART_RE = re.compile(r"[A-Za-z0-9_.\-]{1,255}", re.ASCII)  # every file/folder name inside an integration
TOKEN_RE = re.compile(r"[A-Za-z0-9_]{20,255}", re.ASCII)  # GitHub token shapes: ghp_…, github_pat_…

# Signature scheme: SSH signatures of annotated git tags (`git tag -s` with gpg.format=ssh).
SIGNATURE_NAMESPACE = "git"

# Limits on untrusted input (GitHub answer, tarball content).
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024  # compressed tarball
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024  # sum of declared sizes of every member in the archive
MAX_MEMBERS = 20_000  # members in the whole archive
MAX_FILES = 500  # files of the integration
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_INTEGRATION_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({".py", ".json", ".yaml", ".yml", ".txt", ".md", ".png"})
MAX_TAG_PAGES = 5
MAX_TAGS_TRIED = 25  # newest tags examined when looking for the latest verifiable release
DOWNLOAD_HOST = "codeload.github.com"  # where GitHub's tarball redirect must point
