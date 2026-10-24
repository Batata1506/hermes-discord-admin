#!/usr/bin/env python3
"""Outfit-finding tools for Hermes, as a tiny MCP server.

Standard library only: no third-party packages. Speaks MCP over stdio
(newline-delimited JSON-RPC).

Hermes does the styling and its own web_search / web_extract. This server
adds what Hermes can't do on its own:
  - a free image search (DuckDuckGo), to find store photos of a piece
  - optional paid search through SerpApi (Google Shopping prices, Google Lens
    photo matching) when SERPAPI_KEY is set; without it, the free search is used
  - reading a product page's name, price and photos, where the store allows it
  - saving photos into Hermes's image cache, so they can be posted in Discord
  - remembering the style profile, past looks and what was liked

Data lives in $HERMES_HOME/fashion/, photos in $HERMES_HOME/cache/images/fashion/.
SERPAPI_KEY is read from the environment or Hermes's .env file. If Pillow
happens to be installed, big photos are shrunk to fit Lens's upload limit.
"""

import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

SERVER_NAME = "fashion"
SERVER_VERSION = "0.1.0"

HOME = os.environ.get("HERMES_HOME", "/opt/data")
DATA_DIR = os.environ.get("FASHION_DATA_DIR") or os.path.join(HOME, "fashion")
# Hermes only posts files from its cache folders, so photos must live under cache/images.
IMAGE_DIR = os.path.join(HOME, "cache", "images", "fashion")
KEEP_IMAGE_DAYS = int(os.environ.get("FASHION_KEEP_IMAGE_DAYS", "60"))
REGION = os.environ.get("FASHION_SEARCH_REGION", "ie-en")  # DuckDuckGo region code

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
SEARCH_GAP_SECONDS = 6.0     # DuckDuckGo starts asking for a bot check on bursts
SEARCH_PAUSE_SECONDS = 1800  # after it does, leave it alone; the check can last hours
VERDICTS = ["love", "like", "meh", "dislike"]

SERPAPI = "https://serpapi.com"
# Stop using SerpApi after this many searches a month; 250 is SerpApi's free plan.
SERPAPI_MONTHLY_LIMIT = int(os.environ.get("FASHION_SERPAPI_MONTHLY_LIMIT", "250"))
LENS_UPLOAD_MAX = 500 * 1024  # SerpApi's limit for uploaded photos


class ToolError(Exception):
    pass


def log(msg):
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- HTTP

def check_public(url):
    """Only fetch public http(s) addresses: never localhost or the private network."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError(f"Not a web address: {url}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443)
    except socket.gaierror:
        raise ToolError(f"Could not find the site {parts.hostname}.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ToolError(f"Refusing to fetch a private address: {parts.hostname}")


class PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(PublicRedirects)


def clean_url(url):
    """Escape spaces and other characters some CDNs put in their image links."""
    return urllib.parse.quote(url.strip(), safe=":/?&=%~+,;@!$'()*#[]")


def fetch(url, accept="text/html,application/xhtml+xml,*/*;q=0.8", referer=None,
          limit=MAX_PAGE_BYTES, timeout=20):
    url = clean_url(url)
    check_public(url)
    host = urllib.parse.urlsplit(url).hostname
    headers = {"User-Agent": BROWSER_UA, "Accept": accept, "Accept-Language": "en-IE,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            data = resp.read(limit + 1)
            if len(data) > limit:
                raise ToolError(f"{host} sent more than {limit // (1024 * 1024)} MB; skipped.")
            return data, resp.headers.get_content_charset() or "utf-8", resp.geturl()
    except urllib.error.HTTPError as e:
        raise ToolError(f"{host} answered HTTP {e.code}.")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise ToolError(f"Could not reach {host}: {getattr(e, 'reason', e)}")


def fetch_text(url, **kw):
    data, charset, final_url = fetch(url, **kw)
    return data.decode(charset, errors="replace"), final_url


# ---------------------------------------------------------------- storage

def data_path(name):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, name)


def load_json(name, default):
    try:
        with open(data_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name, value):
    path = data_path(name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prune_old_images():
    cutoff = time.time() - KEEP_IMAGE_DAYS * 86400
    for root, _, files in os.walk(IMAGE_DIR):
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------- image search

_last_search = 0.0
_paused_until = 0.0
SEARCH_BLOCKED = ("DuckDuckGo asked for a bot check, so image search is paused for now. "
                  "Find the product with web_search instead, then get its photos with "
                  "product_info or web_extract.")


def image_search(args):
    global _last_search, _paused_until
    query = args["query"].strip()
    if args.get("site"):
        query = f"site:{args['site'].strip()} {query}"
    limit = max(1, min(int(args.get("limit", 8)), 25))
    if time.time() < _paused_until:
        raise ToolError(SEARCH_BLOCKED)

    wait = SEARCH_GAP_SECONDS - (time.time() - _last_search)
    if wait > 0:
        time.sleep(wait)
    _last_search = time.time()

    try:
        page, _ = fetch_text("https://duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": query, "iax": "images", "ia": "images"}))
        token = re.search(r"vqd=[\"']?([\d-]+)", page)
        raw = None
        if token:
            raw, _ = fetch_text("https://duckduckgo.com/i.js?" + urllib.parse.urlencode(
                {"l": REGION, "o": "json", "q": query, "vqd": token.group(1), "f": ",,,,,",
                 "p": "1"}), accept="application/json", referer="https://duckduckgo.com/")
        results = json.loads(raw)["results"] if raw else None
    except ToolError as e:
        if "HTTP 403" not in str(e) and "HTTP 429" not in str(e):
            raise
        results = None
    except (ValueError, KeyError):  # a bot-check challenge instead of results
        results = None
    if results is None:
        _paused_until = time.time() + SEARCH_PAUSE_SECONDS
        raise ToolError(SEARCH_BLOCKED)

    return [{"title": r.get("title", ""), "image": r["image"], "page": r.get("url", ""),
             "site": urllib.parse.urlsplit(r.get("url", "")).hostname or "",
             "size": f"{r.get('width')}x{r.get('height')}"}
            for r in results[:limit] if r.get("image")]


# ---------------------------------------------------------------- SerpApi (optional, paid)

NO_KEY = ("{} needs a SerpApi key (SERPAPI_KEY), which isn't set. Use the free tools instead: "
          "image_search, product_info, web_search, web_extract, and vision_analyze for photos.")
CURRENCY_SIGNS = {"€": "EUR", "£": "GBP", "$": "USD"}


def env_value(name):
    """A setting from the environment, or from Hermes's .env file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        with open(os.path.join(HOME, ".env")) as f:
            for line in f:
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() == name:
                    value = val.strip().strip("'\"")
    except OSError:
        pass
    return value


def serpapi_used():
    usage = load_json("serpapi_usage.json", {})
    return usage.get("searches", 0) if usage.get("month") == datetime.now().strftime("%Y-%m") else 0


def paid_search_status():
    return {"enabled": bool(env_value("SERPAPI_KEY")), "used_this_month": serpapi_used(),
            "monthly_limit": SERPAPI_MONTHLY_LIMIT}


def can_use_serpapi():
    return bool(env_value("SERPAPI_KEY")) and serpapi_used() < SERPAPI_MONTHLY_LIMIT


def serpapi_request(req):
    # Errors never include the request URL: it carries the API key.
    try:
        with _opener.open(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except ValueError:
            raise ToolError(f"SerpApi answered HTTP {e.code}.")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise ToolError(f"Could not reach SerpApi: {getattr(e, 'reason', e)}")
    except ValueError:
        raise ToolError("SerpApi sent back something that isn't JSON.")
    error = data.get("error") if isinstance(data, dict) else None
    if error and "returned any results" in error:
        return {}  # an empty search, not a failure
    if error:
        raise ToolError(f"SerpApi: {error}")
    return data


def serpapi(what, params):
    """One SerpApi search; counted toward the monthly limit only when it succeeds."""
    key = env_value("SERPAPI_KEY")
    if not key:
        raise ToolError(NO_KEY.format(what))
    if serpapi_used() >= SERPAPI_MONTHLY_LIMIT:
        raise ToolError(f"This month's SerpApi allowance ({SERPAPI_MONTHLY_LIMIT} searches) is used "
                        "up. Use image_search, web_search and product_info until next month.")
    query = urllib.parse.urlencode({**params, "api_key": key})
    data = serpapi_request(urllib.request.Request(f"{SERPAPI}/search?{query}",
                                                  headers={"Accept": "application/json"}))
    month = datetime.now().strftime("%Y-%m")
    usage = load_json("serpapi_usage.json", {})
    if usage.get("month") != month:
        usage = {"month": month, "searches": 0}
    usage["searches"] += 1
    save_json("serpapi_usage.json", usage)
    return data


def currency_of(price_text):
    return next((code for sign, code in CURRENCY_SIGNS.items() if sign in str(price_text)), None)


def compact(d):
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def shopping_search(args):
    limit = max(1, min(int(args.get("limit", 8)), 20))
    if not can_use_serpapi():
        status = paid_search_status()
        why = ("this month's SerpApi allowance is used up" if status["enabled"]
               else "no SerpApi key is set")
        return {"source": "duckduckgo images (free)",
                "note": f"Free search because {why}: no prices. Check prices with product_info "
                        "or web_extract on each result's page.",
                "results": image_search({"query": args["query"], "limit": limit})}

    params = {"engine": "google_shopping", "q": args["query"], "gl": "ie", "hl": "en",
              "google_domain": "google.ie"}
    params.update({k: args[k] for k in ("min_price", "max_price") if args.get(k) is not None})
    data = serpapi("shopping_search", params)
    return {"source": "google shopping (SerpApi)", "searches_used_this_month": serpapi_used(),
            "results": [compact({
                "title": r.get("title"), "store": r.get("source"), "price": r.get("extracted_price"),
                "currency": currency_of(r.get("price")), "delivery": r.get("delivery"),
                "photo": r.get("serpapi_thumbnail") or r.get("thumbnail"),
                "google_link": r.get("product_link"),
                "product_token": r.get("immersive_product_page_token")})
                for r in data.get("shopping_results", [])[:limit]]}


def shopping_stores(args):
    data = serpapi("shopping_stores", {"engine": "google_immersive_product",
                                       "page_token": args["product_token"]})
    product = data.get("product_results") or {}
    return compact({
        "title": product.get("title"), "brand": product.get("brand"),
        "photos": (product.get("thumbnails") or [])[:8],
        "stores": [compact({"store": s.get("name"), "link": s.get("link"),
                            "price": s.get("extracted_price"), "currency": currency_of(s.get("price")),
                            "shipping": s.get("shipping"),
                            "details": ", ".join(map(str, s.get("details_and_offers") or []))})
                   for s in (product.get("stores") or [])[:8]],
        "searches_used_this_month": serpapi_used()})


def shrink_for_lens(data):
    try:
        from PIL import Image
    except ImportError:
        raise ToolError("That photo is over 500 KB, the most SerpApi takes as an upload. "
                        "Pass its web link as image_url instead.")
    img = Image.open(io.BytesIO(data)).convert("RGB")
    for side in (1600, 1200, 900, 640):
        img.thumbnail((side, side))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        if out.tell() <= LENS_UPLOAD_MAX:
            return out.getvalue(), "jpg"
    raise ToolError("Couldn't shrink that photo under 500 KB for Lens.")


def upload_for_lens(path):
    """Upload a photo from Hermes's cache to SerpApi; returns an image_id that lasts 10 minutes."""
    real = os.path.realpath(path)
    roots = [os.path.realpath(os.path.join(HOME, d))
             for d in ("cache", "image_cache", "document_cache")]
    if not any(real.startswith(root + os.sep) for root in roots):
        raise ToolError("Only photos in Hermes's cache folder can be sent to Lens.")
    try:
        with open(real, "rb") as f:
            data = f.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        raise ToolError(f"Can't read {path}.")
    ext = image_type(data)
    if ext not in ("jpg", "png", "webp"):
        raise ToolError("Lens only takes JPEG, PNG or WebP photos.")
    if len(data) > LENS_UPLOAD_MAX:
        data, ext = shrink_for_lens(data)

    boundary = "fashion" + hashlib.sha1(os.urandom(16)).hexdigest()
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext]
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"api_key\"\r\n\r\n"
            f"{env_value('SERPAPI_KEY')}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
            f"filename=\"photo.{ext}\"\r\nContent-Type: {mime}\r\n\r\n").encode()
    body += data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{SERPAPI}/image", data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    image_id = serpapi_request(req).get("image_id")
    if not image_id:
        raise ToolError("SerpApi didn't accept the photo upload.")
    return image_id


def lens_search(args):
    if not env_value("SERPAPI_KEY"):
        raise ToolError(NO_KEY.format("lens_search"))
    limit = max(1, min(int(args.get("limit", 10)), 30))
    params = {"engine": "google_lens", "type": "visual_matches", "country": "ie", "hl": "en"}
    if args.get("image_url"):
        params["url"] = args["image_url"]
    elif args.get("image_path"):
        params["image_id"] = upload_for_lens(args["image_path"])
    else:
        raise ToolError("Pass image_url (the photo's web link) or image_path (a saved photo).")
    if args.get("query"):
        params["q"] = args["query"]
    data = serpapi("lens_search", params)

    matches = []
    for m in data.get("visual_matches", [])[:limit]:
        price, currency = m.get("price"), None  # only some matches have a price
        if isinstance(price, dict):
            currency = currency_of(price.get("currency")) or price.get("currency")
            price = price.get("extracted_value") or price.get("value")
        elif price is not None:
            currency = currency_of(price)
        matches.append(compact({
            "title": m.get("title"), "store": m.get("source"), "page": m.get("link"),
            "photo": m.get("image") or m.get("thumbnail"), "price": price, "currency": currency,
            "in_stock": m.get("in_stock")}))
    return {"searches_used_this_month": serpapi_used(), "matches": matches}


# ---------------------------------------------------------------- product pages

class PageScan(HTMLParser):
    """Collect <meta> tags and JSON-LD blocks, which is where stores describe products."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.ld, self.title, self._in = {}, [], "", None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, []).append(a["content"].strip())
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in = "ld"
            self.ld.append("")
        elif tag == "title":
            self._in = "title"

    def handle_endtag(self, tag):
        if tag in ("script", "title"):
            self._in = None

    def handle_data(self, data):
        if self._in == "ld":
            self.ld[-1] += data
        elif self._in == "title":
            self.title += data


def ld_nodes(value):
    """Every JSON-LD object in a block, including ones nested in @graph or lists."""
    if isinstance(value, list):
        for v in value:
            yield from ld_nodes(v)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "hasVariant", "mainEntity"):
            if key in value:
                yield from ld_nodes(value[key])


def ld_types(node):
    t = node.get("@type", [])
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def image_urls(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [u for v in value for u in image_urls(v)]
    if isinstance(value, dict):
        return image_urls(value.get("contentUrl") or value.get("url") or [])
    return []


def first_offer(offers):
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return {}
    if "offers" in offers and "price" not in offers:  # AggregateOffer wrapping offers
        return first_offer(offers["offers"]) or offers
    return offers


def shopify_images(page_url, html):
    """Shopify stores list every product photo at <product url>.json; pages show only one."""
    path = urllib.parse.urlsplit(page_url).path.rstrip("/")
    if "/products/" not in path or "cdn/shop/" not in html and "cdn.shopify.com" not in html:
        return []
    try:
        raw, _ = fetch_text(urllib.parse.urljoin(page_url, path + ".json"), accept="application/json",
                            timeout=12)
        return [img["src"] for img in json.loads(raw)["product"]["images"] if img.get("src")]
    except (ToolError, ValueError, KeyError, TypeError):
        return []


def product_info(args):
    html, final_url = fetch_text(args["url"], timeout=12)
    scan = PageScan()
    scan.feed(html)

    info = {"url": final_url, "site": urllib.parse.urlsplit(final_url).hostname}
    images = []

    def put(key, value):  # the first real value found wins
        if value not in (None, "", []) and key not in info:
            info[key] = value

    for block in scan.ld:
        try:
            parsed = json.loads(block)
        except ValueError:
            continue
        for node in ld_nodes(parsed):
            if not ld_types(node) & {"product", "productgroup"}:
                continue
            brand = node.get("brand")
            put("name", node.get("name"))
            put("brand", brand.get("name") if isinstance(brand, dict) else brand)
            put("colour", node.get("color"))
            offer = first_offer(node.get("offers"))
            price = offer.get("price", offer.get("lowPrice"))
            if price not in (None, "") and "price" not in info:
                info["price"] = price
                put("currency", offer.get("priceCurrency"))
                stock = str(offer.get("availability", "")).lower()
                put("in_stock", True if "instock" in stock else False if "outofstock" in stock
                    else None)
            images += image_urls(node.get("image"))

    meta = scan.meta
    put("name", (meta.get("og:title") or [scan.title.strip()])[0])
    price = meta.get("product:price:amount") or meta.get("og:price:amount")
    if price and "price" not in info:
        info["price"] = price[0]
        put("currency", (meta.get("product:price:currency") or meta.get("og:price:currency")
                         or [None])[0])
    images += meta.get("og:image", []) + meta.get("og:image:secure_url", [])
    images = shopify_images(final_url, html) + images

    seen = set()  # the same photo often appears at several sizes or on two hosts

    def first_time(u):
        name = u.split("?")[0].rsplit("/", 1)[-1]
        return not (name in seen or seen.add(name))

    info["images"] = [urllib.parse.urljoin(final_url, u) for u in images if u and first_time(u)][:12]
    if "price" not in info and not info["images"]:
        raise ToolError("No product details found on that page. Try web_extract on it instead.")
    return info


# ---------------------------------------------------------------- saving photos

IMAGE_TYPES = [(b"\xff\xd8\xff", "jpg"), (b"\x89PNG", "png"), (b"GIF8", "gif")]
# A width under 1000px in the link's query string, e.g. ?w=375 or &wid=513
SMALL_WIDTH = re.compile(r"(?i)(^|&)(w|wid|width|imwidth)=(\d{1,3})(?=&|$)")


def image_type(data):
    for magic, ext in IMAGE_TYPES:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return None


def bigger_image_url(url):
    """Ask store CDNs for a larger copy of the same photo, if the link asks for a small one."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname == "i.pinimg.com":
        return parts._replace(path=re.sub(r"^/\d+x\d*/", "/736x/", parts.path)).geturl()
    # Edit the raw query so presets like ?$n_640w$ stay exactly as the CDN wrote them.
    return parts._replace(query=SMALL_WIDTH.sub(r"\1\2=1200", parts.query)).geturl()


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:50] or "photo"


def download_image(url):
    for candidate in dict.fromkeys([bigger_image_url(url), url]):
        try:
            # Ask for JPEG first: it's the format every phone and Vinted's photo upload accept.
            data, _, _ = fetch(candidate, accept="image/jpeg,image/png;q=0.9,image/*;q=0.8",
                               limit=MAX_IMAGE_BYTES, timeout=20)
        except ToolError as e:
            error = e
            continue
        ext = image_type(data)
        if ext:
            return data, ext
        error = ToolError("That link is not a photo (the site sent a web page instead).")
    raise error


def save_images(args):
    items = args["images"][:12]
    folder = os.path.join(IMAGE_DIR, datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)
    saved = []
    for item in items:
        url, name = item["url"], item.get("name") or "photo"
        try:
            data, ext = download_image(url)
        except ToolError as e:
            saved.append({"name": name, "url": url, "error": str(e)})
            continue
        digest = hashlib.sha1(data).hexdigest()[:8]
        path = os.path.join(folder, f"{slug(name)}-{digest}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        saved.append({"name": name, "path": path, "media": f"MEDIA:{path}",
                      "kb": len(data) // 1024, "url": url})
    return saved


# ---------------------------------------------------------------- style profile

def get_profile(args):
    profile = load_json("profile.json", {})
    looks = load_json("looks.json", [])
    rated = [{"look": l["id"], "title": l["title"], "verdict": l["rating"]["verdict"],
              "notes": l["rating"].get("notes", "")}
             for l in looks if l.get("rating")][-15:]
    return {"quiz_done": bool(profile.get("quiz_done")), "profile": profile,
            "looks_so_far": len(looks), "recent_feedback": rated,
            "paid_search": paid_search_status()}


def update_profile(args):
    changes = args["changes"]
    if not isinstance(changes, dict) or not changes:
        raise ToolError("Pass changes as an object, e.g. {\"sizes\": {\"shoes\": \"EU 43\"}}.")
    profile = load_json("profile.json", {})
    for key, value in changes.items():
        if value is None:
            profile.pop(key, None)
        else:
            profile[key] = value
    profile["updated"] = now()
    save_json("profile.json", profile)
    return {"ok": True, "profile": profile}


# ---------------------------------------------------------------- looks

def save_look(args):
    looks = load_json("looks.json", [])
    number = max((int(l["id"][1:]) for l in looks if l.get("id", "")[1:].isdigit()), default=0) + 1
    look = {"id": f"L{number}", "date": now(), "title": args["title"],
            "occasion": args.get("occasion", ""), "why": args.get("why", ""),
            "inspiration": args.get("inspiration", ""), "pieces": args.get("pieces", []),
            "total": args.get("total"), "currency": args.get("currency", "EUR")}
    looks.append(look)
    save_json("looks.json", looks[-300:])
    return {"saved": look["id"]}


def rate_look(args):
    verdict = args["verdict"].lower()
    if verdict not in VERDICTS:
        raise ToolError(f"verdict must be one of: {', '.join(VERDICTS)}")
    looks = load_json("looks.json", [])
    for look in looks:
        if look["id"].lower() == str(args["look_id"]).lower():
            look["rating"] = {"verdict": verdict, "notes": args.get("notes", ""), "date": now()}
            save_json("looks.json", looks)
            return {"ok": True, "look": look["id"], "verdict": verdict}
    raise ToolError(f"No look with id {args['look_id']}. Use recent_looks to find ids.")


def recent_looks(args):
    limit = max(1, min(int(args.get("limit", 10)), 50))
    looks = load_json("looks.json", [])
    if args.get("verdict"):
        looks = [l for l in looks if l.get("rating", {}).get("verdict") == args["verdict"]]
    return [{"id": l["id"], "date": l["date"][:10], "title": l["title"],
             "occasion": l.get("occasion", ""),
             "pieces": [{k: p.get(k) for k in ("item", "store", "price") if p.get(k)}
                        for p in l.get("pieces", [])],
             "rating": l.get("rating")}
            for l in reversed(looks[-limit:])]


# ---------------------------------------------------------------- tool table

def s(desc, **extra):
    return dict(type="string", description=desc, **extra)


def tool(fn, desc, props, required=(), read_only=False, open_world=True):
    return {
        "fn": fn,
        "spec": {
            "name": fn.__name__,
            "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": list(required)},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                            "openWorldHint": open_world},
        },
    }


PIECE = {"type": "object", "properties": {
    "item": s("What it is, e.g. 'black suede Chelsea boots'."), "store": s("Store name."),
    "url": s("Product page link."), "price": {"type": "number"},
    "image_path": s("Saved photo path from save_images.")}}

TOOLS = [
    tool(get_profile, "Get the user's style profile (quiz answers, sizes, budget, likes and dislikes), "
         "their recent feedback on looks, and whether paid search (SerpApi) is on and how much "
         "of this month's allowance is used. Call this before suggesting anything. "
         "quiz_done=false means the style quiz still needs doing.", {}, read_only=True,
         open_world=False),
    tool(update_profile, "Save or change style profile fields. Values replace what was there; "
         "null removes a field. Set quiz_done=true when the quiz is finished.",
         {"changes": {"type": "object", "description": "Fields to set, e.g. {\"section\": "
                      "\"menswear\", \"styles\": [\"smart casual\"], \"budget_per_piece\": 60}."}},
         ["changes"], open_world=False),
    tool(image_search, "Free image search (DuckDuckGo, Ireland region). Returns photo links with the "
         "page each came from. Use site= to search one store for product photos, or a "
         "Pinterest/street-style query for outfit inspiration. Paced to avoid bot checks; if it "
         "reports a pause, use web_search instead.",
         {"query": s("What to look for, e.g. 'black suede chelsea boots men'."),
          "site": s("Only results from this site, e.g. 'zalando.ie' or 'pinterest.com'."),
          "limit": {"type": "integer", "description": "1-25, default 8"}},
         ["query"], read_only=True),
    tool(shopping_search, "Find products for sale in Ireland with prices. With paid search on, uses "
         "Google Shopping (1 SerpApi search): title, store, price in euro, a small photo, a Google "
         "Shopping link and a product_token for shopping_stores. Without it, falls back to the free "
         "image search, which has no prices.",
         {"query": s("What to look for, e.g. 'brown suede loafers men'."),
          "max_price": {"type": "number", "description": "Highest price in euro."},
          "min_price": {"type": "number", "description": "Lowest price in euro."},
          "limit": {"type": "integer", "description": "1-20, default 8"}},
         ["query"], read_only=True),
    tool(shopping_stores, "Stores selling a Google Shopping product, with direct store links, prices "
         "and photos. Needs paid search; uses 1 SerpApi search. Use it only when you can't find the "
         "store's own page another way.",
         {"product_token": s("product_token from a shopping_search result.")},
         ["product_token"], read_only=True),
    tool(lens_search, "Google Lens: find products that look like a photo, with full-size photos and "
         "product pages (prices only sometimes). Needs paid search; uses 1 SerpApi search. Pass a "
         "query like 'boots' to focus on one piece of an outfit photo.",
         {"image_url": s("The photo's web link, e.g. an image link from image_search."),
          "image_path": s("Or a saved photo in Hermes's cache, e.g. one the user sent."),
          "query": s("Which piece to match, e.g. 'brown loafers'."),
          "limit": {"type": "integer", "description": "1-30, default 10"}},
         read_only=True),
    tool(product_info, "Read a product page: name, brand, price, currency, stock and photo links. "
         "Many big stores block this; if it fails, use web_extract on the page.",
         {"url": s("Product page URL.")}, ["url"], read_only=True),
    tool(save_images, "Download photos into Hermes's image cache. Each result has a 'media' tag "
         "(MEDIA:/path); put that tag on its own line in your reply to post the photo in Discord. "
         "Asks store CDNs for a larger copy when the link is a small one.",
         {"images": {"type": "array", "description": "Up to 12 photos.", "items": {
             "type": "object", "properties": {"url": s("Photo link."),
                                              "name": s("Short label, e.g. 'look1-boots'.")},
             "required": ["url"]}}},
         ["images"]),
    tool(save_look, "Remember a look you suggested, so it isn't repeated and can be rated. "
         "Returns its id (e.g. L12); mention the id in your reply.",
         {"title": s("Short name, e.g. 'Dublin pub night'."), "occasion": s("What it's for."),
          "why": s("One line on why it suits the user."),
          "inspiration": s("Saved path or link of the inspiration photo."),
          "pieces": {"type": "array", "items": PIECE},
          "total": {"type": "number", "description": "Total price of the pieces."},
          "currency": s("Currency code, default EUR.")},
         ["title"], open_world=False),
    tool(rate_look, "Record what the user thought of a look.",
         {"look_id": s("Look id, e.g. L12."), "verdict": s("Their verdict.", enum=VERDICTS),
          "notes": s("What they liked or didn't, in their words.")},
         ["look_id", "verdict"], open_world=False),
    tool(recent_looks, "List recent looks you suggested, newest first, with any ratings.",
         {"limit": {"type": "integer", "description": "1-50, default 10"},
          "verdict": s("Only looks with this verdict.", enum=VERDICTS)},
         read_only=True, open_world=False),
]


# ---------------------------------------------------------------- MCP stdio

def respond(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(msg, tools):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if msg_id is None:
        return  # a notification, e.g. notifications/initialized
    if method == "initialize":
        respond(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": [t["spec"] for t in tools.values()]})
    elif method == "tools/call":
        t = tools.get(params.get("name"))
        if not t:
            respond(msg_id, error={"code": -32602, "message": f"Unknown tool: {params.get('name')}"})
            return
        try:
            result = t["fn"](params.get("arguments") or {})
            text, is_error = json.dumps(result, ensure_ascii=False, indent=1), False
        except ToolError as e:
            text, is_error = str(e), True
        except (KeyError, ValueError, TypeError) as e:
            text, is_error = f"Bad or missing argument: {e}", True
        respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    else:
        respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main():
    tools = {t["spec"]["name"]: t for t in TOOLS}
    prune_old_images()
    log(f"started, {len(tools)} tools, data in {DATA_DIR}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            respond(None, error={"code": -32700, "message": "Parse error"})
            continue
        try:
            handle(msg, tools)
        except Exception as e:  # keep the server alive whatever happens
            log(f"error handling {msg.get('method')}: {e!r}")
            if msg.get("id") is not None:
                respond(msg["id"], error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    main()
