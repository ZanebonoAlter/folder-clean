"""主入口文件"""
import argparse
from ui import create_ui, init_system


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="文件夹磁盘占用分析工具")
    parser.add_argument("--web", action="store_true", help="启动Web界面（默认）")
    parser.add_argument("--share", action="store_true", help="创建公开分享链接")
    args = parser.parse_args()

    # 默认启动Web界面
    init_system()
    app = create_ui()
    app.launch(share=args.share)


if __name__ == "__main__":
    main()
