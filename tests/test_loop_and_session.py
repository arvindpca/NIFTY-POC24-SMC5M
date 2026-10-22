import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import app.smartapi_client as sc
from app import loop
from app.loop import IST


class FakeApi:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def getCandleData(self, params):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


OK = {"status": True, "data": [["2026-01-01T09:15:00+05:30", 1, 2, 0.5, 1.5, 10]]}


class SessionTests(unittest.TestCase):
    def setUp(self):
        sc.reset_pacing()
        self.p = [mock.patch.object(sc, "_sleep", lambda s: None)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_rate_limit_retries_once_then_raises(self):
        api = FakeApi([
            Exception("Access denied because of exceeding access rate"),
            Exception("Too many requests"),
        ])
        with self.assertRaises(sc.SmartAPIRateLimitError):
            sc._get_candle_data_with_retry(api, {}, "1")
        self.assertEqual(api.calls, sc.MAX_RETRIES + 1)
        self.assertGreater(sc.current_gap(), sc.MIN_CALL_GAP_SEC)

    def test_rate_limit_retry_recovers(self):
        api = FakeApi([
            Exception("Access denied because of exceeding access rate"),
            OK,
        ])
        self.assertEqual(sc._get_candle_data_with_retry(api, {}, "1"), OK)
        self.assertEqual(api.calls, 2)

    def test_invalid_token_relogs_once_then_succeeds(self):
        class S(FakeApi):
            relogs = 0
            def relogin(self):
                S.relogs += 1
                return True
        api = S([{"status": False, "message": "Invalid Token", "data": None}, OK])
        self.assertEqual(sc._get_candle_data_with_retry(api, {}, "1"), OK)
        self.assertEqual(S.relogs, 1)

    def test_invalid_token_only_one_relogin_per_call(self):
        class S(FakeApi):
            relogs = 0
            def relogin(self):
                S.relogs += 1
                return True
        api = S([{"status": False, "message": "Invalid Token"}] * 3)
        with self.assertRaises(RuntimeError):
            sc._get_candle_data_with_retry(api, {}, "1")
        self.assertEqual(S.relogs, 1)

    def test_relogin_rate_limited(self):
        t = [1000.0]
        env = {k: "x" for k in ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]}
        with mock.patch.dict(os.environ, env):
            s = sc.SmartSession(lambda: mock.Mock(generateSession=lambda *a: {"status": True}), lambda: "123456", clock=lambda: t[0])
            s.login()
            self.assertFalse(s.relogin())  # too soon
            t[0] += 130
            self.assertTrue(s.relogin())
            self.assertEqual(s.login_count, 2)


class LoopTests(unittest.TestCase):
    def test_next_cycle_boundaries(self):
        n = datetime(2026, 10, 19, 10, 7, 30, tzinfo=IST)
        self.assertEqual(loop.next_cycle_time(n).strftime("%H:%M:%S"), "10:15:20")
        n = datetime(2026, 10, 19, 10, 15, 0, tzinfo=IST)
        self.assertEqual(loop.next_cycle_time(n).strftime("%H:%M"), "10:30")

    def test_market_open(self):
        self.assertTrue(loop.market_open(datetime(2026, 10, 19, 9, 15, tzinfo=IST)))
        self.assertFalse(loop.market_open(datetime(2026, 10, 17, 11, 0, tzinfo=IST)))  # Saturday
        self.assertFalse(loop.market_open(datetime(2026, 10, 19, 15, 40, tzinfo=IST)))

    def test_loop_runs_cycles_survives_failure_and_stops_at_close(self):
        clock = [datetime(2026, 10, 19, 14, 50, tzinfo=IST)]
        calls = []

        def fake_main(session):
            calls.append(session)
            if len(calls) == 1:
                raise RuntimeError("boom")

        sess = mock.Mock()
        with mock.patch.dict(os.environ, {"PAPER_ONLY": "true"}):
            rc = loop.run(loop_minutes=600, main_fn=fake_main, session_factory=lambda: sess,
                          now_fn=lambda: clock[0], sleep_fn=lambda s: clock.__setitem__(0, clock[0] + timedelta(seconds=s)))
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all(c is sess for c in calls))
        sess.terminateSession.assert_called_once()

    def test_loop_refuses_when_not_paper_only(self):
        with mock.patch.dict(os.environ, {"PAPER_ONLY": "false"}):
            with self.assertRaises(RuntimeError):
                loop.run(main_fn=lambda s: None)


if __name__ == "__main__":
    unittest.main()
