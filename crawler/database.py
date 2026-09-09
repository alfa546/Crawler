import sqlite3
import os
import json
from contextlib import contextmanager

DB_PATH = os.environ.get('CRAWLER_DB_PATH', 'crawler.db')

def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS crawls (
                id TEXT PRIMARY KEY,
                seed_url TEXT,
                status TEXT,
                config TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_pages INTEGER DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pages (
                crawl_id TEXT,
                url TEXT,
                status_code INTEGER,
                title TEXT,
                seo_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (crawl_id, url)
            )
        ''')
        # Queue for pending urls
        conn.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                crawl_id TEXT,
                url TEXT,
                depth INTEGER,
                priority INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                PRIMARY KEY (crawl_id, url)
            )
        ''')

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def save_crawl_state(crawl_id, seed_url, status, config):
    with get_db() as conn:
        conn.execute('''
            INSERT INTO crawls (id, seed_url, status, config)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                config=excluded.config,
                updated_at=CURRENT_TIMESTAMP
        ''', (crawl_id, seed_url, status, json.dumps(config)))

def push_queue(crawl_id, url, depth):
    with get_db() as conn:
        conn.execute('''
            INSERT OR IGNORE INTO queue (crawl_id, url, depth)
            VALUES (?, ?, ?)
        ''', (crawl_id, url, depth))

def pop_queue(crawl_id, limit=1):
    with get_db() as conn:
        cursor = conn.execute('''
            SELECT url, depth FROM queue
            WHERE crawl_id = ? AND status = 'pending'
            ORDER BY priority DESC, depth ASC
            LIMIT ?
        ''', (crawl_id, limit))
        rows = cursor.fetchall()
        
        urls = [r['url'] for r in rows]
        if urls:
            placeholders = ','.join(['?'] * len(urls))
            conn.execute(f'''
                UPDATE queue SET status = 'processing'
                WHERE crawl_id = ? AND url IN ({placeholders})
            ''', [crawl_id] + urls)
        return rows

def save_page_result(crawl_id, url, status_code, title, seo_data):
    with get_db() as conn:
        conn.execute('''
            INSERT INTO pages (crawl_id, url, status_code, title, seo_data)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(crawl_id, url) DO UPDATE SET
                status_code=excluded.status_code,
                title=excluded.title,
                seo_data=excluded.seo_data
        ''', (crawl_id, url, status_code, title, json.dumps(seo_data)))
        
        conn.execute('''
            UPDATE queue SET status = 'completed'
            WHERE crawl_id = ? AND url = ?
        ''', (crawl_id, url))
