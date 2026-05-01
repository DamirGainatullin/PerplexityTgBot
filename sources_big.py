import feedparser
import logging
import requests
import re
import os
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta
from collections import Counter
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urljoin
import time
import urllib3
from urllib.parse import urlparse
urllib3.disable_warnings()
TIME_WINDOW = timedelta(hours=28)
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_BATCH_SIZE = 5
TAVILY_TIMEOUT = 25
DEFAULT_SERVER_CHROME_BINARY = "/opt/google/chrome/chrome"
DEFAULT_LOCAL_CHROME_BINARY = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
FIRST_CHECK_TIMEOUT = 120
FIRST_CHECK_MAX_RETRIES = 3
US_TREASURY_CANDIDATE_URL = "https://home.treasury.gov/news/press-releases/"


RUNTIME_CONFIG = {
    "local_test_mode": False,
    "chrome_binary_location": DEFAULT_SERVER_CHROME_BINARY
}


SOURCE_LINK_HINTS = {
    "OFAC": "ofac.treasury.gov/recent-actions",
    "Eur-lex acts": "eur-lex.europa.eu/oj/daily-view",
    "BIS GOV": "www.bis.gov/news-updates",
    "UK Statutory Instruments": "www.legislation.gov.uk/title/sanctions/data.feed",
    "UK Russia designations": "www.gov.uk/guidance/russia-list-of-designations-and-sanctions-notices",
    "President Gov UA": "www.president.gov.ua/documents/decrees",
    "MOFCOM": "english.mofcom.gov.cn/article/policyrelease/",
}


SOURCE_KEYS = [
    "EU Commission",
    "EU Council",
    "UK OFSI",
    "UK FCDO",
    "US Treasury",
    "US State Dept",
    "OFAC",
    "Eur-lex acts",
    "BIS GOV",
    "UK Statutory Instruments",
    "EU Sanctions FAQ",
    "UK Russia designations",
    # "President Gov UA",
    "FederalRegister"
]

RSS_SOURCES = {
    "EU Commission":
    "https://ec.europa.eu/commission/presscorner/api/rss",
    "EU Council":
    "https://www.consilium.europa.eu/en/rss/pressreleases.ashx",
    "UK OFSI":
    "https://www.gov.uk/government/organisations/office-of-financial-sanctions-implementation.atom",
    "UK FCDO":
    "https://www.gov.uk/government/organisations/foreign-commonwealth-development-office.atom",
    "US Treasury":
    "https://home.treasury.gov/news/press-releases/rss", # 404
    "US State Dept":
    "https://www.state.gov/press-releases/feed/",
    "EU Sanctions FAQ":
    "https://finance.ec.europa.eu/node/1068/rss_en",
    "FederalRegister":
    "https://www.federalregister.gov/api/v1/documents.rss?conditions[search_type_id]=3&conditions[term]=entity+list+OR+export+administration+regulations"
}


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}


HTTP_STATUS_TO_SKIP = {401, 403, 404, 503}


def enable_local_test_mode(log_to_file=False, log_file_path="sources_big.local.log", log_level=logging.INFO, local_chrome_binary=DEFAULT_LOCAL_CHROME_BINARY):
    RUNTIME_CONFIG["local_test_mode"] = True
    RUNTIME_CONFIG["chrome_binary_location"] = local_chrome_binary

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    stream_exists = any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers)
    if not stream_exists:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root_logger.addHandler(stream_handler)

    if log_to_file:
        file_exists = any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "").endswith(log_file_path) for h in root_logger.handlers)
        if not file_exists:
            file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            root_logger.addHandler(file_handler)

    logging.info("[LOCAL TEST] enabled, chrome_binary=%s, log_to_file=%s", local_chrome_binary, log_to_file)


def get_chrome_binary_location():
    return RUNTIME_CONFIG["chrome_binary_location"]


def dump_news_to_json(news, file_path="local_news_dump.json"):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(news, f, ensure_ascii=False, indent=2)
        logging.info("[LOCAL TEST] JSON dump saved: %s", file_path)
    except Exception:
        logging.exception("[LOCAL TEST] Failed to save JSON dump: %s", file_path)


def dump_local_pipeline_json(parsed_raw, first_check_enriched, rewritten_summaries, file_path="local_news_dump.json"):
    payload = {
        "parsed_raw": parsed_raw,
        "first_check_enriched": first_check_enriched,
        "summary_rewritten": rewritten_summaries,
    }
    dump_news_to_json(payload, file_path=file_path)


def _read_text_with_fallback(path: Path):
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1251")


def load_first_check_prompt():
    prompt_path = Path(__file__).parent / "first_check_prompt"
    return _read_text_with_fallback(prompt_path)


def load_summary_rewriter_prompt():
    prompt_path = Path(__file__).parent / "summary_rewriter"
    return _read_text_with_fallback(prompt_path)


def _extract_json_payload(text):
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("Model returned empty response.")

    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def _normalize_news_item(item):
    normalized = {
        "source": str(item.get("source", "")).strip(),
        "title": str(item.get("title", "")).strip(),
        "date": str(item.get("date", "")).strip(),
        "link": str(item.get("link", "")).strip(),
    }
    summary = str(item.get("summary", "")).strip()
    if summary:
        normalized["summary"] = summary
    return normalized


def first_check_filter_news(news_items):
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    if not news_items:
        return []

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    prompt = load_first_check_prompt()
    materials = json.dumps(news_items, ensure_ascii=False, indent=2)
    payload = {
        "model": "openai/gpt-4.1-mini",
        "temperature": 0.0,
        "max_tokens": 8000,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": materials}
        ]
    }

    last_error = None
    for attempt in range(1, FIRST_CHECK_MAX_RETRIES + 1):
        response = None
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=FIRST_CHECK_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            result_text = data["choices"][0]["message"]["content"]
            parsed = _extract_json_payload(result_text)

            if isinstance(parsed, dict):
                maybe_items = parsed.get("news") or parsed.get("items") or parsed.get("data")
                if isinstance(maybe_items, list):
                    parsed = maybe_items

            if not isinstance(parsed, list):
                raise ValueError("first_check model response is not a JSON list.")

            filtered = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_news_item(item)
                if normalized["title"] and normalized["link"] and normalized["source"]:
                    filtered.append(normalized)

            logging.info("[FIRST CHECK] input_items=%s output_items=%s", len(news_items), len(filtered))
            return filtered
        except Exception as exc:
            last_error = exc
            status_code = getattr(response, "status_code", "no-response")
            response_text = (response.text or "")[:500] if response is not None else ""
            logging.warning(
                "[FIRST CHECK RETRY] attempt=%s/%s status=%s input_items=%s body=%r err=%s",
                attempt,
                FIRST_CHECK_MAX_RETRIES,
                status_code,
                len(news_items),
                response_text,
                exc,
            )
            if attempt < FIRST_CHECK_MAX_RETRIES:
                time.sleep(min(2 * attempt, 6))

    logging.error("[FIRST CHECK ERROR] all retries failed, fallback to raw items: %r", last_error)
    return news_items


def rewrite_summaries_with_model(news_items):
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        logging.warning("[SUMMARY REWRITER] OPENROUTER_API_KEY is missing, rewrite skipped")
        return news_items

    if not news_items:
        return []

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    prompt = load_summary_rewriter_prompt()
    materials = json.dumps(news_items, ensure_ascii=False, indent=2)
    payload = {
        "model": "openai/gpt-4.1-mini",
        "temperature": 0.0,
        "max_tokens": 9000,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": materials}
        ]
    }

    last_error = None
    for attempt in range(1, FIRST_CHECK_MAX_RETRIES + 1):
        response = None
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=FIRST_CHECK_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            result_text = data["choices"][0]["message"]["content"]
            parsed = _extract_json_payload(result_text)

            if isinstance(parsed, dict):
                maybe_items = parsed.get("news") or parsed.get("items") or parsed.get("data")
                if isinstance(maybe_items, list):
                    parsed = maybe_items

            if not isinstance(parsed, list):
                raise ValueError("summary_rewriter response is not a JSON list.")

            rewritten = []
            by_key = {}
            for item in news_items:
                if not isinstance(item, dict):
                    continue
                key = (
                    str(item.get("source", "")).strip(),
                    str(item.get("title", "")).strip(),
                    str(item.get("link", "")).strip(),
                    str(item.get("date", "")).strip(),
                )
                by_key[key] = item

            for item in parsed:
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_news_item(item)
                key = (
                    normalized["source"],
                    normalized["title"],
                    normalized["link"],
                    normalized["date"],
                )
                original = by_key.get(key)
                if not original:
                    continue
                new_item = dict(original)
                new_summary = str(item.get("summary", "")).strip()
                if new_summary:
                    new_item["summary"] = new_summary[:2000]
                rewritten.append(new_item)

            if not rewritten:
                raise ValueError("summary_rewriter returned no matched items.")

            logging.info("[SUMMARY REWRITER] input_items=%s output_items=%s", len(news_items), len(rewritten))
            return rewritten
        except Exception as exc:
            last_error = exc
            status_code = getattr(response, "status_code", "no-response")
            response_text = (response.text or "")[:500] if response is not None else ""
            logging.warning(
                "[SUMMARY REWRITER RETRY] attempt=%s/%s status=%s input_items=%s body=%r err=%s",
                attempt,
                FIRST_CHECK_MAX_RETRIES,
                status_code,
                len(news_items),
                response_text,
                exc,
            )
            if attempt < FIRST_CHECK_MAX_RETRIES:
                time.sleep(min(2 * attempt, 6))

    logging.error("[SUMMARY REWRITER ERROR] all retries failed, fallback to enriched summaries: %r", last_error)
    return news_items


def check_response_status(response, source_name):
    if response.status_code in HTTP_STATUS_TO_SKIP:
        logging.warning("[CHECK] %s: HTTP %s", source_name, response.status_code)
        return False

    response.raise_for_status()
    return True


def log_source_count(source_name, count, label, status_code=None):
    prefix = f"[CHECK] {source_name}: "
    if status_code is not None:
        prefix += f"HTTP {status_code}, "

    if count == 0:
        logging.warning("%s%s is empty", prefix, label)
    else:
        logging.info("%s%s=%s", prefix, label, count)


def detect_html_error_page(page_text, source_name):
    page_lower = page_text.lower()
    error_signatures = (
        ("401", ("401", "unauthorized")),
        ("403", ("403", "forbidden")),
        ("403", ("403", "access denied")),
        ("404", ("404", "not found")),
    )

    for code, markers in error_signatures:
        if all(marker in page_lower for marker in markers):
            logging.warning("[CHECK] %s: page looks like HTTP %s", source_name, code)
            return True

    return False


def is_within_24h(published_dt: datetime) -> bool:
    now = datetime.now(timezone.utc)
    return now - published_dt <= TIME_WINDOW


def parse_entry_date(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

    if hasattr(entry, "published"):
        try:
            dt = parsedate_to_datetime(entry.published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    return None


def normalize_entry(entry, source_name):
    published_dt = parse_entry_date(entry)

    if not published_dt:
        return None

    if not is_within_24h(published_dt):
        return None

    title = getattr(entry, "title", "").strip()
    summary = getattr(entry, "summary", "").strip()
    link = getattr(entry, "link", "").strip()

    if not title or not link:
        return None

    return {
        "title": title,
        "summary": summary,
        "date": published_dt.strftime("%d.%m.%Y"),
        "source": source_name,
        "link": link
    }


def parse_english_long_date(date_text):
    if not date_text:
        return None
    try:
        parsed = datetime.strptime(date_text, "%B %d, %Y")
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_nearby_treasury_date(anchor_tag):
    month_pattern = re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}"
    )
    node = anchor_tag
    for _ in range(7):
        node = node.parent
        if not node:
            break
        text = node.get_text(" ", strip=True)
        match = month_pattern.search(text)
        if match:
            return parse_english_long_date(match.group(0))
    return None


def fetch_us_treasury_candidate_news():
    source_name = "US Treasury"
    results = []
    try:
        response = requests.get(US_TREASURY_CANDIDATE_URL, headers=HEADERS, timeout=20, allow_redirects=True)
        if not check_response_status(response, source_name):
            return results
    except Exception:
        logging.exception("[US TREASURY CANDIDATE ERROR]")
        return results

    soup = BeautifulSoup(response.text, "html.parser")
    links = soup.find_all("a", href=True)
    log_source_count(source_name, len(links), "candidate page links", response.status_code)
    if not links:
        return results

    release_link_pattern = re.compile(r"^/news/press-releases/(sb|sm|jy|jl)\d+$", re.IGNORECASE)
    seen = set()

    for anchor in links:
        href = (anchor.get("href") or "").strip()
        if not release_link_pattern.match(href):
            continue

        title = anchor.get_text(" ", strip=True)
        if not title:
            continue

        published_dt = extract_nearby_treasury_date(anchor)
        if not published_dt or not is_within_24h(published_dt):
            continue

        full_link = urljoin(US_TREASURY_CANDIDATE_URL, href)
        key = (title, full_link)
        if key in seen:
            continue

        seen.add(key)
        results.append({
            "source": source_name,
            "title": title,
            "date": published_dt.strftime("%d.%m.%Y"),
            "link": full_link
        })

    logging.info("[US TREASURY CANDIDATE] items=%s", len(results))
    return results


def get_official_updates():
    updates = []

    for source_name, rss_url in RSS_SOURCES.items():
        try:
            response = requests.get(rss_url, headers=HEADERS, timeout=20, allow_redirects=True)

            if not check_response_status(response, source_name):
                if source_name == "US Treasury":
                    updates.extend(fetch_us_treasury_candidate_news())
                continue

            feed = feedparser.parse(response.content)
            total_entries = len(feed.entries)
            log_source_count(source_name, total_entries, "feed entries", response.status_code)

            if total_entries == 0:
                if source_name == "US Treasury":
                    updates.extend(fetch_us_treasury_candidate_news())
                continue

            for entry in feed.entries[:40]:
                normalized = normalize_entry(entry, source_name)
                if normalized:
                    updates.append(normalized)

        except Exception:
            logging.exception("[RSS ERROR] %s", source_name)
            if source_name == "US Treasury":
                updates.extend(fetch_us_treasury_candidate_news())

    return updates


def fetch_ofac_news():
    results = []
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%d.%m.%Y")
    start_date = yesterday.strftime("%m/%d/%Y")

    OFAC_URL = f"https://ofac.treasury.gov/recent-actions?ra-start-date={start_date}"

    try:
        response = requests.get(OFAC_URL, headers=HEADERS, timeout=20)
        if not check_response_status(response, "OFAC"):
            return results
    except Exception:
        logging.exception("[OFAC ERROR]")
        return results

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select(".views-row")
    log_source_count("OFAC", len(items), "raw items", response.status_code)
    if not items:
        return results

    for item in items:
        title_tag = item.select_one("a")
        date_tag = item.select_one(".date-display-single")

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        link = title_tag.get("href", "")
        if not link.startswith("http"):
            link = "https://ofac.treasury.gov" + link

        results.append({
            "source": "OFAC",
            "title": title,
            "date": yesterday_str,
            "link": link
        })

    # logging.info("[OFAC] Found %s news items for %s", len(results), yesterday.strftime("%d.%m.%Y"))
    return results


def fetch_eurlex():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%d%m%Y")
    yesterday_formatted = datetime.strptime(yesterday, "%d%m%Y").strftime("%d.%m.%Y")
    url = f"https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?&ojDate={yesterday}"

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-dbus")
    options.add_argument("--disable-gpu")

    # Server by default, local path when enable_local_test_mode() is called.
    options.binary_location = get_chrome_binary_location()

    driver = None
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        driver.get(url)
        time.sleep(1)

        if detect_html_error_page(f"{driver.title} {driver.page_source[:1200]}", "Eur-lex acts"):
            return []

        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="legal-content"][href*="uri=OJ:L_"]')))

        time.sleep(1)

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Debug
        # with open("debug_eurlex_page.html", "w", encoding="utf-8") as f:
        #     f.write(driver.page_source)
        # logging.info("HTML saved to debug_eurlex_page.html")

        act_links = soup.select('a[href*="legal-content"][href*="uri=OJ:L_"]')
        log_source_count("Eur-lex acts", len(act_links), "raw items")
        if not act_links:
            return []
        # logging.info("Eur-Lex links found: %s", len(act_links))

        results = []
        for link in act_links:
            title = link.text.strip()
            full_url = urljoin("https://eur-lex.europa.eu", link['href'])
            results.append({
                "source": "Eur-lex acts",
                "title": title,
                "date": yesterday_formatted,
                "link": full_url
            })
        return results

    except Exception:
        logging.exception("Error fetching Eur-Lex")
        return []
    finally:
        if driver:
            driver.quit()


def fetch_bis_news():
    target_url = "https://www.bis.gov/news-updates"
    yesterday = (datetime.now() - timedelta(days=1)).date()  # For tests 30 days

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-dbus")
    options.add_argument("--disable-gpu")

    # Server by default, local path when enable_local_test_mode() is called.
    options.binary_location = get_chrome_binary_location()

    driver = None
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        driver.get(target_url)
        time.sleep(3)

        if detect_html_error_page(f"{driver.title} {driver.page_source[:1200]}", "BIS GOV"):
            return []

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Debug
        # with open("debug_bis_page.html", "w", encoding="utf-8") as f:
        #     f.write(driver.page_source)
        # logging.info("HTML saved to debug_bis_page.html")

        results = []

        news_items = soup.find_all('li', attrs={'data-date': True})
        log_source_count("BIS GOV", len(news_items), "raw items")
        if not news_items:
            return []

        for item in news_items:
            date_span = item.find('span', class_='date')

            date_text = date_span.get_text(strip=True)
            if not date_text:
                continue

            try:
                item_date = datetime.strptime(date_text, "%B %d, %Y").date()
            except ValueError:
                try:
                    item_date = datetime.strptime(date_text, "%b %d, %Y").date()
                except ValueError:
                    continue

            if item_date < yesterday:
                continue

            link_tag = item.find('a', href=True)

            if link_tag:
                title_tag = link_tag.find('h3', class_=lambda c: c and 'font-bold' in c)
                if title_tag:
                    title = title_tag.get_text(strip=True)
                else:
                    title = link_tag.get_text(strip=True)

                relative_link = link_tag.get('href')
                if relative_link:
                    full_link = urljoin(target_url, relative_link)
                    formatted_date = item_date.strftime("%d.%m.%Y")
                    results.append({
                        "source": "BIS GOV",
                        "title": title,
                        "date": formatted_date,
                        "link": full_link
                    })
        return results

    except Exception:
        logging.exception("Error fetching BIS news")
        return []
    finally:
        if driver:
            driver.quit()


def fetch_uksi_sanctions():
    off_url = "https://www.legislation.gov.uk/uksi"
    url = "https://www.legislation.gov.uk/title/sanctions/data.feed"
    yesterday_formatted = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")

    results = []
    try:
        response = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        if not check_response_status(response, "UK Statutory Instruments"):
            return []

        feed = feedparser.parse(response.content)
        log_source_count("UK Statutory Instruments", len(feed.entries), "feed entries", response.status_code)
        if not feed.entries:
            return []

        for entry in feed.entries[:20]:
            published = entry.get('published_parsed') or entry.get('updated_parsed')
            if published:
                pub_date = datetime(*published[:6])
                if datetime.now() - pub_date > timedelta(days=2):
                    continue

            title = entry.get('title', '').strip()
            link = entry.get('link', '').strip()

            keywords = ['sanctions', 'financial', 'trade', 'export', 'russia',
                        'belarus', 'iran', 'ukraine', 'asset freeze']
            if any(kw in title.lower() for kw in keywords):
                results.append({
                    "source": "UK Statutory Instruments",
                    "title": title,
                    "date": yesterday_formatted,
                    "link": link
                })

    except Exception:
        logging.exception("[UKSI ATOM ERROR]")
        return []

    return results[:10]


def fetch_uk_designations_updates():
    url = "https://www.gov.uk/guidance/russia-list-of-designations-and-sanctions-notices"
    updates = []

    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        if not check_response_status(response, "UK Russia designations"):
            return updates
    except Exception:
        logging.exception("[UK DESIGNATIONS ERROR]")
        return updates

    soup = BeautifulSoup(response.text, 'html.parser')

    history_anchor = soup.find(id='full-publication-update-history')
    if not history_anchor:
        log_source_count("UK Russia designations", 0, "history items", response.status_code)
        return updates

    history_list = history_anchor.find_next('ol')
    if not history_list:
        log_source_count("UK Russia designations", 0, "history items", response.status_code)
        return updates

    history_items = history_list.find_all('li')
    log_source_count("UK Russia designations", len(history_items), "history items", response.status_code)
    if not history_items:
        return updates

    notices = {}
    notice_links = soup.select('a[href*=".pdf"]')
    for link in notice_links:
        notice_text = link.get_text(strip=True)
        try:
            notice_date_str = notice_text.split(',')[-1].strip()
            notice_date = datetime.strptime(notice_date_str, "%d %B %Y").replace(tzinfo=timezone.utc)
            notices[notice_date.strftime("%Y-%m-%d")] = {
                "title": notice_text,
                "link": "https://www.gov.uk" + link['href'] if not link['href'].startswith('http') else link['href']
            }
        except ValueError:
            pass

    for li in history_items:
        update_text = li.get_text(strip=True)
        if not update_text:
            continue

        try:
            date_parts = update_text.split()[:3]
            date_str = ' '.join(date_parts)
            published_dt = datetime.strptime(date_str, "%d %B %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if not is_within_24h(published_dt):
            continue

        title = update_text[len(date_str):].strip()

        date_key = published_dt.strftime("%Y-%m-%d")
        link = notices.get(date_key, {}).get("link", url)
        if link != url:
            title = notices.get(date_key, {}).get("title", title)

        updates.append({
            "source": "UK Russia designations",
            "title": title[:100],
            "date": published_dt.strftime("%d.%m.%Y"),
            "link": link
        })

    return updates


def parse_ukrainian_date(date_str: str) -> datetime:
    months = {
        'січня': 1, 'лютого': 2, 'березня': 3, 'квітня': 4, 'травня': 5,
        'червня': 6, 'липня': 7, 'серпня': 8, 'вересня': 9, 'жовтня': 10,
        'листопада': 11, 'грудня': 12
    }
    try:
        parts = date_str.split()
        day = int(parts[0])
        month_str = parts[1].lower()
        year = int(parts[2])
        month = months.get(month_str)
        if not month:
            raise ValueError
        return datetime(year, month, day, tzinfo=timezone.utc)
    except Exception:
        return None

# Not used - Bad request
def fetch_ukraine_president_decrees():
    base_url = "https://www.president.gov.ua"
    url = f"{base_url}/documents/decrees"
    results = []

    keywords = ['санкції', 'рішення рнбо', 'персональні санкції',
                'про застосування', 'санкцій', 'ради національної безпеки і оборони']

    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        if not check_response_status(response, "President Gov UA"):
            return []
        soup = BeautifulSoup(response.text, 'html.parser')
        decree_items = soup.find_all('li')
        log_source_count("President Gov UA", len(decree_items), "raw items", response.status_code)
        if not decree_items:
            return []

        decree_items = soup.find_all('li')

        for li in decree_items:
            link_tag = li.find('a', href=True)
            if not link_tag or not link_tag['href'].startswith('/documents/'):
                continue

            full_text = li.get_text(strip=True)
            number_title = link_tag.get_text(strip=True)

            text_after_link = full_text[len(number_title):].strip()
            date_end = text_after_link.find('року') + len('року')
            date_str = text_after_link[:date_end].strip()
            description = text_after_link[date_end:].strip()

            doc_date = parse_ukrainian_date(date_str)
            if not doc_date or not is_within_24h(doc_date):
                continue

            title = f"{number_title} {description}"
            title_lower = title.lower()
            if any(keyword in title_lower for keyword in keywords):
                full_link = urljoin(base_url, link_tag['href'])
                results.append({
                    "source": "President Gov UA",
                    "title": title,
                    "date": doc_date.strftime("%d.%m.%Y"),
                    "link": full_link
                })

    except Exception:
        logging.exception("[UKRAINE DECREES ERROR]")
        return []

    return results


# Not used  
def fetch_mofcom():
    url = "http://english.mofcom.gov.cn/article/policyrelease/"
    results = []

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-dbus")
    options.add_argument("--disable-gpu")

    # Server by default, local path when enable_local_test_mode() is called.
    options.binary_location = get_chrome_binary_location()

    def parse_mofcom_date(text):
        patterns = [
            (r"\b\d{4}-\d{2}-\d{2}\b", "%Y-%m-%d"),
            (r"\b\d{4}/\d{2}/\d{2}\b", "%Y/%m/%d"),
            (r"\b[A-Z][a-z]+ \d{1,2}, \d{4}\b", "%B %d, %Y"),
            (r"\b[A-Z][a-z]{2} \d{1,2}, \d{4}\b", "%b %d, %Y"),
        ]

        for pattern, fmt in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            try:
                parsed = datetime.strptime(match.group(0), fmt)
                return parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        return None

    driver = None
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        driver.get(url)

        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "a")))
        time.sleep(2)

        if detect_html_error_page(f"{driver.title} {driver.page_source[:1200]}", "MOFCOM"):
            return []

        soup = BeautifulSoup(driver.page_source, "html.parser")
        candidate_nodes = soup.select(
            "li, tr, .list-item, .txtList li, .publishList li, .commonList li"
        )
        log_source_count("MOFCOM", len(candidate_nodes), "raw items")
        if not candidate_nodes:
            return []

        seen = set()
        for node in candidate_nodes:
            link_tag = node.find("a", href=True)
            if not link_tag:
                continue

            href = link_tag.get("href", "").strip()
            title = link_tag.get_text(" ", strip=True)
            if not href or not title:
                continue

            full_link = urljoin(url, href)
            key = (title, full_link)
            if key in seen:
                continue

            node_text = node.get_text(" ", strip=True)
            published_dt = parse_mofcom_date(node_text)
            if not published_dt or not is_within_24h(published_dt):
                continue

            seen.add(key)
            results.append({
                "source": "MOFCOM",
                "title": title,
                "date": published_dt.strftime("%d.%m.%Y"),
                "link": full_link
            })

        return results

    except Exception:
        logging.exception("[MOFCOM ERROR]")
        return []
    finally:
        if driver:
            driver.quit()


def fetch_ukraine_sanctions():
    url = "https://www.president.gov.ua/documents/decrees"
    return None


def deduplicate(news):
    seen = set()
    unique = []

    for item in news:
        key = (item.get("title"), item.get("link"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique


def log_news(news):
    titles = []
    for i in news:
        res = f"{i['source']} --- {i['title'][:30]}"
        titles.append(res)
    return titles


def build_missing_summary_links(news):
    links_by_source = {}
    seen_by_source = {}

    for item in news:
        source = item.get("source")
        link = (item.get("link") or "").strip()
        summary = clean_summary_text(item.get("summary", ""))

        if not source or not link:
            continue
        # Skip RSS sources because they already provide useful summaries.
        if source in RSS_SOURCES:
            continue
        if summary and not is_low_quality_summary(summary):
            continue

        if source not in links_by_source:
            links_by_source[source] = []
            seen_by_source[source] = set()

        if link in seen_by_source[source]:
            continue

        seen_by_source[source].add(link)
        links_by_source[source].append(link)

    return links_by_source


def chunked(values, size):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def clean_summary_text(text):
    if not text:
        return ""
    cleaned = text.strip()
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)  # markdown images
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def is_low_quality_summary(text):
    cleaned = clean_summary_text(text).lower()
    if len(cleaned) < 80:
        return True
    bad_markers = [
        "an official website of the united states government",
        "here’s how you know",
        "here's how you know",
        "cookie",
        "javascript",
        "subscribe",
        "privacy policy",
    ]
    return any(marker in cleaned for marker in bad_markers)


def make_fallback_summary_from_title(item):
    title = (item.get("title") or "").strip()
    if not title:
        return ""
    return f"{title}."


def extract_summary_from_tavily_item(extract_item):
    if not isinstance(extract_item, dict):
        return ""

    # Prefer raw page content from Tavily for non-RSS sources.
    raw = (extract_item.get("raw_content") or "").strip()
    if raw:
        return raw[:1000]

    raw_context = (extract_item.get("raw_context") or "").strip()
    if raw_context:
        return raw_context[:1000]

    content = extract_item.get("content")
    if isinstance(content, list):
        joined = " ".join(str(x).strip() for x in content if str(x).strip()).strip()
        return joined[:1000]
    if isinstance(content, str):
        return content.strip()[:1000]

    return ""


def tavily_extract_urls(api_key, urls, extract_depth="advanced", timeout_sec=45.0, content_format="text"):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        TAVILY_EXTRACT_URL,
        headers=headers,
        json={
            "api_key": api_key,
            "urls": urls,
            "extract_depth": extract_depth,
            "format": content_format,
            "timeout": timeout_sec,
        },
        timeout=TAVILY_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def build_extract_candidates(item):
    link = (item.get("link") or "").strip()
    source = (item.get("source") or "").strip()
    candidates = []
    if link:
        candidates.append(link)

    # Eur-lex specific: try alternative permanent formats if TXT view fails.
    if source == "Eur-lex acts" and "/TXT/?uri=" in link:
        candidates.append(link.replace("/TXT/?uri=", "/TXT/HTML/?uri="))
        candidates.append(link.replace("/TXT/?uri=", "/TXT/PDF/?uri="))

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def extract_ofac_direct_summary(link):
    try:
        response = requests.get(link, headers=HEADERS, timeout=30, allow_redirects=True)
        response.raise_for_status()
    except Exception:
        logging.exception("[OFAC DIRECT] failed to load recent action page: %s", link)
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    article = soup.select_one("article")
    if not article:
        return ""

    parts = []
    for p in article.select(".field__item p, p"):
        text = clean_summary_text(p.get_text(" ", strip=True))
        if len(text) > 40:
            parts.append(text)

    # Use treasury press-release page when available for more context.
    press_link = article.select_one(".field--name-field-press-release-link a[href]")
    if press_link:
        press_url = urljoin(link, (press_link.get("href") or "").strip())
        if press_url:
            try:
                pr = requests.get(press_url, headers=HEADERS, timeout=30, allow_redirects=True)
                pr.raise_for_status()
                pr_soup = BeautifulSoup(pr.text, "html.parser")
                region = pr_soup.select_one(".region-content")
                if region:
                    for p in region.select("p"):
                        text = clean_summary_text(p.get_text(" ", strip=True))
                        if len(text) > 60 and "Role of the Treasury" not in text:
                            parts.append(text)
            except Exception:
                logging.exception("[OFAC DIRECT] failed to load press release page: %s", press_url)

    seen = set()
    unique_parts = []
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        unique_parts.append(part)

    return clean_summary_text(" ".join(unique_parts))[:2500]


def extract_eurlex_direct_summary(link):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-dbus")
    options.add_argument("--disable-gpu")
    options.binary_location = get_chrome_binary_location()

    driver = None
    html = ""
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(45)

        candidate_urls = [
            link,
            link.replace("/TXT/?uri=", "/TXT/HTML/?uri="),
            link.replace("/TXT/?uri=", "/TXT/PDF/?uri="),
        ]
        for candidate in candidate_urls:
            driver.get(candidate)
            try:
                WebDriverWait(driver, 12).until(
                    EC.any_of(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "#document1")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, "#text")),
                    )
                )
            except Exception:
                pass

            html = driver.page_source
            soup_try = BeautifulSoup(html, "html.parser")
            if soup_try.select_one("#document1") or soup_try.select_one("#text"):
                break
    except Exception:
        logging.exception("[EURLex DIRECT] failed to load page: %s", link)
        return ""
    finally:
        if driver:
            driver.quit()

    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    main_node = soup.select_one("#document1") or soup.select_one("#text")
    if not main_node:
        return ""

    text = clean_summary_text(main_node.get_text(" ", strip=True))
    text = re.sub(r"An official website of the European Union.*?Accept all cookies", " ", text, flags=re.I)
    text = re.sub(r"Image\s*\d+\s*:?", " ", text, flags=re.I)
    text = re.sub(r"L_\d+[A-Z]{2}\.\d+\d*\.fmx\.xml", " ", text, flags=re.I)
    text = clean_summary_text(text)
    return text[:3000]


def extract_source_specific_summary(item):
    source = (item.get("source") or "").strip()
    link = (item.get("link") or "").strip()
    if not link:
        return ""

    if source == "OFAC":
        return extract_ofac_direct_summary(link)
    if source == "Eur-lex acts":
        return extract_eurlex_direct_summary(link)
    return ""


def load_live_sources_config(path="live_sources"):
    try:
        content = Path(path).read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, list):
            return data
        return []
    except FileNotFoundError:
        logging.info("[LIVE SOURCES] file not found: %s", path)
        return []
    except Exception:
        logging.exception("[LIVE SOURCES] failed to read: %s", path)
        return []


def infer_source_name_from_link(link):
    clean_link = (link or "").strip()
    if not clean_link:
        return ""

    for source_name, rss_link in RSS_SOURCES.items():
        if clean_link == rss_link:
            return source_name

    for source_name, hint in SOURCE_LINK_HINTS.items():
        if hint in clean_link:
            return source_name

    if "state.gov/press-releases/feed" in clean_link:
        return "US State Dept"
    if "home.treasury.gov/news/press-releases/rss" in clean_link:
        return "US Treasury"
    return ""


def parse_problem_sources_from_log(log_path="local_test_run.log"):
    problem_sources = set()
    path = Path(log_path)
    if not path.exists():
        return problem_sources

    try:
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "[CHECK]" not in raw_line:
                continue
            match = re.search(r"\[CHECK\]\s+(.+?):\s+HTTP\s+(\d+)", raw_line)
            if match:
                src = match.group(1).strip()
                status = int(match.group(2))
                if status >= 400:
                    problem_sources.add(src)
                continue
            empty_match = re.search(r"\[CHECK\]\s+(.+?):\s+.*is empty", raw_line)
            if empty_match:
                problem_sources.add(empty_match.group(1).strip())
    except Exception:
        logging.exception("[LIVE SOURCES] failed to parse log: %s", log_path)

    return problem_sources


def get_summary_from_url(api_key, url, title_hint, source_hint):
    summary = ""
    try:
        payload = tavily_extract_urls(
            api_key=api_key,
            urls=[url],
            extract_depth="advanced",
            timeout_sec=55.0,
            content_format="text",
        )
        for res in payload.get("results", []):
            candidate_summary = clean_summary_text(extract_summary_from_tavily_item(res))
            if candidate_summary and not is_low_quality_summary(candidate_summary):
                summary = candidate_summary
                break
    except Exception:
        logging.exception("[TAVILY][PREFILTER EXTRACT] failed for url=%s", url)

    if not summary:
        item = {
            "title": title_hint,
            "link": url,
            "source": source_hint,
        }
        summary = tavily_search_fallback_summary(api_key, item)

    if not summary and title_hint:
        summary = clean_summary_text(f"{title_hint}.")

    return summary[:1000] if summary else ""


def prefilter_probe_with_live_sources(news, live_sources_path="live_sources", log_path="local_test_run.log"):
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        logging.warning("[PREFILTER] TAVILY_API_KEY is empty, prefilter probe skipped")
        return news

    live_entries = load_live_sources_config(live_sources_path)
    if not live_entries:
        return news

    problem_sources = parse_problem_sources_from_log(log_path)
    stub_or_disabled_sources = {"President Gov UA", "MOFCOM", "US Treasury"}

    news = list(news)
    news_by_source = {}
    for item in news:
        src = (item.get("source") or "").strip()
        if src:
            news_by_source.setdefault(src, []).append(item)

    updated_items = 0
    added_items = 0

    for entry in live_entries:
        if not isinstance(entry, dict):
            continue

        primary_link = (entry.get("link") or "").strip()
        if not primary_link:
            continue

        source_name = infer_source_name_from_link(primary_link)
        status_ok = bool(entry.get("status", False))
        work_ok = bool(entry.get("work_status", False))
        should_probe = (
            (not status_ok)
            or (not work_ok)
            or (source_name in problem_sources)
            or (source_name in stub_or_disabled_sources)
        )
        if not should_probe:
            continue

        candidates = [primary_link]
        extra = entry.get("candidates") or []
        if isinstance(extra, list):
            candidates.extend(str(x).strip() for x in extra if str(x).strip())
        candidates = list(dict.fromkeys(candidates))

        source_items = news_by_source.get(source_name, [])
        if source_items:
            for item in source_items:
                current_summary = clean_summary_text(item.get("summary", ""))
                if current_summary and not is_low_quality_summary(current_summary):
                    continue

                item_candidates = [item.get("link", "").strip()] + candidates + build_extract_candidates(item)
                item_candidates = [u for u in dict.fromkeys(item_candidates) if u]
                resolved = ""
                for cand_url in item_candidates:
                    resolved = get_summary_from_url(
                        api_key=api_key,
                        url=cand_url,
                        title_hint=item.get("title", ""),
                        source_hint=source_name or item.get("source", ""),
                    )
                    if resolved and not is_low_quality_summary(resolved):
                        break
                if resolved:
                    item["summary"] = resolved
                    updated_items += 1
        else:
            # No parsed items for this source: create candidate-based pseudo item.
            for cand_url in candidates:
                title_hint = f"Candidate source probe for {source_name or cand_url}"
                resolved = get_summary_from_url(
                    api_key=api_key,
                    url=cand_url,
                    title_hint=title_hint,
                    source_hint=source_name or "Candidate source",
                )
                if not resolved:
                    continue
                pseudo = {
                    "source": source_name or "Candidate source",
                    "title": title_hint,
                    "date": datetime.now(timezone.utc).strftime("%d.%m.%Y"),
                    "link": cand_url,
                    "summary": resolved,
                }
                news.append(pseudo)
                news_by_source.setdefault(pseudo["source"], []).append(pseudo)
                added_items += 1
                break

    news = deduplicate(news)
    logging.info(
        "[PREFILTER] completed: problem_sources=%s updated_items=%s added_items=%s total_news=%s",
        len(problem_sources),
        updated_items,
        added_items,
        len(news),
    )
    return news


def tavily_search_fallback_summary(api_key, item):
    title = (item.get("title") or "").strip()
    link = (item.get("link") or "").strip()
    if not title:
        return ""

    domain = urlparse(link).netloc if link else ""
    query = title if not domain else f"{title} site:{domain}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "api_key": api_key,
        "query": query,
        "topic": "general",
        "search_depth": "advanced",
        "max_results": 1,
        "include_raw_content": "text",
    }
    if domain:
        payload["include_domains"] = [domain]

    try:
        response = requests.post(TAVILY_SEARCH_URL, headers=headers, json=payload, timeout=40)
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        if not results:
            return ""

        top = results[0]
        raw = (top.get("raw_content") or "").strip()
        content = (top.get("content") or "").strip()
        summary = raw[:1000] if raw else content[:1000]
        return clean_summary_text(summary)
    except Exception:
        logging.exception("[TAVILY][SEARCH] fallback failed for link=%s", link)
        return ""


def enrich_news_with_tavily_summary(news):
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        logging.warning("[TAVILY] TAVILY_API_KEY is empty, enrichment skipped")
        return news

    # First pass: source-specific extraction for problematic structures.
    source_specific_updates = 0
    for item in news:
        source = item.get("source")
        if source in RSS_SOURCES:
            continue
        current = clean_summary_text(item.get("summary", ""))
        if current and not is_low_quality_summary(current):
            continue

        direct = clean_summary_text(extract_source_specific_summary(item))
        if direct and not is_low_quality_summary(direct):
            item["summary"] = direct
            source_specific_updates += 1

    if source_specific_updates:
        logging.info("[DIRECT EXTRACT] source-specific summaries updated=%s", source_specific_updates)

    links_by_source = build_missing_summary_links(news)
    if not links_by_source:
        logging.info("[TAVILY] No missing summaries, enrichment skipped")
        return news

    link_to_summary = {}
    total_links = sum(len(v) for v in links_by_source.values())
    logging.info("[TAVILY] Enrichment started: %s links from %s sources", total_links, len(links_by_source))

    failed_links = set()
    for source_name, source_links in links_by_source.items():
        for batch in chunked(source_links, TAVILY_BATCH_SIZE):
            try:
                payload = tavily_extract_urls(
                    api_key=api_key,
                    urls=batch,
                    extract_depth="advanced",
                    timeout_sec=45.0,
                    content_format="text",
                )
            except requests.exceptions.Timeout:
                logging.warning("[TAVILY] Timeout for source=%s batch_size=%s", source_name, len(batch))
                failed_links.update(batch)
                continue
            except Exception:
                logging.exception("[TAVILY] Extract error for source=%s batch_size=%s", source_name, len(batch))
                failed_links.update(batch)
                continue

            results = payload.get("results", [])
            failed_results = payload.get("failed_results", [])
            if not isinstance(results, list):
                logging.warning("[TAVILY] Invalid response format for source=%s", source_name)
                failed_links.update(batch)
                continue

            success_urls = set()
            for item in results:
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                success_urls.add(url)
                summary = extract_summary_from_tavily_item(item)
                summary = clean_summary_text(summary)
                if summary and not is_low_quality_summary(summary):
                    link_to_summary[url] = summary

            for fr in failed_results if isinstance(failed_results, list) else []:
                fr_url = (fr.get("url") or "").strip() if isinstance(fr, dict) else ""
                if fr_url:
                    failed_links.add(fr_url)
            for requested_url in batch:
                if requested_url not in success_urls:
                    failed_links.add(requested_url)

            logging.info(
                "[TAVILY] source=%s batch=%s processed, summaries_collected=%s",
                source_name,
                len(batch),
                len(link_to_summary)
            )

    # Per-link fallback for missing/low-quality summaries (extract candidates + Tavily search).
    for item in news:
        source = item.get("source")
        if source in RSS_SOURCES:
            continue
        link = (item.get("link") or "").strip()
        existing = clean_summary_text(item.get("summary", ""))
        if existing and not is_low_quality_summary(existing):
            continue

        if link in link_to_summary and not is_low_quality_summary(link_to_summary[link]):
            continue

        fallback_summary = ""
        candidates = build_extract_candidates(item)
        if candidates:
            try:
                payload = tavily_extract_urls(
                    api_key=api_key,
                    urls=candidates,
                    extract_depth="advanced",
                    timeout_sec=50.0,
                    content_format="text",
                )
                for res in payload.get("results", []):
                    candidate_summary = clean_summary_text(extract_summary_from_tavily_item(res))
                    if candidate_summary and not is_low_quality_summary(candidate_summary):
                        fallback_summary = candidate_summary
                        break
            except Exception:
                logging.exception("[TAVILY][FALLBACK EXTRACT] failed for link=%s", link)

        if not fallback_summary:
            fallback_summary = tavily_search_fallback_summary(api_key, item)

        if not fallback_summary:
            fallback_summary = make_fallback_summary_from_title(item)

        if fallback_summary:
            link_to_summary[link] = fallback_summary

    enriched_count = 0
    for item in news:
        source = item.get("source")
        if source in RSS_SOURCES:
            continue
        link = (item.get("link") or "").strip()
        summary = link_to_summary.get(link, "")
        if summary:
            item["summary"] = summary
            enriched_count += 1

    logging.info(
        "[TAVILY] Enrichment finished: %s/%s items updated, failed_links=%s",
        enriched_count,
        len(news),
        len(failed_links),
    )
    return news


def collect_all_news():
    news = []

    news.extend(fetch_bis_news())
    logging.info("+ BIS total: %s", len(news))
    logging.info("")
    news.extend(fetch_eurlex())
    logging.info("+ Eur-lex Acts total: %s", len(news))
    logging.info("")
    rss_news = get_official_updates()
    news.extend(rss_news)

    rss_counter = Counter(item['source'] for item in rss_news)
    logging.info("RSS: ...")
    for source, count in rss_counter.items():
        logging.info("   %s: %s", source, count)

    logging.info("+ Rss sources total: %s", len(news))
    logging.info("")
    news.extend(fetch_ofac_news())
    logging.info("+ OFAC total: %s", len(news))
    news.extend(fetch_uksi_sanctions())
    logging.info("+ UKSI total: %s", len(news))
    news.extend(fetch_uk_designations_updates())
    logging.info("+ UK Russia designations total: %s", len(news))
    # news.extend(fetch_ukraine_president_decrees())
    # logging.info("+ President Gov UA total: %s", len(news))

    news = deduplicate(news)

    return news


if __name__ == "__main__":
    # Local run helper: does not affect imports/calls from manage.py
    enable_local_test_mode(log_to_file=False)
    parsed_raw = collect_all_news()
    first_checked = first_check_filter_news(parsed_raw)
    first_check_enriched = enrich_news_with_tavily_summary(first_checked)
    summary_rewritten = rewrite_summaries_with_model(first_check_enriched)
    dump_local_pipeline_json(parsed_raw, first_check_enriched, summary_rewritten)
    logging.info("[LOCAL TEST] raw total: %s", len(parsed_raw))
    logging.info("[LOCAL TEST] first_check total: %s", len(first_checked))
    logging.info("[LOCAL TEST] first_check_enriched with summary: %s", sum(1 for i in first_check_enriched if (i.get("summary") or "").strip()))
    logging.info("[LOCAL TEST] summary_rewritten with summary: %s", sum(1 for i in summary_rewritten if (i.get("summary") or "").strip()))


# python sources_big.py *> local_test_run.log
# TODO add another layer off request to openrouter gpt for rewrite summarize for final svodka
