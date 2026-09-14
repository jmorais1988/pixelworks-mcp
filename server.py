#!/usr/bin/env python
"""pixelworks - MCP server for pixel-art toolchains: a full-coverage bridge to
the Retro Diffusion local backend (the one inside the Retro Diffusion Aseprite
extension), the Aseprite headless-CLI workbench (ase_*) and the Krita bridge
plugin (krita_*). This header lists the Retro Diffusion coverage; see Readme.md
for the complete tool families.

Speaks the extension v15.0.0 WebSocket JSON protocol on ws://127.0.0.1:8765
(handshake version-pinned; one persistent socket per server process).

Feature coverage (UI parity, Aseprite-native items delegated to Aseprite MCP):
  Text to Image ............ rd_generate          (presets, sliders, modifiers,
                                                   tiling, palette-post, preview,
                                                   grid/frames output)
  Image to Image ........... rd_img2img
  Neural Transform ......... rd_neural_transform
  Neural Pixelate .......... rd_neural_pixelate
  Neural Resize ............ rd_neural_resize
  Neural Detail ............ rd_neural_detail
  CN Text to Image ......... rd_cn_txt2img       (5 ControlNets + palettes)
  CN Image to Image ........ rd_cn_img2img
  API txt2img / img2img .... rd_api_txt2img / rd_api_img2img   (retrodiffusion.ai)
  API txt2anim / img2anim .. rd_api_txt2anim / rd_api_img2anim
  Remove Background ........ rd_rembg
  Generate Textures ........ rd_texture_gen      (PBR maps, tiling modes)
  Palettize ................ rd_palettize        (dithering, palette sources)
  Color Style Transfer ..... rd_color_transfer
  Palette Generation ....... rd_palette          (txt2pal model)
  K-Centroid Resize ........ rd_kcentroid
  Prompt Extract ........... rd_prompt_extract
  Translate ................ rd_translate
  Benchmark ................ rd_benchmark
  Palette quantize utility . rd_quantize         (Pillow, GBA-friendly)
  Presets (Apply/Save/...) . rd_preset_list/apply/save/delete
  Backend lifecycle ........ rd_status / rd_start_backend / rd_stop_backend
  LoRA & model catalog ..... rd_list_loras / rd_list_models
  Aseprite interconnect .... ase_import_layer / ase_add_frames
  Aseprite-native operations (color mode, sprite/canvas size, rotate/flip,
  crop, trim, ...) are covered by the ase_* workbench tools below.

Headless guarantees: CREATE_NO_WINDOW + SW_HIDE on every child, RD_MCP_HEADLESS
env disables the extension's console-flashing shell-outs in batch Aseprite,
UTF-8 IO env for the backend, stdin=DEVNULL, logs to mcp-backend.log.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import websockets
from PIL import Image

from fastmcp import FastMCP
from fastmcp.utilities.types import Image as MCPImage

import httpx

mcp = FastMCP("pixelworks")

# ---------------------------------------------------------------------------
# configuration (env-overridable)
# ---------------------------------------------------------------------------
RD_URL = os.environ.get("RD_WS_URL", "ws://127.0.0.1:8765")
RD_VERSION = "15.0.0"


def _default_ext_dir() -> Path:
    """Where Aseprite installs the Retro Diffusion extension by default."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "Aseprite" / "extensions" / "RetroDiffusion"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Aseprite" / "extensions" / "RetroDiffusion"
    return Path.home() / ".config" / "aseprite" / "extensions" / "RetroDiffusion"


def _default_aseprite_exe() -> Path:
    """First existing Aseprite executable among the usual install locations
    (falls back to the first candidate so error messages stay meaningful)."""
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        candidates = [
            Path(pf) / "Aseprite" / "Aseprite.exe",
            Path(pf86) / "Steam" / "steamapps" / "common" / "Aseprite" / "Aseprite.exe",
            Path(pf86) / "Aseprite" / "Aseprite.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [Path("/Applications/Aseprite.app/Contents/MacOS/aseprite")]
    else:
        candidates = [Path("/usr/bin/aseprite"), Path("/usr/local/bin/aseprite")]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


EXT_DIR = Path(os.environ.get("RD_EXTENSION_DIR") or _default_ext_dir())
SD_DIR = EXT_DIR / "stable-diffusion-aseprite"
MODEL_DIR = SD_DIR / "models" / "base"
LORA_DIR = SD_DIR / "models" / "lora"
VENV_PY = SD_DIR / "venv" / "Scripts" / "python.exe"
ASEPRITE_EXE = Path(os.environ.get("ASEPRITE_EXE") or _default_aseprite_exe())
OUT_DIR = Path(os.environ.get("RD_OUT_DIR", str(Path(__file__).parent / "output")))
PRESET_FILE = Path(os.environ.get("RD_PRESET_FILE", str(Path(__file__).parent / "presets.json")))
GEN_TIMEOUT = float(os.environ.get("RD_GEN_TIMEOUT", "900"))

DIM_PRESETS = {
    "16x16": (16, 16),
    "32x32": (32, 32),
    "64x64": (64, 64),
    "128x128": (128, 128),
    "160x144": (160, 144),
    "240x160": (240, 160),
}
DITHER_MAP = {"None": 0, "Bayer 2x2": 2, "Bayer 4x4": 4, "Bayer 8x8": 8}
CONTROLNET_MODELS = ["Composition", "Depth", "Pose", "Sketch", "Tile"]
TEXTURE_TILING = ["none", "seamless", "mirror", "replicate"]

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _hide_startup() -> Dict[str, Any]:
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si}


LORA_ALIASES = {
    "game boy": "gameboy.pxlm", "gameboy": "gameboy.pxlm",
    "game boy advance": "gameboyadvance.pxlm", "gba": "gameboyadvance.pxlm",
    "snes": "snes.pxlm", "nes": "nes.pxlm",
    "sega genesis": "segagenesis.pxlm", "segagenesis": "segagenesis.pxlm",
    "neo-geo": "neogeo.pxlm", "neogeo": "neogeo.pxlm",
    "playstation": "playstation.pxlm", "modern": "modern.pxlm",
    "1bit": "1bit.pxlm", "1-bit": "1bit.pxlm",
    "flatshading": "flatshading.pxlm", "flat": "flatshading.pxlm",
    "gamecharacters": "gamecharacters.pxlm", "game characters": "gamecharacters.pxlm",
    "gamecharactersanime": "gamecharactersanime.pxlm",
    "gamecharactersretro": "gamecharactersretro.pxlm",
    "gameicons": "gameicons.pxlm", "game icons": "gameicons.pxlm",
    "industrial": "industrial.pxlm", "isometric": "isometric.pxlm",
    "tiling": "tiling.pxlm", "tiling16": "tiling16.pxlm", "tiling32": "tiling32.pxlm",
    "topdown": "topdown.pxlm", "top down": "topdown.pxlm",
    "uipanel": "uipanel.pxlm", "ui panel": "uipanel.pxlm",
    "simplegeometric": "simplegeometric.pxlm", "nashorkimitems": "nashorkimitems.pxlm",
}


def _resolve_lora(spec: Dict[str, Any]) -> Dict[str, Any]:
    weight = int(spec.get("weight", 80))
    if spec.get("file"):
        f = Path(spec["file"])
        if not f.is_absolute():
            f = LORA_DIR / f
    else:
        key = str(spec.get("name", "")).strip().lower()
        fname = LORA_ALIASES.get(key)
        if fname is None:
            cand = [p.name for p in LORA_DIR.glob("*.pxlm")]
            match = [c for c in cand if c.lower().startswith(key)] if key else []
            if not match:
                raise ValueError(f"unknown lora {spec.get('name')!r}; available: {cand}")
            fname = sorted(match)[0]
        f = LORA_DIR / fname
    if not f.exists():
        raise ValueError(f"lora file missing: {f}")
    return {"file": str(f), "weight": weight}


def _model_block(model_file: str = "model.pxlm") -> Dict[str, Any]:
    f = MODEL_DIR / model_file
    if not f.exists():
        raise ValueError(f"model missing: {f}")
    return {"file": str(f), "device": "cuda", "precision": "fp16", "optimized": False}


def _clamp(v: float, lo: float, hi: float) -> float:
    return sorted((lo, v, hi))[1]


def _seed(seed: Optional[int]) -> int:
    # only txt2img/img2img convert None to a random seed server-side; the
    # neural/ControlNet/palette paths call torch.manual_seed(seed) directly
    return int(seed) if seed is not None else random.randint(0, 999999)


def _safe_prefix(prompt: str) -> str:
    p = re.sub(r"[^a-z0-9]+", "_", prompt.lower()).strip("_")[:40] or "gen"
    return f"{time.strftime('%Y%m%d-%H%M%S')}_{p}"


def _decode_entry(entry: Dict[str, Any]) -> Image.Image:
    raw = base64.b64decode(entry["image"])
    if entry.get("format") == "png":
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    return Image.frombytes("RGBA", (int(entry["width"]), int(entry["height"])), raw)


def _encode_entry(path: str, name: Optional[str] = None, max_side: Optional[int] = None) -> Dict[str, Any]:
    pil = Image.open(path).convert("RGBA")
    if max_side and max(pil.size) > max_side:
        scale = max_side / max(pil.size)
        pil = pil.resize((max(1, round(pil.width * scale)), max(1, round(pil.height * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return {
        "name": name or Path(path).stem,
        "format": "png",
        "image": base64.b64encode(buf.getvalue()).decode(),
        "width": pil.width,
        "height": pil.height,
    }


def _palette_block(palette_files: Optional[List[str]], palette_url: Optional[str]) -> Tuple[str, str, List[Dict[str, Any]]]:
    if palette_files:
        palettes = [_encode_entry(p, name=Path(p).name) for p in palette_files]
        return "File", "None", palettes
    if palette_url:
        return "URL", palette_url, []
    return "None", "None", []


def _api_key(explicit: Optional[str]) -> str:
    key = explicit or os.environ.get("RD_API_KEY", "")
    if not key:
        try:
            key = json.loads((EXT_DIR / "data" / "settings.json").read_text(encoding="utf-8")).get("rdapikey", "")
        except Exception:
            key = ""
    if not key:
        raise RuntimeError(
            "no Retro Diffusion API key: pass api_key, set RD_API_KEY, or enter it in the "
            "Aseprite Retro Diffusion dialog (settings rdapikey)"
        )
    return key


# ---------------------------------------------------------------------------
# persistent websocket client
# ---------------------------------------------------------------------------
class _PersistentRD:
    def __init__(self) -> None:
        self._ws: Optional[Any] = None
        self._alive = False
        self._lock = asyncio.Lock()

    async def _connect_handshake(self) -> Any:
        ws = await websockets.connect(RD_URL, max_size=300 * 1024 * 1024, ping_interval=None, close_timeout=5)
        try:
            await ws.send(
                json.dumps(
                    {
                        "action": "connected",
                        "type": "dictionary",
                        "value": {"background": True, "play_sound": False, "version": RD_VERSION},
                    }
                )
            )
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                if msg.get("action") == "connected":
                    return ws
                if msg.get("action") == "error":
                    raise RuntimeError("RD backend rejected the handshake")
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass
            raise

    async def call(
        self, packet: Dict[str, Any], timeout: float = GEN_TIMEOUT, collect_progress: bool = False
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        async with self._lock:
            last_err: Optional[Exception] = None
            for attempt in (0, 1):
                if self._ws is None or not self._alive:
                    self._ws = await self._connect_handshake()
                    self._alive = True
                ws = self._ws
                previews: List[Dict[str, Any]] = []
                try:
                    await ws.send(json.dumps(packet))
                    result: Optional[Dict[str, Any]] = None
                    while True:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                        action = msg.get("action")
                        if action == "returning":
                            result = msg
                            break
                        if action == "error":
                            raise RuntimeError("RD backend reported an error (see mcp-backend.log)")
                        if action == "display_image" and collect_progress:
                            previews.extend((msg.get("value") or {}).get("images") or [])
                    try:
                        await ws.send(json.dumps({"action": "recieved"}))
                    except Exception:
                        pass
                    return (result or {}, previews)
                except (websockets.ConnectionClosed, asyncio.IncompleteReadError, ConnectionError, OSError) as e:
                    last_err = e
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    self._ws = None
                    self._alive = False
                    if attempt == 1:
                        break
            raise RuntimeError(f"RD backend connection failed: {last_err}")


RD_CONN = _PersistentRD()


async def _rd_call(packet: Dict[str, Any], timeout: float = GEN_TIMEOUT, collect_progress: bool = False):
    return await RD_CONN.call(packet, timeout, collect_progress)


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------
def _save_images(images: List[Dict[str, Any]], prefix: str) -> List[Dict[str, Any]]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, entry in enumerate(images):
        pil = _decode_entry(entry)
        seed = entry.get("seed")
        name = f"{prefix}_{i}" + (f"_seed{seed}" if seed is not None else "") + ".png"
        path = OUT_DIR / name
        pil.save(path)
        saved.append({"path": str(path), "width": pil.width, "height": pil.height, "seed": seed, "name": entry.get("name")})
    return saved


def _save_previews(previews: List[Dict[str, Any]], prefix: str) -> List[str]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, entry in enumerate(previews):
        try:
            pil = _decode_entry(entry)
        except Exception:
            continue
        p = OUT_DIR / f"{prefix}_preview_{i:02d}.png"
        pil.save(p)
        paths.append(str(p))
    return paths


def _save_grid(saved: List[Dict[str, Any]], prefix: str) -> Optional[str]:
    if len(saved) < 2:
        return None
    imgs = [Image.open(s["path"]) for s in saved]
    w = max(i.width for i in imgs)
    h = max(i.height for i in imgs)
    cols = math.ceil(math.sqrt(len(imgs)))
    rows = math.ceil(len(imgs) / cols)
    grid = Image.new("RGBA", (cols * w, rows * h), (0, 0, 0, 0))
    for i, im in enumerate(imgs):
        grid.paste(im, ((i % cols) * w, (i // cols) * h), im)
    p = OUT_DIR / f"{prefix}_grid.png"
    grid.save(p)
    return str(p)


def _save_frames(images: List[Dict[str, Any]], prefix: str) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, entry in enumerate(images):
        pil = _decode_entry(entry)
        p = OUT_DIR / f"{prefix}_frame_{i:03d}.png"
        pil.save(p)
        frames.append(str(p))
    gif = None
    if frames:
        pil_frames = [Image.open(f).convert("RGBA") for f in frames]
        gif = str(OUT_DIR / f"{prefix}_anim.gif")
        pil_frames[0].save(gif, save_all=True, append_images=pil_frames[1:], duration=150, loop=0, disposal=2)
    return {"frames": frames, "gif": gif}


# ---------------------------------------------------------------------------
# packet builders
# ---------------------------------------------------------------------------
def _dims(width: int, height: int, pixel_size: int, size_preset: Optional[str], swap: bool) -> Tuple[int, int]:
    # size presets denote OUTPUT pixel dimensions (like the UI's sprite-size
    # presets); width/height without a preset denote canvas dimensions.
    if size_preset:
        if size_preset not in DIM_PRESETS:
            raise ValueError(f"unknown size preset {size_preset!r}; options: {sorted(DIM_PRESETS)}")
        w, h = DIM_PRESETS[size_preset]
        if swap:
            w, h = h, w
        ps = max(1, int(pixel_size))
        return w * ps, h * ps
    return int(width), int(height)


def _core_value(
    prompt: str,
    negative: str,
    width: int,
    height: int,
    pixel_size: int,
    generations: int,
    seed: Optional[int],
    cfg_scale: float,
    loras: List[Dict[str, Any]],
    model: str,
    adherence: float,
    prompt_tuning: bool,
    use_ella: bool,
    tile_x: bool,
    tile_y: bool,
    pixelvae: bool,
    rembg: bool,
    post_process: bool,
    send_progress: bool,
    comp: Dict[str, float],
    light: Dict[str, Any],
    add_to_prompt: bool = False,
    quality: Optional[float] = None,
    steps: Optional[int] = None,
) -> Dict[str, Any]:
    # normalize onto RD's native 8px latent grid (small canvases otherwise cook
    # into under-denoised noise)
    out_w = max(16, int(width) // max(1, int(pixel_size)))
    out_h = max(16, int(height) // max(1, int(pixel_size)))
    value: Dict[str, Any] = {
        "prompt": prompt,
        "negative": negative,
        "use_ella": use_ella,
        "adherence": _clamp(adherence, 1, 5),
        "translate": bool(add_to_prompt),
        "prompt_tuning": prompt_tuning,
        "width": out_w * 8,
        "height": out_h * 8,
        "pixel_size": 8,
        "scale": float(cfg_scale),
        "lighting": dict(light),
        "composition": dict(comp),
        "seed": _seed(seed),
        "generations": int(generations),
        "max_batch_size": 512,
        "model": _model_block(model),
        "loras": [_resolve_lora(l) for l in (loras or [])],
        "image": "",
        "tile_x": bool(tile_x),
        "tile_y": bool(tile_y),
        "use_pixelvae": bool(pixelvae),
        "rembg": bool(rembg),
        "send_progress": bool(send_progress),
        "post_process": bool(post_process),
        "background": True,
        "play_sound": False,
    }
    if quality is not None:
        value["quality"] = float(_clamp(quality, 1, 7))
    if steps is not None:
        value["steps"] = int(_clamp(steps, 5, 50))
    return value


def _comp(hue: float, tint: float, brightness: float, saturation: float, contrast: float, outline: float) -> Dict[str, float]:
    return {
        "hue": hue,
        "tint": tint,
        "brightness": brightness,
        "saturation": saturation,
        "contrast": contrast,
        "outline": outline,
    }


def _light(apply: bool, x: float, y: float, z: float) -> Dict[str, Any]:
    return {"apply": bool(apply), "x": x, "y": y, "z": z}


def _cn_entries(controlnets: List[Dict[str, Any]], control_images: List[Optional[str]]) -> Tuple[List[Dict[str, Any]], List[Any]]:
    cn: List[Dict[str, Any]] = []
    imgs: List[Any] = []
    for i, spec in enumerate(controlnets):
        model = spec.get("model", "Composition")
        if model not in CONTROLNET_MODELS:
            raise ValueError(f"unknown controlnet {model!r}; options: {CONTROLNET_MODELS}")
        enabled = bool(spec.get("enabled", True))
        img_path = control_images[i] if i < len(control_images) else None
        if enabled and not img_path:
            # backend cnimg2img decodes enabled slots unguarded and crashes on "none"
            raise ValueError(f"controlnet {spec.get('model')} is enabled but no control image was provided")
        cn.append(
            {
                "enabled": enabled,
                "model": model,
                "process": bool(spec.get("process", True)),
                "weight": int(spec.get("weight", 80)),
            }
        )
        imgs.append(_encode_entry(img_path, name=model) if (enabled and img_path) else "none")
    return cn, imgs


async def _run(packet: Dict[str, Any], prefix: str, save_progress: bool, save_grid: bool, anim: bool = False) -> Dict[str, Any]:
    res, previews = await _rd_call(packet, collect_progress=save_progress)
    images = (res.get("value") or {}).get("images") or []
    if not images:
        raise RuntimeError("RD backend returned no images")
    out: Dict[str, Any] = {}
    if anim:
        out["animation"] = _save_frames(images, prefix)
    else:
        saved = _save_images(images, prefix)
        out["images"] = saved
        out["pixel_dims"] = [saved[0]["width"], saved[0]["height"]]
        if save_grid:
            out["grid"] = _save_grid(saved, prefix)
    if save_progress and previews:
        out["previews"] = _save_previews(previews, prefix)
    return out


# ---------------------------------------------------------------------------
# lifecycle / catalog tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def rd_status(handshake: bool = False) -> Dict[str, Any]:
    """Check whether the Retro Diffusion local backend is reachable on ws://127.0.0.1:8765.

    By default this only opens a socket (zero side effects). With handshake=True
    it also performs the version handshake, which makes the backend minimize its
    console window once - avoid unless diagnosing.
    """
    ok, detail = await _rd_available(force=True)
    try:
        async with websockets.connect(RD_URL, open_timeout=5, ping_interval=None) as ws:
            out: Dict[str, Any] = {
                "backend": "up" if ok else "down",
                "url": RD_URL,
                "version": RD_VERSION,
                "detail": detail,
                "handshake": "not performed (set handshake=true to verify)",
            }
            if handshake:
                await ws.send(
                    json.dumps(
                        {
                            "action": "connected",
                            "type": "dictionary",
                            "value": {"background": True, "play_sound": False, "version": RD_VERSION},
                        }
                    )
                )
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                out["handshake"] = "ok" if msg.get("action") == "connected" else f"unexpected reply: {msg}"
            return out
    except Exception as e:
        return {"backend": "down", "error": str(e), "url": RD_URL, "version": RD_VERSION}


@mcp.tool()
async def rd_start_backend() -> Dict[str, Any]:
    """Start the Retro Diffusion local backend (image_server.py in the extension venv) if not already listening.

    Headless: no console window, UTF-8 IO env, logs to mcp-backend.log.
    """
    st = await rd_status()
    if st.get("backend") == "up":
        return {"started": False, "reason": "already running", **st}
    if not VENV_PY.exists():
        return {"started": False, "error": f"venv python not found at {VENV_PY}; run RD setup in Aseprite first"}
    log_path = SD_DIR / "mcp-backend.log"
    logf = open(log_path, "ab", buffering=0)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["NO_COLOR"] = "1"
    subprocess.Popen(
        [str(VENV_PY), "-u", "scripts/image_server.py"],
        cwd=str(SD_DIR),
        creationflags=_CREATE_NO_WINDOW,
        stdout=logf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        **_hide_startup(),
    )
    for _ in range(120):
        await asyncio.sleep(2)
        st = await rd_status()
        if st.get("backend") == "up":
            return {"started": True, "log": str(log_path), **st}
    return {"started": False, "error": "backend did not come up within 240s", "log": str(log_path)}


@mcp.tool()
async def rd_stop_backend() -> Dict[str, Any]:
    """Ask the Retro Diffusion backend to shut down (also closes its console window loop)."""
    try:
        async with websockets.connect(RD_URL, open_timeout=5, ping_interval=None) as ws:
            await ws.send(json.dumps({"action": "shutdown"}))
        return {"stopped": True}
    except Exception as e:
        return {"stopped": False, "error": str(e)}


@mcp.tool()
async def rd_list_loras() -> List[Dict[str, Any]]:
    """List available Retro Diffusion style LoRAs (.pxlm) with their alias keys."""
    inv: Dict[str, List[str]] = {}
    for alias, fname in LORA_ALIASES.items():
        inv.setdefault(fname, []).append(alias)
    out = []
    for p in sorted(LORA_DIR.glob("*.pxlm")):
        out.append({"file": p.name, "aliases": inv.get(p.name, []), "mb": round(p.stat().st_size / 1e6, 1)})
    return out


@mcp.tool()
async def rd_list_models() -> Dict[str, Any]:
    """List selectable base models, ControlNet models, texture tiling modes and dimension presets."""
    return {
        "models": [p.name for p in sorted(MODEL_DIR.glob("*.pxlm")) if p.name in ("model.pxlm", "modelmicro.pxlm")],
        "controlnets": CONTROLNET_MODELS,
        "texture_tiling": TEXTURE_TILING,
        "dim_presets": {k: list(v) for k, v in DIM_PRESETS.items()},
        "dither_matrices": sorted(DITHER_MAP),
    }


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------
def _presets_load() -> Dict[str, Any]:
    if PRESET_FILE.exists():
        try:
            return json.loads(PRESET_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _presets_save(data: Dict[str, Any]) -> None:
    PRESET_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


@mcp.tool()
def rd_preset_save(name: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Save a named generation preset (any rd_generate kwargs) to presets.json."""
    data = _presets_load()
    data[name] = settings
    _presets_save(data)
    return {"saved": name, "total": len(data)}


@mcp.tool()
def rd_preset_list() -> Dict[str, Any]:
    """List saved generation presets."""
    return _presets_load()


@mcp.tool()
def rd_preset_apply(name: str) -> Dict[str, Any]:
    """Return a saved preset's settings so they can be passed to rd_generate."""
    data = _presets_load()
    if name not in data:
        raise ValueError(f"unknown preset {name!r}; have: {sorted(data)}")
    return data[name]


@mcp.tool()
def rd_preset_delete(name: str) -> Dict[str, Any]:
    """Delete a saved preset."""
    data = _presets_load()
    data.pop(name, None)
    _presets_save(data)
    return {"deleted": name, "total": len(data)}


# ---------------------------------------------------------------------------
# generation tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def rd_generate(
    prompt: str = "",
    negative: str = "",
    preset: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    size_preset: Optional[str] = None,
    swap: bool = False,
    width: int = 512,
    height: int = 512,
    pixel_size: int = 8,
    generations: int = 1,
    seed: Optional[int] = None,
    quality: float = 4.0,
    cfg_scale: float = 5.0,
    adherence: float = 5.0,
    prompt_tuning: bool = True,
    add_to_prompt: bool = False,
    use_ella: bool = True,
    loras: Optional[List[Dict[str, Any]]] = None,
    model: str = "model.pxlm",
    tile_x: bool = False,
    tile_y: bool = False,
    pixelvae: bool = False,
    rembg: bool = False,
    post_process: bool = False,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
    comp_hue: float = 0,
    comp_tint: float = 0,
    comp_brightness: float = 70,
    comp_saturation: float = 50,
    comp_contrast: float = 50,
    comp_outline: float = 50,
    light_apply: bool = False,
    light_x: float = 25,
    light_y: float = 0,
    light_z: float = 0,
) -> Any:
    """Text to Image with the local Retro Diffusion pixel-art model.

    Output pixels = width//pixel_size (or a size_preset like 64x64/160x144,
    swap=True transposes). Internally normalized onto RD's 8px latent grid so
    small sprites never degrade. quality ~1.6-6 drives steps; cfg_scale is the
    prompt scale; adherence 0-5 trades ELLA/CLIP guidance. Modifiers: comp_*
    (hue/tint/brightness/saturation/contrast/outline sliders), light_*
    (directional lighting leco). Rendering options: pixelvae (fast pixel
    decoder), rembg, post_process (auto reduce colors), tile_x/tile_y.
    Display: save_grid composes one grid PNG; save_progress stores intermediate
    previews. preset + overrides replays a saved preset (other args ignored).
    """
    if preset:
        base = dict(rd_preset_apply(preset))
        if prompt:
            base["prompt"] = prompt  # explicit arg beats preset
        base.update(overrides or {})  # overrides beat everything
        base.pop("preset", None)
        base.pop("overrides", None)
        p = base.pop("prompt", "")
        if not p:
            raise ValueError("preset has no stored prompt and none was given")
        return await rd_generate(prompt=p, **base)
    if not prompt:
        raise ValueError("prompt is required (or pass a preset)")
    w, h = _dims(width, height, pixel_size, size_preset, swap)
    value = _core_value(
        prompt, negative, w, h, pixel_size, generations, seed, cfg_scale, loras or [], model,
        adherence, prompt_tuning, use_ella, tile_x, tile_y, pixelvae, rembg, post_process,
        save_progress,
        _comp(comp_hue, comp_tint, comp_brightness, comp_saturation, comp_contrast, comp_outline),
        _light(light_apply, light_x, light_y, light_z),
        add_to_prompt=add_to_prompt,
        quality=quality,
    )
    return await _run({"action": "txt2img", "value": value}, _safe_prefix(prompt), save_progress, save_grid)


@mcp.tool()
async def rd_img2img(
    image_paths: List[str],
    prompt: str,
    negative: str = "",
    strength: int = 50,
    size_preset: Optional[str] = None,
    swap: bool = False,
    width: int = 512,
    height: int = 512,
    pixel_size: int = 8,
    generations: int = 1,
    seed: Optional[int] = None,
    quality: float = 4.0,
    cfg_scale: float = 5.0,
    adherence: float = 5.0,
    prompt_tuning: bool = True,
    add_to_prompt: bool = False,
    use_ella: bool = True,
    loras: Optional[List[Dict[str, Any]]] = None,
    model: str = "model.pxlm",
    tile_x: bool = False,
    tile_y: bool = False,
    pixelvae: bool = False,
    rembg: bool = False,
    post_process: bool = False,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
    comp_hue: float = 0,
    comp_tint: float = 0,
    comp_brightness: float = 70,
    comp_saturation: float = 50,
    comp_contrast: float = 50,
    comp_outline: float = 50,
    light_apply: bool = False,
    light_x: float = 25,
    light_y: float = 0,
    light_z: float = 0,
) -> Any:
    """Image to Image: refine/repaint existing PNGs. strength 0-100.

    Inputs are resized to max side 512 like the UI does. Same advanced options
    as rd_generate.
    """
    w, h = _dims(width, height, pixel_size, size_preset, swap)
    value = _core_value(
        prompt, negative, w, h, pixel_size, generations, seed, cfg_scale, loras or [], model,
        adherence, prompt_tuning, use_ella, tile_x, tile_y, pixelvae, rembg, post_process,
        save_progress,
        _comp(comp_hue, comp_tint, comp_brightness, comp_saturation, comp_contrast, comp_outline),
        _light(light_apply, light_x, light_y, light_z),
        add_to_prompt=add_to_prompt,
        quality=quality,
    )
    value["strength"] = int(strength)
    value["images"] = [_encode_entry(p, max_side=512) for p in image_paths]
    return await _run({"action": "img2img", "value": value}, _safe_prefix("i2i_" + prompt), save_progress, save_grid)


@mcp.tool()
async def rd_cn_txt2img(
    prompt: str,
    controlnets: List[Dict[str, Any]],
    control_images: List[Optional[str]],
    negative: str = "",
    steps: int = 12,
    palette_files: Optional[List[str]] = None,
    palette_url: Optional[str] = None,
    size_preset: Optional[str] = None,
    swap: bool = False,
    width: int = 512,
    height: int = 512,
    pixel_size: int = 8,
    generations: int = 1,
    seed: Optional[int] = None,
    cfg_scale: float = 5.0,
    adherence: float = 5.0,
    prompt_tuning: bool = True,
    add_to_prompt: bool = False,
    use_ella: bool = True,
    loras: Optional[List[Dict[str, Any]]] = None,
    model: str = "model.pxlm",
    pixelvae: bool = False,
    rembg: bool = False,
    post_process: bool = False,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
    comp_hue: float = 0,
    comp_tint: float = 0,
    comp_brightness: float = 70,
    comp_saturation: float = 50,
    comp_contrast: float = 50,
    comp_outline: float = 50,
    light_apply: bool = False,
    light_x: float = 25,
    light_y: float = 0,
    light_z: float = 0,
) -> Any:
    """ControlNet Text to Image.

    controlnets: list of {model: Composition|Depth|Pose|Sketch|Tile, weight: 0-100,
    process: bool (preprocess input), enabled: bool}; control_images: parallel
    list of input PNG paths (None for disabled slots). Palette constraint via
    palette_files (PNG palettes) or palette_url. Uses explicit steps (no quality).
    """
    w, h = _dims(width, height, pixel_size, size_preset, swap)
    value = _core_value(
        prompt, negative, w, h, pixel_size, generations, seed, cfg_scale, loras or [], model,
        adherence, prompt_tuning, use_ella, False, False, pixelvae, rembg, post_process,
        save_progress,
        _comp(comp_hue, comp_tint, comp_brightness, comp_saturation, comp_contrast, comp_outline),
        _light(light_apply, light_x, light_y, light_z),
        add_to_prompt=add_to_prompt,
        steps=steps,
    )
    cn, imgs = _cn_entries(controlnets, control_images)
    value["controlnets"] = cn
    value["images"] = imgs
    src, url, pals = _palette_block(palette_files, palette_url)
    value["source"], value["url"], value["palettes"] = src, url, pals
    return await _run({"action": "cntxt2img", "value": value}, _safe_prefix("cn_" + prompt), save_progress, save_grid)


@mcp.tool()
async def rd_cn_img2img(
    image_path: str,
    prompt: str,
    controlnets: List[Dict[str, Any]],
    control_images: List[Optional[str]],
    negative: str = "",
    strength: int = 50,
    steps: int = 12,
    palette_files: Optional[List[str]] = None,
    palette_url: Optional[str] = None,
    size_preset: Optional[str] = None,
    swap: bool = False,
    width: int = 512,
    height: int = 512,
    pixel_size: int = 8,
    generations: int = 1,
    seed: Optional[int] = None,
    cfg_scale: float = 5.0,
    adherence: float = 5.0,
    prompt_tuning: bool = True,
    add_to_prompt: bool = False,
    use_ella: bool = True,
    loras: Optional[List[Dict[str, Any]]] = None,
    model: str = "model.pxlm",
    pixelvae: bool = False,
    rembg: bool = False,
    post_process: bool = False,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
    comp_hue: float = 0,
    comp_tint: float = 0,
    comp_brightness: float = 70,
    comp_saturation: float = 50,
    comp_contrast: float = 50,
    comp_outline: float = 50,
    light_apply: bool = False,
    light_x: float = 25,
    light_y: float = 0,
    light_z: float = 0,
) -> Any:
    """ControlNet Image to Image: same as rd_cn_txt2img plus a source image
    (appended after the ControlNet slots, resized to 512 like the UI) and
    strength 0-100.
    """
    w, h = _dims(width, height, pixel_size, size_preset, swap)
    value = _core_value(
        prompt, negative, w, h, pixel_size, generations, seed, cfg_scale, loras or [], model,
        adherence, prompt_tuning, use_ella, False, False, pixelvae, rembg, post_process,
        save_progress,
        _comp(comp_hue, comp_tint, comp_brightness, comp_saturation, comp_contrast, comp_outline),
        _light(light_apply, light_x, light_y, light_z),
        add_to_prompt=add_to_prompt,
        steps=steps,
    )
    value["strength"] = int(strength)
    cn, imgs = _cn_entries(controlnets, control_images)
    imgs.append(_encode_entry(image_path, name="i2i", max_side=512))
    value["controlnets"] = cn
    value["images"] = imgs
    src, url, pals = _palette_block(palette_files, palette_url)
    value["source"], value["url"], value["palettes"] = src, url, pals
    return await _run({"action": "cnimg2img", "value": value}, _safe_prefix("cni2i_" + prompt), save_progress, save_grid)


async def _neural(
    action: str,
    image_path: str,
    prompt: str,
    negative: str,
    steps: int,
    cfg_scale: float,
    width: int,
    height: int,
    pixel_size: int,
    generations: int,
    seed: Optional[int],
    loras: List[Dict[str, Any]],
    model: str,
    adherence: float,
    prompt_tuning: bool,
    use_ella: bool,
    pixelvae: bool,
    post_process: bool,
    save_progress: bool,
    save_grid: bool,
    extra: Dict[str, Any],
    prefix: str,
    add_to_prompt: bool = False,
) -> Dict[str, Any]:
    out_w = max(16, int(width) // max(1, int(pixel_size)))
    out_h = max(16, int(height) // max(1, int(pixel_size)))
    value: Dict[str, Any] = {
        "prompt": prompt,
        "negative": negative,
        "use_ella": use_ella,
        "adherence": _clamp(adherence, 1, 5),
        "translate": bool(add_to_prompt),
        "prompt_tuning": prompt_tuning,
        "width": out_w * 8,
        "height": out_h * 8,
        "pixel_size": 8,
        "steps": int(_clamp(steps, 5, 50)),
        "scale": float(cfg_scale),
        "lighting": dict(NEUTRAL_LIGHTING),
        "composition": dict(NEUTRAL_COMPOSITION),
        "seed": _seed(seed),
        "generations": int(generations),
        "max_batch_size": 512,
        "model": _model_block(model),
        "loras": [_resolve_lora(l) for l in (loras or [])],
        "images": [_encode_entry(image_path, name="i2i", max_side=512)],
        "use_pixelvae": bool(pixelvae),
        "send_progress": bool(save_progress),
        "post_process": bool(post_process),
        "background": True,
        "play_sound": False,
    }
    value.update(extra)
    return await _run({"action": action, "value": value}, _safe_prefix(prefix), save_progress, save_grid)


NEUTRAL_LIGHTING = {"apply": False, "x": 25, "y": 0, "z": 0}
NEUTRAL_COMPOSITION = {"hue": 0, "tint": 0, "brightness": 70, "saturation": 50, "contrast": 50, "outline": 50}


@mcp.tool()
async def rd_neural_transform(
    image_path: str,
    prompt: str,
    negative: str = "",
    steps: int = 12,
    cfg_scale: float = 5.0,
    width: int = 512,
    height: int = 512,
    pixel_size: int = 8,
    generations: int = 1,
    seed: Optional[int] = None,
    loras: Optional[List[Dict[str, Any]]] = None,
    model: str = "model.pxlm",
    adherence: float = 5.0,
    prompt_tuning: bool = True,
    add_to_prompt: bool = False,
    use_ella: bool = True,
    pixelvae: bool = False,
    post_process: bool = False,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
) -> Any:
    """Neural Transform: re-imagine the input image in a new style/pose from a prompt."""
    return await _neural(
        "transform", image_path, prompt, negative, steps, cfg_scale, width, height, pixel_size,
        generations, seed, loras or [], model, adherence, prompt_tuning, use_ella, pixelvae,
        post_process, save_progress, save_grid, {}, "transform_" + prompt,
        add_to_prompt=add_to_prompt,
    )


@mcp.tool()
async def rd_neural_pixelate(
    image_path: str,
    description: str = "",
    negative_description: str = "",
    blip: bool = True,
    color_map: bool = True,
    steps: int = 12,
    cfg_scale: float = 5.0,
    width: int = 512,
    height: int = 512,
    pixel_size: int = 8,
    generations: int = 1,
    seed: Optional[int] = None,
    loras: Optional[List[Dict[str, Any]]] = None,
    model: str = "model.pxlm",
    adherence: float = 5.0,
    prompt_tuning: bool = True,
    add_to_prompt: bool = False,
    use_ella: bool = True,
    pixelvae: bool = False,
    post_process: bool = False,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
) -> Any:
    """Neural Pixelate: convert an image (photo/render) into pixel art.

    blip auto-captions the input; color_map preserves the source palette.
    """
    return await _neural(
        "pixelate", image_path, description, negative_description, steps, cfg_scale, width, height,
        pixel_size, generations, seed, loras or [], model, adherence, prompt_tuning, use_ella,
        pixelvae, post_process, save_progress, save_grid,
        {"blip": bool(blip), "color_map": bool(color_map)}, "pixelate",
        add_to_prompt=add_to_prompt,
    )


@mcp.tool()
async def rd_neural_resize(
    image_path: str,
    description: str = "",
    negative_description: str = "",
    blip: bool = True,
    color_map: bool = True,
    steps: int = 12,
    cfg_scale: float = 5.0,
    width: int = 256,
    height: int = 256,
    pixel_size: int = 4,
    generations: int = 1,
    seed: Optional[int] = None,
    loras: Optional[List[Dict[str, Any]]] = None,
    model: str = "model.pxlm",
    adherence: float = 5.0,
    prompt_tuning: bool = True,
    add_to_prompt: bool = False,
    use_ella: bool = True,
    pixelvae: bool = False,
    post_process: bool = False,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
) -> Any:
    """Neural Resize: change pixel density/resolution of pixel art while keeping detail coherent."""
    return await _neural(
        "resize", image_path, description, negative_description, steps, cfg_scale, width, height,
        pixel_size, generations, seed, loras or [], model, adherence, prompt_tuning, use_ella,
        pixelvae, post_process, save_progress, save_grid,
        {"blip": bool(blip), "color_map": bool(color_map)}, "resize",
        add_to_prompt=add_to_prompt,
    )


@mcp.tool()
async def rd_neural_detail(
    image_path: str,
    description: str = "",
    negative_description: str = "",
    detail: int = 5,
    blip: bool = True,
    color_map: bool = True,
    steps: int = 12,
    cfg_scale: float = 5.0,
    width: int = 512,
    height: int = 512,
    pixel_size: int = 8,
    generations: int = 1,
    seed: Optional[int] = None,
    loras: Optional[List[Dict[str, Any]]] = None,
    model: str = "model.pxlm",
    adherence: float = 5.0,
    prompt_tuning: bool = True,
    add_to_prompt: bool = False,
    use_ella: bool = True,
    pixelvae: bool = False,
    post_process: bool = False,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
) -> Any:
    """Neural Detail: add or remove detail (detail 0-100) on existing pixel art."""
    return await _neural(
        "detail", image_path, description, negative_description, steps, cfg_scale, width, height,
        pixel_size, generations, seed, loras or [], model, adherence, prompt_tuning, use_ella,
        pixelvae, post_process, save_progress, save_grid,
        {"blip": bool(blip), "color_map": bool(color_map), "detail": int(_clamp(detail, 1, 10))}, "detail",
        add_to_prompt=add_to_prompt,
    )


# ---------------------------------------------------------------------------
# retrodiffusion.ai cloud API tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def rd_api_txt2img(
    prompt: str,
    style: str = "pixel art",
    api_key: Optional[str] = None,
    width: int = 512,
    height: int = 512,
    generations: int = 1,
    seed: Optional[int] = None,
    strength: int = 100,
    tile_x: bool = False,
    tile_y: bool = False,
    rembg: bool = False,
    palette_files: Optional[List[str]] = None,
    palette_url: Optional[str] = None,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
) -> Any:
    """Retrodiffusion.ai cloud API: text to image (needs an API key: arg,
    RD_API_KEY env, or the extension's settings rdapikey)."""
    src, url, pals = _palette_block(palette_files, palette_url)
    value = {
        "api_key": _api_key(api_key),
        "prompt": prompt,
        "width": int(width),
        "height": int(height),
        "strength": int(strength),
        "source": src,
        "url": url,
        "palettes": pals,
        "seed": _seed(seed),
        "generations": int(generations),
        "style": style,
        "tile_x": bool(tile_x),
        "tile_y": bool(tile_y),
        "image": "",
        "rembg": bool(rembg),
        "send_progress": bool(save_progress),
        "background": True,
        "play_sound": False,
    }
    return await _run({"action": "apitxt2img", "value": value}, _safe_prefix("api_" + prompt), save_progress, save_grid)


@mcp.tool()
async def rd_api_img2img(
    image_paths: List[str],
    prompt: str,
    strength: int = 50,
    style: str = "pixel art",
    api_key: Optional[str] = None,
    width: int = 512,
    height: int = 512,
    generations: int = 1,
    seed: Optional[int] = None,
    tile_x: bool = False,
    tile_y: bool = False,
    rembg: bool = False,
    palette_files: Optional[List[str]] = None,
    palette_url: Optional[str] = None,
    save_progress: bool = False,
    save_grid: bool = False,
    return_image: bool = False,
) -> Any:
    """Retrodiffusion.ai cloud API: image to image (needs an API key)."""
    src, url, pals = _palette_block(palette_files, palette_url)
    value = {
        "api_key": _api_key(api_key),
        "prompt": prompt,
        "width": int(width),
        "height": int(height),
        "strength": int(strength),
        "source": src,
        "url": url,
        "palettes": pals,
        "seed": _seed(seed),
        "generations": int(generations),
        "style": style,
        "tile_x": bool(tile_x),
        "tile_y": bool(tile_y),
        "image": [_encode_entry(p, max_side=512) for p in image_paths],
        "rembg": bool(rembg),
        "send_progress": bool(save_progress),
        "background": True,
        "play_sound": False,
    }
    return await _run({"action": "apiimg2img", "value": value}, _safe_prefix("apii2i_" + prompt), save_progress, save_grid)


@mcp.tool()
async def rd_api_txt2anim(
    prompt: str,
    style: str = "pixel art",
    api_key: Optional[str] = None,
    width: int = 512,
    height: int = 512,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Retrodiffusion.ai cloud API: text to animation. Returns frame PNGs plus
    an assembled GIF (needs an API key)."""
    value = {
        "api_key": _api_key(api_key),
        "prompt": prompt,
        "width": int(width),
        "height": int(height),
        "seed": _seed(seed),
        "style": style,
        "image": "",
        "send_progress": False,
        "background": True,
        "play_sound": False,
    }
    return await _run({"action": "apitxt2anim", "value": value}, _safe_prefix("apianim_" + prompt), False, False, anim=True)


@mcp.tool()
async def rd_api_img2anim(
    image_path: str,
    prompt: str,
    style: str = "pixel art",
    api_key: Optional[str] = None,
    width: int = 512,
    height: int = 512,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Retrodiffusion.ai cloud API: image to animation. Returns frame PNGs plus
    an assembled GIF (needs an API key)."""
    value = {
        "api_key": _api_key(api_key),
        "prompt": prompt,
        "width": int(width),
        "height": int(height),
        "seed": _seed(seed),
        "style": style,
        "image": [_encode_entry(image_path, max_side=512)],
        "send_progress": False,
        "background": True,
        "play_sound": False,
    }
    return await _run({"action": "apiimg2anim", "value": value}, _safe_prefix("apii2anim"), False, False, anim=True)


# ---------------------------------------------------------------------------
# image utility tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def rd_rembg(image_paths: List[str]) -> Dict[str, Any]:
    """Remove Background: cut the background out of images (ISNET matting)."""
    value = {
        "images": [_encode_entry(p) for p in image_paths],
        "model_folder": str(MODEL_DIR),
        "background": True,
        "play_sound": False,
    }
    return await _run({"action": "rembg", "value": value}, _safe_prefix("rembg"), False, False)


@mcp.tool()
async def rd_texture_gen(image_paths: List[str], tiling: str = "none") -> Dict[str, Any]:
    """Generate Textures: PBR maps (normal/roughness/displacement) from images.

    tiling: none | seamless | mirror | replicate (extend).
    """
    if tiling not in TEXTURE_TILING:
        raise ValueError(f"tiling must be one of {TEXTURE_TILING}")
    value = {
        "images": [_encode_entry(p) for p in image_paths],
        "model_folder": str(MODEL_DIR),
        "ops": tiling,
        "background": True,
        "play_sound": False,
    }
    res, _ = await _rd_call({"action": "textureGen", "value": value})
    maps = res.get("value") or {}
    out: Dict[str, Any] = {}
    for cat in ("normal_maps", "roughness_maps", "depth_maps"):
        entries = maps.get(cat) or []
        out[cat] = _save_images(entries, _safe_prefix("texture_" + cat)) if entries else []
    return out


@mcp.tool()
async def rd_palettize(
    image_paths: List[str],
    colors: int = 16,
    palette_files: Optional[List[str]] = None,
    palette_url: Optional[str] = None,
    dither: str = "None",
    dither_strength: int = 5,
    denoise: bool = False,
    smoothness: int = 1,
    intensity: int = 5,
) -> Dict[str, Any]:
    """Palettize: reduce images to a palette (file/URL constrained or automatic).

    dither: None | Bayer 2x2 | Bayer 4x4 | Bayer 8x8; dither_strength, denoise,
    smoothness, intensity are 0-100 sliders mirroring the UI.
    """
    if dither not in DITHER_MAP:
        raise ValueError(f"dither must be one of {sorted(DITHER_MAP)}")
    src, url, pals = _palette_block(palette_files, palette_url)
    value = {
        "images": [_encode_entry(p) for p in image_paths],
        "source": src,
        "url": url,
        "palettes": pals,
        "colors": int(colors),
        "dithering": DITHER_MAP[dither],
        "dither_strength": int(_clamp(dither_strength, 0, 10)),
        "denoise": bool(denoise),
        "smoothness": int(_clamp(smoothness, 1, 10)),
        "intensity": int(_clamp(intensity, 1, 10)),
        "background": True,
        "play_sound": False,
    }
    return await _run({"action": "palettize", "value": value}, _safe_prefix("palettize"), False, False)


@mcp.tool()
async def rd_color_transfer(image_paths: List[str], reference_path: str, strict: bool = False) -> Dict[str, Any]:
    """Color Style Transfer: recolor images to match a reference image's palette.

    strict enforces the exact reference colors.
    """
    value = {
        "images": [_encode_entry(p) for p in image_paths],
        "reference": _encode_entry(reference_path, name="reference"),
        "strict": bool(strict),
        "background": True,
        "play_sound": False,
    }
    return await _run({"action": "colorTransfer", "value": value}, _safe_prefix("colortransfer"), False, False)


@mcp.tool()
async def rd_kcentroid(
    image_paths: List[str],
    width: int = 64,
    height: int = 64,
    centroids: int = 16,
    outline: bool = False,
) -> Dict[str, Any]:
    """K-Centroid Resize: downscale images with k-means palette preservation
    (centroids = target colors), optionally re-adding an outline."""
    value = {
        "images": [_encode_entry(p) for p in image_paths],
        "width": int(width),
        "height": int(height),
        "centroids": int(_clamp(centroids, 2, 16)),
        "outline": bool(outline),
        "background": True,
        "play_sound": False,
    }
    return await _run({"action": "kcentroid", "value": value}, _safe_prefix("kcentroid"), False, False)


@mcp.tool()
async def rd_palette(prompt: str, colors: int = 16, seed: Optional[int] = None) -> Dict[str, Any]:
    """Generate a color palette with Retro Diffusion's palette model.

    Returns the swatch image path plus extracted hex colors (row-major,
    deduplicated).
    """
    size = max(2, min(256, int(colors)))
    pow2 = max(2, 2 ** round(math.log2(size)))
    value = {
        "prompt": f"photo of a {pow2} color palette, {prompt} color palette",
        "colors": size,
        "seed": _seed(seed),
        "model": _model_block("paletteGen.pxlm"),
        "background": True,
        "play_sound": False,
    }
    res, _ = await _rd_call({"action": "txt2pal", "value": value})
    images = (res.get("value") or {}).get("images") or []
    if not images:
        raise RuntimeError("palette generation returned no images")
    saved = _save_images(images, _safe_prefix("pal_" + prompt))
    pil = Image.open(saved[0]["path"]).convert("RGB")
    hexes: List[str] = []
    for y in range(pil.height):
        for x in range(pil.width):
            h = "#%02x%02x%02x" % pil.getpixel((x, y))
            if h not in hexes:
                hexes.append(h)
    return {"image": saved[0]["path"], "colors": hexes[:size], "seed": saved[0].get("seed")}


@mcp.tool()
async def rd_prompt_extract(reference_path: str, description: str = "") -> Dict[str, Any]:
    """Extract a text prompt/description from a reference image (BLIP captioning,
    optionally guided by a partial description)."""
    value = {
        "model_folder": str(MODEL_DIR),
        "reference": _encode_entry(reference_path, name="reference"),
        "description": description,
        "background": True,
        "play_sound": False,
    }
    res, _ = await _rd_call({"action": "promptExtract", "value": value})
    val = res.get("value")
    if isinstance(val, str):
        return {"description": val}
    return (val or {}) if isinstance(val, dict) else {"value": val}


@mcp.tool()
async def rd_translate(prompt: str, negative: str = "", generations: int = 1, seed: Optional[int] = None) -> Dict[str, Any]:
    """Translate/enhance prompts into model-friendly English prompt text."""
    value = {
        "prompt": prompt,
        "negative": negative,
        "generations": int(generations),
        "seed": _seed(seed),
        "model_folder": str(MODEL_DIR),
        "background": True,
        "play_sound": False,
    }
    res, _ = await _rd_call({"action": "translate", "value": value})
    val = res.get("value")
    if isinstance(val, list):
        return {"prompts": val}
    return (val or {}) if isinstance(val, dict) else {"value": val}


@mcp.tool()
async def rd_benchmark(
    time_limit: int = 60,
    max_test_size: int = 512,
    error_range: int = 5,
    seed: Optional[int] = None,
    pixelvae: bool = False,
) -> Dict[str, Any]:
    """Run the backend benchmark (throughput/consistency measurement)."""
    value = {
        "time_limit": int(time_limit),
        "max_test_size": int(max_test_size),
        "error_range": int(error_range),
        "seed": _seed(seed),
        "model": _model_block(),
        "use_pixelvae": bool(pixelvae),
        "background": True,
        "play_sound": False,
    }
    res, _ = await _rd_call({"action": "benchmark", "value": value}, timeout=1800)
    return (res.get("value") or {}) or res


@mcp.tool()
def rd_quantize(image_path: str, colors: int = 16, out_path: Optional[str] = None) -> Dict[str, Any]:
    """Reduce a PNG to an indexed palette (GBA-friendly) and return the hex palette.

    Pillow median-cut quantization; output is a P-mode PNG. Default out_path
    sits next to the input with a _q{colors} suffix.
    """
    src = Path(image_path)
    dst = Path(out_path) if out_path else src.with_name(f"{src.stem}_q{colors}.png")
    pil = Image.open(src).convert("RGB")
    q = pil.quantize(colors=int(colors), method=Image.Quantize.MEDIANCUT)
    q.save(dst)
    pal = q.getpalette() or []
    hexes = ["#%02x%02x%02x" % tuple(pal[i * 3 : i * 3 + 3]) for i in range(min(int(colors), len(pal) // 3))]
    return {"path": str(dst), "colors": hexes}


# ---------------------------------------------------------------------------
# Aseprite interconnect (headless CLI)
# ---------------------------------------------------------------------------
def _run_aseprite_lua(lua: str, timeout: int = 180) -> str:
    if not ASEPRITE_EXE.exists():
        raise RuntimeError(f"Aseprite executable not found at {ASEPRITE_EXE}")
    with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False, encoding="utf-8") as fh:
        fh.write(lua)
        lua_path = fh.name
    try:
        env = dict(os.environ)
        env["RD_MCP_HEADLESS"] = "1"  # extension.lua guard: no shell-outs, no cmd flashes
        proc = subprocess.run(
            [str(ASEPRITE_EXE), "-b", "--script", lua_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
            env=env,
            **_hide_startup(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0 or "RD-MCP-OK" not in out:
            raise RuntimeError(f"Aseprite batch failed (rc={proc.returncode}): {out[-2000:]}")
        return out
    finally:
        try:
            os.unlink(lua_path)
        except OSError:
            pass


def _lua_str(s: str) -> str:
    return "[==[" + s + "]==]"


@mcp.tool()
def ase_import_layer(sprite_path: str, image_path: str, layer_name: str, frame: int = 1) -> Dict[str, Any]:
    """Import a PNG (e.g. an RD generation) into an .aseprite sprite as a new layer.

    The image cel is placed at (0,0) on the given 1-based frame. The sprite file
    is saved in place. Runs headless via the Aseprite CLI.
    """
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local img = Image{{ fromFile = {_lua_str(image_path)} }}
local layer = spr:newLayer()
layer.name = {_lua_str(layer_name)}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
spr:newCel(layer, spr.frames[idx], img, Point(0, 0))
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK layer=" .. layer.name .. " frames=" .. #spr.frames)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "sprite": sprite_path, "layer": layer_name, "log": out.strip().splitlines()[-1:]}


@mcp.tool()
def ase_add_frames(
    sprite_path: str,
    image_paths: List[str],
    layer_name: str = "rd-frames",
    tag_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Append each PNG as a new animation frame on a new layer of an .aseprite sprite.

    Useful to turn RD generations into idle-animation frames. Optionally wraps
    the new frames in an animation tag. Saves in place, headless CLI.
    """
    paths_lua = ",\n  ".join(_lua_str(p) for p in image_paths)
    tag_lua = ""
    if tag_name:
        tag_lua = f"""
local ok, tag = pcall(function() return spr:newTag(fromF, toF) end)
if ok and tag then tag.name = {_lua_str(tag_name)} end
"""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local paths = {{
  {paths_lua}
}}
local layer = spr:newLayer()
layer.name = {_lua_str(layer_name)}
local fromF, toF = 0, 0
for i, p in ipairs(paths) do
  if i > 1 then spr:newFrame() end
  local img = Image{{ fromFile = p }}
  local fr = spr.frames[#spr.frames]
  spr:newCel(layer, fr, img, Point(0, 0))
  if i == 1 then fromF = fr.frameNumber end
  toF = fr.frameNumber
end
{tag_lua}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK frames=" .. #spr.frames .. " tag=" .. tostring({json.dumps(tag_name)}))
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "sprite": sprite_path, "frames_added": len(image_paths), "log": out.strip().splitlines()[-1:]}




# ---------------------------------------------------------------------------
# Aseprite workbench (pixelworks consolidation - replaces pixel-mcp)
#
# Pixel data crosses the CLI boundary as JSON temp files: the server writes
# pixel lists, a headless Lua script reads/writes them via Aseprite's json
# global. Selection + clipboard are server-side state (headless Aseprite is
# stateless per spawn), mirroring pixel-mcp's semantics.
# ---------------------------------------------------------------------------
import struct as _struct

_SEL: Dict[str, Any] = {"kind": None}
_CLIP: Dict[str, Any] = {}


def _px_json_path(tag: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"_px_{tag}_{os.getpid()}.json"


def _lua_dump_cel(sprite_path: str, layer: str, frame: int, out_json: str) -> str:
    return f"""
local spr = app.open({_lua_str(sprite_path)})
local layer = nil
for _, l in ipairs(spr.layers) do if l.name == {_lua_str(layer)} then layer = l end end
if layer == nil then layer = spr.layers[{1}] end
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local cel = layer:cel(spr.frames[idx])
if cel == nil then
  local f2 = io.open({_lua_str(out_json)}, "wb")
  f2:write(json.encode({{ width = spr.width, height = spr.height, layer = layer.name, frame = idx, pixels = {{}} }}))
  f2:close()
  print("RD-MCP-OK dump")
  return
end
-- read sprite-globally: project the (possibly trimmed/offset) cel onto a
-- canvas-sized image so dumps always use sprite coordinates
local img = Image(spr.width, spr.height, ColorMode.RGB)
img:drawImage(cel.image, cel.position)
local px = {{}}
for y = 0, img.height - 1 do
  for x = 0, img.width - 1 do
    local c = img:getPixel(x, y)
    table.insert(px, {{ x = x, y = y, r = app.pixelColor.rgbaR(c), g = app.pixelColor.rgbaG(c), b = app.pixelColor.rgbaB(c), a = app.pixelColor.rgbaA(c) }})
  end
end
local f = io.open({_lua_str(out_json)}, "wb")
f:write(json.encode({{ width = img.width, height = img.height, layer = layer.name, frame = idx, pixels = px }}))
f:close()
print("RD-MCP-OK dump")
"""


def _lua_draw_pixels(sprite_path: str, layer: Optional[str], frame: int, in_json: str, mode: str, save: bool) -> str:
    lay = f"for _, l in ipairs(spr.layers) do if l.name == {_lua_str(layer)} then layer = l end end" if layer else "layer = spr.layers[1]"
    return f"""
local spr = app.open({_lua_str(sprite_path)})
local layer = nil
{lay}
if layer == nil then layer = spr.layers[1] end
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local fr = spr.frames[idx]
{_LUA_NORMALIZE_CEL}
local cel = normalize_cel(spr, layer, fr, true)
local img = cel.image
local f = io.open({_lua_str(in_json)}, "rb")
local data = json.decode(f:read("*a"))
f:close()
for _, p in ipairs(data.pixels) do
  if p.x >= 0 and p.y >= 0 and p.x < img.width and p.y < img.height then
    if {("true" if mode == "replace" else "p.a > 0")} then
      img:drawPixel(p.x, p.y, app.pixelColor.rgba(p.r, p.g, p.b, p.a))
    end
  end
end
print("RD-MCP-OK drawn=" .. #data.pixels)
{"spr:saveAs(" + _lua_str(sprite_path) + ")" if save else ""}
"""


def _dump_cel(sprite_path: str, layer: Optional[str], frame: int) -> Dict[str, Any]:
    out = _px_json_path("dump")
    _run_aseprite_lua(_lua_dump_cel(sprite_path, layer or "", frame, str(out)))
    data = json.loads(out.read_text(encoding="utf-8"))
    try:
        out.unlink()
    except OSError:
        pass
    return data


def _draw_pixels(sprite_path: str, layer: Optional[str], frame: int, pixels: List[Dict[str, int]], mode: str = "replace", save: bool = True) -> int:
    inp = _px_json_path("draw")
    inp.write_text(json.dumps({"pixels": pixels}), encoding="utf-8")
    try:
        _run_aseprite_lua(_lua_draw_pixels(sprite_path, layer, frame, str(inp), mode, save))
    finally:
        try:
            inp.unlink()
        except OSError:
            pass
    return len(pixels)


def _hexpx(p: Dict[str, int]) -> str:
    return "#%02x%02x%02x%02x" % (p["r"], p["g"], p["b"], p["a"])


def _sel_mask(w: int, h: int) -> List[bool]:
    kind = _SEL.get("kind")
    mask = [False] * (w * h)
    if kind is None:
        return mask
    if kind == "all":
        return [True] * (w * h)
    if kind == "rect":
        x0, y0, rw, rh = _SEL["x"], _SEL["y"], _SEL["w"], _SEL["h"]
        for y in range(max(0, y0), min(h, y0 + rh)):
            for x in range(max(0, x0), min(w, x0 + rw)):
                mask[y * w + x] = True
        return mask
    if kind == "ellipse":
        x0, y0, rw, rh = _SEL["x"], _SEL["y"], _SEL["w"], _SEL["h"]
        cx, cy = x0 + (rw - 1) / 2.0, y0 + (rh - 1) / 2.0
        rx, ry = rw / 2.0, rh / 2.0
        for y in range(max(0, y0), min(h, y0 + rh)):
            for x in range(max(0, x0), min(w, x0 + rw)):
                dx, dy = (x - cx) / rx, (y - cy) / ry
                if dx * dx + dy * dy <= 1.0:
                    mask[y * w + x] = True
    return mask


# --- sprite lifecycle / info -----------------------------------------------
@mcp.tool()
def ase_create_canvas(width: int, height: int, color_mode: str = "rgb", out_path: Optional[str] = None) -> Dict[str, Any]:
    """Create a new .aseprite sprite (rgb|grayscale|indexed). Returns its path."""
    dst = Path(out_path) if out_path else OUT_DIR / f"sprite_{time.strftime('%Y%m%d-%H%M%S')}.aseprite"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mode = {"rgb": "ColorMode.RGB", "grayscale": "ColorMode.GRAYSCALE", "indexed": "ColorMode.INDEXED"}.get(color_mode)
    if not mode:
        raise ValueError("color_mode must be rgb|grayscale|indexed")
    lua = f"""
local spr = Sprite({int(width)}, {int(height)}, {mode})
spr:saveAs({_lua_str(str(dst))})
print("RD-MCP-OK created")
"""
    _run_aseprite_lua(lua)
    return {"path": str(dst), "width": width, "height": height, "color_mode": color_mode}


@mcp.tool()
def ase_sprite_info(sprite_path: str) -> Dict[str, Any]:
    """Sprite metadata: dimensions, color mode, frames (with durations), layers, tags."""
    out = _px_json_path("info")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local frames = {{}}
{_lua_dur_helpers()}
for i, fr in ipairs(spr.frames) do
  table.insert(frames, {{ index = i, duration_ms = get_dur_ms(fr) }})
end
local layers = {{}}
for i, l in ipairs(spr.layers) do
  table.insert(layers, {{ index = i, name = l.name, isImage = l.isImage, opacity = l.opacity }})
end
local tags = {{}}
local dirNames = {{ "forward", "reverse", "pingpong" }}
for _, t in ipairs(spr.tags) do
  table.insert(tags, {{ name = t.name, from = t.fromFrame.frameNumber, to = t.toFrame.frameNumber, direction = dirNames[(t.aniDir or 0) + 1] or "forward" }})
end
local f = io.open({_lua_str(str(out))}, "wb")
f:write(json.encode({{ width = spr.width, height = spr.height, colorMode = spr.colorMode, frames = frames, layers = layers, tags = tags }}))
f:close()
print("RD-MCP-OK info")
"""
    _run_aseprite_lua(lua)
    data = json.loads(out.read_text(encoding="utf-8"))
    try:
        out.unlink()
    except OSError:
        pass
    return data


@mcp.tool()
def ase_save_as(sprite_path: str, out_path: str) -> Dict[str, Any]:
    """Save a sprite copy to a new path."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
spr:saveAs({_lua_str(out_path)})
print("RD-MCP-OK saved")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "path": out_path}


# --- pixel read/write -------------------------------------------------------
@mcp.tool()
def ase_draw_pixels(
    sprite_path: str,
    pixels: List[Dict[str, int]],
    layer: Optional[str] = None,
    frame: int = 1,
    merge: bool = False,
) -> Dict[str, Any]:
    """Draw individual pixels [{x,y,r,g,b,a}] (or hex via r/g/b/a ints) on a cel.

    merge=True skips fully transparent pixels (sprite stamping); default
    replaces everything including alpha.
    """
    n = _draw_pixels(sprite_path, layer, frame, pixels, mode="merge" if merge else "replace")
    return {"ok": True, "drawn": n}


@mcp.tool()
def ase_draw_pixels_hex(
    sprite_path: str,
    pixels: List[Dict[str, Any]],
    layer: Optional[str] = None,
    frame: int = 1,
    merge: bool = False,
) -> Dict[str, Any]:
    """Convenience: draw pixels given as [{x, y, color: '#rrggbb' or '#rrggbbaa'}]."""
    conv = []
    for p in pixels:
        c = str(p["color"]).lstrip("#")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        a = int(c[6:8], 16) if len(c) >= 8 else 255
        conv.append({"x": int(p["x"]), "y": int(p["y"]), "r": r, "g": g, "b": b, "a": a})
    n = _draw_pixels(sprite_path, layer, frame, conv, mode="merge" if merge else "replace")
    return {"ok": True, "drawn": n}


@mcp.tool()
def ase_get_pixels(sprite_path: str, x: int = 0, y: int = 0, width: Optional[int] = None, height: Optional[int] = None, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Read a rectangular region of a cel as {width,height,pixels:[{x,y,r,g,b,a,hex}]}."""
    data = _dump_cel(sprite_path, layer, frame)
    w, h = data["width"], data["height"]
    x1 = w if width is None else min(w, x + width)
    y1 = h if height is None else min(h, y + height)
    px = []
    for p in data["pixels"]:
        if x <= p["x"] < x1 and y <= p["y"] < y1:
            p = dict(p)
            p["hex"] = _hexpx(p)
            px.append(p)
    return {"width": w, "height": h, "region": [x, y, x1 - x, y1 - y], "pixels": px}


# --- shapes (rasterized server-side) ---------------------------------------
def _raster_line(x0: int, y0: int, x1: int, y1: int, thickness: int) -> Any:
    pts = []
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx - dy
    t = max(0, thickness - 1) // 2
    while True:
        for ox in range(-t, t + 1):
            for oy in range(-t, t + 1):
                pts.append((x0 + ox, y0 + oy))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy
    return pts


def _raster_circle(cx: int, cy: int, radius: int, filled: bool) -> Any:
    pts = []
    for y in range(cy - radius, cy + radius + 1):
        for x in range(cx - radius, cx + radius + 1):
            d = (x - cx) ** 2 + (y - cy) ** 2
            if filled:
                if d <= radius * radius:
                    pts.append((x, y))
            else:
                if radius * radius - 2 * radius <= d <= radius * radius:
                    pts.append((x, y))
    return pts


def _raster_rect(x: int, y: int, w: int, h: int, filled: bool) -> Any:
    pts = []
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            if filled or xx == x or xx == x + w - 1 or yy == y or yy == y + h - 1:
                pts.append((xx, yy))
    return pts


def _rgba(hexcolor: str) -> Dict[str, int]:
    c = hexcolor.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    a = int(c[6:8], 16) if len(c) >= 8 else 255
    return {"r": r, "g": g, "b": b, "a": a}


def _shape_tool(sprite_path, pts, color, layer, frame, merge) -> Dict[str, Any]:
    col = _rgba(color)
    pixels = [{"x": int(px), "y": int(py), **col} for px, py in pts]
    n = _draw_pixels(sprite_path, layer, frame, pixels, mode="merge" if merge else "replace")
    return {"ok": True, "drawn": n}


@mcp.tool()
def ase_draw_line(sprite_path: str, x1: int, y1: int, x2: int, y2: int, color: str, thickness: int = 1, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Draw a line (Bresenham, optional thickness)."""
    return _shape_tool(sprite_path, _raster_line(x1, y1, x2, y2, thickness), color, layer, frame, True)


@mcp.tool()
def ase_draw_rectangle(sprite_path: str, x: int, y: int, width: int, height: int, color: str, filled: bool = True, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Draw a rectangle (filled or outline)."""
    return _shape_tool(sprite_path, _raster_rect(x, y, width, height, filled), color, layer, frame, True)


@mcp.tool()
def ase_draw_circle(sprite_path: str, center_x: int, center_y: int, radius: int, color: str, filled: bool = True, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Draw a circle (filled or outline)."""
    return _shape_tool(sprite_path, _raster_circle(center_x, center_y, radius, filled), color, layer, frame, True)


@mcp.tool()
def ase_draw_contour(sprite_path: str, points: List[Dict[str, int]], color: str, thickness: int = 1, closed: bool = False, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Draw a polyline/polygon through points."""
    pts: List[Any] = []
    seq = [(int(p["x"]), int(p["y"])) for p in points]
    if closed and len(seq) > 2:
        seq.append(seq[0])
    for i in range(len(seq) - 1):
        pts.extend(_raster_line(seq[i][0], seq[i][1], seq[i + 1][0], seq[i + 1][1], thickness))
    return _shape_tool(sprite_path, pts, color, layer, frame, True)


BAYER = {
    2: [[0, 2], [3, 1]],
    4: [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]],
    8: [[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26], [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22], [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25], [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]],
}


@mcp.tool()
def ase_draw_with_dither(sprite_path: str, x: int, y: int, width: int, height: int, color1: str, color2: str, pattern: str = "bayer_4x4", density: float = 0.5, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Fill a region with a dither pattern (bayer_2x2/4x4/8x8, checkerboard, noise)."""
    c1, c2 = _rgba(color1), _rgba(color2)
    pixels = []
    import random as _rnd
    for yy in range(y, y + height):
        for xx in range(x, x + width):
            if pattern == "checkerboard":
                use2 = ((xx + yy) % 2) == 1
            elif pattern == "noise":
                use2 = _rnd.random() < density
            else:
                n = int(pattern.split("_")[-1][0]) if "_" in pattern else 4
                mat = BAYER.get(n, BAYER[4])
                thr = (mat[yy % n][xx % n] + 0.5) / (n * n)
                use2 = thr < density
            col = c2 if use2 else c1
            pixels.append({"x": xx, "y": yy, **col})
    n = _draw_pixels(sprite_path, layer, frame, pixels, mode="replace")
    return {"ok": True, "drawn": n}


@mcp.tool()
def ase_fill_area(sprite_path: str, x: int, y: int, color: str, layer: Optional[str] = None, frame: int = 1, tolerance: int = 0) -> Dict[str, Any]:
    """Flood fill from a point (paint bucket), RGBA tolerance 0-255."""
    data = _dump_cel(sprite_path, layer, frame)
    w, h = data["width"], data["height"]
    grid = {}
    for p in data["pixels"]:
        grid[(p["x"], p["y"])] = (p["r"], p["g"], p["b"], p["a"])
    target = grid.get((x, y))
    if target is None:
        return {"ok": False, "filled": 0}
    repl = _rgba(color)
    rt = (repl["r"], repl["g"], repl["b"], repl["a"])
    if all(abs(target[i] - rt[i]) <= tolerance for i in range(4)):
        return {"ok": True, "filled": 0}
    stack = [(x, y)]
    seen = set()
    pixels = []
    while stack:
        cx, cy = stack.pop()
        if (cx, cy) in seen or not (0 <= cx < w and 0 <= cy < h):
            continue
        seen.add((cx, cy))
        c = grid.get((cx, cy))
        if c is None or not all(abs(c[i] - target[i]) <= tolerance for i in range(4)):
            continue
        grid[(cx, cy)] = rt
        pixels.append({"x": cx, "y": cy, **repl})
        stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])
    _draw_pixels(sprite_path, layer, frame, pixels, mode="replace")
    return {"ok": True, "filled": len(pixels)}


# --- selection + clipboard (server-side state) -----------------------------
@mcp.tool()
def ase_select_rectangle(x: int, y: int, width: int, height: int) -> Dict[str, Any]:
    """Set the server-side selection to a rectangle."""
    _SEL.clear()
    _SEL.update({"kind": "rect", "x": x, "y": y, "w": width, "h": height})
    return {"selection": dict(_SEL)}


@mcp.tool()
def ase_select_ellipse(x: int, y: int, width: int, height: int) -> Dict[str, Any]:
    """Set the server-side selection to an ellipse (bounding box)."""
    _SEL.clear()
    _SEL.update({"kind": "ellipse", "x": x, "y": y, "w": width, "h": height})
    return {"selection": dict(_SEL)}


@mcp.tool()
def ase_select_all(sprite_path: str, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Select the whole cel."""
    data = _dump_cel(sprite_path, layer, frame)
    _SEL.clear()
    _SEL.update({"kind": "all", "w": data["width"], "h": data["height"]})
    return {"selection": dict(_SEL)}


@mcp.tool()
def ase_deselect() -> Dict[str, Any]:
    """Clear the server-side selection."""
    _SEL.clear()
    _SEL.update({"kind": None})
    return {"selection": dict(_SEL)}


@mcp.tool()
def ase_move_selection(dx: int, dy: int) -> Dict[str, Any]:
    """Move the selection bounds (not pixels)."""
    if _SEL.get("kind") in ("rect", "ellipse"):
        _SEL["x"] += dx
        _SEL["y"] += dy
    return {"selection": dict(_SEL)}


@mcp.tool()
def ase_get_selection() -> Dict[str, Any]:
    """Current selection state."""
    return {"selection": dict(_SEL), "clipboard": {k: v for k, v in _CLIP.items() if k != "pixels"}}


@mcp.tool()
def ase_copy_selection(sprite_path: str, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Copy the selected pixels to the server clipboard (keeps sprite intact)."""
    if _SEL.get("kind") is None:
        raise RuntimeError("no active selection")
    data = _dump_cel(sprite_path, layer, frame)
    w, h = data["width"], data["height"]
    mask = _sel_mask(w, h)
    px = [p for p in data["pixels"] if mask[p["y"] * w + p["x"]]]
    xs = [p["x"] for p in px] or [0]
    ys = [p["y"] for p in px] or [0]
    x0, y0 = min(xs), min(ys)
    _CLIP.clear()
    _CLIP.update({"w": max(xs) - x0 + 1, "h": max(ys) - y0 + 1, "pixels": [{**p, "x": p["x"] - x0, "y": p["y"] - y0} for p in px]})
    return {"ok": True, "width": _CLIP["w"], "height": _CLIP["h"], "pixels": len(px)}


@mcp.tool()
def ase_cut_selection(sprite_path: str, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Copy the selection to the clipboard and clear it in the cel."""
    r = ase_copy_selection(sprite_path, layer, frame)
    data = _dump_cel(sprite_path, layer, frame)
    w, h = data["width"], data["height"]
    mask = _sel_mask(w, h)
    pixels = [{"x": p["x"], "y": p["y"], "r": 0, "g": 0, "b": 0, "a": 0} for p in data["pixels"] if mask[p["y"] * w + p["x"]]]
    _draw_pixels(sprite_path, layer, frame, pixels, mode="replace")
    return r


@mcp.tool()
def ase_paste_clipboard(sprite_path: str, x: int = 0, y: int = 0, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Paste the server clipboard onto a cel at (x,y), respecting alpha."""
    if not _CLIP:
        raise RuntimeError("clipboard is empty")
    pixels = [{**p, "x": p["x"] + x, "y": p["y"] + y} for p in _CLIP["pixels"]]
    n = _draw_pixels(sprite_path, layer, frame, pixels, mode="merge")
    return {"ok": True, "pasted": n}


# --- pixel-art helpers ------------------------------------------------------
@mcp.tool()
def ase_apply_outline(sprite_path: str, color: str, thickness: int = 1, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Draw an outline around non-transparent pixels of a cel."""
    data = _dump_cel(sprite_path, layer, frame)
    w, h = data["width"], data["height"]
    alpha = {}
    for p in data["pixels"]:
        alpha[(p["x"], p["y"])] = p["a"]
    col = _rgba(color)
    pixels = []
    for y in range(h):
        for x in range(w):
            if (alpha.get((x, y), 0) or 0) > 0:
                continue
            near = False
            for oy in range(-thickness, thickness + 1):
                for ox in range(-thickness, thickness + 1):
                    if (alpha.get((x + ox, y + oy), 0) or 0) > 0:
                        near = True
                        break
                if near:
                    break
            if near:
                pixels.append({"x": x, "y": y, **col})
    _draw_pixels(sprite_path, layer, frame, pixels, mode="replace")
    return {"ok": True, "outlined": len(pixels)}


@mcp.tool()
def ase_apply_shading(sprite_path: str, light_direction: str = "top_left", base: str = "#e05830", shadow: str = "#983820", highlight: str = "#f89858", layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Directional cell shading: recolor opaque pixels of the base color toward
    shadow/highlight depending on which way the silhouette faces the light."""
    dirs = {
        "top_left": (-1, -1), "top": (0, -1), "top_right": (1, -1), "left": (-1, 0),
        "right": (1, 0), "bottom_left": (-1, 1), "bottom": (0, 1), "bottom_right": (1, 1),
    }
    lx, ly = dirs.get(light_direction, (-1, -1))
    data = _dump_cel(sprite_path, layer, frame)
    w, h = data["width"], data["height"]
    grid = {}
    for p in data["pixels"]:
        grid[(p["x"], p["y"])] = p
    bt = _rgba(base)
    pixels = []
    for p in data["pixels"]:
        if p["a"] == 0:
            continue
        if (p["r"], p["g"], p["b"]) != (bt["r"], bt["g"], bt["b"]):
            continue
        x, y = p["x"], p["y"]
        exposed_l = not (grid.get((x - lx, y), {}) or {}).get("a")
        exposed_r = not (grid.get((x + lx, y), {}) or {}).get("a")
        exposed_u = not (grid.get((x, y - ly), {}) or {}).get("a") if ly else False
        exposed_d = not (grid.get((x, y + ly), {}) or {}).get("a") if ly else False
        if exposed_l or exposed_u:
            pixels.append({"x": x, "y": y, **_rgba(highlight)})
        elif exposed_r or exposed_d:
            pixels.append({"x": x, "y": y, **_rgba(shadow)})
    _draw_pixels(sprite_path, layer, frame, pixels, mode="replace")
    return {"ok": True, "shaded": len(pixels)}


@mcp.tool()
def ase_suggest_antialiasing(sprite_path: str, layer: Optional[str] = None, frame: int = 1, apply: bool = False) -> Dict[str, Any]:
    """Detect jagged diagonal edges and suggest (or apply) 50% blend pixels."""
    data = _dump_cel(sprite_path, layer, frame)
    w, h = data["width"], data["height"]
    grid = {}
    for p in data["pixels"]:
        grid[(p["x"], p["y"])] = p
    sugg = []
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            c = grid.get((x, y))
            if not c or c["a"] == 0:
                continue
            for dx, dy in ((1, 1), (1, -1)):
                a = grid.get((x + dx, y))
                b = grid.get((x, y + dy))
                d = grid.get((x + dx, y + dy))
                if a and b and d and a["a"] == 0 and b["a"] == 0 and d["a"] > 0:
                    blend = {k: (c[k] + d[k]) // 2 for k in ("r", "g", "b")}
                    blend["a"] = 255
                    sugg.append({"x": x + dx, "y": y + dy, **blend})
    if apply and sugg:
        _draw_pixels(sprite_path, layer, frame, sugg, mode="replace")
    return {"suggestions": len(sugg), "applied": bool(apply and sugg)}


@mcp.tool()
def ase_analyze_palette_harmonies(palette: List[str]) -> Dict[str, Any]:
    """Color-wheel analysis: complementary pairs, triads, analogous groups, temperature."""
    import colorsys
    rgb = []
    for hx in palette:
        c = _rgba(hx)
        rgb.append((c["r"] / 255, c["g"] / 255, c["b"] / 255))
    hsv = [colorsys.rgb_to_hsv(*c) for c in rgb]
    comp, triad, analog = [], [], []
    for i in range(len(hsv)):
        for j in range(i + 1, len(hsv)):
            d = abs(hsv[i][0] - hsv[j][0]) * 360
            d = min(d, 360 - d)
            if 165 <= d <= 195:
                comp.append([palette[i], palette[j]])
            if 105 <= d <= 135:
                triad.append([palette[i], palette[j]])
            if d <= 30:
                analog.append([palette[i], palette[j]])
    warm = [p for p, c in zip(palette, hsv) if c[1] > 0.2 and (c[0] < 0.15 or c[0] > 0.85)]
    cool = [p for p, c in zip(palette, hsv) if c[1] > 0.2 and 0.4 < c[0] < 0.7]
    neutral = [p for p, c in zip(palette, hsv) if c[1] <= 0.2]
    return {"complementary": comp, "triadic": triad, "analogous": analog, "warm": warm, "cool": cool, "neutral": neutral}


# --- palette ops ------------------------------------------------------------
@mcp.tool()
def ase_get_palette(sprite_path: str) -> Dict[str, Any]:
    """Return the sprite palette as hex colors."""
    out = _px_json_path("pal")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local pal = spr.palettes[1]
local cols = {{}}
for i = 0, #pal - 1 do
  local c = pal:getColor(i)
  table.insert(cols, string.format("#%02x%02x%02x", c.red, c.green, c.blue))
end
local f = io.open({_lua_str(str(out))}, "wb")
f:write(json.encode({{ colors = cols }}))
f:close()
print("RD-MCP-OK palette")
"""
    _run_aseprite_lua(lua)
    data = json.loads(out.read_text(encoding="utf-8"))
    try:
        out.unlink()
    except OSError:
        pass
    return data


@mcp.tool()
def ase_set_palette(sprite_path: str, colors: List[str]) -> Dict[str, Any]:
    """Replace the sprite palette with the given hex colors."""
    cols = ", ".join(f"Color{{ r = {_rgba(c)['r']}, g = {_rgba(c)['g']}, b = {_rgba(c)['b']} }}" for c in colors)
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local pal = Palette({len(colors)})
local src = {{ {cols} }}
for i, c in ipairs(src) do
  pal:setColor(i - 1, c)
end
spr:setPalette(pal, false)
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK palette-set")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "colors": len(colors)}


@mcp.tool()
def ase_set_palette_color(sprite_path: str, index: int, color: str) -> Dict[str, Any]:
    """Set one palette entry by index."""
    c = _rgba(color)
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local pal = spr.palettes[1]
pal:setColor({int(index)}, Color{{ r = {c['r']}, g = {c['g']}, b = {c['b']} }})
spr:setPalette(pal, false)
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK palette-color")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "index": index, "color": color}


@mcp.tool()
def ase_add_palette_color(sprite_path: str, color: str) -> Dict[str, Any]:
    """Append a color to the sprite palette (grows by one)."""
    cur = ase_get_palette(sprite_path)["colors"]
    cur.append(color)
    ase_set_palette(sprite_path, cur)
    return {"ok": True, "index": len(cur) - 1}


@mcp.tool()
def ase_sort_palette(sprite_path: str, method: str = "hue", ascending: bool = True) -> Dict[str, Any]:
    """Sort the sprite palette by hue|saturation|brightness|luminance."""
    import colorsys
    cur = ase_get_palette(sprite_path)["colors"]
    def key(hx: str) -> float:
        c = _rgba(hx)
        r, g, b = c["r"] / 255, c["g"] / 255, c["b"] / 255
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return {"hue": h, "saturation": s, "brightness": v, "luminance": lum}[method]
    cur.sort(key=key, reverse=not ascending)
    ase_set_palette(sprite_path, cur)
    return {"ok": True, "colors": cur}


# --- frames / layers / tags -------------------------------------------------
def _lua_dur_helpers() -> str:
    """Aseprite 1.3+ stores Frame.duration in SECONDS; older builds in ms.
    Probe the running build once per script and convert accordingly."""
    return """
local _dur_unit = nil
local function dur_unit()
  if _dur_unit == nil then
    local s = Sprite(2, 2)
    s.frames[1].duration = 0.5
    _dur_unit = 1000
    if math.abs(s.frames[1].duration - 0.5) < 0.001 then _dur_unit = 1 end
    s:close()
  end
  return _dur_unit
end
local function set_dur(fr, ms)
  fr.duration = (ms / 1000.0) * dur_unit()
end
local function get_dur_ms(fr)
  return math.floor(fr.duration * 1000 / dur_unit() + 0.5)
end
"""


@mcp.tool()
def ase_add_frame(sprite_path: str, duration_ms: int = 100) -> Dict[str, Any]:
    """Append a frame; returns the new frame count."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_lua_dur_helpers()}
spr:newFrame()
set_dur(spr.frames[#spr.frames], {int(duration_ms)})
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK frames=" .. #spr.frames)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "frames": int(out.strip().split("frames=")[-1])}


@mcp.tool()
def ase_delete_frame(sprite_path: str, frame: int) -> Dict[str, Any]:
    """Delete a 1-based frame."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
if #spr.frames <= 1 then error("cannot delete last frame") end
spr:deleteFrame(spr.frames[{int(frame)}])
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK frames=" .. #spr.frames)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "frames": int(out.strip().split("frames=")[-1])}


@mcp.tool()
def ase_duplicate_frame(sprite_path: str, source_frame: int, insert_after: int = 0) -> Dict[str, Any]:
    """Duplicate a frame; insert_after=0 appends at the end."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local src = spr.frames[{int(source_frame)}]
local pos = {int(insert_after)}
if pos <= 0 or pos >= #spr.frames then pos = #spr.frames end
spr:newFrame(pos + 1)
for i, layer in ipairs(spr.layers) do
  local cel = layer:cel(src)
  if cel then
    local img = Image(cel.image)
    if layer:cel(spr.frames[pos + 1]) == nil then
      spr:newCel(layer, spr.frames[pos + 1], img, Point(cel.position.x, cel.position.y))
    end
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK frames=" .. #spr.frames)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "frames": int(out.strip().split("frames=")[-1])}


@mcp.tool()
def ase_set_frame_duration(sprite_path: str, frame: int, duration_ms: int) -> Dict[str, Any]:
    """Set a frame's duration in ms."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_lua_dur_helpers()}
set_dur(spr.frames[{int(frame)}], {int(duration_ms)})
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK duration=" .. get_dur_ms(spr.frames[{int(frame)}]) .. "ms")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "frame": frame, "duration_ms": duration_ms}


@mcp.tool()
def ase_create_tag(sprite_path: str, tag_name: str, from_frame: int, to_frame: int, direction: str = "forward") -> Dict[str, Any]:
    """Create an animation tag (forward|reverse|pingpong)."""
    if direction not in ("forward", "reverse", "pingpong"):
        raise ValueError("direction must be forward|reverse|pingpong")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local tag = spr:newTag(spr.frames[{int(from_frame)}], spr.frames[{int(to_frame)}])
tag.name = {_lua_str(tag_name)}
tag.aniDir = ({{ forward = 0, reverse = 1, pingpong = 2 }})["{direction}"] or 0
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK tag")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "tag": tag_name}


@mcp.tool()
def ase_delete_tag(sprite_path: str, tag_name: str) -> Dict[str, Any]:
    """Delete an animation tag by name."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
for _, t in ipairs(spr.tags) do
  if t.name == {_lua_str(tag_name)} then spr:deleteTag(t) break end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK tag-deleted")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "tag": tag_name}


@mcp.tool()
def ase_add_layer(sprite_path: str, layer_name: str) -> Dict[str, Any]:
    """Add a new image layer."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local layer = spr:newLayer()
layer.name = {_lua_str(layer_name)}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK layer")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": layer_name}


@mcp.tool()
def ase_delete_layer(sprite_path: str, layer_name: str) -> Dict[str, Any]:
    """Delete a layer by name."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
if #spr.layers <= 1 then error("cannot delete last layer") end
for _, l in ipairs(spr.layers) do
  if l.name == {_lua_str(layer_name)} then spr:deleteLayer(l) break end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK layer-deleted")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": layer_name}


@mcp.tool()
def ase_flatten_layers(sprite_path: str) -> Dict[str, Any]:
    """Flatten all layers into one."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
app.command.FlattenLayers()
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK flattened")
"""
    _run_aseprite_lua(lua)
    return {"ok": True}


@mcp.tool()
def ase_link_cel(sprite_path: str, layer_name: str, source_frame: int, target_frame: int) -> Dict[str, Any]:
    """Make a cel linked (shared image) with another frame's cel."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local layer = nil
for _, l in ipairs(spr.layers) do if l.name == {_lua_str(layer_name)} then layer = l end end
if layer == nil then
  error("layer not found: {_lua_str(layer_name)}")
end
if spr.frames[{int(source_frame)}] == nil or spr.frames[{int(target_frame)}] == nil then
  error("frame index out of range (sprite has " .. #spr.frames .. " frames)")
end
local srcCel = layer:cel(spr.frames[{int(source_frame)}])
if srcCel == nil then
  error("no cel on layer '" .. layer.name .. "' frame {int(source_frame)}; draw or import something there first")
end
spr:newCel(layer, spr.frames[{int(target_frame)}], srcCel.image, Point(srcCel.position.x, srcCel.position.y))
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK linked")
"""
    _run_aseprite_lua(lua)
    return {"ok": True}


# --- transforms -------------------------------------------------------------
@mcp.tool()
def ase_scale_sprite(sprite_path: str, scale_x: float, scale_y: float, algorithm: str = "nearest") -> Dict[str, Any]:
    """Scale the whole sprite (nearest|bilinear)."""
    meth = "nearest" if algorithm == "nearest" else "bilinear"
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
spr:resize{{ width = math.floor(spr.width * {float(scale_x)}), height = math.floor(spr.height * {float(scale_y)}), method = "{meth}" }}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK resized " .. spr.width .. "x" .. spr.height)
"""
    out = _run_aseprite_lua(lua)
    dims = out.strip().split("resized ")[-1]
    w, h = dims.split("x")
    return {"ok": True, "width": int(w), "height": int(h)}


@mcp.tool()
def ase_downsample(sprite_path: str, width: int, height: int, out_path: Optional[str] = None) -> Dict[str, Any]:
    """Box-ish downsample to target size (bilinear then nearest for pixel art)."""
    dst = out_path or sprite_path
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
spr:resize{{ width = {int(width)}, height = {int(height)}, method = "bilinear" }}
spr:saveAs({_lua_str(dst)})
print("RD-MCP-OK downsampled")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "path": dst, "width": width, "height": height}


@mcp.tool()
def ase_resize_canvas(sprite_path: str, width: int, height: int, anchor: str = "center") -> Dict[str, Any]:
    """Resize canvas without scaling content (anchor: center|top_left|top_right|bottom_left|bottom_right)."""
    if anchor not in ("center", "top_left", "top_right", "bottom_left", "bottom_right"):
        raise ValueError("anchor must be center|top_left|top_right|bottom_left|bottom_right")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local W, H = {int(width)}, {int(height)}
local ow, oh = spr.width, spr.height
local left, top = 0, 0
if "{anchor}" == "center" then
  left = math.floor((W - ow) / 2)
  top = math.floor((H - oh) / 2)
elseif "{anchor}" == "top_right" then
  left = W - ow
elseif "{anchor}" == "bottom_left" then
  top = H - oh
elseif "{anchor}" == "bottom_right" then
  left = W - ow
  top = H - oh
end
local right = W - ow - left
local bottom = H - oh - top
app.command.CanvasSize{{ left = left, top = top, right = right, bottom = bottom }}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK canvas " .. spr.width .. "x" .. spr.height)
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "width": width, "height": height}


@mcp.tool()
def ase_rotate_sprite(sprite_path: str, angle: int, target: str = "sprite") -> Dict[str, Any]:
    """Rotate 90/180/270 degrees clockwise (sprite or active layer)."""
    if angle not in (90, 180, 270):
        raise ValueError("angle must be 90, 180 or 270")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
app.command.Rotate{{ target = {_lua_str(target)}, angle = {int(angle)} }}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK rotated")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "angle": angle}


@mcp.tool()
def ase_flip_sprite(sprite_path: str, direction: str = "horizontal", target: str = "sprite") -> Dict[str, Any]:
    """Flip horizontally|vertically (sprite or active layer)."""
    horiz = "true" if direction == "horizontal" else "false"
    cond = "true" if target == "sprite" else f"layer.name == {_lua_str(str(target))}"
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local function flipImg(img, horiz)
  local copy = Image(img)
  local w, h = img.width, img.height
  for y = 0, h - 1 do
    for x = 0, w - 1 do
      local sx, sy = x, y
      if horiz then sx = w - 1 - x else sy = h - 1 - y end
      img:drawPixel(sx, sy, copy:getPixel(x, y))
    end
  end
end
for _, layer in ipairs(spr.layers) do
  if {cond} then
    for _, cel in ipairs(layer.cels) do
      flipImg(cel.image, {horiz})
    end
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK flipped")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "direction": direction}


@mcp.tool()
def ase_crop_sprite(sprite_path: str, x: int, y: int, width: int, height: int) -> Dict[str, Any]:
    """Crop the sprite to a rectangle."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
spr:crop({int(x)}, {int(y)}, {int(width)}, {int(height)})
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK cropped")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "width": width, "height": height}


@mcp.tool()
def ase_trim_sprite(sprite_path: str, by_grid: bool = False) -> Dict[str, Any]:
    """Trim transparent borders (optionally by grid boundaries)."""
    grid = "true" if by_grid else "false"
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local minX, minY, maxX, maxY = nil, nil, -1, -1
for _, layer in ipairs(spr.layers) do
  for _, cel in ipairs(layer.cels) do
    local img = cel.image
    local ox, oy = cel.position.x, cel.position.y
    for y = 0, img.height - 1 do
      for x = 0, img.width - 1 do
        if app.pixelColor.rgbaA(img:getPixel(x, y)) > 0 then
          local gx, gy = x + ox, y + oy
          if minX == nil or gx < minX then minX = gx end
          if minY == nil or gy < minY then minY = gy end
          if gx > maxX then maxX = gx end
          if gy > maxY then maxY = gy end
        end
      end
    end
  end
end
if minX ~= nil then
  local w, h = maxX - minX + 1, maxY - minY + 1
  if {grid} then
    w = math.ceil(w / 8) * 8
    h = math.ceil(h / 8) * 8
  end
  spr:crop(minX, minY, w, h)
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK trimmed " .. spr.width .. "x" .. spr.height)
"""
    out = _run_aseprite_lua(lua)
    dims = out.strip().split("trimmed ")[-1]
    w, h = dims.split("x")
    return {"ok": True, "width": int(w), "height": int(h)}


@mcp.tool()
def ase_color_mode(sprite_path: str, mode: str, dithering: str = "none", matrix: str = "bayer4x4") -> Dict[str, Any]:
    """Convert color mode: rgb|grayscale|indexed, with optional ordered dithering
    (none|ordered) and bayer2x2|bayer4x4|bayer8x8 matrix."""
    modes = {"rgb": "rgb", "grayscale": "grayscale", "indexed": "indexed"}
    if mode not in modes:
        raise ValueError("mode must be rgb|grayscale|indexed")
    dith = {"none": "DitheringAlgorithm.NONE", "ordered": "DitheringAlgorithm.ORDERED"}.get(dithering)
    if dith is None:
        raise ValueError("dithering must be none|ordered")
    mats = {"bayer2x2": 'app.command.ChangePixelFormat{ format = "%s", ditheringAlgorithm = %s, ditheringMatrix = "%s" }' % (modes[mode], dith, "bayer2x2"),
            "bayer4x4": 'app.command.ChangePixelFormat{ format = "%s", ditheringAlgorithm = %s, ditheringMatrix = "%s" }' % (modes[mode], dith, "bayer4x4"),
            "bayer8x8": 'app.command.ChangePixelFormat{ format = "%s", ditheringAlgorithm = %s, ditheringMatrix = "%s" }' % (modes[mode], dith, "bayer8x8")}
    call = mats.get(matrix, mats["bayer4x4"]) if dithering == "ordered" else 'app.command.ChangePixelFormat{ format = "%s" }' % modes[mode]
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{call}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK colormode")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "mode": mode, "dithering": dithering}


# --- export -----------------------------------------------------------------
@mcp.tool()
def ase_export_sprite(sprite_path: str, output_path: str, format: str = "png", frame: int = 0) -> Dict[str, Any]:
    """Export sprite to png|gif|jpg|bmp; frame=0 exports the animation."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
spr:saveAs({_lua_str(output_path)})
print("RD-MCP-OK exported")
"""
    if format == "gif" or frame == 0:
        _run_aseprite_lua(lua)
    else:
        lua2 = f"""
local spr = app.open({_lua_str(sprite_path)})
local layer = spr.layers[1]
local cel = layer:cel(spr.frames[{int(frame)}])
cel.image:saveAs({_lua_str(output_path)})
print("RD-MCP-OK exported-frame")
"""
        _run_aseprite_lua(lua2)
    return {"ok": True, "path": output_path, "format": format}


@mcp.tool()
def ase_export_spritesheet(sprite_path: str, output_path: str, layout: str = "horizontal", padding: int = 0, include_json: bool = True) -> Dict[str, Any]:
    """Export a spritesheet (horizontal|vertical|rows|columns|packed) with optional JSON metadata."""
    json_path = str(Path(output_path).with_suffix(".json"))
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
app.command.ExportSpriteSheet{{
  type = {_lua_str(layout)},
  textureFilename = {_lua_str(output_path)},
  shapePadding = {int(padding)}
}}
print("RD-MCP-OK sheet")
"""
    _run_aseprite_lua(lua)
    if include_json:
        # This Aseprite build's Lua ExportSpriteSheet ignores data/dataFormat,
        # so author Aseprite-compatible json-hash metadata ourselves.
        info = ase_sprite_info(sprite_path)
        pil = Image.open(output_path)
        sw, sh = pil.size
        w, h = info["width"], info["height"]
        frames: Dict[str, Any] = {}
        for i, fr in enumerate(info["frames"]):
            if layout == "horizontal" and padding == 0:
                x, y = i * w, 0
            elif layout == "vertical" and padding == 0:
                x, y = 0, i * h
            else:
                x, y = -1, -1  # rects unknown for padded/packed layouts
            frames[f"frame_{i}"] = {
                "frame": {"x": x, "y": y, "w": w, "h": h},
                "rotated": False,
                "trimmed": False,
                "spriteSourceSize": {"x": 0, "y": 0, "w": w, "h": h},
                "sourceSize": {"w": w, "h": h},
                "duration": fr["duration_ms"],
            }
        meta = {
            "app": "pixelworks-mcp",
            "version": "1.0",
            "image": Path(output_path).name,
            "format": "RGBA8888",
            "size": {"w": sw, "h": sh},
            "scale": "1",
            "frameTags": [
                {"name": t["name"], "from": t["from"] - 1, "to": t["to"] - 1, "direction": t["direction"]}
                for t in info["tags"]
            ],
            "layers": [{"name": l["name"]} for l in info["layers"]],
        }
        Path(json_path).write_text(json.dumps({"frames": frames, "meta": meta}, indent=2), encoding="utf-8")
    return {"ok": True, "sheet": output_path, "json": json_path if include_json else None}




# ---------------------------------------------------------------------------
# Aseprite workbench v4 expansion
#
# Ported capabilities, reimplemented for this server's headless single-file
# architecture, from diivi/aseprite-mcp (MIT License,
# https://github.com/diivi/aseprite-mcp): layer management, color effects,
# retro palette presets + hue-shifted ramps + quantization, cel lifecycle/
# position/opacity + tweening + oscillation, slices (9-patch), tilemaps,
# onion-skin/frame-diff/color-stats analysis, per-layer & per-tag export,
# and the raw-Lua escape hatch. Shared Lua idioms (find_layer BFS,
# normalize_cel, RGB-HSL conversion, Bayer dither) derive from that
# project's core/lua.py and tool modules.
# ---------------------------------------------------------------------------
_LUA_FIND_LAYER = """
local function find_layer(spr, name)
  local queue = {}
  for _, l in ipairs(spr.layers) do queue[#queue + 1] = l end
  local head = 1
  while head <= #queue do
    local l = queue[head]
    head = head + 1
    if l.name == name then return l end
    if l.isGroup then
      for _, c in ipairs(l.layers) do queue[#queue + 1] = c end
    end
  end
  return nil
end
"""

_LUA_NORMALIZE_CEL = """
local function normalize_cel(spr, layer, frame, create)
  local cel = layer:cel(frame)
  if cel == nil then
    if not create then return nil end
    local img = Image(spr.width, spr.height, spr.colorMode)
    return spr:newCel(layer, frame, img, Point(0, 0))
  end
  if cel.position.x == 0 and cel.position.y == 0
     and cel.image.width == spr.width and cel.image.height == spr.height then
    return cel
  end
  local img = Image(spr.width, spr.height, spr.colorMode)
  img:drawImage(cel.image, cel.position)
  cel.image = img
  cel.position = Point(0, 0)
  return cel
end
"""

_LUA_HSL = """
local function rgb_to_hsl(r, g, b)
  r, g, b = r / 255, g / 255, b / 255
  local maxc = math.max(r, g, b)
  local minc = math.min(r, g, b)
  local l = (maxc + minc) / 2
  if maxc == minc then return 0, 0, l end
  local d = maxc - minc
  local s
  if l > 0.5 then s = d / (2 - maxc - minc) else s = d / (maxc + minc) end
  local h
  if maxc == r then
    h = (g - b) / d
    if g < b then h = h + 6 end
  elseif maxc == g then
    h = (b - r) / d + 2
  else
    h = (r - g) / d + 4
  end
  return h * 60, s, l
end

local function hsl_to_rgb(h, s, l)
  h = h % 360
  if s <= 0 then
    local v = math.floor(l * 255 + 0.5)
    return v, v, v
  end
  local c = (1 - math.abs(2 * l - 1)) * s
  local hp = h / 60
  local x = c * (1 - math.abs(hp % 2 - 1))
  local r1, g1, b1 = 0, 0, 0
  if hp < 1 then r1, g1, b1 = c, x, 0
  elseif hp < 2 then r1, g1, b1 = x, c, 0
  elseif hp < 3 then r1, g1, b1 = 0, c, x
  elseif hp < 4 then r1, g1, b1 = 0, x, c
  elseif hp < 5 then r1, g1, b1 = x, 0, c
  else r1, g1, b1 = c, 0, x end
  local m = l - c / 2
  return math.floor((r1 + m) * 255 + 0.5),
         math.floor((g1 + m) * 255 + 0.5),
         math.floor((b1 + m) * 255 + 0.5)
end
"""


def _lua_layer_one(layer: Optional[str], var: str = "layer") -> str:
    """Lua block resolving ONE layer: by name (group paths supported, hard
    error when missing) or, when no name is given, the first image layer."""
    if layer:
        return f"""
local {var} = find_layer(spr, {_lua_str(layer)})
if {var} == nil then error("layer not found") end
"""
    return f"""
local {var} = nil
for _, l in ipairs(spr.layers) do if l.isImage then {var} = l break end end
if {var} == nil then {var} = spr.layers[1] end
"""


def _lua_layers_all(layer: Optional[str]) -> str:
    """Lua block producing a layers list: one named layer, or every image
    layer (recursing into groups) when no name is given."""
    if layer:
        return f"""
local target = find_layer(spr, {_lua_str(layer)})
if target == nil then error("layer not found") end
local layers = {{ target }}
"""
    return """
local layers = {}
local function collect_layers(list)
  for _, l in ipairs(list) do
    if l.isImage then layers[#layers + 1] = l
    elseif l.isGroup then collect_layers(l.layers) end
  end
end
collect_layers(spr.layers)
"""


def _lua_frames_range(frame: int) -> str:
    """Lua block producing f0/f1 frame bounds; frame<=0 means all frames."""
    f = int(frame)
    f0 = f if f > 0 else 1
    f1 = f if f > 0 else 0
    return f"""
local f0, f1 = {f0}, {f1}
if f1 < f0 then f1 = #spr.frames end
if f1 > #spr.frames then error("frame out of range") end
"""


def _run_aseprite_cli(args: List[str], timeout: int = 180) -> str:
    """Run the Aseprite CLI with arbitrary arguments (no Lua script)."""
    if not ASEPRITE_EXE.exists():
        raise RuntimeError(f"Aseprite executable not found at {ASEPRITE_EXE}")
    env = dict(os.environ)
    env["RD_MCP_HEADLESS"] = "1"
    proc = subprocess.run(
        [str(ASEPRITE_EXE)] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
        env=env,
        **_hide_startup(),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(f"Aseprite CLI failed (rc={proc.returncode}): {out[-2000:]}")
    return out


ASE_PALETTE_PRESETS: Dict[str, List[str]] = {
    "gameboy": ["#0F380F", "#306230", "#8BAC0F", "#9BBC0F"],
    "monochrome": ["#000000", "#FFFFFF"],
    "grayscale_4": ["#000000", "#555555", "#AAAAAA", "#FFFFFF"],
    "cga": ["#000000", "#55FFFF", "#FF55FF", "#FFFFFF"],
    "pico8": [
        "#000000", "#1D2B53", "#7E2553", "#008751",
        "#AB5236", "#5F574F", "#C2C3C7", "#FFF1E8",
        "#FF004D", "#FFA300", "#FFEC27", "#00E436",
        "#29ADFF", "#83769C", "#FF77A8", "#FFCCAA",
    ],
    "c64": [
        "#000000", "#FFFFFF", "#880000", "#AAFFEE",
        "#CC44CC", "#00CC55", "#0000AA", "#EEEE77",
        "#DD8855", "#664400", "#FF7777", "#333333",
        "#777777", "#AAFF66", "#0088FF", "#BBBBBB",
    ],
    "dawnbringer16": [
        "#140C1C", "#442434", "#30346D", "#4E4A4E",
        "#854C30", "#346524", "#D04648", "#757161",
        "#597DCE", "#D27D2C", "#8595A1", "#6DAA2C",
        "#D2AA99", "#6DC2CA", "#DAD45E", "#DEEED6",
    ],
    "dawnbringer32": [
        "#000000", "#222034", "#45283C", "#663931",
        "#8F563B", "#DF7126", "#D9A066", "#EEC39A",
        "#FBF236", "#99E550", "#6ABE30", "#37946E",
        "#4B692F", "#524B24", "#323C39", "#3F3F74",
        "#306082", "#5B6EE1", "#639BFF", "#5FCDE4",
        "#CBDBFC", "#FFFFFF", "#9BADB7", "#847E87",
        "#696A6A", "#595652", "#76428A", "#AC3232",
        "#D95763", "#D77BBA", "#8F974A", "#8A6F30",
    ],
}

_ASE_BLEND_MODES = {
    "normal": "BlendMode.NORMAL", "darken": "BlendMode.DARKEN",
    "multiply": "BlendMode.MULTIPLY", "color_burn": "BlendMode.COLOR_BURN",
    "lighten": "BlendMode.LIGHTEN", "screen": "BlendMode.SCREEN",
    "color_dodge": "BlendMode.COLOR_DODGE", "addition": "BlendMode.ADDITION",
    "overlay": "BlendMode.OVERLAY", "soft_light": "BlendMode.SOFT_LIGHT",
    "hard_light": "BlendMode.HARD_LIGHT", "difference": "BlendMode.DIFFERENCE",
    "exclusion": "BlendMode.EXCLUSION", "subtract": "BlendMode.SUBTRACT",
    "divide": "BlendMode.DIVIDE", "hue": "BlendMode.HSL_HUE",
    "saturation": "BlendMode.HSL_SATURATION", "color": "BlendMode.HSL_COLOR",
    "luminosity": "BlendMode.HSL_LUMINOSITY",
}


# --- layer management ---------------------------------------------------------
@mcp.tool()
def ase_rename_layer(sprite_path: str, layer_name: str, new_name: str) -> Dict[str, Any]:
    """Rename a layer (accepts group/child paths)."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
target.name = {_lua_str(new_name)}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK renamed")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": new_name}


@mcp.tool()
def ase_duplicate_layer(sprite_path: str, layer_name: str, new_name: str = "") -> Dict[str, Any]:
    """Duplicate a layer with independent copies of all its cels, plus opacity
    and blend mode. The copy lands directly above the source; default name is
    the source name plus a copy suffix."""
    final_name = new_name or (layer_name + " copy")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local src = find_layer(spr, {_lua_str(layer_name)})
if src == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
local copy = spr:newLayer()
copy.name = {_lua_str(final_name)}
copy.opacity = src.opacity
copy.blendMode = src.blendMode
copy.stackIndex = src.stackIndex + 1
for _, frame in ipairs(spr.frames) do
  local cel = src:cel(frame)
  if cel then
    local c = spr:newCel(copy, frame, cel.image:clone(), cel.position)
    c.opacity = cel.opacity
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK duplicated")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": final_name}


@mcp.tool()
def ase_reorder_layer(sprite_path: str, layer_name: str, position: int) -> Dict[str, Any]:
    """Move a layer to a 1-based stack position (1 = bottom)."""
    if int(position) < 1:
        raise ValueError("position must be >= 1")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
if {int(position)} > #spr.layers then error("position out of range") end
target.stackIndex = {int(position)}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK reordered")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": layer_name, "position": int(position)}


@mcp.tool()
def ase_set_layer_blend_mode(sprite_path: str, layer_name: str, mode: str) -> Dict[str, Any]:
    """Set a layer's blend mode: normal, darken, multiply, color_burn, lighten,
    screen, color_dodge, addition, overlay, soft_light, hard_light, difference,
    exclusion, subtract, divide, hue, saturation, color, luminosity."""
    blend = _ASE_BLEND_MODES.get(str(mode).lower())
    if blend is None:
        raise ValueError("unknown blend mode; valid: " + ", ".join(sorted(_ASE_BLEND_MODES)))
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
target.blendMode = {blend}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK blend")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": layer_name, "mode": str(mode).lower()}


@mcp.tool()
def ase_merge_layer_down(sprite_path: str, layer_name: str) -> Dict[str, Any]:
    """Merge a layer into the layer directly below it (must not be bottom)."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
if target.stackIndex <= 1 then error("layer is the bottom layer; nothing to merge into") end
app.activeLayer = target
app.command.MergeDownLayer()
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK merged layers=" .. #spr.layers)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "layers": int(out.strip().split("layers=")[-1])}


@mcp.tool()
def ase_set_layer_opacity(sprite_path: str, layer_name: str, opacity: int) -> Dict[str, Any]:
    """Set layer opacity 0-255."""
    op = max(0, min(255, int(opacity)))
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
target.opacity = {op}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK opacity")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": layer_name, "opacity": op}


@mcp.tool()
def ase_set_layer_visibility(sprite_path: str, layer_name: str, visible: bool = True) -> Dict[str, Any]:
    """Show or hide a layer."""
    vis = "true" if visible else "false"
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
target.isVisible = {vis}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK visibility")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": layer_name, "visible": bool(visible)}


# --- color effects -------------------------------------------------------------
@mcp.tool()
def ase_replace_color(
    sprite_path: str,
    from_color: str,
    to_color: str,
    layer: Optional[str] = None,
    frame: int = 0,
    tolerance: int = 0,
) -> Dict[str, Any]:
    """Replace one color with another across a layer (None = all layers),
    preserving alpha. frame=0 processes every frame. tolerance is per-channel
    0-255. Returns the number of pixels replaced."""
    src = _rgba(from_color)
    dst = _rgba(to_color)
    tol = max(0, min(255, int(tolerance)))
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_lua_layers_all(layer)}
{_lua_frames_range(frame)}
local count = 0
for _, l in ipairs(layers) do
  for fi = f0, f1 do
    local cel = l:cel(spr.frames[fi])
    if cel then
      local img = cel.image
      for py = 0, img.height - 1 do
        for px = 0, img.width - 1 do
          local v = img:getPixel(px, py)
          local a = app.pixelColor.rgbaA(v)
          if a > 0 then
            local dr = math.abs(app.pixelColor.rgbaR(v) - {src['r']})
            local dg = math.abs(app.pixelColor.rgbaG(v) - {src['g']})
            local db = math.abs(app.pixelColor.rgbaB(v) - {src['b']})
            if dr <= {tol} and dg <= {tol} and db <= {tol} then
              img:putPixel(px, py, app.pixelColor.rgba({dst['r']}, {dst['g']}, {dst['b']}, a))
              count = count + 1
            end
          end
        end
      end
    end
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK count=" .. count)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "replaced": int(out.strip().split("count=")[-1])}


@mcp.tool()
def ase_adjust_hsl(
    sprite_path: str,
    hue_shift: float = 0.0,
    saturation_shift: float = 0.0,
    lightness_shift: float = 0.0,
    layer: Optional[str] = None,
    frame: int = 0,
) -> Dict[str, Any]:
    """Shift hue (-360..360 deg), saturation (-100..100) and lightness
    (-100..100) of all opaque pixels: palette swaps, night scenes, shadow
    layers. layer None = all layers; frame=0 processes every frame."""
    h = float(hue_shift)
    s = float(saturation_shift)
    li = float(lightness_shift)
    if not (-360 <= h <= 360):
        raise ValueError("hue_shift must be between -360 and 360")
    if not (-100 <= s <= 100) or not (-100 <= li <= 100):
        raise ValueError("saturation_shift/lightness_shift must be between -100 and 100")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_LUA_HSL}
{_lua_layers_all(layer)}
{_lua_frames_range(frame)}
for _, ly in ipairs(layers) do
  for fi = f0, f1 do
    local cel = ly:cel(spr.frames[fi])
    if cel then
      local img = cel.image
      for py = 0, img.height - 1 do
        for px = 0, img.width - 1 do
          local v = img:getPixel(px, py)
          local a = app.pixelColor.rgbaA(v)
          if a > 0 then
            local hh, ss, ll = rgb_to_hsl(app.pixelColor.rgbaR(v), app.pixelColor.rgbaG(v), app.pixelColor.rgbaB(v))
            hh = hh + ({h})
            ss = math.min(1, math.max(0, ss + ({s}) / 100))
            ll = math.min(1, math.max(0, ll + ({li}) / 100))
            local nr, ng, nb = hsl_to_rgb(hh, ss, ll)
            img:putPixel(px, py, app.pixelColor.rgba(nr, ng, nb, a))
          end
        end
      end
    end
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK hsl")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "hue_shift": h, "saturation_shift": s, "lightness_shift": li}


@mcp.tool()
def ase_erase_color(
    sprite_path: str,
    color: str,
    layer: Optional[str] = None,
    frame: int = 0,
    tolerance: int = 0,
) -> Dict[str, Any]:
    """Magic eraser: make every pixel matching a color fully transparent
    (per-channel tolerance 0-255). layer None = all layers, frame=0 = every
    frame. Returns the number of pixels erased."""
    c = _rgba(color)
    tol = max(0, min(255, int(tolerance)))
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_lua_layers_all(layer)}
{_lua_frames_range(frame)}
local count = 0
for _, l in ipairs(layers) do
  for fi = f0, f1 do
    local cel = l:cel(spr.frames[fi])
    if cel then
      local img = cel.image
      for py = 0, img.height - 1 do
        for px = 0, img.width - 1 do
          local v = img:getPixel(px, py)
          if app.pixelColor.rgbaA(v) > 0 then
            local dr = math.abs(app.pixelColor.rgbaR(v) - {c['r']})
            local dg = math.abs(app.pixelColor.rgbaG(v) - {c['g']})
            local db = math.abs(app.pixelColor.rgbaB(v) - {c['b']})
            if dr <= {tol} and dg <= {tol} and db <= {tol} then
              img:putPixel(px, py, app.pixelColor.rgba(0, 0, 0, 0))
              count = count + 1
            end
          end
        end
      end
    end
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK count=" .. count)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "erased": int(out.strip().split("count=")[-1])}


@mcp.tool()
def ase_dither_gradient(
    sprite_path: str,
    x: int,
    y: int,
    width: int,
    height: int,
    color_start: str,
    color_end: str,
    horizontal: bool = False,
    layer: Optional[str] = None,
    frame: int = 1,
) -> Dict[str, Any]:
    """Fill a rectangle with a two-color Bayer 4x4 ordered-dither gradient
    (the classic pixel-art blend: no new intermediate colors). Top-to-bottom
    by default; horizontal=true runs left-to-right. Coordinates are
    sprite-global (the cel is normalized to the canvas)."""
    if int(width) <= 0 or int(height) <= 0:
        raise ValueError("width and height must be > 0")
    c1 = _rgba(color_start)
    c2 = _rgba(color_end)
    axis = ("px - " + str(int(x))) if horizontal else ("py - " + str(int(y)))
    span = int(width) if horizontal else int(height)
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_LUA_NORMALIZE_CEL}
{_lua_layer_one(layer)}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local cel = normalize_cel(spr, layer, spr.frames[idx], true)
local img = cel.image
local bayer = {{
  {{ 0,  8,  2, 10}},
  {{12,  4, 14,  6}},
  {{ 3, 11,  1,  9}},
  {{15,  7, 13,  5}},
}}
local ca = app.pixelColor.rgba({c1['r']}, {c1['g']}, {c1['b']}, 255)
local cb = app.pixelColor.rgba({c2['r']}, {c2['g']}, {c2['b']}, 255)
for py = {int(y)}, {int(y)} + {int(height)} - 1 do
  for px = {int(x)}, {int(x)} + {int(width)} - 1 do
    if px >= 0 and py >= 0 and px < img.width and py < img.height then
      local f = ({axis}) / math.max(1, {span} - 1)
      local threshold = (bayer[(py % 4) + 1][(px % 4) + 1] + 0.5) / 16
      if f >= threshold then img:putPixel(px, py, cb) else img:putPixel(px, py, ca) end
    end
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK dither-gradient")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "x": int(x), "y": int(y), "width": int(width), "height": int(height)}


# --- retro palettes & ramps -----------------------------------------------------
@mcp.tool()
def ase_list_palette_presets() -> Dict[str, Any]:
    """List the built-in retro palette presets (gameboy, pico8, c64, cga,
    dawnbringer16/32, grayscale_4, monochrome) with their hex colors.
    Pure data: works without Aseprite."""
    return {"presets": ASE_PALETTE_PRESETS}


@mcp.tool()
def ase_apply_palette_preset(sprite_path: str, preset: str) -> Dict[str, Any]:
    """Set the sprite palette to a built-in retro preset. Only the palette is
    changed; snap existing pixels with ase_quantize_to_palette afterwards."""
    colors = ASE_PALETTE_PRESETS.get(str(preset).lower())
    if colors is None:
        raise ValueError("unknown preset; available: " + ", ".join(sorted(ASE_PALETTE_PRESETS)))
    ase_set_palette(sprite_path, colors)
    return {"ok": True, "preset": str(preset).lower(), "colors": len(colors)}


@mcp.tool()
def ase_generate_color_ramp(
    base_color: str,
    steps: int = 5,
    hue_shift_degrees: float = 20.0,
    lightness_range: float = 0.5,
) -> Dict[str, Any]:
    """Generate a dark-to-light shading ramp around a base color using the
    standard pixel-art hue-shift technique: shadows lean cooler, highlights
    lean warmer, saturation tapers toward the light end. Pure computation
    (no Aseprite needed); feed the result to ase_set_palette or shading ops.

    Returns the ramp darkest-first."""
    import colorsys
    c = _rgba(base_color)
    n = int(steps)
    if not (2 <= n <= 16):
        raise ValueError("steps must be between 2 and 16")
    lr = float(lightness_range)
    if not (0 <= lr <= 1):
        raise ValueError("lightness_range must be between 0 and 1")
    r, g, b = c["r"] / 255, c["g"] / 255, c["b"] / 255
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    ramp: List[str] = []
    mid = (n - 1) / 2
    for i in range(n):
        t = (i - mid) / (n - 1) if n > 1 else 0.0
        nh = (h - t * (float(hue_shift_degrees) / 360)) % 1.0
        nl = min(1.0, max(0.0, l + t * lr))
        ns = min(1.0, max(0.0, s - t * 0.15))
        nr, ng, nb = colorsys.hls_to_rgb(nh, nl, ns)
        ramp.append("#%02X%02X%02X" % (round(nr * 255), round(ng * 255), round(nb * 255)))
    return {"base": base_color, "ramp": ramp}


@mcp.tool()
def ase_quantize_to_palette(sprite_path: str, layer: Optional[str] = None, frame: int = 0) -> Dict[str, Any]:
    """Snap every opaque pixel to the nearest color in the sprite's own
    palette (RGB distance, cached per color). layer None = all layers,
    frame=0 = every frame. Run after ase_apply_palette_preset /
    ase_set_palette to make existing art conform. Returns the number of
    pixels changed."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local ok, pal = pcall(function() return spr.palettes[1] end)
if not ok or pal == nil or #pal == 0 then error("sprite has no palette") end
{_lua_layers_all(layer)}
{_lua_frames_range(frame)}
local colors = {{}}
for i = 0, #pal - 1 do
  local c = pal:getColor(i)
  colors[#colors + 1] = {{c.red, c.green, c.blue}}
end
local cache = {{}}
local function nearest(r, g, b)
  local key = r * 65536 + g * 256 + b
  local hit = cache[key]
  if hit then return hit end
  local best, best_d = colors[1], math.huge
  for _, c in ipairs(colors) do
    local dr, dg, db = r - c[1], g - c[2], b - c[3]
    local d = dr * dr + dg * dg + db * db
    if d < best_d then best, best_d = c, d end
  end
  cache[key] = best
  return best
end
local count = 0
for _, l in ipairs(layers) do
  for fi = f0, f1 do
    local cel = l:cel(spr.frames[fi])
    if cel then
      local img = cel.image
      for py = 0, img.height - 1 do
        for px = 0, img.width - 1 do
          local v = img:getPixel(px, py)
          local a = app.pixelColor.rgbaA(v)
          if a > 0 then
            local r = app.pixelColor.rgbaR(v)
            local g = app.pixelColor.rgbaG(v)
            local b = app.pixelColor.rgbaB(v)
            local c = nearest(r, g, b)
            if c[1] ~= r or c[2] ~= g or c[3] ~= b then
              img:putPixel(px, py, app.pixelColor.rgba(c[1], c[2], c[3], a))
              count = count + 1
            end
          end
        end
      end
    end
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK count=" .. count)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "quantized": int(out.strip().split("count=")[-1])}


# --- cels & animation -----------------------------------------------------------
@mcp.tool()
def ase_add_frames_blank(sprite_path: str, count: int, duration_ms: int = 100) -> Dict[str, Any]:
    """Append N blank frames, each with the given duration. Returns the new
    frame count."""
    n = int(count)
    if n < 1:
        raise ValueError("count must be >= 1")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_lua_dur_helpers()}
for i = 1, {n} do
  spr:newFrame()
  set_dur(spr.frames[#spr.frames], {int(duration_ms)})
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK frames=" .. #spr.frames)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "frames": int(out.strip().split("frames=")[-1])}


@mcp.tool()
def ase_set_frame_duration_all(sprite_path: str, duration_ms: int) -> Dict[str, Any]:
    """Set every frame's duration in ms (unit-probed for Aseprite 1.3+)."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_lua_dur_helpers()}
for _, fr in ipairs(spr.frames) do set_dur(fr, {int(duration_ms)}) end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK frames=" .. #spr.frames)
"""
    out = _run_aseprite_lua(lua)
    return {"ok": True, "frames": int(out.strip().split("frames=")[-1]), "duration_ms": int(duration_ms)}


@mcp.tool()
def ase_create_cel(sprite_path: str, layer: Optional[str] = None, frame: int = 1, x: int = 0, y: int = 0) -> Dict[str, Any]:
    """Create an empty canvas-sized cel on a layer/frame (no-op if one
    exists). layer None = first image layer."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_lua_layer_one(layer)}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
if layer:cel(spr.frames[idx]) == nil then
  spr:newCel(layer, spr.frames[idx], Image(spr.width, spr.height, spr.colorMode), Point({int(x)}, {int(y)}))
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK cel")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "frame": int(frame)}


@mcp.tool()
def ase_clear_cel(sprite_path: str, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Delete the cel on a layer/frame (makes it truly empty, unlike painting
    it transparent). layer None = first image layer."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_lua_layer_one(layer)}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local cel = layer:cel(spr.frames[idx])
if cel then spr:deleteCel(cel) end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK cel-cleared")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "frame": int(frame)}


@mcp.tool()
def ase_copy_cel(sprite_path: str, source_frame: int, target_frame: int, layer: Optional[str] = None, replace: bool = True) -> Dict[str, Any]:
    """Copy a cel's image from one frame to another (independent clone, unlike
    ase_link_cel which shares the image). layer None = first image layer."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_lua_layer_one(layer)}
if spr.frames[{int(source_frame)}] == nil or spr.frames[{int(target_frame)}] == nil then
  error("frame index out of range (sprite has " .. #spr.frames .. " frames)")
end
local src = layer:cel(spr.frames[{int(source_frame)}])
if src == nil then error("no cel on source frame {int(source_frame)}") end
local dst = layer:cel(spr.frames[{int(target_frame)}])
if dst ~= nil and {"true" if replace else "false"} then
  spr:deleteCel(dst)
  dst = nil
end
if dst == nil then
  spr:newCel(layer, spr.frames[{int(target_frame)}], src.image:clone(), src.position)
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK cel-copied")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "source_frame": int(source_frame), "target_frame": int(target_frame)}


@mcp.tool()
def ase_set_cel_position(sprite_path: str, x: int, y: int, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Place a cel at sprite coordinates (x, y). layer None = first image
    layer; errors when the cel does not exist (create/draw one first)."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_lua_layer_one(layer)}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local cel = layer:cel(spr.frames[idx])
if cel == nil then error("no cel on frame " .. idx .. "; create or draw one first") end
cel.position = Point({int(x)}, {int(y)})
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK cel-position")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "x": int(x), "y": int(y), "frame": int(frame)}


@mcp.tool()
def ase_set_cel_opacity(sprite_path: str, opacity: int, layer: Optional[str] = None, frame: int = 1) -> Dict[str, Any]:
    """Set a single cel's opacity 0-255 (layer None = first image layer)."""
    op = max(0, min(255, int(opacity)))
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_lua_layer_one(layer)}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local cel = layer:cel(spr.frames[idx])
if cel == nil then error("no cel on frame " .. idx) end
cel.opacity = {op}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK cel-opacity")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "opacity": op, "frame": int(frame)}


_LUA_ENSURE_CEL = """
local function ensure_cel(spr, layer, fi, create, src_idx)
  local frame = spr.frames[fi]
  local cel = layer:cel(frame)
  if cel == nil and create then
    local source_cel = layer:cel(spr.frames[src_idx])
    if source_cel then
      cel = spr:newCel(layer, frame, source_cel.image:clone(), source_cel.position)
    else
      cel = spr:newCel(layer, frame, Image(spr.width, spr.height, spr.colorMode), Point(0, 0))
    end
  end
  return cel
end
"""


@mcp.tool()
def ase_propagate_cels(
    sprite_path: str,
    source_frame: int,
    start_frame: int,
    end_frame: int,
    layers: Optional[List[str]] = None,
    replace: bool = True,
) -> Dict[str, Any]:
    """Copy the cels of a source frame across a frame range (per layer list;
    None = every image layer). The animation workhorse: draw once, propagate,
    then tween positions. replace=True overwrites existing cels."""
    if layers:
        names_lua = ", ".join(_lua_str(n) for n in layers)
        resolve = f"""
local targets = {{}}
local name_list = {{ {names_lua} }}
for _, name in ipairs(name_list) do
  local m = find_layer(spr, name)
  if m then targets[#targets + 1] = m end
end
if #targets == 0 then error("none of the named layers were found") end
"""
    else:
        resolve = """
local targets = {}
local function collect_targets(list)
  for _, l in ipairs(list) do
    if l.isImage then targets[#targets + 1] = l
    elseif l.isGroup then collect_targets(l.layers) end
  end
end
collect_targets(spr.layers)
"""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local src_idx = {int(source_frame)}
local start_idx = {int(start_frame)}
local end_idx = {int(end_frame)}
if src_idx < 1 or src_idx > #spr.frames then error("source frame out of range") end
if start_idx < 1 or end_idx > #spr.frames or start_idx > end_idx then error("frame range out of bounds") end
{resolve}
for fi = start_idx, end_idx do
  if fi ~= src_idx then
    local dst_frame = spr.frames[fi]
    for _, l in ipairs(targets) do
      local src_cel = l:cel(spr.frames[src_idx])
      if src_cel then
        local dst_cel = l:cel(dst_frame)
        if dst_cel and {"true" if replace else "false"} then
          spr:deleteCel(dst_cel)
          dst_cel = nil
        end
        if dst_cel == nil then
          spr:newCel(l, dst_frame, src_cel.image:clone(), src_cel.position)
        end
      end
    end
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK propagated")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "source_frame": int(source_frame), "range": [int(start_frame), int(end_frame)]}


@mcp.tool()
def ase_tween_cel_positions(
    sprite_path: str,
    start_frame: int,
    end_frame: int,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    easing: str = "linear",
    layer: Optional[str] = None,
    create_missing: bool = True,
) -> Dict[str, Any]:
    """Tween a cel's position linearly across a frame range (movement arcs,
    attacks, slides). easing: linear | ease_in | ease_out | ease_in_out |
    smoothstep. Missing cels are cloned from the start frame when
    create_missing=true. layer None = first image layer."""
    eas = str(easing).lower().strip()
    if eas not in ("linear", "ease_in", "ease_out", "ease_in_out", "smoothstep"):
        raise ValueError("easing must be linear|ease_in|ease_out|ease_in_out|smoothstep")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_LUA_ENSURE_CEL}
{_lua_layer_one(layer)}
local start_idx = {int(start_frame)}
local end_idx = {int(end_frame)}
if start_idx < 1 or end_idx > #spr.frames or start_idx > end_idx then error("frame range out of bounds") end
local function ease(t)
  local mode = "{eas}"
  if mode == "linear" then return t end
  if mode == "ease_in" then return t * t end
  if mode == "ease_out" then return 1 - (1 - t) * (1 - t) end
  if mode == "ease_in_out" then
    if t < 0.5 then return 2 * t * t end
    local u = -2 * t + 2
    return 1 - (u * u) / 2
  end
  return t * t * (3 - 2 * t)
end
local span = end_idx - start_idx
for fi = start_idx, end_idx do
  local t = 0
  if span > 0 then t = (fi - start_idx) / span end
  local e = ease(t)
  local x = math.floor({int(start_x)} + ({int(end_x)} - {int(start_x)}) * e + 0.5)
  local y = math.floor({int(start_y)} + ({int(end_y)} - {int(start_y)}) * e + 0.5)
  local cel = ensure_cel(spr, layer, fi, {"true" if create_missing else "false"}, start_idx)
  if cel then cel.position = Point(x, y) end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK tweened")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "easing": eas, "range": [int(start_frame), int(end_frame)]}


@mcp.tool()
def ase_oscillate_cel_positions(
    sprite_path: str,
    start_frame: int,
    end_frame: int,
    amplitude_x: int = 0,
    amplitude_y: int = 0,
    cycles: float = 1.0,
    phase_deg: float = 0.0,
    layer: Optional[str] = None,
    create_missing: bool = True,
) -> Dict[str, Any]:
    """Sine-wave cel motion across a frame range (bobbing, breathing,
    hovering). Offsets are applied relative to each cel's position at script
    start, so run once per animation pass. x follows sin, y follows cos.
    layer None = first image layer."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
{_LUA_ENSURE_CEL}
{_lua_layer_one(layer)}
local start_idx = {int(start_frame)}
local end_idx = {int(end_frame)}
if start_idx < 1 or end_idx > #spr.frames or start_idx > end_idx then error("frame range out of bounds") end
local phase = ({float(phase_deg)}) * math.pi / 180
local span = end_idx - start_idx
local bases = {{}}
for fi = start_idx, end_idx do
  local cel = ensure_cel(spr, layer, fi, {"true" if create_missing else "false"}, start_idx)
  if cel then bases[fi] = {{ cel.position.x, cel.position.y }} end
end
for fi = start_idx, end_idx do
  local cel = layer:cel(spr.frames[fi])
  local base = bases[fi]
  if cel and base then
    local t = 0
    if span > 0 then t = (fi - start_idx) / span end
    local angle = 2 * math.pi * {float(cycles)} * t + phase
    local dx = math.floor({int(amplitude_x)} * math.sin(angle) + 0.5)
    local dy = math.floor({int(amplitude_y)} * math.cos(angle) + 0.5)
    cel.position = Point(base[1] + dx, base[2] + dy)
  end
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK oscillated")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "range": [int(start_frame), int(end_frame)], "cycles": float(cycles)}


# --- slices (9-patch, pivots) ----------------------------------------------------
_LUA_FIND_SLICE = """
local function find_slice(spr, name)
  for _, slice in ipairs(spr.slices) do
    if slice.name == name then return slice end
  end
  return nil
end
"""


@mcp.tool()
def ase_create_slice(sprite_path: str, name: str, x: int, y: int, width: int, height: int) -> Dict[str, Any]:
    """Create a named slice (rectangular region exported for game engines)."""
    if int(width) <= 0 or int(height) <= 0:
        raise ValueError("width and height must be > 0")
    if not name:
        raise ValueError("slice name cannot be empty")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_SLICE}
if find_slice(spr, {_lua_str(name)}) then error("slice already exists: " .. {_lua_str(name)}) end
local slice = spr:newSlice(Rectangle({int(x)}, {int(y)}, {int(width)}, {int(height)}))
slice.name = {_lua_str(name)}
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK slice")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "slice": name}


@mcp.tool()
def ase_set_slice_center(sprite_path: str, name: str, x: int, y: int, width: int, height: int) -> Dict[str, Any]:
    """Set a slice's 9-patch center rectangle (relative to the slice origin):
    the stretchable region for game-engine 9-patch scaling."""
    if int(width) <= 0 or int(height) <= 0:
        raise ValueError("width and height must be > 0")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_SLICE}
local slice = find_slice(spr, {_lua_str(name)})
if slice == nil then error("slice not found: " .. {_lua_str(name)}) end
slice.center = Rectangle({int(x)}, {int(y)}, {int(width)}, {int(height)})
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK slice-center")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "slice": name}


@mcp.tool()
def ase_set_slice_pivot(sprite_path: str, name: str, x: int, y: int) -> Dict[str, Any]:
    """Set a slice's pivot point (relative to the slice origin)."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_SLICE}
local slice = find_slice(spr, {_lua_str(name)})
if slice == nil then error("slice not found: " .. {_lua_str(name)}) end
slice.pivot = Point({int(x)}, {int(y)})
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK slice-pivot")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "slice": name}


@mcp.tool()
def ase_list_slices(sprite_path: str) -> Dict[str, Any]:
    """List all slices with bounds, 9-patch centers and pivots."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
for _, slice in ipairs(spr.slices) do
  local b = slice.bounds
  local parts = {{}}
  parts[#parts + 1] = string.format('"name":%s', string.format("%q", slice.name))
  parts[#parts + 1] = string.format('"x":%d,"y":%d,"width":%d,"height":%d', b.x, b.y, b.width, b.height)
  if slice.center then
    local c = slice.center
    parts[#parts + 1] = string.format('"center":{{"x":%d,"y":%d,"width":%d,"height":%d}}', c.x, c.y, c.width, c.height)
  end
  if slice.pivot then
    parts[#parts + 1] = string.format('"pivot":{{"x":%d,"y":%d}}', slice.pivot.x, slice.pivot.y)
  end
  print("SLICE:{{" .. table.concat(parts, ",") .. "}}")
end
print("RD-MCP-OK slices")
"""
    out = _run_aseprite_lua(lua)
    slices: List[Dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SLICE:"):
            slices.append(json.loads(line[len("SLICE:"):]))
    return {"ok": True, "slices": slices}


@mcp.tool()
def ase_delete_slice(sprite_path: str, name: str) -> Dict[str, Any]:
    """Delete a slice by name."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_SLICE}
local slice = find_slice(spr, {_lua_str(name)})
if slice == nil then error("slice not found: " .. {_lua_str(name)}) end
spr:deleteSlice(slice)
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK slice-deleted")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "slice": name}


# --- tilemaps (Aseprite 1.3+) ------------------------------------------------------
@mcp.tool()
def ase_create_tilemap_layer(sprite_path: str, layer_name: str, tile_width: int, tile_height: int) -> Dict[str, Any]:
    """Create a tilemap layer with its own tileset: sets the sprite grid to
    the tile size and adds the layer. Tile index 0 is the reserved empty
    tile; draw real tiles with ase_draw_on_tile (which appends on demand)."""
    if int(tile_width) <= 0 or int(tile_height) <= 0:
        raise ValueError("tile dimensions must be > 0")
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
for _, l in ipairs(spr.layers) do
  if l.name == {_lua_str(layer_name)} then error("layer already exists: " .. {_lua_str(layer_name)}) end
end
app.transaction(function()
  spr.gridBounds = Rectangle(0, 0, {int(tile_width)}, {int(tile_height)})
  app.command.NewLayer{{ tilemap = true }}
  app.activeLayer.name = {_lua_str(layer_name)}
end)
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK tilemap")
"""
    _run_aseprite_lua(lua)
    return {"ok": True, "layer": layer_name, "tile": [int(tile_width), int(tile_height)]}


@mcp.tool()
def ase_draw_on_tile(sprite_path: str, layer_name: str, tile_index: int, pixels: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Paint pixels into a tile of a tilemap layer's tileset. Coordinates are
    tile-local (0,0 = tile top-left); pixels are [{x, y, color: '#rrggbb'}].
    tile_index 1 is the first real tile (0 is the reserved empty tile);
    passing tile_index == current tile count appends a new tile."""
    idx = int(tile_index)
    if idx < 1:
        raise ValueError("tile_index must be >= 1 (0 is the reserved empty tile)")
    if not pixels:
        raise ValueError("pixels list cannot be empty")
    norm: List[Dict[str, int]] = []
    for p in pixels:
        c = _rgba(str(p.get("color", "")))
        norm.append({"x": int(p.get("x", 0)), "y": int(p.get("y", 0)), "r": c["r"], "g": c["g"], "b": c["b"]})
    inp = _px_json_path("tile")
    inp.write_text(json.dumps({"pixels": norm}), encoding="utf-8")
    try:
        lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
if not target.isTilemap then error("layer is not a tilemap layer") end
local ts = target.tileset
if ts == nil then error("layer has no tileset") end
local idx = {idx}
if idx > #ts then error("tile_index out of range (pass " .. #ts .. " to append)") end
local f = io.open({_lua_str(str(inp))}, "rb")
local data = json.decode(f:read("*a"))
f:close()
app.transaction(function()
  if idx == #ts then spr:newTile(ts) end
  local tile = ts:tile(idx)
  local img = tile.image:clone()
  for _, p in ipairs(data.pixels) do
    local pxx, pyy = math.floor(p.x), math.floor(p.y)
    if pxx >= 0 and pyy >= 0 and pxx < img.width and pyy < img.height then
      img:putPixel(pxx, pyy, app.pixelColor.rgba(math.floor(p.r), math.floor(p.g), math.floor(p.b), 255))
    end
  end
  tile.image = img
end)
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK tile-drawn=" .. idx)
"""
        out = _run_aseprite_lua(lua)
    finally:
        try:
            inp.unlink()
        except OSError:
            pass
    return {"ok": True, "tile_index": int(out.strip().split("tile-drawn=")[-1]), "pixels": len(norm)}


@mcp.tool()
def ase_set_tiles(sprite_path: str, layer_name: str, tiles: List[Dict[str, Any]], frame: int = 1) -> Dict[str, Any]:
    """Place tiles on a tilemap layer by grid position. tiles are
    [{col, row, tile_index}] (col/row are 0-based grid coordinates;
    tile_index 0 clears the cell). The cel is (re)built at map size."""
    if not tiles:
        raise ValueError("tiles list cannot be empty")
    norm = [[int(t.get("col", 0)), int(t.get("row", 0)), int(t.get("tile_index", 0))] for t in tiles]
    inp = _px_json_path("tiles")
    inp.write_text(json.dumps({"tiles": norm}), encoding="utf-8")
    try:
        lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
if not target.isTilemap then error("layer is not a tilemap layer") end
local ts = target.tileset
local grid = spr.gridBounds
local tw, th = grid.width, grid.height
local cols = math.ceil(spr.width / tw)
local rows = math.ceil(spr.height / th)
local f = io.open({_lua_str(str(inp))}, "rb")
local data = json.decode(f:read("*a"))
f:close()
for _, t in ipairs(data.tiles) do
  if t[1] < 0 or t[1] >= cols or t[2] < 0 or t[2] >= rows then
    error("tile position (" .. t[1] .. "," .. t[2] .. ") outside the " .. cols .. "x" .. rows .. " map")
  end
  if t[3] < 0 or t[3] >= #ts then
    error("tile_index " .. t[3] .. " out of range (tileset has " .. (#ts - 1) .. " real tiles)")
  end
end
local frame = spr.frames[idx]
-- TRAP (verified on Aseprite 1.3.18 batch): json.decode yields FLOATS and
-- putPixel on a TILEMAP-mode image silently ignores float args, so every
-- coordinate/tile value goes through math.floor. The cel image is rebuilt
-- fresh (map-sized, old contents copied in, painted, then assigned) so
-- offset/partial cels are normalized to the full grid in the same pass.
local cel = target:cel(frame)
local img = Image(cols, rows, ColorMode.TILEMAP)
if cel then
  local old = cel.image
  local ox = math.floor(cel.position.x / tw)
  local oy = math.floor(cel.position.y / th)
  for y = 0, old.height - 1 do
    for x = 0, old.width - 1 do
      local nx, ny = x + ox, y + oy
      if nx >= 0 and ny >= 0 and nx < cols and ny < rows then
        img:putPixel(nx, ny, old:getPixel(x, y))
      end
    end
  end
end
for _, t in ipairs(data.tiles) do
  img:putPixel(math.floor(t[1]), math.floor(t[2]), math.floor(t[3]))
end
if cel then
  cel.image = img
  cel.position = Point(0, 0)
else
  spr:newCel(target, frame, img, Point(0, 0))
end
spr:saveAs({_lua_str(sprite_path)})
print("RD-MCP-OK tiles-set=" .. #data.tiles)
"""
        out = _run_aseprite_lua(lua)
    finally:
        try:
            inp.unlink()
        except OSError:
            pass
    return {"ok": True, "placed": int(out.strip().split("tiles-set=")[-1])}


@mcp.tool()
def ase_get_tile_at(sprite_path: str, layer_name: str, col: int, row: int, frame: int = 1) -> Dict[str, Any]:
    """Read which tile occupies a grid cell (0 = empty)."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
if not target.isTilemap then error("layer is not a tilemap layer") end
local grid = spr.gridBounds
local cel = target:cel(spr.frames[idx])
local tile = 0
if cel then
  local cx = {int(col)} - math.floor(cel.position.x / grid.width)
  local cy = {int(row)} - math.floor(cel.position.y / grid.height)
  if cx >= 0 and cy >= 0 and cx < cel.image.width and cy < cel.image.height then
    tile = app.pixelColor.tileI(cel.image:getPixel(cx, cy))
  end
end
print("TILE:" .. tile)
print("RD-MCP-OK tile-at")
"""
    out = _run_aseprite_lua(lua)
    tile = 0
    for line in out.splitlines():
        if line.strip().startswith("TILE:"):
            tile = int(line.strip()[len("TILE:"):])
    return {"ok": True, "col": int(col), "row": int(row), "tile_index": tile}


@mcp.tool()
def ase_get_tilemap_info(sprite_path: str, layer_name: str) -> Dict[str, Any]:
    """Tilemap layer info: tile size, real tile count (excluding empty tile
    0), and map dimensions in cells."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FIND_LAYER}
local target = find_layer(spr, {_lua_str(layer_name)})
if target == nil then error("layer not found: " .. {_lua_str(layer_name)}) end
if not target.isTilemap then error("layer is not a tilemap layer") end
local ts = target.tileset
local grid = spr.gridBounds
print(string.format("INFO:%d,%d,%d,%d,%d", grid.width, grid.height, #ts - 1,
  math.ceil(spr.width / grid.width), math.ceil(spr.height / grid.height)))
print("RD-MCP-OK tilemap-info")
"""
    out = _run_aseprite_lua(lua)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("INFO:"):
            tw, th, count, cols, rows = [int(v) for v in line[len("INFO:"):].split(",")]
            return {"ok": True, "tile_width": tw, "tile_height": th, "tile_count": count,
                    "map_cols": cols, "map_rows": rows}
    raise RuntimeError("no tilemap info returned")


# --- analysis & visual feedback ----------------------------------------------------
_LUA_FLATTEN_FRAME = """
local function flatten_frame(clone, frame_idx)
  local layer = clone.layers[#clone.layers]
  local img = Image(clone.width, clone.height, ColorMode.RGB)
  local cel = layer:cel(clone.frames[frame_idx])
  if cel then img:drawImage(cel.image, cel.position) end
  return img
end
"""


@mcp.tool()
def ase_render_onion_skin(
    sprite_path: str,
    frame: int,
    before: int = 1,
    after: int = 1,
    scale: int = 4,
    ghost_opacity: int = 100,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Render a frame over translucent ghosts of neighboring frames (batch
    onion-skinning) to check motion continuity without opening Aseprite.
    The flattened frames are exported via the CLI and composited with Pillow
    over white, then nearest-neighbor scaled. Returns the PNG path."""
    sc = max(1, min(64, int(scale)))
    ghost_a = max(0, min(255, int(ghost_opacity)))
    stem = Path(sprite_path).stem
    dst = Path(output_path) if output_path else OUT_DIR / ("onion_%s_f%d_%s.png" % (stem, int(frame), time.strftime("%H%M%S")))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "_px_onion_%d_" % os.getpid()
    outdir_lua = _lua_str(str(OUT_DIR).replace(os.sep, "/"))
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FLATTEN_FRAME}
local idx = {int(frame)}
if idx < 1 or idx > #spr.frames then error("frame out of range (sprite has " .. #spr.frames .. " frames)") end
local clone = Sprite(spr)
clone:flatten()
for fi = idx - {int(before)}, idx + {int(after)} do
  if fi >= 1 and fi <= #clone.frames then
    flatten_frame(clone, fi):saveAs({outdir_lua} .. "/{prefix}" .. fi .. ".png")
  end
end
clone:close()
print("RD-MCP-OK onion-dumped")
"""
    _run_aseprite_lua(lua)
    dumped = {}
    for p in OUT_DIR.glob(prefix + "*.png"):
        try:
            dumped[int(p.stem.split("_")[-1])] = p
        except ValueError:
            pass
    if not dumped:
        raise RuntimeError("onion-skin frame dump produced no PNGs")
    main = Image.open(dumped[int(frame)]).convert("RGBA")
    canvas = Image.new("RGBA", main.size, (255, 255, 255, 255))
    order = [fi for fi in sorted(dumped) if fi != int(frame)]
    order.sort(key=lambda fi: abs(fi - int(frame)), reverse=True)
    for fi in order:
        g = Image.open(dumped[fi]).convert("RGBA")
        a = g.getchannel("A").point(lambda v: int(v * ghost_a / 255))
        g.putalpha(a)
        canvas = Image.alpha_composite(canvas, g)
    canvas = Image.alpha_composite(canvas, main)
    out_img = canvas.convert("RGB")
    if sc > 1:
        out_img = out_img.resize((out_img.width * sc, out_img.height * sc), Image.Resampling.NEAREST)
    out_img.save(dst)
    for p in dumped.values():
        try:
            p.unlink()
        except OSError:
            pass
    return {"ok": True, "path": str(dst), "frame": int(frame), "ghosts": sorted(dumped)}


@mcp.tool()
def ase_compare_frames(sprite_path: str, frame_a: int, frame_b: int) -> Dict[str, Any]:
    """Diff two frames of the flattened sprite: changed pixel count, percent,
    and the bounding box of the changed region. Use while animating to confirm
    a frame actually changed (or did not change too much)."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FLATTEN_FRAME}
if {int(frame_a)} < 1 or {int(frame_a)} > #spr.frames or {int(frame_b)} < 1 or {int(frame_b)} > #spr.frames then
  error("frame index out of range (sprite has " .. #spr.frames .. " frames)")
end
local clone = Sprite(spr)
clone:flatten()
local img_a = flatten_frame(clone, {int(frame_a)})
local img_b = flatten_frame(clone, {int(frame_b)})
local changed = 0
local min_x, min_y = math.huge, math.huge
local max_x, max_y = -1, -1
for py = 0, img_a.height - 1 do
  for px = 0, img_a.width - 1 do
    local va = img_a:getPixel(px, py)
    local vb = img_b:getPixel(px, py)
    local aa = app.pixelColor.rgbaA(va)
    local ab = app.pixelColor.rgbaA(vb)
    local diff
    if aa == 0 and ab == 0 then diff = false else diff = va ~= vb end
    if diff then
      changed = changed + 1
      if px < min_x then min_x = px end
      if py < min_y then min_y = py end
      if px > max_x then max_x = px end
      if py > max_y then max_y = py end
    end
  end
end
if changed == 0 then min_x, min_y, max_x, max_y = 0, 0, -1, -1 end
print(string.format("DIFF:%d,%d,%d,%d,%d,%d", changed, img_a.width * img_a.height, min_x, min_y, max_x, max_y))
clone:close()
print("RD-MCP-OK compared")
"""
    out = _run_aseprite_lua(lua)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("DIFF:"):
            changed, total, mnx, mny, mxx, mxy = [int(v) for v in line[len("DIFF:"):].split(",")]
            res: Dict[str, Any] = {
                "ok": True,
                "frame_a": int(frame_a),
                "frame_b": int(frame_b),
                "changed_pixels": changed,
                "total_pixels": total,
                "percent_changed": round(changed / total * 100, 2) if total else 0.0,
            }
            if changed > 0:
                res["changed_bounds"] = {"x": mnx, "y": mny, "width": mxx - mnx + 1, "height": mxy - mny + 1}
            return res
    raise RuntimeError("no diff data returned")


@mcp.tool()
def ase_get_color_stats(sprite_path: str, frame: int = 1, top: int = 16) -> Dict[str, Any]:
    """Histogram the colors of a flattened frame: unique color count, opaque
    pixel count, and the most-used colors. Catches palette drift and
    near-duplicate colors."""
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
{_LUA_FLATTEN_FRAME}
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local clone = Sprite(spr)
clone:flatten()
local img = flatten_frame(clone, idx)
local counts = {{}}
local opaque = 0
for py = 0, img.height - 1 do
  for px = 0, img.width - 1 do
    local v = img:getPixel(px, py)
    if app.pixelColor.rgbaA(v) > 0 then
      opaque = opaque + 1
      local hex = string.format("#%02X%02X%02X", app.pixelColor.rgbaR(v), app.pixelColor.rgbaG(v), app.pixelColor.rgbaB(v))
      counts[hex] = (counts[hex] or 0) + 1
    end
  end
end
local unique = 0
for hex, count in pairs(counts) do
  unique = unique + 1
  print("COLOR:" .. hex .. "," .. count)
end
print("OPAQUE:" .. opaque)
print("UNIQUE:" .. unique)
clone:close()
print("RD-MCP-OK stats")
"""
    out = _run_aseprite_lua(lua)
    colors: List[Dict[str, Any]] = []
    opaque = unique = 0
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("COLOR:"):
            hx, cnt = line[len("COLOR:"):].split(",")
            colors.append({"color": hx, "count": int(cnt)})
        elif line.startswith("OPAQUE:"):
            opaque = int(line[len("OPAQUE:"):])
        elif line.startswith("UNIQUE:"):
            unique = int(line[len("UNIQUE:"):])
    colors.sort(key=lambda c: c["count"], reverse=True)
    return {"ok": True, "frame": int(frame), "unique_colors": unique,
            "opaque_pixels": opaque, "top_colors": colors[: max(1, int(top))]}


# --- export & scripting ----------------------------------------------------------
@mcp.tool()
def ase_export_layers(sprite_path: str, out_dir: Optional[str] = None, include_hidden: bool = False) -> Dict[str, Any]:
    """Export each visible layer as its own PNG named after the layer
    (--split-layers). out_dir defaults to output/layers_<sprite name>/.
    include_hidden also exports hidden layers."""
    stem = Path(sprite_path).stem
    dst = Path(out_dir) if out_dir else OUT_DIR / ("layers_" + stem)
    dst.mkdir(parents=True, exist_ok=True)
    args = ["--batch"]
    if include_hidden:
        args.append("--all-layers")
    args += ["--split-layers", sprite_path, "--save-as", str(dst / "{layer}.png")]
    _run_aseprite_cli(args)
    produced = sorted(p.name for p in dst.glob("*.png"))
    if not produced:
        raise RuntimeError("Aseprite exited 0 but wrote no PNG files")
    return {"ok": True, "dir": str(dst), "files": produced}


@mcp.tool()
def ase_export_tag(sprite_path: str, tag_name: str, output_path: Optional[str] = None, scale: int = 1) -> Dict[str, Any]:
    """Export an animation tag: .gif output gives an animation, .png a
    frame-numbered sequence. The tag is validated up front because Aseprite's
    --tag silently exports ALL frames for a missing tag. output_path defaults
    to output/<sprite>_<tag>.gif."""
    sc = max(1, min(64, int(scale)))
    info = ase_sprite_info(sprite_path)
    names = [t["name"] for t in info.get("tags", [])]
    if tag_name not in names:
        raise ValueError("tag not found: " + tag_name + " (sprite has: " + ", ".join(names) + ")")
    dst = Path(output_path) if output_path else OUT_DIR / (Path(sprite_path).stem + "_" + tag_name + ".gif")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    args = ["--batch", sprite_path, "--tag", tag_name]
    if sc > 1:
        args += ["--scale", str(sc)]
    args += ["--save-as", str(dst)]
    _run_aseprite_cli(args)
    produced = dst.exists() or any(dst.parent.glob(dst.stem + "*" + dst.suffix))
    if not produced:
        raise RuntimeError("Aseprite exited 0 but wrote no file")
    return {"ok": True, "tag": tag_name, "path": str(dst)}


@mcp.tool()
def ase_run_lua(script: str, sprite_path: str = "") -> Dict[str, Any]:
    """Execute arbitrary Aseprite Lua in batch mode - the escape hatch when no
    dedicated tool fits (full API: https://www.aseprite.org/api/). One script
    can batch many operations into a single Aseprite launch.

    Essentials: when sprite_path is given it is opened first and bound to a
    local 'spr' (also app.activeSprite); changes are NOT saved automatically -
    call spr:saveAs(spr.filename) at the end; print() is the only way to
    return data (captured in the result's output list); do NOT end the script
    with a top-level return (it skips the completion marker and the call is
    reported as failed). Cel-image coordinates are cel-local: offset
    sprite-global coordinates by cel.position.

    WARNING: executes unrestricted Lua (including io/os access) on the host
    running Aseprite. Only pass scripts you trust."""
    if not script.strip():
        raise ValueError("script cannot be empty")
    pre = ""
    if sprite_path:
        if not Path(sprite_path).exists():
            raise ValueError("sprite not found: " + sprite_path)
        pre = "local spr = app.open(" + _lua_str(sprite_path) + ")\n"
    lua = pre + script + '\nprint("RD-MCP-OK run-lua")\n'
    out = _run_aseprite_lua(lua)
    lines = [l for l in out.splitlines() if l.strip() and "RD-MCP-OK" not in l]
    return {"ok": True, "output": lines}


# ---------------------------------------------------------------------------
# Availability probes, call-time gating, image-content return
# ---------------------------------------------------------------------------
_RD_CACHE: Dict[str, Any] = {"ts": 0.0, "ok": None, "detail": ""}
_ASE_CACHE: Dict[str, Any] = {"ts": 0.0, "ok": None, "detail": ""}


async def _rd_available(force: bool = False) -> Tuple[bool, str]:
    now = time.time()
    if not force and _RD_CACHE["ok"] is not None and now - _RD_CACHE["ts"] < 30:
        return _RD_CACHE["ok"], _RD_CACHE["detail"]
    try:
        async with websockets.connect(RD_URL, open_timeout=2, ping_interval=None):
            ok, detail = True, "backend listening on " + RD_URL
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    _RD_CACHE.update(ts=now, ok=ok, detail=detail)
    return ok, detail


async def _require_rd() -> Optional[Dict[str, Any]]:
    ok, detail = await _rd_available()
    if ok:
        return None
    return {
        "available": False,
        "subsystem": "retro_diffusion",
        "error": detail,
        "hint": "Retro Diffusion backend unreachable. Start it headless with rd_start_backend, "
        "or open the Retro Diffusion dialog in Aseprite once. Aseprite tools (ase_*) remain "
        "fully usable meanwhile.",
    }


def _ase_available(force: bool = False) -> Tuple[bool, str]:
    now = time.time()
    if not force and _ASE_CACHE["ok"] is not None and now - _ASE_CACHE["ts"] < 60:
        return _ASE_CACHE["ok"], _ASE_CACHE["detail"]
    if not ASEPRITE_EXE.exists():
        ok, detail = False, f"Aseprite executable not found at {ASEPRITE_EXE} (set ASEPRITE_EXE)"
    else:
        try:
            _run_aseprite_lua('print("RD-MCP-OK probe")')
            ok, detail = True, f"headless CLI working: {ASEPRITE_EXE}"
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
    _ASE_CACHE.update(ts=now, ok=ok, detail=detail)
    return ok, detail


def _require_ase() -> Optional[Dict[str, Any]]:
    ok, detail = _ase_available()
    if ok:
        return None
    return {
        "available": False,
        "subsystem": "aseprite",
        "error": detail,
        "hint": "Aseprite CLI unavailable; point ASEPRITE_EXE at the Aseprite executable. "
        "Retro Diffusion tools (rd_*) are unaffected.",
    }


def _first_image_path(res: Dict[str, Any]) -> Optional[str]:
    imgs = res.get("images")
    if imgs:
        return imgs[0].get("path")
    anim = res.get("animation") or {}
    frames = anim.get("frames") or []
    if frames:
        return frames[0]
    for key in ("normal_maps", "roughness_maps", "depth_maps"):
        lst = res.get(key) or []
        if lst:
            return lst[0].get("path")
    if res.get("image"):
        return res["image"]
    return None


def _image_content(path: str, max_side: int) -> MCPImage:
    pil = Image.open(path).convert("RGB")
    if max(pil.size) > max_side:
        scale = max_side / max(pil.size)
        pil = pil.resize(
            (max(1, round(pil.width * scale)), max(1, round(pil.height * scale))),
            Image.Resampling.LANCZOS,
        )
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return MCPImage(data=buf.getvalue(), format="png")
    return MCPImage(path=path)


@mcp.tool()
def ase_status() -> Dict[str, Any]:
    """Check whether the headless Aseprite CLI is available (executable present
    and a probe batch run succeeds). Cached 60s; file-touching ase_* tools gate
    on this and report {available:false,...} instead of crashing when missing."""
    ok, detail = _ase_available(force=True)
    return {"available": ok, "exe": str(ASEPRITE_EXE), "detail": detail}


@mcp.tool()
async def px_capabilities() -> Dict[str, Any]:
    """One-call overview of what this server can currently do: Aseprite
    (primary subsystem) and Retro Diffusion (conditional) availability, the
    gating behavior, and how to fetch image content back to the caller."""
    rd_ok, rd_detail = await _rd_available()
    ase_ok, ase_detail = _ase_available()
    kr_ok, kr_detail = _krita_available()
    return {
        "primary_subsystem": "aseprite",
        "krita": {"available": kr_ok, "detail": kr_detail, "url": KRITA_URL},
        "aseprite": {"available": ase_ok, "detail": ase_detail},
        "retro_diffusion": {
            "available": rd_ok,
            "detail": rd_detail,
            "enable_with": "rd_start_backend (headless) or open the Retro Diffusion dialog in Aseprite",
        },
        "gating": "rd_* tools return {available:false,...} while the RD backend is down; "
        "file-touching ase_* tools return {available:false,...} while the Aseprite CLI is missing",
        "image_return": "pass return_image=true on generation tools, or call px_view_image / "
        "px_view_images / ase_view_frame to receive MCP image content blocks",
    }


def _png_base64(path: str, max_side: int = 0) -> Dict[str, Any]:
    pil = Image.open(path)
    if pil.mode not in ("RGB", "RGBA"):
        pil = pil.convert("RGBA")
    if max_side and max(pil.size) > max_side:
        scale = max_side / max(pil.size)
        pil = pil.resize(
            (max(1, round(pil.width * scale)), max(1, round(pil.height * scale))),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    data = buf.getvalue()
    return {
        "path": str(path),
        "width": pil.width,
        "height": pil.height,
        "mime": "image/png",
        "bytes": len(data),
        "png_base64": base64.b64encode(data).decode(),
    }


@mcp.tool()
def px_image_base64(path: str, max_side: int = 0) -> Dict[str, Any]:
    """Return a PNG file's bytes as a PLAIN TEXT base64 field in the JSON result
    ({"png_base64": "iVBORw0...", "mime": "image/png", "bytes": N, ...}) instead
    of an ImageContent block, so the caller can decode and write it themselves
    (e.g. into a sandboxed outputs directory). Transparency is preserved.
    max_side=0 keeps the original size; otherwise downscale before encoding."""
    if not Path(path).exists():
        return {"error": f"file not found: {path}"}
    return _png_base64(path, max_side)


@mcp.tool()
def px_view_image(path: str, max_side: int = 512) -> Any:
    """Return an image file as MCP image content so the caller can SEE it
    (downscaled to max_side for token sanity), preceded by an info dict."""
    if not Path(path).exists():
        return [{"error": f"file not found: {path}"}]
    pil = Image.open(path)
    return [{"path": path, "width": pil.width, "height": pil.height}, _image_content(path, max_side)]


@mcp.tool()
def px_view_images(paths: List[str], max_side: int = 384) -> Any:
    """Return several image files as MCP image content blocks (animation frames,
    before/after pairs), each preceded by its info dict."""
    out: List[Any] = []
    for p in paths:
        if not Path(p).exists():
            out.append({"error": f"file not found: {p}"})
            continue
        pil = Image.open(p)
        out.append({"path": p, "width": pil.width, "height": pil.height})
        out.append(_image_content(p, max_side))
    return out


@mcp.tool()
def ase_view_frame(sprite_path: str, layer: Optional[str] = None, frame: int = 1, max_side: int = 512, as_base64: bool = False) -> Any:
    """Render one cel of a sprite to PNG and return it as MCP image content so
    the caller can SEE the current sprite state (layer defaults to first)."""
    out_png = _px_json_path("view").with_suffix(".png")
    lay = f"for _, l in ipairs(spr.layers) do if l.name == {_lua_str(layer)} then layer = l end end" if layer else "layer = spr.layers[1]"
    lua = f"""
local spr = app.open({_lua_str(sprite_path)})
local layer = nil
{lay}
if layer == nil then layer = spr.layers[1] end
local idx = math.max(1, math.min({int(frame)}, #spr.frames))
local cel = layer:cel(spr.frames[idx])
if cel == nil then error("empty cel: layer '" .. layer.name .. "' frame " .. idx) end
cel.image:saveAs({_lua_str(str(out_png))})
print("RD-MCP-OK view " .. cel.image.width .. "x" .. cel.image.height)
"""
    _run_aseprite_lua(lua)
    pil = Image.open(out_png)
    info = {"sprite": sprite_path, "layer": layer or "first", "frame": frame, "width": pil.width, "height": pil.height, "path": str(out_png)}
    if as_base64:
        info.update(_png_base64(str(out_png), max_side))
        return info
    return [info, _image_content(str(out_png), max_side)]


# ---------------------------------------------------------------------------
# Krita subsystem (absorbed from krita-mcp: HTTP bridge to the Krita MCP plugin)
# ---------------------------------------------------------------------------
KRITA_URL = os.environ.get("KRITA_URL", "http://localhost:5678")
_KRITA_CACHE: Dict[str, Any] = {"ts": 0.0, "ok": None, "detail": ""}


def _krita_send(action: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    try:
        r = httpx.post(KRITA_URL, json={"action": action, "params": params or {}}, timeout=timeout)
        return r.json()
    except httpx.ConnectError:
        return {"error": "Cannot connect to Krita. Is Krita running with the MCP plugin enabled?"}
    except Exception as e:
        return {"error": str(e)}


def _krita_available(force: bool = False) -> Tuple[bool, str]:
    now = time.time()
    if not force and _KRITA_CACHE["ok"] is not None and now - _KRITA_CACHE["ts"] < 30:
        return _KRITA_CACHE["ok"], _KRITA_CACHE["detail"]
    try:
        r = httpx.get(f"{KRITA_URL}/health", timeout=5.0)
        data = r.json()
        ok, detail = True, f"Krita plugin active: {data.get('plugin', 'unknown')}"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    _KRITA_CACHE.update(ts=now, ok=ok, detail=detail)
    return ok, detail


def _require_krita() -> Optional[Dict[str, Any]]:
    ok, detail = _krita_available()
    if ok:
        return None
    return {
        "available": False,
        "subsystem": "krita",
        "error": detail,
        "hint": "Start Krita with the MCP plugin enabled (listens on KRITA_URL, default http://localhost:5678).",
    }


@mcp.tool()
def krita_status() -> Dict[str, Any]:
    """Check whether Krita is running with the MCP plugin active (forced probe)."""
    ok, detail = _krita_available(force=True)
    return {"available": ok, "url": KRITA_URL, "detail": detail}


@mcp.tool()
def krita_new_canvas(width: int = 800, height: int = 600, name: str = "New Canvas", background: str = "#1a1a2e") -> Dict[str, Any]:
    """Create a new canvas in Krita."""
    return _krita_send("new_canvas", {"width": width, "height": height, "name": name, "background": background})


@mcp.tool()
def krita_set_color(color: str) -> Dict[str, Any]:
    """Set the foreground (paint) color (hex like #ff6b6b)."""
    return _krita_send("set_color", {"color": color})


@mcp.tool()
def krita_set_brush(preset: Optional[str] = None, size: Optional[int] = None, opacity: Optional[float] = None) -> Dict[str, Any]:
    """Set brush preset and properties (preset partial name, size px, opacity 0-1)."""
    params: Dict[str, Any] = {}
    if preset:
        params["preset"] = preset
    if size:
        params["size"] = size
    if opacity is not None:
        params["opacity"] = opacity
    return _krita_send("set_brush", params)


@mcp.tool()
def krita_stroke(points: List[List[int]], pressure: float = 1.0) -> Dict[str, Any]:
    """Paint a stroke through [x, y] points; pressure 0-1."""
    if len(points) < 2:
        return {"error": "Need at least 2 points for a stroke"}
    return _krita_send("stroke", {"points": points, "pressure": pressure})


@mcp.tool()
def krita_fill(x: int, y: int, radius: int = 50) -> Dict[str, Any]:
    """Fill a circle at (x, y) with the current color."""
    return _krita_send("fill", {"x": x, "y": y, "radius": radius})


@mcp.tool()
def krita_draw_shape(shape: str, x: int, y: int, width: int = 100, height: int = 100, fill: bool = True, stroke: bool = False, x2: Optional[int] = None, y2: Optional[int] = None) -> Dict[str, Any]:
    """Draw rectangle|ellipse|line on the canvas; x2/y2 are line endpoints."""
    params: Dict[str, Any] = {"shape": shape, "x": x, "y": y, "width": width, "height": height, "fill": fill, "stroke": stroke}
    if x2 is not None:
        params["x2"] = x2
    if y2 is not None:
        params["y2"] = y2
    return _krita_send("draw_shape", params)


@mcp.tool()
def krita_get_canvas(filename: str = "canvas.png", as_base64: bool = False, max_side: int = 0) -> Any:
    """Export the current Krita canvas to PNG (plugin output dir).

    as_base64=true returns the PNG bytes as a plain text png_base64 field for
    callers that decode/write themselves; otherwise returns the plugin result
    including the exported path.
    """
    out = _krita_send("get_canvas", {"filename": filename}, timeout=120.0)
    if "error" in out:
        return out
    if as_base64:
        path = out.get("path", "")
        if not path or not Path(path).exists():
            return {"error": f"exported file not found: {path!r}"}
        info = _png_base64(path, max_side)
        info["export"] = out
        return info
    return out


@mcp.tool()
def krita_undo() -> Dict[str, Any]:
    """Undo the last action in Krita."""
    return _krita_send("undo")


@mcp.tool()
def krita_redo() -> Dict[str, Any]:
    """Redo the last undone action in Krita."""
    return _krita_send("redo")


@mcp.tool()
def krita_clear(color: str = "#1a1a2e") -> Dict[str, Any]:
    """Clear the canvas to a solid color."""
    return _krita_send("clear", {"color": color})


@mcp.tool()
def krita_save(path: str) -> Dict[str, Any]:
    """Save the current Krita document to path (.kra/.png/...)."""
    return _krita_send("save", {"path": path})


@mcp.tool()
def krita_get_color_at(x: int, y: int) -> Dict[str, Any]:
    """Sample the canvas color at (x, y) (eyedropper).

    Falls back to exporting the canvas and reading the pixel locally when the
    Krita plugin's native action fails (older plugin builds crash on bytes
    formatting until Krita is restarted with the patched plugin).
    """
    out = _krita_send("get_color_at", {"x": x, "y": y})
    if "error" not in out:
        return out
    probe = _krita_send("get_canvas", {"filename": "px_color_probe.png"}, timeout=120.0)
    path = probe.get("path", "")
    if not path or not Path(path).exists():
        return out
    pil = Image.open(path).convert("RGBA")
    xx, yy = min(max(0, x), pil.width - 1), min(max(0, y), pil.height - 1)
    r, g, b, a = pil.getpixel((xx, yy))
    return {
        "status": "ok",
        "color": "#%02x%02x%02x" % (r, g, b),
        "r": r, "g": g, "b": b, "a": a,
        "source": "canvas-probe",
    }


@mcp.tool()
def krita_list_brushes(filter: str = "", limit: int = 20) -> Dict[str, Any]:
    """List Krita brush presets (partial-name filter)."""
    return _krita_send("list_brushes", {"filter": filter, "limit": limit})


@mcp.tool()
def krita_open_file(path: str) -> Dict[str, Any]:
    """Open a file in Krita (.kra, .png, .jpg, ...)."""
    return _krita_send("open_file", {"path": path})


# --- krita v2 expansion (plugin actions_v2: layers, pixels, filters, selection,
# --- transforms, export, view, action/python escape hatches) --------------------
@mcp.tool()
def krita_document_info() -> Dict[str, Any]:
    """Active Krita document overview: name, filename, size, resolution, color
    model/depth, layer count, active layer, selection state, zoom, Krita version."""
    return _krita_send("document_info", {})


@mcp.tool()
def krita_list_layers() -> Dict[str, Any]:
    """Layer tree of the active document (top-of-stack first): name, type,
    visibility, lock, opacity, blending mode, bounds, nested children."""
    return _krita_send("list_layers", {})


@mcp.tool()
def krita_create_layer(name: str, type: str = "paintlayer", above: Optional[str] = None,
                       opacity: Optional[int] = None, blending_mode: Optional[str] = None) -> Dict[str, Any]:
    """Create a paint or group layer. above = existing layer name to stack it
    over (default: top). The new layer becomes active."""
    params: Dict[str, Any] = {"name": name, "type": type}
    if above:
        params["above"] = above
    if opacity is not None:
        params["opacity"] = int(opacity)
    if blending_mode:
        params["blending_mode"] = blending_mode
    return _krita_send("create_layer", params)


@mcp.tool()
def krita_set_active_layer(name: str) -> Dict[str, Any]:
    """Make a layer the active paint target (searched recursively by exact name)."""
    return _krita_send("set_active_layer", {"name": name})


@mcp.tool()
def krita_set_layer_props(name: str, visible: Optional[bool] = None, locked: Optional[bool] = None,
                          opacity: Optional[int] = None, blending_mode: Optional[str] = None,
                          new_name: Optional[str] = None) -> Dict[str, Any]:
    """Change layer visibility/lock/opacity/blending mode/name. Only the
    provided fields are touched."""
    params: Dict[str, Any] = {"name": name}
    if visible is not None:
        params["visible"] = bool(visible)
    if locked is not None:
        params["locked"] = bool(locked)
    if opacity is not None:
        params["opacity"] = int(opacity)
    if blending_mode:
        params["blending_mode"] = blending_mode
    if new_name:
        params["new_name"] = new_name
    return _krita_send("set_layer_props", params)


@mcp.tool()
def krita_remove_layer(name: str) -> Dict[str, Any]:
    """Delete a layer (refuses to delete the only top-level layer)."""
    return _krita_send("remove_layer", {"name": name})


@mcp.tool()
def krita_duplicate_layer(name: str, new_name: str = "") -> Dict[str, Any]:
    """Duplicate a layer with its full pixel content; the clone lands directly
    above the source and becomes active."""
    params = {"name": name}
    if new_name:
        params["new_name"] = new_name
    return _krita_send("duplicate_layer", params)


@mcp.tool()
def krita_merge_layer_down(name: str) -> Dict[str, Any]:
    """Merge a layer into the one below it."""
    return _krita_send("merge_layer_down", {"name": name})


@mcp.tool()
def krita_get_pixels(x: int = 0, y: int = 0, width: int = 0, height: int = 0,
                     layer: Optional[str] = None, output_path: Optional[str] = None,
                     as_base64: bool = False) -> Dict[str, Any]:
    """Export a canvas region as PNG (merged projection; layer=None reads the
    whole composite). width/height 0 = to the canvas edge. Returns the file
    path; as_base64 also returns the PNG bytes as a text field. Pair with
    px_view_image / read_image to LOOK at the canvas."""
    params: Dict[str, Any] = {"x": int(x), "y": int(y), "w": int(width), "h": int(height)}
    if layer:
        params["layer"] = layer
    if output_path:
        params["output_path"] = output_path
    if as_base64:
        params["as_base64"] = True
    return _krita_send("get_pixels", params, timeout=120.0)


@mcp.tool()
def krita_set_pixels(image_path: str, x: int = 0, y: int = 0, layer: Optional[str] = None) -> Dict[str, Any]:
    """Stamp a PNG file into a layer at (x, y) - the bridge for bringing
    Aseprite sprites or Retro Diffusion generations into Krita. layer=None
    targets the active layer."""
    params: Dict[str, Any] = {"image_path": image_path, "x": int(x), "y": int(y)}
    if layer:
        params["layer"] = layer
    return _krita_send("set_pixels", params, timeout=120.0)


@mcp.tool()
def krita_list_filters(filter: str = "") -> Dict[str, Any]:
    """List Krita filter names usable with krita_apply_filter (partial-name filter)."""
    return _krita_send("list_filters", {"filter": filter})


@mcp.tool()
def krita_adjust_pixels(
    operation: str,
    layer: Optional[str] = None,
    x: int = 0,
    y: int = 0,
    width: int = 0,
    height: int = 0,
    hue_shift: float = 0.0,
    saturation_shift: float = 0.0,
    value_shift: float = 0.0,
    radius: float = 1.0,
    brightness: float = 1.0,
    contrast: float = 1.0,
    bits: int = 4,
) -> Dict[str, Any]:
    """Crash-free image adjustments on a Krita layer region, computed with
    Pillow over the get_pixels/set_pixels PNG bridge (libkis Filter.apply()
    hard-crashes Krita 5.3.3, so native filters are quarantined; see
    krita_list_filters for names).

    operation: invert | grayscale | hsv (hue_shift -360..360,
    saturation_shift/value_shift -100..100) | blur (radius px) |
    bright_contrast (brightness/contrast factors, 1.0 = unchanged) |
    posterize (bits 1-8). width/height 0 = to the canvas edge; layer=None
    targets the active layer. Alpha is preserved throughout."""
    from PIL import ImageOps, ImageEnhance, ImageFilter
    op = str(operation).lower()
    if op not in ("invert", "grayscale", "hsv", "blur", "bright_contrast", "posterize"):
        raise ValueError("operation must be invert|grayscale|hsv|blur|bright_contrast|posterize")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_in = OUT_DIR / ("_krita_adj_in_%d.png" % os.getpid())
    tmp_out = OUT_DIR / ("_krita_adj_out_%d.png" % os.getpid())
    gp = krita_get_pixels(x=int(x), y=int(y), width=int(width), height=int(height),
                          layer=layer, output_path=str(tmp_in))
    if not gp.get("path"):
        return gp
    pil = Image.open(gp["path"]).convert("RGBA")
    alpha = pil.getchannel("A")
    if op == "invert":
        r, g, b, _a = pil.split()
        pil = Image.merge("RGBA", (ImageOps.invert(r), ImageOps.invert(g), ImageOps.invert(b), alpha))
    elif op == "grayscale":
        gray = pil.convert("L")
        pil = Image.merge("RGBA", (gray, gray, gray, alpha))
    elif op == "hsv":
        hsv = pil.convert("HSV")
        h, s, v = hsv.split()
        h = h.point(lambda p: int((p + float(hue_shift) * 255.0 / 360.0) % 256))
        s = s.point(lambda p: max(0, min(255, int(p * (1.0 + float(saturation_shift) / 100.0)))))
        v = v.point(lambda p: max(0, min(255, int(p * (1.0 + float(value_shift) / 100.0)))))
        pil = Image.merge("HSV", (h, s, v)).convert("RGB").convert("RGBA")
        pil.putalpha(alpha)
    elif op == "blur":
        rgb = pil.convert("RGB").filter(ImageFilter.GaussianBlur(max(0.1, float(radius))))
        pil = Image.merge("RGBA", (*rgb.split(), alpha))
    elif op == "bright_contrast":
        rgb = ImageEnhance.Brightness(pil.convert("RGB")).enhance(float(brightness))
        rgb = ImageEnhance.Contrast(rgb).enhance(float(contrast))
        pil = Image.merge("RGBA", (*rgb.split(), alpha))
    elif op == "posterize":
        rgb = ImageOps.posterize(pil.convert("RGB"), max(1, min(8, int(bits))))
        pil = Image.merge("RGBA", (*rgb.split(), alpha))
    pil.save(tmp_out)
    res = krita_set_pixels(str(tmp_out), x=int(x), y=int(y), layer=gp.get("layer"))
    for p in (tmp_in, tmp_out):
        try:
            Path(p).unlink()
        except OSError:
            pass
    if res.get("status") != "ok":
        return res
    return {"status": "ok", "operation": op, "layer": gp.get("layer"),
            "region": [int(x), int(y), gp.get("width"), gp.get("height")]}


@mcp.tool()
def krita_set_selection(x: int = 0, y: int = 0, width: int = 0, height: int = 0,
                        all: bool = False) -> Dict[str, Any]:
    """Set the global selection to a rectangle, or the whole canvas (all=True)."""
    p: Dict[str, Any] = {}
    if all:
        p["all"] = True
    else:
        p.update({"x": int(x), "y": int(y), "w": int(width), "h": int(height)})
    return _krita_send("set_selection", p)


@mcp.tool()
def krita_modify_selection(op: str, radius: int = 2, x_radius: int = 0, y_radius: int = 0,
                           edge_lock: bool = True) -> Dict[str, Any]:
    """Modify the current selection: invert | feather(radius) |
    grow(x_radius,y_radius) | shrink(x_radius,y_radius,edge_lock) | smooth |
    erode | dilate | border(x_radius,y_radius)."""
    p: Dict[str, Any] = {"op": op, "radius": int(radius), "edge_lock": bool(edge_lock),
                         "x_radius": int(x_radius or radius), "y_radius": int(y_radius or radius)}
    return _krita_send("modify_selection", p)


@mcp.tool()
def krita_get_selection() -> Dict[str, Any]:
    """Current global selection: exists flag plus bounding box."""
    return _krita_send("get_selection", {})


@mcp.tool()
def krita_clear_selection() -> Dict[str, Any]:
    """Deselect everything."""
    return _krita_send("clear_selection", {})


@mcp.tool()
def krita_resize_image(x: int = 0, y: int = 0, width: int = 0, height: int = 0) -> Dict[str, Any]:
    """Resize the canvas without scaling content (crop or expand). x/y is the
    offset of the old canvas inside the new one (negative crops)."""
    return _krita_send("resize_image", {"x": int(x), "y": int(y), "width": int(width), "height": int(height)})


@mcp.tool()
def krita_scale_image(width: int, height: int, xres: float = 0, yres: float = 0,
                      strategy: str = "Bilinear") -> Dict[str, Any]:
    """Scale the whole image to new pixel dimensions (resampling strategy:
    Bilinear | NearestNeighbor | Lanczos3 ...). xres/yres 0 keeps the current
    resolution."""
    return _krita_send("scale_image", {"width": int(width), "height": int(height),
                                       "xres": int(xres), "yres": int(yres), "strategy": strategy},
                       timeout=120.0)


@mcp.tool()
def krita_flatten() -> Dict[str, Any]:
    """Flatten all layers of the active document into one."""
    return _krita_send("flatten", {})


@mcp.tool()
def krita_export_layers(output_dir: Optional[str] = None) -> Dict[str, Any]:
    """Export every top-level layer as its own PNG (trimmed to its bounds).
    Defaults to the plugin's canvas output dir; returns per-layer paths."""
    p: Dict[str, Any] = {}
    if output_dir:
        p["output_dir"] = output_dir
    return _krita_send("export_layers", p, timeout=120.0)


@mcp.tool()
def krita_set_canvas_view(zoom: Optional[float] = None, rotation: Optional[float] = None,
                          mirror: Optional[bool] = None, wrap_around: Optional[bool] = None) -> Dict[str, Any]:
    """Control the active view's canvas: zoom level (1.0 = 100%), rotation
    degrees, mirror, wrap-around mode. Only provided fields change; returns
    the resulting zoom/rotation."""
    p: Dict[str, Any] = {}
    if zoom is not None:
        p["zoom"] = float(zoom)
    if rotation is not None:
        p["rotation"] = float(rotation)
    if mirror is not None:
        p["mirror"] = bool(mirror)
    if wrap_around is not None:
        p["wrap_around"] = bool(wrap_around)
    return _krita_send("set_canvas_view", p)


@mcp.tool()
def krita_list_actions(filter: str = "", limit: int = 300) -> Dict[str, Any]:
    """List Krita QAction names runnable via krita_run_action (partial-name
    filter on name or display text)."""
    return _krita_send("list_actions", {"filter": filter, "limit": int(limit)})


@mcp.tool()
def krita_run_action(name: str) -> Dict[str, Any]:
    """Trigger any named Krita action (e.g. flatten_image, mirror_canvas,
    view_zoom_in, selectiontool...). Unknown names return did_you_mean
    suggestions from krita_list_actions."""
    return _krita_send("run_action", {"name": name})


@mcp.tool()
def krita_run_python(code: str) -> Dict[str, Any]:
    """Execute arbitrary Python inside Krita (escape hatch). The namespace
    provides: krita module, Krita/app, doc (active Document), view, InfoObject,
    Selection, QImage, os, json, math. print() is captured into the result's
    output field; assign to a variable named result to return a value (repr'd).

    WARNING: executes unrestricted code inside the running Krita process. Only
    pass code you trust."""
    return _krita_send("run_python", {"code": code}, timeout=120.0)


_RD_GATED = {
    "rd_generate", "rd_img2img", "rd_cn_txt2img", "rd_cn_img2img",
    "rd_neural_transform", "rd_neural_pixelate", "rd_neural_resize", "rd_neural_detail",
    "rd_api_txt2img", "rd_api_img2img", "rd_api_txt2anim", "rd_api_img2anim",
    "rd_rembg", "rd_texture_gen", "rd_palettize", "rd_color_transfer", "rd_kcentroid",
    "rd_palette", "rd_prompt_extract", "rd_translate", "rd_benchmark",
}
_ASE_GATED = {
    "ase_create_canvas", "ase_sprite_info", "ase_save_as", "ase_draw_pixels",
    "ase_draw_pixels_hex", "ase_get_pixels", "ase_draw_line", "ase_draw_rectangle",
    "ase_draw_circle", "ase_draw_contour", "ase_draw_with_dither", "ase_fill_area",
    "ase_copy_selection", "ase_cut_selection", "ase_paste_clipboard", "ase_apply_outline",
    "ase_apply_shading", "ase_suggest_antialiasing", "ase_get_palette", "ase_set_palette",
    "ase_set_palette_color", "ase_add_palette_color", "ase_sort_palette", "ase_add_frame",
    "ase_delete_frame", "ase_duplicate_frame", "ase_set_frame_duration", "ase_create_tag",
    "ase_delete_tag", "ase_add_layer", "ase_delete_layer", "ase_flatten_layers",
    "ase_link_cel", "ase_scale_sprite", "ase_downsample", "ase_resize_canvas",
    "ase_rotate_sprite", "ase_flip_sprite", "ase_crop_sprite", "ase_trim_sprite",
    "ase_color_mode", "ase_export_sprite", "ase_export_spritesheet", "ase_import_layer",
    "ase_add_frames", "ase_view_frame",
    # v4 expansion (ported from diivi/aseprite-mcp); the two pure-computation
    # palette tools (ase_list_palette_presets, ase_generate_color_ramp) stay
    # ungated on purpose - they need no Aseprite.
    "ase_rename_layer", "ase_duplicate_layer", "ase_reorder_layer",
    "ase_set_layer_blend_mode", "ase_merge_layer_down", "ase_set_layer_opacity",
    "ase_set_layer_visibility", "ase_replace_color", "ase_adjust_hsl",
    "ase_erase_color", "ase_dither_gradient", "ase_apply_palette_preset",
    "ase_quantize_to_palette", "ase_add_frames_blank", "ase_set_frame_duration_all",
    "ase_create_cel", "ase_clear_cel", "ase_copy_cel", "ase_set_cel_position",
    "ase_set_cel_opacity", "ase_propagate_cels", "ase_tween_cel_positions",
    "ase_oscillate_cel_positions", "ase_create_slice", "ase_set_slice_center",
    "ase_set_slice_pivot", "ase_list_slices", "ase_delete_slice",
    "ase_create_tilemap_layer", "ase_draw_on_tile", "ase_set_tiles",
    "ase_get_tile_at", "ase_get_tilemap_info", "ase_render_onion_skin",
    "ase_compare_frames", "ase_get_color_stats", "ase_export_layers",
    "ase_export_tag", "ase_run_lua",
}


_KRITA_GATED = {
    "krita_new_canvas", "krita_set_color", "krita_set_brush", "krita_stroke",
    "krita_fill", "krita_draw_shape", "krita_get_canvas", "krita_undo",
    "krita_redo", "krita_clear", "krita_save", "krita_get_color_at",
    "krita_list_brushes", "krita_open_file",
    # v2 expansion (plugin actions_v2)
    "krita_document_info", "krita_list_layers", "krita_create_layer",
    "krita_set_active_layer", "krita_set_layer_props", "krita_remove_layer",
    "krita_duplicate_layer", "krita_merge_layer_down", "krita_get_pixels",
    "krita_set_pixels", "krita_list_filters", "krita_adjust_pixels",
    "krita_set_selection", "krita_modify_selection", "krita_get_selection",
    "krita_clear_selection", "krita_resize_image", "krita_scale_image",
    "krita_flatten", "krita_export_layers", "krita_set_canvas_view",
    "krita_list_actions", "krita_run_action", "krita_run_python",
}


def _install_gates() -> None:
    async def _apply() -> None:
        for tool in await mcp.list_tools():
            name = tool.name
            orig = tool.fn
            if name in _RD_GATED:
                async def rd_wrapped(*args: Any, _orig: Any = orig, **kwargs: Any) -> Any:
                    gate = await _require_rd()
                    if gate is not None:
                        return gate
                    ri = kwargs.pop("return_image", False)
                    res = await _orig(*args, **kwargs)
                    if ri and isinstance(res, dict):
                        p = _first_image_path(res)
                        if p:
                            return [res, _image_content(p, 512)]
                    return res
                tool.fn = rd_wrapped
            elif name in _ASE_GATED:
                try:
                    sig_params = dict(inspect.signature(orig).parameters)
                except (TypeError, ValueError):
                    sig_params = {}
                def ase_wrapped(
                    *args: Any,
                    _orig: Any = orig,
                    _name: str = name,
                    _params: Any = sig_params,
                    _alias_layer: bool = ("layer" in sig_params and "layer_name" not in sig_params),
                    **kwargs: Any,
                ) -> Any:
                    gate = _require_ase()
                    if gate is not None:
                        return gate
                    # Agent callers habitually pass layer_name (the spelling used
                    # by ase_add_layer & friends) to drawing tools whose
                    # parameter is `layer`; accept it as an alias.
                    if _alias_layer and "layer_name" in kwargs and "layer" not in kwargs:
                        kwargs["layer"] = kwargs.pop("layer_name")
                    try:
                        return _orig(*args, **kwargs)
                    except TypeError as exc:
                        traceback.print_exc()
                        return {
                            "ok": False,
                            "tool": _name,
                            "error": f"TypeError: {exc}",
                            "accepted_params": sorted(_params),
                            "hint": "Call again using only the names in accepted_params.",
                        }
                tool.fn = ase_wrapped
            elif name in _KRITA_GATED:
                def krita_wrapped(*args: Any, _orig: Any = orig, **kwargs: Any) -> Any:
                    gate = _require_krita()
                    if gate is not None:
                        return gate
                    return _orig(*args, **kwargs)
                tool.fn = krita_wrapped

    asyncio.run(_apply())


_install_gates()


def main() -> None:
    """Console entry point (`pixelworks` when installed via pip): stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()


