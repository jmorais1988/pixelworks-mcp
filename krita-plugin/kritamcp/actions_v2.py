"""kritamcp actions v2 - expansion mixin for the Krita MCP Bridge plugin.

Adds document/layer/pixel/filter/selection/transform/export/view/action
capabilities on top of the original 14 paint commands, built on the libkis
Python API (Krita 5.x). Loaded by kritamcp.__init__ when present; the plugin
falls back to the original behaviour when this module is missing.

Lineage: the bridge plugin itself derives from nanayax3/krita-mcp (MIT,
Copyright (c) 2025); this expansion was written for the pixelworks MCP server.
"""

import base64
import contextlib
import io as _io
import os
import re

from krita import Krita, InfoObject, Selection
from PyQt5.QtGui import QImage


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))[:80] or "layer"


class ActionsV2:
    """New plugin actions. Each cmd2_* takes params dict, returns JSON dict."""

    # --- helpers ---------------------------------------------------------
    def _doc(self):
        return Krita.instance().activeDocument()

    def _view(self):
        app = Krita.instance()
        w = app.activeWindow()
        return w.activeView() if w else None

    def _find_node(self, doc, name):
        if not name:
            return doc.activeNode()
        found = doc.rootNode().findChildNodes(name, True)
        for n in found:
            if n.name() == name:
                return n
        return found[0] if found else None

    def _node_info(self, node, depth=0):
        d = {
            "name": node.name(),
            "type": node.type(),
            "visible": node.visible(),
            "locked": node.locked(),
            "opacity": node.opacity(),
        }
        try:
            d["blending_mode"] = node.blendingMode()
        except Exception:
            pass
        try:
            b = node.bounds()
            d["bounds"] = {"x": b.x(), "y": b.y(), "width": b.width(), "height": b.height()}
        except Exception:
            pass
        if depth < 8:
            kids = [self._node_info(c, depth + 1) for c in node.childNodes()]
            if kids:
                d["children"] = kids
        return d

    def _qimage_from_data(self, data, w, h):
        img = QImage(bytes(data), w, h, QImage.Format_ARGB32)
        return img.copy()  # detach from the Python buffer

    # --- document info & layers -------------------------------------------
    def cmd2_document_info(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        try:
            fname = doc.filename() or ""
        except Exception:
            fname = ""  # Document.filename() absent on some Krita builds
        info = {
            "status": "ok",
            "name": doc.name(),
            "filename": fname,
            "width": doc.width(),
            "height": doc.height(),
            "resolution": doc.resolution(),
            "color_model": doc.colorModel(),
            "color_depth": doc.colorDepth(),
            "top_level_layers": len(doc.topLevelNodes()),
            "has_selection": doc.selection() is not None,
        }
        try:
            info["modified"] = doc.isModified()
        except Exception:
            pass
        node = doc.activeNode()
        if node:
            info["active_layer"] = {"name": node.name(), "type": node.type()}
        view = self._view()
        if view:
            try:
                info["zoom"] = view.canvas().zoomLevel()
            except Exception:
                pass
        try:
            info["krita_version"] = Krita.instance().version()
        except Exception:
            pass
        return info

    def cmd2_list_layers(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        layers = [self._node_info(n) for n in doc.topLevelNodes()]
        layers.reverse()  # report top-of-stack first, like the Layers docker
        return {"status": "ok", "layers": layers}

    def cmd2_create_layer(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        name = params.get("name") or "layer"
        ntype = params.get("type") or "paintlayer"
        if ntype not in ("paintlayer", "grouplayer"):
            return {"error": "type must be paintlayer or grouplayer"}
        node = doc.createNode(name, ntype)
        above = None
        if params.get("above"):
            above = self._find_node(doc, params["above"])
            if above is None:
                return {"error": "above layer not found: " + str(params["above"])}
        parent = above.parentNode() if above is not None else doc.rootNode()
        if parent is None:
            parent = doc.rootNode()
        parent.addChildNode(node, above)
        if params.get("opacity") is not None:
            node.setOpacity(max(0, min(255, int(params["opacity"]))))
        if params.get("blending_mode"):
            node.setBlendingMode(str(params["blending_mode"]))
        doc.setActiveNode(node)
        doc.refreshProjection()
        return {"status": "ok", "name": node.name(), "type": ntype}

    def cmd2_set_active_layer(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        node = self._find_node(doc, params.get("name"))
        if node is None:
            return {"error": "layer not found: " + str(params.get("name"))}
        doc.setActiveNode(node)
        return {"status": "ok", "active_layer": node.name()}

    def cmd2_set_layer_props(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        node = self._find_node(doc, params.get("name"))
        if node is None:
            return {"error": "layer not found: " + str(params.get("name"))}
        changed = []
        if params.get("visible") is not None:
            node.setVisible(bool(params["visible"]))
            changed.append("visible")
        if params.get("locked") is not None:
            node.setLocked(bool(params["locked"]))
            changed.append("locked")
        if params.get("opacity") is not None:
            node.setOpacity(max(0, min(255, int(params["opacity"]))))
            changed.append("opacity")
        if params.get("blending_mode"):
            node.setBlendingMode(str(params["blending_mode"]))
            changed.append("blending_mode")
        if params.get("new_name"):
            node.setName(str(params["new_name"]))
            changed.append("name")
        doc.refreshProjection()
        return {"status": "ok", "layer": node.name(), "changed": changed}

    def cmd2_remove_layer(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        node = self._find_node(doc, params.get("name"))
        if node is None:
            return {"error": "layer not found: " + str(params.get("name"))}
        if len(doc.topLevelNodes()) <= 1 and node in doc.topLevelNodes():
            return {"error": "cannot remove the only top-level layer"}
        parent = node.parentNode() or doc.rootNode()
        ok = parent.removeChildNode(node)
        doc.refreshProjection()
        return {"status": "ok", "removed": bool(ok), "layer": node.name()}

    def cmd2_duplicate_layer(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        node = self._find_node(doc, params.get("name"))
        if node is None:
            return {"error": "layer not found: " + str(params.get("name"))}
        clone = node.duplicate()
        if params.get("new_name"):
            clone.setName(str(params["new_name"]))
        else:
            clone.setName(node.name() + " copy")
        parent = node.parentNode() or doc.rootNode()
        parent.addChildNode(clone, node)
        doc.setActiveNode(clone)
        doc.refreshProjection()
        return {"status": "ok", "layer": clone.name()}

    def cmd2_merge_layer_down(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        node = self._find_node(doc, params.get("name"))
        if node is None:
            return {"error": "layer not found: " + str(params.get("name"))}
        merged = node.mergeDown()
        doc.refreshProjection()
        return {"status": "ok", "layer": merged.name() if merged else None}

    # --- pixels (PNG bridge in/out) ----------------------------------------
    def cmd2_get_pixels(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        w = int(params.get("w", 0)) or (doc.width() - x)
        h = int(params.get("h", 0)) or (doc.height() - y)
        w = min(w, doc.width() - x)
        h = min(h, doc.height() - y)
        if w <= 0 or h <= 0:
            return {"error": "region out of bounds"}
        node = self._find_node(doc, params.get("layer")) if params.get("layer") else doc.rootNode()
        if node is None:
            return {"error": "layer not found: " + str(params.get("layer"))}
        data = node.projectionPixelData(x, y, w, h)
        img = self._qimage_from_data(data, w, h)
        out = params.get("output_path") or os.path.join(CANVAS_OUTPUT_DIR, "krita_pixels.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if not img.save(out, "PNG"):
            return {"error": "failed to write " + out}
        result = {"status": "ok", "path": out, "width": w, "height": h, "layer": node.name()}
        if params.get("as_base64"):
            with open(out, "rb") as fh:
                result["png_base64"] = base64.b64encode(fh.read()).decode("ascii")
        return result

    def cmd2_set_pixels(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        path = params.get("image_path")
        if not path or not os.path.exists(path):
            return {"error": "image_path not found: " + str(path)}
        node = self._find_node(doc, params.get("layer"))
        if node is None:
            return {"error": "no target layer (pass layer or select one)"}
        src = QImage(path)
        if src.isNull():
            return {"error": "could not decode image: " + str(path)}
        img = src.convertToFormat(QImage.Format_ARGB32)
        w, h = img.width(), img.height()
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        ptr = img.bits()
        ptr.setsize(img.sizeInBytes())
        data = bytes(ptr)
        node.setPixelData(data, x, y, w, h)
        doc.refreshProjection()
        return {"status": "ok", "layer": node.name(), "x": x, "y": y, "width": w, "height": h}

    # --- filters -------------------------------------------------------------
    def cmd2_list_filters(self, params):
        try:
            names = list(Krita.instance().filters())
        except Exception as e:
            return {"error": "filters() unavailable: " + str(e)}
        flt = str(params.get("filter", "")).lower()
        if flt:
            names = [n for n in names if flt in n.lower()]
        return {"status": "ok", "filters": sorted(names), "count": len(names)}

    def cmd2_apply_filter(self, params):
        # QUARANTINED (verified by staged bisection on Krita 5.3.3, git
        # 858d352): building Filter() + setName + setConfiguration is safe,
        # but Filter.apply(node, x, y, w, h) hard-crashes the whole Krita
        # process natively (uncatchable from Python). Do NOT call apply()
        # here until upstream fixes it. The pixelworks server provides
        # krita_adjust_pixels (Pillow over the get_pixels/set_pixels PNG
        # bridge) as the crash-free replacement for common adjustments.
        return {
            "error": "apply_filter is disabled: libkis Filter.apply() crashes "
                     "Krita 5.3.3 natively (verified by staged bisection). Use "
                     "the krita_adjust_pixels tool (invert/grayscale/hsv/blur/"
                     "bright_contrast/posterize via the Pillow bridge), or "
                     "filter masks / run_action for interactive work.",
            "quarantined": True,
        }

    def _cmd2_apply_filter_original_disabled(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        name = params.get("name")
        if not name:
            return {"error": "filter name required"}
        try:
            avail = list(Krita.instance().filters())
        except Exception:
            avail = []
        if avail and name not in avail:
            near = [n for n in avail if name.lower() in n.lower()][:8]
            return {"error": "unknown filter: " + str(name), "did_you_mean": near}
        info = InfoObject()
        for k, v in (params.get("params") or {}).items():
            info.setProperty(str(k), v)
        node = self._find_node(doc, params.get("layer"))
        if node is None:
            return {"error": "no target layer (pass layer or select one)"}
        if node.type() not in ("paintlayer", "clone", "filllayer"):
            return {"error": "filters apply to paint-type layers only; got: " + str(node.type())}
        x = max(0, min(int(params.get("x", 0)), doc.width() - 1))
        y = max(0, min(int(params.get("y", 0)), doc.height() - 1))
        w = int(params.get("w", 0)) or (doc.width() - x)
        h = int(params.get("h", 0)) or (doc.height() - y)
        w = max(1, min(w, doc.width() - x))
        h = max(1, min(h, doc.height() - y))
        f = None
        create = getattr(doc, "createFilter", None)
        if callable(create):
            try:
                f = create(name, info)
            except Exception:
                f = None
        if f is None:
            from krita import Filter
            f = Filter()
            f.setName(name)
            f.setConfiguration(info)
        ok = f.apply(node, x, y, w, h)
        doc.refreshProjection()
        return {"status": "ok", "applied": bool(ok), "filter": name, "layer": node.name()}

    # --- selection -----------------------------------------------------------
    def cmd2_set_selection(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        sel = Selection()
        if params.get("all"):
            sel.selectAll(doc.rootNode(), 255)
            box = {"all": True}
        else:
            x, y = int(params.get("x", 0)), int(params.get("y", 0))
            w, h = int(params.get("w", 0)), int(params.get("h", 0))
            if w <= 0 or h <= 0:
                return {"error": "w and h must be > 0 (or pass all=true)"}
            sel.select(x, y, w, h, 255)
            box = {"x": x, "y": y, "w": w, "h": h}
        doc.setSelection(sel)
        doc.refreshProjection()
        return {"status": "ok", "selection": box}

    def cmd2_modify_selection(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        sel = doc.selection()
        if sel is None:
            return {"error": "no selection to modify"}
        op = str(params.get("op", "")).lower()
        s = sel.duplicate()
        if op == "invert":
            s.invert(doc.rootNode()) if _sel_invert_takes_node() else s.invert()
        elif op == "feather":
            s.feather(int(params.get("radius", 2)))
        elif op == "grow":
            s.grow(int(params.get("x_radius", 2)), int(params.get("y_radius", 2)))
        elif op == "shrink":
            s.shrink(int(params.get("x_radius", 2)), int(params.get("y_radius", 2)), bool(params.get("edge_lock", True)))
        elif op == "smooth":
            s.smooth()
        elif op == "erode":
            s.erode()
        elif op == "dilate":
            s.dilate()
        elif op == "border":
            s.border(int(params.get("x_radius", 2)), int(params.get("y_radius", 2)))
        else:
            return {"error": "op must be invert|feather|grow|shrink|smooth|erode|dilate|border"}
        doc.setSelection(s)
        doc.refreshProjection()
        return {"status": "ok", "op": op}

    def cmd2_get_selection(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        sel = doc.selection()
        if sel is None:
            return {"status": "ok", "exists": False}
        return {"status": "ok", "exists": True, "x": sel.x(), "y": sel.y(),
                "width": sel.width(), "height": sel.height()}

    def cmd2_clear_selection(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        try:
            doc.setSelection(None)
        except Exception:
            sel = Selection()
            sel.clear()
            doc.setSelection(sel)
        doc.refreshProjection()
        return {"status": "ok"}


def _sel_invert_takes_node():
    """Selection.invert() signature changed across Krita versions (node arg
    added in 5.2+); probe once and cache."""
    if getattr(_sel_invert_takes_node, "_cached", None) is None:
        import inspect
        try:
            sig = inspect.signature(Selection.invert)
            _sel_invert_takes_node._cached = len(sig.parameters) > 1
        except (TypeError, ValueError):
            _sel_invert_takes_node._cached = True  # sip methods: assume modern
    return _sel_invert_takes_node._cached

# Same default as the bridge plugin's own config (module-local to avoid a
# circular import with kritamcp/__init__.py).
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")


class ActionsV2Part2(ActionsV2):
    # --- document transforms -------------------------------------------------
    def cmd2_resize_image(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        x, y = int(params.get("x", 0)), int(params.get("y", 0))
        w, h = int(params.get("width", doc.width())), int(params.get("height", doc.height()))
        if w <= 0 or h <= 0:
            return {"error": "width/height must be > 0"}
        doc.resizeImage(x, y, w, h)
        doc.refreshProjection()
        return {"status": "ok", "width": doc.width(), "height": doc.height(), "offset": [x, y]}

    def cmd2_scale_image(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        w, h = int(params.get("width", 0)), int(params.get("height", 0))
        if w <= 0 or h <= 0:
            return {"error": "width/height must be > 0"}
        xres = int(float(params.get("xres", 0)) or doc.resolution())
        yres = int(float(params.get("yres", 0)) or doc.resolution())
        strategy = str(params.get("strategy", "Bilinear"))
        doc.scaleImage(w, h, xres, yres, strategy)
        doc.refreshProjection()
        return {"status": "ok", "width": doc.width(), "height": doc.height()}

    def cmd2_flatten(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        doc.flatten()
        doc.refreshProjection()
        return {"status": "ok", "layers": len(doc.topLevelNodes())}

    def cmd2_export_layers(self, params):
        doc = self._doc()
        if not doc:
            return {"error": "No active document"}
        outdir = params.get("output_dir") or CANVAS_OUTPUT_DIR
        os.makedirs(outdir, exist_ok=True)
        files = []
        for node in doc.topLevelNodes():
            try:
                b = node.bounds()
                bw, bh = b.width(), b.height()
                if bw <= 0 or bh <= 0:
                    continue
                data = node.projectionPixelData(b.x(), b.y(), bw, bh)
                img = self._qimage_from_data(data, bw, bh)
                p = os.path.join(outdir, _safe_name(node.name()) + ".png")
                if img.save(p, "PNG"):
                    files.append({"layer": node.name(), "path": p, "x": b.x(), "y": b.y(),
                                  "width": bw, "height": bh})
            except Exception as e:
                files.append({"layer": node.name(), "error": str(e)})
        return {"status": "ok", "dir": outdir, "files": files}

    # --- canvas view ---------------------------------------------------------
    def cmd2_set_canvas_view(self, params):
        view = self._view()
        if not view:
            return {"error": "No active view"}
        canvas = view.canvas()
        applied = []
        if params.get("zoom") is not None:
            canvas.setZoomLevel(float(params["zoom"]))
            applied.append("zoom")
        if params.get("rotation") is not None:
            canvas.setRotation(float(params["rotation"]))
            applied.append("rotation")
        if params.get("mirror") is not None:
            canvas.setMirror(bool(params["mirror"]))
            applied.append("mirror")
        if params.get("wrap_around") is not None:
            canvas.setWrapAroundMode(bool(params["wrap_around"]))
            applied.append("wrap_around")
        return {"status": "ok", "applied": applied,
                "zoom": canvas.zoomLevel(), "rotation": canvas.rotation()}

    # --- Krita action escape hatches -------------------------------------------
    def cmd2_list_actions(self, params):
        flt = str(params.get("filter", "")).lower()
        limit = int(params.get("limit", 300))
        out = []
        for a in Krita.instance().actions():
            name = a.objectName()
            text = a.text() or ""
            if flt and flt not in name.lower() and flt not in text.lower():
                continue
            out.append({"name": name, "text": text})
            if len(out) >= limit:
                break
        return {"status": "ok", "actions": out, "count": len(out)}

    def cmd2_run_action(self, params):
        name = params.get("name")
        if not name:
            return {"error": "action name required"}
        a = Krita.instance().action(str(name))
        if a is None:
            near = [x.objectName() for x in Krita.instance().actions()
                    if str(name).lower() in x.objectName().lower()][:10]
            return {"error": "unknown action: " + str(name), "did_you_mean": near}
        a.trigger()
        return {"status": "ok", "action": name}

    def cmd2_run_python(self, params):
        code = params.get("code", "")
        if not code.strip():
            return {"error": "code cannot be empty"}
        doc = self._doc()
        view = self._view()
        import krita as _krita_mod
        ns = {
            "krita": _krita_mod, "Krita": Krita, "app": Krita.instance(),
            "doc": doc, "view": view,
            "InfoObject": InfoObject, "Selection": Selection, "QImage": QImage,
            "os": os, "json": __import__("json"), "math": __import__("math"),
        }
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "<krita-mcp-run_python>", "exec"), ns)
            out = {"status": "ok", "output": buf.getvalue()}
            if "result" in ns:
                out["result"] = repr(ns["result"])[:4000]
            return out
        except Exception as e:
            return {"error": type(e).__name__ + ": " + str(e), "output": buf.getvalue()}


# Auto-derived action table: every cmd2_* on either class becomes an action
# (name = method minus the cmd2_ prefix). Built after both classes exist so
# inherited methods resolve (a literal dict in the subclass body could not
# see the parent's methods at class-creation time).
V2_DISPATCH = {}
for _cls in (ActionsV2, ActionsV2Part2):
    for _n in dir(_cls):
        if _n.startswith("cmd2_"):
            V2_DISPATCH[_n[len("cmd2_"):]] = getattr(_cls, _n)
ActionsV2Part2.V2_DISPATCH = V2_DISPATCH

