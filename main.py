from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
import httpx
from .models import ArticleListResponse, Article
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from typing import Set, List
import json
from pathlib import Path
import re

@register("mcbe_news", "astrbot_plugin_mcbe_news", "从 minecraft.net 官网上定时获取最新的更新 blog 并调用 LLM解析", "1.0.2")
class MyPlugin(Star):
    
    bedrock_beta_news_api = "https://feedback.minecraft.net/api/v2/help_center/en-us/sections/360001185332/articles.json?sort_by=created_at&sort_order=desc"
    bedrock_news_api = "https://feedback.minecraft.net/api/v2/help_center/en-us/sections/360001186971/articles.json?sort_by=created_at&sort_order=desc"
    feedback_base_url = "https://feedback.minecraft.net"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = AsyncIOScheduler()
        
        # 数据存储路径
        self.data_dir = Path("data/mcbe_news")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.seen_articles_file = self.data_dir / "seen_articles.json"
        self.registered_groups_file = self.data_dir / "registered_groups.json"
        
        # 检测是否首次运行
        self.is_first_run = not self.seen_articles_file.exists()
        
        # 已见过的文章 ID 集合（用于去重）
        self.seen_article_ids: Set[int] = self._load_seen_articles()
        
        # 注册的群聊映射 {group_id: unified_msg_origin}
        self.registered_groups: dict = self._load_registered_groups()

    def _load_seen_articles(self) -> Set[int]:
        """从文件加载已见过的文章 ID"""
        if self.seen_articles_file.exists():
            try:
                with open(self.seen_articles_file, 'r') as f:
                    data = json.load(f)
                    return set(data.get('seen_ids', []))
            except Exception as e:
                logger.error(f"加载已见文章 ID 失败: {e}")
        return set()
    
    def _save_seen_articles(self):
        """保存已见过的文章 ID 到文件"""
        try:
            with open(self.seen_articles_file, 'w') as f:
                json.dump({'seen_ids': list(self.seen_article_ids)}, f)
        except Exception as e:
            logger.error(f"保存已见文章 ID 失败: {e}")
    
    def _load_registered_groups(self) -> dict:
        """从文件加载注册的群聊"""
        if self.registered_groups_file.exists():
            try:
                with open(self.registered_groups_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载注册群聊失败: {e}")
        return {}
    
    def _save_registered_groups(self):
        """保存注册的群聊到文件"""
        try:
            with open(self.registered_groups_file, 'w') as f:
                json.dump(self.registered_groups, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存注册群聊失败: {e}")
    
    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        # 启动定时任务
        cron_expr = self.config.get('cron_expression', '0 */2 * * *')
        try:
            # 解析 cron 表达式 (分 时 日 月 星期)
            parts = cron_expr.strip().split()
            if len(parts) != 5:
                raise ValueError(f"Cron 表达式格式错误: {cron_expr}")
            
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4]
            )
            
            self.scheduler.add_job(
                self._check_updates,
                trigger=trigger,
                id='mcbe_news_checker',
                replace_existing=True
            )
            
            self.scheduler.start()
            logger.info(f"MCBE 新闻监控已启动，Cron 表达式: {cron_expr}")
        except Exception as e:
            logger.error(f"启动定时任务失败: {e}")

    async def _check_updates(self):
        """定时检查更新"""
        try:
            logger.info("开始检查 MCBE 更新...")
            
            # 如果是首次运行，记录日志
            if self.is_first_run:
                logger.info("检测到首次运行，将只推送最新文章")
            
            new_articles = []
            
            # 检查 Beta 版本
            if self.config.get('enable_beta_monitor', True):
                beta_articles = await self._fetch_articles(self.bedrock_beta_news_api, 'Beta')
                new_articles.extend(beta_articles)
            
            # 检查正式版
            if self.config.get('enable_release_monitor', True):
                release_articles = await self._fetch_articles(self.bedrock_news_api, 'Release')
                new_articles.extend(release_articles)
            
            # 首次运行后，标记为非首次运行
            if self.is_first_run:
                self.is_first_run = False
                logger.info("首次初始化完成")
            
            if new_articles:
                logger.info(f"发现 {len(new_articles)} 篇新文章")
                await self._process_new_articles(new_articles)
            else:
                logger.info("没有发现新文章")
                
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
    
    async def _fetch_articles(self, api_url: str, version_type: str) -> List[tuple]:
        """获取文章列表，返回新文章列表"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(api_url)
                resp.raise_for_status()
                data = ArticleListResponse.model_validate(resp.json())
                
                # 如果是首次运行，只返回最新的一篇文章，但将所有文章标记为已见
                if self.is_first_run:
                    logger.info(f"首次运行，将所有 {version_type} 文章标记为已见，仅推送最新的一篇")
                    
                    # 将所有文章标记为已见
                    for article in data.articles:
                        self.seen_article_ids.add(article.id)
                    
                    # 只返回最新的一篇文章（如果有的话）
                    new_articles = []
                    if data.articles:
                        new_articles.append((data.articles[0], version_type))
                    
                    self._save_seen_articles()
                    return new_articles
                
                # 正常运行：筛选出新文章
                new_articles = []
                for article in data.articles:
                    if article.id not in self.seen_article_ids:
                        new_articles.append((article, version_type))
                        self.seen_article_ids.add(article.id)
                
                # 保存已见文章 ID
                if new_articles:
                    self._save_seen_articles()
                
                return new_articles
                
        except Exception as e:
            logger.error(f"获取 {version_type} 文章失败: {e}")
            return []
    
    async def _process_new_articles(self, articles: List[tuple]):
        """处理新文章并发送到群聊"""
        # 优先使用注册的群聊
        if self.registered_groups:
            logger.info(f"使用注册的群聊: {list(self.registered_groups.keys())}")
            # 为每篇文章生成总结并发送
            for article, version_type in articles:
                try:
                    message_chain = await self._create_article_message(article, version_type)
                    await self._send_to_registered_groups(message_chain)
                except Exception as e:
                    logger.error(f"处理文章 {article.title} 失败: {e}")
            return
        
        # 如果没有注册群聊，尝试使用配置的群聊 ID
        group_ids_str = self.config.get('group_ids', '')
        if not group_ids_str or not group_ids_str.strip():
            logger.warning("未配置群聊 ID 也未注册群聊，跳过消息发送。请使用 /mcbe_register 命令在目标群聊中注册。")
            return
        
        # 解析群聊 ID 列表
        group_ids = [gid.strip() for gid in group_ids_str.split(',') if gid.strip()]
        
        if not group_ids:
            logger.warning("群聊 ID 列表为空，跳过消息发送")
            return
        
        # 为每篇文章生成总结并发送
        for article, version_type in articles:
            try:
                message_chain = await self._create_article_message(article, version_type)
                await self._send_to_groups(group_ids, message_chain)
            except Exception as e:
                logger.error(f"处理文章 {article.title} 失败: {e}")
    
    async def _create_article_message(self, article: Article, version_type: str) -> MessageChain:
        """创建文章消息链"""
        # 解析文章内容和图片
        soup = BeautifulSoup(article.body, "html.parser")
        
        # 提取纯文本内容用于 LLM 总结
        article_text = soup.get_text(separator="\n", strip=True)
        
        # 调用 LLM 生成总结
        summary = await self._summarize_article(article, article_text, version_type)
        
        # 构建消息链
        components = []
        
        # 标题和基本信息
        header = f"📢 {article.title}\n"
        header += f"🗓 发布时间：{article.updated_at.strftime('%Y-%m-%d %H:%M')}\n"
        header += f"🔗 原文链接：{article.html_url}\n"
        header += "━━━━━━━━━━━━━━━━━━━━\n"
        
        components.append(Comp.Plain(header))
        
        # AI 总结
        components.append(Comp.Plain(f"\n📝 AI 总结：\n{summary}\n\n"))
        
        # 按照原文顺序提取图片
        content_components = self._extract_content_with_images(soup)
        components.extend(content_components)
        
        # 创建消息链
        message_chain = MessageChain()
        for comp in components:
            message_chain.chain.append(comp)
        
        return message_chain
    
    def _extract_content_with_images(self, soup: BeautifulSoup) -> List:
        """按照原文顺序提取图片"""
        components = []
        image_count = 0
        max_images = 10  # 限制最多显示的图片数量
        processed_imgs = set()  # 记录已处理的图片，避免重复
        
        # 遍历文章的所有元素，按照文档顺序
        for element in soup.find_all(['figure', 'img']):
            try:
                # 处理 figure 元素（通常包含图片）
                if element.name == 'figure':
                    img = element.find('img')
                    if img and image_count < max_images:
                        src = img.get('src', '')
                        if src and src not in processed_imgs:
                            if src.startswith('/'):
                                src = self.feedback_base_url + src
                            
                            components.append(Comp.Image.fromURL(src))
                            components.append(Comp.Plain("\n"))
                            image_count += 1
                            processed_imgs.add(src)
                            logger.info(f"添加图片 [{image_count}]: {src}")
                
                # 处理独立的图片标签
                elif element.name == 'img':
                    src = element.get('src', '')
                    if src and src not in processed_imgs and image_count < max_images:
                        if src.startswith('/'):
                            src = self.feedback_base_url + src
                        
                        components.append(Comp.Image.fromURL(src))
                        components.append(Comp.Plain("\n"))
                        image_count += 1
                        processed_imgs.add(src)
                        logger.info(f"添加图片 [{image_count}]: {src}")
                
            except Exception as e:
                logger.error(f"处理元素 {element.name} 时出错: {e}")
                continue
        
        if image_count > 0:
            logger.info(f"提取完成：共提取 {image_count} 张图片")
        else:
            logger.info("未发现图片")
        
        return components
    
    async def _summarize_article(self, article: Article, article_text: str, version_type: str) -> str:
        """使用 LLM 总结文章"""
        try:
            # 限制文章长度避免超出 token 限制
            truncated_text = article_text
            
            prompt = f"""请帮我总结以下 Minecraft 基岩版的更新文章内容，并用简洁的中文列出主要更新要点：

版本类型：{version_type}
标题：{article.title}
发布时间：{article.updated_at.strftime('%Y-%m-%d')}

文章内容：
{truncated_text}

请用要点形式总结，包括：
✨ 主要新增功能
🔧 重要修复的 Bug
📌 其他值得注意的变化

重要要求：
1. 请使用纯文本格式，不要使用 Markdown 语法（不要使用 **、*、`、#、- 等符号）
2. 可以使用 emoji 让内容更生动
3. 每个要点单独一行，使用 • 或 emoji 作为项目符号
4. 保持简洁明了，但不要忽视细节"""

            provider_id = self.config.get('llm_provider', None)
            llm_response = await self.context.llm_generate(
                prompt=prompt,
                chat_provider_id=provider_id if provider_id else None
            )
            
            # 从 LLMResponse 对象获取文本内容
            return llm_response.completion_text.strip()
            
        except Exception as e:
            logger.error(f"LLM 总结失败: {e}")
            # 如果 LLM 总结失败，返回简短摘要
            return f"无法生成总结，请查看原文了解详情。\n\n{article_text[:200]}..."
    
    async def _send_to_registered_groups(self, message_chain: MessageChain):
        """发送消息到已注册的群聊"""
        for group_id, unified_msg_origin in self.registered_groups.items():
            try:
                await self.context.send_message(unified_msg_origin, message_chain)
                logger.info(f"消息已发送到群聊: {group_id}")
            except Exception as e:
                logger.error(f"发送消息到群聊 {group_id} 失败: {e}")
    
    async def _send_to_groups(self, group_ids: List[str], message_chain: MessageChain):
        """发送消息到指定的群聊（使用群聊 ID）"""
        for group_id in group_ids:
            try:
                # 构建 unified_msg_origin
                # 格式通常为: platform:group:group_id 或类似格式
                # 这里使用通用格式，具体格式可能需要根据实际平台调整
                unified_msg_origin = f"group_{group_id}"
                
                await self.context.send_message(unified_msg_origin, message_chain)
                logger.info(f"消息已发送到群聊: {group_id}")
                
            except Exception as e:
                logger.error(f"发送消息到群聊 {group_id} 失败: {e}")
    
    @filter.command("mcbe_news")
    async def mcbe_news(self, event: AstrMessageEvent):
        """获取 MCBE 最新更新 BLOG，并调用 LLM 解析回复
        
        用法:
        /mcbe_news - 获取正式版最新文章（默认）
        /mcbe_news beta - 获取 Beta 版最新文章
        /mcbe_news release - 获取正式版最新文章
        """
        try:
            # 解析命令参数
            message_text = event.message_str.strip()
            version_type = 'Release'  # 默认正式版
            api_url = self.bedrock_news_api
            
            # 检查是否指定了版本类型
            if 'beta' in message_text.lower():
                version_type = 'Beta'
                api_url = self.bedrock_beta_news_api
            elif 'release' in message_text.lower():
                version_type = 'Release'
                api_url = self.bedrock_news_api
            
            version_name = "Beta 测试版" if version_type == 'Beta' else "正式版"
            logger.info(f"开始获取最新 MCBE {version_name}文章...")
            
            # 获取最新文章（使用异步请求）
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(api_url)
                resp.raise_for_status()
                data = ArticleListResponse.model_validate(resp.json())
                
                if not data.articles:
                    yield event.plain_result(f"❌ 未找到 {version_name} 文章")
                    return
                
                article = data.articles[0]
            
            logger.info(f"获取到文章: {article.title}")
            
            # 先发送一条提示消息
            yield event.plain_result(f"正在为您解析最新的 MCBE {version_name}更新文章...\n📰 {article.title}")
            
            # 创建消息链
            message_chain = await self._create_article_message(article, version_type)
            
            # 发送消息链
            yield event.chain_result(message_chain.chain)
            logger.info("消息发送成功")
            
        except Exception as e:
            logger.error(f"处理 MCBE 新闻时出错: {str(e)}")
            yield event.plain_result(f"❌ 获取或解析新闻时出错: {str(e)}")
    
    @filter.command("mcbe_list")
    async def mcbe_list(self, event: AstrMessageEvent):
        """获取最新 n 篇文章的预览列表
        
        用法:
        /mcbe_list - 获取最新 3 篇正式版文章预览（默认）
        /mcbe_list 5 - 获取最新 5 篇正式版文章预览
        /mcbe_list beta - 获取最新 3 篇 Beta 版文章预览
        /mcbe_list beta 5 - 获取最新 5 篇 Beta 版文章预览
        """
        try:
            # 解析命令参数
            message_text = event.message_str.strip()
            parts = message_text.split()
            
            # 默认值
            version_type = 'Release'
            api_url = self.bedrock_news_api
            count = 3  # 默认 3 篇
            
            # 解析参数
            for part in parts[1:]:  # 跳过命令本身
                if part.lower() == 'beta':
                    version_type = 'Beta'
                    api_url = self.bedrock_beta_news_api
                elif part.lower() == 'release':
                    version_type = 'Release'
                    api_url = self.bedrock_news_api
                elif part.isdigit():
                    count = min(int(part), 5)  # 最多 5 篇
            
            version_name = "Beta 测试版" if version_type == 'Beta' else "正式版"
            yield event.plain_result(f"🔍 正在获取最新 {count} 篇 MCBE {version_name}文章...")
            
            # 获取文章列表
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(api_url)
                resp.raise_for_status()
                data = ArticleListResponse.model_validate(resp.json())
                
                if not data.articles:
                    yield event.plain_result(f"❌ 未找到 {version_name} 文章")
                    return
                
                # 获取指定数量的文章
                articles = data.articles[:count]
            
            # 构建预览列表
            result = f"📋 最新 {len(articles)} 篇 MCBE {version_name}更新\n"
            result += "━━━━━━━━━━━━━━━━━━━━\n\n"
            
            for i, article in enumerate(articles, 1):
                # 解析文章内容
                soup = BeautifulSoup(article.body, "html.parser")
                article_text = soup.get_text(separator=" ", strip=True)
                
                # 截取前 150 个字符作为预览
                preview = article_text[:150]
                if len(article_text) > 150:
                    preview += "..."
                
                # 格式化单篇文章预览
                result += f"{i}. 📰 {article.title}\n"
                result += f"   🗓 {article.updated_at.strftime('%Y-%m-%d %H:%M')}\n"
                result += f"   📝 {preview}\n"
                result += f"   🔗 {article.html_url}\n"
                
                # 最后一篇不加分隔线
                if i < len(articles):
                    result += "\n" + "─" * 20 + "\n\n"
            
            result += "\n━━━━━━━━━━━━━━━━━━━━\n"
            result += f"💡 使用 /mcbe_news 查看完整文章"
            
            yield event.plain_result(result)
            logger.info(f"成功获取 {len(articles)} 篇文章预览")
            
        except Exception as e:
            logger.error(f"获取文章列表失败: {str(e)}")
            yield event.plain_result(f"❌ 获取文章列表失败: {str(e)}")
    
    @filter.command("mcbe_check")
    async def mcbe_check(self, event: AstrMessageEvent):
        """手动触发检查更新"""
        try:
            yield event.plain_result("🔍 开始检查 MCBE 更新...")
            
            # 手动触发检查
            await self._check_updates()
            
            yield event.plain_result("✅ 检查完成！如有新文章将发送到配置的群聊。")
            
        except Exception as e:
            logger.error(f"手动检查更新失败: {str(e)}")
            yield event.plain_result(f"❌ 检查更新失败: {str(e)}")
    
    @filter.command("mcbe_status")
    async def mcbe_status(self, event: AstrMessageEvent):
        """查看监控状态"""
        try:
            status = "📊 MCBE 新闻监控状态\n"
            status += "━━━━━━━━━━━━━━━━━━━━\n\n"
            
            status += f"🤖 LLM 提供商: {self.config.get('llm_provider', '未配置')}\n"
            
            # 显示注册的群聊
            if self.registered_groups:
                status += f"📱 已注册群聊: {', '.join(self.registered_groups.keys())}\n"
            else:
                status += f"📱 通知群聊: {self.config.get('group_ids', '未配置')}\n"
            
            status += f"🧪 Beta 监控: {'✅ 已开启' if self.config.get('enable_beta_monitor', True) else '❌ 已关闭'}\n"
            status += f"🎮 正式版监控: {'✅ 已开启' if self.config.get('enable_release_monitor', True) else '❌ 已关闭'}\n"
            status += f"⏰ Cron 表达式: {self.config.get('cron_expression', '0 */2 * * *')}\n"
            status += f"📝 已记录文章数: {len(self.seen_article_ids)}\n"
            status += f"🔄 调度器状态: {'✅ 运行中' if self.scheduler.running else '❌ 已停止'}\n"
            
            yield event.plain_result(status)
            
        except Exception as e:
            logger.error(f"获取状态失败: {str(e)}")
            yield event.plain_result(f"❌ 获取状态失败: {str(e)}")
    
    @filter.command("mcbe_register")
    async def mcbe_register(self, event: AstrMessageEvent):
        """在当前群聊中注册以接收更新通知"""
        try:
            # 获取当前会话的 unified_msg_origin
            unified_msg_origin = event.unified_msg_origin
            
            # 尝试从 event 中获取群聊信息
            # 这里使用 unified_msg_origin 作为唯一标识
            group_id = unified_msg_origin
            
            # 尝试获取更友好的群聊名称
            try:
                # 尝试从 event 中提取群号或群名
                if hasattr(event, 'group_id'):
                    group_id = str(event.group_id)
                elif 'group' in unified_msg_origin:
                    # 尝试从 unified_msg_origin 中提取群号
                    parts = unified_msg_origin.split(':')
                    if len(parts) >= 3:
                        group_id = parts[2]
            except:
                pass
            
            # 注册群聊
            self.registered_groups[group_id] = unified_msg_origin
            self._save_registered_groups()
            
            yield event.plain_result(f"✅ 成功注册！\n\n该群聊将接收 MCBE 更新通知。\n群聊标识: {group_id}")
            logger.info(f"群聊已注册: {group_id} -> {unified_msg_origin}")
            
        except Exception as e:
            logger.error(f"注册群聊失败: {str(e)}")
            yield event.plain_result(f"❌ 注册失败: {str(e)}")
    
    @filter.command("mcbe_unregister")
    async def mcbe_unregister(self, event: AstrMessageEvent):
        """取消当前群聊的注册"""
        try:
            # 获取当前会话的 unified_msg_origin
            unified_msg_origin = event.unified_msg_origin
            
            # 查找并删除匹配的注册
            removed = False
            for group_id, saved_origin in list(self.registered_groups.items()):
                if saved_origin == unified_msg_origin:
                    del self.registered_groups[group_id]
                    removed = True
                    self._save_registered_groups()
                    yield event.plain_result(f"✅ 已取消注册！\n\n该群聊将不再接收 MCBE 更新通知。")
                    logger.info(f"群聊已取消注册: {group_id}")
                    break
            
            if not removed:
                yield event.plain_result("ℹ️ 该群聊尚未注册。")
            
        except Exception as e:
            logger.error(f"取消注册失败: {str(e)}")
            yield event.plain_result(f"❌ 取消注册失败: {str(e)}")
    
    @filter.command("mcbe_help")
    async def mcbe_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """📖 MCBE 新闻监控插件帮助

━━━━━━━━━━━━━━━━━━━━

📌 命令列表：

/mcbe_news [版本]
获取并展示最新的 MCBE 更新文章（带完整 AI 总结）
• /mcbe_news - 正式版（默认）
• /mcbe_news beta - Beta 测试版
• /mcbe_news release - 正式版

/mcbe_list [版本] [数量]
获取最新 n 篇文章的简短预览列表（不含 AI 总结）
• /mcbe_list - 最新 3 篇正式版（默认）
• /mcbe_list 5 - 最新 5 篇正式版（最多 5 篇）
• /mcbe_list beta - 最新 3 篇 Beta 版
• /mcbe_list beta 5 - 最新 5 篇 Beta 版

/mcbe_register
在当前群聊中注册以接收自动更新通知
(推荐使用此方式，比配置群聊ID更可靠)

/mcbe_unregister
取消当前群聊的注册

/mcbe_check
手动触发检查更新

/mcbe_status
查看当前监控状态

/mcbe_help
显示此帮助信息

━━━━━━━━━━━━━━━━━━━━

💡 使用建议：
1. 在需要接收通知的群聊中使用 /mcbe_register 注册
2. 在 WebUI 配置页面设置 LLM 和监控选项
3. 使用 /mcbe_list 快速浏览最新文章
4. 使用 /mcbe_news 查看感兴趣文章的完整内容
5. 使用 /mcbe_status 查看运行状态

❓ 如有问题，请查看插件的 README.md"""
        
        yield event.plain_result(help_text)
        
    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("MCBE 新闻监控已停止")
