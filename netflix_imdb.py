import asyncio
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, async_playwright


# 重用威秀程式裡既有的 IMDb 搜尋、資料解析及輸出功能。
# 請將威秀 IMDb 程式 viewshow_imdb.py 與本檔放在同一個資料夾。
from viewshow_imdb import (
    Movie,
    block_heavy_resources,
    generate_plotly_html_dashboard,
    get_imdb_info,
    clean_title,
    save_to_csv,
    save_to_sqlite,
)


from netflix_scraper import fetch_netflix_movies, select_netflix_category


def load_sqlite_results(db_name: str) -> list[dict[str, Any]]:
    """讀取既有 SQLite 電影資料；資料表不存在或沒有資料時回傳空清單。"""
    db_path = Path(db_name)
    if not db_path.exists():
        return []

    try:
        # 設定較短的鎖定等待時間，避免資料庫被其他程式鎖住時看起來一直卡住。
        with closing(sqlite3.connect(db_path, timeout=3)) as conn:
            conn.row_factory = sqlite3.Row
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='movies'"
            ).fetchone()
            if table is None:
                return []

            rows = conn.execute(
                """
                SELECT zh_title, imdb_title, rating, vote_count, genres,
                       primary_genre, budget, gross, url, matched_by, en_title
                FROM movies
                """
            ).fetchall()

        print(f"SQLite 讀取完成，共 {len(rows)} 筆。", flush=True)
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["genre_str"] = item.pop("genres", "無") or "無"
            item["genres"] = [
                genre.strip()
                for genre in item["genre_str"].split(",")
                if genre.strip() and genre.strip() != "無"
            ]
            results.append(item)
        return results
    except sqlite3.Error as e:
        print(f"⚠️ 讀取 SQLite 資料庫失敗（{db_name}）：{e}")
        return []


def ask_refresh_database() -> bool:
    """詢問是否重新擷取 Netflix 片單；空白預設為否，避免意外重爬。"""
    while True:
        answer = input(
            "資料庫已有資料，是否重新爬取 Netflix 分類片名？(y/N): "
        ).strip().lower()
        if answer in {"y", "yes", "是", "要"}:
            return True
        if answer in {"", "n", "no", "否", "不要"}:
            return False
        print("請輸入 y（重新爬取）或 n（使用資料庫既有資料）。")


def ask_result_limit(total: int) -> int:
    """詢問這次要重新查詢 IMDb 的筆數；空白代表全部。"""
    while True:
        answer = input(
            f"要查詢幾筆 IMDb 資料？直接按 Enter 代表全部（可用快取片名共 {total} 筆）："
        ).strip()
        if not answer:
            return total
        try:
            requested = int(answer)
        except ValueError:
            print("請輸入正整數，或直接按 Enter 搜尋全部。")
            continue
        if requested < 1:
            print("筆數必須大於 0，或直接按 Enter 搜尋全部。")
            continue
        if requested > total:
            print(f"輸入 {requested} 筆超過資料庫數量，將以最大值 {total} 筆執行。")
            return total
        return requested


def merge_updated_results(
    existing_results: list[dict[str, Any]],
    updated_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只更新本次查詢到的片名，保留資料庫內未查詢的其他紀錄。"""
    def title_key(item: dict[str, Any]) -> str:
        return clean_title(str(item.get("zh_title", "")).strip()).casefold()

    updated_by_title = {
        title_key(item): item
        for item in updated_results
    }
    merged = [
        updated_by_title.pop(title_key(item), item)
        for item in existing_results
    ]
    merged.extend(updated_by_title.values())
    return sorted(
        merged,
        key=lambda item: item.get("rating", 0.0) or 0.0,
        reverse=True,
    )


def save_sqlite_results(
    results: list[dict[str, Any]],
    db_name: str,
    overwrite: bool,
) -> None:
    """依本次查詢模式儲存 SQLite；完整查詢以新資料庫原子取代舊檔。"""
    db_path = Path(db_name)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not overwrite:
        # 部分查詢會先合併既有紀錄，再將完整合併結果寫回資料庫。
        save_to_sqlite(results, str(db_path))
        return

    # 先寫入暫存資料庫，成功後才取代正式檔，避免寫入失敗時損毀舊資料。
    temp_db_path = db_path.with_name(f"{db_path.stem}.new{db_path.suffix}")
    sidecar_suffixes = ("-journal", "-wal", "-shm")

    temp_sidecars = [Path(f"{temp_db_path}{suffix}") for suffix in sidecar_suffixes]
    for candidate in (temp_db_path, *temp_sidecars):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeError(f"無法清理 SQLite 暫存檔 {candidate}: {error}") from error

    try:
        save_to_sqlite(results, str(temp_db_path))
        if not temp_db_path.exists():
            raise RuntimeError("SQLite 儲存函式未產生暫存資料庫，為避免遺失舊資料，取消覆蓋。")

        # 正式資料庫即將整份替換，清除舊資料庫可能殘留的 WAL/SHM sidecar。
        for suffix in sidecar_suffixes:
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        os.replace(temp_db_path, db_path)
        print(f"✅ 已使用本次完整查詢結果覆蓋 SQLite：{db_path.resolve()}", flush=True)
    finally:
        # 正常關閉的 SQLite 通常不會留下 sidecar；若有殘留則清理暫存檔。
        for candidate in (temp_db_path, *temp_sidecars):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def choose_netflix_category() -> str:
    """要求使用者先選擇 Netflix 分類，回傳 series 或 movies。"""
    print("\n請選擇 Netflix 分類：")
    print("1. 節目")
    print("2. 電影")
    while True:
        choice = input("請輸入選項（1/2）：").strip()
        if choice == "1":
            return "series"
        if choice == "2":
            return "movies"
        print("選項無效，請輸入 1（節目）或 2（電影）。")


async def process_netflix_movie_worker(
    semaphore: asyncio.Semaphore,
    context: BrowserContext,
    movie: Movie,
) -> dict[str, Any]:
    """查詢單一片名；一般進度統一在主流程結束時彙總。"""
    async with semaphore:
        page = await context.new_page()
        try:
            await page.route("**/*", block_heavy_resources)
            result = await get_imdb_info(page, movie.zh_title, movie.en_title)
            return {"zh_title": movie.zh_title, "en_title": movie.en_title, **result}
        finally:
            await page.close()


async def main() -> None:
    # 先選擇分類，再檢查該分類自己的 SQLite 資料庫。
    category = choose_netflix_category()
    category_label = "節目" if category == "series" else "電影"
    db_name = str(Path("data") / "netflix" / f"netflix_{category}_imdb.db")
    csv_name = str(Path("data") / "netflix" / f"netflix_{category}_imdb.csv")
    dashboard_name = str(Path("reports") / f"netflix_{category}_dashboard.html")

    existing_results = load_sqlite_results(db_name)

    # 預設重新擷取 Netflix 分類片單時，視為完整查詢；使用快取片名時再依筆數判定。
    is_full_crawl = True
    if existing_results:
        use_cache_titles = not ask_refresh_database()
    else:
        print(f"Netflix「{category_label}」資料庫尚無資料，將直接爬取分類頁。")
        use_cache_titles = False

    # 快取模式先選擇要重新查詢 IMDb 的片名數量；不讀取 Netflix Cookie，也不開啟 Netflix 分類頁。
    if use_cache_titles:
        cached_movies = [
            Movie(
                zh_title=clean_title(str(item.get("zh_title", "")).strip()),
                en_title=str(item.get("en_title", "") or "").strip(),
            )
            for item in existing_results
            if str(item.get("zh_title", "")).strip()
        ]
        if not cached_movies:
            print("⚠️ SQLite 中沒有可用的快取片名，改為重新爬取 Netflix 分類頁。")
            use_cache_titles = False
        else:
            result_limit = ask_result_limit(len(cached_movies))
            movies = cached_movies[:result_limit]
            is_full_crawl = result_limit == len(cached_movies)
            crawl_mode = "完整查詢：儲存時覆蓋舊資料" if is_full_crawl else "部分查詢：儲存時與舊資料合併"
            print(
                f"將使用資料庫快取片名重新查詢 IMDb {len(movies)} / {len(cached_movies)} 筆；"
                "不會載入 Netflix Cookie 或開啟 Netflix 分類頁。"
            )
            print(f"📦 本次儲存模式：{crawl_mode}", flush=True)

    # 只有需要擷取 Netflix 分類頁時才讀取 Cookie。
    cookies: list[dict[str, Any]] | None = None
    if not use_cache_titles:
        cookie_path = Path("config") / "netflix_cookies.json"
        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"讀取 Netflix Cookie 失敗：{e}")
            return

    async with async_playwright() as p:
        # 對齊威秀程式的 Chromium 啟動設定。
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context_options = {
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "locale": "zh-TW",
            "viewport": {"width": 1280, "height": 800},
        }
        webdriver_init_script = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"

        try:
            if not use_cache_titles:
                # Netflix Cookie 僅放在 Netflix 專用 Context；IMDb 查詢使用另一個乾淨 Context。
                netflix_context = await browser.new_context(**context_options)
                await netflix_context.add_init_script(webdriver_init_script)
                await netflix_context.add_cookies(cookies or [])
                
                category_page = await select_netflix_category(netflix_context, category)
                if existing_results:
                    print(f"已選擇重新爬取，直接從 Netflix「{category_label}」分類頁取得片名。")
                else:
                    print(f"將直接從 Netflix「{category_label}」分類頁取得片名。")

                try:
                    try:
                        movies = await fetch_netflix_movies(netflix_context, category, category_page)
                    except Exception as error:
                        # 不依賴特定 exception 類別的匯入，避免主程式與 scraper 版本不一致。
                        # 只有例外本身攜帶 movies 清單時，才視為已有部分片單可安全保留。
                        partial_movies = getattr(error, "movies", None)
                        if not isinstance(partial_movies, list):
                            raise
                        movies = partial_movies
                        is_full_crawl = False
                        print(
                            "⚠️ Netflix 片單只完成部分抓取；本次會保留舊資料，"
                            "並合併本次成功查詢的結果，不會覆蓋整份資料庫。",
                            flush=True,
                        )
                finally:
                    await netflix_context.close()

                if not movies:
                    print("沒有取得 Netflix 分類片名，請確認 Cookie、登入狀態及分類頁內容。")
                    return
                if is_full_crawl:
                    print("📦 本次儲存模式：完整查詢，完成後以本次結果覆蓋舊資料。", flush=True)
                else:
                    print("📦 本次儲存模式：部分抓取，完成後與既有資料合併。", flush=True)

            # IMDb 使用與 viewshow_imdb.py 相同的 UA、語系、視窗大小及 init script。
            imdb_context = await browser.new_context(**context_options)
            await imdb_context.add_init_script(webdriver_init_script)

            print(f"\n2. 共 {len(movies)} 筆「Netflix {category_label}」片名，開始查詢 IMDb...")
            semaphore = asyncio.Semaphore(2)
            tasks = [
                process_netflix_movie_worker(semaphore, imdb_context, movie)
                for movie in movies
            ]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 分別記錄成功與失敗的查詢，供流程結束時統計未搜尋到的片名。
            valid_results: list[dict[str, Any]] = []
            failed_results: list[tuple[str, str]] = []
            for result_index, result in enumerate(raw_results):
                source_movie = movies[result_index]

                if isinstance(result, Exception):
                    reason = f"任務例外：{result}"
                    failed_results.append((source_movie.zh_title, reason))
                elif (
                    isinstance(result, dict)
                    and result.get("url")
                    and result.get("status") == "成功找到"
                ):
                    valid_results.append(result)
                elif isinstance(result, dict):
                    title = str(result.get("zh_title") or source_movie.zh_title)
                    reason = str(result.get("status") or "未匹配到 IMDb 結果")
                    failed_results.append((title, reason))
                else:
                    reason = "IMDb worker 沒有回傳有效結果"
                    failed_results.append((source_movie.zh_title, reason))

            refreshed_results = sorted(
                valid_results,
                key=lambda item: item.get("rating", 0.0) or 0.0,
                reverse=True,
            )

            # 完整查詢：只使用本次成功結果，讓舊片單中已不存在的紀錄一併移除。
            # 部分查詢：更新查詢成功的片名，其餘舊紀錄全部保留。
            if is_full_crawl:
                final_results = refreshed_results
            else:
                final_results = merge_updated_results(existing_results, refreshed_results)

            if is_full_crawl and not refreshed_results:
                print(
                    "⚠️ 本次完整查詢沒有任何成功的 IMDb 結果。為避免暫時性失敗清空舊資料，"
                    "本次不覆蓋既有檔案。",
                    flush=True,
                )
                return

            print("\n3. 儲存 IMDb 資料...")
            save_sqlite_results(final_results, db_name, overwrite=is_full_crawl)
            save_to_csv(final_results, csv_name)

            print("\n4. 產生 Plotly HTML 互動式儀表板...")
            generate_plotly_html_dashboard(
                final_results,
                dashboard_name,
                dashboard_title=f"Netflix「{category_label}」IMDb 分析報告",
            )
            print(f"\nNetflix「{category_label}」IMDb 查詢完成！")
            print(f"資料庫：{db_name}")
            print(f"CSV：{csv_name}")
            print(f"HTML：{dashboard_name}")

            # 本次統計只計算這次實際查詢的結果，不把舊快取紀錄算入成功筆數。
            print("\n" + "=" * 60)
            print(f"Netflix「{category_label}」IMDb 查詢統計")
            print(f"本次查詢片名：{len(movies)} 部")
            print(f"成功搜尋：{len(valid_results)} 部")
            print(f"未搜尋成功：{len(failed_results)} 部")
            if failed_results:
                print("\n未搜尋成功的片名：")
                for index, (title, reason) in enumerate(failed_results, start=1):
                    print(f"{index}. {title}（{reason}）")
            else:
                print("本次所有片名皆成功取得 IMDb 結果。")
            print("=" * 60)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
