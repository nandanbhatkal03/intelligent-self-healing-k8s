import os
import tempfile
import unittest
from pathlib import Path

from selfheal.config import DEFAULT_CONFIG, load_config


class ConfigTest(unittest.TestCase):
    def test_shipped_config_loads(self):
        cfg = load_config(DEFAULT_CONFIG)
        self.assertEqual(cfg.target.namespace, "shoplite")
        self.assertEqual(cfg.detection.interval_seconds, 15)
        self.assertEqual(cfg.prediction.lookahead_seconds, 300)        # FR-3.2 default
        self.assertEqual(cfg.remediation.verify_window_seconds, 60)    # FR-4.3 default
        self.assertTrue(cfg.remediation.policies)

    def test_semester3_defaults(self):
        """Objective order: prediction (O4) and remediation (O5) ship switched off."""
        cfg = load_config(DEFAULT_CONFIG)
        self.assertEqual(cfg.remediation.mode, "off")
        self.assertFalse(cfg.prediction.enabled)

    def test_unquoted_off_is_tolerated(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("remediation:\n  mode: off\n")
        self.assertEqual(load_config(fh.name).remediation.mode, "off")
        Path(fh.name).unlink()

    def test_env_override_and_validation(self):
        os.environ["SELFHEAL_REMEDIATION_MODE"] = "enforce"
        try:
            self.assertEqual(load_config(DEFAULT_CONFIG).remediation.mode, "enforce")
            os.environ["SELFHEAL_REMEDIATION_MODE"] = "yolo"
            with self.assertRaises(ValueError):
                load_config(DEFAULT_CONFIG)
        finally:
            del os.environ["SELFHEAL_REMEDIATION_MODE"]

    def test_unknown_key_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("detection:\n  intervall_seconds: 5\n")
        with self.assertRaises(ValueError):
            load_config(fh.name)
        Path(fh.name).unlink()
