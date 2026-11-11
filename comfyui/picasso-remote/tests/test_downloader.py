import asyncio
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import loader  # noqa: E402

loader.load()
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from picasso_remote import validation as v  # noqa: E402
from picasso_remote.downloader import Downloads  # noqa: E402
from picasso_remote.routes import CLIENT_HEADER, add_routes  # noqa: E402

DATA = bytes(range(256)) * 4096  # 1 MiB
SHA = hashlib.sha256(DATA).hexdigest()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def only_local(url):
    """Stands in for the host allowlist: the test server and a made-up "elsewhere" host."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.hostname != "127.0.0.1":
        raise v.ValidationError("notAllowed", "host not allowed: %s" % parts.hostname)
    return parts


class Source:
    """A small file server with Range support, redirects and a request log."""

    def __init__(self):
        self.requests = []
        self.ignore_range = False
        self.data = DATA
        self.slow = False

    async def file(self, request):
        self.requests.append((request.path, dict(request.headers)))
        data = self.data
        start = 0
        status = 200
        headers = {}
        rng = request.headers.get("Range")
        if rng and not self.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                return web.Response(status=416)
            status = 206
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, len(data) - 1, len(data))
        if self.slow:
            resp = web.StreamResponse(status=status, headers={**headers, "Content-Length": str(len(data) - start)})
            await resp.prepare(request)
            await resp.write(data[start : start + 1000])
            await asyncio.sleep(30)
            return resp
        return web.Response(status=status, body=data[start:], headers=headers)

    async def redirect(self, request):
        self.requests.append((request.path, dict(request.headers)))
        raise web.HTTPFound(request.query["to"])

    async def denied(self, request):
        return web.Response(status=403)

    def app(self):
        app = web.Application()
        app.router.add_get("/file", self.file)
        app.router.add_get("/go", self.redirect)
        app.router.add_get("/denied", self.denied)
        return app


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vae = os.path.join(self.tmp.name, "vae")
        self.source = Source()
        self.server = TestServer(self.source.app())
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.added = []
        self.downloads = Downloads(
            lambda: {"vae": [self.vae], "custom_nodes": [os.path.join(self.tmp.name, "c")]},
            on_file_added=self.added.append,
            check_url=only_local,
            retry_delay=0.01,
            reserve_bytes=0,
        )
        self.addAsyncCleanup(self.downloads.shutdown)

    def url(self, path="/file"):
        return "http://127.0.0.1:%d%s" % (self.server.port, path)

    def body(self, **over):
        body = {
            "url": self.url(),
            "folder": "vae",
            "filename": "a.safetensors",
            "sizeBytes": len(DATA),
            "sha256": SHA,
        }
        body.update(over)
        return body

    async def finish(self, job, timeout=10):
        for _ in range(int(timeout / 0.02)):
            if job.status not in ("queued", "downloading", "verifying"):
                return job
            await asyncio.sleep(0.02)
        self.fail("job did not finish: %s" % job.view())


class DownloadTests(Base):
    async def test_downloads_verifies_and_places(self):
        job = await self.finish(self.downloads.submit(self.body()))
        self.assertEqual(job.status, "done", job.error)
        dest = os.path.join(self.vae, "a.safetensors")
        self.assertEqual(read(dest), DATA)
        self.assertFalse(os.path.exists(dest + ".part"))
        self.assertEqual(self.added, ["vae"])
        self.assertEqual(job.view()["received"], len(DATA))

    async def test_resumes_from_a_partial_file(self):
        os.makedirs(self.vae)
        with open(os.path.join(self.vae, "a.safetensors.part"), "wb") as f:
            f.write(DATA[:300_000])
        job = await self.finish(self.downloads.submit(self.body()))
        self.assertEqual(job.status, "done", job.error)
        self.assertEqual(self.source.requests[-1][1].get("Range"), "bytes=300000-")
        self.assertEqual(read(os.path.join(self.vae, "a.safetensors")), DATA)

    async def test_server_that_ignores_range_restarts(self):
        self.source.ignore_range = True
        os.makedirs(self.vae)
        with open(os.path.join(self.vae, "a.safetensors.part"), "wb") as f:
            f.write(DATA[:1000])
        job = await self.finish(self.downloads.submit(self.body()))
        self.assertEqual(job.status, "done", job.error)

    async def test_wrong_checksum_deletes_the_file(self):
        job = await self.finish(self.downloads.submit(self.body(sha256="0" * 64)))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error["code"], "hash")
        self.assertEqual(os.listdir(self.vae), [])

    async def test_wrong_size_fails(self):
        job = await self.finish(self.downloads.submit(self.body(sizeBytes=len(DATA) - 5)))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error["code"], "size")
        self.assertFalse(os.path.exists(os.path.join(self.vae, "a.safetensors")))

    async def test_refused_by_the_source(self):
        job = await self.finish(self.downloads.submit(self.body(url=self.url("/denied"))))
        self.assertEqual(job.error["code"], "forbidden")

    async def test_does_not_overwrite(self):
        os.makedirs(self.vae)
        with open(os.path.join(self.vae, "a.safetensors"), "wb") as f:
            f.write(b"mine")
        with self.assertRaises(v.ValidationError) as ctx:
            self.downloads.submit(self.body())
        self.assertEqual(ctx.exception.code, "exists")
        self.assertEqual(read(os.path.join(self.vae, "a.safetensors")), b"mine")

    async def test_refused_request_keeps_the_finished_job_in_the_list(self):
        job = await self.finish(self.downloads.submit(self.body()))
        self.assertEqual(job.status, "done")
        with self.assertRaises(v.ValidationError):
            self.downloads.submit(self.body())
        self.assertIs(self.downloads.get(job.id), job)

    async def test_file_that_appears_while_queued_is_not_replaced(self):
        job = self.downloads.submit(self.body())
        os.makedirs(self.vae, exist_ok=True)
        with open(os.path.join(self.vae, "a.safetensors"), "wb") as f:
            f.write(b"mine")
        await self.finish(job)
        self.assertEqual(read(os.path.join(self.vae, "a.safetensors")), b"mine")

    async def test_same_file_twice_is_one_job(self):
        self.source.slow = True
        first = self.downloads.submit(self.body())
        second = self.downloads.submit(self.body())
        self.assertIs(first, second)

    async def test_pause_keeps_the_partial_file_and_cancel_removes_it(self):
        self.source.slow = True
        job = self.downloads.submit(self.body())
        for _ in range(200):
            if job.received > 0:
                break
            await asyncio.sleep(0.02)
        self.downloads.pause(job.id)
        await asyncio.sleep(0.2)
        self.assertEqual(job.status, "paused")
        part = os.path.join(self.vae, "a.safetensors.part")
        self.assertTrue(os.path.exists(part))
        self.assertGreater(os.path.getsize(part), 0)
        self.downloads.cancel(job.id)
        self.assertFalse(os.path.exists(part))
        self.assertIsNone(self.downloads.get(job.id))

    async def test_cancel_while_running_deletes_the_partial_file(self):
        self.source.slow = True
        job = self.downloads.submit(self.body())
        for _ in range(200):
            if job.received > 0:
                break
            await asyncio.sleep(0.02)
        self.downloads.cancel(job.id)
        await asyncio.sleep(0.2)
        self.assertEqual(job.status, "canceled")
        self.assertEqual(os.listdir(self.vae), [])

    async def test_concurrency_limit(self):
        self.source.slow = True
        jobs = [self.downloads.submit(self.body(filename="f%d.safetensors" % i)) for i in range(4)]
        await asyncio.sleep(0.3)
        self.assertEqual(sorted(j.status for j in jobs), ["downloading", "downloading", "queued", "queued"])

    async def test_too_many_waiting(self):
        self.downloads._max_unfinished = 2
        self.source.slow = True
        self.downloads.submit(self.body(filename="a1.safetensors"))
        self.downloads.submit(self.body(filename="a2.safetensors"))
        with self.assertRaises(v.ValidationError) as ctx:
            self.downloads.submit(self.body(filename="a3.safetensors"))
        self.assertEqual(ctx.exception.code, "busy")

    async def test_redirect_target_is_checked(self):
        # A redirect to a host that is not allowed is refused.
        bad = self.url("/go?to=https://example.com/file")
        job = await self.finish(self.downloads.submit(self.body(url=bad)))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error["code"], "notAllowed")

    async def test_redirect_to_allowed_host_is_followed(self):
        good = self.url("/go?to=" + self.url("/file"))
        job = await self.finish(self.downloads.submit(self.body(url=good)))
        self.assertEqual(job.status, "done", job.error)

    async def test_token_is_not_sent_to_other_hosts(self):
        # 127.0.0.1 is not a Hugging Face host, so the Authorization header must not go out.
        job = await self.finish(
            self.downloads.submit(self.body(headers={"Authorization": "Bearer hf_secret"}))
        )
        self.assertEqual(job.status, "done", job.error)
        for _path, headers in self.source.requests:
            self.assertNotIn("Authorization", headers)

    async def test_invalid_requests(self):
        for over in (
            {"folder": "custom_nodes"},
            {"folder": "nope"},
            {"filename": "../a.safetensors"},
            {"filename": "evil.py"},
            {"url": "https://civitai.com/api/download/models/1"},
            {"sizeBytes": 0},
            {"sha256": "xyz"},
            {"headers": {"Cookie": "a"}},
        ):
            with self.assertRaises(v.ValidationError, msg=repr(over)):
                self.downloads.submit(self.body(**over))
        self.assertEqual(self.downloads.jobs, {})


class StrictUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_checker_refuses_loopback(self):
        with tempfile.TemporaryDirectory() as d:
            downloads = Downloads(lambda: {"vae": [d]})
            with self.assertRaises(v.ValidationError):
                downloads.submit(
                    {
                        "url": "https://127.0.0.1/a.safetensors",
                        "folder": "vae",
                        "filename": "a.safetensors",
                        "sizeBytes": 10,
                    }
                )


class RouteTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        routes = web.RouteTableDef()
        add_routes(routes, self.downloads, lambda: ["vae"], lambda: 12345)
        app = web.Application()
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.h = {CLIENT_HEADER: "1"}

    async def test_info(self):
        res = await self.client.get("/picasso/v1/info")
        body = await res.json()
        self.assertEqual(body["name"], "picasso-remote")
        self.assertEqual(body["api"], 1)
        self.assertEqual(body["folders"], ["vae"])
        self.assertEqual(body["freeBytes"], 12345)
        self.assertNotIn("civitai.com", body["allowedHosts"])

    async def test_mutations_need_the_client_header(self):
        res = await self.client.post("/picasso/v1/downloads", json=self.body())
        self.assertEqual(res.status, 400)
        res = await self.client.delete("/picasso/v1/downloads/x")
        self.assertEqual(res.status, 400)

    async def test_submit_poll_and_cancel(self):
        res = await self.client.post("/picasso/v1/downloads", json=self.body(), headers=self.h)
        self.assertEqual(res.status, 200)
        job = await res.json()
        for _ in range(200):
            got = await (await self.client.get("/picasso/v1/downloads/" + job["id"])).json()
            if got["status"] == "done":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(got["status"], "done")
        listing = await (await self.client.get("/picasso/v1/downloads")).json()
        self.assertEqual([j["id"] for j in listing["jobs"]], [job["id"]])
        res = await self.client.delete("/picasso/v1/downloads/" + job["id"], headers=self.h)
        self.assertEqual(res.status, 200)
        self.assertTrue(os.path.exists(os.path.join(self.vae, "a.safetensors")))
        res = await self.client.get("/picasso/v1/downloads/" + job["id"])
        self.assertEqual(res.status, 404)

    async def test_error_responses(self):
        res = await self.client.post(
            "/picasso/v1/downloads", json=self.body(filename="../x.safetensors"), headers=self.h
        )
        self.assertEqual(res.status, 400)
        self.assertEqual((await res.json())["error"]["code"], "notAllowed")
        os.makedirs(self.vae)
        open(os.path.join(self.vae, "a.safetensors"), "wb").close()
        res = await self.client.post("/picasso/v1/downloads", json=self.body(), headers=self.h)
        self.assertEqual(res.status, 409)
        res = await self.client.post(
            "/picasso/v1/downloads", data="not json", headers={**self.h, "Content-Type": "application/json"}
        )
        self.assertEqual(res.status, 400)


if __name__ == "__main__":
    unittest.main()
