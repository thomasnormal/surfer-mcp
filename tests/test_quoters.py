"""Pure-Python unit tests for Tcl quoting helpers — no SimVision needed."""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from simvision_mcp.client import parse_tcl_list, tcl_brace, tcl_list  # noqa: E402


def test_brace_roundtrip_simple():
    # tcl_brace produces a single-item Tcl word that parses back to the input.
    for src in ["foo", "", "hello world", "a[b]", "has$dollar", "name[3:0]"]:
        assert parse_tcl_list(tcl_brace(src)) == [src], (src, tcl_brace(src))


def test_brace_roundtrip_tricky():
    # Inputs that would break a naive brace wrapper.
    for src in ["unbalanced{", "also}bad", "has\\backslash", 'with "quotes"',
                "nested {{x}} braces"]:
        assert parse_tcl_list(tcl_brace(src)) == [src], (src, tcl_brace(src))


def test_list_roundtrip():
    items = ["a", "b c", "d", "foo[3]", "empty", ""]
    assert parse_tcl_list(tcl_list(items)) == items


def test_parse_tcl_list_basic():
    assert parse_tcl_list("a b c") == ["a", "b", "c"]
    assert parse_tcl_list("") == []
    assert parse_tcl_list("  whitespace   is  ok  ") == ["whitespace", "is", "ok"]


def test_parse_tcl_list_brace_quoted():
    # Items with spaces or special chars are brace-quoted by Tcl.
    assert parse_tcl_list("a {b c} d") == ["a", "b c", "d"]
    assert parse_tcl_list("{foo[3]} {foo[2]} plain") == ["foo[3]", "foo[2]", "plain"]


def test_parse_tcl_list_escapes():
    # Backslash escapes are something my hand-rolled parser dropped. The stdlib
    # Tcl parser handles them correctly.
    got = parse_tcl_list(r"a\ b c")
    assert got == ["a b", "c"], got


def test_parse_tcl_list_nested_braces():
    assert parse_tcl_list("{outer {inner} more}") == ["outer {inner} more"]


def test_parse_tcl_list_roundtrip():
    # tcl_list() encodes -> parse_tcl_list decodes back to the original items.
    items = ["plain", "has space", "with[brackets]", "$dollar"]
    got = parse_tcl_list(tcl_list(items))
    assert got == items, got
