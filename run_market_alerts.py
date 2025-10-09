#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_market_alerts.py
- 从 watch_config.json 读取需要盯盘的场内品种（股票/ETF/指数）
- 优先使用东方财富 push2his 接口拉日线（尝试 secid=1.code / 0.code）
- 计算每个品种最近收盘价与 MA（可全局配置或单品种覆盖）
- 若最新价 < MA，则通过 Server酱 或 企业微信 webhook 推送提醒
- 可在 watch_config.json 中自由增删品种、调整MA天数、调整回溯天数
"""
import os
import json
import time
import logging
import argparse
import datetime as dt
from io import StringIO
from typing import Dict, List

import requests
import pandas as pd
import numpy as np

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
        raise FileNotFoundError(f"配置文件{path} 未找到，请参考 README 新建。")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ------------------ 抓取函数（东方财富 push2his） ------------------
def fetch_from_eastmoney_kline(code: str, start_date: str, end_date: str, timeout=10) -> pd.Series:
    """
    优先尝试 secid = 1.code（上交所），若无结果再尝试 secid = 0.code（深交所）
    返回 pd.Series，index 为日期（datetime），值为收盘价（float）。
    start_date / end_date 格式 'YYYYMMDD'
    """
    base = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params_template = {
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        # fields2 可以留空或默认（我们只解析 klines 字段）
        "beg": start_date,
        "end": end_date,
        "rtntype": "6",
        "klt": "101",   # 日线
        "fqt": "1"      # 复权方式：1 前复权（你可改为 0 不复权）
    }

    last_exc = None
    for market_prefix in ("1", "0"):   # 首试 1.code 再试 0.code
        secid = f"{market_prefix}.{code}"
        params = dict(params_template)
        params["secid"] = secid
        try:
            r = requests.get(base, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            data = j.get("data") or {}
            klines = data.get("klines") or []
            if not klines:
                # 无数据，继续尝试下一个 secid
                continue

            dates = []
            closes = []
            for k in klines:
                # k 通常是逗号分隔的字符串，格式示例: "20250930,open,close,high,low,volume,amount,..."
                parts = k.split(",")
                if not parts:
                    continue
                # 解析日期：可能是 '20250930' 或 '2025-09-30'
                raw_date = parts[0].strip()
                try:
                    if "-" in raw_date:
                        d = pd.to_datetime(raw_date)
                    else:
                        d = pd.to_datetime(raw_date, format="%Y%m%d")
                except Exception:
                    # 如果解析失败，跳过
                    continue
                # 解析收盘价（按常见格式：parts[2] 为收盘价；若无则尝试 parts[1]）
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

            if len(closes) == 0:
                continue

            series = pd.Series(closes, index=pd.to_datetime(dates)).sort_index()
            return series
        except Exception as e:
            last_exc = e
            LOG.debug("东财抓取 %s (%s) 异常: %s", code, secid, e)
            time.sleep(0.15)
            continue

    raise RuntimeError(f"未能通过东财 push2his 获取 {code} 的日线数据（最后异常：{last_exc}）")

# ------------------ 批量拉取工具 ------------------
def fetch_series_for_codes(codes: List[str], start_date: dt.date, end_date: dt.date) -> pd.DataFrame:
    start_s = start_date.strftime("%Y%m%d")
    end_s = end_date.strftime("%Y%m%d")
    series_list = []
    for c in codes:
        LOG.info("拉取 %s: %s -> %s", c, start_s, end_s)
        try:
            s = fetch_from_eastmoney_kline(c, start_s, end_s)
            s.name = c
            series_list.append(s)
            time.sleep(0.2)
        except Exception as e:
            LOG.error(" 拉取 %s 失败: %s", c, e)
            continue
    if not series_list:
        raise ValueError("没有任何品种成功获取数据。")
    df = pd.concat(series_list, axis=1).sort_index()
    df.index = pd.to_datetime(df.index).normalize()
    return df

# ------------------ 推送（Server酱） ------------------
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
    wechat_webhook = os.environ.get("WECHAT_WEBHOOK")  # 仍保留企业微信备用
    wechat_secret = os.environ.get("WECHAT_SECRET")
    ok = False
    if sckey:
        LOG.info("使用 Server酱 发送提醒")
        ok = send_serverchan(sckey, title, body)
    # 如果你也配置了企业微信 webhook，可以用下面的逻辑（我们在前面的对话里已经实现过）
    if not ok and wechat_webhook:
        from urllib.parse import quote_plus
        import hmac, hashlib, base64
        try:
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

# ------------------ 主逻辑 ------------------
def main(debug=False):
    if debug:
        LOG.setLevel(logging.DEBUG)

    cfg = load_config()
    ma_default = int(cfg.get("ma_window", 180))
    fetch_days_back = int(cfg.get("fetch_days_back", 400))
    instruments: Dict[str, dict] = cfg.get("instruments", {})
    if not instruments:
        LOG.error("配置文件中未找到 instruments，请编辑 watch_config.json")
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
        # 如果该 code 没数据，跳过但记录
        if code not in nav_df.columns:
            LOG.warning("%s (%s) 没有价格数据，跳过", name, code)
            continue

        s = nav_df[code].dropna()
        if s.empty:
            LOG.warning("%s (%s) 全为空，跳过", name, code)
            continue

        # 计算 normalized price (直接按收盘价)
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
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    args = parser.parse_args()
    main(debug=args.debug)
