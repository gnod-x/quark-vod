"""Search protocol, concurrency and failure semantics with local fixtures only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

BOOT = tempfile.TemporaryDirectory(prefix="vod-search-evolution-")
os.environ.update(VOD_BASE_DIR=BOOT.name, VOD_DATA_DIR=BOOT.name+"/data", AI_PARSE_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"app"))
from quark_vod import panshare_matrix as matrix, search_plugins as plugins, search
from quark_vod.search_providers import telegram_rows, pansou_rows, DEFAULT_TG_CHANNELS


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def send(self, value, mime="application/json"):
        body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "fixture=retained; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self):
        code = int(self.path.split("?")[0].split("/")[2]) if self.path.startswith("/redirect/") else 302
        self.send_response(code)
        self.send_header("Location", "http://localhost:"+str(self.server.server_port)+"/not-followed?token=private")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.server.calls.append((self.path, self.headers.get("Cookie"), self.client_address[1]))
        if self.path.startswith(("/redirect/", "/s/RedirectChannel")):
            self.redirect()
        elif "/s/" in self.path:
            self.send('<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_text">'
                      'Sample S02 4K <a href="https://pan.quark.cn/s/tgfixture">下载</a>'
                      ' 提取码：ab12</div><time datetime="2026-11-25T12:00:00+00:00"></time></div>', "text/html")
        elif self.path.startswith("/mixed"):
            self.send({"items":[{"name":"Other cloud","share_url":"https://example.invalid/other"},
                                {"name":"Sample 4K","share_url":"https://pan.quark.cn/s/jsonfixture"}]})
        elif self.path.startswith("/challenge"):
            self.send("<html>Just a moment 安全验证</html>", "text/html")
        else:
            self.send({"items":[]})

    def do_POST(self):
        self.server.payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, self.headers.get("Cookie"), self.client_address[1]))
        if self.path.startswith("/redirect/"):
            return self.redirect()
        self.send({"code":0,"data":{"merged_by_type":{"quark":[{"url":"https://pan.quark.cn/s/pansoufixture",
                                                             "note":"Sample S02 4K","password":"cd34"}]}}})
    def log_message(self, *args):
        pass


class EvolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        cls.server.daemon_threads = True
        cls.server.calls = []
        cls.worker = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.worker.start()
        cls.base = "http://127.0.0.1:"+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        matrix._close_clients()
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(2)

    def setUp(self):
        matrix._health.clear()
        matrix._mem_cache.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = patch.object(matrix, "SEARCH_CACHE_DIR",self.temp.name)
        self.cache.start();self.addCleanup(self.cache.stop)
        self.dead = patch.object(matrix.db,"get_dead_sources_set",return_value=set())
        self.dead.start();self.addCleanup(self.dead.stop)
        self.server.calls.clear()

    def snapshot(self, provider="json", **extra):
        return plugins.snapshot_from_nodes([{"id":"fixture","provider":provider,"base":self.base,**extra}])

    def test_tg_config_defaults_categories_and_snapshot_immutability(self):
        snapshot = self.snapshot("tg_web",channels=["Quark_Movies","Quark_Movies"],categories=["动漫"])
        self.assertEqual(snapshot.nodes[0].channels,("Quark_Movies",))
        self.assertEqual(snapshot.nodes[0].categories,("动漫",))
        self.assertGreaterEqual(len(DEFAULT_TG_CHANNELS),15)
        for changes in ({"channels":["bad/channel"]},{"categories":"动漫"},{"channels":list(range(2))}):
            with self.assertRaises(plugins.PluginConfigError):
                self.snapshot("tg_web",**changes)
        result=matrix.PanshareMatrix.search_report("Sample",snapshot=snapshot,category="电影",use_cache=False)
        self.assertEqual(result["items"],[])

    def test_telegram_blocks_keep_title_time_and_extraction_password(self):
        text=('<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_text">'
              'Sample 4K<br><a href="https://pan.quark.cn/s/first">https://pan.quark.cn/s/first</a>'
              ' 提取码：ab12</div><time datetime="2026-11-25T12:00:00+00:00"></time></div>'
              '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_text">'
              'Other 1080p https://pan.quark.cn/s/second</div></div>'
              '<footer>https://pan.quark.cn/s/notamessage</footer>')
        rows=telegram_rows(text,"query","tg")
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["share_url"],"https://pan.quark.cn/s/first#pwd=ab12")
        self.assertEqual(rows[0]["published_at"],"2026-11-25T12:00:00+00:00")
        self.assertNotIn("Other",rows[0]["name"])

    def test_real_tg_and_pansou_post_protocol(self):
        tg=matrix.PanshareMatrix.search_report("Sample",snapshot=self.snapshot("tg_web",channels=["Quark_Movies"]),use_cache=False)
        self.assertFalse(tg["partial"])
        self.assertEqual(tg["items"][0]["share_url"],"https://pan.quark.cn/s/tgfixture#pwd=ab12")
        pansou=matrix.PanshareMatrix.search_report("Sample",snapshot=self.snapshot("pansou"),use_cache=False)
        self.assertFalse(pansou["partial"])
        self.assertEqual(pansou["items"][0]["share_url"],"https://pan.quark.cn/s/pansoufixture#pwd=cd34")
        self.assertEqual(self.server.payload,{"kw":"Sample","cloud_types":["quark"],"res":"merge"})

    def test_pansou_nested_results_and_nonquark_rows(self):
        rows=pansou_rows({"data":{"results":[{"title":"Exact title","links":[
            {"type":"quark","url":"https://pan.quark.cn/s/nested"},
            {"type":"ali","url":"https://www.alipan.com/s/other"}]}]}}, "fallback","node",24)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["name"],"Exact title")
        with self.assertRaises(ValueError):
            pansou_rows({"code":500,"data":{}},"x","node",24)

    def test_persistent_http_connection_and_cookie_jar(self):
        matrix._fetch_html(self.base+"/empty",1)
        matrix._fetch_html(self.base+"/empty",1)
        a,b=self.server.calls[-2:]
        self.assertEqual(a[2],b[2])
        self.assertIn("fixture=retained",b[1])

    def test_other_cloud_does_not_poison_partial(self):
        result=matrix.PanshareMatrix.search_report("Sample",snapshot=self.snapshot(path="/mixed"),use_cache=False)
        self.assertFalse(result["partial"])
        self.assertEqual(len(result["items"]),1)
        self.assertEqual(result["nodes"][0]["errors"],0)

    def test_soft_challenge_is_not_successful_empty_search(self):
        result=matrix.PanshareMatrix.search_report("Sample",snapshot=self.snapshot("html",base=self.base+"/challenge"),use_cache=False)
        self.assertTrue(result["partial"])
        self.assertGreater(result["nodes"][0]["errors"],0)

    def test_circuit_opens_after_three_failures_and_single_half_open(self):
        node=self.snapshot().nodes[0].as_dict()
        for _ in range(3):
            self.assertTrue(matrix._admit_node(node))
            matrix._record_node(node,{"errors":1,"seconds":.1,"candidates":0})
        self.assertFalse(matrix._admit_node(node))
        self.assertEqual(matrix.node_health()[0]["state"],"OPEN")
        matrix._health[matrix._health_key(node)]["opened_until"]=time.monotonic()-.1
        self.assertTrue(matrix._admit_node(node))
        self.assertFalse(matrix._admit_node(node))
        matrix._record_node(node,{"errors":0,"seconds":.1,"candidates":1})
        self.assertTrue(matrix._admit_node(node))
        self.assertEqual(matrix.node_health()[0]["state"],"CLOSED")

    def test_hung_plugin_returns_by_deadline_and_releases_search_slots(self):
        release=threading.Event()
        name="evolution-hung"
        def plugin(*args):
            release.wait(2)
            return [],{"errors":0}
        plugins.register_provider(name,plugin)
        self.addCleanup(plugins._PROVIDERS.pop,name,None)
        snapshot=self.snapshot(name)
        start=time.monotonic()
        try:
            with patch.object(matrix,"NODE_HARD_DEADLINE",.1):
                result=matrix.PanshareMatrix.search_report("Sample",snapshot=snapshot,use_cache=False)
            self.assertLess(time.monotonic()-start,.5)
            self.assertTrue(result["partial"])
            self.assertEqual(result["nodes"][0]["error_type"],"ProviderTimeout")
            self.assertTrue(matrix._search_slots.acquire(blocking=False))
            matrix._search_slots.release()
        finally:
            release.set()

    def test_sparse_detail_failures_are_degraded_but_search_failure_is_partial(self):
        node={"key":"fixture","base":"https://fixture.invalid","provider":"html"}
        def fetch(url, timeout):
            if "/search" in url:
                return "".join('<a href="/voddetail/'+str(i)+'.html">x</a>' for i in range(10))
            if "/9.html" in url:
                raise TimeoutError("fixture detail")
            return "<title>Sample 4K</title>https://pan.quark.cn/s/detail"+url.rsplit("/",1)[-1].split(".")[0]
        with patch.object(matrix,"_fetch_html",side_effect=fetch):
            items,report=matrix._fetch_node(node,"Sample",24,3)
        self.assertEqual(len(items),9)
        self.assertTrue(report["degraded"])
        with patch.object(matrix,"_fetch_node",return_value=(items,report)):
            _,safe=matrix._fetch_provider(node,"Sample",24,3)
        self.assertTrue(safe["degraded"])
        self.assertEqual(safe["errors"],1)

    def test_detail_paths_and_lru_recency(self):
        links=''.join('<a href="'+p+'">x</a>' for p in (
            "/vod/12.html","/detail/9.html","/play/2-1-1.html","//evil.invalid/detail/2.html"))
        self.assertEqual(matrix.detail_paths(links),["/vod/12.html","/detail/9.html","/play/2-1-1.html"])
        payload={"items":[{"name":"x"}],"nodes":[],"partial":False}
        for index in range(128):
            matrix._cache_put(str(index),payload)
        matrix._cache_get("0")
        matrix._cache_put("128",payload)
        self.assertIn("0",matrix._mem_cache)
        self.assertNotIn("1",matrix._mem_cache)

    def test_variants_and_strict_initial_alias_evidence(self):
        variants=search.query_variants("庆余年 (2019)",["Joy of Life","Joy of Life"],2)
        self.assertIn("庆余年 第二季",variants)
        self.assertIn("庆余年第2季",variants)
        self.assertIn("庆余年 S02",variants)
        self.assertIn("Joy of Life",variants)
        self.assertEqual(search.normalize_keyword("Dune II"),"Dune 2")
        self.assertIn("北方谦三水浒传",search.query_variants("北方谦三 水浒传"))
        self.assertTrue(search.trusted_initial_alias("庆余年","QYN2.2019.S02E01.2160p",year=2019,season=2,episode=1))
        for value in ("QYN2.S02E01","QYN2.2018.S02E01","QYN2.2019.S02E02","QYN2.2019.S02E01E02"):
            self.assertFalse(search.trusted_initial_alias("庆余年",value,year=2019,season=2,episode=1))

    def test_tiered_parallel_primary_stops_before_aliases(self):
        snapshot=self.snapshot()
        barrier=threading.Barrier(2)
        calls=[]
        def fake(query,**kwargs):
            calls.append(query)
            barrier.wait(1)
            return {"items":[{"source":"fixture","name":"Sample S02 4K","share_url":"https://pan.quark.cn/s/test"+str(i)} for i in range(5)],
                    "nodes":[],"partial":False,"cached":False}
        with patch.object(matrix,"source_snapshot",return_value=(snapshot,None)),patch.object(matrix.PanshareMatrix,"search_report",side_effect=fake):
            result=search.search_report("Sample",aliases=["Unused alias"],season=2,episode=1,use_cache=False)
        self.assertTrue(result["early_stopped"])
        self.assertEqual(len(calls),2)
        self.assertNotIn("Unused alias",calls)

    def test_redirect_codes_are_retained_without_following_location(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                before = len(self.server.calls)
                with self.assertRaises(matrix.ProviderRateLimited) as caught:
                    matrix._http_request(self.base+"/redirect/"+str(code), 1)
                self.assertEqual(caught.exception.http_status, code)
                self.assertTrue(caught.exception.redirect)
                self.assertEqual(len(self.server.calls), before+1)
                self.assertNotIn("private", str(caught.exception))

    def test_each_builtin_provider_preserves_rejected_redirect_status(self):
        snapshots = [
            self.snapshot("html", base=self.base+"/redirect/302"),
            self.snapshot("json", path="/redirect/302"),
            self.snapshot("pansou", path="/redirect/302"),
            self.snapshot("tg_web", channels=["RedirectChannel"]),
        ]
        for snapshot in snapshots:
            with self.subTest(provider=snapshot.nodes[0].provider):
                before = len(self.server.calls)
                result = matrix.PanshareMatrix.search_report("Sample", snapshot=snapshot, use_cache=False)
                self.assertTrue(result["partial"])
                self.assertEqual(result["nodes"][0]["http_status"], 302)
                self.assertTrue(result["nodes"][0]["redirect"])
                self.assertEqual(len(self.server.calls), before+1)
                self.assertNotIn("private", str(result))
                self.assertNotIn("localhost", str(result))

    def test_detail_redirect_status_is_not_overwritten_by_search_page_200(self):
        node = {"key": "fixture", "provider": "html", "base": self.base}
        def fetch(url, timeout):
            if "/search" in url:
                return '<a href="/voddetail/1.html">one</a>'
            raise matrix.ProviderRateLimited("redirect", http_status=307, redirect=True)
        with patch.object(matrix, "_fetch_html", side_effect=fetch):
            _, report = matrix._fetch_provider(node, "Sample", 24, 3)
        self.assertEqual(report["http_status"], 307)
        self.assertTrue(report["redirect"])
        self.assertEqual(report["errors"], 1)

    def test_tg_mixed_redirect_preserves_status_counts(self):
        snapshot = self.snapshot("tg_web", channels=["Quark_Movies", "RedirectChannel"])
        report = matrix.PanshareMatrix.search_report("Sample", snapshot=snapshot, use_cache=False)
        node = report["nodes"][0]
        self.assertTrue(report["partial"])
        self.assertEqual(node["http_status"], 302)
        self.assertEqual(node["http_status_counts"], {"200": 1, "302": 1})
        self.assertEqual(len(report["items"]), 1)

    def test_search_default_and_explicit_deep_budget_are_validated(self):
        snapshot = self.snapshot()
        empty = {"items": [], "nodes": [], "partial": False, "cached": False}
        for value, expected in ((None, 3.8), (22, 22.0)):
            with self.subTest(value=value), \
                 patch.object(matrix, "source_snapshot", return_value=(snapshot, None)), \
                 patch.object(matrix.PanshareMatrix, "search_report", return_value=empty) as run:
                result = search.search_report("Sample", deadline_seconds=value)
                self.assertEqual(result["budget_seconds"], expected)
                self.assertFalse(result["partial"])
                self.assertFalse(result["deadline_exhausted"])
                remaining = run.call_args.kwargs["deadline_at"]-time.monotonic()
                self.assertGreater(remaining, expected-.5)
                self.assertLessEqual(remaining, expected)
        for value in (True, False, 0, .01, 23, "22", float("inf"), float("nan")):
            with self.subTest(invalid=value), self.assertRaises(ValueError):
                search.search_report("Sample", deadline_seconds=value)

    def test_round_deadline_preserves_candidates_and_reports_unfinished_variants(self):
        snapshot = self.snapshot()
        queries = search.query_variants("Sample", ["Unused alias"], 2)
        def fake(query, **kwargs):
            time.sleep(.04)
            return {"items": [{"source": "fixture", "name": "Sample S02 1080p",
                              "share_url": "https://pan.quark.cn/s/term"+str(queries.index(query))}],
                    "nodes": [{"node": "fixture", "errors": 0}], "partial": False, "cached": False}
        with patch.object(matrix, "source_snapshot", return_value=(snapshot, None)), \
             patch.object(matrix.PanshareMatrix, "search_report", side_effect=fake):
            started = time.monotonic()
            short = search.search_report("Sample", aliases=["Unused alias"], season=2,
                                         episode=1, use_cache=False, deadline_seconds=.07)
            self.assertLess(time.monotonic()-started, .3)
            deep = search.search_report("Sample", aliases=["Unused alias"], season=2,
                                        episode=1, use_cache=False, deadline_seconds=22)
        self.assertTrue(short["partial"])
        self.assertTrue(short["deadline_exhausted"])
        self.assertTrue(short["unexecuted_queries"])
        self.assertTrue(short["incomplete_queries"])
        self.assertEqual(len(short["results"]), 2)
        self.assertTrue(any(n.get("error_type") == "SearchDeadline" for n in short["nodes"]))
        self.assertFalse(deep["partial"])
        self.assertFalse(deep["deadline_exhausted"])
        self.assertEqual(deep["unexecuted_queries"], [])
        self.assertEqual(len(deep["results"]), len(queries))
        self.assertLessEqual({r.share_url for r in short["results"]},
                             {r.share_url for r in deep["results"]})

    def test_matrix_slot_wait_is_inside_absolute_deadline_and_keeps_diagnostics(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        try:
            with patch.object(matrix, "_search_slots", slots), \
                 patch.object(matrix, "_fetch_provider") as fetch:
                started = time.monotonic()
                result = matrix.PanshareMatrix.search_report("Sample", snapshot=self.snapshot(),
                    use_cache=False, deadline_seconds=.06)
                self.assertLess(time.monotonic()-started, .25)
            fetch.assert_not_called()
            self.assertTrue(result["partial"])
            self.assertTrue(result["busy"])
            self.assertEqual(result["nodes"][0]["error_type"], "SearchDeadline")
            self.assertEqual(result["nodes"][0]["node"], "fixture")
        finally:
            slots.release()

    def test_matrix_does_not_restart_budget_after_acquiring_a_queued_slot(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        release = threading.Event()
        name = "queued-budget-plugin"
        def plugin(*args):
            release.wait(1)
            return [], {"errors": 0}
        plugins.register_provider(name, plugin)
        self.addCleanup(plugins._PROVIDERS.pop, name, None)
        timer = threading.Timer(.12, slots.release)
        timer.start()
        try:
            with patch.object(matrix, "_search_slots", slots):
                started = time.monotonic()
                result = matrix.PanshareMatrix.search_report("Sample", snapshot=self.snapshot(name),
                    use_cache=False, deadline_seconds=.2)
                self.assertLess(time.monotonic()-started, .28)
                self.assertTrue(result["partial"])
                self.assertEqual(result["nodes"][0]["error_type"], "ProviderTimeout")
                self.assertTrue(slots.acquire(blocking=False))
                slots.release()
        finally:
            release.set()
            timer.join(1)

    def test_fast_partial_cache_is_read_but_cannot_block_explicit_deep_search(self):
        snapshot = self.snapshot(path="/mixed")
        item = {"source": "fixture", "name": "Sample 4K",
                "share_url": "https://pan.quark.cn/s/jsonfixture"}
        partial = ([item], {"node": "fixture", "provider": "json", "errors": 0,
                            "partial": True, "pages": 1, "candidates": 1})
        with patch.object(matrix, "source_snapshot", return_value=(snapshot, None)):
            with patch.object(matrix, "_fetch_provider", return_value=partial):
                fast = search.search_report("Sample", use_cache=True)
            self.assertTrue(fast["partial"])
            original = matrix._cache_get
            observed = []
            def read_cache(key):
                value = original(key)
                if value is not None:
                    observed.append(bool(value["partial"]))
                return value
            with patch.object(matrix, "_cache_get", side_effect=read_cache), patch.object(
                    matrix, "_fetch_provider", wraps=matrix._fetch_provider) as fetch:
                deep = search.search_report("Sample", use_cache=True, deadline_seconds=22)
            self.assertIn(True, observed)  # Prove an actual short partial cache hit.
            fetch.assert_called_once()  # Longer review still requests the source.
            self.assertFalse(deep["partial"])
            self.assertFalse(deep["cached"])
            self.assertEqual({r.share_url for r in fast["results"]},
                             {r.share_url for r in deep["results"]})
            # Complete results remain reusable by the normal fast path.
            with patch.object(matrix, "_fetch_provider") as fetch:
                complete = search.search_report("Sample", use_cache=True)
            fetch.assert_not_called()
            self.assertTrue(complete["cached"])
            self.assertFalse(complete["partial"])

    def test_caller_deadline_does_not_trip_health_or_block_the_next_search(self):
        release = threading.Event()
        name = "budget-health-plugin"
        def plugin(*args):
            release.wait(1)
            return [{"name": "Sample 4K", "share_url": "https://pan.quark.cn/s/healthy"}], {"errors": 0, "pages": 1}
        plugins.register_provider(name, plugin)
        self.addCleanup(plugins._PROVIDERS.pop, name, None)
        snapshot = self.snapshot(name)
        try:
            for _ in range(3):
                result = matrix.PanshareMatrix.search_report("Sample", snapshot=snapshot,
                    use_cache=False, deadline_seconds=.06)
                self.assertTrue(result["partial"])
                self.assertTrue(result["nodes"][0]["budget_exhausted"])
            self.assertEqual(matrix.node_health()[0]["state"], "CLOSED")
            self.assertEqual(matrix.node_health()[0]["consecutive_failures"], 0)
            self.assertEqual(matrix.node_health()[0]["budget_exhaustions"], 3)
            release.set()
            result = matrix.PanshareMatrix.search_report("Sample", snapshot=snapshot,
                use_cache=False, deadline_seconds=.5)
            self.assertFalse(result["partial"])
            self.assertEqual(len(result["items"]), 1)
        finally:
            release.set()

    def test_real_node_timeout_still_trips_circuit_before_caller_deadline(self):
        snapshot = self.snapshot()
        with patch.object(matrix, "_fetch_html", side_effect=TimeoutError("node timeout")):
            for _ in range(3):
                report = matrix.PanshareMatrix.search_report("Sample", snapshot=snapshot,
                    use_cache=False, deadline_seconds=1)
                self.assertTrue(report["partial"])
                self.assertFalse(report["nodes"][0].get("budget_exhausted"))
        self.assertEqual(matrix.node_health()[0]["state"], "OPEN")
        self.assertEqual(matrix.node_health()[0]["consecutive_failures"], 3)

    def test_outer_deadline_bounds_hung_query_and_leaves_search_slots_reusable(self):
        release = threading.Event()
        done = threading.Event()
        def plugin(*args):
            release.wait(1)
            done.set()
            return [], {"errors": 0}
        name = "round-deadline-hung"
        plugins.register_provider(name, plugin)
        self.addCleanup(plugins._PROVIDERS.pop, name, None)
        try:
            with patch.object(matrix, "source_snapshot", return_value=(self.snapshot(name), None)):
                started = time.monotonic()
                result = search.search_report("Sample", aliases=["Alias"], season=2,
                                               use_cache=False, deadline_seconds=.1)
                self.assertLess(time.monotonic()-started, .4)
            self.assertTrue(result["partial"])
            self.assertTrue(result["deadline_exhausted"])
            self.assertTrue(result["nodes"])
            self.assertTrue(matrix._search_slots.acquire(timeout=.2))
            matrix._search_slots.release()
        finally:
            release.set()
            done.wait(1)

    def test_s00_specials_are_tv_and_wrong_season_stays_rejected(self):
        def candidate(name, media_type="电视剧"):
            return search.SearchResult("panshare","fixture",name,"","",[],share_url="https://pan.quark.cn/s/test",media_type=media_type)
        self.assertGreaterEqual(search.score_search_result(candidate("Sample S00 OVA 4K"),"Sample",season=0,episode=1),30)
        self.assertLess(search.score_search_result(candidate("Sample S01 4K"),"Sample",season=0,episode=1),0)


if __name__=="__main__":
    unittest.main(verbosity=2)
