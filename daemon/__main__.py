"""Entry point: python -m daemon"""
import asyncio
from daemon.main import main

if __name__ == "__main__":
    asyncio.run(main())
