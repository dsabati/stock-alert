from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
PRODUCT_SEARCH_QUERY = "midea portasplit"
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))


def retailer_search_url(base_url: str, query: str) -> str:
    encoded_query = quote_plus(query)
    return base_url.format(query=encoded_query)


DEFAULT_PRODUCTS: list[dict[str, Any]] = [
    {
        "name": "Midea PortaSplit - Amazon",
        "retailer": "Amazon",
        "url": retailer_search_url("https://www.amazon.fr/s?k={query}", PRODUCT_SEARCH_QUERY),
        "expected_keywords": ["midea", "portasplit"],
        "in_stock_keywords": [
            "ajouter au panier",
            "acheter maintenant",
            "livraison gratuite",
        ],
        "out_of_stock_keywords": [
            "actuellement indisponible",
            "temporairement en rupture de stock",
            "nous ne savons pas quand cet article sera de nouveau approvisionné",
        ],
    },
    {
        "name": "Midea PortaSplit - Castorama",
        "retailer": "Castorama",
        "url": retailer_search_url(
            "https://www.castorama.fr/recherche?term={query}",
            PRODUCT_SEARCH_QUERY,
        ),
        "expected_keywords": ["midea", "portasplit"],
        "in_stock_keywords": [
            "ajouter au panier",
            "disponible",
            "livraison",
        ],
        "out_of_stock_keywords": [
            "indisponible",
            "rupture de stock",
            "plus disponible",
        ],
    },
    {
        "name": "Midea PortaSplit - Darty",
        "retailer": "Darty",
        "url": retailer_search_url(
            "https://www.darty.com/nav/recherche?text={query}",
            PRODUCT_SEARCH_QUERY,
        ),
        "expected_keywords": ["midea", "portasplit"],
        "in_stock_keywords": [
            "ajouter au panier",
            "retirer en magasin",
            "livraison",
        ],
        "out_of_stock_keywords": [
            "indisponible",
            "rupture de stock",
            "temporairement indisponible",
        ],
    },
    {
        "name": "Midea PortaSplit - Leroy Merlin",
        "retailer": "Leroy Merlin",
        "url": retailer_search_url(
            "https://www.leroymerlin.fr/recherche/?q={query}",
            PRODUCT_SEARCH_QUERY,
        ),
        "expected_keywords": ["midea", "portasplit"],
        "in_stock_keywords": [
            "ajouter au panier",
            "disponible",
            "livraison",
        ],
        "out_of_stock_keywords": [
            "indisponible",
            "rupture de stock",
            "non disponible",
        ],
    },
]


@dataclass
class ProductResult:
    name: str
    retailer: str
    url: str
    in_stock: bool | None
    details: str


def normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def load_products() -> list[dict[str, Any]]:
    raw_products = os.getenv("STOCK_PRODUCTS")
    if not raw_products:
        return DEFAULT_PRODUCTS

    products = json.loads(raw_products)
    if not isinstance(products, list) or not products:
        raise ValueError("STOCK_PRODUCTS must be a non-empty JSON array")

    return products


def load_state() -> dict[str, dict[str, Any]]:
    if not STATE_FILE.exists():
        return {}

    with STATE_FILE.open("r", encoding="utf-8") as state_file:
        data = json.load(state_file)
        return data if isinstance(data, dict) else {}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def detect_stock_status(page_text: str, product: dict[str, Any]) -> ProductResult:
    normalized_text = normalize_text(page_text)
    expected_keywords = [normalize_text(item) for item in product.get("expected_keywords", [])]
    in_stock_keywords = [normalize_text(item) for item in product.get("in_stock_keywords", [])]
    out_of_stock_keywords = [normalize_text(item) for item in product.get("out_of_stock_keywords", [])]

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


def fetch_product_status(session: requests.Session, product: dict[str, Any]) -> ProductResult:
    response = session.get(
        product["url"],
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    return detect_stock_status(page_text, product)


def build_email_body(changes: list[tuple[ProductResult, bool, bool]]) -> str:
    lines = ["Stock status changed:", ""]
    for result, previous_status, current_status in changes:
        lines.extend(
            [
                f"- {result.name}",
                f"  Retailer: {result.retailer}",
                f"  Previous status: {'in stock' if previous_status else 'out of stock'}",
                f"  Current status: {'in stock' if current_status else 'out of stock'}",
                f"  Detection details: {result.details}",
                f"  URL: {result.url}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def send_email(changes: list[tuple[ProductResult, bool, bool]]) -> None:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("ALERT_FROM_EMAIL")
    recipient = os.getenv("ALERT_TO_EMAIL")
    use_ssl = os.getenv("SMTP_USE_SSL", "").lower() == "true"
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

    required = {
        "SMTP_HOST": smtp_host,
        "SMTP_USERNAME": smtp_username,
        "SMTP_PASSWORD": smtp_password,
        "ALERT_FROM_EMAIL": sender,
        "ALERT_TO_EMAIL": recipient,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Cannot send a stock alert email because the following environment variables "
            f"are missing: {', '.join(missing)}"
        )

    message = EmailMessage()
    message["Subject"] = (
        f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: {len(changes)} status change(s)"
    )
    message["From"] = sender
    message["To"] = recipient
    message.set_content(build_email_body(changes))

    smtp: smtplib.SMTP | smtplib.SMTP_SSL
    if use_ssl:
        smtp = smtplib.SMTP_SSL(smtp_host, smtp_port)
    else:
        smtp = smtplib.SMTP(smtp_host, smtp_port)

    with smtp:
        if not use_ssl and use_tls:
            smtp.starttls()
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def main() -> int:
    products = load_products()
    previous_state = load_state()
    next_state = dict(previous_state)
    changes: list[tuple[ProductResult, bool, bool]] = []
    errors: list[str] = []
    determined_statuses = 0

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
            current_entry = {
                "retailer": result.retailer,
                "url": result.url,
                "in_stock": result.in_stock,
            }
            previous_entry = previous_state.get(result.name)
            previous_status = (
                previous_entry.get("in_stock")
                if isinstance(previous_entry, dict) and isinstance(previous_entry.get("in_stock"), bool)
                else None
            )

            if previous_status is not None and previous_status != result.in_stock:
                changes.append((result, previous_status, result.in_stock))

            next_state[result.name] = current_entry

    if determined_statuses == 0:
        raise RuntimeError(
            "No product status could be determined. "
            "All product checks either failed or returned inconclusive results."
        )

    if changes:
        send_email(changes)

    if next_state != previous_state:
        save_state(next_state)

    if errors:
        for error in errors:
            print(f"WARNING: {error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
