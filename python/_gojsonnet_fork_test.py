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

Each case runs in its own subprocess. That is required rather than merely
convenient: once this process has imported gojsonnet the runtime stays up, so a
DEFER case cannot be run in-process, and a pre-fix hang would otherwise wedge
the whole test session instead of failing one test.
"""

import multiprocessing
import os
import subprocess
import sys
import unittest

# Large enough (~8 MB of JSON) that evaluating it provokes a Go GC cycle, which
# is what the stop-the-world waits for. Small snippets do not hang even without
# the fix, so they would not exercise the bug.
BIG_SNIPPET = 'local s = "abcdefghij"; [ s for i in std.range(1, 500000) ]'

# Substring of the error the bindings are expected to raise in a forked child.
FORK_ERROR_MARKER = "created with fork() from a process which had already"

# Generous: a correctly-behaving child finishes in well under a second, while
# a regressed one hangs forever, so this only ever fires on failure.
TIMEOUT_SECONDS = 120

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if os.environ.get("GOJSONNET_FORK_TEST_EAGER") == "1":
    # The import itself is the thing under test: it is what starts the Go
    # runtime in the process that goes on to fork.
    import _gojsonnet  # noqa: F401


def _child():
    import _gojsonnet

    try:
        out = _gojsonnet.evaluate_snippet("big.jsonnet", BIG_SNIPPET)
    except BaseException as exc:  # noqa: BLE001
        sys.stdout.write("CHILD_ERROR %s\n" % (exc,))
        sys.stdout.flush()
        os._exit(1)
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
    def _run(self, method, eager):
        env = dict(os.environ)
        env["GOJSONNET_FORK_TEST_METHOD"] = method
        env["GOJSONNET_FORK_TEST_EAGER"] = "1" if eager else "0"
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

    def assertFailsFast(self, method):
        proc = self._run(method, eager=True)
        self.assertEqual(
            proc.returncode, 1,
            "expected the forked child to report an error, got rc=%s\n%s%s"
            % (proc.returncode, proc.stdout, proc.stderr),
        )
        self.assertIn(
            FORK_ERROR_MARKER, proc.stdout,
            "expected an actionable fork-safety error, got:\n%s" % proc.stdout,
        )

    def assertWorks(self, method):
        proc = self._run(method, eager=True)
        self.assertEqual(
            proc.returncode, 0,
            "expected %s to keep working, got rc=%s\n%s%s"
            % (method, proc.returncode, proc.stdout, proc.stderr),
        )
        self.assertIn("CHILD_OK", proc.stdout)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                         "fork start method unavailable")
    def test_fork_after_go_runtime_initialised_fails_fast(self):
        self.assertFailsFast("fork")

    @unittest.skipUnless("forkserver" in multiprocessing.get_all_start_methods(),
                         "forkserver start method unavailable")
    def test_forkserver_with_gojsonnet_preloaded_fails_fast(self):
        self.assertFailsFast("forkserver")

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

    @unittest.skipUnless("spawn" in multiprocessing.get_all_start_methods(),
                         "spawn start method unavailable")
    def test_spawn_still_works(self):
        self.assertWorks("spawn")


if __name__ == "__main__":
    sys.exit(_drive())
