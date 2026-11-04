# -*- coding: utf-8 -*-
"""盤後：用 api.ticks 抓全市場整天逐筆，算出「100% 逐筆精確」的回放檔 data/日期.full.json

  python build_replay.py                 # 今天（要等 13:40 之後）
  python build_replay.py --date 2026-10-20
  python build_replay.py --top 600       # 只抓成交額前 600 檔（省流量/省時間）

注意：
  * 官方規定盤中 ticks 查詢不得超過 10 次，所以本程式在 08:30~13:40 之間拒絕執行。
  * 查詢會吃每日流量（未用 API 下單者 500MB/日）；程式每 100 檔檢查一次 api.usage()，快用完會先停。
  * 全市場約 1,800 檔，10 秒 50 次的限制下大約要 8 分鐘。
"""
import argparse
import datetime as _dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from engine import build_frames_from_bins, tw_now  # noqa: E402
from feed_shioaji import fetch_day_bins, fetch_snapshots, list_stocks, login, logout  # noqa: E402
from groups import build_group_sets  # noqa: E402
from server import group_options, load_cfg, report_sets  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.ini"))
    ap.add_argument("--date", default=tw_now().date().isoformat())
    ap.add_argument("--top", type=int, default=0, help="只抓成交額前 N 檔（0=全部）")
    ap.add_argument("--force", action="store_true", help="略過盤中時間檢查（自行承擔超限風險）")
    args = ap.parse_args()

    now = tw_now().time()
    if not args.force and _dt.time(8, 30) <= now <= _dt.time(13, 40):
        raise SystemExit("盤中 ticks 查詢上限 10 次，請 13:40 之後再跑（或用 --force）")

    cfg = load_cfg(args.config)
    gopt = group_options(cfg)
    api = login(cfg)
    try:
        stocks = list_stocks(api, cfg.getboolean("universe", "include_etf", fallback=False))
        sets = build_group_sets(stocks, **gopt)
        report_sets(sets)
        # 依當日成交額排序（流量不夠時至少大的都有）
        snaps = fetch_snapshots(api, [v["contract"] for v in stocks.values()])
        amt = {s["code"]: s["total_amount"] for s in snaps}
        codes = sorted(stocks, key=lambda c: amt.get(c, 0), reverse=True)
        if args.top:
            codes = codes[: args.top]

        def progress(i, n, code):
            if i % 50 == 0 or i == n:
                print("  %d / %d  (%s)" % (i, n, code), flush=True)

        print("[replay] 抓 %s 逐筆，共 %d 檔…" % (args.date, len(codes)), flush=True)
        bins = fetch_day_bins(api, stocks, codes, args.date, progress,
                              auction_neutral=cfg.getboolean("estimate", "auction_neutral", fallback=True))
    finally:
        logout(api)

    # 漲跌幅用合約的參考價（昨收）計算；合約在隔天 08:00 才更新，所以只有「當天」建檔時參考價才對
    same_day = args.date == tw_now().date().isoformat()
    frames, smeta = build_frames_from_bins(bins, stocks, sets, with_pct=same_day)
    covered = sum(amt.get(c, 0) for c in bins)
    total = sum(amt.values()) or 1
    payload = {"date": args.date, "source": "full", "format": 2, "sets": smeta,
               "coverage": {"tick_n": len(bins), "tail_n": 0, "est_ratio": 0.0,
                            "covered_share": round(covered / total, 4)},
               "names": {c: v["name"] for c, v in stocks.items()},
               "frames": frames}
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    out = os.path.join(HERE, "data", "%s.full.json" % args.date)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    last = frames[-1]
    print("[replay] 完成 → %s" % out)
    print("         全市場主動買賣淨額 %+.2f 億，成交額 %.0f 億，涵蓋 %.1f%% 成交額" % (
        last["m"][0], last["m"][1], covered / total * 100))


if __name__ == "__main__":
    main()
