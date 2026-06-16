from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from alerter import print_combined_alert
from parser import (
    build_alert_metrics,
    company_alert_needed,
    latest_state_values,
    parse_company_id,
    yoy_alert_needed,
)
from scraper import launch_browser, polite_company_delay, scrape_company_metrics_with_retries, scrape_latest_companies
from storage import get_storage
from telegram_notifier import (
    TelegramNotifier,
    load_env_file,
    poll_telegram_subscribers_once,
    send_telegram_alert_to_all,
)

STATE_FILE = Path("state.json")
DEFAULT_SCAN_INTERVAL_SECONDS = 300


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)


def load_state(path: Path = STATE_FILE) -> dict[str, dict[str, float]]:
    state_name = os.getenv("STATE_BLOB_NAME", str(path))
    data = get_storage().load_json(state_name, {})
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, dict[str, float]], path: Path = STATE_FILE) -> None:
    state_name = os.getenv("STATE_BLOB_NAME", str(path))
    get_storage().save_json(state_name, state)


async def run_cycle(
    page,
    cycle_number: int,
    state: dict[str, dict[str, float]],
    company_limit: int | None = None,
    direct_company: dict[str, str] | None = None,
    login_wait_seconds: int = 0,
) -> bool:
    configure_console()
    companies = [direct_company] if direct_company else await scrape_latest_companies(page, login_wait_seconds)
    if company_limit is not None:
        companies = companies[:company_limit]

    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Cycle #{cycle_number} started at {start_time} - Found {len(companies)} companies", flush=True)

    alerts_detected = False
    for index, company in enumerate(companies, start=1):
        state_update_allowed = True
        try:
            if index > 1:
                await polite_company_delay()

            _refresh_shared_state(state)
            previous_state = state.get(company["id"])
            yoy_metrics = company.get("yoy") or {}
            alert_metrics = build_alert_metrics({})

            try:
                metrics = await scrape_company_metrics_with_retries(page, company)
                alert_metrics = build_alert_metrics(metrics)
            except Exception as exc:
                if yoy_alert_needed(yoy_metrics):
                    logging.warning(
                        "Company detail scrape failed for %s; evaluating latest-results YOY only: %s",
                        company.get("name", "unknown company"),
                        exc,
                    )
                else:
                    raise

            current_state = _latest_company_state_values(alert_metrics, yoy_metrics)
            should_alert = company_alert_needed(alert_metrics, previous_state)
            yoy_candidate = yoy_alert_needed(yoy_metrics)
            if not should_alert and yoy_candidate:
                should_alert = _company_state_changed(current_state, previous_state)
                if should_alert:
                    print(f"YOY alert candidate detected for {company['name']}", flush=True)
                else:
                    print(f"YOY alert candidate suppressed as duplicate for {company['name']}", flush=True)

            if should_alert:
                alert_message = print_combined_alert(company["name"], alert_metrics, yoy_metrics)
                sent = send_telegram_alert_to_all(alert_message)
                print(f"Telegram alert send attempted for {company['name']}: sent={sent}", flush=True)
                state_update_allowed = sent
                if not sent:
                    logging.warning(
                        "Alert detected for %s but Telegram send failed; state will not be advanced",
                        company.get("name", "unknown company"),
                    )
                alerts_detected = True

            if current_state and state_update_allowed:
                state[company["id"]] = current_state
                save_state(state)
                print(f"State saved for {company['name']}", flush=True)
        except Exception as exc:
            logging.warning("Skipping %s: %s", company.get("name", "unknown company"), exc)

    save_state(state)
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not alerts_detected:
        print(f"\u2705 Cycle #{cycle_number} complete at {end_time} - No significant changes detected", flush=True)
    else:
        print(f"Cycle #{cycle_number} complete at {end_time}", flush=True)
    return alerts_detected


async def monitor(
    headless: bool,
    once: bool,
    company_limit: int | None,
    user_data_dir: str | None,
    company_url: str | None,
    company_name: str | None,
    login_wait_seconds: int,
) -> None:
    configure_console()
    load_env_file()
    configure_logging()
    scan_interval_seconds = _scan_interval_seconds()
    startup_message = f"Screener Bot Started - Monitoring every {scan_interval_seconds} seconds"
    print(startup_message, flush=True)
    await asyncio.to_thread(poll_telegram_subscribers_once)
    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        send_telegram_alert_to_all(startup_message)
    state = load_state()
    playwright, browser, page = await launch_browser(headless=headless, user_data_dir=user_data_dir)
    direct_company = None
    if company_url:
        direct_company = {
            "id": parse_company_id(company_url),
            "name": company_name or parse_company_id(company_url),
            "url": company_url.split("#")[0] + "#quarters",
        }
    try:
        cycle = 1
        while True:
            try:
                await asyncio.to_thread(poll_telegram_subscribers_once)
                try:
                    await run_cycle(
                        page,
                        cycle,
                        state,
                        company_limit=company_limit,
                        direct_company=direct_company,
                        login_wait_seconds=login_wait_seconds,
                    )
                finally:
                    await asyncio.to_thread(poll_telegram_subscribers_once)
            except Exception:
                logging.exception("Cycle #%s failed", cycle)
                if once:
                    raise
            if once:
                break
            await sleep_with_telegram_polling(scan_interval_seconds)
            cycle += 1
    finally:
        await browser.close()
        await playwright.stop()


async def sleep_with_telegram_polling(total_seconds: int) -> None:
    remaining_seconds = max(0, int(total_seconds))
    while remaining_seconds > 0:
        sleep_seconds = min(10, remaining_seconds)
        await asyncio.sleep(sleep_seconds)
        remaining_seconds -= sleep_seconds
        await asyncio.to_thread(poll_telegram_subscribers_once)


def _scan_interval_seconds() -> int:
    try:
        value = int(os.getenv("SCAN_INTERVAL_SECONDS", str(DEFAULT_SCAN_INTERVAL_SECONDS)))
    except ValueError:
        return DEFAULT_SCAN_INTERVAL_SECONDS
    return max(1, value)


def _refresh_shared_state(state: dict[str, dict[str, float]]) -> None:
    if os.getenv("STATE_BACKEND", "").strip().lower() != "blob":
        return

    latest_state = load_state()
    if latest_state:
        state.update(latest_state)


def _company_state_changed(current_state_values: dict[str, float], state_values: dict[str, float] | None = None) -> bool:
    if not state_values:
        return True

    normalized_state = _normalize_state_values(state_values)
    for metric, value in current_state_values.items():
        if normalized_state.get(metric) != value:
            return True
    return False


def _latest_company_state_values(alert_metrics, yoy_metrics=None) -> dict[str, float]:
    values = latest_state_values(alert_metrics)
    if not yoy_metrics:
        return values

    for metric, yoy_values in yoy_metrics.items():
        if yoy_values.current is not None:
            values[f"yoy_{metric}_current"] = yoy_values.current
        if yoy_values.change is not None:
            values[f"yoy_{metric}_change"] = yoy_values.change
    return values


def _normalize_state_values(state_values: dict[str, float]) -> dict[str, float]:
    if (
        "pat_margin_pct" in state_values
        or "ebitda_margin_pct" in state_values
        or any(key.startswith("yoy_") for key in state_values)
    ):
        return state_values

    sales = state_values.get("sales")
    normalized: dict[str, float] = {}
    if sales is not None:
        normalized["sales"] = sales

    pat_margin = _margin_pct(state_values.get("net_profit"), sales)
    if pat_margin is not None:
        normalized["pat_margin_pct"] = pat_margin

    ebitda_margin = _margin_pct(state_values.get("op_profit"), sales)
    if ebitda_margin is not None:
        normalized["ebitda_margin_pct"] = ebitda_margin

    return normalized


def _margin_pct(numerator: float | None, revenue: float | None) -> float | None:
    if numerator is None or revenue is None or revenue == 0:
        return None
    return (numerator / revenue) * 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Screener latest quarterly results.")
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument("--headless", dest="headless", action="store_true", default=True, help="Run Playwright in background mode. This is the default.")
    browser_mode.add_argument("--headed", dest="headless", action="store_false", help="Show the browser window for login/debugging.")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    parser.add_argument("--limit", type=int, default=None, help="Limit companies processed per cycle.")
    parser.add_argument("--user-data-dir", default=None, help="Persistent Playwright profile directory for Screener login.")
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=0,
        help="Wait this long for manual Screener login when using a visible persistent profile.",
    )
    parser.add_argument("--company-url", default=None, help="Process one company URL directly instead of the latest-results listing.")
    parser.add_argument("--company-name", default=None, help="Display name for --company-url test runs.")
    parser.add_argument("--telegram-test", action="store_true", help="Send a Telegram test message and exit.")
    parser.add_argument("--telegram-chat-id", action="store_true", help="Process pending Telegram /start messages and show saved subscribers.")
    return parser.parse_args()


if __name__ == "__main__":
    configure_console()
    args = parse_args()
    if args.telegram_test:
        load_env_file()
        configure_logging()
        notifier = TelegramNotifier.from_env()
        if notifier is None:
            raise SystemExit("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sent = notifier.send_message("Screener Bot Telegram test message")
        if sent:
            print("Telegram test message sent.", flush=True)
        raise SystemExit(0 if sent else 1)

    if args.telegram_chat_id:
        load_env_file()
        configure_logging()
        notifier = TelegramNotifier.from_env()
        if notifier is None:
            raise SystemExit("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")

        notifier.process_updates()
        subscribers = notifier.get_subscribers()
        if not subscribers:
            raise SystemExit("No Telegram subscribers found. Send /start to the bot, then run this again.")

        for candidate in subscribers:
            print(
                f"chat_id={candidate['chat_id']} type={candidate['type']} title={candidate['title']}",
                flush=True,
            )
        raise SystemExit(0)

    asyncio.run(
        monitor(
            headless=args.headless,
            once=args.once,
            company_limit=args.limit,
            user_data_dir=args.user_data_dir,
            company_url=args.company_url,
            company_name=args.company_name,
            login_wait_seconds=args.login_wait_seconds,
        )
    )
