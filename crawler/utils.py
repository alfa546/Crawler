from .proxy_manager import ProxyManager
PROXY_MGR = ProxyManager()
import requests
import re
from urllib.parse import urlparse, urljoin, urlunparse, parse_qs, urlencode
import os

def _http_get(url, **kwargs):
    proxy = PROXY_MGR.get_proxy()
    if proxy:
        kwargs['proxies'] = {'http': proxy, 'https': proxy}
    """requests.get that transparently retries with verify=False on an SSL
    cert-chain failure (incomplete chain / untrusted or self-signed cert).
    Lots of sites load fine in browsers but serve a broken chain; without this,
    robots.txt and sitemap discovery silently fail and the user sees
    "no sitemap found". The returned Response carries .ssl_bypassed."""
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


