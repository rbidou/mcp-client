# MCP Client

A single-file CLI for poking at MCP servers: connect, list what the server
exposes, call a tool with arbitrary arguments, and run a file of assertions as a
regression suite. Useful when you want to know whether a server works *before*
wiring it into an agent, and to isolate whether a broken tool call is the
server's fault or the client's.

```
mcp-client.py [transport] [global options] <command> [command options]
```

Built on the MCP Python SDK v2, so it speaks the `2026-07-28` protocol and falls
back to the `initialize` handshake against older servers without you asking.

## Install

```bash
pip install -r requirements.txt
chmod +x mcp-client.py
```

Python 3.10+. The dependency is the official `mcp` SDK, v2 or newer.

`mcptest --help` lists the transports and global options; `mcptest COMMAND
--help` documents each command, with worked examples for `call` and the case
format for `suite`. Both work before you install anything — the SDK import is
deferred so a bare checkout can still show its help.

## Choosing a transport

Exactly one is required.

| Flag | Transport | Example |
|---|---|---|
| `--stdio CMD` | subprocess over stdin/stdout | `--stdio "npx -y @modelcontextprotocol/server-filesystem /tmp"` |
| `--http URL` | Streamable HTTP | `--http http://10.0.4.10:5000/mcp` |
| `--sse URL` | legacy HTTP+SSE | `--sse http://10.0.4.10:5000/sse` |
| `--config FILE --server NAME` | read the entry from a config file | `--config .mcp.json --server kali` |

`--config` accepts the `.mcp.json` / `claude_desktop_config.json` shape, i.e. an
`mcpServers` object keyed by server name. An entry with `command` becomes a
stdio launch; an entry with `url` becomes HTTP (or SSE if `"type": "sse"` or the
URL ends in `/sse`). `env` and `headers` from the file are merged with anything
you pass on the command line, and your flags win.

Global options:

| Flag | Meaning |
|---|---|
| `--mode` | `auto` (default), `legacy`, or a pinned version like `2026-07-28` |
| `-e K=V` | extra environment variable for a stdio server (repeatable) |
| `--inherit-env` | give the child your whole environment |
| `-H "Header: value"` | extra HTTP header, e.g. auth (repeatable); Streamable HTTP only |
| `--cwd DIR` | working directory for a stdio server |
| `--timeout SEC` | per-request timeout, default 30 |
| `--elicit JSON\|decline` | answer elicitation requests instead of failing |
| `--log-level LEVEL` | ask a 2026-era server to send logs at this level |
| `--json` | machine-readable output for `list` and `call` |

Two v2 behaviours worth knowing, because they will bite you otherwise:

**A stdio child does not inherit your environment.** The SDK gives it a minimal
allow-list (`HOME`, `PATH`, `SHELL`, `TERM`, `USER`, `LOGNAME` on POSIX) so
nothing sensitive leaks into a process you may not have written. A server that
needs an API key won't find it: pass it with `-e`, or `--inherit-env` if you
really want the lot.

**`--mode` decides which protocol era you're testing.** `auto` sends a
`server/discover` probe and falls back to the handshake if the server has never
heard of it — that's the realistic path, and `info` prints which one you landed
on. `legacy` forces the handshake, which is the only era with a server-to-client
back-channel, so it's what you want when testing push-style elicitation or
sampling. A pinned version sends no negotiation traffic at all, which makes it
fast but leaves `server_info` and `server_capabilities` empty. Running the same
suite under `--mode auto` and `--mode legacy` is a cheap way to check a server
really does serve both eras.

## Commands

### `info` — is it alive?

```bash
./mcp-client.py --http http://10.0.4.10:5000/mcp info
```

Connects and prints the negotiated protocol version, the server's name and
version, which capabilities it declared, its instructions string, and how many
tools, resources and prompts it offers. Capabilities the server doesn't
implement are reported as unsupported rather than crashing. This is the fastest
way to distinguish "server is down / URL is wrong / auth is rejected" from
"server is up but the tool I want isn't there".

### `list` — what can it do?

```bash
./mcp-client.py --stdio "npx -y @modelcontextprotocol/server-filesystem /tmp" list
./mcp-client.py --config .mcp.json --server kali list --schema --resources --prompts
```

Prints each tool as `name(arg:type, optional?:type)`, its title if it has one,
and the first line of its description. Paginates until the server stops handing
back a cursor. `--schema` dumps the full input schema, and the output schema
where the tool declares one, which is what you need when a call keeps getting
rejected for a validation reason. `--json`
emits the raw tool definitions — the same text the model will see, which is
worth reading critically: vague descriptions and untyped `object` parameters are
the usual cause of a model calling a tool wrong.

### `describe` — what does this call need?

```bash
./mcp-client.py --config .mcp.json --server kali describe run_command
```

```
run_command(cmd:string, host:string, timeout?:integer)
  Run a shell command

  Execute a command on the selected host and return stdout.

required:
  cmd        string   length 1–512
             The command line to execute.
  host       string   one of: "kali", "dmz", "lab"
             Which host to run on.

optional:
  timeout    integer   default 60; range 1–3600
             Seconds before the command is killed.

example:
  mcptest ... call run_command -a cmd='...' -a host='kali'
  mcptest ... call run_command --args '{"cmd": "...", "host": "kali"}'
```

Splits the tool's input schema into required and optional, resolving each
argument's type, enum values, defaults, ranges, length and pattern constraints,
and description, then prints an invocation you can paste. Nested objects appear
as `env.PATH` and arrays of objects as `targets[].address` — the dotted name is
a path you can navigate in the JSON you're about to send. If the tool declares
an output schema, the shape of `structured_content` is listed too. `--json`
prints the raw schema instead, for when you want to diff it.

An unrecognized tool name exits 2 and suggests close matches.

### `call` — invoke one tool

Three ways to pass arguments, checked in this order:

```bash
# full JSON object — use for nested structures
./mcp-client.py --config .mcp.json --server fs call read_file --args '{"path":"/tmp/a.log"}'

# from a file
./mcp-client.py --config .mcp.json --server fs call write_file --args-file payload.json

# repeated key=value — values are parsed as JSON when possible, else kept as strings
./mcp-client.py --config .mcp.json --server kali call run_command -a cmd="nmap -sV 10.0.4.1" -a timeout=120
```

Before sending, `call` fetches `tools/list` and checks the payload: an unknown
tool name or a missing required argument fails locally with the `describe`
output on stderr, rather than costing you a round trip to read a validation
error. `--no-check` skips it, which is what you want when the point of the test
*is* to see how the server handles a malformed call.

So `limit=5` arrives as the integer 5, `deep=true` as a boolean, `hosts=["a","b"]`
as a list, and `path=/tmp/a.log` as a string. Text blocks are printed as text;
every other block type is summarized with its fields, base64 payloads shown as a
size rather than dumped. `structured_content`, if the tool declares an output
schema, is printed separately — that's the half your code should read, where
`content` is the half the model reads.

### `suite` — regression testing

```bash
./mcp-client.py --config .mcp.json --server kali suite cases.json
```

`cases.json` is a list of assertions:

```json
[
  {
    "label": "reads an existing file",
    "tool": "read_file",
    "arguments": { "path": "/tmp/a.log" },
    "expect_contains": "started"
  },
  {
    "label": "refuses a path outside the sandbox",
    "tool": "read_file",
    "arguments": { "path": "/etc/shadow" },
    "expect_error": true
  }
]
```

| Field | Required | Meaning |
|---|---|---|
| `tool` | yes | tool name; a name the server doesn't expose is an automatic failure |
| `arguments` | no | argument object, default `{}` |
| `expect_error` | no | whether the call should come back with `is_error=True`, default `false` |
| `expect_contains` | no | substring that must appear in the rendered result |
| `expect_structured_key` | no | key that must be present and non-null in `structured_content` |
| `label` | no | name shown in the output, defaults to the tool name |

A case that comes back as a JSON-RPC error rather than a result fails with
`protocol error`, separately from `is_error` — see *Two kinds of failure* below.

Each case prints `ok` or `FAIL` with a reason and the first 300 characters of the
result, then a pass count. Exit code is 1 if anything failed, so it drops into CI
as-is.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the tool returned an error result, or a suite case failed |
| 2 | connection, transport or protocol failure — nothing was tested |
| 130 | interrupted |

The distinction between 1 and 2 matters in a pipeline: 2 means the server never
answered, 1 means it answered and the answer was wrong.

## Troubleshooting

**A stdio server exits immediately.** Its stderr goes to your terminal — read it.
The usual cause is a missing runtime (`npx`, `uvx`) or a required env var the
server reads at startup. Remember the child gets an allow-listed environment,
not yours, so a variable that is set in your shell is *not* set in the server:
pass it with `-e`.

**HTTP returns 404 or 405.** You are probably pointing at the wrong path or the
wrong transport. Streamable HTTP servers usually mount at `/mcp`; older SSE
servers expose `/sse` for the event stream plus a separate POST endpoint. Try
`--sse` if `--http` fails and the server is old.

**`Elicitation not supported`, and the whole call fails.** The server asked your
client a question and you never registered a way to answer, so the SDK refused
on your behalf. Pass `--elicit decline` to answer and see what it wanted, or
`--elicit '{"field":"value"}'` to accept with a payload. Note that registering
the callback *declares the capability*, so a server that checks before asking
will behave differently with the flag than without it — which is exactly the
thing worth testing.

**A `--mode 2026-07-28` run shows no server name or capabilities.** That's the
pin working as designed: it sends no negotiation traffic, so the client never
asked who it was talking to. Use `--mode auto` when you want the identity.

**HTTP returns 401 with a `WWW-Authenticate` header.** The server wants OAuth.
This tool only does static headers, so mint a token separately and pass it with
`-H "Authorization: Bearer $TOK"`.

**A call hangs.** Raise `--timeout`. Long-running tools (a scan, a build) legitimately
exceed 30 seconds; a hang with no output past several minutes usually means the
server is waiting on something interactive.

**Everything works here but fails in your agent.** Compare the tool schema from
`list --json` against what the model was given, and check whether the failing
argument is one the model has to infer. Most "the MCP server is broken" reports
are actually description or schema problems.

---

# Addendum — how MCP actually works

Written to make the output of this tool legible. If you only read one section,
read *The three server primitives* and *Two kinds of failure*.

## The shape of the thing

MCP (Model Context Protocol) is an open standard, introduced by Anthropic in
November 2024 and now maintained under the Agentic AI Foundation, for connecting
AI applications to external systems. The problem it solves is combinatorial: N
AI applications each needing custom glue for M tools is N×M integrations, and MCP
turns that into N+M by standardizing the interface between them. The design is
openly modelled on the Language Server Protocol, which did the same thing for
editors and language tooling.

Three roles:

- **Host** — the AI application: Claude Code, an IDE, your own agent loop. It
  owns the model and decides what the model is allowed to do.
- **Client** — the protocol connector inside the host, one per server, managing
  a single connection. `mcptest` is a client.
- **Server** — the process exposing capabilities: a filesystem, a database, an
  internal API, a scanner.

Servers do not talk to the model. They talk to the client, which decides what to
surface to the model. That indirection is where consent, filtering and audit live.

## Wire format

Everything is JSON-RPC 2.0: requests with an `id` and a `method`, responses
matching that `id`, and notifications with no `id` and no reply.

A tool listing:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list"}
```

```json
{"jsonrpc":"2.0","id":1,"result":{"tools":[
  {"name":"read_file",
   "description":"Read the contents of a file",
   "inputSchema":{"type":"object",
                  "properties":{"path":{"type":"string"}},
                  "required":["path"]}}
]}}
```

A call:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call",
 "params":{"name":"read_file","arguments":{"path":"/tmp/a.log"}}}
```

```json
{"jsonrpc":"2.0","id":2,"result":{
  "content":[{"type":"text","text":"started at 09:14\n"}],
  "isError":false}}
```

Those payloads are the shape to have in your head, not a byte-exact capture: the
2026 revision adds `_meta`, cache hints on list results, and the routing headers
described below. In the Python SDK the same fields arrive as snake_case
attributes — `tool.input_schema`, `result.structured_content`, `result.is_error`.

That's the whole core of it. `mcptest list` is the first exchange, `mcptest call`
is the second. Everything else is variations: `resources/list`, `resources/read`,
`prompts/list`, `prompts/get`, `ping`, and pagination via an opaque `cursor`.

## The three server primitives

Distinguishing these explains most of the behaviour you'll observe.

**Tools** are model-controlled. The model decides when to invoke them, based on
the name, description and JSON Schema. They can have side effects. Because the
model chooses, the description *is* the interface — a badly described tool is a
broken tool even if the code is perfect.

**Resources** are application-controlled. They are addressable read-only context
identified by URI (`file:///var/log/app.log`, `db://users/42`), which the host
attaches deliberately. Servers can also expose URI templates so a client can
construct addresses.

**Prompts** are user-controlled. They are parameterized templates the user picks
explicitly — slash commands, in practice.

There are also client-side features the server can call back into: **roots**
(which directories the client considers in scope), **sampling** (server asks the
host to run a model completion) and **elicitation** (server asks the user for
missing input mid-call). Note that roots and sampling are deprecated as of the
2026-07-28 revision, with a twelve-month support window; don't build new work on
them.

## Transports

**stdio** — the client spawns the server as a subprocess and speaks newline-delimited
JSON over stdin/stdout. Local, no network, no auth, lifetime tied to the client.
Note that the server must never write anything but protocol frames to stdout;
logging goes to stderr. A stray `print()` corrupts the stream, and this is a
common bug in hand-rolled servers.

**Streamable HTTP** — a single endpoint that takes POSTs and can reply either with
a plain JSON body or an SSE stream when the server wants to push progress or
intermediate messages. This is the transport for anything remote.

**HTTP+SSE** — the original two-endpoint remote transport, now deprecated. `--sse`
exists for servers that haven't migrated.

## Lifecycle, and the stateless turn

Through the `2025-11-25` revision, a connection began with a handshake: the client
sent `initialize` with its protocol version and capabilities, the server replied
with its own, the client sent an `initialized` notification, and only then could
either side make requests. Remote servers tracked the session with an
`Mcp-Session-Id` header, which meant load balancers needed sticky sessions and
shared state.

The `2026-07-28` revision changed this fundamentally. The `initialize`/`initialized`
exchange and the session header were retired; every request now carries its own
protocol version, client identity and capabilities in `_meta`, so any request can
land on any instance behind an ordinary round-robin load balancer. A new optional
`server/discover` RPC exists for clients that want capabilities up front. Method
and tool names now travel in `Mcp-Method` and `Mcp-Name` HTTP headers so gateways
can route and rate-limit without parsing bodies, and list responses carry cache
hints (`ttlMs`, `cacheScope`). Server-initiated requests were reworked into Multi
Round-Trip Requests: instead of holding a stream open, the server returns a result
indicating input is required, and the client retries the call with the answers
attached.

Practically: state that used to hide in the transport now has to be explicit. If a
server needs continuity across calls, it returns a handle from one tool and the
model passes it back as an argument to the next — visible to the model, and
visible to you when debugging.

**Where this tool sits.** `mcptest` uses SDK v2, whose `Client` bridges the two
eras for you: by default it probes with `server/discover` and falls back to
`initialize` when the server has never heard of it. You therefore don't pick an
era unless you want to — and when you do, `--mode` is the switch. The one thing
to remember is that the modern era has no back-channel, so anything push-shaped
(sampling, form elicitation driven as a server request) needs `--mode legacy`.

## Two kinds of failure

This trips up nearly everyone, and it's why `mcptest` separates exit codes 1 and 2.

A **protocol error** is a JSON-RPC error response: unknown method, a capability
your client didn't declare, a request the server refuses outright. The model
never sees these. They mean the client and server disagree about something
structural. In the SDK these surface as a raised `MCPError`, the only client
method failure that is an exception rather than a result.

A **tool execution error** is a normal successful response with `is_error=True`
and the failure described in `content`. The file didn't exist; the API returned
403; the command exited non-zero. These are deliberately handed to the model as
text, because the model is often able to recover — fix the path, try a different
argument. That's why `expect_error` in a suite case asserts on `is_error` and not
on an exception.

The dividing line is not where you'd guess. Calling a tool the server doesn't
have is a *tool* error, not a protocol one: nothing raises, you get
`is_error=True` with an unknown-tool message. That's why `suite` checks the name
against `tools/list` itself instead of relying on the call to blow up.

So: a tool that raises `MCPError` where it should return `is_error` is hiding
recoverable failures from the model, and a tool that returns `is_error` for a
schema violation
is teaching the model that its bad call was fine.

## What to look at when you test a server

- Does every tool description say what the tool does *and* when to use it? The
  model has nothing else to go on.
- Is the schema tight — enums instead of free strings, required fields marked,
  types on every property? Loose schemas produce plausible-looking wrong calls.
- Are errors returned as `is_error` with a message a model could act on, rather
  than a stack trace or a bare `false`?
- Are destructive tools distinguishable from read-only ones by name alone? A host
  that gates on consent has only the name and annotations to gate with.
- Is the result compact? Tool output goes into the context window. A tool that
  dumps 50k characters of JSON is technically working and practically unusable.

## References

- Specification: https://modelcontextprotocol.io/specification/2026-07-28
- Changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Python SDK: https://github.com/modelcontextprotocol/python-sdk