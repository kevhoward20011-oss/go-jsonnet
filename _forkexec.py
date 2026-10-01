"""Delegate Jsonnet evaluation to a fresh helper process when this process
was created by fork() after the bindings were loaded.

Loading the bindings starts the Go runtime and its threads. A process created
by fork() afterwards inherits the Go scheduler's state but none of the threads
driving it, so evaluating there blocks forever at the first stop-the-world
garbage collection. The Go runtime cannot be restarted from inside a library,
so the only sound option is to evaluate in a process whose runtime was
initialised after an exec.

This module is called from python/_jsonnet.c when the C helper
jsonnet_internal_fork_safety_error() reports an unsafe process. The child's own
Python interpreter is healthy -- only its Go runtime is not -- so the child
acts purely as a broker: it serialises the request, execs a new interpreter,
and relays the result. It never enters the Go runtime itself.

Detection deliberately does not happen here. Deriving it in Python is unsafe:
multiprocessing's forkserver preload silently ignores modules it cannot import,
so this module may first be imported *inside* a forked child, which would
record the child's own pid and wrongly conclude it is safe. The decision is
made in C from a pid captured by a library constructor at load time.

Not supported by default: import_callback and native_callbacks. They are Python
callables and cannot cross a process boundary, so by default passing them raises
rather than silently dropping them. Evaluating in a parent process before
forking, or using a start method that execs, avoids the situation entirely.

They can optionally be supported by setting GOJSONNET_FORKEXEC_CALLBACKS=1, which
starts a local AF_UNIX server in this process and lets the helper call back into
it. In that mode the callback body still runs *here*, in the process that called
evaluate_* -- so os.getpid() is unchanged -- but it runs on a broker thread
rather than the Python thread that called evaluate_*, and threading.get_ident()
and threading.get_native_id() will differ. Anything relying on thread-local
state must not be used from a callback in that mode. If the bridge cannot be
started the evaluation fails; it never falls back to evaluating here.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKER = os.path.join(_HERE, "_forkexec_worker.py")

# How long a single delegated evaluation may take before the helper is killed.
TIMEOUT = float(os.environ.get("GOJSONNET_FORKEXEC_TIMEOUT", "120"))

# Opt-in: allow import_callback / native_callbacks to be used in a delegated
# evaluation, by calling back into this process over a local AF_UNIX socket.
CALLBACKS_ENABLED = os.environ.get("GOJSONNET_FORKEXEC_CALLBACKS") == "1"

# How often the broker's accept loop checks whether it has been asked to stop.
_ACCEPT_POLL = 0.2

# Mirrors the kwlist in python/_jsonnet.c. Used to flatten the original
# positional and keyword arguments so the helper can rebuild the call exactly.
_KWLIST = {
    "file": ("filename", "jpathdir", "max_stack", "gc_min_objects",
             "gc_growth_trigger", "ext_vars", "ext_codes", "tla_vars",
             "tla_codes", "max_trace", "import_callback", "native_callbacks"),
    "snippet": ("filename", "src", "jpathdir", "max_stack", "gc_min_objects",
                "gc_growth_trigger", "ext_vars", "ext_codes", "tla_vars",
                "tla_codes", "max_trace", "import_callback",
                "native_callbacks"),
}

# Serialisable as-is: plain strings, numbers, and nested dicts/lists of them.
_PLAIN = ("filename", "src", "max_stack", "gc_min_objects",
          "gc_growth_trigger", "ext_vars", "ext_codes", "tla_vars",
          "tla_codes", "max_trace")

# Only ever Python callables or containers of them.
_CALLBACKS = ("import_callback", "native_callbacks")


def _flatten(op, args, kwds):
    """Turn the original call into an ordered mapping of keyword names."""
    names = _KWLIST[op]
    if len(args) > len(names):
        raise TypeError("%s() takes at most %d arguments (%d given)"
                        % (op, len(names), len(args)))
    call = dict(zip(names, args))
    if kwds:
        for key, value in kwds.items():
            if key not in names:
                raise TypeError("%s() got an unexpected keyword argument %r"
                                % (op, key))
            if key in call:
                raise TypeError("%s() got multiple values for argument %r"
                                % (op, key))
            call[key] = value
    return call


class _Broker:
    """Serves import/native callback requests from the helper, over AF_UNIX.

    Runs entirely in this process -- the one whose Go runtime is broken -- and
    therefore never enters the Go runtime: it is plain Python and sockets. The
    callback bodies belong to this process too, so os.getpid() seen by a
    callback is the pid of the process that called evaluate_*.

    Handlers are daemon threads and are never joined, so a callback that blocks
    can delay neither the result nor process exit.
    """

    def __init__(self, path, import_callback, natives):
        self._import_callback = import_callback
        self._natives = natives or {}
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(64)
        # A blocking accept() is not interrupted by close() on Linux, which
        # would strand one _serve thread per delegated evaluation. Poll
        # instead, so the loop notices _stop within _ACCEPT_POLL seconds.
        self._sock.settimeout(_ACCEPT_POLL)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        try:
            # Bounded, so a handler can never block forever on a helper that
            # has died or been killed.
            conn.settimeout(TIMEOUT)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(1 << 20)
                if not chunk:
                    return
                buf += chunk
            request = json.loads(buf.decode())
            reply = self._dispatch(request)
        except BaseException:  # noqa: BLE001
            reply = {"ok": False, "error": "broker could not read a request"}
        try:
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass  # the helper is gone; nothing useful to report
        finally:
            conn.close()

    def _dispatch(self, request):
        op, args = request["op"], request.get("args", [])
        try:
            if op == "import":
                return self._import(args)
            if op.startswith("native:"):
                return self._native(op[len("native:"):], args)
            return {"ok": False, "error": "unknown broker op %r" % op}
        except BaseException as exc:  # noqa: BLE001
            # Match the bindings: exc_to_str() is str(exception).
            return {"ok": False, "error": str(exc)}

    def _import(self, args):
        result = self._import_callback(*args)
        if not isinstance(result, tuple) or len(result) != 2:
            return {"ok": False,
                    "error": "import_callback did not return (string, bytes)"}
        found, content = result
        return {"ok": True, "found": found,
                "content_b64": None if content is None
                else base64.b64encode(content).decode("ascii")}

    def _native(self, name, args):
        result = self._natives[name][1](*args)
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            return {"ok": False,
                    "error": "native callback returned a value that is not "
                             "JSON-representable: %s" % exc}
        return {"ok": True, "value": result}

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        # Join the accept loop so that, by the time delegate() returns, no
        # broker thread is still alive. Bounded, because the loop only has to
        # notice the flag within _ACCEPT_POLL. Handler threads are deliberately
        # not joined: one may still be running a user callback, and it is a
        # daemon thread that cannot delay exit.
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=_ACCEPT_POLL * 10)


def delegate(op, args=(), kwds=None):
    """Evaluate as requested in a fresh helper process. Returns the JSON text
    that evaluate_file/evaluate_snippet would have returned."""
    call = _flatten(op, args, kwds or {})

    wanted = [name for name in _CALLBACKS if call.get(name) is not None]
    if wanted and not CALLBACKS_ENABLED:
        raise RuntimeError(
            "%s=%r cannot be used when evaluating in a process created by "
            "fork() after the bindings were loaded: it is a Python callable "
            "and does not survive the process boundary. Either evaluate "
            "before forking and pass the result to the children, or use a "
            "multiprocessing start method that execs a new interpreter "
            "('spawn'). See https://github.com/google/go-jsonnet/issues/484."
            % (wanted[0], wanted[0]))

    request = {k: v for k, v in call.items() if k in _PLAIN}
    if call.get("jpathdir") is not None:
        request["jpathdir"] = call["jpathdir"]
    request["op"] = op

    # Fail here, with a useful message, rather than in the helper.
    json.dumps(request)

    with tempfile.TemporaryDirectory() as tmp:
        req_path = os.path.join(tmp, "request.json")
        resp_path = os.path.join(tmp, "response.json")
        broker = None
        if wanted:
            sock_path = os.path.join(tmp, "b.sock")
            request["broker"] = sock_path
            if "import_callback" in wanted:
                request["want_import"] = True
            if "native_callbacks" in wanted:
                request["want_native"] = sorted(call["native_callbacks"])
                request["native_params"] = {
                    name: list(spec[0])
                    for name, spec in call["native_callbacks"].items()}
            try:
                broker = _Broker(sock_path, call.get("import_callback"),
                                 call.get("native_callbacks"))
            except BaseException as exc:  # noqa: BLE001
                # Fail closed: never fall back to evaluating here, where the Go
                # runtime would deadlock.
                raise RuntimeError(
                    "could not start the post-fork callback bridge at %s "
                    "(%s: %s); the evaluation was not delegated"
                    % (sock_path, type(exc).__name__, exc)) from exc
            with open(req_path, "w") as handle:
                json.dump(request, handle)
        else:
            with open(req_path, "w") as handle:
                json.dump(request, handle)

        env = dict(os.environ)
        # The helper imports the bindings itself; make sure it can find them.
        env["PYTHONPATH"] = os.pathsep.join(
            [p for p in sys.path if p] + [env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        try:
            try:
                proc = subprocess.run(
                    [sys.executable, _WORKER, req_path, resp_path],
                    capture_output=True, text=True, timeout=TIMEOUT,
                    check=False, env=env,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    "the helper process did not finish within %gs and was "
                    "killed; the Jsonnet program may not terminate" % TIMEOUT)
            except OSError as exc:
                raise RuntimeError(
                    "could not start the helper process (%s: %s); the "
                    "evaluation was not delegated" % (type(exc).__name__, exc))
            if not os.path.exists(resp_path):
                raise RuntimeError(
                    "the helper process exited with status %s without "
                    "producing a result (stderr: %s)"
                    % (proc.returncode,
                       (proc.stderr or "<empty>").strip()[-500:]))
            with open(resp_path) as handle:
                response = json.load(handle)
        finally:
            if broker is not None:
                broker.close()
            # TemporaryDirectory removes the AF_UNIX socket file with the rest.

    if response.get("ok"):
        return response["out"]
    if response.get("kind") == "runtime":
        raise RuntimeError(response["err"])
    raise RuntimeError("the helper process failed (%s): %s"
                       % (response.get("kind"), response.get("err")))