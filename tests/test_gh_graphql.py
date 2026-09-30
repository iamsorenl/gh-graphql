import json
import os
import tempfile
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import gh_graphql as g


class Fake:
    """Local server replaying scripted responses: (status, headers, body)."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.requests.append(json.loads(self.rfile.read(n)))
                status, headers, body = outer.script.pop(0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/graphql"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def ok(data):
    return (200, {}, {"data": data})


def page(nodes, cursor=None):
    return ok({"repository": {"issues": {
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        "nodes": nodes}}})


class Base(unittest.TestCase):
    def serve(self, script):
        f = Fake(script)
        self.addCleanup(f.close)
        for p in (mock.patch.object(g, "API_URL", f.url),
                  mock.patch.object(g, "_sleep")):
            p.start()
            self.addCleanup(p.stop)
        self.sleep = g._sleep
        return f


class QueryTests(Base):
    def test_success(self):
        f = self.serve([ok({"viewer": {"login": "x"}})])
        self.assertEqual(g.query("{viewer{login}}", {"a": 1}, token="t"),
                         {"viewer": {"login": "x"}})
        self.assertEqual(f.requests[0]["variables"], {"a": 1})

    def test_retry_then_success(self):
        self.serve([(502, {}, {}), (503, {}, {}), ok({"a": 1})])
        self.assertEqual(g.query("q", token="t"), {"a": 1})
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])

    def test_gives_up(self):
        self.serve([(500, {}, {})] * 2)
        with self.assertRaises(Exception):
            g.query("q", tries=2, token="t")

    def test_client_error_not_retried(self):
        self.serve([(401, {}, {"message": "bad"})])
        with self.assertRaises(Exception):
            g.query("q", token="t")
        self.sleep.assert_not_called()

    def test_rate_limit_uses_reset_header(self):
        self.serve([(403, {"x-ratelimit-remaining": "0",
                           "x-ratelimit-reset": "1100"}, {}), ok({"a": 1})])
        with mock.patch.object(g, "_now", return_value=1000):
            self.assertEqual(g.query("q", token="t"), {"a": 1})
        self.sleep.assert_called_once_with(101)

    def test_rate_limit_default_60(self):
        self.serve([(429, {}, {}), ok({"a": 1})])
        g.query("q", token="t")
        self.sleep.assert_called_once_with(60.0)

    def test_graphql_rate_limited_error(self):
        self.serve([(200, {"x-ratelimit-reset": "1030"},
                     {"errors": [{"type": "RATE_LIMITED"}]}), ok({"a": 1})])
        with mock.patch.object(g, "_now", return_value=1000):
            self.assertEqual(g.query("q", token="t"), {"a": 1})
        self.sleep.assert_called_once_with(31)

    def test_errors_without_data_raise(self):
        self.serve([(200, {}, {"errors": [{"message": "boom"}]})])
        with self.assertRaises(g.GraphQLError) as cm:
            g.query("q", token="t")
        self.assertEqual(cm.exception.errors, [{"message": "boom"}])

    def test_partial_data_returned_with_warning(self):
        self.serve([(200, {}, {"data": {"a": 1}, "errors": [{"message": "x"}]})])
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            self.assertEqual(g.query("q", token="t"), {"a": 1})
        self.assertEqual(len(w), 1)


class PaginateTests(Base):
    Q = "query($cursor:String){repository{issues(first:2,after:$cursor){nodes{number}}}}"

    def test_three_pages(self):
        f = self.serve([page([{"n": 1}, None], "c1"), page([{"n": 2}], "c2"),
                        page([{"n": 3}])])
        got = list(g.paginate(self.Q, {"o": "x"}, "repository.issues", token="t"))
        self.assertEqual([n["n"] for n in got], [1, 2, 3])
        self.assertEqual([r["variables"]["cursor"] for r in f.requests],
                         [None, "c1", "c2"])
        self.assertEqual(f.requests[0]["variables"]["o"], "x")

    def test_checkpoint_resume_after_crash(self):
        ck = os.path.join(tempfile.mkdtemp(), "ck")
        self.serve([page([{"n": 1}], "c1"), page([{"n": 2}], "c2")])
        it = g.paginate(self.Q, {}, "repository.issues", checkpoint=ck, token="t")
        self.assertEqual(next(it), {"n": 1})
        self.assertEqual(next(it), {"n": 2})  # page 1 consumed -> cursor saved
        with open(ck) as f:
            self.assertEqual(f.read(), "c1")
        it.close()  # simulated crash while page 2 was being processed
        f = self.serve([page([{"n": 2}], "c2"), page([{"n": 3}])])
        got = list(g.paginate(self.Q, {}, "repository.issues", checkpoint=ck,
                              token="t"))
        self.assertEqual([n["n"] for n in got], [2, 3])
        self.assertEqual(f.requests[0]["variables"]["cursor"], "c1")
        self.assertFalse(os.path.exists(ck))

    def test_bad_path_raises(self):
        self.serve([page([])])
        with self.assertRaises(g.GraphQLError):
            list(g.paginate(self.Q, {}, "repository.nope", token="t"))


class TokenTests(unittest.TestCase):
    def run_token(self, env, gh_out):
        res = mock.Mock(stdout=gh_out)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(g.subprocess, "run", return_value=res) as run:
            try:
                return g.token(), run
            except RuntimeError as e:
                return e, run

    def test_order(self):
        self.assertEqual(self.run_token({"GITHUB_TOKEN": "a", "GH_TOKEN": "b"}, "c")[0], "a")
        self.assertEqual(self.run_token({"GH_TOKEN": "b"}, "c")[0], "b")
        t, run = self.run_token({}, "c\n")
        self.assertEqual(t, "c")
        run.assert_called_once()

    def test_none(self):
        e, _ = self.run_token({}, "")
        self.assertIsInstance(e, RuntimeError)

    def test_gh_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(g.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(RuntimeError):
                g.token()


if __name__ == "__main__":
    unittest.main()
