"""Clean baseline monitor.

One run, one direct request path, all configured shops, sequential execution.
No fallback proxies, relay fetches, retry workers, blocked-shop workers, or
parallel request pools. This is intentionally boring so failures are measurable.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

import product_catalog
from retailer_catalog import CORE_SHOPS, ALL_PROSPECTS

STATUS_FILE = Path("status.json")
HEALTH_FILE = Path("monitor_health.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip().rstrip("/")
if NTFY_TOPIC.startswith("https://ntfy.sh/"):
    NTFY_TOPIC = NTFY_TOPIC.removeprefix("https://ntfy.sh/").strip("/")
elif NTFY_TOPIC.startswith("http://ntfy.sh/"):
    NTFY_TOPIC = NTFY_TOPIC.removeprefix("http://ntfy.sh/").strip("/")
elif NTFY_TOPIC.startswith("ntfy.sh/"):
    NTFY_TOPIC = NTFY_TOPIC.removeprefix("ntfy.sh/").strip("/")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.7",
}
TIMEOUT = (5, 15)
# Nur für wiederholt langsame Shops längere Lese-Timeouts; keine pauschale
# Verlängerung, damit ein einzelner Händler den gesamten Lauf nicht ausbremst.
PRODUCT_TIMEOUTS = {
    "lotticards": (5, 8),
    "goblinsgnomes": (5, 20),
}
# Begrenzte Parallelität auf Shop- und Produktebene.
MAX_SHOP_WORKERS = 8
MAX_PRODUCT_WORKERS = 4
DISCOVERY_TIMEOUTS = {
    "comicattack": (3, 7),
    "bulkparadise": (4, 8),
    "gamesisland": (3, 5),
    "lotticards": (5, 8),
    "goblinsgnomes": (5, 20),
}
DETECTION_VERSION = 5

DISCOVERY_OVERRIDES = {
    "imperialcards": "/collections/all",
    "sealedcardzz": "/collections/pokemon-elite-trainer-boxen",
    "kingbreaktcg": "/collections/pokemon-displays-eng",
    "playingcards": "/pokemon.html",
    "nettoonline": "/pokemon-tcg-30th-anniversary-celebration-elite-trainer-box-englisch/p-3203254000",
    "kaufland": "/product/573179383/",
    "poketcgcenter": "/shop/pokemon-tcg/pokemon-tcg-30th-celebration-ultra-premium-collection-day-night/",
    # Pokémon-Produktarchiv statt der Startseite oder einer zu engen 30th-Suche.
    "bulkparadise": "/product-tag/pokemon/",
}

INACTIVE_SHOP_IDS = {"cherrytcg", "evocards", "fantasywelt", "lootcave", "spielwarenkroemer"}


def _all_shops():
    shops = {}
    for source in (CORE_SHOPS, ALL_PROSPECTS):
        for raw in source:
            shop = dict(raw)
            if shop.get("id") in INACTIVE_SHOP_IDS:
                continue
            if shop["id"] in DISCOVERY_OVERRIDES:
                shop["discovery"] = DISCOVERY_OVERRIDES[shop["id"]]
            shop.pop("fallback_discoveries", None)
            shop.pop("fallback_product_urls", None)
            shop.pop("reader_fallback", None)
            shop.pop("max_workers", None)
            # Prospect paths bleiben als reguläre Alternativen für HTTP-404 erhalten.
            shops[shop["id"]] = shop
    return list(shops.values())


SHOPS = _all_shops()


def load_status():
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"products": {}, "shops": {}}
    except Exception:
        return {"products": {}, "shops": {}}


def save_status(status):
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def fetch(url, timeout=TIMEOUT):
    started = time.monotonic()
    response = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    response.raise_for_status()
    if not response.text.strip():
        raise RuntimeError("empty response")
    return response, elapsed_ms


def _clean_url(url):
    return str(url or "").split("#", 1)[0].split("?", 1)[0]


def _looks_30th(text):
    text = str(text or "").lower()
    return bool(re.search(r"\b30th\b|30th[- ](?:celebration|anniversary)|30\.?\s*(?:jahre|years)", text))


def _looks_target(text):
    text = str(text or "").lower()
    return bool(re.search(r"ultra[- ]?premium|\bupc\b|elite trainer box|\betb\b|top[- ]trainer[- ]box|pokemon center.*(?:etb|elite trainer)|\bbooster[ -]?bundle\b", text))


def _product_heading(soup):
    headings = soup.find_all("h1")
    for heading in headings:
        text = heading.get_text(" ", strip=True)
        if _looks_30th(text) and _looks_target(text):
            return heading
    return headings[0] if headings else None


def _page_title(soup):
    heading = _product_heading(soup)
    if heading:
        text = heading.get_text(" ", strip=True)
        if _looks_30th(text) and _looks_target(text):
            return text
    # Some themes have no h1 and truncate og:title to ~70 characters.
    names = [obj.get("name", "") for root in _json_ld_objects(soup)
             for obj in _walk_json(root) if obj.get("@type") == "Product"]
    if len(names) == 1 and names[0]:
        return str(names[0]).strip()
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        return meta["content"].strip()
    if heading:
        return heading.get_text(" ", strip=True)
    title = soup.find("title")
    return title.get_text(" ", strip=True) if title else ""


def _anchor_context(a, absolute):
    parts = [" ".join(a.stripped_strings), a.get("title", ""), a.get("aria-label", "")]
    for img in a.find_all("img"):
        parts.append(img.get("alt", ""))
    parts.append(absolute)
    return " ".join(str(part or "") for part in parts).strip()


def discover(shop):
    url = urljoin(shop["base"], shop["discovery"])
    response = None
    elapsed_ms = 0
    tried_urls = []
    # Ein 404 auf einer Kategorie kann durch einen veralteten Kategoriepfad
    # entstehen. Dann genau eine normale, im Katalog hinterlegte alternative
    # Such-/Kategorie-URL testen. 403/400 werden NICHT umgangen oder wiederholt.
    candidates = [url]
    for candidate in shop.get("prospect_discoveries", ()):
        path = candidate.get("path") if isinstance(candidate, dict) else candidate
        if path:
            alternative = urljoin(shop["base"], path)
            if alternative not in candidates:
                candidates.append(alternative)
    for candidate_url in candidates:
        tried_urls.append(candidate_url)
        try:
            response, elapsed_ms = fetch(
                candidate_url,
                timeout=DISCOVERY_TIMEOUTS.get(shop["id"], TIMEOUT),
            )
            url = candidate_url
            break
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            if candidate_url == candidates[-1]:
                raise
            print(f"[{shop['name']}] DISCOVERY 404 | teste reguläre Alternativ-URL", flush=True)
    if response is None:
        raise RuntimeError("discovery failed without a response")
    soup = BeautifulSoup(response.text, "html.parser")
    found = {}
    base_host = urlparse(shop["base"]).netloc.lower().removeprefix("www.")

    current_title = _page_title(soup)
    current_context = f"{current_title} {response.url}"
    if _looks_30th(current_context):
        found[_clean_url(response.url)] = current_title

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        absolute = urljoin(response.url, href)
        host = urlparse(absolute).netloc.lower().removeprefix("www.")
        if host != base_host:
            continue
        context = _anchor_context(a, absolute)
        if not _looks_30th(context):
            continue
        clean = _clean_url(absolute)
        title = " ".join(a.stripped_strings).strip() or a.get("aria-label", "") or a.get("title", "")
        found.setdefault(clean, title)

    # Explicit, verified product paths supplement JS-rendered category listings.
    # This is still the same direct shop request path, without fallback/worker.
    for seed in shop.get("product_urls", ()):
        absolute = urljoin(shop["base"], seed)
        if urlparse(absolute).netloc.lower().removeprefix("www.") == base_host:
            found.setdefault(_clean_url(absolute), "")

    return found, response.status_code, elapsed_ms, response.url


def _json_ld_objects(soup):
    objects = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            objects.append(json.loads(raw))
        except Exception:
            continue
    return objects


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _main_product_json(soup):
    candidates = []
    for root in _json_ld_objects(soup):
        for obj in _walk_json(root):
            types = obj.get("@type", [])
            types = types if isinstance(types, list) else [types]
            if "Product" in types:
                candidates.append(obj)
    if len(candidates) <= 1:
        return candidates
    title = product_catalog.compact_text(_page_title(soup))
    canonical = soup.find("link", rel="canonical")
    url = _clean_url(canonical.get("href", "")) if canonical else ""
    matching = [obj for obj in candidates if
                product_catalog.compact_text(obj.get("name")) == title or
                (url and _clean_url(obj.get("url")) == url)]
    return matching[:1]


def _schema_availability(soup):
    mapping = {
        "instock": "IN_STOCK",
        "limitedavailability": "IN_STOCK",
        "onlineonly": "IN_STOCK",
        "preorder": "PREORDER",
        "presale": "PREORDER",
        "backorder": "PREORDER",
        "outofstock": "OUT_OF_STOCK",
        "soldout": "OUT_OF_STOCK",
        "discontinued": "OUT_OF_STOCK",
    }
    fallback = ""
    for root in _main_product_json(soup):
        for obj in _walk_json(root):
            availability = obj.get("availability")
            if not availability:
                continue
            key = re.sub(r"[^a-z]", "", str(availability).rsplit("/", 1)[-1].lower())
            state = mapping.get(key, "")
            if not state:
                continue
            raw_type = obj.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if any(str(t or "").lower() == "product" or "offer" in str(t or "").lower() for t in types):
                return state
            fallback = fallback or state
    return fallback


def _primary_product_scope(soup):
    # Prefer the actual buy box; title-only wrappers and page navigation are
    # not evidence for a product's availability.
    for selector in (
        "[id^='ProductInfo-']", "[id^='ProductInformation-']",
        ".product-info__sticky", ".product__info-container",
        ".product-detail-buy", ".product-details", ".hvc-summary-card",
        ".bls__product-details-infor", "form.product-details-buy-form",
        "product-info[id^='MainProduct']",
    ):
        node = soup.select_one(selector)
        if node and node.get_text(" ", strip=True):
            return node
    heading = _product_heading(soup)
    if heading:
        for parent in list(heading.parents)[:10]:
            if parent.name in {"body", "html", "main"}:
                break
            if len(parent.get_text(" ", strip=True)) < len(heading.get_text(" ", strip=True)) + 50:
                continue
            if parent.find(["button", "input"]) or parent.name in {"article", "section"}:
                return parent
    return None


def _primary_product_context(soup, title):
    scope = _primary_product_scope(soup)
    parts = [str(title or "")]
    if scope is not None:
        # Exclude suggestions and FAQs even when nested in the main container.
        copy = BeautifulSoup(str(scope), "html.parser")
        for node in copy.select("script, style, nav, product-recommendations, .related, .related-products, .product-recommendations, .opc-Accordion, [itemtype*='FAQPage']"):
            node.decompose()
        parts.append(copy.get_text(" ", strip=True)[:14000])
    for product in _main_product_json(soup):
        description = product.get("description")
        if isinstance(description, str):
            parts.append(BeautifulSoup(description, "html.parser").get_text(" ", strip=True)[:10000])
    return " ".join(parts)


def _purchase_controls(soup):
    scope = _primary_product_scope(soup)
    if scope is None:
        return []
    controls = []
    for node in scope.find_all(["button", "input"]):
        if node.get("type") == "hidden":
            continue
        text = node.get("value", "") if node.name == "input" else node.get_text(" ", strip=True)
        attrs = " ".join([text, str(node.get("name", "")), str(node.get("class", "")), str(node.get("id", ""))]).lower()
        if re.search(r"add.to.cart|add.to.basket|warenkorb|winkelwagen|pre.?order|vorbestell|single_add_to_cart|productsubmitbutton|buy.button|\bkaufen\b|\bkopen\b", attrs) or node.get("name") == "add":
            disabled = node.has_attr("disabled") or node.get("aria-disabled") == "true"
            controls.append((text.lower(), disabled))
    return controls


def _strong_coming_soon(text):
    t = str(text or "").lower()
    return any(marker in t for marker in (
        "demnächst vorbestellbar", "demnaechst vorbestellbar",
        "vorbestellung öffnet in kürze", "vorbestellung offnet in kurze",
        "vorbestellung startet bald", "verkauf startet bald",
        "pre-order starts soon", "preorder starts soon", "pre order starts soon",
    ))


def _availability(soup, title, page_text):
    primary = _primary_product_context(soup, title).lower()
    controls = _purchase_controls(soup)
    enabled_buy = any(not disabled for _, disabled in controls)
    disabled_buy = bool(controls) and all(disabled for _, disabled in controls)
    schema_state = _schema_availability(soup)
    # A preorder title never overrides an unavailable offer.
    if schema_state == "OUT_OF_STOCK" or disabled_buy:
        return "OUT_OF_STOCK"
    if _strong_coming_soon(primary) and not enabled_buy:
        return "COMING_SOON"
    out = (
        "sold out", "out of stock", "ausverkauft", "nicht verfügbar",
        "nicht verfugbar", "niet op voorraad", "épuisé", "epuise",
        "laat het me weten wanneer het beschikbaar is", "benachrichtigen wenn verfügbar",
    )
    if any(marker in primary for marker in out) and not enabled_buy and schema_state not in {"IN_STOCK", "PREORDER"}:
        return "OUT_OF_STOCK"
    preorder = bool(re.search(r"pre[- ]?order|vorbestell|vorverkauf|preventa|précommande", primary))
    if schema_state in {"IN_STOCK", "PREORDER"}:
        return "PREORDER" if schema_state == "PREORDER" or preorder else "IN_STOCK"
    if enabled_buy:
        return "PREORDER" if preorder else "IN_STOCK"
    if any(marker in primary for marker in ("coming soon", "available soon", "bald erhältlich", "demnächst")):
        return "COMING_SOON"
    if re.search(r"\b(?:auf lager|in stock|direct leverbaar)\b", primary):
        return "IN_STOCK"
    return "UNKNOWN"


def _normalize_price(value):
    text = str(value or "").strip().replace("\u00a0", " ").replace("’", "'")
    if not text:
        return ""
    match = re.search(r"(?<!\d)(\d{1,6}(?:[ .,'’]\d{3})*(?:[.,]\d{1,2})?|\d{1,6}[.,]\d{1,2})(?!\d)", text)
    if not match:
        return ""
    raw = match.group(1).replace(" ", "").replace("'", "").replace("’", "")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        left, right = raw.rsplit(",", 1)
        raw = left.replace(",", "") + ("." + right if len(right) <= 2 else right)
    elif "." in raw:
        parts = raw.split(".")
        if len(parts) > 2:
            raw = "".join(parts[:-1]) + ("." + parts[-1] if len(parts[-1]) <= 2 else parts[-1])
        elif len(parts) == 2 and len(parts[1]) == 3:
            raw = "".join(parts)
    try:
        number = float(raw)
    except ValueError:
        return ""
    if number <= 0 or number > 100000:
        return ""
    return f"{number:.2f}"


def _normalize_currency(value):
    text = str(value or "").strip().upper()
    aliases = {
        "€": "EUR", "EUR": "EUR", "CHF": "CHF", "FR.": "CHF", "SFR": "CHF",
        "PLN": "PLN", "ZŁ": "PLN", "ZL": "PLN", "GBP": "GBP", "£": "GBP",
        "USD": "USD", "US$": "USD", "CAD": "CAD", "C$": "CAD",
        "DKK": "DKK", "SEK": "SEK", "NOK": "NOK",
    }
    return aliases.get(text, text if re.fullmatch(r"[A-Z]{3}", text) else "")


def _offer_data_from_json_ld(value):
    if isinstance(value, list):
        for item in value:
            data = _offer_data_from_json_ld(item)
            if data[0]:
                return data
        return "", ""
    if not isinstance(value, dict):
        return "", ""

    offers = value.get("offers")
    if isinstance(offers, (dict, list)):
        data = _offer_data_from_json_ld(offers)
        if data[0]:
            return data

    for key in ("price", "lowPrice"):
        if value.get(key) is not None:
            price = _normalize_price(value.get(key))
            if price:
                return price, _normalize_currency(value.get("priceCurrency"))

    for key in ("@graph", "mainEntity", "itemListElement"):
        if key in value:
            data = _offer_data_from_json_ld(value[key])
            if data[0]:
                return data
    return "", ""


def _is_unpriced_coming_soon(text):
    text = str(text or "").lower()
    return _strong_coming_soon(text) or any(marker in text for marker in (
        "pre-order soon", "preorder soon", "pre order soon",
        "available soon", "coming soon", "bald erhältlich", "bald erhaltlich",
        "demnächst", "demnaechst",
    ))


def _price_is_pending(text):
    t = str(text or "").lower()
    return any(marker in t for marker in (
        "preis folgt", "price follows", "price to follow", "prijs volgt",
        "price tba", "preis tba", "price: tba", "preis: tba",
    ))


def _currency_from_soup(soup, text=""):
    for attrs in (
        {"property": "product:price:currency"},
        {"property": "og:price:currency"},
        {"itemprop": "priceCurrency"},
    ):
        node = soup.find(attrs=attrs)
        if node:
            currency = _normalize_currency(node.get("content") or node.get("value") or node.get_text(" ", strip=True))
            if currency:
                return currency

    haystack = str(text or "")
    explicit = re.search(r"\b(EUR|CHF|PLN|GBP|USD|CAD|DKK|SEK|NOK)\b", haystack, re.I)
    if explicit:
        return _normalize_currency(explicit.group(1))
    if "€" in haystack:
        return "EUR"
    if "zł" in haystack.lower():
        return "PLN"
    if "£" in haystack:
        return "GBP"
    return ""


def _extract_price(soup, html, page_text, primary_text):
    currency = _currency_from_soup(soup, primary_text)
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
    ):
        node = soup.find(attrs=attrs)
        if node:
            value = node.get("content") or node.get("value") or node.get_text(" ", strip=True)
            price = _normalize_price(value)
            if price:
                return price, currency

    for obj in _main_product_json(soup):
        price, structured_currency = _offer_data_from_json_ld(obj)
        if price:
            return price, structured_currency or currency

    if _is_unpriced_coming_soon(primary_text) or _price_is_pending(primary_text):
        return "", currency

    scope = _primary_product_scope(soup)
    if scope is not None:
        node = scope.find(attrs={"itemprop": "price"})
        if node:
            price = _normalize_price(node.get("content") or node.get("value") or node.get_text(" ", strip=True))
            if price:
                return price, currency

    price_pattern = re.compile(
        r"(?:(€|EUR|CHF|PLN|zł|£|GBP)\s*([0-9][0-9 .,'’]*[0-9])|"
        r"([0-9][0-9 .,'’]*[0-9])\s*(€|EUR|CHF|PLN|zł|£|GBP))",
        re.I,
    )
    match = price_pattern.search(primary_text)
    if match:
        symbol = match.group(1) or match.group(4)
        price = _normalize_price(match.group(2) or match.group(3))
        if price:
            return price, _normalize_currency(symbol) or currency

    for node in (scope.find_all(attrs={"class": re.compile(r"price|preis|amount", re.I)}, limit=30) if scope is not None else []):
        text = node.get_text(" ", strip=True)
        match = price_pattern.search(text)
        if match:
            symbol = match.group(1) or match.group(4)
            price = _normalize_price(match.group(2) or match.group(3))
            if price:
                return price, _normalize_currency(symbol) or currency

    return "", currency


def _detect_case_quantity(title, primary_text, page_text):
    title_text = str(title or "").lower()
    if not re.search(r"\b(?:sealed\s+)?case\b|\bkarton\b|\bmaster case\b", title_text, re.I):
        return 0

    for pattern in (
        r"\b(\d{1,3})\s*(?:stück|stuck|stueck|pcs?|units?)\b",
        r"\b(\d{1,3})\s*x\s*(?:etbs?|elite trainer boxes?|upcs?|ultra[- ]premium collections?)\b",
        r"\b(\d{1,3})\s*(?:etbs?|elite trainer boxes?|upcs?|ultra[- ]premium collections?)\b",
    ):
        match = re.search(pattern, title_text, re.I)
        if match:
            return max(1, int(match.group(1)))

    detail = str(primary_text or "").lower()
    for pattern in (
        r"\b(\d{1,3})\s*x\s*(?:etbs?|elite trainer boxes?|upcs?|ultra[- ]premium collections?)\b",
        r"\b(\d{1,3})\s*(?:etbs?|elite trainer boxes?|upcs?|ultra[- ]premium collections?)\b",
    ):
        match = re.search(pattern, detail, re.I)
        if match:
            return max(1, int(match.group(1)))
    return 0


def _expand_offer_variants(soup, product, primary_text):
    """Read separately priced variants from the same public page, without extra requests."""
    objects = _main_product_json(soup)
    offers = objects[0].get("offers") if objects else None
    if not isinstance(offers, list) or len(offers) < 2:
        return product
    labels = {}
    for option in soup.select('select[name="id"] option[value]'):
        labels[str(option.get("value"))] = option.get_text(" ", strip=True)
    for script in soup.select('script[type="application/json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("variants"), list):
            for variant in data["variants"]:
                if isinstance(variant, dict) and variant.get("id") and variant.get("title"):
                    labels[str(variant["id"])] = str(variant["title"])
    variants = []
    preorder = bool(re.search(r"pre[- ]?order|vorbestell|vorverkauf", primary_text, re.I))
    for index, offer in enumerate(offers):
        if not isinstance(offer, dict):
            continue
        variant_id = parse_qs(urlparse(str(offer.get("url", ""))).query).get("variant", [""])[0]
        if not variant_id or not variant_id.isdigit():
            continue
        label = labels.get(variant_id, "") or str(offer.get("name") or "")
        for attribute in offer.get("additionalProperty", []):
            if isinstance(attribute, dict) and attribute.get("name") in {"size", "Size", "variant", "Version"}:
                label = str(attribute.get("value") or label)
        if not label:
            continue
        title = str(product["title"]) + " · " + label
        if not product_catalog.candidate_allowed(title, product["url"], primary_text):
            continue
        item = dict(product, title=title, variant_id=variant_id, variant_label=label)
        item.pop("pack_quantity", None)
        if index:
            item["url"] = _clean_url(product["url"]) + "?variant=" + variant_id
        state = str(offer.get("availability", "")).rsplit("/", 1)[-1].lower()
        item["state"] = {"instock": "PREORDER" if preorder else "IN_STOCK",
                         "preorder": "PREORDER", "backorder": "PREORDER",
                         "outofstock": "OUT_OF_STOCK", "soldout": "OUT_OF_STOCK"}.get(state, "UNKNOWN")
        item["available"] = item["state"] in {"IN_STOCK", "PREORDER"}
        item["price"] = _normalize_price(offer.get("price"))
        item["currency"] = _normalize_currency(offer.get("priceCurrency"))
        item = product_catalog.enrich_product(item)
        if item:
            variants.append(item)
    if variants and variants[0]["url"] == product["url"]:
        first = variants[0]
        if len(variants) > 1:
            first["_variants"] = variants[1:]
        return first
    return product


def _seller_allowed(shop, soup):
    """Keep bol.com first-party offers; reject third-party and unverified ones."""
    required = str(shop.get("require_seller") or "").strip().lower()
    if not required:
        return True

    names = []
    for product in _main_product_json(soup):
        offers = product.get("offers", ())
        for obj in _walk_json(offers):
            seller = obj.get("seller")
            if isinstance(seller, dict):
                seller = seller.get("name", "")
            if seller:
                names.append(str(seller).strip().lower())
    if names:
        return all(name in {"bol", "bol.com", "bol b.v."} for name in names) if required == "bol" else all(name == required for name in names)

    # Only trust the primary buy box, never a third-party recommendation.
    scope = _primary_product_scope(soup)
    if scope is None:
        return False
    seller_text = scope.get_text(" ", strip=True).lower()
    if required == "bol":
        return bool(re.search(r"\bverkoop door\s+(?:verkoop door\s+)?bol(?:\.com)?\b", seller_text))
    return False


def inspect_product(shop, url, link_title):
    try:
        response, elapsed_ms = fetch(url, timeout=PRODUCT_TIMEOUTS.get(shop["id"], TIMEOUT))
    except requests.HTTPError as exc:
        # OTTO product URLs found in listings sometimes reject the trailing-slash
        # variant with HTTP 400. Try the same public canonical path without the
        # trailing slash once; this is URL normalization, not an access-control
        # workaround. Never retry 403/429 or other access/rate-limit responses.
        if (shop.get("id") == "otto" and exc.response is not None
                and exc.response.status_code == 400 and urlparse(url).path.endswith("/")):
            parsed = urlparse(url)
            normalized_url = parsed._replace(path=parsed.path.rstrip("/")).geturl()
            response, retry_ms = fetch(
                normalized_url, timeout=PRODUCT_TIMEOUTS.get(shop["id"], TIMEOUT)
            )
            url = normalized_url
            elapsed_ms = retry_ms
        else:
            raise
    soup = BeautifulSoup(response.text, "html.parser")
    title = _page_title(soup) or link_title
    page_text = " ".join(soup.stripped_strings)
    if not _looks_30th(f"{title} {url}") and not product_catalog._has_30th_marker(title, url, page_text):
        return None, elapsed_ms
    primary_text = _primary_product_context(soup, title)
    if not product_catalog.candidate_allowed(title, url=url, page_text=primary_text):
        return None, elapsed_ms
    if not _seller_allowed(shop, soup):
        return None, elapsed_ms
    price, currency = _extract_price(soup, response.text, page_text, primary_text)
    currency = currency or shop.get("currency", "")
    if not price and shop.get("currency") in {"DKK", "SEK", "NOK"}:
        # Nordic price displays often use "799,-" or "999 kr" without a symbol.
        match = re.search(r"\b(\d{2,5}(?:[ .]\d{3})*)\s*(?:kr\b|,-|:-)", primary_text, re.I)
        if match:
            price = _normalize_price(match.group(1))
    state = _availability(soup, title, page_text)
    case_quantity = _detect_case_quantity(title, primary_text, page_text)
    product = {
        "shop_id": shop["id"],
        "shop": shop["name"],
        "title": title or link_title or url,
        "url": _clean_url(url),
        "state": state,
        "available": state in {"IN_STOCK", "PREORDER"},
        "price": price,
        "currency": currency,
        "price_pending": bool(not price and (_price_is_pending(primary_text) or
                                             (state == "COMING_SOON" and _strong_coming_soon(primary_text)))),
        "last_checked": int(time.time()),
    }
    enriched = product_catalog.enrich_product(product)
    if enriched and case_quantity > 0 and enriched.get("packaging") == "CASE":
        enriched["pack_quantity"] = case_quantity
    if enriched:
        enriched = _expand_offer_variants(soup, enriched, primary_text)
    return enriched, elapsed_ms


def send_ntfy(title, message, click_url=None):
    if not NTFY_TOPIC:
        return
    headers = {"Title": title, "Priority": "urgent"}
    if click_url:
        headers["Click"] = click_url
    requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), headers=headers, timeout=10).raise_for_status()


def _comparable_price(value):
    normalized = _normalize_price(value)
    return normalized or str(value or "").strip()


def _price_label(product, price):
    currency = _normalize_currency(product.get("currency")) or ""
    return f"{price} {currency}".strip()


def notify_changes(old_products, current_products):
    if not NTFY_TOPIC or not isinstance(current_products, dict):
        return

    allow_new_listing_alerts = bool(old_products)
    sent = 0

    for url, product in current_products.items():
        if not isinstance(product, dict):
            continue
        old = old_products.get(url) if isinstance(old_products, dict) else None
        changes = []
        new_price = _comparable_price(product.get("price"))
        new_state = str(product.get("state") or "UNKNOWN")

        if not isinstance(old, dict):
            if not allow_new_listing_alerts:
                continue
            changes.append("Neues Listing")
        else:
            old_price = _comparable_price(old.get("price"))
            old_state = str(old.get("state") or "UNKNOWN")

            if new_price and new_price != old_price:
                if old_price:
                    changes.append(f"Preis: {_price_label(old, old_price)} -> {_price_label(product, new_price)}")
                else:
                    changes.append(f"Preis entdeckt: {_price_label(product, new_price)}")

            if new_state in {"IN_STOCK", "PREORDER"} and new_state != old_state:
                label = "Jetzt verfügbar" if new_state == "IN_STOCK" else "Jetzt vorbestellbar"
                changes.append(label)

        if not changes:
            continue

        shop = str(product.get("shop") or product.get("shop_id") or "Shop")
        product_title = str(product.get("title") or "Pokémon 30th Produkt")
        details = " | ".join(changes)
        message = f"{shop}\n{product_title}\n{details}"
        if new_price and not any(change.startswith("Preis") for change in changes):
            message += f"\nPreis: {_price_label(product, new_price)}"

        try:
            send_ntfy("Pokemon 30th Monitor", message, click_url=product.get("url") or url)
            sent += 1
        except Exception as exc:
            print(f"NTFY FEHLER | {shop} | {exc}", flush=True)

    if sent:
        print(f"NTFY: {sent} Benachrichtigung(en) gesendet", flush=True)


def process_shop(shop, old_shop, known_urls):
    started = time.monotonic()
    detail_ms = 0
    try:
        links, http_status, discovery_ms, final_url = discover(shop)
        for url in known_urls:
            if urlparse(url).netloc.lower().removeprefix("www.") == urlparse(shop["base"]).netloc.lower().removeprefix("www."):
                links.setdefault(_clean_url(url), "")

        products = {}
        product_errors = []
        failed_product_urls = []
        slow_product_requests = []

        def check_link(item):
            url, link_title = item
            # Klar ausgeschlossene Treffer benötigen keinen Detailabruf.
            if (link_title and _looks_30th(link_title) and _looks_target(link_title)
                    and not product_catalog.candidate_allowed(link_title, url=url)):
                return url, None, 0, None
            request_started = time.monotonic()
            try:
                product, elapsed = inspect_product(shop, url, link_title)
                return url, product, elapsed, None
            except Exception as exc:
                # Auch fehlgeschlagene Requests zeitlich erfassen; bisher gingen
                # deren Laufzeiten in detail_ms verloren.
                elapsed = int((time.monotonic() - request_started) * 1000)
                return url, None, elapsed, exc

        # Alle gefundenen 30th-Kandidaten parallel prüfen. Die begrenzte
        # Workerzahl verhindert unkontrolliert viele Requests an einen Händler.
        with ThreadPoolExecutor(max_workers=min(MAX_PRODUCT_WORKERS, max(1, len(links)))) as pool:
            futures = [pool.submit(check_link, item) for item in links.items()]
            for future in as_completed(futures):
                url, product, elapsed, error = future.result()
                detail_ms += elapsed
                if elapsed >= 3000:
                    slow_product_requests.append({
                        "url": url, "duration_ms": elapsed,
                        "error": str(error) if error is not None else "",
                    })
                if error is not None:
                    product_errors.append(f"{url}: {error}")
                    failed_product_urls.append(url)
                    continue
                if product:
                    variants = product.pop("_variants", [])
                    products[product["url"]] = product
                    products.update({item["url"]: item for item in variants})
        elapsed_ms = int((time.monotonic() - started) * 1000)
        status = {
            "name": shop["name"], "ok": not product_errors,
            "failures": int((old_shop or {}).get("failures", 0)) + 1 if product_errors else 0,
            "product_count": len(products),
            "http_status": http_status, "discovery_url": final_url,
            "discovery_ms": discovery_ms, "detail_ms": detail_ms,
            "duration_ms": elapsed_ms, "product_errors": product_errors,
            "failed_product_urls": failed_product_urls,
            "slow_product_requests": sorted(slow_product_requests, key=lambda item: item["duration_ms"], reverse=True)[:10],
            "discovery_ok": True,
        }
        if product_errors:
            status["last_error"] = f"{len(product_errors)}/{len(links)} Produktseiten fehlgeschlagen: " + "; ".join(product_errors)
            status["last_failure"] = int(time.time())
            if (old_shop or {}).get("last_success"):
                status["last_success"] = old_shop["last_success"]
        else:
            status["last_success"] = int(time.time())
        label = "FEHLER" if product_errors else "OK"
        print(f"[{shop['name']}] {label} HTTP {http_status} | links={len(links)} products={len(products)} errors={len(product_errors)} | {elapsed_ms/1000:.2f}s", flush=True)
        for slow in status["slow_product_requests"]:
            print(f"[{shop['name']}] LANGSAME PRODUKTSEITE | {slow['duration_ms']/1000:.2f}s | {slow['url']}", flush=True)
        for error in product_errors:
            print(f"[{shop['name']}] PRODUKTFEHLER | {error}", flush=True)
        return products, status
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        failures = int((old_shop or {}).get("failures", 0)) + 1
        status = {
            "name": shop["name"], "ok": False, "failures": failures,
            "last_error": str(exc), "last_failure": int(time.time()),
            "product_count": int((old_shop or {}).get("product_count", 0)),
            "duration_ms": elapsed_ms,
            "discovery_ok": False,
        }
        if (old_shop or {}).get("last_success"):
            status["last_success"] = old_shop["last_success"]
        print(f"[{shop['name']}] FEHLER | {exc} | {elapsed_ms/1000:.2f}s", flush=True)
        return {}, status


def merge_shop_products(shop_id, old_products, products, shop_status):
    """Retain only failed reads, while accepting successful updates and removals."""
    merged = dict(products)
    if not shop_status.get("ok"):
        failed_urls = set(shop_status.get("failed_product_urls", []))
        discovery_failed = not shop_status.get("discovery_ok")
        for url, product in old_products.items():
            if not isinstance(product, dict) or product.get("shop_id") != shop_id:
                continue
            if discovery_failed or _clean_url(url) in failed_urls:
                retained = dict(product)
                retained["stale"] = True
                merged.setdefault(url, retained)
    shop_status["product_count"] = len(merged)
    return merged


def main():
    status = load_status()
    old_products = status.get("products", {}) if isinstance(status.get("products"), dict) else {}
    old_shops = status.get("shops", {}) if isinstance(status.get("shops"), dict) else {}
    old_detection_version = int(status.get("detection_version", 0) or 0)
    new_products = {}
    new_shops = {}

    print(
        f"BASELINE START: {len(SHOPS)} Shops | parallel bis zu {min(MAX_SHOP_WORKERS, len(SHOPS))} Shops "
        "| begrenzte Händler-Timeouts | keine Fallbacks/Proxies",
        flush=True,
    )
    run_started = time.monotonic()

    def run_one(index, shop):
        known = [
            url for url, product in old_products.items()
            if isinstance(product, dict) and product.get("shop_id") == shop["id"]
        ]
        print(f"\n[{index}/{len(SHOPS)}] {shop['name']}", flush=True)
        products, shop_status = process_shop(shop, old_shops.get(shop["id"], {}), known)
        return shop, products, shop_status

    # Shop-Aufrufe parallelisieren, Ergebnisse aber ausschließlich im Hauptthread
    # zusammenführen. Dadurch gibt es keine konkurrierenden Änderungen am Status.
    with ThreadPoolExecutor(max_workers=min(MAX_SHOP_WORKERS, len(SHOPS))) as executor:
        futures = {
            executor.submit(run_one, index, shop): index
            for index, shop in enumerate(SHOPS, 1)
        }
        for future in as_completed(futures):
            shop, products, shop_status = future.result()
            new_shops[shop["id"]] = shop_status
            new_products.update(
                merge_shop_products(shop["id"], old_products, products, shop_status)
            )

    pack_quantity_hints = {
        url: int(product.get("pack_quantity", 0) or 0)
        for url, product in new_products.items()
        if isinstance(product, dict) and int(product.get("pack_quantity", 0) or 0) > 0
    }

    status["shops"] = new_shops
    status["products"] = new_products
    enriched, _ = product_catalog.enrich_status(status)
    for url, quantity in pack_quantity_hints.items():
        product = enriched.get("products", {}).get(url)
        if isinstance(product, dict) and product.get("packaging") == "CASE":
            product["pack_quantity"] = quantity

    enriched["detection_version"] = DETECTION_VERSION
    if old_detection_version == DETECTION_VERSION:
        notify_changes(old_products, enriched.get("products", {}))
    else:
        print(
            f"NTFY: Detektor-Migration {old_detection_version}->{DETECTION_VERSION}; "
            "Status wird einmalig ohne Änderungsalarme neu baselined",
            flush=True,
        )
    save_status(enriched)

    ok = sum(1 for s in new_shops.values() if s.get("ok"))
    failed = len(SHOPS) - ok
    products = enriched.get("products", {})
    state_counts = Counter(
        str(product.get("state") or "UNKNOWN")
        for product in products.values()
        if isinstance(product, dict)
    )
    priced = sum(1 for product in products.values() if isinstance(product, dict) and product.get("price"))
    pending_prices = sum(
        1 for product in products.values()
        if isinstance(product, dict) and not product.get("price") and product.get("price_pending")
    )
    unexpected_unpriced = sum(
        1 for product in products.values()
        if isinstance(product, dict) and not product.get("price") and not product.get("price_pending")
    )
    currency_counts = Counter(
        str(product.get("currency") or "UNKNOWN")
        for product in products.values()
        if isinstance(product, dict) and product.get("price")
    )
    quality = {
        "listings": len(products),
        "states": dict(sorted(state_counts.items())),
        "priced": priced,
        "price_pending": pending_prices,
        "unexpected_without_price": unexpected_unpriced,
        "without_price": len(products) - priced,
        "currencies": dict(sorted(currency_counts.items())),
    }
    health = {
        "health": "OK" if failed == 0 else "DEGRADED",
        "mode": "baseline",
        "total_shops": len(SHOPS),
        "successful_shops": ok,
        "failed_shops": failed,
        "products": len(products),
        "duration_ms": int((time.monotonic() - run_started) * 1000),
        "quality": quality,
        "failed": [
            {"id": sid, "name": s.get("name"), "error": s.get("last_error", "")}
            for sid, s in new_shops.items() if not s.get("ok")
        ],
        "shop_timings_ms": {sid: s.get("duration_ms", 0) for sid, s in new_shops.items()},
    }
    HEALTH_FILE.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"QUALITÄT: states={dict(state_counts)} | Preise={priced}/{len(products)} | "
        f"Preis offen={pending_prices} | unerwartet ohne Preis={unexpected_unpriced} | "
        f"Währungen={dict(currency_counts)}",
        flush=True,
    )
    print(f"BASELINE ENDE: {ok}/{len(SHOPS)} OK | {failed} Fehler | {health['duration_ms']/1000:.1f}s", flush=True)


if __name__ == "__main__":
    main()
