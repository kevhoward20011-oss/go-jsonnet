# go-jsonnet

[![GoDoc Widget]][GoDoc] [![Coverage Status Widget]][Coverage Status]

[GoDoc]: https://godoc.org/github.com/google/go-jsonnet
[GoDoc Widget]: https://godoc.org/github.com/google/go-jsonnet?status.png
[Coverage Status Widget]: https://coveralls.io/repos/github/google/go-jsonnet/badge.svg?branch=master
[Coverage Status]: https://coveralls.io/github/google/go-jsonnet?branch=master

This an implementation of [Jsonnet](http://jsonnet.org/) in pure Go. It is a feature complete, production-ready implementation. Bindings to C and Python are available (but not battle-tested).

This code is known to work on Go 1.24 and above. We recommend always using the newest stable release of Go.

Jsonnet can be used on the command line to evaluate Jsonnet input files and produce JSON, Yaml, or other outputs, or it can be used as a library.

**Security note:** If you are running a system that evaluates untrusted Jsonnet
code, extra care should be used to defend against data exfiltration risks. By
default, the `import`, `importstr` and `importbin` language constructs can read
from any path accessible to the Jsonnet process. The Jsonnet library allows you
to provide your own logic for processing imports, which is one way to restrict
imports to known safe sources. You could also consider running Jsonnet inside a
carefully configured gVisor or Firecracker container or other secure container
system to provide more general isolation if evaluating untrusted code.

## Installing the command line tool

```shell
# Using `go get` to install binaries is deprecated.
# The version suffix is mandatory.
go install github.com/google/go-jsonnet/cmd/jsonnet@latest

# Or other tools in the 'cmd' directory
go install github.com/google/go-jsonnet/cmd/jsonnet-lint@latest
```

It's also available on Homebrew:

```
brew install go-jsonnet
```

## Installing tools for working with Jsonnet code (formatter, linter)

`jsonnetfmt` and `jsonnet-lint` are also available as [pre-commit](https://github.com/pre-commit/pre-commit) hooks. Example `.pre-commit-config.yaml`:
```yaml
- repo: https://github.com/google/go-jsonnet
  rev: # ref you want to point at, e.g. v0.17.0
  hooks:
    - id: jsonnet-format
    - id: jsonnet-lint
```

## Using the library

It can also be embedded in your own Go programs as a library:

```go
package main

import (
	"fmt"
	"log"

	"github.com/google/go-jsonnet"
)

func main() {
	vm := jsonnet.MakeVM()

	snippet := `{
		person1: {
		    name: "Alice",
		    welcome: "Hello " + self.name + "!",
		},
		person2: self.person1 { name: "Bob" },
	}`

	jsonStr, err := vm.EvaluateAnonymousSnippet("example1.jsonnet", snippet)
	if err != nil {
		log.Fatal(err)
	}

	fmt.Println(jsonStr)
	/*
	   {
	     "person1": {
	         "name": "Alice",
	         "welcome": "Hello Alice!"
	     },
	     "person2": {
	         "name": "Bob",
	         "welcome": "Hello Bob!"
	     }
	   }
	*/
}
```

## Build instructions (go 1.24+)

```bash
git clone git@github.com:google/go-jsonnet.git
cd go-jsonnet
go build ./cmd/jsonnet
go build ./cmd/jsonnetfmt
go build ./cmd/jsonnet-deps
```
To build with [Bazel](https://bazel.build/) instead:
```bash
git clone git@github.com:google/go-jsonnet.git
cd go-jsonnet
git submodule init
git submodule update
bazel build //cmd/jsonnet
bazel build //cmd/jsonnetfmt
bazel build //cmd/jsonnet-deps
```
The resulting _jsonnet_ program will then be available at a platform-specific path, such as _bazel-bin/cmd/jsonnet/darwin_amd64_stripped/jsonnet_ for macOS.

Bazel also accommodates cross-compiling the program. To build the _jsonnet_ program for various popular platforms, run the following commands:

Target platform | Build command
--------------- | -------------------------------------------------------------------------------------
Current host    | _bazel build //cmd/jsonnet_
Linux           | _bazel build --platforms=@io_bazel_rules_go//go/toolchain:linux_amd64 //cmd/jsonnet_
macOS           | _bazel build --platforms=@io_bazel_rules_go//go/toolchain:darwin_amd64 //cmd/jsonnet_
Windows         | _bazel build --platforms=@io_bazel_rules_go//go/toolchain:windows_amd64 //cmd/jsonnet_

For additional target platform names, see the per-Go release definitions [here](https://github.com/bazelbuild/rules_go/blob/master/go/private/sdk_list.bzl#L21-L31) in the _rules_go_ Bazel package.

Additionally if any files were moved around, see the section [Keeping the Bazel files up to date](#keeping-the-bazel-files-up-to-date).

## Building libjsonnet.wasm

The [WASM](https://webassembly.org/) build can be used to embed go-jsonnet for use (client side) in the web browser, or in other WASM execution environments. For example, this is used to evaluate the live code snippets on https://jsonnet.org/

```bash
GOOS=js GOARCH=wasm go build -o libjsonnet.wasm ./cmd/wasm
```

Or if using bazel:

```
bazel build //cmd/wasm:libjsonnet.wasm
```

## Using the Python or C bindings in a process that forks

Evaluating Jsonnet in a process created with `fork()` after the bindings have
been loaded used to hang forever. The Go runtime cannot be repaired in place, so
the Python bindings now **hand the evaluation to a helper process started with
`exec`**, whose Go runtime is sound. You should still prefer to avoid the
situation, because the helper costs a process per evaluation:

```python
import multiprocessing as mp
import _gojsonnet            # loads the library, starting the Go runtime

def work():
    _gojsonnet.evaluate_file("x.jsonnet")   # runs in a helper process

if __name__ == "__main__":
    mp.get_context("fork").Process(target=work).start()   # works, but see below
```

This applies to the C bindings too, which have no such recovery, and has been
reported as [#484](https://github.com/google/go-jsonnet/issues/484).

### Why it deadlocks

Loading the library starts the Go runtime, which immediately spawns several
threads (measured: a Python process goes from 1 thread to 5 on
`import _gojsonnet`). `fork()` copies only the calling thread, so the child
inherits the Go scheduler's state — GOMAXPROCS, the M and P lists, the GC
state — but none of the threads that are supposed to be driving it. The Go
runtime has no `fork()` hook and cannot notice this.

The child therefore appears to work at first. It hangs the first time it needs a
stop-the-world, which is what the garbage collector does at the start of a cycle
and what evaluating any non-trivial Jsonnet program reliably provokes:
`stopTheWorld()` waits in `semacquire(&worldsema)` for Ms that no longer exist.
The result is a process at 0% CPU that can only be recovered with `SIGKILL`.

This is why the symptom scales with output size — a few KB of output finishes
before the collector's pacer starts a cycle, while a few MB does not. That
threshold is an artefact of the GC heuristic, not a real limit.

Note that a forked child can also block *before* it reaches any Jsonnet code, on
the Go runtime's own initialisation semaphore. On Linux this is reproducible:
`gdb` shows the child parked on a futex at `runtime_init_cond`, inside
`jsonnet_make()`, with a single thread and zero CPU time. Any Go call is
potentially fatal in a forked child, so the check below has to run before the
first call into Go, not merely before evaluating something.

### Why there is no transparent fix

The Go runtime exposes no supported way to restart, re-initialise, or recreate
its threads, so a post-fork child cannot be made usable again from inside the
library. Recovery is limited to detection.

Disabling the collector with `debug.SetGCPercent(-1)` does avoid the
stop-the-world, but it is a mitigation rather than a fix: it only suppresses the
trigger, leaves the scheduler state inconsistent, cannot be undone afterwards,
and costs unbounded memory growth in the child. Failing fast is safer and
cheaper than trading a hang for silent memory exhaustion.

The detection is shared between the C and Python bindings
(`jsonnet_internal_fork_safety_error` in `c-bindings/libjsonnet.cpp`). It
compares the current PID against the one recorded by a library constructor at
load time, because the Go runtime starts at load time while Go *package*
initialisation is deferred until the first call into Go — which, in this
scenario, has already happened inside the forked child. C callers must invoke it
before their first call into the library, including `jsonnet_make()`; the Python
binding does this for you.

### How the Python bindings recover

The Go runtime itself cannot be repaired, so the Python bindings do not try. When
`evaluate_file` or `evaluate_snippet` is called in an unsafe post-fork process,
the request is handed to a **helper process started with `exec`**
(`python/_forkexec.py`). That process loads the bindings for the first time, so
its Go runtime is initialised after the `exec` and is sound. The request and the
result travel through temporary files; the forked child never enters the Go
runtime itself.

Arguments are forwarded unchanged, so `ext_vars`, `ext_codes`, `tla_vars`,
`tla_codes`, `jpathdir`, `max_stack`, the GC settings and error messages all
behave as they do in a normal process. If the helper cannot be started, cannot be
spawned, or does not answer within `GOJSONNET_FORKEXEC_TIMEOUT` seconds (default
120), the call raises a `RuntimeError` — it never falls back to evaluating in the
unsafe process.

This is not yet used for the C API, which has no way to be handed a subprocess;
C callers should keep calling `jsonnet_internal_fork_safety_error` first.

### Callbacks in a delegated evaluation

`import_callback` and `native_callbacks` are Python callables and do not cross a
process boundary, so by default passing them to a delegated evaluation raises a
`RuntimeError`. Set **`GOJSONNET_FORKEXEC_CALLBACKS=1`** to enable them: the
forked process then opens a local `AF_UNIX` socket, and the helper calls back
into it for every import and native call. The callback bodies stay in the
process that called `evaluate_*`.

Two consequences, both deliberate:

* `os.getpid()` seen inside a callback is unchanged — it is the process that
  called `evaluate_*`.
* **The callback runs on a broker thread, not the Python thread that called
  `evaluate_*`.** `threading.get_ident()` and `threading.get_native_id()` will
  therefore differ from the caller's. Do not use thread-local state, or assert on
  the calling thread, from a callback in this mode.
* Each import/native call costs a socket round trip, so callback-heavy programs
  are slower than in a normal process.

If the socket cannot be created the evaluation fails; it never falls back to
evaluating in the unsafe process.

### Recommended safe usage

* Use `multiprocessing` start method **`spawn`**, which `exec`s a fresh
  interpreter and is therefore always safe. It is already the default on macOS
  and the default on Windows; on Linux you must opt in with
  `mp.set_start_method("spawn")` or `mp.get_context("spawn")`.
* **`forkserver` is only safe if the forkserver process never loads the
  bindings.** It forks its children from a helper process, and `multiprocessing`
  preloads your `__main__` module into that helper — so a module-level
  `import _gojsonnet` makes `forkserver` just as unsafe as `fork`. It is safe
  when the import is deferred into the function that needs it.
* Or **do all Jsonnet evaluation in the parent, before forking**, and pass the
  results to the children. This is often the simplest fix, since Jsonnet is
  usually CPU-bound and the workers are doing the I/O.

A minimal, dependency-free way to diagnose an existing hang is to check whether
the wedged process has a single thread:

```bash
sample $(pgrep -f your_app) 3 -f /tmp/app.sample   # look for one Thread_ in the call graph
```

## Running tests

```bash
./tests.sh  # Also runs `go test ./...`
```

## Running Benchmarks

### Method 1

```bash
go get golang.org/x/tools/cmd/benchcmp
```

1. Make sure you build a jsonnet binary _prior_ to making changes.

```bash
go build -o jsonnet-old ./cmd/jsonnet
```

2. Make changes (iterate as needed), and rebuild new binary

```bash
go build ./cmd/jsonnet
```

3. Run benchmark:

```bash
# e.g. ./benchmark.sh Builtin
./benchmark.sh <TestNameFilter>
```

### Method 2

1. get `benchcmp`

```bash
go get golang.org/x/tools/cmd/benchcmp
```

2. Make sure you build a jsonnet binary _prior_ to making changes.

```bash
make build-old
```

3. iterate with (which will also automatically rebuild the new binary `./jsonnet`)

_replace the FILTER with the name of the test you are working on_

```bash
FILTER=Builtin_manifestJsonEx make benchmark
```

## Update cpp-jsonnet sub-repo

This repo depends on [the original Jsonnet repo](https://github.com/google/jsonnet). Shared parts include the standard library, headers files for C API and some tests.

You can update the submodule and regenerate dependent files with one command:
```
./update_cpp_jsonnet.sh
```

Note: It needs to be run from repo root.

## Updating and modifying the standard library

Standard library source code is kept in `cpp-jsonnet` submodule, because it is shared with [Jsonnet C++
implementation](https://github.com/google/jsonnet).

For performance reasons we perform preprocessing on the standard library, so for the changes to be visible, regeneration is necessary:

```bash
go run cmd/dumpstdlibast/dumpstdlibast.go cpp-jsonnet/stdlib/std.jsonnet > astgen/stdast.go
```

The above command creates the _astgen/stdast.go_ file which puts the desugared standard library into the right data structures, which lets us avoid the parsing overhead during execution. Note that this step is not necessary to perform manually when building with Bazel; the Bazel target regenerates the _astgen/stdast.go_ (writing it into Bazel's build sandbox directory tree) file when necessary.

## Keeping the Bazel files up to date
Note that we maintain the Go-related Bazel targets with [the Gazelle tool](https://github.com/bazelbuild/bazel-gazelle). The Go module (_go.mod_ in the root directory) remains the primary source of truth. Gazelle analyzes both that file and the rest of the Go files in the repository to create and adjust appropriate Bazel targets for building Go packages and executable programs.

After changing any dependencies within the files covered by this Go module, it is helpful to run _go mod tidy_ to ensure that the module declarations match the state of the Go source code. In order to synchronize the Bazel rules with material changes to the Go module, run the gazelle command:
```bash
bazel run //:gazelle
```
