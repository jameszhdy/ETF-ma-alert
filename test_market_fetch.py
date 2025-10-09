# test_market_fetch.py
from run_market_alerts import fetch_from_eastmoney_kline
import datetime as dt

code = "511020"
today = dt.date.today()
start = (today - dt.timedelta(days=300)).strftime("%Y%m%d")
end = today.strftime("%Y%m%d")
s = fetch_from_eastmoney_kline(code, start, end)
print(code, "rows:", len(s))
print(s.tail(5))
