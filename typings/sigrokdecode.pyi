"""Type stub for sigrokdecode module."""

# Constants
SRD_CONF_SAMPLERATE: int
OUTPUT_PYTHON: int
OUTPUT_ANN: int
OUTPUT_BINARY: int
OUTPUT_META: int

# Base Decoder class
class Decoder:
    api_version: int
    id: str
    name: str
    longname: str
    desc: str
    license: str
    inputs: list[str]
    outputs: list[str]
    tags: list[str]
    channels: tuple
    options: tuple
    annotations: tuple
    annotation_rows: tuple
    binary: tuple
    
    def __init__(self) -> None: ...
    def metadata(self, key: int, value: any) -> None: ...
    def start(self) -> None: ...
    def put(self, ss: int, es: int, data_type: int, data: any) -> None: ...
    def register(self, output_type: int, *, meta: tuple = None) -> int: ...
