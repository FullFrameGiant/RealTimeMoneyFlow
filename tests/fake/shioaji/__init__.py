# -*- coding: utf-8 -*-
"""測試用的假 shioaji：模擬舊版 (api.Contracts / api.quote.subscribe) 與新版 (api.contracts / api.subscribe) 介面。
FAKE_SJ_NEW=1 時只提供新版介面。"""
import datetime as _dt
import os
import random
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

__version__ = "fake-1.7" if os.environ.get("FAKE_SJ_NEW") == "1" else "fake-1.2"
NEW = os.environ.get("FAKE_SJ_NEW") == "1"
LOG = os.environ.get("FAKE_SJ_LOG")  # 檔案路徑：記錄 login/logout/subscribe


def _log(msg):
    if LOG:
        with open(LOG, "a") as f:
            f.write("%d %s\n" % (os.getpid(), msg))


class QuoteType:
    Tick = "tick"
    BidAsk = "bidask"


class QuoteVersion:
    v1 = "v1"


class SecurityType:
    Stock = "STK"


constant = SimpleNamespace(QuoteType=QuoteType, QuoteVersion=QuoteVersion, TicksQueryType=SimpleNamespace(AllDay="AllDay"))

# 假股票：(code, name, category, exchange, ref)
UNIVERSE = [("2330", "台積電", "24", "TSE", 1000.0), ("2317", "鴻海", "28", "TSE", 200.0),
            ("2454", "聯發科", "24", "TSE", 1300.0), ("2382", "廣達", "25", "TSE", 300.0),
            ("2881", "富邦金", "17", "TSE", 90.0), ("2603", "長榮", "15", "TSE", 200.0),
            ("3231", "緯創", "25", "TSE", 120.0), ("6488", "環球晶", "24", "OTC", 400.0),
            ("0050", "元大台灣50", "00", "TSE", 190.0), ("2881A", "富邦特", "17", "TSE", 60.0),
            ("030001", "某權證", "", "TSE", 1.0)]


class StockContract(object):
    def __init__(self, code, name, category, exchange, ref):
        self.code, self.name, self.category, self.exchange = code, name, category, exchange
        self.reference, self.unit = ref, 1000
        self.security_type = "STK"


class _ExchList(object):
    def __init__(self, items):
        self._items = items

    def __iter__(self):
        return iter(self._items)


class _Stocks(object):
    def __init__(self, all_c):
        self._all = {c.code: c for c in all_c}
        self.TSE = _ExchList([c for c in all_c if c.exchange == "TSE"])
        self.OTC = _ExchList([c for c in all_c if c.exchange == "OTC"])

    def __getitem__(self, code):
        return self._all[code]


class TickSTKv1(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Shioaji(object):
    def __init__(self, simulation=False):
        self._all = [StockContract(*u) for u in UNIVERSE]
        self._subs = []
        self._tick_cb = None
        self._event_cb = None
        self._state = {}
        self._run = False
        if not NEW:
            self.Contracts = SimpleNamespace(Stocks=_Stocks(self._all))
            self.quote = SimpleNamespace(subscribe=self._quote_subscribe,
                                         set_on_tick_stk_v1_callback=self._set_cb,
                                         set_event_callback=self._set_ev)
        else:
            parent = self
            self.contracts = SimpleNamespace(
                get=lambda code: next(c for c in parent._all if c.code == code),
                list=lambda st: [SimpleNamespace(code=c.code, exchange="Exchange." + c.exchange, security_type="STK") for c in parent._all],
                info=lambda c: next(x for x in parent._all if x.code == c.code))

    # --- 新版介面 ---
    def subscribe(self, contract, quote_type="tick"):
        if not NEW:
            raise AttributeError("old api")
        self._do_sub(contract)

    def set_on_tick_stk_v1_callback(self, cb):
        self._set_cb(cb)

    def set_event_callback(self, cb):
        self._set_ev(cb)

    # --- 舊版介面 ---
    def _quote_subscribe(self, contract, quote_type="tick", version=None):
        if version != "v1":
            raise TypeError("need v1")
        self._do_sub(contract)

    def _set_cb(self, cb):
        self._tick_cb = cb

    def _set_ev(self, cb):
        self._event_cb = cb

    def login(self, api_key, secret_key, subscribe_trade=True, **kw):
        if NEW and "contracts_timeout" in kw:
            raise TypeError("unexpected keyword contracts_timeout")
        _log("login %s" % ("new" if NEW else "old"))
        return []

    def logout(self):
        _log("logout")
        self._run = False
        return True

    def usage(self):
        return SimpleNamespace(connections=1, bytes=1000, limit_bytes=500e6, remaining_bytes=499e6)

    def _do_sub(self, contract):
        if len(self._subs) >= 200:
            raise RuntimeError("subscription limit")
        self._subs.append(contract.code)
        _log("sub %s" % contract.code)
        if not self._run:
            self._run = True
            threading.Thread(target=self._pump, daemon=True).start()
            if self._event_cb:
                self._event_cb(0, 16, "", "Subscribe or Unsubscribe ok")

    def _pump(self):
        rnd = random.Random(os.getpid())
        while self._run:
            time.sleep(0.01)
            if not self._subs or not self._tick_cb:
                continue
            code = rnd.choice(self._subs)
            st = self._state.setdefault(code, {"ask": 0, "bid": 0, "tot": 0.0, "px": 100.0})
            vol = rnd.randint(1, 5)
            tt = rnd.choice([1, 1, 2, 0])
            amt = vol * st["px"] * 1000
            if tt == 1:            # 實盤驗證：外盤累計在 bid_side_total_vol（買盤）
                st["bid"] += vol
            elif tt == 2:
                st["ask"] += vol
            st["tot"] += amt
            now = _dt.datetime.now()
            fake_dt = _dt.datetime.combine(now.date(), _dt.time(10, 0)) + _dt.timedelta(seconds=now.second + now.microsecond / 1e6)
            t = TickSTKv1(code=code, datetime=fake_dt, amount=Decimal(str(amt)), volume=vol, tick_type=tt,
                          total_amount=Decimal(str(st["tot"])), ask_side_total_vol=st["ask"],
                          bid_side_total_vol=st["bid"], avg_price=Decimal("100"), close=Decimal("100"),
                          simtrade=0, intraday_odd=0, pct_chg=Decimal("0"))
            self._tick_cb("TSE", t)

    def snapshots(self, contracts):
        assert len(contracts) <= 500
        out = []
        for c in contracts:
            c = next(x for x in self._all if x.code == c.code)
            out.append(SimpleNamespace(code=c.code, total_amount=int(c.reference * 1e6), close=c.reference,
                                       tick_type="Buy", yesterday_volume=1000 + int(c.reference)))
        return out

    def ticks(self, contract, date=None, **kw):
        # 09:00:00 開盤一筆 (tt=0)、09:01:30 外盤、10:15 內盤、13:27 試撮（應被排除）、13:30 收盤一筆
        base = _dt.datetime.strptime(date, "%Y-%m-%d")

        def ns(h, m, s):
            return int(((base + _dt.timedelta(hours=h, minutes=m, seconds=s)) - _dt.datetime(1970, 1, 1)).total_seconds() * 1e9)
        return SimpleNamespace(ts=[ns(9, 0, 0), ns(9, 1, 30), ns(10, 15, 0), ns(13, 27, 0), ns(13, 30, 0)],
                               close=[100.0, 101.0, 99.0, 98.0, 100.0], volume=[10, 5, 3, 7, 2],
                               tick_type=[0, 1, 2, 1, 0])
