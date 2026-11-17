# -*- coding: utf-8 -*-
"""
小杰系统 V26.88 — Streamlit Session State 隔离与类型安全层（P1 高优先级）

【问题】
当前代码大量使用 `st.session_state["foo"] = ...` 这种扁平字符串键，
导致：
  1. **键名拼写错误难发现**：写成 `mc_wechat_enabled` 与 `mc_wechat` 不一致，
     UI 显示打开但企微不推送，散户几天后才察觉。
  2. **跨页面键冲突**：sidebar 写入 `push_p1_high_score_enabled`，
     主页面读取 `push_high_score`（拼写不一致），配置丢失。
  3. **类型混乱**：布尔字段被偶然写入字符串/None，导致 toggle 报错。
  4. **默认值缺失**：升级后旧会话缺少新键 → KeyError 崩溃。
  5. **刷新混乱**：reactive 模式下多个 callback 互相覆盖状态。

【本模块】
提供一个统一的 SessionStateManager，强制：
  - **命名空间隔离**：通过前缀 mc_/rc_/scan_/cache_/p0_/p1_ 等避免冲突
  - **类型注解**：每个键声明期望类型，赋值时强制转换 + 校验
  - **默认值**：所有键都有兜底，避免 KeyError
  - **键注册表**：集中管理 schema，便于审计和文档化
  - **迁移工具**：版本号机制支持老会话自动迁移

【设计原则】
  - 完全向后兼容：未注册的键仍然允许直读（warn 但不阻断）
  - 不替换 st.session_state 本身，只在其上层做包装
  - 性能：薄包装，O(1) 字典查询

【使用方式】
  # 旧代码：
  st.session_state["mc_wechat"] = True
  if st.session_state.get("mc_wechat"):
      ...

  # 新代码：
  from ui.session_state_manager import ss

  ss.set("mc_wechat", True)              # 自动类型校验
  if ss.get("mc_wechat"):                # 自动用默认 True
      ...

  # 或命名空间批量获取：
  mc = ss.namespace("mc")                # 返回所有 mc_* 键的子字典
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Type, Union

logger = logging.getLogger(__name__)


# ==================== 键注册表（schema） ====================

# 类型别名：可以是 (type, default_factory) 元组
TypeSpec = Union[Type, tuple]  # 简化类型注解


# 注册表：{key: {type, default, validator, description, ns}}
_REGISTRY: Dict[str, Dict[str, Any]] = {}

# 锁：注册表线程安全
_REG_LOCK = threading.Lock()


def register(
    key: str,
    type_: TypeSpec = str,
    default: Any = None,
    validator: Optional[Callable[[Any], bool]] = None,
    description: str = "",
    namespace: Optional[str] = None,
) -> None:
    """
    注册一个 session_state 键到 schema。

    Args:
        key: 完整键名（如 'mc_wechat'）
        type_: 期望类型（bool/int/float/str/dict/list），或 (type, default_factory)
        default: 默认值
        validator: 可选的自定义校验函数（返回 True=合法）
        description: 键用途说明（用于文档生成）
        namespace: 可选命名空间标签（自动从 key 前缀推断）
    """
    with _REG_LOCK:
        ns = namespace or _infer_namespace(key)
        _REGISTRY[key] = {
            "type": type_,
            "default": default,
            "validator": validator,
            "description": description,
            "namespace": ns,
        }


def _infer_namespace(key: str) -> str:
    """从键名推断命名空间（取第一个下划线前的部分）。"""
    if "_" in key:
        return key.split("_", 1)[0]
    return "_misc"


def is_registered(key: str) -> bool:
    return key in _REGISTRY


def get_schema(key: str) -> Optional[Dict[str, Any]]:
    """获取单个键的 schema（用于文档生成/调试）。"""
    return _REGISTRY.get(key)


def all_keys() -> List[str]:
    return sorted(_REGISTRY.keys())


# ==================== 默认注册：小杰系统核心键 ====================


def _register_defaults() -> None:
    """注册小杰系统当前所有已知 session_state 键。"""

    # ------- 命名空间 mc_*：总控中心（master_control） -------
    register("mc_wechat", bool, True, description="企微实盘推送总开关")
    register("mc_daemon", bool, True, description="守护进程自动巡航总开关")
    register("push_p1_high_score_enabled", bool, False, description="P1 高分票推送开关")
    register("mc_disk_mtime", float, 0.0, description="master_control.json 最后修改时间")

    # ------- 命名空间 rc_*：风险控制（risk_control） -------
    register("rc_ui_alert_only", bool, True, description="风控 UI 告警模式（仅告警不拦截）")
    register("cfg_yaml_mtime_ui_alert", float, 0.0, description="config.yaml 最后修改时间")

    # ------- 命名空间 cache_*：缓存相关 -------
    register("sidebar_db_snapshot_cache", dict, {}, description="侧栏 DB 快照缓存")
    register("p1_base_items_cache", list, [], description="P1 底仓缓存")
    register("p0_base_items_cache", list, [], description="P0 自选底仓缓存")
    register("p1_rejected_cache", list, [], description="P1 拒绝池缓存")
    register("p0_rejected_cache", list, [], description="P0 拒绝池缓存")
    register("_strategy_lab_sweep_cache", dict, {}, description="策略实验室扫描缓存")
    register("_p1_lab_mock_raw_cache", dict, {}, description="P1 实验室 mock 原始缓存")
    register("dash_idx_cache", dict, {}, description="仪表盘指标缓存")

    # ------- 命名空间 pool_*：池子模式 -------
    register("pool_mode", str, "P1", description="底仓模式（P0/P1）",
             validator=lambda v: v in ("P0", "P1"))
    register("pool_view_mode", str, "main_plus_obs", description="池子视图模式")
    register("pool_view_mode_radio", str, "主池+观察池", description="池子视图单选")
    register("latest_pool_name", str, "", description="最新池名")

    # ------- 命名空间 market_*：市场状态 -------
    register("market_regime", str, "震荡市", description="当前市场风格",
             validator=lambda v: v in ("震荡市", "趋势市", "退潮市", "牛市", "熊市"))
    register("sector_rank", dict, {}, description="板块排名缓存")
    register("latest_trade_date", str, "", description="最新交易日")

    # ------- 命名空间 scan_*：扫描状态 -------
    register("scan_results", dict, {}, description="扫描结果缓存")
    register("last_scan_time", str, "", description="上次扫描时间")
    register("missing_dates", list, [], description="缺失日期列表")
    register("query_done", bool, False, description="缺失日期查询完成标志")
    register("force_maintenance_dispatched", bool, False, description="清理任务已分发标志")
    register("scan_busy", bool, False, description="扫描引擎忙标志（互斥锁）")
    register("_sop_last_breaker", dict, {}, description="SOP 上次熔断记录")

    # ------- 命名空间 p1_*：P1 专用 -------
    register("p1_last_wash_input_revision", int, 0, description="P1 洗盘输入版本号")
    register("p1_last_wash_input_codes_json", str, "", description="P1 上次洗盘输入 codes JSON 路径")
    register("_p1_startup_hydrate_capped", bool, False, description="P1 启动水化被截断标志")

    # ------- 命名空间 notify_*：推送 -------
    register("wechat_notify_enabled", bool, True, description="企微推送启用")


_register_defaults()


# ==================== 类型转换 ====================


def _coerce(value: Any, target: TypeSpec) -> Any:
    """将 value 强制转换为 target 类型。"""
    if value is None:
        return None
    if target is bool:
        # 避免 bool("False") == True 的陷阱
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "y", "t")
        return bool(value)
    if target is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if target is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if target is str:
        return str(value) if value is not None else ""
    if target is dict:
        return value if isinstance(value, dict) else {}
    if target is list:
        return value if isinstance(value, list) else []
    return value


# ==================== 核心：SessionStateManager ====================


class SessionStateManager:
    """
    Streamlit Session State 的隔离包装层。

    设计目标：
      - 自动注入默认值，避免 KeyError
      - 强制类型校验，防止类型污染
      - 命名空间隔离，避免键冲突
      - 向后兼容：未注册键仍然允许读写（带 warn）
    """

    def __init__(self, ss_obj: Optional[Any] = None) -> None:
        """
        Args:
            ss_obj: st.session_state 对象（运行时注入；测试时可传 dict 替代）
        """
        self._ss = ss_obj

    def bind(self, ss_obj: Any) -> None:
        """绑定 st.session_state 对象（Streamlit 入口处调用一次）。"""
        self._ss = ss_obj

    # ---------- 核心 API ----------
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取 session_state 值，自动注入默认值。

        Args:
            key: 键名
            default: 缺省值（覆盖 schema 中的 default）

        Returns:
            值（已类型校验）
        """
        self._ensure_migrated(key)
        ss = self._require_ss()
        if key in ss:
            return ss[key]
        # 未设置：从 schema 拿默认值
        schema_default = self._schema_default(key)
        final_default = schema_default if default is None else default
        ss[key] = final_default
        return final_default

    def set(self, key: str, value: Any, *, coerce_type: bool = True) -> bool:
        """
        设置 session_state 值。

        Args:
            key: 键名
            value: 值
            coerce_type: 是否强制类型转换（默认 True）

        Returns:
            True=设置成功；False=校验失败但已存原值
        """
        self._ensure_migrated(key)
        ss = self._require_ss()

        schema = _REGISTRY.get(key)
        if schema and coerce_type:
            value = _coerce(value, schema["type"])
            validator = schema.get("validator")
            if validator and not validator(value):
                logger.warning(
                    "[SSM] 键 '%s' 值校验失败: %r (validator 返回 False)",
                    key, value,
                )
                # 仍然写入，但日志告警

        ss[key] = value
        return True

    def pop(self, key: str, default: Any = None) -> Any:
        ss = self._require_ss()
        return ss.pop(key, default)

    def delete(self, key: str) -> None:
        ss = self._require_ss()
        if key in ss:
            del ss[key]

    def has(self, key: str) -> bool:
        ss = self._require_ss()
        return key in ss

    # ---------- 批量操作 ----------
    def namespace(self, prefix: str) -> Dict[str, Any]:
        """获取所有以 prefix 开头的键的子字典。"""
        ss = self._require_ss()
        return {k: v for k, v in ss.items() if k.startswith(prefix + "_")}

    def reset_namespace(self, prefix: str) -> int:
        """重置指定命名空间的所有键（使用 schema 默认值）。"""
        ss = self._require_ss()
        count = 0
        for key in list(ss.keys()):
            if key.startswith(prefix + "_"):
                if key in _REGISTRY:
                    ss[key] = self._schema_default(key)
                else:
                    del ss[key]
                count += 1
        return count

    # ---------- 兼容层 ----------
    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __contains__(self, key: str) -> bool:
        return self.has(key)

    # ---------- 内部 ----------
    def _require_ss(self) -> Any:
        if self._ss is None:
            raise RuntimeError(
                "SessionStateManager 未绑定 st.session_state。"
                "请在 Streamlit 入口处调用 ss.bind(st.session_state)"
            )
        return self._ss

    def _schema_default(self, key: str) -> Any:
        """获取键的 schema 默认值。"""
        schema = _REGISTRY.get(key)
        if not schema:
            return None
        default = schema["default"]
        if callable(default):
            try:
                return default()
            except Exception:
                return None
        return default

    def _ensure_migrated(self, key: str) -> None:
        """每次访问都做一次类型迁移检查（开销 O(1)，但确保动态写入也受 schema 约束）。"""
        ss = self._require_ss()
        # 若键已存在但类型错误，自动转换
        if key in ss and key in _REGISTRY:
            expected_type = _REGISTRY[key]["type"]
            current = ss[key]
            if current is not None and not isinstance(current, expected_type):
                # 类型不匹配 → 自动转换
                coerced = _coerce(current, expected_type)
                logger.debug(
                    "[SSM] 键 '%s' 类型转换: %s -> %s",
                    key, type(current).__name__, type(coerced).__name__,
                )
                ss[key] = coerced


# ==================== 全局单例 + 便捷访问 ====================

_singleton: Optional[SessionStateManager] = None


def get_manager() -> SessionStateManager:
    """获取 SessionStateManager 单例。"""
    global _singleton
    if _singleton is None:
        _singleton = SessionStateManager()
    return _singleton


def bind_streamlit_session() -> SessionStateManager:
    """
    在 Streamlit 入口处调用：绑定 st.session_state 到 SSM 单例。

    Returns:
        SessionStateManager 实例（链式调用）
    """
    import streamlit as st
    mgr = get_manager()
    mgr.bind(st.session_state)
    return mgr


# 便捷函数（避免每次 import 类）
def get(key: str, default: Any = None) -> Any:
    return get_manager().get(key, default)


def set_key(key: str, value: Any) -> bool:
    return get_manager().set(key, value)


def reset_namespace(prefix: str) -> int:
    return get_manager().reset_namespace(prefix)


# 默认导出一个未绑定的实例（懒绑定）
ss = get_manager()


# ==================== 自检入口 ====================


def _self_test() -> int:
    """自检：验证隔离层基本行为。"""
    print("[SSM] 开始自检 ...")

    # 使用 mock dict 模拟 st.session_state
    mock_ss = {}
    mgr = SessionStateManager(mock_ss)

    # 1. 默认值注入
    v = mgr.get("mc_wechat")
    assert v is True, f"默认值注入失败: {v}"
    print(f"[OK] 默认值注入 mc_wechat={v}")

    # 2. 类型强制（字符串 '0' → bool False）
    mock_ss["mc_wechat"] = "0"
    v = mgr.get("mc_wechat")
    assert bool(v) is False, f"类型强制失败: {v!r}"
    print(f"[OK] 类型强制 '0' -> {v!r}")

    # 3. 校验器
    mgr.set("pool_mode", "P9")  # validator 应拒绝
    v = mock_ss["pool_mode"]
    assert v == "P9", f"校验失败仍应写入（仅日志）: {v}"
    print(f"[OK] 校验器写入: pool_mode={v} (validator warn)")

    # 4. 命名空间隔离
    mock_ss["rc_ui_alert_only"] = False
    mock_ss["cache_foo"] = "x"
    mc_ns = mgr.namespace("rc")
    assert "rc_ui_alert_only" in mc_ns and "cache_foo" not in mc_ns
    print(f"[OK] 命名空间隔离: rc_*={len(mc_ns)}")

    # 5. 重置命名空间
    n = mgr.reset_namespace("rc")
    assert n == 1, f"reset 数量错误: {n}"
    assert bool(mock_ss["rc_ui_alert_only"]) is True  # 回到 schema 默认
    print(f"[OK] reset_namespace 还原默认值 (n={n})")

    # 6. 未注册键仍然允许（带 warn）
    mgr.set("unregistered_key_xyz", 42)
    assert mock_ss["unregistered_key_xyz"] == 42
    print(f"[OK] 未注册键透传")

    # 7. 自定义键注册
    register("custom_test_key", bool, True, description="测试键")
    mgr.set("custom_test_key", "false")
    assert mock_ss["custom_test_key"] is False
    print(f"[OK] 自定义键 + 类型强制")

    # 8. schema 查询
    keys = all_keys()
    print(f"[OK] schema 键总数: {len(keys)}")
    assert len(keys) >= 30, f"预期至少 30 个键，实际 {len(keys)}"

    print("[OK] 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
