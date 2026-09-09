import os
import random
import logging

logger = logging.getLogger(__name__)

class ProxyManager:
    def __init__(self, proxy_file_path=None):
        self.proxies = []
        self.current_index = 0
        
        if not proxy_file_path:
            proxy_file_path = os.environ.get('PROXY_LIST_PATH', 'proxies.txt')
            
        self.load_proxies(proxy_file_path)

    def load_proxies(self, filepath):
        if not os.path.exists(filepath):
            logger.info(f"No proxy file found at {filepath}. Crawling will be direct.")
            return
            
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # Expected format: IP:PORT or IP:PORT:USER:PASS or http://...
                self.proxies.append(self._parse_proxy(line))
        
        logger.info(f"Loaded {len(self.proxies)} proxies from {filepath}")
        random.shuffle(self.proxies)

    def _parse_proxy(self, line):
        if line.startswith('http://') or line.startswith('https://'):
            return line
            
        parts = line.split(':')
        if len(parts) == 2:
            return f"http://{parts[0]}:{parts[1]}"
        elif len(parts) == 4:
            ip, port, user, pwd = parts
            return f"http://{user}:{pwd}@{ip}:{port}"
        else:
            logger.warning(f"Invalid proxy format: {line}")
            return line

    def get_proxy(self):
        if not self.proxies:
            return None
        
        proxy = self.proxies[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.proxies)
        return proxy
