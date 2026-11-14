"""Deterministic triage from exchange metadata; never a materiality assessment."""
import json
import re
from .db import ROOT


def classify(subject, description):
    text = (subject + ' ' + description).lower()
    rules = [
        ('R01', 10, 'Routine', r'newspaper|trading window|compliance certificate|depositories and participants|postal ballot|shareholders meeting|investor meetings|investor conference|analyst.*meet|institutional.*meet',
         'Meeting logistics or routine disclosure. No material development inferred.'),
        ('R02', 10, 'Routine', r'intimation.*board meeting|board meeting.*(intimation|scheduled|to be held)|auditor.*appointment|appointment.*auditor',
         'Meeting notice or administrative appointment; no financial outcome inferred.'),
        ('R03', 35, 'Source document', r'transcript|audio recording|annual report|investor presentation',
         'A source document was published. Publication alone does not establish a material change.'),
        ('R04', 40, 'Related-company update', r'suzuki motor corporation|parent compan',
         'Related-company disclosure. Not treated as the listed issuer\'s own results.'),
        ('R05', 80, 'Management change', r'resign|appointment|change in management|change in director|cessation|chief financial|chief executive',
         'An explicit management or director change warrants review; business impact is unassessed.'),
        ('R06', 80, 'Results published', r'financial results|quarterly results|audited results|unaudited results',
         'Results are explicitly referenced. Headline numbers and financial impact have not been analysed.'),
        ('R07', 80, 'Transaction', r'\bacquisition\b|\bmerger\b|\bdemerger\b|scheme of arrangement',
         'A transaction is explicitly referenced; size, completion and impact need review.'),
        ('R08', 75, 'Regulatory matter', r'action\(s\) taken|regulatory order|\bpenalty\b|\blitigation\b|\blawsuit\b',
         'A regulatory or legal matter is referenced. Severity and financial impact are unassessed.'),
        ('R09', 70, 'Operations', r'commencement of commercial production|capacity expansion|capital expenditure',
         'An operating or capacity development is referenced; commercial impact needs review.'),
        ('R10', 20, 'Routine', r'dividend|shareholding pattern|takeover regulations|record date|bonus issue|stock split',
         'Corporate-action or ownership disclosure. No investment significance inferred.'),
    ]
    # Document titles take precedence over meeting-language in their covering descriptions.
    if re.search(r'transcript|audio recording|annual report|investor presentation', text):
        return dict(rule='R03', priority=35, category='Source document',
                    reason='A source document was published. Publication alone does not establish a material change.')
    for rule, priority, category, pattern, reason in rules:
        if re.search(pattern, text):
            return dict(rule=rule, priority=priority, category=category, reason=reason)
    return dict(rule='R11', priority=40, category='Needs review',
                reason='No specific priority rule matched. Significance has not been established.')


def matching_contacts(symbol, sector):
    contacts = json.loads((ROOT / 'finn_app' / 'data' / 'contacts.json').read_text())
    matches = []
    for contact in contacts:
        exact = symbol in contact['companies']
        sector_match = sector and sector in contact['sectors']
        if not (exact or sector_match):
            continue
        reason = (f'The fictional profile lists past work at {symbol}.' if exact else
                  f'The fictional profile lists experience in {sector}.')
        matches.append({**contact, 'basis': 'Company match' if exact else 'Sector match',
                        'reason': reason + ' ' + contact['context'], 'score': 100 if exact else 50})
    return sorted(matches, key=lambda c: (-c['score'], c['name']))[:2]
