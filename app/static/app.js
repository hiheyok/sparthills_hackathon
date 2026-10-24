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

  // Two lanes, one per way of getting there. Each has its own plan, briefing agent and path, and
  // they are built at the same time. state.mode is the lane on screen.
  const MODE_KEY = 'supplyline.mode';
  const MODES = { driving: { label: 'Drive', verb: 'drive', ing: 'driving' }, walking: { label: 'Walk', verb: 'walk', ing: 'walking' } };
  const state = { meta: null, customLocations: {}, radius: 8, traces: [], last: null, running: false,
    mode: safeStore.get(MODE_KEY) === 'walking' ? 'walking' : 'driving', lanes: {}, runId: 0, intent: null, origin: null,
    startedAt: 0, wallMs: 0, travelCache: {}, travelPending: new Map(), browseStoreId: null, popupStoreId: null };
  const newLane = () => ({ status: 'working', result: null, alternatives: [], traces: [], error: '',
    road: null, roadState: 'idle', roadPromise: null, roadFailure: '', closest: null, closestState: 'idle' });
  const assistant = { requestId: 0, context: null, pending: false };
  const DOTS = '<span class="dots" role="status" aria-label="working"><span class="sr">working</span><i></i><i></i><i></i></span>';

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

  function storeHours(store, now = pacificMinutes()) {
    const info = store.hours ? store : state.meta?.store_hours?.find((entry) => entry.id === store.id) || store;
    const hours = info.hours;
    if (info.status === 'closed') return { text: 'Closed · reopening time unconfirmed', closed: true };
    if (!hours) return { text: 'Hours unavailable', closed: false };
    if (hours.always || (hours.open === 0 && hours.close === 1440)) return { text: 'Open 24 hours', closed: false };
    const open = Number(hours.open), close = Number(hours.close);
    if (!Number.isFinite(open) || !Number.isFinite(close) || open === close) return { text: 'Hours unavailable', closed: false };
    const clock = (minutes) => {
      const value = ((minutes % 1440) + 1440) % 1440, hour = Math.floor(value / 60);
      return `${hour % 12 || 12}:${String(value % 60).padStart(2, '0')} ${hour < 12 ? 'AM' : 'PM'}`;
    };
    const overnight = open > close;
    const isOpen = overnight ? now >= open || now < close : now >= open && now < close;
    return isOpen ? { text: `Open · closes at ${clock(close)}`, closed: false }
      : { text: `Closed · opens ${!overnight && now >= close ? 'tomorrow ' : ''}at ${clock(open)}`, closed: true };
  }

  function hoursMarkup(store, className = '') {
    const info = store.hours ? store : state.meta?.store_hours?.find((entry) => entry.id === store.id) || store;
    const status = storeHours(info);
    return `<span class="store-hours ${className}${status.closed ? ' is-closed' : ''}" data-store-hours="${esc(store.id)}" data-hours="${esc(JSON.stringify(info.hours || null))}" data-store-status="${esc(info.status || '')}" role="status">${esc(status.text)}</span>`;
  }

  function updateStoreHours() {
    $$('[data-store-hours]').forEach((element) => {
      const status = storeHours({ id: element.dataset.storeHours, hours: JSON.parse(element.dataset.hours), status: element.dataset.storeStatus });
      if (element.textContent !== status.text) element.textContent = status.text;
      element.classList.toggle('is-closed', status.closed);
    });
  }
  setInterval(updateStoreHours, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) updateStoreHours(); });

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
  const mapState = { map: null, initializing: null, straight: null, token: 0, plan: null, cachedRoad: null, line: null, loading: false, bounds: null, maxZoom: 14, lane: null, mode: 'driving', markers: new Map(), pendingStore: null };

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
      if (mapState.plan) drawRoute(mapState.plan, mapState.lane);
      else showOrigin();
      if (mapState.pendingStore) showStoreOnMap(mapState.pendingStore);
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
  function seatMap() {
    const aside = $('.col-map'), slot = $('#mapSlot');
    if (!wide.matches && slot) { if (aside.parentNode !== slot) slot.appendChild(aside); } else rescueMap();
  }
  function placeMap() {
    seatMap();
    if (mapState.map) setTimeout(fitMap, 60);
  }
  wide.addEventListener('change', placeMap);

  // Resize the results column without changing the landing page or phone layout.
  const divider = $('#panelDivider');
  const PANEL_KEY = 'haven.panel-width.v1';
  let panelWidth = safeStore.get(PANEL_KEY);
  if (!Number.isFinite(panelWidth)) panelWidth = 380;
  function resizePanel(width = panelWidth, remember = false) {
    if (!wide.matches || $('.shell').hidden || !document.body.classList.contains('has-results')) return;
    const available = $('.shell').getBoundingClientRect().width;
    panelWidth = Math.round(Math.max(320, Math.min(width, available - 332)));
    $('.shell').style.setProperty('--panel-width', panelWidth + 'px');
    divider.setAttribute('aria-valuemin', '320');
    divider.setAttribute('aria-valuemax', String(Math.max(320, Math.round(available - 332))));
    divider.setAttribute('aria-valuenow', String(panelWidth));
    divider.setAttribute('aria-valuetext', panelWidth + ' pixels');
    if (remember) safeStore.set(PANEL_KEY, panelWidth);
  }
  divider.addEventListener('pointerdown', (event) => {
    if (!wide.matches || event.button !== 0) return;
    event.preventDefault();
    divider.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing-panel');
  });
  divider.addEventListener('pointermove', (event) => {
    if (!divider.hasPointerCapture(event.pointerId)) return;
    resizePanel(event.clientX - $('.shell').getBoundingClientRect().left - 6);
  });
  const stopResizing = () => {
    if (!document.body.classList.contains('resizing-panel')) return;
    document.body.classList.remove('resizing-panel');
    resizePanel(panelWidth, true);
  };
  divider.addEventListener('pointerup', stopResizing);
  divider.addEventListener('pointercancel', stopResizing);
  divider.addEventListener('lostpointercapture', stopResizing);
  divider.addEventListener('dblclick', () => resizePanel(380, true));
  divider.addEventListener('keydown', (event) => {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    resizePanel(event.key === 'Home' ? 320 : event.key === 'End' ? Infinity
      : panelWidth + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 48 : 16), true);
  });
  window.addEventListener('resize', () => resizePanel());
  wide.addEventListener('change', () => resizePanel());
  resizePanel();
  new ResizeObserver(() => requestAnimationFrame(fitMap)).observe($('#map'));
  window.addEventListener('resize', () => requestAnimationFrame(fitMap));

  function fitMap(force = false) {
    const map = mapState.map;
    if (!map) return;
    // A resize can arrive before the media-query event. Seat the map before measuring it.
    seatMap();
    if (!$('#map').offsetWidth || !$('#map').offsetHeight) return;
    map.resize();
    if (force !== true && $('#map .pin.is-focused')) return;
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
    mapState.markers.clear();
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

  // One route request at a time; foreground routes take priority over store previews.
  const routeJobs = [];
  let routeBusy = false;
  function queueRoute(task, runId, priority = 2) {
    return new Promise((resolve, reject) => {
      routeJobs.push({ task, runId, priority, resolve, reject });
      Promise.resolve().then(drainRoutes);
    });
  }
  async function drainRoutes() {
    if (routeBusy || !routeJobs.length) return;
    routeJobs.sort((a, b) => a.priority - b.priority);
    const job = routeJobs.shift();
    if (job.runId !== state.runId) { job.resolve({ skipped: true }); drainRoutes(); return; }
    routeBusy = true;
    try { job.resolve(await job.task()); } catch (error) { job.reject(error); }
    finally { routeBusy = false; setTimeout(drainRoutes, 0); }
  }

  function travelKey(origin, store, mode) {
    return [origin.lat, origin.lng, store.lat, store.lng].map((n) => Number(n).toFixed(6)).join(',') + ':' + mode;
  }
  function validTravel(value) {
    return value && Number.isFinite(value.distance_km) && value.distance_km >= 0
      && Number.isFinite(value.duration_min) && value.duration_min >= 0;
  }
  function freshTravel(value) {
    return validTravel(value) && value.fetchedAt && Date.now() - value.fetchedAt < (value.estimated ? 60000 : 600000);
  }
  function travelValue(origin, store, mode) {
    const cached = state.travelCache[travelKey(origin, store, mode)];
    if (validTravel(cached)) return cached;
    const km = distanceFrom(origin, store) * (mode === 'walking' ? 1.25 : 1.35);
    return { distance_km: km, duration_min: Math.max(1, km / (mode === 'walking' ? 4 : 28) * 60), estimated: true };
  }
  function travelText(value) {
    return `${value.estimated ? 'Est. ' : ''}${fmtMi(value.distance_km)} · ${fmtMin(value.duration_min)}`;
  }
  function rememberFirstLeg(lane, mode) {
    const plan = lane.result?.plan, first = plan?.route.stops[0];
    if (!first || !validRoad(lane.road)) return;
    const leg = lane.road.legs?.[0] || (plan.route.stops.length === 1 ? lane.road : null);
    if (validTravel(leg)) state.travelCache[travelKey(plan.location, first.store, mode)] = {
      distance_km: leg.distance_km, duration_min: leg.duration_min, estimated: false, fetchedAt: Date.now(),
    };
  }
  function requestStoreTravel(origin, store, mode, runId = state.runId) {
    const key = travelKey(origin, store, mode);
    if (freshTravel(state.travelCache[key])) return Promise.resolve(state.travelCache[key]);
    if (state.travelPending.has(key)) return state.travelPending.get(key);
    const pending = state.travelPending;
    const promise = queueRoute(async () => {
      if (freshTravel(state.travelCache[key])) return state.travelCache[key];
      // Skip destinations passed over while the user quickly browses the arrows.
      if (store.id !== state.browseStoreId && store.id !== state.popupStoreId
          && !Object.values(state.lanes).some((lane) => lane.status === 'ready' && closestStore(lane.result.plan)?.id === store.id)) return { skipped: true };
      try {
        const result = await post('/api/route', { mode, points: [origin, store].map(({ lat, lng }) => ({ lat, lng })) });
        if (result.available && validRoad(result)) return { distance_km: result.distance_km, duration_min: result.duration_min, estimated: false, fetchedAt: Date.now() };
      } catch (error) { /* Leave a labelled estimate when routing cannot answer. */ }
      return { ...travelValue(origin, store, mode), estimated: true, fetchedAt: Date.now() };
    }, runId).then((value) => {
      if (runId === state.runId && !value.skipped) {
        state.travelCache[key] = value;
        persist();
      }
      return value;
    }).finally(() => {
      pending.delete(key);
      if (runId === state.runId) updateTravelDisplays();
    });
    pending.set(key, promise);
    return promise;
  }
  function ensureStoreTravel(origin, store) {
    if (!store || !origin) return;
    Object.keys(MODES).forEach((mode) => { requestStoreTravel(origin, store, mode); });
    updateTravelDisplays();
  }

  function rankedStores(plan) {
    if (!plan) return [];
    const stores = new Map();
    const add = (store) => { if (store) stores.set(store.id, { ...stores.get(store.id), ...store }); };
    plan.results.forEach((item) => { item.stores.forEach(add); add(item.nearest_any); });
    (plan.alternatives || []).forEach((item) => item.options.forEach((option) => option.stores?.forEach(add)));
    plan.route.stops.forEach((stop) => add(stop.store));
    return [...stores.values()].sort((a, b) => distanceFrom(plan.location, a) - distanceFrom(plan.location, b) || a.id.localeCompare(b.id));
  }
  function displayPlan() { return laneOf(state.mode)?.result?.plan || mapState.plan; }
  function storeRank(store) {
    const plan = displayPlan();
    const stores = plan ? rankedStores(plan) : [...(state.optionStores?.values() || [])].map((entry) => entry.store)
      .sort((a, b) => distanceFrom(state.base || currentLoc(), a) - distanceFrom(state.base || currentLoc(), b));
    const index = stores.findIndex((candidate) => candidate.id === store.id);
    return index >= 0 ? index + 1 : stores.length + 1;
  }
  function browsedStore() {
    const plan = laneOf(state.mode)?.result?.plan;
    const stores = rankedStores(plan);
    if (!stores.length) return null;
    let index = stores.findIndex((store) => store.id === state.browseStoreId);
    if (index < 0) index = 0;
    state.browseStoreId = stores[index].id;
    return { store: stores[index], index, count: stores.length, origin: plan.location };
  }
  function travelCaption(origin, store) {
    if (Object.keys(MODES).some((mode) => state.travelPending.has(travelKey(origin, store, mode)))) return 'Updating times…';
    return Object.keys(MODES).some((mode) => travelValue(origin, store, mode).estimated)
      ? 'Estimated distance and time' : 'Routed distance · no live traffic';
  }
  function travelMarkup(origin, store, buttons = false) {
    return `<div class="store-travel ${buttons ? 'mode-switch closest-measures' : 'popup-travel'}" data-store-travel="${esc(store.id)}" data-origin-lat="${origin.lat}" data-origin-lng="${origin.lng}" data-store-lat="${store.lat}" data-store-lng="${store.lng}">
      ${Object.keys(MODES).map((mode) => {
        const lane = laneOf(mode);
        const text = buttons && lane?.status !== 'ready' ? (lane?.status === 'error' ? 'unavailable' : DOTS) : esc(travelText(travelValue(origin, store, mode)));
        return buttons ? `<button type="button" class="mode-btn" data-action="mode" data-mode="${mode}" data-travel-mode="${mode}" aria-pressed="${state.mode === mode}"><span class="mode-name">${MODES[mode].label}</span><b class="mode-sub">${text}</b></button>`
          : `<div class="popup-travel-mode" data-travel-mode="${mode}"><span>${MODES[mode].label}</span><b>${text}</b></div>`;
      }).join('')}
      <span class="travel-status" aria-live="polite">${esc(travelCaption(origin, store))}</span></div>`;
  }
  function updateTravelDisplays() {
    $$('[data-store-travel]').forEach((element) => {
      const origin = { lat: +element.dataset.originLat, lng: +element.dataset.originLng };
      const store = { id: element.dataset.storeTravel, lat: +element.dataset.storeLat, lng: +element.dataset.storeLng };
      $$('[data-travel-mode]', element).forEach((tile) => {
        const mode = tile.dataset.travelMode, lane = laneOf(mode);
        const value = $('b', tile);
        if (tile.tagName === 'BUTTON' && lane?.status !== 'ready' && lane?.status !== 'error') {
          if (!$('.dots', value)) value.innerHTML = DOTS;
        } else value.textContent = tile.tagName === 'BUTTON' && lane?.status === 'error' ? 'unavailable' : travelText(travelValue(origin, store, mode));
      });
      $('.travel-status', element).textContent = travelCaption(origin, store);
    });
  }
  function browseStore(step) {
    const selected = browsedStore();
    if (!selected) return;
    const stores = rankedStores(displayPlan());
    const index = Math.max(0, Math.min(stores.length - 1, selected.index + step));
    state.browseStoreId = stores[index].id;
    renderModeSwitch();
    mapState.markers.get(state.browseStoreId)?.highlight(true);
    persist();
    const next = $(`#r-mode [data-action="${step > 0 ? 'next-store' : 'previous-store'}"]`)
      || $('#r-mode [data-action="next-store"], #r-mode [data-action="previous-store"]');
    next?.focus({ preventScroll: true });
  }
  function popupOpened(store) {
    state.popupStoreId = store.id;
    if (rankedStores(displayPlan()).some((entry) => entry.id === store.id)) {
      state.browseStoreId = store.id;
      renderModeSwitch();
    }
    ensureStoreTravel(displayPlan()?.location || state.base, store);
    if (!wide.matches) {
      const panel = $('.col-map'), box = panel.getBoundingClientRect();
      if (box.top < 12 || box.bottom > innerHeight - 12) panel.scrollIntoView({ behavior: 'instant', block: 'center' });
    }
    requestAnimationFrame(updateTravelDisplays);
  }

  function requestRoad(lane, plan, runId) {
    if (lane.roadPromise) return lane.roadPromise;
    const mode = modeOf(plan);
    lane.roadState = 'loading';
    lane.roadPromise = (async () => {
      let road = null, failure = 'Road directions are unavailable right now.';
      try {
        const r = await queueRoute(() => post('/api/route', { points: lanePoints(plan).map(([lat, lng]) => ({ lat, lng })), mode }), runId, mode === state.mode ? 0 : 1);
        road = r.available && validRoad(r) ? r : null;
        if (!road) failure = r.reason || 'The routing service returned an invalid road path.';
      } catch (e) { failure = e.message || failure; }
      if (runId === state.runId) {                                  // a newer search owns the screen now
        lane.road = road;
        lane.roadFailure = road ? '' : failure;
        lane.roadState = road ? 'done' : 'failed';
        if (road) { rememberFirstLeg(lane, mode); persist(); }
        renderModeSwitch();
      }
      return { road, failure };
    })();
    return lane.roadPromise;
  }

  function prefetchRoads(runId) {
    if (runId !== state.runId) return;
    if (Object.values(state.lanes).some((lane) => lane.closestState === 'loading')) return;
    const shown = state.lanes[state.mode];
    if (shown && shown.status === 'working') return;               // the lane on screen is still being built: it asks first
    if (shown && shown.status === 'ready' && shown.result.plan.route.stops.length && shown.roadState !== 'done' && shown.roadState !== 'failed') return;
    for (const m of Object.keys(MODES)) {
      const lane = state.lanes[m];
      if (lane && lane.status === 'ready' && lane.roadState === 'idle' && lane.result.plan.route.stops.length) {
        requestRoad(lane, lane.result.plan, runId).then(() => prefetchRoads(runId));
        return;                                                     // one at a time
      }
      if (lane && lane.roadState === 'loading') return;
    }
    // Reuse a first leg where possible; otherwise request one direct route to the nearest matching store.
    for (const mode of Object.keys(MODES)) {
      const lane = laneOf(mode);
      if (lane && lane.closestState === 'loading') return;
      if (lane && lane.status === 'ready' && lane.closestState === 'idle') {
        loadClosestStore(lane, mode, runId).then(() => prefetchRoads(runId));
        return;
      }
    }
  }

  function distanceFrom(origin, store) {
    const rad = Math.PI / 180;
    const a = Math.sin((store.lat - origin.lat) * rad / 2) ** 2
      + Math.cos(origin.lat * rad) * Math.cos(store.lat * rad) * Math.sin((store.lng - origin.lng) * rad / 2) ** 2;
    return 6371.0088 * 2 * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  function closestStore(plan) { return rankedStores(plan)[0] || null; }

  async function loadClosestStore(lane, mode, runId) {
    lane.closestState = 'loading';
    const plan = lane.result.plan, store = closestStore(plan);
    const value = store ? await requestStoreTravel(plan.location, store, mode, runId) : null;
    if (runId !== state.runId) return;
    lane.closest = store && value && !value.skipped ? { store, ...value } : null;
    lane.closestState = 'done';
    persist();
    updateTravelDisplays();
  }

  async function loadRoad(plan, line, lane) {
    const token = mapState.token;
    const style = ROAD_STYLE[modeOf(plan)];
    mapState.loading = true;
    $('#mapNote').textContent = `Finding the ${modeOf(plan) === 'walking' ? 'best walking' : 'fastest road'} route...`;
    let road = validRoad(lane.road) ? lane.road : null;
    let failure = lane.roadFailure || 'Road directions are unavailable right now.';
    if (!road) ({ road, failure } = await requestRoad(lane, plan, state.runId));
    if (token !== mapState.token || !mapState.map) return;     // a newer search replaced this plan
    mapState.loading = false;
    const note = $('#mapNote');
    if (!road) {
      note.innerHTML = `${esc(failure)} The dashed line only connects stops; it is not a ${style.name} route. Use Directions or <button type="button" class="btn-small" data-action="retry-road">Retry ${modeOf(plan) === 'walking' ? 'walking' : 'road'} route</button>.`;
      prefetchRoads(state.runId);
      return;
    }
    mapState.map.line(road.path, { color: '#ffffff', weight: 10 });
    mapState.map.line(road.path, { color: style.color, weight: 5, dashed: !!style.dash });
    if (mapState.straight) mapState.straight.remove();
    mapState.bounds = [...road.path, ...line];
    mapState.maxZoom = 15;
    // A route finishing in the background must not move away from a selected store.
    if (!$('#map .pin.is-focused')) fitMap();
    // The routing services ask to see the OpenStreetMap credit and a link for fixing the map wherever their routes are shown.
    note.innerHTML = `${esc(style.note)}: about ${esc(fmtMin(road.duration_min))}, ${esc(fmtMi(road.distance_km))} (${esc(style.source)}). Store numbers show nearest first. `
      + '<a href="https://www.openstreetmap.org/fixthemap" target="_blank" rel="noopener">Fix the map</a>.';
    prefetchRoads(state.runId);
  }

  function stockPopup(store, items) {
    const fullName = String(store.name || '');
    const neighborhood = String(store.hood || '');
    const suffix = neighborhood ? `, ${neighborhood}` : '';
    const name = suffix && fullName.endsWith(suffix) ? fullName.slice(0, -suffix.length) : fullName;
    return `<div class="stock-popup"><div class="popup-title-row"><span class="store-rank">${storeRank(store)}</span><div><h3 title="${esc(fullName)}" aria-label="${esc(fullName)}">${esc(name)}</h3>
      ${neighborhood ? `<p class="popup-neighborhood">${esc(neighborhood)}</p>` : ''}</div></div>${kindBadge(store)}
      ${name.length > 32 ? `<details class="popup-full-name"><summary>Full store name</summary><p>${esc(fullName)}</p></details>` : ''}${hoursMarkup(store, 'popup-hours')}<p>Demo inventory</p>
      ${travelMarkup(displayPlan()?.location || state.base, store)}
      <ul class="popup-inventory" tabindex="0" aria-label="Supplies at ${esc(store.name)}">${items.map((i) => `<li>${esc(i.name)}${i.qty ? ` ×${i.qty}` : ''}</li>`).join('')}</ul>
      <p>${directions(store, state.mode)}</p></div>`;
  }

  function drawRoute(plan, lane) {
    mapState.plan = plan;
    mapState.lane = lane;
    mapState.mode = modeOf(plan);
    const map = ensureMap();
    const loc = plan.location;
    const color = ROAD_STYLE[modeOf(plan)].color;
    if (!map) return;
    mapState.token++;
    mapState.plan = plan;
    map.clear();
    mapState.markers.clear();
    const bounds = [[loc.lat, loc.lng]];
    const inRoute = new Map(plan.route.stops.map((stop) => [stop.store.id, stop]));
    rankedStores(plan).forEach((store, index) => {
      const stop = inRoute.get(store.id);
      const items = stop?.items || [
        ...plan.results.filter((item) => item.stores.some((s) => s.id === store.id) || item.nearest_any?.id === store.id).map((item) => ({ name: item.label })),
        ...(plan.alternatives || []).flatMap((item) => item.options.filter((option) => option.stores?.some((s) => s.id === store.id)).map((option) => ({ name: option.name }))),
      ];
      mapState.markers.set(store.id, map.marker([store.lat, store.lng], { kind: stop ? '' : 'pin-alt', label: String(index + 1), title: store.name,
        html: () => stockPopup(store, items), onOpen: () => popupOpened(store) }));
    });
    const line = [[loc.lat, loc.lng]];
    mapState.line = line;
    plan.route.stops.forEach((stop) => { line.push([stop.store.lat, stop.store.lng]); bounds.push([stop.store.lat, stop.store.lng]); });
    mapState.straight = line.length > 1
      ? map.line(line, { color, weight: 4, dashed: true }) : null;
    map.marker([loc.lat, loc.lng], { kind: 'pin-you', title: 'Your location' });
    mapState.bounds = bounds.length > 1 ? bounds : null;
    mapState.maxZoom = 14;
    fitMap();
    mapState.markers.get(state.browseStoreId)?.highlight();
    if (mapState.pendingStore) showStoreOnMap(mapState.pendingStore);
    if (bounds.length === 1) map.setView([loc.lat, loc.lng], 12);
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

  function registerOptionStore(store, itemName) {
    if (!store) return;
    const record = state.optionStores.get(store.id) || { store, items: [] };
    if (!record.items.some((item) => item.name === itemName)) record.items.push({ name: itemName });
    state.optionStores.set(store.id, record);
  }

  function itemMapTarget(store, itemName, className = 'item-map-target') {
    if (!store) return '';
    return `<button type="button" class="${className}" data-action="show-store" data-store="${esc(store.id)}" aria-label="Show ${esc(itemName)} at ${esc(store.name)} on map"></button>`;
  }

  function showStoreOnMap(id) {
    const record = state.optionStores?.get(id);
    if (!record) return;
    mapState.pendingStore = id;
    const panel = $('.col-map');
    if (getComputedStyle(panel).display === 'none') { toast('The map will open when your route is ready.'); return; }
    panel.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
    const map = ensureMap();
    if (!map) return;
    let marker = mapState.markers.get(id);
    if (!marker) {
      const { store, items } = record;
      marker = map.marker([store.lat, store.lng], { kind: 'pin-alt', label: String(storeRank(store)), title: store.name, html: () => stockPopup(store, items), onOpen: () => popupOpened(store) });
      mapState.markers.set(id, marker);
    }
    marker.open();
    mapState.pendingStore = null;
  }

  function storeLine(s, mode) {
    const f = freshness(s);
    return `<button type="button" class="store store-option" data-action="show-store" data-store="${esc(s.id)}" aria-label="Show ${esc(s.name)} on map"><span class="store-line-heading"><span class="store-rank">${storeRank(s)}</span><strong>${esc(s.name)}</strong></span>${kindBadge(s)}
      <span class="facts"><span>${fmtMin(s.eta_min)} ${MODES[mode || 'driving'].verb}</span><span>${fmtMi(s.dist_km)}</span>${hoursMarkup(s)}
      <span class="${f.cls}">${esc(f.text)}</span>${s.low ? '<span class="warn">Low stock</span>' : ''}${s.store_note ? `<span class="warn">${esc(s.store_note)}</span>` : ''}</span></button>`;
  }

  function kindBadge(store) {
    if (!store?.kind || (store.kind === 'Store' && !store.free)) return '';
    return `<span class="place-kind${store.free ? ' free' : ''}">${esc(store.kind)}</span>`;
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

  function renderByItem(results, open, mode, radiusKm, alternatives = []) {
    const altsByItem = new Map(alternatives.map((a) => [a.for_item, a]));
    state.optionStores = new Map();
    results.forEach((item) => {
      item.stores.forEach((store) => registerOptionStore(store, item.label));
      registerOptionStore(item.nearest_any, item.label);
    });
    alternatives.forEach((item) => item.options.forEach((option) => option.stores?.forEach((store) => registerOptionStore(store, option.name))));
    $('#r-items').innerHTML = `<details ${open ? 'open' : ''}><summary>Nearby store options</summary>
      ${alternatives.length ? '<p class="section-sub">For these, we looked at what is in stock near you and picked the closest safe match. Check the notes before you swap.</p>' : ''}` + results.map((r) => {
      const alternative = altsByItem.get(r.item_id);
      if (r.status === 'gap' && alternative) return renderAlternativeItem(r, alternative, mode, radiusKm);
      const chip = r.status === 'found' ? ['In stock nearby', 'ok'] : r.status === 'partial' ? ['Limited stock', 'warn'] : ['Not available nearby', 'bad'];
      return `<div class="item-block store-item" data-item="${esc(r.item_id)}" ${r.status === 'gap' ? 'data-store-gap' : ''}>${itemMapTarget(r.stores[0] || r.nearest_any, r.label)}<div class="item-head"><h4>${esc(r.label)}${r.qty > 1 ? ` x${r.qty}` : ''}</h4>
        <span class="chip ${chip[1]}">${chip[0]}</span>${r.inferred ? '<span class="tag ai" title="You did not say this, but the plan needs it">Inferred</span>' : ''}</div>
        ${r.status === 'gap' ? `<p class="gap-text">${esc(gapText(r, mode, radiusKm))}</p>` : r.stores.map((s) => storeLine(s, mode)).join('')}</div>`;
    }).join('') + '</details>';
  }

  function directions(s, mode) {
    const q = `${s.lat},${s.lng}`, walking = mode === 'walking';
    return `<a class="btn-small" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=${q}&travelmode=${walking ? 'walking' : 'driving'}">Directions</a>
            <a class="btn-small" target="_blank" rel="noopener" href="https://maps.apple.com/?daddr=${q}&dirflg=${walking ? 'w' : 'd'}">Apple Maps</a>`;
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
    resizePanel();
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
      html = `<h2 class="plan-title" id="planTitle">${esc(brief.headline)}</h2>
        <div class="facts-row"><span>${stops.length} stop${stops.length > 1 ? 's' : ''}</span><span>${fmtMin(plan.route.total_travel_min ?? plan.route.total_drive_min)} ${ROAD_STYLE[modeOf(plan)].name}</span><span>${sum.covered ?? sum.in_route} of ${sum.needs} items covered</span>${sum.swapped ? `<span>${sum.swapped} substitute${sum.swapped > 1 ? 's' : ''}</span>` : ''}</div>
        <p class="plan-hours">${hoursMarkup(stops[0].store)}</p>
        <div id="mapSlot" class="map-slot"></div>
        <details class="route-details"><summary>Pickup route · ${stops.length} stops</summary><ol class="route"><li class="origin"><span class="node" aria-hidden="true"></span><div class="origin-text"><b>Start</b><small>${esc(state.originLabel || '')}${state.originLabel ? ', ' : ''}leave about ${esc(plan.route.leaves)}</small></div></li>
        ${stops.map((s) => `<li class="stop"><span class="node" aria-hidden="true">${s.order}</span><div>
          <h3>${esc(s.store.name)}</h3>${kindBadge(s.store)}
          <div class="where-line"><span><b>${fmtMin(s.leg_min)}</b> ${MODES[modeOf(plan)].verb}, ${fmtMi(s.leg_km)}</span><span>Arrive about ${esc(s.arrive)}</span>${hoursMarkup(s.store)}${s.store.store_note ? `<span class="warn">${esc(s.store.store_note)}</span>` : ''}</div>
          ${says[s.order] ? `<p class="say">${esc(says[s.order])}</p>` : ''}
          <ul class="tickets">${s.items.map((i) => ticket(i, s.store.id)).join('')}</ul>
          <div class="stop-actions">${directions(s.store, modeOf(plan))}${reportPanel(s)}</div></div></li>`).join('')}</ol></details>`;
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
    $('#r-route').replaceChildren(...$$('.route-details, .notes-grid', el));
    renderChecklist(plan);
    placeMap();
  }

  const FIT_TEXT = { strong: 'does the same job', partial: 'covers part of the need', workaround: 'temporary workaround' };

  function renderAlternativeItem(item, alternative, mode, radiusKm) {
    const a = alternative;
    const noSub = a.verdict === 'no_safe_substitute';
    const st = a.stats || {};
    const sub = `${st.checked || 0} stores checked within ${fmtMi(radiusKm || state.radius)}` + (a.nearest_any ? `. Nearest with stock: ${a.nearest_any.name}, ${fmtMi(a.nearest_any.dist_km)} away${mode === 'walking' ? ` (${fmtMin(a.nearest_any.eta_min)} on foot)` : ''}.` : '. None have it in stock.');
    return `<article class="sign item-block ${noSub ? 'no-sub' : ''}" data-item="${esc(item.item_id)}" data-store-gap ${a.options.length ? 'data-has-alternatives' : ''}>
      ${itemMapTarget(a.options[0]?.stores?.[0] || a.nearest_any, item.label || a.for_name)}<div class="sign-head"><div class="item-head"><h4>${esc(item.label || a.for_name)}${item.qty > 1 ? ` x${item.qty}` : ''}</h4>
        <span class="chip bad">Not available nearby</span>${a.options.length ? '<span class="chip ok">Alternatives available</span>' : ''}
        ${item.inferred ? '<span class="tag ai" title="You did not say this, but the plan needs it">Inferred</span>' : ''}</div><p>${esc(sub)}</p></div>
      <div class="sign-body">
        ${noSub ? '<p class="caution">No safe substitute.</p>' : ''}
        ${a.quantity_note ? `<p class="caution"><b>Quantity not confirmed:</b> ${esc(a.quantity_note)}</p>` : ''}
        ${a.options.map((o) => `<div class="opt ${esc(o.fit)}">${itemMapTarget(o.stores?.[0], o.name, 'option-map-target')}<h4>${esc(o.name)}<span class="fit">${esc(FIT_TEXT[o.fit] || '')}</span></h4>
          ${o.why ? `<p>${esc(o.why)}</p>` : ''}${o.how_to_use ? `<p>${esc(o.how_to_use)}</p>` : ''}
          ${o.caution ? `<p class="caution"><b>Check:</b> ${esc(o.caution)}</p>` : ''}
          ${o.stores && o.stores[0] ? `<p class="near">${esc(o.stores[0].name)}, ${fmtMin(o.stores[0].eta_min)}, ${fmtMi(o.stores[0].dist_km)}</p>` : ''}</div>`).join('')}
        ${a.no_purchase_tips && a.no_purchase_tips.length ? `<div><h5>Without buying anything</h5><ul>${a.no_purchase_tips.map((t) => `<li>${esc(t)}</li>`).join('')}</ul></div>` : ''}
        ${a.escalate ? `<div class="next-step"><b>Next step</b>${esc(a.escalate)}</div>` : ''}
      </div></article>`;
  }

  function resetAssistant() {
    assistant.requestId++;
    assistant.context = null;
    setAssistantPending(false);
    if ($('#planAssistant').open) $('#planAssistant').close();
    $('#assistantQuestion').value = '';
    $('#assistantReply').hidden = true;
    $('#assistantError').hidden = true;
    $('#assistantEvidence').open = false;
  }

  function setAssistantPending(pending) {
    assistant.pending = pending;
    $('#assistantForm').setAttribute('aria-busy', String(pending));
    $('#assistantQuestion').disabled = pending;
    $('#assistantSend').disabled = pending;
    $('#assistantSend').textContent = pending ? 'Thinking…' : 'Ask';
    $('#assistantStatus').hidden = !pending;
    $$('#planAssistant [data-action="assistant-prompt"]').forEach((button) => { button.disabled = pending; });
  }

  function openAssistant() {
    if ($('#askPlan').disabled) return;
    if (!state.intent?.needs?.length || !state.base) { toast('Create a new plan to ask about its supplies.'); return; }
    const context = `${state.runId}:${state.mode}`;
    if (assistant.context !== context) { resetAssistant(); assistant.context = context; }
    const preparing = Object.values(state.lanes).some((lane) => lane.status === 'working');
    $('#assistantContext').textContent = `${MODES[state.mode].label} plan · ${state.originLabel || 'Your search area'}${preparing ? ' · Store options are still being prepared' : ''}`;
    $('#planAssistant').showModal();
    if (!assistant.pending) $('#assistantQuestion').focus();
  }

  async function askAboutPlan(question = $('#assistantQuestion').value) {
    if (assistant.pending || !state.intent?.needs?.length) return;
    question = question.trim().slice(0, 400);
    if (question.length < 3) { $('#assistantQuestion').focus(); return; }
    const requestId = ++assistant.requestId, runId = state.runId;
    const body = { ...state.base, mode: state.mode, question, intent: state.intent,
      alternatives: Object.fromEntries(Object.keys(MODES).map((mode) => [mode, laneOf(mode)?.alternatives || []])) };
    $('#assistantQuestion').value = question;
    $('#assistantError').hidden = true;
    $('#assistantReply').hidden = true;
    $('#assistantEvidence').open = false;
    setAssistantPending(true);
    try {
      const result = await post('/api/followup', body);
      if (requestId !== assistant.requestId || runId !== state.runId) return;
      if (!result.grounded || typeof result.answer !== 'string' || !result.answer.trim()) throw new Error('No answer about this plan was returned. Please try again.');
      const trace = result.trace || {}, bedrock = trace.mode === 'bedrock';
      $('#assistantReply').hidden = false;
      $('#assistantAsked').textContent = question;
      $('#assistantAnswer').textContent = result.answer;
      $('#assistantSource').textContent = bedrock ? `Amazon Bedrock · ${shortModel(trace.model)}`
        : trace.reason?.includes('AI_MODE=off') ? 'Planner response · AI is turned off' : 'Planner response · Bedrock unavailable';
      const metrics = bedrock ? [ ['Model', trace.model], ['Region', trace.region], ['Time', `${((trace.ms || 0) / 1000).toFixed(1)}s`], ['Tokens in / out', `${trace.tokens_in || 0} / ${trace.tokens_out || 0}`] ]
        : [['Source', 'Rules fallback'], ['Reason', trace.reason || 'Amazon Bedrock did not return an answer.']];
      $('#assistantMetrics').innerHTML = metrics.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('');
      if (result.trace) { state.traces.push(result.trace); renderTrace(); persist(); }
    } catch (error) {
      if (requestId !== assistant.requestId || runId !== state.runId) return;
      $('#assistantError').textContent = error.message;
      $('#assistantError').hidden = false;
    } finally {
      if (requestId === assistant.requestId) setAssistantPending(false);
    }
  }

  $('#assistantForm').addEventListener('submit', (event) => { event.preventDefault(); askAboutPlan(); });
  $('#assistantQuestion').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault(); $('#assistantForm').requestSubmit();
    }
  });

  function renderActions() {
    $('#r-actions').innerHTML = `<button type="button" class="btn-quiet" data-action="copy">Copy</button>
      <a class="btn-quiet" data-action="sms" href="#" style="text-decoration:none;display:inline-flex;align-items:center">Text</a>
      <button type="button" class="btn-quiet" data-action="print">Print</button>`;
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
      <p class="trace-note">Stock, distance, opening hours and routing are plain code, so they are repeatable. Amazon Bedrock does the language work, and each task has its own agent: one reads your request, one finds substitutes, one writes the briefing for each way of getting there, and one answers your questions. Driving and walking each get their own, running together. Agents can only choose catalog items, and substitutes only from items confirmed in stock near you. Prescriptions are never substituted.</p>
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
    resetAssistant();
    $('#askPlan').disabled = true;
    mapState.token++;
    mapState.plan = null;
    mapState.cachedRoad = null;
    mapState.pendingStore = null;
    state.browseStoreId = null;
    state.popupStoreId = null;
    state.travelPending = new Map();
    mapState.markers.clear();
    state.optionStores = new Map();
    mapState.lane = null;
    mapState.line = null;
    mapState.loading = false;
    state.lanes = {};
    showOrigin();
    rescueMap();
    ['r-safety', 'r-mode', 'r-plan', 'r-checklist', 'r-route', 'r-items', 'r-understood', 'r-actions', 'r-trace'].forEach((id) => { $('#' + id).innerHTML = ''; });
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

  // Both lanes go to the device, so a saved plan works offline for walking and for driving.
  function persist() {
    if (!state.last || state.last.runId !== state.runId) return;     // the saved plan belongs to an older search
    const lanes = {};
    Object.keys(MODES).forEach((m) => {
      const l = laneOf(m);
      if (l && l.status === 'ready') lanes[m] = { result: l.result, alternatives: l.alternatives, road: l.road, closest: l.closest };
    });
    const primary = lanes.driving || lanes[state.mode] || lanes.walking;
    if (!primary) return;
    state.last = { ...state.last, result: primary.result, road: primary.road, lanes, mode: state.mode, traces: state.traces, travelCache: state.travelCache, browseStoreId: state.browseStoreId };
    safeStore.set(SAVE_KEY, state.last);
    const records = readLists();
    const matching = records.find((record) => record.snapshot.savedAt === state.last.savedAt);
    if (matching) { matching.snapshot = JSON.parse(JSON.stringify(state.last)); safeStore.set(LISTS_KEY, records); }
  }

  function laneSummary(m) {
    const l = laneOf(m);
    if (!l || l.status === 'working') return DOTS;
    if (l.status === 'error') return 'unavailable';
    const route = l.result.plan.route, n = route.stops.length;
    return esc(n ? `${l.road ? '' : 'Est. '}${fmtMi(l.road?.distance_km ?? route.total_km)} · ${fmtMin(l.road?.duration_min ?? route.total_travel_min ?? route.total_drive_min)}` : 'nothing in range');
  }

  function closestSummary() {
    const selected = browsedStore();
    if (!selected) return '';
    const { store, index, count, origin } = selected;
    const arrow = (step, label, path) => `<button type="button" class="store-browse-arrow" data-action="${step < 0 ? 'previous-store' : 'next-store'}" aria-label="${label}"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${path}"/></svg></button>`;
    return `<div class="closest-store" data-current-store="${esc(store.id)}"><div class="closest-topline"><span class="closest-label">${index === 0 ? 'Closest store by location' : 'Nearby stores by distance'}</span>
      <div class="store-browse-controls"><span class="store-position">${index + 1} of ${count}</span>${index > 0 ? arrow(-1, 'Previous closest store', 'm14 6-6 6 6 6') : ''}${index < count - 1 ? arrow(1, 'Next closest store', 'm10 6 6 6-6 6') : ''}</div></div>
      <div class="closest-title-row"><span class="store-rank">${index + 1}</span><h3><button type="button" data-action="show-store" data-store="${esc(store.id)}" title="Show on map">${esc(store.name)}</button></h3></div>
      ${kindBadge(store)}
      ${hoursMarkup(store, 'closest-hours')}
      ${travelMarkup(origin, store, true)}
      <a target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=${store.lat},${store.lng}&travelmode=${state.mode}">Directions ↗</a></div>`;
  }

  function renderModeSwitch() {
    $('#askPlan').disabled = !state.intent?.needs?.length || !state.base;
    const el = $('#r-mode');
    if (!Object.keys(state.lanes).length) { el.innerHTML = ''; return; }
    const selected = browsedStore();
    el.innerHTML = selected ? closestSummary() : `<div class="mode-switch" role="group" aria-label="How are you getting there">${Object.keys(MODES).map((m) =>
      `<button type="button" class="mode-btn" data-action="mode" data-mode="${m}" aria-pressed="${state.mode === m}">
        <span class="mode-name">${MODES[m].label}</span><span class="mode-sub">${laneSummary(m)}</span></button>`).join('')}</div>`;
    if (selected) ensureStoreTravel(selected.origin, selected.store);
  }

  // Put the chosen lane on screen: its plan, substitutes, store lists and path.
  function showLane(reveal) {
    const lane = laneOf(state.mode);
    if (lane?.status === 'ready' && lane.roadState === 'idle' && lane.result.plan.route.stops.length) requestRoad(lane, lane.result.plan, state.runId);
    renderModeSwitch();
    if (!lane) return;
    if (lane.status === 'ready') {
      const plan = lane.result.plan, mode = modeOf(plan);
      renderPlan(lane.result, reveal);
      renderByItem(plan.results, true, mode, plan.location.radius_km, plan.alternatives);
      drawRoute(plan, lane);
      return;
    }
    rescueMap();                                     // the map may sit inside the section that is about to be replaced
    mapState.token++;
    mapState.plan = null;
    mapState.line = null;
    mapState.loading = false;
    showOrigin();                                    // not the other lane's route
    ['r-items', 'r-checklist', 'r-route'].forEach((id) => { $('#' + id).innerHTML = ''; });
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
    resizePanel();
    ensureMap();
    $('#results').hidden = false;
    resetProgress();
    $('#progress').scrollIntoView({ behavior: scrollBehavior(), block: 'nearest' });
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
        if (state.mode === fg) renderByItem(s.results, true, fg, s.meta.radius_km, alts);
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
      state.last = { savedAt: Date.now(), runId, result: b, intent: u.intent, traces: state.traces, text, radius: state.radius, origin: loc.label, base, categories: Object.fromEntries((state.meta?.catalog || []).map((item) => [item.id, item.category])) };
      if (state.mode === fg) showLane(true); else renderModeSwitch();
      renderActions();
      renderTrace();
      persist();
      prefetchRoads(runId);
      if (state.mode === fg) $('#r-mode').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });   // the switch sits right above the plan
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
      $('#progress').hidden = true;
    }
  }

  function openSaved(saved = safeStore.get(SAVE_KEY)) {
    if (state.running) return;
    if (!saved?.result?.plan) return;
    showWorkspace();
    document.body.classList.add('has-results');
    resizePanel();
    clearResults();
    const runId = ++state.runId;
    state.radius = saved.radius;
    state.originLabel = saved.origin;
    state.traces = saved.traces || [];
    state.last = { ...saved, runId };
    state.travelCache = saved.travelCache || {};
    state.browseStoreId = saved.browseStoreId || null;
    state.intent = saved.intent;
    state.startedAt = 0;
    safeStore.set(SAVE_KEY, state.last);
    const loc = saved.result.plan.location;
    state.base = { ...(saved.base || { lat: loc.lat, lng: loc.lng, radius_km: saved.radius }), local_minutes: pacificMinutes() };
    // A plan saved before there were two lanes holds just one result.
    const stored = saved.lanes || { [modeOf(saved.result.plan)]: { result: saved.result, alternatives: [], road: saved.road } };
    state.lanes = {};
    Object.keys(MODES).forEach((m) => {
      const s = stored[m];
      state.lanes[m] = s && s.result
        ? { ...newLane(), status: 'ready', result: s.result, alternatives: s.alternatives || [], road: s.road || null, roadState: s.road ? 'done' : 'idle', closest: s.closest || null, closestState: s.closest ? 'done' : 'idle' }
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
  function updateFullscreen() {
    const expanded = !!document.fullscreenElement || $('.col-map').classList.contains('is-expanded');
    $('#expandMap').setAttribute('aria-label', expanded ? 'Exit fullscreen map' : 'Expand map');
    $('#expandMap').setAttribute('aria-pressed', String(expanded));
    $('#expandMap').title = expanded ? 'Exit fullscreen map' : 'Expand map';
    $('#expandMap span').textContent = expanded ? 'Exit full screen' : 'Full screen';
    requestAnimationFrame(fitMap);
  }
  document.addEventListener('fullscreenchange', updateFullscreen);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && $('.col-map').classList.contains('is-expanded')) {
      $('.col-map').classList.remove('is-expanded');
      document.body.classList.remove('map-expanded');
      updateFullscreen();
    }
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
    if (act === 'next-store') browseStore(1);
    else if (act === 'previous-store') browseStore(-1);
    else if (act === 'show-store') showStoreOnMap(t.dataset.store);
    else if (act === 'retry') run();
    else if (act === 'retry-road' && mapState.plan && !mapState.loading) {
      const lane = laneOf(modeOf(mapState.plan));
      if (lane) { Object.assign(lane, { road: null, roadState: 'idle', roadPromise: null, roadFailure: '', closest: null, closestState: 'idle' }); loadRoad(mapState.plan, mapState.line, lane); }
    } else if (act === 'mode') setMode(t.dataset.mode);
    else if (act === 'retry-lane') retryLane(t.dataset.mode);
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
      const item = $('#r-items [data-has-alternatives]') || $('#r-items [data-store-gap]');
      if (item) { $('#r-items details').open = true; item.scrollIntoView({ behavior: scrollBehavior(), block: 'start' }); }
      else toast('Your plan has no unavailable supplies needing alternatives.');
    }
    else if (act === 'ask-plan') openAssistant();
    else if (act === 'close-assistant') $('#planAssistant').close();
    else if (act === 'assistant-prompt') askAboutPlan(t.dataset.question);
    else if (act === 'settings') {
      $('#mapsConnection').textContent = $('#map').dataset.provider === 'google' ? 'Connected' : state.meta?.maps?.apiKey ? ($('#map').dataset.provider ? 'Using backup map' : 'Connecting…') : 'Not configured';
      const latest = [...state.traces].reverse().find(Boolean);
      $('#aiConnection').textContent = state.meta?.ai?.mode === 'off' ? 'AI is turned off · local planner'
        : latest?.mode === 'bedrock' ? 'Last response from Amazon Bedrock'
        : latest ? 'Last response used the fallback planner' : 'Ready to try Amazon Bedrock';
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
      if (mapState.bounds) fitMap(true);
      else { const loc = currentLoc(); if (loc && mapState.map) mapState.map.setView([loc.lat, loc.lng], 13); }
    }
    else if (act === 'expand-map') {
      const panel = $('.col-map');
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (panel.classList.contains('is-expanded')) {
        panel.classList.remove('is-expanded');
        document.body.classList.remove('map-expanded');
      } else {
        try { await panel.requestFullscreen(); }
        catch (error) { panel.classList.add('is-expanded'); document.body.classList.add('map-expanded'); }
      }
      updateFullscreen();
    }
    else if (act === 'how' || act === 'about') $('#howDialog').showModal();
    else if (act === 'close-how') $('#howDialog').close();
    else if (act === 'download') {
      if (!state.last) return;
      const collected = state.last.collected || [];
      const result = activeResult();
      if (!result) return;
      const checklist = result.plan.route.stops.flatMap((stop) => stop.items.map((i) =>
        `${collected.includes(`${stop.store.id}:${i.item_id}`) ? '[x]' : '[ ]'} ${i.name} x${i.qty} · ${stop.store.name}`));
      const text = result.plan_text + '\n\nSUPPLY CHECKLIST\n' + checklist.join('\n');
      const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = url; link.download = 'SupplyLine-supply-plan.txt'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast('Supply plan downloaded.');
    }
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
