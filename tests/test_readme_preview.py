import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from readme_data import read_model

class PreviewCoverageTests(unittest.TestCase):
    def test_missing_sources_do_not_use_legacy_marginals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data/work').mkdir(parents=True)
            (root / 'data/work/codex.json').write_text(json.dumps({'2026-09-09': {'totalTokens': 99}}))
            self.assertEqual(read_model(root)['work']['days'], {})

    def test_unready_published_source_blocks_combined_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'data/work'
            directory.mkdir(parents=True)
            ready = {'status': 'ok', 'metrics': {'modelEffort': {'effectiveFrom': '2026-09-09'}},
                     'days': {'2026-09-09': {'validMetrics': ['modelEffort'], 'combinations': []}}}
            unready = {'status': 'ok', 'metrics': {'modelEffort': {'effectiveFrom': None}}, 'days': {}}
            (directory / 'statistics.json').write_text(json.dumps({'sources': {'codex': ready, 'claude': unready}}))
            self.assertEqual(read_model(root)['work']['days'], {})

    def test_native_and_multica_harness_rows_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'data/work'
            directory.mkdir(parents=True)
            source = {'status': 'ok', 'metrics': {'modelEffort': {'effectiveFrom': '2026-09-09'}},
                      'days': {'2026-09-09': {'validMetrics': ['modelEffort'], 'combinations': [
                          {'model': 'gpt-6-astra', 'effort': 'low', 'totalTokens': 15}]}}}
            (directory / 'statistics.json').write_text(json.dumps({'sources': {'codex': source, 'codex-multica': source}}))
            rows = read_model(root)['work']['days']['2026-09-09']
            self.assertEqual(rows, [{'harness': 'codex', 'model': 'gpt-6-astra', 'effort': 'low', 'tokens': 30}])

class StaticReadmeTests(unittest.TestCase):
    def test_example_uses_only_static_svg_and_preserves_configuration_labels(self):
        import xml.etree.ElementTree as ET
        from readme_dashboard import panels
        from readme_preview import example_model
        rendered = dict(panels(example_model()))
        self.assertEqual(len(rendered), 12)
        for svg in rendered.values():
            tree = ET.fromstring(svg)
            forbidden = {'script', 'foreignObject', 'animate', 'set'}
            self.assertFalse(any(node.tag.split('}')[-1] in forbidden for node in tree.iter()))
        self.assertIn('gpt-6-astra', rendered['readme-work-execution-en.svg'])
        self.assertIn('medium', rendered['readme-work-execution-en.svg'])

    def test_zero_usage_is_not_awaiting_collection(self):
        from readme_dashboard import render_execution
        data = dict(days={'2026-09-09': []}, assignment=[], assignmentDate=None)
        svg = render_execution(data, 'work', 'en')
        self.assertIn('No token usage on selected verified dates', svg)
        self.assertNotIn('Awaiting verified model', svg)

    def test_preview_refuses_to_write_into_repository(self):
        from readme_preview import build
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'outside the repository'):
                build(root, root / 'assets', example=True)
