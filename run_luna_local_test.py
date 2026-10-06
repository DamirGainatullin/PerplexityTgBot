"""Live pipeline diagnostic. Calls paid APIs; never imports or sends to Telegram."""
import copy
import io
import json
import logging
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import sources_big as pipeline
from model_client import MODEL, CALL_RECORDS
from content_extraction import Extractor

ROOT = Path(__file__).resolve().parent


def fresh_descriptions(news):
    extractor = Extractor()
    try:
        return extractor.enrich(news, use_cache=False)
    finally:
        extractor.session.close()


def database_audit():
    result = {}
    for filename, tables in [('bot.db', ['chats', 'news_requests', 'daily_news_cache']),
                             ('content_state.sqlite3', ['cache', 'spend'])]:
        path = ROOT / filename
        if not path.exists():
            continue
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            result[filename] = {'bytes': path.stat().st_size,
                                'integrity': db.execute('PRAGMA quick_check').fetchone()[0],
                                'rows': {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}}
    return result


def main():
    load_dotenv(ROOT / '.env')
    if not os.getenv('OPENROUTER_API_KEY'):
        raise RuntimeError('OPENROUTER_API_KEY missing')
    pipeline.enable_local_test_mode()
    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    report = {'started': datetime.now().astimezone().isoformat(), 'model': MODEL,
              'telegram': 'disabled; manage.py not imported', 'status': 'running',
              'description_cache_reads': False,
              'database_before': database_audit(), 'stages': {}, 'timings_seconds': {}}
    path = ROOT / 'GPT_LUNA_LOCAL_TEST.TXT'

    def save():
        # Credentials never belong in a diagnostic artifact, including exception logs.
        text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        for key, value in os.environ.items():
            if value and any(word in key.upper() for word in ('TOKEN', 'API_KEY', 'PASSWORD', 'PROXY')):
                text = text.replace(value, '<redacted>')
        path.write_text(text, encoding='utf-8')

    def stage(name, function, *args):
        logging.info('STAGE START: %s', name)
        start = time.monotonic()
        result = function(*args)
        report['timings_seconds'][name] = round(time.monotonic() - start, 2)
        report['stages'][name] = copy.deepcopy(result)
        report['model_calls'] = list(CALL_RECORDS)
        save()
        logging.info('STAGE DONE: %s (%s)', name, len(result))
        return result

    save()
    try:
        raw = stage('raw_news', pipeline.collect_all_news)
        filtered = stage('first_check', pipeline.first_check_filter_news, raw)
        enriched = stage('descriptions', fresh_descriptions, filtered)
        rewritten = stage('rewritten_descriptions', pipeline.rewrite_summaries_with_model, enriched)
        materials = stage('final_model_input', pipeline.build_final_summary_materials, rewritten)
        if rewritten:
            stage('final_summary', pipeline.ask_final_summary_model, materials)
        else:
            report['stages']['final_summary'] = 'No sanctions news affecting Russia were found in the last 24 hours.'
        report['counts'] = {'raw': len(raw), 'filtered': len(filtered),
                            'descriptions': sum(bool(i.get('summary')) for i in enriched),
                            'rewritten': len(rewritten),
                            'raw_by_source': dict(Counter(i['source'] for i in raw)),
                            'description_origins': dict(Counter(i.get('summary_origin', 'unknown') for i in enriched))}
        degraded = any(marker in logs.getvalue() for marker in ('[FIRST CHECK ERROR]', '[SUMMARY REWRITER ERROR]'))
        report['status'] = 'completed_with_model_fallback' if degraded else 'completed'
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        logging.exception('LOCAL PIPELINE FAILED')
        raise
    finally:
        report['finished'] = datetime.now().astimezone().isoformat()
        report['database_after'] = database_audit()
        report['model_calls'] = list(CALL_RECORDS)
        report['logs'] = logs.getvalue()
        save()
        logging.getLogger().removeHandler(handler)
        print(f'Report: {path}; status={report["status"]}', flush=True)


if __name__ == '__main__':
    main()
