# RPC trace snapshot resource lifecycle

The optional graph tracing collector creates a Babeltrace reader for each
finite LTTng snapshot. In Babeltrace 2.1.2, the reader owns an internal graph,
and that graph owns a callback bound to the reader:

```text
iterator -> graph -> bound callback -> iterator
```

Finishing the Python loop did not immediately destroy this reference cycle.
Automatic garbage collection could reclaim it, but did not run before the
process exhausted its file-descriptor limit. On the Station deployment checked
November 15, 2026, the collector had 1,023 descriptors against a soft limit of
1,024, including 1,000 handles to deleted trace files, and reported
`Too many open files`.

For example, if each reader retains eight trace handles, 100 successive reads
retain about 800 extra handles. Deleting a snapshot directory only removes its
directory entries; open descriptors still retain the underlying files. Keeping
only two visible snapshot directories therefore did not bound retained resources.

Early exits had an additional problem. Hitting the processing limit or raising
an exception left a partially consumed iterator with native message buffers.
Dropping Python references and collecting cycles closed files, but exhausting
the remaining finite iterator was also needed to release those buffers.

The backport from [ros2graph](https://github.com/vincent-tvincent/ros2graph/commit/603ef3048fc3a3b194d09ff71427bbb71d251f39)
changes only this resource lifecycle:

1. Process events in a separate helper frame so message/event/field wrappers
   can be released before finalizing the reader.
2. On an early exit, drain the remaining bounded snapshot through the public
   iterator API. Discarded records never enter the measurement counters.
3. Clear completed exception frames that retain the iterator or constructor.
4. Drop the iterator reference and collect its graph/listener cycle in the
   dedicated collector process. Babeltrace exposes no public `close()` for this
   iterator.

Station's tracing scope, paths, initialization mappings, controller and browser
interfaces are unchanged. A normal Station restart loads the fix and preserves
trace-before-node initialization order. Restarting only the collector would
lose mappings for already-running ROS nodes.

The Station backport passed 51 focused Python checks and four subtests,
including normal completion, processing-limit exits, decoder errors and
constructor errors. Regression tests disable automatic garbage collection to
verify immediate cleanup and prove drained records are not counted.

The same final reader logic passed 660 actual Babeltrace decodes across normal,
early-limit and injected-error paths. Descriptor counts stayed fixed and native
heap stayed around 5 MB after warmup. A separate live ros2graph recording stayed
at six descriptors, with no deleted trace handles or trace loss, for 791.5 seconds.
These tests are separate from the production Station deployment check.
