"""Reasoning adapter hook: offline contract plus the installed LiteLLM converter.

Run with ordinary python3; the real converter cases also run under LiteLLM's
Python environment and skip only if LiteLLM is not installed.
"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "front"))
import ferry_front as FF

M = FF._metrics_module()
NAME = "_translate_openai_usage_to_anthropic_usage_delta"


class HookTests(unittest.TestCase):
    def adapter(self, fail=False):
        sentinel = object()
        class Adapter:
            @classmethod
            def _translate_openai_usage_to_anthropic_usage_delta(cls, usage):
                if fail:
                    raise ValueError("provider conversion failed")
                self.assert_usage = usage
                return sentinel
        self.assertTrue(FF.install_reasoning_usage_hook(Adapter))
        return Adapter, sentinel

    def test_exact_return_identity_and_input_identity(self):
        adapter, sentinel = self.adapter()
        usage = {"completion_tokens_details": {"reasoning_tokens": 9}}
        self.assertIs(getattr(adapter, NAME)(usage), sentinel)
        self.assertIs(self.assert_usage, usage)

    def test_absent_context_does_not_observe_or_change_conversion(self):
        adapter, sentinel = self.adapter()
        self.assertIsNone(M.CURRENT_METRICS.get())
        self.assertIs(getattr(adapter, NAME)(None), sentinel)

    def test_idempotent_install(self):
        adapter, _ = self.adapter()
        installed = adapter.__dict__[NAME].__func__
        self.assertTrue(FF.install_reasoning_usage_hook(adapter))
        self.assertIs(adapter.__dict__[NAME].__func__, installed)

    def test_only_explicit_reasoning_numeric_values_are_captured(self):
        adapter, _ = self.adapter()
        for value in [0, 12, None, True, "8"]:
            with self.subTest(value=value):
                collector = M.RequestMetrics()
                token = M.CURRENT_METRICS.set(collector)
                try:
                    getattr(adapter, NAME)({"prompt_tokens": 100, "completion_tokens": 80,
                        "completion_tokens_details": {"reasoning_tokens": value}})
                finally:
                    M.CURRENT_METRICS.reset(token)
                rec = collector.finish()
                self.assertEqual(rec["reasoning_tokens"], value if type(value) is int else None)
                self.assertIsNone(rec["input_tokens"])
                self.assertIsNone(rec["output_tokens"])

    def test_observer_failure_preserves_original_return_and_original_errors(self):
        collector = mock.Mock()
        collector.observe_openai_usage.side_effect = RuntimeError("observer failure")
        token = M.CURRENT_METRICS.set(collector)
        try:
            adapter, sentinel = self.adapter()
            usage = {"completion_tokens_details": {"reasoning_tokens": 8}}
            self.assertIs(getattr(adapter, NAME)(usage), sentinel)
            failing, _ = self.adapter(fail=True)
            with self.assertRaisesRegex(ValueError, "provider conversion failed"):
                getattr(failing, NAME)(usage)
        finally:
            M.CURRENT_METRICS.reset(token)

    def test_concurrent_contexts_are_isolated(self):
        adapter, _ = self.adapter()
        async def request(n):
            collector = M.RequestMetrics()
            token = M.CURRENT_METRICS.set(collector)
            try:
                await asyncio.sleep(0)
                getattr(adapter, NAME)({"completion_tokens_details": {"reasoning_tokens": n}})
                await asyncio.sleep(0)
                return collector.finish()["reasoning_tokens"]
            finally:
                M.CURRENT_METRICS.reset(token)
        async def run():
            return await asyncio.gather(request(3), request(11))
        self.assertEqual(asyncio.run(run()), [3, 11])
        self.assertIsNone(M.CURRENT_METRICS.get())

    def test_missing_optional_adapter_method_is_nonfatal(self):
        with mock.patch("sys.stderr"):
            self.assertFalse(FF.install_reasoning_usage_hook(type("Missing", (), {})))

    def test_loader_returns_the_same_contextvar_module(self):
        self.assertIs(FF._metrics_module(), M)
        self.assertIs(sys.modules["ferry_metrics"], M)


class InstalledConverterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import LiteLLMAnthropicMessagesAdapter
            from litellm.types.utils import Usage
        except ImportError:
            raise unittest.SkipTest("LiteLLM is not installed in this interpreter")
        cls.adapter = LiteLLMAnthropicMessagesAdapter
        cls.Usage = Usage

    def test_real_conversion_is_byte_equal_and_captures_reasoning_before_drop(self):
        descriptor = self.adapter.__dict__[NAME]
        usage = self.Usage(prompt_tokens=100, completion_tokens=50,
            prompt_tokens_details={"cached_tokens": 20},
            completion_tokens_details={"reasoning_tokens": 17})
        before = getattr(self.adapter, NAME)(usage)
        original_usage = usage.model_dump()
        collector = M.RequestMetrics()
        token = M.CURRENT_METRICS.set(collector)
        try:
            self.assertTrue(FF.install_reasoning_usage_hook())
            after = getattr(self.adapter, NAME)(usage)
            # Exercise the nonstream public usage translator too: it delegates
            # through the same patched delta classmethod.
            nonstream = self.adapter._translate_openai_usage_to_anthropic_usage(usage)
        finally:
            M.CURRENT_METRICS.reset(token)
            setattr(self.adapter, NAME, descriptor)
        self.assertEqual(json.dumps(before).encode(), json.dumps(after).encode())
        self.assertEqual(nonstream, before)
        self.assertEqual(usage.model_dump(), original_usage)
        self.assertEqual(after["input_tokens"], 80)
        self.assertEqual(after["cache_read_input_tokens"], 20)
        self.assertNotIn("reasoning_tokens", after)
        result = collector.finish()
        self.assertEqual(result["reasoning_tokens"], 17)
        self.assertIsNone(result["input_tokens"], "must not mix cache-inclusive input into Anthropic usage")


if __name__ == "__main__":
    unittest.main(verbosity=2)
