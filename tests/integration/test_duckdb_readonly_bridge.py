"""Real DuckDB files and HTTP requests; no mocks, collectors or production data.

Run in the Miniconda environment with DuckDB installed. The TCP fixture binds an
ephemeral loopback port; all database files and lock holders belong to tmp_path.
"""

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb
import httpx
import pytest
import uvicorn
from fastapi import HTTPException

from app.data.sources.cn.duckdb.adapter import JinceDuckDBCNAdapter
from app.data.sources.cn.duckdb.provider import JinceDuckDBCNProvider
from scripts.duckdb_readonly_bridge.server import READ_CONFIG, create_app, open_read_only

pytestmark = pytest.mark.integration
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "market.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("""
            CREATE TABLE dat_day (
                code VARCHAR, trade_time TIMESTAMPTZ, open DOUBLE, high DOUBLE,
                low DOUBLE, close DOUBLE, vol BIGINT, amount DOUBLE
            )
        """)
        rows = []
        for symbol, initial in (("000001.SZ", 10), ("600001.SH", 20)):
            for offset in range(3):
                close = initial + offset
                rows.append((symbol, datetime(2026, 9, 1 + offset, tzinfo=SHANGHAI), close - 0.5,
                             close + 0.5, close - 1, close,
                             100 + offset * 10, 100000 + offset * 1000))
        # More than the provider's minimum page size (100), including a page
        # boundary inside one stock, so a real provider call must paginate.
        for offset in range(120):
            close = 30 + offset
            rows.append(("300001.SZ", datetime(2026, 5, 1, tzinfo=SHANGHAI) + timedelta(days=offset),
                         close - 0.5, close + 0.5, close - 1, close, 200, 200000))
        connection.executemany("INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.execute("CREATE TABLE unrelated_private_table (secret VARCHAR)")
        connection.execute("INSERT INTO unrelated_private_table VALUES ('not-published')")
    return path


@pytest.fixture
async def client(database):
    app = create_app(database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def tcp_url(database):
    app = create_app(database)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, loop="asyncio", lifespan="off",
        access_log=False, log_level="error",
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert server.started, "temporary loopback bridge did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        listener.close()
        assert not thread.is_alive(), "temporary bridge thread did not stop"


async def test_coverage_and_paginated_daily_quotes_preserve_page_first_preclose(client):
    coverage = await client.get("/api/duckdb/coverage")
    assert coverage.status_code == 200
    assert coverage.json()["status"] == "success"
    daily = next(table for table in coverage.json()["tables"] if table["interval"] == "D")
    assert daily["status"] == "success"
    assert daily["min_date"] == "2026-05-01" and daily["max_date"] == "2026-09-03"

    params = {"start_date": "2026-09-02", "end_date": "2026-09-03", "limit": 1}
    records, cursors = [], set()
    for _ in range(10):
        response = await client.get("/api/duckdb/daily_quotes", params=params)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "success" and payload["volume_unit"] == "lot"
        assert len(payload["records"]) <= 1
        records.extend(payload["records"])
        if not payload["has_more"]:
            break
        cursor = payload["next_cursor"]
        pair = (cursor["code"], cursor["trade_date"])
        assert pair not in cursors
        cursors.add(pair)
        params.update(cursor_code=pair[0], cursor_date=pair[1])
    else:
        pytest.fail("daily quote pagination did not terminate")
    assert len(records) == 4
    assert len({(row["code"], row["trade_date"]) for row in records}) == 4
    for row in records:
        expected_close = (10 if row["code"].startswith("000001") else 20)
        expected_close += int(row["trade_date"][-2:]) - 1
        assert row["close"] == expected_close
        # Both the range's first day and every page boundary keep the previous
        # source row, including the predecessor outside the requested range.
        assert row["pre_close"] == expected_close - 1
        assert row["change"] == 1
        assert row["pct_chg"] == pytest.approx(100 / (expected_close - 1))


async def test_real_provider_single_symbol_and_full_market_contract(tcp_url):
    provider = JinceDuckDBCNProvider()
    provider.base_url = tcp_url
    provider.timeout = 5
    assert await provider.connect()
    assert (await provider.get_coverage())["status"] == "success"

    single = await provider.get_daily_quotes("000001", "2026-09-02", "2026-09-03")
    assert single["trade_date"].tolist() == ["2026-09-02", "2026-09-03"]
    assert single["pre_close"].tolist() == [10, 11]
    assert single["volume"].tolist() == [11000, 12000]
    adapted = JinceDuckDBCNAdapter().adapt_daily_quotes(single)
    assert [row.symbol for row in adapted] == ["000001", "000001"]
    assert adapted[0].pre_close == 10

    pages = [page async for page in provider.iter_daily_quotes(
        "2026-01-01", "2026-09-03", batch_size=100)]
    assert len(pages) >= 2
    assert sum(len(page) for page in pages) == 126
    all_keys = [(row["symbol"], row["trade_date"])
                for page in pages for row in page.to_dict("records")]
    assert len(all_keys) == len(set(all_keys))
    for page in pages[1:]:
        first = page.iloc[0]
        if str(first["symbol"]).startswith("300001"):
            assert first["pre_close"] == first["close"] - 1


async def test_single_symbol_http_contract_and_absent_stock(client):
    response = await client.get("/api/backtest/kline_data", params={
        "stock": "000001.SZ", "start": "2026-09-01", "end": "2026-09-03"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert [row["time"] for row in payload["candles"]] == [
        "2026-09-01", "2026-09-02", "2026-09-03"]
    assert [row["value"] for row in payload["volumes"]] == [100, 110, 120]
    empty = await client.get("/api/backtest/kline_data", params={
        "stock": "123456.SZ", "start": "2026-09-01", "end": "2026-09-03"})
    assert empty.status_code == 200
    assert empty.json()["candles"] == []


@pytest.mark.parametrize("path,params,records_key", [
    ("/api/duckdb/daily_quotes", {
        "start_date": "0001-01-01", "end_date": "0001-01-02"}, "records"),
    ("/api/backtest/kline_data", {
        "stock": "000001.SZ", "start": "0001-01-01", "end": "0001-01-02"}, "candles"),
])
async def test_date_min_range_does_not_underflow_lookback(client, database, path, params, records_key):
    before = _hash(database)
    response = await client.get(path, params=params)
    assert response.status_code == 200
    assert response.json()[records_key] == []
    assert _hash(database) == before


@pytest.mark.parametrize("path,params", [
    ("/api/duckdb/daily_quotes", {"start_date": "2026-02-30", "end_date": "2026-09-03"}),
    ("/api/duckdb/daily_quotes", {"start_date": "20260901", "end_date": "2026-09-03"}),
    ("/api/duckdb/daily_quotes", {"start_date": "2026-09-03", "end_date": "2026-09-01"}),
    ("/api/duckdb/daily_quotes", {"start_date": "2026-09-01", "end_date": "2026-09-03", "limit": 0}),
    ("/api/duckdb/daily_quotes", {"start_date": "2026-09-01", "end_date": "2026-09-03", "limit": 50001}),
    ("/api/duckdb/daily_quotes", {"start_date": "2026-09-01", "end_date": "2026-09-03", "cursor_code": "000001.SZ"}),
    ("/api/duckdb/daily_quotes", {"start_date": "2026-09-01", "end_date": "2026-09-03", "cursor_date": "2026-09-02"}),
    ("/api/duckdb/daily_quotes", {"start_date": "2026-09-01", "end_date": "2026-09-03", "cursor_code": "000001.SZ", "cursor_date": "2026-09-04"}),
    ("/api/duckdb/daily_quotes", {"start_date": "2000-01-01", "end_date": "2026-09-03"}),
    ("/api/duckdb/daily_quotes", {"start_date": "2026-09-01", "end_date": "2026-09-03", "cursor_code": "x' OR 1=1--", "cursor_date": "2026-09-02"}),
    ("/api/backtest/kline_data", {"stock": "000001.SZ'; DROP TABLE dat_day;--", "start": "2026-09-01", "end": "2026-09-03"}),
    ("/api/backtest/kline_data", {"stock": "000001.SZ", "start": "invalid", "end": "2026-09-03"}),
])
async def test_invalid_input_is_rejected_without_database_changes(client, database, path, params):
    before = _hash(database)
    response = await client.get(path, params=params)
    assert response.status_code == 422
    assert _hash(database) == before


async def test_only_allowlisted_get_routes_and_no_write_api(client, database):
    before = _hash(database)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = await client.request(method, "/api/duckdb/coverage", json={"sql": "DROP TABLE dat_day"})
        assert response.status_code == 405
    for path in ("/api/duckdb/sql", "/api/duckdb/query", "/api/collect", "/api/scheduler/start",
                 "/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404
    assert _hash(database) == before


@pytest.mark.parametrize("path,params", [
    ("/api/duckdb/coverage", [("sql", "SELECT * FROM unrelated_private_table")]),
    ("/api/duckdb/daily_quotes", [
        ("start_date", "2026-09-01"), ("end_date", "2026-09-03"),
        ("table", "unrelated_private_table")]),
    ("/api/duckdb/daily_quotes", [
        ("start_date", "2026-09-01"), ("start_date", "2026-09-02"),
        ("end_date", "2026-09-03")]),
    ("/api/backtest/kline_data", [
        ("stock", "000001.SZ"), ("start", "2026-09-01"),
        ("end", "2026-09-03"), ("stock", "600001.SH")]),
])
async def test_unknown_or_repeated_parameters_are_rejected(client, database, path, params):
    before = _hash(database)
    response = await client.get(path, params=params)
    assert response.status_code == 422
    assert "not-published" not in response.text
    assert _hash(database) == before


def test_application_exposes_only_three_get_routes(database):
    routes = {(route.path, method) for route in create_app(database).routes
              for method in (getattr(route, "methods", None) or [])}
    assert routes == {(path, "GET") for path in (
        "/api/duckdb/coverage", "/api/duckdb/daily_quotes", "/api/backtest/kline_data")}


def test_real_readonly_connection_enforces_safety_settings(database):
    before = _hash(database)
    with open_read_only(database) as connection:
        access_mode, external_access, timezone_name, temp_directory, temp_limit = connection.execute("""
            SELECT current_setting('access_mode'), current_setting('enable_external_access'),
                   current_setting('TimeZone'), current_setting('temp_directory'),
                   current_setting('max_temp_directory_size')
        """).fetchone()
    assert access_mode.lower() == "read_only"
    assert external_access is False
    assert timezone_name == "Asia/Shanghai"
    assert temp_directory == ""
    # DuckDB exposes size settings as a human-readable string in current wheels.
    assert temp_limit in (0, "0 bytes", "0B")
    assert _hash(database) == before


async def test_bridge_uses_readonly_connections_and_closes_each_request(client, database):
    before = _hash(database)
    # Mixing RO and RW connections to one DuckDB file in the same process is
    # rejected by DuckDB. A successful bridge read here verifies its RO mode.
    with duckdb.connect(str(database), read_only=True, config=READ_CONFIG) as reader:
        with pytest.raises(duckdb.Error):
            reader.execute("INSERT INTO dat_day (code) VALUES ('write-must-fail')")
        assert (await client.get("/api/duckdb/coverage")).status_code == 200
        assert (await client.get("/api/backtest/kline_data", params={
            "stock": "000001.SZ", "start": "2026-09-01", "end": "2026-09-03"})).status_code == 200
    # A RW connection is possible immediately afterwards only when all bridge
    # request-scoped RO connections have been closed. It performs SELECT only.
    with duckdb.connect(str(database), read_only=False) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dat_day").fetchone()[0] == 126
    assert _hash(database) == before


async def test_real_writer_lock_returns_503_then_recovers(client, database):
    before = _hash(database)
    program = """
import duckdb, sys
connection = duckdb.connect(sys.argv[1], read_only=False)
print('LOCK_READY', flush=True)
sys.stdin.readline()
connection.close()
"""
    holder = subprocess.Popen([sys.executable, "-u", "-c", program, str(database)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        line = await asyncio.wait_for(asyncio.to_thread(holder.stdout.readline), timeout=15)
        assert line.strip() == "LOCK_READY", "writer failed to acquire the temporary database"
        for path, params in (("/api/duckdb/coverage", {}),
                             ("/api/duckdb/daily_quotes", {"start_date": "2026-09-01", "end_date": "2026-09-03"}),
                             ("/api/backtest/kline_data", {"stock": "000001.SZ", "start": "2026-09-01", "end": "2026-09-03"})):
            response = await client.get(path, params=params)
            assert response.status_code == 503
    finally:
        try:
            await asyncio.to_thread(holder.communicate, "\n", timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            await asyncio.to_thread(holder.communicate, timeout=5)
    assert holder.returncode == 0
    assert (await client.get("/api/duckdb/coverage")).status_code == 200
    assert _hash(database) == before


async def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.duckdb"
    app = create_app(missing)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/api/duckdb/coverage")
        assert response.status_code == 503
    assert not missing.exists()
    assert not list(tmp_path.glob("*.wal"))


async def test_non_finite_numbers_are_json_null_not_invalid_json(client, database):
    with duckdb.connect(str(database)) as connection:
        connection.execute("INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           ["000009.SZ", datetime(2026, 9, 3, tzinfo=SHANGHAI), float("nan"), float("inf"),
                            -float("inf"), float("nan"), None, float("nan")])
    response = await client.get("/api/duckdb/daily_quotes", params={
        "start_date": "2026-09-03", "end_date": "2026-09-03"})
    assert response.status_code == 200

    def reject_nonfinite(value):
        raise AssertionError(f"non-finite token in JSON: {value}")

    payload = json.loads(response.text, parse_constant=reject_nonfinite)
    row = next(row for row in payload["records"] if row["code"].startswith("000009"))
    assert all(row[field] is None for field in ("open", "high", "low", "close", "vol", "amount"))


async def test_shanghai_session_date_not_utc_date_and_bounded_predecessor(client, database):
    with duckdb.connect(str(database)) as connection:
        connection.executemany("INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("000010.SZ", datetime(2026, 8, 31, 20, tzinfo=timezone.utc), 29, 30, 28, 29, 100, 290000),
            ("000010.SZ", datetime(2026, 9, 1, 20, tzinfo=timezone.utc), 30, 31, 29, 30, 100, 300000),
            ("000001.SZ", datetime(2026, 9, 20, tzinfo=SHANGHAI), 13, 14, 12, 13, 100, 130000),
        ])
    response = await client.get("/api/duckdb/daily_quotes", params={
        "start_date": "2026-09-02", "end_date": "2026-09-02"})
    assert response.status_code == 200
    row = next(row for row in response.json()["records"] if row["code"].startswith("000010"))
    assert row["trade_date"] == "2026-09-02" and row["pre_close"] == 29
    distant = await client.get("/api/duckdb/daily_quotes", params={
        "start_date": "2026-09-20", "end_date": "2026-09-20"})
    assert distant.status_code == 200
    row = next(row for row in distant.json()["records"] if row["code"].startswith("000001"))
    assert row["pre_close"] is None


async def test_duplicate_timestamps_with_rounding_only_collapse_once(client, database):
    with duckdb.connect(str(database)) as connection:
        connection.execute("INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            "000001.SZ", datetime(2026, 9, 2, 8, tzinfo=SHANGHAI),
            10.5, 11.5, 10, 11, 110, 101000.00001,
        ])
    for path, params, records_key in (
        ("/api/duckdb/daily_quotes", {"start_date": "2026-09-02", "end_date": "2026-09-03"}, "records"),
        ("/api/backtest/kline_data", {"stock": "000001.SZ", "start": "2026-09-02", "end": "2026-09-03"}, "candles"),
    ):
        response = await client.get(path, params=params)
        assert response.status_code == 200
        rows = response.json()[records_key]
        if records_key == "records":
            rows = [row for row in rows if row["code"].startswith("000001")]
            assert rows[0]["pre_close"] == 10 and rows[1]["pre_close"] == 11
            assert rows[0]["duplicate_count"] == 2
            assert rows[0]["amount"] == 101000  # Keep the earlier timestamp.
        assert len(rows) == 2


async def test_materially_conflicting_duplicates_fail_closed(client, database):
    with duckdb.connect(str(database)) as connection:
        connection.execute("INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            "000001.SZ", datetime(2026, 9, 2, 8, tzinfo=SHANGHAI),
            10.5, 11.5, 10, 99, 110, 101000,
        ])
    before = _hash(database)
    for path, params in (
        ("/api/duckdb/daily_quotes", {"start_date": "2026-09-02", "end_date": "2026-09-03"}),
        ("/api/backtest/kline_data", {"stock": "000001.SZ", "start": "2026-09-02", "end": "2026-09-03"}),
    ):
        assert (await client.get(path, params=params)).status_code == 503
    assert _hash(database) == before


async def test_mixed_volume_units_are_normalized_or_explicitly_unknown(client, database):
    with duckdb.connect(str(database)) as connection:
        connection.executemany("INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("000011.SZ", datetime(2026, 9, 3, tzinfo=SHANGHAI), 10, 11, 9, 10, 1000000, 10000000),
            ("000012.SZ", datetime(2026, 9, 3, tzinfo=SHANGHAI), 10, 11, 9, 10, 10000, 10000000),
            ("000013.SZ", datetime(2026, 9, 3, tzinfo=SHANGHAI), 10, 11, 9, 10, 10000, 1000),
            ("000014.SZ", datetime(2026, 9, 3, tzinfo=SHANGHAI), 10, 11, 9, 10, 0, 0),
        ])
    response = await client.get("/api/duckdb/daily_quotes", params={
        "start_date": "2026-09-03", "end_date": "2026-09-03"})
    assert response.status_code == 200 and response.json()["volume_unit"] == "lot"
    rows = {row["code"].split(".")[0]: row for row in response.json()["records"]}
    assert rows["000011"]["vol"] == 10000
    assert rows["000011"]["volume_unit_status"] == "inferred_share_to_lot"
    assert rows["000012"]["vol"] == 10000
    assert rows["000012"]["volume_unit_status"] == "inferred_lot"
    assert rows["000013"]["vol"] is None
    assert rows["000013"]["volume_unit_status"] == "unknown"
    assert rows["000014"]["vol"] == 0
    assert rows["000014"]["volume_unit_status"] == "zero_turnover"
    for symbol, volume in (("000011.SZ", 10000), ("000012.SZ", 10000), ("000013.SZ", None), ("000014.SZ", 0)):
        single = await client.get("/api/backtest/kline_data", params={
            "stock": symbol, "start": "2026-09-03", "end": "2026-09-03"})
        assert single.status_code == 200
        assert single.json()["volumes"][0]["value"] == volume


async def test_query_deadline_returns_503_and_database_remains_readable(database):
    before = _hash(database)
    # Real elapsed time between queries must fail closed even if the timer's
    # interrupt happened while DuckDB was idle. No clock/connection mocking.
    with pytest.raises(HTTPException) as caught:
        with open_read_only(database, query_timeout=0.2) as connection:
            connection.execute("SELECT COUNT(*) FROM main.dat_day").fetchone()
            await asyncio.sleep(0.25)
            connection.execute("SELECT COUNT(*) FROM main.dat_day").fetchone()
    assert caught.value.status_code == 503
    recovery = create_app(database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=recovery), base_url="http://testserver") as client:
        assert (await client.get("/api/duckdb/coverage")).status_code == 200
    assert _hash(database) == before
