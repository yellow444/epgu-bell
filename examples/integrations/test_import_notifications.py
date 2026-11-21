"""Synthetic acceptance of the example client. No portal, sessions or 1C."""
import base64
import copy
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from import_notifications import ImportFailure, import_payload, read_api

FIXTURE = Path(__file__).resolve().parents[1] / "snapshot.mock.json"
SCOPE = dict(connection="demo", organization="ORG-DEMO", contour="test")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "inbox.sqlite3"
        self.snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def ingest(self, payload=None, **scope):
        return import_payload(self.snapshot if payload is None else payload, self.db, **{**SCOPE, **scope})

    def rows(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT notification_id,title,body,record_json,last_seen_at FROM inbox ORDER BY notification_id").fetchall()

    def test_two_imports_do_not_duplicate_and_preserve_plain_text(self):
        self.assertEqual(self.ingest()["inserted"], 2)
        second = self.ingest()
        self.assertEqual((second["inserted"], second["unchanged"], second["stored"]), (0, 2, 2))
        texts = [row[2] for row in self.rows()]
        self.assertTrue(any("<script>" in text for text in texts))
        self.assertFalse(second["full_history"])

    def test_record_change_updates_existing_id(self):
        self.ingest()
        changed = copy.deepcopy(self.snapshot)
        changed["notifications"][0]["read_state"] = "read"
        changed["observed_at"] = "2026-11-13T10:00:00Z"
        report = self.ingest(changed)
        self.assertEqual((report["updated"], report["stored"]), (1, 2))

    def test_empty_window_does_not_delete_and_is_not_full_history(self):
        self.ingest()
        empty = {**self.snapshot, "notifications": []}
        report = self.ingest(empty)
        self.assertEqual(report["stored"], 2)
        self.assertFalse(report["full_history"])

    def test_bad_base64_in_last_record_does_not_partially_update(self):
        self.ingest()
        before = self.rows()
        bad = copy.deepcopy(self.snapshot)
        bad["notifications"][0]["title"]["data"] = base64.b64encode(b"changed").decode()
        bad["notifications"][1]["body"]["data"] = "!!!!"
        with self.assertRaisesRegex(ImportFailure, "invalid_base64"):
            self.ingest(bad)
        self.assertEqual(self.rows(), before)

    def test_invalid_utf8_rejected(self):
        bad = copy.deepcopy(self.snapshot)
        bad["notifications"][0]["body"]["data"] = "/w=="
        with self.assertRaisesRegex(ImportFailure, "invalid_base64_utf8"):
            self.ingest(bad)
        self.assertFalse(self.db.exists())

    def test_foreign_context_rejected_before_write(self):
        with self.assertRaisesRegex(ImportFailure, "context_mismatch"):
            self.ingest(organization="OTHER")
        self.assertFalse(self.db.exists())

    def test_database_scope_cannot_be_reused_by_other_organization(self):
        self.ingest()
        changed = copy.deepcopy(self.snapshot)
        changed["context"]["organization_id"] = "OTHER"
        with self.assertRaisesRegex(ImportFailure, "database_context_mismatch"):
            self.ingest(changed, organization="OTHER")
        self.assertEqual(len(self.rows()), 2)

    def test_expired_latest_snapshot_keeps_previous_consumer_data(self):
        self.ingest()
        before = self.rows()
        failed = {**self.snapshot, "status": "needs_user_action", "notifications": []}
        payload = {"snapshot": failed, "items": [{"record": row} for row in self.snapshot["notifications"]]}
        with self.assertRaisesRegex(ImportFailure, "needs_user_action"):
            self.ingest(payload)
        self.assertEqual(self.rows(), before)

    def test_hard_api_window_limit_is_not_treated_as_complete(self):
        with self.assertRaisesRegex(ImportFailure, "api_window_limit"):
            self.ingest({"snapshot": self.snapshot, "items": [{}] * 1000})
        self.assertFalse(self.db.exists())

    def test_unknown_schema_and_unverified_context_rejected(self):
        for field, value in [("schema_version", "9.0"), ("context", {**self.snapshot["context"], "verified": False})]:
            with self.subTest(field=field), self.assertRaises(ImportFailure):
                self.ingest({**self.snapshot, field: value})

    def test_old_snapshot_does_not_overwrite_a_newer_record(self):
        later = copy.deepcopy(self.snapshot)
        later["observed_at"] = "2026-11-13T10:00:00Z"
        later["notifications"][0]["read_state"] = "read"
        self.ingest(later)
        before = self.rows()
        self.ingest()
        self.assertEqual(self.rows(), before)

    def test_duplicate_ids_in_batch_rejected(self):
        bad = copy.deepcopy(self.snapshot)
        bad["notifications"][1]["id"] = bad["notifications"][0]["id"]
        with self.assertRaisesRegex(ImportFailure, "duplicate_id"):
            self.ingest(bad)

    def test_loopback_http_and_redirect_refusal(self):
        snapshot = self.snapshot
        seen = []
        token = "example-only-" + "x" * 24
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if len(seen) == 1:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"snapshot": snapshot, "items": []}).encode())
                else:
                    self.send_response(302)
                    self.send_header("Location", "/not-followed")
                    self.end_headers()
        service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=service.serve_forever)
        worker.start()
        try:
            token_file = Path(self.temp.name) / "token.txt"
            token_file.write_text(token, encoding="utf-8")
            url = "http://127.0.0.1:" + str(service.server_port)
            self.assertEqual(read_api(url, token_file)["snapshot"], self.snapshot)
            with self.assertRaisesRegex(ImportFailure, "api_http_302"):
                read_api(url, token_file)
            self.assertEqual(seen, [("/v1/notifications", "Bearer " + token)] * 2)
        finally:
            service.shutdown()
            service.server_close()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

    def test_remote_url_refused_before_reading_token(self):
        with self.assertRaisesRegex(ImportFailure, "loopback_api_required"):
            read_api("https://example.com", Path("not-a-real-token"))

    def test_cursor_following_before_import(self):
        snapshot=self.snapshot;generation='generation-one';seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                seen.append(self.path)
                second='cursor=' in self.path
                body={'snapshot':snapshot,'items':[{'record':snapshot['notifications'][int(second)],
                    'first_seen_at':snapshot['observed_at'],'last_seen_at':snapshot['observed_at']}],
                    'pagination':{'generation':generation,'has_more':not second,'next_cursor':None if second else 'cursor-one'}}
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps(body).encode())
        service=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=service.serve_forever);worker.start()
        try:
            token_file=Path(self.temp.name)/'token.txt';token_file.write_text('x'*32)
            payload=read_api('http://127.0.0.1:'+str(service.server_port),token_file)
            assert seen==['/v1/notifications','/v1/notifications?cursor=cursor-one']
            assert self.ingest(payload)['stored']==2
        finally:service.shutdown();service.server_close();worker.join(5)

    def test_incomplete_api_page_rejected_before_write(self):
        with self.assertRaisesRegex(ImportFailure,'api_page_incomplete'):
            self.ingest({'snapshot':self.snapshot,'items':[], 'pagination':{'has_more':True,'next_cursor':'next'}})
        self.assertFalse(self.db.exists())

    def test_page_generation_change_and_repeated_cursor_are_rejected(self):
        snapshot=self.snapshot;mode='generation';seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                second='cursor=' in self.path
                generation='changed' if second and mode=='generation' else 'one'
                body={'snapshot':snapshot,'items':[{'record':snapshot['notifications'][0]}],
                    'pagination':{'generation':generation,'has_more':True,'next_cursor':'same-cursor'}}
                seen.append(self.path);self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps(body).encode())
        service=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=service.serve_forever);worker.start()
        try:
            token_file=Path(self.temp.name)/'token.txt';token_file.write_text('x'*32)
            url='http://127.0.0.1:'+str(service.server_port)
            with self.assertRaisesRegex(ImportFailure,'page_snapshot_changed'):read_api(url,token_file)
            mode='repeat'
            with self.assertRaisesRegex(ImportFailure,'api_cursor_stalled'):read_api(url,token_file)
            assert len(seen)==4 and not self.db.exists()
        finally:service.shutdown();service.server_close();worker.join(5)


if __name__ == "__main__":
    unittest.main()
