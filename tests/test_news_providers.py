import unittest

from src.news_providers import MockNewsProvider, NewsProvider, RawNewsEvent


class TestRawNewsEvent(unittest.TestCase):
    def test_only_text_and_source_are_required(self):
        event = RawNewsEvent(text="hello", source="mock")
        self.assertIsNone(event.published_at)
        self.assertIsNone(event.event_id)
        self.assertIsNone(event.url)

    def test_all_fields_can_be_set(self):
        event = RawNewsEvent(text="hello", source="mock", published_at="2026-01-01T00:00:00+00:00", event_id="e1", url="https://example.com")
        self.assertEqual(event.event_id, "e1")
        self.assertEqual(event.url, "https://example.com")


class TestNewsProviderIsAbstract(unittest.TestCase):
    def test_cannot_instantiate_the_base_class_directly(self):
        with self.assertRaises(TypeError):
            NewsProvider()

    def test_a_subclass_must_implement_fetch_events(self):
        class Incomplete(NewsProvider):
            name = "incomplete"

        with self.assertRaises(TypeError):
            Incomplete()


class TestMockNewsProvider(unittest.TestCase):
    def test_default_sample_events_are_returned(self):
        provider = MockNewsProvider()
        events = provider.fetch_events()
        self.assertGreaterEqual(len(events), 1)
        self.assertIsInstance(events[0], RawNewsEvent)

    def test_custom_events_list_of_raw_events(self):
        events_in = [RawNewsEvent(text="a", source="mock"), RawNewsEvent(text="b", source="mock")]
        provider = MockNewsProvider(events=events_in)
        events_out = provider.fetch_events()
        self.assertEqual(events_out, events_in)

    def test_custom_events_list_of_plain_dicts_is_coerced(self):
        provider = MockNewsProvider(events=[{"text": "a", "source": "mock"}])
        events = provider.fetch_events()
        self.assertIsInstance(events[0], RawNewsEvent)
        self.assertEqual(events[0].text, "a")

    def test_an_event_of_the_wrong_type_raises_at_construction(self):
        with self.assertRaises(TypeError):
            MockNewsProvider(events=[42])

    def test_limit_caps_the_returned_events(self):
        events_in = [RawNewsEvent(text=str(i), source="mock") for i in range(5)]
        provider = MockNewsProvider(events=events_in)
        self.assertEqual(len(provider.fetch_events(limit=2)), 2)

    def test_limit_none_returns_everything(self):
        events_in = [RawNewsEvent(text=str(i), source="mock") for i in range(5)]
        provider = MockNewsProvider(events=events_in)
        self.assertEqual(len(provider.fetch_events(limit=None)), 5)

    def test_raise_error_makes_fetch_events_raise_that_error(self):
        provider = MockNewsProvider(raise_error=ConnectionError("network down"))
        with self.assertRaises(ConnectionError):
            provider.fetch_events()

    def test_raise_error_can_be_an_exception_class(self):
        provider = MockNewsProvider(raise_error=TimeoutError)
        with self.assertRaises(TimeoutError):
            provider.fetch_events()

    def test_empty_events_list_returns_nothing(self):
        provider = MockNewsProvider(events=[])
        self.assertEqual(provider.fetch_events(), [])

    def test_has_a_name(self):
        self.assertEqual(MockNewsProvider().name, "mock")


if __name__ == "__main__":
    unittest.main()
