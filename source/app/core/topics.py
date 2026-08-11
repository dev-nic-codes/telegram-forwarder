from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from telethon import functions


@dataclass
class TopicInfo:
    topic_id: int
    title: str
    top_message: int


async def fetch_forum_topics(client, chat_id: int, limit: int = 100, query: Optional[str] = None) -> List[TopicInfo]:
    """
    Fetch forum topics for a forum-enabled supergroup.

    Telethon exposes this as functions.messages.GetForumTopicsRequest
    (not in telethon.tl.functions.channels).
    """
    entity = await client.get_entity(chat_id)

    res = await client(
        functions.messages.GetForumTopicsRequest(
            peer=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=limit,
            q=query,
        )
    )

    topics: List[TopicInfo] = []
    for t in getattr(res, "topics", []):
        topics.append(
            TopicInfo(
                topic_id=int(getattr(t, "id", 0)),
                title=str(getattr(t, "title", "")),
                top_message=int(getattr(t, "top_message", 0)),
            )
        )

    topics.sort(key=lambda x: x.title.lower())
    return topics
