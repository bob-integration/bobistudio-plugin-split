# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

def sync_state(state, params, ctx):
    """Applique l'état live DVE (:8082/state) aux params persistés."""
    params = dict(params)
    params["box_config"] = state.get("boxes") or []
    inputs = state.get("inputs") or []
    params["bg_input"] = (inputs[4] if len(inputs) > 4 else "") or None
    return params
