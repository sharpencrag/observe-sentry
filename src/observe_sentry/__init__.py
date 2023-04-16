import functools
import os
import sys
import platform
import logging

from observatory import events

import sentry_sdk
from sentry_sdk.integrations import logging as sentry_logging
from sentry_sdk import set_tag, set_user, set_context


@functools.lru_cache()
def get_telemetry_logger():
    return logging.getLogger(__file__)


#: bool: True if the telemetry instrumentation has been set up
_INITIALIZED = False

#: bool: True if exceptions will be raised by internal telemetry functions
_RAISES_EXCEPTIONS = False

#: float: The percentage of event / error reports that will be sent to sentry
DEFAULT_SAMPLE_RATE = 1.0

#: logging.Logger: the logger for telemetry
_LOGGER = None


def call_count_tag_format(func):
    """Formats a function's name for tagging in Sentry."""
    return "{func} calls".format(func=func.__name__)


def count_calls(func):
    """DECORATOR: counts the number of times a function is called.

    If a sentry session has been initialized, the count will be sent to sentry
    as a tag, associated with the currently-executing transaction.

    """
    @functools.wraps(func)
    def _wrapped_func(*args, **kwargs):
        tag_id = call_count_tag_format(func)
        current_transaction = sentry_sdk.Hub.current.scope.span
        current_count = int(current_transaction._tags.get(tag_id, 0))
        current_transaction._tags[tag_id] = str(current_count + 1)
        return func(*args, **kwargs)
    return _wrapped_func


def _telemetry(func):
    """DECORATOR: Marks a callable as an internal telemetry action.

    This allows exceptions to be logged and ignored rather than raised.
    """
    @functools.wraps(func)
    def _wrapped_func(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            if _RAISES_EXCEPTIONS:
                raise
            else:
                logger = get_telemetry_logger()
                logger.error("Internal telemetry function failed: {func}"
                             "".format(func=func))
    return _wrapped_func


def init(sentry_dns=None, sample_rate=None, raise_internal_exceptions=False,
         integrations=None, logger=None):
    """Initializes sentry and does additional setup for internal use.

    Args:

        sentry_dns (str, optional): Credentialed URL for a specific sentry
            Project. If not provided, the DNS will be obtained from an
            environment variable. Defaults to None.

        sample_rate (float, optional): Percentage of events (both errors and
            transactions) to be uploaded to sentry. If not provided, the sample
            rate will be set from an environment variable. Defaults to None.

        raise_internal_exceptions (bool, optional): True if errors in internal
            telemetry functions should raise exceptions. If False, errors will
            be logged but exceptions will NOT be raised. Defaults to False.

        integrations (list[sentry_sdk.integrations.Integration], optional):
            Integrations to add to the default sentry and event integrations.
            Defaults to None.

        logger (logging.Logger, optional): A logger to use when tracing events.
            If no logger is provided, a default one from this module will be
            used.

    Raises:

        TelemetryError: If telemetry is already initialized

        TelemetryError: If sentry_sdk.init fails
    """
    # ----------------------------------------------------------------- Setup #
    logger = logger or get_telemetry_logger()


    # add default event integration
    integrations = integrations or list()
    integrations.append(EventIntegration())


    failed_init_msg = "Telemetry could not be initialized."

    global _INITIALIZED
    global _RAISES_EXCEPTIONS
    global _LOGGER
    _RAISES_EXCEPTIONS = raise_internal_exceptions
    _LOGGER = logger
    sentry_logging.DEFAULT_EVENT_LEVEL = logging.CRITICAL

    set_user({"username": platform.node()})

    # -------------------------------------------------- Initialization Check #
    if _INITIALIZED is True:
        already_init_msg = failed_init_msg + " Already initialized."
        if raise_internal_exceptions:
            raise TelemetryError(already_init_msg)
        else:
            logger.warning(already_init_msg + " Ingoring new initialization.")
            return

    # ----------------------------------------------------------- Sentry Init #
    try:
        sentry_dns = sentry_dns or os.environ["SENTRY_DNS"]
    except KeyError:
        raise TelemetryError(failed_init_msg + " No sentry DNS found!")

    # the sample rate defaults to the value at the top of this module
    sample_rate = sample_rate or float(os.environ.get("SENTRY_SAMPLE_RATE", 1.0))

    try:
        sentry_sdk.init(sentry_dns, traces_sample_rate=sample_rate,
                        integrations=integrations)
    except Exception:
        logger.error(failed_init_msg + " Internal sentry init error.")
        if raise_internal_exceptions:
            raise
        else:
            return

    # -------------------------------------------------------------- Success! #
    _INITIALIZED = True


class EventIntegration(sentry_sdk.integrations.Integration):
    """Tracks events through logging and sentry.io's performance monitoring."""
    identifier = "_event"

    @classmethod
    def setup_once(cls):
        # ---------------------------------------------- Performance Tracking #
        events.add_global_event_callback(
            events.EventStatus.ABOUT_TO_RUN, cls.begin_sentry_trace
        )

        # ----------------------------------------------------------- Logging #
        # Logging also adds breadcrumbs to the sentry performance and error
        # tracking systems.

        events.add_global_event_callback(
            events.EventStatus.ABOUT_TO_RUN,
            cls.log_it("about to run...", _LOGGER.info)
        )
        events.add_global_event_callback(
            events.EventStatus.COMPLETED,
            cls.log_it("completed", _LOGGER.info)
        )
        events.add_global_event_callback(
            events.EventStatus.CRASHED,
            cls.log_it("crashed!", _LOGGER.warning)
        )

    @staticmethod
    @_telemetry
    def begin_sentry_trace(enter_event_data):
        """Begin tracing an event as a sentry transaction.

        Args:
            enter_event_data (events.EventData): Data object for the
                currently-evaluating event.
        """
        event = enter_event_data.event

        current_transaction = sentry_sdk.Hub.current.scope.span

        # the `__enter__` calls below are necessary to set the transaction scope

        if current_transaction is None:
            transaction = sentry_sdk.start_transaction(
                op=event.name, name=event.name).__enter__()
        elif event.elevated:
            transaction = sentry_sdk.start_transaction(
                trace_id=current_transaction.trace_id,
                parent_span_id=current_transaction.span_id,
                op=event.name, name=event.name).__enter__()
        else:
            transaction = current_transaction.start_child(
                op=event.name).__enter__()

        @_telemetry
        def _finish(exit_event_data):

            # every time an event is called, a new event_data object gets
            # created.  This check is required in the (unlikely, but possible)
            # case that an event calls itself recursively.
            if not enter_event_data is exit_event_data:
                return

            exc_info = sys.exc_info()

            err = exc_info[1]
            if err is None:
                transaction.set_status("ok")
            else:
                sentry_sdk.capture_exception(err)
            for tag, tag_value in exit_event_data.tags.items():
                transaction.set_tag(tag, tag_value)
            transaction.__exit__(*exc_info)
            event.exited.disconnect(_finish)

        event.exited.connect(_finish)

    @staticmethod
    @_telemetry
    def log_it(status_msg, log_func):
        """Generates a log callback for the given message.

        Args:
            status_msg (str): Message to include after the name of the event.
            log_func (callable): logging function that accepts a string.
        """
        def log_callback(event_data):
            message = "'{name}' {status}".format(
                name=event_data.name, status=status_msg
            )
            if event_data.crashed:
                message = "{msg} <{exc}>".format(
                    msg=message, exc=event_data.exc_desc
                )
            extra = {"args": event_data.args, "kwargs": event_data.kwargs}
            extra.update(event_data.tags)
            log_func(message, extra=extra)
        return log_callback


class TelemetryError(Exception):
    """Raised when a telemetry-specific exception has been thrown."""
    pass
