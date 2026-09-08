"""Read Tianchi's public leaderboard API, retaining every original response."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP, localcontext
import json
import math
import re
import time
import threading
from urllib.parse import urlencode

from beauclaw.core import InvalidBoard, Response, dumps, fetch_url, score_text, utcnow
from beauclaw.diagnostics import exception_details, sanitize

ORIGIN = "https://tianchi.aliyun.com"
API = ORIGIN + "/v3/proxy/competition/api/race/"
BOARD = "leaderboard"
MAX_PAGES = 100


class RequestFailed(InvalidBoard):
    def __init__(self, response: Response):
        self.response = response
        super().__init__(response.error or f"Tianchi API HTTP {response.status}; the complete leaderboard was not compared")


def page_url(event_id: str) -> str:
    return f"{ORIGIN}/competition/entrance/{event_id}/rankingList"


def unpack(body: str | bytes) -> dict:
    try:
        value = json.loads(body, parse_float=str)
    except (ValueError, UnicodeError):
        raise InvalidBoard("Tianchi returned invalid JSON; the leaderboard was not compared") from None
    if not isinstance(value, dict) or value.get("success") is not True or value.get("code") != "SUCCESS":
        raise InvalidBoard("Tianchi API did not return a successful public leaderboard response")
    if not isinstance(value.get("data"), dict):
        raise InvalidBoard("Tianchi response is missing its data object")
    return value["data"]


def current_season(detail: dict) -> dict | None:
    seasons = detail.get("raceSeasons")
    if not isinstance(seasons, list):
        raise InvalidBoard("Tianchi competition metadata is missing its seasons")
    available = [s for s in seasons if isinstance(s, dict) and s.get("hasLeaderBoard") is True]
    current = next((s for s in available if s.get("seasonId") == detail.get("currentSeasonId")), None)
    if current is None and available:
        current = available[-1]
    if current is not None and (current.get("seasonId") is None or type(current.get("seasonNum")) is not int):
        raise InvalidBoard("Tianchi season is missing its ID or number")
    return current


def read_detail(event_id: str, timeout: float = 8) -> dict:
    response = fetch_url(API + "getDetail?" + urlencode({"raceId": event_id}),
                         {"User-Agent": "Mozilla/5.0 BeauClaw", "Accept": "application/json", "Referer": page_url(event_id)}, timeout)
    if response.error or response.status != 200:
        raise ValueError(response.error or f"Tianchi competition metadata returned HTTP {response.status}")
    detail = unpack(response.body)
    if str(detail.get("race", {}).get("raceId")) != event_id:
        raise ValueError("Tianchi returned metadata for a different competition")
    return detail


def competition_title(event_id: str) -> str:
    name = read_detail(event_id).get("race", {}).get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Tianchi competition has no display name")
    return name.strip()


def pagination(data: dict) -> tuple[int, int]:
    total, size = data.get("total"), data.get("pageSize")
    if type(total) is not int or total < 0 or type(size) is not int or size < 1:
        raise InvalidBoard("Tianchi returned invalid pagination metadata")
    pages = max(1, math.ceil(total / size))
    if pages > MAX_PAGES:
        raise InvalidBoard(f"Tianchi leaderboard exceeds the {MAX_PAGES}-page collection limit")
    return total, pages


def fetch(event_id: str, auth: dict | None = None, timeout: float = 8) -> Response:
    # Public requests intentionally use no credentials, cookies or GitCode tokens.
    started, deadline = utcnow(), time.monotonic() + timeout
    bundle = {"provider": "tianchi", "competition_id": event_id, "pages": []}
    diagnostics = {"requests": [], "timeout_seconds": timeout}
    request_lock = threading.Lock()
    headers = {"User-Agent": "Mozilla/5.0 BeauClaw", "Accept": "application/json",
               "Cache-Control": "no-cache", "Referer": page_url(event_id)}

    def request(url: str, phase: str, page: int | None = None) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with request_lock:
                diagnostics["requests"].append({"endpoint": url, "phase": phase, "page": page,
                                                "http_status": None, "category": "network_timeout",
                                                "deadline_exceeded": True})
            raise InvalidBoard("Tianchi snapshot exceeded the request timeout; incomplete pages were not compared")
        response = fetch_url(url, headers, remaining)
        with request_lock:
            diagnostics["requests"].append({**response.diagnostics, "endpoint": url,
                                            "phase": phase, "page": page, "http_status": response.status})
        if response.error or response.status != 200:
            raise RequestFailed(response)
        if time.monotonic() > deadline:
            raise InvalidBoard("Tianchi snapshot exceeded the request timeout; incomplete pages were not compared")
        return {"url": url, "captured_at": response.captured_at,
                "headers": response.headers, "body": response.body.decode("utf-8")}

    error, status, evidence_headers = None, 200, {}
    try:
        bundle["detail"] = request(API + "getDetail?" + urlencode({"raceId": event_id}), "detail")
        detail = unpack(bundle["detail"]["body"])
        season = current_season(detail)
        if season and detail.get("showLeaderBoard") is not False:
            def get_page(page: int, phase: str = "page") -> dict:
                return request(API + "rank/list?" + urlencode({"pageNum": page, "season": season["seasonNum"], "raceId": event_id}), phase, page)
            first = get_page(1)
            bundle["pages"].append(first)
            first_data = unpack(first["body"])
            _, count = pagination(first_data)
            if count > 1:
                with ThreadPoolExecutor(max_workers=4, thread_name_prefix="tianchi-pages") as pool:
                    bundle["pages"].extend(pool.map(get_page, range(2, count + 1)))
                # Check the leading page again to reject a change during pagination.
                confirmation = get_page(1, "confirmation")
                bundle["confirmation"] = confirmation
                confirmed = unpack(confirmation["body"])
                if any(first_data.get(key) != confirmed.get(key) for key in ("total", "list", "scoreShowConfig")):
                    raise InvalidBoard("Tianchi leaderboard changed during pagination; retrying a complete snapshot")
    except (ValueError, OSError) as exc:
        error = str(exc) if isinstance(exc, InvalidBoard) else f"Tianchi snapshot failed ({type(exc).__name__})"
        diagnostics["exception"] = exception_details(exc)
        if isinstance(exc, RequestFailed):
            status, evidence_headers = exc.response.status, exc.response.headers
            if exc.response.diagnostics.get("category"):
                diagnostics["category"] = exc.response.diagnostics["category"]
            bundle["failed_request"] = {"url": exc.response.url, "body": exc.response.body.decode("utf-8", errors="replace")}
    return Response(started, utcnow(), page_url(event_id), status, dumps(bundle).encode(),
                    headers=evidence_headers, error=error, diagnostics=sanitize(diagnostics))


def display_score(score: str, pattern: str | None) -> str:
    if not isinstance(pattern, str) or not re.fullmatch(r"#?0(?:\.0{1,12})?", pattern):
        return score
    places = len(pattern.partition(".")[2])
    with localcontext() as context:
        context.prec = max(50, len(score) + places + 2)
        return format(Decimal(score).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")


def parse_board(body: bytes, observed_at: str) -> dict:
    location = {"phase": "bundle"}
    try:
        bundle = json.loads(body)
        if bundle.get("provider") != "tianchi" or not str(bundle.get("competition_id", "")).isdigit():
            raise InvalidBoard("Snapshot is not a Tianchi competition bundle")
        location = {"phase": "detail"}
        detail = unpack(bundle["detail"]["body"])
        race = detail["race"]
        if str(race["raceId"]) != bundle["competition_id"]:
            raise InvalidBoard("Tianchi snapshot contains a different competition")
        season = current_season(detail)
        result = {"status": "ok", "provider": "tianchi", "primary_board": BOARD,
                  "competition_id": bundle["competition_id"], "competition_name": str(race["name"]),
                  "source_url": page_url(bundle["competition_id"]), "boards": {}}
        if season is None or detail.get("showLeaderBoard") is False:
            return {**result, "status": "unavailable", "reason": "Tianchi has no public leaderboard for an available season"}
        result.update(schedule_id=str(season["seasonId"]), schedule_name=str(season.get("seasonName") or season["seasonId"]))
        pages = bundle["pages"]
        if not isinstance(pages, list) or not pages:
            raise InvalidBoard("Tianchi snapshot has no leaderboard pages")
        location = {"phase": "page", "page": 1}
        first = unpack(pages[0]["body"])
        total, count = pagination(first)
        if len(pages) != count:
            raise InvalidBoard("Tianchi snapshot is incomplete; not all leaderboard pages were captured")
        if count > 1:
            location = {"phase": "confirmation", "page": 1}
            confirmation = unpack(bundle["confirmation"]["body"])
            if any(first.get(key) != confirmation.get(key) for key in ("total", "list", "scoreShowConfig")):
                raise InvalidBoard("Tianchi leaderboard changed during pagination; retrying a complete snapshot")
        location = {"phase": "page", "page": 1}
        config = next((item for item in first.get("scoreShowConfig", []) if item.get("name") == "score"), {})
        members, previous_rank = {}, 1
        for page_number, page in enumerate(pages, 1):
            location = {"phase": "page", "page": page_number}
            data = unpack(page["body"])
            if data.get("pageNum") != page_number or pagination(data) != (total, count):
                raise InvalidBoard("Tianchi pagination changed; the snapshot was not compared")
            rows = data.get("list")
            expected = min(first["pageSize"], max(0, total - (page_number - 1) * first["pageSize"]))
            if not isinstance(rows, list) or len(rows) != expected:
                raise InvalidBoard("Tianchi leaderboard page is incomplete")
            for row_number, row in enumerate(rows, 1):
                location = {"phase": "page", "page": page_number, "row": row_number}
                if not isinstance(row, dict):
                    raise InvalidBoard(f"Tianchi page {page_number} contains an invalid team row")
                if str(row.get("raceId")) != bundle["competition_id"] or str(row.get("seasonId")) != result["schedule_id"]:
                    raise InvalidBoard("Tianchi page mixes different competitions or seasons")
                team_id = row.get("teamId")
                if not str(team_id).isdigit():
                    raise InvalidBoard(f"Tianchi page {page_number} is missing a valid teamId")
                key = f"team_id:{team_id}"
                name = row.get("teamName")
                if name is not None and not isinstance(name, str):
                    raise InvalidBoard(f"Tianchi page {page_number} contains an invalid teamName")
                name = name.strip() if name else ""
                position = len(members) + 1
                if key in members:
                    raise InvalidBoard("Tianchi row has an invalid or duplicate team identity")
                if type(row.get("rank")) is not int or not previous_rank <= row["rank"] <= position:
                    raise InvalidBoard("Tianchi ranking order changed or is incomplete; retrying a complete snapshot")
                previous_rank = row["rank"]
                score = score_text(row.get("score"))
                organization = row.get("teamLeaderOrganization") or ""
                if not isinstance(organization, str):
                    raise InvalidBoard("Tianchi row has an invalid organization")
                members[key] = {"key": key, "name": name or f"未公开队名（ID {team_id}）",
                                "name_source": "api" if name else "unavailable", "rank": position, "score": score,
                                "display_rank": row["rank"],
                                "display_score": display_score(score, config.get("leaderboardFormat")),
                                "organization": organization.strip(), "identity_basis": "team_id"}
        result["boards"][BOARD] = members
        result["total"] = total
        if not members:
            result.update(status="unavailable", reason="Tianchi leaderboard is empty; preserving the previous valid snapshot")
        return result
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, InvalidBoard):
            exc.diagnostics = location
            raise
        field = f" (missing field: {exc.args[0]})" if isinstance(exc, KeyError) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", str(exc.args[0])) else f" ({type(exc).__name__})"
        error = InvalidBoard(f"Invalid Tianchi leaderboard structure{field}; the snapshot was not compared")
        error.diagnostics = location
        raise error from exc
