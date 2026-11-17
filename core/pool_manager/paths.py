# -*- coding: utf-8 -*-
"""
core/pool_manager/paths.py
原 core/pool_manager.py L54-L150

【职责】
- 全局物理底座寻址（get_project_root / PROJECT_ROOT）
- 容错导入 runtime_data_paths 路径工具（path_blacklist_json / path_p1_gene_json 等）
- 回落实现：若 runtime_data_paths 不可用，使用 PROJECT_ROOT/数据

【依赖】
- os
- core.runtime_data_paths（容错导入）

【跨包调用方】
- core.pool_manager.thresholds（黑名单读取）
- core.pool_manager.pool_builder（P1 缓存落盘）
- core.pool_manager.cache_sovereignty（缓存文件路径）
"""
import logging
import os

logger = logging.getLogger(__name__)


def get_project_root() -> str:
    """向上 4 层查找 config.yaml 所在目录；失败回退到 __file__ 父目录。"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        if os.path.exists(os.path.join(current_dir, "config.yaml")):
            return current_dir
        current_dir = os.path.dirname(current_dir)
    return os.path.dirname(os.path.abspath(__file__))


PROJECT_ROOT = get_project_root()

try:
    from core.runtime_data_paths import (
        ensure_runtime_data_layout,
        path_blacklist_json,
        path_p1_gene_json,
        path_sector_rank_history_json,
        path_strategic_mapping_debug_json,
        path_wash_metrics_json,
        path_p1_last_wash_input_codes_json,
    )
except ImportError:
    def ensure_runtime_data_layout():
        os.makedirs(os.path.join(PROJECT_ROOT, "data"), exist_ok=True)

    def path_blacklist_json():
        return os.path.join(PROJECT_ROOT, "data", "blacklist.json")

    def path_sector_rank_history_json():
        return os.path.join(PROJECT_ROOT, "data", "sector_rank_history.json")

    def path_strategic_mapping_debug_json(yyyymmdd: str):
        day = str(yyyymmdd or "").strip()
        if len(day) != 8 or (not day.isdigit()):
            day = "latest"
        return os.path.join(PROJECT_ROOT, "data", f"strategic_mapping_debug_{day}.json")

    def path_wash_metrics_json():
        return os.path.join(PROJECT_ROOT, "data", "wash_metrics_history.json")

    def path_p1_gene_json(yyyymmdd: str):
        return os.path.join(PROJECT_ROOT, "data", f"p1_gene_{yyyymmdd}.json")

    def path_p1_last_wash_input_codes_json():
        return os.path.join(PROJECT_ROOT, "data", "p1_last_wash_input_codes.json")


__all__ = [
    "get_project_root",
    "PROJECT_ROOT",
    "ensure_runtime_data_layout",
    "path_blacklist_json",
    "path_p1_candidate_codes" if False else "path_p1_gene_json",  # 兼容旧名
    "path_sector_rank_history_json",
    "path_strategic_mapping_debug_json",
    "path_wash_metrics_json",
    "path_p1_last_wash_input_codes_json",
]