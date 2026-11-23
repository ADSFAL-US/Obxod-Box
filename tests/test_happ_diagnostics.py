import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import network_diagnostics


class _FakeProcess:
    def __init__(self, pid: int, name: str):
        self.info = {"pid": pid, "name": name, "exe": ""}


class _FakePsutil:
    Error = OSError

    def __init__(self, processes):
        self.processes = processes

    def process_iter(self, _attrs):
        return iter(self.processes)


class HappDiagnosticsTests(unittest.TestCase):
    def test_detects_only_exact_happ_process_names(self):
        fake_psutil = _FakePsutil([
            _FakeProcess(10, "Happ.exe"),
            _FakeProcess(11, "happd.exe"),
            _FakeProcess(12, "HappHelper.exe"),
        ])

        with patch.object(network_diagnostics, "psutil", fake_psutil):
            processes = network_diagnostics._happ_processes()

        self.assertEqual([proc["pid"] for proc in processes], [10, 11])
        self.assertEqual(network_diagnostics._known_process_reason("Happ.exe"), "HAPP")
        self.assertIsNone(network_diagnostics._known_process_reason("HappHelper.exe"))

    def _run_tun_report(self, probe_ok: bool):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "xray.log"
            log_file.write_text(
                "[Error] proxy/tun: connection was refused\n",
                encoding="utf-8",
            )
            manager = SimpleNamespace(
                runtime_config={"inbounds": [{"protocol": "tun"}]},
                xray_status={"running": True},
                log_file=log_file,
            )

            class Config:
                mode = "tun"

                @staticmethod
                def get(_key, default=None):
                    return default

            snapshot = {
                "adapters": [{"name": "obxod-tun", "status": 2}],
                "routes": [{"dest": "0.0.0.0/0", "iface": "obxod-tun"}],
                "probe_iface": "obxod-tun",
            }
            happ_processes = [
                {"pid": 10, "name": "Happ.exe", "reason": "HAPP"},
                {"pid": 11, "name": "happd.exe", "reason": "HAPP"},
            ]
            with (
                patch.object(network_diagnostics, "_process_tree_pids", return_value=set()),
                patch.object(network_diagnostics, "_resolve_probe_ip", return_value="1.1.1.1"),
                patch.object(network_diagnostics, "_network_snapshot", return_value=snapshot),
                patch.object(
                    network_diagnostics,
                    "_probe_with_retries",
                    return_value=(probe_ok, None if probe_ok else "timeout"),
                ),
                patch.object(network_diagnostics, "_conflicting_adapters", return_value=[]),
                patch.object(network_diagnostics, "_radmin_adapters", return_value=[]),
                patch.object(
                    network_diagnostics, "_broken_radmin_adapters", return_value=[]
                ),
                patch.object(
                    network_diagnostics, "_happ_processes", return_value=happ_processes
                ),
            ):
                return network_diagnostics.run_diagnostics(manager, Config())

    def test_tun_refusals_offer_one_happ_termination_action(self):
        report = self._run_tun_report(probe_ok=False)

        self.assertFalse(report.traffic_ok)
        issue = next(i for i in report.issues if i.code == "tun_connection_refused")
        self.assertEqual(issue.severity, "error")
        self.assertEqual(
            issue.actions,
            [{
                "action": "kill_processes",
                "pids": [10, 11],
                "label": "Завершить HAPP",
            }],
        )
        self.assertNotIn("tun_create_failed", {item.code for item in report.issues})

    def test_passing_probe_suppresses_all_issues(self):
        report = self._run_tun_report(probe_ok=True)

        self.assertTrue(report.traffic_ok)
        self.assertEqual(report.issues, [])
        self.assertTrue(report.ok)


if __name__ == "__main__":
    unittest.main()
