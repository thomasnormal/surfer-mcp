# surfer-mcp

An MCP server that lets AI assistants control [Surfer](https://surfer-project.org/), an open-source waveform viewer for hardware design and verification. It communicates with Surfer over the [Waveform Communication Protocol (WCP)](https://surfer-project.org/wcp.html) and can also read waveform files directly via [pywellen](https://github.com/ekiwi/pywellen).

## Features

- **Load & reload** waveform files (VCD, FST, GHW, etc.)
- **Browse hierarchy** — list scopes and signals in the design
- **Control the viewer** — add/remove signals, set cursor position, zoom, set viewport range, add markers, change signal colors
- **Read signal data** — get values at a timestamp, find edges, search for values, get all transitions in a time window
- **Screenshot** the Surfer window (macOS)

## Requirements

- Python 3.10+
- [Surfer](https://surfer-project.org/) installed and on your `PATH`
- macOS (the screenshot feature uses Quartz; other features work cross-platform if you remove the `pyobjc` dependency)

## Installation

```bash
# Clone the repo
git clone https://github.com/PaoloRondot/surfer-mcp.git
cd surfer-mcp

# Install with uv (recommended)
uv pip install -e .

# Or with pip
pip install -e .
```

## Usage with Claude Code

Add to your Claude Code MCP settings (`~/.claude/settings.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "surfer": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/surfer-mcp", "surfer-mcp"]
    }
  }
}
```

Or if you installed it globally:

```json
{
  "mcpServers": {
    "surfer": {
      "command": "surfer-mcp"
    }
  }
}
```

## Usage with other MCP clients

The server runs over **stdio**. Start it with:

```bash
surfer-mcp
```

Or without installing:

```bash
uv run --directory /path/to/surfer-mcp surfer-mcp
```

## Available Tools

| Tool | Description |
|------|-------------|
| `load_waveform` | Load a waveform file into Surfer |
| `reload_waveform` | Reload the current waveform |
| `list_scopes` | List all scopes (modules/instances) in the hierarchy |
| `list_signals` | List signals within a scope |
| `add_signals` | Add signals to the viewer by name |
| `add_scope` | Add all signals in a scope |
| `add_items` / `remove_items` | Add or remove items by path/ID |
| `get_item_list` / `get_item_info` | Query displayed items |
| `set_cursor` | Move the cursor to a timestamp |
| `set_viewport_range` / `set_viewport_to` | Control the visible time range |
| `zoom_to_fit` | Zoom to fit all data |
| `add_markers` | Add named time markers |
| `focus_item` | Scroll to an item |
| `set_item_color` | Change signal display color |
| `get_signal_values` | Read signal values at a timestamp |
| `find_rising_edge` / `find_falling_edge` | Find next edge of a signal |
| `find_value` | Find next occurrence of a specific value |
| `get_transitions` | Get all transitions in a time window |
| `get_time_range` | Get simulation time range and timescale |
| `screenshot` | Capture the Surfer window (macOS) |
| `clear` | Clear all items from the viewer |
| `shutdown_surfer` | Shut down Surfer |

## How it works

When a tool is first called, the server automatically:
1. Launches Surfer with WCP enabled
2. Connects over TCP and performs the WCP handshake
3. Sends commands and returns results

The Surfer process stays running between tool calls so you can interactively explore waveforms.

## License

MIT
