# -*- coding: utf-8 -*-
"""
AI 红绿灯 —— 桌面悬浮窗 (iOS 玻璃质感版)。

每个项目一张半透明磨砂玻璃卡片:
  [圆环+中心亮点 三灯]  项目名
                        正在处理的内容

灯色: 绿=AI运行中  黄=需我处理  红=已完成/空闲
项目来源: VSCode 当前打开的文件夹 (自动)。
状态来源: 各项目根目录 .ai-status.json (无则默认红)。

渲染: Pillow 超采样抗锯齿 + 高斯柔光; 显示: Win32 分层窗口逐像素 alpha。
运行: pythonw app.py
"""

import os
import sys
import json
import time
import datetime
import shutil
import sqlite3
import subprocess
import urllib.parse
import ctypes
from ctypes import wintypes
from PIL import Image, ImageDraw, ImageFont, ImageFilter

CODEX_SESSIONS = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
CODEX_FRESH_SEC = 45   # rollout 文件 mtime 在此秒数内 = Codex 正在该项目干活; Codex 写 rollout 比 Claude 写 transcript 稀疏(每个动作才写一次, 推理/长命令间隔常达数十秒), 窗口太小(旧值 12)会在 Codex 思考时闪红

# Claude 的灯色由 transcript 尾部语义(transcript_state: 球在谁手里)决定, 不再拿 mtime 猜"还在不在干活"
# —— 一条跑十分钟的命令期间 transcript 一个字节都不写, 拿 mtime 判空闲必然把"在跑"误判成红。
# 下面三个秒数只是兜底, 不参与正常判定。
CLAUDE_IDLE_GRACE_SEC = 30  # 尾部=回合已结束、文件却还写着绿(Stop 钩子没响的僵尸绿): 再等这么久没动才落红。
                            # 留余量是因为回合中途 assistant 常先写一条文本、隔几秒才写 tool_use, 那个缝隙
                            # 里尾部看着就像"回合结束", 别闪红。
CLAUDE_BUSY_MAX_SEC = 1800  # 尾部=该 AI 动(工具在跑/正在生成)却这么久没写过一个字节 = 会话被杀/窗口被关,
                            # 没有任何钩子会响 -> 转黄(它没干完, 判红=撒谎说"已完成"), 免得永久绿。
CLAUDE_STALE_SEC = 600      # 尾部读不出来(unknown)时才用的 mtime 兜底。

CREATE_NO_WINDOW = 0x08000000

# 数据目录 (日志/计时历史/备注/窗口设置)。打包成 exe 后不能用 __file__: onefile 模式下它指向
# 每次运行临时解压的目录, 一退出就没了 —— 设置和历史会凭空消失。所以打包版落到 LOCALAPPDATA,
# 源码版仍放仓库目录 (开发时就地可见, 也不动已有的历史文件)。
if getattr(sys, "frozen", False):
    HERE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "AITrafficLight")
    os.makedirs(HERE, exist_ok=True)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(HERE, "widget.log")


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
APPDATA = os.environ.get("APPDATA", "")
VSCODE_STATE = os.path.join(APPDATA, "Code", "User", "globalStorage", "storage.json")
STATUS_FILE = ".ai-status.json"
TASK_MAX_CHARS = 13

C_RED, C_YELLOW, C_GREEN = (255, 59, 48), (255, 204, 0), (52, 199, 89)
LAMPS = [("red", C_RED), ("yellow", C_YELLOW), ("green", C_GREEN)]
LABELS = {"green": "运行中", "yellow": "待处理", "red": "已完成"}

# 逻辑尺寸 (DPI=100% 时的像素)
CARD_W, CARD_H = 250, 50
RADIUS, MARGIN, VGAP = 16, 10, 9
LAMP_R, GAP, PAD_L, TEXT_GAP = 8, 9, 15, 14
NAME_PT, TASK_PT = 17, 10
FIXTURE_W = LAMP_R * 6 + GAP * 2
TEXT_X = PAD_L + FIXTURE_W + TEXT_GAP
TEXT_W = CARD_W - TEXT_X - 14

# 右侧按钮 (常用指令) —— 单行排列, 按标签自适应宽度
BTN_H, BTN_GAP, BTN_PAD_X, BTN_PT = 19, 6, 11, 9
BTN_ZONE_PAD, BTN_ZONE_RPAD = 12, 12

# 贴边收起 (拖到屏幕上/下/左/右边缘 -> 缩进去, 只留一条彩色细边)
DOCK_STRIP = 4                 # 收起后露出的细边厚度 (逻辑 px)
DOCK_SNAP = 16                 # 松手时离边多近算吸附 (逻辑 px)
DOCK_HOT = 8                   # 细边命中区外扩余量: 细边可以很细, 但要好碰 —— 命中区比它宽
DOCK_MS, DOCK_STEPS = 110, 9   # 滑出/滑回动画时长与帧数
STRIP_ALPHA = 235              # 细边不透明度: 不跟随透明度滑块, 收起了也要看得清

# ---------------------------------------------------------------------------
# UpTime 计时器 + 备注 (原 standup.py, 现合并进同一磨砂玻璃窗口)
# ---------------------------------------------------------------------------
NOTES_FILE = os.path.join(HERE, "notes.txt")
STANDUP_LOG_FILE = os.path.join(HERE, "standup_log.json")
UI_FILE = os.path.join(HERE, "standup_ui.json")

TITLE = "UpTime"
TICK_MS = 1000                # 计时器每秒刷新 (数据仍每 3 拍即 ~3s 扫描一次)

# UpTime 卡片布局 (逻辑 px, 与红绿灯卡片同一坐标系)
UP_PAD = 16                   # 卡片左右内边距
UP_TITLE_CY = 22              # 标题行竖直中心
UP_TIMER_CY = 60             # 大字计时竖直中心
UP_STAT_CY = 96              # 统计副标题竖直中心
UP_NOTE_TOP = 112            # 备注面板顶
UP_NOTE_PAD = 8              # 备注面板内边距
UP_NOTE_LH = 19             # 备注行高
UP_NOTE_BOT = 14            # 备注面板到卡片底
UP_SHUT = 44               # 折叠时卡片高
UP_MAX_LINES = 8           # 备注最多显示行数 (超出可在编辑框滚动)
UP_BTN_H = 26              # Start/Stop 胶囊高
UP_BTN_PADX = 14

TITLE_PT, TIMER_PT, STAT_PT, NOTE_PT, UPBTN_PT = 17, 30, 12, 12, 12

# 设置面板 (悬停窗口右下角小手柄 -> 弹出; 两个滑块: 透明度 + 整体尺寸)
SET_GEAR = 22                  # 右下角触发热区 (逻辑 px)
SET_PANEL_H = 96               # 展开后设置面板高 (逻辑 px) —— 含底部一排常用值预设
SET_ROW_H = 30                 # 每个滑块行高
SET_TRACK_H = 6                # 轨道粗细
SET_KNOB_R = 7                 # 旋钮半径 (小一些)
OPA_MIN, OPA_MAX = 0.30, 1.0   # 透明度范围 (整窗 alpha 倍数)
SCL_MIN, SCL_MAX = 0.70, 1.60  # 整体尺寸范围 (相对当前 DPI)

# 一键预设 (常用值) —— 面板底部一排小胶囊: 左键点=套用, 右键点=把当前滑块值存进这颗
SET_CHIP_LABELS = ("淡", "标准", "清晰")
SET_CHIP_H = 18                # 预设胶囊高 (逻辑 px)
SET_CHIP_CY = 76               # 预设行竖直中心 (相对面板顶 y_l)
DEFAULT_PRESETS = [
    {"op": 0.45, "sz": 0.80},  # 淡: 很透 + 偏小 (扫一眼)
    {"op": 0.75, "sz": 1.00},  # 标准
    {"op": 1.00, "sz": 1.20},  # 清晰: 不透 + 偏大 (细看)
]

C_TITLE = (33, 36, 42)        # 标题深色 (同项目名)
C_TIMER = (27, 35, 48)        # 计时深色
C_STAT = (33, 36, 42)         # 副标题: 与标题同款黑
C_CARET = (154, 164, 176)     # 折叠箭头灰
C_NOTE = (38, 50, 63)         # 备注文字
NOTE_FILL = (238, 241, 246, 210)   # 备注面板: 略浅的同族磨砂

# 中英文分别用字体: 英文/数字用 Segoe UI(接近苹果 SF), 中文用微软雅黑
LAT_NAME = "C:/Windows/Fonts/seguisb.ttf"   # Segoe UI Semibold (加粗)
LAT_TASK = "C:/Windows/Fonts/segoeui.ttf"   # Segoe UI
CJK_NAME = "C:/Windows/Fonts/msyhbd.ttc"    # 微软雅黑 Bold (加粗)
CJK_TASK = "C:/Windows/Fonts/msyh.ttc"      # 微软雅黑


def _is_cjk(ch):
    return ord(ch) >= 0x2E80
S = 3  # 超采样倍数


def lerp(c1, c2, t):
    return tuple(round(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


# ---- UpTime 数据 / 格式 (原 standup.py) ----
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("save %s: %r" % (path, e))


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _write_text(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        log("write %s: %r" % (path, e))


def today_key():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def fmt_hms(sec):
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def fmt_dur(sec):
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    return ("%dh%dm" % (h, m)) if h else ("%dm" % m)


def _clamp_onscreen(x, y, w, h):
    """存档坐标落在所有显示器之外时拉回可见虚拟桌面内; 已在屏内则原样不动。"""
    try:
        gm = ctypes.windll.user32.GetSystemMetrics
        vx, vy = gm(76), gm(77)            # SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN
        vw, vh = gm(78), gm(79)            # SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN
        if vw <= 0 or vh <= 0:
            return (x, y)
        x = min(max(x, vx), vx + vw - w)
        y = min(max(y, vy), vy + vh - h)
    except Exception:
        pass
    return (x, y)


# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------
def _find_code_exe():
    c = shutil.which("code")
    if c:
        exe = os.path.join(os.path.dirname(os.path.dirname(c)), "Code.exe")
        if os.path.exists(exe):
            return exe
    for g in (r"D:\vs code\Microsoft VS Code\Code.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
              r"C:\Program Files\Microsoft VS Code\Code.exe"):
        if os.path.exists(g):
            return g
    return c


CODE_EXE = _find_code_exe()


_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _find_vscode_window(name):
    """按标题找到该项目已打开的 VSCode 窗口句柄 (标题含项目名 + 'Visual Studio Code')。"""
    u = ctypes.windll.user32
    found = []
    low = name.lower()

    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value
            if "Visual Studio Code" in t and low in t.lower():
                found.append(hwnd)
                return False
        return True

    u.EnumWindows(_WNDENUMPROC(cb), 0)
    return found[0] if found else None


def _bring_to_front(hwnd):
    u = ctypes.windll.user32
    try:
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)            # SW_RESTORE
        fg = u.GetForegroundWindow()
        t_fg = u.GetWindowThreadProcessId(fg, None)
        t_me = ctypes.windll.kernel32.GetCurrentThreadId()
        u.AttachThreadInput(t_fg, t_me, True)
        u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
        u.AttachThreadInput(t_fg, t_me, False)
    except Exception:
        try:
            u.SetForegroundWindow(hwnd)
        except Exception:
            pass


def open_in_vscode(path):
    """聚焦该文件夹已打开的 VSCode 窗口; 没开才启动一个新窗口。"""
    if not path:
        return
    name = os.path.basename(path.rstrip("\\/"))
    hwnd = _find_vscode_window(name) if name else None
    if hwnd:
        _bring_to_front(hwnd)
        return
    if CODE_EXE and os.path.isdir(path):
        try:
            subprocess.Popen([CODE_EXE, path], creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass


def _uri_to_path(uri):
    p = urllib.parse.unquote(uri or "")
    if p.startswith("file:///"):
        p = p[len("file:///"):]
    p = p.replace("/", "\\")
    if len(p) > 1 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def _recent_folder_map():
    """basename(小写) -> 完整路径; 来自 VSCode 最近打开记录 (state.vscdb)。"""
    m = {}
    db = os.path.join(APPDATA, "Code", "User", "globalStorage", "state.vscdb")
    try:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % db.replace("\\", "/"),
                              uri=True, timeout=0.5)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key='history.recentlyOpenedPathsList'"
            ).fetchone()
        finally:
            con.close()
        if row:
            # entries 按最近打开排序; 同名只保留最近的一条 (setdefault)
            for e in json.loads(row[0]).get("entries", []):
                uri = e.get("folderUri")
                if uri:
                    p = _uri_to_path(uri)
                    if p:
                        m.setdefault(os.path.basename(p.rstrip("\\/")).lower(), p)
    except Exception:
        pass
    # 兜底: storage.json 里的 openedWindows
    if not m:
        try:
            with open(VSCODE_STATE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for win in data.get("windowsState", {}).get("openedWindows", []):
                if win.get("folder"):
                    p = _uri_to_path(win["folder"])
                    m[os.path.basename(p.rstrip("\\/")).lower()] = p
        except Exception:
            pass
    return m


def _open_vscode_window_names():
    """实时枚举 VSCode 窗口, 解析每个窗口打开的文件夹名 (rootName)。"""
    u = ctypes.windll.user32
    names = []

    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value
            idx = t.find(" - Visual Studio Code")
            if idx > 0:
                # 标题: "[活动编辑器 - ]文件夹名 - Visual Studio Code"
                folder = t[:idx].split(" - ")[-1].strip()
                if folder and folder not in names:
                    names.append(folder)
        return True

    try:
        u.EnumWindows(_WNDENUMPROC(cb), 0)
    except Exception:
        pass
    return names


def _norm(p):
    p = (p or "").replace("/", "\\").rstrip("\\")
    if len(p) > 1 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def same_tree(a, b):
    """两路径是否同一项目树 (相等或一个是另一个的父目录)。"""
    a, b = _norm(a).lower(), _norm(b).lower()
    return bool(a) and bool(b) and (a == b or a.startswith(b + "\\") or b.startswith(a + "\\"))


def _codex_title(sid):
    """从 session_index.jsonl 取该会话的智能标题 (thread_name)。"""
    if not sid:
        return None
    path = os.path.join(os.path.dirname(CODEX_SESSIONS), "session_index.jsonl")
    title = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("id") == sid and d.get("thread_name"):
                        title = d["thread_name"]
                except Exception:
                    pass
    except Exception:
        pass
    return title


def codex_active():
    """Codex 此刻正在干活的项目 -> (cwd, 智能标题); 空闲则 None。
    只看今天/昨天最新 rollout 文件 mtime, 轻量。"""
    try:
        now = time.time()
        cand = []
        today = datetime.date.today()
        for d in (today, today - datetime.timedelta(days=1)):
            folder = os.path.join(CODEX_SESSIONS, "%04d" % d.year,
                                  "%02d" % d.month, "%02d" % d.day)
            if os.path.isdir(folder):
                for fn in os.listdir(folder):
                    if fn.endswith(".jsonl"):
                        cand.append(os.path.join(folder, fn))
        if not cand:
            return None
        newest = max(cand, key=os.path.getmtime)
        if now - os.path.getmtime(newest) > CODEX_FRESH_SEC:
            return None
        with open(newest, "r", encoding="utf-8") as f:
            meta = json.loads(f.readline())
        payload = meta.get("payload", {})
        cwd = payload.get("cwd") or meta.get("cwd")
        sid = payload.get("id") or meta.get("id")
        if not cwd:
            return None
        return (_norm(cwd), _codex_title(sid))
    except Exception:
        return None


def get_open_projects():
    """当前在 VSCode 里打开的项目路径 (实时, 基于窗口标题)。"""
    names = _open_vscode_window_names()
    if not names:
        return []
    recent = _recent_folder_map()
    out = []
    for nm in names:
        p = recent.get(nm.lower())
        if p and p not in out:
            out.append(p)
    return out


def _transcript_age(tpath):
    """transcript 多久没被写过(秒); 没有文件 -> 无穷大。只用于兜底, 不用于正常判定。"""
    if not tpath:
        return float("inf")
    try:
        return time.time() - os.path.getmtime(tpath)
    except OSError:
        return float("inf")


# 非对话记录(标题/附件/快照/队列/API 报错), 它们随时会追加, 不代表谁在动 -> 找尾部消息时跳过。
SKIP_RECORDS = ("system", "attachment", "file-history-snapshot",
                "last-prompt", "ai-title", "queue-operation", "summary")
TAIL_BYTES = 262144                   # 只读尾部 256KB: 够装下最后几条记录(含大 tool_result), 又不整文件读

# 这几个工具的"结果"只能由人给出 —— 挂在这儿就等于球在你手里, 该黄。
# 不能靠 Notification 钩子认它们: AI 提问(AskUserQuestion)/等你批计划(ExitPlanMode) 时
# Claude Code 根本不发 Notification, 钩子不响, 文件还停在 UserPromptSubmit 写的绿 ——
# 于是"AI 在等你回答"却一路绿灯, 正是要红绿灯解决的那个场景反而漏了。工具名在 transcript
# 尾部写得明明白白, 认它比认钩子可靠。
WAIT_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


def _blocks(msg):
    """消息里的内容块类型集合, 如 {"text", "tool_use", "thinking", "tool_result"}。"""
    out = set()
    c = msg.get("content")
    if isinstance(c, list):
        for x in c:
            if isinstance(x, dict):
                out.add(str(x.get("type") or ""))
            else:
                out.add("text")
    elif c:
        out.add("text")
    return out


def _tool_names(msg):
    """这条 assistant 记录里发起的工具名集合。"""
    c = msg.get("content")
    if not isinstance(c, list):
        return set()
    return {str(x.get("name") or "") for x in c
            if isinstance(x, dict) and x.get("type") == "tool_use"}


def _msg_text(msg):
    c = msg.get("content")
    if isinstance(c, list):
        parts = []
        for x in c:
            if isinstance(x, dict):
                parts.append(str(x.get("text") or x.get("content") or ""))
            else:
                parts.append(str(x))
        return " ".join(parts)
    return str(c or "")


def transcript_state(tpath):
    """读 transcript 尾部, 判断"球在谁手里" —— 这才是灯色的真信号。
    mtime 不是: 一条跑十分钟的命令期间 transcript 一个字节都不写, 拿"多久没动"判空闲, 必然把
    正在跑长命令的会话误判成红。尾部那条消息则永远是准的:

      "ask"  尾部挂着 WAIT_TOOLS 里的 tool_use (AI 在问你 / 等你批计划) = 球在你手里 -> 黄。
             这类工具不发 Notification 钩子, 只能从工具名认。
      "error" 尾部是带 isApiErrorMessage 的 assistant 记录 (掉线 / 认证失败 / 限额) = AI 不是干完了,
             是死在半路, 你不重试它就永远不动 -> 黄。注意它长得跟正常收尾一模一样(assistant + 纯文本),
             不认这个标记就会被当成 "idle" 判红 = "已完成", 而你根本不知道它其实崩了。
             重试中的瞬时报错不在这儿: 那是 type=system/subtype=api_error, 已被 SKIP_RECORDS 跳过,
             不该因为一次自动重试就点黄灯。
      "tool" AI 发了别的 tool_use 但还没有结果回来。两种可能, transcript 里长得一模一样, 靠文件灯色分:
             文件绿 = 工具正在跑 (跑多久都算在干活); 文件黄 = Notification 说要你批准, 还堵在你这儿。
      "gen"  尾部是 user 记录 (你的话, 或工具结果回来了) = 球在 AI 手里, 它正在生成。
      "idle" 尾部是 assistant 纯文本 = 回合已结束, 球在你手里。(只含 thinking 的记录不算结束:
             思考块永远不会是回合终点, 后面必然还有文本或工具。)
      "interrupted" 尾部是 "[Request interrupted by user]" = 你按了 Esc。中断不触发任何钩子
             (Stop 不在中断时触发, 会话也没结束), 文件还停在绿, 只有这个标记能认。再发话它就不在
             尾部了 -> 自动解除。
      "unknown" 没文件 / 读不出 / 尾部凑不出一条完整记录 -> 交给 mtime 兜底。
    """
    if not tpath:
        return "unknown"
    try:
        with open(tpath, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return "unknown"
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue                       # 半截行(读到一半 / 尾部被截断) -> 看上一条
        if d.get("type") in SKIP_RECORDS:
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        if d.get("type") == "assistant":
            if d.get("isApiErrorMessage"):
                return "error"             # 报错落到尾部 = 这轮真的死了 (见下面 "error" 说明)
            blocks = _blocks(msg)
            if "tool_use" in blocks:
                if _tool_names(msg) & WAIT_TOOLS:
                    return "ask"           # AI 在等你回答/等你批计划 -> 球在你手里
                return "tool"
            if "text" in blocks:
                return "idle"
            return "gen"                   # 只有 thinking: 还在生成, 回合没完
        if d.get("type") == "user":
            if "[Request interrupted by user" in _msg_text(msg):
                return "interrupted"
            return "gen"                   # 你的话 / 工具结果 -> 轮到 AI 动
    return "unknown"


def compact_task(text, limit=TASK_MAX_CHARS, fallback=""):
    raw = str(text or "").strip().replace("\r", " ").replace("\n", " ")
    text = " ".join(raw.split())
    if "重点是" in text:
        text = text.split("重点是", 1)[1]
    elif ":" in text:
        text = text.split(":", 1)[1]
    elif "：" in text:
        text = text.split("：", 1)[1]
    for old in (
        "正在推进", "正在", "任务推进", "已暂停，等待你确认或补充",
        "等待你确认或补充", "已处理完，当前空闲", "已处理完",
        "待确认: ", "待确认：", "当前",
    ):
        text = text.replace(old, "")
    text = text.strip(" ，,。:：;；")
    if text in ("", "已完成", "空闲", "任务推进"):
        text = fallback or raw.strip()
    return text[:limit]


# 摘要末尾的状态后缀 (须与 status_hook.py 的 STATE_SUFFIX 一致): 摘要 = 核心短语 + 后缀之一。
STATE_SUFFIX = {"green": "处理中", "yellow": "待确认", "red": "已完成"}


def retint_suffix(text, status):
    """把摘要末尾的状态后缀换成与当前解析出的灯色匹配的那个。
    app.py 会强制改灯 (僵尸绿落红 / 活动心跳强制绿), 但 .ai-status.json 里的 summary 是上一个
    钩子按旧状态写的, 后缀会和新灯色矛盾 (红灯却写 "…处理中")。这里剥掉旧后缀重贴新的, 核心短语
    ("页面更新") 保留, 让字和灯一致。"""
    t = (text or "").strip()
    for suf in ("处理中", "待确认", "已完成"):
        if t.endswith(suf):
            t = t[:-len(suf)]
            break
    return t + STATE_SUFFIX.get(status, "")


def read_status(path):
    info = {"status": "red", "task": "", "summary": "", "buttons": [], "_topic": "", "_transcript": ""}
    try:
        with open(os.path.join(path, STATUS_FILE), "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        s = str(raw.get("status", "red")).lower()
        if s in ("red", "yellow", "green"):
            info["status"] = s
        info["task"] = str(raw.get("task", "")).strip()
        info["summary"] = str(raw.get("summary", "")).strip()
        info["_topic"] = str(raw.get("_topic", "")).strip()
        # 状态文件里存的是 "~/..." 相对形式(绝对路径带用户名, 那文件可能被提交进版本库), 这里展开。
        # 老文件存的是绝对路径, expanduser 原样返回, 兼容。
        info["_transcript"] = os.path.expanduser(str(raw.get("_transcript", "")).strip())
        btns = raw.get("buttons", [])
        if isinstance(btns, list):
            for b in btns:
                if isinstance(b, dict) and b.get("label"):
                    info["buttons"].append({
                        "label": str(b["label"]),
                        "run": str(b["run"]) if b.get("run") else "",
                        "copy": str(b["copy"]) if b.get("copy") else "",
                        "confirm": str(b["confirm"]) if b.get("confirm") else "",
                    })
    except Exception:
        pass
    return info


def run_command(path, cmd):
    """在项目目录弹出终端运行命令 (cmd /k 保留窗口看输出)。"""
    if not cmd:
        return
    try:
        subprocess.Popen(["cmd", "/k", cmd], cwd=path if os.path.isdir(path) else None,
                         creationflags=0x00000010)   # CREATE_NEW_CONSOLE
    except Exception:
        pass


def set_clipboard(text):
    """把文字复制到剪贴板 (CF_UNICODETEXT)。"""
    if not text:
        return
    k32 = ctypes.windll.kernel32
    u = ctypes.windll.user32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    try:
        if not u.OpenClipboard(None):
            return
        u.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = k32.GlobalAlloc(0x0002, len(data))     # GMEM_MOVEABLE
        ptr = k32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(h)
        u.SetClipboardData(13, h)                  # CF_UNICODETEXT
    except Exception:
        pass
    finally:
        try:
            u.CloseClipboard()
        except Exception:
            pass


def project_name(path):
    return os.path.basename(path.rstrip("\\/")) or path


def gather():
    out = []
    for p in get_open_projects():
        out.append((p, read_status(p)))
    return out


# ---------------------------------------------------------------------------
# 渲染 (Pillow) —— 返回 RGBA 图
# ---------------------------------------------------------------------------
class Renderer:
    def __init__(self, ui_scale):
        self.ui = ui_scale
        self.sc = ui_scale * S
        self.name_lat = ImageFont.truetype(LAT_NAME, round(NAME_PT * self.sc))
        self.name_cjk = ImageFont.truetype(CJK_NAME, round(NAME_PT * self.sc))
        self.task_lat = ImageFont.truetype(LAT_TASK, round(TASK_PT * self.sc))
        self.task_cjk = ImageFont.truetype(CJK_TASK, round(TASK_PT * self.sc))
        self.btn_lat = ImageFont.truetype(LAT_TASK, round(BTN_PT * self.sc))
        self.btn_cjk = ImageFont.truetype(CJK_TASK, round(BTN_PT * self.sc))
        # UpTime 计时器 / 备注 字体
        self.title_lat = ImageFont.truetype(LAT_NAME, round(TITLE_PT * self.sc))
        self.title_cjk = ImageFont.truetype(CJK_NAME, round(TITLE_PT * self.sc))
        self.timer_font = ImageFont.truetype(LAT_NAME, round(TIMER_PT * self.sc))
        self.stat_lat = ImageFont.truetype(LAT_TASK, round(STAT_PT * self.sc))
        self.stat_cjk = ImageFont.truetype(CJK_TASK, round(STAT_PT * self.sc))
        self.note_lat = ImageFont.truetype(LAT_TASK, round(NOTE_PT * self.sc))
        self.note_cjk = ImageFont.truetype(CJK_TASK, round(NOTE_PT * self.sc))
        self.upbtn_lat = ImageFont.truetype(LAT_NAME, round(UPBTN_PT * self.sc))
        self.upbtn_cjk = ImageFont.truetype(CJK_NAME, round(UPBTN_PT * self.sc))

    def _btn_w(self, label):
        """按钮逻辑宽度 = 标签宽 + 左右内边距。"""
        return self._mwidth(label, self.btn_lat, self.btn_cjk) / self.sc + 2 * BTN_PAD_X

    def _zone_w(self, buttons):
        if not buttons:
            return 0
        w = BTN_ZONE_PAD
        for b in buttons:
            w += self._btn_w(b["label"]) + BTN_GAP
        return w - BTN_GAP + BTN_ZONE_RPAD

    def _card_w(self, infos):
        return CARD_W + max([self._zone_w(i.get("buttons", [])) for _, i in infos],
                            default=0)

    # 物理尺寸 (用于窗口/命中)
    def phys_size(self, infos, up, settings=None):
        cw = self._card_w(infos)
        upm = self._up_metrics(cw, up)
        n = len(infos)
        total = MARGIN
        if n:
            total += n * CARD_H + (n - 1) * VGAP + VGAP
        total += upm["h"]
        if settings and settings.get("open"):
            total += VGAP + SET_PANEL_H
        total += MARGIN
        w = round((cw + 2 * MARGIN) * self.ui)
        h = round(total * self.ui)
        return w, h

    def render_strip(self, size, status):
        """收起后贴边的那条细边: 一颗拉长的灯, 颜色 = 最紧急的状态。
        贴边那一侧的圆角会被屏幕边缘裁掉, 看上去就是从边上探出来的半颗胶囊。"""
        w, h = size
        img = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
        col = dict(LAMPS).get(status, C_RED)
        r = min(w, h) * S / 2.0
        ImageDraw.Draw(img).rounded_rectangle(
            [0, 0, w * S - 1, h * S - 1], radius=r, fill=col + (STRIP_ALPHA,))
        return img.resize((w, h), Image.LANCZOS)

    # ---- UpTime 卡片布局 / 度量 ----
    def _wrap(self, text, lat, cjk, maxw_logical):
        """按宽度逐字折行 (中英混排, 保留原有换行)。"""
        maxw = maxw_logical * self.sc
        out = []
        for para in text.split("\n"):
            cur, curw = "", 0.0
            for c in para:
                f = cjk if _is_cjk(c) else lat
                w = f.getlength(c)
                if cur and curw + w > maxw:
                    out.append(cur)
                    cur, curw = "", 0.0
                cur += c
                curw += w
            out.append(cur)
        return out

    def _up_metrics(self, cw, up):
        """返回 UpTime 卡片高度及备注折行结果 (供 phys_size 与 render 共用)。"""
        if up.get("collapsed"):
            return {"h": UP_SHUT}
        notes = up.get("notes", "") or ""
        lines = self._wrap(notes, self.note_lat, self.note_cjk,
                           cw - 2 * UP_PAD - 2 * UP_NOTE_PAD)
        shown = len(lines) if notes.strip() else 1
        shown = max(1, min(shown, UP_MAX_LINES))
        panel_h = shown * UP_NOTE_LH + 2 * UP_NOTE_PAD
        return {"h": UP_NOTE_TOP + panel_h + UP_NOTE_BOT,
                "lines": lines, "shown": shown, "panel_h": panel_h}

    def _compose_title(self, up):
        if up.get("collapsed") and up.get("running"):
            return "%s   %s" % (TITLE, up.get("timer_text", ""))
        return TITLE

    def _button_layout(self, buttons, card_x, card_y):
        """单行排列, 按标签自适应宽度。返回 (b, lx, ly, lw, lh) 逻辑矩形。"""
        rects = []
        bx = card_x + CARD_W + BTN_ZONE_PAD
        my = card_y + (CARD_H - BTN_H) / 2
        for b in buttons:
            lw = self._btn_w(b["label"])
            rects.append((b, bx, my, lw, BTN_H))
            bx += lw + BTN_GAP
        return rects

    def _mwidth(self, text, lat, cjk):
        return sum((cjk if _is_cjk(c) else lat).getlength(c) for c in text)

    def _fit(self, text, lat, cjk):
        maxw = TEXT_W * self.sc
        if self._mwidth(text, lat, cjk) <= maxw:
            return text
        while text and self._mwidth(text + "…", lat, cjk) > maxw:
            text = text[:-1]
        return text + "…"

    def _mtext(self, d, x, y, text, lat, cjk, fill):
        """逐字符按中英文选字体绘制 (英文 Segoe UI / 中文 雅黑)。"""
        cx = x
        for c in text:
            f = cjk if _is_cjk(c) else lat
            d.text((cx, y), c, font=f, fill=fill + (255,), anchor="lm")
            cx += f.getlength(c)

    def render(self, infos, up, settings=None):
        pw, ph = self.phys_size(infos, up, settings)
        W, H = pw * S, ph * S
        sc = self.sc
        cw = self._card_w(infos)
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        lamp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))   # 文字
        db = ImageDraw.Draw(base)
        dl = ImageDraw.Draw(lamp)
        dtw = ImageDraw.Draw(txt)

        active_glows = []
        hit_rects = []     # (rx, ry, rw, rh, button) 物理像素
        ctl_rects = []     # (rx, ry, rw, rh, kind)   物理像素: caret / timer / notes
        n = len(infos)
        for idx, (path, info) in enumerate(infos):
            cy_l = MARGIN + idx * (CARD_H + VGAP)       # 卡片顶 (逻辑)
            y = cy_l * sc
            x = MARGIN * sc
            self._card(db, x, y, cw, sc)
            cy = y + CARD_H * sc / 2
            for i, (key, color) in enumerate(LAMPS):
                cx = x + (PAD_L + LAMP_R + i * (LAMP_R * 2 + GAP)) * sc
                act = (info["status"] == key)
                self._lamp(dl, cx, cy, color, act, sc)
                if act:
                    active_glows.append((cx, cy, color))
            # 文字
            name = project_name(path) if path else "没有打开的项目"
            fallback = {
                "green": name + "处理中",
                "yellow": name + "待确认",
                "red": name + "已完成",
            }.get(info["status"], name)
            task = compact_task(
                info.get("summary") or info["task"] or LABELS.get(info["status"], ""),
                fallback=fallback,
            )
            tx = x + TEXT_X * sc
            self._mtext(dtw, tx, y + CARD_H * 0.33 * sc,
                        self._fit(name, self.name_lat, self.name_cjk),
                        self.name_lat, self.name_cjk, (33, 36, 42))
            self._mtext(dtw, tx, y + CARD_H * 0.67 * sc,
                        self._fit(task, self.task_lat, self.task_cjk),
                        self.task_lat, self.task_cjk, (33, 36, 42))
            # 右侧按钮
            for b, lx, ly, lw, lh in self._button_layout(info.get("buttons", []), MARGIN, cy_l):
                self._draw_button(db, dtw, lx, ly, lw, lh, b["label"], sc)
                hit_rects.append((lx * self.ui, ly * self.ui, lw * self.ui, lh * self.ui, b))

        # ---- UpTime 计时器 + 备注 卡片 (紧跟项目卡片下方, 同宽) ----
        up_y_l = MARGIN + (n * CARD_H + (n - 1) * VGAP + VGAP if n else 0)
        self._draw_up(db, dtw, MARGIN, up_y_l, cw, sc, up, ctl_rects)

        # 透明度只淡化背景玻璃(base: 卡片/UpTime); 文字与灯在各自图层保持清晰。
        # 设置面板与手柄随后再画(不受透明度影响), 方便随时看清并调节。
        op = settings.get("opacity", 1.0) if settings else 1.0
        if op < 0.999:
            base.putalpha(base.split()[3].point(lambda v: int(v * op)))

        # ---- 设置面板 (悬停右下角手柄弹出) + 右下角调节手柄 ----
        sliders = {}
        if settings and settings.get("open"):
            upm = self._up_metrics(cw, up)
            set_y_l = up_y_l + upm["h"] + VGAP
            sliders = self._draw_settings(db, dtw, MARGIN, set_y_l, cw, sc, settings, ctl_rects)
        self._draw_gear(db, W, H, sc, bool(settings and settings.get("open")))

        # 本色柔光: 只模糊 alpha 蒙版, RGB 始终纯色 -> 不会有黑色光晕。
        # 拖动滑块时跳过高斯柔光(最耗时) + 用双线性快速缩放, 让拖动实时不卡; 松手后恢复高清。
        drag = bool(settings and settings.get("dragging"))
        if drag:
            out = base
        else:
            out = Image.alpha_composite(base, self._build_glow(active_glows, W, H, sc))
        out = Image.alpha_composite(out, lamp)
        out = Image.alpha_composite(out, txt)
        resample = Image.BILINEAR if drag else Image.LANCZOS
        return out.resize((W // S, H // S), resample), hit_rects, ctl_rects, sliders

    def _draw_up(self, db, dtw, x_l, y_l, cw, sc, up, ctl_rects):
        upm = self._up_metrics(cw, up)
        ui = self.ui
        x, y = x_l * sc, y_l * sc
        self._card(db, x, y, cw, sc, h=upm["h"])

        # 标题 (折叠且计时中时附带时间)
        self._mtext(dtw, x + UP_PAD * sc, y + UP_TITLE_CY * sc,
                    self._compose_title(up), self.title_lat, self.title_cjk, C_TITLE)
        # 折叠箭头 (右上) —— 用图元画三角, 避免字体缺 ▸/▼ 字形显示成方块
        ccx = x + cw * sc - UP_PAD * sc - 5 * sc
        ccy = y + UP_TITLE_CY * sc
        t = 4.2 * sc
        if up.get("collapsed"):                       # 指向右 (展开箭头)
            tri = [(ccx - t * 0.5, ccy - t), (ccx - t * 0.5, ccy + t), (ccx + t * 0.7, ccy)]
        else:                                          # 指向下 (收起箭头)
            tri = [(ccx - t, ccy - t * 0.5), (ccx + t, ccy - t * 0.5), (ccx, ccy + t * 0.7)]
        dtw.polygon(tri, fill=C_CARET + (255,))
        ctl_rects.append(((x_l + cw - UP_PAD - 26) * ui, y_l * ui,
                          42 * ui, 42 * ui, "caret"))

        if up.get("collapsed"):
            return

        # 大字计时
        dtw.text((x + UP_PAD * sc, y + UP_TIMER_CY * sc), up.get("timer_text", "00:00"),
                 font=self.timer_font, fill=C_TIMER + (255,), anchor="lm")

        # Start / Stop 磨砂胶囊 (右侧, 与红绿灯按钮同风格)
        label = "Stop" if up.get("running") else "Start"
        tw = self._mwidth(label, self.upbtn_lat, self.upbtn_cjk)
        lw = tw / sc + 2 * UP_BTN_PADX
        lx = x_l + cw - UP_PAD - lw
        ly = y_l + UP_TIMER_CY - UP_BTN_H / 2.0
        x0, y0 = lx * sc, ly * sc
        self._round(db, x0, y0, lw * sc, UP_BTN_H * sc, UP_BTN_H * sc / 2,
                    fill=(255, 255, 255, 205), outline=(255, 255, 255, 235),
                    width=max(round(1 * sc), 1))
        self._mtext(dtw, x0 + (lw * sc - tw) / 2, y0 + UP_BTN_H * sc / 2, label,
                    self.upbtn_lat, self.upbtn_cjk, (44, 48, 56))
        ctl_rects.append((lx * ui, ly * ui, lw * ui, UP_BTN_H * ui, "timer"))

        # 统计副标题
        self._mtext(dtw, x + UP_PAD * sc, y + UP_STAT_CY * sc,
                    up.get("stat", ""), self.stat_lat, self.stat_cjk, C_STAT)

        # 备注面板
        px1, py1 = x_l + UP_PAD, y_l + UP_NOTE_TOP
        px2, py2 = x_l + cw - UP_PAD, y_l + UP_NOTE_TOP + upm["panel_h"]
        self._round(db, px1 * sc, py1 * sc, (px2 - px1) * sc, (py2 - py1) * sc,
                    14 * sc, fill=NOTE_FILL, outline=(255, 255, 255, 235),
                    width=max(round(1 * sc), 1))
        ctl_rects.append((px1 * ui, py1 * ui, (px2 - px1) * ui, (py2 - py1) * ui, "notes"))

        # 备注文字 (逐行; 超出 UP_MAX_LINES 的在编辑框里滚动)
        tx = (px1 + UP_NOTE_PAD) * sc
        ty0 = (py1 + UP_NOTE_PAD) * sc
        notes = up.get("notes", "") or ""
        if notes.strip():
            for i, ln in enumerate(upm["lines"][:upm["shown"]]):
                self._mtext(dtw, tx, ty0 + (i + 0.5) * UP_NOTE_LH * sc, ln,
                            self.note_lat, self.note_cjk, C_NOTE)
        else:
            self._mtext(dtw, tx, ty0 + 0.5 * UP_NOTE_LH * sc, "随手记点什么…",
                        self.note_lat, self.note_cjk, C_CARET)

    def _draw_settings(self, db, dtw, x_l, y_l, cw, sc, settings, ctl_rects):
        """底部设置卡片: 透明度 + 尺寸 两个滑块 + 一排常用值预设胶囊。
        返回 {kind:(x0,x1,cy,vmin,vmax)}(ui 坐标); 预设命中区写进 ctl_rects (kind='preset%d')。"""
        ui = self.ui
        self._card(db, x_l * sc, y_l * sc, cw, sc, h=SET_PANEL_H)
        rows = [("透明度", "op", settings.get("opacity", 1.0), OPA_MIN, OPA_MAX),
                ("尺寸", "sz", settings.get("scale", 1.0), SCL_MIN, SCL_MAX)]
        pad, label_w, val_w = 14, 42, 42
        tx0 = x_l + pad + label_w
        tx1 = x_l + cw - pad - val_w
        sliders = {}
        for i, (label, kind, val, vmin, vmax) in enumerate(rows):
            cy_l = y_l + 16 + i * SET_ROW_H
            self._mtext(dtw, (x_l + pad) * sc, cy_l * sc, label,
                        self.stat_lat, self.stat_cjk, C_TITLE)
            th = SET_TRACK_H / 2.0
            # 轨道底: 半透明冷灰玻璃
            self._round(db, tx0 * sc, (cy_l - th) * sc, (tx1 - tx0) * sc, SET_TRACK_H * sc,
                        th * sc, fill=(120, 132, 150, 90))
            t = min(max((val - vmin) / (vmax - vmin), 0.0), 1.0)
            kx = tx0 + t * (tx1 - tx0)
            if kx - tx0 > 2:                          # 已填充: 半透明白玻璃 + 顶部亮沿(玻璃高光, 不用绿)
                self._round(db, tx0 * sc, (cy_l - th) * sc, (kx - tx0) * sc, SET_TRACK_H * sc,
                            th * sc, fill=(244, 247, 250, 150))
                self._round(db, tx0 * sc, (cy_l - th) * sc, (kx - tx0) * sc, (SET_TRACK_H * 0.44) * sc,
                            th * sc, fill=(255, 255, 255, 120))
            kr = SET_KNOB_R
            db.ellipse([(kx - kr - 1.0) * sc, (cy_l - kr - 1.0) * sc,      # 极淡深度圈(仅够和白填充区分)
                        (kx + kr + 1.0) * sc, (cy_l + kr + 1.0) * sc], fill=(80, 90, 110, 42))
            db.ellipse([(kx - kr) * sc, (cy_l - kr) * sc, (kx + kr) * sc, (cy_l + kr) * sc],
                       fill=(255, 255, 255, 205), outline=(255, 255, 255, 235),  # 淡白玻璃+白描边(同Start按钮/标准胶囊)
                       width=max(round(1.4 * sc), 1))
            self._mtext(dtw, (tx1 + 9) * sc, cy_l * sc, "%d%%" % round(val * 100),
                        self.stat_lat, self.stat_cjk, C_STAT)
            sliders[kind] = (tx0 * ui, tx1 * ui, cy_l * ui, vmin, vmax)

        # ---- 底部一排"常用值"预设胶囊 (以透明为主, 命中当前值的那颗实白高亮) ----
        presets = settings.get("presets") or DEFAULT_PRESETS
        cur_op = settings.get("opacity", 1.0)
        cur_sz = settings.get("scale", 1.0)
        cn = len(SET_CHIP_LABELS)
        cgap, cpad = 8, 16
        chw = (cw - 2 * cpad - cgap * (cn - 1)) / cn
        cy_c = y_l + SET_CHIP_CY
        chr_ = SET_CHIP_H / 2
        for i, label in enumerate(SET_CHIP_LABELS):
            cx0 = x_l + cpad + i * (chw + cgap)
            cy0 = cy_c - chr_
            pr = presets[i] if i < len(presets) else DEFAULT_PRESETS[i]
            active = (abs(float(pr.get("op", 1.0)) - cur_op) < 0.02 and
                      abs(float(pr.get("sz", 1.0)) - cur_sz) < 0.02)
            if active:                               # 命中当前值: 亮白玻璃 + 亮边 (选中态)
                self._round(db, cx0 * sc, cy0 * sc, chw * sc, SET_CHIP_H * sc, chr_ * sc,
                            fill=(255, 255, 255, 236), outline=(255, 255, 255, 248),
                            width=max(round(1 * sc), 1))
                tcol = (38, 42, 50)
            else:                                    # 普通: 磨砂玻璃 (够实=字始终清楚, 仍带透明感)
                self._round(db, cx0 * sc, cy0 * sc, chw * sc, SET_CHIP_H * sc, chr_ * sc,
                            fill=(255, 255, 255, 170), outline=(255, 255, 255, 205),
                            width=max(round(1 * sc), 1))
                tcol = (58, 64, 74)
            tw = self._mwidth(label, self.btn_lat, self.btn_cjk)
            self._mtext(dtw, cx0 * sc + (chw * sc - tw) / 2, cy_c * sc, label,
                        self.btn_lat, self.btn_cjk, tcol)
            ctl_rects.append((cx0 * ui, cy0 * ui, chw * ui, SET_CHIP_H * ui, "preset%d" % i))
        return sliders

    def _draw_gear(self, db, W, H, sc, active):
        """右下角极简"三横线"调节手柄 (平时淡, 展开时略实); 纯图元, 不依赖字体字形。"""
        gx1 = W - 8 * sc
        gx0 = gx1 - 13 * sc
        cyc = H - 12 * sc
        a = 215 if active else 140
        for k in (-1, 0, 1):
            yy = cyc + k * 4 * sc
            self._round(db, gx0, yy - 1.1 * sc, (gx1 - gx0), 2.2 * sc, 1.1 * sc,
                        fill=(120, 130, 145, a))

    def _build_glow(self, lamps, W, H, sc):
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if not lamps:
            return glow
        gr = (LAMP_R + 7) * sc
        blur = ImageFilter.GaussianBlur(radius=5 * sc)
        for cx, cy, color in lamps:
            mask = Image.new("L", (W, H), 0)
            ImageDraw.Draw(mask).ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=205)
            mask = mask.filter(blur)
            layer = Image.new("RGBA", (W, H), color + (0,))
            layer.putalpha(mask)
            glow = Image.alpha_composite(glow, layer)
        return glow

    def _round(self, d, x, y, w, h, r, **kw):
        d.rounded_rectangle([x, y, x + w, y + h], radius=r, **kw)

    def _card(self, d, x, y, cw, sc, h=None):
        hh = (CARD_H if h is None else h) * sc
        w, r = cw * sc, RADIUS * sc
        # 磨砂玻璃底 (浅色半透明, 整张统一不透明度) + 白边
        self._round(d, x, y, w, hh, r, fill=(228, 232, 240, 188),
                    outline=(255, 255, 255, 235), width=round(1.6 * sc))

    def _draw_button(self, db, dtw, lx, ly, lw, lh, label, sc):
        x0, y0, w, h = lx * sc, ly * sc, lw * sc, lh * sc
        # 比按钮原先更透, 但仍比底板(alpha 188)更实
        self._round(db, x0, y0, w, h, h / 2, fill=(255, 255, 255, 205),
                    outline=(255, 255, 255, 235), width=max(round(1 * sc), 1))
        tw = self._mwidth(label, self.btn_lat, self.btn_cjk)
        self._mtext(dtw, x0 + (w - tw) / 2, y0 + h / 2, label,
                    self.btn_lat, self.btn_cjk, (44, 48, 56))

    def _lamp(self, dl, cx, cy, color, active, sc):
        """白色圆环 + 本色填充 + 白色中心点 (参考图样式)。
        亮: 本色填充更实, 并由 _build_glow 发本色光; 灭: 淡本色, 不发光。"""
        r = LAMP_R * sc
        ring = max(round(1.2 * sc), 1)
        if active:
            disc = lerp(color, (255, 255, 255), 0.3)          # 本色填充
            dl.ellipse([cx - r, cy - r, cx + r, cy + r], fill=disc + (255,),
                       outline=(255, 255, 255, 255), width=ring)
            pr = r * 0.3                                       # 白色中心点
            dl.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=(255, 255, 255, 255))
        else:
            disc = lerp(color, (255, 255, 255), 0.58)         # 更淡的本色
            dl.ellipse([cx - r, cy - r, cx + r, cy + r], fill=disc + (235,),
                       outline=(255, 255, 255, 210), width=max(round(1 * sc), 1))
            pr = r * 0.28
            dl.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=(255, 255, 255, 225))


# ---------------------------------------------------------------------------
# Win32 分层窗口
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

ULW_ALPHA = 0x02
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
SW_SHOWNA = 8
WM_DESTROY, WM_TIMER = 0x0002, 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN = 0x0201, 0x0202, 0x0204
MK_LBUTTON = 0x0001
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010

# 托盘图标 (右键退出 —— 否则这东西只能去任务管理器杀, 发给别人用说不过去)
WM_TRAY = 0x8000 + 2           # 自定义: 托盘回调
WM_COMMAND = 0x0111
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
IDM_SHOW, IDM_EXIT = 1001, 1002

# 备注就地编辑 (临时置顶 EDIT 弹窗)
WM_SETFONT = 0x0030
WM_KILLFOCUS = 0x0008
WM_KEYDOWN = 0x0100
WM_APP_COMMIT = 0x8000 + 1     # 自定义: 提交并关闭备注编辑框
EM_SETSEL = 0x00B1
GWLP_WNDPROC = -4
VK_ESCAPE = 0x1B
WS_VISIBLE = 0x10000000
WS_BORDER, WS_VSCROLL = 0x00800000, 0x00200000
ES_MULTILINE, ES_AUTOVSCROLL, ES_WANTRETURN = 0x0004, 0x0040, 0x1000


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                ("uCallbackMessage", ctypes.c_uint), ("hIcon", wintypes.HICON),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", ctypes.c_wchar * 256), ("uVersion", ctypes.c_uint),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


LRESULT = ctypes.c_ssize_t
WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                                 ctypes.c_size_t, ctypes.c_ssize_t)

# 返回值/参数类型 (64 位必需, 否则指针被截断)
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.SendMessageW.restype = LRESULT
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT),
    ctypes.POINTER(SIZE), wintypes.HDC, ctypes.POINTER(POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetCapture.restype = wintypes.HWND
user32.SetCapture.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
# 托盘
shell32 = ctypes.windll.shell32
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyIcon.argtypes = [wintypes.HICON]
# 贴边收起: 取窗口所在显示器的工作区 (排除任务栏, 所以贴底边不会被任务栏盖住)
MONITOR_DEFAULTTONEAREST = 2
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
# 窗口枚举/切前台 (点击卡片聚焦对应 VSCode 窗口)
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.MessageBoxW.restype = ctypes.c_int
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint]
# 备注就地编辑用
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.CallWindowProcW.restype = LRESULT
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                   ctypes.c_size_t, ctypes.c_ssize_t]
_SetWLP = getattr(user32, "SetWindowLongPtrW", getattr(user32, "SetWindowLongW", None))
_SetWLP.restype = ctypes.c_void_p
_SetWLP.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
gdi32.CreateFontW.restype = wintypes.HGDIOBJ
gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]


def premultiplied_bgra(img):
    """RGBA -> 预乘 alpha 的 BGRA 字节 (UpdateLayeredWindow 要求)。"""
    over = Image.alpha_composite(Image.new("RGBA", img.size, (0, 0, 0, 255)), img)
    r, g, b, _ = over.split()
    a = img.split()[3]
    pm = Image.merge("RGBA", (r, g, b, a))
    return pm.tobytes("raw", "BGRA")


class FloatingWidget:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
        try:
            dpi = user32.GetDpiForSystem()
            self.ui = max(dpi / 96.0, 1.0)
        except Exception:
            self.ui = 1.0
        self._dpi_ui = self.ui            # 纯 DPI 缩放; 之后 self.ui = _dpi_ui * user_scale

        self.renderer = Renderer(self.ui)
        self.infos = []
        self.manual = {}
        self.manual_base = {}
        self.opacity = 1.0                # 整窗透明度 (滑块调, 存 ui_data)
        self.user_scale = 1.0             # 整体尺寸倍数 (滑块调, 存 ui_data)
        self.settings_open = False        # 设置面板是否展开 (悬停右下角触发)
        self._slider_drag = None          # 'op' / 'sz' / None
        self._last_srender = 0.0          # 滑块拖动重绘限流时间戳
        self._sliders = {}                # kind -> (x0, x1, cy, vmin, vmax) 命中/拖动几何
        self._down = None        # (sx, sy, win_left, win_top, client_x, client_y)
        self._dragging = False
        self.button_rects = []   # [(rx, ry, rw, rh, button)] 物理像素
        self.ctl_rects = []      # [(rx, ry, rw, rh, kind)]   UpTime 控件命中区
        self._note_editor = None # 备注编辑框 hwnd (None=未打开)
        self._edit_font = None
        self._edit_oldproc = None
        self._poll = 0
        self._tray_icon = None    # 托盘图标 HICON (换色时销毁旧的)
        self._tray_status = None

        # ---- 计时 / 备注 状态 (原 standup.py, 复用同一批文件, 不清空历史) ----
        self.ui_data = load_json(UI_FILE, {})
        self.opacity = min(max(float(self.ui_data.get("opacity", 1.0)), OPA_MIN), OPA_MAX)
        self.user_scale = min(max(float(self.ui_data.get("scale", 1.0)), SCL_MIN), SCL_MAX)
        raw = self.ui_data.get("presets")              # 常用值预设 (可被右键覆盖)
        if not (isinstance(raw, list) and len(raw) == len(SET_CHIP_LABELS)
                and all(isinstance(p, dict) for p in raw)):
            raw = [dict(p) for p in DEFAULT_PRESETS]
        self.presets = raw
        self.ui = self._dpi_ui * self.user_scale       # 应用用户尺寸
        self.renderer = Renderer(self.ui)
        self.tlog = load_json(STANDUP_LOG_FILE, {})
        self.up_notes = _read_text(NOTES_FILE)
        self.collapsed = bool(self.ui_data.get("collapsed", False))
        self.running = False
        self.start_ts = None
        rs = self.ui_data.get("running_start")
        if rs and (time.time() - rs) < 8 * 3600 and \
           datetime.datetime.fromtimestamp(rs).strftime("%Y-%m-%d") == today_key():
            self.running = True
            self.start_ts = rs
        elif rs:
            self.ui_data["running_start"] = None
        self.pos = _clamp_onscreen(self.ui_data.get("x", 80),
                                   self.ui_data.get("y", 80), 300, 480)
        # 贴边收起: dock = 停靠的边 (None=没贴边); peeked = 当前是否滑出来了。
        # 重启后一律以"收起"状态回来 —— 贴过边的人要的就是桌面干净。
        dk = self.ui_data.get("dock")
        self.dock = dk if dk in ("left", "right", "top", "bottom") else None
        self.peeked = False

        # 首次运行把 hook 接进 Claude Code (会弹一次框征求同意); 已接过则静默自愈过时的路径。
        try:
            import onboard
            onboard.run(self.ui_data, lambda: save_json(UI_FILE, self.ui_data))
        except Exception as e:
            log("onboard: " + repr(e))               # 接入失败不该拖垮悬浮窗本身

        self._wndproc = WNDPROCTYPE(self._on_msg)
        self._edit_wndproc = WNDPROCTYPE(self._edit_proc)
        self.hwnd = self._make_window()
        self._gather_infos()
        self._render_now()
        self._tray_status = self._worst_status()
        self._tray(NIM_ADD, self._tray_status)
        user32.SetTimer(self.hwnd, 1, TICK_MS, None)
        user32.SetTimer(self.hwnd, 2, 150, None)       # 悬停轮询: 右下角触发设置面板
        self._loop()

    def _make_window(self):
        hInst = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        cls.hInstance = hInst
        cls.lpszClassName = "AITrafficLightWnd"
        cls.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        user32.RegisterClassW(ctypes.byref(cls))

        w, h = self.renderer.phys_size([], self._up_state())
        exstyle = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        hwnd = user32.CreateWindowExW(exstyle, "AITrafficLightWnd", "AI红绿灯",
            WS_POPUP, self.pos[0], self.pos[1], w, h, None, None, hInst, None)
        user32.ShowWindow(hwnd, SW_SHOWNA)
        return hwnd

    # ---- UpTime 状态 / 统计 ----
    def _up_state(self):
        t = fmt_hms(time.time() - self.start_ts) if (self.running and self.start_ts) else "00:00"
        return {"collapsed": self.collapsed, "running": self.running,
                "timer_text": t, "stat": self._stat_text(), "notes": self.up_notes}

    def _stat_text(self):
        day = self.tlog.get(today_key(), [])
        secs = sum(s.get("sec", 0) for s in day)
        cnt = len(day)
        if self.running and self.start_ts:
            secs += int(time.time() - self.start_ts)
            cnt += 1
        return "Standing  %s · %d×" % (fmt_dur(secs), cnt)

    # ---- 数据采集 / 渲染 ----
    def _gather_infos(self):
        codex = codex_active()                  # (cwd, 标题) 或 None
        infos = []
        for p, info in gather():
            if p in self.manual and self.manual_base.get(p) != info.get("status"):
                self.manual.pop(p, None)
                self.manual_base.pop(p, None)
            if p in self.manual:                # 手动固定的灯: 直接采用
                info = dict(info)
                info["status"] = self.manual[p]
            elif codex and same_tree(p, codex[0]):
                # Codex 正在这棵树上跑 = 绿, 与文件里残留的 red/yellow 无关。
                # codex_active() 已保证 rollout 在 CODEX_FRESH_SEC 内动过(= 真的在干活),
                # 所以不能再要求"文件本身是绿"—— 否则某项目上一次是 Claude 干的、Stop 写了红,
                # 之后换 Codex 来跑, 灯就永远卡在红。Codex 无钩子写文件, 只能靠心跳。
                info = dict(info)
                info["status"] = "green"
                # 用 Codex 会话的智能标题(thread_name)当卡片文字 —— 它描述 Codex 正在干的活。
                # 绝不回退到 info["summary"]: Codex 没钩子写 .ai-status.json, 那字段永远是上一个
                # Claude 会话残留的旧摘要(比如 "继续已完成"), 拿来当 Codex 的活是错的。
                info["task"] = codex[1] or "Codex 运行中"
            else:
                # 钩子写的灯色只是"上一个事件说了什么", 真正现在在不在干活, 看 transcript 尾部谁该动。
                old = info.get("status")
                tpath = info.get("_transcript")
                state = transcript_state(tpath)
                age = _transcript_age(tpath)
                new = old
                if state in ("ask", "error"):
                    # AI 停下来了, 而且不是它自己能解决的: 在等你回答 / 崩在半路。
                    # 这两种都不发 Notification 钩子, 指望钩子必漏 -> 只认 transcript 尾部。
                    new = "yellow"
                elif state == "interrupted":
                    new = "red"                       # 按了 Esc: 你自己叫停的, 不用提醒你 -> 红
                elif old == "green":
                    if state in ("tool", "gen"):
                        # 尾部说球在 AI 手里(工具在跑 / 正在生成) -> 绿, 不管 transcript 多久没写:
                        # 长命令期间它本来就不写。久到像会话被杀(半小时)= 进程崩了/窗口被关, 卡在半截
                        # 再也不会动了 -> 黄(要你去看一眼), 不是红: 它没干完, 判"已完成"是撒谎。
                        new = "yellow" if age >= CLAUDE_BUSY_MAX_SEC else "green"
                    elif state == "idle":
                        # 尾部说回合已结束, 文件却还绿 = Stop 钩子没响的僵尸绿 -> 落红(留一点缝隙余量)。
                        new = "red" if age >= CLAUDE_IDLE_GRACE_SEC else "green"
                    else:                             # unknown: 尾部读不出来, 只剩 mtime 可信
                        new = "red" if age >= CLAUDE_STALE_SEC else "green"
                elif old == "yellow":
                    # 黄 = Notification 写的(要你批权限/做抉择)。你批准之后不触发任何钩子, 文件还是黄,
                    # 只能靠 transcript 尾部解除: 尾部还是那条没回结果的 tool_use = 还堵在你这儿, 保持黄;
                    # 一旦工具结果回来了(尾部变成 user 记录) = 你已经批了、AI 又在跑 -> 绿。
                    # 这是"尾部变没变"的单调判断, 不是时间窗 —— 旧的 5 秒心跳窗只要被 3 秒轮询错过一次,
                    # 黄灯就再也翻不回来, 一直黄到回合结束(这就是黄灯显得特别久的原因)。
                    if state == "gen":
                        new = "green"
                    elif state == "idle":
                        new = "red"
                    # tool / unknown -> 保持黄(真的还在等你)
                # old == "red": Stop/SessionEnd 写的 = 回合结束, 认它
                if new != old:
                    info = dict(info)
                    info["status"] = new
                    core = retint_suffix(
                        info.get("summary") or info.get("task") or info.get("_topic") or "", new)
                    if state == "interrupted" and core.endswith("已完成"):
                        core = core[:-3] + "已中断"   # 中断≠完成, 用"已中断"更贴切
                    elif state == "error" and core.endswith("待确认"):
                        core = core[:-3] + "已报错"   # 崩了≠等你拍板, 说清是"它挂了, 你去看看"
                    info["summary"] = info["task"] = core
            infos.append((p, info))
        self.infos = infos

    # ---- 托盘图标 (右键退出; 图标颜色 = 最紧急的灯, 窗口收起了也能一眼看出有没有事) ----
    def _tray_hicon(self, status):
        """现画一颗灯当图标: 状态色实心圆 + 深色描边(浅色任务栏上也看得清)。"""
        n, sc = 32, 4
        img = Image.new("RGBA", (n * sc, n * sc), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        col = dict(LAMPS).get(status, C_RED)
        pad = 2 * sc
        d.ellipse([pad, pad, n * sc - pad, n * sc - pad],
                  fill=col + (255,), outline=(20, 22, 28, 200), width=sc)
        d.ellipse([n * sc * 0.40, n * sc * 0.40, n * sc * 0.60, n * sc * 0.60],
                  fill=(255, 255, 255, 235))                # 中心亮点, 和卡片上的灯一个样式
        img = img.resize((n, n), Image.LANCZOS)
        path = os.path.join(HERE, "_tray.ico")
        img.save(path, sizes=[(16, 16), (32, 32)])
        return user32.LoadImageW(None, path, IMAGE_ICON, 0, 0,
                                 LR_LOADFROMFILE | LR_DEFAULTSIZE)

    def _tray(self, action, status=None):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        if action == NIM_DELETE:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            return
        old = self._tray_icon
        self._tray_icon = self._tray_hicon(status)
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._tray_icon
        nid.szTip = "AI 红绿灯 · %s" % {"yellow": "有项目在等你处理",
                                        "green": "有项目在跑",
                                        "red": "都空闲了"}.get(status, "")
        shell32.Shell_NotifyIconW(action, ctypes.byref(nid))
        if old:
            user32.DestroyIcon(old)                          # 换了图标就把旧的销毁, 免得句柄泄漏

    def _tray_sync(self):
        """灯色变了才重画托盘图标 —— 每秒重画既费事又会让托盘闪。"""
        st = self._worst_status()
        if st != self._tray_status:
            self._tray_status = st
            self._tray(NIM_MODIFY, st)

    def _tray_menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, IDM_SHOW, "显示悬浮窗")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_EXIT, "退出")
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)                # 不抢前台的话, 菜单会点不掉
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)
        if cmd == IDM_EXIT:
            self._quit()
        elif cmd == IDM_SHOW:
            self._reveal()

    def _reveal(self):
        """把窗口弄到看得见的地方: 贴边收着就滑出来, 否则拉回屏内并置顶。"""
        if self.dock and not self.peeked:
            self._peek_out()
            return
        self.pos = _clamp_onscreen(self.pos[0], self.pos[1], 300, 480)
        user32.SetWindowPos(self.hwnd, None, self.pos[0], self.pos[1], 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        self._render_now()

    def _quit(self):
        if self.running:                                     # 正在计时: 先把这段存进历史再走
            self._toggle_timer()
        self._tray(NIM_DELETE)
        user32.DestroyWindow(self.hwnd)

    # ---- 贴边收起 ----
    def _work_area(self):
        """窗口所在显示器的工作区 (排除任务栏)。取不到就退回主屏。"""
        try:
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            hmon = user32.MonitorFromWindow(self.hwnd, MONITOR_DEFAULTTONEAREST)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcWork
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
        gm = user32.GetSystemMetrics
        return 0, 0, gm(0), gm(1)

    def _strip_t(self):
        return max(round(DOCK_STRIP * self.ui), 3)

    def _dock_geom(self):
        """贴边时的三组几何: (滑出后卡片的位置, 细边位置, 细边尺寸)。
        卡片沿边紧贴, 另一轴沿用 self.pos (夹在工作区内)。"""
        wl, wt, wr, wb = self._work_area()
        w, h = self.renderer.phys_size(self.infos, self._up_state(), self._settings_state())
        t = self._strip_t()
        x = min(max(self.pos[0], wl), max(wr - w, wl))
        y = min(max(self.pos[1], wt), max(wb - h, wt))
        if self.dock == "left":
            return (wl, y), (wl, y), (t, h)
        if self.dock == "right":
            return (wr - w, y), (wr - t, y), (t, h)
        if self.dock == "top":
            return (x, wt), (x, wt), (w, t)
        return (x, wb - h), (x, wb - t), (w, t)      # bottom

    def _hidden_pos(self, cx, cy):
        """收起状态下"卡片"该在的位置 (整张推到边外, 只剩细边那么多露在屏内)。
        滑动动画就是在这个位置和 _dock_geom() 的卡片位置之间挪窗口。"""
        wl, wt, wr, wb = self._work_area()
        w, h = self.renderer.phys_size(self.infos, self._up_state(), self._settings_state())
        t = self._strip_t()
        if self.dock == "left":
            return wl - w + t, cy
        if self.dock == "right":
            return wr - t, cy
        if self.dock == "top":
            return cx, wt - h + t
        return cx, wb - t                             # bottom

    def _dock_edge_at(self, rect):
        """窗口离哪条边最近; 近到阈值内(或已越过)就返回那条边, 否则 None。"""
        wl, wt, wr, wb = self._work_area()
        d = {"left": rect.left - wl, "top": rect.top - wt,
             "right": wr - rect.right, "bottom": wb - rect.bottom}
        edge = min(d, key=lambda k: d[k])
        return edge if d[edge] <= max(round(DOCK_SNAP * self.ui), 8) else None

    def _alert(self):
        """有项目在等你处理 (黄灯) —— 收起时它会自己滑出来提醒。"""
        return any(i.get("status") == "yellow" for _, i in self.infos)

    def _worst_status(self):
        """细边取最紧急的那个状态: 有黄就黄, 否则有绿就绿, 全完事才红。"""
        ss = [i.get("status") for _, i in self.infos]
        for s in ("yellow", "green"):
            if s in ss:
                return s
        return "red"

    def _slide(self, x0, y0, x1, y1):
        """滑动: 位图不重画, 只挪窗口 —— 分层窗口的内容跟着走, 几乎不花开销。"""
        for i in range(1, DOCK_STEPS + 1):
            k = 1 - (1 - i / DOCK_STEPS) ** 3         # ease-out
            user32.SetWindowPos(self.hwnd, None,
                                round(x0 + (x1 - x0) * k), round(y0 + (y1 - y0) * k),
                                0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(DOCK_MS / 1000.0 / DOCK_STEPS)

    def _peek_out(self):
        self.peeked = True
        (cx, cy), _, _ = self._dock_geom()
        hx, hy = self._hidden_pos(cx, cy)
        self._render_now(pos=(hx, hy))                # 先在边外画好整张卡片, 再滑进来
        self._slide(hx, hy, cx, cy)

    def _peek_in(self):
        (cx, cy), _, _ = self._dock_geom()
        self._slide(cx, cy, *self._hidden_pos(cx, cy))
        self.peeked = False
        self.settings_open = False
        self._render_now()                            # 换成细边

    def _set_dock(self, edge):
        self.dock = edge
        self.peeked = bool(edge)                      # 刚吸附: 先贴边亮着, 鼠标移开再收
        self.ui_data["dock"] = edge
        self.ui_data["x"], self.ui_data["y"] = self.pos
        save_json(UI_FILE, self.ui_data)
        self._render_now()

    def _render_now(self, pos=None):
        try:
            if self.dock and not self.peeked:         # 收起: 只画细边
                _, spos, ssize = self._dock_geom()
                self.button_rects, self.ctl_rects, self._sliders = [], [], {}
                self._push(self.renderer.render_strip(ssize, self._worst_status()), spos)
                return
            img, self.button_rects, self.ctl_rects, self._sliders = \
                self.renderer.render(self.infos, self._up_state(), self._settings_state())
            if pos is None and self.dock and not self._down:
                pos = self._dock_geom()[0]            # 贴着边站好 (拖动中不干预)
            self._push(img, pos)
        except Exception as e:
            import traceback
            log("render error: " + repr(e) + "\n" + traceback.format_exc())

    def _settings_state(self):
        return {"open": self.settings_open, "opacity": self.opacity,
                "scale": self.user_scale, "dragging": self._slider_drag is not None,
                "presets": self.presets}

    def _push(self, img, pos=None):
        w, h = img.size
        data = premultiplied_bgra(img)
        screen = user32.GetDC(None)
        memdc = gdi32.CreateCompatibleDC(screen)

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h            # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0        # BI_RGB
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(memdc, ctypes.byref(bmi), 0,
                                      ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(memdc, hbmp)
        ctypes.memmove(bits, data, len(data))

        if pos is None:                               # 不指定就原地更新 (UpdateLayeredWindow 会顺带改尺寸)
            rect = RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
            pos = (rect.left, rect.top)
        ptdst = POINT(pos[0], pos[1])
        size = SIZE(w, h)
        ptsrc = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(ptdst),
            ctypes.byref(size), memdc, ctypes.byref(ptsrc), 0,
            ctypes.byref(blend), ULW_ALPHA)

        gdi32.SelectObject(memdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(None, screen)

    # ---- 消息 ----
    def _card_at(self, cy):
        top = MARGIN * self.ui
        step = (CARD_H + VGAP) * self.ui
        ch = CARD_H * self.ui
        for idx, (p, _) in enumerate(self.infos):
            y0 = top + idx * step
            if y0 <= cy <= y0 + ch:
                return p
        return None

    def _client_xy(self, lparam):
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        return (x - 0x10000 if x >= 0x8000 else x,
                y - 0x10000 if y >= 0x8000 else y)

    def _button_at(self, cx, cy):
        for rx, ry, rw, rh, b in self.button_rects:
            if rx <= cx <= rx + rw and ry <= cy <= ry + rh:
                return b
        return None

    def _cursor(self):
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _slider_at(self, cx, cy):
        for kind, (x0, x1, cyc, vmin, vmax) in self._sliders.items():
            if (x0 - SET_KNOB_R * self.ui <= cx <= x1 + SET_KNOB_R * self.ui and
                    abs(cy - cyc) <= (SET_ROW_H / 2) * self.ui):
                return kind
        return None

    def _set_slider(self, kind, cx):
        x0, x1, cyc, vmin, vmax = self._sliders[kind]
        t = (cx - x0) / (x1 - x0) if x1 > x0 else 0.0
        t = min(max(t, 0.0), 1.0)
        val = vmin + t * (vmax - vmin)
        if kind == "op":
            self.opacity = val                       # 实时: 下次 _push 用新 alpha
        else:
            self.user_scale = val                    # 尺寸: 拖动中只更新数值/旋钮, 松手再缩放

    def _apply_scale(self):
        self.user_scale = min(max(self.user_scale, SCL_MIN), SCL_MAX)
        self.ui = self._dpi_ui * self.user_scale
        self.renderer = Renderer(self.ui)            # 按新尺寸重烘焙字体; 下次 _push 自动缩放窗口

    def _apply_preset(self, i):
        """左键点预设胶囊: 一键套用该组常用的 透明度 + 尺寸, 并持久化。"""
        if not (0 <= i < len(self.presets)):
            return
        p = self.presets[i]
        self.opacity = min(max(float(p.get("op", 1.0)), OPA_MIN), OPA_MAX)
        self.user_scale = min(max(float(p.get("sz", 1.0)), SCL_MIN), SCL_MAX)
        self._apply_scale()                          # 重建渲染器 + 下次 _push 缩放窗口
        self.ui_data["opacity"] = round(self.opacity, 3)
        self.ui_data["scale"] = round(self.user_scale, 3)
        save_json(UI_FILE, self.ui_data)
        self._render_now()

    def _save_preset(self, i):
        """右键点预设胶囊: 把当前滑块值存成这颗常用值 (设置我自己的常用值)。"""
        if not (0 <= i < len(self.presets)):
            return
        self.presets[i] = {"op": round(self.opacity, 3), "sz": round(self.user_scale, 3)}
        self.ui_data["presets"] = self.presets
        save_json(UI_FILE, self.ui_data)
        self._render_now()

    def _hover_poll(self):
        """两件事: 贴边时鼠标碰细边(或来了黄灯) -> 滑出, 离开且没黄灯 -> 滑回;
        没贴边时, 右下角小热区 -> 展开设置面板, 移出窗口 -> 收起。拖滑块/拖窗/编辑备注时不动。"""
        if self._slider_drag or self._note_editor or self._down:
            return
        try:
            sx, sy = self._cursor()
            rect = RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        except Exception:
            return
        if self.dock:
            m = DOCK_HOT * self.ui
            near = (rect.left - m <= sx <= rect.right + m and
                    rect.top - m <= sy <= rect.bottom + m)
            want = near or self._alert()             # 黄灯 = 有事找你, 自己滑出来
            if want and not self.peeked:
                self._peek_out()
                return
            if not want and self.peeked:
                self._peek_in()
                return
            if not self.peeked:                      # 收着的细边上没有设置面板
                return
        if self.settings_open:
            m = 6 * self.ui
            inside = (rect.left - m <= sx <= rect.right + m and
                      rect.top - m <= sy <= rect.bottom + m)
            if not inside:
                self.settings_open = False
                self._render_now()
        else:
            hs = SET_GEAR * self.ui
            if (rect.right - hs <= sx <= rect.right and
                    rect.bottom - hs <= sy <= rect.bottom):
                self.settings_open = True
                self._render_now()

    def _on_msg(self, hwnd, msg, wparam, lparam):
        try:
            return self._dispatch(hwnd, msg, wparam, lparam)
        except Exception as e:
            import traceback
            log("msg error: " + repr(e) + "\n" + traceback.format_exc())
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _ctl_at(self, cx, cy):
        for rx, ry, rw, rh, kind in self.ctl_rects:
            if rx <= cx <= rx + rw and ry <= cy <= ry + rh:
                return kind
        return None

    def _dispatch(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_COMMIT:                     # 备注编辑框提交/取消并关闭
            self._close_note_editor(cancel=(wparam == 1))
            return 0
        if msg == WM_LBUTTONDOWN:
            if self._note_editor:                    # 编辑中点玻璃 -> 先提交关闭
                self._close_note_editor(cancel=False)
                return 0
            if self.dock and not self.peeked:        # 手快, 抢在悬停轮询之前点到细边 -> 先滑出来
                self._peek_out()
                return 0
            cxp, cyp = self._client_xy(lparam)
            sk = self._slider_at(cxp, cyp)           # 先判设置滑块
            if sk:
                self._slider_drag = sk
                user32.SetCapture(hwnd)
                self._set_slider(sk, cxp)
                self._render_now()
                return 0
            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            sx, sy = self._cursor()
            self._down = (sx, sy, rect.left, rect.top, cxp, cyp)
            self._dragging = False
            user32.SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE and self._slider_drag and (wparam & MK_LBUTTON):
            cxp, cyp = self._client_xy(lparam)
            self._set_slider(self._slider_drag, cxp)
            now = time.time()
            if now - self._last_srender >= 0.033:    # 拖动重绘限流 ~30fps, 防卡
                self._last_srender = now
                self._render_now()
            return 0
        if msg == WM_MOUSEMOVE and self._down and (wparam & MK_LBUTTON):
            sx, sy = self._cursor()
            dx, dy = sx - self._down[0], sy - self._down[1]
            if not self._dragging and (abs(dx) > 4 or abs(dy) > 4):
                self._dragging = True
            if self._dragging:
                nx, ny = self._down[2] + dx, self._down[3] + dy
                self.pos = (nx, ny)
                user32.SetWindowPos(hwnd, None, nx, ny, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            return 0
        if msg == WM_LBUTTONUP and self._slider_drag:
            user32.ReleaseCapture()
            sk = self._slider_drag
            self._slider_drag = None
            if sk == "sz":                           # 尺寸: 松手才重建渲染器/缩放窗口 (拖动中只动旋钮)
                self._apply_scale()
            self.ui_data["opacity"] = round(self.opacity, 3)
            self.ui_data["scale"] = round(self.user_scale, 3)
            save_json(UI_FILE, self.ui_data)
            self._render_now()
            return 0
        if msg == WM_LBUTTONUP and self._down:
            user32.ReleaseCapture()
            down = self._down
            self._down = None
            if self._dragging:                       # 拖动结束 -> 记住位置; 落在边上就吸附收起
                rect = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                self.pos = (rect.left, rect.top)
                self._set_dock(self._dock_edge_at(rect))
                return 0
            cxp, cyp = down[4], down[5]
            kind = self._ctl_at(cxp, cyp)            # 先判 UpTime 控件
            if kind == "caret":
                self._toggle_collapse()
            elif kind == "timer":
                self._toggle_timer()
            elif kind == "notes":
                self._open_note_editor()
            elif kind and kind.startswith("preset"):
                self._apply_preset(int(kind[6:]))
            else:
                b = self._button_at(cxp, cyp)
                if b:                                # 点到按钮 -> 执行指令
                    ok = True
                    if b.get("confirm"):             # 执行前弹强条提醒确认
                        res = user32.MessageBoxW(hwnd, b["confirm"],
                                                 "执行前确认 · 强条", 0x00040031)
                        ok = (res == 1)              # IDOK
                    if ok:
                        if b.get("copy"):
                            set_clipboard(b["copy"])
                        elif b.get("run"):
                            run_command(self._card_at(cyp), b["run"])
                else:                                # 点到卡片空白 -> 打开 VSCode
                    p = self._card_at(cyp)
                    if p:
                        open_in_vscode(p)
            return 0
        if msg == WM_RBUTTONDOWN:
            cxp, cyp = self._client_xy(lparam)
            kind = self._ctl_at(cxp, cyp)            # 右键预设胶囊 -> 存当前值为常用值
            if kind and kind.startswith("preset"):
                self._save_preset(int(kind[6:]))
                return 0
            p = self._card_at(cyp)
            if p:
                order = ["green", "yellow", "red"]
                base = read_status(p)["status"]
                cur = self.manual.get(p)
                if not cur:
                    cur = base
                self.manual[p] = order[(order.index(cur) + 1) % 3] if cur in order else "green"
                self.manual_base[p] = base
                self._gather_infos()
                self._render_now()
            return 0
        if msg == WM_TRAY:                           # 托盘: 左键=显示, 右键=菜单
            low = lparam & 0xFFFF
            if low == WM_LBUTTONUP:
                self._reveal()
            elif low == 0x0205:                      # WM_RBUTTONUP
                self._tray_menu()
            return 0
        if msg == WM_COMMAND:
            if (wparam & 0xFFFF) == IDM_EXIT:
                self._quit()
            return 0
        if msg == WM_TIMER:
            if wparam == 2:                          # 悬停轮询: 右下角触发/收起设置面板
                self._hover_poll()
                return 0
            if not self._note_editor:                # 编辑备注时不重排, 免得错位
                self._poll += 1
                if self._poll % 3 == 0:              # 每 ~3s 重扫项目/Codex
                    self._gather_infos()
                    self._render_now()
                    self._tray_sync()                # 灯色变了就换托盘图标
                elif self.running:                   # 其余每秒只为刷新计时
                    self._render_now()
            return 0
        if msg == WM_DESTROY:
            self._tray(NIM_DELETE)                   # 别在托盘里留一个点不掉的僵尸图标
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- 折叠 / 计时 ----
    def _toggle_collapse(self):
        self.collapsed = not self.collapsed
        self.ui_data["collapsed"] = self.collapsed
        save_json(UI_FILE, self.ui_data)
        self._render_now()

    def _toggle_timer(self):
        if self.running:
            dur = int(time.time() - self.start_ts) if self.start_ts else 0
            if dur > 0:                              # 追加一段, 不覆盖历史
                self.tlog.setdefault(today_key(), []).append({
                    "start": datetime.datetime.fromtimestamp(self.start_ts).strftime("%H:%M"),
                    "end": datetime.datetime.now().strftime("%H:%M"),
                    "sec": dur,
                })
                save_json(STANDUP_LOG_FILE, self.tlog)
            self.running = False
            self.start_ts = None
            self.ui_data["running_start"] = None
        else:
            self.running = True
            self.start_ts = time.time()
            self.ui_data["running_start"] = self.start_ts
        save_json(UI_FILE, self.ui_data)
        self._render_now()

    # ---- 备注就地编辑 (临时置顶 EDIT 弹窗) ----
    def _open_note_editor(self):
        if self._note_editor:
            return
        try:
            nr = None
            for rx, ry, rw, rh, kind in self.ctl_rects:
                if kind == "notes":
                    nr = (int(rx), int(ry), int(rw), int(rh))
            if not nr:
                return
            rect = RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
            ex, ey = rect.left + nr[0], rect.top + nr[1]
            ew, eh = nr[2], nr[3]
            hInst = ctypes.windll.kernel32.GetModuleHandleW(None)
            style = (WS_POPUP | WS_VISIBLE | WS_BORDER | ES_MULTILINE |
                     ES_AUTOVSCROLL | ES_WANTRETURN | WS_VSCROLL)
            text = self.up_notes.replace("\r\n", "\n").replace("\n", "\r\n")  # EDIT 要 CRLF
            ed = user32.CreateWindowExW(WS_EX_TOPMOST, "EDIT", text, style,
                                        ex, ey, ew, eh, None, None, hInst, None)
            if not ed:
                return
            px = max(round(13 * self.ui), 12)
            font = gdi32.CreateFontW(-px, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0,
                                     "Microsoft YaHei UI")
            user32.SendMessageW(ed, WM_SETFONT, font, 1)
            self._edit_font = font
            self._edit_oldproc = _SetWLP(ed, GWLP_WNDPROC,
                                         ctypes.cast(self._edit_wndproc, ctypes.c_void_p))
            self._note_editor = ed
            n = len(text)
            user32.SendMessageW(ed, EM_SETSEL, n, n)     # 光标置末尾
            try:                                          # 抢前台以便直接输入
                fg = user32.GetForegroundWindow()
                t_fg = user32.GetWindowThreadProcessId(fg, None)
                t_me = ctypes.windll.kernel32.GetCurrentThreadId()
                user32.AttachThreadInput(t_fg, t_me, True)
                user32.SetForegroundWindow(ed)
                user32.SetFocus(ed)
                user32.AttachThreadInput(t_fg, t_me, False)
            except Exception:
                user32.SetFocus(ed)
        except Exception as e:
            import traceback
            log("open editor: " + repr(e) + "\n" + traceback.format_exc())

    def _edit_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_KILLFOCUS:                       # 失焦 -> 提交
            user32.PostMessageW(self.hwnd, WM_APP_COMMIT, 0, 0)
        elif msg == WM_KEYDOWN and wparam == VK_ESCAPE:  # Esc -> 取消
            user32.PostMessageW(self.hwnd, WM_APP_COMMIT, 1, 0)
            return 0
        return user32.CallWindowProcW(self._edit_oldproc, hwnd, msg, wparam, lparam)

    def _close_note_editor(self, cancel=False):
        ed = self._note_editor
        if not ed:
            return
        self._note_editor = None
        try:
            if not cancel:
                n = user32.GetWindowTextLengthW(ed)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(ed, buf, n + 1)
                self.up_notes = buf.value.replace("\r\n", "\n")
                _write_text(NOTES_FILE, self.up_notes)
            if self._edit_oldproc:
                _SetWLP(ed, GWLP_WNDPROC, self._edit_oldproc)
        except Exception as e:
            log("close editor: " + repr(e))
        try:
            user32.DestroyWindow(ed)
        except Exception:
            pass
        if self._edit_font:
            try:
                gdi32.DeleteObject(self._edit_font)
            except Exception:
                pass
            self._edit_font = None
        self._edit_oldproc = None
        self._render_now()

    def _loop(self):
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def _single_instance():
    """已在运行则返回 True (防止开机自启与手动启动叠加多个窗口)。"""
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, "AITrafficLight_Singleton_Mutex")
    return k32.GetLastError() == 183   # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    # 同一个可执行体兼任 hook: `AITrafficLight.exe --hook green` 就是钩子入口。
    # 这样打包成单个 exe 之后, 用户机器上不需要 Python, hook 也不用去找 status_hook.py 在哪。
    if len(sys.argv) > 2 and sys.argv[1] == "--hook":
        import status_hook
        sys.argv = [sys.argv[0], sys.argv[2]]
        status_hook.main()
    elif not _single_instance():
        FloatingWidget()
