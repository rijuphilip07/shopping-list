import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "riju.py"
spec = importlib.util.spec_from_file_location("riju", SCRIPT)
riju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(riju)


class PriceParsingTest(unittest.TestCase):
    def test_model_match_accepts_separator_variants_but_not_longer_codes(self):
        self.assertTrue(riju.model_matches("Sony WH1000XM5 headphones", "WH-1000XM5"))
        self.assertFalse(riju.model_matches("Sony WH-1000XM50 headphones", "WH-1000XM5"))

    def test_structured_offer_is_used_and_visible_text_is_not(self):
        page = {
            "title": "A product",
            "text": "SALE $1.00, unrelated item",
            "jsonld": [{"@context": "https://schema.org", "@type": "Product", "name": "Camera A",
                        "offers": {"@type": "Offer", "price": "799.95", "priceCurrency": "AUD"}}],
        }
        result = riju.extract_product(page)
        self.assertEqual((result["price"], result["currency"], result["evidence"]),
                         (799.95, "AUD", "jsonld_product_offer"))
        self.assertEqual(riju.extract_product({"title": "No offer", "text": "$1.00"})["status"], "needs_review")

    def test_rejects_local_urls(self):
        for url in ("http://localhost:8080", "http://127.0.0.1", "file:///etc/passwd", "http://192.168.1.2"):
            with self.assertRaises(ValueError):
                riju.public_url(url)

    def test_ambiguous_product_data_is_not_used_for_alerts(self):
        page = {"title": "Camera listing", "url": "https://shop.example/cameras", "jsonld": [
            {"@type": "Product", "name": "Camera A", "offers": {"price": "10", "priceCurrency": "AUD"}},
            {"@type": "Product", "name": "Camera B", "offers": {"price": "20", "priceCurrency": "AUD"}},
        ]}
        self.assertEqual(riju.extract_product(page)["status"], "needs_review")

    def test_same_product_listed_twice_is_one_product(self):
        url = "https://www.babykingdom.com.au/products/silver-cross-wave-3-pram-caramel"
        page = {"title": "Silver Cross Wave 3 Pram Caramel – Baby Kingdom", "url": url + "?_su_rec=x", "jsonld": [
            {"@type": "Product", "name": "Silver Cross Wave 3 Pram Caramel", "url": url,
             "offers": [{"@type": "Offer", "price": "1899.99", "priceCurrency": "AUD"}]},
            {"@type": "Product", "name": "Silver Cross Wave 3 Pram Caramel", "url": url,
             "offers": {"@type": "Offer", "price": "1,899.99", "priceCurrency": "AUD"}},
        ]}
        result = riju.extract_product(page)
        self.assertEqual((result["status"], result["price"], result["currency"]), ("ok", 1899.99, "AUD"))
        page["jsonld"][1]["offers"]["price"] = "1,799.99"  # same name, different price: still refuse
        self.assertEqual(riju.extract_product(page)["status"], "needs_review")


class VariantTest(unittest.TestCase):
    page = {"title": "Royal Canin British Shorthair", "jsonld": [{"@type": "Product", "name": "Royal Canin British Shorthair", "offers": [
        {"@type": "Offer", "price": 47.9, "priceCurrency": "AUD", "url": "https://shop.example/products/rc-bsh?variant=111"},
        {"@type": "Offer", "price": 76.99, "priceCurrency": "AUD", "url": "https://shop.example/products/rc-bsh?variant=222"}]}],
        "meta": {"og:price:amount": "47.90"}}

    def test_several_sizes_without_a_size_link_is_not_guessed(self):
        result = riju.extract_product(dict(self.page, url="https://shop.example/products/rc-bsh"))
        self.assertEqual((result["status"], result["price"], result["evidence"]), ("needs_review", None, "several_sizes"))

    def test_size_link_picks_that_size(self):
        result = riju.extract_product(dict(self.page, url="https://shop.example/products/rc-bsh?variant=222"))
        self.assertEqual((result["status"], result["price"], result["evidence"]), ("ok", 76.99, "jsonld_variant_offer"))

    def test_variant_in_the_path(self):
        page = {"title": "x", "url": "https://www.petstock.com.au/products/rc-bsh-variant-63", "jsonld": [{"@type": "Product", "name": "x", "offers": [
            {"price": "84.69", "priceCurrency": "AUD", "url": "https://www.petstock.com.au/products/rc-bsh-variant-63"},
            {"price": "56.49", "priceCurrency": "AUD", "url": "https://www.petstock.com.au/products/rc-bsh-variant-70"}]}]}
        self.assertEqual(riju.extract_product(page)["price"], 84.69)

    def test_one_price_many_offers_still_fine(self):
        page = {"title": "x", "url": "https://shop.example/p", "jsonld": [{"@type": "Product", "name": "x", "offers": [
            {"price": "10", "priceCurrency": "AUD"}, {"price": "10.00", "priceCurrency": "AUD"}]}]}
        self.assertEqual(riju.extract_product(page)["price"], 10.0)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root, self.old_data = riju.ROOT, riju.DATA
        riju.ROOT, riju.DATA = self.root, self.root / "data" / "products.json"
        self.old_wait, riju.RETRY_WAIT_S = riju.RETRY_WAIT_S, 0

    def tearDown(self):
        riju.ROOT, riju.DATA = self.old_root, self.old_data
        riju.RETRY_WAIT_S = self.old_wait
        self.tmp.cleanup()

    def test_add_check_alert_once_and_rearm(self):
        url = "https://shop.example/camera?utm_source=test"
        initial = {"status": "ok", "name": "Camera A", "price": 100.0, "currency": "AUD", "evidence": "jsonld_product_offer"}
        args = SimpleNamespace(action="add", url=url, name=None, target="90", drop=10, currency=None, local_only=True)
        with patch.object(riju, "scrape", return_value=initial):
            added = riju.shopping(args)
        self.assertEqual(added["product"]["url"], "https://shop.example/camera")
        self.assertEqual(added["product"]["postcode"], "3820")
        self.assertEqual(added["sync"], "local_only")
        with patch.object(riju, "scrape", return_value={**initial, "price": 89.0}):
            first = riju.check_prices(SimpleNamespace(local_only=True))
            second = riju.check_prices(SimpleNamespace(local_only=True))
        self.assertEqual(len(first["alerts"]), 1)
        self.assertEqual(second["alerts"], [])
        with patch.object(riju, "scrape", return_value={**initial, "price": 95.0}):
            riju.check_prices(SimpleNamespace(local_only=True))
        with patch.object(riju, "scrape", return_value={**initial, "price": 89.0}):
            rearmed = riju.check_prices(SimpleNamespace(local_only=True))
        self.assertEqual(len(rearmed["alerts"]), 1)
        self.assertEqual(len(riju.load_data()["products"][0]["price_history"]), 4)

    def test_third_failure_alerts_once(self):
        product = {"id": "abc", "name": "Camera", "url": "https://shop.example/camera", "currency": "AUD",
                   "target_price": 90, "drop_percent": 10, "initial_price": 100, "current_price": 100,
                   "price_history": [], "failure_count": 0, "last_alert_price": None}
        riju.save_data({"schema_version": 1, "products": [product]})
        with patch.object(riju, "scrape", side_effect=RuntimeError("blocked_by_challenge")):
            alerts = [riju.check_prices(SimpleNamespace(local_only=True))["alerts"] for _ in range(4)]
        self.assertEqual([len(a) for a in alerts], [0, 0, 1, 0])
        self.assertEqual(json.loads(riju.DATA.read_text())["products"][0]["failure_count"], 4)

    def test_unavailable_offer_does_not_alert(self):
        product = {"id": "abc", "name": "Camera", "url": "https://shop.example/camera", "currency": "AUD",
                   "target_price": 90, "drop_percent": 10, "initial_price": 100, "current_price": 100,
                   "price_history": [], "failure_count": 0, "last_alert_price": None}
        riju.save_data({"schema_version": 1, "products": [product]})
        found = {"status": "ok", "price": 80, "currency": "AUD", "availability": "https://schema.org/OutOfStock"}
        with patch.object(riju, "scrape", return_value=found):
            result = riju.check_prices(SimpleNamespace(local_only=True))
        self.assertEqual(result["alerts"], [])
        self.assertEqual(riju.load_data()["products"][0]["status"], "unavailable")

    def _two_products(self):
        base = {"currency": "AUD", "drop_percent": 10, "price_history": [], "failure_count": 0, "last_alert_price": None}
        riju.save_data({"schema_version": 1, "products": [
            {**base, "id": "a", "name": "Coles Full Cream Milk 3L", "url": "https://shop.example/milk",
             "target_price": 4.45, "initial_price": 4.95, "current_price": 4.95},
            {**base, "id": "b", "name": "Huggies Skin Protect Newborn Nappies Size 1 54 Pack", "url": "https://shop.example/nappies",
             "target_price": 12.96, "initial_price": 14.40, "current_price": 14.40}]})

    def test_summary_when_nothing_changed(self):
        self._two_products()
        prices = {"https://shop.example/milk": 4.95, "https://shop.example/nappies": 14.40}
        with patch.object(riju, "scrape", side_effect=lambda url: {"status": "ok", "price": prices[url], "currency": "AUD"}):
            result = riju.check_prices(SimpleNamespace(local_only=True))
        msg = riju.summary_message(result)
        self.assertEqual(result["alerts"], [])
        self.assertTrue(msg.startswith("✅ Price check done: no price change (2 products)"))
        self.assertIn("Coles Full Cream Milk 3L $4.95", msg)
        self.assertIn("$14.40", msg)

    def test_summary_shows_changes_and_failures_without_alert(self):
        self._two_products()
        def fake(url):
            if "milk" in url:
                return {"status": "ok", "price": 4.80, "currency": "AUD"}
            raise RuntimeError("timed out")
        with patch.object(riju, "scrape", side_effect=fake):
            result = riju.check_prices(SimpleNamespace(local_only=True))
        msg = riju.summary_message(result)
        self.assertEqual(result["alerts"], [])
        self.assertIn("🔄 Coles Full Cream Milk 3L: $4.95 → $4.80, target $4.45", msg)
        self.assertIn("⚠️ Could not check Huggies Skin Protect Newbor", msg)

    def test_quiet_without_summary_stays_silent(self):
        args = riju.parser().parse_args(["check", "--quiet"])
        self.assertFalse(args.summary)
        args = riju.parser().parse_args(["check", "--quiet", "--summary", "--local-only"])
        self.assertTrue(args.summary and args.quiet and args.local_only)

    def test_slow_page_is_retried_once_and_succeeds(self):
        self._two_products()
        calls = []
        def flaky(url):
            calls.append(url)
            if "milk" in url and calls.count(url) == 1:
                raise RuntimeError('Camofox HTTP 500: {"error":"tab create timed out after 30000ms"}')
            return {"status": "ok", "price": 4.95 if "milk" in url else 14.40, "currency": "AUD"}
        with patch.object(riju, "scrape", side_effect=flaky):
            result = riju.check_prices(SimpleNamespace(local_only=True))
        self.assertEqual(calls.count("https://shop.example/milk"), 2)
        self.assertEqual(result["failed"], [])
        self.assertEqual(riju.load_data()["products"][0]["failure_count"], 0)

    def test_refused_page_is_not_retried(self):
        self._two_products()
        calls = []
        def refuse(url):
            calls.append(url)
            raise RuntimeError("Product page returned HTTP 429")
        with patch.object(riju, "scrape", side_effect=refuse):
            result = riju.check_prices(SimpleNamespace(local_only=True))
        self.assertEqual(len(calls), 2)  # one call per product, no retries
        self.assertEqual(len(result["failed"]), 2)

    def test_publish_updates_remote_main(self):
        def cmd(*args, cwd=None):
            return subprocess.run(args, cwd=cwd or self.root, check=True, capture_output=True, text=True)
        bare = self.root / "remote.git"
        cmd("git", "init", "--bare", str(bare))
        cmd("git", "init", "-b", "main")
        cmd("git", "config", "user.email", "test@example.com")
        cmd("git", "config", "user.name", "Riju Test")
        cmd("git", "remote", "add", "origin", str(bare))
        riju.save_data({"schema_version": 1, "products": []})
        cmd("git", "add", "data/products.json")
        cmd("git", "commit", "-m", "Initial")
        cmd("git", "push", "origin", "HEAD:main")
        riju.preflight(False)
        riju.save_data({"schema_version": 1, "products": [{"id": "abc"}]})
        self.assertEqual(riju.publish(False), "published")
        remote = json.loads(cmd("git", f"--git-dir={bare}", "show", "main:data/products.json").stdout)
        self.assertEqual(remote["products"][0]["id"], "abc")

    def test_scrape_uses_camofox_contract(self):
        calls = []
        def fake_request(method, path, data=None):
            calls.append((method, path, data))
            if method == "POST" and path == "/tabs":
                return {"tabId": "tab-1", "navigationOk": True}
            if path.endswith("/snapshot?userId=riju"):
                return {"url": "https://shop.example/camera", "snapshot": "Camera A"}
            if path.endswith("/evaluate"):
                return {"ok": True, "result": {"title": "Camera A", "url": "https://shop.example/camera",
                        "jsonld": [{"@type": "Product", "name": "Camera A",
                                    "offers": {"price": "79.95", "priceCurrency": "AUD"}}]}}
            if method == "DELETE":
                return {}
            raise AssertionError((method, path))
        with patch.object(riju, "request", side_effect=fake_request):
            found = riju.scrape("https://shop.example/camera")
        self.assertEqual((found["status"], found["price"], found["currency"]), ("ok", 79.95, "AUD"))
        self.assertEqual(found["requested_postcode"], "3820")
        self.assertEqual([c[0] for c in calls], ["POST", "GET", "POST", "DELETE"])

    def test_search_uses_postcode(self):
        args = SimpleNamespace(action="search", query="camera")
        with patch.object(riju, "new_tab", return_value="tab-1"), \
             patch.object(riju, "request", return_value={}) as api, \
             patch.object(riju, "snapshot", return_value={"url": "https://google.com", "snapshot": "Results"}), \
             patch.object(riju, "links", return_value=[]):
            result = riju.browser(args)
        self.assertEqual(result["search_postcode"], "3820")
        self.assertEqual(api.call_args.args[2]["query"], "camera Australia 3820")

    def test_compare_searches_multiple_stores_and_requires_exact_model(self):
        searches, closed = [], []
        def fake_browser(args):
            searches.append(args.query)
            store = args.query.split("site:")[1]
            return {"status": "ok", "tab_id": store, "links": [
                {"url": f"https://www.{store}/product", "title": "Headphones"}]}
        def fake_scrape(url):
            is_jb = "jbhifi" in url
            return {"status": "ok", "url": url,
                    "name": "Sony WH-1000XM5" if is_jb else "Sony WH-1000XM4",
                    "price": 399.0 if is_jb else 299.0, "currency": "AUD",
                    "evidence": "jsonld_product_offer", "availability": "InStock"}
        args = SimpleNamespace(query="Sony WH-1000XM5", model="WH-1000XM5",
                               store=["jbhifi.com.au", "thegoodguys.com.au"], max_per_store=2)
        with patch.object(riju, "browser", side_effect=fake_browser), \
             patch.object(riju, "scrape", side_effect=fake_scrape), \
             patch.object(riju, "close_tab", side_effect=closed.append):
            result = riju.compare_prices(args)
        self.assertEqual(len(searches), 2)
        self.assertTrue(all("site:" in query for query in searches))
        self.assertEqual(len(result["comparable_offers"]), 1)
        self.assertEqual(result["comparable_offers"][0]["price"], 399.0)
        self.assertEqual(result["other_candidates"][0]["identity"], "model_mismatch")
        self.assertEqual(len(closed), 2)


    def test_search_reads_camofox_link_shape(self):
        args = SimpleNamespace(action="search", query="camera")
        found = [{"url": "https://www.jbhifi.com.au/products/camera", "text": "Camera"},
                 {"url": "https://duckduckgo.com/about", "text": "About"}]
        with patch.object(riju, "new_tab", return_value="tab-1"), \
             patch.object(riju, "request", return_value={}), \
             patch.object(riju, "snapshot", return_value={"url": "https://duckduckgo.com/?q=camera&ia=web", "snapshot": "Results"}), \
             patch.object(riju, "links", return_value=found):
            result = riju.browser(args)
        self.assertEqual(result["links"], [{"title": "Camera", "url": "https://www.jbhifi.com.au/products/camera"}])

    def test_search_waits_for_results_and_dedupes(self):
        args = SimpleNamespace(action="search", query="camera")
        found = [{"url": "https://www.jbhifi.com.au/products/camera", "text": "jbhifi"},
                 {"url": "https://www.jbhifi.com.au/products/camera", "text": "Camera at JB Hi-Fi"}]
        shots = [{"url": "https://duckduckgo.com/?q=camera", "snapshot": "Loading"},
                 {"url": "https://duckduckgo.com/?q=camera&ia=web", "snapshot": "Results"},
                 {"url": "https://duckduckgo.com/?q=camera&ia=web", "snapshot": "Results"}]
        with patch.object(riju, "new_tab", return_value="tab-1"), \
             patch.object(riju, "request", return_value={}), \
             patch.object(riju, "snapshot", side_effect=shots), \
             patch.object(riju, "links", return_value=found), \
             patch.object(riju.time, "sleep"):
            result = riju.browser(args)
        self.assertEqual(result["snapshot"], "Results")
        self.assertEqual(result["links"], [{"title": "Camera at JB Hi-Fi", "url": "https://www.jbhifi.com.au/products/camera"}])


if __name__ == "__main__":
    unittest.main()
