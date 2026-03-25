from PIL import Image
import os
import numpy as np
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import timedelta
from itertools import count
import hashlib
import sqlite3

# Image file extensions to process
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif', '.tif', '.tiff'}

# Size of file chunk (in bytes) for signature calculation
SIGNATURE_CHUNK_SIZE = 64 * 1024
# Buffer size for file I/O operations (in bytes)
SIGNATURE_IO_BUFFER_SIZE = 256 * 1024
# Length of BLAKE2 digest in bytes (128-bit)
SIGNATURE_DIGEST_SIZE = 16
# Name of the cache database file stored in script directory
CACHE_FILE_NAME = 'NoirDeleter.cache.sqlite3'
# How many files to process before saving cache updates to database
CACHE_SAVE_INTERVAL = 5000


def get_file_signature(path, chunk_size=SIGNATURE_CHUNK_SIZE):
    """
    Generate a fast file signature for use as a cache key.
    
    Creates a unique signature based on file size and content hashes to detect
    if a file has changed since last processing. Uses BLAKE2 hashing for speed.
    
    Signature includes:
      - File size in bytes
      - BLAKE2 hash of first chunk
      - BLAKE2 hash of last chunk (non-overlapping)
    
    For small files (<= 2 chunks), hashes the entire file content.
    For larger files, reads only start and end chunks to minimize I/O.
    
    Args:
        path: Full path to file
        chunk_size: Size of file chunk (bytes) for hashing. Defaults to SIGNATURE_CHUNK_SIZE
    
    Returns:
      Tuple of (size_bytes, head_digest_hex, tail_digest_hex)
    """
    st = os.stat(path)
    size = st.st_size

    blake2b = hashlib.blake2b

    with open(path, 'rb', buffering=SIGNATURE_IO_BUFFER_SIZE) as f:
        if size <= 2 * chunk_size:
            data = f.read(size)
            head_len = min(chunk_size, size)
            tail_len = size - head_len
            head = data[:head_len]
            tail = data[head_len:] if tail_len else b''
        else:
            head = f.read(chunk_size)
            f.seek(size - chunk_size)
            tail = f.read(chunk_size)

    head_digest = blake2b(head, digest_size=SIGNATURE_DIGEST_SIZE).hexdigest()
    tail_digest = blake2b(tail, digest_size=SIGNATURE_DIGEST_SIZE).hexdigest()
    return (size, head_digest, tail_digest)


def _signature_key(signature):
    """
    Convert a file signature tuple into a cache key string.
    
    Args:
        signature: Tuple of (size, head_digest, tail_digest)
        
    Returns:
        String in format "size:head_digest:tail_digest" for use as database key
    """
    size, head_digest, tail_digest = signature
    return f"{size}:{head_digest}:{tail_digest}"


def init_cache_db(cache_path):
    """
    Initialize SQLite cache database for storing file signatures and grayscale scores.
    
    Sets up database with optimized settings (WAL mode, NORMAL sync) and creates
    the cache table with an index for efficient lookups.
    
    Args:
        cache_path: Path to the SQLite database file
        
    Returns:
        sqlite3 connection object configured and ready for use
    """
    conn = sqlite3.connect(cache_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA temp_store=MEMORY')
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS cache (
            signature TEXT PRIMARY KEY,
            score REAL NOT NULL,
            last_seen INTEGER NOT NULL
        )
        '''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cache_last_seen ON cache(last_seen DESC)')
    conn.commit()
    return conn


def load_cache_snapshot(conn):
    """
    Load all cached file signatures and their grayscale scores into memory.
    
    Args:
        conn: sqlite3 database connection
        
    Returns:
        Dictionary mapping signature key strings to grayscale score floats
    """
    rows = conn.execute('SELECT signature, score FROM cache').fetchall()
    return {signature: float(score) for signature, score in rows}


def flush_cache_updates(conn, pending_updates):
    """
    Persist pending cache updates to the database.
    
    Writes accumulated scores and timestamps to the database using INSERT OR UPDATE
    to handle both new entries and updates to existing entries.
    
    Args:
        conn: sqlite3 database connection
        pending_updates: List of tuples (signature_key, score, timestamp) to write
    """
    if not pending_updates:
        return

    conn.executemany(
        '''
        INSERT INTO cache(signature, score, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(signature) DO UPDATE SET
            score = excluded.score,
            last_seen = excluded.last_seen
        ''',
        pending_updates
    )
    conn.commit()

def calculate_grayscale_score(img_array):
    """
    Calculate how grayscale an image is using vectorized numpy operations.
    
    Computes the sum of differences between color channels and normalizes to
    a 0-100 scale, where 100 is completely grayscale and 0 is highly colored.
    
    Args:
        img_array: numpy array of image in RGB format with shape (height, width, 3)
        
    Returns:
        Float between 0 and 100 representing grayscale percentage
    """
    diff = np.sum(np.abs(img_array[:,:,0] - img_array[:,:,1])) + \
           np.sum(np.abs(img_array[:,:,0] - img_array[:,:,2])) + \
           np.sum(np.abs(img_array[:,:,1] - img_array[:,:,2]))
    return 100 - (diff / (3 * 255 * img_array.shape[0] * img_array.shape[1])) * 100

def process_file(args):
    """
    Process a single image file to determine if it should be deleted based on grayscale score.
    
    Checks cache first for performance. If not cached, loads image and calculates grayscale score.
    Deletes file if score meets or exceeds threshold. Designed to run in parallel via ThreadPoolExecutor.
    
    Args:
        args: Tuple of (file_path, threshold, counter, total, cache_snapshot)
            - file_path: Path to image file to process
            - threshold: Minimum grayscale percentage (0-100) to trigger deletion
            - counter: itertools.count object for tracking progress
            - total: Total number of files being processed
            - cache_snapshot: Dictionary of cached signatures to scores
            
    Returns:
        Tuple of (status, signature_key, score) where:
            - status: 'deleted', 'kept', or 'error'
            - signature_key: Cache key for the file (or None if error)
            - score: Grayscale percentage (or None if error)
    """
    file_path, threshold, counter, total, cache_snapshot = args
    try:
        signature = get_file_signature(file_path)
        signature_key = _signature_key(signature)
        score = cache_snapshot.get(signature_key)
        from_cache = score is not None

        if score is None:
            with Image.open(file_path) as img:
                img_array = np.array(img.convert('RGB'))
                score = round(calculate_grayscale_score(img_array), 2)

        if score >= threshold:
            os.remove(file_path)
            cached_tag = ' (cached)' if from_cache else ''
            print(f"({next(counter)}/{total}) 🗑️ Deleted: {file_path} ({score}% grayscale){cached_tag}")
            return ('deleted', signature_key, score)

        cached_tag = ' (cached)' if from_cache else ''
        print(f"({next(counter)}/{total}) ✓ Kept: {file_path} ({score}% grayscale){cached_tag}")
        return ('kept', signature_key, score)
    except Exception as e:
        print(f"({next(counter)}/{total}) ⚠️ Error: {file_path} ({str(e)[:50]})")
        return ('error', None, None)

def process_folder(folder_path, threshold=99, workers=8):
    """
    Scan folder recursively and delete grayscale images based on threshold.
    
    Recursively walks through folder finding all supported image formats.
    Uses threaded processing to evaluate images in parallel. Maintains a cache
    database of file signatures and scores to avoid re-processing unchanged files.
    Outputs progress and statistics to console.
    
    Args:
        folder_path: Root directory path to scan recursively for images
        threshold: Grayscale percentage (0-100) above which to delete images.
                  Default 99 - recommended value for removing mostly bw/grayscale images
        workers: Number of worker threads for parallel processing.
                Default 8 - avoid going above 12 if images are on HDD to prevent bottlenecks
    """
    start_time = time.time()
    # Initialize cache database in script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cache_path = os.path.join(script_dir, CACHE_FILE_NAME)
    conn = init_cache_db(cache_path)

    # Initialize statistics tracking dictionary
    stats = {
        'total': 0,
        'deleted': 0,
        'kept': 0,
        'error': 0
    }
    
    # Recursively collect all supported image files from folder
    files = []
    for root, _, filenames in os.walk(folder_path):
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                files.append(os.path.join(root, f))
    stats['total'] = len(files)

    # Load cached scores for faster re-processing
    cache_snapshot = load_cache_snapshot(conn)
    pending_updates = []
    update_seq = int(time.time() * 1000000)
    processed_since_save = 0
    
    # Process all files in parallel using thread pool
    counter = count(1)
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = executor.map(
                process_file,
                [(f, threshold, counter, stats['total'], cache_snapshot) for f in files]
            )
            # Collect results and update cache periodically
            for status, signature_key, score in results:
                stats[status] += 1

                if signature_key is not None and score is not None:
                    score = float(score)
                    cache_snapshot[signature_key] = score
                    update_seq += 1
                    pending_updates.append((signature_key, score, update_seq))

                processed_since_save += 1
                # Save to database every CACHE_SAVE_INTERVAL files to reduce memory usage
                if processed_since_save >= CACHE_SAVE_INTERVAL:
                    flush_cache_updates(conn, pending_updates)
                    pending_updates.clear()
                    processed_since_save = 0
    finally:
        # Ensure all pending updates are saved before closing database
        flush_cache_updates(conn, pending_updates)
        conn.close()
    
    # Print summary statistics and performance metrics
    elapsed = time.time() - start_time
    print("\n" + "="*50)
    print(f"Supported formats: {', '.join(SUPPORTED_EXTENSIONS)}")
    print(f"Total files: {stats['total']}")
    deleted_ratio = (stats['deleted'] / stats['total']) if stats['total'] else 0
    print(f"Deleted: {stats['deleted']} ({deleted_ratio:.1%})")
    print(f"Kept: {stats['kept']}")
    print(f"Errors: {stats['error']}")
    print(f"Time: {timedelta(seconds=elapsed)}")
    speed = (stats['total'] / elapsed) if elapsed else 0
    print(f"Speed: {speed:.1f} images/sec")
    print(f"Cache file: {cache_path} ({len(cache_snapshot)} hashes)")

#Configuration
# Run the main function with settings:
#   - folder_path: Directory to scan for images (supports subdirectories)
#   - threshold: Minimum grayscale % to delete (0-100). 99% recommended for mostly B&W images
#   - workers: Number of parallel threads (max 12 on HDD to avoid I/O bottlenecks)
process_folder(
    folder_path=r"C:\SomeFolder",
    threshold=99,
    workers=12
)
