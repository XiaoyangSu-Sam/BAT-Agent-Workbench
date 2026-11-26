import asyncio
import gzip
import json
from functools import partial

import httpx
import pytest

from agent_harness.errors import HarnessError
from agent_harness.runtime.manager import RunManager
from agent_harness.runtime.tools import ToolRegistry
from agent_harness.store import Store
from agent_harness.tests.test_runtime import wait_approval


@pytest.fixture(autouse=True)
def search_environment(monkeypatch):
    for name in ("BAT_SEARXNG_URL", "BRAVE_SEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def mock_provider(monkeypatch, respond):
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(respond)))


def streamed_response(status, body, headers=None):
    return httpx.Response(status, stream=httpx.ByteStream(body), headers=headers)


def test_search_disabled_without_configuration(settings, skills):
    registry = ToolRegistry(settings, skills)
    assert settings.public()["web_search_provider"] is None
    assert "web_search" not in [s["function"]["name"] for s in registry.schemas()]
    with pytest.raises(HarnessError) as error:
        registry.validate("web_search", {"query": "cats"})
    assert error.value.code == "unknown_tool"


@pytest.mark.parametrize("provider", ["brave", "searxng"])
def test_provider_request_and_source_filtering(provider, monkeypatch, settings, skills):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-search-key")
    monkeypatch.setenv("LLM_API_KEY", "test-model-key")
    if provider == "searxng":
        monkeypatch.setenv("BAT_SEARXNG_URL", "http://localhost:8080/instance/search")
    public = settings.public()
    assert public["web_search_provider"] == provider
    assert "test-search-key" not in json.dumps(public) and "/instance/search" not in json.dumps(public)
    def respond(request):
        assert request.method == "GET"
        assert request.url.params["q"] == "a & b 文"
        assert "test-model-key" not in str(request.headers)
        assert "authorization" not in request.headers
        if provider == "searxng":
            assert request.url.host == "localhost" and request.url.path == "/instance/search"
            assert request.url.params["format"] == "json"
            assert "x-subscription-token" not in request.headers
            snippet_key = "content"
        else:
            assert str(request.url).startswith("https://api.search.brave.com/res/v1/web/search?")
            assert request.headers["x-subscription-token"] == "test-search-key"
            assert request.url.params["count"] == "5"
            snippet_key = "description"
        rows = [
            {"title": "Unsafe URL", "url": url} for url in (
                "javascript:alert(1)", "//other.test/path", "https://u:p@example.org/",
                "https://other.test\\@example.org/", "https://ex ample.org/", "http://[broken",
            )
        ]
        rows += [{"title": "<b>A</b> &amp; B", "url": f"https://example.org/{i}",
                  snippet_key: "A <strong>useful</strong>   &#38; documented <b>GET</b>/search.<script>ignore</script>"}
                 for i in range(8)]
        payload = {"results": rows} if provider == "searxng" else {"type": "search", "web": {"results": rows}}
        return streamed_response(200, json.dumps(payload).encode())
    mock_provider(monkeypatch, respond)
    result = asyncio.run(ToolRegistry(settings, skills).call("web_search", {"query": " a & b 文 "}))
    assert result == {"provider": provider, "results": [
        {"title": "A & B", "url": f"https://example.org/{i}", "snippet": "A useful & documented GET/search."}
        for i in range(5)
    ]}


@pytest.mark.parametrize("args", [
    {"query": "ab", "url": "https://alternate.test/"}, {"query": "a" * 401}, {"query": "   "}, {},
])
def test_search_accepts_only_bounded_queries(settings, skills, monkeypatch, args):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-search-key")
    mock_provider(monkeypatch, lambda request: pytest.fail("Unexpected request"))
    with pytest.raises(HarnessError) as error:
        asyncio.run(ToolRegistry(settings, skills).call("web_search", args))
    assert error.value.code == "invalid_tool_arguments"


@pytest.mark.parametrize(("status", "body", "code"), [
    (301, "", "search_redirect"), (403, "private detail", "search_access_denied"),
    (429, "private detail", "search_rate_limited"), (502, "", "search_unavailable"),
    (200, "<html>Private login page</html>", "search_invalid_response"),
    (200, '{"results": "wrong"}', "search_invalid_response"),
    (200, "x" * (1024 * 1024), "search_response_too_large"),
], ids=["redirect", "forbidden", "rate", "server", "html", "schema", "size"])
def test_provider_errors_do_not_expose_response_or_configuration(status, body, code, settings, skills, monkeypatch):
    monkeypatch.setenv("BAT_SEARXNG_URL", "https://private.test/search")
    seen = []
    def respond(request):
        seen.append(request.url.host)
        return streamed_response(status, body.encode(), {"Location": "https://other-private.test/"})
    mock_provider(monkeypatch, respond)
    with pytest.raises(HarnessError) as error:
        asyncio.run(ToolRegistry(settings, skills).call("web_search", {"query": "cats"}))
    assert error.value.code == code
    assert "private" not in json.dumps(error.value.as_dict()).lower()
    assert seen == ["private.test"]


@pytest.mark.parametrize("large", [False, True])
def test_gzip_results_respect_decompressed_limit(large, settings, skills, monkeypatch):
    monkeypatch.setenv("BAT_SEARXNG_URL", "https://configured.test/search")
    body = json.dumps({"results": [], "padding": "x" * (1024 * 1024) if large else ""}).encode()
    def respond(request):
        return streamed_response(200, gzip.compress(body), {"Content-Encoding": "gzip"})
    mock_provider(monkeypatch, respond)
    search = ToolRegistry(settings, skills).call("web_search", {"query": "bounded"})
    if large:
        with pytest.raises(HarnessError) as error:
            asyncio.run(search)
        assert error.value.code == "search_response_too_large"
    else:
        assert asyncio.run(search)["results"] == []


@pytest.mark.parametrize(("exc", "code"), [
    (httpx.ReadTimeout("private detail"), "search_timeout"),
    (httpx.ProxyError("private proxy URL"), "search_connection"),
])
def test_transport_errors_hide_details(exc, code, monkeypatch, settings, skills):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-search-key")
    def respond(request):
        raise exc
    mock_provider(monkeypatch, respond)
    with pytest.raises(HarnessError) as error:
        asyncio.run(ToolRegistry(settings, skills).call("web_search", {"query": "cats"}))
    assert error.value.code == code
    assert "private" not in str(error.value.as_dict())


def test_invalid_endpoint_isolated_from_other_tools(monkeypatch, settings, skills):
    monkeypatch.setenv("BAT_SEARXNG_URL", "https://user:private@example.org/search")
    mock_provider(monkeypatch, lambda request: pytest.fail("Invalid endpoint was contacted"))
    registry = ToolRegistry(settings, skills)
    assert asyncio.run(registry.call("list_files", {})) is not None
    with pytest.raises(HarnessError) as error:
        asyncio.run(registry.call("web_search", {"query": "cats"}))
    assert error.value.code == "search_configuration"
    assert "private" not in str(error.value.as_dict())


@pytest.mark.parametrize(("mode", "approved"), [("native", True), ("json", True), ("native", False)])
def test_search_approval_and_model_continuation(mode, approved, monkeypatch, settings, skills):
    monkeypatch.setenv("BAT_SEARXNG_URL", "https://configured.test/search")
    settings.models["demo"].tool_mode = mode
    sent = []
    def respond(request):
        sent.append(request.url.params["q"])
        return streamed_response(200, json.dumps({"results": [
            {"title": "Bulletin", "url": "https://bulletin.test/story", "content": "search evidence"}
        ]}).encode())
    mock_provider(monkeypatch, respond)

    class SearchModel:
        def __init__(self, config):
            pass

        async def complete(self, messages, schemas, emit, tools=True):
            if messages[-1]["content"] == "Find bulletin":
                schema = next(s["function"] for s in schemas if s["function"]["name"] == "web_search")
                assert schema["parameters"]["required"] == ["query"]
                args = {"query": "bulletin"}
                if mode == "native":
                    return {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "search-1", "type": "function",
                        "function": {"name": "web_search", "arguments": json.dumps(args)}}]}
                assert "web_search" in messages[0]["content"].split("Tools:")[-1]
                content = json.dumps({"action": "tool", "name": "web_search", "arguments": args})
            else:
                if mode == "native":
                    assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "search-1"
                    data = json.loads(messages[-1]["content"])
                else:
                    assert messages[-1]["role"] == "user"
                    data = json.loads(messages[-1]["content"].removeprefix("TOOL_RESULT "))
                source = data["result"]["results"][0]
                assert source["url"] == "https://bulletin.test/story" and source["snippet"] == "search evidence"
                content = source["url"] if mode == "native" else json.dumps({"action": "final", "content": source["url"]})
            return {"role": "assistant", "content": content}

    store = Store(settings.state_dir / "runs.sqlite3")
    manager = RunManager(settings, store, skills, SearchModel)
    session = store.create_session(approval_mode="all")
    async def scenario():
        run = manager.start(session["id"], "Find bulletin")
        approval = await wait_approval(manager, run)
        assert not sent
        manager.decide(run, approval, approved)
        await asyncio.wait_for(manager.tasks[run], 5)
        if approved:
            assert sent == ["bulletin"]
            assert store.run(run)["status"] == "completed", store.run(run)
        else:
            assert store.run(run)["error"]["code"] == "approval_denied"
            assert not sent
    asyncio.run(scenario())
