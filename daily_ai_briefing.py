#!/usr/bin/env python3
"""
AI产业+营销AI 每日新闻简报
每天采集昨日新闻，通过 DeepSeek 分类摘要，推送到微信

分类四象限：
  🏠 国内·AI产业  │  🌍 国际·AI产业
  ────────────────┼────────────────
  🏠 国内·营销AI  │  🌍 国际·营销AI

使用方式：
  python daily_ai_briefing.py          # 采集昨天新闻
  python daily_ai_briefing.py --days 2 # 采集过去2天新闻（首次运行用）
"""

import os
import sys
import json
import time
import hashlib
import logging
import argparse
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path

import requests
import feedparser
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daily_briefing")

# ============================================================
# 配置
# ============================================================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

MAX_NEWS_PER_QUADRANT = 5
MAX_TOTAL_NEWS = 16

CACHE_DIR = Path(__file__).parent / ".briefing_cache"
CACHE_DIR.mkdir(exist_ok=True)

# ============================================================
# RSS 订阅源
# ============================================================

RSS_FEEDS = {
    # --- AI产业 国际 ---
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "MIT Tech Review": "https://www.technologyreview.com/feed/",
    "ArXiv AI": "https://rss.arxiv.org/rss/cs.AI",
    # --- AI产业 国内 ---
    "机器之心": "https://www.jiqizhixin.com/rss",
    "量子位": "https://www.qbitai.com/feed",
    # --- 营销AI 国际 ---
    "Martech.org": "https://martech.org/feed/",
    "AdExchanger": "https://www.adexchanger.com/feed/",
    "SearchEngineJournal": "https://www.searchenginejournal.com/feed/",
    # --- 营销AI 国内 ---
    "梅花网": "https://www.meihua.info/feed",
}

# ============================================================
# 工具函数
# ============================================================

def load_sent_cache() -> set:
    """加载已发送新闻的 URL hash，避免重复推送"""
    cache_file = CACHE_DIR / "sent_hashes.json"
    if cache_file.exists():
        try:
            return set(json.loads(cache_file.read_text()))
        except (json.JSONDecodeError, IOError):
            return set()
    return set()


def save_sent_cache(hashes: set):
    cache_file = CACHE_DIR / "sent_hashes.json"
    cache_file.write_text(json.dumps(list(hashes)))
    # 清理15天前的记录，防止无限增长
    if len(hashes) > 5000:
        keep = list(hashes)[-3000:]
        cache_file.write_text(json.dumps(keep))


def url_hash(url: str) -> str:
    return hashlib.md5(url.strip().encode()).hexdigest()


def parse_published(entry) -> Optional[datetime]:
    """解析 RSS entry 的发布时间"""
    for attr in ("published_parsed", "updated_parsed"):
        tp = getattr(entry, attr, None)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


# ============================================================
# 新闻采集
# ============================================================

def fetch_rss_news(target_date: datetime) -> list[dict]:
    """从所有 RSS 源采集指定日期的新闻"""
    all_entries = []
    target_str = date_str(target_date)

    for source_name, feed_url in RSS_FEEDS.items():
        try:
            log.info(f"  采集 RSS: {source_name}")
            feed = feedparser.parse(feed_url)
            if feed.bozo:
                log.warning(f"    ⚠ 解析警告: {feed.bozo_exception}")

            for entry in feed.entries:
                pub_date = parse_published(entry)
                if pub_date and date_str(pub_date) != target_str:
                    continue

                link = entry.get("link", "")
                if not link:
                    continue

                all_entries.append({
                    "title": entry.get("title", "").strip(),
                    "url": link.strip(),
                    "source": source_name,
                    "summary": (entry.get("summary", "") or entry.get("description", ""))[:500].strip(),
                    "published": pub_date.isoformat() if pub_date else "",
                })
            log.info(f"    ✓ {source_name}: 匹配 {sum(1 for e in all_entries if e['source'] == source_name)} 条")

        except Exception as e:
            log.error(f"    ✗ {source_name}: {e}")

    return all_entries


def deduplicate_news(entries: list[dict], sent_cache: set) -> list[dict]:
    """去重：按 URL hash 去重 + 排除已发送"""
    seen = set()
    unique = []
    for e in entries:
        h = url_hash(e["url"])
        if h in seen or h in sent_cache:
            continue
        seen.add(h)
        unique.append(e)
    return unique


# ============================================================
# DeepSeek 分类 + 摘要
# ============================================================

def classify_and_summarize(news_list: list[dict]) -> dict:
    """
    用 DeepSeek 对新闻进行四象限分类并生成中文摘要。
    返回:
    {
        "overview": "今日导读",
        "domestic_ai": [{title, url, source, summary}],
        "domestic_marketing": [...],
        "international_ai": [...],
        "international_marketing": [...],
    }
    """
    if not news_list:
        return {"overview": "今日无相关新闻。", "domestic_ai": [], "domestic_marketing": [],
                "international_ai": [], "international_marketing": []}

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    news_json = json.dumps(
        [{"id": i, "title": n["title"], "source": n["source"], "summary": n.get("summary", "")[:300]}
         for i, n in enumerate(news_list)],
        ensure_ascii=False,
    )

    prompt = f"""你是一位专业的AI产业分析师。请分析以下新闻列表，完成三个任务：

## 任务1：四象限分类
将每条新闻分入以下四个类别之一：
- domestic_ai: 国内AI产业（中国公司/机构的AI技术、大模型、芯片、政策等）
- domestic_marketing: 国内营销AI（中国市场的AI营销、广告、内容生成应用）
- international_ai: 国际AI产业（海外公司/机构的AI技术、大模型、芯片、政策等）
- international_marketing: 国际营销AI（海外市场的AI营销、广告、内容生成应用）

分类规则：
- 涉及中国公司/市场/政策的 → 国内(domestic)
- 涉及海外公司/市场/政策的 → 国际(international)
- 涉及大模型、芯片、AI基础设施、AI政策、AI融资、技术突破 → AI产业(ai)
- 涉及营销、广告投放、内容生成、SEO、客户洞察、Martech → 营销AI(marketing)
- 如果一条新闻同时涉及两个维度，按主要内容归类

## 任务2：生成中文摘要
为每条新闻写一个简洁的中文摘要（30-60字），突出核心信息。

## 任务3：生成今日导读
写一段80-120字的"今日导读"，概括今日新闻的整体趋势和亮点。

## 输出格式（严格JSON，不要markdown代码块）：
{{
  "overview": "今日导读文字...",
  "domestic_ai": [
    {{"id": 0, "summary_cn": "中文摘要"}}
  ],
  "domestic_marketing": [...],
  "international_ai": [...],
  "international_marketing": [...]
}}

## 新闻列表：
{news_json}

请输出JSON："""

    try:
        log.info("  调用 DeepSeek 进行分类和摘要...")
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你是专业的AI产业分析师。请严格按JSON格式输出，不要添加markdown代码块标记。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"  DeepSeek 返回非JSON格式: {e}")
        log.debug(f"  原始返回: {raw[:500]}")
        return merge_results(news_list, fallback_classify(news_list))
    except Exception as e:
        log.error(f"  DeepSeek 调用失败: {e}")
        return merge_results(news_list, fallback_classify(news_list))

    return merge_results(news_list, result)


def fallback_classify(news_list: list[dict]) -> dict:
    """当 DeepSeek 失败时，按来源做简易分类"""
    cn_sources = {"机器之心", "量子位", "梅花网", "36氪"}
    marketing_sources = {"Martech.org", "AdExchanger", "SearchEngineJournal", "梅花网"}

    result = {
        "overview": f"今日共采集 {len(news_list)} 条AI相关新闻（自动分类模式）。",
        "domestic_ai": [], "domestic_marketing": [],
        "international_ai": [], "international_marketing": [],
    }
    for i, n in enumerate(news_list):
        is_cn = n["source"] in cn_sources
        is_mkt = n["source"] in marketing_sources
        key = ("domestic_" if is_cn else "international_") + ("marketing" if is_mkt else "ai")
        result[key].append({"id": i, "summary_cn": ""})
    return result


def merge_results(news_list: list[dict], llm_result: dict) -> dict:
    """将LLM结果与原始新闻数据合并"""
    id_map = {i: n for i, n in enumerate(news_list)}

    def fill(quadrant_key):
        items = []
        for item in llm_result.get(quadrant_key, [])[:MAX_NEWS_PER_QUADRANT]:
            n = id_map.get(item["id"])
            if n:
                items.append({**n, "summary_cn": item.get("summary_cn", "")})
        return items

    return {
        "overview": llm_result.get("overview", ""),
        "domestic_ai": fill("domestic_ai"),
        "domestic_marketing": fill("domestic_marketing"),
        "international_ai": fill("international_ai"),
        "international_marketing": fill("international_marketing"),
    }


# ============================================================
# 格式化输出
# ============================================================

def format_briefing_html(result: dict, target_date: datetime) -> str:
    date_label = target_date.strftime("%Y年%m月%d日")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][target_date.weekday()]

    quadrants = [
        ("🏠 国内 · AI产业", "domestic_ai", "#3b82f6"),
        ("🌍 国际 · AI产业", "international_ai", "#8b5cf6"),
        ("🏠 国内 · 营销AI", "domestic_marketing", "#10b981"),
        ("🌍 国际 · 营销AI", "international_marketing", "#f59e0b"),
    ]

    total = sum(len(result[k]) for k in ["domestic_ai", "domestic_marketing",
                                           "international_ai", "international_marketing"])

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;padding:16px;background:#f5f5f5">
<div style="background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.1)">

<div style="text-align:center;margin-bottom:20px">
  <div style="font-size:22px;font-weight:700;color:#1a1a2e">📰 AI产业+营销AI 日报</div>
  <div style="font-size:14px;color:#888;margin-top:4px">📅 {date_label} 周{weekday}</div>
</div>

<div style="background:#f0f7ff;border-radius:8px;padding:14px 16px;margin-bottom:20px;border-left:4px solid #3b82f6">
  <div style="font-size:13px;font-weight:700;color:#3b82f6;margin-bottom:6px">📊 今日导读</div>
  <div style="font-size:14px;color:#333;line-height:1.7">{result['overview']}</div>
</div>"""

    for section_title, key, color in quadrants:
        items = result[key]
        if not items:
            continue
        html += f"""
<div style="margin-bottom:18px">
  <div style="font-size:15px;font-weight:700;color:{color};border-bottom:2px solid {color};padding-bottom:4px;margin-bottom:10px">{section_title}</div>"""

        for i, n in enumerate(items, 1):
            summary = n.get("summary_cn", "")
            html += f"""
  <div style="margin-bottom:12px;padding:12px;background:#fafafa;border-radius:8px">
    <div style="font-size:14px;font-weight:600;color:#222;line-height:1.5">{i}. {n['title']}</div>"""
            if summary:
                html += f"""
    <div style="font-size:13px;color:#555;margin-top:4px;line-height:1.5">📝 {summary}</div>"""
            html += f"""
    <div style="font-size:12px;color:#999;margin-top:6px">
      <a href="{n['url']}" style="color:#3b82f6;text-decoration:none">🔗 阅读原文</a>
      &nbsp;·&nbsp; 📍 {n['source']}
    </div>
  </div>"""

        html += "</div>"

    html += f"""
<div style="text-align:center;font-size:12px;color:#aaa;margin-top:20px;padding-top:16px;border-top:1px solid #eee">
📊 共 {total} 条 · DeepSeek 智能摘要 · 每日9:00自动推送
</div>

</div></body></html>"""

    return html


def format_briefing_text(result: dict, target_date: datetime) -> str:
    """纯文本版本（终端输出用）"""
    date_label = target_date.strftime("%Y年%m月%d日")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][target_date.weekday()]

    lines = [
        f"📰 AI产业+营销AI 日报",
        f"📅 {date_label} 周{weekday}",
        f"",
        f"━━━ 📊 今日导读 ━━━",
        f"{result['overview']}",
        f"",
    ]

    quadrants = [
        ("🏠 国内 · AI产业", "domestic_ai"),
        ("🌍 国际 · AI产业", "international_ai"),
        ("🏠 国内 · 营销AI", "domestic_marketing"),
        ("🌍 国际 · 营销AI", "international_marketing"),
    ]

    for section_title, key in quadrants:
        items = result[key]
        if not items:
            continue
        lines.append(f"━━━ {section_title} ━━━")
        for i, n in enumerate(items, 1):
            summary = n.get("summary_cn", "")
            lines.append(f"{i}. {n['title']}")
            if summary:
                lines.append(f"   📝 {summary}")
            lines.append(f"   🔗 {n['url']}")
            lines.append(f"   📍 来源: {n['source']}")
        lines.append("")

    total = sum(len(result[k]) for k in ["domestic_ai", "domestic_marketing",
                                           "international_ai", "international_marketing"])
    lines.append(f"📊 共 {total} 条 | DeepSeek 摘要 | 每日9:00自动推送")

    return "\n".join(lines)


# ============================================================
# 推送
# ============================================================

def push_wechat(html_content: str, text_content: str) -> bool:
    """推送简报到微信：优先 PushPlus HTML，fallback Server酱纯文本"""
    if PUSHPLUS_TOKEN:
        return _push_pushplus(html_content)
    if SERVERCHAN_SENDKEY:
        log.warning("  PushPlus 未配置，降级使用 Server酱（排版可能不佳）")
        return _push_serverchan(text_content)
    log.warning("  未配置 PUSHPLUS_TOKEN 或 SERVERCHAN_SENDKEY，跳过推送")
    return False


def _push_pushplus(html_content: str) -> bool:
    """PushPlus 推送（HTML 格式，排版精美）"""
    try:
        resp = requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PUSHPLUS_TOKEN,
                "title": "📰 AI产业+营销AI 日报",
                "content": html_content,
                "template": "html",
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 200:
            log.info("  ✓ PushPlus 推送成功")
            return True
        else:
            log.error(f"  ✗ PushPlus 失败: {data}")
            return False
    except Exception as e:
        log.error(f"  ✗ PushPlus 异常: {e}")
        return False


def _push_serverchan(text_content: str) -> bool:
    """Server酱 推送（纯文本，备用方案）"""
    max_len = 2000
    chunks = []
    current = ""
    for line in text_content.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current += ("\n" + line) if current else line
    if current:
        chunks.append(current)

    success = True
    for i, chunk in enumerate(chunks):
        title = f"AI日报 (第{i+1}/{len(chunks)}页)" if len(chunks) > 1 else "AI日报"
        try:
            resp = requests.post(
                f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send",
                data={"title": title, "desp": chunk},
                timeout=15,
            )
            if resp.status_code == 200:
                log.info(f"  ✓ Server酱推送成功 ({i+1}/{len(chunks)})")
            else:
                log.error(f"  ✗ 推送失败: {resp.status_code} {resp.text[:200]}")
                success = False
        except Exception as e:
            log.error(f"  ✗ 推送异常: {e}")
            success = False

    return success


def save_local_copy(content: str, target_date: datetime):
    """保存本地备份"""
    backup_dir = CACHE_DIR / "history"
    backup_dir.mkdir(exist_ok=True)
    filename = backup_dir / f"briefing_{date_str(target_date)}.txt"
    filename.write_text(content, encoding="utf-8")
    log.info(f"  本地备份: {filename}")


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="AI产业+营销AI 每日新闻简报")
    parser.add_argument("--days", type=int, default=1, help="采集过去N天的新闻（默认1天=昨天）")
    parser.add_argument("--dry-run", action="store_true", help="仅采集分类，不推送")
    parser.add_argument("--output", action="store_true", help="输出到终端")
    args = parser.parse_args()

    if not DEEPSEEK_API_KEY:
        log.error("请在 .env 文件中配置 DEEPSEEK_API_KEY")
        sys.exit(1)

    target_date = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)

    log.info(f"目标日期: {date_str(target_date)} (过去{args.days}天)")

    # Step 1: 采集新闻
    log.info("=" * 50)
    log.info("Step 1/5: 采集RSS新闻...")
    all_news = []
    for d in range(args.days):
        day = target_date - timedelta(days=d)
        all_news.extend(fetch_rss_news(day))
    log.info(f"  采集到 {len(all_news)} 条原始新闻")

    # Step 2: 去重
    log.info("Step 2/5: 去重...")
    sent_cache = load_sent_cache()
    unique_news = deduplicate_news(all_news, sent_cache)
    log.info(f"  去重后剩余 {len(unique_news)} 条")

    if not unique_news:
        log.warning("  无新增新闻，退出")
        return

    # 限制总数，避免 token 超限
    if len(unique_news) > 40:
        unique_news = unique_news[:40]

    # Step 3: DeepSeek 分类+摘要
    log.info("Step 3/5: DeepSeek 分类+摘要...")
    result = classify_and_summarize(unique_news)
    total_classified = sum(len(result[k]) for k in
                           ["domestic_ai", "domestic_marketing",
                            "international_ai", "international_marketing"])
    log.info(f"  分类完成: 国内AI={len(result['domestic_ai'])}条, "
             f"国内营销={len(result['domestic_marketing'])}条, "
             f"国际AI={len(result['international_ai'])}条, "
             f"国际营销={len(result['international_marketing'])}条")

    # Step 4: 格式化
    log.info("Step 4/5: 格式化简报...")
    html_content = format_briefing_html(result, target_date)
    text_content = format_briefing_text(result, target_date)
    save_local_copy(text_content, target_date)

    if args.output:
        print("\n" + "=" * 50)
        print(text_content)

    # Step 5: 推送
    if not args.dry_run:
        log.info("Step 5/5: 推送到微信...")
        push_wechat(html_content, text_content)

        # 记录已发送
        for quadrant in ["domestic_ai", "domestic_marketing",
                         "international_ai", "international_marketing"]:
            for n in result[quadrant]:
                sent_cache.add(url_hash(n["url"]))
        save_sent_cache(sent_cache)
    else:
        log.info("Step 5/5: DRY RUN 模式，跳过推送")

    log.info("✅ 完成!")


if __name__ == "__main__":
    main()