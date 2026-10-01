# Copyright 2026 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression tests for https://github.com/google/go-jsonnet/issues/484

Loading the bindings starts the Go runtime and its threads, so a process
created with fork() afterwards inherits the Go scheduler's state without the
threads backing it. Evaluating Jsonnet in such a child used to hang forever
with no CPU usage, waiting for a stop-the-world garbage collection.

These tests cover the actual trigger, which is "fork() after the Go runtime is
up" rather than the start method itself:

  * EAGER + fork        -- parent imported gojsonnet, then forked. Must fail
                           fast with an actionable error.
  * EAGER + forkserver  -- the forkserver helper is made to load gojsonnet
                           (this is what happens in real programs, because
                           multiprocessing preloads __main__ into it), then
                           forks. Must also fail fast.
  * DEFER + fork        -- parent never started the Go runtime, so the child
                           initialises it itself. Must still work.
  * EAGER + spawn       -- spawn exec()s a fresh interpreter, so the runtime is
                           always started in the new process. Must still work.

Both entry points are covered, evaluate_snippet and evaluate_file, because
jsonnet_evaluate_file reads the entire input file before it reaches any guard in
c-bindings.go. A large enough file therefore used to hang in a forked child even
once evaluate_snippet had been made to fail fast.

Each case runs in its own subprocess. That is required rather than merely
convenient: once this process has imported gojsonnet the runtime stays up, so a
DEFER case cannot be run in-process, and a pre-fix hang would otherwise wedge
the whole test session instead of failing one test.
"""

import multiprocessing
import os
import re
import subprocess
import sys
import tempfile
import unittest

# Large enough (~8 MB of JSON) that evaluating it provokes a Go GC cycle, which
# is what the stop-the-world waits for. Small snippets do not hang even without
# the fix, so they would not exercise the bug.
BIG_SNIPPET = 'local s = "abcdefghij"; [ s for i in std.range(1, 500000) ]'

# Same threshold for the evaluate_file case, which additionally has to allocate
# the whole file in jsonnet_evaluate_file. Measured: 1 MB completed, 8 MB hung.
BIG_FILE_BYTES = 8 * 1024 * 1024

# Substring of the message the bindings raise when a callable cannot be
# delegated to a helper process.
CALLBACK_ERROR_MARKER = "cannot be used when evaluating in a process created by"

# Length of the output the big inputs above are expected to produce. Delegation
# must not truncate or alter it.
EXPECTED_BIG_LEN = 8500003

# Generous: a correctly-behaving child finishes in well under a second, while
# a regressed one hangs forever, so this only ever fires on failure.
TIMEOUT_SECONDS = 120

# The environment variable that opts in to the post-fork callback bridge.
OPT_IN_ENV = "GOJSONNET_FORKEXEC_CALLBACKS"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if os.environ.get("GOJSONNET_FORK_TEST_EAGER") == "1":
    # The import itself is the thing under test: it is what starts the Go
    # runtime in the process that goes on to fork.
    import _gojsonnet  # noqa: F401


def _write_big_file():
    """Write a Jsonnet file big enough to provoke a GC cycle when read.

    The file is a single long string literal: a trivial AST, but reading it
    still has to allocate BIG_FILE_BYTES before any evaluation begins.
    """
    fd, path = tempfile.mkstemp(prefix="gojsonnet-fork-", suffix=".jsonnet")
    with os.fdopen(fd, "w") as handle:
        handle.write('{ a: "' + "x" * BIG_FILE_BYTES + '" }\n')
    return path


def _unlink(path):
    if path is not None:
        try:
            os.unlink(path)
        except OSError:
            pass


def _child():
    import _gojsonnet

    fired = []

    entry = os.environ.get("GOJSONNET_FORK_TEST_ENTRY", "snippet")
    path = _write_big_file() if entry == "file" else None
    try:
        if entry == "callbacks":
            _gojsonnet.evaluate_snippet(
                "cb.jsonnet", '{ x: std.native("f")(1) }',
                native_callbacks={"f": (("a",), lambda a: a)})
        elif entry == "error":
            _gojsonnet.evaluate_snippet("e.jsonnet", 'error "expected boom"')
        elif entry == "callback_ok":
            out = _gojsonnet.evaluate_snippet(
                "cb.jsonnet", '{ x: std.native("add")(2, 3) }',
                native_callbacks={"add": (("a", "b"), lambda a, b: a + b)})
        elif entry == "callback_import":
            def importer(base, rel):
                if rel == "lib.libsonnet":
                    return ("lib.libsonnet", b'{ v: 21 }')
                return ("/nonexistent", b"")
            out = _gojsonnet.evaluate_snippet(
                "cb.jsonnet", 'local l = import "lib.libsonnet"; { v: l.v }',
                import_callback=importer)
        elif entry == "callback_multi":
            fired.append("a")
            out = _gojsonnet.evaluate_snippet(
                "cb.jsonnet", '{ s: std.native("a")() + std.native("b")() }',
                native_callbacks={"a": ((), lambda: fired.append("a") or 10),
                                  "b": ((), lambda: fired.append("b") or 32)})
        elif entry == "callback_raises":
            def boom():
                raise ValueError("callback exploded")
            out = _gojsonnet.evaluate_snippet(
                "cb.jsonnet", '{ x: std.native("b")() }',
                native_callbacks={"b": ((), boom)})
        elif entry == "callback_leak":
            for _ in range(5):
                out = _gojsonnet.evaluate_snippet(
                    "cb.jsonnet", '{ x: std.native("add")(1, 1) }',
                    native_callbacks={"add": (("a", "b"), lambda a, b: a + b)})
        elif entry == "callback_helper_crash":
            import _forkexec
            crasher = os.path.join(os.path.dirname(_forkexec._WORKER),
                                   "_forkexec_test_crasher.py")
            with open(crasher, "w") as handle:
                handle.write("import os, signal\n"
                             "os.kill(os.getpid(), signal.SIGKILL)\n")
            _forkexec._WORKER = crasher
            try:
                out = _gojsonnet.evaluate_snippet(
                    "cb.jsonnet", '{ x: std.native("add")(1, 1) }',
                    native_callbacks={"add": (("a", "b"), lambda a, b: a + b)})
            finally:
                try:
                    os.unlink(crasher)
                except OSError:
                    pass
        elif entry == "callback_timeout":
            out = _gojsonnet.evaluate_snippet(
                "cb.jsonnet", '{ x: std.native("slow")() }',
                native_callbacks={"slow": ((), lambda: __import__("time")
                                           .sleep(5))})
        elif path is not None:
            out = _gojsonnet.evaluate_file(path)
        else:
            out = _gojsonnet.evaluate_snippet("big.jsonnet", BIG_SNIPPET)
    except BaseException as exc:  # noqa: BLE001
        sys.stdout.write("CHILD_ERROR %s\n" % (exc,))
        sys.stdout.flush()
        # Unlink before exiting: os._exit does not run finally blocks, so the
        # error path would otherwise strand the file in the temporary
        # directory on every failing run.
        _unlink(path)
        os._exit(1)
    finally:
        # A child that wedges leaves the file behind, in the system temporary
        # directory, where the OS reclaims it.
        _unlink(path)
    if entry.startswith("callback"):
        sys.stdout.write("CHILD_VALUE %s\n"
                         % " ".join(out.split()))
        if fired:
            sys.stdout.write("CHILD_FIRED %s\n" % ",".join(sorted(set(fired))))
        if entry == "callback_leak":
            import threading
            sys.stdout.write("CHILD_THREADS %d\n" % threading.active_count())
    sys.stdout.write("CHILD_POST_FORK %s\n" % _gojsonnet.is_post_fork())
    sys.stdout.write("CHILD_OK %d\n" % len(out))
    sys.stdout.flush()
    # os._exit skips multiprocessing's atexit teardown, which can itself block
    # in a forked child for reasons unrelated to this test.
    os._exit(0)


def _drive():
    method = os.environ["GOJSONNET_FORK_TEST_METHOD"]
    if method == "forkserver":
        # multiprocessing only preloads __main__ into the forkserver, which in
        # a real program is the module that imported gojsonnet. Under pytest
        # __main__ is pytest, so ask for the preload explicitly to reproduce
        # the real-world condition deterministically.
        multiprocessing.set_forkserver_preload(["_gojsonnet"])
    ctx = multiprocessing.get_context(method)
    proc = ctx.Process(target=_child, daemon=True)
    proc.start()
    proc.join()
    return proc.exitcode


class ForkSafetyTests(unittest.TestCase):
    def _run(self, method, eager, entry="snippet", extra_env=None):
        env = dict(os.environ)
        env["GOJSONNET_FORK_TEST_METHOD"] = method
        env["GOJSONNET_FORK_TEST_EAGER"] = "1" if eager else "0"
        env["GOJSONNET_FORK_TEST_ENTRY"] = entry
        # Remove any inherited opt-in so each case decides for itself.
        env.pop(OPT_IN_ENV, None)
        env.update(extra_env or {})
        env["PYTHONPATH"] = os.pathsep.join(
            [REPO_ROOT, env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        try:
            return subprocess.run(
                [sys.executable, os.path.abspath(__file__)],
                env=env,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail(
                "%s with %s import did not finish within %ds; the child is "
                "deadlocked (see go-jsonnet issue #484)"
                % (method, "eager" if eager else "deferred", TIMEOUT_SECONDS)
            )

    def assertDelegates(self, method, entry="snippet", expected_len=None):
        """The child must evaluate successfully, by delegating to a helper."""
        proc = self._run(method, eager=True, entry=entry)
        self.assertEqual(
            proc.returncode, 0,
            "expected the forked child to evaluate successfully, got rc=%s\n%s%s"
            % (proc.returncode, proc.stdout, proc.stderr),
        )
        self.assertIn("CHILD_OK", proc.stdout)
        if expected_len is not None:
            self.assertIn(
                "CHILD_OK %d" % expected_len, proc.stdout,
                "delegated result was not passed through intact:\n%s" % proc.stdout,
            )

    def assertDelegatesWithError(self, method, entry, marker):
        proc = self._run(method, eager=True, entry=entry)
        self.assertEqual(
            proc.returncode, 1,
            "expected the forked child to report an error, got rc=%s\n%s%s"
            % (proc.returncode, proc.stdout, proc.stderr),
        )
        self.assertIn(
            marker, proc.stdout,
            "expected %r in the error, got:\n%s" % (marker, proc.stdout),
        )

    def assertWorks(self, method, entry="snippet"):
        proc = self._run(method, eager=True, entry=entry)
        self.assertEqual(
            proc.returncode, 0,
            "expected %s to keep working, got rc=%s\n%s%s"
            % (method, proc.returncode, proc.stdout, proc.stderr),
        )
        self.assertIn("CHILD_OK", proc.stdout)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_evaluate_snippet_after_fork_is_delegated(self):
        self.assertDelegates("fork", expected_len=EXPECTED_BIG_LEN)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_evaluate_file_after_fork_is_delegated(self):
        # evaluate_file reads the whole input before any guard in
        # c-bindings.go runs, so it needs its own case.
        proc = self._run("fork", eager=True, entry="file")
        self.assertEqual(
            proc.returncode, 0,
            "expected the forked child to evaluate successfully, got rc=%s\n%s%s"
            % (proc.returncode, proc.stdout, proc.stderr),
        )
        self.assertIn("CHILD_OK", proc.stdout)

    @unittest.skipUnless("forkserver" in multiprocessing.get_all_start_methods(),
                         "forkserver start method unavailable")
    def test_forkserver_with_gojsonnet_preloaded_is_delegated(self):
        self.assertDelegates("forkserver", expected_len=EXPECTED_BIG_LEN)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_evaluate_error_after_fork_is_reported(self):
        # An error from the helper must reach the caller unchanged.
        self.assertDelegatesWithError("fork", "error", "expected boom")

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_evaluate_callables_after_fork_are_rejected(self):
        # Callables cannot cross the process boundary; they must be refused
        # explicitly rather than silently ignored.
        self.assertDelegatesWithError("fork", "callbacks",
                                      CALLBACK_ERROR_MARKER)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_fork_without_go_runtime_in_parent_still_works(self):
        proc = self._run("fork", eager=False)
        self.assertEqual(
            proc.returncode, 0,
            "a child that initialises the Go runtime itself must work, got "
            "rc=%s\n%s%s" % (proc.returncode, proc.stdout, proc.stderr),
        )
        self.assertIn("CHILD_OK", proc.stdout)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_evaluate_file_without_go_runtime_in_parent_still_works(self):
        proc = self._run("fork", eager=False, entry="file")
        self.assertEqual(
            proc.returncode, 0,
            "evaluate_file in a child that initialises the Go runtime itself "
            "must work, got rc=%s\n%s%s"
            % (proc.returncode, proc.stdout, proc.stderr),
        )
        self.assertIn("CHILD_OK", proc.stdout)

    # ------------------------------------------------------------------
    # The post-fork callback bridge. Each of these only means anything in an
    # unsafe post-fork process, so they all run with an eager import and fork.

    def _run_bridged(self, entry, extra_env=None):
        env = {OPT_IN_ENV: "1"}
        env.update(extra_env or {})
        return self._run("fork", eager=True, entry=entry, extra_env=env)

    def _assert_value(self, proc, expected):
        self.assertEqual(proc.returncode, 0,
                         "expected the bridged evaluation to succeed, got "
                         "rc=%s\n%s%s"
                         % (proc.returncode, proc.stdout, proc.stderr))
        self.assertIn("CHILD_POST_FORK True", proc.stdout,
                      "the child must actually be in the unsafe post-fork "
                      "state for this test to mean anything:\n%s" % proc.stdout)
        self.assertIn("CHILD_VALUE %s" % expected, proc.stdout,
                      "expected the callback result to survive the bridge, "
                      "got:\n%s" % proc.stdout)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_native_callback_after_fork(self):
        proc = self._run_bridged("callback_ok")
        self._assert_value(proc, '{ "x": 5 }')

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_import_callback_after_fork(self):
        # Exercises the bytes hop across the bridge, not just JSON scalars.
        proc = self._run_bridged("callback_import")
        self._assert_value(proc, '{ "v": 21 }')

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_multiple_callbacks_after_fork(self):
        proc = self._run_bridged("callback_multi")
        self._assert_value(proc, '{ "s": 42 }')
        self.assertIn("CHILD_FIRED a,b", proc.stdout,
                      "both callbacks must run exactly once:\n%s" % proc.stdout)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_native_callback_exception_after_fork(self):
        proc = self._run_bridged("callback_raises")
        self.assertEqual(proc.returncode, 1,
                         "expected the callback failure to be reported, got "
                         "rc=%s\n%s%s"
                         % (proc.returncode, proc.stdout, proc.stderr))
        self.assertIn("CHILD_ERROR", proc.stdout)
        self.assertIn("failed to execute native callback", proc.stdout,
                      "expected the callback failure to reach the caller:\n%s"
                      % proc.stdout)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_callbacks_refused_without_opt_in(self):
        # The bridge must stay off unless asked for.
        proc = self._run("fork", eager=True, entry="callback_ok")
        self.assertEqual(proc.returncode, 1,
                         "without the opt-in callbacks must still be refused, "
                         "got rc=%s\n%s%s"
                         % (proc.returncode, proc.stdout, proc.stderr))
        self.assertIn("cannot be used when evaluating in a process created by",
                      proc.stdout,
                      "expected the documented refusal:\n%s" % proc.stdout)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_callback_bridge_does_not_leak_threads(self):
        proc = self._run_bridged("callback_leak")
        self.assertEqual(proc.returncode, 0,
                         "rc=%s\n%s%s"
                         % (proc.returncode, proc.stdout, proc.stderr))
        count = int(re.search(r"CHILD_THREADS (\d+)", proc.stdout).group(1))
        # One main thread plus at most one broker accept thread. Without the
        # accept loop honouring its stop flag this grows by one per evaluation.
        self.assertLessEqual(count, 2,
                             "broker threads accumulated across evaluations: "
                             "%d threads after 5 evaluations" % count)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_callback_bridge_leaves_nothing_behind(self):
        proc = self._run_bridged("callback_ok")
        self.assertEqual(proc.returncode, 0,
                         "rc=%s\n%s%s"
                         % (proc.returncode, proc.stdout, proc.stderr))
        stale = []
        for root, _dirs, files in os.walk(tempfile.gettempdir()):
            if "b.sock" in files:
                stale.append(os.path.join(root, "b.sock"))
        self.assertEqual(stale, [],
                         "the broker socket must not outlive the evaluation: %s"
                         % stale)
        ps = subprocess.run(["ps", "-eo", "command"], capture_output=True,
                            text=True).stdout
        workers = [line for line in ps.splitlines()
                   if "_forkexec_worker.py" in line
                   and "ps -eo" not in line]
        self.assertEqual(workers, [],
                         "no helper process may outlive the evaluation: %s"
                         % workers)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_callback_helper_crash_is_reported(self):
        proc = self._run_bridged("callback_helper_crash")
        self.assertEqual(proc.returncode, 1,
                         "a killed helper must be reported, not hang\n%s%s"
                         % (proc.stdout, proc.stderr))
        self.assertIn("without producing a result", proc.stdout,
                      "expected a deterministic helper-failure error:\n%s"
                      % proc.stdout)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_callback_timeout_is_reported(self):
        # The callback sleeps far longer than the timeout, so the timeout
        # always wins; no reliance on how fast the machine is.
        proc = self._run_bridged("callback_timeout",
                                 {"GOJSONNET_FORKEXEC_TIMEOUT": "1"})
        self.assertEqual(proc.returncode, 1,
                         "a timed-out helper must be reported, not hang\n%s%s"
                         % (proc.stdout, proc.stderr))
        self.assertIn("did not finish within", proc.stdout,
                      "expected the timeout to be reported:\n%s" % proc.stdout)

    @unittest.skipUnless("spawn" in multiprocessing.get_all_start_methods(),
                         "spawn start method unavailable")
    def test_spawn_still_works(self):
        self.assertWorks("spawn")

    @unittest.skipUnless("spawn" in multiprocessing.get_all_start_methods(),
                         "spawn start method unavailable")
    def test_spawn_still_works_with_evaluate_file(self):
        self.assertWorks("spawn", entry="file")


if __name__ == "__main__":
    sys.exit(_drive())
