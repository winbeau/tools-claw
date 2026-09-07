"""Provider routing shared by ranking subscriptions, storage and notifications."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from beauclaw import core, tianchi


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    primary_board: str
    boards: dict[str, str]
    requires_login: bool

    def url(self, event_id: str) -> str:
        if not re.fullmatch(r"\d+", str(event_id)):
            raise ValueError("Competition IDs must be numeric")
        if self.id == "tianchi":
            return tianchi.page_url(event_id)
        return f"https://competition.gitcode.com/competition/{event_id}/live-ranking"

    def parse(self, body: bytes, captured_at: str) -> dict:
        return (tianchi.parse_board if self.id == "tianchi" else core.parse_board)(body, captured_at)

    def fetch(self, event_id: str, auth: dict | None = None, timeout: float = 8) -> core.Response:
        return (tianchi.fetch if self.id == "tianchi" else core.fetch)(event_id, auth or {}, timeout)


PROVIDERS = {
    "gitcode": Provider("gitcode", "GitCode", "realtime_region_ranking", core.BOARDS, True),
    "tianchi": Provider("tianchi", "阿里云天池", tianchi.BOARD, {tianchi.BOARD: "天池排行榜"}, False),
}


def get_provider(name: str = "gitcode") -> Provider:
    if name not in PROVIDERS:
        raise ValueError("Unsupported ranking provider; supported providers: gitcode, tianchi")
    return PROVIDERS[name]


def parse_target(value: str) -> tuple[Provider, str]:
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return PROVIDERS["gitcode"], value
    url = urlsplit(value)
    if url.scheme == "https" and url.netloc == "competition.gitcode.com":
        match = re.fullmatch(r"/competition/(\d+)/live-ranking/?", url.path)
        if match:
            return PROVIDERS["gitcode"], match[1]
    if url.scheme == "https" and url.netloc == "tianchi.aliyun.com":
        match = re.fullmatch(r"/competition/entrance/(\d+)/rankingList/?", url.path)
        if match:
            return PROVIDERS["tianchi"], match[1]
    raise ValueError("Provide a GitCode live-ranking URL or an Aliyun Tianchi rankingList URL")
