"""Public OpenRouter model metadata. No inference or credentials are used here."""
import copy
import json
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

SOURCE = 'https://openrouter.ai/api/v1/models'
CACHE_TTL = 900
REFRESH_COOLDOWN = 30
FETCH_TIMEOUT = 12
FETCH_BUDGET = 60
MAX_BODY_BYTES = 32 * 1024 * 1024
_lock = threading.Lock()
_models = []
_fetched_at = None
_last_success = None
_last_attempt = None
_error = None


class CatalogError(Exception):
    pass


def _safe_url(value, base=SOURCE):
    if not isinstance(value, str) or not value:
        raise CatalogError('Invalid catalog pagination URL.')
    url = urllib.parse.urljoin(base, value)
    try:
        parts = urllib.parse.urlsplit(url)
        valid = (parts.scheme == 'https' and parts.hostname == 'openrouter.ai'
                 and parts.port in (None, 443) and not parts.username
                 and not parts.password and not parts.fragment
                 and parts.path == '/api/v1/models')
    except ValueError:
        valid = False
    if not valid:
        raise CatalogError('Unsafe catalog pagination URL rejected.')
    return url


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers,
                                        _safe_url(newurl, req.full_url))


_opener = urllib.request.build_opener(_PublicRedirect())


def _strings(value):
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _price(value):
    # Preserve the API's decimal text, including zero. Unknown never means free.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
        return str(value) if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def _normalize(row):
    if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip():
        raise CatalogError('Catalog contains an invalid model record.')
    architecture = row.get('architecture')
    architecture = architecture if isinstance(architecture, dict) else {}
    pricing = row.get('pricing')
    pricing = pricing if isinstance(pricing, dict) else {}
    context = row.get('context_length')
    if isinstance(context, bool) or not isinstance(context, int) or context < 0:
        context = None
    return {
        'id': row['id'],
        'name': row.get('name') if isinstance(row.get('name'), str) else row['id'],
        'description': row.get('description') if isinstance(row.get('description'), str) else '',
        'context_length': context,
        'pricing': {key: _price(pricing.get(key)) for key in ('prompt', 'completion')},
        'input_modalities': _strings(architecture.get('input_modalities')),
        'output_modalities': _strings(architecture.get('output_modalities')),
        'supported_parameters': _strings(row.get('supported_parameters')),
    }


def _fetch():
    url, visited, models = SOURCE, set(), {}
    deadline = time.monotonic() + FETCH_BUDGET
    remaining_bytes = MAX_BODY_BYTES
    expected_total = None
    while url:
        url = _safe_url(url)
        if url in visited:
            raise CatalogError('Catalog pagination loop detected.')
        visited.add(url)
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise CatalogError('Catalog download exceeded its time budget.')
        request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'llm-ferry-catalog/1'})
        with _opener.open(request, timeout=min(FETCH_TIMEOUT, remaining_time)) as response:
            _safe_url(response.geturl())
            # Bound memory even when a server omits or lies about Content-Length.
            body = response.read(remaining_bytes + 1)
        remaining_bytes -= len(body)
        if remaining_bytes < 0:
            raise CatalogError('Catalog download exceeded its size budget.')
        try:
            page = json.loads(body)
        except (ValueError, UnicodeError):
            raise CatalogError('Catalog returned invalid JSON.') from None
        if not isinstance(page, dict) or not isinstance(page.get('data'), list):
            raise CatalogError('Catalog returned an invalid response.')
        total = page.get('total_count')
        if total is not None:
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise CatalogError('Catalog returned an invalid total count.')
            if expected_total is not None and expected_total != total:
                raise CatalogError('Catalog changed during pagination; retry required.')
            expected_total = total
        for row in page['data']:
            normalized = _normalize(row)
            models.setdefault(normalized['id'], normalized)
        links = page.get('links', {})
        if not isinstance(links, dict):
            raise CatalogError('Catalog returned invalid pagination metadata.')
        next_url = links.get('next')
        url = _safe_url(next_url, url) if next_url is not None else None
    if expected_total is not None and len(models) != expected_total:
        raise CatalogError('Catalog result count is incomplete or inconsistent.')
    if not models:
        raise CatalogError('Catalog returned no models.')
    return list(models.values())


def get_catalog(refresh=False):
    """Return an isolated JSON-ready snapshot, retaining the last success on errors."""
    global _models, _fetched_at, _last_success, _last_attempt, _error
    with _lock:
        now = time.monotonic()
        expired = _last_success is None or now - _last_success >= CACHE_TTL
        cooldown = _last_attempt is not None and now - _last_attempt < REFRESH_COOLDOWN
        if (refresh or expired) and not cooldown:
            _last_attempt = now
            try:
                models = _fetch()
            except CatalogError as exc:
                _error = str(exc)
            except Exception:
                # Never return exception text: URLs, proxies, or headers can contain secrets.
                _error = 'Unable to retrieve the public OpenRouter catalog.'
            else:
                _models = models
                _fetched_at = time.time()
                _last_success = time.monotonic()
                _error = None
        stale = (_last_success is None or _error is not None
                 or time.monotonic() - _last_success >= CACHE_TTL)
        return {'models': copy.deepcopy(_models), 'fetched_at': _fetched_at,
                'stale': stale, 'error': _error, 'source': SOURCE, 'total': len(_models)}
