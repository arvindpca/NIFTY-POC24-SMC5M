
import os
import pyotp
from SmartApi import SmartConnect

def create_data_client():
    api_key = os.getenv("ANGEL_API_KEY")
    client_code = os.getenv("ANGEL_CLIENT_CODE")
    pin = os.getenv("ANGEL_PIN")
    totp_secret = os.getenv("ANGEL_TOTP_SECRET")

    if not all([api_key, client_code, pin, totp_secret]):
        raise RuntimeError("Angel One data credentials are not configured.")

    api = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(totp_secret).now()

    session = api.generateSession(client_code, pin, totp)
    if not session or not session.get("status"):
        raise RuntimeError("Angel One login failed.")

    return api
