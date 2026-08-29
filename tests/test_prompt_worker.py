"""
Regression tests for the headline bug: interactive prompts used to run inline on
the udev poll loop, so a 30s notification blocked event handling and SIGTERM.
"""

import tempfile
import threading
import unittest

from context import RecordingPrompts, buswatchd, identity, make_daemon, usb_event


class TestPromptWorker(unittest.TestCase):
    def test_submitted_work_runs_off_the_calling_thread(self):
        worker = buswatchd.PromptWorker()
        worker.start()
        done = threading.Event()
        seen = {}

        def job():
            seen["thread"] = threading.current_thread().name
            done.set()

        self.assertTrue(worker.submit(job))
        self.assertTrue(done.wait(timeout=5), "prompt job never ran")
        self.assertEqual(seen["thread"], "prompt")
        worker.stop()

    def test_submit_returns_false_when_queue_is_full(self):
        worker = buswatchd.PromptWorker(maxsize=1)  # not started: nothing drains it
        self.assertTrue(worker.submit(lambda: None))
        self.assertFalse(worker.submit(lambda: None))

    def test_worker_exception_does_not_kill_the_thread(self):
        worker = buswatchd.PromptWorker()
        worker.start()
        done = threading.Event()

        def boom():
            raise RuntimeError("prompt blew up")

        worker.submit(boom)
        worker.submit(done.set)
        self.assertTrue(done.wait(timeout=5), "worker died on the first exception")
        worker.stop()

    def test_stop_joins_the_thread(self):
        worker = buswatchd.PromptWorker()
        worker.start()
        worker.stop(timeout=5)
        self.assertFalse(worker._t.is_alive())

    def test_submit_after_stop_is_rejected(self):
        worker = buswatchd.PromptWorker()
        worker.start()
        worker.stop()
        self.assertFalse(worker.submit(lambda: None))


class TestPollLoopStaysUnblocked(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_interactive_add_defers_the_prompt_instead_of_running_it(self):
        daemon = make_daemon(self._tmp.name, cfg={"interactive": True})
        daemon._handle_usb_add(usb_event(ident=identity()))

        # The prompt is queued, not executed: handle_device_event returned without
        # ever calling into the blocking notification backend.
        self.assertEqual(len(daemon.test_prompts.submitted), 1)
        self.assertEqual(daemon.test_notifier.prompts, [])

    def test_deferred_prompt_still_applies_the_choice(self):
        daemon = make_daemon(self._tmp.name, cfg={"interactive": True})
        ident = identity()
        daemon.test_notifier.prompt_result = "trust"

        daemon._handle_usb_add(usb_event(ident=ident))
        daemon.test_prompts.run_pending()

        self.assertTrue(daemon.state.is_trusted(ident))
        self.assertIn("USB trusted", daemon.test_notifier.summaries())

    def test_dropped_prompt_falls_back_to_a_plain_notification(self):
        daemon = make_daemon(self._tmp.name, cfg={"interactive": True}, prompts=RecordingPrompts(accept=False))
        daemon._handle_usb_add(usb_event(ident=identity(), name="Widget"))
        self.assertEqual(daemon.test_notifier.summaries(), ["USB add: Widget"])

    def test_non_interactive_mode_never_prompts(self):
        daemon = make_daemon(self._tmp.name, cfg={"interactive": False})
        daemon._handle_usb_add(usb_event(ident=identity(), name="Widget"))
        self.assertEqual(daemon.test_prompts.submitted, [])
        self.assertEqual(daemon.test_notifier.summaries(), ["USB add: Widget"])

    def test_shutdown_tears_down_prompts_and_children(self):
        daemon = make_daemon(self._tmp.name)
        daemon.shutdown()
        self.assertTrue(daemon.test_prompts.stopped)
        self.assertTrue(daemon.test_children.closed)


class TestChildProcs(unittest.TestCase):
    def test_capture_stdout_returns_child_output(self):
        procs = buswatchd.ChildProcs()
        self.assertEqual(procs.run(["echo", "hi"], capture_stdout=True), "hi\n")

    def test_input_is_delivered_to_the_child(self):
        procs = buswatchd.ChildProcs()
        out = procs.run(["cat"], input_bytes=b"a\nb\n", capture_stdout=True)
        self.assertEqual(out, "a\nb\n")

    def test_missing_binary_returns_none(self):
        procs = buswatchd.ChildProcs()
        self.assertIsNone(procs.run(["buswatchd-does-not-exist"], capture_stdout=True))

    def test_run_after_close_is_refused(self):
        procs = buswatchd.ChildProcs()
        procs.close()
        self.assertIsNone(procs.run(["echo", "hi"], capture_stdout=True))

    def test_close_terminates_a_running_child(self):
        procs = buswatchd.ChildProcs()
        started = threading.Event()
        result = {}

        def run_slow():
            started.set()
            result["out"] = procs.run(["sleep", "60"], capture_stdout=True)

        t = threading.Thread(target=run_slow, daemon=True)
        t.start()
        self.assertTrue(started.wait(timeout=5))
        # Give Popen a moment to register the child before tearing it down.
        for _ in range(100):
            if procs._procs:
                break
            threading.Event().wait(0.02)
        procs.close()
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "close() did not terminate the blocking child")


if __name__ == "__main__":
    unittest.main()
