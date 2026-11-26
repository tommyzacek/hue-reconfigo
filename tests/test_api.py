from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hue_reconfigo.api import BridgeClient, BridgeError


def client(payload, status=200, headers=None):
    session = MagicMock()
    response = MagicMock(status=status, headers=headers or {})
    response.json = AsyncMock(return_value=payload)
    session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    return BridgeClient(session, lambda: ("bridge.invalid", "test-only-key"))


async def test_one_authenticated_batch_request(resource):
    api = client({"data": [resource(), resource(resource_id="dial-b")], "errors": []})
    assert len(await api.rotary_resources()) == 2
    api.session.get.assert_called_once()
    args, kwargs = api.session.get.call_args
    assert args[0].endswith("/resource/relative_rotary")
    assert kwargs["headers"] == {"hue-application-key": "test-only-key"}
    assert kwargs["timeout"].total == 3
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"data": "wrong"},
        {"data": [None]},
        {"data": [], "errors": [{"description": "denied"}]},
    ],
)
async def test_invalid_or_error_response(payload):
    with pytest.raises(BridgeError):
        await client(payload).rotary_resources()


async def test_rate_limit_respects_retry_after():
    with pytest.raises(BridgeError) as caught:
        await client({}, status=429, headers={"Retry-After": "60"}).rotary_resources()
    assert caught.value.retry_after == 60


async def test_button_resources_are_one_authenticated_discovery_read():
    api = client({"data": [{"id": "button-a"}], "errors": []})
    assert await api.button_resources() == [{"id": "button-a"}]
    args, kwargs = api.session.get.call_args
    assert args[0].endswith("/resource/button")
    assert kwargs["headers"]["hue-application-key"] == "test-only-key"


async def test_native_brightness_transition_and_stop_use_hue_v2_resources():
    api = client({"data": [], "errors": []})
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={"data": [], "errors": []})
    api.session.put.return_value.__aenter__ = AsyncMock(return_value=response)
    api.session.put.return_value.__aexit__ = AsyncMock(return_value=False)

    await api.brightness_transition("grouped_light", "group-id", 100, 8000)
    await api.stop_dimming("grouped_light", "group-id")

    calls = api.session.put.call_args_list
    assert calls[0].args[0].endswith("/resource/grouped_light/group-id")
    assert calls[0].kwargs["json"] == {
        "on": {"on": True},
        "dimming": {"brightness": 100},
        "dynamics": {"duration": 8000},
    }
    assert calls[1].kwargs["json"] == {"dimming_delta": {"action": "stop"}}
    assert all(call.kwargs["headers"] == {"hue-application-key": "test-only-key"} for call in calls)
    assert all(call.kwargs["allow_redirects"] is False for call in calls)


async def test_bridge_current_brightness_comes_from_resource_readback():
    api = client(
        {
            "data": [
                {"on": {"on": True}, "dimming": {"brightness": 54.15}},
            ],
            "errors": [],
        }
    )
    response = api.session.get.return_value.__aenter__.return_value
    response.status = 200

    assert await api.read_brightness("grouped_light", "group-id") == 54.15
    args, kwargs = api.session.get.call_args
    assert args[0].endswith("/resource/grouped_light/group-id")
    assert kwargs["headers"] == {"hue-application-key": "test-only-key"}


async def test_native_brightness_delta_requests_only_one_smooth_step():
    api = client({"data": [], "errors": []})
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={"data": [], "errors": []})
    api.session.put.return_value.__aenter__ = AsyncMock(return_value=response)
    api.session.put.return_value.__aexit__ = AsyncMock(return_value=False)

    await api.brightness_delta("grouped_light", "group-id", 5, 800)
    assert api.session.put.call_args.kwargs["json"] == {
        "dimming_delta": {"action": "up", "brightness_delta": 5},
        "dynamics": {"duration": 800},
        "on": {"on": True},
    }


@pytest.mark.parametrize("payload", [None, {"data": [None]}, {"data": [], "errors": [{}]}])
async def test_invalid_button_discovery_is_recoverable(payload):
    with pytest.raises(BridgeError):
        await client(payload).button_resources()


async def test_sse_frame_boundaries_keep_individual_reports():
    api = client({})
    response = await api.session.get.return_value.__aenter__()

    async def lines():
        yield b": heartbeat\n"
        yield b"\n"
        yield b'data: [{"type":"update",\n'
        yield b'data: "data":[{"id":"a"},{"id":"b"}]}]\n'
        yield b"\n"

    response.content.__aiter__.side_effect = lines
    stream = api.button_events()
    assert await anext(stream) == [{"type": "update", "data": [{"id": "a"}, {"id": "b"}]}]
    await stream.aclose()
    args, kwargs = api.session.get.call_args
    assert args[0].endswith("/eventstream/clip/v2")
    assert kwargs["headers"]["Accept"] == "text/event-stream"
    assert "Last-Event-ID" not in kwargs["headers"]
    assert kwargs["timeout"].total is None
    assert kwargs["allow_redirects"] is False


async def test_sse_eof_causes_reconnect_instead_of_busy_loop():
    api = client({})
    response = await api.session.get.return_value.__aenter__()

    async def lines():
        if False:
            yield b""

    response.content.__aiter__.side_effect = lines
    with pytest.raises(BridgeError, match="closed"):
        await anext(api.button_events())


async def test_device_resources_are_discovered_once_without_button_poll_traffic():
    api = client({"data": [{"id": "remote", "product_data": {"model_id": "RWL022"}}]})
    assert (await api.device_resources())[0]["id"] == "remote"
    assert api.session.get.call_args.args[0].endswith("/resource/device")


@pytest.mark.parametrize("data", [None, "invalid", [None]])
async def test_device_discovery_rejects_invalid_resource_lists(data):
    with pytest.raises(BridgeError):
        await client({"data": data}).device_resources()


@pytest.mark.parametrize(
    "method",
    [
        "rotary_resources",
        "device_resources",
        "button_resources",
        "read_brightness",
        "brightness_transition",
        "button_events",
    ],
)
@pytest.mark.parametrize("status", [401, 403])
async def test_revoked_own_key_is_an_auth_failure_even_without_json(method, status):
    from custom_components.hue_reconfigo.api import BridgeAuthenticationError

    api = client({}, status=status)
    reply = api.session.get.return_value.__aenter__.return_value
    reply.json.side_effect = ValueError("non-JSON rejection")
    api.session.put.return_value.__aenter__ = AsyncMock(return_value=reply)
    api.session.put.return_value.__aexit__ = AsyncMock(return_value=False)
    with pytest.raises(BridgeAuthenticationError):
        if method == "button_events":
            await anext(api.button_events())
        elif method == "read_brightness":
            await api.read_brightness("light", "light-id")
        elif method == "brightness_transition":
            await api.brightness_transition("light", "light-id", 50, 500)
        else:
            await getattr(api, method)()
    reply.json.assert_not_awaited()
    http = api.session.put if method == "brightness_transition" else api.session.get
    assert http.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "payload",
    [[{"error": {"type": 1}}], {"errors": [{"type": 1, "description": "secret server text"}]}],
)
async def test_unauthorized_body_surfaces_as_auth_failure_without_server_text(payload):
    from custom_components.hue_reconfigo.api import BridgeAuthenticationError

    with pytest.raises(BridgeAuthenticationError) as exc:
        await client(payload).rotary_resources()
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("status", [401, 403])
async def test_motion_requests_surface_auth_failure_before_decoding(status):
    from custom_components.hue_reconfigo.api import BridgeAuthenticationError
    from custom_components.hue_reconfigo.motion_bridge import request

    api = client({}, status=status)
    reply = api.session.get.return_value.__aenter__.return_value
    reply.json.side_effect = ValueError("non-JSON rejection")
    api.session.put.return_value.__aenter__ = AsyncMock(return_value=reply)
    with pytest.raises(BridgeAuthenticationError):
        await request(api, "motion", "motion-id")
    with pytest.raises(BridgeAuthenticationError):
        await request(api, "motion", "motion-id", {"sensitivity": {"sensitivity": 1}})
    reply.json.assert_not_awaited()
    assert api.session.get.call_args.kwargs["allow_redirects"] is False
    assert api.session.put.call_args.kwargs["allow_redirects"] is False


async def test_motion_unauthorized_body_surfaces_as_auth_failure():
    from custom_components.hue_reconfigo.api import BridgeAuthenticationError
    from custom_components.hue_reconfigo.motion_bridge import request

    with pytest.raises(BridgeAuthenticationError):
        await request(client({"errors": [{"type": 1}]}), "motion", "motion-id")
