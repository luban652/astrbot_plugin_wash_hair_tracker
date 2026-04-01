import sqlite3
import datetime
import aiosqlite
from pathlib import Path
from typing import Tuple
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

@register("astrbot_plugin_wash_hair_tracker", "AstrBot", "记录并管理用户的洗头频率，支持多用户独立存储。", "1.1.0", "https://github.com/astrbot/astrbot_plugin_wash_hair_tracker")
class WashHairTrackerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 修复：使用 StarTools.get_data_dir() 获取规范的数据目录
        data_dir = StarTools.get_data_dir()
        self.db_path = data_dir / "wash_hair.db"
        self._init_db_sync()

    def _init_db_sync(self):
        """同步初始化数据库环境，确保目录存在"""
        try:
            # 修复：移除对 os 的依赖，使用 Path 对象
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS wash_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        group_id TEXT DEFAULT '',
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.commit()
            logger.info(f"洗头记录器数据库初始化成功: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库初始化失败: {str(e)}")

    def _get_target_id(self, event: AstrMessageEvent) -> Tuple[str, str]:
        """获取用户ID和群组ID（根据配置决定是否启用群组隔离）"""
        user_id = event.get_sender_id()
        group_id = ""
        if self.config.get("enable_group_isolation", False):
            # 兼容性处理：优先从 event 获取 group_id
            group_id = getattr(event, "group_id", "") or ""
        return user_id, group_id

    @filter.command("洗头")
    async def record_wash(self, event: AstrMessageEvent):
        """记录一次洗头时间"""
        user_id, group_id = self._get_target_id(event)
        now = datetime.datetime.now()
        
        try:
            # 修复：使用 aiosqlite 进行异步数据库操作
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO wash_records (user_id, group_id, timestamp) VALUES (?, ?, ?)",
                    (user_id, group_id, now)
                )
                
                max_count = self.config.get("max_record_count", 100)
                if max_count > 0:
                    # 修复：SQL 注入风险，使用参数化查询 OFFSET
                    # 注意：SQLite 的 OFFSET 必须配合 LIMIT 使用，这里 LIMIT -1 代表无上限
                    await db.execute(
                        """DELETE FROM wash_records 
                           WHERE id IN (
                               SELECT id FROM wash_records 
                               WHERE user_id = ? AND group_id = ? 
                               ORDER BY timestamp DESC 
                               LIMIT -1 OFFSET ?
                           )""",
                        (user_id, group_id, max_count)
                    )
                await db.commit()
            
            dt_format = self.config.get("datetime_format", "%Y-%m-%d %H:%M:%S")
            time_str = now.strftime(dt_format)
            msg_tmpl = self.config.get("record_success_msg", "已为你记录本次洗头时间：{time}。")
            yield event.plain_result(msg_tmpl.format(time=time_str))
            
        except Exception as e:
            logger.error(f"记录洗头失败: {str(e)}")
            yield event.plain_result("记录失败，请检查后台日志。")

    @filter.command("洗头情况")
    async def query_wash(self, event: AstrMessageEvent):
        """查询历史记录"""
        user_id, group_id = self._get_target_id(event)
        dt_format = self.config.get("datetime_format", "%Y-%m-%d %H:%M:%S")
        
        try:
            # 修复：使用 aiosqlite 进行异步查询
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT timestamp FROM wash_records WHERE user_id = ? AND group_id = ? ORDER BY timestamp ASC",
                    (user_id, group_id)
                ) as cursor:
                    rows = await cursor.fetchall()
            
            if not rows:
                yield event.plain_result("你还没有任何洗头记录哦。")
                return

            header = self.config.get("list_header_msg", "📅 你的洗头历史记录：")
            result_lines = [header]
            
            for idx, (ts_val,) in enumerate(rows, 1):
                try:
                    # 兼容：aiosqlite 可能返回 str 或 datetime 对象
                    if isinstance(ts_val, str):
                        dt = datetime.datetime.fromisoformat(ts_val)
                    else:
                        dt = ts_val
                    formatted_time = dt.strftime(dt_format)
                except Exception:
                    formatted_time = str(ts_val)
                result_lines.append(f"{idx}. {formatted_time}")
            
            yield event.plain_result("\n".join(result_lines))
            
        except Exception as e:
            logger.error(f"查询记录失败: {str(e)}")
            yield event.plain_result("查询失败，请稍后再试。")

    @filter.command("洗头清空")
    async def clear_wash(self, event: AstrMessageEvent):
        """清空记录"""
        user_id, group_id = self._get_target_id(event)
        
        if self.config.get("clear_confirm_required", True):
            confirm_keyword = "确认清空"
            # 使用更健壮的文本匹配方式
            if confirm_keyword not in event.message_str:
                yield event.plain_result(f"⚠️ 警告：该操作将删除你所有的记录且不可恢复。如果确定，请发送：\n/洗头清空 {confirm_keyword}")
                return

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "DELETE FROM wash_records WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id)
                )
                await db.commit()
            yield event.plain_result("已成功清空你的所有洗头记录。")
        except Exception as e:
            logger.error(f"清空记录失败: {str(e)}")
            yield event.plain_result("清空操作失败。")

    async def terminate(self):
        """插件卸载处理"""
        logger.info("洗头记录器插件已停用。")
