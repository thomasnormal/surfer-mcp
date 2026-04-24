from __future__ import annotations

import pytest

from simvision_wcp.server import _EVENT_MENU_TCL, _register_gui_event_hooks


class FakeSimVision:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, tcl: str) -> str:
        self.sent.append(tcl)
        return ""


@pytest.mark.asyncio
async def test_register_gui_event_hooks_skips_non_gui_events() -> None:
    sv = FakeSimVision()

    await _register_gui_event_hooks(sv, {"waveforms_loaded"})

    assert sv.sent == []


@pytest.mark.asyncio
async def test_register_gui_event_hooks_installs_for_gui_events() -> None:
    sv = FakeSimVision()

    await _register_gui_event_hooks(sv, {"add_loads"})

    assert sv.sent == [_EVENT_MENU_TCL]


def test_event_menu_tcl_creates_wcp_namespace_before_proc() -> None:
    namespace_pos = _EVENT_MENU_TCL.index("namespace eval ::wcp {}")
    proc_pos = _EVENT_MENU_TCL.index("proc ::wcp::_installed")

    assert namespace_pos < proc_pos
