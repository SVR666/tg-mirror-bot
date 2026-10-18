"""Deep-link tests without Telegram, download daemons or bot startup."""

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ConnectError, ReadTimeout

ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://api.example.com/lookup?id="
MAGNET = (
    "magnet:?xt=urn:btih:"
    + "a1" * 20
    + "&dn=Test%20Episode&tr=udp%3A%2F%2Ftracker.example%3A80"
)


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper():
    return _load("test_deep_link_helper", "bot/helper/ext_utils/mirror_deep_link.py")


@pytest.fixture
def services(monkeypatch, helper):
    def stub(name, **attrs):
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    for name in (
        "bot",
        "bot.core",
        "bot.modules",
        "bot.helper",
        "bot.helper.ext_utils",
        "bot.helper.telegram_helper",
    ):
        stub(name).__path__ = []
    monkeypatch.setitem(sys.modules, "bot.helper.ext_utils.mirror_deep_link", helper)
    stub("bot.core.config_manager", Config=SimpleNamespace(AT_API_URL=API_URL))
    stub("bot.helper.ext_utils.bot_utils", new_task=lambda func: func)
    stub("bot.helper.telegram_helper.button_build", ButtonMaker=MagicMock())
    stub(
        "bot.helper.telegram_helper.message_utils",
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
        send_file=AsyncMock(),
    )
    stub(
        "bot.helper.telegram_helper.filters",
        CustomFilters=SimpleNamespace(authorized=AsyncMock(return_value=True)),
    )
    stub(
        "bot.helper.telegram_helper.bot_commands",
        BotCommands=SimpleNamespace(MirrorCommand=["mirror_test"], HelpCommand="help"),
    )
    stub("bot.modules.mirror_leech", mirror=AsyncMock())
    module = _load("bot.modules.services", "bot/modules/services.py")
    module.fetch_release_magnet = AsyncMock(return_value=MAGNET)
    return module


def _message(payload):
    return SimpleNamespace(
        text=f"/start {payload}",
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=42),
        id=123,
        entities=["original entity"],
        command=["start", payload],
    )


def test_deep_link_preserves_requester_and_full_magnet(services):
    message = _message("mirror_697866")
    client = object()
    asyncio.run(services.start(client, message))
    services.CustomFilters.authorized.assert_awaited_once_with(client, message)
    services.fetch_release_magnet.assert_awaited_once_with(API_URL, "697866")
    services.mirror.assert_awaited_once()
    actual_client, request = services.mirror.await_args.args
    assert actual_client is client
    assert request.from_user is message.from_user
    assert request.chat is message.chat
    assert request.id == message.id
    assert request.text == f"/mirror_test {MAGNET}"
    assert request.command == ["mirror_test", MAGNET]
    assert request.entities is None
    assert request is not message
    assert message.text == "/start mirror_697866"
    assert message.entities == ["original entity"]


def test_copied_message_retains_client_for_bound_replies(services):
    class BoundMessage:
        # Kurigram's serialization hooks omit _client; copy.copy uses these too.
        def __getstate__(self):
            return {key: value for key, value in self.__dict__.items() if key != "_client"}

        def __setstate__(self, state):
            self.__dict__ = state

        async def reply_text(self, text):
            return await self._client.send_message(self.chat.id, text)

    bound_client = SimpleNamespace(send_message=AsyncMock())
    message = BoundMessage()
    message.__dict__.update(vars(_message("mirror_697866")))
    message._client = bound_client

    async def mirror_with_reply(client, request):
        assert request._client is message._client
        await request.reply_text("Processing...")

    services.mirror.side_effect = mirror_with_reply
    asyncio.run(services.start(bound_client, message))

    bound_client.send_message.assert_awaited_once_with(42, "Processing...")
    assert message._client is bound_client
    assert message.text == "/start mirror_697866"


def test_unauthorized_deep_link_never_fetches_or_downloads(services):
    services.CustomFilters.authorized.return_value = False
    asyncio.run(services.start(object(), _message("mirror_697866")))
    services.fetch_release_magnet.assert_not_awaited()
    services.mirror.assert_not_awaited()
    assert "not authorized" in services.send_message.await_args.args[1]


@pytest.mark.parametrize(
    "payload",
    [
        "mirror_",
        "mirror_0",
        "mirror_-1",
        "mirror_01",
        "mirror_1.2",
        "mirror_" + "1" * 21,
        "mirror_" + "a" * 40,
        "mirror_697866 -up attacker:",
        "mirror_697866\n/mirror https://example.com",
        "mirror_697866\n",
        "mirror_https://example.com",
        "mirror_１２３",
    ],
)
def test_malformed_payload_rejected(services, payload):
    asyncio.run(services.start(object(), _message(payload)))
    services.fetch_release_magnet.assert_not_awaited()
    services.mirror.assert_not_awaited()
    assert services.send_message.await_args.args[1] == "Invalid mirror download link."


def test_lookup_error_is_reported_without_mirror(services):
    services.fetch_release_magnet.side_effect = services.MirrorLookupError(
        "Lookup failed"
    )
    asyncio.run(services.start(object(), _message("mirror_697866")))
    services.mirror.assert_not_awaited()
    assert services.send_message.await_args.args[1] == "Lookup failed"


def test_plain_start_keeps_existing_greeting(services):
    message = _message("")
    message.text = "/start"
    asyncio.run(services.start(object(), message))
    services.fetch_release_magnet.assert_not_awaited()
    services.mirror.assert_not_awaited()
    assert "list of available commands" in services.send_message.await_args.args[1]


def test_url_appends_only_release_id(helper):
    assert helper.release_lookup_url(f" {API_URL} ", "697866") == API_URL + "697866"


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "https://example.com/id",
        API_URL + "=",
        API_URL + "123",
        API_URL.removesuffix("="),
        API_URL + "#fragment",
        "file:///tmp/test?id",
        "https://u:p@example.com/?id",
    ],
)
def test_invalid_or_disabled_config_rejected(helper, prefix):
    with pytest.raises(helper.MirrorLookupError):
        helper.release_lookup_url(prefix, "697866")


def _mock_http(monkeypatch, helper, data=None, status=200, error=None):
    response = MagicMock()
    response.status_code = status
    response.json = MagicMock(return_value=data)
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    if isinstance(error, ValueError):
        response.json.side_effect = error
    elif error is not None:
        session.get.side_effect = error
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=context)
    monkeypatch.setattr(helper, "AsyncClient", factory)
    return factory, session, response


def test_fetch_returns_full_magnet_and_disables_redirects(helper, monkeypatch):
    factory, session, response = _mock_http(monkeypatch, helper, {"magnet_uri": MAGNET})
    assert asyncio.run(helper.fetch_release_magnet(API_URL, "697866")) == MAGNET
    session.get.assert_awaited_once_with(API_URL + "697866")
    factory.assert_called_once_with(timeout=20, follow_redirects=False)
    response.json.assert_called_once_with()


@pytest.mark.parametrize("status", [302, 404, 429, 500])
def test_http_failures_rejected(helper, monkeypatch, status):
    _, _, response = _mock_http(monkeypatch, helper, status=status)
    with pytest.raises(helper.MirrorLookupError, match="Could not fetch"):
        asyncio.run(helper.fetch_release_magnet(API_URL, "697866"))
    response.json.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [ValueError("bad JSON"), ReadTimeout("timeout"), ConnectError("network failure")],
)
def test_json_and_timeout_failures(helper, monkeypatch, error):
    _mock_http(monkeypatch, helper, error=error)
    with pytest.raises(helper.MirrorLookupError, match="Could not fetch"):
        asyncio.run(helper.fetch_release_magnet(API_URL, "697866"))


@pytest.mark.parametrize(
    "data",
    [
        [],
        None,
        {},
        {"error": "not found"},
        {"magnet_uri": None},
        {"magnet_uri": "https://example.com"},
        {"magnet_uri": "magnet:?xt=urn:btih:bad"},
        {"magnet_uri": MAGNET + " -up attacker:"},
        {"magnet_uri": MAGNET + "\n"},
    ],
)
def test_invalid_magnet_responses_rejected(helper, monkeypatch, data):
    _mock_http(monkeypatch, helper, data=data)
    with pytest.raises(helper.MirrorLookupError, match="valid magnet"):
        asyncio.run(helper.fetch_release_magnet(API_URL, "697866"))


def test_start_handler_supports_unsuffixed_telegram_deep_links():
    tree = ast.parse((ROOT / "bot/core/handlers.py").read_text(encoding="utf-8"))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "MessageHandler"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "start"
    )
    filters = next(item.value for item in handler.keywords if item.arg == "filters")
    expression = compile(ast.Expression(filters), "handlers.py", "eval")
    registered = eval(
        expression,
        {
            "BotCommands": SimpleNamespace(StartCommand="start_test"),
            "command": lambda names, **kwargs: names,
        },
    )
    assert registered == ["start", "start_test"]
