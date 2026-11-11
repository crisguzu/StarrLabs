"""Pure readiness regressions; no PostgreSQL process or network."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
p=Path(__file__).resolve().parent/'schema_smoke.py'
spec=importlib.util.spec_from_file_location('schema_readiness_contract',p)
runner=importlib.util.module_from_spec(spec)
with patch.dict(os.environ,{'POSTGRES_BIN':'/synthetic-not-executed'}):spec.loader.exec_module(runner)
class SchemaReadinessTests(unittest.TestCase):
    def test_socket_can_exist_before_accepting(self):
        ready=Mock(side_effect=[False,False,True]);sleep=Mock()
        runner.wait_for_ready(ready,lambda:False,sleep=sleep)
        self.assertEqual(ready.call_count,3)
        self.assertEqual(sleep.call_count,2)
    def test_exited_server_refuses(self):
        ready=Mock(return_value=True)
        with self.assertRaisesRegex(RuntimeError,'exited'):
            runner.wait_for_ready(ready,lambda:True,sleep=Mock())
        ready.assert_not_called()
    def test_timeout_does_not_continue_to_schema(self):
        with self.assertRaisesRegex(RuntimeError,'deadline'):
            runner.wait_for_ready(lambda:False,lambda:False,sleep=Mock(),attempts=3)
