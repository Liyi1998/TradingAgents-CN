"""财务数据仓储。"""

from app.data.storage.mongo.repositories.base import MongoBaseRepo

from typing import Dict, List, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from pymongo import UpdateOne

from app.data.storage.mongo.client import get_motor_db
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.bulk_utils import batched_bulk_write
from app.data.storage.mongo.repositories.key_spec import build_filter
from app.data.schema.base.types import _parse_date
from app.data.schema.domains.financial_data import (
    FINANCIAL_CONTRACT, FINANCIAL_VALUE_FIELDS, financial_date,
    financial_record_as_of,
)


_DISCLOSED_FIELDS = (*FINANCIAL_VALUE_FIELDS, "net_profit_basis", "announce_date")


class FinancialDataRepo(MongoBaseRepo):
    domain = "financial_data"
    range_sort_direction = -1


    async def upsert_many(self, records: List[Dict], market: str) -> int:
        if not records:
            return 0
        db = get_motor_db()
        coll = db[get_collection_name("financial_data", market)]
        ops = []
        for rec in records:
            if market == "CN":
                # A legacy writer must not borrow a preceding v1 snapshot's
                # dates/basis/evidence when it cannot supply its own.
                rec = {**dict.fromkeys(_DISCLOSED_FIELDS),
                       "financial_contract": None, "source_disclosures": {},
                       "field_provenance": {}, "announce_date": None,
                       "net_profit_basis": None, "currency": None,
                       "monetary_unit": None, **rec}
            if rec.get("financial_contract") == FINANCIAL_CONTRACT:
                # A complete contributor snapshot must not inherit old amounts
                # or dates that are absent from its new disclosure evidence.
                rec = {**dict.fromkeys(_DISCLOSED_FIELDS),
                       "source_disclosures": {}, "field_provenance": {},
                       "currency": None, "monetary_unit": None, **rec}
            try:
                filter_doc = build_filter("financial_data", rec)
            except KeyError:
                continue  # 缺少唯一键字段的记录跳过
            ops.append(UpdateOne(
                filter_doc,
                {"$set": rec},
                upsert=True,
            ))
        if not ops:
            return 0
        return await batched_bulk_write(coll, ops)

    async def get_by_symbol(
        self, symbol: str, market: str, statement_type: Optional[str] = None, limit: int = 20,
        start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> List[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("financial_data", market)]
        query = {"symbol": symbol}
        if market == "CN":
            start, end = financial_date(start_date), financial_date(end_date)
            if ((start_date is not None and not start)
                    or (end_date is not None and not end)
                    or (start and end and start > end)):
                raise ValueError("Invalid financial date bounds")
            start_date = start
            end_date = end or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        else:
            start_date, end_date = _parse_date(start_date), _parse_date(end_date)
        if statement_type:
            query["statement_type"] = statement_type
        if start_date or end_date:
            query["report_period"] = {}
            if start_date:
                query["report_period"]["$gte"] = start_date
            if end_date:
                query["report_period"]["$lte"] = end_date
                if market != "CN":
                    query["$or"] = [{"announce_date": {"$lte": end_date}}, {"announce_date": None}]
        if market != "CN":
            cursor = coll.find(query, {"_id": 0}).sort("report_period", -1).limit(limit)
            return await cursor.to_list(length=None)
        # Apply the limit after disclosure filtering: unknown newer reports must
        # not crowd a valid older report out, and one future contributor must not
        # hide already disclosed values in the same composite record.
        records = []
        async for raw in coll.find(query, {"_id": 0}).sort("report_period", -1):
            disclosed = financial_record_as_of(raw, end_date)
            if disclosed is not None:
                records.append(disclosed)
                if limit > 0 and len(records) >= limit:
                    break
        return records
