from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Detection:
    rule: str
    severity: str  # CRITICAL | HIGH | MEDIUM | LOW
    confidence: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
