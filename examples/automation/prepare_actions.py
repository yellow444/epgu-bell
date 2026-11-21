"""Prepare integration examples from Bell JSON. No network or delivery code."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations'))
from import_notifications import ImportFailure, MAX_BYTES, import_payload


TARGETS = {'bitrix24', 'webhook', 'digest'}


def stable_key(scope, identifier, target):
    encoded = json.dumps([scope, identifier, target], sort_keys=True, separators=(',', ':')).encode()
    return 'bell_' + hashlib.sha256(encoded).hexdigest()


def prepare(payload, database, *, connection, organization, contour, targets,
            entity_type_id=None, assigned_by_id=None, include_text=False):
    targets = sorted(set(targets))
    if not targets or set(targets) - TARGETS:
        raise ImportFailure('invalid_target')
    if 'bitrix24' in targets and any(type(v) is not int or v <= 0 for v in (entity_type_id, assigned_by_id)):
        raise ImportFailure('bitrix_mapping_required')
    scope = {'connection_id': connection, 'organization_id': organization, 'contour': contour}
    configuration = json.dumps({'scope': scope, 'targets': targets, 'entity_type_id': entity_type_id,
        'assigned_by_id': assigned_by_id, 'include_text': bool(include_text)}, sort_keys=True)
    database = Path(database)
    if database.exists():
        with closing(sqlite3.connect(database)) as db:
            exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='automation_config'").fetchone()
            prior = db.execute('SELECT value FROM automation_config').fetchone() if exists else None
            if prior and prior[0] != configuration:
                raise ImportFailure('automation_config_mismatch')
    imported = import_payload(payload, database, connection=connection, organization=organization, contour=contour)
    new = refreshed = 0
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS automation_config(value TEXT NOT NULL)')
        if not db.execute('SELECT value FROM automation_config').fetchone():
            db.execute('INSERT INTO automation_config VALUES (?)', (configuration,))
        db.execute('CREATE TABLE IF NOT EXISTS automation_outbox(event_key TEXT PRIMARY KEY,target TEXT NOT NULL,payload TEXT NOT NULL)')
        rows = db.execute('SELECT notification_id,title,body,record_json FROM inbox ORDER BY notification_id').fetchall()
        for identifier, title, body, raw in rows:
            record = json.loads(raw)
            for target in targets:
                key = stable_key(scope, identifier, target)
                notification = {'id': identifier, 'read_state': record['read_state'],
                    'category': record.get('category'), 'occurred_at': record.get('occurred_at')}
                if include_text:
                    notification.update(title=title, body=body)
                action = {'event_key': key, 'target': target, 'delivery': 'disabled',
                    'requires_operator_review': True, 'notification': notification}
                if target == 'bitrix24':
                    fields = {'title': title[:200] if include_text else 'Новое уведомление Госуслуг',
                              'xmlId': key, 'assignedById': assigned_by_id}
                    action.update(lookup={'method': 'crm.item.list', 'params': {'entityTypeId': entity_type_id,
                        'filter': {'=xmlId': key}, 'select': ['id', 'xmlId']}},
                        create_if_missing={'method': 'crm.item.add', 'params': {'entityTypeId': entity_type_id, 'fields': fields}})
                elif target == 'webhook':
                    action['request'] = {'method': 'POST', 'headers': {'Idempotency-Key': key},
                        'json': {'schema_version': 'bell-event-1', 'event_key': key,
                                 'requires_operator_review': True, 'notification': notification}}
                else:
                    text = 'Новое уведомление Госуслуг. Проверить источник. ID: ' + identifier
                    if include_text:
                        text += '\n' + title[:200] + '\n' + body[:1000]
                    action['message'] = {'text': text, 'format': 'plain_text'}
                encoded = json.dumps(action, ensure_ascii=False, sort_keys=True)
                previous = db.execute('SELECT payload FROM automation_outbox WHERE event_key=?', (key,)).fetchone()
                if previous is None:
                    db.execute('INSERT INTO automation_outbox VALUES (?,?,?)', (key, target, encoded)); new += 1
                elif previous[0] != encoded:
                    db.execute('UPDATE automation_outbox SET payload=? WHERE event_key=?', (encoded, key)); refreshed += 1
        actions = [json.loads(r[0]) for r in db.execute('SELECT payload FROM automation_outbox ORDER BY target,event_key')]
    return {'schema_version': 'bell-actions-1', 'delivery': 'disabled', 'requires_operator_review': True,
        'imported': imported, 'queued_new': new, 'refreshed': refreshed, 'actions': actions}


def outside_git(path):
    resolved = Path(path).resolve()
    if any((parent / '.git').exists() for parent in (resolved.parent, *resolved.parents)):
        raise ImportFailure('generated_data_must_stay_outside_git')
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--organization-id', required=True)
    parser.add_argument('--connection-id', required=True)
    parser.add_argument('--contour', choices=['test', 'prod'], required=True)
    parser.add_argument('--targets', default='webhook,digest')
    parser.add_argument('--entity-type-id', type=int)
    parser.add_argument('--assigned-by-id', type=int)
    parser.add_argument('--include-text', action='store_true')
    args = parser.parse_args(argv)
    try:
        db, output = outside_git(args.db), outside_git(args.output)
        if output in {db, args.snapshot.resolve()}:
            raise ImportFailure('output_path_conflict')
        with args.snapshot.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ImportFailure('snapshot_too_large')
        result = prepare(json.loads(raw), db, connection=args.connection_id,
            organization=args.organization_id, contour=args.contour, targets=args.targets.split(','),
            entity_type_id=args.entity_type_id, assigned_by_id=args.assigned_by_id, include_text=args.include_text)
        output.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=output.parent)
        temporary = Path(name)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as file:
                os.chmod(name, 0o600)
                json.dump(result, file, ensure_ascii=False, indent=2)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        print(json.dumps({'delivery': 'disabled', 'actions': len(result['actions']),
            'queued_new': result['queued_new'], 'refreshed': result['refreshed']}))
        return 0
    except ImportFailure as error:
        print(json.dumps({'error': str(error), 'delivery': 'disabled'})); return 2
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print(json.dumps({'error': 'input_or_storage_failed', 'delivery': 'disabled'})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
