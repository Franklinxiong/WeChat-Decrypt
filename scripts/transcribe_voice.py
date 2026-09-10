#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音消息转文字 CLI（最小侵入方案落地入口）。

把 ★解密库 decrypted 中的语音消息（SILK）逐条转写为文字，
结果写入分析库 analysis.db 的独立表 voice_transcripts（只增不覆盖，
绝不动 messages 等现有表的任何数据）。

用法：
    python3 scripts/transcribe_voice.py                 # 全量转写
    python3 scripts/transcribe_voice.py --dry-run       # 只统计语音消息数，不转写
    python3 scripts/transcribe_voice.py --limit 3       # 限量测试前 3 条
    python3 scripts/transcribe_voice.py --batch         # 批量模式：全部转写后一次写入
    python3 scripts/transcribe_voice.py --db PATH --decrypted DIR --model-dir DIR

数据流：
  analysis.db.messages(local_type in (6,34))
    -> decrypted/message 中 VoiceIndex 按 (chat_name_id=Name2Id.rowid, local_id) 取 voice_data
    -> SILK 解码 PCM(24k) -> WAV -> 重采样 16k -> sherpa-onnx(SenseVoiceSmall) -> 纯文字
    -> INSERT OR REPLACE 到 voice_transcripts(session, local_id, create_time, voice_text)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis.voice_transcriber as vt  # noqa: E402

DEFAULT_DECRYPTED = os.path.expanduser("~/Desktop/WeChat-Decrypt/decrypted")
DEFAULT_DB = os.path.expanduser("~/WeChatData/analysis.db")

VOICE_TYPES = (6, 34)


# ---------------------------------------------------------------------------
# 读取 analysis.db 中的语音消息清单
# ---------------------------------------------------------------------------

def _voice_rows_from_analysis(db_path: str) -> list[tuple]:
    """从 analysis.db messages 表取 (session_username, local_id, create_time)。

    仅读取，绝不对 messages 表做任何写入。
    """
    if not os.path.exists(db_path):
        print(f"[err] 分析库不存在: {db_path}")
        return []
    conn = sqlite3.connect(db_path)
    rows = []
    try:
        placeholders = ",".join("?" * len(VOICE_TYPES))
        for r in conn.execute(
            "SELECT session_username, local_id, create_time, content_text "
            f"FROM messages WHERE local_type IN ({placeholders})",
            VOICE_TYPES,
        ):
            session, lid, ct, text = r
            rows.append((session, lid, ct, text or ""))
    finally:
        conn.close()
    return rows


# ---------------------------------------------------------------------------
# 文本持久化到独立表 voice_transcripts
# ---------------------------------------------------------------------------

_VT_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_transcripts(
    session TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    create_time INTEGER NOT NULL,
    voice_text TEXT,
    PRIMARY KEY (session, local_id, create_time)
)"""


def _write_transcripts(db_path: str, records: list[tuple], batch: bool) -> None:
    """records: (session, local_id, create_time, voice_text) 幂等写入独立表。"""
    if not records:
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_VT_SCHEMA)
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO voice_transcripts"
                "(session, local_id, create_time, voice_text) VALUES(?,?,?,?)",
                records,
            )
            conn.commit()
        else:
            for rec in records:
                conn.execute(
                    "INSERT OR REPLACE INTO voice_transcripts"
                    "(session, local_id, create_time, voice_text) VALUES(?,?,?,?)",
                    rec,
                )
                conn.commit()
    finally:
        conn.close()


def _count_transcripts(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM voice_transcripts").fetchone()[0]
    except Exception:
        n = 0
    finally:
        conn.close()
    return int(n)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="微信语音消息转文字（最小侵入）")
    ap.add_argument("--db", default=DEFAULT_DB, help="analysis.db 路径")
    ap.add_argument("--decrypted", default=DEFAULT_DECRYPTED,
                    help="解密库目录（默认 ~/Desktop/WeChat-Decrypt/decrypted）")
    ap.add_argument("--model-dir", default=vt.DEFAULT_MODEL_DIR,
                    help="SenseVoice 模型目录（不存在自动从 ModelScope 下载）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不转写")
    ap.add_argument("--limit", type=int, default=None, help="限量转写条数")
    ap.add_argument("--batch", action="store_true",
                    help="批量模式：全部转写后一次性写入（默认逐条即转即写）")
    args = ap.parse_args()

    # ---- 1) 统计 ----
    rows = _voice_rows_from_analysis(args.db)
    if not rows:
        print(f"[done] analysis.db 中没有语音消息（local_type in {VOICE_TYPES}）")
        return
    print(f"[*] analysis.db 语音消息共 {len(rows)} 条")

    # ---- 2) dry-run ----
    if args.dry_run:
        total = len(rows)
        sessions = len({r[0] for r in rows})
        existing = sum(1 for r in rows if r[1] is not None)  # 占位用
        print(f"[dry-run] 语音消息 {total} 条 / {sessions} 个会话")
        print("[dry-run] 已转写(独立表)条数:", _count_transcripts(args.db))
        print("[dry-run] 本次将仅新增 voice_transcripts 表数据，不触碰 messages 等现有表")
        return

    # ---- 3) 限量 ----
    if args.limit:
        rows = rows[: args.limit]
        print(f"[*] 限量转写前 {len(rows)} 条")

    # ---- 4) 模型与索引 ----
    print("[*] 确保模型就绪（首次会联网下载，可断点续传）...")
    vt.ensure_model(args.model_dir)
    transcriber = vt.SenseVoiceTranscriber(args.model_dir)
    idx = vt.VoiceIndex(os.path.join(args.decrypted, "message"))
    print(f"[*] VoiceIndex 已载入语音 {len(idx)} 条（按 media 库自身 Name2Id 关联会话）")

    # ---- 5) 逐条转写 ----
    records: list[tuple] = []
    ok = fail = skip_no = 0
    for session, lid, ct, legacy_text in rows:
        data = idx.get(session, lid, ct)
        if not data:
            skip_no += 1
            print(f"  [skip] 未找到语音 BLOB: session={session} local_id={lid}")
            continue
        try:
            pcm, rate = vt.decode_silk_to_pcm(data)
            text = transcriber.transcribe_pcm(pcm, rate)
            text = vt.clean_sensevoice_text(text)
            records.append((session, lid, ct, text))
            ok += 1
            print(f"  [ok] {session} #{lid} -> {text[:60] or '(空识别)'}")
            if not args.batch:
                _write_transcripts(args.db, [records[-1]], batch=True)
                records.pop()
        except Exception as exc:
            fail += 1
            print(f"  [fail] session={session} local_id={lid}: {exc}")

    # ---- 6) 批量落盘 ----
    if args.batch and records:
        _write_transcripts(args.db, records, batch=True)

    print()
    print("[done] 转写结束")
    print(f"  成功 {ok} / 失败 {fail} / 跳过(无可提取BLOB) {skip_no}")
    print(f"  voice_transcripts 累计 {_count_transcripts(args.db)} 条（独立表，未动任何现有表）")


if __name__ == "__main__":
    main()
