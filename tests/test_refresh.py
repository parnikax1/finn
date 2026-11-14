import io
import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import date
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from finn_app.db import connect
from finn_app.main import app
from finn_app.refresh import exchange_rows, refresh


START = date(2026, 11, 5)
END = date(2026, 11, 11)


def filing(symbol, sequence, subject, day='10-Nov-2026'):
    return {
        'symbol': symbol,
        'seq_id': sequence,
        'an_dt': f'{day} 11:56:00',
        'desc': subject,
        'attchmntFile': f'https://nsearchives.nseindia.com/corporate/{sequence}.pdf',
    }


class RefreshTests(unittest.TestCase):
    def test_priority_budget_failed_company_and_empty_success(self):
        files = {
            'MARUTI': [filing('MARUTI', 'appointment', 'Appointment'),
                       filing('MARUTI', 'routine', 'Trading window', '11-Nov-2026')],
            'TCS': [filing('TCS', 'penalty', 'Regulatory order')],
        }
        pdf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(pdf)
        downloads = []

        def fetch(checker, name, url, max_mib):
            if name == 'securities':
                return b'SYMBOL,ISIN NUMBER\n'
            if name == 'announcements':
                symbol = parse_qs(urlsplit(url).query)['symbol'][0]
                rows = files[symbol]
                return json.dumps(rows).encode() if rows is not None else None
            downloads.append(url)
            return pdf.getvalue()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, FINN_DB=os.path.join(directory, 'test.sqlite3')
        ), patch('finn_app.refresh.fetch', side_effect=fetch):
            with closing(connect()) as db:
                self.assertEqual(refresh(db, ['MARUTI', 'TCS'], START, END, 1, 1), 0)
                self.assertEqual(downloads, [files['MARUTI'][0]['attchmntFile']])
                cached = db.execute(
                    'SELECT id, filed_at, document_sha FROM events WHERE exchange_id=?',
                    ('appointment',)).fetchone()
                self.assertIsNotNone(cached['document_sha'])
                files['MARUTI'] = None
                files['TCS'] = []
                self.assertEqual(refresh(db, ['MARUTI', 'TCS'], START, END, 1, 1), 1)
                self.assertEqual(len(downloads), 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 3)
                after = db.execute(
                    'SELECT id, filed_at, document_sha FROM events WHERE exchange_id=?',
                    ('appointment',)).fetchone()
                self.assertEqual(tuple(cached), tuple(after))
                statuses = db.execute(
                    'SELECT symbol,status,filing_count FROM refresh_results WHERE run_id=2 ORDER BY symbol'
                ).fetchall()
                self.assertEqual([tuple(row) for row in statuses],
                                 [('MARUTI', 'failed', 0), ('TCS', 'succeeded', 0)])
            with TestClient(app) as client:
                page = client.get('/')
                self.assertIn('1 succeeded, 1 failed', page.text)
                self.assertIn('Earlier cache kept', page.text)
                self.assertIn('Last successful fetch', page.text)
                self.assertIn('2026-11-05 through 2026-11-11 IST', page.text)

    def test_rejects_stale_or_wrong_symbol_response(self):
        for record in (filing('MARUTI', 'stale', 'Appointment', '04-Nov-2026'),
                       filing('TCS', 'wrong', 'Appointment')):
            with self.assertRaises(ValueError):
                exchange_rows(json.dumps([record]).encode(), 'MARUTI', START, END)
