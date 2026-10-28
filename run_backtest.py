# -*- coding: utf-8 -*-
"""回测 CLI: python run_backtest.py  (频次对比 + 参数稳健性)"""
import engine

def main():
    cfg = engine.load_config()
    px = engine.load_panel(cfg)
    print(f"股票池有效数量: {px.shape[1]}  区间: {px.index[0].date()} ~ {px.index[-1].date()}\n")
    print("===== 频次对比 (lookback=%d, top%d) =====" % (cfg["strategy"]["lookback"], cfg["strategy"]["top_n"]))
    for freq in ["daily", "weekly", "biweekly", "monthly"]:
        try:
            engine.backtest(px, cfg, freq=freq, verbose=True)
        except Exception as e:
            print(f"[{freq}] failed: {e}")
    print("\n===== lookback 稳健性 (weekly) =====")
    for lb in [60, 90, 120, 180]:
        cfg2 = engine.load_config()
        cfg2["strategy"]["lookback"] = lb
        engine.backtest(px, cfg2, freq="weekly", verbose=True)

if __name__ == "__main__":
    main()
