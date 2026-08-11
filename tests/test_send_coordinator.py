from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.send_coordinator import DestinationSendCoordinator


class DestinationSendCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_campaign_can_send_to_same_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            coordinator = DestinationSendCoordinator(Path(temp_dir) / "cooldowns.json")
            first = await coordinator.try_acquire(100)
            self.assertIsNotNone(first)
            self.assertIsNone(await coordinator.try_acquire(100))
            other = await coordinator.try_acquire(200)
            self.assertIsNotNone(other)
            other.release()
            first.release()
            self.assertIsNotNone(await coordinator.try_acquire(100))

    async def test_group_wait_is_persistent_and_does_not_block_other_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "cooldowns.json"
            with patch("app.core.send_coordinator.time.time", return_value=1_000.0):
                coordinator = DestinationSendCoordinator(state_path)
                coordinator.defer_group(100, 30, remember_slowmode=True)
                self.assertEqual(coordinator.ready_in_seconds(100), 30)
                self.assertEqual(coordinator.ready_in_seconds(200), 0)

            with patch("app.core.send_coordinator.time.time", return_value=1_010.0):
                restored = DestinationSendCoordinator(state_path)
                self.assertEqual(restored.ready_in_seconds(100), 20)
                self.assertEqual(restored.snapshot()["known_slowmode_seconds"][100], 30)

    async def test_success_reapplies_learned_slow_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "cooldowns.json"
            with patch("app.core.send_coordinator.time.time", return_value=2_000.0):
                coordinator = DestinationSendCoordinator(state_path)
                coordinator.defer_group(100, 15, remember_slowmode=True)
            with patch("app.core.send_coordinator.time.time", return_value=2_020.0):
                restored = DestinationSendCoordinator(state_path)
                self.assertEqual(restored.ready_in_seconds(100), 0)
                self.assertEqual(restored.record_success(100), 15)
                self.assertEqual(restored.ready_in_seconds(100), 15)

    async def test_account_floodwait_blocks_every_group_until_exact_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "cooldowns.json"
            with patch("app.core.send_coordinator.time.time", return_value=3_000.0):
                coordinator = DestinationSendCoordinator(state_path)
                coordinator.defer_global(45)
                self.assertEqual(coordinator.ready_in_seconds(100), 45)
                self.assertEqual(coordinator.ready_in_seconds(200), 45)
            with patch("app.core.send_coordinator.time.time", return_value=3_046.0):
                restored = DestinationSendCoordinator(state_path)
                self.assertTrue(restored.is_ready(100))

    async def test_first_ambiguous_floodwait_only_defers_its_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "cooldowns.json"
            with patch("app.core.send_coordinator.time.time", return_value=4_000.0):
                coordinator = DestinationSendCoordinator(state_path)
                scope, wait_seconds = coordinator.record_flood_wait(100, 45)
                self.assertEqual(scope, "destination")
                self.assertEqual(wait_seconds, 45)
                self.assertEqual(coordinator.ready_in_seconds(100), 45)
                self.assertEqual(coordinator.ready_in_seconds(200), 0)

    async def test_second_distinct_floodwait_confirms_account_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "cooldowns.json"
            with patch("app.core.send_coordinator.time.time", return_value=5_000.0):
                coordinator = DestinationSendCoordinator(state_path)
                first_scope, _ = coordinator.record_flood_wait(100, 45)
                self.assertEqual(first_scope, "destination")
            with patch("app.core.send_coordinator.time.time", return_value=5_005.0):
                second_scope, wait_seconds = coordinator.record_flood_wait(200, 60)
                self.assertEqual(second_scope, "global")
                self.assertEqual(wait_seconds, 60)
                self.assertEqual(coordinator.ready_in_seconds(300), 60)

    async def test_repeated_wait_from_same_destination_does_not_become_global(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "cooldowns.json"
            with patch("app.core.send_coordinator.time.time", return_value=6_000.0):
                coordinator = DestinationSendCoordinator(state_path)
                coordinator.record_flood_wait(100, 45)
            with patch("app.core.send_coordinator.time.time", return_value=6_010.0):
                scope, _ = coordinator.record_flood_wait(100, 60)
                self.assertEqual(scope, "destination")
                self.assertEqual(coordinator.ready_in_seconds(200), 0)

    async def test_old_flood_evidence_does_not_confirm_account_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "cooldowns.json"
            with patch("app.core.send_coordinator.time.time", return_value=7_000.0):
                coordinator = DestinationSendCoordinator(state_path)
                coordinator.record_flood_wait(100, 300)
            with patch("app.core.send_coordinator.time.time", return_value=7_121.0):
                scope, _ = coordinator.record_flood_wait(200, 60)
                self.assertEqual(scope, "destination")
                self.assertEqual(coordinator.snapshot()["global_wait_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
