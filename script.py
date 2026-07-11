# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# ─────────────────────────────────────────────────────────────────────────────
# Split / SuperSource plugin : 4 PiP boxes composités sur 1 fond → 1 sortie.
#   5 entrées : slots 0..3 = boxes, slot 4 = fond. Câblage à chaud par slot.
#   Box : centre (x,y), taille (size, aspect préservé), crop (t/b/l/r).
#   Rappel mémoire = transition temporisée (tween géométrie + crossfade alpha).
#   Contrôle :8082, métriques :8080.
#   MODE TRANCHE (slice_mode/slice_lines, + cadence="flow") : composition bandée sur le bus
#   MXL slices — voir le bloc de config plus bas. slice_mode off → chemin historique intact.
#
# Template str.format : SEULS {config} / {hostname} / {plugin_version} sont des
# placeholders. TOUTE autre accolade littérale doit être doublée {{ }}.
# ─────────────────────────────────────────────────────────────────────────────
import time, threading, json, signal
from collections import deque
import numpy as np
import bobimxl   # migration MXL Phase 1 : 5 entrées Reader + 1 sortie Writer (planar, genlock fond)
from http.server import HTTPServer, BaseHTTPRequestHandler

inst = bobimxl.Instance()   # domaine MXL ($MXL_DOMAIN ou /dev/shm/mxl)

class RollingMs:
    def __init__(self, n=30):
        self.d = deque(maxlen=n); self.last_ns = 0
    def push(self, ms_value):
        self.d.append(ms_value); self.last_ns = time.time_ns()
    def avg(self):
        if not self.d: return None
        if time.time_ns() - self.last_ns > 2_000_000_000: return None
        return round(sum(self.d) / len(self.d), 1)

lat_in  = {{}}  # {{shm_name: RollingMs}} — TRANSIT par entrée (ts_read − ts_in producteur) = arrivée
own_lat = RollingMs()  # traitement PROPRE du nœud (ts_out − ts_cycle_start) → own_latency_ms

# ─── Config injectée (contrat plugin) ───────────────────────
CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

SHM_OUT  = CONFIG.get("shm_out") or (HOSTNAME + "_split")
WIDTH    = int(CONFIG.get("width") or 1280);  WIDTH  -= WIDTH % 2
HEIGHT   = int(CONFIG.get("height") or 720);  HEIGHT -= HEIGHT % 2
FPS      = int(round(float(CONFIG.get("fps") or 25))) or 25
# Genlock broadcast : cadence verrouillée sur l'entrée de RÉFÉRENCE = le FOND (slot 4) → 1 sortie
# par trame de fond (verrou 1:1). Les boxes (0-3) sont alignées par media-timestamp dans une fenêtre
# d'ALIGN_BUDGET trames ; hors budget → présentées en retard + flag. Skew exposé. off → free-run.
_gl = CONFIG.get("genlock", True)
GENLOCK = _gl if isinstance(_gl, bool) else str(_gl).strip().lower() in ("1", "true", "yes", "on")
ALIGN_BUDGET = max(0, int(CONFIG.get("align_budget", 1)))
N_BOXES  = 4
N_INPUTS = 5                       # slots 0..3 = boxes, 4 = fond
INITIAL_BOX_CONFIG = CONFIG.get("box_config") or []
# Entrées persistées par slot (state_field input_0..3 + bg_input) → re-câblage au boot
_INIT_SLOT_SHM = [CONFIG.get("input_0"), CONFIG.get("input_1"),
                  CONFIG.get("input_2"), CONFIG.get("input_3"), CONFIG.get("bg_input")]

# ─── yuv420p/422p/444p format (selon CONFIG[chroma], défaut 4:2:2) ──
CHROMA = str(CONFIG.get("chroma") or "422")
# Profondeur shm 8/10/12 bits. Le blend alpha est invariant à l'échelle (que des poids) :
# seuls le dtype (uint8/uint16), les constantes de couleur (×_SCALE) et les tailles changent.
BIT_DEPTH = int(CONFIG.get("bit_depth") or 8)
_DEEP  = BIT_DEPTH >= 10
_BPS   = 2 if _DEEP else 1
_NP_DT = np.uint16 if _DEEP else np.uint8
_SCALE = 1 << (BIT_DEPTH - 8)
_MAXV  = (1 << BIT_DEPTH) - 1
_NEUTRAL = 1 << (BIT_DEPTH - 1)
_BLACK_B = (16 << (BIT_DEPTH - 8)).to_bytes(_BPS, "little")
_NEUT_B  = _NEUTRAL.to_bytes(_BPS, "little")
_CW = {{"420": 2, "422": 2, "444": 1}}.get(CHROMA, 2)   # diviseur largeur chroma
_CH = {{"420": 2, "422": 1, "444": 1}}.get(CHROMA, 1)   # diviseur hauteur chroma
PIX_FMT = ({{"420": "yuv420p", "422": "yuv422p", "444": "yuv444p"}}.get(CHROMA, "yuv422p")
           + (("12le" if BIT_DEPTH >= 12 else "10le") if _DEEP else ""))
UV_W = WIDTH // _CW
UV_H = HEIGHT // _CH
V_HEADER_SIZE = 64
V_RING_SIZE   = CONFIG.get("shm_video_ring", 10)
Y_SIZE        = WIDTH * HEIGHT * _BPS
UV_SIZE       = UV_W * UV_H * _BPS
V_FRAME_SIZE  = Y_SIZE + 2 * UV_SIZE
V_TOTAL_SIZE  = V_HEADER_SIZE + V_RING_SIZE * V_FRAME_SIZE

# ── MODE TRANCHE (chantier latence sous-trame, cf. patch mxl-planar-slices) ─────────────────
# slice_mode=true → composition BANDE PAR BANDE : le fond ET chaque box suivent LEUR grain via
# get_slice (réveil au commit partiel du producteur — un 2110_io RX slice committe ~toutes les
# 0,7 ms), et la SORTIE est publiée progressivement (open_grain 1×, commit validSlices=1..N) →
# l'étage aval (TX 2110 slice, multiview…) démarre sur la 1ʳᵉ bande sans attendre la trame.
# Convention (identique moteur/multiview) : k tranches ⇔ lignes image [0, k·slice_height)
# valides sur les 3 plans Y|Cb|Cr. Source whole-frame (totalSlices=1) → dégénéré propre (attend
# son grain complet, zéro attente ensuite). Absent/False → chemin historique STRICTEMENT
# octet-identique. slice_lines pilote la granularité CIBLE (36 lignes ≈ 1080/30 → ~30
# tranches), adaptée à la hauteur réelle via _out_slice_h.
_slm = CONFIG.get("slice_mode", False)
SLICE_MODE  = _slm if isinstance(_slm, bool) else str(_slm).strip().lower() in ("1", "true", "yes", "on")
SLICE_LINES = max(1, int(CONFIG.get("slice_lines") or 36))
_SLICE_TARGET = max(1, 1080 // SLICE_LINES)

def _out_slice_h(oh, ch=1):
    """MODE TRANCHE — slice_height de la sortie (même algo que delay/pyramide _proxy_slice_h) :
    le plus petit diviseur sh de oh avec sh ≥ max(1, oh // _SLICE_TARGET), MULTIPLE de ch
    (lignes chroma entières par bande, sinon la dernière ligne d'une bande impaire en 420
    pointerait une ligne chroma non écrite). Aucun diviseur raisonnable (sh > oh//2, ex.
    hauteur première) → 0 = inéligible (whole-frame, chemin historique)."""
    lo = max(1, oh // _SLICE_TARGET)
    for sh in range(lo, oh // 2 + 1):
        if oh % sh == 0 and sh % ch == 0:
            return sh
    return 0

# Éligibilité : sortie PROGRESSIVE (scan=p — l'entrelacé reste whole-frame) + hauteur divisible.
# Les sources entrelacées (grain = champ, taille ≠ canvas) sont ignorées par le garde
# format-fixe existant → dégénéré propre côté entrées aussi.
_SCAN   = str(CONFIG.get("scan") or "p").strip().lower()
SLICE_H = _out_slice_h(HEIGHT, _CH) if (SLICE_MODE and not _SCAN.startswith("i")) else 0
SLICE_ON = SLICE_MODE and SLICE_H > 0
if SLICE_MODE and not SLICE_ON:
    print(f"split: slice_mode demandé mais inéligible (scan={{_SCAN}} h={{HEIGHT}}) — whole-frame")

# ── CADENCE « flow » (tissu en tranches, motif multiview) : pacing = GRILLE TAI — le
# verrou-entrée sur le fond disparaît ; à chaque epoch, chaque couche (fond + boxes) CIBLE
# l'index d'epoch fi_out de SA source et la sortie est écrite À CE MÊME INDEX (alignement
# inter-étages du tissu). Exige le mode tranche éligible ; sinon repli pacing historique.
CADENCE = str(CONFIG.get("cadence") or "").strip().lower()
FLOW = (CADENCE == "flow") and SLICE_ON
if CADENCE == "flow" and not FLOW:
    print("split: cadence=flow exige le mode tranche éligible — repli pacing historique")

def _grid_next(now_s, interval_s):
    import math as _m
    return (_m.floor(now_s / interval_s) + 1) * interval_s


def _hex_to_yuv(h):
    """#RRGGBB (ou #RGB) → (Y,U,V) BT.601 limité 0..255. Défaut blanc."""
    h = str(h or "#ffffff").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
    except Exception:
        r = g = b = 255
    y = int((0.299 * r + 0.587 * g + 0.114 * b) * _SCALE)
    u = int((-0.169 * r - 0.331 * g + 0.5 * b + 128) * _SCALE)
    v = int((0.5 * r - 0.419 * g - 0.081 * b + 128) * _SCALE)
    return max(0, min(_MAXV, y)), max(0, min(_MAXV, u)), max(0, min(_MAXV, v))


def _norm_box(b):
    """Normalize one box dict, filling defaults."""
    b = dict(b or {{}})
    crop = dict(b.get("crop") or {{}})
    bd = dict(b.get("border") or {{}})
    try:
        bw = max(0, min(40, int(float(bd.get("w", 0) or 0))))
    except Exception:
        bw = 0
    return {{
        "enabled": bool(b.get("enabled", False)),
        "x":    max(0.0, min(1.0, float(b.get("x", 0.5)))),
        "y":    max(0.0, min(1.0, float(b.get("y", 0.5)))),
        "size": max(0.05, min(1.0, float(b.get("size", 0.45)))),
        "crop": {{
            "t": max(0.0, min(0.9, float(crop.get("t", 0.0)))),
            "b": max(0.0, min(0.9, float(crop.get("b", 0.0)))),
            "l": max(0.0, min(0.9, float(crop.get("l", 0.0)))),
            "r": max(0.0, min(0.9, float(crop.get("r", 0.0)))),
        }},
        "border": {{"w": bw, "color": str(bd.get("color") or "#ffffff")}},
    }}

def _default_boxes():
    base = [
        {{"x": 0.28, "y": 0.30, "size": 0.42}},
        {{"x": 0.72, "y": 0.30, "size": 0.42}},
        {{"x": 0.28, "y": 0.70, "size": 0.42}},
        {{"x": 0.72, "y": 0.70, "size": 0.42}},
    ]
    return [_norm_box(b) for b in base]

# ─── Runtime state ──────────────────────────────────────────
state_lock = threading.Lock()
_init_boxes = [_norm_box(b) for b in (INITIAL_BOX_CONFIG or [])]
while len(_init_boxes) < N_BOXES:
    _init_boxes.append(_default_boxes()[len(_init_boxes)])
_init_inputs = [str(_INIT_SLOT_SHM[i]) if _INIT_SLOT_SHM[i] else "" for i in range(N_INPUTS)]
state = {{
    "inputs":     _init_inputs,
    "boxes":      _init_boxes[:N_BOXES],
    # transition : None or {{start, duration_ms, from:[boxes], to:[boxes]}}
    "transition": None,
}}

metrics_lock = threading.Lock()
metrics = {{"fps": 0.0, "frame_index": 0, "inputs_latency_ms": {{}}, "own_latency_ms": None, "inputs_skew_ms": {{}},
           "inputs_late": [], "plugin_version": PLUGIN_VERSION, "variant": "v2"}}

bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    print("SIGBUS reçu — réouverture des flux MXL")
    bus_error.set()
signal.signal(signal.SIGBUS, _handle_sigbus)


# ─── HTTP servers : metrics (8080) + control (8082) ─────────
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with metrics_lock: payload = dict(metrics)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
    def log_message(self, *a): pass


def _boxes_snapshot():
    """Effective boxes to report: target boxes while a transition runs, else committed."""
    tr = state["transition"]
    if tr is not None:
        return [dict(b) for b in tr["to"]]
    return [dict(b) for b in state["boxes"]]


class ControlHandler(BaseHTTPRequestHandler):
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n: return {{}}
        try: return json.loads(self.rfile.read(n).decode())
        except Exception: return {{}}

    def _reply(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if self.path == "/state":
            with state_lock:
                s = {{
                    "inputs":     list(state["inputs"]),
                    "boxes":      _boxes_snapshot(),
                    "n_boxes":    N_BOXES,
                    "n_inputs":   N_INPUTS,
                    "transition": bool(state["transition"]),
                    "shm_out":    SHM_OUT,
                    "width":      WIDTH,
                    "height":     HEIGHT,
                    "fps":        FPS,
                    "plugin_version": PLUGIN_VERSION,
                }}
                # state_field par slot (pour la topologie / câblage générique)
                for i in range(N_BOXES):
                    s["input_%d" % i] = state["inputs"][i]
                s["bg_input"] = state["inputs"][4]
            self._reply(200, s)
        else:
            self._reply(404, {{"error": "not found"}})

    def do_POST(self):
        body = self._read_json()
        if self.path == "/input":
            # Câblage générique : {{slot, shm}} (alias `idx` toléré). 0..3 = boxes, 4 = fond.
            raw = body.get("slot")
            if raw is None: raw = body.get("idx")
            try: idx = int(raw)
            except Exception:
                self._reply(400, {{"error": "slot requis"}}); return
            if not (0 <= idx < N_INPUTS):
                self._reply(400, {{"error": "slot hors plage"}}); return
            shm_name = (body.get("shm") or "").strip()
            with state_lock:
                state["inputs"][idx] = shm_name
                state["transition"] = None
            self._reply(200, {{"ok": True}})

        elif self.path == "/box":
            # Live PATCH of a box (cancels any running transition).
            try: idx = int(body.get("idx"))
            except Exception:
                self._reply(400, {{"error": "idx requis"}}); return
            if not (0 <= idx < N_BOXES):
                self._reply(400, {{"error": "idx hors plage"}}); return
            with state_lock:
                state["transition"] = None
                b = dict(state["boxes"][idx])
                if "x" in body:
                    try: b["x"] = max(0.0, min(1.0, float(body["x"])))
                    except Exception: pass
                if "y" in body:
                    try: b["y"] = max(0.0, min(1.0, float(body["y"])))
                    except Exception: pass
                if "size" in body:
                    try: b["size"] = max(0.05, min(1.0, float(body["size"])))
                    except Exception: pass
                if "enabled" in body:
                    b["enabled"] = bool(body["enabled"])
                if "crop" in body and isinstance(body["crop"], dict):
                    crop = dict(b["crop"])
                    for k in ("t", "b", "l", "r"):
                        if k in body["crop"]:
                            try: crop[k] = max(0.0, min(0.9, float(body["crop"][k])))
                            except Exception: pass
                    b["crop"] = crop
                if "border" in body and isinstance(body["border"], dict):
                    bd = dict(b.get("border") or {{}})
                    if "w" in body["border"]:
                        try: bd["w"] = max(0, min(40, int(float(body["border"]["w"]))))
                        except Exception: pass
                    if "color" in body["border"]:
                        bd["color"] = str(body["border"]["color"] or "#ffffff")
                    b["border"] = bd
                state["boxes"][idx] = _norm_box(b)
            self._reply(200, {{"ok": True}})

        elif self.path == "/recall":
            # Recall a memory : tween current boxes -> target boxes over duration_ms.
            targets = body.get("boxes")
            if not isinstance(targets, list) or not targets:
                self._reply(400, {{"error": "boxes requis"}}); return
            tgt = [_norm_box(b) for b in targets[:N_BOXES]]
            while len(tgt) < N_BOXES:
                tgt.append(dict(state["boxes"][len(tgt)]))
            duration = body.get("duration_ms")
            if duration is None: duration = 0
            try: duration = max(0, int(duration))
            except Exception:
                self._reply(400, {{"error": "duration_ms invalide"}}); return
            with state_lock:
                if duration <= 0:
                    state["boxes"] = tgt
                    state["transition"] = None
                else:
                    state["transition"] = {{
                        "start": time.time(),
                        "duration_ms": duration,
                        "from": [dict(b) for b in state["boxes"]],
                        "to":   tgt,
                    }}
            self._reply(200, {{"ok": True}})

        else:
            self._reply(404, {{"error": "not found"}})

    def log_message(self, *a): pass


threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever(),
    daemon=True).start()
threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", 8082), ControlHandler).serve_forever(),
    daemon=True).start()


# ─── Dynamic input wiring (hot re-wire per slot) ────────────
input_handles = [None] * N_INPUTS

def ensure_input(idx):
    """Renvoie le bobimxl.Reader pour l'input idx, en (ré-)ouvrant si le nom de flux a changé.
    Renvoie None si le slot n'est pas câblé ou le flux est indisponible."""
    with state_lock:
        wanted = state["inputs"][idx]
    cur = input_handles[idx]   # (reader, name) ou None
    if not wanted:
        if cur is not None:
            try: cur[0].close()
            except Exception: pass
            input_handles[idx] = None
        return None
    if cur is not None and cur[1] == wanted:
        return cur[0]
    if cur is not None:
        try: cur[0].close()
        except Exception: pass
        input_handles[idx] = None
    try:
        r = bobimxl.Reader(inst, wanted)   # lève si le flux n'existe pas encore
        input_handles[idx] = (r, wanted)
        print(f"input #{{idx}} câblé sur flux MXL {{wanted}}")
        return r
    except Exception:
        return None


def _drop_slot(idx):
    """Ferme le Reader du slot et oublie le handle (→ ensure_input ROUVRE au tour suivant sur
    la génération courante du flux). GC OBLIGATOIRE entre close et réouverture (piège des
    générations, motif multiview/delay) : sans GC, le flux périmé reste résolvable PAR NOM et
    la réouverture retombe sur L'ORPHELIN qu'on vient de lâcher."""
    h = input_handles[idx]
    if h is not None:
        try: h[0].close()
        except Exception: pass
        input_handles[idx] = None
    try: inst.garbage_collect()
    except Exception: pass


def _in_header(idx):
    """(frame_index, write_ts_ns) du producteur de l'entrée `idx`, ou (0, 0) si indispo.
    Sert au verrou de cadence (réf = fond) et à l'alignement par media-timestamp (genlock).
    En MXL : index = headIndex courant, ts = dernière écriture (TAI)."""
    r = ensure_input(idx)
    if r is None: return 0, 0
    try:
        h = r.head_index()
        if h == bobimxl.MXL_UNDEFINED_INDEX: return 0, 0
        return h, r.last_write_time()
    except Exception: return 0, 0


def lire_frame_yuv(r):
    """Lit la frame la plus récente du Reader. Renvoie (bytes, ts_in_ns TAI) ou (None, None).
    Garde format-fixe : un grain de taille ≠ canvas est ignoré (le composite suppose WxH/chroma/depth)."""
    if r is None: return None, None
    try:
        got = r.get_latest()
        if got is None: return None, None
        if len(got[2]) != V_FRAME_SIZE: return None, None   # format ≠ canvas → ignoré
        return bytes(got[2]), r.last_write_time()
    except Exception:
        return None, None


# ─── Compositing : crop-aware PiP box over background ────────
def composite_box_yuv(bgd_yuv, src_yuv, alpha, cx, cy, size, crop, border=None):
    """Composite a PiP box (cropped source) onto bgd_yuv in YUV420.
    cx,cy = box center (normalized); size = scale (aspect preserved); crop = {{t,b,l,r}}
    fractions removed from the source before placing. alpha in [0,1]."""
    if alpha <= 0.001 or src_yuv is None:
        return bgd_yuv
    cl = max(0.0, min(0.9, crop.get("l", 0.0))); cr = max(0.0, min(0.9, crop.get("r", 0.0)))
    ct = max(0.0, min(0.9, crop.get("t", 0.0))); cb = max(0.0, min(0.9, crop.get("b", 0.0)))
    if cl + cr >= 0.95: cl = cr = 0.0
    if ct + cb >= 0.95: ct = cb = 0.0

    # Full box rect (preserves output aspect), then visible (cropped) rect.
    full_w = WIDTH * size; full_h = HEIGHT * size
    dst_w = max(2, int(full_w * (1.0 - cl - cr)))
    dst_h = max(2, int(full_h * (1.0 - ct - cb)))
    if dst_w % 2: dst_w -= 1
    if dst_h % 2: dst_h -= 1
    if dst_w < 2 or dst_h < 2:
        return bgd_yuv

    # Visible rect center : full rect centered at (cx,cy), shifted by asymmetric crop.
    vis_cx = cx * WIDTH + (cl - cr) * 0.5 * full_w
    vis_cy = cy * HEIGHT + (ct - cb) * 0.5 * full_h
    x0 = int(vis_cx - dst_w / 2); y0 = int(vis_cy - dst_h / 2)
    if x0 % 2: x0 -= 1
    if y0 % 2: y0 -= 1

    # Source region (in source pixels) after crop.
    sxr0 = cl * WIDTH;  sxr1 = (1.0 - cr) * WIDTH
    syr0 = ct * HEIGHT; syr1 = (1.0 - cb) * HEIGHT

    # Clip to output bounds.
    out_x0 = max(0, x0); out_y0 = max(0, y0)
    out_x1 = min(WIDTH,  x0 + dst_w); out_y1 = min(HEIGHT, y0 + dst_h)
    if out_x1 <= out_x0 or out_y1 <= out_y0:
        return bgd_yuv
    src_x_off = out_x0 - x0; src_y_off = out_y0 - y0
    vis_w = out_x1 - out_x0; vis_h = out_y1 - out_y0

    src_y = np.frombuffer(src_yuv[:Y_SIZE], dtype=_NP_DT).reshape(HEIGHT, WIDTH)
    src_u = np.frombuffer(src_yuv[Y_SIZE:Y_SIZE+UV_SIZE], dtype=_NP_DT).reshape(UV_H, UV_W)
    src_v = np.frombuffer(src_yuv[Y_SIZE+UV_SIZE:], dtype=_NP_DT).reshape(UV_H, UV_W)

    # Nearest-neighbor sample maps from the cropped source region to the full dst box.
    ix = (sxr0 + (np.arange(dst_w) + 0.5) / dst_w * (sxr1 - sxr0)).astype(np.int32)
    iy = (syr0 + (np.arange(dst_h) + 0.5) / dst_h * (syr1 - syr0)).astype(np.int32)
    ix = np.clip(ix, 0, WIDTH - 1); iy = np.clip(iy, 0, HEIGHT - 1)
    k_y_full = src_y[iy[:, None], ix[None, :]]
    ix2 = (sxr0 / _CW + (np.arange(dst_w // _CW) + 0.5) / (dst_w // _CW) * ((sxr1 - sxr0) / _CW)).astype(np.int32)
    iy2 = (syr0 / _CH + (np.arange(dst_h // _CH) + 0.5) / (dst_h // _CH) * ((syr1 - syr0) / _CH)).astype(np.int32)
    ix2 = np.clip(ix2, 0, UV_W - 1); iy2 = np.clip(iy2, 0, UV_H - 1)
    k_u_full = src_u[iy2[:, None], ix2[None, :]]
    k_v_full = src_v[iy2[:, None], ix2[None, :]]

    k_y = k_y_full[src_y_off:src_y_off+vis_h, src_x_off:src_x_off+vis_w]
    k_u = k_u_full[src_y_off//_CH:(src_y_off+vis_h)//_CH, src_x_off//_CW:(src_x_off+vis_w)//_CW]
    k_v = k_v_full[src_y_off//_CH:(src_y_off+vis_h)//_CH, src_x_off//_CW:(src_x_off+vis_w)//_CW]

    bgd_y = np.frombuffer(bgd_yuv[:Y_SIZE], dtype=_NP_DT).reshape(HEIGHT, WIDTH).copy()
    bgd_u = np.frombuffer(bgd_yuv[Y_SIZE:Y_SIZE+UV_SIZE], dtype=_NP_DT).reshape(UV_H, UV_W).copy()
    bgd_v = np.frombuffer(bgd_yuv[Y_SIZE+UV_SIZE:], dtype=_NP_DT).reshape(UV_H, UV_W).copy()

    reg_y = bgd_y[out_y0:out_y1, out_x0:out_x1].astype(np.float32)
    bgd_y[out_y0:out_y1, out_x0:out_x1] = (reg_y * (1.0 - alpha) +
                                           k_y.astype(np.float32) * alpha).astype(_NP_DT)
    reg_u = bgd_u[out_y0//_CH:out_y1//_CH, out_x0//_CW:out_x1//_CW].astype(np.float32)
    bgd_u[out_y0//_CH:out_y1//_CH, out_x0//_CW:out_x1//_CW] = (reg_u * (1.0 - alpha) +
                                           k_u.astype(np.float32) * alpha).astype(_NP_DT)
    reg_v = bgd_v[out_y0//_CH:out_y1//_CH, out_x0//_CW:out_x1//_CW].astype(np.float32)
    bgd_v[out_y0//_CH:out_y1//_CH, out_x0//_CW:out_x1//_CW] = (reg_v * (1.0 - alpha) +
                                           k_v.astype(np.float32) * alpha).astype(_NP_DT)

    # Bordure pleine (opaque) le long du rect visible de la box, vers l'intérieur.
    bw = 0
    if border:
        try: bw = int(border.get("w") or 0)
        except Exception: bw = 0
    if bw > 0:
        Yb, Ub, Vb = _hex_to_yuv(border.get("color"))
        bw = min(bw, max(0, (out_y1 - out_y0) // 2), max(0, (out_x1 - out_x0) // 2))
        if bw > 0:
            bgd_y[out_y0:out_y0+bw, out_x0:out_x1] = Yb
            bgd_y[out_y1-bw:out_y1, out_x0:out_x1] = Yb
            bgd_y[out_y0:out_y1, out_x0:out_x0+bw] = Yb
            bgd_y[out_y0:out_y1, out_x1-bw:out_x1] = Yb
            cbw_y = max(1, bw // _CH); cbw_x = max(1, bw // _CW)
            uy0, uy1, ux0, ux1 = out_y0//_CH, out_y1//_CH, out_x0//_CW, out_x1//_CW
            for plane, val in ((bgd_u, Ub), (bgd_v, Vb)):
                plane[uy0:uy0+cbw_y, ux0:ux1] = val
                plane[uy1-cbw_y:uy1, ux0:ux1] = val
                plane[uy0:uy1, ux0:ux0+cbw_x] = val
                plane[uy0:uy1, ux1-cbw_x:ux1] = val
    return bgd_y.tobytes() + bgd_u.tobytes() + bgd_v.tobytes()


# ─── Transition interpolation ───────────────────────────────
def _lerp(a, b, p): return a + (b - a) * p

def _interp_box(fr, to, p):
    """Geometry interpolation + alpha from enable crossfade. Returns (box, alpha) or None."""
    fe = fr["enabled"]; te = to["enabled"]
    if not fe and not te:
        return None
    if fe and te:       alpha = 1.0
    elif fe and not te: alpha = 1.0 - p   # fade out
    else:               alpha = p         # fade in
    box = {{
        "x":    _lerp(fr["x"], to["x"], p),
        "y":    _lerp(fr["y"], to["y"], p),
        "size": _lerp(fr["size"], to["size"], p),
        "crop": {{k: _lerp(fr["crop"][k], to["crop"][k], p) for k in ("t", "b", "l", "r")}},
        "border": dict(to.get("border") or {{}}),   # bordure : on suit la cible (pas d'interpolation)
    }}
    return box, alpha


# ─── Output shm ─────────────────────────────────────────────
def _ouvrir_sortie(name):
    """Writer MXL de sortie (planar). Index explicite (frame_index, mode free) ; en cadence
    « flow », index = grille TAI (_next_index). MODE TRANCHE : le flowDef porte slice_height →
    libmxl publie le grain en N tranches égales (commit progressif). Writer forwarde **flow_kw
    à build_flow_def. Sans slice : inchangé (1 tranche, historique octet-identique)."""
    return bobimxl.Writer(inst, name, WIDTH, HEIGHT, CHROMA, BIT_DEPTH, FPS, 1,
                          index_mode=("tai" if FLOW else "free"),
                          **({{"slice_height": SLICE_H}} if SLICE_ON else {{}}))

def _ecrire_sortie(w, yuv, frame_index):
    try:
        _i, gi, vw = w.open_grain(index=frame_index)
        vw[:len(yuv)] = np.frombuffer(yuv, dtype=np.uint8)
        w.commit(gi)
    except Exception:
        pass


# ─── MODE TRANCHE : composition bandée (fond + boxes suivis par tranches) ───────────────────
# Backoff des entrées mortes : slot → [fi_bloqué, timeouts_consécutifs]. Persistant ENTRE les
# trames ; réarmé dès que la tête du slot dépasse fi_bloqué (motif multiview _sl_backoff).
_sl_backoff = {{}}
_sl_lasthead = {{}}   # slot → dernière tête vue (détection génération périmée, cadence flow)
_sl_dbg = None        # [couches, valid0, attentes, replis, dormantes] de la dernière trame


def _plane_views(view):
    """Vues numpy 2D (Y, U, V) ZÉRO-COPIE sur un grain de taille canvas (WxH/chroma/depth)."""
    sy = view[:Y_SIZE].view(_NP_DT).reshape(HEIGHT, WIDTH)
    su = view[Y_SIZE:Y_SIZE + UV_SIZE].view(_NP_DT).reshape(UV_H, UV_W)
    sv = view[Y_SIZE + UV_SIZE:Y_SIZE + 2 * UV_SIZE].view(_NP_DT).reshape(UV_H, UV_W)
    return sy, su, sv


def _arith(idx):
    """(offset, pas) si `idx` est une progression arithmétique à pas ENTIER ≥ 1 → le placement
    peut utiliser une vue STRIDÉE zéro-copie (ratio entier, motif resize_plane du multiview) ;
    sinon None → gather chaîné lignes→colonnes. Le clip aux bords casse la progression → le
    test échoue proprement et on retombe sur le gather (valeurs toujours identiques)."""
    n = int(idx.size)
    if n == 0:
        return None
    if n == 1:
        return (int(idx[0]), 1)
    st = int(idx[1]) - int(idx[0])
    if st >= 1 and bool((idx == (int(idx[0]) + np.arange(n, dtype=np.int64) * st)).all()):
        return (int(idx[0]), st)
    return None


def _box_slice_geom(b, alpha):
    """MODE TRANCHE — géométrie pré-calculée d'une box pour le placement PAR BANDE. Reprend À
    L'IDENTIQUE les calculs (arrondis compris) de composite_box_yuv → résultat octet-identique
    au chemin historique. Les mappings nearest ligne/colonne sont DÉJÀ clippés au rect visible.
    Renvoie None si la box est invisible (alpha≈0, trop petite ou hors cadre)."""
    if alpha <= 0.001:
        return None
    crop = b.get("crop") or {{}}
    cl = max(0.0, min(0.9, crop.get("l", 0.0))); cr = max(0.0, min(0.9, crop.get("r", 0.0)))
    ct = max(0.0, min(0.9, crop.get("t", 0.0))); cb = max(0.0, min(0.9, crop.get("b", 0.0)))
    if cl + cr >= 0.95: cl = cr = 0.0
    if ct + cb >= 0.95: ct = cb = 0.0
    full_w = WIDTH * b["size"]; full_h = HEIGHT * b["size"]
    dst_w = max(2, int(full_w * (1.0 - cl - cr)))
    dst_h = max(2, int(full_h * (1.0 - ct - cb)))
    if dst_w % 2: dst_w -= 1
    if dst_h % 2: dst_h -= 1
    if dst_w < 2 or dst_h < 2:
        return None
    vis_cx = b["x"] * WIDTH + (cl - cr) * 0.5 * full_w
    vis_cy = b["y"] * HEIGHT + (ct - cb) * 0.5 * full_h
    x0 = int(vis_cx - dst_w / 2); y0 = int(vis_cy - dst_h / 2)
    if x0 % 2: x0 -= 1
    if y0 % 2: y0 -= 1
    sxr0 = cl * WIDTH;  sxr1 = (1.0 - cr) * WIDTH
    syr0 = ct * HEIGHT; syr1 = (1.0 - cb) * HEIGHT
    out_x0 = max(0, x0); out_y0 = max(0, y0)
    out_x1 = min(WIDTH,  x0 + dst_w); out_y1 = min(HEIGHT, y0 + dst_h)
    if out_x1 <= out_x0 or out_y1 <= out_y0:
        return None
    src_x_off = out_x0 - x0; src_y_off = out_y0 - y0
    vis_w = out_x1 - out_x0; vis_h = out_y1 - out_y0
    ix = (sxr0 + (np.arange(dst_w) + 0.5) / dst_w * (sxr1 - sxr0)).astype(np.int32)
    iy = (syr0 + (np.arange(dst_h) + 0.5) / dst_h * (syr1 - syr0)).astype(np.int32)
    ix = np.clip(ix, 0, WIDTH - 1); iy = np.clip(iy, 0, HEIGHT - 1)
    ix2 = (sxr0 / _CW + (np.arange(dst_w // _CW) + 0.5) / (dst_w // _CW) * ((sxr1 - sxr0) / _CW)).astype(np.int32)
    iy2 = (syr0 / _CH + (np.arange(dst_h // _CH) + 0.5) / (dst_h // _CH) * ((syr1 - syr0) / _CH)).astype(np.int32)
    ix2 = np.clip(ix2, 0, UV_W - 1); iy2 = np.clip(iy2, 0, UV_H - 1)
    iy_v  = iy[src_y_off:src_y_off + vis_h]
    ix_v  = ix[src_x_off:src_x_off + vis_w]
    iy2_v = iy2[src_y_off // _CH:(src_y_off + vis_h) // _CH]
    ix2_v = ix2[src_x_off // _CW:(src_x_off + vis_w) // _CW]
    bd = b.get("border") or {{}}
    try: bw = int(bd.get("w") or 0)
    except Exception: bw = 0
    if bw > 0:   # même clamp que composite_box_yuv (vers l'intérieur du rect visible)
        bw = min(bw, max(0, (out_y1 - out_y0) // 2), max(0, (out_x1 - out_x0) // 2))
    return {{"x0": out_x0, "y0": out_y0, "x1": out_x1, "y1": out_y1, "alpha": float(alpha),
             "iy": iy_v, "ix": ix_v, "iy2": iy2_v, "ix2": ix2_v,
             "ay": _arith(iy_v), "ax": _arith(ix_v), "ay2": _arith(iy2_v), "ax2": _arith(ix2_v),
             "bw": bw, "byuv": _hex_to_yuv(bd.get("color")) if bw > 0 else None,
             "cbw_y": max(1, bw // _CH) if bw > 0 else 0,
             "cbw_x": max(1, bw // _CW) if bw > 0 else 0}}


def _band_need_row(g, b0, b1):
    """Dernière ligne SOURCE (plan Y, chroma ramenée en lignes image) requise par la bande de
    sortie [b0,b1) pour la box g, ou -1 si la box n'intersecte pas la bande. Convention : k
    tranches valides ⇔ lignes image [0, k·islh) valides sur les 3 plans → couvrir need_row
    couvre aussi la chroma correspondante."""
    a = max(g["y0"], b0); b = min(g["y1"], b1)
    if a >= b:
        return -1
    lb = b - g["y0"]
    need = int(g["iy"][lb - 1])              # iy monotone → dernière ligne = max
    ca = a // _CH; cbnd = b // _CH
    if cbnd > ca:
        lcb = cbnd - g["y0"] // _CH
        need = max(need, (int(g["iy2"][lcb - 1]) + 1) * _CH - 1)
    return need


def _place_box_band(oy, ou, ov, sy, su, sv, g, b0, b1):
    """Blende les lignes [b0,b1) ∩ box sur la sortie — MÊMES valeurs que composite_box_yuv
    (mapping nearest identique, blend float32 identique), restreint à la bande. Vue STRIDÉE
    zéro-copie si le mapping est à pas constant (ratio entier), sinon gather CHAÎNÉ
    lignes→colonnes (JAMAIS np.ix_ 2D, prohibitif à bande×box — mesuré multiview)."""
    a = max(g["y0"], b0); b = min(g["y1"], b1)
    if a >= b:
        return
    la = a - g["y0"]; lb = b - g["y0"]
    x0, x1 = g["x0"], g["x1"]
    alpha = g["alpha"]
    if g["ay"] is not None and g["ax"] is not None:
        o_r, s_r = g["ay"]; o_c, s_c = g["ax"]
        ky = sy[o_r + s_r * la : o_r + s_r * (lb - 1) + 1 : s_r,
                o_c : o_c + s_c * (g["ix"].size - 1) + 1 : s_c]
    else:
        ky = sy[g["iy"][la:lb]][:, g["ix"]]
    if alpha >= 1.0:
        # alpha=1 : reg·0.0 + k·1.0 en float32 = k exactement (valeurs ≤ 16 bits) → assignation
        # directe octet-identique au blend historique, sans le round-trip float32.
        oy[a:b, x0:x1] = ky
    else:
        reg = oy[a:b, x0:x1].astype(np.float32)
        oy[a:b, x0:x1] = (reg * (1.0 - alpha) + ky.astype(np.float32) * alpha).astype(_NP_DT)
    ca = a // _CH; cbnd = b // _CH
    if cbnd <= ca:
        return
    lca = ca - g["y0"] // _CH; lcb = lca + (cbnd - ca)
    cx0 = x0 // _CW; cx1 = x1 // _CW
    for splane, oplane in ((su, ou), (sv, ov)):
        if g["ay2"] is not None and g["ax2"] is not None:
            o_r, s_r = g["ay2"]; o_c, s_c = g["ax2"]
            kc = splane[o_r + s_r * lca : o_r + s_r * (lcb - 1) + 1 : s_r,
                        o_c : o_c + s_c * (g["ix2"].size - 1) + 1 : s_c]
        else:
            kc = splane[g["iy2"][lca:lcb]][:, g["ix2"]]
        if alpha >= 1.0:
            oplane[ca:cbnd, cx0:cx1] = kc
        else:
            reg = oplane[ca:cbnd, cx0:cx1].astype(np.float32)
            oplane[ca:cbnd, cx0:cx1] = (reg * (1.0 - alpha) + kc.astype(np.float32) * alpha).astype(_NP_DT)


def _paint_border_band(oy, ou, ov, g, b0, b1):
    """Bordure pleine (opaque) de la box g, restreinte aux lignes [b0,b1) — mêmes rects que
    composite_box_yuv (haut/bas/côtés vers l'intérieur, chroma cbw), clippés à la bande."""
    bw = g["bw"]
    if bw <= 0:
        return
    Yb, Ub, Vb = g["byuv"]
    x0, y0, x1, y1 = g["x0"], g["y0"], g["x1"], g["y1"]
    for e0, e1 in ((y0, y0 + bw), (y1 - bw, y1)):       # bords haut / bas
        a = max(e0, b0); b = min(e1, b1)
        if a < b:
            oy[a:b, x0:x1] = Yb
    a = max(y0, b0); b = min(y1, b1)                     # côtés gauche / droit
    if a < b:
        oy[a:b, x0:x0 + bw] = Yb
        oy[a:b, x1 - bw:x1] = Yb
    cbw_y = g["cbw_y"]; cbw_x = g["cbw_x"]
    uy0, uy1, ux0, ux1 = y0 // _CH, y1 // _CH, x0 // _CW, x1 // _CW
    cb0_, cb1_ = b0 // _CH, b1 // _CH
    for plane, val in ((ou, Ub), (ov, Vb)):
        for e0, e1 in ((uy0, uy0 + cbw_y), (uy1 - cbw_y, uy1)):
            a = max(e0, cb0_); b = min(e1, cb1_)
            if a < b:
                plane[a:b, ux0:ux1] = val
        a = max(uy0, cb0_); b = min(uy1, cb1_)
        if a < b:
            plane[a:b, ux0:ux0 + cbw_x] = val
            plane[a:b, ux1 - cbw_x:ux1] = val


def _sl_ensure(L, need_k, deadl):
    """Attend (get_slice, réveil au commit partiel) que la couche L couvre need_k tranches —
    budget PAR COUCHE (une couche en retard n'entame que SON budget, borne dure globale =
    deadl). Au timeout : REPLI sur le dernier grain COMPLET (léger tearing d'UNE image sur
    cette couche, jamais de blocage) + comptage backoff (2 timeouts consécutifs sur le MÊME
    grain → dormante aux trames suivantes). Renvoie les ns ATTENDUS (exclus de own_latency_ms
    — le monitoring y lit la saturation du worker, pas le suivi du fil)."""
    if _sl_dbg[1] < 0:
        _sl_dbg[1] = L["valid"]
    if need_k <= L["valid"]:
        return 0
    _sl_dbg[2] += 1
    left = min(deadl - time.monotonic(), L["budget"] / 1e9)
    w0 = time.monotonic()
    g = L["rd"].get_slice(L["fi"], need_k, timeout_ns=max(1, int(left * 1e9))) if left > 0 else None
    dw = int((time.monotonic() - w0) * 1e9)
    L["budget"] -= dw
    if g is not None:
        L["valid"] = max(need_k, int(g[1].validSlices or need_k))
    else:
        bk = _sl_backoff.get(L["slot"])
        _sl_backoff[L["slot"]] = [L["fi"], (bk[1] + 1 if bk and bk[0] == L["fi"] else 1)]
        gp = L["rd"].get(L["fi"] - 1, timeout_ns=2_000_000)
        if gp is not None and len(gp[2]) == V_FRAME_SIZE:
            L["sy"], L["su"], L["sv"] = _plane_views(gp[2])
        _sl_dbg[3] += 1
        L["valid"] = L["total"]        # plus aucune attente pour les bandes suivantes
    return dw


def _slice_frame(out_index, fi_out, tr, boxes, ts_in_per_slot):
    """MODE TRANCHE — compose et PUBLIE la trame BANDE PAR BANDE (SLICE_H lignes/bande) :
    fond + boxes suivis CHACUN sur SON grain via get_slice, sortie committée progressivement
    (validSlices=k+1 → l'aval démarre sur la 1ʳᵉ bande). Budget par couche + backoff des
    entrées mortes + repli grain complet : la sortie n'est JAMAIS bloquée par une source.
    Commit final TOUJOURS (finally) : un grain laissé partiel ne serait jamais lisible.
    Renvoie le cumul des attentes get_slice (à EXCLURE de own_latency_ms)."""
    global _sl_dbg
    wait_ns = 0
    p = None
    if tr is not None:
        p = max(0.0, min(1.0, (time.time() - tr["start"]) * 1000.0 / tr["duration_ms"]))
    # 1) plan des couches à composer (z-ordre : fond puis boxes 0→3), géométrie comprise.
    plan = [(4, None, 1.0)]
    for i in range(N_BOXES):
        if tr is not None:
            res = _interp_box(tr["from"][i], tr["to"][i], p)
            if res is None:
                continue
            b_, alpha = res
        else:
            if not boxes[i]["enabled"]:
                continue
            b_, alpha = boxes[i], 1.0
        plan.append((i, b_, alpha))
    # 2) cible + 1ʳᵉ tranche de chaque couche (le suivi des bandes suivantes se fait au fil
    #    du compose). Cible = tête (peut être EN COURS d'écriture) ; en flow = index d'epoch.
    layers = []
    dormant = 0
    for slot, b_, alpha in plan:
        g = None
        if slot != 4:
            g = _box_slice_geom(b_, alpha)
            if g is None:
                continue
        rd = ensure_input(slot)
        if rd is None:
            continue     # fond absent → bandes noires ; box absente → non composée (historique)
        try:
            hi = rd.head_index()
            got = None
            if FLOW:
                # GÉNÉRATION PÉRIMÉE (motif multiview flow) : tête FIGÉE très en retard sur la
                # grille = Reader sur l'ancien ring d'un producteur recréé sous le même nom →
                # drop (GC entre close et reopen) ; rouvert à la trame suivante.
                if (hi != bobimxl.MXL_UNDEFINED_INDEX and fi_out - hi > 250
                        and hi == _sl_lasthead.get(slot)):
                    _drop_slot(slot)
                    _sl_lasthead.pop(slot, None)
                    continue
                _sl_lasthead[slot] = hi
                # Cibler L'INDEX D'EPOCH fi_out (alignement du tissu) si la source est sur la
                # même grille (|tête−fi_out| ≤ 2) ou pas encore ouverte ; source HORS-GRILLE
                # (index libre) → suivre sa tête comme avant.
                tgt = (fi_out if (hi == bobimxl.MXL_UNDEFINED_INDEX or abs(hi - fi_out) <= 2)
                       else hi)
                got = rd.get_slice(tgt, 1, timeout_ns=3_000_000)
                if got is None and tgt != hi and hi != bobimxl.MXL_UNDEFINED_INDEX:
                    got = rd.get_slice(hi, 1, timeout_ns=1_000_000)   # retard → grain courant
            elif hi != bobimxl.MXL_UNDEFINED_INDEX:
                got = rd.get_slice(hi, 1, timeout_ns=2_000_000)
            if got is None:
                got = rd.get_latest()      # flux whole-frame / tête pas prête → grain complet
            if got is None or len(got[2]) != V_FRAME_SIZE:
                continue   # garde format-fixe : grain ≠ canvas (champ entrelacé compris) → ignoré
            ts = rd.last_write_time()
            if ts:
                ts_in_per_slot[slot] = ts
            fi, gi, view = got
            sy, su, sv = _plane_views(view)   # vues ZÉRO-COPIE (jamais lues au-delà du valide)
            total = max(1, int(gi.totalSlices or 1))
            valid = int(gi.validSlices or total)
            bk = _sl_backoff.get(slot)
            if bk is not None and fi > bk[0]:       # la tête a avancé → réarme la couche
                _sl_backoff.pop(slot, None); bk = None
            if bk is not None and bk[1] >= 2:
                # DORMANTE : 2 timeouts consécutifs sur le MÊME grain (producteur mort
                # mi-grain) → bascule immédiate sur le dernier grain complet, ZÉRO sonde tant
                # que la tête n'avance pas.
                dormant += 1
                gp = rd.get(fi - 1, timeout_ns=2_000_000)
                if gp is not None and len(gp[2]) == V_FRAME_SIZE:
                    sy, su, sv = _plane_views(gp[2])
                valid = total
            layers.append({{"slot": slot, "rd": rd, "fi": fi, "sy": sy, "su": su, "sv": sv,
                            "total": total, "valid": valid,
                            "islh": max(1, HEIGHT // total), "g": g,
                            "budget": int(interval * 1e9)}})
        except Exception:
            continue
    _sl_dbg = [len(layers), -1, 0, 0, dormant]
    bg = next((L for L in layers if L["slot"] == 4), None)
    # 3) grain de sortie + publication PROGRESSIVE bande par bande.
    _gidx, gi_o, vw_o = out.open_grain(index=out_index)
    oy, ou, ovv = _plane_views(vw_o)
    nb = HEIGHT // SLICE_H
    deadl = time.monotonic() + (interval * 1.5)   # garde-fou GLOBAL (borne dure de la trame)
    black_y = 16 * _SCALE
    try:
        for k in range(nb):
            b0 = k * SLICE_H; b1 = b0 + SLICE_H
            cb0 = b0 // _CH; cb1 = b1 // _CH
            if bg is not None:
                wait_ns += _sl_ensure(bg, min(bg["total"], (b1 - 1) // bg["islh"] + 1), deadl)
                oy[b0:b1] = bg["sy"][b0:b1]
                ou[cb0:cb1] = bg["su"][cb0:cb1]
                ovv[cb0:cb1] = bg["sv"][cb0:cb1]
            else:
                # fond couleur (noir) : remplissage par bande trivial (≡ empty_frame)
                oy[b0:b1] = black_y
                ou[cb0:cb1] = _NEUTRAL
                ovv[cb0:cb1] = _NEUTRAL
            for L in layers:
                g = L["g"]
                if g is None:
                    continue
                need = _band_need_row(g, b0, b1)
                if need < 0:
                    continue
                wait_ns += _sl_ensure(L, min(L["total"], need // L["islh"] + 1), deadl)
                _place_box_band(oy, ou, ovv, L["sy"], L["su"], L["sv"], g, b0, b1)
                _paint_border_band(oy, ou, ovv, g, b0, b1)
            if k < nb - 1:
                out.commit(gi_o, valid_slices=k + 1)   # réveille l'aval sur la bande
    finally:
        out.commit(gi_o, valid_slices=None)   # commit final TOUJOURS (grain jamais partiel)
    return wait_ns


out = _ouvrir_sortie(SHM_OUT)
if SLICE_ON:
    metrics["slice_mode"] = True
    print(f"split: MODE TRANCHE actif — {{SLICE_H}} lignes/bande ({{HEIGHT // SLICE_H}} tranches)"
          + (" [cadence=flow]" if FLOW else ""))

frame_index = 0
start = time.time()
_fps_last_idx = 0          # fps en fenêtre glissante (delta depuis le dernier report)
_fps_last_t   = start
next_t = start
interval = 1.0 / FPS
if FLOW:
    next_t = _grid_next(start, interval)   # cadence flow : pacing = grille TAI (epochs)
half_ns = (interval / 2.0) * 1e9
last_ref_idx = 0
last_black = start
empty_frame = _BLACK_B * (WIDTH * HEIGHT) + _NEUT_B * (2 * UV_W * UV_H)   # black yuv

while True:
    if bus_error.is_set():
        bus_error.clear()
        for i in range(N_INPUTS):
            h = input_handles[i]
            if h is not None:
                try: h[0].close()
                except Exception: pass
                input_handles[i] = None
        try: out.close()
        except Exception: pass
        # GC ENTRE close et réouverture (piège des générations) : sans GC, les flux périmés
        # restent résolvables par nom et la réouverture retombe sur les orphelins.
        try: inst.garbage_collect()
        except Exception: pass
        time.sleep(2)
        out = _ouvrir_sortie(SHM_OUT)
        continue

    # Cadence : flow (grille TAI, mode tranche), genlock verrou sur le FOND (slot 4, réf)
    # + alignement des boxes, ou free-run hérité.
    T = None
    _fi_out = None
    if FLOW:
        # CADENCE « flow » (motif multiview) : pacing = grille TAI — le verrou-entrée sur le
        # fond disparaît ; chaque couche CIBLE l'index d'epoch _fi_out de SA source et la
        # sortie est écrite À CE MÊME INDEX (alignement inter-étages du tissu). Fond absent →
        # bandes noires cadencées (signal vivant). _next_index en tai = lecture pure de grille.
        now = time.time()
        wait = next_t - now
        if wait > 0: time.sleep(wait)
        _fi_out = out._next_index()
    elif GENLOCK:
        ri, rw = _in_header(4)
        if ri == 0:
            # pas de fond câblé → noir continu cadencé (signal vivant)
            now = time.time()
            if now - last_black >= interval:
                _ecrire_sortie(out, empty_frame, frame_index); frame_index += 1; last_black = now
            time.sleep(0.002); continue
        if ri == last_ref_idx:
            time.sleep(0.002); continue           # pas de nouvelle trame de fond → patiente
        last_ref_idx = ri; last_black = time.time()
        T = rw
        # MODE TRANCHE : pré-alignement sauté — les attentes get_slice PAR BANDE (budget par
        # couche + backoff) subsument l'alignement ; attendre ICI retarderait le suivi des
        # 1ʳᵉˢ tranches du fond (latence sous-trame perdue).
        if ALIGN_BUDGET > 0 and not SLICE_ON:
            budget_deadline = time.time() + ALIGN_BUDGET * interval
            while time.time() < budget_deadline:
                behind = False
                for c in range(N_BOXES):           # boxes 0..3
                    _, cw = _in_header(c)
                    if cw and cw < T - half_ns:
                        behind = True; break
                if not behind: break
                time.sleep(0.002)
    else:
        now = time.time()
        wait = next_t - now
        if wait > 0: time.sleep(wait)

    # Snapshot state ; resolve transition progress / commit.
    with state_lock:
        tr = state["transition"]
        boxes = [dict(b) for b in state["boxes"]]
        if tr is not None:
            elapsed_ms = (time.time() - tr["start"]) * 1000.0
            if elapsed_ms >= tr["duration_ms"]:
                state["boxes"] = [dict(b) for b in tr["to"]]
                state["transition"] = None
                boxes = [dict(b) for b in state["boxes"]]
                tr = None

    ts_in_per_slot = {{}}
    ts_cycle_start = time.time_ns()   # début du compositing (fond détecté + boxes alignées) → base transit/own
    _sl_waited = 0   # MODE TRANCHE : cumul des attentes get_slice de la trame (exclu de own)

    if SLICE_ON:
        # MODE TRANCHE : composition + publication BANDE PAR BANDE (fond + boxes suivis par
        # tranches sur leur grain, commit progressif de la sortie → l'aval démarre sur la 1ʳᵉ
        # bande). Le chemin historique ci-dessous reste octet-identique (slice_mode off).
        try:
            _sl_waited = _slice_frame(_fi_out if FLOW else frame_index, _fi_out,
                                      tr, boxes, ts_in_per_slot)
        except Exception as _e:
            print(f"split: erreur slice_frame ignorée (frame {{frame_index}}) : {{_e}}")
        ts_out = time.time_ns()
    else:
        # Background (slot 4, full frame).
        bg_frame, ts_bg = lire_frame_yuv(ensure_input(4))
        if ts_bg: ts_in_per_slot[4] = ts_bg
        yuv = bg_frame or empty_frame

        # Compose each box (order 0->3).
        if tr is not None:
            p = max(0.0, min(1.0, (time.time() - tr["start"]) * 1000.0 / tr["duration_ms"]))
        for i in range(N_BOXES):
            if tr is not None:
                res = _interp_box(tr["from"][i], tr["to"][i], p)
                if res is None: continue
                b, alpha = res
            else:
                if not boxes[i]["enabled"]: continue
                b, alpha = boxes[i], 1.0
            f, ts = lire_frame_yuv(ensure_input(i))
            if ts: ts_in_per_slot[i] = ts
            if f:
                yuv = composite_box_yuv(yuv, f, alpha, b["x"], b["y"], b["size"], b["crop"], b.get("border"))

        _ecrire_sortie(out, yuv, frame_index)
        ts_out = time.time_ns()

    with state_lock:
        slot_to_shm = list(state["inputs"])
    _now_tai = bobimxl.now_tai()                                 # base TAI cohérente avec last_write_time
    skew_in = {{}}; late_in = []
    for slot_idx, ts_in in ts_in_per_slot.items():
        shm_name = slot_to_shm[slot_idx] if slot_idx < len(slot_to_shm) else ""
        if not shm_name: continue
        if shm_name not in lat_in:
            lat_in[shm_name] = RollingMs()
        lat_in[shm_name].push((_now_tai - ts_in) / 1e6)         # TRANSIT (arrivée) = âge à la lecture (TAI)
        if T is not None:
            skew_in[shm_name] = round((ts_in - T) / 1e6, 2)
            if ts_in < T - half_ns:
                late_in.append(shm_name)
    # traitement PROPRE (compositing DVE) — HORS attentes get_slice (mode tranche : le
    # monitoring/tissu y lit la saturation du worker, pas le suivi du fil, contrat multiview).
    own_lat.push((ts_out - ts_cycle_start - _sl_waited) / 1e6)
    if T is not None:
        with metrics_lock:
            metrics["inputs_skew_ms"] = skew_in
            metrics["inputs_late"] = late_in

    frame_index += 1
    if FLOW:
        # Recale grille (motif multiview flow) : suivre un grain occupe ~toute la période — un
        # retard < 1 période est RATTRAPÉ (tick immédiat, les attentes par bande absorbent le
        # retard, la phase reconverge seule) ; au-delà, recale sur la prochaine frontière.
        next_t += interval
        if next_t < time.time() and time.time() - next_t >= interval:
            next_t = _grid_next(time.time(), interval)
    else:
        next_t = start + frame_index * interval

    if frame_index % FPS == 0:
        _now = time.time(); _dt = _now - _fps_last_t
        with metrics_lock:
            if _dt > 0 and frame_index >= _fps_last_idx:
                metrics["fps"] = round((frame_index - _fps_last_idx) / _dt, 1)
            _fps_last_idx = frame_index; _fps_last_t = _now
            metrics["frame_index"] = frame_index
            metrics["inputs_latency_ms"] = {{k: v.avg() for k, v in lat_in.items()}}
            metrics["own_latency_ms"] = own_lat.avg()
            if _sl_dbg:
                # Observabilité mode tranche : compteurs de la dernière trame promus sur :8080.
                metrics["slice"] = {{"layers": _sl_dbg[0], "valid0": _sl_dbg[1],
                                    "waits": _sl_dbg[2], "fallbacks": _sl_dbg[3],
                                    "dormant": _sl_dbg[4], "backoff": len(_sl_backoff)}}
