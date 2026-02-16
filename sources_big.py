import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime


TIME_WINDOW = timedelta(hours=36)


RSS_SOURCES = {
    "EU Commission": "https://ec.europa.eu/commission/presscorner/api/rss",
    "EU Council": "https://www.consilium.europa.eu/en/press/press-releases/rss/",
    "UK OFSI": "https://www.gov.uk/government/organisations/office-of-financial-sanctions-implementation.atom",
    "UK FCDO": "https://www.gov.uk/government/organisations/foreign-commonwealth-development-office.atom",
    "US Treasury": "https://home.treasury.gov/news/press-releases/rss",
    "US State Dept": "https://www.state.gov/press-releases/feed/",
    "BIS": "https://www.bis.gov/feeds/news.xml"
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


OFAC_URL = "https://ofac.treasury.gov/recent-actions"


def parse_ofac_date(text):
    try:
        dt = datetime.strptime(text.strip(), "%B %d, %Y")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fetch_ofac_news():
    results = []

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

        if not link:
            continue

        if not link.startswith("http"):
            link = "https://ofac.treasury.gov" + link

        published_dt = None
        if date_tag:
            published_dt = parse_ofac_date(date_tag.get_text())

        if published_dt and not is_within_24h(published_dt):
            continue

        results.append({
            "source": "OFAC",
            "title": title,
            "date": published_dt.strftime("%d.%m.%Y") if published_dt else "",
            "link": link
        })

    return results


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
        titles.append(i['title'][:50])
    return titles


def collect_all_news():
    news = []

    news.extend(get_official_updates())
    news.extend(fetch_ofac_news())

    news = deduplicate(news)

    now = datetime.now(timezone.utc)

    filtered_news = []
    for n in news:
        date_str = n.get('date', '')
        if not date_str:
            continue
        try:
            published_dt = datetime.strptime(date_str, "%d.%m.%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now - published_dt <= TIME_WINDOW:
            filtered_news.append(n)

    news = filtered_news
    print(len(news), log_news(news))

    return news
