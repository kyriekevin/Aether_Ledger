import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from readme_preview import read_model

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
