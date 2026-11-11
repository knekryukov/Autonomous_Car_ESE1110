#!/usr/bin/env python3
"""Remove dark/capped-camera images from a stopped Donkeycar v2 tub.

Preview: python clean_dark_images.py ~/mycar/data
Delete:  python clean_dark_images.py ~/mycar/data --delete
Default: average grayscale brightness <= 15, and at least 99% of pixels <= 40.
Brightness scale: 0=black, 255=white. These are starting cutoffs, not calibrated
to your camera. Preview first; adjust --mean-threshold and --threshold if needed.
Unreadable images: add --include-unreadable to also select decoding failures.
Requires Pillow. Stop manage.py before using --delete.
Catalog rows retain their original indexes; manifest deleted_indexes tells
Donkeycar to skip those records. The manifest is backed up before changing it.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timezone

from PIL import Image


def brightness(image):
    histogram = image.convert('L').histogram()
    count = sum(histogram)
    mean = sum(value * frequency for value, frequency in enumerate(histogram)) / count
    accumulated = 0
    for value, frequency in enumerate(histogram):
        accumulated += frequency
        if accumulated * 100 >= count * 99:
            return mean, value


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('tub', type=Path, help='Tub directory containing images/ and manifest.json')
    parser.add_argument('--threshold', type=int, default=40, help='99th-percentile grayscale brightness limit, 0–255 (default: 40)')
    parser.add_argument('--mean-threshold', type=float, default=15, help='Average grayscale brightness limit, 0–255 (default: 15)')
    parser.add_argument('--delete', action='store_true', help='Update deleted-record markers and physically delete matched images')
    parser.add_argument('--include-unreadable', action='store_true', help='Also select images Pillow cannot decode; filesystem read errors are kept')
    args = parser.parse_args()
    if not 0 <= args.threshold <= 255:
        parser.error('--threshold must be between 0 and 255')
    if not 0 <= args.mean_threshold <= 255:
        parser.error('--mean-threshold must be between 0 and 255')
    root = args.tub.expanduser().resolve()
    images = root / 'images'
    if not images.is_dir():
        parser.error(f'Image directory not found: {images}')
    candidates = set()
    measurements = []
    scanned = failed = dark = unreadable_selected = 0
    for path in sorted(images.rglob('*')):
        if path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff'} or not path.is_file():
            continue
        if path.is_symlink():
            continue
        scanned += 1
        try:
            with Image.open(path) as image:
                mean, percentile = brightness(image)
            name = path.relative_to(images).as_posix()
            measurements.append((mean, percentile, name))
            if mean <= args.mean_threshold and percentile <= args.threshold:
                dark += 1
                candidates.add(name)
        except (OSError, ValueError) as error:
            failed += 1
            # Permission and device/read errors do not prove an image is damaged.
            select = args.include_unreadable and getattr(error, 'errno', None) is None
            if select:
                candidates.add(path.relative_to(images).as_posix())
                unreadable_selected += 1
            if failed <= 10:
                print(f'Unreadable, {"selected" if select else "keeping"}: {path.name}: {error}')
    if failed > 10:
        print(f'... {failed - 10} additional unreadable images (details omitted).')
    print(f'Scanned {scanned} images; {dark} dark; {failed} unreadable '
          f'({unreadable_selected} selected, {failed - unreadable_selected} kept).')
    print(f'Total images selected: {len(candidates)}')
    for name in sorted(candidates)[:20]:
        print(f'  {name}')
    if len(candidates) > 20:
        print(f'  ... and {len(candidates) - 20} more')
    print(f'\nDark rule: mean <= {args.mean_threshold:g} AND 99th percentile <= {args.threshold}.')
    print('Darkest readable images (brightness 0=black, 255=white):')
    for mean, percentile, name in sorted(measurements)[:10]:
        status = 'SELECTED' if name in candidates else 'kept'
        print(f'  {name}: mean={mean:.2f}, p99={percentile}, {status}')

    manifest = root / 'manifest.json'
    try:
        original = manifest.read_bytes()
        lines = original.decode('utf-8').splitlines()
        if len(lines) != 5:
            raise ValueError(f'Expected 5 manifest lines, found {len(lines)}')
        parsed = []
        for number, line in enumerate(lines, 1):
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f'Invalid JSON on manifest line {number}: {error}') from error
        inputs, types, _, _, metadata = parsed
        if not isinstance(inputs, list) or not isinstance(types, list) or len(inputs) != len(types):
            raise ValueError('Invalid input/type lists')
        if not isinstance(metadata, dict):
            raise ValueError('Catalog metadata must be an object')
        catalogs = metadata['paths']
        deleted = metadata['deleted_indexes']
        if not isinstance(catalogs, list) or not all(isinstance(x, str) for x in catalogs):
            raise ValueError('Invalid catalog paths')
        if not isinstance(deleted, list) or not all(type(x) is int and x >= 0 for x in deleted):
            raise ValueError('Invalid deleted_indexes')
        image_fields = [name for name, kind in zip(inputs, types) if kind in ('image_array', 'gray16_array', 'image')]
        if not image_fields:
            raise ValueError('No image fields found in manifest')
        affected = set()
        for catalog in catalogs:
            path = (root / catalog).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f'Catalog path outside tub: {catalog}')
            with path.open() as stream:
                for number, line in enumerate(stream, 1):
                    try:
                        record = json.loads(line)
                        index = record['_index']
                        if type(index) is not int or index < 0:
                            raise ValueError('Invalid record index')
                        if any(record.get(field) in candidates for field in image_fields):
                            affected.add(index)
                    except (ValueError, KeyError, TypeError) as error:
                        raise ValueError(f'{catalog}, line {number}: {error}') from error
        print(f'{len(affected)} matching catalog records; {len(affected - set(deleted))} newly excluded records.')
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'\nMetadata check failed: {error}', file=sys.stderr)
        print('No files changed. Repair manifest/catalog metadata before deleting.', file=sys.stderr)
        return 1

    if not args.delete:
        print('\nPreview only. Stop manage.py, then repeat with --delete to apply.')
        return 0
    if not candidates:
        print('Nothing to delete.')
        return 0
    if manifest.read_bytes() != original:
        raise RuntimeError('Manifest changed during scanning. Stop manage.py and retry.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = root / f'manifest.json.before-dark-cleanup-{stamp}.bak'
    shutil.copy2(manifest, backup)
    metadata['deleted_indexes'] = sorted(set(deleted) | affected)
    lines[4] = json.dumps(metadata, allow_nan=False)
    payload = ('\n'.join(lines) + '\n').encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.manifest-dark-', dir=root)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, manifest.stat().st_mode & 0o777)
        os.replace(temporary, manifest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # Persist exclusions before deleting any images. An interrupted run can be repeated.
    removed = 0
    for name in sorted(candidates):
        try:
            (images / name).unlink()
            removed += 1
        except OSError as error:
            print(f'Could not remove {name}: {error}', file=sys.stderr)
    print(f'\nDeleted {removed}/{len(candidates)} images. Catalog rows and indexes preserved.')
    print(f'Manifest backup: {backup}')
    print('Image deletion is permanent; the metadata backup does not restore images.')
    return 0 if removed == len(candidates) else 1


if __name__ == '__main__':
    sys.exit(main())
