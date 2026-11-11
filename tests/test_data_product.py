import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("data_product", ROOT / "examples/data_product/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

class DataContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / "examples/data_product/fixture.json").read_text())
    def test_valid_and_exact_duplicate(self):
        demo.validate(self.fixture)
    def test_conflicting_duplicate(self):
        self.fixture["observations"][1]["result"] = "defeat"
        with self.assertRaises(ValueError): demo.validate(self.fixture)
    def test_future_event(self):
        self.fixture["observations"][0]["observed_at"] = "2025-01-01T00:00:00Z"
        with self.assertRaises(ValueError): demo.validate(self.fixture)
    def test_unknown_fields(self):
        self.fixture["observations"][0]["secret"] = "synthetic"
        with self.assertRaises(ValueError): demo.validate(self.fixture)
    def test_real_identity_rejected(self):
        self.fixture["observations"][0]["player_tag"] = "not-demo"
        with self.assertRaises(ValueError): demo.validate(self.fixture)
    def test_result_domain(self):
        self.fixture["observations"][0]["result"] = "unknown"
        with self.assertRaises(ValueError): demo.validate(self.fixture)
    def test_timezone_required(self):
        self.fixture["profiles"][0]["captured_at"] = "2026-01-01T00:00:00"
        with self.assertRaises(ValueError): demo.validate(self.fixture)
    def test_ambiguous_snapshot(self):
        self.fixture["profiles"].append(copy.deepcopy(self.fixture["profiles"][0]))
        with self.assertRaises(ValueError): demo.validate(self.fixture)
    def test_bounded_records(self):
        self.fixture["observations"] *= 21
        with self.assertRaises(ValueError): demo.validate(self.fixture)
    def test_boolean_version(self):
        self.fixture["version"] = True
        with self.assertRaises(ValueError): demo.validate(self.fixture)
