#!/usr/bin/env python3
"""Offline role regression; run with LiteLLM's Python for real bridge coverage."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "front"))
from ferry_front import install_chatgpt_system_compat


class CompatibilityTests(unittest.TestCase):
    def test_only_system_messages_change_without_mutation(self):
        original = {"instructions": "Keep these instructions", "input": [
            {"role": "system", "content": [{"type": "input_text", "text": "First"}]},
            {"type": "message", "role": "system", "content": "Second"},
            {"role": "developer", "content": "Existing"},
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
            {"type": "function_call", "name": "Read", "call_id": "1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "1", "output": "File"},
        ], "stream": True, "store": False}
        snapshot = copy.deepcopy(original)

        class OtherProvider:
            def transform_responses_api_request(self):
                return original

        class ChatGPT(OtherProvider):
            pass

        install_chatgpt_system_compat(ChatGPT)
        once = ChatGPT.transform_responses_api_request
        install_chatgpt_system_compat(ChatGPT)
        self.assertIs(once, ChatGPT.transform_responses_api_request)
        result = ChatGPT().transform_responses_api_request()
        expected = copy.deepcopy(snapshot)
        for item in expected["input"][:2]:
            item["role"] = "developer"
        self.assertEqual(result, expected)
        self.assertEqual(original, snapshot)
        self.assertIs(OtherProvider().transform_responses_api_request(), original)

    def test_string_input_is_untouched(self):
        original = {"input": "Hello", "instructions": "System prompt"}

        class ChatGPT:
            def transform_responses_api_request(self):
                return original

        install_chatgpt_system_compat(ChatGPT)
        self.assertIs(ChatGPT().transform_responses_api_request(), original)

    def test_installed_anthropic_bridge_preserves_block_system_prompt(self):
        try:
            from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
            from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
            from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import LiteLLMAnthropicMessagesAdapter
            from litellm.completion_extras.litellm_responses_transformation.transformation import LiteLLMResponsesTransformationHandler
            from litellm.types.router import GenericLiteLLMParams
        except ModuleNotFoundError as exc:
            if exc.name == "litellm":
                self.skipTest("Run with LiteLLM's Python to exercise installed adapters")
            raise

        anthropic = {
            "model": "gpt-6-astra", "max_tokens": 128,
            "system": [{"type": "text", "text": "You are Claude Code.",
                        "cache_control": {"type": "ephemeral"}},
                       {"type": "text", "text": "Preserve project instructions."}],
            "messages": [{"role": "user", "content": "Reply OK"}],
        }
        chat, _ = LiteLLMAnthropicMessagesAdapter().translate_anthropic_to_openai(anthropic)
        inputs, instructions = LiteLLMResponsesTransformationHandler().convert_chat_completion_messages_to_responses_api(chat["messages"])
        self.assertTrue(any(item.get("role") == "system" for item in inputs))
        params = {"instructions": instructions} if instructions else {}
        args = dict(model="gpt-6-astra", input=inputs,
                    response_api_optional_request_params=params,
                    litellm_params=GenericLiteLLMParams(), headers={})
        original = ChatGPTResponsesAPIConfig.transform_responses_api_request
        openai_transform = OpenAIResponsesAPIConfig.transform_responses_api_request
        try:
            install_chatgpt_system_compat()
            baseline = original(ChatGPTResponsesAPIConfig(), **copy.deepcopy(args))
            actual = ChatGPTResponsesAPIConfig().transform_responses_api_request(**copy.deepcopy(args))
            expected = copy.deepcopy(baseline)
            for item in expected["input"]:
                if item.get("role") == "system":
                    item["role"] = "developer"
            self.assertEqual(actual, expected)
            self.assertFalse(any(item.get("role") == "system" for item in actual["input"]))
            self.assertIn("You are Claude Code.", str(actual["input"]))
            self.assertIn("Preserve project instructions.", str(actual["input"]))
            self.assertIs(OpenAIResponsesAPIConfig.transform_responses_api_request, openai_transform)
        finally:
            ChatGPTResponsesAPIConfig.transform_responses_api_request = original


if __name__ == "__main__":
    unittest.main()
