from contextlib import closing
from html import unescape
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from finn_app.db import connect
from finn_app.demo import MARUTI_QUESTION, SNAPSHOT, demo_links, seed
from finn_app.main import app


def link(html, label):
    return unescape(re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>[^<]*' + re.escape(label) + r'</a>', html)[1])


class ReviewerFlowTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict('os.environ', FINN_DB=str(Path(temp) / 'review.sqlite3'), OPENAI_API_KEY=''))
        with closing(connect()) as db:
            seed(db, json.loads(SNAPSHOT.read_text()))
            self.wipro = '/events/' + demo_links(db)['wipro']
        self.client = self.enterContext(TestClient(app))

    def test_destination_fragment_and_feed_filter_are_distinct(self):
        hx = {'HX-Request': 'true'}
        tab = hx | {'X-FINN-Destination': 'true'}
        for path in ('/?window=1', '/research', '/network'):
            response = self.client.get(path, headers=tab)
            self.assertTrue(response.text.lstrip().startswith('<main'))
            self.assertEqual(response.headers['HX-Push-Url'], path)
            self.assertIn('X-FINN-Destination', response.headers['Vary'])
            self.assertNotIn('id="app-nav"', response.text)
            self.assertIn('id="app-nav"', self.client.get(path).text)
        filtered = self.client.get('/?window=1', headers=hx)
        self.assertTrue(filtered.text.lstrip().startswith('<div id="brief-state"'))
        self.assertRegex(filtered.text, r'data-applied="[^"]*window=1')
        self.assertRegex(filtered.text, r'<option value="1"\s+selected>Latest day</option>')
        self.assertRegex(filtered.text, r'id="loading"[^>]* hidden')
        self.assertIn('Browse all 11 filings', filtered.text)
        restored = self.client.get('/?window=1', headers=hx | {'HX-History-Restore-Request': 'true'})
        self.assertIn('id="app-nav"', restored.text)
        live = self.client.get('/research?symbol=CUMMINSIND&question=Sales', headers=tab)
        self.assertEqual(live.headers['Cache-Control'], 'no-store')
        self.assertIn('hx-history="false"', live.text)
        self.assertNotIn('data-cacheable', live.text)

    def test_latest_day_return_and_validation(self):
        latest = self.client.get('/?view=all&window=1', headers={'HX-Request': 'true'}).text
        self.assertIn('11 matching filings', latest)
        card = re.search(r'<a class="event-card"[^>]*href="([^"]+)"[^>]*>\s*'
                         r'<div class="card-top"><span class="ticker">HDFCBANK</span>', latest)
        event = self.client.get(unescape(card[1]))
        returned = link(event.text, 'Back to brief')
        self.assertEqual(parse_qs(urlsplit(returned).query)['window'], ['1'])
        self.assertIn('11 matching filings', self.client.get(returned).text)
        for bad in ('https://example.org', 'window=1&window=7', 'page=0', 'window=other', 'target=//example.org'):
            event = self.client.get(self.wipro, params={'back': bad})
            self.assertEqual(link(event.text, 'Back to brief'), '/')
        self.assertEqual(link(self.client.get(self.wipro).text, 'Back to brief'), '/')

    def test_featured_excerpt_transfers_filters_and_page_selector_state(self):
        state = dict(view='brief', window='30', company='WIPRO', priority='important', q='Guha', evidence='1')
        feed = self.client.get('/', params=state).text
        card_url = link(feed, 'Read excerpt · PDF p. 2')
        event = self.client.get(card_url)
        token = parse_qs(urlsplit(card_url).query)['back'][0]
        self.assertEqual(parse_qs(token), {k: [v] for k, v in (state | {'page': '1'}).items()})
        self.assertIn(f'name="back" value="{token.replace("&", "&amp;")}"', event.text)
        page_three = self.client.get(self.wipro, params={'page': 3, 'back': token})
        self.assertIn('physical PDF page 3', page_three.text)
        self.assertEqual(link(page_three.text, 'Back to brief'), '/?' + token)

        # A normal card on page 2 must round-trip the feed page independently of PDF page.
        with closing(connect()) as db, db:
            event = db.execute('SELECT * FROM events WHERE id=?', (self.wipro.rsplit('/', 1)[1],)).fetchone()
            columns = ', '.join(event.keys())
            for i in range(60):
                values = dict(event) | {'id': f'nav-test-{i}', 'source_url': f'https://example.org/{i}.pdf'}
                db.execute(f'INSERT INTO events ({columns}) VALUES ({",".join("?" for _ in values)})',
                           tuple(values.values()))
        second = self.client.get('/', params=state | {'view': 'all', 'page': 2}).text
        self.assertIn('61 matching filings · page 2', second)
        card = re.search(r'<a class="event-card"[^>]*href="([^"]+)"', second)
        result = self.client.get(unescape(card[1]))
        back = link(result.text, 'Back to brief')
        self.assertEqual(parse_qs(urlsplit(back).query), {k: [v] for k, v in (state | {'view': 'all', 'page': '2'}).items()})
        self.assertIn('61 matching filings · page 2', self.client.get(back).text)

    def test_download_uses_walkthrough_content_without_network(self):
        with patch('socket.create_connection', side_effect=AssertionError):
            feed = self.client.get('/').text
            event = self.client.get(self.wipro).text
            response = self.client.get(link(event, 'Download brief'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('text/markdown'))
        self.assertEqual(response.headers['content-disposition'],
                         'attachment; filename="FINN-WIPRO-Americas-2-appointment-2026-11-06.md"')
        question = re.search(r'id="contact-question"[^>]*>([^<]+)', event)[1]
        for text in ('Angan Guha returns to lead Wipro’s Americas 2', '8 December 2026',
                     'Returning to a unit he led', 'client-delivery continuity'):
            self.assertIn(text, feed)
            self.assertIn(text, response.text)
        for text in ('FINN brief', '06 Nov 2026', 'Meera Rao · Synthetic contact', '#page=2', '#page=3',
                     'PressRelease06112026.pdf', 'SEintimation06112026.pdf)', unescape(question)):
            self.assertIn(text, response.text)
        self.assertEqual(self.client.get('/events/unavailable/brief.md').status_code, 404)

    def test_network_and_research_overview(self):
        for params, fragments in (
            ({'company': 'maruti'}, ('Asha Sen', 'Company match')),
            ({'company': 'Wipro'}, ('Meera Rao', 'Sector match')),
            ({'sector': 'IT services'}, ('Meera Rao', 'Sector match')),
            ({'company': 'NO_MATCH'}, ('No synthetic contact matches',)),
        ):
            result = self.client.get('/network', params=params).text
            for fragment in fragments:
                self.assertIn(fragment, result)
        research = self.client.get('/research', params={'symbol': 'MARUTI', 'question': MARUTI_QUESTION}).text
        for text in ('Sources searched', '5 of 270 PDF pages indexed', '5 of 13 PDF pages indexed',
                     'Maruti Suzuki India Limited', 'Reporting period 2025–2026', 'Call held 2026-10-27',
                     'Read excerpt · PDF p. 3', 'Read excerpt · PDF p. 68'):
            self.assertIn(text, research)
        lead = research.index('id="top-cited-excerpt"')
        sources = research.index('<details class="panel research-source-list">')
        self.assertLess(research.index('</form>'), lead)
        self.assertLess(lead, sources)
        self.assertIn('PDF p. 68', research[lead:sources])
        self.assertIn('#page=68', research[lead:sources])
        self.assertIn('2 documents indexed', research[sources:])
