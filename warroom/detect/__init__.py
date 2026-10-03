"""warroom/detect — non-LLM time-series anomaly prediction + bad-data validation.
Owner: detect builder. Public: StreamDetector, validate_reading."""
from warroom.detect.detector import StreamDetector
from warroom.detect.validate import validate_reading

__all__ = ["StreamDetector", "validate_reading"]
