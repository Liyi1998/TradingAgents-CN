"""Read-only, owner-scoped history across adopted research baselines."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.engine.wyckoff_tracking import stamp, wyckoff_board


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def phase_timeline(db, track, baseline, version, phases):
    refs = track.get("baseline_history", [])
    chain = [*refs, {"analysis_id": track["baseline_analysis_id"]}]
    selected = next(i for i, ref in enumerate(chain) if ref["analysis_id"] == baseline["analysis_id"])
    adopted_at = refs[selected - 1].get("archived_at") if selected else track.get("created_at")
    cutoff = (version or {}).get("created_at") or adopted_at or baseline.get("created_at")
    result = {"history": [], "boundaries": [], "truncated": False, "cutoff_at": stamp(cutoff)}
    if not isinstance(cutoff, datetime):
        return result
    cutoff = aware(cutoff)
    # A selected daily snapshot must not see later evidence. A newly adopted
    # report without a daily version can still retain all judgments known at adoption.
    as_of = version["as_of"] if version else cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    previous = refs[:selected]
    archives = (
        await db["research_baselines"]
        .find(
            {
                "track_id": track["_id"],
                "user_id": track["user_id"],
                "_id": {"$in": [ref["archive_id"] for ref in previous]},
            }
        )
        .to_list(None)
    )
    by_archive = {row["_id"]: row["baseline"] for row in archives}
    origins = {}
    for i, ref in enumerate(chain[: selected + 1]):
        origin = baseline if i == selected else by_archive.get(ref.get("archive_id"))
        if not origin or origin["analysis_id"] != ref["analysis_id"]:
            continue
        known = origin.get("created_at")
        if not isinstance(known, datetime) or aware(known) > cutoff or origin["as_of"] > as_of:
            continue
        origins[origin["analysis_id"]] = origin
        activated = refs[i - 1].get("archived_at") if i else track.get("created_at")
        result["boundaries"].append(
            {
                "analysis_id": origin["analysis_id"],
                "as_of": origin["as_of"],
                "report_created_at": stamp(known),
                "activated_at": stamp(activated)
                if isinstance(activated, datetime) and aware(activated) <= cutoff
                else None,
                "selected": i == selected,
            }
        )
    ids = [key for key in origins if key != baseline["analysis_id"]]
    scope = [{"baseline_analysis_id": {"$in": ids}}]
    if chain[0]["analysis_id"] in ids:
        scope.append({"baseline_analysis_id": {"$exists": False}})
    rows = (
        await db["research_versions"]
        .find(
            {
                "track_id": track["_id"],
                "user_id": track["user_id"],
                "$or": scope,
                "created_at": {"$lte": cutoff},
                "as_of": {"$lte": as_of},
            },
            {
                "baseline_analysis_id": 1,
                "as_of": 1,
                "created_at": 1,
                "modules.technical": 1,
                "snapshot.as_of": 1,
                "snapshot.bars": 1,
                "snapshot.price": 1,
            },
        )
        .sort([("created_at", -1), ("_id", -1)])
        .limit(301)
        .to_list(301)
        if ids
        else []
    )
    result["truncated"] = len(rows) > 300
    grouped = {key: [] for key in ids}
    for row in rows[:300]:
        key = row.get("baseline_analysis_id") or chain[0]["analysis_id"]
        if key in grouped:
            grouped[key].append(row)
    for key in ids:
        result["history"].extend(wyckoff_board(origins[key], grouped[key], None)["history"])
    result["history"].extend(phases["history"])
    current = phases["current"]

    def identity(item):
        return item["source_analysis_id"], item["version_id"]

    result["history"] = [
        {
            **item,
            "display_role": "current" if current and identity(item) == identity(current) else "historical",
            "carried_forward": bool(
                current
                and identity(item) == identity(current)
                and phases["status"] in {"failed", "missing", "inherited"}
            ),
        }
        for item in result["history"]
    ]
    result["history"].sort(
        key=lambda item: (item["as_of"], item["known_at"] or "", item["source_analysis_id"], item["version_id"] or "")
    )
    return result
