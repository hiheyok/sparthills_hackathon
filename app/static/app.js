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
  const fmtMin = (min) => { const m = Math.max(1, Math.round(min)); return m < 90 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ''}`; };
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

  // Two lanes, one per way of getting there. Each has its own plan, briefing agent and path, and
  // they are built at the same time. state.mode is the lane on screen.
  const MODE_KEY = 'supplyline.mode';
  const MODES = { driving: { label: 'Drive', verb: 'drive', ing: 'driving' }, walking: { label: 'Walk', verb: 'walk', ing: 'walking' } };
  const state = { meta: null, customLocations: {}, radius: 8, traces: [], last: null, running: false,
    mode: safeStore.get(MODE_KEY) === 'walking' ? 'walking' : 'driving', lanes: {}, runId: 0, intent: null, origin: null,
    startedAt: 0, wallMs: 0 };
  const newLane = () => ({ status: 'working', result: null, alternatives: [], traces: [], error: '',
    road: null, roadState: 'idle', roadPromise: null, roadFailure: '' });

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
  // The map goes through the HavenMaps adapter (maps.js): Google Maps when a key is configured,
  // otherwise OpenStreetMap/Leaflet. Planning never depends on the map SDK, and a broken or
  // unauthorized map falls back without blocking the plan, the route list or the saved plan.
  const mapState = { adapter: null, creating: null, straight: null, token: 0, plan: null, line: null,
    loading: false, fitPoints: null, maxZoom: 14, mode: 'driving' };

  // Create the adapter once. Returns a promise of the adapter, or null if no map backend is available.
  function ensureMapAsync() {
    if (mapState.adapter) return Promise.resolve(mapState.adapter);
    if (mapState.creating) return mapState.creating;
    const el = $('#map');
    if (!window.HavenMaps) { el.textContent = 'The map could not load. Directions links still work.'; return Promise.resolve(null); }
    el.classList.add('has-map');
    const maps = (state.meta && state.meta.maps) || {};
    const warn = $('#mapWarn');
    mapState.creating = HavenMaps.create(el, { apiKey: maps.apiKey, mapId: maps.mapId }, {
      onFallback: () => { /* Google failed; Leaflet (OpenStreetMap) is used instead. */ },
      onTileError: () => { if (warn) warn.hidden = false; },        // offline or blocked: the route is still drawn
    }).then((adapter) => {
      mapState.adapter = adapter;
      if (warn && adapter && adapter.provider === 'google') warn.hidden = true;
      return adapter;
    }).catch(() => null).finally(() => { mapState.creating = null; });
    return mapState.creating;
  }

  // Synchronous accessor for callers that only touch the map when it already exists.
  const currentAdapter = () => mapState.adapter;


  // On wide screens the map is a sticky column beside the results. On phones a map at the very
  // bottom of a long page is useless, so it moves up to sit right under the plan summary.
  const wide = window.matchMedia('(min-width: 980px)');
  function rescueMap() {
    const aside = $('.col-map'), shell = $('.shell');
    if (aside && aside.parentNode !== shell) shell.appendChild(aside);
  }
  // Put the map where this window width wants it. Safe to repeat.
  function seatMap() {
    const aside = $('.col-map'), slot = $('#mapSlot');
    if (!aside) return;
    if (!wide.matches && slot) { if (aside.parentNode !== slot) slot.appendChild(aside); } else rescueMap();
  }
  function placeMap() {
    seatMap();
    if (mapState.adapter) setTimeout(fitMap, 60);
  }
  wide.addEventListener('change', placeMap);
  window.addEventListener('resize', () => requestAnimationFrame(fitMap));

  // A map nobody can see has no size. The SDK clips the whole route away when it measures one, and the
  // route stays gone until the map is measured again, so a hidden map is never measured.
  const mapEl = () => $('#map');
  const mapShown = () => { const el = mapEl(); return !!mapState.adapter && !!el && el.offsetWidth > 0 && el.offsetHeight > 0; };

  function fitMap() {
    if (!mapState.adapter) return;
    // Narrowing the window hides the map (CSS) until it is moved into the plan. The resize and the
    // media-query change can reach the page in either order, so do not wait for the second one.
    seatMap();
    if (!mapShown()) return;
    mapState.adapter.resize();
    if (mapState.fitPoints && mapState.fitPoints.length > 1) mapState.adapter.fit(mapState.fitPoints, mapState.maxZoom);
  }

  function showOrigin() {
    // Form edits describe the next search; the map still belongs to the displayed plan.
    if (mapState.plan) return;
    const loc = currentLoc();
    if (!loc) return;
    ensureMapAsync().then((adapter) => {
      if (!adapter || mapState.plan) return;
      mapState.token++;                               // a road route still loading belongs to the old view
      mapState.fitPoints = null;
      $('#mapLink').hidden = true;
      $('#mapNote').textContent = 'Pick where you are and your route appears here.';
      adapter.clear();
      adapter.marker([loc.lat, loc.lng], { kind: 'pin-you' });
      adapter.setView([loc.lat, loc.lng], 12);
      setTimeout(() => { if (mapShown()) adapter.resize(); }, 50);
    });
  }
  $('#radius').addEventListener('change', showOrigin);

  // One tap to turn-by-turn with live traffic: the whole trip, in plan order, in Google Maps.
  function googleRouteUrl(plan) {
    const loc = plan.location, pts = plan.route.stops.map((s) => `${s.store.lat},${s.store.lng}`);
    const via = pts.slice(0, -1);
    return `https://www.google.com/maps/dir/?api=1&travelmode=${plan.mode === 'walking' ? 'walking' : 'driving'}&origin=${loc.lat},${loc.lng}&destination=${pts[pts.length - 1]}`
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

  // Driving and walking each have a path. Each lane asks for its own once and shares the answer. The lane
  // on screen is asked first; the other waits until that one has settled, so the person looking at the map
  // is never queued behind the one they are not (and the free routing servers see one request at a time).
  const modeOf = (plan) => (plan && plan.mode === 'walking' ? 'walking' : 'driving');
  const lanePoints = (plan) => [[plan.location.lat, plan.location.lng]].concat(plan.route.stops.map((s) => [s.store.lat, s.store.lng]));
  const ROAD_STYLE = {
    driving: { color: '#0a4da2', dash: null, name: 'driving', note: 'Fastest road route', source: 'OpenStreetMap roads, no live traffic' },
    walking: { color: '#0b6e4c', dash: '1 9', name: 'walking', note: 'Walking route', source: 'OpenStreetMap footpaths and roads, no live conditions' },
  };

  function requestRoad(lane, plan, runId) {
    if (lane.roadPromise) return lane.roadPromise;
    const mode = modeOf(plan);
    lane.roadState = 'loading';
    lane.roadPromise = (async () => {
      let road = null, failure = 'Road directions are unavailable right now.';
      try {
        const r = await post('/api/route', { points: lanePoints(plan).map(([lat, lng]) => ({ lat, lng })), mode });
        road = r.available && validRoad(r) ? r : null;
        if (!road) failure = r.reason || 'The routing service returned an invalid road path.';
      } catch (e) { failure = e.message || failure; }
      if (runId === state.runId) {                                  // a newer search owns the screen now
        lane.road = road;
        lane.roadFailure = road ? '' : failure;
        lane.roadState = road ? 'done' : 'failed';
        if (road) persist();
      }
      return { road, failure };
    })();
    return lane.roadPromise;
  }

  function prefetchRoads(runId) {
    if (runId !== state.runId) return;
    const shown = state.lanes[state.mode];
    if (shown && shown.status === 'working') return;               // the lane on screen is still being built: it asks first
    if (shown && shown.status === 'ready' && shown.result.plan.route.stops.length && shown.roadState !== 'done' && shown.roadState !== 'failed') return;
    for (const m of Object.keys(MODES)) {
      const lane = state.lanes[m];
      if (lane && lane.status === 'ready' && lane.roadState === 'idle' && lane.result.plan.route.stops.length) {
        requestRoad(lane, lane.result.plan, runId).then(() => prefetchRoads(runId));
        return;                                                     // one at a time
      }
    }
  }

  async function loadRoad(plan, line, lane) {
    const token = mapState.token;
    const style = ROAD_STYLE[modeOf(plan)];
    mapState.loading = true;
    $('#mapNote').textContent = `Finding the ${modeOf(plan) === 'walking' ? 'best walking' : 'fastest road'} route...`;
    let road = validRoad(lane.road) ? lane.road : null;
    let failure = lane.roadFailure || 'Road directions are unavailable right now.';
    if (!road) ({ road, failure } = await requestRoad(lane, plan, state.runId));
    const adapter = currentAdapter();
    if (token !== mapState.token || !adapter) return;          // a newer search replaced this plan
    mapState.loading = false;
    const note = $('#mapNote');
    if (!road) {
      note.innerHTML = `${esc(failure)} The dashed line only connects stops; it is not a ${style.name} route. Use Directions or <button type="button" class="btn-small" data-action="retry-road">Retry ${modeOf(plan) === 'walking' ? 'walking' : 'road'} route</button>.`;
      prefetchRoads(state.runId);
      return;
    }
    // White casing under the coloured route so it reads over any basemap.
    adapter.line(road.path, { color: '#ffffff', weight: 10 });
    adapter.line(road.path, { color: style.color, weight: 5, dashed: !!style.dash });
    if (mapState.straight) { mapState.straight.remove(); mapState.straight = null; }
    mapState.fitPoints = road.path.concat(line);
    mapState.maxZoom = 15;
    fitMap();
    // The routing services ask to see the OpenStreetMap credit and a link for fixing the map wherever their routes are shown.
    note.innerHTML = `${esc(style.note)}: about ${esc(fmtMin(road.duration_min))}, ${esc(fmtMi(road.distance_km))} (${esc(style.source)}). Small circles are other stores with stock. `
      + '<a href="https://www.openstreetmap.org/fixthemap" target="_blank" rel="noopener">Fix the map</a>.';
    prefetchRoads(state.runId);
  }

  async function drawRoute(plan, lane) {
    const loc = plan.location;
    const color = ROAD_STYLE[modeOf(plan)].color;
    const adapter = await ensureMapAsync();
    if (!adapter) return;
    // If a newer selection replaced the plan while the adapter was being created the first time,
    // the lane now on screen no longer points at this plan: leave the screen to whoever owns it.
    const shown = state.lanes[state.mode];
    if (shown && shown.result && shown.result.plan !== plan) return;
    mapState.token++;
    mapState.plan = plan;
    mapState.mode = modeOf(plan);
    adapter.clear();
    const bounds = [[loc.lat, loc.lng]];
    const inRoute = new Set(plan.route.stops.map((s) => s.store.id));
    const seen = new Set();
    plan.results.forEach((r) => r.stores.forEach((s) => {
      if (inRoute.has(s.id) || seen.has(s.id)) return;
      seen.add(s.id);
      adapter.marker([s.lat, s.lng], { kind: 'pin-alt', title: s.name });
    }));
    const line = [[loc.lat, loc.lng]];
    mapState.line = line;
    plan.route.stops.forEach((s) => {
      line.push([s.store.lat, s.store.lng]);
      bounds.push([s.store.lat, s.store.lng]);
      adapter.marker([s.store.lat, s.store.lng], { label: String(s.order), title: `${s.order}. ${s.store.name}` });
    });
    mapState.straight = line.length > 1
      ? adapter.line(line, { color, weight: 4, dashed: true }) : null;
    adapter.marker([loc.lat, loc.lng], { kind: 'pin-you' });
    mapState.fitPoints = bounds.length > 1 ? bounds : null;
    mapState.maxZoom = 14;
    fitMap();
    if (bounds.length === 1) adapter.setView([loc.lat, loc.lng], 12);
    const link = $('#mapLink');
    if (plan.route.stops.length) {
      link.href = googleRouteUrl(plan);
      link.hidden = false;
      $('#mapNote').textContent = `Finding the ${modeOf(plan) === 'walking' ? 'best walking' : 'fastest road'} route...`;
      loadRoad(plan, line, lane);
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
      ? `<div class="safety" role="alert"><h2>Call 911 first if anyone is in danger</h2><p>${esc(us.reason || 'This may be an emergency.')}</p><p>Get everyone to safety before you go out for supplies. Your plan is below when you are ready.</p></div>`
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

  function storeLine(s, mode) {
    const f = freshness(s);
    return `<div class="store"><strong>${esc(s.name)}</strong>
      <div class="facts"><span>${fmtMin(s.eta_min)} ${MODES[mode || 'driving'].verb}</span><span>${fmtMi(s.dist_km)}</span>${s.open_note ? `<span>${esc(s.open_note)}</span>` : ''}
      <span class="${f.cls}">${esc(f.text)}</span>${s.low ? '<span class="warn">Low stock</span>' : ''}${s.store_note ? `<span class="warn">${esc(s.store_note)}</span>` : ''}</div></div>`;
  }

  function gapText(r, mode, radiusKm) {
    const st = r.stats || {};
    let t = `Nothing in stock at the ${st.checked || 0} stores within ${fmtMi(radiusKm || state.radius)}`;
    const parts = [];
    if (st.out) parts.push(`${st.out} sold out`);
    if (st.closed) parts.push(`${st.closed} closed`);
    if (st.no_item) parts.push(`${st.no_item} do not carry it`);
    if (parts.length) t += ` (${parts.join(', ')})`;
    t += '.';
    if (r.nearest_any) t += ` Nearest with stock: ${r.nearest_any.name}, ${fmtMi(r.nearest_any.dist_km)} away, about ${fmtMin(r.nearest_any.eta_min)}${mode === 'walking' ? ' on foot' : ''}.`;
    return t;
  }

  function renderByItem(results, open, mode, radiusKm) {
    $('#r-items').innerHTML = `<details ${open ? 'open' : ''}><summary>Every item, store by store</summary>` + results.map((r) => {
      const chip = r.status === 'found' ? ['In stock nearby', 'ok'] : r.status === 'partial' ? ['Limited stock', 'warn'] : ['Not available nearby', 'bad'];
      return `<div class="item-block"><div class="item-head"><h4>${esc(r.label)}${r.qty > 1 ? ` x${r.qty}` : ''}</h4>
        <span class="chip ${chip[1]}">${chip[0]}</span>${r.inferred ? '<span class="tag ai" title="You did not say this, but the plan needs it">Inferred</span>' : ''}</div>
        ${r.status === 'gap' ? `<p class="gap-text">${esc(gapText(r, mode, radiusKm))}</p>` : r.stores.slice(0, 3).map((s) => storeLine(s, mode)).join('')}</div>`;
    }).join('') + '</details>';
  }

  function directions(s, mode) {
    const q = `${s.lat},${s.lng}`, walking = mode === 'walking';
    return `<a class="btn-small" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=${q}&travelmode=${walking ? 'walking' : 'driving'}">Directions</a>
            <a class="btn-small" target="_blank" rel="noopener" href="https://maps.apple.com/?daddr=${q}&dirflg=${walking ? 'w' : 'd'}">Apple Maps</a>`;
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
        <div class="facts-row"><span>${stops.length} stop${stops.length > 1 ? 's' : ''}</span><span>${fmtMin(plan.route.total_travel_min ?? plan.route.total_drive_min)} ${ROAD_STYLE[modeOf(plan)].name}</span><span>${sum.covered ?? sum.in_route} of ${sum.needs} items covered</span>${sum.swapped ? `<span>${sum.swapped} substitute${sum.swapped > 1 ? 's' : ''}</span>` : ''}</div>
        <div id="mapSlot" class="map-slot"></div>
        <ol class="route"><li class="origin"><span class="node" aria-hidden="true"></span><div class="origin-text"><b>You are here</b><small>${esc(state.originLabel || '')}${state.originLabel ? ', ' : ''}leave about ${esc(plan.route.leaves)}</small></div></li>
        ${stops.map((s) => `<li class="stop"><span class="node" aria-hidden="true">${s.order}</span><div>
          <h3>${esc(s.store.name)}</h3>
          <div class="where-line"><span><b>${fmtMin(s.leg_min)}</b> ${MODES[modeOf(plan)].verb}, ${fmtMi(s.leg_km)}</span><span>Arrive about ${esc(s.arrive)}</span>${s.open_note ? `<span>${esc(s.open_note)}</span>` : ''}${s.store.store_note ? `<span class="warn">${esc(s.store.store_note)}</span>` : ''}</div>
          ${says[s.order] ? `<p class="say">${esc(says[s.order])}</p>` : ''}
          <ul class="tickets">${s.items.map(ticket).join('')}</ul>
          <div class="stop-actions">${directions(s.store, modeOf(plan))}${reportPanel(s)}</div></div></li>`).join('')}</ol>`;
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

  function renderAlternatives(alts, mode, radiusKm) {
    const el = $('#r-alts');
    if (!alts || !alts.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<h2 class="section-title">Not in stock nearby</h2>
      <p class="section-sub">For these, we looked at what is in stock near you and picked the closest safe match. Check the notes before you swap.</p>` +
      alts.map((a) => {
        const noSub = a.verdict === 'no_safe_substitute';
        const head = noSub ? `No safe substitute for ${a.for_name.toLowerCase()}` : a.options.length ? `Nobody near you has ${a.for_name.toLowerCase()}. Try instead:` : `Nobody near you has ${a.for_name.toLowerCase()}, and nothing else fits safely`;
        const st = a.stats || {};
        const sub = `${st.checked || 0} stores checked within ${fmtMi(radiusKm || state.radius)}` + (a.nearest_any ? `. Nearest with stock: ${a.nearest_any.name}, ${fmtMi(a.nearest_any.dist_km)} away${mode === 'walking' ? ` (${fmtMin(a.nearest_any.eta_min)} on foot)` : ''}.` : '. None have it in stock.');
        return `<article class="sign ${noSub ? 'no-sub' : ''}"><div class="sign-head"><h3>${esc(head)}</h3><p>${esc(sub)}</p></div><div class="sign-body">
          ${a.quantity_note ? `<p class="caution"><b>Quantity not confirmed:</b> ${esc(a.quantity_note)}</p>` : ''}
          ${a.options.map((o) => `<div class="opt ${esc(o.fit)}"><h4>${esc(o.name)}<span class="fit">${esc(FIT_TEXT[o.fit] || '')}</span></h4>
            ${o.why ? `<p>${esc(o.why)}</p>` : ''}${o.how_to_use ? `<p>${esc(o.how_to_use)}</p>` : ''}
            ${o.caution ? `<p class="caution"><b>Check:</b> ${esc(o.caution)}</p>` : ''}
            ${o.stores && o.stores[0] ? `<p class="near">${esc(o.stores[0].name)}, ${fmtMin(o.stores[0].eta_min)}, ${fmtMi(o.stores[0].dist_km)}</p>` : ''}</div>`).join('')}
          ${a.no_purchase_tips && a.no_purchase_tips.length ? `<div><h5>Without buying anything</h5><ul>${a.no_purchase_tips.map((t) => `<li>${esc(t)}</li>`).join('')}</ul></div>` : ''}
          ${a.escalate ? `<div class="next-step"><b>Next step</b>${esc(a.escalate)}</div>` : ''}
        </div></article>`;
      }).join('');
  }

  function renderActions() {
    $('#r-actions').innerHTML = `<button type="button" class="btn-quiet" data-action="copy">Copy plan as text</button>
      <a class="btn-quiet" data-action="sms" href="#" style="text-decoration:none;display:inline-flex;align-items:center">Text this plan</a>
      <button type="button" class="btn-quiet" data-action="print">Print</button>`;
  }

  // The assistant has its own agent and reads the person's situation on the server, so it can answer as
  // soon as the request is understood, without waiting for either plan. Never rebuilt once it exists,
  // so a question and answer in progress are not wiped when a plan arrives.
  function renderAsk() {
    if ($('#fuForm')) return;
    $('#r-followup').innerHTML = `<h2 class="section-title">Ask about your situation</h2>
      <form id="fuForm"><div class="field"><label for="fuQ" class="sr">Your question</label><input id="fuQ" maxlength="400" autocomplete="off" placeholder="Should I walk or drive? Will the pharmacy still be open?"></div>
      <button class="btn-quiet" type="submit">Ask</button></form><div id="fuAnswer" aria-live="polite"></div>`;
  }

  function renderTrace() {
    const rows = state.traces.filter(Boolean).map((t) => {
      const task = { understand: 'Read your request', alternatives: 'Find alternatives', brief: 'Write your briefing', followup: 'Answer a question' }[t.stage] || t.stage;
      const name = t.lane && MODES[t.lane] ? `${task}, ${MODES[t.lane].ing}` : task;
      const by = t.mode === 'bedrock' ? 'Amazon Bedrock (Converse API)' : 'Rules fallback';
      const note = t.mode === 'bedrock'
        ? (t.fell_back_from && t.fell_back_from.length ? `Used after ${t.fell_back_from.map(shortModel).join(', ')} was unavailable.` : '')
        : (t.reason || '');
      return `<tr><td>${esc(name)}</td><td>${esc(by)}${note ? `<br><small>${esc(note)}</small>` : ''}</td><td>${esc(t.mode === 'bedrock' ? shortModel(t.model) : 'none')}</td>
        <td class="num">${(t.ms / 1000).toFixed(1)}s</td><td class="num">${t.tokens_in} / ${t.tokens_out}</td></tr>`;
    }).join('');
    const keepOpen = $('#r-trace details') && $('#r-trace details').open;   // do not collapse it while someone is reading it
    const work = state.traces.filter((t) => t && t.stage !== 'followup');
    const sum = work.reduce((a, t) => a + (t.ms || 0), 0);
    const wall = state.startedAt ? Math.round(state.finishedAt - state.startedAt) : 0;
    const overlap = wall > 0 && sum > wall * 1.1
      ? `<p class="trace-note"><b>Done in ${(wall / 1000).toFixed(1)}s.</b> The steps below add up to ${(sum / 1000).toFixed(1)}s because the driving and walking plans were built at the same time.</p>` : '';
    $('#r-trace').innerHTML = `<details${keepOpen ? ' open' : ''}><summary>Under the hood: what Bedrock did</summary>
      <p class="trace-note">Stock, distance, opening hours and routing are plain code, so they are repeatable. Amazon Bedrock does the language work, and each task has its own agent: one reads your request, one finds substitutes and one writes the briefing for each way of getting there, and one answers your questions. Driving and walking each get their own, running together. Agents can only choose catalog items, and substitutes only from items confirmed in stock near you. Prescriptions are never substituted.</p>
      ${overlap}
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
    mapState.line = null;
    mapState.loading = false;
    state.lanes = {};
    showOrigin();
    rescueMap();
    ['r-safety', 'r-mode', 'r-plan', 'r-alts', 'r-items', 'r-understood', 'r-actions', 'r-followup', 'r-trace'].forEach((id) => { $('#' + id).innerHTML = ''; });
    $('#notice').hidden = true;
    $('#saved').hidden = true;
    $('#r-plan').classList.remove('reveal');
  }

  // ----------------------------------------------------------------- lanes
  // Driving and walking are two lanes. Each is built by its own agents (substitutes, then briefing) and has
  // its own path, and they run at the same time. A run id lets a lane that is still working for an older
  // search finish quietly without touching the screen.
  const laneOf = (m) => state.lanes[m] || null;
  const activeResult = () => { const l = laneOf(state.mode); return (l && l.result) || (state.last && state.last.result) || null; };
  const slimAlts = (list) => (list || []).map((a) => ({ for_item: a.for_item, options: (a.options || []).map((o) => ({ item_id: o.item_id })) }));

  // Both lanes go to the device, so a saved plan works offline for walking and for driving.
  function persist() {
    if (!state.last || state.last.runId !== state.runId) return;     // the saved plan belongs to an older search
    const lanes = {};
    Object.keys(MODES).forEach((m) => {
      const l = laneOf(m);
      if (l && l.status === 'ready') lanes[m] = { result: l.result, alternatives: l.alternatives, road: l.road };
    });
    const primary = lanes.driving || lanes[state.mode] || lanes.walking;
    if (!primary) return;
    state.last = { ...state.last, result: primary.result, road: primary.road, lanes, mode: state.mode, traces: state.traces };
    safeStore.set(SAVE_KEY, state.last);
  }

  // A small three-dot "thinking" animation. Keeps a screen-reader word so the state is announced
  // and so tests can tell a building lane from a ready one.
  const DOTS = '<span class="dots" role="status" aria-label="working"><span class="sr">working</span><i></i><i></i><i></i></span>';

  function laneSummary(m) {
    const l = laneOf(m);
    if (!l || l.status === 'working') return DOTS;
    if (l.status === 'error') return esc('unavailable');
    const route = l.result.plan.route, n = route.stops.length;
    return esc(n ? `${fmtMin(route.total_travel_min ?? route.total_drive_min)}, ${n} stop${n > 1 ? 's' : ''}` : 'nothing in range');
  }

  function renderModeSwitch() {
    const el = $('#r-mode');
    if (!Object.keys(state.lanes).length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="mode-switch" role="group" aria-label="How are you getting there">${Object.keys(MODES).map((m) =>
      `<button type="button" class="mode-btn" data-action="mode" data-mode="${m}" aria-pressed="${state.mode === m}">
        <span class="mode-name">${MODES[m].label}</span><span class="mode-sub">${laneSummary(m)}</span></button>`).join('')}</div>`;
  }

  // Put the chosen lane on screen: its plan, substitutes, store lists and path.
  function showLane(reveal) {
    renderModeSwitch();
    const lane = laneOf(state.mode);
    if (!lane) return;
    if (lane.status === 'ready') {
      const plan = lane.result.plan, mode = modeOf(plan);
      renderPlan(lane.result, reveal);
      renderAlternatives(plan.alternatives, mode, plan.location.radius_km);
      renderByItem(plan.results, false, mode, plan.location.radius_km);
      drawRoute(plan, lane);
      return;
    }
    rescueMap();                                     // the map may sit inside the section that is about to be replaced
    mapState.token++;
    mapState.plan = null;
    mapState.line = null;
    mapState.loading = false;
    showOrigin();                                    // not the other lane's route
    ['r-alts', 'r-items'].forEach((id) => { $('#' + id).innerHTML = ''; });
    const word = MODES[state.mode].ing;
    $('#r-plan').classList.remove('reveal');
    $('#r-plan').innerHTML = lane.status === 'error'
      ? `<p class="lede">${esc(lane.error || `The ${word} plan could not be built.`)}</p><button type="button" class="btn-quiet" data-action="retry-lane" data-mode="${state.mode}">Try the ${word} plan again</button>`
      : `<p class="lede">Building your ${word} plan ${DOTS}</p>`;
  }

  function setMode(m) {
    if (!MODES[m] || m === state.mode) return;
    state.mode = m;
    safeStore.set(MODE_KEY, m);
    showLane(false);
    persist();
  }

  // A lane that was not on screen while it was built finishes here.
  function settleLane(mode, runId) {
    if (runId !== state.runId) return;
    state.finishedAt = performance.now();
    laneOf(mode).traces.forEach((t) => state.traces.push(t));
    persist();
    if (state.mode === mode) showLane(false); else renderModeSwitch();
    if (state.last && state.last.runId === runId) renderTrace();
    prefetchRoads(runId);
  }

  async function runLane(mode, base, intent, runId) {
    const lane = laneOf(mode);
    try {
      const a = await post('/api/alternatives', { ...base, mode, intent });
      if (runId !== state.runId) return;
      lane.alternatives = a.alternatives;
      if (a.trace) lane.traces.push(a.trace);
      const b = await post('/api/brief', { ...base, mode, intent, alternatives: a.alternatives });
      if (runId !== state.runId) return;
      lane.traces.push(b.trace);
      lane.result = b;
      lane.status = 'ready';
    } catch (e) {
      if (runId !== state.runId) return;
      lane.status = 'error';
      lane.error = e.message || 'This plan could not be built.';
    }
    settleLane(mode, runId);
  }

  function retryLane(mode) {
    if (!MODES[mode] || !state.intent || !state.base || state.running) return;
    state.lanes[mode] = newLane();
    renderModeSwitch();
    if (state.mode === mode) showLane(false);
    const runId = state.runId;
    runLane(mode, state.base, state.intent, runId);
  }

  async function run() {
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
    $('#results').hidden = false;
    resetProgress();
    $('#progress').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    const base = { lat: loc.lat, lng: loc.lng, radius_km: state.radius, local_minutes: pacificMinutes() };
    const runId = ++state.runId;
    // The way of travelling on screen is built step by step below. The other one is built at the same
    // time by its own agents, so switching is instant and neither waits for the other.
    const fg = state.mode, bg = fg === 'driving' ? 'walking' : 'driving';
    state.lanes = { driving: newLane(), walking: newLane() };
    state.base = base;
    state.intent = null;
    state.startedAt = state.finishedAt = performance.now();

    try {
      setStep('understand', 'active');
      const u = await post('/api/understand', { text });
      state.traces.push(u.trace);
      setStep('understand', 'done', u.trace);
      state.intent = u.intent;
      renderSafety(u.intent);
      renderUnderstood(u.intent);
      renderAsk();
      renderModeSwitch();
      runLane(bg, base, u.intent, runId);                    // never rejects: it reports through its lane

      setStep('search', 'active');
      const s = await post('/api/search', { ...base, mode: fg, needs: u.intent.needs });
      const checked = (s.results[0] && s.results[0].stats && s.results[0].stats.checked) || 0;
      setStep('search', 'done', { note: `${checked} stores within ${fmtMi(s.meta.radius_km)}` });
      if (state.mode === fg) renderByItem(s.results, true, fg, s.meta.radius_km);

      let alts = [];
      if (s.gaps.length) {
        setStep('alternatives', 'active');
        const a = await post('/api/alternatives', { ...base, mode: fg, intent: u.intent });
        alts = a.alternatives;
        state.traces.push(a.trace);
        setStep('alternatives', 'done', a.trace);
        if (state.mode === fg) renderAlternatives(alts, fg, s.meta.radius_km);
      } else {
        setStep('alternatives', 'skipped');
      }

      if (state.mode === fg) $('#r-plan').innerHTML = `<p class="lede">Building your ${MODES[fg].ing} route ${DOTS}</p>`;
      setStep('brief', 'active');
      const b = await post('/api/brief', { ...base, mode: fg, intent: u.intent, alternatives: alts });
      state.traces.push(b.trace);
      setStep('brief', 'done', b.trace);
      state.finishedAt = performance.now();
      Object.assign(laneOf(fg), { status: 'ready', result: b, alternatives: alts });
      state.last = { savedAt: Date.now(), runId, result: b, intent: u.intent, traces: state.traces, text, radius: state.radius, origin: loc.label, base };
      if (state.mode === fg) showLane(true); else renderModeSwitch();
      renderActions();
      renderTrace();
      persist();
      prefetchRoads(runId);
      if (state.mode === fg) $('#r-mode').scrollIntoView({ behavior: 'smooth', block: 'start' });   // the switch sits right above the plan
    } catch (e) {
      state.runId++;                                         // a lane still working for this search must not draw over the notice
      state.lanes = {};
      renderModeSwitch();
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
    if (state.running) return;
    const saved = safeStore.get(SAVE_KEY);
    if (!saved || !saved.result) return;
    clearResults();
    const runId = ++state.runId;
    state.radius = saved.radius;
    state.originLabel = saved.origin;
    state.traces = saved.traces || [];
    state.last = { ...saved, runId };
    state.intent = saved.intent;
    state.startedAt = 0;
    const loc = saved.result.plan.location;
    state.base = { ...(saved.base || { lat: loc.lat, lng: loc.lng, radius_km: saved.radius }), local_minutes: pacificMinutes() };
    // A plan saved before there were two lanes holds just one result.
    const stored = saved.lanes || { [modeOf(saved.result.plan)]: { result: saved.result, alternatives: [], road: saved.road } };
    state.lanes = {};
    Object.keys(MODES).forEach((m) => {
      const s = stored[m];
      state.lanes[m] = s && s.result
        ? { ...newLane(), status: 'ready', result: s.result, alternatives: s.alternatives || [], road: s.road || null, roadState: s.road ? 'done' : 'idle' }
        : { ...newLane(), status: 'error', error: `The ${MODES[m].ing} plan was not saved on this device. Search again to build it.` };
    });
    const ready = (m) => state.lanes[m] && state.lanes[m].status === 'ready';
    state.mode = ready(saved.mode) ? saved.mode : (Object.keys(MODES).find(ready) || 'driving');
    addLocation('__saved', { lat: loc.lat, lng: loc.lng, label: saved.origin || 'Saved location' });
    $('#place').value = '__saved';
    $('#radius').value = String(saved.radius);
    $('#progress').hidden = true;
    $('#text').value = saved.text || '';
    $('#results').hidden = false;
    renderSafety(saved.intent);
    renderUnderstood(saved.intent);
    renderAsk();
    showLane(false);
    renderActions();
    renderTrace();
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
    else if (act === 'retry-road' && mapState.plan && !mapState.loading) {
      const lane = laneOf(modeOf(mapState.plan));
      if (lane) { Object.assign(lane, { road: null, roadState: 'idle', roadPromise: null, roadFailure: '' }); loadRoad(mapState.plan, mapState.line, lane); }
    } else if (act === 'mode') setMode(t.dataset.mode);
    else if (act === 'retry-lane') retryLane(t.dataset.mode);
    else if (act === 'open-saved') openSaved();
    else if (act === 'print') window.print();
    else if (act === 'wider') {
      const sel = $('#radius'); const i = sel.selectedIndex;
      if (i < sel.options.length - 1) { sel.selectedIndex = i + 1; run(); } else toast('Already searching the widest area.');
    } else if (act === 'copy') {
      const text = (activeResult() || {}).plan_text;
      if (!text) return;
      try { await navigator.clipboard.writeText(text); toast('Plan copied.'); }
      catch (err) {
        const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); toast('Plan copied.'); } catch (e2) { toast('Could not copy. Use Print instead.'); }
        ta.remove();
      }
    } else if (act === 'sms') {
      e.preventDefault();
      const text = (activeResult() || {}).plan_text;
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
      // The server rebuilds both plans from where the person is and what they need. The plan text is only a backup.
      const alternatives = {};
      Object.keys(MODES).forEach((m) => { const l = laneOf(m); if (l && l.status === 'ready') alternatives[m] = slimAlts(l.alternatives); });
      const r = await post('/api/followup', { ...(state.base || {}), question: q, mode: state.mode, intent: state.intent || undefined,
        alternatives, plan_text: (activeResult() || {}).plan_text || '' });
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
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { /* optional */ });
  })();
})();
