from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from alerter import print_combined_alert
from parser import build_alert_metrics, company_alert_needed, latest_state_values, parse_company_id
from scraper import launch_browser, polite_company_delay, scrape_company_metrics_with_retries, scrape_latest_companies
from telegram_notifier import TelegramNotifier, load_env_file

STATE_FILE = Path("state.json")
SLEEP_SECONDS = 60


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_state(path: Path = STATE_FILE) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("State file is invalid JSON; starting with empty state")
        return {}


def save_state(state: dict[str, dict[str, float]], path: Path = STATE_FILE) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


async def run_cycle(
    page,
    cycle_number: int,
    state: dict[str, dict[str, float]],
    company_limit: int | None = None,
    direct_company: dict[str, str] | None = None,
    login_wait_seconds: int = 0,
    telegram_notifier: TelegramNotifier | None = None,
) -> bool:
    companies = [direct_company] if direct_company else await scrape_latest_companies(page, login_wait_seconds)
    if company_limit is not None:
        companies = companies[:company_limit]

    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Cycle #{cycle_number} started at {start_time} - Found {len(companies)} companies", flush=True)

    alerts_detected = False
    for index, company in enumerate(companies, start=1):
        try:
            if index > 1:
                await polite_company_delay()

            metrics = await scrape_company_metrics_with_retries(page, company)
            previous_state = state.get(company["id"])
            alert_metrics = build_alert_metrics(metrics)
            if company_alert_needed(alert_metrics, previous_state):
                alert_message = print_combined_alert(company["name"], alert_metrics, company.get("yoy"))
                if telegram_notifier:
                    telegram_notifier.send_message(alert_message)
                alerts_detected = True

            state[company["id"]] = latest_state_values(alert_metrics)
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    telegram_notifier = TelegramNotifier.from_env()
    startup_message = f"Screener Bot Started - Monitoring every {SLEEP_SECONDS} seconds"
    print(startup_message, flush=True)
    subscriber_task = None
    if telegram_notifier:
        await asyncio.to_thread(telegram_notifier.process_updates)
        subscriber_count = len(telegram_notifier.get_subscribers())
        print(f"Telegram notifications enabled for {subscriber_count} subscriber(s)", flush=True)
        telegram_notifier.send_message(startup_message)
        subscriber_task = asyncio.create_task(poll_telegram_subscribers(telegram_notifier))
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
                await run_cycle(
                    page,
                    cycle,
                    state,
                    company_limit=company_limit,
                    direct_company=direct_company,
                    login_wait_seconds=login_wait_seconds,
                    telegram_notifier=telegram_notifier,
                )
            except Exception:
                logging.exception("Cycle #%s failed", cycle)
                if once:
                    raise
            if once:
                break
            await asyncio.sleep(SLEEP_SECONDS)
            cycle += 1
    finally:
        if subscriber_task:
            subscriber_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subscriber_task
        await browser.close()
        await playwright.stop()


async def poll_telegram_subscribers(telegram_notifier: TelegramNotifier, interval_seconds: int = 5) -> None:
    while True:
        try:
            changes = await asyncio.to_thread(telegram_notifier.process_updates)
            if changes:
                subscriber_count = len(telegram_notifier.get_subscribers())
                print(f"Telegram subscribers updated: {subscriber_count} active", flush=True)
        except Exception:
            logging.exception("Telegram subscriber polling failed")
        await asyncio.sleep(interval_seconds)


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
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        notifier = TelegramNotifier.from_env()
        if notifier is None:
            raise SystemExit("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sent = notifier.send_message("Screener Bot Telegram test message")
        if sent:
            print("Telegram test message sent.", flush=True)
        raise SystemExit(0 if sent else 1)

    if args.telegram_chat_id:
        load_env_file()
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
