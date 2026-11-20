# nspire-mcp

Lets Claude Code evaluate expressions in the TI-Nspire CX (CAS) Windows app.
A Lua script on a page of an open document watches the Windows clipboard;
an MCP server writes requests there and reads the answers back.

## 1. Make the bridge document (once)

1. In TI-Nspire, File > New TI-Nspire Document. If it asks for a page type, add a Calculator page.
2. Insert > Script Editor > Insert Script. Name it `bridge`.
3. Delete the template text, paste all of `bridge.lua`, click **Set Script** (Ctrl+S), close the editor.
4. The page shows `NSPIRE BRIDGE  tick N` with N going up.
5. File > Save As > `nspire_bridge.tns` (anywhere, e.g. this folder).

To update the script later: open the document, Insert > Script Editor > Edit Script, paste, Set Script, save.

## 2. Each session

Open `nspire_bridge.tns` and leave it on the bridge page. The window can be in
the background or minimised; the script keeps polling.

Definitions (`Define f(x)=...`, `a:=5`) are stored in the document's problem,
so they persist between requests and are saved if you save the document.

## 3. The MCP server

Needs [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`). `server.py`
declares its own dependencies (`mcp<2`, `pywin32`) and Python (3.11–3.13; pywin32
doesn't load on 3.15 yet), so uv builds the environment on first run:

    uv run --script server.py

It speaks MCP over stdio and logs to stderr. Tools:

| Tool | Does |
| --- | --- |
| `nspire_status()` | ping: `alive`, `api_level`, `cas` |
| `nspire_eval(expr, timeout_s=5)` | one result string, or an error |
| `nspire_eval_batch(exprs, timeout_s=5)` | `[{expr, ok, result \| error}]` in order; stops at the first timeout |

One request is in flight at a time. Before each call the server saves your
clipboard text and restores it afterwards (only if the clipboard still holds
bridge traffic, so a copy you made meanwhile wins). Non-text clipboard
contents, such as an image, are not restored.

## 4. Register with Claude Code (user scope)

Use the full path to `uv.exe` so it works even when Claude Code's PATH predates
the uv install (`(Get-Command uv).Source` prints it):

    claude mcp add -s user nspire -- "C:\Users\ville.jokinen\AppData\Local\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe" run --script "C:\Users\ville.jokinen\Documents\nspire-mcp\server.py"

If `claude` isn't on PATH (desktop app only), use the CLI the app bundles:
`%APPDATA%\Claude\claude-code\<version>\<hash>\claude.exe`.

That covers Claude Code (the desktop app's Code tab included). The desktop
app's **Chat** tab reads `%APPDATA%\Claude\claude_desktop_config.json` instead:
add the same command under `"mcpServers": {"nspire": {"command": ..., "args": [...]}}`
and fully quit and reopen the app.

Restart Claude Code, then `claude mcp list` should show `nspire` connected.
Remove with `claude mcp remove -s user nspire`.

## 5. Self-test

With the bridge open:

    uv run --script selftest.py

Pings, then checks `2+2`, `factor`, `solve`, `∫` and a deliberate syntax error.
On the non-CAS edition the symbolic cases pass if they return a clean error.

## Protocol

| Message | Meaning |
| --- | --- |
| `NSPIRE_IN:<id>:<expr>` | evaluate `expr` |
| `NSPIRE_IN:<id>:__PING__` | health check |
| `NSPIRE_OUT:<id>:OK:<result>` | result, exactly as the CAS writes it |
| `NSPIRE_OUT:<id>:OK:PONG:<apiLevel>:<cas\|nocas>` | ping reply |
| `NSPIRE_OUT:<id>:ERR:<message>` | e.g. `error 910 (syntax error)` |

`<id>` must not contain `:`. Each id is answered once.

Notes:
- Negative numbers come back with the Unicode minus `−` (U+2212), e.g. `x=−2 or x=2`.
- `√ π ▶ ≤` and other Unicode survive the round trip.

## Clipboard caveat

While a request runs (well under a second) the clipboard holds the request,
then the reply, then your old text again. Don't copy anything yourself during
that window, or your copy may be lost or the request may time out.

## Troubleshooting

- **Tick counter not moving:** click the bridge page once, or re-run Set Script.
- **apiLevel error when setting the script:** change `'2.7'` to `'2.4'` at the top of `bridge.lua`.
- **"No reply from the TI-Nspire bridge":** TI-Nspire isn't open, the bridge document isn't, or the tick counter has stopped. Run `uv run --script selftest.py` to check outside Claude.
- **`nspire` shows failed in `claude mcp list`:** run the registered command by hand; it should sit silently waiting on stdin (Ctrl+C to quit). An import error means the wrong Python; uv must pick 3.11–3.13.
- **"clipboard stayed locked":** a clipboard manager or remote-desktop session is holding it; retry, or pause that tool.
- **Non-CAS edition:** symbolic input returns a TI-Nspire error with a note; numeric expressions work.
- **No reply:** check the page is the active page of the document. A script on a page you've navigated away from may stop.
