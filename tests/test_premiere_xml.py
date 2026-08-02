from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from premiere_xml import generate_combined_xml, generate_individual_xmls


def _media_files(tmp_path: Path):
    source = tmp_path / "source.mp4"
    clips = [tmp_path / "clip-1.mp4", tmp_path / "clip-2.mp4"]
    shorts = [tmp_path / "short-1.mp4", tmp_path / "short-2.mp4"]
    for path in [source, *clips, *shorts]:
        path.write_bytes(path.stem.encode("utf-8"))
    return source, clips, shorts


def _highlights():
    return [
        {
            "title": "First",
            "start_sec": 10.0,
            "end_sec": 20.0,
        },
        {
            "title": "Second",
            "start_sec": 40.0,
            "end_sec": 55.0,
        },
    ]


def _track_ranges(track: ET.Element) -> list[tuple[int, int]]:
    return [
        (int(item.findtext("start")), int(item.findtext("end")))
        for item in track.findall("clipitem")
    ]


def test_combined_xml_places_source_clips_and_shorts_on_v1_v2_v3(tmp_path):
    source, clips, shorts = _media_files(tmp_path)
    video_info = {
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "duration": 90.0,
    }

    xml_path = generate_combined_xml(
        clips,
        _highlights(),
        video_info,
        tmp_path / "project.xml",
        project_name="Timeline",
        source_video_path=source,
        shorts_paths=shorts,
    )

    root = ET.parse(xml_path).getroot()
    sequences = root.findall(".//project/children/sequence")
    assert len(sequences) == 1
    sequence = sequences[0]
    assert sequence.findtext("duration") == "2700"

    video = sequence.find("./media/video")
    tracks = video.findall("track")
    assert len(tracks) == 3
    assert _track_ranges(tracks[0]) == [(0, 2700)]
    assert _track_ranges(tracks[1]) == [(300, 600), (1200, 1650)]
    assert _track_ranges(tracks[2]) == [(300, 600), (1200, 1650)]
    assert [
        item.findtext("name") for item in tracks[1].findall("clipitem")
    ] == ["First", "Second"]
    assert [
        item.findtext("name") for item in tracks[2].findall("clipitem")
    ] == ["First (Short)", "Second (Short)"]

    sequence_format = video.find("./format/samplecharacteristics")
    assert sequence_format.findtext("width") == "1920"
    assert sequence_format.findtext("height") == "1080"

    audio_tracks = sequence.findall("./media/audio/track")
    assert len(audio_tracks) == 2
    assert all(_track_ranges(track) == [(0, 2700)] for track in audio_tracks)

    files_by_name = {
        file_element.findtext("name"): file_element
        for file_element in root.findall(".//bin/children/clip/file")
    }
    short_video = files_by_name["short-1"].find(
        "./media/video/samplecharacteristics"
    )
    assert short_video.findtext("width") == "1080"
    assert short_video.findtext("height") == "1920"


def test_combined_xml_places_shorts_on_v2_when_no_normal_clips(tmp_path):
    source, _, shorts = _media_files(tmp_path)

    xml_path = generate_combined_xml(
        [],
        _highlights(),
        {"width": 1920, "height": 1080, "fps": 30.0, "duration": 90.0},
        tmp_path / "shorts-only.xml",
        source_video_path=source,
        shorts_paths=shorts,
    )

    sequence = ET.parse(xml_path).getroot().find(
        ".//project/children/sequence"
    )
    tracks = sequence.findall("./media/video/track")
    assert len(tracks) == 2
    assert _track_ranges(tracks[0]) == [(0, 2700)]
    assert _track_ranges(tracks[1]) == [(300, 600), (1200, 1650)]


def test_individual_xmls_keep_all_layers_at_original_time(tmp_path):
    source, clips, shorts = _media_files(tmp_path)
    output_dir = tmp_path / "xml"
    output_dir.mkdir()

    xml_paths = generate_individual_xmls(
        clips,
        _highlights(),
        {"width": 1920, "height": 1080, "fps": 30.0, "duration": 90.0},
        output_dir,
        source_video_path=source,
        shorts_paths=shorts,
    )

    assert len(xml_paths) == 2
    first_sequence = ET.parse(xml_paths[0]).getroot().find(
        ".//project/children/sequence"
    )
    tracks = first_sequence.findall("./media/video/track")
    assert len(tracks) == 3
    assert _track_ranges(tracks[0]) == [(0, 2700)]
    assert _track_ranges(tracks[1]) == [(300, 600)]
    assert _track_ranges(tracks[2]) == [(300, 600)]
