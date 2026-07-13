# -*- coding: utf-8 -*-
"""首次运行的一键接入: 把红绿灯的 hook 写进 Claude Code 的 settings.json, 顺带可选开机自启。

目标是"装上就能用": 用户不该为了看个灯去手编 JSON。所以这里做三件事 ——
  1. 判断当前 settings.json 里的 hook 是不是已经指向"这一份"红绿灯 (路径会变: 换目录、
     从源码切到 exe, 都得能自愈);
  2. 没接过 -> 弹一次框问 (改别人的配置文件不打招呼, 在开源社区是要挨骂的);
  3. 接过但路径过时 -> 你早就同意过了, 静默改好, 不再烦你。

hook 命令统一指向"当前这个可执行体":
  exe:  AITrafficLight.exe --hook green
  源码: python.exe app.py --hook green
所以打包成单个 exe 之后, 用户机器上不需要 Python, 也不需要知道 status_hook.py 在哪。
"""

import os
import sys
import json
import shutil
import ctypes
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

# hook 事件 -> 灯色。与 status_hook.py 的三个入参一致。
EVENTS = (("UserPromptSubmit", "green"), ("Notification", "yellow"), ("Stop", "red"))

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AITrafficLight"

# 认出"我们自己写的 hook 条目"用的指纹: 命中任一即视为旧条目, 重接时先清掉再写新的。
# 认得出才谈得上自愈 —— 否则换个路径就多出一条僵尸 hook, 越攒越多。
MARKS = ("status_hook.py", "--hook", "AITrafficLight")

MB_YESNOCANCEL, MB_ICONQUESTION, MB_TOPMOST = 0x03, 0x20, 0x40000
IDYES, IDNO = 6, 7


def self_cmd():
    """(可执行体, 固定前缀参数) —— 打包成 exe 后就是 exe 自己。"""
    if getattr(sys, "frozen", False):
        return sys.executable, []
    return sys.executable, [os.path.join(HERE, "app.py")]


def _entry(color):
    exe, pre = self_cmd()
    return {"hooks": [{"type": "command", "command": exe,
                       "args": pre + ["--hook", color],
                       "timeout": 10, "async": True}]}


def _is_ours(group):
    blob = json.dumps(group, ensure_ascii=False)
    return any(m in blob for m in MARKS)


def _load(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def wire(settings):
    """把三个 hook 合并进去 (只动我们自己的条目, 别人的原样留着)。返回 (新配置, 是否有改动)。"""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    changed = False
    for event, color in EVENTS:
        groups = hooks.get(event)
        groups = list(groups) if isinstance(groups, list) else []
        kept = [g for g in groups if not _is_ours(g)]      # 别人的 hook 一律留下
        want = _entry(color)
        new = kept + [want]
        if groups != new:
            changed = True
        hooks[event] = new
    settings["hooks"] = hooks
    return settings, changed


def _save(path, data):
    """先备份再原子写 —— 这是别人的配置文件, 写坏了他整个 Claude Code 就废了。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, "%s.bak-%s" % (path, stamp))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def autostart_on():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except Exception:
        return False


def set_autostart(on):
    import winreg
    exe, pre = self_cmd()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
            if on:
                cmd = " ".join('"%s"' % a for a in [exe] + pre)
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def _ask():
    exe, _ = self_cmd()
    msg = ("把 AI 红绿灯接入 Claude Code?\n\n"
           "会在你的 settings.json 里加三个 hook (改动前自动备份), "
           "让 Claude Code 把会话状态报给悬浮窗。\n\n"
           "  [是]   接入, 并设置开机自动启动\n"
           "  [否]   只接入, 不自动启动\n"
           "  [取消] 都不做 (以后不再问)\n\n"
           "程序位置: %s" % exe)
    return ctypes.windll.user32.MessageBoxW(
        None, msg, "AI 红绿灯 · 首次设置",
        MB_YESNOCANCEL | MB_ICONQUESTION | MB_TOPMOST)


def run(ui_data, save_ui):
    """在悬浮窗启动时调一次。ui_data/save_ui 用来记住"问过了", 免得每次开机都弹框。"""
    settings = _load(SETTINGS)
    merged, changed = wire(json.loads(json.dumps(settings)))   # 先在副本上算, 不改原件
    if not changed:
        return                                    # 已经接好且路径没变 -> 什么都不做
    already = bool(ui_data.get("onboarded"))
    if not already:
        if ui_data.get("onboard_declined"):
            return                                # 他说过不要, 别再烦他
        ans = _ask()
        if ans not in (IDYES, IDNO):
            ui_data["onboard_declined"] = True    # 取消 = 以后不再问
            save_ui()
            return
        if ans == IDYES:
            set_autostart(True)
    # already=True 走到这儿 = 他早就同意过, 只是路径变了(换目录/切 exe) -> 静默自愈, 不弹框。
    # 自启的路径也一起改, 否则开机拉起的还是搬走前的老位置。
    if already and autostart_on():
        set_autostart(True)
    try:
        _save(SETTINGS, merged)
    except Exception:
        return
    ui_data["onboarded"] = True
    save_ui()
