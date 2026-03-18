"""
Tests for EPP Print Server.
Mocks win32print, pystray, and other Windows-only deps so tests run on any OS.
"""
import sys
import os
import json
import types
import tempfile
import shutil
import threading
import logging
import pytest

# ── Mock Windows-only modules before importing epp ──────────────────────────

# win32print mock
win32print_mock = types.ModuleType("win32print")
win32print_mock.PRINTER_ENUM_CONNECTIONS = 0x4
win32print_mock.PRINTER_ENUM_LOCAL = 0x2
win32print_mock.EnumPrinters = lambda flags: [
    (None, None, "TestPrinter1", None),
    (None, None, "TestPrinter2", None),
]
win32print_mock.OpenPrinter = lambda name: 1  # fake handle
win32print_mock.StartDocPrinter = lambda h, level, doc_info: 1
win32print_mock.StartPagePrinter = lambda h: None
win32print_mock.WritePrinter = lambda h, data: len(data)
win32print_mock.EndPagePrinter = lambda h: None
win32print_mock.EndDocPrinter = lambda h: None
win32print_mock.ClosePrinter = lambda h: None
sys.modules["win32print"] = win32print_mock

# pystray mock
pystray_mock = types.ModuleType("pystray")
pystray_mock.MenuItem = lambda label, action: None
pystray_mock.Menu = lambda *items: None
pystray_mock.Icon = lambda *a, **kw: types.SimpleNamespace(run=lambda: None, stop=lambda: None)
sys.modules["pystray"] = pystray_mock

# waitress mock
waitress_mock = types.ModuleType("waitress")
waitress_mock.serve = lambda app, **kw: None
sys.modules["waitress"] = waitress_mock

# Set COMPUTERNAME for Linux compat
os.environ.setdefault("COMPUTERNAME", "TESTPC")
os.environ.setdefault("APPDATA", tempfile.gettempdir())

# ── Now import the app ──────────────────────────────────────────────────────

import epp


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Run each test with its own config/history files in a temp dir."""
    config_file = str(tmp_path / "conf.json")
    history_file = str(tmp_path / "print_history.json")
    log_file = str(tmp_path / "server_log.txt")

    monkeypatch.setattr(epp, "CONFIG_FILE", config_file)
    monkeypatch.setattr(epp, "PRINT_HISTORY_FILE", history_file)
    monkeypatch.setattr(epp, "LOG_FILE", log_file)

    # Reset shared state
    monkeypatch.setitem(epp.status, "total_jobs", 0)
    monkeypatch.setitem(epp.status, "last_request", None)

    yield tmp_path


@pytest.fixture
def client():
    """Flask test client."""
    epp.app.config["TESTING"] = True
    with epp.app.test_client() as c:
        yield c


@pytest.fixture
def sample_config(tmp_path):
    """Write a valid config and return the dict."""
    cfg = {
        "DEFAULT": "TestPrinter1",
        "PRINTER_NAME": r"\\TESTPC\TestPrinter1",
        "PORT": 9100,
        "FLASK_PORT": 5000,
        "MAX_REPRINT": 3,
    }
    with open(epp.CONFIG_FILE, "w") as f:
        json.dump(cfg, f)
    return cfg


@pytest.fixture
def sample_history():
    """Write sample print history and return the list."""
    jobs = [
        {
            "id": 1,
            "printer": r"\\TESTPC\TestPrinter1",
            "timestamp": "2025-01-01 12:00:00.000",
            "size": 10,
            "raw_data": b"Hello test".hex(),
            "print_count": 0,
        },
        {
            "id": 2,
            "printer": r"\\TESTPC\TestPrinter1",
            "timestamp": "2025-01-01 12:05:00.000",
            "size": 5,
            "raw_data": b"Job 2".hex(),
            "print_count": 2,
        },
    ]
    with open(epp.PRINT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs, f)
    return jobs


# ════════════════════════════════════════════════════════════════════════════
# Phase 1: Critical Bug Fix Tests
# ════════════════════════════════════════════════════════════════════════════

class TestBugFixes:

    # 1.1 — job_found initialized, no NameError
    def test_reprint_job_not_found(self, sample_config):
        """send_to_printer with nonexistent job_id should return error, not crash."""
        epp.save_print_history([])
        result = epp.send_to_printer(b"data", job_id=999)
        assert result["status"] is False
        assert "not found" in result["message"].lower()

    # 1.2 — restart route executes restart logic (thread starts before return)
    def test_restart_route_returns_html(self, client, sample_config, monkeypatch):
        """GET /restart should return the restart template (not dead code)."""
        # Prevent actual os.execl from running
        monkeypatch.setattr(os, "execl", lambda *a: None)
        resp = client.get("/restart")
        assert resp.status_code == 200
        assert b"restart" in resp.data.lower()

    # 1.3 — reprint counter actually increments
    def test_reprint_increments_counter(self, sample_config, sample_history):
        """Reprinting should increment print_count from 0 to 1."""
        result = epp.send_to_printer(b"Hello test", job_id=1)
        assert result["status"] is True

        history = epp.load_print_history()
        job = next(j for j in history if j["id"] == 1)
        assert job["print_count"] == 1

    # 1.4 — MAX_REPRINT compared as int
    def test_max_reprint_string_in_config(self, tmp_path):
        """MAX_REPRINT stored as string in config should still work."""
        cfg = {
            "DEFAULT": "TestPrinter1",
            "PRINTER_NAME": r"\\TESTPC\TestPrinter1",
            "PORT": 9100,
            "FLASK_PORT": 5000,
            "MAX_REPRINT": "3",  # string on purpose
        }
        with open(epp.CONFIG_FILE, "w") as f:
            json.dump(cfg, f)

        jobs = [{
            "id": 1, "printer": "x", "timestamp": "t",
            "size": 4, "raw_data": b"test".hex(), "print_count": 3,
        }]
        epp.save_print_history(jobs)

        result = epp.send_to_printer(b"test", job_id=1)
        assert result["status"] is False
        assert "max reprint" in result["message"].lower()

    # 1.5 — printer handle closed on exception
    def test_printer_handle_closed_on_error(self, sample_config, monkeypatch):
        """ClosePrinter should be called even if WritePrinter raises."""
        closed = {"called": False}

        def mock_write(h, data):
            raise RuntimeError("write failed")

        def mock_close(h):
            closed["called"] = True

        monkeypatch.setattr(win32print_mock, "WritePrinter", mock_write)
        monkeypatch.setattr(win32print_mock, "ClosePrinter", mock_close)

        result = epp.send_to_printer(b"data")
        assert result["status"] is False
        assert closed["called"] is True

        # restore
        monkeypatch.setattr(win32print_mock, "WritePrinter", lambda h, d: len(d))
        monkeypatch.setattr(win32print_mock, "ClosePrinter", lambda h: None)


# ════════════════════════════════════════════════════════════════════════════
# Phase 2: Security & Safety Tests
# ════════════════════════════════════════════════════════════════════════════

class TestSecurity:

    # 2.1 — thread safety (smoke test: concurrent sends don't crash)
    def test_concurrent_sends_no_crash(self, sample_config):
        """Multiple threads calling send_to_printer should not crash."""
        errors = []

        def do_send():
            try:
                epp.send_to_printer(b"concurrent data")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_send) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    # 2.2 — raw data not logged (check log messages via caplog)
    def test_no_raw_data_in_log(self, sample_config, caplog):
        """After printing, logs should contain byte count, not raw data."""
        test_data = b"SENSITIVE_RECEIPT_DATA_12345"
        with caplog.at_level(logging.INFO):
            epp.send_to_printer(test_data)

        all_messages = " ".join(caplog.messages)
        assert "SENSITIVE_RECEIPT_DATA_12345" not in all_messages
        assert "bytes" in all_messages

    # 2.4 — XSS: clean_log_text no longer produces <br>
    def test_clean_log_no_html(self):
        """clean_log_text should not inject HTML tags."""
        result = epp.clean_log_text("line1\nline2\x1b@stuff")
        assert "<br>" not in result
        assert "<script>" not in result

    # 2.5 — config validation: invalid port returns error
    def test_config_invalid_port(self, client, sample_config):
        """POST with non-numeric port should show error, not crash."""
        resp = client.post("/", data={
            "default_printer": "TestPrinter1",
            "port": "not_a_number",
            "max_reprint": "3",
        })
        assert resp.status_code == 200
        assert b"angka" in resp.data  # error message in Indonesian

    def test_config_invalid_max_reprint(self, client, sample_config):
        """POST with non-numeric max_reprint should show error."""
        resp = client.post("/", data={
            "default_printer": "TestPrinter1",
            "port": "9100",
            "max_reprint": "abc",
        })
        assert resp.status_code == 200
        assert b"angka" in resp.data


# ════════════════════════════════════════════════════════════════════════════
# Phase 3: Code Quality Tests
# ════════════════════════════════════════════════════════════════════════════

class TestCodeQuality:

    # 3.1 — read_log handles emojis without garbling
    def test_read_log_preserves_emojis(self, tmp_path):
        """Emojis in log file should survive read_log without garbling."""
        log_line = "2025-01-01 - INFO - 🚀 Print server running\n"
        with open(epp.LOG_FILE, "w", encoding="utf-8") as f:
            f.write(log_line)

        logs = epp.read_log()
        assert len(logs) == 1
        assert "🚀" in logs[0]

    # 3.1 — read_log with ESC/POS escape sequences
    def test_read_log_strips_escpos(self, tmp_path):
        """ESC/POS escape sequences should be stripped from log output."""
        with open(epp.LOG_FILE, "w", encoding="utf-8") as f:
            f.write("2025-01-01 - INFO - \x1b@Hello\x1dWworld\n")

        logs = epp.read_log()
        assert "\x1b" not in logs[0]
        assert "\x1d" not in logs[0]

    # 3.3 — hexToString null guard is a JS test; we test the Python view endpoint
    def test_view_job_not_found(self, client, sample_config):
        """GET /view/999 should return 404 JSON."""
        epp.save_print_history([])
        resp = client.get("/view/999")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["status"] == "error"


# ════════════════════════════════════════════════════════════════════════════
# Phase 4: New Feature Tests
# ════════════════════════════════════════════════════════════════════════════

class TestNewFeatures:

    # 4.1 — Health check endpoint
    def test_health_endpoint(self, client, sample_config):
        """GET /health should return JSON with expected keys."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "total_jobs" in data
        assert "last_request" in data
        assert "printer" in data
        assert "port" in data

    def test_health_reflects_jobs(self, client, sample_config):
        """Health endpoint should reflect total_jobs after printing."""
        epp.send_to_printer(b"test data")
        resp = client.get("/health")
        data = resp.get_json()
        assert data["total_jobs"] >= 1
        assert data["last_request"] is not None

    # 4.2 — Delete single job
    def test_delete_job(self, client, sample_config, sample_history):
        """POST /history/delete/1 should remove job 1."""
        resp = client.post("/history/delete/1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

        history = epp.load_print_history()
        ids = [j["id"] for j in history]
        assert 1 not in ids
        assert 2 in ids

    def test_delete_nonexistent_job(self, client, sample_config, sample_history):
        """Deleting a nonexistent job should succeed (no-op)."""
        resp = client.post("/history/delete/999")
        assert resp.status_code == 200

    # 4.2 — Clear all history
    def test_clear_history(self, client, sample_config, sample_history):
        """POST /history/clear should empty the history."""
        resp = client.post("/history/clear")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

        history = epp.load_print_history()
        assert history == []


# ════════════════════════════════════════════════════════════════════════════
# Dashboard / Route Tests
# ════════════════════════════════════════════════════════════════════════════

class TestDashboard:

    def test_dashboard_get(self, client, sample_config, sample_history):
        """GET / should return 200 with dashboard content."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"EPP" in resp.data

    def test_dashboard_shows_history(self, client, sample_config, sample_history):
        """Dashboard should render history rows."""
        resp = client.get("/")
        assert b"TestPrinter1" in resp.data
        # Should have the print_count displayed
        assert b"Times" in resp.data

    def test_reprint_success(self, client, sample_config, sample_history):
        """POST /reprint/1 should succeed for a valid job."""
        resp = client.post("/reprint/1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_reprint_not_found(self, client, sample_config):
        """POST /reprint/999 with empty history should return 404."""
        epp.save_print_history([])
        resp = client.post("/reprint/999")
        assert resp.status_code == 404

    def test_reprint_max_reached(self, client, sample_config, sample_history):
        """POST /reprint/2 where print_count=2 and MAX_REPRINT=3 should succeed,
        but a 4th attempt (count=3) should fail."""
        # job 2 has print_count=2, MAX_REPRINT=3 → one more allowed
        resp = client.post("/reprint/2")
        assert resp.status_code == 200

        # Now count=3, another attempt should fail
        resp = client.post("/reprint/2")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "max reprint" in data["message"].lower()

    def test_view_job_success(self, client, sample_config, sample_history):
        """GET /view/1 should return raw_data hex."""
        resp = client.get("/view/1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["raw_data"] == b"Hello test".hex()


# ════════════════════════════════════════════════════════════════════════════
# Utility Function Tests
# ════════════════════════════════════════════════════════════════════════════

class TestUtilities:

    def test_add_reprint_mark(self):
        """add_reprint_mark should prepend ESC/POS header with count."""
        data = b"original"
        result = epp.add_reprint_mark(data, 2)
        assert b"*** REPRINT (2) ***" in result
        assert result.endswith(b"original")

    def test_load_config_creates_default(self):
        """load_config should create conf.json with defaults if missing."""
        config = epp.load_config()
        assert "DEFAULT" in config
        assert "PORT" in config
        assert os.path.exists(epp.CONFIG_FILE)

    def test_save_and_load_config(self):
        """Round-trip config save/load."""
        cfg = {"DEFAULT": "X", "PRINTER_NAME": "Y", "PORT": 1234, "FLASK_PORT": 5000, "MAX_REPRINT": 5}
        epp.save_config(cfg)
        loaded = epp.load_config()
        assert loaded == cfg

    def test_load_print_history_creates_empty(self):
        """load_print_history should create empty file if missing."""
        history = epp.load_print_history()
        assert history == []
        assert os.path.exists(epp.PRINT_HISTORY_FILE)

    def test_save_and_load_history(self):
        """Round-trip history save/load."""
        jobs = [{"id": 1, "data": "test"}]
        epp.save_print_history(jobs)
        loaded = epp.load_print_history()
        assert loaded == jobs

    def test_clean_log_text_strips_escape(self):
        """clean_log_text should remove ESC/POS sequences."""
        # \x1b[@\w]* matches \x1b followed by @ and word chars
        assert "\x1b" not in epp.clean_log_text("\x1b@Hello")
        assert "\x1d" not in epp.clean_log_text("\x1dW test")
        # Standalone ESC followed by non-word chars
        assert epp.clean_log_text("\x1b test") == "test"

    def test_clean_log_text_plain(self):
        """clean_log_text should leave normal text intact."""
        assert epp.clean_log_text("normal log line") == "normal log line"

    def test_read_log_empty_file(self, tmp_path):
        """read_log with missing file should return empty list."""
        # LOG_FILE points to tmp_path which doesn't have the file yet
        if os.path.exists(epp.LOG_FILE):
            os.remove(epp.LOG_FILE)
        assert epp.read_log() == []

    def test_file_is_same(self, tmp_path):
        """file_is_same should compare by size."""
        f1 = str(tmp_path / "a.txt")
        f2 = str(tmp_path / "b.txt")
        with open(f1, "w") as f:
            f.write("hello")
        with open(f2, "w") as f:
            f.write("hello")
        assert epp.file_is_same(f1, f2) is True

        with open(f2, "w") as f:
            f.write("hi")
        assert epp.file_is_same(f1, f2) is False

    def test_get_resource_path(self):
        """get_resource_path should return an absolute path."""
        p = epp.get_resource_path("static/icon.png")
        assert os.path.isabs(p)
        assert p.endswith("static/icon.png")
