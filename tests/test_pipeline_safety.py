import sqlite3
import os
import json
import ast
from pathlib import Path
import logging
import unittest
from datetime import date
from unittest.mock import Mock, patch

from model_client import MODEL, post_completion
from message_utils import split_message
from storage_maintenance import prune_bot_cache
from sources_big import prepare_summary_text, build_final_summary_materials, load_first_check_prompt
from sources_big import first_check_filter_news
from sources_big import rewrite_summaries_with_model, ask_final_summary_model


class PipelineTests(unittest.TestCase):
    def test_distinct_updates_with_same_url_survive_rewriter(self):
        items = [dict(source='UK FCDO', title=title, date='16.10.2026',
                      link='https://www.gov.uk/guidance/list', summary=title)
                 for title in ('Russia correction', 'Russia designation')]
        # A duplicated response must not silently replace the second event.
        bad = {'choices': [{'message': {'content': json.dumps([items[0], items[0]])}}]}
        good = {'choices': [{'message': {'content': json.dumps(items)}}]}
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}), \
             patch('sources_big.post_completion', side_effect=[bad, good]) as model, \
             patch('sources_big.time.sleep'):
            result = rewrite_summaries_with_model(items)
        self.assertEqual(result, items)
        self.assertEqual(model.call_count, 2)

    def test_empty_events_skip_final_model(self):
        with patch('sources_big.post_completion') as model:
            self.assertIn('No sanctions news', ask_final_summary_model(''))
            model.assert_not_called()

    def test_production_pipeline_caches_full_summary_without_telegram(self):
        # Load only business logic: importing manage.py would create a Telegram bot.
        tree = ast.parse(Path('manage.py').read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_get_news_for_today_impl')
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE daily_news_cache (date TEXT PRIMARY KEY, content TEXT)')
            db.execute("INSERT INTO daily_news_cache VALUES ('2000-01-01','expired')")
            item = dict(source='s', title='t', link='url', date='2026-10-06')
            long_summary = 'Complete news. ' * 500
            collect = Mock(return_value=[item])
            model = Mock(return_value=long_summary)
            namespace = dict(conn=db, cursor=db.cursor(), date=date, logging=logging,
                             prune_bot_cache=prune_bot_cache, collect_all_news=collect,
                             first_check_filter_news=lambda x: x,
                             enrich_news_with_tavily_summary=lambda x: x,
                             rewrite_summaries_with_model=lambda x: x,
                             build_final_summary_materials=build_final_summary_materials,
                             ask_model=model, prepare_summary_text=prepare_summary_text)
            exec(compile(ast.Module(body=[function], type_ignores=[]), 'manage.py', 'exec'), namespace)
            run = namespace['_get_news_for_today_impl']
            self.assertEqual(run(), long_summary.strip())
            self.assertEqual(run(), long_summary.strip())
            collect.assert_called_once()
            model.assert_called_once()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_news_cache').fetchone()[0], 1)

    def response(self, reason):
        return Mock(json=lambda: {'model': MODEL, 'choices': [
            {'finish_reason': reason, 'message': {'content': 'answer'}}]})

    def test_model_and_retry_truncated_output(self):
        payloads = []
        responses = iter([self.response('length'), self.response('stop')])
        def send(*args, **kwargs):
            payloads.append(dict(kwargs['json']))
            return next(responses)
        with patch('model_client.requests.post', side_effect=send):
            post_completion({'model': 'old', 'max_tokens': 700, 'temperature': 0.1}, {})
        self.assertEqual([p['max_tokens'] for p in payloads], [16000, 32000])
        self.assertTrue(all(p['model'] == MODEL and 'temperature' not in p for p in payloads))

    def test_never_accept_truncated_output(self):
        with patch('model_client.requests.post', return_value=self.response('length')):
            with self.assertRaises(RuntimeError):
                post_completion({}, {})

    def test_long_text_survives_preparation_and_splitting(self):
        text = ('Long news 😀\n' * 900) + 'Verified sources: test'
        self.assertEqual(prepare_summary_text(text), text)
        parts = split_message(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 4000 for p in parts))
        materials = build_final_summary_materials([dict(source='s', title='t', date='2026-10-06', link='url', summary=text)])
        self.assertIn(text, materials)
        self.assertIn('2026-10-06', materials)

    def test_cache_retention_preserves_other_tables(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE daily_news_cache (date TEXT PRIMARY KEY, content TEXT)')
            db.execute('CREATE TABLE chats (chat_id INTEGER)')
            db.execute('INSERT INTO chats VALUES (1)')
            db.executemany('INSERT INTO daily_news_cache VALUES (?, ?)',
                           [('2026-02-01', 'old'), ('2026-10-04', 'old'), ('2026-10-05', 'yesterday'), ('2026-10-06', 'today')])
            prune_bot_cache(db, date(2026, 10, 6))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_news_cache').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chats').fetchone()[0], 1)

    def test_first_check_prompt_is_readable(self):
        self.assertGreater(len(load_first_check_prompt()), 100)

    def test_filter_cannot_rewrite_source_or_summary(self):
        original = dict(source='US State Dept', title='Real title', date='2026-10-06', link='https://example.org/news', summary='Original text')
        changed = dict(original, source='EU Commission', summary='Invented text')
        response = {'choices': [{'message': {'content': json.dumps([changed])}}]}
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}), patch('sources_big.post_completion', return_value=response):
            self.assertEqual(first_check_filter_news([original]), [original])
