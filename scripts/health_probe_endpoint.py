"""Resolve static Health request identity from Registry's reviewed policy."""

from urllib.parse import urlsplit

KORAD_SOURCE = "https://www.data.go.kr/data/15004206/openapi.do"
KORAD_ROOT = "/openapi/service/weatherMoniterSvc"
KORAD_METHOD = KORAD_ROOT + "/getWeatherMoniterDataList"
KORAD_CORRECTION = {
    "source_path": KORAD_ROOT,
    "request_path": KORAD_METHOD,
    "source": KORAD_SOURCE,
    "upstream_operation_seq": "6836",
}


def resolve_endpoint(selection: dict, dataset: dict, operation: dict) -> dict:
    parsed = urlsplit(operation["endpoint"])
    correction = selection.get("endpoint_correction")
    # As in the existing catalog projection, source query values are excluded.
    # Only the separately reviewed bounded parameter policy supplies parameters.
    # In particular, never carry source credentials into a Health request.
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.netloc != parsed.hostname
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or parsed.fragment
        or any(character in operation["endpoint"] for character in "\\\r\n")
    ):
        raise ValueError("Health source endpoint must have an explicit HTTP(S) authority and pure operation path")
    endpoint = {"scheme": parsed.scheme, "host": parsed.hostname, "path": parsed.path}
    if selection.get("operation_id") == "dpr-op-00000007" and dataset["id"] == "15004206" and parsed.hostname == "www.korad.or.kr" and parsed.path == KORAD_ROOT and correction is None:
        raise ValueError("KORAD service-root selection requires its reviewed operation correction")
    if correction is not None:
        if (
            correction != KORAD_CORRECTION
            or selection.get("operation_id") != "dpr-op-00000007"
            or dataset["id"] != "15004206"
            or str(operation["source"]["raw"]["operation_seq"]) != "6836"
            or parsed.hostname != "www.korad.or.kr"
            or parsed.path not in {KORAD_ROOT, KORAD_METHOD}
        ):
            raise ValueError("Health endpoint correction differs from the reviewed KORAD identity")
        endpoint.update(path=KORAD_METHOD, source_path=parsed.path, correction_source=KORAD_SOURCE)
    return endpoint
