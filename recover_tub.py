"""Recover unambiguous records into a NEW Tub v2; never modify the source.

python recover_tub.py data data_recovered          # preview
python recover_tub.py data data_recovered --apply  # create recovered tub
Run with recording stopped. Requires DonkeyCar, Pillow and NumPy for --apply.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil


def plan(source):
    from PIL import Image

    lines = (source / 'manifest.json').read_text().splitlines()
    if len(lines) != 5:
        raise ValueError('Expected a five-line Tub v2 manifest.')
    inputs, types = json.loads(lines[0]), json.loads(lines[1])
    if len(inputs) != len(types) or len(set(inputs)) != len(inputs):
        raise ValueError('Invalid input schema.')
    image_keys = [k for k, t in zip(inputs, types) if t == 'image_array']
    if len(image_keys) != 1 or not {'user/angle', 'user/throttle'} <= set(inputs):
        raise ValueError('Expected one image input and steering/throttle fields.')
    if set(types) - {'image_array', 'float', 'int', 'str', 'boolean'}:
        raise ValueError('Unsupported schema; recovery needs review for these input types.')
    image_key = image_keys[0]
    meta = json.loads(lines[4])
    deleted = set(meta.get('deleted_indexes', []))
    groups = defaultdict(list)
    report = {'source': str(source), 'skipped': [], 'kept': []}
    for name in meta['paths']:
        path = (source / name).resolve()
        if not path.is_relative_to(source):
            raise ValueError(f'Unsafe catalog path: {name}')
        # Read actual catalogs, ignoring damaged sidecar counts.
        for number, line in enumerate(path.read_text().splitlines(), 1):
            try:
                record = json.loads(line)
                index = record['_index']
                if type(index) is not int or index < 0:
                    raise ValueError('Invalid record index')
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(f'Invalid record at {name}:{number}') from error
            groups[index].append(record)
    # Reject shared image filenames across indexes: label association is unclear.
    owners = defaultdict(set)
    for index, variants in groups.items():
        for record in variants:
            owners[str(record.get(image_key))].add(index)
    kept = []
    for index, variants in sorted(groups.items()):
        reason = None
        payloads = [{k: r[k] for k in inputs if k in r} for r in variants]
        if index in deleted:
            reason = 'previously_deleted'
        elif any(set(p) != set(inputs) for p in payloads):
            reason = 'missing_input'
        elif len({json.dumps(p, sort_keys=True) for p in payloads}) != 1:
            reason = 'conflicting_inputs'
        else:
            record = variants[0]
            ref = record[image_key]
            if not isinstance(ref, str) or Path(ref).name != ref or len(owners[ref]) != 1:
                reason = 'ambiguous_image_reference'
            else:
                image = source / 'images' / ref
                try:
                    if not image.resolve().is_relative_to(source / 'images'):
                        raise ValueError('Image outside source')
                    with Image.open(image) as im:
                        im.load()  # checks zero-byte and unreadable images
                        if im.mode != 'RGB':
                            raise ValueError('Expected RGB image')
                        size = im.size
                except (OSError, ValueError):
                    reason = 'unreadable_or_invalid_image'
        if reason:
            report['skipped'].append({'index': index, 'reason': reason})
        else:
            kept.append((index, payloads[0], image, size))
            report['kept'].append({'source_index': index, 'new_index': len(kept)-1,
                                   'source_records': variants})
    report['missing_index_count'] = sum(i not in groups for i in range(meta['current_index']))
    counts = Counter(row['reason'] for row in report['skipped'])
    print('Recoverable records:', len(kept))
    print('Skipped:', dict(counts))
    print('Missing indexes claimed by manifest:', report['missing_index_count'])
    if len({row[3] for row in kept}) > 1:
        raise ValueError('Recoverable images have mixed dimensions; split datasets before recovery.')
    return inputs, types, image_key, kept, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    destination = args.destination.resolve()
    if destination.exists() or destination.is_relative_to(source):
        parser.error('Destination must be a NEW directory outside the source tub.')
    inputs, types, image_key, kept, report = plan(source)
    if not args.apply:
        print('PREVIEW ONLY. Add --apply to create the recovered tub.')
        return
    if not kept:
        parser.error('No usable records; no recovery created.')
    import numpy as np
    from PIL import Image
    from donkeycar.parts.tub_v2 import Tub

    destination.mkdir(parents=True)
    (destination / 'recovery_audit.json').write_text(json.dumps(report, indent=2))
    tub = Tub(str(destination), inputs=inputs, types=types)
    try:
        for new_index, (_, payload, image, _) in enumerate(kept):
            with Image.open(image) as im:
                record = dict(payload, **{image_key: np.asarray(im).copy()})
            tub.write_record(record)
            # Preserve original JPEG bytes rather than re-encoding training images.
            target = destination / 'images' / Tub._image_file_name(new_index, image_key)
            shutil.copyfile(image, target)
    finally:
        tub.close()
    print('Recovered tub:', destination)
    print('Original data unchanged. Record indexes/session timestamps were regenerated;')
    print('original records and their mapping are preserved in recovery_audit.json.')


if __name__ == '__main__':
    main()
