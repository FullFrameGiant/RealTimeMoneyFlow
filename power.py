# -*- coding: utf-8 -*-
"""Windows 電源計畫與自動關機（非 Windows 自動略過）。

  * 啟動：記下目前的電源計畫 → 切換成「省電」→ 擋住系統睡眠（螢幕仍可關）
  * 結束（正常結束、Ctrl+C、按視窗的 X、登出）：先還原原本的電源計畫，再結束程式
  * 程式被工作管理員強制結束等無法攔截的情況：原本的計畫存在 data/power_restore.json，
    下次啟動會以它為準；也可以雙擊 restore_power.bat 手動還原
  * 當天指定時間（預設 13:40）程式還在跑 → 還原電源、登出、存檔後執行 shutdown /s /t 60

手動還原：python power.py --restore
"""
import ctypes
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import threading

IS_WIN = os.name == "nt"
POWER_SAVER = "a1841308-3541-4fab-bc81-f71556f20b4a"     # Windows 內建「省電」計畫
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_LINE = re.compile(r"(%s)\s*(?:\(([^)]*)\))?\s*(\*)?" % _GUID)

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
CTRL_EVENTS = {0: "Ctrl+C", 1: "Ctrl+Break", 2: "關閉視窗", 5: "登出", 6: "系統關機"}


def _decode(raw):
    encs = []
    if IS_WIN:
        try:
            encs.append("cp%d" % ctypes.windll.kernel32.GetConsoleOutputCP())
        except Exception:
            pass
    for enc in encs + ["cp950", "utf-8"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def run_powercfg(args):
    """回傳 (returncode, 輸出文字)。測試時可替換。"""
    r = subprocess.run(["powercfg"] + list(args), capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, _decode(r.stdout + r.stderr)


def parse_schemes(text):
    """powercfg /list 或 /getactivescheme 的輸出 → [(guid, 名稱, 是否使用中)]（中英文 Windows 都適用）"""
    out = []
    for line in text.splitlines():
        m = _LINE.search(line)
        if m:
            out.append((m.group(1).lower(), (m.group(2) or "").strip(), bool(m.group(3))))
    return out


class PowerPlan(object):
    def __init__(self, runner=run_powercfg):
        self.run = runner

    def active(self):
        code, text = self.run(["/getactivescheme"])
        s = parse_schemes(text)
        return (s[0][0], s[0][1]) if code == 0 and s else (None, "")

    def schemes(self):
        code, text = self.run(["/list"])
        return parse_schemes(text) if code == 0 else []

    def name_of(self, guid):
        for g, n, _ in self.schemes():
            if g == guid:
                return n
        return ""

    def find_power_saver(self):
        for g, n, _ in self.schemes():
            if g == POWER_SAVER or "省電" in n or "power saver" in n.lower():
                return g
        # 有些電腦把省電計畫藏起來了：從 Windows 內建範本複製一份出來
        code, text = self.run(["-duplicatescheme", POWER_SAVER])
        s = parse_schemes(text)
        return s[0][0] if code == 0 and s else None

    def set_active(self, guid):
        code, _ = self.run(["/setactive", guid])
        return code == 0


class PowerGuard(object):
    """執行期間切換到省電計畫，結束時還原。restore() 可重複呼叫、執行緒安全。"""

    def __init__(self, state_file, use_saver=True, keep_awake=True, plan=None, log=print, enabled=IS_WIN):
        self.state_file = state_file
        self.use_saver = use_saver
        self.keep_awake = keep_awake
        self.plan = plan or PowerPlan()
        self.log = log
        self.enabled = enabled
        self.original = None
        self.original_name = ""
        self.lock = threading.Lock()
        self.restored = True

    def _load_pending(self):
        try:
            d = json.load(open(self.state_file, encoding="utf-8"))
            return d.get("guid"), d.get("name", "")
        except (IOError, OSError, ValueError):
            return None, ""

    def start(self):
        if not self.enabled:
            return
        if self.use_saver:
            pend, pend_name = self._load_pending()
            cur, cur_name = self.plan.active()
            if pend:
                self.original, self.original_name = pend, pend_name
                self.log("[power] 上次結束時沒有還原，這次以存檔的原計畫「%s」為準" % (pend_name or pend))
            else:
                self.original, self.original_name = cur, cur_name
            if not self.original:
                self.log("[power] 讀不到目前的電源計畫，略過切換")
            else:
                d = os.path.dirname(self.state_file)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self.state_file, "w", encoding="utf-8") as f:
                    json.dump({"guid": self.original, "name": self.original_name,
                               "saved_at": _dt.datetime.now().isoformat(timespec="seconds")}, f, ensure_ascii=False)
                saver = self.plan.find_power_saver()
                if not saver:
                    self.log("[power] 找不到也建立不了省電計畫，維持「%s」" % self.original_name)
                elif saver == cur:
                    self.log("[power] 目前已經是省電計畫（結束時會還原成「%s」）" % self.original_name)
                elif self.plan.set_active(saver):
                    self.log("[power] 電源計畫：%s → %s（結束時還原）" % (
                        self.original_name or self.original, self.plan.name_of(saver) or "省電"))
                else:
                    self.log("[power] 切換省電計畫失敗，維持原計畫")
                self.restored = False
        if self.keep_awake and IS_WIN:
            # 必須在主執行緒呼叫：效果跟著呼叫的執行緒，程式結束自動失效
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            self.log("[power] 執行期間不讓電腦進入睡眠（螢幕仍可關閉）")

    def restore(self):
        with self.lock:
            if self.restored or not self.original:
                return
            self.restored = True
        ok = self.plan.set_active(self.original)
        if ok:
            try:
                os.remove(self.state_file)
            except OSError:
                pass
            self.log("[power] 已還原電源計畫：%s" % (self.original_name or self.original))
        else:
            self.log("[power] 還原電源計畫失敗！請雙擊 restore_power.bat 或手動切回「%s」" % self.original_name)


_handler_ref = None


def install_console_handler(on_event):
    """攔截 Ctrl+C / 按視窗 X / 登出。on_event(名稱) 會在系統強制結束前執行（關閉視窗約有 5 秒）。"""
    global _handler_ref
    if not IS_WIN:
        return False
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    def handler(ev):
        try:
            on_event(CTRL_EVENTS.get(ev, "事件 %d" % ev))
        except Exception as e:  # 這裡不能讓例外跑出去
            print("[exit] 清理時發生錯誤：%s" % e, flush=True)
        # Ctrl+C / Ctrl+Break 交回 Python 預設處理（變成 KeyboardInterrupt 正常結束）；其他事件到此為止
        return ev not in (0, 1)

    _handler_ref = proto(handler)      # 保留參考，避免被回收
    return bool(ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler_ref, True))


def parse_hhmm(s):
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise SystemExit("shutdown_at 格式要像 13:40（目前是 %r）" % s)
    return _dt.time(int(m.group(1)), int(m.group(2)))


class ShutdownTimer(object):
    """當天 at（台灣時間）程式還在跑 → before() → shutdown /s /t countdown。啟動時已過該時間就不排。"""

    def __init__(self, at, countdown, before, now_fn, log=print, runner=None):
        self.at, self.countdown, self.before, self.now_fn, self.log = at, int(countdown), before, now_fn, log
        self.runner = runner            # 測試用；None = Windows 上真的執行 shutdown
        self.cancel_evt = threading.Event()
        self.target = None
        self.tick = 30                  # 每隔幾秒重新對時

    def start(self):
        now = self.now_fn()
        target = _dt.datetime.combine(now.date(), self.at)
        if now >= target:
            self.log("[power] 現在已過 %s，今天不排自動關機" % self.at.strftime("%H:%M"))
            return False
        self.target = target
        self.log("[power] 今天 %s 會自動關機（程式要還在執行；提早結束程式就不會關機）" % self.at.strftime("%H:%M"))
        threading.Thread(target=self._wait, daemon=True).start()
        return True

    def _wait(self):
        while not self.cancel_evt.is_set():
            left = (self.target - self.now_fn()).total_seconds()
            if left <= 0:
                break
            self.cancel_evt.wait(min(left, self.tick))
        if self.cancel_evt.is_set():
            return
        self.fire()

    def fire(self):
        self.log("[power] %s 到了：還原電源計畫、登出、存檔後關機" % self.at.strftime("%H:%M"))
        try:
            self.before()
        finally:
            msg = "即時資金流：%s 自動關機。要取消請在命令提示字元執行 shutdown /a" % self.at.strftime("%H:%M")
            args = ["shutdown", "/s", "/t", str(self.countdown), "/c", msg]
            if self.runner is not None:
                self.runner(args)
            elif IS_WIN:
                subprocess.run(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                self.log("[power] 非 Windows，略過關機")
                return
            self.log("[power] 已下達關機指令，%d 秒後關機（取消：shutdown /a）" % self.countdown)

    def cancel(self):
        self.cancel_evt.set()


if __name__ == "__main__":
    if "--restore" in sys.argv:
        here = os.path.dirname(os.path.abspath(__file__))
        g = PowerGuard(os.path.join(here, "data", "power_restore.json"), keep_awake=False)
        guid, name = g._load_pending()
        if not guid:
            print("沒有需要還原的紀錄（data/power_restore.json 不存在），目前電源計畫：%s" % (g.plan.active()[1] or "?"))
        else:
            g.original, g.original_name, g.restored = guid, name, False
            g.restore()
    else:
        p = PowerPlan()
        print("目前：", p.active())
        for row in p.schemes():
            print(row)
