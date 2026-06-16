from __future__ import annotations

import unittest
from unittest.mock import patch

import main
import scraper
from alerter import format_combined_alert
from parser import (
    MetricQuarterValues,
    YoyMetricValues,
    build_alert_metrics,
    parse_latest_results_companies,
    parse_quarterly_metrics,
    yoy_alert_needed,
)


def sjs_yoy_metrics() -> dict[str, YoyMetricValues]:
    return {
        "sales": YoyMetricValues(previous=201.0, current=260.0, change=29.353233830845773),
        "op_profit": YoyMetricValues(previous=51.0, current=74.7, change=46.470588235294116),
        "net_profit": YoyMetricValues(previous=33.7, current=48.9, change=45.10385756676558),
    }


def sjs_company() -> dict:
    return {
        "id": "SJS",
        "name": "SJS Enterprises",
        "url": "https://www.screener.in/company/SJS/#quarters",
        "yoy": sjs_yoy_metrics(),
    }


async def failing_company_detail_scrape(page, company):
    raise RuntimeError("detail scrape failed")


class ParserRegressionTests(unittest.TestCase):
    def test_latest_results_skips_pdf_links_filters_market_cap_and_parses_yoy(self):
        html = """
        <div class="result-card">
          <a href="/company/SJS/">SJS Enterprises</a>
          <a href="/company/SJS/consolidated.pdf">PDF</a>
          <span>M.Cap Rs. 6,770 Cr</span>
          <table>
            <tr><th></th><th>YOY</th><th>Mar 2026</th><th>Dec 2025</th><th>Mar 2025</th></tr>
            <tr><td>Sales</td><td>up 30%</td><td>260</td><td>244</td><td>201</td></tr>
            <tr><td>EBIDT</td><td>up 46%</td><td>74.7</td><td>71.4</td><td>51.0</td></tr>
            <tr><td>Net profit</td><td>up 45%</td><td>48.9</td><td>45.0</td><td>33.7</td></tr>
          </table>
        </div>
        <div class="result-card">
          <a href="/company/TINY/">Tiny Co</a>
          <span>M.Cap Rs. 199 Cr</span>
          <table><tr><td>Sales</td><td>up 30%</td><td>2</td><td>1</td><td>1</td></tr></table>
        </div>
        """

        companies = parse_latest_results_companies(html)

        self.assertEqual([company["id"] for company in companies], ["SJS"])
        self.assertEqual(companies[0]["name"], "SJS Enterprises")
        self.assertEqual(companies[0]["market_cap_cr"], 6770.0)
        self.assertAlmostEqual(companies[0]["yoy"]["sales"].change, 29.353233830845773)

    def test_quarterly_parser_handles_expandable_buttons_raw_pdf_and_partial_metrics(self):
        html = """
        <section id="quarters">
          <table class="data-table">
            <thead><tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr></thead>
            <tbody>
              <tr><td><button>Sales <span>+</span></button></td><td>100</td><td>130</td></tr>
              <tr><td>Raw PDF</td><td>x</td><td>x</td></tr>
              <tr><td>Net profit</td><td>5</td><td>13</td></tr>
            </tbody>
          </table>
        </section>
        """

        metrics = parse_quarterly_metrics(html)
        alert_metrics = build_alert_metrics(metrics)

        self.assertEqual(metrics["sales"], MetricQuarterValues(previous=100.0, current=130.0))
        self.assertEqual(metrics["net_profit"], MetricQuarterValues(previous=5.0, current=13.0))
        self.assertNotIn("op_profit", metrics)
        self.assertTrue(alert_metrics["sales"].triggered)
        self.assertTrue(alert_metrics["pat_margin_pct"].triggered)
        self.assertFalse(alert_metrics["ebitda_margin_pct"].triggered)

    def test_sjs_format_only_shows_sales_yoy_when_margin_changes_are_below_threshold(self):
        message = format_combined_alert("SJS Enterprises", build_alert_metrics({}), sjs_yoy_metrics())

        self.assertIn("Sales YOY", message)
        self.assertIn("+29.4%", message)
        self.assertNotIn("EBIDTA YOY", message)
        self.assertNotIn("PAT YOY", message)

    def test_yoy_margin_thresholds_use_margin_change_not_raw_ebitda_or_pat_change(self):
        yoy = sjs_yoy_metrics()

        self.assertTrue(yoy_alert_needed(yoy))
        self.assertAlmostEqual(main._margin_pct(74.7, 260.0), 28.73076923076923)
        self.assertAlmostEqual(main._margin_pct(51.0, 201.0), 25.37313432835821)


class CycleRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_alert_saves_state_immediately(self):
        state: dict[str, dict[str, float]] = {}
        saved_snapshots: list[dict[str, dict[str, float]]] = []

        with (
            patch.object(main, "scrape_company_metrics_with_retries", failing_company_detail_scrape),
            patch.object(main, "send_telegram_alert_to_all", lambda message: True),
            patch.object(main, "save_state", lambda saved_state: saved_snapshots.append(saved_state.copy())),
        ):
            alerts_detected = await main.run_cycle(None, 1, state, direct_company=sjs_company())

        self.assertTrue(alerts_detected)
        self.assertGreaterEqual(len(saved_snapshots), 2)
        self.assertIn("SJS", saved_snapshots[0])
        self.assertEqual(saved_snapshots[0]["SJS"]["yoy_sales_current"], 260.0)

    def test_blob_state_refresh_merges_latest_state_before_company_check(self):
        state: dict[str, dict[str, float]] = {"LOCAL": {"sales": 1.0}}

        with (
            patch.dict("os.environ", {"STATE_BACKEND": "blob"}, clear=True),
            patch.object(main, "load_state", return_value={"ASIANPAINT": {"sales": 100.0}}),
        ):
            main._refresh_shared_state(state)

        self.assertEqual(state["LOCAL"]["sales"], 1.0)
        self.assertEqual(state["ASIANPAINT"]["sales"], 100.0)

    async def test_yoy_alert_sends_even_when_company_detail_scrape_fails(self):
        state: dict[str, dict[str, float]] = {}
        sent_messages: list[str] = []

        with (
            patch.object(main, "scrape_company_metrics_with_retries", failing_company_detail_scrape),
            patch.object(main, "send_telegram_alert_to_all", lambda message: sent_messages.append(message) or True),
            patch.object(main, "save_state", lambda saved_state: None),
        ):
            alerts_detected = await main.run_cycle(None, 1, state, direct_company=sjs_company())

        self.assertTrue(alerts_detected)
        self.assertEqual(len(sent_messages), 1)
        self.assertIn("Sales YOY", sent_messages[0])
        self.assertIn("SJS", state)
        self.assertEqual(state["SJS"]["yoy_sales_current"], 260.0)

    async def test_send_failure_does_not_advance_state(self):
        state: dict[str, dict[str, float]] = {}

        with (
            patch.object(main, "scrape_company_metrics_with_retries", failing_company_detail_scrape),
            patch.object(main, "send_telegram_alert_to_all", lambda message: False),
            patch.object(main, "save_state", lambda saved_state: None),
        ):
            alerts_detected = await main.run_cycle(None, 1, state, direct_company=sjs_company())

        self.assertTrue(alerts_detected)
        self.assertNotIn("SJS", state)

    async def test_yoy_only_duplicate_state_is_suppressed(self):
        existing_state = main._latest_company_state_values(build_alert_metrics({}), sjs_yoy_metrics())
        state = {"SJS": existing_state.copy()}
        sent_messages: list[str] = []

        with (
            patch.object(main, "scrape_company_metrics_with_retries", failing_company_detail_scrape),
            patch.object(main, "send_telegram_alert_to_all", lambda message: sent_messages.append(message) or True),
            patch.object(main, "save_state", lambda saved_state: None),
        ):
            alerts_detected = await main.run_cycle(None, 1, state, direct_company=sjs_company())

        self.assertFalse(alerts_detected)
        self.assertEqual(sent_messages, [])


class ScraperFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_fallback_runs_after_browser_retries_fail(self):
        calls = {"browser": 0, "fallback": 0}
        expected = {"sales": MetricQuarterValues(previous=1.0, current=2.0)}

        async def browser_failure(page, company):
            calls["browser"] += 1
            raise RuntimeError("browser failed")

        def http_success(company):
            calls["fallback"] += 1
            return expected

        with (
            patch.object(scraper, "scrape_company_metrics", browser_failure),
            patch.object(scraper, "_fetch_company_metrics_http", http_success),
        ):
            result = await scraper.scrape_company_metrics_with_retries(
                None,
                {"name": "Fallback Co", "url": "https://www.screener.in/company/FALLBACK/#quarters"},
                retries=1,
            )

        self.assertEqual(result, expected)
        self.assertEqual(calls, {"browser": 2, "fallback": 1})


class TelegramRegressionTests(unittest.TestCase):
    def test_recipients_are_unique_sorted_static_plus_dynamic(self):
        with (
            patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "20", "TELEGRAM_CHAT_IDS": "10,20,30"}, clear=True),
            patch("telegram_notifier._load_dynamic_subscribers", return_value=[{"chat_id": "15"}, {"chat_id": "10"}]),
        ):
            from telegram_notifier import get_all_telegram_recipients

            self.assertEqual(get_all_telegram_recipients(), ["10", "15", "20", "30"])

    def test_start_command_saves_subscriber_and_offset(self):
        import telegram_notifier

        saved_subscribers = []
        saved_offsets = []
        updates = [
            {
                "update_id": 42,
                "message": {
                    "text": "/start",
                    "chat": {"id": 123, "type": "private", "first_name": "Test"},
                },
            }
        ]

        with (
            patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "dummy"}, clear=True),
            patch.object(telegram_notifier, "_load_offset_data", return_value={"offset": 40}),
            patch.object(telegram_notifier, "_fetch_updates", return_value=updates),
            patch.object(telegram_notifier, "_load_dynamic_subscribers", return_value=[]),
            patch.object(telegram_notifier, "_save_dynamic_subscribers", lambda data: saved_subscribers.append(data)),
            patch.object(telegram_notifier, "_save_offset_data", lambda data: saved_offsets.append(data)),
            patch.object(telegram_notifier, "_send_to_chat", return_value=True),
        ):
            changes = telegram_notifier.poll_telegram_subscribers_once()

        self.assertEqual(changes, 1)
        self.assertEqual(saved_subscribers[0][0]["chat_id"], "123")
        self.assertEqual(saved_offsets, [{"offset": 43}])

    def test_stop_command_removes_subscriber_and_saves_offset(self):
        import telegram_notifier

        saved_subscribers = []
        saved_offsets = []
        updates = [
            {
                "update_id": 50,
                "message": {
                    "text": "/stop",
                    "chat": {"id": 123, "type": "private", "first_name": "Test"},
                },
            }
        ]

        with (
            patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "dummy"}, clear=True),
            patch.object(telegram_notifier, "_load_offset_data", return_value={"offset": 50}),
            patch.object(telegram_notifier, "_fetch_updates", return_value=updates),
            patch.object(
                telegram_notifier,
                "_load_dynamic_subscribers",
                return_value=[{"chat_id": "123", "type": "private", "title": "Test"}],
            ),
            patch.object(telegram_notifier, "_save_dynamic_subscribers", lambda data: saved_subscribers.append(data)),
            patch.object(telegram_notifier, "_save_offset_data", lambda data: saved_offsets.append(data)),
            patch.object(telegram_notifier, "_send_to_chat", return_value=True),
        ):
            changes = telegram_notifier.poll_telegram_subscribers_once()

        self.assertEqual(changes, 1)
        self.assertEqual(saved_subscribers, [[]])
        self.assertEqual(saved_offsets, [{"offset": 51}])


if __name__ == "__main__":
    unittest.main()
