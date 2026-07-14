# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du compositing du plugin `split` — chantier « portage mvk » (noyau C).
#
# ★ Ce banc exécute LE VRAI script.py du plugin (rendu par str.format, comme au déploiement),
#   pas une maquette : seules les E/S MXL (Instance/Reader/Writer) sont neutralisées. Le
#   compositing mesuré est donc EXACTEMENT celui qui tourne en production.
#
# Usage (dans un conteneur bobi-compute) :
#   python3 bench_split.py --script /work/script.py --res 1080 [--frames 40] [--hash]
#   BOBI_SPLIT_NOMVK=1 python3 bench_split.py …     → force le repli numpy (référence)
import argparse, hashlib, json, os, sys, time, types

import numpy as np


def load_plugin(script_path, width, height, chroma="422", depth=8, fps=50):
    """Rend le template du plugin et exécute tout SAUF la boucle principale."""
    src = open(script_path).read()
    cut = src.index("out = _ouvrir_sortie(SHM_OUT)")
    src = src[:cut]
    cfg = {"shm_out": "bench_out", "width": width, "height": height, "fps": fps,
           "chroma": chroma, "bit_depth": depth, "genlock": False}
    code = src.format(config=repr(cfg), hostname="bench", plugin_version="bench")

    import bobimxl                                   # le VRAI bobimxl (wrappers mvk compris)

    class _FakeInst:
        def garbage_collect(self):
            pass
    bobimxl.Instance = lambda *a, **k: _FakeInst()   # pas de domaine MXL sur le banc

    mod = types.ModuleType("split_plugin")
    mod.__dict__["__file__"] = script_path
    exec(compile(code, script_path, "exec"), mod.__dict__)
    return mod


def synth_frame(m, seed):
    """Trame source planar (Y,U,V) au format du canvas — motif déterministe, non compressible."""
    rng = np.random.default_rng(seed)
    y = rng.integers(16, 235, size=(m.HEIGHT, m.WIDTH), dtype=np.uint16).astype(m._NP_DT) * m._SCALE
    u = rng.integers(16, 240, size=(m.UV_H, m.UV_W), dtype=np.uint16).astype(m._NP_DT) * m._SCALE
    v = rng.integers(16, 240, size=(m.UV_H, m.UV_W), dtype=np.uint16).astype(m._NP_DT) * m._SCALE
    return y, u, v


# Mêmes réglages que le profil de référence 0.9.x (tableau du chantier) → chiffres comparables.
_BORD = {"inner": 10.0, "bevel": 0.8, "color": "#d4af37"}
_SHAD = {"enabled": True, "dx": 10, "dy": 12, "blur": 10, "opacity": 0.6}
EFFECTS = {
    "socle":         {},
    "adoucissement": {"feather": 10.0},
    "coins":         {"radius": 30.0},
    "ombre":         {"shadow": dict(_SHAD)},
    "bordure":       {"border": dict(_BORD)},
    "opacite":       {"opacity": 0.95},
    "rotation":      {"rotation": 8.0},
    "sans_rotation": {"feather": 10.0, "radius": 30.0, "opacity": 0.95,
                      "shadow": dict(_SHAD), "border": dict(_BORD)},
    "tout":          {"rotation": 8.0, "feather": 10.0, "radius": 30.0, "opacity": 0.95,
                      "shadow": dict(_SHAD), "border": dict(_BORD)},
}


def boxes_for(m, eff):
    base = [{"x": 0.28, "y": 0.30, "size": 0.42}, {"x": 0.72, "y": 0.30, "size": 0.42},
            {"x": 0.28, "y": 0.70, "size": 0.42}, {"x": 0.72, "y": 0.70, "size": 0.42}]
    out = []
    for b in base:
        d = dict(b); d["enabled"] = True
        d.update(json.loads(json.dumps(EFFECTS[eff])))
        out.append(m._norm_box(d))
    return out


def compose(m, canvas, srcs, boxes, tr=None):
    """Réplique EXACTEMENT le corps trame-entière de la boucle principale (script.py)."""
    oy, ou, ov = m._plane_views(canvas)
    for slot, b, alpha in m._plan_layers(tr, boxes):
        sy, su, sv = srcs[slot]
        if m._is_adv(b):
            st = m._stamp_for(slot, b, alpha)
            if st is None:
                continue
            m._paint_shadow_rows(oy, ou, ov, st, 0, m.HEIGHT)
            m._place_adv_rows(oy, ou, ov, sy, su, sv, st, 0, m.HEIGHT)
        else:
            g = m._box_slice_geom(b, alpha)
            if g is None:
                continue
            m._place_box_band(oy, ou, ov, sy, su, sv, g, 0, m.HEIGHT)
            m._paint_border_band(oy, ou, ov, g, 0, m.HEIGHT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default="/work/script.py")
    ap.add_argument("--res", default="1080", choices=("720", "1080"))
    ap.add_argument("--depth", type=int, default=8)
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--effects", default="")
    ap.add_argument("--hash", action="store_true", help="empreinte de la trame composée")
    ap.add_argument("--transition", action="store_true", help="mesure AVEC transition (stamp rebâti)")
    args = ap.parse_args()

    w, h = (1280, 720) if args.res == "720" else (1920, 1080)
    m = load_plugin(args.script, w, h, depth=args.depth)
    mvk = bool(getattr(m, "_MVK", False))
    print("# res=%dx%d depth=%d mvk=%s threads=%s" % (
        w, h, args.depth, mvk,
        (getattr(m, "bobimxl").mvk_threads() if hasattr(m.bobimxl, "mvk_threads") else "?")))

    bg = synth_frame(m, 1)
    srcs = {i: synth_frame(m, 10 + i) for i in range(4)}
    bgbuf = np.concatenate([bg[0].ravel().view(np.uint8), bg[1].ravel().view(np.uint8),
                            bg[2].ravel().view(np.uint8)])

    effs = args.effects.split(",") if args.effects else list(EFFECTS)
    for eff in effs:
        boxes = boxes_for(m, eff)
        m._stamps.clear()
        tr = None
        if args.transition:
            tgt = [dict(b) for b in boxes]
            for b in tgt:
                b["size"] = b["size"] * 0.8
            tr = {"from": boxes, "to": [m._norm_box(b) for b in tgt], "duration_ms": 2000,
                  "curve": "linear", "stagger_ms": 0.0, "_p_raw": 0.0}
        ts = []
        digest = None
        for i in range(args.frames + 3):
            canvas = bgbuf.copy()
            if tr is not None:
                tr["_p_raw"] = (i % 100) / 100.0     # géométrie qui BOUGE → stamp rebâti
            t0 = time.perf_counter()
            compose(m, canvas, srcs, boxes, tr)
            dt = (time.perf_counter() - t0) * 1000.0
            if i >= 3:                                # 3 trames de chauffe (cache de stamp)
                ts.append(dt)
            digest = hashlib.md5(canvas.tobytes()).hexdigest()[:16]
        ts.sort()
        line = "%-14s min %6.2f  med %6.2f  moy %6.2f ms" % (
            eff, ts[0], ts[len(ts) // 2], sum(ts) / len(ts))
        if args.hash:
            line += "   md5=%s" % digest
        print(line)
    miss = getattr(m, "_mvk_miss", None)
    if miss:
        print("# mvk_miss=%s why=%s" % (json.dumps(miss), json.dumps(getattr(m, "_mvk_why", {}))))


if __name__ == "__main__":
    main()
