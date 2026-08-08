"""Command line interface for ulg2mcap."""

import argparse
import sys
from pathlib import Path
from typing import List

from ulg2mcap.converter import ULog2MCAPConverter


def parse_topic_list(val: str) -> List[str]:
    """Parse comma or space separated topic lists."""
    if not val:
        return []
    items = []
    for part in val.replace(",", " ").split():
        cleaned = part.strip()
        if cleaned:
            items.append(cleaned)
    return items


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert PX4 / Zephyr ULog (.ulg) files to MCAP (.mcap) format."
    )
    parser.add_argument(
        "input_path",
        type=str,
        help="Path to an input .ulg file or directory containing .ulg files.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Output .mcap file path or output directory for batch conversion.",
    )
    parser.add_argument(
        "--compress",
        choices=["zstd", "lz4", "none"],
        default="zstd",
        help="Compression mode for MCAP chunks (default: zstd).",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recursively search subdirectories when input_path is a directory.",
    )
    parser.add_argument(
        "--include-topics",
        type=str,
        default=None,
        help="Comma or space-separated topic names to include (e.g. 'vehicle_attitude,sensor_combined').",
    )
    parser.add_argument(
        "--exclude-topics",
        type=str,
        default=None,
        help="Comma or space-separated topic names to exclude.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print verbose conversion output.",
    )

    args = parser.parse_args()

    input_path = Path(args.input_path).resolve()
    if not input_path.exists():
        print(f"Error: Input path '{args.input_path}' does not exist.", file=sys.stderr)
        return 1

    include_topics = parse_topic_list(args.include_topics) if args.include_topics else None
    exclude_topics = parse_topic_list(args.exclude_topics) if args.exclude_topics else None

    converter = ULog2MCAPConverter(
        include_topics=include_topics,
        exclude_topics=exclude_topics,
        compression=args.compress,
    )

    if input_path.is_file():
        if input_path.suffix.lower() != ".ulg":
            print(f"Warning: Input file '{input_path.name}' does not end in .ulg", file=sys.stderr)

        if args.output:
            out_path = Path(args.output).resolve()
            if out_path.is_dir() or args.output.endswith("/") or args.output.endswith("\\"):
                out_path = out_path / f"{input_path.stem}.mcap"
        else:
            out_path = input_path.with_suffix(".mcap")

        if args.verbose:
            print(f"Converting '{input_path}' -> '{out_path}'...")

        try:
            res = converter.convert(input_path, out_path)
            print(f"Successfully created: {res}")
            return 0
        except Exception as e:
            print(f"Error converting '{input_path}': {e}", file=sys.stderr)
            return 1

    elif input_path.is_dir():
        pattern = "**/*.ulg" if args.recursive else "*.ulg"
        ulg_files = sorted(list(input_path.glob(pattern)))

        if not ulg_files:
            print(f"No .ulg files found in directory '{input_path}'.")
            return 0

        out_dir = Path(args.output).resolve() if args.output else input_path
        out_dir.mkdir(parents=True, exist_ok=True)

        success_count = 0
        fail_count = 0

        for ulg_file in ulg_files:
            # Preserve relative path structure if recursive
            if args.recursive and ulg_file.is_relative_to(input_path):
                rel_parent = ulg_file.relative_to(input_path).parent
                target_dir = out_dir / rel_parent
                target_dir.mkdir(parents=True, exist_ok=True)
                out_file = target_dir / f"{ulg_file.stem}.mcap"
            else:
                out_file = out_dir / f"{ulg_file.stem}.mcap"

            if args.verbose:
                print(f"Converting '{ulg_file.name}' -> '{out_file.name}'...")

            try:
                converter.convert(ulg_file, out_file)
                success_count += 1
            except Exception as e:
                print(f"Failed to convert '{ulg_file.name}': {e}", file=sys.stderr)
                fail_count += 1

        print(f"Batch conversion finished: {success_count} succeeded, {fail_count} failed.")
        return 0 if fail_count == 0 else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
