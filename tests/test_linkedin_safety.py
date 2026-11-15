"""Offline tests: every network call and persistent write is mocked."""
import copy
import importlib.util
import os
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "linkedin_upload", Path(__file__).resolve().parents[1] / "scripts" / "linkedin_upload.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class GuardTests(unittest.TestCase):
 def test_age_boundaries(self):
  now=datetime(2026,11,15,tzinfo=timezone.utc)
  for hours,held in [(0,False),(24,False),(24.01,True),(-1,True)]:
   self.assertEqual(m.publication_hold_reason({"scheduled_time":(now-timedelta(hours=hours)).isoformat()},now) is not None,held)
 def test_bad_dates(self):
  for value in [None, "", "bad", 123]:
   self.assertIsNotNone(m.publication_hold_reason({"scheduled_time":value},datetime.now(timezone.utc)))
 def test_stale_queue_untouched_no_network(self):
  queue={"items":[{"id":"old","status":"pending","scheduled_time":"2026-09-21T09:00:00+09:00"}]}
  before=copy.deepcopy(queue)
  with patch.object(m,"load_queue",return_value=queue), patch.object(m,"save_queue") as save, patch.object(m.urllib.request,"urlopen",side_effect=AssertionError("network forbidden")) as network:
   m.run()
   save.assert_not_called(); network.assert_not_called()
  self.assertEqual(queue,before)
 def test_dry_run_no_side_effects_even_deletes(self):
  queue={"items":[{"id":"new","status":"pending","scheduled_time":datetime.now(timezone.utc).isoformat()},{"id":"delete","status":"delete_pending","post_urn":"fake"}]}
  before=copy.deepcopy(queue)
  with patch.object(m,"load_queue",return_value=queue), patch.object(m,"save_queue") as save, patch.object(m,"append_log") as log, patch.object(m.urllib.request,"urlopen",side_effect=AssertionError("network forbidden")) as network, patch.dict(os.environ,{"LINKEDIN_DRY_RUN":"true"}):
   m.run()
   save.assert_not_called(); log.assert_not_called(); network.assert_not_called()
  self.assertEqual(queue,before)
unittest.main()
