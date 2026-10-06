import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

import sources_big as sources


def response(body, url):
    result = Mock(status_code=200, text=body, content=body.encode(), url=url)
    return result


def atom(entries, next_url=None):
    body = '<feed xmlns="http://www.w3.org/2005/Atom">'
    if next_url:
        body += f'<link rel="next" href="{escape(next_url)}"/>'
    for number, title, published in entries:
        body += f'<entry><id>{number}</id><title>{escape(title)}</title>'
        body += f'<link href="https://www.legislation.gov.uk/uksi/2026/{number}/made"/>'
        if published:
            body += f'<published>{published.isoformat()}</published>'
        body += f'<updated>{datetime.now(timezone.utc).isoformat()}</updated></entry>'
    return body + '</feed>'


class SourceParserTests(unittest.TestCase):
    def test_treasury_slashes_absolute_urls_dates_and_duplicates(self):
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)
        old = now - timedelta(days=7)
        body = ''
        for href, title, date in [
            ('/news/press-releases/sb0650/', 'Sanctions update', recent),
            ('https://home.treasury.gov/news/press-releases/sb0650', 'Sanctions update', recent),
            ('/news/press-releases/sb0649', 'Export update', recent),
            ('/news/press-releases/sb0648/', 'Old sanctions', old),
            ('https://example.com/news/press-releases/sb0651/', 'Wrong host', recent),
        ]:
            body += (f'<div><time datetime="{date.isoformat()}">October 1, 2020</time>'
                     f'<h3><a href="{href}">{title}</a></h3></div>')
        with patch.object(sources.requests, 'get', return_value=response(body, sources.US_TREASURY_CANDIDATE_URL)):
            items = sources.fetch_us_treasury_candidate_news()
        self.assertEqual([i['title'] for i in items], ['Sanctions update', 'Export update'])
        self.assertTrue(all(i['date'] == recent.strftime('%d.%m.%Y') for i in items))

    def test_treasury_text_date_fallback(self):
        soup = sources.BeautifulSoup('<div>October 5, 2026<h3><a>Release</a></h3></div>', 'html.parser')
        self.assertEqual(sources.extract_nearby_treasury_date(soup.a),
                         datetime(2026, 10, 5, tzinfo=timezone.utc))

    def test_uksi_pagination_dates_and_no_result_truncation(self):
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)
        old = now - timedelta(days=7)
        next_url = sources.UKSI_FEED_URL + '?page=2'
        first = [(1, 'Sanctions Regulations', recent),
                 (2, 'Old Sanctions Regulations', old),
                 (3, 'Undated Sanctions Regulations', None),
                 (4, 'Future Sanctions Regulations', now + timedelta(days=1)),
                 (5, 'Road Traffic Regulations', recent)]
        second = [(1, 'Sanctions Regulations', recent)] + [
            (i, 'Export Control Regulations', recent) for i in range(6, 17)]
        with patch.object(sources.requests, 'get', side_effect=[
            response(atom(first, next_url), sources.UKSI_FEED_URL),
            response(atom(second), next_url),
        ]) as get:
            items = sources.fetch_uksi_sanctions()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(items), 12)
        self.assertTrue(all(i['date'] == recent.strftime('%d.%m.%Y') for i in items))

    def test_uksi_stops_after_old_page(self):
        old = datetime.now(timezone.utc) - timedelta(days=7)
        with patch.object(sources.requests, 'get', return_value=response(
            atom([(1, 'Sanctions Regulations', old)], sources.UKSI_FEED_URL + '?page=2'),
            sources.UKSI_FEED_URL,
        )) as get:
            self.assertEqual(sources.fetch_uksi_sanctions(), [])
        self.assertEqual(get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
