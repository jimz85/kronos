#!/usr/bin/env python3
"""
strategy_version_control.py - Strategy Version Control & One-Click Rollback
============================================================================

策略版本控制系统，防止错误策略导致重大损失。

功能特性：
    - 版本快照：保存策略配置的完整历史版本
    - 一键回滚：快速回滚到任意历史版本
    - 版本对比：对比不同版本的策略参数差异
    - 自动归档：支持自动创建版本快照
    - 回滚验证：回滚前验证版本兼容性

Key Components:
    - StrategyVersion dataclass: 单个策略版本的数据结构
    - StrategyVersionControl class: 版本控制核心类
    - RollbackResult dataclass: 回滚操作结果

Version: 5.1.0
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger('kronos.strategy_vc')


# ═══════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════

class VersionStatus(Enum):
    """版本状态枚举"""
    ACTIVE = "active"           # 当前活跃版本
    ARCHIVED = "archived"       # 已归档版本
    ROLLED_BACK = "rolled_back" # 已回滚版本
    CORRUPTED = "corrupted"     # 损坏版本


class RollbackType(Enum):
    """回滚类型枚举"""
    FULL = "full"               # 完整回滚（恢复所有参数）
    PARTIAL = "partial"         # 部分回滚（只恢复部分参数）
    EMERGENCY = "emergency"     # 紧急回滚（立即恢复到上一个稳定版本）


@dataclass
class StrategyParams:
    """策略参数字段定义"""
    # Alpha Engine 参数
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    rsi_period: int = 14
    
    # Beta Engine 参数  
    trend_sma_short: int = 20
    trend_sma_long: int = 50
    
    # 置信度参数
    min_confidence: float = 60.0
    confidence_threshold: float = 70.0
    
    # 风控参数
    max_position_pct: float = 10.0
    sl_pct: float = 3.0
    tp_pct: float = 6.0
    
    # 扫描参数
    scan_interval_seconds: int = 180
    max_hold_hours: int = 72
    
    # 其他参数
    use_shadow_validation: bool = True
    use_gemma4_validation: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategyParams":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def compute_hash(self) -> str:
        """计算参数字符串的MD5哈希值"""
        param_str = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.md5(param_str.encode()).hexdigest()[:12]


@dataclass
class StrategyVersion:
    """
    策略版本数据结构。
    
    Attributes:
        version_id: 唯一版本标识符 (格式: v{timestamp}-{hash})
        timestamp: 创建时间 (ISO8601格式)
        params: 策略参数字典
        description: 版本描述
        status: 版本状态
        author: 创建者标识
        rollback_from: 从哪个版本回滚来的（如果是回滚操作）
        parent_version: 父版本ID
        metadata: 附加元数据
        performance_snapshot: 性能快照数据
    """
    version_id: str
    timestamp: str
    params: Dict[str, Any]
    description: str = ""
    status: str = VersionStatus.ACTIVE.value
    author: str = "system"
    rollback_from: Optional[str] = None
    parent_version: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    performance_snapshot: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategyVersion":
        return cls(**data)
    
    def is_active(self) -> bool:
        return self.status == VersionStatus.ACTIVE.value
    
    def is_rollable_back(self) -> bool:
        """检查版本是否可以回滚"""
        return self.status in [VersionStatus.ACTIVE.value, VersionStatus.ARCHIVED.value]


@dataclass
class RollbackResult:
    """回滚操作结果"""
    success: bool
    from_version: str
    to_version: str
    rollback_type: str
    timestamp: str
    message: str
    affected_params: List[str] = field(default_factory=list)
    validation_passed: bool = True
    warnings: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# 核心类
# ═══════════════════════════════════════════════════════════════════════════

class StrategyVersionControl:
    """
    策略版本控制器。
    
    管理策略配置的生命周期：创建版本、查询历史、一键回滚。
    
    Usage:
        vc = StrategyVersionControl()
        
        # 创建新版本
        new_ver = vc.create_version(params, description="更新RSI阈值")
        
        # 列出所有版本
        versions = vc.list_versions()
        
        # 一键回滚
        result = vc.rollback_to("v20260427-abc123")
        
        # 获取当前活跃版本
        active = vc.get_active_version()
    """
    
    # 默认版本存储路径
    DEFAULT_VERSION_DIR = Path.home() / "kronos" / "data" / "strategy_versions"
    
    # 最大保留版本数（超过此数量会自动归档最老的版本）
    MAX_KEEP_VERSIONS = 50
    
    # 紧急回滚目标（回滚到此版本）
    EMERGENCY_ROLLBACK_TARGET = "last_stable"
    
    def __init__(
        self,
        version_dir: Optional[Path] = None,
        auto_backup: bool = True,
        max_versions: int = MAX_KEEP_VERSIONS
    ):
        """
        初始化版本控制器。
        
        Args:
            version_dir: 版本文件存储目录，默认 ~/.kronos/data/strategy_versions
            auto_backup: 是否自动备份当前版本再创建新版本
            max_versions: 最大保留版本数
        """
        self.version_dir = version_dir or self.DEFAULT_VERSION_DIR
        self.auto_backup = auto_backup
        self.max_versions = max_versions
        
        # 确保版本目录存在
        self.version_dir.mkdir(parents=True, exist_ok=True)
        
        # 版本索引文件
        self.index_file = self.version_dir / "version_index.json"
        self.active_file = self.version_dir / "active_version.json"
        
        # 加载索引
        self._index: Dict[str, StrategyVersion] = {}
        self._active_version_id: Optional[str] = None
        self._load_index()
        
        logger.info(f"StrategyVersionControl 初始化完成 | 版本目录: {self.version_dir} | "
                   f"当前版本数: {len(self._index)}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # 索引管理
    # ═══════════════════════════════════════════════════════════════════════
    
    def _load_index(self) -> None:
        """从磁盘加载版本索引"""
        if self.index_file.exists():
            try:
                with open(self.index_file, 'r') as f:
                    data = json.load(f)
                    self._index = {
                        k: StrategyVersion.from_dict(v) 
                        for k, v in data.get('versions', {}).items()
                    }
                    self._active_version_id = data.get('active_version_id')
                logger.debug(f"已加载 {len(self._index)} 个策略版本")
            except Exception as e:
                logger.error(f"加载版本索引失败: {e}")
                self._index = {}
                self._active_version_id = None
    
    def _save_index(self) -> None:
        """保存版本索引到磁盘"""
        try:
            data = {
                'versions': {k: v.to_dict() for k, v in self._index.items()},
                'active_version_id': self._active_version_id,
                'updated_at': datetime.now().isoformat()
            }
            
            # 原子写入防止文件损坏
            tmp_file = self.index_file.with_suffix('.tmp')
            with open(tmp_file, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_file, self.index_file)
            
            logger.debug(f"已保存版本索引，共 {len(self._index)} 个版本")
        except Exception as e:
            logger.error(f"保存版本索引失败: {e}")
            raise
    
    def _save_active_version(self, version: StrategyVersion) -> None:
        """保存活跃版本到专用文件"""
        try:
            with open(self.active_file, 'w') as f:
                json.dump(version.to_dict(), f, indent=2)
        except Exception as e:
            logger.error(f"保存活跃版本失败: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # 版本管理
    # ═══════════════════════════════════════════════════════════════════════
    
    def _generate_version_id(self, params: Dict[str, Any]) -> str:
        """生成唯一版本ID"""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        param_hash = hashlib.md5(
            json.dumps(params, sort_keys=True).encode()
        ).hexdigest()[:8]
        return f"v{timestamp}-{param_hash}"
    
    def create_version(
        self,
        params: Dict[str, Any],
        description: str = "",
        author: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        performance_snapshot: Optional[Dict[str, Any]] = None,
        auto_activate: bool = True
    ) -> StrategyVersion:
        """
        创建新策略版本。
        
        Args:
            params: 策略参数字典
            description: 版本描述
            author: 创建者标识
            metadata: 附加元数据
            performance_snapshot: 性能快照
            auto_activate: 是否自动激活新版本
        
        Returns:
            新创建的 StrategyVersion 对象
        """
        # 如果启用自动备份，先备份当前版本
        if self.auto_backup and self._active_version_id:
            current = self._index.get(self._active_version_id)
            if current and current.status == VersionStatus.ACTIVE.value:
                # 创建备份版本
                backup_desc = f"Auto-backup before creating {self._generate_version_id(params)}"
                self._create_backup_version(current, backup_desc)
        
        # 生成新版本ID
        version_id = self._generate_version_id(params)
        
        # 获取父版本
        parent_version = self._active_version_id
        
        # 创建新版本对象
        new_version = StrategyVersion(
            version_id=version_id,
            timestamp=datetime.now().isoformat(),
            params=params,
            description=description,
            status=VersionStatus.ACTIVE.value,
            author=author,
            parent_version=parent_version,
            metadata=metadata or {},
            performance_snapshot=performance_snapshot
        )
        
        # 更新索引：如果有父版本，先将其标记为ARCHIVED
        if parent_version and parent_version in self._index:
            self._index[parent_version].status = VersionStatus.ARCHIVED.value
        
        # 添加新版本到索引
        self._index[version_id] = new_version
        self._active_version_id = version_id
        
        # 保存
        self._save_index()
        self._save_active_version(new_version)
        
        # 自动归档：如果版本数超过限制，归档最老的非活跃版本
        self._auto_archive()
        
        logger.info(f"创建新策略版本: {version_id} | 描述: {description}")
        
        return new_version
    
    def _create_backup_version(
        self, 
        source_version: StrategyVersion, 
        description: str
    ) -> StrategyVersion:
        """创建备份版本"""
        backup_id = f"backup_{source_version.version_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        backup_version = StrategyVersion(
            version_id=backup_id,
            timestamp=datetime.now().isoformat(),
            params=source_version.params.copy(),
            description=description,
            status=VersionStatus.ARCHIVED.value,
            author="system",
            parent_version=source_version.version_id,
            metadata=source_version.metadata.copy()
        )
        
        self._index[backup_id] = backup_version
        
        logger.info(f"创建备份版本: {backup_id}")
        return backup_version
    
    def _auto_archive(self) -> None:
        """自动归档：保留最近N个版本"""
        if len(self._index) <= self.max_versions:
            return
        
        # 按时间排序，排除活跃版本
        non_active = [
            (vid, v) for vid, v in self._index.items() 
            if v.status != VersionStatus.ACTIVE.value
        ]
        non_active.sort(key=lambda x: x[1].timestamp, reverse=True)
        
        # 归档超出限制的版本
        to_archive = non_active[self.max_versions:]
        for vid, _ in to_archive:
            if vid in self._index:
                self._index[vid].status = VersionStatus.ARCHIVED.value
                logger.debug(f"自动归档版本: {vid}")
        
        if to_archive:
            self._save_index()
    
    # ═══════════════════════════════════════════════════════════════════════
    # 查询操作
    # ═══════════════════════════════════════════════════════════════════════
    
    def list_versions(
        self,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> List[StrategyVersion]:
        """
        列出策略版本。
        
        Args:
            status: 按状态过滤 (active/archived/rolled_back)
            limit: 返回数量限制
            offset: 偏移量
        
        Returns:
            StrategyVersion 列表
        """
        versions = list(self._index.values())
        
        # 过滤
        if status:
            versions = [v for v in versions if v.status == status]
        
        # 按时间倒序
        versions.sort(key=lambda v: v.timestamp, reverse=True)
        
        # 分页
        return versions[offset:offset + limit]
    
    def get_version(self, version_id: str) -> Optional[StrategyVersion]:
        """
        获取指定版本。
        
        Args:
            version_id: 版本ID
        
        Returns:
            StrategyVersion 或 None
        """
        return self._index.get(version_id)
    
    def get_active_version(self) -> Optional[StrategyVersion]:
        """
        获取当前活跃版本。
        
        Returns:
            StrategyVersion 或 None
        """
        if not self._active_version_id:
            return None
        return self._index.get(self._active_version_id)
    
    def get_version_history(
        self, 
        version_id: str, 
        max_depth: int = 10
    ) -> List[StrategyVersion]:
        """
        获取版本的历史追溯链。
        
        Args:
            version_id: 起始版本ID
            max_depth: 最大追溯深度
        
        Returns:
            版本链列表
        """
        history = []
        current_id = version_id
        depth = 0
        
        while current_id and depth < max_depth:
            version = self._index.get(current_id)
            if not version:
                break
            history.append(version)
            current_id = version.parent_version
            depth += 1
        
        return history
    
    def compare_versions(
        self,
        version_id_a: str,
        version_id_b: str
    ) -> Optional[Dict[str, Any]]:
        """
        对比两个版本的参数差异。
        
        Returns:
            差异字典，包含:
            - added: B有A没有的参数
            - removed: A有B没有的参数
            - changed: 双方都有但值不同的参数
        """
        ver_a = self._index.get(version_id_a)
        ver_b = self._index.get(version_id_b)
        
        if not ver_a or not ver_b:
            return None
        
        params_a = ver_a.params
        params_b = ver_b.params
        
        keys_a = set(params_a.keys())
        keys_b = set(params_b.keys())
        
        return {
            "version_a": version_id_a,
            "version_b": version_id_b,
            "added": list(keys_b - keys_a),
            "removed": list(keys_a - keys_b),
            "changed": {
                k: {"a": params_a[k], "b": params_b[k]}
                for k in keys_a & keys_b
                if params_a[k] != params_b[k]
            },
            "unchanged": [
                k for k in keys_a & keys_b
                if params_a[k] == params_b[k]
            ]
        }
    
    # ═══════════════════════════════════════════════════════════════════════
    # 回滚操作
    # ═══════════════════════════════════════════════════════════════════════
    
    def rollback_to(
        self,
        target_version_id: str,
        rollback_type: RollbackType = RollbackType.FULL,
        force: bool = False,
        validation_callback: Optional[callable] = None
    ) -> RollbackResult:
        """
        一键回滚到指定版本。
        
        Args:
            target_version_id: 目标版本ID
            rollback_type: 回滚类型
            force: 强制回滚（跳过验证）
            validation_callback: 自定义验证回调函数
        
        Returns:
            RollbackResult 回滚结果
        """
        timestamp = datetime.now().isoformat()
        
        # 获取目标版本
        target_version = self._index.get(target_version_id)
        if not target_version:
            return RollbackResult(
                success=False,
                from_version=self._active_version_id or "none",
                to_version=target_version_id,
                rollback_type=rollback_type.value,
                timestamp=timestamp,
                message=f"目标版本不存在: {target_version_id}"
            )
        
        # 获取当前版本
        current_version = self.get_active_version()
        from_version_id = current_version.version_id if current_version else "none"
        
        # 如果是紧急回滚
        if rollback_type == RollbackType.EMERGENCY:
            stable_version = self._find_last_stable_version()
            if stable_version:
                target_version = stable_version
                target_version_id = target_version.version_id
        
        # 执行验证
        if not force and validation_callback:
            validation_passed, warnings = validation_callback(target_version)
            if not validation_passed:
                return RollbackResult(
                    success=False,
                    from_version=from_version_id,
                    to_version=target_version_id,
                    rollback_type=rollback_type.value,
                    timestamp=timestamp,
                    message="验证失败，回滚已取消",
                    validation_passed=False,
                    warnings=warnings
                )
        
        # 记录回滚前的版本（用于审计）
        rollback_from_id = from_version_id
        
        # 更新当前活跃版本状态
        if current_version:
            current_version.status = VersionStatus.ROLLED_BACK.value
            current_version.metadata['rolled_back_to'] = target_version_id
            current_version.metadata['rolled_back_at'] = timestamp
        
        # 创建回滚后的新版本（保留历史追踪）
        new_active_version = StrategyVersion(
            version_id=self._generate_version_id(target_version.params),
            timestamp=timestamp,
            params=target_version.params.copy(),
            description=f"Rollback to {target_version_id}",
            status=VersionStatus.ACTIVE.value,
            author="system",
            rollback_from=rollback_from_id,
            parent_version=target_version_id,
            metadata={
                'rollback_type': rollback_type.value,
                'original_rollback_from': from_version_id
            }
        )
        
        # 更新索引
        self._index[new_active_version.version_id] = new_active_version
        self._active_version_id = new_active_version.version_id
        
        # 保存
        self._save_index()
        self._save_active_version(new_active_version)
        
        # 计算受影响的参数
        affected_params = []
        if current_version:
            diff = self.compare_versions(current_version.version_id, new_active_version.version_id)
            if diff:
                affected_params = diff.get('changed', {}).keys()
                affected_params = list(affected_params) if affected_params else []
        
        logger.warning(f"策略回滚完成: {from_version_id} -> {target_version_id} "
                      f"(新版本: {new_active_version.version_id})")
        
        return RollbackResult(
            success=True,
            from_version=from_version_id,
            to_version=target_version_id,
            rollback_type=rollback_type.value,
            timestamp=timestamp,
            message=f"成功回滚到版本 {target_version_id}",
            affected_params=affected_params,
            validation_passed=True
        )
    
    def _find_last_stable_version(self) -> Optional[StrategyVersion]:
        """查找上一个稳定版本（已验证的ARCHIVED版本）"""
        archived = [
            v for v in self._index.values()
            if v.status == VersionStatus.ARCHIVED.value
            and v.performance_snapshot is not None
        ]
        if not archived:
            # 没有任何性能快照，找最近归档的
            archived = [
                v for v in self._index.values()
                if v.status == VersionStatus.ARCHIVED.value
            ]
        
        if not archived:
            return None
        
        # 按时间倒序，取最新的
        archived.sort(key=lambda v: v.timestamp, reverse=True)
        return archived[0]
    
    def emergency_rollback(self) -> RollbackResult:
        """
        紧急回滚：立即回滚到上一个稳定版本。
        
        用于当当前策略造成异常损失时快速止损。
        
        Returns:
            RollbackResult 回滚结果
        """
        stable = self._find_last_stable_version()
        
        if not stable:
            return RollbackResult(
                success=False,
                from_version=self._active_version_id or "none",
                to_version="none",
                rollback_type=RollbackType.EMERGENCY.value,
                timestamp=datetime.now().isoformat(),
                message="紧急回滚失败：未找到稳定版本"
            )
        
        logger.critical(f"执行紧急回滚: {self._active_version_id} -> {stable.version_id}")
        
        return self.rollback_to(
            target_version_id=stable.version_id,
            rollback_type=RollbackType.EMERGENCY,
            force=True
        )
    
    # ═══════════════════════════════════════════════════════════════════════
    # 辅助功能
    # ═══════════════════════════════════════════════════════════════════════
    
    def export_version(
        self, 
        version_id: str, 
        output_path: Optional[Path] = None
    ) -> Optional[Path]:
        """
        导出版本到JSON文件。
        
        Args:
            version_id: 版本ID
            output_path: 输出路径，默认导出到版本目录
        
        Returns:
            输出文件路径
        """
        version = self._index.get(version_id)
        if not version:
            return None
        
        if output_path is None:
            output_path = self.version_dir / f"{version_id}.json"
        
        with open(output_path, 'w') as f:
            json.dump(version.to_dict(), f, indent=2)
        
        logger.info(f"导出版本 {version_id} 到 {output_path}")
        return output_path
    
    def import_version(self, file_path: Path) -> Optional[StrategyVersion]:
        """
        从JSON文件导入版本。
        
        Args:
            file_path: 版本文件路径
        
        Returns:
            导入的 StrategyVersion 或 None
        """
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
            
            version = StrategyVersion.from_dict(data)
            
            # 检查是否已存在
            if version.version_id in self._index:
                logger.warning(f"版本已存在: {version.version_id}")
                return self._index[version.version_id]
            
            self._index[version.version_id] = version
            self._save_index()
            
            logger.info(f"导入版本 {version.version_id}")
            return version
            
        except Exception as e:
            logger.error(f"导入版本失败: {e}")
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """获取版本统计信息"""
        total = len(self._index)
        by_status = {}
        
        for v in self._index.values():
            by_status[v.status] = by_status.get(v.status, 0) + 1
        
        return {
            "total_versions": total,
            "by_status": by_status,
            "active_version": self._active_version_id,
            "version_dir": str(self.version_dir),
            "max_keep_versions": self.max_versions
        }


# ═══════════════════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════════════════

# 全局版本控制器实例（延迟初始化）
_vc_instance: Optional[StrategyVersionControl] = None


def get_version_control() -> StrategyVersionControl:
    """获取全局版本控制器实例"""
    global _vc_instance
    if _vc_instance is None:
        _vc_instance = StrategyVersionControl()
    return _vc_instance


def quick_rollback(target_version: str = "last_stable") -> RollbackResult:
    """
    快速回滚函数。
    
    Args:
        target_version: 目标版本ID，或 "last_stable"
    
    Returns:
        RollbackResult
    """
    vc = get_version_control()
    
    if target_version == "last_stable":
        stable = vc._find_last_stable_version()
        if not stable:
            return RollbackResult(
                success=False,
                from_version="current",
                to_version="none",
                rollback_type=RollbackType.EMERGENCY.value,
                timestamp=datetime.now().isoformat(),
                message="未找到稳定版本"
            )
        target_version = stable.version_id
    
    return vc.rollback_to(target_version)


def create_checkpoint(
    params: Dict[str, Any],
    description: str = "",
    metadata: Optional[Dict[str, Any]] = None
) -> StrategyVersion:
    """
    创建策略检查点（便捷函数）。
    
    相当于 create_version 的别名，更强调"检查点"语义。
    """
    vc = get_version_control()
    return vc.create_version(params, description, metadata=metadata)


# ═══════════════════════════════════════════════════════════════════════════
# 入口（测试用）
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("Kronos v5.1 Strategy Version Control")
    print("=" * 60)
    
    vc = StrategyVersionControl()
    
    # 创建测试参数
    test_params = StrategyParams(
        rsi_oversold=30.0,
        rsi_overbought=70.0,
        min_confidence=65.0
    ).to_dict()
    
    # 创建新版本
    v1 = vc.create_version(
        test_params,
        description="Test version 1"
    )
    print(f"创建版本: {v1.version_id}")
    
    # 修改参数创建新版本
    test_params["rsi_oversold"] = 35.0
    test_params["description"] = "Test version 2"
    
    v2 = vc.create_version(
        test_params,
        description="Test version 2 - adjusted RSI"
    )
    print(f"创建版本: {v2.version_id}")
    
    # 列出所有版本
    print("\n所有版本:")
    for ver in vc.list_versions():
        print(f"  {ver.version_id} | {ver.status} | {ver.description}")
    
    # 对比版本
    print("\n版本对比:")
    diff = vc.compare_versions(v1.version_id, v2.version_id)
    if diff:
        print(f"  Changed params: {list(diff.get('changed', {}).keys())}")
    
    # 回滚
    print("\n执行回滚到第一个版本:")
    result = vc.rollback_to(v1.version_id)
    print(f"  结果: {result.message}")
    
    # 紧急回滚测试
    print("\n紧急回滚测试:")
    result = vc.emergency_rollback()
    print(f"  结果: {result.message}")
    
    # 统计
    print("\n版本统计:")
    stats = vc.get_stats()
    print(f"  总版本数: {stats['total_versions']}")
    print(f"  活跃版本: {stats['active_version']}")
