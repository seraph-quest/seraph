"""Fixed Telegram getFile/file HTTPS boundary; no authority or retry policy."""
from contextlib import asynccontextmanager
import json
import logging

import httpx

from src.work_board.repository import BoardError
from src.work_board.document_channel_ingest import validate_file_path


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record):
        # HTTPX/httpcore messages can contain the token-bearing request target.
        # Suppress those records for the complete bounded private operation.
        return False


class TelegramDocumentHTTP:
    def __init__(self, *, transport=None):
        self._transport = transport

    @asynccontextmanager
    async def acquire(self, token, file_id, *, timeout, before_bytes):
        filters = []
        for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2"):
            logger = logging.getLogger(name); private_filter = _PrivateRequestFilter()
            logger.addFilter(private_filter); filters.append((logger, private_filter))
        try:
            async with httpx.AsyncClient(transport=self._transport, trust_env=False,
                follow_redirects=False, verify=True, timeout=timeout) as client:
                # URLs are constructed only here and never enter public receipts.
                get_url = "https://api.telegram.org/bot" + token + "/getFile"
                async with client.stream("POST", get_url, json={"file_id": file_id}) as response:
                    if response.status_code != 200:
                        raise BoardError("channel_document_provider_rejected", "Telegram rejected document metadata acquisition", status_code=409)
                    body = bytearray()
                    async for chunk in response.aiter_bytes(65536):
                        if len(body) + len(chunk) > 8192:
                            raise BoardError("channel_document_provider_metadata_bound", "Telegram document metadata exceeded its bound", status_code=409)
                        body.extend(chunk)
                    result = json.loads(body)
                    if result.get("ok") is not True or type(result.get("result")) is not dict:
                        raise BoardError("channel_document_provider_rejected", "Telegram document metadata is unavailable", status_code=409)
                    path = validate_file_path(result["result"].get("file_path"))
                await before_bytes()
                byte_url = "https://api.telegram.org/file/bot" + token + "/" + path
                async with client.stream("GET", byte_url) as response:
                    if response.status_code != 200:
                        raise BoardError("channel_document_provider_rejected", "Telegram rejected document byte acquisition", status_code=409)
                    yield response.aiter_bytes(65536)
        except BoardError:
            raise
        except Exception:
            # Never format a raw transport exception/response/request or path.
            raise BoardError("channel_document_transport_unknown", "Original document contact outcome is uncertain; cleanup is required", status_code=409) from None
        finally:
            for logger, private_filter in filters:
                logger.removeFilter(private_filter)
