"""AI分析模块 - 使用OpenAI兼容接口分析扫描结果"""
import os
import logging
from typing import Optional, List
from models import ScanResult
from scanner import FolderScanner, GB_THRESHOLD_BYTES

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class AIAnalyzer:
    """使用AI分析扫描结果并给出清理建议"""

    def __init__(self, api_key: str = None, base_url: str = None, model: str = "gpt-4o-mini"):
        """
        初始化AI分析器

        Args:
            api_key: OpenAI API密钥（如果为None则从环境变量OPENAI_API_KEY读取）
            base_url: API基础URL（如果为None则使用默认OpenAI URL）
            model: 使用的模型名称
        """
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("请先安装openai库: uv add openai")

        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("API Key未设置，请提供api_key参数或设置OPENAI_API_KEY环境变量")

        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = model

        # 初始化客户端
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

    def format_scan_results(self, result: ScanResult, max_items: int = 50) -> str:
        """将扫描结果格式化为适合分析的文本"""
        lines = []
        lines.append("# 文件夹扫描结果分析")
        lines.append("")
        lines.append(f"## 扫描路径")
        lines.append(f"- 路径: {result.path}")
        lines.append(f"- 总大小: {FolderScanner.format_size(result.size_bytes)}")
        lines.append(f"- 大文件夹阈值: {GB_THRESHOLD_BYTES / (1024**3):.0f} GB")
        lines.append("")

        # 收集所有大文件夹
        large_folders = self._collect_large_folders(result)

        lines.append(f"## 发现的大文件夹 (共{len(large_folders)}个)")
        lines.append("")
        lines.append("以下是按大小排序的大文件夹列表：")
        lines.append("")

        # 按大小排序并限制数量
        large_folders.sort(key=lambda x: x['size_bytes'], reverse=True)
        display_folders = large_folders[:max_items]

        for i, folder in enumerate(display_folders, 1):
            lines.append(f"### {i}. {folder['name']}")
            lines.append(f"- 完整路径: `{folder['path']}`")
            lines.append(f"- 大小: {FolderScanner.format_size(folder['size_bytes'])} ({folder['size_gb']:.2f} GB)")
            lines.append(f"- 深度: {folder['depth']}")
            lines.append("")

        if len(large_folders) > max_items:
            lines.append(f"*... 还有 {len(large_folders) - max_items} 个大文件夹未显示*")
            lines.append("")

        # 添加总体统计
        total_large_size = sum(f['size_bytes'] for f in large_folders)
        percentage = (total_large_size / result.size_bytes * 100) if result.size_bytes > 0 else 0

        lines.append("## 总体统计")
        lines.append(f"- 大文件夹总大小: {FolderScanner.format_size(total_large_size)}")
        lines.append(f"- 占总空间比例: {percentage:.1f}%")
        lines.append(f"- 平均每个大文件夹: {FolderScanner.format_size(total_large_size / len(large_folders)) if large_folders else 0}")
        lines.append("")

        return "\n".join(lines)

    def _collect_large_folders(self, result: ScanResult, large_folders: List[dict] = None) -> List[dict]:
        """递归收集所有大文件夹"""
        if large_folders is None:
            large_folders = []

        if result.is_large:
            large_folders.append({
                'name': os.path.basename(result.path) or result.path,
                'path': result.path,
                'size_bytes': result.size_bytes,
                'size_gb': result.size_gb,
                'depth': result.depth
            })

        for child in result.children:
            self._collect_large_folders(child, large_folders)

        return large_folders

    def analyze(self, result: ScanResult, language: str = "zh") -> str:
        """
        分析扫描结果并生成清理建议

        Args:
            result: 扫描结果
            language: 响应语言 ("zh"=中文, "en"=英文)

        Returns:
            AI生成的清理建议文本
        """
        logger.info(f"[AI分析] 开始分析，语言: {language}, 路径: {result.path}, 大小: {result.size_bytes}")
        
        # 格式化扫描结果
        scan_text = self.format_scan_results(result)
        logger.info(f"[AI分析] 格式化结果完成，文本长度: {len(scan_text)} 字符")
        
        if not scan_text or len(scan_text.strip()) == 0:
            logger.warning("[AI分析] 警告: 格式化后的扫描结果为空！")
            return "❌ 扫描结果为空，无法进行分析"

        # 构建提示词
        if language == "zh":
            system_prompt = """你是一个专业的磁盘空间管理专家。请分析用户提供的文件夹扫描结果，并提供具体的清理建议。

你的任务是：
1. 识别哪些文件夹可能包含可以安全删除的内容
2. 对于每个大文件夹，分析其可能的用途和清理建议
3. 提供具体的清理步骤和注意事项
4. 按优先级排序清理建议
5. 警告用户不要删除系统关键文件

请使用清晰的格式，包括：
- 🎯 高优先级清理项（通常可以安全删除）
- ⚠️ 需要小心的项（删除前请检查）
- 🔒 不建议删除的项（系统文件）
- 💡 其他优化建议"""

            user_prompt = f"""请分析以下文件夹扫描结果，并提供清理建议：

{scan_text}

请提供详细的分析和建议。"""
        else:
            system_prompt = """You are a disk space management expert. Analyze the folder scan results and provide cleanup suggestions.

Your tasks:
1. Identify folders that may contain deletable content
2. For each large folder, analyze its potential use and cleanup suggestions
3. Provide specific cleanup steps and precautions
4. Prioritize cleanup suggestions
5. Warn users not to delete critical system files

Use clear formatting including:
- 🎯 High priority items (usually safe to delete)
- ⚠️ Items requiring caution (check before deleting)
- 🔒 Items not recommended for deletion (system files)
- 💡 Other optimization suggestions"""

            user_prompt = f"""Please analyze the following folder scan results and provide cleanup suggestions:

{scan_text}

Provide detailed analysis and suggestions."""

        logger.info(f"[AI分析] 准备调用API，模型: {self.model}, Base URL: {self.base_url}")
        logger.debug(f"[AI分析] System prompt长度: {len(system_prompt)}, User prompt长度: {len(user_prompt)}")

        try:
            # 调用OpenAI API
            logger.info("[AI分析] 正在调用API...")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=2000
            )

            logger.info(f"[AI分析] API调用成功，响应对象: {type(response)}")
            logger.info(f"[AI分析] Response choices数量: {len(response.choices) if hasattr(response, 'choices') else 0}")
            
            if not hasattr(response, 'choices') or len(response.choices) == 0:
                logger.error("[AI分析] 错误: API响应中没有choices")
                return "❌ AI分析失败: API响应格式异常，没有返回结果"
            
            choice = response.choices[0]
            logger.info(f"[AI分析] Choice对象: {type(choice)}, finish_reason: {getattr(choice, 'finish_reason', 'N/A')}")
            
            if not hasattr(choice, 'message') or choice.message is None:
                logger.error("[AI分析] 错误: Choice中没有message")
                return "❌ AI分析失败: API响应格式异常，没有返回消息"
            
            content = choice.message.content
            logger.info(f"[AI分析] 获取到内容，长度: {len(content) if content else 0}, 内容预览: {content[:100] if content else 'None'}...")
            
            if not content or len(content.strip()) == 0:
                logger.warning("[AI分析] 警告: API返回的内容为空")
                return "❌ AI分析失败: API返回了空结果，可能是模型响应异常或token限制"
            
            logger.info("[AI分析] 分析完成，返回结果")
            return content

        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            logger.error(f"[AI分析] 异常发生: {str(e)}")
            logger.error(f"[AI分析] 异常详情:\n{error_detail}")
            return f"❌ AI分析失败: {str(e)}\n\n请检查：\n1. API Key是否正确\n2. Base URL是否可访问\n3. 模型名称是否正确"

    def quick_analyze(self, result: ScanResult) -> str:
        """快速分析 - 只分析最大的前10个文件夹"""
        logger.info(f"[快速分析] 开始快速分析，路径: {result.path}, 大小: {result.size_bytes}")
        
        # 创建一个只包含前10个大文件夹的简化结果
        large_folders = self._collect_large_folders(result)
        logger.info(f"[快速分析] 收集到大文件夹数量: {len(large_folders)}")
        
        large_folders.sort(key=lambda x: x['size_bytes'], reverse=True)
        top_10 = large_folders[:10]
        logger.info(f"[快速分析] 选择前10个文件夹进行分析")

        # 构建简化分析文本
        text = f"# 快速分析 - 最大的10个文件夹\n\n"
        text += f"总大小: {FolderScanner.format_size(result.size_bytes)}\n\n"
        text += "## 占用空间最多的文件夹:\n\n"

        for i, folder in enumerate(top_10, 1):
            text += f"{i}. **{folder['name']}**\n"
            text += f"   - 路径: `{folder['path']}`\n"
            text += f"   - 大小: {FolderScanner.format_size(folder['size_bytes'])}\n\n"

        text += "\n请提供这些文件夹的清理建议。"
        
        logger.info(f"[快速分析] 构建分析文本完成，文本长度: {len(text)} 字符")
        
        if not text or len(text.strip()) == 0:
            logger.warning("[快速分析] 警告: 分析文本为空！")
            return "❌ 快速分析失败: 无法生成分析文本"

        try:
            logger.info(f"[快速分析] 准备调用API，模型: {self.model}, Base URL: {self.base_url}")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个磁盘空间管理专家。请简要分析这些大文件夹，给出清理建议。使用简洁的格式。"
                    },
                    {"role": "user", "content": text}
                ],
                temperature=0.7,
                max_tokens=1000
            )

            logger.info(f"[快速分析] API调用成功，响应对象: {type(response)}")
            logger.info(f"[快速分析] Response choices数量: {len(response.choices) if hasattr(response, 'choices') else 0}")
            
            if not hasattr(response, 'choices') or len(response.choices) == 0:
                logger.error("[快速分析] 错误: API响应中没有choices")
                return "❌ 快速分析失败: API响应格式异常，没有返回结果"
            
            choice = response.choices[0]
            logger.info(f"[快速分析] Choice对象: {type(choice)}, finish_reason: {getattr(choice, 'finish_reason', 'N/A')}")
            
            if not hasattr(choice, 'message') or choice.message is None:
                logger.error("[快速分析] 错误: Choice中没有message")
                return "❌ 快速分析失败: API响应格式异常，没有返回消息"
            
            content = choice.message.content
            logger.info(f"[快速分析] 获取到内容，长度: {len(content) if content else 0}, 内容预览: {content[:100] if content else 'None'}...")
            
            if not content or len(content.strip()) == 0:
                logger.warning("[快速分析] 警告: API返回的内容为空")
                return "❌ 快速分析失败: API返回了空结果，可能是模型响应异常或token限制"
            
            logger.info("[快速分析] 分析完成，返回结果")
            return content

        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            logger.error(f"[快速分析] 异常发生: {str(e)}")
            logger.error(f"[快速分析] 异常详情:\n{error_detail}")
            return f"❌ 快速分析失败: {str(e)}"


# 便捷函数
def create_analyzer(api_key: str = None, base_url: str = None, model: str = "gpt-4o-mini") -> AIAnalyzer:
    """创建AI分析器的便捷函数"""
    return AIAnalyzer(api_key=api_key, base_url=base_url, model=model)
