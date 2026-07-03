#!/usr/bin/env python3.14
"""Lower active fixed-price eBay listing prices by a percentage."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import TextIO


EBAY_NS = "urn:ebay:apis:eBLBaseComponents"
NS = {"e": EBAY_NS}
DEFAULT_COMPATIBILITY_LEVEL = "1455"
DEFAULT_ENTRIES_PER_PAGE = 200
DEFAULT_ENV_FILE = ".env"
ZERO_DECIMAL_CURRENCIES = {
    "BIF",
    "CLP",
    "DJF",
    "GNF",
    "ISK",
    "JPY",
    "KMF",
    "KRW",
    "PYG",
    "RWF",
    "UGX",
    "VND",
    "VUV",
    "XAF",
    "XOF",
    "XPF",
}

ET.register_namespace("", EBAY_NS)


@dataclass(frozen=True)
class PriceChange:
    item_id: str
    listing_name: str
    url: str
    previous_price: Decimal
    new_price: Decimal
    currency: str
    sku: str | None = None

    @property
    def delta(self) -> Decimal:
        return self.new_price - self.previous_price


class EbayApiError(RuntimeError):
    pass


class EbayTradingClient:
    def __init__(
        self,
        access_token: str,
        sandbox: bool,
        site_id: str,
        compatibility_level: str,
        timeout: float,
    ) -> None:
        self.access_token = access_token
        self.sandbox = sandbox
        self.site_id = site_id
        self.compatibility_level = compatibility_level
        self.timeout = timeout
        self.endpoint = (
            "https://api.sandbox.ebay.com/ws/api.dll"
            if sandbox
            else "https://api.ebay.com/ws/api.dll"
        )

    def call(self, call_name: str, root: ET.Element) -> ET.Element:
        body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "X-EBAY-API-CALL-NAME": call_name,
                "X-EBAY-API-COMPATIBILITY-LEVEL": self.compatibility_level,
                "X-EBAY-API-SITEID": self.site_id,
                "X-EBAY-API-IAF-TOKEN": self.access_token,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise EbayApiError(f"{call_name} HTTP {exc.code}: {response_body}") from exc
        except urllib.error.URLError as exc:
            raise EbayApiError(f"{call_name} request failed: {exc.reason}") from exc

        try:
            response_root = ET.fromstring(response_body)
        except ET.ParseError as exc:
            snippet = response_body.decode("utf-8", errors="replace")[:1000]
            raise EbayApiError(f"{call_name} returned invalid XML: {snippet}") from exc

        ack = find_text(response_root, "Ack", "")
        if ack not in {"Success", "Warning"}:
            raise EbayApiError(f"{call_name} failed: {format_errors(response_root)}")

        warnings = [
            format_error(error)
            for error in response_root.findall("e:Errors", NS)
            if find_text(error, "SeverityCode", "") == "Warning"
        ]
        for warning in warnings:
            print(f"Warning from {call_name}: {warning}", file=sys.stderr)

        return response_root


def q(name: str) -> str:
    return f"{{{EBAY_NS}}}{name}"


def ns_path(path: str) -> str:
    return "/".join(f"e:{part}" for part in path.split("/"))


def find(element: ET.Element, path: str) -> ET.Element | None:
    return element.find(ns_path(path), NS)


def find_text(element: ET.Element, path: str, default: str | None = None) -> str | None:
    found = find(element, path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def parse_decimal(value: str, field_name: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{field_name} must be a number") from exc


def parse_percent(value: str) -> Decimal:
    percent = parse_decimal(value, "percent")
    if percent <= 0 or percent >= 100:
        raise argparse.ArgumentTypeError("percent must be greater than 0 and less than 100")
    return percent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lower all active fixed-price eBay listings by a percentage.",
    )
    parser.add_argument(
        "--percent",
        required=True,
        type=parse_percent,
        help="Percentage to lower prices by, for example 10 for 10%%.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually revise eBay prices. Without this, the script only outputs a dry run.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output path. Defaults to stdout.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(DEFAULT_ENV_FILE),
        help=f"Credential/config file. Defaults to {DEFAULT_ENV_FILE}.",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Write CSV instead of the default tab-separated text report.",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Use eBay Sandbox endpoints.",
    )
    parser.add_argument(
        "--site-id",
        help="eBay Trading API site ID. Defaults to EBAY_SITE_ID in .env or 0.",
    )
    parser.add_argument(
        "--compatibility-level",
        default=None,
        help=f"Trading API compatibility level. Defaults to {DEFAULT_COMPATIBILITY_LEVEL}.",
    )
    parser.add_argument(
        "--entries-per-page",
        type=int,
        default=DEFAULT_ENTRIES_PER_PAGE,
        help=f"Active listing page size. Defaults to {DEFAULT_ENTRIES_PER_PAGE}.",
    )
    parser.add_argument(
        "--price-decimals",
        type=int,
        choices=range(0, 5),
        metavar="0-4",
        help="Override decimal places used when rounding new prices.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds. Defaults to 30.",
    )
    return parser.parse_args()


def valid_env_key(key: str) -> bool:
    if not key or not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in key)


def parse_dotenv_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"Could not read {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise SystemExit(f"{path}:{line_number}: expected KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        if not valid_env_key(key):
            raise SystemExit(f"{path}:{line_number}: invalid key {key!r}")
        values[key] = parse_dotenv_value(value)

    return values


def config_value(config: dict[str, str], key: str, default: str | None = None) -> str | None:
    value = config.get(key)
    if value is None or value == "":
        return default
    return value


def token_endpoint(sandbox: bool) -> str:
    return (
        "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
        if sandbox
        else "https://api.ebay.com/identity/v1/oauth2/token"
    )


def oauth_basic_auth_header(client_id: str, client_secret: str) -> str:
    credentials = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")


def request_oauth_token(
    data: dict[str, str],
    client_id: str,
    client_secret: str,
    sandbox: bool,
    timeout: float,
    action: str,
) -> dict[str, object]:
    request = urllib.request.Request(
        token_endpoint(sandbox),
        data=urllib.parse.urlencode(data).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": oauth_basic_auth_header(client_id, client_secret),
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise EbayApiError(f"{action} failed with HTTP {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise EbayApiError(f"{action} failed: {exc.reason}") from exc


def get_access_token(config: dict[str, str], config_path: Path, sandbox: bool, timeout: float) -> str:
    access_token = config_value(config, "EBAY_OAUTH_ACCESS_TOKEN")
    if access_token:
        return access_token

    client_id = config_value(config, "EBAY_CLIENT_ID")
    client_secret = config_value(config, "EBAY_CLIENT_SECRET")
    refresh_token = config_value(config, "EBAY_REFRESH_TOKEN")
    missing = [
        name
        for name, value in (
            ("EBAY_CLIENT_ID", client_id),
            ("EBAY_CLIENT_SECRET", client_secret),
            ("EBAY_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"Missing credentials in {config_path}. Copy .env.example to {config_path}, then set "
            "EBAY_OAUTH_ACCESS_TOKEN, or set "
            + ", ".join(missing)
            + "."
        )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    scopes = config_value(config, "EBAY_OAUTH_SCOPES")
    if scopes:
        data["scope"] = scopes

    payload = request_oauth_token(
        data=data,
        client_id=client_id,
        client_secret=client_secret,
        sandbox=sandbox,
        timeout=timeout,
        action="OAuth refresh",
    )

    try:
        access_token = payload["access_token"]
    except KeyError as exc:
        raise EbayApiError(f"OAuth refresh response did not include access_token: {payload}") from exc
    if not isinstance(access_token, str):
        raise EbayApiError(f"OAuth refresh response did not include access_token: {payload}")
    return access_token


def build_get_active_request(page_number: int, entries_per_page: int) -> ET.Element:
    root = ET.Element(q("GetMyeBaySellingRequest"))
    active_list = ET.SubElement(root, q("ActiveList"))
    ET.SubElement(active_list, q("Include")).text = "true"
    ET.SubElement(active_list, q("ListingType")).text = "FixedPriceItem"
    pagination = ET.SubElement(active_list, q("Pagination"))
    ET.SubElement(pagination, q("EntriesPerPage")).text = str(entries_per_page)
    ET.SubElement(pagination, q("PageNumber")).text = str(page_number)
    ET.SubElement(root, q("HideVariations")).text = "false"
    ET.SubElement(root, q("DetailLevel")).text = "ReturnAll"
    return root


def active_items(
    client: EbayTradingClient,
    entries_per_page: int,
) -> list[ET.Element]:
    page_number = 1
    total_pages = 1
    items: list[ET.Element] = []

    while page_number <= total_pages:
        response = client.call(
            "GetMyeBaySelling",
            build_get_active_request(page_number, entries_per_page),
        )
        active_list = find(response, "ActiveList")
        if active_list is None:
            return items

        for item in active_list.findall("e:ItemArray/e:Item", NS):
            items.append(item)

        total_pages_text = find_text(active_list, "PaginationResult/TotalNumberOfPages", "1")
        try:
            total_pages = max(1, int(total_pages_text or "1"))
        except ValueError:
            total_pages = 1
        page_number += 1

    return items


def price_from(element: ET.Element | None) -> tuple[Decimal, str] | None:
    if element is None or element.text is None:
        return None
    try:
        amount = Decimal(element.text.strip())
    except InvalidOperation:
        return None
    return amount, element.attrib.get("currencyID", "")


def currency_decimals(currency: str, override: int | None) -> int:
    if override is not None:
        return override
    return 0 if currency.upper() in ZERO_DECIMAL_CURRENCIES else 2


def quantizer(decimals: int) -> Decimal:
    return Decimal("1").scaleb(-decimals)


def lowered_price(previous_price: Decimal, percent: Decimal, decimals: int) -> Decimal:
    factor = Decimal("1") - (percent / Decimal("100"))
    new_price = (previous_price * factor).quantize(
        quantizer(decimals),
        rounding=ROUND_HALF_UP,
    )
    minimum_price = quantizer(decimals)
    if new_price < minimum_price:
        return minimum_price
    return new_price


def listing_url(item: ET.Element, item_id: str) -> str:
    return (
        find_text(item, "ListingDetails/ViewItemURL")
        or find_text(item, "ListingDetails/ViewItemURLForNaturalSearch")
        or f"https://www.ebay.com/itm/{item_id}"
    )


def variation_label(variation: ET.Element) -> str:
    title = find_text(variation, "VariationTitle")
    if title:
        return title

    parts: list[str] = []
    for name_value in variation.findall("e:VariationSpecifics/e:NameValueList", NS):
        name = find_text(name_value, "Name", "")
        values = [
            value.text.strip()
            for value in name_value.findall("e:Value", NS)
            if value.text and value.text.strip()
        ]
        if name and values:
            parts.append(f"{name}={','.join(values)}")
    return ", ".join(parts)


def build_price_changes(
    item: ET.Element,
    percent: Decimal,
    price_decimals: int | None,
) -> list[PriceChange]:
    item_id = find_text(item, "ItemID")
    if not item_id:
        print("Skipping listing without ItemID.", file=sys.stderr)
        return []

    title = find_text(item, "Title", f"Item {item_id}") or f"Item {item_id}"
    url = listing_url(item, item_id)
    variations = item.findall("e:Variations/e:Variation", NS)
    changes: list[PriceChange] = []

    if variations:
        for variation in variations:
            sku = find_text(variation, "SKU")
            if not sku:
                print(
                    f"Skipping variation for item {item_id}; ReviseInventoryStatus requires a SKU.",
                    file=sys.stderr,
                )
                continue

            price = price_from(find(variation, "StartPrice")) or price_from(
                find(variation, "SellingStatus/CurrentPrice")
            )
            if not price:
                print(f"Skipping variation {sku} for item {item_id}; no price found.", file=sys.stderr)
                continue

            previous, currency = price
            decimals = currency_decimals(currency, price_decimals)
            label = variation_label(variation)
            display_name = f"{title} [{label}]" if label else title
            changes.append(
                PriceChange(
                    item_id=item_id,
                    listing_name=display_name,
                    url=url,
                    previous_price=previous,
                    new_price=lowered_price(previous, percent, decimals),
                    currency=currency,
                    sku=sku,
                )
            )
        return changes

    price = price_from(find(item, "SellingStatus/CurrentPrice"))
    if not price:
        print(f"Skipping item {item_id}; no current price found.", file=sys.stderr)
        return []

    previous, currency = price
    decimals = currency_decimals(currency, price_decimals)
    changes.append(
        PriceChange(
            item_id=item_id,
            listing_name=title,
            url=url,
            previous_price=previous,
            new_price=lowered_price(previous, percent, decimals),
            currency=currency,
            sku=find_text(item, "SKU"),
        )
    )
    return changes


def build_revise_request(change: PriceChange) -> ET.Element:
    root = ET.Element(q("ReviseInventoryStatusRequest"))
    inventory_status = ET.SubElement(root, q("InventoryStatus"))
    ET.SubElement(inventory_status, q("ItemID")).text = change.item_id
    if change.sku:
        ET.SubElement(inventory_status, q("SKU")).text = change.sku
    start_price = ET.SubElement(inventory_status, q("StartPrice"))
    if change.currency:
        start_price.set("currencyID", change.currency)
    start_price.text = plain_decimal(change.new_price)
    return root


def format_error(error: ET.Element) -> str:
    code = find_text(error, "ErrorCode", "")
    severity = find_text(error, "SeverityCode", "")
    short = find_text(error, "ShortMessage", "")
    long = find_text(error, "LongMessage", "")
    pieces = [piece for piece in (severity, code, short, long) if piece]
    return " | ".join(pieces) if pieces else ET.tostring(error, encoding="unicode")


def format_errors(root: ET.Element) -> str:
    errors = [format_error(error) for error in root.findall("e:Errors", NS)]
    return "; ".join(errors) if errors else "unknown eBay API error"


def plain_decimal(value: Decimal) -> str:
    return format(value, "f")


def format_money(value: Decimal, currency: str, price_decimals: int | None) -> str:
    decimals = currency_decimals(currency, price_decimals)
    rounded = value.quantize(quantizer(decimals), rounding=ROUND_HALF_UP)
    amount = plain_decimal(rounded)
    return f"{amount} {currency}".strip()


def open_output(path: Path | None) -> tuple[TextIO, bool]:
    if path is None:
        return sys.stdout, False
    return path.open("w", newline="", encoding="utf-8"), True


OUTPUT_HEADERS = ["Listing name", "URL", "Previous price", "New price", "Delta"]


def output_values(change: PriceChange, price_decimals: int | None) -> list[str]:
    return [
        change.listing_name,
        change.url,
        format_money(change.previous_price, change.currency, price_decimals),
        format_money(change.new_price, change.currency, price_decimals),
        format_money(change.delta, change.currency, price_decimals),
    ]


def text_cell(value: str) -> str:
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def write_header(output_file: TextIO, csv_writer: csv.writer | None) -> None:
    if csv_writer:
        csv_writer.writerow(OUTPUT_HEADERS)
        return
    output_file.write("\t".join(OUTPUT_HEADERS) + "\n")


def write_row(
    output_file: TextIO,
    csv_writer: csv.writer | None,
    change: PriceChange,
    price_decimals: int | None,
) -> None:
    values = output_values(change, price_decimals)
    if csv_writer:
        csv_writer.writerow(values)
        return
    output_file.write("\t".join(text_cell(value) for value in values) + "\n")


def main() -> int:
    args = parse_args()
    config = load_dotenv(args.env_file)
    sandbox = args.sandbox or config_value(config, "EBAY_ENV", "").lower() == "sandbox"

    site_id = args.site_id or config_value(config, "EBAY_SITE_ID", "0")
    compatibility_level = (
        args.compatibility_level
        or config_value(config, "EBAY_COMPAT_LEVEL", DEFAULT_COMPATIBILITY_LEVEL)
    )

    if not 1 <= args.entries_per_page <= DEFAULT_ENTRIES_PER_PAGE:
        print(f"--entries-per-page must be between 1 and {DEFAULT_ENTRIES_PER_PAGE}.", file=sys.stderr)
        return 2

    try:
        access_token = get_access_token(
            config=config,
            config_path=args.env_file,
            sandbox=sandbox,
            timeout=args.timeout,
        )
        client = EbayTradingClient(
            access_token=access_token,
            sandbox=sandbox,
            site_id=str(site_id),
            compatibility_level=str(compatibility_level),
            timeout=args.timeout,
        )
        items = active_items(client, args.entries_per_page)
        changes = [
            change
            for item in items
            for change in build_price_changes(item, args.percent, args.price_decimals)
        ]
    except EbayApiError as exc:
        print(exc, file=sys.stderr)
        return 1

    output_file, should_close = open_output(args.output)
    failures = 0
    unchanged = 0
    try:
        print(f"Run time: {datetime.now().astimezone().isoformat(timespec='seconds')}")
        csv_writer = csv.writer(output_file) if args.csv else None
        write_header(output_file, csv_writer)

        for change in changes:
            if change.previous_price == change.new_price:
                unchanged += 1
                write_row(output_file, csv_writer, change, args.price_decimals)
                continue

            if args.apply:
                try:
                    client.call("ReviseInventoryStatus", build_revise_request(change))
                except EbayApiError as exc:
                    failures += 1
                    print(f"Failed to update {change.item_id}: {exc}", file=sys.stderr)
                    continue

            write_row(output_file, csv_writer, change, args.price_decimals)
    finally:
        if should_close:
            output_file.close()

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(
        f"{mode}: {len(changes) - failures} price row(s) output, "
        f"{failures} failed, {unchanged} unchanged after rounding.",
        file=sys.stderr,
    )
    if not args.apply:
        print("No eBay prices were changed. Re-run with --apply to update listings.", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
