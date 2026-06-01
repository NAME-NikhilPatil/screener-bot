from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)

METRIC_LABELS = {
    "sales": ("sales", "revenue from operations", "revenue"),
    "op_profit": ("operating profit", "ebitda", "ebidt"),
    "net_profit": ("net profit", "profit after tax", "pat"),
}

METRIC_DISPLAY_NAMES = {
    "sales": "Sales",
    "op_profit": "Operating Profit",
    "net_profit": "Net Profit",
}

ALERT_METRIC_DISPLAY_NAMES = {
    "sales": "Sales",
    "pat_margin_pct": "PAT Margin %",
    "ebitda_margin_pct": "EBITDA Margin %",
}

YOY_METRIC_DISPLAY_NAMES = {
    "sales": "Sales YOY",
    "op_profit": "EBIDTA YOY",
    "net_profit": "PAT YOY",
}

MIN_MARKET_CAP_CR = 200.0


@dataclass(frozen=True)
class MetricQuarterValues:
    previous: float
    current: float


@dataclass(frozen=True)
class AlertMetricValues:
    previous: float | None
    current: float | None
    change: float | None
    direction: str | None
    triggered: bool


@dataclass(frozen=True)
class YoyMetricValues:
    previous: float | None
    current: float | None
    change: float | None


def parse_company_id(url: str) -> str:
    match = re.search(r"/company/([^/?#]+)/?", url)
    if not match:
        raise ValueError(f"Could not parse company id from URL: {url}")
    return match.group(1)


def parse_latest_results_companies(
    html: str,
    base_url: str = "https://www.screener.in",
    min_market_cap_cr: float = MIN_MARKET_CAP_CR,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    companies: list[dict] = []
    seen_ids: set[str] = set()

    for link in soup.select('a[href*="/company/"]'):
        href = link.get("href", "").strip()
        name = " ".join(link.get_text(" ", strip=True).split())
        if not href or not name or "/company/" not in href:
            continue

        url = href if href.startswith("http") else f"{base_url}{href}"
        company_id = parse_company_id(url)
        if company_id in seen_ids:
            continue

        market_cap_cr = parse_market_cap_cr(_nearby_market_cap_text(link))
        if market_cap_cr is None or market_cap_cr < min_market_cap_cr:
            continue

        seen_ids.add(company_id)
        companies.append(
            {
                "id": company_id,
                "name": name,
                "url": url.split("#")[0] + "#quarters",
                "market_cap_cr": market_cap_cr,
                "yoy": parse_latest_results_yoy_table(link.find_next("table")),
            }
        )

    return companies


def parse_latest_results_yoy_table(table) -> dict[str, YoyMetricValues]:
    yoy_values: dict[str, YoyMetricValues] = {}
    if not table:
        return yoy_values

    for row in table.select("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 4:
            continue

        label = _normalize_label(cells[0].get_text(" ", strip=True))
        metric_key = _metric_key_for_label(label)
        if metric_key not in {"sales", "op_profit", "net_profit"}:
            continue

        current = parse_number(cells[2].get_text(" ", strip=True))
        previous = parse_number(cells[-1].get_text(" ", strip=True))
        yoy = calculate_percentage_change(current, previous)
        if yoy is None:
            yoy = parse_yoy_percent(cells[1].get_text(" ", strip=True))
        yoy_values[metric_key] = YoyMetricValues(previous=previous, current=current, change=yoy)

    return yoy_values


def parse_quarterly_metrics(html: str) -> dict[str, MetricQuarterValues]:
    soup = BeautifulSoup(html, "html.parser")
    quarters = soup.select_one("#quarters")
    if not quarters:
        raise ValueError("#quarters section is missing")

    table = quarters.select_one("table")
    if not table:
        raise ValueError("Quarterly results table is missing")

    headers = _extract_headers(table)
    if len(headers) < 2:
        raise ValueError("Quarterly results table does not contain two quarter columns")

    results: dict[str, MetricQuarterValues] = {}
    for row in table.select("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        label = _normalize_label(cells[0].get_text(" ", strip=True))
        metric_key = _metric_key_for_label(label)
        if not metric_key:
            continue

        numeric_cells = cells[1:]
        if len(numeric_cells) < 2:
            raise ValueError(f"Metric row {label!r} does not contain two quarters")

        previous = parse_number(numeric_cells[-2].get_text(" ", strip=True))
        current = parse_number(numeric_cells[-1].get_text(" ", strip=True))
        if previous is None or current is None:
            raise ValueError(f"Metric row {label!r} contains non-numeric latest values")

        results[metric_key] = MetricQuarterValues(previous=previous, current=current)

    missing = [name for name in METRIC_LABELS if name not in results]
    if missing:
        missing_display = ", ".join(METRIC_DISPLAY_NAMES[name] for name in missing)
        raise ValueError(f"Quarterly results table missing metrics: {missing_display}")

    return results


def calculate_percentage_change(current: float, previous: float) -> float | None:
    if previous is None or previous == 0:
        return None
    return ((current - previous) / abs(previous)) * 100


def parse_number(value: str) -> float | None:
    cleaned = (
        value.replace(",", "")
        .replace("\u20b9", "")
        .replace("Rs.", "")
        .replace("Rs", "")
        .replace("%", "")
        .strip()
    )
    if not cleaned or cleaned in {"-", "--"}:
        return None

    bracketed = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None

    number = float(match.group(0))
    return -abs(number) if bracketed else number


def parse_yoy_percent(value: str) -> float | None:
    number = parse_number(value)
    if number is None:
        return None

    lowered = value.lower()
    if "\u2193" in value or "down" in lowered or "fall" in lowered:
        return -abs(number)
    if "\u2191" in value or "up" in lowered or "jump" in lowered:
        return abs(number)
    return number


def parse_market_cap_cr(value: str) -> float | None:
    match = re.search(
        r"M\.?\s*Cap\s*(?:\u20b9|Rs\.?|INR)?\s*([0-9,.]+)\s*(Cr|Crore|Lac|Lakh)?",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    amount = parse_number(match.group(1))
    if amount is None:
        return None

    unit = (match.group(2) or "Cr").lower()
    if unit in {"lac", "lakh"}:
        return amount / 100
    return amount


def _nearby_market_cap_text(link) -> str:
    for parent in link.parents:
        if parent.name in {"body", "html"}:
            break

        text = parent.get_text(" ", strip=True)
        if "M.Cap" in text or "M Cap" in text:
            return text

    return link.get_text(" ", strip=True)


def _extract_headers(table) -> list[str]:
    header_row = table.select_one("thead tr") or table.select_one("tr")
    if not header_row:
        return []
    return [cell.get_text(" ", strip=True) for cell in header_row.find_all(["th", "td"])[1:]]


def _metric_key_for_label(label: str) -> str | None:
    for metric_key, aliases in METRIC_LABELS.items():
        if any(label == alias or label.startswith(f"{alias} ") for alias in aliases):
            return metric_key
    return None


def _normalize_label(label: str) -> str:
    label = label.replace("+", " ")
    label = re.sub(r"\s+", " ", label)
    return label.strip().lower()


def build_alert_metrics(
    metrics: dict[str, MetricQuarterValues],
    threshold: float = 20.0,
) -> dict[str, AlertMetricValues]:
    sales = metrics["sales"]
    net_profit = metrics["net_profit"]
    op_profit = metrics["op_profit"]

    derived_values = {
        "sales": MetricQuarterValues(previous=sales.previous, current=sales.current),
        "pat_margin_pct": MetricQuarterValues(
            previous=calculate_margin_pct(net_profit.previous, sales.previous),
            current=calculate_margin_pct(net_profit.current, sales.current),
        ),
        "ebitda_margin_pct": MetricQuarterValues(
            previous=calculate_margin_pct(op_profit.previous, sales.previous),
            current=calculate_margin_pct(op_profit.current, sales.current),
        ),
    }

    alert_metrics: dict[str, AlertMetricValues] = {}
    for metric, values in derived_values.items():
        change = calculate_percentage_change(values.current, values.previous)
        if change is None:
            LOGGER.warning("Skipping %s because previous value is zero or missing", ALERT_METRIC_DISPLAY_NAMES[metric])
            alert_metrics[metric] = AlertMetricValues(
                previous=values.previous,
                current=values.current,
                change=None,
                direction=None,
                triggered=False,
            )
            continue

        direction = direction_for_change(change)
        triggered = abs(change) >= threshold

        alert_metrics[metric] = AlertMetricValues(
            previous=values.previous,
            current=values.current,
            change=change,
            direction=direction,
            triggered=triggered,
        )

    return alert_metrics


def company_alert_needed(alert_metrics: dict[str, AlertMetricValues], state_values: dict[str, float] | None = None) -> bool:
    if not any(values.triggered for values in alert_metrics.values()):
        return False

    if not state_values:
        return True

    normalized_state = normalize_state_values(state_values)
    for metric, values in alert_metrics.items():
        if values.current is None:
            continue
        if normalized_state.get(metric) != values.current:
            return True

    return False


def company_state_changed(alert_metrics: dict[str, AlertMetricValues], state_values: dict[str, float] | None = None) -> bool:
    if not state_values:
        return True

    normalized_state = normalize_state_values(state_values)
    for metric, values in latest_state_values(alert_metrics).items():
        if normalized_state.get(metric) != values:
            return True

    return False


def yoy_alert_needed(yoy_metrics: dict[str, YoyMetricValues] | None, threshold: float = 20.0) -> bool:
    if not yoy_metrics:
        return False

    for metric in ("sales", "op_profit", "net_profit"):
        change = calculate_yoy_alert_change(metric, yoy_metrics)
        if change is not None and abs(change) >= threshold:
            return True

    return False


def calculate_yoy_alert_change(metric: str, yoy_metrics: dict[str, YoyMetricValues]) -> float | None:
    values = yoy_metrics.get(metric)
    if values is None:
        return None

    if metric == "sales":
        return values.change

    sales_values = yoy_metrics.get("sales")
    previous_margin = calculate_margin_pct(
        values.previous,
        sales_values.previous if sales_values else None,
    )
    current_margin = calculate_margin_pct(
        values.current,
        sales_values.current if sales_values else None,
    )
    return calculate_percentage_change(current_margin, previous_margin)


def latest_state_values(alert_metrics: dict[str, AlertMetricValues]) -> dict[str, float]:
    return {metric: values.current for metric, values in alert_metrics.items() if values.current is not None}


def normalize_state_values(state_values: dict[str, float]) -> dict[str, float]:
    if "pat_margin_pct" in state_values or "ebitda_margin_pct" in state_values:
        return state_values

    sales = state_values.get("sales")
    net_profit = state_values.get("net_profit")
    op_profit = state_values.get("op_profit")
    normalized: dict[str, float] = {}
    if sales is not None:
        normalized["sales"] = sales

    pat_margin = calculate_margin_pct(net_profit, sales)
    if pat_margin is not None:
        normalized["pat_margin_pct"] = pat_margin

    ebitda_margin = calculate_margin_pct(op_profit, sales)
    if ebitda_margin is not None:
        normalized["ebitda_margin_pct"] = ebitda_margin

    return normalized


def calculate_margin_pct(numerator: float | None, revenue: float | None) -> float | None:
    if numerator is None or revenue is None or revenue == 0:
        return None
    return (numerator / revenue) * 100


def direction_for_change(change: float | None) -> str | None:
    if change is None:
        return None
    if change > 0:
        return "JUMP"
    if change < 0:
        return "FALL"
    return "FLAT"
