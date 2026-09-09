import json
import uuid
import threading
import asyncio
from queue import Queue
from flask import Blueprint, request, Response, stream_with_context, jsonify

from .engine import AsyncCrawler
from .database import save_crawl_state, init_db

crawl_api_bp = Blueprint('crawl_api', __name__)

def run_async_crawler_in_thread(crawl_id, seed_url, max_pages, max_depth, event_queue):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    crawler = AsyncCrawler(
        crawl_id=crawl_id,
        seed_url=seed_url,
        max_pages=max_pages,
        max_depth=max_depth,
        event_queue=event_queue
    )
    
    loop.run_until_complete(crawler.run())
    loop.close()

@crawl_api_bp.route('/crawl', methods=['POST'])
def crawl_site():
    data = request.json
    seed_url = (data.get('url', '') or '').strip()
    max_pages = int(data.get('max_pages') or 500)
    max_depth = int(data.get('max_depth') or 10)
    
    if not seed_url:
        return jsonify({'error': 'No URL provided'}), 400
        
    crawl_id = str(uuid.uuid4())
    event_queue = Queue()
    
    # Start background crawler thread
    thread = threading.Thread(
        target=run_async_crawler_in_thread,
        args=(crawl_id, seed_url, max_pages, max_depth, event_queue),
        daemon=True
    )
    thread.start()
    
    def generate():
        yield f"data: {json.dumps({'type': 'start', 'domain': seed_url, 'workers': 'async', 'crawl_id': crawl_id})}\n\n"
        
        while True:
            event = event_queue.get()
            
            if event['type'] == 'done':
                yield f"data: {json.dumps({'type': 'info', 'msg': 'Crawl completed successfully'})}\n\n"
                break
                
            elif event['type'] == 'page':
                # Map to what frontend expects for a page result
                # Format expected: {'type': 'page', 'data': { ... }}
                result_dict = {
                    'url': event['url'],
                    'status': event['status'],
                    'title': event['title'],
                    'depth': event['depth'],
                    'time': event['time'],
                    'internal_link_urls': [], # TODO
                    'external_link_urls': [],
                    'issues': [] # TODO: SEO analysis
                }
                yield f"data: {json.dumps({'type': 'page', 'data': result_dict})}\n\n"
                
            elif event['type'] == 'error':
                yield f"data: {json.dumps({'type': 'error', 'msg': event['error']})}\n\n"
                
    return Response(stream_with_context(generate()), mimetype='text/event-stream')
