/* Map providers share the same small interface; planning never depends on a map SDK. */
(() => {
  'use strict';

  function leafletMap(element, onTileError) {
    if (!window.L) throw new Error('The map is unavailable. Directions still work.');
    const map = L.map(element, { zoomControl: false }).setView([37.3382, -121.8863], 13);
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors', referrerPolicy: 'strict-origin-when-cross-origin',
    }).addTo(map);
    let loaded = 0, failed = 0;
    tiles.on('tileload', () => { loaded++; });
    tiles.on('tileerror', () => { if (!loaded && ++failed >= 3) onTileError?.(); });
    const layer = L.layerGroup().addTo(map);
    return {
      provider: 'openstreetmap',
      clear() { layer.clearLayers(); },
      marker(position, { kind = '', label = '', title = '', html = '' } = {}) {
        const size = kind === 'pin-you' ? 20 : kind === 'pin-alt' ? 16 : 34;
        const icon = L.divIcon({ className: `pin ${kind}`, html: label, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
        const marker = L.marker(position, { icon, title, keyboard: !!html }).addTo(layer);
        if (html) marker.bindPopup(html);
        return marker;
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
      // Google Maps support was removed: always use the OpenStreetMap/Leaflet street map.
      const adapter = leafletMap(element, onTileError);
      element.dataset.provider = adapter.provider;
      return adapter;
    },
  };
})();
