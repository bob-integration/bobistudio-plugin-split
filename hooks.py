def sync_state(state, params, ctx):
    """Applique l'état live DVE (:8082/state) aux params persistés."""
    params = dict(params)
    params["box_config"] = state.get("boxes") or []
    inputs = state.get("inputs") or []
    params["bg_input"] = (inputs[4] if len(inputs) > 4 else "") or None
    return params
