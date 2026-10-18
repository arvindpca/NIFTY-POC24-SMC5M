import pandas as pd, pyotp
from SmartApi import SmartConnect
class AngelDataClient:
    def __init__(self,cfg):
        self.api=SmartConnect(api_key=cfg["api_key"])
        r=self.api.generateSession(cfg["client_code"],cfg["pin"],pyotp.TOTP(cfg["totp_secret"]).now())
        if not r or not r.get("status"): raise RuntimeError(f"Angel One login failed: {r}")
    def candles(self,token,exchange,interval,from_dt,to_dt):
        r=self.api.getCandleData({"exchange":exchange,"symboltoken":str(token),"interval":interval,"fromdate":from_dt.strftime("%Y-%m-%d %H:%M"),"todate":to_dt.strftime("%Y-%m-%d %H:%M")})
        if not r or not r.get("status") or not r.get("data"): raise RuntimeError(f"No candle data for token {token}: {r}")
        d=pd.DataFrame(r["data"],columns=["timestamp","open","high","low","close","volume"])
        d["timestamp"]=pd.to_datetime(d["timestamp"],errors="coerce")
        for k in ["open","high","low","close","volume"]: d[k]=pd.to_numeric(d[k],errors="coerce")
        return d.dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
