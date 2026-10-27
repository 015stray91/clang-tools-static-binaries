#!/usr/bin/env python3
"""Regression tests for build.py source derivation (no network, no build).

Guards the bug where releases.json values (bare source names) were passed
straight to urlretrieve, failing every regular CI build with
``ValueError: unknown url type``.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build


class TestCompilerMapping(unittest.TestCase):
    def test_period_correct_host_gcc(self):
        cases = {"12": "11" if False else "10", "13": "10", "14": "11",
                 "15": "11", "16": "11", "17": "11", "20": "12", "22": "12"}
        for version, gcc in cases.items():
            with self.subTest(version=version):
                args = build.get_linux_compiler_args(version + ".0.0")
                self.assertIn(f"-DCMAKE_C_COMPILER=gcc-{gcc}", args)
                self.assertIn(f"-DCMAKE_CXX_COMPILER=g++-{gcc}", args)


class TestSourceInfo(unittest.TestCase):
    def test_every_release_has_valid_url(self):
        for version, release in build.RELEASES.items():
            with self.subTest(version=version):
                got_release, url, tarball, release_dir = build.source_info(version)
                self.assertEqual(got_release, release)
                # Must be a fetchable https URL, never the bare name.
                self.assertTrue(
                    url.startswith("https://github.com/llvm/llvm-project/releases/download/llvmorg-"),
                    url,
                )
                self.assertNotEqual(url, release)
                self.assertTrue(tarball.name.endswith(".tar.xz"), tarball.name)
                self.assertIn(release, url)
                self.assertEqual(release_dir, Path(release))

    def test_known_good_vectors(self):
        cases = {
            "12": (
                "https://github.com/llvm/llvm-project/releases/download/"
                "llvmorg-12.0.1/llvm-project-12.0.1.src.tar.xz"
            ),
            "14": (
                "https://github.com/llvm/llvm-project/releases/download/"
                "llvmorg-14.0.0/llvm-project-14.0.0.src.tar.xz"
            ),
            "22": (
                "https://github.com/llvm/llvm-project/releases/download/"
                "llvmorg-22.1.0/llvm-project-22.1.0.src.tar.xz"
            ),
        }
        for version, expected_url in cases.items():
            with self.subTest(version=version):
                _, url, _, _ = build.source_info(version)
                self.assertEqual(url, expected_url)


if __name__ == "__main__":
    unittest.main()
