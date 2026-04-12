"""FCP XML (Final Cut Pro 7 XML) export for Premiere Pro."""

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from clipper import format_time_range


def generate_combined_xml(
    clip_paths: list[Path],
    srt_paths: list[Path],
    highlights: list[dict],
    video_info: dict,
    output_path: Path,
    project_name: str = "ClipExtractor Project",
) -> Path:
    """Generate a single FCP XML with multiple sequences (one per clip)."""
    xmeml = _create_xmeml()
    project = ET.SubElement(xmeml, "project")
    ET.SubElement(project, "name").text = project_name
    children = ET.SubElement(project, "children")

    # Add bin for media files
    media_bin = ET.SubElement(children, "bin")
    ET.SubElement(media_bin, "name").text = "Media"
    bin_children = ET.SubElement(media_bin, "children")

    for i, (clip_path, srt_path, highlight) in enumerate(
        zip(clip_paths, srt_paths, highlights), 1
    ):
        duration = highlight["end_sec"] - highlight["start_sec"]
        fps = video_info["fps"]
        frame_duration = int(duration * fps)

        # Determine resolution
        width = video_info["width"]
        height = video_info["height"]

        # Add clip file reference to bin
        file_id = f"file-{i}"
        clip_elem = ET.SubElement(bin_children, "clip", id=f"masterclip-{i}")
        ET.SubElement(clip_elem, "name").text = highlight["title"]
        _add_file_element(clip_elem, file_id, clip_path, width, height, fps, frame_duration)

        # Create sequence for this clip
        seq = _create_sequence(
            parent=children,
            name=highlight["title"],
            clip_path=clip_path,
            srt_path=srt_path,
            file_id=file_id,
            width=width,
            height=height,
            fps=fps,
            frame_duration=frame_duration,
            seq_index=i,
        )

    _write_xml(xmeml, output_path)
    return output_path


def generate_individual_xmls(
    clip_paths: list[Path],
    srt_paths: list[Path],
    highlights: list[dict],
    video_info: dict,
    output_dir: Path,
) -> list[Path]:
    """Generate individual FCP XML files, one per clip."""
    xml_paths = []

    for i, (clip_path, srt_path, highlight) in enumerate(
        zip(clip_paths, srt_paths, highlights), 1
    ):
        duration = highlight["end_sec"] - highlight["start_sec"]
        fps = video_info["fps"]
        frame_duration = int(duration * fps)
        width = video_info["width"]
        height = video_info["height"]

        xmeml = _create_xmeml()
        project = ET.SubElement(xmeml, "project")
        ET.SubElement(project, "name").text = highlight["title"]
        children = ET.SubElement(project, "children")

        file_id = f"file-1"
        _create_sequence(
            parent=children,
            name=highlight["title"],
            clip_path=clip_path,
            srt_path=srt_path,
            file_id=file_id,
            width=width,
            height=height,
            fps=fps,
            frame_duration=frame_duration,
            seq_index=1,
        )

        range_str = format_time_range(highlight["start_sec"], highlight["end_sec"])
        xml_path = output_dir / f"{range_str}.xml"
        _write_xml(xmeml, xml_path)
        xml_paths.append(xml_path)

    return xml_paths


def _create_xmeml() -> ET.Element:
    """Create root xmeml element."""
    return ET.Element("xmeml", version="4")


def _create_sequence(
    parent: ET.Element,
    name: str,
    clip_path: Path,
    srt_path: Path,
    file_id: str,
    width: int,
    height: int,
    fps: float,
    frame_duration: int,
    seq_index: int,
) -> ET.Element:
    """Create a sequence element with video and audio tracks."""
    seq = ET.SubElement(parent, "sequence", id=f"sequence-{seq_index}")
    ET.SubElement(seq, "name").text = name
    ET.SubElement(seq, "duration").text = str(frame_duration)

    rate = ET.SubElement(seq, "rate")
    ET.SubElement(rate, "timebase").text = str(round(fps))
    ET.SubElement(rate, "ntsc").text = "TRUE" if abs(fps - round(fps)) > 0.01 else "FALSE"

    # Timecode
    tc = ET.SubElement(seq, "timecode")
    tc_rate = ET.SubElement(tc, "rate")
    ET.SubElement(tc_rate, "timebase").text = str(round(fps))
    ET.SubElement(tc_rate, "ntsc").text = "FALSE"
    ET.SubElement(tc, "string").text = "00:00:00:00"
    ET.SubElement(tc, "frame").text = "0"
    ET.SubElement(tc, "displayformat").text = "NDF"

    media = ET.SubElement(seq, "media")

    # Video track
    video = ET.SubElement(media, "video")
    _add_format(video, width, height, fps)
    track = ET.SubElement(video, "track")

    clipitem = ET.SubElement(track, "clipitem", id=f"clipitem-v-{seq_index}")
    ET.SubElement(clipitem, "name").text = name
    ET.SubElement(clipitem, "start").text = "0"
    ET.SubElement(clipitem, "end").text = str(frame_duration)
    ET.SubElement(clipitem, "in").text = "0"
    ET.SubElement(clipitem, "out").text = str(frame_duration)

    _add_file_element(clipitem, file_id, clip_path, width, height, fps, frame_duration)

    # Audio tracks (stereo = 2 tracks)
    audio = ET.SubElement(media, "audio")
    for ch in range(1, 3):
        a_track = ET.SubElement(audio, "track")
        a_clipitem = ET.SubElement(a_track, "clipitem", id=f"clipitem-a{ch}-{seq_index}")
        ET.SubElement(a_clipitem, "name").text = name
        ET.SubElement(a_clipitem, "start").text = "0"
        ET.SubElement(a_clipitem, "end").text = str(frame_duration)
        ET.SubElement(a_clipitem, "in").text = "0"
        ET.SubElement(a_clipitem, "out").text = str(frame_duration)

        file_ref = ET.SubElement(a_clipitem, "file", id=file_id)

        source_track = ET.SubElement(a_clipitem, "sourcetrack")
        ET.SubElement(source_track, "mediatype").text = "audio"
        ET.SubElement(source_track, "trackindex").text = str(ch)

    return seq


def _add_file_element(
    parent: ET.Element,
    file_id: str,
    clip_path: Path,
    width: int,
    height: int,
    fps: float,
    frame_duration: int,
) -> ET.Element:
    """Add a file reference element."""
    file_elem = ET.SubElement(parent, "file", id=file_id)
    ET.SubElement(file_elem, "name").text = clip_path.stem
    ET.SubElement(file_elem, "pathurl").text = clip_path.resolve().as_uri()
    ET.SubElement(file_elem, "duration").text = str(frame_duration)

    rate = ET.SubElement(file_elem, "rate")
    ET.SubElement(rate, "timebase").text = str(round(fps))
    ET.SubElement(rate, "ntsc").text = "FALSE"

    file_media = ET.SubElement(file_elem, "media")

    f_video = ET.SubElement(file_media, "video")
    v_chars = ET.SubElement(f_video, "samplecharacteristics")
    ET.SubElement(v_chars, "width").text = str(width)
    ET.SubElement(v_chars, "height").text = str(height)

    f_audio = ET.SubElement(file_media, "audio")
    a_chars = ET.SubElement(f_audio, "samplecharacteristics")
    ET.SubElement(a_chars, "depth").text = "16"
    ET.SubElement(a_chars, "samplerate").text = "48000"

    return file_elem


def _add_format(parent: ET.Element, width: int, height: int, fps: float) -> None:
    """Add format element to video."""
    fmt = ET.SubElement(parent, "format")
    chars = ET.SubElement(fmt, "samplecharacteristics")
    ET.SubElement(chars, "width").text = str(width)
    ET.SubElement(chars, "height").text = str(height)
    rate = ET.SubElement(chars, "rate")
    ET.SubElement(rate, "timebase").text = str(round(fps))
    ET.SubElement(rate, "ntsc").text = "FALSE"


def _write_xml(root: ET.Element, output_path: Path) -> None:
    """Write XML with proper formatting and DOCTYPE."""
    rough = ET.tostring(root, encoding="unicode")
    dom = minidom.parseString(rough)

    # Add DOCTYPE
    doctype = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
    xml_body = dom.toprettyxml(indent="  ", encoding=None)
    # Remove the default XML declaration from minidom
    xml_body = "\n".join(xml_body.split("\n")[1:])

    output_path.write_text(doctype + xml_body, encoding="utf-8")
