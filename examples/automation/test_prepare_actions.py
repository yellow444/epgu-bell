import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from prepare_actions import prepare, main, ImportFailure


class AutomationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = json.loads((Path(__file__).parents[1] / 'snapshot.mock.json').read_text('utf-8'))
        self.options = dict(connection='demo', organization='ORG-DEMO', contour='test',
            targets=['bitrix24', 'webhook', 'digest'], entity_type_id=128, assigned_by_id=7)

    def prepare(self, snapshot=None, **kwargs):
        return prepare(snapshot or self.snapshot, self.root / 'actions.sqlite3', **{**self.options, **kwargs})

    def test_two_runs_have_six_unique_actions_and_no_network(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            first, second = self.prepare(), self.prepare()
        self.assertEqual((first['queued_new'], second['queued_new'], len(second['actions'])), (6, 0, 6))
        self.assertEqual(len({a['event_key'] for a in second['actions']}), 6)
        self.assertTrue(all(a['delivery'] == 'disabled' and a['requires_operator_review'] for a in second['actions']))

    def test_default_export_excludes_notification_text(self):
        result = self.prepare()
        for action in result['actions']:
            self.assertNotIn('title', action['notification']); self.assertNotIn('body', action['notification'])
        for record in self.snapshot['notifications']:
            import base64
            self.assertNotIn(base64.b64decode(record['title']['data']).decode(), json.dumps(result, ensure_ascii=False))

    def test_bitrix_payload_has_lookup_and_stable_xml_id(self):
        action = next(a for a in self.prepare()['actions'] if a['target'] == 'bitrix24')
        fields = action['create_if_missing']['params']['fields']
        self.assertEqual(action['lookup']['params']['filter']['=xmlId'], fields['xmlId'])
        self.assertEqual(fields['xmlId'], action['event_key'])
        self.assertEqual((fields['assignedById'], action['create_if_missing']['params']['entityTypeId']), (7, 128))

    def test_changed_record_refreshes_existing_actions(self):
        self.prepare()
        changed = copy.deepcopy(self.snapshot)
        changed['observed_at'] = '2026-11-21T10:00:00Z'
        record = changed['notifications'][0]
        record['read_state'] = 'read' if record['read_state'] != 'read' else 'unread'
        result = self.prepare(changed)
        self.assertEqual((result['queued_new'], result['refreshed'], len(result['actions'])), (0, 3, 6))

    def test_errors_do_not_generate_actions_or_change_configuration(self):
        self.prepare()
        before = (self.root / 'actions.sqlite3').read_bytes()
        with self.assertRaisesRegex(ImportFailure, 'automation_config_mismatch'):
            self.prepare(assigned_by_id=8)
        self.assertEqual(before, (self.root / 'actions.sqlite3').read_bytes())
        failed = {**self.snapshot, 'status': 'needs_user_action', 'notifications': []}
        with self.assertRaisesRegex(ImportFailure, 'needs_user_action'):
            self.prepare(failed)
        with closing(sqlite3.connect(self.root / 'actions.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM automation_outbox').fetchone()[0], 6)

    def test_text_is_opt_in_and_plain(self):
        result = self.prepare(include_text=True)
        self.assertTrue(all('title' in a['notification'] for a in result['actions']))
        self.assertTrue(all(a['message']['format'] == 'plain_text' for a in result['actions'] if a['target'] == 'digest'))

    def test_missing_bitrix_mapping_and_foreign_source_rejected(self):
        with self.assertRaisesRegex(ImportFailure, 'bitrix_mapping_required'):
            self.prepare(entity_type_id=None)
        with self.assertRaisesRegex(ImportFailure, 'context_mismatch'):
            self.prepare(organization='OTHER')
        self.assertFalse((self.root / 'actions.sqlite3').exists())

    def test_cli_writes_offline_json_and_repeated_run_is_idempotent(self):
        args = ['--snapshot', str(Path(__file__).parents[1] / 'snapshot.mock.json'), '--db', str(self.root / 'cli.sqlite3'),
            '--output', str(self.root / 'actions.json'), '--organization-id', 'ORG-DEMO', '--connection-id', 'demo',
            '--contour', 'test', '--targets', 'bitrix24,webhook,digest', '--entity-type-id', '128', '--assigned-by-id', '7']
        self.assertEqual(main(args), 0); self.assertEqual(main(args), 0)
        result = json.loads((self.root / 'actions.json').read_text('utf-8'))
        self.assertEqual((result['queued_new'], len(result['actions'])), (0, 6))


if __name__ == '__main__':
    unittest.main()
