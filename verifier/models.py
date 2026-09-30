from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VerificationStatus(str, Enum):
    VERIFIED = "verified"        # independently confirmed the claimed outcome actually happened
    FAILED = "failed"            # independently checked and it did NOT happen — overrides the tool's own success claim
    INCONCLUSIVE = "inconclusive"  # no reliable independent check exists for this — never collapsed into a false VERIFIED or an unfair FAILED


@dataclass
class VerificationResult:
    status: VerificationStatus
    reason: str

    @property
    def failed(self) -> bool:
        return self.status == VerificationStatus.FAILED
