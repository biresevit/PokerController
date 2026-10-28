from poker_controller.system_metrics import sample


def test_machine_metric_sample_has_bounded_percentages_and_disk_capacity():
    result=sample()
    assert 0<=result["cpu_percent"]<=100
    assert 0<=result["memory_percent"]<=100
    assert 0<=result["disk_percent"]<=100
    assert 0<=result["disk_free_bytes"]<=result["disk_total_bytes"]
