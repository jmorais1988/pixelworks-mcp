r"""Integration suite for the v4 expansion tools (ported from diivi/aseprite-mcp).
Direct-import style like test_workbench.py; exercises every new tool against
the real headless Aseprite CLI. Run: .venv\Scripts\python tests\test_v4_expansion.py
"""
import json
import shutil
import sys
from pathlib import Path

# Import server.py from the folder above tests/ (works from any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as S  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {detail}"), flush=True)


def T(cond, detail=""):
    return lambda: (bool(cond), detail or str(cond))


OUT = S.OUT_DIR / "_v4_suite"
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)
sp = str(OUT / "suite.aseprite")
sp2 = str(OUT / "tile.aseprite")

# --- setup -------------------------------------------------------------------
S.ase_create_canvas(48, 32, "rgb", sp)
check("create_canvas", T(Path(sp).exists()))
S.ase_add_layer(sp, "body")
S.ase_add_layer(sp, "fx")
base_layer = S.ase_sprite_info(sp)["layers"][0]["name"]
S.ase_draw_rectangle(sp, 4, 4, 16, 12, "#FF0000", True, "body", 1)

# --- layer management ----------------------------------------------------------
def layer_names():
    return [l["name"] for l in S.ase_sprite_info(sp)["layers"]]


def layer_order_lua():
    out = S.ase_run_lua('for _, l in ipairs(app.activeSprite.layers) do print("L:" .. l.name) end', sp)["output"]
    return [o[2:] for o in out if o.startswith("L:")]


S.ase_rename_layer(sp, "body", "torso")
check("rename_layer", T("torso" in layer_names() and "body" not in layer_names(), str(layer_names())))
S.ase_duplicate_layer(sp, "torso")
check("duplicate_layer", T("torso copy" in layer_names(), str(layer_names())))
S.ase_set_layer_blend_mode(sp, "fx", "multiply")
blend = S.ase_run_lua(
    'for _, l in ipairs(app.activeSprite.layers) do if l.name == "fx" then print("B:" .. tostring(l.blendMode)) end end', sp)["output"]
check("set_layer_blend_mode", T(any(o.startswith("B:") and not o.endswith("0") for o in blend), str(blend)))
S.ase_set_layer_opacity(sp, "fx", 128)
opa = S.ase_run_lua(
    'for _, l in ipairs(app.activeSprite.layers) do if l.name == "fx" then print("O:" .. l.opacity) end end', sp)["output"]
check("set_layer_opacity", T(any("O:128" in o for o in opa), str(opa)))
S.ase_set_layer_visibility(sp, "fx", False)
vis = S.ase_run_lua(
    'for _, l in ipairs(app.activeSprite.layers) do if l.name == "fx" then print("V:" .. tostring(l.isVisible)) end end', sp)["output"]
check("set_layer_visibility", T(any("V:false" in o for o in vis), str(vis)))
S.ase_reorder_layer(sp, "fx", 1)
order = layer_order_lua()
check("reorder_layer", T(order and order[0] == "fx", str(order)))
r = S.ase_merge_layer_down(sp, "torso copy")
check("merge_layer_down", T(r.get("ok") and "torso copy" not in layer_order_lua(), str(layer_order_lua())))

# --- color effects -------------------------------------------------------------
r = S.ase_replace_color(sp, "#FF0000", "#00FF00", layer="torso", frame=1)
check("replace_color", T(r["replaced"] > 100, str(r)))
px = S.ase_get_pixels(sp, 8, 8, 1, 1, "torso", 1)["pixels"][0]
check("replace_color_effect", T(px["hex"].lower().startswith("#00ff00"), px["hex"]))
S.ase_adjust_hsl(sp, 0.0, 0.0, -20.0, layer="torso", frame=1)
px2 = S.ase_get_pixels(sp, 8, 8, 1, 1, "torso", 1)["pixels"][0]
check("adjust_hsl", T(px2["g"] < px["g"], f"{px['hex']} -> {px2['hex']}"))
r = S.ase_erase_color(sp, px2["hex"][:7], layer="torso", frame=1, tolerance=12)
check("erase_color", T(r["erased"] > 100, str(r)))
S.ase_dither_gradient(sp, 0, 0, 16, 16, "#000000", "#FFFFFF", False, base_layer, 1)
pxa = S.ase_get_pixels(sp, 0, 0, 16, 16, base_layer, 1)["pixels"]
cols = set(p["hex"][:7].lower() for p in pxa if p["a"] > 0)
check("dither_gradient", T(cols == {"#000000", "#ffffff"}, str(cols)))

# --- palettes & ramps ------------------------------------------------------------
presets = S.ase_list_palette_presets()["presets"]
check("list_palette_presets", T(len(presets) == 8 and len(presets["pico8"]) == 16, str(sorted(presets))))
S.ase_apply_palette_preset(sp, "gameboy")
pal = [c.upper() for c in S.ase_get_palette(sp)["colors"]]
check("apply_palette_preset", T(pal[:4] == ["#0F380F", "#306230", "#8BAC0F", "#9BBC0F"], str(pal[:5])))
ramp = S.ase_generate_color_ramp("#D04648", 5)["ramp"]


def lum(hx):
    return 0.2126 * int(hx[1:3], 16) + 0.7152 * int(hx[3:5], 16) + 0.0722 * int(hx[5:7], 16)


check("generate_color_ramp", T(len(ramp) == 5 and lum(ramp[0]) < lum(ramp[-1]), str(ramp)))
S.ase_draw_rectangle(sp, 24, 4, 8, 8, "#123456", True, "torso", 1)
r = S.ase_quantize_to_palette(sp, layer="torso", frame=1)
check("quantize_to_palette", T(r["quantized"] > 30, str(r)))

# --- frames & cels ----------------------------------------------------------------
r = S.ase_add_frames_blank(sp, 3, 200)
check("add_frames_blank", T(r["frames"] == 4, str(r)))
S.ase_set_frame_duration_all(sp, 150)
durs = [f["duration_ms"] for f in S.ase_sprite_info(sp)["frames"]]
check("set_frame_duration_all", T(durs == [150] * 4, str(durs)))
r = S.ase_copy_cel(sp, 1, 2, layer="torso")
check("copy_cel", T(r.get("ok"), str(r)))
S.ase_set_cel_position(sp, 6, 2, layer="torso", frame=2)


def cel_pos(layer, frame):
    out = S.ase_run_lua(
        'local l = nil for _, x in ipairs(app.activeSprite.layers) do if x.name == "' + layer +
        '" then l = x end end local c = l:cel(app.activeSprite.frames[' + str(frame) +
        ']) if c then print("P:" .. c.position.x .. "," .. c.position.y) else print("P:none") end', sp)["output"]
    return [o[2:] for o in out if o.startswith("P:")][0]


check("set_cel_position", T(cel_pos("torso", 2) == "6,2", cel_pos("torso", 2)))
S.ase_set_cel_opacity(sp, 128, layer="torso", frame=2)
opo = S.ase_run_lua(
    'local l = nil for _, x in ipairs(app.activeSprite.layers) do if x.name == "torso" then l = x end end print("CO:" .. l:cel(app.activeSprite.frames[2]).opacity)', sp)["output"]
check("set_cel_opacity", T(any("CO:128" in o for o in opo), str(opo)))
S.ase_create_cel(sp, layer="fx", frame=2)
S.ase_clear_cel(sp, layer="fx", frame=2)
check("create_clear_cel", T(cel_pos("fx", 2) == "none", cel_pos("fx", 2)))
r = S.ase_propagate_cels(sp, 1, 2, 4, layers=["torso"])
check("propagate_cels", T(r.get("ok") and cel_pos("torso", 3) != "none" and cel_pos("torso", 4) != "none", ""))
S.ase_tween_cel_positions(sp, 1, 4, 0, 0, 15, 0, "smoothstep", layer="torso")
p1, p2, p4 = cel_pos("torso", 1), cel_pos("torso", 2), cel_pos("torso", 4)
check("tween_cel_positions", T(p1 == "0,0" and p4 == "15,0" and p2 not in ("0,0", "15,0"), f"{p1} {p2} {p4}"))
S.ase_oscillate_cel_positions(sp, 1, 4, 0, 3, 1.0, 0.0, layer="torso")
q2 = cel_pos("torso", 2)
check("oscillate_cel_positions", T(q2 != p2, f"was {p2} now {q2}"))

# --- slices -------------------------------------------------------------------------
S.ase_create_slice(sp, "head", 0, 0, 16, 16)
S.ase_set_slice_center(sp, "head", 4, 4, 8, 8)
S.ase_set_slice_pivot(sp, "head", 8, 8)
sl = S.ase_list_slices(sp)["slices"]
check("slices", T(len(sl) == 1 and sl[0]["name"] == "head" and sl[0].get("center", {}).get("width") == 8
                  and sl[0].get("pivot", {}).get("x") == 8, json.dumps(sl)))
S.ase_delete_slice(sp, "head")
check("delete_slice", T(len(S.ase_list_slices(sp)["slices"]) == 0))

# --- tilemaps -------------------------------------------------------------------------
S.ase_create_canvas(32, 32, "rgb", sp2)
r = S.ase_create_tilemap_layer(sp2, "map", 8, 8)
check("create_tilemap_layer", T(r.get("ok"), str(r)))
r = S.ase_draw_on_tile(sp2, "map", 1, [{"x": i, "y": i, "color": "#FF0000"} for i in range(8)])
check("draw_on_tile", T(r.get("ok") and r["pixels"] == 8, str(r)))
tpx = S.ase_run_lua(
    'local t = nil for _, l in ipairs(app.activeSprite.layers) do if l.name == "map" then t = l end end'
    ' print("TP:" .. tostring(app.pixelColor.rgbaR(t.tileset:tile(1).image:getPixel(3, 3))))', sp2)["output"]
check("draw_on_tile_pixels", T(any("TP:255" in o for o in tpx), str(tpx)))
r = S.ase_set_tiles(sp2, "map", [{"col": 0, "row": 0, "tile_index": 1}, {"col": 1, "row": 1, "tile_index": 1}], frame=1)
check("set_tiles", T(r.get("placed") == 2, str(r)))
a = S.ase_get_tile_at(sp2, "map", 0, 0, 1)
b = S.ase_get_tile_at(sp2, "map", 2, 2, 1)
check("get_tile_at", T(a["tile_index"] == 1 and b["tile_index"] == 0, f"{a} {b}"))
r = S.ase_get_tilemap_info(sp2, "map")
check("get_tilemap_info", T(r["tile_width"] == 8 and r["tile_count"] == 1 and r["map_cols"] == 4, str(r)))

# --- analysis ----------------------------------------------------------------------------
r = S.ase_compare_frames(sp, 1, 2)
check("compare_frames", T(r["changed_pixels"] > 0 and "changed_bounds" in r, str(r)))
r = S.ase_render_onion_skin(sp, 2, 1, 1, 2)
from PIL import Image
im = Image.open(r["path"])
check("render_onion_skin", T(im.size == (96, 64), f"{r['path']} {im.size}"))
r = S.ase_get_color_stats(sp, 1, 8)
check("get_color_stats", T(r["unique_colors"] >= 1 and r["opaque_pixels"] > 0 and len(r["top_colors"]) <= 8, str(r)))

# --- export & scripting ---------------------------------------------------------------------
r = S.ase_export_layers(sp)
check("export_layers", T(len(r["files"]) >= 1, str(r)))
S.ase_create_tag(sp, "walk", 1, 4)
gif = str(OUT / "walk.gif")
r = S.ase_export_tag(sp, "walk", gif)
check("export_tag", T(Path(gif).exists(), gif))
r = S.ase_run_lua('print("W=" .. app.activeSprite.width)', sp)
check("run_lua", T(any("W=48" in o for o in r["output"]), str(r)))

print()
print(f"PASSED {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    sys.exit(1)
print("ALL GREEN")

