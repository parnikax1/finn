"""Load the bundled review snapshot into the runtime SQLite database."""
import hashlib
import json
from contextlib import closing
from decimal import Decimal

from .db import ROOT, connect
from .import_cache import import_report
from .market import calculate, store_snapshot
from .evidence import suggested_question
from .rules import matching_contacts


SNAPSHOT = ROOT / 'demo' / 'snapshot.json'
WIPRO_FILE = 'Wipro_Secretarial_06112026190358_SEintimation06112026.pdf'
WIPRO_RELEASE = 'Wipro_Secretarial_06112026191442_PressRelease06112026.pdf'
MARUTI_QUESTION = 'What did Maruti say about exports and production capacity?'
CUMMINS_QUESTION = 'How did domestic sales and exports change?'


def wipro_brief(db, event_id, demo):
    if not demo or not demo['wipro_ready'] or event_id != demo['wipro']:
        return None
    event = db.execute('SELECT e.*, c.name, c.sector FROM events e JOIN companies c '
                       'ON c.symbol=e.symbol WHERE e.id=?', (event_id,)).fetchone()
    event = dict(event)
    contacts = matching_contacts(event['symbol'], event['sector'])
    contact = contacts[0] if contacts else None
    if contact:
        contact['question'] = suggested_question(event, contact)
    release = db.execute('SELECT source_url FROM events WHERE id=?', (demo['wipro_release'],)).fetchone()
    effective_date = '8 December 2026'
    return dict(
        id=event_id, title='Angan Guha returns to lead Wipro’s Americas 2',
        company=event['name'], filed_at=event['filed_at'], effective_date=effective_date,
        appointment=f'President and CEO of Americas 2 from {effective_date}.',
        context='He previously led the unit. That makes continuity in client delivery a useful question for the first quarter.',
        pages=[dict(page=2, label='Appointment and effective date'), dict(page=3, label='Previous Americas 2 role')],
        contact=contact, source_url=event['source_url'], release_id=demo['wipro_release'], release_url=release['source_url'] if release else None,
    )


def seed(db, snapshot, market=None):
    version = snapshot['version']
    marker = 'demo:' + version
    source_note = 'Bundled demo excerpt from the original PDF. Only selected pages are included.'
    snapshot_time = snapshot['captured_at']
    # The SQLite write transaction serializes concurrent starts of the same demo.
    with db:
        new_snapshot = db.execute(
            'INSERT OR IGNORE INTO imports VALUES (?,?,?,?)',
            (marker, snapshot_time, snapshot['to_date'], 'Bundled demo snapshot'),
        ).rowcount
        if new_snapshot:
            for report in snapshot['filings']:
                import_report(db, SNAPSHOT, data=report)
        # Reapply bundled excerpts on startup to add pages to an existing snapshot.
        for document in snapshot['documents']:
            # Use a separate identity for selected pages. A later full PDF
            # import must not mistake the demo excerpts for complete text.
            key = hashlib.sha256(f"demo:2026-11-12-v1:{document['sha']}".encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO documents VALUES (?,NULL,?,?)',
                       (key, 'ready', source_note))
            db.executemany('INSERT OR IGNORE INTO pages VALUES (?,?,?)',
                           ((key, int(page), text) for page, text in document['pages'].items()))
            if document['kind'] == 'filing':
                db.execute('UPDATE events SET document_sha=COALESCE(document_sha, ?) '
                           'WHERE symbol=? AND source_url=?',
                           (key, document['symbol'], document['url']))
            else:
                db.execute('INSERT OR IGNORE INTO research_sources VALUES (?,?,?,?,?,?,?,?)',
                           (document['symbol'], document['kind'], key, document['url'],
                            'Bundled demo snapshot', document['date_label'], document['document_date'],
                            snapshot_time))
                db.execute('INSERT OR IGNORE INTO research_coverage VALUES (?,?,?)',
                           (key, document.get('total_pages'), document.get('publisher')))
        if new_snapshot and market:
            settings = market['settings']
            verified_at = market['verified_at']
            for symbol, report in market['symbols'].items():
                if not db.execute('SELECT 1 FROM market_snapshots WHERE symbol=?', (symbol,)).fetchone():
                    signals = calculate(report['rows'], Decimal(settings['price_pct']),
                                        Decimal(settings['volume_multiple']))
                    store_snapshot(db, symbol, signals, verified_at)
                db.execute('INSERT OR IGNORE INTO market_attempts VALUES (?,?,?,?)',
                           (symbol, verified_at, report['status'],
                            'Bundled verified refresh. ' + report['detail']))
    return bool(new_snapshot)


def demo_links(db):
    marker = db.execute("SELECT as_of FROM imports WHERE path LIKE 'demo:%' ORDER BY as_of DESC LIMIT 1").fetchone()
    if not marker:
        return None
    wipro = db.execute("""
        SELECT id, (SELECT COUNT(*) FROM pages WHERE sha=events.document_sha AND page IN (2, 3)) AS cited_pages
        FROM events WHERE symbol='WIPRO' AND source_url LIKE ?
    """, ('%/' + WIPRO_FILE,)).fetchone()
    release = db.execute("SELECT id FROM events WHERE symbol='WIPRO' AND source_url LIKE ?",
                         ('%/' + WIPRO_RELEASE,)).fetchone()
    return dict(date=marker['as_of'], wipro=wipro['id'] if wipro else None,
                wipro_ready=bool(wipro and wipro['cited_pages'] == 2),
                wipro_release=release['id'] if release else None)


def capacity_walkthrough(db):
    rows = db.execute(
        "SELECT kind, url, date_label, sha FROM research_sources "
        "WHERE symbol='MARUTI' AND provenance='Bundled demo snapshot' ORDER BY run_date DESC"
    )
    sources = {}
    for row in rows:
        sources.setdefault(row['kind'], dict(row))
    citations = (
        ('transcript', 3, 'During the first half of this year,', '250,000 units of annual capacity.'),
        ('annual', 68, 'the Company increased its annual', 'annual production capacity to 4 million units.'),
    )
    for kind, page, start, end in citations:
        if kind in sources:
            row = db.execute('SELECT text FROM pages WHERE sha=? AND page=?', (sources[kind]['sha'], page)).fetchone()
            text = ' '.join(row['text'].split()) if row else ''
            left = text.find(start)
            right = text.find(end, left) if left >= 0 else -1
            sources[kind].update(page=page, excerpt=text[left:right + len(end)] if right >= 0 else '')
    return sources if {'annual', 'transcript'} <= sources.keys() else None



def main():
    snapshot = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    market = json.loads(SNAPSHOT.with_name('market-verification.json').read_text(encoding='utf-8'))
    with closing(connect()) as db:
        loaded = seed(db, snapshot, market)
    print(f"Demo snapshot {snapshot['version']} {'loaded' if loaded else 'already present'}.")


if __name__ == '__main__':
    main()
