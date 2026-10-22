"""Regression tests for completed 15-minute SmartAPI candle label normalization."""
import unittest
from unittest.mock import patch
import pandas as pd

from app.smartapi_client import fetch_15m_candles, IST


class SmartAPICandleLabelTests(unittest.TestCase):
    @patch("app.smartapi_client._get_candle_data_with_retry")
    def test_15m_candles_are_end_labelled_and_forming_bar_is_removed(self, mock_fetch):
        now = pd.Timestamp.now(tz=IST).floor("15min")
        starts = pd.date_range(end=now - pd.Timedelta(minutes=15), periods=80, freq="15min")
        # The last item is a currently forming bar, which must not be returned.
        starts = starts.append(pd.DatetimeIndex([now]))
        rows = []
        for i, ts in enumerate(starts):
            price = 100.0 + i
            rows.append([ts.strftime("%Y-%m-%d %H:%M:%S"), price, price + 1, price - 1, price + 0.5, 1000])
        mock_fetch.return_value = {"status": True, "data": rows}

        result = fetch_15m_candles(object(), "NSE", "123", lookback_days=10)

        self.assertEqual(len(result), 80)
        self.assertEqual(result.iloc[0]["timestamp"], starts[0] + pd.Timedelta(minutes=15))
        self.assertEqual(result.iloc[-1]["timestamp"], starts[79] + pd.Timedelta(minutes=15))
        self.assertNotIn(now + pd.Timedelta(minutes=15), set(result["timestamp"]))
        self.assertTrue((result["timestamp"].diff().dropna() == pd.Timedelta(minutes=15)).all())


if __name__ == "__main__":
    unittest.main()
