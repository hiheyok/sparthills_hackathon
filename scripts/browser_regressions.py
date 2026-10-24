#!/usr/bin/env python3
"""Real Chrome checks. Run: python3 scripts/browser_regressions.py

Requires `pip install playwright` and Chrome (or BROWSER_CHANNEL=chromium with
`playwright install chromium`). Uses an isolated browser and a local server;
AI, road routing and map tiles make no external requests.
"""
import datetime
import os
import re
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'app'))
os.environ['AI_MODE'] = 'off'
os.environ['ROAD_ROUTING'] = 'off'
os.environ.pop('REPORTS_TABLE', None)
from local_server import Local
import handler
import store

TEXT = 'I need bottled water, canned food and AA batteries'


class QuietLocal(Local):
    asset_version = 'one'

    def do_GET(self):
        if self.path == '/static/cache-probe.js':
            body = self.asset_version.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


class BrowserRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.set_store(store.LocalReportStore())
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietLocal)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel=os.environ.get('BROWSER_CHANNEL', 'chrome'))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        handler._hits.clear()
        self.context = self.browser.new_context(service_workers='block',
            permissions=['geolocation'], geolocation={'latitude': 37.34, 'longitude': -121.89})
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.clock.set_fixed_time(datetime.datetime(2026, 10, 23, 17, tzinfo=datetime.timezone.utc))
        self.errors, self.searches, self.paths = [], [], []
        self.page.on('pageerror', lambda err: self.errors.append(str(err)))
        self.page.on('request', lambda req: self.searches.append(req.post_data_json)
                     if req.url.endswith('/api/search') else None)
        self.page.route('https://tile.openstreetmap.org/**', lambda r: r.abort())
        self.page.route('**/api/route', self.road)

    def tearDown(self):
        self.context.close()

    def road(self, route):
        pts = [[p['lat'], p['lng']] for p in route.request.post_data_json['points']]
        path = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            path.extend([[a[0], (a[1] + b[1]) / 2], [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2],
                         [(a[0] + b[0]) / 2, b[1]], b])
        self.paths.append(path)
        route.fulfill(json={'available': True, 'path': path, 'duration_min': 12, 'distance_km': 5})

    def load(self):
        self.page.goto(self.url)
        expect(self.page.locator('#place option').first).to_be_attached()

    def submit(self):
        self.page.locator('#text').fill(TEXT)
        self.page.locator('#text').press('Enter')
        expect(self.page.locator('#planTitle')).to_be_visible()
        expect(self.page.locator('#go')).to_be_enabled()

    def assert_road(self):
        expect(self.page.locator('#mapNote')).to_contain_text('No live traffic')
        line = self.page.locator('#map path[stroke-width="5"]')
        expect(line).to_have_count(1)
        expect(line).to_have_attribute('d', re.compile(r'L.*L'))
        self.assertEqual(self.errors, [])

    def assert_stops_visible(self):
        self.page.wait_for_function("""() => {
            const map = document.querySelector('#map').getBoundingClientRect();
            return [...document.querySelectorAll('#map .pin:not(.pin-alt)')].every(pin => {
                const p = pin.getBoundingClientRect();
                return p.left >= map.left && p.right <= map.right && p.top >= map.top && p.bottom <= map.bottom;
            });
        }""")

    def test_enter_shift_enter_and_composition(self):
        self.load()
        field = self.page.locator('#text')
        field.fill(TEXT)
        field.press('Shift+Enter')
        self.assertTrue(field.input_value().endswith('\n'))
        field.dispatch_event('keydown', {'key': 'Enter', 'isComposing': True})
        self.assertEqual(self.searches, [])
        field.press('Enter')
        self.assert_road()
        self.assertEqual(len(self.searches), 1)

    def test_mobile_search_key_beforeinput(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.load()
        field = self.page.locator('#text')
        field.fill(TEXT)
        field.evaluate("el => el.dispatchEvent(new InputEvent('beforeinput', {inputType: 'insertLineBreak', isComposing: true, bubbles: true, cancelable: true}))")
        self.assertEqual(self.searches, [])
        field.evaluate("el => el.dispatchEvent(new InputEvent('beforeinput', {inputType: 'insertLineBreak', bubbles: true, cancelable: true}))")
        expect(self.page.locator('#planTitle')).to_be_visible()
        self.assert_road()

    def test_enter_while_places_are_loading(self):
        pending = []
        self.page.route('**/api/meta', lambda r: pending.append(r))
        self.page.goto(self.url)
        self.page.locator('#text').fill(TEXT)
        self.page.locator('#text').press('Enter')
        pending[0].continue_()
        expect(self.page.locator('#planTitle')).to_be_visible()
        self.assert_road()

    def test_example_and_enter_recover_after_meta_failure(self):
        self.page.route('**/api/meta', lambda r: r.fulfill(status=503, json={'error': 'temporary'}), times=1)
        self.page.goto(self.url)
        expect(self.page.locator('#notice')).to_be_visible()
        self.page.locator('[data-example="2"]').click()
        expect(self.page.locator('#text')).to_be_focused()
        self.page.keyboard.press('Enter')
        expect(self.page.locator('#planTitle')).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_can_reselect_geolocation(self):
        self.load()
        self.page.locator('#locate').click()
        expect(self.page.locator('#place')).to_have_value('__me')
        self.page.locator('#place').select_option(index=1)
        self.page.locator('#place').select_option('__me')
        self.submit()
        self.assertEqual(self.searches[-1]['lat'], 37.34)
        self.assertEqual(self.searches[-1]['lng'], -121.89)

    def test_stalled_request_releases_submit_button(self):
        pending = []
        self.page.clock.install()
        self.page.route('**/api/understand', lambda r: pending.append(r), times=1)
        self.load()
        self.page.locator('#text').fill(TEXT)
        with self.page.expect_request('**/api/understand'):
            self.page.locator('#text').press('Enter')
        self.page.clock.fast_forward(36000)
        expect(self.page.locator('#notice')).to_contain_text('timed out')
        expect(self.page.locator('#go')).to_be_enabled()
        pending[0].abort()

    def test_stalled_metadata_can_be_retried(self):
        pending = []
        self.page.clock.install()
        self.page.route('**/api/meta', lambda r: pending.append(r), times=1)
        self.page.goto(self.url)
        self.page.locator('#text').fill(TEXT)
        self.page.locator('#text').press('Enter')
        self.page.clock.fast_forward(13000)
        expect(self.page.locator('#formError')).to_contain_text('Could not load locations')
        expect(self.page.locator('#go')).to_be_enabled()
        pending[0].abort()
        self.submit()

    def test_form_edits_do_not_erase_displayed_route(self):
        self.load()
        self.submit()
        self.assert_road()
        self.page.locator('#radius').select_option('16')
        expect(self.page.locator('#map path[stroke-width="5"]')).to_have_count(1)

    def test_saved_plan_restores_search_location_and_radius(self):
        self.load()
        self.page.locator('#place').select_option(index=2)
        self.page.locator('#radius').select_option('16')
        self.submit()
        self.assert_road()
        original = self.searches[-1]
        self.page.reload()
        self.page.locator('[data-action="open-saved"]').click()
        expect(self.page.locator('#radius')).to_have_value('16')
        self.submit()
        for k in ('lat', 'lng', 'radius_km'):
            self.assertEqual(self.searches[-1][k], original[k])

    def test_invalid_geometry_keeps_fallback_and_can_retry(self):
        self.page.route('**/api/route', lambda r: r.fulfill(json={'available': True, 'path': [[None, 1]],
                                                               'duration_min': 12, 'distance_km': 5}), times=1)
        self.load()
        self.submit()
        expect(self.page.locator('#map path[stroke-width="4"]')).to_have_count(1)
        self.page.locator('[data-action="retry-road"]').click()
        self.assert_road()

    def test_server_routing_failure_is_shown_and_escaped(self):
        reason = 'The server could not verify the HTTPS certificate. <img src=x onerror=alert(1)>'
        self.page.route('**/api/route', lambda r: r.fulfill(json={'available': False, 'code': 'tls_certificate', 'reason': reason}))
        self.load()
        self.submit()
        expect(self.page.locator('#mapNote')).to_contain_text(reason)
        expect(self.page.locator('#mapNote img')).to_have_count(0)
        expect(self.page.locator('#map path[stroke-width="4"]')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_unreachable_route_api_does_not_claim_the_browser_is_offline(self):
        self.page.route('**/api/route', lambda r: r.abort())
        self.load()
        self.submit()
        expect(self.page.locator('#mapNote')).to_contain_text('Could not reach the server')
        expect(self.page.locator('#mapNote')).not_to_contain_text('You are offline')

    def test_old_route_response_cannot_overwrite_new_search(self):
        pending = []
        self.page.route('**/api/route', lambda r: pending.append(r), times=1)
        self.load()
        self.submit()
        self.page.route('**/api/understand', lambda r: r.fulfill(status=503, json={'error': 'temporary'}), times=1)
        self.page.locator('#text').press('Enter')
        expect(self.page.locator('#notice')).to_contain_text('temporary')
        self.road(pending[0])
        expect(self.page.locator('#mapLink')).to_be_hidden()
        expect(self.page.locator('#map path[stroke-width="5"]')).to_have_count(0)

    def test_route_survives_mobile_desktop_layout_changes(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.load()
        self.submit()
        self.assert_road()
        self.assert_stops_visible()

        self.page.set_viewport_size({'width': 1280, 'height': 900})
        expect(self.page.locator('#map')).to_be_visible()
        self.assert_road()
        self.assert_stops_visible()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assert_road()
        self.assert_stops_visible()

    def configure_test_google_key(self):
        def meta(route):
            response = route.fetch()
            data = response.json()
            data['maps'] = {'apiKey': 'test-browser-key', 'mapId': 'DEMO_MAP_ID'}
            route.fulfill(response=response, json=data)
        self.page.route('**/api/meta', meta)

    def test_google_download_failure_uses_labeled_backup(self):
        self.configure_test_google_key()
        self.page.route('https://maps.googleapis.com/maps/api/js?**', lambda r: r.fulfill(status=503, body='unavailable'))
        self.load()
        self.submit()
        expect(self.page.locator('#map')).to_have_attribute('data-provider', 'openstreetmap')
        expect(self.page.locator('#mapWarn')).to_contain_text('Google Maps unavailable')
        self.assert_road()

    def test_plan_created_while_google_is_loading_survives_fallback(self):
        self.configure_test_google_key()
        pending = []
        self.page.route('https://maps.googleapis.com/maps/api/js?**', lambda r: pending.append(r))
        self.load()
        self.submit()
        self.page.wait_for_function("() => [...document.scripts].some(s => s.src.startsWith('https://maps.googleapis.com/maps/api/js'))")
        pending[0].fulfill(status=503, body='unavailable')
        self.assert_road()
        self.assert_stops_visible()
        self.assertEqual(self.errors, [])

    def test_original_homepage_opens_existing_large_map_results(self):
        self.page.set_viewport_size({'width': 1440, 'height': 900})
        self.load()
        expect(self.page.locator('#hero .hero-art')).to_be_visible()
        expect(self.page.locator('#map')).to_be_hidden()
        form = self.page.locator('#form').bounding_box()
        self.assertGreater(form['width'], 1000)
        self.submit()
        box = self.page.locator('#map').bounding_box()
        self.assertGreater(box['width'], 1440 * .65)
        self.assertGreater(box['height'], 900 * .75)
        self.assertLess(box['y'], 130)
        self.page.locator('[data-action="expand-map"]').click()
        self.page.wait_for_function('!!document.fullscreenElement')
        self.page.locator('[data-action="expand-map"]').click()
        self.page.wait_for_function('!document.fullscreenElement')
        self.assert_road()
        self.page.locator('[data-action="recenter-map"]').click()
        self.assert_stops_visible()
        self.assertEqual(self.errors, [])

    def test_haven_navigation_and_dialog_keyboard_access(self):
        self.load()
        self.page.locator('[data-action="how"]').click()
        expect(self.page.get_by_role('dialog')).to_be_visible()
        self.page.keyboard.press('Escape')
        expect(self.page.get_by_role('dialog')).to_be_hidden()
        self.submit()
        self.page.locator('[data-action="save-list"]').click()
        self.page.locator('.results-toolbar [data-action="new-request"]').click()
        expect(self.page.locator('#hero')).to_be_visible()
        expect(self.page.locator('#results')).to_be_hidden()
        expect(self.page.locator('#text')).to_be_focused()
        self.page.locator('[data-action="saved-list"]').click()
        expect(self.page.locator('.saved-card')).to_be_visible()
        self.page.locator('[data-action="open-list"]').click()
        expect(self.page.locator('#planTitle')).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_haven_checklist_persists_and_downloads(self):
        self.load()
        self.submit()
        first = self.page.locator('[data-collected]').first
        key = first.get_attribute('data-collected')
        first.check()
        self.page.locator('[data-action="save-list"]').click()
        self.page.reload()
        self.page.locator('[data-action="saved-list"]').click()
        self.page.locator('[data-action="open-list"]').click()
        expect(self.page.locator(f'[data-collected="{key}"]')).to_be_checked()
        with self.page.expect_download() as event:
            self.page.locator('[data-action="download"]').click()
        download = event.value
        self.assertEqual(download.suggested_filename, 'haven-supply-plan.txt')
        text = Path(download.path()).read_text()
        self.assertIn('SUPPLY CHECKLIST', text)
        self.assertIn('[x]', text)
        self.assertEqual(self.errors, [])

    def test_haven_checklist_category_filters_and_saved_list_deletion(self):
        self.load()
        self.submit()
        category = self.page.locator('[data-action="category"]').nth(1)
        selected = category.get_attribute('data-category')
        category.click()
        visible = self.page.locator('.supply-row:visible')
        self.assertGreater(visible.count(), 0)
        self.assertTrue(all(row.get_attribute('data-category') == selected for row in visible.all()))
        self.page.locator('[data-action="category"]').first.click()
        self.page.locator('[data-action="save-list"]').click()
        self.page.locator('[data-action="saved-list"]').click()
        expect(self.page.locator('.saved-card')).to_have_count(1)
        self.page.locator('[data-action="delete-list"]').click()
        expect(self.page.locator('.empty-saved')).to_be_visible()
        expect(self.page.locator('.saved-card')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_haven_store_pins_open_matching_inventory(self):
        self.load()
        self.submit()
        self.assert_road()
        marker = self.page.locator('#map .pin:not(.pin-you):not(.pin-alt)').first
        marker.click()
        expect(self.page.locator('.stock-popup h3')).to_be_visible()
        expect(self.page.locator('.stock-popup .popup-inventory li').first).to_be_visible()
        expect(self.page.locator('.stock-popup')).to_contain_text('Demo inventory')
        self.assertEqual(self.errors, [])

    def test_haven_no_horizontal_overflow_across_screen_sizes(self):
        self.load()
        for width in (360, 390, 768, 980, 1280):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), width)
        self.submit()
        for width in (360, 390, 768, 980, 1280):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), width)
        self.assertEqual(self.errors, [])

    def service_worker_page(self):
        self.context.close()
        self.context = self.browser.new_context(service_workers='allow')
        self.page = self.context.new_page()
        self.page.route('https://tile.openstreetmap.org/**', lambda r: r.abort())
        self.page.clock.set_fixed_time(datetime.datetime(2026, 10, 23, 17, tzinfo=datetime.timezone.utc))
        self.page.on('pageerror', lambda err: self.errors.append(str(err)))
        self.page.goto(self.url)
        self.page.evaluate('() => navigator.serviceWorker.ready')
        self.page.wait_for_function('() => !!navigator.serviceWorker.controller')

    def test_service_worker_revalidates_previously_cached_assets(self):
        self.service_worker_page()
        QuietLocal.asset_version = 'one'
        fetch_asset = "async () => (await fetch('/static/cache-probe.js')).text()"
        self.assertEqual(self.page.evaluate(fetch_asset), 'one')
        QuietLocal.asset_version = 'two'
        self.assertEqual(self.page.evaluate(fetch_asset), 'two')

    def test_offline_shell_opens_saved_plan_and_never_serves_html_as_script(self):
        self.service_worker_page()
        self.submit()
        self.context.set_offline(True)
        self.page.reload()
        self.page.locator('[data-action="open-saved"]').click()
        expect(self.page.locator('#planTitle')).to_be_visible()
        result = self.page.evaluate("async () => { try { const r = await fetch('/static/missing.js'); return r.headers.get('Content-Type'); } catch { return 'network-error'; } }")
        self.assertEqual(result, 'network-error')
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
