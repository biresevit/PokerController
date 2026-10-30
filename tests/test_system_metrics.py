from poker_controller.system_metrics import sample


def test_machine_metric_sample_has_bounded_percentages_and_disk_capacity():
    result=sample()
    assert 0<=result["cpu_percent"]<=100
    assert 0<=result["memory_percent"]<=100
    assert 0<=result["disk_percent"]<=100
    assert 0<=result["disk_free_bytes"]<=result["disk_total_bytes"]
    assert result["poker_controller_version"]
    assert result["pdecision_version"] is None or isinstance(result["pdecision_version"],str)


def test_version_lookup_does_not_require_source_checkout_or_git(monkeypatch):
    from poker_controller import system_metrics
    monkeypatch.setattr(system_metrics,'version',lambda name:'0.1.0')
    assert system_metrics._controller_version()=='0.1.0'
