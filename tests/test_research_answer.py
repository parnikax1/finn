from contextlib import closing
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from finn_app.db import connect
from finn_app.main import app
from finn_app.research_answer import AnswerUnavailable, request_answer, validate_answer


CAPACITY = ('The company increased its annual production capacity from approximately 2.4 million units '
            'to 2.9 million units during FY 2026-27')
TARGET = 'medium-term objective of expanding annual production capacity to 4 million units.'
QUESTION = 'What did Maruti say about production capacity and when exactly will it reach the target?'


class ResearchAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, FINN_DB=str(Path(self.temp.name) / 'test.sqlite'), OPENAI_API_KEY='')
        env.start()
        self.addCleanup(env.stop)
        with closing(connect()) as db, db:
            db.execute("INSERT INTO documents VALUES ('demo', NULL, 'ready', '')")
            db.execute("INSERT INTO pages VALUES ('demo', 68, ?)", (CAPACITY + ' and retains its ' + TARGET,))
            db.execute("INSERT INTO research_sources VALUES ('MARUTI', 'annual', 'demo', ?, "
                       "'automatic:nse_annual_reports', 'Reporting period 2025–2026', '2026', '2026-11-12')",
                       ('https://example.org/report.pdf#old',))
        self.client = TestClient(app)
        self.params = {'symbol': 'MARUTI', 'question': QUESTION}

    def test_search_needs_no_key_and_never_calls_provider(self):
        with patch('finn_app.research_answer.request_answer') as provider:
            page = self.client.get('/research', params=self.params)
            self.assertEqual(page.status_code, 200)
            self.assertIn(CAPACITY, page.text)
            self.assertNotIn('Set OPENAI_API_KEY', page.text)
            self.assertNotIn('Answer from excerpts', page.text)
            direct = self.client.post('/research/answer', params=self.params)
            self.assertIn('Set OPENAI_API_KEY', direct.text)
            self.assertIn(CAPACITY, direct.text)
            provider.assert_not_called()

    def test_cited_capacity_and_target_with_unanswered_part(self):
        response = {'claims': [{'evidence_id': 'E1', 'quote': quote} for quote in (CAPACITY, TARGET)],
                    'gaps': ['when exactly will it reach the target']}
        with patch.dict(os.environ, OPENAI_API_KEY='unit-test-key'), \
             patch('finn_app.research_answer.request_answer', return_value=response) as provider:
            result = self.client.post('/research/answer', params=self.params, headers={'HX-Request': 'true'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.text.count('href="https://example.org/report.pdf#page=68"'), 2)
            self.assertIn('Annual report · Reporting period 2025–2026 · PDF page 68', result.text)
            self.assertIn(CAPACITY, result.text)
            self.assertIn(TARGET, result.text)
            self.assertIn('when exactly will it reach the target', result.text)
            self.assertNotIn('unit-test-key', result.text)
            sent = provider.call_args.args[-1]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]['page'], 68)

    def test_abstention_with_related_but_insufficient_excerpts(self):
        question = 'What will Maruti’s production capacity be in FY 2035–36?'
        with patch.dict(os.environ, OPENAI_API_KEY='unit-test-key'), \
             patch('finn_app.research_answer.request_answer',
                   return_value={'claims': [], 'gaps': [question]}) as provider:
            result = self.client.post('/research/answer', params={'symbol': 'MARUTI', 'question': question},
                                      headers={'HX-Request': 'true'})
            self.assertIn('Insufficient evidence in the retrieved excerpts', result.text)
            self.assertIn(question, result.text)
            self.assertNotIn('PDF page', result.text)
            self.assertTrue(provider.call_args.args[-1])

    def test_no_matching_excerpt_abstains_without_provider(self):
        with patch('finn_app.research_answer.request_answer') as provider:
            result = self.client.post('/research/answer',
                                      params={'symbol': 'MARUTI', 'question': 'How many phlebotomists?'},
                                      headers={'HX-Request': 'true'})
            self.assertIn('Insufficient evidence', result.text)
            provider.assert_not_called()

    def test_validation_rejects_unknown_citations_altered_claims_and_unrelated_gaps(self):
        source = [{'id': 'E1', 'excerpt': CAPACITY}]
        for bad in ({'claims': [{'evidence_id': 'E9', 'quote': CAPACITY}], 'gaps': []},
                    {'claims': [{'evidence_id': 'E1', 'quote': CAPACITY.replace('2.9', '4.0')}], 'gaps': []},
                    {'claims': [], 'gaps': ['new unsupported claim']}):
            with self.assertRaises(AnswerUnavailable):
                validate_answer(bad, QUESTION, source)

    def test_provider_contract_with_simulated_http_response(self):
        evidence = [{'id': 'E1', 'document': 'Annual report', 'excerpt': CAPACITY, 'page': 68,
                     'url': 'https://example.org/report.pdf'}]
        decoded = {'claims': [{'evidence_id': 'E1', 'quote': CAPACITY}], 'gaps': []}
        body = {'status': 'completed', 'output': [{'type': 'reasoning'}, {'type': 'message',
                'content': [{'type': 'output_text', 'text': json.dumps(decoded)}]}]}
        with patch('finn_app.research_answer.build_opener') as opener:
            send = opener.return_value.open
            send.return_value = io.BytesIO(json.dumps(body).encode())
            self.assertEqual(request_answer('unit-test-key', 'MARUTI', QUESTION, evidence), decoded)
            request = send.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
            self.assertEqual(request.get_header('Authorization'), 'Bearer unit-test-key')
            self.assertFalse(payload['store'])
            self.assertNotIn('tools', payload)
            self.assertNotIn('url', json.loads(payload['input'])['excerpts'][0])
            self.assertTrue(payload['text']['format']['strict'])
