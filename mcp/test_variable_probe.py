"""Pure tests of generated variable inspection: no MCP dependencies."""
import contextlib
import io
import unittest

from variable_probe import build_variable_probe, parse_variable_probe

class DummyShell:
    user_ns = {'count': 7, 'items': [1, 2], 'api_token': 'should-not-leak', 'hidden_name': 'x'}

class VariableProbeTests(unittest.TestCase):
    def test_limit_and_payload(self):
        code = build_variable_probe(names=['count', 'items', 'api_token'], limit=5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, {'get_ipython': lambda: DummyShell()})
        line = buf.getvalue().strip()
        result = parse_variable_probe({'status': 'ok', 'outputs': [{'output_type': 'stream', 'name': 'stdout', 'text': line}]})
        rows = {r['name']: r for r in result['variables']}
        self.assertEqual(rows['count']['preview'], '7')
        self.assertEqual(rows['items']['length'], 2)
        self.assertNotIn('preview', rows['api_token'])
    def test_invalid_names(self):
        with self.assertRaises(ValueError):
            build_variable_probe(['x); print(123)'], limit=4)
    def test_invalid_limits(self):
        with self.assertRaises(ValueError):
            build_variable_probe(limit=0)

if __name__ == '__main__':
    unittest.main()
