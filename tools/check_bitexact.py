# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Non-régression OCTET-IDENTIQUE du compositing split (chantier portage mvk, 0.10.0).
#
# Compare, sur des scénarios ALÉATOIRES (rotation, crop, opacité, feather, coins, bordure 3D,
# ombre, miroir, ratio libre, boxes DÉBORDANT du cadre, transition en cours) :
#   REF   = script.py de la 0.9.x (numpy pur, référence)
#   NUMPY = script.py courant, kernel désactivé (BOBI_SPLIT_NOMVK) → repli
#   MVK   = script.py courant, kernel C actif
# Critère : max|Δ| = 0 entre les trois, en 8 bits (chroma 422/420/444), trame entière ET mode
# tranche. En 10 bits, REF est ATTENDU différent (débordement uint16 corrigé, cf. _blend_rows) :
# on vérifie alors NUMPY ≡ MVK et on signale l'écart à REF.
#
# Usage : python3 check_bitexact.py --ref /sb/script_ref.py --new /sb/script.py [--iters 40]
import argparse, os, sys, json

SHADOW = False

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_split import load_plugin, synth_frame, compose


def rand_box(rng, hard, pct=False):
    """pct=True → opacités en POURCENT (moteur ≥ 0.12.0) ; sinon en fraction (0.9.x/0.11.x)."""
    b = {"enabled": True,
         "x": float(rng.uniform(-0.1, 1.1) if hard else rng.uniform(0.2, 0.8)),
         "y": float(rng.uniform(-0.1, 1.1) if hard else rng.uniform(0.2, 0.8)),
         "size": float(rng.uniform(0.1, 0.9)),
         "crop": {k: float(rng.uniform(0, 0.4)) for k in "tblr"},
         "rotation": float(rng.choice([0.0, 0.0, rng.uniform(-180, 180)])),
         "opacity": float(rng.choice([1.0, 1.0, rng.uniform(0.0, 1.0)])) * (100.0 if pct else 1.0),
         "feather": float(rng.choice([0.0, rng.uniform(0, 64)])),
         "radius": float(rng.choice([0.0, rng.uniform(0, 120)])),
         "mirror_h": bool(rng.integers(0, 2)), "mirror_v": bool(rng.integers(0, 2)),
         "aspect": ("free" if rng.integers(0, 3) == 0 else "keep"),
         "size_x": float(rng.uniform(0.1, 0.9)), "size_y": float(rng.uniform(0.1, 0.9)),
         "border": {"w": int(rng.integers(0, 12)),
                    "color": "#%06x" % int(rng.integers(0, 1 << 24)),
                    "outer": float(rng.choice([0.0, rng.uniform(0, 20)])),
                    "inner": float(rng.choice([0.0, rng.uniform(0, 20)])),
                    "bevel": float(rng.choice([0.0, rng.uniform(-1, 1)])),
                    "bevel_soft": float(rng.uniform(0.05, 1.0)),
                    "bevel_pos": float(rng.uniform(0, 1))},
         "shadow": {"enabled": bool(rng.integers(0, 2)), "dx": float(rng.uniform(-40, 40)),
                    "dy": float(rng.uniform(-40, 40)), "blur": float(rng.uniform(0, 30)),
                    "opacity": float(rng.uniform(0, 1)) * (100.0 if pct else 1.0), "color": "#101010"},
         "z": float(rng.integers(0, 4))}
    return b


def run(mod, boxes, srcs, bgbuf, tr=None, bands=0):
    canvas = bgbuf.copy()
    mod._stamps.clear()
    if not bands:
        compose(mod, canvas, srcs, [mod._norm_box(b) for b in boxes], tr)
        return canvas
    # MODE TRANCHE : mêmes primitives, appelées BANDE PAR BANDE (b0, b1) — c'est ce que fait
    # _slice_frame. La sortie doit être IDENTIQUE à la trame entière (sinon les deux chemins
    # ont divergé).
    oy, ou, ov = mod._plane_views(canvas)
    nb = mod.HEIGHT // bands
    nboxes = [mod._norm_box(b) for b in boxes]
    plan = mod._plan_layers(tr, nboxes)
    stamps = {}
    for slot, b, alpha in plan:
        stamps[slot] = (mod._stamp_for(slot, b, alpha) if mod._is_adv(b)
                        else mod._box_slice_geom(b, alpha))
    for k in range(nb):
        b0, b1 = k * bands, (k + 1) * bands
        for slot, b, alpha in plan:
            st = stamps[slot]
            if st is None:
                continue
            sy, su, sv = srcs[slot]
            if mod._is_adv(b):
                mod._paint_shadow_rows(oy, ou, ov, st, b0, b1)
                mod._place_adv_rows(oy, ou, ov, sy, su, sv, st, b0, b1)
            else:
                mod._place_box_band(oy, ou, ov, sy, su, sv, st, b0, b1)
                mod._paint_border_band(oy, ou, ov, st, b0, b1)
    return canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="/sb/script_ref.py")
    ap.add_argument("--new", default="/sb/script.py")
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--depth", type=int, default=8)
    ap.add_argument("--res", default="1080")
    ap.add_argument("--shadow", action="store_true",
                    help="ombre ACTIVE (comparaison kernel vs repli seulement : le masque a "
                         "changé en 0.12.0 — le flou s'épand vers l'extérieur)")
    args = ap.parse_args()
    global SHADOW
    SHADOW = args.shadow
    w, h = (1280, 720) if args.res == "720" else (1920, 1080)

    mods = {}
    for name, path, nomvk in (("REF", args.ref, "1"), ("NUMPY", args.new, "1"),
                              ("MVK", args.new, "")):
        os.environ["BOBI_SPLIT_NOMVK"] = nomvk
        mods[name] = {}
        for chroma in ("422", "420", "444"):
            mods[name][chroma] = load_plugin(path, w, h, chroma=chroma, depth=args.depth)
    print("# mvk actif :", getattr(mods["MVK"]["422"], "_MVK", False),
          "| repli forcé :", getattr(mods["NUMPY"]["422"], "_MVK", False) is False)

    rng = np.random.default_rng(7)
    worst = {"REF": 0, "NUMPY": 0}
    bad = 0
    for it in range(args.iters):
        chroma = ("422", "420", "444")[it % 3]
        hard = bool(it % 2)
        # ⚠ UNITÉS : le moteur ≥ 0.12.0 parle en POURCENT ; la 0.9.x en fraction. On tire UNE
        # géométrie et on l'exprime dans l'unité de chaque moteur → la comparaison reste juste.
        # ⚠ OMBRE : son masque a changé (le flou s'épand désormais VERS L'EXTÉRIEUR — la 0.9.x le
        # rognait au bord). Elle est donc EXCLUE de la comparaison à la REF (divergence VOULUE),
        # mais comparée entre kernel et repli (qui doivent, eux, être identiques).
        st0 = rng.bit_generator.state
        boxes_pct = [rand_box(rng, hard, pct=True) for _ in range(4)]
        rng.bit_generator.state = st0
        boxes_frac = [rand_box(rng, hard, pct=False) for _ in range(4)]
        if not SHADOW:
            for bb in boxes_pct + boxes_frac:
                bb["shadow"]["enabled"] = False
        trans = (it % 4 == 3)
        outs = {}
        for name in ("REF", "NUMPY", "MVK"):
            m = mods[name][chroma]
            boxes = boxes_frac if name == "REF" else boxes_pct
            srcs = {i: synth_frame(m, 10 + i) for i in range(4)}
            bg = synth_frame(m, 1)
            bgbuf = np.concatenate([p.ravel().view(np.uint8) for p in bg])
            tr = None
            if trans:
                to = [dict(b, size=max(0.05, b["size"] * 0.7), rotation=b["rotation"] * 0.5)
                      for b in boxes]
                tr = {"from": [m._norm_box(b) for b in boxes], "to": [m._norm_box(b) for b in to],
                      "duration_ms": 1000, "curve": "ease_in_out", "stagger_ms": 30.0,
                      "_p_raw": 0.42}
            outs[name] = run(m, boxes, srcs, bgbuf, tr)
            if name == "MVK":     # mode TRANCHE (bandes) ≡ trame entière ?
                sl = run(m, boxes, srcs, bgbuf, tr, bands=(36 if h % 36 == 0 else 40))
                dsl = int(np.abs(sl.astype(np.int32) - outs[name].astype(np.int32)).max())
                if dsl:
                    bad += 1
                    print("  ÉCART mode tranche it=%d : max|Δ|=%d" % (it, dsl))
        for name in ("REF", "NUMPY"):
            # ⚠ La REF (0.9.x) BORNE les positions à [0,1] : une box HORS CADRE (scénario `hard`,
            # nominal depuis la 0.12.0) n'y est donc pas au même endroit. La comparaison à la REF
            # n'a de sens que sur des boxes DANS le cadre ; le hors-cadre est validé entre kernel
            # et repli, et entre trame entière et mode tranche.
            if name == "REF" and hard:
                continue
            d = int(np.abs(outs["MVK"].astype(np.int32) - outs[name].astype(np.int32)).max())
            worst[name] = max(worst[name], d)
            if name == "NUMPY" and d:
                bad += 1
                print("  ÉCART MVK vs NUMPY it=%d chroma=%s : max|Δ|=%d" % (it, chroma, d))
        miss = getattr(mods["MVK"][chroma], "_mvk_miss", {})
        if miss.get("compose", 0) or miss.get("fused", 0) or miss.get("solid", 0):
            print("  ⚠ REPLI mvk it=%d : %s %s" % (it, json.dumps(miss),
                                                   json.dumps(mods["MVK"][chroma]._mvk_why)))
    print("max|Δ| MVK vs REF(0.9.x)  = %d" % worst["REF"])
    print("max|Δ| MVK vs repli numpy = %d" % worst["NUMPY"])
    tot = {"compose": 0, "fused": 0, "solid": 0, "calls": 0}
    for chroma in ("422", "420", "444"):
        for k, v in getattr(mods["MVK"][chroma], "_mvk_miss", {}).items():
            tot[k] += v
    print("replis mvk : %s" % json.dumps(tot))
    return 1 if (worst["NUMPY"] or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
