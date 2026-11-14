from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from finn_app.db import connect
from finn_app.demo import MARUTI_QUESTION, SNAPSHOT, demo_links, seed
from finn_app.main import app


class DemoTests(unittest.TestCase):
    def test_snapshot_walkthrough_without_network_or_api_key(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', FINN_DB=str(Path(temp) / 'finn.sqlite3'), OPENAI_API_KEY=''
        ):
            with closing(connect()) as db:
                snapshot = json.loads(SNAPSHOT.read_text())
                market = json.loads(SNAPSHOT.with_name('market-verification.json').read_text())
                self.assertTrue(seed(db, snapshot, market))
                self.assertFalse(seed(db, snapshot))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], sum(len(item['filings']) for item in snapshot['filings']))
                self.assertIsNotNone(demo_links(db)['wipro'])
                filing = '/events/' + demo_links(db)['wipro']
            with TestClient(app) as client, patch('socket.create_connection', side_effect=AssertionError):
                feed = client.get('/').text
                self.assertIn('Demo snapshot · 2026-11-12 IST', feed)
                first_card = feed.split('<a class="event-card"', 1)[1].split('</a>', 1)[0]
                self.assertIn('Wipro appoints Angan Guha', first_card)
                event = client.get(filing).text
                for expected in ('physical PDF page 2', 'Meera Rao', 'December 8, 2026', '#page=2'):
                    self.assertIn(expected, event)
                research = client.get('/research', params={'symbol': 'MARUTI', 'question': MARUTI_QUESTION}).text
                for value in ('Prepared walkthrough', '500,000 units/year', '4 million units/year',
                              '1.1 million units/year', '#page=3', '#page=68', 'Who could add context'):
                    self.assertIn(value, research)
                self.assertIn('#page=68', research)
                self.assertIn('Asha Sen', research)
                self.assertNotIn('Answer from excerpts', research)
                market = client.get('/', params={'company': 'MARUTI'}).text
                self.assertIn('+1.99%', market)
                self.assertIn('1.88×', market)
                latest = client.get('/', params={'window': '1'}, headers={'HX-Request': 'true'}).text
                self.assertIn('2026-11-12 to 2026-11-12 IST', latest)
                self.assertNotIn('Angan Guha', latest)
                self.assertIn('15/15 succeeded', latest)
                self.assertIn('TMPV', latest)
                self.assertIn('Angan Guha', client.get('/', params={'window': 'all'}).text)
                search = client.get('/research', params={'symbol': 'CUMMINSIND', 'question': 'Exports?'}).text
                self.assertIn('Fetch documents', search)

    def test_public_render_origin_is_accepted(self):
        params = {'symbol': 'CUMMINSIND', 'question': 'Exports?'}
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', FINN_DB=str(Path(temp) / 'finn.sqlite3'),
            RENDER_EXTERNAL_URL='https://finn-example.onrender.com'
        ), patch('finn_app.main.start_fetch', return_value=True) as start:
            with TestClient(app) as client:
                route = '/research/fetch'
                good = client.post(route, params=params, headers={'Origin': 'https://finn-example.onrender.com'},
                                   follow_redirects=False)
                self.assertEqual(good.status_code, 303)
                other = client.post(route, params=params, headers={'Origin': 'https://example.org'},
                                    follow_redirects=False)
                self.assertEqual(other.status_code, 403)
                start.assert_called_once()

    def test_cummins_transcript_leads_when_table_rows_are_mixed(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', FINN_DB=str(Path(temp) / 'finn.sqlite3'), OPENAI_API_KEY=''
        ):
            with closing(connect()) as db, db:
                for kind, page, text in (
                    ('annual', 13, 'Domestic sales\n% Var\n19%\nExports\n% Var\n12%'),
                    ('transcript', 3, 'Domestic sales rose. Exports were flat.'),
                ):
                    db.execute("INSERT INTO documents VALUES (?,NULL,'ready','')", (kind,))
                    db.execute('INSERT INTO pages VALUES (?,?,?)', (kind, page, text))
                    db.execute('INSERT INTO research_sources VALUES (?,?,?,?,?,?,?,?)',
                               ('CUMMINSIND', kind, kind, f'https://example.org/{kind}.pdf',
                                'automatic:nse', 'Period label', '2026', '2026-11-14'))
            with TestClient(app) as client:
                html = client.get('/research', params={'symbol': 'CUMMINSIND',
                                                       'question': 'How did domestic sales and exports change?'}).text
                self.assertLess(html.index('Earnings-call transcript'), html.index('Annual report'))
                self.assertIn('transcript.pdf#page=3', html)
                self.assertNotIn('annual.pdf#page=13', html)
                self.assertIn('mixed row labels', html)
                self.assertNotIn('Prepared walkthrough', html)
