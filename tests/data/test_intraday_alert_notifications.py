import asyncio
from uuid import uuid4

import pytest

from app.models.notification import NotificationCreate
from app.services.notifications_service import NotificationsService


@pytest.mark.asyncio
async def test_concurrent_intraday_alert_is_owner_scoped_and_deduplicated(real_mongo_db):
    db = real_mongo_db
    users = ["alert-test-"+uuid4().hex for _ in range(2)]
    service = NotificationsService(db=db)
    try:
        first = NotificationCreate(user_id=users[0], type="alert", title="原目标触及", source="intraday_research")
        ids = await asyncio.gather(*(service.create_and_publish(first, dedupe_key="same-reference") for _ in range(8)))
        assert len(set(ids)) == 1
        assert await db.notifications.count_documents({"user_id": users[0]}) == 1
        second = NotificationCreate(user_id=users[1], type="alert", title="同一标的另一用户", source="intraday_research")
        other = await service.create_and_publish(second, dedupe_key="same-reference")
        assert other != ids[0]
        await service.create_and_publish(first, dedupe_key="different-target")
        assert await db.notifications.count_documents({"user_id": users[0]}) == 2
    finally:
        await db.notifications.delete_many({"user_id": {"$in": users}})
