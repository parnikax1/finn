from contextlib import closing
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from finn_app.db import connect
from finn_app.main import app
from finn_app.research_fetch import (
    DOCUMENT_HOSTS, PDF_LIMIT, ResearchChecker, allowed_url, run_fetch, status_for,
    storage_for, valid_symbol, write_status,
)


class ResearchFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, FINN_DB=str(Path(self.temp.name) / 'test.sqlite'))
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.params = {'symbol': 'DEMO', 'question': 'What did they say about export volumes?'}
        with closing(connect()) as db:
            self.directory = storage_for(db)

    def test_validation_uses_exact_nse_symbol(self):
        data = b'SYMBOL,NAME OF COMPANY\nDEMO,Demo Co\n'
        with patch.object(ResearchChecker, 'fetch', return_value=data):
            self.assertTrue(valid_symbol(self._checker(), 'DEMO'))
            self.assertFalse(valid_symbol(self._checker(), 'DEM'))
        response = self.client.post('/research/fetch', params={**self.params, 'symbol': '../DEMO'})
        self.assertEqual(response.status_code, 422)

    def _checker(self):
        from datetime import date
        from types import SimpleNamespace

        return ResearchChecker(SimpleNamespace(out=self.temp.name, ticker='DEMO', as_of=date(2026, 11, 12),
                                               document=[], timeout=12))

    def test_download_has_limits_and_rejects_unapproved_hosts(self):
        checker = self._checker()
        url = 'https://nsearchives.nseindia.com/a.pdf'
        with patch('check_data.Checker.fetch', return_value=None) as fetch:
            checker.fetch('annual_01', url)
            self.assertEqual(fetch.call_args.kwargs['max_bytes'], PDF_LIMIT)
            self.assertEqual(fetch.call_args.kwargs['attempts'], 1)
        self.assertFalse(allowed_url('https://nsearchives.nseindia.com:8080/a.pdf', DOCUMENT_HOSTS))
        self.assertFalse(allowed_url('https://nsearchives.nseindia.com@example.org/a.pdf', DOCUMENT_HOSTS))
        self.assertTrue(allowed_url(url, DOCUMENT_HOSTS))

    def test_partial_result_preserves_page_citation_and_missing_source_reason(self):
        self.directory.mkdir(parents=True)
        write_status(self.directory / 'DEMO.json', {'state': 'running', 'started': time.time()})
        url = 'https://nsearchives.nseindia.com/corporate/DEMO_Transcript.pdf'
        candidates = {'annual': [], 'transcript': [(url, 'automatic:nse_announcements')]}
        outages = {'annual': ['NSE annual reports'], 'transcript': []}

        def document(checker, kind, original, provenance, index, **limits):
            self.assertEqual((kind, original, index), ('transcript', url, 1))
            pdf = checker.root / 'documents' / 'call.pdf'
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=200, height=200)
            writer.write(pdf)
            text = pdf.with_suffix('.txt')
            text.write_text('--- PDF PAGE 1 ---\nEarnings Conference Call\nAugust 6, 2026\n'
                            '--- PDF PAGE 3 ---\nExport volumes rose across markets.', encoding='utf-8')
            checker.report['documents'].append({
                'kind': kind, 'url': url, 'provenance': provenance, 'download': 'succeeded',
                'extraction': 'succeeded', 'validation': 'needs_validation', 'pages': 3, 'pdf': 'documents/call.pdf',
                'text': 'documents/call.txt', 'sha256': hashlib.sha256(pdf.read_bytes()).hexdigest()})
            return True, []

        with patch('finn_app.research_fetch.valid_symbol', return_value=True), \
             patch('finn_app.research_fetch.discover', return_value=(candidates, outages)), \
             patch.object(ResearchChecker, 'document', document):
            run_fetch('DEMO', self.directory)
        status = status_for(self.directory, 'DEMO')
        self.assertFalse(status['sources']['annual']['indexed'])
        self.assertTrue(status['sources']['transcript']['indexed'])
        page = self.client.get('/research', params=self.params)
        self.assertIn('No candidate PDF was discovered', page.text)
        self.assertIn('Export volumes rose', page.text)
        self.assertIn('Call held 2026-08-06', page.text)
        self.assertIn('DEMO_Transcript.pdf#page=3', page.text)
        self.assertIn('Fetch missing source', page.text)
        poll = self.client.get('/research/fetch/progress', params=self.params, headers={'HX-Request': 'true'})
        self.assertTrue(poll.headers['HX-Redirect'].startswith('/research?symbol=DEMO'))

    def test_search_and_poll_do_not_start_a_fetch(self):
        with patch('finn_app.main.start_fetch') as start:
            page = self.client.get('/research', params=self.params)
            self.assertIn('Fetch documents', page.text)
            start.assert_not_called()
            reply = self.client.post('/research/fetch', params=self.params, follow_redirects=False)
            self.assertEqual(reply.status_code, 303)
            start.assert_called_once_with(self.directory, 'DEMO')
