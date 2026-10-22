from dlss5tool.export_preflight import assess_video_export

GIB = 1024**3
MEMORY = dict(total_physical_bytes=32*GIB, free_physical_bytes=20*GIB,
              free_commit_bytes=24*GIB)
GPU = dict(free_bytes=7*GIB)


def test_duration_does_not_multiply_frame_or_model_memory():
    short = assess_video_export(1920, 1080, 120, 30, {'super_resolution_scale': 2},
                                memory=MEMORY, gpu_memory=GPU)
    long = assess_video_export(1920, 1080, 36000, 30, {'super_resolution_scale': 2},
                               memory=MEMORY, gpu_memory=GPU)
    assert short['estimate'] == long['estimate']
    assert not short['warnings']
    assert long['warnings'] == ['long_run']
    assert long['risk'] == 'medium'


def test_ram_and_vram_pressure_have_distinct_reasons():
    pressure = dict(MEMORY, free_physical_bytes=2*GIB)
    result = assess_video_export(1920, 1080, 30, 30, {'super_resolution_scale': 2},
        memory=pressure, gpu_memory={'free_bytes': 128*1024**2})
    assert result['warnings'] == ['ram_peak', 'vram_peak']
    assert result['risk'] == 'high'


def test_unknown_resources_are_not_reported_as_confirmed_oom():
    result = assess_video_export(1920, 1080, 10, 0, {'super_resolution_scale': 2})
    assert result['duration_seconds'] is None
    assert result['warnings'] == ['unknown_resources']
    assert result['risk'] == 'medium'


def test_tight_but_sufficient_vram_does_not_claim_a_deficit():
    baseline = assess_video_export(1920, 1080, 30, 30, {'super_resolution_scale': 2},
                                  memory=MEMORY, gpu_memory=GPU)
    required = baseline['estimate']['recommended_gpu_bytes']
    result = assess_video_export(1920, 1080, 30, 30, {'super_resolution_scale': 2},
        memory=MEMORY, gpu_memory={'free_bytes': int(required*1.02)})
    assert result['warnings'] == ['vram_margin']


def test_long_video_without_sr_does_not_get_blanket_length_warning():
    result = assess_video_export(1920, 1080, 108000, 30, {}, memory=MEMORY, gpu_memory=GPU)
    assert not result['warnings']
