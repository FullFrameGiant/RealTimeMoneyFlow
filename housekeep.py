# -*- coding: utf-8 -*-
"""data/ 資料夾整理：舊錄製壓縮成 .json.gz、刪掉過期的日子。程式每次啟動時自動執行。

  keep_days = 4  → 保留「最近 4 個有錄製的日子」（以交易日算，週末不佔名額），更早的刪除
  compress = true → 今天以前的錄製壓縮（約剩 1/3 大小），回放照樣能讀
  前幾天的 .state.json（只在當天重開程式接續用）一律刪除
"""
import glob
import gzip
import os
import re

_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(live|full|sim|state)\.json(\.gz)?$")


def scan(data_dir):
    """回傳 [(date, kind, path, is_gz)]"""
    out = []
    for p in glob.glob(os.path.join(data_dir, "*.json*")):
        m = _NAME.match(os.path.basename(p))
        if m:
            out.append((m.group(1), m.group(2), p, bool(m.group(3))))
    return out


def housekeep(data_dir, today, keep_days=4, compress=True, log=print):
    files = scan(data_dir)
    # 只用實盤錄製（live / full）的日子算保留天數；模擬檔不佔名額
    dates = sorted({d for d, k, _, _ in files if k in ("live", "full")} | {today}, reverse=True)
    keep = set(dates[:keep_days]) if keep_days and keep_days > 0 else {d for d, _, _, _ in files} | {today}
    keep.add(today)
    removed = compressed = 0
    saved = 0
    for d, k, p, gz in files:
        try:
            if d not in keep or (k == "state" and d != today):
                os.remove(p)
                removed += 1
            elif compress and not gz and d < today:
                with open(p, "rb") as f:
                    raw = f.read()
                tmp = p + ".gz.tmp"
                with gzip.open(tmp, "wb", compresslevel=6) as f:
                    f.write(raw)
                os.replace(tmp, p + ".gz")
                os.remove(p)
                saved += len(raw) - os.path.getsize(p + ".gz")
                compressed += 1
        except OSError as e:
            log("[data] 整理 %s 失敗：%s" % (os.path.basename(p), e))
    if removed or compressed:
        log("[data] 保留最近 %d 個交易日；壓縮 %d 個檔（省 %.1f MB）、刪除 %d 個過期檔" % (
            keep_days, compressed, saved / 1e6, removed))


def read_day(data_dir, date, kind):
    """讀某天的錄製（.json 或 .json.gz），回傳 bytes；找不到回傳 None。"""
    p = os.path.join(data_dir, "%s.%s.json" % (date, kind))
    if os.path.exists(p):
        with open(p, "rb") as f:
            return f.read()
    if os.path.exists(p + ".gz"):
        with gzip.open(p + ".gz", "rb") as f:
            return f.read()
    return None


_prev_cache = {}


def prev_series(data_dir, date, kinds=("live", "full", "sim")):
    """找 date 之前最近一個有錄製的交易日，回傳每分鐘的 [分鐘, 總進出(億, 舊檔為 None), 成交額(億)]。
    用來算「總進出較前一交易日同時間」。找不到回傳 None。結果會快取。"""
    have = {}
    for d, k, _, _ in scan(data_dir):
        if d < date and k in kinds:
            have.setdefault(d, set()).add(k)
    for d in sorted(have, reverse=True):
        for k in kinds:
            if k not in have[d]:
                continue
            key = (d, k)
            if key not in _prev_cache:
                import json
                raw = read_day(data_dir, d, k)
                try:
                    frames = json.loads(raw.decode("utf-8")).get("frames", []) if raw else []
                except ValueError:
                    frames = []
                pts = []
                for f in frames:
                    m = f.get("m") or []
                    h, mi = f["t"].split(":")
                    pts.append([int(h) * 60 + int(mi), m[2] if len(m) > 2 else None, m[1] if len(m) > 1 else None])
                # 累計值只會增加：由後往前取最小值，濾掉早期錄製開盤時混入前一天成交額的虛高
                for col in (1, 2):
                    lo = None
                    for p in reversed(pts):
                        if p[col] is None:
                            continue
                        if lo is not None and p[col] > lo:
                            p[col] = lo
                        lo = p[col]
                _prev_cache[key] = {"date": d, "kind": k, "pts": pts}
            if _prev_cache[key]["pts"]:
                return _prev_cache[key]
    return None
