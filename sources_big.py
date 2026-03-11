import feedparser
import logging
import requests
import re
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
urllib3.disable_warnings()
TIME_WINDOW = timedelta(hours=28)


SOURCE_KEYS = [
    "EU Commission",
    "EU Council",
    "UK OFSI",
    "UK FCDO",
    # "US Treasury",
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


def get_official_updates():
    updates = []

    for source_name, rss_url in RSS_SOURCES.items():
        try:
            response = requests.get(rss_url, headers=HEADERS, timeout=20, allow_redirects=True)

            if not check_response_status(response, source_name):
                continue

            feed = feedparser.parse(response.content)
            total_entries = len(feed.entries)
            log_source_count(source_name, total_entries, "feed entries", response.status_code)

            if total_entries == 0:
                continue

            for entry in feed.entries[:40]:
                normalized = normalize_entry(entry, source_name)
                if normalized:
                    updates.append(normalized)

        except Exception:
            logging.exception("[RSS ERROR] %s", source_name)

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

    # For prod
    options.binary_location = "/opt/google/chrome/chrome"

    # For local test
    # options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

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

    # For prod
    options.binary_location = "/opt/google/chrome/chrome"

    # For local test
    # options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

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

    # For prod
    options.binary_location = "/opt/google/chrome/chrome"

    # For local test
    # options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

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

# Test
# logging.info(collect_all_news())
