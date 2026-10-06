from datetime import date, timedelta


def prune_bot_cache(connection, today=None):
    """Keep today and yesterday; subscriber IDs and per-chat state are retained."""
    today = today or date.today()
    cutoff = (today - timedelta(days=1)).isoformat()
    with connection:
        connection.execute("DELETE FROM daily_news_cache WHERE date < ?", (cutoff,))
