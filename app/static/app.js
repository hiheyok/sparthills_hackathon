/* SupplyLine front end. No framework, no build step.
   Flow: understand (Bedrock) -> search (code) -> alternatives (Bedrock) -> brief (Bedrock).
   The browser drives the stages one by one so progress on screen is real. */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const KM_TO_MI = 0.621371;
  const fmtMi = (km) => { const v = km * KM_TO_MI; return (v < 10 ? v.toFixed(1) : Math.round(v)) + ' mi'; };
  const SAVE_KEY = 'supplyline.last.v1';

  const safeStore = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage full or blocked */ } },
  };

  const EXAMPLES = [
    { place: 'Downtown', radius: '8',
      text: "Wildfire warning and the power is out. My dad uses insulin and it has to stay cold. We also have a 6-month-old who needs formula and diapers. N95 masks too if you can find them." },
    { place: 'Downtown', radius: '8',
      text: "Earthquake. We smell gas in the garage, there's no running water and the power is out. There are 4 of us and a dog. We need water, canned food, and a way to charge our phones." },
    { place: 'Downtown', radius: '8',
      text: "A storm took out our power three days ago. We need D batteries for our lantern and propane for the camp stove." },
  ];

  const state = { meta: null, loc: null, radius: 8, traces: [], last: null, running: false };

  // ------------------------------------------------------------- utilities
  function pacificMinutes() {
    // The demo stores are in the San Jose area, so opening hours are read in Pacific time.
    try {
      const parts = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Los_Angeles', hour: 'numeric', minute: 'numeric', hourCycle: 'h23' }).formatToParts(new Date());
      return (+parts.find((p) => p.type === 'hour').value % 24) * 60 + +parts.find((p) => p.type === 'minute').value;
    } catch (e) { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); }
  }

  function ago(min) {
    if (min < 1) return 'just now';
    if (min < 60) return `${Math.round(min)} min ago`;
    const h = min / 60;
    return h < 36 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} d ago`;
  }

  function freshness(s) {
    const when = ago(s.age_min);
    if (s.source === 'community') return { text: `Neighbor report ${when}`, cls: 'ok' };
    if (s.confidence === 'stale') return { text: `Checked ${when}, call ahead`, cls: 'warn' };
    return { text: `Checked ${when}`, cls: s.confidence === 'fresh' ? 'ok' : '' };
  }

  function shortModel(id) {
    if (!id) return 'none';
    if (id.includes('claude-haiku-4-5')) return 'Claude Haiku 4.5';
    if (id.includes('nova-2-lite')) return 'Nova 2 Lite';
    if (id.includes('nova-lite')) return 'Nova Lite';
    return id;
  }

  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove('show'), 2600);
  }

  async function post(path, body) {
    let res;
    try {
      res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (e) {
      const err = new Error(navigator.onLine ? 'Could not reach the server. Check your connection and try again.' : 'You are offline.');
      err.offline = true;
      throw err;
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error body */ }
    if (!res.ok) { const err = new Error(data.error || `Request failed (${res.status})`); err.data = data; throw err; }
    return data;
  }

  // -------------------------------------------------------------- location
  function currentLoc() {
    if (state.loc) return state.loc;
    const sel = $('#place');
    const p = state.meta && state.meta.places.find((x) => x.id === sel.value);
    return p ? { lat: p.lat, lng: p.lng, label: p.label } : null;
  }

  function setupPlaces(places) {
    const sel = $('#place');
    sel.innerHTML = places.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
    sel.addEventListener('change', () => { state.loc = null; showOrigin(); });
    showOrigin();
  }

  $('#locate').addEventListener('click', () => {
    const hint = $('#locHint');
    if (!navigator.geolocation) { hint.textContent = 'Your browser cannot share its location. Pick a neighborhood instead.'; return; }
    hint.textContent = 'Finding you...';
    navigator.geolocation.getCurrentPosition((pos) => {
      state.loc = { lat: pos.coords.latitude, lng: pos.coords.longitude, label: 'Your location' };
      let opt = $('#place option[value="__me"]');
      if (!opt) { opt = document.createElement('option'); opt.value = '__me'; opt.textContent = 'Your location'; $('#place').prepend(opt); }
      $('#place').value = '__me';
      hint.textContent = 'Using your location. The demo stores are around San Jose, so far-away results will be empty.';
      showOrigin();
    }, () => { hint.textContent = 'Could not get your location. Pick a neighborhood instead.'; }, { enableHighAccuracy: false, timeout: 8000, maximumAge: 120000 });
  });

  // ------------------------------------------------------------------- map
  const mapState = { map: null, layer: null, straight: null, token: 0 };

  function ensureMap() {
    const el = $('#map');
    if (mapState.map) return mapState.map;
    if (!window.L) { el.textContent = 'The map could not load. Directions links still work.'; return null; }
    el.textContent = '';
    el.classList.add('has-map');
    mapState.map = L.map(el, { zoomControl: true, attributionControl: true }).setView([37.3382, -121.8863], 12);
    // OpenStreetMap's tile policy needs a Referer, so send the site origin (and nothing more).
    const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; OpenStreetMap contributors', referrerPolicy: 'strict-origin-when-cross-origin',
    }).addTo(mapState.map);
    let loaded = 0, failed = 0;
    const warn = $('#mapWarn');
    tiles.on('tileload', () => { loaded++; if (warn) warn.hidden = true; });
    tiles.on('tileerror', () => {
      failed++;
      if (!loaded && failed >= 3 && warn) warn.hidden = false;   // offline or blocked: the route is still drawn
    });
    mapState.layer = L.layerGroup().addTo(mapState.map);
    return mapState.map;
  }

  // On wide screens the map is a sticky column beside the results. On phones a map at the very
  // bottom of a long page is useless, so it moves up to sit right under the plan summary.
  const wide = window.matchMedia('(min-width: 980px)');
  function rescueMap() {
    const aside = $('.col-map'), shell = $('.shell');
    if (aside.parentNode !== shell) shell.appendChild(aside);
  }
  function placeMap() {
    const aside = $('.col-map'), slot = $('#mapSlot');
    if (!wide.matches && slot) { if (aside.parentNode !== slot) slot.appendChild(aside); } else rescueMap();
    if (mapState.map) setTimeout(() => mapState.map.invalidateSize(), 60);
  }
  wide.addEventListener('change', placeMap);

  const pin = (cls, html, size) => L.divIcon({ className: `pin ${cls}`, html, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });

  function showOrigin() {
    const loc = currentLoc();
    const map = ensureMap();
    if (!map || !loc) return;
    mapState.token++;                               // a road route still loading belongs to the old view
    mapState.layer.clearLayers();
    L.marker([loc.lat, loc.lng], { icon: pin('pin-you', '', 20), keyboard: false }).addTo(mapState.layer);
    L.circle([loc.lat, loc.lng], { radius: +$('#radius').value * 1000, color: '#0a4da2', weight: 2, dashArray: '6 6', fillOpacity: 0.04 }).addTo(mapState.layer);
    map.setView([loc.lat, loc.lng], 12);
    setTimeout(() => map.invalidateSize(), 50);
  }
  $('#radius').addEventListener('change', showOrigin);

  // One tap to turn-by-turn with live traffic: the whole trip, in plan order, in Google Maps.
  function googleRouteUrl(plan) {
    const loc = plan.location, pts = plan.route.stops.map((s) => `${s.store.lat},${s.store.lng}`);
    const via = pts.slice(0, -1);
    return `https://www.google.com/maps/dir/?api=1&travelmode=driving&origin=${loc.lat},${loc.lng}&destination=${pts[pts.length - 1]}`
      + (via.length ? `&waypoints=${encodeURIComponent(via.join('|'))}` : '');
  }

  // The fastest road route through the stops, in the order the plan chose. The straight dashed
  // line is already on the map, so the map is useful at once and still works if this fails.
  async function loadRoad(plan, line, cached) {
    const token = mapState.token;
    let road = cached || null, offline = false;
    if (!road) {
      try {
        const r = await post('/api/route', { points: line.map(([lat, lng]) => ({ lat, lng })) });
        road = r.available ? r : null;
      } catch (e) { offline = !!e.offline; }
    }
    if (token !== mapState.token || !mapState.map) return;     // a newer search replaced this plan
    const note = $('#mapNote');
    if (!road) {
      note.textContent = `${offline ? 'You are offline' : 'Road directions are unavailable right now'}, so the dashed line is straight. The Directions buttons still work.`;
      return;
    }
    if (mapState.straight) mapState.layer.removeLayer(mapState.straight);
    L.polyline(road.path, { color: '#ffffff', weight: 10, opacity: 0.9, lineJoin: 'round' }).addTo(mapState.layer);   // casing, so the route reads on any map colour
    const drawn = L.polyline(road.path, { color: '#0a4da2', weight: 5, opacity: 0.95, lineJoin: 'round' }).addTo(mapState.layer);
    mapState.map.invalidateSize();
    mapState.map.fitBounds(drawn.getBounds(), { padding: [40, 40], maxZoom: 15 });
    note.textContent = `Fastest road route: about ${Math.max(1, Math.round(road.duration_min))} min, ${fmtMi(road.distance_km)} (OpenStreetMap roads, no live traffic). Small circles are other stores with stock.`;
    if (!cached && state.last && state.last.result && state.last.result.plan === plan) {
      state.last.road = road;                                  // keep it so a saved plan shows the road route offline
      safeStore.set(SAVE_KEY, state.last);
    }
  }

  function drawRoute(plan, cachedRoad) {
    const map = ensureMap();
    const loc = plan.location;
    if (!map) return;
    mapState.token++;
    mapState.layer.clearLayers();
    const bounds = [[loc.lat, loc.lng]];
    L.circle([loc.lat, loc.lng], { radius: loc.radius_km * 1000, color: '#0a4da2', weight: 2, dashArray: '6 6', fillOpacity: 0.04 }).addTo(mapState.layer);
    const inRoute = new Set(plan.route.stops.map((s) => s.store.id));
    const seen = new Set();
    plan.results.forEach((r) => r.stores.forEach((s) => {
      if (inRoute.has(s.id) || seen.has(s.id)) return;
      seen.add(s.id);
      L.marker([s.lat, s.lng], { icon: pin('pin-alt', '', 12), title: s.name, keyboard: false }).addTo(mapState.layer);
    }));
    const line = [[loc.lat, loc.lng]];
    plan.route.stops.forEach((s) => {
      line.push([s.store.lat, s.store.lng]);
      bounds.push([s.store.lat, s.store.lng]);
      L.marker([s.store.lat, s.store.lng], { icon: pin('', String(s.order), 34), title: s.store.name, keyboard: false })
        .bindTooltip(`${s.order}. ${esc(s.store.name)}`, { direction: 'top', offset: [0, -14] }).addTo(mapState.layer);
    });
    mapState.straight = line.length > 1
      ? L.polyline(line, { color: '#0a4da2', weight: 4, opacity: 0.8, dashArray: '8 8' }).addTo(mapState.layer) : null;
    L.marker([loc.lat, loc.lng], { icon: pin('pin-you', '', 20), keyboard: false }).addTo(mapState.layer);
    map.invalidateSize();
    if (bounds.length > 1) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 }); else map.setView([loc.lat, loc.lng], 12);
    const link = $('#mapLink');
    if (plan.route.stops.length) {
      link.href = googleRouteUrl(plan);
      link.hidden = false;
      $('#mapNote').textContent = 'Finding the fastest road route...';
      loadRoad(plan, line, cachedRoad);
    } else {
      link.hidden = true;
      $('#mapNote').textContent = 'No route yet.';
    }
  }

  // -------------------------------------------------------------- progress
  const STEP_FRIENDLY = { understand: 'Read your request', search: 'Check nearby stores', alternatives: 'Find alternatives', brief: 'Build your plan' };

  function setStep(id, st, info) {
    const li = $(`#progress li[data-step="${id}"]`);
    if (!li) return;
    li.classList.remove('is-active', 'is-done', 'is-skipped', 'is-fallback');
    const meta = $('[data-meta]', li);
    if (st === 'active') { li.classList.add('is-active'); }
    if (st === 'done') {
      li.classList.add('is-done');
      if (info && info.mode === 'bedrock') meta.textContent = `${shortModel(info.model)} on Bedrock, ${(info.ms / 1000).toFixed(1)}s`;
      else if (info && info.mode === 'rules') { li.classList.add('is-fallback'); meta.textContent = 'Rules fallback: Bedrock unavailable'; }
      else if (info && info.note) meta.textContent = info.note;
    }
    if (st === 'skipped') { li.classList.add('is-skipped'); meta.textContent = 'Not needed: everything is in stock'; }
  }

  function resetProgress() {
    $('#progress').hidden = false;
    const defaults = { understand: 'Amazon Bedrock', search: 'Stock, hours, distance', alternatives: 'Amazon Bedrock', brief: 'Amazon Bedrock' };
    $$('#progress li').forEach((li) => { li.className = ''; $('[data-meta]', li).textContent = defaults[li.dataset.step]; });
  }

  // ------------------------------------------------------------- rendering
  const PRIORITY_TEXT = { 1: 'Critical', 2: 'Important', 3: 'Nice to have' };

  function renderSafety(intent) {
    const us = intent.urgent_safety || {};
    $('#r-safety').innerHTML = us.flag
      ? `<div class="safety" role="alert"><h2>Call 911 first if anyone is in danger</h2><p>${esc(us.reason || 'This may be an emergency.')}</p><p>Get everyone to safety before you drive for supplies. Your plan is below when you are ready.</p></div>`
      : '';
  }

  function renderUnderstood(intent) {
    const d = intent.disaster || {}, h = intent.household || {};
    const pills = [];
    if (d.type && d.type !== 'unknown') pills.push(d.type.replace('_', ' '));
    if (h.people) pills.push(`${h.people} people`);
    if (h.infants) pills.push('infant'); if (h.elderly) pills.push('older adult'); if (h.pets) pills.push('pets');
    (h.medical || []).forEach((m) => pills.push(m));
    $('#r-understood').innerHTML = `<details><summary>What we understood</summary>
      ${d.summary ? `<p class="sumline">${esc(d.summary)}</p>` : ''}
      ${pills.length ? `<div class="pills">${pills.map((p) => `<span class="pill">${esc(p)}</span>`).join('')}</div>` : ''}
      <ul class="need-list">${intent.needs.map((n) => `<li><span><b>${esc(n.label)}</b>${n.qty > 1 ? ` x${n.qty}` : ''}
        <span class="chip ${n.priority === 1 ? 'bad' : n.priority === 2 ? 'warn' : ''}">${PRIORITY_TEXT[n.priority] || 'Important'}</span>
        ${n.inferred ? '<span class="tag ai" title="You did not say this, but the plan needs it">Inferred</span>' : ''}</span>
        ${n.why ? `<span class="why">${esc(n.why)}</span>` : ''}</li>`).join('')}</ul>
      ${intent.unmapped && intent.unmapped.length ? `<p class="sumline">Could not match to a store item: ${intent.unmapped.map(esc).join(', ')}.</p>` : ''}
    </details>`;
  }

  function storeLine(s) {
    const f = freshness(s);
    return `<div class="store"><strong>${esc(s.name)}</strong>
      <div class="facts"><span>${s.eta_min} min drive</span><span>${fmtMi(s.dist_km)}</span>${s.open_note ? `<span>${esc(s.open_note)}</span>` : ''}
      <span class="${f.cls}">${esc(f.text)}</span>${s.low ? '<span class="warn">Low stock</span>' : ''}${s.store_note ? `<span class="warn">${esc(s.store_note)}</span>` : ''}</div></div>`;
  }

  function gapText(r) {
    const st = r.stats || {};
    let t = `Nothing in stock at the ${st.checked || 0} stores within ${fmtMi(state.radius)}`;
    const parts = [];
    if (st.out) parts.push(`${st.out} sold out`);
    if (st.closed) parts.push(`${st.closed} closed`);
    if (st.no_item) parts.push(`${st.no_item} do not carry it`);
    if (parts.length) t += ` (${parts.join(', ')})`;
    t += '.';
    if (r.nearest_any) t += ` Nearest with stock: ${r.nearest_any.name}, ${fmtMi(r.nearest_any.dist_km)} away, about ${r.nearest_any.eta_min} min.`;
    return t;
  }

  function renderByItem(results, open) {
    $('#r-items').innerHTML = `<details ${open ? 'open' : ''}><summary>Every item, store by store</summary>` + results.map((r) => {
      const chip = r.status === 'found' ? ['In stock nearby', 'ok'] : r.status === 'partial' ? ['Limited stock', 'warn'] : ['Not available nearby', 'bad'];
      return `<div class="item-block"><div class="item-head"><h4>${esc(r.label)}${r.qty > 1 ? ` x${r.qty}` : ''}</h4>
        <span class="chip ${chip[1]}">${chip[0]}</span>${r.inferred ? '<span class="tag ai" title="You did not say this, but the plan needs it">Inferred</span>' : ''}</div>
        ${r.status === 'gap' ? `<p class="gap-text">${esc(gapText(r))}</p>` : r.stores.slice(0, 3).map(storeLine).join('')}</div>`;
    }).join('') + '</details>';
  }

  function directions(s) {
    const q = `${s.lat},${s.lng}`;
    return `<a class="btn-small" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=${q}&travelmode=driving">Directions</a>
            <a class="btn-small" target="_blank" rel="noopener" href="https://maps.apple.com/?daddr=${q}">Apple Maps</a>`;
  }

  function ticket(i) {
    const f = freshness(i);
    return `<li class="ticket ${i.is_alt ? 'swap' : ''}"><div class="ticket-main">
      ${i.is_alt ? '<span class="tag">Substitute</span>' : ''}<span class="ticket-name">${esc(i.name)}${i.qty > 1 ? ` x${i.qty}` : ''}</span>
      ${i.is_alt ? `<span class="ticket-sub">instead of ${esc(i.replaces_name)}</span>` : ''}</div>
      <div class="ticket-sub"><span class="${f.cls}">${esc(f.text)}</span>${i.low ? '<span class="warn">Low stock</span>' : ''}${i.qty_needed > i.qty ? `<span class="warn">Buy ${i.qty} of the ${i.qty_needed} you need</span>` : ''}</div></li>`;
  }

  function reportPanel(stop) {
    return `<details class="report"><summary>Tell neighbors what you find here</summary><div class="report-body">
      ${stop.items.map((i) => `<div class="report-row"><span>${esc(i.name)}</span>
        <button type="button" data-report data-store="${esc(stop.store.id)}" data-item="${esc(i.item_id)}" data-status="in_stock">Has it</button>
        <button type="button" data-report data-store="${esc(stop.store.id)}" data-item="${esc(i.item_id)}" data-status="low">Running low</button>
        <button type="button" data-report data-store="${esc(stop.store.id)}" data-item="${esc(i.item_id)}" data-status="out">Sold out</button></div>`).join('')}
      <div class="report-row"><span>This store</span><button type="button" data-report data-store="${esc(stop.store.id)}" data-item="*" data-status="closed">It is closed</button></div>
      <p class="hint">Reports last 6 hours and update everyone's results.</p></div></details>`;
  }

  function renderPlan(b, reveal) {
    const { plan, brief } = b;
    const stops = plan.route.stops;
    const sum = plan.summary;
    const says = Object.fromEntries((brief.steps || []).map((s) => [s.stop, s.say]));
    let html;
    if (!stops.length) {
      html = `<div class="empty-plan"><h2 id="planTitle">${esc(brief.headline)}</h2><p>${esc(brief.summary || '')}</p>
        <button type="button" class="btn-quiet" data-action="wider">Search a wider area</button></div>`;
    } else {
      html = `<h2 class="plan-title" id="planTitle">${esc(brief.headline)}</h2>
        ${brief.summary ? `<p class="lede">${esc(brief.summary)}</p>` : ''}
        <div class="facts-row"><span>${stops.length} stop${stops.length > 1 ? 's' : ''}</span><span>${plan.route.total_drive_min} min driving</span><span>${sum.covered ?? sum.in_route} of ${sum.needs} items covered</span>${sum.swapped ? `<span>${sum.swapped} substitute${sum.swapped > 1 ? 's' : ''}</span>` : ''}</div>
        <div id="mapSlot" class="map-slot"></div>
        <ol class="route"><li class="origin"><span class="node" aria-hidden="true"></span><div class="origin-text"><b>You are here</b><small>${esc(state.originLabel || '')}${state.originLabel ? ', ' : ''}leave about ${esc(plan.route.leaves)}</small></div></li>
        ${stops.map((s) => `<li class="stop"><span class="node" aria-hidden="true">${s.order}</span><div>
          <h3>${esc(s.store.name)}</h3>
          <div class="where-line"><span><b>${s.leg_min} min</b> drive, ${fmtMi(s.leg_km)}</span><span>Arrive about ${esc(s.arrive)}</span>${s.open_note ? `<span>${esc(s.open_note)}</span>` : ''}${s.store.store_note ? `<span class="warn">${esc(s.store.store_note)}</span>` : ''}</div>
          ${says[s.order] ? `<p class="say">${esc(says[s.order])}</p>` : ''}
          <ul class="tickets">${s.items.map(ticket).join('')}</ul>
          <div class="stop-actions">${directions(s.store)}${reportPanel(s)}</div></div></li>`).join('')}</ol>`;
    }
    const unresolved = plan.unresolved.filter((u) => !plan.alternatives.some((a) => a.for_item === u.item_id));
    if (unresolved.length) html += `<p class="lede">Not found nearby: ${unresolved.map((u) => esc(u.name)).join(', ')}.</p>`;
    const shorts = plan.shortfalls || [];
    if (shorts.length) html += `<p class="lede">Not enough in stock for the full amount: ${shorts.map((u) => `${esc(u.name)} (${u.found} of ${u.qty})`).join(', ')}.</p>`;
    const notes = [...(brief.safety_notes || [])];
    const stuck = brief.if_stuck || [];
    const box = (title, list) => `<div class="next-step"><b>${title}</b><ul>${list.map((n) => `<li>${esc(n)}</li>`).join('')}</ul></div>`;
    if (notes.length || stuck.length) html += `<div class="notes-grid">${notes.length ? box('Stay safe', notes) : ''}${stuck.length ? box('If you get stuck', stuck) : ''}</div>`;
    const el = $('#r-plan');
    rescueMap();                                   // the map may live inside this section on phones
    el.classList.toggle('reveal', !!reveal);
    el.innerHTML = html;
    placeMap();
  }

  const FIT_TEXT = { strong: 'does the same job', partial: 'covers part of the need', workaround: 'temporary workaround' };

  function renderAlternatives(alts) {
    const el = $('#r-alts');
    if (!alts || !alts.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<h2 class="section-title">Not in stock nearby</h2>
      <p class="section-sub">For these, we looked at what is in stock near you and picked the closest safe match. Check the notes before you swap.</p>` +
      alts.map((a) => {
        const noSub = a.verdict === 'no_safe_substitute';
        const head = noSub ? `No safe substitute for ${a.for_name.toLowerCase()}` : a.options.length ? `Nobody near you has ${a.for_name.toLowerCase()}. Try instead:` : `Nobody near you has ${a.for_name.toLowerCase()}, and nothing else fits safely`;
        const st = a.stats || {};
        const sub = `${st.checked || 0} stores checked within ${fmtMi(state.radius)}` + (a.nearest_any ? `. Nearest with stock: ${a.nearest_any.name}, ${fmtMi(a.nearest_any.dist_km)} away.` : '. None have it in stock.');
        return `<article class="sign ${noSub ? 'no-sub' : ''}"><div class="sign-head"><h3>${esc(head)}</h3><p>${esc(sub)}</p></div><div class="sign-body">
          ${a.options.map((o) => `<div class="opt ${esc(o.fit)}"><h4>${esc(o.name)}<span class="fit">${esc(FIT_TEXT[o.fit] || '')}</span></h4>
            ${o.why ? `<p>${esc(o.why)}</p>` : ''}${o.how_to_use ? `<p>${esc(o.how_to_use)}</p>` : ''}
            ${o.caution ? `<p class="caution"><b>Check:</b> ${esc(o.caution)}</p>` : ''}
            ${o.stores && o.stores[0] ? `<p class="near">${esc(o.stores[0].name)}, ${o.stores[0].eta_min} min, ${fmtMi(o.stores[0].dist_km)}</p>` : ''}</div>`).join('')}
          ${a.no_purchase_tips && a.no_purchase_tips.length ? `<div><h5>Without buying anything</h5><ul>${a.no_purchase_tips.map((t) => `<li>${esc(t)}</li>`).join('')}</ul></div>` : ''}
          ${a.escalate ? `<div class="next-step"><b>Next step</b>${esc(a.escalate)}</div>` : ''}
        </div></article>`;
      }).join('');
  }

  function renderActions() {
    $('#r-actions').innerHTML = `<button type="button" class="btn-quiet" data-action="copy">Copy plan as text</button>
      <a class="btn-quiet" data-action="sms" href="#" style="text-decoration:none;display:inline-flex;align-items:center">Text this plan</a>
      <button type="button" class="btn-quiet" data-action="print">Print</button>`;
    $('#r-followup').innerHTML = `<h2 class="section-title">Ask about your plan</h2>
      <form id="fuForm"><div class="field"><label for="fuQ" class="sr">Your question</label><input id="fuQ" maxlength="400" autocomplete="off" placeholder="Can the insulin go straight into the cooler with ice?"></div>
      <button class="btn-quiet" type="submit">Ask</button></form><div id="fuAnswer" aria-live="polite"></div>`;
  }

  function renderTrace() {
    const rows = state.traces.filter(Boolean).map((t) => {
      const name = { understand: 'Read your request', alternatives: 'Find alternatives', brief: 'Write your briefing', followup: 'Answer a question' }[t.stage] || t.stage;
      const by = t.mode === 'bedrock' ? 'Amazon Bedrock (Converse API)' : 'Rules fallback';
      const note = t.mode === 'bedrock'
        ? (t.fell_back_from && t.fell_back_from.length ? `Used after ${t.fell_back_from.map(shortModel).join(', ')} was unavailable.` : '')
        : (t.reason || '');
      return `<tr><td>${esc(name)}</td><td>${esc(by)}${note ? `<br><small>${esc(note)}</small>` : ''}</td><td>${esc(t.mode === 'bedrock' ? shortModel(t.model) : 'none')}</td>
        <td class="num">${(t.ms / 1000).toFixed(1)}s</td><td class="num">${t.tokens_in} / ${t.tokens_out}</td></tr>`;
    }).join('');
    const keepOpen = $('#r-trace details') && $('#r-trace details').open;   // do not collapse it while someone is reading it
    $('#r-trace').innerHTML = `<details${keepOpen ? ' open' : ''}><summary>Under the hood: what Bedrock did</summary>
      <p class="trace-note">Stock, distance, opening hours and routing are plain code, so they are repeatable. Amazon Bedrock reads your request, judges which substitutes are safe, and writes the briefing. It can only choose catalog items, and substitutes only from items confirmed in stock near you. Prescriptions are never substituted.</p>
      <div class="table-wrap"><table><thead><tr><th>Step</th><th>Handled by</th><th>Model</th><th>Time</th><th>Tokens in / out</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
  }

  // ------------------------------------------------------------------- run
  function showNotice(msg, kind, actionsHtml) {
    const n = $('#notice');
    n.className = 'notice' + (kind === 'bad' ? ' bad' : '');
    n.innerHTML = `${esc(msg)} ${actionsHtml || ''}`;
    n.hidden = false;
  }

  function clearResults() {
    rescueMap();
    ['r-safety', 'r-plan', 'r-alts', 'r-items', 'r-understood', 'r-actions', 'r-followup', 'r-trace'].forEach((id) => { $('#' + id).innerHTML = ''; });
    $('#notice').hidden = true;
    $('#saved').hidden = true;
    $('#r-plan').classList.remove('reveal');
  }

  async function run() {
    if (state.running) return;
    const text = $('#text').value.trim();
    const err = $('#formError');
    err.hidden = true;
    if (text.length < 3) { err.textContent = 'Tell us a little about what you need.'; err.hidden = false; $('#text').focus(); return; }
    const loc = currentLoc();
    if (!loc) { err.textContent = 'Pick where you are first.'; err.hidden = false; return; }
    state.running = true;
    state.radius = +$('#radius').value;
    state.originLabel = loc.label;
    state.traces = [];
    $('#go').disabled = true;
    $('#go').textContent = 'Working...';
    clearResults();
    $('#results').hidden = false;
    resetProgress();
    $('#progress').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    const base = { lat: loc.lat, lng: loc.lng, radius_km: state.radius, local_minutes: pacificMinutes() };

    try {
      setStep('understand', 'active');
      const u = await post('/api/understand', { text });
      state.traces.push(u.trace);
      setStep('understand', 'done', u.trace);
      renderSafety(u.intent);
      renderUnderstood(u.intent);

      setStep('search', 'active');
      const s = await post('/api/search', { ...base, needs: u.intent.needs });
      const checked = (s.results[0] && s.results[0].stats && s.results[0].stats.checked) || 0;
      setStep('search', 'done', { note: `${checked} stores within ${fmtMi(state.radius)}` });
      renderByItem(s.results, true);

      let alts = [];
      if (s.gaps.length) {
        setStep('alternatives', 'active');
        const a = await post('/api/alternatives', { ...base, intent: u.intent });
        alts = a.alternatives;
        state.traces.push(a.trace);
        setStep('alternatives', 'done', a.trace);
        renderAlternatives(alts);
      } else {
        setStep('alternatives', 'skipped');
      }

      $('#r-plan').innerHTML = '<p class="lede">Building your route...</p>';
      setStep('brief', 'active');
      const b = await post('/api/brief', { ...base, intent: u.intent, alternatives: alts });
      state.traces.push(b.trace);
      setStep('brief', 'done', b.trace);
      state.last = { savedAt: Date.now(), result: b, intent: u.intent, traces: state.traces, text, radius: state.radius, origin: loc.label };
      renderPlan(b, true);
      renderAlternatives(b.plan.alternatives);
      renderByItem(b.plan.results, false);
      renderActions();
      renderTrace();
      drawRoute(b.plan);
      safeStore.set(SAVE_KEY, state.last);
      $('#r-plan').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) {
      const actions = state.last || safeStore.get(SAVE_KEY) ? '<button type="button" class="btn-small" data-action="open-saved">Open last saved plan</button>' : '';
      showNotice(e.message || 'Something went wrong.', 'bad', `<button type="button" class="btn-small" data-action="retry">Try again</button> ${actions}`);
      if (e.data && e.data.intent) { renderSafety(e.data.intent); }
    } finally {
      state.running = false;
      $('#go').disabled = false;
      $('#go').textContent = 'Find supplies';
    }
  }

  function openSaved() {
    const saved = safeStore.get(SAVE_KEY);
    if (!saved) return;
    clearResults();
    state.radius = saved.radius;
    state.originLabel = saved.origin;
    state.traces = saved.traces || [];
    state.last = saved;
    $('#text').value = saved.text || '';
    $('#results').hidden = false;
    renderSafety(saved.intent);
    renderUnderstood(saved.intent);
    renderPlan(saved.result, false);
    renderAlternatives(saved.result.plan.alternatives);
    renderByItem(saved.result.plan.results, false);
    renderActions();
    renderTrace();
    drawRoute(saved.result.plan, saved.road);
    showNotice(`Showing the plan saved on this device at ${new Date(saved.savedAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}. Stock may have changed.`);
  }

  function showSavedPrompt() {
    const saved = safeStore.get(SAVE_KEY);
    if (!saved || !saved.result) return;
    const t = new Date(saved.savedAt).toLocaleString([], { hour: 'numeric', minute: '2-digit', month: 'short', day: 'numeric' });
    const el = $('#saved');
    el.hidden = false;
    el.innerHTML = `<p><b>You have a saved plan from ${esc(t)}.</b> It works without a connection, but stock may have changed.</p><button type="button" class="btn-quiet" data-action="open-saved">Open saved plan</button>`;
  }

  // ---------------------------------------------------------------- events
  $('#form').addEventListener('submit', (e) => { e.preventDefault(); run(); });

  // Enter searches; Shift+Enter makes a new line. Do not fire while an input method (Japanese,
  // Chinese, Korean...) is still choosing characters, because Enter confirms those.
  $('#text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); run(); }
  });

  $$('[data-example]').forEach((b) => b.addEventListener('click', () => {
    const ex = EXAMPLES[+b.dataset.example];
    $('#text').value = ex.text;
    const p = state.meta.places.find((x) => x.label.startsWith(ex.place));
    if (p) { state.loc = null; $('#place').value = p.id; }
    $('#radius').value = ex.radius;
    showOrigin();
    $('#text').focus();
  }));

  document.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-action], [data-report]');
    if (!t) return;
    if (t.dataset.report !== undefined) {
      if (t.disabled) return;
      t.disabled = true;
      try {
        await post('/api/report', { store_id: t.dataset.store, item_id: t.dataset.item, status: t.dataset.status });
        t.classList.add('sent');
        t.textContent = 'Sent';
        toast('Thanks. Neighbors will see this for the next 6 hours.');
      } catch (err) {
        t.disabled = false; // let them try again
        toast(err.message || 'Could not send that report.');
      }
      return;
    }
    const act = t.dataset.action;
    if (act === 'retry') run();
    else if (act === 'open-saved') openSaved();
    else if (act === 'print') window.print();
    else if (act === 'wider') {
      const sel = $('#radius'); const i = sel.selectedIndex;
      if (i < sel.options.length - 1) { sel.selectedIndex = i + 1; run(); } else toast('Already searching the widest area.');
    } else if (act === 'copy') {
      const text = state.last && state.last.result.plan_text;
      if (!text) return;
      try { await navigator.clipboard.writeText(text); toast('Plan copied.'); }
      catch (err) {
        const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); toast('Plan copied.'); } catch (e2) { toast('Could not copy. Use Print instead.'); }
        ta.remove();
      }
    } else if (act === 'sms') {
      e.preventDefault();
      const text = state.last && state.last.result.plan_text;
      if (text) window.location.href = 'sms:?&body=' + encodeURIComponent(text);
    }
  });

  document.addEventListener('submit', async (e) => {
    if (e.target.id !== 'fuForm') return;
    e.preventDefault();
    const q = $('#fuQ').value.trim();
    if (q.length < 3) return;
    const out = $('#fuAnswer');
    out.innerHTML = '<p class="answer">Thinking...</p>';
    try {
      const r = await post('/api/followup', { question: q, plan_text: state.last ? state.last.result.plan_text : '' });
      out.innerHTML = `<p class="answer">${esc(r.answer)}</p>`;
      state.traces.push(r.trace);
      renderTrace();
    } catch (err) { out.innerHTML = `<p class="answer bad">${esc(err.message)}</p>`; }
  });

  window.addEventListener('offline', () => showNotice('You are offline. Your last saved plan still works.', 'bad', state.last || safeStore.get(SAVE_KEY) ? '<button type="button" class="btn-small" data-action="open-saved">Open saved plan</button>' : ''));
  window.addEventListener('online', () => { if (!state.running) $('#notice').hidden = true; });

  // ------------------------------------------------------------------ boot
  (async function boot() {
    try {
      const res = await fetch('/api/meta');
      state.meta = await res.json();
      setupPlaces(state.meta.places);
      if (state.meta.data && state.meta.data.demo) $('#dataBadge').title = state.meta.data.note;
    } catch (e) {
      showNotice('Could not load the app data. Check your connection.', 'bad');
      showSavedPrompt();
      return;
    }
    showSavedPrompt();
    if ('serviceWorker' in navigator) { try { navigator.serviceWorker.register('/sw.js'); } catch (e) { /* optional */ } }
  })();
})();
