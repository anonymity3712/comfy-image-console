import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendTests(unittest.TestCase):
    def test_teacache_group_is_attached_to_teacache_section(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        match = re.search(
            r'<div\s+class="group"\s+id="teacacheGroup">\s*<h3>(.*?)</h3>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "teacacheGroup id must remain on the TeaCache section")
        self.assertIn("TeaCache", match.group(1))


if __name__ == "__main__":
    unittest.main()
