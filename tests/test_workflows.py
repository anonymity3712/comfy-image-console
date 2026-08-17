import json
from pathlib import Path
import unittest

import app


ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_registry_workflows_exist_and_validate(self):
        registry = json.loads((ROOT / "model_registry.json").read_text("utf-8"))
        self.assertGreaterEqual(len(registry["models"]), 2)
        for model in registry["models"]:
            with self.subTest(model=model["id"]):
                workflow_path = ROOT / model["workflow"]
                self.assertTrue(workflow_path.is_file())
                workflow = json.loads(workflow_path.read_text("utf-8"))
                result = app.validate_workflow(workflow)
                self.assertEqual(result["ok"], True, result["errors"])


if __name__ == "__main__":
    unittest.main()
