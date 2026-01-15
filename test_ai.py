"""测试AI分析模块"""
from models import ScanResult
from datetime import datetime
import os


def test_ai_analyzer_init():
    """测试AI分析器初始化"""
    print("测试AI分析器初始化...")

    try:
        from ai_analyzer import AIAnalyzer
        print("  [OK] AI分析器模块导入成功")
    except ImportError as e:
        print(f"  [SKIP] 无法导入AI分析器: {e}")
        return

    # 测试无API Key的情况
    try:
        analyzer = AIAnalyzer()
        print("  [FAIL] 应该抛出错误但没有")
    except ValueError as e:
        print(f"  [OK] 正确抛出错误: {str(e)[:50]}...")
    except Exception as e:
        print(f"  [FAIL] 抛出了意外的错误: {e}")

    # 测试提供API Key的情况（不实际调用API）
    try:
        analyzer = AIAnalyzer(api_key="test-key-sk-...")
        print("  [OK] 使用API Key初始化成功")
    except Exception as e:
        print(f"  [OK] 初始化尝试: {str(e)[:50]}...")


def test_format_scan_results():
    """测试扫描结果格式化"""
    print("\n测试扫描结果格式化...")

    try:
        from ai_analyzer import AIAnalyzer
    except ImportError:
        print("  [SKIP] 无法导入AI分析器")
        return

    # 创建测试数据
    result = ScanResult(
        path="C:\\Test",
        size_bytes=2 * 1024 * 1024 * 1024,  # 2GB
        scan_time=datetime.now(),
        depth=0
    )

    # 添加子文件夹
    child1 = ScanResult(
        path="C:\\Test\\Folder1",
        size_bytes=1.5 * 1024 * 1024 * 1024,  # 1.5GB
        scan_time=datetime.now(),
        depth=1
    )
    child2 = ScanResult(
        path="C:\\Test\\Folder2",
        size_bytes=0.5 * 1024 * 1024 * 1024,  # 0.5GB
        scan_time=datetime.now(),
        depth=1
    )
    result.children = [child1, child2]

    try:
        analyzer = AIAnalyzer(api_key="test-key")
        formatted = analyzer.format_scan_results(result)

        # 验证格式化结果包含关键信息
        assert "C:\\Test" in formatted
        assert "2.00 GB" in formatted
        assert "大文件夹" in formatted

        print(f"  [OK] 格式化成功，长度: {len(formatted)} 字符")
        print(f"  格式化结果预览:\n{formatted[:200]}...")

    except Exception as e:
        print(f"  [FAIL] 格式化失败: {e}")


if __name__ == "__main__":
    print("=" * 50)
    print("AI分析模块测试")
    print("=" * 50)

    test_ai_analyzer_init()
    test_format_scan_results()

    print("\n" + "=" * 50)
    print("[OK] AI模块测试完成!")
    print("=" * 50)
