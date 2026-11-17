"""Local verification of SSH signatures (SSHSIG, the format of `git tag -s`).

Only Ed25519 keys are supported. The trusted public keys come from the user's
configuration in Home Assistant, never from the repository being installed.
Format reference: OpenSSH PROTOCOL.sshsig.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_MAGIC = b"SSHSIG"
_KEY_TYPE = b"ssh-ed25519"
_ARMOR_RE = re.compile(
    r"-----BEGIN SSH SIGNATURE-----(?P<body>[A-Za-z0-9+/=\s]{1,4096})-----END SSH SIGNATURE-----"
)


class SignatureError(Exception):
    """Invalid key, malformed signature, or signature that does not verify."""


@dataclass(frozen=True)
class TrustedKey:
    """An Ed25519 public key allowed to sign releases."""

    raw: bytes
    fingerprint: str  # "SHA256:..." as printed by `ssh-keygen -lf`


def _read_string(buf: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 4 > len(buf):
        raise SignatureError("malformed data (truncated length)")
    (length,) = struct.unpack(">I", buf[pos : pos + 4])
    pos += 4
    if length > len(buf) - pos:
        raise SignatureError("malformed data (truncated string)")
    return buf[pos : pos + length], pos + length


def _pack_string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _parse_key_blob(blob: bytes) -> bytes:
    """Return the 32 raw bytes of an ssh-ed25519 public key blob."""
    fmt, pos = _read_string(blob, 0)
    raw, pos = _read_string(blob, pos)
    if fmt != _KEY_TYPE or len(raw) != 32 or pos != len(blob):
        raise SignatureError("only ssh-ed25519 public keys are supported")
    return raw


def parse_public_key(line: str) -> TrustedKey:
    """Parse one OpenSSH public key line: `ssh-ed25519 AAAA... [comment]`."""
    parts = line.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise SignatureError("expected a line like 'ssh-ed25519 AAAA... comment'")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as err:
        raise SignatureError("public key is not valid base64") from err
    raw = _parse_key_blob(blob)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return TrustedKey(raw=raw, fingerprint=f"SHA256:{digest}")


def parse_public_keys(text: str) -> list[TrustedKey]:
    """Parse one key per line; blank lines and `#` comments are ignored."""
    keys = [
        parse_public_key(line)
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not keys:
        raise SignatureError("no public key found")
    return keys


def verify_ssh_signature(
    payload: bytes, armored: str, trusted: list[TrustedKey], namespace: str = "git"
) -> TrustedKey:
    """Verify `armored` (an SSH signature) over `payload`; return the key that signed.

    Raises SignatureError unless the signature is valid, made in `namespace`, and
    made by one of the `trusted` keys.
    """
    match = _ARMOR_RE.search(armored or "")
    if not match:
        raise SignatureError("not an SSH signature")
    try:
        blob = base64.b64decode(re.sub(r"\s+", "", match.group("body")), validate=True)
    except (binascii.Error, ValueError) as err:
        raise SignatureError("signature is not valid base64") from err

    if not blob.startswith(_MAGIC):
        raise SignatureError("bad signature magic")
    pos = len(_MAGIC)
    if blob[pos : pos + 4] != struct.pack(">I", 1):
        raise SignatureError("unsupported signature version")
    pos += 4
    pubkey_blob, pos = _read_string(blob, pos)
    sig_namespace, pos = _read_string(blob, pos)
    reserved, pos = _read_string(blob, pos)
    hash_alg, pos = _read_string(blob, pos)
    sig_blob, pos = _read_string(blob, pos)
    if pos != len(blob):
        raise SignatureError("trailing data after signature")

    if sig_namespace != namespace.encode():
        raise SignatureError("signature made in a different namespace")
    if hash_alg == b"sha512":
        digest = hashlib.sha512(payload).digest()
    elif hash_alg == b"sha256":
        digest = hashlib.sha256(payload).digest()
    else:
        raise SignatureError("unsupported hash algorithm")

    raw_key = _parse_key_blob(pubkey_blob)
    sig_fmt, spos = _read_string(sig_blob, 0)
    raw_sig, spos = _read_string(sig_blob, spos)
    if sig_fmt != _KEY_TYPE or len(raw_sig) != 64 or spos != len(sig_blob):
        raise SignatureError("unsupported signature type")

    signer = next((k for k in trusted if hmac.compare_digest(k.raw, raw_key)), None)
    if signer is None:
        raise SignatureError("signed by a key that is not in the trusted list")

    signed_data = (
        _MAGIC
        + _pack_string(sig_namespace)
        + _pack_string(reserved)
        + _pack_string(hash_alg)
        + _pack_string(digest)
    )
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(raw_sig, signed_data)
    except InvalidSignature as err:
        raise SignatureError("signature does not match the content") from err
    return signer
