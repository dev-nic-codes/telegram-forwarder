from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import _import_ads_from


class CampaignImportTests(unittest.TestCase):
    def test_import_accepts_exported_campaigns_and_legacy_defaults(self) -> None:
        exported = [
            {
                "id": "campaign-1",
                "name": "Test campaign",
                "message_links": ["https://t.me/example/1"],
                "target_refs": [{"group_id": -1001234567890}],
                "send_gap_min_sec": 60,
                "send_gap_max_sec": 120,
                "batch_gap_min_sec": 300,
                "batch_gap_max_sec": 600,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaigns.json"
            path.write_text(json.dumps(exported), encoding="utf-8")

            with patch("app.main.save_campaign") as save_campaign:
                _import_ads_from(path)

        save_campaign.assert_called_once()
        campaign = save_campaign.call_args.args[0]
        self.assertEqual(campaign.id, "campaign-1")
        self.assertEqual(campaign.latest_sources, [])
        self.assertTrue(campaign.enabled)


if __name__ == "__main__":
    unittest.main()
