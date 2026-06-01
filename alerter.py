from __future__ import annotations

from datetime import datetime

from parser import (
    ALERT_METRIC_DISPLAY_NAMES,
    YOY_METRIC_DISPLAY_NAMES,
    AlertMetricValues,
    YoyMetricValues,
    calculate_margin_pct,
    calculate_percentage_change,
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

    if metric == "sales":
        return [
            f"  {YOY_METRIC_DISPLAY_NAMES[metric]}:",
            f"    Previous Yr: {format_currency_cr(values.previous)}",
            f"    Current Yr : {format_currency_cr(values.current)}",
            f"    Change     : {format_percent(values.change)}",
        ]

    sales_values = yoy_metrics.get("sales")
    previous_margin = calculate_margin_pct(
        values.previous,
        sales_values.previous if sales_values else None,
    )
    current_margin = calculate_margin_pct(
        values.current,
        sales_values.current if sales_values else None,
    )
    margin_change = calculate_percentage_change(current_margin, previous_margin)
    return [
        f"  {YOY_METRIC_DISPLAY_NAMES[metric]}:",
        f"    Previous Yr Margin %: {format_metric_value('pat_margin_pct', previous_margin)}",
        f"    Current Yr Margin % : {format_metric_value('pat_margin_pct', current_margin)}",
        f"    Change              : {format_percent(margin_change)}",
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
        "Metrics     : Sales, PAT Margin %, EBITDA Margin %",
    ]

    for metric in ("sales", "pat_margin_pct", "ebitda_margin_pct"):
        values = alert_metrics[metric]
        lines.extend(
            (
                "",
                f"{ALERT_METRIC_DISPLAY_NAMES[metric]}:",
                f"  Previous Q: {format_metric_value(metric, values.previous)}",
                f"  Current Q : {format_metric_value(metric, values.current)}",
                f"  Change    : {format_percent(values.change)}",
                f"  Direction : {values.direction or '-'}",
            )
        )

    if yoy_metrics:
        lines.extend(("", "Latest Results YOY:"))
        for metric in ("sales", "op_profit", "net_profit"):
            lines.extend(format_yoy_metric_lines(metric, yoy_metrics))

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
