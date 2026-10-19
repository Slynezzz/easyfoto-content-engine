import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from engine import server
from engine.config import Config
from engine.db import DB
from engine.instagram import IGError, Instagram

TOKEN = "AbCdEf0123456789_-xyz"


class Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeGraph:
    """Имитация graph.instagram.com с переключаемыми статусами контейнера."""

    def __init__(self, statuses=("IN_PROGRESS", "FINISHED"), fail_metric=None):
        self.statuses = list(statuses)
        self.calls = []
        self.fail_metric = fail_metric

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        if "refresh_access_token" in url:
            return Resp({"access_token": "NEW", "expires_in": 5183944})
        if url.endswith("/insights"):
            metrics = params["metric"].split(",")
            if self.fail_metric and self.fail_metric in metrics:
                return Resp({"error": {"message": "unsupported", "code": 100}})
            return Resp({"data": [{"name": m, "values": [{"value": 7}]} for m in metrics]})
        if params.get("fields") == "status_code,status":
            return Resp({"status_code": self.statuses.pop(0)})
        if params.get("fields") == "permalink":
            return Resp({"permalink": "https://www.instagram.com/reel/XYZ/"})
        return Resp({"error": {"message": "unexpected"}})

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        if url.endswith("/media"):
            return Resp({"id": "CONTAINER1"})
        if url.endswith("/media_publish"):
            return Resp({"id": "MEDIA1"})
        return Resp({"error": {"message": "unexpected"}})


class InstagramTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), ig_user_id="17841400000",
                          ig_access_token="SECRET", public_base_url="https://engine.example.app")
        self.db = DB(self.cfg.db_path)
        self.ig = Instagram(self.cfg, self.db, sleep=lambda s: None)
        self.pid = self.db.create_post(status="approved", format="worlds", hook_id="eras", theme="eras",
                                       script_json={}, caption="Текст #тег", media_token=TOKEN)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _patch(self, graph):
        return mock.patch.multiple("engine.instagram.requests", get=graph.get, post=graph.post)

    def test_publish_happy_path(self):
        g = FakeGraph()
        with self._patch(g):
            link = self.ig.publish(self.pid)
        self.assertEqual(link, "https://www.instagram.com/reel/XYZ/")
        post = self.db.post(self.pid)
        self.assertEqual((post["status"], post["ig_media_id"]), ("published", "MEDIA1"))
        create = next(c for c in g.calls if c[1].endswith("/media"))[2]
        self.assertEqual(create["media_type"], "REELS")
        self.assertEqual(create["video_url"], f"https://engine.example.app/media/{TOKEN}.mp4")
        self.assertEqual(create["caption"], "Текст #тег")
        self.assertIn("/v23.0/", create and next(c for c in g.calls if c[1].endswith("/media"))[1])

    def test_publish_only_approved_and_only_once(self):
        with self._patch(FakeGraph()):
            self.ig.publish(self.pid)
            with self.assertRaises(IGError):
                self.ig.publish(self.pid)

    def test_container_error_marks_failed_and_resets_container(self):
        with self._patch(FakeGraph(statuses=["IN_PROGRESS", "ERROR"])):
            with self.assertRaises(IGError):
                self.ig.publish(self.pid)
        post = self.db.post(self.pid)
        self.assertEqual(post["status"], "failed")
        self.assertIsNone(post["ig_container_id"])
        self.assertIn("не принял", post["error"])

    def test_timeout_keeps_container_for_retry(self):
        with self._patch(FakeGraph(statuses=["IN_PROGRESS"] * 50)):
            with self.assertRaises(IGError):
                self.ig.publish(self.pid, max_wait_s=60, poll_s=20)
        post = self.db.post(self.pid)
        self.assertEqual(post["ig_container_id"], "CONTAINER1")
        # повтор после одобрения использует тот же контейнер
        self.db.claim_post(self.pid, "failed", "approved")
        g = FakeGraph(statuses=["FINISHED"])
        with self._patch(g):
            self.ig.publish(self.pid)
        self.assertFalse(any(c[1].endswith("/media") for c in g.calls))

    def test_permalink_failure_does_not_fail_live_reel(self):
        g = FakeGraph()
        orig = g.get
        g.get = lambda url, params=None, timeout=None: (
            Resp({"error": {"message": "temp", "code": 2}}) if params.get("fields") == "permalink"
            else orig(url, params, timeout))
        with self._patch(g):
            self.ig.publish(self.pid)
        post = self.db.post(self.pid)
        self.assertEqual((post["status"], post["ig_media_id"]), ("published", "MEDIA1"))

    def test_crash_after_publish_is_recovered_without_second_publish(self):
        # ролик вышел, но процесс упал до статуса published
        self.db.update_post(self.pid, status="failed", ig_media_id="MEDIA1", ig_container_id="CONTAINER1")
        self.db.claim_post(self.pid, "failed", "approved")
        g = FakeGraph(statuses=[])
        with self._patch(g):
            self.ig.publish(self.pid)
        self.assertFalse(any(c[1].endswith("/media_publish") for c in g.calls))
        self.assertEqual(self.db.post(self.pid)["status"], "published")

    def test_unexpected_exception_marks_failed(self):
        g = FakeGraph()
        g.post = lambda url, data=None, timeout=None: Resp({"weird": True})  # нет "id"
        with self._patch(g):
            with self.assertRaises(IGError):
                self.ig.publish(self.pid)
        self.assertEqual(self.db.post(self.pid)["status"], "failed")

    def test_unsupported_metric_is_remembered(self):
        with self._patch(FakeGraph(fail_metric="reels_skip_rate")):
            self.ig.insights("MEDIA1")
        self.assertIn("reels_skip_rate", self.db.get("ig_bad_metrics"))
        g = FakeGraph(fail_metric="reels_skip_rate")
        with self._patch(g):
            self.ig.insights("MEDIA2")
        self.assertEqual(sum(1 for c in g.calls if c[1].endswith("/insights")), 1)

    def test_insights_fall_back_to_single_metrics(self):
        with self._patch(FakeGraph(fail_metric="reels_skip_rate")):
            m = self.ig.insights("MEDIA1")
        self.assertEqual(m["plays"], 7)
        self.assertEqual(m["saves"], 7)
        self.assertIsNone(m["skip_rate"])

    def test_collect_all_stores_metrics(self):
        self.db.update_post(self.pid, status="published", ig_media_id="MEDIA1",
                            published_at="2099-01-01T00:00:00+00:00")
        with self._patch(FakeGraph()):
            self.assertEqual(self.ig.collect_all(), 1)
        row = self.db.latest_metrics()[0]
        self.assertEqual((row["plays"], row["skip_rate"]), (7, 7))

    def test_refresh_token_stores_new_token(self):
        with self._patch(FakeGraph()):
            self.assertTrue(self.ig.refresh_token())
        self.assertEqual(self.ig.token, "NEW")

    def test_errors_never_leak_token(self):
        def boom(*a, **k):
            import requests
            raise requests.ConnectionError("failed url ...access_token=SECRET")
        with mock.patch.multiple("engine.instagram.requests", get=boom, post=boom):
            with self.assertRaises(IGError) as ctx:
                self.ig.publish(self.pid)
        self.assertNotIn("SECRET", str(ctx.exception))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.media = Path(cls._tmp.name)
        (cls.media / f"{TOKEN}.mp4").write_bytes(b"0123456789" * 1000)
        (cls.media / "secret.txt").write_text("nope")
        cls.srv = server.start(cls.media, 0, host="127.0.0.1")
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls._tmp.cleanup()

    def _get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, b"", e.headers

    def test_serves_video_with_length_and_type(self):
        code, body, h = self._get(f"/media/{TOKEN}.mp4")
        self.assertEqual((code, len(body), h["Content-Type"]), (200, 10000, "video/mp4"))

    def test_range_requests(self):
        code, body, h = self._get(f"/media/{TOKEN}.mp4", {"Range": "bytes=10-19"})
        self.assertEqual((code, body), (206, b"0123456789"))
        self.assertEqual(h["Content-Range"], "bytes 10-19/10000")

    def test_rejects_everything_else(self):
        for path in ("/media/secret.txt", "/media/../secret.txt", "/media/short.mp4",
                     "/media/", f"/media/{TOKEN}.mp4.bak", "/etc/passwd"):
            with self.subTest(path=path):
                self.assertEqual(self._get(path)[0], 404)

    def test_health(self):
        self.assertEqual(self._get("/health")[:2], (200, b"ok"))


if __name__ == "__main__":
    unittest.main()
