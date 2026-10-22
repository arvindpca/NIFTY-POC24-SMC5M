"""Tests for Yahoo 15m fallback normalization used during SmartAPI cooldown."""
import unittest
from unittest.mock import patch, Mock
import pandas as pd

from app.smartapi_client import fetch_yahoo_15m_candles, IST


class YahooFallbackTests(unittest.TestCase):
    @patch("app.smartapi_client.requests.get")
    def test_yahoo_15m_is_end_labelled_and_drops_forming_bar(self, get):
        now = pd.Timestamp.now(tz=IST).floor("15min")
        # 80 completed bars plus one interval that is still forming.
        starts = pd.date_range(end=now, periods=81, freq="15min")
        stamps = [int(x.timestamp()) for x in starts]
        prices = [100.0 + i for i in range(len(stamps))]
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "chart": {
                "error": None,
                "result": [{
                    "timestamp": stamps,
                    "indicators": {"quote": [{
                        "open": prices,
                        "high": [p + 1 for p in prices],
                        "low": [p - 1 for p in prices],
                        "close": [p + 0.5 for p in prices],
                        "volume": [1000] * len(prices),
                    }]},
                }],
            }
        }
        get.return_value = response

        result = fetch_yahoo_15m_candles("NIFTY", lookback_days=10)

        self.assertEqual(len(result), 80)
        self.assertEqual(result.iloc[0]["timestamp"], starts[0] + pd.Timedelta(minutes=15))
        self.assertEqual(result.iloc[-1]["timestamp"], starts[-2] + pd.Timedelta(minutes=15))
        self.assertTrue((result["timestamp"].diff().dropna() == pd.Timedelta(minutes=15)).all())
        self.assertIn("^NSEI", get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
