# -*- coding: utf-8 -*-
"""主動買賣淨額計算引擎。

主動買賣淨額 = 外盤成交金額 − 內盤成交金額
  * 逐筆 Tick（tick_type 1=外盤、2=內盤、0=無法判定）→ 精確累加
  * 沒訂閱 Tick 的股票 → 用快照的成交金額變化估算（價漲算外盤、價跌算內盤、平盤看最後一筆內外盤）
  * 中途啟動或斷線重連 → 用 Tick v1 內建的「累計外盤量/內盤量」補回漏掉的部分（以均價估算）

所有金額內部用「元」，輸出用「億」。
"""
import datetime as _dt
import json
import os
import threading
import time

YI = 1e8
OPEN_MIN, CLOSE_MIN = 9 * 60, 13 * 60 + 30          # 09:00 ~ 13:30
TRIAL_START = _dt.time(13, 25)                        # 13:25~13:30 收盤前試撮
T_OPEN, T_END = _dt.time(9, 0), _dt.time(13, 36)          # 收盤可能因瞬間價格穩定措施延到 13:33


def tw_now():
    """台灣時間（不受主機時區影響；台灣沒有日光節約）。"""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(hours=8)


def tw_today():
    return tw_now().date()


def hhmm(m):
    return "%02d:%02d" % (m // 60, m % 60)


def parse_hhmm(s):
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def _r(x):
    return round(x / YI, 4)


AUCTION_OPEN_END = _dt.time(9, 5)                     # 每檔「今天第一筆」若在這之前 → 開盤集合競價


def is_auction(t, first):
    """集合競價的成交沒有主動的一方（買賣單一起撮合），只算成交額、不算主動買賣。
    t: datetime.time；first: 是否為這檔今天的第一筆成交。
    開盤：第一筆且在 09:05 前（含瞬間價格穩定措施延後開盤）；收盤：13:25 之後的成交（13:30 收盤撮合、延後收盤）。
    shioaji 實測（2026-11-03）：這兩種成交的 tick_type 仍會標成 1/2，所以要自己辨認。"""
    return t >= TRIAL_START or (first and t < AUCTION_OPEN_END)


def in_session(t):
    """t: datetime.time。排除試撮時段與盤外。"""
    if t < T_OPEN or t >= T_END:
        return False
    if TRIAL_START <= t < _dt.time(13, 30):
        return False
    return True


class StockState(object):
    __slots__ = ("buy", "sell", "neutral", "est_buy", "est_sell", "total",
                 "buy_tot", "sell_tot", "need_repair", "last_dt",
                 "snap_total", "snap_close", "tail_base", "px", "pct")

    def __init__(self):
        self.buy = self.sell = self.neutral = 0.0
        self.est_buy = self.est_sell = 0.0
        self.total = 0.0
        self.buy_tot = self.sell_tot = 0     # 交易所累計外盤量 / 內盤量（張）
        self.need_repair = False
        self.last_dt = None
        self.snap_total = None   # 上一次快照的累計成交金額（尾端估算用）
        self.snap_close = None
        self.tail_base = 0.0     # 尾端股票：啟動前已成交、無法判斷方向的金額
        self.px = None           # 最新成交價
        self.pct = None          # 漲跌幅 %

    @property
    def net(self):
        return (self.buy + self.est_buy) - (self.sell + self.est_sell)

    def dump(self):
        return [self.buy, self.sell, self.neutral, self.est_buy, self.est_sell, self.total,
                self.buy_tot, self.sell_tot, self.snap_total, self.snap_close, self.tail_base, self.px, self.pct]

    @classmethod
    def load(cls, a):
        s = cls()
        (s.buy, s.sell, s.neutral, s.est_buy, s.est_sell, s.total,
         s.buy_tot, s.sell_tot, s.snap_total, s.snap_close, s.tail_base) = a[:11]
        if len(a) >= 13:
            s.px, s.pct = a[11], a[12]
        return s


def _tick_sign(v):
    """快照的 tick_type 可能是 enum / 字串 / 數字。"""
    s = str(v)
    if "Buy" in s or s == "1":
        return 1
    if "Sell" in s or s == "2":
        return -1
    return 0


class FlowEngine(object):
    def __init__(self, stocks, groupsets, date, data_dir="data", mode="live",
                 start_time=None, side_swap=False, repair=True, stock_ckpt_every=5, auction_neutral=True):
        """stocks: {code: {"name", "category", "unit"}}
        groupsets: [GroupSet, ...]（第一組是預設畫面）；也可以直接給一個 GroupMap"""
        self.stocks = stocks
        self.sets = _as_sets(groupsets)
        self.date = date                      # "YYYY-MM-DD"
        self.data_dir = data_dir
        self.mode = mode
        self.side_swap = side_swap
        self.repair = repair
        self.auction_neutral = auction_neutral
        self.stock_ckpt_every = stock_ckpt_every
        self.lock = threading.RLock()
        self.st = {c: StockState() for c in stocks}
        self.tick_codes = set()
        self.frames = []
        self.clock = None                     # 最新資料時間 (datetime)
        self.last_tick_wall = None
        self.tick_count = 0
        self.events = []
        self.index = {}                       # {"tse": [指數, 漲跌點, 漲跌%], "otc": [...]}
        self._gross = 0.0                     # 全市場總進出（外盤＋內盤金額）
        start_time = start_time or tw_now()
        st_min = start_time.hour * 60 + start_time.minute
        self.started_midday = st_min >= OPEN_MIN and start_time.time() > _dt.time(9, 0, 30)
        self.next_frame_min = OPEN_MIN if st_min <= OPEN_MIN else min(st_min, CLOSE_MIN + 1)
        self.finished = False
        os.makedirs(data_dir, exist_ok=True)

    # ---------- 設定 ----------
    def set_tick_codes(self, codes):
        with self.lock:
            self.tick_codes = set(codes)
            if self.started_midday and self.repair:
                for c in self.tick_codes:
                    if c in self.st:
                        self.st[c].need_repair = True

    def log_event(self, msg):
        with self.lock:
            stamp = tw_now().strftime("%H:%M:%S")
            self.events.append("%s %s" % (stamp, msg))
            self.events = self.events[-20:]
        print("[event]", msg, flush=True)

    def mark_reconnect(self):
        """斷線重連後，下一筆 tick 用累計外/內盤量補回漏掉的成交。"""
        if not self.repair:
            return
        with self.lock:
            for c in self.tick_codes:
                self.st[c].need_repair = True
        self.log_event("重新連線，啟動缺口補值")

    # ---------- 資料輸入 ----------
    def _set_px(self, code, s, px, pct=None):
        """記錄最新價與漲跌幅：優先用參考價（昨收）計算，沒有參考價才用資料源給的漲跌幅。"""
        if not px:
            return
        s.px = float(px)
        ref = self.stocks[code].get("reference") or 0
        if ref > 0:
            s.pct = round((s.px - ref) / ref * 100, 2)
        elif pct is not None:
            s.pct = round(float(pct), 2)

    def on_tick(self, code, dt, amount, volume, tick_type, total_amount=None,
                buy_tot=None, sell_tot=None, avg_price=None, close=None,
                simtrade=False, intraday_odd=False, pct=None):
        """buy_tot / sell_tot：交易所提供的開盤至今累計外盤量 / 內盤量（張），中途啟動與斷線補值用。
        shioaji TickSTKv1：bid_side_total_vol = 買盤（外盤）、ask_side_total_vol = 賣盤（內盤）。"""
        if simtrade or intraday_odd:
            return
        if not in_session(dt.time()):
            return
        s = self.st.get(code)
        if s is None:
            return
        if self.side_swap and buy_tot is not None:
            buy_tot, sell_tot = sell_tot, buy_tot
        with self.lock:
            # 收到下一分鐘的第一筆 → 先把已經結束的分鐘收格（以成交時間為準，邊界精確）
            tm = min(dt.hour * 60 + dt.minute, CLOSE_MIN)   # 延後收盤的成交併入 13:30 這格
            while not self.finished and self.next_frame_min < tm and self.next_frame_min <= CLOSE_MIN:
                self._record_frame(self.next_frame_min)
                self.next_frame_min += 1
            if s.need_repair and buy_tot is not None:
                before_buy = buy_tot - (volume if tick_type == 1 else 0)
                before_sell = sell_tot - (volume if tick_type == 2 else 0)
                miss_b, miss_s = before_buy - s.buy_tot, before_sell - s.sell_tot
                px = float(avg_price or close or 0)
                unit = self.stocks[code].get("unit", 1000) or 1000
                if miss_b > 0:
                    s.est_buy += miss_b * px * unit
                if miss_s > 0:
                    s.est_sell += miss_s * px * unit
                s.need_repair = False
            if buy_tot is not None:
                s.buy_tot, s.sell_tot = buy_tot, sell_tot
            self._set_px(code, s, close, pct)
            tt = tick_type
            if self.auction_neutral and is_auction(dt.time(), not self.started_midday and not (s.buy or s.sell or s.neutral)):
                tt = 0                      # 集合競價：只算成交額
            if tt == 1:
                s.buy += amount
            elif tt == 2:
                s.sell += amount
            else:
                s.neutral += amount
            if total_amount is not None:
                s.total = float(total_amount)
            else:
                s.total = s.buy + s.sell + s.neutral + s.est_buy + s.est_sell
            s.last_dt = dt
            if self.clock is None or dt > self.clock:
                self.clock = dt
            self.last_tick_wall = time.time()
            self.tick_count += 1

    def on_index(self, key, close, chg, pct):
        """加權（tse）/ 上櫃（otc）指數。"""
        if not close:
            return
        with self.lock:
            self.index[key] = [round(float(close), 2), round(float(chg or 0), 2), round(float(pct or 0), 2)]

    def on_snapshots(self, snaps, snap_time=None):
        """snaps: iterable of dict(code, total_amount, close, tick_type)。
        只對「沒訂閱 Tick」的股票做估算；有訂閱的只拿來對帳。"""
        pre_open = snap_time is not None and snap_time.time() <= _dt.time(9, 0, 30)
        st_t = snap_time.time() if snap_time is not None else None
        with self.lock:
            for sn in snaps:
                code = sn["code"]
                s = self.st.get(code)
                if s is None:
                    continue
                if sn.get("date") and sn["date"] != self.date:
                    continue        # 開盤前或今天還沒成交的股票，快照仍是前一天的數字 → 不能算進今天
                total = float(sn.get("total_amount") or 0)
                close = float(sn.get("close") or 0)
                if close:
                    self._set_px(code, s, close, sn.get("change_rate"))
                if code in self.tick_codes:
                    s.snap_total, s.snap_close = total, close
                    if s.total <= 0:          # 還沒收到第一筆 tick，先用快照的成交額撐河道粗細
                        s.total = total
                    continue
                first = False
                if s.snap_total is None and not self.started_midday:
                    if total <= 0:
                        continue            # 今天還沒成交：等第一份有量的快照
                    # 開盤前就啟動：今天第一次看到這檔有量 → 從 0 開始算，漲跌以昨收（參考價）為基準
                    s.snap_total, s.snap_close = 0.0, (self.stocks[code].get("reference") or None)
                    first = True
                if s.snap_total is None:
                    # 盤中才啟動：之前已成交的量無法判斷方向，只算進成交額
                    s.tail_base = 0.0 if pre_open else total
                elif total > s.snap_total:
                    d = total - s.snap_total
                    if self.auction_neutral and st_t is not None and (
                            st_t >= _dt.time(13, 30) or (first and st_t < AUCTION_OPEN_END)):
                        sign = 0            # 這段量主要是集合競價（收盤撮合／開盤）→ 不分買賣
                    elif close > (s.snap_close or 0) > 0:
                        sign = 1
                    elif 0 < close < (s.snap_close or 0):
                        sign = -1
                    else:
                        sign = _tick_sign(sn.get("tick_type"))
                    if sign > 0:
                        s.est_buy += d
                    elif sign < 0:
                        s.est_sell += d
                    else:
                        s.neutral += d
                s.snap_total, s.snap_close = total, close
                s.total = total

    # ---------- 彙總 ----------
    def _aggregate(self):
        from groups import industry_class
        per = {gs.id: ({}, {}, {}, {}) for gs in self.sets}      # net, turn, n, cls-weight
        m_net = m_turn = m_gross = 0.0
        tick_turn = tail_turn = est_amt = 0.0
        for code, s in self.st.items():
            net, turn = s.net, s.total
            if turn <= 0 and net == 0:
                continue
            m_net += net
            m_turn += turn
            m_gross += s.buy + s.est_buy + s.sell + s.est_sell
            if code in self.tick_codes:
                tick_turn += turn
                est_amt += s.est_buy + s.est_sell
            else:
                tail_turn += turn
            k = industry_class(self.stocks[code].get("category"))
            for gs in self.sets:
                g_net, g_turn, g_n, g_cls = per[gs.id]
                for g in gs.gm.groups_of(code):
                    g_net[g] = g_net.get(g, 0.0) + net
                    g_turn[g] = g_turn.get(g, 0.0) + turn
                    g_n[g] = g_n.get(g, 0) + 1
                    by = g_cls.setdefault(g, {})
                    by[k] = by.get(k, 0.0) + max(turn, 1.0)
        out = {}
        for gs in self.sets:
            g_net, g_turn, g_n, w = per[gs.id]
            out[gs.id] = (g_net, g_turn, g_n, _pick_class(w, gs.gm.group_class))
        cov = {"tick_n": len(self.tick_codes), "tail_n": max(0, len(self.st) - len(self.tick_codes)),
               "tick_turnover": _r(tick_turn), "tail_turnover": _r(tail_turn),
               "est_ratio": round((est_amt + tail_turn) / m_turn, 4) if m_turn else 0.0}
        self._gross = m_gross
        return m_net, m_turn, out, cov

    def state(self, only_set=None):
        """only_set：只回傳這組族群的明細（手機省流量）；其他組只留設定。"""
        with self.lock:
            m_net, m_turn, per, cov = self._aggregate()
            clock = self.clock.strftime("%H:%M:%S") if self.clock else None
            age = round(time.time() - self.last_tick_wall, 1) if self.last_tick_wall else None
            sets = []
            for gs in self.sets:
                g_net, g_turn, g_n, g_cls = per[gs.id]
                d = gs.meta()
                members = [c for c in self.stocks if gs.gm.groups_of(c)]
                d["members"] = len(members)
                d["members_tick"] = sum(1 for c in members if c in self.tick_codes)
                if only_set and gs.id != only_set:
                    d["groups"] = []
                else:
                    lead = self._leaders(gs)
                    d["groups"] = [
                        {"name": g, "net": _r(g_net[g]), "turnover": _r(g_turn.get(g, 0)),
                         "n": g_n.get(g, 0), "cls": g_cls.get(g, "傳產"),
                         "up": lead.get(g, ([], [], None))[0], "dn": lead.get(g, ([], [], None))[1],
                         "avg": lead.get(g, ([], [], None))[2]} for g in g_net]
                sets.append(d)
            return {"date": self.date, "mode": self.mode, "clock": clock,
                    "server_time": tw_now().strftime("%H:%M:%S"),
                    "session": [hhmm(OPEN_MIN), hhmm(CLOSE_MIN)],
                    "market": {"net": _r(m_net), "turnover": _r(m_turn), "gross": _r(self._gross)},
                    "index": dict(self.index),
                    "sets": sets, "groups": sets[0]["groups"], "coverage": cov,
                    "status": {"ticks": self.tick_count, "last_tick_age": age,
                               "events": self.events[-5:], "group_source": self.sets[0].gm.source,
                               "frames": len(self.frames)}}

    def _leaders(self, gs, k=3):
        """每個族群漲幅前 k 名、跌幅前 k 名、成分股平均漲跌幅：
        {族群: ([[代號, 名稱, 漲跌%], ...], [...], 平均%)}（首頁資金欄輪播、族群點點顏色用）"""
        import heapq
        pool = {}
        for code, s in self.st.items():
            if s.pct is None or s.total <= 0:
                continue
            for g in gs.gm.groups_of(code):
                pool.setdefault(g, []).append((s.pct, code))
        out = {}
        for g, lst in pool.items():
            fmt = lambda pc: [pc[1], self.stocks[pc[1]].get("name", ""), pc[0]]
            out[g] = ([fmt(x) for x in heapq.nlargest(k, lst)], [fmt(x) for x in heapq.nsmallest(k, lst)],
                      round(sum(x[0] for x in lst) / len(lst), 2))
        return out

    def _set(self, sid):
        for gs in self.sets:
            if gs.id == sid:
                return gs
        return self.sets[0]

    def group_detail(self, name, sid=None):
        gs = self._set(sid)
        top = set(gs.gm.top_stocks)
        with self.lock:
            rows = []
            for code, s in self.st.items():
                if name in gs.gm.groups_of(code) and (s.total > 0 or s.net):
                    rows.append({"code": code, "name": self.stocks[code].get("name", ""),
                                 "net": _r(s.net), "turnover": _r(s.total),
                                 "src": "tick" if code in self.tick_codes else "snap",
                                 "top": code in top, "pct": s.pct, "px": s.px})
            rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0)))
            return {"group": name, "set": gs.id, "stocks": rows}

    # ---------- 每分鐘存一格 ----------
    def tick_clock(self, now):
        """由外部每 0.5 秒呼叫一次；now 為「現在時間」（實盤=系統時間，模擬=模擬時間）。"""
        with self.lock:
            while not self.finished:
                m = self.next_frame_min
                if m > CLOSE_MIN:
                    self.finished = True
                    self.save()
                    break
                grace = 240 if m == CLOSE_MIN else 2        # 13:30 格等到 13:35（含延後收盤）
                close_at = _dt.datetime.combine(now.date(), _dt.time(m // 60, m % 60)) + _dt.timedelta(seconds=60 + grace)
                if now < close_at:
                    break
                self._record_frame(m)
                self.next_frame_min = m + 1

    def _record_frame(self, m):
        m_net, m_turn, per, cov = self._aggregate()
        fr = {"t": hhmm(m), "m": [_r(m_net), _r(m_turn), _r(self._gross)],       # [淨額, 成交額, 總進出]
              "g": {sid: {g: [_r(v[0][g]), _r(v[1].get(g, 0))] for g in v[0]} for sid, v in per.items()}}
        if self.index:
            fr["ix"] = {k: list(v) for k, v in self.index.items()}
        if (m - OPEN_MIN) % self.stock_ckpt_every == 0 or m == CLOSE_MIN:
            fr["s"] = {c: [_r(s.net), _r(s.total), s.pct] for c, s in self.st.items() if s.total > 0}
        self.frames.append(fr)
        self._last_cls = {sid: v[3] for sid, v in per.items()}
        self._last_cov = cov
        self.save()

    def series(self, since=None, full=False):
        with self.lock:
            frames = self.frames
            if since:
                sm = parse_hhmm(since)
                frames = [f for f in frames if parse_hhmm(f["t"]) > sm]
            if not full:
                frames = [{k: v for k, v in f.items() if k != "s"} for f in frames]
            return {"date": self.date, "source": self.mode, "frames": frames}

    # ---------- 存檔 / 還原 ----------
    def day_payload(self):
        cls = getattr(self, "_last_cls", None)
        if cls is None:
            cls = {sid: v[3] for sid, v in self._aggregate()[2].items()}
        return {"date": self.date, "source": self.mode, "format": 2,
                "sets": sets_meta(self.sets, self.stocks, cls),
                "coverage": getattr(self, "_last_cov", None),
                "names": {c: v.get("name", "") for c, v in self.stocks.items()},
                "frames": self.frames}

    def _atomic_write(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)

    def save(self):
        if self.mode not in ("live", "sim"):
            return
        base = os.path.join(self.data_dir, self.date)
        self._atomic_write(base + ".%s.json" % self.mode, self.day_payload())
        if self.mode == "live":
            self._atomic_write(base + ".state.json", {
                "date": self.date, "next_frame_min": self.next_frame_min,
                "tick_codes": sorted(self.tick_codes),
                "stocks": {c: s.dump() for c, s in self.st.items() if s.total > 0 or s.snap_total is not None}})

    def try_restore(self):
        """同一天重開程式：接續之前的累計值與時間軸。"""
        base = os.path.join(self.data_dir, self.date)
        try:
            ck = json.load(open(base + ".state.json", encoding="utf-8"))
            day = json.load(open(base + ".live.json", encoding="utf-8"))
        except (IOError, OSError, ValueError):
            return False
        if ck.get("date") != self.date:
            return False
        with self.lock:
            for c, a in ck.get("stocks", {}).items():
                if c in self.st:
                    self.st[c] = StockState.load(a)
                    self.st[c].need_repair = self.repair
            self.frames = day.get("frames", [])
            if self.frames:
                self.next_frame_min = max(self.next_frame_min, parse_hhmm(self.frames[-1]["t"]) + 1)
        self.log_event("已從存檔接續（%d 格）" % len(self.frames))
        return True


# ---------- 盤後：用整天的逐筆資料建出精確版回放 ----------
def bin_add(bins, minute, amount, tick_type, close=None):
    """bins: {minute_of_day: [外盤金額, 內盤金額, 無法判定金額, 該分鐘最後成交價]}"""
    minute = min(minute, CLOSE_MIN)
    b = bins.get(minute)
    if b is None:
        b = bins[minute] = [0.0, 0.0, 0.0, None]
    if close:
        b[3] = close
    if tick_type == 1:
        b[0] += amount
    elif tick_type == 2:
        b[1] += amount
    else:
        b[2] += amount


def build_frames_from_bins(stock_bins, stocks, groupsets, stock_ckpt_every=5, with_pct=True):
    """stock_bins: {code: {minute: [buy, sell, neutral]}}
    回傳 (frames, sets_meta)，格式與即時錄製相同（每分鐘一格，數值為開盤至該分鐘結束的累計）。"""
    from groups import industry_class
    sets = _as_sets(groupsets)
    codes = list(stock_bins)
    cum_net = dict.fromkeys(codes, 0.0)
    cum_turn = dict.fromkeys(codes, 0.0)
    cum_gross = dict.fromkeys(codes, 0.0)
    last_px = dict.fromkeys(codes)
    frames = []

    def pct_of(c):
        ref = stocks.get(c, {}).get("reference") or 0
        return round((last_px[c] - ref) / ref * 100, 2) if (with_pct and ref > 0 and last_px[c]) else None

    for m in range(OPEN_MIN, CLOSE_MIN + 1):
        for c in codes:
            b = stock_bins[c].get(m)
            if b:
                cum_net[c] += b[0] - b[1]
                cum_turn[c] += b[0] + b[1] + b[2]
                cum_gross[c] += b[0] + b[1]
                if len(b) > 3 and b[3]:
                    last_px[c] = b[3]
        per = {gs.id: ({}, {}) for gs in sets}
        m_net = m_turn = m_gross = 0.0
        for c in codes:
            t = cum_turn[c]
            if t <= 0:
                continue
            n = cum_net[c]
            m_gross += cum_gross[c]
            m_net += n
            m_turn += t
            for gs in sets:
                g_net, g_turn = per[gs.id]
                for g in gs.gm.groups_of(c):
                    g_net[g] = g_net.get(g, 0.0) + n
                    g_turn[g] = g_turn.get(g, 0.0) + t
        fr = {"t": hhmm(m), "m": [_r(m_net), _r(m_turn), _r(m_gross)],
              "g": {sid: {g: [_r(v[0][g]), _r(v[1][g])] for g in v[0]} for sid, v in per.items()}}
        if (m - OPEN_MIN) % stock_ckpt_every == 0 or m == CLOSE_MIN:
            fr["s"] = {c: [_r(cum_net[c]), _r(cum_turn[c]), pct_of(c)] for c in codes if cum_turn[c] > 0}
        frames.append(fr)
    cls = {}
    for gs in sets:
        w = {}
        for c in codes:
            k = industry_class(stocks.get(c, {}).get("category"))
            for g in gs.gm.groups_of(c):
                by = w.setdefault(g, {})
                by[k] = by.get(k, 0.0) + max(cum_turn[c], 1.0)
        cls[gs.id] = _pick_class(w, gs.gm.group_class)
    return frames, sets_meta(sets, stocks, cls)


# ---------- 共用 ----------
def _as_sets(groupsets):
    from groups import GroupMap, GroupSet
    if isinstance(groupsets, GroupMap):
        return [GroupSet("industry" if groupsets.format == "industry" else "custom",
                         "產業別" if groupsets.format == "industry" else "我的族群", groupsets)]
    return list(groupsets)


def _pick_class(weights, fixed):
    """族群圓點顏色：檔案有指定就用，否則取成分股（依成交額加權）最多的產業大類。"""
    return {g: fixed.get(g) or (max(by.items(), key=lambda kv: kv[1])[0] if by else "傳產")
            for g, by in weights.items()}


def sets_meta(sets, stocks, cls):
    out = []
    for gs in sets:
        d = gs.meta()
        d["group_class"] = cls.get(gs.id, {})
        d["stock_groups"] = {c: list(gs.gm.groups_of(c)) for c in stocks if gs.gm.groups_of(c)}
        out.append(d)
    return out
