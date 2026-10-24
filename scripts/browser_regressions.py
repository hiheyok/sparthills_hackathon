#!/usr/bin/env python3
"""Real Chrome checks. Run: python3 scripts/browser_regressions.py

Requires `pip install playwright` and Chrome (or BROWSER_CHANNEL=chromium with
`playwright install chromium`). Uses an isolated browser and a local server;
AI, road routing and map tiles make no external requests.
"""
import datetime
import json
import os
import re
import sys
import threading
import time
import traceback
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

# What the page looked like when a test failed. Short on purpose: it ends up in a CI annotation.
PAGE_STATE = """() => {
  const text = (sel) => { const e = document.querySelector(sel); return e ? e.innerText.replace(/\\s+/g, ' ').trim().slice(0, 140) : null; };
  const go = document.querySelector('#go');
  return {
    viewport: innerWidth + 'x' + innerHeight, notice: text('#notice'), formError: text('#formError'),
    plan: text('#planTitle'), mapNote: text('#mapNote'), go: go ? go.textContent.trim() + (go.disabled ? ' (disabled)' : '') : null,
    modes: [...document.querySelectorAll('#r-mode .mode-btn')].map((b) => b.dataset.mode + ' pressed=' + b.getAttribute('aria-pressed') + ' ' + b.querySelector('.mode-sub').innerText),
    paths: [...document.querySelectorAll('#map path')].map((p) => 'w' + p.getAttribute('stroke-width') + ' L' + ((p.getAttribute('d') || '').match(/L/g) || []).length),
  };
}"""


# Lets a test decide when the page hears about a media-query change (a window crossing a breakpoint).
HOLD_MEDIA_CHANGES = """(() => {
  const real = window.matchMedia.bind(window);
  window.__held = [];
  window.__holdChanges = false;
  window.matchMedia = (query) => {
    const list = real(query);
    const add = list.addEventListener.bind(list);
    list.addEventListener = (type, fn, options) => add(type, (event) => {
      if (type === 'change' && window.__holdChanges) window.__held.push(() => fn(event)); else fn(event);
    }, options);
    return list;
  };
})();"""


def annotation(test, err):
    """A GitHub Actions error annotation for a failed test, carrying the page state and the requests it made.
    The job log is not always at hand (it needs sign-in); annotations show on the pull request and in the API."""
    line = ''
    for frame in reversed(traceback.extract_tb(err[2]) if err[2] else []):
        if frame.filename.endswith('browser_regressions.py'):
            line = f',line={frame.lineno}'
            break
    page = getattr(test, 'page', None)
    try:
        state = json.dumps(page.evaluate(PAGE_STATE)) if page else 'no page'
    except Exception as exc:                                  # the page may already be closed
        state = f'page state unavailable ({type(exc).__name__})'
    calls = ', '.join(f"{path}:{(body or {}).get('mode', '-')}" for path, body in getattr(test, 'calls', [])[-16:])
    reason = ' '.join(traceback.format_exception_only(err[0], err[1])).strip()
    reason = reason.split('\nAria snapshot')[0].strip()                  # Playwright appends the whole page tree
    body = f'{reason[:700]}\npage: {state[:900]}\nrequests: {calls[:400]}\npage errors: {str(getattr(test, "errors", ""))[:300]}'
    body = body.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    ident = test.id()
    title = ident.rsplit('.', 1)[-1] if ' ' not in ident else ident        # class-level errors are named "setUpClass (...)"
    title = re.sub(r'[,:\r\n]', ' ', title)
    return f'::error file=scripts/browser_regressions.py{line},title={title}::{body}'


class ReportingResult(unittest.TextTestResult):
    """Builds each annotation while the failed test's page is still open (unittest reports a failure before
    tearDown), and prints them together after the usual report. Diagnostics never fail a run themselves."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.annotations = []

    def _note(self, test, err):
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            return
        try:
            self.annotations.append(annotation(test, err))
        except Exception:
            pass

    def addFailure(self, test, err):
        self._note(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._note(test, err)
        super().addError(test, err)

    def printErrors(self):
        super().printErrors()
        for note in self.annotations:
            self.stream.writeln(note)
        self.stream.flush()


class Runner(unittest.TextTestRunner):
    resultclass = ReportingResult


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
        expect(self.page.locator('#mapNote')).to_contain_text('Fastest road route:')
        line = self.page.locator('#map path[stroke-width="5"]')
        expect(line).to_have_count(1)
        self.assertGreater(line.get_attribute('d').count('L'), 1)
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

    def test_route_survives_a_resize_that_arrives_before_the_layout_change(self):
        # Narrowing the window hides the map where it sits (CSS) until the layout code moves it into the plan.
        # Whether the resize or the media-query change reaches the page first depends on the browser, and on
        # Ubuntu runners it was often the resize: measuring the hidden map clipped the whole route away.
        # Hold the media-query change back so the order is the unlucky one every time.
        self.page.add_init_script(HOLD_MEDIA_CHANGES)
        self.load()
        self.submit()
        self.assert_road()
        self.page.evaluate('() => { window.__holdChanges = true; }')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.evaluate('() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 200))))')
        self.assertGreater(self.page.evaluate('() => window.__held.length'), 0)     # the change really was held back
        self.assert_road()
        self.page.evaluate('() => { window.__holdChanges = false; window.__held.splice(0).forEach((deliver) => deliver()); }')
        expect(self.page.locator('#map')).to_be_visible()
        self.assert_road()
        self.assert_stops_visible()

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
        expect(self.page.locator('.where-line').first).to_contain_text('walk')
        expect(self.page.locator('#planTitle')).to_contain_text('on foot')
        expect(self.page.locator('#mapNote')).to_contain_text('Walking route:')
        # the walking server's usage policy asks for the OpenStreetMap credit and a fix-the-map link wherever its routes show
        expect(self.page.locator('#mapNote a')).to_have_attribute('href', 'https://www.openstreetmap.org/fixthemap')
        expect(self.page.locator('.leaflet-control-attribution a[href="https://www.openstreetmap.org/copyright"]')).to_be_attached()
        line = self.page.locator('#map path[stroke-width="5"]')
        expect(line).to_have_count(1)
        expect(line).to_have_attribute('stroke', '#0b6e4c')
        expect(self.page.locator('.stop-actions a').first).to_have_attribute('href', re.compile('travelmode=walking'))
        expect(self.page.locator('#mapLink')).to_have_attribute('href', re.compile('travelmode=walking'))
        stages = [p for p, _ in self.calls[asked:] if p in ('understand', 'search', 'alternatives', 'brief')]
        self.assertEqual(stages, [])                                # the other plan was already built
        self.switch('driving')
        expect(self.page.locator('#mapNote')).to_contain_text('Fastest road route:')
        expect(self.page.locator('#map path[stroke-width="5"]')).to_have_attribute('stroke', '#0a4da2')
        expect(self.page.locator('.stop-actions a').first).to_have_attribute('href', re.compile('travelmode=driving'))
        self.assertEqual(self.errors, [])

    def test_each_way_of_travelling_gets_its_own_agents_and_the_screen_lane_gets_its_path_first(self):
        self.load()
        self.submit()
        self.lane_ready('walking')
        deadline = time.time() + 5
        while len(self.api_calls('route')) < 2 and time.time() < deadline:
            self.page.wait_for_timeout(50)
        self.assertEqual([b['mode'] for b in self.api_calls('route')], ['driving', 'walking'])
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

    def test_the_assistant_answers_from_the_situation_while_plans_are_still_building(self):
        held = []
        self.page.route('**/api/brief', lambda r: held.append(r))
        self.load()
        self.page.locator('#text').fill(TEXT)
        self.page.locator('#text').press('Enter')
        expect(self.page.locator('#fuForm')).to_be_visible()          # asking works as soon as the request is understood
        expect(self.page.locator('#planTitle')).to_have_count(0)      # while no plan is on screen yet
        self.page.locator('#fuQ').fill('Should I walk or drive?')
        self.page.locator('#fuQ').press('Enter')
        expect(self.page.locator('#fuAnswer')).to_contain_text('Driving')
        expect(self.page.locator('#fuAnswer')).to_contain_text('Walking')
        self.page.unroute('**/api/brief')
        for r in held:
            r.continue_()
        expect(self.page.locator('#planTitle')).to_be_visible()
        question = self.api_calls('followup')[0]
        self.assertEqual((question['mode'], question['radius_km']), ('driving', 8))
        self.assertTrue(question['intent']['needs'])
        self.assertIn('lat', question)
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


if __name__ == '__main__':
    unittest.main(testRunner=Runner, verbosity=2)
