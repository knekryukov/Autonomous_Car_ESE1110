"""Merge Donkeycar v2 tubs, salvaging readable records and known deletions.

Usage: python3 merge_tubs.py INPUT_TUB [INPUT_TUB ...] -o OUTPUT_TUB
Input order determines output order. Original tubs are never modified.
"""
import argparse
import json
import re
import shutil
import time
from pathlib import Path


def dump(value):
    return json.dumps(value, ensure_ascii=True) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('inputs', type=Path, nargs='+', help='Input tub directories, in merge order')
    parser.add_argument('-o', '--output', type=Path, required=True, help='New output tub directory (must not exist)')
    parser.add_argument('--strict', action='store_true',
        help='Fail instead of salvaging truncated manifests or skipping damaged records')
    args = parser.parse_args()
    sources = [p.resolve() for p in args.inputs]
    DEST = args.output.resolve()
    if len(set(sources)) != len(sources):
        parser.error('The same input tub was supplied more than once')
    if DEST.exists():
        raise SystemExit(f'Refusing to overwrite {DEST}')
    if any(DEST.is_relative_to(source) for source in sources):
        parser.error('Output must be outside the input tubs')
    # Complete preparation before creating the destination.
    records, mapping, summaries, sessions = [], [], [], []
    schema = None
    session_map = {}
    image_fields = []
    for source_number, source in enumerate(sources):
        lines = (source / 'manifest.json').read_text().splitlines()
        header = [json.loads(line) for line in lines[:4]]
        if schema is None:
            schema = header[:2]
            image_fields = [name for name, kind in zip(*schema) if kind == 'image_array']
        if schema != header[:2]:
            raise SystemExit(f'Incompatible schema: {source}')
        try:
            deleted = set(json.loads(lines[4])['deleted_indexes'])
            incomplete = False
        except (json.JSONDecodeError, IndexError):
            if args.strict:
                raise SystemExit(f'Incomplete manifest: {source}')
            # Only accept integers followed by a comma or closing bracket.
            # The trailing partial integer must never become a deletion flag.
            match = re.search(r'"deleted_indexes"\s*:\s*\[(.*)', lines[4] if len(lines) > 4 else '')
            tail = match.group(1) if match else ''
            deleted = set()
            # Parse a valid prefix only, never interpreting a cut-off integer.
            while match := re.match(r'\s*(\d+)\s*([,\]])', tail):
                deleted.add(int(match.group(1)))
                tail = tail[match.end():]
            incomplete = True
        stats = dict(source=str(source), deletion_list_incomplete=incomplete,
                     recovered_deleted_flags=len(deleted), readable=0,
                     excluded_deleted=0, exported=0, malformed=[],
                     unreferenced_image_count=0)
        referenced = set()
        catalogs = sorted((p for p in source.glob('catalog_*.catalog')
                           if re.fullmatch(r'catalog_\d+\.catalog', p.name)),
                          key=lambda p: int(p.stem.split('_')[1]))
        if not catalogs:
            raise SystemExit(f'No catalogs found in {source}')
        for catalog in catalogs:
            for line_number, line in enumerate(catalog.read_bytes().splitlines(), 1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or not isinstance(record.get('_index'), int):
                        raise ValueError('Missing record index')
                    if any(field not in record for field in schema[0]):
                        raise ValueError('Missing schema field')
                    for field in image_fields:
                        image = (source / 'images' / record[field]).resolve()
                        if not image.is_relative_to(source / 'images') or not image.is_file():
                            raise ValueError(f'Missing or invalid image path: {field}')
                except (ValueError, UnicodeDecodeError, TypeError) as error:
                    if args.strict:
                        raise SystemExit(f'{catalog}:{line_number}: {error}')
                    stats['malformed'].append(dict(catalog=catalog.name, line=line_number))
                    continue
                stats['readable'] += 1
                old_index = record['_index']
                old_images = {field: record[field] for field in image_fields}
                referenced.update(old_images.values())
                if old_index in deleted:
                    stats['excluded_deleted'] += 1
                    continue
                old_session = record.get('_session_id')
                session_key = (source_number, old_session)
                if session_key not in session_map:
                    session_map[session_key] = f'{time.strftime("%y-%m-%d")}_{len(sessions)}'
                    sessions.append(session_map[session_key])
                session = session_map[session_key]
                new_index = len(records)
                mapping.append(dict(index=new_index, source=str(source),
                                    source_index=old_index, source_images=old_images,
                                    source_session=old_session))
                record.update(_index=new_index, _session_id=session)
                copies = []
                for field_number, (field, old_image) in enumerate(old_images.items()):
                    # Field ordinal also prevents collisions between camera fields.
                    new_image = f'{new_index}_{field_number}_{field.replace("/", "_")}{Path(old_image).suffix}'
                    record[field] = new_image
                    copies.append((source / 'images' / old_image, new_image))
                records.append((record, copies))
                stats['exported'] += 1
        stats['unreferenced_image_count'] = len({p.name for p in (source / 'images').iterdir()} - referenced)
        summaries.append(stats)

    if not records:
        raise SystemExit('No usable, non-deleted records found; no output created')
    DEST.mkdir(parents=True)
    (DEST / 'images').mkdir()
    created = time.time()
    paths = []
    for start in range(0, len(records), 1000):
        name = f'catalog_{start // 1000}.catalog'
        paths.append(name)
        serialized = [dump(record).encode('utf-8') for record, _ in records[start:start + 1000]]
        (DEST / name).write_bytes(b''.join(serialized))
        sidecar = name + '_manifest'
        (DEST / sidecar).write_text(dump(dict(created_at=created,
            line_lengths=[len(line) for line in serialized], path=sidecar, start_index=start)))
    for record, copies in records:
        for image, new_image in copies:
            shutil.copyfile(image, DEST / 'images' / new_image)
    metadata = dict(created_at=created, sessions=dict(all_full_ids=sessions,
        last_id=len(sessions) - 1, last_full_id=sessions[-1]))
    state = dict(paths=paths, current_index=len(records), max_len=1000, deleted_indexes=[])
    (DEST / 'manifest.json').write_text(''.join(dump(v) for v in [*schema, {}, metadata, state]))
    (DEST / 'source_records.jsonl').write_text(''.join(dump(v) for v in mapping))
    report = dict(records=len(records), sources=summaries,
        limitation=('Some source deletion lists are incomplete. Unknown deletion flags cannot be honored.'
                    if any(s['deletion_list_incomplete'] for s in summaries) else None))
    (DEST / 'merge_report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
