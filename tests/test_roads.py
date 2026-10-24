"""Road routing: the OSRM client (network mocked) and the /api/route endpoint."""
import json
import os
import socket
import ssl
import sys
import threading
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ["AI_MODE"] = "off"

import handler  # noqa: E402
import roads  # noqa: E402

A, B, C = (37.3382, -121.8863), (37.3541, -121.9552), (37.3230, -121.8300)


def osrm_ok(coords=None, duration=900.0, distance=7200.0, legs=2):
    coords = coords or [[-121.8863, 37.3382], [-121.9, 37.34], [-121.9552, 37.3541]]
    return json.dumps({"code": "Ok", "routes": [{
        "geometry": {"type": "LineString", "coordinates": coords}, "duration": duration, "distance": distance,
        "legs": [{"duration": duration / legs, "distance": distance / legs} for _ in range(legs)]}]}).encode()


class RoadsClient(unittest.TestCase):
    def setUp(self):
        roads._cache.clear()
        os.environ.pop("ROAD_ROUTING", None)

    def test_parses_answer_and_swaps_to_lat_lng(self):
        with mock.patch.object(roads, "_open", return_value=osrm_ok()) as m:
            r = roads.fetch_route([A, B])
        self.assertEqual(r["path"][0], [37.3382, -121.8863])          # [lat, lng] for Leaflet
        self.assertEqual(r["duration_min"], 15.0)
        self.assertEqual(r["distance_km"], 7.2)
        self.assertEqual(len(r["legs"]), 2)
        url = m.call_args[0][0]
        self.assertIn("/route/v1/driving/-121.88630,37.33820;-121.95520,37.35410?", url)   # OSRM wants lng,lat
        self.assertIn("overview=full", url)

    def test_second_identical_request_is_served_from_cache(self):
        with mock.patch.object(roads, "_open", return_value=osrm_ok()) as m:
            roads.fetch_route([A, B])
            roads.fetch_route([A, B])
        self.assertEqual(m.call_count, 1)

    def test_cache_limits_total_geometry_size(self):
        with mock.patch.object(roads, 'CACHE_MAX_VERTICES', 5), mock.patch.object(roads, '_open', return_value=osrm_ok()):
            roads.fetch_route([A, B])
            roads.fetch_route([A, C])
        self.assertEqual(len(roads._cache), 1)
        self.assertLessEqual(sum(len(v[1]['path']) for v in roads._cache.values()), 5)

    def test_network_errors_become_roads_unavailable(self):
        for exc in (urllib.error.URLError("down"), TimeoutError("slow"), ConnectionResetError()):
            with mock.patch.object(roads, "_open", side_effect=exc):
                with self.assertRaises(roads.RoadsUnavailable):
                    roads.fetch_route([A, B])

    def test_transient_failure_retries_and_returns_street_geometry(self):
        with mock.patch.object(roads, "_open", side_effect=[TimeoutError("slow"), osrm_ok()]) as m:
            r = roads.fetch_route([A, B])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(r["path"][1], [37.34, -121.9])

    def test_bad_answers_become_roads_unavailable(self):
        bad = [b"not json", b"{}", b"[]", b"null", json.dumps({"code": "NoRoute"}).encode(),
               json.dumps({"code": "Ok", "routes": []}).encode(),
               json.dumps({"code": "Ok", "routes": [{"geometry": {}}]}).encode(),
               osrm_ok(coords=[[-121.8, 37.3]]),            # one point is not a route
               osrm_ok(coords=[[float('nan'), 37], [-121, 38]]),
               osrm_ok(coords=[[-121, 91], [-121, 38]]),
               osrm_ok(duration=float('inf')), osrm_ok(distance=-1)]
        for raw in bad:
            roads._cache.clear()
            with mock.patch.object(roads, "_open", return_value=raw):
                with self.assertRaises(roads.RoadsUnavailable, msg=raw[:40]):
                    roads.fetch_route([A, B])

    def test_can_be_switched_off(self):
        os.environ["ROAD_ROUTING"] = "off"
        with mock.patch.object(roads, "_open") as m:
            with self.assertRaises(roads.RoadsUnavailable):
                roads.fetch_route([A, B])
        m.assert_not_called()

    def test_long_street_paths_keep_all_bends(self):
        coords = [[-121.0, 37.0 + i * 1e-5] for i in range(10000)]
        coords[3][0] = -121.001  # a short bend skipped by the old regular sampling
        with mock.patch.object(roads, "_open", return_value=osrm_ok(coords=coords)):
            path = roads.fetch_route([A, B])["path"]
        self.assertEqual(path, [[lat, lng] for lng, lat in coords])

    def test_oversized_geometry_is_rejected(self):
        with mock.patch.object(roads, "_open", return_value=osrm_ok(coords=[[-121, 37]] * (roads.MAX_PATH_POINTS + 1))):
            with self.assertRaises(roads.RoadsUnavailable):
                roads.fetch_route([A, B])

    def test_walking_asks_the_foot_service_and_driving_asks_the_car_service(self):
        with mock.patch.object(roads, "_open", return_value=osrm_ok()) as m:
            walk, drive = roads.fetch_route([A, B], "walking"), roads.fetch_route([A, B])
        walk_url, drive_url = (c[0][0] for c in m.call_args_list)
        self.assertTrue(walk_url.startswith("https://routing.openstreetmap.de/routed-foot/route/v1/driving/"), walk_url)
        self.assertTrue(drive_url.startswith("https://router.project-osrm.org/route/v1/driving/"), drive_url)
        self.assertEqual((walk["mode"], drive["mode"]), ("walking", "driving"))
        self.assertIn("footpaths", walk["source"])
        self.assertIn("no live conditions", walk["source"])
        self.assertIn("no live traffic", drive["source"])

    def test_the_walking_service_can_be_pointed_somewhere_else(self):
        with mock.patch.object(roads, "WALK_URL", "http://127.0.0.1:5001"), \
                mock.patch.object(roads, "_open", return_value=osrm_ok()) as m:
            roads.fetch_route([A, B], "walking")
        self.assertTrue(m.call_args[0][0].startswith("http://127.0.0.1:5001/route/v1/driving/"))

    def test_walking_and_driving_routes_are_cached_separately(self):
        with mock.patch.object(roads, "_open", return_value=osrm_ok()) as m:
            for mode in ("walking", "driving", "walking", "driving"):
                roads.fetch_route([A, B], mode)
        self.assertEqual(m.call_count, 2)
        self.assertEqual({k[0] for k in roads._cache}, {"walking", "driving"})

    def test_one_switch_turns_off_both_and_unknown_modes_are_refused_without_a_network_call(self):
        os.environ["ROAD_ROUTING"] = "off"
        with mock.patch.object(roads, "_open") as m:
            for mode in ("driving", "walking"):
                with self.assertRaises(roads.RoadsUnavailable) as caught:
                    roads.fetch_route([A, B], mode)
                self.assertEqual(caught.exception.code, "disabled")
            os.environ.pop("ROAD_ROUTING")
            with self.assertRaises(roads.RoadsUnavailable) as caught:
                roads.fetch_route([A, B], "hovercraft")
            self.assertEqual(caught.exception.code, "bad_mode")
        m.assert_not_called()

    def test_walking_and_driving_requests_at_the_same_time_do_not_corrupt_the_cache(self):
        errors = []

        def worker(n):
            try:
                for i in range(40):
                    roads.fetch_route([A, (37.30 + n * 0.001 + i * 0.0001, -121.9)], ("walking", "driving")[i % 2])
            except Exception as exc:  # noqa: BLE001 - any failure is the bug
                errors.append(repr(exc))

        with mock.patch.object(roads, "CACHE_MAX", 6), mock.patch.object(roads, "_open", return_value=osrm_ok()):
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(roads._cache), 6)


class RouteEndpoint(unittest.TestCase):
    def setUp(self):
        roads._cache.clear()
        os.environ.pop("ROAD_ROUTING", None)
        handler._hits.clear()

    def post(self, body):
        event = {"rawPath": "/api/route", "requestContext": {"http": {"method": "POST", "sourceIp": "9.9.9.9"}},
                 "body": json.dumps(body)}
        r = handler.handler(event, None)
        return r["statusCode"], json.loads(r["body"])

    def pts(self, *coords):
        return {"points": [{"lat": a, "lng": b} for a, b in coords]}

    def test_success(self):
        with mock.patch.object(roads, "_open", return_value=osrm_ok()):
            status, body = self.post(self.pts(A, B, C))
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        self.assertGreater(len(body["path"]), 1)
        self.assertIn("no live traffic", body["source"])

    def test_walking_mode(self):
        with mock.patch.object(roads, "_open", return_value=osrm_ok()) as m:
            status, body = self.post({**self.pts(A, B), "mode": "walking"})
            status_default, body_default = self.post(self.pts(A, B))
        self.assertEqual((status, body["available"], body["mode"]), (200, True, "walking"))
        self.assertIn("footpaths", body["source"])
        self.assertEqual(body_default["mode"], "driving")             # no mode means driving, as before
        self.assertEqual(m.call_count, 2)

    def test_walking_outage_names_its_mode_and_is_not_an_error(self):
        with mock.patch.object(roads, "_open", side_effect=urllib.error.URLError("down")):
            status, body = self.post({**self.pts(A, B), "mode": "walking"})
        self.assertEqual((status, body["available"], body["mode"]), (200, False, "walking"))

    def test_bad_mode_and_long_walks_are_refused_before_any_network_call(self):
        far = (37.3382 + 0.2, -121.8863)                                # about 22 km: a drive, not a walk
        with mock.patch.object(roads, "_open") as m:
            for body in ({**self.pts(A, B), "mode": "flying"}, {**self.pts(A, B), "mode": ["walking"]},
                         {**self.pts(A, far), "mode": "walking"}):
                self.assertEqual(self.post(body)[0], 400, body)
        m.assert_not_called()
        with mock.patch.object(roads, "_open", return_value=osrm_ok()):
            self.assertEqual(self.post(self.pts(A, far))[0], 200)       # the same hop is fine by car

    def test_routing_outage_is_not_an_error(self):
        with mock.patch.object(roads, "_open", side_effect=urllib.error.URLError("down")):
            status, body = self.post(self.pts(A, B))
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.assertTrue(body["reason"])

    def test_specific_connection_failures_reach_the_browser_without_private_details(self):
        cases = [
            (urllib.error.URLError(ssl.SSLCertVerificationError(1, 'secret proxy detail')), 'tls_certificate', 'certificate'),
            (urllib.error.URLError(socket.gaierror(-1, 'secret host')), 'dns', 'DNS'),
            (TimeoutError('secret route'), 'timeout', 'timed out'),
            (urllib.error.HTTPError('https://secret/', 407, 'secret credential', {}, None), 'proxy_auth', 'proxy'),
            (urllib.error.HTTPError('https://secret/', 429, 'secret', {}, None), 'rate_limited', 'busy'),
        ]
        for failure, code, expected in cases:
            with self.subTest(code=code), mock.patch.object(roads, '_open', side_effect=failure):
                status, body = self.post(self.pts(A, B))
            self.assertEqual(status, 200)
            self.assertFalse(body['available'])
            self.assertEqual(body['code'], code)
            self.assertIn(expected, body['reason'])
            self.assertNotIn('secret', json.dumps(body))

    def test_disabled_routing_explains_how_to_enable_it(self):
        with mock.patch.dict(os.environ, {'ROAD_ROUTING': 'off'}):
            status, body = self.post(self.pts(A, B))
        self.assertEqual(status, 200)
        self.assertEqual(body['code'], 'disabled')
        self.assertIn('ROAD_ROUTING=on', body['reason'])

    def test_input_is_validated_before_any_network_call(self):
        bad = [
            {},                                                         # no points
            {"points": "x"},
            self.pts(A),                                                # one point
            self.pts(*([A] * (roads.MAX_POINTS + 1))),                  # too many
            {"points": [{"lat": "north", "lng": 1}, {"lat": 2, "lng": 3}]},
            {"points": [{"lat": 95, "lng": 0}, {"lat": 2, "lng": 3}]},  # out of range
            {"points": [{"lat": float("nan"), "lng": 0}, {"lat": 2, "lng": 3}]},
            {"points": [1, 2]},
            self.pts(A, (48.8566, 2.3522)),                             # Paris: not a local hop
        ]
        with mock.patch.object(roads, "_open") as m:
            for body in bad:
                status, payload = self.post(body)
                self.assertEqual(status, 400, body)
                self.assertIn("error", payload)
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
