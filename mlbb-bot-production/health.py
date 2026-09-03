"""Tiny dependency-free health endpoint for container/process monitoring."""
import asyncio
import logging

log = logging.getLogger("mlbb-bot.health")


async def start_health_server(host: str, port: int, ready: asyncio.Event) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.read(2048), timeout=2)
            first_line = request.split(b"\r\n", 1)[0]
            if first_line.startswith(b"GET /health ") and ready.is_set():
                body = b'{"status":"ok"}'
                status = b"200 OK"
            else:
                body = b'{"status":"starting"}'
                status = b"503 Service Unavailable"
            response = (
                b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            log.debug("Health request failed", exc_info=True)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port)
    log.info("Health endpoint listening on %s:%s", host, port)
    return server
