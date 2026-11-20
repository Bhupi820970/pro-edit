"""Buffer GraphQL API — post a video to the Instagram channel @benreelr.

Mirrors skywalker-shorts/scripts/buffer_post.py (Bearer key, createPost, shareNow)
but selects the channel at runtime by service username instead of a hardcoded id.
The video URL must be publicly reachable without auth (see transfer.py).
Env: BUFFER_API_KEY (never printed).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import secrets as S

API = "https://api.buffer.com"


class BufferError(RuntimeError):
    pass


def gql(query: str, variables: dict | None = None, api_key: str | None = None) -> dict:
    key = api_key or S.env("BUFFER_API_KEY")
    if not key:
        raise BufferError("BUFFER_API_KEY secret is not set")
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise BufferError(f"Buffer HTTP {e.code}: {S.redact(body)[:300]}") from None
    if d.get("errors"):
        msgs = "; ".join(S.redact(x.get("message", "?")) for x in d["errors"])
        raise BufferError("Buffer GraphQL error: " + msgs[:400])
    return d.get("data") or {}


def organizations(api_key: str | None = None) -> list[dict]:
    d = gql("query { account { organizations { id name } } }", api_key=api_key)
    return ((d.get("account") or {}).get("organizations")) or []


def list_channels(org_id: str, api_key: str | None = None) -> list[dict]:
    q = """query C($org: OrganizationId!) {
      channels(input: { organizationId: $org }) {
        id name displayName service externalLink isLocked isDisconnected
      }
    }"""
    d = gql(q, {"org": org_id}, api_key=api_key)
    return d.get("channels") or []


def _norm_user(s: str) -> str:
    return (s or "").strip().lstrip("@").lower()


def is_instagram_service(service: str | None) -> bool:
    s = (service or "").lower().replace("_", "").replace(" ", "")
    return "instagram" in s


def channel_matches(ch: dict, username: str = "benreelr") -> bool:
    if not is_instagram_service(ch.get("service")):
        return False
    want = _norm_user(username)
    for raw in (ch.get("name"), ch.get("displayName"), ch.get("externalLink")):
        if not raw:
            continue
        if _norm_user(str(raw)) == want:
            return True
        n = str(raw).lower().replace("www.", "")
        if f"instagram.com/{want}" in n:
            return True
    return False


def find_instagram_channel(username: str = "benreelr", api_key: str | None = None) -> dict:
    orgs = organizations(api_key=api_key)
    if not orgs:
        raise BufferError("Buffer account has no organizations")
    seen = []
    for org in orgs:
        for ch in list_channels(org["id"], api_key=api_key):
            seen.append(f"{ch.get('service')}:{ch.get('name') or ch.get('displayName')}")
            if channel_matches(ch, username):
                if ch.get("isLocked") or ch.get("isDisconnected"):
                    raise BufferError(
                        f"Instagram @{_norm_user(username)} is locked or disconnected on Buffer"
                    )
                return {**ch, "organizationId": org["id"], "organizationName": org.get("name")}
    raise BufferError(
        f"no Instagram channel @{_norm_user(username)} on this Buffer account "
        f"(saw: {', '.join(seen) or 'no channels'})"
    )


def create_video_post(channel_id: str, text: str, video_url: str,
                      share_now: bool = True, due_at_utc: str | None = None,
                      api_key: str | None = None) -> dict:
    m = """mutation C($input: CreatePostInput!) { createPost(input: $input) {
      ... on PostActionSuccess { post { id status dueAt sentAt externalLink
        error { message } } }
      ... on MutationError { message } } }"""
    inp = {
        "text": (text or "")[:2200],
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "shareNow" if share_now else "customScheduled",
        "assets": [{
            "video": {
                "url": video_url,
                "metadata": {"thumbnailOffset": 2000},
            }
        }],
        "source": "pro-edit",
    }
    if not share_now and due_at_utc:
        inp["dueAt"] = due_at_utc
    d = gql(m, {"input": inp}, api_key=api_key).get("createPost") or {}
    if not d.get("post"):
        raise BufferError("createPost: " + S.redact(str(d.get("message") or d))[:400])
    return d["post"]


def get_post(org_id: str, channel_id: str, post_id: str, api_key: str | None = None) -> dict | None:
    q = """query P($org: OrganizationId!, $ch: [ChannelId!]) {
      posts(first: 50, input: { organizationId: $org, filter: { channelIds: $ch } }) {
        edges { node { id status dueAt sentAt externalLink error { message } } } } }"""
    d = gql(q, {"org": org_id, "ch": [channel_id]}, api_key=api_key)
    edges = ((d.get("posts") or {}).get("edges")) or []
    for e in edges:
        node = e.get("node") or {}
        if node.get("id") == post_id:
            return node
    return None
