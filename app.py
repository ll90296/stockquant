# -*- coding: utf-8 -*-
"""本地/服务器 Web 界面: python app.py 后浏览器打开 http://127.0.0.1:5000"""
import os
import re
import time
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
from flask import Flask, jsonify, render_template, request

import engine
from notify import send

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

_real_price_cache = {}   # code -> (price, ts)
_cache_ttl = 600
_names_map = None        # code -> 名称 (来自 data/_names.csv)


def stock_name(code: str) -> str:
    global _names_map
    if _names_map is None:
        _names_map = {}
        path = os.path.join(HERE, cfg_paths_data_dir(), "_names.csv")
        if os.path.exists(path):
            df = pd.read_csv(path, dtype={"code": str})
            _names_map = dict(zip(df["code"], df["name"].astype(str)))
    return _names_map.get(str(code), "")


def cfg_paths_data_dir():
    return engine.load_config()["paths"]["data_dir"]


def normalize_code(raw: str):
    """'600039'/'sh600039'/'600039.SH' -> ('600039', 'sh600039'); 非法返回 None"""
    c = re.sub(r"[^0-9a-zA-Z]", "", str(raw)).lower()
    m = re.search(r"(?:sh|sz|bj)?(\d{6})$", c)
    if not m:
        return None
    code = m.group(1)
    prefix = "sh" if code[0] == "6" else ("bj" if code[0] in "48" else "sz")
    return code, prefix + code


def get_panel(cfg):
    st = cfg["strategy"]
    only = st["custom_pool"] if st.get("universe") == "custom" and st.get("custom_pool") else None
    return engine.load_panel(cfg, only=only)


def fetch_real_price(tx_symbol, retries=3):
    """腾讯源取未复权最近收盘价(条件单触发价要用真实价格)"""
    now = time.time()
    if tx_symbol in _real_price_cache and now - _real_price_cache[tx_symbol][1] < _cache_ttl:
        return _real_price_cache[tx_symbol][0]
    import akshare as ak
    for i in range(retries):
        try:
            df = ak.stock_zh_a_hist_tx(symbol=tx_symbol, start_date="20261001",
                                       end_date="20991231", adjust="")
            if df is not None and len(df):
                p = float(df["close"].iloc[-1])
                _real_price_cache[tx_symbol] = (p, now)
                return p
        except Exception:
            time.sleep(2)
    return None


def market_state(cfg):
    """返回 (risk_on: bool, 现价, MA200, 说明)"""
    path = os.path.join(HERE, cfg["paths"]["data_dir"], "_market_510300.csv")
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    s = df.set_index("date")["close"].astype(float)   # 新浪ETF数据为真实价格
    ma = s.rolling(200).mean()
    last, ma_last = float(s.iloc[-1]), float(ma.iloc[-1])
    return last > ma_last, last, ma_last


def current_signal(cfg):
    """当前目标持仓 + 动量排名 (考虑市场过滤)"""
    px = get_panel(cfg)
    if px.empty:
        return None, None, None
    risk_on, mkt_px, mkt_ma = market_state(cfg)
    asof = px.index[-1]
    if cfg["strategy"].get("market_filter") and not risk_on:
        tgt, mom = [], engine.momentum_scores(px, cfg["strategy"]["lookback"]).iloc[-1].dropna().sort_values(ascending=False)
    else:
        tgt, mom = engine.compute_targets_buffered({}, px, cfg, asof)
    return {
        "asof": str(asof.date()),
        "risk_on": bool(risk_on),
        "mkt_px": round(mkt_px, 3),
        "mkt_ma": round(mkt_ma, 3),
        "empty": not tgt,
        "targets": tgt,
        "mom_top10": [(str(c), float(v)) for c, v in mom.head(10).items()],
        "pool_size": px.shape[1],
        "names": {str(c): stock_name(c) for c in
                  [c for c, _ in tgt] + [str(c) for c in mom.head(10).index]},
    }, px, cfg


def conditional_orders(cfg, signal):
    """生成条件单指令: 每只持仓的止损触发价(真实价格口径) + 空仓时的进场提醒"""
    orders = []
    if signal["empty"]:
        orders.append({
            "type": "re-entry",
            "text": (f"当前空仓防守。可在券商APP挂提醒/条件单: 沪深300ETF(510300) "
                     f"收盘站回 {signal['mkt_ma']:.3f} 元(200日均线)上方即重新进场。"),
        })
        return orders
    st = cfg["strategy"]
    per = st["portfolio_value"] / len(signal["targets"])
    for code, w in signal["targets"]:
        sym = ("sh" if code[0] == "6" else "sz") + code
        real = fetch_real_price(sym)
        if real:
            stop = round(real * (1 - st.get("stop_loss_pct", 0.15)), 2)
            shares = int(per / real / 100) * 100
            nm = stock_name(code)
            orders.append({
                "type": "stop-loss", "code": code, "real_price": real,
                "stop": stop, "shares": max(shares, 100),
                "text": (f"{code} {nm}: 现价约 {real:.2f} 元, 挂止损条件单——跌破 {stop:.2f} 元 "
                         f"(≈-{st.get('stop_loss_pct', 0.15):.0%}) 全部卖出; 买入约 {shares} 股(约{per:,.0f}元)。"),
            })
        else:
            nm = stock_name(code)
            orders.append({
                "type": "stop-loss", "code": code, "real_price": None,
                "text": (f"{code} {nm}: 未取到实时价(网络原因), 请以券商APP现价为准, "
                         f"挂止损条件单: 现价 × (1 - {st.get('stop_loss_pct', 0.15):.0%})。"),
            })
    return orders


# ---------------- 页面 ----------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------- API ----------------

@app.route("/api/overview")
def api_overview():
    cfg = engine.load_config()
    signal, _, _ = current_signal(cfg)
    if signal is None:
        return jsonify({"error": "股票池无可用数据, 请先添加个股"}), 400
    return jsonify({**signal, "orders": conditional_orders(cfg, signal),
                    "stop_loss_pct": cfg["strategy"].get("stop_loss_pct", 0.15)})


@app.route("/api/curve")
def api_curve():
    cfg = engine.load_config()
    px = get_panel(cfg)
    if px.empty:
        return jsonify({"error": "股票池无可用数据"}), 400
    stats = engine.backtest(px, cfg, verbose=False)
    nav, dd = stats["nav"], stats["nav"] / stats["nav"].cummax() - 1
    mkt = pd.read_csv(os.path.join(HERE, cfg["paths"]["data_dir"], "_market_510300.csv"))
    mkt["date"] = pd.to_datetime(mkt["date"])
    s = mkt.set_index("date")["close"].astype(float).reindex(nav.index).ffill()
    bench = s / s.iloc[0]
    step = max(1, len(nav) // 400)
    idx = nav.index[::step]
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    return jsonify({
        "dates": [str(d.date()) for d in idx],
        "nav": [round(float(v), 4) for v in nav.iloc[::step]],
        "bench": [round(float(v), 4) for v in bench.iloc[::step]],
        "dd": [round(float(v), 4) for v in dd.iloc[::step]],
        "stats": {k: (round(float(stats[k]), 4) if isinstance(stats[k], (int, float)) else stats[k])
                  for k in ["cagr", "mdd", "sharpe", "annual_turnover", "total_cost_pct", "n_rebalance"]},
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()), "years": round(yrs, 1),
        "freq": stats["freq"],
    })


@app.route("/api/pool", methods=["GET"])
def api_pool_get():
    cfg = engine.load_config()
    st = cfg["strategy"]
    have = {fn[:-4] for fn in os.listdir(os.path.join(HERE, cfg["paths"]["data_dir"]))
            if fn.endswith(".csv") and not fn.startswith("_")}
    return jsonify({"universe": st["universe"], "custom_pool": st.get("custom_pool", []),
                    "custom_names": {c: stock_name(c) for c in st.get("custom_pool", [])},
                    "top_n": st["top_n"], "lookback": st["lookback"], "rebalance": st["rebalance"],
                    "buffer_rank": st["buffer_rank"], "portfolio_value": st["portfolio_value"],
                    "hs300_count": len(have)})


@app.route("/api/pool", methods=["POST"])
def api_pool_post():
    data = request.get_json(force=True)
    action = data.get("action")
    cfg = engine.load_config()
    st = cfg["strategy"]
    st.setdefault("custom_pool", [])
    if action == "add":
        norm = normalize_code(data.get("code", ""))
        if not norm:
            return jsonify({"error": f"无效代码: {data.get('code')}"}), 400
        code, txsym = norm
        if code in st["custom_pool"]:
            return jsonify({"error": f"{code} 已在池中"}), 400
        path = os.path.join(HERE, cfg["paths"]["data_dir"], f"{code}.csv")
        if not os.path.exists(path):
            import akshare as ak
            df = None
            for i in range(3):
                try:
                    df = ak.stock_zh_a_hist_tx(symbol=txsym, start_date="20150101",
                                               end_date="20991231", adjust="hfq")
                    break
                except Exception:
                    time.sleep(2)
            if df is None or not len(df):
                return jsonify({"error": f"{code} 数据获取失败, 稍后再试"}), 502
            df = df.rename(columns={"day": "date"}) if "day" in df.columns else df
            df.to_csv(path, index=False)
        st["custom_pool"].append(code)
        if len(st["custom_pool"]) == 1 and st["universe"] != "custom":
            st["universe"] = "custom"      # 第一个自定义个股加入即切到自定义池
        added_name = stock_name(code)
    elif action == "remove":
        code = data.get("code", "")
        st["custom_pool"] = [c for c in st["custom_pool"] if c != code]
        if not st["custom_pool"]:
            st["universe"] = "hs300"
    elif action == "set_universe":
        st["universe"] = "custom" if data.get("universe") == "custom" else "hs300"
    else:
        return jsonify({"error": "未知操作"}), 400
    with open(os.path.join(HERE, "config.yaml"), "w", encoding="utf-8") as f:
        import yaml as _y
        _y.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return jsonify({"ok": True, "custom_pool": st["custom_pool"], "universe": st["universe"],
                    "name": added_name if action == "add" else None})


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(force=True)
    cfg = engine.load_config()
    st = cfg["strategy"]
    for k in ["lookback", "top_n", "buffer_rank", "portfolio_value"]:
        if k in data:
            st[k] = int(data[k])
    if "rebalance" in data and data["rebalance"] in ("daily", "weekly", "biweekly", "monthly"):
        st["rebalance"] = data["rebalance"]
    if "stop_loss_pct" in data:
        st["stop_loss_pct"] = float(data["stop_loss_pct"])
    with open(os.path.join(HERE, "config.yaml"), "w", encoding="utf-8") as f:
        import yaml as _y
        _y.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return jsonify({"ok": True, "strategy": st})


@app.route("/api/notify-test", methods=["POST"])
def api_notify_test():
    cfg = engine.load_config()
    signal, _, _ = current_signal(cfg)
    if signal is None:
        return jsonify({"error": "无信号可发送"}), 400
    orders = conditional_orders(cfg, signal)
    ok = send(cfg, f"[量化信号 {signal['asof']}]",
              f"市场状态: {'持仓' if signal['risk_on'] else '空仓防守(基准<200日线)'}\n"
              f"目标持仓: {', '.join(c for c, _ in signal['targets']) or '空仓'}\n\n"
              + "\n".join(o["text"] for o in orders))
    return jsonify({"ok": bool(ok), "msg": "已推送: " + ",".join(ok) if ok else "未配置推送渠道(config.yaml notify)"})


if __name__ == "__main__":
    print("打开 http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
