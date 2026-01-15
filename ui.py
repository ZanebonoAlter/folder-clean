"""Gradio Web界面模块"""
import os
import gradio as gr
import queue
import threading
from models import ScanResult
from database import Database
from scanner import FolderScanner, ResultFormatter, GB_THRESHOLD_BYTES
from ai_analyzer import AIAnalyzer


# 全局状态
scanner = None
db = None
last_scan_result = None
current_scanned_results = []  # 存储当前扫描的所有结果
ai_analyzer = None  # AI分析器实例


def init_system():
    """初始化系统"""
    global db, scanner
    if db is None:
        db = Database()
    if scanner is None:
        scanner = FolderScanner(db)
    return "系统已初始化"


def scan_folder(path: str, max_depth: int, exclude_paths: str, progress=gr.Progress()):
    """扫描文件夹 - 支持动态展示进度"""
    global last_scan_result, current_scanned_results

    if not path:
        yield "请输入路径", "", None
        return

    path = path.strip()
    if not os.path.exists(path):
        yield f"路径不存在: {path}", "", None
        return

    # 解析排除路径（支持多行，每行一个路径）
    exclude_list = []
    if exclude_paths:
        for line in exclude_paths.strip().split('\n'):
            line = line.strip()
            if line:
                exclude_list.append(line)

    # 重置扫描结果
    current_scanned_results = []

    # 创建带回调的扫描器（传入排除路径）
    global scanner
    scanner = FolderScanner(db, exclude_paths=exclude_list if exclude_list else None)
    scanner._scan_count = 0
    scanner._total_large_folders = 0

    # 先获取第一层子文件夹总数（用于进度计算）
    first_level_subfolders = scanner.get_immediate_subfolders(path)
    total_first_level = len(first_level_subfolders)
    if total_first_level == 0:
        total_first_level = 1  # 如果没有子文件夹，至少为1（根目录）

    # 进度回调函数 - 只计算第一层进度
    def on_progress(status, count, depth):
        if depth == 0:
            # 根目录
            progress(0.1, desc=status)
        elif depth == 1:
            # 第一层子文件夹，计算进度
            progress_val = min(0.95, count / total_first_level)
            progress(progress_val, desc=f"[{count}/{total_first_level}] {status}")
        else:
            # 更深层级，保持当前进度
            progress(0.95, desc=status)

    # 使用队列收集子线程的结果（线程安全）
    result_queue = queue.Queue()
    
    # 结果回调函数 - 将结果放入队列（线程安全）
    def on_result(result: ScanResult):
        # 将结果放入队列，主线程会处理
        result_queue.put(result)

    # 设置回调
    scanner.progress_callback = on_progress
    scanner.result_callback = on_result

    # 初始状态
    yield "🚀 准备扫描...\n\n请稍候", "", None

    # 启动扫描线程
    scan_thread = None
    scan_exception = None
    
    def run_scan():
        """在单独线程中运行扫描"""
        nonlocal scan_exception
        try:
            result = scanner.scan_path_recursive(
                path,
                depth=0,
                max_depth=max_depth,
                save=True
            )
            # 扫描完成，放入结束标记
            result_queue.put(None)  # None 表示扫描完成
            return result
        except Exception as e:
            scan_exception = e
            result_queue.put(None)  # 即使出错也放入结束标记
            raise
    
    # 启动扫描线程
    scan_thread = threading.Thread(target=run_scan, daemon=True)
    scan_thread.start()
    
    # 主线程：处理队列中的结果并更新界面
    try:
        while True:
            try:
                # 从队列获取结果（超时0.1秒，避免阻塞太久）
                result = result_queue.get(timeout=0.1)
                
                # None 表示扫描完成
                if result is None:
                    break
                
                # 添加结果到列表
                current_scanned_results.append(result)
                
                # 生成当前进度信息（用于扫描摘要）
                depth_info = "根目录" if result.depth == 0 else f"深度{result.depth}"
                progress_text = f"""
🔄 正在扫描...

当前: {result.path}
层级: {depth_info}
已扫描: {scanner._scan_count} 个文件夹
发现大文件夹: {scanner._total_large_folders} 个

当前文件夹:
  大小: {scanner.format_size(result.size_bytes)}
  是大文件夹: {'是' if result.is_large else '否'}
                """.strip()

                # 生成树形展示（增量添加，不清空）
                tree_lines = []
                for r in current_scanned_results:
                    tree_lines.append(ResultFormatter.to_simple_tree(r))

                tree_text = "\n".join(tree_lines)

                # 从sqlite数据库生成数据表格（只显示当前会话的数据）
                df = None
                try:
                    import pandas as pd
                    # 从数据库读取当前扫描会话的记录
                    if scanner._session_id:
                        db_scans = db.get_scans_by_session(scanner._session_id)

                        if db_scans:
                            df_data = []
                            for scan in db_scans:
                                size_bytes = scan['size_bytes']
                                size_gb = size_bytes / (1024**3)
                                size_mb = size_bytes / (1024**2)

                                df_data.append({
                                    "路径": scan['path'],
                                    "大小(GB)": f"{size_gb:.2f}",
                                    "大小(MB)": f"{size_mb:.2f}",
                                    "深度": scan['depth'],
                                    "是大文件夹": "是" if size_bytes >= GB_THRESHOLD_BYTES else "否",
                                    "父路径": scan['parent_path'] or "",
                                    "_size_bytes": size_bytes  # 用于排序的临时列
                                })

                            df = pd.DataFrame(df_data)
                            # 按深度降序，然后按大小降序（数值排序）
                            if not df.empty:
                                df = df.sort_values(by=['深度', '_size_bytes'], ascending=[False, False])
                                # 删除临时排序列
                                df = df.drop(columns=['_size_bytes'])
                except ImportError:
                    pass
                except Exception as e:
                    print(f"从数据库读取扫描记录失败: {e}")

                # 更新界面（增量更新）
                yield progress_text, tree_text, df
                
            except queue.Empty:
                # 队列为空，继续等待
                continue
        
        # 等待扫描线程完成
        scan_thread.join()
        
        # 检查是否有异常
        if scan_exception:
            raise scan_exception
        
        # 获取最终结果（从扫描器的结果中获取）
        result = scanner._scanned_results[0] if scanner._scanned_results else None
        if result and result.depth == 0:
            # 找到根目录结果
            for r in scanner._scanned_results:
                if r.depth == 0:
                    result = r
                    break
        
        if not result:
            # 如果没找到，重新扫描获取结果（这种情况不应该发生）
            result = scanner.scan_path_recursive(path, depth=0, max_depth=max_depth, save=False)
        
        last_scan_result = result

        # 进度完成
        progress(1.0, desc="扫描完成")

        # 生成最终结果（使用堆栈风格展示）
        stack_text = ResultFormatter.to_stack_trace(result, only_large=True)
        df = ResultFormatter.to_dataframe(result)

        summary = f"""
✅ 扫描完成！

路径: {result.path}
总大小: {scanner.format_size(result.size_bytes)}
大文件夹数量: {scanner._total_large_folders} 个
扫描的文件夹总数: {scanner._scan_count} 个

大文件夹定义: >= {GB_THRESHOLD_BYTES / (1024**3):.0f} GB
最大扫描深度: {max_depth}

📊 使用了 {scanner._total_large_folders} 个线程并发扫描
        """.strip()

        yield summary, stack_text, df

    except Exception as e:
        import traceback
        yield f"❌ 扫描失败: {str(e)}\n\n{traceback.format_exc()}", "", None


def analyze_with_ai(config_id: int, session_id: int, quick_mode: bool):
    """使用AI分析扫描结果"""
    global last_scan_result, ai_analyzer, db

    # 检查是否选择了配置
    if not config_id:
        return "❌ 请先选择一个AI配置", ""

    # 从数据库加载配置
    try:
        config = db.get_ai_config(config_id=config_id)
        if not config:
            return "❌ 未找到选中的AI配置", ""
    except Exception as e:
        return f"❌ 加载AI配置失败: {str(e)}", ""

    # 如果提供了session_id，从历史记录加载
    if session_id:
        try:
            result = db.rebuild_scan_result_from_session(session_id)
            if not result:
                return "❌ 未找到该扫描会话的数据", ""
        except Exception as e:
            return f"❌ 加载历史扫描失败: {str(e)}", ""
    else:
        # 使用最后一次扫描的结果
        result = last_scan_result
        if result is None:
            return "❌ 请先扫描文件夹后再使用AI分析功能，或选择历史扫描结果", ""

    try:
        # 创建AI分析器
        ai_analyzer = AIAnalyzer(
            api_key=config['api_key'],
            base_url=config['base_url'],
            model=config['model']
        )

        # 更新配置的最后使用时间
        db.update_ai_config_last_used(config['id'])

        # 生成分析进度文本
        status = f"""
🤖 正在分析...

API配置:
- 配置名称: {config['name']}
- Base URL: {config['base_url'] or '默认'}
- Model: {config['model']}

分析模式: {'快速分析 (前10个大文件夹)' if quick_mode else '完整分析'}
语言: {'中文' if config['language'] == 'zh' else '英文'}

请稍候，AI正在分析扫描结果...
        """.strip()

        yield status, ""

        # 执行分析
        if quick_mode:
            analysis_result = ai_analyzer.quick_analyze(result)
        else:
            analysis_result = ai_analyzer.analyze(result, language=config['language'])

        # 显示结果
        result_text = f"""
🤖 AI分析完成！

配置:
- 配置名称: {config['name']}
- API: {config['base_url'] or 'OpenAI'}
- Model: {config['model']}
- 模式: {'快速' if quick_mode else '完整'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{analysis_result}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        """.strip()

        yield result_text, analysis_result

    except Exception as e:
        import traceback
        error_msg = f"❌ AI分析失败\n\n错误信息: {str(e)}\n\n请检查：\n1. API Key是否正确\n2. Base URL是否可访问\n3. 网络连接是否正常\n4. 模型名称是否正确\n\n详细信息:\n{traceback.format_exc()}"
        yield error_msg, ""


def get_scan_sessions_history():
    """获取扫描会话历史"""
    global db
    if db is None:
        return "数据库未初始化", []

    sessions = db.get_all_scan_sessions(limit=50)
    if not sessions:
        return "暂无扫描历史", []

    lines = ["## 扫描会话历史\n"]
    choices = []
    for session in sessions:
        duration_str = f"{session['duration']:.2f}秒" if session['duration'] else "进行中"
        session_label = f"会话 #{session['id']} - {session['start_time'].strftime('%Y-%m-%d %H:%M:%S')} - {session['root_path']}"
        choices.append((session_label, session['id']))
        lines.append(f"""
### 会话 #{session['id']}
- **时间**: {session['start_time'].strftime('%Y-%m-%d %H:%M:%S')}
- **路径**: {session['root_path']}
- **持续**: {duration_str}
- **深度**: {session['max_depth']}
- **文件夹数**: {session['total_folders']}
- **大文件夹**: {session['large_folders_count']}
- **总大小**: {FolderScanner.format_size(session['total_size_bytes'])}
        """.strip())

    return "\n\n".join(lines), choices


def view_history_scan_detail(session_id: int):
    """查看历史扫描详情"""
    global db
    if db is None:
        return "数据库未初始化", "", None
    
    if not session_id:
        return "请选择扫描会话", "", None
    
    try:
        # 从数据库重建扫描结果
        result = db.rebuild_scan_result_from_session(session_id)
        if not result:
            return "未找到该扫描会话的数据", "", None
        
        # 获取会话信息
        session = db.get_scan_session(session_id)
        if not session:
            return "未找到会话信息", "", None
        
        # 生成摘要
        summary = f"""
✅ 历史扫描结果

会话ID: #{session_id}
路径: {result.path}
总大小: {scanner.format_size(result.size_bytes)}
大文件夹数量: {session['large_folders_count']} 个
扫描的文件夹总数: {session['total_folders']} 个
扫描时间: {session['start_time'].strftime('%Y-%m-%d %H:%M:%S')}
持续时长: {session['duration']:.2f}秒（如果已完成）

大文件夹定义: >= {GB_THRESHOLD_BYTES / (1024**3):.0f} GB
最大扫描深度: {session['max_depth']}
        """.strip()
        
        # 生成堆栈风格的展示（类似Java堆栈）
        stack_text = ResultFormatter.to_stack_trace(result, only_large=True)
        
        # 生成数据表格
        df = ResultFormatter.to_dataframe(result)
        
        return summary, stack_text, df
    
    except Exception as e:
        import traceback
        error_msg = f"❌ 加载历史扫描失败: {str(e)}\n\n{traceback.format_exc()}"
        return error_msg, "", None


def get_ai_configs_list():
    """获取AI配置列表"""
    global db
    if db is None:
        return "数据库未初始化", []

    configs = db.get_all_ai_configs()
    if not configs:
        return "暂无保存的AI配置", []

    # 生成文本列表
    lines = ["## 已保存的AI配置\n"]
    for config in configs:
        default_mark = " ⭐ 默认" if config['is_default'] else ""
        lines.append(f"""
### {config['name']}{default_mark}
- **ID**: {config['id']}
- **模型**: {config['model']}
- **语言**: {'中文' if config['language'] == 'zh' else '英文'}
- **创建时间**: {config['created_at'].strftime('%Y-%m-%d %H:%M:%S')}
- **最后使用**: {config['last_used_at'].strftime('%Y-%m-%d %H:%M:%S') if config['last_used_at'] else '从未'}
        """.strip())

    # 生成下拉选项 - 返回(id, label)格式，label用于显示，id用于实际值
    choices = [(f"{c['name']} ({c['model']})", c['id']) for c in configs]

    return "\n\n".join(lines), choices


def load_ai_config(config_name: str):
    """加载AI配置"""
    global db
    if not config_name:
        return "", "", "", "", ""

    config = db.get_ai_config(name=config_name)
    if not config:
        return "", "", "", "", ""

    # 更新最后使用时间
    db.update_ai_config_last_used(config['id'])

    return (
        config['api_key'],
        config['base_url'] or "",
        config['model'],
        config['language'],
        ""  # 返回空字符串作为状态消息
    )


def save_ai_config_handler(config_name: str, api_key: str, base_url: str, model: str, language: str, config_id: int = None):
    """保存AI配置的处理函数（支持创建和编辑）"""
    global db
    if not config_name:
        return "❌ 请输入配置名称", None

    if not api_key:
        return "❌ 请输入API Key", None

    try:
        if config_id:
            # 编辑现有配置 - 先检查名称是否冲突（如果改名）
            existing_config = db.get_ai_config(config_id=config_id)
            if not existing_config:
                return "❌ 未找到要编辑的配置", None
            
            # 如果名称改变，检查新名称是否已存在
            if existing_config['name'] != config_name:
                name_conflict = db.get_ai_config(name=config_name)
                if name_conflict and name_conflict['id'] != config_id:
                    return f"❌ 配置名称 '{config_name}' 已存在", None
            
            # 更新配置（通过删除旧配置并创建新配置，因为save_ai_config使用INSERT OR REPLACE）
            # 但我们需要保持ID，所以先删除再插入
            db.delete_ai_config(config_id)
            new_id = db.save_ai_config(
                name=config_name,
                api_key=api_key,
                base_url=base_url if base_url else None,
                model=model,
                language=language
            )
            return f"✅ 配置 '{config_name}' 更新成功！", None
        else:
            # 创建新配置
            db.save_ai_config(
                name=config_name,
                api_key=api_key,
                base_url=base_url if base_url else None,
                model=model,
                language=language
            )
            return f"✅ 配置 '{config_name}' 保存成功！", None
    except Exception as e:
        import traceback
        return f"❌ 保存失败: {str(e)}\n\n{traceback.format_exc()}", None


def clear_config_form():
    """清空配置表单"""
    return "", "", "", "zh", None


def load_config_to_form(config_id: int):
    """加载配置到表单（用于编辑）"""
    global db
    if not config_id:
        return "", "", "", "zh", None, ""

    try:
        config = db.get_ai_config(config_id=config_id)
        if not config:
            return "", "", "", "zh", None, "❌ 未找到配置"
        
        return (
            config['name'],
            config['api_key'],
            config['base_url'] or "",
            config['model'],
            config['language'],
            config['id'],
            f"✅ 已加载配置: {config['name']}"
        )
    except Exception as e:
        return "", "", "", "zh", None, f"❌ 加载失败: {str(e)}"


def load_config_to_form_by_name(config_name: str):
    """通过名称加载配置到表单（用于编辑）"""
    global db
    if not config_name:
        return "", "", "", "zh", None, ""

    try:
        config = db.get_ai_config(name=config_name)
        if not config:
            return "", "", "", "zh", None, "❌ 未找到配置"
        
        return (
            config['name'],
            config['api_key'],
            config['base_url'] or "",
            config['model'],
            config['language'],
            config['id'],
            f"✅ 已加载配置: {config['name']}"
        )
    except Exception as e:
        return "", "", "", "zh", None, f"❌ 加载失败: {str(e)}"


def create_ui():
    """创建Gradio界面"""
    with gr.Blocks(title="文件夹磁盘占用分析工具", theme=gr.themes.Soft()) as app:
        gr.Markdown("# 📁 文件夹磁盘占用分析工具")
        gr.Markdown("扫描文件夹并递归分析大于1GB的子文件夹 - **多线程并发加速** - **AI智能分析**")

        with gr.Tab("🔍 扫描分析"):
            with gr.Row():
                path_input = gr.Textbox(
                    label="扫描路径",
                    placeholder="例如: C:\\ 或者 C:\\Users",
                    value="C:\\"
                )
                max_depth_input = gr.Slider(
                    minimum=1,
                    maximum=10,
                    value=3,
                    step=1,
                    label="最大递归深度"
                )

            with gr.Row():
                exclude_paths_input = gr.Textbox(
                    label="排除路径（可选）",
                    placeholder="每行一个路径，例如：\nC:\\Windows\nC:\\Program Files\\Temp\n支持子路径自动排除",
                    lines=5,
                    info="输入要排除的路径，每行一个。子路径也会被自动排除。"
                )

            with gr.Row():
                scan_btn = gr.Button("🚀 开始扫描", variant="primary", size="lg")

            with gr.Row():
                summary_output = gr.Textbox(label="扫描摘要 (实时更新)", lines=10)

            with gr.Row():
                tree_output = gr.Textbox(label="文件夹详情 (堆栈展示)", lines=25)

            with gr.Row():
                dataframe_output = gr.Dataframe(label="详细数据表 (实时更新)")

        with gr.Tab("🤖 AI分析"):
            gr.Markdown("### 使用AI分析扫描结果")
            gr.Markdown("选择AI配置和扫描结果，然后开始分析")

            # 选择AI配置
            with gr.Row():
                gr.Markdown("### ⚙️ 选择AI配置")
            with gr.Row():
                ai_config_dropdown = gr.Dropdown(
                    label="选择AI配置",
                    choices=[],
                    value=None,
                    scale=2,
                    interactive=True,
                    info="请先在'AI配置管理'标签页创建并保存配置"
                )
                refresh_ai_config_for_analysis_btn = gr.Button("🔄 刷新配置列表", scale=1)

            # 选择扫描结果
            with gr.Row():
                gr.Markdown("### 📜 选择扫描结果")
            with gr.Row():
                history_scan_dropdown = gr.Dropdown(
                    label="选择扫描结果（可选）",
                    choices=[],
                    value=None,
                    scale=2,
                    interactive=True,
                    info="选择历史扫描结果进行分析，留空则使用当前扫描结果"
                )
                refresh_history_scans_btn = gr.Button("🔄 刷新历史", scale=1)

            # 分析选项
            with gr.Row():
                quick_mode_input = gr.Checkbox(
                    label="快速模式 (仅分析前10个大文件夹)",
                    value=True,
                    scale=1
                )

            with gr.Row():
                analyze_btn = gr.Button("🤖 开始AI分析", variant="primary", size="lg")

            with gr.Row():
                ai_status_output = gr.Textbox(label="分析状态", lines=8)

            with gr.Row():
                ai_result_output = gr.Markdown(label="AI分析结果")

        with gr.Tab("📜 扫描历史"):
            gr.Markdown("### 扫描会话历史记录")
            with gr.Row():
                refresh_history_btn = gr.Button("🔄 刷新历史", size="sm")
            with gr.Row():
                scan_history_output = gr.Markdown(label="扫描会话历史")
            
            gr.Markdown("### 查看历史扫描详情")
            with gr.Row():
                history_session_dropdown = gr.Dropdown(
                    label="选择扫描会话",
                    choices=[],
                    value=None,
                    scale=2,
                    interactive=True
                )
                view_history_btn = gr.Button("📊 查看详情", scale=1, variant="primary")
            
            with gr.Row():
                history_summary_output = gr.Textbox(label="扫描摘要", lines=10)
            
            with gr.Row():
                history_tree_output = gr.Textbox(label="文件夹详情 (堆栈展示)", lines=25)
            
            with gr.Row():
                history_dataframe_output = gr.Dataframe(label="详细数据表")

        with gr.Tab("⚙️ AI配置管理"):
            gr.Markdown("### 管理AI配置")
            gr.Markdown("创建、编辑和管理AI分析配置（支持OpenAI、Azure OpenAI、DeepSeek、通义千问等）")

            # 配置列表
            with gr.Row():
                gr.Markdown("### 📋 已保存的配置")
            with gr.Row():
                refresh_configs_btn = gr.Button("🔄 刷新配置列表", size="sm")
            with gr.Row():
                ai_configs_output = gr.Markdown(label="AI配置列表")
            
            # 选择配置进行编辑
            with gr.Row():
                edit_config_dropdown = gr.Dropdown(
                    label="选择配置进行编辑（可选）",
                    choices=[],
                    value=None,
                    scale=2,
                    interactive=True,
                    info="选择配置后会自动加载到下方表单"
                )

            # 创建/编辑配置
            with gr.Row():
                gr.Markdown("### ➕ 创建新配置 / 编辑配置")
            with gr.Row():
                config_name_input = gr.Textbox(
                    label="配置名称",
                    placeholder="例如: 我的OpenAI配置",
                    scale=2
                )
                config_id_hidden = gr.Number(value=None, visible=False)  # 隐藏字段，用于编辑时存储ID
            with gr.Row():
                api_key_input = gr.Textbox(
                    label="API Key",
                    placeholder="sk-...",
                    type="password",
                    scale=3
                )
                base_url_input = gr.Textbox(
                    label="Base URL (可选)",
                    placeholder="https://api.openai.com/v1 或其他兼容API地址",
                    scale=2
                )
            with gr.Row():
                model_input = gr.Textbox(
                    label="模型名称",
                    value="gpt-4o-mini",
                    placeholder="gpt-4o-mini, gpt-4, deepseek-chat等",
                    scale=2
                )
                language_input = gr.Radio(
                    choices=[("中文", "zh"), ("英文", "en")],
                    value="zh",
                    label="响应语言",
                    scale=1
                )
            with gr.Row():
                save_config_btn = gr.Button("💾 保存配置", variant="primary", scale=1)
                clear_config_btn = gr.Button("🗑️ 清空表单", scale=1)
            with gr.Row():
                config_status_output = gr.Textbox(label="操作状态", lines=2)

        with gr.Tab("ℹ️ 使用说明"):
            gr.Markdown("""
            ## 功能说明

            ### 🔍 扫描分析
            - **扫描路径**: 输入要扫描的根路径（如 C:\\ 或特定文件夹）
            - **最大递归深度**: 控制下钻扫描的层级（推荐3-5层）
            - **排除路径**: 输入要排除的路径，每行一个（可选）
              - 支持完全匹配和前缀匹配（子路径也会被排除）
              - 例如：排除 `C:\\Windows` 会同时排除 `C:\\Windows\\System32` 等所有子路径
              - 常用于排除系统文件夹、临时文件夹等不需要扫描的路径
            - 点击"开始扫描"后，程序会：
              1. 扫描指定路径的大小（自动跳过排除的路径）
              2. 自动识别大于1GB的文件夹
              3. **使用多线程并发**扫描大文件夹的子目录
              4. **实时增量展示**已扫描的文件夹（不清空重绘）
              5. 保存扫描历史到数据库

            ### 🤖 AI分析
            - **选择AI配置**: 从已保存的配置中选择一个（需先在"AI配置管理"标签页创建配置）
            - **选择扫描结果**: 选择历史扫描结果进行分析，或留空使用当前扫描结果
            - **快速模式**: 只分析前10个大文件夹，节省token
            - AI会提供：
              - 🎯 高优先级清理项
              - ⚠️ 需要小心的项
              - 🔒 不建议删除的项
              - 💡 优化建议

            ### 📜 扫描历史
            - 每次扫描都会创建一个独立的扫描会话
            - 记录扫描时间、路径、持续时长、文件夹数量等信息
            - 可以查看历史扫描记录，对比不同时间的扫描结果

            ### ⚙️ AI配置管理
            - **创建配置**: 输入配置名称、API Key、Base URL、模型名称和语言，然后保存
            - **编辑配置**: 从配置列表中选择配置，会自动加载到表单，修改后保存即可
            - **支持多个配置**: 可以保存多个不同的API配置，方便切换不同的API服务
            - **支持的API**: OpenAI、Azure OpenAI、DeepSeek、通义千问等OpenAI兼容的API
              - OpenAI: `https://api.openai.com/v1`
              - Azure OpenAI: 你的Azure端点
              - DeepSeek: `https://api.deepseek.com/v1`
              - 通义千问: `https://dashscope.aliyuncs.com/compatible-mode/v1`
            - **在AI分析中使用**: 保存配置后，在"AI分析"标签页选择配置即可使用

            ### ⚙️ 工作原理
            - 大文件夹阈值：1GB
            - **多线程并发**: 第一层子文件夹使用8个线程并发扫描
            - 只对大于1GB的文件夹进行下钻扫描
            - **扫描会话管理**: 每次扫描创建独立会话，记录完整信息
              - 会话ID：唯一标识每次扫描
              - 统计信息：扫描时间、持续时长、文件夹总数、大文件夹数量、总大小
            - **数据存储**: 所有扫描结果保存到SQLite数据库
              - scan_sessions表：存储扫描会话信息
              - scans表：存储具体文件夹扫描记录，关联到会话ID
              - ai_configs表：存储AI配置，支持多个配置管理
            - **智能过滤**: 自动跳过大小为0的文件夹，不存储到数据库
            - 支持多次扫描建立历史数据，方便对比分析
            - **AI智能分析**: 使用大语言模型分析并提供清理建议

            ### 💡 使用建议
            - 首次扫描建议从较小的目录开始（如用户目录）
            - 扫描整个C盘可能需要较长时间
            - 定期扫描同一路径可以观察变化趋势
            - 观察实时更新可以看到扫描进度和发现的大文件夹
            - **多线程加速**: 大目录扫描速度提升明显
            - **AI分析建议**: 扫描完成后使用AI分析获取专业的清理建议
            - **保存AI配置**: 常用的API配置保存后，下次使用更方便
            - **查看扫描历史**: 定期查看历史记录，了解磁盘占用变化

            ### 📈 性能优化
            - 使用线程池（8个worker）并发扫描第一层子文件夹
            - 进度条只计算第一层的完成度，更准确
            - 树形结构增量展示，避免界面闪烁
            - 快速模式可节省API调用成本
            - 自动跳过空文件夹，减少数据库存储
            """)

        # 事件绑定
        scan_btn.click(
            fn=scan_folder,
            inputs=[path_input, max_depth_input, exclude_paths_input],
            outputs=[summary_output, tree_output, dataframe_output]
        )

        # AI分析按钮事件
        analyze_btn.click(
            fn=analyze_with_ai,
            inputs=[ai_config_dropdown, history_scan_dropdown, quick_mode_input],
            outputs=[ai_status_output, ai_result_output]
        )
        
        # 刷新AI配置下拉框（用于AI分析）
        def refresh_ai_config_for_analysis():
            _, choices = get_ai_configs_list()
            return gr.Dropdown(choices=choices, value=None)
        
        refresh_ai_config_for_analysis_btn.click(
            fn=refresh_ai_config_for_analysis,
            inputs=[],
            outputs=[ai_config_dropdown]
        )
        
        # 刷新历史扫描结果下拉框（用于AI分析）
        def refresh_history_scans_for_ai():
            _, choices = get_scan_sessions_history()
            return gr.Dropdown(choices=choices, value=None)
        
        refresh_history_scans_btn.click(
            fn=refresh_history_scans_for_ai,
            inputs=[],
            outputs=[history_scan_dropdown]
        )

        # 保存AI配置按钮事件（在AI配置管理标签页）
        save_config_btn.click(
            fn=save_ai_config_handler,
            inputs=[config_name_input, api_key_input, base_url_input, model_input, language_input, config_id_hidden],
            outputs=[config_status_output, config_id_hidden]
        )

        # 清空配置表单按钮事件
        clear_config_btn.click(
            fn=clear_config_form,
            inputs=[],
            outputs=[config_name_input, api_key_input, base_url_input, model_input, language_input, config_id_hidden]
        )

        # 刷新扫描历史按钮事件
        def refresh_history():
            text, choices = get_scan_sessions_history()
            return text, gr.Dropdown(choices=choices, value=None)
        
        refresh_history_btn.click(
            fn=refresh_history,
            inputs=[],
            outputs=[scan_history_output, history_session_dropdown]
        )
        
        # 查看历史扫描详情按钮事件
        view_history_btn.click(
            fn=view_history_scan_detail,
            inputs=[history_session_dropdown],
            outputs=[history_summary_output, history_tree_output, history_dataframe_output]
        )

        # 刷新AI配置列表按钮事件
        def refresh_ai_configs():
            text, choices = get_ai_configs_list()
            return text, gr.Dropdown(choices=choices, value=None)

        refresh_configs_btn.click(
            fn=refresh_ai_configs,
            inputs=[],
            outputs=[ai_configs_output, edit_config_dropdown]
        )
        
        # 从配置下拉框加载配置到表单（用于编辑）- 使用change事件
        def on_config_selected_for_edit(config_id):
            if not config_id:
                return "", "", "", "zh", None, ""
            return load_config_to_form(config_id)
        
        edit_config_dropdown.change(
            fn=on_config_selected_for_edit,
            inputs=[edit_config_dropdown],
            outputs=[config_name_input, api_key_input, base_url_input, model_input, language_input, config_id_hidden, config_status_output]
        )

    return app
