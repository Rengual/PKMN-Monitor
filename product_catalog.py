import re
import unicodedata
from collections import Counter
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

CATALOG_VERSION = 8
SET_ID = "pokemon-tcg-30th-celebration"
SET_NAME = "Pokémon TCG: 30th Celebration"

# Official product families from The Pokémon Company's 30th Celebration
# product showcase (30 June 2026). The catalog intentionally contains only
# these sealed retail families. Retailer-created bundles/cases are packaging
# forms of these products, not additional catalog products.
OFFICIAL_PRODUCTS = (
    {
        "id": "POKEMON_CENTER_ELITE_TRAINER_BOX",
        "name": "Pokémon Center Elite Trainer Box",
        "patterns": (
            r"\bpokemon[ -]?center\b.*\b(?:elite[ -]?trainer[ -]?box|etb|top[ -]?trainer[ -]?box)\b",
            r"\b(?:elite[ -]?trainer[ -]?box|etb|top[ -]?trainer[ -]?box)\b.*\bpokemon[ -]?center\b",
        ),
    },
    {
        "id": "ELITE_TRAINER_BOX",
        "name": "Elite Trainer Box",
        "patterns": (
            r"\belite[ -]?trainer[ -]?box\b",
            r"\betb\b",
            r"\btop[ -]?trainer[ -]?box\b",
        ),
    },
    {
        "id": "ULTRA_PREMIUM_COLLECTION",
        "name": "Ultra-Premium Collection—Day & Night",
        "patterns": (
            r"\bultra[ -]?premium[ -]?(?:collection|kollektion(?:en)?)\b",
            r"\bupc\b",
            r"\bpremium[ -]?(?:collection|kollektion(?:en)?)\b.*\b(?:day|night|tag|nacht)\b",
            r"\b(?:day|night|tag|nacht)\b.*\bpremium[ -]?(?:collection|kollektion(?:en)?)\b",
        ),
    },
)

OFFICIAL_PRODUCT_MAP = {item["id"]: item for item in OFFICIAL_PRODUCTS}
TYPE_LABELS = {item["id"]: item["name"] for item in OFFICIAL_PRODUCTS}
TYPE_LABELS["OTHER"] = "Nicht offiziell zugeordnet"

NON_POKEMON_MARKERS = (
    "spider man", "spider-man", "marvel", "magic the gathering", "mtg",
    "yu-gi-oh", "yugioh", "one piece", "lorcana", "star wars",
    "dragon ball", "digimon", "flesh and blood", "fab tcg", "jurassic park",
)

STATE_PRIORITY = {
    "IN_STOCK": 0,
    "LOW_STOCK": 1,
    "PREORDER": 2,
    "COMING_SOON": 3,
    "OUT_OF_STOCK": 4,
    "UNKNOWN": 5,
}


def ascii_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def compact_text(value):
    return re.sub(r"\s+", " ", ascii_text(value)).strip()


def _match_text(value):
    text = compact_text(value)
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"[_/:]+", " ", text)


def contains_non_pokemon_franchise(title, url=""):
    context = compact_text(f"{title} {url}")
    return any(marker in context for marker in NON_POKEMON_MARKERS)


def detect_language(title, url=""):
    # Storefront locale paths (/de/, /nl/, ...) describe the website, not the
    # product language. Bare language codes are therefore read from the title
    # only; full language words may also appear in the product slug.
    title_text = compact_text(title)
    path = compact_text(urlsplit(str(url or "")).path.replace("/", " "))
    context = f"{title_text} {path}"

    if re.search(r"\b(?:english|englisch|engels|anglais|ingles)\b", context):
        return "EN"
    if re.search(r"(?:^|[\s\[(\-])(?:en|eng)(?:$|[\s\])\-])", title_text):
        return "EN"

    language_rules = (
        ("DE", r"\b(?:german|deutsch|duits)\b", r"(?:^|[\s\[(\-])(?:de|ger)(?:$|[\s\])\-])"),
        ("JA", r"\b(?:japanese|japanisch|japans|japonais)\b", r"(?:^|[\s\[(\-])(?:jp|jpn|ja)(?:$|[\s\])\-])"),
        ("FR", r"\b(?:french|francais|franzosisch|frans)\b", r"(?:^|[\s\[(\-])(?:fr|fra)(?:$|[\s\])\-])"),
        ("ES", r"\b(?:spanish|spanisch|espanol|spaans)\b", r"(?:^|[\s\[(\-])es(?:$|[\s\])\-])"),
        ("IT", r"\b(?:italian|italienisch|italiaans)\b", r"(?:^|[\s\[(\-])it(?:$|[\s\])\-])"),
        ("KO", r"\b(?:korean|koreanisch|koreaans)\b", r"(?:^|[\s\[(\-])(?:kr|kor|ko)(?:$|[\s\])\-])"),
        ("ZH", r"\b(?:chinese|chinesisch|chinees)\b", r"(?:^|[\s\[(\-])(?:cn|zh)(?:$|[\s\])\-])"),
        ("PL", r"\b(?:polish|polnisch|pools)\b", r"(?:^|[\s\[(\-])pl(?:$|[\s\])\-])"),
        ("PT", r"\b(?:portuguese|portugiesisch|portugees)\b", r"(?:^|[\s\[(\-])pt(?:$|[\s\])\-])"),
    )
    for code, word_pattern, code_pattern in language_rules:
        if re.search(word_pattern, context, re.I) or re.search(code_pattern, title_text, re.I):
            return code
    return "EN"


def _has_30th_marker(title, url="", page_text=""):
    context = compact_text(f"{title} {url}")
    if re.search(
        r"\b30(?:th)?\b|\b30th[- ]?(?:anniversary|celebration)\b|\b30\s*(?:years?|jahre)\b",
        context,
    ):
        return True
    page = compact_text(page_text)
    return "30th anniversary" in page or "30th celebration" in page or "30 jahre" in page


def classify_official_product(title, url=""):
    title_context = _match_text(title)
    for item in OFFICIAL_PRODUCTS:
        if any(re.search(pattern, title_context, re.I) for pattern in item["patterns"]):
            return item

    # If the title explicitly names a different sealed 30th product family, do
    # not allow an unrelated category word in the URL to turn it into ETB/UPC.
    explicit_other = re.search(
        r"\b(?:first partner|booster(?: bundle)?|mini tin|tech sticker|battle deck|figure collection|ex box|binder|poster|blister)\b",
        title_context,
        re.I,
    )
    if explicit_other:
        return None

    context = _match_text(f"{title} {url}")
    for item in OFFICIAL_PRODUCTS:
        if any(re.search(pattern, context, re.I) for pattern in item["patterns"]):
            return item
    return None


def detect_product_type(title):
    item = classify_official_product(title)
    return item["id"] if item else "OTHER"


def looks_like_single_card(title):
    text = compact_text(title)
    collector_number = bool(re.search(r"\b30c\s*[- ]?\d{2,3}\b", text))
    rarity = bool(re.search(
        r"\b(?:common|uncommon|rare|double rare|illustration rare|ultra rare|secret rare|special illustration rare)\b",
        text,
    ))
    explicit_card = bool(re.search(r"\b(?:single card|singlecard|karte einzeln|einzelkarte)\b", text))
    return explicit_card or (collector_number and rarity and detect_product_type(title) == "OTHER")


def is_target_standalone_product(title, url=""):
    context = compact_text(f"{title} {url}")
    title_context = compact_text(title)
    if re.search(r"\blive[- ]?break\b|\brip[- ]?(?:and|&)\s*ship\b|\bim live[- ]?stream offnen\b", title_context):
        return False
    if re.search(r"\bbooster bundle\b|\bposter collection\b|\bmini tin\b", title_context) and re.search(r"\bbundle\b|\+", title_context):
        return False
    item = classify_official_product(title, url)
    if not item:
        return False
    product_id = item["id"]

    # Keep every UPC listing, including Day, Night, unspecified versions,
    # sets and sealed cases. For ETBs, cases are also valid. Only retailer
    # bundles/packs that mix a target product with other product families are
    # excluded.
    if product_id == "ULTRA_PREMIUM_COLLECTION":
        return True

    if product_id in {"ELITE_TRAINER_BOX", "POKEMON_CENTER_ELITE_TRAINER_BOX"}:
        if re.search(r"\b(?:bundle|pack:)\b", context, re.I):
            return False
        return True
    return False


def candidate_allowed(title, url="", page_text=""):
    if contains_non_pokemon_franchise(title, url):
        return False
    if detect_language(title, url) != "EN":
        return False
    # A concrete product attribute overrides a language guessed from its URL.
    language = re.search(r"\b(?:sprache|language|taal|jezyk)\s*[:\-]\s*([^|;\n]{1,35})", compact_text(page_text))
    if language and detect_language(language.group(1)) != "EN":
        return False
    if looks_like_single_card(title):
        return False
    if not _has_30th_marker(title, url, page_text):
        return False
    return is_target_standalone_product(title, url)


def detect_packaging(title):
    text = compact_text(title)
    unit_words = (
        r"elite[- ]trainer[- ]box(?:es)?|ultra[- ]premium[- ]collections?|etbs?|upcs?|box(?:es)?|bundles?|tins?|blisters?|collections?|decks?|packs?|units?|pcs?|stuck|stueck"
    )
    bulk_box = bool(re.search(
        rf"\bbox\s*(?:of\s*)?x?\s*\d{{1,3}}\s*(?:{unit_words})\b",
        text,
    ))
    is_case = bool(re.search(r"\b(?:sealed )?case\b|\bkarton\b|\bmaster case\b", text)) or bulk_box
    multipack = re.search(r"\bset of (\d{1,3})\b|\b(\d{1,3})er[- ]?set\b", text)
    if multipack and not is_case:
        return "MULTIPACK", int(multipack.group(1) or multipack.group(2))
    paired_set = re.search(r"\b(\d{1,2})\s*x\s*(?:day|night)\s*\+\s*(\d{1,2})\s*x\s*(?:day|night)\b", text)
    if paired_set and "set" in text and not is_case:
        return "MULTIPACK", int(paired_set.group(1)) + int(paired_set.group(2))
    labelled_quantity = re.search(r"(?:^| · )(\d{1,3})\s*[x×]\s+", text)
    quantity = int(labelled_quantity.group(1)) if labelled_quantity else 1

    quantity_patterns = (
        rf"\b(?:case|karton|box)\s*(?:of\s*)?x?\s*(\d{{1,3}})(?=\s*(?:{unit_words})\b|\s*$)",
        rf"\bx\s*(\d{{1,3}})\s*(?:{unit_words})\b",
        rf"\b(\d{{1,3}})\s*x\s*(?:{unit_words})\b",
    )
    for pattern in quantity_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            try:
                quantity = max(1, int(match.group(1)))
            except (TypeError, ValueError):
                quantity = 1
            break

    if is_case and quantity == 1:
        quantity = 0
    return ("CASE" if is_case else "MULTIPACK" if quantity > 1 else "SINGLE"), quantity


def collapse_shopify_collection_path(url):
    raw = str(url or "")
    parts = urlsplit(raw)
    path = parts.path
    lower = path.lower()
    marker = "/products/"
    if marker not in lower:
        return raw

    marker_index = lower.index(marker)
    prefix = path[:marker_index]
    handle_path = path[marker_index:]
    locale_match = re.match(r"^/([a-z]{2}(?:-[a-z]{2})?)/", prefix, re.I)
    locale_prefix = f"/{locale_match.group(1)}" if locale_match else ""
    new_path = locale_prefix + handle_path
    return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))


def detect_official_variant(product_id, title):
    text = compact_text(title)

    def one_of(pairs, default="UNSPECIFIED"):
        hits = [code for code, pattern in pairs if re.search(pattern, text, re.I)]
        return hits[0] if len(hits) == 1 else default

    if product_id == "TECH_STICKER_COLLECTION":
        return one_of((
            ("ALOLAN_EXEGGUTOR", r"\b(?:alolan )?exeggutor\b"),
            ("LUCARIO", r"\blucario\b"),
        ))
    if product_id == "EX_BOX":
        return one_of((
            ("SYLVEON_EX", r"\bsylveon\b"),
            ("GRENINJA_EX", r"\bgreninja\b"),
        ))
    if product_id == "BATTLE_DECK":
        return one_of((
            ("ESPEON_EX", r"\bespeon\s*ex\b"),
            ("UMBREON_EX", r"\bumbreon\s*ex\b"),
        ))
    if product_id == "ULTRA_PREMIUM_COLLECTION":
        return one_of((
            ("DAY", r"\b(?:day|tag|espeon|psiana)\b"),
            ("NIGHT", r"\b(?:night|nacht|umbreon|nachtara)\b"),
        ))
    if product_id == "FIGURE_COLLECTION":
        return one_of((
            ("MEWTWO", r"\bmewtwo\b"),
            ("MEW", r"\bmew\b"),
        ))
    return "STANDARD"


def canonical_identity(title, url=""):
    item = classify_official_product(title, url)
    product_type = item["id"] if item else "OTHER"
    product_name = item["name"] if item else "Nicht offiziell zugeordnet"
    language = detect_language(title, url)
    packaging, quantity = detect_packaging(title)
    variant = detect_official_variant(product_type, title) if item else "UNSPECIFIED"

    # The canonical identity is the official Pokémon product family. Cases,
    # multipacks and retailer bundles remain listing metadata so they do not
    # create fake catalog products. Specific official variants remain visible
    # on each listing and in the catalog's variants array.
    canonical_id = f"p30-{product_type.lower().replace('_', '-')}" if item else "p30-unclassified"
    return {
        "canonical_id": canonical_id,
        "canonical_title": f"{SET_NAME} – {product_name}",
        "franchise": "POKEMON",
        "set_id": SET_ID,
        "set_name": SET_NAME,
        "language": language,
        "product_type": product_type,
        "official_product": bool(item),
        "official_product_id": product_type if item else "",
        "official_product_name": product_name if item else "",
        "official_variant": variant,
        "packaging": packaging,
        "pack_quantity": quantity,
        "identity_core": variant.lower().replace("_", " ") if variant not in {"STANDARD", "UNSPECIFIED"} else "standard",
        "sealed": True,
        "catalog_version": CATALOG_VERSION,
    }


def rejection_reason(product):
    if not isinstance(product, dict):
        return "invalid product"
    title = str(product.get("title") or "")
    url = str(product.get("url") or "")
    if contains_non_pokemon_franchise(title, url):
        return "explicit non-Pokémon franchise marker"
    language = detect_language(title, url)
    if language != "EN":
        return f"non-English product language: {language}"
    if looks_like_single_card(title):
        return "single card listing; sealed products only"
    if not _has_30th_marker(title, url):
        return "missing 30th Celebration marker"
    if classify_official_product(title, url) is None:
        return "not a monitored 30th Celebration product family"
    if not is_target_standalone_product(title, url):
        return "not a target UPC or ETB listing"
    return ""


def enrich_product(product):
    reason = rejection_reason(product)
    if reason:
        return None
    enriched = dict(product)
    enriched.update(canonical_identity(product.get("title", ""), product.get("url", "")))
    if enriched.get("packaging") in {"CASE", "MULTIPACK"} and int(product.get("pack_quantity") or 0) > 0:
        enriched["pack_quantity"] = int(product["pack_quantity"])
    return enriched


def _aggregate_state(listings):
    states = [item.get("state", "UNKNOWN") for item in listings if not item.get("stale")]
    if not states:
        return "UNKNOWN"
    return min(states, key=lambda state: STATE_PRIORITY.get(state, 99))


def build_catalog(products):
    grouped = {}
    for url, product in products.items():
        if not isinstance(product, dict):
            continue
        canonical_id = product.get("canonical_id")
        if not canonical_id or not product.get("official_product"):
            continue
        grouped.setdefault(canonical_id, []).append((url, product))

    catalog = {}
    for canonical_id, pairs in sorted(grouped.items()):
        listings = []
        states = Counter()
        shops = set()
        variants = set()
        for url, product in sorted(pairs, key=lambda pair: (pair[1].get("shop", ""), pair[0])):
            shops.add(product.get("shop_id") or product.get("shop") or "")
            state = product.get("state", "UNKNOWN")
            states[state] += 1
            variant = product.get("official_variant", "UNSPECIFIED")
            if variant:
                variants.add(variant)
            listings.append({
                "shop_id": product.get("shop_id", ""),
                "shop": product.get("shop", ""),
                "title": product.get("title", ""),
                "url": product.get("url") or url,
                "state": state,
                "price": product.get("price", ""),
                "currency": product.get("currency", ""),
                "stale": bool(product.get("stale")),
                "last_checked": product.get("last_checked"),
                "available": bool(product.get("available")),
                "official_variant": variant,
                "packaging": product.get("packaging", "SINGLE"),
                "pack_quantity": product.get("pack_quantity", 1),
            })

        exemplar = pairs[0][1]
        variant_groups = {}
        for listing in listings:
            variant = listing.get("official_variant", "UNSPECIFIED")
            variant_groups.setdefault(variant, []).append(listing)

        catalog[canonical_id] = {
            "canonical_id": canonical_id,
            "canonical_title": exemplar.get("canonical_title", ""),
            "franchise": "POKEMON",
            "set_id": SET_ID,
            "set_name": SET_NAME,
            "language": exemplar.get("language", "EN"),
            "product_type": exemplar.get("product_type", "OTHER"),
            "official_product": True,
            "official_product_id": exemplar.get("official_product_id", ""),
            "official_product_name": exemplar.get("official_product_name", ""),
            "variants": sorted(variants),
            "variant_groups": variant_groups,
            "sealed": True,
            "listing_count": len(listings),
            "shop_count": len({shop for shop in shops if shop}),
            "aggregate_state": _aggregate_state([item for _, item in pairs]),
            "state_counts": dict(sorted(states.items())),
            "listings": listings,
        }
    return catalog


def enrich_status(status):
    status = deepcopy(status) if isinstance(status, dict) else {}
    source_products = status.get("products", {}) if isinstance(status.get("products"), dict) else {}
    products = {}
    rejected = []

    for url, product in source_products.items():
        reason = rejection_reason(product)
        if reason:
            rejected.append({
                "url": url,
                "shop_id": product.get("shop_id", "") if isinstance(product, dict) else "",
                "title": product.get("title", "") if isinstance(product, dict) else "",
                "reason": reason,
            })
            continue
        enriched = dict(product)
        raw_price = str(enriched.get("price", "") or "").strip().replace(",", ".")
        try:
            if raw_price and float(raw_price) <= 0:
                enriched["price"] = ""
        except ValueError:
            pass
        enriched.update(canonical_identity(product.get("title", ""), product.get("url", "")))
        if enriched.get("packaging") in {"CASE", "MULTIPACK"} and int(product.get("pack_quantity") or 0) > 0:
            enriched["pack_quantity"] = int(product["pack_quantity"])
        products[url] = enriched

    shops = status.get("shops", {}) if isinstance(status.get("shops"), dict) else {}
    counts = Counter(product.get("shop_id") for product in products.values() if product.get("shop_id"))
    for shop_id, shop in shops.items():
        if isinstance(shop, dict):
            shop["product_count"] = counts.get(shop_id, 0)

    status["products"] = products
    status["shops"] = shops
    status["catalog"] = build_catalog(products)
    status["catalog_meta"] = {
        "version": CATALOG_VERSION,
        "official_whitelist": True,
        "official_family_count": len(OFFICIAL_PRODUCTS),
        "official_families_present": len(status["catalog"]),
        "listings": len(products),
        "canonical_products": len(status["catalog"]),
        "rejected_products": len(rejected),
    }
    status["version"] = max(int(status.get("version", 0) or 0), 7)
    return status, rejected
