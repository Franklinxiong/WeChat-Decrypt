# -*- coding: utf-8 -*-
"""微信聊天记录分析 UI（Streamlit）。

仓库不携带任何数据。分析库由用户在本机解密后构建（见 scripts/build_analysis.py），
默认路径 ~/WeChatData/analysis.db，可在 config.json 修改，或用环境变量 WE_CHAT_DB 覆盖。
"""
from __future__ import annotations

import html
import io
import json
import os
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

import analysis.distill as distill
import analysis.export as export
import analysis.loader as loader
import analysis.stats as stats
import analysis.voice_transcriber as voice_tr

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


# =========================================================================
# 语音转写页签（Tab15，最小侵入新增）
# 仅读取/写入 analysis.db 的独立表 voice_transcripts，不触碰任何现有表。
# 转写走 analysis.voice_transcriber（SILK 解码 + SenseVoice 离线转写）。
# =========================================================================
VOICE_TYPES = (6, 34)
_VOICE_KEY_COLS = ["session_username", "local_id", "create_time"]
_VOICE_DEFAULT_DECRYPTED = os.path.join(PROJECT_ROOT, "decrypted")


def _voice_model_status(model_dir: str) -> tuple[bool, str]:
    """检测 SenseVoice 模型文件是否齐全，仅检测不下载。"""
    missing = [f for f in voice_tr.MODEL_FILES
               if not os.path.isfile(os.path.join(model_dir, f))]
    if not missing:
        return True, f"模型已就绪：`{model_dir}`（model.int8.onnx + tokens.txt）"
    return False, (
        f"模型不完整，缺少 {len(missing)} 个文件：{', '.join(missing)}\n\n"
        f"目录：`{model_dir}`\n\n首次使用需联网下载（约 245MB，可断点续传），下载后自动就绪。"
    )


def _voice_transcripts_df(db_path: str) -> pd.DataFrame:
    """读取独立表 voice_transcripts（表不存在时返回空表，绝不建表/写入）。"""
    cols = ["session", "local_id", "create_time", "voice_text"]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            df = pd.read_sql_query(
                "SELECT session, local_id, create_time, voice_text "
                "FROM voice_transcripts",
                conn,
            )
        finally:
            conn.close()
        return df
    except Exception:
        return pd.DataFrame(columns=cols)


def _voice_upsert(db_path: str, session, lid, ct, text) -> None:
    """幂等写入 voice_transcripts（与 scripts/transcribe_voice.py 同一 schema）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO voice_transcripts"
            "(session, local_id, create_time, voice_text) VALUES (?,?,?,?)",
            (session, lid, ct, text),
        )
        conn.commit()
    finally:
        conn.close()


def _voice_view(msgs: pd.DataFrame, trans: pd.DataFrame):
    """把消息表语音行与转写表按 (session_username, local_id, create_time) 关联。"""
    if msgs.empty or "local_type" not in msgs.columns:
        empty = pd.DataFrame(columns=_VOICE_KEY_COLS + ["session_slot", "sender_slot"])
        return empty, {"total": 0, "done": 0, "todo": 0}
    vm = msgs[msgs["local_type"].isin(VOICE_TYPES)].copy()
    if vm.empty:
        return vm, {"total": 0, "done": 0, "todo": 0}
    vm["_key"] = vm[_VOICE_KEY_COLS].astype(str).agg("|".join, axis=1)
    if trans.empty:
        vm["voice_text"] = None
    else:
        ts = trans.copy()
        ts["_key"] = ts[["session", "local_id", "create_time"]].astype(str).agg("|".join, axis=1)
        vm = vm.merge(ts[["_key", "voice_text"]], on="_key", how="left")
    vm["status"] = vm["voice_text"].notna().map({True: "已转写", False: "未转写"})
    stat = {
        "total": int(len(vm)),
        "done": int(vm["status"].eq("已转写").sum()),
        "todo": int(vm["status"].eq("未转写").sum()),
    }
    return vm, stat


def _voice_run_pending(db_path, pending, idx, transcriber, bar, status_el):
    """在会话内逐条增量转写未转写语音，实时更新进度条与状态文案。"""
    n = len(pending)
    ok = fail = skip = 0
    for i, row in enumerate(pending, 1):
        session, lid, ct = row
        try:
            data = idx.get(session, lid, ct)
            if not data:
                skip += 1
            else:
                pcm, rate = voice_tr.decode_silk_to_pcm(data)
                text = voice_tr.clean_sensevoice_text(
                    transcriber.transcribe_pcm(pcm, rate))
                _voice_upsert(db_path, session, lid, ct, text)
                ok += 1
                status_el.markdown(
                    f"**正在转写 {i}/{n}** · 成功 {ok} / 失败 {fail} / 跳过 {skip}　"
                    f"`{session}` #{lid} → {text[:40] or '（空识别）'}")
        except Exception as exc:
            fail += 1
            status_el.markdown(
                f"**正在转写 {i}/{n}** · 成功 {ok} / 失败 {fail} / 跳过 {skip}　"
                f"`{session}` #{lid} 失败：{exc}")
        bar.progress(min(i / n, 1.0))
    return ok, fail, skip


def _voice_tab(db_path: str, msgs: pd.DataFrame, sname, ndname,
               decrypted: str, model_dir: str) -> None:
    st.subheader("语音转写（微信语音 → 文字）")
    st.caption("对解密库中的语音消息（SILK）做离线转写，结果写入 `analysis.db` 的独立表 "
               "`voice_transcripts`，不触碰任何现有表。语音统计独立于侧边栏时间过滤。")
    if "_voice_flash" in st.session_state:
        st.success(st.session_state.pop("_voice_flash"))

    # ---- 状态总览 ----
    trans = _voice_transcripts_df(db_path)
    vm, stat = _voice_view(msgs, trans)
    c1, c2, c3 = st.columns(3)
    c1.metric("语音消息总数", f"{stat['total']:,}")
    c2.metric("已转写", f"{stat['done']:,}")
    c3.metric("未转写", f"{stat['todo']:,}")

    # ---- 1) 模型管理 ----
    st.divider()
    st.markdown("**1. 模型管理**（SenseVoice 离线模型）")
    m_ready, m_msg = _voice_model_status(model_dir)
    if m_ready:
        st.success(m_msg)
    else:
        st.warning(m_msg)
        if st.button("下载模型（约 245MB，首次需联网）", type="primary"):
            with st.spinner("正在下载模型（可断点续传，请耐心等待）..."):
                try:
                    voice_tr.ensure_model(model_dir)
                except Exception as exc:
                    st.error(f"下载失败：{exc}")
                else:
                    st.success("模型下载完成")
            st.rerun()

    # ---- 2) 转写操作区 ----
    st.divider()
    st.markdown("**2. 转写操作**")
    if stat["todo"] == 0:
        st.info("没有未转写的语音消息。")
    else:
        st.caption(f"当前有 **{stat['todo']:,}** 条未转写语音；全量转写耗时较长，"
                   f"可先用「限量快速测试（10 条）」验证效果。")
        col_a, col_b, _ = st.columns([1, 1, 3])
        do_all = col_a.button(
            f"开始转写（全部未转写，共 {stat['todo']:,} 条）", type="primary")
        do_test = col_b.button("限量快速测试（10 条）")
        if (do_all or do_test) and not m_ready:
            st.error("模型未就绪，请先下载模型。")
        elif do_all or do_test:
            limit = 10 if do_test else None
            pend = vm[vm["status"].eq("未转写")][_VOICE_KEY_COLS]
            pend = pend.head(limit) if limit else pend
            pending = [tuple(r) for r in pend.itertuples(index=False)]
            if not pending:
                st.info("没有可转写的语音消息。")
            else:
                if not os.path.isdir(decrypted):
                    st.error(f"解密库目录不存在：`{decrypted}`")
                else:
                    with st.spinner("构建语音索引并加载模型（首次约数秒）..."):
                        idx = voice_tr.VoiceIndex(os.path.join(decrypted, "message"))
                        transcriber = voice_tr.SenseVoiceTranscriber(model_dir)
                    bar = st.progress(0.0)
                    status_el = st.empty()
                    ok, fail, skip = _voice_run_pending(
                        db_path, pending, idx, transcriber, bar, status_el)
                    bar.progress(1.0)
                    status_el.empty()
                    st.session_state["_voice_flash"] = (
                        f"转写完成：成功 {ok} / 失败 {fail} / 跳过 {skip}"
                        f"（累计已转写 {stat['done'] + ok:,} 条）")
                    st.rerun()

    # ---- 3) 结果展示 ----
    st.divider()
    st.markdown("**3. 结果查看与导出**")
    if vm.empty:
        st.info("解码库中暂未发现语音消息（local_type 6 / 34）。")
        return

    scope = st.radio("筛选范围", ["全部语音", "未转写", "已转写"],
                     horizontal=True, index=0, key="vt_scope")
    sess_opts = sorted(vm["session_slot"].dropna().unique().tolist())
    send_opts = sorted(vm["sender_slot"].dropna().unique().tolist())
    c_s, c_e = st.columns(2)
    sel_sess = c_s.selectbox(
        "会话", ["全部"] + sess_opts, key="vt_sess",
        format_func=lambda x: "全部" if x == "全部" else sname(x))
    sel_send = c_e.selectbox(
        "发送者", ["全部"] + send_opts, key="vt_send",
        format_func=lambda x: "全部" if x == "全部" else ndname(x))
    kw = st.text_input("搜索关键词（匹配转写文字 / 会话 / 发送者）",
                       value="", key="vt_kw")

    disp = vm.copy()
    if scope == "未转写":
        disp = disp[disp["status"].eq("未转写")]
    elif scope == "已转写":
        disp = disp[disp["status"].eq("已转写")]
    if sel_sess != "全部":
        disp = disp[disp["session_slot"].eq(sel_sess)]
    if sel_send != "全部":
        disp = disp[disp["sender_slot"].eq(sel_send)]
    if kw.strip():
        k = kw.strip()
        sess_str = disp["session_slot"].map(lambda v: sname(v) if pd.notna(v) else "").astype(str)
        send_str = disp["sender_slot"].map(lambda v: ndname(v) if pd.notna(v) else "").astype(str)
        disp = disp[
            disp["voice_text"].fillna("").str.contains(k, case=False, na=False)
            | sess_str.str.contains(k, case=False)
            | send_str.str.contains(k, case=False)
        ]

    if disp.empty:
        st.info("没有符合筛选条件的语音消息。")
    else:
        disp = disp.sort_values("create_time", ascending=False)
        show = pd.DataFrame({
            "时间": pd.to_datetime(disp["create_time"], unit="s", errors="coerce"),
            "会话": disp["session_slot"].map(lambda v: sname(v) if pd.notna(v) else "（无）"),
            "发送者": disp["sender_slot"].map(lambda v: ndname(v) if pd.notna(v) else "（无）"),
            "转写文字": disp["voice_text"].fillna("[语音]"),
            "状态": disp["status"],
        })
        st.caption(f"当前筛选结果 {len(show):,} 条（最多展示前 2000 条）")
        st.dataframe(show.head(2000), use_container_width=True, height=360)
        csv_bytes = show.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "导出当前结果为 CSV（UTF-8-BOM）", data=csv_bytes,
            file_name="voice_transcripts_filtered.csv", mime="text/csv")

    # ---- 使用说明 ----
    st.divider()
    st.markdown("**4. 使用说明**")
    st.markdown(
        "1. **模型**：首次使用点击「下载模型」自动获取 SenseVoice 离线模型"
        "（约 245MB，仅需联网一次）。\n"
        "2. **快速测试**：建议先用「限量快速测试（10 条）」验证转写效果。\n"
        "3. **全量转写**：「开始转写（全部未转写）」会逐条离线转写并实时显示进度，"
        "耗时取决于语音数量，页面进度条会持续更新，请勿关闭页面。\n"
        "4. **结果查看**：可按会话 / 发送者 / 关键词筛选，未转写消息显示 `[语音]`，"
        "已转写显示真实文字；「导出 CSV」下载当前筛选结果。\n"
        "5. 转写结果写入 `voice_transcripts` 独立表，本页签不修改也不覆盖任何现有数据表。"
    )


# =========================================================================
# 导出页签（Tab7「导出」改造）：按联系人/会话导出，多格式 HTML/MD/TXT/CSV
# 严格最小侵入：仅新增以下辅助函数并在 with tabs[6] 调用；
# 原有「整表导出 CSV」能力保留在 expander 折叠区内。
# =========================================================================
_VOICE_TYPES_EXPORT = (6, 34)


def _now_str() -> str:
    import datetime
    dt = datetime.datetime.now()
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


def _fmt_ts(ts) -> str:
    """Unix 秒 -> 本地时间字符串（容忍非法值）。"""
    try:
        dt = pd.to_datetime(int(ts), unit="s", errors="coerce")
        if pd.isna(dt):
            return "-"
        return (f"{dt.year}-{dt.month:02d}-{dt.day:02d} "
                f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
    except Exception:
        return "-"


def _dialogue_frame(msgs: pd.DataFrame, trans: pd.DataFrame, session=None,
                    sender=None, lo=None, hi=None,
                    voice_mode: str = "含语音消息") -> pd.DataFrame:
    """按条件过滤消息并合并语音转写，得到导出用 frame（新增 _content 列，向量化）。

    - 文本消息：content_text 原样
    - 语音消息(local_type in 6/34)：优先 voice_transcripts.voice_text，
      未转写显示 "[语音未转写]"
    """
    m = msgs.copy()
    if m.empty or "local_type" not in m.columns:
        m["_content"] = []
        return m
    if voice_mode == "仅文本消息":
        m = m[~m["local_type"].isin(_VOICE_TYPES_EXPORT)]
    if session is not None:
        m = m[m["session_slot"] == session]
    if sender is not None:
        m = m[m["sender_slot"] == sender]
    if not m.empty and "create_time" in m.columns:
        t = pd.to_numeric(m["create_time"], errors="coerce")
        keep = pd.Series(True, index=m.index)
        if lo is not None:
            keep &= (t >= lo)
        if hi is not None:
            keep &= (t <= hi)
        m = m[keep]
    # 合并语音转写
    if trans.empty:
        m["voice_text"] = None
    else:
        ts = trans.copy()
        m["_k"] = (m["session_username"].astype(str) + "|"
                   + m["local_id"].astype(str) + "|"
                   + m["create_time"].astype(str))
        ts["_k"] = (ts["session"].astype(str) + "|"
                    + ts["local_id"].astype(str) + "|"
                    + ts["create_time"].astype(str))
        m = m.merge(ts[["_k", "voice_text"]], on="_k", how="left")
        m = m.drop(columns=["_k"])
    # 向量化生成导出内容
    voiced = m["local_type"].isin(_VOICE_TYPES_EXPORT) \
        if "local_type" in m.columns else pd.Series(False, index=m.index)
    txt = m["content_text"].fillna("").astype(str).str.strip()
    vt = m["voice_text"].fillna("").astype(str).str.strip()
    full = vt.where(vt != "", "[语音未转写]")
    m["_content"] = txt.where(~voiced, full)
    return m


def _dialogue_html(frame, sname, ndname, session_label, title) -> str:
    rows = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>",
        "<style>"
        "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
        "max-width:860px;margin:24px auto;padding:0 16px;color:#222;background:#fff;}"
        "h1{font-size:22px;} .meta{color:#888;font-size:13px;}"
        ".msg{margin:10px 0;padding:8px 12px;border-radius:8px;background:#f6f7f9;}"
        ".msg.self{background:#e6f4ff;}"
        ".msg .time{color:#999;font-size:12px;margin-right:8px;}"
        ".msg .sender{font-weight:600;margin-right:6px;}"
        ".msg.voice{border-left:3px solid #faad14;}"
        ".msg.voice .content{color:#7a5c00;background:#fffbe6;padding:2px 6px;"
        "border-radius:4px;}"
        "</style></head><body>",
    ]
    rows.append(f"<h1>{html.escape(title)}</h1>")
    rows.append(f"<p class='meta'>会话：{html.escape(str(session_label))} · "
                f"共 {len(frame)} 条 · 导出时间：{_now_str()}</p>")
    for _, row in frame.iterrows():
        lt = row.get("local_type")
        is_voice = lt in _VOICE_TYPES_EXPORT
        is_self = bool(row.get("is_send"))
        snd = row.get("sender_slot")
        snd_name = html.escape(ndname(snd) if pd.notna(snd) else "（系统）")
        cls = "msg" + (" voice" if is_voice else "") + (" self" if is_self else "")
        t_s = _fmt_ts(row.get("create_time"))
        content = html.escape(str(row.get("_content") or ""))
        voicetag = '<span class="voice-tag">🎤 </span>' if is_voice else ""
        rows.append(
            f'<div class="{cls}"><span class="time">{t_s}</span>'
            f'<span class="sender">{snd_name}</span>: {voicetag}'
            f'<span class="content">{content}</span></div>'
        )
    rows.append("</body></html>")
    return "\n".join(rows)


def _dialogue_markdown(frame, sname, ndname, session_label, title) -> str:
    out = [f"# {title}", "",
           f"> 会话：{session_label} ｜ 共 {len(frame)} 条 ｜ {_now_str()}", ""]
    for _, row in frame.iterrows():
        snd = ndname(row.get("sender_slot")) \
            if pd.notna(row.get("sender_slot")) else "（系统）"
        ts = _fmt_ts(row.get("create_time"))
        content = str(row.get("_content") or "")
        tag = "🎤 " if row.get("local_type") in _VOICE_TYPES_EXPORT else ""
        out.append(f"- **{ts}**　**{snd}**：{tag}{content}")
    return "\n".join(out)


def _dialogue_txt(frame, sname, ndname, session_label, title) -> str:
    out = [f"微信聊天记录 - {title}",
           f"会话：{session_label}  共 {len(frame)} 条  {_now_str()}",
           "-" * 48]
    for _, row in frame.iterrows():
        snd = ndname(row.get("sender_slot")) \
            if pd.notna(row.get("sender_slot")) else "（系统）"
        ts = _fmt_ts(row.get("create_time"))
        content = str(row.get("_content") or "")
        prefix = "[语音] " if row.get("local_type") in _VOICE_TYPES_EXPORT else ""
        out.append(f"[{ts}] {snd}: {prefix}{content}")
    return "\n".join(out)


def _dialogue_csv_bytes(frame, sname, ndname, session_label) -> bytes:
    out = pd.DataFrame({
        "时间": frame["create_time"].map(_fmt_ts),
        "会话": frame["session_slot"].map(
            lambda v: sname(v) if pd.notna(v) else "（无）"),
        "发送者": frame["sender_slot"].map(
            lambda v: ndname(v) if pd.notna(v) else "（系统）"),
        "类型": frame["local_type"],
        "内容": frame["_content"],
    })
    return out.to_csv(index=False).encode("utf-8-sig")


def _export_tab(db_path: str, msgs: pd.DataFrame, dfs: dict, sel, sname, ndname) -> None:
    st.subheader("导出聊天记录")
    st.caption("按联系人/会话导出为 HTML / Markdown / TXT / CSV。"
               "语音消息自动合并转写文字，未转写显示「[语音未转写]」。")
    if msgs.empty or "session_slot" not in msgs.columns or "session_username" not in msgs.columns:
        st.info("消息数据不可用（缺少 session 相关列）。可先在「数据源」内生成分析库。")
        return

    # ---- 会话选择（可搜索） ----
    sess_opts = sorted(msgs["session_slot"].dropna().unique().tolist())
    sel_sess = st.selectbox(
        "选择会话（可输入搜索）", [None] + sess_opts,
        format_func=lambda x: "全部会话" if x is None else sname(x),
        key="ex_sess")

    # ---- 发送者过滤 ----
    base = msgs if sel_sess is None else msgs[msgs["session_slot"] == sel_sess]
    send_opts = sorted(base["sender_slot"].dropna().unique().tolist())
    sel_send = st.selectbox(
        "发送者过滤", [None] + send_opts,
        format_func=lambda x: "全部发送者" if x is None else ndname(x),
        key="ex_send")

    # ---- 时间范围 ----
    ts_series = pd.to_numeric(msgs["create_time"], errors="coerce").dropna()
    time_desc = "全部"
    lo = hi = None
    if not ts_series.empty:
        mn_dt = pd.to_datetime(ts_series.min(), unit="s", utc=True).tz_convert(None)
        mx_dt = pd.to_datetime(ts_series.max(), unit="s", utc=True).tz_convert(None)
        use_side = st.checkbox("使用侧边栏时间范围", value=sel is not None,
                               key="ex_uside")
        if use_side and sel is not None:
            lo = int(pd.Timestamp(sel[0]).timestamp() - 12 * 3600)
            hi = int(pd.Timestamp(sel[1] + pd.Timedelta(days=1)).timestamp())
            time_desc = f"{sel[0]} ~ {sel[1]}（跟随侧边栏）"
        else:
            d1, d2 = st.date_input(
                "自定义时间范围", value=(mn_dt.date(), mx_dt.date()),
                min_value=mn_dt.date(), max_value=mx_dt.date(), key="ex_range")
            lo = int(pd.Timestamp(d1).timestamp() - 12 * 3600)
            hi = int(pd.Timestamp(d2 + pd.Timedelta(days=1)).timestamp())
            time_desc = f"{d1} ~ {d2}"
    st.caption(f"时间范围：{time_desc}")

    # ---- 消息范围 / 格式 ----
    voice_mode = st.radio("消息范围", ["含语音消息", "仅文本消息"],
                          horizontal=True, index=0, key="ex_voice")
    fmt = st.selectbox("导出格式", ["HTML", "Markdown", "TXT", "CSV"],
                       key="ex_fmt")

    # ---- 预览 ----
    trans = _voice_transcripts_df(db_path)
    pv = _dialogue_frame(msgs, trans, session=sel_sess, sender=sel_send,
                         lo=lo, hi=hi, voice_mode=voice_mode)
    st.markdown("**预览（前 20 条）**")
    st.caption(f"当前筛选范围内共 {len(pv):,} 条消息（预览仅显示前 20 条）")
    if pv.empty:
        st.info("当前条件下没有消息。")
    else:
        pm = pv.head(20).copy()
        view = pd.DataFrame({
            "时间": pm["create_time"].map(_fmt_ts),
            "会话": pm["session_slot"].map(
                lambda v: sname(v) if pd.notna(v) else "（无）"),
            "发送者": pm["sender_slot"].map(
                lambda v: ndname(v) if pd.notna(v) else "（系统）"),
            "内容": pm["_content"],
        })
        st.dataframe(view, use_container_width=True)

    # ---- 生成与下载 ----
    if st.button("生成导出文件", type="primary", key="ex_gen"):
        if pv.empty:
            st.warning("当前条件下没有可导出的消息。")
        else:
            title = (f"微信聊天记录 - {sname(sel_sess)}"
                     if sel_sess is not None else "微信聊天记录 - 全部会话")
            session_label = sname(sel_sess) if sel_sess is not None else "全部会话"
            slug = str(sel_sess) if sel_sess is not None else "all"
            if fmt == "HTML":
                data = _dialogue_html(pv, sname, ndname, session_label, title).encode("utf-8")
                fname = f"chat_{slug}.html"
                mime = "text/html"
            elif fmt == "Markdown":
                data = _dialogue_markdown(pv, sname, ndname, session_label, title).encode("utf-8")
                fname = f"chat_{slug}.md"
                mime = "text/markdown"
            elif fmt == "TXT":
                data = _dialogue_txt(pv, sname, ndname, session_label, title).encode("utf-8")
                fname = f"chat_{slug}.txt"
                mime = "text/plain"
            else:
                data = _dialogue_csv_bytes(pv, sname, ndname, session_label)
                fname = f"chat_{slug}.csv"
                mime = "text/csv"
            st.session_state["_ex_payload"] = (data, fname, mime, len(pv))
            st.rerun()

    pl = st.session_state.pop("_ex_payload", None)
    if pl:
        data, fname, mime, n = pl
        st.success(f"已生成文件 `{fname}`（{n:,} 条）。改动筛选条件后请重新「生成导出文件」。")
        st.download_button("下载导出文件", data=data, file_name=fname,
                           mime=mime, key="ex_dl")

    # ---- 附属：保留原有整表导出（折叠） ----
    with st.expander("原有功能：整表导出 CSV"):
        table_map = {
            "sessions 会话": "sessions",
            "contacts 联系人": "contacts",
            "senders 发送者": "senders",
            "messages 消息明细": "messages",
        }
        choice = st.selectbox("选择数据表", list(table_map.keys()), key="ex_table")
        out_df = dfs.get(table_map[choice], pd.DataFrame())
        if out_df.empty:
            st.info("该表为空")
        else:
            fname = f"{table_map[choice]}.csv"
            buf = io.StringIO()
            out_df.to_csv(buf, index=False, encoding="utf-8-sig")
            st.download_button("下载 CSV", data=buf.getvalue(),
                               file_name=fname, mime="text/csv")


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
        "语音转写",
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
        _export_tab(db_path, msgs, dfs, sel, sname, ndname)

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

    # ---- Tab15 语音转写（新增页签，不影响上方 14 页签） ----
    with tabs[14]:
        _voice_tab(
            db_path,
            msgs,  # 全量消息：语音统计独立于侧边栏时间过滤
            sname,
            ndname,
            decrypted=_VOICE_DEFAULT_DECRYPTED,
            model_dir=voice_tr.DEFAULT_MODEL_DIR,
        )

    # 底部说明 -------------------------------------------------
    st.divider()
    st.caption(
        "**数据说明**：本仓库不含任何聊天数据。正在展示的 `analysis.db` 位于仓库之外，"
        "由你本机解密库经 `scripts/build_analysis.py` 构建，含真实昵称与正文，仅存本机、请勿对外分发。"
    )


if __name__ == "__main__":
    main()
