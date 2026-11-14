import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def connect():
    path = Path(os.environ.get('FINN_DB', str(ROOT / '.finn' / 'finn.sqlite3'))).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS companies (
      symbol TEXT PRIMARY KEY, name TEXT NOT NULL, sector TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS documents (
      sha TEXT PRIMARY KEY, path TEXT, extraction TEXT NOT NULL, note TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pages (
      sha TEXT REFERENCES documents(sha), page INTEGER NOT NULL, text TEXT NOT NULL,
      PRIMARY KEY (sha, page));
    CREATE TABLE IF NOT EXISTS market_snapshots (
      symbol TEXT PRIMARY KEY REFERENCES companies(symbol), trading_date TEXT NOT NULL,
      fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS market_attempts (
      symbol TEXT PRIMARY KEY REFERENCES companies(symbol), attempted_at TEXT NOT NULL,
      status TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS research_sources (
      symbol TEXT NOT NULL, kind TEXT NOT NULL, sha TEXT NOT NULL REFERENCES documents(sha),
      url TEXT NOT NULL, provenance TEXT NOT NULL, date_label TEXT, document_date TEXT,
      run_date TEXT NOT NULL, PRIMARY KEY (symbol, kind, sha));
    CREATE TABLE IF NOT EXISTS research_status (
      symbol TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, run_date TEXT NOT NULL,
      PRIMARY KEY (symbol, kind));
    CREATE TABLE IF NOT EXISTS research_coverage (
      sha TEXT PRIMARY KEY REFERENCES documents(sha), total_pages INTEGER, publisher TEXT);
    CREATE TABLE IF NOT EXISTS events (
      id TEXT PRIMARY KEY, symbol TEXT REFERENCES companies(symbol), exchange_id TEXT,
      source_url TEXT NOT NULL, subject TEXT NOT NULL, description TEXT NOT NULL,
      filed_at TEXT, raw_date TEXT NOT NULL, category TEXT NOT NULL, priority INTEGER NOT NULL,
      rule TEXT NOT NULL, reason TEXT NOT NULL, document_sha TEXT REFERENCES documents(sha),
      identity_note TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL,
      UNIQUE(symbol, source_url));
    CREATE INDEX IF NOT EXISTS events_order ON events(priority DESC, filed_at DESC);
    CREATE TABLE IF NOT EXISTS imports (
      path TEXT PRIMARY KEY, imported_at TEXT NOT NULL, as_of TEXT, note TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS refresh_runs (
      id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
      from_date TEXT NOT NULL, to_date TEXT NOT NULL, company_count INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS refresh_results (
      run_id INTEGER NOT NULL REFERENCES refresh_runs(id), symbol TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')),
      fetched_at TEXT, filing_count INTEGER NOT NULL, detail TEXT NOT NULL,
      PRIMARY KEY (run_id, symbol));
    CREATE INDEX IF NOT EXISTS refresh_results_symbol ON refresh_results(symbol, run_id DESC);
    ''')
    for company in portfolio():
        db.execute('INSERT OR IGNORE INTO companies VALUES (?, ?, ?)',
                   (company['symbol'], company['name'], company['sector']))
    db.commit()
    return db


def portfolio():
    import json
    return json.loads((ROOT / 'finn_app' / 'data' / 'portfolio.json').read_text())
