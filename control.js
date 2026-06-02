// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// DVE / SuperSource — UI de contrôle embarquée (plugin). Montée par le shell Traitements via
// window.MXLPlugins.dve.mount(el, vmid, ctx). Un seul éditeur actif (le shell démonte le précédent).
// Endpoints inchangés : /plugin/{state,box,input} (proxy :8082) + /dve/{persist,memories,...} (Flask).
'use strict';
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins.dve = (function () {
    let EL = null, VMID = null, TOAST = () => {};
    let boxes = null, inputs = ['', '', '', '', ''], sources = [];
    let drag = null, boxPostTimer = null, persistTimer = null, pollTimer = null;
    let containers = [], memCache = [], clipboard = null;

    const esc = s => (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const $ = sel => EL ? EL.querySelector(sel) : null;
    function clamp(v, lo, hi){ v=parseFloat(v); if(isNaN(v)) return lo; return Math.max(lo, Math.min(hi, v)); }

    async function loadSources(){
        try {
            const data = await (await fetch('/api/home/summary')).json();
            const seen=new Set(), out=[];
            for(const n of (data.nodes||data.topo_nodes||[]))
                for(const p of (n.produces||[]))
                    if(p.kind==='video' && p.shm && !seen.has(p.shm)){ seen.add(p.shm); out.push({shm:p.shm, label:`${n.hostname||('#'+n.vmid)} · ${p.shm}`}); }
            sources=out;
        } catch(e){ sources=[]; }
    }
    async function loadContainers(){
        try { containers = (await (await fetch('/api/plugins/instances?type=dve')).json())
            .map(c => ({vmid:c.vmid, hostname:c.hostname||('VMID '+c.vmid)})); }
        catch(e){ containers=[]; }
    }
    function srcOptions(selected){
        let h='<option value="">— non câblé —</option>', found=false;
        for(const s of sources){ const sel=(s.shm===selected)?' selected':''; if(sel) found=true;
            h+=`<option value="${esc(s.shm)}"${sel}>${esc(s.label)}</option>`; }
        if(selected && !found) h+=`<option value="${esc(selected)}" selected>${esc(selected)} (?)</option>`;
        return h;
    }

    function editing(){
        if(drag) return true;
        const ae=document.activeElement;
        return ae && EL && EL.contains(ae) && ['INPUT','SELECT'].includes(ae.tagName);
    }

    async function fetchState({rebuild=false}={}){
        if(VMID==null || !EL) return;
        try {
            const r=await fetch(`/api/containers/${VMID}/plugin/state`); if(!r.ok) throw new Error('HTTP '+r.status);
            const st=await r.json();
            if(rebuild || !$('#dve_canvas')){
                boxes=(st.boxes||[]).map(normBox); while(boxes.length<4) boxes.push(normBox({}));
                inputs=(st.inputs||[]).slice(0,5); while(inputs.length<5) inputs.push('');
                renderEditor(); loadMemories();
            } else if(!editing()){
                boxes=(st.boxes||[]).map(normBox); inputs=(st.inputs||[]).slice(0,5);
                syncFields(); drawCanvas();
            }
        } catch(e){ if(EL) EL.innerHTML=`<div style="color:var(--status-stopped-fg)">Erreur : ${esc(e.message)}</div>`; }
    }

    function normBox(b){
        b=b||{}; const crop=b.crop||{}, bd=b.border||{};
        return {
            enabled:!!b.enabled,
            x:clamp(b.x??0.5,0,1), y:clamp(b.y??0.5,0,1), size:clamp(b.size??0.45,0.05,1),
            crop:{t:clamp(crop.t||0,0,0.9), b:clamp(crop.b||0,0,0.9), l:clamp(crop.l||0,0,0.9), r:clamp(crop.r||0,0,0.9)},
            border:{w:Math.round(clamp(bd.w||0,0,40)), color:bd.color||'#ffffff'},
        };
    }

    function renderEditor(){
        let cards=''; for(let i=0;i<4;i++) cards+=boxCardHtml(i);
        EL.innerHTML=`
            <div class="dve-canvas-wrap"><canvas id="dve_canvas"></canvas></div>
            <div class="dve-row"><label>Fond</label>
                <select onchange="__dve.postInput(4, this.value)">${srcOptions(inputs[4])}</select></div>
            <div class="dve-boxes">${cards}</div>
            <div class="dve-memories"><strong>Mémoires</strong>
                <ul class="dve-mem-list" id="dve-mem-list"></ul>
                <div class="dve-mem-bar">
                    <input type="text" id="dve-mem-name" placeholder="Nom de la mémoire" style="width:12em">
                    <button class="btn btn-green" onclick="__dve.saveMemory()">Enregistrer la disposition</button>
                    <button class="btn btn-ghost" onclick="__dve.importClick()">Importer un fichier</button>
                    <input type="file" id="dve-mem-import" accept="application/json,.json" style="display:none" onchange="__dve.onImportMemFile(this)">
                    <label style="margin-left:12px">Transition&nbsp;
                        <input type="range" id="dve-recall-dur" min="0" max="5000" step="100" value="1000"
                               oninput="this.parentNode.querySelector('#dve-dur-val').textContent=(this.value/1000).toFixed(1)+'s'">
                        <span id="dve-dur-val">1.0s</span></label>
                </div></div>`;
        const cv=$('#dve_canvas');
        cv.addEventListener('pointerdown', canvasDown);
        cv.addEventListener('pointermove', canvasMove);
        cv.addEventListener('pointerup', canvasUp);
        cv.addEventListener('pointercancel', canvasUp);
        drawCanvas();
    }

    function boxCardHtml(i){
        const b=boxes[i];
        return `<div class="dve-box-card${b.enabled?' on':''}" id="dve-card-${i}">
            <h4><input type="checkbox" ${b.enabled?'checked':''} onchange="__dve.postBox(${i},{enabled:this.checked})"> Box ${i+1}</h4>
            <div class="dve-row"><label>Source</label>
                <select onchange="__dve.postInput(${i}, this.value)">${srcOptions(inputs[i])}</select></div>
            <div class="dve-row"><label>X</label><input type="number" step="0.01" min="0" max="1" value="${b.x.toFixed(2)}" data-f="x" data-i="${i}" onchange="__dve.fieldChange(${i})">
                <label>Y</label><input type="number" step="0.01" min="0" max="1" value="${b.y.toFixed(2)}" data-f="y" data-i="${i}" onchange="__dve.fieldChange(${i})"></div>
            <div class="dve-row"><label>Taille</label><input type="number" step="0.01" min="0.05" max="1" value="${b.size.toFixed(2)}" data-f="size" data-i="${i}" onchange="__dve.fieldChange(${i})"></div>
            <div class="dve-crop">
                <label>Crop H<input type="number" step="0.01" min="0" max="0.9" value="${b.crop.t.toFixed(2)}" data-c="t" data-i="${i}" onchange="__dve.cropChange(${i})"></label>
                <label>Crop B<input type="number" step="0.01" min="0" max="0.9" value="${b.crop.b.toFixed(2)}" data-c="b" data-i="${i}" onchange="__dve.cropChange(${i})"></label>
                <label>Crop G<input type="number" step="0.01" min="0" max="0.9" value="${b.crop.l.toFixed(2)}" data-c="l" data-i="${i}" onchange="__dve.cropChange(${i})"></label>
                <label>Crop D<input type="number" step="0.01" min="0" max="0.9" value="${b.crop.r.toFixed(2)}" data-c="r" data-i="${i}" onchange="__dve.cropChange(${i})"></label>
            </div>
            <div class="dve-row"><label>Bordure</label>
                <input type="number" step="1" min="0" max="40" value="${b.border.w}" data-b="w" data-i="${i}" onchange="__dve.borderChange(${i})" style="width:4em" title="Épaisseur en pixels (0 = aucune)">
                <input type="color" value="${b.border.color}" data-b="color" data-i="${i}" onchange="__dve.borderChange(${i})" title="Couleur de la bordure"></div>
            <div class="dve-row" style="gap:6px">
                <button class="btn btn-ghost" style="flex:1" onclick="__dve.copierBox(${i})" title="Copier taille, position, crop et bordure">⧉ Copier</button>
                <button class="btn btn-ghost" style="flex:1" onclick="__dve.collerBox(${i})" ${clipboard?'':'disabled'} title="Coller les réglages copiés">⪼ Coller</button>
            </div></div>`;
    }

    function syncFields(){
        for(let i=0;i<4;i++){
            const card=$('#dve-card-'+i); if(!card) continue;
            const b=boxes[i];
            card.classList.toggle('on', b.enabled);
            const cb=card.querySelector('input[type=checkbox]'); if(cb) cb.checked=b.enabled;
            card.querySelectorAll('input[data-f]').forEach(inp=>{ inp.value=b[inp.dataset.f].toFixed(2); });
            card.querySelectorAll('input[data-c]').forEach(inp=>{ inp.value=b.crop[inp.dataset.c].toFixed(2); });
            card.querySelectorAll('input[data-b]').forEach(inp=>{ inp.value=(inp.dataset.b==='w')?b.border.w:b.border.color; });
        }
    }
    function borderChange(i){
        const card=$('#dve-card-'+i);
        const w=parseInt(card.querySelector('input[data-b="w"]').value)||0;
        const color=card.querySelector('input[data-b="color"]').value||'#ffffff';
        boxes[i].border={w:Math.max(0,Math.min(40,w)), color};
        drawCanvas(); postBox(i, {border:boxes[i].border});
    }
    function copierBox(i){
        if(!boxes) return;
        const b=boxes[i];
        clipboard={x:b.x,y:b.y,size:b.size,crop:Object.assign({},b.crop),border:Object.assign({},b.border)};
        updatePasteBtns(); flashBtn(`#dve-card-${i} button[onclick*="copierBox"]`, '✓ Copié');
    }
    function collerBox(i){
        if(!boxes||!clipboard) return;
        const patch={x:clipboard.x,y:clipboard.y,size:clipboard.size,crop:Object.assign({},clipboard.crop),border:Object.assign({},clipboard.border)};
        Object.assign(boxes[i], normBox(Object.assign({}, boxes[i], patch)));
        postBox(i, patch); drawCanvas(); syncFields();
        flashBtn(`#dve-card-${i} button[onclick*="collerBox"]`, '✓ Collé');
    }
    function updatePasteBtns(){ EL && EL.querySelectorAll('button[onclick*="collerBox"]').forEach(b=>{ b.disabled=!clipboard; }); }
    function flashBtn(sel, txt){ const btn=$(sel); if(!btn) return; const t=btn.textContent; btn.textContent=txt; setTimeout(()=>{ btn.textContent=t; },1200); }

    function fieldChange(i){
        const card=$('#dve-card-'+i), patch={};
        card.querySelectorAll('input[data-f]').forEach(inp=>{ patch[inp.dataset.f]=parseFloat(inp.value); });
        Object.assign(boxes[i], normBox(Object.assign({}, boxes[i], patch)));
        drawCanvas(); postBox(i, patch);
    }
    function cropChange(i){
        const card=$('#dve-card-'+i), crop={};
        card.querySelectorAll('input[data-c]').forEach(inp=>{ crop[inp.dataset.c]=parseFloat(inp.value); });
        boxes[i].crop=Object.assign(boxes[i].crop, normBox({crop}).crop);
        drawCanvas(); postBox(i, {crop:boxes[i].crop});
    }

    // ─── Canvas ───────────────────────────────────────────
    function boxRectPx(i, CW, CH){
        const b=boxes[i], fw=b.size*CW, fh=b.size*CH;
        const visW=fw*(1-b.crop.l-b.crop.r), visH=fh*(1-b.crop.t-b.crop.b);
        const cx=b.x*CW+(b.crop.l-b.crop.r)*0.5*fw, cy=b.y*CH+(b.crop.t-b.crop.b)*0.5*fh;
        return {x0:cx-visW/2, y0:cy-visH/2, w:visW, h:visH};
    }
    function drawCanvas(){
        const cv=$('#dve_canvas'); if(!cv||!boxes) return;
        const CW=cv.clientWidth, CH=Math.round(CW*9/16); cv.width=CW; cv.height=CH;
        const ctx=cv.getContext('2d'); ctx.clearRect(0,0,CW,CH);
        ctx.fillStyle=inputs[4]?'#1d3a5f':'#111'; ctx.fillRect(0,0,CW,CH);
        ctx.fillStyle='rgba(255,255,255,.4)'; ctx.font='11px sans-serif';
        ctx.fillText(inputs[4]?('Fond : '+inputs[4]):'Fond (non câblé)', 6, 14);
        for(let i=0;i<4;i++){
            const b=boxes[i], r=boxRectPx(i,CW,CH);
            ctx.globalAlpha=b.enabled?1:0.3; ctx.fillStyle=['#c0504d','#4f81bd','#9bbb59','#8064a2'][i]; ctx.fillRect(r.x0,r.y0,r.w,r.h);
            ctx.globalAlpha=1; ctx.strokeStyle=b.enabled?'#fff':'#888'; ctx.lineWidth=1.5; ctx.strokeRect(r.x0,r.y0,r.w,r.h);
            if(b.border && b.border.w>0){ ctx.strokeStyle=b.border.color; ctx.lineWidth=Math.max(1,b.border.w*CW/1280); ctx.strokeRect(r.x0,r.y0,r.w,r.h); }
            ctx.fillStyle='#fff'; ctx.font='11px sans-serif'; ctx.fillText(`Box ${i+1}${inputs[i]?' · '+inputs[i]:''}`, r.x0+4, r.y0+14);
            ctx.fillStyle='#fff'; ctx.fillRect(r.x0+r.w-7, r.y0+r.h-7, 7, 7);
        }
        redrawSidebar();   // garde l'aperçu de la sidebar en phase (édition live, pas de Deploy)
    }
    function canvasPos(e){ const cv=$('#dve_canvas'), rect=cv.getBoundingClientRect(); return {px:e.clientX-rect.left, py:e.clientY-rect.top, CW:cv.width, CH:cv.height}; }
    // Détecte ce que vise le curseur sur la box i (la plus haute d'abord) :
    // coin bas-droit → resize ; arête → crop du côté (l/r/t/b) ; intérieur → move.
    const EDGE_PX = 7;
    const CURSOR = {move:'move', resize:'nwse-resize', 'crop-l':'ew-resize',
        'crop-r':'ew-resize', 'crop-t':'ns-resize', 'crop-b':'ns-resize'};
    function hitTest(px,py,CW,CH){
        const E=EDGE_PX;
        for(let i=3;i>=0;i--){
            const r=boxRectPx(i,CW,CH);
            if(px<r.x0-E || px>r.x0+r.w+E || py<r.y0-E || py>r.y0+r.h+E) continue;
            if(px>=r.x0+r.w-10 && px<=r.x0+r.w+4 && py>=r.y0+r.h-10 && py<=r.y0+r.h+4)
                return {idx:i, mode:'resize'};
            const onV=py>=r.y0-E && py<=r.y0+r.h+E, onH=px>=r.x0-E && px<=r.x0+r.w+E;
            if(onH && Math.abs(py-r.y0)<=E)        return {idx:i, mode:'crop-t'};
            if(onH && Math.abs(py-(r.y0+r.h))<=E)  return {idx:i, mode:'crop-b'};
            if(onV && Math.abs(px-r.x0)<=E)        return {idx:i, mode:'crop-l'};
            if(onV && Math.abs(px-(r.x0+r.w))<=E)  return {idx:i, mode:'crop-r'};
            if(px>=r.x0 && px<=r.x0+r.w && py>=r.y0 && py<=r.y0+r.h)
                return {idx:i, mode:'move'};
        }
        return null;
    }
    function canvasDown(e){
        const {px,py,CW,CH}=canvasPos(e);
        const h=hitTest(px,py,CW,CH); if(!h) return;
        const b=boxes[h.idx];
        drag={idx:h.idx, mode:h.mode, sx:px, sy:py, bx:b.x, by:b.y, bsize:b.size};
        e.target.setPointerCapture(e.pointerId);
    }
    function canvasMove(e){
        const {px,py,CW,CH}=canvasPos(e);
        if(!drag){   // pas de drag : indique l'action au survol via le curseur
            const cv=$('#dve_canvas'); if(cv){ const h=hitTest(px,py,CW,CH); cv.style.cursor=h?CURSOR[h.mode]:'default'; }
            return;
        }
        const b=boxes[drag.idx], fw=b.size*CW, fh=b.size*CH;
        const capL=v=>clamp(v,0,Math.min(0.9,0.92-b.crop.r)), capR=v=>clamp(v,0,Math.min(0.9,0.92-b.crop.l));
        const capT=v=>clamp(v,0,Math.min(0.9,0.92-b.crop.b)), capB=v=>clamp(v,0,Math.min(0.9,0.92-b.crop.t));
        switch(drag.mode){
            case 'move':   b.x=clamp(drag.bx+(px-drag.sx)/CW,0,1); b.y=clamp(drag.by+(py-drag.sy)/CH,0,1); break;
            case 'resize': b.size=clamp(Math.abs(px-b.x*CW)*2/CW, 0.05, 1); break;
            case 'crop-l': b.crop.l=capL((px-b.x*CW)/fw + 0.5); break;
            case 'crop-r': b.crop.r=capR(0.5 - (px-b.x*CW)/fw); break;
            case 'crop-t': b.crop.t=capT((py-b.y*CH)/fh + 0.5); break;
            case 'crop-b': b.crop.b=capB(0.5 - (py-b.y*CH)/fh); break;
        }
        drawCanvas(); throttledPostBox(drag.idx);
    }
    function canvasUp(){
        if(!drag) return;
        const i=drag.idx; drag=null; syncFields();
        postBox(i, {x:boxes[i].x, y:boxes[i].y, size:boxes[i].size, crop:boxes[i].crop});
    }

    // ─── Écritures serveur ────────────────────────────────
    function throttledPostBox(i){
        if(boxPostTimer) return;
        boxPostTimer=setTimeout(()=>{ boxPostTimer=null; const b=boxes[i]; postBox(i, {x:b.x,y:b.y,size:b.size,crop:b.crop}, true); }, 60);
    }
    async function postBox(i, patch, skipPersist){
        if(VMID==null) return;
        if('enabled' in patch){ boxes[i].enabled=patch.enabled; const c=$('#dve-card-'+i); if(c) c.classList.toggle('on', patch.enabled); }
        try { await fetch(`/api/containers/${VMID}/plugin/box`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(Object.assign({idx:i}, patch))}); } catch(e){}
        if(!skipPersist) schedulePersist();
    }
    async function postInput(idx, shm){
        if(VMID==null) return;
        inputs[idx]=shm;
        try { await fetch(`/api/containers/${VMID}/plugin/input`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({idx, shm})}); } catch(e){}
        drawCanvas(); schedulePersist();
    }
    function schedulePersist(){
        if(persistTimer) clearTimeout(persistTimer);
        persistTimer=setTimeout(()=>{ persistTimer=null; if(VMID!=null) fetch(`/api/containers/${VMID}/dve/persist`, {method:'POST'}).catch(()=>{}); }, 1500);
    }

    // ─── Mémoires ─────────────────────────────────────────
    function drawMemPreview(canvas, bx){
        if(!canvas) return;
        const CW=canvas.clientWidth||150, CH=Math.round(CW*9/16); canvas.width=CW; canvas.height=CH;
        const ctx=canvas.getContext('2d'); ctx.fillStyle='#111'; ctx.fillRect(0,0,CW,CH);
        (bx||[]).forEach((raw,i)=>{
            if(i>=4) return; const b=normBox(raw); if(!b.enabled) return;
            const fw=b.size*CW, fh=b.size*CH, visW=fw*(1-b.crop.l-b.crop.r), visH=fh*(1-b.crop.t-b.crop.b);
            const cx=b.x*CW+(b.crop.l-b.crop.r)*0.5*fw, cy=b.y*CH+(b.crop.t-b.crop.b)*0.5*fh;
            ctx.fillStyle=['#c0504d','#4f81bd','#9bbb59','#8064a2'][i]+'cc'; ctx.fillRect(cx-visW/2, cy-visH/2, visW, visH);
            ctx.strokeStyle='#fff'; ctx.lineWidth=1; ctx.strokeRect(cx-visW/2, cy-visH/2, visW, visH);
        });
    }
    function sendSelect(memId){
        const others=containers.filter(d=>d.vmid!==VMID);
        if(!others.length) return '';
        return `<select onchange="if(this.value){__dve.envoyerMemoire(${memId}, this.value); this.value=''}">
            <option value="">Envoyer vers…</option>
            ${others.map(d=>`<option value="${d.vmid}">${esc(d.hostname)} (#${d.vmid})</option>`).join('')}</select>`;
    }
    async function loadMemories(){
        if(VMID==null) return; const ul=$('#dve-mem-list'); if(!ul) return;
        try {
            const mems=await (await fetch(`/api/containers/${VMID}/dve/memories`)).json();
            memCache=mems;
            if(!mems.length){ ul.innerHTML='<li class="meta">Aucune mémoire.</li>'; return; }
            ul.innerHTML=mems.map(m=>{
                const nOn=(m.config||[]).filter(b=>b&&b.enabled).length;
                return `<li class="dve-mem-card"><b title="${esc(m.name)}">${esc(m.name)}</b>
                    <canvas data-mem-preview="${m.id}"></canvas>
                    <div class="meta" style="font-size:0.92em">${nOn} box active(s)</div>
                    <div class="dve-mem-actions">
                        <button class="btn btn-blue" onclick="__dve.recallMemory(${m.id})">▶ Rappeler</button>
                        <button class="btn btn-ghost" onclick="__dve.exporterMemoire(${m.id})">Export</button>
                        <button class="btn btn-red" onclick="__dve.deleteMemory(${m.id})" title="Supprimer">✕</button>
                        ${sendSelect(m.id)}
                    </div></li>`;
            }).join('');
            mems.forEach(m=>drawMemPreview(ul.querySelector(`canvas[data-mem-preview="${m.id}"]`), m.config));
        } catch(e){ ul.innerHTML=''; }
    }
    async function saveMemory(){
        const name=($('#dve-mem-name').value||'').trim();
        if(!name){ TOAST('Donne un nom à la mémoire.', 'warning'); return; }
        const r=await fetch(`/api/containers/${VMID}/dve/memories`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
        if(r.ok){ $('#dve-mem-name').value=''; loadMemories(); } else TOAST('Erreur enregistrement mémoire', 'error');
    }
    async function recallMemory(id){
        const dur=parseInt($('#dve-recall-dur').value)||0;
        await fetch(`/api/containers/${VMID}/dve/memories/${id}/recall`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({duration_ms:dur})});
        setTimeout(()=>fetchState({rebuild:false}), dur+200);
    }
    async function deleteMemory(id){ await fetch(`/api/containers/${VMID}/dve/memories/${id}`, {method:'DELETE'}); loadMemories(); }
    function exporterMemoire(id){
        const m=memCache.find(x=>x.id===id); if(!m) return;
        const blob=new Blob([JSON.stringify({name:m.name, config:m.config}, null, 2)], {type:'application/json'});
        const url=URL.createObjectURL(blob), a=document.createElement('a');
        a.href=url; a.download=`dve-memoire-${(m.name||'sans-nom').replace(/[^a-zA-Z0-9_-]/g,'_')}.json`; a.click(); URL.revokeObjectURL(url);
    }
    function importClick(){ const i=$('#dve-mem-import'); if(i) i.click(); }
    async function onImportMemFile(input){
        const file=input.files[0]; if(!file) return; input.value='';
        let data; try { data=JSON.parse(await file.text()); } catch(e){ TOAST('Fichier invalide (JSON) : '+e.message, 'error'); return; }
        if(!Array.isArray(data.config)){ TOAST('Fichier invalide : champ "config" manquant.', 'error'); return; }
        const name=(data.name||file.name.replace(/\.json$/i,'')).trim()||'mémoire importée';
        const r=await fetch(`/api/containers/${VMID}/dve/memories`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, config:data.config})});
        if(r.ok) loadMemories(); else TOAST('Erreur import (HTTP '+r.status+')', 'error');
    }
    async function envoyerMemoire(memId, targetVmid){
        const m=memCache.find(x=>x.id===parseInt(memId)); if(!m) return;
        const r=await fetch(`/api/containers/${targetVmid}/dve/memories`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:m.name, config:m.config})});
        const tgt=containers.find(d=>d.vmid===parseInt(targetVmid));
        TOAST(r.ok ? `Mémoire « ${m.name} » copiée vers ${tgt?tgt.hostname:('#'+targetVmid)}.` : 'Erreur envoi (HTTP '+r.status+')', r.ok?'info':'error');
    }

    function onResize(){ if(EL && $('#dve_canvas')) drawCanvas(); }

    // ─── Mini-aperçu autonome (sidebar Traitements, indépendant de l'éditeur monté) ──
    // Dessine le schéma des box d'une instance quelconque sur un canvas fourni.
    function drawSchematic(cv, rawBoxes){
        if(!cv) return;
        const CW=cv.clientWidth||200, CH=Math.round(CW*9/16); cv.width=CW; cv.height=CH;
        const ctx=cv.getContext('2d'); ctx.clearRect(0,0,CW,CH);
        ctx.fillStyle='#111'; ctx.fillRect(0,0,CW,CH);
        const cols=['#c0504d','#4f81bd','#9bbb59','#8064a2'];
        (rawBoxes||[]).slice(0,4).forEach((raw,i)=>{
            const b=normBox(raw); if(!b.enabled) return;
            const fw=b.size*CW, fh=b.size*CH;
            const visW=fw*(1-b.crop.l-b.crop.r), visH=fh*(1-b.crop.t-b.crop.b);
            const cx=b.x*CW+(b.crop.l-b.crop.r)*0.5*fw, cy=b.y*CH+(b.crop.t-b.crop.b)*0.5*fh;
            const x0=cx-visW/2, y0=cy-visH/2;
            ctx.fillStyle=cols[i]; ctx.fillRect(x0,y0,visW,visH);
            ctx.strokeStyle='#fff'; ctx.lineWidth=1; ctx.strokeRect(x0,y0,visW,visH);
            ctx.fillStyle='#fff'; ctx.font='9px sans-serif'; ctx.fillText(String(i+1), x0+3, y0+10);
        });
    }
    async function listPreview(canvas, inst){
        if(!canvas || !inst) return;
        try {
            const st=await (await fetch(`/api/containers/${inst.vmid}/plugin/state`)).json();
            drawSchematic(canvas, st.boxes||[]);
        } catch(e){}
    }
    // Rafraîchit l'aperçu de l'instance ÉDITÉE dans la sidebar à partir des box locales
    // (live, sans fetch) — la DVE n'a pas de bouton Deploy qui déclencherait un refresh.
    function redrawSidebar(){
        if(VMID==null) return;
        const cv=document.querySelector('.tp-inst-preview[data-vmid="'+VMID+'"]');
        if(cv) drawSchematic(cv, boxes||[]);
    }

    function mount(el, vmid, ctx){
        if(pollTimer){ clearInterval(pollTimer); pollTimer=null; }
        EL=el; VMID=vmid; TOAST=(ctx&&ctx.toast)||(()=>{});
        boxes=null; inputs=['','','','','']; sources=[]; drag=null; clipboard=null; memCache=[]; containers=[];
        window.addEventListener('resize', onResize);
        Promise.all([loadSources(), loadContainers()]).then(()=>fetchState({rebuild:true}));
        pollTimer=setInterval(()=>fetchState({rebuild:false}), 2000);
    }
    function unmount(){
        if(pollTimer){ clearInterval(pollTimer); pollTimer=null; }
        if(boxPostTimer){ clearTimeout(boxPostTimer); boxPostTimer=null; }
        if(persistTimer){ clearTimeout(persistTimer); persistTimer=null; }
        window.removeEventListener('resize', onResize);
        EL=null; VMID=null;
    }

    const exp = {mount, unmount, listPreview, postInput, postBox, fieldChange, cropChange, borderChange,
        copierBox, collerBox, saveMemory, recallMemory, deleteMemory, exporterMemoire,
        importClick, onImportMemFile, envoyerMemoire};
    window.__dve = exp;
    return exp;
})();
