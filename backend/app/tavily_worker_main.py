"""Module entry point for the isolated Tavily retrieval worker."""
from .tavily_worker import app

import uvicorn


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level="info")
