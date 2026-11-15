from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from finn_app.db import connect
from finn_app.main import app
from finn_app.market import completed_through, dated_rows, refresh_market, trading_sessions


def calendar():
    return json.dumps({'CM': [
        {'tradingDate': '08-Nov-2026', 'description': 'Diwali Laxmi Pujan*'},
        {'tradingDate': '10-Nov-2026', 'description': 'Diwali-Balipratipada'},
    ]}).encode()


def csv_for(day, close, volume):
    return (f'SYMBOL,SERIES,DATE1,CLOSE_PRICE,TTL_TRD_QNTY\n'
            f'MARUTI,EQ,{day:%d-%b-%Y},{close},{volume}\n').encode()


class MarketTests(unittest.TestCase):
    def test_completed_cutoff_and_exchange_sessions(self):
        self.assertEqual(completed_through(datetime(2026, 11, 12, 13, tzinfo=timezone.utc)),
                         date(2026, 11, 11))
        self.assertEqual(completed_through(datetime(2026, 11, 12, 15, tzinfo=timezone.utc)),
                         date(2026, 11, 12))
        self.assertEqual(trading_sessions(calendar(), date(2026, 11, 11), 4),
                         [date(2026, 11, n) for n in (11, 9, 8, 6)])

    def test_replayed_archive_and_incomplete_row(self):
        archived = date(2026, 11, 9)
        with self.assertRaisesRegex(ValueError, 'date mismatch'):
            dated_rows(csv_for(archived, 100, 500), date(2026, 11, 10), ['MARUTI'])
        rows = dated_rows(csv_for(archived, '', 500), archived, ['MARUTI'])
        self.assertIsNone(rows['MARUTI'])

    def test_separate_baselines_and_cached_signal_on_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, FINN_DB=str(Path(temp) / 'test.sqlite')):
            data = {day: csv_for(date(2026, 11, day), close, volume) for day, close, volume in
                    [(12, 95, 300), (11, 105, 200), (9, 100, 100), (8, 97, 50)]}

            def request(checker, name, url, max_mib):
                return calendar() if name == 'holidays' else data.get(int(name[-2:]))

            with patch('finn_app.market.fetch', side_effect=request), closing(connect()) as db:
                self.assertEqual(refresh_market(db, ['MARUTI'], date(2026, 11, 11),
                                                Decimal('5'), Decimal('2'), 2, 1), 0)
                first = json.loads(db.execute('SELECT payload FROM market_snapshots').fetchone()['payload'])
                self.assertEqual(first['price']['rows'][1]['date'], '2026-11-09')
                self.assertEqual(Decimal(first['price']['move']), 5)
                self.assertTrue(first['price']['triggered'])
                self.assertEqual(Decimal(first['volume']['mean']), 75)
                self.assertTrue(first['volume']['triggered'])
                # Only the price comparison has enough rows for the next refresh.
                data[9] = csv_for(date(2026, 11, 9), 100, '')
                self.assertEqual(refresh_market(db, ['MARUTI'], date(2026, 11, 12),
                                                Decimal('5'), Decimal('2'), 2, 1), 1)
                cached = json.loads(db.execute('SELECT payload FROM market_snapshots').fetchone()['payload'])
                self.assertEqual(cached['price']['rows'][0]['date'], '2026-11-12')
                self.assertEqual(cached['volume'], first['volume'])
                # Missing immediate prior close must not bridge to another session.
                del data[11]
                self.assertEqual(refresh_market(db, ['MARUTI'], date(2026, 11, 12),
                                                Decimal('5'), Decimal('2'), 2, 1), 1)
                after = json.loads(db.execute('SELECT payload FROM market_snapshots').fetchone()['payload'])
                self.assertEqual(after, cached)
            with patch('finn_app.market.fetch') as network:
                response = TestClient(app).get('/', params={'company': 'MARUTI', 'view': 'brief'}, headers={'HX-Request': 'true'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('Move <strong>-9.52%</strong>', response.text)
                self.assertIn('<time datetime="2026-11-12">2026-11-12</time>', response.text)
                self.assertIn('<time datetime="2026-11-11">2026-11-11</time>', response.text)
                self.assertIn('prior 2-session mean 75.00 shares', response.text)
                self.assertIn('Latest refresh failed', response.text)
                self.assertIn('sec_bhavdata_full_09112026.csv', response.text)
                network.assert_not_called()
