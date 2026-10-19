"""Local document extraction with a persistent, budgeted Tavily fallback."""
import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
import trafilatura
from bs4 import BeautifulSoup
from pypdf import PdfReader

MAX_BYTES = 15 * 1024 * 1024
MAX_TEXT = 6000
HEADERS = {"User-Agent": "Mozilla/5.0"}
CONTENT_CACHE_VERSION = 2


def recent_changes(details, now=None):
    now = now or datetime.now(timezone.utc)
    result = []
    for entry in details.get('change_history', []):
        try:
            stamp = datetime.fromisoformat(entry['public_timestamp'].replace('Z', '+00:00'))
            if timedelta(0) <= now - stamp <= timedelta(hours=48) and clean(entry.get('note', '')):
                result.append((entry, stamp))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(result, key=lambda pair: pair[1], reverse=True)


def clean(text):
    if re.search(r"</?(?:p|div|section|span|html|br)\b", text or "", re.I):
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text or "").strip()


def usable(text):
    text = clean(text)
    prefix = text[:800].lower()
    return len(text) >= 80 and not any(marker in prefix for marker in (
        "skip to main content", "here's how you know", "here’s how you know",
        "checking your browser", "enable javascript and cookies", "access denied",
        "an official website of the united states government",
    ))


class Store:
    def __init__(self, path=None):
        self.path = str(path or os.getenv("CONTENT_STATE_PATH") or
                        Path(__file__).with_name("content_state.sqlite3"))
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, value TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS spend (id TEXT PRIMARY KEY, day TEXT, month TEXT, credits REAL)")
            db.execute("DELETE FROM cache WHERE expires <= ?", (time.time(),))
            month = datetime.now(timezone(timedelta(hours=int(os.getenv("TAVILY_DAY_UTC_OFFSET", "3"))))).strftime('%Y-%m')
            db.execute("DELETE FROM spend WHERE month < ?", (month,))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                       (key, time.time() + 6 * 3600, json.dumps(value, ensure_ascii=False)))

    def reserve(self, credits):
        # Moscow calendar days by default; independent of the server timezone.
        now = datetime.now(timezone(timedelta(hours=int(os.getenv("TAVILY_DAY_UTC_OFFSET", "3")))))
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        daily = float(os.getenv("TAVILY_DAILY_CREDITS", "30"))
        monthly = float(os.getenv("TAVILY_MONTHLY_CREDITS", "900"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            today = db.execute("SELECT COALESCE(SUM(credits),0) FROM spend WHERE day=?", (day,)).fetchone()[0]
            used = db.execute("SELECT COALESCE(SUM(credits),0) FROM spend WHERE month=?", (month,)).fetchone()[0]
            if today + credits > daily or used + credits > monthly:
                return None
            token = uuid.uuid4().hex
            db.execute("INSERT INTO spend VALUES (?,?,?,?)", (token, day, month, credits))
        return token

    def settle(self, token, credits):
        with self.connect() as db:
            db.execute("UPDATE spend SET credits=? WHERE id=?", (credits, token))


def advanced_extract(api_key, urls, store=None, query=None):
    """Reserve an upper bound before sending; uncertain requests keep the reserve."""
    if not api_key or not urls:
        return {}
    store = store or Store()
    reserve = 2 * math.ceil(len(urls) / 5)
    headers = {"Authorization": "Bearer " + api_key}
    try:
        usage_response = requests.get("https://api.tavily.com/usage", headers=headers, timeout=15)
        usage_response.raise_for_status()
        usage = usage_response.json()
        account, key = usage["account"], usage["key"]
        # Never intentionally use pay-as-you-go credits.
        remaining = float(account["plan_limit"]) - float(account["plan_usage"])
        if key.get("limit") is not None:
            remaining = min(remaining, float(key["limit"]) - float(key["usage"]))
        if remaining < reserve:
            logging.warning("[CONTENT] Tavily plan/key allowance exhausted")
            return {}
        token = store.reserve(reserve)
        if not token:
            logging.warning("[CONTENT] Tavily daily/monthly budget exhausted")
            return {}
    except Exception as exc:
        logging.warning("[CONTENT] Tavily budget check unavailable: %s", type(exc).__name__)
        return {}
    payload = {"urls": urls, "extract_depth": "advanced", "format": "text",
               "timeout": 45, "include_usage": True}
    if query:
        payload.update(query=query, chunks_per_source=5)
    try:
        response = requests.post("https://api.tavily.com/extract", headers=headers,
                                 json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        actual = data.get("usage", {}).get("credits")
        if isinstance(actual, (int, float)) and math.isfinite(actual) and actual >= 0:
            store.settle(token, actual)
        logging.info("[CONTENT] Tavily advanced URLs=%s credits=%s", len(urls), actual if actual is not None else reserve)
        return data
    except Exception as exc:
        logging.warning("[CONTENT] Tavily failed; budget reservation retained: %s", type(exc).__name__)
        return {}


class Extractor:
    def __init__(self, store=None):
        self.store = store or Store()
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def govuk_updates(self, item):
        """Expand standing-page history into dated events, never into a digest."""
        url = item['link']
        body, _ = self.download('https://www.gov.uk/api/content' + urlsplit(url).path)
        data = json.loads(body)
        details = data.get('details', {})
        sanctions_index = urlsplit(url).path.rstrip('/') == '/government/publications/the-uk-sanctions-list'
        # Articles have their own substantive body, even when corrected later.
        if data.get('document_type') in ('news_story', 'press_release', 'speech', 'statement_to_parliament'):
            return None
        if not details.get('change_history'):
            return [] if sanctions_index else None
        if all(clean(e.get('note', '')).lower().rstrip('.') in ('first published', 'published')
               for e in details['change_history']):
            return None
        events = []
        seen = set()
        for entry, stamp in recent_changes(details):
            note = clean(entry['note'])
            if note.lower().rstrip('.') in ('first published', 'published'):
                continue
            if sanctions_index:
                # This index covers all regimes. Retain only explicit Russia changes.
                clauses = re.split(r'(?<=[.;])\s+|\s+and\s+(?=\d+\s+(?:correction|variation|amendment|designation|specification))', note, flags=re.I)
                note = ' '.join(c for c in clauses if re.search(r'\brussia(?:n)?\b', c, re.I))
                if not note:
                    continue
            identity = entry['public_timestamp'] + '\n' + note
            if identity in seen:
                continue
            seen.add(identity)
            event = dict(item)
            event.update(title=f"{item.get('title', data.get('title', 'GOV.UK'))}: {note}",
                         date=stamp.strftime('%d.%m.%Y'), summary=note,
                         link=url.split('#')[0],
                         summary_origin='govuk_change', summary_status='extracted',
                         content_urls=[url.split('#')[0]], event_timestamp=entry['public_timestamp'])
            events.append(event)
        return events

    def download(self, url):
        with self.session.get(url, timeout=(10, 30), stream=True) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Empty or deferred response")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Document exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("Content-Type", "").lower()

    def document(self, url):
        body, content_type = self.download(url)
        if body.startswith(b"%PDF") or "application/pdf" in content_type:
            pdf = PdfReader(io.BytesIO(body))
            # Scans produce no useful text and go to the normal fallback.
            return clean(" ".join(page.extract_text() or "" for page in pdf.pages[:40]))[:60000]
        if "xml" in content_type:
            root = ET.fromstring(body)
            notes = [node for node in root.iter() if node.tag.split("}")[-1] == "ExplanatoryNotes"]
            return clean(" ".join(" ".join(node.itertext()) for node in (notes or [root])))[:60000]
        return clean(trafilatura.extract(body, include_tables=True, include_comments=False) or "")[:60000]

    def govuk(self, url):
        body, _ = self.download("https://www.gov.uk/api/content" + urlsplit(url).path)
        data = json.loads(body)
        details = data.get("details", {})
        # Histories are expanded separately by govuk_updates; ordinary articles
        # use only their own body, never a collection of revision notes.
        parts, links = [clean(details.get("body", ""))[:MAX_TEXT]], [url]
        return clean(" ".join(parts))[:MAX_TEXT], links

    def local(self, item):
        url = item["link"]
        host = urlsplit(url).hostname or ""
        if host == "www.gov.uk":
            try:
                return (*self.govuk(url), "govuk_api")
            except Exception as exc:
                logging.info("[CONTENT] GOV.UK API unavailable: %s", type(exc).__name__)
        if host == "www.federalregister.gov":
            match = re.search(r"/documents/\d{4}/\d{2}/\d{2}/([^/]+)", url)
            if match:
                try:
                    body, _ = self.download(f"https://www.federalregister.gov/api/v1/documents/{match[1]}.json")
                    data = json.loads(body)
                    text = clean(data.get("abstract") or "")
                    target = data.get("body_html_url") or data.get("full_text_xml_url")
                    if target:
                        text = clean(text + " " + self.document(target))
                    if usable(text):
                        return text[:MAX_TEXT], [url, target] if target else [url], "federal_api"
                except Exception as exc:
                    logging.info("[CONTENT] Federal API unavailable: %s", type(exc).__name__)
        if host == "www.legislation.gov.uk":
            target = re.sub(r"/data\.(?:htm|html|xml)$", "", url).rstrip('/') + '/data.xml'
            try:
                text = self.document(target)
                if usable(text):
                    return text[:MAX_TEXT], [target], "legislation_xml"
            except Exception as exc:
                logging.info("[CONTENT] Legislation XML unavailable: %s", type(exc).__name__)
        existing = clean(item.get("summary", ""))
        if usable(existing) and host not in ("www.gov.uk", "ofac.treasury.gov"):
            return existing[:MAX_TEXT], [url], "feed"
        body, _ = self.download(url)
        if body.startswith(b'%PDF'):
            return self.document(url)[:MAX_TEXT], [url], "pdf"
        text = clean(trafilatura.extract(body, include_tables=True, include_comments=False) or "")
        links = [url]
        # Linked press releases/PDFs may describe different actions. Do not
        # append them indiscriminately to this publication's description.
        return text[:MAX_TEXT], links, "html"

    def enrich(self, news, use_cache=True):
        expanded = []
        seen_events = set()
        for item in news:
            url = item.get('link', '')
            if urlsplit(url).hostname == 'www.gov.uk':
                try:
                    events = self.govuk_updates(item)
                except Exception as exc:
                    # A generic page/Tavily fallback can reintroduce the entire
                    # history. Omit unverified GOV.UK updates on API failure.
                    logging.warning("[CONTENT] GOV.UK event extraction unavailable: %s", type(exc).__name__)
                    continue
                if events is not None:
                    for event in events:
                        key = (event['link'], event['event_timestamp'], event['summary'])
                        if key not in seen_events:
                            seen_events.add(key)
                            expanded.append(event)
                    logging.info("[CONTENT] GOV.UK separate updates=%s", len(events))
                    continue
            expanded.append(item)
        for item in expanded:
            if item.get('summary_origin') == 'govuk_change':
                continue
            url = item.get('link', '')
            if not url:
                continue
            cache_key = hashlib.sha256(json.dumps([CONTENT_CACHE_VERSION, url, item.get('date'), item.get('title')]).encode()).hexdigest()
            result = self.store.get(cache_key) if use_cache else None
            if result:
                item.update(result)
                logging.info("[CONTENT] %s: cache", item.get('source'))
                continue
            text, links, origin = '', [url], 'missing'
            try:
                text, links, origin = self.local(item)
            except Exception as exc:
                logging.info("[CONTENT] %s local extraction failed: %s", item.get('source'), type(exc).__name__)
            if not usable(text):
                data = advanced_extract(os.getenv('TAVILY_API_KEY', ''), [url], self.store,
                                        query=item.get('title'))
                for entry in data.get('results', []):
                    candidate = clean(entry.get('raw_content') or '')
                    if usable(candidate):
                        text, origin = candidate[:MAX_TEXT], 'tavily_advanced'
                        links = [entry.get('url') or url]
                        break
            if not usable(text):
                existing = clean(item.get('summary', ''))
                text, origin = (existing, 'feed_fallback') if usable(existing) else ('', 'missing')
            result = {'summary': text, 'summary_origin': origin, 'content_urls': links,
                      'summary_status': 'extracted' if text else 'missing'}
            item.update(result)
            if text:
                self.store.put(cache_key, result)
            logging.info("[CONTENT] %s: %s chars=%s", item.get('source'), origin, len(text))
        return expanded


def enrich_news(news):
    if not news:
        return news
    extractor = Extractor()
    try:
        return extractor.enrich(news)
    finally:
        extractor.session.close()
