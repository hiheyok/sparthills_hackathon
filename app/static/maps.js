/* Map providers share the same small interface; planning never depends on a map SDK. */
(() => {
  'use strict';
  const point = ([lat, lng]) => ({ lat, lng });
  const padding = { top: 112, right: 60, bottom: 110, left: 44 };
  let googleRequest;

  function loadGoogle(key) {
    if (window.google?.maps?.importLibrary) return Promise.resolve();
    if (googleRequest) return googleRequest;
    googleRequest = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      const timer = setTimeout(() => reject(new Error('Google Maps timed out.')), 12000);
      const finish = (error) => {
        clearTimeout(timer);
        if (error) reject(error); else resolve();
      };
      window.havenGoogleReady = () => { delete window.havenGoogleReady; finish(); };
      window.gm_authFailure = () => finish(new Error('Google Maps could not authorize this key.'));
      script.src = 'https://maps.googleapis.com/maps/api/js?' + new URLSearchParams({
        key, v: 'quarterly', loading: 'async', callback: 'havenGoogleReady', libraries: 'maps,marker',
      });
      script.async = true;
      script.nonce = document.querySelector('script[nonce]')?.nonce || '';
      script.onerror = () => finish(new Error('Google Maps could not load.'));
      document.head.append(script);
    });
    return googleRequest;
  }

  async function googleMap(element, config) {
    await loadGoogle(config.apiKey);
    const [{ Map, InfoWindow, Polyline }, { AdvancedMarkerElement }] = await Promise.all([
      google.maps.importLibrary('maps'), google.maps.importLibrary('marker'),
    ]);
    const map = new Map(element, {
      center: { lat: 37.3382, lng: -121.8863 }, zoom: 13, mapId: config.mapId || 'DEMO_MAP_ID',
      disableDefaultUI: true, zoomControl: true,
      zoomControlOptions: { position: google.maps.ControlPosition.RIGHT_BOTTOM },
      clickableIcons: false, gestureHandling: 'greedy', keyboardShortcuts: true,
    });
    const popupWidth = () => Math.max(180, Math.min(280, element.clientWidth - 48));
    const popup = new InfoWindow({ minWidth: popupWidth(), maxWidth: popupWidth() });
    const fitPopup = () => {
      const content = element.querySelector('.gm-style-iw-c');
      if (!content || !content.offsetParent) return;
      const box = content.getBoundingClientRect(), bounds = element.getBoundingClientRect();
      const dx = box.left < bounds.left + 12 ? box.left - bounds.left - 12 : Math.max(0, box.right - bounds.right + 12);
      const dy = box.top < bounds.top + 12 ? box.top - bounds.top - 12 : Math.max(0, box.bottom - bounds.bottom + 12);
      if (dx || dy) map.panBy(dx, dy);
    };
    popup.addListener('domready', () => {
      element.querySelectorAll('.stock-popup details').forEach((details) => {
        details.addEventListener('toggle', () => requestAnimationFrame(fitPopup));
      });
      requestAnimationFrame(fitPopup);
    });
    const overlays = new Set();
    // Do not accept an SDK download alone as proof that an authorized map rendered.
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Google Maps tiles are unavailable.')), 12000);
      google.maps.event.addListenerOnce(map, 'tilesloaded', () => { clearTimeout(timer); resolve(); });
      window.gm_authFailure = () => { clearTimeout(timer); reject(new Error('Google Maps could not authorize this key.')); };
    });
    return {
      provider: 'google',
      clear() { popup.close(); for (const overlay of overlays) overlay.remove(); overlays.clear(); },
      marker(position, { kind = '', label = '', title = '', html = '', onOpen } = {}) {
        const size = kind === 'pin-you' ? 20 : kind === 'pin-alt' ? 28 : 34;
        const node = document.createElement('span');
        node.className = `pin google-pin ${kind}`;
        node.textContent = label;
        node.style.width = node.style.height = `${size}px`;
        const marker = new AdvancedMarkerElement({
          map, position: point(position), title, gmpClickable: !!html, anchorLeft: '-50%', anchorTop: '-50%',
        });
        marker.append(node);
        const select = (focused = false) => {
          element.querySelectorAll('.pin.is-selected').forEach((pin) => pin.classList.remove('is-selected', 'is-focused'));
          node.classList.add('is-selected');
          if (focused) node.classList.add('is-focused');
        };
        const highlight = (pan = false) => {
          select(pan);
          if (pan) {
            popup.close();
            map.panTo(point(position));
            if ((map.getZoom() || 0) < 14) map.setZoom(14);
          }
        };
        const open = () => {
          if (!html) return;
          select(true);
          map.panTo(point(position));
          if ((map.getZoom() || 0) < 14) map.setZoom(14);
          popup.close();
          popup.setOptions({ minWidth: popupWidth(), maxWidth: popupWidth() });
          popup.setContent(typeof html === 'function' ? html() : html);
          popup.open({ map, anchor: marker, shouldFocus: false });
          google.maps.event.addListenerOnce(map, 'idle', fitPopup);
          onOpen?.();
          requestAnimationFrame(() => onOpen?.());
        };
        if (html) marker.addEventListener('gmp-click', open);
        const handle = { open, highlight, remove() { marker.map = null; } };
        overlays.add(handle);
        return handle;
      },
      line(path, { color = '#1a73e8', weight = 5, dashed = false } = {}) {
        const line = new Polyline({ map, path: path.map(point), strokeColor: color, strokeWeight: weight,
          strokeOpacity: dashed ? 0 : .95, zIndex: weight === 10 ? 1 : 2,
          ...(dashed ? { icons: [{ icon: { path: 'M 0,-1 0,1', strokeOpacity: .8, strokeColor: color, scale: 3 }, offset: '0', repeat: '16px' }] } : {}),
        });
        const handle = { remove() { line.setMap(null); } };
        overlays.add(handle);
        return handle;
      },
      resize() { google.maps.event.trigger(map, 'resize'); },
      setView(position, zoom) { map.setCenter(point(position)); map.setZoom(zoom); },
      getZoom() { return map.getZoom() || 13; },
      fit(points, maxZoom) {
        const bounds = new google.maps.LatLngBounds();
        points.forEach((p) => bounds.extend(point(p)));
        map.fitBounds(bounds, padding);
        google.maps.event.addListenerOnce(map, 'idle', () => { if (map.getZoom() > maxZoom) map.setZoom(maxZoom); });
      },
    };
  }

  function leafletMap(element, onTileError) {
    if (!window.L) throw new Error('The map is unavailable. Directions still work.');
    const map = L.map(element, { zoomControl: false }).setView([37.3382, -121.8863], 13);
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; OpenStreetMap contributors', referrerPolicy: 'strict-origin-when-cross-origin',
    }).addTo(map);
    let loaded = 0, failed = 0;
    tiles.on('tileload', () => { loaded++; });
    tiles.on('tileerror', () => { if (!loaded && ++failed >= 3) onTileError?.(); });
    const layer = L.layerGroup().addTo(map);
    function fitPopup(popup) {
      map.stop();
      const autoPan = popup.options.autoPan;
      popup.options.autoPan = false;
      popup.options.maxHeight = Math.max(180, element.clientHeight - 100);
      popup.update();
      popup.options.autoPan = autoPan;
      const box = popup.getElement().getBoundingClientRect(), bounds = element.getBoundingClientRect();
      const dx = box.left < bounds.left + 16 ? box.left - bounds.left - 16 : Math.max(0, box.right - bounds.right + 16);
      const dy = box.top < bounds.top + 16 ? box.top - bounds.top - 16 : Math.max(0, box.bottom - bounds.bottom + 16);
      if (dx || dy) map.panBy([dx, dy], { animate: false });
    }
    return {
      provider: 'openstreetmap',
      clear() { layer.clearLayers(); },
      marker(position, { kind = '', label = '', title = '', html = '', onOpen } = {}) {
        const size = kind === 'pin-you' ? 20 : kind === 'pin-alt' ? 28 : 34;
        const icon = L.divIcon({ className: `pin ${kind}`, html: label, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
        const marker = L.marker(position, { icon, title, keyboard: !!html }).addTo(layer);
        const popupContent = () => typeof html === 'function' ? html() : html;
        if (html) marker.bindPopup(popupContent, {
          maxWidth: Math.max(180, Math.min(340, element.clientWidth - 64)), minWidth: 150,
          maxHeight: Math.max(180, element.clientHeight - 100), autoPanPadding: [24, 32],
        });
        const select = (focused = true) => {
          element.querySelectorAll('.pin.is-selected').forEach((pin) => pin.classList.remove('is-selected', 'is-focused'));
          marker.getElement()?.classList.add('is-selected');
          if (focused) marker.getElement()?.classList.add('is-focused');
        };
        marker.on('click', () => select());
        marker.on('popupopen', () => {
          const popup = marker.getPopup(), content = popup.getElement()?.querySelector('.stock-popup');
          if (content) {
            // Preserve expanded details while letting Leaflet keep the larger popup inside the map.
            popup.setContent(content);
            content.addEventListener('toggle', () => fitPopup(popup), true);
            fitPopup(popup);
          }
          onOpen?.();
        });
        marker.on('popupclose', () => marker.getPopup()?.setContent(popupContent));
        return { remove() { layer.removeLayer(marker); }, highlight(pan = false) {
          select(pan);
          if (pan) {
            map.closePopup();
            map.setView(position, Math.max(14, map.getZoom()), { animate: false });
          }
        }, open() {
          if (!html) return;
          map.setView(position, Math.max(14, map.getZoom()), { animate: false });
          select();
          marker.openPopup();
        } };
      },
      line(path, { color = '#1a73e8', weight = 5, dashed = false } = {}) {
        const line = L.polyline(path, { color, weight, opacity: .95, ...(dashed ? { dashArray: '8 8' } : {}), smoothFactor: 0 }).addTo(layer);
        return { remove() { layer.removeLayer(line); } };
      },
      resize() { map.invalidateSize({ pan: false }); },
      setView(position, zoom) { map.setView(position, zoom, { animate: false }); },
      getZoom() { return map.getZoom(); },
      fit(points, maxZoom) { map.fitBounds(L.latLngBounds(points), { paddingTopLeft: [44, 112], paddingBottomRight: [60, 110], maxZoom, animate: false }); },
    };
  }

  window.HavenMaps = {
    async create(element, config = {}, { onFallback, onTileError } = {}) {
      element.textContent = '';
      if (config.apiKey) {
        try {
          const adapter = await googleMap(element, config);
          element.dataset.provider = adapter.provider;
          return adapter;
        } catch (error) {
          // A broken/unauthorized map must not prevent access to supplies or the saved plan.
          element.replaceChildren();
          onFallback?.();
        }
      }
      const adapter = leafletMap(element, onTileError);
      element.dataset.provider = adapter.provider;
      return adapter;
    },
  };
})();
