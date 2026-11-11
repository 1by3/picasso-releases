# Picasso remote downloads for ComfyUI.
# Part of Picasso (https://github.com/1by3/picasso-releases).
"""The download queue: streams allowed files into ComfyUI's model folders.

It imports nothing from ComfyUI (the folders come in through `folders`), so tests run it against
a local web server. Jobs live in memory; a restart of ComfyUI forgets them but leaves `.part`
files, which the next request for the same file continues from.
"""

import asyncio
import errno
import hashlib
import os
import shutil
import time
import uuid
from urllib.parse import urljoin

import aiohttp

from . import validation as v

ACTIVE = ("queued", "downloading", "verifying")
CHUNK = 1024 * 1024
REDIRECTS = 8
RETRIES = 3
KEEP_FINISHED = 100


class Job:
    def __init__(self, folder, filename, url, size, sha256, headers, directory):
        self.id = uuid.uuid4().hex
        self.folder = folder
        self.filename = filename
        self.url = url
        self.size = size
        self.sha256 = sha256
        self.headers = headers
        self.dest = v.resolve_destination(directory, filename)
        self.part = self.dest + ".part"
        self.status = "queued"
        self.received = 0
        self.error = None
        self.stop = None  # "pause" or "cancel" while the task is being stopped
        self.task = None
        self.created = time.time()

    def view(self):
        out = {
            "id": self.id,
            "folder": self.folder,
            "filename": self.filename,
            "sizeBytes": self.size,
            "received": self.received,
            "status": self.status,
            "createdAt": int(self.created * 1000),
        }
        if self.error:
            out["error"] = dict(self.error)
        return out


def _hash_prefix(path, hasher, length):
    """Feed the first `length` bytes of a file into `hasher`."""
    left = length
    with open(path, "rb") as f:
        while left > 0:
            data = f.read(min(CHUNK, left))
            if not data:
                break
            hasher.update(data)
            left -= len(data)
    return length - left


def _place(part, dest):
    """Move a finished file into place without ever replacing an existing one."""
    try:
        os.link(part, dest)
    except FileExistsError:
        raise v.ValidationError("exists", "a file with this name already exists") from None
    except OSError:
        # No hard links on this file system: check, then rename (not atomic, but still no overwrite).
        if os.path.exists(dest):
            raise v.ValidationError("exists", "a file with this name already exists") from None
        os.replace(part, dest)
        return
    os.remove(part)


class Downloads:
    def __init__(
        self,
        folders,
        on_file_added=None,
        check_url=v.check_url,
        max_active=2,
        max_unfinished=32,
        retry_delay=2.0,
        reserve_bytes=64 * 1024 * 1024,
        user_agent="picasso-remote",
    ):
        """`folders()` returns {folder name: [directories]}, read on every request."""
        self._folders = folders
        self._on_file_added = on_file_added
        self._check_url = check_url
        self._max_active = max_active
        self._max_unfinished = max_unfinished
        self._retry_delay = retry_delay
        self._reserve = reserve_bytes
        self._user_agent = user_agent
        self._sem = None
        self.jobs = {}

    # -- requests ---------------------------------------------------------------------------

    def _semaphore(self):
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_active)
        return self._sem

    def submit(self, body):
        """Validate a request and queue it. Returns the job (an existing one for the same file)."""
        if not isinstance(body, dict):
            raise v.ValidationError("notAllowed", "expected a JSON object")
        known = self._folders()
        folder = v.check_folder(body.get("folder"), known)
        filename = v.check_filename(body.get("filename"))
        url = body.get("url")
        self._check_url(url)
        size = v.check_size(body.get("sizeBytes"))
        sha256 = v.check_sha256(body.get("sha256"))
        headers = v.check_headers(body.get("headers"))
        directory = v.choose_folder_path(folder, known[folder])
        # Build the destination first: this is where a path that leaves the folder is refused.
        job = Job(folder, filename, url, size, sha256, headers, directory)

        stale = []
        for other in list(self.jobs.values()):
            if other.folder == folder and other.filename == filename:
                if other.status in ACTIVE:
                    return other
                stale.append(other)
        for path in known[folder]:
            if os.path.exists(os.path.join(path, filename)):
                raise v.ValidationError("exists", "a file with this name already exists")
        # Only now forget the earlier (paused, failed) attempts: a refused request leaves them.
        for other in stale:
            del self.jobs[other.id]
        if sum(1 for j in self.jobs.values() if j.status in ACTIVE) >= self._max_unfinished:
            raise v.ValidationError("busy", "too many downloads are waiting")

        self.jobs[job.id] = job
        self._forget_old()
        job.task = asyncio.ensure_future(self._run(job))
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def pause(self, job_id):
        """Stop a running or waiting job and keep its partial file."""
        job = self.jobs.get(job_id)
        if job and job.status in ACTIVE and job.task:
            job.stop = "pause"
            job.task.cancel()
        return job

    def cancel(self, job_id):
        """Stop a job, delete its partial file and forget it. A finished job is only forgotten."""
        job = self.jobs.get(job_id)
        if not job:
            return None
        if job.status in ACTIVE and job.task:
            job.stop = "cancel"
            job.task.cancel()
            return job
        if job.status != "done":
            self._remove(job.part)
        job.status = "canceled" if job.status != "done" else "done"
        del self.jobs[job.id]
        return job

    async def shutdown(self):
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _forget_old(self):
        finished = [j for j in self.jobs.values() if j.status not in ACTIVE]
        for job in sorted(finished, key=lambda j: j.created)[: max(0, len(finished) - KEEP_FINISHED)]:
            del self.jobs[job.id]

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    # -- work -------------------------------------------------------------------------------

    @staticmethod
    async def _offload(fn, *args):
        """Run blocking work in a thread. A cancel waits for the thread, so a file is never
        closed while it is still being written."""
        fut = asyncio.get_running_loop().run_in_executor(None, fn, *args)
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            await asyncio.wait([fut])
            raise

    async def _run(self, job):
        try:
            async with self._semaphore():
                await self._transfer(job)
            job.status = "done"
            job.received = job.size
            if self._on_file_added:
                try:
                    self._on_file_added(job.folder)
                except Exception:  # noqa: BLE001 (a refresh hook must not fail the download)
                    pass
        except asyncio.CancelledError:
            if job.stop == "cancel":
                job.status = "canceled"
                self._remove(job.part)
                self.jobs.pop(job.id, None)
            elif job.stop == "pause":
                job.status = "paused"
            else:
                job.status = "paused"
                raise
        except v.ValidationError as error:
            job.status = "failed"
            job.error = {"code": error.code, "message": error.message}
        except OSError as error:
            job.status = "failed"
            code = "diskFull" if error.errno in (errno.ENOSPC, errno.EDQUOT) else "write"
            job.error = {"code": code, "message": str(error)}
        except Exception as error:  # noqa: BLE001 (report anything unexpected on the job)
            job.status = "failed"
            job.error = {"code": "network", "message": "%s: %s" % (type(error).__name__, error)}

    async def _transfer(self, job):
        if os.path.exists(job.dest):
            raise v.ValidationError("exists", "a file with this name already exists")
        directory = os.path.dirname(job.dest)
        os.makedirs(directory, exist_ok=True)

        offset = os.path.getsize(job.part) if os.path.exists(job.part) else 0
        if offset > job.size:
            self._remove(job.part)
            offset = 0
        free = shutil.disk_usage(directory).free
        if free < job.size - offset + self._reserve:
            raise v.ValidationError("diskFull", "not enough free space")

        state = {"hasher": hashlib.sha256() if job.sha256 else None}
        job.status = "downloading"
        job.received = offset
        if offset and state["hasher"]:
            # The bytes already on disk count toward the checksum.
            job.received = await self._offload(_hash_prefix, job.part, state["hasher"], offset)

        attempt = 0
        while job.received < job.size or not os.path.exists(job.part):
            try:
                await self._stream(job, state)
                if job.received < job.size:
                    raise aiohttp.ClientPayloadError("the transfer ended early")
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as error:
                if attempt >= RETRIES:
                    raise v.ValidationError("network", str(error) or type(error).__name__) from None
                attempt += 1
                await asyncio.sleep(self._retry_delay)
        await self._finish(job, state)

    async def _stream(self, job, state):
        """One request, from `job.received` to the end, following redirects by hand so every
        address is checked and the token only goes to Hugging Face."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
        url = job.url
        hops = 0
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            while True:
                self._check_url(url)
                headers = {"User-Agent": self._user_agent, "Accept-Encoding": "identity"}
                if job.received:
                    headers["Range"] = "bytes=%d-" % job.received
                if job.headers and v.token_allowed_for(url):
                    headers.update(job.headers)
                async with session.get(url, headers=headers, allow_redirects=False) as resp:
                    status = resp.status
                    if status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        hops += 1
                        if not location or hops > REDIRECTS:
                            raise v.ValidationError("http", "too many redirects")
                        url = urljoin(url, location)
                        continue
                    if status == 401:
                        raise v.ValidationError("unauthorized", "HTTP 401")
                    if status == 403:
                        raise v.ValidationError("forbidden", "HTTP 403")
                    if status == 416:
                        if job.received >= job.size:
                            return
                        # The partial file does not match what the server has: start over.
                        self._remove(job.part)
                        job.received = 0
                        state["hasher"] = hashlib.sha256() if job.sha256 else None
                        continue
                    if status in (408, 425, 429) or status >= 500:
                        raise aiohttp.ClientError("HTTP %d" % status)
                    if status not in (200, 206):
                        raise v.ValidationError("http", "HTTP %d" % status)
                    mode = "ab"
                    if status == 200:
                        if resp.content_length is not None and resp.content_length != job.size:
                            raise v.ValidationError("size", "the server reports another size")
                        if job.received:
                            # The server ignored the Range header: take the whole file again.
                            job.received = 0
                            state["hasher"] = hashlib.sha256() if job.sha256 else None
                        mode = "wb"
                    else:
                        rng = resp.headers.get("Content-Range", "")
                        if not rng.startswith("bytes %d-" % job.received) or not rng.endswith(
                            "/%d" % job.size
                        ):
                            raise v.ValidationError("http", "unexpected byte range")
                    await self._write_body(job, state, resp, mode)
                    return

    async def _write_body(self, job, state, resp, mode):
        hasher = state["hasher"]
        f = open(job.part, mode)  # noqa: SIM115 (closed in finally, after the last write)

        def write(chunk):
            f.write(chunk)
            if hasher:
                hasher.update(chunk)

        try:
            async for chunk in resp.content.iter_chunked(CHUNK):
                if job.received + len(chunk) > job.size:
                    f.close()
                    self._remove(job.part)
                    raise v.ValidationError("size", "the file is larger than expected")
                await self._offload(write, chunk)
                job.received += len(chunk)
        finally:
            f.close()

    async def _finish(self, job, state):
        job.status = "verifying"
        if os.path.getsize(job.part) != job.size:
            self._remove(job.part)
            raise v.ValidationError("size", "the file has the wrong size")
        if job.sha256 and state["hasher"].hexdigest() != job.sha256:
            self._remove(job.part)
            raise v.ValidationError("hash", "the checksum does not match")
        _place(job.part, job.dest)
