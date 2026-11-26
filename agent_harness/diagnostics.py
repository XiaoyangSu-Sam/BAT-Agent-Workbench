import base64
import json
import secrets
import struct
import zlib
from .runtime.model import ModelClient
from .runtime.context import json_action, wire_messages
from .errors import HarnessError

ECHO_PROBE = [{"type": "function", "function": {
    "name": "echo_probe", "description": "Echo a diagnostic string.",
    "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                   "required": ["value"], "additionalProperties": False}}}]

async def diagnose(config, model_factory=ModelClient):
    events = []
    async def emit(kind, data):
        events.append({"type":kind,"data":data})
    client = model_factory(config)
    if config.provider == "fake":
        return {"mode":"offline", "chat":"demo_only", "tools":"demo_only",
                "message":"Offline scripted provider. Real endpoint has not been tested.", "events":[]}
    greeting = await client.complete([{"role":"user","content":"Reply with OK."}], [], emit, tools=False)
    if not greeting.get("content") or greeting.get("tool_calls"):
        raise HarnessError("invalid_model_response", "The chat probe did not return a text answer.", "model")
    report = {"chat":"passed","tools":"not_tested","events":events}
    if config.tool_mode == "disabled":
        report["tools"] = "disabled"
        return report
    nonce = secrets.token_hex(8)
    schema = ECHO_PROBE
    prompt = ("Call echo_probe with value " + nonce
              + ". Then report the result field returned by the tool. Do not answer directly.")
    messages = [{"role":"user","content":prompt}]
    if config.tool_mode == "json":
        messages.insert(0, {"role":"system","content":'Return one JSON object: {"action":"tool","name":"echo_probe","arguments":{"value":"..."}}. After TOOL_RESULT, return {"action":"final","content":"the echoed value"}.'})
    first = await client.complete(messages, schema, emit)
    if config.tool_mode == "json":
        _, calls = json_action(first)
    else:
        calls = first.get("tool_calls", [])
    if len(calls) != 1:
        report["tools"] = "not_observed"
        report["message"] = "Chat works but exactly one tool call was not observed. Check provider tool parser or explicit JSON mode."
        return report
    call = calls[0]
    try:
        args = json.loads(call["function"]["arguments"])
    except ValueError:
        args = None
    if call["function"]["name"] != "echo_probe" or args != {"value":nonce}:
        report["tools"] = "invalid_arguments"
        return report
    # The second nonce appears only in the tool result, so an echo of the user's
    # prompt cannot make a broken round trip pass.
    result_nonce = secrets.token_hex(8)
    tool_result = json.dumps({"result": result_nonce})
    messages.append(first)
    if config.tool_mode == "json":
        messages.append({"role":"user","content":"TOOL_RESULT "+tool_result})
        outgoing = messages
    else:
        messages.append({"role":"tool","tool_call_id":call["id"],"content":tool_result})
        outgoing = wire_messages(messages, "native", continue_tool_reasoning=config.continues_tool_reasoning())
    second = await client.complete(outgoing, schema, emit)
    if config.tool_mode == "json":
        text, repeated_calls = json_action(second)
    else:
        text, repeated_calls = second.get("content"), second.get("tool_calls")
    report["tools"] = "passed" if not repeated_calls and result_nonce in (text or "") else "result_not_confirmed"
    report["message"] = "Synthetic echo probe only; no files or commands were executed."
    return report


_DIGITS = (
    "01110 10001 10001 10001 10001 10001 01110",
    "00100 01100 00100 00100 00100 00100 01110",
    "01110 10001 00001 00010 00100 01000 11111",
    "11110 00001 00001 01110 00001 00001 11110",
    "00010 00110 01010 10010 11111 00010 00010",
    "11111 10000 10000 11110 00001 00001 11110",
    "01110 10000 10000 11110 10001 10001 01110",
    "11111 00001 00010 00010 00100 01000 01000",
    "01110 10001 10001 01110 10001 10001 01110",
    "01110 10001 10001 01111 00001 00001 01110",
)


def _digit_png(digits):
    # Write a small grayscale PNG without a font, image library or temporary file.
    glyphs = [_DIGITS[int(digit)].split() for digit in digits]
    rows = ["00" + "0".join(row) + "00" for row in zip(*glyphs)]
    margin = ["0" * len(rows[0])] * 2
    rows = margin + rows + margin
    scale = 8
    pixels = bytearray()
    for row in rows:
        scanline = b"\0" + b"".join((b"\0" if bit == "1" else b"\xff") * scale for bit in row)
        pixels.extend(scanline * scale)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", len(rows[0]) * scale, len(rows) * scale, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


async def diagnose_vision(config, model_factory=ModelClient):
    if config.provider == "fake":
        return {"vision_tools": "demo_only", "message": "The offline demo cannot test image input."}
    if config.tool_mode != "native":
        return {"vision_tools": "native_tools_required",
                "message": "Select Native tool calling and save the connection to run this test."}

    digits = str(100000 + secrets.randbelow(900000))
    encoded = base64.b64encode(_digit_png(digits)).decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Read the six digits in this image and call echo_probe with those digits "
                                  "as the value string. Then reply with the result field returned by the tool."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
    ]}]

    async def discard_event(kind, data):
        pass

    client = model_factory(config)
    first = await client.complete(messages, ECHO_PROBE, discard_event)
    calls = first.get("tool_calls", [])
    if len(calls) != 1 or calls[0]["function"]["name"] != "echo_probe":
        return {"vision_tools": "not_observed", "message": "The model did not make the requested native tool call."}
    call = calls[0]
    if json.loads(call["function"]["arguments"]) != {"value": digits}:
        return {"vision_tools": "image_mismatch", "message": "The tool arguments did not match the digits in the image."}

    result = secrets.token_hex(8)
    messages.extend([first, {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"result": result})}])
    outgoing = wire_messages(messages, "native", continue_tool_reasoning=config.continues_tool_reasoning())
    second = await client.complete(outgoing, ECHO_PROBE, discard_event)
    if second.get("tool_calls") or result not in (second.get("content") or ""):
        return {"vision_tools": "result_not_confirmed",
                "message": "The tool received the correct digits, but the final answer did not confirm its result."}
    return {"vision_tools": "passed", "message": "The model read the image, called the tool and returned its result."}
