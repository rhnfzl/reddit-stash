"""Explicit result contract used by archive save operations."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SaveStatus(str, Enum):
    SAVED = "saved"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class SaveResult:
    status: SaveStatus
    media_size: int = 0
    error: Optional[str] = None
