"""MCP client plumbing: external bank services as agent tools (roadmap П.8).

Why this module exists
----------------------
CRM, scoring, application status and FX live in other teams' systems. Writing a
hand-rolled ``@lc_tool`` in ``app/agent/tools.py`` for each of them means this
repo owns their schemas, their auth and their breaking changes. MCP inverts
that: the owning service publishes an MCP server, and the agent discovers the
tools at startup. Adding a service becomes an env-var change, not a release.

Design rules, all of them load-bearing
--------------------------------------
1. **Nothing here may raise into the caller.** An MCP server is a third-party
   network dependency. If it is down, misconfigured, or the package is not
   installed, the bot must degrade to "a bot without those tools" — never to a
   bot that fails to start or fails a turn. Every public function swallows and
   logs. This mirrors ``app/agent/tracing.py``.
2. **Env is read here, with ``os.getenv``**, not through ``app/config.py``, for
   the same reason ``tracing.py`` does: this module is optional infrastructure
   and must not widen the settings dataclass that every process loads.
3. **Load once, at startup.** ``load_mcp_tools()`` is awaited in the FastAPI
   lifespan; the result goes into the process-wide registry below, which the
   agent reads without importing FastAPI (same pattern and same reason as
   ``app/services/middleware_registry.py``).

Wiring (done by app startup, not by this module)::

    from app.agent.mcp_tools import load_mcp_tools, set_mcp_tools, aclose
    # lifespan startup
    set_mcp_tools(await load_mcp_tools())
    # lifespan shutdown
    await aclose()

and, where the agent's tool list is assembled::

    tools = [*_FAQ_TOOLS, *get_mcp_tools()]

Write tools and the approval gate
---------------------------------
``SENSITIVE_MCP_TOOLS`` names the MCP tools that mutate bank state. It is
deliberately declared here, next to the loader, so the list cannot drift away
from the tools it describes. The agent side (roadmap П.6) feeds it into
``HumanInTheLoopMiddleware``::

    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from app.agent.mcp_tools import get_mcp_tools, is_sensitive_tool

    gated = {
        t.name: {"allowed_decisions": ["approve", "reject"]}
        for t in get_mcp_tools() if is_sensitive_tool(t.name)
    }
    middleware = [HumanInTheLoopMiddleware(interrupt_on=gated)]

Until that middleware lands, this set is inert metadata: importing it has no
effect on behaviour, so nothing here can gate a tool by accident.

Environment variables
---------------------
``MCP_ENABLED``           — master switch, default ``false``.
``MCP_SERVERS``           — JSON object, ``{"name": {...connection...}}``.
``MCP_CRM_URL``           — convenience shorthand for the single-server case.
``MCP_CRM_TOKEN``         — bearer token sent with ``MCP_CRM_URL``.
``MCP_LOAD_TIMEOUT_SECONDS`` — cap on the whole discovery step, default 15.

See ``docs/MCP.md`` for examples and the mock server.
"""
from __future__ import annotations

import asyncio
import json
import logging as _logging
import os
from functools import lru_cache
from typing import Any, Optional

_logger = _logging.getLogger(__name__)

# Transports accepted by langchain_mcp_adapters.sessions.create_session.
# "http" and "streamable-http" are aliases the library maps onto
# "streamable_http"; we normalise to the canonical spelling so a typo in env
# surfaces here as a clear warning instead of a ValueError inside a session.
_HTTP_ALIASES = {"http", "streamable_http", "streamable-http"}
_URL_TRANSPORTS = {"streamable_http", "sse", "websocket"}
_VALID_TRANSPORTS = _URL_TRANSPORTS | {"stdio"}

_DEFAULT_LOAD_TIMEOUT = 15.0

#: MCP tools that WRITE to bank systems and therefore need human approval
#: before execution. Names are matched case-insensitively and ignoring any
#: ``<server>_`` prefix (``MultiServerMCPClient(tool_name_prefix=True)``), so
#: both ``create_application`` and ``crm_create_application`` are covered.
SENSITIVE_MCP_TOOLS = frozenset({
    "create_application",
    "submit_application",
    "update_application",
    "cancel_application",
    "block_card",
    "unblock_card",
    "transfer",
    "transfer_funds",
})


def _env_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str) -> Optional[str]:
    value = (os.getenv(name) or "").strip()
    return value or None


def mcp_enabled() -> bool:
    """True when MCP_ENABLED asks for external MCP tools. Default: off."""
    return _env_truthy(os.getenv("MCP_ENABLED"))


def _load_timeout() -> float:
    """Cap on tool discovery, so an unreachable server cannot hang startup."""
    raw = _env_str("MCP_LOAD_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_LOAD_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        _logger.warning(
            "MCP_LOAD_TIMEOUT_SECONDS=%r is not a number — using %.0fs.",
            raw, _DEFAULT_LOAD_TIMEOUT,
        )
        return _DEFAULT_LOAD_TIMEOUT
    if value <= 0:
        _logger.warning(
            "MCP_LOAD_TIMEOUT_SECONDS=%r must be positive — using %.0fs.",
            raw, _DEFAULT_LOAD_TIMEOUT,
        )
        return _DEFAULT_LOAD_TIMEOUT
    return value


def _normalise_entry(name: str, raw: Any) -> Optional[dict]:
    """Validate one server entry and return a connection dict, or None.

    Returning None (with a warning) rather than raising is the point: one
    malformed server in ``MCP_SERVERS`` must not cost us the other servers,
    and must never take the process down.
    """
    if not isinstance(raw, dict):
        _logger.warning(
            "MCP server %r ignored: expected a JSON object, got %s.",
            name, type(raw).__name__,
        )
        return None

    entry = dict(raw)
    url = entry.get("url")
    command = entry.get("command")

    transport = str(entry.get("transport") or "").strip().lower()
    if transport in _HTTP_ALIASES:
        transport = "streamable_http"
    if not transport:
        # Infer from the shape of the entry: a URL means remote, a command
        # means a locally spawned process.
        transport = "streamable_http" if url else "stdio" if command else ""
    if transport not in _VALID_TRANSPORTS:
        _logger.warning(
            "MCP server %r ignored: transport %r is not one of %s.",
            name, entry.get("transport"), sorted(_VALID_TRANSPORTS),
        )
        return None

    if transport in _URL_TRANSPORTS:
        if not url or not isinstance(url, str):
            _logger.warning(
                "MCP server %r ignored: transport %r requires a string 'url'.",
                name, transport,
            )
            return None
    elif not command or not isinstance(command, str):
        _logger.warning(
            "MCP server %r ignored: transport 'stdio' requires a string 'command'.",
            name,
        )
        return None

    entry["transport"] = transport

    if transport == "stdio":
        # create_session raises if 'args' is absent, so supply the empty list
        # a command-only config obviously means.
        args = entry.get("args") or []
        if not isinstance(args, list):
            _logger.warning(
                "MCP server %r ignored: 'args' must be a JSON array.", name,
            )
            return None
        entry["args"] = [str(a) for a in args]

    headers = entry.get("headers")
    if headers is not None and not isinstance(headers, dict):
        _logger.warning(
            "MCP server %r: 'headers' must be a JSON object — ignoring them.", name,
        )
        entry.pop("headers", None)

    return entry


def _servers_from_json() -> dict[str, dict]:
    """Parse ``MCP_SERVERS``. Any problem logs and yields no servers."""
    raw = _env_str("MCP_SERVERS")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        _logger.warning(
            "MCP_SERVERS is not valid JSON (%s) — no MCP servers configured "
            "from it. Expected an object like "
            '{"crm": {"url": "https://host/mcp", "transport": "http"}}.',
            exc,
        )
        return {}
    if not isinstance(parsed, dict):
        _logger.warning(
            "MCP_SERVERS must be a JSON object mapping server name -> config, "
            "got %s — ignoring.", type(parsed).__name__,
        )
        return {}

    servers: dict[str, dict] = {}
    for name, entry in parsed.items():
        key = str(name).strip()
        if not key:
            _logger.warning("MCP server with an empty name ignored.")
            continue
        normalised = _normalise_entry(key, entry)
        if normalised is not None:
            servers[key] = normalised
    return servers


def _crm_from_env() -> dict[str, dict]:
    """The single-server convenience case: MCP_CRM_URL (+ MCP_CRM_TOKEN)."""
    url = _env_str("MCP_CRM_URL")
    if not url:
        return {}
    entry: dict[str, Any] = {"url": url, "transport": "streamable_http"}
    token = _env_str("MCP_CRM_TOKEN")
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return {"crm": entry}


def _server_config() -> dict[str, dict]:
    """Build the MultiServerMCPClient connection map from the environment.

    ``MCP_SERVERS`` wins over the ``MCP_CRM_*`` shorthand when both define
    ``crm``: the explicit, fully-specified config is the more deliberate one.
    Never raises — a bad config yields fewer servers, or none.
    """
    servers = _crm_from_env()
    from_json = _servers_from_json()
    for name, entry in from_json.items():
        if name in servers:
            _logger.info(
                "MCP server %r defined both in MCP_SERVERS and via MCP_CRM_URL — "
                "using the MCP_SERVERS entry.", name,
            )
        servers[name] = entry
    return servers


# ---------------------------------------------------------------------------
# Client + tool loading
# ---------------------------------------------------------------------------

# The live client, kept only so `aclose()` can release it. MultiServerMCPClient
# opens a fresh session per tool call, so there is no long-lived connection to
# leak here; this is future-proofing plus a handle for tests.
_client: Any = None


@lru_cache(maxsize=1)
def _client_class() -> Any:
    """Import MultiServerMCPClient once, or return None if unavailable.

    ``langchain-mcp-adapters`` is a hard requirement in ``requirements.txt``,
    but a deployment that has not reinstalled dependencies yet must still boot
    — with MCP simply off — rather than crash on import.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        _logger.warning(
            "MCP is enabled but langchain-mcp-adapters is not installed (%s) — "
            "no MCP tools will be loaded. Run: pip install langchain-mcp-adapters",
            exc,
        )
        return None
    return MultiServerMCPClient


async def load_mcp_tools() -> list:
    """Connect to the configured MCP servers and return their LangChain tools.

    Returns ``[]`` — never raises, never blocks past the timeout — when MCP is
    disabled, unconfigured, misconfigured, the package is missing, or any
    server is unreachable. Callers add the result to the agent's tool list; an
    empty list simply means the agent runs without those tools.
    """
    global _client

    if not mcp_enabled():
        _logger.debug("MCP disabled (MCP_ENABLED is not set) — no MCP tools.")
        return []

    servers = _server_config()
    if not servers:
        _logger.warning(
            "MCP_ENABLED is set but no usable server config was found. Set "
            "MCP_SERVERS (JSON) or MCP_CRM_URL. No MCP tools loaded.",
        )
        return []

    client_cls = _client_class()
    if client_cls is None:
        return []

    try:
        client = client_cls(servers)
        tools = await asyncio.wait_for(client.get_tools(), timeout=_load_timeout())
    except asyncio.TimeoutError:
        _logger.warning(
            "MCP tool discovery timed out after %.0fs (servers: %s) — "
            "continuing without MCP tools.",
            _load_timeout(), ", ".join(sorted(servers)),
        )
        return []
    except Exception as exc:
        _logger.warning(
            "Failed to load MCP tools from %s (%s: %s) — continuing without them.",
            ", ".join(sorted(servers)), type(exc).__name__, exc,
        )
        return []

    tools = list(tools or [])
    _client = client
    names = [getattr(t, "name", "?") for t in tools]
    _logger.info(
        "Loaded %d MCP tool(s) from %d server(s): %s",
        len(tools), len(servers), ", ".join(names) or "-",
    )
    sensitive = [n for n in names if is_sensitive_tool(n)]
    if sensitive:
        _logger.info(
            "MCP write tools requiring human approval: %s", ", ".join(sensitive),
        )
    return tools


def is_sensitive_tool(name: str) -> bool:
    """True when ``name`` is a write tool that must go through an approval gate.

    Tolerates the ``<server>_`` prefix that
    ``MultiServerMCPClient(tool_name_prefix=True)`` adds, so callers do not
    have to know how the client was configured.
    """
    if not name:
        return False
    candidate = name.strip().lower()
    if candidate in SENSITIVE_MCP_TOOLS:
        return True
    return any(candidate.endswith(f"_{tool}") for tool in SENSITIVE_MCP_TOOLS)


# ---------------------------------------------------------------------------
# Process-wide registry (mirrors app/services/middleware_registry.py)
# ---------------------------------------------------------------------------

_mcp_tools: list = []


def set_mcp_tools(tools: Optional[list]) -> None:
    """Register the loaded tools. Called once from the FastAPI lifespan."""
    global _mcp_tools
    _mcp_tools = list(tools or [])


def get_mcp_tools() -> list:
    """The registered MCP tools; ``[]`` when MCP is off or not loaded yet.

    Returns a copy, so a caller building an agent tool list cannot mutate the
    registry by accident.
    """
    return list(_mcp_tools)


async def aclose() -> None:
    """Release the MCP client on shutdown. Never raises."""
    global _client
    client, _client = _client, None
    set_mcp_tools([])
    if client is None:
        return
    # MultiServerMCPClient 0.3.x opens a session per call and exposes no
    # close(); call one only if a future version grows it.
    for attr in ("aclose", "close"):
        closer = getattr(client, attr, None)
        if closer is None:
            continue
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning("Error closing MCP client: %s", exc)
        return
