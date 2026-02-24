import feedparser
import requests
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta
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
    "US Treasury",
    "US State Dept",
    "OFAC",
    "Eur-lex acts",
    "BIS.GOV"
]

RSS_SOURCES = {
    "EU Commission":
    "https://ec.europa.eu/commission/presscorner/api/rss",
    "EU Council":
    "https://www.consilium.europa.eu/en/press/press-releases/rss/",
    "UK OFSI":
    "https://www.gov.uk/government/organisations/office-of-financial-sanctions-implementation.atom",
    "UK FCDO":
    "https://www.gov.uk/government/organisations/foreign-commonwealth-development-office.atom",
    "US Treasury":
    "https://home.treasury.gov/news/press-releases/rss",
    "US State Dept":
    "https://www.state.gov/press-releases/feed/"
}


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}


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
            feed = feedparser.parse(rss_url)

            for entry in feed.entries[:40]:
                normalized = normalize_entry(entry, source_name)
                if normalized:
                    updates.append(normalized)

        except Exception as e:
            print(f"[RSS ERROR] {source_name}: {e}")

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
        response.raise_for_status()
    except Exception as e:
        print(f"[OFAC ERROR] {e}")
        return results

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select(".views-row")

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

    # print(f"[OFAC] Found {len(results)} news items for {yesterday.strftime('%d.%m.%Y')}")
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

        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="legal-content"][href*="uri=OJ:L_"]')))

        time.sleep(1)

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Debug
        # with open("debug_eurlex_page.html", "w", encoding="utf-8") as f:
        #     f.write(driver.page_source)
        # print("HTML сохранён в debug_eurlex_page.html")

        act_links = soup.select('a[href*="legal-content"][href*="uri=OJ:L_"]')
        # print(f"Найдено ссылок Eur-Lex: {len(act_links)}")

        results = []
        for link in act_links:
            title = link.text.strip()
            full_url = urljoin("https://eur-lex.europa.eu", link['href'])
            results.append({
                "source": "eur-lex acts",
                "title": title,
                "date": yesterday_formatted,
                "link": full_url
            })
        return results

    except Exception as e:
        print(f"Error fetching Eur-Lex: {e}")
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

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Debug
        # with open("debug_bis_page.html", "w", encoding="utf-8") as f:
        #     f.write(driver.page_source)
        # print("HTML сохранён в debug_bis_page.html")

        results = []

        news_items = soup.find_all('li', attrs={'data-date': True})

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
                        "source": "BIS news",
                        "title": title,
                        "date": formatted_date,
                        "link": full_link
                    })
        return results

    except Exception as e:
        print(f"Error fetching BIS news: {e}")
        return []
    finally:
        if driver:
            driver.quit()


def fetch_uksi_sanctions():
    # Not elevated news
    url = "https://www.legislation.gov.uk/uksi"
    return None


def fetch_mofcom():
    url = "http://english.mofcom.gov.cn/article/policyrelease/"
    return None


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
    print("+ BIS", len(news), log_news(news))
    print()
    news.extend(fetch_eurlex())
    print("+ Eur-lex Acts", len(news), log_news(news))
    print()
    news.extend(get_official_updates())
    print("+ Rss sources", len(news), log_news(news))
    print()
    news.extend(fetch_ofac_news())
    print("+ OFAC", len(news), log_news(news))
    news = deduplicate(news)
    return news

