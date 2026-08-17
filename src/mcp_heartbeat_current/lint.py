"""Mechanical proof that the current path uses no forbidden primitive.

"We didn't call ``initialize``" is a claim a reviewer can only spot-check.
This module turns it into an assertion: it parses every module in the
package and reports any forbidden primitive that appears in *executable*
code — an emitted method name, a header key, a capability identifier.

Docstrings and comments are excluded on purpose. The adapter has to be able
to explain what it refuses and why, and a lint that punished the
explanation would be satisfied by deleting the documentation. What matters
is whether a forbidden string can reach the wire, and only a non-docstring
literal can.

Three modules are allowed to *name* forbidden primitives as data, because
something has to hold the refusal list:

* :mod:`~mcp_heartbeat_current.contract` — the table itself;
* :mod:`~mcp_heartbeat_current.era` — the classifier that refuses them;
* this module — the matcher.

Every other module must be clean. The report is emitted as JSON for the
PRD's "forbidden-primitive lint" evidence artifact.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .contract import FORBIDDEN_PRIMITIVES

#: Modules permitted to spell a forbidden primitive as data.
DECLARATION_MODULES: frozenset[str] = frozenset({"contract.py", "era.py", "lint.py"})

#: The only module permitted to import the official SDK. Keeping the import
#: in one place is what lets the rest of the package be tested — and shipped
#: — in an environment with no MCP SDK at all.
SDK_MODULES: frozenset[str] = frozenset({"sdk.py"})

#: Literal fragments that would put a forbidden primitive on the wire,
#: mapped to the primitive they belong to. Lowercased before matching, so
#: header spelling (``Mcp-Session-Id`` vs ``mcp-session-id``) cannot evade.
FORBIDDEN_LITERALS: Mapping[str, str] = {
    "initialize": "initialize",
    "notifications/initialized": "notifications/initialized",
    "mcp-session-id": "Mcp-Session-Id",
    "last-event-id": "Last-Event-ID",
    "resources/subscribe": "resources/subscribe",
    "text/event-stream": "GET <mcp endpoint> -> text/event-stream",
    "experimental.presencelease": "experimental.presenceLease",
}


@dataclass(frozen=True)
class LintViolation:
    """One forbidden literal found in executable code."""

    module: str
    line: int
    primitive: str
    literal: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "line": self.line,
            "primitive": self.primitive,
            "literal": self.literal,
        }


@dataclass(frozen=True)
class LintReport:
    """The result of one lint run over a package directory."""

    modules_scanned: tuple[str, ...]
    violations: tuple[LintViolation, ...]
    declaration_modules: tuple[str, ...]
    sdk_importers: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": "forbidden-primitive-lint",
            "clean": self.clean,
            "modules_scanned": list(self.modules_scanned),
            "declaration_modules": list(self.declaration_modules),
            "sdk_importers": list(self.sdk_importers),
            "forbidden_primitives": [
                {"primitive": p.primitive, "kind": p.kind, "requirement_level": p.requirement_level}
                for p in FORBIDDEN_PRIMITIVES
            ],
            "violations": [v.to_dict() for v in self.violations],
        }


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """``id()`` of every Constant that is a docstring, so we can skip them."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                skip.add(id(first.value))
    return skip


def _string_constants(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """``(lineno, value)`` for every non-docstring string literal."""
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.lineno, node.value


def _imports_sdk(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "mcp" or a.name.startswith("mcp.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "mcp" or module.startswith("mcp.") or module.startswith("mcp_types"):
                return True
    return False


def lint_package(package_root: Path | None = None) -> LintReport:
    """Scan every module in the current adapter and report violations."""
    root = Path(package_root) if package_root is not None else Path(__file__).resolve().parent
    modules = sorted(p for p in root.glob("*.py"))

    violations: list[LintViolation] = []
    sdk_importers: list[str] = []
    for path in modules:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        if _imports_sdk(tree):
            sdk_importers.append(path.name)
        if path.name in DECLARATION_MODULES:
            continue
        for lineno, value in _string_constants(tree):
            lowered = value.lower()
            for fragment, primitive in FORBIDDEN_LITERALS.items():
                if fragment in lowered:
                    violations.append(
                        LintViolation(
                            module=path.name,
                            line=lineno,
                            primitive=primitive,
                            literal=value,
                        )
                    )

    return LintReport(
        modules_scanned=tuple(p.name for p in modules),
        violations=tuple(violations),
        declaration_modules=tuple(sorted(DECLARATION_MODULES)),
        sdk_importers=tuple(sorted(sdk_importers)),
    )


def unexpected_sdk_importers(report: LintReport) -> tuple[str, ...]:
    """Modules importing the official SDK that are not allowed to.

    The SDK must stay behind one seam so the package remains installable
    and testable without it — the same rule the portable core enforces one
    level down, applied one level up.
    """
    return tuple(m for m in report.sdk_importers if m not in SDK_MODULES)


def format_report(report: LintReport) -> str:
    """A short human summary; the JSON form is :meth:`LintReport.to_dict`."""
    if report.clean:
        return (
            f"forbidden-primitive lint: clean "
            f"({len(report.modules_scanned)} modules, "
            f"{len(FORBIDDEN_PRIMITIVES)} primitives checked)"
        )
    lines = [f"forbidden-primitive lint: {len(report.violations)} violation(s)"]
    lines.extend(
        f"  {v.module}:{v.line} uses {v.primitive} ({v.literal!r})" for v in report.violations
    )
    return "\n".join(lines)


def iter_forbidden_primitives() -> Iterable[str]:
    """Names of every primitive this lint is responsible for."""
    return (p.primitive for p in FORBIDDEN_PRIMITIVES)


__all__ = [
    "DECLARATION_MODULES",
    "FORBIDDEN_LITERALS",
    "SDK_MODULES",
    "LintReport",
    "LintViolation",
    "format_report",
    "iter_forbidden_primitives",
    "lint_package",
    "unexpected_sdk_importers",
]
