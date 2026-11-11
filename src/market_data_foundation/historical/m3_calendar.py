"""Official calendar under the existing Census ticket and a complete IO bound."""

from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
from market_data_foundation.historical.cpi_request_control import CpiRequestControlError
from market_data_foundation.historical.public_web_transport import (
    PublicFetchError,
    fetch_public,
)


def fetch_calendar(control):
    try:
        with control.attempt() as admission:
            retry = None

            def record(status, headers):
                nonlocal retry
                retry = admission.record_response(status, headers)

            response = fetch_public(
                "https://www.census.gov/manufacturing/m3/release_schedule.html",
                "m3_calendar",
                timeout=20,
                on_headers=record,
            )
            if response.status != 200:
                raise CpiScheduleError(
                    "m3_calendar_http_failure",
                    http_status=response.status,
                    retry_at=retry,
                )
            return response.body
    except CpiRequestControlError as error:
        raise CpiScheduleError(
            "m3_calendar_request_wait", retry_at=error.retry_at
        ) from None
    except PublicFetchError as error:
        reason = (
            "m3_calendar_body_limit"
            if error.classification == "payload_too_large"
            else "m3_calendar_" + error.classification
        )
        raise CpiScheduleError(reason, http_status=error.http_status) from None
