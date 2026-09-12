STATIC_VERSION = '2.0'
from flask import Blueprint, render_template, request, Response, stream_with_context, jsonify, send_file, current_app
import json, time, os, re, logging, threading
from .utils import *
from .seo_analyzer import *
from .export_utils import *
from .engine import crawl_site

from . import crawler_bp

from .globals import ACTIVE_CRAWL_RULES, ACTIVE_CRAWL_LIMITS, SUSPENDED_CRAWLS, SUSPENDED_CRAWL_TTL, _CRAWL_FOLDER, _CRAWL_FOLDERS_RO, _CRAWL_TITLE_HISTORY_PATH

_ND_STOP = {
    'a', 'an', 'the', 'and', 'or', 'but', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'should', 'could', 'can',
    'to', 'of', 'in', 'on', 'at', 'by', 'for', 'with', 'about', 'as', 'into', 'through',
    'this', 'that', 'these', 'those', 'i', 'we', 'you', 'they', 'it', 'he', 'she',
    'our', 'your', 'their', 'its', 'his', 'her', 'my',
    'not', 'no', 'so', 'if', 'than', 'then', 'too', 'very', 'just',
    'over', 'under', 'before', 'after', 'between', 'from', 'up', 'down', 'out', 'off',
    'all', 'any', 'each', 'most', 'some', 'other', 'such', 'only', 'own', 'same',
    'us', 'me', 'them', 'who', 'what', 'where', 'when', 'why', 'how',
}

@crawler_bp.route('/crawl-budget/analyze', methods=['POST'])
def crawl_budget_analyze():
    """On-demand crawl-budget scan for the Crawl Budget view."""
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    try:
        return jsonify(_analyze_crawl_budget(url))
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500


@crawler_bp.route('/version')
def version():
    """Local build SHA, served to the UI for display + comparison
    against the GitHub main HEAD (client-side fetch)."""
    return jsonify({
        'sha': _local_commit_sha(),
        'repo': 'alfa546/Crawler',
    })


@crawler_bp.route('/update', methods=['POST'])
def update_self():
    """Reconcile the local checkout to origin/main and report the result.
    Restart is handled separately (POST /restart) so the UI can fire it
    immediately after.

    Windows-safe by design — this is the path that was bricking installs:
      * Every git call runs with `-c gc.auto=0` so git never repacks
        objects mid-update (the usual cause of "Unlink of file failed:
        .git/objects/..." when the app process is still running), plus
        GIT_OPTIONAL_LOCKS=0 to stop background index refreshes grabbing
        locks the running process holds.
      * It no longer refuses when tracked files look "modified" — on
        Windows a CRLF checkout makes git report every file as changed,
        which used to abort the update outright.
      * When a clean fast-forward isn't possible (CRLF-dirtied tree, a
        prior half-applied pull, or diverging history) it hard-resets to
        origin/main. That's the same self-healing behaviour the
        installer's Autostart.ps1 / Update.ps1 / recover-windows.ps1 use,
        so a broken checkout repairs itself on the next update or reboot.
    Untracked files (crawl data, logs, the venv) survive the reset."""
    import subprocess as _sp
    repo = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(os.path.join(repo, '.git')):
        return jsonify({'ok': False, 'error': 'Not a git checkout — install via git clone to use auto-update.'}), 400

    _env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    def _git(*args, timeout=60):
        return _sp.run(['git', '-C', repo, '-c', 'gc.auto=0', *args],
                       capture_output=True, text=True, timeout=timeout, env=_env)

    def _req_hash():
        """SHA1 of requirements.txt so we can tell if deps changed and need a
        reinstall after the pull — an update that adds a dependency would
        otherwise crash the app on restart and look like a brick."""
        try:
            import hashlib
            with open(os.path.join(repo, 'requirements.txt'), 'rb') as f:
                return hashlib.sha1(f.read()).hexdigest()
        except Exception:
            return ''

    before = _local_commit_sha()
    before_req = _req_hash()
    try:
        fetch = _git('fetch', '--quiet', 'origin', 'main')
        if fetch.returncode != 0:
            return jsonify({'ok': False, 'error': ('git fetch failed: ' + (fetch.stderr or fetch.stdout).strip())[:400]}), 500

        # Prefer a clean fast-forward so untouched installs aren't reset; fall
        # back to a hard reset only when that's impossible (the broken-Windows
        # case). Either way the tree ends up exactly on origin/main.
        ff = _git('merge', '--ff-only', 'origin/main')
        if ff.returncode != 0:
            reset = _git('reset', '--hard', 'origin/main')
            if reset.returncode != 0:
                return jsonify({'ok': False, 'error': ('git reset failed: ' + (reset.stderr or reset.stdout).strip())[:400]}), 500
            msg = 'Hard-reset to origin/main (fast-forward was not possible).'
        else:
            msg = (ff.stdout.strip() or 'Fast-forwarded to origin/main.')

        # Reinstall deps if requirements.txt changed, using this venv's pip,
        # before the restart picks up the new code.
        if _req_hash() != before_req:
            if os.name == 'nt':
                pip = os.path.join(repo, 'venv', 'Scripts', 'pip.exe')
            else:
                pip = os.path.join(repo, 'venv', 'bin', 'pip')
            if os.path.exists(pip):
                dep = _sp.run([pip, 'install', '-r', os.path.join(repo, 'requirements.txt')],
                              capture_output=True, text=True, timeout=300)
                msg += ' Dependencies updated.' if dep.returncode == 0 \
                    else ' WARNING: dependency reinstall failed — restart may fail.'
            else:
                msg += ' (requirements changed but venv pip not found — reinstall manually.)'

        after = _local_commit_sha()
        return jsonify({
            'ok': True,
            'before': before,
            'after': after,
            'changed': before != after,
            'message': msg[:400],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300]}), 500


@crawler_bp.route('/restart', methods=['POST'])
def restart_self():
    """Detached restart of the running Flask process. Platform split:
    POSIX uses bash + nohup; Windows uses cmd.exe + start to launch a
    detached pythonw.exe so the new process survives this one's death."""
    import subprocess as _sp
    import sys as _sys
    repo = os.path.dirname(os.path.abspath(__file__))
    own_pid = os.getpid()

    if _sys.platform.startswith('win'):
        # Spawn a detached PowerShell with CREATE_NO_WINDOW + null handles.
        # This is the ONLY launch method that reliably starts from the
        # no-console pythonw process: DETACHED_PROCESS and every cmd.exe/`start`
        # variant silently no-op here (and `start` in a console-less context is
        # what threw the "Windows cannot find '\\'" shell error). The child
        # survives us being killed — Windows doesn't cascade-kill children.
        CREATE_NO_WINDOW = 0x08000000
        pyw = os.path.join(repo, 'venv', 'Scripts', 'pythonw.exe')
        if not os.path.exists(pyw):
            pyw = _sys.executable
        app_py = os.path.join(repo, 'app.py')
        helper = os.path.join(repo, 'restart-helper.ps1')

        if os.path.exists(helper):
            # restart-helper.ps1 kills us, waits for the port to free, then
            # starts + VERIFIES the app, retrying instead of bricking.
            cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                   '-WindowStyle', 'Hidden', '-File', helper,
                   '-OldPid', str(own_pid), '-Port', '5002']
        else:
            # Fallback (pre-helper checkout): inline PowerShell, still no `start`.
            ps = (
                "Start-Sleep -Seconds 1; "
                "try {{ Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue }} catch {{}}; "
                "Start-Sleep -Seconds 2; "
                "Start-Process -FilePath '{pyw}' -ArgumentList '\"{app}\"' "
                "-WorkingDirectory '{repo}' -WindowStyle Hidden"
            ).format(pid=own_pid, pyw=pyw, app=app_py, repo=repo)
            cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                   '-WindowStyle', 'Hidden', '-Command', ps]
        try:
            _sp.Popen(cmd, creationflags=CREATE_NO_WINDOW,
                      stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)[:300]}), 500
        return jsonify({'ok': True, 'message': 'Restarting in ~3s'})

    # POSIX
    log_path = os.path.expanduser('~/site-crawler/.restart.log')
    cmd = (
        f"sleep 1 && "
        f"kill {own_pid} 2>/dev/null; sleep 1; "
        f"cd {repo} && nohup python3 app.py >/dev/null 2>&1 &"
    )
    try:
        _sp.Popen(['bash', '-c', cmd], start_new_session=True,
                  stdout=open(log_path, 'a'), stderr=_sp.STDOUT)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300]}), 500
    return jsonify({'ok': True, 'message': 'Restarting in ~2s'})


@crawler_bp.route('/')
def index():
    return render_template('index.html', v=STATIC_VERSION, build_sha=_local_commit_sha())


@crawler_bp.route('/detect-cms', methods=['POST'])
def detect_cms_route():
    """Fetch a URL and identify the CMS. Used standalone and also at crawl start."""
    data = request.json or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    try:
        resp = _http_get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36'
        })
        result = detect_cms(url, resp.text, dict(resp.headers))
        if result.get('cms'):
            prof = CMS_PROFILES.get(result['cms'], {})
            result['profile'] = {
                'exclude_patterns': prof.get('exclude_patterns', []),
                'suggested_settings': prof.get('suggested_settings', {}),
                'schema_warnings': prof.get('schema_warnings', []),
                'tips': prof.get('tips', []),
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'Detection failed: {str(e)[:100]}'}), 500


@crawler_bp.route('/fetch-robots-txt', methods=['GET'])
def fetch_robots_txt():
    """Fetch a site's robots.txt for the URL filters preview panel."""
    target = (request.args.get('url') or '').strip()
    if not target:
        return jsonify({'error': 'url required'}), 400
    if not target.startswith('http'):
        target = 'https://' + target.lstrip('/')
    try:
        parsed = urlparse(target)
        if not parsed.netloc:
            return jsonify({'error': 'invalid URL'}), 400
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        resp = _http_get(robots_url, timeout=10,
                            headers={'User-Agent': 'Mozilla/5.0 (compatible; Crawler-RobotsPreview)'},
                            allow_redirects=True)
        body = (resp.text or '')[:20000]
        return jsonify({
            'url': robots_url,
            'status': resp.status_code,
            'content': body,
            'length': len(resp.text or ''),
        })
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 200


@crawler_bp.route('/sitemap-analyse', methods=['POST'])
def sitemap_analyse():
    """Discover the site's sitemap(s) and diff against a crawl.

    Body: {"domain": "https://example.com", "results": [...page rows...],
           "inlinks": {url: [source_urls...]}}
    """
    data = request.get_json() or {}
    domain = (data.get('domain') or '').rstrip('/')
    if domain and not domain.startswith('http'):
        domain = 'https://' + domain
    results = data.get('results') or []
    inlinks_map = data.get('inlinks') or {}
    manual_sm = (data.get('sitemap_url') or '').strip()

    if not domain:
        return jsonify({'error': 'domain required'}), 400

    if manual_sm:
        if not manual_sm.startswith('http'):
            manual_sm = 'https://' + manual_sm.lstrip('/')
        discovered = [{'url': manual_sm, 'source': 'manual'}]
        discovery_warnings = []
    else:
        discovered, discovery_warnings = _discover_sitemaps(domain)
        if not discovered:
            return jsonify({
                'sitemaps_found': [],
                'warnings': discovery_warnings + ['No sitemap could be discovered. Tried robots.txt and common default paths.'],
                'tried_paths': list(_SITEMAP_DEFAULT_PATHS),
            }), 200

    seed = [d['url'] for d in discovered]
    sm_urls, sitemaps_meta, sm_errors = _fetch_sitemap_recursive(seed)

    # Drop URLs on a different host than the analysed site (multisite robots.txt
    # pointing at a sibling subdomain). They're a different site, not orphans
    # of this crawl.
    sm_urls, _foreign_hosts = _same_host_sitemap_urls(sm_urls, domain)
    _fh_warning = _foreign_host_warning(_foreign_hosts, domain)
    if _fh_warning:
        discovery_warnings = list(discovery_warnings) + [_fh_warning]

    crawl_by_norm = {}
    for r in results:
        u = r.get('url')
        if not u:
            continue
        crawl_by_norm[_norm_url(u)] = r

    sitemap_by_norm = {}
    for entry in sm_urls:
        sitemap_by_norm.setdefault(_norm_url(entry['url']), entry)

    inlinks_by_norm = {}
    for k, v in inlinks_map.items():
        inlinks_by_norm[_norm_url(k)] = v or []

    pag_re = _re.compile(r'/page/\d+/?$|[?&](page|paged|pg)=\d+', _re.I)

    missing_from_sitemap = []
    orphan_in_sitemap = []
    sitemap_only = []
    non_indexable_in_sitemap = []
    non_200_in_sitemap = []
    redirects_in_sitemap = []
    pagination_in_sitemap = []

    for nrm, r in crawl_by_norm.items():
        if nrm in sitemap_by_norm:
            continue
        sc = r.get('status_code') or 0
        if not r.get('indexable', True):
            continue
        if sc and sc != 200:
            continue
        if r.get('redirect_url'):
            continue
        if r.get('is_pagination'):
            continue
        if _is_non_html_url(r.get('url')):
            continue
        # Skip pages whose <link rel=canonical> points elsewhere — they're
        # not the canonical version, so they shouldn't be in the sitemap.
        # Critical on Shopify where /collections/X/products/Y
        # canonicalises to /products/Y.
        canonical = (r.get('canonical') or '').strip()
        if canonical and _norm_url(canonical) != nrm:
            continue
        missing_from_sitemap.append(r.get('url'))

    # Build a canonical→row lookup so a sitemap URL whose canonical version
    # was crawled under a non-canonical alias isn't reported as sitemap-only.
    canonical_to_row = {}
    for r in results:
        can = (r.get('canonical') or '').strip()
        if can:
            canonical_to_row.setdefault(_norm_url(can), r)

    for nrm, entry in sitemap_by_norm.items():
        original_url = entry['url']
        if pag_re.search(urlparse(original_url).path) or pag_re.search('?' + (urlparse(original_url).query or '')):
            pagination_in_sitemap.append(original_url)
        crawled = crawl_by_norm.get(nrm) or canonical_to_row.get(nrm)
        if crawled is None:
            sitemap_only.append({'url': original_url, 'lastmod': entry.get('lastmod')})
            continue
        sc = crawled.get('status_code') or 0
        if sc and sc != 200:
            non_200_in_sitemap.append({'url': original_url, 'status_code': sc})
        # Only flag a "redirect in sitemap" when the redirect lands somewhere
        # OTHER than the sitemap URL. Trailing-slash / case / scheme normalization
        # often makes a crawled URL redirect to the canonical version that the
        # sitemap already lists — that's not a sitemap problem, that's the sitemap
        # being correct. _norm_url strips trailing slashes, so this catches it.
        _rdest = crawled.get('redirect_url')
        if _rdest and _norm_url(_rdest) != nrm:
            redirects_in_sitemap.append({
                'url': original_url,
                'redirects_to': _rdest,
            })
        if not crawled.get('indexable', True):
            non_indexable_in_sitemap.append({
                'url': original_url,
                'reason': 'noindex',
            })
        if (sc == 200 and not crawled.get('redirect_url')
                and crawled.get('indexable', True)
                and not inlinks_by_norm.get(nrm)):
            orphan_in_sitemap.append(original_url)

    warnings = list(discovery_warnings)
    if any(len(s.get('url', '')) and s['url'].startswith('http://') for s in sitemaps_meta):
        warnings.append('At least one sitemap is served over HTTP, not HTTPS.')
    for sm in sitemaps_meta:
        if (sm.get('url_count') or 0) > 50000:
            warnings.append(f"{sm['url']} contains {sm['url_count']} URLs — over the 50,000 sitemap limit.")
    no_lastmod = sum(1 for u in sm_urls if not u.get('lastmod'))
    if sm_urls and no_lastmod / len(sm_urls) > 0.5:
        warnings.append(f"{no_lastmod}/{len(sm_urls)} URLs in sitemap are missing <lastmod>.")

    return jsonify({
        'domain': domain,
        'sitemaps_found': discovered,
        'sitemaps_walked': sitemaps_meta,
        'sitemap_errors': sm_errors,
        'totals': {
            'urls_in_sitemap': len(sm_urls),
            'urls_in_crawl': len(crawl_by_norm),
            'sitemaps_walked': len(sitemaps_meta),
        },
        'reports': {
            'missing_from_sitemap': missing_from_sitemap,
            'orphan_in_sitemap': orphan_in_sitemap,
            'sitemap_only': sitemap_only,
            'non_indexable_in_sitemap': non_indexable_in_sitemap,
            'non_200_in_sitemap': non_200_in_sitemap,
            'redirects_in_sitemap': redirects_in_sitemap,
            'pagination_in_sitemap': pagination_in_sitemap,
        },
        'warnings': warnings,
    })


@crawler_bp.route('/near-dup-content', methods=['POST'])
def near_dup_content():
    payload = request.get_json(silent=True) or {}
    pages = payload.get('pages') or []
    try:
        threshold = float(payload.get('threshold', 0.90))
    except (TypeError, ValueError):
        threshold = 0.90
    threshold = max(0.5, min(0.99, threshold))
    exclude_sel = (payload.get('exclude_selectors') or '').strip()

    t0 = time.perf_counter()

    docs = []
    skipped = 0
    for p in pages:
        url = (p.get('url') or '').strip()
        body = p.get('body_text') or ''
        if not url or not body:
            skipped += 1
            continue
        canonical = (p.get('canonical') or '').strip()
        if canonical and canonical != url:
            skipped += 1
            continue
        if p.get('indexable') is False:
            skipped += 1
            continue
        if exclude_sel:
            body = _nd_strip_selectors(body, exclude_sel)
        toks = [t for t in _nd_tokenize(body) if t not in _ND_STOP and len(t) > 1]
        if len(toks) < 20:
            skipped += 1
            continue
        docs.append({'url': url, 'tokens': toks})

    df = {}
    for d in docs:
        for t in set(d['tokens']):
            df[t] = df.get(t, 0) + 1
    common_terms = {t for t, c in df.items() if c >= 2}

    n = 5
    sets = []
    for d in docs:
        toks = [t for t in d['tokens'] if t in common_terms]
        if len(toks) < n:
            sets.append({'url': d['url'], 'shingles': set()})
            continue
        shingles = {' '.join(toks[i:i + n]) for i in range(len(toks) - n + 1)}
        sets.append({'url': d['url'], 'shingles': shingles})

    pairs = []
    M = len(sets)
    for i in range(M):
        sa = sets[i]['shingles']
        if not sa:
            continue
        for j in range(i + 1, M):
            sb = sets[j]['shingles']
            if not sb:
                continue
            inter = len(sa & sb)
            union = len(sa | sb)
            if not union:
                continue
            sim = inter / union
            if sim >= threshold:
                sample = next(iter(sa & sb), '') if inter else ''
                pairs.append({
                    'url_a': sets[i]['url'],
                    'url_b': sets[j]['url'],
                    'similarity': round(sim, 4),
                    'shared_phrase_sample': sample,
                })
    pairs.sort(key=lambda p: -p['similarity'])

    return jsonify({
        'pairs': pairs,
        'stats': {
            'docs_analysed': M,
            'docs_skipped': skipped,
            'threshold': threshold,
            'took_ms': int((time.perf_counter() - t0) * 1000),
        },
    })


@crawler_bp.route('/recrawl-url', methods=['POST'])
def recrawl_url():
    """Re-crawl a single URL and return fresh page audit data."""
    import requests as _req
    from urllib.parse import urlparse
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    domain = urlparse(url).netloc
    try:
        with _req.Session() as session:
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,*/*;q=0.9',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            result = _crawl_page(url, session, domain)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@crawler_bp.route('/crawl/save', methods=['POST'])
def crawl_save():
    body = request.json or {}
    name = (body.get('name') or '').strip() or f'crawl-{int(time.time())}'
    results = body.get('results') or []
    inlinks = body.get('inlinks') or {}
    reports = body.get('reports') or {}
    if not results:
        return jsonify({'error': 'No crawl data supplied'}), 400
    os.makedirs(_CRAWL_FOLDER, exist_ok=True)
    safe = _re.sub(r'[^A-Za-z0-9._-]', '_', name)[:80]
    path = os.path.join(_CRAWL_FOLDER, f'{int(time.time())}_{safe}.json')
    try:
        with open(path, 'w') as f:
            json.dump({
                'name': name,
                'saved_at': int(time.time()),
                'pages': len(results),
                'seed': (results[0].get('url') if results else ''),
                'saved_by': (request.remote_addr or 'anon'),
                'results': results,
                'inlinks': inlinks,
                'reports': reports,
            }, f)
    except Exception as e:
        return jsonify({'error': f'Save failed: {str(e)[:200]}'}), 500
    # Permanent title history — append BEFORE the 30-day cleanup so purged
    # crawls still leave their titles on record forever.
    _append_crawl_title_history(name, int(time.time()), results)
    # 30-day cleanup
    try:
        cutoff = int(time.time()) - 30 * 86400
        for fn in os.listdir(_CRAWL_FOLDER):
            if not fn.endswith('.json'):
                continue
            fp = os.path.join(_CRAWL_FOLDER, fn)
            try:
                with open(fp) as f:
                    d = json.load(f)
            except Exception:
                continue
            if (d.get('saved_at') or 0) < cutoff:
                try: os.remove(fp)
                except OSError: pass
    except Exception:
        pass
    return jsonify({'ok': True, 'file': os.path.basename(path), 'name': name})


@crawler_bp.route('/crawl/list', methods=['GET'])
def crawl_list():
    """List ALL saved crawls from the last 30 days, unioning across this
    tool's dir + any configured extra dirs (read-only) so users see the
    same list whichever app they open."""
    cutoff = int(time.time()) - 30 * 86400
    name_map = _ip_name_map()
    out = []
    seen_files = set()
    for folder in _all_crawl_folders():
        if not os.path.isdir(folder): continue
        source = 'site-crawler' if folder == _CRAWL_FOLDER else 'external'
        for fn in sorted(os.listdir(folder), reverse=True):
            if not fn.endswith('.json') or fn in seen_files: continue
            seen_files.add(fn)
            path = os.path.join(folder, fn)
            try:
                with open(path) as f: d = json.load(f)
                if (d.get('saved_at') or 0) < cutoff: continue
                # Cross-tool field reconciliation: site-crawler stores the
                # IP in 'saved_by'; some external tools store it in
                # 'user_ip'. Read both, then translate via the optional
                # name map so the column shows a person's name instead of
                # a raw IP.
                raw_ip = d.get('user_ip') or d.get('saved_by') or ''
                saved_by = name_map.get(raw_ip, '') or raw_ip or 'unknown'
                out.append({
                    'file': fn,
                    'name': d.get('name', fn),
                    'saved_at': d.get('saved_at'),
                    'pages': d.get('pages', 0),
                    'seed': d.get('seed', ''),
                    'saved_by': saved_by,
                    'source': source,
                })
            except Exception:
                continue
    out.sort(key=lambda r: r.get('saved_at') or 0, reverse=True)
    return jsonify({'crawls': out})


@crawler_bp.route('/crawl/load', methods=['GET'])
def crawl_load():
    fn = request.args.get('file', '')
    if not fn or '/' in fn or '\\' in fn or not fn.endswith('.json'):
        return jsonify({'error': 'Invalid file'}), 400
    path = _find_crawl_path(fn)
    if not path:
        return jsonify({'error': 'Not found'}), 404
    try:
        with open(path) as f: d = json.load(f)
    except Exception as e:
        return jsonify({'error': f'Load failed: {str(e)[:200]}'}), 500
    return jsonify({
        'results': d.get('results', []),
        'inlinks': d.get('inlinks', {}),
        'reports': d.get('reports', {}),
        'name': d.get('name', ''),
        'seed': d.get('seed', ''),
    })


@crawler_bp.route('/crawl/delete', methods=['POST'])
def crawl_delete():
    fn = (request.json or {}).get('file', '')
    if not fn or '/' in fn or '\\' in fn or not fn.endswith('.json'):
        return jsonify({'error': 'Invalid file'}), 400
    path = _find_crawl_path(fn)
    if not path:
        return jsonify({'error': 'Not found'}), 404
    try:
        os.remove(path)
    except Exception as e:
        return jsonify({'error': f'Delete failed: {str(e)[:200]}'}), 500
    return jsonify({'ok': True})


@crawler_bp.route('/crawl/compare', methods=['POST'])
def crawl_compare():
    """Diff two crawls. Accepts {a_file, b_file} or {a_file, b_results}.
    Returns aggregate metrics, issues comparison, structure diff,
    plus added/removed/changed URL lists."""
    body = request.json or {}

    def _load_file(fn):
        if not fn or '/' in fn or '\\' in fn or not fn.endswith('.json'):
            return None, 'Invalid file'
        fp = os.path.join(_CRAWL_FOLDER, fn)
        if not os.path.exists(fp):
            return None, 'Not found'
        try:
            with open(fp) as f: d = json.load(f)
        except Exception as e:
            return None, f'Load failed: {str(e)[:200]}'
        return d, None

    a_file = body.get('a_file', '')
    a_data, err = _load_file(a_file)
    if err: return jsonify({'error': err}), 400
    a_results = a_data.get('results', [])
    a_meta = {'name': a_data.get('name',''), 'saved_at': a_data.get('saved_at'), 'pages': a_data.get('pages', len(a_results))}

    b_file = body.get('b_file', '')
    b_results_in = body.get('b_results')
    if b_file:
        b_data, err = _load_file(b_file)
        if err: return jsonify({'error': err}), 400
        b_results = b_data.get('results', [])
        b_meta = {'name': b_data.get('name',''), 'saved_at': b_data.get('saved_at'), 'pages': b_data.get('pages', len(b_results))}
    elif isinstance(b_results_in, list):
        b_results = b_results_in
        b_meta = {'name': 'Current crawl (in memory)', 'saved_at': int(time.time()), 'pages': len(b_results)}
    else:
        return jsonify({'error': 'Supply b_file or b_results'}), 400

    def _key(u):
        return (u or '').rstrip('/').lower()
    a_by = {_key(r.get('url')): r for r in a_results if r.get('url')}
    b_by = {_key(r.get('url')): r for r in b_results if r.get('url')}
    a_urls = set(a_by.keys()); b_urls = set(b_by.keys())
    added = sorted(b_urls - a_urls); removed = sorted(a_urls - b_urls)
    shared = a_urls & b_urls

    watch = (
        'status_code', 'title', 'title_len', 'meta_description', 'meta_len',
        'h1', 'word_count', 'canonical', 'redirect_url', 'indexable',
        'depth', 'response_time', 'internal_links', 'external_links',
        'images_no_alt', 'body_hash',
    )
    def _norm_list(v):
        if v is None: return ''
        if isinstance(v, list): return ', '.join(sorted(str(x) for x in v))
        return str(v)
    changed = []
    for k in sorted(shared):
        ar = a_by[k]; br = b_by[k]
        diffs = {}
        for f in watch:
            av = ar.get(f); bv = br.get(f)
            if (av if av is not None else '') != (bv if bv is not None else ''):
                diffs[f] = {'old': av, 'new': bv}
        sa = _norm_list(ar.get('schema_types')); sb = _norm_list(br.get('schema_types'))
        if sa != sb:
            diffs['schema_types'] = {'old': sa or '—', 'new': sb or '—'}
        if diffs:
            changed.append({'url': ar.get('url') or br.get('url'), 'diffs': diffs})

    def _agg(rows):
        n = len(rows); codes = {'2xx':0,'3xx':0,'4xx':0,'5xx':0,'other':0}
        errors=warns=indexable=noindex=with_schema=redirects=missing_title=missing_meta=missing_h1=missing_canonical=title_too_long=meta_too_long=thin=slow=no_alt=0
        depths=[]; rts=[]
        for r in rows:
            sc = r.get('status_code') or 0
            if 200 <= sc < 300: codes['2xx'] += 1
            elif 300 <= sc < 400: codes['3xx'] += 1
            elif 400 <= sc < 500: codes['4xx'] += 1
            elif 500 <= sc < 600: codes['5xx'] += 1
            else: codes['other'] += 1
            if sc >= 400 or r.get('error'): errors += 1
            if r.get('issues'): warns += 1
            depths.append(r.get('depth') or 0)
            if r.get('response_time'): rts.append(r['response_time'])
            if r.get('indexable') is True: indexable += 1
            elif r.get('indexable') is False: noindex += 1
            if r.get('schema_types'): with_schema += 1
            if r.get('redirect_url'): redirects += 1
            if not r.get('title'): missing_title += 1
            elif (r.get('title_len') or 0) > 60: title_too_long += 1
            if not r.get('meta_description'): missing_meta += 1
            elif (r.get('meta_len') or 0) > 160: meta_too_long += 1
            if not r.get('h1'): missing_h1 += 1
            if not r.get('canonical'): missing_canonical += 1
            if (r.get('word_count') or 0) < 200: thin += 1
            if (r.get('response_time') or 0) > 3: slow += 1
            no_alt += int(r.get('images_no_alt') or 0)
        return {'pages':n,'codes':codes,'errors':errors,'warns_pages':warns,
                'max_depth':max(depths) if depths else 0,
                'avg_depth':round(sum(depths)/len(depths),2) if depths else 0,
                'avg_response_time':round(sum(rts)/len(rts),2) if rts else 0,
                'indexable':indexable,'noindex':noindex,'with_schema':with_schema,
                'redirects':redirects,'missing_title':missing_title,'missing_meta':missing_meta,
                'missing_h1':missing_h1,'missing_canonical':missing_canonical,
                'title_too_long':title_too_long,'meta_too_long':meta_too_long,
                'thin':thin,'slow':slow,'images_no_alt':no_alt}
    agg_a = _agg(a_results); agg_b = _agg(b_results)

    def _normalize_issue(s):
        if not s: return s
        out = _re.sub(r'\s*\([^)]*\)\s*$', '', s).strip()
        out = _re.sub(r'^\d+\s+', '', out).strip()
        return out or s
    def _issue_url_sets(rows):
        m = {}
        for r in rows:
            url = r.get('url') or ''
            seen = set()
            for issue in (r.get('issues') or []):
                norm = _normalize_issue(issue)
                if not norm or norm in seen: continue
                seen.add(norm)
                m.setdefault(norm, set()).add(url)
        return m
    urls_a_by_issue = _issue_url_sets(a_results)
    urls_b_by_issue = _issue_url_sets(b_results)
    issues_compare = []
    _URL_CAP = 500
    for iss in sorted(set(urls_a_by_issue) | set(urls_b_by_issue)):
        ua = urls_a_by_issue.get(iss, set()); ub = urls_b_by_issue.get(iss, set())
        ia = len(ua); ib = len(ub)
        if ia == ib == 0: continue
        only_a = sorted(ua - ub)[:_URL_CAP]
        only_b = sorted(ub - ua)[:_URL_CAP]
        both   = sorted(ua & ub)[:_URL_CAP]
        issues_compare.append({
            'issue': iss, 'a': ia, 'b': ib, 'delta': ib - ia,
            'only_a': only_a, 'only_b': only_b, 'both': both,
            'only_a_total': len(ua - ub),
            'only_b_total': len(ub - ua),
            'both_total':   len(ua & ub),
        })
    issues_compare.sort(key=lambda x: (abs(x['delta']), x['a'] + x['b']), reverse=True)

    from urllib.parse import urlparse as _urlp
    def _dir_counts(rows):
        c = {}
        for r in rows:
            try: p = _urlp(r.get('url') or '').path or '/'
            except Exception: p = '/'
            seg = '/' + p.lstrip('/').split('/', 1)[0] + ('/' if '/' in p.lstrip('/') else '')
            if seg in ('/', '/'): seg = '/' if p == '/' else seg
            c[seg] = c.get(seg, 0) + 1
        return c
    dirs_a = _dir_counts(a_results); dirs_b = _dir_counts(b_results)
    structure = []
    for d in sorted(set(dirs_a) | set(dirs_b)):
        da = dirs_a.get(d, 0); db = dirs_b.get(d, 0)
        if da == db == 0: continue
        structure.append({'path': d, 'a': da, 'b': db, 'delta': db - da})
    structure.sort(key=lambda x: -(x['a'] + x['b']))

    return jsonify({
        'a': a_meta, 'b': b_meta,
        'aggregate': {'a': agg_a, 'b': agg_b},
        'issues': issues_compare,
        'structure': structure[:30],
        'added': [{'url': b_by[k].get('url'), 'status_code': b_by[k].get('status_code'), 'title': b_by[k].get('title')} for k in added],
        'removed': [{'url': a_by[k].get('url'), 'status_code': a_by[k].get('status_code'), 'title': a_by[k].get('title')} for k in removed],
        'changed': changed,
        'summary': {'added': len(added), 'removed': len(removed),
                    'changed': len(changed), 'unchanged': len(shared) - len(changed)},
    })


@crawler_bp.route('/crawl/update-rules', methods=['POST'])
def crawl_update_rules():
    """Apply new include/exclude patterns AND/OR per-host delay to a crawl
    that's already running.

    Merges into the existing entry — fields not present in the payload keep
    their current value. The slider can push only `crawl_delay` without
    blowing away the patterns; the patterns Apply button can push only
    include/exclude without resetting the delay.

    The crawl_id is issued in the 'start' SSE event. Patterns use robots.txt
    syntax: ``*`` is any sequence, ``$`` at end anchors end of URL, everything
    else is literal (including ``?``).
    """
    payload = request.get_json(silent=True) or {}
    crawl_id = (payload.get('crawl_id') or '').strip()
    if not crawl_id or crawl_id not in ACTIVE_CRAWL_RULES:
        return jsonify({'ok': False, 'error': 'No active crawl with that id'}), 404

    def _parse(raw):
        if not raw:
            return []
        return [p.strip() for p in raw.splitlines() if p.strip() and not p.strip().startswith('#')]

    current = ACTIVE_CRAWL_RULES.get(crawl_id) or {}
    if 'exclude_patterns' in payload:
        current['exclude'] = _parse(payload.get('exclude_patterns', ''))
    if 'include_patterns' in payload:
        current['include'] = _parse(payload.get('include_patterns', ''))
    if 'crawl_delay' in payload:
        try:
            current['crawl_delay'] = max(float(payload.get('crawl_delay')), 0.0)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'crawl_delay must be numeric'}), 400
    ACTIVE_CRAWL_RULES[crawl_id] = current
    current_app.logger.info(
        f"[crawler] {crawl_id} rules updated: "
        f"{len(current.get('exclude') or [])} exclude, "
        f"{len(current.get('include') or [])} include, "
        f"delay={current.get('crawl_delay')}s"
    )
    return jsonify({
        'ok': True,
        'exclude': current.get('exclude') or [],
        'include': current.get('include') or [],
        'crawl_delay': current.get('crawl_delay'),
    })


@crawler_bp.route('/crawl/resumable', methods=['GET'])
def crawl_resumable():
    """Return metadata for a suspended crawl (or 404 if unknown/expired).
    Used by the UI to decide whether to show a Resume button."""
    crawl_id = (request.args.get('crawl_id') or '').strip()
    state = SUSPENDED_CRAWLS.get(crawl_id)
    if not state:
        return jsonify({'ok': False, 'error': 'No suspended crawl with that id'}), 404
    age = int(time.time() - state.get('created', 0))
    if age > SUSPENDED_CRAWL_TTL:
        SUSPENDED_CRAWLS.pop(crawl_id, None)
        return jsonify({'ok': False, 'error': 'Suspended crawl expired'}), 410
    return jsonify({
        'ok': True,
        'crawl_id': crawl_id,
        'seed_url': state.get('seed_url'),
        'domain': state.get('domain'),
        'pages': len(state.get('results', [])),
        'queued': len(state.get('queue', [])),
        'errors': state.get('errors', 0),
        'age_seconds': age,
        'ttl_seconds': SUSPENDED_CRAWL_TTL,
    })


@crawler_bp.route('/crawl/continue', methods=['POST'])
def crawl_continue():
    """Resume a crawl that's paused at the page-cap prompt.

    action=continue → bump max_pages by `bump` (default 500) and resume.
    action=finalize → stop with what we have, run summary/reports.
    Either way, signals the generator's continue_event to wake it up.
    """
    payload = request.get_json(silent=True) or {}
    crawl_id = (payload.get('crawl_id') or '').strip()
    action = (payload.get('action') or 'continue').strip().lower()
    bump = int(payload.get('bump', 500) or 500)

    state = ACTIVE_CRAWL_LIMITS.get(crawl_id)
    if not state:
        return jsonify({'ok': False, 'error': 'No paused crawl with that id'}), 404

    if action == 'finalize':
        state['finalize'] = True
    else:
        state['max_pages'] = state.get('max_pages', 0) + bump
        state['bumps'] = state.get('bumps', 0) + 1

    ev = state.get('continue_event')
    if ev is not None:
        ev.set()
    current_app.logger.info(f"[crawler] {crawl_id} {action}: max_pages={state.get('max_pages')}, bumps={state.get('bumps')}")
    return jsonify({'ok': True, 'max_pages': state.get('max_pages'), 'finalize': state.get('finalize'), 'bumps': state.get('bumps')})


@crawler_bp.route('/crawl', methods=['POST'])
def crawl_site_route():
    return crawl_site()
