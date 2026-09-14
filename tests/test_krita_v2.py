r"""Krita v2 bridge battery: document info, layer tree ops, pixel PNG bridge,
Pillow adjustments, selections, transforms, per-layer export, canvas view,
run_action / run_python. Needs Krita running with the bundled plugin enabled.
Run: .venv\Scripts\python tests\test_krita_v2.py
"""
import json
import sys
from pathlib import Path

# Import server.py from the folder above tests/ (works from any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image  # noqa: E402

import server as S  # noqa: E402

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" :: {detail}"), flush=True)

OUT = S.OUT_DIR / "_krita_v2"
OUT.mkdir(parents=True, exist_ok=True)

st = S.krita_status()
check("status", st.get("available"), str(st))

r = S.krita_new_canvas(width=64, height=64, name="px-v2-test", background="#ffffff")
check("new_canvas", r.get("status") == "ok", str(r))

info = S.krita_document_info()
check("document_info", info.get("width") == 64 and info.get("krita_version"), json.dumps(info)[:200])

ll = S.krita_list_layers()
names0 = [l["name"] for l in ll.get("layers", [])]
check("list_layers", len(names0) >= 1, str(names0))

r = S.krita_create_layer("ink", "paintlayer")
check("create_layer", r.get("status") == "ok", str(r))
ll = S.krita_list_layers()
check("create_layer_effect", any(l["name"] == "ink" for l in ll["layers"]), str([l["name"] for l in ll["layers"]]))

r = S.krita_set_active_layer("ink")
check("set_active_layer", r.get("active_layer") == "ink", str(r))

r = S.krita_set_layer_props("ink", opacity=200, blending_mode="multiply", new_name="ink2")
check("set_layer_props", r.get("status") == "ok" and "name" in r.get("changed", []), str(r))
ll = S.krita_list_layers()
ink2 = [l for l in ll["layers"] if l["name"] == "ink2"]
check("set_layer_props_effect", ink2 and ink2[0]["opacity"] == 200, json.dumps(ink2)[:200])

r = S.krita_duplicate_layer("ink2")
check("duplicate_layer", r.get("status") == "ok" and r.get("layer") == "ink2 copy", str(r))

r = S.krita_remove_layer("ink2 copy")
check("remove_layer", r.get("removed") is True, str(r))

# pixel bridge: stamp a red 16x16 PNG into the base paint layer
red = OUT / "red16.png"
Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(red)
base = names0[0]
r = S.krita_set_pixels(str(red), x=8, y=8, layer=base)
check("set_pixels", r.get("status") == "ok", str(r))

gp = S.krita_get_pixels(x=8, y=8, width=16, height=16, output_path=str(OUT / "probe1.png"))
check("get_pixels", Path(gp.get("path", "")).exists(), str(gp)[:200])
px = Image.open(OUT / "probe1.png").convert("RGB").getpixel((8, 8))
check("pixel_bridge_roundtrip", px[0] > 200 and px[1] < 60 and px[2] < 60, str(px))

fl = S.krita_list_filters()
check("list_filters", fl.get("count", 0) > 10, str(fl.get("count")))
r = S.krita_adjust_pixels("invert", layer=base, x=8, y=8, width=16, height=16)
check("adjust_pixels(invert)", r.get("status") == "ok", str(r))
gp = S.krita_get_pixels(x=8, y=8, width=16, height=16, layer=base, output_path=str(OUT / "probe2.png"))
px2 = Image.open(OUT / "probe2.png").convert("RGB").getpixel((8, 8))
check("adjust_pixels_effect", px2[0] < 60 and px2[1] > 190 and px2[2] > 190, f"{px} -> {px2}")
r = S.krita_adjust_pixels("posterize", layer=base, x=0, y=0, width=32, height=32, bits=2)
check("adjust_pixels(posterize)", r.get("status") == "ok", str(r))

r = S.krita_set_selection(x=0, y=0, width=32, height=32)
check("set_selection", r.get("status") == "ok", str(r))
gs = S.krita_get_selection()
check("get_selection", gs.get("exists") and gs.get("width") == 32, str(gs))
r = S.krita_modify_selection("feather", radius=2)
check("modify_selection", r.get("status") == "ok", str(r))
r = S.krita_clear_selection()
gs = S.krita_get_selection()
check("clear_selection", r.get("status") == "ok" and not gs.get("exists", True), str(gs))

r = S.krita_resize_image(x=0, y=0, width=80, height=80)
check("resize_image", r.get("width") == 80, str(r))
r = S.krita_scale_image(width=40, height=40)
check("scale_image", r.get("width") == 40, str(r))

el = S.krita_export_layers(output_dir=str(OUT))
ok_files = [f for f in el.get("files", []) if "path" in f]
check("export_layers", len(ok_files) >= 1 and Path(ok_files[0]["path"]).exists(), str(el)[:250])

cv = S.krita_set_canvas_view(zoom=2.0, mirror=True)
check("set_canvas_view", abs(cv.get("zoom", 0) - 2.0) < 0.01, str(cv))
S.krita_set_canvas_view(zoom=1.0, mirror=False)

la = S.krita_list_actions(filter="flatten")
check("list_actions", la.get("count", 0) >= 1, str(la)[:150])
r = S.krita_run_action("flatten_image")
check("run_action", r.get("status") == "ok", str(r))
ll = S.krita_list_layers()
check("flatten_effect", len(ll["layers"]) == 1, str([l["name"] for l in ll["layers"]]))
bad = S.krita_run_action("definitely_not_an_action")
check("run_action_did_you_mean", "error" in bad, str(bad)[:150])

cur_w = S.krita_document_info().get("width")
rp = S.krita_run_python("print('hello from krita', doc.width()); result = doc.width() * 2")
check("run_python", f"hello from krita {cur_w}" in rp.get("output", "") and rp.get("result") == str(cur_w * 2), str(rp)[:250])

# close all px test documents silently (incl. session-restored probe docs)
cl = S.krita_run_python(
    "n = 0\n"
    "for d in list(app.documents()):\n"
    "    if d.name().startswith('px-v2-test') or d.name().startswith('px-filter-probe'):\n"
    "        d.setModified(False)\n"
    "        d.close()\n"
    "        n += 1\n"
    "result = n")
check("cleanup_close", cl.get("status") == "ok", str(cl)[:200])

print()
print(f"PASSED {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    sys.exit(1)
print("ALL GREEN")
