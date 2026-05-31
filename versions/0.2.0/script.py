# ─────────────────────────────────────────────────────────────────────────────
# DVE / SuperSource plugin : 4 PiP boxes composités sur 1 fond → 1 sortie.
#   5 entrées : slots 0..3 = boxes, slot 4 = fond. Câblage à chaud par slot.
#   Box : centre (x,y), taille (size, aspect préservé), crop (t/b/l/r).
#   Rappel mémoire = transition temporisée (tween géométrie + crossfade alpha).
#   Contrôle :8082, métriques :8080.
#
# Template str.format : SEULS {config} / {hostname} / {plugin_version} sont des
# placeholders. TOUTE autre accolade littérale doit être doublée {{ }}.
# ─────────────────────────────────────────────────────────────────────────────
import mmap, struct, time, threading, json, os, signal
from collections import deque
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler

class RollingMs:
    def __init__(self, n=30):
        self.d = deque(maxlen=n); self.last_ns = 0
    def push(self, ms_value):
        self.d.append(ms_value); self.last_ns = time.time_ns()
    def avg(self):
        if not self.d: return None
        if time.time_ns() - self.last_ns > 2_000_000_000: return None
        return round(sum(self.d) / len(self.d), 1)

lat_in = {{}}  # {{shm_name: RollingMs}}

# ─── Config injectée (contrat plugin) ───────────────────────
CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

SHM_OUT  = CONFIG.get("shm_out") or (HOSTNAME + "_dve")
WIDTH    = int(CONFIG.get("width") or 1280);  WIDTH  -= WIDTH % 2
HEIGHT   = int(CONFIG.get("height") or 720);  HEIGHT -= HEIGHT % 2
FPS      = int(round(float(CONFIG.get("fps") or 25))) or 25
N_BOXES  = 4
N_INPUTS = 5                       # slots 0..3 = boxes, 4 = fond
INITIAL_BOX_CONFIG = CONFIG.get("box_config") or []
# Entrées persistées par slot (state_field input_0..3 + bg_input) → re-câblage au boot
_INIT_SLOT_SHM = [CONFIG.get("input_0"), CONFIG.get("input_1"),
                  CONFIG.get("input_2"), CONFIG.get("input_3"), CONFIG.get("bg_input")]

# ─── yuv420p format (consistent with receiver/mixer/multiview) ──
V_HEADER_SIZE = 64
V_RING_SIZE   = 10
V_FRAME_SIZE  = WIDTH * HEIGHT * 3 // 2
V_TOTAL_SIZE  = V_HEADER_SIZE + V_RING_SIZE * V_FRAME_SIZE
Y_SIZE        = WIDTH * HEIGHT
UV_SIZE       = (WIDTH // 2) * (HEIGHT // 2)


def _hex_to_yuv(h):
    """#RRGGBB (ou #RGB) → (Y,U,V) BT.601 limité 0..255. Défaut blanc."""
    h = str(h or "#ffffff").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
    except Exception:
        r = g = b = 255
    y = int(0.299 * r + 0.587 * g + 0.114 * b)
    u = int(-0.169 * r - 0.331 * g + 0.5 * b + 128)
    v = int(0.5 * r - 0.419 * g - 0.081 * b + 128)
    return max(0, min(255, y)), max(0, min(255, u)), max(0, min(255, v))


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
metrics = {{"fps": 0.0, "frame_index": 0, "inputs_latency_ms": {{}}, "plugin_version": PLUGIN_VERSION, "variant": "v2"}}

bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    print("SIGBUS reçu — réouverture des mmap")
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
    with state_lock:
        wanted = state["inputs"][idx]
    cur = input_handles[idx]
    if not wanted:
        if cur is not None:
            try: cur[1].close(); cur[0].close()
            except Exception: pass
            input_handles[idx] = None
        return None
    if cur is not None and cur[2] == wanted:
        return cur[1]
    if cur is not None:
        try: cur[1].close(); cur[0].close()
        except Exception: pass
        input_handles[idx] = None
    path = f"/dev/shm/{{wanted}}"
    try:
        if not os.path.exists(path): return None
        if os.path.getsize(path) < V_TOTAL_SIZE: return None
        f = open(path, "r+b")
        shm = mmap.mmap(f.fileno(), V_TOTAL_SIZE, prot=mmap.PROT_READ)
        _ = shm[0:16]
        input_handles[idx] = (f, shm, wanted)
        print(f"input #{{idx}} câblé sur {{path}}")
        return shm
    except Exception as e:
        print(f"input #{{idx}} ({{wanted}}) indisponible : {{e}}")
        return None


def lire_frame_yuv(shm):
    if shm is None: return None, None
    try:
        frame_index, ts_in = struct.unpack("QQ", shm[0:16])
        if frame_index == 0: return None, None
        slot = frame_index % V_RING_SIZE
        offset = V_HEADER_SIZE + slot * V_FRAME_SIZE
        return bytes(shm[offset:offset + V_FRAME_SIZE]), ts_in
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

    src_y = np.frombuffer(src_yuv[:Y_SIZE], dtype=np.uint8).reshape(HEIGHT, WIDTH)
    src_u = np.frombuffer(src_yuv[Y_SIZE:Y_SIZE+UV_SIZE], dtype=np.uint8).reshape(HEIGHT//2, WIDTH//2)
    src_v = np.frombuffer(src_yuv[Y_SIZE+UV_SIZE:], dtype=np.uint8).reshape(HEIGHT//2, WIDTH//2)

    # Nearest-neighbor sample maps from the cropped source region to the full dst box.
    ix = (sxr0 + (np.arange(dst_w) + 0.5) / dst_w * (sxr1 - sxr0)).astype(np.int32)
    iy = (syr0 + (np.arange(dst_h) + 0.5) / dst_h * (syr1 - syr0)).astype(np.int32)
    ix = np.clip(ix, 0, WIDTH - 1); iy = np.clip(iy, 0, HEIGHT - 1)
    k_y_full = src_y[iy[:, None], ix[None, :]]
    ix2 = (sxr0 / 2 + (np.arange(dst_w // 2) + 0.5) / (dst_w // 2) * ((sxr1 - sxr0) / 2)).astype(np.int32)
    iy2 = (syr0 / 2 + (np.arange(dst_h // 2) + 0.5) / (dst_h // 2) * ((syr1 - syr0) / 2)).astype(np.int32)
    ix2 = np.clip(ix2, 0, WIDTH//2 - 1); iy2 = np.clip(iy2, 0, HEIGHT//2 - 1)
    k_u_full = src_u[iy2[:, None], ix2[None, :]]
    k_v_full = src_v[iy2[:, None], ix2[None, :]]

    k_y = k_y_full[src_y_off:src_y_off+vis_h, src_x_off:src_x_off+vis_w]
    k_u = k_u_full[src_y_off//2:(src_y_off+vis_h)//2, src_x_off//2:(src_x_off+vis_w)//2]
    k_v = k_v_full[src_y_off//2:(src_y_off+vis_h)//2, src_x_off//2:(src_x_off+vis_w)//2]

    bgd_y = np.frombuffer(bgd_yuv[:Y_SIZE], dtype=np.uint8).reshape(HEIGHT, WIDTH).copy()
    bgd_u = np.frombuffer(bgd_yuv[Y_SIZE:Y_SIZE+UV_SIZE], dtype=np.uint8).reshape(HEIGHT//2, WIDTH//2).copy()
    bgd_v = np.frombuffer(bgd_yuv[Y_SIZE+UV_SIZE:], dtype=np.uint8).reshape(HEIGHT//2, WIDTH//2).copy()

    reg_y = bgd_y[out_y0:out_y1, out_x0:out_x1].astype(np.float32)
    bgd_y[out_y0:out_y1, out_x0:out_x1] = (reg_y * (1.0 - alpha) +
                                           k_y.astype(np.float32) * alpha).astype(np.uint8)
    reg_u = bgd_u[out_y0//2:out_y1//2, out_x0//2:out_x1//2].astype(np.float32)
    bgd_u[out_y0//2:out_y1//2, out_x0//2:out_x1//2] = (reg_u * (1.0 - alpha) +
                                           k_u.astype(np.float32) * alpha).astype(np.uint8)
    reg_v = bgd_v[out_y0//2:out_y1//2, out_x0//2:out_x1//2].astype(np.float32)
    bgd_v[out_y0//2:out_y1//2, out_x0//2:out_x1//2] = (reg_v * (1.0 - alpha) +
                                           k_v.astype(np.float32) * alpha).astype(np.uint8)

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
            cbw = max(1, bw // 2)
            uy0, uy1, ux0, ux1 = out_y0//2, out_y1//2, out_x0//2, out_x1//2
            for plane, val in ((bgd_u, Ub), (bgd_v, Vb)):
                plane[uy0:uy0+cbw, ux0:ux1] = val
                plane[uy1-cbw:uy1, ux0:ux1] = val
                plane[uy0:uy1, ux0:ux0+cbw] = val
                plane[uy0:uy1, ux1-cbw:ux1] = val
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
    path = f"/dev/shm/{{name}}"
    with open(path, "wb") as f:
        f.write(b"\x00" * V_TOTAL_SIZE)
    f = open(path, "r+b")
    return f, mmap.mmap(f.fileno(), V_TOTAL_SIZE)

def _ecrire_sortie(shm, yuv, frame_index):
    slot = frame_index % V_RING_SIZE
    offset = V_HEADER_SIZE + slot * V_FRAME_SIZE
    shm[offset:offset + V_FRAME_SIZE] = yuv
    shm[0:16] = struct.pack("QQ", frame_index, time.time_ns())

out_f, out = _ouvrir_sortie(SHM_OUT)

frame_index = 0
start = time.time()
next_t = start
interval = 1.0 / FPS
empty_frame = b"\x10" * Y_SIZE + b"\x80" * (2 * UV_SIZE)   # black yuv

while True:
    if bus_error.is_set():
        bus_error.clear()
        for i in range(N_INPUTS):
            h = input_handles[i]
            if h is not None:
                try: h[1].close(); h[0].close()
                except Exception: pass
                input_handles[i] = None
        try: out.close(); out_f.close()
        except Exception: pass
        time.sleep(2)
        out_f, out = _ouvrir_sortie(SHM_OUT)
        continue

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
    for slot_idx, ts_in in ts_in_per_slot.items():
        shm_name = slot_to_shm[slot_idx] if slot_idx < len(slot_to_shm) else ""
        if not shm_name: continue
        if shm_name not in lat_in:
            lat_in[shm_name] = RollingMs()
        lat_in[shm_name].push((ts_out - ts_in) / 1e6)

    frame_index += 1
    next_t = start + frame_index * interval

    if frame_index % FPS == 0:
        elapsed = time.time() - start
        with metrics_lock:
            metrics["fps"] = round(frame_index / elapsed, 1)
            metrics["frame_index"] = frame_index
            metrics["inputs_latency_ms"] = {{k: v.avg() for k, v in lat_in.items()}}
