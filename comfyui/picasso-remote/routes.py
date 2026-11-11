# Picasso remote downloads for ComfyUI.
# Part of Picasso (https://github.com/1by3/picasso-releases).
"""The HTTP routes under /picasso/v1. Only aiohttp is imported, so tests mount them on their own app."""

from aiohttp import web

from . import validation as v

NAME = "picasso-remote"
VERSION = "0.1.0"
API = 1
CLIENT_HEADER = "X-Picasso-Client"
MAX_BODY = 16 * 1024
_STATUS = {"busy": 429, "exists": 409}


def _error(error):
    body = {"error": {"code": error.code, "message": error.message}}
    return web.json_response(body, status=_STATUS.get(error.code, 400))


def add_routes(routes, downloads, folders, free_bytes):
    """Register the handlers on an aiohttp route table.

    `folders()` returns the allowed model folder names, `free_bytes()` the free space in bytes (or
    None when it is not known).
    """

    def guard(request):
        # A header a web page cannot add to a cross-site request without a preflight, so a page
        # you happen to have open cannot start downloads on this server.
        if CLIENT_HEADER not in request.headers:
            raise v.ValidationError("notAllowed", "missing %s header" % CLIENT_HEADER)

    @routes.get("/picasso/v1/info")
    async def info(request):
        return web.json_response(
            {
                "name": NAME,
                "version": VERSION,
                "api": API,
                "folders": sorted(folders()),
                "freeBytes": free_bytes(),
                "maxActive": downloads._max_active,
                "allowedHosts": list(v.ALLOWED_HOSTS),
            }
        )

    @routes.post("/picasso/v1/downloads")
    async def submit(request):
        try:
            guard(request)
            if request.content_length is None or request.content_length > MAX_BODY:
                raise v.ValidationError("notAllowed", "request too large")
            try:
                body = await request.json()
            except ValueError:
                raise v.ValidationError("notAllowed", "expected JSON") from None
            job = downloads.submit(body)
        except v.ValidationError as error:
            return _error(error)
        return web.json_response(job.view())

    @routes.get("/picasso/v1/downloads")
    async def list_jobs(request):
        return web.json_response({"jobs": [j.view() for j in downloads.jobs.values()]})

    @routes.get("/picasso/v1/downloads/{id}")
    async def one(request):
        job = downloads.get(request.match_info["id"])
        if not job:
            return web.json_response({"error": {"code": "notFound", "message": "no such job"}}, status=404)
        return web.json_response(job.view())

    @routes.post("/picasso/v1/downloads/{id}/pause")
    async def pause(request):
        try:
            guard(request)
        except v.ValidationError as error:
            return _error(error)
        job = downloads.pause(request.match_info["id"])
        if not job:
            return web.json_response({"error": {"code": "notFound", "message": "no such job"}}, status=404)
        return web.json_response(job.view())

    @routes.delete("/picasso/v1/downloads/{id}")
    async def cancel(request):
        try:
            guard(request)
        except v.ValidationError as error:
            return _error(error)
        job = downloads.cancel(request.match_info["id"])
        if not job:
            return web.json_response({"error": {"code": "notFound", "message": "no such job"}}, status=404)
        return web.json_response(job.view())
