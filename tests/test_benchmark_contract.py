"""Pure probe-contract tests; never start PostgreSQL or measure throughput."""
import importlib.util
from pathlib import Path
import unittest
p=Path(__file__).resolve().parents[1]/'examples/data_product/benchmark.py'
spec=importlib.util.spec_from_file_location('benchmark_contract',p)
probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
class ProbeContractTests(unittest.TestCase):
    def test_fixed_small_workload(self):
        self.assertEqual((probe.ROWS,probe.SAMPLES,probe.SEED),(1000,7,17))
        self.assertIn('generate_series(1,1000)',probe.workload())
        self.assertEqual(probe.workload(),probe.workload())
    def test_nearest_rank(self):
        self.assertEqual(probe.nearest_rank([7,1,3,2,6,5,4],.5),4)
        self.assertEqual(probe.nearest_rank(list(range(1,8)),.95),7)
    def test_invalid_percentile(self):
        for values,p in [([],0.5),([1],0),([1],1.1)]:
            with self.assertRaises(ValueError):probe.nearest_rank(values,p)
    def test_expected_grain(self):
        self.assertIn('public.battle_profile_context',probe.QUERY)
        self.assertIn('c.player_tag=b.player_tag',probe.QUERY)
