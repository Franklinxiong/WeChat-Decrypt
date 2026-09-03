# -*- coding: utf-8 -*-
"""统计聚合函数。所有函数容错：表缺失/列缺失返回空 DataFrame 或 0，不抛异常。"""
from __future__ import annotations

from datetime import datetime, timezone

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


def top_senders(dfs: dict[str, pd.DataFrame], n: int = 10) -> pd.DataFrame:
    """按 sender_slot 分组消息数 topN。"""
    msgs = _get(dfs, "messages")
    if msgs.empty or "sender_slot" not in msgs.columns:
        return pd.DataFrame(columns=["sender_slot", "count"])
    grp = msgs[msgs["sender_slot"] >= 0].groupby("sender_slot").size().rename("count").reset_index()
    return grp.sort_values("count", ascending=False).head(n).reset_index(drop=True)
