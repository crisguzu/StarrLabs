"""Pure policy tests: no FastAPI install, DB, server, network or bytecode writes.

Run: PYTHONDONTWRITEBYTECODE=1 timeout 20s prlimit --as=536870912 --cpu=15
     --fsize=16777216 python3 -m unittest discover -s tests -p test_access_policy.py
The minimal exception double is injected only while invoking the guard; production
imports always use the installed FastAPI HTTPException.
"""
import copy
import sys
import types
import unittest
from unittest.mock import Mock, patch

from access_policy import classify_route, require_standalone_access


class Denied(Exception):
    def __init__(self, status_code, detail, headers):
        self.status_code, self.detail, self.headers = status_code, detail, headers


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.dict(sys.modules, {"fastapi": types.SimpleNamespace(HTTPException=Denied)})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.email = Mock(return_value="owner@example.invalid")
        self.cur = Mock()
        self.cur.fetchone.return_value = (1,)
        self.conn = Mock()
        self.conn.cursor.return_value = self.cur
        self.connect = Mock(return_value=self.conn)

    def request(self, path, method="GET", session=None):
        return types.SimpleNamespace(url=types.SimpleNamespace(path=path), method=method,
                                     session={} if session is None else session,
                                     headers={"cf-access-authenticated-user-email": "spoof@example.invalid"})

    def run_guard(self, req):
        before = copy.deepcopy(req.session)
        try:
            return require_standalone_access(req, self.email, self.connect)
        finally:
            self.assertEqual(before, req.session, "guard must not rewrite session")
            self.conn.commit.assert_not_called()
            for call in self.cur.execute.call_args_list:
                self.assertTrue(call.args[0].startswith("SELECT "))

    def denied(self, req, code):
        with self.assertRaises(Denied) as result:
            self.run_guard(req)
        self.assertEqual(code, result.exception.status_code)
        self.assertEqual("no-store", result.exception.headers["Cache-Control"])
        return result.exception

    def test_public_routes_do_not_touch_identity_or_database(self):
        for path in ["/", "/login", "/how-it-works", "/us", "/event", "/static/app.js",
                     "/api/shared/token", "/api/shared/token/players", "/api/shared/token/meta"]:
            for method in ["GET", "HEAD"]:
                with self.subTest(path=path, method=method):
                    self.run_guard(self.request(path, method))
        for path in ["/api/auth/email/start", "/api/auth/email/verify"]:
            self.run_guard(self.request(path, "POST"))
        self.email.assert_not_called()
        self.connect.assert_not_called()

    def test_email_is_required_for_general_lookup_and_unknown_routes(self):
        self.email.return_value = None
        for path in ["/api/players/2P", "/api/player-daily-stats/2P", "/api/battles/2P/time",
                     "/api/refresh-status/job", "/api/collector-health", "/api/new-route"]:
            self.denied(self.request(path), 401)
        self.connect.assert_not_called()

    def test_selected_public_tag_does_not_grant_private_access(self):
        self.cur.fetchone.return_value = None
        for path, method in [("/api/players", "GET"),
                             ("/api/lists", "GET"), ("/api/lists/7/players", "GET"),
                             ("/api/lists/favorite", "POST"), ("/add-player", "POST"),
                             ("/api/my-club/select", "POST"), ("/api/shared/token/copy", "POST")]:
            with self.subTest(path=path):
                self.denied(self.request(path, method, {"owner_tag": "#2P", "selected_list_id": 7}), 403)
        sql, params = self.cur.execute.call_args.args
        self.assertIn("player_tag = %s AND identity_email = %s", sql)
        self.assertIn("status = 'verified'", sql)
        self.assertIn("revoked_at IS NULL", sql)
        self.assertEqual(("#2P", "owner@example.invalid"), params)

    def test_identity_email_not_session_or_header_controls_relation(self):
        self.email.return_value = "different@example.invalid"
        self.cur.fetchone.return_value = None
        self.denied(self.request("/api/lists", session={"owner_tag": "#2P", "identity_email": "owner@example.invalid"}), 403)
        self.assertEqual(("#2P", "different@example.invalid"), self.cur.execute.call_args.args[1])

    def test_revoked_or_expired_email_session_cannot_use_verified_player(self):
        self.email.return_value = None
        self.denied(self.request("/api/lists", session={"owner_tag": "#2P"}), 401)
        self.connect.assert_not_called()

    def test_current_relation_is_rechecked_and_revocation_denies_next_request(self):
        req = self.request("/api/lists", session={"owner_tag": "#2P"})
        self.run_guard(req)
        self.cur.fetchone.return_value = None
        self.denied(req, 403)
        self.assertEqual(2, self.cur.execute.call_count)
        self.assertEqual(2, self.conn.rollback.call_count)
        self.assertEqual(2, self.conn.close.call_count)

    def test_verified_relation_allows_private_reads_and_mutations(self):
        for path, method in [("/api/lists", "POST"),
                             ("/api/lists/9", "PATCH"), ("/api/shared/token/copy", "POST")]:
            self.run_guard(self.request(path, method, {"owner_tag": " 2p ", "selected_list_id": 999}))
        self.assertEqual(("#2P", "owner@example.invalid"), self.cur.execute.call_args.args[1])
        self.assertEqual(3, self.conn.rollback.call_count)
        self.assertEqual(3, self.cur.close.call_count)
        self.assertEqual(3, self.conn.close.call_count)

    def test_missing_or_malformed_owner_cannot_select_list_to_bypass(self):
        for owner in [None, "", 123, [], "#2P';DROP TABLE", "#2P/../login"]:
            self.denied(self.request("/api/lists", session={"owner_tag": owner, "selected_list_id": 1}), 403)
        self.connect.assert_not_called()

    def test_email_bootstrap_and_account_recovery_require_no_previous_player_proof(self):
        for path, method in [("/identify", "POST"), ("/identify/confirm", "POST"),
                             ("/verify-player/2P", "GET"), ("/api/player-verification/start", "POST"),
                             ("/api/player-verification/challenge", "GET"), ("/switch-player", "POST"),
                             ("/api/accounts/switch", "POST"), ("/api/accounts/unlink", "POST"),
                             ("/api/accounts/primary", "POST"), ("/api/accounts/audit", "GET")]:
            self.run_guard(self.request(path, method, {"owner_tag": "#2P"}))
        self.connect.assert_not_called()
        self.email.return_value = None
        self.denied(self.request("/identify", "POST"), 401)
        self.denied(self.request("/identify/confirm", "POST"), 401)
        self.denied(self.request("/api/player-verification/start", "POST"), 401)
        self.denied(self.request("/api/player-verification/challenge"), 401)

    def test_selected_root_redirects_to_login_or_verification_before_handler(self):
        req = self.request("/", session={"owner_tag": "#2P"})
        self.email.return_value = None
        self.assertEqual("/login?next=%2F", self.denied(req, 303).headers["Location"])
        self.connect.assert_not_called()
        self.email.return_value = "owner@example.invalid"
        self.cur.fetchone.return_value = None
        self.assertEqual("/verify-player/2P", self.denied(req, 303).headers["Location"])
        self.cur.fetchone.return_value = (1,)
        self.run_guard(req)

    def test_browsing_pages_require_email_but_no_owner(self):
        for path in ["/player/2P", "/battles/2P"]:
            self.run_guard(self.request(path))
            self.email.return_value = None
            self.assertTrue(self.denied(self.request(path), 303).headers["Location"].startswith("/login?next="))
            self.email.return_value = "owner@example.invalid"
        self.connect.assert_not_called()

    def test_public_exemptions_are_method_and_exact_path_limited(self):
        self.assertEqual("email", classify_route("GET", "/login/extra"))
        for path, method in [("/api/auth/email/verify/extra", "POST"),
                             ("/api/auth/new", "POST"), ("/event", "POST"),
                             ("/static/file", "POST"), ("/new-mutation", "PATCH"),
                             ("/api/shared/token/copy", "GET"), ("/api/shared/token/meta", "POST"),
                             ("/api/shared/token/players", "DELETE"), ("/api/lists", "OPTIONS"),
                             ("/api/lists/future-route", "POST")]:
            self.assertEqual("owner", classify_route(method, path))

    def test_database_failure_propagates_and_cleans_up_without_authorizing(self):
        self.cur.execute.side_effect = RuntimeError("DB unavailable")
        with self.assertRaisesRegex(RuntimeError, "DB unavailable"):
            self.run_guard(self.request("/api/lists", session={"owner_tag": "#2P"}))
        self.cur.close.assert_called_once()
        self.conn.rollback.assert_called_once()
        self.conn.close.assert_called_once()

    def test_cursor_failure_still_returns_connection(self):
        self.conn.cursor.side_effect = RuntimeError("cursor failed")
        with self.assertRaisesRegex(RuntimeError, "cursor failed"):
            self.run_guard(self.request("/api/lists", session={"owner_tag": "#2P"}))
        self.conn.rollback.assert_called_once()
        self.conn.close.assert_called_once()

    def test_missing_session_middleware_fails_closed(self):
        req = types.SimpleNamespace(url=types.SimpleNamespace(path="/api/lists"), method="GET")
        with self.assertRaises(AttributeError):
            require_standalone_access(req, self.email, self.connect)
        self.email.assert_not_called()
        self.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
