from .globals import ACTIVE_CRAWL_RULES, ACTIVE_CRAWL_LIMITS, SUSPENDED_CRAWLS, SUSPENDED_CRAWL_TTL
import json, time, os, re, logging, threading
from queue import Queue
from urllib.parse import urlparse, urljoin, urlunparse, parse_qs, urlencode
import requests
from bs4 import BeautifulSoup
from .utils import *
from .seo_analyzer import *

from flask import request, stream_with_context, jsonify, current_app, Response
def _crawl_page(url, session, domain, pw_page=None, ignore_noindex=False, capture_no_js=False, challenge_browser=None):
    """Crawl a single page and return audit data dict.

    If ``pw_page`` (a live Playwright page) is provided, the HTML body will be
    re-fetched via a headless browser so JS-rendered content is captured. We
    still do the initial ``requests.get`` to get response headers / redirect
    history cheaply and reliably.

    ``capture_no_js``: when True AND ``pw_page`` produced a successful render,
    also parse the original (pre-JS) HTML and attach a subset of fields to
    ``result['non_js']`` plus a diff/severity summary at ``result['js_diff']``.
    Lets the user see what content is JS-only and therefore at risk for AI
    crawlers (GPTBot, ClaudeBot etc.) which mostly don't execute JS.
    """
    from urllib.parse import urlparse, urljoin
    result = {
        'url': url, 'status_code': 0, 'content_type': '', 'last_modified': '', 'response_time': 0,
        'title': '', 'title_len': 0, 'meta_description': '', 'meta_len': 0,
        'h1': '', 'h1_list': [], 'h2_list': [], 'h2_count': 0,
        'canonical': '', 'canonical_match': False, 'canonical_kind': None,
        'word_count': 0, 'internal_links': 0, 'external_links': 0,
        'internal_link_urls': [], 'images_total': 0, 'images_no_alt': 0,
        'schema_types': [], 'indexable': True, 'is_pagination': False, 'issues': [], 'error': None,
        'depth': 0, 'redirect_url': None, 'redirect_kind': None,
        'redirect_status': None, 'redirect_hops': 0, 'redirect_chain': [],
        'body_hash': '', 'security': {}, 'mixed_content': [],
        'url_issues': [], 'hreflang': [], 'x_robots_tag': '',
        'og_tags': {}, 'twitter_tags': {}, 'analytics': [],
    }

    try:
        # Retry loop for transient failures (5xx, 429, connection errors).
        # 403 gets a UA-fallback instead of a delay-and-retry, because 403 is
        # usually bot-fingerprint detection rather than a rate limit.
        resp = None
        retries_done = 0
        last_exc = None
        ssl_bypassed = False
        challenge_html = None
        challenge_status = None
        for attempt in range(3):  # 1 primary + 2 retries
            try:
                resp = session.get(url, timeout=15, allow_redirects=True)
                last_exc = None
            except requests.exceptions.SSLError as e:
                # Broken/incomplete certificate chain (most often a missing
                # intermediate cert). The site IS reachable — browsers fetch the
                # missing intermediate via AIA automatically, requests does not.
                # Retry with verification OFF so the crawl can proceed, flag it
                # loudly, and disable verify for the rest of this session so we
                # don't pay a failed handshake on every subsequent page.
                last_exc = e
                try:
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                except Exception:
                    pass
                try:
                    resp = session.get(url, timeout=15, allow_redirects=True, verify=False)
                    last_exc = None
                    ssl_bypassed = True
                    session.verify = False
                except requests.exceptions.RequestException as e2:
                    last_exc = e2
                    resp = None
            except requests.exceptions.RequestException as e:
                last_exc = e
                resp = None
            # Decide whether to retry, fall back UA, or accept the response.
            if resp is not None:
                if resp.status_code == 429:
                    # Respect Retry-After header if present, else exponential wait.
                    ra = resp.headers.get('Retry-After', '').strip()
                    try: wait = float(ra) if ra else 2 * (2 ** attempt)
                    except ValueError: wait = 2 * (2 ** attempt)
                    wait = min(wait, 10)
                    if attempt < 2:
                        retries_done += 1
                        time.sleep(wait)
                        continue
                elif 500 <= resp.status_code < 600:
                    # WAF block-page check — Wordfence/Cloudflare/Sucuri 503s.
                    # Retrying just escalates the block, so bail and let the
                    # host-pause window cool the IP down.
                    _waf = _detect_waf_block(resp)
                    if _waf:
                        result['_waf_block'] = _waf
                        break
                    if attempt < 2:
                        retries_done += 1
                        time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s
                        continue
                elif resp.status_code == 403:
                    # WAF check first — UA-swap won't help against an IP-flag,
                    # and trying it just spends quota.
                    _waf = _detect_waf_block(resp)
                    if _waf:
                        result['_waf_block'] = _waf
                        break
                    # Cloudflare-style managed challenge. Testing showed no UA
                    # (Googlebot/bingbot/Chrome/Firefox), no header set and no
                    # headless browser gets through — only a headed one — and
                    # the host issues no clearance cookie, so every page needs
                    # the browser. Skip the UA swap entirely here: it cannot
                    # work and each attempt is another 403 against the host.
                    if _is_challenge_response(resp):
                        result['cf_challenge'] = True
                        if challenge_browser is not None:
                            _ch_html, _ch_status, _ch_err = challenge_browser.fetch(url)
                            if _ch_html and not _ch_err:
                                challenge_html = _ch_html
                                challenge_status = _ch_status or 200
                                result['challenge_solved'] = True
                            else:
                                result['issues'].append(
                                    'Bot challenge (Cloudflare) could not be solved — page not crawlable: '
                                    + (_ch_err or 'unknown error'))
                        else:
                            result['issues'].append(
                                'Bot challenge (Cloudflare) blocked this page — enable "Solve bot challenges" to crawl this site')
                        break
                    # One UA-swap attempt to dodge Cloudflare-style fingerprinting.
                    # Only swap UA on the first attempt so we don't keep cycling.
                    if attempt == 0:
                        for _alt_ua in (
                            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
                            'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
                        ):
                            try:
                                alt = session.get(url, timeout=15, allow_redirects=True,
                                                  headers={'User-Agent': _alt_ua})
                                if alt.status_code not in (403, 429):
                                    resp = alt
                                    result['ua_fallback'] = _alt_ua.split(')')[0] + ')'
                                    break
                            except Exception:
                                continue
                # Response is either final (2xx/3xx/4xx non-403/429) or exhausted retries.
                break
            # resp is None → transient connection error → retry with backoff
            if attempt < 2:
                retries_done += 1
                time.sleep(0.5 * (2 ** attempt))
                continue
        if resp is None:
            # All retries exhausted
            raise last_exc if last_exc else requests.exceptions.RequestException('Connection failed after retries')
        result['retries'] = retries_done
        if ssl_bypassed:
            result['ssl_verify_failed'] = True
            result['issues'].append('SSL certificate verification failed (incomplete chain / untrusted cert) — crawled with verification disabled; fix the cert chain')
        result['status_code'] = challenge_status if challenge_html else resp.status_code
        result['response_time'] = round(resp.elapsed.total_seconds(), 2)
        result['content_type'] = resp.headers.get('Content-Type', '')[:50]
        result['last_modified'] = resp.headers.get('Last-Modified', '')[:60]

        # Track redirects — classify by type so trivial normalizations (trailing slash,
        # www, https) don't pollute the main Redirect bucket
        if resp.history:
            from urllib.parse import urlparse as _up
            orig = _up(url)
            final = _up(resp.url)
            hops = len(resp.history)
            hop_lbl = f'{hops} hop{"s" if hops > 1 else ""}'
            result['redirect_hops'] = hops
            result['redirect_chain'] = [
                {'url': h.url, 'status': h.status_code} for h in resp.history
            ] + [{'url': resp.url, 'status': resp.status_code}]

            # No-op loop: the server bounced us through 1+ hops but landed
            # right back at the requested URL (same scheme + host + path +
            # query). Common with Wordfence / SiteGround / Cloudflare cookie
            # handshakes. Not a real redirect from the user/SEO perspective —
            # don't set redirect_url, don't append an issue, don't classify.
            # Otherwise the Redirects bucket showed "From: X  To: X" rows
            # the user couldn't action.
            is_noop_loop = (
                orig.scheme == final.scheme
                and orig.netloc.lower() == final.netloc.lower()
                and orig.path == final.path
                and orig.query == final.query
            )

            if not is_noop_loop:
                result['redirect_url'] = resp.url
                # The first hop carries the real 3xx code (301/302/307/308).
                # status_code on the row is the FINAL 200 because we follow
                # redirects, so capture the originating status separately —
                # otherwise the Redirects view can't tell permanent from
                # temporary.
                result['redirect_status'] = resp.history[0].status_code

                same_host = orig.netloc.lstrip('www.') == final.netloc.lstrip('www.')
                same_path_stripped = orig.path.rstrip('/') == final.path.rstrip('/')
                same_qs = orig.query == final.query

                redirect_kind = 'other'
                if same_host and same_path_stripped and same_qs:
                    if orig.scheme != final.scheme and orig.scheme == 'http':
                        redirect_kind = 'http_to_https'
                        result['issues'].append(f'HTTP→HTTPS redirect ({hop_lbl})')
                    elif orig.netloc != final.netloc:
                        redirect_kind = 'www_normalize'
                        result['issues'].append(f'www normalization redirect ({hop_lbl})')
                    elif orig.path != final.path:
                        # Pure trailing slash difference
                        if final.path == orig.path + '/' or orig.path == final.path + '/':
                            redirect_kind = 'trailing_slash'
                            result['issues'].append(f'Trailing slash redirect ({hop_lbl})')
                        else:
                            redirect_kind = 'other'
                            result['issues'].append(f'Redirect ({hop_lbl})')
                    else:
                        redirect_kind = 'other'
                        result['issues'].append(f'Redirect ({hop_lbl})')
                else:
                    result['issues'].append(f'Redirect ({hop_lbl})')

                result['redirect_kind'] = redirect_kind

                # Canonicalize: the row should represent the URL that actually
                # returned 200, not the URL we requested. Without this, the seed
                # AND the redirect target both end up as separate "page" rows when
                # the target is later discovered via an internal link.
                result['url'] = resp.url
                result['original_url'] = url

        # --- URL issues (from the source URL, not redirect target) ---
        from urllib.parse import urlparse as _urlparse2
        _parsed_url = _urlparse2(url)
        _url_path_query = _parsed_url.path + (('?' + _parsed_url.query) if _parsed_url.query else '')
        _url_issues = []
        if any(c.isupper() for c in _parsed_url.path):
            _url_issues.append('uppercase')
        if '_' in _parsed_url.path:
            _url_issues.append('underscores')
        if ' ' in url or '%20' in _parsed_url.path:
            _url_issues.append('contains space')
        if len(url) > 115:
            _url_issues.append(f'over 115 chars ({len(url)})')
        if '//' in _parsed_url.path:
            _url_issues.append('multiple slashes')
        try:
            url.encode('ascii')
        except UnicodeEncodeError:
            _url_issues.append('non-ASCII characters')
        if _parsed_url.query:
            _url_issues.append('contains parameters')
            if any(p in _parsed_url.query for p in ('utm_', 'gclid=', 'fbclid=')):
                _url_issues.append('tracking parameters')
        result['url_issues'] = _url_issues

        # --- Pagination detection ---
        import re as _re_pag
        _pag_path = _re_pag.search(r'/page/\d+/?$', _parsed_url.path)
        # Leading _? catches builder-prefixed params like ?_page=2 —
        # \b alone never fires between '_' and 'page' (both word chars).
        _pag_query = _re_pag.search(r'(?:^|[?&;])\s*_?(page|paged|pg)\s*=\s*[2-9]\d*', _parsed_url.query)
        result['is_pagination'] = bool(_pag_path or _pag_query)

        # --- Security headers (applies to every response, HTML or not) ---
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        sec = {
            'hsts': 'strict-transport-security' in hdrs,
            'csp': 'content-security-policy' in hdrs,
            'x_content_type_options': 'x-content-type-options' in hdrs,
            'x_frame_options': 'x-frame-options' in hdrs,
            'referrer_policy': 'referrer-policy' in hdrs,
            'is_https': _parsed_url.scheme == 'https',
        }
        result['security'] = sec
        result['x_robots_tag'] = hdrs.get('x-robots-tag', '')
        if result['x_robots_tag'] and 'noindex' in result['x_robots_tag'].lower():
            result['indexable'] = False

        # Judge on the effective status: a solved challenge carries the real
        # document even though the original response was a 403 interstitial.
        if result['status_code'] >= 400:
            result['error'] = f"HTTP {result['status_code']}"
            result['issues'].append(f"HTTP {result['status_code']} error")
            return result

        # Skip non-HTML
        ctype = result['content_type'].lower()
        if 'text/html' not in ctype and 'application/xhtml' not in ctype:
            result['issues'].append(f'Non-HTML ({ctype.split(";")[0]})')
            return result

        # Limit body size
        raw_html = (challenge_html or resp.text)[:5_000_000]
        # Keep pre-JS HTML for the JS-vs-non-JS diff (cheap; one extra ref).
        pre_js_html = raw_html
        result['js_rendered'] = False

        # Optional: re-render with Playwright to capture JS-inserted content.
        # Strategy: wait for `load` (fires after all initial subresources), then
        # try (but don't require) networkidle as a short grace window so late
        # analytics scripts (GA4, GTM, FB Pixel) can inject. Wix/Shopify/React
        # SPAs often ping telemetry continuously, so networkidle never settles —
        # we still read .content() regardless so we don't fall back to the
        # empty pre-JS HTML. That silent fallback was why "No analytics
        # detected" fired on JS-rendered sites like Wix.
        # Skipped when the body came from the challenge browser: that HTML is
        # already browser-rendered, and re-fetching through the headless
        # renderer would just land back on the interstitial.
        if pw_page is not None and not challenge_html and ('text/html' in ctype or 'application/xhtml' in ctype):
            try:
                pw_page.goto(resp.url, wait_until='load', timeout=20000)
            except Exception as e:
                # Even if goto times out, the page may have loaded enough of
                # the DOM to be useful — keep going and let content() decide.
                result.setdefault('render_errors', []).append(f'goto: {str(e)[:160]}')
            # Best-effort settle window for late-injected trackers
            try:
                pw_page.wait_for_load_state('networkidle', timeout=4000)
            except Exception:
                pass
            try:
                rendered = pw_page.content()
                if rendered and len(rendered) > 100:
                    raw_html = rendered[:5_000_000]
                    result['js_rendered'] = True
            except Exception as e:
                result.setdefault('render_errors', []).append(f'content: {str(e)[:160]}')

        soup = BeautifulSoup(raw_html, 'html.parser')

        # Title
        title_tag = soup.find('title')
        result['title'] = title_tag.get_text(strip=True) if title_tag else ''
        result['title_len'] = len(result['title'])

        # Meta description
        meta_tag = soup.find('meta', attrs={'name': 'description'})
        result['meta_description'] = meta_tag.get('content', '') if meta_tag else ''
        result['meta_len'] = len(result['meta_description'])

        # H1s and H2s (full lists, not just first/count). Pick the first
        # non-empty H1 for the displayed column value — themes that wrap a
        # logo image in <h1> emit an empty H1 as the first tag, which would
        # otherwise mask a real H1 later in the DOM and cause the row to be
        # flagged as both Missing H1 and Multiple H1s.
        # Pass separator=' ' so inline children (spans, <br>, etc.) don't collide
        # into "connectspeople,pay" when the visible H1 reads "connects people, pay".
        # Webflow/Framer split headings across spans for animated reveals.
        # Also strip the space the separator adds before punctuation tokens.
        import re as _re
        def _clean_heading(tag):
            txt = ' '.join(tag.get_text(' ', strip=True).split())
            return _re.sub(r'\s+([,.;:!?\)\]])', r'\1', txt)
        h1_tags = soup.find_all('h1')
        result['h1_list'] = [_clean_heading(t)[:200] for t in h1_tags]
        result['h1'] = next((t for t in result['h1_list'] if t), '')
        h2_tags = soup.find_all('h2')
        result['h2_list'] = [_clean_heading(t)[:200] for t in h2_tags][:20]
        result['h2_count'] = len(h2_tags)

        # Canonical — classify as self / canonicalised / mismatch
        can_tag = soup.find('link', attrs={'rel': 'canonical'})
        result['canonical'] = can_tag.get('href', '') if can_tag else ''
        if result['canonical']:
            can_abs = urljoin(url, result['canonical'])
            same = can_abs.rstrip('/') == url.rstrip('/') or can_abs.rstrip('/') == resp.url.rstrip('/')
            result['canonical_match'] = same
            if same:
                result['canonical_kind'] = 'self'
            else:
                # canonical points elsewhere — that's a canonicalised page
                result['canonical_kind'] = 'canonicalised'
        else:
            result['canonical_match'] = False
            result['canonical_kind'] = 'missing'

        # Hreflang annotations
        for ln in soup.find_all('link', attrs={'rel': 'alternate'}):
            hl = ln.get('hreflang')
            hf = ln.get('href')
            if hl and hf:
                result['hreflang'].append({'lang': hl, 'href': urljoin(url, hf)})

        # Schema types — schema.org @type can be a single string or a list
        # ("@type": ["Service", "LocalBusiness"]). Flatten to individual
        # strings so the client receives list[str] and rendering doesn't
        # choke on a nested array element.
        def _push_type(val):
            if isinstance(val, list):
                for v in val:
                    if isinstance(v, str):
                        result['schema_types'].append(v)
            elif isinstance(val, str):
                result['schema_types'].append(val)
        for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
            try:
                ld = json.loads(script.string or '')
                if isinstance(ld, dict):
                    if '@type' in ld:
                        _push_type(ld['@type'])
                    if '@graph' in ld:
                        for item in ld['@graph']:
                            if isinstance(item, dict) and '@type' in item:
                                _push_type(item['@type'])
                elif isinstance(ld, list):
                    for item in ld:
                        if isinstance(item, dict) and '@type' in item:
                            _push_type(item['@type'])
            except Exception:
                pass
        # Microdata (itemtype="https://schema.org/Product") — older themes
        # mark up with microdata instead of JSON-LD; without this they read
        # as "no structured data" when they plainly have some.
        for el in soup.find_all(attrs={'itemtype': True})[:25]:
            it = (el.get('itemtype') or '').rstrip('/').rsplit('/', 1)[-1].strip()
            if it and it not in result['schema_types']:
                result['schema_types'].append(it)

        # Open Graph and Twitter Card tags
        for m in soup.find_all('meta'):
            prop = m.get('property', '') or ''
            nm = m.get('name', '') or ''
            content = m.get('content', '') or ''
            if prop.startswith('og:') and content:
                result['og_tags'][prop[3:]] = content[:500]
            elif nm.startswith('twitter:') and content:
                result['twitter_tags'][nm[8:]] = content[:500]

        # Analytics / tracking pixels — inspect script srcs + inline JS
        # Each entry: (label, list of regex patterns to search anywhere in the HTML)
        _TRACKERS = [
            ('GA4',               [r'gtag/js\?id=G-', r"gtag\(\s*'config'\s*,\s*'G-"]),
            ('Universal Analytics', [r'google-analytics\.com/analytics\.js', r"'UA-\d+", r'ua-\d+-\d+']),
            ('Google Tag Manager', [r'googletagmanager\.com/gtm\.js', r"'GTM-[A-Z0-9]+'"]),
            ('Google Ads',        [r'googleadservices\.com/pagead/conversion', r'AW-\d+']),
            ('Facebook Pixel',    [r'connect\.facebook\.net/[^"\']*/fbevents\.js', r"fbq\s*\(\s*['\"]init"]),
            ('TikTok Pixel',      [r'analytics\.tiktok\.com/i18n/pixel']),
            ('LinkedIn Insight',  [r'snap\.licdn\.com/li\.lms-analytics']),
            ('Hotjar',            [r'static\.hotjar\.com/c/hotjar', r'hjSiteSettings', r'\(h,o,t,j,a,r\)']),
            ('Microsoft Clarity', [r'clarity\.ms/tag']),
            ('Mixpanel',          [r'cdn\.mxpnl\.com', r'mixpanel\.init']),
            ('Segment',           [r'cdn\.segment\.com/analytics', r'analytics\.load']),
            ('HubSpot',           [r'js\.hs-scripts\.com', r'js\.hs-analytics\.net']),
            ('Plausible',         [r'plausible\.io/js']),
            ('Fathom',            [r'cdn\.usefathom\.com']),
            ('Matomo/Piwik',      [r'matomo\.php', r'_paq\.push']),
            ('Crazy Egg',         [r'script\.crazyegg\.com']),
            ('Microsoft Ads UET', [r'bat\.bing\.com/bat\.js']),
            ('Pinterest Tag',     [r'pintrk\s*\(\s*[\'"]load']),
            ('Snapchat Pixel',    [r'sc-static\.net/scevent']),
        ]
        # Only scan the raw HTML once; much cheaper than re-traversing soup per pattern
        for label, patterns in _TRACKERS:
            for pat in patterns:
                if _re.search(pat, raw_html, _re.I):
                    result['analytics'].append(label)
                    break

        # Indexability
        robots_tag = soup.find('meta', attrs={'name': _re.compile(r'^robots$', _re.I)})
        robots_content = robots_tag.get('content', '').lower() if robots_tag else ''
        if 'noindex' in robots_content:
            result['indexable'] = False
            result['issues'].append('noindex')

        # Word count — strip nav/footer/script/style + class-based nav for
        # non-semantic sites (Elementor/Divi/WP themes that render nav inside
        # <div class="elementor-nav-menu">). Also prefer <main>/<article>
        # content when present so the body word count reflects actual content,
        # not menu/footer boilerplate (otherwise thin-content detection misfires).
        soup_body = BeautifulSoup(raw_html, 'html.parser')
        for tag in soup_body(['script', 'style', 'nav', 'footer', 'header', 'iframe', 'noscript', 'form', 'svg']):
            tag.decompose()
        try:
            nav_like_re = _re.compile(
                r'(main[-_]?menu|primary[-_]?menu|site[-_]?nav|header[-_]?nav|'
                r'mega[-_]?menu|nav[-_]?menu|top[-_]?menu|mobile[-_]?menu|sub[-_]?menu|'
                r'breadcrumb|footer[-_]?menu|site[-_]?header|site[-_]?footer|'
                r'menu[-_]?wrap|navbar|navigation|'
                r'elementor-nav-menu|elementor-menu|elementor-widget-nav-menu|'
                r'et_pb_menu|divi-menu|menu-item-has-children|\bmenu\b|'
                r'offcanvas|hamburger|dropdown-menu)',
                _re.I,
            )
            role_nav_re = _re.compile(r'^(navigation|menubar|menu)$', _re.I)
            for el in list(soup_body.find_all(['div', 'section', 'ul', 'aside'])):
                if not el.parent:
                    continue
                classes = ' '.join(el.get('class') or [])
                ident = el.get('id') or ''
                role = el.get('role') or ''
                aria = el.get('aria-label') or ''
                if (nav_like_re.search(classes) or nav_like_re.search(ident)
                        or role_nav_re.match(role) or nav_like_re.search(aria)):
                    el.decompose()
        except Exception:
            pass
        # Pick the first <article> that ISN'T a post-grid / related-posts card.
        # Elementor and many blog themes render recent/related posts as
        # <article class="elementor-post elementor-grid-item ...">; grabbing the
        # first of those captures the same boilerplate snippet on every page, so
        # every post looks like an exact body duplicate. Skip those cards.
        def _real_article(sb):
            for art in sb.find_all('article'):
                cls = ' '.join(art.get('class') or [])
                if _re.search(r'elementor-post|elementor-grid-item|elementor-posts|post-grid|related|recent[-_]?post|widget', cls, _re.I):
                    continue
                return art
            return None

        main_container = (
            soup_body.find('main')
            or soup_body.find(attrs={'role': 'main'})
            # Elementor Theme Builder renders the real post body in this widget
            # and ships no <main>/<article> wrapper, so it must come before the
            # generic <article> fallback (which would otherwise hit a grid card).
            or soup_body.find(attrs={'class': _re.compile(r'elementor-widget-theme-post-content', _re.I)})
            or _real_article(soup_body)
            or soup_body.find(id=_re.compile(r'^(main|content|primary|page-content)$', _re.I))
            or soup_body.find(attrs={'class': _re.compile(r'(^|\s)(main-content|page-content|entry-content|post-content|article-content|site-main)(\s|$)', _re.I)})
        )
        text_root = main_container if main_container else soup_body
        body_text = text_root.get_text(separator=' ', strip=True)
        result['word_count'] = len(body_text.split()) if body_text else 0
        # Cap body text at 30k chars — sufficient for shingle-based near-dup
        # detection on any reasonable page; keeps the SSE payload bounded.
        result['body_text'] = (body_text[:30_000] if body_text else '')

        # Body hash for exact-duplicate detection — normalize whitespace first
        import hashlib as _hashlib
        _norm = ' '.join(body_text.lower().split())
        result['body_hash'] = _hashlib.md5(_norm.encode('utf-8', errors='ignore')).hexdigest() if _norm else ''

        # Mixed content: HTTPS page loading HTTP resources.
        # <link> is rel-dependent — most rels are pure metadata (rel="profile"
        # for XFN, rel="canonical", rel="alternate", rel="EditURI"/"pingback"/
        # "https://api.w.org/" for WP) and don't trigger any fetch. Browsers
        # only fire mixed-content warnings on the rels below.
        _MIXED_LINK_RELS = {
            'stylesheet',
            'preload', 'prefetch', 'modulepreload',
            'icon', 'shortcut icon', 'apple-touch-icon',
            'apple-touch-icon-precomposed', 'mask-icon', 'fluid-icon',
            'manifest',
        }
        if result.get('security', {}).get('is_https'):
            mixed = []
            for tag_name, attr in (('img', 'src'), ('script', 'src'),
                                    ('iframe', 'src'), ('video', 'src'),
                                    ('audio', 'src'), ('source', 'src')):
                for t in soup.find_all(tag_name, attrs={attr: True}):
                    v = (t.get(attr) or '').strip()
                    if v.startswith('http://'):
                        mixed.append(v)
            for t in soup.find_all('link', attrs={'href': True}):
                v = (t.get('href') or '').strip()
                if not v.startswith('http://'):
                    continue
                rels = [r.lower() for r in (t.get('rel') or [])]
                if any(r in _MIXED_LINK_RELS for r in rels):
                    mixed.append(v)
            result['mixed_content'] = mixed[:20]

        # Images — smarter than "any <img> without alt".
        # Matches what Google + WCAG actually care about:
        #   - Missing `alt` attribute entirely = real issue (flag)
        #   - `alt=""` explicitly = decorative, correct pattern (info, not issue)
        #   - 1x1 tracking pixels, aria-hidden / role=presentation, or images
        #     inside an <a>/<button> that already has accessible text → skipped
        #     (the image doesn't need its own alt for screen readers).
        imgs = soup.find_all('img')
        result['images_total'] = len(imgs)
        no_alt_imgs = []              # offending — no alt attr AND not decorative
        no_alt_data = []              # rich per-image records — feeds the missing-alt detail row
        all_images_data = []          # every meaningful image on the page — feeds the All Images panel
        empty_alt_count = 0           # <img alt=""> on genuinely decorative imagery — correct pattern
        empty_alt_content_imgs = []   # <img alt=""> on what looks like content (photo/screenshot/logo) — needs review
        skipped_decorative = 0        # skipped via heuristics (tracker px, aria-hidden, widgets, labeled parent)

        def _has_accessible_name(node):
            """Whether a parent link/button carries its own accessible name —
            text, aria-label, aria-labelledby, or title (own or on a descendant
            img per W3C accessible-name algorithm) — making a missing/empty img
            alt fine for screen readers."""
            if not node:
                return False
            if (node.get('aria-label') or '').strip():
                return True
            if (node.get('aria-labelledby') or '').strip():
                return True
            if (node.get('title') or '').strip():
                return True
            try:
                for d in node.find_all('img'):
                    if (d.get('title') or '').strip():
                        return True
                    if (d.get('aria-label') or '').strip():
                        return True
            except Exception:
                pass
            clone = node
            txt = (clone.get_text(separator=' ', strip=True) or '')
            return len(txt) >= 2

        for img in imgs:
            alt_attr = img.get('alt')  # None if missing, '' if empty, str otherwise
            src = img.get('src', '') or img.get('data-src', '') or ''

            # Decorative / non-content filters — applied to BOTH the
            # missing-alt list and the All Images list. We don't want
            # tracking pixels or hidden spacers polluting either view.
            aria_hidden = (img.get('aria-hidden') or '').lower() == 'true'
            role = (img.get('role') or '').lower()
            w = (img.get('width') or '').strip()
            h = (img.get('height') or '').strip()
            is_hidden = aria_hidden or role in ('presentation', 'none')
            is_tracker = w in ('1', '0') or h in ('1', '0')
            is_data_uri = src.startswith('data:')

            if is_hidden or is_tracker or is_data_uri or not src:
                if alt_attr == '':
                    empty_alt_count += 1
                else:
                    skipped_decorative += 1
                continue

            # Resolve against the post-redirect URL so an http→https
            # redirect doesn't produce phantom http image URLs.
            _img_base = (getattr(resp, 'url', None) or url) if resp is not None else url
            abs_src = urljoin(_img_base, src)

            # Third-party widget filter — reCAPTCHA badges, analytics pixels,
            # chat widgets etc. The site owner can't write alt text for them
            # and they pollute every page that has a contact form.
            if _is_third_party_widget_image(abs_src):
                skipped_decorative += 1
                continue

            # Capture parent + surrounding once per image — used by both
            # the missing-alt detail row and the All Images panel.
            _ptag = img.find_parent(['a', 'button', 'figure'])
            _ptag_name = _ptag.name if _ptag else ''
            _ptext = (_ptag.get_text(separator=' ', strip=True)[:200]
                      if _ptag else '')
            _block = img.find_parent(['p', 'div', 'section', 'article', 'figure', 'li'])
            _surrounding = (_block.get_text(separator=' ', strip=True)[:400]
                            if _block else '')
            # Per W3C accessible-name computation: when alt="" inside a link,
            # the link still has an accessible name if the img carries
            # title/aria-label OR the link itself does. Don't flag those as
            # "empty in link" — screen readers do read them.
            if alt_attr is None:
                _classification = 'missing'
            elif alt_attr.strip() == '':
                in_interactive = _ptag_name in ('a', 'button')
                if in_interactive:
                    img_title = (img.get('title') or '').strip()
                    img_aria = (img.get('aria-label') or '').strip()
                    parent_named = bool(_ptag) and _has_accessible_name(_ptag)
                    if img_title or img_aria or parent_named:
                        _classification = 'empty'
                    else:
                        _classification = 'empty in link'
                elif _filename_looks_decorative(abs_src):
                    _classification = 'empty'
                else:
                    _classification = 'empty (likely content)'
            else:
                _classification = 'present'

            # Record into All Images (every meaningful image, capped at 50/page).
            if len(all_images_data) < 50:
                all_images_data.append({
                    'src': abs_src,
                    'alt': alt_attr,
                    'classification': _classification,
                    'parent_tag': _ptag_name,
                    'parent_text': _ptext,
                    'surrounding': _surrounding,
                })

            # Branch into the existing alt-correctness counters/lists.
            if alt_attr == '':
                if _classification == 'empty (likely content)':
                    empty_alt_content_imgs.append(abs_src)
                    if len(no_alt_data) < 20:
                        no_alt_data.append({
                            'src': abs_src,
                            'alt': alt_attr,
                            'classification': _classification,
                            'parent_tag': _ptag_name,
                            'parent_text': _ptext,
                            'surrounding': _surrounding,
                        })
                else:
                    empty_alt_count += 1
                continue
            if alt_attr is not None and alt_attr.strip():
                continue  # Has meaningful alt — good

            # alt is missing entirely. Skip if the parent link/button
            # already carries the accessible name.
            parent_interactive = img.find_parent(['a', 'button'])
            if parent_interactive and _has_accessible_name(parent_interactive):
                skipped_decorative += 1
                continue

            no_alt_imgs.append(abs_src)
            if len(no_alt_data) < 20:
                no_alt_data.append({
                    'src': abs_src,
                    'alt': alt_attr,
                    'classification': _classification,
                    'parent_tag': _ptag_name,
                    'parent_text': _ptext,
                    'surrounding': _surrounding,
                })

        result['images_no_alt'] = len(no_alt_imgs)
        result['images_no_alt_urls'] = no_alt_imgs[:20]  # cap at 20 per page
        result['images_no_alt_data'] = no_alt_data       # rich per-image records (cap 20)
        result['images_all_data']    = all_images_data   # every meaningful img (cap 50) — feeds All Images panel
        result['images_empty_alt'] = empty_alt_count
        result['images_empty_alt_content'] = len(empty_alt_content_imgs)
        result['images_empty_alt_content_urls'] = empty_alt_content_imgs[:20]
        result['images_decorative_skipped'] = skipped_decorative

        # Links - extract internal + external with anchor text + placement.
        # Placement = which site region the link sits in (nav / header / footer / main),
        # so users can tell boilerplate links from content links — like Screaming Frog.
        def _placement(a_tag):
            for ancestor in a_tag.parents:
                name = (getattr(ancestor, 'name', None) or '').lower()
                if not name: continue
                if name in ('nav',): return 'nav'
                if name in ('header',): return 'header'
                if name in ('footer',): return 'footer'
                if name in ('aside',): return 'sidebar'
                # Check role / class hints
                classes = ' '.join(ancestor.get('class', []) if hasattr(ancestor, 'get') else []).lower()
                role = (ancestor.get('role', '') if hasattr(ancestor, 'get') else '').lower()
                if 'nav' in classes or role == 'navigation': return 'nav'
                if 'footer' in classes: return 'footer'
                if 'header' in classes: return 'header'
                if name == 'main': return 'main'
            return 'body'

        # Resolve relative hrefs against the FINAL URL after redirects, not
        # the request URL. Otherwise crawling http://example.com/ (which 301s
        # to https://example.com/) produces http://example.com/services for
        # every <a href="/services"> in the body — bogus HTTP URLs that don't
        # exist anywhere in the actual HTML.
        link_base = (getattr(resp, 'url', None) or url) if resp is not None else url
        # Honor <base href> when present. The HTML spec says relative URLs
        # resolve against <base href>, not the document URL. Sites that use
        # directory-style URLs (/page/) + relative links without a leading
        # slash (href="other/") otherwise spawn infinite phantom nested paths
        # (/page/other/, /page/other/more/, …) whenever the server returns 200
        # for arbitrary depths — a crawler trap that buries real pages.
        base_tag = soup.find('base', href=True)
        if base_tag:
            base_href = (base_tag.get('href') or '').strip()
            if base_href:
                link_base = urljoin(link_base, base_href)
        int_links = {}      # normalized target -> {anchor, placement}
        ext_links_list = [] # external links captured with anchor + placement
        ext_count = 0
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if not href:
                continue
            href_low = href.lower()
            # Case-insensitive scheme filter (catches Mailto:, MAILTO:, etc.)
            if (href_low.startswith('#')
                or href_low.startswith('javascript:')
                or href_low.startswith('mailto:')
                or href_low.startswith('tel:')
                or href_low.startswith('sms:')
                or href_low.startswith('skype:')
                or href_low.startswith('whatsapp:')
                or href_low.startswith('data:')
                or href_low.startswith('file:')):
                continue
            # Bare email written without the mailto: prefix —
            # <a href="sales@example.com">. urljoin would otherwise produce
            # https://example.com/path/sales@example.com — the email lands
            # in the crawl as a fake URL.
            if '@' in href and _MAILTO_NO_SCHEME_RE.match(href):
                continue
            # Plain text pasted into an href — e.g. a street address or a
            # Google Maps Plus Code (<a href="7FG4+8Q Springfield, Example
            # State">). A raw space can't appear in a real URL, and
            # with no scheme urljoin resolves the text relative to the
            # CURRENT page, so a site-wide footer link like this
            # manufactures a phantom child 404 under every page crawled
            # (300-page site → ~300 fake 404s). Skip it as a link but
            # record a page issue so the broken href itself still gets
            # reported once per page instead of as hundreds of 404 URLs.
            if ' ' in href and not href.startswith('//') and not _HREF_SCHEME_RE.match(href):
                _bad_href = href if len(href) <= 80 else href[:77] + '…'
                _bad_issue = f'Malformed link href (text, not a URL): "{_bad_href}"'
                if _bad_issue not in result['issues']:
                    result['issues'].append(_bad_issue)
                continue
            resolved = urljoin(link_base, href)
            resolved_path_tail = urlparse(resolved).path.rstrip('/').rsplit('/', 1)[-1]
            if '@' in resolved_path_tail and _MAILTO_NO_SCHEME_RE.match(resolved_path_tail):
                continue
            link_domain = urlparse(resolved).netloc.lower().replace('www.', '')
            # Anchor text: prefer visible text, fall back to aria-label / alt of child <img>
            anchor = (a.get_text(separator=' ', strip=True) or '')[:180]
            if not anchor:
                aria = a.get('aria-label') or a.get('title')
                if aria:
                    anchor = aria.strip()[:180]
                else:
                    img_child = a.find('img')
                    if img_child:
                        alt = (img_child.get('alt') or '').strip()[:140]
                        src = (img_child.get('src') or '').strip()
                        # Extract filename so two images with similar alts (e.g. header
                        # logo "Todd Devine Homes text logo" vs tile image "Todd Homes")
                        # can be disambiguated in the inlinks drawer.
                        fname = ''
                        if src:
                            fname = src.split('?', 1)[0].rstrip('/').split('/')[-1][:80]
                        if alt and fname:
                            anchor = f'[image: {alt} — {fname}]'
                        elif alt:
                            anchor = f'[image: {alt}]'
                        elif fname:
                            anchor = f'[image: {fname}]'
                        else:
                            anchor = '[image]'
            if not anchor:
                anchor = '(empty anchor)'
            placement = _placement(a)

            if link_domain == domain:
                normalized = _normalize_crawl_url(resolved)
                if normalized not in int_links:
                    int_links[normalized] = (anchor, placement)
            else:
                ext_count += 1
                if len(ext_links_list) < 300:  # cap payload
                    # Capture rel + target for the External Links report.
                    # rel is multi-token ("nofollow ugc sponsored noopener") —
                    # join with spaces and lowercase for cheap substring checks.
                    rel_attr = a.get('rel') or []
                    if isinstance(rel_attr, list):
                        rel_str = ' '.join(rel_attr).strip().lower()
                    else:
                        rel_str = str(rel_attr).strip().lower()
                    target_attr = (a.get('target') or '').strip().lower()
                    ext_links_list.append([resolved, anchor, placement, rel_str, target_attr])

        # Transport (internal): [[target, anchor, placement], ...]
        # Transport (external): [[target, anchor, placement, rel, target_attr], ...]
        result['internal_link_urls'] = [[t, a, p] for t, (a, p) in int_links.items()]
        result['internal_links'] = len(int_links)
        result['external_link_urls'] = ext_links_list
        result['external_links'] = ext_count

        # Issues detection — skip content/SEO checks for noindex or pagination pages
        # (noindex = Google won't rank it; pagination = archive duplicate, not a canonical page)
        # Also skip URLs that redirected: the resolved target is crawled separately and
        # any content issues belong on that row, not on the 301 source.
        # When ignore_noindex is set, treat noindex pages like indexable ones for
        # the audit so the user sees the full warning/info list, not just the flag.
        # ALSO skip canonicalised pages — they're declared duplicates so any
        # 'Missing meta' / 'Missing title' / 'Thin content' on them is noise;
        # those issues are real on the canonical page and would surface there.
        # The page itself still appears under the 'Canonicalised' report.
        is_canonicalised = result.get('canonical_kind') == 'canonicalised'
        # Status guard: only run content checks on 2xx responses. 3xx/4xx/5xx
        # responses don't have meaningful content; e.g. an unfollowed 301
        # has empty body, so without this guard it would surface "Missing meta
        # description / Missing title / Missing H1" even though the source is
        # just a redirect that never returns HTML.
        _status = result.get('status_code', 0) or 0
        if (result['indexable'] or ignore_noindex) and not result.get('is_pagination') and not result.get('redirect_url') and not is_canonicalised and 200 <= _status < 300:
            if not result['title']:
                result['issues'].append('Missing title')
            elif result['title_len'] > 60:
                result['issues'].append(f'Title too long ({result["title_len"]})')
            elif result['title_len'] < 30:
                result['issues'].append(f'Title too short ({result["title_len"]})')

            if not result['meta_description']:
                result['issues'].append('Missing meta description')
            elif result['meta_len'] > 160:
                result['issues'].append(f'Meta desc too long ({result["meta_len"]})')
            elif result['meta_len'] < 70:
                result['issues'].append(f'Meta desc too short ({result["meta_len"]})')

            # "Missing H1" only fires when no H1 tag has any text — an
            # <h1></h1> wrapping a logo image doesn't make the page
            # "missing" if a populated H1 sits below it.
            h1_count = len(result['h1_list'])
            if not result['h1']:
                result['issues'].append('Missing H1')
            elif h1_count > 1:
                result['issues'].append(f'Multiple H1s ({h1_count})')
            if result['h1'] and result['title'] and result['h1'].strip().lower() == result['title'].strip().lower():
                result['issues'].append('H1 same as title')

            if result['canonical_kind'] == 'missing':
                result['issues'].append('Missing canonical')
            elif result['canonical_kind'] == 'canonicalised':
                result['issues'].append('Canonicalised (points elsewhere)')

            if result['word_count'] < 200:
                result['issues'].append(f'Thin content ({result["word_count"]} words)')

            if result['images_no_alt'] > 0:
                result['issues'].append(f'{result["images_no_alt"]} imgs missing alt')
            if result.get('images_empty_alt_content', 0) > 0:
                result['issues'].append(f'{result["images_empty_alt_content"]} imgs with empty alt on content imagery')

            if not result['schema_types']:
                result['issues'].append('No schema')

            # Viewport
            if not soup.find('meta', attrs={'name': _re.compile(r'^viewport$', _re.I)}):
                result['issues'].append('Missing viewport')

            # Open Graph — any indexable page should have at least og:title + og:image for social sharing
            og = result['og_tags']
            if not og.get('title') and not og.get('image'):
                result['issues'].append('Missing Open Graph tags')
            elif not og.get('image'):
                result['issues'].append('Missing og:image')

            # Twitter Card — not critical but worth flagging
            if not result['twitter_tags']:
                result['issues'].append('Missing Twitter Card')

            # Analytics — flag pages with no tracking at all. But if the site
            # is clearly JS-rendered (Wix, Shopify, Squarespace, Webflow, or
            # client-side React/Vue/Next) and we crawled without JS rendering,
            # the HTML we scanned was the unhydrated shell — so "No analytics
            # detected" is a false negative. Emit a more actionable warning
            # instead so the user knows to re-run with Render JS enabled.
            if not result['analytics']:
                platform = _detect_js_platform(raw_html) if not result.get('js_rendered') else None
                if platform:
                    result['js_platform'] = platform
                    result['issues'].append(f'Analytics unknown — {platform} site needs Render JS')
                else:
                    result['issues'].append('No analytics detected')

        # --- Issues that apply regardless of indexability ---
        # Canonicalised flag — surfaced even when content checks are skipped
        # so the page still appears under the Canonicalised report.
        if is_canonicalised:
            result['issues'].append('Canonicalised (points elsewhere)')
        if result['response_time'] > 3:
            result['issues'].append(f'Slow ({result["response_time"]}s)')

        # URL hygiene (Screaming Frog URL tab)
        for ui in result.get('url_issues', []):
            result['issues'].append(f'URL: {ui}')

        # Mixed content
        if result['mixed_content']:
            result['issues'].append(f'Mixed content ({len(result["mixed_content"])} resources)')

        # Security headers — we still capture their presence in result['security']
        # for the page-detail panel, but we don't flag missing HSTS / X-Content-Type-Options
        # / X-Frame-Options / CSP / Referrer-Policy as issues (low signal-to-noise for SEO).
        sec = result.get('security', {})
        if not sec.get('is_https'):
            result['issues'].append('Served over HTTP (insecure)')

        # JS-vs-non-JS diff. Only when caller opted in AND we successfully
        # rendered a JS version different from the pre-JS HTML. Surfaces
        # JS-only content that's invisible to non-rendering AI crawlers.
        if capture_no_js and result.get('js_rendered') and pre_js_html:
            try:
                base = result.get('url') or url
                nojs = _parse_no_js_subset(pre_js_html, base)
                result['non_js'] = nojs
                result['js_diff'] = _compute_js_diff(result, nojs)
                sev = result['js_diff'].get('severity')
                if sev == 'critical':
                    fields = ', '.join(result['js_diff'].get('fields', [])) or 'title/meta/schema'
                    result['issues'].append(f'JS-only content (critical: {fields})')
                elif sev == 'high':
                    fields = ', '.join(result['js_diff'].get('fields', [])) or 'h1/word_count'
                    result['issues'].append(f'JS-only content (high: {fields})')
            except Exception as e:
                result.setdefault('render_errors', []).append(f'no-js diff: {str(e)[:160]}')

    except requests.exceptions.Timeout:
        result['error'] = 'Timeout'
        result['issues'].append('Timeout')
    except requests.exceptions.SSLError as e:
        # Reached only when even verify=False failed (rare). Still tell the
        # user it's a certificate problem, not a vague "connection error".
        result['error'] = 'SSL certificate error'
        _m = str(e)
        if 'CERTIFICATE_VERIFY_FAILED' in _m or 'unable to get local issuer' in _m or 'self signed' in _m.lower():
            result['issues'].append('SSL certificate verification failed (incomplete chain or untrusted/self-signed cert) — loads in browsers but blocks crawlers')
        else:
            result['issues'].append('SSL error: ' + str(e)[:90])
    except requests.exceptions.ConnectionError as e:
        # Distinguish a true connection failure (DNS/refused/reset) from the
        # generic bucket so the user can act on it.
        result['error'] = 'Connection error'
        _m = str(e).lower()
        if 'name or service not known' in _m or 'failed to resolve' in _m or 'nodename nor servname' in _m:
            result['issues'].append('Connection error — DNS lookup failed (domain not resolving)')
        elif 'refused' in _m:
            result['issues'].append('Connection error — connection refused (server not accepting connections)')
        elif 'reset' in _m:
            result['issues'].append('Connection error — connection reset (often bot/WAF blocking non-browser clients)')
        else:
            result['issues'].append('Connection error')
    except Exception as e:
        result['error'] = str(e)[:100]
        result['issues'].append(f'Error: {str(e)[:60]}')

    return result



def crawl_site():
    """BFS site crawl with SSE streaming of per-page results."""
    from collections import deque
    from urllib.parse import urlparse

    data = request.json

    # Resume support: if resume_crawl_id is provided AND we have saved state
    # for it that's not yet expired, restore everything (config + queue +
    # visited + results + inlinks). Otherwise fall through to a fresh crawl.
    resume_id = (data.get('resume_crawl_id') or '').strip()
    resumed_state = None
    if resume_id:
        cached = SUSPENDED_CRAWLS.pop(resume_id, None)
        if cached and (time.time() - cached.get('created', 0)) <= SUSPENDED_CRAWL_TTL:
            resumed_state = cached
        elif cached:
            current_app.logger.info(f"[crawler] resume {resume_id} expired, falling back to fresh")

    if resumed_state:
        cfg = resumed_state.get('config', {})
        seed_url = resumed_state.get('seed_url') or (data.get('url', '') or '').strip()
        max_pages = int(data.get('max_pages') or cfg.get('max_pages') or 500)
        if max_pages >= 5000:
            max_pages = 999999
        max_depth = min(int(cfg.get('max_depth', 10) or 10), 20)
        crawl_delay = max(float(cfg.get('crawl_delay', 0.4) or 0.4), 0.0)
        render_js = bool(cfg.get('render_js', False))
        ignore_robots = bool(cfg.get('ignore_robots', False))
        ignore_noindex = bool(cfg.get('ignore_noindex', False))
        compare_no_js = bool(cfg.get('compare_no_js', False)) and render_js
        user_agent_opt = cfg.get('user_agent') or ''
        solve_challenges = bool(cfg.get('solve_challenges', True))
    else:
        seed_url = (data.get('url', '') or '').strip()
        if not seed_url:
            return json.dumps({'error': 'URL is required'}), 400
        if not seed_url.startswith('http'):
            seed_url = 'https://' + seed_url

        max_pages = int(data.get('max_pages', 500) or 500)
        if max_pages >= 5000:
            max_pages = 999999  # unlimited
        max_depth = min(int(data.get('max_depth', 10) or 10), 20)
        crawl_delay = max(float(data.get('crawl_delay', 0.4) or 0.4), 0.0)
        render_js = bool(data.get('render_js', False))
        ignore_robots = bool(data.get('ignore_robots', False))
        ignore_noindex = bool(data.get('ignore_noindex', False))
        # JS vs non-JS compare. Gated on render_js — only meaningful when
        # we have something to compare against.
        compare_no_js = bool(data.get('compare_no_js', False)) and render_js
        # Crawl identity. Accepts a preset key ('googlebot', 'bingbot', ...) or
        # a raw UA string; blank keeps the default Chrome identity.
        user_agent_opt = data.get('user_agent') or ''
        # Headed-browser fallback for hosts behind a bot challenge. On by
        # default: it only launches if a challenge is actually encountered.
        solve_challenges = bool(data.get('solve_challenges', True))
    # Concurrent workers. Default 5 matches Screaming Frog. Clamped to [1, 20].
    # When render_js is on, Playwright can't share a single page across threads —
    # force single-worker mode so page state stays consistent.
    max_workers = int(data.get('max_workers', 5) or 5)
    max_workers = max(1, min(20, max_workers))
    if render_js:
        max_workers = 1

    # URL include/exclude patterns — robots.txt syntax (Google's spec):
    #   *       matches any sequence
    #   $       at end anchors end of URL
    #   ?, .    are LITERAL (no fnmatch single-char wildcard surprise)
    # Exclude beats include. If include is non-empty, URLs must match at least one.
    # Stored in ACTIVE_CRAWL_RULES so /crawl/update-rules can mutate them mid-crawl.
    import uuid as _uuid
    def _parse_patterns(raw):
        if not raw: return []
        return [p.strip() for p in raw.splitlines() if p.strip() and not p.strip().startswith('#')]
    crawl_id = _uuid.uuid4().hex[:12]
    # crawl_delay also lives here so /crawl/update-rules can mutate it mid-crawl
    # (the slider in the UI auto-pushes the new value while a crawl is running).
    ACTIVE_CRAWL_RULES[crawl_id] = {
        'include': _parse_patterns(data.get('include_patterns', '')),
        'exclude': _parse_patterns(data.get('exclude_patterns', '')),
        'crawl_delay': crawl_delay,
    }
    ACTIVE_CRAWL_LIMITS[crawl_id] = {
        'max_pages': max_pages,
        'continue_event': threading.Event(),
        'finalize': False,
        'bumps': 0,
    }

    def _current_max():
        return (ACTIVE_CRAWL_LIMITS.get(crawl_id) or {}).get('max_pages', max_pages)

    # /cdn-cgi/ — Cloudflare infra paths injected by the proxy. The most
    # common is /cdn-cgi/l/email-protection (the obfuscated-email endpoint
    # Cloudflare auto-injects) which returns 404 when fetched directly
    # because it's only meant to be loaded as a script via the email
    # obfuscation. /cdn-cgi/scripts/, /cdn-cgi/challenge-platform/,
    # /cdn-cgi/rum, /cdn-cgi/bm/ etc. are all infra, not site content.
    # Skipping the whole prefix keeps the 404 report focused on real pages.
    _NON_PAGE_PATH_FRAGMENTS = (
        '/feed/', '/feed.atom', '/feed.rss', '/comments/feed/',
        '/wp-json/', '/wp-admin/', '/wp-login.php', '/xmlrpc.php',
        '/?wc-ajax=', '/cart/?', '/checkout/?',
        '/sitemap.xml', '/sitemap_index.xml',
        '/cdn-cgi/',
    )

    def _url_allowed(u):
        # Drop media files + non-HTML endpoints so they never enter the
        # results table or pollute per-page reports (Schema by Page, etc.).
        if _is_non_html_url(u):
            return False
        try:
            _pu = urlparse(u)
            path_lower = (_pu.path or '').lower()
            query_lower = (_pu.query or '').lower()
        except Exception:
            path_lower = ''
            query_lower = ''
        if any(frag in path_lower for frag in _NON_PAGE_PATH_FRAGMENTS):
            return False
        # Page-builder AJAX pagination/filter traps. Elementor Pro's Posts/Loop
        # widget paginates via ?e-page-<widgetid>=N (and filters via
        # ?e-filter-<id>=...), so a single listing page exposes hundreds of
        # query-string variants — /commentary/ on a real site linked
        # ?e-page-...=854 — each a rel=canonical duplicate of the base page.
        # A link-following crawler chases every one, AND each variant re-exposes
        # the next, so the queue explodes combinatorially toward infinity (the
        # classic "26k queued on a 9k-page site" blow-up). These are never
        # content you want indexed, so we skip them by default — matched on the
        # query-param KEY so it's widget-id- and page-number-agnostic. Jetpack
        # Infinite Scroll (?infinity) gets the same treatment.
        if query_lower:
            for _part in query_lower.split('&'):
                _qk = _part.split('=', 1)[0].strip()
                if _qk.startswith('e-page-') or _qk.startswith('e-filter-') or _qk == 'infinity':
                    return False
        # Repeating-segment guard: refuses URLs where the same path slug
        # appears more than once (e.g. /our-team/our-team/ or
        # /case-studies/our-team/case-studies/). This is the classic
        # crawler trap caused by sites whose nav uses path-relative hrefs
        # ("our-team/" instead of "/our-team/") - resolved against any
        # sub-page, the relative link bleeds the parent path infinitely.
        # Legitimate URLs almost never repeat the same slug 2+ times in a
        # path, so this is a high-precision filter. Short numeric segments
        # ("/2026/01/01/") are excluded from the dedup check so date-based
        # archives still resolve.
        try:
            _segs = [s for s in path_lower.strip('/').split('/') if s]
            _word_segs = [s for s in _segs if not s.isdigit() and len(s) >= 4]
            if len(_word_segs) != len(set(_word_segs)):
                return False
        except Exception:
            pass
        rules = ACTIVE_CRAWL_RULES.get(crawl_id) or {}
        excl = rules.get('exclude') or []
        incl = rules.get('include') or []
        if excl and any(_robots_pattern_match(p, u) for p in excl):
            return False
        if incl and not any(_robots_pattern_match(p, u) for p in incl):
            return False
        return True

    parsed = urlparse(seed_url)
    domain = parsed.netloc.lower().replace('www.', '')

    current_app.logger.info(f"[crawler] Starting crawl of {seed_url} (max={max_pages}, depth={max_depth}, delay={crawl_delay}s, js={render_js}) from {request.remote_addr}")

    def generate():
        # Fetch robots.txt up-front so the user sees what we're following.
        # If ignore_robots is set, we still fetch (informational) but won't enforce.
        # We replace stdlib urllib.robotparser with our own wildcard-aware
        # checker because stdlib silently drops '*' and '$' patterns - any
        # site whose robots.txt uses 'Disallow: *?swoof*' or 'Disallow: *.pdf*'
        # would otherwise leak those URLs into the crawl.
        robots_can_fetch = lambda u: True
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        robots_status = 'fetching'
        robots_rules = 0
        robots_issues = []        # AI/search-engine blocking flags, attached to the homepage row
        robots_attached = False   # attach once, to the first (seed) page emitted
        try:
            resp = _http_get(robots_url, timeout=8, headers={'User-Agent': 'SEO-Audit-Bot'})
            if resp.status_code == 200:
                robots_can_fetch = _build_robots_checker(resp.text)
                robots_rules = resp.text.count('Disallow')
                robots_status = 'ignored' if ignore_robots else 'respecting'
                robots_issues = _analyze_robots_txt(resp.text)
                yield f"data: {json.dumps({'type':'info','msg':f'Downloaded robots.txt ({robots_rules} Disallow rules) — {robots_status}'})}\n\n"
                if robots_issues:
                    yield f"data: {json.dumps({'type':'info','msg':'robots.txt: ' + '; '.join(robots_issues)})}\n\n"
            else:
                robots_status = 'not found'
                yield f"data: {json.dumps({'type':'info','msg':f'robots.txt returned HTTP {resp.status_code} — no rules to enforce'})}\n\n"
        except Exception as e:
            robots_status = 'error'
            yield f"data: {json.dumps({'type':'info','msg':f'robots.txt unreachable ({str(e)[:80]}) — continuing without'})}\n\n"

        session = requests.Session()
        # Realistic Chrome header set. Default python-requests UA + empty
        # Accept-Language triggers 403 on Shopify/Cloudflare-fronted stores.
        # Sec-Fetch-Site=same-origin is correct for crawl traffic (we follow
        # links from a previously fetched page on the same host).
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-AU,en;q=0.9,en-US;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Ch-Ua': '"Chromium";v="132", "Not_A Brand";v="24"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
        })

        # Crawl identity override (Googlebot/bingbot/mobile/custom). Applied to
        # the requests session and mirrored onto the browsers below so every
        # fetch path presents the same agent.
        crawl_ua = session.headers['User-Agent']
        if user_agent_opt:
            try:
                from challenge_browser import resolve_user_agent
                _ua = resolve_user_agent(user_agent_opt)
            except Exception:
                _ua = str(user_agent_opt).strip() or None
            if _ua:
                crawl_ua = _ua
                session.headers['User-Agent'] = _ua
                # The Sec-Ch-Ua hints describe desktop Chrome; sending them
                # alongside a Googlebot/Firefox agent is self-contradictory and
                # is itself a bot signal, so drop them for non-Chrome agents.
                if 'Chrome/' not in _ua:
                    for _h in ('Sec-Ch-Ua', 'Sec-Ch-Ua-Mobile', 'Sec-Ch-Ua-Platform'):
                        session.headers.pop(_h, None)
                yield f"data: {json.dumps({'type': 'info', 'msg': f'Crawling as: {_ua[:70]}'})}\n\n"

        # Headed-browser fallback for bot challenges. Constructed eagerly but
        # launches lazily on the first challenge, so a normal crawl never pays
        # for it. Runs on its own virtual display — no window is ever shown.
        challenge_browser = None
        if solve_challenges:
            try:
                from challenge_browser import ChallengeBrowser
                challenge_browser = ChallengeBrowser(
                    user_agent=crawl_ua,
                    delay=max(crawl_delay, 2.0),
                )
            except Exception as e:
                current_app.logger.warning(f"[crawler] challenge browser unavailable: {e}")
                challenge_browser = None

        # Launch Playwright browser once per crawl if JS rendering requested
        pw_ctx = None
        pw_browser = None
        pw_page = None
        if render_js:
            try:
                from playwright.sync_api import sync_playwright
                pw_ctx = sync_playwright().start()
                pw_browser = pw_ctx.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                pw_page = pw_browser.new_page(
                    viewport={'width': 1280, 'height': 900},
                    user_agent=crawl_ua,
                )
                pw_page.set_default_timeout(20000)
                yield f"data: {json.dumps({'type': 'info', 'msg': 'JS rendering enabled (Playwright). Crawl will be 3-5x slower.'})}\n\n"
            except Exception as e:
                current_app.logger.warning(f"[crawler] Playwright init failed: {e}")
                yield f"data: {json.dumps({'type': 'info', 'msg': f'JS rendering unavailable ({str(e)[:100]}); using raw HTML only.'})}\n\n"
                pw_page = None

        if resumed_state:
            queue = deque(tuple(item) for item in resumed_state.get('queue', []))
            visited = set(resumed_state.get('visited', []))
            results = list(resumed_state.get('results', []))
            errors = int(resumed_state.get('errors', 0))
            total_time = float(resumed_state.get('total_time', 0))
            inlinks_map = dict(resumed_state.get('inlinks_map', {}))
            yield f"data: {json.dumps({'type': 'resumed', 'previous_pages': len(results), 'queued': len(queue), 'errors': errors})}\n\n"
        else:
            queue = deque()
            _seed_norm = _normalize_crawl_url(seed_url)
            queue.append((_seed_norm, 0))
            # Pre-mark the seed (and its slash-alt) as visited so any page that
            # links back to the homepage doesn't re-queue it. visited =
            # "URL has been seen / queued / processed".
            visited = {_seed_norm}
            _seed_alt = _crawl_slash_alt(_seed_norm)
            if _seed_alt:
                visited.add(_seed_alt)
            results = []
            errors = 0
            total_time = 0
            # Map of target URL -> list of source URLs linking to it (inlinks / Screaming Frog style)
            inlinks_map = {}

        tracked_kws = []  # left in place for payload compatibility; no external data sources in the public build
        yield f"data: {json.dumps({'type': 'start', 'domain': domain, 'workers': max_workers, 'crawl_id': crawl_id})}\n\n"

        # CMS fingerprint using the seed page (cheap HEAD+GET already handles this below,
        # but we want the info up-front so the UI can badge + offer one-click recommendations).
        try:
            cms_resp = session.get(seed_url, timeout=10, allow_redirects=True)
            cms_info = detect_cms(seed_url, cms_resp.text, dict(cms_resp.headers))
            if cms_info.get('cms'):
                prof = CMS_PROFILES.get(cms_info['cms'], {})
                cms_info['profile'] = {
                    'exclude_patterns': prof.get('exclude_patterns', []),
                    'suggested_settings': prof.get('suggested_settings', {}),
                    'schema_warnings': prof.get('schema_warnings', []),
                    'tips': prof.get('tips', []),
                }
            yield f"data: {json.dumps({'type': 'cms_detected', **cms_info})}\n\n"
        except Exception as _cms_err:
            current_app.logger.info(f"[crawler] CMS detection skipped: {_cms_err}")

        # Per-host politeness: minimum gap between two requests to the same host.
        # Workers on DIFFERENT hosts run freely; same-host workers serialise via this lock+timestamp map.
        from concurrent.futures import ThreadPoolExecutor, wait as _fwait, FIRST_COMPLETED
        from urllib.parse import urlparse as _up
        host_last_fetch = {}
        host_lock = threading.Lock()
        host_backoff = {}      # host → multiplier for adaptive slow-down on 429/403/503
        host_pause_until = {}  # host → epoch-seconds to halt ALL workers for this host

        def _wait_host_turn(u):
            # Reads crawl_delay live from ACTIVE_CRAWL_RULES so the slider in
            # the UI can change politeness mid-crawl. Falls back to the local
            # if the rules entry has been cleared (post-stop / cleanup).
            # Honours BOTH the normal per-host delay AND a hard pause window
            # set after a 429/403/503. The pause blocks every worker targeting
            # this host, not just the one that saw the error.
            host = _up(u).netloc
            while True:
                _live = (ACTIVE_CRAWL_RULES.get(crawl_id) or {}).get('crawl_delay')
                live_delay = float(_live) if _live is not None else crawl_delay
                with host_lock:
                    now = time.time()
                    last = host_last_fetch.get(host, 0)
                    pause_until = host_pause_until.get(host, 0)
                    backoff = host_backoff.get(host, 1.0)
                    effective_delay = live_delay * backoff
                    wait = max((last + effective_delay) - now, pause_until - now)
                    if wait <= 0:
                        host_last_fetch[host] = now
                        return
                time.sleep(min(wait, 5))  # wake periodically in case pause is extended

        def _adjust_host_backoff(u, page_data):
            host = _up(u).netloc
            status = page_data.get('status_code', 0)
            waf = page_data.get('_waf_block')  # 'wordfence'/'cloudflare'/'sucuri'/'siteground' or None
            with host_lock:
                cur = host_backoff.get(host, 1.0)
                if status in (429, 503):
                    # Hard slow-down on the first hit: jump straight to 10×.
                    host_backoff[host] = max(cur * 2, 10.0) if cur < 10.0 else min(cur * 2, 40.0)
                    hint = float(page_data.get('_retry_hint') or 0)
                    existing = host_pause_until.get(host, 0) - time.time()
                    if waf:
                        # WAF tripped — 30s won't help (Wordfence default block is 5-60min).
                        # Pause 5 min minimum and jump backoff to 20× so any resume crawls
                        # at a crawl.
                        pause_secs = max(hint, 300.0, existing)
                        host_backoff[host] = max(cur * 5, 20.0) if cur < 20.0 else min(cur * 2, 40.0)
                    else:
                        # Plain transient 503 (host briefly down, not WAF) — short pause.
                        pause_secs = max(hint, 30.0, existing)
                    host_pause_until[host] = time.time() + pause_secs
                elif status == 403:
                    # 403 = Cloudflare/Shopify bot block. Treat like 429 — pause
                    # the host so all workers stop hammering, and escalate backoff
                    # hard so when we resume we're an order of magnitude slower.
                    host_backoff[host] = max(cur * 3, 10.0) if cur < 10.0 else min(cur * 2, 30.0)
                    existing = host_pause_until.get(host, 0) - time.time()
                    if waf:
                        pause_secs = max(300.0, existing)
                        host_backoff[host] = max(cur * 5, 20.0) if cur < 20.0 else min(cur * 2, 40.0)
                    else:
                        pause_secs = max(60.0, existing)
                    host_pause_until[host] = time.time() + pause_secs
                elif status == 0 or page_data.get('error'):
                    # Transient connection/network error — bump backoff lightly,
                    # don't pause the host (other URLs may still work fine).
                    host_backoff[host] = min(cur * 1.5, 20.0)
                else:
                    # Decay back toward 1.0 on successes
                    if cur > 1.0:
                        host_backoff[host] = max(1.0, cur * 0.8)

        def _fetch_job(url, depth):
            """Worker: politeness-wait, fetch, return (url, depth, page_data)."""
            _wait_host_turn(url)
            pd = _crawl_page(url, session, domain, pw_page=pw_page, ignore_noindex=ignore_noindex, capture_no_js=compare_no_js, challenge_browser=challenge_browser)
            pd['depth'] = depth
            _adjust_host_backoff(url, pd)
            return url, depth, pd

        in_flight = {}  # future -> (url, depth)
        consecutive_errors = 0


        def _dequeue_next():
            """Pop the next URL that passes filters + robots. Returns (url, depth) or None.
            URLs are added to `visited` at enqueue time now (to prevent the same URL
            from being queued N times when N pages link to it), so we don't gate on
            visited here — it would skip every URL since they're all in visited."""
            while queue:
                url, depth = queue.popleft()
                if depth > max_depth:
                    continue
                if not _url_allowed(url):
                    continue
                if not ignore_robots:
                    try:
                        if not robots_can_fetch(url):
                            continue
                    except Exception:
                        pass
                return url, depth
            return None

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
              # Outer loop lets /crawl/continue bump the page cap and resume
              # without restarting the crawl from scratch.
              while True:
                # Prime the pool
                while len(in_flight) < max_workers and (len(results) + len(in_flight)) < _current_max():
                    nxt = _dequeue_next()
                    if nxt is None:
                        break
                    fut = executor.submit(_fetch_job, nxt[0], nxt[1])
                    in_flight[fut] = nxt

                while in_flight:
                    done_set, _ = _fwait(in_flight.keys(), return_when=FIRST_COMPLETED)
                    for fut in done_set:
                        submitted = in_flight.pop(fut, None)
                        try:
                            url, depth, page_data = fut.result()
                        except Exception as e:
                            url = submitted[0] if submitted else '?'
                            page_data = {
                                'url': url, 'status_code': 0, 'error': str(e)[:200],
                                'issues': [f'Fetch error: {str(e)[:80]}'], 'depth': submitted[1] if submitted else 0,
                            }

                        # Count adaptive signal for surfacing a speed notice
                        status = page_data.get('status_code', 0)
                        _waf_kind = page_data.get('_waf_block')
                        if _waf_kind:
                            # WAF block — surface it immediately, regardless of streak count.
                            # 5-min pause is already applied via _adjust_host_backoff.
                            _pause = max(0, host_pause_until.get(_up(url).netloc, 0) - time.time())
                            yield f"data: {json.dumps({'type': 'speed_adjusted', 'reason': f'{_waf_kind.title()} blocked us — host paused {int(_pause)}s, backoff escalated'})}\n\n"
                        if status in (429, 503, 403) or status == 0 or page_data.get('error'):
                            consecutive_errors += 1
                            if consecutive_errors in (3, 6, 12):
                                _reason_code = status if status else 'conn'
                                yield f"data: {json.dumps({'type': 'speed_adjusted', 'reason': f'HTTP {_reason_code} — per-host back-off active'})}\n\n"
                        else:
                            consecutive_errors = max(0, consecutive_errors - 1)

                        link_urls = page_data.get('internal_link_urls', [])

                        # Tracked-keyword enrichment
                        if tracked_kws:
                            page_url_clean = url.rstrip('/')
                            matching_kws = [k for k in tracked_kws if k.get('url','').rstrip('/') == page_url_clean]
                            if matching_kws:
                                page_data['tracked_keywords'] = matching_kws

                        # If a redirect happened, _crawl_page rewrote page_data['url']
                        # to the canonical final URL. Mark BOTH the requested URL and
                        # the canonical URL as visited so a later internal link to
                        # the canonical URL doesn't queue a duplicate row.
                        canonical_url = page_data.get('url')
                        if canonical_url and canonical_url != url:
                            visited.add(canonical_url)
                            _alt = _crawl_slash_alt(canonical_url)
                            if _alt:
                                visited.add(_alt)
                            if any(r.get('url') == canonical_url for r in results):
                                continue

                        results.append(page_data)
                        if page_data.get('error'):
                            errors += 1
                        total_time += page_data.get('response_time', 0)

                        # Enqueue discovered links + record inlinks
                        source_url = page_data.get('url') or url
                        for entry in link_urls:
                            if isinstance(entry, (list, tuple)):
                                link = entry[0]
                                anchor = entry[1] if len(entry) > 1 else ''
                                placement = entry[2] if len(entry) > 2 else ''
                            else:
                                link, anchor, placement = entry, '', ''
                            bucket = inlinks_map.setdefault(link, [])
                            key = (source_url, anchor, placement)
                            if not any((e.get('source'), e.get('anchor'), e.get('placement')) == key for e in bucket):
                                bucket.append({'source': source_url, 'anchor': anchor, 'placement': placement})
                            alt = _crawl_slash_alt(link)
                            if link not in visited and (not alt or alt not in visited) and _url_allowed(link):
                                # Mark on enqueue (not dequeue) so the same URL
                                # discovered from N pages doesn't get queued N
                                # times before it's pulled.
                                visited.add(link)
                                if alt:
                                    visited.add(alt)
                                queue.append((link, depth + 1))

                        # Hreflang discovery — region/language alternates are often
                        # NOT hyperlinked anywhere (JS-only country switchers), so
                        # without this whole region subtrees never get crawled and
                        # flood the sitemap "not crawled" report. Mirror Screaming
                        # Frog: enqueue same-host alternates as discovery-only URLs
                        # — deliberately NOT recorded in inlinks_map, so orphan
                        # reports still show pages that have no real inbound
                        # hyperlinks.
                        for _hl in page_data.get('hreflang') or []:
                            _href = (_hl.get('href') or '').split('#')[0]
                            if not _href.startswith(('http://', 'https://')):
                                continue
                            if _up(_href).netloc.lower().replace('www.', '') != domain:
                                continue
                            _halt = _crawl_slash_alt(_href)
                            if _href not in visited and (not _halt or _halt not in visited) and _url_allowed(_href):
                                visited.add(_href)
                                if _halt:
                                    visited.add(_halt)
                                queue.append((_href, depth + 1))

                        # Site-level robots.txt findings ride on the homepage row
                        # (first non-error page emitted) so they show up in the
                        # issues list/sidebar like every other red issue.
                        if robots_issues and not robots_attached and not page_data.get('error'):
                            page_data['issues'] = list(robots_issues) + (page_data.get('issues') or [])
                            robots_attached = True

                        from itertools import islice as _islice
                        queue_sample = [u for u, _d in _islice(queue, 100)]
                        yield f"data: {json.dumps({'type': 'page', 'data': page_data, 'crawled': len(results), 'queued': len(queue), 'errors': errors, 'queue_sample': queue_sample})}\n\n"

                    # Keep the pool topped up
                    while len(in_flight) < max_workers and (len(results) + len(in_flight)) < _current_max():
                        nxt = _dequeue_next()
                        if nxt is None:
                            break
                        fut2 = executor.submit(_fetch_job, nxt[0], nxt[1])
                        in_flight[fut2] = nxt

                # In-flight drained. Either queue is exhausted, the user asked
                # us to finalize, or we hit the page cap with URLs still queued.
                _state = ACTIVE_CRAWL_LIMITS.get(crawl_id) or {}
                if _state.get('finalize') or not queue:
                    break

                # Hit the cap with URLs still queued — surface a prompt and
                # wait for the user to either bump the cap or finalize.
                from itertools import islice as _islice
                queue_sample = [u for u, _d in _islice(queue, 100)]
                yield f"data: {json.dumps({'type': 'limit_reached', 'queued': len(queue), 'fetched': len(results), 'current_max': _current_max(), 'crawl_id': crawl_id, 'queue_sample': queue_sample})}\n\n"
                _ev = _state.get('continue_event')
                if _ev is None:
                    break
                while not _ev.wait(timeout=15):
                    yield ': waiting\n\n'  # SSE comment heartbeat
                _ev.clear()
                _state2 = ACTIVE_CRAWL_LIMITS.get(crawl_id) or {}
                if _state2.get('finalize'):
                    break
                yield f"data: {json.dumps({'type': 'crawl_resumed', 'new_max': _current_max(), 'queued': len(queue)})}\n\n"

        except GeneratorExit:
            # Re-enqueue any in-flight URLs so resume picks them up. URL stays
            # in `visited` (it's been seen) — dequeue doesn't gate on visited.
            for _fut, _submitted in list(in_flight.items()):
                if not _submitted:
                    continue
                _u, _d = _submitted
                queue.appendleft((_u, _d))
            # Prune anything older than TTL while we're here
            _now = time.time()
            for _cid in list(SUSPENDED_CRAWLS.keys()):
                if _now - SUSPENDED_CRAWLS[_cid].get('created', 0) > SUSPENDED_CRAWL_TTL:
                    SUSPENDED_CRAWLS.pop(_cid, None)
            # Persist state for resume
            SUSPENDED_CRAWLS[crawl_id] = {
                'created': _now,
                'seed_url': seed_url,
                'domain': domain,
                'queue': list(queue),
                'visited': list(visited),
                'results': results,
                'inlinks_map': inlinks_map,
                'errors': errors,
                'total_time': total_time,
                'config': {
                    'max_pages': max_pages,
                    'max_depth': max_depth,
                    'crawl_delay': crawl_delay,
                    'render_js': render_js,
                    'compare_no_js': compare_no_js,
                    'ignore_robots': ignore_robots,
                    'ignore_noindex': ignore_noindex,
                    'max_workers': max_workers,
                },
            }
            current_app.logger.info(f"[crawler] {crawl_id} suspended (resumable for {SUSPENDED_CRAWL_TTL//60}m): {len(results)} done, {len(queue)} queued")
        finally:
            executor.shutdown(wait=False)
            # The challenge browser owns an Xvfb display and a Chrome process.
            # Close it on every exit path — an unhandled error here would
            # otherwise leak both for the lifetime of the app. close() is
            # idempotent, so the explicit calls below stay harmless.
            if challenge_browser is not None:
                challenge_browser.close()

        session.close()
        _teardown_pw(pw_page, pw_browser, pw_ctx)

        # Summary
        avg_time = round(total_time / len(results), 2) if results else 0
        issue_counts = {}
        for r in results:
            for issue in r.get('issues', []):
                # Normalize issue name for counting
                base = issue.split('(')[0].strip()
                issue_counts[base] = issue_counts.get(base, 0) + 1

        summary = {
            'total': len(results),
            'errors': errors,
            'warnings': sum(1 for r in results if r.get('issues')),
            'avg_time': avg_time,
            'issue_counts': issue_counts,
            'js_rendered_count': sum(1 for r in results if r.get('js_rendered')),
            'render_js': render_js,
            'compare_no_js': compare_no_js,
            'js_diff_counts': {
                'critical': sum(1 for r in results if (r.get('js_diff') or {}).get('severity') == 'critical'),
                'high':     sum(1 for r in results if (r.get('js_diff') or {}).get('severity') == 'high'),
                'medium':   sum(1 for r in results if (r.get('js_diff') or {}).get('severity') == 'medium'),
                'none':     sum(1 for r in results if (r.get('js_diff') or {}).get('severity') == 'none'),
                'pages_with_diff': sum(1 for r in results if r.get('js_diff') and (r.get('js_diff') or {}).get('severity') != 'none'),
                'total_compared': sum(1 for r in results if r.get('js_diff')),
            },
        }

        # Attach inlinks per page (cap to 20 for payload size)
        inlinks_payload = {}
        for r in results:
            u = r.get('url')
            if not u:
                continue
            # A redirected row's url is the FINAL destination, but inbound links
            # were recorded against the originally-linked (redirecting) URL.
            # Emit under both keys so the Redirects view can show "pages linking
            # to the redirecting URL — update these" instead of an empty list.
            keys = [u]
            ou = r.get('original_url')
            if ou and ou != u:
                keys.append(ou)
            for k in keys:
                # Look up by exact URL and normalized variants
                sources = inlinks_map.get(k) or inlinks_map.get(k.rstrip('/')) or inlinks_map.get(k + '/') or []
                if sources:
                    inlinks_payload[k] = sources[:20]

        # ---------- Post-crawl aggregated reports ----------
        from collections import defaultdict as _dd

        # Duplicate titles / metas / H1s / body
        # Skip redirected URLs (http/https/www variants that 301 to the canonical)
        # and non-200 responses — those aren't unique content, just transit stops.
        # Dedupe within each group by normalised URL so pagination (/page/2/)
        # and tracking/ecommerce params (?add-to-cart=, ?utm_*, ?replytocom=)
        # don't fragment a single canonical page across its variants.
        def _group_by(field_getter):
            g = _dd(dict)  # value -> {normalised_url: original_url}
            for r in results:
                if not (r.get('indexable', True) or ignore_noindex):
                    continue
                if r.get('redirect_url'):
                    continue
                if r.get('status_code') and r['status_code'] >= 300:
                    continue
                # Skip canonicalised-elsewhere pages — rel=canonical already
                # declares them duplicates of another URL, so flagging here
                # is double-counting. Critical on Shopify where the same
                # product is reachable via /products/X and
                # /collections/Y/products/X (canonical points to bare URL).
                url = r.get('url') or ''
                canonical = (r.get('canonical') or '').strip()
                if canonical and _norm_url(canonical) != _norm_url(url):
                    continue
                v = field_getter(r)
                if not v:
                    continue
                norm = _normalize_url_for_dup(url)
                if norm not in g[v]:
                    g[v][norm] = url
            return {k: list(d.values()) for k, d in g.items() if len(d) > 1}

        dup_titles = _group_by(lambda r: (r.get('title') or '').strip().lower())
        dup_metas = _group_by(lambda r: (r.get('meta_description') or '').strip().lower())
        dup_h1s = _group_by(lambda r: (r.get('h1') or '').strip().lower())
        dup_bodies = _group_by(lambda r: r.get('body_hash') or '')

        # Response code summary
        rc_buckets = {'2xx': 0, '3xx': 0, '4xx': 0, '5xx': 0, 'other': 0}
        for r in results:
            sc = r.get('status_code', 0)
            if 200 <= sc < 300: rc_buckets['2xx'] += 1
            elif 300 <= sc < 400: rc_buckets['3xx'] += 1
            elif 400 <= sc < 500: rc_buckets['4xx'] += 1
            elif 500 <= sc < 600: rc_buckets['5xx'] += 1
            else: rc_buckets['other'] += 1

        # Redirect chains (2+ hops) — surfaced distinctly from single redirects
        redirect_chains = [
            {'url': r['url'], 'chain': r['redirect_chain'], 'hops': r['redirect_hops']}
            for r in results if r.get('redirect_hops', 0) >= 2
        ]

        # Orphan detection via sitemap (URLs in sitemap but not in crawl).
        # Handle sitemap index by recursively fetching sub-sitemaps (max 20 to bound cost).
        orphans = []
        sitemap_urls_set = set()
        import xml.etree.ElementTree as _ET

        def _fetch_sitemap(sm_url, depth=0, seen=None):
            if seen is None: seen = set()
            if depth > 2 or sm_url in seen or len(seen) > 20:
                return
            seen.add(sm_url)
            try:
                r = _http_get(sm_url, timeout=10, headers={'User-Agent': 'SEO-Audit-Bot'})
                if r.status_code != 200:
                    return
                root = _ET.fromstring(r.content)
                tag = root.tag.lower()
                if tag.endswith('sitemapindex'):
                    # Recurse into each sub-sitemap
                    for loc in root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc'):
                        if loc.text:
                            _fetch_sitemap(loc.text.strip(), depth + 1, seen)
                else:
                    # urlset — collect page URLs
                    for loc in root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc'):
                        if loc.text:
                            sitemap_urls_set.add(loc.text.strip().rstrip('/'))
            except Exception:
                pass

        _fetch_sitemap(f"{parsed.scheme}://{parsed.netloc}/sitemap.xml")
        crawled_urls_set = {r['url'].rstrip('/') for r in results}
        # Also include redirect targets (since they were actually reached)
        for r in results:
            if r.get('redirect_url'):
                crawled_urls_set.add(r['redirect_url'].rstrip('/'))
        orphans = sorted(sitemap_urls_set - crawled_urls_set)[:200]

        # Crawl depth distribution (depth tracking needs to be added in BFS queue,
        # stored on page_data via tuple queue; for now derive via shortest inlink path
        # approximation — homepage=0, pages linked from homepage=1, etc.)
        # Simpler: use what we have — each page's 'depth' is set by BFS via queue tuple.
        depth_dist = _dd(int)
        for r in results:
            depth_dist[r.get('depth', 0)] += 1

        # Soft-404 / infinite-URL-trap probe (2 requests, reuses crawl session)
        url_traps = _probe_url_traps(f"{parsed.scheme}://{parsed.netloc}", results, session)

        reports = {
            'url_traps': url_traps,
            'response_codes': rc_buckets,
            'duplicate_titles': [{'value': k, 'urls': v} for k, v in sorted(dup_titles.items(), key=lambda x: -len(x[1]))][:100],
            'duplicate_metas': [{'value': k, 'urls': v} for k, v in sorted(dup_metas.items(), key=lambda x: -len(x[1]))][:100],
            'duplicate_h1s': [{'value': k, 'urls': v} for k, v in sorted(dup_h1s.items(), key=lambda x: -len(x[1]))][:100],
            'duplicate_bodies': [{'value': k[:8], 'urls': v} for k, v in sorted(dup_bodies.items(), key=lambda x: -len(x[1]))][:100],
            'redirect_chains': redirect_chains[:200],
            'orphans': orphans,
            'sitemap_count': len(sitemap_urls_set),
            'depth_distribution': dict(depth_dist),
        }

        # If the crawl ended with just the start page (or nothing), tell the
        # user WHY instead of silently stopping. Most reports of "it only
        # crawled the homepage then stopped" are a failed seed fetch (SSL/
        # connection), a JS-rendered nav with no static links, or every link
        # filtered out by robots / URL rules.
        stop_reason = None
        if len(results) <= 1:
            seed_row = results[0] if results else None
            if not seed_row:
                stop_reason = 'No pages could be crawled — the start URL could not be fetched.'
            elif seed_row.get('error'):
                extra = '; '.join((seed_row.get('issues') or [])[:2])
                stop_reason = (f"Crawl stopped at the start page — it returned an error "
                               f"({seed_row.get('error')}). {extra}").strip()
            elif not (seed_row.get('internal_link_urls') or []):
                stop_reason = ("Crawl stopped at the start page — it loaded but no internal links were found. "
                               "If the site builds its navigation with JavaScript, turn on 'Render JS' and retry.")
            else:
                stop_reason = ("Crawl stopped at the start page — links were found but none were crawlable "
                               "(blocked by robots.txt, removed by your URL include/exclude filters, or pointing "
                               "to other domains). Adjust the filters or enable 'Ignore robots.txt' and retry.")

        current_app.logger.info(f"[crawler] Crawl complete: {len(results)} pages, {errors} errors, {avg_time}s avg, {len(dup_titles)} dup titles, {len(orphans)} orphans" + (f" | stop_reason: {stop_reason}" if stop_reason else ""))
        yield f"data: {json.dumps({'type': 'complete', 'total': len(results), 'summary': summary, 'inlinks': inlinks_payload, 'reports': reports, 'stop_reason': stop_reason})}\n\n"
        yield "data: [DONE]\n\n"
        ACTIVE_CRAWL_RULES.pop(crawl_id, None)
        ACTIVE_CRAWL_LIMITS.pop(crawl_id, None)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*'
        }
    )


