from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.alerts.control_panel import ForwarderInlineControlPanel
from app.core.destinations import Destination
from app.core.group_sync import save_group_topic_selection, sync_sendable_groups
from app.core.targets import DestinationTarget, load_targets, save_targets
from app.core.topics import TopicInfo
from app.utils.storage import save_json


class GroupTopicSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_only_campaign_update_does_not_request_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = Destination(
                id=123,
                title="Renamed Group",
                username=None,
                kind="group",
                status="likely",
                peer_type="channel",
                sendable=True,
                paid_message_stars=5,
            )
            campaign = SimpleNamespace(
                id="metadata-ad",
                target_refs=[
                    {
                        "group_id": 123,
                        "group_title": "Old Group Name",
                        "peer_type": "channel",
                        "topic_id": None,
                        "paid_message_stars": None,
                        "is_paid": False,
                    }
                ],
            )
            with (
                patch("app.core.group_sync.DESTINATIONS_CACHE", root / "destinations.json"),
                patch("app.core.group_sync.GROUP_SYNC_STATUS", root / "group_sync_status.json"),
                patch("app.core.group_sync.GROUP_SELECTION", root / "group_selection.json"),
                patch("app.core.group_sync.GROUP_TOPIC_SELECTION", root / "topic_selection.json"),
                patch("app.core.targets.TARGETS_PATH", root / "targets.json"),
                patch("app.core.group_sync.sync_destinations", AsyncMock(return_value=[destination])),
                patch("app.core.group_sync.list_campaigns", return_value=[campaign]),
                patch("app.core.group_sync.replace_campaign") as replace_campaign,
            ):
                save_targets([DestinationTarget(group_id=123, group_title="Old Group Name")])
                report = await sync_sendable_groups(object())

            self.assertEqual(report.changed_campaign_ids, ["metadata-ad"])
            self.assertEqual(report.restart_campaign_ids, [])
            replace_campaign.assert_called_once_with(campaign)

    async def test_removed_campaign_target_requests_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            campaign = SimpleNamespace(
                id="structural-ad",
                target_refs=[{"group_id": 123, "group_title": "Removed Group", "topic_id": None}],
            )
            with (
                patch("app.core.group_sync.DESTINATIONS_CACHE", root / "destinations.json"),
                patch("app.core.group_sync.GROUP_SYNC_STATUS", root / "group_sync_status.json"),
                patch("app.core.group_sync.GROUP_SELECTION", root / "group_selection.json"),
                patch("app.core.group_sync.GROUP_TOPIC_SELECTION", root / "topic_selection.json"),
                patch("app.core.targets.TARGETS_PATH", root / "targets.json"),
                patch("app.core.group_sync.sync_destinations", AsyncMock(return_value=[])),
                patch("app.core.group_sync.list_campaigns", return_value=[campaign]),
                patch("app.core.group_sync.replace_campaign"),
            ):
                save_targets([DestinationTarget(group_id=123, group_title="Removed Group")])
                report = await sync_sendable_groups(object())

            self.assertEqual(report.restart_campaign_ids, ["structural-ad"])
            self.assertEqual(campaign.target_refs, [])

    async def test_forum_topic_menu_is_paginated_and_callback_data_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_path = root / "destinations.json"
            topic_selection_path = root / "topic_selection.json"
            save_json(
                cache_path,
                [
                    {
                        "id": 1234567890,
                        "title": "Forum Group",
                        "kind": "group",
                        "sendable": True,
                        "is_forum": True,
                        "topics": [
                            {"topic_id": topic_id, "title": f"Topic {topic_id}", "top_message": topic_id}
                            for topic_id in range(1, 13)
                        ],
                    }
                ],
            )
            with (
                patch("app.alerts.control_panel.DESTINATIONS_CACHE", cache_path),
                patch("app.core.group_sync.GROUP_TOPIC_SELECTION", topic_selection_path),
            ):
                panel = ForwarderInlineControlPanel(None)
                text = panel._topics_text(1234567890, 0)
                keyboard = panel._topics_keyboard(1234567890, 2, 0)

            self.assertIn("Page:</b> 1/2", text)
            callbacks = [
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertTrue(any(value.startswith("fw:resources:topic:") for value in callbacks))
            self.assertLessEqual(max(map(len, callbacks)), 64)

    async def test_selected_forum_topics_replace_generic_reusable_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = Destination(
                id=123,
                title="Forum Group",
                username="forum_group",
                kind="group",
                status="likely",
                peer_type="channel",
                sendable=True,
                is_forum=True,
            )
            path_patches = (
                patch("app.core.group_sync.DESTINATIONS_CACHE", root / "destinations.json"),
                patch("app.core.group_sync.GROUP_SYNC_STATUS", root / "group_sync_status.json"),
                patch("app.core.group_sync.GROUP_SELECTION", root / "group_selection.json"),
                patch("app.core.group_sync.GROUP_TOPIC_SELECTION", root / "topic_selection.json"),
                patch("app.core.targets.TARGETS_PATH", root / "targets.json"),
            )
            for path_patch in path_patches:
                path_patch.start()
            self.addCleanup(lambda: [path_patch.stop() for path_patch in reversed(path_patches)])

            save_targets([DestinationTarget(group_id=123, group_title="Forum Group")])
            save_group_topic_selection({123: {11, 22}})
            topics = [
                TopicInfo(topic_id=11, title="Advertising", top_message=11),
                TopicInfo(topic_id=22, title="Marketplace", top_message=22),
            ]
            with (
                patch("app.core.group_sync.sync_destinations", AsyncMock(return_value=[destination])),
                patch("app.core.group_sync.fetch_forum_topics", AsyncMock(return_value=topics)),
                patch("app.core.group_sync.list_campaigns", return_value=[]),
            ):
                report = await sync_sendable_groups(object(), refresh_topics=True)

            targets = load_targets()
            self.assertEqual({target.topic_id for target in targets}, {11, 22})
            self.assertEqual({target.topic_title for target in targets}, {"Advertising", "Marketplace"})
            self.assertEqual(report.forum_groups, 1)
            self.assertEqual(report.topics_cached, 2)
            self.assertEqual(report.topic_errors, 0)

    async def test_existing_group_target_is_preserved_until_topics_are_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = Destination(
                id=123,
                title="Forum Group",
                username=None,
                kind="group",
                status="likely",
                peer_type="channel",
                sendable=True,
                is_forum=True,
            )
            with (
                patch("app.core.group_sync.DESTINATIONS_CACHE", root / "destinations.json"),
                patch("app.core.group_sync.GROUP_SYNC_STATUS", root / "group_sync_status.json"),
                patch("app.core.group_sync.GROUP_SELECTION", root / "group_selection.json"),
                patch("app.core.group_sync.GROUP_TOPIC_SELECTION", root / "topic_selection.json"),
                patch("app.core.targets.TARGETS_PATH", root / "targets.json"),
                patch("app.core.group_sync.sync_destinations", AsyncMock(return_value=[destination])),
                patch("app.core.group_sync.fetch_forum_topics", AsyncMock(return_value=[])),
                patch("app.core.group_sync.list_campaigns", return_value=[]),
            ):
                save_targets([DestinationTarget(group_id=123, group_title="Forum Group")])
                await sync_sendable_groups(object(), refresh_topics=True)
                targets = load_targets()

            self.assertEqual(len(targets), 1)
            self.assertIsNone(targets[0].topic_id)


if __name__ == "__main__":
    unittest.main()
