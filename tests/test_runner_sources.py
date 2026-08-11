from __future__ import annotations

import unittest

from app.core.runner import (
    _SourceUnavailableError,
    _friendly_source_error,
    _parse_message_link,
    _resolve_source_inputs,
)


class _FakeClient:
    def __init__(self, *, source_error: Exception | None = None) -> None:
        self.source_error = source_error
        self.get_entity_calls = 0
        self.get_input_calls = 0

    async def get_entity(self, source_ref):
        self.get_entity_calls += 1
        if self.source_error is not None:
            raise self.source_error
        return ("entity", source_ref)

    async def get_input_entity(self, entity):
        self.get_input_calls += 1
        return ("input", entity)


class RunnerSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_private_message_link_uses_stable_numeric_peer(self) -> None:
        peer, message_id = _parse_message_link("https://t.me/c/1234567890/112")
        self.assertEqual(peer, -1001234567890)
        self.assertIsInstance(peer, int)
        self.assertEqual(message_id, 112)

    async def test_unresolvable_source_is_identified_as_source_failure(self) -> None:
        original = ValueError('No user has "examplechannel" as username')
        client = _FakeClient(source_error=original)
        with self.assertRaises(_SourceUnavailableError) as raised:
            await _resolve_source_inputs(
                client,
                source_ref="examplechannel",
                source_label="https://t.me/examplechannel/112",
                cache_key="examplechannel",
                entity_cache={},
                input_cache={},
            )
        self.assertIs(raised.exception.original, original)
        self.assertIn("Destinations were not affected", _friendly_source_error(
            raised.exception.source,
            raised.exception.original,
        ))

    async def test_connection_error_remains_retryable_transport_failure(self) -> None:
        client = _FakeClient(source_error=ConnectionError("connection lost"))
        with self.assertRaises(ConnectionError):
            await _resolve_source_inputs(
                client,
                source_ref="source",
                source_label="source",
                cache_key="source",
                entity_cache={},
                input_cache={},
            )

    async def test_source_entity_and_input_are_cached(self) -> None:
        client = _FakeClient()
        entity_cache = {}
        input_cache = {}
        first = await _resolve_source_inputs(
            client,
            source_ref=-1001234567890,
            source_label="source",
            cache_key="-1001234567890",
            entity_cache=entity_cache,
            input_cache=input_cache,
        )
        second = await _resolve_source_inputs(
            client,
            source_ref=-1001234567890,
            source_label="source",
            cache_key="-1001234567890",
            entity_cache=entity_cache,
            input_cache=input_cache,
        )
        self.assertEqual(first, second)
        self.assertEqual(client.get_entity_calls, 1)
        self.assertEqual(client.get_input_calls, 1)


if __name__ == "__main__":
    unittest.main()
