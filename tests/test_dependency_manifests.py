"""Keep the requirements manifests, the pins and the import guards in agreement."""

import subprocess
import sys
import unittest
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent


def _requirements(filename):
    lines = (line.split('#')[0].strip() for line in (ROOT / filename).read_text().splitlines())
    return [Requirement(line) for line in lines if line and not line.startswith('-')]


def _applies_on_linux(requirement):
    if requirement.marker is None:
        return True
    return any(
        requirement.marker.evaluate({
            'python_version': version,
            'python_full_version': f'{version}.0',
            'sys_platform': 'linux',
            'platform_system': 'Linux',
        })
        for version in ('3.11', '3.12', '3.13', '3.14')
    )


def _import_fails_without(blocked, module):
    code = f"import sys; sys.modules[{blocked!r}] = None; import {module}"
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    return result.returncode != 0


class TestManifests(unittest.TestCase):
    def test_pins_satisfy_every_range_in_the_linux_closure(self):
        pins = {
            canonicalize_name(pin.name): next(iter(pin.specifier)).version
            for pin in _requirements('constraints.txt')
        }
        queue = [
            requirement
            for filename in ('requirements-minimal.txt', 'requirements.txt', 'requirements-dropbox.txt', 'requirements-s3.txt')
            for requirement in _requirements(filename)
        ]
        seen = set()
        while queue:
            requirement = queue.pop()
            name = canonicalize_name(requirement.name)
            self.assertIn(name, pins)
            self.assertTrue(
                requirement.specifier.contains(pins[name], prereleases=True),
                f'{name}=={pins[name]} is outside {requirement}',
            )
            if name in seen:
                continue
            seen.add(name)
            try:
                dist = metadata.distribution(name)
            except metadata.PackageNotFoundError:
                continue  # Not in this job's manifest, or only needed on another Python.
            # The weekly unpinned run holds other versions, whose requirements say nothing about the pins.
            if dist.version == pins[name]:
                queue += [r for r in map(Requirement, dist.requires or []) if _applies_on_linux(r)]

    def test_prawcore_is_declared_because_it_is_imported_directly(self):
        self.assertIn('prawcore', {r.name for r in _requirements('requirements-minimal.txt')})


class TestImportGuards(unittest.TestCase):
    def test_missing_required_library_fails_at_import(self):
        # A silent fallback would lose retries or image checks, or change the stored digest.
        for blocked, module in (
            ('blake3', 'utils.storage.content_hash'),
            ('blake3', 'utils.media_services.base_downloader'),
            ('tenacity', 'utils.media_services.base_downloader'),
            ('PIL', 'utils.media_services.base_downloader'),
        ):
            with self.subTest(blocked=blocked, module=module):
                self.assertTrue(_import_fails_without(blocked, module))

    def test_curl_cffi_stays_optional(self):
        self.assertFalse(_import_fails_without('curl_cffi', 'utils.media_services.base_downloader'))


if __name__ == '__main__':
    unittest.main()
