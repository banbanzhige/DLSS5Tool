"""CPU-only contracts for persistent DXGI adapter selection."""
from pathlib import Path
import unittest
from unittest import mock

from dlss5tool import app_settings, dlss_engine


ROOT = Path(__file__).resolve().parents[1]


def native_info(*, name="RTX Fixture", vendor=0x10DE, device=0x2684,
                subsys=0x12345678, revision=1, memory=12 * 1024 ** 3,
                flags=2 | 4, index=0, luid_low=10):
    info = dlss_engine._NativeAdapterInfo()
    info.struct_size = 1
    info.preference_index = index
    info.vendor_id = vendor
    info.device_id = device
    info.subsys_id = subsys
    info.revision = revision
    info.flags = flags
    info.dedicated_video_memory = memory
    info.luid_high = 0
    info.luid_low = luid_low
    info.description = name
    return info


class FakeAdapterLibrary:
    def __init__(self):
        self.selection = None

    def dlssnr_select_adapter_auto(self):
        self.selection = "auto"

    def dlssnr_select_adapter(self, high, low):
        self.selection = (int(high), int(low))


class AdapterPolicyTests(unittest.TestCase):
    def test_records_filter_non_nvidia_software_and_unsupported_devices(self):
        records = dlss_engine._adapter_records([
            native_info(name="Intel", vendor=0x8086, luid_low=1),
            native_info(name="Software", flags=1 | 2, luid_low=2),
            native_info(name="No D3D12", flags=0, luid_low=3),
            native_info(name="RTX", luid_low=4),
        ])
        self.assertEqual([record["name"] for record in records], ["RTX"])

    def test_partitioned_views_are_deduplicated_by_stable_hardware_id(self):
        first = native_info(index=0, luid_low=100)
        partition = native_info(index=3, luid_low=200)
        records = dlss_engine._adapter_records([first, partition])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["luid_low"], 100)
        self.assertNotIn("100", records[0]["id"])

    def test_cuda_luids_keep_physical_identical_gpus_and_drop_driver_views(self):
        first = native_info(index=0, luid_low=100)
        partition = native_info(index=2, luid_low=200)
        second_gpu = native_info(index=3, luid_low=300)
        records = dlss_engine._adapter_records(
            [first, partition, second_gpu],
            {
                (0, 100): {"cuda_index": 0, "pci_bus_id": "0000:01:00.0"},
                (0, 300): {"cuda_index": 1, "pci_bus_id": "0000:02:00.0"},
            },
        )
        self.assertEqual([record["cuda_index"] for record in records], [0, 1])
        self.assertEqual([record["luid_low"] for record in records], [100, 300])
        self.assertTrue(records[0]["id"].endswith(":P000001000"))
        self.assertTrue(records[1]["id"].endswith(":P000002000"))

    def test_auto_and_explicit_selection_resolve_to_current_luid(self):
        native = native_info(luid_low=321)
        record = dlss_engine._adapter_records([native])[0]
        library = FakeAdapterLibrary()
        with mock.patch.object(dlss_engine, "_native_adapter_infos", return_value=[native]), \
                mock.patch.object(dlss_engine, "_cuda_adapter_luids", return_value={}):
            _records, selected = dlss_engine._configure_render_adapter(
                library, {"render_gpu": "auto"}, "v2",
            )
            self.assertEqual(library.selection, "auto")
            self.assertEqual(selected["id"], record["id"])
            _records, selected = dlss_engine._configure_render_adapter(
                library, {"render_gpu": record["id"]}, "v2",
            )
            self.assertEqual(library.selection, (0, 321))
            self.assertEqual(selected["name"], "RTX Fixture")

    def test_unavailable_explicit_selection_does_not_silently_fallback(self):
        library = FakeAdapterLibrary()
        with mock.patch.object(
            dlss_engine, "_native_adapter_infos", return_value=[native_info()],
        ), mock.patch.object(dlss_engine, "_cuda_adapter_luids", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "当前不可用"):
                dlss_engine._configure_render_adapter(
                    library,
                    {"render_gpu": "dxgi:10DE:9999:00000000:00000000:0000000000000000"},
                    "v2",
                )

    def test_settings_validate_adapter_id_and_reject_arbitrary_text(self):
        adapter_id = dlss_engine._adapter_records([native_info()])[0]["id"]
        self.assertEqual(app_settings.validate({"render_gpu": adapter_id})["render_gpu"], adapter_id)
        self.assertEqual(app_settings.validate({"render_gpu": "../../wrong"})["render_gpu"], "auto")

    def test_native_host_never_creates_the_default_null_adapter(self):
        source = (ROOT / "native/host_v2/dlssnr_host_v2.cpp").read_text(encoding="utf-8")
        self.assertNotIn("create_device(nullptr", source)
        self.assertIn("adapter.vendor_id == kNvidiaVendorId", source)
        self.assertIn("DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE", source)


if __name__ == "__main__":
    unittest.main()
