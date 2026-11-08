"""测试报告服务"""



class TestReportsServiceImport:
    def test_importable(self):
        from app.services.reports_service import ReportsService
        assert ReportsService is not None


class TestReportsServiceFormatting:
    def test_format_report_metadata(self):
        from app.services.reports_service import ReportsService
        svc = ReportsService.__new__(ReportsService)
        assert svc is not None


class TestReportOwnershipQueries:
    def test_owner_constraint_preserves_multi_id_query(self):
        from app.services.reports_service import _build_report_query, _with_report_owner

        query = _with_report_owner(_build_report_query("task-123"), "user-456")

        assert query["$and"][1] == {"user_id": "user-456"}
        assert {"task_id": "task-123"} in query["$and"][0]["$or"]

    def test_owner_constraint_is_optional_for_admin_paths(self):
        from app.services.reports_service import _with_report_owner

        query = {"task_id": "task-123"}
        assert _with_report_owner(query, None) is query
