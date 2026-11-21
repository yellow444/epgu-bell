"""Example integration client, not the closed Bell collector. Python 3.11+."""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlencode

MAX_BYTES = 8 * 1024 * 1024


class ImportFailure(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_api(base_url: str, token_file: Path) -> dict:
    url = urlsplit(base_url)
    if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username or url.password or url.path not in {"", "/"}
            or url.query or url.fragment):
        raise ImportFailure("loopback_api_required")
    token = token_file.read_text(encoding="utf-8-sig").strip()
    if len(token) < 24 or any(c.isspace() for c in token):
        raise ImportFailure("invalid_local_token")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    items, seen_cursors = [], set()
    cursor, generation, first_snapshot = None, None, None
    total_bytes = 0
    for page_index in range(1000):
        query = '?' + urlencode({'cursor': cursor}) if cursor else ''
        request = urllib.request.Request(base_url.rstrip('/') + '/v1/notifications' + query,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
        try:
            with opener.open(request, timeout=15) as response:
                if response.headers.get_content_type() != 'application/json':
                    raise ImportFailure('api_response_not_json')
                raw = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise ImportFailure('api_http_' + str(error.code)) from None
        except urllib.error.URLError:
            raise ImportFailure('api_unavailable') from None
        total_bytes += len(raw)
        if len(raw) > MAX_BYTES or total_bytes > 64 * 1024 * 1024:
            raise ImportFailure('response_too_large')
        page = json.loads(raw)
        if not isinstance(page, dict) or not isinstance(page.get('items'), list):
            raise ImportFailure('invalid_api_page')
        pagination = page.get('pagination')
        if pagination is None:
            if page_index:
                raise ImportFailure('pagination_contract_changed')
            return page  # Older releases retain the guarded legacy import path.
        if not isinstance(pagination, dict) or type(pagination.get('has_more')) is not bool:
            raise ImportFailure('invalid_api_pagination')
        if page_index == 0:
            generation, first_snapshot = pagination.get('generation'), page.get('snapshot')
        if pagination.get('generation') != generation or page.get('snapshot') != first_snapshot:
            raise ImportFailure('page_snapshot_changed')
        items.extend(page['items'])
        if len(items) > 100000:
            raise ImportFailure('api_record_limit')
        if not pagination['has_more']:
            if pagination.get('next_cursor') is not None:
                raise ImportFailure('invalid_api_pagination')
            return {**page, 'items': items}
        cursor = pagination.get('next_cursor')
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 or cursor in seen_cursors or not page['items']:
            raise ImportFailure('api_cursor_stalled')
        seen_cursors.add(cursor)
    raise ImportFailure('api_page_limit')


def decoded(value: dict) -> str:
    if not isinstance(value, dict) or value.get("encoding") != "base64" or value.get("charset") != "utf-8":
        raise ImportFailure("invalid_text_envelope")
    try:
        return base64.b64decode(value["data"], validate=True).decode("utf-8", errors="strict")
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
        raise ImportFailure("invalid_base64_utf8") from None


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise ImportFailure("invalid_observation_time") from None


def import_payload(payload: dict, database: Path, *, connection: str, organization: str, contour: str) -> dict:
    """Upsert a bounded window. Never delete records absent from that window."""
    snapshot = payload.get("snapshot") if "items" in payload else payload
    if not isinstance(snapshot, dict):
        raise ImportFailure("no_snapshot")
    context = snapshot.get("context", {})
    scope = {"connection_id": connection, "organization_id": organization, "contour": contour}
    if any(context.get(k) != v for k, v in scope.items()) or context.get("subject_type") == "person":
        raise ImportFailure("context_mismatch")
    if snapshot.get("schema_version") not in {"1.0", "1.2", "1.3"}:
        raise ImportFailure("unsupported_schema")
    if snapshot.get("status") == "needs_user_action" or context.get("verified") is not True:
        raise ImportFailure("needs_user_action_previous_records_preserved")
    if snapshot.get("status") not in {"succeeded", "partial"}:
        raise ImportFailure("unknown_snapshot_status")
    if "items" in payload:
        items = payload["items"]
        pagination = payload.get('pagination')
        if pagination is not None and (not isinstance(pagination, dict) or pagination.get('has_more') is not False or pagination.get('next_cursor') is not None):
            raise ImportFailure('api_page_incomplete')
        if not isinstance(items, list) or len(items) > 100000 or (len(items) >= 1000 and pagination is None):
            raise ImportFailure("api_window_limit_no_pagination")
    else:
        records = snapshot.get("notifications")
        if not isinstance(records, list) or len(records) > (2000 if snapshot.get('schema_version') == '1.3' else 200):
            raise ImportFailure("invalid_snapshot_records")
        items = [{"record": r, "first_seen_at": snapshot.get("observed_at"),
                  "last_seen_at": snapshot.get("observed_at")} for r in records]
    normalized, identifiers = [], set()
    # Decode and check the whole batch before opening the write transaction.
    for item in items:
        record = item.get("record", {})
        identifier = record.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"ntf_[a-f0-9]{64}", identifier):
            raise ImportFailure("invalid_notification_id")
        if identifier in identifiers:
            raise ImportFailure("duplicate_id_in_batch")
        identifiers.add(identifier)
        title, body, sender = (decoded(record.get(k)) for k in ("title", "body", "sender"))
        if record.get("read_state") not in {"read", "unread", "unknown"}:
            raise ImportFailure("invalid_read_state")
        if not all(isinstance(item.get(k), str) and item[k] for k in ("first_seen_at", "last_seen_at")):
            raise ImportFailure("missing_observation_time")
        if timestamp(item["first_seen_at"]) > timestamp(item["last_seen_at"]):
            raise ImportFailure("invalid_observation_time")
        normalized.append((identifier, title, body, sender,
            json.dumps(record, ensure_ascii=False, sort_keys=True), item["first_seen_at"], item["last_seen_at"]))
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    inserted = updated = unchanged = 0
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS integration_scope(value TEXT NOT NULL)")
        encoded_scope = json.dumps(scope, sort_keys=True)
        prior = db.execute("SELECT value FROM integration_scope").fetchone()
        if prior and prior[0] != encoded_scope:
            raise ImportFailure("database_context_mismatch")
        if not prior:
            db.execute("INSERT INTO integration_scope VALUES (?)", (encoded_scope,))
        db.execute("""CREATE TABLE IF NOT EXISTS inbox(
            notification_id TEXT PRIMARY KEY, title TEXT, body TEXT, sender TEXT,
            record_json TEXT, first_seen_at TEXT, last_seen_at TEXT)""")
        for row in normalized:
            old = db.execute("SELECT record_json,last_seen_at FROM inbox WHERE notification_id=?", (row[0],)).fetchone()
            if old is None:
                db.execute("INSERT INTO inbox VALUES (?,?,?,?,?,?,?)", row)
                inserted += 1
            else:
                if timestamp(row[6]) < timestamp(old[1]):
                    unchanged += 1
                    continue
                if old[0] == row[4]:
                    unchanged += 1
                else:
                    updated += 1
                db.execute("UPDATE inbox SET title=?,body=?,sender=?,record_json=?,last_seen_at=? WHERE notification_id=?",
                           (row[1], row[2], row[3], row[4], row[6], row[0]))
        total = db.execute("SELECT count(*) FROM inbox").fetchone()[0]
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "stored": total,
            "snapshot_status": snapshot["status"], "full_history": False,
            "scope": scope, "collection_triggered": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="One-shot Bell integration example; no scheduler")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path)
    source.add_argument("--base-url")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--contour", required=True, choices=["test", "prod"])
    args = parser.parse_args(argv)
    if args.base_url and not args.token_file:
        parser.error("--token-file is required with --base-url")
    try:
        if args.snapshot:
            with args.snapshot.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ImportFailure("snapshot_too_large")
            payload = json.loads(raw)
        else:
            payload = read_api(args.base_url, args.token_file)
        report = import_payload(payload, args.db, connection=args.connection_id,
            organization=args.organization_id, contour=args.contour)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except ImportFailure as error:
        print(json.dumps({"error": str(error), "collection_triggered": False}))
        return 2
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        # Never print payloads, tokens, file contents or database records on error.
        print(json.dumps({"error": "input_or_storage_failed", "collection_triggered": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
