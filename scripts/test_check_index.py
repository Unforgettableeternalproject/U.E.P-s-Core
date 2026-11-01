"""check_index 的最小測試（標準庫 unittest，使用暫存目錄）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_index  # noqa: E402


def full_tracking(extra: str = "", skip: str = "") -> str:
    ids = check_index.REQUIRED["V"] + check_index.REQUIRED["D"] + check_index.REQUIRED["A"]
    rows = [f"| {i} | x | T-01 | — | — | — | 未開始 |" for i in ids if i != skip]
    rows.append("| T-01 | 卡 | C | c | — | — | — |")
    return "\n".join(rows) + "\n" + extra


class CheckIndexTest(unittest.TestCase):
    def make_root(self, tracking: str, index: str = "# Index\n") -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "docs" / "index").mkdir(parents=True)
        (root / "docs" / "index" / "TRACKING.md").write_text(tracking, encoding="utf-8")
        (root / "docs" / "index" / "INDEX.md").write_text(index, encoding="utf-8")
        (root / "real.py").write_text("", encoding="utf-8")
        return root

    def test_clean_passes(self):
        res = check_index.run(self.make_root(full_tracking("`real.py`\n")))
        self.assertEqual(res["errors"], [])

    def test_missing_v_row(self):
        res = check_index.run(self.make_root(full_tracking(skip="V07")))
        self.assertTrue(any("V07" in e for e in res["errors"]))

    def test_missing_path_fails_but_planned_and_fence_pass(self):
        extra = "`nope/missing.py`\n`later/x.py` planned\n```\n`fenced/x.py`\n```\n"
        res = check_index.run(self.make_root(full_tracking(extra)))
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("nope/missing.py", res["errors"][0])

    def test_unknown_task_card(self):
        res = check_index.run(self.make_root(full_tracking("T-99 在此\n")))
        self.assertTrue(any("T-99" in e for e in res["errors"]))

    def test_uep_package_not_in_index_warns(self):
        root = self.make_root(full_tracking(), index="`uep/events/`\n")
        (root / "uep" / "events").mkdir(parents=True)
        (root / "uep" / "presence").mkdir()
        res = check_index.run(root)
        self.assertEqual(res["warnings"], ["uep/presence/ 未列入 INDEX.md"])


if __name__ == "__main__":
    unittest.main()
