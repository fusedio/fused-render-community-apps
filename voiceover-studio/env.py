import importlib.metadata
import importlib.util
import sys


def main():
    spec = importlib.util.find_spec("mlx_audio")
    version = None
    if spec is not None:
        try:
            version = importlib.metadata.version("mlx-audio")
        except Exception:
            version = "unknown"
    return {"mlx_audio": version, "python": sys.version.split()[0]}
