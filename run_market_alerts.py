#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_market_alerts.py (增强版：东财 -> 新浪 Kline -> akshare 回退)
针对场内品种（ETF/股票/指数）抓取日线并检测是否低于 MA
"""
import os
import json
import time
import logging
import argparse
import datetime as dt
from typing import List, Dict

import requests
import pandas as pd
import numpy as np

# akshare 作为最后备选（可选安装）
try:
    import akshare as ak
    HAS_AKSHARE = True
except Exception:
    HAS_AKSHARE = False

LOG = logging.getLogger("market_alerts")
LOG.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
LOG.addHandler(handler)

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(THIS_DIR, "watch_config.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}


def load_config(path=CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件 {path} 未找到")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------- 东财 push2his（日线） ----------------
def fetch_from_eastmoney_kline(code: str, start_date: str, end_date: str, timeout=10) -> pd.Series:
    """
    用 push2his 获取日线
    start_date/end_date 格式 YYYYMMDD
    若无法获取返回 raise RuntimeError
    """
    base = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params_template = {
        "beg": start_date,
        "end": end_date,
        "rtntype": "6",
        "klt": "101",   # 日线
        "fqt": "1"      # 复权：1 前复权（可改）
    }

    last_resp_text = None
    last_exc = None
    # 先试 1.code 再试 0.code
    for market_prefix in ("1", "0"):
        secid = f"{market_prefix}.{code}"
        params = dict(params_template)
        params["secid"] = secid
        headers = HEADERS.copy()
        headers["Referer"] = f"https://quote.eastmoney.com/{code}.html"
        try:
            r = requests.get(base, params=params, headers=headers, timeout=timeout)
            if r.status_code != 200:
                last_resp_text = f"HTTP {r.status_code}"
                last_exc = Exception(f"HTTP {r.status_code}")
                LOG.debug("东财 %s 返回状态 %s", secid, r.status_code)
                time.sleep(0.15)
                continue
            j = r.json()
            data = j.get("data") or {}
            klines = data.get("klines") or []
            last_resp_text = r.text[:2000]
            if not klines:
                LOG.debug("东财 %s 返回但 klines 为空（前500字符示例）: %s", secid, last_resp_text[:500])
                time.sleep(0.15)
                continue

            dates = []
            closes = []
            for k in klines:
                parts = k.split(",")
                if not parts:
                    continue
                raw_date = parts[0].strip()
                try:
                    if "-" in raw_date:
                        d = pd.to_datetime(raw_date)
                    else:
                        d = pd.to_datetime(raw_date, format="%Y%m%d")
                except Exception:
                    continue
                close = None
                for idx in (2, 1, 3, 4):
                    if len(parts) > idx:
                        try:
                            close = float(parts[idx])
                            break
                        except Exception:
                            continue
                if close is None:
                    continue
                dates.append(d.normalize())
                closes.append(close)

            if not closes:
                LOG.debug("东财 %s klines 存在但未解析出收盘价", secid)
                time.sleep(0.15)
                continue

            series = pd.Series(closes, index=pd.to_datetime(dates)).sort_index()
            return series
        except Exception as e:
            last_exc = e
            LOG.debug("东财请求 %s 异常: %s", secid, e)
            time.sleep(0.15)
            continue

    raise RuntimeError(f"未能通过东财 push2his 获取 {code} 的日线数据（最后响应片段：{str(last_resp_text)[:300]}，最后异常：{last_exc}）")


# ---------------- 新浪 CN_MarketData.getKLineData（回退） ----------------
def fetch_from_sina_kline(code: str, start_date: str, end_date: str, timeout=10) -> pd.Series:
    """
    用新浪的历史 K 线接口回退抓取（常用而可靠）
    - 尝试 symbol 形式 sh{code} 和 sz{code}
    - scale=240 表示日线； datalen 最多 1023
    返回 pd.Series(date-index, close)
    """
    base = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    # 计算 datalen ：保证包含我们需要的天数，但上限 1023
    try:
        s_dt = pd.to_datetime(start_date, format="%Y%m%d")
        e_dt = pd.to_datetime(end_date, format="%Y%m%d")
        days = (e_dt - s_dt).days + 10
        datalen = min(1023, max(100, days))
    except Exception:
        datalen = 1023

    for prefix in ("sh", "sz"):
        symbol = f"{prefix}{code}"
        params = {
            "symbol": symbol,
            "scale": "240",   # 日线
            "ma": "no",
            "datalen": str(datalen)
        }
        headers = HEADERS.copy()
        headers["Referer"] = f"https://finance.sina.com.cn/"
        try:
            r = requests.get(base, params=params, headers=headers, timeout=timeout)
            if r.status_code != 200:
                LOG.debug("新浪 %s 返回 HTTP %s", symbol, r.status_code)
                time.sleep(0.15)
                continue
            txt = r.text.strip()
            if not txt or txt in ("null", "[]"):
                LOG.debug("新浪 %s 返回空: %s", symbol, txt[:200])
                time.sleep(0.15)
                continue
            # 解析 JSON（多数情况下是合法 JSON）
            try:
                data = json.loads(txt)
            except Exception:
                # 尝试用 ast.literal_eval 作为容错
                import ast
                try:
                    data = ast.literal_eval(txt)
                except Exception as e:
                    LOG.debug("新浪 %s JSON 解析失败: %s", symbol, e)
                    time.sleep(0.15)
                    continue
            # data 应为 list of dict with keys like day/open/high/low/close/volume
            if not isinstance(data, list) or len(data) == 0:
                LOG.debug("新浪 %s 返回数据结构不符合预期（示例前200字符）：%s", symbol, txt[:200])
                time.sleep(0.15)
                continue
            dates = []
            closes = []
            for row in data:
                # row 可能是 dict 或类似对象
                try:
                    day = row.get("day") or row.get("date") or row.get("time") or row.get("Day")
                    close = row.get("close") or row.get("closeprice") or row.get("yclose") or row.get("price")
                    if day is None or close is None:
                        continue
                    d = pd.to_datetime(str(day))
                    c = float(close)
                    dates.append(d.normalize())
                    closes.append(c)
                except Exception:
                    continue
            if not closes:
                LOG.debug("新浪 %s 解析后无有效收盘价", symbol)
                time.sleep(0.15)
                continue
            ser = pd.Series(closes, index=pd.to_datetime(dates)).sort_index()
            return ser
        except Exception as e:
            LOG.debug("新浪 %s 请求异常: %s", symbol, e)
            time.sleep(0.15)
            continue

    raise RuntimeError(f"新浪未能返回 {code} 的 K 线数据")


# ---------------- akshare 备选（如已安装） ----------------
def fetch_from_akshare(code: str, start_date: str, end_date: str) -> pd.Series:
    if not HAS_AKSHARE:
        raise RuntimeError("akshare 未安装作为回退")
    # 试 sh/sz 前缀（ak 的 symbol 形式例如 sh600519 或 sz000001）
    for prefix in ("sh", "sz"):
        symbol = f"{prefix}{code}"
        try:
            # ak.stock_zh_a_daily 可能存在版本差异：尝试不同接口名
            # 使用较通用的 ak.stock_zh_a_daily
            df = ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                continue
            # 兼容字段
            if "close" in df.columns and ("date" in df.columns or df.index.name):
                idx = pd.to_datetime(df["date"]) if "date" in df.columns else pd.to_datetime(df.index)
                ser = pd.Series(df["close"].values, index=idx)
            elif "收盘" in df.columns:
                idx = pd.to_datetime(df["日期"]) if "日期" in df.columns else pd.to_datetime(df.index)
                ser = pd.Series(df["收盘"].values, index=idx)
            else:
                # 尝试第一个数值列
                float_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
                if not float_cols:
                    continue
                idx = pd.to_datetime(df.index)
                ser = pd.Series(df[float_cols[0]].values, index=idx)
            ser.index = pd.to_datetime(ser.index).normalize()
            return ser.sort_index()
        except Exception as e:
            LOG.debug("akshare %s (%s) 失败: %s", prefix, code, e)
            time.sleep(0.15)
            continue
    raise RuntimeError("akshare 未能获取数据")


# ---------------- 批量抓取：按优先级 东财 -> 新浪 -> akshare ----------------
def fetch_series_for_codes(codes: List[str], start_date: dt.date, end_date: dt.date) -> pd.DataFrame:
    start_s = start_date.strftime("%Y%m%d")
    end_s = end_date.strftime("%Y%m%d")
    series_list = []
    for c in codes:
        LOG.info("拉取 %s: %s -> %s", c, start_s, end_s)
        s = None
        # 1) 东财
        try:
            s = fetch_from_eastmoney_kline(c, start_s, end_s)
            LOG.info(" 东财成功: %s (%d 行)", c, len(s))
        except Exception as e:
            LOG.warning(" 东财失败: %s (回退新浪) 详细: %s", c, e)
            # 2) 新浪
            try:
                s = fetch_from_sina_kline(c, start_s, end_s)
                LOG.info(" 新浪回退成功: %s (%d 行)", c, len(s))
            except Exception as e2:
                LOG.warning(" 新浪也失败: %s (尝试 akshare) 详细: %s", c, e2)
                # 3) akshare
                try:
                    s = fetch_from_akshare(c, start_s, end_s)
                    LOG.info(" akshare 回退成功: %s (%d 行)", c, len(s))
                except Exception as e3:
                    LOG.error(" akshare 也失败: %s", e3)
                    s = None
        if s is not None and len(s) > 0:
            s.name = c
            series_list.append(s)
        else:
            LOG.error(" 拉取 %s 失败: 未能获取任何日线数据", c)
        time.sleep(0.2)
    if not series_list:
        raise ValueError("没有任何品种成功获取数据。")
    df = pd.concat(series_list, axis=1).sort_index()
    df.index = pd.to_datetime(df.index).normalize()
    return df


# ---------------- 推送（Server酱 + 企业微信 备份） ----------------
def send_serverchan(sckey: str, title: str, desp: str) -> bool:
    url = f"https://sctapi.ftqq.com/{sckey}.send"
    payload = {"title": title, "desp": desp}
    try:
        r = requests.post(url, json=payload, timeout=8)
        LOG.info("Server酱返回: %s %s", r.status_code, r.text)
        return r.status_code == 200
    except Exception as e:
        LOG.error("Server酱发送失败: %s", e)
        return False


def send_notifications(title: str, body: str):
    sckey = os.environ.get("SERVERCHAN_SCKEY") or os.environ.get("SERVERCHAN_SCKEY_TURBO")
    wechat_webhook = os.environ.get("WECHAT_WEBHOOK")
    wechat_secret = os.environ.get("WECHAT_SECRET")
    ok = False
    if sckey:
        LOG.info("使用 Server酱 发送提醒")
        ok = send_serverchan(sckey, title, body)
    if not ok and wechat_webhook:
        try:
            from urllib.parse import quote_plus
            import hmac, hashlib, base64
            url = wechat_webhook
            if wechat_secret:
                timestamp = str(int(time.time() * 1000))
                string_to_sign = f"{timestamp}\n{wechat_secret}"
                hmac_code = hmac.new(wechat_secret.encode('utf-8'),
                                     string_to_sign.encode('utf-8'),
                                     digestmod=hashlib.sha256).digest()
                sign = base64.b64encode(hmac_code).decode('utf-8')
                sign_encoded = quote_plus(sign)
                url = f"{wechat_webhook}&timestamp={timestamp}&sign={sign_encoded}"
            payload = {"msgtype": "text", "text": {"content": f"{title}\n\n{body}"}}
            r = requests.post(url, json=payload, timeout=8)
            LOG.info("企业微信返回: %s %s", r.status_code, r.text)
            ok = (r.status_code == 200)
        except Exception as e:
            LOG.error("企业微信发送失败: %s", e)
            ok = False
    if not ok:
        LOG.warning("未发送任何提醒（未配置或发送失败）。")


# ---------------- 主逻辑 ----------------
def main(debug=False):
    if debug:
        LOG.setLevel(logging.DEBUG)

    cfg = load_config()
    ma_default = int(cfg.get("ma_window", 180))
    fetch_days_back = int(cfg.get("fetch_days_back", 400))
    instruments: Dict[str, dict] = cfg.get("instruments", {})
    if not instruments:
        LOG.error("配置文件中未找到 instruments")
        return

    codes = list(instruments.keys())
    LOG.info("待监控品种数量: %d  示例: %s", len(codes), codes[:6])

    today = dt.date.today()
    start_date = today - dt.timedelta(days=fetch_days_back)
    end_date = today
    LOG.info("拉取区间: %s ~ %s", start_date, end_date)

    nav_df = fetch_series_for_codes(codes, start_date, end_date)
    LOG.info("拿到价格表：行 %d 列 %d", len(nav_df), len(nav_df.columns))

    alerts = []
    for code, meta in instruments.items():
        name = meta.get("name", code)
        ma_days = int(meta.get("ma", ma_default))
        if code not in nav_df.columns:
            LOG.warning("%s (%s) 没有价格数据，跳过", name, code)
            continue
        s = nav_df[code].dropna()
        if s.empty:
            LOG.warning("%s (%s) 全为空，跳过", name, code)
            continue

        price_series = s
        ma_series = price_series.rolling(ma_days, min_periods=ma_days).mean()
        last_price = float(price_series.iloc[-1])
        last_ma = float(ma_series.iloc[-1]) if not pd.isna(ma_series.iloc[-1]) else None
        last_date = price_series.index[-1].date()

        LOG.info("%s (%s) 最后交易日 %s 价格=%f MA%d=%s", name, code, last_date, last_price, ma_days, str(last_ma))
        if last_ma is None:
            LOG.info("%s MA%d 未计算（数据不足），跳过报警判断", name, ma_days)
            continue

        if last_price < last_ma:
            gap = (last_price - last_ma) / last_ma
            title = f"[预警] {name}({code}) 跌破 MA{ma_days}"
            body = (f"品种: {name} ({code})\n日期: {last_date}\n最新价: {last_price:.6f}\nMA{ma_days}: {last_ma:.6f}\n"
                    f"低于幅度: {gap:.2%}\n")
            alerts.append((title, body))

    if alerts:
        for t, b in alerts:
            send_notifications(t, b)
    else:
        LOG.info("无需要报警的品种。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    main(debug=args.debug)
