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
    inputs = state.get("inputs") or []
    params["bg_input"] = (inputs[4] if len(inputs) > 4 else "") or None
    light = state.get("light")
    if isinstance(light, dict) and light:
        params["light"] = {k: light[k] for k in ("angle", "altitude") if k in light}
    trans = state.get("transition_defaults")
    if isinstance(trans, dict) and trans:
        params["transition"] = {k: trans[k] for k in ("curve", "stagger_ms") if k in trans}
    return params
