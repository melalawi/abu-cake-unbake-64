"""Jobserver tokens and subprocess descriptor forwarding."""

import os
import subprocess
import unittest
from pathlib import Path

from tests.process_fakes import boundary
from unbake.project import setup_proof
from unbake.config import Held


class SetupBudgetTests(unittest.TestCase):
    def test_shared_slots_forward_tokens_and_close_after_failure(self):
        with setup_proof.job_slots(12, 3) as slots:
            self.assertEqual(len(slots.descriptors), 2)
            reader, writer = slots.descriptors
            os.set_blocking(reader, False)
            tokens = os.read(reader, 100)
            self.assertEqual(len(tokens), 9)
            os.write(writer, tokens)

            def run(command, **kwargs):
                self.assertEqual(kwargs["pass_fds"], slots.descriptors)
                self.assertEqual(kwargs["env"]["MAKEFLAGS"], slots.environment["MAKEFLAGS"])
                return subprocess.CompletedProcess(command, 1, "fixture failure", "")

            with boundary(setup_proof, run), self.assertRaisesRegex(Held, "fixture failure"):
                setup_proof.run(
                    ["make", "failure"], Path.cwd(), environment=slots.environment, descriptors=slots.descriptors
                )
            self.assertEqual(os.read(reader, 100), tokens)
        for descriptor in (reader, writer):
            with self.assertRaises(OSError):
                os.fstat(descriptor)
