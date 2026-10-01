"""Fresh-process Jsonnet evaluator.

Run as a script by _forkexec.py:

    python3 _forkexec_worker.py <request.json> <response.json>

This process was started with exec, so the bindings load here for the first
time and the Go runtime is initialised after the exec. It is therefore sound,
unlike a process created by fork() from one that had already loaded them.

The result goes in the response file rather than on stdout so that anything the
evaluator prints cannot corrupt it.
"""

import base64
import json
import socket
import sys
import traceback

# Request keys that map straight onto the bindings' keyword arguments.
_PLAIN = ("filename", "src", "jpathdir", "max_stack", "gc_min_objects",
          "gc_growth_trigger", "ext_vars", "ext_codes", "tla_vars",
          "tla_codes", "max_trace")


def _call_broker(path, op, args, timeout):
    """One request/response round trip to the process that called us."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall((json.dumps({"op": op, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(1 << 20)
            if not chunk:
                raise RuntimeError(
                    "the post-fork callback bridge closed the connection")
            buf += chunk
    reply = json.loads(buf.decode())
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "callback failed"))
    return reply


def _import_proxy(path, timeout):
    def callback(base, rel):
        reply = _call_broker(path, "import", [base, rel], timeout)
        content = reply.get("content_b64")
        return (reply.get("found"),
                None if content is None else base64.b64decode(content))
    return callback


def _native_proxy(path, name, timeout):
    def callback(*args):
        return _call_broker(path, "native:" + name, list(args),
                            timeout)["value"]
    return callback


def main():
    request_path, response_path = sys.argv[1], sys.argv[2]
    with open(request_path) as handle:
        request = json.load(handle)

    response = {"ok": False}
    try:
        import _gojsonnet

        # filename/src are passed positionally below, so keep them out of the
        # keyword arguments even though they are part of the request.
        positional = ("filename", "src")
        kwargs = {key: request[key] for key in _PLAIN
                  if key in request and key not in positional}

        # Callbacks arrive as proxies so that the callables themselves stay in
        # the process that owns them. These proxies are what the bindings
        # invoke, and they run on the thread that called evaluate_*, so the
        # bindings' own thread-state requirement is satisfied here.
        timeout = request.get("broker_timeout", 300)
        if request.get("want_import"):
            kwargs["import_callback"] = _import_proxy(request["broker"],
                                                       timeout)
        for name in request.get("want_native", []):
            kwargs.setdefault("native_callbacks", {})
            kwargs["native_callbacks"][name] = (
                tuple(request["native_params"][name]),
                _native_proxy(request["broker"], name, timeout),
            )

        if request["op"] == "snippet":
            out = _gojsonnet.evaluate_snippet(
                request["filename"], request["src"], **kwargs)
        elif request["op"] == "file":
            out = _gojsonnet.evaluate_file(request["filename"], **kwargs)
        else:
            raise ValueError("unknown op %r" % (request["op"],))
        response = {"ok": True, "out": out}
    except RuntimeError as exc:
        # Same exception type and message the bindings raise in-process, so
        # callers cannot tell whether they were routed through a helper.
        response = {"ok": False, "kind": "runtime", "err": str(exc)}
    except BaseException as exc:  # noqa: BLE001
        response = {"ok": False, "kind": type(exc).__name__, "err": str(exc),
                    "traceback": traceback.format_exc()[-2000:]}
    finally:
        with open(response_path, "w") as handle:
            json.dump(response, handle)
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())