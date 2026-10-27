import asyncio
import csv
import difflib
import json
import re
import sqlite3
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import nest_asyncio
import plotly.express as px
from plotly.subplots import make_subplots
from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, Route, async_playwright
import unicodedata

from playwright.async_api import Page

# ================================
# 常數與預編譯正規表達式
# ================================
VIEWSHOW_API_URL = "https://www.vscinemas.com.tw/ShowTimes/ShowTimes/GetShowTimes"
EXCLUDE_KEYWORDS = {"PRE", "TBC", "LIVE", "SUPPORT"}

RE_BRACKET_CONTENT = re.compile(r"\((.*?)\)")
RE_LEADING_BRACKET = re.compile(r"^(\([^)]*\)\s*)+")
RE_TRAILING_BRACKET = re.compile(r"(\s*\([^)]*\))+$")
RE_RELEASE_OR_RESTORED = re.compile(r"\s*\S*(?:修復版|修復|重映)")
RE_MULTIPLE_SPACES = re.compile(r"\s+")


@dataclass(slots=True)
class Movie:
    zh_title: str
    en_title: str

# ================================
# 工具函式
# ================================
def should_exclude(en_title: str) -> bool:
    for content in RE_BRACKET_CONTENT.findall(en_title):
        if any(kw in content.upper() for kw in EXCLUDE_KEYWORDS):
            return True
    return False


def clean_title(title: str) -> str:
    title = RE_LEADING_BRACKET.sub("", title)
    title = RE_TRAILING_BRACKET.sub("", title)
    title = RE_RELEASE_OR_RESTORED.sub("", title)
    return RE_MULTIPLE_SPACES.sub(" ", title).strip()


def calculate_similarity(s1: str, s2: str) -> float:
    c1 = RE_MULTIPLE_SPACES.sub(" ", s1.lower()).strip()
    c2 = RE_MULTIPLE_SPACES.sub(" ", s2.lower()).strip()
    if c1 == c2:
        return 1.0
    return difflib.SequenceMatcher(None, c1, c2).ratio()


# ================================
# Playwright 資源攔截與驗證碼處理
# ================================
async def block_heavy_resources(route: Route) -> None:
    req = route.request
    res_type = req.resource_type
    # 保留 stylesheet，避免 IMDb 人機驗證頁的手動操作介面因缺少 CSS 而無法正常顯示。
    # 僅封鎖媒體與字型，降低不必要的資源載入。
    if res_type in ["media", "font"]:
        await route.abort()
    elif res_type == "image" and not any(k in req.url.lower() for k in ["captcha", "geetest", "aws"]):
        await route.abort()
    else:
        await route.continue_()


async def _is_imdb_captcha_success_redirect(page: Page) -> bool:
    """辨識驗證成功後、IMDb 尚在轉址中的過渡畫面。"""
    try:
        body_text = (await page.locator("body").inner_text(timeout=1_500)).casefold()
    except Exception:
        body_text = ""

    success_markers = (
        "即将为您重定向",
        "即將為您重新導向",
        "成功！即将为您重定向",
        "成功! 即将为您重定向",
        "success! you will be redirected",
        "success, you will be redirected",
        "you will be redirected shortly",
    )
    return any(marker.casefold() in body_text for marker in success_markers)


async def _is_imdb_captcha_visible(page: Page) -> bool:
    """檢查目前頁面是否仍停留在 IMDb 人機驗證畫面。"""
    # 驗證成功後可能短暫顯示「成功！即將為您重定向」，即使 title
    # 仍是 Human Verification，也應讓驗證流程結束，接著由搜尋流程重送原網址。
    if await _is_imdb_captcha_success_redirect(page):
        return False

    try:
        title = (await page.title()).casefold()
    except Exception:
        title = ""

    title_markers = (
        "human verification",
        "robot check",
        "security check",
    )
    if any(marker in title for marker in title_markers):
        return True

    challenge_selectors = (
        "form#geetest-form",
        ".aws-captcha",
        "#captcha",
        'iframe[src*="captcha"]',
        'form[action*="captcha"]',
        '[data-testid*="captcha"]',
    )
    for selector in challenge_selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                return True
        except Exception:
            continue

    try:
        body_text = (await page.locator("body").inner_text(timeout=1_500)).casefold()
    except Exception:
        body_text = ""

    body_markers = (
        "verify you are human",
        "verify that you are human",
        "prove you are not a robot",
        "prove you're not a robot",
        "complete the security check",
        "automated requests",
        "human verification",
        "robot check",
        "我们需要确认您是人类",
        "在继续操作之前，请完成安全检查",
        "此步骤将验证您不是机器人",
        "完成安全检查",
        "请完成安全检查",
    )
    return any(marker.casefold() in body_text for marker in body_markers)


async def handle_captcha_if_needed(page: Page) -> bool:
    """若 IMDb 出現人機驗證，等待使用者在瀏覽器中手動完成後才繼續。"""
    try:
        detected = await _is_imdb_captcha_visible(page)
        if not detected:
            return False

        print(
            "\n  [!] ⚠️ 偵測到 IMDb 機器人驗證！"
            "請在目前開啟的瀏覽器視窗中手動完成驗證；程式會持續等待，驗證頁完全消失後才繼續...",
            flush=True,
        )

        while await _is_imdb_captcha_visible(page):
            await page.wait_for_timeout(1_500)

        # 等驗證完成後的轉址或 DOM 更新，避免立即讀到舊的驗證頁內容。
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        await page.wait_for_timeout(800)
        print("  [✓] 驗證畫面已消失，繼續檢查 IMDb 頁面。\n", flush=True)
        return True
    except Exception as e:
        print(f"  ⚠️ 檢查 IMDb 驗證狀態時發生錯誤：{e}", flush=True)
        return False


# ================================
# IMDb 搜尋與內頁詳細資料抓取
# ================================
async def parse_imdb_detail_page(page: Page, imdb_url: str) -> dict[str, Any]:
    """進入 IMDb 內頁，解析 JSON-LD 及 DOM 取得精確數據。"""
    try:
        await page.goto(imdb_url, wait_until="domcontentloaded", timeout=20_000)
        captcha_detected = await handle_captcha_if_needed(page)
        if captcha_detected:
            print("      驗證完成，重新載入 IMDb 詳細頁以確保取得影片內容...", flush=True)
            await page.goto(imdb_url, wait_until="domcontentloaded", timeout=20_000)
            await handle_captcha_if_needed(page)

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        rating = 0.0
        votes = 0
        genres: list[str] = []
        budget = 0
        gross = 0

        # 1. 從 JSON-LD 抓取結構化資料
        json_script = soup.find("script", {"type": "application/ld+json"})
        if json_script and json_script.string:
            try:
                data = json.loads(json_script.string)
                agg_rating = data.get("aggregateRating", {})
                rating = float(agg_rating.get("ratingValue", 0.0))
                votes = int(agg_rating.get("ratingCount", 0))

                genre_raw = data.get("genre", [])
                if isinstance(genre_raw, str):
                    genres = [genre_raw]
                elif isinstance(genre_raw, list):
                    genres = genre_raw
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        # 2. 抓取預算與全球票房 (Box Office Section)
        box_office_section = soup.select_one('section[data-testid="BoxOffice"]')
        if box_office_section:
            # 加入 separator=" " 避免文字黏在一起影響正規表達式匹配
            text = box_office_section.get_text(separator=" ")

            if "Budget" in text:
                budget_match = re.search(r"Budget\s*.*?\$([\d,]+)", text)
                if budget_match:
                    budget = int(budget_match.group(1).replace(",", ""))

            if "Gross worldwide" in text:
                gross_match = re.search(r"Gross worldwide\s*.*?\$([\d,]+)", text)
                if gross_match:
                    gross = int(gross_match.group(1).replace(",", ""))

        return {
            "rating": rating,
            "vote_count": votes,
            "genres": genres,
            "primary_genre": genres[0] if genres else "未分類",
            "genre_str": ", ".join(genres) if genres else "無",
            "budget": budget,
            "gross": gross,
        }
    except Exception as e:
        print(f"      ❌ 讀取內頁失敗 ({imdb_url}): {e}")
        return {
            "rating": 0.0, "vote_count": 0, "genres": [],
            "primary_genre": "未知", "genre_str": "無",
            "budget": 0, "gross": 0
        }


def normalize_title(title: str) -> str:
    """統一大小寫並移除空白與標點符號。"""
    title = unicodedata.normalize("NFKC", title).casefold()
    return re.sub(r"[\W_]+", "", title)


async def print_imdb_search_failure(
    page: Page,
    query_title: str,
    reason: str,
) -> None:
    """在終端機顯示搜尋失敗資訊；不建立或寫入除錯檔案。"""
    try:
        current_url = page.url
    except Exception:
        current_url = "無法取得目前網址"

    try:
        page_title = await page.title()
    except Exception as e:
        page_title = f"無法取得頁面標題：{e}"

    try:
        body_text = await page.locator("body").inner_text(timeout=3_000)
    except Exception as e:
        body_text = f"無法讀取頁面文字：{e}"

    print("      🔎 IMDb 搜尋失敗資訊：", flush=True)
    print(f"         查詢片名：{query_title}", flush=True)
    print(f"         失敗原因：{reason}", flush=True)
    print(f"         實際頁面網址：{current_url}", flush=True)
    print(f"         頁面標題：{page_title}", flush=True)
    if body_text and not body_text.startswith("無法讀取"):
        snippet = " ".join(body_text.split())[:500]
        print(f"         頁面文字摘要：{snippet or '(空白)'}", flush=True)


async def get_search_candidates(
    page: Page,
    query_title: str,
    zh_title: str,
    en_title: str,
    top_n: int = 2
) -> list[dict[str, Any]]:
    """搜尋 IMDb；遇到人機驗證時等待人工處理，再重新送出原搜尋網址。"""
    search_url = f"https://www.imdb.com/find/?q={quote(query_title)}&s=tt"
    items_selector = "li.ipc-metadata-list-summary-item"
    captcha_retries = 0
    redirect_only_retries = 0
    max_redirect_only_retries = 3

    try:
        while True:
            print(f"      開啟 IMDb 搜尋頁：{search_url}", flush=True)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)
            captcha_detected = await handle_captcha_if_needed(page)
            success_redirect = await _is_imdb_captcha_success_redirect(page)

            # 人工驗證完成後重新開啟原搜尋網址，不沿用驗證頁上的 DOM。
            if captcha_detected:
                captcha_retries += 1
                print(
                    f"      驗證已完成，重新送出原搜尋（第 {captcha_retries} 次）...",
                    flush=True,
                )
                await page.wait_for_timeout(1_200)
                continue

            # 有時頁面一開始就處於驗證成功後的轉址狀態。
            if success_redirect:
                redirect_only_retries += 1
                if redirect_only_retries > max_redirect_only_retries:
                    await print_imdb_search_failure(
                        page,
                        query_title,
                        "IMDb 多次停留在驗證成功後的重定向頁；已達重試上限，停止自動重送。",
                    )
                    return []
                print(
                    f"      IMDb 正在完成驗證後轉址，重新送出原搜尋（第 {redirect_only_retries} 次）...",
                    flush=True,
                )
                await page.wait_for_timeout(1_200)
                continue

            items_locator = page.locator(items_selector)
            try:
                await items_locator.first.wait_for(state="visible", timeout=15_000)
                break
            except Exception as first_error:
                # 等待期間若出現驗證或轉址畫面，重新送出原搜尋網址。
                captcha_detected = await handle_captcha_if_needed(page)
                success_redirect = await _is_imdb_captcha_success_redirect(page)
                if captcha_detected:
                    captcha_retries += 1
                    print(
                        f"      等待期間完成 IMDb 驗證，重新送出原搜尋（第 {captcha_retries} 次）...",
                        flush=True,
                    )
                    await page.wait_for_timeout(1_200)
                    continue

                if success_redirect:
                    redirect_only_retries += 1
                    if redirect_only_retries > max_redirect_only_retries:
                        await print_imdb_search_failure(
                            page,
                            query_title,
                            "搜尋結果等待逾時，且頁面反覆停留在驗證成功後的重定向畫面；"
                            f"已達重試上限。詳細錯誤：{first_error}",
                        )
                        return []
                    print(
                        f"      搜尋結果等待期間仍在轉址，重新送出原搜尋（第 {redirect_only_retries} 次）...",
                        flush=True,
                    )
                    await page.wait_for_timeout(1_200)
                    continue

                await print_imdb_search_failure(
                    page,
                    query_title,
                    "等待選擇器 li.ipc-metadata-list-summary-item 逾時；"
                    "目前未辨識到驗證頁，可能是搜尋結果載入較慢、IMDb 頁面結構改變或選擇器失效。"
                    f"詳細錯誤：{first_error}",
                )
                return []

        zh_normalized = normalize_title(zh_title)
        en_normalized = normalize_title(en_title)
        candidates: list[dict[str, Any]] = []
        count = await items_locator.count()

        for i in range(min(count, 10)):
            item = items_locator.nth(i)
            title_link = item.locator("a.ipc-title-link-wrapper").first
            title_elem = title_link.locator("h4.ipc-title__text").first

            if await title_elem.count() == 0:
                continue

            raw_title = (await title_elem.inner_text()).strip()
            rel_link = await title_link.get_attribute("href")
            if not rel_link:
                continue

            imdb_url = f"https://www.imdb.com{rel_link.split('?')[0]}"
            candidate_title = re.sub(r"^\d+\.\s*", "", raw_title).strip()
            normalized_title = normalize_title(candidate_title)
            exact_zh = bool(zh_normalized) and normalized_title == zh_normalized
            exact_en = bool(en_normalized) and normalized_title == en_normalized
            score_zh = calculate_similarity(zh_title, candidate_title)
            score_en = calculate_similarity(en_title, candidate_title)

            candidates.append({
                "imdb_title": candidate_title,
                "url": imdb_url,
                "score": max(score_zh, score_en),
                "exact_match": exact_zh or exact_en,
                "exact_en": exact_en,
                "exact_zh": exact_zh,
            })

        if not candidates:
            await print_imdb_search_failure(
                page,
                query_title,
                f"找到 {count} 個搜尋結果容器，但沒有解析出有效片名連結；"
                "請確認目前 IMDb 頁面的連結與標題選擇器。",
            )
            return []

        candidates.sort(
            key=lambda candidate: (candidate["exact_match"], candidate["score"]),
            reverse=True,
        )
        return candidates[:top_n]

    except Exception as e:
        print(f"      ❌ 搜尋異常 ({query_title}): {type(e).__name__}: {e}", flush=True)
        try:
            await print_imdb_search_failure(
                page,
                query_title,
                f"get_search_candidates 發生例外：{type(e).__name__}: {e}",
            )
        except Exception as info_error:
            print(f"      ⚠️ 顯示搜尋失敗資訊時發生錯誤：{info_error}", flush=True)
        return []


async def get_imdb_info(page: Page, zh_title: str, en_title: str) -> dict[str, Any]:
    zh_title = (zh_title or "").strip()
    en_title = (en_title or "").strip()
    all_candidates = []

    # 只搜尋有提供的語言；中文或英文任一欄有值即可查詢。
    search_count = int(bool(zh_title)) + int(bool(en_title))
    search_index = 0

    if zh_title:
        search_index += 1
        print(f"  └─ [{search_index}/{search_count}] 嘗試中文搜尋: '{zh_title}'")
        zh_candidates = await get_search_candidates(
            page, zh_title, zh_title, en_title, top_n=2
        )
        for candidate in zh_candidates:
            candidate["source_query"] = "中文搜尋"
            print(
                f"      中文候選：{candidate['imdb_title']} "
                f"| score={candidate['score']:.3f} "
                f"| exact={candidate['exact_match']} "
                f"| {candidate['url']}"
            )
        all_candidates.extend(zh_candidates)
    else:
        print("  └─ 未提供中文片名，略過中文搜尋。")

    if en_title:
        search_index += 1
        print(f"  └─ [{search_index}/{search_count}] 嘗試英文搜尋: '{en_title}'")
        en_candidates = await get_search_candidates(
            page, en_title, zh_title, en_title, top_n=2
        )
        for candidate in en_candidates:
            candidate["source_query"] = "英文搜尋"
            print(
                f"      英文候選：{candidate['imdb_title']} "
                f"| score={candidate['score']:.3f} "
                f"| exact={candidate['exact_match']} "
                f"| {candidate['url']}"
            )
        all_candidates.extend(en_candidates)
    else:
        print("  └─ 未提供英文片名，略過英文搜尋。")

    if not zh_title and not en_title:
        print("  └─ 中文與英文片名皆未提供，無法搜尋 IMDb。")

    if not all_candidates:
        print(
            "  ⚠️ IMDb 沒有產生任何可解析的候選結果。"
            f"中文片名={zh_title!r}；英文片名={en_title!r}。"
            "請查看上方終端機列出的目前網址、頁面標題與文字摘要。"
        )
        return {
            "imdb_title": "未找到結果", "rating": 0.0, "vote_count": 0,
            "genres": [], "primary_genre": "無", "genre_str": "無",
            "budget": 0, "gross": 0, "url": None, "matched_by": "無",
            "status": "未找到搜尋結果或未提供片名",
        }

    # 完整片名吻合優先，其次比較相似度。
    all_candidates.sort(
        key=lambda candidate: (candidate["exact_match"], candidate["score"]),
        reverse=True,
    )

    best = all_candidates[0]
    print(
        f"  └─ 綜合評比勝出: "
        f"{best['imdb_title']} (來源：{best['source_query']}) -> {best['url']}"
    )

    detail_info = await parse_imdb_detail_page(page, best["url"])
    print(f"      IMDb 內頁解析結果：{detail_info}")

    return {
        "imdb_title": best["imdb_title"],
        "url": best["url"],
        "score": best["score"],
        "matched_by": best["source_query"],
        "status": "成功找到",
        **detail_info,
    }

async def fetch_viewshow_movies(context: BrowserContext) -> list[Movie]:
    print("1. 正在取得威秀影城場次資料...")
    try:
        response = await context.request.post(
            VIEWSHOW_API_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
            form={"CinemaCode": "TP"},
            timeout=10_000,
        )
        result_text = await response.text()
    except Exception as e:
        print(f"❌ 取得威秀場次失敗: {e}")
        return []

    soup = BeautifulSoup(result_text, "html.parser")
    raw_zh = [t.get_text(strip=True) for t in soup.select("strong.LangTW.MovieName")]
    raw_en = [t.get_text(strip=True) for t in soup.select("strong.LangEN.MovieName")]

    movies: list[Movie] = []
    seen_en_titles = set()

    for zh, en in zip(raw_zh, raw_en):
        if should_exclude(en):
            continue
        zh_clean = clean_title(zh)
        en_clean = clean_title(en)
        if en_clean and en_clean not in seen_en_titles:
            seen_en_titles.add(en_clean)
            movies.append(Movie(zh_title=zh_clean, en_title=en_clean))

    return movies


# ================================
# Plotly HTML 儀表板生成模組
# ================================
def generate_plotly_html_dashboard(
    results: list[dict[str, Any]],
    dashboard_filename: str = "reports/viewshow_movies_dashboard.html",
    dashboard_title: str = "威秀上映電影分析報告",
) -> None:
    valid_data = [r for r in results if r.get("rating", 0) > 0 and r.get("vote_count", 0) > 0]
    if not valid_data:
        print("⚠️ 無資料可繪製圖表！")
        return

    total_movies = len(valid_data)
    avg_votes = sum(d["vote_count"] for d in valid_data) / total_movies
    avg_rating = sum(d["rating"] for d in valid_data) / total_movies

    # 建立 3 列 1 行的圖表畫布
    fig = make_subplots(
        rows=3, cols=1,
        subplot_titles=(
            "【圖一】IMDb 評分 vs. 投票數",
            "【圖二】各電影類型數量與平均評分",
            "【圖三】商業指標分析 (預算 / 票房 / 評分)"
        ),
        vertical_spacing=0.08
    )

    # 1. 評分 vs 投票數
    f1 = px.scatter(
        valid_data, x="vote_count", y="rating", color="primary_genre",
        hover_name="zh_title", hover_data={"en_title": True, "genre_str": True}
    )
    for trace in f1.data:
        fig.add_trace(trace, row=1, col=1)

    fig.add_vline(x=avg_votes, line_dash="dash", line_color="#ef4444", row=1, col=1)
    fig.add_hline(y=avg_rating, line_dash="dash", line_color="#10b981", row=1, col=1)

    # 2. 類型統計
    genre_stats = {}
    for d in valid_data:
        g = d["primary_genre"]
        if g not in genre_stats:
            genre_stats[g] = {"count": 0, "total_rating": 0.0}
        genre_stats[g]["count"] += 1
        genre_stats[g]["total_rating"] += d["rating"]

    genre_list = [
        {"genre": g, "count": s["count"], "avg_rating": round(s["total_rating"] / s["count"], 2)}
        for g, s in genre_stats.items()
    ]

    f2 = px.bar(
        genre_list, x="genre", y="count", color="avg_rating", text="avg_rating",
        color_continuous_scale="Blues"
    )
    for trace in f2.data:
        fig.add_trace(trace, row=2, col=1)

    # 3. 商業氣泡圖
    x_col = "budget" if any(d.get("budget", 0) > 0 for d in valid_data) else "vote_count"
    y_col = "gross" if any(d.get("gross", 0) > 0 for d in valid_data) else "rating"

    f3 = px.scatter(
        valid_data, x=x_col, y=y_col, size="rating", color="primary_genre", hover_name="zh_title"
    )
    for trace in f3.data:
        trace.showlegend = False  # 隱藏重複的圖例，保持右側乾淨
        fig.add_trace(trace, row=3, col=1)

    # 設定簡潔白色主題與版面調整
    fig.update_layout(
        title_text=f"🎬 {dashboard_title}（總計：{total_movies} 部）",
        title_x=0.5,
        template="plotly_white",       # 切換為純白簡潔背景
        height=1250,
        coloraxis_showscale=False,     # 關閉右側漸層色階條
        showlegend=True
    )

    # 設定座標軸名稱
    fig.update_xaxes(title_text="投票人數", row=1, col=1)
    fig.update_yaxes(title_text="評分", row=1, col=1)
    fig.update_xaxes(title_text="類型", row=2, col=1)
    fig.update_yaxes(title_text="數量", row=2, col=1)
    fig.update_xaxes(title_text="預算 (USD)" if x_col == "budget" else "投票人數", row=3, col=1)
    fig.update_yaxes(title_text="全球總票房 (USD)" if y_col == "gross" else "評分", row=3, col=1)

    dashboard_path = Path(dashboard_filename)
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(dashboard_path))
    print(f"  [✓] 白底簡潔版儀表板已產出: '{dashboard_filename}'")


# ================================
# DB & CSV 儲存
# ================================
def save_to_sqlite(results: list[dict[str, Any]], db_name: str = "data/vieshow/viewshow_movies_imdb.db") -> None:
    Path(db_name).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS movies")
        cursor.execute("""
            CREATE TABLE movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zh_title TEXT,
                imdb_title TEXT,
                rating REAL,
                vote_count INTEGER,
                genres TEXT,
                primary_genre TEXT,
                budget INTEGER,
                gross INTEGER,
                url TEXT,
                matched_by TEXT,
                en_title TEXT
            )
        """)

        data = [
            (
                item.get("zh_title"), item.get("imdb_title"), item.get("rating", 0.0),
                item.get("vote_count", 0), item.get("genre_str"), item.get("primary_genre"),
                item.get("budget", 0), item.get("gross", 0), item.get("url"),
                item.get("matched_by"), item.get("en_title")
            )
            for item in results
        ]

        cursor.executemany("""
            INSERT INTO movies (zh_title, imdb_title, rating, vote_count, genres, primary_genre, budget, gross, url, matched_by, en_title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)

    print(f"  [✓] 資料已寫入 SQLite: '{db_name}'")


def save_to_csv(results: list[dict[str, Any]], file_name: str = "data/vieshow/viewshow_movies_imdb.csv") -> None:
    # 欄位 Key 與 CSV 標題映射
    field_map = {
        "rank": "排名",
        "zh_title": "中文片名",
        "imdb_title": "IMDb標題",
        "rating": "IMDb評分",
        "vote_count": "投票數",
        "primary_genre": "主類型",
        "genre_str": "所有類型",
        "budget": "預算(USD)",
        "gross": "全球票房(USD)",
        "url": "IMDb網址",
        "matched_by": "匹配管道",
        "en_title": "英文片名",
    }

    headers = list(field_map.values())

    Path(file_name).parent.mkdir(parents=True, exist_ok=True)
    with open(file_name, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        for idx, item in enumerate(results, start=1):
            writer.writerow([
                idx,
                item.get("zh_title", ""),
                item.get("imdb_title", ""),
                item.get("rating", 0.0),
                item.get("vote_count", 0),
                item.get("primary_genre", ""),
                item.get("genre_str", ""),
                item.get("budget", 0),
                item.get("gross", 0),
                item.get("url", ""),
                item.get("matched_by", ""),
                item.get("en_title", "")
            ])

    print(f"  [✓] 已成功匯出 CSV 檔: '{file_name}'")


# ================================
# 主程序
# ================================
async def process_movie_worker(
    semaphore: asyncio.Semaphore, context: BrowserContext, movie: Movie, idx: int, total: int
) -> dict[str, Any]:
    async with semaphore:
        print(f"\n[{idx}/{total}] 正在爬取內頁：{movie.zh_title} / {movie.en_title}")
        page = await context.new_page()
        await page.route("**/*", block_heavy_resources)
        try:
            result = await get_imdb_info(page, movie.zh_title, movie.en_title)
            return {"zh_title": movie.zh_title, "en_title": movie.en_title, **result}
        finally:
            await page.close()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="zh-TW",
            viewport={"width": 1280, "height": 800},
        )

        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        movies = await fetch_viewshow_movies(context)
        if not movies:
            print("找不到符合條件的電影！")
            await browser.close()
            return

        print(f"\n2. 共 {len(movies)} 部電影，開啟分頁深入內頁抓取 JSON-LD 資料...")

        CONCURRENCY_LIMIT = 2
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        tasks = [
            process_movie_worker(semaphore, context, movie, idx, len(movies))
            for idx, movie in enumerate(movies, start=1)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        # 濾除執行失敗的 Exception 物件
        valid_results = [r for r in raw_results if isinstance(r, dict)]

        sorted_results = sorted(valid_results, key=lambda x: x.get("rating", 0.0), reverse=True)

        print("\n3. 儲存數據資料...")
        save_to_sqlite(sorted_results, "data/vieshow/viewshow_movies_imdb.db")
        save_to_csv(sorted_results, "data/vieshow/viewshow_movies_imdb.csv")

        print("\n4. 產生 Plotly HTML 互動式儀表板...")
        generate_plotly_html_dashboard(sorted_results, "reports/viewshow_movies_dashboard.html")

        print("\n[✓] 全流程執行完成！")
        await browser.close()

async def test_single_movie(zh_title: str, en_title: str) -> dict[str, Any]:
    """針對單一電影進行 IMDb 搜尋與內頁解析測試"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # 設為 False 可以親眼看到瀏覽器操作與驗證碼處理
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="zh-TW",
            viewport={"width": 1280, "height": 800},
        )

        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        page = await context.new_page()
        await page.route("**/*", block_heavy_resources)

        try:
            print(f"🎬 開始單一測試：{zh_title} ({en_title})\n")
            result = await get_imdb_info(page, zh_title, en_title)
            
            print("\n" + "="*40)
            print("📊 IMDb 最終解析結果：")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("="*40)
            
            return result
        finally:
            await page.close()
            await browser.close()

if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())

    # test_zh = "馬男波傑克"
    # test_en = ""
    
    # asyncio.run(test_single_movie(test_zh, test_en))