"""Join independent startup work before releasing any of its owned resources."""
import asyncio


async def start_together(*operations):
    """Cancel and join siblings on failure or cancellation, preserving input order."""
    jobs = [asyncio.create_task(operation) for operation in operations]
    try:
        return await asyncio.gather(*jobs)
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
