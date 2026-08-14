from pathlib import Path


def test_daily_monitor_script_exists_and_does_not_import_real_broker():
    root = Path(__file__).resolve().parents[1]
    text = (root / "Scripts" / "run_daily_paper_monitor.py").read_text(encoding="utf-8")
    assert "PaperTradingMonitor" in text
    assert "PaperBrokerService" in text
    assert "real_broker" not in text.lower()
    assert "data_mode=\"auto\"" in text
