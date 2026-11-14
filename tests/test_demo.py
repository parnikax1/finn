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
                self.assertTrue(seed(db, snapshot))
                self.assertFalse(seed(db, snapshot))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 30)
                self.assertIsNotNone(demo_links(db)['wipro'])
                filing = '/events/' + demo_links(db)['wipro']
            with TestClient(app) as client, patch('socket.create_connection', side_effect=AssertionError):
                self.assertIn('Demo snapshot · 2026-11-12 IST', client.get('/').text)
                event = client.get(filing).text
                for expected in ('physical PDF page 2', 'Meera Rao', 'December 8, 2026', '#page=2'):
                    self.assertIn(expected, event)
                research = client.get('/research', params={'symbol': 'MARUTI', 'question': MARUTI_QUESTION}).text
                self.assertIn('4 million units', research)
                self.assertIn('#page=68', research)
                self.assertNotIn('Answer from excerpts', research)
                market = client.get('/', params={'company': 'MARUTI'}).text
                self.assertIn('+1.99%', market)
                self.assertIn('1.88×', market)
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
