"""Refresh portfolio end-of-day signals from NSE's security-wise cash archive."""
import argparse
import fcntl
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

from check_data import Checker, NSE_HOLIDAYS_URL, bhavcopy_url, csv_rows
from .db import connect, portfolio
from .refresh import IST, fetch


def completed_through(now):
    local = now.astimezone(IST)
    return local.date() - timedelta(days=local.time() < time(20))


def trading_sessions(body, as_of, count):
    if not body:
        raise ValueError('NSE trading calendar unavailable')
    calendar = json.loads(body)
    if not isinstance(calendar, dict) or not isinstance(calendar.get('CM'), list):
        raise ValueError('Unexpected NSE trading calendar')
    closed, special, years = set(), set(), set()
    for item in calendar['CM']:
        day = datetime.strptime(item['tradingDate'], '%d-%b-%Y').date()
        description = str(item.get('description') or '').casefold()
        years.add(day.year)
        if 'muhurat' in description or ('diwali laxmi pujan' in description and '*' in description):
            special.add(day)
        else:
            closed.add(day)
    sessions, day = [], as_of
    while len(sessions) < count:
        if day.year not in years:
            raise ValueError(f'NSE calendar coverage is unavailable for {day.year}')
        if day in special or (day.weekday() < 5 and day not in closed):
            sessions.append(day)
        day -= timedelta(days=1)
    return sessions


def dated_rows(body, day, symbols):
    if not body:
        raise ValueError('NSE daily archive unavailable')
    rows = csv_rows(body)
    dates = {row.get('DATE1') for row in rows}
    expected = day.strftime('%d-%b-%Y')
    if dates != {expected}:
        actual = ', '.join(sorted(str(value) for value in dates)) or 'none'
        raise ValueError(f'NSE CSV date mismatch (expected {expected}, received {actual})')
    result = {}
    for row in rows:
        symbol = row.get('SYMBOL')
        if symbol not in symbols or row.get('SERIES') != 'EQ':
            continue
        if symbol in result:
            result[symbol] = None
            continue
        try:
            close, volume = Decimal(row['CLOSE_PRICE']), int(row['TTL_TRD_QNTY'])
            if not close.is_finite() or close <= 0 or volume <= 0:
                raise ValueError('Incomplete cash row')
        except (InvalidOperation, KeyError, ValueError):
            result[symbol] = None
            continue
        result[symbol] = dict(date=day.isoformat(), close=str(close), volume=volume,
                              source_url=bhavcopy_url(day), raw_file=f'raw/bhav_{day:%Y%m%d}.body',
                              source_row=row)
    return result


def calculate(rows, price_pct, volume_multiple):
    current, previous, *_ = rows
    price, volume = None, None
    if current and previous:
        current_close, previous_close = Decimal(current['close']), Decimal(previous['close'])
        delta = current_close - previous_close
        # Compare with the unrounded threshold. Only round for presentation.
        move = delta * 100 / previous_close
        price = dict(triggered=abs(delta) * 100 >= price_pct * previous_close, move=str(move),
                     threshold=str(price_pct), rows=[current, previous])
    if all(rows):
        prior = rows[1:]
        total = sum(row['volume'] for row in prior)
        mean = Decimal(total) / len(prior)
        multiple = Decimal(current['volume'] * len(prior)) / total
        volume = dict(triggered=current['volume'] * len(prior) >= volume_multiple * total,
                      multiple=str(multiple), mean=str(mean), threshold=str(volume_multiple), rows=rows)
    return dict(price=price, volume=volume)


def store_snapshot(db, symbol, results, fetched_at):
    previous = db.execute('SELECT payload FROM market_snapshots WHERE symbol=?', (symbol,)).fetchone()
    snapshot = json.loads(previous['payload']) if previous else {}
    changed = False
    for kind in ('price', 'volume'):
        result = results[kind]
        saved = snapshot.get(kind)
        if result and (not saved or result['rows'][0]['date'] >= saved['rows'][0]['date']):
            snapshot[kind] = result
            changed = True
    if changed:
        latest = max(result['rows'][0]['date'] for result in snapshot.values())
        db.execute("""
            INSERT INTO market_snapshots VALUES (?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET trading_date=excluded.trading_date,
                fetched_at=excluded.fetched_at, payload=excluded.payload
        """, (symbol, latest, fetched_at, json.dumps(snapshot)))


def read_market(db, company):
    return [dict(row) | {'snapshot': json.loads(row['payload']) if row['payload'] else {}}
            for row in db.execute('''
                SELECT c.symbol, c.name, s.trading_date, s.fetched_at, s.payload,
                       a.attempted_at, a.status, a.detail
                FROM companies c
                LEFT JOIN market_snapshots s ON s.symbol=c.symbol
                LEFT JOIN market_attempts a ON a.symbol=c.symbol
                WHERE (s.symbol IS NOT NULL OR a.symbol IS NOT NULL) AND (?='' OR c.symbol=?)
                ORDER BY s.trading_date DESC, c.symbol
            ''', (company, company))]


def refresh_market(db, symbols, as_of, price_pct, volume_multiple, window, timeout):
    path = Path(db.execute('PRAGMA database_list').fetchone()['file'])
    stamp = datetime.now(timezone.utc)
    checker = Checker(SimpleNamespace(
        out=str(path.parent / 'market-runs' / path.name), ticker='portfolio',
        as_of=as_of, document=[], timeout=timeout))
    report = dict(as_of=as_of.isoformat(), source='NSE security-wise daily archive (EQ)',
                  settings=dict(price_pct=str(price_pct), volume_multiple=str(volume_multiple),
                                volume_window=window), sessions=[], failures={}, symbols={})
    archives = {}
    try:
        sessions = trading_sessions(fetch(checker, 'holidays', NSE_HOLIDAYS_URL, 4), as_of, window + 1)
    except (ValueError, TypeError, KeyError) as exc:
        sessions = []
        report['calendar_failure'] = str(exc)

    def archive(day):
        try:
            return dated_rows(fetch(checker, f'bhav_{day:%Y%m%d}', bhavcopy_url(day), 4), day, symbols), ''
        except (ValueError, UnicodeError) as exc:
            return {}, str(exc)

    with ThreadPoolExecutor(max_workers=5) as pool:
        for day, (rows, error) in zip(sessions, pool.map(archive, sessions)):
            archives[day] = rows
            if error:
                report['failures'][day.isoformat()] = error
    report['sessions'] = [day.isoformat() for day in sessions]

    attempted_at = stamp.isoformat()
    fetched_at = datetime.now(timezone.utc).isoformat()
    for symbol in symbols:
        if not sessions:
            entry = dict(status='failed', detail=report['calendar_failure'])
        else:
            rows = [archives[day].get(symbol) for day in sessions]
            missing = [day.isoformat() for day, row in zip(sessions, rows) if row is None]
            results = calculate(rows, price_pct, volume_multiple)
            if missing:
                detail = 'No usable EQ row for ' + ', '.join(missing) + '.'
                status = 'partial' if any(results.values()) else 'failed'
                entry = dict(status=status, detail=detail, missing_sessions=missing, **results)
            else:
                entry = dict(status='succeeded', detail=f'Checked session {sessions[0]}.', **results)
        report['symbols'][symbol] = entry

    report_path = checker.root / 'report.json'
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    with db:
        for symbol, entry in report['symbols'].items():
            if entry['status'] != 'failed':
                store_snapshot(db, symbol, entry, fetched_at)
            db.execute('''
                INSERT INTO market_attempts VALUES (?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET attempted_at=excluded.attempted_at,
                    status=excluded.status, detail=excluded.detail
            ''', (symbol, attempted_at, entry['status'], entry['detail']))
    for symbol, entry in report['symbols'].items():
        metrics = []
        if price := entry.get('price'):
            metrics.append(f"{Decimal(price['move']):+.2f}%")
        if volume := entry.get('volume'):
            metrics.append(f"{Decimal(volume['multiple']):.2f}× volume")
        if metrics:
            print(f"{symbol}: {sessions[0]} · " + ' · '.join(metrics))
        if entry['status'] != 'succeeded':
            print(f"{symbol}: {entry['status'].capitalize()}. {entry['detail']}")
    print(f'REPORT: {report_path}')
    return int(any(entry['status'] != 'succeeded' for entry in report['symbols'].values()))


def positive_decimal(value):
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError('enter a positive finite number') from None
    if not number.is_finite() or number <= 0:
        raise argparse.ArgumentTypeError('enter a positive finite number')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols', nargs='+', help='Portfolio tickers (defaults to the full portfolio)')
    parser.add_argument('--as-of', type=date.fromisoformat, help='Trading-date cutoff (IST)')
    parser.add_argument('--price-pct', default=Decimal('2'), type=positive_decimal)
    parser.add_argument('--volume-multiple', default=Decimal('2'), type=positive_decimal)
    parser.add_argument('--volume-window', default=10, type=int, help='Number of prior sessions (1–20)')
    parser.add_argument('--timeout', default=12, type=int, help='Network timeout in seconds (1–30)')
    args = parser.parse_args()
    allowed = [item['symbol'] for item in portfolio()]
    symbols = list(dict.fromkeys(item.upper() for item in args.symbols)) if args.symbols else allowed
    if any(symbol not in allowed for symbol in symbols):
        parser.error('Choose tickers from the configured portfolio.')
    if not 1 <= args.volume_window <= 20 or not 1 <= args.timeout <= 30:
        parser.error('Use --volume-window 1–20 and --timeout 1–30.')
    cutoff = completed_through(datetime.now(timezone.utc))
    if args.as_of and args.as_of > cutoff:
        parser.error(f'The latest permitted cutoff is {cutoff} IST (today is eligible after 20:00 IST).')
    with closing(connect()) as db:
        lock_path = Path(db.execute('PRAGMA database_list').fetchone()['file']).with_suffix('.market.lock')
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, 'A market refresh is already running for this database.\n')
            return refresh_market(db, symbols, args.as_of or cutoff, args.price_pct, args.volume_multiple,
                                  args.volume_window, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
