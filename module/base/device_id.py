"""设备 ID 管理模块。

基于不可逆的硬件指纹哈希生成匿名的设备唯一标识符，用于本地配置区分与遥测统计。
"""
import json
import threading
from pathlib import Path
from typing import Optional

from module.logger import logger

# 部署和运行时复用同一算法；运行时仍独立负责旧身份迁移和定时刷新。
from deploy.device_id import (
    generate_device_id, save_device_id, set_runtime_device_id,
)


_device_id: Optional[str] = None
_identity_lock = threading.RLock()
_old_device_id: Optional[str] = None # 用于记录迁移前的旧 ID
_refresh_timer: Optional[threading.Timer] = None
_REFRESH_INTERVAL = 300


def get_device_id() -> str:
    """获取当前设备 ID。

    Returns:
        str: 32 位设备标识符。
    """
    global _device_id
    with _identity_lock:
        if _device_id is None:
            _device_id = _init_device_id()
        return _device_id


def get_old_device_id() -> Optional[str]:
    """获取迁移前的旧 ID。

    Returns:
        str | None: 迁移前的旧设备 ID，若无变更则为 None。
    """
    global _old_device_id
    return _old_device_id


def _init_device_id(root=None) -> str:
    """初始化设备 ID 并检查旧 ID 迁移。

    Returns:
        str: 初始化后的设备 ID。
    """
    global _old_device_id
    device_id = generate_device_id()
    
    project_root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    device_id_file = project_root / 'log' / 'device_id.json'
    
    # 自动识别变更并暂存旧 ID 用于数据库热迁移
    if device_id_file.exists():
        try:
            with device_id_file.open('r', encoding='utf-8') as f:
                old_data = json.load(f)
                stored_id = old_data.get('device_id')
                if stored_id and stored_id != device_id:
                    _old_device_id = stored_id
                    logger.info(f'检测到设备ID变更并用于迁移，旧ID: {stored_id[:8]}，新ID: {device_id[:8]}')
        except Exception:
            pass

    set_runtime_device_id(device_id)

    # 立即覆写新 ID
    _overwrite_device_id(device_id, device_id_file)

    _start_refresh_timer(device_id, device_id_file)
    
    return device_id


def _overwrite_device_id(device_id: str, file_path: Path):
    """将设备 ID 写入本地日志文件。

    Args:
        device_id (str): 32 位设备 ID。
        file_path (Path): 目标 json 文件路径。
    """
    try:
        save_device_id(device_id, file_path)
    except Exception as e:
        logger.warning(f'[设备-ID] 覆盖设备ID文件失败: {e}')


def _refresh_callback(device_id: str, file_path: Path):
    """定时器回调：定期刷新设备 ID 文件。

    Args:
        device_id (str): 设备 ID。
        file_path (Path): 目标 json 文件路径。
    """
    _overwrite_device_id(device_id, file_path)
    _start_refresh_timer(device_id, file_path)


def _start_refresh_timer(device_id: str, file_path: Path):
    """启动设备 ID 文件的定期刷新后台定时器。

    Args:
        device_id (str): 设备 ID。
        file_path (Path): 目标 json 文件路径。
    """
    global _refresh_timer
    if _refresh_timer is not None:
        _refresh_timer.cancel()
    _refresh_timer = threading.Timer(_REFRESH_INTERVAL, _refresh_callback, args=(device_id, file_path))
    _refresh_timer.daemon = True
    _refresh_timer.start()
