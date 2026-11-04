# -*- coding: utf-8 -*-
"""shioaji 資料源。

同時相容舊版（api.Contracts / api.quote.subscribe）與 1.7 之後的新版（api.contracts / api.subscribe）。
使用限制（官方文件 Use Restrictions）：
  * api.subscribe 上限 200 檔；同一 person_id 最多 5 個連線
  * snapshots / ticks / kbars 合計 10 秒內最多 50 次；盤中 ticks 查詢不得超過 10 次
  * 訂閱推播不計流量；查詢類依近 30 日成交額每日 500MB / 2GB / 10GB
"""
import datetime as _dt
import multiprocessing as mp
import os
import threading
import time

try:
    import shioaji as sj
except ImportError:  # 讓模擬模式不裝 shioaji 也能跑
    sj = None


from engine import tw_now  # noqa: E402

# ---------------- 基本工具 ----------------
class RateLimiter(object):
    """查詢類 API 的節流：預設每 0.25 秒最多一次（=10 秒 40 次，留點餘裕）。"""

    def __init__(self, min_interval=0.25):
        self.min_interval = min_interval
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            d = time.time() - self.last
            if d < self.min_interval:
                time.sleep(self.min_interval - d)
            self.last = time.time()


RATE = RateLimiter()


def _need_sj():
    if sj is None:
        raise SystemExit("找不到 shioaji，請先 pip install shioaji")


def login(cfg, quiet=False):
    _need_sj()
    api = sj.Shioaji(simulation=cfg.getboolean("shioaji", "simulation", fallback=False))
    unq = lambda v: (v or "").strip().strip('"').strip("'").strip()   # 有沒有加引號都可以
    key = os.environ.get("SJ_API_KEY") or unq(cfg.get("shioaji", "api_key", fallback=""))
    sec = os.environ.get("SJ_SEC_KEY") or unq(cfg.get("shioaji", "secret_key", fallback=""))
    kf = cfg.get("shioaji", "key_file", fallback="").strip().strip('"')
    if (not key or not sec) and kf:
        # 沿用既有程式的金鑰檔（例如 monitor_bot 的 user_config.json：{"api_key": ..., "secret_key": ...}）
        import json
        try:
            kd = json.load(open(kf, encoding="utf-8-sig"))
        except (IOError, OSError, ValueError) as e:
            raise SystemExit("讀不到金鑰檔 %s：%s" % (kf, e))
        key = key or kd.get("api_key") or kd.get("SJ_API_KEY") or ""
        sec = sec or kd.get("secret_key") or kd.get("SJ_SEC_KEY") or ""
    if not key or not sec:
        raise SystemExit("請在 config.ini 的 [shioaji] 填 api_key / secret_key（或設環境變數 SJ_API_KEY / SJ_SEC_KEY）")
    tries = [dict(subscribe_trade=False, contracts_timeout=30000),   # 舊版：等合約下載完
             dict(subscribe_trade=False),                            # 1.7+：合約按需載入
             dict()]
    last = None
    for extra in tries:
        try:
            api.login(api_key=key, secret_key=sec, **extra)
            break
        except TypeError as e:
            last = e
            continue
    else:
        raise last
    if not quiet:
        print("[shioaji] 登入成功，版本 %s" % getattr(sj, "__version__", "?"), flush=True)
    return api


def logout(api):
    try:
        api.logout()
    except Exception:
        pass


def get_contract(api, code):
    try:
        c = api.contracts.get(code)
        if c is not None:
            return c
    except Exception:
        pass
    return api.Contracts.Stocks[code]


def _is_common_stock(code, include_etf):
    if not code.isdigit():
        return False
    if len(code) == 4 and not code.startswith("0"):
        return True
    return include_etf and code.startswith("00")


def list_stocks(api, include_etf=False):
    """回傳 {code: {name, category, exchange, unit, reference, contract}}，只留普通股（可選含 ETF）。"""
    out = {}

    def add(c, exch):
        code = str(c.code)
        if not _is_common_stock(code, include_etf):
            return
        cat = str(getattr(c, "category", "") or "")
        if include_etf and code.startswith("00"):
            cat = "ETF"
        out[code] = {"name": str(getattr(c, "name", "") or ""), "category": cat,
                     "exchange": exch, "unit": float(getattr(c, "unit", 1000) or 1000),
                     "reference": float(getattr(c, "reference", 0) or 0), "contract": c}

    try:  # 舊版寫法（1.7 仍可用，只是不再維護）
        for exch in ("TSE", "OTC"):
            for c in getattr(api.Contracts.Stocks, exch):
                add(c, exch)
    except Exception as e:
        print("[shioaji] 舊版合約介面不可用（%s），改用新版 api.contracts.list" % e, flush=True)
    if not out:
        for c in api.contracts.list(sj.SecurityType.Stock):
            info = c if hasattr(c, "category") else api.contracts.info(c)
            exch = str(getattr(c, "exchange", "")).split(".")[-1]
            if exch in ("TSE", "OTC"):
                add(info, exch)
            if "contract" in out.get(str(c.code), {}):
                out[str(c.code)]["contract"] = c
    print("[shioaji] 普通股 %d 檔（上市 %d / 上櫃 %d）" % (
        len(out), sum(1 for v in out.values() if v["exchange"] == "TSE"),
        sum(1 for v in out.values() if v["exchange"] == "OTC")), flush=True)
    return out


# 加權指數 / 上櫃指數：新舊版 shioaji 代號不同，依序嘗試已知代號，找不到再用名稱搜尋
INDEX_SPEC = {
    "tse": ("TSE", ["IX0001", "001", "TSE001"], ["加權指數", "發行量加權股價指數"], "加權"),
    "otc": ("OTC", ["IX0043", "101", "OTC101"], ["櫃買指數", "櫃檯指數", "OTC指數"], "櫃買"),
}


def find_indexes(api, override=None):
    """回傳 {"tse": contract, "otc": contract}（找不到的就沒有那個 key）"""
    override = override or {}
    found = {}
    idx = getattr(getattr(api, "Contracts", None), "Indexs", None)
    for key, (exch, codes, names, word) in INDEX_SPEC.items():
        grp = getattr(idx, exch, None) if idx is not None else None
        codes = ([override[key]] if override.get(key) else []) + codes
        c = None
        for code in codes:
            for getter in ((lambda k: grp[k]) if grp is not None else None, lambda k: api.contracts.get(k)):
                if getter is None or c is not None:
                    continue
                try:
                    c = getter(code)
                except Exception:
                    c = None
            if c is not None:
                break
        if c is None and grp is not None:
            try:
                items = list(grp)
                c = next((x for x in items if str(getattr(x, "name", "")) in names), None) or \
                    next((x for x in items if word in str(getattr(x, "name", "")) and "報酬" not in str(getattr(x, "name", ""))), None)
            except Exception:
                c = None
        if c is not None:
            found[key] = c
    print("[shioaji] 指數：" + "、".join("%s=%s %s" % (k, getattr(v, "code", "?"), getattr(v, "name", ""))
                                       for k, v in found.items()) if found else "[shioaji] 找不到加權／上櫃指數合約", flush=True)
    return found


def fetch_snapshots(api, contracts):
    res = []
    for i in range(0, len(contracts), 500):
        RATE.wait()
        try:
            snaps = api.snapshots(contracts[i:i + 500])
        except Exception as e:
            print("[shioaji] snapshots 失敗：%s" % e, flush=True)
            continue
        for s in snaps:
            ts = getattr(s, "ts", None)
            res.append({"code": str(s.code), "total_amount": float(s.total_amount or 0),
                        "date": ns_to_local(ts).date().isoformat() if ts else None,
                        "close": float(s.close or 0), "tick_type": getattr(s, "tick_type", None),
                        "change_rate": float(getattr(s, "change_rate", 0) or 0),
                        "yesterday_volume": float(getattr(s, "yesterday_volume", 0) or 0)})
    return res


def _set_tick_callback(api, cb):
    for owner in (getattr(api, "quote", None), api):
        f = getattr(owner, "set_on_tick_stk_v1_callback", None) if owner is not None else None
        if f:
            f(cb)
            return
    api.on_tick_stk_v1()(cb)


def _set_event_callback(api, cb):
    for owner in (getattr(api, "quote", None), api):
        f = getattr(owner, "set_event_callback", None) if owner is not None else None
        if f:
            try:
                f(cb)
                return
            except Exception:
                pass


def subscribe_tick(api, contract):
    try:
        api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick,
                            version=sj.constant.QuoteVersion.v1)
    except (AttributeError, TypeError):
        api.subscribe(contract, quote_type=sj.QuoteType.Tick)


def tick_tuple(tick):
    """把 TickSTKv1 轉成可跨行程傳遞的 tuple（callback 內只做這件事，保持輕量）。"""
    return (str(tick.code), tick.datetime, float(tick.amount), int(tick.volume), int(tick.tick_type),
            float(tick.total_amount), int(tick.ask_side_total_vol), int(tick.bid_side_total_vol),
            float(tick.avg_price), float(tick.close), bool(tick.simtrade), bool(tick.intraday_odd),
            float(getattr(tick, "pct_chg", 0) or 0))


def feed_tuple(engine, t):
    # t[6] = ask_side_total_vol（賣盤＝內盤累計）、t[7] = bid_side_total_vol（買盤＝外盤累計）
    # 2026-11-03 實盤驗證：200 檔中 199 檔的 tick_type 累加方向與 bid_side 一致
    engine.on_tick(t[0], t[1], t[2], t[3], t[4], total_amount=t[5], buy_tot=t[7], sell_tot=t[6],
                   avg_price=t[8], close=t[9], simtrade=t[10], intraday_odd=t[11],
                   pct=t[12] if len(t) > 12 else None)


def _is_reconnect(event_code, event):
    return event_code == 13 or "reconnected" in str(event).lower()


# ---------------- 額外連線（第 2~5 個）跑在子行程 ----------------
def _worker_main(idx, codes, cfg_path, q, stop_evt):
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")
    api = login(cfg, quiet=True)
    _set_tick_callback(api, lambda exchange, tick: q.put(tick_tuple(tick)))
    _set_event_callback(api, lambda rc, ec, info, ev: q.put(("__event__", idx, ec, str(info), str(ev))))
    n = 0
    for code in codes:
        try:
            subscribe_tick(api, get_contract(api, code))
            n += 1
        except Exception as e:
            q.put(("__event__", idx, -1, "訂閱 %s 失敗：%s" % (code, e), "error"))
    q.put(("__ready__", idx, n))
    stop_evt.wait()
    logout(api)


class LiveFeed(object):
    def __init__(self, cfg, cfg_path):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.api = None
        self.stocks = {}
        self.ranked = []
        self.workers = []
        self.stop_evt = threading.Event()
        self.mp_stop = None
        self.engine = None
        self.priority = []        # 優先訂閱逐筆的股票（你的族群檔裡的股票）

    # 1) 登入、列股票、用快照排出成交額大小
    def prepare(self):
        self.api = login(self.cfg)
        self.stocks = list_stocks(self.api, self.cfg.getboolean("universe", "include_etf", fallback=False))
        contracts = [v["contract"] for v in self.stocks.values()]
        print("[shioaji] 取得全市場快照排序成交額…", flush=True)
        snaps = fetch_snapshots(self.api, contracts)
        score = {}
        for s in snaps:
            info = self.stocks.get(s["code"])
            if not info:
                continue
            px = s["close"] or info["reference"]
            score[s["code"]] = max(s["total_amount"], s["yesterday_volume"] * px * info["unit"])
        if not score:
            print("[shioaji] 警告：拿不到快照，無法依成交額挑選前 N 檔，改用代號順序訂閱（覆蓋率會很差）", flush=True)
        self.ranked = sorted(self.stocks, key=lambda c: score.get(c, 0), reverse=True)
        self._first_snaps = snaps
        return self.stocks

    # 2) 開始訂閱與輪詢
    def start(self, engine):
        self.engine = engine
        n_conn = max(1, min(5, self.cfg.getint("shioaji", "connections", fallback=1)))
        per = max(1, min(200, self.cfg.getint("shioaji", "per_connection", fallback=200)))
        pr = set(self.priority)
        order = [c for c in self.ranked if c in pr] + [c for c in self.ranked if c not in pr]
        if pr:
            print("[shioaji] 族群檔股票 %d 檔優先訂閱逐筆，其餘名額依成交額補滿" % len(pr), flush=True)
            if len(pr) > n_conn * per:
                print("[shioaji] 注意：族群檔股票超過 %d 檔訂閱上限，超出的改用快照估算；可增加 connections" % (n_conn * per), flush=True)
        chunks = [order[i * per:(i + 1) * per] for i in range(n_conn)]
        chunks = [c for c in chunks if c]
        tick_codes = [c for ch in chunks for c in ch]
        engine.set_tick_codes(tick_codes)
        engine.on_snapshots(self._first_snaps, tw_now())

        def on_tick(exchange, tick):
            try:
                feed_tuple(engine, tick_tuple(tick))
            except Exception as e:
                print("[tick] 解析失敗：%s" % e, flush=True)

        def on_event(resp_code, event_code, info, event):
            engine.log_event("連線0 事件 %s %s" % (event_code, event))
            if _is_reconnect(event_code, event):
                engine.mark_reconnect()

        _set_tick_callback(self.api, on_tick)
        _set_event_callback(self.api, on_event)
        for code in chunks[0]:
            try:
                subscribe_tick(self.api, self.stocks[code]["contract"])
            except Exception as e:
                engine.log_event("訂閱 %s 失敗：%s" % (code, e))
        engine.log_event("連線0 已訂閱 %d 檔" % len(chunks[0]))

        if len(chunks) > 1:
            ctx = mp.get_context("spawn")
            q = ctx.Queue(maxsize=200000)
            self.mp_stop = ctx.Event()
            for i, ch in enumerate(chunks[1:], start=1):
                p = ctx.Process(target=_worker_main, args=(i, ch, self.cfg_path, q, self.mp_stop), daemon=True)
                p.start()
                self.workers.append(p)
            threading.Thread(target=self._drain, args=(q,), daemon=True).start()

        threading.Thread(target=self._snapshot_loop, daemon=True).start()
        threading.Thread(target=self._index_loop, daemon=True).start()
        threading.Thread(target=self._clock_loop, daemon=True).start()

    def _drain(self, q):
        while not self.stop_evt.is_set():
            try:
                t = q.get(timeout=1)
            except Exception:
                continue
            if t[0] == "__event__":
                self.engine.log_event("連線%d 事件 %s %s %s" % (t[1], t[2], t[4], t[3]))
                if _is_reconnect(t[2], t[4]):
                    self.engine.mark_reconnect()
            elif t[0] == "__ready__":
                self.engine.log_event("連線%d 已訂閱 %d 檔" % (t[1], t[2]))
            else:
                feed_tuple(self.engine, t)

    def _index_loop(self):
        """每 index_interval 秒（預設 5）取一次加權／上櫃指數快照（兩個合約一次查詢）。"""
        interval = self.cfg.getint("estimate", "index_interval", fallback=5)
        try:
            idx = find_indexes(self.api, {"tse": self.cfg.get("estimate", "tse_index", fallback=""),
                                          "otc": self.cfg.get("estimate", "otc_index", fallback="")})
        except Exception as e:
            self.engine.log_event("找指數合約失敗：%s" % e)
            return
        if not idx or interval <= 0:
            return
        keys, contracts = list(idx), [idx[k] for k in idx]
        first = True
        while first or not self.stop_evt.wait(interval):
            was_first, first = first, False
            now = tw_now()
            if not was_first and not (_dt.time(8, 59) <= now.time() <= _dt.time(13, 36)):
                continue                       # 第一次一定抓（盤前也能先顯示昨收），之後只在盤中抓
            if self.api is None:
                return
            RATE.wait()
            try:
                snaps = self.api.snapshots(contracts)
            except Exception as e:
                print("[shioaji] 指數快照失敗：%s" % e, flush=True)
                continue
            for k, s in zip(keys, snaps):
                self.engine.on_index(k, float(s.close or 0), float(getattr(s, "change_price", 0) or 0),
                                     float(getattr(s, "change_rate", 0) or 0))

    def _snapshot_loop(self):
        interval = self.cfg.getint("estimate", "snapshot_interval", fallback=60)
        if interval <= 0:
            return
        tail = [self.stocks[c]["contract"] for c in self.ranked if c not in self.engine.tick_codes]
        last_usage = 0
        paused = False
        while not self.stop_evt.wait(interval):
            now = tw_now()
            if not (_dt.time(8, 59) <= now.time() <= _dt.time(13, 36)):
                continue
            if time.time() - last_usage > 600:
                last_usage = time.time()
                try:
                    u = self.api.usage()
                    remain = getattr(u, "remaining_bytes", None)
                    self.engine.log_event("流量剩餘 %.0f MB" % (remain / 1e6) if remain is not None else "流量 %s" % u)
                    paused = remain is not None and remain < 50e6
                    if paused:
                        self.engine.log_event("流量不足 50MB，暫停快照估算")
                except Exception:
                    pass
            if paused or not tail:
                continue
            snaps = fetch_snapshots(self.api, tail)
            self.engine.on_snapshots(snaps, now)

    def _clock_loop(self):
        auto_logout = self.cfg.getboolean("shioaji", "auto_logout", fallback=True)
        while not self.stop_evt.wait(0.5):
            now = tw_now()
            self.engine.tick_clock(now)
            if auto_logout and self.engine.finished and now.time() > _dt.time(13, 35):
                self.engine.log_event("收盤，自動登出釋放連線（畫面仍可回放）")
                self.stop()
                return

    def stop(self):
        self.stop_evt.set()
        if self.mp_stop is not None:
            self.mp_stop.set()
        for p in self.workers:
            p.join(timeout=10)
        if self.api is not None:
            logout(self.api)
            self.api = None


# ---------------- 盤後：抓全市場整天逐筆，建精確版回放 ----------------
def ns_to_local(ts):
    """shioaji 的 ts 是「台灣時間當成 UTC」的奈秒數。"""
    return _dt.datetime(1970, 1, 1) + _dt.timedelta(microseconds=int(ts) // 1000)


def fetch_day_bins(api, stocks, codes, date, progress=None, min_remaining=30e6, auction_neutral=True):
    """回傳 {code: {minute: [buy, sell, neutral]}}。依 codes 順序抓（建議依成交額由大到小），流量不足時提早停。"""
    from engine import bin_add, in_session, is_auction
    bins = {}
    offset = None
    for i, code in enumerate(codes):
        if i % 100 == 0 and i:
            try:
                u = api.usage()
                remain = getattr(u, "remaining_bytes", None)
                if remain is not None and remain < min_remaining:
                    print("[replay] 流量剩 %.0f MB，先停在第 %d 檔" % (remain / 1e6, i), flush=True)
                    break
            except Exception:
                pass
        RATE.wait()
        try:
            tk = api.ticks(contract=stocks[code]["contract"], date=date)
        except Exception as e:
            print("[replay] %s 失敗：%s" % (code, e), flush=True)
            continue
        unit = stocks[code]["unit"]
        b = {}
        ts_list = list(tk.ts)
        if ts_list and offset is None:
            # 自動判斷時區：如果大部分時間落在 01:00~05:30，代表是真 UTC，要 +8 小時
            hrs = [ns_to_local(x).hour for x in ts_list[: min(200, len(ts_list))]]
            offset = _dt.timedelta(hours=8) if sum(1 for h in hrs if 1 <= h <= 5) > len(hrs) / 2 else _dt.timedelta(0)
        first = True
        for ts, close, vol, tt in zip(ts_list, tk.close, tk.volume, tk.tick_type):
            t = ns_to_local(ts) + (offset or _dt.timedelta(0))
            if not in_session(t.time()):
                continue
            tt = int(tt)
            if auction_neutral and is_auction(t.time(), first):
                tt = 0                      # 集合競價：只算成交額（與盤中引擎相同規則）
            first = False
            bin_add(b, t.hour * 60 + t.minute, float(close) * float(vol) * unit, tt, float(close))
        if b:
            bins[code] = b
        if progress:
            progress(i + 1, len(codes), code)
    return bins
