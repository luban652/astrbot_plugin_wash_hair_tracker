import time
import datetime
import asyncio
import aiosqlite
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

@register("shampoo_tracker", "AstrBot-Assistant", "记录并统计用户的洗头频率，支持记录每次洗头的时间戳并提供历史记录查询功能。", "1.1.0", "https://github.com/user/astrbot_plugin_shampoo_tracker")
class ShampooTrackerPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.db = None
        self._lock = asyncio.Lock()
        
        # 修正：使用 StarTools.get_data_dir() 获取数据目录，该方法返回 Path 对象
        self.data_dir = StarTools.get_data_dir()
        self.db_path = self.data_dir / "shampoo.db"

    async def _get_db(self) -> aiosqlite.Connection:
        """懒加载并初始化数据库连接，确保异步安全"""
        async with self._lock:
            if self.db is not None:
                return self.db
            
            # 异步环境下确保目录存在
            if not self.data_dir.exists():
                self.data_dir.mkdir(parents=True, exist_ok=True)
                
            try:
                # aiosqlite.connect 接受 Path 对象
                self.db = await aiosqlite.connect(self.db_path)
                # 开启 WAL 模式提高并发性能
                await self.db.execute("PRAGMA journal_mode=WAL;")
                await self.db.execute('''
                    CREATE TABLE IF NOT EXISTS shampoo_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        timestamp INTEGER,
                        formatted_time TEXT
                    )
                ''')
                await self.db.commit()
                logger.info(f"洗头追踪插件数据库初始化成功: {self.db_path}")
                return self.db
            except Exception as e:
                logger.error(f"洗头追踪插件数据库初始化异常: {type(e).__name__}: {str(e)}")
                return None

    @filter.command("洗头")
    async def record_shampoo(self, event: AstrMessageEvent):
        """记录一次洗头行为"""
        db = await self._get_db()
        if not db:
            yield event.plain_result("数据库连接失败，请检查插件后台日志。")
            return

        user_id = event.unified_msg_origin
        now_ts = int(time.time())
        dt_format = self.config.get("datetime_format", "%Y-%m-%d %H:%M:%S")
        now_str = datetime.datetime.fromtimestamp(now_ts).strftime(dt_format)

        # 连续洗头检查逻辑
        if self.config.get("enable_consecutive_check", True):
            threshold = self.config.get("duplicate_threshold_minutes", 30)
            try:
                async with db.execute(
                    "SELECT timestamp FROM shampoo_logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1", 
                    (user_id,)
                ) as cursor:
                    last_record = await cursor.fetchone()
                    if last_record and (now_ts - last_record[0]) < (threshold * 60):
                        yield event.plain_result(f"提示：您在 {threshold} 分钟内已有记录，请确认是否重复操作。")
            except Exception as e:
                logger.warning(f"重复检查执行失败: {e}")

        try:
            await db.execute(
                "INSERT INTO shampoo_logs (user_id, timestamp, formatted_time) VALUES (?, ?, ?)",
                (user_id, now_ts, now_str)
            )
            await db.commit()

            async with db.execute(
                "SELECT COUNT(*) FROM shampoo_logs WHERE user_id = ?", (user_id,)
            ) as cursor:
                count_row = await cursor.fetchone()
                count = count_row[0] if count_row else 1

            yield event.plain_result(f"已记录！当前时间：{now_str}。这是您的第 {count} 次洗头。")
        except Exception as e:
            logger.error(f"写入记录失败: {e}")
            yield event.plain_result("记录失败，数据库写入错误。")

    @filter.command("洗头情况")
    async def show_stats(self, event: AstrMessageEvent):
        """查看洗头历史记录和统计"""
        db = await self._get_db()
        if not db:
            yield event.plain_result("数据库服务不可用。")
            return

        user_id = event.unified_msg_origin
        max_display = self.config.get("max_history_display", 10)
        
        try:
            async with db.execute(
                "SELECT COUNT(*), MAX(formatted_time) FROM shampoo_logs WHERE user_id = ?", 
                (user_id,)
            ) as cursor:
                summary = await cursor.fetchone()
                total_count = summary[0] if summary else 0
                last_time = summary[1] if summary and summary[1] else "无记录"

            if total_count == 0:
                yield event.plain_result("您还没有洗头记录。发送 /洗头 开启第一条记录吧！")
                return

            tmpl = self.config.get("stats_summary_template", "统计报告：\n总共洗头 {count} 次。\n上次：{last_time}")
            resp_text = tmpl.format(count=total_count, last_time=last_time)
            
            actual_display = min(total_count, max_display)
            resp_text += f"\n\n最近 {actual_display} 条记录："
            
            async with db.execute(
                "SELECT formatted_time FROM shampoo_logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
                (user_id, max_display)
            ) as cursor:
                rows = await cursor.fetchall()
                for idx, row in enumerate(rows, 1):
                    resp_text += f"\n{idx}. {row[0]}"

            yield event.plain_result(resp_text)
        except Exception as e:
            logger.error(f"统计查询失败: {e}")
            yield event.plain_result("查询统计数据时发生错误。")

    def terminate(self):
        """
        插件卸载时的资源释放。
        考虑到异步环境，若 self.db 存在，尝试调度关闭任务。
        """
        if self.db:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.db.close())
                else:
                    # 如果 loop 已关闭，同步关闭可能不完全，但 aiosqlite 会随进程结束释放连接
                    pass
            except Exception as e:
                logger.debug(f"数据库关闭异常: {e}")
            finally:
                self.db = None
                logger.info("洗头追踪插件已释放数据库资源。")