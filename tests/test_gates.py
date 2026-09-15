r"""Headless gate/wrapper unit battery - no Aseprite, no Krita, no RD backend.

Covers the tool-dispatch layer that sits between FastMCP and the subsystem
implementations: argument binding, parameter aliasing, sprite existence
pre-checks, gate coverage and Aseprite executable discovery.

Run: .venv\Scripts\python tests\test_gates.py
"""
import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as S  # noqa: E402

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}  {detail}")


def section(title):
    print(f"\n== {title} ==")


def structured_error(res):
    """True when res is a dict describing a rejected call rather than a result."""
    return isinstance(res, dict) and res.get("ok") is False and "error" in res


# Tools are wrapped at import time; grab the live wrapped callables.
TOOLS = {t.name: t for t in asyncio.run(S.mcp.list_tools())}


def call(name, *args, **kwargs):
    """Invoke a wrapped tool, awaiting it when the underlying tool is async."""
    res = TOOLS[name].fn(*args, **kwargs)
    if inspect.isawaitable(res):
        res = asyncio.run(res)
    return res


# ---------------------------------------------------------------------------
section("1. unknown keyword arguments are rejected uniformly (not raised)")
# ---------------------------------------------------------------------------
# An agent calling a tool with a wrong parameter name must get an actionable
# structured error naming the accepted parameters - for EVERY subsystem, not
# just Aseprite. Today only ase_* has this guard.

r = call("ase_create_canvas", nonsense_param=1, width=8, height=8)
check("ase: unknown kwarg -> structured error", structured_error(r), repr(r)[:200])
check("ase: error lists accepted_params",
      isinstance(r, dict) and "width" in (r.get("accepted_params") or []), repr(r)[:200])
check("ase: error names the offending parameter",
      isinstance(r, dict) and "nonsense_param" in (r.get("unexpected_params") or []), repr(r)[:200])

try:
    r = call("krita_set_pixels", nonsense_param=1)
    ok = structured_error(r)
    detail = repr(r)[:200]
except TypeError as exc:
    ok, detail = False, f"raised TypeError: {exc}"
check("krita: unknown kwarg -> structured error", ok, detail)

try:
    r = call("rd_generate", nonsense_param=1)
    ok = structured_error(r)
    detail = repr(r)[:200]
except TypeError as exc:
    ok, detail = False, f"raised TypeError: {exc}"
check("rd: unknown kwarg -> structured error", ok, detail)
check("rd: argument check precedes the backend probe",
      isinstance(r, dict) and r.get("ok") is False and "available" not in r, repr(r)[:200])


# ---------------------------------------------------------------------------
section("2. binding errors are detected WITHOUT masking real bugs")
# ---------------------------------------------------------------------------
# The guard must distinguish "caller passed the wrong parameter name" from
# "the tool body itself raised TypeError". Swallowing the latter would hide
# genuine defects behind a fake 'call it differently' hint.

def _sample(sprite_path, frame=1):
    raise TypeError("internal boom: unsupported operand")

check("binding check helper exists", hasattr(S, "_binding_error"),
      "expected S._binding_error(fn, args, kwargs)")

if hasattr(S, "_binding_error"):
    err = S._binding_error(_sample, (), {"nope": 1})
    check("binding: bad kwarg detected", isinstance(err, dict) and "nope" in json.dumps(err), repr(err)[:200])
    check("binding: accepted_params reported",
          isinstance(err, dict) and "sprite_path" in (err.get("accepted_params") or []), repr(err)[:200])
    ok_bind = S._binding_error(_sample, ("a.aseprite",), {})
    check("binding: valid call returns None", ok_bind is None, repr(ok_bind)[:200])

if hasattr(S, "_call_guarded"):
    here = str(Path(__file__).resolve())  # a path that exists, so the sprite check passes
    raised = False
    try:
        S._call_guarded(_sample, (here,), {}, "sample")
    except TypeError as exc:
        raised = "internal boom" in str(exc)
    check("body TypeError propagates (not masked as a binding hint)", raised,
          "guard swallowed a TypeError raised inside the tool body")

    # Ordering: a bad parameter name is reported even when the subsystem is down,
    # because it is a caller error either way.
    def _gate_down():
        return {"available": False, "subsystem": "test"}
    res = S._call_guarded(_sample, (), {"nope": 1}, "sample", gate=_gate_down)
    check("binding error outranks a closed gate", structured_error(res), repr(res)[:200])

    # But a missing sprite must not mask an unavailable subsystem: fix the
    # subsystem first, otherwise the path complaint is a red herring.
    res = S._call_guarded(_sample, ("no_such_file.aseprite",), {}, "sample", gate=_gate_down)
    check("closed gate outranks a missing sprite",
          isinstance(res, dict) and res.get("available") is False, repr(res)[:200])
else:
    check("call guard helper exists", False, "expected S._call_guarded(fn, args, kwargs, name)")


# ---------------------------------------------------------------------------
section("3. missing sprite files fail cleanly before launching Aseprite")
# ---------------------------------------------------------------------------
# A wrong path must not reach the Lua layer, where app.open() returns nil and
# the script dies with 'attempt to index a nil value (local spr)' and an
# unsigned exit code.

GHOST = str(Path(S.OUT_DIR) / "definitely_not_here_xyz.aseprite")
if Path(GHOST).exists():
    Path(GHOST).unlink()

for tool, kwargs in [
    ("ase_draw_pixels_hex", {"pixels": [{"x": 0, "y": 0, "color": "#ff0000"}]}),
    ("ase_sprite_info", {}),
    ("ase_view_frame", {}),
]:
    try:
        r = call(tool, sprite_path=GHOST, **kwargs)
        ok = structured_error(r) and "not found" in json.dumps(r).lower()
        detail = repr(r)[:200]
    except Exception as exc:
        ok, detail = False, f"raised {type(exc).__name__}: {str(exc)[:160]}"
    check(f"{tool}: missing sprite -> structured error", ok, detail)

# Positional calls must be checked too - sprite_path is positional-first on 85 tools.
try:
    r = call("ase_sprite_info", GHOST)
    ok = structured_error(r)
    detail = repr(r)[:200]
except Exception as exc:
    ok, detail = False, f"raised {type(exc).__name__}: {str(exc)[:160]}"
check("positional sprite_path is checked too", ok, detail)

# The invariant that makes the check safe: no tool uses sprite_path for a file
# it is supposed to CREATE. ase_create_canvas writes to out_path instead.
creators = []
for name in dir(S):
    if not name.startswith("ase_"):
        continue
    fn = getattr(S, name, None)
    if not callable(fn):
        continue
    try:
        params = inspect.signature(fn).parameters
        src = inspect.getsource(fn)
    except (TypeError, ValueError, OSError):
        continue
    if "sprite_path" in params and "Sprite(" in src and "app.open(" not in src:
        creators.append(name)
check("invariant: no ase tool creates its sprite_path", not creators, f"creators={creators}")


# ---------------------------------------------------------------------------
section("4. parameter aliases: helpful where unambiguous, silent never")
# ---------------------------------------------------------------------------
check("alias resolver exists", hasattr(S, "_resolve_aliases"),
      "expected S._resolve_aliases(params, kwargs)")

if hasattr(S, "_resolve_aliases"):
    # Existing behaviour that must keep working.
    out = S._resolve_aliases({"sprite_path", "layer", "frame"}, {"layer_name": "fx"})
    check("alias: layer_name -> layer", out.get("layer") == "fx", repr(out))

    # A tool with exactly one path parameter can accept common synonyms.
    out = S._resolve_aliases({"width", "height", "color_mode", "out_path"}, {"sprite_path": "s.aseprite"})
    check("alias: sprite_path -> out_path when unambiguous", out.get("out_path") == "s.aseprite", repr(out))

    out = S._resolve_aliases({"image_path", "x", "y", "layer"}, {"path": "img.png"})
    check("alias: path -> image_path when unambiguous", out.get("image_path") == "img.png", repr(out))

    # Ambiguity guard: two path params means we must NOT guess.
    out = S._resolve_aliases({"sprite_path", "out_path"}, {"path": "a.aseprite"})
    check("alias: refuses to guess with two path params",
          "path" in out and "out_path" not in out and out.get("sprite_path") is None, repr(out))

    # Never clobber an explicit value.
    out = S._resolve_aliases({"sprite_path", "layer"}, {"layer": "real", "layer_name": "alias"})
    check("alias: explicit value wins", out.get("layer") == "real", repr(out))

    # 'scale' is a real parameter elsewhere but means something different from
    # max_side; guessing would silently produce wrong output sizes.
    out = S._resolve_aliases({"sprite_path", "max_side"}, {"scale": 2})
    check("alias: does not map scale -> max_side", "max_side" not in out, repr(out))

    # image_path (a raster file) and sprite_path (an .aseprite document) name
    # different kinds of file. Drawing tools save in place, so rewriting one
    # into the other could paint over a PNG the caller meant to import.
    out = S._resolve_aliases({"sprite_path", "pixels", "layer"}, {"image_path": "art.png"})
    check("alias: does not map image_path -> sprite_path", "sprite_path" not in out, repr(out))
    out = S._resolve_aliases({"image_path", "x", "y"}, {"sprite_path": "s.aseprite"})
    check("alias: does not map sprite_path -> image_path", "image_path" not in out, repr(out))

    # Generic names carry no file-kind meaning, so they stay resolvable.
    out = S._resolve_aliases({"sprite_path", "pixels"}, {"path": "s.aseprite"})
    check("alias: generic path still resolves to sprite_path", out.get("sprite_path") == "s.aseprite", repr(out))

# ase_view_frame(scale=...) should therefore be a clean rejection, not a crash.
r = call("ase_view_frame", sprite_path=GHOST, scale=2)
check("ase_view_frame: scale rejected with guidance",
      structured_error(r) and "max_side" in json.dumps(r), repr(r)[:200])


# ---------------------------------------------------------------------------
section("5. every tool that touches a subsystem is gated")
# ---------------------------------------------------------------------------
# An ungated tool that shells out raises RuntimeError instead of returning the
# friendly {'available': False, ...} payload when the subsystem is absent.
# ase_status is the deliberate exception: it is the availability probe itself,
# so gating it would be circular - it must run precisely when Aseprite is gone.
PROBES = {"ase_status"}
SHELLING = []
for name in TOOLS:
    if not name.startswith("ase_"):
        continue
    fn = getattr(S, name, None)
    if not callable(fn):
        continue
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        continue
    touches = any(tok in src for tok in ("_run_aseprite_lua", "_dump_cel", "_draw_pixels", "ASEPRITE_EXE"))
    if touches and name not in S._ASE_GATED and name not in PROBES:
        SHELLING.append(name)
check("no ungated ase tool reaches the Aseprite CLI", not SHELLING, f"ungated: {SHELLING}")

check("krita gate covers every krita tool except status",
      {n for n in TOOLS if n.startswith("krita_")} - S._KRITA_GATED == {"krita_status"},
      str({n for n in TOOLS if n.startswith("krita_")} - S._KRITA_GATED))


# ---------------------------------------------------------------------------
section("6. Aseprite discovery finds real-world installs")
# ---------------------------------------------------------------------------
# Aseprite is commonly installed outside the system drive (a 'Program Files'
# on D:, a Steam library on another disk). Probing only %ProgramFiles% on C:
# reports 'not installed' on machines where it plainly is.
check("candidate enumerator exists", hasattr(S, "_aseprite_candidates"),
      "expected S._aseprite_candidates() -> list[Path]")

if hasattr(S, "_aseprite_candidates"):
    cands = [str(c) for c in S._aseprite_candidates()]
    check("candidates is a non-empty list", bool(cands), repr(cands[:5]))
    check("candidates are de-duplicated", len(cands) == len(set(cands)),
          f"{len(cands)} entries, {len(set(cands))} unique")
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        check("keeps the classic %ProgramFiles% location",
              any(str(Path(pf) / "Aseprite" / "Aseprite.exe") == c for c in cands), repr(cands[:8]))
        drives = {c[:1].upper() for c in cands if len(c) > 1 and c[1] == ":"}
        check("probes more than the system drive", len(drives) > 1, f"drives={sorted(drives)}")

if sys.platform == "win32" and hasattr(S, "_registry_aseprite_paths"):
    reg = [str(p) for p in S._registry_aseprite_paths()]
    print(f"     (registry reports: {reg})")
    check("registry lookup returns a list", isinstance(reg, list), repr(reg))
else:
    check("registry lookup helper exists (win32)", sys.platform != "win32",
          "expected S._registry_aseprite_paths()")

# Steam installs stay supported: library roots come from libraryfolders.vdf.
check("steam library parser exists", hasattr(S, "_steam_library_paths"),
      "expected S._steam_library_paths(text)")
if hasattr(S, "_steam_library_paths"):
    vdf = '''"libraryfolders"
{
	"0"
	{
		"path"		"D:\\\\Program Files\\\\Steam"
	}
	"1"
	{
		"path"		"C:\\\\SteamLibrary"
	}
}'''
    got = [str(p) for p in S._steam_library_paths(vdf)]
    check("steam: parses every library root",
          got == [r"D:\Program Files\Steam", r"C:\SteamLibrary"], repr(got))
    check("steam: tolerates malformed vdf", isinstance(S._steam_library_paths("not a vdf"), list))

# The resolved executable must actually be found on a machine where Aseprite
# is installed - this is the user-visible symptom.
print(f"     (resolved ASEPRITE_EXE: {S.ASEPRITE_EXE})")
if os.environ.get("PIXELWORKS_EXPECT_ASEPRITE"):
    check("resolves to an existing executable", Path(S.ASEPRITE_EXE).exists(), str(S.ASEPRITE_EXE))

# Aseprite can be installed anywhere - a portable copy, a custom folder, an
# unusual drive. No candidate list can enumerate every path, so when discovery
# fails the message must show WHERE it looked, instead of naming a single
# guessed path the user never chose.
_saved_exe, _saved_cache = S.ASEPRITE_EXE, S._ASE_CACHE.copy()
try:
    S.ASEPRITE_EXE = Path(r"Z:\nowhere\Aseprite.exe")
    S._ASE_CACHE.update(ts=0.0, ok=None, detail="")
    st = S.ase_status()
    check("not-found: still reports unavailable", st.get("available") is False, repr(st)[:200])
    check("not-found: names the env var to set", "ASEPRITE_EXE" in json.dumps(st), repr(st)[:300])
    check("not-found: reports where it searched", bool(st.get("searched")), repr(st)[:300])
    check("not-found: lists several probed locations",
          len(st.get("searched") or []) > 1, repr(st.get("searched"))[:300])
finally:
    S.ASEPRITE_EXE = _saved_exe
    S._ASE_CACHE.clear()
    S._ASE_CACHE.update(_saved_cache)


# ---------------------------------------------------------------------------
section("7. gate payloads stay stable when a subsystem is absent")
# ---------------------------------------------------------------------------
_saved = S._ASE_CACHE.copy()
S._ASE_CACHE.update(ts=9e18, ok=False, detail="forced: not installed")
try:
    # Uses a real file so the sprite pre-check cannot pre-empt the gate.
    real = str(Path(__file__).resolve())
    r = call("ase_sprite_info", sprite_path=real)
    check("absent subsystem -> available:false payload",
          isinstance(r, dict) and r.get("available") is False, repr(r)[:200])
    check("absent subsystem payload names the subsystem",
          isinstance(r, dict) and r.get("subsystem") == "aseprite", repr(r)[:200])
finally:
    S._ASE_CACHE.clear()
    S._ASE_CACHE.update(_saved)


# ---------------------------------------------------------------------------
section("8. lifecycle tools invalidate the availability cache")
# ---------------------------------------------------------------------------
# Availability is cached (30s for RD, 60s for Aseprite) so the gate does not
# reconnect on every call. Starting or stopping a backend changes that answer
# immediately, so those tools must clear the cache - otherwise the gate waves
# a call through to a socket that is already gone and the caller gets a raw
# connection error instead of the friendly {"available": false, ...} payload.
check("rd cache invalidator exists", hasattr(S, "_invalidate_rd_cache"),
      "expected S._invalidate_rd_cache()")

if hasattr(S, "_invalidate_rd_cache"):
    S._RD_CACHE.update(ts=time.time(), ok=True, detail="stale: backend was up")
    S._invalidate_rd_cache()
    check("rd invalidation forces a fresh probe", S._RD_CACHE.get("ok") is None,
          repr(dict(S._RD_CACHE))[:200])

# The lifecycle tools must actually call it, or the cache goes stale in exactly
# the situation the user hits: stop the backend, then make a call.
for tool_name in ("rd_start_backend", "rd_stop_backend"):
    fn = getattr(S, tool_name, None)
    src = inspect.getsource(fn) if fn else ""
    check(f"{tool_name} invalidates the rd cache", "_invalidate_rd_cache" in src,
          f"{tool_name} leaves a stale reading for up to 30s")


print(f"\n{PASSED} passed, {len(FAILED)} failed")
if FAILED:
    print("FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL GREEN")
