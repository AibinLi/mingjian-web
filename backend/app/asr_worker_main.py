import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.app.asr_worker:app",
        host="0.0.0.0",
        port=8003,
        log_level="info"
    )
