"""Validation result types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Check:
    name: str
    target: str            # "report" or a canonical id
    status: str            # pass | warn | fail
    detail: str = ""


@dataclass
class GateResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def passed(self) -> bool:
        return not self.failures

    def failing_papers(self) -> set[str]:
        return {c.target for c in self.failures if c.target != "report"}

    def report_level_failures(self) -> list[Check]:
        return [c for c in self.failures if c.target == "report"]

    def summary(self) -> dict:
        return {"passed": self.passed, "checks": len(self.checks), "failures": len(self.failures),
                "warnings": len(self.warnings)}
