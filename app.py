# -*- coding: utf-8 -*-
"""微信聊天记录分析 UI（Streamlit）。

仓库不携带任何数据。分析库由用户在本机解密后构建（见 scripts/build_analysis.py），
默认路径 ~/WeChatData/analysis.db，可在 config.json 修改，或用环境变量 WE_CHAT_DB 覆盖。
"""
from __future__ import annotations

import io
import json
import os

import pandas as pd
import plotly.express as px
import streamlit as st

import analysis.export as export
import analysis.loader as loader
import analysis.stats as stats

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _resolve_db() -> str:
    """数据源优先级：环境变量 WE_CHAT_DB > config.json 的 db_path > 默认外部路径。"""
    env = os.environ.get("WE_CHAT_DB", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    cfg = {}
    try:
        with open(os.path.join(PROJECT_ROOT, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return os.path.abspath(os.path.expanduser(cfg.get("db_path", "~/WeChatData/analysis.db")))


DEFAULT_DB = _resolve_db()
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


@st.cache_data(show_spinner="正在加载数据…")
def load(db_path: str):
    dfs = loader.load_all(db_path)
    meta = loader.load_meta(db_path)
    return dfs, meta


def main() -> None:
    st.set_page_config(page_title="微信聊天记录分析", page_icon="🧾", layout="wide")

    # 侧边栏 ---------------------------------------------------------
    with st.sidebar:
        st.header("数据源")
        st.markdown(f"**当前：** `{DEFAULT_DB}`")
        other = st.text_input("或连接其它库（绝对路径，留空用默认）", value="")
        db_path = os.path.abspath(os.path.expanduser(other)) if other.strip() else DEFAULT_DB
        if not os.path.exists(db_path):
            st.error(
                f"分析库不存在:\n`{db_path}`\n\n"
                "请先解密本机微信库并运行 `python3 scripts/build_analysis.py` 生成分析库，"
                "或点击「刷新数据」查看说明。"
            )
            st.stop()
        dfs, meta = load(db_path)

        # 时间范围过滤
        msgs = dfs.get("messages", pd.DataFrame())
        ts_series = (
            pd.to_numeric(msgs["create_time"], errors="coerce").dropna()
            if not msgs.empty and "create_time" in msgs.columns
            else pd.Series(dtype=float)
        )
        if not ts_series.empty:
            mn = pd.to_datetime(ts_series.min(), unit="s", utc=True).tz_convert(None)
            mx = pd.to_datetime(ts_series.max(), unit="s", utc=True).tz_convert(None)
            default = (mn.date(), mx.date())
            sel = st.date_input("时间范围过滤", value=default, min_value=mn.date(), max_value=mx.date())
            st.caption(f"数据范围 {mn.date()} ~ {mx.date()}")
        else:
            sel = None
            st.caption("（无时间数据）")
        refresh = st.button("刷新数据", use_container_width=True)
        if refresh:
            load.clear()
            st.rerun()

    # 过滤后的数据 -------------------------------------------------
    if sel is not None and not msgs.empty:
        lo = int(pd.Timestamp(sel[0]).timestamp() - 12 * 3600)  # 含时区余量
        hi = int(pd.Timestamp(sel[1] + pd.Timedelta(days=1)).timestamp())
        msgs_f = msgs[
            pd.to_numeric(msgs["create_time"], errors="coerce").between(lo, hi)
        ].copy()
    else:
        msgs_f = msgs.copy()
    dfs_f = dict(dfs)
    dfs_f["messages"] = msgs_f

    metrics = stats.total_metrics(dfs_f)

    # 会话/发送者显示名映射（真实库带 username 与 display_name）
    session_name = {}
    sdf = dfs_f.get("sessions", pd.DataFrame())
    if not sdf.empty and {"session_slot", "display_name"}.issubset(sdf.columns):
        session_name = dict(zip(sdf["session_slot"], sdf["display_name"]))
    sender_name = {}
    snddf = dfs_f.get("senders", pd.DataFrame())
    if not snddf.empty and {"sender_slot", "display_name"}.issubset(snddf.columns):
        sender_name = dict(zip(snddf["sender_slot"], snddf["display_name"]))

    def sname(slot):
        return session_name.get(slot, f"[会话 {slot}]")

    def ndname(slot):
        if slot is None:
            return "（无）"
        return sender_name.get(slot, f"[发送者 {slot}]")

    # 顶部 ---------------------------------------------------------
    st.title("微信聊天记录分析")
    st.caption("数据来自本机解密库构建的 `analysis.db`，仅存本机，请勿对外分发。")

    tabs = st.tabs(["总览", "会话分析", "时间趋势", "发送者", "明细浏览", "导出"])

    # ---- Tab1 总览 ----
    with tabs[0]:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("总消息数", f"{metrics['总消息数']:,}")
        c2.metric("会话数", metrics["会话数"])
        c3.metric("发送者数", metrics["发送者数"])
        c4.metric("时间跨度(天)", metrics["时间跨度(天)"])

        col_a, col_b = st.columns([3, 2])
        with col_a:
            st.subheader("消息量按天趋势")
            day_df = stats.messages_by_day(dfs_f)
            if not day_df.empty:
                day_df = day_df.reset_index()
                day_df.columns = ["day", "count"]
                fig = px.line(day_df, x="day", y="count", markers=True)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无按天数据")
        with col_b:
            st.subheader("消息类型分布")
            td = stats.type_distribution(dfs_f)
            if not td.empty:
                fig = px.pie(td, names="type_name", values="count")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无类型数据")

        with st.expander("查看 meta（数据口径）"):
            if meta:
                st.dataframe(pd.DataFrame(list(meta.items()), columns=["key", "value"]),
                             use_container_width=True)

    # ---- Tab2 会话分析 ----
    with tabs[1]:
        c_a, c_b = st.columns([2, 3])
        with c_a:
            st.subheader("Top 会话（按消息量）")
            tops = stats.top_sessions(dfs_f, n=10)
            if not tops.empty:
                tops = tops.copy()
                tops["会话"] = tops["session_slot"].map(sname)
                fig = px.bar(tops, x="会话", y="count")
                st.plotly_chart(fig, use_container_width=True)
        with c_b:
            st.subheader("会话概览")
            view = sdf
            if not view.empty and {"display_name", "is_group"}.issubset(view.columns):
                cols = ["display_name", "is_group", "last_timestamp", "msg_count_est"] \
                    if "msg_count_est" in view.columns \
                    else ["display_name", "is_group", "last_timestamp"]
                v2 = view[cols].copy()
                v2["is_group"] = v2["is_group"].map({1: "群聊", 0: "单聊"})
                v2["last_timestamp"] = pd.to_datetime(
                    v2["last_timestamp"], unit="s", utc=True).dt.tz_convert(None)
                v2["last_timestamp"] = v2["last_timestamp"].dt.strftime("%Y-%m-%d %H:%M")
                st.dataframe(v2.sort_values("msg_count_est", ascending=False),
                             use_container_width=True, hide_index=True)
            else:
                st.info("暂无会话数据")

    # ---- Tab3 时间趋势 ----
    with tabs[2]:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("按小时分布")
            hdf = stats.messages_by_hour(dfs_f)
            if not hdf.empty:
                fig = px.bar(hdf.reset_index(), x="hour", y="count")
                st.plotly_chart(fig, use_container_width=True)
        with c2:
            st.subheader("按星期分布")
            wdf = stats.messages_by_weekday(dfs_f)
            if not wdf.empty:
                wdf = wdf.reset_index()
                wdf["weekday"] = wdf["weekday"].map(lambda x: WEEKDAY_CN[x] if 0 <= x < 7 else x)
                fig = px.bar(wdf, x="weekday", y="count")
                st.plotly_chart(fig, use_container_width=True)

    # ---- Tab4 发送者 ----
    with tabs[3]:
        c_a, c_b = st.columns([2, 3])
        with c_a:
            st.subheader("Top 发送者（按消息量）")
            tsd = stats.top_senders(dfs_f, n=10)
            if not tsd.empty:
                tsd = tsd.copy()
                tsd["发送者"] = tsd["sender_slot"].map(ndname)
                fig = px.bar(tsd, x="count", y="发送者", orientation="h")
                st.plotly_chart(fig, use_container_width=True)
        with c_b:
            st.subheader("发送者消息量明细")
            snd = dfs_f.get("senders", pd.DataFrame())
            if not snd.empty and {"display_name", "msg_count"}.issubset(snd.columns):
                st.dataframe(
                    snd.sort_values("msg_count", ascending=False)[
                        ["display_name", "msg_count", "first_seen", "last_seen"]
                    ],
                    use_container_width=True, hide_index=True,
                )
            else:
                st.info("暂无发送者数据")

    # ---- Tab5 明细浏览 ----
    with tabs[4]:
        st.subheader("消息明细浏览")
        msgs_m = dfs_f.get("messages", pd.DataFrame())
        need = {"create_time", "type_name", "content_text", "session_slot", "sender_slot"}
        if msgs_m.empty or not need.issubset(msgs_m.columns):
            st.info("暂无消息明细数据")
        else:
            c1, c2, c3 = st.columns([2, 2, 1])
            sessions = sorted(msgs_m["session_slot"].dropna().unique())
            senders = sorted(msgs_m["sender_slot"].dropna().unique())
            sel_s = c1.selectbox("选择会话", options=sessions, format_func=sname,
                                 index=0) if sessions else None
            sel_n = c2.selectbox("选择发送者", options=[-1] + senders,
                                 format_func=lambda s: "（所有发送者）" if s == -1 else ndname(s),
                                 index=0) if senders else None
            limit = c3.selectbox("显示条数", [50, 200, 500])
            view = msgs_m
            if sel_s is not None:
                view = view[view["session_slot"] == sel_s]
            if sel_n is not None and sel_n != -1:
                view = view[view["sender_slot"] == sel_n]
            show = pd.DataFrame({
                "时间": pd.to_datetime(view["create_time"], unit="s", utc=True).dt.tz_convert(None).dt.strftime("%Y-%m-%d %H:%M"),
                "会话": view["session_slot"].map(sname),
                "发送者": view["sender_slot"].map(ndname),
                "类型": view["type_name"],
                "内容": view["content_text"],
            })
            show = show.sort_values("时间", ascending=False).head(limit)
            st.dataframe(show, use_container_width=True, hide_index=True)

    # ---- Tab6 导出 ----
    with tabs[5]:
        st.subheader("导出数据表为 CSV")
        table_map = {
            "sessions 会话": "sessions",
            "contacts 联系人": "contacts",
            "senders 发送者": "senders",
            "messages 消息明细": "messages",
        }
        choice = st.selectbox("选择数据表", list(table_map.keys()))
        out_df = dfs_f.get(table_map[choice], pd.DataFrame())
        if out_df.empty:
            st.info("该表为空")
        else:
            fname = f"{table_map[choice]}.csv"
            buf = io.StringIO()
            out_df.to_csv(buf, index=False, encoding="utf-8-sig")
            st.download_button("下载 CSV", data=buf.getvalue(), file_name=fname, mime="text/csv")

    # 底部说明 -------------------------------------------------
    st.divider()
    st.caption(
        "**数据说明**：本仓库不含任何聊天数据。正在展示的 `analysis.db` 位于仓库之外，"
        "由你本机解密库经 `scripts/build_analysis.py` 构建，含真实昵称与正文，仅存本机、请勿对外分发。"
    )


if __name__ == "__main__":
    main()
