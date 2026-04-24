# SimVision Waveform Menu Extension Hang

## Summary

Xcelium 2403 SimVision hangs when a `window extensions menu create` runs
**re-entrantly inside the idletask pump that `waveform new` performs during
window construction**. After ~4 such re-entrant menu creates, the 5th
`waveform new` wedges indefinitely.

This is not specific to `-type waveform` vs `-window $wname`. Both scopes
hang. What matters is *when* the menu create runs:

| Install timing | Result |
| --- | --- |
| No menu install, 10× `waveform new` | ok |
| Inline synchronous (`waveform new` → `menu create -window $name` in same Tcl flow) | ok |
| Global `menu create -type waveform` once, then 8× `waveform new` | hangs on #5 |
| `notify add !create` → `after idle` → per-window `menu create` + 8× `waveform new` | hangs on #5 |
| `notify add !create` → **`after 1`** → per-window `menu create` + 8× `waveform new` | ok |

The hang reproduces without Python, WCP, databases, or the MCP TCP bridge.

### Root cause: two interacting SimVision bugs

Two distinct SimVision behaviors combine to produce the hang:

**1. `after idle` callbacks run re-entrantly during `waveform new`.** SimVision's
`waveform new` pumps the Tcl idle queue internally while constructing the
window. An `after idle` callback scheduled from a `!create` notifier fires
*inside* that pump, on a still-initializing window. This alone is not fatal;
it produces errors but usually doesn't hang.

**2. Uncaught errors inside an `after` timer callback wedge `waveform new`.**
If a `menu create -window $wname` call inside the timer callback errors (e.g.
because the `"WCP"` parent menu does not yet exist on a freshly-created
window), the error propagates uncaught back out of the timer into SimVision's
bgerror path. The next `waveform new` then hangs indefinitely. Wrapping each
`menu create` in `catch` swallows the error and keeps SimVision's event loop
healthy.

Both conditions must hold. The fix is defensive against both:

- `after 1` (a real 1ms timer) queues the callback on the timer heap, not the
  idle queue. SimVision's internal idletask pump does not drain timer
  callbacks, so the menu install waits until `waveform new` has fully returned
  before running.
- Each `menu create` inside `_install_menus` is wrapped in `catch {}`. Even if
  the parent menu is unexpectedly missing, the error never escapes into
  SimVision's event loop.

The global `-type waveform` path is poisonous for similar reasons:
SimVision's `-type` auto-injection of the menu into each new window runs
during `waveform new`'s internal idletask processing, and any error during
that injection corrupts the waveform-window class state.

Because of this behavior, `simvision-wcp` installs menus per-window with
`-window <wname>`, defers the install via `after 1` from a `!create`
notifier, and wraps every `menu create` in `catch`.

## Minimal Reproducer

Save as `/tmp/sv-hang.tcl`:

```tcl
window extensions menu create -type waveform \
    "Test>Click" command -command {puts clicked}

for {set i 0} {$i < 6} {incr i} {
    waveform new -name W$i
}
exit 0
```

Run:

```bash
Xvfb :99 -screen 0 1920x1200x24 -nolisten tcp &
DISPLAY=:99 simvision -nosplash -input /tmp/sv-hang.tcl
```

Expected: SimVision creates six waveform windows and exits with status 0.

Observed with Xcelium 2403 SimVision: SimVision hangs on the fifth
`waveform new`. Killing the process is the only recovery. Removing the
`window extensions menu create` command makes repeated `waveform new` calls
complete normally.

Closing each waveform window between iterations does not avoid the hang. One
menu item is sufficient to trigger it.

## `simvision-wcp` Behavior

`simvision-wcp` has Tcl support for WCP GUI-click events:

- `goto_declaration`
- `add_drivers`
- `add_loads`

Those events require SimVision waveform context-menu items. The implementation
intentionally avoids the global waveform-type extension path and uses
per-window menu creation:

```tcl
window extensions menu create -window $wname \
    "WCP>Goto Declaration" command \
    -command {::mcp::push_event goto_declaration variable %o}
```

Future waveform windows are handled by:

```tcl
window extensions notify add -caller wcp_gui_menus \
    !create !delete ::wcp::_window_event

proc ::wcp::_window_event {args} {
    array set ev $args
    if {$ev(type) ne "waveform"} return
    if {$ev(event) eq "!create"} {
        after 1 [list ::wcp::_install_menus $ev(window)]
    }
    ...
}
```

The key is `after 1`, not `after idle`. `after idle` fires during
`waveform new`'s own idletask pump and causes the hang described above.
The notifier only schedules timers; it never calls `menu create` directly,
which would also run re-entrantly.

Inside `_install_menus`, every `menu create` is wrapped in `catch {}`. Even
with `after 1` deferral, an uncaught error propagating out of the timer
callback hangs the next `waveform new`:

```tcl
catch {window extensions menu create -window $wname \
    "WCP>Goto Declaration" command \
    -command {::mcp::push_event goto_declaration variable %o}}
```

The menu Tcl includes `namespace eval ::wcp {}` before creating
WCP namespace procs; without that namespace prelude, SimVision rejects
fully-qualified `::wcp::*` proc creation and the menus never install.

The installer deletes stale menu entries before recreating them, so a
half-installed menu from an earlier SimVision run does not produce a
duplicate-entry error.

## Workaround Smoke Test

The intended workaround is covered by
`tests/test_wcp_event_hooks.py::test_event_menu_tcl_in_real_simvision_waveform_new_smoke`.
That test runs the production Tcl in real SimVision, then creates eight
waveform windows. It should pass on a host with working Xvfb and SimVision.
