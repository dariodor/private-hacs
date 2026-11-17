"""Repository data model."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Repository:
    """A tracked GitHub repository providing a custom integration."""

    full_name: str
    integration_name: str | None = None
    installed_version: str | None = None  # "1.2.3"
    installed_commit: str | None = None  # full sha of the signed tag's commit
    installed_fingerprint: str | None = None  # key that signed the installed release

    @property
    def key(self) -> str:
        return self.full_name.lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Repository:
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in allowed})
