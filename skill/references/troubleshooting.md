# Troubleshooting

Start with `nspire_status()`. Most problems are the bridge document not being
open or not being on its first page.

## "No reply from the TI-Nspire bridge" / `alive: false`

- TI-Nspire is not running, `nspire_bridge.tns` is not open, or the bridge page
  is not the active page. Ask the user to open it and stay on the page that
  shows `NSPIRE BRIDGE  tick N`.
- The user can check outside the agent by running `uv run --script selftest.py`
  in the server folder.

## Tick count rises but requests still time out

TI-Nspire has stopped seeing clipboard changes. Ask the user to close and
reopen `nspire_bridge.tns`, or restart TI-Nspire. Nothing on the agent side
fixes this; do not keep retrying.

## Tick counter not moving

Ask the user to click the bridge page once. If that does not help, the script
may need to be set again (see the server's README, "Rebuilding the bridge
document").

## `nspire` shows as failed in `claude mcp list`

- On the very first run the connection can time out while dependencies
  download. The user runs `uv sync --script server.py` in the server folder
  and restarts Claude Code.
- An import error means the wrong Python; uv must pick 3.11 to 3.14.
- Rerunning `setup.ps1` repairs the registration (for example after moving
  the folder).

## The MCP tools are missing in the session

The server is not registered or Claude Code was not restarted after setup.
See `setup.md`.

## "clipboard stayed locked"

A clipboard manager or a remote-desktop session is holding the clipboard.
Retry once, or ask the user to pause that tool.

## Odd answers

Variables stored in the bridge document (`a:=5`, `Define f(x)=...`) are
substituted into later answers. `nspire_latex` lists the ones it substituted in
its `warnings`; `nspire_vars` lists all of them. The user may have stored them,
or an earlier session may have, so ask before deleting any with
`nspire_clear(["a"])` or `DelVar a`.

Trig answers that look like degrees, or decimals where you expected exact values,
come from the document's settings: check `modes` in `nspire_status` (angle mode,
auto or approx). The user changes them in TI-Nspire's document settings.

## `nspire_status` reports `protocol: 1`

The open `nspire_bridge.tns` has the older bridge script. Everything works, but
`nspire_eval_batch` sends one request per expression. Suggest the user open the
current `nspire_bridge.tns` from the nspire-mcp folder.

## Symbolic input returns an error with a note about CAS

The non-CAS edition of TI-Nspire is installed (`nspire_status` reports
`cas: false`). Only numeric expressions work.

## LaTeX could not be parsed

Translate the problem to TI-Nspire syntax yourself and use `nspire_eval`.

## `nspire_notes` built a page of the wrong type or a half-built page

- Half-built: TI-Nspire lost focus while typing (the user clicked elsewhere),
  so it stopped. The new document is unsaved; the user can close it and you
  can run the call again once they are not using the keyboard.
- Wrong page type or wrong kind of document: the menus of the user's
  TI-Nspire version differ from version 6.x, which the tool assumes. Try
  `page_size="handheld"`, which uses a different route.
- "TI-Nspire window not found" although a window says TI-Nspire: the tool only
  types into the TI-Nspire CX (CAS) Student or Teacher Software program, never a
  browser tab or editor with that title. The error names the programs it
  ignored. Ask the user to open the TI-Nspire software.
