# -*- coding: utf-8 -*-
"""读取脱敏 SQLite -> pandas DataFrame。"""
from __future__ import annotations

import os
import sqlite3

import pandas as pd

TABLES = ("sessions", "contacts", "senders", "messages", "meta")


def _read_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    try:
        return pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
    except Exception:
        return pd.DataFrame()


def load_all(db_path: str) -> dict[str, pd.DataFrame]:
    """读取整个脱敏库，返回 {表名: DataFrame}；缺表返回空 DataFrame，不抛异常。"""
    result: dict[str, pd.DataFrame] = {}
    if not os.path.exists(db_path):
        return {t: pd.DataFrame() for t in TABLES}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return {t: pd.DataFrame() for t in TABLES}
    with conn:
        for t in TABLES:
            result[t] = _read_table(conn, t)
    return result


def load_meta(db_path: str) -> dict[str, str]:
    """读取 meta 表为 dict（key->value，仅保留两列）。"""
    df = load_all(db_path).get("meta", pd.DataFrame())
    out: dict[str, str] = {}
    if not df.empty and {"key", "value"}.issubset(df.columns):
        for _, row in df.iterrows():
            out[str(row["key"])] = str(row["value"])
    return out
