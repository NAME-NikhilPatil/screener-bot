from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright

from parser import parse_latest_results_companies, parse_quarterly_metrics

LATEST_RESULTS_URL = "https://www.screener.in/results/latest/"
LOGIN_URL = "https://www.screener.in/login/?next=/results/latest/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


async def scrape_latest_companies(page: Page, login_wait_seconds: int = 0) -> list[dict[str, str]]:
    await page.goto(LATEST_RESULTS_URL, wait_until="domcontentloaded")
    if "/login/" in page.url or "/register/" in page.url:
        if await _attempt_env_login(page):
            await page.goto(LATEST_RESULTS_URL, wait_until="domcontentloaded")
        elif login_wait_seconds > 0:
            print(
                f"Screener login required. Complete login in the opened browser within {login_wait_seconds} seconds.",
                flush=True,
            )
            deadline = asyncio.get_running_loop().time() + login_wait_seconds
            while asyncio.get_running_loop().time() < deadline:
                if "/login/" not in page.url and "/register/" not in page.url:
                    await page.goto(LATEST_RESULTS_URL, wait_until="domcontentloaded")
                    break
                await asyncio.sleep(2)

            if "/login/" not in page.url and "/register/" not in page.url:
                await page.wait_for_load_state("domcontentloaded")
            else:
                raise RuntimeError("Screener login was not completed before the wait timeout.")

        else:
            raise RuntimeError(
                "Screener redirected to login/register. For background mode, set SCREENER_USERNAME and "
                "SCREENER_PASSWORD in .env. For one-time manual login troubleshooting, run with --headed "
                "--user-data-dir .\\screener-profile --login-wait-seconds 120."
            )
    await page.wait_for_selector('a[href*="/company/"]', timeout=30000)
    html = await page.content()
    return parse_latest_results_companies(html)


async def _attempt_env_login(page: Page) -> bool:
    username = os.environ.get("SCREENER_USERNAME") or os.environ.get("SCREENER_EMAIL")
    password = os.environ.get("SCREENER_PASSWORD")
    if not username or not password:
        return False

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.locator("input[name='username']").fill(username)
    await page.locator("input[name='password']").fill(password)
    await page.locator("button[type='submit']").click()
    try:
        await page.wait_for_url(lambda url: "/login/" not in url and "/register/" not in url, timeout=30000)
    except Exception:
        return False
    return True


async def scrape_company_metrics(page: Page, company: dict[str, str]) -> dict[str, Any]:
    await page.goto(company["url"], wait_until="domcontentloaded")
    await page.wait_for_selector("#quarters", timeout=30000)
    await page.locator("#quarters").scroll_into_view_if_needed()
    await page.wait_for_selector("#quarters table", timeout=30000)
    html = await page.content()
    return parse_quarterly_metrics(html)


async def scrape_company_metrics_with_retries(page: Page, company: dict[str, str], retries: int = 2) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await scrape_company_metrics(page, company)
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            await asyncio.sleep(1 + attempt)
    raise RuntimeError(f"Failed to scrape {company['name']} after {retries + 1} attempts: {last_error}")


async def polite_company_delay(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


async def launch_browser(headless: bool = True, user_data_dir: str | None = None) -> tuple[Any, Any, Page]:
    playwright = await async_playwright().start()
    if user_data_dir:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(user_data_dir)),
            headless=headless,
            user_agent=USER_AGENT,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        return playwright, context, page

    browser = await playwright.chromium.launch(headless=headless)
    context = await browser.new_context(user_agent=USER_AGENT)
    page = await context.new_page()
    return playwright, browser, page
