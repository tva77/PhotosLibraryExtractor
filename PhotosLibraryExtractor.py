import hashlib
import os
import shutil
import sys
from argparse import ArgumentParser


ignored_files = {".DS_Store", "PLEDB"}
ignored_file_exts = {".bat", ".sh", ".py", ".zip", ".7z"}


def md5sum(filename, cache=None):
    stat = os.stat(filename)
    signature = (stat.st_size, stat.st_mtime_ns)
    if cache is not None and filename in cache:
        cached_signature, digest = cache[filename]
        if cached_signature == signature:
            return digest

    h = hashlib.md5()
    with open(filename, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            h.update(chunk)

    digest = h.hexdigest()
    if cache is not None:
        cache[filename] = (signature, digest)
    return digest


def date_valid(metadata, field):
    return field in metadata and not metadata[field].isspace()


def metadata_info(metadata):
    if date_valid(metadata, "QuickTime:ContentCreateDate"):
        # usually the date to go by for videos transcoded in Photos.app... CreateDate will be the transcode date for those
        date = metadata["QuickTime:ContentCreateDate"]
    elif date_valid(metadata, "EXIF:DateTimeOriginal"):
        date = metadata["EXIF:DateTimeOriginal"]
    elif date_valid(metadata, "EXIF:CreateDate"):
        date = metadata["EXIF:CreateDate"]
    elif date_valid(metadata, "QuickTime:CreateDate"):
        date = metadata["QuickTime:CreateDate"]
    elif date_valid(metadata, "EXIF:ModifyDate"):
        date = metadata["EXIF:ModifyDate"]
    else:
        date = False

    if "MakerNotes:ContentIdentifier" in metadata:
        content_id = metadata["MakerNotes:ContentIdentifier"]
    elif "QuickTime:ContentIdentifier" in metadata:
        content_id = metadata["QuickTime:ContentIdentifier"]
    else:
        content_id = False

    return {"date": date, "content_ID": content_id}


def load_metadata(exif_tool, paths, batch_size=128):
    metadata_by_path = {}
    for start in range(0, len(paths), batch_size):
        batch = paths[start:start + batch_size]
        if exif_tool.__class__.__name__ == "ExifToolHelper":
            metadata = exif_tool.get_metadata(batch)
        else:
            metadata = exif_tool.get_metadata(*batch)
        if isinstance(metadata, dict):
            metadata = [metadata]
        for path, values in zip(batch, metadata):
            source_file = values.get("SourceFile", path)
            metadata_by_path[os.path.abspath(source_file)] = values
    return metadata_by_path


def destination_from_date(in_date, in_path, out_dir):
    filename = os.path.basename(in_path)
    if in_date:
        date, time = in_date.split(" ", 1)
        year, month, day = date.split(":")
        hour, minute, second = time.split(":")
        if "+" in second:
            second = second.split("+", 1)[0]
        elif "-" in second:  # i think there can be negative offsets too ?
            second = second.split("-", 1)[0]

        ext = os.path.splitext(filename)[1]
        folder_path = os.path.join(out_dir, year, month)
        filename = "%s-%s-%s %s.%s.%s%s" % (
            year, month, day, hour, minute, second, ext
        )
    else:  # no date
        folder_path = os.path.join(out_dir, "Unknown Dates")

    return os.path.join(folder_path, filename)


def copy_handler(input_path, destination, hash_cache):
    dest_folder = os.path.dirname(destination)
    if not os.path.isdir(dest_folder):
        os.makedirs(dest_folder)

    base, ext = os.path.splitext(os.path.basename(destination))
    if ext.lower() == ".jpeg":  # change jpeg to jpg for consistency
        ext = ".jpg"

    i = 97  # 97 is a
    new_name = base + ext
    final_path = os.path.join(dest_folder, new_name)
    while os.path.isfile(final_path):
        if md5sum(final_path, hash_cache) == md5sum(input_path, hash_cache):
            return False
        new_name = base + chr(i) + ext  # append letter to filename if it already exists
        final_path = os.path.join(dest_folder, new_name)
        i += 1

    print(input_path, "-->", final_path)
    shutil.copy(input_path, final_path)
    hash_cache[final_path] = hash_cache[input_path]
    return True


def add_to_processed_files(filepath, processed_db):
    processed_db.write(filepath + "\n")
    processed_db.flush()


def main():
    parser = ArgumentParser()
    parser.add_argument(
        "-i", action="store", dest="input", help="Folder with pictures you want to process a.k.a. input folder", required=True
    )
    parser.add_argument(
        "-o", action="store", dest="output", help="Photos will be copied to this folder a.k.a. output folder", required=True
    )
    parser.add_argument("-db", action="store", dest="db_path", help="Path to PLEDB file", required=False)
    parsed = parser.parse_args()

    in_dir = os.path.abspath(parsed.input)
    if ".photoslibrary" in in_dir:
        originals_path = os.path.join(in_dir, "originals/")
        if os.path.isdir(originals_path):
            in_dir = originals_path

        masters_path = os.path.join(in_dir, "Masters/")  # high sierra
        if os.path.isdir(masters_path):
            in_dir = masters_path

    out_dir = os.path.abspath(parsed.output)
    already_processed_db = os.path.abspath(parsed.db_path) if parsed.db_path else os.path.join(out_dir, "PLEDB")

    print("Input folder:", in_dir)
    print("Destination:", out_dir)
    print("DB file:", already_processed_db)
    print("---")

    if not os.path.isdir(in_dir):
        print("Error! This doesn't seem to be a folder:", in_dir)
        return 1

    if not os.path.isdir(out_dir):
        print("Making output folder:", out_dir)
        os.makedirs(out_dir)

    content_id_files = {}
    handled_hashes = set()
    duplicate_files = []
    files_copied = 0
    hash_cache = {}

    previously_handled_files = set()
    if os.path.isfile(already_processed_db):
        print("Reading", already_processed_db)
        with open(already_processed_db) as processed_db:
            previously_handled_files = {line.rstrip() for line in processed_db}
        print(len(previously_handled_files), "files in PLEDB")
        print("They will be skipped this run. If you want to start fresh, delete the file:", already_processed_db)
        print("---")

    records = []
    skipped_files = len(previously_handled_files)
    for dirpath, _, filenames in os.walk(in_dir):
        for filename in filenames:
            if filename in ignored_files or os.path.splitext(filename)[1].lower() in ignored_file_exts:
                print("Ignored:", filename)
                continue

            in_file = os.path.abspath(os.path.join(dirpath, filename))
            if in_file in previously_handled_files:
                continue

            file_hash = md5sum(in_file, hash_cache)
            if file_hash in handled_hashes:
                print("Duplicate:", filename)
                duplicate_files.append(in_file)
                continue

            handled_hashes.add(file_hash)
            records.append((in_file, file_hash))

    if not records:
        if duplicate_files:
            with open(already_processed_db, "a") as processed_db:
                for duplicate_path in duplicate_files:
                    add_to_processed_files(duplicate_path, processed_db)
        skipped_files += len(duplicate_files)
        print("Files processed:", len(records))
        print("Files copied:", files_copied)
        print("Skipped files:", skipped_files)
        print("Done")
        return 0

    import exiftool

    exiftool_class = getattr(exiftool, "ExifToolHelper", exiftool.ExifTool)
    with exiftool_class() as et, open(already_processed_db, "a") as processed_db:
        for duplicate_path in duplicate_files:
            add_to_processed_files(duplicate_path, processed_db)

        metadata_by_path = load_metadata(et, [path for path, _ in records])
        for in_file, file_hash in records:
            info = metadata_info(metadata_by_path[in_file])
            date = info["date"]
            content_id = info["content_ID"]
            ext = os.path.splitext(in_file)[1]

            if content_id:
                matched = content_id_files.pop(content_id, None)
                if matched:
                    matched_filename, matched_info = matched
                    if ext.lower() == ".mov":
                        video_path, picture_path = in_file, matched_filename
                    else:
                        video_path, picture_path = matched_filename, in_file

                    picture_date = matched_info["date"] if picture_path == matched_filename else date
                    if picture_path == in_file:
                        picture_date = date

                    if copy_handler(video_path, destination_from_date(picture_date, video_path, out_dir), hash_cache):
                        files_copied += 1
                    if copy_handler(picture_path, destination_from_date(picture_date, picture_path, out_dir), hash_cache):
                        files_copied += 1
                else:
                    content_id_files[content_id] = (in_file, info)
            else:
                if copy_handler(in_file, destination_from_date(date, in_file, out_dir), hash_cache):
                    files_copied += 1

            add_to_processed_files(in_file, processed_db)

        if content_id_files:
            print("---")
            print("There are", len(content_id_files), "files with unpaired Content IDs left")
            for leftover_path, info in content_id_files.values():
                if copy_handler(leftover_path, destination_from_date(info["date"], leftover_path, out_dir), hash_cache):
                    files_copied += 1
            print("---")

    skipped_files += len(duplicate_files)
    print("Files processed:", len(records))
    print("Files copied:", files_copied)
    print("Skipped files:", skipped_files)
    print("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
