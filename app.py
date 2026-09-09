import os
import uuid
import socket
import ipaddress
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel


app = FastAPI(title="ClipShortener Acquisition Service")

STORAGE = Path("/tmp/acquisition")
STORAGE.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class AcquireRequest(BaseModel):
    url: str


def validate_url(url: str) -> None:
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "Only HTTP and HTTPS URLs are supported.")

    if not parsed.hostname:
        raise HTTPException(400, "Invalid URL.")

    hostname = parsed.hostname.lower()

    if hostname in {"localhost", "localhost.localdomain"}:
        raise HTTPException(400, "Local addresses are not allowed.")

    try:
        addresses = socket.getaddrinfo(
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise HTTPException(400, "Could not resolve the host.")

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(400, "Private or local addresses are not allowed.")


@app.get("/")
async def root():
    return {
        "service": "ClipShortener Acquisition Service",
        "status": "ok",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/acquire")
async def acquire(request: AcquireRequest):
    validate_url(request.url)

    job_id = uuid.uuid4().hex
    output = STORAGE / f"{job_id}.mp4"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=TIMEOUT,
        ) as client:

            async with client.stream("GET", request.url) as response:
                response.raise_for_status()

                content_length = response.headers.get("content-length")

                if content_length:
                    try:
                        if int(content_length) > MAX_FILE_SIZE:
                            raise HTTPException(
                                413,
                                "Video is larger than the 500 MB limit.",
                            )
                    except ValueError:
                        pass

                total = 0

                with output.open("wb") as file:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)

                        if total > MAX_FILE_SIZE:
                            file.close()
                            output.unlink(missing_ok=True)

                            raise HTTPException(
                                413,
                                "Video is larger than the 500 MB limit.",
                            )

                        file.write(chunk)

        if not output.exists() or output.stat().st_size == 0:
            output.unlink(missing_ok=True)
            raise HTTPException(400, "The downloaded file was empty.")

        return {
            "status": "success",
            "id": job_id,
            "size": output.stat().st_size,
            "download_url": f"/files/{job_id}",
        }

    except HTTPException:
        raise

    except httpx.HTTPError as exc:
        output.unlink(missing_ok=True)
        raise HTTPException(
            502,
            f"Source download failed: {exc}",
        )

    except Exception as exc:
        output.unlink(missing_ok=True)
        raise HTTPException(
            500,
            f"Acquisition failed: {exc}",
        )


@app.get("/files/{job_id}")
async def get_file(job_id: str):
    if not job_id.isalnum():
        raise HTTPException(400, "Invalid file ID.")

    path = STORAGE / f"{job_id}.mp4"

    if not path.exists():
        raise HTTPException(404, "File not found.")

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{job_id}.mp4",
              )
