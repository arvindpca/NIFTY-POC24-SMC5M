import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
def get_config():
    if os.getenv("PAPER_ONLY", "true").lower() != "true":
        raise RuntimeError("Safety stop: PAPER_ONLY must be true")
    keys=["ANGEL_API_KEY","ANGEL_CLIENT_CODE","ANGEL_PIN","ANGEL_TOTP_SECRET","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"]
    missing=[k for k in keys if not os.getenv(k)]
    if missing: raise RuntimeError("Missing environment secrets: "+", ".join(missing))
    return {"api_key":os.environ["ANGEL_API_KEY"],"client_code":os.environ["ANGEL_CLIENT_CODE"],"pin":os.environ["ANGEL_PIN"],"totp_secret":os.environ["ANGEL_TOTP_SECRET"],"telegram_token":os.environ["TELEGRAM_BOT_TOKEN"],"telegram_chat_id":os.environ["TELEGRAM_CHAT_ID"],"symbol_csv":Path(os.getenv("SYMBOL_TOKEN_CSV","config/symbol_tokens.csv")),"data_dir":Path(os.getenv("DATA_DIR","data"))}
