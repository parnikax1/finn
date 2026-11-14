"""Keep filing navigation on a validated, relative brief URL."""
import re
from urllib.parse import parse_qs, urlencode


def brief_state(value):
    if len(value) > 3000:
        return ''
    try:
        fields = parse_qs(value, keep_blank_values=True, max_num_fields=6,
                          strict_parsing=True, errors='strict')
    except (ValueError, UnicodeError):
        return ''
    if any(len(items) != 1 for items in fields.values()):
        return ''
    state = {key: items[0] for key, items in fields.items()}
    if (state.keys() - {'window', 'company', 'priority', 'q', 'evidence', 'page'}
            or state.get('window', '7') not in {'1', '7', '30', 'all'}
            or not re.fullmatch(r'[A-Z0-9&_.-]{0,32}', state.get('company', ''))
            or state.get('priority', '') not in {'', 'important', 'routine'}
            or len(state.get('q', '')) > 200
            or state.get('evidence', '0') not in {'0', '1'}
            or not re.fullmatch(r'[1-9][0-9]{0,8}', state.get('page', '1'))):
        return ''
    return urlencode(state)


def event_url(event_id, back='', *, page=None, anchor=''):
    params = {}
    if page is not None:
        params['page'] = page
    if back:
        params['back'] = back
    return '/events/' + event_id + ('?' + urlencode(params) if params else '') + anchor
