"""The SSH signature verifier, checked against real git/ssh-keygen output."""
from __future__ import annotations

import subprocess

import pytest

from custom_components.private_hacs.sigverify import (
    SignatureError,
    parse_public_key,
    parse_public_keys,
    verify_ssh_signature,
)


def _signed(repo, signer="good"):
    tag = repo.release("1.0.0", signer=signer)
    v = repo.tags[tag]["verification"]
    return v["payload"].encode(), v["signature"]


def test_parse_public_key_fingerprint_matches_ssh_keygen(repo):
    out = subprocess.run(
        ["ssh-keygen", "-lf", str(repo.keys / "good.pub")], capture_output=True, text=True, check=True
    ).stdout
    assert parse_public_key(repo.pubkey()).fingerprint == out.split()[1]


def test_parse_public_keys_ignores_blank_and_comments(repo):
    text = f"\n# my key\n{repo.pubkey()}\n\n{repo.pubkey('evil')}\n"
    assert len(parse_public_keys(text)) == 2


@pytest.mark.parametrize(
    "line",
    [
        "",
        "ssh-ed25519",
        "ssh-ed25519 !!!notbase64",
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA",  # truncated blob
    ],
)
def test_parse_public_key_rejects_garbage(line):
    with pytest.raises(SignatureError):
        parse_public_key(line)


def test_parse_public_keys_requires_at_least_one():
    with pytest.raises(SignatureError):
        parse_public_keys("# nothing here\n\n")


def test_rsa_key_from_ssh_keygen_is_refused(repo, tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(tmp_path / "rsa")], check=True)
    with pytest.raises(SignatureError):
        parse_public_key((tmp_path / "rsa.pub").read_text())


def test_valid_signature_returns_signer(repo):
    payload, sig = _signed(repo)
    signer = verify_ssh_signature(payload, sig, parse_public_keys(repo.pubkey()))
    assert signer.fingerprint == parse_public_key(repo.pubkey()).fingerprint


def test_any_trusted_key_may_sign(repo):
    payload, sig = _signed(repo, "evil")
    trusted = parse_public_keys(repo.pubkey("good") + "\n" + repo.pubkey("evil"))
    assert verify_ssh_signature(payload, sig, trusted).fingerprint == parse_public_key(repo.pubkey("evil")).fingerprint


def test_tampered_payload_is_rejected(repo):
    payload, sig = _signed(repo)
    with pytest.raises(SignatureError, match="does not match"):
        verify_ssh_signature(payload + b"x", sig, parse_public_keys(repo.pubkey()))


def test_untrusted_signer_is_rejected(repo):
    payload, sig = _signed(repo, "evil")
    with pytest.raises(SignatureError, match="not in the trusted list"):
        verify_ssh_signature(payload, sig, parse_public_keys(repo.pubkey("good")))


def test_other_namespace_is_rejected(repo, tmp_path):
    data = tmp_path / "data"
    data.write_bytes(b"hello")
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-q", "-f", str(repo.keys / "good"), "-n", "file", str(data)], check=True
    )
    sig = (tmp_path / "data.sig").read_text()
    with pytest.raises(SignatureError, match="namespace"):
        verify_ssh_signature(b"hello", sig, parse_public_keys(repo.pubkey()))
    # same signature is fine when the namespace is the one it was made in
    assert verify_ssh_signature(b"hello", sig, parse_public_keys(repo.pubkey()), namespace="file")


@pytest.mark.parametrize(
    "armored",
    [
        "",
        "not a signature",
        "-----BEGIN SSH SIGNATURE-----\n!!!\n-----END SSH SIGNATURE-----",
        "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----",  # magic only
        "-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----",
    ],
)
def test_malformed_signatures_are_rejected(repo, armored):
    with pytest.raises(SignatureError):
        verify_ssh_signature(b"x", armored, parse_public_keys(repo.pubkey()))


def test_truncated_signature_is_rejected(repo):
    payload, sig = _signed(repo)
    lines = sig.strip().splitlines()
    cut = "\n".join([lines[0], lines[1][:20], lines[-1]])
    with pytest.raises(SignatureError):
        verify_ssh_signature(payload, cut, parse_public_keys(repo.pubkey()))
