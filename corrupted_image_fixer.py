from pathlib import Path, PurePosixPath
from collections import defaultdict
import json
import shutil
import tempfile
import os

# Configuration
DATA_DIR = Path("./data")
IMAGE_DIR = DATA_DIR / "images"
MANIFEST_PATH = DATA_DIR / "manifest.json"

# Preview first. Change to False to execute.
DRY_RUN = False


def image_filename(value):
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid image reference: {value!r}")

    return PurePosixPath(value.replace("\\", "/")).name


def main():
    # Read the five-line DonkeyCar manifest
    if not MANIFEST_PATH.is_file():
        raise FileNotFoundError(MANIFEST_PATH)

    lines = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()

    if len(lines) != 5:
        raise RuntimeError(
            f"Expected 5 manifest lines, got {len(lines)}"
        )

    inputs = json.loads(lines[0])
    types = json.loads(lines[1])
    catalog_meta = json.loads(lines[4])

    image_keys = [
        name for name, typ in zip(inputs, types)
        if typ == "image_array"
    ]

    if len(image_keys) != 1:
        raise RuntimeError(
            f"Expected one image_array input, found {image_keys}"
        )

    image_key = image_keys[0]

    current_index = catalog_meta["current_index"]
    max_len = catalog_meta["max_len"]
    catalog_paths = catalog_meta["paths"]

    existing_deleted = set(
        catalog_meta.get("deleted_indexes", [])
    )

    # Find empty image files
    empty_images = sorted(
        p for p in IMAGE_DIR.rglob("*")
        if p.is_file() and p.stat().st_size == 0
    )

    if not empty_images:
        print("No empty files found.")
        return

    empty_names = {p.name for p in empty_images}

    # Avoid ambiguous references to duplicate filenames
    if len(empty_names) != len(empty_images):
        raise RuntimeError(
            "Multiple empty files have the same basename. "
            "Aborting to avoid ambiguous record matching."
        )

    print(f"Found {len(empty_images)} empty files.")

    # Identify every catalog record referencing an empty image
    matching_records = defaultdict(list)

    global_index = 0

    for catalog_name in catalog_paths:
        catalog_path = (DATA_DIR / catalog_name).resolve()

        if not catalog_path.is_relative_to(DATA_DIR.resolve()):
            raise RuntimeError(f"Invalid catalog path: {catalog_name}")

        if not catalog_path.is_file():
            raise FileNotFoundError(catalog_path)

        expected_count = min(
            max_len,
            current_index - global_index
        )

        if expected_count <= 0:
            raise RuntimeError("Unexpected extra catalog.")

        catalog_start = global_index
        catalog_count = 0

        # Validate catalog sidecar when available
        sidecar = catalog_path.with_name(
            catalog_path.stem + ".catalog_manifest"
        )

        if sidecar.is_file():
            sidecar_lines = sidecar.read_text(
                encoding="utf-8"
            ).splitlines()

            if not sidecar_lines:
                raise RuntimeError(f"Empty sidecar: {sidecar}")

            sidecar_meta = json.loads(sidecar_lines[0])

            if sidecar_meta.get("start_index") != catalog_start:
                raise RuntimeError(
                    f"Incorrect start index in {sidecar}"
                )

            if len(sidecar_meta["line_lengths"]) != expected_count:
                raise RuntimeError(
                    f"Incorrect record count in {sidecar}"
                )

        # Catalogs are newline-delimited JSON
        with catalog_path.open(encoding="utf-8") as f:
            for line_number, line in enumerate(f, 1):

                if not line.strip():
                    raise RuntimeError(
                        f"Blank record: {catalog_name}:{line_number}"
                    )

                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"Invalid JSON in {catalog_name}, "
                        f"line {line_number}: {e}"
                    )

                if image_key not in record:
                    raise RuntimeError(
                        f"Missing {image_key} in "
                        f"{catalog_name}:{line_number}"
                    )

                filename = image_filename(record[image_key])

                if filename in empty_names:
                    matching_records[filename].append(global_index)

                global_index += 1
                catalog_count += 1

        if catalog_count != expected_count:
            raise RuntimeError(
                f"Catalog count mismatch for {catalog_name}: "
                f"expected {expected_count}, found {catalog_count}"
            )

    # Ensure global indices agree with manifest
    if global_index != current_index:
        raise RuntimeError(
            f"Global index mismatch: "
            f"manifest={current_index}, catalogs={global_index}"
        )

    # Calculate deletion indices
    record_indexes = sorted({
        index
        for indexes in matching_records.values()
        for index in indexes
    })

    new_indexes = sorted(
        set(record_indexes) - existing_deleted
    )

    print("\n--- Cleanup report ---")

    for image in empty_images:
        indexes = matching_records.get(image.name, [])
        print(f"{image.name}: {indexes}")

    print(f"\nEmpty files: {len(empty_images)}")
    print(f"Records referencing them: {len(record_indexes)}")
    print(f"Already deleted: {len(record_indexes) - len(new_indexes)}")
    print(f"New records to exclude: {len(new_indexes)}")

    if DRY_RUN:
        print("\nDRY RUN: No modifications made.")
        print("Set DRY_RUN = False to execute.")
        return

    # Update native DonkeyCar deletion metadata
    if new_indexes:
        backup = MANIFEST_PATH.with_name(
            "manifest.json.pre_cleanup.bak"
        )

        counter = 1
        while backup.exists():
            backup = MANIFEST_PATH.with_name(
                f"manifest.json.pre_cleanup.{counter}.bak"
            )
            counter += 1

        shutil.copy2(MANIFEST_PATH, backup)
        print(f"\nManifest backup: {backup}")

        catalog_meta["deleted_indexes"] = sorted(
            existing_deleted | set(record_indexes)
        )

        lines[4] = json.dumps(
            catalog_meta,
            separators=(",", ":")
        )

        # Write atomically
        fd, temp_name = tempfile.mkstemp(
            dir=DATA_DIR,
            prefix=".manifest_cleanup_",
            suffix=".tmp"
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

            os.replace(temp_name, MANIFEST_PATH)

        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    # Physically delete empty files
    deleted_files = 0

    for image in empty_images:
        try:
            if image.exists() and image.stat().st_size == 0:
                image.unlink()
                deleted_files += 1
        except OSError as e:
            print(f"Could not delete {image}: {e}")

    print("\n--- Cleanup complete ---")
    print(f"Deleted image files: {deleted_files}")
    print(f"Newly excluded records: {len(new_indexes)}")


main()