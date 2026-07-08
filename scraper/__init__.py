"""
Пакет scraper: модули парсинга источников.

Каждый источник (SourceType) обрабатывается своим коллектором:
    tg       -> TelegramCollector   (Telethon)
    rss      -> RssCollector        (feedparser)
    github   -> GithubCollector     (aiohttp + GitHub REST API)
    newsdata -> NewsdataCollector   (aiohttp + NewsData.io API)

CollectorManager (scraper/manager.py) — фасад: читает активные источники,
маршрутизирует их в нужные коллекторы, аггрегирует результат.
"""
