#!/usr/bin/env python3
"""mcptest — probe an MCP server from the command line.

Lists what a server exposes, calls its tools with arbitrary arguments, and runs
a file of assertions as a regression suite. Built on the MCP Python SDK v2, so
it speaks protocol 2026-07-28 and falls back to the initialize handshake for
older servers.

Run `mcptest --help` for the options and `mcptest <command> --help` for each
command.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import shlex
import sys
import textwrap
from contextlib import asynccontextmanager
from typing import Any

VERSION = "1.0.0"

# The SDK is imported lazily-ish: a missing dependency must not stop --help from
# working, so the failure is recorded and raised only when we try to connect.
try:
    import httpx2

    from mcp import Client, MCPError, StdioServerParameters
    from mcp.client import ClientRequestContext
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import ElicitResult, TextContent

    SDK_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the SDK
    SDK_ERROR = exc

    class MCPError(Exception):  # placeholder so `except MCPError` still parses
        pass

    class TextContent:  # placeholder for isinstance()
        pass


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

def make_elicitation_callback(spec: str):
    """Answer a server's elicitation request.

    On a handshake-era session the server pushes `elicitation/create`; on a
    2026-07-28 session the tool returns an InputRequiredResult and the Client
    routes it here, then retries the call. Same callback either way.

    Registering this *declares the capability*, which changes what the server
    is willing to ask for — that's why it is opt-in via --elicit.
    """
    payload = None if spec == "decline" else json.loads(spec)

    async def handle(context: ClientRequestContext, params: Any) -> ElicitResult:
        question = getattr(params, "message", None) or getattr(params, "url", "")
        print(f"[elicit/{getattr(params, 'mode', '?')}] {question}", file=sys.stderr)
        if payload is None:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content=payload)

    return handle


@asynccontextmanager
async def connect(args):
    """Yield a connected Client for whichever transport was chosen."""
    kwargs: dict[str, Any] = {"mode": args.mode}
    if args.elicit is not None:
        kwargs["elicitation_callback"] = make_elicitation_callback(args.elicit)
    if args.log_level:
        kwargs["log_level"] = args.log_level

    if args.stdio:
        argv = shlex.split(args.stdio)
        env = dict(os.environ) if args.inherit_env else {}
        env.update(args.env)
        params = StdioServerParameters(
            command=argv[0],
            args=argv[1:],
            env=env or None,
            cwd=args.cwd,
        )
        async with Client(params, **kwargs) as client:
            yield client

    elif args.http:
        # In v2, headers/timeouts/proxies/mTLS all live on the httpx2 client:
        # streamable_http_client() takes url, http_client and terminate_on_close.
        async with httpx2.AsyncClient(
            headers=args.header or None,
            timeout=httpx2.Timeout(30.0, read=args.timeout),
            follow_redirects=True,
        ) as http_client:
            transport = streamable_http_client(args.http, http_client=http_client)
            async with Client(transport, **kwargs) as client:
                yield client

    elif args.sse:
        if args.header:
            print("warning: -H is ignored on --sse", file=sys.stderr)
        async with Client(sse_client(args.sse), **kwargs) as client:
            yield client

    else:  # pragma: no cover - argparse enforces this
        raise SystemExit("no transport selected")


def apply_config(args) -> None:
    """Load one server entry out of a .mcp.json / claude_desktop_config.json."""
    if not args.config:
        return
    with open(args.config) as fh:
        cfg = json.load(fh)
    servers = cfg.get("mcpServers", cfg)
    if not args.server:
        raise SystemExit(f"--server required; found: {', '.join(servers) or '(none)'}")
    if args.server not in servers:
        raise SystemExit(f"{args.server!r} not in {args.config}: {', '.join(servers)}")

    entry = servers[args.server]
    if "command" in entry:
        args.stdio = shlex.join([entry["command"], *entry.get("args", [])])
        args.env = {**entry.get("env", {}), **args.env}
    elif "url" in entry:
        url = entry["url"]
        if entry.get("type") == "sse" or url.rstrip("/").endswith("sse"):
            args.sse = url
        else:
            args.http = url
        args.header = {**entry.get("headers", {}), **args.header}
    else:
        raise SystemExit(f"entry {args.server!r} has neither 'command' nor 'url'")


class Session:
    """The connected client plus a per-request timeout."""

    def __init__(self, client, timeout: float):
        self.client = client
        self.timeout = timeout

    async def rpc(self, coro):
        return await asyncio.wait_for(coro, self.timeout)

    async def all_tools(self) -> list:
        """Walk every page. Most servers answer in one."""
        tools, cursor = [], None
        while True:
            page = await self.rpc(self.client.list_tools(cursor=cursor))
            tools.extend(page.tools)
            if page.next_cursor is None:
                return tools
            cursor = page.next_cursor


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def render(content: list) -> str:
    """Flatten a CallToolResult.content list into printable text.

    content is a union of TextContent / ImageContent / AudioContent /
    ResourceLink / EmbeddedResource, so only TextContent is unwrapped directly;
    the rest are summarized without guessing at their fields.
    """
    out = []
    for block in content:
        if isinstance(block, TextContent):
            out.append(block.text)
            continue
        fields = block.model_dump(exclude_none=True, mode="json")
        fields.pop("type", None)
        summary = ", ".join(
            f"{k}={len(v)}B base64" if isinstance(v, str) and len(v) > 120 else f"{k}={v!r}"
            for k, v in fields.items()
        )
        out.append(f"<{type(block).__name__} {summary}>")
    return "\n".join(out)


ARGS_HINT = """hint: -a takes one KEY=VALUE and repeats; --args takes one JSON object:
  ... call TOOL -a a=1 -a b=3
  ... call TOOL --args '{"a": 1, "b": 3}'"""


def decode_object(text: str, source: str) -> dict[str, Any]:
    """Decode a JSON object, naming the flag that carried it when it isn't one."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{source} is not valid JSON: {exc}\n{ARGS_HINT}")
    if not isinstance(value, dict):
        raise SystemExit(
            f"{source} must be a JSON object, got {type(value).__name__}\n{ARGS_HINT}")
    return value


def parse_args_payload(args) -> dict[str, Any]:
    """--args JSON wins, then --args-file, then repeated -a key=value."""
    if args.args:
        payload = decode_object(args.args, "--args")
    elif args.args_file:
        with open(args.args_file) as fh:
            payload = decode_object(fh.read(), f"--args-file {args.args_file}")
    else:
        payload = {}
        for item in args.arg or []:
            key, _, raw = item.partition("=")
            try:
                payload[key] = json.loads(raw)  # numbers, bools, lists, objects
            except json.JSONDecodeError:
                payload[key] = raw  # plain string
    return payload


def signature(schema: dict | None) -> str:
    schema = schema or {}
    required = schema.get("required", [])
    props = schema.get("properties", {})
    return ", ".join(
        f"{n}{'' if n in required else '?'}:{p.get('type', 'any')}" for n, p in props.items()
    )


# --- argument introspection ------------------------------------------------
#
# A tool's inputSchema is the contract, but reading raw JSON Schema to work out
# what a call needs is tedious. These turn it into something you can act on.

def type_name(prop: dict) -> str:
    """A short type label for one schema property."""
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]
    for key in ("anyOf", "oneOf"):
        if key in prop:
            return " | ".join(type_name(sub) for sub in prop[key])
    if "const" in prop:
        return "const"
    if "enum" in prop and "type" not in prop:
        return "enum"

    kind = prop.get("type", "any")
    if isinstance(kind, list):
        return " | ".join(kind)
    if kind == "array":
        return f"array[{type_name(prop.get('items', {}))}]"
    return kind


def constraints(prop: dict) -> str:
    """Everything else the schema says about a property, in one line."""
    notes = []
    if "const" in prop:
        notes.append(f"must be {json.dumps(prop['const'])}")
    if "enum" in prop:
        values = [json.dumps(v) for v in prop["enum"]]
        shown = ", ".join(values[:6]) + (", …" if len(values) > 6 else "")
        notes.append(f"one of: {shown}")
    if "default" in prop:
        notes.append(f"default {json.dumps(prop['default'])}")
    if "format" in prop:
        notes.append(prop["format"])

    lo = prop.get("minimum", prop.get("exclusiveMinimum"))
    hi = prop.get("maximum", prop.get("exclusiveMaximum"))
    if lo is not None or hi is not None:
        notes.append(f"range {lo if lo is not None else '−∞'}–{hi if hi is not None else '∞'}")
    if "minLength" in prop or "maxLength" in prop:
        notes.append(f"length {prop.get('minLength', 0)}–{prop.get('maxLength', '∞')}")
    if "minItems" in prop or "maxItems" in prop:
        notes.append(f"{prop.get('minItems', 0)}–{prop.get('maxItems', '∞')} items")
    if "pattern" in prop:
        notes.append(f"matches /{prop['pattern']}/")

    return "; ".join(notes)


def walk_properties(schema: dict, prefix: str = "", depth: int = 0):
    """Yield (dotted_name, property, is_required) for a schema, nested included.

    Nested objects come back as `filter.since`, arrays of objects as
    `hosts[].address`, so the name in the first column is a path you can
    actually navigate in the JSON you're about to send.
    """
    required = set(schema.get("required", []))
    for name, prop in (schema.get("properties") or {}).items():
        path = f"{prefix}{name}"
        yield path, prop, name in required
        if depth >= 2:
            continue
        if prop.get("type") == "object" and prop.get("properties"):
            yield from walk_properties(prop, f"{path}.", depth + 1)
        items = prop.get("items") or {}
        if prop.get("type") == "array" and items.get("properties"):
            yield from walk_properties(items, f"{path}[].", depth + 1)


def placeholder(prop: dict):
    """A stand-in value of the right type, for the example invocation."""
    if "default" in prop:
        return prop["default"]
    if prop.get("enum"):
        return prop["enum"][0]
    kind = prop.get("type")
    if isinstance(kind, list):
        kind = kind[0]
    return {"string": "...", "integer": 0, "number": 0.0, "boolean": False,
            "array": [], "object": {}}.get(kind, None)


def describe_arguments(tool, prog: str = "mcptest ...") -> str:
    """Spell out what a call to this tool needs.

    Splits the input schema into required and optional, with each argument's
    type, constraints and description, and ends with an invocation you can
    paste. This is the answer to "what do I have to pass?" without reading
    JSON Schema by eye.
    """
    schema = tool.input_schema or {}
    rows = list(walk_properties(schema))

    lines = [f"{tool.name}({signature(schema)})"]
    if tool.title:
        lines.append(f"  {tool.title}")
    if tool.description:
        lines.append("")
        lines += ["  " + ln for ln in tool.description.strip().splitlines()]

    if not rows:
        lines += ["", "takes no arguments"]
        return "\n".join(lines)

    width = max(len(name) for name, _, _ in rows) + 2
    for label, wanted in (("required", True), ("optional", False)):
        group = [(n, p) for n, p, req in rows
                 if req == wanted and "." not in n and "[]." not in n]
        nested = [(n, p) for n, p, req in rows if "." in n or "[]." in n]
        if not group:
            lines += ["", f"{label}: (none)"]
            continue
        lines += ["", f"{label}:"]
        for name, prop in group:
            note = constraints(prop)
            lines.append(f"  {name:<{width}}{type_name(prop)}"
                         + (f"   {note}" if note else ""))
            if prop.get("description"):
                lines.append(f"  {'':<{width}}{prop['description'].strip()}")
            for child, cprop in nested:
                if not child.startswith(name + ".") and not child.startswith(name + "[]."):
                    continue
                cnote = constraints(cprop)
                lines.append(f"    {child:<{width}}{type_name(cprop)}"
                             + (f"   {cnote}" if cnote else ""))

    example = {n: placeholder(p) for n, p, req in rows if req and "." not in n}
    if example:
        flags = " ".join(
            f"-a {k}={json.dumps(v) if not isinstance(v, str) else v!r}"
            for k, v in example.items())
        lines += ["", "example:",
                  f"  {prog} call {tool.name} {flags}",
                  f"  {prog} call {tool.name} --args '{json.dumps(example)}'"]

    out_schema = getattr(tool, "output_schema", None)
    if out_schema:
        lines += ["", "returns structured_content matching:",
                  "  " + ", ".join(
                      f"{n}:{type_name(p)}" for n, p, _ in walk_properties(out_schema)
                      if "." not in n) or "  (see --schema)"]
    return "\n".join(lines)


def missing_required(schema: dict | None, payload: dict) -> list[str]:
    """Top-level required keys the payload doesn't supply."""
    return [n for n in (schema or {}).get("required", []) if n not in payload]


def dump(obj) -> str:
    return json.dumps(obj.model_dump(exclude_none=True, mode="json"), indent=2)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

async def cmd_info(session: Session, args) -> int:
    client = session.client
    info = client.server_info

    print(f"protocol     {client.protocol_version}")
    print(f"server       {info.name} {info.version}" if info else
          "server       (not reported — pinned mode, or a 2026 server that omits it)")

    caps = client.server_capabilities
    declared = [
        name for name in ("tools", "resources", "prompts", "completions", "logging")
        if caps is not None and getattr(caps, name, None) is not None
    ]
    print(f"capabilities {', '.join(declared) or '(none)'}")

    if client.instructions:
        first = client.instructions.strip().splitlines()[0]
        print(f"instructions {first}")

    counts = [("tools", client.list_tools, "tools"),
              ("resources", client.list_resources, "resources"),
              ("prompts", client.list_prompts, "prompts")]
    for label, method, attr in counts:
        try:
            result = await session.rpc(method())
            print(f"{label:12} {len(getattr(result, attr))}")
        except MCPError as exc:
            print(f"{label:12} unsupported ({exc})")
    return 0


async def cmd_list(session: Session, args) -> int:
    client = session.client
    tools = await session.all_tools()

    if args.json:
        print(json.dumps(
            [t.model_dump(exclude_none=True, mode="json") for t in tools], indent=2))
        return 0

    if not tools:
        print("(server exposes no tools)")
    for tool in tools:
        label = f" — {tool.title}" if tool.title else ""
        print(f"\n\033[1m{tool.name}\033[0m({signature(tool.input_schema)}){label}")
        if tool.description:
            print("  " + tool.description.strip().splitlines()[0])
        if args.schema:
            print(json.dumps(tool.input_schema, indent=2))
        if args.schema and getattr(tool, "output_schema", None):
            print("  output:")
            print(json.dumps(tool.output_schema, indent=2))

    if args.resources:
        listed = await session.rpc(client.list_resources())
        templates = await session.rpc(client.list_resource_templates())
        print(f"\nresources ({len(listed.resources)}):")
        for r in listed.resources:
            print(f"  {r.uri}  {r.name or ''}")
        print(f"templates ({len(templates.resource_templates)}):")
        for t in templates.resource_templates:
            print(f"  {t.uri_template}  {t.name or ''}")

    if args.prompts:
        prompts = await session.rpc(client.list_prompts())
        print(f"\nprompts ({len(prompts.prompts)}):")
        for p in prompts.prompts:
            needs = ", ".join(a.name for a in (p.arguments or []))
            print(f"  {p.name}({needs})  {p.description or ''}")
    return 0


async def cmd_describe(session: Session, args) -> int:
    tools = {t.name: t for t in await session.all_tools()}
    tool = tools.get(args.name)
    if tool is None:
        near = difflib.get_close_matches(args.name, tools, n=3)
        print(f"no tool named {args.name!r}" +
              (f"; did you mean {', '.join(near)}?" if near else ""), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(tool.input_schema, indent=2))
    else:
        print(describe_arguments(tool))
    return 0


async def cmd_call(session: Session, args) -> int:
    payload = parse_args_payload(args)

    if not args.no_check:
        tools = {t.name: t for t in await session.all_tools()}
        tool = tools.get(args.name)
        if tool is None:
            near = difflib.get_close_matches(args.name, tools, n=3)
            print(f"error: server exposes no tool named {args.name!r}" +
                  (f"; did you mean {', '.join(near)}?" if near else ""), file=sys.stderr)
            return 2
        gaps = missing_required(tool.input_schema, payload)
        if gaps:
            print(f"error: missing required argument(s): {', '.join(gaps)}\n",
                  file=sys.stderr)
            print(describe_arguments(tool), file=sys.stderr)
            return 2

    result = await session.rpc(session.client.call_tool(args.name, payload))

    if args.json:
        print(dump(result))
    else:
        print(render(result.content))
        if result.structured_content is not None:
            print("\nstructured:")
            print(json.dumps(result.structured_content, indent=2))
        if result.is_error:
            print("\n(is_error=True)", file=sys.stderr)
    return 1 if result.is_error else 0


async def cmd_suite(session: Session, args) -> int:
    """Run a JSON file of cases:
       [{"tool": "read_file", "arguments": {...},
         "expect_error": false, "expect_contains": "hello"}]
    """
    with open(args.file) as fh:
        cases = json.load(fh)

    available = {t.name for t in await session.all_tools()}
    failures = 0

    for i, case in enumerate(cases, 1):
        name = case["tool"]
        label = case.get("label", name)
        if name not in available:
            print(f"[{i}] FAIL {label}: tool not exposed by server")
            failures += 1
            continue
        try:
            result = await session.rpc(
                session.client.call_tool(name, case.get("arguments", {})))
            text = render(result.content)
            errored = bool(result.is_error)
        except MCPError as exc:
            # A JSON-RPC error, not a tool error: the request was rejected.
            print(f"[{i}] FAIL {label}: protocol error {exc}")
            failures += 1
            continue
        except Exception as exc:
            print(f"[{i}] FAIL {label}: raised {type(exc).__name__}: {exc}")
            failures += 1
            continue

        problems = []
        if errored != bool(case.get("expect_error", False)):
            problems.append("is_error=True" if errored else "expected is_error=True")
        needle = case.get("expect_contains")
        if needle and needle not in text:
            problems.append(f"missing {needle!r}")
        key = case.get("expect_structured_key")
        if key and (result.structured_content or {}).get(key) is None:
            problems.append(f"no structured_content[{key!r}]")

        if problems:
            failures += 1
            print(f"[{i}] FAIL {label}: {'; '.join(problems)}")
            print("      " + text[:300].replace("\n", "\n      "))
        else:
            print(f"[{i}] ok   {label}")

    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    def kv(text: str) -> tuple[str, str]:
        key, sep, value = text.partition("=")
        if not sep:
            raise argparse.ArgumentTypeError("expected KEY=VALUE")
        return key, value

    def header(text: str) -> tuple[str, str]:
        key, sep, value = text.partition(":")
        if not sep:
            raise argparse.ArgumentTypeError("expected 'Header: value'")
        return key.strip(), value.strip()

    raw = argparse.RawDescriptionHelpFormatter

    def para(text: str) -> str:
        """Wrap a description; the raw formatter leaves epilogs alone."""
        return textwrap.fill(text, 78)

    p = argparse.ArgumentParser(
        prog="mcptest",
        description=__doc__,
        formatter_class=raw,
        epilog="""\
examples:
  mcptest --stdio "npx -y @modelcontextprotocol/server-filesystem /tmp" list
  mcptest --http http://10.0.4.10:5000/mcp -H "Authorization: Bearer $TOK" info
  mcptest --config .mcp.json --server kali call run_command -a cmd="uname -a"
  mcptest --config .mcp.json --server kali suite cases.json

exit codes:
  0  success
  1  the tool returned is_error=True, or a suite case failed
  2  connection, transport or protocol failure — nothing was tested
""")
    p.add_argument("-V", "--version", action="version",
                   version=f"%(prog)s {VERSION}")

    t = p.add_argument_group(
        "transport", "exactly one is required"
    ).add_mutually_exclusive_group(required=True)
    t.add_argument("--stdio", metavar="CMD",
                   help='command line to spawn, e.g. "uvx my-server --flag"')
    t.add_argument("--http", metavar="URL",
                   help="Streamable HTTP endpoint, usually ending in /mcp")
    t.add_argument("--sse", metavar="URL",
                   help="legacy HTTP+SSE endpoint (deprecated by the spec)")
    t.add_argument("--config", metavar="FILE",
                   help="read the server from a .mcp.json; needs --server")

    conn = p.add_argument_group("connection")
    conn.add_argument("--server", metavar="NAME",
                      help="which entry of --config to use")
    conn.add_argument("--mode", default="auto", metavar="MODE",
                      help="auto: probe with server/discover, fall back to the "
                           "initialize handshake (default). legacy: force the "
                           "handshake, the only era with a server-to-client "
                           "back-channel. Or pin a version, e.g. 2026-07-28")
    conn.add_argument("--timeout", type=float, default=30.0, metavar="SEC",
                      help="per-request timeout in seconds (default: 30)")
    conn.add_argument("-H", "--header", metavar="'H: V'", type=header,
                      action="append", default=[],
                      help="extra HTTP header, repeatable; --http only")
    conn.add_argument("-e", "--env", metavar="K=V", type=kv,
                      action="append", default=[],
                      help="environment variable for a stdio server, repeatable")
    conn.add_argument("--inherit-env", action="store_true",
                      help="give the child your whole environment; without this "
                           "it gets the SDK's allow-list plus -e")
    conn.add_argument("--cwd", metavar="DIR",
                      help="working directory for a stdio server")

    out = p.add_argument_group("behaviour")
    out.add_argument("--elicit", metavar="JSON|decline",
                     help="answer elicitation requests instead of failing; note "
                          "this declares the capability to the server")
    out.add_argument("--log-level", metavar="LEVEL",
                     help="ask a 2026-era server to send logs at this level")
    out.add_argument("--json", action="store_true",
                     help="machine-readable output for list and call")

    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND",
                           title="commands",
                           description="run mcptest COMMAND --help for details")

    sub.add_parser(
        "info", formatter_class=raw,
        help="connect and summarize the server",
        description=para("Connect and print the negotiated protocol version, the "
                         "server's identity and capabilities, and how many tools, "
                         "resources and prompts it offers. The fastest way to tell "
                         "'server is down' from 'server is up, tool is missing'."))

    pl = sub.add_parser(
        "list", formatter_class=raw,
        help="list the tools a server exposes",
        description=para("List every tool, paging until the server stops handing "
                         "back a cursor. With --json, prints the raw definitions: "
                         "the same text a model would be given."))
    pl.add_argument("--schema", action="store_true",
                    help="print the full input and output JSON Schemas")
    pl.add_argument("--resources", action="store_true",
                    help="also list resources and resource templates")
    pl.add_argument("--prompts", action="store_true",
                    help="also list prompts and their arguments")

    pd = sub.add_parser(
        "describe", formatter_class=raw,
        help="spell out the arguments one tool needs",
        description=para("Break a tool's input schema into required and "
                         "optional arguments, with types, constraints and "
                         "descriptions, and print an invocation you can paste. "
                         "With --json, prints the raw input schema instead."),
        epilog="""\
example:
  mcptest --config .mcp.json --server kali describe run_command
""")
    pd.add_argument("name", help="tool name, as shown by `list`")

    pc = sub.add_parser(
        "call", formatter_class=raw,
        help="invoke one tool",
        description=para("Call a single tool. Arguments come from --args, then "
                              "--args-file, then -a; the first one given wins."),
        epilog="""\
examples:
  mcptest ... call read_file --args '{"path": "/tmp/a.log"}'
  mcptest ... call write_file --args-file payload.json
  mcptest ... call scan -a host=10.0.4.1 -a deep=true -a ports='[22,443]'
""")
    pc.add_argument("name", help="tool name, as shown by `list`")
    pc.add_argument("--args", metavar="JSON", help="arguments as a JSON object")
    pc.add_argument("--args-file", metavar="FILE",
                    help="read the argument object from a file")
    pc.add_argument("-a", "--arg", metavar="K=V", action="append",
                    help="one argument, repeatable; the value is parsed as JSON "
                         "when it can be, kept as a string otherwise")
    pc.add_argument("--no-check", action="store_true",
                    help="skip the local tools/list pre-flight, so an unknown "
                         "tool or an incomplete payload reaches the server and "
                         "you see how it responds")

    ps = sub.add_parser(
        "suite", formatter_class=raw,
        help="run a JSON file of assertions",
        description=para("Run each case and report pass/fail. Exits 1 if any case "
                         "fails, so it drops into CI as-is."),
        epilog="""\
each case is an object:
  tool                   required, the tool name
  arguments              the argument object, default {}
  expect_error           whether is_error should be True, default false
  expect_contains        substring that must appear in the result
  expect_structured_key  key that must be non-null in structured_content
  label                  name shown in the output
""")
    ps.add_argument("file", help="path to the JSON file of cases")

    return p


async def main_async(args) -> int:
    handler = {"info": cmd_info, "list": cmd_list, "describe": cmd_describe,
               "call": cmd_call, "suite": cmd_suite}[args.cmd]
    async with connect(args) as client:
        return await handler(Session(client, args.timeout), args)


def leaf_errors(exc: BaseException) -> list[BaseException]:
    """Flatten anyio's nested ExceptionGroups down to the real causes.

    Every request runs inside the SDK's task groups, so anything raised while a
    session is open comes back wrapped — usually twice. Reporting the wrapper
    ("unhandled errors in a TaskGroup") reports nothing at all.
    """
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in leaf_errors(sub)]
    return [exc]


def report(errors: list[BaseException], timeout: float) -> int:
    """Print every real cause behind a failed run and pick an exit code."""
    if any(isinstance(exc, KeyboardInterrupt) for exc in errors):
        return 130
    for exc in errors:
        if isinstance(exc, SystemExit):
            print(f"error: {exc}", file=sys.stderr)
        elif isinstance(exc, asyncio.TimeoutError):
            print(f"error: no answer within {timeout}s", file=sys.stderr)
        elif isinstance(exc, MCPError):
            print(f"error: protocol error: {exc}", file=sys.stderr)
        else:
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 2


def main() -> int:
    args = build_parser().parse_args()
    if SDK_ERROR is not None:
        print(f'error: missing dependency ({SDK_ERROR.name}): '
              'pip install "mcp>=2,<3"', file=sys.stderr)
        return 2
    args.env = dict(args.env)
    args.header = dict(args.header)
    for attr in ("stdio", "http", "sse"):
        setattr(args, attr, getattr(args, attr, None))
    apply_config(args)

    try:
        return asyncio.run(main_async(args))
    except BaseException as exc:  # re-reported by report(), never swallowed
        return report(leaf_errors(exc), args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())