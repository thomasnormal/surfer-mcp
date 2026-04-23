"""Live tests that run against the shared session-scoped SimVision fixture.

These are skipped when SimVision isn't installed or a display isn't available.
"""

from __future__ import annotations

import os
import shutil

import pytest

from simvision_mcp.client import SimVisionError


pytestmark = [
    pytest.mark.skipif(
        shutil.which("simvision") is None,
        reason="simvision not on PATH (Cadence env not sourced)",
    ),
    pytest.mark.skipif(
        not os.environ.get("DISPLAY"),
        reason="no X display available",
    ),
]


def test_expr(sv, aio):
    assert aio(sv.send("expr 1+2")) == "3"


def test_string_toupper(sv, aio):
    # string reverse was added in Tcl 8.5; SimVision ships Tcl 8.4.
    assert aio(sv.send("set x hello; string toupper $x")) == "HELLO"


def test_utf8_roundtrip(sv, aio):
    # `return` at global scope errors — use set instead.
    assert aio(sv.send('set tmp "æøå →"')) == "æøå →"


def test_multiline_semicolons(sv, aio):
    assert aio(sv.send("set a 1; set b 2; expr $a + $b")) == "3"


def test_error_propagation(sv, aio):
    with pytest.raises(SimVisionError) as exc_info:
        aio(sv.send("this_is_not_a_command"))
    assert "invalid command name" in str(exc_info.value)


def test_scope_get(sv, aio):
    # No database loaded — scope is the root "::".
    assert aio(sv.send("scope get")) == "::"


def test_database_find_empty(sv, aio):
    # No databases opened yet in this fresh session.
    assert aio(sv.send("database find")) == ""


def test_cursor_lifecycle(sv, aio):
    name = "PytestCur"
    # Clean up in case a prior test left it behind.
    aio(sv.send(f"catch {{cursor delete {name}}}"))

    r = aio(sv.send(f"cursor new -name {name} -time 250ns"))
    assert name in r

    assert name in aio(sv.send("cursor find"))

    aio(sv.send(f"cursor delete {name}"))
    assert name not in aio(sv.send("cursor find"))


def test_tcl_info(sv, aio):
    # Document the Tcl version we're talking to — sanity check + debugging aid.
    ver = aio(sv.send("info patchlevel"))
    assert ver.startswith("8."), f"unexpected Tcl version: {ver!r}"
    print(f"\n  SimVision Tcl version: {ver}")
