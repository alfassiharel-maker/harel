"""Diagnostics: how the compiler says no.

One rule drives the design: a footprint policy is reviewed by a human before it
is applied, so a compiler that stops at the first error wastes a review cycle
per typo. The lexer, parser and checker therefore collect diagnostics and keep
going wherever recovery is unambiguous, and the caller decides whether warnings
are fatal.

Codes are stable strings (`SQZ0101`) so a CI job can allow a specific warning
without allowing all warnings. The first two digits name the phase: 01 lexer,
02 parser, 03 checker, 04 planner, 05 verifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ["Diagnostic", "DiagnosticBag", "Severity", "SqueezeError"]


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    #: Not a defect: something the plan could not compute, reported so the
    #: reviewer sees the gap instead of a plausible-looking zero.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    line: int
    column: int
    #: What the reader should do about it. Omitted when there is nothing useful
    #: to say beyond the message.
    hint: str | None = None

    def render(self, source_name: str = "<policy>") -> str:
        head = f"{source_name}:{self.line}:{self.column}: {self.severity.value} [{self.code}] {self.message}"
        return head if self.hint is None else f"{head}\n    hint: {self.hint}"


class SqueezeError(Exception):
    """Raised when compilation cannot continue or produced errors."""

    def __init__(self, diagnostics: list[Diagnostic], source_name: str = "<policy>") -> None:
        self.diagnostics = diagnostics
        self.source_name = source_name
        body = "\n".join(d.render(source_name) for d in diagnostics)
        super().__init__(f"{len(diagnostics)} diagnostic(s)\n{body}")


@dataclass
class DiagnosticBag:
    items: list[Diagnostic] = field(default_factory=list)

    def error(self, code: str, message: str, line: int, column: int, hint: str | None = None) -> None:
        self.items.append(Diagnostic(code, Severity.ERROR, message, line, column, hint))

    def warn(self, code: str, message: str, line: int, column: int, hint: str | None = None) -> None:
        self.items.append(Diagnostic(code, Severity.WARNING, message, line, column, hint))

    def unknown(self, code: str, message: str, line: int, column: int, hint: str | None = None) -> None:
        self.items.append(Diagnostic(code, Severity.UNKNOWN, message, line, column, hint))

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity is Severity.WARNING]

    @property
    def unknowns(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity is Severity.UNKNOWN]

    def has_errors(self) -> bool:
        return any(d.severity is Severity.ERROR for d in self.items)

    def raise_if_errors(self, source_name: str = "<policy>") -> None:
        if self.has_errors():
            raise SqueezeError(self.errors, source_name)

    def extend(self, other: DiagnosticBag) -> None:
        self.items.extend(other.items)
