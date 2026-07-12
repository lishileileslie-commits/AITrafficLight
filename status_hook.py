# -*- coding: utf-8 -*-
"""
Claude Code hook: update the current project's .ai-status.json from AI activity.

Expected hook mapping:
  UserPromptSubmit -> python status_hook.py green
  Notification     -> python status_hook.py yellow
  Stop             -> python status_hook.py red

The hook reads JSON from stdin. Known fields include cwd, prompt, and
transcript_path. It preserves buttons, but refreshes task/summary so the
traffic-light card describes the project state instead of merely echoing the
last user sentence.
"""

import json
import os
import re
import sys


DEFAULT_TASK = {
    "green": "正在处理",
    "yellow": "待你处理",
    "red": "空闲",
}

SUMMARY_MAX_CHARS = 13

# 状态后缀(各 3 字): 永远保留, 先扣掉它再给核心短语分配字数, 绝不被截断。
STATE_SUFFIX = {"green": "处理中", "yellow": "待确认", "red": "已完成"}

LEAD_WORDS = (
    "你先帮我", "你先", "你帮我", "帮我", "请帮我", "请", "麻烦你", "麻烦",
    "现在", "然后", "接下来", "我想要", "我想", "我要", "我需要",
    "能不能帮我", "能不能", "可不可以", "可以帮我", "可以", "给我",
    "我的", "你的", "咱们", "我们", "帮忙", "我", "你", "的", "把",
)

# 命中即用"概括词"当摘要 —— 一律不回显用户原话。按顺序取第一个命中。
ACTION_PATTERNS = (
    (r"弹窗|弹出|多个网页|noopener|递归|window\.open|isTrusted|popup", "弹窗修复"),
    (r"妙搭|Miaoda|aiforce|release|发布轮询", "妙搭发布"),
    (r"工作流|workflow", "工作流修复"),
    (r"部署|发布|上线|deploy|firebase|hosting", "发布上线"),
    (r"git|提交|commit|推送|push|拉取|pull|克隆|clone|仓库|版本库|代码库|库已更新", "代码提交"),
    (r"邮箱|邮件|mail|gmail|附件|报价单", "邮件整理"),
    (r"最低价|最高价|多少钱|价格|报价|售价|成本|库存", "查价核对"),
    (r"多少次|几次|次数|多少个|几个|数量|条数|个数|多少", "数量核对"),
    (r"绿灯|黄灯|红灯|灯色|traffic light|灯不对|僵尸绿", "灯色修复"),
    (r"报告|dashboard|仪表盘|页面|html|网页|卡片|界面|ui", "页面更新"),
    (r"修|bug|报错|错误|失败|不对|问题|崩|卡住|异常|坏了", "问题修复"),
    (r"总结|摘要|概括|梳理", "内容总结"),
    (r"翻译|translate", "翻译处理"),
    (r"搜索|查找|定位|路径|哪个文件|找一下|在哪", "文件定位"),
    (r"整理|归纳|汇总|归档", "资料整理"),
    (r"写|生成|创建|新建|做一个|做个|加个|加一个", "内容生成"),
)

# 没命中类目时, 从话题里剥掉"提问脚手架/客套", 抽出核心对象短语当概括。
# 话首要剥的: 疑问词 + 客套 + "做成/改成…"这类壳动词。
QUESTION_LEAD = (
    "您能不能帮我", "您能不能", "您可不可以", "您可以帮我", "您可以", "您帮我", "您",
    "能不能帮我", "能不能", "能否", "可不可以", "可否", "是不是", "是否",
    "可以帮我", "帮我", "帮忙", "请帮我", "请", "麻烦你", "麻烦", "给我", "跟我",
    "现在", "然后", "接下来", "我想要", "我想", "我要", "我需要", "我", "你", "咱们", "我们",
    "做成", "改成", "弄成", "搞成", "整成", "换成", "变成", "把", "将",
)
# 话尾要剥的: 客套 + "的形式/的方式"这类壳后缀。
TRAIL_FILLER = (
    "方便我理解", "方便理解", "方便我看", "方便查看", "方便我", "谢谢你", "谢谢", "麻烦了",
    "可以吗", "行不行", "好不好", "好吗", "行吗", "怎么样", "一下", "这个功能", "的功能",
    "的形式", "的方式", "的样子", "这个", "一点", "一些", "吗", "呢", "吧", "么", "啊", "呀",
)


# 元对话 / 闲聊碎片: 抽出来的短语若是这些, 不是"在干的活" -> 拒绝, 守住不回显闲聊。
META_PREFIX = ("没理解", "不理解", "没明白", "不明白", "没懂", "没听懂", "理解错", "误解",
               "搞错", "弄错", "想错", "记错", "不是说", "刚才说", "你说",
               "觉得", "感觉", "认为", "这里", "那里", "再说", "再改", "改改", "重新想")
META_CONTAINS = ("我的意思", "什么意思", "不是这个意思", "不对劲")


def distill_topic(topic):
    """没命中类目时, 剥掉话题里的提问脚手架 / 客套, 抽出核心对象短语
    (如 '您能不能做成工作树的形式方便我理解' -> '工作树')。不是回显整句 —— 是提炼
    用户在做的那个'东西'当概括, 比 '项目名+任务' 到位。抽不出干净对象(空 / 元对话)就返回空串。"""
    t = clip(topic, 40)
    for words, at_head in ((QUESTION_LEAD, True), (TRAIL_FILLER, False)):
        changed = True
        while changed:
            changed = False
            for w in words:
                hit = t.startswith(w) if at_head else t.endswith(w)
                if hit and len(t) > len(w):
                    t = (t[len(w):] if at_head else t[:-len(w)]).strip(" ，,。、的了")
                    changed = True
                    break
    t = t.strip(" ，,。、的了")
    if any(t.startswith(p) for p in META_PREFIX) or any(k in t for k in META_CONTAINS):
        return ""                                     # 元对话/闲聊 -> 交回上层退通用兜底
    return t


def has_work_signal(text):
    """这句话是否带"实义工作信号" = 命中类目关键词, 或能抽出核心对象。
    用来决定要不要拿它更新话题: 纯澄清/抱怨(无信号)不该冲掉上一个好话题。"""
    if not text:
        return False
    for pattern, _ in ACTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return bool(distill_topic(text))


def clip(text, limit):
    text = (text or "").strip().replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def first_clause(prompt):
    text = clip(prompt, 160)
    if not text:
        return ""
    seg = re.split(r"[。！？!?；;，,\n]", text, 1)[0].strip() or text
    changed = True
    while changed:
        changed = False
        for word in LEAD_WORDS:
            if seg.startswith(word) and len(seg) > len(word):
                seg = seg[len(word):].strip()
                changed = True
                break
    return clip(seg, 28)


def project_label(cwd):
    name = os.path.basename(os.path.normpath(cwd or "")) or "当前项目"
    known = {
        "amazon": "Amazon",
        "104": "104",
        "AITrafficLight": "AI红绿灯",
        "upet-recall": "UPet Recall",
    }
    return known.get(name, name)


def make_summary(cwd, topic, prompt, status):
    """13 字以内的"概括" —— 描述在干的活, 绝不照抄用户原话: 命中类目取概括词,
    没命中就用项目名兜底成"XX任务"。末尾恒定接状态后缀(处理中/待确认/已完成)且保证不被截断。"""
    suffix = STATE_SUFFIX.get(status, "处理中")
    budget = SUMMARY_MAX_CHARS - len(suffix)          # 先给后缀留位, 剩下的才给核心短语
    # 类目匹配扫"整句"(prompt 全文 + 主题), 关键词落在逗号后面也能命中; 类目产出的是概括词, 不会回显。
    haystack = (prompt or "") + " " + (topic or "")
    core = ""
    for pattern, label in ACTION_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            core = label
            break
    if not core:                                      # 没命中类目: 先试着从话题里抽核心对象("工作树"/"头程计算器")
        obj = distill_topic(topic)
        # 干净的短对象短语(装得下、结尾不是疑问语气)就用它当概括; 否则(接近整句/带疑问尾)
        # 退回"项目名+任务", 绝不把用户整句截断回显。
        if obj and len(obj) <= budget and obj[-1] not in "吗呢吧么？?":
            core = obj
        else:
            core = project_label(cwd) + "任务"
    return clip(clip(core, budget) + suffix, SUMMARY_MAX_CHARS)


def make_task(topic, summary, status):
    return clip(summary or DEFAULT_TASK.get(status, "正在处理"), SUMMARY_MAX_CHARS)


def transcript_title(tpath):
    if not tpath:
        return ""
    try:
        with open(tpath, "r", encoding="utf-8-sig") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                title = item.get("aiTitle")
                if item.get("type") == "ai-title" and title:
                    return str(title)
    except Exception:
        pass
    return ""


def main():
    status = sys.argv[1] if len(sys.argv) > 1 else "green"
    if status not in ("green", "yellow", "red"):
        status = "green"

    try:
        raw = sys.stdin.buffer.read()
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except Exception:
        data = {}

    cwd = data.get("cwd") or os.getcwd()
    fp = os.path.join(cwd, ".ai-status.json")

    old = {}
    try:
        with open(fp, "r", encoding="utf-8-sig") as f:
            old = json.load(f)
    except Exception:
        pass

    tpath = data.get("transcript_path") or old.get("_transcript", "")
    prompt = data.get("prompt", "")
    if str(prompt).lstrip().startswith("<task-notification>"):
        sys.exit(0)

    topic = old.get("_topic", "")
    title = transcript_title(tpath)
    if title and has_work_signal(title):
        topic = title
    if status == "green" and prompt:
        nt = first_clause(prompt)
        # 话题黏性: 只有带实义工作信号的新句子才更新话题; 纯澄清/抱怨("没理解我的意思")
        # 不冲掉上一个好话题。没有历史话题时, 至少留住这条兜底。
        if nt and (has_work_signal(nt) or not topic):
            topic = nt

    summary = make_summary(cwd, topic, prompt, status)
    task = make_task(topic, summary, status)
    out = {
        "status": status,
        "task": task,
        "summary": summary,
        "_topic": topic,
    }

    if tpath:
        out["_transcript"] = tpath

    if isinstance(old.get("buttons"), list):
        out["buttons"] = old["buttons"]

    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
