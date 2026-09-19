"""数据集按档案分文件加载与事件回放。"""

import unittest

from evidence_base.loader import load_dataset_dir


class LoaderTest(unittest.TestCase):
    def test_separate_archives_loaded(self):
        ds = load_dataset_dir("fixtures/dataset")
        self.assertTrue(ds.store.has_record("CD-HLX001"))
        self.assertTrue(ds.store.has_record("SL-BRX-US-2025"))
        self.assertIn("RB-2026", ds.rulebook.versions)
        self.assertEqual(ds.period("FY2025")["rulebook_version"], "RB-2026")

    def test_event_replay_creates_versions_with_late_visibility(self):
        ds = load_dataset_dir("fixtures/dataset")
        early = ds.store.version_at("AP-HLX001-US", "2026-07-31T00:00:00Z")
        late = ds.store.version_at("AP-HLX001-US", "2027-03-01T00:00:00Z")
        self.assertEqual(early.version, 1)
        self.assertEqual(early.data["decision_date"], "2025-09-12")
        self.assertEqual(late.version, 2)
        self.assertEqual(late.data["decision_date"], "2025-08-29")
        # 后到的海外证据在早期不可见
        self.assertIsNone(ds.store.version_at("AP-HLX001-JP", "2026-07-31T00:00:00Z"))
        self.assertIsNotNone(ds.store.version_at("AP-HLX001-JP", "2027-03-01T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
