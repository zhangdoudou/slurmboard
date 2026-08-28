import importlib.util
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("slurmboard_scalable", ROOT / "slurmboard.py")
SLURMBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SLURMBOARD)


class CompactSummaryTests(unittest.TestCase):
    def test_aggregates_partitions_without_double_counting_overlapping_nodes(self):
        output = (
            "gpu-a|up|1-00:00:00|2|idle|0/8/0/8|1000|gpu:a100:2|node[01-02]\n"
            "gpu-b|up|02:00:00|1|alloc|4/0/0/4|1000|gpu:a100:2|node02\n"
        )

        with mock.patch.object(SLURMBOARD, "_run", return_value=output) as run:
            summary, partitions = SLURMBOARD.collect_partition_summaries()

        self.assertEqual(run.call_count, 1)
        self.assertEqual(summary["node_count"], 2)
        self.assertEqual(summary["cpu_total"], 8)
        self.assertEqual(summary["gpu_total"], 4)
        self.assertIsNone(summary["gpu_alloc"])
        self.assertEqual(summary["gpu_by_type"]["a100"]["nodes"], 2)
        self.assertEqual([part["name"] for part in partitions], ["gpu-a", "gpu-b"])
        self.assertEqual(partitions[0]["gpu_total"], 4)
        self.assertEqual(partitions[1]["gpu_total"], 2)
        self.assertFalse(partitions[0]["nodes_loaded"])
        self.assertFalse(partitions[0]["jobs_loaded"])

    def test_knows_roihu_gpu_memory_sizes(self):
        self.assertEqual(SLURMBOARD._vram_gb_from_gres("gpu:gh200:4"), 96)
        self.assertEqual(SLURMBOARD._vram_gb_from_gres("gpu:l40:2"), 48)

    def test_initial_snapshot_does_not_query_personal_or_global_jobs(self):
        compact = {
            "generated_at": "now", "summary": {}, "partitions": [], "nodes": []
        }
        with mock.patch.object(
            SLURMBOARD, "build_cluster_summary", return_value=compact
        ), mock.patch.object(
            SLURMBOARD, "collect_active_queue", side_effect=AssertionError("queried queue")
        ), mock.patch.object(
            SLURMBOARD, "collect_user_jobs", side_effect=AssertionError("queried history")
        ):
            snapshot = SLURMBOARD.build_snapshot()

        self.assertFalse(snapshot["active_queue_loaded"])
        self.assertFalse(snapshot["user_jobs_loaded"])
        self.assertEqual(snapshot["active_queue"], [])


class AdaptiveResourceEnrichmentTests(unittest.TestCase):
    def setUp(self):
        SLURMBOARD._CACHE.clear()

    def tearDown(self):
        SLURMBOARD._CACHE.clear()

    @staticmethod
    def _compact(node_count=2):
        summary = {
            "cpu_alloc": 0, "cpu_total": 16,
            "mem_alloc_mb": None, "mem_total_mb": 2000,
            "gpu_alloc": None, "gpu_total": 8,
            "node_count": node_count, "node_states": {"IDLE": node_count},
            "gpu_by_type": {}, "lazy": True,
        }
        partition = {
            "name": "gpu", "nodes": node_count,
            "cpu_alloc": 0, "cpu_total": 16,
            "mem_alloc_mb": None, "mem_total_mb": 2000,
            "gpu_alloc": None, "gpu_idle": None, "gpu_total": 8,
            "gpu_vram_gb": None, "states": {"IDLE": node_count},
        }
        return summary, [partition]

    def test_parses_and_deduplicates_compact_node_resources(self):
        output = (
            "r01 gpu idle 0/8/0/8 1000 100 gpu:gh200:4 gpu:gh200:1(IDX:0)\n"
            "r01 interactive idle 0/8/0/8 1000 100 gpu:gh200:4 gpu:gh200:1(IDX:0)\n"
            "r02 gpu mix 4/4/0/8 1000 600 gpu:l40:2 gpu:l40:2(IDX:0-1)\n"
        )

        with mock.patch.object(SLURMBOARD, "_run", return_value=output) as run:
            nodes = SLURMBOARD.collect_compact_node_resources()

        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["partitions"], ["gpu", "interactive"])
        self.assertEqual(nodes[0]["gpu_alloc"], 1)
        self.assertEqual(nodes[0]["gpu_vram_gb"], 96)
        self.assertEqual(nodes[1]["mem_alloc_mb"], 600)
        self.assertEqual(nodes[1]["gpu_vram_gb"], 48)

    def test_small_cluster_blanks_are_filled_from_cached_exact_nodes(self):
        summary, partitions = self._compact()
        nodes = [
            {
                "name": "r01", "state": "IDLE", "partitions": ["gpu"],
                "cpu_alloc": 0, "cpu_total": 8,
                "mem_alloc_mb": 100, "mem_total_mb": 1000,
                "gpu_type": "gh200", "gpu_alloc": 1, "gpu_total": 4,
                "gpu_vram_gb": 96,
            },
            {
                "name": "r02", "state": "MIX", "partitions": ["gpu"],
                "cpu_alloc": 4, "cpu_total": 8,
                "mem_alloc_mb": 600, "mem_total_mb": 1000,
                "gpu_type": "gh200", "gpu_alloc": 2, "gpu_total": 4,
                "gpu_vram_gb": 96,
            },
        ]

        with mock.patch.object(
            SLURMBOARD, "collect_partition_summaries",
            return_value=(summary, partitions),
        ), mock.patch.object(
            SLURMBOARD, "collect_compact_node_resources", return_value=nodes,
        ) as collect_nodes, mock.patch.object(
            SLURMBOARD, "collect_job_counts", return_value={
                "gpu": {
                    "running": 7, "pending": 2,
                    "jobs": [{"id": "123", "state": "RUNNING"}],
                },
            },
        ) as collect_jobs:
            snapshot = SLURMBOARD.build_cluster_summary(refresh=True)
            SLURMBOARD.build_cluster_summary(refresh=True)

        result = snapshot["summary"]
        self.assertEqual(collect_nodes.call_count, 1)
        self.assertEqual(collect_jobs.call_count, 1)
        self.assertEqual(result["mem_alloc_mb"], 700)
        self.assertEqual(result["gpu_alloc"], 3)
        self.assertEqual(result["gpu_by_type"]["gh200"]["alloc"], 3)
        self.assertFalse(result["lazy"])
        self.assertEqual(snapshot["partitions"][0]["gpu_idle"], 5)
        self.assertEqual(snapshot["partitions"][0]["gpu_vram_gb"], 96)
        self.assertEqual(snapshot["partitions"][0]["jobs_running"], 7)
        self.assertEqual(snapshot["partitions"][0]["jobs_pending"], 2)
        self.assertTrue(snapshot["partitions"][0]["jobs_loaded"])

    def test_large_clusters_keep_the_lightweight_summary(self):
        summary, partitions = self._compact(SLURMBOARD._NODE_ENRICH_LIMIT + 1)
        with mock.patch.object(
            SLURMBOARD, "collect_partition_summaries",
            return_value=(summary, partitions),
        ), mock.patch.object(
            SLURMBOARD, "collect_compact_node_resources",
            side_effect=AssertionError("large cluster enumerated"),
        ) as collect_nodes:
            with mock.patch.object(
                SLURMBOARD, "collect_job_counts",
                side_effect=AssertionError("large cluster queue loaded"),
            ) as collect_jobs:
                snapshot = SLURMBOARD.build_cluster_summary(refresh=True)

        collect_nodes.assert_not_called()
        collect_jobs.assert_not_called()
        self.assertIsNone(snapshot["summary"]["mem_alloc_mb"])
        self.assertTrue(snapshot["summary"]["lazy"])

    def test_small_cluster_initial_snapshot_loads_active_queue(self):
        compact = {
            "generated_at": "now",
            "summary": {"node_count": 626},
            "partitions": [], "nodes": [],
        }
        active = [{"id": "925069", "state": "PENDING"}]
        with mock.patch.object(
            SLURMBOARD, "build_cluster_summary", return_value=compact,
        ), mock.patch.object(
            SLURMBOARD, "collect_active_queue", return_value=active,
        ) as collect_active, mock.patch.object(
            SLURMBOARD, "collect_user_jobs",
            side_effect=AssertionError("history should load in the browser"),
        ):
            snapshot = SLURMBOARD.build_snapshot()

        collect_active.assert_called_once()
        self.assertTrue(snapshot["auto_load_personal"])
        self.assertTrue(snapshot["active_queue_loaded"])
        self.assertEqual(snapshot["active_queue"], active)
        self.assertFalse(snapshot["user_jobs_loaded"])


class LazyPartitionTests(unittest.TestCase):
    def tearDown(self):
        SLURMBOARD._PARTITION_NODELISTS.clear()

    def test_parses_exact_node_details_for_one_partition(self):
        output = (
            "nid000018 lumid mix 88/168/0/256 2048000 901120 "
            "gpu:a40:8,nvme:40000 gpu:a40:8(IDX:0-7),nvme:0\n"
            "nid000019 lumid idle 0/256/0/256 2048000 0 "
            "gpu:a40:8,nvme:40000 gpu:a40:0(IDX:N/A),nvme:0\n"
        )

        with mock.patch.object(SLURMBOARD, "_run", return_value=output) as run:
            nodes = SLURMBOARD.collect_partition_nodes("lumid")

        command = run.call_args.args[0]
        self.assertIn("-p", command)
        self.assertIn("lumid", command)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["gpu_alloc"], 8)
        self.assertEqual(nodes[1]["gpu_idle"], 8)
        self.assertEqual(nodes[0]["mem_alloc_mb"], 901120)

    def test_reuses_compact_hostlist_for_scoped_scontrol_query(self):
        SLURMBOARD._PARTITION_NODELISTS["lumid"] = ("nid[000016-000023]",)
        output = (
            "NodeName=nid000016 State=IDLE CPUAlloc=0 CPUTot=256 CPULoad=0.10 "
            "Gres=gpu:a40:8 Partitions=lumid RealMemory=2048000 AllocMem=0 "
            "CfgTRES=cpu=256,gres/gpu=8,gres/gpu:a40=8 "
            "AllocTRES=cpu=0,gres/gpu=0,gres/gpu:a40=0\n"
        )

        with mock.patch.object(SLURMBOARD, "_run", return_value=output) as run:
            nodes = SLURMBOARD.collect_partition_nodes("lumid")

        command = run.call_args.args[0]
        self.assertEqual(command, [
            "scontrol", "-o", "show", "node", "nid[000016-000023]",
        ])
        self.assertEqual(nodes[0]["name"], "nid000016")
        self.assertEqual(nodes[0]["gpu_idle"], 8)
        self.assertEqual(nodes[0]["load"], 0.10)

    def test_partition_job_query_is_scoped(self):
        with mock.patch.object(SLURMBOARD, "_run", return_value="") as run:
            SLURMBOARD.collect_job_counts("standard-g")

        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-p", "standard-g"])


class CacheTests(unittest.TestCase):
    def test_short_cache_reuses_value_until_forced(self):
        SLURMBOARD._CACHE.clear()
        loader = mock.Mock(side_effect=[{"value": 1}, {"value": 2}])

        first = SLURMBOARD._cached("sample", 30, loader)
        second = SLURMBOARD._cached("sample", 30, loader)
        refreshed = SLURMBOARD._cached("sample", 30, loader, refresh=True)

        self.assertIs(first, second)
        self.assertEqual(refreshed, {"value": 2})
        self.assertEqual(loader.call_count, 2)


class DashboardRefreshUITests(unittest.TestCase):
    def test_dashboard_offers_persistent_refresh_intervals(self):
        page = SLURMBOARD.PAGE_TEMPLATE

        self.assertIn('id="refresh-interval"', page)
        self.assertIn('<option value="0">Manual</option>', page)
        self.assertIn('<option value="60">1 minute</option>', page)
        self.assertIn('<option value="300">5 minutes</option>', page)
        self.assertIn("const DEFAULT_REFRESH_SECONDS = 60", page)
        self.assertIn("slurmboard_refresh_seconds", page)
        self.assertNotIn('onclick="location.reload()"', page)
        self.assertIn("if (SNAPSHOT.auto_load_personal)", page)
        self.assertIn("refreshHistoryJobs(false)", page)


class StorageQuotaTests(unittest.TestCase):
    def test_parses_lumi_space_and_file_quotas(self):
        output = """
Disk area                          Capacity(used/max)  Files(used/max)
Personal home folder
/users/cong                              1,7G/22G         43K/100K
Project: project_465000005
/projappl/project_465000005              4.1K/54G           1/100K
/scratch/project_465000005               3.8G/55T          72/2.0M
"""

        rows = SLURMBOARD.parse_lumi_quota(output)

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["scope"], "Personal home")
        self.assertEqual(rows[1]["scope"], "project_465000005")
        self.assertEqual(rows[0]["files_used"], 43000)
        self.assertEqual(rows[2]["files_limit"], 2000000)
        self.assertGreater(rows[0]["space_percent"], 7)

    def test_parses_triton_user_and_group_quotas(self):
        output = """
User quotas for cong
     Filesystem   space   quota   limit   grace   files   quota   limit   grace
/home              484M    977M   1075M           10264       0       0
/scratch          3237G    200G    210G       -    158M      1M      1M       -

Group quotas
Filesystem   group                  space   quota   limit   grace   files   quota   limit   grace
/scratch     domain users            132G     10M     10M       -    310M    5000    5000       -
/scratch     some-group              16T     20T     20T       -   1088M      5M      5M       -
"""

        rows = SLURMBOARD.parse_posix_quota(output)

        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["scope"], "User")
        self.assertIsNone(rows[0]["files_limit"])
        self.assertEqual(rows[1]["files_limit"], 1000000)
        self.assertEqual(rows[2]["scope"], "Group: domain users")
        self.assertEqual(rows[3]["space_limit_label"], "20T")

    def test_parses_gpfs_space_and_file_quotas(self):
        output = """
                        Block Limits         |     File Limits
Filesystem Fileset type   KB quota  limit doubt grace | files quota limit doubt grace
gpfs2      fset4   USR  4104 10240 153600     0  none |     1  1000  5000     0  none
"""

        rows = SLURMBOARD.parse_gpfs_quota(output)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "gpfs2:fset4")
        self.assertEqual(rows[0]["space_used_label"], "4104K")
        self.assertEqual(rows[0]["files_limit"], 1000)

    def test_parses_beegfs_storage_pool_quota(self):
        output = """
Quota information for storage pool Default (ID: 1):
      user/group   ||          size        ||   chunk files
     name |  id   ||    used   |    hard  ||   used  |   hard
alice     | 1000  || 1024.00 MiB|1024.00 MiB|| 1     |unlimited
"""

        rows = SLURMBOARD.parse_beegfs_quota(output)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope"], "User: alice")
        self.assertEqual(rows[0]["path"], "BeeGFS pool Default")
        self.assertEqual(rows[0]["space_percent"], 100.0)
        self.assertEqual(rows[0]["files_limit_label"], "unlimited")

    def test_quota_collection_uses_lumi_helper_when_available(self):
        sample = "/users/cong 1G/22G 10K/100K\n"
        availability = lambda command: (
            f"/usr/bin/{command}" if command == "lumi-workspaces" else None
        )
        with mock.patch.object(
            SLURMBOARD, "_QUOTA_COMMAND", None
        ), mock.patch.object(
            SLURMBOARD.shutil, "which", side_effect=availability
        ), mock.patch.object(
            SLURMBOARD, "_run_quota", return_value=sample
        ) as run:
            result = SLURMBOARD.collect_storage_quota()

        run.assert_called_once_with(["lumi-workspaces"])
        self.assertEqual(result["source"], "lumi-workspaces")
        self.assertEqual(len(result["rows"]), 1)

    def test_custom_quota_command_is_split_without_a_shell(self):
        sample = "/data 2G/10G 20K/100K\n"
        with mock.patch.object(
            SLURMBOARD, "_QUOTA_COMMAND", "site-quota --human readable"
        ), mock.patch.object(
            SLURMBOARD, "_run_quota", return_value=sample
        ) as run:
            result = SLURMBOARD.collect_storage_quota()

        run.assert_called_once_with(["site-quota", "--human", "readable"])
        self.assertEqual(result["source"], "custom")
        self.assertEqual(len(result["rows"]), 1)


if __name__ == "__main__":
    unittest.main()
