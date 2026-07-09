from __future__ import annotations

import json
import os
import smtplib
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
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


@dataclass
class StatusChange:
    result: ProductResult
    previous_in_stock: bool
    current_in_stock: bool


@dataclass
class InitialStatus:
    result: ProductResult


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
    normalized_lines = [
        normalize_match_text(text)
        for text in soup.stripped_strings
        if text and text.strip()
    ]
    if not normalized_lines:
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=None,
            details="page is empty",
        )

    start_markers = [
        "ce que le moniteur portasplit observe en ce moment",
        "disponibilite en ligne",
    ]
    end_markers = [
        "alertes e-mail",
        "comment ca marche",
        "questions frequentes",
    ]

    in_availability_section = False
    section_lines: list[str] = []
    for line in normalized_lines:
        if not in_availability_section and any(marker in line for marker in start_markers):
            in_availability_section = True

        if not in_availability_section:
            continue

        if any(marker in line for marker in end_markers):
            break

        section_lines.append(line)

    if not section_lines:
        section_lines = normalized_lines

    blocked_markers = ["acces interdit", "access denied"]
    if any(marker in line for line in section_lines for marker in blocked_markers):
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=None,
            details="access to climradar page was blocked",
        )

    status_lines = [
        line
        for line in section_lines
        if "en stock" in line or "stock faible" in line or "rupture" in line
    ]

    if not status_lines:
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=None,
            details="no climradar status markers found",
        )

    has_positive = any("en stock" in line or "stock faible" in line for line in status_lines)
    has_rupture = any("rupture" in line for line in status_lines)

    if has_positive:
        details = "climradar reports at least one entry as en stock/stock faible"
        if has_rupture:
            details += " (other entries may still be rupture)"
        return ProductResult(
            name=product["name"],
            retailer=product["retailer"],
            url=product["url"],
            in_stock=True,
            details=details,
        )

    return ProductResult(
        name=product["name"],
        retailer=product["retailer"],
        url=product["url"],
        in_stock=False,
        details="climradar reports only rupture entries",
    )


def fetch_product_status(session: requests.Session, product: dict[str, Any]) -> ProductResult:
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
    if "climradar.fr" in hostname.lower():
        return detect_climradar_status(soup, product)

    page_text = soup.get_text(" ", strip=True)
    return detect_stock_status(page_text, product)


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
    send_email_message(subject=subject, body=body)


def send_recovery_email() -> None:
    subject = f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: stock check recovered"
    body = "Stock check recovered: at least one product status was determined in this run."
    send_email_message(subject=subject, body=body)


def send_webhook_message(subject: str, body: str) -> bool:
    webhook_url = os.getenv("NOTIFICATION_WEBHOOK_URL")
    if not webhook_url:
        return False

    timeout = int(os.getenv("NOTIFICATION_WEBHOOK_TIMEOUT", "15"))
    payload = {
        "subject": subject,
        "message": body,
        "text": f"{subject}\n\n{body}",
        "content": f"{subject}\n\n{body}",
    }
    headers = {
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        print(
            "WARNING: Failed to send webhook notification. "
            f"Check NOTIFICATION_WEBHOOK_URL and endpoint availability. Error: {error}"
        )
        return False


def send_email_message(subject: str, body: str) -> None:
    webhook_sent = send_webhook_message(subject=subject, body=body)

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

    if not smtp_host:
        if not webhook_sent:
            print(
                "WARNING: No notification channel configured. "
                "Set SMTP_* + ALERT_* variables or NOTIFICATION_WEBHOOK_URL."
            )
        return

    missing = [name for name, value in required.items() if not value]
    if missing:
        print(
            "WARNING: SMTP is configured but missing required environment "
            f"variables are missing: {', '.join(missing)}"
        )
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as smtp:
                smtp.login(smtp_username, smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as smtp:
                if use_tls:
                    smtp.starttls()
                smtp.login(smtp_username, smtp_password)
                smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        print(
            "WARNING: Failed to send stock alert email. "
            f"Check SMTP_* settings and server availability. Error: {error}"
        )


def send_email(changes: list[StatusChange]) -> None:
    subject = f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: {len(changes)} status change(s)"
    send_email_message(subject=subject, body=build_email_body(changes))


def send_initial_email(statuses: list[InitialStatus]) -> None:
    subject = (
        f"{os.getenv('EMAIL_SUBJECT_PREFIX', 'Stock alert')}: "
        f"initial status for {len(statuses)} product(s)"
    )
    send_email_message(subject=subject, body=build_initial_email_body(statuses))


def main() -> int:
    products = load_products()
    state_data = load_state()
    previous_state = state_data[STATE_PRODUCTS_KEY]
    is_first_run = not bool(state_data[STATE_INITIALIZED_KEY])
    next_state = dict(previous_state)
    changes: list[StatusChange] = []
    initial_statuses: list[InitialStatus] = []
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
                changes.append(
                    StatusChange(
                        result=result,
                        previous_in_stock=previous_status,
                        current_in_stock=result.in_stock,
                    )
                )
            elif is_first_run:
                initial_statuses.append(InitialStatus(result=result))

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

        alert_on_inconclusive = os.getenv("ALERT_ON_INCONCLUSIVE", "true").lower() == "true"
        alert_every_run = os.getenv("ALERT_ON_INCONCLUSIVE_EVERY_RUN", "false").lower() == "true"
        if alert_on_inconclusive and (alert_every_run or not previous_inconclusive):
            send_inconclusive_email(
                products=products,
                errors=errors,
                previous_inconclusive=previous_inconclusive,
            )

        save_state(
            next_state,
            initialized=bool(state_data[STATE_INITIALIZED_KEY]),
            last_run_inconclusive=True,
        )
        return 0

    alert_on_recovery = os.getenv("ALERT_ON_RECOVERY", "false").lower() == "true"
    if previous_inconclusive and alert_on_recovery:
        send_recovery_email()

    if is_first_run and initial_statuses:
        send_initial_email(initial_statuses)
    elif changes:
        send_email(changes)

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
