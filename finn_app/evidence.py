"""Choose source text for appointments described in exchange metadata."""
from datetime import datetime
import re


APPOINTMENT = re.compile(
    r'(?i:\A(?:.*?has informed the exchange regarding\s+)?(?:the\s+)?appointment of\s+)'
    r'(?:(?i:Mr|Ms|Mrs|Dr)\.?\s+)?'
    r'(?P<name>[A-Z][\w.’\'-]*(?:\s+[A-Z][\w.’\'-]*){1,5})'
    r'(?i:\s+as\s+)(?P<role>[^.;]{4,180}?)'
    r'(?=\s+(?i:of the company|with effect from|effective|w\.?e\.?f\.?)\b|[.;]|$)'
)
EFFECTIVE = r'\b(?:with effect from|effective(?:\s+from)?|w\s*\.?\s*e\s*\.?\s*f\.?)\s*[:,]?\s*'
MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
DATE = rf'(?:{MONTH}\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}[\s-]+{MONTH}\.?[\s,-]+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})'


def calendar_date(value):
    cleaned = ' '.join(re.sub(r'[,.]', ' ', value).split())
    for form in ('%B %d %Y', '%b %d %Y', '%d %B %Y', '%d %b %Y', '%Y-%m-%d'):
        try:
            candidate = cleaned.replace('-', ' ') if form.startswith('%d') else cleaned
            return datetime.strptime(candidate, form).date().isoformat()
        except ValueError:
            continue
    return None


def appointment_details(event):
    if event.get('rule') != 'R05':
        return None
    description = ' '.join(event['description'].split())
    match = APPOINTMENT.match(description)
    if match:
        effective = re.search(EFFECTIVE + f'(?P<date>{DATE})', description, re.I)
        if not effective and re.search(EFFECTIVE, description[match.end():], re.I):
            # An unreadable metadata date cannot be checked against the PDF.
            return match['name'], match['role'].strip(), ''
        return match['name'], match['role'].strip(), calendar_date(effective['date']) if effective else None
    return None


def event_title(event):
    appointment = appointment_details(event)
    if appointment is None:
        return event['subject']
    name, role, _ = appointment
    role = re.sub(r'\bChief Executive Officer\b', 'CEO', role, flags=re.I)
    title = f"{event['name']} appoints {name} as {role}"
    return title if len(title) <= 135 else f"{event['name']} appoints {name}"


def source_phrase(phrase):
    return r'\b' + r'\s+'.join(re.escape(word) for word in phrase.split()) + r'\b'


def appointment_excerpt(text, appointment):
    """Return a contiguous, dated appointment statement with normalized whitespace."""
    name, role, effective = appointment
    person = source_phrase(name)
    job = source_phrase(role)
    honorific = r'(?:(?:Mr|Ms|Mrs|Dr)\s*\.?\s+)?'
    appointed = r'(?:will\s+(?:take\s+over|serve|assume\s+charge)|(?:has\s+been|was|is)\s+appointed)'
    statement = re.compile(
        rf'(?:\b{honorific}{person}\s+{appointed}\s+as\s+|\b(?:appointment\s+of|appointed)\s+{honorific}{person}\s+as\s+)'
        rf'{job}(?:\s+of the company)?[, ]*\s*{EFFECTIVE}(?P<date>{DATE})\b\.?',
        re.I,
    )
    normalized = ' '.join(text.split())
    for match in statement.finditer(normalized):
        if effective is None or calendar_date(match['date']) == effective:
            return match.group()
    return None


def suggested_question(event, contact):
    if event['rule'] != 'R05' or 'IT delivery' not in contact['role']:
        return None
    if re.search(r'\bAmericas\s+2\b', event['description'], re.I):
        return ('From your IT delivery experience, what staffing and client-delivery indicators '
                'would you watch in the first quarter after the Americas 2 leadership handover?')
    return ('From your IT delivery experience, what would you watch for in staffing and '
            'client delivery after this leadership change?')
