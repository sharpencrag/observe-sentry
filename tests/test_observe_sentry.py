import observe_sentry
from observatory import events

import unittest
from unittest import mock
import os


def mocked_sentry_init(*args, **kwargs):
    integrations = kwargs["integrations"]
    for integration in integrations:
        integration.setup_once()


class TestTelemetryDecorator(unittest.TestCase):

    def setUp(self):
        observe_sentry._RAISES_EXCEPTIONS = False

    @mock.patch("observe_sentry.get_telemetry_logger")
    def test_telemetry_decorator_logs_error(self, logger):
        error = logger().error
        @observe_sentry._telemetry
        def run_it():
            raise Exception()
        run_it()
        error.assert_called()

    @mock.patch("observe_sentry.get_telemetry_logger")
    def test_telemetry_decorator_raises_exception(self, logger):
        warning = logger().warning
        @observe_sentry._telemetry
        def run_it():
            1/0
        with mock.patch("observe_sentry._RAISES_EXCEPTIONS", True):
            with self.assertRaises(ZeroDivisionError):
                run_it()
        self.assertEqual(warning.call_count, 0)


@mock.patch.dict("os.environ", {"SENTRY_DSN": "", "SENTRY_SAMPLE_RATE": "0.25"})
@mock.patch("sentry_sdk.init")  # prevent sentry connection
class TestTelemetryInit(unittest.TestCase):

    def setUp(self):
        observe_sentry._INITIALIZED = False
        observe_sentry._RAISES_EXCEPTIONS = False

    @mock.patch("observe_sentry.get_telemetry_logger")
    def test_telemetry_init_when_already_initialized(self, logger, _):
        warning = logger().warning
        observe_sentry._INITIALIZED = True
        with self.assertRaises(observe_sentry.TelemetryError):
            observe_sentry.init(raise_internal_exceptions=True)
        observe_sentry.init()
        warning.assert_called()

    def test_telemetry_init_fails_no_dsn(self, _):
        copied_env = dict(os.environ)
        copied_env.pop("SENTRY_DSN")
        with self.assertRaises(observe_sentry.TelemetryError):
            with mock.patch.dict("os.environ", copied_env, clear=True):
                observe_sentry.init(raise_internal_exceptions=True)

    def test_telemetry_init_gets_passed_sample_rate(self, mocked_sentry_init):
        observe_sentry.init(sample_rate=0.15, raise_internal_exceptions=True)
        _, sentry_init_kwargs = mocked_sentry_init.call_args
        self.assertEqual(sentry_init_kwargs["traces_sample_rate"], 0.15)

    def test_telemetry_init_gets_env_sample_rate(self, mocked_sentry_init):
        observe_sentry.init()
        _, sentry_init_kwargs = mocked_sentry_init.call_args
        self.assertEqual(sentry_init_kwargs["traces_sample_rate"], 0.25)

    def test_telemetry_init_gets_default_sample_rate(self, mocked_sentry_init):
        copied_env = dict(os.environ)
        copied_env.pop("SENTRY_SAMPLE_RATE")
        with mock.patch.dict("os.environ", copied_env, clear=True):
            observe_sentry.init()
        _, sentry_init_kwargs = mocked_sentry_init.call_args
        self.assertEqual(sentry_init_kwargs["traces_sample_rate"], 1.0)

    def test_telemetry_init_sets_global_variables(self, _):
        observe_sentry.init(raise_internal_exceptions=True)
        self.assertTrue(observe_sentry._INITIALIZED)
        self.assertTrue(observe_sentry._RAISES_EXCEPTIONS)


@mock.patch.dict("os.environ", {"SENTRY_DSN": "", "SENTRY_SAMPLE_RATE": "0.25"})
@mock.patch("sentry_sdk.init", mocked_sentry_init)
@mock.patch("sentry_sdk.Hub")
class TestTelemetryEventIntegration(unittest.TestCase):

    def setUp(self):
        self.event_run = False

        @events.event()
        def dummy_event(*args, **kwargs):
            self.event_run = True

        @events.event()
        def dummy_event_fail(*args, **kwargs):
            raise Exception("oops!")

        self.dummy_event = dummy_event
        self.dummy_event_fail = dummy_event_fail

    def tearDown(self):
        observe_sentry._INITIALIZED = False
        self.event_run = False
        events.clear_global_event_callbacks(events.EventStatus.ABOUT_TO_RUN)
        events.clear_global_event_callbacks(events.EventStatus.COMPLETED)
        events.clear_global_event_callbacks(events.EventStatus.CRASHED)

    @mock.patch("observe_sentry.EventIntegration.setup_once")
    def test_sanity_check_integration_setup_run_in_mock(self, setup, _):
        observe_sentry.init(raise_internal_exceptions=True)
        setup.assert_called()

    @mock.patch("sentry_sdk.start_transaction")
    def test_new_transaction_on_event_trigger(self, start, hub):
        hub.current.scope.span = None
        observe_sentry.init(raise_internal_exceptions=True)
        self.dummy_event()
        start.assert_called()
        start().__enter__().__exit__.assert_called_once()

    def test_child_span_on_event_trigger(self, hub):
        observe_sentry.init(raise_internal_exceptions=True)
        self.dummy_event()
        start_child = hub.current.scope.span.start_child
        start_child.assert_called()
        start_child().__enter__().__exit__.assert_called_once()

    @mock.patch("observe_sentry.get_telemetry_logger")
    def test_event_integration_logging_success(self, logger, _):
        info = logger().info
        observe_sentry.init(raise_internal_exceptions=True)
        self.dummy_event(1, 2, 3, four=4)
        # two calls, one for about_to_run, one for completed
        self.assertEqual(info.call_count, 2)
        args, kwargs = info.call_args
        self.assertIn("dummy_event", args[0])
        extra_arg = kwargs["extra"]

        self.assertEqual(extra_arg, extra_arg | {"args": (1, 2, 3), "kwargs": {"four": 4}})

    @mock.patch("observe_sentry.get_telemetry_logger")
    def test_event_integration_logging_error(self, logger, _):
        warning = logger().warning
        observe_sentry.init(raise_internal_exceptions=True)
        with self.assertRaises(Exception):
            self.dummy_event_fail(1, 2, 3, four=4)
        warning.assert_called()
        args, kwargs = warning.call_args
        self.assertIn("dummy_event", args[0])
        self.assertIn("Exception: oops!", args[0])
        extra_arg = kwargs["extra"]
        self.assertEqual(extra_arg, extra_arg | {"args": (1, 2, 3), "kwargs": {"four": 4}})

    @mock.patch("sentry_sdk.capture_exception")
    def test_event_exception_captured_when_raised_in_nested_event(self, capture, _):
        observe_sentry.init(raise_internal_exceptions=True)
        @events.event()
        def dummy_fail_parent():
            self.dummy_event_fail()
        with self.assertRaises(Exception):
            dummy_fail_parent()
        capture.assert_called()

    @mock.patch("sentry_sdk.start_transaction")
    def test_event_tag_update_propagates_to_sentry(self, transaction, hub):
        hub.current.scope.span = None
        observe_sentry.init(raise_internal_exceptions=True)

        @events.event()
        def dummy_event_set_tags():
            dummy_event_set_tags["key"] = "value"

        dummy_event_set_tags()
        transaction().__enter__().set_tag.assert_called_with("key", "value")


@observe_sentry.count_calls
def increment_func():
    pass


class TestIncrementDecorator(unittest.TestCase):

    def setUp(self):
        observe_sentry._RAISES_EXCEPTIONS = False

    @observe_sentry.count_calls
    def increment_method(self):
        pass

    @classmethod
    @observe_sentry.count_calls
    def increment_class_method(cls):
        pass

    @staticmethod
    @observe_sentry.count_calls
    def increment_static_method():
        pass

    @mock.patch("sentry_sdk.Hub")
    def test_increment_decorator_func(self, hub):
        tags = hub.current.scope.span._tags = dict()
        call_count_id = observe_sentry.call_count_tag_format(increment_func)

        # sanity check
        with self.assertRaises(KeyError):
            tags[call_count_id]

        increment_func()
        increment_func()
        increment_func()
        self.assertEqual(tags[call_count_id], "3")

    @mock.patch("sentry_sdk.Hub")
    def test_increment_decorator_method(self, hub):
        tags = hub.current.scope.span._tags = dict()
        call_count_id = observe_sentry.call_count_tag_format(self.increment_method)

        # sanity check
        with self.assertRaises(KeyError):
            tags[call_count_id]

        self.increment_method()
        self.increment_method()
        self.increment_method()
        self.increment_method()
        self.assertEqual(tags[call_count_id], "4")

    @mock.patch("sentry_sdk.Hub")
    def test_increment_decorator_class_method(self, hub):
        tags = hub.current.scope.span._tags = dict()
        call_count_id = observe_sentry.call_count_tag_format(self.increment_class_method)

        # sanity check
        with self.assertRaises(KeyError):
            tags[call_count_id]

        self.increment_class_method()
        self.increment_class_method()
        self.increment_class_method()
        self.increment_class_method()
        self.increment_class_method()
        self.assertEqual(tags[call_count_id], "5")

    @mock.patch("sentry_sdk.Hub")
    def test_increment_decorator_static_method(self, hub):
        tags = hub.current.scope.span._tags = dict()
        call_count_id = observe_sentry.call_count_tag_format(self.increment_static_method)

        # sanity check
        with self.assertRaises(KeyError):
            tags[call_count_id]

        self.increment_static_method()
        self.increment_static_method()
        self.increment_static_method()
        self.increment_static_method()
        self.increment_static_method()
        self.increment_static_method()
        self.assertEqual(tags[call_count_id], "6")

if __name__ == "__main__":
    unittest.main()