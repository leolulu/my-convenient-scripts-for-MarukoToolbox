from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

from pgs_subtitle import PgsSubtitleError, normalize_sup_file


PCS = 0x16
WDS = 0x17
PDS = 0x14
ODS = 0x15
END = 0x80


class Segment(NamedTuple):
    raw: bytes
    pts: int
    dts: int
    type: int
    payload: bytes


def make_segment(segment_type: int, payload: bytes = b"", pts: int = 90_000) -> bytes:
    return (
        struct.pack(">2sIIBH", b"PG", pts, 0, segment_type, len(payload))
        + payload
    )


def make_composition_object(
    object_id: int,
    *,
    x: int = 100,
    y: int = 200,
    flags: int = 0,
    crop: tuple[int, int, int, int] | None = None,
    window_id: int = 0,
) -> bytes:
    payload = struct.pack(">HBBHH", object_id, window_id, flags, x, y)
    if crop is not None:
        payload += struct.pack(">HHHH", *crop)
    return payload


def make_pcs(objects: list[bytes], pts: int = 90_000) -> bytes:
    payload = struct.pack(">HHBHBBBB", 1920, 1080, 0, 1, 0x80, 0, 0, len(objects))
    return make_segment(PCS, payload + b"".join(objects), pts)


def encode_rows(rows: list[list[int]]) -> bytes:
    encoded = bytearray()
    for row in rows:
        for color in row:
            if color:
                encoded.append(color)
            else:
                encoded.extend((0, 1))
        encoded.extend((0, 0))
    return bytes(encoded)


def make_ods(
    object_id: int,
    rows: list[list[int]],
    *,
    version: int = 3,
    fragment_size: int = 65_535,
    pts: int = 90_000,
) -> list[bytes]:
    width = len(rows[0])
    height = len(rows)
    rle = encode_rows(rows)
    data_length = len(rle) + 4
    fragments = [
        rle[position : position + fragment_size]
        for position in range(0, len(rle), fragment_size)
    ]
    segments = []
    for index, fragment in enumerate(fragments):
        first = index == 0
        last = index == len(fragments) - 1
        if first:
            sequence = 0xC0 if last else 0x80
        else:
            sequence = 0x40 if last else 0
        payload = struct.pack(">HBB", object_id, version, sequence)
        if first:
            payload += data_length.to_bytes(3, "big")
            payload += struct.pack(">HH", width, height)
        segments.append(make_segment(ODS, payload + fragment, pts))
    return segments


def make_display_set(
    composition_objects: list[bytes],
    ods_segments: list[bytes] | None = None,
    *,
    pts: int = 90_000,
    include_palette_and_window: bool = False,
) -> bytes:
    segments = [make_pcs(composition_objects, pts)]
    if include_palette_and_window:
        window = struct.pack(">BBHHHH", 1, 0, 0, 0, 1920, 1080)
        palette = b"\x00\x01" + bytes((1, 255, 128, 64, 255))
        segments.extend((make_segment(WDS, window, pts), make_segment(PDS, palette, pts)))
    segments.extend(ods_segments or [])
    segments.append(make_segment(END, pts=pts))
    return b"".join(segments)


def parse_sup(data: bytes) -> list[list[Segment]]:
    display_sets: list[list[Segment]] = []
    current: list[Segment] = []
    position = 0
    while position < len(data):
        magic, pts, dts, segment_type, size = struct.unpack_from(">2sIIBH", data, position)
        if magic != b"PG":
            raise AssertionError("bad test output magic")
        end = position + 13 + size
        payload = data[position + 13 : end]
        segment = Segment(data[position:end], pts, dts, segment_type, payload)
        current.append(segment)
        position = end
        if segment_type == END:
            display_sets.append(current)
            current = []
    if current or position != len(data):
        raise AssertionError("bad test output segment boundary")
    return display_sets


def parse_composition_objects(payload: bytes) -> list[dict[str, int | None]]:
    count = payload[10]
    position = 11
    objects: list[dict[str, int | None]] = []
    for _ in range(count):
        object_id, window_id, flags, x, y = struct.unpack_from(">HBBHH", payload, position)
        position += 8
        item: dict[str, int | None] = {
            "id": object_id,
            "window_id": window_id,
            "flags": flags,
            "x": x,
            "y": y,
            "crop_x": None,
            "crop_y": None,
            "crop_w": None,
            "crop_h": None,
        }
        if flags & 0x80:
            values = struct.unpack_from(">HHHH", payload, position)
            position += 8
            item.update(zip(("crop_x", "crop_y", "crop_w", "crop_h"), values))
        objects.append(item)
    if position != len(payload):
        raise AssertionError("bad test output PCS size")
    return objects


def decode_rle(width: int, height: int, data: bytes) -> list[list[int]]:
    rows: list[list[int]] = []
    position = 0
    for _ in range(height):
        row: list[int] = []
        while True:
            first = data[position]
            position += 1
            if first:
                row.append(first)
            else:
                second = data[position]
                position += 1
                if second == 0:
                    break
                if second < 0x40:
                    count, color = second, 0
                elif second < 0x80:
                    count = ((second & 0x3F) << 8) | data[position]
                    position += 1
                    color = 0
                elif second < 0xC0:
                    count = second & 0x3F
                    color = data[position]
                    position += 1
                else:
                    count = ((second & 0x3F) << 8) | data[position]
                    color = data[position + 1]
                    position += 2
                row.extend([color] * count)
        if len(row) != width:
            raise AssertionError("bad test output RLE row width")
        rows.append(row)
    if position != len(data):
        raise AssertionError("bad test output RLE height")
    return rows


def parse_objects(display_set: list[Segment]) -> dict[int, dict[str, object]]:
    pending: dict[int, dict[str, object]] = {}
    completed: dict[int, dict[str, object]] = {}
    for segment in display_set:
        if segment.type != ODS:
            continue
        payload = segment.payload
        object_id, version, sequence = struct.unpack_from(">HBB", payload)
        if sequence in (0x80, 0xC0):
            data_length = int.from_bytes(payload[4:7], "big")
            width, height = struct.unpack_from(">HH", payload, 7)
            item: dict[str, object] = {
                "id": object_id,
                "version": version,
                "width": width,
                "height": height,
                "data_length": data_length,
                "rle_parts": [payload[11:]],
                "sequences": [sequence],
                "payload_sizes": [len(payload)],
            }
            pending[object_id] = item
        else:
            item = pending[object_id]
            item["rle_parts"].append(payload[4:])  # type: ignore[union-attr]
            item["sequences"].append(sequence)  # type: ignore[union-attr]
            item["payload_sizes"].append(len(payload))  # type: ignore[union-attr]
        if sequence in (0x40, 0xC0):
            item = pending.pop(object_id)
            rle = b"".join(item["rle_parts"])  # type: ignore[arg-type]
            if len(rle) + 4 != item["data_length"]:
                raise AssertionError("bad test output data_length")
            item["pixels"] = decode_rle(item["width"], item["height"], rle)  # type: ignore[arg-type]
            completed[object_id] = item
    if pending:
        raise AssertionError("incomplete test output ODS")
    return completed


class PgsSubtitleTests(unittest.TestCase):
    def normalize(self, source_data: bytes) -> tuple[int, bytes]:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "input.sup"
            destination = root / "output.sup"
            source.write_bytes(source_data)
            count = normalize_sup_file(source, destination)
            output = destination.read_bytes() if count else b""
            self.assertEqual(source.read_bytes(), source_data)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["input.sup", "output.sup"] if count else ["input.sup"],
            )
            return count, output

    def test_no_crop_preserves_source_and_does_not_touch_destination(self) -> None:
        source_data = make_display_set(
            [make_composition_object(7, flags=0x40)],
            make_ods(7, [[1, 2], [3, 4]]),
        )
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "input.sup"
            destination = root / "output.sup"
            source.write_bytes(source_data)
            destination.write_bytes(b"existing output")

            count = normalize_sup_file(source, destination)

            self.assertEqual(count, 0)
            self.assertEqual(source.read_bytes(), source_data)
            self.assertEqual(destination.read_bytes(), b"existing output")
            destination.unlink()
            self.assertEqual(normalize_sup_file(source, destination), 0)
            self.assertFalse(destination.exists())

    def test_crop_changes_only_bitmap_and_cropping_fields(self) -> None:
        rows = [[1, 1, 2, 2], [3, 4, 4, 5], [6, 6, 7, 8]]
        source_data = make_display_set(
            [
                make_composition_object(
                    7,
                    x=321,
                    y=123,
                    flags=0xC0,
                    crop=(1, 1, 2, 2),
                )
            ],
            make_ods(7, rows),
            include_palette_and_window=True,
        )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "input.sup"
            destination = root / "output.sup"
            source.write_bytes(source_data)
            count = normalize_sup_file(source, destination)
            original_sets = parse_sup(source_data)
            output_sets = parse_sup(destination.read_bytes())

        self.assertEqual(count, 1)
        composition = parse_composition_objects(output_sets[0][0].payload)[0]
        self.assertEqual(composition["id"], 7)
        self.assertEqual(composition["flags"], 0x40)
        self.assertEqual((composition["x"], composition["y"]), (321, 123))
        self.assertIsNone(composition["crop_x"])
        output_object = parse_objects(output_sets[0])[7]
        self.assertEqual((output_object["width"], output_object["height"]), (2, 2))
        self.assertEqual(output_object["pixels"], [[4, 4], [6, 7]])
        self.assertEqual(output_sets[0][0].pts, original_sets[0][0].pts)
        self.assertEqual(output_sets[0][1].raw, original_sets[0][1].raw)
        self.assertEqual(output_sets[0][2].raw, original_sets[0][2].raw)

    def test_fragmented_object_is_cropped_and_refragmented_within_segment_limit(self) -> None:
        rows = [
            [((x + y) % 255) + 1 for x in range(257)]
            for y in range(300)
        ]
        source_data = make_display_set(
            [make_composition_object(9, crop=(0, 1, 257, 299), flags=0x80)],
            make_ods(9, rows, fragment_size=503),
        )

        count, output = self.normalize(source_data)

        self.assertEqual(count, 1)
        output_object = parse_objects(parse_sup(output)[0])[9]
        self.assertEqual(output_object["pixels"], rows[1:])
        self.assertGreaterEqual(len(output_object["sequences"]), 2)
        self.assertEqual(output_object["sequences"][0], 0x80)
        self.assertEqual(output_object["sequences"][-1], 0x40)
        self.assertTrue(
            all(size <= 65_535 for size in output_object["payload_sizes"])
        )

    def test_uncropped_reuse_reoutputs_original_object(self) -> None:
        rows = [[1, 2, 3], [4, 5, 6]]
        first = make_display_set(
            [make_composition_object(7, crop=(1, 0, 2, 2), flags=0x80)],
            make_ods(7, rows),
            pts=90_000,
        )
        second = make_display_set(
            [make_composition_object(7, flags=0x40)],
            pts=180_000,
        )

        count, output = self.normalize(first + second)

        self.assertEqual(count, 1)
        output_sets = parse_sup(output)
        self.assertEqual(parse_objects(output_sets[0])[7]["pixels"], [[2, 3], [5, 6]])
        restored = parse_objects(output_sets[1])[7]
        self.assertEqual((restored["width"], restored["height"]), (3, 2))
        self.assertEqual(restored["pixels"], rows)
        second_pcs = next(segment for segment in output_sets[1] if segment.type == PCS)
        composition = parse_composition_objects(second_pcs.payload)[0]
        self.assertEqual(composition["flags"], 0x40)

    def test_different_crops_of_one_object_receive_independent_bitmaps(self) -> None:
        rows = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]
        source_data = make_display_set(
            [
                make_composition_object(5, crop=(0, 0, 2, 2), flags=0x80),
                make_composition_object(5, crop=(2, 1, 2, 2), flags=0x80),
            ],
            make_ods(5, rows),
        )

        count, output = self.normalize(source_data)

        self.assertEqual(count, 2)
        display_set = parse_sup(output)[0]
        pcs = next(segment for segment in display_set if segment.type == PCS)
        compositions = parse_composition_objects(pcs.payload)
        first_id = compositions[0]["id"]
        second_id = compositions[1]["id"]
        self.assertNotEqual(first_id, second_id)
        objects = parse_objects(display_set)
        self.assertEqual(objects[first_id]["pixels"], [[1, 2], [5, 6]])
        self.assertEqual(objects[second_id]["pixels"], [[7, 8], [11, 12]])

    def test_full_object_crop_only_crops_pcs_flags(self) -> None:
        rows = [[0, 2], [3, 0]]
        source_data = make_display_set(
            [make_composition_object(4, crop=(0, 0, 2, 2), flags=0xC0)],
            make_ods(4, rows),
        )

        count, output = self.normalize(source_data)

        self.assertEqual(count, 1)
        display_set = parse_sup(output)[0]
        pcs = next(segment for segment in display_set if segment.type == PCS)
        self.assertEqual(parse_composition_objects(pcs.payload)[0]["flags"], 0x40)
        self.assertEqual(parse_objects(display_set)[4]["pixels"], rows)

    def test_invalid_crop_raises_and_leaves_no_output_or_temporary_file(self) -> None:
        source_data = make_display_set(
            [make_composition_object(7, crop=(1, 0, 2, 2), flags=0x80)],
            make_ods(7, [[1, 2], [3, 4]]),
        )
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "input.sup"
            destination = root / "output.sup"
            source.write_bytes(source_data)

            with self.assertRaisesRegex(PgsSubtitleError, "裁剪区域越界"):
                normalize_sup_file(source, destination)

            self.assertEqual(source.read_bytes(), source_data)
            self.assertFalse(destination.exists())
            self.assertEqual([path.name for path in root.iterdir()], ["input.sup"])

    def test_cropped_reference_to_missing_object_is_rejected(self) -> None:
        source_data = make_display_set(
            [make_composition_object(42, crop=(0, 0, 1, 1), flags=0x80)]
        )
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "input.sup"
            destination = root / "output.sup"
            source.write_bytes(source_data)

            with self.assertRaisesRegex(PgsSubtitleError, "缺失的对象 42"):
                normalize_sup_file(source, destination)

            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
