from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, Field


class Severity(IntEnum):
    info = 0
    warning = 1
    lossy = 2
    manual = 3
    error = 4


class ReportItem(BaseModel):
    severity: Severity
    code: str
    path: str = ""
    message: str = ""


class Report(BaseModel):
    source: str = ""
    target: str = ""
    items: list[ReportItem] = Field(default_factory=list)

    def add(self, severity: Severity, code: str, path: str = "", message: str = "") -> None:
        self.items.append(ReportItem(severity=severity, code=code, path=path, message=message))

    def extend(self, other: Report) -> None:
        self.items.extend(other.items)

    @property
    def worst(self) -> Severity:
        return max((i.severity for i in self.items), default=Severity.info)

    @property
    def status(self) -> str:
        if self.worst >= Severity.error:
            return "failed"
        if self.worst >= Severity.lossy:
            return "partial"
        return "ok"
