from __future__ import annotations

from datetime import datetime

from parser import (
    ALERT_METRIC_DISPLAY_NAMES,
    YOY_METRIC_DISPLAY_NAMES,
    AlertMetricValues,
    YoyMetricValues,
    calculate_yoy_alert_change,
)


def format_currency_cr(value: float) -> str:
    return f"Rs. {value:,.2f} Cr"


def format_percent(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.1f}%"


def format_metric_value(metric: str, value: float | None) -> str:
    if value is None:
        return "N/A"
    if metric == "sales":
        return format_currency_cr(value)
    return f"{value:.2f}%"


def format_yoy_metric_lines(metric: str, yoy_metrics: dict[str, YoyMetricValues]) -> list[str]:
    values = yoy_metrics.get(metric)
    if values is None:
        return []

    change = calculate_yoy_alert_change(metric, yoy_metrics)
    if change is None or abs(change) < 20:
        return []

    return [
        f"  {YOY_METRIC_DISPLAY_NAMES[metric]}:",
        f"    Change              : {format_percent(change)}",
    ]


def format_combined_alert(
    company_name: str,
    alert_metrics: dict[str, AlertMetricValues],
    yoy_metrics: dict[str, YoyMetricValues] | None = None,
) -> str:
    detected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "\u26a0\ufe0f  ALERT DETECTED",
        f"Company     : {company_name}",
    ]

    for metric in ("sales", "pat_margin_pct", "ebitda_margin_pct"):
        values = alert_metrics[metric]
        if not values.triggered:
            continue
        lines.extend(
            (
                "",
                f"{ALERT_METRIC_DISPLAY_NAMES[metric]}:",
                f"  Change    : {format_percent(values.change)}",
                f"  Direction : {values.direction or '-'}",
            )
        )

    if yoy_metrics:
        yoy_lines: list[str] = []
        for metric in ("sales", "op_profit", "net_profit"):
            yoy_lines.extend(format_yoy_metric_lines(metric, yoy_metrics))
        if yoy_lines:
            lines.extend(("", "Latest Results YOY:"))
            lines.extend(yoy_lines)

    lines.append(f"Detected At : {detected_at}")
    return "\n".join(lines)


def print_combined_alert(
    company_name: str,
    alert_metrics: dict[str, AlertMetricValues],
    yoy_metrics: dict[str, YoyMetricValues] | None = None,
) -> str:
    message = format_combined_alert(company_name, alert_metrics, yoy_metrics)
    print(message, flush=True)
    print("", flush=True)
    return message
