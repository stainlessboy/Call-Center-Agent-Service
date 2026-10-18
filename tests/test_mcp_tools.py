"""MCP client plumbing (roadmap П.8 — app/agent/mcp_tools.py).

No network, no MCP server: every test either exercises pure config parsing or
mocks ``MultiServerMCPClient``. The property that actually matters and is
asserted from several angles: a broken or unreachable MCP setup degrades to
"no MCP tools", never to an exception reaching the caller — because the caller
is app startup and the agent's tool list.
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from app.agent import mcp_tools


def run(coro):
    return asyncio.run(coro)


_ENV_VARS = (
    "MCP_ENABLED",
    "MCP_SERVERS",
    "MCP_CRM_URL",
    "MCP_CRM_TOKEN",
    "MCP_LOAD_TIMEOUT_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    mcp_tools._client_class.cache_clear()
    mcp_tools.set_mcp_tools([])
    yield
    mcp_tools._client_class.cache_clear()
    mcp_tools.set_mcp_tools([])
    mcp_tools._client = None


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeClient:
    """Stand-in for MultiServerMCPClient: records the config it was built with."""

    last_connections = None

    def __init__(self, connections=None, **kwargs):
        type(self).last_connections = connections
        self.connections = connections
        self.kwargs = kwargs

    async def get_tools(self, *, server_name=None):
        return [FakeTool("get_application_status"), FakeTool("create_application")]


@pytest.fixture
def fake_client(monkeypatch):
    """Patch the real client class at its source, where the lazy import resolves."""
    import langchain_mcp_adapters.client as client_mod

    FakeClient.last_connections = None
    monkeypatch.setattr(client_mod, "MultiServerMCPClient", FakeClient)
    mcp_tools._client_class.cache_clear()
    return FakeClient


# ---------------------------------------------------------------------------
# mcp_enabled
# ---------------------------------------------------------------------------

class TestMcpEnabled:
    def test_off_by_default(self):
        assert mcp_tools.mcp_enabled() is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("MCP_ENABLED", value)
        assert mcp_tools.mcp_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "  ", "maybe"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("MCP_ENABLED", value)
        assert mcp_tools.mcp_enabled() is False


# ---------------------------------------------------------------------------
# _server_config
# ---------------------------------------------------------------------------

class TestServerConfig:
    def test_no_env_no_servers(self):
        assert mcp_tools._server_config() == {}

    def test_valid_json_http_server(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_SERVERS",
            '{"crm": {"url": "https://crm.example/mcp", "transport": "http",'
            ' "headers": {"Authorization": "Bearer xyz"}}}',
        )
        cfg = mcp_tools._server_config()
        assert set(cfg) == {"crm"}
        # "http" is normalised to the canonical spelling create_session expects.
        assert cfg["crm"]["transport"] == "streamable_http"
        assert cfg["crm"]["url"] == "https://crm.example/mcp"
        assert cfg["crm"]["headers"] == {"Authorization": "Bearer xyz"}

    def test_multiple_servers(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_SERVERS",
            '{"crm": {"url": "https://crm.example/mcp"},'
            ' "scoring": {"url": "https://score.example/mcp", "transport": "sse"}}',
        )
        cfg = mcp_tools._server_config()
        assert set(cfg) == {"crm", "scoring"}
        # transport inferred from the presence of a url
        assert cfg["crm"]["transport"] == "streamable_http"
        assert cfg["scoring"]["transport"] == "sse"

    def test_stdio_server_gets_args_default(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_SERVERS",
            '{"mock": {"command": "python", "transport": "stdio"}}',
        )
        cfg = mcp_tools._server_config()
        # create_session raises without 'args'; a command-only entry means [].
        assert cfg["mock"] == {"command": "python", "transport": "stdio", "args": []}

    def test_stdio_args_preserved(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_SERVERS",
            '{"mock": {"command": "python", "args": ["srv.py"]}}',
        )
        cfg = mcp_tools._server_config()
        assert cfg["mock"]["transport"] == "stdio"
        assert cfg["mock"]["args"] == ["srv.py"]

    def test_malformed_json_yields_no_servers(self, monkeypatch, caplog):
        monkeypatch.setenv("MCP_SERVERS", "{not json at all")
        with caplog.at_level("WARNING"):
            cfg = mcp_tools._server_config()
        assert cfg == {}
        assert "MCP_SERVERS is not valid JSON" in caplog.text

    def test_json_array_rejected(self, monkeypatch, caplog):
        monkeypatch.setenv("MCP_SERVERS", '["crm"]')
        with caplog.at_level("WARNING"):
            assert mcp_tools._server_config() == {}
        assert "must be a JSON object" in caplog.text

    def test_bad_entry_skipped_good_entry_kept(self, monkeypatch, caplog):
        monkeypatch.setenv(
            "MCP_SERVERS",
            '{"broken": {"transport": "http"},'
            ' "good": {"url": "https://ok.example/mcp"}}',
        )
        with caplog.at_level("WARNING"):
            cfg = mcp_tools._server_config()
        # One malformed server must not cost us the others.
        assert set(cfg) == {"good"}
        assert "broken" in caplog.text

    def test_unknown_transport_rejected(self, monkeypatch, caplog):
        monkeypatch.setenv(
            "MCP_SERVERS", '{"crm": {"url": "https://x/mcp", "transport": "carrier-pigeon"}}',
        )
        with caplog.at_level("WARNING"):
            assert mcp_tools._server_config() == {}

    def test_non_object_entry_rejected(self, monkeypatch):
        monkeypatch.setenv("MCP_SERVERS", '{"crm": "https://x/mcp"}')
        assert mcp_tools._server_config() == {}

    def test_non_dict_headers_dropped_but_server_kept(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_SERVERS", '{"crm": {"url": "https://x/mcp", "headers": "Bearer z"}}',
        )
        cfg = mcp_tools._server_config()
        assert set(cfg) == {"crm"}
        assert "headers" not in cfg["crm"]

    # --- single-server convenience vars ---

    def test_crm_url_shorthand(self, monkeypatch):
        monkeypatch.setenv("MCP_CRM_URL", "http://127.0.0.1:8765/mcp")
        cfg = mcp_tools._server_config()
        assert cfg == {
            "crm": {"url": "http://127.0.0.1:8765/mcp", "transport": "streamable_http"},
        }

    def test_crm_url_with_token_sets_bearer_header(self, monkeypatch):
        monkeypatch.setenv("MCP_CRM_URL", "https://crm.example/mcp")
        monkeypatch.setenv("MCP_CRM_TOKEN", "s3cret")
        cfg = mcp_tools._server_config()
        assert cfg["crm"]["headers"] == {"Authorization": "Bearer s3cret"}

    def test_token_without_url_is_ignored(self, monkeypatch):
        monkeypatch.setenv("MCP_CRM_TOKEN", "s3cret")
        assert mcp_tools._server_config() == {}

    def test_json_entry_wins_over_shorthand(self, monkeypatch):
        monkeypatch.setenv("MCP_CRM_URL", "http://shorthand/mcp")
        monkeypatch.setenv("MCP_SERVERS", '{"crm": {"url": "http://explicit/mcp"}}')
        cfg = mcp_tools._server_config()
        assert cfg["crm"]["url"] == "http://explicit/mcp"

    def test_shorthand_and_json_combine_for_different_names(self, monkeypatch):
        monkeypatch.setenv("MCP_CRM_URL", "http://crm/mcp")
        monkeypatch.setenv("MCP_SERVERS", '{"scoring": {"url": "http://score/mcp"}}')
        assert set(mcp_tools._server_config()) == {"crm", "scoring"}


# ---------------------------------------------------------------------------
# load_mcp_tools
# ---------------------------------------------------------------------------

class TestLoadMcpTools:
    def test_returns_empty_when_disabled(self, monkeypatch, fake_client):
        monkeypatch.setenv("MCP_CRM_URL", "http://127.0.0.1:8765/mcp")
        # MCP_ENABLED unset
        assert run(mcp_tools.load_mcp_tools()) == []
        assert fake_client.last_connections is None  # never even connected

    def test_returns_empty_when_enabled_but_unconfigured(self, monkeypatch, caplog):
        monkeypatch.setenv("MCP_ENABLED", "true")
        with caplog.at_level("WARNING"):
            assert run(mcp_tools.load_mcp_tools()) == []
        assert "no usable server config" in caplog.text

    def test_loads_tools_and_passes_config(self, monkeypatch, fake_client):
        monkeypatch.setenv("MCP_ENABLED", "true")
        monkeypatch.setenv("MCP_CRM_URL", "http://127.0.0.1:8765/mcp")
        tools = run(mcp_tools.load_mcp_tools())
        assert [t.name for t in tools] == [
            "get_application_status", "create_application",
        ]
        assert fake_client.last_connections["crm"]["url"] == "http://127.0.0.1:8765/mcp"

    def test_returns_empty_when_client_constructor_raises(self, monkeypatch, caplog):
        monkeypatch.setenv("MCP_ENABLED", "true")
        monkeypatch.setenv("MCP_CRM_URL", "http://127.0.0.1:8765/mcp")

        import langchain_mcp_adapters.client as client_mod

        class Boom:
            def __init__(self, *a, **kw):
                raise RuntimeError("bad connection config")

        monkeypatch.setattr(client_mod, "MultiServerMCPClient", Boom)
        mcp_tools._client_class.cache_clear()
        with caplog.at_level("WARNING"):
            assert run(mcp_tools.load_mcp_tools()) == []
        assert "Failed to load MCP tools" in caplog.text

    def test_returns_empty_when_get_tools_raises(self, monkeypatch, caplog):
        """An unreachable MCP server must not break the bot."""
        monkeypatch.setenv("MCP_ENABLED", "true")
        monkeypatch.setenv("MCP_CRM_URL", "http://127.0.0.1:8765/mcp")

        import langchain_mcp_adapters.client as client_mod

        class Unreachable(FakeClient):
            async def get_tools(self, *, server_name=None):
                raise ConnectionError("connection refused")

        monkeypatch.setattr(client_mod, "MultiServerMCPClient", Unreachable)
        mcp_tools._client_class.cache_clear()
        with caplog.at_level("WARNING"):
            assert run(mcp_tools.load_mcp_tools()) == []
        assert "connection refused" in caplog.text

    def test_returns_empty_on_timeout(self, monkeypatch, caplog):
        monkeypatch.setenv("MCP_ENABLED", "true")
        monkeypatch.setenv("MCP_CRM_URL", "http://127.0.0.1:8765/mcp")
        monkeypatch.setenv("MCP_LOAD_TIMEOUT_SECONDS", "0.01")

        import langchain_mcp_adapters.client as client_mod

        class Hanging(FakeClient):
            async def get_tools(self, *, server_name=None):
                await asyncio.sleep(5)

        monkeypatch.setattr(client_mod, "MultiServerMCPClient", Hanging)
        mcp_tools._client_class.cache_clear()
        with caplog.at_level("WARNING"):
            assert run(mcp_tools.load_mcp_tools()) == []
        assert "timed out" in caplog.text

    def test_returns_empty_when_package_missing(self, monkeypatch, caplog):
        monkeypatch.setenv("MCP_ENABLED", "true")
        monkeypatch.setenv("MCP_CRM_URL", "http://127.0.0.1:8765/mcp")
        # keep the lru_cache interface the fixture teardown relies on
        monkeypatch.setattr(
            mcp_tools, "_client_class", functools.lru_cache(maxsize=1)(lambda: None),
        )
        with caplog.at_level("WARNING"):
            assert run(mcp_tools.load_mcp_tools()) == []

    def test_malformed_json_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("MCP_ENABLED", "true")
        monkeypatch.setenv("MCP_SERVERS", "}{")
        assert run(mcp_tools.load_mcp_tools()) == []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_empty_before_load(self):
        assert mcp_tools.get_mcp_tools() == []

    def test_set_then_get(self):
        tools = [FakeTool("a"), FakeTool("b")]
        mcp_tools.set_mcp_tools(tools)
        assert [t.name for t in mcp_tools.get_mcp_tools()] == ["a", "b"]

    def test_set_none_clears(self):
        mcp_tools.set_mcp_tools([FakeTool("a")])
        mcp_tools.set_mcp_tools(None)
        assert mcp_tools.get_mcp_tools() == []

    def test_get_returns_copy(self):
        mcp_tools.set_mcp_tools([FakeTool("a")])
        got = mcp_tools.get_mcp_tools()
        got.append(FakeTool("b"))
        assert len(mcp_tools.get_mcp_tools()) == 1

    def test_aclose_clears_registry(self, monkeypatch, fake_client):
        monkeypatch.setenv("MCP_ENABLED", "true")
        monkeypatch.setenv("MCP_CRM_URL", "http://127.0.0.1:8765/mcp")
        mcp_tools.set_mcp_tools(run(mcp_tools.load_mcp_tools()))
        assert mcp_tools.get_mcp_tools()
        run(mcp_tools.aclose())
        assert mcp_tools.get_mcp_tools() == []
        assert mcp_tools._client is None

    def test_aclose_without_client_is_noop(self):
        run(mcp_tools.aclose())
        assert mcp_tools.get_mcp_tools() == []

    def test_aclose_swallows_client_errors(self, monkeypatch):
        class Rude:
            async def aclose(self):
                raise RuntimeError("nope")

        monkeypatch.setattr(mcp_tools, "_client", Rude())
        run(mcp_tools.aclose())  # must not raise
        assert mcp_tools._client is None


# ---------------------------------------------------------------------------
# Sensitive (write) tools — input to the П.6 approval gate
# ---------------------------------------------------------------------------

class TestSensitiveTools:
    def test_write_tools_listed(self):
        for name in ("create_application", "block_card", "transfer"):
            assert name in mcp_tools.SENSITIVE_MCP_TOOLS

    def test_set_is_immutable(self):
        assert isinstance(mcp_tools.SENSITIVE_MCP_TOOLS, frozenset)

    @pytest.mark.parametrize(
        "name", ["create_application", "CREATE_APPLICATION", "block_card", "transfer"],
    )
    def test_is_sensitive(self, name):
        assert mcp_tools.is_sensitive_tool(name) is True

    @pytest.mark.parametrize(
        "name", ["get_application_status", "get_scoring", "faq_lookup", "", "  "],
    )
    def test_read_tools_are_not_sensitive(self, name):
        assert mcp_tools.is_sensitive_tool(name) is False

    def test_server_prefixed_name_matches(self):
        """MultiServerMCPClient(tool_name_prefix=True) prefixes the server name."""
        assert mcp_tools.is_sensitive_tool("crm_create_application") is True
        assert mcp_tools.is_sensitive_tool("crm_get_application_status") is False

    def test_mock_server_write_tool_is_gated(self):
        """The mock server's WRITE tool must be caught by the gate."""
        assert mcp_tools.is_sensitive_tool("create_application") is True
