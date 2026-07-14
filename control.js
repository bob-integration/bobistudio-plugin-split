// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// Split / SuperSource — UI de contrôle embarquée (plugin), montée par le shell Traitements via
// window.MXLPlugins.split.mount(el, vmid, ctx).
//
// Refonte 0.9.1 :
//   • Conforme DESIGN.md : tokens de thème (aucune couleur en dur), switches .ios-toggle pour
//     TOUT booléen, i18n (window.t, clés plugin.split.*), typographie/espacements du système.
//   • Barre d'outils TRANSPOSÉE de l'éditeur de layout multiview / PiP (static/pip_editor.js) :
//     mêmes groupes, MÊMES PICTOS SVG (aligner ×6, distribuer ×2, copier/coller les réglages),
//     déplacement clavier pixel par pixel (flèches = 1 px de la sortie, Maj = 10 px).
//   • Pilote tout le moteur 0.9.0 : mode avancé par box (rotation, opacité, feather, coins,
//     miroir, ratio libre, z libre), bordure ATEM (intérieur/extérieur/biseau) + source de
//     lumière, ombre portée, couche ART, transitions à courbes + stagger.
//   • ★ RIEN N'EST CODÉ EN DUR : bornes, défauts, courbes et modes de clé sont lus dans
//     /state.caps. Le budget de trame (:8080 adv) est affiché en permanence et la rotation
//     annonce son coût AVANT qu'on la touche (pas de contrôle muet, pas de décrochage muet).
'use strict';
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins.split = (function () {
    let EL = null, VMID = null, TOAST = () => {};
    let st = null, caps = null, boxes = null, inputs = ['', '', '', '', ''];
    let sel = [], clipboard = null, tab = 'geom';
    let sources = [], containers = [], memCache = [], adv = null, metricsErr = false;
    let drag = null, boxPostTimer = null, persistTimer = null, pollTimer = null, metTimer = null;

    // Couleurs de repérage des box (donnée, pas décoration : identifient la box dans l'aperçu).
    const BOX_COLORS = ['#c0504d', '#4f81bd', '#9bbb59', '#8064a2'];

    const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const $ = sel_ => (EL ? EL.querySelector(sel_) : null);
    const T = (k, fb) => { const v = window.t ? window.t('plugin.split.' + k) : ''; return (v && v !== 'plugin.split.' + k) ? v : fb; };
    const clamp = (v, lo, hi) => { v = parseFloat(v); if (isNaN(v)) return lo; return Math.max(lo, Math.min(hi, v)); };
    const r3 = v => Math.round(v * 1000) / 1000;

    // ─── Domaine des valeurs : LU dans /state.caps, jamais codé en dur ──────────────
    // cap(groupe, clé) → [min, max, défaut]. Repli neutre si le moteur est plus vieux que l'UI.
    function cap(group, key) {
        const c = caps || {};
        if (group === 'crop') return c.crop || [0, 0.9, 0];
        if (group === 'stagger') return c.stagger_ms || [0, 2000, 0];
        const g = c[group] || {};
        return g[key] || [0, 1, 0];
    }
    function stepOf(min, max) {
        const range = Math.abs(max - min);
        if (range <= 2.5) return 0.01;
        if (range <= 20) return 0.1;
        return 1;
    }
    const easings = () => (caps && caps.easings) || ['linear', 'ease_in_out'];
    const artKeys = () => (caps && caps.art_keys) || ['alpha', 'luma', 'chroma', 'none'];

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
            const s_ = (s.shm === selected) ? ' selected' : ''; if (s_) found = true;
            h += `<option value="${esc(s.shm)}"${s_}>${esc(s.label)}</option>`;
        }
        if (selected && !found) h += `<option value="${esc(selected)}" selected>${esc(selected)} (?)</option>`;
        return h;
    }

    // ─── Modèle ─────────────────────────────────────────────────────────────────────
    function normBox(b) {
        b = b || {};
        const crop = b.crop || {}, bd = b.border || {}, sh = b.shadow || {};
        const cv = (g, k, v) => { const [lo, hi, dflt] = cap(g, k); return clamp(v == null ? dflt : v, lo, hi); };
        const cc = v => { const [lo, hi, dflt] = cap('crop'); return clamp(v == null ? dflt : v, lo, hi); };
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
    // Rectangle VISIBLE (crop appliqué) en coordonnées normalisées 0..1.
    function rectN(b) {
        const fw = wOf(b), fh = hOf(b);
        const visW = fw * (1 - b.crop.l - b.crop.r), visH = fh * (1 - b.crop.t - b.crop.b);
        const cx = b.x + (b.crop.l - b.crop.r) * 0.5 * fw, cy = b.y + (b.crop.t - b.crop.b) * 0.5 * fh;
        return { x0: cx - visW / 2, y0: cy - visH / 2, w: visW, h: visH, cx, cy };
    }
    // Repose le rectangle visible en x0/y0 → recalcule le centre b.x/b.y (le crop décale le centre).
    function setRectPos(b, x0, y0) {
        const r = rectN(b);
        if (x0 != null) b.x = r3(clamp(b.x + (x0 - r.x0), 0, 1));
        if (y0 != null) b.y = r3(clamp(b.y + (y0 - r.y0), 0, 1));
    }
    const primary = () => (sel.length ? sel[0] : -1);

    // ─── Fetch état / métriques ─────────────────────────────────────────────────────
    function editing() {
        if (drag) return true;
        const ae = document.activeElement;
        return !!(ae && EL && EL.contains(ae) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(ae.tagName));
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
            } else if (!editing()) {
                boxes = (s.boxes || []).map(normBox);
                inputs = (s.inputs || []).slice(0, 5);
                syncAll();
            }
        } catch (e) {
            if (EL) EL.innerHTML = `<div class="split-error">${esc(T('err_state', 'État du Split injoignable'))} : ${esc(e.message)}</div>`;
        }
    }
    // Métriques :8080 (bloc `adv` du moteur 0.9.0) : budget de trame, boxes tournées, cache.
    async function fetchMetrics() {
        if (VMID == null || !EL) return;
        try {
            const r = await fetch(`/api/containers/${VMID}/split/metrics`);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const m = await r.json();
            adv = m.adv || null; metricsErr = false;
        } catch (e) { adv = null; metricsErr = true; }
        updateBudget();
    }

    // ─── Pictos : IDENTIQUES au composer multiview / éditeur de PiP (static/pip_editor.js) ──
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
    // Référence : la box PRIMAIRE (1ʳᵉ sélectionnée) si ≥2 sélectionnées, sinon le cadre entier.
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
    // Même taille que la référence (box primaire). Ratio libre → size_x/size_y suivent aussi.
    function matchSize(mode) {
        if (!boxes || sel.length < 2) {
            TOAST(T('flash_select2', 'Sélectionnez au moins 2 box (Maj+clic).'), 'warning'); return;
        }
        const ref = boxes[primary()]; if (!ref) return;
        sel.forEach(i => {
            if (i === primary()) return;
            const b = boxes[i]; if (!b) return;
            const patch = {};
            if (mode === 'both' || mode === 'w' || mode === 'h') { b.size = ref.size; patch.size = ref.size; }
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
        if (!boxes || sel.length < 3) {
            TOAST(T('flash_select3', 'Sélectionnez au moins 3 box (Maj+clic).'), 'warning'); return;
        }
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
    // Copier/coller LES RÉGLAGES d'une box (hors position) — même contrat que le PiP.
    const CLIP_KEYS = ['size', 'size_x', 'size_y', 'aspect', 'rotation', 'opacity', 'feather',
        'radius', 'mirror_h', 'mirror_v'];
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

    // ─── Déplacement clavier PIXEL PAR PIXEL (flèches = 1 px sortie, Maj = 10 px) ────
    const NUDGE = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    function onKeyDown(e) {
        if (!EL || !boxes || !sel.length) return;
        const dir = NUDGE[e.key]; if (!dir) return;
        const ae = document.activeElement;
        if (ae && /INPUT|SELECT|TEXTAREA/.test(ae.tagName)) return;   // on édite un champ : laisser faire
        if (!EL.contains(ae) && ae !== document.body) return;
        e.preventDefault();
        const W = (st && st.width) || 1920, H = (st && st.height) || 1080;
        const mult = e.shiftKey ? 10 : 1;
        let dx = (dir[0] * mult) / W, dy = (dir[1] * mult) / H;
        sel.forEach(i => {                       // borne collective : la sélection reste dans le cadre
            const b = boxes[i];
            dx = Math.max(-b.x, Math.min(1 - b.x, dx));
            dy = Math.max(-b.y, Math.min(1 - b.y, dy));
        });
        if (!dx && !dy) return;
        sel.forEach(i => {
            const b = boxes[i];
            b.x = r3(b.x + dx); b.y = r3(b.y + dy);
            postBox(i, { x: b.x, y: b.y });
        });
        syncAll();
    }

    // ─── Shell ──────────────────────────────────────────────────────────────────────
    function renderShell() {
        EL.innerHTML = `
<div class="split-ui">
  <div class="split-toolbar" id="split_toolbar"></div>
  <div class="split-cols">
    <div class="split-stage">
      <div class="split-canvas-wrap"><canvas id="split_canvas" tabindex="0"
           aria-label="${esc(T('canvas_aria', 'Composition des box'))}"></canvas></div>
      <div class="split-hint">${T('hint', 'Cliquer une box pour la sélectionner · <kbd>Maj</kbd>+clic = multi-sélection · glisser = déplacer · coin bas-droit = taille · arêtes = crop · <kbd>←↑→↓</kbd> = 1 px, <kbd>Maj</kbd> = 10 px')}</div>
      <div class="split-strip" id="split_strip"></div>
      <div class="split-field" style="max-width:340px">
        <label for="split_bg">${esc(T('bg', 'Fond (plein cadre)'))}</label>
        <select id="split_bg">${srcOptions(inputs[4])}</select>
      </div>
    </div>
    <aside class="split-props" id="split_props"></aside>
  </div>
  <div class="split-sections">
    <div class="split-card" id="split_art"></div>
    <div class="split-card" id="split_look"></div>
    <div class="split-card split-memories">
      <h3>${esc(T('memories', 'Mémoires'))}</h3>
      <ul class="split-mem-list" id="split_mem_list"></ul>
      <div class="split-mem-bar">
        <div class="split-field"><label for="split_mem_name">${esc(T('mem_name', 'Nom'))}</label>
          <input type="text" id="split_mem_name" placeholder="${esc(T('mem_name_ph', 'Nom de la mémoire'))}"></div>
        <div class="split-field"><label for="split_recall_dur">${esc(T('mem_duration', 'Transition (ms)'))}</label>
          <input type="number" id="split_recall_dur" min="0" max="10000" step="100" value="1000"></div>
        <button type="button" class="btn btn-green" id="split_mem_save">${esc(T('mem_save', 'Enregistrer la disposition'))}</button>
        <button type="button" class="btn" id="split_mem_import">${esc(T('mem_import', 'Importer un fichier'))}</button>
        <input type="file" id="split_mem_file" accept="application/json,.json" hidden>
      </div>
      <p class="split-note">${esc(T('mem_note', 'Le rappel utilise la courbe et le décalage réglés ci-dessus (Lumière & transitions).'))}</p>
    </div>
  </div>
</div>`;

        // Barre d'outils : mêmes groupes / mêmes pictos que le composer multiview.
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

        renderArt(); renderLook(); syncAll(); updateBudget();
    }

    // ─── Bande des box (activation + source) ────────────────────────────────────────
    function renderStrip() {
        const strip = $('#split_strip'); if (!strip) return;
        strip.innerHTML = boxes.map((b, i) => `
<div class="split-chip${sel.includes(i) ? ' sel' : ''}" data-box="${i}">
  <div class="split-chip-head">
    <span class="split-chip-dot" style="background:${BOX_COLORS[i % 4]}"></span>
    <span class="split-chip-name">${esc(T('box', 'Box'))} ${i + 1}</span>
    <input type="checkbox" class="ios-toggle" data-en="${i}" ${b.enabled ? 'checked' : ''}
           title="${esc(T('box_enable', 'Afficher cette box'))}"
           aria-label="${esc(T('box_enable', 'Afficher cette box'))}">
  </div>
  <select data-src="${i}">${srcOptions(inputs[i])}</select>
</div>`).join('');
        strip.querySelectorAll('.split-chip').forEach(chip => {
            chip.addEventListener('click', e => {
                if (e.target.closest('select') || e.target.closest('input')) return;
                selectBox(parseInt(chip.dataset.box), e.shiftKey);
            });
        });
        strip.querySelectorAll('input[data-en]').forEach(cb => {
            cb.onchange = function () { const i = parseInt(this.dataset.en); boxes[i].enabled = this.checked; postBox(i, { enabled: this.checked }); drawCanvas(); };
        });
        strip.querySelectorAll('select[data-src]').forEach(s => {
            s.onchange = function () { postInput(parseInt(this.dataset.src), this.value); };
        });
    }
    function selectBox(i, additive) {
        if (additive) {
            if (sel.includes(i)) sel = sel.filter(x => x !== i);
            else sel = sel.concat([i]);
            if (!sel.length) sel = [i];
        } else sel = [i];
        syncAll();
    }

    // ─── Panneau de propriétés (bornes LUES dans caps) ──────────────────────────────
    function numField(id, label, group, key, path, opts) {
        const [min, max, dflt] = cap(group, key);
        const step = (opts && opts.step) || stepOf(min, max);
        const b = boxes[primary()];
        let val = dflt;
        if (b) val = (path ? (b[path] || {})[key] : b[key]);
        if (val == null) val = (opts && opts.nullValue != null) ? opts.nullValue : dflt;
        const dec = step >= 1 ? 0 : (step >= 0.1 ? 1 : 2);
        return `<div class="split-field">
  <label for="${id}" title="${esc((opts && opts.title) || '')}">${esc(label)}</label>
  <input type="number" id="${id}" data-key="${key}" data-path="${path || ''}"
         min="${min}" max="${max}" step="${step}" value="${Number(val).toFixed(dec)}">
</div>`;
    }
    function colorField(id, label, path, key) {
        const b = boxes[primary()];
        const val = (b && (path ? (b[path] || {})[key] : b[key])) || '#ffffff';
        return `<div class="split-field"><label for="${id}">${esc(label)}</label>
  <input type="color" id="${id}" data-key="${key}" data-path="${path || ''}" value="${esc(val)}"></div>`;
    }
    function toggleField(key, path, label) {
        const b = boxes[primary()];
        const on = !!(b && (path ? (b[path] || {})[key] : b[key]));
        return `<label class="toggle-inline">
  <input type="checkbox" class="ios-toggle" data-flag="${key}" data-path="${path || ''}" ${on ? 'checked' : ''}>
  <span>${esc(label)}</span></label>`;
    }

    // Coût de la rotation : ANNONCÉ AVANT d'y toucher (mesures du moteur 0.9.0, help.md).
    // 1080p50 → hors budget ; 1080p25 → ~80 % du budget ; pendant une transition → hors budget.
    function rotationWarning() {
        const h = (st && st.height) || 0, fps = (st && st.fps) || 25;
        if (h >= 1080 && fps >= 50)
            return { cls: 'err', msg: T('rot_over', 'Rotation HORS BUDGET dans ce format (1080p50) : ~31,9 ms/trame pour un budget de 20 ms — le moteur décrochera (fps sous la consigne). Réservez-la au 720p, ou baissez la cadence.') };
        if (h >= 1080)
            return { cls: '', msg: T('rot_tight', 'Rotation MARGINALE en 1080p25 (~31,9 ms pour 40 ms de budget, soit 80 %) : aucune marge, et hors budget pendant une transition.') };
        return { cls: '', msg: T('rot_ok', 'Rotation confortable dans ce format (~11,4 ms/trame en 720p).') };
    }

    function renderProps() {
        const host = $('#split_props'); if (!host) return;
        const i = primary();
        if (i < 0 || !boxes[i]) { host.innerHTML = `<p class="split-empty">${esc(T('no_sel', 'Aucune box sélectionnée.'))}</p>`; return; }
        const b = boxes[i];
        const rw = rotationWarning();
        const multi = sel.length > 1
            ? `<p class="split-note">${esc(T('multi_note', 'Multi-sélection : les champs modifient la box de référence ; les outils (aligner, distribuer, coller) agissent sur toute la sélection.'))}</p>` : '';
        host.innerHTML = `
<h3><span class="split-chip-dot" style="display:inline-block;background:${BOX_COLORS[i % 4]}"></span>
    ${esc(T('box', 'Box'))} ${i + 1}${sel.length > 1 ? ` <span class="split-note">(+${sel.length - 1})</span>` : ''}</h3>
${multi}
<div class="tabs" role="tablist">
  <button type="button" class="tab${tab === 'geom' ? ' active' : ''}" data-tab="geom" role="tab">${esc(T('tab_geom', 'Géométrie'))}</button>
  <button type="button" class="tab${tab === 'fx' ? ' active' : ''}" data-tab="fx" role="tab">${esc(T('tab_fx', 'Effets'))}</button>
  <button type="button" class="tab${tab === 'border' ? ' active' : ''}" data-tab="border" role="tab">${esc(T('tab_border', 'Bordure'))}</button>
  <button type="button" class="tab${tab === 'shadow' ? ' active' : ''}" data-tab="shadow" role="tab">${esc(T('tab_shadow', 'Ombre'))}</button>
</div>

<div class="split-pane" data-pane="geom" ${tab === 'geom' ? '' : 'hidden'}>
  <div class="split-grid">
    ${numField('sp_x', T('f_x', 'X (centre)'), 'box_fields', 'x', '')}
    ${numField('sp_y', T('f_y', 'Y (centre)'), 'box_fields', 'y', '')}
    ${numField('sp_size', T('f_size', 'Taille'), 'box_fields', 'size', '')}
    <div class="split-field"><label for="sp_aspect">${esc(T('f_aspect', 'Ratio'))}</label>
      <select id="sp_aspect" data-key="aspect" data-path="">
        <option value="keep"${b.aspect === 'keep' ? ' selected' : ''}>${esc(T('aspect_keep', 'Conservé (ATEM)'))}</option>
        <option value="free"${b.aspect === 'free' ? ' selected' : ''}>${esc(T('aspect_free', 'Libre (étirement)'))}</option>
      </select></div>
    ${numField('sp_size_x', T('f_size_x', 'Largeur (0 = taille)'), 'box_fields', 'size_x', '')}
    ${numField('sp_size_y', T('f_size_y', 'Hauteur (0 = taille)'), 'box_fields', 'size_y', '')}
    ${numField('sp_z', T('f_z', 'Z (ordre)'), 'box_fields', 'z', '', { nullValue: i, step: 1 })}
    ${numField('sp_rot', T('f_rotation', 'Rotation (°)'), 'box_fields', 'rotation', '', { step: 1 })}
  </div>
  <div class="split-warn${rw.cls ? ' ' + rw.cls : ''}" id="split_rot_warn">${esc(rw.msg)}</div>
  <div class="split-grid">
    ${numField('sp_ct', T('f_crop_t', 'Crop haut'), 'crop', 't', 'crop')}
    ${numField('sp_cb', T('f_crop_b', 'Crop bas'), 'crop', 'b', 'crop')}
    ${numField('sp_cl', T('f_crop_l', 'Crop gauche'), 'crop', 'l', 'crop')}
    ${numField('sp_cr', T('f_crop_r', 'Crop droite'), 'crop', 'r', 'crop')}
  </div>
</div>

<div class="split-pane" data-pane="fx" ${tab === 'fx' ? '' : 'hidden'}>
  <div class="split-grid">
    ${numField('sp_op', T('f_opacity', 'Opacité'), 'box_fields', 'opacity', '')}
    ${numField('sp_fe', T('f_feather', 'Adoucissement (px)'), 'box_fields', 'feather', '')}
    ${numField('sp_ra', T('f_radius', 'Coins arrondis (px)'), 'box_fields', 'radius', '')}
  </div>
  <div class="split-toggles">
    ${toggleField('mirror_h', '', T('f_mirror_h', 'Miroir horizontal'))}
    ${toggleField('mirror_v', '', T('f_mirror_v', 'Miroir vertical'))}
  </div>
  <p class="split-note">${esc(T('fx_note', 'Ces effets tiennent le budget de trame (mesuré 1080p : feather 8,0 ms · coins 8,9 · opacité 12,8 pour 4 box). Seule la rotation est hors budget en 1080p50.'))}</p>
</div>

<div class="split-pane" data-pane="border" ${tab === 'border' ? '' : 'hidden'}>
  <div class="split-grid">
    ${numField('sp_bw', T('f_border_w', 'Trait plat (px)'), 'border_fields', 'w', 'border', { step: 1 })}
    ${colorField('sp_bc', T('f_border_color', 'Couleur'), 'border', 'color')}
    ${numField('sp_bo', T('f_border_outer', 'Extérieur (px)'), 'border_fields', 'outer', 'border')}
    ${numField('sp_bi', T('f_border_inner', 'Intérieur (px)'), 'border_fields', 'inner', 'border')}
    ${numField('sp_bv', T('f_bevel', 'Biseau (−rentrant/+sortant)'), 'border_fields', 'bevel', 'border')}
    ${numField('sp_bs', T('f_bevel_soft', 'Adoucissement du biseau'), 'border_fields', 'bevel_soft', 'border')}
    ${numField('sp_bp', T('f_bevel_pos', 'Position du biseau'), 'border_fields', 'bevel_pos', 'border')}
  </div>
  <p class="split-note">${esc(T('border_note', 'Le relief du biseau vient de la SOURCE DE LUMIÈRE (globale, ci-dessous) : sans elle, un biseau n\'est qu\'un dégradé.'))}</p>
</div>

<div class="split-pane" data-pane="shadow" ${tab === 'shadow' ? '' : 'hidden'}>
  <div class="split-toggles">${toggleField('enabled', 'shadow', T('f_shadow_on', 'Ombre portée'))}</div>
  <div class="split-grid">
    ${numField('sp_sdx', T('f_shadow_dx', 'Décalage X (px)'), 'shadow_fields', 'dx', 'shadow', { step: 1 })}
    ${numField('sp_sdy', T('f_shadow_dy', 'Décalage Y (px)'), 'shadow_fields', 'dy', 'shadow', { step: 1 })}
    ${numField('sp_sbl', T('f_shadow_blur', 'Flou (px)'), 'shadow_fields', 'blur', 'shadow')}
    ${numField('sp_sop', T('f_shadow_op', 'Opacité'), 'shadow_fields', 'opacity', 'shadow')}
    ${colorField('sp_scol', T('f_shadow_color', 'Couleur'), 'shadow', 'color')}
  </div>
</div>`;

        host.querySelectorAll('.tab[data-tab]').forEach(t => {
            t.onclick = () => { tab = t.dataset.tab; renderProps(); };
        });
        host.querySelectorAll('input[type=number], input[type=color], select[data-key]').forEach(inp => {
            inp.onchange = () => onFieldChange(inp);
        });
        host.querySelectorAll('input[data-flag]').forEach(cb => {
            cb.onchange = () => {
                const i2 = primary(), path = cb.dataset.path, key = cb.dataset.flag;
                if (path) { boxes[i2][path][key] = cb.checked; postBox(i2, { [path]: { [key]: cb.checked } }); }
                else { boxes[i2][key] = cb.checked; postBox(i2, { [key]: cb.checked }); }
                drawCanvas();
            };
        });
        syncFieldsDisabled();
    }
    function onFieldChange(inp) {
        const i = primary(); if (i < 0) return;
        const key = inp.dataset.key, path = inp.dataset.path;
        let val = (inp.type === 'number') ? parseFloat(inp.value) : inp.value;
        if (inp.type === 'number' && isNaN(val)) return;
        const b = boxes[i];
        if (path) { b[path][key] = val; }
        else b[key] = val;
        Object.assign(b, normBox(b));                          // re-borne (bornes = caps)
        const patch = path ? { [path]: { [key]: (path ? b[path][key] : b[key]) } } : { [key]: b[key] };
        postBox(i, patch);
        renderProps(); drawCanvas();
    }
    // Ratio conservé → largeur/hauteur libres sans effet : on les DÉSACTIVE en le DISANT (title).
    function syncFieldsDisabled() {
        const b = boxes[primary()]; if (!b) return;
        const free = (b.aspect === 'free');
        ['sp_size_x', 'sp_size_y'].forEach(id => {
            const el = $('#' + id); if (!el) return;
            el.disabled = !free;
            el.title = free ? '' : T('free_only', 'Actif seulement en ratio « Libre » — l\'ATEM (ratio conservé) impose une taille unique.');
            const lab = el.parentNode.querySelector('label'); if (lab) lab.title = el.title;
        });
        const sh = b.shadow || {};
        ['sp_sdx', 'sp_sdy', 'sp_sbl', 'sp_sop', 'sp_scol'].forEach(id => {
            const el = $('#' + id); if (!el) return;
            el.disabled = !sh.enabled;
            el.title = sh.enabled ? '' : T('shadow_off', 'Activez l\'ombre portée pour régler ces valeurs.');
        });
    }

    // ─── Habillage (couche ART) ─────────────────────────────────────────────────────
    function renderArt() {
        const host = $('#split_art'); if (!host || !st) return;
        const a = st.art || {};
        const keyOpts = artKeys().map(k => `<option value="${esc(k)}"${a.key === k ? ' selected' : ''}>${esc(T('artkey_' + k, k))}</option>`).join('');
        host.innerHTML = `
<h3>${esc(T('art', 'Habillage (couche ART)'))}</h3>
<div class="split-toggles">
  <label class="toggle-inline"><input type="checkbox" class="ios-toggle" id="art_en" ${a.enabled ? 'checked' : ''}>
    <span>${esc(T('art_enabled', 'Activer l\'habillage'))}</span></label>
  <label class="toggle-inline"><input type="checkbox" class="ios-toggle" id="art_above" ${a.above ? 'checked' : ''}>
    <span>${esc(T('art_above', 'Au-dessus des box'))}</span></label>
  <label class="toggle-inline"><input type="checkbox" class="ios-toggle" id="art_inv" ${a.invert ? 'checked' : ''}>
    <span>${esc(T('art_invert', 'Inverser la clé'))}</span></label>
</div>
<div class="split-grid">
  <div class="split-field"><label for="art_key">${esc(T('art_key', 'Incrustation'))}</label>
    <select id="art_key">${keyOpts}</select></div>
  <div class="split-field"><label for="art_chroma">${esc(T('art_chroma', 'Couleur de clé (chroma)'))}</label>
    <input type="color" id="art_chroma" value="${esc(a.chroma_key || '#00b140')}"></div>
  <div class="split-field"><label for="art_clip">${esc(T('art_clip', 'Seuil (clip)'))}</label>
    <input type="number" id="art_clip" min="0" max="1" step="0.01" value="${Number(a.clip || 0).toFixed(2)}"></div>
  <div class="split-field"><label for="art_gain">${esc(T('art_gain', 'Gain'))}</label>
    <input type="number" id="art_gain" min="0.01" max="16" step="0.1" value="${Number(a.gain == null ? 1 : a.gain).toFixed(2)}"></div>
  <div class="split-field"><label for="art_op">${esc(T('art_opacity', 'Opacité'))}</label>
    <input type="number" id="art_op" min="0" max="1" step="0.01" value="${Number(a.opacity == null ? 1 : a.opacity).toFixed(2)}"></div>
</div>
<div class="split-mem-bar">
  <button type="button" class="btn" id="art_pick">${esc(T('art_import', 'Importer une image…'))}</button>
  <input type="file" id="art_file" accept="image/png,image/jpeg" hidden>
  <span class="split-note" id="art_state"></span>
</div>
${a.err ? `<div class="split-warn err">${esc(a.err)}</div>` : ''}
<p class="split-note">${esc(T('art_note', 'Image importée (PNG/JPEG), pas une 6ᵉ entrée vidéo : un ART vidéo live coûterait 21,7 ms/trame en 1080p (108 % du budget 50 fps). Un PNG à fond transparent + clé « alpha » est le cas le plus propre. L\'image n\'est pas persistée : elle est à réimporter après un redéploiement du conteneur.'))}</p>`;
        const noImg = !a.has_image;
        $('#art_state').textContent = noImg ? T('art_none', 'Aucune image importée.')
            : (a.loaded ? T('art_loaded', 'Image chargée et clé précalculée.') : T('art_pending', 'Image importée — activez l\'habillage pour la cuire.'));
        $('#art_en').onchange = function () { postArt({ enabled: this.checked }); };
        $('#art_above').onchange = function () { postArt({ above: this.checked }); };
        $('#art_inv').onchange = function () { postArt({ invert: this.checked }); };
        $('#art_key').onchange = function () { postArt({ key: this.value }); };
        $('#art_chroma').onchange = function () { postArt({ chroma_key: this.value }); };
        $('#art_clip').onchange = function () { postArt({ clip: parseFloat(this.value) }); };
        $('#art_gain').onchange = function () { postArt({ gain: parseFloat(this.value) }); };
        $('#art_op').onchange = function () { postArt({ opacity: parseFloat(this.value) }); };
        $('#art_pick').onclick = () => $('#art_file').click();
        $('#art_file').onchange = function () { onArtFile(this); };
        // Les réglages de clé n'ont de sens qu'avec une image : on le DIT (pas de contrôle muet).
        ['art_key', 'art_chroma', 'art_clip', 'art_gain', 'art_op', 'art_inv', 'art_en', 'art_above'].forEach(id => {
            const el = $('#' + id); if (!el || !noImg) return;
            el.disabled = true;
            el.title = T('art_need_img', 'Importez d\'abord une image d\'habillage.');
        });
    }
    async function onArtFile(input) {
        const f = input.files[0]; if (!f) return; input.value = '';
        if (f.size > 12 * 1024 * 1024) { TOAST(T('art_too_big', 'Image trop lourde (max 12 Mo).'), 'error'); return; }
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
            if (!r.ok || j.error) TOAST((T('art_err', 'Habillage refusé') + ' : ' + (j.error || ('HTTP ' + r.status))), 'error');
        } catch (e) { TOAST(T('art_err', 'Habillage refusé') + ' : ' + e.message, 'error'); }
        await fetchState({ rebuild: false });
        renderArt();
    }

    // ─── Lumière & transitions ──────────────────────────────────────────────────────
    function renderLook() {
        const host = $('#split_look'); if (!host || !st) return;
        const L = st.light || {};
        // Un moteur antérieur au contrat 0.9.0 final ne publie pas `transition_defaults` : on
        // retombe sur les défauts du manifeste plutôt que d'afficher une courbe FAUSSE en silence.
        const stale = !st.transition_defaults;
        const TR = st.transition_defaults || { curve: 'ease_in_out', stagger_ms: 0 };
        const [amin, amax] = cap('light_fields', 'angle');
        const [hmin, hmax] = cap('light_fields', 'altitude');
        const [smin, smax] = cap('stagger');
        host.innerHTML = `
<h3>${esc(T('look', 'Lumière & transitions'))}</h3>
<div class="split-grid">
  <div class="split-field"><label for="lt_angle">${esc(T('l_angle', 'Direction de la lumière (°)'))}</label>
    <input type="number" id="lt_angle" min="${amin}" max="${amax}" step="1" value="${Number(L.angle || 0).toFixed(0)}"></div>
  <div class="split-field"><label for="lt_alt">${esc(T('l_altitude', 'Hauteur de la lumière (°)'))}</label>
    <input type="number" id="lt_alt" min="${hmin}" max="${hmax}" step="1" value="${Number(L.altitude || 0).toFixed(0)}"></div>
  <div class="split-field"><label for="lt_curve">${esc(T('t_curve', 'Courbe de transition'))}</label>
    <select id="lt_curve">${easings().map(c => `<option value="${esc(c)}"${TR.curve === c ? ' selected' : ''}>${esc(T('easing_' + c, c))}</option>`).join('')}</select></div>
  <div class="split-field"><label for="lt_stag">${esc(T('t_stagger', 'Décalage inter-box (ms)'))}</label>
    <input type="number" id="lt_stag" min="${smin}" max="${smax}" step="10" value="${Number(TR.stagger_ms || 0).toFixed(0)}"></div>
</div>
<p class="split-note">${esc(T('look_note', 'La lumière donne le relief des bordures biseautées (une seule lumière pour toute la scène, comme l\'ATEM). Le décalage fait arriver les box l\'une après l\'autre — il est compté DANS la durée demandée.'))}</p>
${stale ? `<div class="split-warn">${esc(T('stale_engine', 'Le moteur déployé sur ce conteneur ne publie pas ses réglages de transition (script antérieur au contrat 0.9.0) : les valeurs ci-dessus sont les défauts du manifeste, pas l\'état réel. Redéployez le conteneur pour lever l\'ambiguïté.'))}</div>` : ''}`;
        $('#lt_angle').onchange = function () { postLight({ angle: parseFloat(this.value) }); };
        $('#lt_alt').onchange = function () { postLight({ altitude: parseFloat(this.value) }); };
        $('#lt_curve').onchange = function () { postTransition({ curve: this.value }); };
        $('#lt_stag').onchange = function () { postTransition({ stagger_ms: parseFloat(this.value) }); };
    }
    async function postLight(patch) {
        if (VMID == null) return;
        try { await fetch(`/api/containers/${VMID}/plugin/light`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) }); } catch (e) {}
        schedulePersist();
    }
    async function postTransition(patch) {
        if (VMID == null) return;
        const body = Object.assign({ curve: (st.transition_defaults || {}).curve }, patch);
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/transition`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const j = await r.json().catch(() => ({}));
            if (r.ok && j.transition) st.transition_defaults = j.transition;
            else if (!r.ok) TOAST(T('trans_err', 'Réglage de transition refusé') + ' : ' + (j.error || ('HTTP ' + r.status)), 'error');
        } catch (e) {}
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
        const rotTxt = rot ? ` · ${T('budget_rot', 'rotation')} ×${rot}` : '';
        host.innerHTML = `<span>${esc(T('budget', 'Budget de trame'))}</span>` +
            `<span class="split-budget-val">${pct == null ? '—' : pct + ' %'}${esc(rotTxt)}</span>`;
        host.title = over
            ? T('budget_over_t', 'Le compositing dépasse le budget de trame : le Split décroche (fps sous la consigne). La rotation est de loin le poste le plus lourd — la retirer est le premier geste.')
            : (warn ? T('budget_warn_t', 'Le compositing approche du budget de trame — plus de marge pour un effet supplémentaire.')
                : T('budget_ok_t', 'Coût de composition par trame, rapporté au budget de la cadence (100 % = décrochage).'));
        const w = $('#split_rot_warn');
        if (w && over && rot) {
            w.className = 'split-warn err';
            w.textContent = T('rot_live_over', 'Le moteur dépasse actuellement le budget de trame et ') + rot + T('rot_live_over2', ' box est/sont en rotation : remettez la rotation à 0 pour retrouver la cadence.');
        }
    }

    // ─── Canvas ─────────────────────────────────────────────────────────────────────
    function zOrder() {
        return boxes.map((b, i) => ({ i, z: (b.z == null ? i : b.z) }))
            .sort((a, b_) => (a.z - b_.z) || (a.i - b_.i)).map(o => o.i);
    }
    function rectPx(i, CW, CH) {
        const r = rectN(boxes[i]);
        return { x0: r.x0 * CW, y0: r.y0 * CH, w: r.w * CW, h: r.h * CH, cx: r.cx * CW, cy: r.cy * CH };
    }
    function pathRect(ctx, r, radPx) {
        const rad = Math.max(0, Math.min(radPx, Math.min(r.w, r.h) / 2));
        if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(r.x0, r.y0, r.w, r.h, rad); }
        else { ctx.beginPath(); ctx.rect(r.x0, r.y0, r.w, r.h); }
    }
    function drawCanvas() {
        const cv = $('#split_canvas'); if (!cv || !boxes) return;
        const CW = cv.clientWidth || 640, CH = Math.round(CW * 9 / 16);
        cv.width = CW; cv.height = CH;
        const ctx = cv.getContext('2d');
        const css = getComputedStyle(cv);
        ctx.clearRect(0, 0, CW, CH);
        ctx.fillStyle = css.backgroundColor || '#111';
        ctx.fillRect(0, 0, CW, CH);
        const muted = css.getPropertyValue('--text-muted') || '#888';
        const strong = css.getPropertyValue('--text-strong') || '#fff';
        const accent = css.getPropertyValue('--accent') || '#7aa2c8';
        ctx.font = '11px Inter, system-ui, sans-serif';
        ctx.fillStyle = muted;
        ctx.fillText(inputs[4] ? (T('bg_short', 'Fond') + ' : ' + inputs[4]) : T('bg_none', 'Fond (non câblé)'), 6, 14);
        const scale = CW / ((st && st.width) || 1280);   // px moteur → px canvas

        zOrder().forEach(i => {
            const b = boxes[i], r = rectPx(i, CW, CH);
            ctx.save();
            ctx.translate(r.cx, r.cy);
            if (b.rotation) ctx.rotate(-b.rotation * Math.PI / 180);
            ctx.translate(-r.cx, -r.cy);
            ctx.globalAlpha = (b.enabled ? 1 : 0.28) * (b.opacity == null ? 1 : b.opacity);
            if (b.shadow && b.shadow.enabled && b.enabled) {
                ctx.save();
                ctx.globalAlpha *= b.shadow.opacity;
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
            ctx.setLineDash(sel.includes(i) ? [] : []);
            pathRect(ctx, r, (b.radius || 0) * scale);
            ctx.stroke();
            ctx.fillStyle = strong;
            ctx.fillText(`${T('box', 'Box')} ${i + 1}${inputs[i] ? ' · ' + inputs[i] : ''}`, r.x0 + 4, r.y0 + 14);
            if (sel.includes(i)) {   // poignée de redimensionnement (coin bas-droit)
                ctx.fillStyle = accent;
                ctx.fillRect(r.x0 + r.w - 7, r.y0 + r.h - 7, 7, 7);
            }
            ctx.restore();
        });
        redrawSidebar();
    }
    function canvasPos(e) {
        const cv = $('#split_canvas'), rect = cv.getBoundingClientRect();
        return { px: e.clientX - rect.left, py: e.clientY - rect.top, CW: cv.width, CH: cv.height };
    }
    const EDGE_PX = 7;
    const CURSOR = { move: 'move', resize: 'nwse-resize', 'crop-l': 'ew-resize', 'crop-r': 'ew-resize', 'crop-t': 'ns-resize', 'crop-b': 'ns-resize' };
    function hitTest(px, py, CW, CH) {
        const order = zOrder().slice().reverse();   // la plus haute d'abord
        for (const i of order) {
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
        drag = { idx: h.idx, mode: h.mode, sx: px, sy: py, bx: b.x, by: b.y, bsize: b.size };
        e.target.setPointerCapture(e.pointerId);
    }
    function canvasMove(e) {
        const { px, py, CW, CH } = canvasPos(e);
        if (!drag) {
            const cv = $('#split_canvas');
            if (cv) { const h = hitTest(px, py, CW, CH); cv.style.cursor = h ? CURSOR[h.mode] : 'default'; }
            return;
        }
        const b = boxes[drag.idx], fw = wOf(b) * CW, fh = hOf(b) * CH;
        const [cmin, cmax] = cap('crop');
        const capL = v => clamp(v, cmin, Math.min(cmax, 0.92 - b.crop.r)), capR = v => clamp(v, cmin, Math.min(cmax, 0.92 - b.crop.l));
        const capT = v => clamp(v, cmin, Math.min(cmax, 0.92 - b.crop.b)), capB = v => clamp(v, cmin, Math.min(cmax, 0.92 - b.crop.t));
        const [smin, smax] = cap('box_fields', 'size');
        switch (drag.mode) {
            case 'move': b.x = clamp(drag.bx + (px - drag.sx) / CW, 0, 1); b.y = clamp(drag.by + (py - drag.sy) / CH, 0, 1); break;
            case 'resize': b.size = clamp(Math.abs(px - b.x * CW) * 2 / CW, smin, smax); break;
            case 'crop-l': b.crop.l = capL((px - b.x * CW) / fw + 0.5); break;
            case 'crop-r': b.crop.r = capR(0.5 - (px - b.x * CW) / fw); break;
            case 'crop-t': b.crop.t = capT((py - b.y * CH) / fh + 0.5); break;
            case 'crop-b': b.crop.b = capB(0.5 - (py - b.y * CH) / fh); break;
        }
        drawCanvas(); throttledPostBox(drag.idx);
    }
    function canvasUp() {
        if (!drag) return;
        const i = drag.idx; drag = null;
        const b = boxes[i];
        postBox(i, { x: b.x, y: b.y, size: b.size, crop: b.crop });
        syncAll();
    }

    // ─── Écritures serveur ──────────────────────────────────────────────────────────
    function throttledPostBox(i) {
        if (boxPostTimer) return;
        boxPostTimer = setTimeout(() => {
            boxPostTimer = null;
            const b = boxes[i];
            postBox(i, { x: b.x, y: b.y, size: b.size, crop: b.crop }, true);
        }, 60);
    }
    async function postBox(i, patch, skipPersist) {
        if (VMID == null) return;
        try {
            const r = await fetch(`/api/containers/${VMID}/plugin/box`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({ idx: i }, patch)),
            });
            if (!r.ok) {   // le moteur refuse → le DIRE (jamais d'échec silencieux)
                const j = await r.json().catch(() => ({}));
                TOAST(T('box_err', 'Réglage refusé par le moteur') + ' : ' + (j.error || ('HTTP ' + r.status)), 'error');
            }
        } catch (e) { TOAST(T('box_err', 'Réglage refusé par le moteur') + ' : ' + e.message, 'error'); }
        if (!skipPersist) schedulePersist();
    }
    async function postInput(idx, shm) {
        if (VMID == null) return;
        inputs[idx] = shm;
        try {
            await fetch(`/api/containers/${VMID}/plugin/input`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ idx, shm }),
            });
        } catch (e) {}
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
        const ctx = canvas.getContext('2d');
        const css = getComputedStyle(canvas);
        ctx.fillStyle = css.backgroundColor || '#111'; ctx.fillRect(0, 0, CW, CH);
        (bx || []).slice(0, 4).forEach((raw, i) => {
            const b = normBox(raw); if (!b.enabled) return;
            const r = rectN(b);
            ctx.globalAlpha = (b.opacity == null ? 1 : b.opacity);
            ctx.fillStyle = BOX_COLORS[i % 4];
            ctx.fillRect(r.x0 * CW, r.y0 * CH, r.w * CW, r.h * CH);
            ctx.globalAlpha = 1;
            ctx.strokeStyle = css.getPropertyValue('--text-strong') || '#fff';
            ctx.lineWidth = 1;
            ctx.strokeRect(r.x0 * CW, r.y0 * CH, r.w * CW, r.h * CH);
        });
    }
    function sendSelect(memId) {
        const others = containers.filter(d => d.vmid !== VMID);
        if (!others.length) return '';
        return `<select data-send="${memId}">
            <option value="">${esc(T('mem_send', 'Envoyer vers…'))}</option>
            ${others.map(d => `<option value="${d.vmid}">${esc(d.hostname)} (#${d.vmid})</option>`).join('')}</select>`;
    }
    async function loadMemories() {
        if (VMID == null) return;
        const ul = $('#split_mem_list'); if (!ul) return;
        try {
            const mems = await (await fetch(`/api/containers/${VMID}/split/memories`)).json();
            memCache = mems;
            if (!mems.length) { ul.innerHTML = `<li class="split-empty">${esc(T('mem_empty', 'Aucune mémoire.'))}</li>`; return; }
            ul.innerHTML = mems.map(m => {
                const nOn = (m.config || []).filter(b => b && b.enabled).length;
                return `<li class="split-mem-card"><b title="${esc(m.name)}">${esc(m.name)}</b>
                    <canvas data-mem-preview="${m.id}"></canvas>
                    <span class="split-note">${nOn} ${esc(T('mem_active', 'box active(s)'))}</span>
                    <div class="split-mem-actions">
                        <button type="button" class="btn btn-blue" data-recall="${m.id}">${esc(T('mem_recall', 'Rappeler'))}</button>
                        <button type="button" class="btn" data-export="${m.id}">${esc(T('mem_export', 'Export'))}</button>
                        <button type="button" class="btn btn-red" data-del="${m.id}" title="${esc(T('mem_delete', 'Supprimer'))}">✕</button>
                        ${sendSelect(m.id)}
                    </div></li>`;
            }).join('');
            mems.forEach(m => drawMemPreview(ul.querySelector(`canvas[data-mem-preview="${m.id}"]`), m.config));
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
        if (!name) { TOAST(T('mem_need_name', 'Donnez un nom à la mémoire.'), 'warning'); return; }
        const r = await fetch(`/api/containers/${VMID}/split/memories`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
        });
        if (r.ok) { $('#split_mem_name').value = ''; loadMemories(); }
        else TOAST(T('mem_save_err', 'Erreur d\'enregistrement de la mémoire'), 'error');
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
        catch (e) { TOAST(T('mem_bad_file', 'Fichier invalide (JSON)') + ' : ' + e.message, 'error'); return; }
        if (!Array.isArray(data.config)) { TOAST(T('mem_bad_cfg', 'Fichier invalide : champ « config » manquant.'), 'error'); return; }
        const name = (data.name || file.name.replace(/\.json$/i, '')).trim() || T('mem_imported', 'mémoire importée');
        const r = await fetch(`/api/containers/${VMID}/split/memories`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, config: data.config }),
        });
        if (r.ok) loadMemories(); else TOAST(T('mem_import_err', 'Erreur d\'import') + ' (HTTP ' + r.status + ')', 'error');
    }
    async function envoyerMemoire(memId, targetVmid) {
        const m = memCache.find(x => x.id === parseInt(memId)); if (!m) return;
        const r = await fetch(`/api/containers/${targetVmid}/split/memories`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: m.name, config: m.config }),
        });
        const tgt = containers.find(d => d.vmid === parseInt(targetVmid));
        TOAST(r.ok ? `${T('mem_sent', 'Mémoire copiée vers')} ${tgt ? tgt.hostname : ('#' + targetVmid)}.`
            : T('mem_send_err', 'Erreur d\'envoi') + ' (HTTP ' + r.status + ')', r.ok ? 'info' : 'error');
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
    function syncAll() { renderStrip(); renderProps(); updateToolbar(); drawCanvas(); }
    function onResize() { if (EL && $('#split_canvas')) drawCanvas(); }

    // ─── Mini-aperçu de la sidebar Traitements ──────────────────────────────────────
    function drawSchematic(cv, rawBoxes) {
        if (!cv) return;
        const CW = cv.clientWidth || 200, CH = Math.round(CW * 9 / 16);
        cv.width = CW; cv.height = CH;
        const ctx = cv.getContext('2d'), css = getComputedStyle(cv);
        ctx.clearRect(0, 0, CW, CH);
        ctx.fillStyle = css.backgroundColor || '#111'; ctx.fillRect(0, 0, CW, CH);
        (rawBoxes || []).slice(0, 4).forEach((raw, i) => {
            const b = normBox(raw); if (!b.enabled) return;
            const r = rectN(b);
            ctx.fillStyle = BOX_COLORS[i % 4];
            ctx.fillRect(r.x0 * CW, r.y0 * CH, r.w * CW, r.h * CH);
            ctx.strokeStyle = css.getPropertyValue('--text-strong') || '#fff';
            ctx.lineWidth = 1;
            ctx.strokeRect(r.x0 * CW, r.y0 * CH, r.w * CW, r.h * CH);
            ctx.fillStyle = css.getPropertyValue('--text-strong') || '#fff';
            ctx.font = '9px Inter, system-ui, sans-serif';
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
        sources = []; containers = []; memCache = []; sel = [0]; clipboard = null; adv = null; metricsErr = false;
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
        EL = null; VMID = null; drag = null;
    }

    const exp = { mount, unmount, listPreview };
    window.__split = exp;
    return exp;
})();
