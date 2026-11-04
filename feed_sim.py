# -*- coding: utf-8 -*-
"""模擬資料源：不需要 shioaji 帳號，用來測試畫面與計算流程。
產生的是「假股票」（代號 S0001…，名稱如「半導體03」），不是真實行情。"""
import datetime as _dt
import math
import random
import threading
import time

from engine import CLOSE_MIN, OPEN_MIN

# (產業代碼, 成交額占比)
SIM_INDUSTRIES = [
    ("24", 30), ("28", 10), ("25", 10), ("26", 4), ("27", 5), ("31", 4), ("17", 6), ("15", 3),
    ("22", 3), ("03", 1.5), ("10", 1.5), ("05", 4), ("21", 1), ("08", 0.6), ("23", 1), ("14", 2),
    ("36", 0.5), ("30", 0.7), ("29", 1.5), ("32", 0.3), ("20", 2), ("35", 1), ("02", 0.8),
    ("04", 0.8), ("12", 0.7), ("16", 0.5), ("33", 0.1), ("01", 0.5), ("18", 0.5), ("06", 0.8),
]
# 劇本：幾個族群有明確方向（其餘隨機遊走）
SCRIPT = {"24": -0.10, "25": +0.12, "08": +0.08, "28": -0.03}


def build_universe(seed=7, extra_codes=None):
    rnd = random.Random(seed)
    stocks, weight = {}, {}
    from groups import industry_name
    k = 1
    for cat, share in SIM_INDUSTRIES:
        n = max(3, int(4 + share * 2.2 + rnd.random() * 6))
        raw = [1.0 / (i + 1) ** (1.6 if cat == "24" else 1.1) for i in range(n)]
        tot = sum(raw)
        for i in range(n):
            code = "S%04d" % k
            k += 1
            stocks[code] = {"name": "%s%02d" % (industry_name(cat), i + 1), "category": cat,
                            "unit": 1000, "exchange": "SIM", "reference": round(rnd.uniform(15, 900), 1)}
            weight[code] = share * raw[i] / tot
    # 族群檔裡的真實代號也放進模擬（名稱沿用檔案，行情是假的）
    elec = ["24", "28", "25", "26", "27", "31", "24", "28"]
    for i, (code, name) in enumerate(sorted((extra_codes or {}).items())):
        cat = elec[int(code) % len(elec)] if code.isdigit() else "24"
        stocks[code] = {"name": name or code, "category": cat, "unit": 1000, "exchange": "SIM",
                        "reference": round(rnd.uniform(30, 900), 1)}
        weight[code] = (8.0 if code == "2330" else 0.35) * rnd.lognormvariate(0, 0.8)
    return stocks, weight


class SimFeed(object):
    def __init__(self, speed=20.0, start="09:00", per_connection=200, seed=None, day_turnover=6000e8,
                 snapshot_every=60, extra_codes=None):
        self.speed = float(speed)
        h, m = start.split(":")
        self.start_min = int(h) * 60 + int(m)
        self.per_connection = per_connection
        self.rnd = random.Random(seed)
        self.day_turnover = day_turnover
        self.snapshot_every = snapshot_every
        self.extra = set(extra_codes or {})
        self.stocks, self.weight = build_universe(extra_codes=extra_codes)
        self.stop_evt = threading.Event()
        self.engine = None

    def prepare(self):
        return self.stocks

    def start(self, engine):
        self.engine = engine
        ranked = sorted(self.stocks, key=lambda c: (c not in self.extra, -self.weight[c]))
        self.tick_codes = set(ranked[: self.per_connection])
        engine.set_tick_codes(self.tick_codes)
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_evt.set()

    def _bias(self, cat, x, phase):
        # x: 0~1 交易時間進度
        base = SCRIPT.get(cat, 0.0) * (0.4 + 1.2 * x)
        wave = 0.07 * math.sin(2 * math.pi * (x * (1.3 + phase[1]) + phase[0]))
        return max(-0.32, min(0.32, base + wave))

    def _run(self):
        rnd = self.rnd
        codes = list(self.stocks)
        cum_w, acc = [], 0.0
        for c in codes:
            acc += self.weight[c]
            cum_w.append(acc)
        phase = {cat: (rnd.random(), rnd.random()) for cat, _ in SIM_INDUSTRIES}
        price = {c: v["reference"] for c, v in self.stocks.items()}
        ask_tot = dict.fromkeys(codes, 0)
        bid_tot = dict.fromkeys(codes, 0)
        total = dict.fromkeys(codes, 0.0)
        vwap_num = dict.fromkeys(codes, 0.0)
        last_tt = dict.fromkeys(codes, 0)
        ticks_per_min = min(3000, int(4000 * 60 / self.speed))
        n_session = CLOSE_MIN - OPEN_MIN
        ix = {"tse": [44920.14, 0.0], "otc": [302.15, 0.0]}      # [昨收, 目前漲跌%]
        next_ix = None
        from engine import tw_today
        today = tw_today()
        sim_t = _dt.datetime.combine(today, _dt.time(self.start_min // 60, self.start_min % 60))
        if self.start_min < OPEN_MIN:
            sim_t = _dt.datetime.combine(today, _dt.time(8, 59, 50))
        next_snap = sim_t
        wall = time.time()
        while not self.stop_evt.is_set():
            time.sleep(0.05)
            now = time.time()
            dt_sim = (now - wall) * self.speed
            wall = now
            t0, sim_t = sim_t, sim_t + _dt.timedelta(seconds=dt_sim)
            mins = sim_t.hour * 60 + sim_t.minute + sim_t.second / 60.0
            if OPEN_MIN <= mins < 13 * 60 + 25:
                x = (mins - OPEN_MIN) / n_session
                intensity = 1.0 + 1.6 * math.exp(-x * 9) + 0.8 * math.exp(-(1 - x) * 12)  # U 型量能
                n = int(ticks_per_min * intensity * dt_sim / 60.0 + rnd.random())
                avg_amt = self.day_turnover / (n_session * ticks_per_min * 1.25)
                for j in range(n):
                    c = codes[min(len(codes) - 1, _bisect(cum_w, rnd.random() * acc))]
                    cat = self.stocks[c]["category"]
                    p_buy = 0.5 + self._bias(cat, x, phase[cat])
                    r = rnd.random()
                    tt = 0 if r < 0.06 else (1 if rnd.random() < p_buy else 2)
                    px = price[c]
                    amt = avg_amt * rnd.lognormvariate(0, 0.9)
                    vol = max(1, int(amt / (px * 1000)))
                    amt = vol * px * 1000
                    ref = self.stocks[c]["reference"]
                    if tt == 1:          # 外盤 → 交易所的「買盤」累計（bid_side）
                        ask_tot[c] += vol
                        price[c] = round(min(ref * 1.1, px * (1 + 0.00025 * rnd.random())), 2)   # 漲跌幅限制 ±10%
                    elif tt == 2:
                        bid_tot[c] += vol
                        price[c] = round(max(ref * 0.9, px * (1 - 0.00025 * rnd.random())), 2)
                    total[c] += amt
                    vwap_num[c] += amt
                    last_tt[c] = tt
                    if c in self.tick_codes:
                        ts = t0 + _dt.timedelta(seconds=dt_sim * (j + 1) / (n + 1))
                        self.engine.on_tick(c, ts, amt, vol, tt, total_amount=total[c],
                                            buy_tot=ask_tot[c], sell_tot=bid_tot[c],
                                            avg_price=price[c], close=price[c])
            if OPEN_MIN <= mins <= CLOSE_MIN and (next_ix is None or sim_t >= next_ix):
                next_ix = sim_t + _dt.timedelta(seconds=5)
                x = (mins - OPEN_MIN) / n_session
                avg_b = sum(self._bias(cat, x, phase[cat]) for cat, _ in SIM_INDUSTRIES) / len(SIM_INDUSTRIES)
                for k, beta in (("tse", 1.0), ("otc", 1.3)):
                    v = ix[k]
                    v[1] = max(-9.9, min(9.9, v[1] + avg_b * 0.05 * beta + rnd.gauss(0, 0.012)))
                    close = v[0] * (1 + v[1] / 100)
                    self.engine.on_index(k, close, close - v[0], v[1])
            if sim_t >= next_snap:
                next_snap = sim_t + _dt.timedelta(seconds=self.snapshot_every)
                snaps = [{"code": c, "total_amount": total[c], "close": price[c],
                          "tick_type": {1: "Buy", 2: "Sell"}.get(last_tt[c], "None")}
                         for c in codes if c not in self.tick_codes]
                self.engine.on_snapshots(snaps, sim_t)
            self.engine.tick_clock(sim_t)
            if self.engine.finished:
                self.engine.log_event("模擬收盤")
                return


def _bisect(a, x):
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo
