#!/usr/bin/env python3
"""
AI产业+营销AI 每日新闻简报
每天采集昨日新闻，通过 DeepSeek 分类摘要，推送到微信

分类四象限（国内优先展示）：
  🏠 国内·AI产业  │  🏠 国内·营销AI
  ────────────────┼────────────────
  🌍 国际·AI产业  │  🌍 国际·营销AI

重要性分级：S(重大里程碑) > A(重要动态) > B(值得关注) > C(可选阅读)
配额制：S级无条件保留，A/B级按象限配额填充

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

CACHE_DIR = Path(__file__).parent / ".briefing_cache"
CACHE_DIR.mkdir(exist_ok=True)

# 各象限配额: (最小条数, 最大条数)
QUOTA_CONFIG = {
    "domestic_ai":           (3, 5),
    "domestic_marketing":    (2, 4),
    "international_ai":      (3, 5),
    "international_marketing": (2, 4),
}

# ============================================================
# RSS 订阅源（按象限分组）
# ============================================================

RSS_FEEDS = {
    # ===== 国内 · AI产业 =====
    "量子位":   "https://www.qbitai.com/feed",

    # ===== 国内 · 营销AI / 科技 =====
    "36氪 AI":  "https://36kr.com/feed?tagId=人工智能",

    # ===== 国际 · AI产业 =====
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge AI":   "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "MIT Tech Review": "https://www.technologyreview.com/feed/",

    # ===== 国际 · 营销AI =====
    "SearchEngineJournal": "https://www.searchenginejournal.com/feed/",
}

# ============================================================
# 工具函数
# ============================================================

def load_sent_cache() -> set:
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
    if len(hashes) > 5000:
        keep = list(hashes)[-3000:]
        cache_file.write_text(json.dumps(keep))


def url_hash(url: str) -> str:
    return hashlib.md5(url.strip().encode()).hexdigest()


def parse_published(entry) -> Optional[datetime]:
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
# DeepSeek 分类 + 摘要 + 分级
# ============================================================

CLASSIFY_PROMPT = """你是一位资深的AI产业+营销AI信息筛选专家。请对以下新闻列表执行四步分析。

═══════════════════════════════════════
第一步：AI相关性判定（宽松保留原则）
═══════════════════════════════════════

【核心原则】宁可多留，不要漏掉！边界模糊一律保留！

来源先验可信度（重要！）：
- 量子位、机器之心、36氪 是国内 AI/科技垂直媒体，它们发布的新闻默认与 AI 产业相关，
  除非内容明显是纯非科技类（如纯娱乐、纯体育），否则一律保留。
- TechCrunch AI、The Verge AI、MIT Tech Review、Adweek、Marketing AI Institute、SEJ
  均为 AI/科技/营销垂直源，同样适用宽松保留原则。

【保留标准 — 满足以下任意一条即保留】
✅ 明确涉及 AI/ML/DL/NLP/CV/语音/神经网络/强化学习/知识图谱技术本身
✅ 涉及大模型与生成式AI: LLM/GPT/Claude/Gemini/DeepSeek/通义千问/文心一言/Kimi/豆包/
   Mistral/Llama/多模态(AIGC)/Sora/Midjourney/DALL-E/Suno/Prompt工程/RAG/Function Calling/Agent
✅ 涉及 AI 产品与应用: AI Agent/Copilot/数字人/具身智能/自动驾驶/机器人/端侧AI/
   AI编程(Cursor/Devin)/AI搜索(Perplexity)/AI办公/AI客服/智能推荐
✅ 涉及 AI 基础设施: GPU/TPU/NPU/推理芯片/向量数据库/MLOps/数据标注/合成数据/云计算AI
✅ 涉及 AI 安全治理: Safety/Alignment/Ethics/Regulation/Deepfake检测/隐私保护/AI法规政策
✅ 涉及 AI 商业化: SaaS/API服务/融资并购估值/开源项目/半导体/算力租赁
✅ 国内 AI 公司（百度/阿里/字节/腾讯/华为/商汤/旷视/云从/科大讯飞/智谱/月之暗面/
   MiniMax/百川/零一万物等）的产品发布、融资、人事、战略调整、技术进展
✅ 海外 AI 公司（OpenAI/Anthropic/Google/Meta/xAI/Microsoft/NVIDIA 等）的相关动态
✅ 带有 AI 功能的消费级产品或企业级服务的发布、更新、评测（即使不是纯 AI 产品）
✅ AI 人才流动 / AI 实验室动态 / AI 开源社区重要事件

【营销AI专项】（同时涉及"AI"+"营销场景"即保留）
✅ AI广告投放优化 / AI内容生成(文案/图片/视频) / AI SEO / AI客户数据分析 /
   AI用户画像 / AI CRM / AI营销自动化 / AI电商推荐 / AI社媒运营工具 /
   AI客服聊天机器人(营销场景) / 数字人直播带货 / AI销售线索评分

【仅在以下情况 skip】⚠️ 极少使用，慎用！
❌ 与 AI/科技完全无关的内容（如纯娱乐八卦、纯体育赛事、纯时尚、纯美食，
   且标题和摘要中不包含任何 AI/智能/算法/模型 关键词）
❌ 纯金融股市行情播报（未提及任何 AI 公司或 AI 技术）

【边界案例处理规则 — 最高优先级】
⚠️⚠️⚠️ 当你犹豫是否要 skip 时，选择保留！
⚠️⚠️⚠️ 当一条新闻同时涉及 AI 和其他领域时，保留并归入最接近的象限！
⚠️⚠️⚠️ 来自垂直 AI 媒体源的新闻，默认保留，除非 100% 确定 与 AI 无关！
⚠️⚠️⚠️ 即使是"某公司发布新产品"，只要产品带 AI 功能，就保留！
⚠️⚠️⚠️ 即使是"某公司融资/人事/价格调整"，只要该公司是 AI 公司，就保留！

═══════════════════════════════════════
第二步：四象限分类
═══════════════════════════════════════

• domestic_ai         → 国内·AI产业（中国主体：大模型/芯片/基础设施/政策/融资/研究/开源）
• domestic_marketing  → 国内·营销AI（中国主体的AI+营销应用，见上方"营销AI专项"标准）
• international_ai    → 国际·AI产业（海外主体，标准同domestic_ai）
• international_marketing → 国际·营销AI（海外主体的AI+营销应用）

【来源优先判定 — 强信号】
- 量子位、机器之心、36氪 → 95%+ 概率归入 domestic_* 象限
- TechCrunch AI、The Verge AI → 倾向 international_ai
- Adweek、Marketing AI Institute、SEJ → 倾向 international_marketing
- MIT Tech Review → 内容混合，需逐条判断
- 判定逻辑：先看内容主题，再看来源归属；两者冲突时以内容为准，但来源是重要参考依据

═══════════════════════════════════════
第三步：重要性分级（核心！）
═══════════════════════════════════════

【S级 - 🔥重大里程碑】改变行业格局的事件
  例: 新旗舰模型发布(GPT-n/Claude-n/国产大模型代际跳跃)、国家层面AI政策法规、
      超10亿美元并购、算力芯片突破性进展、AGI相关重大宣布

【A级 - 重要动态】头部玩家关键动作
  例: 主要模型更新/能力提升、头部公司融资>1亿人民币或等值、重要开源项目发布、
      主要监管动作、大厂AI战略调整、权威评测榜单变化

【B级 - 值得关注】有价值的行业信息
  例: 中型公司产品发布/功能更新、重要技术论文/研究突破、行业报告关键数据、
      中型融资(1000万-1亿)、实用工具/框架更新、区域性市场动态

【C级 - 可选阅读】补充性内容
  例: 观点分析文章、访谈/播客要点、小版本迭代、教程/入门指南、趣味应用展示

═══════════════════════════════════════
第四步：配额筛选 + 摘要 + 导读
═══════════════════════════════════════

【配额规则】每个象限按以下优先级选取：
1. S级：全部保留（不设上限，确保大事不漏）
2. A级：最多取 3 条（按重要性排序）
3. B级：在 S+A 总数 < 该象限最小配额时补入，最多 2 条
4. C级：仅在所有象限 S+A+B 总数 < 12 时才考虑补入

各象限配额：
  domestic_ai:           min=3, max=5
  domestic_marketing:    min=2, max=4
  international_ai:      min=3, max=5
  international_marketing: min=2, max=4

全局总量控制：如果 S级总数 >= 8，启用"密集模式"——各象限只保留 S级 + A级第1名，
  并在导读中说明"今日为AI高密度日，已精选最关键的N条"

【摘要要求】
- 每条保留新闻写中文摘要，30-60字
- S级新闻摘要开头加 🔥 标记
- A级新闻突出核心数据和影响
- B/C级摘要简洁概括即可

【今日导读】80-120字：
1. 今日最重要的1-2条S级事件（如果有）
2. 各象限新闻分布情况
3. 一句话趋势判断

═══════════════════════════════════════
输出格式（严格JSON，不要markdown代码块）
═══════════════════════════════════════
{{
  "overview": "今日导读...",
  "mode": "normal",
  "domestic_ai": [
    {{"id": 0, "level": "S", "summary_cn": "🔥 摘要..."}}
  ],
  "domestic_marketing": [...],
  "international_ai": [...],
  "international_marketing": [...]
}}

注意：id对应输入新闻列表下标；level必须是S/A/B/C之一；skip的不出现。

═══════════════════════════════════════
新闻列表（共 N 条）
═══════════════════════════════════════
{news_json}

请输出JSON："""


def classify_and_summarize(news_list: list[dict]) -> dict:
    if not news_list:
        return {"overview": "今日无相关新闻。", "mode": "normal",
                "domestic_ai": [], "domestic_marketing": [],
                "international_ai": [], "international_marketing": []}

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    news_json = json.dumps(
        [{"id": i, "title": n["title"], "source": n["source"], "summary": n.get("summary", "")[:300]}
         for i, n in enumerate(news_list)],
        ensure_ascii=False,
    )

    try:
        log.info("  调用 DeepSeek 进行分类和摘要...")
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你是专业的AI产业分析师。请严格按JSON格式输出，不要添加markdown代码块标记。"},
                {"role": "user", "content": CLASSIFY_PROMPT.format(news_json=news_json)},
            ],
            temperature=0.5,
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
    cn_sources = {"机器之心", "量子位", "36氪"}
    marketing_sources = {"Adweek AI", "Marketing AI Institute", "SearchEngineJournal"}

    result = {
        "overview": f"今日共采集 {len(news_list)} 条AI相关新闻（自动分类模式）。",
        "mode": "normal",
        "domestic_ai": [], "domestic_marketing": [],
        "international_ai": [], "international_marketing": [],
    }
    for i, n in enumerate(news_list):
        is_cn = n["source"] in cn_sources
        is_mkt = n["source"] in marketing_sources
        key = ("domestic_" if is_cn else "international_") + ("marketing" if is_mkt else "ai")
        result[key].append({"id": i, "level": "B", "summary_cn": ""})
    return result


def merge_results(news_list: list[dict], llm_result: dict) -> dict:
    id_map = {i: n for i, n in enumerate(news_list)}

    def fill(quadrant_key):
        raw_items = llm_result.get(quadrant_key, [])
        items = []
        for item in raw_items:
            n = id_map.get(item["id"])
            if n:
                items.append({
                    **n,
                    "summary_cn": item.get("summary_cn", ""),
                    "level": item.get("level", "B"),
                })

        level_order = {"S": 0, "A": 1, "B": 2, "C": 3}
        items.sort(key=lambda x: level_order.get(x["level"], 9))

        s_items = [x for x in items if x["level"] == "S"]
        non_s = [x for x in items if x["level"] != "S"]
        min_q, max_q = QUOTA_CONFIG.get(quadrant_key, (2, 5))

        if len(s_items) <= max_q:
            remaining = max(0, min_q - len(s_items))
            result = s_items + non_s[:max(remaining, max_q - len(s_items))]
        else:
            result = s_items + non_s[:2]

        return result

    return {
        "overview": llm_result.get("overview", ""),
        "mode": llm_result.get("mode", "normal"),
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
        ("🏠 国内 · 营销AI", "domestic_marketing", "#10b981"),
        ("🌍 国际 · AI产业", "international_ai", "#8b5cf6"),
        ("🌍 国际 · 营销AI", "international_marketing", "#f59e0b"),
    ]

    total = sum(len(result[k]) for k in ["domestic_ai", "domestic_marketing",
                                           "international_ai", "international_marketing"])
    mode_tag = ""
    if result.get("mode") == "dense":
        mode_tag = '<span style="background:#ef4444;color:#fff;font-size:11px;padding:2px 8px;border-radius:4px;margin-left:8px">高密度日</span>'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;padding:16px;background:#f5f5f5">
<div style="background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.1)">

<div style="text-align:center;margin-bottom:20px">
  <div style="font-size:22px;font-weight:700;color:#1a1a2e">📰 AI产业+营销AI 日报</div>
  <div style="font-size:14px;color:#888;margin-top:4px">📅 {date_label} 周{weekday}{mode_tag}</div>
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
            level = n.get("level", "B")
            is_hot = level == "S"

            if is_hot:
                bg_color = "#fffbeb"
                border_l = "#f59e0b"
                hot_badge = '<span style="background:#ef4444;color:#fff;font-size:11px;padding:1px 6px;border-radius:4px;margin-left:6px">重磅</span>'
            else:
                bg_color = "#fafafa"
                border_l = "#e5e7eb"
                hot_badge = ""

            html += f"""
  <div style="margin-bottom:12px;padding:12px;background:{bg_color};border-radius:8px;border-left:3px solid {border_l}">
    <div style="font-size:14px;font-weight:600;color:#222;line-height:1.5">{i}. {n['title']}{hot_badge}</div>"""
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
📊 共 {total} 条 · DeepSeek 智能分级摘要 · 每日9:00自动推送
</div>

</div></body></html>"""

    return html


def format_briefing_text(result: dict, target_date: datetime) -> str:
    date_label = target_date.strftime("%Y年%m月%d日")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][target_date.weekday()]
    mode_suffix = " [高密度日]" if result.get("mode") == "dense" else ""

    lines = [
        f"📰 AI产业+营销AI 日报",
        f"📅 {date_label} 周{weekday}{mode_suffix}",
        f"",
        f"━━━ 📊 今日导读 ━━━",
        f"{result['overview']}",
        f"",
    ]

    quadrants = [
        ("🏠 国内 · AI产业", "domestic_ai"),
        ("🏠 国内 · 营销AI", "domestic_marketing"),
        ("🌍 国际 · AI产业", "international_ai"),
        ("🌍 国际 · 营销AI", "international_marketing"),
    ]

    for section_title, key in quadrants:
        items = result[key]
        if not items:
            continue
        lines.append(f"━━━ {section_title} ━━━")
        for i, n in enumerate(items, 1):
            summary = n.get("summary_cn", "")
            level = n.get("level", "B")
            level_tag = "🔥" if level == "S" else ""
            title_prefix = f"{level_tag} " if level_tag else ""
            lines.append(f"{i}. {title_prefix}{n['title']}")
            if summary:
                lines.append(f"   📝 {summary}")
            lines.append(f"   🔗 {n['url']}")
            lines.append(f"   📍 来源: {n['source']}")
        lines.append("")

    total = sum(len(result[k]) for k in ["domestic_ai", "domestic_marketing",
                                           "international_ai", "international_marketing"])
    lines.append(f"📊 共 {total} 条 | DeepSeek S/A/B/C分级 | 每日9:00自动推送")

    return "\n".join(lines)


# ============================================================
# 推送
# ============================================================

def push_wechat(html_content: str, text_content: str) -> bool:
    if PUSHPLUS_TOKEN:
        return _push_pushplus(html_content)
    if SERVERCHAN_SENDKEY:
        log.warning("  PushPlus 未配置，降级使用 Server酱")
        return _push_serverchan(text_content)
    log.warning("  未配置推送Token，跳过推送")
    return False


def _push_pushplus(html_content: str) -> bool:
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

    log.info("=" * 50)
    log.info("Step 1/5: 采集RSS新闻...")
    all_news = []
    for d in range(args.days):
        day = target_date - timedelta(days=d)
        all_news.extend(fetch_rss_news(day))
    log.info(f"  采集到 {len(all_news)} 条原始新闻")

    log.info("Step 2/5: 去重...")
    sent_cache = load_sent_cache()
    unique_news = deduplicate_news(all_news, sent_cache)
    log.info(f"  去重后剩余 {len(unique_news)} 条")

    if not unique_news:
        log.warning("  无新增新闻，退出")
        return

    if len(unique_news) > 60:
        unique_news = unique_news[:60]

    log.info("Step 3/5: DeepSeek 分类+分级+摘要...")
    result = classify_and_summarize(unique_news)
    total_classified = sum(len(result[k]) for k in
                           ["domestic_ai", "domestic_marketing",
                            "international_ai", "international_marketing"])

    s_count = sum(1 for k in ["domestic_ai","domestic_marketing","international_ai","international_marketing"]
                  for n in result[k] if n.get("level")=="S")
    log.info(f"  分类完成: 总{total_classified}条 (S级{s_count}条), "
             f"国内AI={len(result['domestic_ai'])}, "
             f"国内营销={len(result['domestic_marketing'])}, "
             f"国际AI={len(result['international_ai'])}, "
             f"国际营销={len(result['international_marketing'])}")

    log.info("Step 4/5: 格式化简报...")
    html_content = format_briefing_html(result, target_date)
    text_content = format_briefing_text(result, target_date)
    save_local_copy(text_content, target_date)

    if args.output:
        print("\n" + "=" * 50)
        print(text_content)

    if not args.dry_run:
        log.info("Step 5/5: 推送到微信...")
        push_wechat(html_content, text_content)

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
