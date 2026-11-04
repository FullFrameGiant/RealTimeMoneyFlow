# -*- coding: utf-8 -*-
"""即時資金流 — 主程式

  python server.py                  # 實盤：登入 shioaji，盤中即時計算並開網頁
  python server.py --sim            # 模擬：不用帳號，假資料跑一天（預設 20 倍速）
  python server.py --view           # 只看存檔回放，不連線
然後用瀏覽器開 http://127.0.0.1:8765
"""
import argparse
import base64
import configparser
import gzip
import hmac
import datetime as _dt
import glob
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from engine import FlowEngine, tw_now, tw_today  # noqa: E402
from groups import build_group_sets, load_groups  # noqa: E402
from housekeep import housekeep, prev_series, read_day, scan  # noqa: E402
from power import PowerGuard, ShutdownTimer, install_console_handler, parse_hhmm  # noqa: E402

STATE = {"engine": None, "data_dir": os.path.join(HERE, "data"), "mode": "view", "auth": None, "client": {}}


def load_cfg(path):
    """讀 config.ini；記事本存成 UTF-8（含/不含 BOM）或 ANSI(Big5) 都可以。"""
    cfg = configparser.ConfigParser()
    if os.path.exists(path):
        raw = open(path, "rb").read()
        for enc in ("utf-8-sig", "cp950"):
            try:
                cfg.read_string(raw.decode(enc), source=path)
                break
            except UnicodeDecodeError:
                continue
    return cfg


def list_days(data_dir):
    days = {}
    for d, k, _, _ in scan(data_dir):
        if k != "state" and k not in days.get(d, []):
            days.setdefault(d, []).append(k)
    order = {"full": 0, "live": 1, "sim": 2}
    return [{"date": d, "kinds": sorted(k, key=order.get)} for d, k in sorted(days.items(), reverse=True)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _authorized(self):
        """config.ini [server] password 有設定時，用 HTTP Basic 驗證（帳號 username，預設 me）。"""
        need = STATE["auth"]
        if not need:
            return True
        got = self.headers.get("Authorization", "")
        if got.startswith("Basic "):
            try:
                if hmac.compare_digest(base64.b64decode(got[6:]).decode("utf-8"), need):
                    return True
            except (ValueError, UnicodeDecodeError):
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="moneyflow", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        gz = len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "")
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorized():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        eng = STATE["engine"]
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                    head = (b'<!doctype html>\n<html lang="zh-Hant"><meta charset="utf-8">'
                            b'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n')
                    return self._send(200, head + f.read(), "text/html; charset=utf-8")
            if u.path == "/api/config":
                d = {"mode": STATE["mode"], "live": eng is not None, "date": eng.date if eng else None}
                d.update(STATE["client"])
                return self._send(200, d)
            if u.path == "/api/state":
                return self._send(200, eng.state(q.get("set"))) if eng else self._send(404, {"error": "no live engine"})
            if u.path == "/api/series":
                if not eng:
                    return self._send(404, {"error": "no live engine"})
                return self._send(200, eng.series(q.get("since"), q.get("full") == "1"))
            if u.path == "/api/group":
                return self._send(200, eng.group_detail(q.get("name", ""), q.get("set"))) if eng else self._send(404, {})
            if u.path == "/api/live_day":
                return self._send(200, eng.day_payload()) if eng else self._send(404, {})
            if u.path == "/api/prev":       # 前一個有錄製的交易日（總進出比較用）
                d, kind = q.get("date", ""), q.get("kind", "")
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    return self._send(400, {"error": "bad date"})
                kinds = [kind] + [k for k in ("live", "full", "sim") if k != kind] if kind in ("live", "full", "sim") \
                    else ["live", "full", "sim"]
                if kind != "sim":
                    kinds = [k for k in kinds if k != "sim"]          # 實盤不跟模擬資料比
                return self._send(200, prev_series(STATE["data_dir"], d, tuple(kinds)) or {})
            if u.path == "/api/dates":
                return self._send(200, list_days(STATE["data_dir"]))
            if u.path == "/api/day":
                d, kind = q.get("date", ""), q.get("kind", "")
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    return self._send(400, {"error": "bad date"})
                kinds = [kind] if kind in ("full", "live", "sim") else ["full", "live", "sim"]
                for k in kinds:
                    raw = read_day(STATE["data_dir"], d, k)      # .json 或壓縮過的 .json.gz
                    if raw is not None:
                        return self._send(200, raw)
                return self._send(404, {"error": "not found"})
            return self._send(404, {"error": "not found"})
        except BrokenPipeError:
            pass


def group_options(cfg):
    """從 config.ini 讀族群相關設定。"""
    gf = cfg.get("universe", "group_file", fallback="").strip().strip('"') or None
    if gf and not os.path.isabs(gf):
        gf = os.path.join(HERE, gf)
    if gf and not os.path.exists(gf):
        raise SystemExit("找不到族群檔：%s（請檢查 config.ini 的 group_file）" % gf)
    return dict(group_file=gf,
                unassigned=cfg.get("universe", "unassigned", fallback="drop"),
                show_industry=cfg.getboolean("universe", "show_industry", fallback=True),
                include_commented=cfg.getboolean("universe", "include_commented", fallback=True),
                custom_label=cfg.get("universe", "custom_label", fallback="我的族群"))


def report_sets(sets):
    for gs in sets:
        gm = gs.gm
        extra = ""
        if gm.format != "industry":
            extra = "，涵蓋 %d 檔" % len(gm.custom_codes)
            if gm.top_groups:
                extra += "，重點分類：%s" % "、".join(gm.top_groups)
        print("[groups] %s（%s）：%d 個族群%s" % (gs.label, gm.source, len(gm.all_groups()), extra), flush=True)
        if gm.unknown_codes:
            print("[groups]   族群檔有 %d 個代號不在普通股清單（ETF/下市/打錯？）：%s" % (
                len(gm.unknown_codes), ",".join(gm.unknown_codes[:20])), flush=True)


def main():
    ap = argparse.ArgumentParser(description="即時資金流")
    ap.add_argument("--config", default=os.path.join(HERE, "config.ini"))
    ap.add_argument("--sim", action="store_true", help="模擬模式（假資料）")
    ap.add_argument("--speed", type=float, default=20, help="模擬倍速")
    ap.add_argument("--start", default="09:00", help="模擬從幾點開始")
    ap.add_argument("--view", action="store_true", help="只開網頁看存檔")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    host = cfg.get("server", "host", fallback="127.0.0.1")
    poll = cfg.get("server", "poll_seconds", fallback="").strip()
    STATE["client"] = {"poll_seconds": float(poll) if poll else None,
                       "anim_fps": cfg.getint("server", "anim_fps", fallback=30)}
    unq = lambda v: (v or "").strip().strip('"').strip("'").strip()   # 有沒有加引號都可以
    pw = unq(cfg.get("server", "password", fallback=""))
    if pw:
        STATE["auth"] = "%s:%s" % (unq(cfg.get("server", "username", fallback="me")) or "me", pw)
    elif host not in ("127.0.0.1", "localhost"):
        print("[server] 注意：host=%s 但沒有設定 password，同網路的人都能開這個網頁" % host, flush=True)
    port = args.port or cfg.getint("server", "port", fallback=8765)
    gopt = group_options(cfg)
    housekeep(STATE["data_dir"], tw_today().isoformat(),
              keep_days=cfg.getint("data", "keep_days", fallback=4),
              compress=cfg.getboolean("data", "compress", fallback=True))
    feed = None
    srv = None

    # ---- 電源計畫：先記下目前的計畫、切到省電；任何方式結束都會先還原 ----
    power = None
    if not args.view:
        power = PowerGuard(os.path.join(STATE["data_dir"], "power_restore.json"),
                           use_saver=cfg.getboolean("power", "power_saver", fallback=True),
                           keep_awake=cfg.getboolean("power", "keep_awake", fallback=True))
        power.start()
    closing = {"done": False, "lock": threading.Lock()}

    def cleanup(reason):
        with closing["lock"]:
            if closing["done"]:
                return
            closing["done"] = True
        print("\n[exit] %s：先還原電源計畫，再結束程式…" % reason, flush=True)
        if power is not None:
            power.restore()                     # 1. 還原電源計畫（按 X 時 Windows 只給約 5 秒，所以最先做）
        if STATE["engine"] is not None:
            STATE["engine"].save()              # 2. 存檔（很快）
        if feed is not None:
            feed.stop()                         # 3. 登出 shioaji、釋放連線（最慢，放最後）

    install_console_handler(cleanup)            # Ctrl+C、按視窗 X、登出

    try:
        if args.sim:
            from feed_sim import SimFeed
            STATE["mode"] = "sim"
            extra = {}
            if gopt["group_file"]:   # 模擬也用你的族群檔：檔案裡的代號會變成模擬股票
                d = load_groups(gopt["group_file"], gopt["include_commented"])
                extra = {c: d["names"].get(c, "") for c in d["membership"]}
            feed = SimFeed(speed=args.speed, start=args.start, extra_codes=extra)
            stocks = feed.prepare()
            sets = build_group_sets(stocks, **gopt)
            report_sets(sets)
            h, m = args.start.split(":")
            start_dt = _dt.datetime.combine(tw_today(), _dt.time(int(h), int(m)))
            eng = FlowEngine(stocks, sets, tw_today().isoformat(), STATE["data_dir"], mode="sim",
                             start_time=start_dt,
                             auction_neutral=cfg.getboolean("estimate", "auction_neutral", fallback=True))
            feed.start(eng)
            STATE["engine"] = eng
        elif not args.view:
            from feed_shioaji import LiveFeed
            STATE["mode"] = "live"
            feed = LiveFeed(cfg, args.config)
            stocks = feed.prepare()
            sets = build_group_sets(stocks, **gopt)
            report_sets(sets)
            if cfg.get("shioaji", "subscribe_priority", fallback="groups") == "groups":
                feed.priority = [c for gs in sets for c in gs.gm.custom_codes]
            eng = FlowEngine(stocks, sets, tw_today().isoformat(), STATE["data_dir"], mode="live",
                             side_swap=cfg.getboolean("estimate", "side_swap", fallback=False),
                             repair=cfg.getboolean("estimate", "repair", fallback=True),
                             auction_neutral=cfg.getboolean("estimate", "auction_neutral", fallback=True))
            eng.try_restore()
            feed.start(eng)
            STATE["engine"] = eng

        srv = ThreadingHTTPServer((host, port), Handler)
        url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
        print("[server] 開啟 %s  （Ctrl+C 結束）" % url, flush=True)
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()

        # ---- 當天指定時間自動關機（程式還在跑才會關）----
        at = parse_hhmm(cfg.get("power", "shutdown_at", fallback="13:40"))
        if at and not args.view:
            def before_shutdown():
                cleanup("%s 自動關機" % at.strftime("%H:%M"))
                threading.Timer(2.0, srv.shutdown).start()
            ShutdownTimer(at, cfg.getint("power", "shutdown_countdown", fallback=60),
                          before_shutdown, tw_now).start()

        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup("程式結束")
        if srv is not None:
            srv.server_close()


if __name__ == "__main__":
    main()
