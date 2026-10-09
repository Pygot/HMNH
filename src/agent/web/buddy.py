# src/agent/web/buddy.py
from agent.web.common import (
    json_errors,
    protected_header,
    read_preferences,
    requires,
    save_preferences,
    voice_ready,
)
from starlette.exceptions import HTTPException
from agent.permissions import Permission
from agent.web.context import context_of
from starlette.responses import Response
from agent.web.voice import speak_text
from starlette.requests import Request
from agent.web.prefs import BuddyMode

import json


@requires(Permission.TIE_USE)
@json_errors
async def choose_buddy(request: Request) -> Response:
    """Save the buddy mode that the user picked.

    The request needs a valid CSRF header and a JSON body with a mode field. The
    choice is stored in the preferences cookie. Voice mode is only accepted when voice
    is configured.

    Args:
        request: Incoming POST request with a JSON body such as {"mode": "voice"}.

    Returns:
        An empty 204 response that carries the updated preferences cookie.

    Raises:
        HTTPException: with status 400 when the mode is missing or unknown, or when
            voice is chosen but not available.
    """
    ctx = protected_header(request, request.state.session)
    try:
        mode = BuddyMode(json.loads(await request.body()).get("mode", ""))
    except (ValueError, AttributeError) as error:
        raise HTTPException(400) from error
    if mode is BuddyMode.VOICE and not voice_ready(ctx):
        raise HTTPException(400)
    preferences, _ = read_preferences(request)
    response = Response(status_code=204)
    save_preferences(response, ctx.web, preferences.model_copy(update={"buddy": mode}))
    return response


@requires(Permission.VOICE_USE)
@json_errors
async def buddy_line(request: Request) -> Response:
    """Return the spoken audio of one of the fixed buddy lines.

    Only lines listed in the settings can be requested, and the text is taken from
    the settings rather than from the request. Speaking counts against the session's
    voice allowance.

    Args:
        request: Incoming GET request whose path names the line.

    Returns:
        An audio/mpeg response with the speech.

    Raises:
        HTTPException: with status 404 when the line is not a known voice line.
    """
    ctx = context_of(request)
    tuning = ctx.settings.buddy
    line = request.path_params["line"]
    # Only whitelisted line names are accepted and the spoken text comes from settings, so a caller
    # cannot make the voice service read arbitrary text.
    if line not in tuning.voice_lines:
        raise HTTPException(404)
    audio = await speak_text(ctx, tuning.lines[line], request.state.session.id)
    return Response(audio, media_type="audio/mpeg")
