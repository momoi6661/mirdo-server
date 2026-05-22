from __future__ import annotations

import os
from pathlib import Path

import uvicorn


def main() -> None:
    os.chdir(Path(__file__).resolve().parent)
    uvicorn.run("app.main:app", host="127.0.0.1", port=5678, reload=False)


if __name__ == "__main__":
    main()
