"""Bounded browser-initiated research discovery for a local FINN service."""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import build_opener

from check_data import (
    Checker, Links, NSE_SECURITIES_URL, NSE_SITE, add_document_candidates, announcements_url, csv_rows, select_document_candidates,
)
from .db import ROOT, connect
from .refresh import ATTACHMENT_HOSTS, IST, NoRedirect
from .research import KINDS, index_report, ticker


NSE_ANNUAL = NSE_SITE + '/api/annual-reports?'
SCREENER = 'https://www.screener.in/company/'
DOCUMENT_HOSTS = ATTACHMENT_HOSTS | {'www.bseindia.com', 'bseindia.com', 'archives.bseindia.com'}
REQUEST_TIMEOUT = 12
JOB_TIMEOUT = 120
PDF_LIMIT = 40 * 1024 * 1024
METADATA_LIMIT = 4 * 1024 * 1024
ATTEMPT_LIMIT = 3


def allowed_url(url, hosts):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname in hosts
                and parts.port in (None, 443) and parts.username is None and parts.password is None)
    except ValueError:
        return False


class ResearchChecker(Checker):
    def fetch(self, name, url):
        document = name.startswith(('annual_', 'transcript_')) and name != 'annual_catalog'
        hosts = DOCUMENT_HOSTS if document else ATTACHMENT_HOSTS | {'www.screener.in'}

        def open_approved(request, *, timeout):
            if not allowed_url(request.full_url, hosts):
                raise ValueError('URL is outside the approved source hosts')
            return build_opener(NoRedirect()).open(request, timeout=timeout)

        return super().fetch(name, url, max_bytes=PDF_LIMIT if document else METADATA_LIMIT,
                             attempts=1, open_url=open_approved)


def json_rows(body, field=None):
    try:
        data = json.loads(body) if body else None
        if field:
            data = data.get(field) if isinstance(data, dict) else None
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    except (ValueError, UnicodeError):
        pass
    return None


def discover(checker, symbol):
    end = checker.args.as_of
    screener = SCREENER + quote(symbol, safe='') + '/consolidated/'
    sources = {
        'annual_catalog': NSE_ANNUAL + urlencode({'index': 'equities', 'symbol': symbol}),
        'announcements': announcements_url(symbol, end - timedelta(days=400), end),
        'screener': screener,
    }
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        requests = {name: pool.submit(checker.fetch, name, url) for name, url in sources.items()}
        bodies = {name: task.result() for name, task in requests.items()}
    reports = json_rows(bodies['annual_catalog'], 'data')
    filings = json_rows(bodies['announcements'])
    candidates = {kind: [] for kind in ('annual', 'transcript', 'filing')}

    if reports is not None:
        def year_key(row):
            return str(row.get('toYr') or '').zfill(4)
        # NSE's direct archive name includes the symbol; ignore old ZIP bundles.
        for row in sorted(reports, key=year_key, reverse=True):
            url = row.get('fileName')
            if (isinstance(url, str) and allowed_url(url, ATTACHMENT_HOSTS)
                    and f'_{symbol}_' in unquote(urlsplit(url).path).upper()
                    and urlsplit(url).path.lower().endswith('.pdf')):
                candidates['annual'].append((url, 'automatic:nse_annual_reports'))
                break
    links = []
    if bodies['screener']:
        parser = Links()
        parser.feed(bodies['screener'].decode('utf-8', errors='replace'))
        for label, href in parser.links:
            try:
                links.append((label, urljoin(screener, href)))
            except ValueError:
                continue
    relevant = [row for row in filings or [] if row.get('symbol') == symbol
                and isinstance(row.get('attchmntFile'), str)]
    for row in relevant:
        # The existing helper sorts by the timestamp supplied by NSE.
        row['sort_date'] = str(row.get('sort_date') or row.get('an_dt') or '')
    add_document_candidates(candidates, links, relevant)
    # Prefer full PDFs on the exchange over viewer links from a discovery index.
    candidates['transcript'].sort(key=lambda item: item[1] != 'automatic:nse_announcements')
    outages = {
        'annual': [label for ok, label in ((reports is not None, 'NSE annual reports'),
                   (filings is not None, 'NSE announcements'), (bool(links), 'Screener')) if not ok],
        'transcript': [label for ok, label in ((filings is not None, 'NSE announcements'),
                        (bool(links), 'Screener')) if not ok],
    }
    return candidates, outages


def valid_symbol(checker, symbol):
    for name in ('securities', 'securities_retry'):
        body = checker.fetch(name, NSE_SECURITIES_URL)
        if body is None:
            continue
        try:
            rows = csv_rows(body)
        except (ValueError, UnicodeError):
            continue
        if rows and 'SYMBOL' in rows[0]:
            return any(row['SYMBOL'] == symbol for row in rows)
    return None


def download_kind(checker, kind, choices):
    attempts = []
    minimum = KINDS[kind][1]
    pending = list(select_document_candidates(choices))
    seen = set()
    while pending and len(attempts) < ATTEMPT_LIMIT:
        url, origin = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not allowed_url(url, DOCUMENT_HOSTS):
            doc = {'kind': kind, 'url': url, 'provenance': origin, 'download': 'failed',
                   'extraction': 'not_attempted', 'validation': 'needs_validation',
                   'error': 'Candidate is outside the supported NSE and BSE PDF hosts.'}
            checker.report['documents'].append(doc)
            attempts.append(doc)
            continue
        ok, links = checker.document(kind, url, origin, len(attempts) + 1,
                                     max_pages=650, max_text_chars=4_000_000)
        doc = checker.report['documents'][-1]
        attempts.append(doc)
        if ok and doc['pages'] >= minimum:
            break
        if ok:
            doc['content_note'] = 'Possible cover letter, not full source.'
            pending[:0] = [(link, origin + ':embedded_pdf_link') for link in links[:100]
                           if allowed_url(link, DOCUMENT_HOSTS)
                           and re.search(r'annual|report|transcript|earningscall', link, re.I)
                           and urlsplit(link).path.lower().endswith('.pdf')][:1]
    return attempts


def write_status(path, data):
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    temporary.replace(path)


def read_status(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return {}


def status_for(directory, symbol):
    status = read_status(directory / f'{symbol}.json')
    if status.get('state') == 'running' and time.time() - status['started'] > JOB_TIMEOUT + 5:
        return {**status, 'state': 'failed', 'message': 'Fetch stopped before completion. You can retry.'}
    return status


def storage_for(db):
    db_path = Path(db.execute('PRAGMA database_list').fetchone()['file'])
    return db_path.parent / 'research-fetch' / db_path.name


def missing_kinds(db, symbol):
    rows = db.execute('SELECT DISTINCT kind FROM research_sources WHERE symbol=?', (symbol,))
    cached = {row['kind'] for row in rows}
    return [kind for kind in KINDS if kind not in cached]


def run_fetch(symbol, directory):
    status_path = directory / f'{symbol}.json'
    status = read_status(status_path)
    status.setdefault('started', time.time())

    def update(message, state='running'):
        status.update(message=message, state=state)
        write_status(status_path, status)

    checker = ResearchChecker(SimpleNamespace(
        ticker=symbol, as_of=datetime.now(IST).date(), document=[],
        timeout=REQUEST_TIMEOUT, out=str(directory / 'runs')))
    validated = valid_symbol(checker, symbol)
    if validated is not True:
        detail = ('Ticker was not found in NSE’s equity securities list.' if validated is False
                  else 'The NSE securities list could not be verified. Try again later.')
        checker.check('securities', 'failed', detail)
        checker.finish()
        update(detail, 'failed')
        return
    checker.check('securities', 'succeeded', 'Matched the NSE equity securities list.')
    with closing(connect()) as db:
        kinds = missing_kinds(db, symbol)
    if not kinds:
        update('Both sources are already indexed.', 'done')
        return

    update('Discovering annual reports and transcripts.')
    candidates, outages = discover(checker, symbol)
    checker.report['candidates'] = {kind: candidates[kind] for kind in kinds}
    selected_by_kind = {kind: select_document_candidates(candidates[kind]) for kind in kinds}
    results = status['sources'] = {
        kind: {'title': KINDS[kind][0], 'discovered': len(selected), 'downloaded': 0, 'indexed': False}
        for kind, selected in selected_by_kind.items()}
    for kind, selected in selected_by_kind.items():
        title = KINDS[kind][0]
        entry = results[kind]
        update(f'Checking {title.lower()} candidates.')
        attempts = download_kind(checker, kind, selected)
        entry['downloaded'] = sum(doc['download'] == 'succeeded' for doc in attempts)
        entry['attempted'] = len(attempts)
        usable = any(doc['extraction'] == 'succeeded' and not doc.get('content_note') for doc in attempts)
        if usable:
            detail = 'PDF text extracted.'
        elif selected:
            detail = 'Discovered candidate PDFs, but none yielded usable text within the fetch limits.'
        else:
            detail = 'No candidate PDF was discovered.'
        if outages[kind]:
            detail += ' Discovery unavailable from ' + ', '.join(outages[kind]) + '.'
        checker.check(kind + '_content', 'succeeded' if usable else 'failed', detail)
        update(f'Indexing {title.lower()} evidence.')
        checker.finish()
        with closing(connect()) as db, db:
            index_report(db, symbol, checker.root / 'report.json', kinds=(kind,), prefer_cached_text=True)
            source = db.execute('SELECT url FROM research_sources WHERE symbol=? AND kind=?',
                                (symbol, kind)).fetchone()
            entry['indexed'] = bool(source)
            if source:
                entry['url'] = source['url']
            else:
                row = db.execute('SELECT detail FROM research_status WHERE symbol=? AND kind=?',
                                 (symbol, kind)).fetchone()
                entry['detail'] = row['detail'] if row else detail
        update(f'Finished checking {title.lower()}.')

    indexed = sum(entry['indexed'] for entry in results.values())
    update('Fetch complete. Searchable evidence is shown below.' if indexed
           else 'Fetch complete. No new sources were indexed.', 'done')


def supervise(directory, symbol, lock):
    path = directory / f'{symbol}.json'
    try:
        with (directory / f'{symbol}.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(
                [sys.executable, '-m', 'finn_app.research_fetch', symbol, str(directory)],
                cwd=ROOT, stdout=log, stderr=log, timeout=JOB_TIMEOUT + 2,
                env={**os.environ, 'FINN_DB': str(directory.parents[1] / directory.name)},
                pass_fds=(lock.fileno(),))
        if result.returncode:
            status = read_status(path)
            message = ('The fetch time limit was reached. Any indexed source is still available.'
                       if result.returncode == -signal.SIGALRM else 'Fetch stopped unexpectedly. You can retry.')
            write_status(path, {**status, 'state': 'failed', 'message': message})
    except subprocess.TimeoutExpired:
        write_status(path, {**read_status(path), 'state': 'failed',
                            'message': 'The fetch time limit was reached. Any indexed source is still available.'})
    except OSError:
        write_status(path, {**read_status(path), 'state': 'failed',
                            'message': 'The fetch worker could not start. You can retry.'})
    finally:
        lock.close()


def start_fetch(directory, symbol):
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / '.fetch.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return status_for(directory, symbol).get('state') == 'running'
    try:
        write_status(directory / f'{symbol}.json', {'state': 'running', 'started': time.time(),
                                                    'message': 'Validating ticker with NSE.'})
        Thread(target=supervise, args=(directory, symbol, lock), daemon=True).start()
    except BaseException:
        lock.close()
        raise
    return True


if __name__ == '__main__':
    symbol = ticker(sys.argv[1])
    if not symbol:
        raise SystemExit('Invalid ticker')
    signal.alarm(JOB_TIMEOUT)
    run_fetch(symbol, Path(sys.argv[2]))
