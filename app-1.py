from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

APP_VERSION = "2.0.0-safe-direct-media"
STORAGE = Path("/tmp/acquisition")
STORAGE.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 500 * 1024 * 1024
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 60.0
TOTAL_TIMEOUT = 15 * 60.0
CHUNK_SIZE = 1024 * 1024

app = FastAPI(title="ClipShortener Acquisition Service", version=APP_VERSION)


class AcquireRequest(BaseModel):
    url: str


def host_is_public(host: str) -> bool:
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def validate_url(value: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "Enter a valid HTTP or HTTPS direct media URL.")
    if not host_is_public(parsed.hostname):
        raise HTTPException(400, "The URL must point to a public internet host.")
    return value


async def open_stream(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, str]:
    current = validate_url(url)

    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if not host_is_public(parsed.hostname or ""):
            raise HTTPException(400, "A redirect targeted a private or unsafe host.")

        response = await client.send(
            client.build_request(
                "GET",
                current,
                headers={
                    "User-Agent": "ClipShortener-Acquisition/2.0",
                    "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.1",
                },
            ),
            follow_redirects=False,
            stream=True,
        )

        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise HTTPException(400, "The media server returned an invalid redirect.")
            current = urljoin(current, location)
            continue

        if response.status_code < 200 or response.status_code >= 300:
            status = response.status_code
            await response.aclose()
            raise HTTPException(400, f"The media server returned HTTP {status}.")

        return response, current

    raise HTTPException(400, "Too many redirects.")


def filename_from_response(response: httpx.Response, final_url: str) -> str:
    content_disposition = response.headers.get("content-disposition", "")
    name = ""
    marker = 'filename='
    if marker in content_disposition.lower():
        name = content_disposition.split(marker, 1)[1].strip().strip('"\'')
    if not name:
        name = Path(urlparse(final_url).path).name
    name = Path(name or "video.mp4").name
    if "." not in name:
        name += ".mp4"
    return name[:180]


@app.get("/")
async def root():
    return {"service": "ClipShortener Acquisition Service", "version": APP_VERSION}


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/acquire")
async def acquire(payload: AcquireRequest):
    url = validate_url(payload.url)
    job_id = uuid.uuid4().hex
    path = STORAGE / f"{job_id}.bin"

    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT,
        write=READ_TIMEOUT,
        pool=CONNECT_TIMEOUT,
    )

    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response, final_url = await open_stream(client, url)
        try:
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_FILE_SIZE:
                        raise HTTPException(413, "The media file is larger than 500 MB.")
                except ValueError:
                    pass

            total = 0
            with path.open("wb") as target:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_FILE_SIZE:
                        path.unlink(missing_ok=True)
                        raise HTTPException(413, "The media file is larger than 500 MB.")
                    target.write(chunk)

                    if time.monotonic() - started > TOTAL_TIMEOUT:
                        path.unlink(missing_ok=True)
                        raise HTTPException(408, "The media download timed out.")

        finally:
            await response.aclose()

    if total <= 0:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "The media server returned an empty file.")

    filename = filename_from_response(response, final_url)
    # Return the service URL. The processing backend will stream it into its own job storage.
    return {
        "ok": True,
        "job_id": job_id,
        "filename": filename,
        "size": total,
        "download_url": f"/files/{job_id}.bin",
    }


@app.get("/files/{name}")
async def file_download(name: str):
    # Kept intentionally simple: only generated .bin files are exposed.
    if "/" in name or "\\" in name or not name.endswith(".bin"):
        raise HTTPException(400, "Invalid acquisition file.")
    path = STORAGE / Path(name).name
    if not path.is_file():
        raise HTTPException(404, "Acquisition file not found or expired.")

    from fastapi.responses import FileResponse
    return FileResponse(path, media_type="application/octet-stream")


async def cleanup_loop():
    while True:
        cutoff = time.time() - 60 * 60
        for path in STORAGE.glob("*.bin"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        await asyncio.sleep(600)


@app.on_event("startup")
async def startup():
    asyncio.create_task(cleanup_loop())
