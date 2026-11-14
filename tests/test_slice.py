"""Isolated fake fixtures; none are shipped as application data."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from finn_app.db import connect
from finn_app.import_cache import import_paths, filing_time
from finn_app.main import app
from finn_app.rules import classify, matching_contacts


class SliceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, FINN_DB=str(self.root / 'test.sqlite3'))
        self.env.start()
        docs = self.root / 'cache' / 'documents'
        docs.mkdir(parents=True)
        self.pdf = docs / 'filing.pdf'
        writer = PdfWriter()
        for content in ['First actual PDF page.', 'Second actual PDF page.']:
            page = writer.add_blank_page(width=500, height=500)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 30 460 Td ({content}) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(self.pdf)
        rows = []
        for seq, desc, text, url in [
            ('1', 'General Updates', 'Reference to financial results issued by Suzuki Motor Corporation', 'filing.pdf'),
            ('2', 'Appointment', 'Appointment of an independent director.', 'appointment.pdf'),
            ('3', 'Copy of Newspaper Publication', '<script>bad()</script> Routine notice.', 'notice.pdf')]:
            rows.append(dict(symbol='MARUTI', seq_id=seq, sm_name='Maruti Suzuki',
                             sm_isin='INE585B01010', desc=desc, attchmntText=text,
                             attchmntFile='https://example.com/' + url,
                             an_dt='10-Nov-2026 11:56:00'))
        report = dict(ticker='MARUTI', as_of='2026-11-11', filings=rows,
                      security=[{'SYMBOL':'MARUTI', 'ISIN NUMBER':'INE585B01010'}],
                      documents=[dict(url='https://example.com/filing.pdf', pdf='documents/filing.pdf',
                                      sha256=hashlib.sha256(self.pdf.read_bytes()).hexdigest())])
        (self.root / 'cache' / 'report.json').write_text(json.dumps(report))
        self.assertEqual(import_paths([self.root / 'cache']), 0)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close(); self.env.stop(); self.temp.cleanup()

    def event(self, seq):
        db = connect()
        row = dict(db.execute('SELECT * FROM events WHERE exchange_id=?', (seq,)).fetchone())
        db.close()
        return row

    def test_reimport_keeps_identity_and_count(self):
        before = self.event('1')
        self.assertEqual(import_paths([self.root / 'cache']), 0)
        after = self.event('1')
        db = connect()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 3)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM pages').fetchone()[0], 2)
        db.close()
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(after['filed_at'], '2026-11-10T11:56:00+05:30')
        self.assertEqual(after['source_url'], 'https://example.com/filing.pdf')

    def test_evidence_preserves_physical_page_and_contact_reason(self):
        response = self.client.get('/events/' + self.event('1')['id'] + '?page=2')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Second actual PDF page.', response.text)
        self.assertIn('physical PDF page 2', response.text)
        self.assertIn('SYNTHETIC CONTACT', response.text)
        self.assertIn('fictional profile lists past work at MARUTI', response.text)
        self.assertIn('Related-company disclosure', response.text)

    def test_missing_evidence_is_not_manufactured(self):
        response = self.client.get('/events/' + self.event('2')['id'])
        self.assertIn('No cached document excerpt', response.text)
        self.assertNotIn('<blockquote>', response.text)

    def test_routine_stays_collapsed_and_html_escaped(self):
        response = self.client.get('/?priority=routine')
        self.assertEqual(response.status_code, 200)
        self.assertIn('&lt;script&gt;', response.text)
        self.assertNotIn('<script>bad()', response.text)
        self.assertLess(self.event('3')['priority'], 30)

    def test_htmx_filters_and_cached_pdf(self):
        response = self.client.get('/?evidence=1', headers={'HX-Request':'true'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('1 matching filings', response.text)
        self.assertNotIn('<!doctype', response.text)
        event_id = self.event('1')['id']
        response = self.client.get('/events/' + event_id + '/document')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.pdf.write_bytes(b'changed')
        self.assertEqual(self.client.get('/events/' + event_id + '/document').status_code, 409)

    def test_rules_and_missing_date(self):
        self.assertLess(classify('Updates', 'Investor conference on 19 November')['priority'], 30)
        self.assertLess(classify('General Updates', 'Financial results issued by Suzuki Motor Corporation')['priority'], 70)
        self.assertGreaterEqual(classify('Appointment', 'Independent director appointed')['priority'], 70)
        self.assertLess(classify('Earnings Call Transcript', 'Investor conference transcript')['priority'], 70)
        self.assertIsNone(filing_time({'an_dt':'bad', 'dt':'10112026115600'})[0])
        self.assertEqual(matching_contacts('UNKNOWN', 'Unmapped sector'), [])

    def test_changed_attachment_does_not_inherit_old_evidence(self):
        report_path = self.root / 'cache' / 'report.json'
        report = json.loads(report_path.read_text())
        report['filings'][0]['attchmntFile'] = 'https://example.com/revised-filing.pdf'
        report_path.write_text(json.dumps(report))
        event_id = self.event('1')['id']
        self.assertEqual(import_paths([self.root / 'cache']), 0)
        self.assertEqual(self.event('1')['id'], event_id)
        self.assertIsNone(self.event('1')['document_sha'])
        self.assertIn('No cached document excerpt', self.client.get('/events/' + event_id).text)


if __name__ == '__main__':
    unittest.main()
