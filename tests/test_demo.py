from contextlib import closing
import json
from pathlib import Path
from html import unescape
import re
from urllib.parse import urlsplit
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
                release = '/events/' + demo_links(db)['wipro_release']
                expected_cards = [
                    f'/events/{row["id"]}' for row in db.execute(
                        'SELECT e.id FROM events e ORDER BY e.priority DESC, '
                        'EXISTS(SELECT 1 FROM pages p WHERE p.sha=e.document_sha) DESC, e.filed_at DESC, e.id'
                    )
                ]
            with TestClient(app) as client, patch('socket.create_connection', side_effect=AssertionError):
                feed = client.get('/').text
                self.assertIn('12 Nov 2026 IST', feed)
                full = client.get('/?view=all').text
                self.assertIn('View all filings', feed)
                first_card = feed.split('<ul class="priority-list">', 1)[1].split('</a>', 1)[0]
                self.assertIn('RBI approves reappointment', first_card)
                self.assertIn('id="featured-heading"', feed)
                self.assertEqual(feed.split('<ul class="priority-list">', 1)[1].split('</ul>', 1)[0].count('<li>'), 2)
                for page in (2, 3):
                    self.assertRegex(feed, rf'{filing}\?page={page}&amp;back=[^"]+#source-evidence')
                self.assertRegex(feed, rf'{filing}\?back=[^"]+">Read brief')
                cards = [urlsplit(unescape(href)).path
                         for href in re.findall(r'class="event-card"[^>]* href="([^"]+)"', full)]
                self.assertEqual(cards, expected_cards)
                self.assertIn('PressRelease06112026.pdf', client.get(release).text)
                release_only = client.get('/', params={'q': 'press release dated'}).text
                self.assertNotIn('id="featured-heading"', release_only)
                self.assertIn(f'class="event-card" href="{release}?back=', release_only)
                evidence_only = client.get('/', params={'evidence': '1', 'view': 'brief'}).text
                self.assertIn('id="featured-heading"', evidence_only)
                self.assertNotIn('Related press release', evidence_only)
                event = client.get(filing).text
                for expected in ('physical PDF page 2', 'Meera Rao', 'December 8, 2026', '#page=2'):
                    self.assertIn(expected, event)
                profile = client.get(filing, params={'page': 3}).text
                self.assertIn('physical PDF page 3', profile)
                self.assertIn('including CEO of the Americas 2 Business Unit', profile)
                self.assertIn('#page=3', profile)
                self.assertIn('Sources and export', profile)
                self.assertRegex(profile, rf'<a href="{release}">Press release filing</a>')
                research = client.get('/research', params={'symbol': 'MARUTI', 'question': MARUTI_QUESTION}).text
                for value in ('Prepared walkthrough', '500,000 units/year', 'max="4" value="2.9"',
                              '1.1 million units/year', '#page=3', '#page=68', 'Who could add context'):
                    self.assertIn(value, research)
                self.assertIn('#page=68', research)
                self.assertIn('Asha Sen', research)
                self.assertIn('Which plants could close the remaining capacity gap', research)
                self.assertNotIn('Answer from excerpts', research)
                market = client.get('/', params={'company': 'MARUTI', 'view': 'brief'}).text
                self.assertIn('+1.99%', market)
                self.assertIn('1.88×', market)
                self.assertIn('<time datetime="2026-11-11">2026-11-11</time>', market)
                self.assertIn('<details class="market-alert">', market)
                self.assertIn('Previous-session CSV', market)
                self.assertIn('2026-11-08 CSV', market)
                self.assertIn('Financial results from Suzuki Motor Corporation', full)
                with closing(connect()) as db:
                    maruti_event = db.execute(
                        "SELECT id, description FROM events WHERE symbol='MARUTI' AND subject='General Updates'"
                    ).fetchone()
                maruti_detail = client.get('/events/' + maruti_event['id']).text
                self.assertIn(maruti_event['description'], maruti_detail)
                self.assertIn('Exchange title:</strong> General Updates', maruti_detail)
                self.assertIn('<h3>Acquisition</h3>', full)
                self.assertIn('<h3>October 2026 production, sales and export figures</h3>', full)
                self.assertIn('<h3>Allotment of 52,771 equity shares</h3>', full)
                latest = client.get('/', params={'window': '1'}, headers={'HX-Request': 'true'}).text
                self.assertIn('View all filings', latest)
                self.assertNotIn('Angan Guha', latest)
                self.assertIn('name="window" id="filter-window"', latest)
                self.assertIn('15/15 succeeded', latest)
                self.assertIn('TMPV', latest)
                self.assertIn('Angan Guha', client.get('/', params={'window': 'all'}).text)
                search = client.get('/research', params={'symbol': 'CUMMINSIND', 'question': 'Exports?'}).text
                self.assertIn('Fetch documents', search)

    def test_reseeding_adds_page_three_to_existing_demo(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', FINN_DB=str(Path(temp) / 'finn.sqlite3')
        ), closing(connect()) as db:
            snapshot = json.loads(SNAPSHOT.read_text())
            older_snapshot = json.loads(SNAPSHOT.read_text())
            wipro = next(doc for doc in older_snapshot['documents'] if doc['symbol'] == 'WIPRO')
            del wipro['pages']['3']
            self.assertTrue(seed(db, older_snapshot))
            self.assertFalse(demo_links(db)['wipro_ready'])
            event_count = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            self.assertFalse(seed(db, snapshot))
            self.assertTrue(demo_links(db)['wipro_ready'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], event_count)

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
