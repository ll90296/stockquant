# -*- coding: utf-8 -*-
"""核心库: 数据加载 / 信号计算 / 回测引擎 / 成本模型"""
import os
import yaml
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

def load_config(path=None):
    path = path or os.path.join(HERE, "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

# ---------------- 数据 ----------------

def load_panel(cfg, min_bars=250, only=None):
    """读取缓存数据 -> 收盘价面板 (index=日期, columns=股票代码, 后复权)。
    only: 指定股票代码列表(如自定义池); None=读 data/ 下全部缓存。"""
    data_dir = os.path.join(HERE, cfg["paths"]["data_dir"])
    if only is not None:
        wanted = [f"{c}.csv" for c in only if os.path.exists(os.path.join(data_dir, f"{c}.csv"))]
    else:
        wanted = [fn for fn in os.listdir(data_dir) if fn.endswith(".csv") and not fn.startswith("_")]
    frames = {}
    for fn in wanted:
        sym = fn[:-4]
        df = pd.read_csv(os.path.join(data_dir, fn))
        if "date" not in df.columns or len(df) < min_bars:
            continue
        df["date"] = pd.to_datetime(df["date"])
        s = df.set_index("date")["close"].astype(float)
        # 修复不复权/复权数据中的份额拆分缺口(|单日收益|>35% 视为异常)
        r = s.pct_change()
        bad = r[r.abs() > 0.35]
        for dt in bad.index:
            i = s.index.get_loc(dt)
            if i > 0:
                factor = s.iloc[i] / s.iloc[i - 1]
                s = pd.concat([s.iloc[:i] * factor, s.iloc[i:]])
        frames[sym] = s
    px = pd.DataFrame(frames).sort_index()
    if px.empty:
        return px
    min_stocks = max(1, int(px.shape[1] * 0.5))   # 剔除股票池成员变化造成的早期稀疏段(小池子至少留1)
    px = px[px.count(axis=1) >= min_stocks]
    return px.ffill(limit=10)

# ---------------- 信号 ----------------

def momentum_scores(px, lookback):
    return px.pct_change(lookback)

def rebalance_dates(index, freq="weekly", weekday=5):
    """产生调仓日序列: 每周/每两周/每月的指定交易日"""
    s = pd.Series(index, index=index)
    if freq == "daily":
        return set(index)
    if freq == "weekly":
        groups = s.groupby([index.isocalendar().year, index.isocalendar().week])
    elif freq == "biweekly":
        iso = index.isocalendar()
        period = (iso.year.astype(int) * 53 + iso.week.astype(int)) // 2
        groups = s.groupby(period)
    elif freq == "monthly":
        groups = s.groupby(index.to_period("M"))
    else:
        raise ValueError(freq)
    out = []
    for _, g in groups:
        i = min(weekday, len(g)) - 1
        out.append(g.index[i])
    return set(out)

def compute_targets(px, cfg, asof=None):
    """给定截至 asof 的数据, 返回目标持仓列表 [(code, weight)]"""
    st = cfg["strategy"]
    px = px if asof is None else px.loc[:asof]
    mom = px.pct_change(st["lookback"]).iloc[-1].dropna()
    mom = mom.sort_values(ascending=False)
    # 剔除动量不达标
    mom = mom[mom > st.get("min_momentum", 0.0)]
    picks = mom.index[: st["top_n"]].tolist()
    w = 1.0 / max(len(picks), 1)
    return [(c, w) for c in picks], mom

def compute_targets_buffered(prev_weights, px, cfg, asof):
    """带缓冲带的目标持仓: 旧持仓仍在 buffer_rank 内则保留, 空位由新排名补足"""
    st = cfg["strategy"]
    mom = px.loc[:asof].pct_change(st["lookback"]).iloc[-1].dropna().sort_values(ascending=False)
    mom = mom[mom > st.get("min_momentum", 0.0)]
    ranked = list(mom.index)
    keep = [c for c in prev_weights if c in ranked and ranked.index(c) < st["buffer_rank"]]
    fills = [c for c in ranked if c not in keep][: st["top_n"] - len(keep)]
    picks = (keep + fills)[: st["top_n"]]
    w = 1.0 / max(len(picks), 1)
    return [(c, w) for c in picks], mom

# ---------------- 成本 ----------------

def trade_cost(value, cfg, is_sell):
    """单笔成交成本(元): 佣金(有最低5元) + 卖出印花税 + 滑点"""
    f = cfg["fees"]
    c = max(abs(value) * f["commission_rate"], f["commission_min"])
    if is_sell:
        c += abs(value) * f["stamp_tax"]
    c += abs(value) * f["slippage"]
    return c

# ---------------- 回测引擎 ----------------

# ---------------- 市场状态过滤 ----------------

def market_risk_on(px, cfg):
    """风险开关: 基准指数(默认沪深300ETF) 收盘 > 200日均线 时为 True。
    优先读 data/_market_510300.csv(由 fetch/update 维护), 缺失时用股票池等权均值代理。"""
    data_dir = os.path.join(HERE, cfg["paths"]["data_dir"])
    mkt_path = os.path.join(data_dir, "_market_510300.csv")
    if os.path.exists(mkt_path):
        df = pd.read_csv(mkt_path)
        df["date"] = pd.to_datetime(df["date"])
        s = df.set_index("date")["close"].astype(float)
    else:
        s = (px.pct_change().mean(axis=1).fillna(0) + 1).cumprod()
    ma = s.rolling(200).mean()
    on = (s > ma).reindex(px.index).ffill().fillna(False)
    return on

def backtest(px, cfg, freq=None, verbose=False):
    """事件驱动回测: 调仓日收盘定权重, 次日起按新权重计收益, 换手扣成本。
    若开启 market_filter: 基准跌破 200 日线时空仓(收益 0)。"""
    st = cfg["strategy"]
    freq = freq or st["rebalance"]
    ret = px.pct_change()
    rdates = rebalance_dates(px.index, freq, st.get("rebalance_weekday", 5))
    risk_on = market_risk_on(px, cfg) if st.get("market_filter") else None
    weights = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    prev, n_trades, traded_value = {}, 0, 0.0
    cost_by_date, holdings_hist = {}, []
    for dt in px.index:
        if dt in rdates:
            if risk_on is not None and not bool(risk_on.loc[dt]):
                tgt = []
            else:
                tgt, _ = compute_targets_buffered(prev, px, cfg, dt)
            new = dict(tgt)
            day_cost, day_trades, day_value = 0.0, 0, 0.0
            for c in set(new) | set(prev):
                dv = new.get(c, 0) - prev.get(c, 0)
                if abs(dv) < 1e-9:
                    continue
                v = abs(dv) * st["portfolio_value"]
                day_cost += trade_cost(v, cfg, is_sell=dv < 0)
                day_trades += 1
                day_value += v
            if day_trades:
                cost_by_date[dt] = day_cost
                n_trades += day_trades
                traded_value += day_value
            prev = new
            holdings_hist.append([str(dt.date()), ",".join(new.keys())])
        for c, w in prev.items():
            weights.loc[dt, c] = w
    gross = (weights.shift() * ret).sum(axis=1)
    net = gross.copy()
    for dt, c in cost_by_date.items():
        net.loc[dt] -= c / st["portfolio_value"]
    net.iloc[0] = 0
    nav = (1 + net).cumprod()
    years = (px.index[-1] - px.index[0]).days / 365.25
    stats = {
        "freq": freq,
        "cagr": nav.iloc[-1] ** (1 / years) - 1,
        "mdd": (nav / nav.cummax() - 1).min(),
        "sharpe": net.mean() / net.std() * np.sqrt(244) if net.std() > 0 else 0,
        "annual_turnover": traded_value / 2 / years / st["portfolio_value"],
        "total_cost_pct": sum(cost_by_date.values()) / st["portfolio_value"],
        "n_rebalance": len(holdings_hist),
        "nav": nav, "holdings": holdings_hist,
    }
    if verbose:
        print(f"[{freq}] 年化 {stats['cagr']:+.2%}  回撤 {stats['mdd']:.2%}  夏普 {stats['sharpe']:.2f}  "
              f"年均换手 {stats['annual_turnover']:.1f}x  总成本 {stats['total_cost_pct']:.1%}")
    return stats
