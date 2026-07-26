"""Tests for the read-only MCP tools."""

from __future__ import annotations

from _mcp_state_factory import make_sample, make_state, make_zone

from ecoloop.mcp import tools_observe


class TestNoDataAvailable:
    """A cold server with nothing published must degrade gracefully, not raise."""

    def test_zone_telemetry_is_empty(self) -> None:
        assert tools_observe.get_zone_telemetry(make_state()) == ()

    def test_site_conditions_is_none(self) -> None:
        assert tools_observe.get_site_conditions(make_state()) is None

    def test_comfort_status_reports_no_samples(self) -> None:
        result = tools_observe.get_comfort_status(make_state())
        assert result.any_samples_available is False
        assert result.zones == ()

    def test_energy_totals_is_zero(self) -> None:
        result = tools_observe.get_energy_totals(make_state())
        assert result.samples_in_window == 0
        assert result.total_kwh == 0.0

    def test_weather_forecast_is_empty_without_a_sample(self) -> None:
        state = make_state()
        assert tools_observe.get_weather_forecast(state, hours_ahead=6) == ()


class TestZoneTelemetry:
    def test_returns_all_zones_when_none_specified(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(
            make_sample(zones=(make_zone("CORE_ZN"), make_zone("PERIMETER_ZN_1")))
        )
        results = tools_observe.get_zone_telemetry(state)
        assert {r.zone for r in results} == {"CORE_ZN", "PERIMETER_ZN_1"}

    def test_filters_to_one_zone_case_insensitively(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        results = tools_observe.get_zone_telemetry(state, zone="core_zn")
        assert len(results) == 1
        assert results[0].zone == "CORE_ZN"

    def test_unknown_zone_returns_empty(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        assert tools_observe.get_zone_telemetry(state, zone="NOT_A_ZONE") == ()


class TestComfortStatus:
    def test_identifies_the_worst_offender(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(
            make_sample(
                zones=(
                    make_zone("CORE_ZN", pmv=0.1),
                    make_zone("PERIMETER_ZN_1", pmv=-1.8),
                    make_zone("PERIMETER_ZN_2", pmv=0.9),
                )
            )
        )
        result = tools_observe.get_comfort_status(state)
        assert result.worst_zone == "PERIMETER_ZN_1"
        assert result.worst_abs_pmv == 1.8

    def test_zone_without_pmv_reports_within_ashrae_55_as_none(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(
            make_sample(zones=(make_zone("CORE_ZN", pmv=None, ppd_pct=None),))
        )
        result = tools_observe.get_comfort_status(state)
        assert result.zones[0].within_ashrae_55 is None

    def test_pmv_outside_band_is_flagged(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN", pmv=1.2),)))
        result = tools_observe.get_comfort_status(state)
        assert result.zones[0].within_ashrae_55 is False

    def test_pmv_inside_band_is_not_flagged(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN", pmv=0.1),)))
        result = tools_observe.get_comfort_status(state)
        assert result.zones[0].within_ashrae_55 is True


class TestEnergyTotals:
    def test_defaults_to_the_configured_aggregate_window(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        result = tools_observe.get_energy_totals(state)
        assert result.window_minutes == state.settings.bus.aggregate_window_minutes

    def test_custom_window_is_respected(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        result = tools_observe.get_energy_totals(state, window_minutes=30.0)
        assert result.window_minutes == 30.0


class TestSignals:
    def test_carbon_intensity_defaults_to_current_hour(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),), hour=12))
        result = tools_observe.get_carbon_intensity(state)
        assert result.hour_of_day == 12
        assert result.unit == "gCO2/kWh"

    def test_carbon_intensity_shifts_forward_and_wraps(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),), hour=22))
        result = tools_observe.get_carbon_intensity(state, hours_ahead=4)
        assert result.hour_of_day == 2  # 22 + 4, wrapped past midnight

    def test_tariff_reflects_the_configured_signal(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),), hour=9))
        result = tools_observe.get_tariff(state)
        assert result.value > 0


class TestDemandStatus:
    def test_no_electricity_reports_zero_demand(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        result = tools_observe.get_demand_status(state)
        assert result.rolling_average_kw == 0.0
        assert result.approaching_cap is False
