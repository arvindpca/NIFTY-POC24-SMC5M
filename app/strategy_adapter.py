
"""Strategy B signal adapter.

Live signals remain disabled until the original strategy is integrated
and validated against historical results.
"""

class StrategyAdapter:
    def __init__(self):
        self.validated = False

    def get_new_signals(self, market_data):
        if not self.validated:
            raise RuntimeError(
                "Strategy B adapter is not validated. "
                "No paper-trading signals may be generated."
            )

        raise NotImplementedError(
            "Connect the original Strategy B signal logic here."
        )
