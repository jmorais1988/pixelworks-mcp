r"""Aseprite workbench smoke test: canvas, drawing, selection/clipboard, transforms,
palette, frames/tags/layers, color mode and export - against the real headless
Aseprite CLI. Run: .venv\Scripts\python tests\test_workbench.py
"""
import json
import sys
from pathlib import Path

# Import server.py from the folder above tests/ (works from any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as S  # noqa: E402

S.OUT_DIR.mkdir(parents=True, exist_ok=True)
TMP = str(S.OUT_DIR / "wb_test.aseprite")
res = {}
def step(name, fn):
    try:
        res[name] = fn()
    except Exception as e:
        res[name] = "ERR: " + str(e)[:180]

step("create", lambda: S.ase_create_canvas(32, 32, "rgb", TMP))
step("draw_hex", lambda: S.ase_draw_pixels_hex(TMP, [{"x": x, "y": y, "color": "#e05830"} for y in range(8, 24) for x in range(8, 24)]))
step("get", lambda: {"n": len(S.ase_get_pixels(TMP, 8, 8, 4, 4)["pixels"]), "first": S.ase_get_pixels(TMP, 8, 8, 1, 1)["pixels"][0]["hex"]})
step("fill", lambda: S.ase_fill_area(TMP, 10, 10, "#f8a838"))
step("select", lambda: S.ase_select_rectangle(8, 8, 16, 16))
step("copy", lambda: S.ase_copy_selection(TMP))
step("paste", lambda: S.ase_paste_clipboard(TMP, 16, 16))
step("line", lambda: S.ase_draw_line(TMP, 0, 0, 31, 31, "#481408", 1))
step("circle", lambda: S.ase_draw_circle(TMP, 16, 16, 6, "#f8e878", False))
step("rect", lambda: S.ase_draw_rectangle(TMP, 2, 2, 6, 6, "#983820", True))
step("dither", lambda: S.ase_draw_with_dither(TMP, 24, 2, 6, 6, "#f8f8f8", "#481408", "bayer_4x4", 0.5))
step("outline", lambda: S.ase_apply_outline(TMP, "#201008", 1))
step("rotate", lambda: S.ase_rotate_sprite(TMP, 90))
step("flip", lambda: S.ase_flip_sprite(TMP, "horizontal"))
step("crop", lambda: S.ase_crop_sprite(TMP, 4, 4, 24, 24))
step("trim", lambda: S.ase_trim_sprite(TMP))
step("palette_set", lambda: S.ase_set_palette(TMP, ["#000000", "#e05830", "#f8a838", "#f8e878", "#ffffff"]))
step("palette_get", lambda: S.ase_get_palette(TMP)["colors"])
step("palette_sort", lambda: S.ase_sort_palette(TMP, "hue")["ok"])
step("frame_add", lambda: S.ase_add_frame(TMP, 150))
step("frame_dup", lambda: S.ase_duplicate_frame(TMP, 1, 1))
step("frame_dur", lambda: S.ase_set_frame_duration(TMP, 2, 250))
step("tag", lambda: S.ase_create_tag(TMP, "idle", 1, 3, "pingpong"))
step("layer_add", lambda: S.ase_add_layer(TMP, "fx"))
step("info", lambda: S.ase_sprite_info(TMP))
step("colormode", lambda: S.ase_color_mode(TMP, "indexed"))
step("sheet", lambda: S.ase_export_spritesheet(TMP, TMP.replace(".aseprite", "_sheet.png"), "horizontal", 0, True))
step("export_gif", lambda: S.ase_export_sprite(TMP, TMP.replace(".aseprite", ".gif"), "gif", 0))
print(json.dumps(res, indent=1, default=str))

failed = [k for k, v in res.items() if isinstance(v, str) and v.startswith("ERR:")]
print()
print(f"PASSED {len(res) - len(failed)} / {len(res)}")
if failed:
    print("FAILED:", ", ".join(failed))
    sys.exit(1)
print("ALL GREEN")
