#!/usr/bin/env python3
"""Outfit-finding tools for Hermes, as a tiny MCP server.

Standard library only: no third-party packages. Speaks MCP over stdio
(newline-delimited JSON-RPC).

Hermes does the styling and its own web_search / web_extract. This server
adds what Hermes can't do on its own:
  - a free image search (DuckDuckGo), to find store photos of a piece
  - reading a product page's name, price and photos, where the store allows it
  - saving photos into Hermes's image cache, so they can be posted in Discord
  - remembering the style profile, past looks and what was liked

Data lives in $HERMES_HOME/fashion/, photos in $HERMES_HOME/cache/images/fashion/.
"""

import hashlib
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
            "looks_so_far": len(looks), "recent_feedback": rated}


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
    tool(get_profile, "Get the user's style profile (quiz answers, sizes, budget, likes and dislikes) "
         "and their recent feedback on looks. Call this before suggesting anything. "
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
