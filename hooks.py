# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

def sync_state(state, params, ctx):
    """Applique l'état live Split (:8082/state) aux params persistés.

    Persistés : la disposition (boxes), le câblage du fond, et les réglages GLOBAUX que le
    moteur relit au boot depuis CONFIG (source de lumière + défauts de transition) — sinon un
    redéploiement les perdrait en silence. La couche ART n'est PAS persistée : le moteur ne la
    relit pas depuis CONFIG (l'image vit dans le conteneur), l'UI le dit explicitement.
    """
    params = dict(params)
    params["box_config"] = state.get("boxes") or []
    # ★ Unité d'opacité : le moteur parle en POURCENT (0-100) depuis la 0.12.0. Le marqueur rend
    # la migration IDEMPOTENTE (un 1.0 persisté est ambigu : « opaque » en ancienne unité, « 1 % »
    # en nouvelle — seul un marqueur tranche, aucune heuristique ne le peut).
    params["opacity_unit"] = "pct"
    # Canal GLOBAL (calque de transformation du groupe) et FOND (mode + couleur) : persistés,
    # sinon un redéploiement perdrait la mise en scène en silence.
    g = state.get("global")
    if isinstance(g, dict) and g:
        params["global"] = {k: g[k] for k in ("dx", "dy", "scale", "rotation", "opacity") if k in g}
    bg = state.get("bg")
    if isinstance(bg, dict) and bg:
        # La couleur est persistée en HEX (sRGB) — JAMAIS en YUV : reconvertie à la profondeur et
        # à la colorimétrie du conteneur (un redéploiement en 10 bits doit rester juste).
        params["bg"] = {k: bg[k] for k in ("mode", "color", "legal") if k in bg}
    inputs = state.get("inputs") or []
    params["bg_input"] = (inputs[4] if len(inputs) > 4 else "") or None
    light = state.get("light")
    if isinstance(light, dict) and light:
        params["light"] = {k: light[k] for k in ("angle", "altitude") if k in light}
    trans = state.get("transition_defaults")
    if isinstance(trans, dict) and trans:
        params["transition"] = {k: trans[k] for k in ("curve", "stagger_ms") if k in trans}
    return params
