class StrategyAdapter:
    """Safety gate: implement using the original Strategy B functions, then parity-test.

    Required exact integration: prepare_symbol(), detect_signals(), NIFTY 1H trend alignment,
    Strategy B filter tuple (NIFTY=True, stock HTF=False, POC=True, volume=False, wick=False),
    next-candle-open paper fill, fixed 1.5R, original SL buffer and entry/exit timing.
    Only completed candles may be used. Never pass future bars to a live decision.
    """
    validated = False
    def get_new_signals(self, *args, **kwargs):
        raise RuntimeError("Safety stop: Strategy B live adapter is not integrated/parity-tested. No signals emitted.")
