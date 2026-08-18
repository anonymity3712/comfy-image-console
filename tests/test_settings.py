import unittest

import app


class SettingsTests(unittest.TestCase):
    def test_normalize_settings_accepts_text_lists_and_ports(self):
        result = app._normalize_settings({
            "comfyui_url": "http://127.0.0.1:8189/",
            "discovery_ports": "8187, 8188;8189",
            "auto_start": True,
            "diffusion_dirs": "D:\\models\r\nD:\\extra",
            "lora_dirs": ["D:\\loras"],
        })
        self.assertEqual(result["comfyui_url"], "http://127.0.0.1:8189")
        self.assertEqual(result["discovery_ports"], [8187, 8188, 8189])
        self.assertTrue(result["auto_start"])
        self.assertEqual(result["diffusion_dirs"], ["D:\\models", "D:\\extra"])
        self.assertEqual(result["lora_dirs"], ["D:\\loras"])

    def test_normalize_settings_rejects_invalid_url(self):
        with self.assertRaises(ValueError):
            app._normalize_settings({"comfyui_url": "ftp://example.invalid"})


if __name__ == "__main__":
    unittest.main()
