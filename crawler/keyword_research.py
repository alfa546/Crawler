"""
crawler.keyword_research
========================

100% free Keyword Research & Regional Trend Intel Engine.

Pipeline (stateless):
    1. AutocompleteMultiplier  -- recursive Google Autocomplete expansion
    2. TrendsEngine            -- pytrends: history, region matrix, related
    3. SerpProber              -- free SERP competition proxies
    4. KeywordResearchPipeline -- orchestration -> pandas DataFrames

Honesty notes (surfaced in the UI too):
    * "Volume Proxy" is a heuristic (result-stats + autocomplete rank +
      trend interest). There is NO free ground-truth search volume source.
    * Autocomplete & SERP parsing are fail-soft: Google markup drift
      degrades a row's scores, never the whole run.
    * pytrends shares Google Trends' public rate limits; each keyword
      degrades independently to status="rate_limited".

Network discipline mirrors crawler.engine: ThreadPoolExecutor workers,
jittered politeness delays, full-jitter exponential backoff on HTTP 429
(honouring Retry-After), UA rotation, hard circuit-break.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pandas as pd
from bs4 import BeautifulSoup

from .utils import _http_get  # proxy-aware, SSL-fallback GET used by the engine

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

_AUTOCOMPLETE_URL = "https://www.google.com/complete/search"
_SERP_URL = "https://www.google.com/search"
_ALPHABET = [chr(c) for c in range(ord('a'), ord('z') + 1)]
_DIGITS = [str(d) for d in range(10)]

_MONEY_MODIFIERS = {
    'buy', 'price', 'prices', 'pricing', 'cost', 'cheap', 'cheapest', 'best',
    'top', 'review', 'reviews', 'rated', 'vs', 'versus', 'compare', 'coupon',
    'discount', 'deal', 'deals', 'sale', 'free', 'trial', 'download', 'near',
    'shop', 'store', 'order', 'online', 'subscription', 'license', 'alternative',
    'software', 'tool', 'tools', 'service', 'services', 'company', 'hire',
    'agency', 'course', 'tutorial', 'template', 'for sale',
}

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

_LANG_BY_GEO = {
    'us': 'en-US', 'gb': 'en-GB', 'uk': 'en-GB', 'ca': 'en-CA', 'au': 'en-AU',
    'ie': 'en-IE', 'nz': 'en-NZ', 'in': 'en-IN', 'de': 'de-DE', 'at': 'de-AT',
    'ch': 'de-CH', 'fr': 'fr-FR', 'be': 'fr-BE', 'es': 'es-ES', 'mx': 'es-MX',
    'ar': 'es-AR', 'co': 'es-CO', 'cl': 'es-CL', 'it': 'it-IT', 'nl': 'nl-NL',
    'pt': 'pt-PT', 'br': 'pt-BR', 'se': 'sv-SE', 'no': 'nb-NO', 'dk': 'da-DK',
    'fi': 'fi-FI', 'pl': 'pl-PL', 'cz': 'cs-CZ', 'tr': 'tr-TR', 'ru': 'ru-RU',
    'ua': 'uk-UA', 'jp': 'ja-JP', 'kr': 'ko-KR', 'cn': 'zh-CN', 'tw': 'zh-TW',
    'hk': 'zh-HK', 'sg': 'en-SG', 'my': 'en-MY', 'id': 'id-ID', 'th': 'th-TH',
    'vn': 'vi-VN', 'ph': 'en-PH', 'za': 'en-ZA', 'ng': 'en-NG', 'eg': 'ar-EG',
    'ae': 'ar-AE', 'sa': 'ar-SA', 'il': 'he-IL', 'gr': 'el-GR', 'ro': 'ro-RO',
    'hu': 'hu-HU', 'bg': 'bg-BG', 'hr': 'hr-HR', 'rs': 'sr-RS', 'sk': 'sk-SK',
    'si': 'sl-SI', 'lt': 'lt-LT', 'lv': 'lv-LV', 'ee': 'et-EE',
}


class RateLimited(Exception):
    """Raised internally after exhausting backoff on HTTP 429/5xx."""


class NetworkError(Exception):
    """Connection-level failure (DNS, timeout, reset) after retries."""


def _norm(s: str) -> str:
    """Normalise a keyword for dedup: NFKC, collapse whitespace, casefold."""
    s = unicodedata.normalize('NFKC', s or '')
    return re.sub(r'\s+', ' ', s).strip().casefold()


class UARotator:
    """Random User-Agent + geo-matched Accept-Language header factory."""

    def __init__(self, geo: str = 'us'):
        self.geo = (geo or 'us').lower()
        self.lang = _LANG_BY_GEO.get(self.geo, 'en-US')
        self._lock = threading.Lock()

    def headers(self) -> Dict[str, str]:
        with self._lock:
            ua = random.choice(_UA_POOL)
        return {
            'User-Agent': ua,
            'Accept-Language': f'{self.lang},en;q=0.8',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }


def _politeness_sleep(min_d: float, max_d: float) -> None:
    time.sleep(random.uniform(min_d, max_d))


# --------------------------------------------------------------------------
# 1. Autocomplete multiplier
# --------------------------------------------------------------------------

class AutocompleteMultiplier:
    """Recursive breadth-first Google Autocomplete expansion.

    Depth 1: seed x [a-z0-9] (~36 requests). Depth 2: each depth-1 result
    x [a-z0-9], capped by ``max_results``. ThreadPoolExecutor with jittered
    delays, full-jitter exponential backoff on 429 (honouring Retry-After),
    and a circuit-break that cools down after repeated 429 bursts.
    """

    def __init__(self, geo: str = 'us', lang: str = '', max_workers: int = 6,
                 min_delay: float = 0.8, max_delay: float = 2.2,
                 max_retries: int = 4, cb_threshold: int = 5,
                 cb_cooldown: float = 90.0):
        self.geo = (geo or 'us').lower()
        self.lang = (lang or _LANG_BY_GEO.get(self.geo, 'en-US'))[:2]
        self.max_workers = max(1, min(max_workers, 8))
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.cb_threshold = cb_threshold
        self.cb_cooldown = cb_cooldown
        self.ua = UARotator(self.geo)
        self.stats = {'requests': 0, '429s': 0, 'errors': 0, 'circuit_breaks': 0}
        self._cb_lock = threading.Lock()
        self._consecutive_429 = 0


    # -- internals ---------------------------------------------------------

    def _fetch_suggestions(self, query: str) -> List[str]:
        """One autocomplete lookup with retry/backoff. [] on hard failure."""
        params = f'?hl={quote_plus(self.lang)}&gl={quote_plus(self.geo)}'
        url = f'{_AUTOCOMPLETE_URL}{params}&client=firefox&q={quote_plus(query)}'
        delay = random.uniform(1.0, 2.0)
        for attempt in range(self.max_retries + 1):
            with self._cb_lock:
                hot = self._consecutive_429 >= self.cb_threshold
            if hot:
                self.stats['circuit_breaks'] += 1
                logger.warning('Autocomplete circuit-break: cooling down %.0fs', self.cb_cooldown)
                time.sleep(self.cb_cooldown + random.uniform(0, 5))
                with self._cb_lock:
                    self._consecutive_429 = 0

            _politeness_sleep(self.min_delay, self.max_delay)
            self.stats['requests'] += 1
            try:
                r = _http_get(url, timeout=10, headers=self.ua.headers())
            except Exception as exc:  # connection dropouts
                self.stats['errors'] += 1
                delay = min(60.0, (2 ** attempt) * random.uniform(1.0, 2.0))
                logger.debug('AC network error %r (attempt %d): %s', query, attempt + 1, exc)
                continue

            if r.status_code == 200:
                with self._cb_lock:
                    self._consecutive_429 = 0
                try:
                    data = json.loads(r.text)
                    return [s for s in (data[1] if isinstance(data, list) else [])
                            if isinstance(s, str)]
                except ValueError:
                    logger.debug('AC parse failure for %r (200, unexpected body)', query)
                    return []

            if r.status_code == 429:
                self.stats['429s'] += 1
                with self._cb_lock:
                    self._consecutive_429 += 1
                try:
                    retry_after = max(5.0, float(r.headers.get('Retry-After', 5)))
                except (TypeError, ValueError):
                    retry_after = 5.0
                delay = min(120.0, retry_after * (1.5 ** attempt) * random.uniform(1.0, 1.8))
                logger.debug('AC 429 for %r, next delay %.1fs', query, delay)
                continue

            self.stats['errors'] += 1
            delay = (2 ** attempt) * random.uniform(1.0, 2.0)

        logger.warning('Autocomplete gave up on %r after %d retries', query, self.max_retries)
        return []


    # -- public API ---------------------------------------------------------

    def expand(self, seed: str, depth: int = 1, max_results: int = 500,
               progress_cb=None) -> List[Dict]:
        """Return deduped suggestion rows.

        Row shape: {keyword, source, depth, ac_rank}. ``ac_rank`` is the
        position within its suggestion payload (Google's relevance order) —
        a strong free relevance/demand signal.
        """
        seed = (seed or '').strip()
        if not seed:
            return []
        seen = {_norm(seed)}
        rows: List[Dict] = []
        frontier = [seed]
        suffixes = _ALPHABET + _DIGITS

        for level in range(1, max(1, depth) + 1):
            if len(rows) >= max_results:
                break
            batch: List[Tuple[str, str]] = []
            for base in frontier:
                for suf in suffixes:
                    q = f'{base} {suf}'
                    if _norm(q) not in seen:
                        batch.append((q, base))
            if not batch:
                break

            level_rows: List[Dict] = []
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {pool.submit(self._fetch_suggestions, q): (q, base)
                           for q, base in batch}
                done = 0
                for fut in as_completed(futures):
                    q, base = futures[fut]
                    done += 1
                    if progress_cb and done % 10 == 0:
                        progress_cb(f'autocomplete level {level}: {done}/{len(batch)} queries')
                    for rank, sug in enumerate(fut.result(), 1):
                        key = _norm(sug)
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        level_rows.append({'keyword': sug.strip(),
                                           'source': f'{base} + suffix',
                                           'depth': level, 'ac_rank': rank})
                    if len(level_rows) >= max_results:
                        break

            random.shuffle(level_rows)  # unbiased truncation when capping
            level_rows = level_rows[:max_results - len(rows)]
            rows.extend(level_rows)
            frontier = [r['keyword'] for r in level_rows]
            logger.info('Autocomplete level %d: +%d keywords (total %d)',
                        level, len(level_rows), len(rows))

        return rows[:max_results]


# --------------------------------------------------------------------------
# 2. Trends engine (pytrends wrapper)
# --------------------------------------------------------------------------

class TrendsEngine:
    """Hardened pytrends wrapper.

    pytrends talks to the same public Google Trends endpoints everyone uses,
    so every call is wrapped: a 429 / ResponseError marks the affected
    keywords ``status='rate_limited'`` instead of aborting the run.

    Google's Trends payload limit is 5 keywords per request, so keyword
    lists are chunked automatically.
    """

    CHUNK = 5  # Google Trends hard limit per request

    def __init__(self, geo: str = 'us', timeframe: str = 'today 12-m'):
        self.geo = (geo or 'us').upper()
        self.timeframe = timeframe

    _retry_patched = False

    @classmethod
    def _patch_urllib3_retry(cls):
        """pytrends 4.9.x passes the removed ``method_whitelist`` kwarg to
        urllib3 Retry (removed in urllib3 2.0). Shim it -> allowed_methods."""
        if cls._retry_patched:
            return
        try:
            import inspect
            import urllib3.util.retry as retry_mod
            if 'method_whitelist' in inspect.signature(retry_mod.Retry.__init__).parameters:
                cls._retry_patched = True
                return
            orig = retry_mod.Retry.__init__

            def patched(self, *args, **kwargs):
                mw = kwargs.pop('method_whitelist', None)
                if mw is not None and 'allowed_methods' not in kwargs:
                    kwargs['allowed_methods'] = mw
                return orig(self, *args, **kwargs)

            retry_mod.Retry.__init__ = patched
            cls._retry_patched = True
            logger.debug('pytrends/urllib3 Retry shim installed')
        except Exception as exc:
            logger.debug('Retry shim skipped: %s', exc)

    def _client(self):
        self._patch_urllib3_retry()
        from pytrends.request import TrendReq
        return TrendReq(hl='en-US', tz=0, timeout=(10, 30),
                        retries=2, backoff_factor=1.5)

    @staticmethod
    def _slope_and_velocity(df: pd.DataFrame, kw: str) -> Tuple[float, float]:
        """Trend direction (-100..+100) and QoQ velocity (%) from monthly interest.

        Direction = normalised (last-3-month mean - first-3-month mean).
        Velocity  = percent change between the two halves' means, clipped.
        """
        try:
            if df is None or df.empty or kw not in df.columns:
                return 0.0, 0.0
            s = df[kw].fillna(0).astype(float)
            if len(s) < 6:
                return 0.0, 0.0
            head, tail = s.iloc[:3].mean(), s.iloc[-3:].mean()
            direction = (tail - head) / 100.0 * 100.0
            direction = max(-100.0, min(100.0, direction))
            velocity = 0.0
            if head > 0:
                velocity = max(-100.0, min(100.0, (tail - head) / head * 100.0))
            elif tail > 0:
                velocity = 100.0
            return round(direction, 1), round(velocity, 1)
        except Exception as exc:
            logger.debug('slope calc failed for %r: %s', kw, exc)
            return 0.0, 0.0


    def analyse_batch(self, keywords: List[str], sleep_between: float = 12.0,
                      progress_cb=None) -> Dict[str, Dict]:
        """Interest history + region matrix + mean interest per keyword.

        Returns {keyword: {trend_direction, velocity_pct, mean_interest,
        status, regions: [{code, name, score}]}}.
        """
        out: Dict[str, Dict] = {
            kw: {'trend_direction': 0.0, 'velocity_pct': 0.0, 'mean_interest': 0.0,
                 'status': 'pending', 'regions': []}
            for kw in keywords
        }
        if not keywords:
            return out
        try:
            pytrends = self._client()
        except Exception as exc:
            logger.error('pytrends init failed: %s', exc)
            for kw in keywords:
                out[kw]['status'] = 'error'
            return out

        for i in range(0, len(keywords), self.CHUNK):
            chunk = keywords[i:i + self.CHUNK]
            if progress_cb:
                progress_cb(f'trends chunk {i // self.CHUNK + 1} '
                            f'({i + 1}-{min(i + self.CHUNK, len(keywords))} of {len(keywords)})')
            try:
                pytrends.build_payload(chunk, timeframe=self.timeframe, geo=self.geo)
            except Exception as exc:
                logger.warning('Trends payload failed for chunk %s: %s', chunk, exc)
                for kw in chunk:
                    out[kw]['status'] = 'rate_limited'
                time.sleep(sleep_between)
                continue

            # Interest over time -> direction + velocity
            try:
                otdf = pytrends.interest_over_time()
                if isinstance(otdf, pd.DataFrame) and not otdf.empty:
                    if 'isPartial' in otdf.columns:
                        otdf = otdf.drop(columns=['isPartial'])
                    for kw in chunk:
                        if kw in otdf.columns:
                            d, v = self._slope_and_velocity(otdf, kw)
                            out[kw]['trend_direction'] = d
                            out[kw]['velocity_pct'] = v
                            out[kw]['mean_interest'] = round(float(otdf[kw].fillna(0).mean()), 1)
                            out[kw]['status'] = 'ok'
                        else:
                            out[kw]['status'] = 'ok' if out[kw]['status'] == 'pending' else out[kw]['status']
            except Exception as exc:
                logger.warning('interest_over_time failed: %s', exc)
                for kw in chunk:
                    out[kw]['status'] = 'rate_limited' if out[kw]['status'] == 'pending' else out[kw]['status']

            time.sleep(sleep_between + random.uniform(0, 4))


            # Interest by region (sub-region matrix) for this chunk
            try:
                regdf = pytrends.interest_by_region(resolution='REGION',
                                                    inc_low_vol=False,
                                                    inc_geo_code=True)
                if isinstance(regdf, pd.DataFrame) and not regdf.empty:
                    for kw in chunk:
                        if kw not in regdf.columns:
                            continue
                        col = regdf[kw].fillna(0).astype(float)
                        if out[kw]['status'] == 'pending':
                            out[kw]['status'] = 'ok'
                        regions = []
                        for geo_code, row in regdf.iterrows():
                            score = float(row[kw]) if kw in regdf.columns else 0.0
                            if score <= 0:
                                continue
                            name = str(geo_code)
                            code = name
                            try:
                                if isinstance(geo_code, tuple):
                                    name = str(geo_code[1] or geo_code[0])
                                    code = str(geo_code[0])
                                if 'geoName' in regdf.columns:
                                    name = str(row.get('geoName', name))
                                if 'geoCode' in regdf.columns:
                                    code = str(row.get('geoCode', code))
                            except Exception:
                                pass
                            regions.append({'code': code, 'name': name,
                                            'score': round(score, 1)})
                        regions.sort(key=lambda r: r['score'], reverse=True)
                        out[kw]['regions'] = regions[:25]
            except Exception as exc:
                logger.debug('interest_by_region failed: %s', exc)

            time.sleep(sleep_between + random.uniform(0, 4))

        return out


    def related_queries(self, seed: str, progress_cb=None) -> List[Dict]:
        """Rising + top related queries for the seed. Fail-soft."""
        rows: List[Dict] = []
        try:
            pytrends = self._client()
            if progress_cb:
                progress_cb('trends: related queries')
            pytrends.build_payload([seed], timeframe=self.timeframe, geo=self.geo)
            rq = pytrends.related_queries() or {}
        except Exception as exc:
            logger.warning('related_queries failed for %r: %s', seed, exc)
            return rows
        for kw, frames in rq.items():
            for kind in ('rising', 'top'):
                frame = frames.get(kind)
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    for _, r in frame.iterrows():
                        rows.append({
                            'keyword': kw,
                            'query': str(r.get('query', '')),
                            'type': kind,
                            'growth': str(r.get('value', '')),
                        })
        return rows


# --------------------------------------------------------------------------
# 3. SERP prober (free competition proxies)
# --------------------------------------------------------------------------

class SerpProber:
    """Scrape free competition signals from the live Google SERP.

    Signals per keyword:
      * resultStats          -> raw result count -> log-scaled Competition 0-100
      * AdWords presence     -> Ad Density 0-100 (paid slots at top/bottom)
      * Featured snippet / Top Stories / PAA / Local pack booleans
      * Commercial Intent    = weighted blend of the above + money modifiers

    Uses ``_http_get`` (inherits project proxy support + SSL fallback),
    UA rotation, jittered delays, 429 backoff.
    """

    def __init__(self, geo: str = 'us', lang: str = '', max_retries: int = 3,
                 min_delay: float = 2.0, max_delay: float = 5.0):
        self.geo = (geo or 'us').lower()
        self.lang = (lang or _LANG_BY_GEO.get(self.geo, 'en-US'))
        self.max_retries = max_retries
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.ua = UARotator(self.geo)
        self.stats = {'requests': 0, '429s': 0, 'errors': 0}


    # -- internals ---------------------------------------------------------

    _RESULT_STATS_RE = re.compile(r'([\d.,\s]+)\s*(?:results|Ergebnisse|résultats)')

    def _parse_result_count(self, html: str) -> int:
        m = self._RESULT_STATS_RE.search(html or '')
        if not m:
            return 0
        digits = re.sub(r'[^\d]', '', m.group(1))
        try:
            return int(digits)
        except ValueError:
            return 0

    def _probe_one(self, keyword: str) -> Dict:
        """Fetch + parse one SERP. Returns signal dict with status."""
        url = (f'{_SERP_URL}?q={quote_plus(keyword)}&num=20'
               f'&hl={quote_plus(self.lang)}&gl={quote_plus(self.geo)}&pws=0')
        delay = random.uniform(2.0, 4.0)
        for attempt in range(self.max_retries + 1):
            _politeness_sleep(self.min_delay, self.max_delay)
            self.stats['requests'] += 1
            try:
                r = _http_get(url, timeout=15, headers=self.ua.headers())
            except Exception as exc:
                self.stats['errors'] += 1
                delay = (2 ** attempt) * random.uniform(1.5, 3.0)
                logger.debug('SERP network error %r: %s', keyword, exc)
                continue
            if r.status_code == 200:
                return self._parse_serp(keyword, r.text)
            if r.status_code == 429:
                self.stats['429s'] += 1
                try:
                    ra = max(10.0, float(r.headers.get('Retry-After', 10)))
                except (TypeError, ValueError):
                    ra = 10.0
                delay = min(120.0, ra * (1.5 ** attempt))
                logger.debug('SERP 429 for %r, delay %.1fs', keyword, delay)
                continue
            self.stats['errors'] += 1
            delay = (2 ** attempt) * random.uniform(1.5, 3.0)
        return {'keyword': keyword, 'status': 'rate_limited'}


    def _parse_serp(self, keyword: str, html: str) -> Dict:
        soup = BeautifulSoup(html or '', 'lxml')
        result_count = self._parse_result_count(html)

        # AdWords: known containers for paid slots + /aclk tracking links.
        ad_nodes = soup.select('#tads, #taw, #bottomads, div[data-text-ad], u.d5oXvf')
        ad_links = soup.select('a[href*="/aclk?"], a[href*="&adurl="]')
        ad_count = len(ad_nodes) + len({a.get('href', '')[:80] for a in ad_links})

        has_snippet = bool(soup.select('block-component, .xpdopen .IZ6rdc, div[data-attrid="wa:/description"]'))
        has_top_stories = bool(soup.select('g-section-with-header, [data-attrid*="Stories"]'))
        has_paa = bool(soup.select('div.related-question-pair, [jsname="yEVEwb"]'))
        has_local = bool(soup.select('div[data-attrid="kc:/local:one line summary"], .rzgmgc'))

        # Log-scaled competition: 10k results ~40, 1M ~60, 100M+ ~80-90.
        if result_count > 0:
            competition = min(100.0, max(0.0,
                (math.log10(result_count) - 3) / 7 * 100))
        else:
            competition = 50.0  # unknown -> neutral

        ad_density = min(100.0, ad_count * 25.0)

        words = set(_norm(keyword).split())
        money_hits = len(words & _MONEY_MODIFIERS)
        money_score = min(25.0, money_hits * 12.5)

        commercial_intent = min(100.0,
            ad_density * 0.40 +
            (15.0 if has_local else 0.0) +
            (10.0 if has_snippet else 0.0) +
            money_score +
            max(0.0, 100.0 - competition) * 0.10)

        return {
            'keyword': keyword,
            'status': 'ok',
            'result_count': result_count,
            'ad_count': ad_count,
            'ad_density': round(ad_density, 1),
            'competition': round(competition, 1),
            'commercial_intent': round(commercial_intent, 1),
            'has_featured_snippet': has_snippet,
            'has_top_stories': has_top_stories,
            'has_paa': has_paa,
            'has_local_pack': has_local,
        }

    def probe_many(self, keywords: List[str], max_workers: int = 3,
                   cap: int = 120, progress_cb=None) -> Dict[str, Dict]:
        """Probe up to ``cap`` sampled keywords. Returns {keyword: signals}."""
        sample = keywords[:cap]
        out: Dict[str, Dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 4))) as pool:
            futures = {pool.submit(self._probe_one, kw): kw for kw in sample}
            done = 0
            for fut in as_completed(futures):
                kw = futures[fut]
                done += 1
                if progress_cb and done % 5 == 0:
                    progress_cb(f'SERP probes: {done}/{len(sample)}')
                try:
                    out[kw] = fut.result()
                except Exception as exc:
                    logger.warning('SERP probe crashed for %r: %s', kw, exc)
                    out[kw] = {'keyword': kw, 'status': 'error'}
        return out


# --------------------------------------------------------------------------
# 4. Pipeline orchestrator
# --------------------------------------------------------------------------

class KeywordResearchPipeline:
    """Stateless orchestrator: seed -> keyword DataFrames.

    Usage (CLI or Flask):
        pipe = KeywordResearchPipeline(seed='crm software', geo='us')
        sheets = pipe.run()      # {'Keywords': DF, 'RegionalInterest': DF,
                                 #  'RelatedQueries': DF, 'Summary': DF}
    """

    def __init__(self, seed: str, geo: str = 'us', lang: str = '',
                 depth: int = 1, max_results: int = 500,
                 serp_enabled: bool = True, serp_cap: int = 120,
                 trends_enabled: bool = True, progress_cb=None):
        self.seed = (seed or '').strip()
        self.geo = (geo or 'us').lower()
        self.lang = lang
        self.depth = max(1, min(int(depth), 2))
        self.max_results = max(10, min(int(max_results), 2000))
        self.serp_enabled = serp_enabled
        self.serp_cap = serp_cap
        self.trends_enabled = trends_enabled
        self.progress_cb = progress_cb
        self.started = datetime.now(timezone.utc)

    def _tick(self, msg: str) -> None:
        if self.progress_cb:
            try:
                self.progress_cb(msg)
            except Exception:
                pass
        logger.info('[KW] %s', msg)


    @staticmethod
    def _volume_proxy(ac_rank: float, mean_interest: float,
                      result_count: int) -> float:
        """Free "Volume Proxy" 0-100.

        Blend of autocomplete rank (Google relevance), Trends mean interest
        and SERP result count. Explicitly a PROXY — no ground truth exists
        for free; the UI labels it as such.
        """
        rank_score = max(0.0, 100.0 - (ac_rank - 1) * 12.0) if ac_rank else 25.0
        interest_score = min(100.0, mean_interest * 1.5)
        count_score = min(100.0, (math.log10(max(result_count, 1)) - 2) / 6 * 100)
        return round(rank_score * 0.45 + interest_score * 0.40 +
                     count_score * 0.15, 1)

    def run(self) -> Dict[str, pd.DataFrame]:
        if not self.seed:
            raise ValueError('A seed keyword is required.')
        self._tick(f'starting pipeline for "{self.seed}" (geo={self.geo})')

        # 1) Autocomplete expansion
        ac = AutocompleteMultiplier(geo=self.geo, lang=self.lang)
        rows = ac.expand(self.seed, depth=self.depth,
                         max_results=self.max_results,
                         progress_cb=self._tick)
        self._tick(f'autocomplete: {len(rows)} unique keywords')
        kw_data = {r['keyword']: dict(r) for r in rows}
        kw_data.setdefault(self.seed, {'keyword': self.seed, 'source': 'seed',
                                       'depth': 0, 'ac_rank': 1})

        # 2) Trends
        trends: Dict[str, Dict] = {}
        if self.trends_enabled:
            try:
                te = TrendsEngine(geo=self.geo)
                trends = te.analyse_batch(list(kw_data.keys()),
                                          progress_cb=self._tick)
                if not self.serp_enabled:
                    rq = te.related_queries(self.seed, progress_cb=self._tick)
            except Exception as exc:
                logger.error('Trends stage failed: %s', exc)
        related_rows = locals().get('rq', [])


        # 3) SERP competition proxies
        serp: Dict[str, Dict] = {}
        if self.serp_enabled:
            try:
                sp = SerpProber(geo=self.geo, lang=self.lang)
                serp = sp.probe_many(list(kw_data.keys()),
                                     cap=self.serp_cap, progress_cb=self._tick)
                self._tick(f'SERP probes: {len(serp)} completed')
            except Exception as exc:
                logger.error('SERP stage failed: %s', exc)

        # 4) Merge into Keywords DataFrame
        records = []
        for kw, base in kw_data.items():
            t = trends.get(kw, {})
            s = serp.get(kw, {})
            regions = t.get('regions', [])
            top_region = regions[0]['name'] if regions else ''
            vol = self._volume_proxy(base.get('ac_rank') or 0,
                                     float(t.get('mean_interest') or 0),
                                     int(s.get('result_count') or 0))
            comp = float(s.get('competition') or 50.0)
            ci = float(s.get('commercial_intent') or 0.0)
            trend_dir = float(t.get('trend_direction') or 0.0)
            # Opportunity: rising trend + low competition + commercial intent.
            opportunity = round(
                max(0.0, trend_dir) * 0.30 +
                (100.0 - comp) * 0.40 +
                ci * 0.20 +
                min(100.0, vol) * 0.10, 1)
            records.append({
                'keyword': kw,
                'source': base.get('source', ''),
                'depth': base.get('depth', 0),
                'ac_rank': base.get('ac_rank', ''),
                'word_count': len(kw.split()),
                'volume_proxy': vol,
                'trend_direction': trend_dir,
                'velocity_pct': t.get('velocity_pct', 0.0),
                'mean_interest': t.get('mean_interest', 0.0),
                'top_region': top_region,
                'region_top_score': regions[0]['score'] if regions else '',
                'result_count': s.get('result_count', ''),
                'competition': round(comp, 1),
                'ad_density': s.get('ad_density', ''),
                'commercial_intent': round(ci, 1),
                'featured_snippet': bool(s.get('has_featured_snippet')),
                'top_stories': bool(s.get('has_top_stories')),
                'paa': bool(s.get('has_paa')),
                'opportunity_score': opportunity,
                'trends_status': t.get('status', 'skipped'),
                'serp_status': s.get('status', 'skipped'),
            })
        kw_df = pd.DataFrame(records)
        if not kw_df.empty:
            kw_df = kw_df.sort_values('opportunity_score', ascending=False,
                                      ignore_index=True)


        # 5) Regional interest sheet
        reg_rows = []
        for kw, t in trends.items():
            for r in t.get('regions', []):
                reg_rows.append({'keyword': kw, 'region_code': r['code'],
                                 'region_name': r['name'], 'region_score': r['score']})
        reg_df = (pd.DataFrame(reg_rows, columns=['keyword', 'region_code',
                                                  'region_name', 'region_score'])
                  .sort_values('region_score', ascending=False, ignore_index=True)
                  if reg_rows else pd.DataFrame(columns=['keyword', 'region_code',
                                                         'region_name', 'region_score']))

        # 6) Related queries sheet
        rel_df = (pd.DataFrame(related_rows, columns=['keyword', 'query',
                                                      'type', 'growth'])
                  if related_rows else pd.DataFrame(columns=['keyword', 'query',
                                                             'type', 'growth']))

        # 7) Summary sheet
        dur = (datetime.now(timezone.utc) - self.started).total_seconds()
        summary = pd.DataFrame([
            {'metric': 'seed', 'value': self.seed},
            {'metric': 'geo', 'value': self.geo.upper()},
            {'metric': 'depth', 'value': self.depth},
            {'metric': 'keywords_found', 'value': len(kw_df)},
            {'metric': 'trends_ok', 'value': int((kw_df['trends_status'] == 'ok').sum()) if not kw_df.empty else 0},
            {'metric': 'trends_rate_limited', 'value': int((kw_df['trends_status'] == 'rate_limited').sum()) if not kw_df.empty else 0},
            {'metric': 'serp_probed', 'value': len(serp)},
            {'metric': 'autocomplete_requests', 'value': ac.stats['requests']},
            {'metric': 'autocomplete_429s', 'value': ac.stats['429s']},
            {'metric': 'duration_seconds', 'value': round(dur, 1)},
            {'metric': 'volume_is_proxy', 'value': 'yes — no free ground-truth source'},
            {'metric': 'generated_utc', 'value': self.started.strftime('%Y-%m-%d %H:%M UTC')},
        ])
        self._tick('pipeline complete')
        return {'Keywords': kw_df, 'RegionalInterest': reg_df,
                'RelatedQueries': rel_df, 'Summary': summary}


# --------------------------------------------------------------------------
# 5. Export helpers (multi-sheet XLSX / CSV bulk)
# --------------------------------------------------------------------------

def export_workbook_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    """Styled multi-sheet openpyxl workbook (frozen header, autofit-ish)."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    head_font = Font(bold=True, color='FFFFFF')
    head_fill = PatternFill(start_color='6B5CE7', end_color='6B5CE7', fill_type='solid')
    for name, df in sheets.items():
        ws = wb.create_sheet(title=name[:31])
        ws.append([str(c) for c in df.columns])
        for cell in ws[1]:
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal='center')
        for _, row in df.iterrows():
            ws.append(['' if pd.isna(v) else v for v in row.tolist()])
        ws.freeze_panes = 'A2'
        for ci, col in enumerate(df.columns, 1):
            vals = [len(str(col))] + [len(str(v)) for v in df[col].head(50)]
            ws.column_dimensions[get_column_letter(ci)].width = min(max(max(vals) + 2, 10), 50)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_csv_zip_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    """One CSV per sheet, zipped."""
    from io import BytesIO
    import zipfile
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, df in sheets.items():
            zf.writestr(f'{name}.csv', df.to_csv(index=False))
    return buf.getvalue()


# --------------------------------------------------------------------------
# CLI smoke harness
# --------------------------------------------------------------------------

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    import sys
    seed = sys.argv[1] if len(sys.argv) > 1 else 'project management software'
    geo = sys.argv[2] if len(sys.argv) > 2 else 'us'
    pipe = KeywordResearchPipeline(seed=seed, geo=geo, depth=1, max_results=60,
                                   serp_enabled=True, serp_cap=10,
                                   progress_cb=print)
    sheets = pipe.run()
    with pd.option_context('display.max_columns', None, 'display.width', 200):
        print(sheets['Keywords'].head(20))
        print(sheets['Summary'])













