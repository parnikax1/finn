"""Load the bundled review snapshot into the runtime SQLite database."""
import hashlib
import json
from contextlib import closing
from decimal import Decimal

from .db import ROOT, connect
from .import_cache import import_report
from .market import calculate, store_snapshot


SNAPSHOT = ROOT / 'demo' / 'snapshot.json'
WIPRO_FILE = 'Wipro_Secretarial_06112026190358_SEintimation06112026.pdf'
MARUTI_QUESTION = 'What did Maruti say about exports and production capacity?'


def seed(db, snapshot):
    version = snapshot['version']
    marker = 'demo:' + version
    source_note = 'Bundled demo excerpt from the original PDF. Only selected pages are included.'
    snapshot_time = snapshot['captured_at']
    # The SQLite write transaction serializes concurrent starts of the same demo.
    with db:
        if not db.execute(
            'INSERT OR IGNORE INTO imports VALUES (?,?,?,?)',
            (marker, snapshot_time, snapshot['to_date'], 'Bundled demo snapshot'),
        ).rowcount:
            return False
        for report in snapshot['filings']:
            import_report(db, SNAPSHOT, data=report)
        for document in snapshot['documents']:
            # Use a separate identity for selected pages. A later full PDF
            # import must not mistake the demo excerpts for complete text.
            key = hashlib.sha256(f"{marker}:{document['sha']}".encode()).hexdigest()
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
        market = snapshot['market']
        if not db.execute('SELECT 1 FROM market_snapshots WHERE symbol=?', ('MARUTI',)).fetchone():
            signals = calculate(market['volume']['rows'], Decimal(market['price']['threshold']),
                                Decimal(market['volume']['threshold']))
            store_snapshot(db, 'MARUTI', signals, snapshot_time)
    return True


def demo_links(db):
    marker = db.execute("SELECT as_of FROM imports WHERE path LIKE 'demo:%' ORDER BY as_of DESC LIMIT 1").fetchone()
    if not marker:
        return None
    wipro = db.execute("SELECT id FROM events WHERE symbol='WIPRO' AND source_url LIKE ?",
                       ('%/' + WIPRO_FILE,)).fetchone()
    return dict(date=marker['as_of'], wipro=wipro['id'] if wipro else None)


def main():
    snapshot = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    with closing(connect()) as db:
        loaded = seed(db, snapshot)
    print(f"Demo snapshot {snapshot['version']} {'loaded' if loaded else 'already present'}.")


if __name__ == '__main__':
    main()
