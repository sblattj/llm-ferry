#!/usr/bin/env python3
"""Offline contract tests for the public model catalog."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


class Response(io.BytesIO):
    def geturl(self):
        return 'https://openrouter.ai/api/v1/models'


class CatalogTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('catalog_under_test', Path(__file__).with_name('ferry_catalog.py'))
        self.catalog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.catalog)
        self.now = 1000
        self.clock = patch.object(self.catalog.time, 'monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def response(self, rows, **extra):
        return Response(json.dumps({'data': rows, **extra}).encode())

    def test_all_results_no_cap_and_modalities_preserved(self):
        rows = [{'id': f'vendor/model-{n}', 'architecture': {'input_modalities': ['image'], 'output_modalities': ['image']}} for n in range(1607)]
        with patch.object(self.catalog._opener, 'open', return_value=self.response(rows, total_count=1607)):
            result = self.catalog.get_catalog()
        self.assertEqual(result['total'], 1607)
        self.assertEqual(result['models'][-1]['output_modalities'], ['image'])
        self.assertFalse(result['stale'])

    def test_pagination_deduplicates(self):
        pages = [self.response([{'id': 'a'}, {'id': 'b'}], total_count=3, links={'next': '?cursor=2'}), self.response([{'id': 'b'}, {'id': 'c'}], total_count=3, links={'next': None})]
        with patch.object(self.catalog._opener, 'open', side_effect=pages) as opened:
            result = self.catalog.get_catalog()
        self.assertEqual([r['id'] for r in result['models']], ['a', 'b', 'c'])
        self.assertEqual(opened.call_args_list[1].args[0].full_url, self.catalog.SOURCE + '?cursor=2')

    def test_cache_ttl_refresh_and_snapshot_isolation(self):
        def fetch(*args, **kwargs):
            return self.response([{'id': 'a'}])
        with patch.object(self.catalog._opener, 'open', side_effect=fetch) as opened:
            first = self.catalog.get_catalog()
            first['models'].clear()
            self.assertEqual(self.catalog.get_catalog(refresh=True)['total'], 1)
            self.assertEqual(opened.call_count, 1)
            self.now += 31
            self.catalog.get_catalog(refresh=True)
            self.assertEqual(opened.call_count, 2)
            self.now += 899
            self.catalog.get_catalog()
            self.assertEqual(opened.call_count, 2)
            self.now += 1
            self.catalog.get_catalog()
            self.assertEqual(opened.call_count, 3)

    def test_stale_failure_retains_success_and_does_not_leak_errors(self):
        with patch.object(self.catalog._opener, 'open', return_value=self.response([{'id': 'a'}])):
            good = self.catalog.get_catalog()
        self.now += 901
        with patch.object(self.catalog._opener, 'open', side_effect=OSError('secret-token password Authorization')) as opened:
            result = self.catalog.get_catalog()
            self.catalog.get_catalog(refresh=True)
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(result['models'], good['models'])
        self.assertEqual(result['fetched_at'], good['fetched_at'])
        self.assertTrue(result['stale'])
        self.assertNotIn('secret', result['error'])

    def test_unknown_and_zero_pricing(self):
        row = self.catalog._normalize({'id': 'a', 'pricing': {'prompt': '0', 'completion': 0}, 'context_length': False})
        self.assertEqual(row['pricing'], {'prompt': '0', 'completion': '0'})
        self.assertIsNone(row['context_length'])
        for bad in (None, '', 'NaN', 'Infinity', -1, True, {}, []):
            self.assertIsNone(self.catalog._price(bad))
        self.assertEqual(self.catalog._price('0.0000010'), '0.0000010')
        self.assertIsNone(self.catalog._normalize({'id': 'a'})['pricing']['prompt'])

    def test_malformed_and_truncated_never_succeed(self):
        cases = [b'bad json', b'[]', b'{"data":{}}', b'{"data":[null]}', b'{"data":[{"id":"a"}],"total_count":2}', b'{"data":[]}', b'{"data":[{"id":"a"}],"links":false}']
        for body in cases:
            with self.subTest(body=body), patch.object(self.catalog._opener, 'open', return_value=Response(body)):
                self.now += 31
                result = self.catalog.get_catalog(refresh=True)
                self.assertTrue(result['stale'])
                self.assertIsNotNone(result['error'])
                self.assertEqual(result['total'], 0)

    def test_host_scheme_path_and_redirect_rejection(self):
        for url in ('https://evil.example/api/v1/models', 'http://openrouter.ai/api/v1/models', 'https://openrouter.ai:444/api/v1/models', 'https://x:y@openrouter.ai/api/v1/models', 'https://openrouter.ai/api/v1/chat/completions', '//127.0.0.1/api/v1/models'):
            with self.subTest(url=url), self.assertRaises(self.catalog.CatalogError):
                self.catalog._safe_url(url)
        request = self.catalog.urllib.request.Request(self.catalog.SOURCE)
        with self.assertRaises(self.catalog.CatalogError):
            self.catalog._PublicRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://evil.example/')

    def test_public_headers_only_and_pagination_loop(self):
        with patch.object(self.catalog._opener, 'open', return_value=self.response([{'id': 'a'}], links={'next': self.catalog.SOURCE})) as opened:
            result = self.catalog.get_catalog()
        headers = dict(opened.call_args.args[0].header_items())
        self.assertEqual(set(headers), {'Accept', 'User-agent'})
        self.assertEqual(opened.call_count, 1)
        self.assertIn('loop', result['error'])

    def test_size_budget_and_second_page_failure_are_atomic(self):
        with patch.object(self.catalog, 'MAX_BODY_BYTES', 10), patch.object(self.catalog._opener, 'open', return_value=self.response([{'id': 'a'}])):
            self.assertIn('size budget', self.catalog.get_catalog()['error'])
        self.now += 31
        with patch.object(self.catalog._opener, 'open', side_effect=[self.response([{'id': 'a'}], links={'next': '?page=2'}), OSError('outage')]):
            result = self.catalog.get_catalog()
        self.assertEqual(result['total'], 0)
        self.assertIsNone(result['fetched_at'])

    def test_catalog_edge_branches(self):
        # Line 30: invalid url
        with self.assertRaises(self.catalog.CatalogError) as cm:
            self.catalog._safe_url('')
        self.assertIn('Invalid catalog pagination URL', str(cm.exception))
        with self.assertRaises(self.catalog.CatalogError):
            self.catalog._safe_url(None)

        # Lines 38-39: urlsplit/port ValueError
        with self.assertRaises(self.catalog.CatalogError) as cm:
            self.catalog._safe_url('https://openrouter.ai:99999/api/v1/models')
        self.assertIn('Unsafe catalog pagination URL', str(cm.exception))

        # Line 103: time budget exceeded
        self.now += 31
        calls = [0]
        def fake_mono():
            calls[0] += 1
            return 1000.0 if calls[0] >= 3 else 0.0

        with patch('time.monotonic', side_effect=fake_mono):
            res = self.catalog.get_catalog(refresh=True)
            self.assertIn('time budget', res['error'])

        # Line 121: invalid total count (boolean or negative)
        self.now += 31
        with patch.object(self.catalog._opener, 'open', return_value=self.response([{'id': 'a'}], total_count=True)):
            res = self.catalog.get_catalog(refresh=True)
            self.assertIn('invalid total count', res['error'])
        self.now += 31
        with patch.object(self.catalog._opener, 'open', return_value=self.response([{'id': 'a'}], total_count=-1)):
            res = self.catalog.get_catalog(refresh=True)
            self.assertIn('invalid total count', res['error'])

        # Line 123: total count changed during pagination
        self.now += 31
        resp1 = self.response([{'id': 'a'}], links={'next': '?page=2'}, total_count=2)
        resp2 = self.response([{'id': 'b'}], total_count=3)
        with patch.object(self.catalog._opener, 'open', side_effect=[resp1, resp2]):
            res = self.catalog.get_catalog(refresh=True)
            self.assertIn('retry required', res['error'])


if __name__ == '__main__':
    unittest.main()
