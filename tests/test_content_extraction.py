import json
import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from content_extraction import Extractor, Store, advanced_extract, clean
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


ARTICLE = 'The regulator announced new export restrictions and licensing requirements for specified goods. ' * 3


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.env = patch.dict(os.environ, {'TAVILY_DAILY_CREDITS': '2',
                                          'TAVILY_MONTHLY_CREDITS': '900', 'TAVILY_API_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def item(self):
        return {'source': 'Treasury', 'link': 'https://home.treasury.gov/news/press-releases/sb1',
                'title': 'Export restrictions', 'date': '06.10.2026'}

    def test_expired_cache_and_previous_month_are_pruned(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO cache VALUES ('expired', 1, '{}')")
            db.execute("INSERT INTO spend VALUES ('old-month', '2000-01-01', '2000-01', 5)")
        self.store.put('fresh', {'summary': ARTICLE})
        current = self.store.reserve(2)
        reopened = Store(self.path)
        self.assertIsNone(reopened.get('expired'))
        self.assertEqual(reopened.get('fresh')['summary'], ARTICLE)
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT id FROM spend').fetchall(), [(current,)])

    def test_budget_is_atomic_and_persists_between_instances(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            tokens = list(pool.map(lambda _: Store(self.path).reserve(2), range(4)))
        self.assertEqual(sum(token is not None for token in tokens), 1)
        self.assertIsNone(Store(self.path).reserve(2))
        self.store.settle(next(token for token in tokens if token), 0)
        self.assertIsNotNone(Store(self.path).reserve(2))

    def test_monthly_budget_survives_day_change(self):
        month = datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m')
        with self.store.connect() as db:
            db.execute("INSERT INTO spend VALUES ('old', '2000-01-01', ?, 899)", (month,))
        self.assertIsNone(self.store.reserve(2))

    def test_zero_daily_budget_and_new_day(self):
        with patch.dict(os.environ, {'TAVILY_DAILY_CREDITS': '0'}):
            self.assertIsNone(self.store.reserve(2))
        token = self.store.reserve(2)
        with self.store.connect() as db:
            db.execute("UPDATE spend SET day='2000-01-01' WHERE id=?", (token,))
        self.assertIsNotNone(self.store.reserve(2))

    def test_local_extraction_without_api_key_and_cache(self):
        extractor = Extractor(self.store)
        self.addCleanup(extractor.session.close)
        with patch.object(extractor, 'local', return_value=(ARTICLE, ['url'], 'html')) as local, \
                patch('content_extraction.advanced_extract') as advanced:
            result = extractor.enrich([self.item()])[0]
            self.assertEqual(result['summary_origin'], 'html')
            self.assertEqual(extractor.enrich([self.item()])[0]['summary'], ARTICLE)
            self.assertEqual(local.call_count, 1)
            advanced.assert_not_called()

    def test_bad_tavily_output_is_not_replaced_with_title(self):
        extractor = Extractor(self.store)
        self.addCleanup(extractor.session.close)
        with patch.object(extractor, 'local', side_effect=ValueError), \
                patch('content_extraction.advanced_extract', return_value={
                    'results': [{'raw_content': "Skip to main content Here's how you know " + ARTICLE}]}):
            result = extractor.enrich([self.item()])[0]
        self.assertEqual(result['summary'], '')
        self.assertEqual(result['summary_status'], 'missing')

    def test_advanced_only_after_local_failure(self):
        extractor = Extractor(self.store)
        self.addCleanup(extractor.session.close)
        with patch.object(extractor, 'local', return_value=('', [], 'html')), \
                patch('content_extraction.advanced_extract', return_value={
                    'results': [{'raw_content': ARTICLE}]}) as advanced:
            result = extractor.enrich([self.item()])[0]
        self.assertEqual(result['summary_origin'], 'tavily_advanced')
        self.assertEqual(advanced.call_count, 1)

    def usage(self, remaining=100):
        response = Mock()
        response.json.return_value = {'account': {'plan_limit': 1000, 'plan_usage': 1000-remaining},
                                      'key': {'limit': None, 'usage': 0}}
        return response

    def test_provider_exhaustion_and_usage_failure_prevent_spending(self):
        with patch('content_extraction.requests.get', return_value=self.usage(0)), \
                patch('content_extraction.requests.post') as post:
            self.assertEqual(advanced_extract('key', ['url'], self.store), {})
            post.assert_not_called()
        with patch('content_extraction.requests.get', side_effect=TimeoutError), \
                patch('content_extraction.requests.post') as post:
            self.assertEqual(advanced_extract('key', ['url'], self.store), {})
            post.assert_not_called()

    def test_timeout_keeps_reservation_and_blocks_next_request(self):
        with patch('content_extraction.requests.get', return_value=self.usage()), \
                patch('content_extraction.requests.post', side_effect=TimeoutError) as post:
            advanced_extract('key', ['url'], self.store)
            advanced_extract('key', ['url'], self.store)
            self.assertEqual(post.call_count, 1)

    def test_reported_credit_usage_replaces_reservation(self):
        result = Mock()
        result.json.return_value = {'usage': {'credits': 0.4}, 'results': []}
        with patch('content_extraction.requests.get', return_value=self.usage()), \
                patch('content_extraction.requests.post', return_value=result) as post:
            advanced_extract('key', ['url'], self.store)
            self.assertEqual(post.call_args.kwargs['json']['extract_depth'], 'advanced')
        with self.store.connect() as db:
            self.assertAlmostEqual(db.execute('SELECT SUM(credits) FROM spend').fetchone()[0], 0.4)

    def test_govuk_api_uses_change_history_instead_of_rss_description(self):
        extractor = Extractor(self.store)
        self.addCleanup(extractor.session.close)
        data = {'details': {'change_history': [{'public_timestamp': '2026-10-06T10:00:00Z',
                                              'note': ARTICLE}], 'body': '<p>Standing page</p>'}}
        with patch.object(extractor, 'download', return_value=(json.dumps(data).encode(), 'application/json')):
            text, _, origin = extractor.local({'link': 'https://www.gov.uk/guidance/example', 'summary': 'General list'})
        self.assertIn(ARTICLE.strip(), text)
        self.assertEqual(origin, 'govuk_api')

    def test_xml_explanatory_notes_and_rss_html(self):
        extractor = Extractor(self.store)
        self.addCleanup(extractor.session.close)
        xml = b'<Legislation><Body>Long act</Body><ExplanatoryNotes>Explanation of the changes</ExplanatoryNotes></Legislation>'
        with patch.object(extractor, 'download', return_value=(xml, 'application/xml')):
            self.assertEqual(extractor.document('url'), 'Explanation of the changes')
        self.assertEqual(clean('<p>Text <strong>from RSS</strong></p>'), 'Text from RSS')

    def test_pdf_download_url_without_extension(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 200 Td (Sanctions notice and export restrictions) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        buffer = io.BytesIO()
        writer.write(buffer)
        extractor = Extractor(self.store)
        self.addCleanup(extractor.session.close)
        with patch.object(extractor, 'download', return_value=(buffer.getvalue(), 'application/pdf')):
            self.assertIn('Sanctions notice', extractor.document('https://ofac.treasury.gov/media/123/download'))


if __name__ == '__main__':
    unittest.main()
