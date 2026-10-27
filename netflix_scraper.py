"""Netflix 分類頁與「我的口袋名單」擷取流程。"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from viewshow_imdb import Movie, clean_title


async def select_netflix_category(context: BrowserContext, category: str) -> Page:
    """開啟 Netflix、選擇 Luke 個人檔案，再進入指定分類。"""
    page = await context.new_page()
    category_label = "節目" if category == "series" else "電影"
    category_selectors = (
        [
            'a[data-uia="nav-series"]',
            'a[data-uia*="nav-series"]',
            'a:text-is("節目")',
            'a:text-is("TV Shows")',
            'a:text-is("Series")',
        ]
        if category == "series"
        else [
            'a[data-uia="nav-movies"]',
            'a[data-uia*="nav-movies"]',
            'a:text-is("電影")',
            'a:text-is("Movies")',
            'a:text-is("Films")',
        ]
    )
    profile_selector = 'div[data-uia="profile-selector"]'
    luke_selector = 'button[data-uia="profile-selector+tile-NTCOR6MPNNFJJM5O5HMLR5SWPU"]'

    async def find_category_button(timeout_seconds: int) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            for selector in category_selectors:
                locator = page.locator(selector)
                try:
                    for index in range(min(await locator.count(), 8)):
                        candidate = locator.nth(index)
                        if await candidate.is_visible():
                            return candidate
                except Exception:
                    continue
            await page.wait_for_timeout(350)
        return None

    async def wait_for_profile_change(timeout_seconds: float = 6) -> bool:
        try:
            await page.locator(profile_selector).wait_for(
                state="hidden", timeout=int(timeout_seconds * 1_000)
            )
            return True
        except PlaywrightTimeoutError:
            return await find_category_button(2) is not None

    async def pause_for_manual_action(message: str) -> None:
        try:
            await asyncio.to_thread(input, message)
        except (EOFError, KeyboardInterrupt):
            print("⚠️ 無法讀取 Enter 輸入。", flush=True)

    try:
        try:
            await page.goto("https://www.netflix.com/browse", timeout=30_000)
        except PlaywrightTimeoutError:
            print("⚠️ Netflix 完整載入逾時，改檢查目前頁面。", flush=True)
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)

        privacy_pattern = re.compile(
            r"隱私偏好中心|隐私偏好中心|Privacy Preference Center", re.IGNORECASE
        )
        privacy_items = page.locator("h1, h2, [role='dialog']")
        for index in range(await privacy_items.count()):
            item = privacy_items.nth(index)
            try:
                if await item.is_visible() and privacy_pattern.search(await item.inner_text()):
                    await pause_for_manual_action(
                        "請在瀏覽器處理隱私偏好中心，完成後按 Enter 繼續..."
                    )
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                continue

        profile = page.locator(profile_selector).first
        try:
            await profile.wait_for(state="visible", timeout=8_000)
            profile_visible = True
        except PlaywrightTimeoutError:
            profile_visible = False

        if profile_visible:
            luke = page.locator(luke_selector)
            try:
                await luke.wait_for(state="visible", timeout=15_000)
            except PlaywrightTimeoutError as error:
                names = await profile.locator("button").all_inner_texts()
                raise RuntimeError(
                    f"找不到 Luke 個人檔案按鈕；目前可見的個人檔案：{names}"
                ) from error

            try:
                await luke.click(timeout=10_000)
            except Exception as error:
                print(f"⚠️ 點擊 Luke 失敗：{error}", flush=True)

            changed = await wait_for_profile_change()
            if not changed:
                print("⚠️ 點擊後頁面未切換，重新載入並再試一次。", flush=True)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(1_000)
                    profile = page.locator(profile_selector).first
                    if await profile.is_visible():
                        luke = page.locator(luke_selector)
                        await luke.wait_for(state="visible", timeout=8_000)
                        await luke.click(timeout=8_000)
                    changed = await wait_for_profile_change(8)
                except Exception as error:
                    print(f"⚠️ 重新選擇 Luke 失敗：{error}", flush=True)

            if not changed:
                await pause_for_manual_action(
                    "⏸️ 請在瀏覽器手動選擇 Luke，看到分類頁後按 Enter..."
                )

        category_button = await find_category_button(20)
        if category_button is None:
            print(f"⚠️ 找不到 Netflix「{category_label}」導覽列。", flush=True)
            print(f"目前網址：{page.url}", flush=True)
            try:
                print(f"頁面標題：{await page.title()}", flush=True)
                print(f"頁面標題文字：{await page.locator('h1, h2').all_inner_texts()}", flush=True)
                debug_dir = Path("debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                await page.screenshot(
                    path=str(debug_dir / "netflix_category_navigation_timeout.png"),
                    full_page=True,
                )
            except Exception:
                pass
            await pause_for_manual_action(
                "⏸️ 請檢查瀏覽器是否有驗證或其他提示，處理後按 Enter 重新偵測..."
            )
            category_button = await find_category_button(10)

        if category_button is None:
            raise RuntimeError(f"仍找不到 Netflix「{category_label}」導覽列。")

        try:
            await category_button.click(timeout=10_000)
            await page.wait_for_timeout(1_200)
        except Exception as error:
            raise RuntimeError(
                f"找到 Netflix「{category_label}」導覽項目，但點擊失敗；目前網址：{page.url}"
            ) from error

        print(f"已進入 Netflix「{category_label}」分類。", flush=True)
        return page
    except Exception:
        await page.close()
        raise


class IncompleteNetflixCrawlError(RuntimeError):
    """輪播抓取中斷但已有部分片名時，攜帶片名交由主流程合併舊資料。"""

    def __init__(self, message: str, movies: list[Movie]) -> None:
        super().__init__(message)
        self.movies = movies


async def fetch_netflix_movies(
    context: BrowserContext,
    category: str,
    page: Page | None = None,
) -> list[Movie]:
    """擷取「我的口袋名單」輪播，最後一次列印完整片單。"""
    category_label = "節目" if category == "series" else "電影"
    movies: list[Movie] = []
    seen_keys: set[str] = set()
    if page is None:
        page = await select_netflix_category(context, category)

    card_selector = (
        'a[data-uia="standard-card"], a[data-uia="title-card"], '
        'a[href*="jbv="], a[href*="/watch/"]'
    )
    scroller_selector = 'div[data-uia="carousel-scroller"]'
    right_button_selector = 'button[data-uia="carousel-hawkins-right-button"]'
    heading_selector = 'h2:text-is("我的口袋名單")'

    async def collect_cards(section) -> int:
        before = len(movies)
        data = await section.locator(card_selector).evaluate_all(
            """elements => elements.map(element => {
                const image = element.querySelector('img');
                return {
                    href: (element.getAttribute('href') || '').trim(),
                    title: (element.getAttribute('aria-label') ||
                        element.getAttribute('title') ||
                        (image ? image.getAttribute('alt') : '') || '').trim()
                };
            })"""
        )
        for item in data:
            href = item["href"]
            title = clean_title(item["title"])
            if not href or not title:
                continue
            id_match = re.search(r"(?:jbv=|/watch/|/title/)(\d+)", href)
            key = (
                f"id:{id_match.group(1)}"
                if id_match
                else f"title:{re.sub(r'[\W_]+', '', title.casefold())}"
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            movies.append(Movie(zh_title=title, en_title=""))
        return len(movies) - before

    def print_movie_list() -> None:
        print(f"\nNetflix「{category_label}」我的口袋名單：共 {len(movies)} 部", flush=True)
        for index, movie in enumerate(movies, start=1):
            print(f"{index}. {movie.zh_title}", flush=True)

    async def move_mouse_to_heading(heading) -> None:
        try:
            box = await heading.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                viewport = await page.evaluate(
                    "() => ({width: innerWidth, height: innerHeight})"
                )
                if 0 <= x < viewport["width"] and 0 <= y < viewport["height"]:
                    await page.mouse.move(x, y)
        except Exception:
            pass

    async def restore_vertical_position(expected_y: float) -> None:
        try:
            current_y = await page.evaluate("() => window.scrollY")
            if abs(current_y - expected_y) > 1:
                await page.evaluate(
                    "top => window.scrollTo({top, behavior: 'instant'})", expected_y
                )
        except Exception:
            pass

    async def wait_for_carousel_stable(section, timeout_ms: int = 10_000) -> bool:
        """DOM 與水平位置連續穩定後才讀取，避免動畫中過早判斷沒有新增。"""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        previous = None
        stable_count = 0
        elapsed_ms = 0
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = await section.locator(scroller_selector).first.evaluate(
                    """element => ({
                        left: Math.round(element.scrollLeft),
                        width: element.scrollWidth,
                        cards: Array.from(element.querySelectorAll(
                            'a[data-uia="standard-card"], a[data-uia="title-card"], a[href*="jbv="], a[href*="/watch/"]'
                        )).map(card => ({
                            href: card.getAttribute('href') || '',
                            title: card.getAttribute('aria-label') || card.getAttribute('title') ||
                                (card.querySelector('img')?.getAttribute('alt') || '')
                        }))
                    })"""
                )
                signature = json.dumps(state, ensure_ascii=False, sort_keys=True)
                if signature == previous:
                    stable_count += 1
                else:
                    previous, stable_count = signature, 0
            except Exception:
                stable_count = 0
                previous = None

            if elapsed_ms >= 1_500 and stable_count >= 3:
                return True
            await page.wait_for_timeout(500)
            elapsed_ms += 500
        return False

    try:
        heading = page.locator(heading_selector).first
        try:
            await heading.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError as error:
            print(f"⚠️ 找不到「我的口袋名單」標題；網址：{page.url}", flush=True)
            print(f"頁面標題：{await page.title()}；h2：{await page.locator('h2').all_inner_texts()}", flush=True)
            raise RuntimeError("Netflix 分類頁沒有顯示「我的口袋名單」。") from error

        section = None
        candidates = page.locator('section:has(h2:text("我的口袋名單"))')
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            try:
                if (
                    await candidate.locator(scroller_selector).count()
                    and await candidate.locator(card_selector).count()
                ):
                    section = candidate
                    break
            except Exception:
                continue

        if section is None:
            section = heading.locator(
                "xpath=ancestor::*[.//div[@data-uia='carousel-scroller'] and "
                ".//a[@data-uia='standard-card' or @data-uia='title-card' "
                "or contains(@href, 'jbv=') or contains(@href, '/watch/')]][1]"
            )
        if not await section.count():
            raise RuntimeError("已找到片單標題，但找不到輪播容器。")

        scroller = section.locator(scroller_selector).first
        await scroller.wait_for(state="attached", timeout=10_000)
        await section.locator(card_selector).first.wait_for(state="attached", timeout=10_000)

        await heading.evaluate(
            """element => {
                const rect = element.getBoundingClientRect();
                window.scrollTo({top: Math.max(0, window.scrollY + rect.top - 120), behavior: 'instant'});
            }"""
        )
        await page.wait_for_timeout(500)
        locked_scroll_y = await page.evaluate("() => window.scrollY")
        await collect_cards(section)

        no_progress = 0
        stalled = 0
        unstable_rounds = 0
        max_stalled = 3
        max_no_progress = 9
        max_clicks = 150
        stop_reason = "輪播已結束"
        click_count = 0

        while click_count < max_clicks:
            await move_mouse_to_heading(heading)
            await restore_vertical_position(locked_scroll_y)
            viewport = await page.evaluate(
                "() => ({width: innerWidth, height: innerHeight})"
            )
            scroller_box = await scroller.bounding_box()
            if not scroller_box:
                stop_reason = "無法取得輪播位置"
                break

            right_button = section.locator(right_button_selector).first
            button_box = await right_button.bounding_box() if await right_button.count() else None
            target_x = (
                button_box["x"] + button_box["width"] / 2
                if button_box else scroller_box["x"] + scroller_box["width"] - 32
            )
            target_y = (
                button_box["y"] + button_box["height"] / 2
                if button_box else scroller_box["y"] + scroller_box["height"] / 2
            )
            if not (0 <= target_x < viewport["width"] and 0 <= target_y < viewport["height"]):
                stop_reason = "右側箭頭不在目前視窗內"
                break

            await page.mouse.move(target_x, target_y)
            await page.wait_for_timeout(350)
            right_button = section.locator(right_button_selector).first
            if not await right_button.count() or not await right_button.is_visible():
                stop_reason = "找不到可見的右側箭頭"
                break
            if (
                not await right_button.is_enabled()
                or await right_button.get_attribute("disabled") is not None
            ):
                stop_reason = "右側箭頭已停用，輪播到底"
                break

            button_box = await right_button.bounding_box()
            if not button_box:
                stop_reason = "無法取得右側箭頭位置"
                break
            x = button_box["x"] + button_box["width"] / 2
            y = button_box["y"] + button_box["height"] / 2
            if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
                stop_reason = "右側箭頭不在目前視窗內"
                break

            before_left = await scroller.evaluate("element => Math.round(element.scrollLeft)")
            await page.mouse.move(x, y)
            await page.wait_for_timeout(250)
            await page.mouse.click(x, y)
            click_count += 1

            stable = await wait_for_carousel_stable(section)
            await restore_vertical_position(locked_scroll_y)
            added = await collect_cards(section)
            after_left = await scroller.evaluate("element => Math.round(element.scrollLeft)")
            moved = abs(after_left - before_left) > 2

            if not stable:
                unstable_rounds += 1
            if added:
                no_progress = stalled = 0
            else:
                no_progress += 1
                stalled = 0 if moved else stalled + 1
                if stalled >= max_stalled:
                    stop_reason = "輪播位置連續沒有移動"
                    break
                if no_progress >= max_no_progress:
                    stop_reason = "連續多次滑動沒有新增片名，停止以避免無限輪播"
                    break

        if unstable_rounds:
            print(f"⚠️ 有 {unstable_rounds} 次輪播等待穩定逾時；已依目前畫面整理片單。", flush=True)
        if click_count >= max_clicks:
            stop_reason = f"已達安全滑動上限 {max_clicks} 次"

        await page.wait_for_timeout(400)
        await collect_cards(section)
        print(f"輪播結束：{stop_reason}；滑動 {click_count} 次。", flush=True)
        print_movie_list()
        try:
            await asyncio.to_thread(
                input,
                "⏸️ 請確認片單正確；按 Enter 後開始查詢 IMDb...",
            )
        except (EOFError, KeyboardInterrupt):
            print("⚠️ 無法讀取 Enter 輸入，繼續後續流程。", flush=True)
        return movies

    except Exception as error:
        print(f"⚠️ 取得 Netflix「{category_label}」片單失敗：{error}", flush=True)
        if movies:
            print(f"已取得 {len(movies)} 部；將保留部分結果，不覆蓋完整資料。", flush=True)
            print_movie_list()
            try:
                await asyncio.to_thread(input, "檢查瀏覽器後按 Enter，繼續使用已取得片名...")
            except (EOFError, KeyboardInterrupt):
                pass
            raise IncompleteNetflixCrawlError(str(error), movies) from error
        try:
            await asyncio.to_thread(input, "請檢查 Netflix 瀏覽器狀態後按 Enter 結束...")
        except (EOFError, KeyboardInterrupt):
            pass
        return []
    finally:
        await page.close()
