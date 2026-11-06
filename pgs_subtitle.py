"""规范化 SUP/PGS 字幕中的裁剪合成对象。"""

from __future__ import annotations

import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SEGMENT_HEADER_SIZE = 13
_PCS = 0x16
_WDS = 0x17
_PDS = 0x14
_ODS = 0x15
_END = 0x80
_MAX_SEGMENT_PAYLOAD = 0xFFFF
_MAX_OBJECT_DATA_LENGTH = 0xFFFFFF


class PgsSubtitleError(Exception):
    """SUP/PGS 字幕解析或规范化失败。"""


@dataclass(frozen=True)
class _Segment:
    raw: bytes
    segment_type: int
    payload: bytes


@dataclass(frozen=True)
class _ObjectData:
    object_id: int
    version: int
    width: int
    height: int
    rle: bytes


@dataclass(frozen=True)
class _OdsGroup:
    segment_indices: tuple[int, ...]
    object: _ObjectData


@dataclass(frozen=True)
class _CompositionObject:
    object_id: int
    window_id: int
    flags: int
    x: int
    y: int
    crop: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class _Pcs:
    segment_index: int
    prefix: bytes
    objects: tuple[_CompositionObject, ...]


@dataclass(frozen=True)
class _DisplaySet:
    segments: tuple[_Segment, ...]
    pcs: tuple[_Pcs, ...]
    ods: dict[int, _OdsGroup]


@dataclass(frozen=True)
class _Reference:
    pcs: _Pcs
    object_index: int
    source: _ObjectData
    crop: tuple[int, int, int, int] | None
    needs_crop: bool


@dataclass
class _PendingOds:
    object_id: int
    version: int
    data_length: int
    width: int
    height: int
    segment_indices: list[int]
    fragments: list[bytes]


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _decode_rle(
    width: int,
    height: int,
    data: bytes,
    object_id: int,
) -> tuple[bytes, ...]:
    rows: list[bytes] = []
    position = 0

    for row_index in range(height):
        row = bytearray()
        while True:
            if position >= len(data):
                raise PgsSubtitleError(
                    f"对象 {object_id} 的 RLE 数据在第 {row_index + 1} 行意外结束"
                )
            first = data[position]
            position += 1
            if first:
                row.append(first)
            else:
                if position >= len(data):
                    raise PgsSubtitleError(
                        f"对象 {object_id} 的 RLE 转义序列不完整"
                    )
                second = data[position]
                position += 1
                if second == 0:
                    if len(row) != width:
                        raise PgsSubtitleError(
                            f"对象 {object_id} 的第 {row_index + 1} 行宽度为 "
                            f"{len(row)}，应为 {width}"
                        )
                    break

                operation = second & 0xC0
                if operation == 0:
                    count = second
                    color = 0
                elif operation == 0x40:
                    if position >= len(data):
                        raise PgsSubtitleError(
                            f"对象 {object_id} 的 RLE 长透明行程不完整"
                        )
                    count = ((second & 0x3F) << 8) | data[position]
                    position += 1
                    color = 0
                elif operation == 0x80:
                    count = second & 0x3F
                    if position >= len(data):
                        raise PgsSubtitleError(
                            f"对象 {object_id} 的 RLE 着色行程不完整"
                        )
                    color = data[position]
                    position += 1
                else:
                    if position + 1 >= len(data):
                        raise PgsSubtitleError(
                            f"对象 {object_id} 的 RLE 长着色行程不完整"
                        )
                    count = ((second & 0x3F) << 8) | data[position]
                    color = data[position + 1]
                    position += 2

                if count == 0:
                    raise PgsSubtitleError(
                        f"对象 {object_id} 的 RLE 包含长度为零的行程"
                    )
                row.extend(bytes((color,)) * count)
                if len(row) > width:
                    raise PgsSubtitleError(
                        f"对象 {object_id} 的第 {row_index + 1} 行超出声明宽度 "
                        f"{width}"
                    )
        rows.append(bytes(row))

    if position != len(data):
        raise PgsSubtitleError(
            f"对象 {object_id} 的 RLE 在声明的 {height} 行之后仍有冗余数据"
        )
    return tuple(rows)


def _append_run(output: bytearray, color: int, count: int) -> None:
    while count:
        run_length = min(count, 0x3FFF)
        if color == 0:
            if run_length <= 0x3F:
                output.extend((0, run_length))
            else:
                output.extend(
                    (0, 0x40 | (run_length >> 8), run_length & 0xFF)
                )
        elif run_length <= 0x3F:
            output.extend((0, 0x80 | run_length, color))
        else:
            output.extend(
                (
                    0,
                    0xC0 | (run_length >> 8),
                    run_length & 0xFF,
                    color,
                )
            )
        count -= run_length


def _encode_rle(rows: tuple[bytes, ...]) -> bytes:
    output = bytearray()
    for row in rows:
        position = 0
        while position < len(row):
            color = row[position]
            end = position + 1
            while end < len(row) and row[end] == color:
                end += 1
            count = end - position
            if color and count <= 2:
                output.extend(bytes((color,)) * count)
            else:
                _append_run(output, color, count)
            position = end
        output.extend((0, 0))
    return bytes(output)


def _parse_pcs(payload: bytes, segment_index: int) -> _Pcs:
    if len(payload) < 11:
        raise PgsSubtitleError("PCS 段长度不足 11 字节")

    prefix = payload[:11]
    object_count = payload[10]
    position = 11
    objects: list[_CompositionObject] = []
    for object_index in range(object_count):
        if position + 8 > len(payload):
            raise PgsSubtitleError(
                f"PCS 第 {object_index + 1} 个合成对象的固定字段不完整"
            )
        object_id, window_id, flags, x, y = struct.unpack_from(
            ">HBBHH", payload, position
        )
        position += 8
        crop = None
        if flags & 0x80:
            if position + 8 > len(payload):
                raise PgsSubtitleError(
                    f"PCS 第 {object_index + 1} 个合成对象的裁剪字段不完整"
                )
            crop = struct.unpack_from(">HHHH", payload, position)
            position += 8
        objects.append(
            _CompositionObject(object_id, window_id, flags, x, y, crop)
        )

    if position != len(payload):
        raise PgsSubtitleError("PCS 段包含对象数量字段之外的冗余数据")
    return _Pcs(segment_index, prefix, tuple(objects))


def _finish_ods(pending: _PendingOds) -> _OdsGroup:
    rle = b"".join(pending.fragments)
    expected_length = pending.data_length - 4
    if len(rle) != expected_length:
        raise PgsSubtitleError(
            f"对象 {pending.object_id} 的 ODS 数据长度为 {len(rle)}，"
            f"data_length 声明为 {expected_length}"
        )
    object_data = _ObjectData(
        pending.object_id,
        pending.version,
        pending.width,
        pending.height,
        rle,
    )
    return _OdsGroup(tuple(pending.segment_indices), object_data)


def _parse_display_set(segments: list[_Segment]) -> _DisplaySet:
    pcs_segments: list[_Pcs] = []
    ods_groups: dict[int, _OdsGroup] = {}
    pending: dict[int, _PendingOds] = {}

    for index, segment in enumerate(segments):
        payload = segment.payload
        if segment.segment_type == _PCS:
            pcs_segments.append(_parse_pcs(payload, index))
        elif segment.segment_type == _WDS:
            if not payload or (len(payload) - 1) % 9:
                raise PgsSubtitleError("WDS 段的窗口定义长度无效")
            if payload[0] != (len(payload) - 1) // 9:
                raise PgsSubtitleError("WDS 段的窗口数量与实际长度不符")
        elif segment.segment_type == _PDS:
            if len(payload) < 2 or (len(payload) - 2) % 5:
                raise PgsSubtitleError("PDS 段的调色板条目长度无效")
        elif segment.segment_type == _ODS:
            if len(payload) < 4:
                raise PgsSubtitleError("ODS 段长度不足 4 字节")
            object_id, version, sequence = struct.unpack_from(">HBB", payload)
            if sequence in (0x80, 0xC0):
                if object_id in pending or object_id in ods_groups:
                    raise PgsSubtitleError(
                        f"对象 {object_id} 的 ODS 序列重复或未完整结束"
                    )
                if len(payload) < 11:
                    raise PgsSubtitleError("首个 ODS 段长度不足 11 字节")
                data_length = int.from_bytes(payload[4:7], "big")
                if data_length < 4:
                    raise PgsSubtitleError(
                        f"对象 {object_id} 的 data_length 小于宽高字段长度"
                    )
                width, height = struct.unpack_from(">HH", payload, 7)
                if width == 0 or height == 0:
                    raise PgsSubtitleError(
                        f"对象 {object_id} 的位图宽高必须大于零"
                    )
                current = _PendingOds(
                    object_id,
                    version,
                    data_length,
                    width,
                    height,
                    [index],
                    [payload[11:]],
                )
                if sequence == 0xC0:
                    ods_groups[object_id] = _finish_ods(current)
                else:
                    pending[object_id] = current
            elif sequence in (0x00, 0x40):
                current = pending.get(object_id)
                if current is None:
                    raise PgsSubtitleError(
                        f"对象 {object_id} 的 ODS 后续分片缺少首个分片"
                    )
                if current.version != version:
                    raise PgsSubtitleError(
                        f"对象 {object_id} 的 ODS 分片版本号不一致"
                    )
                current.segment_indices.append(index)
                current.fragments.append(payload[4:])
                if sequence == 0x40:
                    ods_groups[object_id] = _finish_ods(current)
                    del pending[object_id]
            else:
                raise PgsSubtitleError(
                    f"对象 {object_id} 的 ODS 序列标志 0x{sequence:02x} 无效"
                )

    if pending:
        object_ids = "、".join(str(object_id) for object_id in pending)
        raise PgsSubtitleError(f"对象 {object_ids} 的 ODS 分片在 END 前未结束")
    return _DisplaySet(tuple(segments), tuple(pcs_segments), ods_groups)


def _parse_sup(data: bytes) -> list[_DisplaySet]:
    if not data:
        raise PgsSubtitleError("SUP 文件为空")

    display_sets: list[_DisplaySet] = []
    segments: list[_Segment] = []
    position = 0
    while position < len(data):
        if position + _SEGMENT_HEADER_SIZE > len(data):
            raise PgsSubtitleError(f"偏移 {position} 处的 PG 段头不完整")
        header = data[position : position + _SEGMENT_HEADER_SIZE]
        if header[:2] != b"PG":
            raise PgsSubtitleError(f"偏移 {position} 处缺少 PG magic")
        segment_type = header[10]
        if segment_type not in (_PCS, _WDS, _PDS, _ODS, _END):
            raise PgsSubtitleError(
                f"偏移 {position} 处的 PG 段类型 0x{segment_type:02x} 无效"
            )
        payload_length = int.from_bytes(header[11:13], "big")
        end = position + _SEGMENT_HEADER_SIZE + payload_length
        if end > len(data):
            raise PgsSubtitleError(
                f"偏移 {position} 处的 PG 段超出文件边界"
            )
        payload = data[position + _SEGMENT_HEADER_SIZE : end]
        if segment_type == _END and payload:
            raise PgsSubtitleError("END 段不应包含负载")
        segments.append(_Segment(data[position:end], segment_type, payload))
        position = end
        if segment_type == _END:
            display_sets.append(_parse_display_set(segments))
            segments = []

    if segments:
        raise PgsSubtitleError("最后一个 Display Set 缺少 END 段")
    return display_sets


def _crop_object(
    source: _ObjectData,
    crop: tuple[int, int, int, int],
    object_id: int,
) -> _ObjectData:
    crop_x, crop_y, crop_width, crop_height = crop
    rows = _decode_rle(source.width, source.height, source.rle, source.object_id)
    cropped_rows = tuple(
        row[crop_x : crop_x + crop_width]
        for row in rows[crop_y : crop_y + crop_height]
    )
    return _ObjectData(
        object_id,
        source.version,
        crop_width,
        crop_height,
        _encode_rle(cropped_rows),
    )


def _encode_ods(object_data: _ObjectData, template: _Segment) -> list[_Segment]:
    data_length = len(object_data.rle) + 4
    if data_length > _MAX_OBJECT_DATA_LENGTH:
        raise PgsSubtitleError(
            f"对象 {object_data.object_id} 裁剪后的 data_length 超过 24 位上限"
        )

    segments: list[_Segment] = []
    position = 0
    first = True
    while first or position < len(object_data.rle):
        capacity = _MAX_SEGMENT_PAYLOAD - (11 if first else 4)
        chunk = object_data.rle[position : position + capacity]
        position += len(chunk)
        last = position == len(object_data.rle)
        if first:
            sequence = 0xC0 if last else 0x80
        else:
            sequence = 0x40 if last else 0x00
        payload = bytearray(struct.pack(">HBB", object_data.object_id,
                                       object_data.version, sequence))
        if first:
            payload.extend(data_length.to_bytes(3, "big"))
            payload.extend(struct.pack(">HH", object_data.width, object_data.height))
        payload.extend(chunk)

        header = bytearray(template.raw[:11])
        header[10] = _ODS
        header.extend(len(payload).to_bytes(2, "big"))
        header.extend(payload)
        raw = bytes(header)
        segments.append(_Segment(raw, _ODS, bytes(payload)))
        first = False
    return segments


def _build_pcs_payload(
    pcs: _Pcs,
    assigned_ids: dict[tuple[int, int], int],
) -> bytes:
    payload = bytearray(pcs.prefix)
    for index, composition_object in enumerate(pcs.objects):
        object_id = assigned_ids.get(
            (pcs.segment_index, index), composition_object.object_id
        )
        payload.extend(
            struct.pack(
                ">HBBHH",
                object_id,
                composition_object.window_id,
                composition_object.flags & 0x7F,
                composition_object.x,
                composition_object.y,
            )
        )
    return bytes(payload)


def _replace_payload(segment: _Segment, payload: bytes) -> _Segment:
    raw = segment.raw[:11] + len(payload).to_bytes(2, "big") + payload
    return _Segment(raw, segment.segment_type, payload)


def _ods_template(display_set: _DisplaySet, object_id: int) -> _Segment:
    group = display_set.ods.get(object_id)
    if group is not None:
        return display_set.segments[group.segment_indices[0]]
    for segment in reversed(display_set.segments):
        if segment.segment_type == _ODS:
            return segment
    if display_set.pcs:
        return display_set.segments[display_set.pcs[0].segment_index]
    return display_set.segments[-1]


def _normalize(display_sets: list[_DisplaySet]) -> tuple[bytes, int]:
    used_ids = {
        composition_object.object_id
        for display_set in display_sets
        for pcs in display_set.pcs
        for composition_object in pcs.objects
    }
    used_ids.update(
        object_id for display_set in display_sets for object_id in display_set.ods
    )

    def allocate_object_id() -> int:
        for object_id in range(1, 0x10000):
            if object_id not in used_ids:
                used_ids.add(object_id)
                return object_id
        raise PgsSubtitleError("没有可用于裁剪变体的空闲对象 ID")

    source_objects: dict[int, _ObjectData] = {}
    decoder_objects: dict[int, _ObjectData] = {}
    output = bytearray()
    crop_count = 0

    for display_set in display_sets:
        current_sources = dict(source_objects)
        current_sources.update(
            (object_id, group.object)
            for object_id, group in display_set.ods.items()
        )
        source_objects.update(current_sources)

        references: list[_Reference] = []
        references_by_id: dict[int, list[_Reference]] = {}
        for pcs in display_set.pcs:
            for object_index, composition_object in enumerate(pcs.objects):
                source = current_sources.get(composition_object.object_id)
                if source is None:
                    raise PgsSubtitleError(
                        f"PCS 引用了缺失的对象 {composition_object.object_id}"
                    )
                crop = composition_object.crop
                needs_crop = False
                if crop is not None:
                    crop_count += 1
                    crop_x, crop_y, crop_width, crop_height = crop
                    if crop_width == 0 or crop_height == 0:
                        raise PgsSubtitleError(
                            f"对象 {source.object_id} 的裁剪宽高必须大于零"
                        )
                    if (
                        crop_x > source.width
                        or crop_y > source.height
                        or crop_width > source.width - crop_x
                        or crop_height > source.height - crop_y
                    ):
                        raise PgsSubtitleError(
                            f"对象 {source.object_id} 的裁剪区域越界："
                            f"crop=({crop_x}, {crop_y}, {crop_width}, {crop_height})，"
                            f"位图尺寸={source.width}x{source.height}"
                        )
                    needs_crop = crop != (0, 0, source.width, source.height)
                reference = _Reference(
                    pcs, object_index, source, crop, needs_crop
                )
                references.append(reference)
                references_by_id.setdefault(source.object_id, []).append(reference)

        assigned_ids: dict[tuple[int, int], int] = {}
        generated: dict[int, _ObjectData] = {}
        replaced_ids: set[int] = set()
        uncropped_ids = {
            source_id
            for source_id, source_references in references_by_id.items()
            if any(not reference.needs_crop for reference in source_references)
        }
        for source_id, source_references in references_by_id.items():
            cropped_references = [
                reference for reference in source_references if reference.needs_crop
            ]
            if not cropped_references:
                continue
            overwrite_original = (
                len(cropped_references) == 1 and source_id not in uncropped_ids
            )
            for reference in cropped_references:
                assert reference.crop is not None
                object_id = (
                    source_id if overwrite_original else allocate_object_id()
                )
                generated[object_id] = _crop_object(
                    reference.source, reference.crop, object_id
                )
                assigned_ids[(reference.pcs.segment_index, reference.object_index)] = (
                    object_id
                )
                if overwrite_original:
                    replaced_ids.add(source_id)

        pcs_by_index = {pcs.segment_index: pcs for pcs in display_set.pcs}
        output_segments: list[_Segment] = []
        emitted_generated: set[int] = set()
        for index, segment in enumerate(display_set.segments):
            if segment.segment_type == _PCS:
                pcs = pcs_by_index[index]
                if any(
                    composition_object.crop is not None
                    for composition_object in pcs.objects
                ):
                    segment = _replace_payload(
                        segment, _build_pcs_payload(pcs, assigned_ids)
                    )
                output_segments.append(segment)
            elif segment.segment_type == _ODS:
                object_id = _u16(segment.payload, 0)
                if object_id not in replaced_ids:
                    output_segments.append(segment)
                    continue
                group = display_set.ods[object_id]
                if index == group.segment_indices[0]:
                    output_segments.extend(
                        _encode_ods(
                            generated[object_id],
                            display_set.segments[group.segment_indices[0]],
                        )
                    )
                    emitted_generated.add(object_id)
            elif segment.segment_type == _END:
                for object_id, object_data in generated.items():
                    if object_id not in emitted_generated:
                        output_segments.extend(
                            _encode_ods(
                                object_data, _ods_template(display_set, object_id)
                            )
                        )
                        emitted_generated.add(object_id)
                output_segments.append(segment)
            else:
                output_segments.append(segment)

        planned_decoder_objects = dict(decoder_objects)
        for object_id, group in display_set.ods.items():
            if object_id not in replaced_ids:
                planned_decoder_objects[object_id] = group.object
        planned_decoder_objects.update(generated)

        restored_ids: set[int] = set()
        for reference in references:
            if reference.needs_crop:
                continue
            source_id = reference.source.object_id
            if (
                source_id not in restored_ids
                and planned_decoder_objects.get(source_id) != reference.source
            ):
                original = reference.source
                encoded = _encode_ods(
                    original, _ods_template(display_set, source_id)
                )
                output_segments[-1:-1] = encoded
                planned_decoder_objects[source_id] = original
                restored_ids.add(source_id)

        for segment in output_segments:
            output.extend(segment.raw)
        decoder_objects = planned_decoder_objects

    return bytes(output), crop_count


def _same_file(source: Path, destination: Path) -> bool:
    if source.resolve() == destination.resolve():
        return True
    try:
        return destination.exists() and os.path.samefile(source, destination)
    except OSError:
        return False


def normalize_sup_file(source: Path, destination: Path) -> int:
    """裁剪 SUP 中的 PCS 对象；返回处理的裁剪合成对象数量。

    没有裁剪对象时不创建或修改 destination。写入采用同目录临时文件替换，
    因此解析或写入失败不会留下不完整的输出文件。
    """

    source = Path(source)
    destination = Path(destination)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise PgsSubtitleError(f"无法读取 SUP 文件 {source}：{exc}") from exc

    normalized, crop_count = _normalize(_parse_sup(data))
    if crop_count == 0:
        return 0
    if _same_file(source, destination):
        raise PgsSubtitleError("规范化输出不能覆盖源 SUP 文件")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(normalized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except OSError as exc:
        raise PgsSubtitleError(
            f"无法写入规范化 SUP 文件 {destination}：{exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return crop_count
