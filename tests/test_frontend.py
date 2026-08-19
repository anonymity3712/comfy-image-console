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

    def test_full_prompt_dice_has_large_combination_space(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        match = re.search(
            r"const PROMPT_DICE_MODULES = \{(.*?)\n\};",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "full prompt dice modules must exist")
        total = 1
        module_count = 0
        for _, array_source in re.findall(r"(\w+):\s*\[(.*?)\]", match.group(1), re.DOTALL):
            options = re.findall(r'"([^"]*)"', array_source)
            self.assertGreaterEqual(len(options), 4)
            total *= len(options)
            module_count += 1
        self.assertGreaterEqual(module_count, 23)
        self.assertGreaterEqual(total, 1_000_000_000_000)
        self.assertIn("function rollFullPrompt()", html)


if __name__ == "__main__":
    unittest.main()
