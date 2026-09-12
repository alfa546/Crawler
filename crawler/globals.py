ACTIVE_CRAWL_RULES = {}
ACTIVE_CRAWL_LIMITS = {}
SUSPENDED_CRAWLS = {}
SUSPENDED_CRAWL_TTL = 30 * 60

import os

_CRAWL_FOLDER = os.path.expanduser('~/.site-crawler-crawls')
_CRAWL_FOLDERS_RO = [os.path.expanduser(p) for p in
                     os.environ.get('SITE_CRAWLER_EXTRA_CRAWL_DIRS', '').split(':')
                     if p.strip()]

_CRAWL_TITLE_HISTORY_PATH = os.path.join(_CRAWL_FOLDER, 'crawl-titles.jsonl')
