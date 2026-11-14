"""Import existing check_data.py reports. Never fetches the network or edits cache files."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pypdf import PdfReader
from .db import ROOT, connect
from .rules import classify


def canonical_url(value):
    parts = urlsplit(str(value or '').strip())
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return ''
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ''))


def filing_time(row):
    # an_dt is the exchange filing timestamp, not the date embedded in a filename.
    for field, formats in [('an_dt', ['%d-%b-%Y %H:%M:%S']),
                           ('sort_date', ['%Y-%m-%d %H:%M:%S'])]:
        value = str(row.get(field) or '')
        for pattern in formats:
            try:
                parsed = datetime.strptime(value, pattern).replace(tzinfo=ZoneInfo('Asia/Kolkata'))
                return parsed.isoformat(), f'{field}={value}'
            except ValueError:
                pass
    return None, str(row.get('an_dt') or row.get('sort_date') or 'Unavailable')


def local_file(run, value):
    if not value or not isinstance(value, str):
        return None
    candidate = (run / value).resolve()
    if not candidate.is_relative_to(run.resolve()):
        return None
    return candidate if candidate.is_file() else None


def load_document(db, run, doc, *, prefer_cached_text=False):
    pdf = local_file(run, doc.get('pdf'))
    text_file = local_file(run, doc.get('text'))
    if not pdf and not text_file:
        return None
    digest = hashlib.sha256((pdf or text_file).read_bytes()).hexdigest()
    if pdf and doc.get('sha256') and digest != doc['sha256']:
        print(f'VALIDATION: PDF hash mismatch, not imported: {pdf}')
        return None
    existing = db.execute('SELECT * FROM documents WHERE sha=?', (digest,)).fetchone()
    if existing and existing['extraction'] == 'ready':
        if pdf:
            db.execute('UPDATE documents SET path=? WHERE sha=?', (str(pdf), digest))
        return digest
    pages, note = [], 'PDF text extracted locally; content and table accuracy remain unvalidated.'
    if pdf and not (prefer_cached_text and text_file):
        try:
            pages = [(i, page.extract_text() or '') for i, page in enumerate(PdfReader(pdf).pages, 1)]
        except Exception as exc:
            note = f'PDF extraction failed ({type(exc).__name__}).'
    if not any(text.strip() for _, text in pages) and text_file:
        text = text_file.read_text(encoding='utf-8', errors='replace')
        segments = re.split(r'(?m)^\s*--- PDF PAGE (\d+) ---\s*$', text)
        if len(segments) > 1:
            pages = [(int(segments[i]), segments[i + 1]) for i in range(1, len(segments) - 1, 2)]
        else:
            # Text exists but has no trustworthy page mapping. Never invent page 1.
            pages = [(0, text)]
        note = 'Using cached text; correspondence to the PDF remains unvalidated.'
    pages = [(page, text.strip()) for page, text in pages if text.strip()]
    db.execute('INSERT INTO documents VALUES (?, ?, ?, ?) ON CONFLICT(sha) DO UPDATE SET '
               'path=excluded.path, extraction=excluded.extraction, note=excluded.note',
               (digest, str(pdf) if pdf else None, 'ready' if pages else 'unavailable', note))
    db.execute('DELETE FROM pages WHERE sha=?', (digest,))
    db.executemany('INSERT INTO pages VALUES (?, ?, ?)', [(digest, page, text) for page, text in pages])
    return digest


def import_report(db, report_path, *, data=None):
    if data is None:
        data = json.loads(report_path.read_text(encoding='utf-8'))
    symbol = str(data.get('ticker') or '').upper()
    if not re.fullmatch(r'[A-Z0-9&_-]+', symbol):
        raise ValueError('Missing or invalid report ticker')
    records = data.get('filings', [])
    if not isinstance(records, list):
        raise ValueError('filings must be a list')
    sample = next((r for r in records if r.get('symbol') == symbol), {})
    db.execute('INSERT OR IGNORE INTO companies VALUES (?, ?, ?)',
               (symbol, sample.get('sm_name') or symbol, sample.get('smIndustry') or 'Unmapped sector'))
    master = next((r.get('ISIN NUMBER', '') for r in data.get('security', [])
                   if r.get('SYMBOL') == symbol), '')
    documents = {}
    for doc in data.get('documents', []):
        url = canonical_url(doc.get('url'))
        if url and (doc.get('pdf') or doc.get('text')):
            documents[url] = doc
    added = updated = linked = skipped = 0
    for row in records:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            skipped += 1; continue
        url = canonical_url(row.get('attchmntFile'))
        if not url:
            skipped += 1; continue
        exchange_id = str(row.get('seq_id') or '')
        existing = db.execute('SELECT id, document_sha, source_url, identity_note FROM events WHERE symbol=? AND '
                              '(source_url=? OR (?<>\'\' AND exchange_id=?))',
                              (symbol, url, exchange_id, exchange_id)).fetchone()
        event_id = existing['id'] if existing else hashlib.sha256(
            f'{symbol}|{exchange_id or url}'.encode()).hexdigest()[:24]
        subject = str(row.get('desc') or 'Exchange filing')
        description = str(row.get('attchmntText') or '')
        triage = classify(subject, description)
        filed_at, raw_date = filing_time(row)
        isin = str(row.get('sm_isin') or '')
        identity_note = (f'Identity conflict: securities list {master}; announcement metadata {isin}. '
                         'Historical-code issue remains flagged.' if master and isin and master != isin else '')
        if existing and (not master or not isin):
            identity_note = existing['identity_note']
        sha = existing['document_sha'] if existing and existing['source_url'] == url else None
        if url in documents:
            # Only an exact normalized source URL can attach document evidence to an event.
            sha = load_document(db, report_path.parent, documents[url]) or sha
        db.execute('''INSERT INTO events
          (id,symbol,exchange_id,source_url,subject,description,filed_at,raw_date,category,
           priority,rule,reason,document_sha,identity_note,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET source_url=excluded.source_url, subject=excluded.subject,
           description=excluded.description, filed_at=excluded.filed_at, raw_date=excluded.raw_date,
           category=excluded.category, priority=excluded.priority, rule=excluded.rule,
           reason=excluded.reason, document_sha=excluded.document_sha,
           identity_note=excluded.identity_note, origin=excluded.origin,
           exchange_id=excluded.exchange_id''',
          (event_id, symbol, exchange_id, url, subject, description, filed_at, raw_date,
           triage['category'], triage['priority'], triage['rule'], triage['reason'], sha,
           identity_note, str(report_path.resolve())))
        added += not bool(existing)
        updated += bool(existing)
        linked += bool(sha)
    note = f'{added} added, {updated} updated, {linked} document links, {skipped} skipped'
    db.execute('INSERT INTO imports VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET '
               'imported_at=excluded.imported_at, as_of=excluded.as_of, note=excluded.note',
               (str(report_path.resolve()), datetime.now(timezone.utc).isoformat(), data.get('as_of'), note))
    print(f'{symbol}: {note}')
    return added


def import_paths(paths):
    reports = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if path.is_file() and path.name == 'report.json':
            reports.add(path)
        elif path.is_dir():
            reports.update(path.rglob('report.json'))
        else:
            print(f'NOTICE: cache path not found: {path}')
    if not reports:
        raise ValueError('No report.json found. Use --cache with the directory created by check_data.py.')
    db, failures = connect(), 0
    try:
        for report in sorted(reports):
            try:
                with db:
                    import_report(db, report)
            except Exception as exc:
                failures += 1
                print(f'FAILED {report}: {type(exc).__name__}: {exc}')
        total = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        evidence = db.execute('SELECT COUNT(DISTINCT e.id) FROM events e JOIN pages p '
                              'ON p.sha=e.document_sha').fetchone()[0]
        print(f'Database: {total} events; {evidence} with cached document text; {failures} reports failed.')
    finally:
        db.close()
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', action='append', help='Cache directory or report.json; repeatable')
    args = parser.parse_args()
    try:
        failures = import_paths(args.cache or [ROOT / 'data_checks', ROOT / 'runs'])
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    if failures:
        parser.exit(1, 'Some reports failed; successful reports were retained.\n')


if __name__ == '__main__':
    main()
