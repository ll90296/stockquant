# -*- coding: utf-8 -*-
"""抓取沪深300成分股日线(后复权, 新浪源)到 data/ 缓存, 带重试与断点续传。"""
import warnings; warnings.filterwarnings("ignore")
import os, sys, time, json
import akshare as ak
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
START, END = "20180101", "20261231"

def get_constituents():
    cache = os.path.join(DATA, "hs300_cons.json")
    if os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    for i in range(8):
        try:
            df = ak.index_stock_cons_sina(symbol="000300")
            codes = df["code"].tolist()  # 形如 sh600000
            json.dump(codes, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"constituents: {len(codes)}", file=sys.stderr)
            return codes
        except Exception as e:
            print(f"cons attempt {i+1}: {type(e).__name__}", file=sys.stderr)
            time.sleep(5)
    raise SystemExit("failed to fetch constituents")

def fetch_one(sym):
    path = os.path.join(DATA, f"{sym}.csv")
    if os.path.exists(path):
        return True
    for i in range(5):
        try:
            df = ak.stock_zh_a_hist_tx(symbol=sym, start_date=START, end_date=END, adjust="hfq")
            if df is not None and len(df) > 100:
                df.to_csv(path, index=False)
                return True
            return False
        except Exception as e:
            print(f"{sym} attempt {i+1}: {type(e).__name__}", file=sys.stderr)
            time.sleep(3)
    return False

codes = get_constituents()
ok, fail = 0, []
for k, sym in enumerate(codes):
    if fetch_one(sym):
        ok += 1
    else:
        fail.append(sym)
    if (k + 1) % 30 == 0:
        print(f"progress {k+1}/{len(codes)} ok={ok}", file=sys.stderr)
print(f"DONE ok={ok} fail={len(fail)} {fail[:10]}", file=sys.stderr)
