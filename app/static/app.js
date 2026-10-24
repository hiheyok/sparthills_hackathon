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
  const LISTS_KEY = 'haven.supply-lists.v1';

  const safeStore = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } },
  };

  const EXAMPLES = [
    { place: 'Downtown', radius: '8',
      text: "Wildfire warning and the power is out. My dad uses insulin and it has to stay cold. We also have a 6-month-old who needs formula and diapers. N95 masks too if you can find them." },
    { place: 'Downtown', radius: '8',
      text: "Earthquake. We smell gas in the garage, there's no running water and the power is out. There are 4 of us and a dog. We need water, canned food, and a way to charge our phones." },
    { place: 'Downtown', radius: '8',
      text: "A storm took out our power three days ago. We need D batteries for our lantern and propane for the camp stove." },
    { place: 'Downtown', radius: '8', text: 'A hurricane warning is in effect. We need bottled water, canned food, flashlights, batteries and a first aid kit.' },
    { place: 'Downtown', radius: '8', text: 'Flooding has disrupted our water supply. We need bottled water, canned food, a first aid kit and hygiene supplies.' },
    { place: 'Downtown', radius: '8', text: 'A tornado has damaged our neighborhood and the power is out. We need water, canned food, flashlights and a first aid kit.' },
  ];

  const state = { meta: null, customLocations: {}, radius: 8, traces: [], last: null, running: false };

  const scrollBehavior = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';

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

  async function fetchJson(path, options, timeout = 35000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const res = await fetch(path, { ...options, signal: controller.signal });
      let data = {};
      try { data = await res.json(); }
      catch (e) { if (e.name === 'AbortError') throw e; /* non-JSON error body */ }
      return { res, data };
    } finally { clearTimeout(timer); }
  }

  async function post(path, body) {
    let res, data;
    try {
      ({ res, data } = await fetchJson(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
    } catch (e) {
      const err = new Error(e.name === 'AbortError' ? 'The request timed out. Please try again.'
        : navigator.onLine ? 'Could not reach the server. Check your connection and try again.' : 'You are offline.');
      err.offline = e.name !== 'AbortError';
      throw err;
    }
    if (!res.ok) { const err = new Error(data.error || `Request failed (${res.status})`); err.data = data; throw err; }
    return data;
  }

  // -------------------------------------------------------------- location
  function currentLoc() {
    const sel = $('#place');
    if (state.customLocations[sel.value]) return state.customLocations[sel.value];
    const p = state.meta && state.meta.places.find((x) => x.id === sel.value);
    return p ? { lat: p.lat, lng: p.lng, label: p.label } : null;
  }

  function setupPlaces(places) {
    const sel = $('#place');
    const selected = sel.value;
    sel.innerHTML = places.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
    Object.entries(state.customLocations).forEach(([id, loc]) => addLocation(id, loc));
    if ([...sel.options].some((o) => o.value === selected)) sel.value = selected;
    showOrigin();
  }

  function addLocation(id, loc) {
    state.customLocations[id] = loc;
    let opt = [...$('#place').options].find((o) => o.value === id);
    if (!opt) { opt = document.createElement('option'); opt.value = id; $('#place').prepend(opt); }
    opt.textContent = loc.label;
  }
  $('#place').addEventListener('change', showOrigin);

  $('#locate').addEventListener('click', () => {
    const hint = $('#locHint');
    if (!navigator.geolocation) { hint.textContent = 'Your browser cannot share its location. Pick a neighborhood instead.'; return; }
    hint.textContent = 'Finding you...';
    navigator.geolocation.getCurrentPosition((pos) => {
      addLocation('__me', { lat: pos.coords.latitude, lng: pos.coords.longitude, label: 'Your location' });
      $('#place').value = '__me';
      hint.textContent = 'Using your location. The demo stores are around San Jose, so far-away results will be empty.';
      showOrigin();
    }, () => { hint.textContent = 'Could not get your location. Pick a neighborhood instead.'; }, { enableHighAccuracy: false, timeout: 8000, maximumAge: 120000 });
  });

  // ------------------------------------------------------------------- map
  const mapState = { map: null, initializing: null, straight: null, token: 0, plan: null, cachedRoad: null, line: null, loading: false, bounds: null, maxZoom: 14 };

  function ensureMap() {
    // The restored landing page uses Haven's illustration; load the live map on results.
    if (!document.body.classList.contains('has-results')) return null;
    if (mapState.map) return mapState.map;
    if (mapState.initializing) return null;
    const el = $('#map');
    el.classList.add('has-map');
    if (!window.HavenMaps) { el.textContent = 'The map is unavailable. Directions still work.'; return null; }
    mapState.initializing = HavenMaps.create(el, state.meta?.maps || {}, {
      onFallback() {
        $('#mapWarn').textContent = 'Google Maps unavailable. Showing backup map.';
        $('#mapWarn').hidden = false;
      },
      onTileError() { $('#mapWarn').hidden = false; },
    }).then((adapter) => {
      mapState.map = adapter;
      if (mapState.plan) drawRoute(mapState.plan, mapState.cachedRoad);
      else showOrigin();
    }).catch(() => { el.textContent = 'The map is unavailable. Directions still work.'; });
    return null;
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
    if (mapState.map) setTimeout(fitMap, 60);
  }
  wide.addEventListener('change', placeMap);
  new ResizeObserver(() => requestAnimationFrame(fitMap)).observe($('#map'));
  window.addEventListener('resize', () => requestAnimationFrame(fitMap));

  function fitMap() {
    const map = mapState.map;
    if (!map) return;
    map.resize();
    if (mapState.bounds) map.fit(mapState.bounds, mapState.maxZoom);
    else {
      const loc = mapState.plan ? mapState.plan.location : currentLoc();
      if (loc) map.setView([loc.lat, loc.lng], map.getZoom());
    }
  }

  function showOrigin() {
    // Form edits describe the next search; the map still belongs to the displayed plan.
    if (mapState.plan) return;
    const loc = currentLoc();
    const map = ensureMap();
    if (!map || !loc) return;
    mapState.token++;                               // a road route still loading belongs to the old view
    mapState.bounds = null;
    $('#mapLink').hidden = true;
    $('#mapNote').textContent = 'Select a store to view supplies.';
    map.clear();
    map.marker([loc.lat, loc.lng], { kind: 'pin-you', title: 'Your location' });
    map.setView([loc.lat, loc.lng], 13);
    $('#mapLocation').textContent = loc.label;
    setTimeout(fitMap, 50);
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
  function validRoad(road) {
    return road && Array.isArray(road.path) && road.path.length >= 2 && road.path.every((p) =>
      Array.isArray(p) && p.length === 2 && p.every(Number.isFinite) && Math.abs(p[0]) <= 90 && Math.abs(p[1]) <= 180)
      && Number.isFinite(road.duration_min) && road.duration_min >= 0
      && Number.isFinite(road.distance_km) && road.distance_km >= 0;
  }

  async function loadRoad(plan, line, cached) {
    const token = mapState.token;
    mapState.loading = true;
    $('#mapNote').textContent = 'Finding route…';
    let road = validRoad(cached) ? cached : null;
    let failure = 'Road directions are unavailable right now.';
    if (!road) {
      try {
        const r = await post('/api/route', { points: line.map(([lat, lng]) => ({ lat, lng })) });
        road = r.available && validRoad(r) ? r : null;
        if (!road) failure = r.reason || 'The routing service returned an invalid road path.';
      } catch (e) { failure = e.message || failure; }
    }
    if (token !== mapState.token || !mapState.map) return;     // a newer search replaced this plan
    mapState.loading = false;
    const note = $('#mapNote');
    if (!road) {
      note.innerHTML = `${esc(failure)} Dashed line: stops only. <button type="button" class="btn-small" data-action="retry-road">Retry route</button>.`;
      return;
    }
    mapState.map.line(road.path, { color: '#ffffff', weight: 10 });
    mapState.map.line(road.path, { color: '#1a73e8', weight: 5 });
    if (mapState.straight) mapState.straight.remove();
    mapState.bounds = [...road.path, ...line];
    mapState.maxZoom = 15;
    fitMap();
    note.textContent = `${Math.max(1, Math.round(road.duration_min))} min · ${fmtMi(road.distance_km)} · No live traffic`;
    if (!cached && state.last && state.last.result && state.last.result.plan === plan) {
      state.last.road = road;                                  // keep it so a saved plan shows the road route offline
      safeStore.set(SAVE_KEY, state.last);
    }
  }

  function stockPopup(store, items) {
    return `<div class="stock-popup"><h3>${esc(store.name)}</h3><p>Demo inventory</p>
      <ul class="popup-inventory" tabindex="0" aria-label="Supplies at ${esc(store.name)}">${items.map((i) => `<li>${esc(i.name)}${i.qty ? ` ×${i.qty}` : ''}</li>`).join('')}</ul>
<p>${directions(store)}</p></div>`;
  }

  function drawRoute(plan, cachedRoad) {
    mapState.plan = plan;
    mapState.cachedRoad = cachedRoad;
    const map = ensureMap();
    const loc = plan.location;
    if (!map) return;
    mapState.token++;
    mapState.plan = plan;
    map.clear();
    const bounds = [[loc.lat, loc.lng]];
    const inRoute = new Set(plan.route.stops.map((s) => s.store.id));
    const seen = new Set();
    plan.results.forEach((r) => r.stores.forEach((s) => {
      if (inRoute.has(s.id) || seen.has(s.id)) return;
      seen.add(s.id);
      const items = plan.results.filter((result) => result.stores.some((store) => store.id === s.id)).map((result) => ({ name: result.label }));
      map.marker([s.lat, s.lng], { kind: 'pin-alt', title: s.name, html: stockPopup(s, items) });
    }));
    const line = [[loc.lat, loc.lng]];
    mapState.line = line;
    plan.route.stops.forEach((s) => {
      line.push([s.store.lat, s.store.lng]);
      bounds.push([s.store.lat, s.store.lng]);
      map.marker([s.store.lat, s.store.lng], { label: String(s.order), title: s.store.name, html: stockPopup(s.store, s.items) });
    });
    mapState.straight = line.length > 1
      ? map.line(line, { weight: 4, dashed: true }) : null;
    map.marker([loc.lat, loc.lng], { kind: 'pin-you', title: 'Your location' });
    mapState.bounds = bounds.length > 1 ? bounds : null;
    mapState.maxZoom = 14;
    fitMap();
    if (bounds.length === 1) map.setView([loc.lat, loc.lng], 12);
    const link = $('#mapLink');
    if (plan.route.stops.length) {
      link.href = googleRouteUrl(plan);
      link.hidden = false;
      $('#mapNote').textContent = 'Finding route…';
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
      else if (info && info.mode === 'rules') { li.classList.add('is-fallback'); meta.textContent = 'Local planner'; }
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
    $('#r-understood').innerHTML = `<details><summary>Your request</summary>
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
    $('#r-items').innerHTML = `<details ${open ? 'open' : ''}><summary>All supplies</summary>` + results.map((r) => {
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

  function ticket(i, storeId) {
    const f = freshness(i);
    const key = `${storeId}:${i.item_id}`;
    const checked = !!(state.last && (state.last.collected || []).includes(key));
    return `<li class="ticket ${i.is_alt ? 'swap' : ''} ${checked ? 'collected' : ''}"><div class="ticket-content"><div class="ticket-main">
      ${i.is_alt ? '<span class="tag">Substitute</span>' : ''}<span class="ticket-name">${esc(i.name)}${i.qty > 1 ? ` x${i.qty}` : ''}</span>
      ${i.is_alt ? `<span class="ticket-sub">instead of ${esc(i.replaces_name)}</span>` : ''}</div>
      <div class="ticket-sub"><span class="${f.cls}">${esc(f.text)}</span>${i.low ? '<span class="warn">Low stock</span>' : ''}${i.qty_needed > i.qty ? `<span class="warn">Buy ${i.qty} of the ${i.qty_needed} you need</span>` : ''}</div></div></li>`;
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
    document.body.classList.add('has-results');
    $('#mapLocation').textContent = state.originLabel || 'Your search area';
    const { plan, brief } = b;
    const stops = plan.route.stops;
    const sum = plan.summary;
    const says = Object.fromEntries((brief.steps || []).map((s) => [s.stop, s.say]));
    let html;
    if (!stops.length) {
      html = `<div class="empty-plan"><h2 id="planTitle">${esc(brief.headline)}</h2><p>${esc(brief.summary || '')}</p>
        <button type="button" class="btn-quiet" data-action="wider">Search a wider area</button></div>`;
    } else {
      html = `<h2 class="plan-title" id="planTitle">Your route</h2>
        <div class="facts-row"><span>${stops.length} stop${stops.length > 1 ? 's' : ''}</span><span>${plan.route.total_drive_min} min driving</span><span>${sum.covered ?? sum.in_route} of ${sum.needs} items covered</span>${sum.swapped ? `<span>${sum.swapped} substitute${sum.swapped > 1 ? 's' : ''}</span>` : ''}</div>
        <div id="mapSlot" class="map-slot"></div>
        <details class="route-details"><summary>Pickup route · ${stops.length} stops</summary><ol class="route"><li class="origin"><span class="node" aria-hidden="true"></span><div class="origin-text"><b>Start</b><small>${esc(state.originLabel || '')}${state.originLabel ? ', ' : ''}leave about ${esc(plan.route.leaves)}</small></div></li>
        ${stops.map((s) => `<li class="stop"><span class="node" aria-hidden="true">${s.order}</span><div>
          <h3>${esc(s.store.name)}</h3>
          <div class="where-line"><span><b>${s.leg_min} min</b> drive, ${fmtMi(s.leg_km)}</span><span>Arrive about ${esc(s.arrive)}</span>${s.open_note ? `<span>${esc(s.open_note)}</span>` : ''}${s.store.store_note ? `<span class="warn">${esc(s.store.store_note)}</span>` : ''}</div>
          ${says[s.order] ? `<p class="say">${esc(says[s.order])}</p>` : ''}
          <ul class="tickets">${s.items.map((i) => ticket(i, s.store.id)).join('')}</ul>
          <div class="stop-actions">${directions(s.store)}${reportPanel(s)}</div></div></li>`).join('')}</ol></details>`;
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
    if (state.last?.text) {
      const quote = document.createElement('div');
      quote.className = 'request-quote';
      quote.innerHTML = `<span>You asked</span><p>${esc(state.last.text)}</p>`;
      $('#planTitle', el)?.insertAdjacentElement('afterend', quote);
    }
    $('#r-route').replaceChildren(...$$('.route-details, .notes-grid', el));
    renderChecklist(plan);
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
          ${a.quantity_note ? `<p class="caution"><b>Quantity not confirmed:</b> ${esc(a.quantity_note)}</p>` : ''}
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
    $('#r-actions').innerHTML = `<button type="button" class="btn-quiet" data-action="copy">Copy</button>
      <a class="btn-quiet" data-action="sms" href="#" style="text-decoration:none;display:inline-flex;align-items:center">Text</a>
      <button type="button" class="btn-quiet" data-action="print">Print</button>`;
    $('#r-followup').innerHTML = `<h2 class="section-title">Ask a question</h2>
      <form id="fuForm"><div class="field"><label for="fuQ" class="sr">Your question</label><input id="fuQ" maxlength="400" autocomplete="off" placeholder="Ask about your supplies…"></div>
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
    $('#r-trace').innerHTML = `<details${keepOpen ? ' open' : ''}><summary>Plan details</summary>
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
    mapState.token++;
    mapState.plan = null;
    mapState.cachedRoad = null;
    mapState.line = null;
    mapState.loading = false;
    showOrigin();
    rescueMap();
    ['r-safety', 'r-plan', 'r-checklist', 'r-route', 'r-alts', 'r-items', 'r-understood', 'r-actions', 'r-followup', 'r-trace'].forEach((id) => { $('#' + id).innerHTML = ''; });
    $('#notice').hidden = true;
    $('#saved').hidden = true;
    $('#r-plan').classList.remove('reveal');
  }

  async function run() {
    showWorkspace();
    if (state.running) return;
    const text = $('#text').value.trim();
    const err = $('#formError');
    err.hidden = true;
    if (text.length < 3) { err.textContent = 'Tell us a little about what you need.'; err.hidden = false; $('#text').focus(); return; }
    state.running = true;
    $('#go').disabled = true;
    $('#go').textContent = 'Loading locations...';
    try {
      if (!currentLoc()) await loadMeta();
    } catch (e) {
      err.textContent = 'Could not load locations. Check your connection and press Enter to try again.';
      err.hidden = false;
    }
    const loc = currentLoc();
    if (!loc) {
      state.running = false;
      $('#go').disabled = false;
      $('#go').textContent = 'Find supplies';
      return;
    }
    state.radius = +$('#radius').value;
    state.originLabel = loc.label;
    state.traces = [];
    $('#go').disabled = true;
    $('#go').textContent = 'Working...';
    clearResults();
    document.body.classList.add('has-results');
    ensureMap();
    $('#results').hidden = false;
    resetProgress();
    $('#progress').scrollIntoView({ behavior: scrollBehavior(), block: 'nearest' });
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
      state.last = { savedAt: Date.now(), result: b, intent: u.intent, traces: state.traces, text, radius: state.radius, origin: loc.label, categories: Object.fromEntries((state.meta?.catalog || []).map((item) => [item.id, item.category])) };
      renderPlan(b, true);
      renderAlternatives(b.plan.alternatives);
      renderByItem(b.plan.results, false);
      renderActions();
      renderTrace();
      drawRoute(b.plan);
      safeStore.set(SAVE_KEY, state.last);
      $('#r-plan').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
    } catch (e) {
      const actions = state.last || safeStore.get(SAVE_KEY) ? '<button type="button" class="btn-small" data-action="open-saved">Open last saved plan</button>' : '';
      showNotice(e.message || 'Something went wrong.', 'bad', `<button type="button" class="btn-small" data-action="retry">Try again</button> ${actions}`);
      if (e.data && e.data.intent) { renderSafety(e.data.intent); }
    } finally {
      state.running = false;
      $('#go').disabled = false;
      $('#go').textContent = 'Find supplies';
      $('#progress').hidden = true;
    }
  }

  function openSaved(saved = safeStore.get(SAVE_KEY)) {
    if (state.running) return;
    if (!saved) return;
    showWorkspace();
    clearResults();
    state.radius = saved.radius;
    state.originLabel = saved.origin;
    state.traces = saved.traces || [];
    state.last = saved;
    safeStore.set(SAVE_KEY, saved);
    const loc = saved.result.plan.location;
    addLocation('__saved', { lat: loc.lat, lng: loc.lng, label: saved.origin || 'Saved location' });
    $('#place').value = '__saved';
    $('#radius').value = String(saved.radius);
    $('#progress').hidden = true;
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
    showNotice(`Saved ${new Date(saved.savedAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}. Stock may have changed.`);
  }

  function showSavedPrompt() {
    const saved = safeStore.get(SAVE_KEY);
    if (!saved || !saved.result) return;
    const t = new Date(saved.savedAt).toLocaleString([], { hour: 'numeric', minute: '2-digit', month: 'short', day: 'numeric' });
    const el = $('#saved');
    el.hidden = false;
    el.innerHTML = `<p><b>Saved ${esc(t)}.</b> Available offline.</p><button type="button" class="btn-quiet" data-action="open-saved">Open saved plan</button>`;
  }

  // ---------------------------------------------- Haven checklist and saved-list components
  function supplyIcon(category) {
    const paths = {
      Water: '<path d="M12 3s-7 8-7 12a7 7 0 0 0 14 0c0-4-7-12-7-12Z"/><path d="M8 15a4 4 0 0 0 4 4"/>',
      Food: '<path d="M4 3v6a3 3 0 0 0 6 0V3M7 3v18M18 3c-3 3-4 7-4 9h5M19 3v18"/>',
      'Power and light': '<rect x="2" y="7" width="17" height="10" rx="2"/><path d="M22 10v4M7 10v4"/>',
      Medical: '<path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6Z"/>',
      'Health and safety': '<path d="M12 3 3 7v6c0 5 9 8 9 8s9-3 9-8V7l-9-4Z"/><path d="m8 12 3 3 5-6"/>',
      Pets: '<path d="M7 15c-3 4 1 7 5 5 4 2 8-1 5-5l-3-3c-1-1-3-1-4 0l-3 3Z"/><ellipse cx="5" cy="9" rx="2" ry="3"/><ellipse cx="10" cy="5" rx="2" ry="3"/><ellipse cx="16" cy="5" rx="2" ry="3"/><ellipse cx="21" cy="10" rx="2" ry="3"/>',
    };
    return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[category] || '<path d="m3 7 9-4 9 4v10l-9 4-9-4Z"/><path d="m3 7 9 4 9-4M12 11v10"/>'}</svg>`;
  }

  function categoryFor(id) {
    return state.meta?.catalog?.find((item) => item.id === id)?.category || state.last?.categories?.[id] || 'Supplies';
  }

  function updateChecklistCount() {
    const boxes = $$('#r-checklist [data-collected]');
    const label = $('#collectedCount');
    if (label) label.textContent = `${boxes.filter((box) => box.checked).length}/${boxes.length} pickups collected`;
  }

  function renderChecklist(plan) {
    const collected = new Set(state.last?.collected || []);
    const rows = plan.route.stops.flatMap((stop) => stop.items.map((item) => ({
      ...item, store: stop.store, category: categoryFor(item.item_id), pickupKey: `${stop.store.id}:${item.item_id}`,
    })));
    const missing = plan.unresolved.map((item) => ({ ...item, category: categoryFor(item.item_id), unavailable: true }));
    const allRows = [...rows, ...missing];
    const categories = ['All supplies', ...new Set(allRows.map((item) => item.category))];
    $('#r-checklist').innerHTML = `<div class="checklist-heading"><h2>Your supply checklist <span>${allRows.length}</span></h2><span id="collectedCount"></span></div>
      <div class="category-tabs" role="group" aria-label="Filter supplies">${categories.map((category, index) => `<button type="button" data-action="category" data-category="${esc(category)}" aria-pressed="${index === 0}" class="${index === 0 ? 'active' : ''}">${esc(category)}</button>`).join('')}</div>
      <div class="supply-list">${allRows.map((item) => {
        const checked = collected.has(item.pickupKey);
        return `<div class="supply-row ${checked ? 'collected' : ''}" data-category="${esc(item.category)}">
          <label class="collect-check"><input type="checkbox" ${item.unavailable ? 'disabled' : `data-collected="${esc(item.pickupKey)}"`} aria-label="Mark ${esc(item.name)} collected${item.store ? ` at ${esc(item.store.name)}` : ''}" ${checked ? 'checked' : ''}></label>
          <span class="supply-icon" aria-hidden="true">${supplyIcon(item.category)}</span>
          <div class="supply-name"><b>${esc(item.name)}</b><small>${item.qty} unit${item.qty === 1 ? '' : 's'}${item.is_alt ? '<span class="alt-label">Alternative</span>' : ''}${item.store ? ` · ${esc(item.store.name)}` : ''}</small></div>
          <span class="stock-label ${item.unavailable ? 'out' : item.low ? 'low' : 'available'}">${item.unavailable ? 'Not in route' : item.low ? 'Limited stock' : 'In stock'}<span class="stock-dot"></span></span></div>`;
      }).join('') || '<p class="empty-checklist">No supplies in this plan.</p>'}</div>
      <button type="button" class="alternative-card" data-action="alternatives"><span class="alt-icon" aria-hidden="true">✦</span><b>Find alternatives</b><span aria-hidden="true">↗</span></button>`;
    updateChecklistCount();
  }

  function readLists() {
    const records = safeStore.get(LISTS_KEY);
    return Array.isArray(records) ? records.filter((record) => record?.id && record?.snapshot?.result?.plan) : [];
  }

  function updateSavedCount() {
    const count = readLists().length;
    $('#savedCount').textContent = String(count);
    $('#savedCount').hidden = !count;
  }

  function showWorkspace() {
    $('.shell').hidden = false;
    $('#savedPage').hidden = true;
    $('.nav-link[data-action="new-request"]').classList.add('active');
    $('.nav-link[data-action="saved-list"]').classList.remove('active');
    if (mapState.map) requestAnimationFrame(fitMap);
  }

  function saveList() {
    if (!state.last || state.running) return;
    const records = readLists();
    const existing = records.find((record) => record.snapshot.text === state.last.text && record.snapshot.origin === state.last.origin && record.snapshot.radius === state.last.radius);
    const record = { id: existing?.id || crypto.randomUUID(), snapshot: JSON.parse(JSON.stringify(state.last)) };
    if (safeStore.set(LISTS_KEY, [record, ...records.filter((entry) => entry.id !== record.id)])) {
      updateSavedCount();
      toast(existing ? 'Supply list updated.' : 'Supply list saved.');
    } else toast('Your browser could not save this list. Try downloading it.');
  }

  function showSavedLists() {
    if (state.running) { toast('Your plan is still being prepared.'); return; }
    const records = readLists();
    $('.shell').hidden = true;
    $('#savedPage').hidden = false;
    $('.nav-link[data-action="new-request"]').classList.remove('active');
    $('.nav-link[data-action="saved-list"]').classList.add('active');
    $('#savedLists').innerHTML = records.length ? records.map((record) => {
      const snapshot = record.snapshot;
      const count = snapshot.result.plan.route.stops.reduce((total, stop) => total + stop.items.length, 0);
      return `<article class="saved-card"><div class="saved-card-top"><span class="scenario-icon" aria-hidden="true">${supplyIcon('Supplies')}</span><button type="button" class="icon-btn" data-action="delete-list" data-id="${esc(record.id)}" aria-label="Delete ${esc(snapshot.text)}">×</button></div><small>${esc(new Date(snapshot.savedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }))}</small><h2>${esc(snapshot.origin || 'Supply list')}</h2><p>${esc(snapshot.text)}</p><p>${count} pickups · ${snapshot.result.plan.route.stops.length} stops</p><button type="button" class="text-button" data-action="open-list" data-id="${esc(record.id)}">Open supply list ↗</button></article>`;
    }).join('') : `<div class="empty-saved"><div aria-hidden="true">♧</div><h2>No saved lists</h2><p>Save a supply plan to see it here.</p><button type="button" class="primary" data-action="new-request">Create a plan ↗</button>${safeStore.get(SAVE_KEY) ? '<button type="button" class="text-button" data-action="open-saved">Open last automatic save</button>' : ''}</div>`;
    updateSavedCount();
    $('#savedTitle').setAttribute('tabindex', '-1');
    $('#savedTitle').focus();
  }

  // ---------------------------------------------------------------- events
  document.addEventListener('fullscreenchange', () => {
    const expanded = !!document.fullscreenElement;
    $('#expandMap').setAttribute('aria-label', expanded ? 'Exit fullscreen map' : 'Expand map');
    $('#expandMap').title = expanded ? 'Exit fullscreen map' : 'Expand map';
    requestAnimationFrame(fitMap);
  });
  document.addEventListener('change', (e) => {
    const checkbox = e.target.closest('[data-collected]');
    if (!checkbox || !state.last) return;
    const collected = new Set(state.last.collected || []);
    if (checkbox.checked) collected.add(checkbox.dataset.collected);
    else collected.delete(checkbox.dataset.collected);
    state.last.collected = [...collected];
    checkbox.closest('.supply-row').classList.toggle('collected', checkbox.checked);
    safeStore.set(SAVE_KEY, state.last);
    const records = readLists();
    const matching = records.find((record) => record.snapshot.text === state.last.text && record.snapshot.origin === state.last.origin && record.snapshot.radius === state.last.radius);
    if (matching) { matching.snapshot = JSON.parse(JSON.stringify(state.last)); safeStore.set(LISTS_KEY, records); }
    updateChecklistCount();
  });
  $('#form').addEventListener('submit', (e) => { e.preventDefault(); run(); });

  // Enter searches; Shift+Enter makes a new line. Do not fire while an input method (Japanese,
  // Chinese, Korean...) is still choosing characters, because Enter confirms those.
  let shiftEnter = false;
  $('#text').addEventListener('keydown', (e) => {
    shiftEnter = e.key === 'Enter' && e.shiftKey;
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); run(); }
  });
  // Chrome on Android can send only beforeinput for the virtual search/Enter key.
  $('#text').addEventListener('keyup', () => { shiftEnter = false; });
  $('#text').addEventListener('blur', () => { shiftEnter = false; });
  $('#text').addEventListener('beforeinput', (e) => {
    if ((e.inputType === 'insertLineBreak' || e.inputType === 'insertParagraph') && !e.isComposing && !shiftEnter) {
      e.preventDefault(); run();
    }
  });

  $$('[data-example]').forEach((b) => b.addEventListener('click', () => {
    const ex = EXAMPLES[+b.dataset.example];
    $('#text').value = ex.text;
    const p = state.meta && state.meta.places.find((x) => x.label.startsWith(ex.place));
    if (p) $('#place').value = p.id;
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
    else if (act === 'retry-road' && mapState.plan && !mapState.loading) loadRoad(mapState.plan, mapState.line);
    else if (act === 'open-saved') openSaved();
    else if (act === 'saved-list') showSavedLists();
    else if (act === 'save-list') saveList();
    else if (act === 'open-list') {
      const saved = readLists().find((record) => record.id === t.dataset.id);
      if (saved) openSaved(saved.snapshot);
    }
    else if (act === 'delete-list') {
      if (safeStore.set(LISTS_KEY, readLists().filter((record) => record.id !== t.dataset.id))) { showSavedLists(); toast('Supply list removed.'); }
      else toast('Your browser could not remove this list.');
    }
    else if (act === 'category') {
      $$('#r-checklist .category-tabs button').forEach((button) => { const active = button === t; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
      $$('#r-checklist .supply-row').forEach((row) => { row.hidden = t.dataset.category !== 'All supplies' && row.dataset.category !== t.dataset.category; });
    }
    else if (act === 'alternatives') {
      if ($('#r-alts').textContent.trim()) $('#r-alts').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
      else toast('Your plan has no unavailable supplies needing alternatives.');
    }
    else if (act === 'settings') {
      $('#mapsConnection').textContent = $('#map').dataset.provider === 'google' ? 'Connected' : state.meta?.maps?.apiKey ? ($('#map').dataset.provider ? 'Using backup map' : 'Connecting…') : 'Not configured';
      $('#aiConnection').textContent = state.traces.some((trace) => trace?.mode === 'bedrock') ? 'Connected' : 'Local planner';
      $('#settingsDialog').showModal();
    }
    else if (act === 'close-settings') $('#settingsDialog').close();
    else if (act === 'new-request') {
      if (state.running) { toast('Your plan is still being prepared.'); return; }
      showWorkspace();
      clearResults();
      document.body.classList.remove('has-results');
      $('#results').hidden = true;
      $('#progress').hidden = true;
      $('#formError').hidden = true;
      $('#text').value = '';
      showSavedPrompt();
      $('#text').focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: scrollBehavior() });
    }
    else if (act === 'recenter-map') {
      if (mapState.bounds) fitMap();
      else { const loc = currentLoc(); if (loc && mapState.map) mapState.map.setView([loc.lat, loc.lng], 13); }
    }
    else if (act === 'expand-map') {
      try {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await $('.col-map').requestFullscreen();
      } catch (error) { toast('Fullscreen is unavailable in this browser.'); }
    }
    else if (act === 'how' || act === 'about') $('#howDialog').showModal();
    else if (act === 'close-how') $('#howDialog').close();
    else if (act === 'download') {
      if (!state.last) return;
      const collected = state.last.collected || [];
      const checklist = state.last.result.plan.route.stops.flatMap((stop) => stop.items.map((i) =>
        `${collected.includes(`${stop.store.id}:${i.item_id}`) ? '[x]' : '[ ]'} ${i.name} x${i.qty} · ${stop.store.name}`));
      const text = state.last.result.plan_text + '\n\nSUPPLY CHECKLIST\n' + checklist.join('\n');
      const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = url; link.download = 'haven-supply-plan.txt'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast('Supply plan downloaded.');
    }
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
  let metaRequest = null;
  function loadMeta() {
    if (state.meta) return Promise.resolve(state.meta);
    if (!metaRequest) {
      metaRequest = (async () => {
        const { res, data: meta } = await fetchJson('/api/meta', { cache: 'no-store' }, 12000);
        if (!res.ok) throw new Error('Could not load locations.');
        if (!Array.isArray(meta.places) || !meta.places.length) throw new Error('No locations available.');
        state.meta = meta;
        setupPlaces(meta.places);
        if (meta.data && meta.data.demo) $('#dataBadge').title = meta.data.note;
        return meta;
      })().finally(() => { metaRequest = null; });
    }
    return metaRequest;
  }
  (async function boot() {
    try {
      await loadMeta();
    } catch (e) {
      showNotice('Could not load the app data. Check your connection.', 'bad');
      showSavedPrompt();
      return;
    }
    showSavedPrompt();
    updateSavedCount();
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { /* optional */ });
  })();
})();
