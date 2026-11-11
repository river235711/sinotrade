# -*- coding: utf-8 -*-
"""
日誌分流與每日分檔單元測試 (live/log_setup.py)
1. paper / live 日誌目錄分開，ShioajiTMFTrader 依模式寫入對應目錄
2. DailyFileHandler 跨日自動切換 trader_YYYYMMDD.log
3. 超過保留天數的 trader_*.log / state_*.json 自動清除，orders_*.csv 永久保留
"""
import os
import sys
import time
import shutil
import logging
import datetime
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.log_setup import get_mode_log_dir, prune_old_files, DailyFileHandler, LOG_RETENTION_DAYS


class TestLogSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mode_dirs_are_separated(self):
        live_dir = get_mode_log_dir("live", self.tmp)
        paper_dir = get_mode_log_dir("paper", self.tmp)
        self.assertNotEqual(live_dir, paper_dir)
        self.assertTrue(live_dir.endswith(os.path.join("", "live")))
        self.assertTrue(paper_dir.endswith(os.path.join("", "paper")))
        with self.assertRaises(ValueError):
            get_mode_log_dir("backtest", self.tmp)

    def test_trader_log_dir_follows_simulation_flag(self):
        from live.shioaji_trader import ShioajiTMFTrader
        paper = ShioajiTMFTrader(simulation=True)
        live = ShioajiTMFTrader(simulation=False)
        self.assertEqual(os.path.basename(paper.log_dir), "paper")
        self.assertEqual(os.path.basename(live.log_dir), "live")
        self.assertEqual(paper.state_manager.log_dir, paper.log_dir)
        self.assertEqual(live.state_manager.log_dir, live.log_dir)

    def test_prune_keeps_recent_and_orders(self):
        today = datetime.date(2026, 11, 11)
        old = (today - datetime.timedelta(days=LOG_RETENTION_DAYS + 1)).strftime("%Y%m%d")
        recent = (today - datetime.timedelta(days=LOG_RETENTION_DAYS - 1)).strftime("%Y%m%d")
        names = [f"trader_{old}.log", f"state_{old}.json", f"orders_{old}.csv",
                 f"trader_{recent}.log", f"state_{recent}.json", "stdout.log"]
        for n in names:
            open(os.path.join(self.tmp, n), "w").close()

        removed = prune_old_files(self.tmp, LOG_RETENTION_DAYS, today)

        self.assertCountEqual(removed, [f"trader_{old}.log", f"state_{old}.json"])
        left = set(os.listdir(self.tmp))
        self.assertIn(f"orders_{old}.csv", left)          # 稽核紀錄永久保留
        self.assertIn(f"trader_{recent}.log", left)
        self.assertIn(f"state_{recent}.json", left)
        self.assertIn("stdout.log", left)

    def test_daily_handler_switches_file_on_new_day(self):
        handler = DailyFileHandler(self.tmp)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test_daily_handler")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            today = datetime.date.today()
            tomorrow = today + datetime.timedelta(days=1)

            logger.info("day1")
            rec = logger.makeRecord(logger.name, logging.INFO, __file__, 0, "day2", None, None)
            rec.created = time.mktime(datetime.datetime.combine(tomorrow, datetime.time(0, 0, 5)).timetuple())
            logger.handle(rec)
        finally:
            logger.removeHandler(handler)
            handler.close()

        f1 = os.path.join(self.tmp, f"trader_{today:%Y%m%d}.log")
        f2 = os.path.join(self.tmp, f"trader_{tomorrow:%Y%m%d}.log")
        with open(f1, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "day1")
        with open(f2, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "day2")


if __name__ == "__main__":
    unittest.main()
