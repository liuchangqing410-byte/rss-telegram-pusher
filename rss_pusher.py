import feedparser
import requests
import logging
import asyncio
import json
import os
import re
from telegram import Bot
from telegram.error import TelegramError

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
RSS_URL = os.getenv("RSS_URL")
POSTS_FILE = "sent_posts.json"
MAX_PUSH_PER_RUN = 5  # 单次最多推送5条

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def translate_text(text):
    """使用 Google 翻译 API 将英文翻译成中文"""
    try:
        # 只翻译英文标题（避免重复翻译）
        if not text or len(text.strip()) < 2:
            return text
        
        # 检测是否包含中文字符，如果有则直接返回
        if re.search(r'[\u4e00-\u9fff]', text):
            return text
        
        # 使用 Google 翻译（免费接口）
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            'client': 'gtx',
            'sl': 'en',      # 源语言：英文
            'tl': 'zh-CN',   # 目标语言：简体中文
            'dt': 't',
            'q': text
        }
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            # 解析翻译结果
            translated = ''.join([part[0] for part in result[0] if part[0]])
            logging.info(f"翻译成功：{text[:30]}... → {translated[:30]}...")
            return translated
        else:
            logging.warning(f"翻译失败，状态码：{response.status_code}")
            return text
    except Exception as e:
        logging.warning(f"翻译出错：{str(e)}，返回原文")
        return text

def load_sent_posts():
    try:
        if os.path.exists(POSTS_FILE):
            with open(POSTS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        logging.info("首次运行，创建空ID列表")
        return []
    except Exception as e:
        logging.error(f"读取已发送ID失败：{str(e)}")
        return []

def save_sent_posts(post_ids):
    try:
        with open(POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(post_ids, f, ensure_ascii=False, indent=2)
        logging.info(f"已保存ID列表（共{len(post_ids)}条）")
    except Exception as e:
        logging.error(f"保存已发送ID失败：{str(e)}")

def fetch_updates():
    try:
        logging.info(f"获取RSS源：{RSS_URL}")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': 'application/rss+xml, application/xml, text/xml, */*'
        }
        response = requests.get(RSS_URL, headers=headers, timeout=30)
        response.encoding = 'utf-8'

        if response.status_code != 200:
            logging.error(f"HTTP请求失败，状态码：{response.status_code}")
            return None

        feed = feedparser.parse(response.text)

        if feed.bozo:
            logging.warning(f"RSS解析警告：{feed.bozo_exception}")
            if not feed.entries:
                logging.error("解析后无任何条目")
                return None

        logging.info(f"成功获取 {len(feed.entries)} 条 RSS 条目")
        return feed
    except requests.exceptions.RequestException as e:
        logging.error(f"网络请求失败：{str(e)}")
        return None
    except Exception as e:
        logging.error(f"获取RSS失败：{str(e)}")
        return None

def escape_markdown(text):
    special_chars = r"_*~`>#+-.!()"
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text

async def send_message(bot, title, link, delay=3):
    try:
        await asyncio.sleep(delay)
        
        # 翻译标题
        translated_title = translate_text(title)
        
        escaped_title = escape_markdown(translated_title)
        escaped_link = escape_markdown(link)
        
        # 如果翻译后的标题和原标题不同，显示双语
        if translated_title != title:
            message = f"📦 **{escaped_title}**\n🔗 {escaped_link}\n📖 原文：`{escape_markdown(title[:50])}...`"
        else:
            message = f"📦 **{escaped_title}**\n🔗 {escaped_link}"
        
        logging.info(f"发送消息：{message[:100]}")
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode="MarkdownV2"
        )
        logging.info("消息发送成功")
        return True
    except TelegramError as e:
        logging.error(f"Telegram发送失败：{str(e)}")
        return False

async def check_for_updates(sent_post_ids):
    updates = fetch_updates()
    if not updates:
        return

    new_posts = []
    for entry in updates.entries:
        try:
            # 提取ID（适配URL格式）
            guid_parts = entry.guid.split("-")
            if len(guid_parts) < 2:
                logging.warning(f"无效GUID格式：{entry.guid}，跳过")
                continue
            post_id = guid_parts[-1].split(".")[0]
            if not post_id.isdigit():
                logging.warning(f"提取的ID非数字：{post_id}，跳过")
                continue
            logging.info(f"解析到有效ID：{post_id}，标题：{entry.title[:20]}...")
            if post_id not in sent_post_ids:
                new_posts.append((post_id, entry.title, entry.link))
        except Exception as e:
            logging.error(f"解析条目失败（GUID：{entry.guid}）：{str(e)}")
            continue

    if new_posts:
        new_posts.sort(key=lambda x: int(x[0]))
        new_posts = new_posts[:MAX_PUSH_PER_RUN]
        logging.info(f"发现{len(new_posts)}条新帖子（单次最多推{MAX_PUSH_PER_RUN}条），准备依次推送（间隔3秒）")

        async with Bot(token=TELEGRAM_TOKEN) as bot:
            for i, (post_id, title, link) in enumerate(new_posts):
                success = await send_message(bot, title, link, delay=3 if i > 0 else 0)
                if success:
                    sent_post_ids.append(post_id)

        save_sent_posts(sent_post_ids)
    else:
        logging.info("无新帖子需要推送")

async def main():
    logging.info("===== 脚本开始运行 =====")
    sent_post_ids = load_sent_posts()
    try:
        await check_for_updates(sent_post_ids)
    except Exception as e:
        logging.error(f"主逻辑执行失败：{str(e)}")
    logging.info("===== 脚本运行结束 =====")

if __name__ == "__main__":
    asyncio.run(main())
