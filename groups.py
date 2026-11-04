# -*- coding: utf-8 -*-
"""族群分類載入。

支援兩種來源：
1. 使用者自訂族群檔（CSV / XLSX / JSON），一檔股票可以屬於多個族群。
2. 沒給檔案時，用 shioaji 合約裡的 category（證交所/櫃買產業代碼）對應成產業名稱。

族群檔格式（自動判斷）：
  長表：每列一組 (代號, 族群)，欄名可用 代號/代碼/股票代號/code/stock_id 與 族群/分類/group/sector。
        族群欄位可放多個，用 、 , ; / | 分隔。可選欄位「大類」(電子/傳產/金融) 決定圓點顏色。
  寬表：第一列是族群名稱，每欄底下列出成員代號（可寫成 "2330 台積電"）。
  JSON：{"族群名": ["2330", "2303", ...], ...}
"""
import csv
import io
import json
import os
import re

# 證交所 / 櫃買中心 產業代碼 → 簡稱（影片用的就是這些簡稱）
INDUSTRY = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維", "05": "電機機械",
    "06": "電器電纜", "07": "化學生技醫療", "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵",
    "11": "橡膠", "12": "汽車", "13": "電子", "14": "建材營造", "15": "航運",
    "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學", "22": "生技醫療", "23": "油電燃氣", "24": "半導體", "25": "電腦及週邊",
    "26": "光電", "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活", "80": "管理股票",
}

ELECTRONIC_CODES = {"13", "24", "25", "26", "27", "28", "29", "30", "31", "34", "36"}
FINANCE_CODES = {"17"}

# 圓點顏色：電子=黃綠、傳產=紫、金融=藍（照影片）
CLASS_ELEC, CLASS_TRAD, CLASS_FIN = "電子", "傳產", "金融"


def industry_class(category_code):
    c = str(category_code or "").zfill(2)
    if c in ELECTRONIC_CODES:
        return CLASS_ELEC
    if c in FINANCE_CODES:
        return CLASS_FIN
    return CLASS_TRAD


def industry_name(category_code):
    c = str(category_code or "").strip()
    if not c:
        return "其他"
    return INDUSTRY.get(c.zfill(2), "其他")


_CODE_COLS = ["代號", "代碼", "股票代號", "股票代碼", "證券代號", "證券代碼", "code", "stock_id", "symbol", "ticker", "id"]
_GROUP_COLS = ["族群", "族群名稱", "分類", "類股", "概念股", "題材", "group", "groups", "sector", "theme", "industry", "產業"]
_CLASS_COLS = ["大類", "類別", "class", "color"]
_SPLIT = re.compile(r"[、,，;；/|｜\n]+")
_CODE_RE = re.compile(r"(?<![0-9A-Za-z])(\d{4,6}[A-Z]?)(?![0-9A-Za-z])")


def _norm(s):
    return str(s or "").strip().lower().replace(" ", "")


def _find_col(header, candidates):
    h = [_norm(x) for x in header]
    for cand in candidates:
        c = _norm(cand)
        if c in h:
            return h.index(c)
    for cand in candidates:  # 部分符合，例如「股票代號(上市)」
        c = _norm(cand)
        for i, x in enumerate(h):
            if c and c in x:
                return i
    return None


def _read_rows(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        try:
            import openpyxl
        except ImportError:
            raise SystemExit("讀 Excel 需要 openpyxl：pip install openpyxl（或把族群檔另存成 CSV）")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [["" if v is None else str(v).strip() for v in r] for r in ws.iter_rows(values_only=True)]
        wb.close()
        return rows
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp950", "big5", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit("族群檔編碼無法辨識，請存成 UTF-8 CSV")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;") if text.strip() else csv.excel
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]


def _extract_code(cell):
    m = _CODE_RE.search(str(cell or ""))
    return m.group(1) if m else None


# ---------- stocks.txt 格式（與 quant_system/monitoring/parsers.py 相同規則）----------
#   ###010:IC設計/IP          → 分類定義（3 個 #）
#   2454聯發科:001,010,012     → 股票與所屬分類
#   #3661世芯-KY:010,011       → 前面 1 個 #：明日預報忽略，但盤中仍算所屬分類（預設納入）
#   ## 任何文字                 → 忽略
#   TOP_CLASS=031,042,100      → 重點分類（畫面加 ★）
#   TOP_STOCK=2330,6488        → 重點個股（成分股明細加 ★）
_CAT_RE = re.compile(r"^###(\d+)\s*:\s*(.+)$")
_STK_RE = re.compile(r"^#?(\d{4}[^:]*)\s*:\s*(.+)$")


def is_stocks_txt(path):
    if not path.lower().endswith(".txt"):
        return False
    try:
        text = _read_text(path)
    except SystemExit:
        return False
    return any(_CAT_RE.match(l.strip()) for l in text.splitlines())


def _read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp950", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SystemExit("族群檔編碼無法辨識：%s" % path)


def load_stocks_txt(path, include_commented=True):
    cat, membership, names = {}, {}, {}
    top_cls, top_stk, commented = [], [], set()
    for line in _read_text(path).splitlines():
        s = line.strip()
        bare = s.lstrip("#").strip()
        if bare.startswith("TOP_CLASS=") or bare.startswith("TOP_STOCK="):
            if not s.startswith("#"):
                vals = [v.strip() for v in bare.split("=", 1)[1].split(",") if v.strip()]
                (top_cls if bare.startswith("TOP_CLASS") else top_stk).extend(vals)
            continue
        m = _CAT_RE.match(s)
        if m:
            cat[m.group(1)] = m.group(2).strip()
            continue
        m = _STK_RE.match(s)
        if not m:
            continue
        head = m.group(1).strip()
        code = head[:4]
        if not code.isdigit():
            continue
        if s.startswith("#"):
            commented.add(code)
            if not include_commented:
                continue
        names[code] = head[4:].strip()
        membership[code] = [g.strip() for g in m.group(2).split(",") if g.strip()]
    label = lambda c: cat.get(c, "分類" + c)
    return {"format": "stocks.txt",
            "membership": {code: [label(c) for c in gs] for code, gs in membership.items()},
            "group_class": {},
            "group_codes": {label(c): c for c in cat},
            "top_groups": [label(c) for c in top_cls],
            "top_stocks": top_stk, "names": names, "commented": sorted(commented)}


def load_groups(path, include_commented=True):
    """自動判斷族群檔格式，回傳 dict(membership, group_class, top_groups, top_stocks, format, names)。"""
    if is_stocks_txt(path):
        return load_stocks_txt(path, include_commented)
    m, c = load_group_file(path)
    return {"format": "table", "membership": m, "group_class": c, "top_groups": [], "top_stocks": [], "names": {}}


def load_group_file(path):
    """回傳 (membership, group_class)
    membership: {code: [group, ...]}
    group_class: {group: "電子"/"傳產"/"金融"}（檔案有寫才有）
    """
    if is_stocks_txt(path):
        d = load_stocks_txt(path)
        return d["membership"], d["group_class"]
    membership, group_class = {}, {}

    def add(code, group, klass=None):
        group = str(group).strip()
        if not code or not group:
            return
        lst = membership.setdefault(code, [])
        if group not in lst:
            lst.append(group)
        if klass:
            k = str(klass).strip()
            if k in (CLASS_ELEC, CLASS_TRAD, CLASS_FIN):
                group_class[group] = k

    if path.lower().endswith(".json"):
        data = json.load(open(path, encoding="utf-8-sig"))
        for g, codes in data.items():
            for c in codes:
                add(_extract_code(c), g)
        return membership, group_class

    rows = [r for r in _read_rows(path) if any(x for x in r)]
    if not rows:
        return membership, group_class
    header = rows[0]
    ci, gi, ki = _find_col(header, _CODE_COLS), _find_col(header, _GROUP_COLS), _find_col(header, _CLASS_COLS)

    if ci is not None and gi is not None:  # 長表
        for r in rows[1:]:
            if len(r) <= max(ci, gi):
                continue
            code = _extract_code(r[ci])
            klass = r[ki] if ki is not None and ki < len(r) else None
            for g in _SPLIT.split(r[gi]):
                add(code, g, klass)
    else:  # 寬表：每欄一個族群
        for col, g in enumerate(header):
            if not str(g).strip():
                continue
            for r in rows[1:]:
                if col < len(r):
                    for token in _SPLIT.split(r[col]):
                        add(_extract_code(token), g)
    return membership, group_class


class GroupMap:
    """一組「股票 → 族群」對應。
    group_file=None：證交所/櫃買產業別。
    有 group_file：用自訂族群；沒涵蓋的股票依 unassigned 處理（industry / other / drop）。"""

    def __init__(self, stocks, group_file=None, unassigned="industry", include_commented=True):
        # stocks: {code: {"name":..., "category":..., ...}}
        self.stock_groups = {}
        self.group_class = {}
        self.top_groups, self.top_stocks, self.file_names = [], [], {}
        self.source = "證交所產業分類"
        self.format = "industry"
        custom = {}
        if group_file:
            d = load_groups(group_file, include_commented)
            custom = d["membership"]
            self.group_class.update(d["group_class"])
            self.top_groups, self.top_stocks = d["top_groups"], d["top_stocks"]
            self.file_names = d.get("names", {})
            self.format = d["format"]
            self.source = os.path.basename(group_file)
        for code, info in stocks.items():
            if code in custom:
                self.stock_groups[code] = list(custom[code])
            elif not group_file or unassigned == "industry":
                self.stock_groups[code] = [industry_name(info.get("category"))]
            elif unassigned == "other":
                self.stock_groups[code] = ["未分類"]
            # unassigned == "drop"：不計入任何族群（仍計入全市場）
        self.custom_codes = [c for c in custom if c in stocks]
        self.unknown_codes = sorted(set(custom) - set(stocks)) if custom else []

    def groups_of(self, code):
        return self.stock_groups.get(code, ())

    def all_groups(self):
        s = set()
        for g in self.stock_groups.values():
            s.update(g)
        return sorted(s)


class GroupSet(object):
    """畫面上可切換的一組族群（例如「我的族群」「產業別」）。"""

    def __init__(self, sid, label, gm):
        self.id, self.label, self.gm = sid, label, gm

    def meta(self):
        return {"id": self.id, "label": self.label, "group_source": self.gm.source,
                "top_groups": self.gm.top_groups, "top_stocks": self.gm.top_stocks}


def build_group_sets(stocks, group_file=None, unassigned="drop", show_industry=True,
                     include_commented=True, custom_label="我的族群"):
    """有族群檔 → [我的族群, 產業別]；沒有 → [產業別]。"""
    sets = []
    if group_file:
        un = unassigned if not show_industry else ("drop" if unassigned == "industry" else unassigned)
        sets.append(GroupSet("custom", custom_label, GroupMap(stocks, group_file, un, include_commented)))
    if not group_file or show_industry:
        sets.append(GroupSet("industry", "產業別", GroupMap(stocks, None)))
    return sets
