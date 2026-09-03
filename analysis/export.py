# -*- coding: utf-8 -*-
"""导出 CSV。"""
from __future__ import annotations

import pandas as pd


def to_csv(df: pd.DataFrame, path: str, **kwargs) -> str:
    """将 DataFrame 导出为 CSV，返回写入路径；空 DataFrame 时也写入（仅表头）。"""
    df = df.copy() if df is not None else pd.DataFrame()
    df.to_csv(path, index=False, encoding="utf-8-sig", **kwargs)
    return path
