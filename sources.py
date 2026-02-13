import feedparser
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta


TIME_WINDOW = timedelta(days=1)


RSS_SOURCES = {
    # "OFAC": "https://ofac.treasury.gov/rss.xml", # html parsing
    # "BIS": "https://www.bis.gov/news.xml" # Not supported (temp disable)
    "GOV.UK": "https://www.gov.uk/government/organisations/foreign-commonwealth-development-office.atom",
    "EU": "https://ec.europa.eu/commission/presscorner/api/rss"
}


def is_within_24h(published_dt: datetime) -> bool:
    now = datetime.now(timezone.utc)
    return now - published_dt <= TIME_WINDOW


def parse_entry_date(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
    return None


def normalize_entry(entry, source_name):
    published_dt = parse_entry_date(entry)
    if not published_dt:
        return None

    if not is_within_24h(published_dt):
        return None

    return {
        "title": entry.title.strip(),
        "summary": entry.summary.strip() if hasattr(entry, "summary") else "",
        "date": published_dt.strftime("%d.%m.%Y"),
        "source": source_name,
        "link": entry.link
    }


def get_official_updates():
    updates = []

    for source_name, rss_url in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(rss_url)

            for entry in feed.entries[:15]:
                normalized = normalize_entry(entry, source_name)
                if normalized:
                    updates.append(normalized)

        except Exception as e:
            print(f"[RSS ERROR] {source_name}: {e}")


    return updates


OFAC_BASE = "https://ofac.treasury.gov/recent-actions"


def fetch_ofac_news():
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%m/%d/%Y")
    url = f"{OFAC_BASE}?ra-start-date={yesterday}"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": "https: // ofac.treasury.gov / recent - actions?search_api_fulltext = & ra - start - date = 02 % 2F12 % 2F2026 & ra - end - date = & ra_year ="
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print("OFAC request error:", e)
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    results = []

    # Найти блоки с результатами (OFAC использует Drupal views)
    items = soup.select(".views-row")

    for item in items:
        title_tag = item.find("a")
        date_tag = item.find("span", class_="date-display-single")

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        link = "https://ofac.treasury.gov" + title_tag["href"]

        date_text = date_tag.get_text(strip=True) if date_tag else None

        results.append({
            "source": "OFAC",
            "title": title,
            "date": str(date_text),
            "link": link
        })

    return results


def collect_all_news():
    news = []
    news.extend(get_official_updates())
    news.extend(fetch_ofac_news())
    print(len(news), news[0])
    return news
