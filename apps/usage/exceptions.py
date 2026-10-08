"""API errors raised when metering refuses work (mapped to stable error codes)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import APIException


class InsufficientCredits(APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = "Insufficient credits for this request."
    default_code = "insufficient_credits"


class SpendingLimitReached(APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = "The organization's monthly spending limit has been reached."
    default_code = "spending_limit_reached"


class QuotaExceeded(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "The plan quota for this feature is exhausted for the current period."
    default_code = "quota_exceeded"


class ConcurrencyLimitExceeded(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "Too many concurrent runs for this organization; wait for one to finish."
    default_code = "concurrency_limit"
