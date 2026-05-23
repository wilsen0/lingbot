"""Property-based + unit tests for ``LedgerRenderer``.

Properties (per design.md "Testing Strategy"):

* **Property 5** — Determinism:identical input ⇒ byte-identical
  ``Message.content`` (Req 10.2 / 11.4).
* **Property 6** — Budget upper bound:rendered content never
  exceeds ``Total_Char_Budget`` (Req 4.6 / 4.8 / 11.1 / 11.2).
* **Property 7** — Truncation count accounting:``<truncated
  count="N"/>`` accurately tracks dropped events (Req 4.7).
* **Property 8** — XML round-trip:final ``Message.content`` parses
  cleanly under XML 1.0, even when input fields carry special
  characters (Req 5.3 / 5.4 / 5.5 / 6.5 / 6.6 / 11.6).
* **Property 9** — Empty / fully-filtered → ``None`` (Req 1.12 /
  4.9 / 4.10 / 10.3 / 11.7).

Plus boundary unit tests for the ``total_char_budget`` range guard
and the audit-decoupling invariant.
"""

from __future__ import annotations

import inspect
from xml.etree import ElementTree as ET

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from linling_core.pipeline import DslEvent

from linling_agent.ledger import LedgerRenderer

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Plain ASCII subset that round-trips through XML without surprises;
# used by Property 5 / 6 / 7 / 9 where we don't need to stress XML
# escaping.
_safe_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        max_codepoint=0x7E,
    ),
    min_size=0,
    max_size=20,
)

# Stress text including XML metacharacters AND control characters
# (which are XML-1.0-illegal and must be scrubbed, not just escaped).
# Property 8 explicitly probes the renderer's control-char handling
# because real DSL output / KV-restored data can carry NUL bytes,
# form feeds, etc.
_xml_stress_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # surrogates only — leave Cc in
        whitelist_characters="<>&\"'",
        max_codepoint=0x10FFFF,
    ),
    min_size=0,
    max_size=15,
)


def _event_strategy(
    *,
    text_strategy: st.SearchStrategy[str] = _safe_text,
    summary_strategy: st.SearchStrategy[str] | None = None,
) -> st.SearchStrategy[DslEvent]:
    return st.builds(
        DslEvent,
        timestamp=st.from_regex(r"\A[0-9]{2}:[0-9]{2}:[0-9]{2}\Z"),
        trigger=text_strategy.filter(lambda s: len(s) > 0),
        args=st.lists(text_strategy, max_size=4).map(tuple),
        summary=(summary_strategy or text_strategy),
        outcome=st.sampled_from(["ok", "error"]),
        mode=st.sampled_from(["trigger_only", "with_result"]),
        actor_id=text_strategy.filter(lambda s: len(s) > 0),
        occurred_at=st.floats(min_value=0, max_value=1e10, allow_nan=False),
    )


# ---------------------------------------------------------------------------
# Property 5 — Renderer determinism
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(events=st.lists(_event_strategy(), max_size=12))
def test_property_5_renderer_determinism(events: list[DslEvent]) -> None:
    renderer = LedgerRenderer(total_char_budget=800, include_actor=False)
    a = renderer.render(events)
    b = renderer.render(events)
    if a is None:
        assert b is None
    else:
        assert b is not None
        # Byte-identical under UTF-8.
        assert a.content.encode("utf-8") == b.content.encode("utf-8")
        assert a.role == b.role == "system"


# ---------------------------------------------------------------------------
# Property 6 — Budget upper bound
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(
    events=st.lists(_event_strategy(), max_size=20),
    budget=st.integers(min_value=200, max_value=8000),
)
def test_property_6_render_within_budget(events: list[DslEvent], budget: int) -> None:
    renderer = LedgerRenderer(total_char_budget=budget)
    out = renderer.render(events)
    if out is None:
        return
    # Wrapping tags are present exactly once, no surrounding whitespace.
    assert out.content.startswith("<recent_user_actions>")
    assert out.content.endswith("</recent_user_actions>")
    assert out.content.count("<recent_user_actions>") == 1
    assert out.content.count("</recent_user_actions>") == 1
    assert len(out.content) <= budget


# ---------------------------------------------------------------------------
# Property 7 — Truncation count accounting
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(
    events=st.lists(_event_strategy(), min_size=1, max_size=20),
    budget=st.integers(min_value=200, max_value=8000),
)
def test_property_7_truncation_count_matches_drops(
    events: list[DslEvent],
    budget: int,
) -> None:
    """When the renderer drops N events, ``<truncated count="N"/>``
    appears with that exact N; if no events were dropped, the marker
    is absent."""
    renderer = LedgerRenderer(total_char_budget=budget)
    out = renderer.render(events)
    if out is None:
        return

    # Reconstruct from the visible-action count.
    visible_input = [e for e in events if e.outcome == "ok"]
    action_lines = out.content.count("<action ")

    if action_lines == len(visible_input):
        assert "<truncated" not in out.content
    else:
        omitted = len(visible_input) - action_lines
        assert omitted > 0
        # Truncation marker is present and counts the omitted events.
        marker = f'<truncated count="{omitted}"/>'
        assert marker in out.content


# ---------------------------------------------------------------------------
# Property 8 — XML 1.0 round-trip with special characters
# ---------------------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(events=st.lists(_event_strategy(text_strategy=_xml_stress_text), min_size=1, max_size=8))
def test_property_8_xml_round_trip(events: list[DslEvent]) -> None:
    """``Message.content`` parses without error under XML 1.0."""
    renderer = LedgerRenderer(total_char_budget=8000, include_actor=True)
    out = renderer.render(events)
    if out is None:
        return
    # Should parse without raising.
    root = ET.fromstring(out.content)
    assert root.tag == "recent_user_actions"


# ---------------------------------------------------------------------------
# Property 9 — Empty / fully-filtered input → None
# ---------------------------------------------------------------------------


@settings(max_examples=20, deadline=None)
@given(
    error_events=st.lists(
        _event_strategy().map(
            lambda e: DslEvent(
                timestamp=e.timestamp,
                trigger=e.trigger,
                args=e.args,
                summary=e.summary,
                outcome="error",  # force every event to error
                mode=e.mode,
                actor_id=e.actor_id,
                occurred_at=e.occurred_at,
            )
        ),
        max_size=10,
    ),
)
def test_property_9_all_error_events_yield_none(error_events: list[DslEvent]) -> None:
    renderer = LedgerRenderer()
    assert renderer.render(error_events) is None


def test_property_9_empty_input_yields_none() -> None:
    assert LedgerRenderer().render([]) is None


# ---------------------------------------------------------------------------
# Boundary unit tests
# ---------------------------------------------------------------------------


def test_total_char_budget_out_of_range_raises_value_error() -> None:
    with pytest.raises(ValueError):
        LedgerRenderer(total_char_budget=199)
    with pytest.raises(ValueError):
        LedgerRenderer(total_char_budget=8001)


def test_renderer_does_not_access_audit_sink() -> None:
    """The renderer's constructor accepts no audit handle; that's the
    decoupling guarantee from Requirement 10.2 / 10.3."""
    sig = inspect.signature(LedgerRenderer.__init__)
    assert "audit" not in sig.parameters
    assert "audit_sink" not in sig.parameters


def test_with_actor_returns_self_when_flag_unchanged() -> None:
    """``with_actor`` is a zero-allocation no-op when the flag matches."""
    renderer = LedgerRenderer(include_actor=False)
    assert renderer.with_actor(False) is renderer
    flipped = renderer.with_actor(True)
    assert flipped is not renderer
    # Original instance unchanged.
    assert renderer.with_actor(False) is renderer


def test_control_characters_in_summary_do_not_break_xml() -> None:
    """Regression:NUL/form-feed/etc. in handler output must be scrubbed.

    XML 1.0 forbids most C0 control chars outright — they cannot even
    be expressed as numeric character references. The renderer
    replaces them with U+FFFD so the output stays parseable
    (Requirement 11.6).
    """
    event = DslEvent(
        timestamp="00:00:00",
        trigger="ping",
        args=("with\x00null", "form\x0cfeed"),
        summary="bell\x07inside\x1f",
        outcome="ok",
        mode="with_result",
        actor_id="alice",
        occurred_at=1.0,
    )
    out = LedgerRenderer(include_actor=True).render([event])
    assert out is not None
    # The XML parser must not raise on the rendered content.
    root = ET.fromstring(out.content)
    assert root.tag == "recent_user_actions"
    # The replacement character has taken the place of every illegal
    # control char.
    action = root.find("action")
    assert action is not None
    assert "\x00" not in action.attrib.get("args", "")
    assert "\x0c" not in action.attrib.get("args", "")
    assert "\x07" not in action.attrib.get("summary", "")
    assert "\x1f" not in action.attrib.get("summary", "")


def test_legal_xml_whitespace_preserved() -> None:
    """Tab / LF / CR are legal in XML 1.0 and must survive scrubbing."""
    event = DslEvent(
        timestamp="00:00:00",
        trigger="ping",
        args=(),
        summary="line1\nline2\tcol\rend",
        outcome="ok",
        mode="with_result",
        actor_id="u",
        occurred_at=1.0,
    )
    out = LedgerRenderer().render([event])
    assert out is not None
    root = ET.fromstring(out.content)
    summary = root.find("action").attrib.get("summary", "")  # type: ignore[union-attr]
    # Legal whitespace round-trips intact.
    assert "\n" in summary
    assert "\t" in summary
    assert "\r" in summary or "\n" in summary  # XML normalises \r\n→\n
    # The replacement character was *not* introduced.
    assert "\ufffd" not in summary
