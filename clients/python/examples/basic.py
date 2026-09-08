"""Run against a local API: python examples/basic.py"""
import os
import time

from autobuy_client import ApiError, AutoBuyClient

client = AutoBuyClient(os.getenv("AUTOBUY_API_URL", "http://localhost:8000"),
                       on_tokens_changed=lambda t: print("tokens changed:", "logged in" if t else "logged out"))

email = f"demo-{int(time.time())}@example.com"
password = "Str0ng!Passw0rd"

client.register(email, password)
client.login(email, password)
print("me:", client.me())

try:
    purchase = client.create_purchase({"crypto": "BTC", "amount_usdt": 50, "frequency": "weekly"})
    print("created purchase", purchase["id"])
except ApiError as e:  # unverified KYC / missing exchange key
    print("purchase rejected:", e.status, e.detail)

txs = client.transactions.list_transactions(limit=10)
print("transactions:", txs["total"])
print("stats:", client.dashboard.quick_stats())

client.refresh_tokens()  # explicit refresh (normally automatic on 401)
client.logout()
client.close()
