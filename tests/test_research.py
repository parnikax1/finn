from contextlib import closing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from finn_app.db import connect
from finn_app.evidence import appointment_details, appointment_excerpt
from finn_app.main import app
from finn_app.research import date_from_pages, passages, query_features, revalidate_periods


class ResearchTests(unittest.TestCase):
    def test_appointment_matches_dated_statement_instead_of_heading_or_old_role(self):
        event = dict(rule='R05', description='Appointment of Mr. Example Person as CEO for Americas 2 '
                                            'of the company w.e.f. December 08, 2026.')
        appointment = appointment_details(event)
        self.assertEqual(appointment, ('Example Person', 'CEO for Americas 2', '2026-12-08'))
        self.assertIsNone(appointment_excerpt('Change in Senior Management Personnel', appointment))
        self.assertIsNone(appointment_excerpt('Mr. Example Person was appointed as CEO for Americas 2 '
                                               'effective November 8, 2026.', appointment))
        source = 'Annexure A\nMr. Example Person will take over as CEO for Americas\n2 with effect from\nDecember 8, 2026.'
        self.assertEqual(appointment_excerpt(source, appointment),
                         'Mr. Example Person will take over as CEO for Americas 2 with effect from December 8, 2026.')

    def test_transcript_uses_call_date_instead_of_exchange_cover_letter_date(self):
        date, _ = date_from_pages('transcript', ['Exchange filing\nOctober 23, 2026',
                                                 'Earnings Conference Call\nOctober 15th, 2026'])
        self.assertEqual(date, '2026-10-15')

    def test_search_displays_pdf_page_and_missing_source_separately(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, FINN_DB=str(Path(temp) / 'test.sqlite')):
            with closing(connect()) as db, db:
                db.execute("INSERT INTO documents VALUES ('test', NULL, 'ready', '')")
                db.execute("INSERT INTO pages VALUES ('test', 4, 'Exports grew after production capacity expanded.')")
                db.execute("INSERT INTO research_sources VALUES ('DEMO', 'transcript', 'test', "
                           "'https://example.org/call.pdf', 'automatic:nse_announcements', "
                           "'Call held 2026-10-15', '2026-10-15', '2026-11-12')")
                db.execute("INSERT INTO research_status VALUES ('DEMO', 'annual', "
                           "'Download failed (HTTP 403)', '2026-11-12')")
            client = TestClient(app)
            response = client.get('/research', params={'symbol': 'demo.ns', 'question': 'What about exports?'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Exports grew', response.text)
            self.assertIn('call.pdf#page=4', response.text)
            self.assertIn('2026-10-15', response.text)
            self.assertIn('Download failed (HTTP 403)', response.text)
            empty = client.get('/research', params={'symbol': 'DEMO', 'question': 'aluminium'})
            self.assertIn('No excerpt matched', empty.text)
            self.assertNotIn('Exports grew', empty.text)

    def test_report_period_uses_explicit_scope_and_ignores_filing_reference(self):
        letter = ('NSE & BSE / 2026-27 / 084\n'
                  'Sub: Submission of the Annual Report of the Company for FY 2025-26')
        self.assertEqual(date_from_pages('annual', [letter, '2026\nAnnual Report']),
                         ('2026', 'Reporting period 2025–2026'))
        self.assertEqual(date_from_pages('annual', ['Annual Integrated Report\n2025-26']),
                         ('2026', 'Reporting period 2025–2026'))
        self.assertEqual(date_from_pages('annual', ['NSE & BSE / 2026-27 / 084', '2026\nAnnual Report']),
                         (None, None))
        self.assertEqual(date_from_pages('annual', [letter, 'Annual Report\n2024-25']), (None, None))

    def test_report_period_can_use_directors_audited_year_end(self):
        pages = ['2026\nAnnual Report'] + ['Contents'] * 12 + [
            'Your Directors are pleased to present the Thirty-sixth Annual Report of your Company along '
            'with the Audited Financial Statements for the Financial Year ended March 31, 2026.']
        self.assertEqual(date_from_pages('annual', pages), ('2026', 'Financial year ended 31 March 2026'))

    def test_ranking_preserves_ai_and_rewards_contiguous_phrases(self):
        query = query_features('What did Persistent say about AI-led delivery and enterprise demand?', 'PERSISTENT')
        terms, _, _ = query
        self.assertIn('ai', terms)
        self.assertNotIn('led', terms)
        pages = [
            {'page': 1, 'text': 'Meeting enterprise demand with practitioner-led delivery.'},
            {'page': 2, 'text': 'Delivery and demand in the enterprise use AI.'},
            {'page': 3, 'text': 'AI–led delivery supports enterprise demand.'},
        ]
        hits = passages(pages, *query)
        self.assertEqual([hit['page'] for hit in hits], [3, 2, 1])
        self.assertNotIn('led', hits[-1]['terms'])

    def test_snippet_boundaries_preserve_amounts_and_units(self):
        query = query_features('production capacity', 'MARUTI')
        for amount in ('4 million units', '₹ 4.2 crore', '(4 million vehicles per annum)', '17.5 %'):
            for before in (78, 79):
                text = 'production capacity ' + 'context ' * (before - 2) + amount + ' tail ' * 20
                hit = passages([{'page': 1, 'text': text}], *query)[0]
                self.assertIn(amount, hit['text'])
        text = 'context ' * 48 + '₹ 4.2 crore ' + 'production capacity ' * 25
        hit = passages([{'page': 1, 'text': text}], *query)[0]
        self.assertIn('₹ 4.2 crore', hit['text'])

    def test_revalidation_updates_previously_cached_annual_labels(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, FINN_DB=str(Path(temp) / 'test.sqlite')):
            with closing(connect()) as db, db:
                db.execute("INSERT INTO documents VALUES ('test', NULL, 'ready', '')")
                db.execute("INSERT INTO pages VALUES ('test', 1, 'NSE & BSE / 2026-27 / 084')")
                db.execute("INSERT INTO research_sources VALUES ('DEMO', 'annual', 'test', "
                           "'https://example.org/report.pdf', 'manual_url', "
                           "'Reporting period 2026–2027', '2027', '2026-11-12')")
                revalidate_periods(db, 'DEMO')
                self.assertIsNone(db.execute("SELECT date_label FROM research_sources").fetchone()[0])
            response = TestClient(app).get('/research', params={'symbol': 'DEMO', 'question': 'capacity'})
            self.assertIn('Period unverified', response.text)
