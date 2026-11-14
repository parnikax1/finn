import hashlib
import os
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .db import ROOT, connect
from .demo import CUMMINS_QUESTION, MARUTI_QUESTION, demo_links, capacity_walkthrough, wipro_brief
from .evidence import appointment_details, appointment_excerpt, event_title, suggested_question
from .navigation import brief_state, event_url
from .rules import contact_profiles, matching_contacts
from .market import read_market
from .research import lookup, ticker
from .research_answer import AnswerUnavailable, answer_question, api_key, evidence_from
from .research_fetch import missing_kinds, start_fetch, status_for, storage_for

app = FastAPI(title='FINN portfolio feed', docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=ROOT / 'finn_app' / 'static'), name='static')
templates = Jinja2Templates(directory=ROOT / 'finn_app' / 'templates')
templates.env.globals['event_url'] = event_url


def format_date(value):
    if not value:
        return 'Filing date unavailable'
    return datetime.fromisoformat(value).strftime('%d %b %Y · %H:%M IST')


templates.env.filters['filing_date'] = format_date
templates.env.filters['event_title'] = event_title


def fetch_time(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Kolkata')).strftime(
        '%d %b %Y · %H:%M IST')


templates.env.filters['fetch_time'] = fetch_time


@app.get('/', response_class=HTMLResponse)
def feed(request: Request, company: str = Query('', max_length=32, pattern=r'^[A-Z0-9&_.-]*$'),
         priority: Literal['', 'important', 'routine'] = '', q: str = Query('', max_length=200),
         evidence: bool = False, window: Literal['1', '7', '30', 'all'] = '7',
         page: int = Query(1, ge=1, le=999_999_999)):
    with closing(connect()) as db:
        demo = demo_links(db)
        latest = db.execute('SELECT MAX(filed_at) FROM events').fetchone()[0]
        latest_day = latest[:10] if latest else None
        if demo and (not latest_day or demo['date'] > latest_day):
            latest_day = demo['date']
        window_start = None
        predicates, values = ['1=1'], []
        if window != 'all' and latest_day:
            window_start = (datetime.fromisoformat(latest_day) - timedelta(days=int(window) - 1)).date().isoformat()
            predicates.append('e.filed_at>=?')
            values.append(window_start)
        if company:
            predicates.append('e.symbol=?'); values.append(company)
        if priority == 'important':
            predicates.append('e.priority>=70')
        elif priority == 'routine':
            predicates.append('e.priority<30')
        if q:
            predicates.append('(e.subject LIKE ? OR e.description LIKE ? OR e.symbol LIKE ?)')
            values.extend(['%' + q + '%'] * 3)
        if evidence:
            predicates.append('EXISTS (SELECT 1 FROM pages p WHERE p.sha=e.document_sha)')
        where = ' AND '.join(predicates)
        total = db.execute(f'SELECT COUNT(*) FROM events e WHERE {where}', values).fetchone()[0]
        events = [dict(r) for r in db.execute(f'''SELECT e.*, c.name, c.sector,
          EXISTS(SELECT 1 FROM pages p WHERE p.sha=e.document_sha) AS has_evidence
          FROM events e JOIN companies c ON c.symbol=e.symbol WHERE {where}
          ORDER BY e.priority DESC, has_evidence DESC, e.filed_at DESC, e.id LIMIT 60 OFFSET ?''', values + [(page-1)*60])]
        companies = [dict(r) for r in db.execute('''SELECT c.*, COUNT(e.id) AS events FROM companies c
          LEFT JOIN events e ON e.symbol=c.symbol
          WHERE c.symbol<>'TATAMOTORS' OR e.id IS NOT NULL GROUP BY c.symbol ORDER BY c.name''')]
        stats = dict(db.execute('''SELECT COUNT(*) AS total, MIN(filed_at) AS earliest,
          MAX(filed_at) AS latest,
          COALESCE(SUM(priority>=70),0) AS important,
          SUM(EXISTS(SELECT 1 FROM pages p WHERE p.sha=events.document_sha)) AS evidence
          FROM events''').fetchone())
        market = read_market(db, company)
        run = db.execute('SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1').fetchone()
        last_fetch = db.execute('SELECT MAX(fetched_at) FROM refresh_results').fetchone()[0]
        results = list(db.execute('''
            SELECT current.symbol, current.status, current.filing_count, current.detail,
                   previous.fetched_at, window.from_date, window.to_date
            FROM refresh_results current
            LEFT JOIN refresh_results previous ON previous.symbol=current.symbol
              AND previous.run_id=(
                SELECT MAX(run_id) FROM refresh_results
                WHERE symbol=current.symbol AND status='succeeded' AND run_id<=current.run_id
              )
            LEFT JOIN refresh_runs window ON window.id=previous.run_id
            WHERE current.run_id=? ORDER BY current.symbol
        ''', (run['id'],))) if run else []
        main_events = [event for event in events if event['priority'] >= 30]
        featured = wipro_brief(db, main_events[0]['id'], demo) if main_events else None
        related_release = None
        if featured:
            main_events.pop(0)
            related_release = next((event for event in main_events if event['id'] == demo['wipro_release']), None)
            if related_release:
                main_events.remove(related_release)
    def state(number):
        return urlencode(dict(company=company, priority=priority, q=q,
                              evidence=int(evidence), window=window, page=number))
    partial = request.headers.get('HX-Request') == 'true'
    context = dict(events=events, routine=[e for e in events if e['priority'] < 30],
                   main_events=main_events, featured=featured, related_release=related_release,
                   partial=partial, total=total,
                   companies=companies, stats=stats, company=company, priority=priority,
                   q=q, evidence=evidence, page=page, back=state(page), active='brief',
                   previous='/?' + state(page-1) if page > 1 else None,
                   next='/?' + state(page+1) if page*60 < total else None,
                   cached_companies=sum(c['events'] > 0 for c in companies),
                   refresh_run=run, refresh_results=results, last_fetch=last_fetch, market=market, demo=demo,
                   window=window, latest_day=latest_day, window_start=window_start,
                   demo_research_url=research_urls('MARUTI', MARUTI_QUESTION)['search'] if demo else '')
    template = 'feed_items.html' if partial else 'feed.html'
    return templates.TemplateResponse(request=request, name=template, context=context)


def same_origin(request):
    origin = request.headers.get('Origin')
    public_url = os.getenv('RENDER_EXTERNAL_URL', '').rstrip('/')
    return not origin or origin.rstrip('/') in {str(request.base_url).rstrip('/'), public_url}


def research_urls(symbol, question):
    query = urlencode({'symbol': symbol, 'question': question})
    return {key: path + '?' + query for key, path in (
        ('search', '/research'), ('fetch', '/research/fetch'), ('progress', '/research/fetch/progress'),
        ('answer', '/research/answer'))}


templates.env.globals['research_starters'] = (
    ('MARUTI capacity', research_urls('MARUTI', MARUTI_QUESTION)['search']),
    ('CUMMINSIND domestic sales and exports', research_urls('CUMMINSIND', CUMMINS_QUESTION)['search']),
)


def research_context(db, symbol, question):
    company = db.execute('SELECT sector FROM companies WHERE symbol=?', (symbol,)).fetchone()
    contacts = matching_contacts(symbol, company['sector'] if company else None)
    walkthrough = capacity_walkthrough(db) if symbol == 'MARUTI' and question == MARUTI_QUESTION else None
    return contacts, walkthrough


@app.get('/research', response_class=HTMLResponse)
def research(request: Request, symbol: str = Query('', max_length=35),
             question: str = Query('', max_length=300), busy: bool = False):
    normalized = ticker(symbol)
    question = question.strip()
    error = ''
    groups = None
    urls, status, missing = {}, {}, []
    contacts, walkthrough = [], None
    if symbol or question:
        if not normalized:
            error = 'Enter an NSE ticker, such as MARUTI.'
        elif not question:
            error = 'Enter a question to search the cached documents.'
        else:
            urls = research_urls(normalized, question)
            with closing(connect()) as db:
                groups = lookup(db, normalized, question)
                status = status_for(storage_for(db), normalized)
                missing = missing_kinds(db, normalized)
                contacts, walkthrough = research_context(db, normalized, question)
    return templates.TemplateResponse(request=request, name='research.html', context=dict(
        symbol=normalized or symbol, question=question, groups=groups, error=error,
        urls=urls, status=status, missing=missing, busy=busy,
        has_evidence=bool(groups and evidence_from(groups)), answer_enabled=bool(api_key()),
        contacts=contacts, walkthrough=walkthrough, active='research'))


@app.post('/research/answer', response_class=HTMLResponse)
def research_answer(request: Request, symbol: str = Query(max_length=35),
                    question: str = Query(min_length=1, max_length=300)):
    normalized = ticker(symbol)
    question = question.strip()
    if not normalized or not question:
        raise HTTPException(422, 'Enter an NSE ticker and question.')
    if not same_origin(request):
        raise HTTPException(403, 'Open Company research to request an answer.')

    with closing(connect()) as db:
        groups = lookup(db, normalized, question)
        status = status_for(storage_for(db), normalized)
        missing = missing_kinds(db, normalized)
        contacts, walkthrough = research_context(db, normalized, question)
    evidence = evidence_from(groups)
    key = api_key()
    answer, answer_error = None, ''
    try:
        answer = answer_question(normalized, question, evidence, key)
    except AnswerUnavailable as exc:
        answer_error = str(exc)

    template = ('research_answer_result.html' if request.headers.get('HX-Request') == 'true'
                else 'research.html')
    return templates.TemplateResponse(request=request, name=template,
        context=dict(symbol=normalized, question=question, groups=groups,
                     urls=research_urls(normalized, question), status=status, missing=missing,
                     has_evidence=bool(evidence), answer_enabled=bool(key),
                     answer=answer, answer_error=answer_error, contacts=contacts, walkthrough=walkthrough,
                     active='research'),
        headers={'Cache-Control': 'no-store'})


@app.post('/research/fetch')
def fetch_research(request: Request, symbol: str = Query(max_length=35),
                   question: str = Query(min_length=1, max_length=300)):
    normalized = ticker(symbol)
    if not normalized or not question.strip():
        raise HTTPException(422, 'Enter an NSE ticker and question.')
    if not same_origin(request):
        raise HTTPException(403, 'Open Company research to start a fetch.')
    urls = research_urls(normalized, question.strip())
    with closing(connect()) as db:
        missing = missing_kinds(db, normalized)
        directory = storage_for(db)
    busy = bool(missing) and not start_fetch(directory, normalized)
    return RedirectResponse(urls['search'] + ('&busy=1' if busy else ''), status_code=303)


@app.get('/research/fetch/progress', response_class=HTMLResponse)
def research_progress(request: Request, symbol: str = Query(max_length=35),
                      question: str = Query(max_length=300)):
    normalized = ticker(symbol)
    if not normalized:
        raise HTTPException(422, 'Enter an NSE ticker.')
    urls = research_urls(normalized, question)
    with closing(connect()) as db:
        status = status_for(storage_for(db), normalized)
    if status.get('state') != 'running':
        if request.headers.get('HX-Request') == 'true':
            return Response(headers={'HX-Redirect': urls['search'], 'Cache-Control': 'no-store'})
        return RedirectResponse(urls['search'], status_code=303)
    return templates.TemplateResponse(request=request, name='research_fetch_status.html',
                                      context=dict(status=status, urls=urls),
                                      headers={'Cache-Control': 'no-store'})


@app.get('/events/{event_id}', response_class=HTMLResponse)
def event_detail(request: Request, event_id: str, page: int | None = Query(None, ge=0), back: str = ''):
    back = brief_state(back)
    with closing(connect()) as db:
        event = db.execute('SELECT e.*,c.name,c.sector FROM events e JOIN companies c '
                           'ON c.symbol=e.symbol WHERE e.id=?', (event_id,)).fetchone()
        if not event:
            raise HTTPException(404, 'Event not found')
        event = dict(event)
        pages = [r[0] for r in db.execute('SELECT page FROM pages WHERE sha=? ORDER BY page',
                                         (event['document_sha'],))]
        selected = page if page in pages else (pages[0] if pages else None)
        excerpt = None
        matched = False
        appointment = appointment_details(event)
        if appointment and pages and page is None:
            source_pages = db.execute(
                'SELECT page, text FROM pages WHERE sha=? AND page>0 ORDER BY page',
                (event['document_sha'],))
            for source in source_pages:
                passage = appointment_excerpt(source['text'], appointment)
                if passage:
                    excerpt = passage
                    selected = source['page']
                    matched = True
                    break
        if excerpt is None and pages:
            text = db.execute('SELECT text FROM pages WHERE sha=? AND page=?',
                              (event['document_sha'], selected)).fetchone()['text']
            excerpt = text[:1500]
            if len(text) > len(excerpt):
                excerpt = excerpt.rsplit(' ', 1)[0] + ' […]'
        doc = db.execute('SELECT * FROM documents WHERE sha=?',
                         (event['document_sha'],)).fetchone()
        doc = dict(doc) if doc else None
        brief = wipro_brief(db, event_id, demo_links(db)) if event['symbol'] == 'WIPRO' else None
    contacts = matching_contacts(event['symbol'], event['sector'])
    if contacts:
        contacts[0]['question'] = (brief['contact']['question'] if brief and brief['contact']
                                   else suggested_question(event, contacts[0]))
    return templates.TemplateResponse(request=request, name='event.html', context=dict(
        event=event, pages=pages, selected=selected, excerpt=excerpt, doc=doc, matched=matched,
        appointment=bool(appointment), default_view=page is None, contacts=contacts,
        back=back, brief_url='/?' + back if back else '/', active='brief', brief=brief))


@app.get('/events/{event_id}/brief.md')
def download_brief(event_id: str):
    with closing(connect()) as db:
        brief = wipro_brief(db, event_id, demo_links(db))
    if not brief:
        raise HTTPException(404, 'No prepared FINN brief is available for this filing.')
    content = templates.env.get_template('wipro_brief.md').render(brief=brief)
    return Response(content, media_type='text/markdown',
                    headers={'Content-Disposition': 'attachment; filename="FINN-WIPRO-Americas-2-appointment-2026-11-06.md"'})


@app.get('/network', response_class=HTMLResponse)
def network(request: Request, company: str = Query('', max_length=80), sector: str = Query('', max_length=80)):
    with closing(connect()) as db:
        companies = db.execute('SELECT symbol, name, sector FROM companies ORDER BY name').fetchall()
    sectors = sorted({tag for contact in contact_profiles() for tag in contact['sectors']}
                     | {row['sector'] for row in companies if row['sector'] != 'Unmapped sector'})
    company = company.strip()
    sector = sector.strip()
    symbol = ticker(company)
    known_sector = next((tag for tag in sectors if tag.casefold() == sector.casefold()), None)
    row = next((row for row in companies if row['symbol'] == symbol
                or row['name'].casefold() == company.casefold()), None)
    contacts = matching_contacts(row['symbol'], row['sector']) if company and row else (
        matching_contacts('', known_sector) if not company and known_sector else [])
    return templates.TemplateResponse(request=request, name='network.html', context=dict(
        active='network', companies=companies, sectors=sectors, company=company, sector=sector,
        contacts=contacts, searched=bool(company or sector), lookup_label=company or sector))


@app.get('/events/{event_id}/document')
def cached_document(event_id: str):
    with closing(connect()) as db:
        doc = db.execute('SELECT d.* FROM documents d JOIN events e ON e.document_sha=d.sha '
                         'WHERE e.id=?', (event_id,)).fetchone()
    if not doc or not doc['path'] or not Path(doc['path']).is_file():
        raise HTTPException(404, 'Cached PDF not available; use the original source link.')
    if hashlib.sha256(Path(doc['path']).read_bytes()).hexdigest() != doc['sha']:
        raise HTTPException(409, 'Cached file changed since import. Re-import before opening it.')
    return FileResponse(doc['path'], media_type='application/pdf',
                        headers={'Content-Disposition': 'inline; filename="source-filing.pdf"'})


@app.get('/health')
def health():
    return {'status': 'ok'}
