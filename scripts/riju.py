#!/usr/bin/env python3
"""Small, dependency-free Camofox client and Git-backed price tracker."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(os.environ.get("RIJU_REPO_DIR", Path(__file__).resolve().parents[1])).expanduser()
DATA = ROOT / "data" / "products.json"
API = os.environ.get("CAMOFOX_URL", "http://127.0.0.1:9377").rstrip("/")
USER_ID = "riju"
# Slow shop pages can take ~60 s to open in the browser (12 Nov 2026); wait a little longer than that.
CAMOFOX_HTTP_TIMEOUT_S = 75
RETRY_WAIT_S = 5  # wait before the one retry of a page that failed to open
DEFAULT_POSTCODE = "3820"
DEFAULT_STORES = ("jbhifi.com.au", "thegoodguys.com.au", "harveynorman.com.au",
                  "amazon.com.au", "officeworks.com.au")
CHALLENGE = re.compile(r"captcha|verify you are human|cloudflare.*challenge|access denied|robot check|unusual traffic", re.I)
PRICE_JS = r"""(() => {
  const meta = {};
  for (const e of document.querySelectorAll('meta[property],meta[name],meta[itemprop]')) {
    const k = e.getAttribute('property') || e.getAttribute('name') || e.getAttribute('itemprop');
    if (k && e.content) meta[k.toLowerCase()] = e.content;
  }
  const jsonld = [];
  for (const e of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { jsonld.push(JSON.parse(e.textContent)); } catch (_) {}
  }
  const prices = [];
  for (const e of document.querySelectorAll('[itemprop="price"]')) {
    prices.push(e.getAttribute('content') || e.textContent || '');
  }
  return {title: document.title, url: location.href, meta, jsonld,
    itempropPrices: prices.slice(0, 10), text: (document.body?.innerText || '').slice(0, 16000)};
})()"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def configured_postcode() -> str:
    value = str(os.environ.get("RIJU_POSTCODE") or (load_data().get("settings") or {}).get("postcode") or DEFAULT_POSTCODE)
    if not re.fullmatch(r"\d{4}", value):
        raise ValueError("Postcode must be four digits")
    return value


def money(value) -> float | None:
    try:
        d = Decimal(str(value).replace(",", "").strip())
        if not d.is_finite() or d <= 0 or d > 100000000:
            return None
        return float(d.quantize(Decimal("0.01")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def public_url(value: str) -> str:
    p = urllib.parse.urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Use a public HTTP(S) product URL without credentials")
    host = p.hostname.lower()
    if host == "localhost" or host.endswith(".local") or "." not in host:
        raise ValueError("Local and private hosts are not supported")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError("Local and private IP addresses are not supported")
    except ValueError as e:
        if "not supported" in str(e):
            raise
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def canonical_url(url: str) -> str:
    p = urllib.parse.urlsplit(public_url(url))
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
            if not (k.lower().startswith("utm_") or k.lower() in {"fbclid", "gclid", "mc_cid", "mc_eid"})]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/",
                                     urllib.parse.urlencode(keep), ""))


def request(method: str, path: str, data=None):
    if not API.startswith("http://127.0.0.1:") and not API.startswith("http://localhost:"):
        raise RuntimeError("CAMOFOX_URL must point to a loopback server")
    url = API + path
    body = None if data is None else json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if os.environ.get("CAMOFOX_ACCESS_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["CAMOFOX_ACCESS_KEY"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=CAMOFOX_HTTP_TIMEOUT_S) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode(errors="replace")
        raise RuntimeError(f"Camofox HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Camofox unavailable at {API}: {e.reason}") from e


def tab_path(tab_id: str, suffix="") -> str:
    return "/tabs/" + urllib.parse.quote(tab_id, safe="") + suffix


def new_tab(url=None):
    payload = {"userId": USER_ID, "sessionKey": "riju-" + uuid.uuid4().hex}
    if url:
        payload["url"] = public_url(url)
    result = request("POST", "/tabs", payload)
    if not result.get("tabId"):
        raise RuntimeError(f"Camofox did not return a tabId: {result}")
    if result.get("navigationOk") is False:
        with contextlib.suppress(Exception):
            close_tab(result["tabId"])
        raise RuntimeError(f"Product page returned HTTP {result.get('httpStatus')}")
    return result["tabId"]


def close_tab(tab_id):
    request("DELETE", tab_path(tab_id) + "?userId=" + USER_ID)


def snapshot(tab_id):
    return request("GET", tab_path(tab_id, "/snapshot") + "?userId=" + USER_ID)


def links(tab_id):
    return request("GET", tab_path(tab_id, "/links") + "?userId=" + USER_ID + "&limit=200").get("links", [])


def settle_search(tab_id):
    """DuckDuckGo renders results after load and adds ia= to its URL once they are in."""
    shot = snapshot(tab_id)
    waited = False
    for _ in range(12):
        url = shot.get("url") or ""
        if urllib.parse.urlsplit(url).hostname != "duckduckgo.com" or "ia=" in url:
            break
        waited = True
        time.sleep(1)
        shot = snapshot(tab_id)
    if waited:
        time.sleep(1)
        shot = snapshot(tab_id)
    return shot


def browser(args):
    if args.action == "health":
        return request("GET", "/health")
    if args.action in ("open", "search"):
        tab_id = new_tab(args.url if args.action == "open" else None)
        if args.action == "search":
            postcode = configured_postcode()
            query = args.query if re.search(r"(?<!\d)" + re.escape(postcode) + r"(?!\d)", args.query) else f"{args.query} Australia {postcode}"
            try:
                request("POST", tab_path(tab_id, "/navigate"),
                        {"userId": USER_ID, "macro": "@google_search", "query": query})
            except Exception:
                with contextlib.suppress(Exception):
                    close_tab(tab_id)
                raise
        shot = settle_search(tab_id) if args.action == "search" else snapshot(tab_id)
        status = "blocked_by_challenge" if CHALLENGE.search(shot.get("snapshot", "")) else "ok"
        result = {"status": status, "tab_id": tab_id, "url": shot.get("url"),
                  "snapshot": shot.get("snapshot", "")[:20000]}
        if args.action == "search":
            result["search_postcode"] = postcode
            candidates = {}
            for link in links(tab_id):
                try:
                    dest = public_url(link.get("url") or link.get("href") or "")
                except ValueError:
                    continue
                host = urllib.parse.urlsplit(dest).hostname or ""
                if host.endswith("google.com") or host.endswith("google.com.au"):
                    wrapped = urllib.parse.parse_qs(urllib.parse.urlsplit(dest).query)
                    candidate = (wrapped.get("q") or wrapped.get("url") or [None])[0]
                    if not candidate:
                        continue
                    try:
                        dest = public_url(candidate)
                    except ValueError:
                        continue
                    host = urllib.parse.urlsplit(dest).hostname or ""
                    if host.endswith("google.com") or host.endswith("google.com.au"):
                        continue
                if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
                    continue
                title = link.get("text", "")[:180]
                if dest in candidates:
                    if len(title) > len(candidates[dest]["title"]):
                        candidates[dest]["title"] = title
                    continue
                if len(candidates) >= 15:
                    break
                candidates[dest] = {"title": title, "url": dest}
            result["links"] = list(candidates.values())
        return result
    if args.action == "snapshot":
        shot = snapshot(args.tab_id)
        return {"status": "ok", "tab_id": args.tab_id, "url": shot.get("url"),
                "snapshot": shot.get("snapshot", "")[:20000]}
    if args.action == "links":
        return {"status": "ok", "tab_id": args.tab_id, "links": links(args.tab_id)[:50]}
    if args.action == "scroll":
        request("POST", tab_path(args.tab_id, "/scroll"),
                {"userId": USER_ID, "direction": args.direction, "amount": args.amount})
        shot = snapshot(args.tab_id)
        return {"status": "ok", "tab_id": args.tab_id, "url": shot.get("url"),
                "snapshot": shot.get("snapshot", "")[:20000]}
    if args.action == "set-postcode":
        request("POST", tab_path(args.tab_id, "/type"),
                {"userId": USER_ID, "ref": args.ref, "text": configured_postcode(),
                 "clear": True, "submit": args.submit})
        shot = snapshot(args.tab_id)
        return {"status": "ok", "tab_id": args.tab_id, "requested_postcode": configured_postcode(),
                "url": shot.get("url"), "snapshot": shot.get("snapshot", "")[:20000]}
    if args.action == "click":
        before = snapshot(args.tab_id)
        matching = [line for line in before.get("snapshot", "").splitlines()
                    if re.search(r"(?<!\w)" + re.escape(args.ref) + r"(?!\w)", line)]
        if not matching:
            raise ValueError("Element ref not found in the current snapshot")
        if re.search(r"checkout|place order|buy now|pay now|confirm order|add to cart", matching[0], re.I):
            raise ValueError("Shopping and checkout controls are outside this browser skill")
        request("POST", tab_path(args.tab_id, "/click"), {"userId": USER_ID, "ref": args.ref})
        shot = snapshot(args.tab_id)
        return {"status": "ok", "tab_id": args.tab_id, "url": shot.get("url"),
                "snapshot": shot.get("snapshot", "")[:20000]}
    if args.action == "close":
        close_tab(args.tab_id)
        return {"status": "closed", "tab_id": args.tab_id}
    raise ValueError("Unknown browser action")


def product_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from product_nodes(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(t == "Product" or (isinstance(t, str) and t.rstrip("/").endswith("/Product")) for t in types):
            yield value
        if "@graph" in value:
            yield from product_nodes(value["@graph"])


def offer_value(offer):
    spec = offer.get("priceSpecification") or {}
    if isinstance(spec, list):
        spec = next((s for s in spec if isinstance(s, dict) and s.get("price")), {})
    if not isinstance(spec, dict):
        spec = {}
    return money(offer.get("price") or offer.get("lowPrice") or spec.get("price")), spec


def offer_for_page(offers, page_url):
    """Several sizes/variants with different prices: only the offer that matches the exact link (e.g. ?variant=123
    or .../product-variant-123) is trusted. Returns None when the link doesn't pick one size."""
    page = urllib.parse.urlsplit(page_url or "")
    page_variant = urllib.parse.parse_qs(page.query).get("variant", [None])[0]
    hits = []
    for offer in offers:
        o = urllib.parse.urlsplit(str(offer.get("url") or ""))
        o_variant = urllib.parse.parse_qs(o.query).get("variant", [None])[0]
        if page_variant and o_variant == page_variant:
            hits.append(offer)
        elif o.path and o.path.rstrip("/") == page.path.rstrip("/") and "variant" in o.path.lower():
            hits.append(offer)
    return hits[0] if len(hits) == 1 else None


def same_product(nodes):
    """True when every Product entry has the same name and one same price (shops like Shopify often repeat the product)."""
    names, prices = set(), set()
    for product in nodes:
        names.add(" ".join(str(product.get("name") or "").lower().split()))
        offers = product.get("offers") or []
        for offer in [offers] if isinstance(offers, dict) else offers:
            if isinstance(offer, dict):
                value = money(offer.get("price") or offer.get("lowPrice"))
                if value is not None:
                    prices.add(value)
    return len(names) == 1 and "" not in names and len(prices) == 1


def extract_product(page):
    title = page.get("title") or ""
    meta = page.get("meta") or {}
    products = [product for block in page.get("jsonld") or [] for product in product_nodes(block)]
    if len(products) > 1:
        page_url = urllib.parse.urlsplit(page.get("url") or "")
        matches = []
        for product in products:
            product_url = urllib.parse.urlsplit(product.get("url") or product.get("@id") or "")
            if (product_url.hostname == page_url.hostname and product_url.path.rstrip("/") == page_url.path.rstrip("/")):
                matches.append(product)
        pool = matches or products
        if len(matches) != 1 and len(pool) > 1 and same_product(pool):
            matches = pool[:1]
        if len(matches) != 1:
            return {"status": "needs_review", "name": title, "price": None, "currency": None,
                    "availability": None, "evidence": "ambiguous_product_data"}
        products = matches
    for product in products:
        offers = product.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        offers = [offer for offer in offers if isinstance(offer, dict)]
        if len({offer_value(offer)[0] for offer in offers} - {None}) > 1:
            chosen = offer_for_page(offers, page.get("url"))
            if chosen is None:
                return {"status": "needs_review", "name": product.get("name") or title, "price": None, "currency": None,
                        "availability": None, "evidence": "several_sizes"}
            value, spec = offer_value(chosen)
            return {"status": "ok", "name": product.get("name") or title, "price": value,
                    "currency": chosen.get("priceCurrency") or spec.get("priceCurrency") or meta.get("product:price:currency"),
                    "availability": chosen.get("availability"), "evidence": "jsonld_variant_offer"}
        for offer in offers:
            value, spec = offer_value(offer)
            if value is not None:
                return {"status": "ok", "name": product.get("name") or title,
                        "price": value, "currency": offer.get("priceCurrency") or spec.get("priceCurrency") or meta.get("product:price:currency"),
                        "availability": offer.get("availability"), "evidence": "jsonld_product_offer"}
    value = money(meta.get("product:price:amount") or meta.get("og:price:amount") or meta.get("price"))
    if value is not None:
        return {"status": "ok", "name": meta.get("og:title") or title, "price": value,
                "currency": meta.get("product:price:currency") or meta.get("og:price:currency"),
                "availability": None, "evidence": "price_meta"}
    for candidate in page.get("itempropPrices") or []:
        value = money(candidate)
        if value is not None:
            return {"status": "ok", "name": meta.get("og:title") or title, "price": value,
                    "currency": meta.get("product:price:currency"), "availability": None,
                    "evidence": "itemprop_price"}
    return {"status": "needs_review", "name": meta.get("og:title") or title,
            "price": None, "currency": None, "availability": None, "evidence": "no_structured_price"}


# --- Shopify shops' own product data (Session 40, approved by Riju 12 Nov 2026) ---------------------------------
# Every Shopify shop publishes /products/<handle>.js: the product with every size/variant, its name and price.
# Only for Australian shops (prices in AUD). Anything unexpected -> None, and the normal page reading is used.
SHOPIFY_FETCH_JS = "fetch(location.href, {credentials: 'omit'}).then(r => r.ok ? r.text() : '')"


def shopify_json_url(url):
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if not host.endswith(".au"):
        return None
    m = re.fullmatch(r"(?:/collections/[^/]+)?/products/([A-Za-z0-9][A-Za-z0-9_.%-]*)", p.path.rstrip("/"))
    if not m or m.group(1).endswith(".js") or m.group(1).endswith(".json"):
        return None
    return f"https://{host}/products/{m.group(1)}.js"


def shopify_product(url):
    """The shop's product data as a dict, or None (not Shopify / not readable)."""
    json_url = shopify_json_url(url)
    if not json_url:
        return None
    tab_id = new_tab()
    try:
        request("POST", tab_path(tab_id, "/navigate"), {"userId": USER_ID, "url": json_url})
        page = request("POST", tab_path(tab_id, "/evaluate"), {"userId": USER_ID, "expression": SHOPIFY_FETCH_JS})
        data = json.loads(page.get("result") or "") if page.get("ok") else None
    finally:
        with contextlib.suppress(Exception):
            close_tab(tab_id)
    if not isinstance(data, dict) or not isinstance(data.get("variants"), list) or not data["variants"]:
        return None
    return data


def shopify_variant_price(variant):
    value = variant.get("price")
    if isinstance(value, int):  # .js gives cents
        return money(Decimal(value) / 100)
    return money(value)


def from_shopify(data, url):
    """Pick the variant for this link. Returns a scrape-style result."""
    variants = [v for v in data["variants"] if isinstance(v, dict) and shopify_variant_price(v) is not None]
    if not variants:
        return None
    wanted = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("variant", [None])[0]
    chosen = None
    if wanted:
        chosen = next((v for v in variants if str(v.get("id")) == wanted), None)
        if chosen is None:
            return None  # the link's size isn't in the data: let the page decide
    elif len({shopify_variant_price(v) for v in variants}) == 1:
        chosen = next((v for v in variants if v.get("available")), variants[0])
    title = " ".join(str(data.get("title") or "").split())
    if chosen is None:
        sizes = ", ".join(f"{v.get('title')} ${shopify_variant_price(v):.2f}" for v in variants[:6])
        return {"status": "needs_review", "name": title, "price": None, "currency": None, "availability": None,
                "evidence": "several_sizes", "sizes": sizes}
    vt = str(chosen.get("title") or "")
    name = title if vt.lower() in ("", "default title") or vt.lower() in title.lower() else f"{title} - {vt}"
    return {"status": "ok", "name": name, "price": shopify_variant_price(chosen), "currency": "AUD",
            "availability": "https://schema.org/InStock" if chosen.get("available") else "https://schema.org/OutOfStock",
            "evidence": "shopify_product_json"}


def shopify_variant_for_size(url, wanted_size):
    """Session 40: in a search, the variant whose name matches the size typed (e.g. "4kg") -> (exact link, result)."""
    data = shopify_product(url)
    if not data:
        return None
    want = re.sub(r"[^a-z0-9.]", "", wanted_size.lower())
    size_re = re.compile(r"(?<![0-9.])" + re.escape(want) + r"(?![0-9])") if want else None
    hits = [v for v in data["variants"] if isinstance(v, dict) and size_re
            and size_re.search(re.sub(r"[^a-z0-9.]", "", str(v.get("title") or "").lower()))]
    if len(hits) != 1:
        return None
    p = urllib.parse.urlsplit(url)
    exact = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, f"variant={hits[0].get('id')}", ""))
    result = from_shopify(data, exact)
    return (exact, result) if result and result.get("status") == "ok" else None


def scrape(url):
    url = public_url(url)
    # Session 40: Shopify shops' own product data first (fast, exact sizes); the page if that isn't possible.
    if shopify_json_url(url):
        try:
            data = shopify_product(url)
            found = from_shopify(data, url) if data else None
        except Exception:  # noqa: BLE001 - never let this path break normal reading
            found = None
        if found:
            found.update({"url": url, "snapshot": "", "requested_postcode": configured_postcode(), "postcode_visible": False})
            return found
    # Slow shop pages (12 Nov 2026): open an empty tab, then navigate (its time limit can be raised with
    # NAVIGATE_TIMEOUT_MS). If loading still times out, read what has loaded: product data comes first.
    tab_id = new_tab()
    nav_error = None
    try:
        try:
            request("POST", tab_path(tab_id, "/navigate"), {"userId": USER_ID, "url": url})
        except (RuntimeError, OSError) as exc:
            if re.search(r"HTTP 4(?!08)\d\d", str(exc)):  # the shop refused / not found: don't read on
                raise
            nav_error = exc
        shot = snapshot(tab_id)
        page = request("POST", tab_path(tab_id, "/evaluate"), {"userId": USER_ID, "expression": PRICE_JS})
        if not page.get("ok"):
            raise RuntimeError(f"Camofox evaluate failed: {page}")
        data = page.get("result") or {}
        if not isinstance(data, dict):
            raise RuntimeError("Camofox evaluate returned no page data")
        text = (shot.get("snapshot") or "") + "\n" + (data.get("text") or "")[:2000]
        if CHALLENGE.search(text):
            return {"status": "blocked_by_challenge", "url": shot.get("url") or url,
                    "name": data.get("title"), "price": None, "requested_postcode": configured_postcode(),
                    "postcode_visible": False, "snapshot": shot.get("snapshot", "")[:5000]}
        result = extract_product(data)
        if nav_error is not None and result.get("status") != "ok":
            raise nav_error  # page never loaded far enough: report the real problem
        result["url"] = public_url(data.get("url") or shot.get("url") or url)
        result["snapshot"] = (shot.get("snapshot") or "")[:5000]
        result["requested_postcode"] = configured_postcode()
        result["postcode_visible"] = bool(re.search(r"(?<!\d)" + re.escape(configured_postcode()) + r"(?!\d)", text))
        return result
    finally:
        with contextlib.suppress(Exception):
            close_tab(tab_id)


def store_domain(value):
    domain = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", domain) or ".." in domain:
        raise ValueError("--store must be a retailer domain such as jbhifi.com.au")
    return domain


def same_store(url, domain):
    host = urllib.parse.urlsplit(url).hostname or ""
    return host == domain or host.endswith("." + domain)


def model_matches(name, model):
    if not model:
        return False
    parts = re.findall(r"[a-z0-9]+", model.lower())
    if not parts:
        return False
    pattern = r"(?<![a-z0-9])" + r"[\W_]*".join(map(re.escape, parts)) + r"(?![a-z0-9])"
    return bool(re.search(pattern, name or "", re.I))


def compare_prices(args):
    stores = list(dict.fromkeys(store_domain(x) for x in (args.store or DEFAULT_STORES)))
    if not 2 <= len(stores) <= 8:
        raise ValueError("Compare 2 to 8 distinct retailer domains")
    if args.max_per_store not in (1, 2, 3):
        raise ValueError("--max-per-store must be 1, 2, or 3")
    offers, problems = [], []
    for store in stores:
        try:
            found = browser(argparse.Namespace(action="search", query=f'{args.query} site:{store}'))
            try:
                if found["status"] != "ok":
                    problems.append({"store": store, "reason": found["status"]})
                    continue
                candidates = []
                for link in found.get("links", []):
                    url = canonical_url(link["url"])
                    if same_store(url, store) and url not in candidates:
                        candidates.append(url)
                if not candidates:
                    problems.append({"store": store, "reason": "No direct retailer links found"})
                for url in candidates[:args.max_per_store]:
                    try:
                        item = scrape(url)
                        identity = "model_matched" if model_matches(item.get("name"), args.model) else (
                            "model_mismatch" if args.model else "identity_review_required")
                        offers.append({"store": store, "url": item.get("url") or url,
                                       "name": item.get("name"), "price": item.get("price"),
                                       "currency": item.get("currency"), "status": item.get("status"),
                                       "availability": item.get("availability"), "evidence": item.get("evidence"),
                                       "identity": identity, "postcode_visible": item.get("postcode_visible", False)})
                    except (RuntimeError, ValueError, OSError) as exc:
                        problems.append({"store": store, "url": url, "reason": str(exc)[:200]})
            finally:
                with contextlib.suppress(Exception):
                    close_tab(found["tab_id"])
        except (RuntimeError, ValueError, OSError) as exc:
            problems.append({"store": store, "reason": str(exc)[:200]})
    comparable = [item for item in offers if item["status"] == "ok" and
                  item["identity"] == "model_matched" and item["currency"] and
                  available(item["availability"])]
    comparable.sort(key=lambda item: (item["currency"], item["price"]))
    return {"query": args.query, "model": args.model, "requested_postcode": configured_postcode(),
            "stores_searched": stores, "comparable_offers": comparable,
            "other_candidates": [item for item in offers if item not in comparable],
            "problems": problems,
            "note": "Compare only exact model matches with the same currency. Verify variant, stock, shipping, and selected postcode before tracking."}


def load_data():
    if not DATA.exists():
        return {"schema_version": 1, "settings": {"postcode": DEFAULT_POSTCODE}, "products": []}
    value = json.loads(DATA.read_text())
    if value.get("schema_version") != 1 or not isinstance(value.get("products"), list):
        raise ValueError("Unsupported products.json schema")
    return value


def save_data(data):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=DATA.parent, prefix=".products-", delete=False) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        temp = Path(f.name)
    temp.replace(DATA)


@contextlib.contextmanager
def lock_data():
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / ".riju.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def git(*args, check=True):
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"git {' '.join(args)} failed")
    return result


def preflight(local_only):
    if local_only:
        return
    git("rev-parse", "--is-inside-work-tree")
    if not git("remote", "get-url", "origin", check=False).returncode == 0:
        raise RuntimeError("Set a GitHub origin remote, or use --local-only for a local trial")
    git("pull", "--ff-only", "origin", "main")


def publish(local_only):
    if local_only:
        return "local_only"
    git("add", "data/products.json")
    if not git("diff", "--cached", "--quiet", check=False).returncode == 0:
        git("commit", "-m", "Update Riju product tracking")
    git("push", "origin", "HEAD:main")
    return "published"


def find_product(data, product_id):
    for item in data["products"]:
        if item["id"] == product_id:
            return item
    raise ValueError(f"No product with id {product_id}")


def threshold(product):
    if product.get("target_price") is not None:
        return product["target_price"]
    if product.get("initial_price") is not None:
        return round(product["initial_price"] * (1 - product["drop_percent"] / 100), 2)
    return None


def available(offer):
    value = str(offer or "").lower()
    return not any(token in value for token in ("outofstock", "soldout", "discontinued", "preorder"))


def shopping(args):
    if args.action == "list":
        return load_data()
    if args.action == "add":
        checked = scrape(args.url)
        with lock_data():
            preflight(args.local_only)
            data = load_data()
            key = canonical_url(args.url)
            if any(canonical_url(p["url"]) == key for p in data["products"]):
                raise ValueError("That product URL is already tracked")
            currency = args.currency or checked.get("currency")
            if not currency:
                raise ValueError("The page did not identify its currency; pass --currency explicitly")
            current = checked.get("price")
            item = {"id": uuid.uuid4().hex[:12], "name": args.name or checked.get("name") or "Unnamed product",
                    "url": key, "retailer": urllib.parse.urlsplit(key).hostname,
                    "postcode": configured_postcode(), "postcode_visible": checked.get("postcode_visible", False),
                    "currency": currency.upper(), "target_price": money(args.target) if args.target else None,
                    "drop_percent": args.drop, "initial_price": current, "current_price": current,
                    "last_checked_at": now(), "status": checked["status"] if available(checked.get("availability")) else "unavailable",
                    "availability": checked.get("availability"),
                    "evidence": checked.get("evidence"), "last_error": None,
                    "failure_count": 0, "last_alert_price": None, "created_at": now(),
                    "price_history": ([{"at": now(), "price": current}] if current is not None else [])}
            data["products"].append(item)
            save_data(data)
            sync = publish(args.local_only)
        return {"status": "added", "sync": sync, "product": item,
                "note": "Price alerts start after a verified price is found" if current is None else None}
    if args.action in ("remove", "set-target"):
        with lock_data():
            preflight(args.local_only)
            data = load_data()
            item = find_product(data, args.id)
            if args.action == "remove":
                data["products"].remove(item)
            else:
                item["target_price"] = money(args.target) if args.target else None
                item["last_alert_price"] = None
            save_data(data)
            sync = publish(args.local_only)
        return {"status": "updated", "sync": sync, "id": args.id}
    raise ValueError("Unknown shopping action")


def scrape_with_retry(url):
    """riju.scrape, retried once after a short wait if the page failed to open.

    Shops that refuse us (HTTP 429/403) are not retried: we read pages politely.
    """
    try:
        return scrape(url)
    except (RuntimeError, OSError) as exc:
        if "HTTP 429" in str(exc) or "HTTP 403" in str(exc):
            raise
        time.sleep(RETRY_WAIT_S)
        return scrape(url)


def check_prices(args):
    alerts, changes, failed = [], [], []
    with lock_data():
        preflight(args.local_only)
        data = load_data()
        for product in data["products"]:
            try:
                found = scrape_with_retry(product["url"])
                if found["status"] != "ok" or found.get("price") is None:
                    raise RuntimeError(found["status"])
                if found.get("currency") and found["currency"].upper() != product["currency"].upper():
                    raise RuntimeError(f"Currency changed from {product['currency']} to {found['currency']}")
                old = product.get("current_price")
                price = found["price"]
                product["current_price"] = price
                product["name"] = product["name"] or found.get("name")
                product["status"] = "ok" if available(found.get("availability")) else "unavailable"
                product["availability"] = found.get("availability")
                product["postcode_visible"] = found.get("postcode_visible", False)
                product["evidence"] = found.get("evidence")
                product["last_error"] = None
                product["failure_count"] = 0
                product["last_checked_at"] = now()
                if product.get("initial_price") is None:
                    product["initial_price"] = price
                if old is not None and old != price:
                    changes.append({"name": product["name"], "old": old, "new": price, "target": threshold(product)})
                if old != price:
                    product["price_history"].append({"at": now(), "price": price})
                    product["price_history"] = product["price_history"][-90:]
                limit = threshold(product)
                prior_alert = product.get("last_alert_price")
                if product["status"] == "unavailable":
                    continue
                if limit is not None and price > limit:
                    product["last_alert_price"] = None
                elif limit is not None and price <= limit and (prior_alert is None or price <= round(prior_alert * 0.95, 2)):
                    product["last_alert_price"] = price
                    alerts.append(f"Price alert: {product['name']} — {product['currency']} {price:.2f} (target {limit:.2f})\n{product['url']}")
            except Exception as e:
                failed.append(product["name"])
                product["failure_count"] = product.get("failure_count", 0) + 1
                product["status"] = "check_failed"
                product["last_error"] = str(e)[:300]
                product["last_checked_at"] = now()
                if product["failure_count"] == 3:
                    alerts.append(f"Price check needs attention: {product['name']} failed 3 times. {product['last_error']}\n{product['url']}")
        save_data(data)
        sync = publish(args.local_only)
    prices = [{"name": p["name"], "price": p.get("current_price"), "currency": p.get("currency")}
              for p in data["products"]]
    return {"status": "checked", "sync": sync, "alerts": alerts, "count": len(data["products"]),
            "changes": changes, "failed": failed, "prices": prices}


def short_name(name, limit=28):
    name = " ".join(str(name or "").split())
    return name if len(name) <= limit else name[:limit - 1].rstrip() + "…"


def summary_message(result):
    """One short message for a check with no alert, so silence never means 'all OK'."""
    count = result.get("count", 0)
    changes, failed = result.get("changes") or [], result.get("failed") or []
    if not changes and not failed:
        lines = [f"✅ Price check done: no price change ({count} products)"]
    else:
        lines = [f"Price check done ({count} products), no alerts"]
        for c in changes:
            target = f", target ${c['target']:.2f}" if c.get("target") is not None else ""
            lines.append(f"🔄 {short_name(c['name'])}: ${c['old']:.2f} → ${c['new']:.2f}{target}")
        for name in failed:
            lines.append(f"⚠️ Could not check {short_name(name)} this time")
    shown = [f"{short_name(p['name'])} ${p['price']:.2f}" for p in result.get("prices") or [] if p.get("price") is not None]
    if shown:
        lines.append(" · ".join(shown))
    return "\n".join(lines)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("browser")
    bs = b.add_subparsers(dest="action", required=True)
    bs.add_parser("health")
    q = bs.add_parser("search"); q.add_argument("query")
    o = bs.add_parser("open"); o.add_argument("url")
    for action in ("snapshot", "links", "close"):
        x = bs.add_parser(action); x.add_argument("tab_id")
    s = bs.add_parser("scroll"); s.add_argument("tab_id")
    s.add_argument("--direction", choices=["up", "down"], default="down")
    s.add_argument("--amount", type=int, default=800)
    pc = bs.add_parser("set-postcode"); pc.add_argument("tab_id"); pc.add_argument("ref")
    pc.add_argument("--submit", action="store_true")
    cl = bs.add_parser("click"); cl.add_argument("tab_id"); cl.add_argument("ref")
    pr = sub.add_parser("price")
    ps = pr.add_subparsers(dest="action", required=True)
    sc = ps.add_parser("scrape"); sc.add_argument("url")
    co = ps.add_parser("compare"); co.add_argument("query")
    co.add_argument("--model", help="Exact model code required for automatic like-for-like ranking")
    co.add_argument("--store", action="append", help="Retailer domain; repeat for 2-8 stores")
    co.add_argument("--max-per-store", type=int, default=2)
    sh = sub.add_parser("shopping")
    ss = sh.add_subparsers(dest="action", required=True)
    ss.add_parser("list")
    a = ss.add_parser("add"); a.add_argument("url")
    a.add_argument("--name"); a.add_argument("--target")
    a.add_argument("--drop", type=float, default=10)
    a.add_argument("--currency")
    r = ss.add_parser("remove"); r.add_argument("id")
    t = ss.add_parser("set-target"); t.add_argument("id"); t.add_argument("--target")
    c = sub.add_parser("check")
    for x in (a, r, t, c):
        x.add_argument("--local-only", action="store_true")
    c.add_argument("--quiet", action="store_true", help="Print only alerts; empty stdout means no Telegram notification")
    c.add_argument("--summary", action="store_true", help="With --quiet: when there is no alert, print a short 'no price change' summary instead of nothing")
    return p


def main():
    args = parser().parse_args()
    if getattr(args, "drop", 10) <= 0 or getattr(args, "drop", 10) >= 100:
        raise ValueError("--drop must be between 0 and 100")
    if getattr(args, "target", None) and money(args.target) is None:
        raise ValueError("--target must be a positive price")
    if args.command == "browser":
        result = browser(args)
    elif args.command == "price":
        result = scrape(args.url) if args.action == "scrape" else compare_prices(args)
    elif args.command == "shopping":
        result = shopping(args)
    else:
        result = check_prices(args)
    if args.command == "check" and args.quiet:
        if result["alerts"]:
            print("\n\n".join(result["alerts"]))
        elif getattr(args, "summary", False):
            print(summary_message(result))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as e:
        print(f"Riju error: {e}", file=sys.stderr)
        sys.exit(1)
