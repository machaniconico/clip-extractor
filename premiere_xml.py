"""FCP XML (Final Cut Pro 7 XML) export for Premiere Pro."""

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from clipper import format_time_range


def generate_combined_xml(
    clip_paths: list[Path],
    highlights: list[dict],
    video_info: dict,
    output_path: Path,
    project_name: str = "ClipExtractor Project",
    *,
    source_video_path: Path,
    shorts_paths: list[Path] | None = None,
) -> Path:
    """Generate one source-length Premiere timeline as FCP XML.

    The original source is placed on V1. Normal clips are placed on V2 at
    their original source times. When normal clips and Shorts are both
    present, Shorts are placed on V3; for a Shorts-only export they use V2.
    SRT captions remain separate Premiere imports.
    """
    clip_paths = [Path(path) for path in clip_paths]
    shorts_paths = [Path(path) for path in (shorts_paths or [])]
    _validate_timeline_inputs(clip_paths, shorts_paths, highlights, video_info)

    xmeml = _create_xmeml()
    project = ET.SubElement(xmeml, "project")
    ET.SubElement(project, "name").text = project_name
    children = ET.SubElement(project, "children")

    source_path = Path(source_video_path)
    file_ids = _add_media_bin(
        children,
        source_path,
        clip_paths,
        shorts_paths,
        highlights,
        video_info,
    )
    _create_timeline_sequence(
        parent=children,
        name=project_name,
        source_path=source_path,
        clip_paths=clip_paths,
        shorts_paths=shorts_paths,
        highlights=highlights,
        video_info=video_info,
        file_ids=file_ids,
        seq_index=1,
    )

    _write_xml(xmeml, output_path)
    return output_path


def generate_individual_xmls(
    clip_paths: list[Path],
    highlights: list[dict],
    video_info: dict,
    output_dir: Path,
    *,
    source_video_path: Path,
    shorts_paths: list[Path] | None = None,
) -> list[Path]:
    """Generate one source-length layered FCP XML per highlight."""
    clip_paths = [Path(path) for path in clip_paths]
    shorts_paths = [Path(path) for path in (shorts_paths or [])]
    _validate_timeline_inputs(clip_paths, shorts_paths, highlights, video_info)

    xml_paths = []
    source_path = Path(source_video_path)

    for index, highlight in enumerate(highlights):
        selected_clips = [clip_paths[index]] if clip_paths else []
        selected_shorts = [shorts_paths[index]] if shorts_paths else []
        selected_highlights = [highlight]
        xmeml = _create_xmeml()
        project = ET.SubElement(xmeml, "project")
        title = str(highlight.get("title") or f"Highlight {index + 1}")
        ET.SubElement(project, "name").text = title
        children = ET.SubElement(project, "children")

        file_ids = _add_media_bin(
            children,
            source_path,
            selected_clips,
            selected_shorts,
            selected_highlights,
            video_info,
        )
        _create_timeline_sequence(
            parent=children,
            name=title,
            source_path=source_path,
            clip_paths=selected_clips,
            shorts_paths=selected_shorts,
            highlights=selected_highlights,
            video_info=video_info,
            file_ids=file_ids,
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


def _validate_timeline_inputs(
    clip_paths: list[Path],
    shorts_paths: list[Path],
    highlights: list[dict],
    video_info: dict,
) -> None:
    if not clip_paths and not shorts_paths:
        raise ValueError("Premiere XMLに配置する切り抜きがありません")
    expected = len(highlights)
    if expected == 0:
        raise ValueError("Premiere XMLに配置するハイライトがありません")
    if clip_paths and len(clip_paths) != expected:
        raise ValueError("通常切り抜きとハイライトの件数が一致しません")
    if shorts_paths and len(shorts_paths) != expected:
        raise ValueError("ショート動画とハイライトの件数が一致しません")
    if float(video_info.get("fps") or 0) <= 0:
        raise ValueError("元動画のFPSが不正です")
    if float(video_info.get("duration") or 0) <= 0:
        raise ValueError("元動画の長さが不正です")


def _highlight_frames(highlight: dict, fps: float, source_frames: int) -> tuple[int, int]:
    start = max(0, round(float(highlight["start_sec"]) * fps))
    end = max(start + 1, round(float(highlight["end_sec"]) * fps))
    start = min(start, source_frames - 1)
    end = min(max(start + 1, end), source_frames)
    return start, end


def _add_media_bin(
    parent: ET.Element,
    source_path: Path,
    clip_paths: list[Path],
    shorts_paths: list[Path],
    highlights: list[dict],
    video_info: dict,
) -> dict[str, list[str] | str]:
    media_bin = ET.SubElement(parent, "bin")
    ET.SubElement(media_bin, "name").text = "Media"
    bin_children = ET.SubElement(media_bin, "children")

    fps = float(video_info["fps"])
    source_frames = max(1, round(float(video_info["duration"]) * fps))
    source_id = "file-source"
    _add_master_clip(
        bin_children,
        "masterclip-source",
        source_id,
        source_path,
        source_path.stem,
        int(video_info["width"]),
        int(video_info["height"]),
        fps,
        source_frames,
    )

    clip_ids: list[str] = []
    for index, (path, highlight) in enumerate(zip(clip_paths, highlights), 1):
        file_id = f"file-clip-{index}"
        clip_ids.append(file_id)
        start, end = _highlight_frames(highlight, fps, source_frames)
        _add_master_clip(
            bin_children,
            f"masterclip-clip-{index}",
            file_id,
            path,
            str(highlight.get("title") or path.stem),
            int(video_info["width"]),
            int(video_info["height"]),
            fps,
            end - start,
        )

    short_ids: list[str] = []
    for index, (path, highlight) in enumerate(zip(shorts_paths, highlights), 1):
        file_id = f"file-short-{index}"
        short_ids.append(file_id)
        start, end = _highlight_frames(highlight, fps, source_frames)
        _add_master_clip(
            bin_children,
            f"masterclip-short-{index}",
            file_id,
            path,
            f"{highlight.get('title') or path.stem} (Short)",
            1080,
            1920,
            fps,
            end - start,
        )

    return {"source": source_id, "clips": clip_ids, "shorts": short_ids}


def _add_master_clip(
    parent: ET.Element,
    master_id: str,
    file_id: str,
    path: Path,
    name: str,
    width: int,
    height: int,
    fps: float,
    frame_duration: int,
) -> None:
    clip_elem = ET.SubElement(parent, "clip", id=master_id)
    ET.SubElement(clip_elem, "name").text = name
    _add_file_element(
        clip_elem,
        file_id,
        path,
        width,
        height,
        fps,
        frame_duration,
    )


def _create_timeline_sequence(
    parent: ET.Element,
    name: str,
    source_path: Path,
    clip_paths: list[Path],
    shorts_paths: list[Path],
    highlights: list[dict],
    video_info: dict,
    file_ids: dict[str, list[str] | str],
    seq_index: int,
) -> ET.Element:
    """Create V1 source, V2 normal clips, and optional V3 Shorts."""
    fps = float(video_info["fps"])
    width = int(video_info["width"])
    height = int(video_info["height"])
    source_frames = max(1, round(float(video_info["duration"]) * fps))

    seq = ET.SubElement(parent, "sequence", id=f"sequence-{seq_index}")
    ET.SubElement(seq, "name").text = name
    ET.SubElement(seq, "duration").text = str(source_frames)

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
    source_track = ET.SubElement(video, "track")
    _add_video_clipitem(
        source_track,
        f"clipitem-v1-{seq_index}",
        source_path.stem,
        str(file_ids["source"]),
        0,
        source_frames,
    )

    if clip_paths:
        clip_track = ET.SubElement(video, "track")
        _add_overlay_clipitems(
            clip_track,
            "v2",
            clip_paths,
            highlights,
            list(file_ids["clips"]),
            fps,
            source_frames,
            short=False,
        )

    if shorts_paths:
        short_track = ET.SubElement(video, "track")
        layer = "v3" if clip_paths else "v2"
        _add_overlay_clipitems(
            short_track,
            layer,
            shorts_paths,
            highlights,
            list(file_ids["shorts"]),
            fps,
            source_frames,
            short=True,
        )

    # Keep only the original source audio to prevent doubled/tripled playback.
    audio = ET.SubElement(media, "audio")
    for ch in range(1, 3):
        a_track = ET.SubElement(audio, "track")
        a_clipitem = ET.SubElement(
            a_track,
            "clipitem",
            id=f"clipitem-a{ch}-{seq_index}",
        )
        ET.SubElement(a_clipitem, "name").text = source_path.stem
        ET.SubElement(a_clipitem, "start").text = "0"
        ET.SubElement(a_clipitem, "end").text = str(source_frames)
        ET.SubElement(a_clipitem, "in").text = "0"
        ET.SubElement(a_clipitem, "out").text = str(source_frames)

        ET.SubElement(a_clipitem, "file", id=str(file_ids["source"]))

        source_audio = ET.SubElement(a_clipitem, "sourcetrack")
        ET.SubElement(source_audio, "mediatype").text = "audio"
        ET.SubElement(source_audio, "trackindex").text = str(ch)

    return seq


def _add_overlay_clipitems(
    track: ET.Element,
    layer: str,
    paths: list[Path],
    highlights: list[dict],
    file_ids: list[str],
    fps: float,
    source_frames: int,
    *,
    short: bool,
) -> None:
    for index, (path, highlight, file_id) in enumerate(
        zip(paths, highlights, file_ids),
        1,
    ):
        start, end = _highlight_frames(highlight, fps, source_frames)
        title = str(highlight.get("title") or path.stem)
        if short:
            title += " (Short)"
        _add_video_clipitem(
            track,
            f"clipitem-{layer}-{index}",
            title,
            file_id,
            start,
            end,
        )


def _add_video_clipitem(
    track: ET.Element,
    item_id: str,
    name: str,
    file_id: str,
    start: int,
    end: int,
) -> None:
    duration = end - start
    clipitem = ET.SubElement(track, "clipitem", id=item_id)
    ET.SubElement(clipitem, "name").text = name
    ET.SubElement(clipitem, "start").text = str(start)
    ET.SubElement(clipitem, "end").text = str(end)
    ET.SubElement(clipitem, "in").text = "0"
    ET.SubElement(clipitem, "out").text = str(duration)
    ET.SubElement(clipitem, "file", id=file_id)


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
