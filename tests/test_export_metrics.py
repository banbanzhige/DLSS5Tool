from dlss5tool.export_metrics import ExportTelemetry, FrameRate


def test_rate_excludes_initialization_and_duplicate_callbacks():
    rate = FrameRate()
    assert rate.update(0, 1000, now=0)['fps'] is None
    assert rate.update(1, 1000, now=60)['fps'] is None
    assert rate.update(1, 1000, now=61)['fps'] is None
    result = rate.update(21, 1000, now=62)
    assert result['fps'] == 10
    assert result['remaining_seconds'] == 97.9


def test_rate_adapts_to_stall_and_new_job_without_negative_eta():
    rate = FrameRate(window=10)
    rate.update(1, 100, now=0)
    assert rate.update(21, 100, now=2)['fps'] == 10
    assert rate.update(21, 100, now=6)['fps'] < 4
    assert rate.update(21, 100, now=20)['fps'] is None
    assert rate.update(1, 100, now=21)['fps'] is None
    assert rate.update(100, 100, now=24)['remaining_seconds'] == 0


def test_metrics_are_bounded_and_do_not_attribute_global_memory():
    tick = [0.0]
    telemetry = ExportTelemetry(clock=lambda: tick[0], memory_reader=lambda: {
        'free_physical_bytes': 200, 'free_commit_bytes': 300})
    for index in range(200):
        tick[0] = index*2.0
        telemetry.record('sr', .25)
    result = telemetry.snapshot()
    assert len(result['system_memory_samples']) == 64
    assert result['stages']['sr']['calls'] == 200
    assert result['stages']['sr']['warm_calls'] == 196
    assert result['stages']['sr']['seconds'] == 50
    assert 'overlapping' in result['scope']
