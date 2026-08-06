"""The wire protocol foundation: envelope, framing, negotiation, errors.

Four properties are load-bearing and every test here defends one of them:

1. **One event model.** An ``ev`` frame's ``d`` is the *activity* envelope from
   ``mantis_agent.activity.events`` — decoding an event yields a ``NodeStatus``,
   not a dict and not a parallel struct.
2. **Malformed input never reaches the caller as an exception.** A daemon reads
   from a socket somebody else controls; a bad byte must produce a recorded
   error, not a traceback that kills the read loop.
3. **Version mismatch refuses.** Never a silent downgrade, and the refusal
   carries the supported range so the client can say something useful.
4. **Capabilities gate operations.** An op outside the negotiated set is refused
   before it is attempted.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import msgspec
import pytest

from mantis_agent.activity.events import NodeAction, NodeCreated, NodeStatus
from mantis_agent.errors import AgentError
from mantis_agent.protocol import (
    ERROR_CODES,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOLS,
    CapabilityNotNegotiatedError,
    ErrorFrame,
    Event,
    FrameReader,
    Hello,
    LeaseHeldError,
    MalformedFrameError,
    MessageTooLargeError,
    ProtocolError,
    ReplayTruncatedError,
    Request,
    Response,
    UnknownOperationError,
    VersionMismatchError,
    Welcome,
    check_operation,
    decode_frame,
    encode_frame,
    encode_line,
    error_frame,
    error_from_frame,
    negotiate,
    parse_hello,
    require_capability,
    response_to,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _roundtrip(frame):
    """Encode one frame, read it back through the incremental reader."""

    reader = FrameReader()
    out = reader.feed(encode_line(frame))
    assert reader.drain_errors() == []
    assert len(out) == 1
    return out[0]


# --------------------------------------------------------------------------
# envelope round trips
# --------------------------------------------------------------------------


def test_request_round_trip() -> None:
    req = Request(op="session.subscribe", id="c7", d={"session_id": "s1", "from_seq": 12})
    back = _roundtrip(req)
    assert isinstance(back, Request)
    assert back == req
    assert back.v == PROTOCOL_VERSION


def test_response_round_trip() -> None:
    res = Response(id="c7", d={"session_count": 3})
    back = _roundtrip(res)
    assert isinstance(back, Response)
    assert back.ok is True
    assert back.d == {"session_count": 3}


def test_event_round_trip_carries_the_activity_struct() -> None:
    ev = Event.from_activity(
        NodeStatus(seq=4821, ts=1.5, node_id="job:3", status="running"),
        session_id="s1",
    )
    assert ev.seq == 4821  # lifted from the payload, never restated by hand
    back = _roundtrip(ev)
    assert isinstance(back, Event)
    assert isinstance(back.d, NodeStatus)  # not a dict, not a second model
    assert back.d.node_id == "job:3"
    assert back.session_id == "s1"


def test_event_wire_shape_is_the_activity_encoding() -> None:
    ev = Event.from_activity(NodeAction(seq=7, ts=0.0, node_id="wf:1", action="stop", actor="user"))
    body = json.loads(encode_frame(ev))
    assert body["t"] == "ev"
    assert body["seq"] == 7
    assert body["d"]["type"] == "node_action"  # the activity tag, untouched
    assert body["d"]["actor"] == "user"


def test_every_activity_variant_survives_an_event_frame() -> None:
    payloads = [
        NodeCreated(seq=1, ts=0.0, node_id="job:1", parent_id=None, kind="job", title="t"),
        NodeStatus(seq=2, ts=0.0, node_id="job:1", status="done"),
        NodeAction(seq=3, ts=0.0, node_id="job:1", action="stop", actor="user"),
    ]
    for payload in payloads:
        back = _roundtrip(Event.from_activity(payload))
        assert back.d == payload


def test_error_frame_round_trip() -> None:
    frame = ErrorFrame(code="not_authorized", msg="nope", id="c7", d={"device": "phone"})
    back = _roundtrip(frame)
    assert isinstance(back, ErrorFrame)
    assert back.code == "not_authorized"
    assert back.d == {"device": "phone"}


def test_spec_wire_examples_decode() -> None:
    """The four lines printed in the plan's §6 must decode as written."""

    lines = [
        b'{"v": 1, "id": "c7", "t": "req", "op": "session.subscribe", "d": {}}',
        b'{"v": 1, "id": "c7", "t": "res", "ok": true, "d": {}}',
        b'{"v": 1, "t": "ev", "seq": 4821, "d": {"type": "node_status", "seq": 4821,'
        b' "ts": 1.0, "node_id": "job:3", "status": "running"}}',
        b'{"v": 1, "id": "c7", "t": "err", "code": "not_authorized", "msg": "..."}',
    ]
    reader = FrameReader()
    frames = reader.feed(b"\n".join(lines) + b"\n")
    assert reader.drain_errors() == []
    assert [type(f) for f in frames] == [Request, Response, Event, ErrorFrame]


def test_response_to_and_error_frame_echo_the_request_id() -> None:
    req = Request(op="ping", id="42")
    assert response_to(req, {"pong": True}).id == "42"
    frame = error_frame(LeaseHeldError(holder="workstation", expires_at=99.0), request_id=req.id)
    assert frame.id == "42"
    assert frame.code == "lease_held"
    assert frame.d == {"holder": "workstation", "expires_at": 99.0}


# --------------------------------------------------------------------------
# framing: malformed input is contained
# --------------------------------------------------------------------------


MALFORMED = [
    pytest.param(b"{", id="truncated-json"),
    pytest.param(b"not json at all", id="not-json"),
    pytest.param(b"[1, 2, 3]", id="json-array"),
    pytest.param(b'"a string"', id="json-string"),
    pytest.param(b"null", id="json-null"),
    pytest.param(b"17", id="json-number"),
    pytest.param(b'{"v": 1, "id": "c7"}', id="no-tag"),
    pytest.param(b'{"v": 1, "t": "nope"}', id="unknown-tag"),
    pytest.param(b'{"v": 1, "t": 7}', id="non-string-tag"),
    pytest.param(b'{"v": "one", "t": "req", "op": "ping"}', id="wrong-field-type"),
    pytest.param(b'{"v": 1, "t": "req"}', id="req-missing-op"),
    pytest.param(b'{"v": 1, "t": "err", "msg": "x"}', id="err-missing-code"),
    pytest.param(b'{"v": 1, "t": "ev", "seq": 1, "d": {"type": "nope"}}', id="bad-activity-tag"),
    pytest.param(b'{"v": 1, "t": "ev", "seq": 1, "d": {}}', id="untagged-activity"),
    pytest.param(b'{"v": 1, "t": "req", "op": "ping", "d": []}', id="d-not-object"),
    pytest.param(b"\xff\xfe\x00", id="invalid-utf8"),
]


@pytest.mark.parametrize("line", MALFORMED)
def test_malformed_frames_never_raise_into_the_caller(line: bytes) -> None:
    reader = FrameReader()
    frames = reader.feed(line + b"\n")
    assert frames == []
    errs = reader.drain_errors()
    assert len(errs) == 1
    assert isinstance(errs[0], MalformedFrameError)
    assert errs[0].code == "malformed_frame"
    assert reader.drain_errors() == []  # drained, not repeated


def test_reader_resynchronizes_after_a_bad_line() -> None:
    good = encode_line(Request(op="ping", id="1"))
    reader = FrameReader()
    frames = reader.feed(good + b"garbage{\n" + good)
    assert len(frames) == 2
    assert len(reader.drain_errors()) == 1


def test_blank_and_crlf_lines_are_tolerated() -> None:
    reader = FrameReader()
    payload = encode_frame(Request(op="ping", id="1"))
    frames = reader.feed(b"\n  \n" + payload + b"\r\n\n")
    assert len(frames) == 1
    assert reader.drain_errors() == []


def test_frame_split_across_chunks_is_reassembled() -> None:
    blob = encode_line(Request(op="session.prompt", id="9", d={"text": "hello"}))
    reader = FrameReader()
    seen = []
    for i in range(0, len(blob), 7):
        seen.extend(reader.feed(blob[i : i + 7]))
    assert len(seen) == 1
    assert seen[0].d == {"text": "hello"}
    assert reader.drain_errors() == []


def test_truncated_final_line_is_discarded() -> None:
    reader = FrameReader()
    frames = reader.feed(encode_line(Request(op="ping", id="1")) + b'{"v":1,"t":"re')
    assert len(frames) == 1
    assert reader.pending == 14  # still buffered, not yet an error
    assert reader.drain_errors() == []
    assert reader.close() == 14  # dropped on close, silently
    assert reader.pending == 0
    assert reader.drain_errors() == []


def test_oversized_line_is_refused_and_the_reader_recovers() -> None:
    reader = FrameReader(max_message_bytes=64)
    huge = b'{"v":1,"t":"req","op":"' + b"x" * 200 + b'"}\n'
    frames = reader.feed(huge + encode_line(Request(op="ping", id="1")))
    assert len(frames) == 1  # the good line after the bomb still arrives
    errs = reader.drain_errors()
    assert len(errs) == 1
    assert isinstance(errs[0], MessageTooLargeError)
    assert errs[0].data["limit"] == 64


def test_unterminated_oversized_buffer_is_dropped_not_grown() -> None:
    reader = FrameReader(max_message_bytes=64)
    assert reader.feed(b"x" * 500) == []  # no newline: a memory bomb
    assert reader.pending == 0
    assert isinstance(reader.drain_errors()[0], MessageTooLargeError)
    # the tail of the bomb is skipped up to the next newline, then normal service
    frames = reader.feed(b"junk-tail\n" + encode_line(Request(op="ping", id="1")))
    assert len(frames) == 1
    assert reader.drain_errors() == []


def test_default_message_limit_matches_the_configured_maximum() -> None:
    assert MAX_MESSAGE_BYTES == 1048576
    assert FrameReader().max_message_bytes == MAX_MESSAGE_BYTES


def test_decode_frame_raises_a_protocol_error_not_a_msgspec_error() -> None:
    with pytest.raises(MalformedFrameError):
        decode_frame(b'{"t": "nope"}')
    with pytest.raises(MalformedFrameError):
        decode_frame("")


# --------------------------------------------------------------------------
# version negotiation
# --------------------------------------------------------------------------


def test_handshake_example_negotiates() -> None:
    hello = parse_hello(
        {
            "client": "mantis-mobile/0.3",
            "protocol": [1],
            "caps": ["control", "stream", "replay"],
        }
    )
    neg, welcome = negotiate(hello, server="mantisd/2.62.0", session_count=3)
    assert isinstance(welcome, Welcome)
    assert neg.version == 1
    assert welcome.protocol == 1
    assert welcome.server == "mantisd/2.62.0"
    assert welcome.session_count == 3
    assert welcome.auth == "required"
    assert set(welcome.caps) == {"control", "stream", "replay"}


def test_negotiate_picks_the_highest_shared_version() -> None:
    neg, _ = negotiate(Hello(client="x", protocol=(1, 2, 3)))
    assert neg.version == max(SUPPORTED_PROTOCOLS)


def test_version_too_low_is_refused_with_the_supported_range() -> None:
    with pytest.raises(VersionMismatchError) as exc:
        negotiate(Hello(client="x", protocol=(0,)))
    assert exc.value.code == "version_mismatch"
    assert exc.value.data["supported"] == list(SUPPORTED_PROTOCOLS)
    assert exc.value.data["requested"] == [0]
    assert exc.value.data["min"] == min(SUPPORTED_PROTOCOLS)
    assert exc.value.data["max"] == max(SUPPORTED_PROTOCOLS)


def test_version_too_high_is_refused_never_downgraded() -> None:
    with pytest.raises(VersionMismatchError) as exc:
        negotiate(Hello(client="x", protocol=(99,)))
    assert exc.value.data["requested"] == [99]


def test_missing_version_is_refused() -> None:
    with pytest.raises(VersionMismatchError):
        negotiate(parse_hello({"client": "x", "caps": ["stream"]}))


def test_malformed_hello_payload_is_a_protocol_error() -> None:
    with pytest.raises(MalformedFrameError):
        parse_hello({"client": "x", "protocol": "one"})
    with pytest.raises(MalformedFrameError):
        parse_hello({"protocol": [1], "caps": "stream"})


# --------------------------------------------------------------------------
# capability negotiation
# --------------------------------------------------------------------------


def test_capabilities_are_the_intersection() -> None:
    neg, welcome = negotiate(
        Hello(client="x", protocol=(1,), caps=("stream", "replay", "wormhole")),
        capabilities=("stream", "control", "replay"),
    )
    assert neg.caps == frozenset({"stream", "replay"})
    assert "wormhole" not in welcome.caps  # invented caps are dropped, not granted
    assert welcome.caps == ("replay", "stream")  # sorted, so the wire is stable


def test_client_asking_for_nothing_gets_nothing() -> None:
    neg, _ = negotiate(Hello(client="x", protocol=(1,)))
    assert neg.caps == frozenset()


def test_operation_outside_the_negotiated_set_is_refused() -> None:
    neg, _ = negotiate(Hello(client="x", protocol=(1,), caps=("stream",)))
    check_operation(neg, "session.subscribe")  # granted
    check_operation(neg, "ping")  # free, needs no capability
    with pytest.raises(CapabilityNotNegotiatedError) as exc:
        check_operation(neg, "session.prompt")
    assert exc.value.code == "capability_not_negotiated"
    assert exc.value.data["op"] == "session.prompt"
    assert exc.value.data["capability"] == "control"
    assert exc.value.data["negotiated"] == ["stream"]


def test_replay_is_an_argument_level_check_on_top_of_stream() -> None:
    """``session.subscribe`` needs ``stream``; the same op *with* ``from_seq``
    also needs ``replay`` — the check the daemon performs on the arguments."""

    neg, _ = negotiate(Hello(client="x", protocol=(1,), caps=("stream",)))
    check_operation(neg, "session.subscribe")
    with pytest.raises(CapabilityNotNegotiatedError) as exc:
        require_capability(neg, "replay", "session.subscribe")
    assert exc.value.data == {
        "op": "session.subscribe",
        "capability": "replay",
        "negotiated": ["stream"],
    }


def test_permission_response_needs_its_own_capability() -> None:
    watcher, _ = negotiate(Hello(client="x", protocol=(1,), caps=("stream",)))
    phone, _ = negotiate(Hello(client="x", protocol=(1,), caps=("stream", "respond_permission")))
    assert not watcher.allows("permission.respond")
    assert phone.allows("permission.respond")


def test_unknown_operation_is_refused_before_capability_lookup() -> None:
    neg, _ = negotiate(Hello(client="x", protocol=(1,), caps=("control",)))
    with pytest.raises(UnknownOperationError) as exc:
        check_operation(neg, "session.selfdestruct")
    assert exc.value.data["op"] == "session.selfdestruct"


def test_free_operations_work_with_no_capabilities_at_all() -> None:
    neg, _ = negotiate(Hello(client="x", protocol=(1,)))
    for op in ("hello", "ping", "caps"):
        check_operation(neg, op)


# --------------------------------------------------------------------------
# error taxonomy
# --------------------------------------------------------------------------


def test_error_codes_are_unique_and_stable() -> None:
    assert ERROR_CODES["replay_truncated"] is ReplayTruncatedError
    assert len({cls.code for cls in ERROR_CODES.values()}) == len(ERROR_CODES)
    for code, cls in ERROR_CODES.items():
        assert cls.code == code
        assert code.islower()


def test_taxonomy_covers_the_specified_errors() -> None:
    expected = {
        "version_mismatch",
        "unknown_operation",
        "capability_not_negotiated",
        "not_authenticated",
        "not_authorized",
        "pairing_code_invalid",
        "pairing_rate_limited",
        "certificate_pin_mismatch",
        "lease_held",
        "lease_expired",
        "session_not_found",
        "session_not_live",
        "replay_truncated",
        "slow_consumer",
        "message_too_large",
        "teleport_incompatible",
        "teleport_in_flight",
        "teleport_integrity",
        "teleport_sealed",
    }
    assert expected <= set(ERROR_CODES)


def test_duplicate_error_code_fails_at_class_definition() -> None:
    """A second class claiming ``lease_held`` would silently mis-route every
    client that branches on the code, so it is a hard failure at import."""

    before = dict(ERROR_CODES)
    with pytest.raises(RuntimeError, match="duplicate protocol error code"):

        class _Clashing(ProtocolError):
            code = "lease_held"

    assert ERROR_CODES == before  # the registry is not polluted by the attempt


def test_error_frame_copies_the_recovery_data() -> None:
    exc = LeaseHeldError(holder="phone", expires_at=1.0)
    frame = error_frame(exc)
    frame.d["holder"] = "somebody-else"
    assert exc.data["holder"] == "phone"  # the frame owns its own dict
    assert frame.id == ""  # unsolicited errors correlate to nothing


def test_protocol_errors_are_agent_errors() -> None:
    assert issubclass(ProtocolError, AgentError)
    assert isinstance(ReplayTruncatedError(requested_seq=5, earliest_seq=90), ProtocolError)


def test_recovery_data_survives_the_wire() -> None:
    exc = ReplayTruncatedError(requested_seq=5, earliest_seq=90)
    frame = error_frame(exc, request_id="c7")
    back = error_from_frame(decode_frame(encode_frame(frame)))
    assert isinstance(back, ReplayTruncatedError)
    assert back.data == {"requested_seq": 5, "earliest_seq": 90}
    assert back.msg == exc.msg


def test_unknown_error_code_degrades_to_the_base_class() -> None:
    back = error_from_frame(ErrorFrame(code="from_the_future", msg="?", d={"x": 1}))
    assert type(back) is ProtocolError
    assert back.code == "from_the_future"  # instance shadows the class default
    assert back.data == {"x": 1}


def test_error_frame_of_a_plain_exception_is_an_internal_error() -> None:
    frame = error_frame(ValueError("boom"), request_id="1")
    assert frame.code == "internal_error"
    assert "boom" not in frame.msg  # never leak an arbitrary exception's text


# --------------------------------------------------------------------------
# portability
# --------------------------------------------------------------------------


def test_protocol_package_parses_as_python_39() -> None:
    pkg = Path(__file__).resolve().parents[1] / "mantis_agent" / "protocol"
    for path in sorted(pkg.glob("*.py")):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))


def test_structs_are_frozen() -> None:
    with pytest.raises((AttributeError, TypeError, msgspec.ValidationError)):
        Request(op="ping").op = "other"  # type: ignore[misc]
