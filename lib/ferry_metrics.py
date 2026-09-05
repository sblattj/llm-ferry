"""Bounded, best-effort observations of provider response copies.

No request or response content is retained after ``finish``. Token values are
provider reports, never estimates, and reasoning is a subset of output tokens.
"""
import contextvars
import json
import time

CURRENT_METRICS = contextvars.ContextVar("ferry_request_metrics", default=None)


class RequestMetrics:
    MAX_SSE_FRAME = 256 * 1024
    MAX_JSON_BODY = 1024 * 1024

    def __init__(self, started_at=None, clock=time.monotonic):
        self._clock = clock
        self._started = clock() if started_at is None else started_at
        self._values = dict.fromkeys(("stream", "response_start_ms", "first_text_ms",
                                     "total_duration_ms", "input_tokens", "output_tokens",
                                     "reasoning_tokens", "cached_input_tokens"))
        self._values["response_complete"] = False
        self._buffer = bytearray()
        self._data = []
        self._frame_size = 0
        self._discard = False
        self._sse = False
        self._finished = False
        self._tentative_zero = set()

    def _elapsed(self):
        return max(0.0, (self._clock() - self._started) * 1000)

    def set_request(self, doc, path=None):
        if isinstance(doc, dict):
            stream = doc.get("stream", False)
            self._values["stream"] = stream if isinstance(stream, bool) else None

    def start_response(self, headers, status):
        if self._finished:
            return
        if self._values["response_start_ms"] is None:
            self._values["response_start_ms"] = self._elapsed()
        try:
            pairs = headers.items() if hasattr(headers, "items") else headers
            for key, value in pairs:
                if isinstance(key, bytes):
                    key = key.decode("latin1")
                if isinstance(value, bytes):
                    value = value.decode("latin1")
                if key.lower() == "content-type":
                    self._sse = "text/event-stream" in value.lower()
        except (TypeError, ValueError, AttributeError):
            pass

    def _numeric(self, source, key, target):
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # NaN, infinity and negative counts are not usable token reports.
            if 0 <= value < float("inf"):
                self._values[target] = value
                if target in ("input_tokens", "output_tokens") and value > 0:
                    self._tentative_zero.clear()
                elif value != 0:
                    self._tentative_zero.discard(target)

    def observe_openai_usage(self, usage):
        if self._finished:
            return
        try:
            if not isinstance(usage, dict):
                usage = usage.model_dump(exclude_unset=True)
            if not isinstance(usage, dict):
                return
            self._numeric(usage, "prompt_tokens", "input_tokens")
            self._numeric(usage, "completion_tokens", "output_tokens")
            self._numeric(usage, "input_tokens", "input_tokens")
            self._numeric(usage, "output_tokens", "output_tokens")
            self._numeric(usage, "cache_read_input_tokens", "cached_input_tokens")
            for key in ("completion_tokens_details", "output_tokens_details"):
                details = usage.get(key)
                if isinstance(details, dict):
                    self._numeric(details, "reasoning_tokens", "reasoning_tokens")
            for key in ("prompt_tokens_details", "input_tokens_details"):
                details = usage.get(key)
                if isinstance(details, dict):
                    self._numeric(details, "cached_tokens", "cached_input_tokens")
        except Exception:
            # SDK objects and malformed provider payloads must not affect serving.
            pass

    def _text(self, value):
        if isinstance(value, str) and value and self._values["first_text_ms"] is None:
            self._values["first_text_ms"] = self._elapsed()

    def _content(self, content):
        if isinstance(content, str):
            self._text(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("text", "output_text"):
                    self._text(block.get("text"))

    def _document(self, doc):
        if not isinstance(doc, dict):
            return
        self.observe_openai_usage(doc.get("usage"))
        kind = doc.get("type")
        if kind == "message_start":
            message = doc.get("message")
            if isinstance(message, dict):
                usage = message.get("usage")
                self.observe_openai_usage(usage)
                if isinstance(usage, dict) and usage.get("input_tokens") == 0 and usage.get("output_tokens") == 0:
                    # The compatibility adapter emits placeholder 0/0 even when
                    # no provider usage exists. Keep all-zero-only usage unknown.
                    self._tentative_zero.update(("input_tokens", "output_tokens"))
                self._content(message.get("content"))
        elif kind == "content_block_delta":
            delta = doc.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                self._text(delta.get("text"))
        elif kind == "content_block_start":
            block = doc.get("content_block")
            if isinstance(block, dict) and block.get("type") == "text":
                self._text(block.get("text"))
        elif kind == "response.output_text.delta":
            self._text(doc.get("delta"))
        response = doc.get("response")
        if isinstance(response, dict):
            self._document(response)
        if doc.get("role") in (None, "assistant"):
            self._content(doc.get("content"))
        choices = doc.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                for key in ("delta", "message"):
                    part = choice.get(key)
                    if isinstance(part, dict) and part.get("role") in (None, "assistant"):
                        self._content(part.get("content"))
        output = doc.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict) and item.get("type") == "message" and item.get("role") in (None, "assistant"):
                    self._content(item.get("content"))

    def _parse(self, raw):
        try:
            self._document(json.loads(raw))
            return True
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return False

    def _line(self, line):
        line = line.removesuffix(b"\r")
        if not line:
            if not self._discard and self._data:
                self._parse(b"\n".join(self._data))
            self._data.clear()
            self._frame_size = 0
            self._discard = False
            return
        self._frame_size += len(line) + 1
        if self._frame_size > self.MAX_SSE_FRAME:
            self._discard = True
            self._data.clear()
        if not self._discard and line.startswith(b"data:"):
            self._data.append(line[5:].removeprefix(b" "))

    def feed(self, body_bytes):
        if self._finished or not body_bytes:
            return
        try:
            if not self._sse:
                if self._discard:
                    return
                if len(self._buffer) + len(body_bytes) > self.MAX_JSON_BODY:
                    self._buffer.clear()
                    self._discard = True
                    return
                self._buffer.extend(body_bytes)
                if self._buffer.rstrip().endswith((b"}", b"]")):
                    self._parse(self._buffer)
                return
            # Process slices without copying an unbounded input chunk into storage.
            start = 0
            while start < len(body_bytes):
                end = body_bytes.find(b"\n", start)
                stop = len(body_bytes) if end < 0 else end
                size = stop - start
                if len(self._buffer) + size + self._frame_size > self.MAX_SSE_FRAME:
                    self._discard = True
                    self._data.clear()
                    self._buffer.clear()
                elif not self._discard:
                    self._buffer.extend(body_bytes[start:stop])
                if end < 0:
                    # Record that a discarded line is nonempty across chunk splits.
                    if self._discard and size:
                        self._buffer[:] = b"\r" if not self._buffer and body_bytes[start:stop] == b"\r" else b"x"
                    break
                if self._discard:
                    # Only a truly empty delimiter ends a discarded event.
                    empty = self._buffer in (b"", b"\r") and body_bytes[start:stop] in (b"", b"\r")
                    if empty:
                        self._line(b"")
                    self._buffer.clear()
                else:
                    self._line(bytes(self._buffer))
                    self._buffer.clear()
                start = end + 1
        except (ValueError, TypeError, AttributeError, RecursionError):
            pass

    def finish(self, complete=True):
        if not self._finished:
            if not self._sse and not self._discard and self._buffer:
                self._parse(self._buffer)
            for field in self._tentative_zero:
                self._values[field] = None
            self._values["total_duration_ms"] = self._elapsed()
            self._values["response_complete"] = bool(complete)
            self._finished = True
            self._buffer.clear()
            self._data.clear()
            self._frame_size = 0
        return dict(self._values)
