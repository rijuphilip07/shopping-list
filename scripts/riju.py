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
DEFAULT_POSTCODE = "3820"
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
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=45) as r:
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
    return request("GET", tab_path(tab_id, "/links") + "?userId=" + USER_ID).get("links", [])


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
        shot = snapshot(tab_id)
        status = "blocked_by_challenge" if CHALLENGE.search(shot.get("snapshot", "")) else "ok"
        result = {"status": status, "tab_id": tab_id, "url": shot.get("url"),
                  "snapshot": shot.get("snapshot", "")[:20000]}
        if args.action == "search":
            result["search_postcode"] = postcode
            candidates = []
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
                candidates.append({"title": link.get("text", "")[:180], "url": dest})
                if len(candidates) >= 15:
                    break
            result["links"] = candidates
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
        if len(matches) != 1:
            return {"status": "needs_review", "name": title, "price": None, "currency": None,
                    "availability": None, "evidence": "ambiguous_product_data"}
        products = matches
    for product in products:
        offers = product.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            spec = offer.get("priceSpecification") or {}
            if isinstance(spec, list):
                spec = next((s for s in spec if isinstance(s, dict) and s.get("price")), {})
            if not isinstance(spec, dict):
                spec = {}
            value = money(offer.get("price") or offer.get("lowPrice") or spec.get("price"))
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


def scrape(url):
    url = public_url(url)
    tab_id = new_tab(url)
    try:
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
        result["url"] = public_url(data.get("url") or shot.get("url") or url)
        result["snapshot"] = (shot.get("snapshot") or "")[:5000]
        result["requested_postcode"] = configured_postcode()
        result["postcode_visible"] = bool(re.search(r"(?<!\d)" + re.escape(configured_postcode()) + r"(?!\d)", text))
        return result
    finally:
        with contextlib.suppress(Exception):
            close_tab(tab_id)


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


def check_prices(args):
    alerts = []
    with lock_data():
        preflight(args.local_only)
        data = load_data()
        for product in data["products"]:
            try:
                found = scrape(product["url"])
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
                product["failure_count"] = product.get("failure_count", 0) + 1
                product["status"] = "check_failed"
                product["last_error"] = str(e)[:300]
                product["last_checked_at"] = now()
                if product["failure_count"] == 3:
                    alerts.append(f"Price check needs attention: {product['name']} failed 3 times. {product['last_error']}\n{product['url']}")
        save_data(data)
        sync = publish(args.local_only)
    return {"status": "checked", "sync": sync, "alerts": alerts, "count": len(data["products"])}


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
        result = scrape(args.url)
    elif args.command == "shopping":
        result = shopping(args)
    else:
        result = check_prices(args)
    if args.command == "check" and args.quiet:
        if result["alerts"]:
            print("\n\n".join(result["alerts"]))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as e:
        print(f"Riju error: {e}", file=sys.stderr)
        sys.exit(1)
