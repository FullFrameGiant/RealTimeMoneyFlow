# -*- coding: utf-8 -*-
"""環境自我檢查：第一次使用前先跑這支。

  python check_env.py            # 盤中跑最完整（會訂閱 5 檔看 60 秒 tick）

檢查項目：
  1. shioaji 版本、登入、流量
  2. 能否列出上市櫃普通股與產業代碼
  3. 族群檔能否讀取、對到幾檔
  4. 快照
  5. 盤中：訂閱 5 檔熱門股 60 秒，驗證「累計買盤量 bid_side_total_vol」是否等於 tick_type=1（外盤）的量加總
     （用來確認中途啟動/斷線補值的方向；若相反，請在 config.ini 設 side_swap = true）
"""
import datetime as _dt
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from feed_shioaji import (_set_tick_callback, fetch_snapshots, find_indexes, list_stocks, login, logout,  # noqa: E402
                          subscribe_tick, tick_tuple)
from groups import build_group_sets  # noqa: E402
from server import group_options, load_cfg, report_sets  # noqa: E402


def main():
    cfg = load_cfg(os.path.join(HERE, "config.ini"))
    import shioaji as sj
    print("Python", sys.version.split()[0], "| shioaji", getattr(sj, "__version__", "?"))
    api = login(cfg)
    try:
        try:
            print("流量：", api.usage())
        except Exception as e:
            print("流量：無法取得（%s）" % e)

        stocks = list_stocks(api, cfg.getboolean("universe", "include_etf", fallback=False))
        cats = {}
        for v in stocks.values():
            cats[v["category"]] = cats.get(v["category"], 0) + 1
        print("產業代碼分布（前 10）：", sorted(cats.items(), key=lambda kv: -kv[1])[:10])

        sets = build_group_sets(stocks, **group_options(cfg))
        report_sets(sets)
        for gs in sets:
            print("  %s 例：%s" % (gs.label, "、".join(gs.gm.all_groups()[:12])))
        mine = [c for gs in sets for c in gs.gm.custom_codes]

        idx = find_indexes(api)
        if idx:
            try:
                for k, s in zip(idx, api.snapshots([idx[k] for k in idx])):
                    print("  %s 指數快照：%s  漲跌 %s（%s%%）" % (k, s.close, getattr(s, "change_price", "?"), getattr(s, "change_rate", "?")))
            except Exception as e:
                print("  指數快照失敗：%s" % e)

        snaps = fetch_snapshots(api, [v["contract"] for v in stocks.values()])
        snaps.sort(key=lambda s: -s["total_amount"])
        tot = sum(s["total_amount"] for s in snaps) or 1
        for n in (200, 400, 600, 1000):
            print("成交額前 %4d 檔占全市場 %.1f%%" % (n, sum(s["total_amount"] for s in snaps[:n]) / tot * 100))
        print("快照範例：", snaps[0] if snaps else None)
        if mine:
            mt = sum(s["total_amount"] for s in snaps if s["code"] in set(mine))
            print("族群檔 %d 檔，占全市場成交額 %.1f%%（這些會優先訂閱逐筆）" % (len(mine), mt / tot * 100))

        from engine import tw_now
        now = tw_now().time()
        if not (_dt.time(9, 0) <= now <= _dt.time(13, 25)):
            print("\n非盤中，略過 tick 驗證。盤中再跑一次可以驗證內外盤累計量方向。")
            return
        codes = [s["code"] for s in snaps[:5]]
        print("\n訂閱 %s，觀察 60 秒…" % codes)
        recs = {c: [] for c in codes}
        lock = threading.Lock()

        def cb(exchange, tick):
            t = tick_tuple(tick)
            with lock:
                if t[0] in recs:
                    recs[t[0]].append(t)

        _set_tick_callback(api, cb)
        for c in codes:
            subscribe_tick(api, stocks[c]["contract"])
        time.sleep(60)
        ok = swap = 0
        for c, rows in recs.items():
            rows = [r for r in rows if not r[10]]
            if len(rows) < 3:
                print("  %s tick 太少（%d 筆）" % (c, len(rows)))
                continue
            vb = sum(r[3] for r in rows[1:] if r[4] == 1)
            vs = sum(r[3] for r in rows[1:] if r[4] == 2)
            da = rows[-1][6] - rows[0][6]
            db = rows[-1][7] - rows[0][7]
            print("  %s ticks=%d  外盤量=%d 內盤量=%d | Δbid_side=%d Δask_side=%d" % (c, len(rows), vb, vs, db, da))
            if abs(db - vb) + abs(da - vs) <= abs(db - vs) + abs(da - vb):
                ok += 1
            else:
                swap += 1
        if ok >= swap:
            print("→ 方向正確：bid_side_total_vol = 買盤（外盤）。config.ini 維持 side_swap = false")
        else:
            print("→ 方向相反：請在 config.ini 設 side_swap = true")
    finally:
        logout(api)


if __name__ == "__main__":
    main()
