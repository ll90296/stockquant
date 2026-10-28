# -*- coding: utf-8 -*-
"""
每日运行入口 (服务器 cron 调用):
  python run_daily.py            -> 若非调仓日只推送提示
  python run_daily.py --update   -> 先增量更新数据再算信号

合规边界: 本程序只产生信号并推送, 不连接券商接口, 不自动下单。
执行方式: 收到推送后, 在券商 APP 手动下单, 或对持仓设置条件单防守。
"""
import os, sys, argparse, datetime, traceback
import pandas as pd
import engine, notify

HERE = os.path.dirname(os.path.abspath(__file__))

def update_data(cfg):
    """增量更新: 股票池最近 45 天行情 + 市场基准(沪深300ETF), 合并进缓存"""
    import akshare as ak
    data_dir = os.path.join(HERE, cfg["paths"]["data_dir"])
    cons = [fn[:-4] for fn in os.listdir(data_dir)
            if fn.endswith(".csv") and not fn.startswith("_")]
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=45)).strftime("%Y%m%d")
    n_ok = 0
    for sym in cons:
        try:
            new = ak.stock_zh_a_hist_tx(symbol=sym, start_date=start, end_date=end, adjust="hfq")
            path = os.path.join(data_dir, f"{sym}.csv")
            if new is None or len(new) == 0:
                continue
            old = pd.read_csv(path)
            merged = pd.concat([old, new]).drop_duplicates(subset=["date"]).sort_values("date")
            merged.to_csv(path, index=False)
            n_ok += 1
        except Exception:
            continue
    # 市场基准
    try:
        mkt = ak.fund_etf_hist_sina(symbol="sh510300")
        mkt.to_csv(os.path.join(data_dir, "_market_510300.csv"), index=False)
        mkt_msg = "市场基准已更新"
    except Exception as e:
        mkt_msg = f"市场基准更新失败: {type(e).__name__}"
    return f"数据更新 {n_ok}/{len(cons)}, {mkt_msg}"

def _names(cfg):
    """code -> 名称 (来自 data/_names.csv, 缺失返回空串)"""
    import json
    p = os.path.join(HERE, cfg["paths"]["data_dir"], "_names.csv")
    if not os.path.exists(p):
        return {}
    df = pd.read_csv(p, dtype={"code": str})
    return dict(zip(df["code"], df["name"].astype(str)))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="运行前先增量更新数据")
    args = ap.parse_args()
    cfg = engine.load_config()
    msg = []
    if args.update:
        try:
            msg.append(update_data(cfg))
        except Exception:
            msg.append("数据更新失败: " + traceback.format_exc(limit=1))

    px = engine.load_panel(cfg)
    asof = px.index[-1]
    st = cfg["strategy"]
    rdates = engine.rebalance_dates(px.index, st["rebalance"], st.get("rebalance_weekday", 5))
    prev_state = _load_state(cfg)
    risk_on = engine.market_risk_on(px, cfg).loc[asof] if st.get("market_filter") else True
    if not risk_on:
        tgt, scores = [], px.loc[:asof].pct_change(st["lookback"]).iloc[-1].dropna().sort_values(ascending=False)
    else:
        tgt, scores = engine.compute_targets_buffered(prev_state, px, cfg, asof)

    is_reb = asof in rdates
    names = _names(cfg)
    top10 = [(c, f"{v:+.1%}") for c, v in scores.head(10).items()]
    lines = [
        f"数据截止: {asof.date()}",
        f"市场状态: {'持仓(基准在200日线上)' if risk_on else '空仓防守(基准跌破200日线)'}",
        f"今天是{'调仓日' if is_reb else '非调仓日(仅提示)'}",
        "",
        ("目标持仓(等权):" if tgt else "当前应空仓防守"),
    ] + ([f"  {c} {names.get(c, '')} -> {w:.0%}" for c, w in tgt] if tgt else []) + [
        "",
        "动量前10:",
    ] + [f"  {c} {names.get(c, '')}  {v}" for c, v in top10]
    text = "\n".join(lines)
    if is_reb:
        _save_state(cfg, dict(tgt))
        title = f"[调仓信号] {asof.date()} 持仓更新"
    else:
        title = f"[动量监测] {asof.date()}"
    sent = notify.send(cfg, title, text)
    msg.append(f"通知渠道: {sent or '无(未配置)'}")
    print(text)
    print("\n".join(msg))

    os.makedirs(os.path.join(HERE, cfg["paths"]["log_dir"]), exist_ok=True)
    with open(os.path.join(HERE, cfg["paths"]["log_dir"], "signals.log"), "a", encoding="utf-8") as f:
        f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M} asof={asof.date()} reb={is_reb} =====\n{text}\n")

def _state_path(cfg):
    return os.path.join(HERE, cfg["paths"]["log_dir"], "state.json")

def _load_state(cfg):
    import json
    p = _state_path(cfg)
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return {}

def _save_state(cfg, weights):
    import json, os
    os.makedirs(os.path.dirname(_state_path(cfg)), exist_ok=True)
    json.dump(weights, open(_state_path(cfg), "w", encoding="utf-8"), ensure_ascii=False)

if __name__ == "__main__":
    main()
