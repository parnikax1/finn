"""Build Brief's curated stories and market highlights from indexed demo data."""

from .navigation import event_url


# Pin editorial wording to the exact exchange announcements it describes.
FILING_STORIES = {
    'd6eda4fa4e2f8ddf357ec5e4': (
        'RBI clears HDFC Bank director’s reappointment',
        'The exchange notice names V. Srinivasa Rangan as whole-time director.',
        'blue',
    ),
    'd62d7c1627264c0e24b915fc': (
        'Asian Paints appoints Leo Puri',
        'The filing identifies Puri as an independent director.',
        'violet',
    ),
}
MARUTI_WINDOW_FILING = 'b079aa8106d632c948f8fb3d'


def market_highlights(market):
    """Choose the largest price move and volume multiple on the latest signal date."""
    signals = [
        (item, kind, signal, signal['rows'][0]['date'])
        for item in market for kind, signal in item['snapshot'].items()
        if kind in {'price', 'volume'} and signal and signal['triggered']
    ]
    if not signals:
        return []
    latest = max(date for _, _, _, date in signals)
    highlights = []
    for kind, field, style in [('price', 'move', '+.2f'), ('volume', 'multiple', '.2f')]:
        candidates = [row for row in signals if row[1] == kind and row[3] == latest]
        if not candidates:
            continue
        item, _, signal, day = max(candidates, key=lambda row: abs(float(row[2][field])))
        value = format(float(signal[field]), style) + ('%' if kind == 'price' else '×')
        highlights.append(dict(kind=kind, symbol=item['symbol'], name=item['name'], value=value,
                               date=day, baseline_days=len(signal['rows']) - 1, href=f"#signal-{item['symbol']}"))
    return highlights


def featured_stories(events, wipro, walkthrough, maruti_url, highlights, window_start, latest_day,
                     back, *, filings_only=False):
    stories = []
    if wipro:
        stories.append(dict(kind='date', tone='electric', label='WIPRO · LEADERSHIP',
                            title=wipro['title'], description=wipro['context'],
                            visual_date=wipro['effective_date'], visual_label='Appointment takes effect',
                            source='Exchange filing · PDF pp. 2–3', source_date=wipro['filed_at'][:10],
                            action='Read brief',
                            href=event_url(wipro['id'], back)))
    by_id = {event['id']: event for event in events}
    maruti = by_id.get(MARUTI_WINDOW_FILING)
    if maruti and walkthrough and walkthrough['annual']['excerpt']:
        stories.append(dict(kind='capacity', tone='violet', label='MARUTI · CAPACITY',
                            title='Maruti’s reported capacity and 4 million target',
                            description='The annual report puts capacity at about 2.9 million units a year.',
                            source='Research context · Annual report p. 68 · Filing', source_date=maruti['filed_at'][:10],
                            reported=walkthrough['reported_capacity'], target=walkthrough['capacity_target'],
                            action='See capacity research', href=maruti_url))
    for signal in highlights:
        if filings_only or (window_start and not window_start <= signal['date'] <= latest_day):
            continue
        price = signal['kind'] == 'price'
        stories.append(dict(kind='metric', tone='blue' if price else 'violet',
                            label=f"{signal['symbol']} · {'PRICE MOVE' if price else 'VOLUME'}",
                            title=(f"{signal['name']} moves {signal['value']}" if price
                                   else f"{signal['name']} volume hits {signal['value']} baseline"),
                            description=('Close versus the previous session.' if price
                                         else f"Trading volume versus the prior {signal['baseline_days']}-session average."),
                            visual_value=signal['value'], visual_label='Close-to-close' if price else 'Volume vs. baseline',
                            source_date=signal['date'], source='NSE cash market', action='View signal',
                            href=signal['href']))
    for event_id, (title, description, tone) in FILING_STORIES.items():
        event = by_id.get(event_id)
        if event:
            stories.append(dict(kind='date', tone=tone, label=f"{event['symbol']} · EXCHANGE FILING",
                                title=title, description=description, visual_date=event['filed_at'][:10],
                                iso_date=True, visual_label='Filing date', source='Exchange filing',
                                action='Read filing', href=event_url(event_id, back)))
    return stories
