"""文件夹扫描模块"""
import os
from datetime import datetime
from typing import List, Callable, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from models import ScanResult
from database import Database


# 常量定义
GB_THRESHOLD_BYTES = 1 * 1024 * 1024 * 1024  # 1GB阈值
MAX_WORKERS = 8  # 最大线程数


class FolderScanner:
    """扫描文件夹并计算大小，支持递归下钻和多线程"""

    def __init__(self, db: Database, progress_callback: Optional[Callable] = None, result_callback: Optional[Callable] = None, exclude_paths: Optional[List[str]] = None):
        self.db = db
        self.progress_callback = progress_callback  # 进度回调(status, current, total)
        self.result_callback = result_callback      # 结果回调(ScanResult)
        self._scan_count = 0
        self._total_large_folders = 0
        self._scanned_results = []  # 存储已扫描的结果
        self._lock = threading.Lock()  # 线程锁，保护共享状态
        self._session_id = None  # 当前扫描会话ID
        self._root_path = None  # 根路径
        self._max_depth = None  # 最大深度
        # 处理排除路径：标准化路径，支持大小写不敏感匹配
        self.exclude_paths = []
        if exclude_paths:
            for exclude_path in exclude_paths:
                exclude_path = exclude_path.strip()
                if exclude_path:
                    # 标准化路径（统一使用小写，统一路径分隔符）
                    normalized = os.path.normpath(exclude_path.lower())
                    self.exclude_paths.append(normalized)

    def is_path_excluded(self, path: str) -> bool:
        """检查路径是否应该被排除"""
        if not self.exclude_paths:
            return False
        
        # 标准化当前路径
        normalized_path = os.path.normpath(path.lower())
        
        # 检查是否匹配任何排除路径（支持前缀匹配，即子路径也会被排除）
        for exclude_path in self.exclude_paths:
            # 完全匹配
            if normalized_path == exclude_path:
                return True
            # 前缀匹配（当前路径是排除路径的子路径）
            if normalized_path.startswith(exclude_path + os.sep) or normalized_path.startswith(exclude_path + '/'):
                return True
        
        return False

    def get_folder_size(self, path: str) -> int:
        """递归计算文件夹总大小（字节），自动排除指定路径"""
        # 如果当前路径本身被排除，直接返回0
        if self.is_path_excluded(path):
            return 0
        
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                # 检查当前目录是否应该被排除
                if self.is_path_excluded(dirpath):
                    # 从dirnames中移除所有子目录，这样os.walk就不会遍历它们
                    dirnames[:] = []
                    continue
                
                # 过滤掉被排除的子目录（修改dirnames列表会影响os.walk的遍历）
                dirnames[:] = [d for d in dirnames if not self.is_path_excluded(os.path.join(dirpath, d))]
                
                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    try:
                        total_size += os.path.getsize(file_path)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass
        return total_size

    def get_immediate_subfolders(self, path: str) -> List[str]:
        """获取直接子文件夹（排除指定路径）"""
        try:
            subfolders = []
            for item in os.listdir(path):
                item_path = os.path.join(path, item)
                if os.path.isdir(item_path):
                    # 检查是否应该被排除
                    if not self.is_path_excluded(item_path):
                        subfolders.append(item_path)
            return sorted(subfolders)
        except (OSError, PermissionError):
            return []

    def scan_path_recursive(
        self,
        path: str,
        depth: int = 0,
        max_depth: int = 5,
        save: bool = True,
        parent_path: str = None,
        is_parallel: bool = False
    ) -> ScanResult:
        """递归扫描路径，对大于1GB的文件夹进行下钻"""
        if not os.path.exists(path):
            raise ValueError(f"路径不存在: {path}")
        
        # 检查路径是否应该被排除（根目录不检查，因为用户明确指定了要扫描的路径）
        if depth > 0 and self.is_path_excluded(path):
            # 返回一个空结果，表示该路径被排除
            return ScanResult(
                path=path,
                size_bytes=0,
                scan_time=datetime.now(),
                depth=depth
            )

        # 如果是根目录（depth=0），创建新的扫描会话
        if depth == 0:
            self._session_id = self.db.create_scan_session(path, max_depth)
            self._root_path = path
            self._max_depth = max_depth
            self._scan_count = 0
            self._total_large_folders = 0
            self._scanned_results = []

        # 线程安全的计数器更新
        with self._lock:
            self._scan_count += 1
            current_count = self._scan_count

        # 对于根目录（depth=0），如果子文件夹很多，跳过完整扫描，直接进入子文件夹扫描
        # 这样可以避免扫描整个 C: 盘时卡住
        subfolders = self.get_immediate_subfolders(path)
        skip_root_full_scan = (depth == 0 and len(subfolders) > 5)
        
        if skip_root_full_scan:
            # 根目录跳过完整扫描，大小初始为0，后续通过子文件夹累加
            size = 0
            scan_time = datetime.now()
            result = ScanResult(
                path=path,
                size_bytes=0,  # 初始为0，后续累加
                scan_time=scan_time,
                depth=depth
            )
            
            # 报告进度
            if self.progress_callback:
                self.progress_callback(f"正在扫描根目录: {path} (跳过完整扫描，直接扫描子文件夹)", current_count, depth)
        else:
            # 正常扫描：先计算当前文件夹大小
            size = self.get_folder_size(path)
            scan_time = datetime.now()
            result = ScanResult(
                path=path,
                size_bytes=size,
                scan_time=scan_time,
                depth=depth
            )

        # 只保存1GB以上的文件夹到数据库
        if save and size > GB_THRESHOLD_BYTES and self._session_id:
            self.db.save_scan(self._session_id, path, size, scan_time, depth, parent_path)

        # 将结果添加到已扫描列表
        with self._lock:
            self._scanned_results.append(result)

        # 调用结果回调，实时传递扫描结果（这会更新树形结构和扫描摘要）
        if self.result_callback:
            self.result_callback(result)

        # 报告进度（只更新进度条，不影响其他输出）
        if self.progress_callback and not skip_root_full_scan:
            status = f"正在扫描: {path}"
            if depth > 0:
                status = f"[深度{depth}] " + status
            self.progress_callback(status, current_count, depth)

        # 对于根目录且跳过完整扫描的情况，直接进入子文件夹扫描
        if skip_root_full_scan:
            # 第一层子文件夹使用多线程扫描
            if len(subfolders) > 1 and not is_parallel:
                self._scan_children_parallel(result, subfolders, depth, max_depth, save, update_parent_size=True)
            else:
                # 串行扫描
                for subfolder in subfolders:
                    try:
                        child_result = self.scan_path_recursive(
                            subfolder,
                            depth=depth + 1,
                            max_depth=max_depth,
                            save=save,
                            parent_path=path,
                            is_parallel=is_parallel
                        )
                        result.children.append(child_result)
                        # 累加子文件夹大小到根目录
                        with self._lock:
                            result.size_bytes += child_result.size_bytes
                    except Exception as e:
                        print(f"扫描子文件夹失败 {subfolder}: {e}")
                        continue
            
            # 更新根目录大小到数据库（在所有子文件夹扫描完成后，只保存大于1GB的）
            if save and self._session_id and result.size_bytes > GB_THRESHOLD_BYTES:
                self.db.save_scan(self._session_id, path, result.size_bytes, scan_time, depth, parent_path)
        # 如果大于1GB且未达到最大深度，继续下钻
        elif result.is_large and depth < max_depth:
            with self._lock:
                self._total_large_folders += 1

            # 第一层子文件夹使用多线程扫描
            if depth == 0 and len(subfolders) > 1 and not is_parallel:
                self._scan_children_parallel(result, subfolders, depth, max_depth, save)
            else:
                # 其他情况使用串行扫描
                for subfolder in subfolders:
                    try:
                        child_result = self.scan_path_recursive(
                            subfolder,
                            depth=depth + 1,
                            max_depth=max_depth,
                            save=save,
                            parent_path=path,
                            is_parallel=is_parallel
                        )
                        result.children.append(child_result)
                    except Exception as e:
                        print(f"扫描子文件夹失败 {subfolder}: {e}")
                        continue

        # 如果是根目录，更新扫描会话统计信息并完成会话
        if depth == 0 and self._session_id:
            self.db.update_scan_session(
                self._session_id,
                total_folders=self._scan_count,
                large_folders_count=self._total_large_folders,
                total_size_bytes=result.size_bytes
            )
            self.db.finish_scan_session(self._session_id)

        return result

    def _scan_children_parallel(self, parent_result: ScanResult, subfolders: List[str],
                                depth: int, max_depth: int, save: bool, update_parent_size: bool = False):
        """使用线程池并发扫描子文件夹"""
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有扫描任务
            future_to_folder = {
                executor.submit(
                    self.scan_path_recursive,
                    subfolder,
                    depth + 1,
                    max_depth,
                    save,
                    parent_result.path,
                    is_parallel=True  # 标记为并行模式，避免嵌套线程池
                ): subfolder
                for subfolder in subfolders
            }

            # 收集结果
            for future in as_completed(future_to_folder):
                subfolder = future_to_folder[future]
                try:
                    child_result = future.result()
                    with self._lock:
                        parent_result.children.append(child_result)
                        # 如果需要更新父目录大小（用于跳过根目录完整扫描的情况）
                        if update_parent_size:
                            parent_result.size_bytes += child_result.size_bytes
                except Exception as e:
                    print(f"扫描子文件夹失败 {subfolder}: {e}")

    @staticmethod
    def format_size(size_bytes: int) -> str:
        """格式化大小显示"""
        mb = size_bytes / (1024 * 1024)
        gb = size_bytes / (1024 * 1024 * 1024)
        if gb >= 1:
            return f"{gb:.2f} GB"
        else:
            return f"{mb:.2f} MB"


class ResultFormatter:
    """结果格式化工具"""

    @staticmethod
    def to_tree(result: ScanResult) -> str:
        """将扫描结果转换为树形文本"""
        lines = []
        indent = "  " * result.depth

        size_info = f"{result.size_gb:.2f} GB" if result.size_gb >= 1 else f"{result.size_mb:.2f} MB"
        large_mark = " ⚠️ 大" if result.is_large else ""

        lines.append(f"{indent}📁 {os.path.basename(result.path) or result.path}")
        lines.append(f"{indent}   完整路径: {result.path}")
        lines.append(f"{indent}   大小: {size_info}{large_mark}")

        if result.children:
            lines.append(f"{indent}   子文件夹 ({len(result.children)}个):")
            for child in result.children:
                lines.append(ResultFormatter.to_tree(child))

        return "\n".join(lines)

    @staticmethod
    def to_stack_trace(result: ScanResult, only_large: bool = True) -> str:
        """将扫描结果转换为Java堆栈风格的展示（从最深层向上展示）"""
        lines = []
        large_folders = []
        
        def collect_large_folders(node: ScanResult, path_stack: List[ScanResult]):
            """递归收集大文件夹及其路径堆栈"""
            current_stack = path_stack + [node]
            
            # 如果是大文件夹，添加到结果中
            if node.is_large:
                large_folders.append((node, current_stack))
            
            # 递归处理子文件夹
            for child in node.children:
                if not only_large or child.is_large or any(c.is_large for c in child.children):
                    collect_large_folders(child, current_stack)
        
        collect_large_folders(result, [])
        
        if not large_folders:
            return "未发现大文件夹（>= 1GB）"
        
        # 按大小排序，最大的在前
        large_folders.sort(key=lambda x: x[0].size_bytes, reverse=True)
        
        # 生成堆栈展示
        for folder, path_stack in large_folders:
            # 从最深层（大文件夹本身）开始，向上展示到根目录
            stack_lines = []
            for i, item in enumerate(reversed(path_stack)):
                indent = "  " * i
                size_info = f"{item.size_gb:.2f} GB" if item.size_gb >= 1 else f"{item.size_mb:.2f} MB"
                if i == 0:
                    # 最深层（大文件夹本身）
                    folder_name = os.path.basename(item.path) or item.path
                    stack_lines.append(f"Large folder: {folder_name} ({size_info})")
                    stack_lines.append(f"  at {item.path}")
                else:
                    # 父文件夹
                    stack_lines.append(f"{indent}at {item.path} ({size_info})")
            lines.extend(stack_lines)
            lines.append("")  # 空行分隔
        
        return "\n".join(lines)

    @staticmethod
    def to_simple_tree(result: ScanResult) -> str:
        """将扫描结果转换为简化树形文本（单行显示）"""
        indent = "  " * result.depth
        size_info = f"{result.size_gb:.2f} GB" if result.size_gb >= 1 else f"{result.size_mb:.2f} MB"
        mark = " [大]" if result.is_large else ""
        return f"{indent}{'└─' if result.depth > 0 else ''} {os.path.basename(result.path) or result.path} - {size_info}{mark}"

    @staticmethod
    def to_dataframe(result: ScanResult, parent_df=None):
        """将扫描结果转换为DataFrame"""
        try:
            import pandas as pd
        except ImportError:
            return None

        data = [result.to_dict()]

        if parent_df is None:
            df = pd.DataFrame(data)
        else:
            df = pd.concat([parent_df, pd.DataFrame(data)], ignore_index=True)

        for child in result.children:
            df = ResultFormatter.to_dataframe(child, df)

        # 按深度降序，然后按大小降序（数值排序）
        if not df.empty and '深度' in df.columns and '字节数' in df.columns:
            df = df.sort_values(by=['深度', '字节数'], ascending=[False, False])

        return df

    @staticmethod
    def get_summary(result: ScanResult, max_depth: int) -> str:
        """生成扫描摘要"""
        return f"""
✅ 扫描完成！

路径: {result.path}
总大小: {FolderScanner.format_size(result.size_bytes)}
大文件夹数量: 统计中...
扫描的文件夹总数: 统计中...

大文件夹定义: >= {GB_THRESHOLD_BYTES / (1024**3):.0f} GB
最大扫描深度: {max_depth}
        """.strip()
