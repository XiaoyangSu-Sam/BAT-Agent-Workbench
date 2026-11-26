import asyncio
import base64
import json
import struct
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.config import ModelConfig
from agent_harness.diagnostics import diagnose_vision
from agent_harness.errors import HarnessError
from agent_harness.runtime.model import ModelClient
from agent_harness.server import create_app


def read_fixture_digits(url):
    prefix = "data:image/png;base64,"
    assert url.startswith(prefix)
    png = base64.b64decode(url[len(prefix):], validate=True)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    chunks = {}
    offset = 8
    while offset < len(png):
        size, kind = struct.unpack_from(">I4s", png, offset)
        data = png[offset + 8:offset + 8 + size]
        assert struct.unpack_from(">I", png, offset + 8 + size)[0] == zlib.crc32(kind + data)
        chunks[kind] = data
        offset += 12 + size
    assert list(chunks) == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, color, _, _, _ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, color) == (8, 0)
    pixels = zlib.decompress(chunks[b"IDAT"])
    stride = width + 1
    assert len(pixels) == stride * height
    # Read alternating 2 and 7 from two sample pixels per glyph.
    scale = height // 11
    digits = []
    for index in range(6):
        left = (2 + index * 6) * scale
        top_left = pixels[2 * scale * stride + 1 + left]
        bottom_right = pixels[8 * scale * stride + 1 + left + 4 * scale]
        digits.append({(255, 0): "2", (0, 255): "7"}[(top_left, bottom_right)])
    return "".join(digits)


def reply(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message.update(tool_calls=calls, reasoning_content="I read the digits.")
    return httpx.Response(200, json={"choices": [{"message": message}]})


def echo_call(value):
    return {"id": "native-probe-12", "type": "function",
            "function": {"name": "echo_probe", "arguments": json.dumps({"value": value})}}


def test_saved_model_vision_round_trip_reads_png_and_tool_result(settings, monkeypatch):
    monkeypatch.setattr("agent_harness.diagnostics.secrets.randbelow", lambda bound: 627272)
    model = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    requests = []

    def handle(request):
        body = json.loads(request.content)
        assert request.url.path == "/custom/completions"
        assert body["model"] == model and body["stream"] is False
        assert body["tool_choice"] == "auto" and body["tools"][0]["function"]["name"] == "echo_probe"
        messages = body["messages"]
        requests.append(messages)
        if len(requests) == 1:
            assert len(messages) == 1 and messages[0]["role"] == "user"
            text, image = messages[0]["content"]
            assert text["type"] == "text" and image["type"] == "image_url"
            digits = read_fixture_digits(image["image_url"]["url"])
            assert digits == "727272"
            assert digits not in json.dumps([text, body["tools"]])
            return reply(calls=[echo_call(digits)])
        assert len(messages) == 3 and messages[0] == requests[0][0]
        assert messages[1]["reasoning_content"] == "I read the digits."
        assert messages[1]["tool_calls"] == [echo_call("727272")]
        assert messages[2]["role"] == "tool" and messages[2]["tool_call_id"] == "native-probe-12"
        result = json.loads(messages[2]["content"])["result"]
        assert result and result not in json.dumps(messages[:2])
        return reply(result)

    app = create_app(settings.root, lambda cfg: ModelClient(cfg, httpx.MockTransport(handle)))
    app.state.settings.models = {"default": ModelConfig(provider="fake"),
        "selected": ModelConfig(endpoint="https://example.invalid/custom/completions",
                                endpoint_mode="exact", model=model)}
    app.state.settings.default_model = "default"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["x-harness-token"] = app.state.token
        response = client.post("/api/diagnostics/selected/vision", json={})
        assert response.status_code == 200 and response.json()["vision_tools"] == "passed"
        assert len(requests) == 2
        assert requests[0][0]["content"][1]["image_url"]["url"].split(",")[1] not in response.text
        assert client.get("/api/sessions").json() == []


@pytest.mark.parametrize("failure, expected", [
    ("text_instead_of_call", "not_observed"), ("wrong_image_value", "image_mismatch"),
    ("echo_image_value", "result_not_confirmed"), ("repeated_call", "result_not_confirmed"),
])
def test_vision_diagnostic_rejects_partial_round_trips(monkeypatch, failure, expected):
    monkeypatch.setattr("agent_harness.diagnostics.secrets.randbelow", lambda bound: 627272)
    requests = []

    def handle(request):
        messages = json.loads(request.content)["messages"]
        requests.append(messages)
        if len(requests) == 1:
            if failure == "text_instead_of_call":
                return reply(json.dumps(echo_call("727272")))
            return reply(calls=[echo_call("0" if failure == "wrong_image_value" else "727272")])
        if failure == "repeated_call":
            return reply(json.loads(messages[-1]["content"])["result"], [echo_call("727272")])
        return reply("727272")

    cfg = ModelConfig(endpoint="https://example.invalid/v1")
    result = asyncio.run(diagnose_vision(cfg, lambda c: ModelClient(c, httpx.MockTransport(handle))))
    assert result["vision_tools"] == expected
    assert len(requests) == (2 if expected == "result_not_confirmed" else 1)


@pytest.mark.parametrize("options,expected", [
    ({"provider": "fake"}, "demo_only"), ({"tool_mode": "json"}, "native_tools_required"),
    ({"tool_mode": "disabled"}, "native_tools_required"),
])
def test_vision_diagnostic_skips_unavailable_protocols(options, expected):
    cfg = ModelConfig(**options)
    result = asyncio.run(diagnose_vision(cfg, lambda _: pytest.fail("Unexpected model request")))
    assert result["vision_tools"] == expected


def test_rejected_image_request_does_not_echo_provider_body():
    def reject(request):
        return httpx.Response(415, content=request.content)

    cfg = ModelConfig(endpoint="https://example.invalid/v1")
    with pytest.raises(HarnessError) as error:
        asyncio.run(diagnose_vision(cfg, lambda c: ModelClient(c, httpx.MockTransport(reject))))
    assert error.value.details["http_status"] == 415
    assert "base64" not in json.dumps(error.value.as_dict())
