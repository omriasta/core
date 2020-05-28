"""Support for views."""
import asyncio
import json
import logging
from typing import Any, Callable, List, Optional

from aiohttp import web
from aiohttp.typedefs import LooseHeaders
from aiohttp.web_exceptions import (
    HTTPBadRequest,
    HTTPInternalServerError,
    HTTPUnauthorized,
)
import voluptuous as vol

from homeassistant import exceptions
from homeassistant.const import CONTENT_TYPE_JSON, HTTP_OK
from homeassistant.core import Context, is_callback
from homeassistant.helpers.json import JSONEncoder

from .const import KEY_AUTHENTICATED, KEY_HASS, KEY_REAL_IP

_LOGGER = logging.getLogger(__name__)


class HomeAssistantView:
    """Base view for all views."""

    url: Optional[str] = None
    extra_urls: List[str] = []
    # Views inheriting from this class can override this
    requires_auth = True
    cors_allowed = False

    @staticmethod
    def context(request: web.Request) -> Context:
        """Generate a context from a request."""
        user = request.get("hass_user")
        if user is None:
            return Context()

        return Context(user_id=user.id)

    @staticmethod
    def json(
        result: Any, status_code: int = HTTP_OK, headers: Optional[LooseHeaders] = None,
    ) -> web.Response:
        """Return a JSON response."""
        try:
            msg = json.dumps(
                result, sort_keys=True, cls=JSONEncoder, allow_nan=False
            ).encode("UTF-8")
        except (ValueError, TypeError) as err:
            _LOGGER.error("Unable to serialize to JSON: %s\n%s", err, result)
            raise HTTPInternalServerError
        response = web.Response(
            body=msg,
            content_type=CONTENT_TYPE_JSON,
            status=status_code,
            headers=headers,
        )
        response.enable_compression()
        return response

    def json_message(
        self,
        message: str,
        status_code: int = HTTP_OK,
        message_code: Optional[str] = None,
        headers: Optional[LooseHeaders] = None,
    ) -> web.Response:
        """Return a JSON message response."""
        data = {"message": message}
        if message_code is not None:
            data["code"] = message_code
        return self.json(data, status_code, headers=headers)

    def register(self, app: web.Application, router: web.UrlDispatcher) -> None:
        """Register the view with a router."""
        assert self.url is not None, "No url set for view"
        urls = [self.url] + self.extra_urls
        routes = []

        for method in ("get", "post", "delete", "put", "patch", "head", "options"):
            handler = getattr(self, method, None)

            if not handler:
                continue

            handler = request_handler_factory(self, handler)

            for url in urls:
                routes.append(router.add_route(method, url, handler))

        if not self.cors_allowed:
            return

        for route in routes:
            app["allow_cors"](route)


def request_handler_factory(view: HomeAssistantView, handler: Callable) -> Callable:
    """Wrap the handler classes."""
    assert asyncio.iscoroutinefunction(handler) or is_callback(
        handler
    ), "Handler should be a coroutine or a callback."

    async def handle(request: web.Request) -> web.StreamResponse:
        """Handle incoming request."""
        if not request.app[KEY_HASS].is_running:
            return web.Response(status=503)

        authenticated = request.get(KEY_AUTHENTICATED, False)

        if view.requires_auth and not authenticated:
            raise HTTPUnauthorized()

        _LOGGER.debug(
            "Serving %s to %s (auth: %s)",
            request.path,
            request.get(KEY_REAL_IP),
            authenticated,
        )

        try:
            result = handler(request, **request.match_info)

            if asyncio.iscoroutine(result):
                result = await result
        except vol.Invalid:
            raise HTTPBadRequest()
        except exceptions.ServiceNotFound:
            raise HTTPInternalServerError()
        except exceptions.Unauthorized:
            raise HTTPUnauthorized()

        if isinstance(result, web.StreamResponse):
            # The method handler returned a ready-made Response, how nice of it
            return result

        status_code = HTTP_OK

        if isinstance(result, tuple):
            result, status_code = result

        if isinstance(result, bytes):
            bresult = result
        elif isinstance(result, str):
            bresult = result.encode("utf-8")
        elif result is None:
            bresult = b""
        else:
            assert (
                False
            ), f"Result should be None, string, bytes or Response. Got: {result}"

        return web.Response(body=bresult, status=status_code)

    return handle
