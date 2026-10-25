"""部署阶段可用的匿名设备身份；不导入日志、配置或游戏模块。"""

import hashlib
import json
import platform
import re
import subprocess
import threading
import time
from pathlib import Path

from deploy.atomic import atomic_write

_lock = threading.RLock()
_runtime_id: str | None = None
_bootstrap_ids: dict[Path, str] = {}


def valid_device_id(value) -> bool:
    """设备身份沿用现有的 32 位十六进制算法。"""
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def _wmic_query(wmic_class: str, field: str) -> str:
    try:
        result = subprocess.run(
            ['wmic', wmic_class, 'get', field], capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == 'Windows' else 0,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return lines[1] if len(lines) >= 2 else ''
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return ''


def collect_hardware_fingerprint() -> str:
    """保持运行时已有的字段、顺序和 MAC 排除规则，避免改变设备身份。"""
    parts = []
    if platform.system() == 'Windows':
        for name, field in (('baseboard', 'serialnumber'), ('cpu', 'processorid'),
                            ('bios', 'serialnumber'), ('diskdrive', 'serialnumber')):
            value = _wmic_query(name, field)
            if value and value.lower() not in ('to be filled by o.e.m.', 'default string', 'none', ''):
                parts.append(f'{name}.{field}={value}')
    else:
        for filename in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
            try:
                value = Path(filename).read_text(encoding='utf-8').strip()
                if value:
                    parts.append(f'machine-id={value}')
                    break
            except (OSError, UnicodeError):
                continue
        if platform.system() == 'Darwin':
            try:
                result = subprocess.run(['system_profiler', 'SPHardwareDataType'],
                                        capture_output=True, text=True, timeout=10)
                for line in result.stdout.splitlines():
                    if 'Hardware UUID' in line:
                        parts.append(f'hw-uuid={line.split(":")[-1].strip()}')
                        break
            except (OSError, UnicodeError, subprocess.SubprocessError):
                pass
    parts.append(f'platform={platform.node()}-{platform.machine()}')
    return '|'.join(parts)


def generate_device_id() -> str:
    return hashlib.sha256(collect_hardware_fingerprint().encode('utf-8')).hexdigest()[:32]


def save_device_id(value: str, path: Path) -> None:
    """原子写入，避免请求线程读取到刷新中的半份 JSON。"""
    if not valid_device_id(value):
        raise ValueError('设备 ID 必须是 32 位小写十六进制字符串')
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'device_id': value, '_generated_by': 'hardware_fingerprint_v2_no_mac',
            '_last_refresh': time.strftime('%Y-%m-%d %H:%M:%S'),
            '_warning': 'This file is auto-generated and overwritten every 5 minutes.'}
    atomic_write(str(path), json.dumps(data, ensure_ascii=False, indent=2))


def set_runtime_device_id(value: str) -> None:
    """运行时完成迁移检测后发布身份，后续请求立即使用新值。"""
    if not valid_device_id(value):
        raise ValueError('运行时设备 ID 无效')
    global _runtime_id
    with _lock:
        _runtime_id = value


def request_device_id(root: Path | None = None) -> str:
    """优先使用运行时或已有身份；首次部署初始化但不覆盖旧迁移资料。"""
    with _lock:
        if root is None and _runtime_id is not None:
            return _runtime_id
        root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        path = root / 'log' / 'device_id.json'
        try:
            value = json.loads(path.read_text(encoding='utf-8')).get('device_id')
            if valid_device_id(value):
                return value
        except (OSError, ValueError, AttributeError):
            pass
        root = root.resolve()
        value = _bootstrap_ids.get(root)
        if value is None:
            value = generate_device_id()
        if not valid_device_id(value):
            raise ValueError('无法生成合法设备 ID，取消请求')
        _bootstrap_ids[root] = value
        # 文件存在时可能保存旧算法身份；留给运行时判断迁移，不在部署阶段覆盖。
        if not path.exists():
            save_device_id(value, path)
        return value
