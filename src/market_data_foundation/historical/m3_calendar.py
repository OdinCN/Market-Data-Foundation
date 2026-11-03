"""Single official calendar HTTP exchange under the existing Census control."""

import http.client
import time

from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
from market_data_foundation.historical.cpi_request_control import CpiRequestControlError


def fetch_calendar(control):
    connection = http.client.HTTPSConnection("www.census.gov", timeout=20)
    deadline = time.monotonic() + 20
    try:
        with control.attempt() as admission:
            connection.request(
                "GET",
                "/manufacturing/m3/release_schedule.html",
                headers={"Accept": "text/html", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            retry = admission.record_response(
                response.status, dict(response.getheaders())
            )
            if response.status != 200:
                raise CpiScheduleError(
                    "m3_calendar_http_failure",
                    http_status=response.status,
                    retry_at=retry,
                )
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if connection.sock:
                    connection.sock.settimeout(remaining)
                block = response.read1(min(65536, 2_000_001 - len(body)))
                if not block:
                    break
                body.extend(block)
                if len(body) > 2_000_000:
                    raise CpiScheduleError("m3_calendar_body_limit")
            return bytes(body)
    except CpiRequestControlError as error:
        raise CpiScheduleError(
            "m3_calendar_request_wait", retry_at=error.retry_at
        ) from None
    except CpiScheduleError:
        raise
    except Exception:
        raise CpiScheduleError("m3_calendar_transport_failure") from None
    finally:
        connection.close()
