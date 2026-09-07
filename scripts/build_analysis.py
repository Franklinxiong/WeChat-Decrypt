#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机解密后的微信数据库（~/Desktop/WeChat-Decrypt/decrypted/）构建为统一分析库 ~/WeChatData/analysis.db。

只读取你本机自己解密出来的数据库，产物 analysis.db 也仅存本机，切勿对外分发。
decrypted 为当前项目下最新解析出的明文库（root 属主、WAL 模式且无 -wal 残留），
普通 sqlite3.connect 会因只读目录/库报 "attempt to write a readonly database"，
因此本脚本统一改用 `file:<path>?mode=ro&immutable=1` URI 只读连接读取。
用途：为 app.py 提供与示例库同构、但来自真实聊天记录的 sessions/contacts/senders/messages 表。

用法:
    python3 scripts/build_analysis.py [--src ~/Desktop/WeChat-Decrypt/decrypted] [--out ~/WeChatData/analysis.db]
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
# 系统类消息：既非"自己发送"也非"对方主动发言"，收发统计时按接收方/系统处理
SYS_TYPES = {10000, 10002, 10004, 1048625}
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

    def __init__(self, path: str, self_wxid: str | None = None):
        self.path = path
        # 注意：decrypted 目录为 root 属主、WAL 模式库且无 -wal 残留，
        # 普通 connect 会因只读报 "attempt to write a readonly database"，
        # 故统一用 `file:<path>?mode=ro&immutable=1` URI 只读连接（通过校验）。
        self.conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        self.conn.text_factory = lambda b: b  # 保持 bytes，压缩判定用
        self.name2id = self._load_name2id()          # username -> is_session
        self.rowid2user = self._load_rowid2user()    # rowid -> username（real_sender_id 的反查索引）
        self.self_sender_id = self._find_self_sender_id(self_wxid)

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

    def _load_rowid2user(self) -> dict[int, str]:
        """Name2Id 的 rowid 即消息表 real_sender_id 的索引，
        Python 侧仅能通过 obj.? 拿到隐式 rowid，故用 ROWID 显式查询。"""
        out: dict[int, str] = {}
        try:
            rows = self.conn.execute("SELECT ROWID, user_name FROM Name2Id")
            for rid, u in rows:
                if isinstance(u, bytes):
                    u = u.decode("utf-8", "replace")
                if u:
                    out[int(rid)] = u
        except Exception:
            pass
        return out

    def _find_self_sender_id(self, self_wxid: str | None) -> int | None:
        """定位"本机 wxid 对应的 sender slot id"。

        微信消息的 real_sender_id 语义上等于 Name2Id 的 rowid：本机在自己
        账号下拥有一个固定的 slot（各库不一定都是 2），对方则是该会话在
        Name2Id 中的 rowid。给定本机 wxid，即可在每库独立解析出 self slot，
        从而在纯 SQL 层复刻 WeFlow 用 wcdb_set_my_wxid 计算 is_send 的机制。
        """
        if not self_wxid:
            return None
        for rid, u in self.rowid2user.items():
            if u == self_wxid:
                return rid
        return None

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


def clean_wxid(name: str) -> str:
    """清洗账号目录名为标准 wxid（复刻 WeFlow cleanAccountDirName）。

    wxid_xxx_3dff -> wxid_xxx；自定义号_4位后缀 -> 自定义号；否则原样。
    清洗结果用于与 Name2Id 中 user_name 匹配（本机 account 也登记在 Name2Id 中）。
    """
    name = (name or "").strip()
    if not name:
        return name
    if name.lower().startswith("wxid_"):
        m = re.match(r"^(wxid_[^_]+)", name, re.I)
        if m:
            return m.group(1)
        return name
    m = re.match(r"^(.+)_([a-zA-Z0-9]{4})$", name)
    if m:
        return m.group(1)
    return name


def _detect_self_wxid(src: str) -> str | None:
    """自动推导本机 wxid：扫描微信容器账号目录名（形如 wxid_xxx_3dff / wxid_xxx）。

    清洗规则与 WeFlow 的 cleanAccountDirName 一致：
      - wxid_ 开头：取第一个 "_" 之后首段 + wxid_ 前缀
      - 其他（自定义微信号_4位后缀）：去掉 4 位后缀。
    返回清洗后的 wxid（对应 Name2Id 中 user_name 的取值）。
    """
    import fnmatch

    if src and os.path.isdir(src):
        for top in (src, os.path.dirname(src)):
            try:
                for d in os.listdir(top):
                    if d.startswith("wxid_") and os.path.isdir(os.path.join(top, d)):
                        return clean_wxid(d)
            except Exception:
                continue
    # 微信沙盒容器
    candidates = [
        os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data"),
        os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"),
    ]
    for base_c in candidates:
        if not os.path.isdir(base_c):
            continue
        try:
            for d in os.listdir(base_c):
                if d.startswith("wxid_") and os.path.isdir(os.path.join(base_c, d)):
                    return clean_wxid(d)
        except Exception:
            continue
    return None


def build(src: str, out: str, self_wxid: str | None = None) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if not self_wxid:
        self_wxid = _detect_self_wxid(src)
    if self_wxid:
        print(f"[*] 本机 wxid (self): {self_wxid}")
    else:
        print("[warn] 未识别本机 wxid，私聊方向判定将退化为旧规则（仅群聊有效）")
    message_dir = os.path.join(src, "message")
    contact_db = os.path.join(src, "contact", "contact.db")
    session_db = os.path.join(src, "session", "session.db")

    # ---- 联系人映射 ----
    contact: dict[str, dict[str, str]] = {}
    contact_rows: list[tuple] = []
    if os.path.exists(contact_db):
        try:
            conn = sqlite3.connect(f"file:{contact_db}?mode=ro&immutable=1", uri=True)
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
            conn = sqlite3.connect(f"file:{session_db}?mode=ro&immutable=1", uri=True)
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
            mdb = MessageDB(path, self_wxid)
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
                is_group = username.endswith("@chatroom")
                for msg in mdb.iter_rows(username):
                    raw = msg["content_raw"]
                    payload = decompress_if_needed(raw)
                    sender, body = split_sender(payload)   # 群聊前缀发送者；私聊无前缀则 None
                    rsid = msg["real_sender_id"]
                    rsid_user = mdb.rowid2user.get(int(rsid)) if isinstance(rsid, (int, float)) else None
                    lt = msg["local_type"] or 0
                    text = extract_text(lt, body)
                    ct = msg["create_time"]
                    # ---- 方向判定：复刻 WeFlow 机制 ----
                    # 群聊：前缀即真实发送者；私聊（无前缀）：real_sender_id 反查 Name2Id
                    # 得出真实发送者，与"无前缀即本人"的旧规则彻底解耦。
                    if sender is None and rsid_user:
                        sender = rsid_user
                    is_send = 0
                    if mdb.self_sender_id is not None and rsid is not None:
                        is_send = 1 if int(rsid) == mdb.self_sender_id else 0
                    elif sender is not None:
                        is_send = 1 if sender == self_wxid else 0
                    # 系统类消息不计入对发言人（收发统计归系统/接收方）
                    if int(lt) in SYS_TYPES:
                        is_send = 1 if (sender is not None and sender == self_wxid) else 0
                    # 私聊下若 sender 仍未解析出（无前缀且反查失败），归为本人兜底
                    if not is_group and sender is None:
                        sender = self_wxid
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
                    if isinstance(ct, (int, float)):
                        cur = sessions[username]["last_timestamp"]
                        if not isinstance(cur, (int, float)) or ct > cur:
                            sessions[username]["last_timestamp"] = ct
                    messages.append((msg["local_id"], username, sender, ct, lt, text, is_send))
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
                create_time INTEGER, local_type INTEGER, type_name TEXT, content_text TEXT,
                is_send INTEGER
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
        for mid, session_u, sender_u, ct, lt, text, is_send in messages:
            st = session_slots.get(session_u)
            if st is None:
                continue
            ss = sender_slots.get(sender_u) if sender_u else None
            con.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mid, st, session_u, ss, sender_u,
                 display_name_of(contact, sender_u) if sender_u else None,
                 ct, lt, TYPE_NAMES.get(int(lt or 0), "其他"), text, is_send))
        # meta
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta = {
            "source": f"decrypted (本机解密: {src})", "generated_at": now,
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
    ap.add_argument("--src", default=os.path.expanduser("~/Desktop/WeChat-Decrypt/decrypted"),
                    help="解密后的明文库目录（默认 ~/Desktop/WeChat-Decrypt/decrypted，即当前项目下最新解析结果）")
    ap.add_argument("--out", default=os.path.expanduser(os.path.join("~", "WeChatData", "analysis.db")))
    ap.add_argument("--self-wxid", default=None,
                    help="本机 wxid（如 wxid_<your_account_id>）。默认从账号目录自动推导")
    args = ap.parse_args()
    build(args.src, args.out, args.self_wxid)


if __name__ == "__main__":
    main()
