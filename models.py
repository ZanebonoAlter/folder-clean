"""数据模型定义"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class ScanResult:
    """表示一次扫描的结果"""
    path: str
    size_bytes: int
    scan_time: datetime
    depth: int = 0
    children: List['ScanResult'] = field(default_factory=list)

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 * 1024 * 1024)

    @property
    def is_large(self, threshold_gb: float = 1.0) -> bool:
        """判断是否为大文件夹"""
        return self.size_gb >= threshold_gb

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "路径": self.path,
            "大小(GB)": f"{self.size_gb:.2f}",
            "大小(MB)": f"{self.size_mb:.2f}",
            "字节数": self.size_bytes,
            "深度": self.depth,
            "扫描时间": self.scan_time.strftime("%Y-%m-%d %H:%M:%S"),
            "是否大文件夹": "是" if self.is_large else "否"
        }
