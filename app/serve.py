"""Production entry point: one worker owns the coordinator and SQLite transactions."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
