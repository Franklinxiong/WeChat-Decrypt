# -*- coding: utf-8 -*-
"""analysis/distill.py — 纯本地规则「聊天人蒸馏 Skill」

把某个微信会话(session_slot) / 发送者(sender_slot / "self")蒸馏为可植入
agent 平台的角色人设 SKILL：SKILL.md + assets/(persona_card.md, style_samples.md, wordcloud.png)。

设计约束：
- 纯本地规则：jieba 词频 / 高频短语 / 语气词 / emoji / 标点习惯 / 长度分布 / 活跃时段，
  无 LLM、无 API Key。
- 隐私打码：手机号 / wxid / 16 位以上数字串 / email 正则掩码，SKILL 正文与 sample 均打码。
- 默认输出到仓库外 ~/WeChatData/skills/<slot>/，slot 取昵称(打码)或手动标题 + 时间戳。
- 全程容错：依赖缺失 / 数据不足时返回空产出并说明原因，不抛异常。
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import stats as _stats


# ---------------------------------------------------------------- 隐私打码

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_WXID_RE = re.compile(r"wxid_[A-Za-z0-9_-]+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_NUM_RE = re.compile(r"(?<!\d)\d{11,}(?!\d)")
_IP_RE = re.compile(r"\d{1,3}(\.\d{1,3}){3}")
_ATMD_RE = re.compile(r"@[\w\u4e00-\u9fff\u3400-\u4dbf\-\.·]{1,24}")


def sanitize(text: str) -> str:
    """对内容做隐私打码：手机号/邮箱/IP/wxid/@提及用户/长数字串。"""
    if not isinstance(text, str):
        return ""
    t = _IP_RE.sub("[IP]", text)
    t = _PHONE_RE.sub("[手机号]", t)
    t = _EMAIL_RE.sub("[邮箱]", t)
    t = _WXID_RE.sub("[用户]", t)
    t = _ATMD_RE.sub("[@用户]", t)
    t = _NUM_RE.sub("[数字]", t)
    return t


def _safe_filename(name: str) -> str:
    """把名字转成安全目录名：去掉路径分隔与控制字符，保留中文/字母数字。"""
    out = []
    for ch in name or "":
        if ch in "/\\:*?\"<>|" or unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        out.append(ch)
    s = "".join(out).strip().strip(".")
    return s[:48] or "persona"


# ---------------------------------------------------------------- 字体与词云

_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]


def find_cn_font() -> str | None:
    """macOS 中文字体探测：PingFang 优先，含回退列表。"""
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


_TONE_WORDS = ("啊", "呢", "吧", "哈", "啦", "呀", "哦", "嘛", "啵", "哟", "嘞", "呗", "嗯", "哇", "耶", "嘿嘿", "哈哈")
_EMOJI_RANGE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002700-\U000027BF\U0001F1E6-\U0001F1FF\U00002600-\U000026FF\u2B00-\u2BFF\U0001F900-\U0001F9FF\U00002049\U0000203C\uFE0F]"
)


# ---------------------------------------------------------------- 特征提取


def _iter_tokens(texts: pd.Series) -> dict:
    """jieba 分词（逐条去重、过滤停用词/单字/噪声），返回 {词: 词频}。"""
    cnt: dict = {}
    for c in _stats._clean_text_series(texts):
        if not c:
            continue
        seen = set()
        for w in _stats._jieba.cut(c):
            w = w.strip()
            if len(w) < 2 or w in _stats.STOPWORDS:
                continue
            if not re.search(r"[\u4e00-\u9fff\u3400-\u4dbfA-Za-z]", w):
                continue
            # 疑似 ID/用户名 token（如 a32587944）不进词频
            if re.search(r"\d{3,}", w):
                continue
            if w in seen:
                continue
            seen.add(w)
            cnt[w] = cnt.get(w, 0) + 1
    return cnt


def collect_profile(dfs: dict[str, pd.DataFrame], session=None, sender=None,
                    chattype: str = "all") -> dict:
    """收集目标文本消息并统计风格画像，返回 dict。纯本地规则。

    chattype: 'all' / 'dm'(仅私聊) / 'group'(仅群聊)——群聊成员发言与私聊严格隔离，
    同一成员在群聊与私聊中的发言可被区分蒸馏。
    """
    base = {
        "ok": False, "reason": "",
        "title": "", "total_messages": 0, "text_messages": 0, "total_chars": 0,
        "avg_len": 0.0, "median_len": 0.0, "longest_len": 0,
        "top_words": [], "top_phrases": [], "tone_counts": {}, "emoji_counts": {},
        "exclaim_ratio": 0.0, "question_ratio": 0.0, "ellipsis_ratio": 0.0,
        "hour_peaks": [], "samples": [], "contains_self": False, "contains_other": False,
    }
    msgs = _stats._get(dfs, "messages")
    if msgs.empty or "content_text" not in msgs.columns or "create_time" not in msgs.columns:
        base["reason"] = "消息表为空或缺少 content_text/create_time 列"
        return base

    mask = _stats._text_mask(msgs)
    if session is not None and "session_slot" in msgs.columns:
        sl = pd.to_numeric(msgs["session_slot"], errors="coerce")
        mask &= sl == int(session)
    mask &= _stats._sender_mask(msgs, sender)
    mask &= _stats._chattype_mask(dfs, msgs, chattype)
    if not mask.any():
        base["reason"] = "该范围内没有文本消息可蒸馏"
        return base

    sub = msgs.loc[mask]
    texts = sub["content_text"].astype(str)
    # 剥离微信转发/引用产生的「用户名:」前缀（形如 a32587944: 内容）
    texts = texts.str.replace(r"^[A-Za-z0-9_-]{2,}\s*[:：]\s*", "", regex=True).str.strip()
    # 超大样本（某活跃发送者可能数万条）抽样上限，保证蒸馏秒级完成
    if len(texts) > 20000:
        texts = texts.sample(20000, random_state=7)
        sub = sub.loc[texts.index]

    base["total_messages"] = int(len(sub))
    base["text_messages"] = int(len(texts))
    base["total_chars"] = int(texts.str.len().sum())
    base["avg_len"] = round(float(texts.str.len().mean()), 1)
    lens = texts.str.len()
    base["median_len"] = int(lens.median())
    base["longest_len"] = int(lens.max())

    if _stats._HAS_JIEBA:
        tok = _iter_tokens(texts)
        base["top_words"] = sorted(tok, key=lambda x: -tok[x])[:30]
        base["_word_freq"] = tok
        base["top_phrases"] = [
            {"phrase": sanitize(p.get("phrase", "")), "count": int(p.get("count", 0))}
            for p in _stats._freq_phrases(texts, top=30).to_dict("records")
        ]

    # 语气词 / emoji / 标点习惯
    tone: dict = {}
    emoji: dict = {}
    for t in texts:
        for w in _TONE_WORDS:
            if w in t:
                tone[w] = tone.get(w, 0) + t.count(w)
        for e in _EMOJI_RANGE.findall(t):
            emoji[e] = emoji.get(e, 0) + 1
    base["tone_counts"] = dict(sorted(tone.items(), key=lambda x: -x[1])[:12])
    base["emoji_counts"] = dict(sorted(emoji.items(), key=lambda x: -x[1])[:15])
    base["exclaim_ratio"] = round(float((texts.str.count(r"!") + texts.str.count(r"！")).clip(0, 1).mean()), 3)
    base["question_ratio"] = round(float((texts.str.count(r"\?") + texts.str.count(r"？")).clip(0, 1).mean()), 3)
    base["ellipsis_ratio"] = round(float((texts.str.count(r"\.\.\.") + texts.str.count(r"……")).clip(0, 1).mean()), 3)

    # 活跃时段
    try:
        local = _stats._local_ts(sub)
        if not local.empty:
            vc = local.dt.hour.value_counts().sort_index()
            peak = vc[vc > 0].sort_values(ascending=False).head(4).index.astype(int).sort_values().tolist()
            base["hour_peaks"] = [f"{h:02d}:00" for h in peak]
    except Exception:
        base["hour_peaks"] = []

    base["contains_self"] = bool((_stats._sent_mask(sub) if "is_send" in sub.columns else pd.Series(False, index=sub.index)).any())
    base["contains_other"] = int(sub["sender_slot"].notna().sum()) > 0

    # 风格样本：多种代表性消息（打码）
    base["samples"] = pick_samples(texts)

    base["ok"] = True
    return base


def pick_samples(texts: pd.Series, top: int = 18) -> list[dict]:
    """挑选风格代表性消息：长 / 短 / 感叹 / 问句 / emoji / 语气词 / 深夜。返回打码文本+标签。"""
    picked: dict[str, str] = {}
    labels: dict[str, str] = {}

    def _add(key, idx):
        if idx < 0 or idx >= len(texts) or key in picked:
            return
        picked[key] = texts.iloc[idx]
        labels[key] = key

    cleaned = texts.reset_index(drop=True)
    lens = cleaned.str.len()
    _add("长段输出", int(lens.idxmax())) if len(cleaned) else None
    _add("短句风格", int(lens.idxmin())) if len(cleaned) else None
    ex = cleaned[cleaned.str.contains(r"[!！]", na=False)]
    if len(ex):
        _add("感叹语气", int(ex.count().index[0] if False else cleaned[cleaned.str.contains(r"[!！]", na=False)].index[0]))
    q = cleaned[cleaned.str.contains(r"[?？]", na=False)]
    if len(q):
        _add("反问提问", int(q.index[0]))
    em = cleaned[cleaned.str.contains("[\U0001F000-\U0001FAFF\u2700-\u27BF]", na=False)]
    if len(em):
        _add("表情流", int(em.index[0]))
    tone = cleaned[cleaned.str.contains(r"[啊呢吧啦呀嘛哈]", na=False)]
    if len(tone):
        _add("口头禅", int(tone.index[0]))
    # 补充其余到 top
    order = []
    for label in picked:
        order.append(picked[label])
    rest_idx = [i for i in range(len(cleaned)) if cleaned.iloc[i] not in set(order)]
    # 简单补齐：取开头/中间/末尾代表
    if len(rest_idx):
        for i in (0, len(cleaned) // 3, 2 * len(cleaned) // 3, len(cleaned) - 1):
            if i in rest_idx and len(picked) < top:
                picked[f"日常片段 {len(picked)+1}"] = cleaned.iloc[i]
    out = []
    for label, txt in picked.items():
        if not txt or not str(txt).strip():
            continue
        out.append({"label": label, "text": sanitize(str(txt))[:240]})
    return out[:top]


# ---------------------------------------------------------------- SKILL 生成

def _fmt_ratio(x) -> str:
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "-"


def build_skill(
    dfs: dict[str, pd.DataFrame],
    out_dir: str | None = None,
    title: str | None = None,
    session=None,
    sender=None,
    words_limit: int = 2000,
    chattype: str = "all",
) -> dict:
    """蒸馏并写出 SKILL 文件夹，返回 {skill_dir, files, summary, ok, reason}。

    chattype: all/dm/group——发送者/自己蒸馏时限定会话类型（群聊与私聊隔离）。
    """
    profile = collect_profile(dfs, session=session, sender=sender, chattype=chattype)
    if not profile["ok"]:
        return {"skill_dir": "", "files": [], "summary": {}, "ok": False, "reason": profile["reason"]}

    title = (title or "").strip()
    if not title:
        base_name = "人设蒸馏"
        if session is not None and "sessions" in dfs:
            row = dfs["sessions"]
            if "session_slot" in row.columns and len(row):
                hit = row[row["session_slot"].astype(str) == str(session)]
                if not hit.empty and "display_name" in row.columns:
                    base_name = str(hit.iloc[0]["display_name"]) if hit.iloc[0].get("display_name") else base_name
        if sender is not None and "senders" in dfs:
            row = dfs["senders"]
            if "sender_slot" in row.columns and len(row):
                hit = row[row["sender_slot"].astype(str) == str(sender)]
                if not hit.empty and "display_name" in row.columns:
                    base_name = str(hit.iloc[0]["display_name"]) if hit.iloc[0].get("display_name") else base_name
        title = f"{sanitize(base_name)}（{profile['text_messages']}条样本）"

    slot = _safe_filename(sanitize(title))
    if out_dir is None:
        out_dir = os.path.join(os.path.expanduser("~/WeChatData/skills"), slot)
    out_dir = os.path.abspath(out_dir)
    assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    files: list[str] = []

    # ---- SKILL.md ----
    skill_md = _render_skill_md(profile, title)
    p_skill = os.path.join(out_dir, "SKILL.md")
    with open(p_skill, "w", encoding="utf-8") as f:
        f.write(skill_md)
    files.append(p_skill)

    # ---- persona_card.md ----
    persona_md = _render_persona_md(profile, title)
    p_persona = os.path.join(assets_dir, "persona_card.md")
    with open(p_persona, "w", encoding="utf-8") as f:
        f.write(persona_md)
    files.append(p_persona)

    # ---- style_samples.md ----
    samples_md = _render_samples_md(profile["samples"])
    p_samples = os.path.join(assets_dir, "style_samples.md")
    with open(p_samples, "w", encoding="utf-8") as f:
        f.write(samples_md)
    files.append(p_samples)

    # ---- wordcloud.png ----
    p_wc = os.path.join(assets_dir, "wordcloud.png")
    if _render_wordcloud(profile, p_wc, words_limit=words_limit):
        files.append(p_wc)

    summary = {
        "title": title,
        "slot": slot,
        "text_messages": profile["text_messages"],
        "total_chars": profile["total_chars"],
        "top_words": profile["top_words"][:10],
        "files": files,
    }
    return {"skill_dir": out_dir, "files": files, "summary": summary, "ok": True, "reason": ""}


def _render_skill_md(profile: dict, title: str) -> str:
    w = profile["top_words"][:12]
    words_str = "、".join(w) if w else "（样本不足）"
    phrases = profile["top_phrases"][:8]
    phrases_str = ("；".join(f"「{p.get('phrase','')}」" for p in phrases) if phrases else "（样本不足）")
    tone_str = " ".join(f"{k}×{v}" for k, v in list(profile["tone_counts"].items())[:8]) or "无明显口头禅"
    active = "、".join(profile["hour_peaks"]) or "（分布较均匀）"
    is_self = "含本人发言" if profile["contains_self"] else "以对方发言为主"
    return f"""---
name: {title}
description: >
  模仿「{title}」的中文微信聊天口吻与说话习惯。统计自 {profile['text_messages']} 条真实文本消息（当前会话/发送者），
  覆盖用词、口头禅、语气词、标点与 emoji 偏好。活跃时段约 {active}，发言长度以 {profile['median_len']} 字左右为主。
---

# {title} · 聊天人设 Skill

> 蒸馏来源：本地微信聊天分析（{is_self}）。纯本地规则生成，无外部模型。示例文本已做隐私打码。

## 角色定位

你将以 **{title}** 的口吻与人进行中文聊天。你不是助手，而是一个有性格的真实聊天对象，像微信好友那样自然回应。

## 语言风格要点

1. **用词习惯**：高频使用 —— {words_str}。
2. **口头禅 / 高频短句**：{phrases_str}。
3. **语气词**：{tone_str}。
4. **语速与长度**：单条消息中位约 {profile['median_len']} 字，平均 {profile['avg_len']} 字，偏向{'简短碎片化' if profile['median_len'] <= 15 else '中段叙述'}；长消息多为一次性叙述。
5. **标点习惯**：感叹号出现占比约 {_fmt_ratio(profile['exclaim_ratio'])}，问句占比约 {_fmt_ratio(profile['question_ratio'])}，省略号占比约 {_fmt_ratio(profile['ellipsis_ratio'])}。
6. **emoji 偏好**：{' '.join(list(profile['emoji_counts'].keys())[:8]) if profile['emoji_counts'] else '使用较少'}（按频率排序）。
7. **活跃时段**：常见于 {active}。

## 说话纪律

- 模仿口吻而非内容：不编造 TA 未提及的个人经历、联系方式或隐私。
- 复用 TA 的高频词、口头禅、语气词与标点习惯，但要自然、不堆砌。
- 消息长度贴合 {profile['median_len']} 字左右；对方问短则答短，对方长聊则适度展开。

## 使用示例

- 用户说：今天好累
  - 你（模仿口吻）：{_render_example_reply(profile)}
- 用户说：周末一起吃饭吗
  - 你（模仿口吻）：能用上面的高频词与口头禅组合成自然回应。

## 数据说明

- 样本：{profile['text_messages']} 条文本消息 / {profile['total_chars']} 字。
- 全部示例见 `assets/style_samples.md`，人物画像见 `assets/persona_card.md`，词云见 `assets/wordcloud.png`。
- 若消息中带有人名/手机号等敏感信息，已统一打码处理。
"""


def _render_example_reply(profile: dict) -> str:
    w = profile["top_words"][:4]
    if not w:
        return "嗯嗯，确实是这样。"
    return f"「{''.join(w[:3])}」什么的……今天确实有点累，早点休息呀。"


def _render_persona_md(profile: dict, title: str) -> str:
    """渲染 assets/persona_card.md：结构化人物画像。"""
    tone = "；".join(f"{k}×{v}" for k, v in list(profile["tone_counts"].items())[:10]) or "无明显口头禅"
    emoji = " ".join(list(profile["emoji_counts"].keys())[:10]) or "使用较少"
    phrases = "；".join(f"「{p.get('phrase', '')}」" for p in profile["top_phrases"][:10]) or "（样本不足）"
    active = "、".join(profile["hour_peaks"]) or "（分布较均匀）"
    words = "、".join(profile["top_words"][:20]) or "（样本不足）"
    rhythm = "短句碎片化" if profile["median_len"] <= 15 else "中段叙述为主"
    return f"""# {title} · 人物画像卡

> 由本地微信聊天记录统计生成，纯本地规则，示例文本均已做隐私打码。

## 基本数据

- 文本消息：{profile['text_messages']} 条 / 共 {profile['total_chars']} 字
- 单条长度：平均 {profile['avg_len']} 字，中位 {profile['median_len']} 字，最长 {profile['longest_len']} 字
- 发言构成：{'含本人发言' if profile['contains_self'] else '以对方发言为主'}

## 用词画像

- 高频词：{words}
- 高频短句：{phrases}

## 表达习惯

- 语气词：{tone}
- emoji 偏好：{emoji}
- 感叹占比：{_fmt_ratio(profile['exclaim_ratio'])}；问句占比：{_fmt_ratio(profile['question_ratio'])}；省略号占比：{_fmt_ratio(profile['ellipsis_ratio'])}
- 活跃时段：{active}

## 风格结论

- 话语节奏：{rhythm}；单条消息通常 {profile['median_len']} 字左右。
"""


def _render_samples_md(samples: list[dict]) -> str:
    """渲染 assets/style_samples.md：带标签的风格样本。"""
    if not samples:
        return "# 风格样本\n\n（样本不足，未能生成）\n"
    lines = ["# 风格样本", "", "以下为从聊天记录中挑选出的代表性消息（已隐私打码），用于模仿口吻与语感：", ""]
    for s in samples:
        lines.append(f"- **{s.get('label', '片段')}**：{s.get('text', '')}")
    lines.append("")
    return "\n".join(lines)


def _render_wordcloud(profile: dict, path: str, words_limit: int = 2000) -> bool:
    """用 wordcloud 生成 assets/wordcloud.png，失败时返回 False。"""
    freq = profile.get("_word_freq") or {}
    if not freq:
        freq = {w: 1 for w in profile.get("top_words") or []}
    if not freq:
        return False
    words_limit = min(max(int(words_limit), 100), 10000)
    if len(freq) > words_limit:
        freq = dict(sorted(freq.items(), key=lambda x: -x[1])[:words_limit])
    font = find_cn_font()
    if not font:
        return False
    try:
        import matplotlib
        matplotlib.use("Agg")
        from wordcloud import WordCloud
    except Exception:
        return False
    try:
        wc = WordCloud(
            font_path=font,
            width=1200,
            height=800,
            background_color="white",
            max_words=200,
            collocations=False,
            prefer_horizontal=0.95,
            random_state=7,
        )
        wc.generate_from_frequencies(freq).to_file(path)
        return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception:
        return False
