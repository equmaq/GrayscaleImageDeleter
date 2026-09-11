# NoirDeleter

Automatically delete grayscale or mostly black-and-white images from a folder.

## What it does

Scans a folder recursively and deletes images that exceed a grayscale threshold (0-100%). Uses fast file signatures and SQLite caching to avoid re-processing unchanged files.

**Supported formats:** JPG, JPEG, PNG, BMP, WebP, GIF, TIF, TIFF

## Requirements

```bash
pip install Pillow numpy
```

## Usage

Edit the configuration at the bottom of `NoirDeleter.py`:

```python
process_folder(
    folder_path=r"F:\Example\Folder",  # Directory to scan (includes subdirectories)
    threshold=99,                    # Grayscale % to delete (0-100). Recommended: 99
    workers=12                       # Parallel threads (max 12 on HDD)
)
```

Then run:
```bash
python NoirDeleter.py
```

## How it works

1. Calculates a grayscale score (0-100) for each image by comparing RGB channel differences
2. Deletes images meeting or exceeding the threshold
3. Caches results in `NoirDeleter.cache.sqlite3` to skip unchanged files on subsequent runs
4. Processes images in parallel using worker threads

## Performance

- **Cached files** are skipped entirely
- **Speed:** 30-150 images/sec depending on image size, about 500/s for cached files (guesstimated on:5600X, sata SSDs, 3600MTs ram)
- Periodic saves prevent excessive memory usage on large folders

## Notes

- The script **permanently deletes** matched files—use with caution
- Threshold of 99 is recommended to catch only nearly-grayscale images
- Results are cached; delete `NoirDeleter.cache.sqlite3` to re-evaluate all images
