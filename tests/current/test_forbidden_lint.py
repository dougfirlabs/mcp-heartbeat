"""The forbidden-primitive lint, and the lint's own credibility.

A clean lint is only worth something if it can fail. Half of this file
proves the matcher actually catches each forbidden primitive when one is
planted, because a lint that cannot go red is a comment.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mcp_heartbeat_current import contract, lint
from mcp_heartbeat_current.lint import (
    DECLARATION_MODULES,
    FORBIDDEN_LITERALS,
    SDK_MODULES,
    lint_package,
    unexpected_sdk_importers,
)


@pytest.fixture(scope="module")
def report():
    return lint_package()


# ── the real package ──────────────────────────────────────────────────


def test_the_current_adapter_uses_no_forbidden_primitive(report) -> None:
    assert report.clean, lint.format_report(report)


def test_every_module_was_actually_scanned(report) -> None:
    """A lint that silently scanned nothing would also be "clean"."""
    package = Path(contract.__file__).resolve().parent
    on_disk = {path.name for path in package.glob("*.py")}
    assert set(report.modules_scanned) == on_disk
    assert len(on_disk) >= 10


def test_only_the_declaration_modules_may_name_a_forbidden_primitive(report) -> None:
    """Something has to hold the refusal list; nothing else may."""
    assert DECLARATION_MODULES == {"contract.py", "era.py", "lint.py"}
    assert DECLARATION_MODULES.issubset(set(report.modules_scanned))


def test_only_the_sdk_seam_imports_the_official_sdk(report) -> None:
    assert report.sdk_importers == ("sdk.py",)
    assert unexpected_sdk_importers(report) == ()
    assert SDK_MODULES == {"sdk.py"}


def test_the_lint_covers_every_primitive_hb00_forbade() -> None:
    covered = set(FORBIDDEN_LITERALS.values())
    declared = {p.primitive for p in contract.FORBIDDEN_PRIMITIVES}
    uncovered = declared - covered
    assert uncovered == {"server-initiated JSON-RPC requests on an SSE stream"}, (
        "the only primitive with no literal to match is the one that is a "
        "transport behaviour rather than a string; it is covered by the "
        "absence of any stream-writing code, proven by test_no_module_opens_a_stream"
    )


def test_no_module_opens_a_stream_or_speaks_http_directly(report) -> None:
    """The adapter builds messages; a transport moves them.

    That separation is what makes "no standalone GET stream" and "no
    server-initiated request on an SSE stream" true by construction rather
    than by discipline.
    """
    package = Path(contract.__file__).resolve().parent
    banned = ("http.client", "urllib.request", "socket", "requests", "httpx", "sse")
    for name in report.modules_scanned:
        source = (package / name).read_text(encoding="utf-8")
        for module in banned:
            assert f"import {module}" not in source, f"{name} imports {module}"


# ── the lint can fail ─────────────────────────────────────────────────


@pytest.mark.parametrize("fragment,primitive", sorted(FORBIDDEN_LITERALS.items()))
def test_the_lint_catches_each_planted_primitive(
    fragment: str, primitive: str, tmp_path: Path
) -> None:
    (tmp_path / "offender.py").write_text(
        textwrap.dedent(
            f'''
            """A module docstring that is not scanned."""
            METHOD = "{fragment}"
            '''
        ),
        encoding="utf-8",
    )
    planted = lint_package(tmp_path)
    assert not planted.clean
    # Membership, not position: matching is by substring, so a literal can
    # legitimately trip more than one entry ("notifications/initialized"
    # contains "initialize"). Over-reporting a forbidden primitive is the
    # safe direction for this lint to err in.
    assert primitive in {v.primitive for v in planted.violations}
    assert {v.module for v in planted.violations} == {"offender.py"}
    assert "offender.py" in lint.format_report(planted)


def test_a_docstring_mention_is_not_a_violation(tmp_path: Path) -> None:
    """The adapter must be able to explain what it refuses.

    A lint that punished the explanation would be satisfied by deleting the
    documentation, which is the opposite of what should be incentivised.
    """
    (tmp_path / "explainer.py").write_text(
        textwrap.dedent(
            '''
            """We never call initialize, and we ignore Mcp-Session-Id."""


            def why() -> str:
                """resources/subscribe was replaced by subscriptions/listen."""
                return "modern"
            '''
        ),
        encoding="utf-8",
    )
    assert lint_package(tmp_path).clean


def test_header_spelling_cannot_evade_the_matcher(tmp_path: Path) -> None:
    (tmp_path / "sneaky.py").write_text(
        'HEADER = "MCP-SESSION-ID"\n', encoding="utf-8"
    )
    report = lint_package(tmp_path)
    assert not report.clean
    assert report.violations[0].primitive == "Mcp-Session-Id"


def test_an_unexpected_sdk_import_is_reported(tmp_path: Path) -> None:
    (tmp_path / "leaky.py").write_text("from mcp.client import Client\n", encoding="utf-8")
    report = lint_package(tmp_path)
    assert report.sdk_importers == ("leaky.py",)
    assert unexpected_sdk_importers(report) == ("leaky.py",)


def test_the_report_serialises_for_the_evidence_artifact(report) -> None:
    import json

    payload = report.to_dict()
    assert payload["artifact"] == "forbidden-primitive-lint"
    assert payload["clean"] is True
    assert len(payload["forbidden_primitives"]) == 8
    json.dumps(payload)  # must be archivable as-is
