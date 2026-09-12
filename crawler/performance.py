"""Performance / Page Speed analysis.

Two layers:

1. ``analyze_performance`` — zero-dependency, runs on every page as part of
   the normal crawl. Measures what we can honestly measure from a plain
   HTTP fetch + HTML parse: document weight, TTFB, wall-clock load time,
   render-blocking resources, DOM node count, images without explicit
   dimensions (CLS risk), base64-inlined images, third-party scripts. It
   canNOT measure LCP/CLS/FID — those need a real browser — so anything
   browser-only is derived as an explicit *estimate* and labelled as such.

2. ``run_pagespeed`` — the real thing. Calls Google's PageSpeed Insights
   API (Lighthouse under the hood) for true LCP / CLS / FCP / TBT / Speed
   Index + the official 0-100 performance score. Opt-in, on-demand (one
   URL at a time) because it costs Google quota and runs ~30s per URL.
   Works without an API key (shared quota) but a key removes rate limits:
   set the PAGESPEED_API_KEY env var or paste a key in the UI.
"""

import re
import logging

logger = logging.getLogger(__name__)

# --- Thresholds (aligned with Core Web Vitals / common SEO guidance) ---
SLOW_LOAD_S = 3.0        # matches the existing "Slow (>3s)" issue
HEAVY_HTML_KB = 150      # main document weight (transfer-level, uncompressed)
TTFB_WARN_MS = 800       # Google: TTFB should be < 800ms for a good LCP
DOM_NODES_WARN = 1500    # "an excessive DOM size" per Lighthouse
IMG_NO_DIMS_WARN = 5     # images without width/height -> layout shift risk
BASE64_IMG_WARN = 3      # inlined data: URIs bloat the document
THIRD_PARTY_SCRIPT_WARN = 5
RENDER_BLOCKING_SCRIPT_WARN = 3   # <script src> without async/defer in <head>
RENDER_BLOCKING_CSS_WARN = 2      # <link rel=stylesheet> in <head>

_DATA_URI_RE = re.compile(r'^data:', re.I)


def _third_party_hosts(soup, page_domain):
    """Hosts of <script src> / <img src> / <iframe src> / stylesheet href
    that are NOT this site (we only flag clearly-off-domain hosts)."""
    from urllib.parse import urlparse
    hosts = {}
    if not page_domain:
        return hosts
    page_domain = page_domain.lower().lstrip('www.')
    for tag in soup.find_all(['script', 'img', 'iframe', 'link']):
        src = tag.get('src')
        if not src and tag.name == 'link':
            rels = [r.lower() for r in (tag.get('rel') or [])]
            if 'stylesheet' in rels:
                src = tag.get('href')
        if not src:
            continue
        if _DATA_URI_RE.match(src) or src.startswith('#'):
            continue
        try:
            host = urlparse(src).netloc.lower()
        except Exception:
            continue
        if not host or host.lstrip('www.') == page_domain:
            continue
        hosts[host] = hosts.get(host, 0) + 1
    return hosts
def analyze_performance(result, soup, raw_html, resp, fetch_wall_s=None):
    """Compute the perf dict for one page and append issue strings.

    Mutates ``result``: sets ``result['perf']`` and appends to
    ``result['issues']``. Never raises — a perf bug must not kill a crawl.
    """
    perf = {
        'html_kb': 0.0, 'fetch_ms': 0, 'ttfb_ms': None,
        'scripts': 0, 'external_scripts': 0, 'inline_scripts': 0,
        'render_blocking_scripts': 0, 'stylesheets': 0,
        'render_blocking_css': 0, 'inline_styles': 0,
        'images': 0, 'imgs_no_dims': 0, 'imgs_lazy': 0,
        'base64_images': 0, 'iframes': 0, 'dom_nodes': 0,
        'third_party_scripts': 0, 'third_party_hosts': [],
        'load_time_s': 0.0, 'warnings': [], 'score': None,
    }
    result['perf'] = perf
    try:
        if soup is None:
            return perf

        perf['html_kb'] = round(len(raw_html.encode('utf-8', 'ignore')) / 1024.0, 1) if raw_html else 0.0
        perf['dom_nodes'] = len(soup.find_all(True))
        perf['fetch_ms'] = int((fetch_wall_s or 0) * 1000)
        perf['load_time_s'] = round(fetch_wall_s or 0, 2)
        if resp is not None and getattr(resp, 'elapsed', None):
            perf['ttfb_ms'] = int(resp.elapsed.total_seconds() * 1000)

        head = soup.find('head')

        # Scripts
        for s in soup.find_all('script'):
            perf['scripts'] += 1
            if s.get('src'):
                perf['external_scripts'] += 1
                # Render-blocking: classic <script src> WITHOUT async/defer
                # inside <head> blocks the parser.
                if head and s.find_parent('head') is head and not s.get('async') and not s.get('defer'):
                    perf['render_blocking_scripts'] += 1
            else:
                perf['inline_scripts'] += 1

        # Stylesheets — all stylesheets in <head> are render-blocking by
        # default (media="print" is the standard print-only exemption).
        for link in soup.find_all('link', rel=True):
            rels = [r.lower() for r in (link.get('rel') or [])]
            if 'stylesheet' in rels:
                perf['stylesheets'] += 1
                if head and link.find_parent('head') is head and (link.get('media') or '').lower() != 'print':
                    perf['render_blocking_css'] += 1

        perf['inline_styles'] = len(soup.find_all('style'))

        # Images
        for img in soup.find_all('img'):
            perf['images'] += 1
            if (img.get('loading') or '').lower() == 'lazy':
                perf['imgs_lazy'] += 1
            if not img.get('width') and not img.get('height') and not _DATA_URI_RE.match(img.get('src') or ''):
                perf['imgs_no_dims'] += 1
            if _DATA_URI_RE.match(img.get('src') or ''):
                perf['base64_images'] += 1

        perf['iframes'] = len(soup.find_all('iframe'))

        # Third-party weight
        page_domain = ''
        try:
            from urllib.parse import urlparse
            page_domain = urlparse(result.get('url') or '').netloc
        except Exception:
            pass
        tp_hosts = _third_party_hosts(soup, page_domain)
        perf['third_party_hosts'] = sorted(tp_hosts.keys())
        for s in soup.find_all('script', src=True):
            try:
                from urllib.parse import urlparse
                h = urlparse(s['src']).netloc.lower()
            except Exception:
                continue
            if h and h not in (page_domain or '').lower():
                perf['third_party_scripts'] += 1
# ---- Warnings (each shown in the UI + badge) ----
        score = 100
        if perf['load_time_s'] > SLOW_LOAD_S:
            perf['warnings'].append(f"Slow load ({perf['load_time_s']}s — over the {SLOW_LOAD_S}s target)")
            score -= 20
        if perf['ttfb_ms'] is not None and perf['ttfb_ms'] > TTFB_WARN_MS:
            perf['warnings'].append(f"High TTFB ({perf['ttfb_ms']}ms — Google wants under {TTFB_WARN_MS}ms for a good LCP)")
            score -= 15
        if perf['html_kb'] > HEAVY_HTML_KB:
            perf['warnings'].append(f"Heavy HTML ({perf['html_kb']} KB document weight — over the ~{HEAVY_HTML_KB} KB guideline)")
            score -= 10
        if perf['dom_nodes'] > DOM_NODES_WARN:
            perf['warnings'].append(f"Large DOM ({perf['dom_nodes']} nodes — Lighthouse flags over {DOM_NODES_WARN})")
            score -= 10
        if perf['imgs_no_dims'] > IMG_NO_DIMS_WARN:
            perf['warnings'].append(f"{perf['imgs_no_dims']} images missing width/height (layout shift risk)")
            score -= 5
        if perf['base64_images'] > BASE64_IMG_WARN:
            perf['warnings'].append(f"{perf['base64_images']} base64-inlined images (document bloat)")
            score -= 5
        if perf['third_party_scripts'] > THIRD_PARTY_SCRIPT_WARN:
            perf['warnings'].append(f"{perf['third_party_scripts']} third-party scripts (each is a speed + privacy risk)")
            score -= 10
        if perf['render_blocking_scripts'] > RENDER_BLOCKING_SCRIPT_WARN:
            perf['warnings'].append(f"{perf['render_blocking_scripts']} render-blocking scripts (add async/defer)")
            score -= 10
        if perf['render_blocking_css'] > RENDER_BLOCKING_CSS_WARN:
            perf['warnings'].append(f"{perf['render_blocking_css']} render-blocking stylesheets in head")
            score -= 3

        # Estimated score — honest heuristic from measured signals, explicitly
        # NOT a Lighthouse score (browser LCP/CLS needs Google's own run).
        perf['score'] = max(0, min(100, score))
        return perf
    except Exception as e:
        # Perf analysis is best-effort; never let it break the crawl row.
        try:
            result.setdefault('render_errors', []).append(f'perf: {str(e)[:120]}')
        except Exception:
            pass
_PSI_URL = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'


def run_pagespeed(url, api_key=None, strategy='mobile', timeout=120):
    """Run Google PageSpeed Insights (Lighthouse) for one URL.

    Returns a dict with ``ok`` plus, on success: url, strategy, performance
    (0-100), fcp_ms, lcp_ms, cls, tbt_ms, speed_index_ms, ttfb_ms and the key
    optimisation audits. On failure returns ``{'ok': False, 'error': msg}``.

    Raises nothing — always returns an error dict on failure.
    """
    import requests
    params = {'url': url, 'category': 'performance', 'strategy': strategy or 'mobile'}
    if api_key:
        params['key'] = api_key
    try:
        r = requests.get(_PSI_URL, params=params, timeout=timeout)
    except Exception as e:
        return {'ok': False, 'error': f'PageSpeed request failed: {str(e)[:160]}'}
    if r.status_code == 429:
        return {'ok': False, 'error': 'PageSpeed API rate limit hit (429). Add a free API key to raise the quota.'}
    if r.status_code == 403:
        return {'ok': False, 'error': 'PageSpeed API rejected the request (403). Check the API key / restrictions.'}
    if r.status_code != 200:
        try:
            msg = r.json().get('error', {}).get('message', '')
        except Exception:
            msg = r.text[:160]
        return {'ok': False, 'error': f'PageSpeed API error {r.status_code}: {msg}'}
    try:
        data = r.json()
    except Exception as e:
        return {'ok': False, 'error': f'PageSpeed response not JSON: {str(e)[:120]}'}

    try:
        lr = data.get('lighthouseResult') or {}
        cats = (lr.get('categories') or {}).get('performance') or {}
        audits = lr.get('audits') or {}

        def _num(audit_id):
            v = (audits.get(audit_id) or {}).get('numericValue')
            return round(v, 2) if isinstance(v, (int, float)) else None

        # LCP is reported in seconds by Lighthouse; normalise to ms for the UI.
        lcp_val = _num('largest-contentful-paint')
        lcp_ms = int(lcp_val * 1000) if isinstance(lcp_val, (int, float)) else None

        return {
            'ok': True,
            'url': url,
            'strategy': params['strategy'],
            # Lighthouse scores are 0-1; scale to the familiar 0-100.
            'performance': (lambda v: round(v * 100) if isinstance(v, (int, float)) else None)(cats.get('score')),
            'fcp_ms': _num('first-contentful-paint'),
            'lcp_ms': lcp_ms,
            'cls': _num('cumulative-layout-shift'),
            'tbt_ms': _num('total-blocking-time'),
            'speed_index_ms': _num('speed-index'),
            'ttfb_ms': _num('server-response-time'),
            'audits': {
                aid: {'score': (audits.get(aid) or {}).get('score'),
                      'displayValue': (audits.get(aid) or {}).get('displayValue', ''),
                      'title': (audits.get(aid) or {}).get('title', '')}
                for aid in ('render-blocking-resources', 'uses-responsive-images',
                            'unused-css-rules', 'unused-javascript',
                            'modern-image-formats', 'offscreen-images',
                            'uses-text-compression', 'dom-size',
                            'uses-optimized-images', 'total-byte-weight')
                if aid in audits
            },
        }
    except Exception as e:
        return {'ok': False, 'error': f'Failed to parse Lighthouse result: {str(e)[:120]}'}