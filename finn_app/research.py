"""Index checker documents separately from portfolio events, then search cached pages."""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
from contextlib import closing
from datetime import date
from itertools import groupby
import json
import math
from pathlib import Path
import re

from .db import ROOT, connect
from .evidence import DATE, calendar_date
from .import_cache import canonical_url, load_document, local_file


KINDS = {'annual': ('Annual report', 30), 'transcript': ('Earnings-call transcript', 3)}
SYMBOL = re.compile(r'[A-Z0-9&_-]{1,32}')
YEAR_RANGE = r'(20\d\d)\s*[-–—/]\s*(20\d\d|\d\d)\b'
REPORT_TITLE = r'(?:annual(?: integrated)? report|integrated annual report)'
PERIOD_STATEMENT = re.compile(
    rf'(?:\b{REPORT_TITLE}(?: of (?:the|your) company)? for (?:the )?'
    rf'(?:(?:financial year|fy)\s*)?|\breporting period\s*[:–-]?\s*(?:fy\s*)?){YEAR_RANGE}', re.I)
COVER_PERIOD = re.compile(
    rf'^\s*{REPORT_TITLE}\s+(?:(?:financial year|fy)\s*)?{YEAR_RANGE}|'
    rf'^\s*{YEAR_RANGE}\s+{REPORT_TITLE}\b', re.I | re.M)
DIRECTORS_PERIOD = re.compile(
    rf'\b(?:present|submit)\b[^.!?]{{0,65}}\b{REPORT_TITLE}\b[^.!?]{{0,140}}'
    rf'\bfor (?:the )?(?:financial )?year ended(?: on)?\s+({DATE})', re.I)
COMPOUND_MODIFIERS = {'based', 'driven', 'enabled', 'led', 'powered'}
COMPOUND = re.compile(
    r'\b([^\W_]+)\s*[-‐‑–—]\s*(' + '|'.join(sorted(COMPOUND_MODIFIERS)) + r')\b', re.I)
MEASUREMENT = re.compile(
    r'(?<!\w)(?:(?:₹|\$|€|£|INR|USD|EUR|GBP|Rs\.?)\s*)?'
    r'[−+-]?\d+(?:[.,]\d+)*(?:\s*[–-]\s*\d+(?:[.,]\d+)*)?'
    r'(?:\s+(?:hundred|thousand|million|billion|trillion|lakh|crore)s?\b)?'
    r'(?:\s*(?:%|×)|\s+(?:units?|vehicles?|cars?|tonnes?|tons?|kilograms?|kg|km|MWp?|'
    r'GW|kWh|MWh|litres?|liters?|shares?|employees?|people|bps|basis points?|percent|per cent)\b)?'
    r'(?:\s+per\s+(?:annum|year|month|day|hour|share|unit|cent)\b)?', re.I)
STOP = set('a about also an and are as at be by can could did do does for from had has have how i in into is it its of on or our say says said should that the their them there these they this to us was were what when where which who why will with would you your company management report transcript'.split())


def ticker(value):
    symbol = value.strip().upper().removesuffix('.NS')
    return symbol if SYMBOL.fullmatch(symbol) else None


def date_from_pages(kind, pages):
    if kind == 'transcript':
        for page in pages[:4]:
            heading = re.search(r'\b(?:earnings\s+(?:conference\s+)?call|conference\s+call\s+transcript)\b',
                                page, re.I)
            if not heading:
                continue
            for line in page[heading.start():heading.start() + 320].splitlines():
                line = re.sub(r'(?<=\d)(?:st|nd|rd|th)\b', '', line.strip(), flags=re.I)
                match = re.fullmatch(DATE, line.strip(' .'), re.I)
                if match and (day := calendar_date(match[0])):
                    return day, f'Call held {day}'
        return None, None
    stated = set()
    for index, page in enumerate(pages[:8]):
        statements = PERIOD_STATEMENT.findall(' '.join(page.split()))
        if index < 3:
            statements += [(a or c, b or d) for a, b, c, d in COVER_PERIOD.findall(page)]
        for start, end in statements:
            first = int(start)
            last = int(end) + (2000 if len(end) == 2 else 0)
            if last == first + 1:
                stated.add((first, last))
    if len(stated) > 1:
        return None, None
    if stated:
        first, last = stated.pop()
        return str(last), f'Reporting period {first}–{last}'

    # Some standalone reports have only a single year on the cover. Use the
    # directors' declaration where it explicitly names the audited year-end.
    endings = {day for page in pages for match in DIRECTORS_PERIOD.findall(' '.join(page.split()))
               if (day := calendar_date(match))}
    if len(endings) == 1:
        day = date.fromisoformat(endings.pop())
        return str(day.year), f'Financial year ended {day.strftime("%d %B %Y")}'
    return None, None


def indexed_date(db, sha, kind):
    limit = 4 if kind == 'transcript' else -1
    pages = [row['text'] for row in db.execute(
        'SELECT text FROM pages WHERE sha=? AND page>0 ORDER BY page LIMIT ?', (sha, limit))]
    return date_from_pages(kind, pages)


def revalidate_periods(db, symbol):
    sources = db.execute("SELECT sha FROM research_sources WHERE symbol=? AND kind='annual'",
                         (symbol,)).fetchall()
    for source in sources:
        sha = source['sha']
        year, label = indexed_date(db, sha, 'annual')
        db.execute("UPDATE research_sources SET document_date=?, date_label=? "
                   "WHERE symbol=? AND kind='annual' AND sha=?", (year, label, symbol, sha))


def download_failure(run, doc):
    if doc.get('error'):
        return doc['error']
    for path in sorted((run / 'raw').glob(f"{doc['kind']}_*.metadata.json")):
        try:
            metadata = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        attempts = metadata.get('attempts') or []
        if metadata.get('requested_url') == doc.get('url') and attempts:
            return attempts[-1].get('error') or 'Download failed; see checker report'
    return 'Download failed; see checker report'


def index_report(db, symbol, report_path, *, kinds=KINDS, prefer_cached_text=False):
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if ticker(str(report.get('ticker') or '')) != symbol:
        return
    run_date = report.get('completed_at') or report.get('as_of') or report_path.parent.name
    checks = {c['name']: c for c in report.get('checks', [])}
    for kind in kinds:
        _, minimum_pages = KINDS[kind]
        attempts = [doc for doc in report.get('documents', []) if doc['kind'] == kind]
        if not attempts and kind + '_content' not in checks:
            continue
        failures = []
        accepted = 0
        for doc in attempts:
            provenance = doc.get('provenance') or 'Cache provenance unavailable'
            if doc.get('download') != 'succeeded':
                failures.append(f'{provenance}: {download_failure(report_path.parent, doc)}')
                continue
            url = canonical_url(doc.get('url'))
            if doc.get('content_note') or doc.get('pages', 0) < minimum_pages:
                failures.append(f'{provenance}: PDF is too short to use as a full source (possible cover letter)')
                continue
            if doc.get('extraction') != 'succeeded':
                failures.append(f'{provenance}: usable PDF text extraction failed')
                continue
            if not url or not local_file(report_path.parent, doc.get('pdf')):
                failures.append(f'{provenance}: original URL or cached PDF missing')
                continue
            sha = load_document(db, report_path.parent, doc, prefer_cached_text=prefer_cached_text)
            if not sha or not db.execute('SELECT 1 FROM pages WHERE sha=? AND page>0 LIMIT 1',
                                         (sha,)).fetchone():
                failures.append(f'{provenance}: no usable text with physical PDF page references')
                continue
            day, label = indexed_date(db, sha, kind)
            db.execute('''INSERT INTO research_sources VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,kind,sha) DO UPDATE SET
                url=excluded.url, provenance=excluded.provenance, date_label=excluded.date_label,
                document_date=excluded.document_date, run_date=excluded.run_date
                WHERE excluded.run_date>=research_sources.run_date''',
                (symbol, kind, sha, url, provenance, label, day, run_date))
            accepted += 1
        detail = 'Indexed readable PDF evidence. ' if accepted else ''
        detail += ' · '.join(failures) if failures else (
            '' if accepted else checks.get(kind + '_content', {}).get(
                'detail', 'No candidate PDF was discovered within the checker’s source and candidate limits.'))
        db.execute('''INSERT INTO research_status VALUES (?,?,?,?)
            ON CONFLICT(symbol,kind) DO UPDATE SET detail=excluded.detail, run_date=excluded.run_date
            WHERE excluded.run_date>=research_status.run_date''', (symbol, kind, detail.strip(), run_date))


def search_words(value):
    # Match simple inflections, keeping the returned PDF text intact.
    return [word.removesuffix('s') if len(word) > 4 and word.endswith('s') and not word.endswith('ss')
            else word for word in re.findall(r'[^\W_]+', value.casefold())]


def query_features(question, symbol):
    words = search_words(question)
    stop = STOP | set(search_words(symbol))
    terms = set(words) - stop - COMPOUND_MODIFIERS
    heads = {search_words(match[1])[0] for match in COMPOUND.finditer(question)} & terms
    phrases = set()
    for relevant, group in groupby(words, key=lambda word: word not in stop):
        if not relevant:
            continue
        segment = list(group)
        for size in (2, 3):
            for start in range(len(segment) - size + 1):
                phrase = tuple(segment[start:start + size])
                # "led delivery" is too generic to stand in for "AI-led delivery".
                if phrase[0] in terms and (phrase[-1] in terms or phrase[0] in heads):
                    phrases.add(phrase)
    return terms, heads, phrases


def quantity_boundaries(text, word_starts):
    boundaries = {}
    for match in MEASUREMENT.finditer(text):
        first = bisect_right(word_starts, match.start()) - 1
        last = bisect_left(word_starts, match.end())
        for cut in range(first + 1, last):
            boundaries[cut] = (first, last)
    return boundaries


def passages(pages, terms, heads, phrases):
    candidates = []
    frequencies = Counter()
    for row in pages:
        text = ' '.join(row['text'].split())
        page_terms = set(search_words(text)) & terms
        if not page_terms:
            continue
        frequencies.update(page_terms)
        starts = [match.start() for match in re.finditer(r'\S+', text)] + [len(text)]
        length = len(starts) - 1
        protected = quantity_boundaries(text, starts)
        # Keep matches in context without spanning physical page boundaries.
        for start in range(0, length, 50):
            if start and start + 30 >= length:
                break
            stop = min(start + 80, length)
            first = protected.get(start, (start, start))[0]
            last = protected.get(stop, (stop, stop))[1]
            excerpt = text[starts[first]:starts[last]].strip()
            tokens = search_words(excerpt)
            matches = set(tokens) & terms
            if not matches:
                continue
            contiguous = {tuple(tokens[i:i + size]) for size in (2, 3)
                          for i in range(len(tokens) - size + 1)}
            # Prefer the longest phrase so overlapping bigrams don't double count.
            phrase_weight = max((len(phrase) - 1 for phrase in phrases & contiguous), default=0)
            candidates.append((row['page'], excerpt, matches, first > 0, last < length, phrase_weight))
    weights = {term: 1 + math.log((len(pages) + 1) / (count + 1))
               for term, count in frequencies.items()}
    candidates.sort(key=lambda c: (-len(c[2] & heads),
                                   -len(c[2]) - 0.75 * c[5],
                                   -sum(weights[t] for t in sorted(c[2])), c[0]))
    found = {}
    seen_text = set()
    for page, excerpt, matches, before, after, _ in candidates:
        if page in found or excerpt in seen_text:
            continue
        found[page] = {'page': page, 'text': ('… ' if before else '') + excerpt + (' …' if after else ''),
                       'terms': ', '.join(sorted(matches))}
        seen_text.add(excerpt)
        if len(found) == 3:
            break
    return list(found.values())


def lookup(db, symbol, question):
    terms, heads, phrases = query_features(question, symbol)
    groups = []
    for kind, (title, _) in KINDS.items():
        doc = db.execute('''SELECT * FROM research_sources WHERE symbol=? AND kind=?
            ORDER BY document_date DESC,
                     CASE WHEN provenance LIKE 'automatic:%' THEN 0 ELSE 1 END,
                     run_date DESC, sha LIMIT 1''', (symbol, kind)).fetchone()
        status = db.execute('SELECT detail FROM research_status WHERE symbol=? AND kind=?',
                            (symbol, kind)).fetchone()
        hits = []
        omitted_tables = 0
        if doc and terms:
            pages = db.execute('SELECT page,text FROM pages WHERE sha=? AND page>0 ORDER BY page',
                               (doc['sha'],)).fetchall()
            if symbol == 'CUMMINSIND' and kind == 'annual':
                # Extracted highlights interleave figures, labels and variances.
                prose_pages = [row for row in pages if len(re.findall(r'%\s*Var', row['text'], re.I)) < 2]
                omitted_tables = len(pages) - len(prose_pages)
                pages = prose_pages
            hits = passages(pages, terms, heads, phrases)
        groups.append(dict(title=title, doc=doc, hits=hits, status=status['detail'] if status else None,
                           omitted_tables=omitted_tables,
                           unknown_date='Period unverified' if kind == 'annual' else 'Document date unavailable'))
    if symbol == 'CUMMINSIND' and groups[0]['omitted_tables'] and groups[1]['hits']:
        groups.reverse()
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ticker')
    parser.add_argument('--cache', type=Path, action='append', help='Checker output directory or report.json')
    args = parser.parse_args()
    symbol = ticker(args.ticker)
    if symbol is None:
        parser.error('Enter an NSE ticker such as MARUTI.')
    reports = set()
    for root in args.cache or [ROOT / 'data_checks', ROOT / 'runs']:
        root = root.expanduser().resolve()
        if root.is_file():
            reports.add(root)
        elif root.name == symbol:
            reports.update(root.glob('*/report.json'))
        else:
            reports.update((root / symbol).glob('*/report.json'))
    failures = 0
    with closing(connect()) as db:
        if not reports and not db.execute('SELECT 1 FROM research_sources WHERE symbol=? LIMIT 1',
                                          (symbol,)).fetchone():
            parser.exit(1, f'No checker reports found for {symbol}. Run check_data.py first or supply --cache.\n')
        for path in sorted(reports):
            try:
                with db:
                    index_report(db, symbol, path)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                failures += 1
                print(f'{path}: {exc}')
        with db:
            revalidate_periods(db, symbol)
        for group in lookup(db, symbol, ''):
            print(f"{symbol} · {group['title']} · {group['status'] or 'No discovery run recorded'}")
            if group['doc']:
                print(f"  Searchable: {group['doc']['date_label'] or group['unknown_date']}")
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
