"""Live tests that run against the shared session-scoped SimVision fixture."""

from __future__ import annotations

import shutil

import pytest

from simvision_mcp.client import SimVisionError


pytestmark = pytest.mark.skipif(
    shutil.which("simvision") is None,
    reason="simvision not on PATH (Cadence env not sourced)",
)


async def test_expr(sv):
    assert await sv.send("expr 1+2") == "3"


async def test_string_toupper(sv):
    # string reverse was added in Tcl 8.5; SimVision ships Tcl 8.4.
    assert await sv.send("set x hello; string toupper $x") == "HELLO"


async def test_utf8_roundtrip(sv):
    # `return` at global scope errors — use set instead.
    assert await sv.send('set tmp "æøå →"') == "æøå →"


async def test_multiline_semicolons(sv):
    assert await sv.send("set a 1; set b 2; expr $a + $b") == "3"


async def test_error_propagation(sv):
    with pytest.raises(SimVisionError) as exc_info:
        await sv.send("this_is_not_a_command")
    assert "invalid command name" in str(exc_info.value)


async def test_scope_get(sv):
    # No database loaded — scope is the root "::".
    assert await sv.send("scope get") == "::"


async def test_database_find_empty(sv):
    # No databases opened yet in this fresh session.
    assert await sv.send("database find") == ""


async def test_cursor_lifecycle(sv):
    name = "PytestCur"
    # Clean up in case a prior test left it behind.
    await sv.send(f"catch {{cursor delete {name}}}")

    r = await sv.send(f"cursor new -name {name} -time 250ns")
    assert name in r

    assert name in await sv.send("cursor find")

    await sv.send(f"cursor delete {name}")
    assert name not in await sv.send("cursor find")


async def test_tcl_info(sv):
    # Document the Tcl version we're talking to — sanity check + debugging aid.
    ver = await sv.send("info patchlevel")
    assert ver.startswith("8."), f"unexpected Tcl version: {ver!r}"
    print(f"\n  SimVision Tcl version: {ver}")
