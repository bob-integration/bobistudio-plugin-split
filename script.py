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
import time, threading, json, signal, math, base64, io, os
from collections import deque, OrderedDict
import numpy as np
import bobimxl   # migration MXL Phase 1 : 5 entrées Reader + 1 sortie Writer (planar, genlock fond)

class _StageDelayNulle:
    """Repli si le bobimxl de l'IMAGE est plus ancien que le script poussé.

    `deploy.py` pousse `bobimxl.py` à côté du script (il SHADOWE celui de l'image), mais le fait
    en BEST-EFFORT : un échec de ce push est journalisé et le déploiement continue avec le binding
    de l'image. Sans ce repli, l'absence de `StageDelay` lèverait AttributeError à l'import et
    tuerait le conteneur — pour une métrique manquante. Une mesure absente doit coûter la mesure,
    pas le module. Cf. docs/reference/LATENCE_CHAINE.md.
    """
    def observe(self, *a, **k):
        return False

    def publish(self):
        return None


def _sd_new():
    return bobimxl.StageDelay() if hasattr(bobimxl, "StageDelay") else _StageDelayNulle()



def _mxl_lib_state():
    """Variante libmxl réellement chargée (baseline / x86-64-v3) — diagnostic seul, ne doit
    JAMAIS faire échouer /state."""
    try:
        return bobimxl.lib_info()
    except Exception:
        return None
from http.server import HTTPServer, BaseHTTPRequestHandler

inst = bobimxl.Instance()   # domaine MXL ($MXL_DOMAIN ou /dev/shm/mxl)

# ─── Noyau de compositing C (libbobi_mvk, ABI 4 — image bobi-compute ≥ 0.15) ────────────────
# Le mode AVANCÉ (rotation, feather, coins, opacité, bordure 3D) était le DERNIER compositeur
# du projet resté en numpy pur. Ses passes sont memory-bound : gather 2D + ruban + blend, une
# passe mémoire chacune. Les kernels mvk_spl_* les fusionnent en UNE passe (script_templates/
# mvcompose.c) — arithmétique 256e IDENTIQUE (accumulation uint32), donc BIT-EXACTE.
# GARDES (motif mixer/multiview) : lib absente (vieille image), ABI < 4, forme/dtype/contiguïté
# non conformes → le wrapper renvoie False → REPLI numpy intégral (le code d'origine, qui reste
# la référence). ★ Un repli SILENCIEUX a déjà coûté 26 % des blends du multiview : ici chaque
# repli est COMPTÉ et exposé sur :8080 (`mvk_miss`/`mvk_why`) — il doit rester à ZÉRO.
_MVK = bool(getattr(bobimxl, "mvk_available", lambda: False)()) \
    and bool(getattr(bobimxl, "mvk_spl_compose_into", None)) \
    and not os.environ.get("BOBI_SPLIT_NOMVK")
_mvk_miss = {{"compose": 0, "fused": 0, "solid": 0, "gradient": 0,
              "blur2": 0, "rotmap": 0, "rotring": 0, "prefuse": 0, "outer": 0, "calls": 0}}
_mvk_why = {{}}   # signature (forme/dtype) des replis — pour identifier la cause sans deviner
# ABI 6 : kernels de CONSTRUCTION d'empreinte (régime animé). Drapeau SÉPARÉ de `_MVK` — une
# image ≥ 0.15 sans ABI 6 garde tout le compositing C et ne perd que la construction.
_MVK_STAMP = _MVK and bool(getattr(bobimxl, "mvk_spl_rotmap", None))


def _mvk_try(kind, fn, *a, **kw):
    """Appelle un kernel mvk ; compte et SIGNE le repli s'il refuse (jamais d'échec muet)."""
    _mvk_miss["calls"] += 1
    if _MVK and fn(*a, **kw):
        return True
    if _MVK:
        _mvk_miss[kind] += 1
        d = a[0]
        k = "%s %s %s" % (kind, getattr(d, "shape", "?"), getattr(d, "dtype", "?"))
        _mvk_why[k] = _mvk_why.get(k, 0) + 1
    return False


def _mvk_try_r(kind, fn, *a, **kw):
    """Variante de `_mvk_try` pour un kernel qui RENVOIE ses tableaux (et non un booléen) :
    renvoie le résultat, ou None en comptant le repli."""
    _mvk_miss["calls"] += 1
    r = fn(*a, **kw) if _MVK_STAMP else None
    if r is not None:
        return r
    if _MVK_STAMP:
        _mvk_miss[kind] += 1
        _mvk_why["%s %sx%s" % (kind, a[0], a[1])] = _mvk_why.get("%s %sx%s" % (kind, a[0], a[1]), 0) + 1
    return None

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
# DÉLAI D'ÉTAGE en TRAMES — grandeur DISTINCTE de own_lat, qui est un temps de CALCUL.
# MODE TRANCHE (0.24.2) : la sortie PROPAGE l'index de son contenu (open_grain(src_index=…), le
# plus ancien des index lus sur la grille) → étage « propage », écart d'index nul par construction
# (StageDelay observe avec propage=True). Fond uni seul → grille. TRAME PLEINE : coordonnée de
# grille, délai non mesuré (cf. le commentaire du chemin trame pleine). Multi-couches : on passe
# TOUTES les couches liées, StageDelay en retient les deux bornes.
# Cf. docs/reference/LATENCE_CHAINE.md.
delai_etage = _sd_new()

# ─── Config injectée (contrat plugin) ───────────────────────
CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

# ─── Niveau de log ─────────────────────────────────────────────────────────
# `log_level` (config_schema du plugin, défaut « info ») filtre les impressions du script.
# Le critère n'est PAS « verbeux vs silencieux » mais ÉVÉNEMENT vs MÉTRIQUE :
#   debug   — le lance-flammes : par trame, par bande, décisions internes
#   info    — ÉVÉNEMENTS rares et signifiants  ← DÉFAUT (toujours visible) : démarrage/
#             arrêt, session ouverte/fermée, changement de format, reconnexion, repli sur
#             un chemin dégradé, entrée qui apparaît/disparaît, rebascule.
#   warning — anomalies et replis subis
#   error   — échecs
# RÈGLE 1 : après une panne, le journal PAR DÉFAUT doit permettre de RECONSTITUER
#   l'histoire. Élever le niveau après coup ne récupère RIEN : ce qui n'a pas été écrit
#   est perdu. On ne coupe donc pas l'information, on coupe la redondance.
# RÈGLE 2 : une MÉTRIQUE PÉRIODIQUE (fps, compteurs) ne se journalise PAS — elle est déjà
#   publiée sur :8080 et échantillonnée par l'orchestrateur. La journaliser duplique la
#   mesure ET consomme la fenêtre de rétention (journal Docker non roté : le bruit purge
#   les lignes utiles anciennes). Au mieux `debug`.
# RÈGLE 3 : un événement qui peut partir EN RAFALE s'AGRÈGE sur une fenêtre et sort en UNE
#   ligne périodique (« N frames lentes sur la dernière minute, pire … ») — le signal
#   reste, le spam disparaît.
# Réglable à chaud, sans redéployer, quand le plugin expose l'endpoint de contrôle :
# POST :8082/log_level {{"level": "debug"}} (exposé aux macros via param_tree/actions).
_LOG_ORDER = {{"debug": 10, "info": 20, "warning": 30, "error": 40}}
LOG_LEVEL = str(CONFIG.get("log_level") or "info").strip().lower()
if LOG_LEVEL not in _LOG_ORDER:
    LOG_LEVEL = "info"
_LOG_MIN = _LOG_ORDER[LOG_LEVEL]


def log(msg, niveau="info"):
    """Impression gatée par le niveau de log courant (défaut du message : « info »)."""
    if _LOG_ORDER.get(niveau, 20) >= _LOG_MIN:
        print(msg, flush=True)


def set_log_level(niveau):
    """Change le niveau à chaud. Renvoie True si le niveau est reconnu."""
    global LOG_LEVEL, _LOG_MIN
    lv = str(niveau or "").strip().lower()
    if lv not in _LOG_ORDER:
        return False
    LOG_LEVEL, _LOG_MIN = lv, _LOG_ORDER[lv]
    return True



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

# ═════════════════════════════════════════════════════════════════════════════════════════════
#  ★ OPACITÉ EN POURCENT (0-100) — UNE SEULE UNITÉ, celle qu'on AFFICHE (0.12.0)
#
#  Le moteur parlait en fraction (0..1) : illisible à l'écran, et pénible au stepper (pas de
#  0,01). L'unité du MOTEUR devient le POURCENT — box, ombre, habillage, canal Global — et
#  `/state.caps` l'expose (0..100, pas de 1) : l'UI ne convertit RIEN, il n'y a pas deux vérités.
#  La conversion % → fraction se fait à UN SEUL endroit : le point d'usage du compositing.
#
#  ★ LA MIGRATION N'EST PAS IDEMPOTENTE PAR NATURE : un `1.0` persisté veut dire « opaque » en
#  ancienne unité et « 1 % » en nouvelle — aucune heuristique ne lève l'ambiguïté. On ne devine
#  donc pas : un MARQUEUR tranche. `opacity_unit: "pct"` = déjà en pourcent ; absent = ancienne
#  unité → ×100 UNE FOIS, au boot. Le hook de persistance réécrit le marqueur → jamais deux fois.
OPACITY_UNIT = str(CONFIG.get("opacity_unit") or "").strip().lower()


def _mig_op(v, dflt):
    try:
        f = float(v)
    except Exception:
        return dflt
    return f if OPACITY_UNIT == "pct" else f * 100.0


def _migrate_boxes_opacity(boxes):
    if OPACITY_UNIT == "pct" or not boxes:
        return boxes or []
    out = []
    for b in boxes:
        b = dict(b or {{}})
        if "opacity" in b:
            b["opacity"] = _mig_op(b["opacity"], 100.0)
        sh = dict(b.get("shadow") or {{}})
        if "opacity" in sh:
            sh["opacity"] = _mig_op(sh["opacity"], 50.0)
            b["shadow"] = sh
        out.append(b)
    log("split: opacités migrées de la fraction (0..1) vers le POURCENT (0-100) — unité unique "
        "moteur+UI (marqueur opacity_unit=pct persisté au prochain sync)", "info")
    return out


INITIAL_BOX_CONFIG = _migrate_boxes_opacity(CONFIG.get("box_config") or [])
# Entrées persistées par slot (state_field input_0..3 + bg_input) → re-câblage au boot
_INIT_SLOT_SHM = [CONFIG.get("input_1"), CONFIG.get("input_2"),
                  CONFIG.get("input_3"), CONFIG.get("input_4"), CONFIG.get("bg_input")]

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
Y_SIZE        = WIDTH * HEIGHT * _BPS
UV_SIZE       = UV_W * UV_H * _BPS
V_FRAME_SIZE  = Y_SIZE + 2 * UV_SIZE
# (ex-shm_video_ring/V_RING_SIZE/V_HEADER_SIZE/V_TOTAL_SIZE : dimensionnaient un ancien shm
# maison ; la sortie vidéo passe par bobimxl (MXL) — profondeur gérée par domaine, cf.
# réglage de nœud mxl_history_ms. Retiré 2026-08-09, constante morte.)

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
# ── CHEMIN DE RENDU GPU (render=gpu|auto, cf. help.md « CPU ou GPU ? ») ────────────────────────
# Le GPU ne rend PAS le CPU obsolète — il déplace un plafond précis, et un seul : celui de la
# géométrie qui BOUGE. Sur GPU il n'y a pas d'empreinte à reconstruire (chaque pixel évalue sa
# propre géométrie), donc le coût d'une trame animée est celui d'une trame fixe. Sur CPU, la même
# trame animée coûte jusqu'à 12× la trame fixe. Une composition qui ne bouge pas n'a donc RIEN à
# gagner à passer sur GPU — elle y perdrait même les transferts PCIe.
#   render=cpu   (défaut) : chemin historique, strictement inchangé.
#   render=gpu            : EXIGE le GPU — absence de carte/cupy = échec BRUYANT (on ne rend pas
#                           en silence 10× plus lentement que ce que l'exploitant a demandé).
#   render=auto           : GPU si disponible, sinon CPU EN LE DISANT.
_RENDER = str(CONFIG.get("render") or "cpu").strip().lower()
if _RENDER not in ("cpu", "gpu", "auto"):
    _RENDER = "cpu"
GPU = False
_GPU_NAME = None
_GPU_WHY = None            # pourquoi le GPU n'est PAS actif — exposé sur :8080, jamais muet
cp = None
if _RENDER in ("gpu", "auto"):
    try:
        import cupy as cp
        cp.cuda.runtime.getDeviceCount()          # lève si aucun device (pas de --gpus)
        _GPU_NAME = cp.cuda.runtime.getDeviceProperties(0)["name"].decode("utf-8", "replace")
        GPU = True
    except Exception as _e:
        cp = None
        _GPU_WHY = "cupy/GPU indisponible : %s" % _e

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
    log(f"split: slice_mode demandé mais inéligible (scan={{_SCAN}} h={{HEIGHT}}) — whole-frame", "warning")

# ★ MODE TRANCHE et GPU sont EXCLUSIFS, et ce n'est pas un détail d'implémentation : le mode
# tranche existe pour publier la sortie bande par bande et retirer une image de latence de
# traversée ; un aller-retour vers la carte se fait par TRAME et la réintroduit. Les tenir tous
# les deux donnerait une latence de mode tranche avec le coût du GPU — le pire des deux. On
# tranche ici, une fois, en le DISANT (un « GPU demandé, silencieusement ignoré » serait
# exactement l'échec muet que ce plugin s'interdit).
if GPU and SLICE_ON:
    GPU = False
    _GPU_WHY = "mode tranche actif (latence sous-trame) — exclusif du rendu GPU (aller-retour par trame)"
    log("split: render=%s ignoré — %s" % (_RENDER, _GPU_WHY), "warning")
if _RENDER == "gpu" and not GPU:
    # `gpu` EXIGE la carte : on refuse de rendre en silence sur un chemin 10× plus lent que celui
    # que l'exploitant a demandé. `auto` est là pour qui veut le repli.
    raise SystemExit("split: render=gpu demandé mais GPU inutilisable — %s. "
                     "Utiliser render=auto pour accepter le repli CPU." % (_GPU_WHY or "?"))
if _RENDER == "auto" and not GPU:
    log("split: render=auto — repli CPU (%s)" % (_GPU_WHY or "?"), "warning")

# ── CADENCE « flow » (tissu en tranches, motif multiview) : pacing = GRILLE TAI — le
# verrou-entrée sur le fond disparaît ; à chaque epoch, chaque couche (fond + boxes) CIBLE
# l'index d'epoch fi_out de SA source et la sortie est écrite À CE MÊME INDEX (alignement
# inter-étages du tissu). Exige le mode tranche éligible ; sinon repli pacing historique.
CADENCE = str(CONFIG.get("cadence") or "").strip().lower()
FLOW = (CADENCE == "flow") and SLICE_ON
if CADENCE == "flow" and not FLOW:
    log("split: cadence=flow exige le mode tranche éligible — repli pacing historique", "warning")

def _grid_next(now_s, interval_s):
    import math as _m
    return (_m.floor(now_s / interval_s) + 1) * interval_s


def _hex_to_yuv(h):
    """#RRGGBB (ou #RGB) → (Y, U, V) DANS LE FORMAT DÉCLARÉ du conteneur. Défaut blanc.

    ★ CORRECTION 0.12.2 — Cette fonction (couleurs de BORDURE, d'OMBRE et de CLÉ CHROMA) portait
    une matrice **BT.601 pleine échelle CODÉE EN DUR**, quelle que soit la colorimétrie du
    conteneur : dans un pipeline 709 (le cas courant) ou 2020, la couleur demandée n'était PAS
    celle rendue (un rouge de bordure sortait faux). Elle délègue désormais à `_rgb_to_yuv`, seule
    conversion du plugin : matrice = `colorimetry` DÉCLARÉE, échelle = `bit_depth`, plage LÉGALE
    (broadcast). ⚠ Les octets d'une composition existante utilisant une couleur CHANGENT — c'est
    VOULU : ils étaient FAUX, ils sont maintenant conformes au format déclaré."""
    y, u, v, _clipped = _rgb_to_yuv(h or "#ffffff", True)
    return y, u, v


def _f(v, lo, hi, dflt):
    """float borné, tolérant (valeur absente/illisible → défaut)."""
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return dflt


# ═════════════════════════════════════════════════════════════════════════════════════════════
#  CANAL GLOBAL (0.12.0) — un CALQUE DE TRANSFORMATION, pas une commodité d'édition
#
#  ★ NE PAS CONFONDRE AVEC LA MULTI-SÉLECTION. La multi-sélection ÉCRIT N valeurs dans N box :
#  elle ÉCRASE les réglages individuels. Le Global, lui, est un objet À PART, persistant, qui se
#  COMPOSE par-dessus les réglages propres de chaque box :
#
#        rendu(box i) = Global ∘ box_i
#
#  Chaque box garde SES x/y/taille/rotation/opacité ; le Global apporte SA transformation
#  (décalage, échelle, rotation, opacité). ⇒ toute la composition sort du cadre d'un bloc,
#  chaque box gardant sa position RELATIVE — c'est l'« animation de groupe », impossible avec
#  une multi-édition (qui, elle, écraserait les valeurs individuelles).
#
#  ★ COÛT : ZÉRO PASSE SUPPLÉMENTAIRE. La transformation est REPLIÉE dans la géométrie de chaque
#  box AVANT le rendu (quelques flottants par box et par trame, dans _plan_layers) — elle finit
#  donc dans le STAMP de la box, comme n'importe quel réglage. Surtout PAS une seconde passe
#  plein cadre : on a déjà mesuré ce que coûte une passe pleine trame (l'ART en source VIVE :
#  21,7 ms/trame en 1080p, 108 % du budget 50 fps À ELLE SEULE).
#
#  CHAMPS RETENUS (et pourquoi PAS les autres) : dx/dy (décalage), scale (échelle), rotation,
#  opacity — les 4 grandeurs qui ont un sens pour un GROUPE et qui se composent proprement.
#  Volontairement ÉCARTÉS : miroir (un miroir de groupe échange la gauche et la droite des box
#  SANS miroiter leur contenu — geste ambigu, à faire par box), adoucissement/coins/bordure/
#  ombre (ce sont des attributs de BORD d'une box : appliqués « au groupe » ils ne voudraient
#  rien dire tant qu'on ne compose pas le groupe dans un buffer intermédiaire — ce qui coûterait
#  précisément la passe plein cadre qu'on s'interdit).
# ═════════════════════════════════════════════════════════════════════════════════════════════
POS_MIN, POS_MAX = -1.0, 2.0        # bornes x/y d'une box (hors cadre autorisé), cf. caps
GLOBAL_NEUTRAL = {{"dx": 0.0, "dy": 0.0, "scale": 1.0, "rotation": 0.0, "opacity": 100.0}}


# ═════════════════════════════════════════════════════════════════════════════════════════════
#  FOND (slot 4) — TROIS MODES : `none` (noir), `source` (entrée vidéo), `color` (couleur unie).
#
#  ★ COÛT : la conversion RGB → YUV est faite UNE FOIS, à la sélection (cache), JAMAIS par trame.
#  Le remplissage se réduit alors à trois affectations scalaires (memset par plan) — en mode
#  TRANCHE il se fait bande par bande, exactement comme le fond vidéo : aucun chemin nouveau.
#
#  ★ COLORIMÉTRIE ET PROFONDEUR : RIEN N'EST EN DUR — TOUT SE LIT DANS LE FORMAT DÉCLARÉ.
#  La matrice vient de CONFIG["colorimetry"] (601 / 709 / 2020 — 709 aujourd'hui, mais ça se
#  RÈGLE), et toutes les bornes se dérivent de CONFIG["bit_depth"] par 2^(bit_depth − 8) :
#      8 bits  : Y 16-235,  C 16-240,   neutre 128
#      10 bits : Y 64-940,  C 64-960,   neutre 512
#      12 bits : Y 256-3760, C 256-3840, neutre 2048
#  (calculées, jamais tabulées : une constante 8 bits ici casserait le 10/12 bits EN SILENCE —
#  c'est exactement le débordement uint16 qu'on vient de payer sur les blends.)
#  Par défaut la couleur est encodée en PLAGE LIMITÉE (« legal ») : c'est ce qu'attend une chaîne
#  broadcast, un blanc à pleine échelle est HORS GAMME et serait écrêté en aval. L'écrêtage n'est
#  PAS silencieux : `bg.clipped` le dit, et `bg.legal=false` donne la plage COMPLÈTE en
#  connaissance de cause. La couleur est persistée en HEX (sRGB) — jamais en YUV : si le
#  conteneur est redéployé en 10 bits ou dans une autre colorimétrie, elle est RECONVERTIE.
BG_MODES = ("none", "source", "color", "gradient")   # gradient = mélange N arrêts + wipe

# Matrices luma (Kr, Kb) par colorimétrie DÉCLARÉE. `_hex_to_yuv` (bordure/ombre/clé chroma) est,
# lui, resté sur du BT.601 pleine échelle codé en dur — dette CONNUE, cf. help.md § audit.
_KRKB = {{"601": (0.299, 0.114), "709": (0.2126, 0.0722), "2020": (0.2627, 0.0593)}}
_COL_RAW = str(CONFIG.get("colorimetry") or "").strip()
COLORIMETRY = _COL_RAW.lower().replace("bt", "").replace(".", "")
# ★ JAMAIS DE DEVINETTE MUETTE : le format déclaré porte normalement `colorimetry` (l'orchestrateur
# le pousse depuis le format résolu de la source). S'il est ABSENT ou inconnu, on retombe sur 709
# (la colorimétrie HD, celle de tout ce parc) — et ON LE DIT : log au boot + `colorimetry_assumed`
# dans /state (l'UI l'affiche). Une couleur rendue dans la mauvaise matrice est un défaut SILENCIEUX
# — exactement ce qu'on refuse.
COLORIMETRY_ASSUMED = COLORIMETRY not in _KRKB
if COLORIMETRY_ASSUMED:
    log(f"split: colorimétrie NON DÉCLARÉE ou inconnue (« {{_COL_RAW}} ») — on SUPPOSE BT.709 pour "
        f"toutes les couleurs (bordure, ombre, clé chroma, habillage, fond). Si la chaîne est en "
        f"601 ou 2020, les couleurs seront FAUSSES : déclarez `colorimetry` dans le format.", "warning")
    COLORIMETRY = "709"


def _yuv01(r, g, b):
    """(R, G, B) 0..1 (scalaires OU tableaux numpy) → (Y, U, V) 0..1 / ±0,5, avec la matrice de la
    colorimétrie DÉCLARÉE. C'EST LA SEULE MATRICE DU PLUGIN — bordure, ombre, clé chroma,
    habillage et fond passent tous par ici (plus aucune conversion couleur en dur ailleurs)."""
    kr, kb = _KRKB[COLORIMETRY]
    kg = 1.0 - kr - kb
    y = kr * r + kg * g + kb * b
    return y, (b - y) / (2.0 * (1.0 - kb)), (r - y) / (2.0 * (1.0 - kr))


def _encode_yuv(y, u, v, legal=True):
    """(Y, U, V) 0..1 / ±0,5 → échantillons du format : bornes DÉRIVÉES de la profondeur
    (S = 2^(depth−8)), jamais tabulées.
        limitée  : Y = (16 + 219·y)·S     ·  C = neutre + 224·S·c   (8 b : 16-235 / 16-240)
        complète : Y = maxv·y             ·  C = neutre + maxv·c
    Fonctionne sur des scalaires ET sur des tableaux numpy (l'habillage y passe plein cadre)."""
    if legal:
        return ((16.0 + 219.0 * y) * _SCALE,
                _NEUTRAL + 224.0 * _SCALE * u,
                _NEUTRAL + 224.0 * _SCALE * v)
    return (_MAXV * y, _NEUTRAL + _MAXV * u, _NEUTRAL + _MAXV * v)


def _hex_rgb01(hexstr, dflt=0.0):
    """#RRGGBB (ou #RGB) → (r, g, b) 0..1, tolérant."""
    h = str(hexstr or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0
    except Exception:
        return dflt, dflt, dflt


def _rgb_to_yuv(hexstr, legal=True):
    """#RRGGBB (sRGB) → (Y, U, V, clipped) DANS LE FORMAT DÉCLARÉ : matrice = colorimétrie du
    conteneur, échelle = sa profondeur. `clipped` = la couleur sortait de la plage encodable."""
    r, g, b = _hex_rgb01(hexstr, 0.0)
    fy, fu, fv = _encode_yuv(*_yuv01(r, g, b), legal=legal)
    vals = [int(round(c)) for c in (fy, fu, fv)]
    clipped = any(c < 0 or c > _MAXV for c in vals)
    return tuple(max(0, min(_MAXV, c)) for c in vals) + (clipped,)


_bg_cache = {{}}     # (couleur, legal) -> (Y, U, V, clipped) — conversion faite UNE FOIS


def _bg_yuv(color, legal):
    key = (str(color), bool(legal))
    got = _bg_cache.get(key)
    if got is None:
        got = _rgb_to_yuv(color, legal)
        _bg_cache[key] = got
    return got


# Défaut de dégradé : noir → blanc horizontal, dégradé progressif (douceur 1).
GRAD_STOPS_MIN, GRAD_STOPS_MAX = 2, 4
_GRAD_DEFAULT = {{"angle": 0.0, "softness": 1.0,
                  "stops": [{{"color": "#000000", "pos": 0.0}}, {{"color": "#ffffff", "pos": 1.0}}]}}


def _norm_gradient(g):
    """Normalise un dégradé : angle 0-360, douceur 0-1 (0 = wipe net), 2 à 4 arrêts (couleur hex +
    position 0-1), TRIÉS par position. Défauts = le dégradé par défaut ci-dessus."""
    g = dict(g or {{}})
    raw = g.get("stops")
    if not isinstance(raw, list) or len(raw) < GRAD_STOPS_MIN:
        raw = [dict(s) for s in _GRAD_DEFAULT["stops"]]
    stops = []
    for s in raw[:GRAD_STOPS_MAX]:
        s = dict(s or {{}})
        stops.append({{"color": str(s.get("color") or "#000000"),
                       "pos": _f(s.get("pos", 0.0), 0.0, 1.0, 0.0)}})
    stops.sort(key=lambda x: x["pos"])
    return {{"angle": _f(g.get("angle", 0.0), 0.0, 360.0, 0.0),
             "softness": _f(g.get("softness", 1.0), 0.0, 1.0, 1.0),
             "stops": stops}}


def _norm_bg(bg):
    """Normalise le fond : mode + couleur (uni) + dégradé + plage. Défaut = `none` (noir)."""
    bg = dict(bg or {{}})
    mode = str(bg.get("mode") or "none").strip().lower()
    if mode not in BG_MODES:
        mode = "none"
    lg = bg.get("legal", True)
    return {{"mode": mode, "color": str(bg.get("color") or "#000000"),
             "legal": (lg if isinstance(lg, bool)
                       else str(lg).strip().lower() in ("1", "true", "yes", "on")),
             "gradient": _norm_gradient(bg.get("gradient"))}}


def _bg_public(bg):
    """État fond pour /state — conversion effective ET drapeau d'écrêtage (jamais muet)."""
    y, u, v, clipped = _bg_yuv(bg["color"], bg["legal"])
    gclip = False
    if bg["mode"] == "gradient":
        for s in bg["gradient"]["stops"]:
            gclip = gclip or _bg_yuv(s["color"], bg["legal"])[3]
    return dict(bg) | {{"yuv": [y, u, v],
                        "clipped": bool((clipped and bg["mode"] == "color") or gclip)}}


def _bg_values(bg):
    """(Y, U, V) de remplissage : couleur choisie, sinon NOIR — valeurs historiques exactes."""
    if bg["mode"] == "color":
        y, u, v, _c = _bg_yuv(bg["color"], bg["legal"])
        return y, u, v
    return 16 * _SCALE, _NEUTRAL, _NEUTRAL


def _interp_bg(fr, to, p):
    """Transition de fond : deux couleurs → FONDU en YUV. Modes différents (source → couleur) →
    bascule à mi-course : il n'existe pas d'entre-deux honnête entre une vidéo et une couleur
    (le fondu vidéo↔couleur demanderait de mélanger le grain source par trame — c'est le travail
    du mixer, pas du split)."""
    if fr["mode"] == "color" and to["mode"] == "color":
        return {{"mode": "color", "legal": to["legal"],
                 "color": _lerp_hex(fr["color"], to["color"], p)}}
    if fr["mode"] == "gradient" and to["mode"] == "gradient":
        # ★ ANIMATION GRATUITE : deux mémoires gradient → un WIPE/dégradé ANIMÉ (angle, douceur,
        # couleurs et positions tous interpolés). Aucune mécanique nouvelle : c'est la transition
        # de fond qui existe depuis la 0.12.0, étendue à tous les champs du dégradé.
        return {{"mode": "gradient", "legal": to["legal"],
                 "gradient": _interp_gradient(fr["gradient"], to["gradient"], p)}}
    return dict(to if p >= 0.5 else fr)


# ═════════════════════════════════════════════════════════════════════════════════════════════
#  FOND DÉGRADÉ (0.13.0) — mélange de couleurs + WIPE, le même objet
#
#  Un dégradé = liste d'arrêts (couleur hex + position) + un ANGLE + une DOUCEUR. La douceur
#  unifie mélange et wipe : 0 = frontière NETTE (wipe), 1 = dégradé progressif, entre = molle.
#  Chaque arrêt est converti par `_rgb_to_yuv` (LA conversion du plugin : colorimétrie DÉCLARÉE,
#  profondeur courante, plage légale — aucune matrice en dur). L'interpolation entre arrêts se
#  fait EN YUV à la profondeur courante, dans le noyau C (mvk_spl_gradient) — repli numpy IDENTIQUE.
#
#  ★ COÛT : une valeur PAR PIXEL → passe plein cadre. STATIQUE, l'empreinte (axe + arrêts YUV) est
#  cachée et seul le remplissage tourne ; ANIMÉ (wipe qui balaie pendant une transition), il est
#  refait à chaque trame — d'où le C. Mesuré : cf. help.md.
#
#  ★ BANDING : un dégradé plat en 8 bits montre des BANDES (défaut classique, visible à l'antenne).
#  Un TRAMAGE ordonné (Bayer 8×8, ±½ LSB) est ajouté dans le noyau avant l'arrondi. Amplitude
#  constante en LSB (±0,49 code) : franc en 8 bits, négligeable en 10/12 (c'est voulu).
GRAD_AMP = 1.0
_BAYER8 = [[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
           [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
           [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
           [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]]
# Bayer CENTRÉ sur 0 (±0,492) et en float32 — MÊME table pour le kernel (aplatie) et le repli.
_BAYER = ((np.array(_BAYER8, dtype=np.float32) - np.float32(31.5)) / np.float32(64.0))
_BAYER_FLAT = np.ascontiguousarray(_BAYER.ravel(), dtype=np.float32)


def _f32r(x):
    """Arrondi float32 d'un scalaire : les paramètres d'axe passent au kernel en c_float (float32)
    ET au repli numpy — on les fige donc en float32 des DEUX côtés, sinon kernel ≢ repli."""
    return float(np.float32(x))


_grad_cache = {{}}   # clé config → empreinte (axe + arrêts YUV) — précalcul fait UNE FOIS


def _grad_prep(bg):
    """Empreinte d'un dégradé (cachée) : axe (cos/sin/pmin/inv_range float32), arrêts YUV float32
    à la profondeur/colorimétrie, douceur, table Bayer. Le calcul par trame ne fait QUE le
    remplissage — l'empreinte ne bouge qu'au changement de réglage."""
    g = bg["gradient"]; legal = bg["legal"]
    key = (round(g["angle"], 4), round(g["softness"], 5), legal,
           tuple((s["color"], round(s["pos"], 5)) for s in g["stops"]))
    got = _grad_cache.get(key)
    if got is not None:
        return got
    th = math.radians(g["angle"])
    cos = _f32r(math.cos(th)); sin = _f32r(math.sin(th))
    # normalisation [0,1] sur le cadre : min/max de la projection AUX 4 COINS
    projs = [x * cos + y * sin for x in (0.0, float(WIDTH)) for y in (0.0, float(HEIGHT))]
    pmin = _f32r(min(projs)); pmax = _f32r(max(projs))
    inv = _f32r(1.0 / (pmax - pmin)) if pmax > pmin else _f32r(0.0)
    pos = np.array([s["pos"] for s in g["stops"]], dtype=np.float32)
    ys, us, vs = [], [], []
    for s in g["stops"]:
        y, u, v, _c = _bg_yuv(s["color"], legal)
        ys.append(y); us.append(u); vs.append(v)
    gp = {{"key": key, "axis": (cos, sin, pmin, inv),
           "pos": np.ascontiguousarray(pos, dtype=np.float32),
           "yv": np.ascontiguousarray(ys, dtype=np.float32),
           "uv": np.ascontiguousarray(us, dtype=np.float32),
           "vv": np.ascontiguousarray(vs, dtype=np.float32),
           "softness": _f32r(g["softness"])}}
    _grad_cache[key] = gp
    return gp


def _grad_eval_np(t, pos, yv, uv, vv, soft):
    """REPLI numpy — reproduit `grad_at` du kernel TERME À TERME, en float64 (le kernel calcule en
    double, la promotion float32→double est exacte → mêmes octets). t float64 (h,w)."""
    n = pos.shape[0]
    p = pos.astype(np.float64); Yv = yv.astype(np.float64); Uv = uv.astype(np.float64); Vv = vv.astype(np.float64)
    Y = np.full(t.shape, Yv[n - 1]); U = np.full(t.shape, Uv[n - 1]); V = np.full(t.shape, Vv[n - 1])
    for i in range(n - 1):
        seg = p[i + 1] - p[i]
        local = (t - p[i]) / seg if seg > 1e-9 else np.zeros_like(t)
        if soft <= 1e-6:
            w = np.where(local < 0.5, 0.0, 1.0)
        else:
            w = np.clip((local - 0.5) / soft + 0.5, 0.0, 1.0)
        iw = 1.0 - w
        m = (t >= p[i]) & (t < p[i + 1])
        Y[m] = (Yv[i] * iw + Yv[i + 1] * w)[m]
        U[m] = (Uv[i] * iw + Uv[i + 1] * w)[m]
        V[m] = (Vv[i] * iw + Vv[i + 1] * w)[m]
    le0 = t <= p[0]; ge1 = t >= p[n - 1]              # bornes : recouvrent les segments
    for A, Av in ((Y, Yv), (U, Uv), (V, Vv)):
        A[le0] = Av[0]; A[ge1] = Av[n - 1]
    return Y, U, V


def _bg_gradient_np(oy, ou, ov, gp, b0, b1):
    """REPLI numpy du remplissage dégradé, bande [b0,b1) — bit-exact au kernel (double = float64,
    même ordre, tramage Bayer identique)."""
    cos, sin, pmin, inv = gp["axis"]
    soft = gp["softness"]
    cos = np.float64(cos); sin = np.float64(sin); pmin = np.float64(pmin); inv = np.float64(inv)
    soft = float(np.float64(soft)); amp = np.float64(GRAD_AMP)
    bay = _BAYER.astype(np.float64)
    # LUMA
    ys = np.arange(b0, b1, dtype=np.float64)[:, None]
    xs = np.arange(WIDTH, dtype=np.float64)[None, :]
    py = (ys + 0.5) * sin
    t = np.clip(((xs + 0.5) * cos + py - pmin) * inv, 0.0, 1.0)
    Y, _u, _v = _grad_eval_np(t, gp["pos"], gp["yv"], gp["uv"], gp["vv"], soft)
    dy = amp * bay[(np.arange(b0, b1)[:, None] & 7), (np.arange(WIDTH)[None, :] & 7)]
    oy[b0:b1] = np.clip(np.floor(Y + dy + 0.5), 0, _MAXV).astype(_NP_DT)
    # CHROMA
    cb0, cb1 = b0 // _CH, b1 // _CH
    cy = np.arange(cb0, cb1, dtype=np.float64)[:, None]
    cx = np.arange(UV_W, dtype=np.float64)[None, :]
    ly = cy * _CH + _CH * 0.5; lx = cx * _CW + _CW * 0.5
    t2 = np.clip((lx * cos + ly * sin - pmin) * inv, 0.0, 1.0)
    _y, U, V = _grad_eval_np(t2, gp["pos"], gp["yv"], gp["uv"], gp["vv"], soft)
    dc = amp * bay[(np.arange(cb0, cb1)[:, None] & 7), (np.arange(UV_W)[None, :] & 7)]
    ou[cb0:cb1] = np.clip(np.floor(U + dc + 0.5), 0, _MAXV).astype(_NP_DT)
    ov[cb0:cb1] = np.clip(np.floor(V + dc + 0.5), 0, _MAXV).astype(_NP_DT)


_grad_render = OrderedDict()   # clé dégradé → (Y, U, V) plein cadre — rendu UNE FOIS
_GRAD_RENDER_MAX = 6           # borne (une transition génère un dégradé distinct/trame)


def _grad_planes(gp):
    """Rend le dégradé PLEIN CADRE une fois (kernel mvk, sinon repli numpy IDENTIQUE) et le CACHE.
    ★ STATIQUE → rendu une fois puis simple recopie (≈ fond uni). ANIMÉ (wipe en transition) →
    la clé change à chaque trame ⇒ rendu par trame (le C, mesuré ~20 ms en 1080p) — c'est le prix
    d'un balayage animé plein cadre, DIT dans help.md. Cache borné (transition = churn)."""
    key = gp["key"]
    got = _grad_render.get(key)
    if got is not None:
        _grad_render.move_to_end(key)
        return got
    Y = np.empty((HEIGHT, WIDTH), _NP_DT)
    U = np.empty((UV_H, UV_W), _NP_DT); V = np.empty((UV_H, UV_W), _NP_DT)
    cos, sin, pmin, inv = gp["axis"]
    if not _mvk_try("gradient", bobimxl.mvk_spl_gradient_into, Y, U, V, WIDTH, HEIGHT,
                    _CW, _CH, 0, HEIGHT, cos, sin, pmin, inv,
                    gp["pos"], gp["yv"], gp["uv"], gp["vv"], gp["softness"], _MAXV,
                    GRAD_AMP, _BAYER_FLAT):
        _bg_gradient_np(Y, U, V, gp, 0, HEIGHT)
    _grad_render[key] = (Y, U, V)
    while len(_grad_render) > _GRAD_RENDER_MAX:
        _grad_render.popitem(last=False)
    return Y, U, V


def _bg_fill_band(oy, ou, ov, bgfill, b0, b1):
    """Remplit la bande [b0,b1) du fond : couleur UNIE (3 memset, octet-identique) ou DÉGRADÉ
    (rendu caché → recopie de bande). Une seule voie pour le mode tranche et la trame entière
    (b0=0,b1=HEIGHT) : les deux ne peuvent pas diverger."""
    kind, data = bgfill
    cb0, cb1 = b0 // _CH, b1 // _CH
    if kind == "solid":
        y, u, v = data
        oy[b0:b1] = y; ou[cb0:cb1] = u; ov[cb0:cb1] = v
        return
    Y, U, V = _grad_planes(data)
    oy[b0:b1] = Y[b0:b1]; ou[cb0:cb1] = U[cb0:cb1]; ov[cb0:cb1] = V[cb0:cb1]


def _interp_gradient(fr, to, p):
    """Interpole DEUX dégradés (tous les champs : angle, douceur, chaque arrêt = couleur + pos) —
    c'est ce qui rend le WIPE ANIMÉ possible sans mécanique nouvelle. Nombre d'arrêts différent →
    bascule à mi-course (pas d'appariement honnête entre 2 et 4 arrêts)."""
    fs, ts = fr["stops"], to["stops"]
    if len(fs) != len(ts):
        return dict(to if p >= 0.5 else fr)
    return {{"angle": _lerp(fr["angle"], to["angle"], p),
             "softness": _lerp(fr["softness"], to["softness"], p),
             "stops": [{{"color": _lerp_hex(a["color"], b["color"], p),
                         "pos": _lerp(a["pos"], b["pos"], p)}}
                        for a, b in zip(fs, ts)]}}


def _norm_global(g):
    """Normalise le canal Global (défauts = NEUTRE : le rendu est alors STRICTEMENT celui d'avant)."""
    g = dict(g or {{}})
    return {{
        "dx":       _f(g.get("dx", 0.0), -2.0, 2.0, 0.0),        # décalage, en largeurs de cadre
        "dy":       _f(g.get("dy", 0.0), -2.0, 2.0, 0.0),        # décalage, en hauteurs de cadre
        "scale":    _f(g.get("scale", 1.0), 0.05, 4.0, 1.0),     # échelle du groupe
        "rotation": _f(g.get("rotation", 0.0), -180.0, 180.0, 0.0),
        "opacity":  _f(g.get("opacity", 100.0), 0.0, 100.0, 100.0),   # POURCENT (cf. OPACITÉ)
    }}


def _g_neutral(g):
    """Le Global est-il NEUTRE ? Oui → aucune composition, chemin d'avant OCTET-IDENTIQUE."""
    return (abs(g["dx"]) < 1e-9 and abs(g["dy"]) < 1e-9
            and abs(g["scale"] - 1.0) < 1e-9 and abs(g["rotation"]) < 1e-9
            and g["opacity"] >= 99.9999)


def _apply_global(b, g):
    """Compose le Global SUR une box : rendu = Global ∘ box. Renvoie une NOUVELLE box.

    La transformation opère autour du CENTRE DU CADRE, en pixels (et non en coordonnées
    normalisées) — sinon une rotation de groupe cisaillerait la scène dès que le cadre n'est
    pas carré (16:9 : une rotation en coordonnées normalisées transforme un carré en losange).
    L'ordre est : échelle, puis rotation, puis décalage — celui qu'attend un opérateur
    (« je grossis le groupe, je l'incline, je le sors du cadre »).
    Le SENS de rotation est celui d'une box (même matrice) → un groupe tourné et une box tournée
    penchent du même côté."""
    if _g_neutral(g):
        return b
    cx, cy = WIDTH / 2.0, HEIGHT / 2.0
    vx = b["x"] * WIDTH - cx
    vy = b["y"] * HEIGHT - cy
    s = float(g["scale"])
    th = math.radians(float(g["rotation"]))
    c, sn = math.cos(th), math.sin(th)
    rx = (vx * c - vy * sn) * s
    ry = (vx * sn + vy * c) * s
    nb = dict(b)
    nb["x"] = (cx + rx) / WIDTH + float(g["dx"])
    nb["y"] = (cy + ry) / HEIGHT + float(g["dy"])
    nb["size"] = b.get("size", 0.45) * s
    if b.get("size_x"): nb["size_x"] = b["size_x"] * s
    if b.get("size_y"): nb["size_y"] = b["size_y"] * s
    nb["rotation"] = b.get("rotation", 0.0) + float(g["rotation"])
    nb["opacity"] = b.get("opacity", 100.0) * float(g["opacity"]) / 100.0   # % ∘ % = %
    return _norm_box(nb)


def _interp_global(fr, to, p):
    """Interpole le Global d'une transition (comme une box : sinon il SAUTERAIT en plein
    mouvement — c'est justement là qu'il sert le plus : entrées/sorties de champ de groupe)."""
    return {{k: _lerp(float(fr.get(k, GLOBAL_NEUTRAL[k])), float(to.get(k, GLOBAL_NEUTRAL[k])), p)
             for k in GLOBAL_NEUTRAL}}

def _norm_box(b):
    """Normalise une box, défauts compris.

    Deux étages :
    — SOCLE (historique) : enabled/x/y/size/crop/border{{w,color}} — inchangé, mêmes bornes.
    — MODE AVANCÉ (0.9.0) : rotation, opacité, adoucissement (feather), coins arrondis, miroir,
      ratio libre, ombre portée, bordure 3D (biseau + éclairage), z-ordre libre. TOUS les
      réglages avancés sont NEUTRES par défaut → `_is_adv()` est faux → le compositing emprunte
      le chemin historique OCTET-IDENTIQUE (aucun surcoût pour qui ne s'en sert pas).
    """
    b = dict(b or {{}})
    crop = dict(b.get("crop") or {{}})
    bd = dict(b.get("border") or {{}})
    sh = dict(b.get("shadow") or {{}})
    try:
        bw = max(0, min(40, int(float(bd.get("w", 0) or 0))))
    except Exception:
        bw = 0
    return {{
        "enabled": bool(b.get("enabled", False)),
        # ★ HORS CADRE (0.12.0) : x/y ne sont plus bornés à [0,1] mais à [POS_MIN, POS_MAX]
        # (= −1..2 : une box peut être entièrement sortie du cadre, d'un côté comme de l'autre).
        # SANS ÇA, AUCUNE ENTRÉE NI SORTIE DE CHAMP N'EST POSSIBLE — donc pas d'animation de
        # groupe. Le rendu était déjà correct pour une box à moitié dehors (rect visible clippé
        # au cadre) : c'est la BORNE qui l'interdisait. Les bornes sont publiées dans /state.caps
        # (l'UI ne code rien en dur).
        "x":    _f(b.get("x", 0.5), POS_MIN, POS_MAX, 0.5),
        "y":    _f(b.get("y", 0.5), POS_MIN, POS_MAX, 0.5),
        "size": _f(b.get("size", 0.45), 0.05, 1.0, 0.45),
        # ★ ROGNAGE ARMÉ (0.15.0) : interrupteur SÉPARÉ des quatre valeurs, comme sur un
        # pupitre ATEM (bouton CROP). Désarmé, les valeurs sont CONSERVÉES telles quelles et
        # simplement mises en veille — les réarmer les restitue. Le rendu ne lit jamais
        # `crop` en direct mais `_eff_crop(box)`, qui rend des zéros quand c'est désarmé :
        # une seule règle, un seul endroit. Sans cet interrupteur, le bouton CROP du pupitre
        # ne pouvait qu'échouer (rien où l'écrire, notre retour d'état le remettait à OFF).
        # DÉFAUT rétrocompatible : absent du dict → armé SI un rognage est déjà posé (les
        # compositions existantes rendent à l'identique), désarmé si tout est à zéro.
        "crop_enabled": bool(b.get("crop_enabled",
                                   any(_f(crop.get(k, 0.0), 0.0, 0.9, 0.0) > 0
                                       for k in ("t", "b", "l", "r")))),
        "crop": {{
            "t": _f(crop.get("t", 0.0), 0.0, 0.9, 0.0),
            "b": _f(crop.get("b", 0.0), 0.0, 0.9, 0.0),
            "l": _f(crop.get("l", 0.0), 0.0, 0.9, 0.0),
            "r": _f(crop.get("r", 0.0), 0.0, 0.9, 0.0),
        }},
        # ── bordure : socle (w/color) + étage 3D (ATEM : intérieur/extérieur, biseau, lumière) ──
        # ★ BORDURE ARMÉE (0.16.0) : interrupteur séparé des épaisseurs, comme le bouton
        # BORDER du pupitre ATEM. Désarmée, les épaisseurs sont CONSERVÉES et simplement mises
        # en veille — les réarmer les restitue. Même patron que `crop_enabled` (0.15.0), et
        # pour la même raison : mettre les épaisseurs à zéro pour « éteindre » détruirait les
        # réglages de l'opérateur sans rien pour les rétablir.
        # DÉFAUT rétrocompatible : absent → armé SI une épaisseur est déjà posée.
        "border_enabled": bool(b.get("border_enabled",
                                     bw > 0
                                     or _f(bd.get("outer", 0), 0.0, 40.0, 0.0) > 0
                                     or _f(bd.get("inner", 0), 0.0, 40.0, 0.0) > 0)),
        "border": {{
            "w":       bw,                                   # socle : trait plat (compat 0.8.0)
            "color":   str(bd.get("color") or "#ffffff"),
            "outer":   _f(bd.get("outer", 0), 0.0, 40.0, 0.0),   # largeur EXTÉRIEURE (px)
            "inner":   _f(bd.get("inner", 0), 0.0, 40.0, 0.0),   # largeur INTÉRIEURE (px)
            "bevel":   _f(bd.get("bevel", 0), -1.0, 1.0, 0.0),   # <0 rentrant, >0 sortant
            "bevel_soft": _f(bd.get("bevel_soft", 0.3), 0.0, 1.0, 0.3),   # adoucissement
            "bevel_pos":  _f(bd.get("bevel_pos", 0.5), 0.0, 1.0, 0.5),    # position du biseau
        }},
        # ── mode avancé ──
        "rotation": _f(b.get("rotation", 0.0), -180.0, 180.0, 0.0),   # degrés
        "opacity":  _f(b.get("opacity", 100.0), 0.0, 100.0, 100.0),   # POURCENT (0-100)
        "feather":  _f(b.get("feather", 0.0), 0.0, 64.0, 0.0),        # px (adoucissement bords)
        "radius":   _f(b.get("radius", 0.0), 0.0, 300.0, 0.0),        # px (coins arrondis)
        "mirror_h": bool(b.get("mirror_h", False)),
        "mirror_v": bool(b.get("mirror_v", False)),
        # ratio libre : aspect="free" → size_x/size_y indépendants (l'ATEM impose "keep")
        "aspect": ("free" if str(b.get("aspect") or "keep") == "free" else "keep"),
        "size_x": _f(b.get("size_x", 0.0), 0.0, 1.0, 0.0),   # 0 = suit `size`
        "size_y": _f(b.get("size_y", 0.0), 0.0, 1.0, 0.0),
        "shadow": {{
            "enabled": bool(sh.get("enabled", False)),
            "dx":      _f(sh.get("dx", 8), -100.0, 100.0, 8.0),
            "dy":      _f(sh.get("dy", 8), -100.0, 100.0, 8.0),
            "blur":    _f(sh.get("blur", 8), 0.0, 64.0, 8.0),
            "opacity": _f(sh.get("opacity", 50.0), 0.0, 100.0, 50.0),   # POURCENT (0-100)
            "color":   str(sh.get("color") or "#000000"),
        }},
        # z-ordre LIBRE (l'ATEM fige 1→2→3→4). Défaut None → l'index de la box fait foi.
        "z": (None if b.get("z") is None else _f(b.get("z"), -99.0, 99.0, 0.0)),
    }}


_NO_CROP = {{"t": 0.0, "b": 0.0, "l": 0.0, "r": 0.0}}


def _eff_crop(b):
    """Rognage EFFECTIF d'une box : ses quatre valeurs si le rognage est armé, zéro sinon.
    ★ Le rendu ne doit JAMAIS lire `b["crop"]` en direct — sinon un rognage désarmé
    continuerait de rogner. Un seul point de vérité, ici. Les valeurs stockées ne sont
    jamais effacées par le désarmement : réarmer les restitue telles quelles."""
    if not b.get("crop_enabled", True):
        return _NO_CROP
    return b.get("crop") or _NO_CROP


_NO_BORDER = {{"w": 0, "color": "#ffffff", "outer": 0.0, "inner": 0.0,
              "bevel": 0.0, "bevel_soft": 0.3, "bevel_pos": 0.5}}


def _eff_border(b):
    """Bordure EFFECTIVE : celle de la box si elle est armée, aucune épaisseur sinon.
    ★ Le rendu ne lit JAMAIS `b["border"]` en direct — sinon une bordure désarmée
    continuerait de s'afficher. Les épaisseurs stockées ne sont jamais effacées."""
    if not b.get("border_enabled", True):
        return _NO_BORDER
    return b.get("border") or _NO_BORDER


def _is_adv(b):
    """La box demande-t-elle le compositing AVANCÉ ? Faux → chemin historique (octet-identique).
    Garde-fou de perf : un réglage neutre ne doit RIEN coûter."""
    bd = _eff_border(b)
    sh = b.get("shadow") or {{}}
    return bool(
        abs(b.get("rotation", 0.0)) > 1e-6
        or b.get("opacity", 100.0) < 99.9
        or b.get("feather", 0.0) > 0.0
        or b.get("radius", 0.0) > 0.0
        or b.get("mirror_h") or b.get("mirror_v")
        or (b.get("aspect") == "free" and (b.get("size_x") or b.get("size_y")))
        or (sh.get("enabled") and sh.get("opacity", 0.0) > 0.0)
        or bd.get("outer", 0.0) > 0.0 or bd.get("inner", 0.0) > 0.0
        or abs(bd.get("bevel", 0.0)) > 1e-6
    )

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
_init_global = _norm_global(dict(CONFIG.get("global") or {{}}) | (
    {{"opacity": _mig_op((CONFIG.get("global") or {{}}).get("opacity", 1.0), 100.0)}}
    if (CONFIG.get("global") or {{}}).get("opacity") is not None else {{}}))
_init_bg = _norm_bg(CONFIG.get("bg"))
# Rétro-compat : un fond CÂBLÉ (bg_input) sans mode explicite = mode `source` (comportement d'avant).
if not CONFIG.get("bg") and _INIT_SLOT_SHM[4]:
    _init_bg["mode"] = "source"
LATENCY_MODES = ("fixed_1", "fastest")


def _latency_mode(v):
    v = str(v or "").strip().lower()
    return v if v in LATENCY_MODES else "fixed_1"


state = {{
    "inputs":     _init_inputs,
    "boxes":      _init_boxes[:N_BOXES],
    # CANAL GLOBAL : calque de transformation composé SUR chaque box (cf. _apply_global).
    "global":     _init_global,
    # FOND : none (noir) | source (entrée vidéo) | color (couleur unie).
    "bg":         _init_bg,
    # transition : None or {{start, duration_ms, from:[boxes], to:[boxes], gfrom, gto, bgfrom,
    #                        bgto, curve, stagger_ms}}
    "transition": None,
    # DÉLAI (0.24.4) : "fixed_1" (défaut) = lire l'image du créneau PRÉCÉDENT de la grille — délai
    # constant d'une image, toutes les sources arrivent à temps ; "fastest" = lire l'image du
    # créneau COURANT, attente bornée de sa 1ʳᵉ tranche, une source en retard passe avec sa
    # dernière image (comptée). Réglable à chaud (/latency), persisté (hooks.sync_state).
    "latency_mode": _latency_mode(CONFIG.get("latency_mode")),
}}

# ─── SOURCE DE LUMIÈRE (globale) : direction + hauteur. C'est elle qui donne le RELIEF aux
# bordures biseautées (un cadre plat ≠ un cadre 3D). Réglage unique pour toutes les boxes
# (comme l'ATEM : une seule lumière éclaire la scène — 4 lumières incohérentes = décor faux).
_lc = CONFIG.get("light") or {{}}
LIGHT = {{"angle":    _f(_lc.get("angle", 36.0), 0.0, 360.0, 36.0),      # direction (°)
          "altitude": _f(_lc.get("altitude", 25.0), 0.0, 90.0, 25.0)}}   # hauteur (°)

# ─── Réglages de transition par défaut (surchargeables par /recall) ───
EASINGS = ("linear", "ease_in", "ease_out", "ease_in_out",
           "cubic_in", "cubic_out", "cubic_in_out", "sine_in_out", "expo_out")
_tc = CONFIG.get("transition") or {{}}
TRANS = {{"curve":      (str(_tc.get("curve") or "ease_in_out")),
          "stagger_ms": _f(_tc.get("stagger_ms", 0), 0.0, 2000.0, 0.0)}}
if TRANS["curve"] not in EASINGS:
    TRANS["curve"] = "ease_in_out"

metrics_lock = threading.Lock()
metrics = {{"fps": 0.0, "frame_index": 0, "inputs_latency_ms": {{}}, "own_latency_ms": None, "inputs_skew_ms": {{}},
           "inputs_late": [], "plugin_version": PLUGIN_VERSION, "variant": "v2",
           # Noyau C : actif ? combien de threads ? et surtout — combien de REPLIS numpy
           # (doit rester à 0 : un repli = 5-20× plus lent, et il ne doit JAMAIS être muet).
           "mvk": _MVK,
           "mvk_threads": (getattr(bobimxl, "mvk_threads", lambda: 0)() if _MVK else 0),
           "mvk_miss": dict(_mvk_miss), "mvk_why": {{}}}}

bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    log("SIGBUS reçu — réouverture des flux MXL", "warning")
    bus_error.set()
signal.signal(signal.SIGBUS, _handle_sigbus)


# ─── HTTP servers : metrics (8080) + control (8082) ─────────
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with metrics_lock: payload = dict(metrics)
        # Bouches-trous amont ÉCARTÉS (cf. bobimxl.MXL_GRAIN_FLAG_INVALID) : seul témoin du
        # comblement d'un trou par le producteur — cadence, index et statut de lecture restent
        # tous parfaits pendant l'épisode. Calculé à la requête : aucun coût par trame.
        payload["invalid_grains"] = bobimxl.invalid_grains_report()
        # Avance des index émis sur la grille TAI (bobimxl.index_report, grid_ms en ms signées, positif =
        # futur) : seul témoin d'une sortie écrite dans le futur, la cadence restant parfaite.
        payload["index_ahead"] = bobimxl.index_report()
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
                    "mxl_lib":    _mxl_lib_state(),
                    # ── CONTRAT UI (0.9.0) ────────────────────────────────────────────────
                    # Tout ce que l'UI doit connaître est publié ici : réglages courants ET
                    # domaine des valeurs (`caps`) → l'UI n'a RIEN à coder en dur.
                    "light":      dict(LIGHT),
                    # Format des COULEURS : matrice réellement utilisée, et si elle est SUPPOSÉE
                    # (colorimetry absente du format déclaré) — l'UI le dit, ce n'est pas muet.
                    "colorimetry": COLORIMETRY,
                    "colorimetry_assumed": COLORIMETRY_ASSUMED,
                    "global":     dict(state["global"]),      # calque de transformation du GROUPE
                    "bg":         _bg_public(state["bg"]),    # fond : mode + couleur + écrêtage
                    # NB : `transition` (plus haut) reste le BOOLÉEN historique « une transition
                    # est en cours » — ne pas le repeupler avec les réglages, des consommateurs
                    # le testent comme un drapeau. Les réglages vivent sous `transition_defaults`.
                    "transition_defaults": dict(TRANS),
                    "latency_mode": state["latency_mode"],
                    "art":        _art_public(),
                    "caps": {{
                        "easings": list(EASINGS),
                        "latency_modes": list(LATENCY_MODES),
                        # Bornes des positions : le HORS CADRE est autorisé (entrées/sorties de
                        # champ). L'UI lit ces bornes — elle n'en code aucune.
                        "pos": [POS_MIN, POS_MAX],
                        "bg_modes": list(BG_MODES),
                        # Fond DÉGRADÉ : bornes des champs (l'UI ne code rien en dur).
                        "gradient_fields": {{
                            "angle": [0.0, 360.0, 0.0], "softness": [0.0, 1.0, 1.0],
                            "pos": [0.0, 1.0, 0.0],
                        }},
                        "gradient_stops": [GRAD_STOPS_MIN, GRAD_STOPS_MAX],
                        # Canal GLOBAL : champ → [min, max, défaut]
                        "global_fields": {{
                            "dx": [-2.0, 2.0, 0.0], "dy": [-2.0, 2.0, 0.0],
                            "scale": [0.05, 4.0, 1.0], "rotation": [-180.0, 180.0, 0.0],
                            "opacity": [0.0, 100.0, 100.0],
                        }},
                        # Habillage (ART) : mêmes bornes publiées que le reste (rien en dur côté UI)
                        "art_fields": {{
                            "clip": [0.0, 1.0, 0.15], "gain": [0.01, 16.0, 1.0],
                            "opacity": [0.0, 100.0, 100.0],
                        }},
                        # ★ L'opacité est en POURCENT (0-100) — unité UNIQUE moteur+UI (0.12.0).
                        "opacity_unit": "pct",
                        "art_keys": ["alpha", "luma", "chroma", "none"],
                        "box_fields": {{
                            # champ : [min, max, défaut]
                            # HORS CADRE : bornes réelles du moteur (l'UI les lit, cf. caps.pos)
                            "x": [POS_MIN, POS_MAX, 0.5], "y": [POS_MIN, POS_MAX, 0.5],
                            "size": [0.05, 1.0, 0.45],
                            "rotation": [-180.0, 180.0, 0.0],
                            "opacity": [0.0, 100.0, 100.0],
                            "feather": [0.0, 64.0, 0.0],
                            "radius": [0.0, 300.0, 0.0],
                            "size_x": [0.0, 1.0, 0.0], "size_y": [0.0, 1.0, 0.0],
                            "z": [-99.0, 99.0, None],
                        }},
                        "box_flags": ["enabled", "mirror_h", "mirror_v", "crop_enabled", "border_enabled"],
                        "box_enums": {{"aspect": ["keep", "free"]}},
                        "crop": [0.0, 0.9, 0.0],
                        "border_fields": {{
                            "w": [0, 40, 0], "outer": [0.0, 40.0, 0.0],
                            "inner": [0.0, 40.0, 0.0], "bevel": [-1.0, 1.0, 0.0],
                            "bevel_soft": [0.0, 1.0, 0.3], "bevel_pos": [0.0, 1.0, 0.5],
                        }},
                        "shadow_fields": {{
                            "dx": [-100.0, 100.0, 8.0], "dy": [-100.0, 100.0, 8.0],
                            "blur": [0.0, 64.0, 8.0], "opacity": [0.0, 100.0, 50.0],
                        }},
                        "light_fields": {{"angle": [0.0, 360.0, 36.0],
                                          "altitude": [0.0, 90.0, 25.0]}},
                        "stagger_ms": [0.0, 2000.0, 0.0],
                    }},
                }}
                # state_field par slot (pour la topologie / câblage générique)
                for i in range(N_BOXES):
                    s["input_%d" % (i + 1)] = state["inputs"][i]
                s["bg_input"] = state["inputs"][4]
                s["log_level"] = LOG_LEVEL   # lisible en condition de macro
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

        elif self.path == "/latency":
            # Délai À CHAUD : "fixed_1" | "fastest". Passer de fastest à fixed_1 recule la cible
            # d'un cran → UNE trame retenue (l'aval garde l'image précédente) ; l'inverse saute
            # une image. Aucune autre conséquence.
            m = str(body.get("mode") or body.get("latency_mode") or "").strip().lower()
            if m not in LATENCY_MODES:
                self._reply(400, {{"error": "mode attendu : " + " | ".join(LATENCY_MODES)}}); return
            with state_lock:
                state["latency_mode"] = m
            log("split: delay -> %s" % m, "info")
            self._reply(200, {{"ok": True, "latency_mode": m}})

        elif self.path == "/log_level":
            # Verbosité À CHAUD (pas de redéploiement) : instruction d'incident. Le niveau
            # PERSISTANT reste le champ `log_level` du config_schema ; celui-ci est volatil.
            ok = set_log_level(body.get("level") or body.get("log_level"))
            self._reply(200 if ok else 400, {{"ok": ok, "log_level": LOG_LEVEL}})

        elif self.path == "/box":
            # PATCH live d'une box (annule la transition en cours). FUSION PROFONDE : l'UI peut
            # n'envoyer QUE le champ modifié ({{idx, rotation}}, {{idx, shadow:{{blur}}}}…) — les
            # sous-dicts (crop/border/shadow) sont fusionnés, jamais remplacés. _norm_box borne
            # et complète tout le reste.
            try: idx = int(body.get("idx"))
            except Exception:
                self._reply(400, {{"error": "idx requis"}}); return
            if not (0 <= idx < N_BOXES):
                self._reply(400, {{"error": "idx hors plage"}}); return
            with state_lock:
                state["transition"] = None
                b = dict(state["boxes"][idx])
                for k, v in body.items():
                    if k == "idx":
                        continue
                    if k in ("crop", "border", "shadow") and isinstance(v, dict):
                        sub = dict(b.get(k) or {{}}); sub.update(v); b[k] = sub
                    else:
                        b[k] = v
                state["boxes"][idx] = _norm_box(b)
            self._reply(200, {{"ok": True, "box": state["boxes"][idx]}})

        elif self.path == "/global":
            # CANAL GLOBAL : calque de transformation du groupe (fusion partielle : l'UI n'envoie
            # que le champ touché). Annule la transition en cours, comme /box.
            with state_lock:
                state["transition"] = None
                g = dict(state["global"])
                for k, v in body.items():
                    g[k] = v
                state["global"] = _norm_global(g)
                out_g = dict(state["global"])
            self._reply(200, {{"ok": True, "global": out_g}})

        elif self.path == "/bg":
            # FOND : mode (none|source|color|gradient) + couleur + dégradé + plage légale. Le mode
            # `source` suppose le slot 4 câblé — on ne le décide PAS à sa place : on le DIT.
            # `gradient` = {{angle, softness, stops:[{{color,pos}}]}} (fusion : l'UI peut n'envoyer
            # qu'un champ, on complète depuis l'état courant).
            with state_lock:
                bg = dict(state["bg"])
                for k in ("mode", "color", "legal"):
                    if k in body:
                        bg[k] = body[k]
                if isinstance(body.get("gradient"), dict):
                    g = dict(bg.get("gradient") or {{}})
                    g.update(body["gradient"])
                    bg["gradient"] = g
                state["bg"] = _norm_bg(bg)
                out_bg = _bg_public(state["bg"])
                wired = bool(state["inputs"][4])
            if out_bg["mode"] == "source" and not wired:
                out_bg["warn"] = "no_source"      # fond `source` mais slot 4 non câblé → noir
            if out_bg.get("clipped"):
                out_bg["warn"] = "clipped"        # couleur hors plage légale → écrêtée (dite, pas tue)
            self._reply(200, {{"ok": True, "bg": out_bg}})

        elif self.path == "/light":
            # SOURCE DE LUMIÈRE globale (relief des bordures biseautées).
            with state_lock:
                if "angle" in body:    LIGHT["angle"] = _f(body["angle"], 0.0, 360.0, LIGHT["angle"])
                if "altitude" in body: LIGHT["altitude"] = _f(body["altitude"], 0.0, 90.0, LIGHT["altitude"])
            _stamps.clear()      # la lumière est dans la clé de cache → rebake des rubans
            self._reply(200, {{"ok": True, "light": dict(LIGHT)}})

        elif self.path == "/transition":
            # Réglages de transition par défaut : courbe d'accélération + décalage inter-boxes.
            c = str(body.get("curve") or TRANS["curve"])
            if c not in EASINGS:
                self._reply(400, {{"error": "courbe inconnue", "easings": list(EASINGS)}}); return
            TRANS["curve"] = c
            if "stagger_ms" in body:
                TRANS["stagger_ms"] = _f(body["stagger_ms"], 0.0, 2000.0, TRANS["stagger_ms"])
            self._reply(200, {{"ok": True, "transition": dict(TRANS)}})

        elif self.path == "/art":
            # COUCHE ART (habillage) : image importée (base64) + incrustation luma/chroma/alpha.
            # Le « cuisson » (décodage + clé) est faite UNE FOIS ici, hors trame.
            with _art_lock:
                for k in ("enabled", "above", "invert"):
                    if k in body: _art[k] = bool(body[k])
                if "image" in body:  _art["image"] = str(body["image"] or "")
                if "key" in body:
                    kk = str(body["key"] or "alpha")
                    _art["key"] = kk if kk in ("alpha", "luma", "chroma", "none") else "alpha"
                if "clip" in body:    _art["clip"] = _f(body["clip"], 0.0, 1.0, 0.15)
                if "gain" in body:    _art["gain"] = _f(body["gain"], 0.01, 16.0, 1.0)
                if "opacity" in body: _art["opacity"] = _f(body["opacity"], 0.0, 100.0, 100.0)
                if "chroma_key" in body: _art["chroma_key"] = str(body["chroma_key"] or "#00b140")
            _bake_art()
            with _art_lock:
                # `warn` renvoyé DÈS L'IMPORT (l'UI affiche le bandeau sans attendre un poll) :
                # image sans canal alpha + clé « alpha », et/ou habillage qui masque tout.
                self._reply(200, {{"ok": not _art["err"], "error": _art["err"],
                                   "loaded": _art.get("baked") is not None,
                                   "warn": list(_art["warn"]),
                                   "has_alpha": _art["has_alpha"], "cover": _art["cover"],
                                   "holes": _art["holes"]}})

        elif self.path == "/recall":
            # Rappel de mémoire : tween boxes courantes -> boxes cibles sur duration_ms, avec
            # COURBE d'accélération (0.8.0 : linéaire pur) et DÉCALAGE inter-boxes (stagger).
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
            curve = str(body.get("curve") or TRANS["curve"])
            if curve not in EASINGS:
                curve = TRANS["curve"]
            stagger = _f(body.get("stagger_ms", TRANS["stagger_ms"]), 0.0, 2000.0, 0.0)
            # Une mémoire porte AUSSI le canal Global et le fond : sans eux, rappeler une
            # mémoire « groupe hors champ » ou « fond bleu » ne restituerait rien.
            gto = _norm_global(body["global"]) if isinstance(body.get("global"), dict) else None
            bgto = _norm_bg(body["bg"]) if isinstance(body.get("bg"), dict) else None
            with state_lock:
                if duration <= 0:
                    state["boxes"] = tgt
                    if gto is not None: state["global"] = gto
                    if bgto is not None: state["bg"] = bgto
                    state["transition"] = None
                else:
                    state["transition"] = {{
                        "start": time.time(),
                        "duration_ms": duration,
                        "curve": curve,
                        "stagger_ms": stagger,
                        "from": [dict(b) for b in state["boxes"]],
                        "to":   tgt,
                        "gfrom": dict(state["global"]), "gto": gto,
                        "bgfrom": dict(state["bg"]), "bgto": bgto,
                    }}
            self._reply(200, {{"ok": True, "curve": curve, "stagger_ms": stagger}})

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
        log(f"input #{{idx}} câblé sur flux MXL {{wanted}}", "info")   # entrée qui apparaît = événement
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


# ─── Sources ENTRELACÉES NATIVES (modèle SDK MXL : 1 grain = 1 CHAMP, ½ hauteur) ───────────
# Le split compose sur un canvas de TRAME. Une source entrelacée livre des grains de
# V_FRAME_SIZE/2 octets : on TISSE les 2 grains-champs de la dernière trame complète (index
# PAIR = 1er champ ; tff → lignes paires, bff → impaires — convention unique du projet, cf.
# recorder/2110_io). Sans ça le grain-champ était REJETÉ (taille ≠ canvas) → entrée noire.
_IL_SRC = {{}}   # nom de flux -> "tff"/"bff" (None = progressif)

def _il_order(r):
    key = getattr(r, "name", None) or id(r)
    if key in _IL_SRC:
        return _IL_SRC[key]
    fo = None
    try:
        f = r.format()
        if f and f.get("interlaced"):
            fo = str(f.get("field_order") or "tff").lower()
    except Exception:
        pass
    _IL_SRC[key] = fo
    return fo

def _weave_fields(first_b, second_b, bff):
    """Tisse 2 grains-champs en une trame planar pleine (Y puis U puis V)."""
    planes = [(Y_SIZE // 2, WIDTH * _BPS),
              (UV_SIZE // 2, UV_W * _BPS), (UV_SIZE // 2, UV_W * _BPS)]
    out = bytearray(); off = 0
    for (psz, rb) in planes:
        a = np.frombuffer(first_b,  dtype=np.uint8, count=psz, offset=off).reshape(-1, rb)
        b = np.frombuffer(second_b, dtype=np.uint8, count=psz, offset=off).reshape(-1, rb)
        fr = np.empty((a.shape[0] * 2, rb), dtype=np.uint8)
        if bff: fr[1::2] = a; fr[0::2] = b
        else:   fr[0::2] = a; fr[1::2] = b
        out += fr.tobytes(); off += psz
    return bytes(out)

def lire_frame_yuv(r):
    """Lit la frame la plus récente du Reader. Renvoie (bytes, ts_in_ns TAI) ou (None, None).
    Garde format-fixe : un grain de taille ≠ canvas est ignoré (le composite suppose WxH/chroma/depth).
    Source ENTRELACÉE (grain = champ) → tissage des 2 champs en une trame pleine."""
    if r is None: return None, None
    try:
        got = r.get_latest()
        if got is None: return None, None
        n = len(got[2])
        if n == V_FRAME_SIZE // 2:
            fo = _il_order(r)
            if fo is None: return None, None
            fi = (got[0] - 1) // 2
            if fi < 0: return None, None
            g0 = r.get(2 * fi); g1 = r.get(2 * fi + 1)
            if (g0 is None or g1 is None
                    or len(g0[2]) != n or len(g1[2]) != n): return None, None
            return _weave_fields(bytes(g0[2]), bytes(g1[2]), fo == "bff"), r.last_write_time()
        if n != V_FRAME_SIZE: return None, None   # format ≠ canvas → ignoré
        return bytes(got[2]), r.last_write_time()
    except Exception:
        return None, None


def _read_slot(slot):
    """CHEMIN TRAME ENTIÈRE — lit le slot `slot` en NOMMANT chaque renoncement (0.11.0). Le
    chemin historique renvoyait (None, None) pour trois causes très différentes — flux absent,
    producteur muet, grain d'un AUTRE format — et l'appelant faisait `continue` sans un mot.
    Même comptabilité que le mode tranche (`_slot_skip` : compté, exposé sur :8080, loggé une
    fois, lecteur relâché/rouvert si ça dure → rattrape un producteur redéployé)."""
    rd = ensure_input(slot)
    if rd is None:
        with state_lock:
            wired = bool(state["inputs"][slot])
        _slot_skip(slot, "input_absent" if wired else "not_wired")
        return None, None
    try:
        got = rd.get_latest()
    except Exception as _e:
        _slot_skip(slot, "exception", "%s: %s" % (type(_e).__name__, _e))
        return None, None
    if got is None:
        _slot_skip(slot, "no_grain")
        return None, None
    n = len(got[2])
    if n != V_FRAME_SIZE and not (n == V_FRAME_SIZE // 2 and _il_order(rd) is not None):
        _slot_skip(slot, "format", "grain %d o ≠ canvas %d o (%dx%d %s %d bits)"
                   % (n, V_FRAME_SIZE, WIDTH, HEIGHT, CHROMA, BIT_DEPTH))
        return None, None
    f, ts = lire_frame_yuv(rd)
    if f is None:
        _slot_skip(slot, "no_grain")
        return None, None
    return f, ts


# ─── Transitions : courbes d'accélération + interpolation ────────────────────────────────────
# 0.8.0 : interpolation strictement LINÉAIRE (mouvement mécanique) et bordure NON interpolée
# (elle SAUTAIT à la cible pendant que la géométrie glissait). 0.9.0 : jeu de courbes standard
# + interpolation de TOUS les paramètres (géométrie, bordure, rotation, opacité, ombre, coins…)
# — un réglage qui n'est pas interpolé produit un saut visible en cours de transition.
def _lerp(a, b, p): return a + (b - a) * p

def _ease(p, curve):
    """Courbe d'accélération sur p∈[0,1]. Jeu volontairement restreint aux courbes utiles en
    habillage (pas de rebond/élastique : hors sujet broadcast)."""
    if p <= 0.0: return 0.0
    if p >= 1.0: return 1.0
    if curve == "ease_in":       return p * p                      # quadratique entrante
    if curve == "ease_out":      return 1.0 - (1.0 - p) ** 2       # quadratique sortante
    if curve == "ease_in_out":   return 2 * p * p if p < 0.5 else 1.0 - ((-2 * p + 2) ** 2) / 2
    if curve == "cubic_in":      return p ** 3
    if curve == "cubic_out":     return 1.0 - (1.0 - p) ** 3
    if curve == "cubic_in_out":  return 4 * p ** 3 if p < 0.5 else 1.0 - ((-2 * p + 2) ** 3) / 2
    if curve == "sine_in_out":   return -(math.cos(math.pi * p) - 1.0) / 2.0
    if curve == "expo_out":      return 1.0 - math.pow(2.0, -10.0 * p)
    return p                                                        # "linear" (défaut)


def _lerp_hex(c0, c1, p):
    """Interpolation de couleur en RGB (les couleurs de bordure/ombre glissent au lieu de sauter)."""
    def _rgb(h):
        h = str(h or "#ffffff").lstrip("#")
        if len(h) == 3: h = "".join(c * 2 for c in h)
        try: return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except Exception: return 255, 255, 255
    r0, g0, b0 = _rgb(c0); r1, g1, b1 = _rgb(c1)
    return "#%02x%02x%02x" % (int(round(_lerp(r0, r1, p))),
                              int(round(_lerp(g0, g1, p))),
                              int(round(_lerp(b0, b1, p))))


def _interp_box(fr, to, p):
    """Interpole TOUTE la box (0.9.0) + alpha du fondu d'apparition/disparition.
    Renvoie (box, alpha) ou None si la box est absente des deux côtés."""
    fe = fr["enabled"]; te = to["enabled"]
    if not fe and not te:
        return None
    if fe and te:       alpha = 1.0
    elif fe and not te: alpha = 1.0 - p   # fondu sortant
    else:               alpha = p         # fondu entrant

    fb = fr.get("border") or {{}}; tb = to.get("border") or {{}}
    fs = fr.get("shadow") or {{}}; ts = to.get("shadow") or {{}}
    box = {{
        "enabled": True,
        "x":    _lerp(fr["x"], to["x"], p),
        "y":    _lerp(fr["y"], to["y"], p),
        "size": _lerp(fr["size"], to["size"], p),
        # Le rognage s'INTERPOLE toujours entre les valeurs EFFECTIVES des deux bouts : un
        # rappel de mémoire qui arme (ou désarme) le rognage le fait donc apparaître (ou
        # disparaître) PROGRESSIVEMENT, au lieu de sauter au milieu de la transition. La box
        # tweenée est marquée armée : ses valeurs sont déjà effectives.
        "crop_enabled": True,
        "crop": {{k: _lerp(_eff_crop(fr)[k], _eff_crop(to)[k], p) for k in ("t", "b", "l", "r")}},
        # ── bordure : INTERPOLÉE (0.8.0 sautait à la cible) ──
        "border": {{
            "w":          _lerp(float(fb.get("w", 0) or 0), float(tb.get("w", 0) or 0), p),
            "color":      _lerp_hex(fb.get("color"), tb.get("color"), p),
            "outer":      _lerp(fb.get("outer", 0.0), tb.get("outer", 0.0), p),
            "inner":      _lerp(fb.get("inner", 0.0), tb.get("inner", 0.0), p),
            "bevel":      _lerp(fb.get("bevel", 0.0), tb.get("bevel", 0.0), p),
            "bevel_soft": _lerp(fb.get("bevel_soft", 0.3), tb.get("bevel_soft", 0.3), p),
            "bevel_pos":  _lerp(fb.get("bevel_pos", 0.5), tb.get("bevel_pos", 0.5), p),
        }},
        # ── mode avancé : tout est interpolé (sinon l'angle/l'opacité SAUTENT en plein mouvement) ──
        "rotation": _lerp(fr.get("rotation", 0.0), to.get("rotation", 0.0), p),
        "opacity":  _lerp(fr.get("opacity", 100.0), to.get("opacity", 100.0), p),
        "feather":  _lerp(fr.get("feather", 0.0), to.get("feather", 0.0), p),
        "radius":   _lerp(fr.get("radius", 0.0), to.get("radius", 0.0), p),
        # booléens/énums : pas d'entre-deux → bascule à mi-course (jamais de valeur bâtarde)
        "mirror_h": (to if p >= 0.5 else fr).get("mirror_h", False),
        "mirror_v": (to if p >= 0.5 else fr).get("mirror_v", False),
        "aspect":   (to if p >= 0.5 else fr).get("aspect", "keep"),
        "size_x":   _lerp(fr.get("size_x", 0.0) or fr["size"], to.get("size_x", 0.0) or to["size"], p),
        "size_y":   _lerp(fr.get("size_y", 0.0) or fr["size"], to.get("size_y", 0.0) or to["size"], p),
        "shadow": {{
            "enabled": bool(fs.get("enabled") or ts.get("enabled")),
            "dx":      _lerp(fs.get("dx", 8.0), ts.get("dx", 8.0), p),
            "dy":      _lerp(fs.get("dy", 8.0), ts.get("dy", 8.0), p),
            "blur":    _lerp(fs.get("blur", 8.0), ts.get("blur", 8.0), p),
            # une ombre qui s'allume/s'éteint fond son opacité (0 → cible) au lieu d'apparaître sec
            "opacity": _lerp(fs.get("opacity", 0.0) if fs.get("enabled") else 0.0,
                             ts.get("opacity", 0.0) if ts.get("enabled") else 0.0, p),
            "color":   _lerp_hex(fs.get("color") or "#000000", ts.get("color") or "#000000", p),
        }},
        "z": (to if p >= 0.5 else fr).get("z"),
    }}
    return box, alpha


def _box_progress(p_raw, i, curve, stagger_ms, duration_ms):
    """Progression de la box `i` : DÉCALAGE (stagger) puis courbe. Les boxes arrivent l'une
    après l'autre (i·stagger) au lieu de bouger toutes ensemble. Le décalage est compté DANS la
    durée totale : la dernière box finit à `duration_ms` (sinon un stagger allongerait la
    transition et désynchroniserait le rappel de mémoire du réalisateur)."""
    if stagger_ms <= 0 or duration_ms <= 0:
        return _ease(p_raw, curve)
    span = max(1.0, duration_ms - stagger_ms * (N_BOXES - 1))
    t = p_raw * duration_ms - i * stagger_ms
    return _ease(max(0.0, min(1.0, t / span)), curve)


# ─── Output shm ─────────────────────────────────────────────
def _ouvrir_sortie(name):
    """Writer MXL de sortie (planar). Index TOUJOURS explicite et TOUJOURS pris sur la grille TAI
    (bobimxl.Writer.next_index), quelle que soit la cadence : l'index d'un grain est une coordonnée de
    TEMPS, pas un numéro de séquence (docs/Timing.md du SDK, normatif — GrainIndex = Timestamp /
    GrainDurationNs). MODE TRANCHE : le flowDef porte slice_height →
    libmxl publie le grain en N tranches égales (commit progressif). Writer forwarde **flow_kw
    à build_flow_def. Sans slice : inchangé (1 tranche, historique octet-identique)."""
    return bobimxl.Writer(inst, name, WIDTH, HEIGHT, CHROMA, BIT_DEPTH, FPS, 1,
                          **({{"slice_height": SLICE_H}} if SLICE_ON else {{}}))

def _ecrire_sortie(w, yuv, frame_index):
    try:
        _i, gi, vw = w.open_grain(index=frame_index)
        arr = yuv if isinstance(yuv, np.ndarray) else np.frombuffer(yuv, dtype=np.uint8)
        bobimxl.blit(vw, arr)
        w.commit(gi)
    except Exception:
        pass


def _plan_layers(tr, boxes, glob=None):
    """Plan des boxes à composer, DANS L'ORDRE DE SUPERPOSITION. Renvoie [(slot, box, alpha)].

    ORDRE LIBRE (0.9.0) : l'ATEM fige 1→2→3→4 ; ici le champ `z` de chaque box l'emporte
    (défaut : l'index de la box → comportement historique à l'octet près). Tri STABLE : deux
    boxes de même z gardent leur ordre d'index.
    En transition, la box interpolée porte le z choisi (bascule à mi-course : un z ne
    s'interpole pas, il commute)."""
    # CANAL GLOBAL : replié dans la géométrie de CHAQUE box (rendu = Global ∘ box_i). Coût :
    # quelques flottants par box — AUCUNE passe supplémentaire (cf. le bloc CANAL GLOBAL).
    # En transition, il est INTERPOLÉ comme une box (sinon il sauterait en plein mouvement —
    # c'est justement là qu'il sert : entrées/sorties de champ de groupe).
    if glob is None:
        with state_lock:
            glob = dict(state["global"])
    if tr is not None and tr.get("gto") is not None:
        glob = _interp_global(tr.get("gfrom") or GLOBAL_NEUTRAL, tr["gto"],
                              _ease(tr["_p_raw"], tr.get("curve") or "linear"))
    out_plan = []
    for i in range(N_BOXES):
        if tr is not None:
            p_i = _box_progress(tr["_p_raw"], i, tr.get("curve") or "linear",
                                tr.get("stagger_ms") or 0.0, tr.get("duration_ms") or 0)
            res = _interp_box(tr["from"][i], tr["to"][i], p_i)
            if res is None:
                continue
            b_, alpha = res
        else:
            if not boxes[i]["enabled"]:
                continue
            b_, alpha = boxes[i], 1.0
        b_ = _apply_global(b_, glob)      # ← identité si le Global est neutre (octet-identique)
        out_plan.append((i, b_, alpha))
    out_plan.sort(key=lambda t: (t[1].get("z") if t[1].get("z") is not None else t[0]))
    global _n_rot
    _n_rot = sum(1 for _, b_, _a in out_plan if abs(b_.get("rotation", 0.0)) > 1e-6)
    return out_plan


def _art_above():
    with _art_lock:
        return bool(_art["enabled"] and _art.get("baked") is not None and _art["above"])


def _art_below():
    with _art_lock:
        return bool(_art["enabled"] and _art.get("baked") is not None and not _art["above"])


# ─── MODE TRANCHE : composition bandée (fond + boxes suivis par tranches) ───────────────────
# Backoff des entrées mortes : slot → [fi_bloqué, timeouts_consécutifs]. Persistant ENTRE les
# trames ; réarmé dès que la tête du slot dépasse fi_bloqué (motif multiview _sl_backoff).
_sl_backoff = {{}}
_sl_lasthead = {{}}   # slot → dernière tête vue (détection génération périmée, cadence flow)
_sl_dbg = None        # [couches, valid0, attentes, replis, dormantes] de la dernière trame
# Estampille de la sortie (mode tranche), cumuls publiés sur :8080 sous slice.stamp :
#   propagated = sortie portant l'index de son contenu (src_index) ; grid = aucune couche source
#   sur la grille (fond uni seul) → index de grille ; held = cible PAS plus récente que le dernier
#   grain publié → trame RETENUE (non publiée, l'aval garde la précédente) ; late = couche sur la
#   grille dont l'image visée n'est pas arrivée dans le budget → elle passe avec sa dernière image
#   (son contenu est plus vieux que l'étiquette) ; late_by_slot = le même, par entrée.
# ⚠ Une trame dont l'index source ne progresse pas est RETENUE, jamais publiée à « compteur+1 » :
# `next_index(src_index=s)` rend max(s, compteur+1), et ce cran d'avance ne se résorbe JAMAIS
# (chaque trame suivante refait max(s, compteur+1)) — on retomberait pour toujours dans le défaut
# corrigé ici, une trame de retard du contenu sur son étiquette. Même cas après une trame publiée
# sur la grille (fond uni), dont l'index est en avance sur toute source.
_stamp = dict(propagated=0, grid=0, held=0, late=0, late_by_slot=dict())
_stamp_last_out = -1     # dernier index PUBLIÉ (= compteur du writer : seuls ces appels le bougent)


def _grid_now():
    """Créneau COURANT de la grille TAI, SANS consommer le compteur du writer : `out.next_index()`
    le ferait avancer jusqu'à la grille, et la propagation `src_index` (qui ne recule jamais) ne
    pourrait plus rendre l'index, plus ancien, du contenu réellement lu.
    Binding d'image trop ancien (cf. _StageDelayNulle) → `next_index()`, le comportement de 0.24.1."""
    if not hasattr(bobimxl, "current_index"):
        return out.next_index()
    return bobimxl.current_index(FPS, 1)


# SAUT D'HORLOGE (cf. le mélangeur, vécu le 2026-10-25 sur dl360-1 : ~149 s en avant puis retour) :
# un recul de la cible de plus de CLOCK_JUMP_SLOTS sous le dernier numéro publié n'est pas une
# gigue. Retenir attendrait que l'horloge rattrape ce numéro « futur » — sortie figée. On repart
# sur une NOUVELLE GÉNÉRATION de la sortie (Writer.regenerate : flux purgé puis recréé ; rouvrir
# le writer réattacherait le même flux, tête qui redescend) et de l'heure présente.
CLOCK_JUMP_SLOTS = 2 * FPS


# CADENCE FLOW sur la GRILLE MXL (0.24.7) : un tour par créneau de `current_index()`. La cadence
# dormait jusqu'à une frontière de `time.time()` — or le créneau MXL change à MI-créneau de
# l'horloge (mesuré le 2026-10-25 : 10,00 ms, 50/50) : le split démarrait chaque image 10 ms
# après le début de son créneau. Phase CALIBRÉE sur une frontière observée, comme le mélangeur.
_SLOT_NS = int(round(1e9 / FPS))
_flow_last_grid = 0
_flow_phase = None


def _calibrate_grid_phase():
    g0 = int(bobimxl.current_index(FPS, 1))
    t_end = time.monotonic() + 2.5 / FPS
    while time.monotonic() < t_end:
        if int(bobimxl.current_index(FPS, 1)) != g0:
            return int(bobimxl.now_tai()) % _SLOT_NS
    return 0


def _wait_next_slot():
    """Créneau SUIVANT de la grille MXL (dort jusqu'à lui). Recul > CLOCK_JUMP_SLOTS = saut
    d'horloge → recalage immédiat au lieu d'attendre l'ampleur du recul."""
    global _flow_last_grid, _flow_phase
    if not hasattr(bobimxl, "current_index"):
        time.sleep(1.0 / FPS)
        return _grid_now()
    if _flow_phase is None:
        _flow_phase = _calibrate_grid_phase()
    while True:
        g = int(bobimxl.current_index(FPS, 1))
        if g > _flow_last_grid or _flow_last_grid - g > CLOCK_JUMP_SLOTS:
            _flow_last_grid = g
            return g
        left = _SLOT_NS - ((int(bobimxl.now_tai()) - _flow_phase) % _SLOT_NS)
        time.sleep(max(0.0002, left / 1e9))


def _reopen_output():
    global out
    log("split: new generation of the output after a clock jump", "warning")
    if hasattr(out, "regenerate"):
        out.regenerate()
        return
    try: out.close()
    except Exception: pass
    out = _ouvrir_sortie(SHM_OUT)
# Cumulatifs depuis le boot (suffixe _total sur :8080) — accumulés PAR ÉVÉNEMENT dans
# _sl_ensure (robuste à une exception mi-trame). Leçon du banc mixer : un compteur cumulatif
# AMBIGU se lit mal → le cumul est explicitement suffixé _total, l'état courant (dormant,
# backoff) reste INSTANTANÉ.
_sl_tot = {{"waits": 0, "fallbacks": 0, "waited_ms": 0.0}}


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
    L'IDENTIQUE les calculs (arrondis compris) du compositing 0.8.0 → résultat octet-identique
    au chemin historique. Les mappings nearest ligne/colonne sont DÉJÀ clippés au rect visible.
    Renvoie None si la box est invisible (alpha≈0, trop petite ou hors cadre)."""
    if alpha <= 0.001:
        return None
    crop = _eff_crop(b)
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
    bd = _eff_border(b)
    try: bw = int(bd.get("w") or 0)
    except Exception: bw = 0
    if bw > 0:   # même clamp qu'en 0.8.0 (vers l'intérieur du rect visible)
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
    """Blende les lignes [b0,b1) ∩ box sur la sortie — MÊMES valeurs qu'en 0.8.0
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
    qu'en 0.8.0 (haut/bas/côtés vers l'intérieur, chroma cbw), clippés à la bande."""
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


# ═════════════════════════════════════════════════════════════════════════════════════════════
#  MODE AVANCÉ (0.9.0) — rotation, feather, coins arrondis, opacité, miroir, ratio libre,
#  ombre portée, bordure 3D (biseau + source de lumière).
#
#  ★ LA CONTRAINTE QUI DÉCIDE DE TOUT : LE COÛT (banc mesuré sur nœud 30, yuv422p 8 bits).
#  Ces effets sont des rééchantillonnages/convolutions par trame — en numpy naïf, ils font
#  décrocher le plugin. La clé mesurée est double :
#
#  1) CACHE STATIQUE (motif éprouvé du projet : fond des VU-mètres, chrome du multiview). Tant
#     qu'une box ne CHANGE PAS de réglage, sa rotation, son masque de coins, son ombre floutée
#     et son ruban de bordure sont IDENTIQUES d'une trame à l'autre → on les précalcule une fois
#     dans un « STAMP » (empreinte) et on ne refait le travail qu'au changement (clé de cache).
#     Par trame il ne reste qu'un GATHER + un BLEND.
#
#  2) CONSTRUCTION SÉPARABLE. Le piège : une TRANSITION change la géométrie à CHAQUE trame → le
#     cache se reconstruirait à chaque trame. Mesuré : une construction naïve (mgrid + trig 2D)
#     coûte 14,5 ms/box en 1080p → 58 ms pour 4 boxes = transition impossible. Or tout le stamp,
#     SAUF la rotation, est une fonction de la DISTANCE AUX BORDS → séparable en 2 rampes 1D
#     (produit externe) + 4 petits patchs de coins. Mesuré : 0,8 ms (alpha) + 1,0 ms (ombre)
#     au lieu de 14,5 → une transition avec tous les effets tient le budget.
#
#  Coût par trame MESURÉ (1080p, 4 boxes, Y+U+V, budget 40 ms @25 / 20 ms @50) :
#     réglages par défaut (aucun effet)      →  5,5 ms   (chemin historique, INCHANGÉ)
#     tous les effets actifs (cache chaud)   → 16,2 ms   (40 % @25fps, 81 % @50fps)
#  Le plafond connu est le 1080p50 avec rotation ET transition sur les 4 boxes simultanément
#  (la carte d'index 2D de la rotation doit être refaite : +2,8 ms/box) — cf. help.md.
# ═════════════════════════════════════════════════════════════════════════════════════════════

_stamps = {{}}         # idx box -> (clé, stamp) — cache statique, invalidé par la clé
_stamp_stats = {{"builds": 0, "hits": 0, "build_ms": 0.0}}
_over_warned = [False]   # alerte « hors budget » loggée une fois par franchissement
_n_rot = 0               # boxes TOURNÉES sur la dernière trame (poste de coût n°1)


def _blur1(v, r):
    """Flou ~gaussien d'un vecteur 1D : 3 box-blurs successifs (théorème central limite).
    SÉPARABLE : le flou d'un rectangle plein est le produit externe de 2 rampes 1D floutées —
    c'est ce qui rend l'ombre portée quasi gratuite à construire (mesuré : 1,0 ms au lieu de
    10,8 ms pour une convolution 2D)."""
    r = max(1, int(r))
    for _ in range(3):
        c = np.cumsum(np.pad(v, (r, r)), dtype=np.float32)
        v = (c[2 * r:] - c[:-2 * r]) / (2 * r)
    return v


def _blur2(m, r):
    """Flou ~gaussien 2D d'un masque NON séparable (silhouette d'une box tournée) : 3 box-blurs
    séparés en 2 passes 1D (lignes puis colonnes) via cumsum — jamais de convolution 2D.

    ★ Poste de coût n°1 du régime ANIMÉ (mesuré : 76 ms/trame sur 230 en 1080p, 4 boxes) : le
    repli numpy fait 6 × (pad + cumsum + slice + division), soit une allocation et plusieurs
    passes mémoire par étage. Le kernel C fait les mêmes sommes préfixes float32, en place."""
    r = max(1, int(r))
    if _MVK_STAMP and m.dtype == np.float32 and m.flags.c_contiguous:
        out = m if m.base is None else m.copy()
        if _mvk_try("blur2", bobimxl.mvk_spl_blur2, out, r, 3):
            return out
        m = out
    for _ in range(3):
        c = np.cumsum(np.pad(m, ((r, r), (0, 0))), axis=0, dtype=np.float32)
        m = (c[2 * r:] - c[:-2 * r]) / (2 * r)
        c = np.cumsum(np.pad(m, ((0, 0), (r, r))), axis=1, dtype=np.float32)
        m = (c[:, 2 * r:] - c[:, :-2 * r]) / (2 * r)
    return m


def _edge_ramp(n, fe):
    """Rampe 1D de distance au bord, normalisée par le feather (0 au bord → 1 à `fe` px)."""
    i = np.arange(n, dtype=np.float32)
    d = np.minimum(i, n - 1 - i)
    if fe <= 0.0:
        return np.ones(n, dtype=np.float32)
    return np.clip(d / fe, 0.0, 1.0)


def _build_alpha(dst_w, dst_h, fe, rad, op):
    """Masque alpha d'une box : feather × coins arrondis × opacité. Renvoie
    (a16, a16_chroma, marge, alpha_scalaire). SÉPARABLE (produit externe de 2 rampes + 4 patchs
    de coins R×R) → aucun balayage 2D coûteux.

    Trois régimes, du moins cher au plus cher — c'est ici que se joue le coût par trame :
      • PLEINEMENT OPAQUE (rien d'activé) → PAS DE MASQUE (None) : la box se COPIE. Une box
        seulement TOURNÉE ne paie pas le feather qu'elle n'utilise pas.
      • OPACITÉ UNIFORME (opacité globale, sans feather ni coins) → alpha SCALAIRE : le blend
        n'a pas à LIRE un masque plein rect (une passe mémoire de moins par plan).
      • MASQUE 2D (feather et/ou coins) → seul un RUBAN de `marge` px est mélangé, l'intérieur
        est copié (cf. _blend_rows)."""
    if fe <= 0.0 and rad <= 0 and op >= 0.999:
        return None, None, 0, None                      # opaque : aucun masque
    if fe <= 0.0 and rad <= 0:
        return None, None, -1, op                       # opacité uniforme : alpha scalaire
    a = np.multiply.outer(_edge_ramp(dst_h, fe), _edge_ramp(dst_w, fe))
    if rad > 0:
        j, i = np.mgrid[0:rad, 0:rad].astype(np.float32)   # patchs R×R seulement (jamais 2D plein)
        d = np.sqrt((rad - 1 - i) ** 2 + (rad - 1 - j) ** 2)
        soft = max(1.0, fe if fe > 0 else 1.0)
        cmask = np.clip((rad - d) / soft + (0.0 if fe > 0 else 1.0), 0.0, 1.0).astype(np.float32)
        a[:rad, :rad] *= cmask
        a[:rad, dst_w - rad:] *= cmask[:, ::-1]
        a[dst_h - rad:, :rad] *= cmask[::-1, :]
        a[dst_h - rad:, dst_w - rad:] *= cmask[::-1, ::-1]
    if op < 0.999:
        a *= op
    a16 = (a * 256.0).astype(np.uint16)        # blend ENTIER (mesuré plus rapide que le float32)
    a16_2 = a16[::_CH, ::_CW].copy()
    # Marge « non opaque » : au-delà, alpha == 256 partout → COPIE DIRECTE (aucun blend).
    m = int(math.ceil(fe)) + rad + 1
    if op < 0.999:
        m = max(dst_w, dst_h)                  # opacité globale → tout le rect est mélangé
    marg = max(0, min(m, dst_w // 2, dst_h // 2))
    return a16, a16_2, marg, None


def _build_alpha_rot(aw, ah, rot_loc, fe, rad, op, bw_tot):
    """Masque alpha d'une box TOURNÉE, dans son AABB. Le masque est calculé en coordonnées
    LOCALES à la box → feather et coins arrondis TOURNENT AVEC ELLE. Hors du quadrilatère
    incliné, alpha = 0 (on voit le fond).

    Les bords obliques sont ANTIALIASÉS par construction : alpha = clip(distance_au_bord + ½),
    ce qui donne une transition d'un pixel — sans quoi une box tournée aurait des marches
    d'escalier sur ses bords."""
    dedge, ul, vl, s, c, bw, bh = rot_loc
    if fe > 0.0:
        a = np.clip(dedge / fe, 0.0, 1.0)
    else:
        a = np.clip(dedge + 0.5, 0.0, 1.0)          # anticrénelage 1 px du bord oblique
    if rad > 0:
        # coins arrondis EN LOCAL : distance au centre de l'arc de coin
        cx = np.maximum(0.0, np.abs(ul) - (bw / 2.0 - rad))
        cy = np.maximum(0.0, np.abs(vl) - (bh / 2.0 - rad))
        rr = np.sqrt(cx * cx + cy * cy)
        soft = max(1.0, fe if fe > 0 else 1.0)
        a *= np.clip((rad - rr) / soft + (0.0 if fe > 0 else 1.0), 0.0, 1.0)
    if op < 0.999:
        a *= op
    a16 = (a * 256.0).astype(np.uint16)
    a16_2 = a16[::_CH, ::_CW].copy()
    return a16, a16_2, _rot_marg(aw, ah, bw, bh, fe, rad, op, bw_tot), None


def _rot_marg(aw, ah, bw, bh, fe, rad, op, bw_tot):
    """Marge opaque d'une box TOURNÉE (scalaire pur — commun au repli numpy et au kernel C).
    Le quad tourné contient son CERCLE INSCRIT (rayon min(bw,bh)/2 − marge de bord) ; le carré
    axis-aligned centré de demi-côté r/√2 tient dans ce cercle → on peut y COPIER directement
    (alpha=256, hors bordure). Marge conservatrice commune aux 2 axes."""
    edge_m = math.ceil(fe) + rad + math.ceil(bw_tot) + 2
    r = min(bw, bh) / 2.0 - edge_m
    if op >= 0.999 and r > 8:
        hs = r / 1.4143
        m = int(math.ceil(max(aw / 2.0 - hs, ah / 2.0 - hs)))
        return max(0, min(m, aw // 2, ah // 2))
    return max(aw, ah)                              # pas d'intérieur sûr → blend plein AABB


def _adv_key(b, alpha):
    """Clé de cache du stamp : TOUT ce dont dépend le précalcul. La position (x,y) n'en fait
    PAS partie — une box qui se contente de TRANSLATER réutilise son stamp tel quel (la carte
    d'échantillonnage ne dépend que de la taille, du crop, de la rotation et du miroir)."""
    bd = _eff_border(b); sh = b.get("shadow") or {{}}
    cr = _eff_crop(b)
    return (
        round(b.get("size", 0.0), 5), b.get("aspect"),
        round(b.get("size_x", 0.0), 5), round(b.get("size_y", 0.0), 5),
        round(cr.get("t", 0), 5), round(cr.get("b", 0), 5),
        round(cr.get("l", 0), 5), round(cr.get("r", 0), 5),
        round(b.get("rotation", 0.0), 3), bool(b.get("mirror_h")), bool(b.get("mirror_v")),
        round(b.get("opacity", 1.0), 4), round(b.get("feather", 0.0), 2),
        round(b.get("radius", 0.0), 2), round(float(alpha), 4),
        round(float(bd.get("w", 0) or 0), 2), str(bd.get("color")),
        round(bd.get("outer", 0.0), 2), round(bd.get("inner", 0.0), 2),
        round(bd.get("bevel", 0.0), 3), round(bd.get("bevel_soft", 0.3), 3),
        round(bd.get("bevel_pos", 0.5), 3),
        bool(sh.get("enabled")), round(sh.get("dx", 0.0), 2), round(sh.get("dy", 0.0), 2),
        round(sh.get("blur", 0.0), 2), round(sh.get("opacity", 0.0), 3), str(sh.get("color")),
        round(LIGHT["angle"], 2), round(LIGHT["altitude"], 2),
    )


def _build_stamp(b, alpha):
    """Précalcule l'EMPREINTE (stamp) d'une box : cartes d'échantillonnage + masque alpha +
    ruban de bordure 3D + masque d'ombre. Appelé UNIQUEMENT au changement de réglage (clé de
    cache) — jamais dans le régime établi. Tout est construit de façon SÉPARABLE.

    Renvoie None si la box est invisible (trop petite, hors cadre, alpha nul)."""
    if alpha <= 0.001:
        return None
    crop = _eff_crop(b)
    cl = _f(crop.get("l", 0.0), 0.0, 0.9, 0.0); cr_ = _f(crop.get("r", 0.0), 0.0, 0.9, 0.0)
    ct = _f(crop.get("t", 0.0), 0.0, 0.9, 0.0); cb = _f(crop.get("b", 0.0), 0.0, 0.9, 0.0)
    if cl + cr_ >= 0.95: cl = cr_ = 0.0
    if ct + cb >= 0.95: ct = cb = 0.0

    # ── Géométrie. RATIO LIBRE (aspect="free") : size_x/size_y indépendants → la box s'ÉTIRE
    #    (l'ATEM impose le ratio d'origine ; on autorise les deux).
    if b.get("aspect") == "free":
        sx = b.get("size_x") or b.get("size", 0.45)
        sy = b.get("size_y") or b.get("size", 0.45)
    else:
        sx = sy = b.get("size", 0.45)
    full_w = WIDTH * float(sx); full_h = HEIGHT * float(sy)
    dst_w = max(2, int(full_w * (1.0 - cl - cr_)))
    dst_h = max(2, int(full_h * (1.0 - ct - cb)))
    if dst_w % 2: dst_w -= 1
    if dst_h % 2: dst_h -= 1
    if dst_w < 2 or dst_h < 2:
        return None
    vis_cx = b["x"] * WIDTH + (cl - cr_) * 0.5 * full_w
    vis_cy = b["y"] * HEIGHT + (ct - cb) * 0.5 * full_h
    x0 = int(vis_cx - dst_w / 2); y0 = int(vis_cy - dst_h / 2)
    if x0 % 2: x0 -= 1
    if y0 % 2: y0 -= 1

    sxr0 = cl * WIDTH;  sxr1 = (1.0 - cr_) * WIDTH
    syr0 = ct * HEIGHT; syr1 = (1.0 - cb) * HEIGHT

    rot = float(b.get("rotation", 0.0))
    mh = bool(b.get("mirror_h")); mv = bool(b.get("mirror_v"))

    # ── Cartes d'échantillonnage (nearest). Deux régimes :
    #    • SANS rotation → 1D séparables (ix/iy), comme le chemin historique : quasi gratuit.
    #    • AVEC rotation → carte d'index PLAT 2D (np.take), construite par BROADCAST float32
    #      (pas de mgrid : mesuré 2,8 ms/box en 1080p au lieu de 14,5 ms).
    #    Le MIROIR est un simple retournement des rampes (jamais un flip du buffer : gratuit).
    u = (np.arange(dst_w, dtype=np.float32) + 0.5) / dst_w        # 0..1 sur la box
    v = (np.arange(dst_h, dtype=np.float32) + 0.5) / dst_h
    if mh: u = u[::-1].copy()
    if mv: v = v[::-1].copy()

    fe = float(b.get("feather", 0.0))
    rad = int(round(min(float(b.get("radius", 0.0)), dst_w / 2.0 - 1, dst_h / 2.0 - 1)))
    op = float(b.get("opacity", 100.0)) / 100.0 * float(alpha)   # POURCENT → fraction
    bd = _eff_border(b)
    b_in = float(bd.get("inner", 0.0)); b_flat = float(bd.get("w", 0) or 0)
    bevel = float(bd.get("bevel", 0.0))
    bw_tot = b_in + b_flat

    flat = flat2 = None
    ix = iy = ix2 = iy2 = None
    row_need = None
    _a16_rot = _a16_2_rot = None     # alpha déjà bâti par le kernel (chemin rapide tourné)
    rot_loc = None          # (dedge, sgn_x, sgn_y) en coordonnées LOCALES à la box (cas tourné)

    if abs(rot) > 1e-6:
        # ─────────── BOX TOURNÉE ───────────
        # ★ La rotation fait basculer LA TUILE ENTIÈRE (cadre compris), pas seulement le contenu
        # à l'intérieur d'un rectangle resté droit : hors du quadrilatère incliné, on voit le
        # FOND. Le stamp est donc posé sur la BOÎTE ENGLOBANTE (AABB) du quad tourné, et chaque
        # pixel est ramené en coordonnées LOCALES à la box par la rotation inverse — ce sont ces
        # coordonnées locales qui pilotent l'échantillonnage ET les masques (feather, coins,
        # bordure), de sorte que tout tourne AVEC la box.
        th = math.radians(rot); s, c = math.sin(th), math.cos(th)
        aw = int(math.ceil(abs(dst_w * c) + abs(dst_h * s))) + 2
        ah = int(math.ceil(abs(dst_w * s) + abs(dst_h * c))) + 2
        aw -= aw % 2; ah -= ah % 2
        aw = min(aw, WIDTH * 3); ah = min(ah, HEIGHT * 3)
        X = (np.arange(aw, dtype=np.float32) - (aw - 1) / 2.0)
        Y = (np.arange(ah, dtype=np.float32) - (ah - 1) / 2.0)
        # rotation INVERSE : (X,Y) écran → (ul,vl) local box (centré, en px)
        # Rampes séparées : ul = ys[j] + xc[i], vl = yc[j] + nxs[i]. Le kernel C les prend telles
        # quelles → mêmes valeurs float32 que le produit externe numpy, à l'ULP près (le repli
        # ci-dessous EST le produit externe).
        _ys = (Y * s).astype(np.float32); _yc = (Y * c).astype(np.float32)
        _xc = (X * c).astype(np.float32); _nxs = (-X * s).astype(np.float32)
        hw = dst_w / 2.0; hh = dst_h / 2.0
        # ★ CHEMIN RAPIDE (kernel C, ABI 6) : carte d'échantillonnage + alpha + distance aux
        # bords en UNE passe. Ce bloc n'est PAS une optimisation de confort — c'est le régime
        # ANIMÉ (géométrie qui change à chaque trame) qui décroche sans lui : l'empreinte est
        # alors rebâtie 50 fois par seconde et sa construction numpy pèse 90 % de la trame.
        _rot = _mvk_try_r("rotmap", bobimxl.mvk_spl_rotmap,
                          aw, ah, _ys, _yc, _xc, _nxs, hw, hh, float(dst_w), float(dst_h),
                          mh, mv, sxr0, sxr1 - sxr0, syr0, syr1 - syr0,
                          WIDTH, HEIGHT, UV_W, UV_H, _CW, _CH,
                          fe > 0.0, fe, rad, (max(1.0, fe if fe > 0 else 1.0)),
                          (0.0 if fe > 0 else 1.0), hw - rad, hh - rad,
                          op < 0.999, op, want_dedge=True, want_rowmax=SLICE_ON)
        if _rot is not None:
            flat, flat2, _a16_rot, _a16_2_rot, dedge, _rowmax = _rot
            ul = vl = None                      # jamais matérialisés : le kernel les recalcule
            if SLICE_ON:
                row_need = np.maximum.accumulate(_rowmax).astype(np.int32)
        else:
            ul = np.add.outer(Y * s, X * c)
            vl = np.add.outer(Y * c, -X * s)
            # distance SIGNÉE aux bords de la box, en local (>0 dedans, <0 dehors)
            dxl = hw - np.abs(ul); dyl = hh - np.abs(vl)
            dedge = np.minimum(dxl, dyl)
            # échantillonnage : local normalisé → région source croppée
            fu = ul / dst_w + 0.5; fv = vl / dst_h + 0.5
            if mh: fu = 1.0 - fu
            if mv: fv = 1.0 - fv
            mx = np.clip(sxr0 + fu * (sxr1 - sxr0), 0, WIDTH - 1)
            my = np.clip(syr0 + fv * (syr1 - syr0), 0, HEIGHT - 1)
            myi = my.astype(np.int32); mxi = mx.astype(np.int32)
            flat = (myi.astype(np.int64) * WIDTH + mxi).ravel()
            my2 = np.clip(myi[::_CH, ::_CW] // _CH, 0, UV_H - 1)
            mx2 = np.clip(mxi[::_CH, ::_CW] // _CW, 0, UV_W - 1)
            flat2 = (my2.astype(np.int64) * UV_W + mx2).ravel()
            if SLICE_ON:
                # MODE TRANCHE : la rotation échantillonne EN DIAGONALE → une bande de sortie
                # peut réclamer des lignes source bien plus basses. Max par ligne, cumulé.
                row_need = np.maximum.accumulate(myi.max(axis=1)).astype(np.int32)
        # le stamp couvre l'AABB ; on recentre le rect de destination dessus
        x0 += (dst_w - aw) // 2
        y0 += (dst_h - ah) // 2
        if x0 % 2: x0 -= 1
        if y0 % 2: y0 -= 1
        rot_loc = (dedge, ul, vl, s, c, dst_w, dst_h)   # …, largeur/hauteur RÉELLES de la box
        dst_w, dst_h = aw, ah          # ← le stamp vit désormais dans l'AABB
    else:
        # ─────────── BOX DROITE : cartes 1D séparables (chemin historique, quasi gratuit) ──────
        u = (np.arange(dst_w, dtype=np.float32) + 0.5) / dst_w
        v = (np.arange(dst_h, dtype=np.float32) + 0.5) / dst_h
        if mh: u = u[::-1].copy()      # MIROIR = retournement de rampe (jamais un flip de buffer)
        if mv: v = v[::-1].copy()
        ix = np.clip((sxr0 + u * (sxr1 - sxr0)).astype(np.int32), 0, WIDTH - 1)
        iy = np.clip((syr0 + v * (syr1 - syr0)).astype(np.int32), 0, HEIGHT - 1)
        u2 = (np.arange(dst_w // _CW, dtype=np.float32) + 0.5) / max(1, dst_w // _CW)
        v2 = (np.arange(dst_h // _CH, dtype=np.float32) + 0.5) / max(1, dst_h // _CH)
        if mh: u2 = u2[::-1].copy()
        if mv: v2 = v2[::-1].copy()
        ix2 = np.clip((sxr0 / _CW + u2 * ((sxr1 - sxr0) / _CW)).astype(np.int32), 0, UV_W - 1)
        iy2 = np.clip((syr0 / _CH + v2 * ((syr1 - syr0) / _CH)).astype(np.int32), 0, UV_H - 1)

    # ── Masque ALPHA : feather × coins arrondis × opacité × fondu de transition.
    if rot_loc is not None:
        if _a16_rot is not None:
            # alpha déjà produit par `mvk_spl_rotmap`, dans la même passe que la carte
            # d'échantillonnage : il ne reste que la MARGE (calcul scalaire).
            a16, a16_2, a_scalar = _a16_rot, _a16_2_rot, None
            marg = _rot_marg(dst_w, dst_h, rot_loc[5], rot_loc[6], fe, rad, op, bw_tot)
        else:
            a16, a16_2, marg, a_scalar = _build_alpha_rot(dst_w, dst_h, rot_loc, fe, rad,
                                                          op, bw_tot)
    else:
        a16, a16_2, marg, a_scalar = _build_alpha(dst_w, dst_h, fe, rad, op)

    # ── BORDURE 3D : largeur intérieure/extérieure, biseau (rentrant/sortant), adoucissement,
    #    position, et SOURCE DE LUMIÈRE (direction + hauteur) → relief. Le ruban est une
    #    fonction de la distance au bord ⇒ SÉPARABLE (minimum externe de 2 rampes).
    b_out = float(bd.get("outer", 0.0))   # (bd / b_in / b_flat / bevel / bw_tot : définis + haut)
    ring_a = ring_y = ring_u = ring_v = None
    if bw_tot > 0.0 or b_out > 0.0:
        Yb, Ub, Vb = _hex_to_yuv(bd.get("color"))
        inner_w = max(0.0, bw_tot)
        # ★ Le ruban ne fait que `bw_tot` px de large : construire des tableaux PLEIN RECT pour
        # lui, c'est calculer des dizaines de milliers de pixels dont on sait déjà qu'ils valent
        # zéro. On n'évalue donc QUE les 4 bandes du ruban (le reste reste à zéro, jamais lu) —
        # mesuré : construction de la bordure 3D 38,1 → 12,3 ms pour 4 boxes en 1080p.
        ring_a = np.zeros((dst_h, dst_w), dtype=np.uint16)
        ring_y = np.zeros((dst_h, dst_w), dtype=_NP_DT)
        m = max(1, min(int(math.ceil(inner_w)) + 2, dst_h // 2, dst_w // 2))
        la = math.radians(LIGHT["angle"]); lalt = math.radians(LIGHT["altitude"])
        lx = math.cos(la) * math.cos(lalt); ly = -math.sin(la) * math.cos(lalt)
        lz = max(0.05, math.sin(lalt))
        sgn = 1.0 if bevel > 0 else -1.0
        pos = float(bd.get("bevel_pos", 0.5)); soft = max(0.05, float(bd.get("bevel_soft", 0.3)))
        if rot_loc is not None:
            # BOX TOURNÉE : le ruban suit les bords OBLIQUES (distance locale), et la normale du
            # biseau est TOURNÉE elle aussi — sinon le relief resterait éclairé comme si la box
            # était droite (cadre incohérent avec la lumière de la scène).
            dedge_l, ul, vl, s_, c_, _bw, _bh = rot_loc
            _ring_done = False
            if ul is None:
                # ★ CHEMIN RAPIDE : le ruban oblique est bâti par le kernel C, en une passe, à
                # partir des mêmes rampes que la carte d'échantillonnage — au lieu d'une dizaine
                # de tableaux 2D numpy (abs/where/sign/clip/sqrt) qui ne servent qu'ici.
                _ring_done = _mvk_try(
                    "rotring", bobimxl.mvk_spl_rotring, ring_a, ring_y, _ys, _yc, _xc, _nxs,
                    _bw / 2.0, _bh / 2.0, s_, c_, inner_w, abs(bevel) > 1e-6, abs(bevel), sgn,
                    pos * inner_w, soft * max(1.0, inner_w), lx, ly, lz,
                    Yb, 16 * _SCALE, _MAXV)
            if not _ring_done:
                if ul is None:      # kernel refusé : reconstruire ce qu'il aurait produit
                    ul = np.add.outer(_ys, _xc); vl = np.add.outer(_yc, _nxs)
                dxl = _bw / 2.0 - np.abs(ul); dyl = _bh / 2.0 - np.abs(vl)
                de = np.minimum(dxl, dyl) if dedge_l is None else dedge_l
                near_x_l = np.less(dyl, dxl)              # bord le plus proche = haut/bas
                nlx = np.where(near_x_l, 0.0, np.sign(ul)).astype(np.float32)
                nly = np.where(near_x_l, np.sign(vl), 0.0).astype(np.float32)
                gx = (nlx * c_ - nly * s_).astype(np.float32)   # normale locale → repère ÉCRAN
                gy = (nlx * s_ + nly * c_).astype(np.float32)
                ring = np.clip(inner_w - de, 0.0, 1.0) * (de > 0)
                if abs(bevel) > 1e-6:
                    shade = np.clip((lx * gx + ly * gy) * sgn * abs(bevel) + lz, 0.0, 1.0)
                    t = np.clip((de - pos * inner_w) / (soft * max(1.0, inner_w)) + 0.5, 0.0, 1.0)
                    prof = 0.35 + 0.65 * (1.0 - np.abs(2.0 * t - 1.0))
                    lit = np.clip(shade * prof * 2.0, 0.0, 1.6)
                    ring_y[:] = np.clip(Yb * lit, 16 * _SCALE, _MAXV).astype(_NP_DT)
                else:
                    ring_y[:] = Yb
                ring_a[:] = (ring * 256.0).astype(np.uint16)
            m = max(dst_w, dst_h)     # ruban oblique → pas de bande axis-aligned exploitable
        else:
            ax = np.arange(dst_w, dtype=np.float32); ay = np.arange(dst_h, dtype=np.float32)
            ex_f = np.minimum(ax, dst_w - 1 - ax); ey_f = np.minimum(ay, dst_h - 1 - ay)
            sx_f = np.where(ax < dst_w / 2.0, 1.0, -1.0).astype(np.float32)
            sy_f = np.where(ay < dst_h / 2.0, 1.0, -1.0).astype(np.float32)
            for (r0, r1, c0, c1) in _ring_slices(dst_h, dst_w, m):
                ey_s = ey_f[r0:r1]; ex_s = ex_f[c0:c1]
                dedge = np.minimum.outer(ey_s, ex_s)          # distance au bord (séparable)
                ring = np.clip(inner_w - dedge, 0.0, 1.0) if inner_w > 0 else np.zeros_like(dedge)
                if abs(bevel) > 1e-6:
                    # Relief : la normale du biseau pointe vers l'extérieur du bord LE PLUS
                    # PROCHE, et on l'éclaire avec la source de lumière (direction + hauteur) →
                    # un bord clair, l'opposé sombre. C'est CE gradient qui distingue un cadre 3D
                    # d'un trait plat. Normale ANALYTIQUE (pas de np.gradient 2D) :
                    # dedge = min(ey,ex) ⇒ gradient ±1 sur l'axe du bord le plus proche, 0 sinon.
                    near_x = np.less.outer(ey_s, ex_s)
                    gx = np.where(near_x, 0.0, sx_f[c0:c1][None, :]).astype(np.float32)
                    gy = np.where(near_x, sy_f[r0:r1][:, None], 0.0).astype(np.float32)
                    shade = np.clip((lx * gx + ly * gy) * sgn * abs(bevel) + lz, 0.0, 1.0)
                    # position + adoucissement du biseau : profil sur l'épaisseur du ruban
                    t = np.clip((dedge - pos * inner_w) / (soft * max(1.0, inner_w)) + 0.5, 0.0, 1.0)
                    prof = 0.35 + 0.65 * (1.0 - np.abs(2.0 * t - 1.0))
                    lit = np.clip(shade * prof * 2.0, 0.0, 1.6)
                    ring_y[r0:r1, c0:c1] = np.clip(Yb * lit, 16 * _SCALE, _MAXV).astype(_NP_DT)
                else:
                    ring_y[r0:r1, c0:c1] = Yb
                ring_a[r0:r1, c0:c1] = (ring * 256.0).astype(np.uint16)
        ring_u = np.full((dst_h // _CH, dst_w // _CW), Ub, dtype=_NP_DT)
        ring_v = np.full((dst_h // _CH, dst_w // _CW), Vb, dtype=_NP_DT)
        ring_a2 = ring_a[::_CH, ::_CW].copy()
        ring_marg = m
    else:
        ring_a2 = None
        ring_marg = 0

    # ── OMBRE PORTÉE : masque de la box FLOUTÉ (convolution) puis DÉCALÉ. Séparable → le flou
    #    est fait sur 2 vecteurs 1D et recombiné par produit externe (mesuré 1,0 ms au lieu de
    #    10,8 ms). Entièrement PRÉCALCULÉ : par trame il ne reste qu'un blend de ruban.
    # ★ LE FLOU DOIT S'ÉPANDRE VERS L'EXTÉRIEUR (fix 0.12.0). Jusqu'ici le masque d'ombre était
    # bâti sur le rect de la box LUI-MÊME : le flou ne pouvait que MANGER VERS L'INTÉRIEUR, et il
    # était COUPÉ NET au bord (mesuré : masque encore à 77/128 sur la dernière colonne, cf. le
    # profil du banc). Ce n'est pas une ombre, c'est un rectangle dégradé à l'intérieur de
    # lui-même. Le masque est donc bâti sur un rect ÉLARGI d'une marge de flou : le dégradé
    # s'éteint alors À ZÉRO dans la marge, comme une vraie ombre.
    #
    # Marge = SPREAD RÉEL du noyau : _blur1/_blur2 enchaînent 3 box-blurs de rayon r → l'épandage
    # est de 3·r (et non r). Un blur de 20 déborde donc de 60 px. La marge est ancrée sur la
    # grille chroma (paire) — l'ombre reste chroma-alignée (fix 4:2:0). Le DÉCALAGE (dx/dy) sort
    # déjà l'ombre de la box : il est appliqué à l'origine, comme avant.
    # Coût : le rect d'ombre grandit (mesuré au banc, cf. help.md) — c'est le prix d'une ombre
    # correcte, et il est DIT plutôt que subi. Le rognage final au CADRE est fait au rendu
    # (_paint_shadow_rows clippe à WIDTH/HEIGHT) : une ombre peut donc sortir du cadre, comme une
    # box (chantier hors-cadre).
    sh = b.get("shadow") or {{}}
    shadow = None
    if sh.get("enabled") and float(sh.get("opacity", 0.0)) > 0.001:
        br = int(round(float(sh.get("blur", 8.0))))
        amp = 256.0 * (float(sh.get("opacity", 50.0)) / 100.0) * float(alpha)
        pad = 3 * br + 2 if br > 0 else 0        # épandage des 3 box-blurs + garde
        if pad % 2:
            pad += 1                             # marge PAIRE → chroma-alignée (420/422)
        sw, shh = dst_w + 2 * pad, dst_h + 2 * pad
        if rot_loc is not None:
            # BOX TOURNÉE : l'ombre est la SILHOUETTE INCLINÉE floutée (une ombre rectangulaire
            # droite sous une box penchée trahirait immédiatement le truquage). La silhouette
            # n'est pas séparable → flou 2D séparé en 2 passes 1D (lignes puis colonnes). Elle est
            # posée dans le rect ÉLARGI (zéro dans la marge) → le flou s'y épand librement.
            sil = np.zeros((shh, sw), dtype=np.float32)
            sil[pad:pad + dst_h, pad:pad + dst_w] = np.clip(rot_loc[0] + 0.5, 0.0, 1.0)
            if br > 0:
                sil = _blur2(sil, br)
            sa16 = (sil * amp).astype(np.uint16)
        else:
            # SÉPARABLE : la silhouette d'un rect plein est le produit externe de 2 créneaux 1D →
            # on floute les créneaux (posés dans un vecteur ÉLARGI), jamais une convolution 2D.
            ix_ = np.zeros(sw, np.float32); ix_[pad:pad + dst_w] = 1.0
            iy_ = np.zeros(shh, np.float32); iy_[pad:pad + dst_h] = 1.0
            sm_x = _blur1(ix_, br) if br > 0 else ix_
            sm_y = _blur1(iy_, br) if br > 0 else iy_
            # L'échelle (opacité × fondu × 256) est repliée dans les VECTEURS 1D AVANT le produit
            # externe → le masque sort déjà à l'échelle : un balayage plein rect au lieu de trois.
            sm_y = (sm_y * amp).astype(np.float32)
            sm_x = np.ascontiguousarray(sm_x, dtype=np.float32)
            sa16 = np.empty((shh, sw), dtype=np.uint16)
            if not _mvk_try("outer", bobimxl.mvk_spl_outer_u16, sa16, sm_y, sm_x):
                sa16 = np.multiply.outer(sm_y, sm_x).astype(np.uint16)
        sYa, sUa, sVa = _hex_to_yuv(sh.get("color") or "#000000")
        _sx0 = x0 + int(round(float(sh.get("dx", 8.0)))) - pad
        _sy0 = y0 + int(round(float(sh.get("dy", 8.0)))) - pad
        _sx0 -= _sx0 % _CW          # ancrage sur la grille CHROMA (cf. fix 4:2:0)
        _sy0 -= _sy0 % _CH
        shadow = {{
            "x0": _sx0,
            "y0": _sy0,
            "w": sw, "h": shh,
            "a": sa16,
            "a2": sa16[::_CH, ::_CW].copy(),
            "yuv": (sYa, sUa, sVa),
        }}

    # Rect INTÉRIEUR pleinement OPAQUE de la box (alpha == 256 partout) : tout ce qui est peint
    # dessous y est INVISIBLE → l'ombre portée n'a pas à y être calculée (cf. _paint_shadow_rows).
    # ★ Le « trou » opaque doit lui aussi être CHROMA-ALIGNÉ (fix 4:2:0) : ses bords servent de
    # frontières à la découpe guillotine de l'ombre. On le RÉTRÉCIT jusqu'à la grille chroma
    # (jamais on ne l'agrandit) → il reste entièrement couvert par la box : rien de visible, et
    # les sous-rectangles d'ombre tombent tous sur des lignes/colonnes chroma entières.
    op_rect = None
    if op >= 0.999 and marg < min(dst_w, dst_h) // 2:
        _ox0, _oy0 = x0 + marg, y0 + marg
        _ox1, _oy1 = x0 + dst_w - marg, y0 + dst_h - marg
        _ox0 += (-_ox0) % _CW; _oy0 += (-_oy0) % _CH        # arrondi VERS L'INTÉRIEUR
        _ox1 -= _ox1 % _CW;    _oy1 -= _oy1 % _CH
        if _ox1 > _ox0 and _oy1 > _oy0:
            op_rect = (_ox0, _oy0, _ox1, _oy1)

    # ── FUSION du ruban de bordure et du blend alpha (une seule passe) ────────────────────────
    # Quand il n'y a PAS d'intérieur opaque exploitable (box TOURNÉE : le ruban et les bords sont
    # obliques ; ou opacité globale), le compositing ferait DEUX balayages pleins du rect : un
    # pour peindre la bordure dans le calque source, un pour le fondre sur la sortie. On les
    # PRÉ-FUSIONNE ici, hors trame :
    #     sortie = out·(1−a) + [src·(1−ring) + couleur·ring]·a
    #            = out·(1−a) + src·[(1−ring)·a] + [couleur·ring·a]
    #              └ inv_a ┘         └── A1 ──┘   └────── C2 ──────┘   (précalculés)
    # → il ne reste qu'UNE passe par plan. Mesuré : box tournée avec bordure, ~30 % de moins.
    fused = None
    if (ring_a is not None) and marg >= min(dst_w, dst_h) // 2:
        _a = a16 if a16 is not None else np.full((dst_h, dst_w), 256, np.uint16)
        _a2 = a16_2 if a16_2 is not None else np.full((dst_h // _CH, dst_w // _CW), 256, np.uint16)
        # ★ POSTE DE COÛT n°1 DU CHEMIN NON TOURNÉ (mesuré : 17 ms sur 41 en 1080p animé, 4 boxes).
        # Neuf tableaux plein rect, ~20 passes mémoire en numpy — et l'ironie est que ce bloc
        # existe pour rendre la trame RAPIDE : il ne se paie que quand la géométrie BOUGE, cas
        # que le cache d'empreinte ne couvre pas. Le kernel fait les trois sorties d'un plan en
        # une passe ; U et V partagent alpha et ne diffèrent que par la couleur du ruban.
        _oy = np.empty((dst_h, dst_w), np.uint32)
        _ou = np.empty((dst_h // _CH, dst_w // _CW), np.uint32)
        _ov = np.empty((dst_h // _CH, dst_w // _CW), np.uint32)
        _ia = np.empty((dst_h, dst_w), np.uint16); _a1 = np.empty((dst_h, dst_w), np.uint16)
        _ia2 = np.empty((dst_h // _CH, dst_w // _CW), np.uint16)
        _a12 = np.empty((dst_h // _CH, dst_w // _CW), np.uint16)
        if (_mvk_try("prefuse", bobimxl.mvk_spl_prefuse, _a, ring_a, ring_y, _ia, _a1, _oy)
                and _mvk_try("prefuse", bobimxl.mvk_spl_prefuse, _a2, ring_a2, ring_u,
                             _ia2, _a12, _ou)
                and _mvk_try("prefuse", bobimxl.mvk_spl_prefuse, _a2, ring_a2, ring_v,
                             None, None, _ov)):
            fused = {{"inv_a": _ia, "inv_a2": _ia2, "A1": _a1, "A1_2": _a12,
                      "C2y": _oy, "C2u": _ou, "C2v": _ov}}
        else:
            A1 = ((_a.astype(np.uint32) * (256 - ring_a)) >> 8).astype(np.uint16)
            A1_2 = ((_a2.astype(np.uint32) * (256 - ring_a2)) >> 8).astype(np.uint16)
            ar = ((_a.astype(np.uint32) * ring_a) >> 8).astype(np.uint16)
            ar2 = ((_a2.astype(np.uint32) * ring_a2) >> 8).astype(np.uint16)
            fused = {{
                "inv_a": (256 - _a).astype(np.uint16),
                "inv_a2": (256 - _a2).astype(np.uint16),
                "A1": A1, "A1_2": A1_2,
                "C2y": ((ring_y.astype(np.uint32) * ar) >> 8).astype(np.uint32),
                "C2u": ((ring_u.astype(np.uint32) * ar2) >> 8).astype(np.uint32),
                "C2v": ((ring_v.astype(np.uint32) * ar2) >> 8).astype(np.uint32),
            }}

    return {{
        "x0": x0, "y0": y0, "w": dst_w, "h": dst_h,
        "flat": flat, "flat2": flat2, "ix": ix, "iy": iy, "ix2": ix2, "iy2": iy2,
        "row_need": row_need,
        "a": a16, "a2": a16_2, "marg": marg, "a_scalar": a_scalar, "fused": fused,
        "opaque": (op >= 0.999 and marg == 0),
        "op_rect": op_rect, "op_marg": marg,
        "ring_a": ring_a, "ring_a2": ring_a2, "ring_y": ring_y, "ring_u": ring_u,
        "ring_v": ring_v, "ring_marg": ring_marg,
        "shadow": shadow, "alpha": float(alpha),
    }}


# ═══════════════════════════════════════════════════════════════════════════════════════════════
#  CHEMIN GPU — compositing sans empreinte
#
#  Tout ce qui suit est le PENDANT de `_build_stamp` + `_place_adv_rows` + `_paint_shadow_rows`,
#  mais SANS rien précalculer : la géométrie de chaque box se réduit à une poignée de scalaires
#  passés au kernel, qui les évalue par pixel. Il n'y a donc ni empreinte, ni cache, ni clé de
#  cache, ni « régime animé » — c'est exactement ce que le GPU achète, et la seule chose.
#
#  ⚠ CE N'EST PAS BIT-EXACT avec le chemin CPU (blend flottant vs entier 256e) : c'est un chemin
#  de rendu DISTINCT, choisi explicitement. Cf. help.md « CPU ou GPU ? ».
# ═══════════════════════════════════════════════════════════════════════════════════════════════

_gpu = {{"mod": None, "canvas": None, "src": {{}}, "sil": None, "tmp": None, "stream": None,
         "pin_out": None, "pin_in": {{}}}}


def _gpu_init():
    """Charge les kernels (NVRTC) et alloue les tampons VRAM persistants. Une seule fois."""
    if _gpu["mod"] is not None:
        return _gpu["mod"]
    path = os.path.join(os.path.dirname(os.path.abspath(bobimxl.__file__)), "split_gpu.cu")
    with open(path) as f:
        code = f.read()
    mod = cp.RawModule(code=code, backend="nvrtc", options=("-std=c++11",))
    _gpu["mod"] = mod
    _gpu["box"] = mod.get_function("spl_box_u16" if _DEEP else "spl_box_u8")
    _gpu["sha"] = mod.get_function("spl_shadow_u16" if _DEEP else "spl_shadow_u8")
    _gpu["sil_k"] = mod.get_function("spl_silhouette")
    _gpu["bh"] = mod.get_function("spl_blur_h")
    _gpu["bv"] = mod.get_function("spl_blur_v")
    _gpu["canvas"] = [cp.empty((HEIGHT, WIDTH), dtype=_NP_DT),
                      cp.empty((UV_H, UV_W), dtype=_NP_DT),
                      cp.empty((UV_H, UV_W), dtype=_NP_DT)]
    # Tampons d'ombre : dimensionnés au pire cas (AABB d'une box plein cadre + marge de flou).
    _n = (WIDTH + 256) * (HEIGHT + 256)
    _gpu["sil"] = cp.zeros(_n, dtype=cp.float32)
    _gpu["tmp"] = cp.zeros(_n, dtype=cp.float32)
    _gpu["stream"] = cp.cuda.Stream(non_blocking=True)
    # Mémoire ÉPINGLÉE pour les transferts : sans elle le driver recopie par un tampon
    # intermédiaire à chaque sens, et le H2D coûte le double.
    _gpu["pin_out"] = np.frombuffer(cp.cuda.alloc_pinned_memory(V_FRAME_SIZE),
                                    dtype=np.uint8, count=V_FRAME_SIZE)
    log("split: rendu GPU actif — %s (kernels %s)" % (_GPU_NAME, path))
    return mod


def _gpu_src(slot, frame_bytes):
    """Monte une trame source en VRAM (tampon persistant par slot, épinglé côté hôte)."""
    buf = _gpu["src"].get(slot)
    if buf is None:
        buf = [cp.empty((HEIGHT, WIDTH), dtype=_NP_DT),
               cp.empty((UV_H, UV_W), dtype=_NP_DT),
               cp.empty((UV_H, UV_W), dtype=_NP_DT)]
        _gpu["src"][slot] = buf
        _gpu["pin_in"][slot] = np.frombuffer(cp.cuda.alloc_pinned_memory(V_FRAME_SIZE),
                                             dtype=np.uint8, count=V_FRAME_SIZE)
    pin = _gpu["pin_in"][slot]
    pin[:] = np.frombuffer(frame_bytes, dtype=np.uint8, count=V_FRAME_SIZE)
    sy, su, sv = _plane_views(pin)
    st = _gpu["stream"]
    buf[0].set(sy, stream=st); buf[1].set(su, stream=st); buf[2].set(sv, stream=st)
    return buf


def _gpu_box_geom(b, alpha):
    """Scalaires de géométrie d'une box — MÊMES formules que `_build_stamp`, sans rien allouer.
    Renvoie None si la box est invisible. Tout ce que le CPU matérialisait (cartes, masques,
    ruban, silhouette) tient ici dans des flottants passés au kernel."""
    if alpha <= 0.001:
        return None
    crop = _eff_crop(b)
    cl = _f(crop.get("l", 0.0), 0.0, 0.9, 0.0); cr_ = _f(crop.get("r", 0.0), 0.0, 0.9, 0.0)
    ct = _f(crop.get("t", 0.0), 0.0, 0.9, 0.0); cb = _f(crop.get("b", 0.0), 0.0, 0.9, 0.0)
    if cl + cr_ >= 0.95: cl = cr_ = 0.0
    if ct + cb >= 0.95: ct = cb = 0.0
    if b.get("aspect") == "free":
        sx = b.get("size_x") or b.get("size", 0.45); sy_ = b.get("size_y") or b.get("size", 0.45)
    else:
        sx = sy_ = b.get("size", 0.45)
    full_w = WIDTH * float(sx); full_h = HEIGHT * float(sy_)
    dst_w = max(2, int(full_w * (1.0 - cl - cr_))) & ~1
    dst_h = max(2, int(full_h * (1.0 - ct - cb))) & ~1
    if dst_w < 2 or dst_h < 2:
        return None
    vis_cx = b["x"] * WIDTH + (cl - cr_) * 0.5 * full_w
    vis_cy = b["y"] * HEIGHT + (ct - cb) * 0.5 * full_h
    rot = float(b.get("rotation", 0.0))
    th = math.radians(rot); s_, c_ = math.sin(th), math.cos(th)
    aw = (int(math.ceil(abs(dst_w * c_) + abs(dst_h * s_))) + 2) & ~1
    ah = (int(math.ceil(abs(dst_w * s_) + abs(dst_h * c_))) + 2) & ~1
    x0 = int(vis_cx - aw / 2) & ~1
    y0 = int(vis_cy - ah / 2) & ~1
    fe = float(b.get("feather", 0.0))
    rad = float(min(float(b.get("radius", 0.0)), dst_w / 2.0 - 1, dst_h / 2.0 - 1))
    op = float(b.get("opacity", 100.0)) / 100.0 * float(alpha)
    bd = _eff_border(b)
    inner_w = max(0.0, float(bd.get("inner", 0.0)) + float(bd.get("w", 0) or 0))
    bevel = float(bd.get("bevel", 0.0))
    return {{
        "x0": x0, "y0": y0, "aw": aw, "ah": ah,
        "s": s_, "c": c_, "hw": dst_w / 2.0, "hh": dst_h / 2.0,
        "fdw": float(dst_w), "fdh": float(dst_h),
        "mh": 1 if b.get("mirror_h") else 0, "mv": 1 if b.get("mirror_v") else 0,
        "sxr0": cl * WIDTH, "dsx": (1.0 - cr_ - cl) * WIDTH,
        "syr0": ct * HEIGHT, "dsy": (1.0 - cb - ct) * HEIGHT,
        "use_fe": 1 if fe > 0 else 0, "fe": max(fe, 1e-6), "rad": rad,
        "cref_x": dst_w / 2.0 - rad, "cref_y": dst_h / 2.0 - rad,
        "csoft": max(1.0, fe if fe > 0 else 1.0), "cadd": 0.0 if fe > 0 else 1.0,
        "op": op, "inner_w": inner_w, "bevel": bevel,
        "bd": bd, "shadow": b.get("shadow") or {{}}, "alpha": float(alpha),
    }}


def _gpu_compose(buf, layers):
    """Compose la trame entière sur GPU. `buf` (bytes du canvas déjà rempli par le fond) est
    monté, composé, puis rapatrié EN PLACE. `layers` = [(slot, box, alpha, frame_bytes)]."""
    _gpu_init()
    st = _gpu["stream"]
    la = math.radians(LIGHT["angle"]); lalt = math.radians(LIGHT["altitude"])
    lx = math.cos(la) * math.cos(lalt); ly = -math.sin(la) * math.cos(lalt)
    lz = max(0.05, math.sin(lalt))
    cvs = _gpu["canvas"]
    with st:
        oy, ou, ov = _plane_views(buf)
        cvs[0].set(oy, stream=st); cvs[1].set(ou, stream=st); cvs[2].set(ov, stream=st)
        for slot, b, alpha, fb in layers:
            g = _gpu_box_geom(b, alpha)
            if g is None:
                _slot_skip(slot, "geom_none"); continue
            src = _gpu_src(slot, fb)
            sh = g["shadow"]
            if sh.get("enabled") and float(sh.get("opacity", 0.0)) > 0.001:
                _gpu_shadow(cvs, g, sh)
            Yb, Ub, Vb = _hex_to_yuv(g["bd"].get("color"))
            sgn = 1.0 if g["bevel"] > 0 else -1.0
            pos = float(g["bd"].get("bevel_pos", 0.5))
            soft = max(0.05, float(g["bd"].get("bevel_soft", 0.3)))
            for p, (pcw, pch, ringc) in enumerate(((1, 1, Yb), (_CW, _CH, Ub), (_CW, _CH, Vb))):
                paw, pah = g["aw"] // pcw, g["ah"] // pch
                if paw < 1 or pah < 1:
                    continue
                grid = ((paw + 15) // 16, (pah + 15) // 16)
                _gpu["box"](grid, (16, 16), (
                    cvs[p], WIDTH // pcw, HEIGHT // pch, src[p], WIDTH // pcw, HEIGHT // pch,
                    g["x0"] // pcw, g["y0"] // pch, paw, pah, pcw, pch,
                    np.float32(g["s"]), np.float32(g["c"]),
                    np.float32(g["hw"]), np.float32(g["hh"]),
                    np.float32(g["fdw"]), np.float32(g["fdh"]), g["mh"], g["mv"],
                    np.float32(g["sxr0"] / pcw), np.float32(g["dsx"] / pcw),
                    np.float32(g["syr0"] / pch), np.float32(g["dsy"] / pch),
                    g["use_fe"], np.float32(g["fe"]), np.float32(g["rad"]),
                    np.float32(g["cref_x"]), np.float32(g["cref_y"]),
                    np.float32(g["csoft"]), np.float32(g["cadd"]), np.float32(g["op"]),
                    np.float32(g["inner_w"]), 1 if abs(g["bevel"]) > 1e-6 else 0,
                    np.float32(abs(g["bevel"])), np.float32(sgn),
                    np.float32(pos * g["inner_w"]),
                    np.float32(soft * max(1.0, g["inner_w"])),
                    np.float32(lx), np.float32(ly), np.float32(lz),
                    np.float32(ringc), np.float32(16 * _SCALE), np.float32(_MAXV)))
            _slot_ok(slot)
        pin = _gpu["pin_out"]
        py, pu, pv = _plane_views(pin)
        cvs[0].get(stream=st, out=py); cvs[1].get(stream=st, out=pu); cvs[2].get(stream=st, out=pv)
    st.synchronize()
    buf[:] = pin


def _gpu_shadow(cvs, g, sh):
    """Ombre portée : silhouette INCLINÉE floutée, posée dans un rect élargi (le flou doit
    s'épandre vers l'extérieur), puis mélangée SOUS la box. Rebâtie à chaque trame — sur GPU
    c'est assez peu cher pour qu'aucun cache ne se justifie."""
    br = int(round(float(sh.get("blur", 8.0))))
    pad = (3 * br + 2) if br > 0 else 0
    if pad % 2: pad += 1
    aw, ah = g["aw"], g["ah"]
    sw, shh = aw + 2 * pad, ah + 2 * pad
    n = sw * shh
    if n > _gpu["sil"].size:
        return
    sil = _gpu["sil"][:n].reshape(shh, sw)
    tmp = _gpu["tmp"][:n].reshape(shh, sw)
    sil.fill(0.0)
    _gpu["sil_k"](((aw + 15) // 16, (ah + 15) // 16), (16, 16),
                  (sil, sw, shh, pad, aw, ah, np.float32(g["s"]), np.float32(g["c"]),
                   np.float32(g["hw"]), np.float32(g["hh"])))
    if br > 0:
        gh = (((sw + 127) // 128 + 7) // 8, (shh + 7) // 8)
        gv = ((sw + 7) // 8, ((shh + 127) // 128 + 7) // 8)
        for _ in range(3):
            _gpu["bh"](gh, (8, 8), (sil, tmp, shh, sw, br))
            _gpu["bv"](gv, (8, 8), (tmp, sil, shh, sw, br))
    sYa, sUa, sVa = _hex_to_yuv(sh.get("color") or "#000000")
    amp = (float(sh.get("opacity", 50.0)) / 100.0) * g["alpha"]
    sx0 = g["x0"] + int(round(float(sh.get("dx", 8.0)))) - pad
    sy0 = g["y0"] + int(round(float(sh.get("dy", 8.0)))) - pad
    sx0 -= sx0 % _CW; sy0 -= sy0 % _CH
    for p, (pcw, pch, col) in enumerate(((1, 1, sYa), (_CW, _CH, sUa), (_CW, _CH, sVa))):
        pw, ph = sw // pcw, shh // pch
        if pw < 1 or ph < 1:
            continue
        _gpu["sha"](((pw + 15) // 16, (ph + 15) // 16), (16, 16),
                    (cvs[p], WIDTH // pcw, HEIGHT // pch, sil, sw, shh, sx0, sy0,
                     pcw, pch, np.float32(amp), np.float32(col), np.float32(_MAXV)))


def _stamp_for(idx, b, alpha):
    """Stamp de la box `idx`, depuis le CACHE (reconstruit seulement si la clé a changé)."""
    key = _adv_key(b, alpha)
    cur = _stamps.get(idx)
    if cur is not None and cur[0] == key:
        _stamp_stats["hits"] += 1
        st = cur[1]
    else:
        t0 = time.monotonic()
        st = _build_stamp(b, alpha)
        _stamp_stats["builds"] += 1
        _stamp_stats["build_ms"] += (time.monotonic() - t0) * 1000.0
        _stamps[idx] = (key, st)
    if st is None:
        return None
    # La POSITION ne fait pas partie de la clé (translation pure = stamp réutilisé tel quel) :
    # on la réapplique ici, à coût nul.
    if b.get("aspect") == "free":
        sx = b.get("size_x") or b.get("size", 0.45); sy = b.get("size_y") or b.get("size", 0.45)
    else:
        sx = sy = b.get("size", 0.45)
    crop = _eff_crop(b)
    cl = _f(crop.get("l", 0.0), 0.0, 0.9, 0.0); cr_ = _f(crop.get("r", 0.0), 0.0, 0.9, 0.0)
    ct = _f(crop.get("t", 0.0), 0.0, 0.9, 0.0); cb = _f(crop.get("b", 0.0), 0.0, 0.9, 0.0)
    if cl + cr_ >= 0.95: cl = cr_ = 0.0
    if ct + cb >= 0.95: ct = cb = 0.0
    full_w = WIDTH * float(sx); full_h = HEIGHT * float(sy)
    vis_cx = b["x"] * WIDTH + (cl - cr_) * 0.5 * full_w
    vis_cy = b["y"] * HEIGHT + (ct - cb) * 0.5 * full_h
    x0 = int(vis_cx - st["w"] / 2); y0 = int(vis_cy - st["h"] / 2)
    if x0 % 2: x0 -= 1
    if y0 % 2: y0 -= 1
    if x0 != st["x0"] or y0 != st["y0"]:
        dx = x0 - st["x0"]; dy = y0 - st["y0"]
        if st["shadow"] is not None:
            st["shadow"]["x0"] += dx
            st["shadow"]["y0"] += dy
        if st.get("op_rect") is not None:
            ox0, oy0, ox1, oy1 = st["op_rect"]
            st["op_rect"] = (ox0 + dx, oy0 + dy, ox1 + dx, oy1 + dy)
        st["x0"] = x0; st["y0"] = y0
    if st["x0"] + st["w"] <= 0 or st["y0"] + st["h"] <= 0 or st["x0"] >= WIDTH or st["y0"] >= HEIGHT:
        return None
    return st


def _blend_rows(dst, y_a, y_b, x_a, x_b, k, a16, marg, a_scalar=None):
    """Compose `k` sur `dst[y_a:y_b, x_a:x_b]` — REPLI NUMPY (le kernel mvk fait la même chose
    en une passe, cf. _adv_plane). TROIS RÉGIMES (du moins cher au plus cher) :
      • marg == 0 (masque None)  → COPIE DIRECTE : la box est opaque, rien à mélanger.
      • a_scalar (marg == -1)    → blend à alpha CONSTANT : pas de masque à lire.
      • masque 2D                → seul le RUBAN de `marg` px est mélangé, l'intérieur est COPIÉ.
        (mesuré : blend plein rect 7,2 ms/box en 1080p, ruban ~0,1 ms).
    ⚠ ACCUMULATION uint32 (0.10.0) : l'accumulateur uint16 d'origine DÉBORDAIT en 10/12 bits
    (4095·256 > 65535 → repliement, image cassée). Octets INCHANGÉS en 8 bits (255·256 tenait)."""
    h = y_b - y_a; w = x_b - x_a
    if h <= 0 or w <= 0:
        return
    if a_scalar is not None:                        # opacité uniforme → alpha scalaire
        av = int(round(max(0.0, min(1.0, a_scalar)) * 256.0))
        if av >= 256:
            dst[y_a:y_b, x_a:x_b] = k
            return
        rr = dst[y_a:y_b, x_a:x_b].astype(np.uint32)
        dst[y_a:y_b, x_a:x_b] = ((rr * (256 - av) + k.astype(np.uint32) * av) >> 8).astype(_NP_DT)
        return
    if a16 is None or marg <= 0:                    # box pleinement opaque → copie directe
        dst[y_a:y_b, x_a:x_b] = k
        return
    # ★ BUG CORRIGÉ (0.12.2) — « l'intérieur est opaque, on le COPIE » : FAUX EN CHROMA 4:2:0.
    # Le raccourci d'origine ne mélangeait que le RUBAN de `marg` px et COPIAIT l'intérieur, en
    # supposant qu'il y vaut alpha = 256. Vrai en luma (avec une opacité globale, `marg` couvre
    # tout le rect) — mais en 4:2:0 la marge chroma est DEUX FOIS PLUS PETITE (`marg // 2`) alors
    # que le rect chroma l'est aussi : l'intérieur chroma n'est PAS vide, et son alpha n'est PAS
    # 256 dès qu'une opacité globale est en jeu. Résultat : une box semi-transparente avec
    # adoucissement ou coins arrondis gardait, EN 4:2:0, une CHROMA À PLEINE OPACITÉ à l'intérieur
    # (la luma fondait, la couleur non) — teinte fausse, en silence, depuis toujours.
    # Le kernel mvk, lui, mélange PIXEL PAR PIXEL : c'est le repli qui mentait. Le repli fait
    # désormais la même chose que le kernel (blend plein rect sous masque) — un repli qui diverge
    # du kernel, c'est un bug qui n'apparaît QUE quand le kernel ne charge pas : invisible au banc,
    # bien réel en prod. Le masque vaut 256 partout où la box est opaque : le résultat y est
    # identique à la copie (aucune régression), et JUSTE là où il ne l'est pas.
    rr = dst[y_a:y_b, x_a:x_b].astype(np.uint32)
    dst[y_a:y_b, x_a:x_b] = ((rr * (256 - a16) + k.astype(np.uint32) * a16) >> 8).astype(_NP_DT)


def _gather(src, flat, riy, cix):
    """Rééchantillonnage nearest du plan source : carte d'index PLAT 2D (box tournée) ou
    cartes 1D séparables (box droite). C'est LE poste que mvk fusionne avec le blend."""
    if flat is not None:
        return np.take(src.ravel(), flat)
    return src[riy][:, cix]


def _adv_plane(dst, src, flat, riy, cix, ring_a, ring_c, ring_marg, a16, marg, a_scalar,
               fused3=None):
    """MODE AVANCÉ — compose UN plan (Y, U ou V) sur la région `dst` : gather ⊗ ruban de
    bordure ⊗ blend alpha. Chemin mvk = UNE passe C (bit-exact) ; sinon REPLI numpy, qui est
    exactement le code d'origine (plusieurs passes)."""
    if fused3 is not None:
        inv_a, a1, c2 = fused3
        if _mvk_try("fused", bobimxl.mvk_spl_fused_into, dst, src, inv_a, a1, c2,
                    flat=flat, row_idx=riy, col_idx=cix, maxv=_MAXV):
            return
        k = _gather(src, flat, riy, cix)
        dst[:] = (((dst.astype(np.uint32) * inv_a) >> 8)
                  + ((k.astype(np.uint32) * a1) >> 8) + c2).clip(0, _MAXV).astype(_NP_DT)
        return
    # Régime de blend (identique à _blend_rows) : masque 2D, alpha scalaire, ou copie.
    mask = None; av = 256
    if a_scalar is not None:
        av = int(round(max(0.0, min(1.0, a_scalar)) * 256.0))
    elif a16 is not None and marg > 0:
        mask = a16
    if _mvk_try("compose", bobimxl.mvk_spl_compose_into, dst, src, flat=flat, row_idx=riy,
                col_idx=cix, ring_a=ring_a, ring_c=ring_c, alpha=mask, a_scalar=av):
        return
    k = _gather(src, flat, riy, cix)
    if ring_a is not None:
        # ★ Ruban peint sur TOUT le rect (le masque `ring_a` est nul ailleurs — donc c'est un
        # no-op hors ruban). La 0.9.x n'évaluait QUE les 4 bandes de bord du rect COURANT :
        # correct en trame entière (le bord du rect EST le bord de la box), FAUX en mode
        # TRANCHE — le rect courant est une BANDE, et le bord haut/bas de la box tombe alors
        # au MILIEU de la bande, hors des 4 bandes évaluées ⇒ bordure MANQUANTE sur les lignes
        # concernées. Le kernel mvk applique le ruban par pixel : le repli fait pareil.
        k[:] = ((k.astype(np.uint32) * (256 - ring_a)
                 + ring_c.astype(np.uint32) * ring_a) >> 8).astype(_NP_DT)
    _blend_rows(dst, 0, dst.shape[0], 0, dst.shape[1], k, a16, marg, a_scalar)


def _ring_slices(h, w, m):
    """Les 4 bandes du RUBAN de bord d'un rect h×w d'épaisseur m (haut, bas, gauche, droite)."""
    return ((0, m, 0, w), (h - m, h, 0, w), (m, h - m, 0, m), (m, h - m, w - m, w))


def _adv_need_row(st, b0, b1):
    """MODE TRANCHE — dernière ligne SOURCE requise par la bande [b0,b1) pour le stamp `st`,
    ou -1 si la box n'intersecte pas la bande. AVEC ROTATION l'échantillonnage est DIAGONAL :
    on utilise le max par ligne précalculé (`row_need`), sinon on lirait des lignes source non
    encore committées (déchirure)."""
    y0, y1 = st["y0"], st["y0"] + st["h"]
    a = max(y0, b0); b = min(y1, b1)
    if a >= b:
        return -1
    lb = b - y0
    if st["row_need"] is not None:
        return int(st["row_need"][min(lb - 1, st["h"] - 1)])
    need = int(st["iy"][min(lb - 1, st["h"] - 1)])
    if st["iy2"] is not None:
        lcb = (b - y0) // _CH
        if lcb > 0:
            need = max(need, (int(st["iy2"][min(lcb - 1, st["iy2"].size - 1)]) + 1) * _CH - 1)
    return need


def _paint_shadow_rows(oy, ou, ov, st, b0, b1):
    """OMBRE PORTÉE — assombrit le FOND (sous la box) sur les lignes [b0,b1). Masque flouté
    PRÉCALCULÉ → il ne reste qu'un blend. Peinte AVANT la box (z : sous elle)."""
    sh = st.get("shadow")
    if sh is None:
        return
    x0, y0, w, h = sh["x0"], sh["y0"], sh["w"], sh["h"]
    Ys, Us, Vs = sh["yuv"]

    # ★ L'ombre est ENTIÈREMENT RECOUVERTE par la box là où celle-ci est opaque : la peindre y
    # serait du travail jeté. On découpe donc le rect d'ombre en BANDES autour du rect opaque de
    # la box (découpe guillotine). Une ombre typique (box opaque, léger décalage) se réduit ainsi
    # à deux fines bandes en L au lieu d'un rect plein. Box semi-transparente (op_rect=None) →
    # aucune découpe : l'ombre DOIT transparaître (correction avant optimisation).
    opr = st.get("op_rect")
    for (rx0, ry0, rx1, ry1) in _sub_rects(x0, y0, x0 + w, y0 + h, opr):
        ya = max(ry0, b0, 0); yb = min(ry1, b1, HEIGHT)
        xa = max(rx0, 0); xb = min(rx1, WIDTH)
        if ya >= yb or xa >= xb:
            continue
        ra, rb = ya - y0, yb - y0
        ca, cb_ = xa - x0, xb - x0
        a = sh["a"][ra:rb, ca:cb_]
        reg = oy[ya:yb, xa:xb]
        if reg.shape != a.shape:
            continue
        # mvk : blend d'une couleur unie sous masque en 1 passe ; sinon repli numpy (uint32 —
        # l'accumulateur uint16 d'origine débordait en 10/12 bits ; 8 bits inchangé).
        if not _mvk_try("solid", bobimxl.mvk_spl_solid_into, reg, a, int(Ys)):
            reg[:] = ((reg.astype(np.uint32) * (256 - a) + int(Ys) * a) >> 8).astype(_NP_DT)
        cya, cyb = ya // _CH, yb // _CH
        cxa, cxb = xa // _CW, xb // _CW
        if cyb <= cya or cxb <= cxa:
            continue
        a2 = sh["a2"][(ya - y0) // _CH:(ya - y0) // _CH + (cyb - cya),
                      (xa - x0) // _CW:(xa - x0) // _CW + (cxb - cxa)]
        for plane, val in ((ou, int(Us)), (ov, int(Vs))):
            r2 = plane[cya:cyb, cxa:cxb]
            if r2.shape != a2.shape:
                continue
            if not _mvk_try("solid", bobimxl.mvk_spl_solid_into, r2, a2, val):
                r2[:] = ((r2.astype(np.uint32) * (256 - a2) + val * a2) >> 8).astype(_NP_DT)


def _sub_rects(x0, y0, x1, y1, hole):
    """Découpe le rect [x0,x1)×[y0,y1) en bandes NON CHEVAUCHANTES excluant `hole` (découpe
    guillotine : haut, bas, gauche, droite). `hole` None ou disjoint → le rect entier."""
    if hole is None:
        return [(x0, y0, x1, y1)]
    hx0, hy0, hx1, hy1 = hole
    hx0 = max(hx0, x0); hy0 = max(hy0, y0); hx1 = min(hx1, x1); hy1 = min(hy1, y1)
    if hx0 >= hx1 or hy0 >= hy1:
        return [(x0, y0, x1, y1)]          # pas de recouvrement
    out = []
    if hy0 > y0: out.append((x0, y0, x1, hy0))          # bande haute
    if hy1 < y1: out.append((x0, hy1, x1, y1))          # bande basse
    if hx0 > x0: out.append((x0, hy0, hx0, hy1))        # bande gauche
    if hx1 < x1: out.append((hx1, hy0, x1, hy1))        # bande droite
    return out


def _place_adv_rows(oy, ou, ov, sy, su, sv, st, b0, b1):
    """MODE AVANCÉ — compose la box (rotation/miroir/ratio libre + feather/coins/opacité +
    bordure 3D) sur les lignes [b0,b1). Fonctionne à l'identique en TRAME ENTIÈRE (b0=0,
    b1=HEIGHT) et en MODE TRANCHE (une bande) : une seule implémentation, donc pas de dérive
    entre les deux chemins.

    Par trame il ne reste que : un GATHER (carte d'index CACHÉE) + un blend de RUBAN."""
    x0, y0, w, h = st["x0"], st["y0"], st["w"], st["h"]
    ya = max(y0, b0, 0); yb = min(y0 + h, b1, HEIGHT)
    xa = max(x0, 0); xb = min(x0 + w, WIDTH)
    if ya >= yb or xa >= xb:
        return
    ra, rb = ya - y0, yb - y0          # lignes locales (dans la box)
    ca, cb_ = xa - x0, xb - x0         # colonnes locales
    fu = st.get("fused")

    # ── 1) PLAN Y : gather (carte d'index CACHÉE : plate 2D si tournée, sinon 1D séparable)
    #    ⊗ ruban de bordure ⊗ blend — UNE passe mvk, sinon repli numpy (cf. _adv_plane).
    fl = st["flat"].reshape(h, w)[ra:rb, ca:cb_] if st["flat"] is not None else None
    riy = st["iy"][ra:rb] if st["flat"] is None else None
    cix = st["ix"][ca:cb_] if st["flat"] is None else None
    _adv_plane(oy[ya:yb, xa:xb], sy, fl, riy, cix,
               (st["ring_a"][ra:rb, ca:cb_] if (fu is None and st["ring_a"] is not None) else None),
               (st["ring_y"][ra:rb, ca:cb_] if (fu is None and st["ring_a"] is not None) else None),
               st["ring_marg"],
               (st["a"][ra:rb, ca:cb_] if st["a"] is not None else None),
               st["marg"], st["a_scalar"],
               fused3=((fu["inv_a"][ra:rb, ca:cb_], fu["A1"][ra:rb, ca:cb_],
                        fu["C2y"][ra:rb, ca:cb_]) if fu is not None else None))

    # ── 2) CHROMA (mêmes opérations, plans sous-échantillonnés)
    cya, cyb = ya // _CH, yb // _CH
    cxa, cxb = xa // _CW, xb // _CW
    if cyb <= cya or cxb <= cxa:
        return
    cra = (ya - y0) // _CH; cca = (xa - x0) // _CW
    crb = cra + (cyb - cya); ccb = cca + (cxb - cxa)
    ch2, cw2 = h // _CH, w // _CW
    if crb > ch2 or ccb > cw2:
        return
    fl2 = st["flat2"].reshape(ch2, cw2)[cra:crb, cca:ccb] if st["flat2"] is not None else None
    riy2 = st["iy2"][cra:crb] if st["flat2"] is None else None
    cix2 = st["ix2"][cca:ccb] if st["flat2"] is None else None
    a2 = st["a2"][cra:crb, cca:ccb] if st["a2"] is not None else None
    m2 = st["marg"] // max(1, _CH) if st["marg"] > 0 else st["marg"]
    for splane, oplane, ringp, c2f in ((su, ou, st["ring_u"], "C2u"), (sv, ov, st["ring_v"], "C2v")):
        r2 = oplane[cya:cyb, cxa:cxb]
        if a2 is not None and r2.shape != a2.shape:
            continue
        _adv_plane(r2, splane, fl2, riy2, cix2,
                   (st["ring_a2"][cra:crb, cca:ccb]
                    if (fu is None and st["ring_a2"] is not None and ringp is not None) else None),
                   (ringp[cra:crb, cca:ccb]
                    if (fu is None and st["ring_a2"] is not None and ringp is not None) else None),
                   st["ring_marg"], a2, m2, st["a_scalar"],
                   fused3=((fu["inv_a2"][cra:crb, cca:ccb], fu["A1_2"][cra:crb, cca:ccb],
                            fu[c2f][cra:crb, cca:ccb]) if fu is not None else None))


# ═══ COUCHE « ART » (habillage graphique) — la fonction phare du SuperSource ═════════════════
# Un décor/logo/cadre posé AU-DESSUS ou EN DESSOUS des boxes, avec incrustation LUMA ou CHROMA
# (seuil/clip, gain, inversion) → un décor de plateau avec des « trous » où se logent les caméras.
#
# ★ POURQUOI UNE IMAGE IMPORTÉE ET NON UNE 6ᵉ ENTRÉE VIDÉO (l'ATEM prend une source vidéo) :
#   c'est un ARBITRAGE MESURÉ, pas une facilité. L'incrustation d'une source VIVANTE impose de
#   recalculer le masque de clé à CHAQUE TRAME sur le cadre PLEIN — banc nœud 30, 1080p, Y+U+V :
#       ART = 6ᵉ entrée vidéo live   → 21,7 ms/trame   (108 % du budget 50 fps À ELLE SEULE)
#       ART = image importée         →  0,9 ms/trame   (clé PRÉCALCULÉE une fois à l'import)
#   Une entrée vidéo consommerait donc tout le budget de trame avant même de composer les boxes.
#   L'image importée est en outre le cas d'usage réel de l'habillage (décor/logo fixe), et c'est
#   le motif déjà éprouvé du multiview (overlays PIL → YUV, cachés). Le jour où l'ART vivant
#   sera nécessaire, il faudra le noyau C (mvk) — chantier en soi, cf. help.md.
_art_lock = threading.Lock()
_art = {{"enabled": False, "above": True, "image": "", "key": "alpha", "clip": 0.15,
         "gain": 1.0, "invert": False, "opacity": 1.0, "chroma_key": "#00b140",
         "baked": None, "err": "",
         # ── DIAGNOSTIC DE L'HABILLAGE (0.10.1) — cf. _bake_art. Un habillage OPAQUE PARTOUT et
         # posé AU-DESSUS efface toute la composition : les box disparaissent, et jusqu'ici le
         # plugin ne disait RIEN (échec silencieux : l'exploitant voit sa photo plein cadre et
         # n'a aucun moyen de comprendre). On MESURE et on DIT — on ne corrige PAS en douce
         # (forcer `above=false` ou baisser l'opacité serait décider à sa place : un fond plein
         # cadre peut être voulu).
         "has_alpha": None,     # l'image importée a-t-elle un canal de transparence ?
         "cover": None,         # opacité MOYENNE du masque cuit (0..1)
         "holes": None,         # part du cadre réellement TRANSPARENTE (α = 0) — les « trous »
         "warn": []}}           # codes d'avertissement (l'UI/i18n les traduit)


def _art_public():
    """État ART pour /state — SANS le blob base64 (une image 1080p en base64 pèse ~1 Mo : la
    renvoyer à chaque poll de /state saturerait l'UI). `has_image` suffit à l'UI."""
    with _art_lock:
        return {{k: _art[k] for k in ("enabled", "above", "key", "clip", "gain", "invert",
                                      "opacity", "chroma_key", "err", "has_alpha", "cover", "holes")}} | {{
            "has_image": bool(_art["image"]),
            "loaded": _art.get("baked") is not None,
            "warn": list(_art["warn"]),
        }}


def _bake_art():
    """Décode l'image ART (base64 PNG/JPEG) → plans YUV + masque alpha PRÉCALCULÉ selon le mode
    d'incrustation. Fait UNE FOIS à l'import/au changement de réglage (jamais par trame)."""
    with _art_lock:
        cfg = dict(_art)
    if not cfg["enabled"] or not cfg["image"]:
        with _art_lock:
            _art["baked"] = None; _art["err"] = ""
            _art["warn"] = []; _art["cover"] = None; _art["has_alpha"] = None
            _art["holes"] = None
        return
    try:
        from PIL import Image
        raw = cfg["image"]
        if "," in raw[:64] and raw.strip().startswith("data:"):
            raw = raw.split(",", 1)[1]
        im0 = Image.open(io.BytesIO(base64.b64decode(raw)))
        # ★ CANAL DE TRANSPARENCE : un JPEG (ou un PNG opaque) n'en a PAS → `convert("RGBA")`
        # fabrique un alpha à 255 partout → la clé « alpha » ne découpe RIEN → l'habillage est
        # plein cadre. C'est le piège n°1 de l'habillage, et il était muet.
        has_alpha = bool("A" in im0.getbands() or im0.info.get("transparency") is not None)
        im = im0.convert("RGBA")
        if im.size != (WIDTH, HEIGHT):
            im = im.resize((WIDTH, HEIGHT), Image.LANCZOS)
        # ★ MÊME CONVERSION QUE TOUT LE RESTE (0.12.2) : matrice de la colorimétrie DÉCLARÉE,
        # échelle de la profondeur, plage LÉGALE. L'habillage portait, lui aussi, une matrice
        # BT.601 pleine échelle CODÉE EN DUR : dans un pipeline 709/2020, l'image importée sortait
        # avec des couleurs FAUSSES (teintes décalées). Une seule conversion pour tout le plugin.
        arr = np.asarray(im).astype(np.float32) / 255.0        # RGBA en 0..1 (sRGB)
        r, g, b, al = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
        y01, u01, v01 = _yuv01(r, g, b)                        # 0..1 / ±0,5, matrice DÉCLARÉE
        Yf, Uf, Vf = _encode_yuv(y01, u01, v01, legal=True)    # échantillons du format

        mode = cfg["key"]
        clip = float(cfg["clip"]); gain = max(0.01, float(cfg["gain"]))
        if mode == "luma":
            # Clé sur la LUMA de la source (0..1), pas sur l'échantillon encodé : le seuil de
            # l'exploitant reste 0..1 quelle que soit la profondeur ou la plage.
            a = np.clip((y01 - clip) * gain, 0.0, 1.0)
        elif mode == "chroma":
            # Distance à la couleur de clé dans le plan (U, V) de la MÊME matrice → une clé verte
            # est la même en 601, 709 ou 2020 (elle suit la colorimétrie déclarée).
            kr_, kg_, kb_ = _hex_rgb01(cfg["chroma_key"] or "#00b140")
            _ky, ku, kv = _yuv01(kr_, kg_, kb_)
            du = u01 - ku; dv = v01 - kv
            dist = np.sqrt(du * du + dv * dv) / 0.5            # 0..1 (±0,5 = pleine amplitude)
            a = np.clip((dist - clip) * gain, 0.0, 1.0)        # proche de la clé → transparent
        else:                                                  # "alpha" : canal alpha du PNG
            a = np.clip((al - clip) * gain, 0.0, 1.0)
        if cfg["invert"]:
            a = 1.0 - a
        a *= float(cfg["opacity"]) / 100.0     # POURCENT → fraction

        a16 = (a * 256.0).astype(np.uint16)
        Yp = np.clip(Yf, 0, _MAXV).astype(_NP_DT)
        Up = np.clip(Uf, 0, _MAXV).astype(_NP_DT)[::_CH, ::_CW].copy()
        Vp = np.clip(Vf, 0, _MAXV).astype(_NP_DT)[::_CH, ::_CW].copy()
        a16_2 = a16[::_CH, ::_CW].copy()
        # ★ Décomposition OPAQUE / PARTIEL : un décor est fait de grandes zones pleines et de
        #   grands trous. Les zones OPAQUES se COPIENT (np.copyto), seule la frange PARTIELLE se
        #   mélange → mesuré 0,9 ms au lieu de 5,1 ms (blend plein cadre) en 1080p.
        opaque = a16 >= 255
        part = (a16 > 0) & ~opaque
        rows_o = np.nonzero(opaque.any(axis=1))[0]
        rows_p = np.nonzero(part.any(axis=1))[0]
        baked = {{
            "y": Yp, "u": Up, "v": Vp, "a": a16, "a2": a16_2,
            "opaque": opaque, "opaque2": opaque[::_CH, ::_CW].copy(),
            "oy0": int(rows_o[0]) if rows_o.size else 0,
            "oy1": int(rows_o[-1]) + 1 if rows_o.size else 0,
            "py0": int(rows_p[0]) if rows_p.size else 0,
            "py1": int(rows_p[-1]) + 1 if rows_p.size else 0,
        }}
        # ── DIAGNOSTIC (0.10.1) : l'habillage cache-t-il TOUT ? ────────────────────────────────
        # `cover` = part de la surface où le masque est OPAQUE (le fond et les box y sont
        # intégralement recouverts). Deux avertissements, JAMAIS une correction d'office :
        #   • no_alpha  : image SANS canal de transparence + clé « alpha » → alpha = 255 partout ;
        #   • covers_all: masque opaque sur ≥ 99 % de la surface ET habillage AU-DESSUS → la
        #                 composition (box comprises) est entièrement masquée.
        # `cover` = opacité MOYENNE du masque (pas la part de pixels PLEINEMENT opaques : une
        # photo sans alpha cuite avec le seuil par défaut donne α = 0,85 PARTOUT — aucun pixel
        # « plein », et pourtant les box sont noyées). `holes` = part réellement TRANSPARENTE
        # (α = 0) : c'est par là que les box se voient. Aucun trou + forte opacité = tout est
        # masqué, quel que soit le détail du masque.
        cover = float(a16.mean()) / 256.0
        holes = float((a16 == 0).mean())
        warn = []
        if mode == "alpha" and not has_alpha and not cfg["invert"]:
            warn.append("no_alpha")
        if cfg["above"] and holes < 0.01 and cover >= 0.75:
            warn.append("covers_all")
        with _art_lock:
            _art["baked"] = baked; _art["err"] = ""
            _art["has_alpha"] = has_alpha; _art["cover"] = round(cover, 4)
            _art["holes"] = round(holes, 4)
            _art["warn"] = warn
        log(f"split: ART cuit ({{mode}}) — opaque[{{baked['oy0']}}:{{baked['oy1']}}] "
            f"partiel[{{baked['py0']}}:{{baked['py1']}}] opacité_moyenne={{cover * 100:.1f}} % "
            f"trous(α=0)={{holes * 100:.1f}} % canal_alpha={{has_alpha}}", "info")
        # ★ JAMAIS EN SILENCE (anti-patron n°1 du projet) : l'habillage vient d'effacer toute la
        # composition — on le DIT, en nommant la cause et les remèdes. Rien n'est corrigé d'office.
        if "no_alpha" in warn:
            log("split: ATTENTION — l'image d'habillage n'a AUCUN canal de transparence "
                "(JPEG ou PNG opaque) et la clé est « alpha » : elle est donc OPAQUE PARTOUT. "
                "Remèdes : importer un PNG à fond transparent, choisir la clé « luma » ou "
                "« chroma », ou placer l'habillage EN DESSOUS des box.", "warning")
        if "covers_all" in warn:
            print(f"split: ATTENTION — l'habillage couvre tout le cadre (opacité moyenne "
                  f"{{cover * 100:.0f}} %, trous transparents {{holes * 100:.1f}} %) ET il est posé "
                  f"AU-DESSUS : il MASQUE les box (elles sont composées, puis recouvertes). Rien "
                  f"n'est corrigé d'office (un fond plein cadre peut être voulu) : décochez "
                  f"« au-dessus », changez de clé, ou baissez l'opacité.")
    except Exception as e:
        with _art_lock:
            _art["baked"] = None; _art["err"] = str(e)
            _art["warn"] = []; _art["cover"] = None; _art["holes"] = None
        log(f"split: ART illisible — {{e}}", "error")


def _paint_art_rows(oy, ou, ov, b0, b1):
    """Compose la couche ART sur les lignes [b0,b1). Tout est PRÉCALCULÉ (clé comprise) : il ne
    reste qu'une COPIE sur les zones opaques + un blend sur la frange partielle."""
    with _art_lock:
        bk = _art.get("baked")
    if bk is None:
        return
    # zones OPAQUES → copie directe
    ya = max(bk["oy0"], b0); yb = min(bk["oy1"], b1)
    if ya < yb:
        np.copyto(oy[ya:yb], bk["y"][ya:yb], where=bk["opaque"][ya:yb])
        cya, cyb = ya // _CH, yb // _CH
        if cyb > cya:
            for plane, src in ((ou, bk["u"]), (ov, bk["v"])):
                np.copyto(plane[cya:cyb], src[cya:cyb], where=bk["opaque2"][cya:cyb])
    # frange PARTIELLE → blend
    ya = max(bk["py0"], b0); yb = min(bk["py1"], b1)
    if ya < yb:
        # Accumulation uint32 (0.10.0) : uint16 débordait en 10/12 bits (cf. _blend_rows).
        a = bk["a"][ya:yb]
        reg = oy[ya:yb]
        reg[:] = ((reg.astype(np.uint32) * (256 - a) + bk["y"][ya:yb].astype(np.uint32) * a) >> 8).astype(_NP_DT)
        cya, cyb = ya // _CH, yb // _CH
        if cyb > cya:
            a2 = bk["a2"][cya:cyb]
            for plane, src in ((ou, bk["u"]), (ov, bk["v"])):
                r2 = plane[cya:cyb]
                if r2.shape != a2.shape:
                    continue
                r2[:] = ((r2.astype(np.uint32) * (256 - a2)
                          + src[cya:cyb].astype(np.uint32) * a2) >> 8).astype(_NP_DT)


# ═════════════════════════════════════════════════════════════════════════════════════════════
#  ★ POURQUOI CE BLOC EXISTE (0.11.0) — « le split ne compose AUCUNE box, et tout va bien »
#
#  BUG VÉCU EN PROD (conteneur 598) : le split publiait une image VIDE à la bonne cadence, sans
#  une seule erreur. `:8080` disait fps 25,0, budget 2,5 %, aucun repli, aucune exception… et
#  `slice.layers: 0`. Un split qui ne compose RIEN ressemblait EXACTEMENT à un split qui va bien.
#
#  CAUSE RACINE (reproduite au banc) : le producteur (`avsync`) a été REDÉPLOYÉ **après** le
#  split, et il a changé de format (1080p50 → 640×360p25). Le Reader du split, ouvert sur
#  l'ANCIENNE génération du flux, y est resté COLLÉ : cette génération est morte, sa tête est
#  FIGÉE, et ses grains font la taille de l'ANCIEN format → la garde format-fixe les rejetait à
#  chaque trame, EN SILENCE.
#  Et la garde « génération périmée » ne le rattrapait pas : elle testait `fi_out - hi > 250`,
#  or l'ancienne génération était sur la grille TAI **50 fps** (tête ≈ 8,92e10) et le split sur
#  la grille **25 fps** (fi_out ≈ 4,46e10) → la différence était **NÉGATIVE** (la tête morte est
#  « en avance ») → la garde ne se déclenchait JAMAIS. Le slot n'était jamais lâché, donc jamais
#  rouvert : mort définitive, muette.
#
#  DEUX CORRECTIFS, indissociables :
#  1. DÉTECTION ROBUSTE : une tête FIGÉE est périmée quelle que soit sa POSITION (|Δ| et non Δ),
#     et TOUT slot qui n'a rien donné d'utilisable depuis _REOPEN_AFTER trames est relâché
#     (close + GC + réouverture) — quelle que soit la raison. Une réouverture retombe sur la
#     génération VIVANTE (chemin déjà éprouvé : un redéploiement du producteur À FORMAT ÉGAL
#     était, lui, correctement rattrapé).
#  2. PLUS UN SEUL `continue` MUET : chaque renoncement est COMPTÉ par slot AVEC SA RAISON,
#     exposé sur `:8080` (`slots`), et loggé UNE FOIS par transition (pas à chaque trame). Le
#     rétablissement est loggé aussi. L'exception de la boucle de rendu n'est plus avalée.
# ═════════════════════════════════════════════════════════════════════════════════════════════
_REOPEN_AFTER = 50      # trames consécutives sans grain utilisable → on relâche le slot (≈ 2 s)
_SKIP_LABELS = {{
    "not_wired":  "slot non câblé",
    "input_absent": "flux introuvable (le producteur n'existe pas encore ?)",
    "no_grain":   "aucun grain lisible (producteur muet ou génération morte)",
    "format":     "grain ≠ format du canvas (producteur d'un AUTRE format, ou génération périmée)",
    "stamp_none": "empreinte nulle (box invisible : trop petite, hors cadre, alpha nul)",
    "geom_none":  "géométrie nulle (box invisible : trop petite ou hors cadre)",
    "exception":  "exception dans la boucle de rendu",
    "stale_gen":  "génération périmée (tête figée) → slot relâché et rouvert",
}}
_slot_skips = {{}}    # slot -> {{raison: compte cumulé}} — exposé sur :8080
_slot_state = {{}}    # slot -> [raison_courante, trames_consécutives, détail]


def _slot_skip(slot, reason, detail=""):
    """Renonce à composer le slot `slot` — COMPTÉ, EXPOSÉ, et loggé UNE FOIS par transition.
    Après _REOPEN_AFTER trames consécutives d'échec, le lecteur est RELÂCHÉ (close + GC) : la
    réouverture retombera sur la génération vivante du flux (c'est le remède au producteur
    redéployé). `not_wired` est un état normal (pas de fond câblé, par ex.) → aucune réouverture."""
    d = _slot_skips.setdefault(slot, {{}})
    d[reason] = d.get(reason, 0) + 1
    cur = _slot_state.get(slot)
    if cur is None or cur[0] != reason:
        _slot_state[slot] = [reason, 1, detail]
        if reason != "not_wired":
            log(f"split: slot {{slot}} NON COMPOSÉ — {{_SKIP_LABELS.get(reason, reason)}}"
                + (f" ({{detail}})" if detail else ""), "warning")
        return
    cur[1] += 1
    if reason != "not_wired" and cur[1] % _REOPEN_AFTER == 0:
        # Rien d'utilisable depuis ≈ 2 s : la cause la plus probable est un flux dont la
        # GÉNÉRATION a changé sous nos pieds (producteur redéployé). On lâche le lecteur ; il
        # sera rouvert au tour suivant, sur la génération vivante.
        log(f"split: slot {{slot}} muet depuis {{cur[1]}} trames "
            f"({{_SKIP_LABELS.get(reason, reason)}}) — RELÂCHE du lecteur et réouverture", "warning")
        _drop_slot(slot)


def _slot_ok(slot):
    """Le slot compose de nouveau — remet les compteurs d'état à zéro et le DIT (une fois)."""
    cur = _slot_state.pop(slot, None)
    if cur is not None and cur[0] != "not_wired":
        log(f"split: slot {{slot}} RÉTABLI (après {{cur[1]}} trames « {{cur[0]}} »)", "info")


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
    _sl_tot["waits"] += 1
    left = min(deadl - time.monotonic(), L["budget"] / 1e9)
    w0 = time.monotonic()
    g = L["rd"].get_slice(L["fi"], need_k, timeout_ns=max(1, int(left * 1e9))) if left > 0 else None
    dw = int((time.monotonic() - w0) * 1e9)
    L["budget"] -= dw
    _sl_tot["waited_ms"] += dw / 1e6
    if g is not None:
        L["valid"] = max(need_k, int(g[1].validSlices or need_k))
    else:
        bk = _sl_backoff.get(L["slot"])
        _sl_backoff[L["slot"]] = [L["fi"], (bk[1] + 1 if bk and bk[0] == L["fi"] else 1)]
        gp = L["rd"].get(L["fi"] - 1, timeout_ns=2_000_000)
        if gp is not None and len(gp[2]) == V_FRAME_SIZE:
            L["sy"], L["su"], L["sv"] = _plane_views(gp[2])
        _sl_dbg[3] += 1
        _sl_tot["fallbacks"] += 1
        L["valid"] = L["total"]        # plus aucune attente pour les bandes suivantes
    return dw


def _slice_frame(grid_ref, fi_out, tr, boxes, ts_in_per_slot, bg_fill=("solid", None), bg_is_src=True):
    """MODE TRANCHE — compose et PUBLIE la trame BANDE PAR BANDE (SLICE_H lignes/bande) :
    fond + boxes suivis CHACUN sur SON grain via get_slice, sortie committée progressivement
    (validSlices=k+1 → l'aval démarre sur la 1ʳᵉ bande). Budget par couche + backoff des
    entrées mortes + repli grain complet : la sortie n'est JAMAIS bloquée par une source.
    Commit final TOUJOURS (finally) : un grain laissé partiel ne serait jamais lisible.
    Renvoie le cumul des attentes get_slice (à EXCLURE de own_latency_ms)."""
    global _sl_dbg, _stamp_last_out
    wait_ns = 0
    # 1) plan des couches à composer (fond, puis les boxes dans l'ordre de superposition `z`).
    # FOND : `source` → slot 4 lu comme une couche ; `none`/`color` → remplissage par bande
    # (memset des 3 plans : la couleur est DÉJÀ convertie et cachée — jamais par trame).
    plan = ([(4, None, 1.0)] if bg_is_src else []) + _plan_layers(tr, boxes)
    # 2a) CIBLE (0.24.4) : définie par l'HORLOGE, jamais par une source. "fastest" = le créneau
    #     courant (grid_ref) ; "fixed_1" = le créneau précédent. Toutes les couches sur la grille
    #     sont lues à cette cible, et la sortie la PORTE (cf. 3).
    #     ⛔ Plus jamais « la plus ancienne des têtes » (0.24.2/0.24.3) : au réveil, une caméra 2110
    #     n'a pas encore commencé le créneau courant (1ʳᵉ tranche à 1,4 ms) → la plus ancienne tête
    #     valait TOUJOURS créneau − 1, soit une image de retard forcée (mesuré le 2026-10-25 sur
    #     dl360-1 : source publiée au créneau X, split au créneau X+1, 200/200). Et une source en
    #     panne, figée dans la bande, retenait toutes les autres.
    with state_lock:
        _lmode = state["latency_mode"]
    target = grid_ref if _lmode == "fastest" else grid_ref - 1
    # Budget d'attente de la 1ʳᵉ tranche, ABSOLU et partagé par toutes les couches : une
    # demi-période. Au-delà, la 1ʳᵉ bande de sortie partirait trop tard pour l'aval (l'émetteur
    # 2110 réclame l'image ~3,6 ms avant l'époque suivante). En fixed_1 la cible est déjà là :
    # le budget ne sert qu'aux sources de plus d'une image de retard.
    _bind_deadl = time.monotonic() + 0.5 * interval
    # 2b) 1ʳᵉ tranche de chaque couche (le suivi des bandes suivantes se fait au fil du compose).
    layers = []
    dormant = 0
    for slot, b_, alpha in plan:
        g = None; st = None
        if slot != 4:
            # MODE AVANCÉ → stamp caché ; sinon géométrie historique (octet-identique).
            if _is_adv(b_):
                st = _stamp_for(slot, b_, alpha)
                if st is None:
                    _slot_skip(slot, "stamp_none")
                    continue
            else:
                g = _box_slice_geom(b_, alpha)
                if g is None:
                    _slot_skip(slot, "geom_none")
                    continue
        rd = ensure_input(slot)
        if rd is None:
            # fond absent → bandes noires ; box absente → non composée. Distingué : un slot NON
            # CÂBLÉ est un état normal ; un slot câblé dont le flux ne s'ouvre pas est un PROBLÈME.
            with state_lock:
                wired = bool(state["inputs"][slot])
            _slot_skip(slot, "input_absent" if wired else "not_wired",
                       (state["inputs"][slot] if wired else ""))
            continue
        try:
            hi = rd.head_index()
            got = None
            if FLOW:
                # GÉNÉRATION PÉRIMÉE (motif multiview flow) : tête FIGÉE = Reader collé sur
                # l'ancien ring d'un producteur recréé sous le même nom → drop (GC entre close et
                # reopen) ; rouvert à la trame suivante.
                # ★ FIX 0.11.0 — la garde testait `fi_out - hi > 250` : elle ne voyait qu'une tête
                # morte EN RETARD. Quand le producteur redéployé CHANGE DE CADENCE (1080p50 →
                # 640×360p25, cas du 598), l'ancienne génération est sur la grille TAI 50 fps et
                # la nouvelle sur celle à 25 fps : la tête morte est DEUX FOIS PLUS GRANDE que
                # notre index d'epoch → la différence est NÉGATIVE → la garde ne se déclenchait
                # JAMAIS, et le slot restait collé à vie sur un flux mort (grains à l'ancien
                # format, rejetés en silence par la garde format-fixe). C'est le FIGEMENT de la
                # tête qui trahit une génération morte, pas le SIGNE de l'écart → valeur ABSOLUE.
                if (hi != bobimxl.MXL_UNDEFINED_INDEX and abs(fi_out - hi) > 250
                        and hi == _sl_lasthead.get(slot)):
                    _slot_skip(slot, "stale_gen", "tête figée à %d, grille à %d" % (hi, fi_out))
                    _drop_slot(slot)
                    _sl_lasthead.pop(slot, None)
                    continue
                _sl_lasthead[slot] = hi
            # Cible : l'index COMMUN (2a) pour une source sur la grille ou pas encore ouverte ;
            # source HORS GRILLE → sa tête, comme avant.
            on_grid = (hi == bobimxl.MXL_UNDEFINED_INDEX or abs(hi - grid_ref) <= 2)
            tgt = target if on_grid else hi
            _left = max(1, int((_bind_deadl - time.monotonic()) * 1e9))
            got = rd.get_slice(tgt, 1, timeout_ns=_left)
            if got is None and tgt != hi and hi != bobimxl.MXL_UNDEFINED_INDEX:
                got = rd.get_slice(hi, 1, timeout_ns=1_000_000)   # cible absente → grain courant
            if got is None:
                got = rd.get_latest()      # flux whole-frame / tête pas prête → grain complet
            if (got is not None and len(got[2]) == V_FRAME_SIZE // 2
                    and _il_order(rd) is not None):
                # Source ENTRELACÉE (grain = champ) : le producteur committe par CHAMPS → pas de
                # suivi par tranches possible ; repli whole-frame TISSÉ (cf. lire_frame_yuv).
                fr, ts = lire_frame_yuv(rd)
                if fr is None:
                    _slot_skip(slot, "no_grain", "source entrelacée : tissage impossible")
                    continue
                if ts: ts_in_per_slot[slot] = ts
                sy, su, sv = _plane_views(np.frombuffer(fr, dtype=np.uint8))
                layers.append({{"slot": slot, "rd": rd, "fi": int(got[0]), "sy": sy, "su": su,
                                "sv": sv, "total": 1, "valid": 1, "islh": HEIGHT, "g": g, "st": st,
                                "budget": int(interval * 1e9), "ongrid": False}})
                _slot_ok(slot)
                continue
            if got is None:
                _slot_skip(slot, "no_grain")
                continue
            if len(got[2]) != V_FRAME_SIZE:
                # ★ Garde format-fixe — LA garde qui, muette, a fait passer le 598 pour sain :
                # elle rejetait TOUS les grains d'une génération périmée restée à l'ancien format.
                _slot_skip(slot, "format",
                           "grain %d o ≠ canvas %d o (%dx%d %s %d bits)"
                           % (len(got[2]), V_FRAME_SIZE, WIDTH, HEIGHT, CHROMA, BIT_DEPTH))
                continue
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
            is_dormant = bk is not None and bk[1] >= 2
            if is_dormant:
                # DORMANTE : 2 timeouts consécutifs sur le MÊME grain (producteur mort
                # mi-grain) → bascule immédiate sur le dernier grain complet, ZÉRO sonde tant
                # que la tête n'avance pas.
                dormant += 1
                gp = rd.get(fi - 1, timeout_ns=2_000_000)
                if gp is not None and len(gp[2]) == V_FRAME_SIZE:
                    sy, su, sv = _plane_views(gp[2])
                valid = total
            # Sur la grille ET vivante : seule une telle couche fixe l'index de la sortie (une
            # dormante — producteur mort — ne doit pas la retenir).
            ongrid = on_grid and not is_dormant and abs(int(fi) - grid_ref) <= 2
            if on_grid and int(fi) != target:
                _stamp["late"] += 1
                _stamp["late_by_slot"][slot] = _stamp["late_by_slot"].get(slot, 0) + 1
            layers.append({{"slot": slot, "rd": rd, "fi": fi, "sy": sy, "su": su, "sv": sv,
                            "total": total, "valid": valid,
                            "islh": max(1, HEIGHT // total), "g": g, "st": st,
                            "budget": int(interval * 1e9), "ongrid": ongrid}})
            _slot_ok(slot)
        except Exception as _e:
            # ★ PLUS D'EXCEPTION AVALÉE : une exception dans la boucle de rendu était jusqu'ici
            # invisible (continue muet). Comptée, signée, loggée une fois — et le slot finit par
            # être relâché/rouvert si ça dure (cf. _slot_skip).
            _slot_skip(slot, "exception", "%s: %s" % (type(_e).__name__, _e))
            continue
    _sl_dbg = [len(layers), -1, 0, 0, dormant]
    bg = next((L for L in layers if L["slot"] == 4), None)
    # 3) grain de sortie + publication PROGRESSIVE bande par bande.
    # La sortie PORTE la cible : l'image qu'elle contient pour toute couche arrivée à temps. Une
    # couche en retard (comptée `late`) y figure avec sa dernière image. Aucune couche sur la
    # grille (fond uni seul, ou sources hors grille) → grille.
    src = [target] if any(L.get("ongrid") for L in layers) else []
    if _stamp_last_out - (min(src) if src else grid_ref) > CLOCK_JUMP_SLOTS:
        _reopen_output()
        _stamp["reanchored"] = _stamp.get("reanchored", 0) + 1
        _stamp_last_out = -1
    if src and min(src) <= _stamp_last_out:
        # RETENUE : index imposé ≤ dernier publié → open_grain rend un brouillon, commit ne publie
        # rien (cf. bobimxl.Writer.open_grain). Surtout pas src_index= ici (cf. _stamp).
        _stamp["held"] += 1
        _gidx, gi_o, vw_o = out.open_grain(index=min(src))
    elif src:
        _stamp["propagated"] += 1
        _gidx, gi_o, vw_o = out.open_grain(src_index=min(src))
        _stamp_last_out = int(_gidx)
    else:
        _stamp["grid"] += 1
        _gidx, gi_o, vw_o = out.open_grain()
        _stamp_last_out = int(_gidx)
    # `layers` porte rd (reader) et fi (index réellement lu) par couche — les deux chemins de
    # liaison renseignent ces deux clés.
    delai_etage.observe(out, _gidx,
                        [(L["rd"], L["fi"]) for L in layers
                         if L.get("rd") is not None and L.get("fi") is not None],
                        propage=bool(src))
    oy, ou, ovv = _plane_views(vw_o)
    nb = HEIGHT // SLICE_H
    deadl = time.monotonic() + (interval * 1.5)   # garde-fou GLOBAL (borne dure de la trame)
    if bg_fill[1] is None:
        bg_fill = ("solid", (16 * _SCALE, _NEUTRAL, _NEUTRAL))
    art_below = _art_below(); art_above = _art_above()
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
                # fond UNI (noir par défaut, ou couleur choisie) ou DÉGRADÉ — remplissage PAR
                # BANDE : le `t` d'une bande est un sous-ensemble contigu, aucun état à reporter.
                _bg_fill_band(oy, ou, ovv, bg_fill, b0, b1)
            if art_below:                       # ART SOUS les boxes (sur le fond)
                _paint_art_rows(oy, ou, ovv, b0, b1)
            for L in layers:
                g = L["g"]; st = L["st"]
                if g is None and st is None:
                    continue
                if st is not None:
                    # MODE AVANCÉ. L'ombre portée ne lit PAS la source (masque précalculé) →
                    # peinte sans attendre la moindre tranche ; la box, elle, attend ses lignes.
                    _paint_shadow_rows(oy, ou, ovv, st, b0, b1)
                    need = _adv_need_row(st, b0, b1)
                    if need < 0:
                        continue
                    wait_ns += _sl_ensure(L, min(L["total"], need // L["islh"] + 1), deadl)
                    _place_adv_rows(oy, ou, ovv, L["sy"], L["su"], L["sv"], st, b0, b1)
                    continue
                need = _band_need_row(g, b0, b1)
                if need < 0:
                    continue
                wait_ns += _sl_ensure(L, min(L["total"], need // L["islh"] + 1), deadl)
                _place_box_band(oy, ou, ovv, L["sy"], L["su"], L["sv"], g, b0, b1)
                _paint_border_band(oy, ou, ovv, g, b0, b1)
            if art_above:                       # ART AU-DESSUS des boxes (habillage à trous)
                _paint_art_rows(oy, ou, ovv, b0, b1)
            if k < nb - 1:
                out.commit(gi_o, valid_slices=k + 1)   # réveille l'aval sur la bande
    finally:
        out.commit(gi_o, valid_slices=None)   # commit final TOUJOURS (grain jamais partiel)
    return wait_ns


# ─── ART persisté (deploy_config.art) : cuit UNE FOIS au boot ───
_ac = CONFIG.get("art") or {{}}
if _ac:
    with _art_lock:
        for k in ("enabled", "above", "invert"):
            if k in _ac: _art[k] = bool(_ac[k])
        for k in ("image", "key", "chroma_key"):
            if k in _ac: _art[k] = str(_ac[k] or "")
        for k in ("clip", "gain", "opacity"):
            if k in _ac: _art[k] = _f(_ac[k], 0.0, 16.0, _art[k])
        if not _art["key"]: _art["key"] = "alpha"
    _bake_art()

out = _ouvrir_sortie(SHM_OUT)
if SLICE_ON:
    metrics["slice_mode"] = True
    log(f"split: MODE TRANCHE actif — {{SLICE_H}} lignes/bande ({{HEIGHT // SLICE_H}} tranches)"
        + (" [cadence=flow]" if FLOW else ""), "info")

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
# ★ Le choix de cadence (genlock verrouillé sur le fond) LIT `bg_is_src` AVANT que la trame
# courante ne le recalcule : c'est délibéré (valeur de la trame précédente — un changement de
# mode de fond n'a pas à décaler la cadence dans la même trame). Mais il n'était jamais initialisé
# → NameError à la PREMIÈRE itération, donc boucle de redémarrage, pour tout split en genlock.
# Ne se voyait pas tant qu'aucun déploiement ne partait sans fond câblé.
bg_is_src = (state["bg"]["mode"] == "source")

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
        _fi_out = _wait_next_slot()
    elif GENLOCK and bg_is_src:
        # ★ Le genlock se verrouille sur le FOND (slot 4) : c'est la référence de cadence. Un
        # fond UNI (none/color) n'est pas une source → aucune référence → cadence libre (else).
        # NB : en 0.11.x, `genlock` + fond non câblé publiait du NOIR sans composer les box (le
        # verrou attendait une référence qui n'arrivait jamais). Un split à 4 box câblées et sans
        # fond affichait donc du noir, en silence. Il compose désormais ses box sur le fond uni.
        ri, rw = _in_header(4)
        if ri == 0:
            # pas de fond câblé → noir continu cadencé (signal vivant)
            now = time.time()
            if now - last_black >= interval:
                _ecrire_sortie(out, empty_frame, out.next_index())
                frame_index += 1; last_black = now
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
                if tr.get("gto") is not None:  state["global"] = dict(tr["gto"])
                if tr.get("bgto") is not None: state["bg"] = dict(tr["bgto"])
                state["transition"] = None
                boxes = [dict(b) for b in state["boxes"]]
                tr = None
    if tr is not None:
        # progression BRUTE (linéaire) de la transition — la courbe d'accélération et le
        # décalage inter-boxes (stagger) sont appliqués PAR BOX dans _box_progress.
        tr = dict(tr)
        tr["_p_raw"] = max(0.0, min(1.0,
                           (time.time() - tr["start"]) * 1000.0 / max(1, tr["duration_ms"])))

    # FOND effectif de la trame : mode + couleur (interpolés si une transition est en cours).
    with state_lock:
        bg_cur = dict(state["bg"])
    if tr is not None and tr.get("bgto") is not None:
        bg_cur = _norm_bg(_interp_bg(tr.get("bgfrom") or bg_cur, tr["bgto"],
                                     _ease(tr["_p_raw"], tr.get("curve") or "linear")))
    bg_is_src = (bg_cur["mode"] == "source")
    # FOND UNI (3 memset, octet-identique) ou DÉGRADÉ (empreinte cachée → passe pixel, kernel C).
    if bg_cur["mode"] == "gradient":
        bg_fill = ("grad", _grad_prep(bg_cur))
    else:
        bg_fill = ("solid", _bg_values(bg_cur))

    ts_in_per_slot = {{}}
    ts_cycle_start = time.time_ns()   # début du compositing (fond détecté + boxes alignées) → base transit/own
    _sl_waited = 0   # MODE TRANCHE : cumul des attentes get_slice de la trame (exclu de own)

    if SLICE_ON:
        # MODE TRANCHE : composition + publication BANDE PAR BANDE (fond + boxes suivis par
        # tranches sur leur grain, commit progressif de la sortie → l'aval démarre sur la 1ʳᵉ
        # bande). Le chemin historique ci-dessous reste octet-identique (slice_mode off).
        try:
            _sl_waited = _slice_frame(_fi_out if FLOW else _grid_now(), _fi_out,
                                      tr, boxes, ts_in_per_slot,
                                      bg_fill=bg_fill, bg_is_src=bg_is_src)
        except Exception as _e:
            log(f"split: erreur slice_frame ignorée (frame {{frame_index}}) : {{_e}}", "warning")
        ts_out = time.time_ns()
    else:
        # ── Composition TRAME ENTIÈRE (chemin historique). Depuis 0.9.0 elle s'appuie sur les
        # MÊMES primitives par bandes que le mode tranche (bande = trame entière) : une seule
        # implémentation du compositing → les deux chemins ne peuvent plus diverger.
        # Réglages par défaut (aucun effet avancé) ⇒ strictement le rendu de 0.8.0.
        bg_frame, ts_bg = (_read_slot(4) if bg_is_src else (None, None))
        if ts_bg: ts_in_per_slot[4] = ts_bg
        if bg_frame:
            buf = np.frombuffer(bg_frame, dtype=np.uint8).copy()
            oy, ou, ovv = _plane_views(buf)
        else:
            # Fond UNI (noir/couleur) ou DÉGRADÉ — même helper que le mode tranche (bande = trame
            # entière) : un seul remplissage, les deux chemins ne peuvent pas diverger.
            buf = np.empty(V_FRAME_SIZE, dtype=np.uint8)
            oy, ou, ovv = _plane_views(buf)
            _bg_fill_band(oy, ou, ovv, bg_fill, 0, HEIGHT)

        if _art_below():                      # ART SOUS les boxes (décor de fond)
            _paint_art_rows(oy, ou, ovv, 0, HEIGHT)

        if GPU:
            # CHEMIN GPU : une seule montée du canvas, N kernels, une seule redescente. La
            # géométrie peut changer à chaque trame sans surcoût — il n'y a rien à reconstruire.
            _gl = []
            for slot, b, alpha in _plan_layers(tr, boxes):
                f, ts = _read_slot(slot)
                if ts: ts_in_per_slot[slot] = ts
                if not f:
                    continue
                _gl.append((slot, b, alpha, f))
            if _gl:
                _gpu_compose(buf, _gl)
                oy, ou, ovv = _plane_views(buf)   # `buf` réécrit en place → revues
        else:
         for slot, b, alpha in _plan_layers(tr, boxes):
            f, ts = _read_slot(slot)          # (compte et NOMME les renoncements — cf. _slot_skip)
            if ts: ts_in_per_slot[slot] = ts
            if not f:
                continue
            sy, su, sv = _plane_views(np.frombuffer(f, dtype=np.uint8))
            if _is_adv(b):
                st = _stamp_for(slot, b, alpha)
                if st is None:
                    _slot_skip(slot, "stamp_none"); continue
                _paint_shadow_rows(oy, ou, ovv, st, 0, HEIGHT)     # ombre SOUS la box
                _place_adv_rows(oy, ou, ovv, sy, su, sv, st, 0, HEIGHT)
            else:
                g = _box_slice_geom(b, alpha)                       # chemin 0.8.0 inchangé
                if g is None:
                    _slot_skip(slot, "geom_none"); continue
                _place_box_band(oy, ou, ovv, sy, su, sv, g, 0, HEIGHT)
                _paint_border_band(oy, ou, ovv, g, 0, HEIGHT)
            _slot_ok(slot)

        if _art_above():                      # ART AU-DESSUS des boxes (habillage à trous)
            _paint_art_rows(oy, ou, ovv, 0, HEIGHT)

        # ⚠ CHEMIN TRAME PLEINE : pas de délai d'étage publié ici. `_read_slot` ne rend que
        # (octets, ts) — ni reader ni index de grain — donc rien à confronter à l'index de sortie.
        # `delai_etage.publish()` renverra None, que l'interface affiche « délai non mesuré » :
        # c'est la règle (absence de mesure ≠ zéro). L'instrumenter demanderait de faire remonter
        # l'index par `_read_slot`, sur le chemin chaud. Le mode TRANCHE, lui, est mesuré.
        _oi = out.next_index()
        delai_etage.observe(out, _oi, [])   # compte le refus (cf. commentaire ci-dessus)
        _ecrire_sortie(out, buf, _oi)
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
            metrics["delai_etage_trames"] = delai_etage.publish()
            # ★ JAMAIS DE REPLI MUET : un kernel mvk qui refuse (forme/dtype inattendus) renvoie
            # False et l'appelant retombe en numpy — 5 à 20× plus lent. Compté ET signé ici.
            metrics["mvk_miss"] = dict(_mvk_miss)
            metrics["mvk_why"] = dict(_mvk_why)
            # Chemin de rendu EFFECTIF, et pourquoi. `render` dit ce qui a été DEMANDÉ, `gpu` ce
            # qui tourne VRAIMENT, `gpu_why` la raison du repli — un « auto » retombé sur CPU ne
            # doit pas se déduire d'un écart de fps.
            metrics["render"] = _RENDER
            metrics["gpu"] = GPU
            metrics["gpu_name"] = _GPU_NAME
            metrics["gpu_why"] = _GPU_WHY
            # ★ LE COMPTEUR QUI MANQUAIT (0.11.0) : combien de couches COMPOSÉES, et pour chaque
            # slot renoncé, POURQUOI. Un split qui ne compose rien ne peut plus passer pour sain :
            # `composed` tombe à 0 et `slots` nomme la cause (format / génération périmée / flux
            # absent / exception…), avec le nombre de trames consécutives.
            with state_lock:
                _wired = list(state["inputs"])
            metrics["slots"] = {{
                str(s): {{"shm": (_wired[s] if s < len(_wired) else ""),
                          "skips": dict(_slot_skips.get(s, {{}})),
                          "reason": (_slot_state[s][0] if s in _slot_state else None),
                          "frames": (_slot_state[s][1] if s in _slot_state else 0),
                          "detail": (_slot_state[s][2] if s in _slot_state else ""),
                          "why": (_SKIP_LABELS.get(_slot_state[s][0], "")
                                  if s in _slot_state else "")}}
                for s in range(N_INPUTS)
            }}
            metrics["composed"] = (_sl_dbg[0] if (SLICE_ON and _sl_dbg) else
                                   sum(1 for s in range(N_BOXES) if s not in _slot_state))
            # ── MODE AVANCÉ : santé du CACHE STATIQUE. `builds` qui grimpe en régime établi =
            # une clé de cache qui bouge sans raison → le précalcul serait refait par trame
            # (c'est LE mode de défaillance de perf du mode avancé). `hit_rate` doit valoir ~1
            # hors transition. `budget_pct` = coût propre / budget de trame.
            _tot = _stamp_stats["builds"] + _stamp_stats["hits"]
            _own = own_lat.avg()
            _pct = (round(_own / (interval * 1000.0) * 100.0, 1)) if _own is not None else None
            metrics["adv"] = {{
                "stamp_builds": _stamp_stats["builds"],
                "stamp_hits": _stamp_stats["hits"],
                "stamp_hit_rate": (round(_stamp_stats["hits"] / _tot, 3) if _tot else None),
                "stamp_build_ms_total": round(_stamp_stats["build_ms"], 1),
                "art": (_art.get("key") if _art.get("baked") is not None else None),
                # Habillage : couverture opaque + avertissements (« il masque tout ») — visibles
                # dans le monitoring, pas seulement dans le log du conteneur.
                "art_cover": _art.get("cover"),
                "art_holes": _art.get("holes"),
                "art_warn": list(_art.get("warn") or []),
                "budget_pct": _pct,
                "over_budget": bool(_pct is not None and _pct > 100.0),
                "rotated_boxes": _n_rot,
            }}
            # ★ JAMAIS EN SILENCE : si le compositing dépasse le budget de trame, le split
            # décroche (fps < consigne) — l'exploitant doit savoir POURQUOI. La rotation est de
            # loin le poste le plus lourd en numpy (rééchantillonnage 2D sur la boîte englobante) :
            # on la nomme. Alerte loggée UNE FOIS par franchissement (pas à chaque trame).
            if _pct is not None and _pct > 100.0:
                if not _over_warned[0]:
                    _over_warned[0] = True
                    _hint = ("noyau C mvk ACTIF" if _MVK else
                             "★ noyau C mvk INACTIF (image bobi-compute < 0.15 ?) — tout le "
                             "compositing tourne en repli numpy, 5 à 20× plus lent")
                    log(f"split: ATTENTION — compositing à {{_pct}} % du budget de trame "
                        f"({{interval * 1000.0:.0f}} ms @ {{FPS}} fps) : le split va décrocher. "
                        f"Boxes tournées : {{_n_rot}} ; {{_hint}} ; replis mvk : {{_mvk_miss}}. "
                        f"Poste n°1 restant : une rotation qui BOUGE (transition/curseur) "
                        f"reconstruit son empreinte 2D à chaque trame (~13 ms/box en 1080p).", "warning")
            elif _pct is not None and _pct < 90.0:
                _over_warned[0] = False
            if _sl_dbg:
                # Observabilité mode tranche (sémantique commune multiview/mixer) :
                # — DERNIÈRE trame (instantané) : layers (≡ tiles/inputs), valid0, waits,
                #   fallbacks, dormant, backoff, waited_ms (attentes get_slice, exclues de
                #   own_latency_ms), bands (bandes émises par trame) ;
                # — cumulatifs depuis le boot, suffixés _total (leçon banc mixer : un
                #   compteur cumulatif ambigu se lit mal) : les waits/replis intermittents
                #   entre deux échantillonnages (1 s) restent visibles ici.
                metrics["slice"] = {{"layers": _sl_dbg[0], "valid0": _sl_dbg[1],
                                    "waits": _sl_dbg[2], "fallbacks": _sl_dbg[3],
                                    "dormant": _sl_dbg[4], "backoff": len(_sl_backoff),
                                    "stamp": dict(_stamp, latency_mode=state["latency_mode"],
                                                  late_by_slot=dict(
                                                      (("bg" if k == 4 else "box%d" % (k + 1)), v)
                                                      for k, v in _stamp["late_by_slot"].items())),
                                    "bands": HEIGHT // SLICE_H,
                                    "waited_ms": round(_sl_waited / 1e6, 2),
                                    "waits_total": _sl_tot["waits"],
                                    "fallbacks_total": _sl_tot["fallbacks"],
                                    "waited_ms_total": round(_sl_tot["waited_ms"], 1)}}
