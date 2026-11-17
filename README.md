# Private HACS (signed)

A small Home Assistant custom integration that installs and updates **your own custom
integrations from private GitHub repositories**, and installs a release **only if its tag is
signed by a key you trust**.

Fork of [pestevez/private-hacs](https://github.com/pestevez/private-hacs) (MIT). The original
tracks branches/releases/commits and trusts whatever GitHub returns; this fork replaces that with
signed tags, strict archive handling, rollback, no services and no stored token. See
*Differences* below.

## What it checks

An install happens only if **all** of these hold:

1. The tag is an **annotated `vX.Y.Z` tag**, signed with an **SSH Ed25519 key** (what
   `git tag -s` produces with `gpg.format=ssh`). The signature is verified **locally**, in
   Python, against the public keys you typed into Home Assistant. GitHub's own "verified" flag
   is never used, and keys are never read from a repository.
2. The tag name, the commit and the object type are read from the **signed payload**, not from
   what the GitHub API says next to it. A valid signed tag served under another tag name is refused.
3. The archive carries, in its header, **exactly that signed commit id** (all 40 hex digits; GitHub's
   tarballs are made by `git archive`, which records it there). The *bytes* of the archive still
   come from GitHub over TLS (`api.github.com`, then `codeload.github.com`, nothing else is
   followed, and the token is never sent to the second).
4. Only `custom_components/<name>/` is extracted, where `<name>` is the folder you declared when
   adding the repository (the manifest `domain` must equal it, its `version` must equal the tag).
   The rest of the archive is ignored. Inside the folder, links, special files, hidden files,
   duplicate names (also differing only by case), names other than ASCII letters, digits and
   `_ . -`, files other than `.py .json .yaml .yml .txt .md .png` (lowercase) and anything over
   the limits (500 files, 5 MB each, 20 MB total, 32 MB download, 128 MB decompressed) are refused.
5. It is not a downgrade and not a changed tag: an older version, a **same version with a different
   commit** (a moved tag), or a version that cannot be compared is refused, and so is
   reinstalling exactly the same code, unless you tick *allow*. (That also keeps the rollback copy
   from being overwritten by accident.)
6. It will not take the name of a core Home Assistant integration, of Private HACS, or of another
   tracked repository, and it will **not overwrite a folder that Private HACS did not install**.

The new version is written to a temporary folder, flushed to disk and put in place with two
renames; the previous version is kept for **rollback**, which only restores it if the key that
signed it is still trusted. A crash between the renames is repaired the next time the entry is
set up, and the stored state is then aligned with what is on disk.

Someone who can write to your repository, or who steals the GitHub token, but does **not** have
your signing key **cannot get unsigned or differently signed code installed**.

## The GitHub token

**It is not stored anywhere.** (Upstream keeps it in `.storage/core.config_entries`, plain JSON;
Home Assistant offers integrations no encrypted secret store.) You type a fine-grained read-only
token in the dialog for each action that talks to GitHub; it lives in memory for that action, in
a client that is created for it and dropped afterwards. Nothing persistent (config entry,
`.storage`, backups) contains it, and it is not logged. A password manager makes pasting it easy.

While you type it, it passes through your browser and Home Assistant's websocket like any
password typed in the UI: a reverse proxy that logs request bodies, or debug logging of the
websocket, could capture it. Use a short-lived token.

## Setup

1. **Signing key** (once, on your machine; use a passphrase and `ssh-add` it, git needs 2.34+):
   `ssh-keygen -t ed25519 -f ~/.ssh/ha_release_signing -C ha-release`
2. **Sign each release** in each integration repository (the manifest `version` must equal the tag):
   ```
   git -c gpg.format=ssh -c user.signingkey=~/.ssh/ha_release_signing.pub tag -s v1.2.3 -m "my_integration 1.2.3"
   git push origin v1.2.3
   ```
3. **GitHub token**: fine-grained personal access token, *Only select repositories*, permission
   **Contents: Read-only**, with an expiry date.
4. **Install this integration** (see below), restart, then *Settings → Devices & services → Add
   integration → Private HACS* and paste your **public** signing key (`cat ~/.ssh/ha_release_signing.pub`).
5. *Configure → Add a private repository*: `owner/repo`, the token and the **integration folder
   name**, install, then **restart** Home Assistant (a reload is not enough: the swap replaces
   files that may already be imported).

Day to day: *Configure → Install / update / roll back / remove*. Without a tag it takes the
newest tag that verifies (up to 25 are tried; bogus newer tags are skipped and listed in the
notification); with a tag it installs exactly that one or fails. Roll back and remove need no token.

There are **no services** and no background polling: nothing runs unless an administrator
opens the dialog. (`manifest.json` says `iot_class: calculated` because the class list has no
"on demand" option.)

### Installing Private HACS itself

It cannot update itself (its own name is reserved), so it comes from outside:

- **Marketplace, "Add from a GitHub link"** (Settings → Marketplace): needs a **public** GitHub
  repository, so publish this fork publicly (it contains no secrets). That path verifies
  nothing: you trust GitHub and your account. Repository requirements, all present here:
  `hacs.json` and `README.md` in the root; `custom_components/private_hacs/` with a
  `manifest.json` (`domain`, `name`, `version`, `documentation`, `issue_tracker`, `codeowners`),
  `translations/en.json` and `brand/icon.png`. For a versioned install publish a **GitHub
  Release** whose tag equals the manifest version (`0.2.0`; whether a leading `v` is accepted is
  not documented, so use the bare version). Without a release it installs the latest commit of
  the default branch.
- **Manually**: copy `custom_components/private_hacs/` into your Home Assistant
  `custom_components/` and restart.

## Limits and what is persisted

- **Whoever holds your signing key and passphrase can ship code.** Keep it like a production key.
- **Removing a key** stops accepting new signatures from the next action on (an action already
  running finishes with the old keys) and blocks rollback to code it signed. It does not
  uninstall or re-verify what is already installed.
- **Rollback** keeps one previous version, restores it without re-downloading, and refuses if
  its recorded signature is missing or from a key no longer trusted.
- **Python `requirements`** declared by an installed integration are installed by Home Assistant
  at startup, outside this signature check. The notification after an install lists them.
- A signature proves who released the code, not that it is correct: read the diff before signing.
- The extension whitelist rejects common files: `.js .html .css .svg .jpg .mo .so`, files with no
  extension (e.g. `LICENSE`) and hidden files inside the integration folder. A release that
  ships one cannot be installed.
- Installed integrations run inside Home Assistant without a sandbox, like any custom integration.

What is stored: the trusted **public** keys in the config entry; in
`.storage/private_hacs.signed_repositories`, per repository: name, integration folder, installed
version, commit and key fingerprint; in each installed folder `.private_hacs.json` (commit,
fingerprint, version); and one previous version per integration in
`custom_components/.private_hacs_backups/`.

## Differences from upstream

| | upstream | this fork |
|---|---|---|
| What is installed | branch / tag / release / commit, unverified | only annotated `vX.Y.Z` tags with a valid trusted signature |
| Services (`add_repository`, `install`, …) | yes | removed (any automation could have installed code) |
| GitHub token | kept in `.storage` (plain JSON); classic `repo` or fine-grained | **never stored**, typed per action; fine-grained read-only |
| Tarball | links skipped, no limits, no file filter | strict validation, limits, extension whitelist, commit bound by the full id, decompression cap |
| Install | delete, then extract | stage, validate, flush, two renames, backup, rollback, crash recovery |
| Redirects | followed with the token | followed by hand, never with the token, only to `codeload.github.com` |
| Downgrades / moved tags | silent | refused unless explicit |
| Existing folder of the same name | overwritten | refused |

(The upstream column describes the original code as read for this fork.)

## Development

```
python -m pytest        # needs pytest-homeassistant-custom-component, git and ssh-keygen
ruff check .
```

255 tests. They create real git repositories with real SSH-signed tags, so the verifier is
checked against what `git` and `ssh-keygen` produce; the tarballs come from `git archive` or
carry a GitHub-style header. The GitHub client is tested against scripted responses only. The unit
tests run on Home Assistant 2026.10.4 (the newest the test plugin supports).

## Verified

Verified: the unit tests above; the whole flow in a real Home Assistant **2026.10.4 and
2026.11.2** (config and options flows driven over REST, token absent from every file) against a
local fake GitHub that serves real signed tags; the GitHub client against GitHub's public API
(tag objects, `verification.payload`/`signature`, redirect to `codeload.github.com`, and that
the tarball header carries the full commit id); the dependency audit (see below).

Dependencies: the code imports `aiohttp`, `yarl`, `cryptography` and `probatio`
(`voluptuous` as a fallback), all provided by Home Assistant (`manifest.json` has no
`requirements`). `pip-audit` on the 221 packages of the test environment (HA 2026.10.4) reports
nothing for those; its only findings are Pillow 12.3.0 (pinned by that Home Assistant version,
fixed in 13.0.0, which Home Assistant 2026.11.2 ships) and the disputed jsonpickle 1.4.1
(test environment only). This integration uses neither.

## License

MIT, see [LICENSE](LICENSE). Original work © 2026 Pablo Estevez; changes © 2026 Dario Dorfelli.
