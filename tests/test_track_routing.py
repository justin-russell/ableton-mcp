"""Tests for get_track_routing / set_track_routing on the Remote Script."""

import pytest

from test_remote_script_bridge import _load_script_module, make_instance


@pytest.fixture
def script_module():
    return _load_script_module()


class _Rt(object):
    def __init__(self, name):
        self.display_name = name


class _Track(object):
    """Live 10+ track with RoutingType / RoutingChannel objects."""

    def __init__(self, name):
        self.name = name
        self.available_output_routing_types = [_Rt("Master"), _Rt("Sends Only"), _Rt("3-SC Bus")]
        self.output_routing_type = self.available_output_routing_types[0]
        self.available_output_routing_channels = [_Rt("Track In")]
        self.output_routing_channel = self.available_output_routing_channels[0]
        self.available_input_routing_types = [_Rt("Ext. In"), _Rt("2-Kick"), _Rt("No Input")]
        self.input_routing_type = self.available_input_routing_types[0]
        self.available_input_routing_channels = [_Rt("Pre FX"), _Rt("Post FX"), _Rt("Post Mixer")]
        self.input_routing_channel = self.available_input_routing_channels[0]
        self.current_monitoring_state = 1


class _LegacyTrack(object):
    """Older string-based routing API."""

    def __init__(self, name):
        self.name = name
        self.output_routings = ["Master", "Sends Only", "3-SC Bus"]
        self.current_output_routing = "Master"
        self.output_sub_routings = ["Track In"]
        self.current_output_sub_routing = "Track In"
        self.input_routings = ["Ext. In", "No Input"]
        self.current_input_routing = "Ext. In"
        self.current_monitoring_state = 1


class _Song(object):
    def __init__(self, tracks):
        self.tracks = tracks


def _inst(script_module, *tracks):
    inst = make_instance(script_module)
    inst._song = _Song(list(tracks))
    return inst


def test_get_routing_lists_current_and_options(script_module):
    inst = _inst(script_module, _Track("Rumble"))
    r = inst._get_track_routing(0)
    assert r["name"] == "Rumble"
    assert r["output"]["current"] == "Master"
    assert r["output"]["options"] == ["Master", "Sends Only", "3-SC Bus"]
    assert r["input"]["channel_options"] == ["Pre FX", "Post FX", "Post Mixer"]
    assert r["monitoring"] == "Auto"


def test_set_output_to_another_track_by_partial_name(script_module):
    track = _Track("Rumble")
    inst = _inst(script_module, track)
    r = inst._set_track_routing(0, output="sc bus")
    assert track.output_routing_type.display_name == "3-SC Bus"
    assert r["output"]["current"] == "3-SC Bus"


def test_set_input_channel_and_monitoring(script_module):
    track = _Track("SC Trigger")
    inst = _inst(script_module, track)
    inst._set_track_routing(0, input="2-Kick", input_channel="Post FX", monitoring="in")
    assert track.input_routing_type.display_name == "2-Kick"
    assert track.input_routing_channel.display_name == "Post FX"
    assert track.current_monitoring_state == 0


def test_ambiguous_or_unknown_names_are_rejected_with_options(script_module):
    track = _Track("Rumble")
    inst = _inst(script_module, track)
    with pytest.raises(ValueError) as err:
        inst._set_track_routing(0, input_channel="Post")  # Post FX vs Post Mixer
    assert "Post FX" in str(err.value) and "Post Mixer" in str(err.value)
    with pytest.raises(ValueError):
        inst._set_track_routing(0, output="Nowhere")
    with pytest.raises(ValueError):
        inst._set_track_routing(0, monitoring="Loud")
    assert track.output_routing_type.display_name == "Master"  # unchanged


def test_legacy_string_api(script_module):
    track = _LegacyTrack("Rumble")
    inst = _inst(script_module, track)
    inst._set_track_routing(0, output="Sends Only")
    assert track.current_output_routing == "Sends Only"
    assert inst._get_track_routing(0)["output"]["options"] == ["Master", "Sends Only", "3-SC Bus"]


def test_track_index_out_of_range(script_module):
    inst = _inst(script_module, _Track("Rumble"))
    with pytest.raises(IndexError):
        inst._get_track_routing(3)


def test_routing_commands_dispatch(script_module):
    track = _Track("Rumble")
    inst = _inst(script_module, track)
    r = inst._process_command({"type": "set_track_routing",
                               "params": {"track_index": 0, "output": "SC Bus"}})
    assert r["status"] == "success"
    assert r["result"]["output"]["current"] == "3-SC Bus"
    r = inst._process_command({"type": "get_track_routing", "params": {"track_index": 0}})
    assert r["result"]["output"]["current"] == "3-SC Bus"
