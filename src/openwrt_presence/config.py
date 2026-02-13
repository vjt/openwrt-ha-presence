from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Config:
    """Placeholder – will be implemented in the config task."""

    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        return cls(raw=data)
