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

import analysis.distill as distill
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


def _year_options(msgs: pd.DataFrame) -> list[int]:
    """从 create_time（Unix 秒）取可用年份列表（倒序）。"""
    ct = pd.to_numeric(msgs.get("create_time", pd.Series(dtype=float)), errors="coerce").dropna()
    if ct.empty:
        return []
    return sorted(
        {int(t.year) for t in pd.to_datetime(ct, unit="s", utc=True).dt.tz_convert(None)},
        reverse=True,
    )


@st.cache_data(show_spinner="正在分词统计词频…")
def _freq_cache(texts: tuple, top: int) -> pd.DataFrame:
    """jieba 分词结果与词频缓存（入参为待分词文本元组，命中即复用）。"""
    if not texts:
        return pd.DataFrame(columns=["word", "count"])
    df = pd.DataFrame({
        "content_text": list(texts),
        "local_type": [1] * len(texts),
        "sender_slot": [None] * len(texts),
        "sender_username": [None] * len(texts),
    })
    return stats.word_frequency({"messages": df}, top=top, max_msgs=len(texts))


def _wordcloud_bytes(wf: pd.DataFrame, max_words: int = 160) -> bytes:
    """用 wordcloud 渲染词云，返回 PNG 字节（探测 macOS 中文字体）。"""
    if wf.empty or wf.shape[1] < 2:
        return b""
    try:
        from wordcloud import WordCloud
    except Exception:
        return b""
    font = distill.find_cn_font()
    wc = WordCloud(
        font_path=font,
        width=1000,
        height=520,
        background_color="white",
        max_words=max_words,
        colormap="plasma",
        random_state=7,
    )
    wc.generate_from_frequencies(dict(zip(wf.iloc[:, 0], wf.iloc[:, 1].astype(int))))
    buf = io.BytesIO()
    wc.to_image().save(buf, format="PNG")
    return buf.getvalue()


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

    tabs = st.tabs([
        "总览", "会话分析", "行为洞察", "时间趋势", "发送者", "明细浏览", "导出",
        "年度报告", "词云", "日历热力图", "情感分析", "个性关键词", "双人关系", "人设蒸馏",
    ])

    # ---- Tab1 总览 ----
    with tabs[0]:
        sr = stats.sent_received(dfs_f)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("总消息数", f"{metrics['总消息数']:,}")
        k2.metric("会话数", metrics["会话数"])
        k3.metric("发送者数", metrics["发送者数"])
        k4.metric("时间跨度(天)", metrics["时间跨度(天)"])

        col_a, col_b = st.columns([3, 2])
        with col_a:
            st.subheader("活跃强度（逐日发送量 + 日均基线 + 7日均线）")
            act = stats.activity_series(dfs_f)
            if not act.empty and "sent" in act.columns:
                act_df = act.reset_index()
                act_df.columns = ["day", "sent", "base", "ma7", "is_peak"]
                fig = px.line(act_df, x="day", y=["sent", "ma7"], labels={"value": "条数", "variable": "序列"})
                fig.add_hline(y=act_df["base"].iloc[0], line_dash="dot", line_color="orange",
                              annotation_text=f"日均基线 {act_df['base'].iloc[0]:.1f}")
                peaks = act_df[act_df["is_peak"]]
                if not peaks.empty:
                    mx = float(peaks["sent"].max())
                    fig.add_scatter(x=peaks["day"], y=peaks["sent"], mode="markers",
                                    marker=dict(color="red", size=10, symbol="triangle-up"),
                                    name=f"峰值 {int(mx)} 条")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无按天数据")
        with col_b:
            st.subheader("收发占比")
            donut = pd.DataFrame({
                "类别": ["自己发送", "接收"],
                "数量": [sr["sent"], sr["received"]],
            })
            fig = px.pie(donut, names="类别", values="数量", hole=0.5)
            fig.update_traces(textinfo="percent+label")
            st.plotly_chart(fig, use_container_width=True)
            st.subheader("消息类型分布")
            td = stats.type_distribution(dfs_f)
            if not td.empty:
                fig = px.pie(td, names="type_name", values="count")
                fig.update_traces(textposition="inside", textinfo="percent")
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

    # ---- Tab4 时间趋势 ----
    with tabs[3]:
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

    # ---- Tab5 发送者 ----
    with tabs[4]:
        st_cat = st.radio("会话类型", ["全部", "仅私聊", "仅群聊"], horizontal=True, key="snd_chattype")
        chattype = {"全部": "all", "仅私聊": "dm", "仅群聊": "group"}[st_cat]
        c_a, c_b = st.columns([2, 3])
        with c_a:
            st.subheader(f"Top 发送者（按消息量 · {st_cat}）")
            tsd = stats.top_senders(dfs_f, n=10, chattype=chattype)
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

    # ---- Tab6 明细浏览 ----
    with tabs[5]:
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

    # ---- Tab3 行为洞察 ----
    with tabs[2]:
        st.subheader("行为习惯画像")
        b1, b2, b3, b4 = st.columns(4)
        streak = stats.longest_streak(dfs_f)
        b1.metric("最长连续聊天(天)", f"{streak['days']} 天", help=f"{streak['start']} ~ {streak['end']}")
        lns = stats.late_night_stats(dfs_f)
        b2.metric("深夜消息(0-6点)", f"{lns['count']:,}", help=f"占比 {lns['share'] * 100:.1f}%")
        ini = stats.conversation_initiative(dfs_f)
        b3.metric("对话主动性", f"{ini['ratio'] * 100:.1f}%", help=f"发起对话 {ini['initiated']}/{ini['total']}")
        rsp = stats.response_speed(dfs_f)
        b4.metric("平均回应速度", f"{rsp['avg_minutes']:.0f} 分钟" if rsp["count"] else "-",
                  help=f"样本 {rsp['count']} 次，中位 {rsp['median_seconds']:.0f} 秒")

        c_left, c_right = st.columns([3, 2])
        with c_left:
            st.subheader("24×7 活跃热力")
            hm = stats.heatmap_7x24(dfs_f)
            if not hm.empty:
                fig = px.imshow(hm.values, x=[str(h) for h in hm.columns], y=WEEKDAY_CN,
                                color_continuous_scale="YlOrRd", aspect="auto",
                                labels=dict(x="小时", y="星期", color="消息数"))
                fig.update_layout(height=360, coloraxis_colorbar=dict(thickness=12))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无热力数据")
        with c_right:
            st.subheader("日活跃强度")
            act = stats.daily_activity(dfs_f)
            if not act.empty:
                act2 = act.rename_axis("day").reset_index()
                fig = px.bar(act2.melt(id_vars="day", var_name="type", value_name="count"),
                             x="day", y="count", color="type", barmode="group")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无日活跃数据")
            st.subheader("我的高频短句")
            fp = stats.frequent_short_phrases(dfs_f, top=15)
            if not fp.empty:
                st.dataframe(fp, use_container_width=True, hide_index=True)
            else:
                st.info("暂无高频短句")

        with st.expander("深夜活跃 Top 发送者"):
            if lns.get("top"):
                st.dataframe(pd.DataFrame(list(lns["top"].items()), columns=["发送者", "深夜消息数"]),
                             use_container_width=True, hide_index=True)
            else:
                st.info("无深夜消息")

    # ---- Tab7 导出 ----
    with tabs[6]:
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

    # ---- Tab8 年度报告 ----
    with tabs[7]:
        st.subheader("年度报告总览")
        years = _year_options(dfs_f.get("messages", pd.DataFrame()))
        if not years:
            st.info("暂无消息时间数据")
        else:
            year = st.selectbox("选择年份", years, index=0, key="ar_year")
            ar = stats.annual_report_data(dfs_f, year)
            if ar["total_msgs"] == 0:
                st.info(f"{year} 年暂无消息")
            else:
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("总消息数", f"{ar['total_msgs']:,}")
                m2.metric("总字符数", f"{ar['total_chars']:,}")
                m3.metric("活跃天数", ar["active_days"])
                m4.metric("最长连续聊天", f"{ar['longest_streak_days']} 天")
                m5, m6 = st.columns(2)
                bf = ar["best_friend"]
                m5.metric("年度最佳好友", bf["name"] if bf else "-", help=f"消息 {bf['count']} 条" if bf else None)
                no = ar["night_owl"]
                m6.metric("年度夜猫子", no["name"] if no else "-", help=f"深夜消息 {no['count']} 条" if no else None)
                m7, m8 = st.columns(2)
                m7.metric("峰值日", f"{ar['peak_day']}（{ar['peak_count']} 条）")
                m8.metric("自己发送占比", f"{ar['sent_ratio'] * 100:.1f}%")
                c1, c2 = st.columns(2)
                with c1:
                    st.subheader("月度消息轨迹")
                    ml = ar["monthly"].reset_index().rename(columns={"month": "月份", "count": "条数"})
                    if not ml.empty:
                        fig = px.bar(ml, x="月份", y="条数")
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("暂无月度数据")
                with c2:
                    st.subheader("年度高频词")
                    tw = ar["top_words"]
                    if not tw.empty:
                        fig = px.bar(tw.head(20), x="word", y="count")
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("暂无分词数据")

    # ---- Tab9 词云 ----
    with tabs[8]:
        st.subheader("聊天词云")
        scope = st.radio("词云范围", ["全站", "会话", "发送者"], horizontal=True, key="wc_scope")
        if scope == "会话":
            chattype = "all"
            st.caption("会话范围按所选会话自身类型计算（群聊/私聊天然隔离）")
        else:
            st_cat = st.radio("会话类型", ["全部", "仅私聊", "仅群聊"], horizontal=True, key="wc_chattype")
            chattype = {"全部": "all", "仅私聊": "dm", "仅群聊": "group"}[st_cat]
        txt_all = stats.text_messages(dfs_f, chattype=chattype)
        if txt_all.empty or not stats._HAS_JIEBA:
            st.warning("缺少 jieba 或该范围内没有文本，无法生成词云")
        else:
            session = None
            sender = None
            if scope == "会话":
                opts = sorted(txt_all["session_slot"].dropna().unique())
                if opts:
                    session = st.selectbox("选择会话", opts, format_func=sname, key="wc_session")
            elif scope == "发送者":
                opts = sorted(txt_all["sender_slot"].dropna().unique())
                if opts:
                    sender = st.selectbox("选择发送者", opts, format_func=ndname, key="wc_sender")
            if session is not None:
                sub_txt = txt_all[txt_all["session_slot"] == session]
            elif sender is not None:
                sub_txt = txt_all[txt_all["sender_slot"] == sender]
            else:
                sub_txt = txt_all
            if sub_txt.empty:
                st.info("该范围没有可分词文本")
            else:
                sample = sub_txt["content_text"].fillna("").astype(str)
                if len(sample) > 30000:
                    sample = sample.sample(30000, random_state=7)
                wf = _freq_cache(tuple(sample), 200)
                if wf.empty:
                    st.info("分词结果为空")
                else:
                    imgb = _wordcloud_bytes(wf, 160)
                    if imgb:
                        st.image(imgb, caption="词云（已过滤停用词/单字）", use_container_width=True)
                    st.markdown("**词频 Top 50**")
                    t2 = wf.head(50).copy()
                    t2["词"] = t2["word"]
                    t2["频次"] = t2["count"]
                    st.dataframe(t2[["词", "频次"]], use_container_width=True, hide_index=True)

    # ---- Tab10 日历热力图 ----
    with tabs[9]:
        st.subheader("GitHub 风格日历热力图")
        years = _year_options(dfs_f.get("messages", pd.DataFrame()))
        if not years:
            st.info("暂无消息时间数据")
        else:
            year = st.selectbox("选择年份", years, index=0, key="hm_year")
            ch = stats.calendar_heatmap(dfs_f, year)
            if ch is None or ch.empty:
                st.info(f"{year} 年无消息")
            else:
                fig = px.imshow(ch.values, x=[f"W{c}" for c in ch.columns], y=WEEKDAY_CN,
                                color_continuous_scale="greens", aspect="auto",
                                labels=dict(x="ISO 周", y="星期", color="消息数"))
                fig.update_layout(height=320, coloraxis_colorbar=dict(thickness=12))
                st.plotly_chart(fig, use_container_width=True)
                st.caption(f"{year} 年共 {int(ch.values.sum())} 条消息")

    # ---- Tab11 情感分析 ----
    with tabs[10]:
        st.subheader("文本情感趋势（SnowNLP）")
        st_cat = st.radio("会话类型", ["全部", "仅私聊", "仅群聊"], horizontal=True, key="sent_cat")
        chattype = {"全部": "all", "仅私聊": "dm", "仅群聊": "group"}[st_cat]
        tm = stats.text_messages(dfs_f, chattype=chattype)
        if tm.empty or not stats._HAS_SNOWNLP:
            st.warning("缺少 SnowNLP 或文本数据，无法进行情感分析")
        else:
            freq = st.radio("聚合粒度", ["月度", "周度"], horizontal=True, key="sent_freq")
            fr = "ME" if freq == "月度" else "W"
            texts = tm["content_text"].fillna("").astype(str)
            pick = texts.sample(min(3000, len(texts)), random_state=7)
            sdfs = pd.DataFrame({
                "content_text": pick.to_numpy(),
                "local_type": [1] * len(pick),
                "create_time": tm.loc[pick.index, "create_time"].to_numpy(),
            })
            sen = stats.sentiment_by_period({"messages": sdfs}, freq=fr, max_total=3000, seed=7, chattype="all")
            if sen.empty:
                st.info("情感分析结果为空")
            else:
                fig = px.line(sen, x="period", y="avg_sentiment", markers=True,
                              labels={"period": "时间", "avg_sentiment": "平均情感分(0~1)"})
                fig.add_hline(y=0.5, line_dash="dot", line_color="gray")
                st.plotly_chart(fig, use_container_width=True)
                c1, c2 = st.columns(2)
                with c1:
                    st.subheader("正向/负向消息数")
                    fig2 = px.bar(sen, x="period", y=["pos", "neg"], barmode="group",
                                  labels={"value": "消息数", "variable": "方向"})
                    st.plotly_chart(fig2, use_container_width=True)
                with c2:
                    st.subheader("逐期详情")
                    st.dataframe(sen[["period", "total", "avg_sentiment", "pos", "neg"]],
                                 use_container_width=True, hide_index=True)

    # ---- Tab12 个性关键词 ----
    with tabs[11]:
        st.subheader("个性关键词（手写 TF-IDF）")
        if not stats._HAS_JIEBA:
            st.warning("缺少 jieba")
        else:
            st_cat = st.radio("会话类型", ["全部", "仅私聊", "仅群聊"], horizontal=True, key="kw_cat")
            chattype = {"全部": "all", "仅私聊": "dm", "仅群聊": "group"}[st_cat]
            tm_s = stats.text_messages(dfs_f, chattype=chattype)
            opts = sorted(tm_s["sender_slot"].dropna().unique()) if not tm_s.empty else []
            if not opts:
                st.info("该会话类型下暂无可分析发送者")
            else:
                sender = st.selectbox("选择发送者（对比其与全库他人的用词差异）", opts, format_func=ndname, key="kw_sender")
                if st.button("计算个性关键词", type="primary"):
                    tfr = stats.tfidf_top_words(dfs_f, sender, top=20, chattype=chattype)
                    if tfr.empty:
                        st.info("该发送者无有效分词结果")
                    else:
                        st.markdown(f"**{ndname(sender)} 的区分度用词**（高 TF-IDF = 更专属）")
                        c1, c2 = st.columns([3, 2])
                        with c1:
                            fig = px.bar(tfr.iloc[::-1], x="tfidf", y="word", orientation="h")
                            st.plotly_chart(fig, use_container_width=True)
                        with c2:
                            st.dataframe(
                                tfr[["word", "tfidf", "freq"]].rename(
                                    columns={"word": "词", "tfidf": "TF-IDF", "freq": "出现消息数"}),
                                use_container_width=True, hide_index=True,
                            )

    # ---- Tab13 双人关系 ----
    with tabs[12]:
        st.subheader("双人关系统计：对话者 vs 我")
        st.caption("仅统计私聊会话中该对话者与您本人的往来关系（含双方发言量、相互应答、最大连击、"
                   "共同高频短语）；群聊中成员发言与私聊严格隔离，不参与本统计。")
        msgs_p = dfs_f.get("messages", pd.DataFrame())
        opts = []
        if not msgs_p.empty and "sender_slot" in msgs_p.columns and "session_slot" in msgs_p.columns:
            dm_mask = stats._chattype_mask(dfs_f, msgs_p, "dm")
            opts = sorted(msgs_p.loc[dm_mask & msgs_p["sender_slot"].notna(), "sender_slot"].unique())
        if not opts:
            st.info("无私聊发送者数据，无法做双人关系分析")
        else:
            other = st.selectbox("选择对话者", opts, format_func=ndname, index=0, key="pair_other")
            ps = stats.pair_stats(dfs_f, other)
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("对方消息数", f"{ps['counts']['other']:,}")
            k2.metric("我方消息数", f"{ps['counts']['self']:,}")
            k3.metric("应答配对", ps["reply"]["pairs"])
            k4.metric("最大连击", ps["longest_streak"])
            if ps["reply"]["pairs"]:
                st.caption(f"应答中位耗时 {ps['reply']['median_seconds']:,.0f}s / 平均 {ps['reply']['avg_seconds']:,.0f}s")
            ml = ps["monthly"]
            if not ml.empty:
                ml2 = ml.copy()
                ml2["对方"] = ml2["other"]
                ml2["我"] = ml2["self"]
                fig = px.line(ml2, x="month", y=["对方", "我"], markers=True,
                              labels={"month": "月份", "value": "消息数"})
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无月度轨迹")
            cp = ps["common_phrases"]
            if not cp.empty:
                st.subheader("双方共同高频短语（私聊内）")
                st.dataframe(
                    cp.rename(columns={"phrase": "短语", "count_other": "对方次数", "count_self": "我方次数"}).head(20),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.info("未发现共同高频短语")

    # ---- Tab14 人设蒸馏 ----
    with tabs[13]:
        st.subheader("人设蒸馏 → 生成 SKILL")
        st.caption("选择会话 / 发送者 / 自己，纯本地规则提取语言风格，生成可植入 agent 平台的 "
                   "SKILL.md 技能目录（含 assets 资产）。默认输出到仓库外 ~/WeChatData/skills/<标题>/。")
        if dfs_f.get("messages", pd.DataFrame()).empty:
            st.info("暂无数据")
        else:
            obj_type = st.radio("蒸馏对象类型", ["会话", "发送者", "自己"], horizontal=True, key="dist_type")
            session = None
            sender = None
            if obj_type == "会话":
                chattype = "all"
                st.caption("会话蒸馏按所选会话自身类型计算（群聊/私聊天然隔离）")
                opts = sorted(dfs_f["messages"]["session_slot"].dropna().unique())
                if opts:
                    session = st.selectbox("选择会话", opts, format_func=sname, key="dist_session")
            else:
                st_cat = st.radio("会话类型", ["全部", "仅私聊", "仅群聊"], horizontal=True, key="dist_cat")
                chattype = {"全部": "all", "仅私聊": "dm", "仅群聊": "group"}[st_cat]
                if obj_type == "发送者":
                    opts = sorted(dfs_f["messages"]["sender_slot"].dropna().unique())
                    if opts:
                        sender = st.selectbox("选择发送者", opts, format_func=ndname, key="dist_sender")
                else:
                    sender = "self"
            title = st.text_input("SKILL 标题（留空自动生成）", value="", key="dist_title")
            out = st.text_input("输出目录（留空用默认 ~/WeChatData/skills/<标题>）", value="", key="dist_out")
            if st.button("开始蒸馏", type="primary"):
                res = distill.build_skill(
                    dfs_f,
                    out_dir=out or None,
                    title=title or None,
                    session=session if obj_type == "会话" else None,
                    sender=None if obj_type == "会话" else sender,
                    chattype=chattype,
                )
                if not res["ok"]:
                    st.error(f"蒸馏失败：{res['reason']}")
                else:
                    st.success(f"蒸馏完成，共 {len(res['files'])} 个文件")
                    st.markdown(f"**输出目录**：`{res['skill_dir']}`")
                    for fp in res["files"]:
                        st.markdown(f"- `{fp}`")
                    st.subheader("风格摘要")
                    sm = res["summary"]
                    st.markdown(
                        f"- 样本：{sm['text_messages']} 条文本 / {sm['total_chars']} 字\n"
                        f"- Top 用词：{' '.join(sm['top_words'])}"
                    )

    # 底部说明 -------------------------------------------------
    st.divider()
    st.caption(
        "**数据说明**：本仓库不含任何聊天数据。正在展示的 `analysis.db` 位于仓库之外，"
        "由你本机解密库经 `scripts/build_analysis.py` 构建，含真实昵称与正文，仅存本机、请勿对外分发。"
    )


if __name__ == "__main__":
    main()
