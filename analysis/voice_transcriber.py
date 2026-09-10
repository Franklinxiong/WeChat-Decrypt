# -*- coding: utf-8 -*-
"""语音消息转文字模块（SILK -> PCM/WAV -> sherpa-onnx 离线 ASR）。

设计目标：与现有解密 / 构建 / 分析链路完全隔离，零侵入。
  - 只读访问 decrypted/message 下的消息库与媒体库，绝不修改 decrypted 任何文件；
  - 不触碰 analysis.db 的 messages 等现有表，转写结果由调用方写入独立表 voice_transcripts；
  - SILK 解码：优先 pilk（微信专用，自动处理 0x02232153 腾讯头），其次 pysilk，再次 ffmpeg；
  - ASR：sherpa-onnx 的 SenseVoiceSmall（离线），输出经 <|xxx|> 富文本标签清理为纯文字；
  - 模型：不存在时从 ModelScope 下载到 ~/Documents/WeChatDecrypt/models/sensevoice/，
    支持 Range 断点续传、可重复执行。
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

SILK_MAGIC = b"\x02\x23\x21\x53"          # 微信 SILK v3 魔数
ASR_SAMPLE_RATE = 16000                    # SenseVoice 输入采样率（Hz）

DEFAULT_MODEL_DIR = os.path.expanduser(
    "~/Documents/WeChatDecrypt/models/sensevoice"
)
MODEL_REPO = "pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue"
MODEL_BASE_URL = (
    "https://modelscope.cn/models/" + MODEL_REPO + "/resolve/master/"
)
MODEL_FILES = {
    "model.int8.onnx": MODEL_BASE_URL + "model.int8.onnx",
    "tokens.txt":      MODEL_BASE_URL + "tokens.txt",
}
_MODEL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 腾讯头 -> 标准 SILK 头的修复（部分解码器需要标准头）
_TENCENT_HEAD = b"\x02\x23\x21\x53"
_STD_HEAD = b"#!SILK_V3"

# SenseVoice 富文本标签清理
_SENSEVOICE_TAG_RE = re.compile(r"<\|[^|>]*\|>")


class VoiceError(RuntimeError):
    """语音处理（提取 / 解码 / 转写）过程中的统一异常。"""

    pass


# ---------------------------------------------------------------------------
# 模型下载（可重复执行 + Range 断点续传）
# ---------------------------------------------------------------------------

def _download_with_resume(url: str, dest: str, timeout: int = 60) -> None:
    """使用 Range 断点续传下载单个文件，可重复执行。"""
    dest = str(dest)
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": _MODEL_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
    except Exception as exc:  # HEAD 失败不代表不可下载
        total = 0

    headers = {"User-Agent": _MODEL_UA, "Range": "bytes={}-".format(
        os.path.getsize(tmp))} if os.path.exists(tmp) and os.path.getsize(tmp) > 0 else {"User-Agent": _MODEL_UA}

    req = urllib.request.Request(url, headers=headers)
    mode = "ab" if "Range" in headers else "wb"
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, mode) as fh:
        while True:
            chunk = resp.read(1024 * 512)
            if not chunk:
                break
            fh.write(chunk)

    if total and os.path.getsize(tmp) < total:
        # 未下完：递归续传，直至完整
        _download_with_resume(url, dest, timeout=timeout)
        return
    os.replace(tmp, dest)


def ensure_model(model_dir: str | None = None) -> tuple[str, str]:
    """确保模型文件存在，缺则从 ModelScope 下载。

    返回 (model.onnx 路径, tokens.txt 路径)。
    """
    model_dir = model_dir or DEFAULT_MODEL_DIR
    os.makedirs(model_dir, exist_ok=True)
    missing = {f: MODEL_FILES[f] for f in MODEL_FILES
               if not os.path.isfile(os.path.join(model_dir, f))}
    if missing:
        print(f"[*] 下载 SenseVoice 模型到 {model_dir}（首次仅需联网一次）...")
        for name, url in missing.items():
            dest = os.path.join(model_dir, name)
            print(f"    下载 {name} -> {dest}")
            _download_with_resume(url, dest)
    model = os.path.join(model_dir, "model.int8.onnx")
    tokens = os.path.join(model_dir, "tokens.txt")
    for p in (model, tokens):
        if not os.path.isfile(p):
            raise ModelNotFoundError(
                "缺少模型文件: {}（请检查网络或 ModelScope 域名可达性）".format(p))
    return model, tokens


class ModelNotFoundError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 数据库只读访问（decrypted 媒体库 / 消息库）
# ---------------------------------------------------------------------------

def ro_connect(path: str) -> sqlite3.Connection:
    """只读 URI 连接 WAL 明文库（与 build_analysis.py 同策略）。"""
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    """PRAGMA table_info 探测真实列名，绝不硬编码。"""
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    except Exception:
        return []


def _pick(cols: list[str], *candidates: str):
    """在候选列名中取首个真实存在的列（大小写不敏感）。"""
    lower = {c.lower() for c in cols}
    for cand in candidates:
        if cand.lower() in lower:
            return next(c for c in cols if c.lower() == cand.lower())
    return None


class DECRYPTED_FILES:
    """decrypted/message 目录内的文件别名（用于过滤真实库文件）。"""


def _message_db_files(message_dir: str) -> list[str]:
    return sorted(
        f for f in os.listdir(message_dir)
        if f.endswith(".db") and not f.endswith(("-wal.db", "-shm.db"))
        and "message_" in f and "biz" not in f and "fts" not in f
        and "weclaw" not in f and "resource" not in f
    )


def _media_db_files(message_dir: str) -> list[str]:
    return sorted(
        f for f in os.listdir(message_dir)
        if f.endswith(".db") and not f.endswith(("-wal.db", "-shm.db"))
        and ("media_" in f or "msg" == os.path.splitext(f)[0])
    )


def _load_name2id(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[int, str]]:
    """读取 Name2Id：返回 (username->rowid, rowid->username)。

    chat_name_id 在微信库中即 Name2Id 的 ROWID（与 real_sender_id 同一索引体系）。
    """
    user2rid: dict[str, int] = {}
    rid2user: dict[int, str] = {}
    try:
        for rid, u in conn.execute("SELECT ROWID, user_name FROM Name2Id"):
            if isinstance(u, bytes):
                u = u.decode("utf-8", "replace")
            if u:
                user2rid.setdefault(u, int(rid))
                rid2user.setdefault(int(rid), u)
    except Exception:
        pass
    return user2rid, rid2user


class VoiceIndex:
    """聚合 media 库 VoiceInfo 表的语音索引，支持按会话+local_id 取 BLOB。

    关联语义（实测验证）：
      - 微信 media 库自带独立的 Name2Id 表（user_name 列，ROWID 为隐式索引）；
      - VoiceInfo.chat_name_id 即「同一 media 库内 Name2Id 的 ROWID」，
        与消息库 Name2Id 是两套独立索引，故必须用 media 本地 Name2Id 反查会话名；
      - VoiceInfo.local_id 与消息表 Msg_* 的 local_id 一致，可精确匹配；
      - create_time（秒精度）作为辅助键；同秒多条语音按 local_id 排序的相对索引防串音。
    """

    def __init__(self, message_dir: str):
        self.by_user_local: dict[tuple[str, int], bytes] = {}   # (username, local_id) -> voice_data
        self.by_user_time: dict[tuple[str, int], list[tuple[int, bytes]]] = {}  # (username, create_time) -> [(local_id, data)]
        self.user_count: int = 0
        for f in _media_db_files(message_dir):
            path = os.path.join(message_dir, f)
            try:
                conn = ro_connect(path)
            except Exception:
                continue
            try:
                rid2user = self._load_media_name2id(conn)
                if not rid2user:
                    continue
                for t in [r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'VoiceInfo%'")]:
                    self._load_table(conn, t, rid2user)
            finally:
                conn.close()

    @staticmethod
    def _load_media_name2id(conn) -> dict[int, str]:
        """读取 media 库自身的 Name2Id：ROWID -> user_name。"""
        out: dict[int, str] = {}
        try:
            for rid, u in conn.execute("SELECT ROWID, user_name FROM Name2Id"):
                if isinstance(u, bytes):
                    u = u.decode("utf-8", "replace")
                if u:
                    out.setdefault(int(rid), u)
        except Exception:
            pass
        return out

    def _load_table(self, conn, table: str, rid2user: dict[int, str]) -> None:
        cols = columns_of(conn, table)
        c_chat = _pick(cols, "chat_name_id", "session_id", "chatId")
        c_time = _pick(cols, "create_time", "timestamp", "createTime")
        c_lid = _pick(cols, "local_id", "localId")
        c_data = _pick(cols, "voice_data", "buf", "voicebuf", "data")
        if not (c_chat and c_lid and c_data):
            return
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            d = dict(zip(cols, row))
            rid = d.get(c_chat)
            lid = d.get(c_lid)
            data = d.get(c_data)
            if rid is None or lid is None or not data:
                continue
            try:
                rid, lid = int(rid), int(lid)
            except (TypeError, ValueError):
                continue
            username = rid2user.get(rid)
            if not username:
                continue
            if isinstance(data, str):
                data = data.encode("latin-1", "replace")
            data = bytes(data)
            self.by_user_local[(username, lid)] = data
            self.user_count += 1
            ct = d.get(c_time)
            if ct is not None:
                try:
                    ct = int(ct)
                    self.by_user_time.setdefault((username, ct), []).append((lid, data))
                except (TypeError, ValueError):
                    pass

    def get(self, username: str, local_id: int, create_time: int | None) -> bytes | None:
        """按 (username, local_id) 取语音；缺失时兜底按 (username, create_time) + local_id 相对索引。"""
        data = self.by_user_local.get((username, local_id))
        if data is not None:
            return data
        if create_time is not None:
            group = self.by_user_time.get((username, int(create_time)))
            if group:
                # 同秒多条：按 local_id 排序取相对索引，防串音
                group.sort(key=lambda x: x[0])
                lids = [x[0] for x in group]
                if local_id in lids:
                    return group[lids.index(local_id)][1]
        return None

    def __len__(self):
        return self.user_count


# ---------------------------------------------------------------------------
# SILK 解码（pilk -> pysilk -> ffmpeg，逐级兜底）
# ---------------------------------------------------------------------------

def decode_silk_to_pcm(silk_bytes: bytes) -> tuple[bytes, int]:
    """把 SILK BLOB 解码为 s16le PCM。

    返回 (pcm_bytes, sample_rate)。微信 SILK 采样率为 24000 Hz。
    微信 SILK v3 魔数为 02 23 21 53，pilk 可自动处理；如遇头异常先修复为标准头。
    """
    if not silk_bytes:
        raise VoiceError("sil 数据为空")

    # 1) pilk（微信专用，自动处理腾讯头）
    decoded, rate = _decode_with_pilk(silk_bytes)
    if decoded is not None:
        return decoded, rate

    # 2) pysilk（部分发行版以 pysilk 命名提供同 API）
    decoded, rate = _decode_with_pysilk(silk_bytes)
    if decoded is not None:
        return decoded, rate

    # 3) ffmpeg（兜底；对 SILK 支持依赖编译选项）
    decoded, rate = _decode_with_ffmpeg(silk_bytes)
    if decoded is not None:
        return decoded, rate

    raise VoiceError(
        "SILK 解码失败：未找到可用解码器。请安装 pilk（pip install pilk）"
        "或 pysilk-mod，或安装含 SILK 解码的 ffmpeg。"
    )


def _write_silk_tmp(silk_bytes: bytes) -> tuple[str, bool]:
    """写入临时 .silk 文件，返回 (path, 是否标准头)。

    p 微信 BLOB 若以 02 23 21 53 开头：写入前替换为标准头，另存原始以便两种都试。
    """
    if silk_bytes[:4] == _TENCENT_HEAD:
        fixed = _STD_HEAD + silk_bytes[4:]
        return fixed, True
    return silk_bytes, False


def _decode_with_pilk(silk_bytes: bytes) -> tuple[bytes | None, int | None]:
    try:
        import pilk
    except Exception:
        return None, None
    raw = silk_bytes
    with tempfile.TemporaryDirectory() as td:
        silk_path = os.path.join(td, "v.silk")
        pcm_path = os.path.join(td, "v.pcm")
        # 先试原样
        with open(silk_path, "wb") as f:
            f.write(raw)
        try:
            pilk.decode(silk_path, pcm_path, 24000)
            with open(pcm_path, "rb") as f:
                return f.read(), 24000
        except Exception:
            pass
        # 修复腾讯头后再试（部分库为标准头）
        if raw[:4] == _TENCENT_HEAD:
            fixed = _STD_HEAD + raw[4:]
            with open(silk_path, "wb") as f:
                f.write(fixed)
            try:
                pilk.decode(silk_path, pcm_path, 24000)
                with open(pcm_path, "rb") as f:
                    return f.read(), 24000
            except Exception:
                pass
    return None, None


def _decode_with_pysilk(silk_bytes: bytes) -> tuple[bytes | None, int | None]:
    try:
        import pysilk  # 部分发行版 pysilk-mod 亦以 import pysilk 提供
    except Exception:
        return None, None
    try:
        pcm = pysilk.decode(silk_bytes, 24000)
        if hasattr(pcm, "tobytes"):
            pcm = pcm.tobytes()
        return bytes(pcm), 24000
    except Exception:
        pass
    # 修复头再试
    if silk_bytes[:4] == _TENCENT_HEAD:
        fixed = _STD_HEAD + silk_bytes[4:]
        try:
            pcm = pysilk.decode(fixed, 24000)
            if hasattr(pcm, "tobytes"):
                pcm = pcm.tobytes()
            return bytes(pcm), 24000
        except Exception:
            pass
    return None, None


def _decode_with_ffmpeg(silk_bytes: bytes) -> tuple[bytes | None, int | None]:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
    except Exception:
        return None, None
    with tempfile.TemporaryDirectory() as td:
        silk_path = os.path.join(td, "v.silk")
        pcm_path = os.path.join(td, "v.pcm")
        with open(silk_path, "wb") as f:
            f.write(silk_bytes)
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", silk_path,
                 "-f", "s16le", "-ar", "24000", "-ac", "1", pcm_path],
                capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(pcm_path) and os.path.getsize(pcm_path) > 0:
                with open(pcm_path, "rb") as f:
                    return f.read(), 24000
        except Exception:
            pass
    return None, None


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """PCM(s16le, mono) -> WAV（44 字节标准头）。"""
    n = len(pcm) // 2
    data_size = n * 2
    header = b"RIFF" + (36 + data_size).to_bytes(4, "little") + b"WAVE" \
        + b"fmt " + (16).to_bytes(4, "little") \
        + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") \
        + sample_rate.to_bytes(4, "little") \
        + (sample_rate * 2).to_bytes(4, "little") \
        + (2).to_bytes(2, "little") + (16).to_bytes(2, "little") \
        + b"data" + data_size.to_bytes(4, "little")
    return header + pcm


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """把 s16le mono PCM 线性重采样到目标采样率（24000 -> 16000）。"""
    if src_rate == dst_rate or not pcm:
        return pcm
    import array
    src = array.array("h", pcm)
    if len(src) < 2:
        return pcm
    n = int(len(src) * dst_rate / src_rate)
    out = array.array("h")
    for i in range(n):
        pos = i * src_rate / dst_rate
        i0 = int(pos)
        i1 = min(i0 + 1, len(src) - 1)
        frac = pos - i0
        out.append(int(src[i0] * (1 - frac) + src[i1] * frac))
    return out.tobytes()


class SenseVoiceTranscriber:
    """sherpa-onnx SenseVoiceSmall 离线 ASR 封装。"""

    def __init__(self, model_dir: str | None = None, num_threads: int = 2):
        try:
            import sherpa_onnx
        except Exception as exc:
            raise VoiceError(
                "缺少 sherpa-onnx：请先 pip install sherpa-onnx（原始错误: {}）".format(exc))
        self._sherpa = sherpa_onnx
        model, tokens = ensure_model(model_dir)
        self.model_dir = os.path.dirname(model)
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model,
            tokens=tokens,
            num_threads=num_threads,
            sample_rate=ASR_SAMPLE_RATE,
            feature_dim=80,
            use_itn=True,
            debug=False,
        )

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = 24000) -> str:
        """PCM -> 纯文字（自动清理 SenseVoice 富文本标签）。"""
        if not pcm:
            return ""
        pcm16 = resample_pcm16(pcm, sample_rate, ASR_SAMPLE_RATE)
        import array
        import numpy as np
        arr = array.array("h", pcm16)
        wf = np.asarray(arr, dtype=np.float32) / 32768.0
        if wf.size < ASR_SAMPLE_RATE // 20:  # 过短过滤
            return ""
        s = self.recognizer.create_stream()
        s.accept_waveform(ASR_SAMPLE_RATE, wf)
        self.recognizer.decode_stream(s)
        return clean_sensevoice_text(s.result.text)


def clean_sensevoice_text(text: str) -> str:
    """去掉 SenseVoice 的 <|itn|> / <|HAPPY|> / 语言标签等富文本标记。"""
    if not text:
        return ""
    return _SENSEVOICE_TAG_RE.sub("", text).strip()
