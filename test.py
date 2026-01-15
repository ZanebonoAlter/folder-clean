"""测试脚本"""
from models import ScanResult
from database import Database
from scanner import FolderScanner, ResultFormatter
from datetime import datetime
import os


def test_models():
    """测试数据模型"""
    print("测试数据模型...")
    result = ScanResult(
        path="C:\\Test",
        size_bytes=2 * 1024 * 1024 * 1024,  # 2GB
        scan_time=datetime.now()
    )
    print(f"  路径: {result.path}")
    print(f"  大小: {result.size_gb:.2f} GB")
    print(f"  是大文件夹: {result.is_large}")
    print("  [OK] 数据模型测试通过")


def test_database():
    """测试数据库"""
    print("\n测试数据库...")
    db = Database(":memory:")  # 使用内存数据库

    # 测试保存和读取
    scan_time = datetime.now()
    db.save_scan("C:\\Test", 1024 * 1024, scan_time)

    result = db.get_latest_scan("C:\\Test")
    assert result is not None
    assert result["size_bytes"] == 1024 * 1024
    print("  [OK] 数据库测试通过")


def test_scanner():
    """测试扫描器"""
    print("\n测试扫描器...")
    db = Database(":memory:")
    scanner = FolderScanner(db)

    # 测试格式化
    size_str = scanner.format_size(1024 * 1024 * 1024)
    assert "GB" in size_str
    print(f"  格式化结果: {size_str}")
    print("  [OK] 扫描器测试通过")


def test_formatter():
    """测试格式化器"""
    print("\n测试格式化器...")
    result = ScanResult(
        path="C:\\Test\\Folder",
        size_bytes=2 * 1024 * 1024 * 1024,
        scan_time=datetime.now(),
        depth=0
    )

    tree = ResultFormatter.to_tree(result)
    assert "C:\\Test\\Folder" in tree
    assert "2.00 GB" in tree
    print(f"  Tree structure contains: {len(tree)} characters")
    print("  [OK] 格式化器测试通过")


if __name__ == "__main__":
    print("=" * 50)
    print("运行测试")
    print("=" * 50)

    test_models()
    test_database()
    test_scanner()
    test_formatter()

    print("\n" + "=" * 50)
    print("[OK] 所有测试通过!")
    print("=" * 50)
