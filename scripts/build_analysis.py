#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机解密后的微信数据库（~/WeChatData/wechat_decrypted/）构建为统一分析库 ~/WeChatData/analysis.db。

只读取你本机自己解密出来的数据库，产物 analysis.db 也仅存本机，切勿对外分发。
用途：为 app.py 提供与示例库同构、但来自真实聊天记录的 sessions/contacts/senders/messages 表。

用法:
    python3 scripts/build_analysis.py [--src ~/WeChatData/wechat_decrypted] [--out ~/WeChatData/analysis.db]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import zlib
from datetime import datetime, timezone

try:
    import zstandard as zstd
    _ZSTD = True
except Exception:
    _ZSTD = False


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_dctx = zstd.ZstdDecompressor() if _ZSTD else None

# local_type -> 类型名（微信 Mac 常见取值）
TYPE_NAMES = {
    1: "文本", 2: "文本", 3: "图片", 4: "位置", 5: "名片", 6: "语音",
    7: "视频", 8: "红包", 9: "图片", 10: "文件", 13: "系统", 17: "表情",
    34: "语音", 35: "文件", 36: "位置", 37: "联系人", 40: "视频", 42: "文件",
    43: "视频", 44: "视频", 45: "视频通话", 46: "语音通话", 47: "表情",
    48: "位置", 49: "文件/链接", 50: "视频通话", 51: "视频通话", 52: "语音通话",
    53: "视频通话", 54: "系统", 55: "系统", 56: "系统", 57: "引用",
    62: "小程序", 63: "视频号", 66: "视频号", 10000: "系统消息",
    10002: "撤回消息", 10004: "拍一拍", 1048625: "拍一拍",
}
TEXTISH = {1, 2}
SIMPLE_PLACEHOLDER = {
    3: "[图片]", 6: "[语音]", 7: "[视频]", 34: "[语音]", 40: "[视频]",
    43: "[视频]", 47: "[表情]", 48: "[位置]",
}
USERNAME_RE = re.compile(
    r"^(wxid_[A-Za-z0-9_\-]+|gh_[A-Za-z0-9_\-]+|[A-Za-z0-9_\-]+@chatroom|[1-9]\d{6,15}):\r?\n"
)
XML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)
XML_DES_RE = re.compile(r"<des[^>]*>(.*?)</des>", re.S)


class MessageDB:
    """打开一个 message_*.db，提供会话列表与消息迭代。"""

    def __init__(self, path: str):
        self.path = path
        # 注意：解出来的库带 WAL，用 mode=ro 会读不到主文件数据，
        # 因此用默认连接并置 query_only 防意外写入。
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA query_only=1")
        self.conn.text_factory = lambda b: b  # 保持 bytes，压缩判定用
        self.name2id = self._load_name2id()

    def _load_name2id(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        try:
            rows = self.conn.execute("SELECT user_name, is_session FROM Name2Id")
            for u, iss in rows:
                if isinstance(u, bytes):
                    u = u.decode("utf-8", "replace")
                if u:
                    out[u] = bool(iss)
        except Exception:
            pass
        return out

    def session_tables(self) -> list[tuple[str, bool]]:
        """返回 [(username, is_session), ...]，只列出有真实 Msg 表的会话。"""
        result = []
        for u, iss in self.name2id.items():
            h = hashlib.md5(u.encode()).hexdigest()
            if self.table_exists(f"Msg_{h}"):
                result.append((u, iss))
        return result

    def table_exists(self, name: str) -> bool:
        try:
            self.conn.execute(f'SELECT 1 FROM "{name}" LIMIT 1')
            return True
        except Exception:
            return False

    def iter_rows(self, username: str):
        h = hashlib.md5(username.encode()).hexdigest()
        tab = f"Msg_{h}"
        cols = [c[1].decode() if isinstance(c[1], bytes) else c[1]
                for c in self.conn.execute(f'PRAGMA table_info("{tab}")')]
        want = ["local_id", "local_type", "create_time", "message_content",
                "WCDB_CT_message_content", "real_sender_id"]
        idcs = {c: i for i, c in enumerate(cols)}
        for row in self.conn.execute(f'SELECT * FROM "{tab}"'):
            get = lambda c: row[idcs[c]] if c in idcs else None
            yield {
                "local_id": get("local_id"),
                "local_type": get("local_type"),
                "create_time": get("create_time"),
                "content_raw": get("message_content"),
                "compressed": get("WCDB_CT_message_content"),
                "real_sender_id": get("real_sender_id"),
            }

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def decompress_if_needed(raw) -> bytes | str:
    """message_content 可能是 zstd 压缩（以魔数开头），也可能是明文。"""
    if raw is None:
        return b""
    if isinstance(raw, bytes) and raw[:4] == ZSTD_MAGIC:
        if _ZSTD:
            try:
                return _dctx.decompress(raw)
            except Exception:
                pass
        # 兜底：尝试 zlib（部分版本用 zlib 窗口）
        try:
            return zlib.decompress(raw, 47)
        except Exception:
            return raw
    return raw


def split_sender(payload) -> tuple[str | None, str]:
    """去掉 'wxid_xxx:\n' 前缀，返回 (sender_username, body)。"""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8", "replace")
        except Exception:
            payload = payload.decode("latin-1", "replace")
    if not payload:
        return None, payload
    m = USERNAME_RE.match(payload)
    if m:
        return m.group(1), payload[m.end():]
    return None, payload


def extract_text(local_type: int, body: str) -> str:
    """从消息体提取可读文本。"""
    local_type = int(local_type or 0)
    body = (body or "").strip()
    if local_type in TEXTISH:
        return body
    if local_type in SIMPLE_PLACEHOLDER:
        return SIMPLE_PLACEHOLDER[local_type]
    if local_type == 49:
        t = XML_TITLE_RE.search(body)
        d = XML_DES_RE.search(body)
        parts = [g for g in (t.group(1) if t else None, d.group(1) if d else None) if g and g.strip()]
        return " | ".join(parts)[:500] if parts else "[文件/链接]"
    if local_type == 10000 or local_type == 10002:
        return body[:500]
    return body[:200] or ""


def display_name_of(contact: dict[str, str], username: str) -> str:
    """联系人显示名：备注 > 昵称 > wxid 短号。"""
    c = contact.get(username)
    if c:
        if c.get("remark"):
            return c["remark"]
        if c.get("nick_name"):
            return c["nick_name"]
    if username.endswith("@chatroom"):
        return "群聊"
    return username


def build(src: str, out: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    message_dir = os.path.join(src, "message")
    contact_db = os.path.join(src, "contact", "contact.db")
    session_db = os.path.join(src, "session", "session.db")

    # ---- 联系人映射 ----
    contact: dict[str, dict[str, str]] = {}
    contact_rows: list[tuple] = []
    if os.path.exists(contact_db):
        try:
            conn = sqlite3.connect(contact_db)
            conn.execute("PRAGMA query_only=1")
            conn.text_factory = lambda b: b
            cols = [c[1].decode() if isinstance(c[1], bytes) else c[1]
                    for c in conn.execute("PRAGMA table_info(contact)")]
            for row in conn.execute("SELECT * FROM contact"):
                d = {c: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                     for c, v in zip(cols, row)}
                uname = d.get("username") or ""
                if uname:
                    contact[uname] = d
                contact_rows.append(tuple(d.get(c) for c in
                                          ["id", "username", "local_type", "remark", "nick_name", "is_chatroom"]))
            conn.close()
        except Exception as e:
            print("[warn] contact.db 读取失败:", e)

    # ---- session 会话 ----
    session_meta: dict[str, dict] = {}
    if os.path.exists(session_db):
        try:
            conn = sqlite3.connect(session_db)
            conn.execute("PRAGMA query_only=1")
            conn.text_factory = lambda b: b
            cols = [c[1].decode() if isinstance(c[1], bytes) else c[1]
                    for c in conn.execute("PRAGMA table_info(SessionTable)")]
            for row in conn.execute("SELECT * FROM SessionTable"):
                d = {c: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                     for c, v in zip(cols, row)}
                uname = d.get("username")
                if uname:
                    session_meta[uname] = d
            conn.close()
        except Exception as e:
            print("[warn] session.db 读取失败:", e)

    # ---- 遍历消息库 ----
    sessions: dict[str, dict] = {}        # username -> session 汇总
    senders: dict[str, dict] = {}         # username -> sender 汇总
    messages: list[tuple] = []
    db_files = sorted(
        f for f in os.listdir(message_dir)
        if f.endswith(".db") and not f.endswith(("-wal.db", "-shm.db"))
        and "message_" in f and "biz" not in f and "fts" not in f
        and "media" not in f and "weclaw" not in f and "resource" not in f
    )
    if not db_files:
        print(f"[err] 未在 {message_dir} 找到 message_*.db")
        return

    for fname in db_files:
        path = os.path.join(message_dir, fname)
        print(f"[*] 处理 {fname} ...")
        try:
            mdb = MessageDB(path)
        except Exception as e:
            print("   跳过:", e)
            continue
        try:
            for username, is_session in mdb.session_tables():
                if username not in sessions:
                    smd = session_meta.get(username, {})
                    sessions[username] = {
                        "username": username,
                        "display_name": smd.get("display_name") or "",
                        "is_group": int(username.endswith("@chatroom")),
                        "last_timestamp": smd.get("last_timestamp"),
                        "msg_count": 0,
                    }
                for msg in mdb.iter_rows(username):
                    raw = msg["content_raw"]
                    payload = decompress_if_needed(raw)
                    sender, body = split_sender(payload)
                    if sender is None:
                        sender = body.split("\n", 1)[0].strip()[:64] or None
                        body = body
                    if sender and sender not in senders:
                        senders[sender] = {"msg_count": 0, "first_seen": None, "last_seen": None}
                    if sender:
                        s = senders[sender]
                        s["msg_count"] += 1
                        ct = msg["create_time"]
                        if isinstance(ct, (int, float)):
                            s["first_seen"] = ct if s["first_seen"] is None else min(s["first_seen"], ct)
                            s["last_seen"] = ct if s["last_seen"] is None else max(s["last_seen"], ct)
                    sessions[username]["msg_count"] += 1
                    lt = msg["local_type"] or 0
                    text = extract_text(lt, body)
                    ct = msg["create_time"]
                    if isinstance(ct, (int, float)):
                        cur = sessions[username]["last_timestamp"]
                        if not isinstance(cur, (int, float)) or ct > cur:
                            sessions[username]["last_timestamp"] = ct
                    messages.append((msg["local_id"], username, sender, ct, lt, text))
        finally:
            mdb.close()

    if not messages:
        print("[err] 未提取到任何消息，请确认已解密 message 库。")
        return

    # ---- 写库 ----
    if os.path.exists(out):
        os.remove(out)
    con = sqlite3.connect(out)
    with con:
        con.execute("""
            CREATE TABLE sessions(
                session_slot INTEGER PRIMARY KEY,
                username TEXT, display_name TEXT, is_group INTEGER,
                unread_count INTEGER, last_timestamp INTEGER, msg_count_est INTEGER,
                summary TEXT
            )""")
        con.execute("""
            CREATE TABLE contacts(
                id INTEGER, username TEXT, local_type INTEGER, remark TEXT,
                nick_name TEXT, is_chatroom INTEGER
            )""")
        con.execute("""
            CREATE TABLE senders(
                sender_slot INTEGER PRIMARY KEY,
                username TEXT, display_name TEXT, msg_count INTEGER,
                first_seen INTEGER, last_seen INTEGER
            )""")
        con.execute("""
            CREATE TABLE messages(
                local_id INTEGER, session_slot INTEGER, session_username TEXT,
                sender_slot INTEGER, sender_username TEXT, sender_display_name TEXT,
                create_time INTEGER, local_type INTEGER, type_name TEXT, content_text TEXT
            )""")
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")

        session_slots: dict[str, int] = {}
        for i, uname in enumerate(sorted(sessions.keys())):
            session_slots[uname] = i
        sender_slots: dict[str, int] = {}
        for i, uname in enumerate(sorted(set(m[2] for m in messages if m[2]))):
            sender_slots[uname] = i

        # sessions
        for uname, s in sessions.items():
            con.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                (session_slots[uname], uname,
                 display_name_of(contact, uname),
                 s["is_group"], 0, s["last_timestamp"], s["msg_count"], ""))
        # contacts
        con.executemany(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            [tuple(None if v is None else v for v in r) for r in contact_rows])
        # senders
        for uname, s in senders.items():
            con.execute(
                "INSERT INTO senders VALUES(?,?,?,?,?,?)",
                (sender_slots[uname], uname, display_name_of(contact, uname),
                 s["msg_count"], s["first_seen"], s["last_seen"]))
        # messages
        for mid, session_u, sender_u, ct, lt, text in messages:
            st = session_slots.get(session_u)
            if st is None:
                continue
            ss = sender_slots.get(sender_u) if sender_u else None
            con.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mid, st, session_u, ss, sender_u,
                 display_name_of(contact, sender_u) if sender_u else None,
                 ct, lt, TYPE_NAMES.get(int(lt or 0), "其他"), text))
        # meta
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta = {
            "source": "real-decrypted (本机解密)", "generated_at": now,
            "message_count": str(len(messages)),
            "session_count": str(len(sessions)),
            "sender_count": str(len(senders)),
            "note": "来自用户本机解密库，仅本机使用，勿分发",
        }
        con.executemany("INSERT INTO meta VALUES(?,?)", list(meta.items()))

    con.close()
    print(f"\n[done] 已生成 {out}")
    print(f"  消息 {len(messages)} 条 | 会话 {len(sessions)} | 发送者 {len(senders)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="解密库 -> analysis.db")
    ap.add_argument("--src", default=os.path.expanduser(os.path.join("~", "WeChatData", "wechat_decrypted")))
    ap.add_argument("--out", default=os.path.expanduser(os.path.join("~", "WeChatData", "analysis.db")))
    args = ap.parse_args()
    build(args.src, args.out)


if __name__ == "__main__":
    main()
