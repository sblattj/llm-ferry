#!/usr/bin/env python3
import copy
import json
import unittest
from ferry_metrics import CURRENT_METRICS, RequestMetrics


class Clock:
    value = 10
    def __call__(self):
        return self.value


class MetricsTests(unittest.TestCase):
    def observer(self, sse=True):
        clock = Clock()
        metrics = RequestMetrics(clock=clock)
        metrics.set_request({'stream': sse}, '/v1/chat/completions')
        clock.value = 10.1
        metrics.start_response([(b'content-type', b'text/event-stream' if sse else b'application/json')], 200)
        return metrics, clock

    def event(self, metrics, doc):
        metrics.feed(b'data: ' + json.dumps(doc, ensure_ascii=False).encode() + b'\n\n')

    def test_timings_and_chat_usage(self):
        m, c = self.observer()
        self.event(m, {'choices': [{'delta': {'role': 'assistant'}}]})
        c.value = 10.2
        self.event(m, {'choices': [{'delta': {'reasoning_content': 'private', 'tool_calls': [{'function': {'arguments': 'hi'}}]}}]})
        self.assertIsNone(m._values['first_text_ms'])
        c.value = 10.3
        self.event(m, {'choices': [{'delta': {'content': 'hello'}}]})
        c.value = 10.4
        self.event(m, {'usage': {'prompt_tokens': 23, 'completion_tokens': 17, 'completion_tokens_details': {'reasoning_tokens': 10}, 'prompt_tokens_details': {'cached_tokens': 0}}})
        c.value = 10.5
        result = m.finish()
        for key, value in [('response_start_ms', 100), ('first_text_ms', 300), ('total_duration_ms', 500)]:
            self.assertAlmostEqual(result[key], value)
        self.assertEqual([result[k] for k in ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cached_input_tokens')], [23, 17, 10, 0])
        self.assertTrue(result['response_complete'])
        self.assertFalse(m._buffer)
        self.assertFalse(m._data)
        self.assertEqual(m.finish(False), result)

    def test_anthropic_cumulative_cache_and_reasoning(self):
        m, c = self.observer()
        self.event(m, {'type': 'message_start', 'message': {'usage': {'input_tokens': 5, 'output_tokens': 1, 'cache_read_input_tokens': 20}}})
        self.event(m, {'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': 'hidden'}})
        self.assertIsNone(m._values['first_text_ms'])
        self.event(m, {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'visible'}})
        self.event(m, {'type': 'message_delta', 'usage': {'output_tokens': 12}})
        m.observe_openai_usage({'completion_tokens_details': {'reasoning_tokens': 8}})
        result = m.finish()
        self.assertEqual([result[k] for k in ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cached_input_tokens')], [5, 12, 8, 20])

    def test_anthropic_interrupted_synthetic_zero(self):
        for later, expected in [(None, (None, None)), ({'output_tokens': 0}, (None, None))]:
            m, _ = self.observer()
            self.event(m, {'type': 'message_start', 'message': {'usage': {'input_tokens': 0, 'output_tokens': 0}}})
            if later is not None:
                self.event(m, {'type': 'message_delta', 'usage': later})
            r = m.finish(False)
            self.assertEqual((r['input_tokens'], r['output_tokens']), expected)
        m, _ = self.observer()
        self.event(m, {'type': 'message_start', 'message': {'usage': {'input_tokens': 0, 'output_tokens': 0}}})
        m.observe_openai_usage({'completion_tokens_details': {'reasoning_tokens': 0}})
        complete_without_final_usage = m.finish(True)
        self.assertEqual(complete_without_final_usage['reasoning_tokens'], 0)
        self.assertIsNone(complete_without_final_usage['input_tokens'])
        self.assertIsNone(complete_without_final_usage['output_tokens'])
        m, _ = self.observer()
        self.event(m, {'type': 'message_start', 'message': {'usage': {'input_tokens': 3, 'output_tokens': 0}}})
        self.assertEqual(m.finish(False)['input_tokens'], 3)

    def test_responses_json_and_sse(self):
        for sse in (False, True):
            m, c = self.observer(sse)
            response = {'object': 'response', 'output': [{'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'hidden'}]}, {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hello'}]}], 'usage': {'input_tokens': 9, 'output_tokens': 8, 'output_tokens_details': {'reasoning_tokens': 3}, 'input_tokens_details': {'cached_tokens': 4}}}
            c.value = 10.2
            if sse:
                self.event(m, {'type': 'response.output_text.delta', 'delta': 'hello'})
                self.event(m, {'type': 'response.completed', 'response': response})
            else:
                m.feed(json.dumps(response).encode())
            r = m.finish()
            self.assertAlmostEqual(r['first_text_ms'], 200)
            self.assertEqual([r[k] for k in ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cached_input_tokens')], [9, 8, 3, 4])

    def test_nonstream_completion_arrival_and_anthropic_json(self):
        for doc in ({'choices': [{'message': {'role': 'assistant', 'content': 'hi'}}]}, {'type': 'message', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'hi'}], 'usage': {'input_tokens': 2, 'output_tokens': 3}}):
            m, c = self.observer(False)
            body = json.dumps(doc).encode()
            m.feed(body[:-1])
            self.assertIsNone(m._values['first_text_ms'])
            c.value = 10.5
            m.feed(body[-1:])
            self.assertAlmostEqual(m.finish()['first_text_ms'], 500)

    def test_utf8_crlf_multiline_every_split(self):
        body = b': ping\r\nevent: chunk\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"' + '🦊'.encode() + b'"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
        for split in range(len(body) + 1):
            m, _ = self.observer()
            m.feed(body[:split])
            m.feed(body[split:])
            self.assertIsNotNone(m.finish()['first_text_ms'], split)
        m, _ = self.observer()
        for value in body:
            m.feed(bytes([value]))
        self.assertIsNotNone(m.finish()['first_text_ms'])

    def test_malformed_oversize_recovery_and_release(self):
        m, _ = self.observer()
        m.MAX_SSE_FRAME = 100
        m.feed(b'data: invalid\n\n')
        for _ in range(20):
            m.feed(b'x' * 60)
            self.assertLessEqual(len(m._buffer) + sum(map(len, m._data)), 100)
        m.feed(b'\n\r')
        m.feed(b'\n')
        self.event(m, {'usage': {'completion_tokens': 7}})
        r = m.finish()
        self.assertEqual(r['output_tokens'], 7)
        self.assertIsNone(r['first_text_ms'])
        self.assertFalse(m._buffer or m._data)
        m, _ = self.observer(False)
        m.MAX_JSON_BODY = 32
        m.feed(b'x' * 10000)
        self.assertEqual(len(m._buffer), 0)
        self.assertIsNone(m.finish()['input_tokens'])

    def test_tool_only_and_incomplete_responses(self):
        m, _ = self.observer(False)
        m.feed(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'function': {'arguments': '{"text":"not visible"}'}}]}}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 6}}).encode())
        self.assertIsNone(m.finish()['first_text_ms'])
        m, _ = self.observer()
        self.event(m, {'type': 'response.incomplete', 'response': {'usage': {'input_tokens': 2, 'output_tokens': 8}}})
        result = m.finish()
        self.assertEqual(result['output_tokens'], 8)
        self.assertIsNone(result['reasoning_tokens'])
        self.assertIsNone(result['first_text_ms'])

    def test_oversize_many_lines_recovers_at_every_split(self):
        oversized = b'data: ' + b'x' * 60 + b'\r\n' + b'data: ' + b'y' * 60 + b'\r\n\r\n'
        valid = b'data: {"usage":{"output_tokens":5}}\r\n\r\n'
        body = oversized + valid
        for split in range(len(body) + 1):
            m, _ = self.observer()
            m.MAX_SSE_FRAME = 100
            m.feed(body[:split])
            m.feed(body[split:])
            self.assertEqual(m.finish()['output_tokens'], 5, split)

    def test_unknown_zero_typed_usage_no_mutation(self):
        m, _ = self.observer()
        doc = {'prompt_tokens': 0, 'completion_tokens': 4, 'completion_tokens_details': {'reasoning_tokens': 0}}
        before = copy.deepcopy(doc)
        class Usage:
            def model_dump(self, **kwargs):
                self.kwargs = kwargs
                return doc
        usage = Usage()
        m.observe_openai_usage(usage)
        m.observe_openai_usage({'prompt_tokens': None, 'completion_tokens': True, 'completion_tokens_details': {'reasoning_tokens': '7'}})
        self.assertEqual(usage.kwargs, {'exclude_unset': True})
        self.assertEqual(doc, before)
        r = m.finish()
        self.assertEqual((r['input_tokens'], r['output_tokens'], r['reasoning_tokens']), (0, 4, 0))
        self.assertIsNone(r['cached_input_tokens'])
        self.assertIsNone(r['first_text_ms'])
        self.assertIsNone(CURRENT_METRICS.get())


if __name__ == '__main__':
    unittest.main()
