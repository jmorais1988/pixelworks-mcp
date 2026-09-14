# pixelworks — developer notes

Protocol details, headless plumbing and the traps discovered while building
this server. User-facing setup lives in `README.md`.

## Architecture in one paragraph

Everything is in `server.py`: a fastmcp 4 `FastMCP("pixelworks")` with 169
tools in four families (`rd_*` 31, `ase_*` 95, `krita_*` 39, `px_*` 4). Each
family talks to its subsystem through a thin transport — a persistent
WebSocket to the Retro Diffusion backend, `Aseprite.exe --batch --script`
spawns with JSON temp files for the workbench, and HTTP POSTs to the Krita
bridge plugin. `_install_gates()` (bottom of the file) wraps every
subsystem-dependent tool at import time via `await mcp.list_tools()` +
`FunctionTool.fn` swapping, so an unavailable subsystem yields
`{"available": false, "subsystem", "error", "hint"}` instead of an exception.

## Retro Diffusion backend protocol (extension v15.0.0)

- Transport: WebSocket JSON at `ws://127.0.0.1:8765` (`image_server.py`
  inside the extension's `stable-diffusion-aseprite/` folder, own venv).
- Handshake: client sends `{"action":"connected", ..., "version":"15.0.0"}`;
  the server replies `{"action":"connected"}` only when versions match — the
  pin is `RD_VERSION` in `server.py`.
- Generations: `{"action":"txt2img"|"img2img"|..., "value":{...}}`; the
  backend streams `display_title` / `display_image` / `ping` and finishes
  with `{"action":"returning", ...}`; the client acks `{"action":"recieved"}`
  (sic — the typo is the protocol's).
- Image entries: `format: "png"` → base64 PNG, `format: "bytes"` → base64
  raw RGBA.
- Cloud `rd_api_*` key (`_api_key()`): explicit `api_key` arg → `RD_API_KEY`
  env → `EXT_DIR/data/settings.json["rdapikey"]` (what the RD Aseprite
  dialog saves). Any read error on that file is swallowed and treated as
  "no key".
- The backend minimizes/restores its console window on **every** handshake,
  so the server keeps ONE persistent socket per process (handshake once,
  auto-reconnect on drop). `rd_status` only opens a socket by default;
  `handshake=true` is for diagnosis.
- UI slider ranges are enforced server-side: quality 1–7, steps 5–50,
  adherence 1–5, detail 1–10, centroids 2–16, dither_strength 0–10,
  smoothness/intensity 1–10, dither matrices None / Bayer 2x2 / 4x4 / 8x8.

### Known RD traps (handled here — beware in raw use)

- Small sprites requested as `width=64, pixel_size=1` give a degenerate 8×8
  latent (`canvas // 8`) that RD cannot cook; output looks like an early
  sampling step. All generation tools normalize onto RD's native grid:
  canvas = out × 8 with `pixel_size = 8`, keeping the requested output size.
  `size_preset` therefore means **output** pixels.
- `null` seeds crash the neural / ControlNet / palette paths
  (`torch.manual_seed(None)`) — seeds are always resolved to an int
  client-side.
- `dither_strength >= 34` makes palettize's gamma term zero/negative
  (ZeroDivisionError); the UI range is 0–10.
- `rd_prompt_extract` / `rd_translate` must return dicts (MCP schema
  validation rejects bare strings).

## Headless behavior (Windows; other platforms untested)

Everything Windows-specific is guarded by `sys.platform == "win32"`:
`_CREATE_NO_WINDOW`, `_hide_startup()`, the `%APPDATA%` / `Program Files`
defaults and `VENV_PY` (`venv/Scripts/python.exe` vs `venv/bin/python`).
On macOS/Linux those collapse to no-ops / POSIX paths; nothing else in the
server is platform-dependent, but no run has been done there.

- Every child process (Aseprite batch, backend launch) uses
  `CREATE_NO_WINDOW` + `STARTUPINFO(SW_HIDE)`.
- `rd_start_backend` adds a UTF-8 IO env (rich's legacy Windows renderer
  otherwise dies on cp1252-encoding its banner), `-u`, `stdin=DEVNULL` (the
  script has `input()` prompts) and logs to
  `stable-diffusion-aseprite/mcp-backend.log`. **Never** redirect the
  backend's stdout to an undrained pipe: progress prints fill the 4 KB buffer
  and stall sampling.
- The RD extension's init shells out (`cmd /c cd ... && del /q "*.bytes"`,
  even Unix-only `rm` on Windows) on **every** Aseprite start, including
  headless batches — each one flashed a console window. `extension.lua` gets
  an env-guarded no-op for `os.execute` when `RD_MCP_HEADLESS=1` (set by this
  server's batch spawns; GUI sessions unaffected). Re-apply after extension
  updates with `reapply_headless_patch.py` (idempotent).

## Aseprite workbench notes (1.3.18 batch mode)

- Pixel data crosses the headless CLI as JSON temp files (Aseprite's Lua
  `json` global). Selection and clipboard live in the **server process**
  because headless Aseprite is stateless per spawn.
- Flip/trim are pure-Lua pixel ops (no `FlipHorizontal` / `Trim` commands
  exist in batch on this build); tag direction uses `tag.aniDir` ints;
  palette length via `#pal`.
- `ResizeMethod` enum is absent → use string methods. `Sprite:resizeCanvas`
  is absent → `CanvasSize` command with left/top/right/bottom additions.
- **Frame duration units:** Aseprite 1.3+ stores `Frame.duration` in
  **seconds** (older builds: milliseconds). `_lua_dur_helpers()` probes the
  running build once per script (write 0.5, read back) and converts ms
  accordingly. Symptom of getting this wrong: durations clamped at 65535 and
  GIFs playing at ~65 s/frame.
- Cels are normalized to canvas geometry (sprite-global coordinates) before
  drawing/reading — `merge_layer_down` leaves a trimmed cel and previously
  dropped pixels silently.
- `json.decode` yields **floats**, and `putPixel` on TILEMAP-mode images
  silently ignores float args (regular images tolerate them): floor every
  coordinate and tile value before writing a tilemap cel.
- Tilemap cel writes must build a **fresh** `Image(cols, rows,
  ColorMode.TILEMAP)` and assign it (`cel.image = <distinct object>`);
  in-place mutation plus self-assign never persists. Tileset writes
  (`tile.image = clone`) persist fine inside `app.transaction`.
- Python 3.12 (PEP 701) f-strings parse a Lua brace-quote concat as a
  replacement field: braces inside Lua string literals embedded in f-strings
  must be doubled, or the tool silently prints tuple garbage.
- Agents habitually pass `layer_name` to drawing tools whose parameter is
  `layer`; the ase gate wrapper accepts it as an alias and turns stray
  `TypeError`s into `{"ok": false, "accepted_params": [...]}`.

## Krita bridge notes (libkis, Krita 5.3.3)

The bundled `krita-plugin/kritamcp` is a fork of nanayax3/krita-mcp's plugin:
the original 14 paint actions are untouched; `actions_v2.py` is a mixin
adding 24 actions, wired into `__init__.py` by a 3-point patch (mixin import
with fallback, dynamic class bases, `V2_DISPATCH` hook).

- Node has `projectionPixelData(x, y, w, h)` but **no** QImage projection
  accessor; region export goes `QImage(bytes(data), w, h,
  Format_ARGB32).copy()` then `save(path, "PNG")` — Krita's BGRA byte order
  matches `Format_ARGB32` on little-endian.
- Import direction: `QImage(path).convertToFormat(Format_ARGB32)`, then
  `img.bits()` + `setsize(sizeInBytes())` → `node.setPixelData(bytes, x, y,
  w, h)`.
- **`Filter.apply(node, x, y, w, h)` hard-crashes Krita 5.3.3** (staged
  bisection: `Filter()` + `setName` + `setConfiguration` are safe, `apply` is
  lethal — process gone, uncatchable). The plugin action returns a graceful
  disabled-error; the server exposes `krita_adjust_pixels` (Pillow over the
  get_pixels / set_pixels PNG bridge) instead.
- Selection: `select(x, y, w, h, 255)` / `selectAll(rootNode, 255)`;
  `invert()` gained a node argument in newer Krita — probed via `inspect`
  with a sip fallback.
- `Node.compositeOp` is `blendingMode()` / `setBlendingMode()` in libkis.
- Canvas (via `view.canvas()`) owns zoom / rotation / mirror / wrapAround,
  not View.
- `Document.scaleImage` xres/yres must be **int** (float raises a sip
  TypeError); `Document.filename()` does not exist (use `name()`).
- Plugin edits load only on a full Krita restart; until then new actions
  answer `{"error": "Unknown action: ..."}` through the standard gate
  contract. A running instance can be hot-patched by reassigning
  `ActionsV2Part2.V2_DISPATCH` entries via the `run_python` action.

## Tests

`tests/` — live integration batteries, direct-import style (no MCP client):

- `test_workbench.py` — Aseprite basics.
- `test_v4_expansion.py` — 40-step suite over the ported aseprite-mcp tools.
- `test_krita_v2.py` — 32-step battery against a running Krita.

Each ends with `ALL GREEN` / exit 0 or a `FAILED:` list / exit 1.

## Cutting a release

`.github/workflows/release.yml` runs on any `v*` tag: it checks the tag
against `version` in `pyproject.toml`, byte-compiles the shipped Python,
zips the files listed in the README's *Repository layout* (no `.git`,
`.github`, `.gitignore` or pycache), and publishes a GitHub Release with the
zip, a `.sha256`, and auto-generated notes.

```bash
# 1. bump the version
sed -i 's/^version = .*/version = "1.1.0"/' pyproject.toml
git commit -am "Release 1.1.0"
# 2. tag and push (the tag push is what triggers the workflow)
git tag v1.1.0
git push origin main v1.1.0
```

A tag containing `-` (e.g. `v1.1.0-rc1`) is published as a pre-release.
If the tag and `pyproject.toml` disagree the job fails and no release is
created; delete the tag (`git push --delete origin vX`), fix, and re-tag.

## Lineage

- pixel-mcp (Brandon Williams, MIT 2025) — Aseprite-through-AI concept and
  the original workbench toolset.
- krita-mcp (nanayax3, MIT 2025) — the `krita_*` surface and the bridge
  plugin this fork extends.
- aseprite-mcp (Divyansh Singh, MIT 2024) — the v4 expansion (layers, cels,
  slices, tilemaps, analysis, export, `ase_run_lua`) and shared Lua idioms.

See `README.md` → Acknowledgements for the full notices.
