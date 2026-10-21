"""Structured JSON logging with correlation IDs (doc NFR-23).

Uses AWS Lambda Powertools Logger when available in the runtime; otherwise an
equivalent std-lib JSON formatter so unit tests need no extra dependency.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from typing import Any

_CONTEXT: dict[str, Any] = {}


def set_correlation_id(cid: str) -> None:
    _CONTEXT["correlation_id"] = cid


def get_correlation_id() -> str:
    return _CONTEXT.get("correlation_id") or os.environ.get(
        "CORRELATION_ID", "") or str(uuid.uuid4())[:8]


def new_correlation_id() -> str:
    cid = str(uuid.uuid4())
    set_correlation_id(cid)
    return cid


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "correlationId": get_correlation_id(),
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str, **context: Any) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        try:  # Powertools when bundled (prod-identical output), std-lib fallback otherwise
            from aws_lambda_powertools import Logger as PL  # type: ignore

            logger.handlers = []
            logger.addHandler(PL(service=name, **context))
        except Exception:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(_JsonFormatter())
            logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
    for key, value in context.items():
        setattr(logger, f"_ctx_{key}", value)
    return logger


def log_extra(logger: logging.Logger, **fields: Any) -> None:
    logger.info(
        "event",
        extra={"extra_fields": {**fields, "correlationId": get_correlation_id()}},
    )
