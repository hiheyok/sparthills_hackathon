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
import time
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
        self.errors, self.searches, self.paths, self.calls = [], [], [], []
        self.page.on('request', lambda req: self.calls.append((req.url.split('/api/')[1].split('?')[0], req.post_data_json))
                     if '/api/' in req.url and req.method == 'POST' else None)
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
        expect(self.page.locator('#mapNote')).to_contain_text('no live traffic')
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
        self.assertEqual(download.suggested_filename, 'SupplyLine-supply-plan.txt')
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
    # ---- driving and walking: two plans, built together, one switch ----
    def api_calls(self, name):
        return [body for path, body in self.calls if path == name]

    def switch(self, mode):
        self.page.locator(f'#r-mode [data-mode="{mode}"]').click()

    def lane_ready(self, mode):
        expect(self.page.locator(f'#r-mode [data-mode="{mode}"] .mode-sub')).not_to_contain_text('working')

    def test_both_plans_are_built_together_and_switching_asks_nobody(self):
        self.load()
        self.submit()
        self.assert_road()
        self.lane_ready('walking')
        expect(self.page.locator('#r-mode [data-mode="driving"]')).to_have_attribute('aria-pressed', 'true')
        asked = len(self.calls)
        self.switch('walking')
        expect(self.page.locator('#r-mode [data-mode="walking"]')).to_have_attribute('aria-pressed', 'true')
        expect(self.page.locator('.facts-row')).to_contain_text('walking')
        expect(self.page.locator('#r-route .where-line').first).to_contain_text('walk')
        expect(self.page.locator('#planTitle')).to_contain_text('on foot')
        expect(self.page.locator('#mapNote')).to_contain_text('Walking route:')
        # the walking server's usage policy asks for the OpenStreetMap credit and a fix-the-map link wherever its routes show
        expect(self.page.locator('#mapNote a')).to_have_attribute('href', 'https://www.openstreetmap.org/fixthemap')
        expect(self.page.locator('.leaflet-control-attribution')).to_contain_text('OpenStreetMap')
        line = self.page.locator('#map path[stroke-width="5"]')
        expect(line).to_have_count(1)
        expect(line).to_have_attribute('stroke', '#0b6e4c')
        expect(self.page.locator('#r-route .stop-actions a').first).to_have_attribute('href', re.compile('travelmode=walking'))
        expect(self.page.locator('#mapLink')).to_have_attribute('href', re.compile('travelmode=walking'))
        stages = [p for p, _ in self.calls[asked:] if p in ('understand', 'search', 'alternatives', 'brief')]
        self.assertEqual(stages, [])                                # the other plan was already built
        self.switch('driving')
        expect(self.page.locator('#mapNote')).to_contain_text('Fastest road route:')
        expect(self.page.locator('#map path[stroke-width="5"]')).to_have_attribute('stroke', '#0a4da2')
        expect(self.page.locator('#r-route .stop-actions a').first).to_have_attribute('href', re.compile('travelmode=driving'))
        self.assertEqual(self.errors, [])

    def test_each_way_of_travelling_gets_its_own_agents_and_the_screen_lane_gets_its_path_first(self):
        self.load()
        self.submit()
        self.lane_ready('walking')
        deadline = time.time() + 5
        while len(self.api_calls('route')) < 2 and time.time() < deadline:
            self.page.wait_for_timeout(50)
        self.assertEqual([b['mode'] for b in self.api_calls('route')[:2]], ['driving', 'walking'])
        self.assertEqual(sorted(b['mode'] for b in self.api_calls('brief')), ['driving', 'walking'])
        # bottled water is out downtown for both, so each lane asks its own substitutes agent
        self.assertEqual(sorted(b['mode'] for b in self.api_calls('alternatives')), ['driving', 'walking'])
        self.assertEqual(len(self.searches), 1)                   # still one search per request

    def test_the_traces_name_the_agent_and_the_lane(self):
        self.load()
        self.submit()
        self.lane_ready('walking')
        self.page.locator('#r-trace summary').click()
        trace = self.page.locator('#r-trace')
        expect(trace).to_contain_text('Write your briefing, driving')
        expect(trace).to_contain_text('Write your briefing, walking')

    def test_a_failing_walking_plan_leaves_driving_alone_and_can_be_retried(self):
        failing = {'on': True}

        def brief(route):
            if failing['on'] and route.request.post_data_json.get('mode') == 'walking':
                route.fulfill(status=503, json={'error': 'The walking plan is temporarily unavailable.'})
            else:
                route.continue_()
        self.page.route('**/api/brief', brief)
        self.load()
        self.submit()
        self.assert_road()
        expect(self.page.locator('#r-mode [data-mode="walking"] .mode-sub')).to_have_text('unavailable')
        expect(self.page.locator('#go')).to_be_enabled()
        self.switch('walking')
        expect(self.page.locator('#r-plan')).to_contain_text('temporarily unavailable')
        expect(self.page.locator('#map path[stroke-width="5"]')).to_have_count(0)      # not driving's route on a walking screen
        failing['on'] = False
        self.page.locator('[data-action="retry-lane"]').click()
        expect(self.page.locator('#r-plan .facts-row')).to_contain_text('walking')
        expect(self.page.locator('#r-mode [data-mode="walking"] .mode-sub')).not_to_have_text('unavailable')
        self.assertEqual(self.errors, [])

    def test_main_request_box_stays_available_without_an_extra_question_form(self):
        self.load()
        self.submit()
        expect(self.page.locator('#text')).to_be_visible()
        expect(self.page.locator('#text')).to_be_editable()
        expect(self.page.locator('#fuForm, #r-followup')).to_have_count(0)
        expect(self.page.locator('header .brand')).to_have_text('SupplyLine')
        expect(self.page).to_have_title('SupplyLine · Find essential supplies')
        self.assertEqual(self.api_calls('followup'), [])
        self.assertEqual(self.errors, [])

    def test_plan_assistant_uses_both_plans_and_labels_the_real_fallback(self):
        self.load()
        self.submit()
        button = self.page.locator('#askPlan')
        expect(button).to_be_enabled()
        button.click()
        expect(self.page.locator('#assistantQuestion')).to_be_focused()
        self.page.get_by_role('button', name='Walk or drive?', exact=True).click()
        expect(self.page.locator('#assistantAnswer')).to_contain_text('Driving (shown):')
        expect(self.page.locator('#assistantAnswer')).to_contain_text('Walking')
        expect(self.page.locator('#assistantSource')).to_have_text('Planner response · AI is turned off')
        self.assertEqual(len(self.api_calls('followup')), 1)
        body = self.api_calls('followup')[0]
        self.assertTrue(body['intent']['needs'])
        self.assertEqual(set(body['alternatives']), {'driving', 'walking'})
        self.assertEqual(body['mode'], 'driving')
        self.assertEqual(len(self.searches), 1)  # asking does not submit another stock search
        self.page.locator('#assistantEvidence summary').click()
        expect(self.page.locator('#assistantMetrics')).to_contain_text('Rules fallback')
        self.page.keyboard.press('Escape')
        expect(button).to_be_focused()
        button.click()
        expect(self.page.locator('#assistantReply')).to_be_visible()
        self.page.keyboard.press('Escape')
        self.switch('walking')
        button.click()
        expect(self.page.locator('#assistantReply')).to_be_hidden()
        expect(self.page.locator('#assistantContext')).to_contain_text('Walk plan')
        self.assertEqual(self.errors, [])

    def test_plan_assistant_shows_model_evidence_and_renders_answers_as_text(self):
        pending = []
        self.page.route('**/api/followup', lambda route: pending.append(route))
        self.page.set_viewport_size({'width': 360, 'height': 800})
        self.load()
        self.submit()
        expect(self.page.locator('#askPlan')).to_be_enabled()
        self.page.locator('#askPlan').click()
        field = self.page.locator('#assistantQuestion')
        field.fill('Which store first?')
        field.press('Shift+Enter')
        self.assertTrue(field.input_value().endswith('\n'))
        self.assertEqual(self.api_calls('followup'), [])
        field.press('Enter')
        expect(self.page.locator('#assistantSend')).to_be_disabled()
        expect(self.page.locator('#assistantStatus')).to_be_visible()
        self.assertEqual(len(pending), 1)
        answer = 'Start with your first planned store. <img src=x onerror=alert(1)>'
        pending[0].fulfill(json={'answer': answer, 'grounded': True, 'trace': {
            'stage': 'followup', 'mode': 'bedrock', 'model': 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
            'region': 'us-east-1', 'ms': 1100, 'tokens_in': 99, 'tokens_out': 20}})
        expect(self.page.locator('#assistantAnswer')).to_have_text(answer)
        expect(self.page.locator('#assistantAnswer img')).to_have_count(0)
        expect(self.page.locator('#assistantSource')).to_have_text('Amazon Bedrock · Claude Haiku 4.5')
        self.page.locator('#assistantEvidence summary').click()
        expect(self.page.locator('#assistantMetrics')).to_contain_text('99 / 20')
        expect(self.page.locator('#assistantSend')).to_be_enabled()
        self.assertTrue(self.page.locator('#planAssistant').evaluate('(e) => e.scrollWidth <= e.clientWidth + 1'))
        self.assertEqual(self.errors, [])

    def test_plan_assistant_network_failure_can_retry(self):
        self.page.route('**/api/followup', lambda route: route.abort())
        self.load()
        self.submit()
        expect(self.page.locator('#askPlan')).to_be_enabled()
        self.page.locator('#askPlan').click()
        self.page.locator('#assistantQuestion').fill('Which store first?')
        self.page.locator('#assistantSend').click()
        expect(self.page.locator('#assistantError')).to_be_visible()
        expect(self.page.locator('#assistantSend')).to_be_enabled()
        expect(self.page.locator('#assistantReply')).to_be_hidden()
        self.page.unroute('**/api/followup')
        self.page.locator('#assistantSend').click()
        expect(self.page.locator('#assistantReply')).to_be_visible()
        expect(self.page.locator('#assistantError')).to_be_hidden()
        expect(self.page.locator('#assistantSource')).to_contain_text('Planner response')
        self.assertEqual(self.errors, [])

    def test_old_plan_answer_does_not_appear_in_a_new_plan(self):
        pending = []
        self.page.route('**/api/followup', lambda route: pending.append(route))
        self.load()
        self.submit()
        expect(self.page.locator('#askPlan')).to_be_enabled()
        self.page.locator('#askPlan').click()
        self.page.locator('#assistantQuestion').fill('Which store first?')
        self.page.locator('#assistantSend').click()
        expect(self.page.locator('#assistantSend')).to_be_disabled()
        self.page.keyboard.press('Escape')
        self.page.locator('.results-toolbar [data-action="new-request"]').click()
        self.submit()
        expect(self.page.locator('#askPlan')).to_be_enabled()
        self.page.locator('#askPlan').click()
        pending[0].fulfill(json={'answer': 'OLD ANSWER', 'grounded': True, 'trace': {'mode': 'rules'}})
        expect(self.page.locator('#assistantReply')).to_be_hidden()
        expect(self.page.locator('#assistantSend')).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_store_hours_update_in_popup_and_start_summary_at_closing_and_opening(self):
        def brief(route):
            response = route.fetch()
            result = response.json()
            if result['plan']['route']['stops']:
                store = result['plan']['route']['stops'][0]['store']
                store.update(hours={'open': 480, 'close': 1200}, status='open')
            route.fulfill(response=response, json=result)
        self.page.route('**/api/brief', brief)
        self.load()
        self.submit()
        self.lane_ready('walking')
        first = self.page.evaluate("JSON.parse(localStorage.getItem('supplyline.last.v1')).lanes.driving.result.plan.route.stops[0].store")
        self.page.locator(f'#r-items .store-option[data-store="{first["id"]}"]').first.click()
        summary = self.page.locator('#r-plan .plan-hours')
        popup = self.page.locator('.stock-popup .popup-hours')
        expect(summary).to_have_text('Open · closes at 8:00 PM')
        expect(popup).to_have_text('Open · closes at 8:00 PM')
        self.page.clock.set_fixed_time(datetime.datetime(2026, 10, 24, 3, tzinfo=datetime.timezone.utc))  # 8 PM Pacific
        self.page.clock.fast_forward(15001)
        expect(summary).to_have_text('Closed · opens tomorrow at 8:00 AM')
        expect(popup).to_have_text('Closed · opens tomorrow at 8:00 AM')
        self.page.clock.set_fixed_time(datetime.datetime(2026, 10, 24, 15, tzinfo=datetime.timezone.utc))  # 8 AM Pacific
        self.page.clock.fast_forward(15001)
        expect(summary).to_have_text('Open · closes at 8:00 PM')
        expect(popup).to_have_text('Open · closes at 8:00 PM')
        self.assertEqual(self.errors, [])

    def test_store_hours_handle_all_day_and_unscheduled_closures(self):
        selected = {'hours': {'always': True}, 'status': 'open'}
        def brief(route):
            response = route.fetch()
            result = response.json()
            if result['plan']['route']['stops']:
                result['plan']['route']['stops'][0]['store'].update(selected)
            route.fulfill(response=response, json=result)
        self.page.route('**/api/brief', brief)
        self.load()
        self.submit()
        expect(self.page.locator('#r-plan .plan-hours')).to_have_text('Open 24 hours')
        self.lane_ready('walking')
        selected.update(hours={'open': 480, 'close': 1200}, status='closed')
        self.submit()
        expect(self.page.locator('#r-plan .plan-hours')).to_have_text('Closed · reopening time unconfirmed')
        self.assertEqual(self.errors, [])

    def test_a_saved_plan_keeps_both_plans(self):
        self.load()
        self.submit()
        self.lane_ready('walking')
        self.page.wait_for_function("""() => { const s = JSON.parse(localStorage.getItem('supplyline.last.v1') || 'null');
                                              return !!(s && s.lanes && s.lanes.driving && s.lanes.walking); }""")
        self.page.reload()
        self.page.locator('[data-action="open-saved"]').click()
        expect(self.page.locator('#planTitle')).to_be_visible()
        expect(self.page.locator('#r-mode [data-mode="walking"] .mode-sub')).not_to_have_text('unavailable')
        self.switch('walking')
        expect(self.page.locator('.facts-row')).to_contain_text('walking')
        expect(self.page.locator('#map path[stroke-width="5"]')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_a_plan_saved_before_there_were_two_plans_still_opens_and_can_build_the_walk(self):
        self.load()
        intent = self.page.request.post(self.url + '/api/understand', data={'text': TEXT}).json()['intent']
        body = {'lat': 37.3382, 'lng': -121.8863, 'radius_km': 8, 'local_minutes': 600, 'intent': intent}
        brief = self.page.request.post(self.url + '/api/brief', data=body).json()
        brief.pop('mode', None)
        brief['plan'].pop('mode', None)
        for key in ('mode', 'total_travel_min'):
            brief['plan']['route'].pop(key, None)
        old = {'savedAt': 1, 'result': brief, 'intent': intent, 'traces': [], 'text': TEXT, 'radius': 8, 'origin': 'Downtown'}
        self.page.evaluate('(saved) => localStorage.setItem("supplyline.last.v1", JSON.stringify(saved))', old)
        self.page.reload()
        self.page.locator('[data-action="open-saved"]').click()
        expect(self.page.locator('#planTitle')).to_be_visible()
        expect(self.page.locator('#r-mode [data-mode="driving"]')).to_have_attribute('aria-pressed', 'true')
        expect(self.page.locator('.facts-row')).to_contain_text('driving')
        expect(self.page.locator('#r-mode [data-mode="walking"] .mode-sub')).to_have_text('unavailable')
        self.switch('walking')
        expect(self.page.locator('#r-plan')).to_contain_text('not saved')
        self.page.locator('[data-action="retry-lane"]').click()
        expect(self.page.locator('#r-plan .facts-row')).to_contain_text('walking')
        self.assertEqual(self.errors, [])

    def test_the_way_of_travelling_is_remembered_and_leads_the_next_search(self):
        self.load()
        self.submit()
        self.lane_ready('walking')
        self.switch('walking')
        self.page.reload()
        self.submit()
        expect(self.page.locator('#r-mode [data-mode="walking"]')).to_have_attribute('aria-pressed', 'true')
        expect(self.page.locator('.facts-row')).to_contain_text('walking')
        self.assertEqual(self.searches[-1]['mode'], 'walking')
        self.lane_ready('driving')
        self.switch('driving')
        expect(self.page.locator('.facts-row')).to_contain_text('driving')

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

    def test_results_panel_drag_persists_and_keyboard_resize_keeps_map_visible(self):
        self.page.set_viewport_size({'width': 1440, 'height': 900})
        self.load()
        expect(self.page.locator('#panelDivider')).to_be_hidden()
        self.submit()
        divider = self.page.locator('#panelDivider')
        expect(divider).to_be_visible()
        panel = self.page.locator('.col-main')
        initial = panel.bounding_box()['width']
        box = divider.bounding_box()
        self.page.mouse.move(box['x'] + box['width'] / 2, box['y'] + 100)
        self.page.mouse.down()
        self.page.mouse.move(box['x'] + 226, box['y'] + 100, steps=8)
        self.page.mouse.up()
        self.assertGreater(panel.bounding_box()['width'], initial + 180)
        saved_width = self.page.evaluate('JSON.parse(localStorage.getItem("haven.panel-width.v1"))')
        self.page.reload()
        self.submit()
        self.assertAlmostEqual(panel.bounding_box()['width'], saved_width, delta=2)
        divider.focus()
        self.page.keyboard.press('Home')
        self.assertAlmostEqual(panel.bounding_box()['width'], 320, delta=2)
        self.page.keyboard.press('End')
        self.assertGreaterEqual(self.page.locator('#map').bounding_box()['width'], 318)
        self.assert_road()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        expect(divider).to_be_hidden()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.errors, [])

    def test_closest_store_has_routed_miles_and_minutes_for_each_travel_mode(self):
        def route_for_mode(route):
            data = route.request.post_data_json
            path = [[p['lat'], p['lng']] for p in data['points']]
            walk = data['mode'] == 'walking'
            route.fulfill(json={'available': True, 'path': path, 'duration_min': 21 if walk else 12,
                                'distance_km': 1.4 if walk else 5})
        self.page.route('**/api/route', route_for_mode)
        self.load()
        self.submit()
        expect(self.page.locator('.closest-measures [aria-pressed="true"] .mode-sub')).to_have_text('3.1 mi · 12 min')
        expect(self.page.locator('.closest-store')).to_contain_text('Routed distance')
        self.lane_ready('walking')
        self.switch('walking')
        expect(self.page.locator('.closest-measures [aria-pressed="true"] .mode-sub')).to_have_text('0.9 mi · 21 min')
        expect(self.page.locator('.closest-store > a')).to_have_attribute('href', re.compile('travelmode=walking'))
        self.page.wait_for_function("""() => {const s = JSON.parse(localStorage.getItem('supplyline.last.v1'));
            return s?.lanes.walking?.closest && s?.lanes.driving?.closest;}""")
        self.page.reload()
        self.page.locator('[data-action="open-saved"]').click()
        expect(self.page.locator('.closest-measures [aria-pressed="true"] .mode-sub')).to_have_text('0.9 mi · 21 min')
        self.assertEqual(self.errors, [])

    def test_closest_store_uses_a_labelled_estimate_when_routes_are_unavailable(self):
        self.page.route('**/api/route', lambda r: r.fulfill(json={'available': False, 'reason': 'Temporarily unavailable'}))
        self.load()
        self.submit()
        expect(self.page.locator('.closest-measures [aria-pressed="true"] .mode-sub')).to_contain_text('Est.')
        expect(self.page.locator('.closest-store')).to_contain_text('Estimated distance and time')
        expect(self.page.locator('.closest-store')).not_to_contain_text('Routed distance')
        self.assertEqual(self.errors, [])

    def test_store_browser_ranks_times_popups_and_saved_selection_stay_matched(self):
        def route_for_destination(route):
            data = route.request.post_data_json
            destination = data['points'][-1]
            distance = round(abs(destination['lat'] - 37) * 100, 2)
            walk = data['mode'] == 'walking'
            route.fulfill(json={'available': True,
                'path': [[p['lat'], p['lng']] for p in data['points']],
                'distance_km': distance * (1 if walk else 2),
                'duration_min': distance * (20 if walk else 3)})
        self.page.route('**/api/route', route_for_destination)
        self.load()
        self.submit()
        self.lane_ready('walking')
        card = self.page.locator('.closest-store')
        expect(card.locator('.travel-status')).to_have_text('Routed distance · no live traffic')
        expect(card.locator('[data-action="previous-store"]')).to_have_count(0)
        expect(card.locator('.store-rank')).to_have_text('1')
        first = card.get_attribute('data-current-store')
        first_values = card.locator('.mode-sub').all_text_contents()
        card.locator('[data-action="next-store"]').click()
        expect(card.locator('.store-rank')).to_have_text('2')
        expect(self.page.locator('#map .pin.is-selected')).to_have_text('2')
        expect(card.locator('.travel-status')).to_have_text('Routed distance · no live traffic')
        second = card.get_attribute('data-current-store')
        self.assertNotEqual(first, second)
        second_values = card.locator('.mode-sub').all_text_contents()
        self.assertNotEqual(first_values, second_values)
        expect(card.locator('[data-action="previous-store"]')).to_have_count(1)
        ranks = self.page.locator('#map .pin:not(.pin-you)').all_text_contents()
        self.assertEqual(sorted(int(rank) for rank in ranks), list(range(1, len(ranks) + 1)))
        for width in (1440, 390):
            self.page.set_viewport_size({'width': width, 'height': 900})
            card.locator('[data-action="show-store"]').click()
            popup = self.page.locator('.stock-popup')
            expect(popup.locator('.store-rank')).to_have_text('2')
            self.assertEqual(popup.locator('.popup-travel b').all_text_contents(), second_values)
            self.assertTrue(popup.evaluate('(e) => e.scrollWidth <= e.clientWidth + 1'))
            self.assertTrue(popup.locator('.popup-travel').evaluate('''e => {
                const box = e.getBoundingClientRect();
                const rows = [...e.querySelectorAll('.popup-travel-mode')];
                return rows[1].getBoundingClientRect().top >= rows[0].getBoundingClientRect().bottom
                    && rows.every(row => [...row.children].every(child => {
                        const b = child.getBoundingClientRect();
                        return b.left >= box.left && b.right <= box.right && child.scrollWidth <= child.clientWidth + 1;
                    }));
            }'''))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.page.reload()
        self.page.locator('[data-action="open-saved"]').click()
        expect(card).to_have_attribute('data-current-store', second)
        self.assertEqual(card.locator('.mode-sub').all_text_contents(), second_values)
        card.locator('[data-action="previous-store"]').click()
        expect(card).to_have_attribute('data-current-store', first)
        expect(self.page.locator('#map .pin.is-selected')).to_have_text('1')
        expect(card.locator('[data-action="previous-store"]')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_long_waypoint_store_names_wrap_inside_popup_on_desktop_and_mobile(self):
        long_name = 'Neighborhood Emergency Supply Distribution Center ' + 'VeryLongStoreName' * 12
        def brief(route):
            response = route.fetch()
            result = response.json()
            if result['plan']['route']['stops']:
                result['plan']['route']['stops'][0]['store']['name'] = long_name
            route.fulfill(response=response, json=result)
        self.page.route('**/api/brief', brief)
        self.load()
        self.submit()
        self.assert_road()
        for width in (1440, 390):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.page.locator('#map').scroll_into_view_if_needed()
            self.page.locator('#map .pin:not(.pin-you):not(.pin-alt)').first.click()
            title = self.page.locator('.stock-popup h3')
            expect(title).to_have_text(long_name)
            self.assertTrue(title.evaluate('(e) => e.scrollWidth <= e.clientWidth + 1'))
            self.assertLessEqual(title.bounding_box()['height'], 38)
            self.page.locator('.popup-full-name summary').click()
            expect(self.page.locator('.popup-full-name p')).to_have_text(long_name)
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            self.page.locator('.leaflet-popup-close-button').click()
            expect(self.page.locator('.stock-popup')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_map_fullscreen_fallback_exits_with_escape(self):
        self.load()
        self.submit()
        self.page.locator('.col-map').evaluate("(e) => e.requestFullscreen = () => Promise.reject(new Error('Unsupported'))")
        button = self.page.locator('#expandMap')
        expect(button).to_contain_text('Full screen')
        button.click()
        expect(self.page.locator('.col-map')).to_have_class(re.compile('is-expanded'))
        expect(button).to_have_attribute('aria-pressed', 'true')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('.col-map')).not_to_have_class(re.compile('is-expanded'))
        expect(button).to_have_attribute('aria-pressed', 'false')
        self.assert_road()
        self.assertEqual(self.errors, [])



    def test_page_stops_at_footer_while_desktop_results_scroll_independently(self):
        self.page.set_viewport_size({'width': 1440, 'height': 900})
        self.load()
        self.submit()
        for width, height in ((1440, 900), (980, 600)):
            self.page.set_viewport_size({'width': width, 'height': height})
            self.page.locator('.col-main').evaluate('e => e.scrollTop = e.scrollHeight')
            self.page.evaluate('window.scrollTo({top: 100000, behavior: "instant"})')
            self.assertEqual(self.page.evaluate('scrollY'), 0)
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollHeight'), height + 1)
            self.assertTrue(self.page.locator('footer').evaluate('(e) => e.getBoundingClientRect().bottom <= innerHeight + 1'))
            self.assertGreater(self.page.locator('.col-main').evaluate('(e) => e.scrollTop'), 0)
        self.page.locator('[data-action="save-list"]').click()
        self.page.locator('[data-action="saved-list"]').click()
        expect(self.page.locator('.saved-card')).to_be_visible()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.locator('[data-action="open-list"]').click()
        self.page.evaluate('window.scrollTo({top: document.documentElement.scrollHeight, behavior: "instant"})')
        self.assertTrue(self.page.locator('footer').evaluate('(e) => Math.abs(e.getBoundingClientRect().bottom - innerHeight) < 2'))
        self.assertEqual(self.page.evaluate('getComputedStyle(document.documentElement).overscrollBehaviorY'), 'none')
        self.assertEqual(self.errors, [])



    def test_store_gaps_and_alternatives_share_one_list_without_repeating_request(self):
        self.page.clock.set_fixed_time(datetime.datetime(2026, 10, 24, 4, tzinfo=datetime.timezone.utc))
        self.load()
        self.page.locator('#text').fill('A storm took out our power three days ago. We need D batteries for our lantern and propane for the camp stove.')
        self.page.locator('#text').press('Enter')
        expect(self.page.locator('#planTitle')).to_be_visible()
        self.lane_ready('walking')
        self.switch('walking')
        expect(self.page.locator('.request-quote')).to_have_count(0)
        expect(self.page.locator('#r-alts')).to_have_count(0)
        expect(self.page.locator('#r-items > details > summary')).to_have_text('Nearby store options')
        expect(self.page.locator('#r-items [data-item]')).to_have_count(4)
        expect(self.page.locator('#r-items .chip.bad')).to_have_count(4)
        expect(self.page.locator('#r-items .chip.ok')).to_have_count(1)
        expect(self.page.locator('#r-items .chip.ok')).to_have_text('Alternatives available')
        lantern = self.page.locator('#r-items [data-has-alternatives]')
        expect(lantern.locator('.item-head h4')).to_have_text('LED lantern (battery)')
        expect(lantern.locator('.opt h4').first).to_contain_text('LED flashlight')
        expect(lantern).to_contain_text('Gives light but cannot light a whole room.')
        expect(lantern).to_contain_text('Check which batteries it takes.')
        saved = self.page.evaluate("JSON.parse(localStorage.getItem('supplyline.last.v1'))")
        alternative = next(a for a in saved['lanes']['walking']['result']['plan']['alternatives'] if a['options'])
        expect(lantern.locator('.near').first).to_contain_text(alternative['options'][0]['stores'][0]['name'])
        expect(self.page.locator('#r-items')).not_to_contain_text('Nobody near you has')
        self.page.locator('[data-action="alternatives"]').click()
        expect(lantern).to_be_in_viewport()
        self.page.locator('[data-action="save-list"]').click()
        self.page.reload()
        self.page.locator('[data-action="saved-list"]').click()
        self.page.locator('[data-action="open-list"]').click()
        expect(self.page.locator('#r-items [data-item]')).to_have_count(4)
        expect(self.page.locator('.request-quote')).to_have_count(0)
        self.assertEqual(self.errors, [])



    def test_store_rows_and_item_containers_open_matching_map_pins(self):
        self.page.set_viewport_size({'width': 1440, 'height': 900})
        self.load()
        self.submit()
        self.assert_road()
        option = self.page.locator('#r-items .store-option').nth(1)
        name = option.locator('strong').inner_text()
        option.click()
        expect(self.page.locator('.stock-popup h3')).to_have_attribute('title', name)
        expect(self.page.locator('#map .pin.is-selected')).to_have_count(1)
        expect(self.page.locator('#r-plan > .lede')).to_have_count(0)
        self.page.locator('#r-items .item-map-target').first.focus()
        self.page.keyboard.press('Enter')
        expect(self.page.locator('.stock-popup h3')).to_have_count(1)
        expect(self.page.locator('.stock-popup h3')).to_be_visible()
        title_size = self.page.locator('#r-items > details > summary').evaluate('(e) => parseFloat(getComputedStyle(e).fontSize)')
        item_size = self.page.locator('#r-items .item-head h4').first.evaluate('(e) => parseFloat(getComputedStyle(e).fontSize)')
        self.assertGreater(title_size, item_size)
        self.assertEqual(self.errors, [])

    def test_alternative_card_scrolls_mobile_to_map_and_opens_its_store(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.clock.set_fixed_time(datetime.datetime(2026, 10, 24, 4, tzinfo=datetime.timezone.utc))
        self.load()
        self.page.locator('#text').fill('A storm took out our power three days ago. We need D batteries for our lantern and propane for the camp stove.')
        self.page.locator('#text').press('Enter')
        expect(self.page.locator('#planTitle')).to_be_visible()
        self.lane_ready('walking')
        self.switch('walking')
        option = self.page.locator('#r-items .option-map-target').last
        store_id = option.get_attribute('data-store')
        saved = self.page.evaluate("JSON.parse(localStorage.getItem('supplyline.last.v1'))")
        stores = [store for alternative in saved['lanes']['walking']['result']['plan']['alternatives'] for item in alternative['options'] for store in item['stores']]
        name = next(store['name'] for store in stores if store['id'] == store_id)
        option.click()
        expect(self.page.locator('#map')).to_be_in_viewport()
        expect(self.page.locator('.stock-popup h3')).to_have_attribute('title', name)
        self.assertEqual(self.errors, [])



    def test_route_finishing_does_not_pan_away_from_selected_store(self):
        self.page.set_viewport_size({'width': 1440, 'height': 900})
        pending = []
        self.page.route('**/api/route', lambda r: pending.append(r), times=1)
        self.load()
        self.submit()
        row = self.page.locator('#r-items .store-option').last
        name = row.locator('strong').inner_text()
        row.click()
        expect(self.page.locator('.stock-popup h3')).to_have_attribute('title', name)
        self.road(pending[0])
        expect(self.page.locator('#mapNote')).to_contain_text('no live traffic')
        expect(self.page.locator('#map path[stroke-width="5"]')).to_have_count(1)
        expect(self.page.locator('#map .pin.is-selected')).to_be_in_viewport()
        expect(self.page.locator('.stock-popup h3')).to_have_attribute('title', name)
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
