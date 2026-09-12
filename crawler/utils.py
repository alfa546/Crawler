from .proxy_manager import ProxyManager
PROXY_MGR = ProxyManager()
import requests
import re as _re
from urllib.parse import urlparse, urljoin, urlunparse, parse_qs, urlencode
import os
from .globals import _CRAWL_FOLDER, _CRAWL_FOLDERS_RO, _CRAWL_TITLE_HISTORY_PATH

def _http_get(url, **kwargs):
    proxy = PROXY_MGR.get_proxy()
    if proxy:
        kwargs['proxies'] = {'http': proxy, 'https': proxy}
    """Wrapper for requests.get that falls back to verify=False if SSLError occurs."""
    try:
        r = requests.get(url, **kwargs)
        r.ssl_bypassed = False
        return r
    except requests.exceptions.SSLError:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        kwargs['verify'] = False
        r = requests.get(url, **kwargs)
        r.ssl_bypassed = True
        return r

def _http_head(url, **kwargs):
    proxy = PROXY_MGR.get_proxy()
    if proxy:
        kwargs['proxies'] = {'http': proxy, 'https': proxy}
    """HEAD counterpart to _http_get with the same SSL-fallback behaviour."""
    try:
        r = requests.head(url, **kwargs)
        r.ssl_bypassed = False
        return r
    except requests.exceptions.SSLError:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        kwargs['verify'] = False
        r = requests.head(url, **kwargs)
        r.ssl_bypassed = True
        return r




def _robots_pattern_match(pattern, url):
    """robots.txt-style path pattern matcher (Google's spec).

    Wildcards: ``*`` matches any sequence, ``$`` at end anchors end of URL.
    Everything else is literal — including ``?``. Match is anchored at the
    start of the path; a leading ``*`` lets it match anywhere. Tests path+query
    first, then full URL so users can paste either form.
    """
    if not pattern:
        return False
    p = pattern.strip()
    if not p:
        return False
    end_anchor = p.endswith('$')
    if end_anchor:
        p = p[:-1]
    rx = '.*'.join(_re.escape(part) for part in p.split('*'))
    if end_anchor:
        rx += r'\Z'
    rx = '^' + rx
    try:
        prog = _re.compile(rx)
    except _re.error:
        return False
    parsed = urlparse(url)
    path_q = parsed.path + (('?' + parsed.query) if parsed.query else '')
    if prog.match(path_q):
        return True
    if prog.match(url):
        return True
    return False


def _build_robots_checker(robots_text):
    """Parse robots.txt and return a fn(url) -> bool that honours
    Google's wildcard spec (* and $) and longest-match-wins precedence.

    Python's stdlib urllib.robotparser does NOT support wildcards or end-
    of-URL anchors, so rules like 'Disallow: *?swoof*' or 'Disallow: *?*'
    silently turn into no-ops and the crawler picks up the dynamic URLs
    they were meant to keep out. Common on WooCommerce sites with the
    WOOF/Yoast filter combo. We use the existing _robots_pattern_match
    (which is already Google-spec) and apply longest-pattern-wins so a
    more specific Allow can lift a broader Disallow.

    Reads the User-agent: * block only - we crawl as a generic bot.
    """
    if not robots_text:
        return lambda u: True
    allows, disallows = [], []
    in_star_block = False
    for line in robots_text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line or ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip().lower()
        value = value.strip()
        if key == 'user-agent':
            in_star_block = (value == '*')
            continue
        if not in_star_block:
            continue
        if key == 'disallow' and value:
            disallows.append(value)
        elif key == 'allow' and value:
            allows.append(value)
    if not disallows and not allows:
        return lambda u: True

    def can_fetch(url):
        best_allow = -1
        best_disallow = -1
        for a in allows:
            if len(a) > best_allow and _robots_pattern_match(a, url):
                best_allow = len(a)
        for d in disallows:
            if len(d) > best_disallow and _robots_pattern_match(d, url):
                best_disallow = len(d)
        # Tie or longer Allow wins; only block when Disallow is strictly
        # more specific. This matches Google's robots.txt parser.
        return best_disallow <= best_allow

    return can_fetch


def _parse_robots_groups(robots_text):
    """Parse robots.txt into {lower_user_agent: {'disallow': [...], 'allow': [...]}}.

    Consecutive `User-agent:` lines share the rule block that follows them, per the
    robots.txt spec. Unlike _build_robots_checker (which only reads the `*` group to
    drive crawling), this keeps every named group so we can audit per-bot blocking.
    """
    groups = {}
    current = []
    starting_group = True
    for raw in (robots_text or '').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip().lower()
        value = value.strip()
        if key == 'user-agent':
            if not starting_group:
                current = []
                starting_group = True
            ua = value.lower()
            current.append(ua)
            groups.setdefault(ua, {'disallow': [], 'allow': []})
        elif key in ('disallow', 'allow'):
            starting_group = False
            for ua in current:
                groups[ua][key].append(value)
        else:
            starting_group = False
    return groups


def _robots_root_blocked(group):
    """True if the group disallows the site root ('/') with no Allow: / lifting it."""
    if not group:
        return False
    blocked = any(d.strip() in ('/', '/*') for d in group.get('disallow', []))
    if blocked and any(a.strip() == '/' for a in group.get('allow', [])):
        return False
    return blocked


def _analyze_robots_txt(robots_text):
    """Inspect robots.txt for crawler-blocking that hurts SEO / AI visibility.

    Returns a list of human-readable issue strings. Blocking answer-engine AI
    bots or search engines is a red error (see the sev() classifier); blocking
    training-only crawlers (CCBot, Bytespider etc.) is a deliberate policy
    choice and is reported as a separate, info-level issue string so it never
    trips the error badge.
    """
    issues = []
    if not robots_text:
        return issues
    groups = _parse_robots_groups(robots_text)
    star = groups.get('*')

    def is_blocked(ua):
        g = groups.get(ua.lower())
        if g is not None:
            return _robots_root_blocked(g)
        return _robots_root_blocked(star)  # no own group -> falls back to the * group

    # Cloudflare-style Content-Signal opt-out (e.g. "search=yes,ai-train=no").
    # ai-input=no cuts the site out of live AI answers (RAG/grounding) — answer
    # tier. ai-train=no only opts out of model training — training tier.
    input_signal = train_signal = False
    for raw in robots_text.splitlines():
        l = raw.split('#', 1)[0].strip().lower().replace(' ', '')
        if l.startswith('content-signal'):
            if 'ai-input=no' in l:
                input_signal = True
            if 'ai-train=no' in l:
                train_signal = True

    def blocked_names(ua_list):
        seen, names = set(), []
        for ua in ua_list:
            if is_blocked(ua) and ua.lower() not in seen:
                seen.add(ua.lower())
                names.append(ua)
        return names

    # Answer/citation AI bots — blocking these removes the site from AI answers.
    ai_names = blocked_names(_AI_CRAWLER_UAS)
    if ai_names or input_signal:
        if ai_names:
            shown = ai_names[:10]
            if len(ai_names) > 10:
                shown.append(f'+{len(ai_names) - 10} more')
            tail = ' (+ Content-Signal ai-input=no)' if input_signal else ''
            issues.append('AI crawlers blocked in robots.txt — ' + ', '.join(shown) + tail)
        else:
            issues.append('AI crawlers blocked in robots.txt — Content-Signal set to ai-input "no"')

    # Training-only crawlers — blocking these does not affect AI answer
    # visibility; usually deliberate. Info-level note, distinct issue string.
    train_names = blocked_names(_AI_TRAINING_UAS)
    if train_names or train_signal:
        if train_names:
            tail = ' (+ Content-Signal ai-train=no)' if train_signal else ''
            issues.append('AI training bots blocked in robots.txt — ' + ', '.join(train_names) + tail
                          + ' — training/data-collection only; AI answer visibility is not affected')
        else:
            issues.append('AI training bots blocked in robots.txt — Content-Signal ai-train=no'
                          + ' — training opt-out only; AI answer visibility is not affected')

    # Classic search engines.
    blocked_se = [ua for ua in _SEARCH_ENGINE_UAS if is_blocked(ua)]
    if blocked_se:
        issues.append('Search engines blocked in robots.txt — ' + ', '.join(blocked_se))
    elif _robots_root_blocked(star):
        issues.append('Search engines blocked in robots.txt — User-agent: * Disallow: /')

    return issues


def _normalize_url_for_dup(url):
    """Dedup key inside `_group_by`. Strips the entire query string + collapses
    pagination tails so filter/sort/search variants of the same page (e.g.
    /faq/ vs /faq/?category=planning) collapse to one entry. The grouper has
    already bucketed by identical meta/title/H1/body; URLs with truly distinct
    content sit in different buckets so this can't cause false collapses."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    # Collapse http vs https — the same URL on different schemes is the same
    # page, not a duplicate-title issue. (HTTP-only pages are surfaced
    # separately by the security report.)
    scheme = 'https'
    netloc = (parsed.netloc or '').lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    path = parsed.path or '/'
    path = _re.sub(r'/page/\d+/?$', '/', path)
    path = _re.sub(r'/comment-page-\d+/?$', '/', path)
    # Strip trailing whitespace / %20 / slashes in any combo so /foo,
    # /foo/, /foo%20, /foo%20/ all collapse. Common on Shopify when an
    # internal <a href> has a trailing space → encoded as %20.
    while path and path != '/':
        low = path.lower()
        if low.endswith('%20'):
            path = path[:-3]
        elif path[-1].isspace() or path[-1] == '/':
            path = path[:-1]
        else:
            break
    if not path:
        path = '/'
    return urlunparse((scheme, netloc, path, '', '', ''))


def _local_commit_sha():
    """Short SHA of the currently-installed build, or 'dev' if not a
    git checkout. Resolved per-call (not cached at startup) so devs
    don't get a stale 'update available' banner the moment they push
    a commit without restarting the dev server."""
    try:
        import subprocess as _sp
        out = _sp.run(['git', '-C', os.path.dirname(os.path.abspath(__file__)),
                       'rev-parse', '--short', 'HEAD'],
                      capture_output=True, text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return 'dev'


def _normalize_crawl_url(url):
    """Normalize URL for deduplication: strip fragments, utm/tracking/action
    params, lowercase host.

    Strips WooCommerce action endpoints (?add-to-cart=, ?wc-ajax= …) so
    every product page's "Add to cart" button doesn't surface as its own
    URL with no meta description.
    """
    from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
    parsed = urlparse(url)
    path = parsed.path or '/'
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        cleaned = {
            k: v for k, v in params.items()
            if not k.lower().startswith('utm_')
            and k.lower() not in _CRAWL_NOISE_PARAMS
        }
        query = urlencode(cleaned, doseq=True)
    else:
        query = ''
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, '', query, ''))


def _crawl_slash_alt(url):
    """Return the slash-toggled variant of a URL for dedup, or None if not applicable."""
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    path = p.path
    last_seg = path.rstrip('/').split('/')[-1]
    if '.' in last_seg:
        return None
    if path.endswith('/') and path != '/':
        alt_path = path.rstrip('/')
    else:
        alt_path = path + '/'
    return urlunparse((p.scheme, p.netloc, alt_path, p.params, p.query, p.fragment))


def _is_non_html_url(u):
    if not u:
        return False
    try:
        path = (urlparse(u).path or '').lower()
    except Exception:
        return False
    return path.endswith(_NON_HTML_EXTS)


def _norm_url(u):
    """Normalise a URL for set-comparison.

    Collapses http vs https, www vs non-www, and trailing slash so the
    sitemap-vs-crawl comparison doesn't flag http variants as "missing from
    sitemap" when the sitemap only lists https URLs. Also strips trailing
    whitespace / %20 — Shopify and other CMSs serve the same page at /foo
    and /foo%20 when an internal <a href> has a trailing space.
    """
    if not u:
        return ''
    u = u.strip()
    try:
        p = urlparse(u)
        host = (p.netloc or '').lower()
        if host.startswith('www.'):
            host = host[4:]
        path = p.path or ''
        while path:
            low = path.lower()
            if low.endswith('%20'):
                path = path[:-3]
            elif path[-1].isspace() or path[-1] == '/':
                path = path[:-1]
            else:
                break
        if not path:
            path = '/'
        return f"https://{host}{path if path != '/' else ''}".lower()
    except Exception:
        return u.rstrip('/').lower()


def _bare_host(url):
    """Normalise a URL/domain to a bare host for matching: strip scheme,
    www., path and trailing slash."""
    h = (url or '').lower().strip()
    h = h.replace('https://', '').replace('http://', '').rstrip('/')
    h = h[4:] if h.startswith('www.') else h
    return h.split('/')[0]


def _same_host_sitemap_urls(sm_urls, domain):
    """Keep only sitemap entries on the same host as the analysed domain.

    robots.txt on multisite setups frequently points at a *sibling* subdomain's
    sitemap (e.g. shop.example.com → blog.example.com). Those
    URLs belong to a different site and must NOT be diffed against this crawl —
    otherwise every uncrawled foreign URL floods the "in sitemap, not crawled"
    orphan report. Returns (kept_entries, foreign_host_counts) so the caller can
    tell the user exactly what was excluded instead of silently dropping it.
    """
    base = _bare_host(domain)
    if not base:
        return list(sm_urls), {}
    kept, foreign = [], {}
    for entry in sm_urls:
        h = _bare_host(entry.get('url') or '')
        if h and h != base:
            foreign[h] = foreign.get(h, 0) + 1
        else:
            kept.append(entry)
    return kept, foreign


def _nd_tokenize(text):
    return _re.findall(r"[a-z][a-z'\-]{1,}", (text or '').lower())


def _nd_strip_selectors(html_or_text, selectors):
    if not selectors or not html_or_text:
        return html_or_text
    sel_list = [s.strip() for s in selectors.split(',') if s.strip()]
    if not sel_list or '<' not in html_or_text or '>' not in html_or_text:
        return html_or_text
    try:
        soup = BeautifulSoup(html_or_text, 'html.parser')
        for sel in sel_list:
            for node in soup.select(sel):
                node.decompose()
        return soup.get_text(separator=' ', strip=True)
    except Exception:
        return html_or_text



def _foreign_host_warning(foreign_hosts, domain):
    if not foreign_hosts:
        return None
    total = sum(foreign_hosts.values())
    detail = ', '.join(f"{h} ({n})" for h, n in sorted(foreign_hosts.items(), key=lambda x: -x[1]))
    return (f"Excluded {total} sitemap URL(s) on a different host — {detail} — from the "
            f"orphan / coverage diff. They belong to another site, not {_bare_host(domain)}, "
            f"so they are not orphans of this crawl.")



def _cb_normalise_key(k):
    """Collapse page-builder widget-id suffixes so e-page-0b1537f and
    e-page-1478160 both group as e-page-* (Elementor uses <name>-<hex>)."""
    m = _re.match(r'^([a-z][a-z0-9]*-[a-z0-9]+)-[0-9a-f]{6,}$', k)
    return (m.group(1) + '-*') if m else k


def _cb_classify(norm_key):
    base = norm_key[:-1] if norm_key.endswith('-*') else norm_key  # e-page-* -> e-page-
    for matcher, typ, why in _CB_PARAM_RULES:
        try:
            if matcher(base) or matcher(norm_key):
                return typ, why
        except Exception:
            continue
    return None, None


def _cb_fetch_sitemap_urls(base_url, ua):
    """Self-contained sitemap fetch (handles sitemap index + robots Sitemap:)."""
    import html as _html2
    urls, seen = [], set()

    def parse(sm, depth=0):
        if depth > 5 or sm in seen:
            return
        seen.add(sm)
        try:
            r = requests.get(sm, headers=ua, timeout=15)
            if r.status_code != 200:
                return
            c = r.text
            if '<sitemapindex' in c:
                for m in _re.findall(r'<loc>\s*(.*?)\s*</loc>', c):
                    parse(_html2.unescape(m.strip()), depth + 1)
            else:
                for m in _re.findall(r'<loc>\s*(.*?)\s*</loc>', c):
                    u = _html2.unescape(m.strip())
                    if u:
                        urls.append(u)
        except Exception:
            pass

    hp = base_url.rstrip('/')
    robots_sitemaps = []
    try:
        r = requests.get(hp + '/robots.txt', headers=ua, timeout=10)
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.strip().lower().startswith('sitemap:'):
                    robots_sitemaps.append(line.split(':', 1)[1].strip())
    except Exception:
        pass
    for sm in robots_sitemaps:
        parse(sm)
    if not urls:
        parse(hp + '/sitemap_index.xml')
    if not urls:
        parse(hp + '/sitemap.xml')
    if not urls:
        parse(hp + '/wp-sitemap.xml')
    return urls


def _teardown_pw(pw_page, pw_browser, pw_ctx):
    for name, obj, method in (('page', pw_page, 'close'), ('browser', pw_browser, 'close'), ('pw', pw_ctx, 'stop')):
        if obj is not None:
            try: getattr(obj, method)()
            except Exception: pass


def _discover_sitemaps(domain):
    """Find sitemap URLs for a domain. Tries robots.txt first, then common
    default paths. When robots.txt points at a sibling subdomain (multisite
    misconfiguration) we surface a warning AND also probe the analysed
    domain's own paths so the diff isn't comparing the crawl against the
    wrong site's URL set.
    """
    found = []
    seen = set()
    warnings = []

    def _add(u, src):
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            found.append({'url': u, 'source': src})

    analysed_host = (urlparse(domain).netloc or '').lower()

    try:
        r = _http_get(f"{domain.rstrip('/')}/robots.txt", timeout=10,
                         headers={'User-Agent': 'Mozilla/5.0'})
        if r.ok and r.text:
            for line in r.text.splitlines():
                line = line.strip()
                if line.lower().startswith('sitemap:'):
                    sm_url = line.split(':', 1)[1].strip()
                    sm_host = (urlparse(sm_url).netloc or '').lower()
                    src = 'robots.txt'
                    if sm_host and analysed_host and sm_host != analysed_host:
                        src = 'robots.txt (DIFFERENT DOMAIN)'
                        warnings.append(
                            f"robots.txt declares sitemap on a different host ({sm_host}) "
                            f"than the site being analysed ({analysed_host}). "
                            f"Likely multisite misconfiguration — also probing default paths."
                        )
                    _add(sm_url, src)
    except Exception:
        pass

    has_onsite = any((urlparse(s['url']).netloc or '').lower() == analysed_host for s in found)
    if not has_onsite:
        for path in _SITEMAP_DEFAULT_PATHS:
            url = f"{domain.rstrip('/')}{path}"
            try:
                resp = _http_head(url, timeout=8, allow_redirects=True,
                                     headers={'User-Agent': 'Mozilla/5.0'})
                if resp.status_code == 405:
                    resp = _http_get(url, timeout=10, allow_redirects=True,
                                        headers={'User-Agent': 'Mozilla/5.0'},
                                        stream=True)
                    resp.close()
                if resp.ok:
                    ct = (resp.headers.get('content-type') or '').lower()
                    if 'xml' in ct or path.endswith('.xml') or path.endswith('.gz'):
                        _add(url, 'default-path')
                        break
            except Exception:
                pass

    return found, warnings


def _fetch_sitemap_recursive(seed_urls, max_depth=5):
    """Walk a sitemap (handling sitemap-index recursion) and collect every URL."""
    import xml.etree.ElementTree as ET
    import gzip

    urls = []
    sitemaps_meta = []
    errors = []
    visited = set()

    def _walk(sm_url, depth):
        if depth > max_depth or sm_url in visited:
            return
        visited.add(sm_url)
        try:
            r = _http_get(sm_url, timeout=20,
                             headers={'User-Agent': 'Mozilla/5.0',
                                      'Accept': 'application/xml,text/xml,*/*'})
            if not r.ok:
                errors.append({'sitemap': sm_url, 'error': f'http_{r.status_code}'})
                sitemaps_meta.append({'url': sm_url, 'url_count': 0, 'error': f'http_{r.status_code}'})
                return
            content = r.content
            if sm_url.lower().endswith('.gz'):
                try:
                    content = gzip.decompress(content)
                except Exception:
                    pass
            try:
                root = ET.fromstring(content)
            except ET.ParseError as e:
                errors.append({'sitemap': sm_url, 'error': f'xml_parse: {str(e)[:120]}'})
                sitemaps_meta.append({'url': sm_url, 'url_count': 0, 'error': 'xml_parse'})
                return

            tag = root.tag.split('}', 1)[-1] if '}' in root.tag else root.tag
            count_here = 0
            if tag == 'sitemapindex':
                for sm_node in root.findall(f'{_SITEMAP_NS}sitemap'):
                    loc = sm_node.find(f'{_SITEMAP_NS}loc')
                    if loc is not None and loc.text:
                        _walk(loc.text.strip(), depth + 1)
                sitemaps_meta.append({'url': sm_url, 'url_count': 0, 'error': None,
                                      'is_index': True})
            else:
                for url_node in root.findall(f'{_SITEMAP_NS}url'):
                    loc = url_node.find(f'{_SITEMAP_NS}loc')
                    if loc is None or not loc.text:
                        continue
                    lastmod = url_node.find(f'{_SITEMAP_NS}lastmod')
                    urls.append({
                        'url': loc.text.strip(),
                        'lastmod': lastmod.text.strip() if lastmod is not None and lastmod.text else None,
                        'source_sitemap': sm_url,
                    })
                    count_here += 1
                sitemaps_meta.append({'url': sm_url, 'url_count': count_here, 'error': None,
                                      'is_index': False})
        except Exception as e:
            errors.append({'sitemap': sm_url, 'error': str(e)[:200]})
            sitemaps_meta.append({'url': sm_url, 'url_count': 0, 'error': str(e)[:120]})

    for u in seed_urls:
        _walk(u, 0)
    return urls, sitemaps_meta, errors


def _append_crawl_title_history(name, saved_at, results):
    """Append one line per crawled page capturing its title + meta description.
    Best-effort: never raises into the caller."""
    try:
        os.makedirs(_CRAWL_FOLDER, exist_ok=True)
        from datetime import datetime as _dt3
        ts = _dt3.utcfromtimestamp(saved_at).isoformat(timespec='seconds') + 'Z'
        seed = (results[0].get('url') if results else '') or ''
        lines = []
        for p in results:
            if not isinstance(p, dict) or not p.get('url'):
                continue
            lines.append(json.dumps({
                'ts': ts,
                'crawl': name,
                'seed': seed,
                'url': p.get('url'),
                'title': p.get('title') or '',
                'meta_description': p.get('meta_description') or '',
            }, ensure_ascii=False))
        if lines:
            with open(_CRAWL_TITLE_HISTORY_PATH, 'a') as f:
                f.write('\n'.join(lines) + '\n')
    except Exception as e:
        app.logger.warning(f'[crawl-titles] failed to append title history: {e}')


def _all_crawl_folders():
    """Folders to scan for /crawl/list and /crawl/load. Own dir first
    so collisions on filename prefer our own copy."""
    return [_CRAWL_FOLDER] + _CRAWL_FOLDERS_RO


def _ip_name_map():
    """Optional IP → friendly name map (JSON dict) so a shared office
    instance can label saved crawls with people's names instead of IPs.
    Falls back to empty dict if the file is missing/unreadable."""
    try:
        path = os.environ.get('SITE_CRAWLER_USER_MAP',
                              os.path.expanduser('~/.site-crawler-users.json'))
        with open(path) as f:
            m = json.load(f)
            return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _find_crawl_path(fn):
    """Locate a saved-crawl file in any of our read folders."""
    for folder in _all_crawl_folders():
        p = os.path.join(folder, fn)
        if os.path.exists(p): return p
    return None




__all__ = [name for name in dir() if not name.startswith('__') and name not in ['requests', 're', 'os', 'BeautifulSoup', 'urlparse', 'urljoin', 'urlunparse', 'parse_qs', 'urlencode', 'PROXY_MGR', 'ProxyManager']]

_NON_HTML_EXTS = (
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp', '.tiff', '.avif',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.csv', '.txt', '.rtf',
    '.zip', '.tar', '.gz', '.7z', '.rar',
    '.mp4', '.mov', '.webm', '.m4v', '.avi', '.mkv', '.mp3', '.wav', '.ogg', '.flac',
    '.css', '.js', '.json', '.xml', '.map',
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
)
__all__.append('_NON_HTML_EXTS')

# ---- Module-level constants below (recovered from temp_app.py) ----
# These were referenced by utils.py / engine.py / routes.py but never defined
# in the crawler package after the temp_app.py refactor, causing
#     NameError: name '_...' is not defined
# mid-crawl (the "Crawl stopped at the start page" error). They are defined
# AFTER __all__ above, so each is appended to __all__ explicitly for the
# `from .utils import *` consumers (engine.py, routes.py).

_AI_CRAWLER_UAS = [
    'GPTBot', 'OAI-SearchBot', 'ChatGPT-User',            # OpenAI
    'ClaudeBot', 'Claude-Web', 'Claude-SearchBot',         # Anthropic
    'Claude-User', 'anthropic-ai',
    'Google-Extended',                                     # Google AI / Gemini
    'PerplexityBot', 'Perplexity-User',                    # Perplexity
    'Amazonbot',                                           # Amazon / Alexa
    'Meta-ExternalAgent', 'meta-externalagent',            # Meta AI
    'MistralAI-User',                                      # Mistral / Le Chat
    'DuckAssistBot', 'YouBot',                             # DuckDuckGo / You.com
]

_AI_TRAINING_UAS = [
    'CCBot',                                               # Common Crawl (training corpora)
    'Bytespider',                                          # ByteDance scraper
    'Applebot-Extended',                                   # Apple training opt-out token
    'cohere-ai', 'Diffbot', 'AI2Bot',
    'Timpibot', 'ImagesiftBot', 'Omgilibot',
]

_SEARCH_ENGINE_UAS = ['Googlebot', 'Bingbot', 'Slurp', 'DuckDuckBot', 'Baiduspider', 'YandexBot']

_CB_PARAM_RULES = [
    (lambda k: k.startswith('e-page-'),            'pagination', 'Elementor Pro Posts/Loop AJAX pagination'),
    (lambda k: k.startswith('e-filter-'),          'faceting',   'Elementor Pro taxonomy filter'),
    (lambda k: k in ('page', 'paged', 'pg', 'pagenum', 'start', 'offset'), 'pagination', 'Pagination parameter'),
    (lambda k: k in ('orderby', 'order', 'sort', 'sort_by', 'sortby'),     'sort',       'Result sorting — duplicate views of the same set'),
    (lambda k: k in ('filter', 'filters', 'filter_by') or k.startswith('filter_') or k.endswith('_filter') or k.startswith('pa_') or k in ('color', 'colour', 'size', 'brand', 'min_price', 'max_price', 'swoof', 'jsf'), 'faceting', 'Faceted navigation filter'),
    (lambda k: k.startswith('utm_') or k in ('gclid', 'fbclid', 'msclkid', 'mc_cid', 'mc_eid', 'yclid'), 'tracking', 'Campaign / click tracking tag'),
    (lambda k: k in ('replytocom', 'phpsessid', 'sessionid', 'sid', 'jsessionid'), 'session', 'Session / comment-reply parameter'),
    (lambda k: k in ('s', 'q', 'search', 'query', 'keyword'), 'search', 'Internal site-search query'),
]

_CRAWL_NOISE_PARAMS = frozenset({
    # Tracking
    'fbclid', 'gclid', 'mc_cid', 'mc_eid', 'gad_source', 'gbraid', 'wbraid',
    'msclkid', 'yclid', 'dclid', 'igshid', 'srsltid',
    'ref', 'ref_src', 'ref_url',
    # WooCommerce action endpoints — not real pages.
    'add-to-cart', 'remove_item', 'removed_item', 'undo_item',
    'wc-ajax', 'wc-api', 'wcml_currency', 'orderby', 'product-page',
    'min_price', 'max_price',
    # Other common ecommerce/forum noise
    'replytocom', 'unapproved', 'moderation-hash',
    'share', 'sharesource',
})

_MAILTO_NO_SCHEME_RE = _re.compile(r'^[^/\s:?#]+@[^/\s:?#]+\.[A-Za-z]{2,}$')

_HREF_SCHEME_RE = _re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*:')

_SITEMAP_DEFAULT_PATHS = (
    '/sitemap.xml', '/sitemap_index.xml', '/sitemap-index.xml',
    '/wp-sitemap.xml',
    '/sitemap.xml.gz',
    '/sitemap1.xml', '/sitemap-1.xml',
    '/post-sitemap.xml', '/page-sitemap.xml',
)

_SITEMAP_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'

for _name in ('_AI_CRAWLER_UAS', '_AI_TRAINING_UAS', '_SEARCH_ENGINE_UAS',
              '_CB_PARAM_RULES', '_CRAWL_NOISE_PARAMS', '_MAILTO_NO_SCHEME_RE',
              '_HREF_SCHEME_RE', '_SITEMAP_DEFAULT_PATHS', '_SITEMAP_NS'):
    __all__.append(_name)
