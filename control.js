// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// Split — UI de contrôle embarquée (plugin), montée par le shell Traitements via
// window.MXLPlugins.split.mount(el, vmid, ctx).
//
// ── D'où vient chaque morceau (rien n'est inventé ici) ────────────────────────────────────
//  • LANGAGE VISUEL du panneau de réglages : le CORRECTEUR DE COULEUR (plugins/color_corrector)
//    — LA référence d'UI du projet. On en reprend : les sections `.cc-section` à micro-titre `h4`
//    majuscule, la bascule « mode avancé » `.cc-adv-toggle`, le bandeau `.cc-error`, la note
//    `.cc-hint`, et surtout le COMPOSANT ROTATIF `.cc-knob` (cadran + champ chiffré) avec ses
//    interactions exactes : glissé vertical (Maj = pas fin), molette, flèches / PgUp-PgDn /
//    Début-Fin, double-clic = valeur par défaut. Seul AJOUT : la variante shape="angle" (cercle
//    complet, repère à 12 h) pour les ANGLES — même cadran, même champ, géométrie différente.
//    ★ Le correcteur N'A PAS d'onglets : il empile des sections. Le Split fait pareil désormais
//      (cf. « POURQUOI PLUS D'ONGLETS » dans renderProps — c'était un échec silencieux).
//  • BARRE D'OUTILS + SNAP MAGNÉTIQUE : l'éditeur de layout multiview / PiP (static/pip_editor.js)
//    — mêmes groupes, mêmes pictos SVG, mêmes cibles d'aimantation, mêmes guides, même bouton.
//
// ── Règles du projet appliquées ──────────────────────────────────────────────────────────
//  • Bornes / défauts / courbes / modes de clé LUS dans /state.caps — rien codé en dur.
//  • i18n FR/EN pour tout libellé (window.t, clés plugin.split.*).
//  • Jamais de contrôle muet : budget de trame moteur affiché en permanence, coût de la rotation
//    annoncé AVANT usage, contrôle désactivé qui dit pourquoi, refus moteur remonté à l'écran.
'use strict';
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins.split = (function () {
    let EL = null, VMID = null, TOAST = () => {};
    let st = null, caps = null, boxes = null, inputs = ['', '', '', '', ''];
    let sel = [], clipboard = null, advanced = false, errorMsg = null;
    // ★ CIBLE DU PANNEAU DE RÉGLAGES (0.12.1) : une box, plusieurs box, OU le canal Global. Le
    // Global est un ÉLÉMENT SÉLECTIONNABLE COMME UN AUTRE — il n'a donc PAS de panneau à lui :
    // il réutilise LE panneau (mêmes cadrans, mêmes libellés, mêmes pas, mêmes steppers). Les
    // réglages qui ne le concernent pas sont MASQUÉS (et on dit pourquoi, une fois, en tête).
    let selGlobal = false;
    let sources = [], containers = [], memCache = [], adv = null, metricsErr = false;
    let drag = null, knobDrag = null, boxPostTimer = null, persistTimer = null, pollTimer = null, metTimer = null;
    // Snap magnétique — transposé de l'éditeur de PiP : mêmes cibles (bords + centre du cadre,
    // bords + centre des autres box), seuil ∝ résolution du canvas, guides pointillés, même
    // bouton. ★ Le nudge clavier CONTOURNE le snap (c'est sa raison d'être : le pixel près).
    let snapEnabled = true, snapGuides = [];
    const _postPend = new Set();   // box en attente d'envoi (multi-déplacement : toutes suivent)

    const BOX_COLORS = ['#c0504d', '#4f81bd', '#9bbb59', '#8064a2'];   // repérage des box (donnée, pas décor)

    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const $ = q => (EL ? EL.querySelector(q) : null);
    const T = (k, fb) => { const v = window.t ? window.t('plugin.split.' + k) : ''; return (v && v !== 'plugin.split.' + k) ? v : fb; };
    const clamp = (v, lo, hi) => { v = parseFloat(v); if (isNaN(v)) return lo; return Math.max(lo, Math.min(hi, v)); };
    const r3 = v => Math.round(v * 1000) / 1000;
    const decimals = step => { const s = String(step); return s.includes('.') ? s.split('.')[1].length : 0; };
    const fmt = (v, step, unit) => Number(v).toFixed(decimals(step)) + (unit || '');
    const SNAP_PX = () => { const cv = $('#split_canvas'); return Math.max(4, Math.round((cv ? cv.width : 640) / 80)); };

    // ─── Domaine des valeurs : LU dans /state.caps ──────────────────────────────────
    function cap(group, key) {
        const c = caps || {};
        if (group === 'crop') return c.crop || [0, 0.9, 0];
        if (group === 'stagger') return c.stagger_ms || [0, 2000, 0];
        return (c[group] || {})[key] || [0, 1, 0];
    }
    function stepOf(min, max) {
        const range = Math.abs(max - min);
        if (range <= 2.5) return 0.01;
        if (range <= 20) return 0.1;
        return 1;
    }
    const easings = () => (caps && caps.easings) || ['linear', 'ease_in_out'];
    const artKeys = () => (caps && caps.art_keys) || ['alpha', 'luma', 'chroma', 'none'];

    // ─── Erreur (bandeau .cc-error du correcteur de couleur) ────────────────────────
    function showError(m) { errorMsg = m; renderError(); }
    function clearError() { if (errorMsg === null) return; errorMsg = null; renderError(); }
    function renderError() {
        const slot = $('#split-error-slot'); if (!slot) return;
        slot.innerHTML = errorMsg
            ? `<div class="cc-error" role="alert"><span>⚠ ${esc(errorMsg)}</span>
               <button type="button" id="split-error-close" aria-label="${esc(T('close', 'Fermer'))}">✕</button></div>` : '';
        const b = $('#split-error-close'); if (b) b.onclick = clearError;
    }

    // ─── Sources / instances ────────────────────────────────────────────────────────
    async function loadSources() {
        try {
            const data = await (await fetch('/api/home/summary')).json();
            const seen = new Set(), out = [];
            for (const n of (data.nodes || data.topo_nodes || []))
                for (const p of (n.produces || []))
                    if (p.kind === 'video' && p.shm && !seen.has(p.shm)) {
                        seen.add(p.shm); out.push({ shm: p.shm, label: `${n.hostname || ('#' + n.vmid)} · ${p.shm}` });
                    }
            sources = out;
        } catch (e) { sources = []; }
    }
    async function loadContainers() {
        try {
            containers = (await (await fetch('/api/plugins/instances?type=split')).json())
                .map(c => ({ vmid: c.vmid, hostname: c.hostname || ('VMID ' + c.vmid) }));
        } catch (e) { containers = []; }
    }
    function srcOptions(selected) {
        let h = `<option value="">${esc(T('src_none', '— non câblé —'))}</option>`, found = false;
        for (const s of sources) {
            const k = (s.shm === selected) ? ' selected' : ''; if (k) found = true;
            h += `<option value="${esc(s.shm)}"${k}>${esc(s.label)}</option>`;
        }
        if (selected && !found) h += `<option value="${esc(selected)}" selected>${esc(selected)} (?)</option>`;
        return h;
    }

    // ─── Modèle ─────────────────────────────────────────────────────────────────────
    function normBox(b) {
        b = b || {};
        const crop = b.crop || {}, bd = b.border || {}, sh = b.shadow || {};
        const cv = (g, k, v) => { const [lo, hi, d] = cap(g, k); return clamp(v == null ? d : v, lo, hi); };
        const cc = v => { const [lo, hi, d] = cap('crop'); return clamp(v == null ? d : v, lo, hi); };
        return {
            enabled: !!b.enabled,
            x: cv('box_fields', 'x', b.x), y: cv('box_fields', 'y', b.y),
            size: cv('box_fields', 'size', b.size),
            rotation: cv('box_fields', 'rotation', b.rotation),
            opacity: cv('box_fields', 'opacity', b.opacity),
            feather: cv('box_fields', 'feather', b.feather),
            radius: cv('box_fields', 'radius', b.radius),
            size_x: cv('box_fields', 'size_x', b.size_x), size_y: cv('box_fields', 'size_y', b.size_y),
            aspect: (b.aspect === 'free' ? 'free' : 'keep'),
            mirror_h: !!b.mirror_h, mirror_v: !!b.mirror_v,
            z: (b.z == null ? null : cv('box_fields', 'z', b.z)),
            crop: { t: cc(crop.t), b: cc(crop.b), l: cc(crop.l), r: cc(crop.r) },
            border: {
                w: Math.round(cv('border_fields', 'w', bd.w)), color: bd.color || '#ffffff',
                outer: cv('border_fields', 'outer', bd.outer), inner: cv('border_fields', 'inner', bd.inner),
                bevel: cv('border_fields', 'bevel', bd.bevel),
                bevel_soft: cv('border_fields', 'bevel_soft', bd.bevel_soft),
                bevel_pos: cv('border_fields', 'bevel_pos', bd.bevel_pos),
            },
            shadow: {
                enabled: !!sh.enabled,
                dx: cv('shadow_fields', 'dx', sh.dx), dy: cv('shadow_fields', 'dy', sh.dy),
                blur: cv('shadow_fields', 'blur', sh.blur), opacity: cv('shadow_fields', 'opacity', sh.opacity),
                color: sh.color || '#000000',
            },
        };
    }
    const wOf = b => (b.aspect === 'free' && b.size_x > 0) ? b.size_x : b.size;
    const hOf = b => (b.aspect === 'free' && b.size_y > 0) ? b.size_y : b.size;
    function rectN(b) {
        const fw = wOf(b), fh = hOf(b);
        const visW = fw * (1 - b.crop.l - b.crop.r), visH = fh * (1 - b.crop.t - b.crop.b);
        const cx = b.x + (b.crop.l - b.crop.r) * 0.5 * fw, cy = b.y + (b.crop.t - b.crop.b) * 0.5 * fh;
        return { x0: cx - visW / 2, y0: cy - visH / 2, w: visW, h: visH, cx, cy };
    }
    function setRectPos(b, x0, y0) {
        const r = rectN(b);
        if (x0 != null) b.x = r3(clamp(b.x + (x0 - r.x0), 0, 1));
        if (y0 != null) b.y = r3(clamp(b.y + (y0 - r.y0), 0, 1));
    }
    const primary = () => (sel.length ? sel[0] : -1);

    // ═══ ÉDITION MULTI-BOX ══════════════════════════════════════════════════════════
    // La multi-sélection valait pour le canvas et la barre d'outils, mais le panneau
    // n'écrivait QUE dans boxes[primary()] : on sélectionnait 3 box, on tournait un cadran,
    // une seule bougeait. Tout réglage passe désormais par applySetting(), qui écrit dans
    // TOUTES les box sélectionnées — un POST /box CIBLÉ par box, avec le SEUL champ touché
    // (fusion profonde côté moteur : on ne réécrit jamais l'état complet d'une box).
    //
    // ★ DELTA vs ABSOLU — le choix, et pourquoi :
    //   • GÉOMÉTRIE (x, y, size, size_x, size_y, z) → DELTA. Appliquer une valeur absolue
    //     identique à N box les empilerait au même endroit (et écraserait leurs tailles/
    //     ordres respectifs) : absurde. On applique donc l'ÉCART demandé sur la box de
    //     référence à chacune, qui garde sa disposition relative. Uniformiser une taille
    //     reste possible : c'est l'outil dédié « même taille que la référence » de la barre.
    //   • APPARENCE (crop, rotation, opacité, adoucissement, coins, bordure, ombre, miroir,
    //     ratio, couleurs) → ABSOLU : là, on VEUT uniformiser la sélection.
    const DELTA_PATHS = new Set(['x', 'y', 'size', 'size_x', 'size_y', 'z']);
    const getV = (b, path) => { const p = path.split('.'); return p.length === 1 ? b[p[0]] : (b[p[0]] || {})[p[1]]; };
    const setV = (b, path, v) => { const p = path.split('.'); if (p.length === 1) b[p[0]] = v; else b[p[0]][p[1]] = v; };
    const patchOf = (path, v) => { const p = path.split('.'); return p.length === 1 ? { [p[0]]: v } : { [p[0]]: { [p[1]]: v } }; };
    // `z` vaut null tant qu'il n'est pas forcé : sa valeur EFFECTIVE est l'index de la box.
    const effV = (i, path) => { const v = getV(boxes[i], path); return (path === 'z' && v == null) ? i : v; };
    // Valeurs divergentes dans la sélection → le champ ne doit pas MENTIR en affichant celle
    // de la box de référence (cf. renderProps : placeholder « — » + libellé « ≠ »).
    function isMixed(path) {
        if (sel.length < 2) return false;
        const ref = effV(sel[0], path);
        return sel.some(i => {
            const v = effV(i, path);
            if (typeof ref === 'number' && typeof v === 'number') return Math.abs(v - ref) > 1e-6;
            return v !== ref;
        });
    }
    // Écrit UN réglage dans toute la sélection. `newVal` = valeur saisie/tournée sur la box de
    // référence. Ne touche QUE ce réglage : les autres restent ceux de chaque box.
    function applySetting(path, newVal) {
        const refI = primary(); if (refI < 0 || !boxes) return;
        const isDelta = DELTA_PATHS.has(path) && typeof newVal === 'number';
        const d = isDelta ? (newVal - (effV(refI, path) || 0)) : 0;
        sel.forEach(i => {
            const b = boxes[i];
            const v = isDelta ? ((effV(i, path) || 0) + d) : newVal;
            setV(b, path, v);
            Object.assign(b, normBox(b));                 // re-borne via caps
            postBox(i, patchOf(path, getV(b, path)));     // POST ciblé, champ unique
        });
        drawCanvas();
    }

    // ─── Fetch état / métriques ─────────────────────────────────────────────────────
    function userIsEditing() {
        if (drag || knobDrag) return true;
        const ae = document.activeElement;
        if (!ae || !EL || !EL.contains(ae)) return false;
        if (['INPUT', 'SELECT', 'TEXTAREA'].includes(ae.tagName)) return true;
        return !!(ae.classList && ae.classList.contains('cc-knob-dial'));
    }
    async function fetchState({ rebuild = false } = {}) {
        if (VMID == null || !EL) return;
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/state`);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const s = await r.json();
            st = s; caps = s.caps || caps;
            if (rebuild || !$('#split_canvas')) {
                boxes = (s.boxes || []).map(normBox);
                while (boxes.length < (s.n_boxes || 4)) boxes.push(normBox({}));
                inputs = (s.inputs || []).slice(0, 5); while (inputs.length < 5) inputs.push('');
                sel = sel.filter(i => i < boxes.length); if (!sel.length) sel = [0];
                renderShell(); loadMemories();
            } else if (!userIsEditing()) {
                boxes = (s.boxes || []).map(normBox);
                inputs = (s.inputs || []).slice(0, 5);
                syncAll();
            }
        } catch (e) {
            if (EL && !$('#split_canvas'))
                EL.innerHTML = `<div class="cc-error" role="alert"><span>⚠ ${esc(T('err_state', 'État du Split injoignable'))} : ${esc(e.message)}</span></div>`;
            else showError(T('err_state', 'État du Split injoignable') + ' : ' + e.message);
        }
    }
    async function fetchMetrics() {
        if (VMID == null || !EL) return;
        try {
            const r = await fetch(`/api/containers/${VMID}/split/metrics`);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            adv = (await r.json()).adv || null; metricsErr = false;
        } catch (e) { adv = null; metricsErr = true; }
        updateBudget();
    }

    // ═══ ROTATIF — COMPOSANT DU CORRECTEUR DE COULEUR, repris tel quel ══════════════
    // Mêmes classes (.cc-knob / .cc-knob-dial / .cc-knob-value), même cadran SVG, mêmes
    // interactions. AJOUT : shape="angle" → cercle complet, repère à 12 h, pour la ROTATION
    // d'une box et la DIRECTION de la lumière (un angle n'est pas une jauge).
    //
    // ★ BOUTONS −/+ (0.11.1, taille corrigée en 0.11.2) : le champ chiffré n'est plus exclu du
    // stepper GLOBAL (static/scripts.js : MutationObserver → enrobage `.num-stepper`). ÉTALON =
    // le « Max entrées » du multiview : champ pleine largeur, boutons de 30 px sur toute la
    // hauteur du champ — c'est CE confort (tactile compris) qui fait l'intérêt du composant.
    // La 0.9.2 l'excluait (`no-stepper`, « ça ne rentre pas ») et la 0.11.1 rétrécissait les
    // boutons pour les faire rentrer : deux façons d'adapter le COMPOSANT à la mise en page.
    // C'est l'inverse — la colonne du cadran est élargie (control.css), le stepper garde sa
    // taille de référence, et le bloc « valeur + −/+ » prend toute la largeur sous le cadran.
    // Le PAS est le même pour tout le monde (`step` du champ = `data-step` du cadran, dérivé de
    // /state.caps) : bouton −/+, flèches clavier et molette avancent EXACTEMENT du même pas —
    // une seule vérité. Le stepper émet `change` → le commit passe par le handler du rotatif.
    // ÉTAGE 1 de la migration : seul le TRACÉ passe au catalogue. Aucune classe ni règle de mise
    // en page n'est touchée — c'est en les retirant d'un bloc que la page avait été cassée : le
    // conteneur .cc-knob ne portait pas qu'un rotatif, il portait aussi les contraintes de largeur
    // qui tiennent la colonne.
    // Toutes les formes rendent un ARC, y compris la direction : un réglage est une valeur chiffrée
    // bornée et se lit de la même façon partout (règle de parc, cf. controls.css).
    function knobSvg(p01, shape, val, def01) {
        return window.MXLControls.knobSvg('arc', p01, def01);
    }
    // p = {target, label, min, max, step, def, unit, shape}. `target` = destination du POST :
    // 'box:<champ>' · 'box:<sous-dict>.<champ>' · 'light:<champ>' · 'art:<champ>'.
    function knobHtml(p, value) {
        const v = (value == null) ? p.def : value, shape = p.shape || 'arc';
        // Valeurs divergentes dans la sélection : le cadran reste posé sur la valeur de la box
        // de RÉFÉRENCE (il faut bien une aiguille), mais il est grisé, son libellé porte « ≠ »
        // et le champ chiffré est VIDE avec le placeholder « — » : il n'affirme pas une valeur
        // commune qui n'existe pas. Agir dessus UNIFORMISE la sélection (c'est un absolu).
        const mixed = !!p.mixed;
        const multiTitle = mixed
            ? T('mixed_knob', 'Valeurs multiples dans la sélection. Agir sur ce réglage l\'applique à TOUTES les box sélectionnées (uniformisation).')
            : (sel.length > 1 ? T('multi_knob', 'S\'applique à TOUTES les box sélectionnées.') : '');
        return `<div class="cc-knob${mixed ? ' is-mixed' : ''}" data-target="${p.target}" data-min="${p.min}" data-max="${p.max}"
      data-step="${p.step}" data-default="${p.def}" data-unit="${p.unit || ''}" data-shape="${shape}"
      data-ref="${v}" data-mixed="${mixed ? '1' : '0'}">
  <div class="cc-knob-label">${esc(p.label)}${mixed ? ' <span class="mixed-mark" aria-hidden="true">≠</span>' : ''}</div>
  <div class="cc-knob-dial" role="slider" tabindex="0" aria-label="${esc(p.label)}"
       aria-valuemin="${p.min}" aria-valuemax="${p.max}" aria-valuenow="${v}"
       aria-valuetext="${mixed ? esc(T('mixed_short', 'valeurs multiples')) : esc(fmt(v, p.step, p.unit))}"
       title="${esc((T('knob_title', 'Glisser vertical, molette, flèches (Maj = pas fin). Double-clic = valeur par défaut.') + (multiTitle ? ' ' + multiTitle : '')))}">${knobSvg((v - p.min) / ((p.max - p.min) || 1), shape, v, (p.def - p.min) / ((p.max - p.min) || 1))}</div>
  <input type="number" class="cc-knob-value" min="${p.min}" max="${p.max}" step="${p.step}"
         value="${mixed ? '' : v}" placeholder="${mixed ? '—' : ''}"
         title="${esc(multiTitle)}" aria-label="${esc(p.label)}"></div>`;
    }
    function knobApply(knob, v) {
        const min = +knob.dataset.min, max = +knob.dataset.max, step = +knob.dataset.step;
        const unit = knob.dataset.unit || '', shape = knob.dataset.shape;
        v = parseFloat(Number(v).toFixed(decimals(step)));
        const dial = knob.querySelector('.cc-knob-dial'), num = knob.querySelector('.cc-knob-value');
        dial.innerHTML = knobSvg((v - min) / ((max - min) || 1), shape, v,
            (parseFloat(knob.dataset.default) - min) / ((max - min) || 1));
        dial.setAttribute('aria-valuenow', v);
        dial.setAttribute('aria-valuetext', fmt(v, step, unit));
        if (document.activeElement !== num) num.value = v;
    }
    const knobClamp = (knob, v) => Math.max(+knob.dataset.min, Math.min(+knob.dataset.max, v));
    // Envoie la valeur d'un rotatif vers le bon endpoint (fusion profonde côté moteur).
    function knobPost(knob, v) {
        const [kind, path] = String(knob.dataset.target).split(':');
        if (kind === 'glob') { postGlobal({ [path]: v }); return; }   // canal GLOBAL
        if (kind === 'bggrad') { postBg({ gradient: { [path]: v } }); return; }   // dégradé de fond
        if (kind === 'light') { postLight({ [path]: v }); return; }
        if (kind === 'art') { postArt({ [path]: v }); return; }
        applySetting(path, v);              // ★ toute la sélection, pas seulement la référence
        const k = knob.closest('.cc-knob');
        if (k) { k.classList.remove('is-mixed'); k.dataset.mixed = '0'; }   // la valeur vient d'être uniformisée
    }
    function bindKnobs(root) {
        (root || EL).querySelectorAll('.cc-knob').forEach(knob => {
            if (knob._bound) return; knob._bound = true;
            const dial = knob.querySelector('.cc-knob-dial'), num = knob.querySelector('.cc-knob-value');
            const move = e => {
                if (!knobDrag) return;
                const dy = knobDrag.startY - e.clientY, range = knobDrag.max - knobDrag.min;
                const vpp = e.shiftKey ? knobDrag.step : range / 200;   // Maj = pas fin (comme le correcteur)
                const v = knobClamp(knob, knobDrag.startValue + dy * vpp);
                knobApply(knob, Math.round(v / knobDrag.step) * knobDrag.step);
            };
            const up = () => {
                if (!knobDrag) return;
                dial.removeEventListener('pointermove', move);
                dial.removeEventListener('pointerup', up);
                dial.removeEventListener('pointercancel', up);
                const v = parseFloat(num.value); knobDrag = null;
                if (!isNaN(v)) { knobPost(knob, v); }
            };
            dial.addEventListener('pointerdown', e => {
                if (e.button !== undefined && e.button !== 0) return;
                e.preventDefault();
                // Champ vide = valeurs multiples → on part de la valeur de la box de référence.
                const start = (num.value === '') ? (+knob.dataset.ref || 0) : (parseFloat(num.value) || 0);
                knobDrag = { startY: e.clientY, startValue: start,
                    min: +knob.dataset.min, max: +knob.dataset.max, step: +knob.dataset.step };
                try { dial.setPointerCapture(e.pointerId); } catch (_) {}
                dial.addEventListener('pointermove', move);
                dial.addEventListener('pointerup', up);
                dial.addEventListener('pointercancel', up);
                dial.focus();
            });
            dial.addEventListener('dblclick', () => {
                const d = +knob.dataset.default;
                knobApply(knob, d); knobPost(knob, d);
            });
            dial.addEventListener('wheel', e => {
                e.preventDefault();
                const step = +knob.dataset.step;
                const cur = (num.value === '') ? (+knob.dataset.ref || 0) : parseFloat(num.value);
                const v = knobClamp(knob, cur + (e.deltaY < 0 ? 1 : -1) * step * (e.shiftKey ? 10 : 1));
                knobApply(knob, v); knobPost(knob, parseFloat(num.value));
            }, { passive: false });
            dial.addEventListener('keydown', e => {
                const step = +knob.dataset.step, min = +knob.dataset.min, max = +knob.dataset.max;
                const mult = e.shiftKey ? 10 : 1;
                let v = parseFloat(num.value);
                if (isNaN(v)) v = +knob.dataset.ref || 0;      // mixte : partir de la référence
                let handled = true;
                switch (e.key) {
                    case 'ArrowUp': case 'ArrowRight': v += step * mult; break;
                    case 'ArrowDown': case 'ArrowLeft': v -= step * mult; break;
                    case 'PageUp': v += step * 10; break;
                    case 'PageDown': v -= step * 10; break;
                    case 'Home': v = min; break;
                    case 'End': v = max; break;
                    case 'Enter': case ' ': v = +knob.dataset.default; break;
                    default: handled = false;
                }
                if (!handled) return;
                e.preventDefault();
                e.stopPropagation();       // ne PAS déplacer la box (nudge clavier du canvas)
                knobApply(knob, knobClamp(knob, v)); knobPost(knob, parseFloat(num.value));
            });
            num.addEventListener('input', () => {          // saisie chiffrée : le cadran suit
                const v = parseFloat(num.value); if (isNaN(v)) return;
                const min = +knob.dataset.min, max = +knob.dataset.max;
                const c = Math.max(min, Math.min(max, v));
                dial.innerHTML = knobSvg((c - min) / ((max - min) || 1), knob.dataset.shape, c,
                    (parseFloat(knob.dataset.default) - min) / ((max - min) || 1));
                dial.setAttribute('aria-valuenow', c);
            });
            num.addEventListener('change', () => {         // commit (y compris depuis −/+)
                const v = parseFloat(num.value); if (isNaN(v)) return;
                const c = knobClamp(knob, v);
                knobApply(knob, c); knobPost(knob, c);
            });
            // VALEURS DIVERGENTES : le champ est VIDE (il n'affirme pas une valeur commune qui
            // n'existe pas). Un −/+ partirait donc de 0 — et écraserait la sélection avec ±1 pas
            // depuis zéro. On amorce le champ sur la valeur de la box de RÉFÉRENCE juste avant
            // que le stepper ne calcule, exactement comme le fait le cadran quand on le saisit.
            // Écoute en CAPTURE sur le rotatif : le stepper global est enrobé après coup (rAF),
            // ses boutons n'existent pas encore au moment du bind.
            knob.addEventListener('pointerdown', e => {
                const btn = e.target && e.target.closest && e.target.closest('.num-btn');
                if (btn && num.value === '') num.value = (+knob.dataset.ref || 0);
            }, true);
        });
    }

    // ─── Pictos de la barre d'outils : IDENTIQUES à l'éditeur de PiP / composer multiview ──
    const SVG = {
        align_left: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="1.6" height="14" rx="0.8"/><rect x="4" y="3" width="9.5" height="3.6" rx="1"/><rect x="4" y="9.4" width="6" height="3.6" rx="1"/></svg>',
        align_hcenter: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="3" width="12" height="3.6" rx="1"/><rect x="4" y="9.4" width="8" height="3.6" rx="1"/><rect x="7.2" y="1" width="1.6" height="14" rx="0.8"/></svg>',
        align_right: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="13.4" y="1" width="1.6" height="14" rx="0.8"/><rect x="2.5" y="3" width="9.5" height="3.6" rx="1"/><rect x="6" y="9.4" width="6" height="3.6" rx="1"/></svg>',
        align_top: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="14" height="1.6" rx="0.8"/><rect x="3" y="4" width="3.6" height="9.5" rx="1"/><rect x="9.4" y="4" width="3.6" height="6" rx="1"/></svg>',
        align_vcenter: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="2" width="3.6" height="12" rx="1"/><rect x="9.4" y="4" width="3.6" height="8" rx="1"/><rect x="1" y="7.2" width="14" height="1.6" rx="0.8"/></svg>',
        align_bottom: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="13.4" width="14" height="1.6" rx="0.8"/><rect x="3" y="2.5" width="3.6" height="9.5" rx="1"/><rect x="9.4" y="6" width="3.6" height="6" rx="1"/></svg>',
        size_w: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="2" width="10" height="4.5" rx="1"/><rect x="3" y="9.5" width="10" height="4.5" rx="1" fill-opacity="0.55"/></svg>',
        size_h: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="3" width="4.5" height="10" rx="1"/><rect x="9.5" y="3" width="4.5" height="10" rx="1" fill-opacity="0.55"/></svg>',
        size_both: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1.5" y="1.5" width="8" height="8" rx="1" fill-opacity="0.55"/><rect x="6.5" y="6.5" width="8" height="8" rx="1"/></svg>',
        distribute_h: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="1.6" height="14" rx="0.8"/><rect x="13.4" y="1" width="1.6" height="14" rx="0.8"/><rect x="6" y="4" width="4" height="8" rx="1"/></svg>',
        distribute_v: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="14" height="1.6" rx="0.8"/><rect x="1" y="13.4" width="14" height="1.6" rx="0.8"/><rect x="4" y="6" width="8" height="4" rx="1"/></svg>',
        copy_settings: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="5.5" y="1.5" width="9" height="9" rx="1.5" fill-opacity="0.45"/><rect x="1.5" y="5.5" width="9" height="9" rx="1.5"/></svg>',
        paste_settings: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="12" rx="1.5" fill-opacity="0.45"/><rect x="5.5" y="1" width="5" height="3" rx="1"/><rect x="5" y="6.5" width="6" height="6" rx="1"/></svg>',
    };
    function toolGroup(label, defs) {
        const grp = document.createElement('div');
        grp.className = 'tool-group';
        grp.innerHTML = `<span class="tool-group-label" aria-hidden="true">${esc(label)}</span>`;
        const row = document.createElement('div');
        row.className = 'tool-row'; row.setAttribute('role', 'group'); row.setAttribute('aria-label', label);
        defs.forEach(([svg, title, fn, id]) => {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'tool-btn'; b.innerHTML = svg;
            b.title = title; b.setAttribute('aria-label', title);
            if (id) b.id = id;
            b.onclick = fn;
            row.appendChild(b);
        });
        grp.appendChild(row);
        return grp;
    }

    // ─── Outils de disposition (même sémantique que le composer multiview / PiP) ─────
    function alignBoxes(mode) {
        if (!boxes || !sel.length) return;
        const p = boxes[primary()];
        const ref = (sel.length >= 2 && p) ? rectN(p) : { x0: 0, y0: 0, w: 1, h: 1 };
        sel.forEach(i => {
            if (sel.length >= 2 && i === primary()) return;
            const b = boxes[i]; if (!b) return;
            const r = rectN(b);
            switch (mode) {
                case 'left': setRectPos(b, ref.x0, null); break;
                case 'right': setRectPos(b, ref.x0 + ref.w - r.w, null); break;
                case 'hcenter': setRectPos(b, ref.x0 + (ref.w - r.w) / 2, null); break;
                case 'top': setRectPos(b, null, ref.y0); break;
                case 'bottom': setRectPos(b, null, ref.y0 + ref.h - r.h); break;
                case 'vcenter': setRectPos(b, null, ref.y0 + (ref.h - r.h) / 2); break;
            }
            postBox(i, { x: b.x, y: b.y });
        });
        syncAll();
    }
    function matchSize(mode) {
        if (!boxes || sel.length < 2) { TOAST(T('flash_select2', 'Sélectionnez au moins 2 box (Maj+clic).'), 'warning'); return; }
        const ref = boxes[primary()]; if (!ref) return;
        sel.forEach(i => {
            if (i === primary()) return;
            const b = boxes[i]; if (!b) return;
            const patch = { size: ref.size };
            b.size = ref.size;
            if (ref.aspect === 'free') {
                b.aspect = 'free'; patch.aspect = 'free';
                if (mode === 'w' || mode === 'both') { b.size_x = ref.size_x; patch.size_x = ref.size_x; }
                if (mode === 'h' || mode === 'both') { b.size_y = ref.size_y; patch.size_y = ref.size_y; }
            }
            postBox(i, patch);
        });
        syncAll();
    }
    function distributeBoxes(axis) {
        if (!boxes || sel.length < 3) { TOAST(T('flash_select3', 'Sélectionnez au moins 3 box (Maj+clic).'), 'warning'); return; }
        const items = sel.map(i => ({ i, b: boxes[i] })).filter(o => o.b);
        const k = (axis === 'h') ? 'cx' : 'cy';
        items.sort((a, b_) => rectN(a.b)[k] - rectN(b_.b)[k]);
        const v0 = rectN(items[0].b)[k], vN = rectN(items[items.length - 1].b)[k];
        const step = (vN - v0) / (items.length - 1);
        items.forEach((o, n) => {
            if (n === 0 || n === items.length - 1) return;
            const target = v0 + step * n, r = rectN(o.b);
            if (axis === 'h') o.b.x = r3(clamp(o.b.x + (target - r.cx), 0, 1));
            else o.b.y = r3(clamp(o.b.y + (target - r.cy), 0, 1));
            postBox(o.i, { x: o.b.x, y: o.b.y });
        });
        syncAll();
    }
    const CLIP_KEYS = ['size', 'size_x', 'size_y', 'aspect', 'rotation', 'opacity', 'feather', 'radius', 'mirror_h', 'mirror_v'];
    function copySettings() {
        const i = primary(); if (i < 0 || !boxes) return;
        const b = boxes[i], c = {};
        CLIP_KEYS.forEach(k => { c[k] = b[k]; });
        c.crop = Object.assign({}, b.crop);
        c.border = Object.assign({}, b.border);
        c.shadow = Object.assign({}, b.shadow);
        clipboard = c;
        TOAST(T('copied', 'Réglages de la box copiés (hors position).'), 'info');
        updateToolbar();
    }
    function pasteSettings() {
        if (!clipboard || !boxes || !sel.length) return;
        sel.forEach(i => {
            const patch = JSON.parse(JSON.stringify(clipboard));
            Object.assign(boxes[i], normBox(Object.assign({}, boxes[i], patch)));
            postBox(i, patch);
        });
        syncAll();
        TOAST(T('pasted', 'Réglages collés.'), 'info');
    }

    // ─── Déplacement clavier PIXEL PAR PIXEL — CONTOURNE le snap (comme l'éditeur de PiP) ──
    const NUDGE = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    function onKeyDown(e) {
        if (!EL || !boxes || !sel.length) return;
        const dir = NUDGE[e.key]; if (!dir) return;
        const ae = document.activeElement;
        if (ae && /INPUT|SELECT|TEXTAREA/.test(ae.tagName)) return;
        if (ae && ae.classList && ae.classList.contains('cc-knob-dial')) return;   // rotatif focalisé
        if (!EL.contains(ae) && ae !== document.body) return;
        e.preventDefault();
        const W = (st && st.width) || 1920, H = (st && st.height) || 1080;
        const mult = e.shiftKey ? 10 : 1;
        let dx = (dir[0] * mult) / W, dy = (dir[1] * mult) / H;
        sel.forEach(i => {
            const b = boxes[i];
            dx = Math.max(-b.x, Math.min(1 - b.x, dx));
            dy = Math.max(-b.y, Math.min(1 - b.y, dy));
        });
        if (!dx && !dy) return;
        sel.forEach(i => {
            const b = boxes[i];
            b.x = r3(b.x + dx); b.y = r3(b.y + dy);   // ★ aucun snap : le nudge sert à en sortir
            postBox(i, { x: b.x, y: b.y });
        });
        snapGuides = [];
        syncAll();
    }

    // ─── Shell ──────────────────────────────────────────────────────────────────────
    function renderShell() {
        EL.innerHTML = `
<div class="split-ui">
  <div id="split-error-slot"></div>
  <div class="split-toolbar" id="split_toolbar"></div>

  <!-- Étage 1 : APERÇU (compact) + cible d'édition (les 4 box et le fond) à sa droite.
       Même patron que l'éditeur de PiP : une grille minmax(0,1fr) + colonne fixe, qui se replie
       en une colonne sous 1100 px. L'aperçu est volontairement PLAFONNÉ en largeur : il sert à
       viser, pas à occuper l'écran — les réglages, eux, passent en dessous sur TOUTE la largeur. -->
  <div class="split-cols">
    <div class="split-stage">
      <div class="split-canvas-wrap"><canvas id="split_canvas" tabindex="0"
           aria-label="${esc(T('canvas_aria', 'Composition des box'))}"></canvas></div>
      <p class="cc-hint">${T('hint', 'Cliquer une box pour la sélectionner · <kbd>Maj</kbd>+clic = multi-sélection (glisser déplace TOUTE la sélection) · coin bas-droit = taille · arêtes = crop · <kbd>←↑→↓</kbd> = 1 px (contourne le magnétisme), <kbd>Maj</kbd> = 10 px. Le BORD DU CADRE aimante : la box s\'y colle par l\'intérieur (entièrement visible) ou juste au-delà (entièrement sortie — départ/arrivée d\'une entrée de champ).')}</p>
    </div>
    <aside class="split-targets">
      <h4>${esc(T('targets', 'Box & fond'))}</h4>
      <!-- ★ CANAL GLOBAL : un CALQUE, pas une multi-sélection. Il se COMPOSE par-dessus les
           réglages propres de chaque box (rendu = Global ∘ box) → tout le groupe sort du cadre
           d'un bloc SANS écraser les valeurs individuelles. C'est ce qui rend les animations de
           groupe (entrées/sorties de champ) possibles. -->
      <button type="button" class="split-chip split-chip-global" id="split_global_chip"
              aria-pressed="false" title="${esc(T('global_tip', 'Canal GLOBAL : un calque de transformation appliqué PAR-DESSUS les réglages de chaque box (décalage, échelle, rotation, opacité du groupe). Il ne remplace pas les valeurs des box — il s\'y compose. C\'est lui qui permet les entrées/sorties de champ de tout le groupe.'))}">
        <span class="split-chip-dot split-dot-global"></span>
        <span>${esc(T('global', 'Global (groupe)'))}</span>
        <span class="split-chip-state" id="split_global_state"></span>
      </button>
      <div class="split-strip" id="split_strip"></div>
      <!-- FOND : trois modes explicites (noir / source vidéo / couleur unie) -->
      <div class="field split-bg-field">
        <label for="split_bg_mode">${esc(T('bg_mode', 'Fond (plein cadre)'))}</label>
        <select id="split_bg_mode">${bgModeOptions()}</select>
      </div>
      <div class="field split-bg-field" id="split_bg_src_f">
        <label for="split_bg">${esc(T('bg_src', 'Source du fond'))}</label>
        <select id="split_bg">${srcOptions(inputs[4])}</select>
      </div>
      <div class="field split-bg-field" id="split_bg_col_f">
        <label for="split_bg_color">${esc(T('bg_color', 'Couleur du fond'))}</label>
        <input type="color" id="split_bg_color" value="${esc(((st && st.bg) || {}).color || '#000000')}">
        <label class="toggle-inline split-bg-legal"><input type="checkbox" class="ios-toggle" id="split_bg_legal">
          <span>${esc(T('bg_legal', 'Plage légale (broadcast)'))}</span></label>
      </div>
      <div class="split-bg-field" id="split_bg_grad_f"></div>
      <div id="split_bg_warn"></div>
    </aside>
  </div>

  <!-- Étage 2 : TOUS les réglages, PLEINE LARGEUR, en grille responsive (patron de la
       bibliothèque de modèles de PiP sortie de son aside). Les sections empilées du correcteur
       de couleur restent la structure — elles se répartissent simplement en colonnes. -->
  <div class="split-wide" id="split_props"></div>
  <div class="split-wide">
    <div id="split_art"></div>
    <div id="split_look"></div>
    <div class="cc-section split-full" id="split_mem">
      <h4>${esc(T('memories', 'Mémoires'))}</h4>
      <ul class="split-mem-list" id="split_mem_list"></ul>
      <div class="row split-mem-bar">
        <div class="field"><label for="split_mem_name">${esc(T('mem_name', 'Nom'))}</label>
          <input type="text" id="split_mem_name" placeholder="${esc(T('mem_name_ph', 'Nom de la mémoire'))}"></div>
        <div class="field"><label for="split_recall_dur">${esc(T('mem_duration', 'Transition (ms)'))}</label>
          <input type="number" id="split_recall_dur" min="0" max="10000" step="100" value="1000"></div>
        <button type="button" class="btn btn-green" id="split_mem_save">${esc(T('mem_save', 'Enregistrer la disposition'))}</button>
        <button type="button" class="btn" id="split_mem_import">${esc(T('mem_import', 'Importer un fichier'))}</button>
        <input type="file" id="split_mem_file" accept="application/json,.json" hidden>
      </div>
      <p class="cc-hint">${esc(T('mem_note', 'Le rappel utilise la courbe et le décalage réglés dans « Lumière & transitions ».'))}</p>
    </div>
  </div>
</div>`;

        const tb = $('#split_toolbar');
        tb.appendChild(toolGroup(T('group_align', 'Aligner'), [
            [SVG.align_left, T('align_left', 'Aligner à gauche'), () => alignBoxes('left')],
            [SVG.align_hcenter, T('align_hcenter', 'Centrer horizontalement'), () => alignBoxes('hcenter')],
            [SVG.align_right, T('align_right', 'Aligner à droite'), () => alignBoxes('right')],
            [SVG.align_top, T('align_top', 'Aligner en haut'), () => alignBoxes('top')],
            [SVG.align_vcenter, T('align_vcenter', 'Centrer verticalement'), () => alignBoxes('vcenter')],
            [SVG.align_bottom, T('align_bottom', 'Aligner en bas'), () => alignBoxes('bottom')],
        ]));
        tb.appendChild(toolGroup(T('group_size', 'Taille'), [
            [SVG.size_w, T('size_w', 'Même largeur que la référence'), () => matchSize('w'), 'split_size_w'],
            [SVG.size_h, T('size_h', 'Même hauteur que la référence'), () => matchSize('h'), 'split_size_h'],
            [SVG.size_both, T('size_both', 'Même taille que la référence'), () => matchSize('both'), 'split_size_both'],
        ]));
        tb.appendChild(toolGroup(T('group_distribute', 'Distribuer'), [
            [SVG.distribute_h, T('distribute_h', 'Distribuer horizontalement'), () => distributeBoxes('h'), 'split_dist_h'],
            [SVG.distribute_v, T('distribute_v', 'Distribuer verticalement'), () => distributeBoxes('v'), 'split_dist_v'],
        ]));
        tb.appendChild(toolGroup(T('group_settings', 'Réglages'), [
            [SVG.copy_settings, T('copy_settings', 'Copier les réglages de la box (hors position)'), copySettings, 'split_copy'],
            [SVG.paste_settings, T('paste_settings', 'Coller les réglages dans les box sélectionnées'), pasteSettings, 'split_paste'],
        ]));
        // Magnétisme : même contrôle que l'éditeur de PiP (label + switch .ios-toggle).
        const snapLbl = document.createElement('label');
        snapLbl.className = 'toggle-inline split-snap';
        snapLbl.title = T('snap_title', 'Aimante la box aux bords et au centre du cadre, et aux autres box. Les flèches du clavier contournent le magnétisme (réglage au pixel).');
        snapLbl.innerHTML = `<input type="checkbox" class="ios-toggle" id="split_snap" ${snapEnabled ? 'checked' : ''}>` +
            `<span>${esc(T('snap', 'Snap magnétique'))}</span>`;
        tb.appendChild(snapLbl);
        snapLbl.querySelector('#split_snap').onchange = function () { snapEnabled = this.checked; snapGuides = []; drawCanvas(); };

        const budget = document.createElement('div');
        budget.className = 'split-budget'; budget.id = 'split_budget';
        tb.appendChild(budget);

        const cv = $('#split_canvas');
        cv.addEventListener('pointerdown', canvasDown);
        cv.addEventListener('pointermove', canvasMove);
        cv.addEventListener('pointerup', canvasUp);
        cv.addEventListener('pointercancel', canvasUp);
        $('#split_bg').onchange = function () { postInput(4, this.value); };
        $('#split_mem_save').onclick = saveMemory;
        $('#split_mem_import').onclick = () => $('#split_mem_file').click();
        $('#split_mem_file').onchange = function () { onImportMemFile(this); };

        bindBg(); renderArt(); renderLook(); syncAll(); updateBudget(); renderError();
    }

    // ─── Bande des box ──────────────────────────────────────────────────────────────
    function renderStrip() {
        const strip = $('#split_strip'); if (!strip) return;
        strip.innerHTML = boxes.map((b, i) => `
<div class="split-chip${sel.includes(i) ? ' sel' : ''}" data-box="${i}">
  <div class="split-chip-head">
    <span class="split-chip-dot" style="background:${BOX_COLORS[i % 4]}"></span>
    <span class="split-chip-name">${esc(T('box', 'Box'))} ${i + 1}</span>
    <input type="checkbox" class="ios-toggle" data-en="${i}" ${b.enabled ? 'checked' : ''}
           title="${esc(T('box_enable_one', 'Afficher cette box (réglage propre à cette box, jamais appliqué à la sélection).'))}"
           aria-label="${esc(T('box_enable', 'Afficher cette box'))}">
  </div>
  <div class="field"><select data-src="${i}" aria-label="${esc(T('box', 'Box'))} ${i + 1}"
       title="${esc(T('box_src_one', 'Source de cette box (réglage propre à cette box, jamais appliqué à la sélection).'))}">${srcOptions(inputs[i])}</select></div>
</div>`).join('');
        strip.querySelectorAll('.split-chip').forEach(chip => {
            chip.addEventListener('click', e => {
                if (e.target.closest('select') || e.target.closest('input')) return;
                selectBox(parseInt(chip.dataset.box), e.shiftKey);
            });
        });
        strip.querySelectorAll('input[data-en]').forEach(cb => {
            cb.onchange = function () {
                const i = parseInt(this.dataset.en);
                boxes[i].enabled = this.checked; postBox(i, { enabled: this.checked }); drawCanvas();
            };
        });
        strip.querySelectorAll('select[data-src]').forEach(s => {
            s.onchange = function () { postInput(parseInt(this.dataset.src), this.value); };
        });
    }
    function selectBox(i, additive) {
        selGlobal = false;                      // sélectionner une box quitte le canal Global
        if (additive) {
            sel = sel.includes(i) ? sel.filter(x => x !== i) : sel.concat([i]);
            if (!sel.length) sel = [i];
        } else sel = [i];
        syncAll();
    }

    // ─── Champs simples ─────────────────────────────────────────────────────────────
    function numField(id, label, group, key, path, opts) {
        const [min, max, dflt] = cap(group, key);
        const step = (opts && opts.step) || stepOf(min, max);
        const b = boxes[primary()];
        const full = path ? `${path}.${key}` : key;
        let val = b ? (path ? (b[path] || {})[key] : b[key]) : dflt;
        if (val == null) val = (opts && opts.nullValue != null) ? opts.nullValue : (dflt == null ? 0 : dflt);
        const mixed = isMixed(full);
        const delta = DELTA_PATHS.has(full) && sel.length > 1;
        // Géométrie en multi = DELTA (une valeur absolue empilerait les box) ; apparence = ABSOLU.
        const tip = sel.length < 2 ? ''
            : (delta ? T('multi_delta', 'Multi-sélection : la valeur saisie est appliquée en DÉCALAGE (l\'écart demandé sur la box de référence est reporté sur chaque box, qui garde sa disposition).')
                     : T('multi_abs', 'S\'applique à TOUTES les box sélectionnées (uniformisation).'));
        return `<div class="field${mixed ? ' is-mixed' : ''}">
  <label for="${id}" title="${esc(tip)}">${esc(label)}${mixed ? ' <span class="mixed-mark" aria-hidden="true">≠</span>' : ''}</label>
  <input type="number" id="${id}" data-key="${key}" data-path="${path || ''}" data-ref="${Number(val)}"
         min="${min}" max="${max}" step="${step}" title="${esc(tip)}"
         value="${mixed ? '' : Number(val).toFixed(decimals(step))}" placeholder="${mixed ? '—' : ''}">
</div>`;
    }
    function colorField(id, label, path, key) {
        const b = boxes[primary()];
        const full = path ? `${path}.${key}` : key;
        const val = (b && (path ? (b[path] || {})[key] : b[key])) || '#ffffff';
        const mixed = isMixed(full);
        const tip = mixed ? T('mixed_color', 'Couleurs différentes dans la sélection : choisir une couleur les uniformise.')
            : (sel.length > 1 ? T('multi_abs', 'S\'applique à TOUTES les box sélectionnées (uniformisation).') : '');
        return `<div class="field${mixed ? ' is-mixed' : ''}"><label for="${id}" title="${esc(tip)}">${esc(label)}${mixed ? ' <span class="mixed-mark" aria-hidden="true">≠</span>' : ''}</label>
  <input type="color" id="${id}" data-key="${key}" data-path="${path || ''}" value="${esc(val)}" title="${esc(tip)}"></div>`;
    }
    function toggleField(key, path, label) {
        const b = boxes[primary()];
        const full = path ? `${path}.${key}` : key;
        const on = !!(b && (path ? (b[path] || {})[key] : b[key]));
        const mixed = isMixed(full);
        const tip = mixed ? T('mixed_toggle', 'États différents dans la sélection : basculer applique le même état à TOUTES les box sélectionnées.')
            : (sel.length > 1 ? T('multi_abs', 'S\'applique à TOUTES les box sélectionnées (uniformisation).') : '');
        return `<label class="toggle-inline${mixed ? ' is-mixed' : ''}" title="${esc(tip)}">
  <input type="checkbox" class="ios-toggle" data-flag="${key}" data-path="${path || ''}" ${on ? 'checked' : ''}
         ${mixed ? 'data-indeterminate="1"' : ''}>
  <span>${esc(label)}${mixed ? ' <span class="mixed-mark" aria-hidden="true">≠</span>' : ''}</span></label>`;
    }

    // Coût de la rotation : ANNONCÉ AVANT usage, selon le format courant (mesures du moteur).
    // ★ RÉ-OUVERTE en 0.10.0 : le compositing tourne sur le noyau C (mvk) — rotation FIXE mesurée
    // à 8,4 ms/trame en 1080p (4 box) pour 20 ms de budget @50 fps, contre 38,6 ms en numpy. Ce
    // qui reste hors budget, c'est une rotation qui BOUGE (transition / glissement du curseur) :
    // l'empreinte 2D est alors reconstruite à chaque trame (~53 ms en 1080p). L'avertissement ne
    // parle donc plus de la rotation en soi, mais de la rotation ANIMÉE.
    // ★ PLUS DE BAVARDAGE QUAND TOUT VA BIEN (0.12.0). Depuis le noyau C, une rotation FIXE tient
    // largement le budget (8,4 ms en 1080p50 pour 20 ms) : annoncer « c'est confortable » revenait
    // à s'excuser d'un problème qui n'existe plus. Le SEUL cas encore hors budget est la rotation
    // ANIMÉE (transition / curseur qu'on fait glisser) : l'empreinte 2D est alors reconstruite à
    // chaque trame (~53 ms en 1080p). On ne le dit donc QUE là où c'est VRAI : une box de la
    // sélection est effectivement tournée ET le format est lourd. Sinon : rien.
    function rotationWarning() {
        const h = (st && st.height) || 0;
        const rot = sel.some(i => boxes[i] && Math.abs(boxes[i].rotation || 0) > 0.001)
            || Math.abs(gGet().rotation || 0) > 0.001;
        if (!rot || h < 1080) return null;
        return { cls: 'alert-warning', msg: T('rot_anim', 'Rotation ANIMÉE (transition, ou curseur qu\'on fait glisser) hors budget dans ce format : les cartes 2D se reconstruisent à chaque trame (~53 ms en 1080p). À l\'ARRÊT, la rotation ne coûte presque rien (noyau C).') };
    }

    // ─── Panneau de réglages ────────────────────────────────────────────────────────
    // ★ POURQUOI PLUS D'ONGLETS (régression corrigée). La 0.9.1 mettait Géométrie / Effets /
    // Bordure / Ombre dans une barre `.tabs`/`.tab` du système : ELLE ÉTAIT INVISIBLE. Le
    // composant suppose une barre posée sur le fond de PAGE (`--bg`) : l'onglet ACTIF se peint en
    // `--bg-elev` pour paraître « soulevé » sur la surface élevée, les inactifs sont en
    // `--text-muted` sur fond transparent. Posée DANS une carte (fond = `--bg-elev`), la barre
    // peint donc l'onglet actif de la couleur EXACTE de son conteneur, et les inactifs en gris
    // sourd sur ce même fond : plus rien ne se détache. Échec silencieux classique (composant
    // présent, zéro erreur, rien à l'écran). Corrigé À LA RACINE en adoptant la structure de la
    // RÉFÉRENCE (correcteur de couleur), qui n'a pas d'onglets : des SECTIONS empilées + une
    // bascule « mode avancé ». Bonus : plus de contenu caché derrière un onglet qu'on ne voit pas.
    function renderProps() {
        const host = $('#split_props'); if (!host) return;
        if (selGlobal) { renderPropsGlobal(host); return; }   // même panneau, autre cible
        const i = primary();
        if (i < 0 || !boxes[i]) { host.innerHTML = `<p class="meta">${esc(T('no_sel', 'Aucune box sélectionnée.'))}</p>`; return; }
        const b = boxes[i];
        const rw = rotationWarning();   // null = rien à dire (cas nominal)
        const K = (target, label, group, key, unit, shape) => {
            const [min, max, dflt] = cap(group, key);
            const path = target.split(':')[1];
            const val = getV(b, path);
            return knobHtml({ target, label, min, max, step: stepOf(min, max),
                def: (dflt == null ? 0 : dflt), unit, shape, mixed: isMixed(path) }, val);
        };
        host.innerHTML = `
<div class="split-props-head">
  ${sel.length > 1
    ? sel.map(k => `<span class="split-chip-dot" style="background:${BOX_COLORS[k % 4]}"></span>`).join('')
    : `<span class="split-chip-dot" style="background:${BOX_COLORS[i % 4]}"></span>`}
  <h3>${sel.length > 1
    ? esc(sel.length + ' ' + T('multi_head', 'box sélectionnées'))
    : esc(T('box', 'Box') + ' ' + (i + 1))}</h3>
  ${sel.length > 1 ? `<span class="meta">${esc(T('multi_ref', 'référence : Box') + ' ' + (i + 1))}</span>` : ''}
</div>
${sel.length > 1 ? `<p class="meta split-full">${esc(T('multi_note', 'Tout réglage modifié ci-dessous s\'applique à TOUTES les box sélectionnées. Position, taille et z-ordre sont reportés en DÉCALAGE (chaque box garde sa disposition relative) ; l\'apparence (crop, rotation, opacité, bordure, ombre…) est UNIFORMISÉE. Un « ≠ » signale des valeurs divergentes : tant que vous ne touchez pas ce réglage, il n\'est pas écrasé. La source et l\'activation restent propres à chaque box (colonne « Box & fond »).'))}</p>` : ''}

<div class="cc-section">
  <h4>${esc(T('sec_geom', 'Géométrie'))}</h4>
  <div class="split-grid">
    ${numField('sp_x', T('f_x', 'X (centre)'), 'box_fields', 'x', '')}
    ${numField('sp_y', T('f_y', 'Y (centre)'), 'box_fields', 'y', '')}
    ${numField('sp_size', T('f_size', 'Taille'), 'box_fields', 'size', '')}
    <div class="field${isMixed('aspect') ? ' is-mixed' : ''}"><label for="sp_aspect">${esc(T('f_aspect', 'Ratio'))}${isMixed('aspect') ? ' <span class="mixed-mark" aria-hidden="true">≠</span>' : ''}</label>
      <select id="sp_aspect" data-key="aspect" data-path=""
              title="${esc(sel.length > 1 ? T('multi_abs', 'S\'applique à TOUTES les box sélectionnées (uniformisation).') : '')}">
        ${isMixed('aspect') ? `<option value="" selected disabled>${esc(T('mixed_short', 'valeurs multiples'))}</option>` : ''}
        <option value="keep"${(!isMixed('aspect') && b.aspect === 'keep') ? ' selected' : ''}>${esc(T('aspect_keep', 'Conservé (format de la source)'))}</option>
        <option value="free"${(!isMixed('aspect') && b.aspect === 'free') ? ' selected' : ''}>${esc(T('aspect_free', 'Libre (étirement)'))}</option>
      </select></div>
    ${numField('sp_size_x', T('f_size_x', 'Largeur (0 = taille)'), 'box_fields', 'size_x', '')}
    ${numField('sp_size_y', T('f_size_y', 'Hauteur (0 = taille)'), 'box_fields', 'size_y', '')}
    ${numField('sp_z', T('f_z', 'Z (ordre)'), 'box_fields', 'z', '', { nullValue: i, step: 1 })}
  </div>
  <div class="cc-knob-row">
    ${K('box:crop.t', T('k_crop_t', 'Crop H'), 'crop', 't', '')}
    ${K('box:crop.b', T('k_crop_b', 'Crop B'), 'crop', 'b', '')}
    ${K('box:crop.l', T('k_crop_l', 'Crop G'), 'crop', 'l', '')}
    ${K('box:crop.r', T('k_crop_r', 'Crop D'), 'crop', 'r', '')}
  </div>
</div>

<div class="cc-adv-toggle-row">
  <button type="button" class="cc-adv-toggle ${advanced ? 'is-open' : ''}" id="split_adv_toggle"
          aria-expanded="${advanced ? 'true' : 'false'}" aria-controls="split_adv">
    <span class="cc-adv-toggle-chevron" aria-hidden="true">▾</span>
    <span>${esc(advanced ? T('adv_hide', 'Masquer le mode avancé') : T('adv_show', 'Afficher le mode avancé'))}</span>
  </button>
</div>

${!advanced ? '' : `<div id="split_adv">
  <div class="cc-section">
    <h4>${esc(T('sec_fx', 'Effets'))}</h4>
    <div class="cc-knob-row">
      ${K('box:rotation', T('k_rotation', 'Rotation'), 'box_fields', 'rotation', '°', 'angle')}
      ${K('box:opacity', T('k_opacity', 'Opacité'), 'box_fields', 'opacity', '')}
      ${K('box:feather', T('k_feather', 'Adoucis.'), 'box_fields', 'feather', 'px')}
      ${K('box:radius', T('k_radius', 'Coins'), 'box_fields', 'radius', 'px')}
    </div>
    ${rw ? `<div class="alert ${rw.cls}" id="split_rot_warn">${esc(rw.msg)}</div>` : ''}
    <div class="split-toggles">
      ${toggleField('mirror_h', '', T('f_mirror_h', 'Miroir horizontal'))}
      ${toggleField('mirror_v', '', T('f_mirror_v', 'Miroir vertical'))}
    </div>
  </div>

  <div class="cc-section">
    <h4>${esc(T('sec_border', 'Bordure'))}</h4>
    <div class="split-grid">
      ${numField('sp_bw', T('f_border_w', 'Trait plat (px)'), 'border_fields', 'w', 'border', { step: 1 })}
      ${colorField('sp_bc', T('f_border_color', 'Couleur'), 'border', 'color')}
      ${numField('sp_bo', T('f_border_outer', 'Extérieur (px)'), 'border_fields', 'outer', 'border')}
      ${numField('sp_bi', T('f_border_inner', 'Intérieur (px)'), 'border_fields', 'inner', 'border')}
    </div>
    <div class="cc-knob-row">
      ${K('box:border.bevel', T('k_bevel', 'Biseau'), 'border_fields', 'bevel', '')}
      ${K('box:border.bevel_soft', T('k_bevel_soft', 'Adoucis.'), 'border_fields', 'bevel_soft', '')}
      ${K('box:border.bevel_pos', T('k_bevel_pos', 'Position'), 'border_fields', 'bevel_pos', '')}
    </div>
    <p class="meta">${esc(T('border_note', 'Le relief du biseau vient de la source de lumière (globale, plus bas) : sans elle, un biseau n\'est qu\'un dégradé.'))}</p>
  </div>

  <div class="cc-section">
    <h4>${esc(T('sec_shadow', 'Ombre portée'))}
      <label class="split-inline-switch"><input type="checkbox" class="ios-toggle" data-flag="enabled" data-path="shadow"
             ${b.shadow.enabled ? 'checked' : ''} aria-label="${esc(T('f_shadow_on', 'Activer l\'ombre portée'))}"></label>
    </h4>
    <div class="cc-knob-row ${b.shadow.enabled ? '' : 'is-off'}">
      ${K('box:shadow.dx', T('k_shadow_dx', 'Décal. X'), 'shadow_fields', 'dx', 'px')}
      ${K('box:shadow.dy', T('k_shadow_dy', 'Décal. Y'), 'shadow_fields', 'dy', 'px')}
      ${K('box:shadow.blur', T('k_shadow_blur', 'Flou'), 'shadow_fields', 'blur', 'px')}
      ${K('box:shadow.opacity', T('k_shadow_op', 'Opacité'), 'shadow_fields', 'opacity', '')}
    </div>
    <div class="split-grid">${colorField('sp_scol', T('f_shadow_color', 'Couleur'), 'shadow', 'color')}</div>
    ${b.shadow.enabled ? '' : `<p class="meta">${esc(T('shadow_off', 'Activez l\'ombre portée (interrupteur ci-dessus) pour régler ces valeurs.'))}</p>`}
  </div>
</div>`}`;

        const at = $('#split_adv_toggle');
        if (at) at.onclick = () => { advanced = !advanced; renderProps(); };
        host.querySelectorAll('input[type=number], input[type=color], select[data-key]').forEach(inp => {
            if (inp.classList.contains('cc-knob-value')) return;   // géré par bindKnobs
            inp.onchange = () => onFieldChange(inp);
            // Champs SIMPLES (X/Y, tailles, z, largeurs de bordure) : ils portent aussi les
            // boutons −/+ (stepper global). Même précaution qu'aux rotatifs pour une sélection
            // aux VALEURS DIVERGENTES (champ vide) : amorcer sur la valeur de référence, sinon
            // le − /+ partirait de zéro. (Capture : les boutons sont enrobés après coup.)
            const f = inp.closest('.field');
            if (f && inp.type === 'number' && inp.dataset.ref !== undefined) {
                f.addEventListener('pointerdown', e => {
                    const btn = e.target && e.target.closest && e.target.closest('.num-btn');
                    if (btn && inp.value === '') inp.value = (+inp.dataset.ref || 0);
                }, true);
            }
        });
        host.querySelectorAll('input[data-flag]').forEach(cb => {
            if (cb.dataset.indeterminate) cb.indeterminate = true;   // états divergents : ni coché ni décoché
            cb.onchange = () => {
                const path = cb.dataset.path, key = cb.dataset.flag;
                applySetting(path ? `${path}.${key}` : key, cb.checked);   // ★ toute la sélection
                renderProps();
            };
        });
        bindKnobs(host);
        syncFieldsDisabled();
    }
    function onFieldChange(inp) {
        if (primary() < 0) return;
        const key = inp.dataset.key, path = inp.dataset.path;
        const full = path ? `${path}.${key}` : key;
        let val = (inp.type === 'number') ? parseFloat(inp.value) : inp.value;
        if (inp.type === 'number' && isNaN(val)) return;     // champ vidé (mixte) : on ne touche à RIEN
        applySetting(full, val);                             // ★ toute la sélection
        renderProps();
    }
    // Ratio conservé → largeur/hauteur libres sans effet : DÉSACTIVÉES en le DISANT.
    function syncFieldsDisabled() {
        const b = boxes[primary()]; if (!b) return;
        const free = (b.aspect === 'free');
        ['sp_size_x', 'sp_size_y'].forEach(id => {
            const el = $('#' + id); if (!el) return;
            el.disabled = !free;
            el.title = free ? '' : T('free_only', 'Actif seulement en ratio « Libre » : à ratio conservé, la box garde le format de sa source et une seule taille suffit.');
            const f = el.closest('.field'), lab = f && f.querySelector('label');
            if (lab) lab.title = el.title;
        });
        const sc = $('#sp_scol');
        if (sc) {
            sc.disabled = !b.shadow.enabled;
            sc.title = b.shadow.enabled ? '' : T('shadow_off', 'Activez l\'ombre portée (interrupteur ci-dessus) pour régler ces valeurs.');
        }
    }

    // ─── LE MÊME PANNEAU, CIBLE = CANAL GLOBAL ──────────────────────────────────────
    // ★ Aucun composant maison : ce sont les cadrans du panneau des box (knobHtml/bindKnobs),
    // les bornes et les pas viennent de /state.caps.global_fields, les steppers −/+ globaux
    // s'y posent tout seuls, et le double-clic sur un cadran remet la valeur par défaut (c'est
    // le « remettre à neutre » du système — le bouton dédié de la 0.12.0 faisait DOUBLON : retiré).
    // Les réglages qui ne s'appliquent pas au Global sont MASQUÉS — mais on DIT pourquoi, une
    // fois, en tête de panneau (les faire disparaître sans un mot, c'est l'échec silencieux).
    function renderPropsGlobal(host) {
        const g = gGet();
        const rw = rotationWarning();
        const K = (key, label, unit, shape) => {
            const [min, max, dflt] = cap('global_fields', key);
            return knobHtml({ target: 'glob:' + key, label, min, max, step: stepOf(min, max),
                              def: (dflt == null ? 0 : dflt), unit, shape }, g[key]);
        };
        host.innerHTML = `
<div class="split-props-head">
  <span class="split-chip-dot split-dot-global"></span>
  <h3>${esc(T('global', 'Global (groupe)'))}</h3>
  ${!gNeutral(g) ? `<span class="split-badge-on">${esc(T('global_on', 'actif'))}</span>` : ''}
</div>
<p class="meta split-full">${esc(T('global_scope', 'Le Global transforme le GROUPE : il se compose par-dessus les réglages de chaque box (rendu = Global ∘ box), sans en écraser aucun — les box gardent leurs positions relatives. C\'est ce qui permet de faire entrer ou sortir tout le groupe du cadre. Les attributs de BORD d\'une box (crop, adoucissement, coins, bordure, biseau, ombre, miroir, ratio) et ce qui est propre à une box (source, activation, z-ordre) ne s\'appliquent donc pas à lui : ils ne sont pas affichés ici.'))}</p>
<div class="cc-section">
  <h4>${esc(T('g_sec_geo', 'Transformation du groupe'))}</h4>
  <div class="cc-knob-row">
    ${K('dx', T('g_dx', 'Décal. X'), '', '')}
    ${K('dy', T('g_dy', 'Décal. Y'), '', '')}
    ${K('scale', T('g_scale', 'Échelle'), '×', '')}
    ${K('rotation', T('g_rot', 'Rotation'), '°', 'angle')}
    ${K('opacity', T('g_op', 'Opacité'), '%', '')}
  </div>
  ${rw ? `<div class="alert ${rw.cls}">${esc(rw.msg)}</div>` : ''}
  <p class="meta">${esc(T('g_hint', 'Double-clic sur un cadran = valeur par défaut (le Global redevient neutre réglage par réglage). Il est enregistré dans les mémoires et interpolé pendant les transitions. Coût mesuré : nul — il est replié dans l\'empreinte de chaque box. Tant qu\'il n\'est pas neutre, le magnétisme du canvas est désactivé (aimanter dans un repère tourné donnerait des guides faux).'))}</p>
</div>`;
        bindKnobs(host);
    }

    // ─── FOND : mode (noir / source / couleur) ──────────────────────────────────────
    // Les modes viennent de /state.caps.bg_modes (rien en dur). La couleur est saisie en sRGB
    // (hex) : c'est le MOTEUR qui la convertit — à SA profondeur (8/10/12 bits) et à SA
    // colorimétrie déclarée. On ne persiste jamais du YUV.
    const bgModes = () => (caps && caps.bg_modes) || ['none', 'source', 'color'];
    const BG_LBL = { none: ['bg_m_none', 'Noir'], source: ['bg_m_source', 'Source vidéo'],
                     color: ['bg_m_color', 'Couleur unie'], gradient: ['bg_m_grad', 'Dégradé / wipe'] };
    function bgModeOptions() {
        const cur = ((st && st.bg) || {}).mode || 'none';
        return bgModes().map(m => {
            const [k, fb] = BG_LBL[m] || [m, m];
            return `<option value="${esc(m)}"${cur === m ? ' selected' : ''}>${esc(T(k, fb))}</option>`;
        }).join('');
    }
    // Éditeur du DÉGRADÉ : réutilise les cadrans (angle, douceur) et une liste d'arrêts (couleur
    // + position). Bornes et nombre d'arrêts min/max lus dans /state.caps — rien en dur.
    const gradCap = k => (caps && caps.gradient_fields && caps.gradient_fields[k]) || [0, 1, 0];
    const gradNStops = () => (caps && caps.gradient_stops) || [2, 4];
    function renderBgGradient() {
        const host = $('#split_bg_grad_f'); if (!host) return;
        const bg = (st && st.bg) || {};
        if (bg.mode !== 'gradient') { host.style.display = 'none'; host.innerHTML = ''; return; }
        host.style.display = '';
        const g = bg.gradient || { angle: 0, softness: 1, stops: [] };
        const stops = g.stops || [];
        const [amin, amax, adef] = gradCap('angle'), [smin, smax, sdef] = gradCap('softness');
        const [nmin, nmax] = gradNStops();
        const knob = (key, label, unit, shape) => {
            const [mn, mx, df] = gradCap(key);
            return knobHtml({ target: 'bggrad:' + key, label, min: mn, max: mx, step: stepOf(mn, mx),
                              def: df, unit, shape }, g[key]);
        };
        const rows = stops.map((sp, i) => `
<div class="split-grad-stop" data-i="${i}">
  <input type="color" class="split-grad-col" data-i="${i}" value="${esc(sp.color || '#000000')}">
  <input type="number" class="split-grad-pos" data-i="${i}" min="0" max="1" step="0.01"
         data-ref="${Number(sp.pos || 0)}" value="${Number(sp.pos || 0).toFixed(2)}"
         aria-label="${esc(T('grad_pos', 'Position'))}">
  <button type="button" class="btn split-grad-del" data-i="${i}" ${stops.length <= nmin ? 'disabled' : ''}
          title="${esc(T('grad_del', 'Retirer cet arrêt'))}">✕</button>
</div>`).join('');
        host.innerHTML = `
<div class="cc-knob-row">
  ${knob('angle', T('grad_angle', 'Angle'), '°', 'angle')}
  ${knob('softness', T('grad_soft', 'Douceur'), '', '')}
</div>
<p class="meta">${esc(T('grad_soft_note', 'Douceur 0 = frontière NETTE (wipe) ; 1 = dégradé progressif. Un wipe n\'est qu\'un dégradé de douceur nulle.'))}</p>
<div class="split-grad-stops">${rows}</div>
${stops.length < nmax ? `<button type="button" class="btn" id="split_grad_add">${esc(T('grad_add', '+ Ajouter un arrêt'))}</button>` : ''}
<p class="meta">${esc(T('grad_anim_note', 'Le dégradé est enregistré dans les mémoires et interpolé pendant les transitions : deux mémoires (l\'une position 0, l\'autre position 1) rappelées avec une durée donnent un WIPE ANIMÉ qui balaie l\'écran — sans mécanique nouvelle. Statique il ne coûte rien (rendu caché) ; animé plein cadre il tient le 1080p50 pour 2 arrêts, il est au budget à 3-4 (cf. aide).'))}</p>`;
        host.querySelectorAll('.split-grad-col').forEach(el => el.onchange = () => setGradStop(+el.dataset.i, { color: el.value }));
        host.querySelectorAll('.split-grad-pos').forEach(el => el.onchange = () => { const v = parseFloat(el.value); if (!isNaN(v)) setGradStop(+el.dataset.i, { pos: Math.max(0, Math.min(1, v)) }); });
        host.querySelectorAll('.split-grad-del').forEach(el => el.onclick = () => delGradStop(+el.dataset.i));
        const add = $('#split_grad_add'); if (add) add.onclick = addGradStop;
        bindKnobs(host);
    }
    function curStops() { return (((st && st.bg) || {}).gradient || {}).stops || []; }
    function setGradStop(i, patch) {
        const stops = curStops().map((s, j) => j === i ? Object.assign({}, s, patch) : Object.assign({}, s));
        postBg({ gradient: { stops } });
    }
    function addGradStop() {
        const stops = curStops().map(s => Object.assign({}, s));
        const last = stops[stops.length - 1] || { pos: 0.5, color: '#808080' };
        stops.push({ color: last.color, pos: Math.min(1, (last.pos + 0.5) / 1) });   // milieu vers la fin
        postBg({ gradient: { stops } });
    }
    function delGradStop(i) {
        const stops = curStops().filter((_, j) => j !== i).map(s => Object.assign({}, s));
        postBg({ gradient: { stops } });
    }
    function bindBg() {
        const bg = (st && st.bg) || { mode: 'none' };
        const srcF = $('#split_bg_src_f'), colF = $('#split_bg_col_f');
        if (srcF) srcF.style.display = (bg.mode === 'source') ? '' : 'none';
        if (colF) colF.style.display = (bg.mode === 'color') ? '' : 'none';
        renderBgGradient();
        const lg = $('#split_bg_legal'); if (lg) lg.checked = (bg.legal !== false);
        const md = $('#split_bg_mode'); if (md) md.onchange = () => postBg({ mode: md.value });
        const col = $('#split_bg_color'); if (col) col.onchange = () => postBg({ color: col.value });
        if (lg) lg.onchange = () => postBg({ legal: lg.checked });
        // ★ JAMAIS MUET : fond « source » sans slot câblé → l'image sera NOIRE ; couleur hors
        // plage légale → elle a été ÉCRÊTÉE. On le dit, on ne corrige rien d'office.
        const w = $('#split_bg_warn'); if (!w) return;
        let html = '';
        if (bg.mode === 'source' && !inputs[4]) {
            html = `<div class="alert alert-warning">${esc(T('bg_w_nosrc', 'Fond « source vidéo » mais aucune source câblée : le fond sera NOIR. Choisissez une source ci-dessus, ou passez le fond en noir/couleur.'))}</div>`;
        } else if (bg.mode === 'color' && bg.clipped) {
            html = `<div class="alert alert-warning">${esc(T('bg_w_clip', 'Couleur HORS de la plage légale (broadcast) : elle a été écrêtée à la plage encodable. Décochez « Plage légale » pour l\'encoder en plage complète (au risque d\'un écrêtage en aval).'))}</div>`;
        }
        w.innerHTML = html;
    }
    async function postBg(patch) {
        if (VMID == null) return;
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/bg`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
            });
            const j = await r.json().catch(() => ({}));
            if (!r.ok || j.error) showError(T('bg_err', 'Fond refusé') + ' : ' + (j.error || ('HTTP ' + r.status)));
        } catch (e) { showError(T('bg_err', 'Fond refusé') + ' : ' + e.message); }
        await fetchState({ rebuild: false });
        renderShell();
    }

    async function postGlobal(patch) {
        if (VMID == null) return;
        if (st) st.global = Object.assign({}, gGet(), patch);   // aperçu immédiat
        drawCanvas();
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/global`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
            });
            const j = await r.json().catch(() => ({}));
            if (!r.ok || j.error) showError(T('global_err', 'Global refusé') + ' : ' + (j.error || ('HTTP ' + r.status)));
            else if (j.global && st) st.global = j.global;
        } catch (e) { showError(T('global_err', 'Global refusé') + ' : ' + e.message); }
        drawCanvas();
    }

    // ─── Habillage : AVERTISSEMENTS (0.10.1) ────────────────────────────────────────
    // ★ LE PIÈGE PAYÉ EN PROD : une photo SANS canal de transparence (JPEG, PNG opaque) importée
    // avec la clé « alpha » est opaque PARTOUT ; posée AU-DESSUS, elle recouvre intégralement les
    // 4 box. L'utilisateur voyait sa photo plein cadre, ses box « disparues », et RIEN ne le lui
    // disait (le moteur cuisait l'image sans broncher). Le moteur publie désormais `art.warn`
    // (`no_alpha`, `covers_all`), `art.has_alpha` et `art.cover` : on les AFFICHE, et on ne
    // corrige RIEN d'office (un habillage plein cadre peut être voulu — c'est son choix, pas le
    // nôtre : jamais de correction muette, jamais de contrôle muet).
    function artWarnHtml(a) {
        const w = (a && a.warn) || [];
        if (!w.length) return '';
        const pct = (a.cover == null) ? null : Math.round(a.cover * 100);
        const out = [];
        if (w.indexOf('no_alpha') >= 0) {
            out.push(`<div class="alert alert-warning">⚠ ${esc(T('art_w_noalpha',
                'Cette image n\'a AUCUN canal de transparence (JPEG, ou PNG opaque). Avec la clé « alpha », elle est donc entièrement opaque et MASQUERA les box. Remèdes : importer un PNG à fond transparent, choisir la clé « luma » ou « chroma », ou placer l\'habillage EN DESSOUS des box.'))}</div>`);
        }
        if (w.indexOf('covers_all') >= 0) {
            const holes = (a.holes == null) ? null : Math.round(a.holes * 100);
            const det = (pct == null) ? '' : ` (${T('art_w_cover_lbl', 'opacité moyenne')} ${pct} %` +
                (holes == null ? '' : `, ${T('art_w_holes_lbl', 'trous')} ${holes} %`) + ')';
            out.push(`<div class="alert alert-error">⚠ ${esc(T('art_w_covers',
                'L\'habillage est opaque sur la QUASI-TOTALITÉ du cadre et il est posé AU-DESSUS : il recouvre intégralement les box (elles sont bien composées, puis effacées). Décochez « Au-dessus des box », changez de clé, ou baissez l\'opacité.'))}${esc(det)}</div>`);
        }
        return out.join('');
    }

    // ─── Habillage (couche ART) ─────────────────────────────────────────────────────
    function renderArt() {
        const host = $('#split_art'); if (!host || !st) return;
        const a = st.art || {};
        const noImg = !a.has_image;
        const keyOpts = artKeys().map(k => `<option value="${esc(k)}"${a.key === k ? ' selected' : ''}>${esc(T('artkey_' + k, k))}</option>`).join('');
        host.className = 'cc-section';
        host.innerHTML = `
<h4>${esc(T('art', 'Habillage (couche ART)'))}
  <label class="split-inline-switch"><input type="checkbox" class="ios-toggle" id="art_en" ${a.enabled ? 'checked' : ''}
         aria-label="${esc(T('art_enabled', 'Activer l\'habillage'))}"></label></h4>
<div class="row">
  <button type="button" class="btn btn-blue" id="art_pick">${esc(T('art_import', 'Importer une image…'))}</button>
  <input type="file" id="art_file" accept="image/png,image/jpeg" hidden>
  <span class="meta" id="art_state"></span>
</div>
<div class="split-toggles">
  <label class="toggle-inline"><input type="checkbox" class="ios-toggle" id="art_above" ${a.above ? 'checked' : ''}>
    <span>${esc(T('art_above', 'Au-dessus des box'))}</span></label>
  <label class="toggle-inline"><input type="checkbox" class="ios-toggle" id="art_inv" ${a.invert ? 'checked' : ''}>
    <span>${esc(T('art_invert', 'Inverser la clé'))}</span></label>
</div>
<div class="split-grid split-grid-narrow">
  <div class="field"><label for="art_key">${esc(T('art_key', 'Incrustation'))}</label>
    <select id="art_key">${keyOpts}</select></div>
  <div class="field"><label for="art_chroma">${esc(T('art_chroma', 'Couleur de clé (chroma)'))}</label>
    <input type="color" id="art_chroma" value="${esc(a.chroma_key || '#00b140')}"></div>
</div>
<div class="cc-knob-row ${noImg ? 'is-off' : ''}" id="art_knobs">
  ${knobHtml({ target: 'art:clip', label: T('k_clip', 'Seuil'), min: 0, max: 1, step: 0.01, def: 0.15, unit: '' }, a.clip)}
  ${knobHtml({ target: 'art:gain', label: T('k_gain', 'Gain'), min: 0.01, max: 16, step: 0.01, def: 1, unit: '' }, a.gain)}
  ${knobHtml({ target: 'art:opacity', label: T('k_art_op', 'Opacité'), min: 0, max: 1, step: 0.01, def: 1, unit: '' }, a.opacity)}
</div>
${a.err ? `<div class="alert alert-error">${esc(a.err)}</div>` : ''}
${artWarnHtml(a)}
${noImg ? `<p class="meta">${esc(T('art_need_img', 'Importez d\'abord une image d\'habillage : sans image, l\'incrustation n\'a pas d\'objet.'))}</p>` : ''}
<p class="meta">${esc(T('art_note', 'Image importée (PNG/JPEG), pas une 6ᵉ entrée vidéo : un habillage vidéo live coûterait 21,7 ms/trame en 1080p (108 % du budget 50 fps). Un PNG à fond transparent + clé « alpha » est le cas le plus propre. L\'image n\'est pas persistée : à réimporter après un redéploiement du conteneur.'))}</p>`;
        $('#art_state').textContent = noImg ? T('art_none', 'Aucune image importée.')
            : (a.loaded ? T('art_loaded', 'Image chargée et clé précalculée.') : T('art_pending', 'Image importée — activez l\'habillage pour la cuire.'));
        $('#art_en').onchange = function () { postArt({ enabled: this.checked }); };
        $('#art_above').onchange = function () { postArt({ above: this.checked }); };
        $('#art_inv').onchange = function () { postArt({ invert: this.checked }); };
        $('#art_key').onchange = function () { postArt({ key: this.value }); };
        $('#art_chroma').onchange = function () { postArt({ chroma_key: this.value }); };
        $('#art_pick').onclick = () => $('#art_file').click();
        $('#art_file').onchange = function () { onArtFile(this); };
        // Sans image, les réglages de clé n'ont pas d'objet : désactivés ET expliqués (ci-dessus).
        ['art_en', 'art_above', 'art_inv', 'art_key', 'art_chroma'].forEach(id => {
            const el = $('#' + id); if (!el || !noImg) return;
            el.disabled = true;
            el.title = T('art_need_img', 'Importez d\'abord une image d\'habillage : sans image, l\'incrustation n\'a pas d\'objet.');
        });
        bindKnobs(host);
    }
    async function onArtFile(input) {
        const f = input.files[0]; if (!f) return; input.value = '';
        if (f.size > 12 * 1024 * 1024) { showError(T('art_too_big', 'Image trop lourde (max 12 Mo).')); return; }
        const b64 = await new Promise((res, rej) => {
            const rd = new FileReader();
            rd.onload = () => res(String(rd.result));
            rd.onerror = () => rej(new Error('read'));
            rd.readAsDataURL(f);
        });
        await postArt({ image: b64, enabled: true });
    }
    async function postArt(patch) {
        if (VMID == null) return;
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/art`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
            });
            const j = await r.json().catch(() => ({}));
            if (!r.ok || j.error) showError(T('art_err', 'Habillage refusé') + ' : ' + (j.error || ('HTTP ' + r.status)));
        } catch (e) { showError(T('art_err', 'Habillage refusé') + ' : ' + e.message); }
        await fetchState({ rebuild: false });
        renderArt();
    }

    // ─── Lumière & transitions ──────────────────────────────────────────────────────
    function renderLook() {
        const host = $('#split_look'); if (!host || !st) return;
        const L = st.light || {};
        // Un moteur antérieur au contrat 0.9.0 final ne publie pas `transition_defaults` : plutôt
        // que d'afficher une courbe FAUSSE en silence, on retombe sur le défaut du manifeste ET on le dit.
        const stale = !st.transition_defaults;
        const TR = st.transition_defaults || { curve: 'ease_in_out', stagger_ms: 0 };
        const [amin, amax, adef] = cap('light_fields', 'angle');
        const [hmin, hmax, hdef] = cap('light_fields', 'altitude');
        const [smin, smax] = cap('stagger');
        host.className = 'cc-section';
        host.innerHTML = `
<h4>${esc(T('look', 'Lumière & transitions'))}</h4>
<div class="cc-knob-row">
  ${knobHtml({ target: 'light:angle', label: T('k_light_angle', 'Direction'), min: amin, max: amax, step: 1, def: adef, unit: '°', shape: 'angle' }, L.angle)}
  ${knobHtml({ target: 'light:altitude', label: T('k_light_alt', 'Hauteur'), min: hmin, max: hmax, step: 1, def: hdef, unit: '°' }, L.altitude)}
</div>
<div class="split-grid split-grid-narrow">
  <div class="field"><label for="lt_curve">${esc(T('t_curve', 'Courbe de transition'))}</label>
    <select id="lt_curve">${easings().map(c => `<option value="${esc(c)}"${TR.curve === c ? ' selected' : ''}>${esc(T('easing_' + c, c))}</option>`).join('')}</select></div>
  <div class="field"><label for="lt_stag">${esc(T('t_stagger', 'Décalage inter-box (ms)'))}</label>
    <input type="number" id="lt_stag" min="${smin}" max="${smax}" step="10" value="${Number(TR.stagger_ms || 0).toFixed(0)}"></div>
</div>
<p class="meta">${esc(T('look_note', 'La lumière donne le relief des bordures biseautées — une seule lumière éclaire toute la scène (quatre lumières incohérentes = décor faux). Le décalage fait arriver les box l\'une après l\'autre ; il est compté DANS la durée demandée.'))}</p>
${stale ? `<div class="alert alert-warning">${esc(T('stale_engine', 'Le moteur déployé sur ce conteneur ne publie pas ses réglages de transition (script antérieur au contrat 0.9.0) : les valeurs ci-dessus sont les défauts du manifeste, pas l\'état réel. Redéployez le conteneur pour lever l\'ambiguïté.'))}</div>` : ''}`;
        $('#lt_curve').onchange = function () { postTransition({ curve: this.value }); };
        $('#lt_stag').onchange = function () { postTransition({ stagger_ms: parseFloat(this.value) }); };
        bindKnobs(host);
    }
    async function postLight(patch) {
        if (VMID == null) return;
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/light`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
            });
            if (r.ok) { const j = await r.json().catch(() => ({})); if (j.light && st) st.light = j.light; }
        } catch (e) { showError(T('light_err', 'Réglage de lumière refusé') + ' : ' + e.message); }
        schedulePersist();
    }
    async function postTransition(patch) {
        if (VMID == null) return;
        const body = Object.assign({ curve: (st.transition_defaults || {}).curve }, patch);
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/transition`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            const j = await r.json().catch(() => ({}));
            if (r.ok && j.transition) st.transition_defaults = j.transition;
            else if (!r.ok) showError(T('trans_err', 'Réglage de transition refusé') + ' : ' + (j.error || ('HTTP ' + r.status)));
        } catch (e) { showError(T('trans_err', 'Réglage de transition refusé') + ' : ' + e.message); }
        schedulePersist();
    }

    // ─── Budget de trame (:8080 adv) — jamais de décrochage muet ────────────────────
    function updateBudget() {
        const host = $('#split_budget'); if (!host) return;
        if (metricsErr || !adv) {
            host.className = 'split-budget';
            host.innerHTML = `<span>${esc(T('budget', 'Budget de trame'))}</span><span class="split-budget-val">—</span>`;
            host.title = T('budget_na', 'Métriques du moteur indisponibles (:8080) — le conteneur tourne-t-il ?');
            return;
        }
        const pct = adv.budget_pct, rot = adv.rotated_boxes || 0;
        const over = !!adv.over_budget, warn = (pct != null && pct > 80);
        host.className = 'split-budget' + (over ? ' over' : (warn ? ' warn' : ''));
        host.innerHTML = `<span>${esc(T('budget', 'Budget de trame'))}</span>` +
            `<span class="split-budget-val">${pct == null ? '—' : pct + ' %'}${rot ? esc(' · ' + T('budget_rot', 'rotation') + ' ×' + rot) : ''}</span>`;
        host.title = over ? T('budget_over_t', 'Le compositing dépasse le budget de trame : le Split décroche (fps sous la consigne). La rotation est de loin le poste le plus lourd — la retirer est le premier geste.')
            : (warn ? T('budget_warn_t', 'Le compositing approche du budget de trame — plus de marge pour un effet supplémentaire.')
                : T('budget_ok_t', 'Coût de composition par trame, rapporté au budget de la cadence (100 % = décrochage).'));
        const w = $('#split_rot_warn');
        if (w && over && rot) {
            w.className = 'alert alert-error';
            w.textContent = T('rot_live_over', 'Le moteur dépasse actuellement le budget de trame et ') + rot +
                T('rot_live_over2', ' box est/sont en rotation : remettez la rotation à 0 pour retrouver la cadence.');
        }
    }

    // ─── Snap magnétique (miroir de computeSnap/computeSnapResize du composer) ───────
    // ★ CIBLES DE MAGNÉTISME (corrigé 0.12.2) — LE BORD DU CADRE EST LA CIBLE N°1.
    //
    // BUG INTRODUIT EN 0.12.0 (hors-cadre) : ces cibles étaient `0` et `CW` — c'est-à-dire les
    // bords du CANVAS. Or depuis que le canvas entoure le cadre d'une MARGE, les bords du canvas
    // ne sont plus le cadre : ils tombent DANS le hors-cadre. Le bord de l'image avait donc PERDU
    // tout magnétisme, et se caler pile contre l'arête était devenu impossible (« on ne peut pas
    // se positionner facilement contre un bord »). Les cibles sont désormais les arêtes du CADRE
    // DE SORTIE (rectangle intérieur), avec le MÊME seuil que les autres (SNAP_PX) : la cible la
    // plus utile n'est pas la plus dure à attraper.
    //
    // ★ ET LES DEUX GESTES SONT SERVIS D'UN COUP, sans cible supplémentaire : les arêtes de la
    // box testées sont {gauche, droite, centre} contre les cibles {bord gauche, bord droit,
    // centre du cadre} — donc
    //     • box AFFLEURANTE À L'INTÉRIEUR : arête gauche ≡ bord gauche (entièrement visible) ;
    //     • box ENTIÈREMENT DEHORS, juste au-delà : arête DROITE ≡ bord GAUCHE (position de
    //       départ/arrivée naturelle d'une entrée/sortie de champ).
    // Idem en haut/bas. C'est la combinatoire {arêtes} × {bords} qui donne les deux, gratuitement.
    function snapTargets(excludeIdx, CW, CH) {
        const V = view(CW, CH);
        const xT = [V.MX, V.MX + V.FW, V.MX + V.FW / 2];   // bord GAUCHE, bord DROIT, centre
        const yT = [V.MY, V.MY + V.FH, V.MY + V.FH / 2];   // bord HAUT, bord BAS, centre
        boxes.forEach((b, i) => {
            if (i === excludeIdx || !b.enabled) return;
            const r = rectPx(i, CW, CH);
            xT.push(r.x0, r.x0 + r.w, r.x0 + r.w / 2);
            yT.push(r.y0, r.y0 + r.h, r.y0 + r.h / 2);
        });
        return { xT, yT };
    }
    function computeSnap(excludeIdx, x, y, w, h, CW, CH) {
        const { xT, yT } = snapTargets(excludeIdx, CW, CH), guides = [], SP = SNAP_PX();
        let bestDx = SP + 1, snapX = x;
        [{ e: 'l', v: x }, { e: 'r', v: x + w }, { e: 'c', v: x + w / 2 }].forEach(({ e, v }) => {
            xT.forEach(t => {
                const d = Math.abs(v - t);
                if (d <= SP && d < bestDx) {
                    bestDx = d; snapX = e === 'l' ? t : e === 'r' ? t - w : t - w / 2;
                    guides[0] = { type: 'v', pos: t };
                }
            });
        });
        let bestDy = SP + 1, snapY = y;
        [{ e: 't', v: y }, { e: 'b', v: y + h }, { e: 'c', v: y + h / 2 }].forEach(({ e, v }) => {
            yT.forEach(t => {
                const d = Math.abs(v - t);
                if (d <= SP && d < bestDy) {
                    bestDy = d; snapY = e === 't' ? t : e === 'b' ? t - h : t - h / 2;
                    guides[1] = { type: 'h', pos: t };
                }
            });
        });
        return { x: snapX, y: snapY, guides: guides.filter(Boolean) };
    }
    // Resize : le Split garde le RATIO (contrairement au PiP, W/H indépendants) → on retient le
    // facteur d'échelle de l'arête la plus proche.
    function computeSnapResize(excludeIdx, r, CW, CH) {
        const { xT, yT } = snapTargets(excludeIdx, CW, CH), SP = SNAP_PX();
        let best = SP + 1, factor = 1, guides = [];
        xT.forEach(t => {
            const d = Math.abs((r.x0 + r.w) - t);
            if (d <= SP && d < best && t > r.x0 && r.w > 0) { best = d; factor = (t - r.x0) / r.w; guides = [{ type: 'v', pos: t }]; }
        });
        yT.forEach(t => {
            const d = Math.abs((r.y0 + r.h) - t);
            if (d <= SP && d < best && t > r.y0 && r.h > 0) { best = d; factor = (t - r.y0) / r.h; guides = [{ type: 'h', pos: t }]; }
        });
        return { factor, guides };
    }
    function drawGuides(ctx, CW, CH, css) {
        if (!drag || !snapGuides.length) return;
        ctx.save();
        ctx.strokeStyle = (css.getPropertyValue('--status-warning-fg') || '#c4a667').trim();
        ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
        snapGuides.forEach(g => {
            ctx.beginPath();
            if (g.type === 'v') { ctx.moveTo(g.pos, 0); ctx.lineTo(g.pos, CH); }
            else { ctx.moveTo(0, g.pos); ctx.lineTo(CW, g.pos); }
            ctx.stroke();
        });
        ctx.restore();
    }

    // ─── Canvas ─────────────────────────────────────────────────────────────────────
    function zOrder() {
        return boxes.map((b, i) => ({ i, z: (b.z == null ? i : b.z) }))
            .sort((a, b_) => (a.z - b_.z) || (a.i - b_.i)).map(o => o.i);
    }
    function rectPx(i, CW, CH) {
        const r = rectN(gTransform(boxes[i])), v = view(CW, CH);
        return { x0: v.MX + r.x0 * v.FW, y0: v.MY + r.y0 * v.FH, w: r.w * v.FW, h: r.h * v.FH,
                 cx: v.MX + r.cx * v.FW, cy: v.MY + r.cy * v.FH };
    }
    function pathRect(ctx, r, radPx) {
        const rad = Math.max(0, Math.min(radPx, Math.min(r.w, r.h) / 2));
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(r.x0, r.y0, r.w, r.h, rad);
        else ctx.rect(r.x0, r.y0, r.w, r.h);
    }
    // Aperçu FIDÈLE du dégradé (douceur comprise) : on échantillonne la MÊME rampe que le moteur
    // (frontière nette à douceur 0 = wipe ; rampe à douceur 1) en RGB — la couleur exacte reste
    // celle du rendu YUV, mais l'allure (wipe vs dégradé, angle, arrêts) est juste.
    function _rgb(h) { h = String(h || '#000000').replace('#', ''); if (h.length === 3) h = h.split('').map(c => c + c).join(''); return [parseInt(h.slice(0, 2), 16) || 0, parseInt(h.slice(2, 4), 16) || 0, parseInt(h.slice(4, 6), 16) || 0]; }
    function _gradSample(t, stops, soft) {
        const n = stops.length;
        if (t <= stops[0].pos) return _rgb(stops[0].color);
        if (t >= stops[n - 1].pos) return _rgb(stops[n - 1].color);
        let i = 0; while (i < n - 2 && t >= stops[i + 1].pos) i++;
        const seg = stops[i + 1].pos - stops[i].pos;
        const local = seg > 1e-9 ? (t - stops[i].pos) / seg : 0;
        let w = soft <= 1e-6 ? (local < 0.5 ? 0 : 1) : Math.max(0, Math.min(1, (local - 0.5) / soft + 0.5));
        const a = _rgb(stops[i].color), b = _rgb(stops[i + 1].color);
        return [0, 1, 2].map(k => Math.round(a[k] * (1 - w) + b[k] * w));
    }
    function bgGradientFill(ctx, V, g) {
        const stops = (g.stops || []).slice().sort((p, q) => p.pos - q.pos);
        if (stops.length < 2) return stops.length ? (stops[0].color || '#000') : '#000';
        const th = (g.angle || 0) * Math.PI / 180, cs = Math.cos(th), sn = Math.sin(th);
        // point de projection min/max parmi les 4 coins du cadre (px)
        const cor = [[V.MX, V.MY], [V.MX + V.FW, V.MY], [V.MX, V.MY + V.FH], [V.MX + V.FW, V.MY + V.FH]];
        let lo = cor[0], hi = cor[0], plo = 1e9, phi = -1e9;
        cor.forEach(c => { const p = c[0] * cs + c[1] * sn; if (p < plo) { plo = p; lo = c; } if (p > phi) { phi = p; hi = c; } });
        const grad = ctx.createLinearGradient(lo[0], lo[1], hi[0], hi[1]);
        const soft = (g.softness == null) ? 1 : g.softness, N = 48;
        for (let k = 0; k <= N; k++) { const t = k / N; const c = _gradSample(t, stops, soft); grad.addColorStop(t, `rgb(${c[0]},${c[1]},${c[2]})`); }
        return grad;
    }
    function drawCanvas() {
        const cv = $('#split_canvas'); if (!cv || !boxes) return;
        const CW = cv.clientWidth || 640, CH = Math.round(CW * 9 / 16);
        cv.width = CW; cv.height = CH;
        const ctx = cv.getContext('2d'), css = getComputedStyle(cv);
        ctx.clearRect(0, 0, CW, CH);
        ctx.fillStyle = css.backgroundColor || '#111';
        ctx.fillRect(0, 0, CW, CH);
        const muted = (css.getPropertyValue('--text-muted') || '#888').trim();
        const strong = (css.getPropertyValue('--text-strong') || '#fff').trim();
        const accent = (css.getPropertyValue('--accent') || '#7aa2c8').trim();
        const V = view(CW, CH);
        // ★ LE CADRE EST UN RECTANGLE INTÉRIEUR (marge autour) : sans cette marge, on ne pourrait
        // ni tirer une box DEHORS, ni la voir revenir — donc aucune entrée/sortie de champ.
        // Le FOND est peint tel qu'il sera rendu : couleur unie choisie, ou noir, ou « source ».
        const bg = (st && st.bg) || { mode: 'none' };
        if (bg.mode === 'gradient' && bg.gradient) {
            ctx.fillStyle = bgGradientFill(ctx, V, bg.gradient);
        } else {
            ctx.fillStyle = (bg.mode === 'color') ? (bg.color || '#000') : '#000';
        }
        ctx.fillRect(V.MX, V.MY, V.FW, V.FH);
        ctx.font = '11px Inter, system-ui, sans-serif';
        const scale = V.FW / ((st && st.width) || 1280);

        zOrder().forEach(i => {
            const b = gTransform(boxes[i]), r = rectPx(i, CW, CH);
            ctx.save();
            ctx.translate(r.cx, r.cy);
            if (b.rotation) ctx.rotate(-b.rotation * Math.PI / 180);
            ctx.translate(-r.cx, -r.cy);
            ctx.globalAlpha = (b.enabled ? 1 : 0.28) * (b.opacity == null ? 1 : b.opacity / 100);
            if (b.shadow && b.shadow.enabled && b.enabled) {
                ctx.save();
                ctx.globalAlpha *= (b.shadow.opacity || 0) / 100;
                ctx.fillStyle = b.shadow.color;
                ctx.filter = b.shadow.blur ? `blur(${Math.max(1, b.shadow.blur * scale)}px)` : 'none';
                pathRect(ctx, { x0: r.x0 + b.shadow.dx * scale, y0: r.y0 + b.shadow.dy * scale, w: r.w, h: r.h }, (b.radius || 0) * scale);
                ctx.fill();
                ctx.restore();
            }
            pathRect(ctx, r, (b.radius || 0) * scale);
            ctx.fillStyle = BOX_COLORS[i % 4];
            ctx.fill();
            const bw = Math.max(b.border.w, b.border.outer + b.border.inner);
            if (bw > 0) { ctx.strokeStyle = b.border.color; ctx.lineWidth = Math.max(1, bw * scale); ctx.stroke(); }
            ctx.globalAlpha = 1;
            ctx.strokeStyle = sel.includes(i) ? accent : (b.enabled ? strong : muted);
            ctx.lineWidth = sel.includes(i) ? 2 : 1;
            pathRect(ctx, r, (b.radius || 0) * scale);
            ctx.stroke();
            ctx.fillStyle = strong;
            ctx.fillText(`${T('box', 'Box')} ${i + 1}${inputs[i] ? ' · ' + inputs[i] : ''}`, r.x0 + 4, r.y0 + 14);
            if (sel.includes(i)) { ctx.fillStyle = accent; ctx.fillRect(r.x0 + r.w - 7, r.y0 + r.h - 7, 7, 7); }
            ctx.restore();
        });
        // ★ « ON DOIT COMPRENDRE QU'AU-DELÀ, ON QUITTE LA ZONE VISIBLE » (0.12.2).
        // 1) La PORTION HORS CADRE de chaque box est HACHURÉE : elle ne sera PAS diffusée (elle
        //    est rognée à l'antenne). On ne découvre pas à l'antenne que la moitié d'une box
        //    n'existe pas — on le voit ici, tout de suite.
        const hatch = outHatch(ctx, muted);
        ctx.save();
        ctx.beginPath();                                   // tout SAUF le cadre (règle even-odd)
        ctx.rect(0, 0, CW, CH);
        ctx.rect(V.MX, V.MY, V.FW, V.FH);
        ctx.clip('evenodd');
        zOrder().forEach(i => {
            const b = gTransform(boxes[i]);
            if (!boxes[i].enabled) return;
            const r = rectPx(i, CW, CH);
            ctx.save();
            ctx.translate(r.cx, r.cy);
            if (b.rotation) ctx.rotate(-b.rotation * Math.PI / 180);
            ctx.translate(-r.cx, -r.cy);
            pathRect(ctx, r, (b.radius || 0) * scale);
            ctx.fillStyle = hatch; ctx.fill();
            ctx.restore();
        });
        // 2) Le HORS-CADRE lui-même est voilé (il reste manipulable, mais il est « dehors »).
        ctx.fillStyle = 'rgba(0,0,0,0.42)';
        ctx.fillRect(0, 0, CW, CH);
        ctx.restore();
        // 3) L'ARÊTE DU CADRE DE SORTIE est FRANCHE (2 px pleins + liseré clair) : c'est une
        //    LIMITE, pas une nuance de fond.
        ctx.save();
        ctx.lineWidth = 2;
        ctx.strokeStyle = strong;
        ctx.strokeRect(V.MX + 1, V.MY + 1, V.FW - 2, V.FH - 2);
        ctx.lineWidth = 1;
        ctx.strokeStyle = 'rgba(255,255,255,0.25)';
        ctx.strokeRect(V.MX + 2.5, V.MY + 2.5, V.FW - 5, V.FH - 5);
        ctx.restore();
        ctx.fillStyle = muted;
        const bgLbl = (bg.mode === 'gradient') ? (T('bg_grad_lbl', 'Fond : dégradé') + ((bg.gradient && (bg.gradient.softness || 0) <= 1e-6) ? ' (' + T('bg_wipe', 'wipe') + ')' : ''))
            : (bg.mode === 'color') ? (T('bg_color_lbl', 'Fond : couleur') + ' ' + (bg.color || ''))
            : (bg.mode === 'source' ? (T('bg_short', 'Fond') + ' : ' + (inputs[4] || T('bg_unwired', 'non câblé')))
                                    : T('bg_none_lbl', 'Fond : noir'));
        ctx.fillText(bgLbl, V.MX + 6, V.MY + 14);
        // Dire ce qu'est la zone hachurée — un pictogramme muet n'explique rien.
        const clipped = boxes.some((b, i) => {
            if (!b.enabled) return false;
            const r = rectPx(i, CW, CH);
            return r.x0 < V.MX - 0.5 || r.y0 < V.MY - 0.5
                || r.x0 + r.w > V.MX + V.FW + 0.5 || r.y0 + r.h > V.MY + V.FH + 0.5;
        });
        ctx.fillStyle = muted;
        ctx.fillText(clipped ? T('out_clipped', 'Zone hachurée = hors cadre : rognée à l\'antenne')
                             : T('out_zone', 'Hors cadre (non diffusé)'), V.MX + 6, V.MY + V.FH - 8);
        drawGuides(ctx, CW, CH, css);
        redrawSidebar();
    }
    // Pointeur → coordonnées du canvas. ★ On rapporte TOUJOURS à la résolution interne
    // (cv.width/height) via le facteur rect→buffer : l'aperçu est désormais plafonné en largeur
    // par la CSS, et un mapping qui supposerait « 1 px écran = 1 px canvas » se décalerait dès
    // que la taille d'affichage diffère du buffer (redimensionnement, zoom navigateur, DPI).
    function canvasPos(e) {
        const cv = $('#split_canvas'), rect = cv.getBoundingClientRect();
        const sx = cv.width / (rect.width || 1), sy = cv.height / (rect.height || 1);
        return { px: (e.clientX - rect.left) * sx, py: (e.clientY - rect.top) * sy, CW: cv.width, CH: cv.height };
    }
    // ★ HORS CADRE (0.12.0) : le canvas ne montre plus SEULEMENT le cadre — il l'entoure d'une
    // MARGE, sans laquelle on ne pourrait ni tirer une box dehors, ni la voir revenir (pas
    // d'entrée ni de sortie de champ). Le cadre est donc un rectangle INTÉRIEUR au canvas ;
    // tout le mapping écran ↔ normalisé passe par `view()`. Les BORNES, elles, viennent de
    // `/state.caps.pos` (rien en dur).
    const OUT_M = 0.18;                       // marge visible autour du cadre (× la taille du cadre)
    let _hatch = null;                        // motif de hachures « hors cadre » (créé une fois)
    function outHatch(ctx, color) {
        if (_hatch) return _hatch;
        const c = document.createElement('canvas'); c.width = c.height = 8;
        const g = c.getContext('2d');
        g.strokeStyle = color; g.globalAlpha = 0.55; g.lineWidth = 1.5;
        g.beginPath(); g.moveTo(-2, 6); g.lineTo(6, -2); g.moveTo(2, 10); g.lineTo(10, 2); g.stroke();
        _hatch = ctx.createPattern(c, 'repeat');
        return _hatch;
    }
    function view(CW, CH) {
        const FW = CW / (1 + 2 * OUT_M), FH = CH / (1 + 2 * OUT_M);
        return { FW, FH, MX: (CW - FW) / 2, MY: (CH - FH) / 2 };
    }
    const posCap = () => (caps && caps.pos) || [0, 1];       // bornes x/y publiées par le moteur

    // ═══ CANAL GLOBAL — calque de transformation du GROUPE (rendu = Global ∘ box) ═══════════
    // L'aperçu applique EXACTEMENT la composition du moteur (_apply_global) : autour du centre
    // du cadre, EN PIXELS (sinon une rotation de groupe cisaillerait la scène en 16:9), ordre
    // échelle → rotation → décalage. Le canvas dit donc la VÉRITÉ du rendu.
    const G_NEUTRAL = { dx: 0, dy: 0, scale: 1, rotation: 0, opacity: 100 };
    const gGet = () => Object.assign({}, G_NEUTRAL, (st && st.global) || {});
    const gNeutral = g => (!g.dx && !g.dy && Math.abs((g.scale == null ? 1 : g.scale) - 1) < 1e-9
                           && !g.rotation && (g.opacity == null || g.opacity >= 100));
    function gTransform(b) {
        const g = gGet();
        if (gNeutral(g)) return b;
        const W = (st && st.width) || 1280, H = (st && st.height) || 720;
        const cx = W / 2, cy = H / 2, th = g.rotation * Math.PI / 180;
        const c = Math.cos(th), sn = Math.sin(th), sc = g.scale;
        const vx = b.x * W - cx, vy = b.y * H - cy;
        const rx = (vx * c - vy * sn) * sc, ry = (vx * sn + vy * c) * sc;
        return Object.assign({}, b, {
            x: (cx + rx) / W + g.dx, y: (cy + ry) / H + g.dy,
            size: b.size * sc,
            size_x: b.size_x ? b.size_x * sc : b.size_x,
            size_y: b.size_y ? b.size_y * sc : b.size_y,
            rotation: (b.rotation || 0) + g.rotation,
            opacity: (b.opacity == null ? 100 : b.opacity) * g.opacity / 100,
        });
    }
    // Glisser une box quand le Global est actif : le geste se fait dans l'espace TRANSFORMÉ, la
    // valeur écrite est dans l'espace de la box → on ANNULE la transformation sur le déplacement
    // (sans quoi tirer vers la droite déplacerait la box en biais dès que le groupe est incliné).
    function gInvDelta(dxn, dyn) {
        const g = gGet();
        if (gNeutral(g)) return { dx: dxn, dy: dyn };
        const W = (st && st.width) || 1280, H = (st && st.height) || 720;
        const th = -g.rotation * Math.PI / 180, c = Math.cos(th), sn = Math.sin(th);
        const sc = g.scale || 1;
        const px = dxn * W, py = dyn * H;
        return { dx: ((px * c - py * sn) / sc) / W, dy: ((px * sn + py * c) / sc) / H };
    }
    function toN(px, py, CW, CH) {                            // écran → normalisé (cadre)
        const v = view(CW, CH);
        return { nx: (px - v.MX) / v.FW, ny: (py - v.MY) / v.FH };
    }
    const EDGE_PX = 7;
    const CURSOR = { move: 'move', resize: 'nwse-resize', 'crop-l': 'ew-resize', 'crop-r': 'ew-resize', 'crop-t': 'ns-resize', 'crop-b': 'ns-resize' };
    function hitTest(px, py, CW, CH) {
        for (const i of zOrder().slice().reverse()) {
            const r = rectPx(i, CW, CH), E = EDGE_PX;
            if (px < r.x0 - E || px > r.x0 + r.w + E || py < r.y0 - E || py > r.y0 + r.h + E) continue;
            if (px >= r.x0 + r.w - 10 && px <= r.x0 + r.w + 4 && py >= r.y0 + r.h - 10 && py <= r.y0 + r.h + 4) return { idx: i, mode: 'resize' };
            const onV = py >= r.y0 - E && py <= r.y0 + r.h + E, onH = px >= r.x0 - E && px <= r.x0 + r.w + E;
            if (onH && Math.abs(py - r.y0) <= E) return { idx: i, mode: 'crop-t' };
            if (onH && Math.abs(py - (r.y0 + r.h)) <= E) return { idx: i, mode: 'crop-b' };
            if (onV && Math.abs(px - r.x0) <= E) return { idx: i, mode: 'crop-l' };
            if (onV && Math.abs(px - (r.x0 + r.w)) <= E) return { idx: i, mode: 'crop-r' };
            if (px >= r.x0 && px <= r.x0 + r.w && py >= r.y0 && py <= r.y0 + r.h) return { idx: i, mode: 'move' };
        }
        return null;
    }
    function canvasDown(e) {
        const { px, py, CW, CH } = canvasPos(e);
        const h = hitTest(px, py, CW, CH);
        $('#split_canvas').focus();
        if (!h) return;
        selectBox(h.idx, e.shiftKey);
        const b = boxes[h.idx];
        // ★ MULTI-DÉPLACEMENT (0.12.0) : glisser une box SÉLECTIONNÉE entraîne TOUTE la
        // sélection, en DELTA — chaque box garde sa position RELATIVE (même logique que les
        // champs de géométrie en multi). On mémorise donc la position de départ de CHACUNE.
        // Le REDIMENSIONNEMENT, lui, s'applique à chaque box AUTOUR DE SON PROPRE CENTRE
        // (chacune grandit sur place) : c'est le geste attendu, et il ne déplace personne.
        drag = { idx: h.idx, mode: h.mode, sx: px, sy: py, bx: b.x, by: b.y, bsize: b.size,
                 group: sel.slice(), start: sel.map(i => ({ i, x: boxes[i].x, y: boxes[i].y,
                                                            size: boxes[i].size })) };
        e.target.setPointerCapture(e.pointerId);
    }
    function canvasMove(e) {
        const { px, py, CW, CH } = canvasPos(e);
        if (!drag) {
            const cv = $('#split_canvas');
            if (cv) { const h = hitTest(px, py, CW, CH); cv.style.cursor = h ? CURSOR[h.mode] : 'default'; }
            return;
        }
        const V = view(CW, CH);
        const b = boxes[drag.idx], fw = wOf(b) * V.FW, fh = hOf(b) * V.FH;
        const [pmin, pmax] = posCap();
        const [cmin, cmax] = cap('crop');
        const capL = v => clamp(v, cmin, Math.min(cmax, 0.92 - b.crop.r)), capR = v => clamp(v, cmin, Math.min(cmax, 0.92 - b.crop.l));
        const capT = v => clamp(v, cmin, Math.min(cmax, 0.92 - b.crop.b)), capB = v => clamp(v, cmin, Math.min(cmax, 0.92 - b.crop.t));
        const [smin, smax] = cap('box_fields', 'size');
        snapGuides = [];
        switch (drag.mode) {
            case 'move': {
                // Déplacement de la box TIRÉE (bornes = caps.pos : le hors-cadre est permis)…
                const dn = gInvDelta((px - drag.sx) / V.FW, (py - drag.sy) / V.FH);
                b.x = clamp(drag.bx + dn.dx, pmin, pmax);
                b.y = clamp(drag.by + dn.dy, pmin, pmax);
                // Snap DÉSACTIVÉ tant que le Global n'est pas neutre : aimanter dans un repère
                // tourné/échelonné donnerait des guides qui mentent (on le DIT dans l'UI).
                if (snapEnabled && gNeutral(gGet())) {   // aimantation aux bords/centre du cadre et des autres box
                    const r = rectPx(drag.idx, CW, CH);
                    const s = computeSnap(drag.idx, r.x0, r.y0, r.w, r.h, CW, CH);
                    b.x = clamp(b.x + (s.x - r.x0) / V.FW, pmin, pmax);
                    b.y = clamp(b.y + (s.y - r.y0) / V.FH, pmin, pmax);
                    snapGuides = s.guides;
                }
                // … puis REPORT DU MÊME DÉCALAGE sur le reste de la sélection (écarts relatifs
                // conservés). Le décalage est celui RÉELLEMENT appliqué (snap compris).
                if (drag.group.length > 1) {
                    const dx = b.x - drag.bx, dy = b.y - drag.by;
                    drag.start.forEach(s0 => {
                        if (s0.i === drag.idx) return;
                        const o = boxes[s0.i];
                        o.x = clamp(s0.x + dx, pmin, pmax);
                        o.y = clamp(s0.y + dy, pmin, pmax);
                    });
                }
                break;
            }
            case 'resize': {
                const f0 = b.size, gsc = gGet().scale || 1;
                b.size = clamp(Math.abs(px - (V.MX + gTransform(b).x * V.FW)) * 2 / V.FW / gsc, smin, smax);
                if (snapEnabled && gNeutral(gGet())) {
                    const r = rectPx(drag.idx, CW, CH);
                    const s = computeSnapResize(drag.idx, r, CW, CH);
                    if (s.guides.length) { b.size = clamp(b.size * s.factor, smin, smax); snapGuides = s.guides; }
                }
                // MULTI : même FACTEUR d'échelle pour toutes, chacune AUTOUR DE SON CENTRE (elles
                // grandissent sur place — aucune ne se déplace, la disposition est préservée).
                if (drag.group.length > 1 && f0 > 0) {
                    const k = b.size / (drag.bsize || f0);
                    drag.start.forEach(s0 => {
                        if (s0.i === drag.idx) return;
                        boxes[s0.i].size = clamp(s0.size * k, smin, smax);
                    });
                }
                break;
            }
            case 'crop-l': b.crop.l = capL((px - (V.MX + b.x * V.FW)) / fw + 0.5); break;
            case 'crop-r': b.crop.r = capR(0.5 - (px - (V.MX + b.x * V.FW)) / fw); break;
            case 'crop-t': b.crop.t = capT((py - (V.MY + b.y * V.FH)) / fh + 0.5); break;
            case 'crop-b': b.crop.b = capB(0.5 - (py - (V.MY + b.y * V.FH)) / fh); break;
        }
        drawCanvas();
        if (drag.group.length > 1 && (drag.mode === 'move' || drag.mode === 'resize')) {
            drag.group.forEach(i => throttledPostBox(i));    // toute la sélection suit, en direct
        } else {
            throttledPostBox(drag.idx);
        }
    }
    function canvasUp() {
        if (!drag) return;
        const grp = (drag.mode === 'move' || drag.mode === 'resize') ? drag.group : [drag.idx];
        drag = null; snapGuides = [];
        grp.forEach(i => {
            const b = boxes[i];
            postBox(i, { x: b.x, y: b.y, size: b.size, crop: b.crop });   // commit de CHAQUE box
        });
        syncAll();
    }

    // ─── Écritures serveur ──────────────────────────────────────────────────────────
    function throttledPostBox(i) {
        _postPend.add(i);
        if (boxPostTimer) return;
        boxPostTimer = setTimeout(() => {
            boxPostTimer = null;
            const pend = Array.from(_postPend); _postPend.clear();
            pend.forEach(j => {
                const bj = boxes[j];
                if (bj) postBox(j, { x: bj.x, y: bj.y, size: bj.size, crop: bj.crop }, true);
            });
        }, 60);
    }
    async function postBox(i, patch, skipPersist) {
        if (VMID == null) return;
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/box`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({ idx: i }, patch)),
            });
            if (!r.ok) {
                const j = await r.json().catch(() => ({}));
                showError(T('box_err', 'Réglage refusé par le moteur') + ' : ' + (j.error || ('HTTP ' + r.status)));
            } else clearError();
        } catch (e) { showError(T('box_err', 'Réglage refusé par le moteur') + ' : ' + e.message); }
        if (!skipPersist) schedulePersist();
    }
    async function postInput(idx, shm) {
        if (VMID == null) return;
        inputs[idx] = shm;
        try {
            await fetch(`/api/containers/${VMID}/plugin/input`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ idx, shm }),
            });
        } catch (e) { showError(T('input_err', 'Câblage refusé') + ' : ' + e.message); }
        drawCanvas(); schedulePersist();
    }
    function schedulePersist() {
        if (persistTimer) clearTimeout(persistTimer);
        persistTimer = setTimeout(() => {
            persistTimer = null;
            if (VMID != null) fetch(`/api/containers/${VMID}/split/persist`, { method: 'POST' }).catch(() => {});
        }, 1500);
    }

    // ─── Mémoires ───────────────────────────────────────────────────────────────────
    function drawMemPreview(canvas, bx) {
        if (!canvas) return;
        const CW = canvas.clientWidth || 150, CH = Math.round(CW * 9 / 16);
        canvas.width = CW; canvas.height = CH;
        const ctx = canvas.getContext('2d'), css = getComputedStyle(canvas);
        ctx.fillStyle = css.backgroundColor || '#111'; ctx.fillRect(0, 0, CW, CH);
        (bx || []).slice(0, 4).forEach((raw, i) => {
            const b = normBox(raw); if (!b.enabled) return;
            const r = rectN(b);
            ctx.globalAlpha = (b.opacity == null ? 1 : b.opacity);
            ctx.fillStyle = BOX_COLORS[i % 4];
            ctx.fillRect(r.x0 * CW, r.y0 * CH, r.w * CW, r.h * CH);
            ctx.globalAlpha = 1;
            ctx.strokeStyle = (css.getPropertyValue('--text-strong') || '#fff').trim();
            ctx.lineWidth = 1;
            ctx.strokeRect(r.x0 * CW, r.y0 * CH, r.w * CW, r.h * CH);
        });
    }
    function sendSelect(memId) {
        const others = containers.filter(d => d.vmid !== VMID);
        if (!others.length) return '';
        return `<select data-send="${memId}" aria-label="${esc(T('mem_send', 'Envoyer vers…'))}">
            <option value="">${esc(T('mem_send', 'Envoyer vers…'))}</option>
            ${others.map(d => `<option value="${d.vmid}">${esc(d.hostname)} (#${d.vmid})</option>`).join('')}</select>`;
    }
    // Une mémoire est soit une LISTE de box (format ≤ 0.11), soit {boxes, global, bg} (0.12.0 :
    // le canal Global et le fond en font partie — sans eux, « groupe hors champ » ou « fond bleu »
    // ne se rappelleraient pas). Les deux se lisent.
    const memBoxes = m => (Array.isArray(m.config) ? m.config : ((m.config && m.config.boxes) || []));

    async function loadMemories() {
        if (VMID == null) return;
        const ul = $('#split_mem_list'); if (!ul) return;
        try {
            const mems = await (await fetch(`/api/containers/${VMID}/split/memories`)).json();
            memCache = mems;
            if (!mems.length) { ul.innerHTML = `<li class="meta">${esc(T('mem_empty', 'Aucune mémoire.'))}</li>`; return; }
            ul.innerHTML = mems.map(m => {
                const nOn = memBoxes(m).filter(b => b && b.enabled).length;
                return `<li class="split-mem-card"><b title="${esc(m.name)}">${esc(m.name)}</b>
                    <canvas data-mem-preview="${m.id}"></canvas>
                    <span class="meta">${nOn} ${esc(T('mem_active', 'box active(s)'))}</span>
                    <div class="split-mem-actions">
                        <button type="button" class="btn btn-sm btn-blue" data-recall="${m.id}">${esc(T('mem_recall', 'Rappeler'))}</button>
                        <button type="button" class="btn btn-sm" data-export="${m.id}">${esc(T('mem_export', 'Export'))}</button>
                        <button type="button" class="btn btn-sm btn-red" data-del="${m.id}"
                                title="${esc(T('mem_delete', 'Supprimer'))}" aria-label="${esc(T('mem_delete', 'Supprimer'))}">✕</button>
                        ${sendSelect(m.id)}
                    </div></li>`;
            }).join('');
            mems.forEach(m => drawMemPreview(ul.querySelector(`canvas[data-mem-preview="${m.id}"]`), memBoxes(m)));
            ul.querySelectorAll('button[data-recall]').forEach(b => { b.onclick = () => recallMemory(parseInt(b.dataset.recall)); });
            ul.querySelectorAll('button[data-export]').forEach(b => { b.onclick = () => exporterMemoire(parseInt(b.dataset.export)); });
            ul.querySelectorAll('button[data-del]').forEach(b => { b.onclick = () => deleteMemory(parseInt(b.dataset.del)); });
            ul.querySelectorAll('select[data-send]').forEach(s => {
                s.onchange = function () { if (this.value) { envoyerMemoire(parseInt(this.dataset.send), this.value); this.value = ''; } };
            });
        } catch (e) { ul.innerHTML = ''; }
    }
    async function saveMemory() {
        const name = ($('#split_mem_name').value || '').trim();
        if (!name) { showError(T('mem_need_name', 'Donnez un nom à la mémoire.')); return; }
        const r = await fetch(`/api/containers/${VMID}/split/memories`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
        });
        if (r.ok) { $('#split_mem_name').value = ''; clearError(); loadMemories(); }
        else showError(T('mem_save_err', 'Erreur d\'enregistrement de la mémoire'));
    }
    async function recallMemory(id) {
        const dur = parseInt($('#split_recall_dur').value) || 0;
        const TR = (st && st.transition_defaults) || {};
        await fetch(`/api/containers/${VMID}/split/memories/${id}/recall`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ duration_ms: dur, curve: TR.curve, stagger_ms: TR.stagger_ms }),
        });
        setTimeout(() => fetchState({ rebuild: false }), dur + 250);
    }
    async function deleteMemory(id) { await fetch(`/api/containers/${VMID}/split/memories/${id}`, { method: 'DELETE' }); loadMemories(); }
    function exporterMemoire(id) {
        const m = memCache.find(x => x.id === id); if (!m) return;
        const blob = new Blob([JSON.stringify({ name: m.name, config: m.config }, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob), a = document.createElement('a');
        a.href = url; a.download = `split-memoire-${(m.name || 'sans-nom').replace(/[^a-zA-Z0-9_-]/g, '_')}.json`;
        a.click(); URL.revokeObjectURL(url);
    }
    async function onImportMemFile(input) {
        const file = input.files[0]; if (!file) return; input.value = '';
        let data;
        try { data = JSON.parse(await file.text()); }
        catch (e) { showError(T('mem_bad_file', 'Fichier invalide (JSON)') + ' : ' + e.message); return; }
        const okCfg = Array.isArray(data.config)
            || (data.config && Array.isArray(data.config.boxes));   // format 0.12 (boxes+global+bg)
        if (!okCfg) { showError(T('mem_bad_cfg', 'Fichier invalide : champ « config » manquant.')); return; }
        const name = (data.name || file.name.replace(/\.json$/i, '')).trim() || T('mem_imported', 'mémoire importée');
        const r = await fetch(`/api/containers/${VMID}/split/memories`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, config: data.config }),
        });
        if (r.ok) { clearError(); loadMemories(); } else showError(T('mem_import_err', 'Erreur d\'import') + ' (HTTP ' + r.status + ')');
    }
    async function envoyerMemoire(memId, targetVmid) {
        const m = memCache.find(x => x.id === parseInt(memId)); if (!m) return;
        const r = await fetch(`/api/containers/${targetVmid}/split/memories`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: m.name, config: m.config }),
        });
        const tgt = containers.find(d => d.vmid === parseInt(targetVmid));
        if (r.ok) TOAST(`${T('mem_sent', 'Mémoire copiée vers')} ${tgt ? tgt.hostname : ('#' + targetVmid)}.`, 'info');
        else showError(T('mem_send_err', 'Erreur d\'envoi') + ' (HTTP ' + r.status + ')');
    }

    // ─── Sync ───────────────────────────────────────────────────────────────────────
    function updateToolbar() {
        const set = (id, disabled, reason) => {
            const b = $('#' + id); if (!b) return;
            b.disabled = !!disabled;
            if (disabled && reason) b.title = reason;
        };
        // « Pas de contrôle muet » : un outil désactivé DIT pourquoi (title).
        const need2 = T('flash_select2', 'Sélectionnez au moins 2 box (Maj+clic).');
        const need3 = T('flash_select3', 'Sélectionnez au moins 3 box (Maj+clic).');
        ['split_size_w', 'split_size_h', 'split_size_both'].forEach(id => set(id, sel.length < 2, need2));
        ['split_dist_h', 'split_dist_v'].forEach(id => set(id, sel.length < 3, need3));
        set('split_copy', sel.length !== 1, T('copy_need1', 'Sélectionnez UNE box à copier.'));
        set('split_paste', !clipboard || !sel.length, T('paste_need_clip', 'Copiez d\'abord les réglages d\'une box.'));
        if (sel.length >= 2) { const b = $('#split_size_w'); if (b) b.title = T('size_w', 'Même largeur que la référence'); }
    }
    function syncAll() { renderStrip(); renderProps(); renderChipGlobal(); bindBg(); updateToolbar(); drawCanvas(); }

    // Le chip « Global » est un SÉLECTEUR DE CIBLE, exactement comme une box : il SÉLECTIONNE le
    // canal (le panneau de réglages bascule dessus). Il dit aussi son état — neutre ou actif.
    function renderChipGlobal() {
        const gc = $('#split_global_chip'); if (!gc) return;
        const on = !gNeutral(gGet());
        gc.classList.toggle('is-on', on);
        gc.classList.toggle('is-sel', selGlobal);
        gc.setAttribute('aria-pressed', selGlobal ? 'true' : 'false');
        const stt = $('#split_global_state');
        if (stt) stt.textContent = on ? T('global_on', 'actif') : T('global_off', 'neutre');
        gc.onclick = () => { selGlobal = true; syncAll(); };
    }
    function onResize() { if (EL && $('#split_canvas')) drawCanvas(); }

    // ─── Mini-aperçu de la sidebar Traitements ──────────────────────────────────────
    function drawSchematic(cv, rawBoxes) {
        if (!cv) return;
        const CW = cv.clientWidth || 200, CH = Math.round(CW * 9 / 16);
        cv.width = CW; cv.height = CH;
        const ctx = cv.getContext('2d'), css = getComputedStyle(cv);
        ctx.clearRect(0, 0, CW, CH);
        ctx.fillStyle = css.backgroundColor || '#111'; ctx.fillRect(0, 0, CW, CH);
        const strong = (css.getPropertyValue('--text-strong') || '#fff').trim();
        (rawBoxes || []).slice(0, 4).forEach((raw, i) => {
            const b = normBox(raw); if (!b.enabled) return;
            const r = rectN(b);
            ctx.fillStyle = BOX_COLORS[i % 4];
            ctx.fillRect(r.x0 * CW, r.y0 * CH, r.w * CW, r.h * CH);
            ctx.strokeStyle = strong; ctx.lineWidth = 1;
            ctx.strokeRect(r.x0 * CW, r.y0 * CH, r.w * CW, r.h * CH);
            ctx.fillStyle = strong; ctx.font = '9px Inter, system-ui, sans-serif';
            ctx.fillText(String(i + 1), r.x0 * CW + 3, r.y0 * CH + 10);
        });
    }
    async function listPreview(canvas, inst) {
        if (!canvas || !inst) return;
        try {
            const s = await (await fetch(`/api/containers/${inst.vmid}/plugin/state`)).json();
            if (!caps && s.caps) caps = s.caps;
            drawSchematic(canvas, s.boxes || []);
        } catch (e) {}
    }
    function redrawSidebar() {
        if (VMID == null) return;
        const cv = document.querySelector('.tp-inst-preview[data-vmid="' + VMID + '"]');
        if (cv) drawSchematic(cv, boxes || []);
    }

    // ─── Cycle de vie ───────────────────────────────────────────────────────────────
    function mount(el, vmid, ctx) {
        unmount();
        EL = el; VMID = vmid; TOAST = (ctx && ctx.toast) || (() => {});
        st = null; boxes = null; caps = null; inputs = ['', '', '', '', ''];
        sources = []; containers = []; memCache = []; sel = [0]; clipboard = null;
        adv = null; metricsErr = false; errorMsg = null; advanced = false;
        window.addEventListener('resize', onResize);
        document.addEventListener('keydown', onKeyDown);
        Promise.all([loadSources(), loadContainers()]).then(() => fetchState({ rebuild: true }));
        pollTimer = setInterval(() => fetchState({ rebuild: false }), 2000);
        metTimer = setInterval(fetchMetrics, 2000);
        fetchMetrics();
    }
    function unmount() {
        [pollTimer, metTimer].forEach(t => t && clearInterval(t));
        [boxPostTimer, persistTimer].forEach(t => t && clearTimeout(t));
        pollTimer = metTimer = boxPostTimer = persistTimer = null;
        window.removeEventListener('resize', onResize);
        document.removeEventListener('keydown', onKeyDown);
        EL = null; VMID = null; drag = null; knobDrag = null;
    }

    const exp = { mount, unmount, listPreview };
    window.__split = exp;
    return exp;
})();
