from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.alerts.control_panel import (
    ForwarderInlineControlPanel,
    PendingInput,
    _CAMPAIGN_EDIT_PARSERS,
    _campaign_field_name,
    _parse_windows,
)
from app.core.campaigns import Campaign
from app.service_runtime import ForwarderService, ServiceOptions


def _campaign() -> Campaign:
    return Campaign(
        id="testad1234",
        name="Test Ad",
        message_links=["https://t.me/source/10"],
        target_refs=[{"group_id": 100, "group_title": "Target"}],
        send_gap_min_sec=1800,
        send_gap_max_sec=1800,
        batch_gap_min_sec=0,
        batch_gap_max_sec=0,
        latest_sources=["me"],
    )


def _callbacks(markup) -> list[str]:
    return [
        str(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


class ControlPanelSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = ForwarderInlineControlPanel(SimpleNamespace(control=None))
        self.campaign = _campaign()

    def test_every_ad_settings_page_opens_and_has_valid_callbacks(self) -> None:
        with patch.object(self.panel, "_campaign_or_raise", return_value=self.campaign):
            for section in ("sources", "pacing", "schedule", "limits", "modes"):
                text = self.panel._campaign_section_text(self.campaign.id, section)
                callbacks = _callbacks(self.panel._campaign_section_keyboard(self.campaign.id, section))
                self.assertTrue(text)
                self.assertTrue(callbacks)
                self.assertTrue(all(len(item.encode("utf-8")) <= 64 for item in callbacks))

    def test_main_ad_menu_exposes_all_settings_pages(self) -> None:
        callbacks = _callbacks(self.panel._campaign_keyboard(self.campaign.id))
        for section in ("sources", "pacing", "schedule", "limits", "modes"):
            self.assertIn(f"fw:section:{self.campaign.id}:{section}", callbacks)
        self.assertIn(f"fw:stats:ad:{self.campaign.id}", callbacks)

    def test_every_edit_button_uses_the_registered_parser(self) -> None:
        with patch.object(self.panel, "_campaign_or_raise", return_value=self.campaign):
            callbacks: list[str] = []
            for section in ("sources", "pacing", "schedule", "limits", "modes"):
                callbacks.extend(_callbacks(self.panel._campaign_section_keyboard(self.campaign.id, section)))
        for callback in callbacks:
            parts = callback.split(":")
            if len(parts) >= 6 and parts[1:3] == ["campaign", "edit"]:
                self.assertEqual(_CAMPAIGN_EDIT_PARSERS.get(_campaign_field_name(parts[4])), parts[5])

    def test_fixed_message_editor_uses_message_links_not_saved_source_indexes(self) -> None:
        pending = PendingInput(
            scope="campaign",
            field="message_links",
            parser="links",
            campaign_id=self.campaign.id,
            return_to="campaign",
            prompt="",
        )
        with patch.object(self.panel, "_campaign_or_raise", return_value=self.campaign), patch(
            "app.alerts.control_panel.replace_campaign"
        ) as replace:
            self.panel._apply_campaign_input(pending, "https://t.me/c/1234567890/112?single")
        self.assertEqual(self.campaign.message_links, ["https://t.me/c/1234567890/112"])
        replace.assert_called_once_with(self.campaign)

    def test_schedule_windows_reject_invalid_clock_values(self) -> None:
        with self.assertRaises(ValueError):
            _parse_windows("29:99-30:00")

    def test_destination_delay_can_be_set_and_cleared_per_ad(self) -> None:
        pending = PendingInput(
            scope="targets",
            field="delay",
            parser="selection",
            campaign_id=self.campaign.id,
            return_to="targets",
            prompt="",
        )
        with patch.object(self.panel, "_campaign_or_raise", return_value=self.campaign), patch(
            "app.alerts.control_panel.replace_campaign"
        ):
            self.panel._apply_target_input(pending, "1 | 30m")
            self.assertEqual(self.campaign.target_refs[0]["extra_delay_sec"], 1800)
            self.panel._apply_target_input(pending, "1 | none")
            self.assertIsNone(self.campaign.target_refs[0]["extra_delay_sec"])


class RuntimeAdReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_ad_statistics_are_scoped_to_the_selected_campaign(self) -> None:
        panel = ForwarderInlineControlPanel(SimpleNamespace(control=None))
        campaign = _campaign()
        timestamp = datetime.now().isoformat()
        history = SimpleNamespace(
            get_by_campaign=AsyncMock(
                return_value=[
                    {
                        "timestamp": timestamp,
                        "campaign_id": campaign.id,
                        "group_id": 100,
                        "topic_id": None,
                        "success": 1,
                        "stars_cost": 0,
                    },
                    {
                        "timestamp": timestamp,
                        "campaign_id": campaign.id,
                        "group_id": 200,
                        "topic_id": 10,
                        "success": 0,
                        "stars_cost": 5,
                    },
                ]
            )
        )
        with patch.object(panel, "_campaign_or_raise", return_value=campaign), patch(
            "app.alerts.control_panel._campaign_status", return_value=("🟢", "Running")
        ), patch("app.alerts.control_panel.get_history", AsyncMock(return_value=history)):
            text = await panel._campaign_stats_text(campaign.id)
        self.assertIn("Ad Statistics", text)
        self.assertIn("Sent: <b>2</b>", text)
        self.assertIn("Successful: <b>1</b>", text)
        self.assertIn("Failed: <b>1</b>", text)
        self.assertIn("Destinations reached: <b>2</b>", text)
        history.get_by_campaign.assert_awaited_once_with(campaign.id)

    async def test_disable_button_uses_service_control_that_stops_the_task(self) -> None:
        control = SimpleNamespace(
            disable_ad=AsyncMock(return_value="Ad disabled and stopped"),
        )
        panel = ForwarderInlineControlPanel(SimpleNamespace(control=control))
        campaign = _campaign()
        with patch.object(panel, "_campaign_or_raise", return_value=campaign):
            result = await panel._run_campaign_action(campaign.id, "disable")
        self.assertEqual(result, "Ad disabled and stopped")
        control.disable_ad.assert_awaited_once_with(campaign.id)

    async def test_running_ad_is_restarted_with_fresh_saved_configuration(self) -> None:
        service = ForwarderService(ServiceOptions(allow_risky=True))
        service.main_loop = asyncio.get_running_loop()
        old_task = asyncio.create_task(asyncio.sleep(3600))
        service.running_tasks["testad1234"] = {
            "task": old_task,
            "name": "Old Name",
            "dry": False,
        }
        fresh = _campaign()
        with patch("app.service_runtime.get_campaign", return_value=fresh), patch.object(
            service,
            "_start_campaign_background",
            AsyncMock(return_value=(True, "started")),
        ) as start:
            result = await service.ctrl_reload_ad(fresh.id)
        self.assertEqual(result, "Saved and applied to the running ad.")
        self.assertTrue(old_task.cancelled())
        start.assert_awaited_once_with(
            fresh,
            dry=False,
            allow_risky=True,
            notify_start=False,
        )

    async def test_stopped_ad_change_is_saved_without_starting_it(self) -> None:
        service = ForwarderService(ServiceOptions())
        service.main_loop = asyncio.get_running_loop()
        result = await service.ctrl_reload_ad("testad1234")
        self.assertEqual(result, "Saved. The change will apply when this ad starts.")


if __name__ == "__main__":
    unittest.main()
