from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_EMOJI_RE = re.compile("[\U00010000-\U0010ffff]", flags=re.UNICODE)
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
STATE_PRODUCTS_KEY = "products"
STATE_INITIALIZED_KEY = "initialized"
STATE_HEALTH_KEY = "health"
STATE_INCONCLUSIVE_KEY = "last_run_inconclusive"


@dataclass
class ProductResult:
    name: str
    retailer: str
    url: str
    in_stock: bool | None
    details: str
    section_entries: dict[str, list[dict[str, Any]]] | None = None


@dataclass
class StatusChange:
    result: ProductResult
    previous_in_stock: bool
    current_in_stock: bool


@dataclass
class InitialStatus:
    result: ProductResult


@dataclass
class RestockEvent:
    product_name: str
    section: str
    entry_name: str
    location: str
    price: str | None
    url: str


def normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", normalize_text(value))
    return "".join(char for char in normalized if not unicodedata.combining(char))


def load_products() -> list[dict[str, Any]]:
    raw_products = os.getenv("STOCK_PRODUCTS")
    if not raw_products:
        raise RuntimeError(
            "STOCK_PRODUCTS is required and must define exact product page URLs. "
            "In GitHub Actions, set it in either secrets.STOCK_PRODUCTS or vars.STOCK_PRODUCTS."
        )

    products = json.loads(raw_products)
    if not isinstance(products, list) or not products:
        raise ValueError("STOCK_PRODUCTS must be a non-empty JSON array")

    normalized_products: list[dict[str, Any]] = []
    for idx, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            raise ValueError(f"STOCK_PRODUCTS[{idx}] must be a JSON object")

        missing = [key for key in ("name", "retailer", "url") if not product.get(key)]
        if missing:
            raise ValueError(
                f"STOCK_PRODUCTS[{idx}] is missing required fields: {', '.join(missing)}"
            )

        normalized_products.append(
            {
                "name": str(product["name"]),
                "retailer": str(product["retailer"]),
                "url": str(product["url"]),
                "expected_keywords": list(product.get("expected_keywords", [])),
                "in_stock_keywords": list(product.get("in_stock_keywords", [])),
                "out_of_stock_keywords": list(product.get("out_of_stock_keywords", [])),
            }
        )

    return normalized_products


def load_state() -> dict[str, dict[str, Any]]:
    if not STATE_FILE.exists():
        return {
            STATE_INITIALIZED_KEY: False,
            STATE_PRODUCTS_KEY: {},
            STATE_HEALTH_KEY: {STATE_INCONCLUSIVE_KEY: False},
        }

    with STATE_FILE.open("r", encoding="utf-8") as state_file:
        data = json.load(state_file)
        if not isinstance(data, dict):
            return {
                STATE_INITIALIZED_KEY: False,
                STATE_PRODUCTS_KEY: {},
                STATE_HEALTH_KEY: {STATE_INCONCLUSIVE_KEY: False},
            }

        # Backward compatibility for the previous state format (flat product map).
        if STATE_PRODUCTS_KEY not in data:
            products = {
                key: value
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
            return {
                STATE_INITIALIZED_KEY: bool(products),
                STATE_PRODUCTS_KEY: products,
                STATE_HEALTH_KEY: {STATE_INCONCLUSIVE_KEY: False},
            }

        products = data.get(STATE_PRODUCTS_KEY)
        initialized = data.get(STATE_INITIALIZED_KEY)
        health = data.get(STATE_HEALTH_KEY)
        if not isinstance(health, dict):
            health = {STATE_INCONCLUSIVE_KEY: False}
        return {
            STATE_INITIALIZED_KEY: bool(initialized),
            STATE_PRODUCTS_KEY: products if isinstance(products, dict) else {},
            STATE_HEALTH_KEY: {
                STATE_INCONCLUSIVE_KEY: bool(health.get(STATE_INCONCLUSIVE_KEY, False))
            },
        }


def save_state(
    state: dict[str, dict[str, Any]],
    initialized: bool = True,
    last_run_inconclusive: bool = False,
) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                STATE_INITIALIZED_KEY: initialized,
                STATE_PRODUCTS_KEY: state,
                STATE_HEALTH_KEY: {STATE_INCONCLUSIVE_KEY: last_run_inconclusive},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def detect_stock_status(page_text: str, product: dict[str, Any]) -> ProductResult:
    normalized_text = normalize_match_text(page_text)
    expected_keywords = [normalize_match_text(item) for item in product.get("expected_keywords", [])]
    in_stock_keywords = [normalize_match_text(item) for item in product.get("in_stock_keywords", [])]
    out_of_stock_keywords = [normalize_match_text(item) for item in product.get("out_of_stock_keywords", [])]

    if expected_keywords and not all(keyword in normalized_text for keyword in expected_keywords):
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=None,
            details="expected product keywords were not found on the page",
        )

    if any(keyword in normalized_text for keyword in out_of_stock_keywords):
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=False,
            details="matched an out-of-stock keyword",
        )

    if any(keyword in normalized_text for keyword in in_stock_keywords):
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=True,
            details="matched an in-stock keyword",
        )

    return ProductResult(
        name=product["name"],
        retailer=product["retailer"],
        url=product["url"],
        in_stock=None,
        details="no stock keyword matched",
    )


def detect_climradar_status(soup: BeautifulSoup, product: dict[str, Any]) -> ProductResult:
    def parse_li_entry(li: Any) -> dict[str, Any] | None:
        row_el = li.find(["a", "div"], recursive=False)
        if not row_el:
            return None
        info_div = row_el.select_one("div.min-w-0.flex-1")
        if not info_div:
            return None
        ps = info_div.find_all("p", recursive=False)
        name = ps[0].get_text(strip=True) if ps else ""
        sub = _EMOJI_RE.sub("", ps[1].get_text(strip=True)).strip() if len(ps) > 1 else ""
        status_span = row_el.select_one("span.inline-flex")
        status_text = normalize_match_text(status_span.get_text(strip=True)) if status_span else ""
        is_available = "en stock" in status_text or "stock faible" in status_text
        price_span = row_el.select_one("span.tabular-nums")
        price = price_span.get_text(strip=True).replace("\xa0", "\u202f") if price_span else None
        return {"name": name, "sub": sub, "price": price, "isAvailable": is_available}

    page_text_norm = normalize_match_text(soup.get_text(" ", strip=True))
    if any(m in page_text_norm for m in ["acces interdit", "access denied"]):
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=None,
            details="access to climradar page was blocked",
        )

    online_section: Any = None
    physical_section: Any = None
    for h3 in soup.find_all("h3"):
        text = normalize_match_text(h3.get_text())
        if "disponibilit" in text and "en ligne" in text:
            online_section = h3.parent
        elif "magasins physiques" in text:
            physical_section = h3.parent

    # Fallback for minor DOM/header shifts: pick first two sections that contain stock rows.
    if not online_section or not physical_section:
        candidate_sections = [
            section for section in soup.find_all("section") if section.find("ul") and section.find("li")
        ]
        if not online_section and candidate_sections:
            online_section = candidate_sections[0]
        if not physical_section and len(candidate_sections) > 1:
            physical_section = candidate_sections[1]

    online_entries: list[dict[str, Any]] = []
    physical_entries: list[dict[str, Any]] = []

    if online_section:
        ul = online_section.find("ul")
        if ul:
            for li in ul.find_all("li"):
                entry = parse_li_entry(li)
                if entry:
                    item: dict[str, Any] = {
                        "name": entry["name"],
                        "website": entry["sub"],
                        "isAvailable": entry["isAvailable"],
                    }
                    if entry["price"]:
                        item["price"] = entry["price"]
                    online_entries.append(item)

    if physical_section:
        ul = physical_section.find("ul")
        if ul:
            for li in ul.find_all("li"):
                entry = parse_li_entry(li)
                if entry:
                    item = {
                        "name": entry["name"],
                        "localisation": entry["sub"],
                        "isAvailable": entry["isAvailable"],
                    }
                    if entry["price"]:
                        item["price"] = entry["price"]
                    physical_entries.append(item)

    debug_data = {"online": online_entries, "physical": physical_entries}
    debug_path = Path(os.getenv("CLIMRADAR_DEBUG_FILE", "climradar_debug.json"))
    debug_path.write_text(
        json.dumps(debug_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[DEBUG] climradar: {len(online_entries)} online + {len(physical_entries)} physical entries → {debug_path}")

    if not online_entries and not physical_entries:
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=None,
            details="no climradar entries found",
        )

    all_entries = online_entries + physical_entries
    has_positive = any(e["isAvailable"] for e in all_entries)

    if has_positive:
        available_names = [e["name"] for e in all_entries if e["isAvailable"]]
        details = "climradar: en stock/stock faible at: " + ", ".join(available_names)
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=True,
            details=details,
            section_entries=debug_data,
        )

    return ProductResult(
        name=product["name"],
        retailer=product["retailer"],
        url=product["url"],
        in_stock=False,
        details="climradar reports only rupture entries",
        section_entries=debug_data,
    )


def fetch_climradar_html(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
        # Expand all collapsed sections (online + physical)
        expand_locator = page.get_by_role("button", name=re.compile(r"[Dd][eé]plier"))
        while expand_locator.count() > 0:
            expand_locator.first.click()
            page.wait_for_load_state("networkidle")
        html = page.content()
        browser.close()
        return html


def fetch_product_status(session: requests.Session, product: dict[str, Any]) -> ProductResult:
    hostname = urlparse(product["url"]).hostname or ""
    if "climradar.fr" in hostname.lower():
        html = fetch_climradar_html(product["url"])
        soup = BeautifulSoup(html, "html.parser")
        return detect_climradar_status(soup, product)

    response = session.get(
        product["url"],
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    hostname = urlparse(product["url"]).hostname or ""

    page_text = soup.get_text(" ", strip=True)
    return detect_stock_status(page_text, product)


def _entry_key(name: str, location: str) -> str:
    return f"{normalize_match_text(name)}|{normalize_match_text(location)}"


def build_state_entry(result: ProductResult) -> dict[str, Any]:
    if result.section_entries:
        online_state = []
        for entry in result.section_entries.get("online", []):
            website = str(entry.get("website", ""))
            online_state.append(
                {
                    "key": _entry_key(str(entry.get("name", "")), website),
                    "name": str(entry.get("name", "")),
                    "website": website,
                    "price": entry.get("price"),
                    "is_available": bool(entry.get("isAvailable", False)),
                }
            )

        physical_state = []
        for entry in result.section_entries.get("physical", []):
            localisation = str(entry.get("localisation", ""))
            physical_state.append(
                {
                    "key": _entry_key(str(entry.get("name", "")), localisation),
                    "name": str(entry.get("name", "")),
                    "localisation": localisation,
                    "price": entry.get("price"),
                    "is_available": bool(entry.get("isAvailable", False)),
                }
            )

        return {
            "retailer": result.retailer,
            "url": result.url,
            "online": online_state,
            "physical": physical_state,
            "summary_in_stock": result.in_stock,
        }

    return {
        "retailer": result.retailer,
        "url": result.url,
        "online": [
            {
                "key": _entry_key(result.name, result.retailer),
                "name": result.name,
                "website": result.retailer,
                "price": None,
                "is_available": bool(result.in_stock),
            }
        ],
        "physical": [],
        "summary_in_stock": result.in_stock,
    }


def detect_restock_events(
    previous_entry: dict[str, Any] | None,
    current_entry: dict[str, Any],
    product_name: str,
    product_url: str,
) -> list[RestockEvent]:
    events: list[RestockEvent] = []
    prev_online_map = {
        str(item.get("key", "")): bool(item.get("is_available", False))
        for item in previous_entry.get("online", [])
        if isinstance(item, dict) and item.get("key")
    } if isinstance(previous_entry, dict) else {}
    prev_physical_map = {
        str(item.get("key", "")): bool(item.get("is_available", False))
        for item in previous_entry.get("physical", [])
        if isinstance(item, dict) and item.get("key")
    } if isinstance(previous_entry, dict) else {}

    for item in current_entry.get("online", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", ""))
        now_available = bool(item.get("is_available", False))
        was_available = prev_online_map.get(key)
        if was_available is False and now_available:
            events.append(
                RestockEvent(
                    product_name=product_name,
                    section="online",
                    entry_name=str(item.get("name", "unknown")),
                    location=str(item.get("website", "")),
                    price=str(item.get("price")) if item.get("price") else None,
                    url=product_url,
                )
            )

    for item in current_entry.get("physical", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", ""))
        now_available = bool(item.get("is_available", False))
        was_available = prev_physical_map.get(key)
        if was_available is False and now_available:
            events.append(
                RestockEvent(
                    product_name=product_name,
                    section="physical",
                    entry_name=str(item.get("name", "unknown")),
                    location=str(item.get("localisation", "")),
                    price=str(item.get("price")) if item.get("price") else None,
                    url=product_url,
                )
            )

    return events


def build_email_body(changes: list[StatusChange]) -> str:
    lines = ["Stock status changed:", ""]
    for change in changes:
        result = change.result
        lines.extend(
            [
                f"- {result.name}",
                f"  Retailer: {result.retailer}",
                f"  Previous status: {'in stock' if change.previous_in_stock else 'out of stock'}",
                f"  Current status: {'in stock' if change.current_in_stock else 'out of stock'}",
                f"  Detection details: {result.details}",
                f"  URL: {result.url}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def build_restock_email_body(events: list[RestockEvent]) -> str:
    lines = [
        "Restock detected:",
        "",
        f"{len(events)} previously unavailable entries are now available.",
        "",
    ]
    for event in events:
        lines.append(f"- Product: {event.product_name}")
        lines.append(f"  Section: {event.section}")
        lines.append(f"  Name: {event.entry_name}")
        lines.append(f"  Location: {event.location}")
        if event.price:
            lines.append(f"  Price: {event.price}")
        lines.append(f"  URL: {event.url}")
        lines.append("")
    return "\n".join(lines).strip()


def build_initial_email_body(statuses: list[InitialStatus]) -> str:
    lines = ["Initial stock status:", ""]
    for status in statuses:
        result = status.result
        lines.extend(
            [
                f"- {result.name}",
                f"  Retailer: {result.retailer}",
                f"  Current status: {'in stock' if result.in_stock else 'out of stock'}",
                f"  Detection details: {result.details}",
                f"  URL: {result.url}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def build_inconclusive_email_body(
    products: list[dict[str, Any]],
    errors: list[str],
    previous_inconclusive: bool,
) -> str:
    lines = [
        "Stock check is inconclusive.",
        "",
        "No product status could be determined.",
        "All product checks either failed or returned inconclusive results.",
        "",
        f"Previous run inconclusive: {'yes' if previous_inconclusive else 'no'}",
        "",
        "Configured products:",
    ]

    for product in products:
        lines.append(f"- {product.get('name', 'unknown product')}: {product.get('url', '')}")

    if errors:
        lines.extend(["", "Errors:"])
        for error in errors:
            lines.append(f"- {error}")

    return "\n".join(lines).strip()


def send_inconclusive_email(
    products: list[dict[str, Any]],
    errors: list[str],
    previous_inconclusive: bool,
) -> None:
    subject = f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: stock check inconclusive"
    body = build_inconclusive_email_body(
        products=products,
        errors=errors,
        previous_inconclusive=previous_inconclusive,
    )
    send_notification_message(subject=subject, body=body)


def send_recovery_email() -> None:
    subject = f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: stock check recovered"
    body = "Stock check recovered: at least one product status was determined in this run."
    send_notification_message(subject=subject, body=body)


def send_webhook_message(subject: str, body: str) -> bool:
    raw_urls = os.getenv("NOTIFICATION_WEBHOOK_URLS", "")
    urls: list[str] = []
    if raw_urls.strip():
        for chunk in raw_urls.replace("\n", ",").split(","):
            candidate = chunk.strip()
            if candidate:
                urls.append(candidate)

    # Preserve order while removing duplicates.
    unique_urls = list(dict.fromkeys(urls))
    if not unique_urls:
        return False

    timeout = int(os.getenv("NOTIFICATION_WEBHOOK_TIMEOUT", "15"))
    debug_webhook = os.getenv("NOTIFICATION_DEBUG", "false").lower() == "true"
    payload = {
        "subject": subject,
        "message": body,
        "text": f"{subject}\n\n{body}",
        "content": f"{subject}\n\n{body}",
    }
    headers = {
        "Content-Type": "application/json",
    }
    sent_any = False
    for webhook_url in unique_urls:
        webhook_host = (urlparse(webhook_url).hostname or "unknown-host").lower()
        if debug_webhook:
            print(
                "INFO: Webhook notification attempt "
                f"host={webhook_host} subject={subject} timeout={timeout}s"
            )

        try:
            response = requests.post(
                webhook_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            sent_any = True
            if debug_webhook:
                print(
                    "INFO: Webhook notification sent successfully "
                    f"host={webhook_host} status={response.status_code}"
                )
        except requests.RequestException as error:
            print(
                "WARNING: Failed to send webhook notification. "
                "Check NOTIFICATION_WEBHOOK_URLS and endpoint availability. "
                f"Host={webhook_host}. Error: {error}"
            )

    return sent_any


def send_notification_message(subject: str, body: str) -> None:
    if not send_webhook_message(subject=subject, body=body):
        print(
            "WARNING: No webhook notification sent. "
            "Set NOTIFICATION_WEBHOOK_URLS to enable alerts."
        )


def send_email(changes: list[StatusChange]) -> None:
    subject = f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: {len(changes)} status change(s)"
    send_notification_message(subject=subject, body=build_email_body(changes))


def send_restock_email(events: list[RestockEvent]) -> None:
    subject = f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: {len(events)} restock(s) detected"
    send_notification_message(subject=subject, body=build_restock_email_body(events))


def send_initial_email(statuses: list[InitialStatus]) -> None:
    subject = (
        f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: "
        f"initial status for {len(statuses)} product(s)"
    )
    send_notification_message(subject=subject, body=build_initial_email_body(statuses))


def main() -> int:
    products = load_products()
    state_data = load_state()
    previous_state = state_data[STATE_PRODUCTS_KEY]
    is_first_run = not bool(state_data[STATE_INITIALIZED_KEY])
    next_state = dict(previous_state)
    restock_events: list[RestockEvent] = []
    errors: list[str] = []
    determined_statuses = 0
    previous_inconclusive = bool(
        state_data.get(STATE_HEALTH_KEY, {}).get(STATE_INCONCLUSIVE_KEY, False)
    )

    with requests.Session() as session:
        for product in products:
            product_name = str(product.get("name", product.get("url", "unknown product")))
            try:
                result = fetch_product_status(session, product)
            except (requests.RequestException, ValueError, TypeError, KeyError) as error:
                errors.append(f"{product_name}: {error}")
                continue

            print(f"{result.name}: {result.in_stock} ({result.details})")

            if result.in_stock is None:
                continue

            determined_statuses += 1
            current_entry = build_state_entry(result)
            previous_entry = previous_state.get(result.name)

            restock_events.extend(
                detect_restock_events(
                    previous_entry=previous_entry if isinstance(previous_entry, dict) else None,
                    current_entry=current_entry,
                    product_name=result.name,
                    product_url=result.url,
                )
            )

            next_state[result.name] = current_entry

    if determined_statuses == 0:
        message = (
            "No product status could be determined. "
            "All product checks either failed or returned inconclusive results."
        )
        print(f"WARNING: {message}")
        if errors:
            for error in errors:
                print(f"WARNING: {error}")

        save_state(
            next_state,
            initialized=bool(state_data[STATE_INITIALIZED_KEY]),
            last_run_inconclusive=True,
        )
        return 0

    available_online = 0
    available_physical = 0
    tracked_online = 0
    tracked_physical = 0
    for product_state in next_state.values():
        if not isinstance(product_state, dict):
            continue
        online_entries = product_state.get("online", [])
        physical_entries = product_state.get("physical", [])
        if isinstance(online_entries, list):
            tracked_online += len(online_entries)
            available_online += sum(
                1 for entry in online_entries if isinstance(entry, dict) and entry.get("is_available")
            )
        if isinstance(physical_entries, list):
            tracked_physical += len(physical_entries)
            available_physical += sum(
                1 for entry in physical_entries if isinstance(entry, dict) and entry.get("is_available")
            )

    print("Summary:")
    print(f"- Determined products: {determined_statuses}/{len(products)}")
    print(f"- Online availability: {available_online}/{tracked_online}")
    print(f"- Physical availability: {available_physical}/{tracked_physical}")
    print(f"- New restocks: {len(restock_events)}")

    if restock_events:
        send_restock_email(restock_events)

    if next_state != previous_state or is_first_run:
        save_state(next_state, initialized=True, last_run_inconclusive=False)
    elif previous_inconclusive:
        save_state(next_state, initialized=True, last_run_inconclusive=False)

    if errors:
        for error in errors:
            print(f"WARNING: {error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
