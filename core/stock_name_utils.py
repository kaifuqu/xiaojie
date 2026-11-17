# -*- coding: utf-8 -*-
"""证券简称展示清洗：去掉行情源偶发行首或中间用于标记的 + / ＋。"""
from __future__ import annotations

import re
from typing import Any


def normalize_stock_display_name(name: Any) -> str:
    s = str(name or "")
    s = re.sub(r"^[\s\+＋]+", "", s).strip()
    s = re.sub(r"\s*\+\s*", " ", s)
    s = re.sub(r"\s*＋\s*", " ", s)
    s = re.sub(r" {2,}", " ", s).strip()
    return s
