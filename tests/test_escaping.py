import importlib.util
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Job, node and partition fields come from Slurm, and job names, users and
# reasons are chosen by any cluster user, so the page must escape them.
RAW_FIELD = re.compile(r"\$\{[jnp]\.\w+(\|\|'[^']*')?\}")


class PageTemplateEscapingTests(unittest.TestCase):
    def assert_no_raw_fields(self, module):
        self.assertEqual(RAW_FIELD.findall(module.PAGE_TEMPLATE), [])

    def test_dashboard_template_escapes_slurm_fields(self):
        self.assert_no_raw_fields(load(ROOT / "slurmboard.py", "slurmboard_escaping"))

    def test_app_template_escapes_slurm_fields(self):
        self.assert_no_raw_fields(
            load(ROOT / "SlurmboardApp" / "slurmboard.py", "slurmboard_app_escaping"))


if __name__ == "__main__":
    unittest.main()
