# ULog to MCAP Converter (`ulg2mcap`)

`ulg2mcap` is a high-performance Python tool and library for converting PX4 / Zephyr ULog (`.ulg`) flight logs into MCAP (`.mcap`) log containers for multimodal visualization in **Foxglove Studio** and analysis with MCAP toolchains.

---

## Features

- **Dynamic JSON Schema Generation:** Converts any ULog topic and array field structure directly to JSON Schema without pre-compiled `.proto` or ROS `.msg` dependencies.
- **`zstd` & `lz4` Compression:** Highly compressed log containers enabled by default for compact file storage.
- **Nanosecond Precision:** Scales ULog microsecond timestamps to standard MCAP nanoseconds with chronological message sorting.
- **Text Log Streaming:** Converts embedded ULog text messages to a streaming topic (`log`) for real-time viewing in Foxglove Log panels.
- **System Metadata:** Retains ULog system hardware, software version, build hash, and initial parameters as MCAP metadata records.
- **Batch Processing:** Easily converts single files or entire directory trees.

---

## Installation & Setup

Using [`uv`](https://github.com/astral-sh/uv):

```bash
uv pip install -e .
```

---

## Usage

### Command Line Interface

#### Single File Conversion
```bash
uv run ulg2mcap flight_log.ulg -o flight_log.mcap
```

#### Batch Conversion (Directory)
```bash
uv run ulg2mcap logs/ -o mcap_output/ --recursive
```

#### CLI Options
```text
positional arguments:
  input_path            Path to an input .ulg file or directory containing .ulg files.

options:
  -o, --output OUTPUT   Output .mcap file path or output directory for batch conversion.
  --compress {zstd,lz4,none}
                        Compression mode for MCAP chunks (default: zstd).
  -r, --recursive       Recursively search subdirectories when input_path is a directory.
  --include-topics INCLUDE_TOPICS
                        Comma or space-separated topic names to include.
  --exclude-topics EXCLUDE_TOPICS
                        Comma or space-separated topic names to exclude.
  -v, --verbose         Print verbose conversion output.
```

### Python Library Usage

```python
from ulg2mcap import ULog2MCAPConverter

converter = ULog2MCAPConverter(compression="zstd")
converter.convert("input_log.ulg", "output_log.mcap")
```

---

## Running Tests

```bash
uv run --extra test pytest tests/
```

The sample-log tests skip unless `ULG2MCAP_SAMPLE_ULG` points at a `.ulg` file.

## License

MIT. See [LICENSE](LICENSE).
