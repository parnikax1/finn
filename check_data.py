#!/usr/bin/env python3
"""Read-only NSE data feasibility checks. Python 3.10+; pip install pypdf."""
import argparse
import csv
import hashlib
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pypdf import PdfReader


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.current, self.words = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.current, self.words = dict(attrs).get('href'), []

    def handle_data(self, text):
        if self.current:
            self.words.append(text)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current:
            self.links.append((' '.join(' '.join(self.words).split()), self.current))
            self.current = None


def isin_valid(value):
    if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', value):
        return False
    digits = ''.join(str(int(c, 36)) for c in value)
    nums = [int(c) * (2 if i % 2 else 1) for i, c in enumerate(digits[::-1])]
    return sum(n // 10 + n % 10 for n in nums) % 10 == 0


def csv_rows(body):
    return [{k.strip(): (v or '').strip() for k, v in row.items() if k}
            for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig')))]


NSE_SITE = 'https://www.nseindia.in'
NSE_ARCHIVE = 'https://nsearchives.nseindia.in'
NSE_HOLIDAYS_URL = NSE_SITE + '/api/holiday-master?type=trading'
NSE_SECURITIES_URL = NSE_ARCHIVE + '/content/equities/EQUITY_L.csv'


def bhavcopy_url(day):
    return (NSE_ARCHIVE + '/products/content/sec_bhavdata_full_'
            + day.strftime('%d%m%Y') + '.csv')



def announcements_url(symbol, start, end):
    params = urlencode({
        'index': 'equities', 'symbol': symbol,
        'from_date': start.strftime('%d-%m-%Y'), 'to_date': end.strftime('%d-%m-%Y'),
    })
    return NSE_SITE + '/api/corporate-announcements?' + params


def add_document_candidates(candidates, links, filings):
    for label, url in links:
        if re.match(r'^Annual Report\s+\d{4}$', label):
            candidates['annual'].append((url, 'automatic:screener'))
        elif label == 'Transcript':
            candidates['transcript'].append((url, 'automatic:screener'))
    ordered = sorted(filings, key=lambda r: r.get('sort_date', r.get('an_dt', '')), reverse=True)
    for record in ordered:
        url = record.get('attchmntFile')
        if not url:
            continue
        description = ' '.join(str(record.get(k) or '') for k in ('desc', 'attchmntText'))
        if re.search(r'transcript', description, re.I):
            candidates['transcript'].append((url, 'automatic:nse_announcements'))
        if re.search(r'annual report', description, re.I):
            candidates['annual'].append((url, 'automatic:nse_announcements'))
        candidates['filing'].append((url, 'automatic:nse_announcements'))


def select_document_candidates(choices):
    selected, seen, screener_seen = [], set(), False
    for url, provenance in choices:
        if url in seen or (provenance.endswith('screener') and screener_seen):
            continue
        seen.add(url)
        screener_seen |= provenance.endswith('screener')
        selected.append((url, provenance))
    return selected


class Checker:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.out) / args.ticker / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        (self.root / 'raw').mkdir(parents=True)
        (self.root / 'documents').mkdir()
        self.report = {'ticker': args.ticker, 'as_of': args.as_of.isoformat(),
                       'checks': [], 'documents': [], 'manual_inputs': args.document,
                       'limitations': ['No OCR or validated financial-table extraction.',
                                       'Document company, reporting period and accounting basis require review.',
                                       'Official investor-relations website crawling is not implemented.',
                                       'HTTP success is not a guarantee of future availability.']}

    def check(self, name, status, detail, **extra):
        self.report['checks'].append(dict(name=name, status=status, detail=detail, **extra))

    def fetch(self, name, url, *, max_bytes=80 * 1024 * 1024, attempts=2, open_url=None):
        """Save exact decompressed-by-server response bytes and metadata, including failures."""
        open_url = open_url or urlopen
        base = self.root / 'raw' / name
        meta = {'requested_url': url, 'attempts': []}
        for attempt in range(attempts):
            stamp = datetime.now(timezone.utc).isoformat()
            try:
                req = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'})
                with open_url(req, timeout=self.args.timeout) as response:
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise ValueError(f'Response exceeds {max_bytes:,}-byte limit')
                    entry = dict(retrieved_at=stamp, status=response.status,
                                 final_url=response.geturl(), headers=dict(response.headers))
                entry.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                meta['attempts'].append(entry)
                base.with_suffix('.body').write_bytes(body)
                base.with_suffix('.metadata.json').write_text(json.dumps(meta, indent=2))
                print(f'HTTP {entry["status"]}: {name}', flush=True)
                return body
            except Exception as exc:
                entry = {'retrieved_at': stamp, 'error': f'{type(exc).__name__}: {exc}'}
                if isinstance(exc, HTTPError):
                    # A failure response can also be unexpectedly large.
                    try:
                        body = exc.read(min(max_bytes, 64 * 1024))
                    except OSError:
                        body = b''
                    finally:
                        exc.close()
                    entry.update(status=exc.code, headers=dict(exc.headers),
                                 sha256=hashlib.sha256(body).hexdigest())
                    base.with_suffix('.error.body').write_bytes(body)
                meta['attempts'].append(entry)
                if isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                    break
                if attempt + 1 < attempts:
                    time.sleep(1)
        base.with_suffix('.metadata.json').write_text(json.dumps(meta, indent=2))
        print(f'FAILED: {name}: {entry["error"]}', flush=True)
        return None

    def document(self, kind, url, provenance, index, *, max_pages=None, max_text_chars=None):
        # Some IR sites wrap a direct PDF in ?pdf=/path/to/file.pdf.
        viewer_pdf = parse_qs(urlparse(url).query).get('pdf', [])
        if viewer_pdf and '.pdf' in viewer_pdf[0].lower():
            url = urljoin(url, viewer_pdf[0])
            provenance += ':viewer_query'
        name = f'{kind}_{index:02d}'
        doc = dict(kind=kind, url=url, provenance=provenance, download='failed',
                   extraction='not_attempted', validation='needs_validation')
        self.report['documents'].append(doc)
        body = self.fetch(name, url)
        if body is None:
            return False, []
        if not body.lstrip().startswith(b'%PDF-'):
            doc['error'] = 'Response is not PDF bytes (may be an HTML viewer).'
            return False, []
        pdf = self.root / 'documents' / f'{name}.pdf'
        pdf.write_bytes(body)
        doc.update(download='succeeded', pdf=str(pdf.relative_to(self.root)),
                   sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
        try:
            reader = PdfReader(io.BytesIO(body))
            if max_pages is not None and len(reader.pages) > max_pages:
                raise ValueError(f'PDF exceeds {max_pages} pages')
            texts, embedded = [], []
            total_chars = 0
            for page in reader.pages:
                text = page.extract_text() or ''
                total_chars += len(text)
                if max_text_chars is not None and total_chars > max_text_chars:
                    raise ValueError('Extracted text exceeds the character limit')
                texts.append(text)
                for annotation in page.get('/Annots', []):
                    action = annotation.get_object().get('/A', {})
                    if action and action.get('/URI'):
                        embedded.append(str(action['/URI']))
            chars = sum(len(t.strip()) for t in texts)
            output = '\n'.join(f'\n--- PDF PAGE {i + 1} ---\n{t}' for i, t in enumerate(texts))
            text_path = pdf.with_suffix('.txt')
            text_path.write_text(output, encoding='utf-8')
            doc.update(pages=len(texts), text_chars=chars,
                       nonempty_pages=sum(bool(t.strip()) for t in texts),
                       text=str(text_path.relative_to(self.root)),
                       extraction='succeeded' if chars >= 300 else 'needs_validation',
                       preview=' '.join(output.split())[:500])
            # Link extraction is bounded to one extra PDF, not a general website crawler.
            embedded += re.findall(r'https?://[^\s<>]+', '\n'.join(texts))
            embedded = list(dict.fromkeys(u.rstrip(').,;') for u in embedded))
            doc['embedded_urls'] = embedded
            return chars >= 300, embedded
        except Exception as exc:
            doc.update(extraction='failed', error=f'{type(exc).__name__}: {exc}')
            return False, []

    def run(self):
        a, symbol = self.args, self.args.ticker
        start = a.as_of - timedelta(days=400)
        queries = {
            'securities': NSE_SECURITIES_URL,
            'announcements': announcements_url(symbol, start, a.as_of),
            'yahoo': f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS?range=3mo&interval=1d',
            'screener': f'https://www.screener.in/company/{symbol}/consolidated/',
            'holidays': NSE_HOLIDAYS_URL,
        }
        with ThreadPoolExecutor(max_workers=5) as pool:
            bodies = dict(zip(queries, pool.map(lambda key: self.fetch(key, queries[key]), queries)))
        master_isin, filings, links, holidays = '', [], [], set()
        for key, body in bodies.items():
            if body is None:
                self.check(key, 'failed', 'See raw HTTP metadata.'); continue
            try:
                if key == 'securities':
                    matches = [r for r in csv_rows(body) if r.get('SYMBOL') == symbol]
                    if not matches:
                        raise ValueError('Ticker absent from downloaded securities list')
                    self.report['security'] = matches
                    master_isin = matches[0].get('ISIN NUMBER', '')
                    self.check(key, 'succeeded', f'{len(matches)} matching securities', isin=master_isin)
                elif key == 'announcements':
                    data = json.loads(body)
                    if not isinstance(data, list):
                        raise ValueError('Expected a JSON array')
                    filings = [r for r in data if r.get('symbol') == symbol]
                    self.report['filings'] = filings
                    self.check(key, 'succeeded' if filings else 'needs_validation',
                               f'{len(filings)} symbol-matching records; {len(data)-len(filings)} others rejected')
                elif key == 'screener':
                    parser = Links(); parser.feed(body.decode('utf-8', errors='replace'))
                    links = [(label, urljoin(queries[key], href)) for label, href in parser.links]
                    self.check(key, 'succeeded', f'{len(links)} links parsed; discovery only')
                elif key == 'holidays':
                    data = json.loads(body)
                    for row in data.get('CM', []):
                        holidays.add(datetime.strptime(row['tradingDate'], '%d-%b-%Y').date())
                    self.check(key, 'succeeded' if holidays else 'needs_validation',
                               f'{len(holidays)} CM holidays; only years actually present are covered')
                elif key == 'yahoo':
                    data = json.loads(body)['chart']['result'][0]
                    if data['meta']['symbol'] != symbol + '.NS':
                        raise ValueError('Returned symbol does not match input')
                    quote = data['indicators']['quote'][0]
                    rows = [dict(date=datetime.fromtimestamp(ts, ZoneInfo('Asia/Kolkata')).date().isoformat(),
                                 close=quote['close'][i], volume=quote['volume'][i])
                            for i, ts in enumerate(data['timestamp'])]
                    eligible = [r for r in rows if r['date'] <= a.as_of.isoformat()]
                    valid = [r for r in eligible if r['close'] is not None and r['volume'] is not None]
                    self.report['prices'] = rows
                    self.check(key, 'succeeded' if valid else 'failed',
                               f'{len(valid)} complete rows; {len(eligible)-len(valid)} incomplete rows excluded',
                               latest_valid=valid[-1] if valid else None)
                    if len(valid) != len(eligible):
                        self.check('price_completeness', 'needs_validation', 'Do not alert from incomplete Yahoo rows.')
            except Exception as exc:
                self.check(key, 'failed', f'Parse/validation error: {exc}')

        feed_isins = sorted({r['sm_isin'] for r in filings if r.get('sm_isin')})
        all_isins = list(dict.fromkeys(([master_isin] if master_isin else []) + feed_isins))
        self.check('isin_consistency', 'succeeded' if master_isin and feed_isins == [master_isin]
                   else 'needs_validation', 'Compare source fields; never silently overwrite a conflict.',
                   master_isin=master_isin, announcement_isins=feed_isins,
                   checksums={value: isin_valid(value) for value in all_isins})

        dates = list(dict.fromkeys([a.as_of] + a.audit_date))
        with ThreadPoolExecutor(max_workers=3) as pool:
            csv_bodies = list(pool.map(lambda d: self.fetch('bhav_' + d.strftime('%Y%m%d'),
                bhavcopy_url(d)), dates))
        hashes = {}
        for day, body in zip(dates, csv_bodies):
            name = 'bhav_' + day.isoformat()
            if body is None:
                self.check(name, 'failed', 'Unavailable; this alone does not prove a market holiday.'); continue
            try:
                rows = csv_rows(body)
                embedded_dates = sorted({r.get('DATE1', '') for r in rows})
                matches = [r for r in rows if r.get('SYMBOL') == symbol and r.get('SERIES') == 'EQ']
                equal = embedded_dates == [day.strftime('%d-%b-%Y')]
                digest = hashlib.sha256(body).hexdigest()
                if digest in hashes:
                    self.check('identical_bhav_bodies', 'needs_validation',
                               f'{day} and {hashes[digest]} returned identical bytes; preserve embedded dates.')
                hashes[digest] = day.isoformat()
                self.check(name, 'succeeded' if equal and matches else 'needs_validation',
                           'Use embedded trade date, never filename date.', requested_date=day.isoformat(),
                           embedded_dates=embedded_dates, matching_rows=matches,
                           listed_holiday=day in holidays, sha256=digest)
            except Exception as exc:
                self.check(name, 'failed', str(exc))

        candidates = {'annual': [], 'transcript': [], 'filing': []}
        for item in a.document:
            kind, sep, url = item.partition('=')
            if not sep or kind not in candidates or urlparse(url).scheme != 'https':
                raise ValueError('--document must be annual=https://... or transcript=https://... or filing=https://...')
            candidates[kind].append((url, 'manual_url'))
        filings.sort(key=lambda row: row.get('sort_date', row.get('an_dt', '')), reverse=True)
        add_document_candidates(candidates, links, filings)
        self.report['candidates'] = candidates
        for kind, choices in candidates.items():
            selected = select_document_candidates(choices)
            succeeded = False
            for i, (url, provenance) in enumerate(selected[:a.max_candidates], 1):
                ok, embedded = self.document(kind, url, provenance, i)
                if ok and kind in ('annual', 'transcript'):
                    # Reject short cover letters, even when their text is perfectly readable.
                    minimum_pages = 30 if kind == 'annual' else 3
                    if self.report['documents'][-1].get('pages', 0) < minimum_pages:
                        self.report['documents'][-1]['content_note'] = 'Possible cover letter, not full source.'
                        nested = [u for u in embedded if '.pdf' in u.lower() and
                                  re.search(r'annual|(?:^|[/_-])ar[-_]|report|transcript|earningscall', u, re.I)]
                        ok = False
                        for j, target in enumerate(nested[:2], 1):
                            ok, _ = self.document(kind, target, provenance + ':embedded_pdf_link', i * 100 + j)
                            if ok and self.report['documents'][-1].get('pages', 0) >= minimum_pages:
                                break
                            ok = False
                if ok:
                    succeeded = True; break
            self.check(kind + '_content', 'needs_validation' if succeeded else 'failed',
                       'Readable text obtained; review company, period, tables and completeness.' if succeeded
                       else 'No usable PDF obtained within candidate limit; manual IR discovery may be needed.')
        self.finish()

    def finish(self):
        self.report['completed_at'] = datetime.now(timezone.utc).isoformat()
        (self.root / 'report.json').write_text(json.dumps(self.report, indent=2), encoding='utf-8')
        lines = [f'# Data checks: {self.args.ticker}', '', f'As of: {self.args.as_of}', '',
                 '| Check | Status | Detail |', '|---|---|---|']
        for check in self.report['checks']:
            lines.append(f'| {check["name"]} | {check["status"]} | {check["detail"].replace("|", "/")} |')
        lines += ['', '## Documents', '']
        for doc in self.report['documents']:
            lines.append(f'- {doc["kind"]}: download={doc["download"]}; extraction={doc["extraction"]}; '
                         f'validation={doc["validation"]}; provenance={doc["provenance"]}; URL: {doc["url"]}')
        lines += ['', '## Limits', ''] + ['- ' + s for s in self.report['limitations']]
        (self.root / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'REPORT: {self.root / "report.md"}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ticker', help='NSE symbol, e.g. INFY or MARUTI; optional .NS suffix')
    parser.add_argument('--as-of', type=date.fromisoformat,
                        default=datetime.now(ZoneInfo('Asia/Kolkata')).date() - timedelta(days=1))
    parser.add_argument('--audit-date', type=date.fromisoformat, action='append', default=[])
    parser.add_argument('--out', default='data_checks')
    parser.add_argument('--timeout', type=int, default=20)
    parser.add_argument('--max-candidates', type=int, default=3)
    parser.add_argument('--document', action='append', default=[], metavar='KIND=HTTPS_URL')
    args = parser.parse_args()
    args.ticker = args.ticker.upper().removesuffix('.NS')
    if not re.fullmatch(r'[A-Z0-9&_-]+', args.ticker):
        parser.error('Use an NSE ticker, not a company name or URL.')
    checker = Checker(args)
    try:
        checker.run()
    except Exception as exc:
        checker.check('unexpected_error', 'failed', f'{type(exc).__name__}: {exc}')
        checker.finish()
        raise


if __name__ == '__main__':
    main()
