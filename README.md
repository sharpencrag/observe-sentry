# observe-sentry

Automatic `sentry.io` performance and error tracking through the `observatory` event system!

NOTE: this code is a work-in-progress, and should not be relied on for any production code.

Before Running
--------------

Two environment variables must be set before using this utility:

- `SENTRY_DNS`, a credentialed URL used to connect to our Sentry account.  This
  value can be found on the sentry site under Settings|Projects|Instrumentation

- `SENTRY_SAMPLE_RATE`, a 0 to 1 floating point number representing the
  percentage of sentry events (errors and transactions) to send to sentry.io.


Usage
-----

Setting up basic telemetry tracking is extremely easy.  In most cases, it just
requires two lines of code at the top level of an application:
```python
import observe_sentry
observe_sentry.init()
```

The `init` function provides additional configuration options, but the defaults are good
in most cases.

For debugging, you may wish to turn on exceptions for telemetry functions:
```python
observe_sentry.init(raise_internal_exceptions=True)
```
This will allow any exceptions thrown by the telemetry system itself to be
raised (thereby exiting the current process).  By default, any errors in the
telemetry system are logged and ignored.


Event Tracking
--------------

By default, `observe_sentry.init` sets up performance tracking for any configured
`Event`s that get called after initialization.  See `observatory.events` for more
details on creating and using `Event`s.

For every event, a log entry and sentry breadcrumb are created when the event
is run and after it completes or crashes.

Additionally, every event is sent to sentry.io as a transaction, with any
events it calls in turn sent as spans.
