"""Refresh NSE portfolio filings from a terminal. See REFRESH_README.md."""
import argparse
import fcntl
import hashlib
import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from zoneinfo import ZoneInfo

from check_data import Checker, NSE_SECURITIES_URL, announcements_url, csv_rows
from .db import connect, portfolio
from .import_cache import canonical_url, filing_time, import_report, load_document


IST = ZoneInfo('Asia/Kolkata')
ATTACHMENT_HOSTS = {f'{host}.nseindia.{suffix}'
                    for host in ('nsearchives', 'archives', 'www') for suffix in ('com', 'in')}


class NoRedirect(HTTPRedirectHandler):
    """Refresh uses direct exchange URLs. A redirect is reported as a failure."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def fetch(checker, name, url, max_mib):
    return checker.fetch(
        name, url, max_bytes=max_mib * 1024 * 1024, attempts=1,
        open_url=build_opener(NoRedirect()).open,
    )


def exchange_rows(body, symbol, start, end):
    if body is None:
        raise ValueError('NSE request failed. See the saved HTTP log.')
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise ValueError('Expected an NSE announcement array')
    accepted = []
    for row in rows:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            raise ValueError('Response includes a record for an unexpected symbol or shape')
        filed_at, _ = filing_time(row)
        if not filed_at or not start.isoformat() <= filed_at[:10] <= end.isoformat():
            raise ValueError('Response includes a missing or out-of-window filing date')
        accepted.append(row)
    return accepted


def can_download(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname in ATTACHMENT_HOSTS
                and parts.username is None and parts.password is None
                and parts.port in (None, 443))
    except ValueError:
        return False


def evidence_candidates(db, successful):
    candidates = {}
    for symbol, checker in successful.items():
        for row in checker.report['filings']:
            url = canonical_url(row.get('attchmntFile'))
            if not can_download(url):
                continue
            event = db.execute('''
                SELECT e.id, e.source_url, e.priority, e.filed_at, e.document_sha, d.path,
                    EXISTS(SELECT 1 FROM pages p WHERE p.sha=e.document_sha) AS has_text
                FROM events e LEFT JOIN documents d ON d.sha=e.document_sha
                WHERE e.symbol=? AND e.source_url=? AND e.priority>=70
            ''', (symbol, url)).fetchone()
            if event and not event['has_text'] and not (event['path'] and Path(event['path']).is_file()):
                candidates[event['id']] = (symbol, event)
    return sorted(candidates.values(), key=lambda pair: (
        pair[1]['priority'], pair[1]['filed_at'], pair[1]['id']), reverse=True)


def save_report(checker):
    path = checker.root / 'report.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(checker.report, indent=2), encoding='utf-8')
    temporary.replace(path)
    return path


def download_evidence(db, checker, event, index):
    name = f'priority_{index:02d}'
    body = fetch(checker, name, event['source_url'], 12)
    if body is None:
        return 'download failed'
    if not body.startswith(b'%PDF-'):
        return 'response is not a PDF'
    relative_path = f'documents/{name}.pdf'
    (checker.root / relative_path).write_bytes(body)
    doc = {'url': event['source_url'], 'pdf': relative_path,
           'sha256': hashlib.sha256(body).hexdigest()}
    # Keep the report and original bytes available even if extraction fails.
    checker.report['documents'].append(doc)
    save_report(checker)
    with db:
        sha = load_document(db, checker.root, doc)
        if sha is None:
            return 'PDF validation failed'
        updated = db.execute('''
            UPDATE events SET document_sha=? WHERE id=? AND source_url=? AND document_sha IS ?
        ''', (sha, event['id'], event['source_url'], event['document_sha'])).rowcount
        if not updated:
            return 'event or evidence changed during refresh'
        ready = db.execute('SELECT extraction FROM documents WHERE sha=?', (sha,)).fetchone()
    return 'cached text and PDF' if ready['extraction'] == 'ready' else 'PDF saved, text unavailable'


def refresh(db, symbols, start, end, timeout, evidence_limit):
    with db:
        run_id = db.execute('''
            INSERT INTO refresh_runs(started_at,from_date,to_date,company_count)
            VALUES (?,?,?,?)
        ''', (now_utc(), start.isoformat(), end.isoformat(), len(symbols))).lastrowid
    db_path = Path(db.execute('PRAGMA database_list').fetchone()['file'])
    run_root = db_path.parent / 'refresh' / f'run-{run_id}'
    print(f'Run {run_id} | {start} through {end} IST | {len(symbols)} companies')
    successful = {}
    securities = []
    for index, symbol in enumerate(symbols):
        checker = Checker(SimpleNamespace(
            out=str(run_root), ticker=symbol, as_of=end, document=[], timeout=timeout))
        if index == 0:
            body = fetch(checker, 'securities', NSE_SECURITIES_URL, 4)
            if body:
                try:
                    securities = csv_rows(body)
                except (UnicodeError, ValueError) as exc:
                    print(f'Securities list could not be parsed: {exc}')
        checker.report['security'] = [row for row in securities if row.get('SYMBOL') == symbol]
        try:
            body = fetch(checker, 'announcements', announcements_url(symbol, start, end), 4)
            rows = exchange_rows(body, symbol, start, end)
            fetched_at = now_utc()
            checker.report.update(filings=rows, fetched_at=fetched_at, from_date=start.isoformat())
            report_path = save_report(checker)
            with db:
                import_report(db, report_path)
                db.execute('INSERT INTO refresh_results VALUES (?,?,?,?,?,?)',
                           (run_id, symbol, 'succeeded', fetched_at, len(rows),
                            'Response accepted and imported.'))
            successful[symbol] = checker
            print(f'{symbol}: succeeded ({len(rows)} source rows)')
        except Exception as exc:
            detail = str(exc)[:300] or type(exc).__name__
            with db:
                db.execute('INSERT INTO refresh_results VALUES (?,?,?,?,?,?)',
                           (run_id, symbol, 'failed', None, 0, detail))
            print(f'{symbol}: failed, cache kept. {detail}')

    attempts = evidence_candidates(db, successful)[:evidence_limit] if evidence_limit else []
    for index, (symbol, event) in enumerate(attempts, 1):
        try:
            outcome = download_evidence(db, successful[symbol], event, index)
        except Exception as exc:
            outcome = f'evidence error: {type(exc).__name__}: {str(exc)[:180]}'
        print(f"{symbol}: evidence {index}/{len(attempts)} (priority {event['priority']}), {outcome}")
    with db:
        db.execute('UPDATE refresh_runs SET completed_at=? WHERE id=?', (now_utc(), run_id))
    failures = len(symbols) - len(successful)
    print(f'Run {run_id} complete. {len(successful)} succeeded, {failures} failed. '
          f'{len(attempts)} evidence attempts. Logs: {run_root}')
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols', nargs='+', metavar='TICKER', help='Portfolio subset')
    parser.add_argument('--days', type=int, default=7, help='Inclusive filing dates, 1–31')
    parser.add_argument('--as-of', type=date.fromisoformat, default=datetime.now(IST).date(),
                        help='End date in IST, YYYY-MM-DD')
    parser.add_argument('--evidence-limit', type=int, default=5, help='Total PDF attempts, 0–15')
    parser.add_argument('--timeout', type=int, default=8, help='Network timeout in seconds, 1–30')
    args = parser.parse_args()
    if not 1 <= args.days <= 31 or not 0 <= args.evidence_limit <= 15 or not 1 <= args.timeout <= 30:
        parser.error('Use --days 1–31, --evidence-limit 0–15 and --timeout 1–30.')
    if args.as_of > datetime.now(IST).date():
        parser.error('--as-of cannot be in the future in IST.')
    allowed = [item['symbol'] for item in portfolio()]
    symbols = list(dict.fromkeys(symbol.upper() for symbol in args.symbols)) if args.symbols else allowed
    if any(symbol not in allowed for symbol in symbols):
        parser.error('Unknown portfolio ticker. Choose from ' + ', '.join(allowed))
    start = args.as_of - timedelta(days=args.days - 1)
    with closing(connect()) as db:
        lock_path = Path(db.execute('PRAGMA database_list').fetchone()['file']).with_suffix('.refresh.lock')
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, 'Another refresh is already running for this database.\n')
            return refresh(db, symbols, start, args.as_of, args.timeout, args.evidence_limit)


if __name__ == '__main__':
    raise SystemExit(main())
