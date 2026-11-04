# -*- coding: utf-8 -*-
"""自我測試：python tests/test_all.py（不需要 shioaji 帳號）"""
import configparser
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import FlowEngine, bin_add, build_frames_from_bins, in_session, is_auction  # noqa: E402
from groups import GroupMap, build_group_sets, industry_name, load_group_file, load_groups  # noqa: E402

TMP = tempfile.mkdtemp()
D = dt.date(2026, 10, 20)
ok = 0


def T(h, m, s=0):
    return dt.datetime.combine(D, dt.time(h, m, s))


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1


STOCKS = {"2330": {"name": "台積電", "category": "24", "unit": 1000},
          "2303": {"name": "聯電", "category": "24", "unit": 1000},
          "2382": {"name": "廣達", "category": "25", "unit": 1000},
          "2881": {"name": "富邦金", "category": "17", "unit": 1000}}


def test_groups():
    p = os.path.join(TMP, "long.csv")
    open(p, "w", encoding="utf-8-sig").write("代號,名稱,族群,大類\n2330,台積電,AI晶片、CoWoS,電子\n2382 廣達,廣達,AI伺服器,電子\n2303,聯電,成熟製程,\n")
    m, cls = load_group_file(p)
    check(m["2330"] == ["AI晶片", "CoWoS"], "長表多族群拆分")
    check(m["2382"] == ["AI伺服器"], "代號欄含名稱也能抓")
    check(cls.get("AI晶片") == "電子", "大類欄位")
    p2 = os.path.join(TMP, "wide.csv")
    open(p2, "w", encoding="cp950").write("AI伺服器,金融\n2382 廣達,2881\n2330,\n")
    m2, _ = load_group_file(p2)
    check(set(m2) == {"2382", "2881", "2330"} and m2["2881"] == ["金融"], "寬表 + cp950")
    p3 = os.path.join(TMP, "g.json")
    json.dump({"散熱": ["3017", "3324"]}, open(p3, "w", encoding="utf-8"))
    check(load_group_file(p3)[0]["3017"] == ["散熱"], "JSON")
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["股票代號", "概念股"])
    ws.append([2330, "AI晶片"])
    p4 = os.path.join(TMP, "g.xlsx")
    wb.save(p4)
    check(load_group_file(p4)[0]["2330"] == ["AI晶片"], "XLSX 數字代號")
    gm = GroupMap(STOCKS, p, "industry")
    check(gm.groups_of("2881") == ["金融保險"], "未涵蓋股票歸產業別")
    gm2 = GroupMap(STOCKS, p, "drop")
    check(gm2.groups_of("2881") == (), "drop 模式")
    check(industry_name("25") == "電腦及週邊" and industry_name("") == "其他", "產業代碼對照")
    # stocks.txt 格式（與 quant_system/monitoring/parsers.py 同規則）
    p5 = os.path.join(TMP, "stocks.txt")
    open(p5, "w", encoding="utf-8").write(
        "##################\n##    分類要 3 個 ###\nTOP_CLASS=010,100\nTOP_STOCK=2330\n"
        "###001:上市七大\n###010:IC設計/IP\n###100:被動元件\n##^TWII加權指數\n"
        "2330台積電:001,010\n#2303聯電:010\n#3661世芯-KY:010\n2382 廣達 : 001\n")
    d = load_groups(p5)
    check(d["format"] == "stocks.txt", "辨識 stocks.txt 格式")
    check(d["membership"]["2330"] == ["上市七大", "IC設計/IP"], "分類代碼轉名稱")
    check("2303" in d["membership"] and "3661" in d["membership"], "單個 # 的股票盤中仍納入")
    check(d["membership"]["2382"] == ["上市七大"], "代號名稱間有空白也能讀")
    check(d["top_groups"] == ["IC設計/IP", "被動元件"] and d["top_stocks"] == ["2330"], "TOP_CLASS / TOP_STOCK")
    check("2303" not in load_groups(p5, include_commented=False)["membership"], "可選擇排除 # 股票")
    sets = build_group_sets(STOCKS, p5)
    check([x.id for x in sets] == ["custom", "industry"], "兩組族群：我的族群 + 產業別")
    check(sets[0].gm.groups_of("2881") == () and sets[1].gm.groups_of("2881") == ["金融保險"], "我的族群不混入產業別")


def test_engine_ticks():
    gm = GroupMap(STOCKS, None)
    e = FlowEngine(STOCKS, gm, D.isoformat(), TMP, mode="test", start_time=T(8, 50))
    e.set_tick_codes(["2330", "2303", "2382"])
    e.on_tick("2330", T(8, 59, 50), 9e8, 900, 1, simtrade=True)          # 試撮：忽略
    e.on_tick("2330", T(9, 0, 0), 5e8, 500, 0, buy_tot=0, sell_tot=0)      # 開盤集合競價 tt=0
    e.on_tick("2330", T(9, 0, 30), 3e8, 300, 1, buy_tot=300, sell_tot=0)
    e.on_tick("2330", T(9, 1, 10), 1e8, 100, 2, buy_tot=300, sell_tot=100)
    e.on_tick("2303", T(9, 0, 0), 6e8, 600, 2, buy_tot=0, sell_tot=600)      # 開盤集合競價但標成內盤 → 不算
    e.on_tick("2382", T(9, 0, 1), 8e8, 800, 1, buy_tot=800, sell_tot=0)      # 開盤集合競價但標成外盤 → 不算
    e.on_tick("2303", T(9, 1, 20), 2e8, 200, 2, buy_tot=0, sell_tot=800)
    e.on_tick("2382", T(9, 1, 40), 4e8, 400, 1, buy_tot=1200, sell_tot=0)
    e.on_tick("2382", T(13, 26, 0), 7e8, 700, 1, buy_tot=1100, sell_tot=0)   # 收盤前試撮時段：忽略
    st = e.state()
    g = {x["name"]: x for x in st["sets"][0]["groups"]}
    check(abs(g["半導體"]["net"] - (3 - 1 - 2)) < 1e-9, "半導體淨額 = 3-1-2 = 0 億")
    check(abs(g["電腦及週邊"]["net"] - 4) < 1e-9, "電腦及週邊 +4 億")
    check(abs(st["market"]["net"] - 4) < 1e-9, "全市場 +4 億")
    check(abs(g["半導體"]["turnover"] - 17) < 1e-9, "成交額含集合競價與 tt=0（5+3+1+6+2=17 億）")
    check([f["t"] for f in e.frames] == ["09:00"], "收到 09:01 的成交時就收 09:00 格")
    e.tick_clock(T(9, 1, 59))
    check(len(e.frames) == 1, "沒有新成交時，等時鐘過寬限期才收格")
    e.tick_clock(T(9, 2, 5))
    check([f["t"] for f in e.frames] == ["09:00", "09:01"], "收 09:00、09:01 兩格")
    check(abs(e.frames[0]["g"]["industry"]["半導體"][0] - 3) < 1e-9, "09:00 格只含 09:00:xx 的成交")
    check("s" in e.frames[0] and "s" not in e.frames[1], "每 5 分鐘存個股明細")


def test_auction_ticks():
    gm = GroupMap(STOCKS, None)
    for flag in (True, False):
        e = FlowEngine(STOCKS, gm, D.isoformat(), TMP, mode="test", start_time=T(8, 50), auction_neutral=flag)
        e.set_tick_codes(["2330"])
        e.on_tick("2330", T(9, 0, 0, ), 50e8, 1, 2)      # 開盤撮合（shioaji 標內盤）
        e.on_tick("2330", T(9, 0, 1), 1e8, 1, 1)
        e.on_tick("2330", T(9, 0, 2), 2e8, 1, 2)
        e.on_tick("2330", T(13, 30, 0), 80e8, 1, 1)     # 收盤撮合（shioaji 標外盤）
        s = e.st["2330"]
        if flag:
            check(s.net == -1e8 and s.neutral == 130e8 and s.total == 133e8, "開盤、收盤集合競價不算主動買賣、只算成交額")
        else:
            check(s.net == 29e8, "auction_neutral = false 時照 tick_type 計入")
    check(is_auction(dt.time(9, 2, 30), True) and not is_auction(dt.time(9, 2, 30), False)
          and not is_auction(dt.time(9, 6), True) and is_auction(dt.time(13, 33), False), "集合競價判斷")
    e = FlowEngine(STOCKS, gm, D.isoformat(), TMP, mode="test", start_time=T(9, 40))
    e.set_tick_codes(["2330"])
    e.on_tick("2330", T(9, 40, 5), 1e8, 1, 1)
    check(e.st["2330"].buy == 1e8, "盤中才啟動，第一筆不會被當成開盤撮合")


def test_repair_midday():
    gm = GroupMap(STOCKS, None)
    e = FlowEngine(STOCKS, gm, D.isoformat(), TMP, mode="test", start_time=T(10, 30))
    e.set_tick_codes(["2330"])
    # 10:30 才啟動：第一筆 tick 的累計外盤 1000 張、內盤 600 張（含本筆外盤 10 張），均價 1000 元
    e.on_tick("2330", T(10, 30, 5), 10 * 1000 * 1000, 10, 1, total_amount=1.61e9,
              buy_tot=1000, sell_tot=600, avg_price=1000, close=1000)
    s = e.st["2330"]
    check(abs(s.est_buy - 990 * 1000 * 1000) < 1, "補回啟動前外盤 990 張 × 均價")
    check(abs(s.est_sell - 600 * 1000 * 1000) < 1, "補回啟動前內盤 600 張 × 均價")
    check(abs(s.net - (9.9e8 + 1e7 - 6e8)) < 1, "淨額 = 估算 + 本筆")
    # 正常連續 tick 不應重複補
    e.on_tick("2330", T(10, 30, 6), 5e6, 5, 2, buy_tot=1000, sell_tot=605, avg_price=1000, close=1000)
    check(abs(s.est_sell - 6e8) < 1, "連續 tick 不重複補值")
    # 斷線重連：漏掉 50 張外盤
    e.mark_reconnect()
    e.on_tick("2330", T(10, 40, 0), 1e6, 1, 1, buy_tot=1051, sell_tot=605, avg_price=1000, close=1010)
    check(abs(s.est_buy - (9.9e8 + 50 * 1000 * 1000)) < 1, "重連後補回漏掉的 50 張外盤")
    check(e.frames[0]["t"] == "10:30" and e.frames[-1]["t"] == "10:39", "中途啟動從當下開始收格，不偽造早盤")


def test_pct_and_sides():
    from feed_shioaji import feed_tuple
    stocks = {"2330": {"name": "台積電", "category": "24", "unit": 1000, "reference": 1000.0},
              "2303": {"name": "聯電", "category": "24", "unit": 1000, "reference": 0}}
    e = FlowEngine(stocks, GroupMap(stocks, None), D.isoformat(), TMP, mode="test", start_time=T(10, 0))
    e.set_tick_codes(["2330"])
    # 真實 tick tuple：(code, dt, amount, vol, tick_type, total, ask_side(賣盤), bid_side(買盤), avg, close, sim, odd, pct)
    feed_tuple(e, ("2330", T(10, 0, 1), 1.03e6, 1, 1, 9e9, 3000, 5001, 1010.0, 1030.0, False, False, 3.0))
    s = e.st["2330"]
    check(abs(s.est_buy - 5000 * 1010 * 1000) < 1 and abs(s.est_sell - 3000 * 1010 * 1000) < 1,
          "中途啟動補值：bid_side=買盤(外盤)、ask_side=賣盤(內盤)")
    check(s.pct == 3.0 and s.px == 1030.0, "漲跌幅用參考價計算")
    e.on_snapshots([{"code": "2303", "total_amount": 1e8, "close": 50, "change_rate": -2.5}], T(10, 1))
    check(e.st["2303"].pct == -2.5, "沒有參考價時用快照的漲跌幅")
    gd = e.group_detail("半導體")
    check([r["code"] for r in gd["stocks"]] == ["2330", "2303"] and gd["stocks"][0]["pct"] == 3.0, "成分股依漲跌幅排序、含漲跌幅")
    e.tick_clock(T(10, 1, 5))
    check(e.frames[0]["s"]["2330"][2] == 3.0, "每 5 分鐘的個股明細含漲跌幅（回放用）")
    g = {x["name"]: x for x in e.state("industry")["sets"][0]["groups"]}["半導體"]
    check(g["up"][0][:2] == ["2330", "台積電"] and g["up"][0][2] == 3.0 and g["dn"][0][0] == "2303",
          "首頁輪播：族群漲幅第 1 名 / 跌幅第 1 名")
    check(g["avg"] == 0.25, "族群平均漲跌幅（點點顏色用）：(3.0 + −2.5) / 2")
    e.on_index("tse", 43307.54, -1612.6, -3.59)
    st = e.state("industry")
    check(st["index"]["tse"] == [43307.54, -1612.6, -3.59], "加權指數進入即時狀態")
    e.tick_clock(T(10, 2, 5))
    check(e.frames[-1]["ix"]["tse"][2] == -3.59, "指數每分鐘存進錄製檔（回放用）")


def test_tail_snapshot():
    gm = GroupMap(STOCKS, None)
    e = FlowEngine(STOCKS, gm, D.isoformat(), TMP, mode="test", start_time=T(8, 55))
    e.set_tick_codes(["2330"])
    e.on_snapshots([{"code": "2881", "total_amount": 250e8, "close": 88, "date": "2026-10-19"}], T(8, 59))
    check(e.st["2881"].total == 0 and e.st["2881"].snap_total is None, "前一天的快照（開盤前）不算進今天的成交額")
    e.on_snapshots([{"code": "2881", "total_amount": 0, "close": 90, "tick_type": "None", "date": D.isoformat()}], T(8, 59))
    e.on_snapshots([{"code": "2881", "total_amount": 1e8, "close": 90, "tick_type": "Buy"}], T(9, 0, 50))  # 開盤撮合為主 → 不分買賣
    e.on_snapshots([{"code": "2881", "total_amount": 6e8, "close": 91, "tick_type": "Sell"}], T(9, 1, 50))   # 價漲 → 外盤
    e.on_snapshots([{"code": "2881", "total_amount": 8e8, "close": 90.5, "tick_type": "Buy"}], T(9, 2, 50))  # 價跌 → 內盤
    e.on_snapshots([{"code": "2881", "total_amount": 9e8, "close": 90.5, "tick_type": "Buy"}], T(9, 3, 50))  # 平盤 → 看最後一筆
    s = e.st["2881"]
    check(abs(s.net - (5e8 - 2e8 + 1e8)) < 1 and s.neutral == 1e8, "快照估算：開盤不分、漲算買、跌算賣、平看內外盤")
    # 開盤前啟動：今天第一次看到就已有成交 → 從 0 起算
    stk = {"9998": {"name": "x", "category": "24", "unit": 1000, "reference": 100.0},
           "9999": {"name": "y", "category": "24", "unit": 1000, "reference": 100.0}}
    e3 = FlowEngine(stk, GroupMap(stk, None), D.isoformat(), TMP, mode="test", start_time=T(8, 40))
    e3.on_snapshots([{"code": "9998", "total_amount": 0, "close": 100, "date": D.isoformat()}], T(8, 59, 50))
    e3.on_snapshots([{"code": "9998", "total_amount": 3e8, "close": 103, "date": D.isoformat()}], T(9, 1))
    a = e3.st["9998"]
    check(a.neutral == 3e8 and a.net == 0 and a.total == 3e8, "開盤第一份快照的量（集合競價為主）只算成交額")
    e3.on_snapshots([{"code": "9999", "total_amount": 2e8, "close": 97, "date": D.isoformat()}], T(10, 15))
    check(e3.st["9999"].est_sell == 2e8 and e3.st["9999"].tail_base == 0, "盤中才第一次成交：從 0 起算、較昨收下跌→內盤")
    e3.on_snapshots([{"code": "9998", "total_amount": 5e8, "close": 104, "date": D.isoformat()}], T(13, 24, 40))
    e3.on_snapshots([{"code": "9998", "total_amount": 9e8, "close": 106, "date": D.isoformat()}], T(13, 30, 40))
    check(a.est_buy == 2e8 and a.neutral == 7e8, "收盤撮合那段快照增量只算成交額")
    st = e.state()
    check(st["coverage"]["tail_n"] == 3 and st["coverage"]["est_ratio"] == 1.0, "覆蓋率統計")
    # 盤中才啟動：第一次快照的累計量不判方向
    e2 = FlowEngine(STOCKS, gm, D.isoformat(), TMP, mode="test", start_time=T(11, 0))
    e2.on_snapshots([{"code": "2881", "total_amount": 9e8, "close": 90}], T(11, 0))
    check(e2.st["2881"].net == 0 and e2.st["2881"].total == 9e8, "盤中啟動的尾端股票不猜早盤方向")


def test_multi_sets():
    p = os.path.join(TMP, "stocks2.txt")
    open(p, "w", encoding="utf-8").write("TOP_STOCK=2330\n###010:AI晶片\n###020:伺服器\n2330台積電:010\n#2303聯電:010\n2382廣達:020,010\n")
    sets = build_group_sets(STOCKS, p)
    e = FlowEngine(STOCKS, sets, D.isoformat(), TMP, mode="test", start_time=T(8, 50), auction_neutral=False)
    e.set_tick_codes(list(STOCKS))
    e.on_tick("2330", T(9, 0, 5), 5e8, 1, 1)
    e.on_tick("2382", T(9, 0, 6), 2e8, 1, 2)
    e.on_tick("2881", T(9, 0, 7), 1e8, 1, 1)
    e.on_tick("2330", T(9, 1, 0), 1e8, 1, 1)
    st = e.state()
    cu = {x["name"]: x["net"] for x in st["sets"][0]["groups"]}
    ind = {x["name"]: x["net"] for x in st["sets"][1]["groups"]}
    check(abs(cu["AI晶片"] - 4) < 1e-9 and abs(cu["伺服器"] + 2) < 1e-9, "我的族群：一檔屬多族群各自全額計入")
    check(abs(ind["半導體"] - 6) < 1e-9 and abs(ind["金融保險"] - 1) < 1e-9, "產業別同時計算")
    check(abs(st["market"]["net"] - 5) < 1e-9, "全市場只算一次（5+1-2+1=5）")
    check(set(e.frames[0]["g"]) == {"custom", "industry"}, "每格同時存兩組族群")
    gd = e.group_detail("AI晶片", "custom")
    check([x["code"] for x in gd["stocks"]] == ["2330", "2382"] and gd["stocks"][0]["top"], "成分股明細 + TOP_STOCK 標記")
    pl = e.day_payload()
    check([x["id"] for x in pl["sets"]] == ["custom", "industry"] and pl["sets"][0]["top_stocks"] == ["2330"], "存檔含兩組族群設定")


class FakePowercfg(object):
    """模擬繁中 Windows 的 powercfg 輸出。"""
    BAL, HIGH, SAVER = "381b4222-f694-41f0-9685-ff5bb260df2e", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", "a1841308-3541-4fab-bc81-f71556f20b4a"

    def __init__(self, hide_saver=False, active=None):
        self.schemes = {self.BAL: "平衡", self.HIGH: "高效能"}
        if not hide_saver:
            self.schemes[self.SAVER] = "省電"
        self.active = active or self.BAL
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "/getactivescheme":
            return 0, "電源配置 GUID: %s  (%s)" % (self.active, self.schemes[self.active])
        if args[0] == "/list":
            rows = ["電源配置 GUID: %s  (%s)%s" % (g, n, " *" if g == self.active else "") for g, n in self.schemes.items()]
            return 0, "現有的電源配置 (* 表示使用中)\n-----------------------------------\n" + "\n".join(rows)
        if args[0] == "/setactive":
            if args[1] in self.schemes:
                self.active = args[1]
                return 0, ""
            return 1, "指定的電源配置不存在"
        if args[0] == "-duplicatescheme":
            g = "11111111-2222-3333-4444-555555555555"
            self.schemes[g] = "省電"
            return 0, "電源配置 GUID: %s  (省電)" % g
        return 1, ""


def test_power():
    from power import PowerGuard, PowerPlan, ShutdownTimer, parse_schemes
    check(parse_schemes("Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced) *") ==
          [("381b4222-f694-41f0-9685-ff5bb260df2e", "Balanced", True)], "英文 powercfg 輸出")
    check(parse_schemes("電源配置 GUID: A1841308-3541-4FAB-BC81-F71556F20B4A  (省電)")[0][:2] ==
          ("a1841308-3541-4fab-bc81-f71556f20b4a", "省電"), "繁中 powercfg 輸出")
    sf = os.path.join(TMP, "power_restore.json")
    logs = []
    pc = FakePowercfg()
    g = PowerGuard(sf, plan=PowerPlan(pc), keep_awake=False, log=logs.append, enabled=True)
    g.start()
    check(pc.active == pc.SAVER and os.path.exists(sf), "啟動：記下原計畫並切到省電")
    g.restore()
    check(pc.active == pc.BAL and not os.path.exists(sf), "結束：還原成原本的「平衡」並刪掉紀錄")
    n = len(pc.calls)
    g.restore()
    check(len(pc.calls) == n, "重複還原不會再動作（Ctrl+C 後又走 finally）")
    pc2 = FakePowercfg(hide_saver=True, active=FakePowercfg.HIGH)
    g2 = PowerGuard(sf, plan=PowerPlan(pc2), keep_awake=False, log=logs.append, enabled=True)
    g2.start()
    check(pc2.active == "11111111-2222-3333-4444-555555555555", "省電計畫被隱藏時自動補建")
    g2.restore()
    check(pc2.active == pc2.HIGH, "還原成原本的「高效能」")
    # 上次被強制結束：電腦還停在省電，紀錄檔還在
    pc3 = FakePowercfg(active=FakePowercfg.SAVER)
    json.dump({"guid": FakePowercfg.BAL, "name": "平衡"}, open(sf, "w", encoding="utf-8"))
    g3 = PowerGuard(sf, plan=PowerPlan(pc3), keep_awake=False, log=logs.append, enabled=True)
    g3.start()
    g3.restore()
    check(pc3.active == pc3.BAL, "上次沒還原：以紀錄檔的原計畫為準")
    off = PowerGuard(sf, plan=PowerPlan(FakePowercfg()), keep_awake=False, log=logs.append, enabled=False)
    off.start()
    off.restore()
    check(not os.path.exists(sf), "非 Windows 不動作")
    # 關機排程
    order = []
    tw = [dt.datetime(2026, 11, 3, 13, 50)]
    t = ShutdownTimer(dt.time(13, 40), 60, lambda: order.append("before"), lambda: tw[0], log=logs.append,
                      runner=lambda a: order.append(a))
    check(t.start() is False and order == [], "13:40 後才啟動：當天不關機")
    tw[0] = dt.datetime(2026, 11, 3, 13, 39, 59)
    t = ShutdownTimer(dt.time(13, 40), 60, lambda: order.append("before"), lambda: tw[0], log=logs.append,
                      runner=lambda a: order.append(a))
    t.tick = 0.05
    check(t.start() is True, "13:40 前啟動：排程關機")
    time.sleep(0.2)
    check(order == [], "時間未到不動作")
    tw[0] = dt.datetime(2026, 11, 3, 13, 40, 1)
    for _ in range(50):
        if len(order) >= 2:
            break
        time.sleep(0.1)
    check(order[:1] == ["before"] and order[1][:4] == ["shutdown", "/s", "/t", "60"], "到點：先清理（還原電源）再下關機指令")


def test_housekeep():
    import gzip as _gz
    from housekeep import housekeep, read_day
    from server import list_days
    d = os.path.join(TMP, "hk")
    os.makedirs(d)
    days = ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
    for x in days:
        json.dump({"date": x, "frames": []}, open(os.path.join(d, x + ".live.json"), "w"))
        open(os.path.join(d, x + ".state.json"), "w").write("{}")
    json.dump({"date": "2026-10-30"}, open(os.path.join(d, "2026-10-30.sim.json"), "w"))
    json.dump({"date": "2026-11-01"}, open(os.path.join(d, "2026-11-01.sim.json"), "w"))       # 週日跑的模擬
    open(os.path.join(d, "notes.txt"), "w").write("keep me")
    housekeep(d, "2026-11-03", keep_days=4, log=lambda m: None)
    left = sorted(os.listdir(d))
    check(left == ["2026-10-29.live.json.gz", "2026-10-30.live.json.gz", "2026-10-30.sim.json.gz",
                   "2026-11-02.live.json.gz", "2026-11-03.live.json", "2026-11-03.state.json", "notes.txt"],
          "保留最近 4 個交易日、舊的壓縮、過期與舊 state 刪除、今天不動：%s" % left)
    check(json.loads(read_day(d, "2026-10-29", "live"))["date"] == "2026-10-29", "回放讀得到壓縮檔")
    check([x["date"] for x in list_days(d)] == ["2026-11-03", "2026-11-02", "2026-10-30", "2026-10-29"], "日期選單含壓縮檔")
    housekeep(d, "2026-11-03", keep_days=4, log=lambda m: None)
    check(sorted(os.listdir(d)) == left, "重複執行不會出錯")


def test_gross_prev():
    from housekeep import prev_series
    gm = GroupMap(STOCKS, None)
    e = FlowEngine(STOCKS, gm, D.isoformat(), TMP, mode="test", start_time=T(8, 50), auction_neutral=False)
    e.set_tick_codes(list(STOCKS))
    e.on_tick("2330", T(9, 0, 5), 5e8, 1, 1)
    e.on_tick("2303", T(9, 0, 6), 2e8, 1, 2)
    e.on_tick("2382", T(9, 0, 7), 1e8, 1, 0)        # 無法判定：算成交額、不算總進出
    e.tick_clock(T(9, 1, 5))
    check(e.state()["market"]["gross"] == 7.0 and e.frames[0]["m"] == [3.0, 8.0, 7.0], "總進出 = 外盤 + 內盤（不含無法判定）")
    d = os.path.join(TMP, "prev")
    os.makedirs(d)
    for day, g in (("2026-10-29", 100), ("2026-10-30", 200)):
        json.dump({"frames": [{"t": "09:00", "m": [1, g + 5, g]}, {"t": "09:01", "m": [1, g + 15, g + 10]}]},
                  open(os.path.join(d, day + ".live.json"), "w"))
    json.dump({"frames": [{"t": "09:00", "m": [0, 999]}]}, open(os.path.join(d, "2026-10-31.sim.json"), "w"))
    p = prev_series(d, "2026-11-02", ("live", "full"))
    check(p["date"] == "2026-10-30" and p["pts"][1] == [541, 210, 215], "前一交易日：跳過週末、不拿模擬資料")
    check(prev_series(d, "2026-10-29", ("live",)) is None, "沒有更早的錄製回傳 None")
    json.dump({"frames": [{"t": "09:00", "m": [0, 900]}, {"t": "09:01", "m": [0, 400]}, {"t": "09:02", "m": [0, 500]}]},
              open(os.path.join(d, "2026-11-02.live.json"), "w"))
    p = prev_series(d, "2026-11-03", ("live",))
    check([x[2] for x in p["pts"]] == [400, 400, 500], "前一日累計值開盤虛高會被濾掉")


def test_bins_equal_engine():
    """同一串 tick，盤中引擎與盤後建檔結果要一致。"""
    import random
    rnd = random.Random(1)
    gm = GroupMap(STOCKS, None)
    e = FlowEngine(STOCKS, gm, D.isoformat(), TMP, mode="test", start_time=T(8, 50))
    e.set_tick_codes(list(STOCKS))
    bins = {}
    t = T(9, 0)
    while t < T(13, 25):
        code = rnd.choice(list(STOCKS))
        amt = rnd.randint(1, 50) * 1e6
        tt = rnd.choice([0, 1, 2])
        e.on_tick(code, t, amt, 1, tt)
        bin_add(bins.setdefault(code, {}), t.hour * 60 + t.minute, amt, 0 if is_auction(t.time(), code not in bins or not bins[code]) else tt)
        t += dt.timedelta(seconds=rnd.randint(1, 20))
    e.on_tick("2330", T(13, 33, 1), 3e6, 1, 1)   # 延後收盤的成交
    bin_add(bins["2330"], 13 * 60 + 33, 3e6, 0)
    e.tick_clock(T(13, 34))
    check(len(e.frames) == 270, "13:30 格等延後收盤")
    e.tick_clock(T(13, 35, 1))
    frames, _ = build_frames_from_bins(bins, STOCKS, gm)
    check(len(frames) == len(e.frames) == 271, "09:00~13:30 共 271 格")
    worst = max(abs(a["g"]["industry"][g][0] - b["g"]["industry"].get(g, [0])[0]) for a, b in zip(e.frames, frames) for g in a["g"]["industry"])
    check(worst < 1e-3, "盤中引擎 vs 盤後建檔 最大誤差 %.6f 億" % worst)
    check(not in_session(dt.time(13, 27)) and in_session(dt.time(13, 33, 5)) and not in_session(dt.time(8, 59)), "盤中時段判斷")


def _fake_env(new):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(HERE, "fake") + os.pathsep + ROOT
    env["FAKE_SJ_NEW"] = "1" if new else "0"
    env["FAKE_SJ_LOG"] = os.path.join(TMP, "sjlog_%d.txt" % new)
    return env


FEED_SCRIPT = r"""
import configparser, datetime as dt, sys, time, json
sys.path.insert(0, %(root)r)
from feed_shioaji import LiveFeed, fetch_day_bins, list_stocks, login, logout
from engine import FlowEngine, build_frames_from_bins
from groups import GroupMap
if __name__ == "__main__":
    cfg = configparser.ConfigParser()
    cfg.read_dict({"shioaji": {"api_key": "k", "secret_key": "s", "connections": "2", "per_connection": "3"},
                   "estimate": {"snapshot_interval": "0"}})
    cfg_path = %(cfgp)r
    with open(cfg_path, "w") as f: cfg.write(f)
    feed = LiveFeed(cfg, cfg_path)
    stocks = feed.prepare()
    feed.priority = ["2881", "3231"]      # 成交額排最後的兩檔，優先訂閱後應該在逐筆名單內
    gm = GroupMap(stocks, None)
    eng = FlowEngine(stocks, gm, dt.date.today().isoformat(), %(tmp)r, mode="test", start_time=dt.datetime.now().replace(hour=8, minute=0))
    feed.start(eng)
    time.sleep(6)
    st = eng.state()
    seen = sorted(c for c, s in eng.st.items() if s.total > 0)
    feed.stop()
    api = login(cfg, quiet=True)
    bins = fetch_day_bins(api, stocks, ["2330"], "2026-10-20")
    logout(api)
    fr, _ = build_frames_from_bins(bins, stocks, gm)
    print(json.dumps({"codes": sorted(stocks), "ranked": feed.ranked[:6], "tick_codes": sorted(eng.tick_codes),
                      "seen": seen, "ticks": eng.tick_count, "bins": {str(k): v for k, v in bins["2330"].items()},
                      "last": fr[-1]["g"]["industry"]}))
"""


def test_feed_fake(new):
    script = os.path.join(TMP, "feed_%d.py" % new)
    open(script, "w").write(FEED_SCRIPT % {"root": ROOT, "tmp": TMP, "cfgp": os.path.join(TMP, "cfg_%d.ini" % new)})
    out = subprocess.run([sys.executable, script], env=_fake_env(new), capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        print(out.stdout, out.stderr)
    check(out.returncode == 0, "假 shioaji（%s）跑完" % ("新版" if new else "舊版"))
    r = json.loads(out.stdout.strip().splitlines()[-1])
    check("0050" not in r["codes"] and "2881A" not in r["codes"] and "030001" not in r["codes"], "排除 ETF/特別股/權證")
    check(r["ranked"][0] == "2454", "依成交額排序（快照）")
    check(len(r["tick_codes"]) == 6, "2 個連線 × 3 檔 = 6 檔逐筆")
    check({"2881", "3231"} <= set(r["tick_codes"]) and "2603" not in r["tick_codes"], "族群檔股票優先訂閱逐筆")
    check(set(r["seen"]) >= set(r["tick_codes"]) and r["ticks"] > 30, "兩個連線的 tick 都有進引擎（%d 筆）" % r["ticks"])
    b = r["bins"]
    check(set(b) == {"540", "541", "615", "810"}, "盤後逐筆：13:27 試撮被排除")
    check(abs(b["541"][0] - 101 * 5 * 1000) < 1 and abs(b["615"][1] - 99 * 3 * 1000) < 1, "盤後逐筆金額 = 價 × 張 × 1000")
    log = open(os.path.join(TMP, "sjlog_%d.txt" % new)).read()
    check(log.count("login") == log.count("logout"), "每個連線都有登出（%d 次）" % log.count("logout"))
    check(len({ln.split()[0] for ln in log.splitlines() if " sub " in ln}) == 2, "訂閱分散在 2 個行程")


if __name__ == "__main__":
    try:
        test_groups()
        test_engine_ticks()
        test_auction_ticks()
        test_repair_midday()
        test_tail_snapshot()
        test_pct_and_sides()
        test_multi_sets()
        test_power()
        test_housekeep()
        test_gross_prev()
        test_bins_equal_engine()
        test_feed_fake(False)
        test_feed_fake(True)
        print("全部通過：%d 項檢查" % ok)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
