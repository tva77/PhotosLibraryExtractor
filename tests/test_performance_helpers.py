import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import PhotosLibraryExtractor as ple


class FakeExifTool:
    def __init__(self, metadata):
        self.metadata = metadata
        self.calls = []

    def get_metadata(self, *paths):
        self.calls.append(paths)
        return [dict(self.metadata[path], **{"SourceFile": path}) for path in paths]


class PerformanceHelpersTests(unittest.TestCase):
    def test_metadata_precedence_and_content_id(self):
        metadata = {
            "QuickTime:ContentCreateDate": "2024:01:02 03:04:05+00:00",
            "EXIF:DateTimeOriginal": "2020:01:02 03:04:05",
            "MakerNotes:ContentIdentifier": "live-id",
        }

        self.assertEqual(
            ple.metadata_info(metadata),
            {"date": "2024:01:02 03:04:05+00:00", "content_ID": "live-id"},
        )

    def test_load_metadata_uses_one_persistent_tool_and_maps_paths(self):
        paths = ["/tmp/one.jpg", "/tmp/two.mov"]
        tool = FakeExifTool({path: {"EXIF:DateTimeOriginal": "2024:01:02 03:04:05"} for path in paths})

        result = ple.load_metadata(tool, paths)

        self.assertEqual(set(result), set(paths))
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(result[paths[0]]["EXIF:DateTimeOriginal"], "2024:01:02 03:04:05")

    def test_load_metadata_falls_back_when_a_batch_fails(self):
        class ExifToolHelper:
            def get_metadata(self, paths):
                if len(paths) > 1:
                    raise RuntimeError("batch failed")
                return [{"SourceFile": paths[0], "EXIF:DateTimeOriginal": "2024:01:02 03:04:05"}]

        paths = ["/tmp/one.jpg", "/tmp/two.jpg"]
        result = ple.load_metadata(ExifToolHelper(), paths)

        self.assertEqual(set(result), set(paths))
        self.assertEqual(result[paths[1]]["EXIF:DateTimeOriginal"], "2024:01:02 03:04:05")

    def test_hash_cache_returns_md5_without_rehashing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"photo bytes")
            expected = hashlib.md5(b"photo bytes").hexdigest()
            cache = {}

            self.assertEqual(ple.md5sum(str(path), cache), expected)
            path.write_bytes(b"changed bytes")
            self.assertNotEqual(ple.md5sum(str(path), cache), expected)

    def test_destination_uses_year_month_and_unknown_dates(self):
        self.assertEqual(
            ple.destination_from_date("2024:01:02 03:04:05", "/input/photo.jpg", "/output"),
            "/output/2024/01/2024-01-02 03.04.05.jpg",
        )
        self.assertEqual(
            ple.destination_from_date(False, "/input/photo.jpg", "/output"),
            "/output/Unknown Dates/photo.jpg",
        )

    def test_destination_strips_colon_from_timezone_offset(self):
        self.assertEqual(
            ple.destination_from_date("2012:01:02 03:04:05-03:00", "/input/photo.jpg", "/output"),
            "/output/2012/01/2012-01-02 03.04.05.jpg",
        )

    def test_main_pairs_live_photo_and_records_duplicates(self):
        class FakeExifTool:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_metadata(self, *paths):
                result = []
                for path in paths:
                    if path.endswith(".mov"):
                        values = {
                            "QuickTime:ContentCreateDate": "2024:01:02 03:04:05+00:00",
                            "QuickTime:ContentIdentifier": "live-id",
                        }
                    else:
                        values = {
                            "EXIF:DateTimeOriginal": "2024:01:02 03:04:05",
                            "MakerNotes:ContentIdentifier": "live-id",
                        }
                    result.append(dict(values, SourceFile=path))
                return result

        with tempfile.TemporaryDirectory() as directory:
            input_dir = Path(directory) / "input"
            output_dir = Path(directory) / "output"
            input_dir.mkdir()
            (input_dir / "photo.jpg").write_bytes(b"photo")
            (input_dir / "photo-copy.jpg").write_bytes(b"photo")
            (input_dir / "live.mov").write_bytes(b"video")
            fake_exiftool = types.SimpleNamespace(ExifTool=FakeExifTool)

            with patch.dict(sys.modules, {"exiftool": fake_exiftool}), patch.object(
                sys, "argv", ["PhotosLibraryExtractor.py", "-i", str(input_dir), "-o", str(output_dir)]
            ):
                self.assertEqual(ple.main(), 0)

            self.assertTrue((output_dir / "2024" / "01" / "2024-01-02 03.04.05.jpg").exists())
            self.assertTrue((output_dir / "2024" / "01" / "2024-01-02 03.04.05.mov").exists())
            self.assertEqual(set((output_dir / "PLEDB").read_text().splitlines()), {
                str(input_dir / "photo.jpg"),
                str(input_dir / "photo-copy.jpg"),
                str(input_dir / "live.mov"),
            })


if __name__ == "__main__":
    unittest.main()
