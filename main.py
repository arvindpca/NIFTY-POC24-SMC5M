import pandas as pd
from .config import get_config
from .smartapi_client import AngelDataClient
from .strategy_adapter import StrategyAdapter
def main():
    cfg=get_config()
    if not cfg["symbol_csv"].is_file(): raise FileNotFoundError(f"Create {cfg['symbol_csv']} with verified official symbol tokens; include NIFTY.")
    symbols=pd.read_csv(cfg["symbol_csv"],dtype=str).fillna("")
    if not {"symbol","token","exchange"}.issubset(symbols.columns): raise ValueError("CSV columns required: symbol,token,exchange")
    if not (symbols.symbol.str.upper()=="NIFTY").any(): raise ValueError("Verified NIFTY index token is required")
    AngelDataClient(cfg)
    print("Angel One data login successful. PAPER ONLY; no order placement is implemented.")
    StrategyAdapter().get_new_signals({},None)
if __name__=="__main__": main()
