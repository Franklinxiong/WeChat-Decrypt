# -*- coding: utf-8 -*-
"""统计聚合函数。所有函数容错：表缺失/列缺失返回空 DataFrame 或 0，不抛异常。"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

EMPTY = pd.DataFrame()


def _get(dfs: dict[str, pd.DataFrame], key: str) -> pd.DataFrame:
    df = dfs.get(key)
    return df if df is not None else EMPTY


def total_metrics(dfs: dict[str, pd.DataFrame]) -> dict:
    """总消息数 / 会话数 / 发送者数 / 时间跨度(天) / 日均。"""
    msgs = _get(dfs, "messages")
    sessions = _get(dfs, "sessions")
    senders = _get(dfs, "senders")
    metrics = {
        "总消息数": int(len(msgs)),
        "会话数": int(len(sessions)) if not sessions.empty else 0,
        "发送者数": int(len(senders)) if not senders.empty else 0,
        "时间跨度(天)": 0,
        "日均消息数": 0.0,
    }
    if not msgs.empty and "create_time" in msgs.columns:
        ts = pd.to_numeric(msgs["create_time"], errors="coerce").dropna()
        if not ts.empty:
            span = (ts.max() - ts.min()) / 86400.0
            metrics["时间跨度(天)"] = round(float(span), 1)
            if span > 0:
                metrics["日均消息数"] = round(len(ts) / span, 1)
    return metrics


def messages_by_day(dfs: dict[str, pd.DataFrame], freq: str = "D") -> pd.DataFrame:
    """按天计数（保持稀疏，不填充 0）。返回 index=day, 列 count。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return pd.DataFrame(columns=["count"])
    ts = pd.to_numeric(msgs["create_time"], errors="coerce").dropna()
    if ts.empty:
        return pd.DataFrame(columns=["count"])
    day = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(None).dt.floor("D")
    return day.value_counts().sort_index().rename("count").to_frame()


def messages_by_hour(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """0-23 时分布。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return pd.DataFrame(index=range(24), columns=["count"], data=0)
    ts = pd.to_numeric(msgs["create_time"], errors="coerce").dropna()
    if ts.empty:
        return pd.DataFrame(index=range(24), columns=["count"], data=0)
    hour = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(None).dt.hour
    series = hour.value_counts().sort_index()
    out = series.reindex(range(24)).fillna(0).astype(int).rename("count").to_frame()
    out.index.name = "hour"
    return out


def messages_by_weekday(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """按星期分布（周一=0 ... 周日=6）。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return pd.DataFrame(index=range(7), columns=["count"], data=0)
    ts = pd.to_numeric(msgs["create_time"], errors="coerce").dropna()
    if ts.empty:
        return pd.DataFrame(index=range(7), columns=["count"], data=0)
    wd = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(None).dt.weekday
    out = wd.value_counts().reindex(range(7)).fillna(0).astype(int).rename("count").to_frame()
    out.index.name = "weekday"
    return out


def type_distribution(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """local_type 分组计数 + type_name。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "local_type" not in msgs.columns:
        return pd.DataFrame(columns=["local_type", "type_name", "count"])
    grp = msgs.groupby("local_type", dropna=False).size().rename("count").reset_index()
    if "type_name" in msgs.columns:
        tname = msgs[["local_type", "type_name"]].drop_duplicates("local_type")
        grp = grp.merge(tname, on="local_type", how="left")
        grp["type_name"] = grp["type_name"].fillna("其他")
    else:
        grp["type_name"] = "其他"
    return grp.sort_values("count", ascending=False).reset_index(drop=True)


def top_sessions(dfs: dict[str, pd.DataFrame], n: int = 10) -> pd.DataFrame:
    """按 session_slot 分组消息数 topN。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "session_slot" not in msgs.columns:
        return pd.DataFrame(columns=["session_slot", "count"])
    grp = msgs.groupby("session_slot").size().rename("count").reset_index()
    return grp.sort_values("count", ascending=False).head(n).reset_index(drop=True)


def top_senders(dfs: dict[str, pd.DataFrame], n: int = 10, chattype: str = "all") -> pd.DataFrame:
    """按 sender_slot 分组消息数 topN。chattype: all/dm/group，群私隔离。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "sender_slot" not in msgs.columns:
        return pd.DataFrame(columns=["sender_slot", "count"])
    scope = msgs[_chattype_mask(dfs, msgs, chattype)]
    grp = scope[scope["sender_slot"] >= 0].groupby("sender_slot").size().rename("count").reset_index()
    return grp.sort_values("count", ascending=False).head(n).reset_index(drop=True)


# ============================================================
# 行为学指标（借鉴 WeFlow-5.1.0 的 analyticsService 与页面组件）
# 所有函数均容错：表缺失 / 列缺失返回空 DataFrame 或 0，不抛异常。
# ============================================================

import re


def _sent_mask(msgs: pd.DataFrame) -> pd.Series:
    """判定"自己发送"：优先 is_send 列，兼容旧库（sender_slot 为空/无效）。"""
    if "is_send" in msgs.columns:
        return msgs["is_send"].fillna(0).astype(int) == 1
    if "sender_slot" in msgs.columns:
        col = pd.to_numeric(msgs["sender_slot"], errors="coerce")
        return col.isna() | (col < 0)
    return pd.Series(False, index=msgs.index)


def _local_ts(msgs: pd.DataFrame) -> pd.Series:
    """epoch 秒 -> 本机本地时区的 naive datetime。"""
    ts = pd.to_numeric(msgs["create_time"], errors="coerce").dropna()
    if ts.empty:
        return pd.Series(dtype="datetime64[ns]")
    s = pd.to_datetime(ts, unit="s", utc=True)
    try:
        from datetime import datetime
        tz = datetime.now().astimezone().tzinfo
        s = s.dt.tz_convert(tz)
    except Exception:
        pass
    return s.dt.tz_localize(None)


def sent_received(dfs: dict[str, pd.DataFrame]) -> dict:
    """收发比：自己发送 / 对方发送（接收）消息数及占比。"""
    msgs = _get(dfs, "messages")
    empty = {"sent": 0, "received": 0, "total": 0, "sent_ratio": 0.0, "received_ratio": 0.0}
    if msgs.empty:
        return empty
    sent = int(_sent_mask(msgs).sum())
    total = int(len(msgs))
    return {
        "sent": sent,
        "received": total - sent,
        "total": total,
        "sent_ratio": round(sent / total, 3) if total else 0.0,
        "received_ratio": round((total - sent) / total, 3) if total else 0.0,
    }


def daily_activity(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """逐日 发送/接收 数量（补零连续日轴）。index=day, 列=[sent, received]。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return pd.DataFrame(columns=["sent", "received"])
    local = _local_ts(msgs)
    if local.empty:
        return pd.DataFrame(columns=["sent", "received"])
    sent = _sent_mask(msgs).astype(int)
    df = pd.DataFrame({"day": local.dt.floor("D"), "sent": sent.values, "received": (1 - sent.values)})
    g = df.groupby("day").sum()
    if not g.index.empty:
        idx = pd.date_range(g.index.min(), g.index.max(), freq="D")
        g = g.reindex(idx, fill_value=0)
    g["sent"] = g["sent"].astype(int)
    g["received"] = g["received"].astype(int)
    return g


def activity_series(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """活跃强度折线数据：逐日发送量 + 日均基线 + 7日均线 + 峰值标记。
    返回 index=day, 列=[sent, base, ma7, is_peak]。"""
    g = daily_activity(dfs)
    if g.empty or "sent" not in g.columns:
        return pd.DataFrame(columns=["sent", "base", "ma7", "is_peak"])
    out = g[["sent"]].copy()
    days = max(int(len(out)), 1)
    total = int(out["sent"].sum())
    out["base"] = round(total / days, 2)
    out["ma7"] = out["sent"].rolling(7, min_periods=1).mean().round(1)
    mx = out["sent"].max()
    out["is_peak"] = (out["sent"] == mx) & (mx > 0)
    return out


def longest_streak(dfs: dict[str, pd.DataFrame]) -> dict:
    """最长连续聊天天数：按日去重后，相邻自然日即 +1。返回 dict。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return {"days": 0, "start": None, "end": None, "active_days": 0}
    local = _local_ts(msgs)
    if local.empty:
        return {"days": 0, "start": None, "end": None, "active_days": 0}
    days = sorted(set(pd.to_datetime(local).dt.normalize()))
    active = len(days)
    best = 1
    best_start = best_end = days[0]
    cur = 1
    cs = ce = days[0]
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days == 1:
            cur += 1
            ce = days[i]
            if cur > best:
                best, best_start, best_end = cur, cs, ce
        else:
            cur, cs, ce = 1, days[i], days[i]
    return {
        "days": best,
        "start": str(best_start.date()),
        "end": str(best_end.date()),
        "active_days": active,
    }


def late_night_stats(dfs: dict[str, pd.DataFrame], top: int = 5) -> dict:
    """深夜（0-6 点）消息统计：数量、占比、按发送者 TopN。"""
    msgs = _get(dfs, "messages")
    base = {"total": 0, "count": 0, "share": 0.0, "top": [], "hours": None}
    if msgs.empty or "create_time" not in msgs.columns:
        return base
    local = _local_ts(msgs)
    if local.empty:
        return base
    hour = local.dt.hour
    late = hour < 6
    base["total"] = int(len(msgs))
    base["count"] = int(late.sum())
    base["share"] = round(base["count"] / base["total"], 3) if base["total"] else 0.0

    # 按发送者统计（sender_display_name 优先，None/自己 -> "自己"）
    if "sender_display_name" in msgs.columns:
        label = msgs["sender_display_name"].fillna("自己")
    else:
        label = pd.Series("自己", index=msgs.index)
        if "sender_slot" in msgs.columns:
            sl = pd.to_numeric(msgs["sender_slot"], errors="coerce")
            idx = sl.notna() & (sl >= 0)
            label[idx] = "未知"
    cnt = label[late].value_counts().head(top).to_dict()
    names = dfs.get("senders", pd.DataFrame())
    if not names.empty and {"sender_slot", "display_name"}.issubset(names.columns):
        name_map = dict(zip(names["sender_slot"], names["display_name"]))
    base["top"] = cnt
    return base


def conversation_initiative(dfs: dict[str, pd.DataFrame]) -> dict:
    """对话主动性：会话内按时间排序，间隔 >3600s 切新对话；新对话首条为 sent 计 initiated。"""
    msgs = _get(dfs, "messages")
    base = {"total": 0, "initiated": 0, "ratio": 0.0, "avg_msgs": 0.0}
    if msgs.empty or "create_time" not in msgs.columns or "session_slot" not in msgs.columns:
        return base
    local = _local_ts(msgs)
    if local.empty:
        return base
    sent = _sent_mask(msgs)
    df = pd.DataFrame({
        "session": msgs["session_slot"].fillna(-1).astype(int).values,
        "t": (local.values.astype("datetime64[ns]").astype("int64") / 1e9).astype(float),
        "sent": sent.astype(int).values,
    })
    df = df.sort_values(["session", "t"]).reset_index(drop=True)
    if df.empty:
        return base
    sess = df["session"].to_numpy()
    t = df["t"].to_numpy()
    is_start = np.zeros(len(df), dtype=bool)
    is_start[0] = True
    for i in range(1, len(df)):
        if sess[i] != sess[i - 1] or (t[i] - t[i - 1]) > 3600:
            is_start[i] = True
    started_sent = int((is_start & (df["sent"].to_numpy() == 1)).sum())
    n_conv = int(is_start.sum())
    base["total"] = n_conv
    base["initiated"] = started_sent
    base["ratio"] = round(started_sent / n_conv, 3) if n_conv else 0.0
    base["avg_msgs"] = round(len(df) / n_conv, 2) if n_conv else 0.0
    return base


def response_speed(dfs: dict[str, pd.DataFrame]) -> dict:
    """回应速度：会话内相邻方向翻转（自己↔对方）且间隔 <86400s 的平均/中位秒数。"""
    msgs = _get(dfs, "messages")
    base = {"count": 0, "avg_seconds": 0.0, "median_seconds": 0.0, "avg_minutes": 0.0}
    if msgs.empty or "create_time" not in msgs.columns or "session_slot" not in msgs.columns:
        return base
    local = _local_ts(msgs)
    if local.empty:
        return base
    sent = _sent_mask(msgs).astype(int).values
    sess = msgs["session_slot"].fillna(-1).astype(int).values
    t = local.values.astype("datetime64[ns]").astype("int64") / 1e9
    order = np.lexsort((t, sess))
    sess = sess[order]
    sent = sent[order]
    t = t[order]
    gaps = []
    for i in range(1, len(t)):
        if sess[i] == sess[i - 1] and sent[i] != sent[i - 1]:
            g = t[i] - t[i - 1]
            if 1 <= g < 86400:
                gaps.append(g)
    if not gaps:
        return base
    arr = np.array(gaps)
    avg = float(arr.mean())
    med = float(np.median(arr))
    return {
        "count": len(gaps),
        "avg_seconds": round(avg, 1),
        "median_seconds": round(med, 1),
        "avg_minutes": round(avg / 60, 1),
    }


def heatmap_7x24(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """7×24 活跃热力矩阵：weekday(0=周一..6=周日) × hour(0-23)。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return pd.DataFrame(index=range(7), columns=range(24), data=0)
    local = _local_ts(msgs)
    if local.empty:
        return pd.DataFrame(index=range(7), columns=range(24), data=0)
    wd = local.dt.weekday
    hr = local.dt.hour
    cts = pd.crosstab(wd, hr)
    cts = cts.reindex(index=range(7), columns=range(24), fill_value=0)
    cts.index.name = "weekday"
    cts.columns.name = "hour"
    return cts


def frequent_short_phrases(dfs: dict[str, pd.DataFrame], top: int = 20) -> pd.DataFrame:
    """高频短句：仅自己发送的文本消息，去除 URL/XML/中括号噪声，保留 2-20 字符，topN。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "content_text" not in msgs.columns:
        return pd.DataFrame(columns=["phrase", "count"])
    sent = _sent_mask(msgs)
    textmask = pd.Series(True, index=msgs.index)
    if "local_type" in msgs.columns:
        lt = pd.to_numeric(msgs["local_type"], errors="coerce")
        textmask = lt.isin([1, 2])
    sub = msgs[sent & textmask]
    if sub.empty:
        return pd.DataFrame(columns=["phrase", "count"])

    def clean(x: str) -> str:
        if not isinstance(x, str):
            return ""
        x = re.sub(r"https?://\S+|www\.\S+", "", x)
        x = re.sub(r"<[^>]+>", "", x)
        x = re.sub(r"\[[^\]]*\]", "", x)
        x = x.replace(" ", "").replace("\n", "").replace("\r", "")
        return x.strip()

    phrases = sub["content_text"].map(clean)
    phrases = phrases[(phrases.str.len() >= 2) & (phrases.str.len() <= 20)]
    if phrases.empty:
        return pd.DataFrame(columns=["phrase", "count"])
    cnt = phrases.value_counts().head(top).rename("count")
    return cnt.rename_axis("phrase").reset_index()


def messages_by_month(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """按 年-月 计数（YYYY-MM 连续轴）。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return pd.DataFrame(columns=["count"])
    local = _local_ts(msgs)
    if local.empty:
        return pd.DataFrame(columns=["count"])
    ym = local.dt.to_period("M")
    series = ym.value_counts().sort_index()
    if not series.empty:
        idx = pd.period_range(series.index.min(), series.index.max(), freq="M")
        series = series.reindex(idx, fill_value=0)
    out = series.rename("count").to_frame()
    out.index = out.index.astype(str)
    out.index.name = "month"
    return out


# ============================================================
# 增强分析：词云 / 日历热力图 / 情感分析 / 个性关键词(TF-IDF) /
#           双人关系 / 年度报告（全部手写规则，无 sklearn/LLM）
# 依赖可选：jieba / snownlp 缺失时相关函数返回空，不抛异常。
# ============================================================

import math

try:
    import jieba as _jieba
    _HAS_JIEBA = True
except Exception:
    _jieba = None
    _HAS_JIEBA = False

try:
    from snownlp import SnowNLP as _SnowNLP
    _HAS_SNOWNLP = True
except Exception:
    _SnowNLP = None
    _HAS_SNOWNLP = False

# 常用中文停用词（虚词、语气词、单字、微信噪声）
STOPWORDS = set(
    """的 了 是 在 和 有 我 你 他 她 它 我们 你们 他们 就 不 也 都 会 说 到 去
    要 着 好 没 这 那 这个 那个 这种 这些 那些 一样 一点 一个 一下 自己
    人 东西 时间 还有 这么 那么 什么 怎么 为什么 谁 哪里 多少 是否 因为
    所以 不是 不要 没有 就是 可以 可能 应该 现在 时候 已经 知道 觉得 真的
    但是 还是 只是 不过 然后 而且 或者 其实 因此 于是 如果 虽然 尽管 大家
    一起 出来 起来 过来 进去 回来 过去 下来 上来 包括 关于 对于 其中 之一
    等等 以及 并且 这样 那样 每天 今天 明天 昨天 然后 反正 居然 竟然 确实
    好像 感觉 认为 讲 看 想 做 拿 放 给 被 把 让 跟 从 向 往 对 或 与 及
    但 而 则 所 按 比 由 依 凭 中 上 呀 啊 嗯 哦 呢 吗 吧 嘛 哈 啦 唉 哎
    诶 呃 噢 唔 嘿 喂 呗 咋 哟 嘞 噻 哈哈 呵呵 嘻嘻 嘿嘿 哈哈哈哈 呵呵呵
    哈哈哈 嗯嗯 嗯嗯嗯 哦哦 啊啊 啦啊 nbsp url img br p div span 微信 消息
    图片 表情 链接 视频 语音 撤回 系统 转发了 该消息 网络异常 请先添加对方
    拍了拍 你撤回了一条消息""".split()
)


def _clean_text_series(series: pd.Series) -> pd.Series:
    """文本清理：去 URL / XML / 非中英文字符，统一小写。供分词使用。"""

    def _one(x):
        if not isinstance(x, str):
            return ""
        x = re.sub(r"https?://\S+|www\.\S+", " ", x)
        x = re.sub(r"<[^>]+>", " ", x)
        x = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9]+", " ", x)
        return " ".join(x.lower().split())

    return series.map(_one)


_TEXT_PLACEHOLDER = {"[图片]", "[语音]", "[视频]", "[表情]", "[位置]",
                     "[文件/链接]", "[文件]", "[链接]", "[音乐]", "[红包]"}


def _text_mask(msgs: pd.DataFrame) -> pd.Series:
    """纯文本消息掩码：文本类型 / 有效正文 / 非占位符。"""
    if msgs.empty or "content_text" not in msgs.columns:
        return pd.Series(False, index=msgs.index)
    mask = pd.Series(True, index=msgs.index)
    if "local_type" in msgs.columns:
        lt = pd.to_numeric(msgs["local_type"], errors="coerce")
        mask &= lt.isin([1, 2])
    else:
        mask &= msgs["type_name"].fillna("").astype(str).str.contains("文本|Text", na=False)
    txt = msgs["content_text"].fillna("").astype(str).str.strip()
    mask &= txt.str.len() > 0
    mask &= ~txt.isin(_TEXT_PLACEHOLDER)
    return mask


def _sender_mask(msgs: pd.DataFrame, sender) -> pd.Series:
    """发送者过滤掩码。sender 为 None(不限) / 'self' / int(slot) / 用户名 str。"""
    if sender is None:
        return pd.Series(True, index=msgs.index)
    if sender == "self":
        return _sent_mask(msgs)
    if isinstance(sender, (int, float)):
        sl = pd.to_numeric(msgs.get("sender_slot", pd.Series(-1, index=msgs.index)), errors="coerce")
        return sl == int(sender)
    s = str(sender)
    if s.isdigit() and "sender_slot" in msgs.columns:
        sl = pd.to_numeric(msgs["sender_slot"], errors="coerce")
        return sl == int(s)
    if "sender_username" in msgs.columns:
        return msgs["sender_username"].fillna("").astype(str) == s
    return pd.Series(False, index=msgs.index)


def _session_is_group(dfs: dict[str, pd.DataFrame], msgs: pd.DataFrame) -> pd.Series:
    """逐行判定 msgs 所属会话是否群聊。优先 session_username 后缀 '@chatroom'，
    找不到再用 sessions 表的 is_group（按 session_slot 映射）。"""
    if not msgs.empty and "session_username" in msgs.columns:
        return msgs["session_username"].fillna("").astype(str).str.endswith("@chatroom")
    sessions = _get(dfs, "sessions")
    if not sessions.empty and {"session_slot", "is_group"}.issubset(sessions.columns):
        m = dict(zip(pd.to_numeric(sessions["session_slot"], errors="coerce"),
                     pd.to_numeric(sessions["is_group"], errors="coerce")))
        sl = pd.to_numeric(msgs["session_slot"], errors="coerce")
        return sl.map(m).fillna(0).astype(bool)
    return pd.Series(False, index=msgs.index)


def _chattype_mask(dfs: dict[str, pd.DataFrame], msgs: pd.DataFrame | None = None,
                   chattype: str = "all") -> pd.Series:
    """会话类型过滤掩码。chattype: 'all'（不限制）/ 'dm'（仅私聊）/ 'group'（仅群聊）。

    群聊（sessions.is_group=1 / 会话名以 @chatroom 结尾）与私聊按会话归属严格分开，
    确保同一成员在群聊与私聊中的发言可被区分计算。
    """
    if msgs is None:
        msgs = _get(dfs, "messages")
    mask = pd.Series(True, index=msgs.index)
    if chattype in ("dm", "group"):
        g = _session_is_group(dfs, msgs)
        mask = g if chattype == "group" else (~g)
    return mask


def text_messages(dfs: dict[str, pd.DataFrame], session=None, sender=None,
                  chattype: str = "all") -> pd.DataFrame:
    """纯文本消息筛选（内部复用）。返回 messages 子集（保留原始列）。

    chattype: 'all' / 'dm'(仅私聊) / 'group'(仅群聊)，用于群私隔离。
    """
    msgs = _get(dfs, "messages")
    if msgs.empty:
        return msgs
    mask = _text_mask(msgs)
    if session is not None and "session_slot" in msgs.columns:
        sl = pd.to_numeric(msgs["session_slot"], errors="coerce")
        mask &= sl == int(session)
    mask &= _sender_mask(msgs, sender)
    mask &= _chattype_mask(dfs, msgs, chattype)
    return msgs.loc[mask].copy()


def word_frequency(dfs, session=None, sender=None, top: int = 100, max_msgs: int = 50000,
                   chattype: str = "all") -> pd.DataFrame:
    """jieba 分词词频表（过滤停用词/URL/XML/单字/刷屏重复词）。

    超大语料（全站）随机抽样 max_msgs 条控制耗时，适用词云展示。
    返回 [word, count] topN。chattype 用于群聊/私聊隔离。
    """
    empty = pd.DataFrame(columns=["word", "count"])
    if not _HAS_JIEBA:
        return empty
    sub = text_messages(dfs, session=session, sender=sender, chattype=chattype)
    if sub.empty:
        return empty
    texts = sub["content_text"].astype(str)
    if len(texts) > max_msgs:
        texts = texts.sample(max_msgs, random_state=7)
    cleaned = _clean_text_series(texts)
    counter: dict = {}
    for c in cleaned:
        if not c:
            continue
        seen = set()
        for w in _jieba.cut(c):
            w = w.strip()
            if len(w) < 2 or w in STOPWORDS:
                continue
            if not re.search(r"[\u4e00-\u9fff\u3400-\u4dbfA-Za-z]", w):
                continue
            if w in seen:      # 单条消息内去重，降低刷屏词权重
                continue
            seen.add(w)
            counter[w] = counter.get(w, 0) + 1
    if not counter:
        return empty
    srs = pd.Series(counter).sort_values(ascending=False).head(top)
    out = srs.rename_axis("word").reset_index()
    out.columns = ["word", "count"]
    return out


def calendar_heatmap(dfs, year: int) -> pd.DataFrame:
    """GitHub 风格年历活跃矩阵：index=weekday(0周一..6周日) × columns=ISO周(1..max)。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return pd.DataFrame()
    local = _local_ts(msgs)
    if local.empty:
        return pd.DataFrame()
    in_year = local.dt.year == int(year)
    if not in_year.any():
        return pd.DataFrame()
    d = local[in_year]
    wd = d.dt.weekday
    weeks = d.dt.isocalendar().week.astype(int)
    # 两处全年 12/31 可能归入下一年第1周，强制归位本年第52/53周
    weeks = weeks.where(weeks != 1, weeks.max if False else weeks)  # noqa: 交给下方 reindex 兜底
    cts = pd.crosstab(wd, weeks)
    mx = int(weeks.max()) if len(weeks) else 53
    cts = cts.reindex(index=range(7), columns=range(0, mx + 1), fill_value=0)
    cts = cts.drop(columns=[0], errors="ignore") if 0 in cts.columns else cts
    cts.index.name = "weekday"
    cts.columns.name = "week"
    return cts


def _tokenize_dedup(texts: pd.Series) -> dict:
    """对文本逐条 jieba 切词（单条消息内去重），返回 dict[词]=出现消息数。"""
    cnt: dict = {}
    cleaned = _clean_text_series(texts)
    for c in cleaned:
        if not c:
            continue
        seen = set()
        for w in _jieba.cut(c):
            w = w.strip()
            if len(w) < 2 or w in STOPWORDS:
                continue
            if not re.search(r"[\u4e00-\u9fff\u3400-\u4dbfA-Za-z]", w):
                continue
            if w in seen:
                continue
            seen.add(w)
            cnt[w] = cnt.get(w, 0) + 1
    return cnt


def tfidf_top_words(dfs, sender, top: int = 20, bgs: int = 300, per_doc: int = 200,
                    chattype: str = "all") -> pd.DataFrame:
    """手写 TF-IDF：文档集=最活跃 N 个发送者(+目标)的消息词集合，返回目标发送者区分度词。

    - 不使用 sklearn：idf = ln((1+N)/(1+df)) + 1 平滑；score = tf * idf。
    - 背景语料每文档采样 per_doc 条、最多 bgs 个文档，控制性能。
    - chattype: all/dm/group——目标发送者与背景语料均限定在同一会话类型内，
      使同一成员在群聊与私聊中的发言互不渗透。
    返回 [word, tfidf, freq]。
    """
    empty = pd.DataFrame(columns=["word", "tfidf", "freq"])
    if not _HAS_JIEBA:
        return empty
    msgs = _get(dfs, "messages")
    if msgs.empty or "content_text" not in msgs.columns:
        return empty
    tm = _text_mask(msgs)
    ct = _chattype_mask(dfs, msgs, chattype)
    key = pd.Series("self", index=msgs.index, dtype=object)
    if "sender_slot" in msgs.columns:
        sl = pd.to_numeric(msgs["sender_slot"], errors="coerce")
        ok = sl.notna() & (sl >= 0)
        key[ok] = sl[ok].astype(int)
    elif "sender_username" in msgs.columns:
        key = msgs["sender_username"].fillna("self").astype(str)
    if isinstance(sender, (int, float)):
        target = int(sender)
    else:
        target = str(sender)
    sel = tm & ct & (key == target)
    if not sel.any():
        return empty
    target_texts = msgs.loc[sel, "content_text"].astype(str)
    if len(target_texts) > per_doc * 3:
        target_texts = target_texts.sample(per_doc * 3, random_state=7)

    # 背景文档：同一会话类型内除目标外最活跃的 bgs 个 key，各采样 per_doc 条
    docs: dict = {}
    sub = msgs.loc[tm & ct & ~sel, ["content_text"]].copy()
    if not sub.empty:
        sub["_key"] = key[tm & ct & ~sel]
        sizes = sub.groupby("_key").size().sort_values(ascending=False)
        keep = set(sizes.head(bgs).index)
        sub = sub[sub["_key"].isin(keep)]
        for k, g in sub.groupby("_key"):
            docs[k] = _tokenize_dedup(g["content_text"].astype(str))

    docs[target] = _tokenize_dedup(target_texts)
    tgt = docs[target]
    if not tgt:
        return empty
    n_docs = len(docs)
    df_all: dict = {}
    for cnt in docs.values():
        for w in cnt:
            df_all[w] = df_all.get(w, 0) + 1
    rows = []
    for w, tf in tgt.items():
        idf = math.log((1 + n_docs) / (1 + df_all.get(w, 0))) + 1.0
        rows.append((w, round(tf * idf, 3), tf))
    if not rows:
        return empty
    out = pd.DataFrame(rows, columns=["word", "tfidf", "freq"])
    out = out.sort_values("tfidf", ascending=False).head(top).reset_index(drop=True)
    return out


def sentiment_by_period(dfs, freq: str = "ME", max_total: int = 4000, seed: int = 7,
                        chattype: str = "all") -> pd.DataFrame:
    """文本消息逐期（月ME/周W）情感聚合：均值 + 正负计数。

    SnowNLP 逐条打分成本高，全库抽样 max_total 条控制耗时（默认4000）。
    返回 [period, avg_sentiment, pos, neg, total]。0.55 以上计正向、0.45 以下计负向。
    chattype: all/dm/group，群私隔离。
    """
    empty = pd.DataFrame(columns=["period", "avg_sentiment", "pos", "neg", "total"])
    if not _HAS_SNOWNLP:
        return empty
    sub = text_messages(dfs, chattype=chattype)
    if sub.empty or "create_time" not in sub.columns:
        return empty
    local = _local_ts(sub)
    if local.empty:
        return empty
    # pandas: to_period 使用 'M'/'W'，resample 语义的 'ME' 需映射回 'M'
    period_freq = "M" if freq == "ME" else freq
    df = pd.DataFrame({
        "text": sub["content_text"].astype(str).values,
        "period": local.dt.to_period(period_freq).astype(str),
    })
    if len(df) > max_total:
        df = df.sample(max_total, random_state=seed)
    if df.empty:
        return empty
    rows = []
    for p, g in df.groupby("period", sort=False):
        scores = []
        for txt in g["text"]:
            if not txt or len(txt) > 160:
                continue
            try:
                scores.append(_SnowNLP(txt[:160]).sentiments)
            except Exception:
                continue
        if not scores:
            continue
        arr = np.array(scores)
        rows.append({
            "period": p,
            "avg_sentiment": round(float(arr.mean()), 3),
            "pos": int((arr > 0.55).sum()),
            "neg": int((arr < 0.45).sum()),
            "total": int(len(scores)),
        })
    return pd.DataFrame(rows)


def _freq_phrases(texts: pd.Series, top: int = 40) -> pd.DataFrame:
    """通用高频短句（用于双人关系等，不限"自己发送"）。"""

    def clean(x):
        if not isinstance(x, str):
            return ""
        x = re.sub(r"https?://\S+|www\.\S+", "", x)
        x = re.sub(r"<[^>]+>", "", x)
        x = re.sub(r"\[[^\]]*\]", "", x)
        x = x.replace(" ", "").replace("\n", "")
        return x.strip()

    ph = texts.map(clean)
    ph = ph[(ph.str.len() >= 2) & (ph.str.len() <= 20)]
    if ph.empty:
        return pd.DataFrame(columns=["phrase", "count"])
    c = ph.value_counts().head(top)
    out = c.rename_axis("phrase").reset_index()
    out.columns = ["phrase", "count"]
    return out


def pair_stats(dfs, other) -> dict:
    """双人关系统计：某一对话者(other) vs 用户本人(self) 的往来关系。

    仅统计私聊（dm）会话内部的消息：群聊中成员发言与私聊严格隔离，
    不参与"双人关系"，避免同一成员在群聊与私聊中的发言互相污染。
    本人判定沿用统一规则：is_send=1 或 sender_slot 为空。

    返回 dict: counts{other, self, total}, reply{pairs,median_seconds,avg_seconds},
    monthly[month,other,self], common_phrases[phrase,count_other,count_self], longest_streak。
    """
    base = {
        "counts": {"other": 0, "self": 0, "total": 0},
        "reply": {"pairs": 0, "median_seconds": 0.0, "avg_seconds": 0.0},
        "monthly": pd.DataFrame(columns=["month", "other", "self"]),
        "common_phrases": pd.DataFrame(columns=["phrase", "count_other", "count_self"]),
        "longest_streak": 0,
    }
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns or "session_slot" not in msgs.columns:
        return base
    dm = _chattype_mask(dfs, msgs, "dm")
    mo = dm & _sender_mask(msgs, other)
    # 本人消息须限定在与对话者相同的私聊会话内，否则会混入其他会话的"我"
    pair_sessions = pd.to_numeric(msgs.loc[mo, "session_slot"], errors="coerce").dropna().unique()
    in_pair = msgs["session_slot"].isin(set(pair_sessions))
    ms = dm & in_pair & _sent_mask(msgs)  # 用户本人（仅该对话者会话内）
    no = int(mo.sum())
    ns = int(ms.sum())
    base["counts"] = {"other": no, "self": ns, "total": no + ns}
    local = _local_ts(msgs)
    if local.empty:
        return base
    dirs = np.zeros(len(msgs), dtype=int)
    dirs[mo.to_numpy()] = 1      # 对话者
    dirs[ms.to_numpy()] = -1     # 用户本人
    seq = pd.DataFrame({
        "session": msgs["session_slot"].fillna(-1).astype(int).to_numpy(),
        "t": local.to_numpy().astype("datetime64[ns]").astype("int64") / 1e9,
        "d": dirs,
    }, index=msgs.index)
    seq = seq[seq["d"] != 0]
    if seq.empty:
        return base
    seq = seq.sort_values(["session", "t"])
    d = seq["d"].to_numpy()
    # 最大长连击（同一侧连续发言）
    best = cur = 1
    for i in range(1, len(d)):
        if d[i] == d[i - 1]:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 1
    base["longest_streak"] = best
    # 应答耗时：同一私聊会话内方向翻转，间隔 <86400s
    sess = seq["session"].to_numpy()
    t = seq["t"].to_numpy()
    gaps = []
    for i in range(1, len(t)):
        if sess[i] == sess[i - 1] and d[i] != d[i - 1]:
            g = t[i] - t[i - 1]
            if 1 <= g < 86400:
                gaps.append(g)
    if gaps:
        arr = np.array(gaps)
        base["reply"] = {
            "pairs": len(gaps),
            "median_seconds": round(float(np.median(arr)), 1),
            "avg_seconds": round(float(arr.mean()), 1),
        }
    # 月度轨迹
    seq_m = seq.copy()
    seq_m["month"] = local.loc[seq.index].dt.to_period("M").astype(str)
    o_m = seq_m[seq_m["d"] == 1].groupby("month").size()
    s_m = seq_m[seq_m["d"] == -1].groupby("month").size()
    if not o_m.empty or not s_m.empty:
        ml = pd.DataFrame({"month": o_m.index.union(s_m.index)})
        ml["other"] = ml["month"].map(o_m).fillna(0).astype(int)
        ml["self"] = ml["month"].map(s_m).fillna(0).astype(int)
        base["monthly"] = ml.sort_values("month").reset_index(drop=True)
    # 共同高频短语（仅私聊内双方）
    tm = _text_mask(msgs)
    po = _freq_phrases(msgs.loc[mo & tm, "content_text"], top=40) if (mo & tm).any() and "content_text" in msgs.columns else pd.DataFrame(columns=["phrase", "count"])
    ps = _freq_phrases(msgs.loc[ms & tm, "content_text"], top=40) if (ms & tm).any() and "content_text" in msgs.columns else pd.DataFrame(columns=["phrase", "count"])
    if not po.empty and not ps.empty:
        co = po.set_index("phrase")["count"]
        cs = ps.set_index("phrase")["count"]
        inter = co.index.intersection(cs.index)
        if len(inter):
            base["common_phrases"] = pd.DataFrame({
                "phrase": inter,
                "count_other": co.loc[inter].astype(int).to_numpy(),
                "count_self": cs.loc[inter].astype(int).to_numpy(),
            }).sort_values("count_other", ascending=False).reset_index(drop=True)
    return base


def annual_report_data(dfs, year: int) -> dict:
    """年度报告数据聚合 dict：消息量/字符/活跃天数/最佳好友/峰值日/最长连续/熬夜/收发比/关键词/月度。"""
    out = {
        "year": int(year),
        "total_msgs": 0, "total_chars": 0, "active_days": 0,
        "best_friend": None, "peak_day": None, "peak_count": 0,
        "longest_streak_days": 0, "night_owl": None,
        "sent": 0, "received": 0, "sent_ratio": 0.0, "received_ratio": 0.0,
        "top_words": pd.DataFrame(columns=["word", "count"]),
        "monthly": pd.DataFrame(columns=["count"]),
    }
    msgs = _get(dfs, "messages")
    if msgs.empty or "create_time" not in msgs.columns:
        return out
    local = _local_ts(msgs)
    if local.empty:
        return out
    in_year = local.dt.year == int(year)
    if not in_year.any():
        return out
    sub = msgs.loc[in_year]
    out["total_msgs"] = int(len(sub))
    tm = _text_mask(sub)
    if "content_text" in sub.columns:
        out["total_chars"] = int(sub.loc[tm, "content_text"].fillna("").astype(str).str.len().sum())
    out["active_days"] = int(local[in_year].dt.date.nunique())
    sent = int(_sent_mask(sub).sum())
    out["sent"] = sent
    out["received"] = out["total_msgs"] - sent
    out["sent_ratio"] = round(sent / out["total_msgs"], 3) if out["total_msgs"] else 0.0
    out["received_ratio"] = round(out["received"] / out["total_msgs"], 3) if out["total_msgs"] else 0.0
    if "sender_display_name" in sub.columns:
        snd = sub[sub["sender_display_name"].notna() & (sub["sender_display_name"] != "")]["sender_display_name"]
        if not snd.empty:
            vc = snd.value_counts()
            out["best_friend"] = {"name": str(vc.index[0]), "count": int(vc.iloc[0])}
    day = local[in_year].dt.date
    vc = day.value_counts()
    if not vc.empty:
        out["peak_day"] = str(vc.index[0])
        out["peak_count"] = int(vc.iloc[0])
    out["longest_streak_days"] = longest_streak({"messages": sub})["days"]
    lns = late_night_stats({"messages": sub}, top=1)
    top_ln = lns.get("top") or {}
    if top_ln:
        k = next(iter(top_ln))
        out["night_owl"] = {"name": str(k), "count": int(top_ln[k])}
    out["top_words"] = word_frequency({"messages": sub}, top=20)
    out["monthly"] = messages_by_month({"messages": sub})
    return out
